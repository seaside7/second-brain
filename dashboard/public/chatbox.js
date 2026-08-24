/* ═══════════════════════════════════════════════════════════════════
   chatbox.js — "Ask the Second Brain" chat window + suggestion palette.

   Opened from the header 💬 Ask button. The input row has a "/"-triggered
   question palette (also opened via the 💡 button): a categorized list of
   suggested questions — permanent categories (Today/Work/Finance/
   Intelligence/Second Brain) plus 5-10 context-aware dynamic ones built
   server-side per workspace. Clicking a suggestion populates the input and
   sends it; a plain message just sends as typed.

   Workspace-scoped: the active workspace (from /api/chat-suggestions, which
   mirrors workspaces.json) is shown as a small chip; POST /api/chat answers
   in that workspace's persona and the server never mixes workspace data.
   Reuses the shared shell (Drawer) — no new layout machinery.

   Palette rules:
     - "/" alone (or at start of the input) opens the palette with all.
     - "/word" filters by keyword (server suggestions + permanent list).
     - Escape closes the palette; Enter sends the highlighted suggestion.
   ═══════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const btnChat = document.getElementById('btn-chat');
  if (!btnChat) return;

  /* ---- state (kept in module scope so the chat survives tab switches) ---- */
  const CHAT_STORAGE_KEY = 'psb_chat_messages';
  const Chat = {
    suggestions: null,          // {workspace, display_name, mode, permanent[], dynamic[]}
    messages: (() => { try { return JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY)) || []; } catch(e) { return []; } })(),
    pendingText: '',            // text sent but not yet answered
    paletteOpen: false,
    filtered: [],               // currently rendered suggestion rows
    hl: 0,                      // highlighted row index in `filtered`
    lastFetch: 0,
    answered: false,
  };

  function saveChat() {
    try { localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(Chat.messages.slice(-50))); } catch(e) {}
  }

  const PALETTE_TTL_MS = 10 * 60 * 1000;   // suggestions refetched at most every 10 min

  /* ---- suggestion cache helpers ---- */
  async function ensureSuggestions(force = false) {
    const fresh = Chat.suggestions &&
      (Date.now() - Chat.lastFetch) < PALETTE_TTL_MS;
    if (!force && fresh) return;
    const wsQuery = window.PSB_SAMUDERA ? '?workspace=samudera' : '';
    try {
      const d = await U.fetchJSON('/api/chat-suggestions' + wsQuery);
      Chat.suggestions = d;
      Chat.lastFetch = Date.now();
    } catch (err) {
      Chat.suggestions = {
        workspace: window.PSB_SAMUDERA ? 'samudera' : 'catalyze',
        display_name: window.PSB_SAMUDERA ? 'Samudera Indonesia' : 'Catalyze',
        mode: window.PSB_SAMUDERA ? 'executive' : 'developer',
        permanent: [], dynamic: [], __error: err.message };
    }
  }

  /* ---- palette rendering ---- */
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

    const pal = document.getElementById('chat-palette');
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
    const pal = document.getElementById('chat-palette');
    const input = document.getElementById('chat-input');
    Chat.paletteOpen = true;
    pal.classList.add('is-open');
    ensureSuggestions().then(() => renderPalette(filter));
    if (input) input.focus();
  }

  function closePalette() {
    const pal = document.getElementById('chat-palette');
    Chat.paletteOpen = false;
    pal.classList.remove('is-open');
  }

  /* ---- message rendering ---- */
  function chatBodyHTML() {
    const parts = [];
    for (const m of Chat.messages) {
      if (m.role === 'user') {
        parts.push(`<div class="chat-msg chat-msg-user"><div class="chat-bubble">${U.esc(m.text)}</div></div>`);
      } else {
        parts.push(`<div class="chat-msg chat-msg-ai"><div class="chat-bubble chat-bubble-ai">${U.mdToHtml(m.text)}</div></div>`);
      }
    }
    if (Chat.pendingText) {
      parts.push(`<div class="chat-msg chat-msg-ai"><div class="chat-bubble chat-bubble-ai chat-typing">…</div></div>`);
    }
    return parts.join('');
  }

  function renderChat() {
    const body = document.getElementById('chat-body');
    body.innerHTML = chatBodyHTML();
    body.scrollTop = body.scrollHeight;
  }

  /* ---- sending ---- */
  async function send(text) {
    text = (text || '').trim();
    if (!text || Chat.pendingText) return;
    Chat.messages.push({ role: 'user', text });
    Chat.pendingText = text;
    Chat.answered = false;
    closePalette();
    saveChat();
    renderChat();

    try {
      const res = await U.fetchJSON('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, workspace: Chat.suggestions?.workspace }),
        timeoutMs: 140000,
      });
      Chat.messages.push({ role: 'ai', text: res.reply || '(empty reply)' });
    } catch (err) {
      Chat.messages.push({ role: 'ai', text: `⚠️ **Could not get an answer:** ${U.esc(err.message)}` });
    }
    Chat.pendingText = '';
    Chat.answered = true;
    saveChat();
    renderChat();
  }

  /* ---- opening the chat window (shared Drawer shell) ---- */
  function openChat() {
    ensureSuggestions().then(() => {
      const ws = Chat.suggestions?.display_name || '…';
      const mode = Chat.suggestions?.mode || '';
      const badge = mode ? ` <span class="chat-ws-badge">${U.esc(mode)}</span>` : '';
      const body = Drawer._paint('💬 Ask the Second Brain',
        `<div class="chatbox">
           <div class="chat-ws-line">Workspace: <b>${U.esc(ws)}</b>${badge}</div>
           <div id="chat-body" class="chat-body">${chatBodyHTML()}</div>
           <div class="chat-composer">
             <div id="chat-palette" class="chat-palette"></div>
             <div class="chat-input-row">
               <button id="chat-new" class="chat-sugg-btn" title="New conversation - clears history so old wrong claims stop anchoring answers">🧹</button>
               <button id="chat-sugg" class="chat-sugg-btn" title="Suggested questions">💡</button>
               <input id="chat-input" class="chat-input" type="text"
                      placeholder='Ask anything… type "/" for suggestions'
                      autocomplete="off" spellcheck="false" />
               <button id="chat-send" class="chat-send-btn" title="Send">➤</button>
             </div>
           </div>
         </div>`);
      if (!body) return;

      const input = body.querySelector('#chat-input');
      const pal = body.querySelector('#chat-palette');
      const sugg = body.querySelector('#chat-sugg');
      const sendBtn = body.querySelector('#chat-send');

      const doSend = () => { const v = input.value; input.value = ''; send(v); };
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          e.preventDefault();
          if (Chat.paletteOpen && Chat.filtered[Chat.hl]) {
            const sel = Chat.filtered[Chat.hl].text;
            input.value = '';
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
          if (input.value === '') {
            e.preventDefault();
            openPalette('');
          }
        }
      });
      input.addEventListener('input', () => {
        if (Chat.paletteOpen) renderPalette(currentFilter(input));
      });
      pal.addEventListener('mousedown', e => {
        const item = e.target.closest('.chat-pal-item');
        if (!item) return;
        e.preventDefault();   // keep input focus; select below
        const sel = Chat.filtered[Number(item.dataset.i)];
        if (sel) { input.value = ''; send(sel.text); }
      });
      sugg.addEventListener('click', () => {
        if (Chat.paletteOpen) closePalette();
        else openPalette(currentFilter(input));
      });
      const newBtn = body.querySelector('#chat-new');
      newBtn.addEventListener('click', () => {
        Chat.messages = [];
        saveChat();
        renderChat();
        Comp.toast('New conversation started', true);
      });
      sendBtn.addEventListener('click', doSend);
      input.focus();
    });
  }

  function currentFilter(input) {
    const v = input.value || '';
    return v.startsWith('/') ? v.slice(1) : v;
  }

  btnChat.addEventListener('click', () => {
    if (Drawer.root?.classList.contains('is-open') &&
        document.querySelector('.chatbox')) {
      Drawer.close();
      return;
    }
    openChat();
  });
})();
