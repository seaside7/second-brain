/* ═══════════════════════════════════════════════════════════════════
   tab-inbox.js — 📥 Inbox: SEMUA inquiry masuk (Slack mention/DM, Gmail,
   GDoc comment, Jira) dalam satu antrian follow-up. Data: /api/inbox
   (journal/state/inbox.json, di-refresh cron 30-menit + tombol ↻ Sweep).

   Per item: triage reversible (✓ Beres / 🙈 Abaikan ↔ ↩ Buka lagi, semua
   lewat /api/inbox-action + Undo toast), link ke ticket tracker, dan AI
   copilot — tombol 🤖 Kerjain (atau instruksi bebas) spawn headless
   claude (opus, subscription) via /api/ai-task kind:'inbox'; hasil +
   live log kebaca di drawer lewat shared AI poller components.js.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(() => {
  const state = {
    inbox: null,          // /api/inbox payload
    error: null,
    src: 'all',           // all | slack | gmail | gdoc | jira
    status: 'open',       // open | done | ignored
    sweeping: false,
    openDetailId: null,   // id of the item whose detail drawer is open (for live refresh)
  };
  const SRC_ICON = { slack: '💬', gmail: '📧', gdoc: '📄', jira: '🎫' };
  const byId = new Map();  // id -> item (for the drawer)
  let names = {};          // UID -> display name (from /api/inbox)

  /* <@UID> -> @Name for readable display in the draft textarea */
  const resolveMentions = t => (t || '').replace(/<@([A-Z0-9]+)>/g,
    (_m, id) => '@' + (names[id] || id));
  /* @Name -> <@UID> before sending so mentions still ping (longest name first
     so "@Teammate Dev Singh" wins over a hypothetical "@Teammate") */
  function unresolveMentions(t) {
    let out = t || '';
    Object.entries(names)
      .sort((a, b) => b[1].length - a[1].length)
      .forEach(([id, nm]) => {
        const esc = nm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        out = out.replace(new RegExp('@' + esc, 'g'), `<@${id}>`);
      });
    return out;
  }

  async function load() {
    const panel = document.getElementById('tab-inbox');
    try {
      state.inbox = await U.fetchJSON('/api/inbox');
      names = state.inbox.names || {};
      state.error = null;
    } catch (err) {
      state.error = err.message;
    }
    if (!panel) return;
    render(panel);
  }

  function render(panel) {
    if (state.error) {
      panel.innerHTML = `<div class="load-error">Inbox unavailable: ${U.esc(state.error)}</div>`;
      return;
    }
    const inbox = state.inbox || {};
    const items = inbox.items || [];
    byId.clear();
    items.forEach(it => byId.set(it.id, it));

    const openBySrc = (inbox.counts && inbox.counts.by_source) || {};
    const nOpen = (inbox.counts && inbox.counts.open) || 0;
    const nDone = (inbox.counts && inbox.counts.done) || 0;
    const nIgn = (inbox.counts && inbox.counts.ignored) || 0;

    const srcChip = (key, label) => {
      const n = key === 'all'
        ? nOpen
        : (openBySrc[key] || 0);
      return `<button class="chip ibx-src-chip${state.src === key ? ' is-active' : ''}"
        data-src="${key}">${label}${n ? ` <span class="num">${n}</span>` : ''}</button>`;
    };
    const stChip = (key, label, n) =>
      `<button class="chip ibx-status-chip${state.status === key ? ' is-active' : ''}"
        data-status="${key}">${label}${n ? ` <span class="num">${n}</span>` : ''}</button>`;

    const sweepAge = inbox.last_sweep
      ? U.fmtAge((Date.now() / 1000 - inbox.last_sweep) / 3600) + ' ago'
      : 'belum pernah';
    const srcNotes = Object.entries(inbox.sources || {})
      .filter(([, s]) => !s.ok)
      .map(([n, s]) => `${n}: ${s.note}`).join(' · ');

    const visible = items.filter(it =>
      it.status === state.status && (state.src === 'all' || it.source === state.src));

    /* Open view is triaged: reply-needed (🔥 high dulu) di atas, FYI di bawah —
       the owner langsung lihat mana yang perlu dibales, sisanya nggak berisik */
    let rows;
    if (!visible.length) {
      rows = Comp.emptyState({
        icon: state.status === 'open' ? '🌤' : '🗂',
        title: state.status === 'open' ? 'Antrian bersih' : `Tidak ada item ${state.status}`,
        hint: state.status === 'open' ? 'Tidak ada inquiry menunggu di filter ini.' : '',
      });
    } else if (state.status === 'open') {
      const reply = visible.filter(it => it.triage === 'reply')
        .sort((a, b) => (b.priority_hi ? 1 : 0) - (a.priority_hi ? 1 : 0) || (b.ts || 0) - (a.ts || 0));
      const fyi = visible.filter(it => it.triage !== 'reply');
      rows =
        (reply.length ? `<div class="section-label">${Comp.ic('dot')} Perlu dibales (${reply.length})</div>
           <div class="rows">${reply.map(itemRow).join('')}</div>` : '') +
        (fyi.length ? `<div class="section-label">📎 FYI — nggak nunggu jawaban lo (${fyi.length})</div>
           <div class="rows">${fyi.map(itemRow).join('')}</div>` : '');
    } else {
      rows = `<div class="rows">${visible.map(itemRow).join('')}</div>`;
    }

    panel.innerHTML = `
      <div class="row" data-key="ibx-toolbar">
        <span class="row-icon">${Comp.ic('download')}</span>
        <span class="row-title"><b>Inbox</b> — semua inquiry masuk, satu antrian follow-up</span>
        <span class="row-meta" title="sweep terakhir">sweep ${U.esc(sweepAge)}</span>
        <span class="row-right">
          <button class="prep-link ibx-sweep"${state.sweeping ? ' disabled' : ''}>
            ${state.sweeping ? '⏳ Sweeping…' : '↻ Sweep sekarang'}</button>
        </span>
      </div>
      ${srcNotes ? `<p class="row-note">⚠ ${U.esc(srcNotes)}</p>` : ''}
      <div class="chips">
        ${srcChip('all', 'Semua')}
        ${srcChip('slack', '💬 Slack')}
        ${srcChip('gmail', '📧 Gmail')}
        ${srcChip('gdoc', '📄 GDoc')}
        ${srcChip('jira', '🎫 Jira')}
        <span class="section-label" style="margin:0 4px"></span>
        ${stChip('open', 'Open', nOpen)}
        ${stChip('done', 'Done', nDone)}
        ${stChip('ignored', 'Ignored', nIgn)}
      </div>
      ${rows}`;
  }

  function itemRow(it) {
    const icon = SRC_ICON[it.source] || '·';
    const age = it.ts ? U.fmtAge((Date.now() / 1000 - it.ts) / 3600) : '';
    const who = it.from || it.from_id || it.channel || '?';
    const hi = it.priority_hi ? '🔥 ' : '';
    const ticket = it.linked_ticket ? Comp.ticketChip(it.linked_ticket) : '';
    const runPill = it.last_run ? Comp.aiResultPill({ run: { kind: 'inbox', ref: it.id, ...it.last_run } }) : '';
    const replyChip = it.draft_reply
      ? `<button class="prep-link ibx-open-detail" data-id="${U.esc(it.id)}" title="Draft balasan siap — review di drawer">${Comp.ic('edit')} draft siap</button>` : '';
    const draftChip = (!it.last_run && it.ai_draft)
      ? `<button class="prep-link" data-drawer-path="${U.esc(it.ai_draft)}"
           data-drawer-title="AI draft — ${U.esc(it.id)}">${Comp.ic('note')} draft</button>` : '';
    const actions = it.status === 'open'
      ? `<button class="prep-link ibx-done" data-id="${U.esc(it.id)}" title="Tandai beres">${Comp.ic('check')} Beres</button>
         <button class="prep-link ibx-ignore" data-id="${U.esc(it.id)}" title="Abaikan (bukan buat gw)">🙈</button>`
      : `<button class="prep-link ibx-reopen" data-id="${U.esc(it.id)}" title="Buka lagi (undo)">↩ Buka lagi</button>`;
    return `<div class="row ibx-row${it.status !== 'open' ? ' is-dim' : ''}" data-key="ibx:${U.esc(it.id)}">
      <span class="row-icon" title="${U.esc(it.source)}">${icon}</span>
      <span class="row-title ibx-open-detail" data-id="${U.esc(it.id)}" title="${U.esc(it.title || it.text || '')}">
        ${hi}<b>${U.esc(who)}</b>${it.msg_count > 1 ? ` <span class="num">·${it.msg_count} msg</span>` : ''} · ${U.esc(it.title || (it.text || '').slice(0, 110))}</span>
      <span class="row-badges">${Comp.badge('muted', it.channel || it.source)}</span>
      <span class="row-meta">${U.esc(age)}${age ? ' ago' : ''}</span>
      <span class="row-right">${ticket}${replyChip}${runPill}${draftChip}${actions}
        <button class="prep-link ibx-open-detail" data-id="${U.esc(it.id)}" title="Open detail" aria-label="Open detail">${Comp.ic('search')}</button></span>
    </div>`;
  }

  /* ── drawer: konteks penuh + AI copilot per item ── */
  function openDetail(id) {
    const it = byId.get(id);
    if (!it) return;
    const icon = SRC_ICON[it.source] || '·';
    const links = [];
    if (it.permalink) links.push(it.permalink);
    if (it.ai_draft) links.push({ url: it.ai_draft, label: 'AI draft' });
    const runPill = it.last_run
      ? Comp.aiResultPill({ run: { kind: 'inbox', ref: it.id, ...it.last_run } })
      : '';
    const ticketVal = it.linked_ticket || '';
    const triageLabel = it.triage === 'reply'
      ? (it.priority_hi ? '🔥 perlu dibales (prioritas)' : '🔴 perlu dibales')
      : '📎 FYI';
    /* riwayat percakapan: satu bubble per pesan, kronologis — bukan blob.
       Teks dirender sebagai markdown (numbered list, `code`, [label](url)) +
       bare URL di-linkify dulu, jadi formatnya setara tampilan Slack. */
    const linkify = s => (s || '').replace(/(?<![("\]])(https?:\/\/[^\s<)\]]+)/g, '[$1]($1)');
    const msgs = Array.isArray(it.messages) && it.messages.length
      ? it.messages.map(m => {
          const t = m.ts ? new Date(m.ts * 1000).toLocaleString('en-GB',
            { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Jakarta' }) : '';
          return `<div class="ibx-msg"><span class="ibx-msg-head"><b>${U.esc(m.from || '?')}</b> <span class="ibx-msg-ts">${U.esc(t)} WIB</span></span>
            <div class="ibx-msg-text md">${U.mdToHtml(linkify(m.text || ''))}</div></div>`;
        }).join('')
      : `<div class="md">${U.mdToHtml(linkify(it.text || it.title || '(no content)'))}</div>`;
    const canSend = it.source === 'slack' && it.send_channel && it.status === 'open';
    const srcTag = it.draft_source === 'claude-copilot' ? 'AI copilot (instruksi lo)'
      : it.draft_source === 'claude' ? 'AI (riset konteks)'
      : it.draft_source === 'glm' ? 'GLM (placeholder cepat)' : 'AI';
    const draftBlock = it.draft_reply ? `
      <div class="section-label">${Comp.ic('edit')} Draft balasan — ${U.esc(srcTag)}. Edit dulu kalau perlu; Approve = kirim AS OWNER.</div>
      <textarea class="draft-area ibx-draft-area" rows="5">${U.esc(resolveMentions(it.draft_reply))}</textarea>
      <div class="action-bar">
        ${canSend ? `<button class="prep-link ibx-approve-send" data-id="${U.esc(it.id)}"
            data-channel="${U.esc(it.channel || '')}">✅ Approve & kirim</button>` : ''}
        <button class="prep-link ibx-copy-draft">${Comp.ic('list')} Copy draft</button>
        ${!canSend && it.source === 'gmail' ? `<span class="row-note">gmail: copy lalu balas dari Gmail (send API belum support thread-reply)</span>` : ''}
      </div>` : (it.triage === 'reply'
        ? `<p class="row-note">⏳ Draft belum digenerate — nunggu siklus digest berikutnya, atau pakai ${Comp.ic('bot')} Kerjain di bawah.</p>` : '');
    const sentNote = it.sent_permalink
      ? `<p class="row-note">📨 Terkirim: <a href="${U.esc(it.sent_permalink)}" target="_blank" rel="noopener">lihat di Slack ↗</a></p>` : '';
    const body = `<div class="stack" data-ibx-id="${U.esc(it.id)}">
      <p class="row-note">${icon} ${U.esc(it.source)} · <b>${U.esc(it.from || it.from_id || '?')}</b>
        · ${U.esc(it.channel || '')} · ${it.msg_count > 1 ? `${it.msg_count} pesan · ` : ''}status <b>${U.esc(it.status)}</b> · ${triageLabel}</p>
      <div class="ibx-thread">${msgs}</div>
      ${sentNote}
      ${links.length ? `<p class="row-note">Referensi: ${Comp.linkChips(links)}</p>` : ''}
      ${draftBlock}
      <div class="action-bar">
        <input type="text" class="ibx-ticket-input" placeholder="Link ke ticket (T-123 / MTG-…)"
          value="${U.esc(ticketVal)}" />
        <button class="prep-link ibx-link-save" data-id="${U.esc(it.id)}">💾 Link</button>
        ${it.status === 'open'
          ? `<button class="prep-link ibx-done" data-id="${U.esc(it.id)}">✓ Beres</button>
             <button class="prep-link ibx-ignore" data-id="${U.esc(it.id)}">🙈 Abaikan</button>`
          : `<button class="prep-link ibx-reopen" data-id="${U.esc(it.id)}">↩ Buka lagi</button>`}
      </div>
      <div class="section-label">${Comp.ic('bot')} AI copilot (opus — riset konteks, iket ke ticket, siapin draft; tidak pernah kirim apa pun)</div>
      <textarea class="draft-area ibx-instruction" rows="3"
        placeholder="${it.draft_reply ? 'Revisi: kasih instruksi buat perbaiki draft di atas (mis. \'lebih singkat\', \'tambahin minta timeline\', \'tolak opsi B\'). Hasilnya nimpa draft + siap di-Approve.' : 'Opsional: instruksi spesifik (mis. \'balas setuju tapi minta timeline\', \'putuskan opsi A/B pakai data PRD\'). Kosongkan untuk riset+rekomendasi standar.'}"></textarea>
      <div class="ibx-ai-slot">
        <button class="prep-link ibx-ai-run" data-id="${U.esc(it.id)}">${it.draft_reply ? '♻ Revise draft' : '🤖 Kerjain'}</button>
        ${runPill}
      </div>
    </div>`;
    state.openDetailId = id;
    Drawer.openWide(`${icon} ${it.title || it.id}`, body);
  }

  /* fetch fresh inbox state then re-open a detail drawer — used after an AI copilot
     run finishes so the new draft_reply + Approve & kirim appear in place */
  async function reopenDetail(id) {
    try {
      state.inbox = await U.fetchJSON('/api/inbox');
      names = state.inbox.names || names;
      (state.inbox.items || []).forEach(it => byId.set(it.id, it));
      const panel = document.getElementById('tab-inbox');
      if (panel) render(panel);
      if (byId.has(id)) openDetail(id);
    } catch { /* leave the current drawer as-is on a transient fetch fail */ }
  }

  /* ── actions (reversible; Undo di toast) ── */
  async function inboxAction(id, action, ticket) {
    const payload = { id, action };
    if (action === 'link') payload.ticket = ticket || '';
    await U.fetchJSON('/api/inbox-action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  function undoFor(id, prevAction) {
    /* done/ignore ↔ reopen: setiap triage bisa dibalik dari toast-nya */
    return {
      label: 'Undo',
      onClick: async () => {
        await inboxAction(id, prevAction === 'reopen' ? 'done' : 'reopen');
        Comp.toast(prevAction === 'reopen' ? `Ditutup lagi: ${id}` : `Dibuka lagi: ${id}`, true);
        load();
      },
    };
  }

  async function triage(id, action, label) {
    try {
      await inboxAction(id, action);
      Comp.toast(`${label}: ${id}`, true, undoFor(id, action));
      load();
    } catch (err) {
      Comp.toast(`Gagal: ${err.message}`, false);
    }
  }

  /* ── delegated clicks (narrow matches; drawer content lives outside the
     panel, so listen at document level like app.js does for its buttons) ── */
  document.addEventListener('click', async e => {
    const sweep = e.target.closest('.ibx-sweep');
    if (sweep) {
      e.preventDefault();
      if (state.sweeping) return;
      state.sweeping = true;
      const panel = document.getElementById('tab-inbox');
      if (panel) render(panel);
      try {
        const r = await U.fetchJSON('/api/inbox-sweep', { method: 'POST', timeoutMs: 95000 });
        Comp.toast(r.summary || 'Sweep beres', true);
      } catch (err) {
        Comp.toast(`Sweep gagal: ${err.message}`, false);
      }
      state.sweeping = false;
      load();
      return;
    }

    const chip = e.target.closest('.ibx-src-chip, .ibx-status-chip');
    if (chip) {
      e.preventDefault();
      if (chip.dataset.src) state.src = chip.dataset.src;
      if (chip.dataset.status) state.status = chip.dataset.status;
      const panel = document.getElementById('tab-inbox');
      if (panel) render(panel);
      return;
    }

    const approve = e.target.closest('.ibx-approve-send');
    if (approve) {
      e.preventDefault();
      const id = approve.dataset.id;
      const chan = approve.dataset.channel || '?';
      const ta = approve.closest('[data-ibx-id]')?.querySelector('.ibx-draft-area');
      const readable = ta ? ta.value.trim() : '';
      if (!readable) { Comp.toast('Draft kosong', false); return; }
      /* convert @Name back to <@ID> so mentions ping; confirm shows the readable form */
      const text = unresolveMentions(readable);
      /* double-confirm: klik Approve = persetujuan eksplisit the owner atas draft yang
         TAMPIL — konfirmasi kedua nunjukin target + potongan teks biar nggak salah kirim */
      const ok = window.confirm(`Kirim AS OWNER ke ${chan}?\n\n"${readable.slice(0, 220)}${readable.length > 220 ? '…' : ''}"`);
      if (!ok) return;
      approve.disabled = true;
      approve.textContent = '⏳ Mengirim…';
      try {
        /* one-shot approval token, minted for THIS item right after the confirm and
           consumed by the send. Server-side the send route also demands the browser
           fetch-metadata headers, so an AI worker with a shell on this box can't
           curl localhost and push a message out as the owner. */
        const t = await U.fetchJSON('/api/inbox-send-token', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }),
        });
        const r = await U.fetchJSON('/api/inbox-send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-PSB-Send-Token': t.token },
          body: JSON.stringify({ id, text }), timeoutMs: 60000,
        });
        Comp.toast(`Terkirim ke ${chan} ✓`, true);
        if (r.permalink) window.open(r.permalink, '_blank', 'noopener');
        document.querySelector('.drawer-close')?.click();
        load();
      } catch (err) {
        approve.disabled = false;
        approve.textContent = '✅ Approve & kirim';
        Comp.toast(`Gagal kirim: ${err.message}`, false);
      }
      return;
    }

    const copyDraft = e.target.closest('.ibx-copy-draft');
    if (copyDraft) {
      e.preventDefault();
      const ta = copyDraft.closest('[data-ibx-id]')?.querySelector('.ibx-draft-area');
      try {
        await navigator.clipboard.writeText(ta ? ta.value : '');
        Comp.toast('Draft dicopy — paste di Slack/Gmail', true);
      } catch (err) {
        Comp.toast(`Copy gagal: ${err.message}`, false);
      }
      return;
    }

    const detail = e.target.closest('.ibx-open-detail');
    if (detail) { e.preventDefault(); openDetail(detail.dataset.id); return; }

    const done = e.target.closest('.ibx-done');
    if (done) { e.preventDefault(); done.disabled = true; triage(done.dataset.id, 'done', 'Beres'); return; }

    const ign = e.target.closest('.ibx-ignore');
    if (ign) { e.preventDefault(); ign.disabled = true; triage(ign.dataset.id, 'ignore', 'Diabaikan'); return; }

    const rop = e.target.closest('.ibx-reopen');
    if (rop) { e.preventDefault(); rop.disabled = true; triage(rop.dataset.id, 'reopen', 'Dibuka lagi'); return; }

    const linkSave = e.target.closest('.ibx-link-save');
    if (linkSave) {
      e.preventDefault();
      const wrap = linkSave.closest('[data-ibx-id]');
      const input = wrap && wrap.querySelector('.ibx-ticket-input');
      const id = linkSave.dataset.id;
      try {
        await inboxAction(id, 'link', input ? input.value.trim() : '');
        Comp.toast(input && input.value.trim() ? `Linked: ${id} → ${input.value.trim()}` : `Link dihapus: ${id}`, true);
        load();
      } catch (err) {
        Comp.toast(`Gagal link: ${err.message}`, false);
      }
      return;
    }

    const aiRun = e.target.closest('.ibx-ai-run');
    if (aiRun) {
      e.preventDefault();
      const id = aiRun.dataset.id;
      const wrap = aiRun.closest('[data-ibx-id]');
      const instr = wrap ? (wrap.querySelector('.ibx-instruction') || {}).value : '';
      aiRun.disabled = true;
      aiRun.textContent = '⏳ Spawning…';
      try {
        const r = await U.fetchJSON('/api/ai-task', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'inbox', ref: id, instruction: (instr || '').trim() }),
        });
        /* adopt into the SHARED poller so the pill live-updates + drawer log
           works exactly like every other AI run on the dashboard */
        const slot = wrap && wrap.querySelector('.ibx-ai-slot');
        if (slot) {
          slot.innerHTML = `<span class="row-note">⏳ AI lagi riset + nyiapin draft… drawer refresh sendiri pas selesai.</span>`
            + Comp.aiResultPill({ run: { id: r.id, status: 'running', kind: 'inbox', ref: id } });
        }
        Comp.toast('AI jalan — draft bakal muncul di sini pas beres', true);
      } catch (err) {
        aiRun.disabled = false;
        aiRun.textContent = '🤖 Kerjain';
        Comp.toast(`Gagal spawn AI: ${err.message}`, false);
      }
      return;
    }
  });

  /* selesai run inbox copilot → re-open the SAME detail drawer so the fresh
     draft_reply + Approve & kirim + Revise show up in place (the copilot saved
     its reply into draft_reply via set-draft). Falls back to a list refresh. */
  window.addEventListener('psb:ai-done', e => {
    if (!e.detail || e.detail.kind !== 'inbox') return;
    const drawerOpen = document.getElementById('drawer-root')?.classList.contains('is-open');
    if (state.openDetailId && e.detail.ref === state.openDetailId && drawerOpen) {
      Comp.toast('Draft AI siap — review lalu Approve', true);
      reopenDetail(state.openDetailId);
    } else {
      load();
    }
  });

  /* drawer closed → forget which detail was open (don't auto-reopen later) */
  document.getElementById('drawer-root')?.addEventListener('click', e => {
    if (e.target.closest('.drawer-close, .drawer-overlay')) state.openDetailId = null;
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') state.openDetailId = null;
  });

  Tabs.inbox = { load };
})();
