/* ═══════════════════════════════════════════════════════════════════
   tab-transactions.js — 💳 Transactions tab (personal only)

   Sub-views via hash: #transactions | /overview | /all | /spending |
   /transfers | /review | /accounts | /imports | /rules
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
  let _uploadBusy = false;
  let _categories = [];

  /* ── nav chips ──────────────────────────────────────────────────── */
  const VIEWS = [
    { id: 'overview',  label: 'Overview',  icon: '📊' },
    { id: 'all',       label: 'All Txns',  icon: '📋' },
    { id: 'spending',  label: 'Spending',  icon: '💸' },
    { id: 'transfers', label: 'Transfers', icon: '🔄' },
    { id: 'review',    label: 'Review',    icon: '✅', badge: true },
    { id: 'accounts',  label: 'Accounts',  icon: '🏦' },
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
            </select>
            <button id="tx-sync-btn" class="btn tx-btn-outline">🔄 Sync Gmail</button>
            <button id="tx-reprocess-btn" class="btn tx-btn-outline" title="Re-fetch imported emails and re-run the v3 parser + deterministic categorizer in place">♻️ Reprocess</button>
            <button id="tx-upload-btn" class="btn tx-btn-primary">📄 Upload GoPay PDF</button>
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
      refreshView();
    });
    panel.querySelector('#tx-sync-btn').addEventListener('click', _syncGmail);
    panel.querySelector('#tx-reprocess-btn').addEventListener('click', _reprocess);
    panel.querySelector('#tx-upload-btn').addEventListener('click', _uploadPDF);
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
      switch (_activeView) {
        case 'overview':  return await _renderOverview(body);
        case 'all':       return await _renderAll(body);
        case 'spending':  return await _renderSpending(body);
        case 'transfers': return await _renderTransfers(body);
        case 'review':    return await _renderReview(body);
        case 'accounts':  return await _renderAccounts(body);
        case 'imports':   return await _renderImports(body);
        case 'rules':     return await _renderRules(body);
        default:          return await _renderOverview(body);
      }
    } catch (err) {
      body.innerHTML = `<div class="tx-error">Error: ${U.esc(err.message)}</div>`;
      toast(err.message, false);
    } finally {
      _busy(false);
    }
    const cats = body.querySelectorAll('.tx-cat');
    cats.forEach(sel => sel.addEventListener('change', () => _changeCategory(sel)));
  }

  async function _changeCategory(sel) {
    const id = Number(sel.dataset.id);
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

  /* ── overview ───────────────────────────────────────────────────── */
  async function _renderOverview(el) {
    const d = await U.fetchJSON(`/api/transactions/overview?period=${_period}`);
    el.innerHTML = `
      <div class="tx-stats">
        <div class="tx-stat"><div class="tx-stat-label">Expense</div><div class="tx-stat-value tx-neg">${rp(d.expense)}</div></div>
        <div class="tx-stat"><div class="tx-stat-label">Fee</div><div class="tx-stat-value tx-neg">${rp(d.fee)}</div></div>
        <div class="tx-stat"><div class="tx-stat-label">Income</div><div class="tx-stat-value tx-pos">${rp(d.income)}</div></div>
        <div class="tx-stat"><div class="tx-stat-label">Refund</div><div class="tx-stat-value tx-pos">${rp(d.refund)}</div></div>
        <div class="tx-stat"><div class="tx-stat-label">Cashback</div><div class="tx-stat-value tx-pos">${rp(d.cashback)}</div></div>
        <div class="tx-stat"><div class="tx-stat-label">Net</div><div class="tx-stat-value ${d.net >= 0 ? 'tx-pos' : 'tx-neg'}">${rpSigned(d.net)}</div></div>
      </div>
      <div class="tx-review-bar">
        ${d.pending_review ? `<span class="tx-badge tx-badge-warn">${d.pending_review} need review</span>` : ''}
        ${d.suggested_transfers ? `<span class="tx-badge tx-badge-info">${d.suggested_transfers} transfer suggestions</span>` : ''}
        ${d.uncategorized ? `<span class="tx-badge tx-badge-muted">${d.uncategorized} uncategorized</span>` : ''}
      </div>
      ${_renderRecent(d.recent || [])}
    `;
  }

  function _renderRecent(rows) {
    if (!rows.length) return '<div class="tx-card"><div class="tx-empty">No transactions yet. Upload a GoPay PDF or sync Gmail to start.</div></div>';
    return `<div class="tx-card"><h3 class="tx-card-title">Recent</h3>${_txTable(rows)}</div>`;
  }

  /* ── all transactions ───────────────────────────────────────────── */
  async function _renderAll(el) {
    const d = await U.fetchJSON(`/api/transactions/list?limit=100`);
    el.innerHTML = `
      <div class="tx-card">
        <div class="tx-card-header"><h3 class="tx-card-title">All Transactions (${d.total || 0})</h3></div>
        ${_txTable(d.rows || [])}
      </div>
    `;
  }

  /* ── spending ───────────────────────────────────────────────────── */
  async function _renderSpending(el) {
    const d = await U.fetchJSON(`/api/transactions/spending?period=${_period}`);
    const cats = d.by_category || [];
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Spending by Category</h3>
        ${cats.length ? cats.map(c => `
          <div class="tx-row">
            <span>${U.esc(c.category_name || 'Uncategorized')}</span>
            <span class="tx-amount">${rp(c.total)} <span class="tx-count">(${c.count})</span></span>
          </div>
        `).join('') : '<div class="tx-empty">No spending data.</div>'}
      </div>
    `;
  }

  /* ── transfers ──────────────────────────────────────────────────── */
  async function _renderTransfers(el) {
    const d = await U.fetchJSON('/api/transactions/transfers');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Transfers</h3>
        ${d.rows && d.rows.length ? d.rows.map(r => `
          <div class="tx-row tx-transfer-row">
            <span>${U.esc(r.from_desc || r.from_nature || '?')}</span>
            <span class="tx-arrow">→</span>
            <span>${U.esc(r.to_desc || r.to_nature || '?')}</span>
            <span class="tx-amount">${rp(r.principal_amount)}</span>
            <span class="tx-badge tx-badge-${r.status === 'confirmed' ? 'good' : 'warn'}">${r.status}</span>
          </div>
        `).join('') : '<div class="tx-empty">No transfers yet.</div>'}
      </div>
    `;
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
    const cats = el.querySelectorAll('.tx-cat');
    cats.forEach(sel => sel.addEventListener('change', () => _changeCategory(sel)));
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

  /* ── accounts ───────────────────────────────────────────────────── */
  async function _renderAccounts(el) {
    const d = await U.fetchJSON('/api/transactions/accounts');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Accounts</h3>
        ${d.accounts && d.accounts.length ? d.accounts.map(a => `
          <div class="tx-row">
            <span class="tx-account-alias">${U.esc(a.alias)}</span>
            <span class="tx-account-provider">${U.esc(a.provider)}</span>
            <span class="tx-account-masked">${U.esc(a.masked || '---')}</span>
          </div>
        `).join('') : '<div class="tx-empty">No accounts registered.</div>'}
      </div>
    `;
  }

  /* ── imports ────────────────────────────────────────────────────── */
  async function _renderImports(el) {
    const d = await U.fetchJSON('/api/transactions/imports');
    const rows = (d.batches || []).map(b => {
      const canConfirm = b.state === 'preview';
      return `
        <div class="tx-row" data-batch="${b.id}">
          <span class="tx-import-kind">${b.kind === 'gopay_pdf' ? '📄' : '📧'} ${U.esc(b.provider || '')}</span>
          <span class="tx-import-state tx-badge tx-badge-${b.state === 'committed' ? 'good' : 'warn'}">${b.state}</span>
          <span class="tx-import-date">${U.esc(b.created_at || '')}</span>
          ${canConfirm ? '<button class="tx-btn tx-btn-sm" onclick="Tabs.transactions._confirmImport(event)">Confirm</button>' : ''}
        </div>`;
    }).join('');
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Import History</h3>
        ${rows || '<div class="tx-empty">No imports yet. Upload a GoPay PDF to start.</div>'}
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

  /* ── upload PDF ─────────────────────────────────────────────────── */
  async function _uploadPDF() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf';
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      if (_uploadBusy) return toast('Upload already in progress', false);
      _uploadBusy = true;
      _busy(true);
      try {
        const b64 = await _fileToBase64(file);
        const res = await _post('/api/transactions/upload',
          { filename: file.name, data: b64 }, 30000);
        if (res.ok) {
          toast(`Uploaded: ${res.row_count} rows parsed`);
          await refreshView();
        } else {
          toast(res.error || 'Upload failed', false);
        }
      } catch (e) {
        toast(e.message, false);
      } finally {
        _busy(false);
        _uploadBusy = false;
      }
    };
    input.click();
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
