/* Bid Estimator front end.
 *
 * Plain DOM, no framework and no build step: a local tool started with
 * `.venv/bin/python ui/run.py`, offline, no CDN.
 *
 * One page, top to bottom: status bar, takeoff + confirmation grid, project
 * details, results, export. State lives in `S`; the confirmation grid is the
 * single source of truth for what gets priced, and the server re-classifies
 * every line from its raw text on every request.
 */
'use strict';

const S = {
  lines: [],          // {row_id, item_no, description, unit, quantity, include,
                      //  family (override or ''), amount, quote, cls, issues,
                      //  confidence}
  parse: null,
  options: null,
  status: null,
  result: null,       // last /api/estimate response
  resultReq: null,    // the request that produced it
  market: 'municipal',
  bidders: { mode: 'auto', suggestion: null },
  seq: 0,
  stale: false,
};

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

function el(tag, attrs, children) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return n;
}

/* ------------------------------------------------------------- formatting */

const isNum = v => v !== null && v !== undefined && v !== '' && !isNaN(v);
const money = (v, nd) => !isNum(v) ? '—'
  : '$' + Number(v).toLocaleString('en-US',
      { minimumFractionDigits: nd || 0, maximumFractionDigits: nd || 0 });

function rate(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  const nd = a < 1000 ? 2 : 0;
  return '$' + Number(v).toLocaleString('en-US',
    { minimumFractionDigits: nd, maximumFractionDigits: nd });
}

function short$(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e6) return '$' + (v / 1e6).toFixed(a >= 1e7 ? 1 : 2) + 'M';
  if (a >= 1e3) return '$' + Math.round(v / 1e3).toLocaleString('en-US') + 'k';
  return money(v);
}

const qtyFmt = v => !isNum(v) ? '' :
  Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });
const pct = (v, nd) => !isNum(v) ? '—'
  : (100 * v).toFixed(nd === undefined ? 0 : nd) + '%';

function parseNum(s) {
  if (s === null || s === undefined) return null;
  const t = String(s).replace(/[$,\s]/g, '');
  if (!t || !/^-?\d*\.?\d+$/.test(t)) return null;
  const f = parseFloat(t);
  return isFinite(f) ? f : null;
}

/* ------------------------------------------------------------------- http */

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    if (ct.includes('json')) {
      try { const j = await r.json(); msg = j.detail || j.error || msg; } catch (e) { /* keep */ }
    }
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return ct.includes('json') ? r.json() : r.text();
}

const postJSON = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

function toast(msg, kind, ms) {
  const t = el('div', { class: 'toast ' + (kind || 'info'), text: msg });
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), ms || (kind === 'err' ? 9000 : 4200));
}

function notice(kind, title, body) {
  return el('div', { class: 'notice ' + kind }, [
    el('span', { class: 'ic', text: kind === 'severe' ? '✕'
      : kind === 'caution' ? '!' : kind === 'ok' ? '✓' : 'i' }),
    el('div', { class: 'body-txt' }, [
      title ? el('h4', { text: title }) : null,
      body ? (typeof body === 'object' ? body : el('div', { text: body })) : null,
    ]),
  ]);
}

async function withBusy(btn, fn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> working…';
  try { await fn(); }
  catch (e) { toast(String(e.message || e), 'err'); }
  finally { btn.disabled = false; btn.innerHTML = old; refreshEstimateButton(); }
}

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ============================================================ STATUS BAR */

/* ================================================================= THEME */

// System (the default) follows the OS live; Light / Dark are stored once the
// user picks one. Every storage access is guarded: blocked storage only
// means the choice is not remembered.
const THEME_KEY = 'bid-estimator-theme';
const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
let themeMemo = null;    // the choice when storage is blocked

function themeMode() {
  try {
    const m = window.localStorage.getItem(THEME_KEY);
    if (m === 'light' || m === 'dark') return m;
    return themeMemo || 'system';
  } catch (e) { return themeMemo || 'system'; }
}

function applyTheme(mode) {
  const dark = mode === 'dark' || (mode === 'system' && !!(darkQuery && darkQuery.matches));
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme-mode', mode);
  $$('#theme-toggle button').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.mode === mode));
    if (b.dataset.mode === 'system') {
      b.textContent = 'System (' + (darkQuery && darkQuery.matches ? 'dark' : 'light') + ')';
    }
  });
}

function setTheme(mode) {
  themeMemo = mode;
  try {
    if (mode === 'system') window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, mode);
  } catch (e) { /* not remembered; still applied */ }
  applyTheme(mode);
}

function initTheme() {
  $$('#theme-toggle button').forEach(b => b.addEventListener('click', () => setTheme(b.dataset.mode)));
  if (darkQuery) {
    const follow = () => { if (themeMode() === 'system') applyTheme('system'); };
    if (darkQuery.addEventListener) darkQuery.addEventListener('change', follow);
    else if (darkQuery.addListener) darkQuery.addListener(follow);
  }
  applyTheme(themeMode());
}

function renderStatusBar() {
  const st = S.status;
  const bar = $('#status-items');
  bar.innerHTML = '';
  if (!st) {
    bar.appendChild(el('span', { html: '<span class="spinner"></span> connecting…' }));
    return;
  }
  const eng = st.engine || {};
  const est = st.estimator || {};
  const data = st.data || {};
  const up = st.upstream || {};
  const item = (dot, label, value, title) => el('span', { class: 'sitem', title: title || '' }, [
    el('span', { class: 'dot ' + dot }),
    el('span', { class: 'slab', text: label }),
    document.createTextNode(value),
  ]);
  if (!st.ready) {
    bar.appendChild(el('span', { class: 'sitem' }, [
      el('span', { class: 'spinner' }),
      document.createTextNode(eng.error
        ? ' Estimator failed to load: ' + eng.error
        : ' Loading the production estimator (' + (eng.step || 'starting') + ')…'),
    ]));
  } else {
    bar.appendChild(item('ok', 'Data', 'built ' + (data.master_panel_mtime || '?')
      + ' · latest letting ' + (data.latest_letting || '?'),
      'data/clean/master_panel.csv modified ' + data.master_panel_mtime
      + '; calibration written ' + data.calibration_mtime + '. Comparables pools: '
      + Object.entries(data.pool_lettings || {}).map(([k, v]) => v + ' ' + k).join(', ')
      + ' public lettings. ' + (data.train_scope || '')));
    bar.appendChild(item('ok', 'Estimator', 'estimate_low_bid.py md5 ' + (est.md5_short || '?')
      + ' · ' + (est.rule || ''),
      'src/estimate_low_bid.py md5 ' + est.md5 + '\n'
      + Object.entries(est.md5_all || {}).map(([k, v]) => k + ' ' + v).join('\n')));
    bar.appendChild(item('dim', 'Loaded', (eng.load_s + eng.history_s).toFixed(1) + ' s · '
      + (eng.rss_mb || '?') + ' MB',
      'Reference tables and comparables pools ' + eng.load_s + ' s; public bid totals, line prices and bidder-count history '
      + eng.history_s + ' s. Memory now ' + eng.rss_mb + ' MB, peak ' + eng.peak_rss_mb + ' MB.'));
  }
  bar.appendChild(item(up.parser_available ? 'ok' : 'bad', 'Parser',
    up.parser_available ? 'linked' + (up.parser_version ? ' (' + up.parser_version.split('-').slice(0, 3).join('-') + ')' : '')
      : 'UNAVAILABLE',
    up.parser_available ? 'src/parse_bidtabs.py ' + up.parser_version
      + '; size buckets from ' + up.size_bucket_source
      : 'src/parse_bidtabs.py did not import: ' + up.parser_error));
  const ocr = st.ocr || {};
  bar.appendChild(item(ocr.real ? 'ok' : (ocr.mock ? 'warn' : 'dim'), 'OCR',
    ocr.real ? 'available' : (ocr.mock ? 'MOCK only' : 'not installed')));
  bar.appendChild(el('span', { class: 'dim local', text: 'local only · nothing leaves this machine' }));
}

async function pollStatus() {
  try {
    S.status = await api('/api/status');
  } catch (e) {
    S.status = null;
  }
  renderStatusBar();
  if (S.status && S.status.ready) {
    if (!S.options) await loadOptions();
    return;
  }
  setTimeout(pollStatus, S.status && S.status.engine && S.status.engine.error ? 15000 : 800);
}

async function loadOptions() {
  try {
    S.options = await api('/api/options');
  } catch (e) {
    toast('Could not load options: ' + e.message, 'err');
    return;
  }
  const bd = $('#in-biddate');
  if (!bd.value) bd.value = S.options.today;
  fillMarketLists();
  renderDiscountPreview();
  if (!S.status.upstream.parser_available) {
    toast('src/parse_bidtabs.py did not import; classification runs on degraded '
      + 'fallbacks.', 'err', 14000);
  }
  if (S.lines.length) { await classifyAll(); }
  refreshEstimateButton();
}

/* ================================================================ INPUT */

const SAMPLE = [
  '101\tMobilization\tLS\t1',
  '102\tTemporary Erosion Control\tLS\t1',
  '103\tUnclassified Channel Excavation\tCY\t4,200',
  '104\tGrouted Rock Riprap, 18 inch\tSY\t2,600',
  '105\tGabion Mattress, 12 inch\tSY\t880',
  '106\t8 inch PVC SDR-26 Sanitary Sewer Pipe\tLF\t1,450',
  '107\tBlock Sod, Bermuda\tSY\t9,500',
  '108\tTrench Safety System\tLF\t1,450',
  '109\tSubmersible Pump, 15 HP\tEA\t2',
  '110\tOwner\'s Contingency Allowance\tLS\t1',
].join('\n');

