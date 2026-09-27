/* ═══════════════════════════════════════════════════════════════════
   tab-meetings.js — 🎥 Meetings tab (Stage 2, owns this file only).
   Health strip (live, polled) + Recent meetings (fathom registry, by day)
   + MOMs & notes (Drawer) + Bot activity (collapsed). Only Comp, U,
   Drawer + documented utility CSS classes are used; no dead data sources.
   Wrapped in an IIFE so internal names can't collide with the other
   Stage-2 tab modules sharing this same classic-script global scope.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';
window.Tabs = window.Tabs || {};

(function () {
  const state = {
    meetings: null, meetingsErr: null,
    recorder: null, recorderErr: null,
    health: null, healthErr: null,
    loaded: false,
  };
  let healthTimer = null;

  /* WIB (UTC+7) date string, offset in days — server writes date_wib in this zone,
     so grouping must anchor to WIB regardless of the host machine's own clock zone. */
  function wibDate(offsetDays) {
    const d = new Date(Date.now() + 7 * 3600000 + offsetDays * 86400000);
    const p = n => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  }

  function skeleton(n) {
    let out = '<div class="skeleton">';
    const w = ['', 'w-80', 'w-60'];
    for (let i = 0; i < n; i++) out += `<div class="skeleton-line ${w[i % 3]}"></div>`;
    return out + '</div>';
  }

  function clientBadge(client) {
    if (client === 'Work') return Comp.badge('cat-2', client);
    if (client === 'Secondary') return Comp.badge('cat-5', client);
    return Comp.badge('muted', client || '—');
  }

  /* ═══ Health strip — NOT a card, always visible, own DOM slot so the
     20s poll can refresh it without disturbing expansion state below ═══ */
  function healthStrip() {
    const h = state.health;
    let chips;
    if (!h && state.healthErr) {
      chips = `<div class="load-error">Recorder health unavailable: ${U.esc(state.healthErr)}</div>`;
    } else if (!h) {
      chips = skeleton(1);
    } else {
      const c = h.checks || {};
      const mk = (key, label) => {
        const chk = c[key];
        if (!chk) return Comp.badge('muted', `${label} —`);
        return Comp.badge(chk.ok ? 'good' : 'serious', `${label}: ${chk.detail || (chk.ok ? 'ok' : 'down')}`);
      };
      const local = (state.recorder && state.recorder.local || [])[0];
      let localBadge;
      if (!local) {
        localBadge = Comp.badge('muted', 'Local recorder — no runs yet');
      } else {
        const ageH = (Date.now() - Date.parse(local.ts)) / 3600000;
        localBadge = ageH > 72
          ? Comp.badge('muted', `Local recorder idle ${U.fmtAge(ageH)}`)
          : Comp.badge('good', `Local recorder ${U.fmtAge(ageH)} ago`);
      }
      /* Chip set follows the ACTIVE backend. Hardcoding "Vexa container" here
         survived the meetbot cutover and rendered a chip about a system that
         was no longer recording — never label these statically again. */
      const backendChip = h.backend === 'meetbot'
        ? mk('service', 'meetbot service')
        : h.backend === 'vexa'
          ? mk('container', 'Vexa container')
          : Comp.badge('serious', `Active recorder UNKNOWN: ${U.esc(h.backend_detected_by || 'cannot determine')}`);
      chips = `<div class="chips">${backendChip}${mk('api', 'API')}${mk('whisper', 'Whisper')}${mk('cron', 'Cron')}${localBadge}</div>`;
    }
    /* Always name the backend in the title: a green light must never be
       ambiguous about WHICH recorder it refers to. */
    const be = h && h.backend ? h.backend : '?';
    const overall = h && h.overall && h.overall !== 'ok' ? ` — ${h.overall}` : '';
    return `<div class="escalation-strip" data-key="meetings-health">
      <div class="escalation-title">🩺 Recorder health · ${U.esc(be)}${U.esc(overall)}</div>
      ${chips}
    </div>`;
  }

  function renderHealthSlot() {
    const slot = document.getElementById('meetings-health-slot');
    if (slot) slot.innerHTML = healthStrip();
  }

  /* ═══ Recent meetings — group by WIB day; today + yesterday open,
     everything older folds into one collapsed "Earlier" card ═══ */
  function meetingRowHtml(r) {
    const link = r.url ? `<a class="prep-link" href="${U.esc(r.url)}" target="_blank" rel="noopener">↗ Fathom</a>` : '';
    return `<div class="row" data-key="mtg:${U.esc((r.url || (r.meeting || '') + (r.date || '') + (r.time || '')))}">
      <span class="time-pill">${U.esc(r.time || '—')}</span>
      <span class="row-title" title="${U.esc(r.meeting)}">${U.esc(r.meeting)}</span>
      ${clientBadge(r.client)}
      <span class="row-meta">${U.esc(r.duration || '')}</span>
      <span class="row-right">${link}</span>
    </div>`;
  }

  function dayGroup(rows, emptyHint) {
    return rows.length
      ? `<div class="rows">${rows.map(meetingRowHtml).join('')}</div>`
      : Comp.emptyState({ icon: 'sun', title: emptyHint });
  }

  /* ISO week key (Mon-start, ISO 8601 week numbering) for a 'YYYY-MM-DD' date
     string — meetings-per-week cadence needs week buckets, not calendar days. */
  function isoWeekKey(dateStr) {
    const d = new Date(`${dateStr}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return null;
    const day = (d.getUTCDay() + 6) % 7;              // Mon=0 .. Sun=6
    d.setUTCDate(d.getUTCDate() - day + 3);             // Thursday of this ISO week
    const firstThursday = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
    const fdDay = (firstThursday.getUTCDay() + 6) % 7;
    firstThursday.setUTCDate(firstThursday.getUTCDate() - fdDay + 3);
    const week = 1 + Math.round((d - firstThursday) / (7 * 86400000));
    return `${d.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
  }

  /* Comp.miniBars of meetings/ISO-week, last 8 weeks — "kurang grafik" fix:
     a glanceable cadence read sitting right above the day-by-day list (no
     custom header slot in Comp.card, so this lives at the top of the body:
     closest fit to a "card header region" without touching components.js). */
  function meetingsWeekChart(rows) {
    const counts = new Map();
    rows.forEach(r => {
      const wk = isoWeekKey(r.date);
      if (wk) counts.set(wk, (counts.get(wk) || 0) + 1);
    });
    const weeks = [...counts.keys()].sort().slice(-8);
    /* <2 buckets isn't a cadence read — a single fat bar just looks broken,
       so the strip hides until the registry window spans multiple weeks */
    if (weeks.length < 2) return '';
    const points = weeks.map(wk => ({ label: wk.slice(5), value: counts.get(wk) || 0 }));
    return `<div class="meetings-week-chart">
      <span class="row-meta">Meetings / week</span>
      ${Comp.miniBars(points, { w: 168, h: 32, label: 'meetings per ISO week', kind: 'cat-2' })}
    </div>`;
  }

  function meetingsSection(rows) {
    const today = wibDate(0), yest = wibDate(-1);
    const grp = { today: [], yesterday: [], earlier: [] };
    rows.forEach(r => (r.date === today ? grp.today : r.date === yest ? grp.yesterday : grp.earlier).push(r));
    const earlierCard = grp.earlier.length
      ? Comp.card({
          key: 'meetings-earlier', icon: 'archive', title: 'Earlier', count: `${grp.earlier.length}`,
          open: false, body: dayGroup(grp.earlier),
        })
      : '';
    const body = `
      ${meetingsWeekChart(rows)}
      <div class="section-label">Today</div>${dayGroup(grp.today, 'No meetings today')}
      <div class="section-label">Yesterday</div>${dayGroup(grp.yesterday, 'No meetings yesterday')}
      ${earlierCard}`;
    return Comp.card({ key: 'meetings', icon: 'film', title: 'Recent meetings', count: `${rows.length}`, open: true, body });
  }

  /* ═══ MOMs & notes — click opens Drawer via declarative data-drawer-path ═══ */
  function momRowHtml(m) {
    const ageH = (Date.now() - Date.parse(m.mtime)) / 3600000;
    const versionsBadge = m.versions > 1 ? Comp.badge('muted', `${m.versions} versi`) : '';
    return `<div class="row" data-key="mom:${U.esc(m.relPath)}" data-drawer-path="${U.esc(m.relPath)}" data-drawer-title="${U.esc(m.title)}">
      <span class="row-icon">${Comp.ic('note')}</span>
      <span class="row-title" title="${U.esc(m.title)}">${U.esc(m.title)}</span>
      ${clientBadge(m.client)}${versionsBadge}
      <span class="row-meta">${U.esc(U.fmtAge(ageH))}</span>
    </div>`;
  }

  function momsSection(moms) {
    const body = moms.length
      ? `<div class="rows">${moms.map(momRowHtml).join('')}</div>`
      : Comp.emptyState({ icon: 'inboxes', title: 'No MOMs or notes yet' });
    return Comp.card({ key: 'moms', icon: 'note', title: 'MOMs & notes', count: `${moms.length}`, open: false, body });
  }

  /* ═══ Bot activity — Vexa sends + local recorder runs, collapsed by default ═══ */
  function botStatusBadge(status, okFlag) {
    const s = String(status || '');
    if (/^skipped/i.test(s)) return Comp.badge('warn', 'skipped (not admitted)');
    const ok = okFlag !== undefined ? okFlag : !/fail|error/i.test(s);
    const label = s ? (s.length > 40 ? `${s.slice(0, 40)}…` : s) : (ok ? 'ok' : 'failed');
    return Comp.badge(ok ? 'good' : 'serious', label);
  }

  function vexaRowHtml(v) {
    return `<div class="row" data-key="vexa:${U.esc(v.key)}">
      <span class="row-icon">${Comp.ic('bot')}</span>
      <span class="row-title" title="${U.esc(v.title)}">${U.esc(v.title)}</span>
      ${Comp.badge('muted', v.platform || '—')}
      <span class="row-meta">${U.esc(U.fmtAge((Date.now() - Date.parse(v.sent_at)) / 3600000))}</span>
      <span class="row-right">${botStatusBadge(v.status, v.ok)}</span>
    </div>`;
  }

  function localRowHtml(l) {
    return `<div class="row" data-key="local:${U.esc(l.rec_id || l.file)}">
      <span class="row-icon">${Comp.ic('mic2')}</span>
      <span class="row-title" title="${U.esc(l.file)}">${U.esc(l.file)}</span>
      <span class="row-meta">${U.esc(U.fmtAge((Date.now() - Date.parse(l.ts)) / 3600000))}</span>
      <span class="row-right">${botStatusBadge(l.status)}</span>
    </div>`;
  }

  function botsSection(recorder) {
    const vexa = recorder.vexa || [], local = recorder.local || [];
    const body = `
      <div class="section-label">Vexa bot sends (${vexa.length})</div>
      ${vexa.length ? `<div class="rows">${vexa.slice(0, 20).map(vexaRowHtml).join('')}</div>` : Comp.emptyState({ icon: 'bot', title: 'No bot sends recorded' })}
      <div class="section-label">Local recorder runs (${local.length})</div>
      ${local.length ? `<div class="rows">${local.slice(0, 20).map(localRowHtml).join('')}</div>` : Comp.emptyState({ icon: 'mic2', title: 'No local runs recorded' })}`;
    return Comp.card({ key: 'bots', icon: 'bot', title: 'Bot activity', count: `${vexa.length + local.length}`, open: false, body });
  }

  /* ═══ Full render + data fetch ═══ */
  function render() {
    const panel = document.getElementById('tab-meetings');
    if (!panel) return;
    const parts = [`<div id="meetings-health-slot">${healthStrip()}</div>`];
    if (state.meetingsErr) parts.push(`<div class="load-error">Meetings unavailable: ${U.esc(state.meetingsErr)}</div>`);
    else if (state.meetings) parts.push(meetingsSection(state.meetings));
    else parts.push(skeleton(4));
    if (state.recorderErr) {
      parts.push(`<div class="load-error">Recorder data unavailable: ${U.esc(state.recorderErr)}</div>`);
    } else if (state.recorder) {
      parts.push(momsSection(state.recorder.moms || []));
      parts.push(botsSection(state.recorder));
    } else {
      parts.push(skeleton(4));
    }
    panel.innerHTML = parts.join('\n');
  }

  function fetchAll() {
    Promise.allSettled([
      U.fetchJSON('/api/meetings'),
      U.fetchJSON('/api/recorder'),
      U.fetchJSON('/api/vexa-health'),
    ]).then(([m, r, h]) => {
      state.meetings = m.status === 'fulfilled' ? (m.value.meetings || []) : null;
      state.meetingsErr = m.status === 'rejected' ? m.reason.message : null;
      state.recorder = r.status === 'fulfilled' ? r.value : null;
      state.recorderErr = r.status === 'rejected' ? r.reason.message : null;
      if (h.status === 'fulfilled') { state.health = h.value; state.healthErr = null; }
      else state.healthErr = h.reason.message;
      state.loaded = true;
      render();
    });
  }

  /* ═══ Vexa-health poll lifecycle: every 20s, ONLY while #meetings is the
     active tab AND the document is visible. startHealthPoll() always clears
     any existing timer first, so calling it repeatedly (load() fires on
     every tab switch + every 60s app-level refresh) can never stack
     intervals. A page-level hashchange/visibilitychange listener (attached
     once, at module-init time — never inside load()) is what actually
     stops the poll when the user leaves this tab or backgrounds the page. ═══ */
  function activeTabName() {
    return ((location.hash || '#today').replace(/^#/, '').split('/')[0]) || 'today';
  }

  function refreshHealth() {
    U.fetchJSON('/api/vexa-health')
      .then(h => { state.health = h; state.healthErr = null; renderHealthSlot(); })
      .catch(err => { state.healthErr = err.message; renderHealthSlot(); });
  }

  function stopHealthPoll() {
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  }

  function startHealthPoll() {
    stopHealthPoll();   // clear-before-set: guarantees no leaked/duplicate intervals
    healthTimer = setInterval(() => {
      if (activeTabName() !== 'meetings' || document.hidden) { stopHealthPoll(); return; }
      refreshHealth();
    }, 20000);
  }

  window.addEventListener('hashchange', () => { if (activeTabName() !== 'meetings') stopHealthPoll(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopHealthPoll();
    else if (activeTabName() === 'meetings') startHealthPoll();
  });

  window.Tabs.meetings = {
    load() {
      const panel = document.getElementById('tab-meetings');
      if (panel && !state.loaded) {
        panel.innerHTML = `<div id="meetings-health-slot">${healthStrip()}</div>${skeleton(6)}`;
      }
      fetchAll();
      startHealthPoll();
    },
  };
})();
