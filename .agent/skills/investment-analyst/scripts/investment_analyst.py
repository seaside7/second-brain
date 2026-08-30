#!/usr/bin/env python3
"""investment_analyst.py - the Investment Analyst (personal workspace).

Read-only Indonesian stock (IDX) analyst. Fetches raw market data from Yahoo
Finance's public endpoints and reports it with the discipline of a data
analyst: it separates RAW MARKET FACTS (what the API returned) from CALCULATED
METRICS (derived from those facts, e.g. PBV = price / book value) and NEVER
issues analyst interpretation itself - that is left to the chat assistant with
this data as its grounding input.

Data sources (Yahoo Finance public JSON endpoints, no API key):
  - v8/finance/chart/{SYM}.JK          -> price, change, currency, timestamps
  - v10/finance/quoteSummary/{SYM}.JK  -> fundamentals (requires a crumb token,
    fetched once per run via the v1/test/getcrumb flow)

Design guarantees:
  - floatShares is reported AS A VALUE, never translated into "sold to the
    public vs held back" - that interpretation belongs to the reader.
  - heldPercentInstitutions is reported as an ownership indicator, never
    branded a "whale signal".
  - Every quote carries: ticker, company name, currency, market timestamp,
    source, and data-timing (realtime/delayed/unknown-as-reported).
  - Requests are paced (--delay, default 1.0s) and cached under the personal
    workspace state dir so screening 40-60 tickers respects rate limits.
  - Partial results: if a ticker fails, the run continues and the failure is
    listed in an "unavailable" section. No fabricated numbers, ever.

Usage:
  python3 .agent/skills/investment-analyst/scripts/investment_analyst.py \
      quote --symbols BBCA,ASII
  python3 .agent/skills/investment-analyst/scripts/investment_analyst.py \
      quote --symbols BBCA --json
  python3 .agent/skills/investment-analyst/scripts/investment_analyst.py \
      screen [--sector bank] [--top 10] [--force] [--delay 1.0] [--json]
  python3 .agent/skills/investment-analyst/scripts/investment_analyst.py \
      watchlist
"""
import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlreq

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
SKILL_DIR = BASE_DIR / '.agent' / 'skills' / 'investment-analyst'
WATCHLIST_PATH = SKILL_DIR / 'watchlist.json'
CACHE_PATH = BASE_DIR / '.agent' / 'workspaces' / 'personal' / 'state' / 'investment_cache.json'

WIB = timezone(timedelta(hours=7))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      'Chrome/120.0 Safari/537.36')
CACHE_TTL = timedelta(minutes=10)
SYM_RE = re.compile(r'^[A-Z0-9]{1,4}$')

# Financial statements only change on a new filing, so a long TTL is safe and
# avoids hammering Yahoo. Kept separate from the 10-min market cache.
STATEMENT_CACHE_PATH = BASE_DIR / '.agent' / 'workspaces' / 'personal' / 'state' / 'deepdive_cache.json'
STATEMENT_TTL = timedelta(days=7)

# Modules needed for the deep-dive data package (statements + profile + extras).
STATEMENT_MODULES = ('incomeStatementHistory,incomeStatementHistoryQuarterly,'
                     'balanceSheetHistory,cashflowStatementHistory,'
                     'assetProfile,defaultKeyStatistics,financialData,'
                     'summaryDetail,price')
KS_CLI = BASE_DIR / '.agent' / 'skills' / 'knowledge-store' / 'scripts' / 'knowledge_store.py'
MEMORY_NOTES_PATH = BASE_DIR / '.agent' / 'workspaces' / 'personal' / 'state' / 'memory_notes.json'
DEEP_DIVES_DIR = BASE_DIR / '.agent' / 'workspaces' / 'personal' / 'state' / 'deepdives'


def _now():
    return datetime.now(timezone.utc).astimezone(WIB)


def _fmt_ts(epoch):
    """Epoch seconds -> ISO WIB string, or None."""
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).astimezone(WIB).isoformat(timespec='seconds')
    except Exception:
        return None


# ─────────────────────────────── helpers ───────────────────────────────