function initInput() {
  $$('#input-tabs button').forEach(b => b.addEventListener('click', () => {
    $$('#input-tabs button').forEach(x => x.setAttribute('aria-selected', String(x === b)));
    $$('#sec-input .tabpane').forEach(p =>
      p.classList.toggle('active', p.id === 'pane-' + b.dataset.pane));
  }));
  $('#btn-sample').addEventListener('click', () => { $('#paste-text').value = SAMPLE; });
  $('#btn-parse-text').addEventListener('click', async () => {
    const text = $('#paste-text').value;
    if (!text.trim()) { toast('Nothing to read — paste your items first.', 'err'); return; }
    await withBusy($('#btn-parse-text'), async () => {
      const p = await postJSON('/api/takeoff/text', { text });
      await acceptParse(p);
    });
  });
  const dz = $('#dropzone'), fi = $('#file-input');
  dz.addEventListener('click', () => fi.click());
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('over');
    if (e.dataTransfer.files[0]) uploadTakeoff(e.dataTransfer.files[0]);
  });
  fi.addEventListener('change', () => { if (fi.files[0]) uploadTakeoff(fi.files[0]); fi.value = ''; });

  $('#btn-add-row').addEventListener('click', () => {
    S.lines.push(newLine({}));
    renderGrid(); markStale();
    const rows = $$('#review-body tr[data-row]');
    const last = rows[rows.length - 1];
    if (last) $('input[data-col=description]', last).focus();
  });

  // Enter / arrows move down the same column, like a spreadsheet.
  $('#review-table').addEventListener('keydown', e => {
    const t = e.target;
    if (!t.classList || !t.classList.contains('cellinput')) return;
    if (e.key !== 'Enter' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const col = t.dataset.col;
    const i = parseInt(t.dataset.i, 10);
    const next = i + (e.key === 'ArrowUp' ? -1 : 1);
    t.blur();
    const target = $('#review-body input[data-col="' + col + '"][data-i="' + next + '"]');
    if (target) { target.focus(); target.select && target.select(); }
  });

  // bid tabulation (staging only)
  const bz = $('#bidtab-drop'), bi = $('#bidtab-input');
  bz.addEventListener('click', () => bi.click());
  bz.addEventListener('dragover', e => { e.preventDefault(); bz.classList.add('over'); });
  bz.addEventListener('dragleave', () => bz.classList.remove('over'));
  bz.addEventListener('drop', e => {
    e.preventDefault(); bz.classList.remove('over');
    if (e.dataTransfer.files[0]) uploadBidtab(e.dataTransfer.files[0]);
  });
  bi.addEventListener('change', () => { if (bi.files[0]) uploadBidtab(bi.files[0]); bi.value = ''; });
}

function newLine(l) {
  return {
    row_id: l.row_id || ('r' + (++S.seq)),
    item_no: l.item_no || '',
    description: l.description || '',
    unit: l.unit || '',
    quantity: isNum(l.quantity) ? Number(l.quantity) : null,
    include: true,
    family: '',
    amount: null,
    amount_src: null,     // 'file' | 'user'
    file_amount: isNum(l.amount) ? Number(l.amount) : null,
    file_unit_price: isNum(l.unit_price) ? Number(l.unit_price) : null,
    quote: null,
    cls: null,
    issues: l.issues || [],
    confidence: l.confidence || 'ok',
  };
}

async function uploadTakeoff(file) {
  const st = $('#file-status');
  st.innerHTML = '';
  st.appendChild(notice('info', null, 'Reading ' + file.name + '…'));
  const fd = new FormData();
  fd.append('file', file);
  try {
    const p = await api('/api/takeoff/file', { method: 'POST', body: fd });
    st.innerHTML = '';
    await acceptParse(p);
  } catch (e) {
    st.innerHTML = '';
    st.appendChild(notice('severe', 'Could not read ' + file.name, String(e.message || e)));
  }
}

async function acceptParse(p) {
  S.parse = p;
  S.seq = 0;
  S.lines = (p.lines || []).map(l => newLine(Object.assign({}, l, { row_id: null })));
  S.bidders.mode = 'auto';
  clearResult();
  renderParseNotes();
  renderGrid();
  renderOwnerSet();
  if (!S.lines.length) {
    toast('No line items could be read — see the notes.', 'err');
    return;
  }
  toast('Read ' + S.lines.length + ' line item(s). Check the classification column.', 'ok');
  await classifyAll();
  document.getElementById('sec-input').scrollIntoView({ behavior: 'smooth' });
}

function renderParseNotes() {
  const host = $('#parse-notes');
  host.innerHTML = '';
  const p = S.parse;
  if (!p) return;
  if (p.mock) {
    host.appendChild(notice('severe', 'These items are MOCK data, not read from your file',
      'The OCR engine (src/ocr_extract.py) has not been delivered, so the image reader '
      + 'returned a stand-in. Nothing here may be priced. Paste the items as text instead.'));
  }
  if (p.notes && p.notes.length) {
    host.appendChild(notice('info', 'How "' + (p.source || 'input') + '" was read ('
      + (p.method || '?') + ')',
      el('ul', { class: 'tight' }, p.notes.map(n => el('li', { text: n })))));
  }
  const fold = $('#skipped-fold');
  fold.innerHTML = '';
  fold.hidden = !p.n_skipped;
  if (p.n_skipped) {
    fold.appendChild(el('summary', { text: p.n_skipped
      + ' row(s) were seen and not treated as pay items — show them' }));
    fold.appendChild(el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, [el('th', { text: 'Row' }), el('th', { text: 'Why' }),
        el('th', { text: 'Content' })])),
      el('tbody', {}, (p.skipped || []).map(s => el('tr', {}, [
        el('td', { text: s.row }), el('td', { text: s.reason }),
        el('td', { class: 'dim', text: s.raw })]))),
    ]));
  }
}

/* -------------------------------------------------------- classification */

function linePayload(l) {
  return { row_id: l.row_id, description: l.description, unit: l.unit, family: l.family || '',
    quantity: l.quantity, file_amount: l.file_amount, file_unit_price: l.file_unit_price };
}

// An uploaded takeoff's amount / unit-price column is used for owner-set
// allowance lines only (the server decides: cls.import_amount), and never
// over an amount the user typed.
function adoptImportAmount(l) {
  const c = l.cls;
  if (!c || l.amount_src === 'user') return;
  if (c.owner_set_like && c.import_amount > 0) {
    l.amount = c.import_amount;
    l.amount_src = 'file';
  } else if (l.amount_src === 'file') {
    l.amount = null;
    l.amount_src = null;
  }
}

async function classifyAll() {
  if (!S.options || !S.lines.length) { renderGrid(); return; }
  try {
    const r = await postJSON('/api/classify', {
      market: S.market, lines: S.lines.map(linePayload) });
    const by = {};
    (r.lines || []).forEach(c => { by[c.row_id] = c; });
    S.lines.forEach(l => { if (by[l.row_id]) { l.cls = by[l.row_id]; adoptImportAmount(l); } });
  } catch (e) {
    toast('Classification failed: ' + e.message, 'err');
  }
  renderGrid();
  renderQuotes();
  renderOwnerSet();
  suggestBidders();
}

const classifyOne = debounce(async (l) => {
  if (!S.options) return;
  try {
    const r = await postJSON('/api/classify', { market: S.market, lines: [linePayload(l)] });
    if (r.lines && r.lines[0]) { l.cls = r.lines[0]; adoptImportAmount(l); }
  } catch (e) { toast('Classification failed: ' + e.message, 'err'); }
  renderGrid();
  renderQuotes();
  renderOwnerSet();
  suggestBidders();
}, 350);

/* ---------------------------------------------------------------- the grid */

// The grid is rebuilt wholesale on every change, and classification is
// async, so a rebuild can land while the estimator types somewhere else. The
// focused cell's own text and caret are carried across every rebuild.
function gridFocusState() {
  const a = document.activeElement;
  if (!a || !a.dataset || !a.dataset.col) return null;
  const tr = a.closest && a.closest('tr[data-row]');
  if (!tr) return null;
  let sel = null;
  try { sel = [a.selectionStart, a.selectionEnd]; } catch (e) { /* not text */ }
  return { row: tr.dataset.row, col: a.dataset.col, value: a.value, sel };
}

function restoreGridFocus(st) {
  if (!st) return;
  const cell = document.querySelector('tr[data-row="' + CSS.escape(st.row)
    + '"] input[data-col="' + st.col + '"]');
  if (!cell) return;
  if (cell.value !== st.value) cell.value = st.value;
  cell.focus();
  if (st.sel) { try { cell.setSelectionRange(st.sel[0], st.sel[1]); } catch (e) { /* ok */ } }
}

