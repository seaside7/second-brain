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
    panel.querySelector('#tx-upload-btn').addEventListener('click', _uploadPDF);
  }

  /* ── refresh ────────────────────────────────────────────────────── */
  async function refreshView() {
    const body = document.getElementById('tx-body');
    if (!body) return;
    body.innerHTML = '<div class="tx-loading">Loading...</div>';
    _busy(true);

    try {
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
        <div class="tx-stat"><div class="tx-stat-label">Net</div><div class="tx-stat-value ${d.net >= 0 ? 'tx-pos' : 'tx-neg'}">${rp(d.net)}</div></div>
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
    if (!rows.length) return '<div class="tx-empty">No transactions yet. Upload a GoPay PDF or sync Gmail to start.</div>';
    return `<div class="tx-card"><h3 class="tx-card-title">Recent</h3>${
      rows.map(r => _txRow(r)).join('')
    }</div>`;
  }

  /* ── all transactions ───────────────────────────────────────────── */
  async function _renderAll(el) {
    const d = await U.fetchJSON(`/api/transactions/list?limit=100`);
    el.innerHTML = `
      <div class="tx-card">
        <div class="tx-card-header"><h3 class="tx-card-title">All Transactions (${d.total || 0})</h3></div>
        ${d.rows && d.rows.length ? d.rows.map(r => _txRow(r)).join('') : '<div class="tx-empty">No transactions yet.</div>'}
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
    el.innerHTML = `
      <div class="tx-card"><h3 class="tx-card-title">Review Queue (${d.count || 0})</h3>
        ${d.rows && d.rows.length ? d.rows.map(r => `
          <div class="tx-row tx-review-row" data-id="${r.id}">
            <div class="tx-review-info">
              <span>${U.esc(r.description || r.merchant || r.notes || 'Unknown')}</span>
              <span class="tx-amount">${rp(r.amount)}</span>
            </div>
            <div class="tx-review-actions">
              <button class="btn tx-btn-sm tx-btn-skip" data-id="${r.id}">Skip</button>
              <button class="btn tx-btn-sm tx-btn-cat" data-id="${r.id}">Categorize</button>
            </div>
          </div>
        `).join('') : '<div class="tx-empty">All clear! No transactions need review.</div>'}
      </div>
    `;
    el.querySelectorAll('.tx-btn-skip').forEach(b =>
      b.addEventListener('click', () => _reviewSkip(b.dataset.id)));
    el.querySelectorAll('.tx-btn-cat').forEach(b =>
      b.addEventListener('click', () => _reviewCategorize(b.dataset.id)));
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

  async function _reviewCategorize(id) {
    // TODO: open category picker drawer
    toast('Category picker coming soon');
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

  /* ── single transaction row ─────────────────────────────────────── */
  function _txRow(r) {
    const desc = U.esc(r.description || r.merchant || r.notes || 'Unknown');
    const amount = r.amount || r.total_amount || 0;
    const dir = r.direction === 'in' ? 'tx-pos' : 'tx-neg';
    const sign = r.direction === 'in' ? '+' : '-';
    const nature = r.nature || 'needs_review';
    const statusBadge = r.review_status === 'review' ? '<span class="tx-badge tx-badge-warn">review</span>' :
                        r.review_status === 'uncategorized' ? '<span class="tx-badge tx-badge-muted">uncategorized</span>' : '';
    return `
      <div class="tx-row" data-id="${r.id}">
        <div class="tx-row-main">
          <span class="tx-row-desc">${desc}</span>
          <span class="tx-row-amount ${dir}">${sign}${rp(amount)}</span>
        </div>
        <div class="tx-row-meta">
          <span class="tx-row-nature">${nature}</span>
          <span class="tx-row-date">${U.esc(r.occurred_at || r.created_at || '')}</span>
          ${statusBadge}
        </div>
      </div>`;
  }

  return { load, _confirmImport };
})();

window.Tabs.transactions = TransactionsTab;