def _num(v):
    """Extract a numeric value from a Yahoo number-or-dict field."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get('raw') if v.get('raw') is not None else v.get('fmt')
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except Exception:
        return None


def _dt(v):
    """Best-effort display string from a Yahoo date/dict field."""
    if isinstance(v, dict):
        return v.get('fmt') or v.get('raw')
    if v is None:
        return None
    return str(v)


def _pct(v):
    """Normalize a fraction (0.0588) to a percent string, or None."""
    n = _num(v)
    if n is None:
        return None
    return round(n * 100, 2)


def load_watchlist():
    try:
        doc = json.loads(WATCHLIST_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'sectors': {}, 'note': 'watchlist unreadable'}
    return doc


def all_symbols(wl):
    out = []
    for syms in (wl.get('sectors') or {}).values():
        for s in syms:
            if s not in out:
                out.append(s)
    return out


def load_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'entries': {}}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')


# ─────────────────────────────── yahoo clients ───────────────────────────────

class YahooClient:
    """Small Yahoo Finance client with cookie+crumb handling, pacing, retry."""

    def __init__(self, delay=1.0, force=False):
        self.delay = delay
        self.force = force
        self.opener, self.crumb = self._start()
        self.last_request = 0.0

    def _start(self):
        cj = http.cookiejar.CookieJar()
        op = urlreq.build_opener(urlreq.HTTPCookieProcessor(cj))
        try:
            op.open(urlreq.Request('https://fc.yahoo.com', headers={'User-Agent': UA}), timeout=12)
        except Exception:
            pass  # fc.yahoo.com returning 404 is expected; it still sets the cookie
        try:
            crumb = op.open(
                urlreq.Request('https://query1.finance.yahoo.com/v1/test/getcrumb',
                               headers={'User-Agent': UA}), timeout=12).read().decode().strip()
        except Exception:
            crumb = None
        return op, crumb

    def _pace(self):
        wait = self.delay - (time.time() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.time()

    def _get(self, url, timeout=14):
        self._pace()
        try:
            with self.opener.open(urlreq.Request(url, headers={'User-Agent': UA}),
                                  timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8', 'replace'))
        except urlerror.HTTPError as e:
            return e.code, None
        except Exception as e:
            return None, None

    def chart(self, sym):
        url = ('https://query1.finance.yahoo.com/v8/finance/chart/%s.JK'
               '?interval=1d&range=5d' % sym)
        code, data = self._get(url)
        if code != 200 or not data:
            return None
        result = ((data.get('chart') or {}).get('result') or [])
        if not result:
            return None
        return result[0].get('meta') or {}

    def fundamentals(self, sym):
        if not self.crumb:
            return None
        url = ('https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s.JK'
               '?modules=summaryDetail,financialData,defaultKeyStatistics,price'
               '&crumb=%s' % (sym, self.crumb))
        code, data = self._get(url)
        if code != 200 or not data:
            return None
        result = ((data.get('quoteSummary') or {}).get('result') or [])
        if not result:
            return None
        return result[0]


# ─────────────────────────────── fetch + build ───────────────────────────────

def fetch_quote(sym, client, cache):
    """Fetch one symbol. Returns a normalized quote dict with raw/cached split.

    Layout deliberately separates:
      raw          - exactly what the source returned (facts as reported)
      calculated   - metrics derived from raw values, with the derivation shown
      unavailable  - fields the source did not return
    No interpretation lives here; that is the chat assistant's job."""
    now = _now()
    if client.force:
        cached = None
    else:
        cached = cache['entries'].get(sym)
    if cached and cached.get('fetched_at_utc'):
        try:
            fetched = datetime.strptime(cached['fetched_at_utc'],
                                        '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - fetched) <= CACHE_TTL:
                return cached, 'cache'
        except Exception:
            pass

    chart = client.chart(sym)
    funds = client.fundamentals(sym) if (client.crumb or client.force) else None

    if not chart and not funds:
        unavailable = [sym]
        quote = {'ticker': sym, 'unavailable': True, 'reason': 'no data from source'}
        return quote, 'miss'

    # feed = the actual provider truth if chart failed but fundamentals worked
    quote = {'ticker': sym, 'unavailable': False}

    # ---- raw (facts as reported by the source) ----
    raw = {}
    if chart:
        raw['price'] = _num(chart.get('regularMarketPrice'))
        raw['previous_close'] = _num(chart.get('chartPreviousClose')
                                     or chart.get('previousClose'))
        raw['currency'] = chart.get('currency')
        raw['exchange_name'] = chart.get('fullExchangeName') or chart.get('exchangeName')
        raw['market_timestamp'] = _fmt_ts(chart.get('regularMarketTime'))
        raw['market_state'] = chart.get('marketState')
        raw['timezone'] = chart.get('exchangeTimezoneName')
    if funds:
        sd = funds.get('summaryDetail') or {}
        dk = funds.get('defaultKeyStatistics') or {}
        fd = funds.get('financialData') or {}
        pr = funds.get('price') or {}
        raw['company_name'] = (pr.get('longName') or pr.get('shortName')
                               or (chart or {}).get('shortName'))
        if not raw.get('currency'):
            raw['currency'] = pr.get('currency')
        if not raw.get('market_timestamp'):
            raw['market_timestamp'] = _fmt_ts(_num(pr.get('regularMarketTime')))
        if not raw.get('market_state'):
            raw['market_state'] = pr.get('marketState')
        raw['shares_outstanding'] = _num(dk.get('sharesOutstanding'))
        raw['float_shares'] = _num(dk.get('floatShares'))
        raw['held_percent_institutions'] = _pct(dk.get('heldPercentInstitutions'))
        raw['held_percent_insiders'] = _pct(dk.get('heldPercentInsiders'))
        raw['book_value'] = _num(dk.get('bookValue'))
        raw['trailing_eps'] = _num(dk.get('trailingEps'))
        raw['return_on_equity'] = _pct(fd.get('returnOnEquity'))
        raw['dividend_yield'] = _pct(sd.get('dividendYield'))
        raw['dividend_rate'] = _num(sd.get('dividendRate'))
        raw['trailing_pe'] = _num(sd.get('trailingPE'))
        raw['price_to_book'] = _num(sd.get('priceToBook'))
        raw['fifty_two_week_high'] = _num(sd.get('fiftyTwoWeekHigh'))
        raw['fifty_two_week_low'] = _num(sd.get('fiftyTwoWeekLow'))
        raw['market_cap'] = _num(sd.get('marketCap'))
        raw['source_symbol'] = (pr.get('symbol') or chart.get('symbol'))
        # Enriched fields (used by deep-dive; ignored by basic quote/screen views)
        raw['enterprise_value'] = _num(dk.get('enterpriseValue'))
        raw['enterprise_to_revenue'] = _num(dk.get('enterpriseToRevenue'))
        raw['enterprise_to_ebitda'] = _num(dk.get('enterpriseToEbitda'))
        raw['payout_ratio'] = _pct(dk.get('payoutRatio'))
        raw['last_dividend_value'] = _num(dk.get('lastDividendValue'))
        raw['last_dividend_date'] = _dt(dk.get('lastDividendDate'))
        raw['five_year_avg_dividend_yield'] = _pct(dk.get('fiveYearAvgDividendYield'))
        raw['profit_margin'] = _pct(fd.get('profitMargins'))
        raw['operating_margin'] = _pct(fd.get('operatingMargins'))
        raw['gross_margin'] = _pct(fd.get('grossMargins'))
        raw['return_on_assets'] = _pct(fd.get('returnOnAssets'))
        raw['total_debt'] = _num(fd.get('totalDebt'))
        raw['total_cash'] = _num(fd.get('totalCash'))
        raw['revenue_growth'] = _pct(fd.get('revenueGrowth'))
        raw['earnings_growth'] = _pct(fd.get('earningsGrowth'))

    quote['raw'] = {k: v for k, v in raw.items() if v is not None}
    quote['source'] = 'Yahoo Finance (public JSON endpoints: v8/chart + v10/quoteSummary)'

    # data timing - honest about what the source tells us
    timing = 'not reported by source'
    if chart and chart.get('marketState'):
        timing = 'live market state: %s' % chart['marketState']
    elif funds and (funds.get('price') or {}).get('marketState'):
        timing = 'live market state: %s' % funds['price']['marketState']
    quote['data_timing'] = timing

    # ---- calculated (derived from raw, derivation shown) ----
    calc = {}
    price = raw.get('price')
    bv = raw.get('book_value')
    if price is not None and bv:
        calc['pbv'] = round(price / bv, 2)
        calc['pbv_formula'] = 'price / book_value'
    so = raw.get('shares_outstanding')
    fs = raw.get('float_shares')
    if so and fs:
        calc['float_pct'] = round(fs / so * 100, 2)
        calc['float_pct_formula'] = 'float_shares / shares_outstanding * 100'
    if price is not None and raw.get('previous_close'):
        prev = raw['previous_close']
        if prev:
            calc['change'] = round(price - prev, 2)
            calc['change_pct'] = round((price - prev) / prev * 100, 2)
            calc['change_formula'] = '(price - previous_close) / previous_close * 100'
    quote['calculated'] = {k: v for k, v in calc.items() if v is not None}

    # ---- unavailable ----
    missing = []
    if not chart:
        missing.append('chart/price')
    if not funds:
        missing.append('fundamentals (quoteSummary)')
    else:
        probes = {
            'shares_outstanding': dk.get('sharesOutstanding'),
            'float_shares': dk.get('floatShares'),
            'return_on_equity': fd.get('returnOnEquity'),
            'dividend_yield': sd.get('dividendYield'),
            'book_value': dk.get('bookValue'),
            'held_percent_institutions': dk.get('heldPercentInstitutions'),
        }
        for label, val in probes.items():
            if _num(val) is None:
                missing.append(label.replace('_', ' '))
    if missing:
        quote['unavailable'] = missing
    else:
        quote.pop('unavailable', None)

    quote['fetched_at_utc'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    cache['entries'][sym] = quote
    return quote, 'fetch'


# ─────────────────────────────── commands ───────────────────────────────

def cmd_quote(args):
    cache = load_cache()
    client = YahooClient(delay=args.delay, force=args.force)
    out = {'quotes': [], 'unavailable': []}
    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    for sym in symbols:
        q, how = fetch_quote(sym, client, cache)
        q['fetch'] = how
        if q.get('unavailable') is True:
            out['unavailable'].append({'ticker': sym, 'reason': q.get('reason')})
        else:
            out['quotes'].append(q)
    save_cache(cache)
    return out


def cmd_screen(args):
    wl = load_watchlist()
    cache = load_cache()
    client = YahooClient(delay=args.delay, force=args.force)
    syms = []
    if args.sector:
        syms = list((wl.get('sectors') or {}).get(args.sector, []))
        if not syms:
            return {'error': 'unknown sector %r' % args.sector,
                    'sectors': sorted((wl.get('sectors') or {}).keys())}
    else:
        syms = all_symbols(wl)
    if args.top:
        syms = syms[:args.top]

    quotes, unavailable = [], []
    for sym in syms:
        q, how = fetch_quote(sym, client, cache)
        q['fetch'] = how
        if q.get('unavailable') is True:
            unavailable.append({'ticker': sym, 'reason': q.get('reason')})
        else:
            quotes.append(q)

    # sorted screen (no input number -> leave insertion order)
    order = args.sort or 'ticker'
    reverse = args.desc
    if order in ('pbv', 'roe', 'dividend_yield', 'float_pct', 'price'):
        def keyof(q):
            if order in ('pbv', 'roe', 'float_pct'):
                return (q.get('calculated') or {}).get(order)
            if order == 'dividend_yield':
                return (q.get('raw') or {}).get('dividend_yield')
            return (q.get('raw') or {}).get('price')
        quotes = sorted(
            (q for q in quotes if keyof(q) is not None),
            key=keyof, reverse=reverse)

    save_cache(cache)
    return {'quotes': quotes, 'unavailable': unavailable}


def cmd_watchlist(args):
    wl = load_watchlist()
    if args.action == 'list':
        return wl
    # add/remove mutate watchlist.json
    sectors = wl.get('sectors') or {}
    if args.action == 'add':
        for sym in args.symbols:
            sym = sym.upper()
            if not SYM_RE.match(sym):
                return {'error': 'invalid ticker %r (1-4 letters/digits)' % sym}
            placed = False
            for s in sectors.values():
                if sym in s:
                    placed = True
                    break
            if not placed:
                sectors.setdefault('unassigned', []).append(sym)
        wl['sectors'] = sectors
        wl['updated_wib'] = _now().isoformat(timespec='seconds')
        WATCHLIST_PATH.write_text(json.dumps(wl, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
        return {'status': 'added', 'watchlist': wl}
    if args.action == 'remove':
        removed = []
        for sym in args.symbols:
            sym = sym.upper()
            for sector_name, s in list(sectors.items()):
                if sym in s:
                    s.remove(sym)
                    removed.append(sym)
        if removed:
            wl['sectors'] = sectors
            wl['updated_wib'] = _now().isoformat(timespec='seconds')
            WATCHLIST_PATH.write_text(json.dumps(wl, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
        return {'status': 'removed', 'removed': removed, 'watchlist': wl}
    return {'error': 'unknown watchlist action'}


def cmd_intent(args):
    """List watchlist tickers whose exact symbol appears in the text, or a
    skipped (English-word) ticker ONLY when investment context is also present.

    Used by the chat intent hook so only real watchlist tickers (not random
    uppercase words) trigger a data fetch. Skip list guards ordinary words
    like BIRD/LEAD/GOTO that would otherwise false-positive."""
    text = (args.text or '').upper()
    wl = load_watchlist()
    skip = set(wl.get('intent_skip') or [])
    kw = [k.lower() for k in (wl.get('intent_keywords') or [])]
    lower = (args.text or '').lower()
    has_ctx = any(k in lower for k in kw)
    found = []
    for s in all_symbols(wl):
        if not re.search(r'\b%s\b' % re.escape(s), text):
            continue
        if s in skip and not has_ctx:
            continue
        found.append(s)
    return {'matched': sorted(found, key=len, reverse=True)}


# ─────────────────────────────── deep dive ───────────────────────────────

def load_stmt_cache():
    try:
        return json.loads(STATEMENT_CACHE_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'entries': {}}


def save_stmt_cache(cache):
    STATEMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATEMENT_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch_statements(sym, client, stmt_cache, refresh=False):
    """7-day-cached quoteSummary with all statement modules for a symbol.

    Returns (payload, from_cache). Statements only change on a new filing, so a
    long TTL is safe. The payload is the raw module dict (income/balance/cash
    flow history, assetProfile, defaultKeyStatistics, financialData, ...)."""
    if not refresh:
        entry = stmt_cache['entries'].get(sym, {})
        fetched_at = entry.get('fetched_at_utc')
        if entry.get('payload') and fetched_at:
            try:
                fetched = datetime.strptime(fetched_at, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - fetched) <= STATEMENT_TTL:
                    return entry['payload'], True
            except ValueError:
                pass
    if not client.crumb:
        return None, False
    url = ('https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s.JK'
           '?modules=%s&crumb=%s' % (sym, STATEMENT_MODULES, client.crumb))
    code, data = client._get(url)
    if code != 200 or not data:
        return None, False
    result = ((data.get('quoteSummary') or {}).get('result') or [])
    if not result:
        return None, False
    stmt_cache['entries'][sym] = {
        'fetched_at_utc': _now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'payload': result[0],
    }
    return result[0], False


def _income_rows(payload, quarterly=False):
    if not payload:
        return []
    if quarterly:
        items = ((payload.get('incomeStatementHistoryQuarterly') or {})
                 .get('quarterlyIncomeStatements') or [])
    else:
        items = ((payload.get('incomeStatementHistory') or {})
                 .get('incomeStatementHistory') or [])
    out = []
    for r in items:
        rr = {k: _num(v) for k, v in r.items() if k != 'endDate'}
        rr['endDate'] = _dt(r.get('endDate'))
        out.append(rr)
    return out


def _build_annual_statements(rows, shares_outstanding):
    """Normalize annual income rows into {available, latest_period, range,
    periods[{end_date, reported, calculated}]} with growth rates."""
    if not rows:
        return {'available': False,
                'note': 'Yahoo tidak mengembalikan laporan laba-rugi tahunan untuk ticker ini'}
    parsed = []
    for r in rows:
        parsed.append({'end_date': _dt(r.get('endDate')),
                       'reported': {
                           'revenue': r.get('totalRevenue'),
                           'net_income': r.get('netIncome') or r.get('netIncomeApplicableToCommonShares'),
                           'ebit': r.get('ebit'),
                           'income_before_tax': r.get('incomeBeforeTax'),
                           'interest_expense': r.get('interestExpense')}})
    parsed = [p for p in parsed if p['end_date']]
    parsed.sort(key=lambda p: str(p['end_date'])[:10])
    periods = []
    prev = None
    for p in parsed:
        rev, ni = p['reported']['revenue'], p['reported']['net_income']
        calc = {}
        if ni is not None and shares_outstanding:
            calc['eps_approx'] = round(ni / shares_outstanding, 2)
            calc['eps_formula'] = ('net_income / shares_outstanding '
                                   '(perkiraan; Yahoo IDX tidak menyediakan diluted EPS)')
        if rev and ni:
            calc['net_margin_pct'] = round(ni / rev * 100, 2)
            calc['net_margin_formula'] = 'net_income / total_revenue * 100'
        if prev:
            pr, pn = prev['reported']['revenue'], prev['reported']['net_income']
            if rev and pr:
                calc['revenue_growth_yoy_pct'] = round((rev - pr) / pr * 100, 2)
                calc['revenue_growth_formula'] = '(revenue - prev_year_revenue) / prev_year_revenue * 100'
            if ni and pn:
                calc['net_income_growth_yoy_pct'] = round((ni - pn) / pn * 100, 2)
        periods.append({'end_date': p['end_date'],
                        'reported': {k: v for k, v in p['reported'].items() if v is not None},
                        'calculated': {k: v for k, v in calc.items() if v is not None}})
        prev = p
    latest = periods[-1]['end_date'] if periods else None
    rng = None
    if len(periods) > 1 and latest:
        rng = '%s-%s' % (str(periods[0]['end_date'])[:4], str(latest)[:4])
    return {'available': True, 'latest_period': latest, 'range': rng,
            'periods': periods}


def _build_quarterly_statements(rows, shares_outstanding):
    """Normalize quarterly income rows (up to 8) with QoQ and YoY (same quarter
    last year) growth. Returns {available, latest_period, periods[...]}."""
    if not rows:
        return {'available': False,
                'note': 'Laporan kuartalan tidak tersedia dari Yahoo IDX - lihat Missing data'}
    parsed = []
    for r in rows:
        parsed.append({'end_date': _dt(r.get('endDate')),
                       'revenue': r.get('totalRevenue'),
                       'net_income': r.get('netIncome') or r.get('netIncomeApplicableToCommonShares')})
    parsed = [p for p in parsed if p and str(p['end_date'] or '')[:10]]
    parsed.sort(key=lambda p: str(p['end_date'])[:10])
    by_date = {str(p['end_date'])[:10]: p for p in parsed}
    last8 = parsed[-8:]
    periods = []
    prev = None
    for p in last8:
        d = datetime.strptime(str(p['end_date'])[:10], '%Y-%m-%d')
        yoy_peer = by_date.get('%04d-%02d-%02d' % (d.year - 1, d.month, d.day))
        calc = {}
        if prev and p.get('revenue') and prev.get('revenue'):
            calc['revenue_qoq_pct'] = round((p['revenue'] - prev['revenue']) / prev['revenue'] * 100, 2)
            calc['revenue_qoq_formula'] = '(revenue - previous_quarter_revenue) / previous_quarter_revenue * 100'
        if yoy_peer and p.get('revenue') and yoy_peer.get('revenue'):
            calc['revenue_yoy_pct'] = round((p['revenue'] - yoy_peer['revenue']) / yoy_peer['revenue'] * 100, 2)
            calc['revenue_yoy_formula'] = '(revenue - same_quarter_prior_year_revenue) / same_quarter_prior_year_revenue * 100'
        if prev and p.get('net_income') and prev.get('net_income'):
            calc['net_income_qoq_pct'] = round((p['net_income'] - prev['net_income']) / prev['net_income'] * 100, 2)
        if yoy_peer and p.get('net_income') and yoy_peer.get('net_income'):
            calc['net_income_yoy_pct'] = round((p['net_income'] - yoy_peer['net_income']) / yoy_peer['net_income'] * 100, 2)
        if shares_outstanding and p.get('net_income'):
            calc['eps_approx'] = round(p['net_income'] / shares_outstanding, 2)
        periods.append({'end_date': p['end_date'],
                        'reported': {k: v for k, v in p.items() if k != 'end_date' and v is not None},
                        'calculated': {k: v for k, v in calc.items() if v is not None}})
        prev = p
    latest = periods[-1]['end_date'] if periods else None
    return {'available': True, 'latest_period': latest, 'periods': periods}


def _company_info(payload):
    ap = (payload or {}).get('assetProfile') or {}
    return {'sector': ap.get('sector'), 'industry': ap.get('industry'),
            'business_summary': (ap.get('longBusinessSummary') or '').strip(),
            'website': ap.get('website')}


def _search_documents(*queries):
    """Retrieve-first: look for existing reports/notes about this company in the
    knowledge store and the personal memory before asking the user for a doc.

    Returns [{kind, origin, title, date}] - nothing is ever fabricated here."""
    found, seen = [], set()
    for q in queries:
        q = ' '.join(str(q).split()[:4]).strip()
        if not q:
            continue
        try:
            r = subprocess.run([sys.executable, str(KS_CLI), 'search', '--query', q],
                               cwd=str(BASE_DIR), capture_output=True, text=True, timeout=45)
            for line in (r.stdout or '').splitlines():
                m = re.match(r'\s*\[(\w+)\]\s+(.*?)\s+\((\d{4}-\d{2}-\d{2})\)', line)
                if m:
                    key = (m.group(1), m.group(2))
                    if key not in seen:
                        seen.add(key)
                        found.append({'kind': 'knowledge', 'origin': 'knowledge store',
                                      'title': m.group(2), 'date': m.group(3)})
        except Exception:
            pass
    try:
        notes = json.loads(MEMORY_NOTES_PATH.read_text(encoding='utf-8'))
        terms = [str(q).lower() for q in queries if q]
        for entry in (notes.get('entries') or {}).values():
            blob = ('%s %s' % (entry.get('text') or '', entry.get('title') or '')).lower()
            if any(t in blob for t in terms if t):
                found.append({'kind': 'memory', 'origin': 'personal memory',
                              'title': entry.get('title') or entry.get('text') or '',
                              'date': entry.get('created_wib') or entry.get('date')})
    except Exception:
        pass
    return found


def _sector_of(wl, sym):
    for name, syms in (wl.get('sectors') or {}).items():
        if sym in syms:
            return name
    return None


def _missing_data(sector, quarterly_available):
    """The honest missing-data document requests. Each entry names the company
    context-free (the carrier attaches the ticker), the field, the period, the
    suggested official document, and where the field normally sits. Source
    priority is IDX filing/disclosure > audited annual/quarterly > IR material
    > Yahoo Finance - Yahoo is what we have today, so everything else must be
    asked for explicitly when required."""
    items = [
        {'field': 'Laporan keuangan triwulanan/kuartalan (pendapatan & laba per kuartal, YoY)',
         'period': '8 kuartal terakhir' if False else 'Kuartal terakhir + YoY',
         'suggested_document': 'Laporan Keuangan Publikasi Triwulanan dari situs IDX (idx.co.id)',
         'where_normally_found': 'Laporan Keuangan Interim / Publikasi - Laporan Laba Rugi komprehensif'},
        {'field': 'Rincian perubahan modal & saham beredar (riwayat)',
         'period': 'Tahunan',
         'suggested_document': 'Laporan Tahunan (Catatan atas Laporan Keuangan - Ekuitas) atau Laporan Publikasi IDX',
         'where_normally_found': 'Catatan atas Laporan Keuangan, bagian Modal Saham / Ekuitas'},
        {'field': 'Public float vs kepemilikan pemegang pengendali',
         'period': 'Terakhir',
         'suggested_document': 'Laporan Tahunan (Susunan Pemegang Saham) atau data IDX/KSEI',
         'where_normally_found': 'Profil Perusahaan - Susunan Pemegang Saham (pemegang di atas/bawah 5%)'},
        {'field': 'Kepemilikan institusional rinci (per pemegang)',
         'period': 'Terakhir',
         'suggested_document': 'Laporan Tahunan (Susunan Pemegang Saham) / data KSEI',
         'where_normally_found': 'Susunan Pemegang Saham dan Daftar Pemegang Saham'},
        {'field': 'Laporan arus kas multi-tahun (Yahoo IDX hampir tidak menyediakan)',
         'period': 'Tahunan',
         'suggested_document': 'Laporan Tahunan (Laporan Arus Kas konsolidasi)',
         'where_normally_found': 'Laporan Keuangan Auditan - Laporan Arus Kas'},
        {'field': 'Struktur jatuh tempo utang & pinjaman',
         'period': 'Tahunan / triwulanan',
         'suggested_document': 'Laporan Tahunan atau Laporan Publikasi (Catatan atas Utang Bank & Efek Utang)',
         'where_normally_found': 'Catatan atas Laporan Keuangan - Utang Bank / Efek Utang, skedul jatuh tempo'},
        {'field': 'Transaksi pihak berelasi',
         'period': 'Tahunan',
         'suggested_document': 'Laporan Tahunan (Catatan tentang Pihak Berelasi)',
         'where_normally_found': 'Catatan atas Laporan Keuangan - Saldo & Transaksi dengan Pihak Berelasi'},
        {'field': 'Kinerja per segmen usaha',
         'period': 'Tahunan / triwulanan',
         'suggested_document': 'Laporan Tahunan (Pembahasan & Analisis Manajemen)',
         'where_normally_found': 'Pembahasan dan Analisis Manajemen - Segmen Usaha'},
        {'field': 'Laporan keuangan tahun kelima (Yahoo hanya menyediakan ~4 tahun)',
         'period': 'Tahunan',
         'suggested_document': 'Laporan Keuangan Auditan 5 tahun di situs IDX / Laporan Tahunan',
         'where_normally_found': 'Ikhtisar Data Keuangan Penting 5 (lima) tahun pada Laporan Tahunan'},
    ]
    if quarterly_available:
        items.pop(0)
    if sector == 'bank':
        items += [
            {'field': 'NPL (non-performing loan) & NPL coverage',
             'period': 'Triwulanan',
             'suggested_document': 'Laporan Keuangan Publikasi Triwulanan (IDX) atau Laporan Tahunan (Manajemen Risiko Kredit)',
             'where_normally_found': 'Rasio keuangan pada Laporan Publikasi / bab Manajemen Risiko Laporan Tahunan'},
            {'field': 'CAR (rasio kecukupan modal)',
             'period': 'Triwulanan',
             'suggested_document': 'Laporan Keuangan Publikasi Triwulanan (IDX) atau Laporan Tahunan (Manajemen Permodalan)',
             'where_normally_found': 'Rasio keuangan / bab Manajemen Permodalan'},
            {'field': 'NIM, CASA ratio, cost of funds, cost of credit',
             'period': 'Triwulanan',
             'suggested_document': 'Laporan Tahunan (Analisis dan Pembahasan Manajemen) atau Laporan Publikasi',
             'where_normally_found': 'Analisis kinerja manajemen - pendapatan bunga bersih & kualitas aset/liabilitas'},
            {'field': 'LDR/FDR (loan to deposit ratio)',
             'period': 'Triwulanan',
             'suggested_document': 'Laporan Keuangan Publikasi Triwulanan (IDX)',
             'where_normally_found': 'Rasio likuiditas pada Laporan Publikasi'},
        ]
    return items


def _peer_snapshot(q):
    raw = q.get('raw') or {}
    calc = q.get('calculated') or {}
    return {
        'ticker': q['ticker'],
        'company_name': raw.get('company_name'),
        'currency': raw.get('currency'),
        'price': raw.get('price'),
        'change_pct': calc.get('change_pct'),
        'trailing_pe': raw.get('trailing_pe'),
        'pbv': calc.get('pbv'),
        'roe': raw.get('return_on_equity'),
        'dividend_yield': raw.get('dividend_yield'),
        'market_cap': raw.get('market_cap'),
        'ev_to_revenue': raw.get('enterprise_to_revenue'),
        'market_timestamp': raw.get('market_timestamp'),
        'data_timing': q.get('data_timing'),
        'missing': q.get('unavailable') or [],
    }


def _pick_peers(wl, sym, sector, override, top):
    if override:
        out = [s.strip().upper() for s in override.split(',') if s.strip()]
        return out[:top], bool(out)
    if sector:
        peers = [s for s in (wl.get('sectors') or {}).get(sector, []) if s != sym]
        return peers[:top], bool(peers)
    return [], False


def cmd_deep_dive(args):
    """Assemble the full deep-dive data package (carrier) for one ticker.

    Pure data aggregation - no LLM, no narrative. Everything the chat model
    needs for an 18-section Stock Deep Dive report: market snapshot, annual +
    quarterly income trends, profitability, cash-flow/balance/valuation/
    dividend/ownership layers, industry metrics, peer comparison, existing
    documents, missing-data requests, and sources. Reported facts and
    calculated metrics (formulas attached) are kept strictly separate."""
    sym = args.symbol.strip().upper()
    if not SYM_RE.match(sym):
        return {'error': 'invalid ticker %r (1-4 letters/digits)' % sym}
    wl = load_watchlist()
    sector = _sector_of(wl, sym)
    market_cache = load_cache()
    stmt_cache = load_stmt_cache()
    client = YahooClient(delay=args.delay, force=args.refresh_market)

    target_quote, how = fetch_quote(sym, client, market_cache)
    target_quote['fetch'] = how
    if target_quote.get('unavailable') is True:
        save_stmt_cache(stmt_cache)
        return {'error': 'deep-dive failed', 'ticker': sym,
                'reason': target_quote.get('reason')}

    payload, stmts_from_cache = fetch_statements(
        sym, client, stmt_cache, refresh=args.refresh_stmts)
    raw = target_quote.get('raw') or {}
    calc = target_quote.get('calculated') or {}
    shares = raw.get('shares_outstanding')

    annual_path = args.period != 'quarterly'
    quarterly_path = args.period != 'annual'
    annual = _build_annual_statements(_income_rows(payload) if annual_path else [],
                                      shares) if annual_path else {'available': False, 'note': 'tidak diminta'}
    quarterly = _build_quarterly_statements(
        _income_rows(payload, quarterly=True) if quarterly_path else [],
        shares) if quarterly_path else {'available': False, 'note': 'tidak diminta'}

    company = _company_info(payload)
    missing = _missing_data(sector, bool(quarterly.get('available')))

    # ---- profitability / cash flow / balance / valuation / dividends / ownership
    profitability = {
        'reported': {k: raw[k] for k in ('profit_margin', 'operating_margin',
                                         'gross_margin', 'return_on_equity',
                                         'return_on_assets') if k in raw},
        'calculated': {},
        'missing': [],
    }
    if 'return_on_assets' not in raw:
        profitability['missing'].append('return_on_assets (butuh total aset dari neraca auditan)')
    profitability['missing'].append('return_on_invested_capital (ROIC) - butuh laporan resmi')

    book_equity_est = None
    debt_to_equity_est = None
    if raw.get('book_value') and shares:
        book_equity_est = round(raw['book_value'] * shares, 0)
    balance = {
        'reported': {k: raw[k] for k in ('total_debt', 'total_cash', 'book_value') if k in raw},
        'calculated': {},
        'missing': ['total_assets & total_equity auditan', 'long_term_debt & jatuh tempo',
                    'transaksi pihak berelasi', 'kualitas aset (bank: NPL, LDR)'],
    }
    if raw.get('total_debt') is not None and raw.get('total_cash') is not None:
        balance['calculated']['net_debt'] = round(raw['total_debt'] - raw['total_cash'], 0)
        balance['calculated']['net_debt_formula'] = 'total_debt - total_cash'
    if raw.get('total_debt') is not None and book_equity_est:
        debt_to_equity_est = round(raw['total_debt'] / book_equity_est, 2)
        balance['calculated']['debt_to_equity_est'] = debt_to_equity_est
        balance['calculated']['debt_to_equity_formula'] = 'total_debt / (book_value * shares_outstanding)'
    if book_equity_est:
        balance['calculated']['book_equity_est'] = book_equity_est
        balance['calculated']['book_equity_est_formula'] = 'book_value * shares_outstanding'

    ebit_latest = None
    if annual.get('available'):
        ebit_latest = (annual['periods'][-1].get('reported') or {}).get('ebit')
    valuation = {
        'reported': {k: raw[k] for k in ('trailing_pe', 'price_to_book',
                                         'enterprise_value', 'enterprise_to_revenue',
                                         'enterprise_to_ebitda', 'market_cap',
                                         'dividend_yield', 'dividend_rate') if k in raw},
        'calculated': {},
        'missing': [],
    }
    if calc.get('pbv') is not None:
        valuation['calculated']['pbv'] = calc['pbv']
        valuation['calculated']['pbv_formula'] = 'price / book_value'
    if raw.get('enterprise_value') is not None and ebit_latest:
        valuation['calculated']['ev_to_ebitda_est'] = round(raw['enterprise_value'] / ebit_latest, 2)
        valuation['calculated']['ev_to_ebitda_formula'] = 'enterprise_value / ebit_latest_fiscal_year (perkiraan tahunan, bukan TTM)'
    else:
        valuation['missing'].append('EV/EBITDA (Yahoo tidak menyediakan EBITDA/enterpriseToEbitda untuk ticker ini atu EBITDA tak tersedia)')
    if 'enterprise_to_ebitda' not in raw:
        valuation['missing'].append('EV/EBITDA TTM (perlu laporan resmi untuk EBITDA TTM)')
    if 'five_year_avg_dividend_yield' not in raw:
        valuation['missing'].append('kebijakan & riwayat dividen 5 tahun (perlu laporan resmi)')

    dividends = {
        'reported': {k: raw[k] for k in ('dividend_yield', 'dividend_rate',
                                         'last_dividend_value', 'last_dividend_date',
                                         'payout_ratio', 'five_year_avg_dividend_yield') if k in raw},
        'calculated': {},
        'missing': [],
    }
    if 'payout_ratio' not in raw:
        dividends['missing'].append('payout ratio (perlu laporan resmi)')
    if 'five_year_avg_dividend_yield' not in raw:
        dividends['missing'].append('riwayat & keberlanjutan dividen 5 tahun')

    ownership = {
        'reported': {k: raw[k] for k in ('shares_outstanding', 'float_shares',
                                         'held_percent_institutions',
                                         'held_percent_insiders') if k in raw},
        'calculated': {},
        'missing': ['kepemilikan pemegang pengendali %', 'rincian institusional per pemegang',
                    'jumlah pemegang saham, saham treasury, riwayat dilusi'],
    }
    if calc.get('float_pct') is not None:
        ownership['calculated']['float_pct'] = calc['float_pct']
        ownership['calculated']['float_pct_formula'] = 'float_shares / shares_outstanding * 100'

    industry_metrics = {'reported': {}, 'calculated': {}, 'missing': []}
    for m in missing:
        if m['field'] in ('NPL (non-performing loan) & NPL coverage', 'CAR (rasio kecukupan modal)',
                          'NIM, CASA ratio, cost of funds, cost of credit', 'LDR/FDR (loan to deposit ratio)'):
            industry_metrics['missing'].append(m['field'])

    # ---- peers
    client.force = False
    peer_syms, have_peers = _pick_peers(wl, sym, sector, args.peers, args.top)
    peer_items = []
    for p in peer_syms:
        pq, ph = fetch_quote(p, client, market_cache)
        if pq.get('unavailable') is True:
            continue
        peer_items.append(_peer_snapshot(pq))
    peer_mode = 'override' if args.peers else ('sector' if sector else 'none')
    if not have_peers:
        peer_items = []
    peers = {'available': have_peers, 'mode': peer_mode,
             'sector': sector, 'items': peer_items,
             'comparability_note': ('Peer dipilih dari sector watchlist yang sama: %s. Model bisnis tetap '
                                    'harus dibandingkan dengan hati-hati dan dijelaskan bila tidak '
                                    'langsung sebanding (geografi, franchise, struktur neraca).' % sector)
             if sector else 'Ticker tidak ada di watchlist - tambahkan dulu atau gunakan --peers.'}

    # ---- documents existing (retrieve-first before asking)
    documents_existing = _search_documents(sym, raw.get('company_name') or sym)

    # ---- periods + sources
    analysis_date = _now().strftime('%Y-%m-%d')
    financial_period = {
        'description': ('Laporan keuangan tahunan %s; harga pasar terpisah dari periode pelaporan.'
                        % (annual.get('range') or annual.get('latest_period') or 'tidak tersedia')),
        'annual_latest': annual.get('latest_period'),
        'annual_range': annual.get('range'),
        'quarterly_latest': quarterly.get('latest_period'),
        'quarterly_available': bool(quarterly.get('available')),
        'merged': 'Pasar: %s | Keuangan: %s | Kuartal: %s' % (
            raw.get('market_timestamp') or '?',
            annual.get('latest_period') or 'tidak tersedia',
            quarterly.get('latest_period') or 'tidak tersedia'),
    }
    sources = [
        {'item': 'Market snapshot (price, change, 52-week, market cap, currency)',
         'source': 'Yahoo Finance v8/finance/chart + v10/quoteSummary (price)',
         'as_of': raw.get('market_timestamp')},
        {'item': 'Fundamentals & ownership (PE, book value, float, inst/insider %)',
         'source': 'Yahoo Finance v10/quoteSummary (summaryDetail, defaultKeyStatistics, financialData)',
         'as_of': raw.get('market_timestamp')},
        {'item': 'Income statements (annual%s)' % (' + quarterly' if quarterly.get('available') else ''),
         'source': 'Yahoo Finance v10/quoteSummary incomeStatementHistory%s' % ('Quarterly' if quarterly.get('available') else ''),
         'as_of': annual.get('latest_period'),
         'cached': stmts_from_cache},
        {'item': 'Peer quotes', 'source': 'Yahoo Finance (same v8/v10 endpoints, 10-min cache)',
         'as_of': raw.get('market_timestamp')},
    ]

    save_cache(market_cache)
    save_stmt_cache(stmt_cache)

    return {'report_type': 'deep-dive',
            'ticker': sym,
            'company_name': raw.get('company_name'),
            'sector': company.get('sector'),
            'industry': company.get('industry'),
            'business_summary': company.get('business_summary'),
            'website': company.get('website'),
            'currency': raw.get('currency'),
            'analysis_date': analysis_date,
            'market_price_timestamp': raw.get('market_timestamp'),
            'data_timing': target_quote.get('data_timing'),
            'source': target_quote.get('source'),
            'financial_reporting_period': financial_period,
            'market': {'raw': {k: raw[k] for k in ('price', 'previous_close',
                                                   'market_cap', 'fifty_two_week_high',
                                                   'fifty_two_week_low') if k in raw},
                       'calculated': calc,
                       'unavailable': target_quote.get('unavailable') or []},
            'statements': {'annual': annual, 'quarterly': quarterly},
            'profitability': profitability,
            'cash_flow': {'reported': {}, 'calculated': {},
                          'missing': ['multi-year cash flow statements (Yahoo IDX hampir tidak menyediakan)',
                                      'operating cash flow, capex, free cash flow riwayat']},
            'balance': balance,
            'valuation': valuation,
            'dividends': dividends,
            'ownership': ownership,
            'industry_metrics': industry_metrics,
            'peers': peers,
            'documents_existing': documents_existing,
            'missing_data': missing,
            'sources_and_timestamps': sources,
            'statement_cache_used': stmts_from_cache}


# ─────────────────────────────── main ───────────────────────────────

def _pp_deep_dive(blob):
    """Human-readable digest of the deep-dive data package (no narrative - a
    report is generated by the chat assistant from this package)."""
    print('Stock Deep Dive - %s (%s)' % (blob.get('company_name') or '?', blob.get('ticker')))
    print('  Analisis: %s | Harga pasar per: %s' % (blob.get('analysis_date'),
                                                    blob.get('market_price_timestamp')))
    print('  Periode pelaporan keuangan: %s' % (blob.get('financial_reporting_period') or {}).get('merged'))
    print('  Sektor: %s | Industri: %s | Mata uang: %s' % (blob.get('sector'),
                                                           blob.get('industry'), blob.get('currency')))
    mkt = blob.get('market') or {}
    print('  Market: %s | cache-flag: %s' % (mkt.get('raw'), 'stmts-cache' if blob.get('statement_cache_used') else 'live'))
    ann = (blob.get('statements') or {}).get('annual') or {}
    if ann.get('available'):
        print('  Tren tahunan (%s):' % ann.get('range'))
        print('    %-12s %16s %16s %9s %9s %8s %8s' % ('Periode', 'Revenue', 'NetIncome', 'RevYoy%', 'NlYoy%', 'EPS', 'Mar%'))
        for p in ann.get('periods', []):
            rep, c = p.get('reported') or {}, p.get('calculated') or {}
            print('    %-12s %16s %16s %9s %9s %8s %8s' % (
                p.get('end_date'), rep.get('revenue') or '-', rep.get('net_income') or '-',
                c.get('revenue_growth_yoy_pct') or '-', c.get('net_income_growth_yoy_pct') or '-',
                c.get('eps_approx') or '-', c.get('net_margin_pct') or '-'))
    else:
        print('  Tren tahunan: %s' % ann.get('note', 'tidak tersedia'))
    qt = (blob.get('statements') or {}).get('quarterly') or {}
    print('  Tren kuartalan: %s' % (('tersedia s.d. %s (%d kuartal)' % (qt.get('latest_period'), len(qt.get('periods', [])))) if qt.get('available') else 'tidak tersedia dari Yahoo - lihat Missing data'))
    print('  Profitability: %s' % (blob.get('profitability') or {}).get('reported'))
    print('  Balance  : %s' % (blob.get('balance') or {}).get('calculated'))
    print('  Valuation: %s' % (blob.get('valuation') or {}).get('calculated'))
    print('  Dividends: %s' % (blob.get('dividends') or {}).get('reported'))
    own = (blob.get('ownership') or {}).get('calculated')
    print('  Ownership: reported %s | calc %s' % ((blob.get('ownership') or {}).get('reported'), own))
    peers = blob.get('peers') or {}
    print('  Peer comparison (%s, mode=%s):' % (
        len(peers.get('items', [])), peers.get('mode')))
    if peers.get('items'):
        print('    %-6s %-42s %9s %7s %7s %7s %8s' % ('Ticker', 'Company', 'Price', 'PBV', 'ROE%', 'Div%', 'PE'))
        for p in peers['items']:
            print('    %-6s %-42s %9s %7s %7s %7s %8s' % (
                p['ticker'], (p.get('company_name') or '?')[:42], p.get('price') or '-',
                p.get('pbv') or '-', p.get('roe') or '-', p.get('dividend_yield') or '-',
                p.get('trailing_pe') or '-'))
    else:
        print('    (tidak ada peer - tambahkan ticker ke watchlist atau pakai --peers)')
    docs = blob.get('documents_existing') or []
    print('  Dokumen ditemukan (retrieve-first): %d' % len(docs))
    for d in docs[:5]:
        print('    - [%s] %s (%s)' % (d.get('origin'), d.get('title'), d.get('date')))
    missing = blob.get('missing_data') or []
    print('  Missing data - %d dokumen diminta (lihat --json untuk detail):' % len(missing))
    for m in missing[:6]:
        print('    - %s -> %s' % (m['field'], m['suggested_document']))
    if len(missing) > 6:
        print('    ... dan %d lainnya' % (len(missing) - 6))


def _pp(blob, args):
    if args.json:
        print(json.dumps(blob, ensure_ascii=False, indent=2))
        return
    if isinstance(blob, dict) and blob.get('report_type') == 'deep-dive':
        _pp_deep_dive(blob)
        return
    if isinstance(blob, dict) and 'error' in blob:
        print('ERROR:', blob['error'])
        if 'sectors' in blob:
            print('Known sectors:', ', '.join(blob.get('sectors', [])))
        return
    if isinstance(blob, dict) and blob.get('status'):
        for k, v in blob.items():
            if k != 'watchlist':
                print('%s: %s' % (k, v))
        print('watchlist written to %s' % WATCHLIST_PATH)
        return
    if isinstance(blob, dict) and 'sectors' in blob:
        sectors = blob.get('sectors') or {}
        print('Investment watchlist - %s tickers across %s sectors' % (
            sum(len(v) for v in sectors.values()), len(sectors)))
        for name in sorted(sectors):
            syms = sectors[name] or []
            if syms:
                print('  %-16s %s' % (name + ':', ', '.join(sorted(syms))))
        if sectors.get('unassigned'):
            print('  %s: %s' % ('unassigned', ', '.join(sorted(sectors['unassigned']))))
        if not any(sectors.values()):
            print('(empty - add tickers with: watchlist add <TICKER>)')
        return
    for quote in blob.get('quotes', []):
        print('-' * 46)
        print('%s%s  %s' % (
            quote['ticker'],
            '  [fresh]' if quote.get('fetch') == 'fetch' else '  [cache]',
            (quote.get('raw') or {}).get('company_name') or '?',
        ))
        raw = quote.get('raw') or {}
        calc = quote.get('calculated') or {}
        print('  price %s %s (prev %s)' % (
            raw.get('price'), raw.get('currency') or '?',
            raw.get('previous_close') or '?'))
        if calc.get('change_pct') is not None:
            print('  change %s%%  pbv %s  roe %s%%' % (
                calc['change_pct'], calc.get('pbv'), raw.get('return_on_equity')))
        print('  div yield %s%%  div rate %s' % (
            raw.get('dividend_yield'), raw.get('dividend_rate')))
        print('  shares out %s  float %s  float %% %s  inst %s%%' % (
            raw.get('shares_outstanding'), raw.get('float_shares'),
            calc.get('float_pct'), raw.get('held_percent_institutions')))
        print('  mkt cap %s  PE %s  eps %s  mkt time %s' % (
            raw.get('market_cap'), raw.get('trailing_pe'), raw.get('trailing_eps'),
            raw.get('market_timestamp')))
        print('  timing: %s; source: Yahoo Finance' % quote.get('data_timing'))
        if quote.get('unavailable'):
            print('  missing: %s' % ', '.join(quote['unavailable']))
    if blob.get('unavailable'):
        print('-' * 46)
        print('UNAVAILABLE:')
        for u in blob['unavailable']:
            print('  %s: %s' % (u['ticker'], u['reason']))
    if not blob.get('quotes') and not blob.get('unavailable'):
        print('(no results)')
    if isinstance(blob, dict) and 'matched' in blob:
        print('matched: %s' % ', '.join(blob['matched']))
    if isinstance(blob, dict) and 'error' in blob:
        return


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1].strip())
    sub = p.add_subparsers(dest='cmd', required=True)

    q = sub.add_parser('quote', help='quote one or more symbols (comma-separated)')
    q.add_argument('--symbols', required=True, help='e.g. BBCA,ASII')
    q.add_argument('--delay', type=float, default=1.0)
    q.add_argument('--force', action='store_true')
    q.add_argument('--json', action='store_true')
    q.set_defaults(fn=cmd_quote)

    s = sub.add_parser('screen', help='screen the watchlist by sector or all')
    s.add_argument('--sector')
    s.add_argument('--top', type=int)
    s.add_argument('--sort', choices=['ticker', 'pbv', 'roe', 'dividend_yield', 'float_pct', 'price'])
    s.add_argument('--desc', action='store_true')
    s.add_argument('--delay', type=float, default=1.0)
    s.add_argument('--force', action='store_true')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_screen)

    w = sub.add_parser('watchlist', help='list/modify the watchlist')
    w.add_argument('action', choices=['list', 'add', 'remove'])
    w.add_argument('symbols', nargs='*')
    w.add_argument('--json', action='store_true')
    w.set_defaults(fn=cmd_watchlist)

    it = sub.add_parser('intent', help='watchlist tickers matching the given text')
    it.add_argument('--text')
    it.add_argument('--json', action='store_true')
    it.set_defaults(fn=cmd_intent)

    dd = sub.add_parser(
        'deep-dive',
        help='structured data package for a full Stock Deep Dive report')
    dd.add_argument('symbol', help='e.g. BMRI')
    dd.add_argument('--period', choices=['annual', 'quarterly', 'both'], default='both')
    dd.add_argument('--peers', help='comma-separated peer overrides, e.g. BBCA,BBRI')
    dd.add_argument('--top', type=int, default=5, help='max peer count (default 5)')
    dd.add_argument('--delay', type=float, default=1.0)
    dd.add_argument('--refresh-stmts', action='store_true',
                    help='bypass the 7-day financial-statement cache')
    dd.add_argument('--refresh-market', action='store_true',
                    help='bypass the 10-min market cache for this run')
    dd.add_argument('--json', action='store_true')
    dd.set_defaults(fn=cmd_deep_dive)

    args = p.parse_args()
    blob = args.fn(args)
    _pp(blob, args)


if __name__ == '__main__':
    main()