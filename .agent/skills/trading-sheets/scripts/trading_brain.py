"""Trading Brain - daily learner.

Reads the trading-brain journal sheet, detects NEW trades since the last run
(append-only diff per tab), computes stats (cumulative month, today, delta since
last learn), and writes a plain-language 'what is happening / what to improve'
summary via DeepSeek (cheap) with a deterministic fallback when no API key.

Read-only against the spreadsheet. Snapshots, seen-tracking and the learned
summary all live under .agent/workspaces/personal/state/trading_sheets/ (gitignored).

Usage:
    python trading_brain.py learn          # diff + summarize (skips LLM when no new rows)
    python trading_brain.py learn --force  # re-summarize even without new rows
    python trading_brain.py status         # what is stored right now
    python trading_brain.py history        # prior learn runs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent.parent / '.agent' / 'scripts'))

from sheets_reader import (  # noqa: E402
    SPREADSHEET_ID, STATE_DIR, _build_service, _all_values,
)

from deepseek_call import call as deepseek_call  # noqa: E402

WIB = ZoneInfo('Asia/Jakarta')
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=WIB)

# Ordering of strategies for output (CURRENT first, then ATR_D).
STRATEGY_KEYS = ('CURRENT', 'ATR_D')

# Known labels the engine writes in the Strategy column of each tab.
STRATEGY_MAP = {'BB20_fixed_tp_sl': 'CURRENT', 'BB20_atr_dynamic': 'ATR_D'}

# Context the LLM uses so its suggestions stay grounded in the actual paper
# engine (kept in sync with the knowledge store + SKILL.md facts).
ENGINE_CONTEXT = (
    "Both are Paper futures traders on Binance USDT-M, leverage 4x. "
    "CURRENT = BB20_fixed_tp_sl: entry on BB(20,2) touch, fixed TP 1.50% / SL 0.75%, "
    "PnL% lands flat at +6 / -3 after leverage, single pair (ASTERUSDT). "
    "ATR_D = BB20_atr_dynamic: same entry, exits on ATR(14): TP 2.0x ATR / SL 1.0x ATR "
    "(variable PnL%), two pairs (ASTERUSDT + SUIUSDT) with SEPARATE ~$1000 balances each. "
    "Both trade 05:30-09:00 WIB, skip Fri/Sat/Sun, max ~3 trades/day. Ambition: ~2R:1R "
    "reward:risk (TP 2x/SL 1x). Known artifact: when an SL hits mid-candle, the engine "
    "can re-enter the same signal on the same candle (instant flip-flop)."
)


def _today():
    return datetime.now(WIB).date()


def _serial_to_dt(serial):
    """Convert an Excel serial (Date integer + time fraction) to an aware datetime."""
    if serial in (None, ''):
        return None
    try:
        v = float(serial)
    except (TypeError, ValueError):
        return None
    return EXCEL_EPOCH + timedelta(days=v)


def _fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M') if dt else '-'


def _parse_number(v):
    if v in (None, ''):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_trades(values):
    """Turn raw sheet rows into trade dicts, keyed by the known A:R header."""
    if not values or len(values) < 2:
        return []
    header = values[0]
    trades = []
    for row in values[1:]:
        rec = {}
        for i, name in enumerate(header):
            val = row[i] if i < len(row) else ''
            if name in ('Date', 'Entry Time', 'Exit Time'):
                rec[name] = _serial_to_dt(val)
            elif name in ('Quantity', 'Leverage', 'PnL ($)', 'PnL (%)',
                          'Balance Before', 'Balance After',
                          'Entry Price', 'Exit Price'):
                rec[name] = _parse_number(val)
            else:
                rec[name] = val
        if rec.get('PnL ($)') is None:
            continue  # empty/partial row
        trades.append(rec)
    return trades


def strategy_stats(trades):
    """Mirror trading-brain summary_updater.calculate_stats (kept identical so the
    dashboard comparison agrees with the sheet's own SUMMARY tab)."""
    pnls = [t['PnL ($)'] for t in trades if t.get('PnL ($)') is not None]
    if not pnls:
        return None
    total = len(pnls)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    win_count, lose_count = len(winners), len(losers)
    win_rate = win_count / total * 100 if total else 0
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    net_profit = sum(pnls)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    avg_win = gross_profit / win_count if win_count else 0
    avg_loss = gross_loss / lose_count if lose_count else 0
    peak = running = max_dd = 0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    expectancy = net_profit / total if total else 0
    return {
        'trades': total, 'wins': win_count, 'losses': lose_count,
        'win_rate': round(win_rate, 1), 'profit_factor': round(profit_factor, 2),
        'net_profit': round(net_profit, 2), 'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2), 'max_drawdown': round(max_dd, 2),
        'expectancy': round(expectancy, 2),
    }


def trade_line(t):
    """One compact, evidence-carrying line per trade for the LLM prompt."""
    dt = _fmt_dt(t.get('Date'))
    side = t.get('Side')
    pair = t.get('Pair')
    pnl = t.get('PnL ($)')
    pct = t.get('PnL (%)')
    reason = t.get('Exit Reason')
    bal = t.get('Balance After')
    return (f'{dt} {side:5} {pair:10} PnL ${pnl:+.2f} ({pct:+.2f}%) '
            f'after-bal ${bal:,.2f} | {reason}')


def _read_tab_data(service, strategy_label, month_name):
    """Read the month tab for a strategy. Returns (tab_name, trades)."""
    tab = f'{strategy_label} {month_name}'
    try:
        values = _all_values(service, tab)
    except Exception:
        return tab, []
    return tab, parse_trades(values)


def _month_name():
    return _today().strftime('%b %Y')


def _load_seen():
    path = STATE_DIR / 'seen.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_seen(seen):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / 'seen.json').write_text(json.dumps(seen, indent=2), encoding='utf-8')


def _load_state():
    path = STATE_DIR / 'trading_brain.json'
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / 'trading_brain.json').write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')