function renderGrid() {
  const focus = gridFocusState();
  const has = S.lines.length > 0;
  $('#grid-head').hidden = !has;
  $('#grid-wrap').hidden = !has;
  const body = $('#review-body');
  body.innerHTML = '';
  S.lines.forEach((l, i) => body.appendChild(gridRow(l, i)));
  const n = S.lines.filter(l => l.include).length;
  const noq = S.lines.filter(l => l.include && !(l.quantity > 0)).length;
  const unc = S.lines.filter(l => l.include && l.cls && l.cls.coverage_class).length;
  $('#review-count').textContent = n + ' of ' + S.lines.length + ' lines included'
    + (noq ? ' · ' + noq + ' with no quantity' : '')
    + (unc ? ' · ' + unc + ' not covered' : '');
  restoreGridFocus(focus);
  refreshEstimateButton();
}

function clsCell(l) {
  const c = l.cls;
  if (!c) return el('span', { class: 'dim', text: l.description ? 'classifying…' : '—' });
  const badges = [];
  if (c.coverage_class) badges.push(el('span', { class: 'badge unc',
    title: 'Not covered: specialty item with almost no comparable bid history. It is not '
      + 'priced and not in P50/P80 — enter a vendor quote under Project details.',
    text: 'not covered: ' + c.coverage_label }));
  if (c.lowconf_class) badges.push(el('span', { class: 'badge lowc',
    title: 'Low confidence: lump sums like this vary widely between bidders; see the '
      + 'low-confidence panel in the results.', text: c.lowconf_label }));
  if (c.owner_set_like && !(l.amount > 0)) badges.push(el('span', { class: 'badge missing',
    title: 'Owner-set money (allowance / contingency / force account): enter the amount from '
      + 'the bid form under Project details \u2192 Owner-set amounts (or in "Owner-set $"). '
      + 'Until then it is priced from history, which is not meaningful for an allowance.',
    text: 'allowance amount missing' }));
  if (l.amount > 0) badges.push(el('span', { class: 'badge allow',
    title: 'Owner-set money, taken at face value and kept out of the competition discount'
      + (l.amount_src === 'file' ? ' (amount read from the uploaded file)' : ''),
    text: 'owner-set ' + money(l.amount) + (l.amount_src === 'file' ? ' (file)' : '') }));
  if (c.family === 'other' || c.family === 'needs_review' || !c.family) badges.push(
    el('span', { class: 'badge uncl', title: 'No item family matched: priced from item '
      + 'words only, if at all. Use Change to pick a family.', text: 'unclassified' }));
  (c.issues || []).filter(x => /unit/.test(x)).forEach(x =>
    badges.push(el('span', { class: 'badge no', text: x })));
  return el('div', { class: 'clscell' }, [
    el('div', {}, [
      el('span', { class: 'fam' + (c.family_overridden ? ' over' : ''),
        title: 'family ' + c.family + (c.family_overridden
          ? ' (your choice; the parser said ' + c.family_parser + ')' : ' (parser)')
          + '\nunit ' + c.unit + (c.size_bucket ? '\nsize bucket ' + c.size_bucket
          + ' (size ' + c.size_in + ')' : '') + '\ndesc_norm: ' + c.desc_norm,
        text: c.family_label }),
      c.family_overridden ? el('span', { class: 'badge info', text: 'your pick' }) : null,
      c.size_bucket ? el('span', { class: 'size', text: c.size_bucket }) : null,
      el('span', { class: 'unitn', text: c.unit || 'no unit' }),
    ]),
    badges.length ? el('div', { class: 'badges' }, badges) : null,
  ]);
}

function gridRow(l, i) {
  const tr = el('tr', { class: l.include ? '' : 'excluded', 'data-row': l.row_id });
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    type: 'checkbox', checked: l.include, title: 'Include this line in the estimate',
    onchange: e => { l.include = e.target.checked; renderGrid(); renderQuotes();
      renderOwnerSet(); markStale(); suggestBidders(); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput', type: 'text', value: l.item_no, 'data-col': 'item_no', 'data-i': i,
    oninput: e => { l.item_no = e.target.value; markStale(); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput', type: 'text', value: l.description,
    'data-col': 'description', 'data-i': i,
    oninput: e => { l.description = e.target.value; markStale(); classifyOne(l); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput' + (l.unit ? '' : ' flag'), type: 'text', value: l.unit,
    'data-col': 'unit', 'data-i': i, placeholder: 'unit',
    oninput: e => { l.unit = e.target.value; markStale(); classifyOne(l); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput num' + (l.quantity > 0 ? '' : ' flag'), type: 'text',
    value: qtyFmt(l.quantity), 'data-col': 'quantity', 'data-i': i, placeholder: 'required',
    onchange: e => {
      l.quantity = parseNum(e.target.value);
      e.target.value = qtyFmt(l.quantity);
      e.target.classList.toggle('flag', !(l.quantity > 0));
      markStale(); renderGrid(); suggestBidders();
    },
  })));
  tr.appendChild(el('td', {}, clsCell(l)));
  const allow = l.cls && l.cls.owner_set_like;
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput num' + (allow && !(l.amount > 0) ? ' hintfill' : ''), type: 'text',
    value: l.amount > 0 ? qtyFmt(l.amount) : '', 'data-col': 'amount', 'data-i': i,
    placeholder: allow ? 'owner’s $' : '',
    title: 'Owner-set money only: the owner’s amount, taken at face value and kept out '
      + 'of the competition discount. Leave blank for a line bidders price.',
    onchange: e => {
      const v = parseNum(e.target.value);
      l.amount = v > 0 ? v : null;
      l.amount_src = 'user';
      e.target.value = l.amount ? qtyFmt(l.amount) : '';
      markStale(); renderGrid(); renderOwnerSet(); suggestBidders();
    },
  })));
  tr.appendChild(el('td', {}, el('div', { class: 'row-actions nowrap' }, [
    el('button', { class: 'btn ghost sm', text: 'Change',
      title: 'Correct the family, or look at earlier public line items like this one',
      onclick: () => openPicker(l) }),
    el('button', { class: 'btn ghost sm danger', text: '✕', title: 'Delete row',
      onclick: () => { S.lines = S.lines.filter(x => x !== l); renderGrid(); renderQuotes();
        renderOwnerSet(); markStale(); suggestBidders(); } }),
  ])));
  return tr;
}

/* --------------------------------------------- classification picker modal */

function openPicker(l) {
  const host = $('#modal-host');
  const fams = (S.options && S.options.families) || [];
  const cur = l.cls ? l.cls.family : '';
  const famSel = el('select', {}, [el('option', { value: '', text:
    '— the parser’s family' + (l.cls ? ' (' + l.cls.family_parser + ')' : '') + ' —' })]
    .concat(fams.map(f => el('option', { value: f.id, text: f.label + '  ·  ' + f.id,
      selected: l.family && f.id === l.family }))));
  const list = el('div', {});
  const qinput = el('input', { type: 'text', value: l.description || '',
    placeholder: 'search earlier public line items…' });

  function apply(family, unit) {
    l.family = family || '';
    if (unit) l.unit = unit;
    close();
    markStale();
    classifyOne(l);
    toast('Row ' + (l.item_no || '') + ': family set to '
      + (family || 'the parser’s') + (unit ? ', unit ' + unit : '') + '.', 'ok');
  }

  function paint(cands) {
    list.innerHTML = '';
    if (!cands.length) {
      list.appendChild(el('div', { class: 'muted-note', text: 'Nothing matched. Try the '
        + 'material and size ("18 inch rock riprap") rather than a project-specific label.' }));
      return;
    }
    cands.forEach(c => list.appendChild(el('div', { class: 'pick' + (c.item_family === cur ? ' sel' : '') }, [
      el('span', { class: 'lab' }, [
        el('span', { class: 't', text: c.label || '(unnamed)' }),
        el('span', { class: 's', text: c.family_label + ' (' + c.item_family + ') · '
          + c.unit + ' · ' + c.detail
          + (c.median_price ? ' · median ' + rate(c.median_price) + '/' + c.unit : '') }),
      ]),
      el('span', { class: 'badge ' + (c.n_projects >= 5 ? 'ok' : c.n_projects >= 3 ? 'thin' : 'no'),
        text: c.n_projects >= 5 ? 'usable' : c.n_projects >= 3 ? 'thin' : 'too thin' }),
      el('button', { class: 'btn sm', text: 'Use family',
        title: 'Set this line’s family to ' + c.item_family,
        onclick: () => apply(c.item_family, '') }),
      c.unit && c.unit !== (l.cls && l.cls.unit) ? el('button', { class: 'btn sm',
        text: 'Use family + ' + c.unit, onclick: () => apply(c.item_family, c.unit) }) : null,
    ])));
  }

  let timer = null;
  async function search() {
    const q = qinput.value.trim();
    if (!q) { paint([]); return; }
    try {
      const r = await api('/api/history/search?q=' + encodeURIComponent(q)
        + '&unit=' + encodeURIComponent((l.cls && l.cls.unit) || l.unit || '') + '&limit=20');
      paint(r.candidates || []);
    } catch (e) { list.innerHTML = ''; list.appendChild(notice('caution', 'Search failed', e.message)); }
  }
  qinput.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 260); });

  function close() { host.innerHTML = ''; document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  const c = l.cls || {};
  const back = el('div', { class: 'modal-back', onclick: e => { if (e.target === back) close(); } },
    el('div', { class: 'modal' }, [
      el('header', {}, [
        el('h3', { text: 'Classification: ' + (l.description || '(blank)') }),
        el('button', { class: 'btn sm close', text: 'Close', onclick: close }),
      ]),
      el('div', { class: 'mbody' }, [
        el('dl', { class: 'kv', style: 'margin-bottom:11px' }, [
          el('dt', { text: 'Parser’s family' }), el('dd', { text: c.family_parser || '—' }),
          el('dt', { text: 'Unit' }), el('dd', { text: c.unit || '—' }),
          el('dt', { text: 'Size' }), el('dd', { text: c.size_bucket
            ? c.size_bucket + ' (' + c.size_in + ')' : 'none parsed' }),
          el('dt', { text: 'Normalised text' }), el('dd', { text: c.desc_norm || '' }),
        ]),
        el('label', { class: 'field' }, [
          el('span', { class: 'lab', text: 'Family' }),
          el('div', { class: 'inline' }, [famSel,
            el('button', { class: 'btn sm primary', text: 'Apply',
              onclick: () => apply(famSel.value, '') })]),
          el('span', { class: 'sub', text: 'The family keys the reference cells after the '
            + 'item-word match. Change it only when the parser is wrong; the unit is '
            + 'edited in the grid.' }),
        ]),
        el('label', { class: 'field' }, [
          el('span', { class: 'lab', text: 'Search earlier public line items' }),
          qinput,
          el('span', { class: 'sub', text: 'City bid tabs (DFW) and the TxDOT price book, '
            + 'public lettings only. "Use family" adopts that item’s family.' }),
        ]),
        list,
      ]),
    ]));
  host.appendChild(back);
  qinput.focus();
  qinput.select();
  search();
}

