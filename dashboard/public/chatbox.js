/* ═══════════════════════════════════════════════════════════════════
   chatbox.js — "Ask the Second Brain" — a dedicated full-page tab with a
   ChatGPT-style conversation list.

   Layout: two panes in #tab-chat
     - left sidebar : + New chat + list of all conversations (title, age,
                      workspace chip). Click to open, hover ✕ to delete.
     - right pane   : the message thread + composer (with the "/" suggestion
                      palette, Shift+Enter for newline, Enter to send).

   This is now a GLOBAL assistant (1 memory for all): conversations live in a
   single server-side store (journal/state/chats.json), shared across devices,
   and are NOT partitioned by workspace or URL route. Each conversation still
   keeps a workspace tag that drives its suggestion palette and persona;
   memory recall itself spans every workspace.

   Routing: the header 💬 Ask button sets location.hash = '#chat' (new tab).
   The tab module registers as Tabs.chat.load() so it works with the app's
   hash router + 60s refresh loop.
   ═══════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const btnChat = document.getElementById('btn-chat');
  const ACTIVE_KEY = 'psb_active_convo';
  const PALETTE_TTL_MS = 10 * 60 * 1000;

  const Chat = {
    suggestions: null,     // {workspace, display_name, mode, permanent[], dynamic[]}
    convos: [],            // [{id, title, workspace, updated}]
    activeId: '',          // currently open conversation id ('' = none)
    messages: [],          // current conversation's messages [{role,text}]
    pendingText: '',       // text sent but not yet answered
    paletteOpen: false,
    filtered: [],          // currently rendered suggestion rows
    hl: 0,                 // highlighted row index in `filtered`
    lastFetch: 0,
    mounted: false,
  };

  /* ---------- small helpers ---------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function ageLabel(iso) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return iso;
    const diff = Date.now() - t;
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'now';
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h`;
    const d = Math.floor(h / 24);
    if (d < 30) return `${d}d`;
    const mo = Math.floor(d / 30);
    return `${mo}mo`;
  }
  function wsDisplayName(ws) {
    if (ws === 'samudera') return 'Samudera Indonesia';
    if (ws === 'catalyze') return 'Catalyze';
    if (ws === 'personal') return 'Personal';
    return ws || 'Second Brain';
  }
  function displayMode(mode) { return (mode || '').toUpperCase(); }

  /* ---------- suggestions (per-conversation workspace) ---------- */
  async function ensureSuggestions(wsName, force = false) {
    const fresh = Chat.suggestions &&
      (Date.now() - Chat.lastFetch) < PALETTE_TTL_MS;
    if (!force && fresh) return;
    const ws = (wsName || '').trim();
    const q = ws ? '?workspace=' + encodeURIComponent(ws) : '';
    try {
      const d = await U.fetchJSON('/api/chat-suggestions' + q);
      Chat.suggestions = d;
      Chat.lastFetch = Date.now();
    } catch (err) {
      Chat.suggestions = {
        workspace: ws || 'samudera',
        display_name: wsDisplayName(ws || 'samudera'),
        mode: (ws === 'samudera') ? 'executive' : 'developer',
        permanent: [], dynamic: [], __error: err.message };
    }
  }

  /* ---------- conversation list ---------- */
  async function refreshConvos() {
    try {
      const d = await U.fetchJSON('/api/chat-conversations', { timeoutMs: 20000 });
      Chat.convos = (d && Array.isArray(d.conversations)) ? d.conversations : Chat.convos;
    } catch (e) { /* keep stale list */ }
  }

  async function openConversation(id) {
    if (!id) return;
    try {
      const d = await U.fetchJSON('/api/chat-conversations?id=' + encodeURIComponent(id),
        { timeoutMs: 20000 });
      if (!d || !d.id) return;
      Chat.activeId = d.id;
      Chat.messages = (d.messages || []).map(m =>
        ({ role: m.role === 'user' ? 'user' : 'ai', text: m.text || '' }));
      Chat.pendingText = '';
      localStorage.setItem(ACTIVE_KEY, d.id);
      const c = Chat.convos.find(x => x.id === d.id);
      ensureSuggestions(c && c.workspace, false).finally(renderAll);
      renderAll();
    } catch (e) {
      /* keep current view */
    }
  }

  async function newConversation() {
    let conv = null;
    try {
      conv = await U.fetchJSON('/api/chat-conversations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
        timeoutMs: 20000,
      });
    } catch (e) { conv = null; }
    Chat.activeId = conv && conv.id ? conv.id : '';
    Chat.messages = [];
    Chat.pendingText = '';
    if (conv && conv.id) localStorage.setItem(ACTIVE_KEY, conv.id);
    await refreshConvos();
    await ensureSuggestions(conv && conv.workspace, true);
    renderAll();
    const input = $('#chat-input');
    if (input) input.focus();
  }

  async function deleteConversation(id) {
    try {
      await U.fetchJSON('/api/chat-conversations/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
        timeoutMs: 20000,
      });
    } catch (e) { /* best-effort */ }
    if (Chat.activeId === id) {
      Chat.activeId = '';
      Chat.messages = [];
      localStorage.removeItem(ACTIVE_KEY);
    }
    await refreshConvos();
    if (!Chat.activeId && Chat.convos.length) openConversation(Chat.convos[0].id);
    else renderAll();
  }

  /* ---------- palette ---------- */
  function flatSuggestions() {
    const s = Chat.suggestions || { permanent: [], dynamic: [] };
    const rows = [];
    (s.permanent || []).forEach(cat => (cat.questions || []).forEach(q => {
      rows.push({ text: q, cat: cat.category, icon: cat.icon });
    }));
    (s.dynamic || []).forEach(q => rows.push({ text: q, cat: 'Context-aware', icon: '✨' }));
    return rows;
  }

  function renderPalette(filter) {
    const all = flatSuggestions();
    const f = (filter || '').trim().toLowerCase();
    Chat.filtered = f
      ? all.filter(r => r.text.toLowerCase().includes(f) || r.cat.toLowerCase().includes(f))
      : all;
    Chat.hl = Math.min(Chat.hl, Math.max(0, Chat.filtered.length - 1));

    const pal = $('#chat-palette');
    if (!pal) return;
    const empty = Chat.filtered.length === 0;
    pal.innerHTML = empty
      ? `<div class="chat-pal-empty">No suggestions match "${U.esc(filter || '')}"</div>`
      : Chat.filtered.map((r, i) => `
        <button class="chat-pal-item${i === Chat.hl ? ' is-hl' : ''}" data-i="${i}"
                title="${U.esc(r.cat)}">
          <span class="chat-pal-icon">${U.esc(r.icon)}</span>
          <span class="chat-pal-text">${U.esc(r.text)}</span>
          <span class="chat-pal-cat">${U.esc(r.cat)}</span>
        </button>`).join('');
    pal.classList.toggle('is-open', !empty);
  }

  function openPalette(filter) {
    const pal = $('#chat-palette');
    const input = $('#chat-input');
    Chat.paletteOpen = true;
    if (pal) pal.classList.add('is-open');
    const activeWs = (Chat.convos.find(c => c.id === Chat.activeId) || {}).workspace;
    ensureSuggestions(activeWs).then(() => renderPalette(filter));
    if (input) input.focus();
  }

  function closePalette() {
    const pal = $('#chat-palette');
    Chat.paletteOpen = false;
    if (pal) pal.classList.remove('is-open');
  }

  /* ---------- thread rendering ---------- */
  function threadHTML() {
    const parts = [];
    for (const m of Chat.messages) {
      if (m.role === 'user') {
        parts.push(`<div class="chat-msg chat-msg-user"><div class="chat-bubble">${U.esc(m.text)}</div></div>`);
      } else {
        parts.push(`<div class="chat-msg chat-msg-ai"><div class="chat-bubble chat-bubble-ai">${U.mdToHtml(m.text)}</div></div>`);
      }
    }
    if (!Chat.messages.length && !Chat.pendingText) {
      parts.push(`<div class="chat-hint">Ask the Second Brain anything. It recalls memory from every workspace. Use 💡 or type "/" for suggestions, Shift+Enter for a new line.</div>`);
    }
    if (Chat.pendingText) {
      parts.push(`<div class="chat-msg chat-msg-ai"><div class="chat-bubble chat-bubble-ai chat-typing">…</div></div>`);
    }
    return parts.join('');
  }

  function renderThread() {
    const body = $('#chat-body');
    if (!body) return;
    body.innerHTML = threadHTML();
    body.scrollTop = body.scrollHeight;
  }

  function renderComposer() {
    const wrap = $('#chat-composer');
    if (!wrap) return;
    wrap.innerHTML =
      `<div id="chat-palette" class="chat-palette"></div>
       <div class="chat-input-row">
         <button id="chat-new" class="chat-sugg-btn" title="Start a new conversation">＋</button>
         <button id="chat-sugg" class="chat-sugg-btn" title="Suggested questions">💡</button>
         <textarea id="chat-input" class="chat-input" rows="1"
                placeholder='Ask anything… type "/" for suggestions (Shift+Enter for new line)'
                autocomplete="off" spellcheck="false"></textarea>
         <button id="chat-send" class="chat-send-btn" title="Send">➤</button>
       </div>`;
    wireComposer(wrap);
  }

  function renderSidebar() {
    const list = $('#chat-convos');
    if (!list) return;
    if (!Chat.convos.length) {
      list.innerHTML = `<div class="chat-sidebar-empty">No conversations yet. Start one below.</div>`;
      return;
    }
    list.innerHTML = Chat.convos.map(c => {
      const active = c.id === Chat.activeId ? ' is-active' : '';
      const ws = wsDisplayName(c.workspace);
      return `<div class="chat-convo${active}" data-id="${U.esc(c.id)}">
        <button class="chat-convo-title" title="${U.esc(c.title)}">${U.esc(c.title)}</button>
        <span class="chat-convo-ws">${U.esc(ws)}</span>
        <button class="chat-convo-del" data-del="${U.esc(c.id)}" title="Delete conversation">✕</button>
        <span class="chat-convo-time">${ageLabel(c.updated)}</span>
      </div>`;
    }).join('');
  }

  function renderHeader() {
    const el = $('#chat-ws-line');
    if (!el) return;
    const c = Chat.convos.find(x => x.id === Chat.activeId);
    const ws = c ? wsDisplayName(c.workspace) : '';
    const mode = (Chat.suggestions && Chat.suggestions.mode) || '';
    el.innerHTML = `<b>${U.esc(ws || 'Second Brain')}</b>` +
      (mode ? ` <span class="chat-ws-badge">${U.esc(displayMode(mode))}</span>` : '') +
      (Chat.activeId ? ' · global memory (all workspaces)' : '');
  }

  function renderAll() {
    renderThread();
    renderSidebar();
    renderHeader();
  }

  /* ---------- sending ---------- */
  async function send(text) {
    text = (text || '').trim();
    if (!text || Chat.pendingText) return;
    if (!Chat.activeId) {
      // no conversation yet: create one on the fly
      await newConversation();
    }
    const ws = (Chat.convos.find(c => c.id === Chat.activeId) || {}).workspace || '';
    Chat.messages.push({ role: 'user', text });
    Chat.pendingText = text;
    closePalette();
    renderThread();
    renderSidebar();

    try {
      const res = await U.fetchJSON('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, conversation_id: Chat.activeId, workspace: ws }),
        timeoutMs: 140000,
      });
      Chat.messages.push({ role: 'ai', text: res.reply || '(empty reply)' });
      if (res.conversation_id) {
        Chat.activeId = res.conversation_id;
        localStorage.setItem(ACTIVE_KEY, res.conversation_id);
      }
    } catch (err) {
      Chat.messages.push({ role: 'ai', text: `⚠️ **Could not get an answer:** ${U.esc(err.message)}` });
    }
    Chat.pendingText = '';
    await refreshConvos();
    renderAll();
  }

  function currentFilter(input) {
    const v = input.value || '';
    return v.startsWith('/') ? v.slice(1) : v;
  }

  /* ---------- composer wiring (input row inside a container) ---------- */
  function wireComposer(wrap) {
    const input = $('#chat-input', wrap);
    const pal = $('#chat-palette', wrap);
    const sugg = $('#chat-sugg', wrap);
    const sendBtn = $('#chat-send', wrap);
    const newBtn = $('#chat-new', wrap);
    if (!input) return;

    const autoResize = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; };
    const doSend = () => { const v = input.value; input.value = ''; autoResize(); send(v); };

    input.addEventListener('input', autoResize);
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (Chat.paletteOpen && Chat.filtered[Chat.hl]) {
          const sel = Chat.filtered[Chat.hl].text;
          input.value = '';
          autoResize();
          send(sel);
        } else {
          doSend();
        }
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (Chat.paletteOpen && Chat.filtered.length) {
          e.preventDefault();
          Chat.hl = (Chat.hl + (e.key === 'ArrowDown' ? 1 : -1) + Chat.filtered.length) % Chat.filtered.length;
          renderPalette(currentFilter(input));
        }
        return;
      }
      if (e.key === 'Escape' && Chat.paletteOpen) { closePalette(); return; }
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !Chat.paletteOpen) {
        if (input.value === '') { e.preventDefault(); openPalette(''); }
      }
    });
    input.addEventListener('input', () => {
      if (Chat.paletteOpen) renderPalette(currentFilter(input));
    });
    if (pal) pal.addEventListener('mousedown', e => {
      const item = e.target.closest('.chat-pal-item');
      if (!item) return;
      e.preventDefault();
      const sel = Chat.filtered[Number(item.dataset.i)];
      if (sel) { input.value = ''; send(sel.text); }
    });
    if (sugg) sugg.addEventListener('click', () => {
      if (Chat.paletteOpen) closePalette();
      else openPalette(currentFilter(input));
    });
    if (newBtn) newBtn.addEventListener('click', newConversation);
    if (sendBtn) sendBtn.addEventListener('click', doSend);
  }

  /* ---------- main mount / tab load ---------- */
  function ensureMounted() {
    if (Chat.mounted) return true;
    const panel = document.getElementById('tab-chat');
    if (!panel) return false;
    const wsLine = (Chat.suggestions &&
      Chat.suggestions.display_name) || 'Second Brain';
    const mode = (Chat.suggestions && Chat.suggestions.mode) || '';
    panel.innerHTML = `
      <div class="chat-page">
        <aside class="chat-sidebar">
          <button id="chat-side-new" class="chat-side-new">＋ New chat</button>
          <div class="chat-sidebar-label">Conversations</div>
          <div id="chat-convos" class="chat-convos"></div>
        </aside>
        <section class="chat-pane">
          <div class="chat-ws-line" id="chat-ws-line">${U.esc(wsLine)}</div>
          <div id="chat-body" class="chat-body chat-body-page">${threadHTML()}</div>
          <div class="chat-composer" id="chat-composer"></div>
        </section>
      </div>`;
    renderComposer();
    const newSide = $('#chat-side-new');
    if (newSide) newSide.addEventListener('click', newConversation);
    const convos = $('#chat-convos');
    if (convos) convos.addEventListener('click', e => {
      const del = e.target.closest('.chat-convo-del');
      if (del) { e.stopPropagation(); deleteConversation(del.dataset.del); return; }
      const item = e.target.closest('.chat-convo');
      if (item) openConversation(item.dataset.id);
    });
    Chat.mounted = true;
    return true;
  }

  function load() {
    if (!ensureMounted()) return;
    refreshConvos().then(() => {
      if (Chat.activeId && Chat.convos.some(c => c.id === Chat.activeId)) {
        renderSidebar();
        renderHeader();
        return;
      }
      const stored = localStorage.getItem(ACTIVE_KEY);
      if (stored && Chat.convos.some(c => c.id === stored)) {
        openConversation(stored);
        return;
      }
      if (Chat.convos.length) {
        openConversation(Chat.convos[0].id);
      } else {
        Chat.activeId = '';
        Chat.messages = [];
        renderAll();
      }
    });
  }

  window.Tabs = window.Tabs || {};
  window.Tabs.chat = { load };
  window.PSB_CHAT_LOAD = load;

  /* header 💬 Ask → go to the dedicated Chat tab */
  if (btnChat) {
    btnChat.addEventListener('click', () => {
      if (location.hash === '#chat') {
        location.hash = '#today';
      } else {
        location.hash = '#chat';
      }
    });
  }
})();
