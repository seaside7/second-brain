/* ═══════════════════════════════════════════════════════════════════
   tab-agents.js — 🤖 Agents: the Samudera executive-agent architecture
   panel. Shows the conceptual map (Orchestrator → specialists → support →
   guardrails), with each node's LIVE status derived server-side from the
   real repo (registered in orchestrator SKILL_REGISTRY + script file exists):
     🟢 ACTIVE   — skill implemented + registered
     🟡 PLANNED  — concept only, no skill (no fake files are ever created)
     🔴 UNAVAILABLE — skill exists but not usable
   Clicking a node opens a detail drawer: purpose, responsibilities,
   capabilities, model/routing, required data & credentials (status/scope
   only — never values), and the REAL prompt file (SKILL.md) read straight
   from .agent/skills/<skill>/. The prompt editor writes that same file back
   to disk — the markdown file stays the single source of truth. Routing
   CODE is never editable from here, only markdown.

   Samudera-primary: the combined dashboard hides this tab (app.js
   SAMUDERA_ONLY_TABS), and every fetch carries the X-PSB-Workspace header.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(() => {
  const state = {
    map: null,           // /api/agents-map payload
    error: null,
    status: 'all',       // all | active | planned | unavailable
    detail: null,        // last /api/agents-skill payload (original content)
  };

  const STATUS_META = {
    active: { badge: 'good', label: '🟢 ACTIVE' },
    planned: { badge: 'warn', label: '🟡 PLANNED' },
    unavailable: { badge: 'serious', label: '🔴 UNAVAILABLE' },
  };

  function nodeStatus(n) {
    return STATUS_META[n.status] || STATUS_META.planned;
  }

  async function load() {
    const panel = document.getElementById('tab-agents');
    try {
      state.map = await U.fetchJSON('/api/agents-map');
      state.error = null;
    } catch (err) {
      state.error = err.message;
    }
    if (panel) render(panel);
  }

  function render(panel) {
    if (state.error) {
      panel.innerHTML = `<div class="load-error">Agents unavailable: ${U.esc(state.error)}</div>`;
      return;
    }
    const nodes = (state.map && state.map.nodes) || [];
    const nActive = nodes.filter(n => n.status === 'active').length;
    const nPlanned = nodes.filter(n => n.status === 'planned').length;
    const nUnavail = nodes.filter(n => n.status === 'unavailable').length;
    const join = state.map && state.map.join_date
      ? ` · join ${state.map.join_date}` : '';
    const visible = state.status === 'all'
      ? nodes : nodes.filter(n => n.status === state.status);

    const chip = (key, label, n) =>
      `<button class="chip agents-status-chip${state.status === key ? ' is-active' : ''}"
        data-status="${key}">${label}${n ? ` <span class="num">${n}</span>` : ''}</button>`;

    panel.innerHTML = `
      <div class="row" data-key="agents-toolbar">
        <span class="row-icon">🤖</span>
        <span class="row-title"><b>Agents & AI Architecture</b> — bagaimana executive AI lo disusun</span>
        <span class="row-meta">${nodes.length} node · ${nActive} aktif · ${nPlanned} planned${join}</span>
      </div>
      <p class="row-note">Klik satu node buat lihat detail: responsibilities, data & kredensial yang
        dibutuhkan, routing model, dan prompt aslinya (SKILL.md). Status dihitung langsung dari repo —
        skill yang beneran diimplementasi dan terdaftar.</p>
      <div class="chips">
        ${chip('all', 'Semua', nodes.length)}
        ${chip('active', 'Aktif', nActive)}
        ${chip('planned', 'Planned', nPlanned)}
        ${chip('unavailable', 'Unavailable', nUnavail)}
      </div>
      <div class="agents-flow">${flowHtml(visible)}</div>`;
  }

  function flowHtml(nodes) {
    if (!nodes.length) {
      return Comp.emptyState({ icon: '🤖', title: 'Tidak ada node', hint: 'Tidak ada agent di kategori ini.' });
    }
    const byLevel = {};
    nodes.forEach(n => { (byLevel[n.level] = byLevel[n.level] || []).push(n); });
    const levels = Object.keys(byLevel).map(Number).sort((a, b) => a - b);
    return levels.map((lv, i) => {
      const label = lv === 0 ? 'intent' : lv === 1 ? 'spesialis eksekutif' : lv === 2 ? 'pendukung' : 'penjaga';
      const link = i < levels.length - 1 ? '<div class="agents-link" aria-hidden="true">↓</div>' : '';
      return `<div class="agents-level">
        <div class="agents-level-label">${label}</div>
        <div class="agents-band">${byLevel[lv].map(nodeCard).join('')}</div>
        ${link}
      </div>`;
    }).join('');
  }

  function nodeCard(n) {
    const m = nodeStatus(n);
    return `<button class="agents-node" data-node="${U.esc(n.id)}" data-status="${n.status}"
      title="${U.esc(n.purpose || '')}">
      <span class="agents-node-emoji">${n.emoji}</span>
      <span class="agents-node-name">${U.esc(n.name)}</span>
      <span class="agents-node-status">${Comp.badge(m.badge, m.label)}</span>
      ${n.skill ? `<span class="agents-skill">${U.esc(n.skill)}</span>` : ''}
      <span class="agents-node-purpose">${U.esc(n.purpose || '')}</span>
      ${n.planned_note ? `<span class="agents-node-note">${U.esc(n.planned_note)}</span>` : ''}
    </button>`;
  }

  /* ── detail drawer ── */

  async function openDetail(nodeId, file) {
    let url = `/api/agents-skill?node=${encodeURIComponent(nodeId)}`;
    if (file) url += `&file=${encodeURIComponent(file)}`;
    const d = await U.fetchJSON(url);
    state.detail = d;
    Drawer.openFull(`${d.node.emoji} ${d.node.name} — ${d.node.id}`, detailHtml(d));
  }

  function section(title, html) {
    return `<div class="section-label">${title}</div>${html}`;
  }

  function detailHtml(d) {
    const n = d.node;
    const m = nodeStatus(n);
    let out = '<div class="stack">';

    out += `<div class="agents-detail-head">
      <span class="agents-node-emoji big">${n.emoji}</span>
      <div>
        <div class="agents-node-name big">${U.esc(n.name)}</div>
        <div class="agents-detail-sub">${U.esc(n.id)}${n.skill ? ` · skill: ${U.esc(n.skill)}` : ''}</div>
      </div>
      <span>${Comp.badge(m.badge, m.label)}</span>
    </div>`;

    out += section('Purpose', `<p class="row-note">${U.esc(n.purpose || '')}</p>`);

    /* prompt source right after purpose - the real SKILL.md, big */
    out += promptSourceHtml(d);

    if (n.planned_note) {
      out += section('Status', `<p class="row-note">${Comp.badge('warn', '🟡 Planned')} ${U.esc(n.planned_note)}</p>`);
    }

    if (Array.isArray(n.responsibilities) && n.responsibilities.length) {
      out += section('Responsibilities',
        `<ul class="agents-list">${n.responsibilities.map(r => `<li>${U.esc(r)}</li>`).join('')}</ul>`);
    }

    if (Array.isArray(n.capabilities) && n.capabilities.length) {
      out += section('Capabilities',
        `<ul class="agents-list">${n.capabilities.map(c => `<li>${U.esc(c)}</li>`).join('')}</ul>`);
    }

    if (n.model_routing) {
      out += section('Model / Routing', `<p class="row-note">${U.esc(n.model_routing)}</p>`);
    }

    const creds = d.credentials || [];
    if (creds.length) {
      out += section('Data & Credentials', creds.map(c => {
        const st = c.status === 'configured_working'
          ? Comp.badge('good', '✅ configured')
          : c.status === 'post_join'
            ? Comp.badge('warn', '⏳ post-join')
            : Comp.badge('muted', c.status || '—');
        const extra = [c.platform, c.required ? 'required' : null, c.read_only ? 'read-only' : null]
          .filter(Boolean).join(' · ');
        return `<div class="row-subtext"><b>${U.esc(c.name)}</b> ${st}
          <span class="row-note">${U.esc(c.min_scope || '')}${extra ? ' · ' + U.esc(extra) : ''}</span></div>`;
      }).join(''));
    } else if (n.status === 'active') {
      out += section('Data & Credentials',
        `<p class="row-note">Tidak ada kredensial khusus terdaftar (memakai model keys root / state file lokal).</p>`);
    }

    /* prompt source + editor (only for nodes backed by a skill) */
    function promptSourceHtml(d) {
      if (d.node.status === 'planned') return '';
      if (!d.files || !d.files.length) {
        return section('Prompt (SKILL.md)',
          `<p class="row-note">⚠️ Skill ${U.esc((d.skill && d.skill.name) || '')} belum punya file
            instruksi markdown — belum ada prompt yang bisa diedit dari panel ini.</p>`);
      }
      const fileChips = d.files.map(f =>
        `<button class="chip agents-file-chip${f.name === d.file ? ' is-active' : ''}"
          data-file="${U.esc(f.name)}">${U.esc(f.name)}</button>`).join('');
      return `<div class="section-label">Prompt source (file asli di repo)</div>
        <div class="chips">${fileChips}</div>
        <div class="agents-prompt-box">
          <div class="row-subtext"><b>${U.esc(d.file)}</b>
            <span class="row-note">${U.esc(d.markdown_path || '')}</span></div>
          <div class="md agents-prompt-md agents-prompt-raw">${U.esc(d.markdown || '')}</div>
          <div class="action-bar">
            <button class="prep-link agents-detail-edit">✏️ Edit prompt</button>
            <span class="row-note">Edit menulis ulang file ini di repo — file tetap source of truth.
              Routing code tidak pernah bisa diedit dari sini.</span>
          </div>
        </div>`;
    }

    out += '</div>';
    return out;
  }

  function editHtml() {
    const d = state.detail;
    if (!d || !d.node) return '';
    return `<div class="stack">
      <div class="row-subtext"><b>${U.esc(d.node.name)}</b> — edit ${U.esc(d.file)}</div>
      <p class="row-note">File: <code>${U.esc(d.markdown_path || '')}</code></p>
      <textarea id="agents-prompt-editor" class="draft-area" rows="24" spellcheck="false">${U.esc(d.markdown || '')}</textarea>
      <div class="action-bar">
        <button class="prep-link agents-detail-save">💾 Simpan</button>
        <button class="prep-link agents-detail-cancel">✋ Batal</button>
        <span class="row-note">Simpan = menulis ulang file di repo (UTF-8). Batal = kembali tanpa mengubah file.</span>
      </div>
    </div>`;
  }

  function cancelEdit() {
    const d = state.detail;
    if (!d || !d.node) return;
    Drawer.openFull(`${d.node.emoji} ${d.node.name} — ${d.node.id}`, detailHtml(d));
  }

  async function saveEdit() {
    const ta = document.getElementById('agents-prompt-editor');
    if (!ta) return;
    const d = state.detail;
    if (!d || !d.node) return;
    const btn = document.querySelector('.agents-detail-save');
    if (btn) { btn.disabled = true; btn.textContent = '⏳…'; }
    try {
      await U.fetchJSON('/api/agents-skill-save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node: d.node.id, file: d.file, content: ta.value }),
      });
      Comp.toast(`Prompt tersimpan: ${d.file}`, true);
      await openDetail(d.node.id, d.file);   // re-fetch so the view shows the saved content
    } catch (err) {
      Comp.toast(`Gagal simpan: ${err.message}`, false);
      if (btn) { btn.disabled = false; btn.textContent = '💾 Simpan'; }
    }
  }

  /* ── delegated clicks ── */
  document.addEventListener('click', e => {
    const chip = e.target.closest('.agents-status-chip');
    if (chip) {
      e.preventDefault();
      state.status = chip.dataset.status;
      const panel = document.getElementById('tab-agents');
      if (panel) render(panel);
      return;
    }
    const node = e.target.closest('.agents-node');
    if (node) {
      e.preventDefault();
      openDetail(node.dataset.node).catch(err =>
        Comp.toast(`Gagal buka detail: ${err.message}`, false));
      return;
    }
    const file = e.target.closest('.agents-file-chip');
    if (file) {
      e.preventDefault();
      const d = state.detail;
      if (d && d.node) {
        openDetail(d.node.id, file.dataset.file).catch(err =>
          Comp.toast(`Gagal buka file: ${err.message}`, false));
      }
      return;
    }
    const edit = e.target.closest('.agents-detail-edit');
    if (edit) {
      e.preventDefault();
      Drawer.openFull('Edit prompt', editHtml());
      return;
    }
    const cancel = e.target.closest('.agents-detail-cancel');
    if (cancel) {
      e.preventDefault();
      cancelEdit();
      return;
    }
    const save = e.target.closest('.agents-detail-save');
    if (save) {
      e.preventDefault();
      saveEdit();
      return;
    }
  });

  Tabs.agents = { load };
})();
