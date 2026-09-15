/* ═══════════════════════════════════════════════════════════════════
   tab-transactions.js — 💳 Transactions tab (personal only)

Sub-views via hash: #transactions | /overview | /all |
    /transfers | /review | /imports | /rules
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const TransactionsTab = (() => {
  /* toast lives on Comp (U has no toast) - route through it with a
     safe fallback so a failure is never a silent/uncaught error */
  const toast = (msg, ok) => {
    try { Comp.toast(String(msg || 'Unknown error'), !!ok); }
    catch (_) { alert(String(msg || 'Unknown error')); }
  };

  /* POST + parse JSON, throwing with the server's `error` field so the
     popup shows the real reason instead of a bare "HTTP 400" */
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
      if (!res.ok) {
        throw new Error((data && data.error) || `HTTP ${res.status} on ${path}`);
      }
      return data;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /* global in-tab progress bar (rides under the header) */
  function _busy(on) {
    const p = document.getElementById('tx-progress');
    if (p) p.hidden = !on;
  }

  /* ── state ──────────────────────────────────────────────────────── */
  let _activeView = 'overview';
  let _period = 'current_month';
  let _from = '';
  let _to = '';

  /* Build the period/date-range query string for overview/../list calls. */
  function _rangeQS() {
    if (_period === 'custom') {
      const p = [];
      if (_from) p.push(`from=${encodeURIComponent(_from + 'T00:00:00')}`);
      if (_to) p.push(`to=${encodeURIComponent(_to + 'T23:59:59')}`);
      return p.length ? p.join('&') : 'period=all';
    }
    return `period=${_period}`;
  }
  let _uploadBusy = false;
  let _categories = [];
  let _accounts = [];
  let _allPage = 0;
  let _allPageSize = 50;
  /* overview (finance dashboard) filter state */
  let _ovCats = [];          // selected category ids, [] = all
  let _ovProviders = '';     // '' = all wallets/banks
  let _ovFees = true;        // fees count as spend
  let _ovTransfers = false;  // person-transfers count as spend
  /* all-view drill-down state (set by chart clicks) */
  let _allCat = '';          // category id filter, '' = all
  /* transfers-view state */
  let _trNature = 'all';     // all | transfer_to_person | internal_transfer | top_up
  let _trPage = 0;
  let _trPageSize = 50;

  /* Query string for /api/transactions/analytics from overview state. */
  function _analyticsQS() {
    return `${_rangeQS()}&${_analyticsFilterQS()}`;
  }

  /* Filter-only params (no date range) - shared by the period call and the
     trailing-6-months trend call. */
  function _analyticsFilterQS() {
    const p = [];
    if (_ovCats.length) p.push('categories=' + _ovCats.join(','));
    if (_ovProviders) p.push('providers=' + encodeURIComponent(_ovProviders));
    p.push('include_fees=' + (_ovFees ? '1' : '0'));
    p.push('include_transfers=' + (_ovTransfers ? '1' : '0'));
    return p.join('&');
  }

  /* Trailing-6-months window ending at the selected period's end month. */
  function _trendRange(toISO) {
    const end = toISO ? new Date(toISO.slice(0, 10)) : new Date();
    const endM = new Date(end.getFullYear(), end.getMonth(), 1);
    const startM = new Date(endM.getFullYear(), endM.getMonth() - 5, 1);
    const last = new Date(endM.getFullYear(), endM.getMonth() + 1, 0).getDate();
    const p = n => String(n).padStart(2, '0');
    const f = d => `${d.getFullYear()}-${p(d.getMonth() + 1)}`;
    return [`${f(startM)}-01T00:00:00`, `${f(endM)}-${p(last)}T23:59:59`];
  }

  /* Jump to the All table pre-filtered (chart drill-down). */
  function _drill(from, to, categoryId) {
    _period = 'custom';
    _from = from || '';
    _to = to || '';
    _allCat = categoryId ? String(categoryId) : '';
    _allPage = 0;
    _activeView = 'all';
    location.hash = '#transactions/all';
  }

  /* Stable per-render color per category: rank by current spend, cycle the
     8 app palette vars so bars, donut and legend always agree. Trend-only
     categories (outside the selected range) are appended after. */
  function _ovColorMap(mom, buckets) {
    const map = {};
    let i = 0;
    (mom || []).forEach(m => {
      if (!(m.category_id in map)) map[m.category_id] = (i++ % 8) + 1;
    });
    const totals = {};
    (buckets || []).forEach(b => Object.entries(b.by_category || {}).forEach(([cid, v]) => {
      totals[cid] = (totals[cid] || 0) + v;
    }));
    Object.entries(totals).sort((a, b) => b[1] - a[1]).forEach(([cid]) => {
      if (!(cid in map)) map[cid] = (i++ % 8) + 1;
    });
    return cid => map[cid] || 8;
  }
  const _ovColor = k => `var(--cat-${k})`;

  /* ── nav chips ──────────────────────────────────────────────────── */
  const VIEWS = [
    { id: 'overview',  label: 'Overview',  icon: '📊' },
    { id: 'all',       label: 'All Txns',  icon: '📋' },
    { id: 'transfers', label: 'Transfers', icon: '🔄' },
    { id: 'review',    label: 'Review',    icon: '✅', badge: true },
    { id: 'imports',   label: 'Imports',   icon: '📥' },
    { id: 'rules',     label: 'Rules',     icon: '⚙️' },
  ];

  /* ── helpers ────────────────────────────────────────────────────── */
  const rp = n => n == null ? '-' : `Rp${Number(n).toLocaleString('id-ID')}`;
  /* negative values as −Rp1.434.338 (minus BEFORE Rp, proper U+2212) so the
   sign and number can never split across lines */
