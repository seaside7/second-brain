/* ═══════════════════════════════════════════════════════════════════
   tab-trading.js — 📈 Trading Brain (personal only)

   Daily journal learner UI: shows the last learned summary (what is
   happening / what to improve), month-to-date stats for CURRENT vs
   ATR_D, new-record deltas and recent trades. "Learn now" triggers a
   fresh read + LLM summary via POST /api/trading/learn (read-only vs
   the spreadsheet; nothing here writes to the journal).
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const TradingTab = (() => {
  const toast = (msg, ok) => {
    try { Comp.toast(String(msg || 'Unknown error'), !!ok); }
    catch (_) { alert(String(msg || 'Unknown error')); }
  };

  /* strategies order shared with the backend */
  const KEYS = ['CURRENT', 'ATR_D'];
  const HDR = { CURRENT: 'BB20 fixed TP/SL', ATR_D: 'BB20 + ATR(14) exits' };

  const money = v => (v == null ? '-' : `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const signedMoney = v => (v == null ? '-' : `${Number(v) >= 0 ? '+' : ''}${money(v)}`);
  const money0 = v => (v == null ? '-' : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 })}`);
  const num = (v, d = 1) => (v == null ? '-' : Number(v).toLocaleString('en-US', { maximumFractionDigits: d }));
  const pct = v => (v == null ? '-' : `${num(v, 1)}%`);
  const when = iso => {
    if (!iso) return '-';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso)
      : d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  };

  let _busy = false;

  async function _post(path, body, timeoutMs) {
    const ctrl = new AbortController();
    const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
    try {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? '{}' : JSON.stringify(body),
        signal: ctrl.signal,
      });
      let data = null;
      try { data = await res.json(); } catch (_) {}
      if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status} on ${path}`);
      return data;
    } finally { if (timer) clearTimeout(timer); }
  }

  /* stat tile - glanceable hero number */
  const tile = (label, value, sub, cls) => `
    <div class="stat-tile ${cls || ''}">
      <span class="stat-label">${label}</span>
      <span class="stat-value">${value}</span>
      <span class="stat-sub">${sub || ''}</span>
    </div>`;

  const summaryCard = (icon, title, text) => {
    const body = (text || '').trim()
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .split('\n').map(l =>
        l.trim() ? `<p class="trading-prose-line${l.trim().startsWith('-') ? ' is-bullet' : ''}">${l.trim().replace(/^-\s*/, '')}</p>` : '')
      .join('');
    return Comp.card({ key: `trading-${title.replace(/\s+/g, '-').toLowerCase()}`, icon, title,
      open: true, body: body || '<p class="row-note">No summary yet - click Learn now.</p>' });
  };

  const metricRow = (label, cv, av, winner, fmt) => `
    <tr>
      <td class="trading-metric">${U.esc(label)}</td>
      <td class="num">${fmt(cv)}</td>
      <td class="num">${fmt(av)}</td>
      <td>${winner ? `<span class="trading-winner">${U.esc(winner)}</span>` : '-'}</td>
    </tr>`;

  const comparisonCard = st => {
    const s = st.month || {};
    const rows = [
      ['Total Trades', s.CURRENT?.trades, s.ATR_D?.trades, 'higher', v => num(v, 0)],
      ['Win Rate %', s.CURRENT?.win_rate, s.ATR_D?.win_rate, 'higher', pct],
      ['Profit Factor', s.CURRENT?.profit_factor, s.ATR_D?.profit_factor, 'higher', v => num(v, 2)],
      ['Net Profit $', s.CURRENT?.net_profit, s.ATR_D?.net_profit, 'higher', signedMoney],
      ['Avg Win $', s.CURRENT?.avg_win, s.ATR_D?.avg_win, 'higher', money0],
      ['Avg Loss $', s.CURRENT?.avg_loss, s.ATR_D?.avg_loss, 'lower', money0],
      ['Max Drawdown $', s.CURRENT?.max_drawdown, s.ATR_D?.max_drawdown, 'lower', money0],
      ['Expectancy $/trade', s.CURRENT?.expectancy, s.ATR_D?.expectancy, 'higher', v => v == null ? '-' : signedMoney(v)],
    ];
    const body = rows.map(([l, cv, av, dir]) => {
      let w = 'TIE';
      if (cv != null && av != null) {
        const better = (dir === 'higher') ? (a, b) => a > b : (a, b) => a < b;
        w = better(cv, av) ? 'CURRENT' : better(av, cv) ? 'ATR_D' : 'TIE';
      } else if (cv != null) w = 'CURRENT';
      else if (av != null) w = 'ATR_D';
      return metricRow(l, cv, av, w, dir === 'higher' ? signedMoney : money0);
    }).join('');

    const today = st.today || {};
    const delta = st.delta || {};
    const todayBits = KEYS.map(k =>
      today[k] ? `<span class="trading-chip">${k} today: ${today[k].trades} tr / ${signedMoney(today[k].net_profit)}</span>` : '').join('');
    const deltaBits = KEYS.map(k =>
      delta[k] ? `<span class="trading-chip">${k} since learn: ${delta[k].trades} tr / ${signedMoney(delta[k].net_profit)}</span>` : '').join('');
    const chips = (todayBits || deltaBits) ? `
      <div class="trading-chips">${todayBits}${deltaBits}</div>` : '';

    return Comp.card({
      key: 'trading-compare', icon: '⚖️', title: `Month-to-date (${U.esc(st.month || '')})`,
      count: num(st.new_records, 0) + ' new',
      open: true,
      body: `
        <div class="table-scroll">
          <table class="trading-table">
            <thead><tr><th>Metric</th>
              <th class="num">CURRENT<br><span class="trading-sub">${HDR.CURRENT}</span></th>
              <th class="num">ATR_D<br><span class="trading-sub">${HDR.ATR_D}</span></th>
              <th>Winner</th></tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        ${chips}`,
    });
  };

  const tradesCard = rows => {
    if (!rows || !rows.length) return '';
    const tr = rows.map(t => `
      <tr>
        <td class="nowrap">${U.esc(t.date)}</td>
        <td>${U.esc(t.strategy || '')}</td>
        <td>${U.esc(t.pair || '')}</td>
        <td class="num">${U.esc(t.side || '')}</td>
        <td class="nowrap num">${U.esc(t.entry || '')}</td>
        <td class="nowrap num">${U.esc(t.exit || '')}</td>
        <td class="num">${num(t.entry_price, 4)}</td>
        <td class="num">${num(t.exit_price, 4)}</td>
        <td class="num">${t.pnl == null ? '-' : `<span class="${t.pnl >= 0 ? 'trading-pos' : 'trading-neg'}">${signedMoney(t.pnl)}</span>`}</td>
        <td class="num trading-sub">${pct(t.pnl_pct)}</td>
        <td class="trading-sub">${U.esc(t.exit_reason || '')}</td>
      </tr>`).join('');
    return Comp.card({
      key: 'trading-recent', icon: '🧾', title: 'Recent trades',
      count: num(rows.length, 0), open: false,
      body: `<div class="table-scroll">
        <table class="trading-table">
          <thead><tr><th>Date</th><th>Strat</th><th>Pair</th><th>Side</th>
            <th>Entry</th><th>Exit</th><th class="num">Entry</th><th class="num">Exit price</th>
            <th class="num">PnL $</th><th class="num">PnL %</th><th>Reason</th></tr></thead>
          <tbody>${tr}</tbody>
        </table></div>`,
    });
  };

  function render(card, payload) {
    if (!card) return;
    const d = payload.state;
    if (!d) {
      card.innerHTML = Comp.emptyState({
        icon: '📈', title: 'No Trading Brain learning yet',
        hint: 'Nothing has been read from the journal sheet yet. Hit "Learn now" to run the first diff + summary.',
      }) + `<p style="text-align:center"><button class="btn" id="trading-learn-btn">⟳ Learn now</button></p>`;
      const b = $id('trading-learn-btn');
      if (b) b.addEventListener('click', learnNow);
      return;
    }

    const s = d.stats || {};
    const m = s.month || {};
    const bestNet = KEYS.map(k => m[k]?.net_profit ?? -Infinity).filter(v => v !== -Infinity);
    const bestK = KEYS.find(k => m[k]?.net_profit === Math.max(...bestNet));

    const header = `
      <div class="trading-head">
        <div class="trading-head-title">
          <h2 class="trading-title">📈 Trading Brain</h2>
          <div class="trading-meta">
            <span>Journal: <b>Daily Trade Journal</b> · ${U.esc(d.month || '')}</span>
            <span>Learned ${when(d.learned_at)}</span>
            <span>Checked ${when(d.checked_at)}</span>
          </div>
        </div>
        <button class="btn btn-primary" id="trading-learn-btn" ${_busy ? 'disabled' : ''}>${_busy ? 'Learning…' : '⟳ Learn now'}</button>
      </div>
      <div class="trading-chips">
        <span class="trading-chip">${d.has_llm ? '🧠 LLM summary' : '📄 deterministic summary'}</span>
        <span class="trading-chip"><b>${num(d.new_records, 0)}</b> new record(s) at last learn</span>
        ${d.llm_note ? `<span class="trading-chip is-dim">fallback reason: ${U.esc(d.llm_note)}</span>` : ''}
      </div>`;

    const tiles = `
      <div class="grid-cards">
        ${tile('Month net profit $', signedMoney(m[bestK]?.net_profit), `${U.esc(bestK || '')} leads this month`, m[bestK]?.net_profit >= 0 ? '' : 'stat--warn')}
        ${tile('ATR_D win rate', pct(m.ATR_D?.win_rate), `${num(m.ATR_D?.trades, 0)} trades · exp ${signedMoney(m.ATR_D?.expectancy)}/trade`)}
        ${tile('ATR_D profit factor', num(m.ATR_D?.profit_factor, 2), `maxDD ${money0(m.ATR_D?.max_drawdown)}`)}
        ${tile('CURRENT win rate', pct(m.CURRENT?.win_rate), `${num(m.CURRENT?.trades, 0)} trades · exp ${signedMoney(m.CURRENT?.expectancy)}/trade`)}
        ${tile('CURRENT profit factor', num(m.CURRENT?.profit_factor, 2), `maxDD ${money0(m.CURRENT?.max_drawdown)}`)}
      </div>`;

    card.innerHTML = [
      header,
      tiles,
      summaryCard('🧭', 'What is happening', d.summary?.what_happening),
      summaryCard('🔧', 'What to improve', d.summary?.improvements),
      comparisonCard(d),
      tradesCard(d.recent_trades),
    ].join('');

    const b = $id('trading-learn-btn');
    if (b) b.addEventListener('click', learnNow);
  }

  async function learnNow() {
    if (_busy) return;
    _busy = true;
    try {
      const r = await _post('/api/trading/learn', {}, 180000);
      if (r && r.new_records != null) {
        toast(r.learned ? `Learned ${r.new_records} new record(s)` : 'No new records - journal unchanged', true);
      }
    } catch (err) {
      toast(err.message, false);
    } finally {
      _busy = false;
      load();
    }
  }

  async function load() {
    const card = $id('tab-trading');
    if (!card) return;
    card.innerHTML = skeleton();
    try {
      const res = await U.fetchJSON('/api/trading/overview');
      render(card, res);
    } catch (err) {
      card.innerHTML = `<div class="load-error">Could not load Trading Brain: ${U.esc(err.message)}</div>`;
    }
  }

  const skeleton = () => `<div class="skeleton"><div class="skeleton-line w-80"></div><div class="skeleton-line"></div><div class="skeleton-line w-60"></div></div>`;

  return { load };
})();

window.Tabs.trading = TradingTab;