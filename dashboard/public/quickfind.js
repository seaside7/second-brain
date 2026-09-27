/* quickfind.js — global ticket / ledger quick-find in the header.
   Type an ID (COM-0284, WAIT-0111, DEC-0003) or free text; results across
   the three JSON ledgers (commitments / waiting_on / decisions) render in the
   Drawer. A bare Jira-style key (MBA-237, MSP-…, STOR-…, MPS-…) also offers a
   Work Jira browse deep-link. Read-only: /api/ledger-find never mutates.
   Shortcuts: `/` or Ctrl/Cmd+K focus the box; Enter searches now; Esc blurs. */
(() => {
  'use strict';

  const input = document.getElementById('quickfind-input');
  if (!input) return;

  let timer = null;
  let lastQuery = '';

  /* map a ledger status string to one of Comp.badge's semantic kinds */
  function statusKind(status) {
    const s = (status || '').toLowerCase();
    if (s === 'breached') return 'critical';
    if (s === 'open' || s === 'pending' || s === '') return 'warn';
    if (s === 'answered' || s === 'closed' || s === 'done' || s === 'decided') return 'good';
    return 'muted';
  }

  const KIND_ICON = { commitments: '✅', waiting_on: '⏳', decisions: '⚖' };

  /* a source link: repo-relative path -> Drawer opener; http(s) -> new tab */
  function sourceLink(link) {
    if (!link) return '';
    if (/^https?:\/\//i.test(link)) {
      return `<a class="doc-link" href="${U.esc(link)}" target="_blank" rel="noopener">${Comp.ic('link')} source</a>`;
    }
    return `<button class="prep-link" data-drawer-path="${U.esc(link)}" ` +
      `data-drawer-title="${U.esc(link.split('/').pop())}">${Comp.ic('file')} source</button>`;
  }

  /* one label:value line inside the detail card; skipped when value empty */
  function detailLine(label, value, cls) {
    if (!value) return '';
    return `<div class="qf-d-line${cls ? ' ' + cls : ''}">` +
      `<span class="qf-d-key">${U.esc(label)}</span>` +
      `<span class="qf-d-val">${U.esc(value)}</span></div>`;
  }

  /* the expandable body: context + PIC + timeline + last follow-up + notes + source */
  function detailBody(r) {
    const kindLabel = { commitments: "the owner owes", waiting_on: 'Waiting on', decisions: 'Decision' }[r.kind] || '';
    const created = r.created_wib ? `${r.created_wib} (${r.created_ago})` : '';
    let followup = r.followup_wib ? `${r.followup_wib} (${r.followup_ago})` : 'never nudged';
    if (r.nudge_count) followup += ` · ${r.nudge_count}× nudged`;
    const breached = r.breached_wib ? `${r.breached_wib} (${r.breached_ago})` : '';

    const lines = [
      r.context ? `<div class="qf-d-context">${U.esc(r.context)}</div>` : '',
      detailLine(kindLabel || 'PIC', r.owner || '—'),
      detailLine('Status', r.status || 'open'),
      detailLine('Project', r.project),
      detailLine('Opened', created),
      detailLine('Due', r.due),
      detailLine('Last follow-up', followup),
      detailLine('Breached', breached, 'qf-d-breach'),
    ].filter(Boolean).join('');

    const notes = r.notes && r.notes.length
      ? `<div class="qf-d-notes"><div class="qf-d-key">Notes / timeline</div>` +
        r.notes.map(n => `<div class="qf-d-note">• ${U.esc(n)}</div>`).join('') + `</div>`
      : '';

    const src = r.link ? `<div class="qf-d-src">${sourceLink(r.link)}</div>` : '';

    return `<div class="qf-detail">${lines}${notes}${src}</div>`;
  }

  function resultRow(r) {
    const badges = [Comp.badge(statusKind(r.status), r.status || 'open')];
    if (r.priority) badges.unshift(Comp.badge('p0', 'priority'));
    // compact meta on the summary line: PIC · due · last follow-up
    const metaBits = [
      r.owner,
      r.due ? `due ${r.due}` : '',
      r.followup_wib ? `nudged ${r.followup_ago}` : '',
    ].filter(Boolean).map(U.esc).join(' · ');
    return Comp.listRow({
      key: `qf:${r.id}`,
      icon: KIND_ICON[r.kind] || '🎫',
      title: `${r.id} — ${r.title || '(no title)'}`,
      badges,
      meta: metaBits,
      expandBody: detailBody(r),
    });
  }

  function renderResults(data) {
    const q = data.query || '';
    const results = Array.isArray(data.results) ? data.results : [];
    const parts = [];

    if (data.jira) {
      parts.push(
        `<div class="qf-jira">Jira: ` +
        `<a class="doc-link" href="${U.esc(data.jira.url)}" target="_blank" rel="noopener">` +
        `↗ open ${U.esc(data.jira.key)} in Work Jira</a></div>`
      );
    }

    if (!results.length) {
      parts.push(Comp.emptyState({
        icon: 'search', title: `No ledger item matches "${U.esc(q)}"`,
        hint: data.jira ? 'Not in the local ledgers — try the Jira link above.'
          : 'Try a full ID (COM-0284) or a keyword (ExampleVendor, OTP).',
      }));
    } else {
      parts.push(`<div class="section-label">${results.length} match${results.length === 1 ? '' : 'es'}</div>`);
      parts.push(`<div class="rows">${results.map(resultRow).join('')}</div>`);
    }
    Drawer.openHtml(`${Comp.ic('search')} Find: ${U.esc(q)}`, parts.join(''));
  }

  async function run(q) {
    q = (q || '').trim();
    if (!q) return;
    lastQuery = q;
    try {
      const data = await U.fetchJSON(`/api/ledger-find?q=${encodeURIComponent(q)}`);
      if (q !== lastQuery) return;   // a newer query superseded this one
      renderResults(data);
    } catch (err) {
      Drawer.openHtml(`${Comp.ic('search')} Find: ${U.esc(q)}`,
        `<div class="load-error">${U.esc(err.message || 'search failed')}</div>`);
    }
  }

  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(timer);
    if (q.length < 2) return;
    timer = setTimeout(() => run(q), 220);
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(input.value); }
    else if (e.key === 'Escape') { input.blur(); }
  });

  /* global shortcuts: `/` or Ctrl/Cmd+K focus the box (ignored while already
     typing in a field so it never eats a real keystroke) */
  document.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')
      || document.activeElement?.isContentEditable;
    if ((e.key === '/' && !typing) ||
        ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K'))) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
