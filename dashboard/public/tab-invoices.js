/* ═══════════════════════════════════════════════════════════════════
   tab-invoices.js — 🧾 Invoice tab (personal/Catalyze only)

   Generate Catalyze invoices from the monthly time-log sheet, then
   download the resulting PDF to local disk.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const InvoiceTab = (() => {
  const { toast } = U;

  const MONTHS = ['January','February','March','April','May','June','July',
                  'August','September','October','November','December'];

  async function listInvoices() {
    try {
      return await U.fetchJSON('/api/invoices');
    } catch { return { invoices: [] }; }
  }

  async function generate(month) {
    try {
      return await U.fetchJSON('/api/invoice/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ month }),
      });
    } catch (e) { return { error: e.message }; }
  }

  async function load() {
    const panel = document.getElementById('tab-invoices');
    if (!panel) return;
    render(panel);
    await refreshList();
  }

  function render(slot) {
    const now = new Date();
    slot.innerHTML = `
      <div class="inv-container">
        <div class="inv-card">
          <h3 class="mem-section-title">🧾 Generate Invoice</h3>
          <p class="inv-hint">Generate a Catalyze invoice from the monthly time-log sheet
            (108,000→ per month, Rp175,000/hr).</p>
          <div class="inv-generate-row">
            <select id="inv-month" class="inv-select"></select>
            <button id="inv-generate-btn" class="btn inv-btn">⚡ Generate</button>
          </div>
          <div id="inv-result" class="inv-result"></div>
        </div>

        <div class="inv-card">
          <div class="mem-panel-header">
            <h3 class="mem-section-title">📄 Generated Invoices</h3>
            <button id="inv-refresh" class="btn inv-btn-sm">↻ Refresh</button>
          </div>
          <div id="inv-list" class="inv-list"></div>
        </div>
      </div>
    `;
    _populateMonths(slot);
    slot.querySelector('#inv-generate-btn').addEventListener('click', _doGenerate);
    slot.querySelector('#inv-refresh').addEventListener('click', refreshList);
    slot.querySelector('#inv-month').addEventListener('change', () => {
      const r = slot.querySelector('#inv-result');
      if (r) r.innerHTML = '';
    });
  }

  function _populateMonths(slot) {
    const sel = slot.querySelector('#inv-month');
    const now = new Date();
    const currentMonth = now.getMonth(); // 0-based
    let html = '';
    // Offer last few months + current; default to current month
    for (let i = 0; i < 4; i++) {
      const idx = (currentMonth - i + 12) % 12;
      const m = MONTHS[idx];
      html += `<option value="${m}" ${i === 0 ? 'selected' : ''}>${m}</option>`;
    }
    sel.innerHTML = html;
  }

  function _doGenerate() {
    const month = document.getElementById('inv-month').value;
    const btn = document.getElementById('inv-generate-btn');
    const res = document.getElementById('inv-result');
    if (!month || !btn || !res) return;
    btn.disabled = true;
    btn.textContent = '⏳ Generating…';
    res.innerHTML = '<div class="mem-loading">Reading sheet + building PDF…</div>';
    res.classList.remove('inv-err', 'inv-ok');
    generate(month).then(data => {
      btn.disabled = false;
      btn.textContent = '⚡ Generate';
      if (data.error) {
        res.classList.add('inv-err');
        res.innerHTML = `<b>Failed:</b> ${U.esc(data.error)}`;
        return;
      }
      res.classList.add('inv-ok');
      const lines = [];
      for (const key of ['invoice_number','invoice_date','total_hours','total_amount']) {
        if (data[key]) lines.push(`<div class="inv-sum"><span>${key.replace(/_/g,' ')}</span><b>${U.esc(data[key])}</b></div>`);
      }
      res.innerHTML = `<div class="inv-success">✅ ${U.esc(data.invoice_number || 'Invoice')} generated</div>` +
        (lines.length ? `<div class="inv-sums">${lines.join('')}</div>` : '');
      refreshList();
    }).catch(err => {
      btn.disabled = false;
      btn.textContent = '⚡ Generate';
      res.classList.add('inv-err');
      res.innerHTML = `<b>Error:</b> ${U.esc(err.message)}`;
    });
  }

  async function refreshList() {
    const el = document.getElementById('inv-list');
    if (!el) return;
    el.innerHTML = '<div class="mem-loading">Loading…</div>';
    const data = await listInvoices();
    const items = data.invoices || [];
    if (!items.length) {
      el.innerHTML = '<div class="mem-empty">No invoices generated yet. Generate one above.</div>';
      return;
    }
    el.innerHTML = items.map(it => `
      <div class="inv-row">
        <div class="inv-row-info">
          <div class="inv-row-title">📄 ${U.esc(it.name)}</div>
          <div class="inv-row-meta">${it.mtime_wib || ''} · ${U.esc(_fmtSize(it.size))}</div>
        </div>
        <a class="btn inv-btn-sm" href="${it.url}" download>⬇ Download</a>
      </div>
    `).join('');
  }

  function _fmtSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }

  return { load };
})();

window.Tabs.invoices = InvoiceTab;
