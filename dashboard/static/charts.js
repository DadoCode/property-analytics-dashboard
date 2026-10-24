/* Shared Chart.js defaults + formatters, applied once so every chart on
   every page looks like one system instead of each template inventing its
   own axis/tooltip/gridline treatment. Per the design brief: minimal
   gridlines, no border box, no permanent point markers, short currency
   labels ("£10k" not "£10,000.00"), clean tooltips. */
/* One colour per concept, on every page (mirrors the --series-* tokens). */
const FS = {
  revenue: '#0e9f6e', costs: 'rgba(88,100,95,0.78)', profit: '#3f6489', capex: 'rgba(169,179,174,0.85)',
  brand: '#0e9f6e', brandSoft: 'rgba(14,159,110,0.12)', grid: 'rgba(20,32,28,0.05)',
};

(function () {
  if (typeof Chart === 'undefined') return;

  Chart.defaults.font.family = "'Inter', sans-serif";
  Chart.defaults.font.size = 11.5;
  Chart.defaults.color = '#8a948f';
  Chart.defaults.borderColor = 'rgba(20,32,28,0.045)';
  Chart.defaults.maintainAspectRatio = false;
  // Change the two values IN PLACE. Replacing the whole object drops Chart.js's own keys (type, fn, from, to, loop, delay): colour animations then
  // have no interpolator and throw "this._fn is not a function" on every update that changes a bar colour, which leaves the chart stuck mid-frame.
  Chart.defaults.animation.duration = 260;
  Chart.defaults.animation.easing = 'easeOutCubic';

  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 3.5;
  Chart.defaults.elements.point.hitRadius = 10;
  Chart.defaults.elements.point.hoverBorderWidth = 2;
  Chart.defaults.elements.line.borderWidth = 1.75;
  Chart.defaults.elements.line.borderJoinStyle = 'round';
  Chart.defaults.elements.bar.borderRadius = 2;
  Chart.defaults.elements.bar.borderSkipped = false;
  Chart.defaults.datasets.bar.maxBarThickness = 30;          // a few months must not become slabs
  Chart.defaults.datasets.bar.categoryPercentage = 0.7;
  Chart.defaults.scales.linear.border = { display: false };
  Chart.defaults.scales.linear.ticks = Object.assign({}, Chart.defaults.scales.linear.ticks, { padding: 8 });
  Chart.defaults.scales.category.grid = { display: false };
  Chart.defaults.scales.category.border = { display: false };

  Chart.defaults.plugins.legend.position = 'top';
  Chart.defaults.plugins.legend.align = 'start';
  Chart.defaults.plugins.legend.labels.boxWidth = 7;
  Chart.defaults.plugins.legend.labels.boxHeight = 7;
  Chart.defaults.plugins.legend.labels.pointStyleWidth = 7;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.padding = 14;
  Chart.defaults.plugins.legend.labels.color = '#6f7b75';

  // a quiet floating panel, not a black box
  Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(255,255,255,0.94)';
  Chart.defaults.plugins.tooltip.titleColor = '#14201c';
  Chart.defaults.plugins.tooltip.bodyColor = '#46544e';
  Chart.defaults.plugins.tooltip.borderColor = 'rgba(20,32,28,0.10)';
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.padding = 11;
  Chart.defaults.plugins.tooltip.cornerRadius = 10;
  Chart.defaults.plugins.tooltip.caretSize = 0;
  Chart.defaults.plugins.tooltip.titleFont = { weight: '600', size: 12 };
  Chart.defaults.plugins.tooltip.bodyFont = { size: 12 };
  Chart.defaults.plugins.tooltip.boxPadding = 5;
  Chart.defaults.plugins.tooltip.usePointStyle = true;
  Chart.defaults.plugins.tooltip.callbacks.title = (items) => items.length ? monthLabel(items[0].label) : '';   // set the one callback; replacing the whole object would delete Chart.js's own label/labelColor defaults and break every tooltip
  Chart.defaults.plugins.tooltip.displayColors = true;
})();

function moneyShort(v) {
  if (v === null || v === undefined) return '';
  const abs = Math.abs(v);
  if (abs >= 1000) return (v < 0 ? '\u2212' : '') + '£' + (abs / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  const r = Math.round(v);
  return (r < 0 ? '\u2212' : '') + '£' + Math.abs(r);
}

function moneyFull(v) {
  const n = Number(v);
  return (n < 0 && Math.round(Math.abs(n)) > 0 ? '\u2212' : '') + '£' + Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 0, maximumFractionDigits: 0 });   // −£4,162, never £-4,162
}

