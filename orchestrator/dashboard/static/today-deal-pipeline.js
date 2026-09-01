/*
 * Compact, read-only deal pipeline for the Home / Today surface.
 *
 * The full Growth board owns pipeline mutation. This component only gives the
 * operator enough present-tense context to decide whether sales needs attention:
 * six fixed lanes, two deals per lane, and links into the full pipeline.
 *
 * Browser: <script src="/static/today-deal-pipeline.js"> exposes
 * window.TodayDealPipeline. Node tests can require() the same file.
 */
;(function (root) {
  'use strict';

  var LIVE_STAGES = ['lead', 'engaged', 'qualified', 'demo', 'proposal', 'won'];
  var SELLING_STAGES = LIVE_STAGES.slice(0, 5);
  var CARD_CAP = 2;
  var instance = 0;

  var DEFAULT_META = {
    lead:      { label: 'Lead', color: '#a1a1aa' },
    engaged:   { label: 'Engaged', color: '#5eead4' },
    qualified: { label: 'Qualified', color: '#93c5fd' },
    demo:      { label: 'Demo', color: '#fbbf24' },
    proposal:  { label: 'Proposal', color: '#c4b5fd' },
    won:       { label: 'Won / Active', color: '#6ee7b7' },
  };

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function amount(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function sumDeals(deals) {
    return deals.reduce(function (total, deal) {
      return total + amount(deal && deal.value);
    }, 0);
  }

  function buildModel(payload) {
    var byStage = payload && payload.by_stage && typeof payload.by_stage === 'object'
      ? payload.by_stage : {};
    var lanes = LIVE_STAGES.map(function (stage) {
      var deals = list(byStage[stage]).slice();
      return {
        stage: stage,
        count: deals.length,
        deals: deals,
        shown: deals.slice(0, CARD_CAP),
        remaining: Math.max(0, deals.length - CARD_CAP),
        value: sumDeals(deals),
      };
    });
    var selling = lanes.filter(function (lane) {
      return SELLING_STAGES.indexOf(lane.stage) !== -1;
    });
    var won = lanes[lanes.length - 1];
    var stalledDeals = list(byStage.stalled);
    var activeCount = lanes.reduce(function (total, lane) { return total + lane.count; }, 0);
    return {
      lanes: lanes,
      sellingCount: selling.reduce(function (total, lane) { return total + lane.count; }, 0),
      sellingValue: selling.reduce(function (total, lane) { return total + lane.value; }, 0),
      wonCount: won.count,
      wonValue: won.value,
      stalledCount: stalledDeals.length,
      stalledValue: sumDeals(stalledDeals),
      empty: activeCount === 0,
    };
  }

  function documentFor(container) {
    if (!container || !container.ownerDocument) {
      throw new TypeError('TodayDealPipeline requires a DOM container');
    }
    return container.ownerDocument;
  }

  function element(doc, tag, className, text) {
    var el = doc.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }

  function button(doc, className, label, text, callback) {
    var el = element(doc, 'button', className, text);
    el.type = 'button';
    el.setAttribute('aria-label', label);
    if (typeof callback === 'function') el.addEventListener('click', callback);
    return el;
  }

  function defaultMoney(value, currency) {
    var code = String(currency || 'MXN').toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency', currency: code, maximumFractionDigits: 0,
      }).format(amount(value));
    } catch (_) {
      return code + ' ' + Math.round(amount(value)).toLocaleString();
    }
  }

  function moneyFormatter(options) {
    var custom = options && typeof options.formatMoney === 'function'
      ? options.formatMoney : null;
    return function (value, currency) {
      if (custom) {
        try {
          var formatted = custom(amount(value), currency || 'MXN');
          if (formatted !== undefined && formatted !== null) return String(formatted);
        } catch (_) { /* fall back to the safe formatter */ }
      }
      return defaultMoney(value, currency);
    };
  }

  function metaFor(stage, options) {
    var supplied = options && options.stageMeta && options.stageMeta[stage];
    var fallback = DEFAULT_META[stage] || { label: stage, color: '#a1a1aa' };
    return {
      label: supplied && supplied.label != null ? String(supplied.label) : fallback.label,
      color: supplied && supplied.color != null ? String(supplied.color) : fallback.color,
    };
  }

  function currencyFor(deals) {
    for (var i = 0; i < deals.length; i += 1) {
      if (deals[i] && deals[i].currency) return deals[i].currency;
    }
    return 'MXN';
  }

  function baseRoot(doc, state) {
    var section = element(doc, 'section', 'today-deal-pipeline w-full max-w-full min-w-0 overflow-hidden');
    section.dataset.state = state;
    section.dataset.pipelineState = state;
    section.setAttribute('aria-label', 'Deal pipeline overview');
    section.style.width = '100%';
    section.style.maxWidth = '100%';
    section.style.minWidth = '0';
    section.style.overflow = 'hidden';
    section.style.boxSizing = 'border-box';
    return section;
  }

  function mount(container, node) {
    container.replaceChildren(node);
    return node;
  }

  function renderLoading(container) {
    var doc = documentFor(container);
    var section = baseRoot(doc, 'loading');
    var status = element(doc, 'div', 'rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-400', 'Loading deal pipeline…');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    section.appendChild(status);
    return mount(container, section);
  }

  function renderError(container, retry) {
    var doc = documentFor(container);
    var section = baseRoot(doc, 'error');
    var box = element(doc, 'div', 'rounded-xl border border-red-900/60 bg-red-950/20 p-4');
    box.setAttribute('role', 'alert');
    box.appendChild(element(doc, 'p', 'text-sm text-red-200', 'Deal pipeline could not be loaded.'));
    var retryButton = button(
      doc,
      'mt-3 rounded-lg border border-red-800 px-3 py-1.5 text-xs font-medium text-red-100 hover:bg-red-900/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400',
      'Retry loading deal pipeline',
      'Retry',
      typeof retry === 'function' ? retry : function () {}
    );
    retryButton.dataset.action = 'retry';
    box.appendChild(retryButton);
    section.appendChild(box);
    return mount(container, section);
  }

  function render(container, payload, options) {
    options = options || {};
    var doc = documentFor(container);
    var model = buildModel(payload);
    var formatMoney = moneyFormatter(options);
    var onDeal = typeof options.onDeal === 'function' ? options.onDeal : function () {};
    var onFullPipeline = typeof options.onFullPipeline === 'function'
      ? options.onFullPipeline : function () {};
    var uid = 'today-deal-pipeline-' + (++instance);
    var section = baseRoot(doc, model.empty ? 'empty' : 'ready');

    var header = element(doc, 'div', 'mb-3 flex items-start justify-between gap-3');
    var headingBox = element(doc, 'div', 'min-w-0');
    headingBox.appendChild(element(doc, 'h3', 'text-sm font-semibold text-zinc-100', '💼 Deal pipeline'));
    var totals = element(doc, 'div', 'mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-zinc-400');
    var allSellingDeals = model.lanes.slice(0, 5).flatMap(function (lane) { return lane.deals; });
    var wonDeals = model.lanes[5].deals;
    var sellingText = 'Selling · ' + model.sellingCount + ' · ' +
      formatMoney(model.sellingValue, currencyFor(allSellingDeals));
    var wonText = 'Won / Active · ' + model.wonCount + ' · ' +
      formatMoney(model.wonValue, currencyFor(wonDeals));
    totals.appendChild(element(doc, 'span', 'today-deal-pipeline-selling font-mono', sellingText));
    totals.appendChild(element(doc, 'span', 'today-deal-pipeline-won font-mono text-emerald-300', wonText));
    headingBox.appendChild(totals);
    header.appendChild(headingBox);
    var fullButton = button(
      doc,
      'flex-none rounded-lg px-2 py-1 text-xs font-medium text-blue-300 hover:bg-zinc-800 hover:text-blue-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
      'Open full deal pipeline',
      'Full pipeline →',
      function () { onFullPipeline(null); }
    );
    fullButton.dataset.action = 'full-pipeline';
    header.appendChild(fullButton);
    section.appendChild(header);

    if (model.empty) {
      var empty = element(doc, 'p', 'mb-3 rounded-lg border border-dashed border-zinc-800 px-3 py-2 text-xs text-zinc-500', 'No active opportunities.');
      empty.setAttribute('role', 'status');
      section.appendChild(empty);
    }

    var rail = element(doc, 'div', 'today-deal-pipeline-rail grid w-full max-w-full grid-cols-2 gap-2 pb-2 sm:grid-cols-3');
    rail.setAttribute('role', 'region');
    rail.setAttribute('aria-label', 'Deal pipeline stages');
    rail.tabIndex = 0;
    rail.style.width = '100%';
    rail.style.maxWidth = '100%';
    rail.style.display = 'grid';
    rail.style.boxSizing = 'border-box';

    model.lanes.forEach(function (lane) {
      var meta = metaFor(lane.stage, options);
      var laneEl = element(doc, 'section', 'today-deal-pipeline-lane min-w-0 rounded-xl border border-zinc-800 bg-zinc-900/50 p-2.5');
      laneEl.dataset.stage = lane.stage;
      laneEl.dataset.pipelineStage = lane.stage;
      laneEl.style.minWidth = '0';
      laneEl.style.boxSizing = 'border-box';
      var titleId = uid + '-' + lane.stage;
      laneEl.setAttribute('aria-labelledby', titleId);

      var laneHead = element(doc, 'div', 'mb-2 flex items-center justify-between gap-2');
      var laneTitle = element(doc, 'h4', 'truncate text-[11px] font-semibold', meta.label);
      laneTitle.id = titleId;
      laneTitle.style.color = meta.color;
      laneHead.appendChild(laneTitle);
      var count = element(doc, 'span', 'rounded bg-zinc-800 px-1.5 py-px text-[10px] text-zinc-400', lane.count);
      count.setAttribute('aria-label', lane.count + ' deals');
      laneHead.appendChild(count);
      laneEl.appendChild(laneHead);

      var cards = element(doc, 'div', 'flex min-h-[3.25rem] flex-col gap-1.5');
      if (lane.shown.length === 0) {
        cards.appendChild(element(doc, 'p', 'py-3 text-center text-[10px] text-zinc-600', 'No deals'));
      } else {
        lane.shown.forEach(function (deal) {
          deal = deal || {};
          var title = String(deal.title || deal.account_name || 'Untitled deal');
          var cardMoney = formatMoney(deal.value, deal.currency || 'MXN');
          var dealButton = button(
            doc,
            'today-deal-pipeline-deal w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2 py-1.5 text-left hover:border-zinc-700 hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            title + ', ' + meta.label + ', ' + cardMoney,
            undefined,
            function (event) { onDeal(deal.id, event.currentTarget); }
          );
          dealButton.dataset.dealId = deal.id == null ? '' : String(deal.id);
          dealButton.appendChild(element(doc, 'span', 'block truncate text-[11px] font-medium text-zinc-200', title));
          dealButton.appendChild(element(doc, 'span', 'mt-0.5 block truncate font-mono text-[10px] text-zinc-500', cardMoney));
          cards.appendChild(dealButton);
        });
      }
      laneEl.appendChild(cards);

      if (lane.remaining > 0) {
        laneEl.appendChild(button(
          doc,
          'mt-1.5 w-full rounded-md py-1 text-center text-[10px] text-blue-300 hover:bg-zinc-800 hover:text-blue-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'View ' + lane.remaining + ' more ' + meta.label + ' deals in full pipeline',
          '+' + lane.remaining + ' more',
          function () { onFullPipeline(lane.stage); }
        ));
      }
      rail.appendChild(laneEl);
    });
    section.appendChild(rail);

    if (model.stalledCount > 0) {
      var stalledCurrency = currencyFor(list(payload && payload.by_stage && payload.by_stage.stalled));
      var stalled = button(
        doc,
        'today-deal-pipeline-stalled mt-2 w-full rounded-lg px-2 py-1.5 text-left text-[11px] text-cyan-300 hover:bg-cyan-950/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400',
        'View ' + model.stalledCount + ' stalled deals in full pipeline',
        '🧊 ' + model.stalledCount + ' stalled · ' + formatMoney(model.stalledValue, stalledCurrency) + ' · view →',
        function () { onFullPipeline('stalled'); }
      );
      section.appendChild(stalled);
    }

    return mount(container, section);
  }

  var API = {
    LIVE_STAGES: LIVE_STAGES.slice(),
    buildModel: buildModel,
    render: render,
    renderLoading: renderLoading,
    renderError: renderError,
  };

  root.TodayDealPipeline = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);
