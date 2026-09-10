/* ═══════════════════════════════════════════════════════════════════
   tab-settings.js — ⚙️ AI Models tab. Owns #tab-settings only.
   Per-module model override editor backed by /api/models* (model_router).
   Effective provider/model + source badge per module, primary + fallback
   selects limited to catalog models compatible with the module's
   capability, enabled toggle, Test (live call, text_generation only),
   Reset, and a bulk-apply bar (deepseek/openai, confirm-then-apply) plus
   reset-all. Uses ONLY U + Comp + .settings-* classes.
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
    busy: false,
    bulk: null,        // pending {provider, model, affected:[...]} awaiting confirm
    testing: {},       // module_id -> true while Test runs
    bound: false,
  };

  const SRC_BADGE = {
    module: 'good', workspace: 'cat-3', capability: 'cat-1',
    system: 'muted', env: 'cat-6', none: 'muted',
  };

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
    // [{provider, model, displayName}] for one capability (catalog-backed).
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

  function capLabel(cap) {
    const c = state.catalog && state.catalog.capabilities && state.catalog.capabilities[cap];
    return (c && c.label) || cap;
  }

  /* ── rendering ── */

  function esc(s) { return U.esc(s); }

  function sourceBadge(src) {
    const cls = SRC_BADGE[src] || 'muted';
    return `<span class="badge badge--${cls}">${esc(src || 'n/a')}</span>`;
  }

  function selOptions(options, selected, noneLabel) {
    const parts = noneLabel
      ? [`<option value="">${esc(noneLabel)}</option>`] : [];
    for (const o of options) {
      parts.push(`<option value="${esc(o.model)}" ${o.model === selected ? 'selected' : ''}>${esc(o.displayName)}</option>`);
    }
    return parts.join('');
  }

  function moduleCard(m) {
    const cap = m.capability;
    const provs = providersFor(cap);
    const fb = m.fallback || {};
    const eff = (m.model ? `${m.provider}/${m.model}` : 'none available');
    const testable = cap === 'text_generation';
    const testing = state.testing[m.module_id];

    return `
    <div class="settings-card" data-module="${esc(m.module_id)}">
      <div class="settings-card-head">
        <div class="settings-card-title">
          <strong>${esc(m.module)}</strong>
          <code class="settings-pill">${esc(m.module_id)}</code>
        </div>
        <div class="settings-card-meta">
          <span class="badge badge--muted settings-eff">↳ ${esc(eff)}</span>
          ${sourceBadge(m.source)}
          ${m.enabled ? '' : '<span class="badge badge--serious">disabled</span>'}
          ${m.compatible ? '' : '<span class="badge badge--warn">incompatible model</span>'}
        </div>
      </div>
      <p class="settings-purpose">${esc(m.purpose || '')} — ${esc(m.capability_description || cap)}</p>
      <div class="settings-controls">
        <label class="settings-c">
          <input type="checkbox" data-set-enabled="1" ${m.enabled ? 'checked' : ''} />
          enabled
        </label>
        <label class="settings-c">primary
          <select data-set-provider>
            ${provs.map(p => `<option value="${esc(p)}" ${p === m.provider ? 'selected' : ''}>${esc(p)}</option>`).join('')}
          </select>
        </label>
        <label class="settings-c">model
          <select data-set-model>${selOptions(modelsFor(cap, m.provider), m.model, '')}</select>
        </label>
        <label class="settings-c">fallback provider
          <select data-set-fbp>
            <option value="">none</option>
            ${provs.filter(p => p !== m.provider).map(p => `<option value="${esc(p)}" ${p === fb.provider ? 'selected' : ''}>${esc(p)}</option>`).join('')}
          </select>
        </label>
        <label class="settings-c">fallback model
          <select data-set-fbm>${selOptions(provs.filter(p => p !== m.provider).flatMap(p => modelsFor(cap, p)), fb.model, 'none')}</select>
        </label>
        ${testable ? `<button class="btn-settings" data-act="test" ${testing ? 'disabled' : ''}>${testing ? '…testing' : '▶ Test'}</button>` : ''}
        <button class="btn-settings" data-act="save">💾 Save</button>
        <button class="btn-settings" data-act="reset">↺ Reset</button>
      </div>
      <div class="settings-result" data-result></div>
    </div>`;
  }

  function bulkBar() {
    const opts = capOptions('text_generation');
    const deepseek = opts.filter(o => o.provider === 'deepseek');
    const openai = opts.filter(o => o.provider === 'openai');
    let html = `
    <div class="settings-bulk">
      <select data-bulk-provider>
        <option value="deepseek">deepseek</option>
        <option value="openai">openai</option>
      </select>
      <select data-bulk-model>${selOptions(deepseek, deepseek[0] && deepseek[0].displayName ? opts.find(o => o.provider === 'deepseek').model : '')}</select>
    `;
    if (state.bulk) {
      const n = state.bulk.affected.length;
      html += `<span class="settings-bulk-info">applies to <strong>${n}</strong> text_generation module${n === 1 ? '' : 's'}</span>`;
      html += `<button class="btn-settings" data-act="bulk-confirm">✓ confirm</button>`;
      html += `<button class="btn-settings" data-act="bulk-cancel">✕ cancel</button>`;
    } else {
      html += `<button class="btn-settings" data-act="bulk-review">Review affected</button>`;
    }
    html += `<button class="btn-settings" data-act="reset-all">↺ Reset all</button>`;
    html += `</div>`;
    return html;
  }

  function render(panel) {
    if (state.err) {
      panel.innerHTML = `<div class="load-error">Models unavailable: ${esc(state.err)}</div>`;
      return;
    }
    if (!state.catalog) {
      panel.innerHTML = `<div class="load-note">Loading model catalog…</div>`;
      return;
    }
    if (!state.data || state.err) {
      panel.innerHTML = `<div class="load-note">Loading…</div>`;
      return;
    }
    const rows = state.data.modules || [];
    const byCap = {};
    for (const r of rows) (byCap[r.capability] = byCap[r.capability] || []).push(r);

    const parts = [];
    parts.push(`
      <div class="settings-head">
        <h2>⚙️ AI Models</h2>
        <label class="settings-ws">workspace
          <select data-ws>
            ${['personal', 'samudera', 'catalyze', 'shared'].map(w =>
              `<option value="${w}" ${w === state.ws ? 'selected' : ''}>${w}</option>`).join('')}
          </select>
        </label>
        ${bulkBar()}
      </div>
      <p class="settings-note">Effective = resolved provider/model (module override → workspace default → capability → system → env). Edits save a per-workspace module override.</p>`);

    for (const cap of Object.keys(byCap)) {
      const list = byCap[cap];
      parts.push(`
      <details class="settings-group" open>
        <summary><span class="badge badge--${cap === 'text_generation' ? 'good' : 'cat-4'}">${esc(capLabel(cap))}</span>
          <span class="settings-count">${list.length}</span></summary>
        ${list.map(moduleCard).join('')}
      </details>`);
    }
    panel.innerHTML = parts.join('');
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

  async function save(mid, card) {
    const body = {
      workspace: state.ws,
      module_id: mid,
      provider: val(card, 'data-set-provider'),
      model: val(card, 'data-set-model'),
      enabled: card.querySelector('[data-set-enabled]').checked,
    };
    const fbp = val(card, 'data-set-fbp');
    const fbm = val(card, 'data-set-fbm');
    if (fbp) { body.fallback_provider = fbp; body.fallback_model = fbm; }
    const res = await postJSON('/api/models/set', body);
    if (res.error !== undefined) throw new Error(res.error || 'save failed');
    return res;
  }

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

  async function setResult(card, html) {
    const box = card.querySelector('[data-result]');
    if (box) box.innerHTML = html;
  }

  function targetCard(el) {
    return el.closest('.settings-card');
  }

  async function onAction(card, act, el) {
    const mid = card && card.dataset.module;
    try {
      if (act === 'save') {
        const res = await save(mid, card);
        Comp.toast(`Saved ${mid} → ${res.module && res.module.provider}/${res.module && res.module.model}`, true);
        await refresh();
      } else if (act === 'reset') {
        const res = await postJSON('/api/models/reset', { workspace: state.ws, module_id: mid });
        if (res.error !== undefined) throw new Error(res.error);
        Comp.toast(`Reset ${mid}`, true);
        await refresh();
      } else if (act === 'test') {
        state.testing[mid] = true;
        render(document.getElementById('tab-settings'));
        const res = await postJSON('/api/models/test', { workspace: state.ws, module_id: mid });
        if (res.error !== undefined) throw new Error(res.error);
        await setResult(targetCard(el), res.ok
          ? `<span class="settings-ok">✓ ${esc(res.provider)}/${esc(res.model)} (${esc(res.source)}): ${esc(res.reply)}</span>`
          : `<span class="settings-bad">✗ ${esc(res.provider || '')}/${esc(res.model || '')}: ${esc(res.detail || res.error || 'failed')}</span>`);
        state.testing[mid] = false;
      }
    } catch (err) {
      await setResult(card, `<span class="settings-bad">✗ ${esc(err.message)}</span>`);
    }
  }

  async function bulkReview(provider, model) {
    const res = await postJSON('/api/models/bulk-apply',
      { workspace: state.ws, provider, model });
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
    Comp.toast(`Reset ALL model overrides (${state.ws})`, true);
    await refresh();
  }

  function bindEvents() {
    if (state.bound) return;
    state.bound = true;
    document.addEventListener('click', async ev => {
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      ev.preventDefault();
      const card = targetCard(btn);
      const act = btn.dataset.act;
      try {
        if (act === 'bulk-review') {
          const p = document.querySelector('[data-bulk-provider]').value;
          const m = document.querySelector('[data-bulk-model]').value;
          if (!m) return;
          await bulkReview(p, m);
        } else if (act === 'bulk-confirm') { await bulkApply(); }
        else if (act === 'bulk-cancel') { state.bulk = null; render(document.getElementById('tab-settings')); }
        else if (act === 'reset-all') { await resetAll(); }
        else if (card) { await onAction(card, act, btn); }
      } catch (err) {
        Comp.toast('Action failed: ' + (err.message || String(err)), false);
      }
    }, true);

    document.addEventListener('change', ev => {
      const wsSel = ev.target.closest('[data-ws]');
      if (wsSel) { state.ws = wsSel.value; state.data = null; refresh(); return; }
      const card = targetCard(ev.target);
      if (!card) return;
      const bp = ev.target.closest('[data-bulk-provider]');
      if (bp) rebuildBulkModel();
      if (ev.target.closest('[data-set-provider]')) rebuildModelSelect(card);
      else if (ev.target.closest('[data-set-fbp]')) rebuildFallbackSelect(card);
    });
  }

  function rebuildBulkModel() {
    const p = document.querySelector('[data-bulk-provider]').value;
    const sel = document.querySelector('[data-bulk-model]');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = selOptions(modelsFor('text_generation', p), cur, '');
  }

  function rebuildModelSelect(card) {
    const prov = val(card, 'data-set-provider');
    const cap = state.data && state.data.modules.find(m => m.module_id === card.dataset.module);
    const modelSel = card.querySelector('[data-set-model]');
    if (cap && modelSel) modelSel.innerHTML = selOptions(modelsFor(cap.capability, prov), '', '');
    rebuildFallbackSelect(card);
  }

  function rebuildFallbackSelect(card) {
    const prov = val(card, 'data-set-provider');
    const cap = state.data && state.data.modules.find(m => m.module_id === card.dataset.module);
    const provs = providersFor(cap && cap.capability).filter(p => p !== prov);
    const fbSel = card.querySelector('[data-set-fbm]');
    if (fbSel) fbSel.innerHTML = selOptions(provs.flatMap(p => modelsFor(cap.capability, p)), '', 'none');
  }

  async function load() {
    const panel = document.getElementById('tab-settings');
    if (!panel) return;
    bindEvents();
    await refresh();
  }

  return { load };
})();