def _append_history(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        'learned_at': state.get('learned_at'),
        'new_records': state.get('new_records'),
        'has_llm': state.get('has_llm'),
    }
    month = state.get('stats', {}).get('month') or {}
    for k in STRATEGY_KEYS:
        s = month.get(k)
        if s:
            rec[f'{k}_trades'] = s.get('trades')
            rec[f'{k}_net'] = s.get('net_profit')
            rec[f'{k}_wr'] = s.get('win_rate')
    with open(STATE_DIR / 'history.jsonl', 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _build_prompt(state, new_trades, month_trades):
    month = state['stats']['month']
    delta = state['stats']['delta']
    today = state['stats']['today']

    def table(k):
        s = month.get(k)
        if not s:
            return f'  {k}: no trades this month'
        return (f'  {k}: {s["trades"]} trades, win={s["win_rate"]}%, '
                f'PF={s["profit_factor"]}, net ${s["net_profit"]:+.2f}, '
                f'exp ${s["expectancy"]:+.2f}/trade, maxDD ${s["max_drawdown"]:.2f}')
    month_lines = '\n'.join(table(k) for k in STRATEGY_KEYS)

    delta_lines = []
    for k in STRATEGY_KEYS:
        s = delta.get(k)
        row = today.get(k)
        tag = '  %s' % k
        if s:
            delta_lines.append(f'{tag}: since last learn {s["trades"]} trades, '
                               f'net ${s["net_profit"]:+.2f}, WR {s["win_rate"]}%')
        if row:
            delta_lines.append(f'{tag}: today so far {row["trades"]} trades, '
                               f'net ${row["net_profit"]:+.2f}, WR {row["win_rate"]}%')

    new_lines = [trade_line(t) for t in new_trades[-20:]]
    if len(new_trades) > 20:
        new_lines.insert(0, f'(showing last 20 of {len(new_trades)} new trades)')

    return (
        'You are the trading analyst for a personal paper-trading system. '
        f'ENGINE: {ENGINE_CONTEXT}\n\n'
        f'MONTH SO FAR ({_month_name()}):\n{month_lines}\n\n'
        f'DELTA / TODAY:\n' + ('\n'.join(delta_lines) if delta_lines else '  no trades today yet') +
        f'\n\nNEW TRADES SINCE LAST LEARN ({len(new_trades)}):\n'
        + '\n'.join(new_lines) +
        '\n\nWrite a concise markdown analysis with EXACTLY two sections:\n'
        '## What is happening\n'
        '## What to improve\n'
        'Rules: base every claim on the numbers/trades above only, never invent '
        'external facts; note direction (long/short) and pair trends; compare '
        'CURRENT vs ATR_D; point out concrete, actionable engine improvements '
        '(e.g. exit sizing, timing, same-candle re-entry artifact, pair '
        'concentration, position sizing, things worth changing in the trading-brain '
        'code). Keep each section under 90 words, plain flowing prose, no headers '
        'beyond the two.\n'
    )


def _deterministic_fallback(state):
    month = state['stats']['month']
    lines = []
    for k in STRATEGY_KEYS:
        s = month.get(k)
        if s:
            lines.append(f'{k}: {s["trades"]} trades, WR {s["win_rate"]}%, '
                         f'PF {s["profit_factor"]}, net ${s["net_profit"]:+.2f}.')
    net_full = ' '.join(lines)
    what = (f'Since last learning, {state["new_records"]} new trade(s) were logged. '
            f'{net_full}')
    what_imp = ('Run `validation`: check exit sizing vs ATR (TP 2x vs SL 1x) and '
                'the same-candle re-entry artifact; verify with the SUMMARY tab '
                '(update it manually) before changing engine logic.')
    return {'what_happening': what, 'improvements': what_imp}


def _summarize(state, new_trades, month_trades):
    prompt = _build_prompt(state, new_trades, month_trades)
    ok, text, meta = deepseek_call(prompt, workspace='personal', max_tokens=900)
    if not ok:
        return _deterministic_fallback(state), False, meta.get('reason', 'no key')
    # Split the two sections out of the LLM markdown.
    what = ''
    imp = ''
    current = None
    for line in (text or '').splitlines():
        l = line.strip()
        if l.startswith('## What is happening'):
            current = 'what'
            continue
        if l.startswith('## What to improve'):
            current = 'imp'
            continue
        if not l or l.startswith('#') or l.startswith('**'):
            continue
        if current == 'what':
            what += line + '\n'
        elif current == 'imp':
            imp += line + '\n'
    if not what and not imp:
        what, imp = text, ''
    return {'what_happening': what.strip(), 'improvements': imp.strip()}, True, None


def learn(force=False):
    service = _build_service()
    seen = _load_seen()
    month_name = _month_name()

    fresh = {}
    stats = {'month': {}, 'today': {}, 'delta': {}}
    recent = []
    new_all = []
    total_new = 0

    today = _today()
    for label in STRATEGY_KEYS:
        tab, trades = _read_tab_data(service, label, month_name)
        prev_count = int(seen.get(tab, {}).get('rows', 0)) if isinstance(seen.get(tab), dict) else 0
        rows_now = len(trades)
        new_trades = trades[prev_count:] if prev_count else trades
        # A truncation/correction upstream: rows removed -> treat all as seen not new.
        if prev_count and rows_now < prev_count:
            new_trades = []
        if label == 'CURRENT':
            fresh[tab] = {'rows': rows_now, 'paired': 'CURRENT'}
        else:
            fresh[tab] = {'rows': rows_now, 'paired': 'ATR_D'}

        stats['month'][label] = strategy_stats(trades)
        stats['today'][label] = strategy_stats(
            [t for t in trades if t.get('Date') and t['Date'].date() == today])
        stats['delta'][label] = strategy_stats(new_trades)

        total_new += len(new_trades)
        new_all.extend(new_trades)
        if trades:
            recent = (recent + trades)[-10:]

    now_iso = datetime.now(WIB).isoformat(timespec='seconds')
    state = {
        'learned_at': now_iso, 'checked_at': now_iso,
        'stats': {k: v for k, v in stats.items()},
    }

    has_new = total_new > 0
    if not has_new and not force:
        # Keep last learned summary; only refresh the check timestamp.
        prev = _load_state() or {}
        prev['checked_at'] = now_iso
        prev['new_records'] = 0
        _save_state(prev)
        print(f'no new records (checked {now_iso}); last learn: {prev.get("learned_at", "-")}')
        return {'ok': True, 'new_records': 0, 'learned': False,
                'checked_at': now_iso, 'state_path': str(STATE_DIR / 'trading_brain.json')}

    if force and not has_new:
        print(f'--force: re-summarizing with no new records ({month_name})')

    summary, has_llm, llm_note = _summarize(state, new_all, [])

    state.update({
        'new_records': total_new,
        'has_llm': has_llm,
        'llm_note': llm_note,
        'summary': summary,
        'recent_trades': [
            {
                'date': _fmt_dt(t.get('Date')),
                'strategy': STRATEGY_MAP.get(t.get('Strategy'), t.get('Strategy')),
                'pair': t.get('Pair'), 'side': t.get('Side'),
                'entry': _fmt_dt(t.get('Entry Time') or t.get('Date')),
                'exit': _fmt_dt(t.get('Exit Time')),
                'entry_price': t.get('Entry Price'), 'exit_price': t.get('Exit Price'),
                'qty': t.get('Quantity'), 'leverage': t.get('Leverage'),
                'pnl': t.get('PnL ($)'), 'pnl_pct': t.get('PnL (%)'),
                'balance_after': t.get('Balance After'),
                'exit_reason': t.get('Exit Reason'),
            }
            for t in new_all[-10:] or recent
        ],
        'month': month_name,
        'seen': fresh,
    })
    _save_state(state)
    _save_seen(fresh)
    _append_history(state)
    print(f'learned {total_new} new record(s) at {now_iso} '
          f'({"LLM" if has_llm else "fallback"})')
    return {'ok': True, 'new_records': total_new, 'learned': True,
            'learned_at': now_iso, 'has_llm': has_llm,
            'month': month_name, 'state_path': str(STATE_DIR / 'trading_brain.json')}


def status():
    state = _load_state()
    if not state:
        print('no trading_brain.json state yet - run `trading_brain.py learn` first')
        return
    print(f'learned_at : {state.get("learned_at")}')
    print(f'checked_at : {state.get("checked_at")}')
    print(f'new_records: {state.get("new_records")}')
    print(f'LLM summary: {"yes" if state.get("has_llm") else "no (fallback)"}')
    print(f'month      : {state.get("month")}')
    for k in STRATEGY_KEYS:
        s = (state.get('stats', {}).get('month') or {}).get(k)
        if s:
            print(f'  {k:8}: {s["trades"]:>3} trades  WR {s["win_rate"]:>5}%  '
                  f'PF {s["profit_factor"]:>5}  net ${s["net_profit"]:+.2f}')
    if state.get('summary'):
        print('-- what is happening --')
        print((state.get('summary') or {}).get('what_happening', '')[:400])


def history(limit=20):
    path = STATE_DIR / 'history.jsonl'
    if not path.exists():
        print('no history yet')
        return
    for line in path.read_text(encoding='utf-8').splitlines()[-limit:]:
        try:
            print(json.dumps(json.loads(line), ensure_ascii=False))
        except ValueError:
            continue


def main():
    ap = argparse.ArgumentParser(description='Trading Brain daily learner')
    ap.add_argument('cmd', choices=['learn', 'status', 'history'])
    ap.add_argument('--force', action='store_true',
                    help='re-summarize even with no new records')
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.cmd == 'learn':
        result = learn(force=args.force)
        if not result.get('ok'):
            sys.exit(1)
    elif args.cmd == 'status':
        status()
    elif args.cmd == 'history':
        history()


if __name__ == '__main__':
    main()