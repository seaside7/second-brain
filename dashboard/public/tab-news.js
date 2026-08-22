/* ═══════════════════════════════════════════════════════════════════
   tab-news.js — Daily Intelligence Feed tab. Owns #tab-news only.
   Editorial briefing design: numbered story cards, teaser on collapse,
   colored section accents, tinted MY TAKE quote block.
   Loads from /api/intelligence. Uses ONLY Comp, U + .intel-* classes.
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

window.Tabs = window.Tabs || {};

(function () {
  const CATS = [
    { key: 'global_economy', icon: '\uD83C\uDF0E', label: 'Global Economy' },
    { key: 'ai_tech', icon: '\uD83E\uDD16', label: 'AI & Tech' },
    { key: 'crypto', icon: '\u20BF', label: 'Crypto' },
  ];

  const state = {
    activeCat: 'global_economy',
    data: null,
    dataError: null,
    generating: false,
  };

  async function load(cat) {
    if (cat) state.activeCat = cat;
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

  async function generate() {
    state.generating = true;
    render();
    try {
      await U.fetchJSON('/api/intelligence/generate', { method: 'POST' });
    } catch (err) { /* endpoint already fires the job; keep polling */ }
    // Poll until a fresh feed shows up (generation takes ~1-2 min)
    const started = Date.now();
    const before = JSON.stringify(stamp(state.data));
    const poll = setInterval(async () => {
      try {
        const fresh = await U.fetchJSON('/api/intelligence');
        if (JSON.stringify(stamp(fresh)) !== before || Date.now() - started > 180000) {
          clearInterval(poll);
          state.data = fresh;
          state.generating = false;
          render();
        }
      } catch (e) { /* keep polling */ }
    }, 10000);
  }

  function stamp(d) {
    return d && d.generated_wib ? d.generated_wib : null;
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

    // ── Header ──
    parts.push('<div class="intel-head">');
    parts.push('<div>');
    parts.push('<h2>Daily Intelligence</h2>');
    const when = data.generated_wib ? data.generated_wib.replace('T', ' · ').slice(0, 22) : '';
    parts.push('<div class="intel-updated">' + (when ? 'Updated ' + U.esc(when) + ' WIB' : '&nbsp;') + '</div>');
    parts.push('</div>');
    parts.push(refreshBtn());
    parts.push('</div>');

    // ── Segmented category control ──
    parts.push('<nav class="intel-seg">');
    for (const c of CATS) {
      const active = state.activeCat === c.key;
      parts.push('<a href="#news/' + c.key + '" class="' + (active ? 'is-active' : '') + '">' +
        '<span>' + c.icon + '</span><span>' + U.esc(c.label) + '</span></a>');
    }
    parts.push('</nav>');

    // ── Content ──
    if (data.empty || !data.categories) {
      parts.push(emptyBlock());
    } else {
      const catData = data.categories[state.activeCat];
      const stories = catData && catData.stories ? catData.stories : [];
      if (stories.length === 0) {
        parts.push('<div class="intel-empty"><div class="intel-empty-icon">\uD83D\uDCED</div>' +
          '<div class="intel-empty-title">No stories in this category yet</div>' +
          '<div class="intel-empty-hint">Hit Refresh to pull the latest briefing.</div></div>');
      } else {
        for (let i = 0; i < stories.length; i++) {
          parts.push(storyCard(stories[i], i));
        }
        parts.push('<div class="intel-updated" style="margin-top:var(--sp-4);text-align:center">' +
          U.esc(catData.label || '') + ' \u00B7 ' + catData.fetched_items + ' RSS items scanned \u00B7 GPT-4o-mini analysis</div>');
      }
    }

    tab.innerHTML = parts.join('');
  }

  function refreshBtn() {
    if (state.generating) {
      return '<button class="intel-refresh" disabled><span class="intel-refresh-spin">\u27F3</span> Generating\u2026</button>';
    }
    return '<button class="intel-refresh" onclick="window.Tabs.news.generate()">\uD83D\uDD04 Refresh feed</button>';
  }

  function emptyBlock() {
    let inner;
    if (state.generating) {
      inner = '<div class="intel-empty-title">Cooking up your first briefing\u2026</div>' +
        '<div class="intel-empty-hint">Fetching RSS feeds and analysing with AI. This takes 1-2 minutes.</div>';
    } else {
      inner = '<div class="intel-empty-title">No intelligence feed yet</div>' +
        '<div class="intel-empty-hint">Generate your first daily briefing now.</div>' +
        '<div style="margin-top:var(--sp-4)"><button class="intel-refresh" onclick="window.Tabs.news.generate()">\u2728 Generate Now</button></div>';
    }
    return '<div class="intel-empty"><div class="intel-empty-icon">\uD83D\uDCF0</div>' + inner + '</div>';
  }

  /* ── Editorial story card ─────────────────────────────────────── */

  function storyCard(story, idx) {
    const num = String(idx + 1).padStart(2, '0');
    const teaser = firstSentence(story.news || story.market_data || '');
    const verdict = verdictPill(story.verdict);

    const topline = [];
    if (story.source) topline.push('<span class="intel-source">' + U.esc(story.source) + '</span>');
    if (verdict) topline.push(verdict);
    if (story.importance) topline.push(dotRating(story.importance));

    return '<details class="intel-story" data-key="intel:' + U.esc(cardKey(story)) + '">' +
      '<summary>' +
        '<div class="intel-row">' +
          '<div class="intel-num">' + num + '</div>' +
          '<div class="intel-main">' +
            (topline.length ? '<div class="intel-topline">' + topline.join('') + '</div>' : '') +
            '<h3 class="intel-headline">' + U.esc(story.headline || 'Untitled') + '</h3>' +
            (teaser ? '<p class="intel-teaser">' + U.esc(teaser) + '</p>' : '') +
            '<div class="intel-baseline"><span>Read briefing</span></div>' +
          '</div>' +
          '<span class="intel-chev">\u25B8</span>' +
        '</div>' +
      '</summary>' +
      '<div class="intel-body">' +
        sectionsFor(story) +
        sourceLink(story.url) +
      '</div>' +
    '</details>';
  }

  function cardKey(story) {
    return (story.url ? story.url.slice(-24) : (story.headline || '').slice(0, 24)).replace(/[^\w-]/g, '');
  }

  function sectionsFor(story) {
    const secs = [];

    // MARKET first for crypto
    if (story.market_data) {
      secs.push(sec('market', 'Market', story.market_data));
    }
    if (story.news) {
      secs.push(sec('news', 'News', story.news));
    }
    if (story.why_it_matters) {
      secs.push(sec('why', 'Why it matters', story.why_it_matters));
    }
    if (story.impact_on_indonesia) {
      secs.push(sec('impact', 'Impact on Indonesia', story.impact_on_indonesia));
    }
    if (story.how_it_supports_work) {
      secs.push(sec('impact', 'How it can support my work', story.how_it_supports_work));
    }
    if (story.what_to_watch) {
      secs.push(sec('watch', 'What to watch next', story.what_to_watch));
    }
    if (story.my_take) {
      secs.push('<div class="intel-take"><span class="intel-label">\uD83D\uDCA1 My take</span>' +
        '<div class="intel-text">' + U.esc(story.my_take) + '</div></div>');
    }
    return secs.join('');
  }

  function sec(kind, label, text) {
    return '<div class="intel-sec intel-sec--' + kind + '">' +
      '<span class="intel-label">' + U.esc(label) + '</span>' +
      '<div class="intel-text">' + U.esc(text) + '</div>' +
    '</div>';
  }

  function dotRating(n) {
    let dots = '';
    for (let i = 1; i <= 5; i++) {
      dots += '<span class="intel-dot' + (i <= Math.round(n / 2) ? ' on' : '') + '"></span>';
    }
    return '<span class="intel-dots" title="Importance ' + n + '/10">' + dots + '</span>';
  }

  function verdictPill(v) {
    if (!v) return '';
    const cls = v === 'TRY NOW' ? 'try-now' : v === 'MONITOR' ? 'monitor' : 'ignore';
    return '<span class="intel-verdict intel-verdict--' + cls + '">' + U.esc(v) + '</span>';
  }

  function sourceLink(url) {
    if (!url) return '';
    return '<div class="intel-foot"><a class="intel-src-link" href="' + U.esc(url) +
      '" target="_blank" rel="noopener">\uD83D\uDD17 Read source \u2197</a></div>';
  }

  function firstSentence(text) {
    const t = String(text || '').trim();
    if (!t) return '';
    const m = t.match(/^(.+?[.!?:])\s/);
    return m ? m[1] : t.slice(0, 160);
  }

  Tabs.news = { load: load, generate: generate };
})();
