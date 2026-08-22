/* ═══════════════════════════════════════════════════════════════════
   app.js — boot, hash router, UI state, refresh loop, Drawer wiring,
   and the complete ⭐ Today tab. Other tabs are owned by tab-work.js /
   tab-meetings.js / tab-system.js registering on window.Tabs; if a
   module is missing, its panel renders a calm "under construction"
   EmptyState instead of erroring.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const $id = id => document.getElementById(id);

/* ── Office-safe /samudera mode ──────────────────────────────────────
   The server serves this page (and every /samudera page) with
   window.PSB_MODE='samudera'. Two rules keep it office-safe:
     1. Every fetch carries the X-PSB-Workspace: samudera header, so GET
        endpoints resolve to the Samudera-ONLY data sources and anything
        not Samudera-aware is denied server-side (404 + scope:'samudera').
     2. Personal-harness tabs (Meetings/Hours/System) are hidden.
   The flag is exposed on window so tab modules + chatbox can branch. */
const PSB_SAMUDERA = window.PSB_MODE === 'samudera';
window.PSB_SAMUDERA = PSB_SAMUDERA;

if (PSB_SAMUDERA) {
  const _fetchJSON = U.fetchJSON.bind(U);
  U.fetchJSON = async (url, opts = {}) =>
    _fetchJSON(url, {
      ...opts,
      headers: { ...(opts.headers || {}), 'X-PSB-Workspace': 'samudera' },
    });
}

/* ── UI state: expansion memory (openKeys/closedKeys + sessionStorage) ── */
const UI = {
  openKeys: new Set(),    // keys the user explicitly opened
  closedKeys: new Set(),  // keys the user explicitly closed (beats defaultOpen)

  load() {
    try {
      const raw = JSON.parse(sessionStorage.getItem('psb.open') || '{}');
      UI.openKeys = new Set(raw.o || []);
      UI.closedKeys = new Set(raw.c || []);
    } catch { /* fresh session */ }
  },

  save() {
    try {
      sessionStorage.setItem('psb.open',
        JSON.stringify({ o: [...UI.openKeys], c: [...UI.closedKeys] }));
    } catch { /* storage blocked — expansion just won't persist */ }
  },

  /* Comp._open delegates here: explicit user state wins, else the default */
  isOpen(key, def) {
    if (UI.openKeys.has(key)) return true;
    if (UI.closedKeys.has(key)) return false;
    return !!def;
  },
};
window.UI = UI;

/* record every <details data-key> toggle ('toggle' doesn't bubble → capture) */
document.addEventListener('toggle', e => {
  const el = e.target;
  if (!(el instanceof HTMLDetailsElement) || !el.dataset.key) return;
  if (el.open) { UI.openKeys.add(el.dataset.key); UI.closedKeys.delete(el.dataset.key); }
  else { UI.openKeys.delete(el.dataset.key); UI.closedKeys.add(el.dataset.key); }
  UI.save();
}, true);

/* ── App state ── */
const App = {
  overview: null,
  overviewError: null,
  lastFetched: null,
  progress: null,          // /api/progress — momentum band; null = absent (fetch failed or not yet loaded)
  briefing: null,          // /api/briefing — briefing card; null = absent
  calendar: null,
  calendarState: 'idle',   // idle | loading | ready | error
  calendarError: null,
  calendarAt: 0,
  activeTab: 'today',
  filter: null,
};

/* focus guard: never re-render a container the user is typing in */
function canRender(container) {
  const a = document.activeElement;
  return !(a && container && container.contains(a) && a.matches('input, textarea, select'));
}

/* ── Router: #today (default) | #reminders | #news | #finance | #memory ── */
const TAB_NAMES = ['today', 'reminders', 'news', 'finance', 'memory'];