const rpSigned = n => {
  if (n == null) return '-';
  const v = Number(n);
  const s = Math.abs(v).toLocaleString('id-ID');
  return v < 0 ? `−Rp${s}` : `Rp${s}`;
};

  const _WALLET = {
    bca:   { label: 'BCA',   color: '#00aaef' },
    bni:   { label: 'BNI',   color: '#ed7b00' },
    gopay: { label: 'GoPay', color: '#00aa13' },
  };
  function _walletHtml(p) {
    const w = _WALLET[p] || { label: p || '-', color: 'var(--text-muted)' };
    return `<span class="tx-wallet"><span class="tx-wallet-dot" style="background:${w.color}"></span>${w.label}</span>`;
  }
  function _fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return U.esc(iso);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const txn  = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diff = Math.round((today - txn) / 86400000);
    const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    if (diff === 0) return `Today ${time}`;
    if (diff === 1) return `Yesterday ${time}`;
    if (d.getFullYear() === now.getFullYear()) {
      return d.toLocaleDateString('en-GB', { month: 'short', day: 'numeric' }) + ' ' + time;
    }
    return d.toLocaleDateString('en-GB', { month: 'short', day: 'numeric', year: 'numeric' }) + ' ' + time;
  }

  function _txTable(rows) {
    if (!rows.length) return '<div class="tx-empty">No transactions yet. Upload a GoPay PDF or sync Gmail to start.</div>';
    return `<div class="tx-table-wrap"><table class="tx-table">
      <colgroup>
        <col class="tx-col-date"><col class="tx-col-desc"><col class="tx-col-account">
        <col class="tx-col-cat"><col class="tx-col-amount"><col class="tx-col-status">
      </colgroup>
      <thead><tr>
        <th class="tx-th-date">Date</th>
        <th class="tx-th-desc">Description</th>
        <th class="tx-th-wallet">Account</th>
        <th class="tx-th-cat">Category</th>
        <th class="tx-th-amount">Amount</th>
        <th class="tx-th-status">Status</th>
      </tr></thead>
      <tbody>${rows.map(r => _txTableRow(r)).join('')}</tbody>
    </table></div>`;
  }

  function _descHtml(r) {
    const main = U.esc(r.description || r.merchant || r.notes || 'Unknown');
    const sub = r.recipient
      ? `<div class="tx-desc-sub">ke ${U.esc(r.recipient)}</div>`
      : (r.email_subject && r.email_subject !== (r.description || r.email_subject))
        ? `<div class="tx-desc-sub">${U.esc(r.email_subject)}</div>` : '';
    const srcTitle = r.raw_description ? ` title="raw: ${U.esc(r.raw_description.slice(0, 200))}"` : '';
    return `<div class="tx-desc-main"${srcTitle}>${main}${sub}</div>`;
  }

  function _confDot(r) {
    const level = r.confidence || 'none';
    const reason = U.esc((r.confidence_reason || (level === 'none' ? 'No rule matched' : '')).slice(0, 140));
    return `<span class="tx-conf tx-conf-${level}" title="${level} confidence${reason ? ' - ' + reason : ''}"></span>`;
  }

  function _catSelect(r) {
    const wantedCatId = r.category_id ? Number(r.category_id) : null;
    const groups = {};
    (_categories || []).forEach(c => {
      const g = c.group || 'Other';
      (groups[g] = groups[g] || []).push(c);
    });
    const opts = ['<option value="">Uncategorized</option>'];
    Object.keys(groups).sort().forEach(g => {
      let inner = groups[g].map(c =>
        `<option value="${c.id}" ${c.id === wantedCatId ? 'selected' : ''}>${U.esc(c.name)}</option>`
      ).join('');
      opts.push(`<optgroup label="${U.esc(g)}">${inner}</optgroup>`);
    });
    if (wantedCatId != null && !_categories.some(c => c.id === wantedCatId)) {
      opts.push(`<option value="${wantedCatId}" selected>Category ${wantedCatId}</option>`);
    }
    opts.push('<option value="__new">+ New category…</option>');
    return `<select class="tx-cat" data-id="${r.id}" aria-label="Category">${opts.join('')}</select>`;
  }

  function _txTableRow(r) {
    const amount  = r.amount || r.total_amount || 0;
    const dir     = r.direction === 'in' ? 'tx-pos' : 'tx-neg';
    const sign    = r.direction === 'in' ? '+' : '−';
    const nature  = r.nature || 'needs_review';
    const status  = r.review_status === 'review' ? '<span class="tx-badge tx-badge-warn">review</span>' :
                    r.review_status === 'uncategorized' ? '<span class="tx-badge tx-badge-muted">uncategorized</span>' :
                    nature === 'needs_review' ? '<span class="tx-badge tx-badge-muted">review</span>' : '';
    return `<tr class="tx-tr" data-id="${r.id}">
      <td class="tx-td-date" data-label="Date">${_fmtDate(r.occurred_at || r.created_at)}</td>
      <td class="tx-td-desc" data-label="Description">${_descHtml(r)}</td>
      <td class="tx-td-wallet" data-label="Account">${_walletHtml(r.provider)}</td>
      <td class="tx-td-cat" data-label="Category"><div class="tx-cat-wrap">${_confDot(r)}${_catSelect(r)}</div></td>
      <td class="tx-td-amount ${dir}" data-label="Amount">${sign}${rp(amount)}</td>
      <td class="tx-td-status" data-label="Status">${status}</td>
    </tr>`;
  }

  /* ── load ───────────────────────────────────────────────────────── */
  async function load(filter) {
    const panel = document.getElementById('tab-transactions');
    if (!panel) return;
    _activeView = (filter && VIEWS.find(v => v.id === filter)) ? filter : 'overview';
    render(panel);
    await refreshView();
  }

  /* ── render shell ───────────────────────────────────────────────── */
  function render(panel) {
    const chips = VIEWS.map(v =>
      `<button class="tx-chip ${v.id === _activeView ? 'is-active' : ''}" data-view="${v.id}">${v.icon} ${v.label}</button>`
    ).join('');

    panel.innerHTML = `
      <div class="tx-container">
        <div class="tx-header">
          <div class="tx-chips">${chips}</div>
          <div class="tx-actions">
            <select id="tx-period" class="tx-select">
              <option value="current_month" ${_period==='current_month'?'selected':''}>This Month</option>
              <option value="last_month" ${_period==='last_month'?'selected':''}>Last Month</option>
              <option value="current_year" ${_period==='current_year'?'selected':''}>This Year</option>
              <option value="all" ${_period==='all'?'selected':''}>All Time</option>
              <option value="custom" ${_period==='custom'?'selected':''}>Custom Dates…</option>
            </select>
            <span class="tx-dates" id="tx-dates" ${_period==='custom' ? '' : 'hidden'}>
              <input id="tx-from" class="tx-select tx-date" type="date" value="${_from}" title="From">
              <input id="tx-to" class="tx-select tx-date" type="date" value="${_to}" title="To">
              <button id="tx-dates-clear" class="btn tx-btn-outline" title="Reset to This Month">✕</button>
            </span>
            <button id="tx-sync-btn" class="btn tx-btn-outline">🔄 Sync Gmail</button>
            <button id="tx-reprocess-btn" class="btn tx-btn-outline" title="Re-fetch imported emails and re-run the v3 parser + deterministic categorizer in place">♻️ Reprocess</button>
            <button id="tx-add-btn" class="btn tx-btn-primary" title="Record income/expense by hand (emails can be missed)">➕ Add</button>
            <button id="tx-upload-btn" class="btn tx-btn-outline">📄 Upload</button>
          </div>
        </div>
        <div id="tx-progress" class="tx-progress" hidden><div class="tx-progress-bar"></div></div>
        <div id="tx-body" class="tx-body"></div>
      </div>
    `;

    panel.querySelectorAll('.tx-chip').forEach(b =>
      b.addEventListener('click', () => {
        _activeView = b.dataset.view;
        location.hash = `#transactions/${_activeView}`;
      }));
    panel.querySelector('#tx-period').addEventListener('change', e => {
      _period = e.target.value;
      const dates = panel.querySelector('#tx-dates');
      if (_period === 'custom') {
        dates.hidden = false;
        if (!_from && !_to) {
          const today = new Date();
          _to = today.toISOString().slice(0, 10);
          _from = today.toISOString().slice(0, 8) + '01';
          panel.querySelector('#tx-from').value = _from;
          panel.querySelector('#tx-to').value = _to;
        }
      } else {
        _from = ''; _to = '';
        dates.hidden = true;
      }
      refreshView();
    });
    panel.querySelector('#tx-from').addEventListener('change', e => { _from = e.target.value; refreshView(); });
    panel.querySelector('#tx-to').addEventListener('change', e => { _to = e.target.value; refreshView(); });
    panel.querySelector('#tx-dates-clear').addEventListener('click', () => {
      _period = 'current_month'; _from = ''; _to = '';
      panel.querySelector('#tx-period').value = 'current_month';
      panel.querySelector('#tx-dates').hidden = true;
      refreshView();
    });
    panel.querySelector('#tx-sync-btn').addEventListener('click', _syncGmail);
    panel.querySelector('#tx-reprocess-btn').addEventListener('click', _reprocess);
    panel.querySelector('#tx-add-btn').addEventListener('click', _openManual);
    panel.querySelector('#tx-upload-btn').addEventListener('click', _openUpload);
  }

  /* ── refresh ────────────────────────────────────────────────────── */
  async function refreshView() {
    const body = document.getElementById('tx-body');
    if (!body) return;
    body.innerHTML = '<div class="tx-loading">Loading...</div>';
    _busy(true);

    try {
      if (!_categories.length) {
        try {
          const c = await U.fetchJSON('/api/transactions/categories');
          _categories = (c.categories || []).sort((a, b) =>
            (a.group || a.name).localeCompare(b.group || b.name));
        } catch (_) { /* categories stay cacheable-clean on failure */ }
      }
      if (!_accounts.length) {
        try {
          const a = await U.fetchJSON('/api/transactions/accounts');
          _accounts = a.accounts || [];
        } catch (_) { /* accounts are optional for manual entry */ }
      }
      switch (_activeView) {
        case 'overview':  await _renderOverview(body); break;
        case 'all':       await _renderAll(body); break;
        case 'transfers': await _renderTransfers(body); break;
        case 'review':    await _renderReview(body); break;
        case 'imports':   await _renderImports(body); break;
        case 'rules':     await _renderRules(body); break;
        default:          await _renderOverview(body); break;
      }
    } catch (err) {
      body.innerHTML = `<div class="tx-error">Error: ${U.esc(err.message)}</div>`;
      toast(err.message, false);
    } finally {
      _busy(false);
    }
    const cats = body.querySelectorAll('.tx-cat');
    cats.forEach(sel => {
      sel.addEventListener('focus', () => { sel.dataset.was = sel.value; });
      sel.addEventListener('change', () => _changeCategory(sel));
    });
  }

  async function _changeCategory(sel) {
    const id = Number(sel.dataset.id);
    if (sel.value === '__new') {
      _inlineNewCategory(sel, id);
      return;
    }
    const category_id = sel.value ? Number(sel.value) : null;
    _busy(true);
    try {
      await _post(`/api/transactions/${id}/edit`, { category_id }, 15000);
      toast(category_id ? 'Category updated' : 'Category cleared', true);
      await refreshView();
    } catch (e) {
      toast(e.message, false);
      sel.value = sel.dataset.was || '';
    } finally {
      _busy(false);
    }
  }

  function _inlineNewCategory(sel, id) {
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'tx-cat tx-cat-new';
    input.placeholder = 'Type new category… Enter ↵ saves';
    input.maxLength = 80;

    let finished = false;
    const finish = () => { if (finished) return; finished = true; refreshView(); };

    requestAnimationFrame(() => {
      if (finished || !sel.isConnected) return;
      sel.replaceWith(input);
      input.focus();
    });

    const apply = async () => {
      if (finished) return;
      let raw = (input.value || '').trim();
      if (!raw) { finish(); return; }
      let name = raw, group = '';
      const sep = raw.indexOf('|');
      if (sep > 0) {
        group = raw.slice(0, sep).trim();
        name = raw.slice(sep + 1).trim();
      }
      if (!name) { toast('Category name is required', false); return; }
      _busy(true);
      try {
        const cat = await _post('/api/transactions/categories/create',
          { name, group: group || null }, 15000);
        if (!_categories.some(c => c.id === cat.category_id)) {
          _categories.push({ id: cat.category_id, name, group });
          _categories.sort((a, b) => (a.group || a.name).localeCompare(b.group || b.name));
        }
        await _post(`/api/transactions/${id}/edit`, { category_id: cat.category_id }, 15000);
        toast(`Category updated: ${name}`, true);
        finished = true;
        await refreshView();
      } catch (e) {
        toast(e.message, false);
        finish();
      } finally {
        _busy(false);
      }
    };

    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); apply(); }
      else if (e.key === 'Escape') { finish(); }
    });
  }

  /* ── overview: finance dashboard ──────────────────────────────── */
  async function _renderOverview(el) {
    const d = await U.fetchJSON(`/api/transactions/analytics?${_analyticsQS()}&granularity=month`);
    const [trFrom, trTo] = _trendRange(d.to_date);
    const tr = await U.fetchJSON(
      `/api/transactions/analytics?from=${encodeURIComponent(trFrom)}&to=${encodeURIComponent(trTo)}&granularity=month&${_analyticsFilterQS()}`);
    const t = d.totals || {};
    const mom = d.mom || [];
    const trend = tr.buckets || [];
    const colorOf = _ovColorMap(mom, trend);
    const spend = t.spend || 0;
    const prevTotal = mom.reduce((a, m) => a + (m.previous || 0), 0);
    const momPct = prevTotal > 0 ? Math.round((spend - prevTotal) / prevTotal * 100) : null;
    const rangeLabel = _ovRangeLabel(d);

    const catChips = (_categories || []).map(c => `
      <button class="ov-chip ${_ovCats.includes(c.id) ? 'is-active' : ''}"
        data-ovcat="${c.id}" title="${U.esc(c.group || '')}">${U.esc(c.name)}</button>`).join('');

    el.innerHTML = `
      <div class="tx-stats">
        <div class="tx-stat" title="Expense${_ovFees ? ' + bank fees' : ''}${_ovTransfers ? ' + person-transfers' : ''} in range">
          <div class="tx-stat-label">Spend${_ovFees ? ' <span class="tx-stat-sub">incl. fees</span>' : ''}</div>
          <div class="tx-stat-value tx-neg">${rp(spend)}</div></div>
        <div class="tx-stat" title="Income + refund + cashback in range">
          <div class="tx-stat-label">Income</div>
          <div class="tx-stat-value tx-pos">${rp(t.income || 0)}</div></div>
        <div class="tx-stat" title="Income minus spend in range">
          <div class="tx-stat-label">Net</div>
          <div class="tx-stat-value ${(t.income_minus_spend || 0) >= 0 ? 'tx-pos' : 'tx-neg'}">${rpSigned(t.income_minus_spend || 0)}</div></div>
        <div class="tx-stat" title="Spend vs previous comparable period (${U.esc(_ovCmpLabel(d))})">
          <div class="tx-stat-label">MoM</div>
          <div class="tx-stat-value ${momPct == null ? '' : momPct <= 0 ? 'tx-pos' : 'tx-neg'}">${momPct == null ? 'new' : (momPct > 0 ? '+' : '') + momPct + '%'}</div></div>
      </div>
      <div class="ov-move" title="Money movement excluded from Spend">
        <span>Transfer out <b>${rp(t.transfer_out || 0)}</b></span>
        <span>Internal <b>${rp(t.internal || 0)}</b></span>
        <span>Top-ups <b>${rp(t.top_up || 0)}</b></span>
        <span>Fees <b>${rp(t.fees || 0)}</b></span>
      </div>
      <div class="tx-review-bar">
        ${t.uncategorized ? `<span class="tx-badge tx-badge-muted">${t.uncategorized} uncategorized</span>` : ''}
        ${t.pending_review ? `<span class="tx-badge tx-badge-warn">${t.pending_review} need review</span>` : ''}
        ${t.suggested_transfers ? `<span class="tx-badge tx-badge-info">${t.suggested_transfers} transfer suggestions</span>` : ''}
        <span class="tx-range-hint">${U.esc(rangeLabel)}</span>
      </div>
      <div class="tx-card"><h3 class="tx-card-title">Filters</h3>
        <div class="ov-filters">
          <select id="ov-provider" class="tx-select" title="Wallet / bank">
            <option value="">All wallets</option>
            <option value="bca" ${_ovProviders === 'bca' ? 'selected' : ''}>BCA</option>
            <option value="bni" ${_ovProviders === 'bni' ? 'selected' : ''}>BNI</option>
            <option value="gopay" ${_ovProviders === 'gopay' ? 'selected' : ''}>GoPay</option>
          </select>
          <label class="ov-toggle"><input type="checkbox" id="ov-fees" ${_ovFees ? 'checked' : ''}> fees in spend</label>
          <label class="ov-toggle"><input type="checkbox" id="ov-transfers" ${_ovTransfers ? 'checked' : ''}> person-transfers in spend</label>
          <button class="btn tx-btn-sm" id="ov-reset">Reset</button>
        </div>
        <div class="ov-chips">${catChips || '<span class="tx-empty">No categories yet.</span>'}</div>
      </div>
      <div class="ov-grid">
        <div class="tx-card"><h3 class="tx-card-title">Spend trend <span class="tx-stat-sub">per month · last 6 months · click a bar to drill down</span></h3>
          ${_ovBars(trend, colorOf, 'month')}</div>
        <div class="tx-card"><h3 class="tx-card-title">By category <span class="tx-stat-sub">click to drill down</span></h3>
          ${_ovDonut(mom, colorOf, spend)}</div>
      </div>
      <div class="tx-card"><h3 class="tx-card-title">Month on month <span class="tx-stat-sub">current vs ${U.esc(_ovCmpLabel(d))} · click a row to drill down</span></h3>
        ${_ovMomTable(mom, colorOf, d)}</div>
    `;

    el.querySelectorAll('[data-ovcat]').forEach(b => b.addEventListener('click', () => {
      const id = Number(b.dataset.ovcat);
      _ovCats = _ovCats.includes(id) ? _ovCats.filter(x => x !== id) : [..._ovCats, id];
      refreshView();
    }));
    el.querySelector('#ov-provider').addEventListener('change', e => { _ovProviders = e.target.value; refreshView(); });
    el.querySelector('#ov-fees').addEventListener('change', e => { _ovFees = e.target.checked; refreshView(); });
    el.querySelector('#ov-transfers').addEventListener('change', e => { _ovTransfers = e.target.checked; refreshView(); });
    el.querySelector('#ov-reset').addEventListener('click', () => {
      _ovCats = []; _ovProviders = ''; _ovFees = true; _ovTransfers = false; refreshView();
    });
    el.querySelectorAll('[data-drill]').forEach(n => n.addEventListener('click', () => {
      _drill(n.dataset.from || '', n.dataset.to || '', n.dataset.cat || '');
    }));
  }

  function _ovRangeLabel(d) {
    const f = (d.from_date || '').slice(0, 10), t = (d.to_date || '').slice(0, 10);
    if (f && t) return `${f} – ${t}`;
    if (f) return `from ${f}`;
    if (t) return `until ${t}`;
    return 'all time';
  }
  function _ovCmpLabel(d) {
    const f = (d.cmp_from || '').slice(0, 10), t = (d.cmp_to || '').slice(0, 10);
    return (f && t) ? `${f} – ${t}` : 'previous period';
  }
  function _ovBucketRange(key, granularity) {
    if (granularity === 'month') {
      const [y, m] = key.split('-').map(Number);
      const last = new Date(y, m, 0).getDate();
      const p = n => String(n).padStart(2, '0');
      return [`${y}-${p(m)}-01`, `${y}-${p(m)}-${last}`];
    }
    return [key, key];
  }
  function _ovMonthName(key) {
    const [y, m] = key.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString('en-GB', { month: 'short' });
  }

  /* Stacked spend bars (top-5 categories + other), clickable per bucket. */
  function _ovBars(buckets, colorOf, granularity) {
    if (!buckets.length) return '<div class="tx-empty">No spend in this range.</div>';
    const W = 620, H = 190, padL = 8, padB = 22, padT = 8;
    const max = Math.max(1, ...buckets.map(b => b.spend || 0));
    const slot = (W - padL * 2) / buckets.length;
    const bw = Math.max(4, Math.min(26, slot * 0.62));
    // top-5 categories across the range define the stack order/colors
    const totals = {};
    buckets.forEach(b => Object.entries(b.by_category || {}).forEach(([cid, v]) => {
      totals[cid] = (totals[cid] || 0) + v;
    }));
    const top = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 5).map(e => e[0]);
    const bars = buckets.map((b, i) => {
      const h = Math.max(b.spend > 0 ? 2 : 0, (b.spend / max) * (H - padB - padT));
      const x = padL + slot * i + (slot - bw) / 2;
      let y = H - padB;
      const segs = top.filter(cid => (b.by_category || {})[cid] > 0).map(cid => {
        const sh = (((b.by_category || {})[cid] || 0) / b.spend) * h;
        y -= sh;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(sh - 0.6, 0.4).toFixed(1)}" style="fill:${_ovColor(colorOf(cid))}"/>`;
      }).join('');
      const other = b.spend - top.reduce((a, cid) => a + ((b.by_category || {})[cid] || 0), 0);
      let otherRect = '';
      if (other > 0) {
        const oh = (other / b.spend) * h;
        y -= oh;
        otherRect = `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(oh - 0.6, 0.4).toFixed(1)}" style="fill:var(--text-muted)" opacity="0.45"/>`;
      }
      const [f, t] = _ovBucketRange(b.key, granularity);
      const tip = granularity === 'month'
        ? `${_ovMonthName(b.key)} ${b.key.slice(0, 4)}: ${rp(b.spend)}`
        : `${b.key}: ${rp(b.spend)}`;
      const lbl = granularity === 'month' ? _ovMonthName(b.key)
        : (buckets.length > 16 ? (i % 5 === 0 ? b.key.slice(8) : '') : b.key.slice(8));
      return `<g data-drill data-from="${f}" data-to="${t}" style="cursor:pointer"><title>${U.esc(tip)}</title>` +
        `${otherRect}${segs}` +
        `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 8}" font-size="9" text-anchor="middle" fill="var(--text-muted)">${lbl}</text></g>`;
    }).join('');
    return `<svg class="ov-bars" viewBox="0 0 ${W} ${H}" role="img" aria-label="spend trend">${bars}</svg>`;
  }

  /* Donut of current-period spend by category, with clickable legend. */
  function _ovDonut(mom, colorOf, total) {
    const rows = (mom || []).filter(m => (m.current || 0) > 0);
    if (!rows.length) return '<div class="tx-empty">No spend in this range.</div>';
    const sz = 170, sw = 26, r = (sz - sw) / 2, c = sz / 2, circ = 2 * Math.PI * r;
    let off = 0;
    const rings = rows.map(m => {
      const len = (m.current / total) * circ;
      const el = `<circle cx="${c}" cy="${c}" r="${r}" fill="none" data-drill data-cat="${m.category_id}" ` +
        `style="fill:none;stroke:${_ovColor(colorOf(m.category_id))};cursor:pointer" stroke-width="${sw}" ` +
        `stroke-dasharray="${Math.max(len - 2, 0.5).toFixed(2)} ${(circ - Math.max(len - 2, 0)).toFixed(2)}" ` +
        `stroke-dashoffset="${(-off).toFixed(2)}"><title>${U.esc(`${m.name} — ${rp(m.current)}`)}</title></circle>`;
      off += len;
      return el;
    }).join('');
    const legend = rows.map(m => {
      const pct = total > 0 ? Math.round(m.current / total * 100) : 0;
      return `<div class="ov-legend-row" data-drill data-cat="${m.category_id}" title="Drill to ${U.esc(m.name)}">` +
        `<span class="ov-dot" style="background:${_ovColor(colorOf(m.category_id))}"></span>` +
        `<span class="ov-legend-name">${U.esc(m.name)}</span>` +
        `<span class="ov-legend-val">${rp(m.current)} <span class="tx-count">${pct}%</span></span></div>`;
    }).join('');
    return `<div class="ov-donut-wrap"><svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}" role="img" aria-label="spend by category">` +
      `<g transform="rotate(-90 ${c} ${c})">${rings}</g>` +
      `<text x="${c}" y="${c}" class="donut-total" text-anchor="middle" dominant-baseline="central" font-size="17">${rp(total)}</text></svg>` +
      `<div class="ov-legend">${legend}</div></div>`;
  }

  /* MoM table: current vs previous per category. Click a row to drill down. */
  function _ovMomTable(mom, colorOf, d) {
    if (!mom.length) return '<div class="tx-empty">No spend in this range.</div>';
    const [f, t] = [((d.from_date || '').slice(0, 10)), ((d.to_date || '').slice(0, 10))];
    const rows = mom.map(m => {
      const pct = m.pct == null ? 'new' : `${m.pct > 0 ? '+' : ''}${m.pct}%`;
      const cls = m.delta > 0 ? 'tx-neg' : m.delta < 0 ? 'tx-pos' : '';
      return `<tr class="ov-mom-row" data-drill data-from="${f}" data-to="${t}" data-cat="${m.category_id}" title="Drill to ${U.esc(m.name)}">` +
        `<td><span class="ov-dot" style="background:${_ovColor(colorOf(m.category_id))}"></span> ${U.esc(m.name)}</td>` +
        `<td class="tx-num">${rp(m.current)}</td>` +
        `<td class="tx-num tx-muted">${rp(m.previous)}</td>` +
        `<td class="tx-num ${cls}">${rpSigned(m.delta)}</td>` +
        `<td class="tx-num ${cls}">${pct}</td></tr>`;
    }).join('');
    return `<div class="tx-table-wrap"><table class="tx-table ov-mom"><thead><tr>` +
      `<th>Category</th><th class="tx-num">This period</th><th class="tx-num">Previous</th>` +
      `<th class="tx-num">Δ Rp</th><th class="tx-num">Δ %</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  /* ── all transactions ───────────────────────────────────────────── */
  async function _renderAll(el) {
    const size = _allPageSize;
    const catQ = _allCat ? `&category=${encodeURIComponent(_allCat)}` : '';
    let d = await U.fetchJSON(`/api/transactions/list?limit=${size}&offset=${_allPage * size}&${_rangeQS()}${catQ}`);
    const total = d.total || 0;
    const pages = Math.max(1, Math.ceil(total / size));
    if (_allPage >= pages) {
      _allPage = Math.max(0, pages - 1);
      return _renderAll(el);
    }
    const pager = (d.rows || []).length || total > size ? `
      <div class="tx-pager">
        <button class="btn tx-btn-sm" data-pg="prev" ${_allPage <= 0 ? 'disabled' : ''}>← Prev</button>
        <span class="tx-pager-info">Page ${_allPage + 1} / ${pages} · ${total} txns</span>
        <select class="tx-pager-size" title="Rows per page">
          ${[25, 50, 100, 200].map(n =>
            `<option value="${n}" ${n === size ? 'selected' : ''}>${n}/page</option>`).join('')}
        </select>
        <button class="btn tx-btn-sm" data-pg="next" ${_allPage >= pages - 1 ? 'disabled' : ''}>Next →</button>
      </div>` : '';
    el.innerHTML = `
      <div class="tx-card">
        <div class="tx-card-header">
          <h3 class="tx-card-title">All Transactions (${total})</h3>
          <span class="ov-allfilters">
            <select id="tx-all-cat" class="tx-select" title="Filter by category">
              <option value="">All categories</option>
              ${(_categories || []).map(c =>
                `<option value="${c.id}" ${String(c.id) === String(_allCat) ? 'selected' : ''}>${U.esc(c.name)}</option>`).join('')}
            </select>
            ${_allCat || (_from && _to && _period === 'custom') ? '<button class="btn tx-btn-sm" id="tx-all-clear">Clear</button>' : ''}
          </span>
        </div>
        ${pages > 1 ? `<span class="tx-pager-info">showing ${_allPage * size + 1}–${Math.min((_allPage + 1) * size, total)}</span>` : ''}
        ${_txTable(d.rows || [])}
        ${pager}
      </div>
    `;
    el.querySelector('#tx-all-cat').addEventListener('change', e => {
      _allCat = e.target.value; _allPage = 0; refreshView();
    });
    const clr = el.querySelector('#tx-all-clear');
    if (clr) clr.addEventListener('click', () => {
      _allCat = ''; _allPage = 0; refreshView();
    });
    if (pages > 1) {
      el.querySelectorAll('[data-pg]').forEach(b => b.addEventListener('click', () => {
        if (b.dataset.pg === 'prev' && _allPage > 0) { _allPage--; refreshView(); }
        if (b.dataset.pg === 'next' && _allPage < pages - 1) { _allPage++; refreshView(); }
      }));
      el.querySelector('.tx-pager-size').addEventListener('change', e => {
        _allPageSize = Number(e.target.value);
        _allPage = 0;
        refreshView();
      });
    }
  }

  /* ── transfers: every transfer-nature row, editable in place ──── */
  async function _renderTransfers(el) {
    const size = _trPageSize;
    const natQ = _trNature !== 'all' ? `&nature=${_trNature}` : '';
    let d = await U.fetchJSON(`/api/transactions/transfers?limit=${size}&offset=${_trPage * size}&${_rangeQS()}${natQ}`);
    const total = d.total || 0;
    const pages = Math.max(1, Math.ceil(total / size));
    if (_trPage >= pages) {
      _trPage = Math.max(0, pages - 1);
      return _renderTransfers(el);
    }
    const pairs = (d.pairs || []).filter(p => p.status !== 'rejected');
    const natLabel = { all: 'All transfers', transfer_to_person: 'To person', internal_transfer: 'Internal', top_up: 'Top-ups' };
    el.innerHTML = `
      ${pairs.length ? `<div class="tx-card"><h3 class="tx-card-title">Suggested pairs (${pairs.length})</h3>
        ${pairs.map(p => `
          <div class="tx-row tx-transfer-row">
            <span>${U.esc(p.from_desc || p.from_nature || '?')}</span>
            <span class="tx-arrow">→</span>
            <span>${U.esc(p.to_desc || p.to_nature || '?')}</span>
            <span class="tx-amount">${rp(p.principal_amount)}</span>
            <span class="tx-badge tx-badge-${p.status === 'confirmed' ? 'good' : 'warn'}">${p.status}</span>
          </div>`).join('')}</div>` : ''}
      <div class="tx-card">
        <div class="tx-card-header">
          <h3 class="tx-card-title">Transfers (${total})</h3>
          <span class="ov-allfilters">
            <select id="tx-tr-nature" class="tx-select" title="Transfer type">
              ${Object.entries(natLabel).map(([v, l]) =>
                `<option value="${v}" ${v === _trNature ? 'selected' : ''}>${l}</option>`).join('')}
            </select>
          </span>
        </div>
        <div class="tx-range-hint">To person = money out to someone else (categorize it into spend here).
          Internal = your own accounts/wallets (excluded from spend).</div>
        ${_txTable(d.rows || [])}
        ${pages > 1 ? `<div class="tx-pager">
          <button class="btn tx-btn-sm" data-pg="prev" ${_trPage <= 0 ? 'disabled' : ''}>← Prev</button>
          <span class="tx-pager-info">Page ${_trPage + 1} / ${pages} · ${total} txns</span>
          <button class="btn tx-btn-sm" data-pg="next" ${_trPage >= pages - 1 ? 'disabled' : ''}>Next →</button>
        </div>` : ''}
      </div>
    `;
    el.querySelector('#tx-tr-nature').addEventListener('change', e => {
      _trNature = e.target.value; _trPage = 0; refreshView();
    });
    el.querySelectorAll('[data-pg]').forEach(b => b.addEventListener('click', () => {
      if (b.dataset.pg === 'prev' && _trPage > 0) { _trPage--; refreshView(); }
      if (b.dataset.pg === 'next' && _trPage < pages - 1) { _trPage++; refreshView(); }
    }));
  }

  /* ── review queue ───────────────────────────────────────────────── */
  async function _renderReview(el) {
    const d = await U.fetchJSON('/api/transactions/review');
    const rows = (d.rows || []).map(r => `
      <tr class="tx-tr" data-id="${r.id}">
        <td class="tx-td-date" data-label="Date">${_fmtDate(r.occurred_at || r.created_at)}</td>
        <td class="tx-td-desc" data-label="Description">${_descHtml(r)}</td>
        <td class="tx-td-wallet" data-label="Account">${_walletHtml(r.provider)}</td>
        <td class="tx-td-cat" data-label="Category"><div class="tx-cat-wrap">${_confDot(r)}${_catSelect(r)}</div></td>
        <td class="tx-td-amount ${r.direction === 'in' ? 'tx-pos' : 'tx-neg'}" data-label="Amount">${r.direction === 'in' ? '+' : '−'}${rp(r.amount)}</td>
        <td class="tx-td-status tx-review-actions" data-label="Status"><span class="tx-status-inner">
          <button class="btn tx-btn-sm tx-btn-skip" data-id="${r.id}">Skip</button>
        </span></td>
      </tr>`).join('');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Review Queue (${d.count || 0})</h3>
        ${d.rows && d.rows.length
          ? `<div class="tx-table-wrap"><table class="tx-table"><colgroup>
               <col class="tx-col-date"><col class="tx-col-desc"><col class="tx-col-account">
               <col class="tx-col-cat"><col class="tx-col-amount"><col class="tx-col-status">
             </colgroup><thead><tr>
               <th class="tx-th-date">Date</th>
               <th class="tx-th-desc">Description</th>
               <th class="tx-th-wallet">Account</th>
               <th class="tx-th-cat">Category</th>
               <th class="tx-th-amount">Amount</th>
               <th class="tx-th-status">Status</th>
             </tr></thead><tbody>${rows}</tbody></table></div>`
          : '<div class="tx-empty">All clear! No transactions need review.</div>'}
      </div>
    `;
    el.querySelectorAll('.tx-btn-skip').forEach(b =>
      b.addEventListener('click', () => _reviewSkip(b.dataset.id)));
  }

  async function _reviewSkip(id) {
    _busy(true);
    try {
      await _post('/api/transactions/review/respond', { id: Number(id), action: 'skip' }, 15000);
      toast('Skipped');
      await refreshView();
    } catch (e) {
      toast(e.message, false);
    } finally {
      _busy(false);
    }
  }

  /* ── imports ────────────────────────────────────────────────────── */
  const _IMPORT_ICON = k =>
    (k === 'gmail') ? '📧' : '📄';

  async function _renderImports(el) {
    const d = await U.fetchJSON('/api/transactions/imports');
    const rows = (d.batches || []).map(b => {
      const canConfirm = b.state === 'preview';
      return `
        <div class="tx-row" data-batch="${b.id}">
          <span class="tx-import-kind">${_IMPORT_ICON(b.kind)} ${U.esc(b.provider || '')}</span>
          <span class="tx-import-state tx-badge tx-badge-${b.state === 'committed' ? 'good' : 'warn'}">${b.state}</span>
          <span class="tx-import-date">${U.esc(b.created_at || '')}</span>
          ${canConfirm ? '<button class="tx-btn tx-btn-sm" onclick="Tabs.transactions._confirmImport(event)">Confirm</button>' : ''}
        </div>`;
    }).join('');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Import History</h3>
        ${rows || '<div class="tx-empty">No imports yet. Upload a statement PDF (GoPay / BCA / BNI) to start.</div>'}
      </div>
    `;
  }

  async function _confirmImport(ev) {
    const btn = ev.currentTarget;
    const batchId = btn.closest('.tx-row').dataset.batch;
    btn.disabled = true; btn.textContent = 'Confirming...';
    _busy(true);
    try {
      const accts = await U.fetchJSON('/api/transactions/accounts');
      const acctId = (accts.accounts && accts.accounts[0] && accts.accounts[0].id) || null;
      const res = await _post('/api/transactions/import/confirm',
        { batch_id: Number(batchId), account_id: acctId }, 20000);
      toast(`Batch confirmed: ${res.new_rows || 0} ledger rows`);
      await refreshView();
    } catch (e) {
      toast(e.message, false);
      btn.disabled = false; btn.textContent = 'Confirm';
    } finally {
      _busy(false);
    }
  }

  /* ── rules ──────────────────────────────────────────────────────── */
  async function _renderRules(el) {
    const d = await U.fetchJSON('/api/transactions/rules');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Categorization Rules</h3>
        ${d.rules && d.rules.length ? d.rules.map(r => `
          <div class="tx-row">
            <span class="tx-rule-pattern">"${U.esc(r.merchant_or_recipient)}"</span>
            <span class="tx-arrow">→</span>
            <span>${U.esc(r.category_name || '?')} / ${r.nature}</span>
            <span class="tx-count">(${r.times_applied}x)</span>
          </div>
        `).join('') : '<div class="tx-empty">No rules yet. Use "Remember" when correcting a category.</div>'}
      </div>
    `;
  }

  /* ── manual entry (email notifications can be missed) ───────────── */
  async function _openManual() {
    try {
      const c = await U.fetchJSON('/api/transactions/categories');
      _categories = (c.categories || []).sort((a, b) =>
        (a.group || a.name).localeCompare(b.group || b.name));
    } catch (_) { /* keep cached categories on failure */ }
    const accounts = _accountOptions();
    const catOptions = _categoryOptions();
    const card = document.createElement('div');
    card.className = 'tx-modal-backdrop';
    card.innerHTML = `
      <div class="tx-modal-card">
        <h3>Add transaction</h3>
        <div class="tx-form">
          <label class="tx-form-row tx-form-type">
            <span>Type</span>
            <select id="tx-m-type">
              <option value="out">Expense (out)</option>
              <option value="in">Income (in)</option>
            </select>
          </label>
          <label class="tx-form-row">
            <span>Date</span>
            <input id="tx-m-date" type="date" value="${new Date().toISOString().slice(0, 10)}">
          </label>
          <label class="tx-form-row">
            <span>Amount (Rp)</span>
            <input id="tx-m-amount" type="number" min="1" step="1000" placeholder="1.000.000">
          </label>
          <label class="tx-form-row">
            <span>Description</span>
            <input id="tx-m-desc" type="text" maxlength="200"
                   placeholder="e.g. Salary, Gojek, Tokopedia, Transfer from X">
          </label>
          <label class="tx-form-row">
            <span>Account</span>
            <select id="tx-m-account">${accounts}</select>
          </label>
          <label class="tx-form-row">
            <span>Category</span>
            <select id="tx-m-cat">${catOptions}</select>
          </label>
          <div id="tx-m-newcat" class="tx-newcat" hidden>
            <input id="tx-m-newcat-name" type="text" maxlength="60"
                   placeholder="New category name, e.g. Rent">
            <input id="tx-m-newcat-group" type="text" maxlength="60"
                   placeholder="Group (optional), e.g. Housing">
          </div>
          <label class="tx-form-row">
            <span>Notes</span>
            <input id="tx-m-notes" type="text" maxlength="200">
          </label>
          <div class="tx-form-actions">
            <button id="tx-m-cancel" class="btn tx-btn-outline">Cancel</button>
            <button id="tx-m-save" class="btn tx-btn-primary">Save</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(card);
    card.querySelector('#tx-m-cancel').addEventListener('click', () => card.remove());
    card.addEventListener('click', e => { if (e.target === card) card.remove(); });
    const catSel = card.querySelector('#tx-m-cat');
    const newCat = card.querySelector('#tx-m-newcat');
    const toggleNewCat = () => {
      const show = catSel.value === '__new';
      newCat.hidden = !show;
      if (show) card.querySelector('#tx-m-newcat-name').focus();
    };
    catSel.addEventListener('change', toggleNewCat);
    toggleNewCat();
    card.querySelector('#tx-m-save').addEventListener('click', async () => {
      const amount = (card.querySelector('#tx-m-amount').value || '').trim();
      const desc = (card.querySelector('#tx-m-desc').value || '').trim();
      const date = (card.querySelector('#tx-m-date').value || '').trim();
      if (!amount || Number(amount) <= 0) { toast('Amount must be > 0', false); return; }
      if (!desc) { toast('Description is required', false); return; }
      if (!date) { toast('Date is required', false); return; }
      let categoryId = catSel && catSel.value !== '__new' && catSel.value
        ? Number(catSel.value) : null;
      _busy(true);
      try {
        if (catSel && catSel.value === '__new') {
          const name = (card.querySelector('#tx-m-newcat-name').value || '').trim();
          const group = (card.querySelector('#tx-m-newcat-group').value || '').trim();
          if (!name) { toast('New category name is required', false); return; }
          const cat = await _post('/api/transactions/categories/create',
            { name, group: group || null }, 15000);
          categoryId = cat.category_id;
        }
        const res = await _post('/api/transactions/manual', {
          direction: card.querySelector('#tx-m-type').value,
          amount: Number(amount),
          date: `${date}T12:00`,
          description: desc,
          account_id: card.querySelector('#tx-m-account').value || null,
          category_id: categoryId,
          notes: (card.querySelector('#tx-m-notes').value || '').trim(),
        }, 15000);
        toast(`Added: ${desc}`, true);
        card.remove();
        await refreshView();
      } catch (e) {
        toast(e.message, false);
      } finally {
        _busy(false);
      }
    });
  }

  function _accountOptions() {
    let html = '<option value="">(no account)</option>';
    _accounts.forEach(a =>
      html += `<option value="${a.id}">${U.esc(a.alias || a.provider || a.id)}</option>`);
    return html;
  }

  function _categoryOptions() {
    let html = '<option value="">Auto</option>';
    const groups = {};
    (_categories || []).forEach(c => {
      const g = c.group || 'Other';
      (groups[g] = groups[g] || []).push(c);
    });
    Object.keys(groups).sort().forEach(g => {
      html += `<optgroup label="${U.esc(g)}">`;
      html += groups[g].map(c =>
        `<option value="${c.id}">${U.esc(c.name)}</option>`).join('');
      html += '</optgroup>';
    });
    html += '<option value="__new">+ New category…</option>';
    return html;
  }

  /* ── upload PDF (GoPay / BCA / BNI) ─────────────────────────────── */
  const _WALLETS = [
    { id: 'gopay', label: 'GoPay', hint: 'Saldo rows only (Coins are skipped automatically)' },
    { id: 'bca', label: 'BCA', hint: 'Rekening Tahapan e-statement' },
    { id: 'bni', label: 'BNI', hint: 'e-statement needs the PDF password (pre-filled)' },
  ];

  async function _openUpload() {
    const today = new Date();
    const defMonth = today.toISOString().slice(0, 7);
    const card = document.createElement('div');
    card.className = 'tx-modal-backdrop';
    card.innerHTML = `
      <div class="tx-modal-card">
        <h3>Upload statement PDF</h3>
        <div class="tx-form">
          <label class="tx-form-row">
            <span>Wallet</span>
            <select id="tx-up-provider">
              ${_WALLETS.map(w =>
                `<option value="${w.id}" ${w.id === 'bni' ? 'selected' : ''}>${w.label}</option>`).join('')}
            </select>
          </label>
          <label class="tx-form-row">
            <span>Month</span>
            <input id="tx-up-month" type="month" value="${defMonth}">
          </label>
          <label class="tx-form-row">
            <span>PDF password</span>
            <input id="tx-up-password" type="text" value="01041988"
                   title="Only encrypted e-statements (BNI) use this; it is ignored otherwise"
                   placeholder="(leave blank if none)">
          </label>
          <div id="tx-up-hint" class="tx-up-hint"></div>
          <div id="tx-up-file" class="tx-up-drop">
            <div class="tx-up-drop-label">Choose PDF…</div>
          </div>
          <div class="tx-form-actions">
            <button id="tx-up-cancel" class="btn tx-btn-outline">Cancel</button>
            <button id="tx-up-submit" class="btn tx-btn-primary">Upload</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(card);
    const fileLabel = card.querySelector('.tx-up-drop-label');
    const hintEl = card.querySelector('#tx-up-hint');
    let file = null;

    const updateHint = () => {
      const w = _WALLETS.find(x => x.id === card.querySelector('#tx-up-provider').value);
      hintEl.textContent = w ? w.hint : '';
    };
    card.querySelector('#tx-up-provider').addEventListener('change', updateHint);
    updateHint();

    card.querySelector('.tx-up-drop').addEventListener('click', () => input.click());
    card.querySelector('#tx-up-cancel').addEventListener('click', () => card.remove());
    card.addEventListener('click', e => { if (e.target === card) card.remove(); });

    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf';
    input.onchange = () => {
      file = input.files[0];
      fileLabel.textContent = file ? `📄 ${file.name}` : 'Choose PDF…';
    };

    card.querySelector('#tx-up-submit').addEventListener('click', async () => {
      if (!file) { toast('Choose a PDF file first', false); return; }
      if (_uploadBusy) return toast('Upload already in progress', false);
      const provider = card.querySelector('#tx-up-provider').value;
      const month = card.querySelector('#tx-up-month').value;
      const password = (card.querySelector('#tx-up-password').value || '').trim();
      card.remove();
      _uploadBusy = true;
      _busy(true);
      const lock = _lockLayer();
      const start = Date.now();
      const fake = setInterval(() => {
        const el = document.getElementById('tx-upload-bar');
        if (el) el.style.width = `${Math.min(92, (Date.now() - start) / 2000)}%`;
      }, 200);
      try {
        const b64 = await _fileToBase64(file);
        const res = await _post('/api/transactions/upload',
          { filename: file.name, data: b64, provider, password, month }, 120000);
        if (res.ok) {
          toast(`Imported ${res.new_rows || 0} rows (${res.duplicate_rows || 0} duplicates skipped)` +
                (res.skipped_rows ? `, ${res.skipped_rows} out-of-month skipped` : ''), true);
          await refreshView();
        } else {
          toast(res.error || 'Upload failed', false);
        }
      } catch (e) {
        toast(e.message, false);
      } finally {
        clearInterval(fake);
        lock.remove();
        _busy(false);
        _uploadBusy = false;
      }
    });
  }

  /* Full-screen overlay that blocks any click behind the upload and shows
     the (indeterminate-till-done) progress bar while the browser sends the
     PDF and the server parses + auto-confirms it. */
  function _lockLayer() {
    const lock = document.createElement('div');
    lock.className = 'tx-upload-lock';
    lock.innerHTML = `
      <div class="tx-upload-card">
        <div class="tx-upload-title">Importing statement…</div>
        <div class="tx-upload-track"><div id="tx-upload-bar" class="tx-upload-bar" style="width:6%"></div></div>
        <div class="tx-upload-sub">Parsing PDF, checking duplicates, categorizing — the page stays locked until done.</div>
      </div>`;
    document.body.appendChild(lock);
    return lock;
  }

  function _fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const b64 = reader.result.split(',')[1];
        resolve(b64);
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  /* ── reprocess (in-place v3 re-parse + re-categorize) ────────────── */
  async function _reprocess() {
    const okBtn = window.confirm(
      'Re-fetch all imported emails and re-run the v3 parser + deterministic ' +
      'categorizer in place?\n\nExisting manual corrections are preserved. ' +
      'This can take a minute.');
    if (!okBtn) return;
    const btn = document.getElementById('tx-reprocess-btn');
    if (btn) { btn.disabled = true; btn.textContent = '♻️ Reprocessing...'; }
    _busy(true);
    try {
      const res = await _post('/api/transactions/reprocess', { provider: null }, 120000);
      if (res.ok) {
        toast(`Reprocessed ${res.processed} docs (${res.updated_ext} updated, ` +
              `${res.added_ledger} fee rows, ${res.uncategorized} still need review)`);
        await refreshView();
      } else {
        toast(res.error || 'Reprocess failed', false);
      }
    } catch (e) {
      toast(e.message, false);
    } finally {
      _busy(false);
      if (btn) { btn.disabled = false; btn.textContent = '♻️ Reprocess'; }
    }
  }

  /* ── sync Gmail ─────────────────────────────────────────────────── */
  async function _syncGmail() {
    const btn = document.getElementById('tx-sync-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Syncing...'; }
    _busy(true);
    try {
      const res = await _post('/api/transactions/sync/gmail', undefined, 60000);
      if (res.ok) {
        toast(`Synced: ${res.synced} new, ${res.skipped} skipped`);
        await refreshView();
      } else {
        toast(res.error || 'Sync failed', false);
      }
    } catch (e) {
      toast(e.message, false);
    } finally {
      _busy(false);
      if (btn) { btn.disabled = false; btn.textContent = '🔄 Sync Gmail'; }
    }
  }

  return { load, _confirmImport };
})();

window.Tabs.transactions = TransactionsTab;
