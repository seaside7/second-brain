/* tab-tasks.js — 🎫 Tasks tab: Trello cards assigned to you, harvested from
   Gmail "added you to the card" notifications (see dashboard/tasks_agent.py
   for why — there's no direct Trello API connection yet, so this is all the
   detail the notification email carries: card name, board, a direct link).
   Manual refresh only, per the owner — no background poller.

   Each task gets a fuzzy-matched (or manually picked) repo. "Submit" hands
   the card off to the Coding Agent tab with that repo opened and the prompt
   pre-filled via a one-time sessionStorage handoff — the owner still has to
   click Send there themselves; this never starts an agent run on its own. */
'use strict';

window.Tabs = window.Tabs || {};

const TasksTab = (() => {
  const T = { tasks: [], repos: [], lastRefreshed: null, refreshing: false };

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function fmtWhen(iso) {
    if (!iso) return 'not pulled yet';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return 'not pulled yet';
    return `last pulled ${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} · ${
      d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
  }

  function repoOptions(selected) {
    const opts = ['<option value="">— pick repo —</option>'].concat(
      T.repos.map(r => `<option value="${U.esc(r)}"${r === selected ? ' selected' : ''}>${U.esc(r)}</option>`)
    );
    return opts.join('');
  }

  function taskRow(t, { dismissed = false } = {}) {
    const meta = [t.person, fmtDate(t.date)].filter(Boolean).join(' · ');
    const badges = t.board ? [Comp.badge('muted', t.board)] : [];
    const right = dismissed
      ? `<span class="task-actions" data-id="${U.esc(t.id)}">
           ${t.repo ? `<span class="row-meta">${U.esc(t.repo)}</span>` : ''}
           <button class="task-reopen-btn" data-role="reopen">${Comp.ic('refresh')} Reopen</button>
         </span>`
      : `<span class="task-actions" data-id="${U.esc(t.id)}">
           <select class="task-repo-select" data-role="repo">${repoOptions(t.repo)}</select>
           <button class="task-submit-btn" data-role="submit"${t.repo ? '' : ' disabled'}
             title="${t.repo ? 'Open in Coding Agent' : 'Pick a repo first'}">${Comp.ic('send')} Submit</button>
           <button class="task-dismiss-btn" data-role="dismiss" title="Dismiss" aria-label="Dismiss">${Comp.ic('close')}</button>
         </span>`;
    const expandBody = t.card_url
      ? `${t.person ? `<p class="row-subtext">${U.esc(t.person)} added you to this card.</p>` : ''}
         <p class="row-subtext"><a href="${U.esc(t.card_url)}" target="_blank" rel="noopener">${Comp.ic('link')} Open in Trello ↗</a></p>`
      : null;
    return Comp.listRow({ key: `task-${t.id}`, icon: 'ticket', title: t.card, badges, meta, right, expandBody });
  }

  function render(panel) {
    if (!panel) return;
    const open = T.tasks.filter(t => t.status !== 'dismissed');
    const dismissed = T.tasks.filter(t => t.status === 'dismissed');
    panel.innerHTML = `
      <div class="task-toolbar">
        <span class="task-refreshed">${U.esc(fmtWhen(T.lastRefreshed))}</span>
        <button class="btn-refresh" id="task-refresh-btn"${T.refreshing ? ' disabled' : ''}>
          ${Comp.ic('refresh')}<span>${T.refreshing ? 'Pulling…' : 'Refresh'}</span></button>
      </div>
      <p class="row-note">Pulled from Trello's "added you to the card" emails — card, board and a link, no
        description yet (that needs the Trello API key/token). Repo is auto-matched to the board name where
        confident; pick it yourself otherwise.</p>
      ${open.length
        ? `<div class="rows">${open.map(t => taskRow(t)).join('')}</div>`
        : Comp.emptyState({ icon: 'ticket', title: 'No open tasks',
                            hint: 'Click Refresh to pull new Trello assignments from Gmail.' })}
      ${dismissed.length ? Comp.card({
          key: 'tasks-dismissed', icon: 'archive', title: 'Dismissed',
          count: String(dismissed.length),
          body: `<div class="rows">${dismissed.map(t => taskRow(t, { dismissed: true })).join('')}</div>`,
        }) : ''}`;
  }

  async function load() {
    const panel = $id('tab-tasks');
    if (!panel) return;
    panel.innerHTML = `<div class="skeleton"><div class="skeleton-line w-60"></div>
      <div class="skeleton-line w-80"></div><div class="skeleton-line"></div></div>`;
    try {
      const data = await U.fetchJSON('/api/tasks');
      T.tasks = data.tasks || [];
      T.repos = data.repos || [];
      T.lastRefreshed = data.last_refreshed || null;
      render(panel);
    } catch (err) {
      panel.innerHTML = `<div class="load-error">Could not load tasks: ${U.esc(err.message)}</div>`;
    }
  }

  async function refresh() {
    if (T.refreshing) return;
    T.refreshing = true;
    render($id('tab-tasks'));
    try {
      const data = await U.fetchJSON('/api/tasks/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      T.tasks = data.tasks || [];
      T.lastRefreshed = data.last_refreshed || null;
      Comp.toast('Tasks refreshed', true);
    } catch (err) {
      Comp.toast(`Refresh failed: ${err.message}`, false);
    } finally {
      T.refreshing = false;
      render($id('tab-tasks'));
    }
  }

  async function setRepo(id, repo) {
    try {
      const data = await U.fetchJSON(`/api/tasks/${encodeURIComponent(id)}/repo`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repo }),
      });
      T.tasks = data.tasks || T.tasks;
    } catch (err) {
      Comp.toast(`Could not set repo: ${err.message}`, false);
    }
    render($id('tab-tasks'));
  }

  async function setStatus(id, status) {
    const verb = status === 'dismissed' ? 'dismiss' : 'reopen';
    try {
      const data = await U.fetchJSON(`/api/tasks/${encodeURIComponent(id)}/${verb}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      T.tasks = data.tasks || T.tasks;
      Comp.toast(status === 'dismissed' ? 'Task dismissed' : 'Task reopened', true);
    } catch (err) {
      Comp.toast(`Failed: ${err.message}`, false);
    }
    render($id('tab-tasks'));
  }

  function submit(id) {
    const t = T.tasks.find(x => x.id === id);
    if (!t || !t.repo) return;
    const prompt = [
      `Trello task: ${t.card}`,
      `Board: ${t.board}`,
      t.person ? `Assigned by: ${t.person}` : null,
      t.card_url ? `Link: ${t.card_url}` : null,
    ].filter(Boolean).join('\n');
    try { sessionStorage.setItem('psb:coding-prefill', prompt); } catch (e) { /* private mode etc — non-fatal */ }
    location.hash = `#coding/${encodeURIComponent(t.repo)}`;
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('#tab-tasks')) return;
    if (e.target.closest('#task-refresh-btn')) { refresh(); return; }
    const actions = e.target.closest('.task-actions');
    if (!actions) return;
    const id = actions.dataset.id;
    const roleBtn = e.target.closest('[data-role]');
    if (!roleBtn) return;
    if (roleBtn.dataset.role === 'submit') submit(id);
    else if (roleBtn.dataset.role === 'dismiss') setStatus(id, 'dismissed');
    else if (roleBtn.dataset.role === 'reopen') setStatus(id, 'open');
  });

  document.addEventListener('change', e => {
    const sel = e.target.closest('.task-repo-select');
    if (!sel) return;
    const id = sel.closest('.task-actions').dataset.id;
    setRepo(id, sel.value || null);
  });

  window.Tabs.tasks = { load };
  return { load };
})();
