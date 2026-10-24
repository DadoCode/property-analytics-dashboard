/* Shell behaviour: navigation progress line and the ⌘K quick-jump. */
(function () {
  /* ---------- view-only accounts: show everything, allow no changes ---------- */
  function lockForms(root) {
    if (!document.body.classList.contains('viewer')) return;
    root.querySelectorAll('form[method="post"]').forEach(f => {
      f.classList.add('view-only');
      f.querySelectorAll('input, select, textarea, button').forEach(el => { el.disabled = true; });
      f.addEventListener('submit', e => e.preventDefault());
    });
  }
  document.addEventListener('DOMContentLoaded', () => lockForms(document));
  document.addEventListener('htmx:afterSwap', (e) => lockForms(e.target));

  /* ---------- upload progress overlay: reassurance during the real, synchronous extraction wait ---------- */
  (function () {
    const overlay = document.getElementById('upload-overlay');
    const stageEl = document.getElementById('upload-overlay-stage');
    if (!overlay || !stageEl) return;
    const stages = ['Uploading…', 'Reading the document…', 'Extracting line items…', 'Checking for duplicates…', 'Almost done…'];
    document.querySelectorAll('form.upload-form').forEach(form => {
      form.addEventListener('submit', (e) => {
        if (e.defaultPrevented || !form.checkValidity()) return;
        overlay.classList.add('open');
        stageEl.textContent = stages[0];
        let i = 0;
        setInterval(() => { i = Math.min(i + 1, stages.length - 1); stageEl.textContent = stages[i]; }, 1400);
        form.querySelectorAll('button[type="submit"]').forEach(b => { b.disabled = true; });
      });
    });
  })();

  /* ---------- keyboard access for clickable rows ---------- */
  /* <tr class="clickable"> (transaction/vendor/category/document/booking
     rows) only ever worked with a mouse -- a <tr> isn't focusable and has
     no keyboard handler, so Tab skips it and Enter does nothing. This
     makes it a real button for keyboard/screen-reader use without
     changing how it looks or works with a mouse. */
  function enableRowKeyboardAccess(root) {
    root.querySelectorAll('tr.clickable:not([tabindex])').forEach(row => {
      row.setAttribute('tabindex', '0');
      row.setAttribute('role', 'button');
      row.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        row.click();
      });
    });
  }
  document.addEventListener('DOMContentLoaded', () => enableRowKeyboardAccess(document));
  document.addEventListener('htmx:afterSwap', (e) => enableRowKeyboardAccess(e.target));

  /* ---------- filter menus: a popover that would run off the screen edge is nudged back inside ---------- */
  document.addEventListener('toggle', (e) => {
    const m = e.target;
    if (!m.matches || !m.matches('details.pr-menu') || !m.open) return;
    const pop = m.querySelector('.pr-pop');
    if (!pop) return;
    pop.style.marginLeft = '';
    const r = pop.getBoundingClientRect(), vw = document.documentElement.clientWidth;
    if (r.right > vw - 8) pop.style.marginLeft = (vw - 8 - r.right) + 'px';
    else if (r.left < 8) pop.style.marginLeft = (8 - r.left) + 'px';
  }, true);

  /* ---------- navigation progress ---------- */
  const bar = document.getElementById('navbar');
  function start() { if (!bar) return; bar.style.opacity = 1; bar.style.width = '70%'; }
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || a.target === '_blank') return;
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript') || a.hasAttribute('hx-get') || a.origin !== location.origin) return;
    start();
  });
  document.addEventListener('submit', (e) => { if (!e.defaultPrevented) start(); });
  window.addEventListener('pageshow', () => { if (bar) { bar.style.opacity = 0; bar.style.width = '0'; } });

  /* ---------- quick jump ---------- */
  const box = document.getElementById('palette');
  if (!box) return;
  const input = document.getElementById('palette-input');
  const list = document.getElementById('palette-list');
  const items = JSON.parse(document.getElementById('palette-data').textContent);
  let shown = [], cursor = 0;

  // fine-line glyphs, the same optical weight as the sidebar icons
  const svg = (d) => '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
  const GLYPH = {
    overview: svg('<rect x="3.5" y="3.5" width="7" height="9" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="5" rx="1.6"/><rect x="13.5" y="11.5" width="7" height="9" rx="1.6"/><rect x="3.5" y="15.5" width="7" height="5" rx="1.6"/>'),
    property: svg('<path d="M4.5 20.5V5.6c0-.9.7-1.6 1.6-1.6h7.3c.9 0 1.6.7 1.6 1.6v14.9"/><path d="M15 9.5h3c.9 0 1.5.7 1.5 1.5v9.5"/><path d="M3 20.5h18"/><path d="M8 8h3M8 11.5h3M8 15h3"/>'),
    bookings: svg('<rect x="3.5" y="5" width="17" height="15.5" rx="2.6"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    expenses: svg('<path d="M6 3.5h12v17l-3-2-3 2-3-2-3 2z"/><path d="M9 8.5h6M9 12.2h6"/>'),
    documents: svg('<path d="M7.2 3.5h6.6L19 8.7v10.6c0 .9-.7 1.5-1.5 1.5h-10c-.8 0-1.5-.6-1.5-1.5V5c0-.8.7-1.5 1.2-1.5z"/><path d="M13.5 3.5v5.2H19"/>'),
    reports: svg('<path d="M4 4v16h16"/><path d="M8.5 16v-5M12.5 16V8M16.5 16v-3"/>'),
    targets: svg('<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r=".7"/>'),
    import: svg('<path d="M12 3.5v11.5M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 20h15"/>'),
  };
  const glyphFor = (i) => {
    if (i.hint === 'Property') return GLYPH.property;
    if (i.hint === 'Imports') return GLYPH.import;
    const l = i.label.toLowerCase();
    for (const k of ['overview', 'properties', 'bookings', 'expenses', 'documents', 'reports', 'targets']) if (l.indexOf(k.slice(0, 7)) !== -1) return GLYPH[k === 'properties' ? 'property' : k];
    if (l.indexOf('calendar') !== -1 || l.indexOf('occupancy') !== -1) return GLYPH.bookings;
    return GLYPH.overview;
  };
  const GROUPS = ['Pages', 'Imports', 'Properties'];
  const groupOf = (i) => (i.hint === 'Property' ? 'Properties' : (i.hint === 'Imports' ? 'Imports' : 'Pages'));
  const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function render() {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(i => !q || i.label.toLowerCase().includes(q) || i.hint.toLowerCase().includes(q)).slice(0, 14)
      .map((i, n) => ({ i, n })).sort((a, b) => (GROUPS.indexOf(groupOf(a.i)) - GROUPS.indexOf(groupOf(b.i))) || (a.n - b.n)).map(x => x.i);
    cursor = Math.min(cursor, Math.max(0, shown.length - 1));
    let last = '';
    list.innerHTML = shown.length ? shown.map((i, k) => {
      const g = groupOf(i);
      const head = g !== last ? '<div class="palette-group" role="presentation">' + g + '</div>' : '';
      last = g;
      return head + '<a href="' + esc(i.url) + '" class="palette-item' + (k === cursor ? ' sel' : '') + '" data-k="' + k + '">' + glyphFor(i) +
        '<span class="pi-label">' + esc(i.label) + '</span><span class="pi-hint">' + esc(i.hint === 'Property' || i.hint === 'Page' ? '' : i.hint) + '</span></a>';
    }).join('') : '<div class="palette-empty">Nothing matches “' + esc(input.value) + '”.</div>';
    const sel = list.querySelector('.palette-item.sel');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  }
  list.addEventListener('mousemove', (e) => {
    const a = e.target.closest('.palette-item');
    if (a && +a.dataset.k !== cursor) { list.querySelector('.palette-item.sel')?.classList.remove('sel'); cursor = +a.dataset.k; a.classList.add('sel'); }
  });
  function open() { box.hidden = false; input.value = ''; cursor = 0; render(); input.focus(); }
  function close() { box.hidden = true; }
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box.hidden ? open() : close(); return; }
    if (e.key === '/' && !typing && box.hidden) { e.preventDefault(); open(); return; }
    if (box.hidden) return;
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowDown') { e.preventDefault(); cursor = Math.min(cursor + 1, shown.length - 1); render(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); cursor = Math.max(cursor - 1, 0); render(); }
    if (e.key === 'Enter' && shown[cursor]) { start(); location.href = shown[cursor].url; }
  });
  input.addEventListener('input', () => { cursor = 0; render(); });
  box.addEventListener('click', (e) => { if (e.target === box) close(); });
  const trigger = document.getElementById('palette-open');
  if (trigger) trigger.addEventListener('click', open);
})();

