/* ═══════════════════════════════════════════════════════════════════
   tab-coding.js — 🤖 Coding Agent tab

   Run OpenCode coding jobs against repos under CODING_PROJECTS_ROOT.
   Each job gets its own opencode server + git worktree, staging changes on
   the repo's `staging` branch by default. Plan/build/commit/push are all
   explicit, human-gated steps surfaced in the job detail view.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const CodingTab = (() => {
  const { fetchJSON } = U;

  const T = {
    repos: [],
    configured: false,
    root: null,
    selectedRepo: null,
    jobs: [],
    selected: null,   // job id currently open in detail
    interval: null,
    lastJobsHash: '',
  };

  const $ = id => document.getElementById(id);
  const esc = U.esc;

  const STATUS_BADGE = {
    created: 'muted', planning: 'warn', awaiting_build_approval: 'warn',
    building: 'warn', testing: 'warn', await_commit_approval: 'warn',
    awaiting_commit_approval: 'warn', testing_failed: 'critical',
    awaiting_push_approval: 'serious', pushed: 'ok', failed: 'critical',
    cancelled: 'muted',
  };

  async function load() {
    const panel = $('tab-coding');
    if (!panel) return;
    if (!T.repos.length) await refreshRepos();
    renderShell(panel);
    bindEvents(panel);
    await refreshJobs();
    startPoll();
  }

  function stopPoll() {
    if (T.interval) { clearInterval(T.interval); T.interval = null; }
  }

  function startPoll() {
    stopPoll();
    T.interval = setInterval(() => {
      const panel = $('tab-coding');
      if (!panel || !panel.classList.contains('is-active') || document.hidden) { stopPoll(); return; }
      const hasActive = T.jobs.some(j => /planning|building|testing|pushing|running/.test(j.status));
      if (!hasActive) { stopPoll(); return; }
      refreshJobs();
    }, 10000);
  }

  function activeTabName() {
    return ((location.hash || '#today').replace(/^#/, '').split('/')[0]) || 'today';
  }

  async function refreshRepos() {
    try {
      const res = await fetchJSON('/api/coding/repos');
      T.repos = res.repos || [];
      T.configured = !!res.configured;
      T.root = res.root;
    } catch {
      T.repos = [];
      T.configured = false;
    }
  }

  /* ── shell (built once; sub-containers update on refresh) ────────── */
  function renderShell(panel) {
    if (panel.querySelector('#coding-shell')) return;
    panel.innerHTML = `
      <div id="coding-shell">
        <div class="coding-intro">
          <strong>🤖 Coding Agent</strong>
          <span class="coding-intro-sub ${T.configured ? '' : 'is-warn'}">
            ${T.configured ? `repos: ${esc(T.root)}` : 'not configured — set CODING_PROJECTS_ROOT on the server'}
          </span>
        </div>

        <details class="card" data-key="coding:newjob">
          <summary>
            <span class="card-icon">🛠️</span>
            <span class="card-title">New coding job</span>
            <span class="card-spacer"></span>
            <span class="card-chevron">▸</span>
          </summary>
          <div class="card-body">
            <div class="coding-form">
              <div class="coding-field">
                <label>Repository</label>
                <select id="coding-repo"></select>
              </div>
              <div class="coding-field">
                <label>Mode</label>
                <div class="coding-actions">
                  <label class="coding-pill"><input type="radio" name="coding-mode" value="plan" checked>
                    <span>📋 Plan (read-only)</span></label>
                  <label class="coding-pill"><input type="radio" name="coding-mode" value="build">
                    <span>🔨 Build (implement)</span></label>
                </div>
              </div>
              <div class="coding-field">
                <label>Task</label>
                <textarea id="coding-task"
                  placeholder="Describe what to build e.g. Add a Health endpoint returning { status: 'ok' }"></textarea>
              </div>
              <div class="coding-field">
                <label>Branch (default: staging — feature branch only when you type one here)</label>
                <input type="text" id="coding-branch" value="staging">
              </div>
              <div class="coding-field">
                <label>Attachments (max 10 × 2MB — images/docs to reference)</label>
                <input type="file" id="coding-files" multiple>
              </div>
              <div class="coding-actions">
                <button class="coding-btn coding-btn--primary" data-coding-action="start">▶ Start job</button>
              </div>
            </div>
          </div>
        </details>

        <div id="coding-repos"></div>
        <div id="coding-jobs"></div>
        <div id="coding-detail"></div>
      </div>`;
    // repo options
    fillRepos();
  }

  function fillRepos() {
    const sel = $('coding-repo');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = T.repos.map(r =>
      `<option value="${esc(r.name)}" ${r.name === current ? 'selected' : ''}>${esc(r.name)}</option>`
    ).join('') || '<option value="">— no repos —</option>';
  }

  function bindEvents(panel) {
    if (panel.dataset.codingBound) return;
    panel.dataset.codingBound = '1';
    panel.addEventListener('click', async e => {
      const btn = e.target.closest('[data-coding-action]');
      if (!btn) return;
      const action = btn.dataset.codingAction;
      const id = btn.dataset.jobId || null;
      btn.disabled = true;
      try {
        if (action === 'start') await startJob();
        else if (action === 'approve') await approve(id, btn.dataset.gate);
        else if (action === 'permission') await permission(id, btn.dataset.permissionId, btn.dataset.response);
        else if (action === 'stop') await stopJob(id);
        else if (action === 'delete') {
          if (confirm('Delete this job permanently? This removes its worktree and log.')) await deleteJob(id);
        }
        else if (action === 'preview-start') await preview('start', id);
        else if (action === 'preview-stop') await preview('stop', id);
        else if (action === 'prompt') await sendPrompt(id);
        else if (action === 'select') T.selected = id;
        else if (action === 'select-repo') {
          const sel = $('coding-repo');
          const repo = btn.dataset.repo;
          if (sel && repo) { sel.value = repo; T.selectedRepo = repo; }
          const details = document.querySelector('#tab-coding details[data-key="coding:newjob"]');
          if (details) { details.open = true; details.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
          renderRepos();
        }
        await refreshJobs();
      } catch (err) {
        Comp.toast(`Gagal: ${err.message}`, false);
      } finally {
        btn.disabled = false;
      }
    });
    panel.addEventListener('input', e => {
      if (e.target.id === 'coding-branch') {
        const v = e.target.value.trim();
        if (['main', 'master', 'develop'].includes(v)) {
          Comp.toast(`${v} cannot be used — falling back to staging`, false);
          e.target.value = 'staging';
        }
      }
    });
  }

  /* ── job lifecycle actions ────────────────────────────────────────── */
  async function startJob() {
    const repo = ($('coding-repo') || {}).value || '';
    const mode = (panelValue('input[name="coding-mode"]:checked')) || 'build';
    const task = ($('coding-task') || {}).value || '';
    const branch = ($('coding-branch') || {}).value || 'staging';
    if (!repo) throw new Error('pick a repo first');
    if (!task.trim()) throw new Error('task is required');
    const files = await readFiles($('coding-files'));
    const res = await fetchJSON('/api/coding/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, mode, task, branch, files }),
    });
    if (!res || !res.ok) throw new Error((res && res.error) || 'create failed');
    Comp.toast(`Job started: ${res.job.id}`);
    T.selected = res.job.id;
    const ta = $('coding-task'); if (ta) ta.value = '';
    const fi = $('coding-files'); if (fi) fi.value = '';
  }

  function panelValue(selector) {
    const el = document.querySelector(`#tab-coding ${selector}`);
    return el ? el.value : '';
  }

  function readFiles(input) {
    const files = input && input.files ? [...input.files] : [];
    return Promise.all(files.map(f => new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res({ name: f.name, content_b64: String(r.result).split(',')[1] || '' });
      r.onerror = rej;
      r.readAsDataURL(f);
    })));
  }

  async function approve(id, gate) {
    const res = await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate }),
    });
    const err = res && res.error;
    if (err) throw new Error(err);
    Comp.toast(`Approved: ${gate}`);
  }

  async function permission(id, permissionId, response) {
    await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/permission`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ permission_id: permissionId, response }),
    });
    Comp.toast(response === 'allowed' ? 'Permission allowed' : 'Permission denied');
  }

  async function stopJob(id) {
    await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/stop`, { method: 'POST' });
    Comp.toast('Job stopped');
  }

  async function deleteJob(id) {
    const res = await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/delete`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (res && res.error) throw new Error(res.error);
    if (T.selected === id) T.selected = null;
    Comp.toast('Job deleted');
  }

  async function preview(mode, id) {
    const res = await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/preview/${mode}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (res && res.error) throw new Error(res.error);
    if (mode === 'start' && res && res.url) window.open(res.url, '_blank');
    Comp.toast(mode === 'start' ? 'Preview started' : 'Preview stopped');
  }

  async function sendPrompt(id) {
    const ta = $('coding-followup');
    const text = ta ? ta.value.trim() : '';
    if (!text) return;
    await fetchJSON(`/api/coding/jobs/${encodeURIComponent(id)}/prompt`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (ta) ta.value = '';
    Comp.toast('Follow-up sent');
  }

  /* ── rendering: repos + jobs + detail ─────────────────────────────── */
  function renderRepos() {
    const slot = $('coding-repos');
    if (!slot) return;
    if (!T.configured) {
      slot.innerHTML = `<div class="empty-state"><div class="empty-icon">⚙️</div>
        <div class="empty-title">Coding Agent not configured</div>
        <div class="empty-hint">Set CODING_PROJECTS_ROOT (and CODING_OPENCODE_BIN) on the server, then restart.</div></div>`;
      return;
    }
    if (!T.repos.length) {
      slot.innerHTML = `<div class="empty-state"><div class="empty-title">No repos found under PROJECTS_ROOT</div>
        <div class="empty-hint">Each child directory must contain a .git folder.</div></div>`;
      return;
    }
    slot.innerHTML = `<div class="coding-repo-grid">` + T.repos.map(r => {
      const cfg = r.config || {};
      const dirty = r.dirty ? '<span class="badge badge--warn">dirty</span>' : '';
      const selCls = T.selectedRepo === r.name ? ' is-repo-selected' : '';
      return `<div class="coding-repo-card${selCls}" data-coding-action="select-repo" data-repo="${esc(r.name)}">
        <div class="coding-repo-name">📦 ${esc(r.name)}${selCls ? ' ✓' : ''}</div>
        <div class="coding-repo-meta">${esc(cfg.type || 'repo')} · ${esc(r.current_branch)} → ${esc(r.default_branch)}</div>
        <div class="coding-repo-meta">${esc(r.origin_url || 'no origin')}</div>
        <div class="coding-actions">${dirty}
          ${r.config && r.config.lintCommand ? `<span class="badge">lint ✓</span>` : ''}
        </div>
      </div>`;
    }).join('') + `</div>`;
  }

  function renderJobs() {
    const slot = $('coding-jobs');
    if (!slot) return;
    if (!T.jobs.length) {
      slot.innerHTML = '';
      return;
    }
    slot.innerHTML = `<h3 class="coding-section-title">🧩 Jobs</h3>` + T.jobs.slice(0, 20).map(j => {
      const badge = Comp.badge(STATUS_BADGE[j.status] || 'muted', j.status.replace(/_/g, ' '));
      const sel = T.selected === j.id ? ' is-selected' : '';
      const deletable = !/planning|building|testing|pushing|running/.test(j.status);
      return `<div class="coding-job-row${sel}" data-coding-action="select" data-job-id="${esc(j.id)}">
        <div class="coding-job-main">
          <span class="coding-job-id">${esc(j.id)}</span>
          <span class="coding-job-meta">${esc(j.repo)} · ${esc(j.mode)} · ${esc(j.branch)}</span>
        </div>
        <div class="coding-actions">${badge}
          ${deletable ? `<button class="coding-btn coding-btn--ghost" data-coding-action="delete"
            data-job-id="${esc(j.id)}" title="Delete job">🗑</button>` : ''}
        </div>
      </div>`;
    }).join('') + `<div class="coding-hint">tap a job to open it · auto-refreshes while open</div>`;
  }

  async function renderDetail() {
    const slot = $('coding-detail');
    if (!slot) return;
    if (!T.selected) { slot.innerHTML = ''; return; }
    let job;
    try {
      const res = await fetchJSON(`/api/coding/jobs/${encodeURIComponent(T.selected)}`);
      job = res && res.id ? res : null;
    } catch { job = null; }
    if (!job) { slot.innerHTML = `<div class="empty-state"><div class="empty-title">Job gone</div></div>`; return; }
    const hasWorktree = !!job.directory;
    let files = [], diff = '';
    if (hasWorktree) {
      try {
        const d = await fetchJSON(`/api/coding/jobs/${encodeURIComponent(job.id)}/diff`);
        diff = d.diff || ''; files = d.files || [];
      } catch { /* preview detail still renders */ }
    }

    const questions = (job.questions || []).filter(q => !q.answered).map(q => `
      <div class="coding-q">
        <strong>⚠️ ${esc(q.event)}</strong>
        <pre>${esc(q.text)}</pre>
        <div class="coding-actions">
          <button class="coding-btn coding-btn--primary" data-coding-action="permission"
            data-job-id="${esc(job.id)}" data-permission-id="${esc(q.id)}" data-response="allowed">Allow</button>
          <button class="coding-btn coding-btn--danger" data-coding-action="permission"
            data-job-id="${esc(job.id)}" data-permission-id="${esc(q.id)}" data-response="denied">Deny</button>
        </div>
      </div>`).join('') || '<div class="coding-hint">no pending permission requests</div>';

    const msgs = (job.messages || []).map(m => `
      <div class="coding-msg">
        <div class="role">${esc(m.role)}</div>
        <div class="body">${U.mdToHtml(m.text)}</div>
      </div>`).join('') || '<div class="coding-hint">no messages yet — the job is thinking</div>';

    const tests = (job.test_results || []).map(t => `
      <div class="coding-file">${esc(t.step)}: ${t.ok ? '✅ pass' : '❌ fail'} (rc=${esc(String(t.rc))}) · ${esc(t.command)}</div>
      ${t.tail ? `<pre class="coding-diff">${esc(t.tail.slice(-1200))}</pre>` : ''}`).join('') ||
      '<div class="coding-hint">verification not run yet</div>';

    const filesHtml = (files.length ? files.map(f =>
      `<div class="coding-file">${esc(f.flag.padEnd(3))} ${esc(f.path)}
         <a class="coding-file-open" target="_blank"
            href="/api/coding/jobs/${encodeURIComponent(job.id)}/file?path=${encodeURIComponent(f.path)}">view</a>
       </div>`).join('') : '<div class="coding-hint">no changes yet</div>');

    const approvals = [];
    const isPlanOnly = job.mode === 'plan' && job.status === 'awaiting_build_approval';
    if (isPlanOnly) {
      approvals.push(`<div class="coding-hint">plan complete — approve build to implement, or close</div>`);
    }
    if (job.status === 'awaiting_build_approval' && job.plan && !isPlanOnly) {
      approvals.push(`<button class="coding-btn coding-btn--primary" data-coding-action="approve"
        data-job-id="${esc(job.id)}" data-gate="build">✅ Approve build</button>`);
    }
    if (['awaiting_commit_approval', 'testing_failed'].includes(job.status)) {
      approvals.push(`<button class="coding-btn coding-btn--primary" data-coding-action="approve"
        data-job-id="${esc(job.id)}" data-gate="commit">✅ Approve commit</button>`);
    }
    if (job.status === 'awaiting_push_approval') {
      approvals.push(`<button class="coding-btn coding-btn--primary" data-coding-action="approve"
        data-job-id="${esc(job.id)}" data-gate="push">🚀 Approve push to ${esc(job.branch)}</button>`);
    }

    const previewBlock = hasWorktree ? `
      <div class="coding-actions">
        ${job.preview && job.preview.active
          ? `<button class="coding-btn" data-coding-action="preview-stop" data-job-id="${esc(job.id)}">🛑 Stop preview</button>
             <a class="coding-btn" href="/api/coding/preview/${encodeURIComponent(job.id)}/" target="_blank" rel="noopener">🔗 Open preview</a>`
          : `<button class="coding-btn" data-coding-action="preview-start" data-job-id="${esc(job.id)}">▶ Start preview</button>`}
      </div>` : '';

    const serverLive = job.opencode && job.opencode.pid;
    const actions = [
      ...approvals,
      serverLive
        ? `<button class="coding-btn coding-btn--danger" data-coding-action="stop" data-job-id="${esc(job.id)}">⏹ Stop</button>` : '',
      `<button class="coding-btn" data-coding-action="refresh" data-job-id="">↻ Refresh</button>`,
    ].filter(Boolean).join('') || '<div class="coding-hint">no actions available</div>';

    slot.innerHTML = `
      <div class="card coding-detail-card">
        <summary class="coding-detail-head">
          <span class="coding-job-id">${esc(job.id)}</span>
          <span class="coding-job-meta">${esc(job.repo)} · ${esc(job.mode)} · branch ${esc(job.branch || 'staging')}</span>
          ${Comp.badge(STATUS_BADGE[job.status] || 'muted', job.status.replace(/_/g, ' '))}
        </summary>
        <div class="card-body">
          <div class="coding-field"><label>Task</label><div class="coding-plan">${esc(job.task)}</div></div>

          ${job.plan ? `<div class="coding-field"><label>📋 Plan</label>
            <pre class="coding-plan">${esc(job.plan)}</pre></div>` : ''}

          <div class="coding-field"><label>💬 Conversation</label>${msgs}</div>

          ${questions !== '<div class="coding-hint">no pending permission requests</div>'
            ? `<div class="coding-field"><label>🔐 Permission requests</label>${questions}</div>` : ''}

          ${hasWorktree ? `<div class="coding-field"><label>📄 Changed files</label>${filesHtml}</div>` : ''}
          ${diff ? `<div class="coding-field"><label>🔀 Diff vs ${esc(job.diff_base || 'HEAD')}</label>
            <pre class="coding-diff">${esc(diff)}</pre></div>` : ''}
          ${hasWorktree ? `<div class="coding-field"><label>🧪 Verification</label>${tests}</div>` : ''}
          ${previewBlock}
          <div class="coding-field"><label>Actions</label><div class="coding-actions">${actions}</div></div>

          <div class="coding-field"><label>Follow-up</label>
            <textarea id="coding-followup" placeholder="Ask the agent to fix tests / add details…"></textarea>
            <div class="coding-actions"><button class="coding-btn coding-btn--primary"
              data-coding-action="prompt" data-job-id="${esc(job.id)}">Send →</button></div>
          </div>

          ${job.last_error ? `<div class="coding-q"><strong>Error</strong><pre>${esc(job.last_error)}</pre></div>` : ''}
          <div class="coding-field"><label>🪵 Log</label><pre class="coding-diff">${esc((job.log_tail || []).join('\n'))}</pre></div>
        </div>
      </div>`;
  }

  async function refreshJobs() {
    try {
      const res = await fetchJSON('/api/coding/jobs');
      T.jobs = (res && res.jobs) || [];
    } catch { T.jobs = []; }
    renderRepos();
    fillRepos();
    renderJobs();
    if (T.selected) await renderDetail();
  }

  return { load, _stopPoll: stopPoll };
})();

window.Tabs.coding = CodingTab;

window.addEventListener('hashchange', () => {
  if (((location.hash || '#today').replace(/^#/, '').split('/')[0]) !== 'coding') {
    CodingTab._stopPoll && CodingTab._stopPoll();
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) CodingTab._stopPoll && CodingTab._stopPoll();
});