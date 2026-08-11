/* ═══════════════════════════════════════════════════════════════════
   tab-news.js — 📰 News tab. Owns #tab-news only.
   Loads curated AI + Samudera Indonesia briefings from /api/news.
   Shows latest morning + midday briefings with story cards.
   Each story card shows: headline, summary, why it matters, relevance,
   source/URL, importance, and confidence.
   Supports category filter: #news/ai | #news/samudera | #news/all.
   Uses ONLY Comp, U (per components.js) + CSS classes.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(function () {
  const state = {
    filter: null,
    data: null,
    dataError: null,
  };

  async function load(filter) {
    state.filter = filter || null;
    const tab = document.getElementById('tab-news');
    if (!tab) return;

    tab.innerHTML = '<div class="load-note">Loading news...</div>';

    let url = '/api/news';
    if (filter && filter !== 'all') {
      url += '?category=' + encodeURIComponent(filter);
    }

    try {
      const data = await U.fetchJSON(url);
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
      tab.innerHTML = '<div class="load-error">Failed to load news: ' + U.esc(state.dataError) + '</div>';
      return;
    }

    const data = state.data;
    if (!data) {
      tab.innerHTML = '<div class="load-note">Loading...</div>';
      return;
    }

    const parts = [];

    // ── Category filter chips ──
    parts.push('<div class="chip-row" style="margin-bottom:var(--sp-4)">');
    const cats = [
      { key: null, label: 'All' },
      { key: 'ai', label: 'AI' },
      { key: 'samudera_indonesia', label: 'Samudera Indonesia' },
    ];
    for (const c of cats) {
      const active = (state.filter || null) === c.key;
      const href = c.key ? '#news/' + c.key : '#news';
      parts.push('<a href="' + href + '" class="' + (active ? 'chip chip--active' : 'chip') + '">' + U.esc(c.label) + '</a>');
    }
    parts.push('</div>');

    // ── Stat tiles: story counts ──
    const totalStories = data.stories ? data.stories.length : 0;
    const morningStories = data.morning ? data.morning.stories_count || 0 : 0;
    const middayStories = data.midday ? data.midday.stories_count || 0 : 0;

    parts.push('<div class="hero-row">');
    parts.push(Comp.statTile({
      key: 'news-total', icon: '📰', label: 'Selected today',
      value: totalStories, sub: 'top stories across all categories',
      status: totalStories > 0 ? 'good' : null,
    }));
    parts.push(Comp.statTile({
      key: 'news-morning', icon: '🌅', label: 'Morning',
      value: morningStories, sub: data.morning ? data.morning.time : 'not yet',
      status: morningStories > 0 ? 'good' : null,
    }));
    parts.push(Comp.statTile({
      key: 'news-midday', icon: '☀', label: 'Midday',
      value: middayStories, sub: data.midday ? data.midday.time : 'not yet',
      status: middayStories > 0 ? 'good' : null,
    }));
    parts.push('</div>');

    // ── Stories ──
    if (!data.stories || data.stories.length === 0) {
      parts.push(Comp.emptyState({
        icon: '📭',
        title: 'No news briefings yet',
        hint: 'Run `python .agent/skills/news-intelligence/scripts/news_briefing.py test --mode morning` to generate one.',
      }));
    } else {
      for (const story of data.stories) {
        parts.push(storyCard(story));
      }
    }

    // ── Source hint ──
    if (data.generated_wib) {
      parts.push('<div class="text-muted" style="margin-top:var(--sp-4);font-size:var(--fs-sm)">Generated: ' + U.esc(data.generated_wib) + ' WIB</div>');
    }

    tab.innerHTML = parts.join('');
  }

  function storyCard(story) {
    const cat = story.category || 'ai';
    const catLabel = cat === 'ai' ? 'AI' : 'Samudera';
    const catKind = cat === 'ai' ? 'cat-1' : 'cat-3';

    const rows = [];
    if (story.summary) {
      rows.push('<div class="text-secondary" style="margin-bottom:var(--sp-2)">' + U.esc(story.summary) + '</div>');
    }
    if (story.why_it_matters) {
      rows.push('<div style="margin-bottom:var(--sp-1)"><span class="text-muted">Why it matters:</span> ' + U.esc(story.why_it_matters) + '</div>');
    }
    if (story.relevance_to_me) {
      rows.push('<div style="margin-bottom:var(--sp-1)"><span class="text-muted">Relevance:</span> ' + U.esc(story.relevance_to_me) + '</div>');
    }

    const metaParts = [];
    if (story.source) {
      metaParts.push(U.esc(story.source));
    }
    if (story.published) {
      metaParts.push(U.esc(story.published));
    }

    const badges = [];
    badges.push(Comp.badge(catKind, catLabel));
    if (story.importance) {
      const impKind = story.importance >= 8 ? 'good' : story.importance >= 6 ? 'warn' : 'muted';
      badges.push(Comp.badge(impKind, 'Imp ' + story.importance + '/10'));
    }
    if (story.confidence) {
      badges.push(Comp.badge('muted', 'Conf ' + story.confidence + '/10'));
    }

    const rightHtml = story.url
      ? '<a href="' + U.esc(story.url) + '" target="_blank" rel="noopener" class="chip">🔗 Source</a>'
      : '';

    const cardBody = '<div class="rows">' + Comp.listRow({
      key: 'news-' + (story.url_hash || Math.random().toString(36).slice(2, 8)),
      icon: '📰',
      title: story.headline || story.title || 'Untitled',
      badges: badges,
      meta: metaParts.join(' · '),
      right: rightHtml,
      expandBody: rows.join(''),
    }) + '</div>';

    return cardBody;
  }

  Tabs.news = { load: load };
})();