/* ---------- small shared behaviours ---------- */
(function () {
  // "All metrics": reveals the secondary rows of a comparison table
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-toggle-rows]');
    if (!b) return;
    const t = document.getElementById(b.dataset.toggleRows);
    if (!t) return;
    const on = t.classList.toggle('show-all');
    b.setAttribute('aria-expanded', on ? 'true' : 'false');
    b.textContent = on ? 'Fewer metrics' : 'All metrics';
  });
  // a horizontal scroller that has more to show gets a fade at its edge (nav strip, tab bar, wide tables)
  const cue = (el) => {
    const f = () => el.classList.toggle('has-more', el.scrollWidth - el.clientWidth - el.scrollLeft > 6);
    f(); el.addEventListener('scroll', f, { passive: true }); window.addEventListener('resize', f);
  };
  const init = () => document.querySelectorAll('.tabs, .sidebar, .card.flush').forEach(cue);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

/* ---------- info controls (the ⓘ next to a metric): hover, keyboard focus, click and tap all show the same short definition ---------- */
(function () {
  let pop = null, pinned = null;
  const textOf = (el) => el.getAttribute('data-info') || el.getAttribute('aria-label') || el.getAttribute('title') || '';
  const prep = (el) => {
    if (el.hasAttribute('title')) { el.setAttribute('data-info', el.getAttribute('title')); el.removeAttribute('title'); }   // one tooltip, not the native one on top
    if (!el.hasAttribute('role')) el.setAttribute('role', 'button');
    if (!el.hasAttribute('tabindex')) el.tabIndex = 0;
    if (!el.hasAttribute('aria-expanded')) el.setAttribute('aria-expanded', 'false');
  };
  const hide = () => { if (pop) { pop.remove(); pop = null; } document.querySelectorAll('.info-dot[aria-expanded="true"]').forEach((d) => d.setAttribute('aria-expanded', 'false')); };
  const show = (el) => {
    hide();
    prep(el);
    pop = document.createElement('div');
    pop.className = 'info-pop';
    pop.setAttribute('role', 'tooltip');
    pop.textContent = textOf(el);
    document.body.appendChild(pop);
    el.setAttribute('aria-expanded', 'true');
    const r = el.getBoundingClientRect(), w = Math.min(280, window.innerWidth - 24);
    pop.style.width = w + 'px';
    pop.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left + r.width / 2 - w / 2)) + 'px';
    const below = r.bottom + 8, h = pop.offsetHeight;
    pop.style.top = (below + h > window.innerHeight - 8 && r.top - h - 8 > 8 ? r.top - h - 8 : below) + 'px';
  };
  const dot = (e) => e.target.closest && e.target.closest('.info-dot');
  document.addEventListener('mouseover', (e) => { const d = dot(e); if (d && !pinned) show(d); });
  document.addEventListener('mouseout', (e) => { if (dot(e) && !pinned) hide(); });
  document.addEventListener('focusin', (e) => { const d = dot(e); if (d && !pinned) show(d); });
  document.addEventListener('focusout', (e) => { if (dot(e) && !pinned) hide(); });
  document.addEventListener('click', (e) => {
    const d = dot(e);
    if (d) {
      e.preventDefault();                                   // an ⓘ inside a linked tile or header must not follow the link
      if (pinned === d) { pinned = null; hide(); } else { pinned = d; show(d); }
    } else if (pinned || pop) { pinned = null; hide(); }
  });
  document.addEventListener('keydown', (e) => {
    const d = dot(e);
    if (d && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); d.click(); }
    if (e.key === 'Escape' && (pinned || pop)) { pinned = null; hide(); }
  });
  window.addEventListener('scroll', () => { if (pop && !pinned) hide(); }, { passive: true });
})();

