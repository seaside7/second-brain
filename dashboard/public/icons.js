/* ═══════════════════════════════════════════
   ICONS — inline SVG icon set (Heroicons-style, 24px grid, 1.75 stroke)

   WHY THIS EXISTS
   The UI previously used ~466 emoji as its icon system. That is not just
   a taste problem:
     • 🤖 renders as a DIFFERENT robot on Windows/macOS/Android, so the
       app looked different on every device.
     • Emoji ignore font-size, so they don't scale with the type ramp.
     • Screen readers announce "robot face" mid-sentence; the 21 existing
       aria-labels can't cover 466 glyph sites.
   SVG fixes all three and costs no network request (no icon font, no CDN).

   USAGE
     Icons.icon('home')                  → inline <svg> markup
     Icons.icon('home', { cls: 'x' })    → extra class
     Icons.legacy('🏠')                  → emoji mapped to an icon

   Decorative by default: icons are aria-hidden and focusable="false" so
   they stay out of the accessibility tree. The adjacent TEXT carries the
   meaning — which also keeps the validated "icon + label always
   accompany color" mitigation intact for status badges.
   ═══════════════════════════════════════════ */
(function (global) {
  'use strict';

  /* 24×24 viewBox paths. Kept terse on purpose — this file ships to the
     browser on every load, and these are inlined into innerHTML. */
  var P = {
    /* ── navigation / chrome ── */
    home:      '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9.5 21v-6h5v6"/>',
    chat:      '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/>',
    bell:      '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.7 20a2 2 0 0 1-3.4 0"/>',
    news:      '<path d="M4 5h13v14H4z"/><path d="M17 9h3v8a2 2 0 0 1-2 2h-1z"/><path d="M7 8.5h7M7 12h7M7 15.5h4"/>',
    wallet:    '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H19v14H5.5A2.5 2.5 0 0 1 3 16.5Z"/><path d="M3 8h13"/><circle cx="16.5" cy="12.5" r="1.2"/>',
    card:      '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 9.5h19"/><path d="M6 14.5h3.5"/>',
    chart:     '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 16.5v-5M12.5 16.5V8M17 16.5v-8"/>',
    chartDown: '<path d="M4 20V4"/><path d="M4 20h16"/><path d="M8 8.5v5M12.5 11.5v5M17 12.5v8"/>',
    brain:     '<path d="M9.5 4.5A2.5 2.5 0 0 0 7 7a2.5 2.5 0 0 0-2 4 2.5 2.5 0 0 0 1 4.5 2.5 2.5 0 0 0 3.5 2.2V4.5Z"/><path d="M14.5 4.5A2.5 2.5 0 0 1 17 7a2.5 2.5 0 0 1 2 4 2.5 2.5 0 0 1-1 4.5 2.5 2.5 0 0 1-3.5 2.2V4.5Z"/>',
    receipt:   '<path d="M5 3.5h14v17l-2.3-1.6-2.4 1.6-2.3-1.6L9.7 20.5 7.3 18.9 5 20.5Z"/><path d="M9 8.5h6M9 12.5h6"/>',
    bot:       '<rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 4.5V8"/><circle cx="12" cy="3.5" r="1.3"/><path d="M9 13h.01M15 13h.01"/>',
    cog:       '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.2 5.2l2.1 2.1M16.7 16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1"/>',
    search:    '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/>',
    note:      '<path d="M5 3.5h14v17l-2.3-1.6-2.4 1.6-2.3-1.6L9.7 20.5 7.3 18.9 5 20.5Z"/><path d="M9 8.5h6M9 12h4"/>',
    refresh:   '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v4.5h-4.5"/>',
    clock:     '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    calendar:  '<rect x="3.5" y="5" width="17" height="16" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>',

    /* ── status ── */
    check:     '<path d="M4.5 12.5 9.5 17.5 19.5 6.5"/>',
    checkCircle:'<circle cx="12" cy="12" r="8.5"/><path d="M8 12.2l2.8 2.8L16 9.5"/>',
    alert:     '<path d="M12 3.5 21.5 20h-19Z"/><path d="M12 9.5v4.5M12 17h.01"/>',
    hourglass: '<path d="M6.5 3.5h11M6.5 20.5h11"/><path d="M7.5 3.5v3.2c0 2 4.5 3.8 4.5 5.3s-4.5 3.3-4.5 5.3v3.2M16.5 3.5v3.2c0 2-4.5 3.8-4.5 5.3s4.5 3.3 4.5 5.3v3.2"/>',
    ban:       '<circle cx="12" cy="12" r="8.5"/><path d="M6 6l12 12"/>',
    info:      '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
    dot:       '<circle cx="12" cy="12" r="5"/>',

    /* ── actions ── */
    plus:      '<path d="M12 5v14M5 12h14"/>',
    close:     '<path d="M6 6l12 12M18 6 6 18"/>',
    edit:      '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17Z"/><path d="M14.5 7.5 17 10"/>',
    trash:     '<path d="M4 6.5h16M9.5 6.5V4.5h5v2M6.5 6.5 7.5 20h9l1-13.5"/>',
    expand:    '<path d="M9 4.5H4.5V9M15 4.5h4.5V9M9 19.5H4.5V15M15 19.5h4.5V15"/>',
    filter:    '<path d="M3.5 5.5h17l-6.5 7.5v6l-4 2v-8Z"/>',
    download:  '<path d="M12 3.5v12M7.5 11l4.5 4.5 4.5-4.5"/><path d="M4.5 19.5h15"/>',
    copy:      '<rect x="8.5" y="8.5" width="12" height="12" rx="2"/><path d="M15.5 5.5h-9a2 2 0 0 0-2 2v9"/>',
    link:      '<path d="M10 13.5a3.5 3.5 0 0 0 5 0l3-3a3.5 3.5 0 0 0-5-5l-1.2 1.2"/><path d="M14 10.5a3.5 3.5 0 0 0-5 0l-3 3a3.5 3.5 0 0 0 5 5l1.2-1.2"/>',
    send:      '<path d="M21 3.5 10.5 14M21 3.5l-6.8 17-3.7-6.5L4 10.3Z"/>',
    sparkle:   '<path d="M12 3.5 13.9 9 19.5 11 13.9 13 12 18.5 10.1 13 4.5 11 10.1 9Z"/><path d="M18.5 16.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7Z"/>',
    play:      '<path d="M7 4.5 19 12 7 19.5Z"/>',
    folder:    '<path d="M3.5 6.5h6l2 2.5h9v10a2 2 0 0 1-2 2h-15Z"/>',
    file:      '<path d="M6 3.5h8l4.5 4.5V20H6Z"/><path d="M14 3.5V8h4.5"/>',
    terminal:  '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l2.5 2L7 14M12.5 14.5h4"/>',
    map:       '<path d="M9 4.5 3.5 6.8V20L9 17.7l6 2.3 5.5-2.3V4.5L15 6.8Z"/><path d="M9 4.5v13.2M15 6.8v13.2"/>',
    user:      '<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20.5c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5"/>',
    users:     '<circle cx="9" cy="8" r="3.4"/><path d="M2.5 20c0-3.6 2.9-5.8 6.5-5.8s6.5 2.2 6.5 5.8"/><path d="M16 5.2a3.4 3.4 0 0 1 0 6.6M17.5 14.6c2.4.6 4 2.4 4 5.4"/>',
    key:       '<circle cx="8" cy="12" r="4"/><path d="M12 12h9M17.5 12v3.5M20.5 12v2.5"/>',
    pin:       '<path d="M12 21.5s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Z"/><circle cx="12" cy="10.5" r="2.6"/>',
    camera:    '<rect x="2.5" y="6.5" width="14" height="11" rx="2"/><path d="M16.5 11 21.5 8.5v7l-5-2.5Z"/>',
    video:     '<rect x="2.5" y="6" width="13" height="12" rx="2"/><path d="M15.5 10.5 21.5 7.5v9l-6-3Z"/>',
    mail:      '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M3 6.5l9 6 9-6"/>',
    inbox:     '<path d="M3.5 13.5h4l1.5 3h6l1.5-3h4"/><path d="M5.4 5.5h13.2l2.4 8v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Z"/>',
    ticket:    '<path d="M3.5 8.5A2 2 0 0 1 5.5 6.5h13a2 2 0 0 1 2 2V9a2.2 2.2 0 0 0 0 4.4v1.6a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V13a2.2 2.2 0 0 0 0-4.4Z"/><path d="M12 6.5v1.8M12 11v2M12 15.7v1.8"/>',
    fire:      '<path d="M12 3.5s5.5 4.2 5.5 9a5.5 5.5 0 0 1-11 0c0-2 1-3.4 1-3.4s.6 1.5 2 1.5c0-3 2.5-7.1 2.5-7.1Z"/>',
    walletOut: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H19v14H5.5A2.5 2.5 0 0 1 3 16.5Z"/><path d="M15 12.5h5M17.5 10l2.5 2.5-2.5 2.5"/>',
    calc:      '<rect x="4.5" y="3" width="15" height="18" rx="2"/><path d="M8 7h8"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 16h.01M12 16h.01M15.5 16h.01"/>',
    route:     '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H15a3 3 0 0 1 0 6H9a3 3 0 0 0 0 6h6.5"/>',
    hand:      '<path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V11M12 11V4.5a1.5 1.5 0 0 1 3 0V11M15 11V6.5a1.5 1.5 0 0 1 3 0V15a5.5 5.5 0 0 1-5.5 5.5H11a5 5 0 0 1-4.3-2.5L4 13.5a1.6 1.6 0 0 1 2.6-1.8L9 14"/>',
    mic:       '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/>',
    gavel:     '<path d="M13.5 3.5 20 10M17 6.5 10.5 13M3.5 20.5h9M8 12l4 4"/>',
    flag:      '<path d="M5 21V4M5 4h11l-1.5 3.5L16 11H5"/>',
    sun:       '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/>',
    target:    '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    hash:      '<path d="M5 9h14M5 15h14M10 4l-2 16M16 4l-2 16"/>',
    list:      '<path d="M8 6.5h12M8 12h12M8 17.5h12M4 6.5h.01M4 12h.01M4 17.5h.01"/>',
    grid:      '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
    zap:       '<path d="M13.5 3 5 13.5h5.5L10 21l8.5-10.5H13Z"/>',
    layers:    '<path d="M12 3.5 3.5 8 12 12.5 20.5 8Z"/><path d="M3.5 12.5 12 17l8.5-4.5M3.5 16.5 12 21l8.5-4.5"/>',
    handshake: '<path d="m11 17-2.5-2.5-3 1L3 13l3-3 2 2 4-1 3 3-4 3Z"/><path d="M11 8.5 8.5 6 6 7.5M13 8.5 16 5l5 3-3 3.5-2-1.5"/>',
    swap:      '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
    rocket:    '<path d="M12 3c3.5 2.5 5.5 6 5.5 10L12 18l-5.5-5C6.5 9 8.5 5.5 12 3Z"/><circle cx="12" cy="10" r="1.8"/><path d="M9 18c-1 1.5-1 3-1 3s1.5 0 3-1M15 18c1 1.5 1 3 1 3s-1.5 0-3-1"/>',
    signal:    '<path d="M4.5 18.5h.01M9 15.5v3M13.5 11.5v7M18 7.5v11"/><path d="M4.5 9.5a12 12 0 0 1 15 0"/>',
    construction:'<path d="M4 20h16"/><path d="M6 20v-4a2 2 0 0 1 2-2h2v6M14 20v-3.5a2 2 0 0 1 2-2h2V20"/><path d="M9.5 6.5 12 4l2.5 2.5"/>',
    shield:    '<path d="M12 3.5 19.5 6v6c0 4.5-3.2 7.6-7.5 9-4.3-1.4-7.5-4.5-7.5-9V6Z"/><path d="M9 12.2l2.2 2.2L15.5 10"/>',
    briefcase: '<rect x="3" y="7.5" width="18" height="12" rx="2"/><path d="M8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5"/><path d="M3 12.5h18"/>',
    bank:      '<path d="M3.5 9.5 12 4.5l8.5 5"/><path d="M5 9.5V19M9.5 9.5V19M14.5 9.5V19M19 9.5V19"/><path d="M3 19.5h18"/>',
    tree:      '<path d="M12 3v6M12 21v-4"/><path d="M12 6 8 6l4-3 4 3ZM12 11H7l5-3.5L17 11Z"/><path d="M12 17H7.5L12 13l4.5 4Z"/>',
    thread:    '<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="18" cy="18" r="2.5"/>',
    book:      '<path d="M4 4.5h6a3 3 0 0 1 3 3v12a2.5 2.5 0 0 0-2.5-2.5H4Z"/><path d="M20 4.5h-6a3 3 0 0 0-3 3v12a2.5 2.5 0 0 1 2.5-2.5H20Z"/>',
    film:      '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 5v14M16 5v14M3 12h18M3 8.5h5M3 15.5h5M16 8.5h5M16 15.5h5"/>',
    archive:   '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8"/><path d="M10 12h4"/>',
    mic2:      '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M8.5 20.5h7"/>',
    megaphone: '<path d="M4 10v4a1.5 1.5 0 0 0 1.5 1.5H8l7 4.5V5.5L8 10Z"/><path d="M18 9.5a3.5 3.5 0 0 1 0 5"/>',
    inboxes:   '<path d="M3.5 13.5h4l1.5 3h6l1.5-3h4"/><path d="M5.4 5.5h13.2l2.4 8v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Z"/><path d="M8 9.5h8"/>',
    sunrise:   '<path d="M12 3.5v5M5.5 10.5 8 8M18.5 10.5 16 8M2.5 18h19"/><path d="M7 18a5 5 0 0 1 10 0Z"/><path d="M9 21h6"/>',
    moon:      '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>',
    part:      '<path d="M12 3.5 20.5 8v8L12 20.5 3.5 16V8Z"/><path d="M12 12 20.5 8M12 12v8.5M12 12 3.5 8"/>',
    puzzle:    '<path d="M9.5 4.5A2 2 0 0 1 12 6.4 2 2 0 0 1 14.5 4.5H19v4.6a2 2 0 0 0 1.9 2 2 2 0 0 1-1.9 2V19h-4.5a2 2 0 0 0-2-2 2 2 0 0 0-2 2H5v-4.5a2 2 0 0 1 2-2 2 2 0 0 1 0-4H5V4.5Z"/>',
    notepad:   '<path d="M6 3.5h12v17H6Z"/><path d="M9 8h6M9 12h6M9 16h3"/>',
    compass:   '<circle cx="12" cy="12" r="8.5"/><path d="M15 9l-2 5-4 1 2-5Z"/>',
    tag:       '<path d="M3.5 11V4.5H10L20.5 15 15 20.5Z"/><circle cx="7.5" cy="8.5" r="1.4"/>',
    globe:     '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17Z"/>',
    bitcoin:   '<circle cx="12" cy="12" r="8.5"/><path d="M9.5 8.5h4a2 2 0 0 1 0 4h-4h4.5a2 2 0 0 1 0 4h-4.5"/><path d="M11.5 6.5v11M13.5 6.5v11"/>',
    wheat:     '<path d="M12 21V9.5"/><path d="M12 9.5c0-2 1.5-3.5 3.5-3.5 0 2-1.5 3.5-3.5 3.5ZM12 9.5c0-2-1.5-3.5-3.5-3.5 0 2 1.5 3.5 3.5 3.5Z"/><path d="M12 14c0-2 1.5-3.5 3.5-3.5 0 2-1.5 3.5-3.5 3.5ZM12 14c0-2-1.5-3.5-3.5-3.5 0 2 1.5 3.5 3.5 3.5ZM12 18.5c0-2 1.5-3.5 3.5-3.5 0 2-1.5 3.5-3.5 3.5ZM12 18.5c0-2-1.5-3.5-3.5-3.5 0 2 1.5 3.5 3.5 3.5Z"/>',
    flask:     '<path d="M9.5 3.5h5M10.5 3.5v6L5 18.5a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5.5-9v-6"/><path d="M7.5 15h9"/>'
  };

  /* Emoji → icon name. Lets call sites migrate incrementally: an existing
     "🏠" keeps working but renders as crisp SVG. Anything unmapped is
     returned as-is so no glyph ever disappears during the migration. */
  var LEGACY = {
    '🏠': 'home', '💬': 'chat', '⏰': 'clock', '📰': 'news', '💰': 'wallet',
    '💳': 'card', '📈': 'chart', '🧠': 'brain', '🧾': 'receipt', '🤖': 'bot',
    '⚙': 'cog', '⚙️': 'cog', '🔎': 'search', '🔍': 'search', '📝': 'note',
    '↻': 'refresh', '✓': 'check', '✅': 'checkCircle', '⚠': 'alert',
    '⏳': 'hourglass', '⛔': 'ban', '✕': 'close', '❌': 'close', '➕': 'plus',
    '✏️': 'edit', '🗑': 'trash', '⛶': 'expand', '🔗': 'link', '✍': 'edit',
    '🧮': 'calc', '🎫': 'ticket', '📄': 'file', '📋': 'list', '📊': 'chart',
    '🗺': 'map', '🎥': 'video', '📷': 'camera', '🔴': 'dot', '🔥': 'fire',
    '💸': 'walletOut', '📉': 'chartDown', '✋': 'hand', '🔑': 'key',
    '📌': 'pin', '📬': 'inbox', '✉️': 'mail', '👤': 'user', '👥': 'users',
    '☀️': 'sun', '🎯': 'target', '⚡': 'zap', '🗺️': 'map', '📁': 'folder',
    '▶': 'play', '🎛': 'cog', '🛠': 'cog', '🔧': 'cog', '📊': 'chart',
    '➜': 'route', '↗': 'route', '🧭': 'route', '🛤': 'route', '🔔': 'bell',
    '📞': 'mail', '🕐': 'clock', '📅': 'calendar', '🗓': 'calendar',
    '🌐': 'globe', '🧪': 'zap', '🎨': 'sparkle', '✨': 'sparkle', '⭐': 'sparkle',
    '💡': 'info', '🔍‍': 'search', '📤': 'download', '⬇️': 'download',
    '⬆️': 'download', '🔄': 'refresh', '🗃': 'grid', '🧱': 'layers',
    /* second wave — glyphs found in live `icon:` params across tab modules */
    '🤝': 'handshake', '🔀': 'swap', '🚀': 'rocket', '📡': 'signal',
    '🛡': 'shield', '💼': 'briefcase', '🏦': 'bank', '🌳': 'tree',
    '🧵': 'thread', '📚': 'book', '🎬': 'film', '🗂': 'archive',
    '📭': 'inboxes', '🎙': 'mic2', '📟': 'megaphone', '🌅': 'sunrise',
    '🌙': 'moon', '🧩': 'puzzle', '🗒': 'notepad', '🧭': 'compass',
    '🚧': 'construction', '🎟️': 'ticket', '⚖️': 'gavel', '🕒': 'clock',
    '🗓': 'calendar', '⏱': 'clock', '🌤': 'sun', '🏷': 'tag',
    '📥': 'download', '♻': 'refresh', '🖨': 'file', '🗃️': 'archive'
  };

  var VB = 'viewBox="0 0 24 24"';

  function icon(name, opts) {
    opts = opts || {};
    var key = LEGACY[name] || name;
    var d = P[key];
    /* Unknown name: return the literal so a typo is visible, not blank. */
    if (!d) return name;
    var cls = 'ico' + (opts.cls ? ' ' + opts.cls : '');
    /* Fill icons (dot/fire) read better solid; the rest are stroked. */
    var fill = opts.fill || key === 'dot' || key === 'fire';
    /* An explicit size goes inline so it beats the .ico em-based rule;
       without one the icon inherits the surrounding font-size. */
    var dim = opts.size
      ? ' style="width:' + opts.size + 'px;height:' + opts.size + 'px"'
      : '';
    return '<svg class="' + cls + '" ' + VB + dim +
      ' fill="' + (fill ? 'currentColor' : 'none') +
      '" stroke="' + (fill ? 'none' : 'currentColor') + '" stroke-width="' +
      (opts.weight || 1.75) + '" stroke-linecap="round" stroke-linejoin="round"' +
      ' aria-hidden="true" focusable="false">' + d + '</svg>';
  }

  /* Map an emoji (or icon name) to markup. Unknown emoji pass through
     unchanged so nothing is silently dropped mid-migration. */
  function legacy(ch, opts) {
    if (!ch) return '';
    var key = LEGACY[ch];
    return key ? icon(key, opts) : ch;
  }

  /* True when a glyph has an SVG equivalent — lets call sites assert. */
  function has(ch) { return !!(LEGACY[ch] || P[ch]); }

  global.Icons = { icon: icon, legacy: legacy, has: has, paths: P, map: LEGACY };
})(window);
