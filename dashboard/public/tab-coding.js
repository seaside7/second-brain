/* ═══════════════════════════════════════════════════════════════════
   tab-coding.js — 🤖 Coding Agent tab (per-repo terminal)

   Simplified model: a grid of repo cards. Click a card to open a
   terminal-style chat bound to ONE persistent opencode session per repo
   (rooted in the real checkout). Plan ⇄ Build is a visible toggle.
   Preview is only relevant for frontend repos.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const CodingTab = (() => {
  const { fetchJSON } = U;

  const T = {
    repos: [],
    configured: false,
    root: null,
    active: null,        // repo name currently open in the terminal
    session: null,       // current repo's session view
    interval: null,
  };

  const $ = id => document.getElementById(id);
  const esc = U.esc;

  async function load() {
    const panel = $('tab-coding');
    if (!panel) return;
    if (!T.repos.length) await refreshRepos();
    renderShell(panel);
    bindEvents(panel);
    renderBody();
    startPoll();
  }

  function stopPoll() {
    if (T.interval) { clearInterval(T.interval); T.interval = null; }
  }

  function startPoll() {
    stopPoll();
    T.interval = setInterval(async () => {
      const panel = $('tab-coding');
      if (!panel || !panel.classList.contains('is-active') || document.hidden) { stopPoll(); return; }
      if (T.active) {
        // terminal open -> refresh the session (new messages / answers)
        await refreshSession(false);
      } else {
        // grid -> refresh repo dirty/type only (cheap), no session spin-up
        await refreshRepos();
        renderRepos();
      }
    }, 8000);
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

  async function refreshSession(spinUp = true) {
    if (!T.active) return;
    try {
      const res = await fetchJSON(`/api/coding/repos/${encodeURIComponent(T.active)}/session`);
      if (res && res.repo) T.session = res;
      else T.session = null;
    } catch (err) {
      T.session = null;
      if (spinUp) Comp.toast(`Session error: ${err.message}`, false);
    }
    renderBody();
  }

  /* ── shell + body rendering (grid OR terminal) ───────────────────── */
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
        <div id="coding-body"></div>
      </div>`;
  }

  function renderBody() {
    const body = $('coding-body');
    if (!body) return;
    if (T.active) renderTerminal(body);
    else renderGrid(body);
  }

  /* ── grid: repo cards only ───────────────────────────────────────── */
  function renderGrid(body) {
    if (!T.configured) {
      body.innerHTML = `<div class="empty-state"><div class="empty-icon">⚙️</div>
        <div class="empty-title">Coding Agent not configured</div>
        <div class="empty-hint">Set CODING_PROJECTS_ROOT (and CODING_OPENCODE_BIN) on the server, then restart.</div></div>`;
      return;
    }
    if (!T.repos.length) {
      body.innerHTML = `<div class="empty-state"><div class="empty-title">No repos found under PROJECTS_ROOT</div>
        <div class="empty-hint">Each child directory must contain a .git folder.</div></div>`;
      return;
    }
    body.innerHTML = `<div class="coding-repo-grid">` + T.repos.map(r => {
      const cfg = r.config || {};
      const dirty = r.dirty ? '<span class="badge badge--warn">dirty</span>' : '';
      const type = cfg.type || 'other';
      const isFe = type === 'fe' || type === 'frontend';
      const isBe = type === 'be' || type === 'api' || type === 'cms' || type === 'php' || type === 'backend';
      const preview = cfg.previewEnabled ? 'preview ✓' : 'no preview';
      return `<div class="coding-repo-card" data-coding-action="open" data-repo="${esc(r.name)}">
        <div class="coding-repo-name">📦 ${esc(r.name)}</div>
        <div class="coding-repo-meta">type: ${esc(type)} · ${esc(r.current_branch)} → ${esc(r.default_branch)}</div>
        <div class="coding-repo-meta">${esc(r.origin_url || 'no origin')}</div>
        ${dirty}
        <div class="coding-actions">
          <span class="coding-type-label">Type:</span>
          <button class="coding-type-btn${isFe ? ' is-active' : ''}" data-coding-action="set-type"
            data-repo="${esc(r.name)}" data-type="fe">FE</button>
          <button class="coding-type-btn${isBe ? ' is-active' : ''}" data-coding-action="set-type"
            data-repo="${esc(r.name)}" data-type="be">BE</button>
          <span class="coding-type-muted">· ${esc(preview)}</span>
        </div>
      </div>`;
    }).join('') + `</div>
    <div class="coding-hint">click a repo to open its terminal (one persistent session per repo)</div>`;
  }

  /* ── terminal: repo open ─────────────────────────────────────────── */
  function renderTerminal(body) {
    const s = T.session || { mode: 'build', messages: [], questions: [], repo_info: {} };
    const info = s.repo_info || {};
    const cfg = info.config || {};
    const type = cfg.type || 'other';
    const isPlan = s.mode === 'plan';
    const previewAllowed = cfg.previewEnabled !== false;
    const hasWorktree = !!info.path;

    const questions = (s.questions || []).filter(q => !q.answered).map(q => `
      <div class="coding-q">
        <strong>⚠️ ${esc(q.event || 'permission')}</strong>
        <pre>${esc(q.text)}</pre>
        <div class="coding-actions">
          <button class="coding-btn coding-btn--primary" data-coding-action="permission"
            data-permission-id="${esc(q.id)}" data-response="allowed">Allow</button>
          <button class="coding-btn coding-btn--danger" data-coding-action="permission"
            data-permission-id="${esc(q.id)}" data-response="denied">Deny</button>
        </div>
      </div>`).join('');

    const msgs = (s.messages || []).map(m => `
      <div class="coding-msg ${esc(m.role)}">
        <div class="role">${m.role === 'user' ? 'you' : 'agent'}</div>
        <div class="body">${U.mdToHtml(m.text)}</div>
      </div>`).join('') || '<div class="coding-hint">no messages yet — type below to start talking to this repo</div>';

    body.innerHTML = `
      <div class="card coding-detail-card coding-terminal">
        <div class="coding-terminal-head">
          <button class="coding-btn" data-coding-action="back">← Repos</button>
          <div class="coding-term-title">📦 ${esc(T.active)}</div>
          <div class="coding-term-sub">${esc(type)} · ${esc(info.default_branch || '')} · ${esc(info.origin_url || '')}</div>
          <div class="coding-actions">
            <span class="coding-type-label">Mode:</span>
            <span class="coding-mode-toggle">
              <button class="coding-type-btn${isPlan ? ' is-active' : ''}" data-coding-action="mode"
                data-mode="plan">📋 Plan</button>
              <button class="coding-type-btn${!isPlan ? ' is-active' : ''}" data-coding-action="mode"
                data-mode="build">🔨 Build</button>
            </span>
          </div>
        </div>
        <div class="card-body">
          ${(s.questions || []).some(q => !q.answered)
            ? `<div class="coding-field"><label>🔐 Permission requests</label>${questions}</div>` : ''}

          <div class="coding-field"><label>💬 Conversation</label><div class="coding-stream">${msgs}</div></div>

          ${hasWorktree ? `<details class="coding-collapse" data-key="coding:diff:${esc(T.active)}">
            <summary>🔀 Changed files / diff vs HEAD</summary>
            <div id="coding-diff-body"><span class="coding-hint">loading…</span></div>
          </details>` : ''}

          <div class="coding-field"><label>Send to agent</label>
            <textarea id="coding-input" class="coding-input" rows="3"
              placeholder="Type a task or question… (Enter = send, Shift+Enter = newline)"></textarea>
            <div class="coding-actions">
              <button class="coding-btn coding-btn--primary" data-coding-action="send">Send →</button>
              ${previewAllowed && hasWorktree ? `<button class="coding-btn" data-coding-action="preview">▶ Preview</button>` : ''}
              <button class="coding-btn coding-btn--ghost" data-coding-action="reset" title="Forget this repo's session memory">↺ Reset</button>
            </div>
          </div>
        </div>
      </div>`;
    bindDiff(T.active);
    scrollTerminal();
  }

  function scrollTerminal() {
    const stream = document.querySelector('#tab-coding .coding-stream');
    if (stream) stream.scrollTop = stream.scrollHeight;
  }

  async function bindDiff(repo) {
    const details = document.querySelector(`#coding-shell details[data-key="coding:diff:${CSS.escape(repo)}"]`);
    if (!details) return;
    details.addEventListener('toggle', async () => {
      if (!details.open) return;
      const slot = $('coding-diff-body');
      const d = slot.dataset.loaded;
      if (d) return;
      slot.dataset.loaded = '1';
      try {
        const res = await fetchJSON(`/api/coding/repos/${encodeURIComponent(repo)}/diff`);
        const files = (res.files || []).length
          ? res.files.map(f => `<div class="coding-file">${esc(f.flag.padEnd(3))} ${esc(f.path)}</div>`).join('')
          : '<div class="coding-hint">no changes yet</div>';
        slot.innerHTML = (res.diff ? `<pre class="coding-diff">${esc(res.diff)}</pre>` : '') + `<div class="coding-files-list">${files}</div>`;
      } catch (err) {
        slot.innerHTML = `<div class="coding-hint">could not load diff: ${esc(err.message)}</div>`;
      }
    });
  }

  /* ── actions ─────────────────────────────────────────────────────── */
  function bindEvents(panel) {
    if (panel.dataset.codingBound) return;
    panel.dataset.codingBound = '1';
    panel.addEventListener('click', async e => {
      const btn = e.target.closest('[data-coding-action]');
      if (!btn) return;
      const action = btn.dataset.codingAction;
      const repo = btn.dataset.repo || T.active;
      btn.disabled = true;
      try {
        if (action === 'open') {
          T.active = repo;
          T.session = null;
          await refreshSession(true);
        }
        else if (action === 'back') {
          T.active = null;
          T.session = null;
          renderBody();
        }
        else if (action === 'set-type') { e.stopPropagation(); await setRepoType(btn.dataset.repo, btn.dataset.type); }
        else if (action === 'mode') await setMode(btn.dataset.mode);
        else if (action === 'send') await sendPrompt();
        else if (action === 'permission') await permission(btn.dataset.permissionId, btn.dataset.response);
        else if (action === 'reset') { if (confirm('Forget this repo\'s session memory and start fresh?')) await resetRepo(); }
        else if (action === 'preview') { if (infoPreviewUrl()) window.open(infoPreviewUrl(), '_blank'); }
      } catch (err) {
        Comp.toast(`Gagal: ${err.message}`, false);
      } finally {
        btn.disabled = false;
      }
    });
    panel.addEventListener('keydown', e => {
      if (e.target && e.target.id === 'coding-input' && e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const send = panel.querySelector('[data-coding-action="send"]');
        if (send) send.click();
      }
    });
  }

  function infoPreviewUrl() {
    return null; // no backend preview for the per-repo terminal yet; kept as a no-op placeholder
  }

  async function setRepoType(repo, type) {
    const res = await fetchJSON(`/api/coding/repos/${encodeURIComponent(repo)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type }),
    });
    if (res && res.error) throw new Error(res.error);
    if (res && res.repo) {
      const i = T.repos.findIndex(x => x.name === repo);
      if (i >= 0) T.repos[i] = res.repo;
    }
    Comp.toast(`${repo} flagged as ${type.toUpperCase()}`);
    renderBody();
  }

  async function setMode(mode) {
    if (!T.active) return;
    const res = await fetchJSON(`/api/coding/repos/${encodeURIComponent(T.active)}/session/mode`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (res && res.error) throw new Error(res.error);
    T.session = res;
    Comp.toast(mode === 'plan' ? 'Plan mode (read-only)' : 'Build mode (implement)');
    renderBody();
  }

  async function sendPrompt() {
    if (!T.active) return;
    const ta = $('coding-input');
    const text = ta ? ta.value.trim() : '';
    if (!text) return;
    ta.value = '';
    await fetchJSON(`/api/coding/repos/${encodeURIComponent(T.active)}/session/message`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    await refreshSession(true);
  }

  async function permission(permissionId, response) {
    await fetchJSON(`/api/coding/repos/${encodeURIComponent(T.active)}/permission`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ permission_id: permissionId, response }),
    });
    Comp.toast(response === 'allowed' ? 'Permission allowed' : 'Permission denied');
    await refreshSession(true);
  }

  async function resetRepo() {
    await fetchJSON(`/api/coding/repos/${encodeURIComponent(T.active)}/session/reset`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    await refreshSession(true);
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
