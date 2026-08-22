/* tab-finance.js — 💰 Finance tab: personal cash position from the Finance
   Google Sheet via GET /api/finance (server-cached 10 min).
   Sections: Cash hero · Income scenarios · Obligations · Friend debts. */

window.Tabs = window.Tabs || {};
window.Tabs.finance = {

  load() {
    const panel = $id('tab-finance');
    panel.innerHTML = `<div class="hero-row">${'<div class="skeleton skeleton-block-2"></div>'.repeat(4)}</div>`;
    U.fetchJSON('/api/finance')
      .then(d => {
        if (d.error) throw new Error(d.details || d.error);
        panel.innerHTML = this.render(d);
      })
      .catch(err => {
        panel.innerHTML = `<div class="load-error">Could not load finance data: ${U.esc(err.message)}</div>`;
      });
  },

  rp(n) {
    const v = Number(n) || 0;
    return 'Rp' + Math.round(v).toLocaleString('id-ID');
  },

  render(d) {
    const confIcon = { high: '🟢', variable: '🟡', uncertain: '🔴' };
    const income = (d.income_sources || []).map(s => `
      <div class="fin-row">
        <span>${confIcon[s.confidence] || '⚪'} ${U.esc(s.name)}</span>
        <strong>${this.rp(s.amount)}</strong>
      </div>`).join('');

    const scen = d.scenarios || {};
    const order = ['conservative', 'expected', 'optimistic'];
    const scenRows = order.filter(k => scen[k]).map(k => `
      <div class="fin-row">
        <span><strong>${k}</strong> <span class="fin-sub">${U.esc(scen[k].description || '')}</span></span>
        <strong>${this.rp(scen[k].monthly_income)}/mo</strong>
      </div>`).join('');

    const obligations = (d.obligations || []).map(o => {
      let dueTxt = '<span class="fin-sub">no due date</span>';
      if (o.due_day) {
        const dt = new Date(o.due_date + 'T00:00:00');
        const when = Number.isNaN(dt.getTime())
          ? `day ${o.due_day}`
          : dt.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
        dueTxt = o.overdue
          ? `<span class="fin-late">due ${when} · ${o.days_overdue}d late</span>`
          : `<span class="fin-sub">due ${when}</span>`;
      }
      const arrears = o.outstanding > 0
        ? `<span class="fin-sub fin-arr">arrears ${this.rp(o.outstanding)}</span>` : '';
      return `
      <div class="fin-row${o.overdue ? ' is-overdue' : ''}">
        <span>${U.esc(o.name)}${arrears}${dueTxt}</span>
        <strong>${this.rp(o.amount)}<span class="fin-sub">/mo</span></strong>
      </div>`;
    }).join('');

    const debts = (d.friend_debts || []).map(x => `
      <div class="fin-row">
        <span>${U.esc(x.name)}${x.note ? ` <span class="fin-sub">${U.esc(x.note)}</span>` : ''}
              ${x.paid > 0 ? `<span class="fin-paid">paid ${this.rp(x.paid)}</span>` : ''}</span>
        <strong>${this.rp(x.remaining ?? x.total)}</strong>
      </div>`).join('');

    const updated = d.generated_wib
      ? new Date(d.generated_wib).toLocaleString('en-GB',
          { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
      : '';

    return [
      `<div class="fin-hero">
         <div class="fin-hero-label">Current cash</div>
         <div class="fin-hero-value">${this.rp(d.cash)}</div>
         ${updated ? `<div class="fin-hero-updated">sheet sync ${U.esc(updated)} WIB</div>` : ''}
       </div>`,
      Comp.card({ key: 'fin-scenarios', icon: '📈', title: 'Income scenarios', open: true, body: scenRows }),
      Comp.card({ key: 'fin-income', icon: '💼', title: 'Income sources', body: income }),
      Comp.card({ key: 'fin-obligations', icon: '🏦', title: 'Obligations', count: String((d.obligations || []).length), open: true, body: obligations }),
      Comp.card({
        key: 'fin-debts', icon: '🤝', title: 'Friend debts',
        count: `${(d.friend_debts || []).length} · ${this.rp(d.friend_debts_total)}`,
        open: true, body: debts,
      }),
    ].join('\n');
  },
};