/* ---------- collapsible sidebar: remembered, tooltips when collapsed ---------- */
(function () {
  const root = document.documentElement, btn = document.getElementById('sb-toggle');
  const collapsed = () => root.dataset.sidebar === 'collapsed';
  const sync = () => {
    if (!btn) return;
    const c = collapsed();
    btn.setAttribute('aria-expanded', c ? 'false' : 'true');
    btn.setAttribute('aria-label', c ? 'Expand sidebar' : 'Collapse sidebar');
    btn.title = c ? 'Expand sidebar' : 'Collapse sidebar';
  };
  sync();
  if (btn) btn.addEventListener('click', () => {
    if (collapsed()) delete root.dataset.sidebar; else root.dataset.sidebar = 'collapsed';
    try { localStorage.setItem('dash_sidebar', collapsed() ? 'collapsed' : 'expanded'); } catch (e) {}
    sync(); hideTip();
    setTimeout(() => window.dispatchEvent(new Event('resize')), 230);       // charts re-measure after the content area changes width
  });
  let tip = null;
  function hideTip() { if (tip) { tip.remove(); tip = null; } }
  function showTip(el) {
    if (!collapsed() || window.innerWidth <= 860) return;
    hideTip();
    tip = document.createElement('div'); tip.className = 'sb-tip'; tip.setAttribute('role', 'tooltip'); tip.textContent = el.getAttribute('data-tip');
    document.body.appendChild(tip);
    const r = el.getBoundingClientRect();
    tip.style.left = (r.right + 10) + 'px'; tip.style.top = (r.top + (r.height - tip.offsetHeight) / 2) + 'px';
  }
  document.querySelectorAll('.sidebar [data-tip]').forEach((el) => {
    el.addEventListener('mouseenter', () => showTip(el)); el.addEventListener('mouseleave', hideTip);
    el.addEventListener('focus', () => { if (el.matches(':focus-visible')) showTip(el); }); el.addEventListener('blur', hideTip);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
})();