/* ======================================================= PROJECT DETAILS */

function initProject() {
  $$('#in-market button').forEach(b => b.addEventListener('click', async () => {
    if (S.market === b.dataset.v) return;
    S.market = b.dataset.v;
    $$('#in-market button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
    fillMarketLists();
    renderDiscountPreview();
    markStale();
    await classifyAll();
  }));
  $('#in-owner').addEventListener('change', () => { markStale(); });
  $('#in-worktype').addEventListener('change', () => { markStale(); suggestBidders(); });
  $('#in-biddate').addEventListener('change', () => { markStale(); suggestBidders(); });
  $('#in-bidders').addEventListener('input', () => {
    const v = parseInt($('#in-bidders').value, 10);
    S.bidders.mode = v > 0 ? 'user' : 'auto';
    renderBiddersHint();
    renderDiscountPreview();
    markStale();
    refreshEstimateButton();
  });
  $('#btn-bidders-reset').addEventListener('click', () => {
    S.bidders.mode = 'auto';
    if (S.bidders.suggestion) $('#in-bidders').value = S.bidders.suggestion.n;
    renderBiddersHint();
    renderDiscountPreview();
    markStale();
  });
  $('#btn-estimate').addEventListener('click', () => withBusy($('#btn-estimate'), runEstimate));
  $('#btn-reestimate').addEventListener('click', () => withBusy($('#btn-reestimate'), runEstimate));
}

function fillMarketLists() {
  const o = S.options;
  if (!o) return;
  const fill = (sel, rows, keep) => {
    const cur = keep ? sel.value : '';
    sel.innerHTML = '';
    sel.appendChild(el('option', { value: '', text: '— not specified —' }));
    rows.forEach(r => sel.appendChild(el('option', { value: r.id,
      text: r.label + '  ·  ' + r.lettings + ' letting' + (r.lettings === 1 ? '' : 's')
        + ' on record' })));
    if (cur && rows.some(r => r.id === cur)) sel.value = cur;
  };
  fill($('#in-owner'), (o.owners || {})[S.market] || [], true);
  fill($('#in-worktype'), (o.work_types || {})[S.market] || [], true);
}

function biddersValue() {
  const v = parseInt($('#in-bidders').value, 10);
  return v > 0 ? v : null;
}

function renderDiscountPreview() {
  const box = $('#discount-preview');
  box.innerHTML = '';
  const o = S.options;
  const n = biddersValue();
  if (!o || !n) return;
  const d = (o.discount || {})[S.market];
  if (!d || !d.rows.length) return;
  let row = null, kind = 'ok', msg;
  if (n <= 1) {
    kind = 'caution';
    msg = 'One bid is not a competition: no discount is applied (f = 1.00).';
  } else {
    row = d.rows.reduce((a, b) => Math.abs(b.n_bidders - n) < Math.abs(a.n_bidders - n) ? b : a);
    if (Math.abs(row.n_bidders - n) > (o.max_bin_distance || 2)) {
      kind = 'severe';
      msg = 'No discount bin within ' + (o.max_bin_distance || 2) + ' of ' + n
        + ' bidders: the estimator refuses to extrapolate and gives no estimate.';
    } else {
      msg = 'f(' + n + ') = ×' + row.f.toFixed(3) + ' on the median bid · '
        + d.table + ' table, ' + row.n_bidders + '-bidder bin, ' + row.lettings
        + ' lettings' + (row.n_bidders !== n ? ' (nearest bin)' : '') + '.'
        + (S.market === 'txdot' ? ' TxDOT may use the size-aware D1 discount instead.' : '');
    }
  }
  box.appendChild(el('div', { class: 'disc ' + kind, text: msg }));
}

const suggestBidders = debounce(async () => {
  if (!S.options || !S.lines.some(l => l.include && l.description)) return;
  try {
    const sug = await postJSON('/api/bidders/suggest', estimateRequest('auto'));
    S.bidders.suggestion = sug;
    if (S.bidders.mode === 'auto') $('#in-bidders').value = sug.n;
  } catch (e) {
    S.bidders.suggestion = null;
  }
  renderBiddersHint();
  renderDiscountPreview();
  refreshEstimateButton();
}, 450);

function renderBiddersHint() {
  const h = $('#bidders-hint');
  const s = S.bidders.suggestion;
  $('#btn-bidders-reset').hidden = !(S.bidders.mode === 'user' && s);
  if (!s) {
    h.textContent = 'Prefilled from earlier lettings once the takeoff is read. Edit it if you know better.';
    return;
  }
  const src = s.cell === 'default' ? 'no history cell had 10 earlier lettings, so the rule’s default of 4'
    : 'median of ' + s.history_lettings + ' earlier ' + s.cell_label + ', bid before ' + s.before;
  h.innerHTML = '';
  h.appendChild(el('span', { text: (S.bidders.mode === 'user' ? 'You entered '
    + biddersValue() + '. The suggestion is ' + s.n + ': ' : 'Suggested ' + s.n + ': ')
    + src + ' (rule N1). ' }));
  h.appendChild(el('span', { class: 'help', text: '?', title: 'Rule N1 (registered '
    + '2026-11-20): the median bidder count of earlier lettings of the same market × '
    + 'work type × size class (this schedule’s own model total, '
    + short$(s.size_total) + ' → ' + s.size_class + '); fallbacks: market × size, '
    + 'market, all markets (10 lettings each), else 4. ' + (s.note || '') }));
}

function renderQuotes() {
  const box = $('#quotes-box');
  box.innerHTML = '';
  const unc = S.lines.filter(l => l.include && l.cls && l.cls.coverage_class);
  if (!S.lines.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'Read a takeoff first.' }));
    return;
  }
  if (!unc.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'No not-covered lines in this '
      + 'takeoff. Pumps, generators and electrical gear, prefab or pedestrian bridges, tanks, '
      + 'specialty buildings, synthetic turf / play equipment and meter vaults would be '
      + 'listed here for a vendor quote.' }));
    return;
  }
  box.appendChild(el('div', { class: 'muted-note', style: 'margin-bottom:6px', text:
    'These lines have almost no comparable bid history. They are not priced and not in '
    + 'P50 / P80. Enter a vendor quote for each to get a complete total.' }));
  unc.forEach(l => box.appendChild(el('div', { class: 'qrow' }, [
    el('div', { class: 'qdesc' }, [
      el('div', { text: (l.item_no ? l.item_no + '  ' : '') + l.description }),
      el('div', { class: 'dim', text: l.cls.coverage_label + ' · ' + qtyFmt(l.quantity)
        + ' ' + (l.cls.unit || '') }),
    ]),
    el('input', { type: 'text', class: 'num', placeholder: 'quote $',
      value: l.quote > 0 ? qtyFmt(l.quote) : '',
      onchange: e => {
        const v = parseNum(e.target.value);
        l.quote = v > 0 ? v : null;
        e.target.value = l.quote ? qtyFmt(l.quote) : '';
        markStale();
      } }),
  ])));
}

