/* ═══════════════════════════════════════════════════════════════════
   tab-settings.js — ⚙️ Model routing tab. Owns #tab-settings only.
   Dense rows grouped by capability; click a row to expand its editor.
   Each row shows the effective route (provider → model) with a source
   dot; the editor previews exactly what Save will write. Bulk switch
   (deepseek/openai) and reset-all live in the toolbar. Uses ONLY U +
   Comp + .settings-* classes.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

window.Tabs.settings = (() => {
  const { fetchJSON } = U;

  const state = {
    ws: window.PSB_SAMUDERA ? 'samudera' : 'personal',
    data: null,        // GET /api/models payload
    catalog: null,     // GET /api/models/catalog payload
    err: null,
    capFilter: 'all',  // 'all' | capability id
    openGroups: new Set(),      // capability ids with visible rows
    openEditors: new Set(),     // module ids with visible editor
    bulk: null,        // pending {provider, model, affected:[...]} awaiting confirm
    testing: {},       // module_id -> true while Test runs
    bound: false,
  };

  const SOURCE_DOT = {
    module: 'dot-module', workspace: 'dot-workspace', capability: 'dot-capability',
    system: 'dot-system', env: 'dot-env', none: 'dot-none',
  };

  const CAP_ORDER = ['text_generation', 'embedding', 'image', 'audio_transcription', 'coding'];

  /* ── fetch ── */

  async function getModels(ws) {
    const q = ws ? '?workspace=' + encodeURIComponent(ws) : '';
    return fetchJSON('/api/models' + q);
  }

  async function getCatalog() {
    return fetchJSON('/api/models/catalog');
  }

  /* ── catalog helpers ── */

  function capOptions(cap) {
    const spec = (state.catalog && state.catalog.capabilities &&
                  state.catalog.capabilities[cap]) || {};
    const models = (state.catalog && state.catalog.models) || {};
    const out = [];
    for (const m of (spec.models || [])) {
      const e = models[m];
      if (!e || e.enabled === false) continue;
      out.push({ provider: e.provider, model: m, displayName: e.displayName || m });
    }
    return out;
  }

  function providersFor(cap) {
    const seen = new Set();
    const out = [];
    for (const o of capOptions(cap)) {
      if (!seen.has(o.provider)) { seen.add(o.provider); out.push(o.provider); }
    }
    return out;
  }

  function modelsFor(cap, provider) {
    return capOptions(cap).filter(o => o.provider === provider);
  }

  function capMeta(cap) {
    const c = state.catalog && state.catalog.capabilities && state.catalog.capabilities[cap];
    return {
      label: (c && c.label) || cap,
      desc: (c && c.description) || '',
    };
  }

  /* ── rendering ── */

  function esc(s) { return U.esc(s); }

  function selOptions(options, selected, noneLabel) {
    const parts = noneLabel
      ? [`<option value="">${esc(noneLabel)}</option>`] : [];
    for (const o of options) {
      parts.push(`<option value="${esc(o.model)}" ${o.model === selected ? 'selected' : ''}>${esc(o.displayName)}</option>`);
    }
    return parts.join('');
  }

  function routeHTML(m) {
    const src = SOURCE_DOT[m.source] || 'dot-none';
    const model = m.model || 'no backend';
    return `<span class="settings-route"><span class="settings-route-prov">${esc(m.provider || '—')}</span><span class="settings-route-arrow">→</span><span class="settings-route-model">${esc(model)}</span><span class="settings-source ${src}">${esc(m.source || 'none')}</span></span>`;
  }

  function toolbarHTML() {
    return `
    <div class="settings-toolbar">
      <div class="settings-title">
        <h2>Model routing</h2>
        <p class="settings-sub">What every module runs on, and what it falls back to when a call fails.</p>
      </div>
      <div class="settings-tool-actions">
        <label class="settings-field settings-field--ws"><span>workspace</span>
          <select data-ws>
            ${['personal', 'samudera', 'catalyze', 'shared'].map(w =>
              `<option value="${w}" ${w === state.ws ? 'selected' : ''}>${w}</option>`).join('')}
          </select>
        </label>
        <button class="btn-settings btn-settings--ghost" data-act="reset-all" type="button">Reset all</button>
      </div>
    </div>
    ${bulkHTML()}`;
  }

  function bulkHTML() {
    const opts = capOptions('text_generation');
    const deepseek = opts.filter(o => o.provider === 'deepseek');
    const openai = opts.filter(o => o.provider === 'openai');
    let inner;
    if (state.bulk) {
      const n = state.bulk.affected.length;
      inner = `
        <span class="settings-bulk-info">will switch <strong>${n}</strong> text-generation module${n === 1 ? '' : 's'} to <span class="settings-bulk-target">${esc(state.bulk.provider)}/${esc(state.bulk.model)}</span> — keep?</span>
        <button class="btn-settings btn-settings--primary" data-act="bulk-confirm" type="button">Yes, apply</button>
        <button class="btn-settings btn-settings--ghost" data-act="bulk-cancel" type="button">Not yet</button>`;
    } else {
      inner = `
        <label class="settings-field settings-field--inline"><span>provider</span>
          <select data-bulk-provider>
            <option value="deepseek">deepseek</option>
            <option value="openai">openai</option>
          </select>
        </label>
        <label class="settings-field settings-field--inline"><span>model</span>
          <select data-bulk-model>${selOptions(deepseek, deepseek[0] ? deepseek[0].model : '', '')}</select>
        </label>
        <button class="btn-settings btn-settings--primary" data-act="bulk-review" type="button">Review</button>
        <span class="settings-bulk-info">bulk switch all text-generation modules</span>`;
    }
    return `<div class="settings-bulk">${inner}</div>`;
  }

  function chipsHTML() {
    const rows = state.data.modules || [];
    const counts = { all: rows.length };
    for (const r of rows) counts[r.capability] = (counts[r.capability] || 0) + 1;
    const caps = CAP_ORDER.filter(c => counts[c]);  // order matters
    const parts = caps.map(c => {
      const meta = capMeta(c);
      return `<button class="chip ${state.capFilter === c ? 'is-active' : ''}" data-cap="${esc(c)}" type="button">${esc(meta.label)} · ${counts[c]}</button>`;
    });
    return `<div class="settings-chips">
      <button class="chip ${state.capFilter === 'all' ? 'is-active' : ''}" data-cap="all" type="button">All · ${counts.all}</button>
      ${parts.join('')}
    </div>`;
  }

  function groupHTML(cap, list) {
    const meta = capMeta(cap);
    const open = state.openGroups.has(cap);
    const rows = list.map(rowHTML).join('');
    return `
    <section class="settings-group">
      <button class="settings-group-head" data-group-toggle="${esc(cap)}" type="button">
        <span class="settings-group-name">${esc(meta.label)}</span>
        <span class="settings-group-count">${list.length}</span>
        <span class="settings-group-desc">${esc(meta.desc)}</span>
        <span class="settings-chev">${open ? '▾' : '▸'}</span>
      </button>
      <div class="settings-group-body" ${open ? '' : 'hidden'}>
        ${rows}
      </div>
    </section>`;
  }

  function rowHTML(m) {
    const fb = m.fallback || {};
    const editorOpen = state.openEditors.has(m.module_id);
    return `
    <div class="settings-row" data-row="${esc(m.module_id)}" role="button" tabindex="0" aria-expanded="${editorOpen}">
      <span class="settings-row-name">${esc(m.module)}<code class="settings-pill">${esc(m.module_id)}</code></span>
      ${routeHTML(m)}
      <span class="settings-row-meta">${fb.model ? `fallback ${esc(fb.provider)}/${esc(fb.model)}` : (m.emergency ? 'emergency ai_call/sonnet' : 'no fallback')}</span>
      <span class="settings-row-side">
        <label class="settings-switch" title="${m.enabled ? 'Running — click to pause' : 'Paused — click to run'}" onclick="event.stopPropagation()">
          <input type="checkbox" data-toggle-enabled="${esc(m.module_id)}" ${m.enabled ? 'checked' : ''} />
          <span class="settings-switch-label">${m.enabled ? 'on' : 'off'}</span>
        </label>
        <span class="settings-chev">${editorOpen ? '▾' : '▸'}</span>
      </span>
    </div>
    ${editorHTML(m, editorOpen, fb)}`;
  }

  function editorHTML(m, open, fb) {
    const cap = m.capability;
    const provs = providersFor(cap);
    const testable = cap === 'text_generation';
    const testing = state.testing[m.module_id];
    const fbOptions = provs.filter(p => p !== m.provider).flatMap(p => modelsFor(cap, p));
    return `
    <div class="settings-editor" data-editor="${esc(m.module_id)}" ${open ? '' : 'hidden'}>
      <div class="settings-editor-route" data-preview="${esc(m.module_id)}">
        <span class="settings-editor-route-key">save sets</span>
        <span class="settings-route-prov">${esc(m.provider || '—')}</span><span class="settings-route-arrow">→</span><span class="settings-route-model">${esc(m.model || 'no backend')}</span>
      </div>
      <div class="settings-fields">
        <label class="settings-field"><span>provider</span>
          <select data-set-provider>
            ${provs.map(p => `<option value="${esc(p)}" ${p === m.provider ? 'selected' : ''}>${esc(p)}</option>`).join('')}
          </select>
        </label>
        <label class="settings-field"><span>model</span>
          <select data-set-model>${selOptions(modelsFor(cap, m.provider), m.model, '')}</select>
        </label>
        <label class="settings-field"><span>fallback provider</span>
          <select data-set-fbp>
            <option value="">none</option>
            ${provs.filter(p => p !== m.provider).map(p => `<option value="${esc(p)}" ${p === fb.provider ? 'selected' : ''}>${esc(p)}</option>`).join('')}
          </select>
        </label>
        <label class="settings-field"><span>fallback model</span>
          <select data-set-fbm>${selOptions(fbOptions, fb.model, 'model')}</select>
        </label>
      </div>
      <div class="settings-editor-actions">
        ${testable ? `<button class="btn-settings btn-settings--test" data-act="test" type="button" ${testing ? 'disabled' : ''}>${testing ? 'Testing…' : 'Test'}</button>` : ''}
        <button class="btn-settings btn-settings--primary" data-act="save" type="button">Save</button>
        <button class="btn-settings btn-settings--ghost" data-act="reset" type="button">Reset</button>
      </div>
      <div class="settings-result" data-result></div>
    </div>`;
  }

  function render(panel) {
    if (state.err) {
      panel.innerHTML = `<div class="load-error">Models unavailable: ${esc(state.err)}</div>`;
      return;
    }
    if (!state.catalog || !state.data) {
      panel.innerHTML = `<div class="load-note">Loading…</div>`;
      return;
    }
    const rows = state.data.modules || [];
    const byCap = {};
    for (const r of rows) (byCap[r.capability] = byCap[r.capability] || []).push(r);

    const parts = [toolbarHTML(), chipsHTML()];
    for (const cap of CAP_ORDER) {
      if (!byCap[cap]) continue;
      if (state.capFilter !== 'all' && state.capFilter !== cap) continue;
      parts.push(groupHTML(cap, byCap[cap]));
    }
    panel.innerHTML = `<div class="settings-wrap">${parts.join('')}</div>`;
  }

  /* ── actions ── */

  async function refresh() {
    state.err = null;
    const results = await Promise.allSettled([getModels(state.ws), getCatalog()]);
    if (results[0].status === 'fulfilled') state.data = results[0].value;
    else state.err = results[0].reason && results[0].reason.message;
    if (results[1].status === 'fulfilled') state.catalog = results[1].value;
    else if (!state.err) state.err = (results[1].reason && results[1].reason.message) || 'catalog failed';
    const panel = document.getElementById('tab-settings');
    if (panel) render(panel);
  }

  function moduleRow(mid) { return (state.data.modules || []).find(r => r.module_id === mid); }

  function val(card, attr) {
    const el = card.querySelector(`[${attr}]`);
    return el ? el.value : '';
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    });
    let parsed = null;
    try { parsed = await r.json(); } catch (err) { /* non-JSON */ }
    if (!r.ok && parsed && parsed.error !== undefined) return { error: parsed.error };
    if (!r.ok) return { error: 'HTTP ' + r.status };
    return parsed;
  }

  async function setResult(mid, html) {
    const editor = document.querySelector(`[data-editor="${CSS.escape(mid)}"]`);
    const box = editor && editor.querySelector('[data-result]');
    if (box) box.innerHTML = html;
  }

  async function saveOverride(mid, card) {
    const body = {
      workspace: state.ws,
      module_id: mid,
      provider: val(card, 'data-set-provider'),
      model: val(card, 'data-set-model'),
      enabled: true,
    };
    const fbp = val(card, 'data-set-fbp');
    const fbm = val(card, 'data-set-fbm');
    if (fbp) { body.fallback_provider = fbp; body.fallback_model = fbm; }
    const res = await postJSON('/api/models/set', body);
    if (res.error !== undefined) throw new Error(res.error || 'save failed');
    return res;
  }

  async function runAction(mid, act, btn) {
    try {
      if (act === 'save') {
        const card = btn.closest('.settings-editor');
        const res = await saveOverride(mid, card);
        Comp.toast(`Saved ${mid} → ${res.module && res.module.provider}/${res.module && res.module.model}`, true);
        await refresh();
      } else if (act === 'reset') {
        const res = await postJSON('/api/models/reset', { workspace: state.ws, module_id: mid });
        if (res.error !== undefined) throw new Error(res.error);
        Comp.toast(`Reset ${mid}`, true);
        await refresh();
      } else if (act === 'test') {
        state.testing[mid] = true;
        updateTestButton(mid, true);
        const res = await postJSON('/api/models/test', { workspace: state.ws, module_id: mid });
        if (res.error !== undefined) throw new Error(res.error);
        await setResult(mid, res.ok
          ? `<span class="settings-result-ok">✓ ${esc(res.provider)}/${esc(res.model)} responded - ${esc(res.reply)}</span>`
          : `<span class="settings-result-bad">✗ ${esc(res.provider || '')}/${esc(res.model || '')} - ${esc(res.detail || res.error || 'failed')}</span>`);
        updateTestButton(mid, false);
      }
    } catch (err) {
      updateTestButton(mid, false);
      await setResult(mid, `<span class="settings-result-bad">✗ ${esc(err.message)}</span>`);
    }
  }

  function updateTestButton(mid, busy) {
    const editors = document.querySelectorAll(`[data-editor="${CSS.escape(mid)}"]`);
    for (const ed of editors) {
      const tb = ed.querySelector('[data-act="test"]');
      if (tb) { tb.disabled = busy; tb.textContent = busy ? 'Testing…' : 'Test'; }
    }
  }

  async function toggleEnabled(mid, checked) {
    const m = moduleRow(mid);
    if (!m) return;
    try {
      const body = { workspace: state.ws, module_id: mid, enabled: checked,
                      provider: m.provider, model: m.model };
      const fb = m.fallback;
      if (fb && fb.provider && fb.model) {
        body.fallback_provider = fb.provider;
        body.fallback_model = fb.model;
      }
      const res = await postJSON('/api/models/set', body);
      if (res.error !== undefined) throw new Error(res.error);
      Comp.toast(`${checked ? 'On' : 'Paused'} ${mid}`, true);
      await refresh();
    } catch (err) {
      Comp.toast('Toggle failed: ' + (err.message || String(err)), false);
      await refresh();
    }
  }

  async function bulkReview(provider, model) {
    const res = await postJSON('/api/models/bulk-apply', { workspace: state.ws, provider, model });
    if (res.error !== undefined) throw new Error(res.error);
    state.bulk = { provider, model, affected: res.affected || [] };
    render(document.getElementById('tab-settings'));
  }

  async function bulkApply() {
    const b = state.bulk;
    if (!b) return;
    const res = await postJSON('/api/models/bulk-apply',
      { workspace: state.ws, provider: b.provider, model: b.model, module_ids: b.affected, confirm: true });
    if (res.error !== undefined) throw new Error(res.error);
    state.bulk = null;
    Comp.toast(`Applied ${b.provider}/${b.model} to ${res.applied} modules`, true);
    await refresh();
  }

  async function resetAll() {
    const res = await postJSON('/api/models/reset', { workspace: state.ws });
    if (res.error !== undefined) throw new Error(res.error);
    state.bulk = null;
    Comp.toast(`Reset all model overrides (${state.ws})`, true);
    await refresh();
  }

  /* ── live editor logic ── */

  function syncEditorRoute(card) {
    const mid = card.dataset.editor;
    const m = moduleRow(mid);
    const cap = m ? m.capability : '';
    const prov = val(card, 'data-set-provider');
    const model = val(card, 'data-set-model');
    const dest = card.querySelector('[data-preview]');
    if (dest) {
      dest.querySelector('.settings-route-prov').textContent = prov;
      dest.querySelector('.settings-route-model').textContent = model || '—';
    }
    const fbp = card.querySelector('[data-set-fbp]');
    const fbm = card.querySelector('[data-set-fbm]');
    if (fbp && fbm) {
      const opts = providersFor(cap).filter(p => p !== prov).flatMap(p => modelsFor(cap, p));
      fbm.innerHTML = selOptions(opts, '', 'model');
      if (!fbp.value) fbm.value = '';
    }
  }

  function bindEvents() {
    if (state.bound) return;
    state.bound = true;

    document.addEventListener('change', ev => {
      const wsSel = ev.target.closest('[data-ws]');
      if (wsSel) { state.ws = wsSel.value; state.data = null; state.openGroups = new Set(); state.openEditors = new Set(); refresh(); return; }
      const bulkPro = ev.target.closest('[data-bulk-provider]');
      if (bulkPro) {
        const p = bulkPro.value;
        const sel = document.querySelector('[data-bulk-model]');
        if (sel) sel.innerHTML = selOptions(modelsFor('text_generation', p), '', '');
        return;
      }
      const setProv = ev.target.closest('[data-set-provider]');
      if (setProv) {
        const card = setProv.closest('.settings-editor');
        const mid = card.dataset.editor;
        const m = moduleRow(mid);
        const modelSel = card.querySelector('[data-set-model]');
        if (m && modelSel) modelSel.innerHTML = selOptions(modelsFor(m.capability, setProv.value), '', '');
        syncEditorRoute(card);
        return;
      }
      const setFbp = ev.target.closest('[data-set-fbp]');
      if (setFbp) { syncEditorRoute(setFbp.closest('.settings-editor')); return; }
      const setFbm = ev.target.closest('[data-set-fbm]');
      if (setFbm) { syncEditorRoute(setFbm.closest('.settings-editor')); }
    });

    document.addEventListener('click', async ev => {
      const toggle = ev.target.closest('[data-group-toggle]');
      if (toggle) {
        const cap = toggle.dataset.groupToggle;
        if (state.openGroups.has(cap)) state.openGroups.delete(cap); else state.openGroups.add(cap);
        render(document.getElementById('tab-settings'));
        return;
      }
      const chip = ev.target.closest('[data-cap]');
      if (chip) {
        state.capFilter = chip.dataset.cap;
        if (state.capFilter !== 'all') state.openGroups.add(state.capFilter);
        render(document.getElementById('tab-settings'));
        return;
      }
      const row = ev.target.closest('[data-row]');
      if (row) {
        const mid = row.dataset.row;
        if (state.openEditors.has(mid)) state.openEditors.delete(mid); else state.openEditors.add(mid);
        const rows = document.querySelectorAll(`[data-row="${CSS.escape(mid)}"]`);
        for (const r of rows) r.setAttribute('aria-expanded', state.openEditors.has(mid) ? 'true' : 'false');
        const ed = document.querySelector(`[data-editor="${CSS.escape(mid)}"]`);
        if (ed) ed.hidden = !state.openEditors.has(mid);
        return;
      }
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      ev.preventDefault();
      const act = btn.dataset.act;
      const editor = btn.closest('.settings-editor');
      try {
        if (act === 'bulk-review') {
          const p = document.querySelector('[data-bulk-provider]').value;
          const m = document.querySelector('[data-bulk-model]').value;
          if (!m) { Comp.toast('Choose a model first', false); return; }
          await bulkReview(p, m);
        } else if (act === 'bulk-confirm') { await bulkApply(); }
        else if (act === 'bulk-cancel') { state.bulk = null; render(document.getElementById('tab-settings')); }
        else if (act === 'reset-all') { await resetAll(); }
        else if (editor && editor.dataset.editor) {
          await runAction(editor.dataset.editor, act, btn);
        }
      } catch (err) {
        Comp.toast('Action failed: ' + (err.message || String(err)), false);
      }
    });

    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Enter') return;
      const row = ev.target.closest('[data-row]');
      if (row) {
        ev.preventDefault();
        row.click();
      }
    });
  }

  /* checkbox toggles: intercept so the row doesn't open the editor */
  document.addEventListener('change', ev => {
    const sw = ev.target.closest('[data-toggle-enabled]');
    if (sw) { ev.stopPropagation(); toggleEnabled(sw.dataset.toggleEnabled, sw.checked); }
  }, true);

  async function load() {
    const panel = document.getElementById('tab-settings');
    if (!panel) return;
    bindEvents();
    await refresh();
  }

  return { load };
})();