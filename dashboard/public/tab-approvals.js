/* ═══════════════════════════════════════════════════════════════════
   tab-approvals.js — 🛡 Approvals: the human-approval gate for external
   actions. Proposed sends/docs/commits sit in journal/state/approval_queue.json
   (workspace-tagged); the owner approves or rejects each here. Data: GET
   /api/approval-queue (server filters by the current workspace — in /samudera
   mode the X-PSB-Workspace header keeps this view Samudera-only). Decisions:
   POST /api/approval-decision, which only flips the item's status and appends
   one line to the append-only action_audit.jsonl — it has NO external effect.

   EXECUTION IS DISABLED BY DESIGN. The external-execution step
   (POST /api/approval-execute) stays off until the owner explicitly enables it
   after joining Samudera (post 2026-08-18, once credentials are provisioned —
   see .agent/workspaces/samudera/CREDENTIALS.md). The gate is triple-layered:
   (1) EXECUTION_ENABLED below, (2) /api/approval-execute is not in the server's
   samudera POST allowlist, (3) the approval-queue skill has no executors
   registered. This panel only records decisions.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(() => {
  const state = {
    items: null,          // /api/approval-queue payload
    error: null,
    status: 'pending',    // all | pending | approved | rejected
  };

  /* Hard gate for the execute affordance. Keep false until the owner flips it
     AFTER joining Samudera (external sends/docs/commits). When true, an
     APPROVED item in a NON-samudera view gets a ▶ Execute button (POST
     /api/approval-execute). The /samudera view never offers execute — the
     server denies that route for samudera regardless of this flag. */
  const EXECUTION_ENABLED = false;

  const STATUS_BADGE = { pending: 'warn', approved: 'good', rejected: 'muted', executed: 'good' };
  const STATUS_LABEL = { pending: '⏳ pending', approved: '✅ approved', rejected: '✋ rejected', executed: '▶ executed' };

  function fmtWib(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString('en-GB',
      { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Jakarta' });
  }

  async function load() {
    const panel = document.getElementById('tab-approvals');
    try {
      state.items = await U.fetchJSON('/api/approval-queue');
      state.error = null;
    } catch (err) {
      state.error = err.message;
    }
    if (panel) render(panel);
  }

  function render(panel) {
    if (state.error) {
      panel.innerHTML = `<div class="load-error">Approvals unavailable: ${U.esc(state.error)}</div>`;
      return;
    }
    const items = (state.items && state.items.items) || [];
    const nPending = items.filter(i => i.status === 'pending').length;
    const nApproved = items.filter(i => i.status === 'approved').length;
    const nRejected = items.filter(i => i.status === 'rejected').length;

    const stChip = (key, label, n) =>
      `<button class="chip apv-status-chip${state.status === key ? ' is-active' : ''}"
        data-status="${key}">${label}${n ? ` <span class="num">${n}</span>` : ''}</button>`;

    const visible = state.status === 'all'
      ? items
      : items.filter(i => i.status === state.status);

    let rows;
    if (!visible.length) {
      rows = Comp.emptyState({
        icon: '🛡',
        title: state.status === 'pending' ? 'Tidak ada aksi menunggu keputusan' : 'Kosong',
        hint: state.status === 'pending'
          ? 'Belum ada proposal aksi eksternal yang butuh approval.'
          : `Tidak ada item berstatus ${state.status}.`,
      });
    } else {
      /* pending first (oldest up), then decided (most recent decision up) */
      const pending = visible.filter(i => i.status === 'pending')
        .sort((a, b) => (a.proposed_wib || '').localeCompare(b.proposed_wib || ''));
      const decided = visible.filter(i => i.status !== 'pending')
        .sort((a, b) => (b.decided_wib || b.proposed_wib || '').localeCompare(a.decided_wib || a.proposed_wib || ''));
      rows =
        (pending.length ? `<div class="section-label">⏳ Menunggu keputusan (${pending.length})</div>
           <div class="rows">${pending.map(itemRow).join('')}</div>` : '') +
        (decided.length ? `<div class="section-label">📜 Sudah diputuskan (${decided.length})</div>
           <div class="rows">${decided.map(itemRow).join('')}</div>` : '');
    }

    const execNote = EXECUTION_ENABLED
      ? `<p class="row-note">▶ Execute enabled — approved items dapat dieksekusi dari panel (non-samudera).</p>`
      : window.PSB_SAMUDERA
        ? `<p class="row-note">🔒 Eksekusi eksternal dinonaktifkan sampai kredensial Samudera diprovisi setelah 2026-08-18. Panel ini hanya mencatat keputusan (approve/reject) ke audit log.</p>`
        : `<p class="row-note">🔒 Eksekusi eksternal dinonaktifkan. Approve/reject hanya mencatat keputusan ke action_audit.jsonl; eksekusi diaktifkan setelah join Samudera (2026-08-18).</p>`;

    panel.innerHTML = `
      <div class="row" data-key="apv-toolbar">
        <span class="row-icon">🛡</span>
        <span class="row-title"><b>Approvals</b> — aksi eksternal yang nunggu keputusan lo</span>
        <span class="row-meta">${U.esc(items.length)} item · audit append-only</span>
      </div>
      ${execNote}
      <div class="chips">
        ${stChip('all', 'Semua', items.length)}
        ${stChip('pending', 'Pending', nPending)}
        ${stChip('approved', 'Approved', nApproved)}
        ${stChip('rejected', 'Rejected', nRejected)}
      </div>
      ${rows}`;
  }

  function itemRow(it) {
    const decided = it.status !== 'pending';
    const statusPill = Comp.badge(STATUS_BADGE[it.status] || 'muted', STATUS_LABEL[it.status] || it.status);
    const wsBadge = it.workspace ? Comp.badge('muted', it.workspace) : '';
    const when = fmtWib(it.proposed_wib);
    const canExecute = EXECUTION_ENABLED && !window.PSB_SAMUDERA && it.status === 'approved';
    const actions = it.status === 'pending'
      ? `<button class="prep-link apv-approve" data-id="${U.esc(it.id)}" title="Setujui — catat ke audit log (tidak langsung mengeksekusi)">✅ Approve</button>
         <button class="prep-link apv-reject" data-id="${U.esc(it.id)}" title="Tolak">✋ Reject</button>`
      : (canExecute
          ? `<button class="prep-link apv-execute" data-id="${U.esc(it.id)}" title="Eksekusi aksi yang sudah disetujui">▶ Execute</button>`
          : '');
    return `<div class="row apv-row${decided ? ' is-dim' : ''}" data-key="apv:${U.esc(it.id)}">
      <span class="row-icon">${it.status === 'pending' ? '⏳' : it.status === 'approved' ? '✅' : it.status === 'executed' ? '▶' : '✋'}</span>
      <span class="row-title apv-open-detail" data-id="${U.esc(it.id)}" title="${U.esc(it.detail || '')}">
        <b>${U.esc(it.id)}</b> ${U.esc((it.action || '') + ' → ' + (it.target || ''))} — ${U.esc((it.detail || '').slice(0, 110))}</span>
      <span class="row-badges">${wsBadge}${it.project ? Comp.badge('cat', it.project) : ''}</span>
      <span class="row-meta">${U.esc(when)}</span>
      <span class="row-right">${statusPill}${actions}
        <button class="prep-link apv-open-detail" data-id="${U.esc(it.id)}">🔍</button></span>
    </div>`;
  }

  function openDetail(id) {
    const it = ((state.items && state.items.items) || []).find(x => x.id === id);
    if (!it) return;
    const rows = [
      ['ID', it.id],
      ['Workspace', it.workspace || '—'],
      ['Action', it.action || '—'],
      ['Target', it.target || '—'],
      ['Project', it.project || '—'],
      ['Detail', it.detail || '—'],
      ['Status', it.status || '—'],
      ['Proposed', fmtWib(it.proposed_wib) || '—'],
      ['Decided', fmtWib(it.decided_wib) || '—'],
      ['Decision note', it.decision_note || '—'],
      ['Executed', fmtWib(it.executed_wib) || '—'],
    ].map(([k, v]) =>
      `<div class="row-subtext"><b>${U.esc(k)}</b>: <span class="row-note">${U.esc(v)}</span></div>`).join('');
    const body = `<div class="stack">
      ${rows}
      <div class="action-bar">
        ${it.status === 'pending'
          ? `<button class="prep-link apv-approve" data-id="${U.esc(it.id)}">✅ Approve</button>
             <button class="prep-link apv-reject" data-id="${U.esc(it.id)}">✋ Reject</button>
             <span class="row-note">Keputusan dicatat ke action_audit.jsonl (append-only). Eksekusi tetap nonaktif sampai setelah join Samudera.</span>`
          : `<span class="row-note">${Comp.badge(STATUS_BADGE[it.status] || 'muted', STATUS_LABEL[it.status] || it.status)} Sudah diputuskan — tidak bisa diubah dari UI.</span>`}
      </div>
    </div>`;
    Drawer.openHtml(`${it.id} — ${it.action || 'action'}`, body);
  }

  async function decide(id, decision, label) {
    const it = ((state.items && state.items.items) || []).find(x => x.id === id);
    const target = it ? `${it.action} → ${it.target}` : id;
    const ok = window.confirm(`${label === 'Approve' ? '✅ Setujui' : '✋ Tolak'} "${target}"?\n\nIni hanya mencatat keputusan ke audit log — tidak ada yang dikirim/dieksekusi.`);
    if (!ok) return;
    const btn = document.querySelector(`.apv-${label.toLowerCase()}[data-id="${CSS.escape(id)}"]`);
    if (btn) { btn.disabled = true; }
    try {
      await U.fetchJSON('/api/approval-decision', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, decision: decision, workspace: it ? it.workspace : undefined }),
      });
      Comp.toast(`${label}: ${id} (tercatat di audit log)`, true);
      load();
    } catch (err) {
      Comp.toast(`Gagal: ${err.message}`, false);
      if (btn) btn.disabled = false;
    }
  }

  async function execute(id) {
    const it = ((state.items && state.items.items) || []).find(x => x.id === id);
    const ok = window.confirm(`▶ Eksekusi "${it ? it.action + ' → ' + it.target : id}"? Aksi eksternal akan dijalankan.`);
    if (!ok) return;
    const btn = document.querySelector(`.apv-execute[data-id="${CSS.escape(id)}"]`);
    if (btn) { btn.disabled = true; btn.textContent = '⏳…'; }
    try {
      await U.fetchJSON('/api/approval-execute', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, workspace: it ? it.workspace : undefined }),
      });
      Comp.toast(`Eksekusi: ${id}`, true);
      load();
    } catch (err) {
      Comp.toast(`Eksekusi gagal: ${err.message}`, false);
      if (btn) { btn.disabled = false; btn.textContent = '▶ Execute'; }
    }
  }

  /* ── delegated clicks ── */
  document.addEventListener('click', async e => {
    const chip = e.target.closest('.apv-status-chip');
    if (chip) {
      e.preventDefault();
      state.status = chip.dataset.status;
      const panel = document.getElementById('tab-approvals');
      if (panel) render(panel);
      return;
    }
    const detail = e.target.closest('.apv-open-detail');
    if (detail) { e.preventDefault(); openDetail(detail.dataset.id); return; }
    const apv = e.target.closest('.apv-approve');
    if (apv) { e.preventDefault(); await decide(apv.dataset.id, 'approve', 'Approve'); return; }
    const rej = e.target.closest('.apv-reject');
    if (rej) { e.preventDefault(); await decide(rej.dataset.id, 'reject', 'Reject'); return; }
    const exec = e.target.closest('.apv-execute');
    if (exec) { e.preventDefault(); await execute(exec.dataset.id); return; }
  });

  Tabs.approvals = { load };
})();