function renderOwnerSet() {
  const box = $('#ownerset-box');
  if (!box) return;
  box.innerHTML = '';
  const al = S.lines.filter(l => l.include && l.cls && l.cls.owner_set_like);
  if (!S.lines.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'Read a takeoff first.' }));
    return;
  }
  if (!al.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'No allowance, contingency or '
      + 'force-account line found. Any other owner-set line can take its amount in the '
      + 'grid\u2019s "Owner-set $" column.' }));
    return;
  }
  box.appendChild(el('div', { class: 'muted-note', style: 'margin-bottom:6px', text:
    'The owner writes these amounts into the bid form and every bidder copies them. Enter '
    + 'each from the bid form: it goes in at face value, outside the competition discount. '
    + 'A historical price is not meaningful for an allowance.' }));
  al.forEach(l => box.appendChild(el('div', { class: 'qrow owner' + (l.amount > 0 ? '' : ' missing') }, [
    el('div', { class: 'qdesc' }, [
      el('div', { text: (l.item_no ? l.item_no + '  ' : '') + l.description }),
      el('div', { class: 'dim', text: qtyFmt(l.quantity) + ' ' + (l.cls.unit || '')
        + (l.amount > 0 ? (l.amount_src === 'file' ? ' \u00b7 from the uploaded file' : '')
          : ' \u00b7 amount missing') }),
    ]),
    el('input', { type: 'text', class: 'num', placeholder: 'amount $',
      value: l.amount > 0 ? qtyFmt(l.amount) : '',
      onchange: e => {
        const v = parseNum(e.target.value);
        l.amount = v > 0 ? v : null;
        l.amount_src = 'user';
        e.target.value = l.amount ? qtyFmt(l.amount) : '';
        markStale(); renderGrid(); renderOwnerSet(); suggestBidders();
      } }),
  ])));
}

function estimateRequest(n) {
  return {
    market: S.market,
    owner: $('#in-owner').value,
    work_type: $('#in-worktype').value,
    bid_date: $('#in-biddate').value || null,
    n_bidders: n,
    items: S.lines.filter(l => l.include).map(l => ({
      row_id: l.row_id, item_no: l.item_no, description: l.description, unit: l.unit,
      quantity: l.quantity, family: l.family || '', amount: l.amount, quote: l.quote,
    })),
  };
}

function refreshEstimateButton() {
  const b = $('#btn-estimate');
  const n = S.lines.filter(l => l.include).length;
  const ready = !!(S.status && S.status.ready && S.options);
  b.disabled = !(ready && n);
  $('#estimate-hint').textContent = !ready ? 'Waiting for the estimator to load…'
    : !n ? 'Read a takeoff first.' : '';
}

function markStale() {
  if (!S.result) return;
  S.stale = true;
  $('#stale-banner').hidden = false;
}

/* ================================================================= RESULTS */

async function runEstimate() {
  if (S.parse && S.parse.mock) {
    toast('These line items are mock OCR output and will not be priced.', 'err', 9000);
    return;
  }
  const req = estimateRequest(S.bidders.mode === 'user' && biddersValue() ? biddersValue() : 'auto');
  const r = await postJSON('/api/estimate', req);
  S.result = r;
  S.resultReq = req;
  S.stale = false;
  $('#stale-banner').hidden = true;
  if (S.bidders.mode === 'auto') {
    $('#in-bidders').value = r.competition.n_bidders;
    if (r.competition.suggestion) S.bidders.suggestion = r.competition.suggestion;
    renderBiddersHint();
    renderDiscountPreview();
  }
  renderResult();
  $('#sec-results').hidden = false;
  $('#sec-export').hidden = false;
  $('#jump-results').classList.remove('off');
  $('#jump-export').classList.remove('off');
  document.getElementById('sec-results').scrollIntoView({ behavior: 'smooth' });
}

function clearResult() {
  S.result = null;
  S.stale = false;
  $('#sec-results').hidden = true;
  $('#sec-export').hidden = true;
  $('#stale-banner').hidden = true;
  $('#jump-results').classList.add('off');
  $('#jump-export').classList.add('off');
}

function renderResult() {
  const r = S.result;
  if (!r) return;
  const i = r.inputs;
  $('#result-sub').textContent = i.market_label + (i.owner_label ? ' · ' + i.owner_label : '')
    + (i.work_type_label ? ' · ' + i.work_type_label : '')
    + ' · bid ' + (i.bid_date || 'date not given') + ' · ' + i.n_bidders + ' bidders ('
    + (i.bidders_source === 'rule N1' ? 'suggested' : 'yours') + ')';
  renderHeadline(r);
  renderContext(r);
  renderWarnings(r);
  renderNotCovered(r);
  renderLowConf(r);
  renderLines(r);
  renderCompetition(r);
  renderSimilar(r);
}

function renderHeadline(r) {
  const host = $('#r-headline');
  host.innerHTML = '';
  (r.alerts || []).filter(a => a.placement === 'above_headline')
    .forEach(a => host.appendChild(notice(a.level, null, a.text)));
  const h = r.headline;
  const nc = r.not_covered;
  if (h.p50 === null || h.p50 === undefined) {
    host.appendChild(notice('severe', nc.as_a_whole ? 'No estimate: the schedule is not covered as a whole'
      : 'No estimate', nc.as_a_whole ? (nc.why || []).join('; ')
        + ' — price the job from vendor / specialty quotes.'
      : 'The estimator could not price this schedule; see the warnings below.'));
    return;
  }
  const cov = h.covered_only ? el('div', { class: 'covnote', text: 'Covered lines only — '
    + nc.n + ' not-covered line' + (nc.n === 1 ? ' is' : 's are') + ' not in this figure.' }) : null;
  const band = (b, lab) => b ? el('div', { class: 'band' }, [
    el('span', { class: 'bl', text: lab }),
    el('span', { class: 'bv', text: short$(b[0]) + ' – ' + short$(b[1]) })]) : null;
  host.appendChild(el('div', { class: 'headline' }, [
    el('div', { class: 'bigcard' }, [
      el('div', { class: 'cap', text: 'Expected low bid (P50)' }),
      el('div', { class: 'big', text: money(h.p50) }),
      el('div', { class: 'expl', text: 'Our best estimate of the low bid.' }),
      band(h.p50_band, 'Half of past lettings landed within'),
      cov,
    ]),
    el('div', { class: 'bigcard lead' }, [
      el('div', { class: 'cap', text: 'Recommended OPCC (P80)' }),
      el('div', { class: 'big', text: money(h.p80) }),
      el('div', { class: 'expl', text: 'The low bid came in at or below this in about 80% '
        + 'of past lettings' + (h.p80_multiplier ? ' (P50 × ' + h.p80_multiplier.toFixed(3) + ').' : '.') }),
      band(h.p80_band, '80% of past lettings landed within'),
      cov ? cov.cloneNode(true) : null,
    ]),
    el('div', { class: 'calib' }, [
      el('div', { text: 'Bands calibrated on ' + (h.calibration_n || '?') + ' ' + (h.calibration_market || '')
        + ' lettings, each priced only from lettings dated before it.' }),
      h.fixed_total ? el('div', { text: 'Includes ' + money(h.fixed_total)
        + ' of owner-set money at face value.' }) : null,
      el('div', { class: 'dim', text: 'P50 is centred ×' + (h.level_correction || 1).toFixed(4)
        + '; the P80 margin is set by the not-under guard'
        + (h.not_under_level ? ' (level ' + h.not_under_level + ')' : '') + '.' }),
    ]),
  ]));
}

function renderContext(r) {
  const c = r.context;
  const host = $('#r-context');
  host.innerHTML = '';
  if (!c) return;
  host.appendChild(el('div', { class: 'context' }, [
    el('b', { text: 'How accurate this has been: ' }),
    document.createTextNode(c.label + ' (' + c.lettings + ', forward in time): P50 median error '
      + pct(c.p50_median_error) + '; P80 came in under the low bid on ' + pct(c.p80_under)
      + ' of lettings; when above, the median overshoot was ' + pct(c.median_overshoot) + '.'),
  ]));
}

// Alerts are decided on the result's keys by the server; the estimator's own
// warnings follow verbatim as notes (their wording is never parsed here).
function renderWarnings(r) {
  const host = $('#r-warnings');
  host.innerHTML = '';
  (r.alerts || []).filter(a => a.placement !== 'above_headline')
    .forEach(a => host.appendChild(notice(a.level, null, a.text)));
  const ws = r.warnings || [];
  if (!ws.length) return;
  host.appendChild(el('details', { class: 'fold warnfold' }, [
    el('summary', { text: 'Estimator notes (' + ws.length + ')' }),
    el('ul', { class: 'warnlist' }, ws.map(w => el('li', { text: w }))),
  ]));
}

function renderNotCovered(r) {
  const nc = r.not_covered;
  const host = $('#r-notcovered');
  host.innerHTML = '';
  $('#nc-sub').textContent = nc.n ? nc.n + ' line' + (nc.n === 1 ? '' : 's') : 'none';
  if (!nc.n) {
    host.appendChild(el('div', { class: 'muted-note', text: 'Every line has comparable bid '
      + 'history; nothing needs a vendor quote. (Pumps, generators and electrical gear, '
      + 'prefab / pedestrian bridges, tanks and process equipment, specialty buildings, '
      + 'synthetic turf / play equipment and meter vaults are listed here when present.)' }));
    return;
  }
  host.appendChild(notice('caution', 'P50 and P80 above EXCLUDE these lines.',
    'They are specialty items with almost no comparable bid history, so any figure '
    + 'from history would be a guess. Price them from vendor quotes (enter them under '
    + 'Project details) and add them.'));
  host.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, ['Item', 'Description', 'Class', 'Qty', 'Unit', 'Vendor quote']
      .map((h, k) => el('th', { class: k >= 3 && k !== 4 ? 'num' : '', text: h })))),
    el('tbody', {}, nc.lines.map(l => el('tr', {}, [
      el('td', { text: l.item_no }), el('td', { text: l.description }),
      el('td', { text: l.class_label }), el('td', { class: 'num', text: qtyFmt(l.quantity) }),
      el('td', { text: l.unit }),
      el('td', { class: 'num', text: l.quote ? money(l.quote) : 'needs a quote' }),
    ]))),
  ]));
  if (nc.complete_total !== null && nc.complete_total !== undefined) {
    host.appendChild(el('div', { class: 'complete' }, [
      el('div', {}, [el('span', { class: 'cl', text: 'Complete total at P50 (P50 + quotes ' + money(nc.quoted_total) + ')' }),
        el('span', { class: 'cv', text: money(nc.complete_total) })]),
      nc.complete_total_p80 !== null ? el('div', {}, [el('span', { class: 'cl', text: 'Complete total at P80 (P80 + quotes)' }),
        el('span', { class: 'cv', text: money(nc.complete_total_p80) })]) : null,
    ]));
  } else {
    host.appendChild(el('div', { class: 'muted-note', text: (nc.quoted_n || 0) + ' of ' + nc.n
      + ' quoted — the complete total appears once every not-covered line has a quote.' }));
  }
}

