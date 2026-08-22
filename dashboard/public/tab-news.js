/* ═══════════════════════════════════════════════════════════════════
   tab-news.js — Daily Intelligence Feed tab.
   Loads from /api/intelligence with 3 categories:
   - Global Economic Update (Bloomberg-style)
   - AI & Technology Update
   - Crypto Update
   Uses ONLY Comp, U (per components.js) + CSS classes.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(function () {
  const CATS = [
    { key: 'global_economy', icon: '\uD83C\uDF0E', label: 'Global Economy' },
    { key: 'ai_tech', icon: '\uD83E\uDD16', label: 'AI & Tech' },
    { key: 'crypto', icon: '\u20BF', label: 'Crypto' },
  ];

  const VERDICT_COLORS = {
    'TRY NOW': 'good',
    'MONITOR': 'warn',
    'IGNORE': 'muted',
  };

  const state = {
    activeCat: 'global_economy',
    data: null,
    dataError: null,
    generating: false,
  };

  async function load(cat) {
    state.activeCat = cat || 'global_economy';
    const tab = document.getElementById('tab-news');
    if (!tab) return;

    tab.innerHTML = '<div class="load-note">Loading intelligence feed...</div>';

    try {
      const data = await U.fetchJSON('/api/intelligence');
      state.data = data;
      state.dataError = null;
    } catch (err) {
      state.dataError = err.message || String(err);
      state.data = null;
    }

    render();
  }

  function render() {
    const tab = document.getElementById('tab-news');
    if (!tab) return;

    if (state.dataError) {
      tab.innerHTML = '<div class="load-error">Failed to load: ' + U.esc(state.dataError) + '</div>';
      return;
    }

    const data = state.data;
    if (!data) {
      tab.innerHTML = '<div class="load-note">Loading...</div>';
      return;
    }

    const parts = [];

    // Header with refresh button
    parts.push('<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--sp-4)">');
    parts.push('<div>');
    parts.push('<h2 style="margin:0 0 var(--sp-1) 0;font-size:var(--fs-lg);font-weight:600">Daily Intelligence</h2>');
    if (data.generated_wib) {
      parts.push('<div class="text-muted" style="font-size:var(--fs-sm)">Updated: ' + U.esc(data.generated_wib) + '</div>');
    }
    parts.push('</div>');
    if (state.generating) {
      parts.push('<button class="chip chip--active" disabled style="cursor:wait">⏳ Generating...</button>');
    } else {
      parts.push('<button id="intel-refresh" class="chip" onclick="window.Tabs.news.generate()">🔄 Refresh</button>');
    }
    parts.push('</div>');

    // Category tabs
    parts.push('<div class="chip-row" style="margin-bottom:var(--sp-4)">');
    for (const c of CATS) {
      const active = state.activeCat === c.key;
      const href = '#news/' + c.key;
      parts.push('<a href="' + href + '" class="' + (active ? 'chip chip--active' : 'chip') + '">' + c.icon + ' ' + U.esc(c.label) + '</a>');
    }
    parts.push('</div>');

    // Content
    if (data.empty || !data.categories) {
      parts.push('<div style="text-align:center;padding:var(--sp-8) 0">');
      parts.push('<div style="font-size:2rem;margin-bottom:var(--sp-2)">\uD83D\uDCF0</div>');
      parts.push('<div style="font-weight:600;margin-bottom:var(--sp-2)">No intelligence feed yet</div>');
      if (state.generating) {
        parts.push('<div class="text-muted">Generating feed... This may take 1-2 minutes.</div>');
      } else {
        parts.push('<button id="intel-refresh" class="chip chip--active" onclick="window.Tabs.news.generate()" style="margin-top:var(--sp-2)">🔄 Generate Now</button>');
      }
      parts.push('</div>');
    } else {
      const catData = data.categories[state.activeCat];
      if (!catData || !catData.stories || catData.stories.length === 0) {
        parts.push(Comp.emptyState({
          icon: '\uD83D\uDCED',
          title: 'No stories in this category',
          hint: 'Check back later or run the feed generator.',
        }));
      } else {
        // Stats row
        parts.push('<div class="hero-row" style="margin-bottom:var(--sp-4)">');
        parts.push(Comp.statTile({
          key: 'int-stories',
          icon: catData.icon || '\uD83D\uDCF0',
          label: catData.label || state.activeCat,
          value: catData.stories_count,
          sub: 'stories today',
          status: catData.stories_count > 0 ? 'good' : null,
        }));
        parts.push(Comp.statTile({
          key: 'int-fetched',
          icon: '\uD83D\uDD0D',
          label: 'Fetched',
          value: catData.fetched_items || 0,
          sub: 'RSS items scanned',
        }));
        parts.push('</div>');

        // Story cards
        for (const story of catData.stories) {
          parts.push(storyCard(story, state.activeCat));
        }
      }
    }

    // Source hint
    parts.push('<div class="text-muted" style="margin-top:var(--sp-4);font-size:var(--fs-sm)">Powered by RSS feeds + GPT-4o-mini analysis</div>');

    tab.innerHTML = parts.join('');
  }

  function storyCard(story, catKey) {
    const rows = [];

    // Main content sections based on category
    if (catKey === 'global_economy') {
      if (story.news) rows.push(section('NEWS', story.news));
      if (story.why_it_matters) rows.push(section('WHY IT MATTERS', story.why_it_matters));
      if (story.impact_on_indonesia) rows.push(section('IMPACT ON INDONESIA', story.impact_on_indonesia));
      if (story.what_to_watch) rows.push(section('WHAT TO WATCH', story.what_to_watch));
      if (story.my_take) rows.push(section('MY TAKE', story.my_take));
    } else if (catKey === 'ai_tech') {
      if (story.news) rows.push(section('NEWS', story.news));
      if (story.why_it_matters) rows.push(section('WHY IT MATTERS', story.why_it_matters));
      if (story.how_it_supports_work) rows.push(section('HOW IT CAN SUPPORT MY WORK', story.how_it_supports_work));
      if (story.what_to_watch) rows.push(section('WHAT TO WATCH', story.what_to_watch));
      if (story.my_take) rows.push(section('MY TAKE', story.my_take));
    } else if (catKey === 'crypto') {
      if (story.market_data) rows.push(section('MARKET', story.market_data));
      if (story.news) rows.push(section('NEWS', story.news));
      if (story.why_it_matters) rows.push(section('WHY IT MATTERS', story.why_it_matters));
      if (story.what_to_watch) rows.push(section('WHAT TO WATCH', story.what_to_watch));
      if (story.my_take) rows.push(section('MY TAKE', story.my_take));
    }

    // Badges
    const badges = [];
    if (story.importance) {
      const impKind = story.importance >= 8 ? 'good' : story.importance >= 6 ? 'warn' : 'muted';
      badges.push(Comp.badge(impKind, 'Imp ' + story.importance + '/10'));
    }
    if (story.verdict) {
      const vKind = VERDICT_COLORS[story.verdict] || 'muted';
      badges.push(Comp.badge(vKind, story.verdict));
    }

    // Source link
    const rightHtml = story.url
      ? '<a href="' + U.esc(story.url) + '" target="_blank" rel="noopener" class="chip">\uD83D\uDD17 Source</a>'
      : '';

    const metaParts = [];
    if (story.source) metaParts.push(U.esc(story.source));

    return '<div class="rows">' + Comp.listRow({
      key: 'intel-' + (story.url ? story.url.slice(-8) : Math.random().toString(36).slice(2, 8)),
      icon: CATS.find(c => c.key === catKey)?.icon || '\uD83D\uDCF0',
      title: story.headline || 'Untitled',
      badges: badges,
      meta: metaParts.join(' \u00B7 '),
      right: rightHtml,
      expandBody: rows.join(''),
    }) + '</div>';
  }

  function section(label, text) {
    return '<div style="margin-bottom:var(--sp-2)"><span class="text-muted" style="font-size:var(--fs-xs);text-transform:uppercase;letter-spacing:0.05em">' + U.esc(label) + '</span><div style="margin-top:2px">' + U.esc(text) + '</div></div>';
  }

  async function generate() {
    state.generating = true;
    render();
    try {
      await U.fetchJSON('/api/intelligence/generate', { method: 'POST' });
      // Poll for new feed after 30 seconds
      setTimeout(async () => {
        try {
          state.data = await U.fetchJSON('/api/intelligence');
        } catch (e) { /* ignore */ }
        state.generating = false;
        render();
      }, 30000);
    } catch (err) {
      state.generating = false;
      render();
    }
  }

  Tabs.news = { load: load, generate: generate };
})();