function parseHash() {
  const h = (location.hash || '#today').replace(/^#/, '');
  const [tab, ...rest] = h.split('/');
  return {
    tab: TAB_NAMES.includes(tab) ? tab : 'today',
    filter: rest.join('/') || null,
  };
}

function applyRoute() {
  const { tab, filter } = parseHash();
  App.activeTab = tab;
  App.filter = filter;
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('is-active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('is-active', p.id === `tab-${tab}`));
  loadActiveTab();
}

function loadActiveTab() {
  if (App.activeTab === 'today') {
    renderHome();
    return;
  }
  const mod = window.Tabs[App.activeTab];
  const panel = $id(`tab-${App.activeTab}`);
  if (mod && typeof mod.load === 'function') {
    try {
      mod.load(App.filter);
    } catch (err) {
      panel.innerHTML = `<div class="load-error">Tab failed to render: ${U.esc(err.message)}</div>`;
    }
  } else {
    panel.innerHTML = Comp.emptyState({
      icon: '🚧',
      title: `${App.activeTab[0].toUpperCase()}${App.activeTab.slice(1)} tab is under construction`,
      hint: 'Its module has not shipped yet — everything else keeps working.',
    });
  }
}

/* ── Refresh loop: briefing + AI runs + active tab, every 60s, paused when
   hidden. The old /api/dashboard (overview) and /api/progress calls are
   gone — the hero tiles, momentum band and work cards they fed no longer
   exist on Home. The ai-task list seeds AI.adoptList so rows built with
   Comp.aiButton show the done pill for a completed run after a reload. */
async function refreshOverview(manual = false) {
  if (document.hidden && !manual) return;
  const btn = $id('btn-refresh');
  btn.classList.add('is-busy');
  const [bRes, aiRes] = await Promise.allSettled([
    U.fetchJSON('/api/briefing'),
    U.fetchJSON('/api/ai-task?list=1'),
  ]);
  if (aiRes.status === 'fulfilled') AI.adoptList(aiRes.value && aiRes.value.runs);
  App.commandQueue = null;
  if (bRes.status === 'fulfilled') {
    App.briefing = bRes.value;
    App.lastFetched = new Date();
  } else if (manual) {
    Comp.toast(`Refresh failed: ${bRes.reason?.message || bRes.reason}`, false);
  }
  btn.classList.remove('is-busy');
  updateChrome();
  /* Background polls must never clobber a tab the user is reading (e.g.
     mid-story in News). Home is designed for live refresh; every other tab
     reloads only on explicit navigation or a manual Refresh click. */
  if (manual || App.activeTab === 'today') loadActiveTab();
}

/* header date + updated-at */
function updateChrome() {
  const now = new Date();
  $id('app-date').textContent = now.toLocaleDateString('en-GB',
    { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  $id('app-updated').textContent = App.lastFetched
    ? `updated ${App.lastFetched.toLocaleTimeString('en-GB')}`
    : 'loading…';
}

/* ═══════════════════════════════════════════
   ⭐ TODAY TAB
   ═══════════════════════════════════════════ */

/* project name → fixed categorical slot (never cycled) */
function projCat(project) {
  const p = (project || '').toLowerCase();
  if (p.includes('marketplace')) return 'cat-1';
  if (p.includes('b2c') || p.includes('superapp') || p.includes('super app')) return 'cat-2';
  if (p.includes('pim')) return 'cat-3';
  if (p.includes('seller')) return 'cat-4';
  if (p.includes('ExampleProgram') || p.includes('exampleco')) return 'cat-5';
  if (p.includes('work id')) return 'cat-6';
  if (p.includes('ecom')) return 'cat-7';
  return 'cat-8';
}
window.projCat = projCat;   // Stage 2 tabs reuse the same project→slot map

function skeletonBlock(lines = 3) {
  let out = '<div class="skeleton">';
  const widths = ['', 'w-80', 'w-60'];
  for (let i = 0; i < lines; i++) out += `<div class="skeleton-line ${widths[i % 3]}"></div>`;
  return out + '</div>';
}

function renderHome() {
  const panel = $id('tab-today');
  if (!canRender(panel)) return;
  panel.innerHTML =
    skeletonBlock(2) + skeletonBlock(3) + skeletonBlock(3);
  U.fetchJSON('/api/home')
    .then(h => {
      const el = $id('tab-today');
      if (!el) return;
      App.home = h;
      if (!canRender(el)) return;
      el.innerHTML = [
        briefingCard(),
        topTicketsCard(h),
        topNewsCard(h),
      ].join('\n');
    })
    .catch(err => {
      const el = $id('tab-today');
      if (el) el.innerHTML = `<div class="load-error">Could not load Home: ${U.esc(err.message)}</div>`;
    });
}

function homeWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) +
    ' · ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

/* Top Tickets — up to 3 items per category:
   ⏰ reminders due today/overdue · 🎯 project deadlines from memory ·
   🔨 what I'm currently working on */
function topTicketsCard(h) {
  const remRow = r => `
    <div class="home-row">
      <span class="home-row-text">${U.esc(r.text)}</span>
      <span class="home-when${r.bucket === 'overdue' ? ' is-late' : ''}">${r.bucket === 'overdue' ? 'overdue' : 'today'} · ${homeWhen(r.due)}</span>
    </div>`;
  const dlRow = d => `
    <div class="home-row">
      <span class="home-row-text">${U.esc(d.text)}</span>
      <span class="home-when">${homeWhen(d.date)}</span>
    </div>`;
  const wkRow = w => `
    <div class="home-row">
      <span class="home-row-text">${U.esc(w.text)}</span>
      ${w.due ? `<span class="home-when">${homeWhen(w.due)}</span>` : ''}
    </div>`;

  const section = (icon, title, items, rowFn, emptyHint, manageHref) => `
    <div class="home-section">
      <div class="home-section-head">
        <span>${icon} ${title}</span>
        ${manageHref && items.length ? `<a class="prep-link" href="${manageHref}">kelola →</a>` : ''}
      </div>
      ${items.length
        ? items.map(rowFn).join('')
        : `<div class="home-empty">${emptyHint}</div>`}
    </div>`;

  return Comp.card({
    key: 'home-top-tickets',
    icon: '🎟️', title: 'Top Tickets',
    count: String((h.reminders.length + h.deadlines.length + h.working_on.length)),
    open: true,
    body:
      section('⏰', 'Reminders', h.reminders, remRow, 'nothing due today', '#reminders') +
      section('🎯', 'Deadlines', h.deadlines, dlRow, 'no dated deadlines in memory') +
      section('🔨', 'Working on', h.working_on, wkRow, "tell the chat what you're working on"),
  });
}

/* Current news — top story per intel category; the full read lives in News */
function topNewsCard(h) {
  const rows = (h.news || []).map(n => `
    <div class="home-row">
      <span class="home-news-cat">${n.icon || '📰'} ${U.esc(n.label)}</span>
      <a class="home-row-text" href="${U.esc(n.url)}" target="_blank" rel="noopener">${U.esc(n.headline)}</a>
      <span class="home-when">${U.esc(n.source || '')}</span>
    </div>`).join('');
  if (!rows) return '';
  return Comp.card({
    key: 'home-top-news',
    icon: '📰', title: 'Current news',
    open: true,
    body: rows + `<p class="row-note"><a href="#news" class="prep-link">Buka News tab untuk full briefing →</a></p>`,
  });
}

function wibHourFromIso(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : (d.getUTCHours() + 7) % 24;
}

/* 🗞️ Briefing card — newest Pagi/Malam section from /api/briefing. Collapsed
   by default, EXCEPT a still-fresh morning briefing before 15:00 WIB (opened
   so the owner sees it without an extra click). "Lihat yang sebelumnya" opens
   `other` (the other kind's most recent section) in the Drawer. Empty/failed
   fetch -> card absent. */
function briefingCard() {
  const b = App.briefing;
  if (!b || !b.latest) return '';
  const kindEmoji = b.latest.kind === 'pagi' ? '🌅' : '🌙';
  const rawTitle = b.latest.title || '';
  /* the raw Dashboard.md header USUALLY already carries its own 🌅/🌙 (kind
     was detected off it) — only prefix ours when it's genuinely missing, so
     the kind indicator never doubles up */
  const count = /[🌅🌙]/.test(rawTitle) ? rawTitle : `${kindEmoji} ${rawTitle}`.trim();
  const hour = wibHourFromIso(App.home?.generated_wib);
  const defaultOpen = b.latest.kind === 'pagi' && hour != null && hour < 15;
  const otherBtn = b.other
    ? `<button class="prep-link briefing-other-btn">Lihat yang sebelumnya</button>` : '';
  const body = `<div class="md">${U.mdToHtml(b.latest.markdown || '')}</div>` +
    (otherBtn ? `<p class="row-note">${otherBtn}</p>` : '');
  return Comp.card({
    key: 'briefing', icon: '🗞️', title: 'Briefing (Pagi/Malam)',
    count, body, open: defaultOpen,
  });
}

/* "Lihat yang sebelumnya" -> open the OTHER briefing section in the Drawer
   (rendered markdown) — not a repo file, so Drawer.openHtml not Drawer.open */
document.addEventListener('click', e => {
  const btn = e.target.closest('.briefing-other-btn');
  if (!btn) return;
  e.preventDefault();
  const other = App.briefing?.other;
  if (!other) return;
  const emoji = other.kind === 'pagi' ? '🌅' : '🌙';
  Drawer.openHtml(`${emoji} ${other.title || 'Briefing'}`, `<div class="md">${U.mdToHtml(other.markdown || '')}</div>`);
});

/* ── boot ── */
/* The /samudera office-safe view hides personal-finance surfaces:
   reminders + finance. Memory stays visible — it is workspace-scoped
   (samudera notes live in the samudera workspace, never personal ones). */
const SAMUDERA_HIDDEN_TABS = ['reminders', 'finance'];

function applySamuderaMode() {
  const hidden = new Set(SAMUDERA_HIDDEN_TABS);
  document.querySelectorAll('.tab-btn').forEach(b => {
    if (hidden.has(b.dataset.tab)) b.style.display = 'none';
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    if (hidden.has(p.id.replace('tab-', ''))) p.style.display = 'none';
  });
  const { tab } = parseHash();
  if (hidden.has(tab)) {
    location.hash = '#today';   // parseHash/applyRoute re-run via hashchange
    applyRoute();
  }
}

function applyCombinedMode() {
  /* combined view ships everything — no tabs to hide */
}

function boot() {
  if (PSB_SAMUDERA) applySamuderaMode();
  else applyCombinedMode();
  UI.load();
  Drawer.init();
  updateChrome();

  $id('tab-nav').addEventListener('click', e => {
    const btn = e.target.closest('.tab-btn');
    if (btn) location.hash = `#${btn.dataset.tab}`;
  });

  $id('btn-refresh').addEventListener('click', () => refreshOverview(true));

  /* ActionBar saves (components.js emits this) → targeted refetch */
  window.addEventListener('psb:ticket-saved', () => refreshOverview(true));

  /* ⏱ Chase (components.js emits this) → refetch: escalations may now
     include the new watchdog item; toast is already handled by the button */
  window.addEventListener('psb:waiting-added', () => refreshOverview(true));

  window.addEventListener('hashchange', applyRoute);
  applyRoute();
  refreshOverview(true);
  setInterval(() => refreshOverview(false), 60000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshOverview(false);
  });
}

boot();
