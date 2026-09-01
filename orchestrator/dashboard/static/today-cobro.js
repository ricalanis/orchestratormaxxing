/*
 * Compact, read-only cash-flow view for the Home / Today surface.
 *
 * The CRM owns invoicing and payment mutation. This component only presents
 * the frozen /api/crm/cash-flow payload and routes navigation through callbacks.
 *
 * Browser: <script src="/static/today-cobro.js"> exposes window.TodayCobro.
 * Node tests can require() the same file.
 */
;(function (root) {
  'use strict';

  var OVERDUE_CAP = 2;
  var WEEK_CAP = 3;
  var WEEKDAYS = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function amount(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function count(value) {
    return Math.max(0, Math.floor(amount(value)));
  }

  function titleFor(row) {
    row = row || {};
    return String(row.title || row.account_name || 'Cobro sin nombre');
  }

  function shortDate(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return 'sin fecha';
    var year = Number(match[1]);
    var month = Number(match[2]);
    var day = Number(match[3]);
    var date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
      return 'sin fecha';
    }
    return WEEKDAYS[date.getUTCDay()] + ' ' + day;
  }

  function weekState(row) {
    row = row || {};
    var daysLate = count(row.days_late);
    if (row.kind === 'launch') {
      if (daysLate >= 1) {
        return { key: 'launch-late', label: 'lanzar hace ' + daysLate + 'd', className: 'text-amber-300' };
      }
      return { key: 'launch', label: 'lanzar cobro', className: 'text-blue-300' };
    }
    if (row.paid) {
      return { key: 'paid', label: '✅', className: 'text-emerald-300' };
    }
    if (daysLate >= 3) {
      return { key: 'overdue', label: 'esperado hace ' + daysLate + 'd', className: 'text-red-200' };
    }
    if (daysLate >= 1) {
      return { key: 'late', label: 'esperado hace ' + daysLate + 'd', className: 'text-amber-300' };
    }
    if (row.invoiced) {
      return { key: 'invoiced', label: 'facturado', className: 'text-amber-300' };
    }
    return {
      key: 'uninvoiced',
      label: 'sin factura',
      className: 'text-zinc-400',
      deliveryLabel: row.delivered ? 'entregado' : 'sin entregar',
      deliveryClassName: 'text-zinc-500',
    };
  }

  function leakModel(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    var items = [];
    var uninvoicedCount = count(raw.uninvoiced_count);
    var noExpectedCount = count(raw.no_expected_count);
    var noProjectCount = count(raw.no_project_count);
    var launchOverdueCount = count(raw.launch_overdue_count);
    if (launchOverdueCount > 0) {
      items.push({
        key: 'launch-overdue',
        count: launchOverdueCount,
        dealId: raw.first_launch_overdue_deal_id,
        className: 'text-amber-300',
      });
    }
    if (uninvoicedCount > 0) {
      items.push({
        key: 'uninvoiced',
        count: uninvoicedCount,
        value: amount(raw.uninvoiced_value),
        dealId: raw.first_uninvoiced_deal_id,
        className: 'text-amber-300',
      });
    }
    if (noExpectedCount > 0) {
      items.push({
        key: 'no-expected',
        count: noExpectedCount,
        dealId: raw.first_no_expected_deal_id,
        className: 'text-zinc-500',
      });
    }
    if (noProjectCount > 0) {
      items.push({
        key: 'no-project',
        count: noProjectCount,
        dealId: raw.first_no_project_deal_id,
        className: 'text-zinc-500',
      });
    }
    return items;
  }

  function buildModel(payload) {
    payload = payload && typeof payload === 'object' ? payload : {};
    var week = payload.week && typeof payload.week === 'object' ? payload.week : {};
    var month = payload.month && typeof payload.month === 'object' ? payload.month : {};
    var weekRows = list(week.rows).slice();
    var overdueRows = list(payload.overdue).filter(function (row) {
      return row && count(row.days_late) >= 3;
    });
    var leaks = leakModel(payload.leaks);
    var target = amount(month.target);
    var hasTarget = month.target !== null && month.target !== undefined && target > 0;
    var collected = amount(month.collected);
    var slippage = payload.slippage && typeof payload.slippage === 'object'
      ? payload.slippage : null;

    return {
      week: {
        total: amount(week.total),
        count: weekRows.length,
        rows: weekRows.map(function (row) {
          row = row || {};
          return {
            dealId: row.deal_id,
            title: titleFor(row),
            value: amount(row.paid && row.cash != null ? row.cash : row.value),
            currency: row.currency || 'MXN',
            dateLabel: (row.kind === 'launch' ? '🧾 ' : '')
              + shortDate(row.date || row.expected_payment_date),
            state: weekState(row),
          };
        }),
        shown: weekRows.slice(0, WEEK_CAP).map(function (row) {
          row = row || {};
          return {
            dealId: row.deal_id,
            title: titleFor(row),
            value: amount(row.paid && row.cash != null ? row.cash : row.value),
            currency: row.currency || 'MXN',
            dateLabel: (row.kind === 'launch' ? '🧾 ' : '')
              + shortDate(row.date || row.expected_payment_date),
            state: weekState(row),
          };
        }),
        remaining: Math.max(0, weekRows.length - WEEK_CAP),
        launches: weekRows.filter(function (row) {
          return row && row.kind === 'launch';
        }).length,
        measured: weekRows.length > 0,
      },
      overdue: {
        rows: overdueRows,
        shown: overdueRows.slice(0, OVERDUE_CAP),
        remaining: Math.max(0, overdueRows.length - OVERDUE_CAP),
      },
      month: {
        label: String(month.label || ''),
        collected: collected,
        invoiced: amount(month.invoiced),
        expected: amount(month.expected),
        target: target,
        hasTarget: hasTarget,
        progress: hasTarget ? Math.max(0, Math.min(100, (collected / target) * 100)) : 0,
      },
      leaks: leaks,
      slippage: slippage ? {
        medianDays: amount(slippage.median_days),
        count: count(slippage.count),
        className: Math.abs(amount(slippage.median_days)) > 7 ? 'text-amber-300' : 'text-zinc-500',
      } : null,
      narrative: payload.narrative && payload.narrative.text != null
        ? String(payload.narrative.text) : '',
      emptyHealthy: overdueRows.length === 0 && weekRows.length === 0 && leaks.length === 0,
    };
  }

  function documentFor(container) {
    if (!container || !container.ownerDocument) {
      throw new TypeError('TodayCobro requires a DOM container');
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

  function defaultMoney(value) {
    var rounded = Math.round(amount(value));
    try {
      return '$' + rounded.toLocaleString('es-MX');
    } catch (_) {
      return '$' + String(rounded).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
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
      return defaultMoney(value);
    };
  }

  function baseRoot(doc, state) {
    var section = element(doc, 'section', 'today-cobro w-full max-w-full min-w-0 overflow-hidden');
    section.dataset.state = state;
    section.dataset.cobroState = state;
    section.setAttribute('aria-label', 'Cobro y flujo de efectivo');
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
    var status = element(doc, 'div', 'rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-400', 'Cargando cobros…');
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
    box.appendChild(element(doc, 'p', 'text-sm text-red-200', 'No se pudieron cargar los cobros.'));
    var retryButton = button(
      doc,
      'mt-3 rounded-lg border border-red-800 px-3 py-1.5 text-xs font-medium text-red-100 hover:bg-red-900/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400',
      'Reintentar carga de cobros',
      'Reintentar',
      typeof retry === 'function' ? retry : function () {}
    );
    retryButton.dataset.action = 'retry';
    box.appendChild(retryButton);
    section.appendChild(box);
    return mount(container, section);
  }

  function renderMonth(doc, model, formatMoney) {
    var line = element(doc, 'div', 'today-cobro-month flex min-w-0 items-center gap-2 text-[11px] text-zinc-400');
    line.appendChild(element(doc, 'span', 'flex-none font-semibold text-zinc-300', model.month.label));
    if (model.month.hasTarget) {
      var bar = element(doc, 'div', 'today-cobro-month-bar h-1 min-w-[2rem] flex-1 overflow-hidden rounded bg-zinc-800');
      var fill = element(doc, 'div', 'h-full rounded bg-emerald-400');
      fill.style.width = model.month.progress + '%';
      bar.appendChild(fill);
      line.appendChild(bar);
      line.appendChild(element(
        doc,
        'span',
        'flex-none font-mono text-[11px] text-zinc-400',
        'fact ' + formatMoney(model.month.invoiced, 'MXN') + ' · '
          + formatMoney(model.month.collected, 'MXN') + ' / ' + formatMoney(model.month.expected, 'MXN')
      ));
    } else {
      line.appendChild(element(
        doc,
        'span',
        'min-w-0 text-[11px] text-zinc-400',
        'facturado ' + formatMoney(model.month.invoiced, 'MXN')
          + ' · cobrado ' + formatMoney(model.month.collected, 'MXN')
          + ' · esperado ' + formatMoney(model.month.expected, 'MXN')
      ));
    }
    if (model.slippage) {
      line.appendChild(element(
        doc,
        'span',
        'today-cobro-slippage flex-none ' + model.slippage.className,
        '· media ' + (model.slippage.medianDays >= 0 ? '+' : '') + model.slippage.medianDays + 'd'
      ));
    }
    return line;
  }

  function render(container, payload, options) {
    options = options || {};
    var doc = documentFor(container);
    var model = buildModel(payload);
    var formatMoney = moneyFormatter(options);
    var onDeal = typeof options.onDeal === 'function' ? options.onDeal : function () {};
    var onGrowth = typeof options.onGrowth === 'function' ? options.onGrowth : function () {};
    var section = baseRoot(doc, model.emptyHealthy ? 'empty' : 'ready');

    var header = element(doc, 'div', 'mb-3 flex items-start justify-between gap-3');
    header.appendChild(element(doc, 'h3', 'text-sm font-semibold text-zinc-100', '💰 Cobro'));
    var growthButton = button(
      doc,
      'flex-none rounded-lg px-2 py-1 text-xs text-blue-300 hover:bg-zinc-800 hover:text-blue-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
      'Abrir CRM',
      'CRM →',
      function () { onGrowth(); }
    );
    growthButton.dataset.action = 'growth';
    header.appendChild(growthButton);
    section.appendChild(header);

    if (model.emptyHealthy) {
      section.appendChild(element(doc, 'p', 'today-cobro-healthy mb-2 text-xs text-emerald-300', '✓ Nada por cobrar esta semana'));
      section.appendChild(renderMonth(doc, model, formatMoney));
      return mount(container, section);
    }

    if (model.overdue.shown.length > 0) {
      var overdueBox = element(doc, 'div', 'today-cobro-overdue mb-3 rounded-xl border border-red-900/60 bg-red-950/20 p-3');
      model.overdue.shown.forEach(function (row) {
        row = row || {};
        var title = titleFor(row);
        var account = String(row.account_name || row.title || 'Cobro sin nombre');
        var text = '🔴 ' + formatMoney(row.value, row.currency || 'MXN') + ' · ' + account + ' · +' + count(row.days_late) + 'd';
        var rowButton = button(
          doc,
          'today-cobro-overdue-row block w-full rounded px-1 py-1 text-left text-xs text-red-200 hover:bg-red-900/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400',
          text,
          text,
          function () { onDeal(row.deal_id, title); }
        );
        rowButton.dataset.dealId = row.deal_id == null ? '' : String(row.deal_id);
        overdueBox.appendChild(rowButton);
      });
      if (model.overdue.remaining > 0) {
        overdueBox.appendChild(button(
          doc,
          'mt-1 rounded px-1 py-1 text-xs text-red-200 hover:bg-red-900/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400',
          'Ver ' + model.overdue.remaining + ' cobros vencidos más',
          '+' + model.overdue.remaining + ' →',
          function () { onGrowth(); }
        ));
      }
      section.appendChild(overdueBox);
    }

    var hero = element(doc, 'div', 'today-cobro-week mb-3');
    hero.appendChild(element(
      doc,
      'div',
      model.week.measured ? 'text-2xl font-bold tabular-nums text-zinc-100' : 'text-2xl font-bold tabular-nums text-zinc-600',
      model.week.measured ? formatMoney(model.week.total, 'MXN') : '—'
    ));
    hero.appendChild(element(doc, 'div', 'text-[11px] text-zinc-400',
      'entra esta semana · ' + (model.week.count - model.week.launches) + ' cobros'
      + (model.week.launches > 0 ? ' · ' + model.week.launches + ' lanzamiento' + (model.week.launches === 1 ? '' : 's') : '')));

    if (model.week.shown.length > 0) {
      var weekList = element(doc, 'div', 'mt-2 flex flex-col gap-1');
      model.week.shown.forEach(function (row) {
        var rowButton = button(
          doc,
          'today-cobro-week-row flex w-full min-w-0 items-center gap-2 rounded px-1 py-1 text-left hover:bg-zinc-900/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          row.dateLabel + ', ' + row.title + ', ' + formatMoney(row.value, row.currency),
          undefined,
          function () { onDeal(row.dealId, row.title); }
        );
        rowButton.dataset.dealId = row.dealId == null ? '' : String(row.dealId);
        rowButton.dataset.rowState = row.state.key;
        rowButton.appendChild(element(doc, 'span', 'flex-none font-mono text-[10px] text-zinc-500', row.dateLabel));
        rowButton.appendChild(element(doc, 'span', 'min-w-0 flex-1 truncate text-[11px] text-zinc-200', row.title));
        rowButton.appendChild(element(doc, 'span', 'flex-none font-mono text-[10px] text-zinc-300', formatMoney(row.value, row.currency)));
        rowButton.appendChild(element(doc, 'span', 'flex-none text-[10px] ' + row.state.className, row.state.label));
        if (row.state.deliveryLabel) {
          rowButton.appendChild(element(
            doc,
            'span',
            'flex-none rounded bg-zinc-800 px-1 py-px text-[9px] ' + row.state.deliveryClassName,
            row.state.deliveryLabel
          ));
        }
        weekList.appendChild(rowButton);
      });
      if (model.week.remaining > 0) {
        weekList.appendChild(button(
          doc,
          'rounded px-1 py-1 text-left text-[11px] text-blue-300 hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'Ver ' + model.week.remaining + ' cobros más',
          '+' + model.week.remaining + ' →',
          function () { onGrowth(); }
        ));
      }
      hero.appendChild(weekList);
    }
    section.appendChild(hero);
    section.appendChild(renderMonth(doc, model, formatMoney));

    if (model.leaks.length > 0) {
      var leaksLine = element(doc, 'div', 'today-cobro-leaks mt-2 flex min-w-0 flex-wrap items-center gap-x-1 text-[11px]');
      leaksLine.appendChild(element(doc, 'span', 'text-amber-300', '⚠'));
      model.leaks.forEach(function (leak, index) {
        if (index > 0) leaksLine.appendChild(element(doc, 'span', 'text-zinc-600', '·'));
        var label = leak.key === 'uninvoiced'
          ? leak.count + ' won sin facturar · ' + formatMoney(leak.value, 'MXN')
          : leak.key === 'launch-overdue'
            ? '🧾 ' + leak.count + ' lanzamiento' + (leak.count === 1 ? '' : 's') + ' vencido' + (leak.count === 1 ? '' : 's')
            : leak.key === 'no-expected'
              ? leak.count + ' sin fecha'
              : leak.count + ' sin proyecto';
        var leakButton = button(
          doc,
          'today-cobro-leak today-cobro-leak-' + leak.key + ' rounded px-0.5 py-0.5 ' + leak.className + ' hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          label,
          label,
          function () { onDeal(leak.dealId); }
        );
        leakButton.dataset.dealId = leak.dealId == null ? '' : String(leak.dealId);
        leaksLine.appendChild(leakButton);
      });
      section.appendChild(leaksLine);
    }

    if (model.narrative) {
      section.appendChild(element(doc, 'p', 'today-cobro-narrative mt-2 truncate text-[11px] italic text-zinc-400', model.narrative));
    }

    return mount(container, section);
  }

  var API = {
    buildModel: buildModel,
    render: render,
    renderLoading: renderLoading,
    renderError: renderError,
  };

  root.TodayCobro = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);
