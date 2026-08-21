/* ═══════════════════════════════════════════════════════════════════
   tab-memory.js — 🧠 Memory Dashboard tab (Samudera-only)

   Three panels:
     1. Memory Search (unified recall across knowledge + Drive + state)
     2. Drive Index (project list, rebuild button, search)
     3. Knowledge Store (categories, entries, rebuild embeddings)
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

const MemoryTab = (() => {
  const { toast } = U;

  /* ── State ──────────────────────────────────────────────────────── */
  let _status = null;
  let _projects = [];
  let _lastRecall = null;
  let _knowledgeStatus = null;

  /* ── API helpers ────────────────────────────────────────────────── */
  async function getStatus() {
    try {
      return await U.fetchJSON('/api/memory-status');
    } catch { return null; }
  }

  async function recall(q, top = 10) {
    try {
      return await U.fetchJSON('/api/memory-recall?q=' + encodeURIComponent(q) + '&top=' + top);
    } catch { return { results: [] }; }
  }

  async function lastRecall() {
    try {
      return await U.fetchJSON('/api/memory-last');
    } catch { return { results: [] }; }
  }

  async function driveIndex() {
    try {
      return await U.fetchJSON('/api/drive-index');
    } catch { return { exists: false }; }
  }

  async function driveProjects() {
    try {
      return await U.fetchJSON('/api/drive-projects');
    } catch { return []; }
  }

  async function driveSearch(q, project) {
    const params = new URLSearchParams({ q });
    if (project) params.set('project', project);
    try {
      return await U.fetchJSON('/api/drive-search?' + params.toString());
    } catch { return []; }
  }

  async function knowledgeStatus() {
    try {
      return await U.fetchJSON('/api/knowledge-status');
    } catch { return {}; }
  }

  async function knowledgeEntries(cat) {
    const params = cat ? '?category=' + encodeURIComponent(cat) : '';
    try {
      return await U.fetchJSON('/api/knowledge-entries' + params);
    } catch { return []; }
  }

  async function rebuildDriveIndex() {
    try {
      return await U.fetchJSON('/api/drive-index-rebuild', { method: 'POST' });
    } catch (e) { return { error: e.message }; }
  }

  async function buildEmbeddings() {
    try {
      return await U.fetchJSON('/api/knowledge-build-embeddings', { method: 'POST' });
    } catch (e) { return { error: e.message }; }
  }

  /* ── Rendering helpers ──────────────────────────────────────────── */
  function scoreBar(score) {
    const pct = Math.round(score * 100);
    const color = pct >= 70 ? '#4caf50' : pct >= 40 ? '#ff9800' : '#f44336';
    return `<span style="display:inline-block;width:${pct}%;max-width:60px;height:6px;background:${color};border-radius:3px;vertical-align:middle"></span> <span style="font-size:0.8em;opacity:0.7">${pct}%</span>`;
  }

  function sourceIcon(src) {
    return { knowledge: '📚', drive: '📂', state: '📋' }[src] || '❓';
  }

  function projectBadge(p, pt) {
    if (!p) return '';
    return `<span class="mem-project-badge" data-pt="${pt || 'project'}">${U.esc(p)}</span>`;
  }

  /* ── Main load entry point (matches other tabs' pattern) ─────── */
  async function load() {
    const panel = document.getElementById('tab-memory');
    if (!panel) return;
    render(panel);
    await _refreshAll();
  }

  /* ── Main render ────────────────────────────────────────────────── */
  function render(slot) {
    slot.innerHTML = `
      <div class="mem-container">
        <div class="mem-search-panel">
          <h3 class="mem-section-title">🧠 Unified Memory Search</h3>
          <div class="mem-search-row">
            <input type="search" id="mem-search-q" class="mem-search-input"
                   placeholder="Search knowledge, Drive, tasks…" autocomplete="off" />
            <button id="mem-search-btn" class="btn mem-search-btn">Search</button>
          </div>
          <div id="mem-search-results" class="mem-results"></div>
        </div>

        <div class="mem-two-col">
          <div class="mem-drive-panel">
            <div class="mem-panel-header">
              <h3 class="mem-section-title">📂 Drive Index</h3>
              <button id="mem-drive-rebuild" class="btn mem-btn-sm">↻ Rebuild</button>
            </div>
            <div id="mem-drive-status" class="mem-status-line"></div>
            <div class="mem-drive-search-row">
              <input type="search" id="mem-drive-q" class="mem-search-input"
                     placeholder="Search Drive files…" autocomplete="off" />
              <select id="mem-drive-project" class="mem-select">
                <option value="">All projects</option>
              </select>
            </div>
            <div id="mem-drive-results" class="mem-results"></div>
          </div>

          <div class="mem-knowledge-panel">
            <div class="mem-panel-header">
              <h3 class="mem-section-title">📚 Knowledge Store</h3>
              <button id="mem-kb-build" class="btn mem-btn-sm">🏗 Build Embeddings</button>
            </div>
            <div id="mem-kb-status" class="mem-status-line"></div>
            <div id="mem-kb-categories" class="mem-categories"></div>
            <div id="mem-kb-entries" class="mem-results"></div>
          </div>
        </div>
      </div>
    `;
    _bindEvents(slot);
    _refreshAll();
  }

  function _bindEvents(slot) {
    /* Memory search */
    const q = slot.querySelector('#mem-search-q');
    const btn = slot.querySelector('#mem-search-btn');
    btn.addEventListener('click', () => _doMemorySearch(q.value));
    q.addEventListener('keydown', e => { if (e.key === 'Enter') _doMemorySearch(q.value); });

    /* Drive search */
    const dq = slot.querySelector('#mem-drive-q');
    const dp = slot.querySelector('#mem-drive-project');
    dq.addEventListener('keydown', e => { if (e.key === 'Enter') _doDriveSearch(dq.value, dp.value); });

    /* Rebuild buttons */
    slot.querySelector('#mem-drive-rebuild').addEventListener('click', _doRebuildDrive);
    slot.querySelector('#mem-kb-build').addEventListener('click', _doBuildEmbeddings);
  }

  /* ── Actions ────────────────────────────────────────────────────── */
  async function _refreshAll() {
    _status = await getStatus();
    _projects = await driveProjects();
    _lastRecall = await lastRecall();
    _knowledgeStatus = await knowledgeStatus();
    _renderDriveStatus();
    _renderProjectDropdown();
    _renderKnowledgeStatus();
    _renderLastRecall();
  }

  function _renderDriveStatus() {
    const el = document.getElementById('mem-drive-status');
    if (!el) return;
    if (!_status) { el.textContent = 'Loading…'; return; }
    if (!_status.drive_index) {
      el.innerHTML = '<span class="mem-warn">No index built.</span> Click Rebuild to scan your Drive.';
      return;
    }
    el.innerHTML = `<span class="mem-ok">Indexed</span>: ${_status.drive_files} files, ` +
                   `last scan: ${_status.drive_indexed_wib || '?'}`;
  }

  function _renderProjectDropdown() {
    const sel = document.getElementById('mem-drive-project');
    if (!sel) return;
    sel.innerHTML = '<option value="">All projects</option>';
    for (const p of _projects) {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.textContent = `${p.name} (${p.file_count} files) [${p.project_type}]`;
      sel.appendChild(opt);
    }
  }

  function _renderKnowledgeStatus() {
    const el = document.getElementById('mem-kb-status');
    if (!el) return;
    if (!_status) { el.textContent = 'Loading…'; return; }
    const cats = Object.keys(_knowledgeStatus || {}).length;
    el.innerHTML = `<span class="mem-ok">${_status.knowledge_entries}</span> entries across ` +
                   `${cats} categories. FAISS index: ${_status.knowledge_faiss ? '<span class="mem-ok">built</span>' : '<span class="mem-warn">not built</span>'}`;
    _renderKnowledgeCategories();
  }

  function _renderKnowledgeCategories() {
    const el = document.getElementById('mem-kb-categories');
    if (!el) return;
    const cats = _knowledgeStatus || {};
    const entries = Object.entries(cats).sort((a, b) => b[1] - a[1]);
    el.innerHTML = entries.map(([name, count]) =>
      `<button class="mem-cat-btn" data-cat="${name}">${name} <span class="mem-cat-count">${count}</span></button>`
    ).join('');
    el.querySelectorAll('.mem-cat-btn').forEach(btn => {
      btn.addEventListener('click', () => _loadKnowledgeEntries(btn.dataset.cat));
    });
  }

  async function _loadKnowledgeEntries(cat) {
    const el = document.getElementById('mem-kb-entries');
    if (!el) return;
    el.innerHTML = '<div class="mem-loading">Loading…</div>';
    const entries = await knowledgeEntries(cat);
    if (!entries.length) {
      el.innerHTML = '<div class="mem-empty">No entries found.</div>';
      return;
    }
    el.innerHTML = entries.map(e => `
      <div class="mem-entry-card">
        <div class="mem-entry-title">${U.esc(e.title)}</div>
        <div class="mem-entry-meta">
          <span class="mem-cat-badge">${e.category}</span>
          ${e.date ? `<span class="mem-date">${e.date}</span>` : ''}
          <span class="mem-conf mem-conf-${e.confidence}">${e.confidence}</span>
          ${e.tags ? `<span class="mem-tags">${U.esc(e.tags)}</span>` : ''}
        </div>
        <div class="mem-entry-preview">${U.esc(e.preview).substring(0, 200)}</div>
      </div>
    `).join('');
  }

  function _renderLastRecall() {
    if (!_lastRecall || !_lastRecall.results || !_lastRecall.results.length) return;
    const el = document.getElementById('mem-search-results');
    if (!el) return;
    const r = _lastRecall;
    el.innerHTML = `<div class="mem-cached-note">Last search: "${U.esc(r.query)}" at ${r.timestamp_wib || '?'}</div>` +
                   _renderRecallResults(r.results);
  }

  function _renderRecallResults(results) {
    if (!results || !results.length) {
      return '<div class="mem-empty">No results found.</div>';
    }
    return results.map(r => `
      <div class="mem-result-card mem-result-${r.source}">
        <div class="mem-result-header">
          <span class="mem-result-icon">${sourceIcon(r.source)}</span>
          <span class="mem-result-title">${U.esc(r.title || '?')}</span>
          ${projectBadge(r.project, r.project_type)}
          <span class="mem-result-score">${scoreBar(r.score)}</span>
        </div>
        <div class="mem-result-meta">
          <span class="mem-source-badge">${r.source}</span>
          ${r.category ? `<span class="mem-cat-badge">${r.category}</span>` : ''}
          ${r.date ? `<span class="mem-date">${r.date.substring(0, 10)}</span>` : ''}
          ${r.confidence ? `<span class="mem-conf mem-conf-${r.confidence}">${r.confidence}</span>` : ''}
        </div>
        <div class="mem-result-content">${U.esc((r.content || '').substring(0, 300))}</div>
      </div>
    `).join('');
  }

  async function _doMemorySearch(q) {
    if (!q.trim()) return;
    const el = document.getElementById('mem-search-results');
    if (!el) return;
    el.innerHTML = '<div class="mem-loading">Searching…</div>';
    const data = await recall(q.trim());
    _lastRecall = data;
    el.innerHTML = _renderRecallResults(data.results);
  }

  async function _doDriveSearch(q, project) {
    if (!q.trim()) return;
    const el = document.getElementById('mem-drive-results');
    if (!el) return;
    el.innerHTML = '<div class="mem-loading">Searching…</div>';
    const results = await driveSearch(q.trim(), project || null);
    if (!results.length) {
      el.innerHTML = '<div class="mem-empty">No files found.</div>';
      return;
    }
    el.innerHTML = results.map(r => `
      <div class="mem-result-card mem-result-drive">
        <div class="mem-result-header">
          <span class="mem-result-icon">📂</span>
          <span class="mem-result-title">${U.esc(r.name || '?')}</span>
          ${projectBadge(r.project, r.project_type)}
        </div>
        <div class="mem-result-meta">
          <span class="mem-date">${(r.modifiedTime || '').substring(0, 10)}</span>
          <span class="mem-mime">${r.mimeType || ''}</span>
        </div>
        <div class="mem-result-content">${U.esc(r.folder_path || '/')}</div>
      </div>
    `).join('');
  }

  async function _doRebuildDrive() {
    const btn = document.getElementById('mem-drive-rebuild');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning…'; }
    const el = document.getElementById('mem-drive-status');
    if (el) el.innerHTML = '<span class="mem-loading">Scanning Drive folders…</span>';
    const result = await rebuildDriveIndex();
    if (result.ok) {
      toast('Drive index rebuilt', 'success');
      await _refreshAll();
    } else {
      toast('Drive rebuild failed: ' + (result.error || 'unknown'), 'error');
      if (el) el.innerHTML = '<span class="mem-warn">Rebuild failed.</span>';
    }
    if (btn) { btn.disabled = false; btn.textContent = '↻ Rebuild'; }
  }

  async function _doBuildEmbeddings() {
    const btn = document.getElementById('mem-kb-build');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Building…'; }
    const el = document.getElementById('mem-kb-status');
    if (el) el.innerHTML = '<span class="mem-loading">Building FAISS embeddings…</span>';
    const result = await buildEmbeddings();
    if (result.ok) {
      toast('FAISS index built', 'success');
      await _refreshAll();
    } else {
      toast('Embeddings build failed: ' + (result.error || 'unknown'), 'error');
      if (el) el.innerHTML = '<span class="mem-warn">Build failed.</span>';
    }
    if (btn) { btn.disabled = false; btn.textContent = '🏗 Build Embeddings'; }
  }

  return { load };
})();

/* Register with the Tab system (same pattern as tab-agents.js) */
window.Tabs.memory = MemoryTab;