function renderLowConf(r) {
  const lc = r.low_confidence;
  const host = $('#r-lowconf');
  host.innerHTML = '';
  const lines = lc.lines || [];
  if (!lines.length) {
    host.appendChild(el('div', { class: 'muted-note', text: 'No mobilization or general-condition '
      + 'lump sum in the priced lines.' }));
    return;
  }
  host.appendChild(el('div', { class: 'muted-note', style: 'margin-bottom:8px', text:
    'Bidders price these very differently and our lump-sum prices barely move with job size, '
    + 'so the amounts are a reference only; P50 and P80 are not changed by this panel. Each '
    + 'class is set against the share of the job the low bidders of earlier ' + (r.inputs.market === 'txdot' ? 'TxDOT' : 'city / county')
    + ' lettings gave it. ' + lc.guide }));
  const rows = lines.map(g => {
    const h = g.history;
    const pos = g.position;
    return el('tr', {}, [
      el('td', {}, [el('div', { text: g.label }),
        el('div', { class: 'dim', text: 'item ' + (g.items || []).filter(x => x).join(', ') })]),
      el('td', { class: 'num', text: g.ours_p50 !== null ? money(g.ours_p50) : '—' }),
      el('td', { class: 'num', text: pct(g.share_of_p50, 1) }),
      el('td', { class: 'num', text: h ? pct(h.p25, 1) + ' – ' + pct(h.p75, 1) : '—',
        title: h ? 'median ' + pct(h.median, 1) + '; ' + h.lettings + ' earlier lettings, '
          + h.size_label + (h.scope === 'size' ? ' (rest of the job)' : '') : '' }),
      el('td', { class: 'num', text: g.implied_range ? money(g.implied_range[0]) + ' – '
        + money(g.implied_range[1]) : 'too few lettings' }),
      el('td', {}, pos ? el('span', { class: 'badge ' + (pos === 'within' ? 'ok' : 'thin'),
        text: pos === 'within' ? 'within the range'
          : pos === 'above' ? 'above: P50 likely high here' : 'below: P50 likely low here' }) : null),
    ]);
  });
  host.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, ['Class', 'Ours at P50 level', 'Share of P50',
      'Earlier low bidders (p25–p75)', 'Implied $ range', 'Ours is']
      .map((t, k) => el('th', { class: k >= 1 && k <= 4 ? 'num' : '', text: t })))),
    el('tbody', {}, rows),
  ]));
}

function accBadge(a) {
  if (!a) return el('span', { class: 'dim', text: '—' });
  const cls = 'acc ' + a.tier + (a.weak ? ' weak' : '');
  const prefix = a.tier === 'na' ? '' : a.weak === 'lump_sum' ? 'LS · '
    : a.weak === 'unclassified' ? '? · ' : a.weak === 'allowance' ? 'allow · ' : '';
  return el('span', { class: cls, title: a.tooltip || '', text: prefix + a.text });
}

function renderLines(r) {
  const body = $('#result-body');
  body.innerHTML = '';
  (r.lines || []).forEach(l => {
    body.appendChild(resultRow(l));
    body.appendChild(el('tr', { class: 'detail', 'data-detail': l.row_id, style: 'display:none' },
      el('td', { colspan: 10 }, lineDetail(l))));
  });
  const foot = $('#result-foot');
  foot.innerHTML = '';
  const sum = (r.lines || []).reduce((a, l) => a + (l.amount_p50 || 0), 0);
  foot.appendChild(el('tr', {}, [
    el('td', { colspan: 6, class: 'num', text: 'Sum of lines at the P50 level (= P50)' }),
    el('td', { class: 'num money p50', text: money(sum) }),
    el('td', { colspan: 3, class: 'dim', text: r.totals.unpriced ? r.totals.unpriced
      + ' line(s) not priced and not in the sum' : '' }),
  ]));
}

function resultRow(l) {
  const tr = el('tr', { 'data-line': l.row_id, class: 'clickable st-' + l.status
    + (l.amount_missing ? ' st-amount-missing' : '') });
  tr.addEventListener('click', () => {
    const d = $('tr[data-detail="' + CSS.escape(l.row_id) + '"]');
    if (!d) return;
    const open = d.style.display === 'none';
    d.style.display = open ? 'table-row' : 'none';
    const host = $('[data-refs="' + CSS.escape(l.row_id) + '"]', d);
    if (open && host && !host.dataset.loaded && !host.dataset.loading) {
      host.dataset.loading = '1';
      loadReferences(l, host).finally(() => { delete host.dataset.loading; });
    }
  });
  const tags = [];
  if (l.status === 'not_covered' || l.status === 'not_covered_whole') tags.push(el('span', { class: 'badge unc', text: 'not covered' }));
  if (l.status === 'owner_set') tags.push(el('span', { class: 'badge allow', text: 'owner-set' }));
  if (l.amount_missing) tags.push(el('span', { class: 'badge missing', text: 'allowance amount missing' }));
  if (l.lowconf_class && l.status === 'priced') tags.push(el('span', { class: 'badge lowc', text: 'low confidence' }));
  if (l.status === 'unpriced') tags.push(el('span', { class: 'badge no', text: 'not priced' }));
  tr.appendChild(el('td', { class: 'dim', text: l.item_no }));
  tr.appendChild(el('td', {}, [el('span', { text: l.description }), tags.length ? el('span', { class: 'tags' }, tags) : null]));
  tr.appendChild(el('td', {}, [
    el('span', { class: 'fam' + (l.family_overridden ? ' over' : ''), text: l.family_label }),
    l.size_bucket ? el('span', { class: 'size', text: l.size_bucket }) : null,
  ]));
  tr.appendChild(el('td', { class: 'num money', text: qtyFmt(l.quantity) }));
  tr.appendChild(el('td', { text: l.unit }));
  tr.appendChild(el('td', { class: 'num money', text: l.unit_price_p50 ? rate(l.unit_price_p50) : '—' }));
  tr.appendChild(el('td', { class: 'num money p50', text: l.amount_p50 ? money(l.amount_p50)
    : (l.status === 'not_covered' && l.quote ? '(' + money(l.quote) + ')' : '—'),
    title: l.status === 'not_covered' && l.quote ? 'vendor quote, not in P50' : '' }));
  tr.appendChild(el('td', { class: 'basis', text: l.basis, title: (l.why || []).join('\n') }));
  tr.appendChild(el('td', {}, accBadge(l.accuracy)));
  const w = l.warnings || [];
  tr.appendChild(el('td', {}, w.length ? el('div', { class: 'flags' }, w.map(x =>
    el('span', { class: 'flag ' + x.level, text: x.text }))) : null));
  return tr;
}