function pctShort(v) {
  return Math.round(v) + '%';
}

/* No vertical gridlines, one faint horizontal set -- the "minimal
   gridlines, no unnecessary border" rule applied to a Chart.js scale pair. */
const MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function monthLabel(l) {
  const m = /^(\d{4})-(\d{2})$/.exec(l);
  return m ? MONTHS_SHORT[+m[2] - 1] + " '" + m[1].slice(2) : l;
}
function monthTicks() {
  return { callback: function (v) { return monthLabel(this.getLabelForValue(v)); }, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 };
}

/* True when every value across the given series is null/undefined --
   i.e. there's genuinely no history to plot, as opposed to a series with
   a real zero in it somewhere. */
function isAllEmpty(...series) {
  return series.every((arr) => !arr || arr.every((v) => v === null || v === undefined));
}

/* Hides the canvas and shows a compact message in its place, instead of
   Chart.js auto-scaling a nonsense axis (e.g. repeated £1/£0 ticks) from
   an all-null series. */
function chartEmptyState(canvas, message) {
  canvas.style.display = 'none';
  const box = canvas.closest('.chart-box') || canvas.parentElement;
  const div = document.createElement('div');
  div.className = 'chart-empty';
  div.textContent = message || 'No historical data for this property yet.';
  box.appendChild(div);
}

function quietScales(yTickFormatter, extra) {
  return Object.assign({
    x: { grid: { display: false }, border: { display: false }, ticks: monthTicks() },
    y: {
      beginAtZero: true,
      grid: { color: FS.grid },
      border: { display: false },
      ticks: { callback: yTickFormatter, maxTicksLimit: 6 },
    },
  }, extra || {});
}

/* One right-hand drawer for every "inspect this record" interaction
   (transactions today; bookings, documents, targets follow). Rows or links
   use hx-get / hx-target="#drawer"; this opens it with a skeleton at once
   and keeps the user on the page they were on. */
let drawerOpener = null;
function openDrawer() {
  const d = document.getElementById('drawer');
  if (!d.classList.contains('open')) drawerOpener = document.activeElement;      // where focus returns to on close
  document.getElementById('drawer-backdrop').classList.add('open');
  d.classList.add('open');
  d.setAttribute('role', 'dialog'); d.setAttribute('aria-modal', 'true'); d.setAttribute('aria-label', 'Details');
  d.setAttribute('tabindex', '-1');
}
function closeDrawer() {
  const d = document.getElementById('drawer');
  const wasOpen = d.classList.contains('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
  d.classList.remove('open');
  if (wasOpen && drawerOpener && document.contains(drawerOpener) && typeof drawerOpener.focus === 'function') drawerOpener.focus();
  drawerOpener = null;
}
// once a drawer's content has loaded, move focus into it so keyboard and screen-reader users land on it
document.addEventListener('htmx:afterSwap', (e) => {
  const d = document.getElementById('drawer');
  if (e.target === d && d.classList.contains('open')) {
    const first = d.querySelector('.drawer-title, h2, h3');
    const label = first ? first.textContent.trim() : '';
    if (label) d.setAttribute('aria-label', label);
    d.focus();
  }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
document.addEventListener('DOMContentLoaded', () => {
  document.body.addEventListener('htmx:beforeRequest', (e) => {
    const t = e.detail.target;
    if (t && t.id === 'drawer') {
      t.innerHTML = '<div class="skeleton" style="height:34px;width:60%"></div><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line" style="width:70%"></div>';
      openDrawer();
    }
  });
});


/* Hosted demo: every write is refused server-side; this also greys out the
   controls so nobody fills in a form that cannot save. Runs on load and on
   each drawer swap (htmx), since drawers carry edit/delete forms. */
function applyDemoMode(root) {
  const msg = document.body.dataset.demoMessage;
  if (!document.body.dataset.demo) return;
  (root || document).querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
    if ((form.getAttribute('action') || '').indexOf('/login') !== -1) return;
    form.querySelectorAll('button:not([type="button"]), input[type="submit"], input[type="file"]').forEach((el) => {
      el.disabled = true; el.title = msg;
    });
    form.addEventListener('submit', (e) => e.preventDefault());
  });
  (root || document).querySelectorAll('.add-btn, [data-demo-off]').forEach((el) => { el.disabled = true; el.title = msg; });
}
document.addEventListener('DOMContentLoaded', () => {
  applyDemoMode();
  document.body.addEventListener('htmx:afterSwap', (e) => applyDemoMode(e.target));
});
