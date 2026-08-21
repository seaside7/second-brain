/* notes-chat.js - Memory Notes chatbox (Samudera workspace) */
(() => {
  'use strict';

  const btnNotes = document.getElementById('btn-notes');
  if (!btnNotes) return;

  const TYPE_ICONS = {
    definition: '📖', fact: '📝', project_knowledge: '🏗️',
    decision: '⚖️', observation: '👁️', strategy: '🎯',
    task: '✅', milestone: '🏁', reminder: '⏰',
  };

  const TYPE_LABELS = {
    definition: 'Definition', fact: 'Fact', project_knowledge: 'Project Knowledge',
    decision: 'Decision', observation: 'Observation', strategy: 'Strategy',
    task: 'Task', milestone: 'Milestone', reminder: 'Reminder',
  };

  const Notes = {
    recent: [],
    pending: false,
    lastFetch: 0,
  };

  async function fetchRecent() {
    const wsQuery = window.PSB_SAMUDERA ? '?workspace=samudera' : '';
    try {
      Notes.recent = await U.fetchJSON('/api/memory-notes' + wsQuery);
      Notes.lastFetch = Date.now();
    } catch (e) {
      Notes.recent = [];
    }
  }

  function recentListHTML() {
    if (!Notes.recent.length) {
      return '<div class="notes-empty">No memories stored yet. Write your first note below.</div>';
    }
    return '<div class="notes-recent-title">Recent memories</div>' +
      Notes.recent.map(n => {
        const icon = TYPE_ICONS[n.type] || '📝';
        const label = TYPE_LABELS[n.type] || n.type;
        const date = n.date ? ` (due: ${n.date})` : '';
        const proj = n.project ? ` <span class="notes-proj">${U.esc(n.project)}</span>` : '';
        const entities = (n.entities || []).length ? ` <span class="notes-ents">${U.esc(n.entities.join(', '))}</span>` : '';
        return `<div class="notes-item" data-id="${U.esc(n.id)}" title="${U.esc(n.text || '')}">`
          + `<span class="notes-type-badge">${icon} ${label}</span>`
          + `<span class="notes-item-title">${U.esc(n.title || n.text?.substring(0, 60) || '?')}</span>`
          + `${proj}${entities}${date}`
          + `<span class="notes-item-time">${U.esc((n.created_wib || '').substring(0, 16))}</span>`
          + `</div>`;
      }).join('');
  }

  function resultHTML(result) {
    if (result.duplicate) {
      return `<div class="notes-result notes-duplicate">📌 Already stored: ${U.esc(result.message || 'duplicate')}</div>`;
    }
    if (!result.ok) {
      return `<div class="notes-result notes-error">⚠️ ${U.esc(result.error || result.message || 'Failed to store')}</div>`;
    }
    const cls = result.classification || {};
    const icon = TYPE_ICONS[result.type] || '📝';
    const label = TYPE_LABELS[result.type] || result.type;
    let detail = '';
    if (result.entities && result.entities.length) detail += `<div>Entities: ${U.esc(result.entities.join(', '))}</div>`;
    if (result.project) detail += `<div>Project: ${U.esc(result.project)}</div>`;
    if (result.date) detail += `<div>Date: ${U.esc(result.date)}</div>`;
    if (result.stored_to) detail += `<div>Stored to: ${U.esc(result.stored_to)}</div>`;
    return `<div class="notes-result notes-ok">`
      + `<div class="notes-result-header">✅ Stored</div>`
      + `<div class="notes-result-type">${icon} ${label}</div>`
      + `<div class="notes-result-title">${U.esc(result.title || '')}</div>`
      + (result.summary ? `<div class="notes-result-summary">${U.esc(result.summary)}</div>` : '')
      + (detail ? `<div class="notes-result-detail">${detail}</div>` : '')
      + `</div>`;
  }

  async function send(text) {
    text = (text || '').trim();
    if (!text || Notes.pending) return;
    Notes.pending = true;
    renderSendState(text);

    try {
      const res = await U.fetchJSON('/api/memory-note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, workspace: window.PSB_SAMUDERA ? 'samudera' : '' }),
        timeoutMs: 30000,
      });
      appendResult(text, res);
      await fetchRecent();
    } catch (err) {
      appendResult(text, { ok: false, error: err.message });
    }
    Notes.pending = false;
    renderComposer();
  }

  function renderSendState(text) {
    const comp = document.getElementById('notes-composer');
    if (comp) {
      comp.innerHTML = `<div class="notes-pending">Processing: ${U.esc(text.substring(0, 80))}...</div>`;
    }
  }

  function appendResult(text, result) {
    const body = document.getElementById('notes-body');
    if (!body) return;
    body.innerHTML = recentListHTML()
      + `<div class="notes-msg notes-msg-user"><div class="notes-bubble">${U.esc(text)}</div></div>`
      + resultHTML(result)
      + `<div class="notes-sep"></div>`
      + `<div id="notes-composer" class="notes-composer-placeholder"></div>`;
    renderComposer();
    body.scrollTop = body.scrollHeight;
  }

  function renderComposer() {
    const comp = document.getElementById('notes-composer');
    if (!comp) return;
    comp.innerHTML = `<div class="notes-input-row">`
      + `<input id="notes-input" class="notes-input" type="text"`
      + ` placeholder="Write a note... abbreviation, task, milestone, observation"`
      + ` autocomplete="off" spellcheck="false" />`
      + `<button id="notes-send" class="notes-send-btn" title="Store note">➤</button>`
      + `</div>`;
    const input = comp.querySelector('#notes-input');
    const sendBtn = comp.querySelector('#notes-send');
    const doSend = () => { const v = input.value; input.value = ''; send(v); };
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); doSend(); }
    });
    sendBtn.addEventListener('click', doSend);
    input.focus();
  }

  function openNotes() {
    if (Drawer.root && Drawer.root.classList.contains('is-open') && document.querySelector('.notes-box')) {
      Drawer.close();
      return;
    }
    fetchRecent().then(() => {
      const body = Drawer._paint('📝 Memory Notes',
        `<div class="notes-box">`
        + `<div class="notes-hint">Store knowledge, definitions, tasks, milestones, and observations. The system classifies and stores them automatically.</div>`
        + `<div id="notes-body" class="notes-body">${recentListHTML()}<div id="notes-composer" class="notes-composer-placeholder"></div></div>`
        + `</div>`);
      if (!body) return;
      renderComposer();
      const bodyEl = body.querySelector('#notes-body');
      if (bodyEl) bodyEl.scrollTop = bodyEl.scrollHeight;
    });
  }

  btnNotes.addEventListener('click', openNotes);
})();