function lineDetail(l) {
  const ev = l.evidence || {};
  const kv = [
    ['Classification', l.family + ' · ' + l.unit + (l.size_bucket ? ' · ' + l.size_bucket : '')
      + (l.family_overridden ? ' (family chosen by you)' : ' (parser)')],
    ['Market unit price (all bids)', l.market_unit_price ? rate(l.market_unit_price) + ' / ' + l.unit : '—'],
    ['Evidence', (ev.usable ? 'reference cell usable = ' + ev.usable : '—')
      + (ev.n_projects ? ' · ' + ev.n_projects + ' projects' : '')
      + (ev.qty_band ? ' · quantity band ' + ev.qty_band : '')],
  ];
  if (l.fixed_amount) kv.push(['Owner-set amount', money(l.fixed_amount)]);
  if (l.quote) kv.push(['Vendor quote', money(l.quote) + ' (not in P50 / P80)']);
  const refsHost = el('div', { class: 'refs', 'data-refs': l.row_id },
    el('span', { class: 'dim', html: '<span class="spinner"></span> loading the references…' }));
  return el('div', { class: 'detailbox' }, [
    el('h5', { text: 'References behind this price' }),
    refsHost,
    el('details', { class: 'fold', style: 'margin-top:8px' }, [
      el('summary', { text: 'Estimator notes and typical accuracy' }),
      el('div', { class: 'detailcols', style: 'margin-top:6px' }, [
        el('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])),
        el('div', {}, [
          (l.why || []).length ? el('ul', { class: 'tight' }, l.why.map(w => el('li', { text: w })))
            : el('div', { class: 'muted-note', text: 'No estimator notes.' }),
          el('div', { class: 'muted-note', style: 'margin-top:6px', text: (l.accuracy || {}).tooltip || '' }),
        ]),
      ]),
    ]),
  ]);
}

async function loadReferences(l, host, tries) {
  try {
    const r = await postJSON('/api/references', { request: S.resultReq, row_id: l.row_id });
    host.innerHTML = '';
    host.appendChild(referencesView(r));
    host.dataset.loaded = '1';
  } catch (e) {
    if (e.status === 503 && (tries || 0) < 20) {
      host.innerHTML = '';
      host.appendChild(el('span', { class: 'dim', html: '<span class="spinner"></span> '
        + 'the reference index is still loading (it is built once after start-up)…' }));
      setTimeout(() => loadReferences(l, host, (tries || 0) + 1), 1500);
      return;
    }
    host.innerHTML = '';
    host.appendChild(notice('caution', 'Could not load the references', String(e.message || e)));
  }
}

const REF_COLS = [
  ['date', 'Letting', x => x.date],
  ['project', 'Project', null],
  ['bidders', 'Bidders', null],
  ['low_total', 'Project low bid', null],
  ['item', 'Item as written', null],
  ['quantity', 'Qty', x => x.quantity || 0],
  ['median_unit', 'Median bid $/unit', x => x.median_unit],
  ['low_unit', 'Low bid $/unit', null],
  ['range', 'Bids range', null],
];

function referencesView(r) {
  const box = el('div', {});
  if (r.special) {
    box.appendChild(notice(r.special === 'unpriced' || r.special === 'no_cell' ? 'caution' : 'info',
      null, r.text));
    return box;
  }
  box.appendChild(el('div', { class: 'deriv', text: r.derivation }));
  if (r.warning) box.appendChild(notice('caution', null, r.warning));
  const rep = r.reproduced || {};
  box.appendChild(el('div', { class: 'muted-note repro ' + (rep.price_match && rep.rows_match ? 'ok' : 'bad'),
    text: (rep.price_match && rep.rows_match)
      ? '✓ Checked: the median of these lettings’ median bids is the estimator’s cell price '
        + money(rep.published_cell_price, 2) + ', and with the factors above it gives exactly '
        + rate(rep.trace_price) + '/' + r.unit + '.'
      : '! The listed rows do not reproduce the estimator’s price exactly (cell '
        + money(rep.published_cell_price, 2) + ' vs ' + money(rep.cell_price_from_rows, 2)
        + ' from the rows; price ' + rate(rep.price) + ' vs ' + rate(rep.trace_price) + ').' }));
  if (r.note) box.appendChild(el('div', { class: 'muted-note', text: r.note }));
  const all = r.references || [];
  if (!all.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'No letting of this cell is dated before the bid date.' }));
    return box;
  }
  const state = { all: false, sort: null, dir: -1 };
  const tableHost = el('div', {});
  const more = el('button', { class: 'btn ghost sm' });
  function rows() {
    let list = all.slice();
    if (state.sort) {
      const f = REF_COLS.find(c => c[0] === state.sort)[2];
      list.sort((a, b) => (f(a) > f(b) ? 1 : f(a) < f(b) ? -1 : 0) * state.dir);
    }
    return state.all ? list : list.slice(0, r.top || 3);
  }
  function paint() {
    tableHost.innerHTML = '';
    const head = el('tr', {}, REF_COLS.map(([k, lab, f]) => el('th', {
      class: (['bidders', 'low_total', 'quantity', 'median_unit', 'low_unit'].includes(k) ? 'num' : '')
        + (f && state.all ? ' sortable' : '') + (state.sort === k ? ' sorted' : ''),
      title: f && state.all ? 'Sort by ' + lab.toLowerCase() : '',
      text: lab + (state.sort === k ? (state.dir > 0 ? ' ▲' : ' ▼') : ''),
      onclick: f && state.all ? () => {
        state.dir = state.sort === k ? -state.dir : -1;
        state.sort = k;
        paint();
      } : null,
    })).concat([el('th', {})]));
    const body = el('tbody', {});
    rows().forEach(x => {
      const bidsRow = el('tr', { class: 'bidsrow', style: 'display:none' },
        el('td', { colspan: REF_COLS.length + 1 }, bidsTable(x)));
      body.appendChild(el('tr', {}, [
        el('td', { class: 'nowrap', text: x.date }),
        el('td', { title: (x.title || '') + (x.source && x.source.url ? '\n' + x.source.url : '') }, [
          el('div', { text: x.project }),
          el('div', { class: 'dim', text: x.owner + (x.same_owner ? ' (same owner)' : '') }),
        ]),
        el('td', { class: 'num', text: x.bidders || '—' }),
        el('td', { class: 'num', text: money(x.low_total) }),
        el('td', { text: x.item }),
        el('td', { class: 'num', text: qtyFmt(x.quantity) }),
        el('td', { class: 'num' }, [
          el('div', { class: 'strong', text: rate(x.median_unit) }),
          x.escalated_unit ? el('div', { class: 'dim', text: '≈ ' + rate(x.escalated_unit)
            + ' in ' + r.to_year + ' $' }) : null,
        ]),
        el('td', { class: 'num', text: rate(x.low_unit) }),
        el('td', { class: 'num nowrap', text: rate(x.min_unit) + ' – ' + rate(x.max_unit) }),
        el('td', {}, el('button', { class: 'btn ghost sm', text: x.n_bids + ' bids',
          title: 'Every bid on this item in that letting',
          onclick: () => { bidsRow.style.display = bidsRow.style.display === 'none' ? 'table-row' : 'none'; } })),
      ]));
      body.appendChild(bidsRow);
    });
    tableHost.appendChild(el('table', { class: 'mini reftable' }, [el('thead', {}, head), body]));
    more.textContent = state.all ? 'show the top ' + (r.top || 3) + ' only'
      : '… show all ' + all.length + ' reference' + (all.length === 1 ? '' : 's');
    more.hidden = all.length <= (r.top || 3);
  }
  more.addEventListener('click', () => { state.all = !state.all; if (!state.all) state.sort = null; paint(); });
  box.appendChild(el('div', { class: 'muted-note', style: 'margin:6px 0 3px', text:
    (all.length > (r.top || 3) ? 'The ' + (r.top || 3) + ' most relevant of ' + all.length
      : 'All ' + all.length) + ' public letting' + (all.length === 1 ? '' : 's')
    + ' of this cell dated before ' + r.bid_date + ' (ranked by recency, wording, quantity and owner). '
    + 'The median bid is what the estimator uses.' }));
  box.appendChild(tableHost);
  box.appendChild(more);
  paint();
  return box;
}

function bidsTable(x) {
  return el('div', { class: 'bidsbox' }, [
    el('div', { class: 'dim', text: x.project + ' · ' + x.date + ' · '
      + (x.source && x.source.document ? x.source.document : '') }),
    el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, ['Bidder', 'Qty', '$/unit', ''].map((t, k) =>
        el('th', { class: k === 1 || k === 2 ? 'num' : '', text: t })))),
      el('tbody', {}, (x.bids || []).map(b => el('tr', { class: b.low ? 'low' : '' }, [
        el('td', { text: b.bidder || '—' }),
        el('td', { class: 'num', text: qtyFmt(b.quantity) }),
        el('td', { class: 'num', text: rate(b.unit_price) }),
        el('td', { class: 'dim', text: b.low ? 'low bidder' : '' }),
      ]))),
    ]),
  ]);
}

// Similar past projects: comparables.py's own top picks (the jobs the P50
// blend uses), most similar first, up to four. Public lettings dated before
// the bid date only -- the server never sends anything else.
function renderSimilar(r) {
  const s = r.similar || {};
  const host = $('#r-similar');
  host.innerHTML = '';
  const cards = s.cards || [];
  $('#sim-sub').textContent = cards.length
    ? 'the ' + cards.length + ' most similar earlier public job' + (cards.length === 1 ? '' : 's')
      + ', bid before ' + s.before
    : 'none';
  if (s.why_fewer) {
    host.appendChild(notice(cards.length ? 'info' : 'caution',
      cards.length ? 'Fewer than ' + s.max + ' similar projects' : 'No similar past project',
      s.why_fewer));
  }
  if (cards.length) {
    host.appendChild(el('div', { class: 'muted-note', style: 'margin-bottom:9px', text: s.how
      + ' Line-priced ' + money(s.line_price) + ' · comparables ' + money(s.cmp_price)
      + ' → blend ' + money(s.blend) + (s.level_correction ? ' → ×'
      + s.level_correction.toFixed(4) + ' centring' : '') + '.' }));
  }
  const detail = el('div', { class: 'simdetail' });
  const grid = el('div', { class: 'simgrid' });
  cards.forEach(c => grid.appendChild(simCard(c, r, detail, grid)));
  host.appendChild(grid);
  host.appendChild(detail);
}

