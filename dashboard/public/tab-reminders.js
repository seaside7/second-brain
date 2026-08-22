/* tab-reminders.js — ⏰ Reminders tab + the Today-tab reminder card.
   Data: GET/POST /api/reminders (personal workspace only; never shipped to
   /samudera — see SAMUDERA_HIDDEN_TABS in app.js).
   Sections: Overdue → Today → Upcoming → Done(collapsed). */
window.Reminders = (() => {

  let cache = { all: null, at: 0 };

  /* ── helpers ── */
  const fmtDue = iso => {
    if (!iso) return 'no date';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diffDays = Math.round((day - today) / 86400000);
    const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    const hasTime = !(d.getHours() === 0 && d.getMinutes() === 0);
    const dayName =
      diffDays === 0 ? 'Today' :
      diffDays === 1 ? 'Tomorrow' :
      diffDays === -1 ? 'Yesterday' :
      d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    return hasTime ? `${dayName} · ${time}` : dayName;
  };

  async function api(action, body) {
    const res = await fetch(`/api/reminders/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.details || data.error || `HTTP ${res.status}`);
    return data;
  }

  function invalidate() { cache.at = 0; }

  /* ── row ── */
  function row(r) {
    const doneCls = r.done ? ' is-done' : '';
    const overCls = r.bucket === 'overdue' ? ' is-overdue' : '';
    const checkIcon = r.done ? '↺' : '✓';
    const checkTitle = r.done ? 'Reopen' : 'Mark done';
    return `
      <div class="rem-row${doneCls}${overCls}" data-id="${U.esc(r.id)}">
        <button class="rem-check" data-act="${r.done ? 'reopen' : 'close'}" title="${checkTitle}">${checkIcon}</button>
        <div class="rem-body">
          <div class="rem-text">${U.esc(r.text)}</div>
          <div class="rem-meta"><span class="rem-due">📅 ${fmtDue(r.due)}</span></div>
        </div>
        <button class="rem-del" data-act="delete" title="Delete">✕</button>
      </div>`;
  }

  function section(title, items, open = true) {
    if (!items.length) return '';
    return Comp.card({
      key: `rem-${title.toLowerCase()}`,
      icon: title === 'Overdue' ? '🚨' : title === 'Today' ? '⏰' : title === 'Upcoming' ? '📆' : '✅',
      title,
      count: String(items.length),
      status: title === 'Overdue' ? 'serious' : '',
      open,
      body: items.map(row).join(''),
    });
  }

  function addForm() {
    return `
      <form class="rem-add" id="rem-add-form">
        <input type="text" id="rem-add-text" class="rem-add-input"
               placeholder="Type a reminder… e.g. meeting tomorrow at 3 for savvy"
               autocomplete="off" />
        <button type="submit" class="rem-add-btn">Add</button>
      </form>
      <p class="row-note">Dates are parsed automatically — "tomorrow at 3", "friday siang", "25 aug", "in 2 days".</p>`;
  }

  /* ── tab loader ── */
  async function load() {
    const panel = $id('tab-reminders');
    panel.innerHTML = `<div class="hero-row">${'<div class="skeleton skeleton-block-2"></div>'.repeat(3)}</div>`;
    try {
      const data = await U.fetchJSON('/api/reminders?scope=all');
      cache.all = data.reminders || [];
      cache.at = Date.now();
      const groups = { overdue: [], today: [], upcoming: [], done: [] };
      for (const r of cache.all) (groups[r.bucket] || groups.upcoming).push(r);
      // server returns bucket 'upcoming' for future + undated; keep order
      panel.innerHTML = [
        addForm(),
        section('Overdue', groups.overdue),
        section('Today', groups.today),
        section('Upcoming', groups.upcoming),
        section('Done', groups.done.slice(-20), false),
        (!cache.all.length
          ? Comp.emptyState({ icon: '⏰', title: 'No reminders yet',
                              hint: 'Add one above, or just tell the chat: "remind me … tomorrow at 3".' })
          : ''),
      ].join('\n');
    } catch (err) {
      panel.innerHTML = `<div class="load-error">Could not load reminders: ${U.esc(err.message)}</div>`;
    }
  }

  /* ── Today-tab card (called from app.js with already-fetched scope=today) ── */
  function todayCard(items) {
    const live = items.filter(r => !r.done);
    if (!live.length) return '';
    const rows = live.map(row).join('');
    return Comp.card({
      key: 'today-reminders-card',
      icon: '⏰', title: "Today's reminders",
      count: String(live.length),
      status: live.some(r => r.bucket === 'overdue') ? 'serious' : '',
      open: true,
      body: rows + `<p class="row-note"><a href="#reminders" class="prep-link">Kelola semua →</a></p>`,
    });
  }

  /* ── actions (event delegation, both tabs + today card) ── */
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-act]');
    if (!btn || !btn.closest('.rem-row')) return;
    e.preventDefault();
    const rowEl = btn.closest('.rem-row');
    const id = rowEl.dataset.id;
    const act = btn.dataset.act;
    btn.disabled = true;
    try {
      await api(act, { id });
      invalidate();
      Comp.toast(act === 'delete' ? 'Reminder deleted' :
                 act === 'close' ? 'Reminder done' : 'Reminder reopened', true);
      await load();
      if (App.activeTab === 'today') ensureReminders(true);
    } catch (err) {
      btn.disabled = false;
      Comp.toast(`Failed: ${err.message}`, false);
    }
  });

  document.addEventListener('submit', async e => {
    if (e.target.id !== 'rem-add-form') return;
    e.preventDefault();
    const input = $id('rem-add-text');
    const text = (input.value || '').trim();
    if (!text) return;
    const submitBtn = e.target.querySelector('.rem-add-btn');
    submitBtn.disabled = true;
    try {
      const created = await api('add', { text });
      input.value = '';
      Comp.toast(created.due ? `Added — due ${fmtDue(created.due)}` : 'Reminder added', true);
      invalidate();
      await load();
      ensureReminders(true);
    } catch (err) {
      Comp.toast(`Failed to add: ${err.message}`, false);
    } finally {
      submitBtn.disabled = false;
    }
  });

  window.Tabs = window.Tabs || {};
  window.Tabs.reminders = { load };

  return { load, todayCard };
})();