function simCard(c, r, detail, grid) {
  const b = c.bids || {};
  const sh = c.shares || {};
  const at = c.at_their_prices;
  const f = c.factors;
  const norm = x => String(x || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const showTitle = c.title && norm(c.title) !== norm(c.project);
  const simTip = f ? 'Similarity ' + c.similarity.toFixed(3) + ' (comparables.py) = overlap '
    + pct(f.overlap) + ' × size ' + f.size.toFixed(2) + ' × same owner '
    + f.same_owner.toFixed(1) + ' × same work type ' + f.same_work_type.toFixed(1)
    + ' × age ' + f.age.toFixed(2) + ' (' + f.age_years.toFixed(1) + ' years before)' : '';
  const btn = el('button', { class: 'btn sm', text: 'Matched lines (' + (c.matched_lines || []).length + ')' });
  const card = el('div', { class: 'simcard' }, [
    el('div', { class: 'simtop' }, [
      el('span', { class: 'rank', text: c.rank }),
      el('span', { class: 'own', text: c.owner }),
      el('span', { class: 'dim', text: c.date }),
    ]),
    el('div', { class: 'simname', title: c.key, text: c.project }),
    showTitle ? el('div', { class: 'simtitle', title: c.title, text: c.title }) : null,
    el('div', { class: 'simsub', text: (c.work_type || 'work type not given') + ' · '
      + c.bidders + ' bidder' + (c.bidders === 1 ? '' : 's') }),
    el('dl', { class: 'kv simkv' }, [
      el('dt', { text: 'Low bid' }), el('dd', { class: 'strong', text: money(b.low) }),
      el('dt', { text: 'Other bids' }), el('dd', { text: '2nd ' + short$(b.second) + ' · median '
        + short$(b.median) + ' · high ' + short$(b.high) }),
      el('dt', { text: 'Size' }), el('dd', { text: isNum(c.size_vs_this_job)
        ? c.size_vs_this_job.toFixed(2) + '× this job' : '—',
        title: 'their low bid / our P50' }),
      el('dt', { text: 'Overlap' }), el('dd', { title: simTip, text: 'shares ' + sh.types + ' of '
        + sh.of_types + ' item types, ' + pct(sh.dollar_share) + ' of the dollars' }),
      el('dt', { text: 'At their prices' }), el('dd', {
        title: at ? 'This job priced by comparables.py from this project’s low-bid lines alone '
          + '(escalated, quantity- and competition-adjusted as in the blend); lines it has no match '
          + 'for stay at our own line price. Owner-set money added at face value.' : '',
        text: at ? money(at.job) + ' (' + (isNum(at.vs_p50) ? at.vs_p50.toFixed(2) + '× our P50; ' : '')
          + 'their lines ' + pct(at.matched_share) + ')' : '—' }),
    ]),
    el('div', { class: 'simsrc' }, [
      el('span', { class: 'dim', text: 'Source: ' }),
      document.createTextNode(c.source.document || c.key),
      c.source.url ? el('div', { class: 'url', text: c.source.url }) : null,
    ]),
    el('div', { class: 'simfoot' }, [btn,
      el('span', { class: 'dim', title: simTip, text: 'score ' + (isNum(c.similarity) ? c.similarity.toFixed(2) : '—') })]),
  ]);
  btn.addEventListener('click', () => {
    const open = card.classList.contains('sel');
    $$('.simcard', grid).forEach(x => x.classList.remove('sel'));
    detail.innerHTML = '';
    if (open) return;
    card.classList.add('sel');
    detail.appendChild(matchedTable(c));
  });
  return card;
}

function matchedTable(c) {
  const rows = c.matched_lines || [];
  return el('div', {}, [
    el('h5', { text: '#' + c.rank + ' ' + c.project + ' — matched lines' }),
    el('div', { class: 'muted-note', style: 'margin-bottom:6px', text: 'Each of our lines with the line '
      + 'comparables.py would transfer from this project (best word match, then the closest '
      + 'quantity). Their unit prices are as bid on ' + c.date + '; ours are at the P50 level for '
      + 'this letting.' }),
    rows.length ? el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, ['Our item', 'Our description', 'Qty', 'Unit', 'Ours $/unit (P50)',
        'Their line', 'Their qty', 'Their low bidder $/unit', 'Their median bidder $/unit', 'Match']
        .map((t, k) => el('th', { class: [2, 4, 6, 7, 8, 9].includes(k) ? 'num' : '', text: t })))),
      el('tbody', {}, rows.map(m => el('tr', {}, [
        el('td', { text: m.item_no }), el('td', { text: m.description }),
        el('td', { class: 'num', text: qtyFmt(m.quantity) }), el('td', { text: m.unit }),
        el('td', { class: 'num', text: rate(m.our_unit_p50) }),
        el('td', { text: (m.their_item_no ? m.their_item_no + '  ' : '') + (m.their_description || '—') }),
        el('td', { class: 'num', text: qtyFmt(m.their_quantity) + ' ' + (m.their_unit || '') }),
        el('td', { class: 'num', text: rate(m.their_low_unit) }),
        el('td', { class: 'num', text: rate(m.their_median_unit), title: m.their_bids_on_line
          ? m.their_bids_on_line + ' bids on the line' : '' }),
        el('td', { class: 'num', text: isNum(m.match) ? m.match.toFixed(2) : '' }),
      ]))),
    ]) : el('div', { class: 'muted-note', text: 'No line of this project matches ours one to one.' }),
  ]);
}

function renderCompetition(r) {
  const c = r.competition;
  const host = $('#r-competition');
  host.innerHTML = '';
  const s = c.suggestion;
  host.appendChild(el('dl', { class: 'kv' }, [
    el('dt', { text: 'Bidders used' }), el('dd', { text: c.n_bidders + (c.source === 'rule N1'
      ? '  (suggested by rule N1)' : '  (entered by you)') }),
    s ? el('dt', { text: 'Rule N1 suggests' }) : null,
    s ? el('dd', { text: s.n + '  — ' + (s.cell === 'default' ? 'default (no history cell)'
      : 'median of ' + s.history_lettings + ' earlier ' + s.cell_label) }) : null,
    el('dt', { text: 'Discount f(n)' }), el('dd', { text: c.available ? '×' + Number(c.f).toFixed(4)
      + ' on the median bid' : 'none — no estimate' }),
    c.available ? el('dt', { text: 'From' }) : null,
    c.available ? el('dd', { text: (c.d1 ? 'size-aware D1 discount (TxDOT T1)'
      : c.table + ' table, ' + c.bin + '-bidder bin') + ' · ' + c.lettings + ' lettings'
      + (c.extrapolated ? ' (nearest bin)' : '') }) : null,
    c.available && c.p25 ? el('dt', { text: 'Middle half of lettings' }) : null,
    c.available && c.p25 ? el('dd', { text: '×' + Number(c.p25).toFixed(3) + ' – ×'
      + Number(c.p75).toFixed(3) }) : null,
    c.note ? el('dt', { text: 'Note' }) : null, c.note ? el('dd', { text: c.note }) : null,
  ]));
  host.appendChild(el('div', { class: 'muted-note', style: 'margin-top:8px', text:
    'The low bid is the lowest of n bids, so more bidders means a lower expected low bid. '
    + 'The discount goes on the bid total, not on each unit price; owner-set money is added '
    + 'after it.' }));
}

/* ================================================================= EXPORT */

function initExport() {
  const go = async (kind) => {
    if (!S.result) return;
    if (S.stale) toast('Exporting the last estimate — inputs changed since; re-estimate first '
      + 'if you want them in.', 'warn', 7000);
    const r = await fetch('/api/export/' + kind, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result: S.result, request: S.resultReq }),
    });
    if (!r.ok) {
      let m = r.status + '';
      try { m = (await r.json()).detail || m; } catch (e) { /* keep */ }
      toast('Export failed: ' + m, 'err');
      return;
    }
    const blob = await r.blob();
    const cd = r.headers.get('content-disposition') || '';
    const name = (cd.match(/filename="([^"]+)"/) || [])[1] || ('bid_estimate.' + kind);
    const a = el('a', { href: URL.createObjectURL(blob), download: name });
    document.body.appendChild(a); a.click(); a.remove();
    toast('Saved ' + name + '.', 'ok');
  };
  $('#btn-export-csv').addEventListener('click', () => go('csv'));
  $('#btn-export-xlsx').addEventListener('click', () => go('xlsx'));
}

/* ======================================================= BID TAB (staging) */

async function uploadBidtab(file) {
  const out = $('#bidtab-result');
  out.innerHTML = '';
  out.appendChild(notice('info', null, 'Parsing ' + file.name + ' with src/parse_bidtabs.py…'));
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await api('/api/bidtab/parse', { method: 'POST', body: fd });
    out.innerHTML = '';
    if (!r.ok) { out.appendChild(notice('severe', 'Parse failed', r.error)); return; }
    out.appendChild(notice('ok', r.file + ': ' + r.n_rows.toLocaleString('en-US')
      + ' line-item rows from ' + r.n_bidders + ' bidder(s)', r.note));
    out.appendChild(el('dl', { class: 'kv' }, [
      el('dt', { text: 'Units seen' }),
      el('dd', { text: Object.entries(r.units || {}).map(([k, v]) => k + ' (' + v + ')').join(', ') }),
      el('dt', { text: 'Parse time' }), el('dd', { text: r.elapsed_s + ' s' }),
      el('dt', { text: 'Staged at' }), el('dd', { text: r.staged_path || '—' }),
    ]));
  } catch (e) {
    out.innerHTML = '';
    out.appendChild(notice('severe', 'Parse failed', String(e.message || e)));
  }
}

/* =================================================================== BOOT */

function boot() {
  initTheme();
  initInput();
  initProject();
  initExport();
  renderStatusBar();
  renderQuotes();
  renderOwnerSet();
  refreshEstimateButton();
  pollStatus();
}

document.addEventListener('DOMContentLoaded', boot);
