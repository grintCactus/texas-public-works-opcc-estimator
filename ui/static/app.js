// An OPCC usually carries a contingency; 10% is the common default (user,
// 2026-11-21). Applied to your line amounts except owner-set allowances.
const DEFAULT_CONTINGENCY = 10;
// Contingency percentages are picked from a list, not typed (user,
// 2026-11-21): a contingency line's share of the other lines, and the
// worksheet's own contingency.
const LINE_CONTINGENCY_PCTS = [5, 10, 15, 20, 25];
const WS_CONTINGENCY_PCTS = [0, 5, 10, 15, 20, 25];
// A contingency allowance is a lump sum: LS, an allowance unit, no unit, or
// one EA. A "contingency item" measured in SY, CY, TON... is a bid item that
// bidders price. (Public bid history, 316 contingency lines: about 230 are
// lump sums or allowances that every bidder copies; a dozen in SY, CY, TON,
// LF are priced by each bidder.)
const LUMP_UNIT_RE = /^(?:|LS|L\.?\s*S\.?|L\/S|LUMP(?:\s*SUM)?|ALLOW(?:ANCE)?|ALW|ALLW|DOL|\$)$/i;
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
  // the OPCC worksheet: the estimator's own prices -- OUTPUT ONLY, never part
  // of an estimate request
  ws: { lines: {}, contingency: DEFAULT_CONTINGENCY, refs: null, refsReq: null, filter: false,
        open: {}, name: '', round: 'off', prepared_by: '', project_no: '', basis: 'history',
        refsLoading: false, refsError: '', refsPromise: null, refsPendingReq: null },
};

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

/* ------------------------------------------------------------ public demo */

// Public-demo mode: the server marks the page <html data-public="1">
// (ui/public_mode.py). Nothing is kept on the server there: the corrections
// memory and saved worksheets live in this browser's localStorage. Every
// storage access is guarded -- blocked storage only means nothing is
// remembered.
const PUBLIC = document.documentElement.getAttribute('data-public') === '1';
const CORR_KEY = 'bid-estimator-corrections';
const WS_KEY = 'bid-estimator-worksheets';
const WS_MAX = 20;

function lsGet(key, dflt) {
  try {
    const v = window.localStorage.getItem(key);
    return v ? JSON.parse(v) : dflt;
  } catch (e) { return dflt; }
}

function lsSet(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch (e) { return false; }
}

function browserCorrections() {
  const d = lsGet(CORR_KEY, null);
  return d && d.entries && typeof d.entries === 'object' && !Array.isArray(d.entries) ? d.entries : {};
}

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

// a wrapping text box as tall as its text
function growDesc(t) {
  if (!t) return;
  t.style.height = 'auto';
  t.style.height = (t.scrollHeight + 2) + 'px';
}
let growTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(growTimer);
  growTimer = setTimeout(() => document.querySelectorAll('#review-body textarea.desc').forEach(growDesc), 120);
});

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
  // Match the server's generous arithmetic limit, including worksheet prices.
  return isFinite(f) && Math.abs(f) <= 1e100 ? f : null;
}

/* ------------------------------------------------------------------- http */

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    let body = null;
    if (ct.includes('json')) {
      try { body = await r.json(); msg = body.detail || body.error || msg; } catch (e) { /* keep */ }
    }
    if (r.status >= 500 && r.status !== 503) {
      // the details stay out of the page (About this tool / the server log)
      msg = 'Something went wrong on the server (error ' + r.status + '). Try again; '
        + 'if it keeps happening, reload the page.';
    }
    if (r.status === 401 && body && body.login) {
      window.location.href = body.login + '?next=' + encodeURIComponent(window.location.pathname);
    }
    const err = new Error(msg);
    err.status = r.status;
    err.body = body;
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

// A warning you tick once read (user, 2026-11-21): a short title, what to
// do, and the lines it is about. Ticked, it turns gray and steps back;
// nothing is blocked either way. The tick holds while the warning says the
// same thing (S.acks, keyed on its code and title).
function ackKey(a) { return (a.code || '') + '|' + (a.title || a.text || ''); }

function ackNotice(a) {
  const key = ackKey(a);
  const done = !!(S.acks && S.acks[key]);
  const items = (a.items || []).map(t => el('li', { text: t }));
  if (a.more) items.push(el('li', { class: 'dim', text: 'and ' + a.more + ' more' }));
  const box = el('div', { class: 'notice ack ' + (done ? 'acked' : a.level), 'data-ack': a.code || '' }, [
    el('label', { class: 'ackchk', title: 'Tick once you have read and checked this' },
      el('input', { type: 'checkbox', checked: done, 'aria-label': 'Checked: ' + (a.title || a.text || ''),
        onchange: e => { S.acks = S.acks || {}; S.acks[key] = e.target.checked;
          box.className = 'notice ack ' + (e.target.checked ? 'acked' : a.level); } })),
    el('span', { class: 'ic', text: a.level === 'severe' ? '\u2715' : '!' }),
    el('div', { class: 'body-txt' }, [
      el('div', {}, [el('b', { text: a.title || a.text || '' }),
        a.action ? document.createTextNode(' ' + a.action) : null]),
      items.length ? el('ul', { class: 'ackitems' }, items) : null,
    ]),
  ]);
  return box;
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

/* ------------------------------------------- rebuild a block, keep focus */

// The grid, the owner-set box and the worksheet are rebuilt wholesale on a
// change. A text field's change event fires when the estimator leaves it --
// usually by clicking or tabbing straight into the next field -- so a rebuild
// run inside that event replaced the field being clicked and swallowed the
// click (QA 2026-11-20). Rebuilds triggered by a change therefore wait until
// the click is over (pointer up) and the next turn of the event loop; every
// field that a rebuild replaces carries data-fk, and the focused one is found
// again in the new block with its text and caret.
const focusKey = (...p) => p.join(':');

function focusState() {
  const a = document.activeElement;
  if (!a || !a.dataset || !a.dataset.fk) return null;
  let sel = null;
  try { sel = [a.selectionStart, a.selectionEnd]; } catch (e) { /* not text */ }
  return { fk: a.dataset.fk, value: a.value, sel, el: a };
}

function restoreFocus(st) {
  if (!st || (st.el && st.el.isConnected && document.activeElement === st.el)) return;
  const cell = document.querySelector('[data-fk="' + CSS.escape(st.fk) + '"]');
  if (!cell) return;
  if ((cell.tagName === 'INPUT' || cell.tagName === 'TEXTAREA') && cell.value !== st.value) {
    cell.value = st.value;
    if (cell.tagName === 'TEXTAREA') growDesc(cell);
  }
  cell.focus();
  if (st.sel && st.sel[0] !== null) {
    try { cell.setSelectionRange(st.sel[0], st.sel[1]); } catch (e) { /* ok */ }
  }
}

let pointerIsDown = false;
const afterQueue = [];
function flushAfter() {
  setTimeout(() => {
    const fns = afterQueue.splice(0);
    if (!fns.length) return;
    const st = focusState();
    fns.forEach(fn => fn());
    restoreFocus(st);
  }, 0);
}
function afterInput(fn) {
  if (!afterQueue.includes(fn)) afterQueue.push(fn);
  if (!pointerIsDown) flushAfter();
  else setTimeout(() => { if (afterQueue.length) { pointerIsDown = false; flushAfter(); } }, 1500);
}
document.addEventListener('pointerdown', () => { pointerIsDown = true; }, true);
['pointerup', 'pointercancel'].forEach(t => document.addEventListener(t, () => {
  pointerIsDown = false;
  if (afterQueue.length) flushAfter();
}, true));

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

// The status bar says one plain thing (how recent the prices are, or that
// the page is still loading); the technical details -- data build, estimator
// version and rule, load time, parser, OCR, the Claude reader -- are under
// "About this tool" at the foot of the page (user, 2026-11-20: "no one needs
// to see that").
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function monthYear(iso) {
  const m = /^(\d{4})-(\d{2})/.exec(String(iso || ''));
  return m ? MONTHS[parseInt(m[2], 10) - 1] + ' ' + m[1] : '';
}

function renderStatusBar() {
  const st = S.status;
  const bar = $('#status-items');
  bar.innerHTML = '';
  if (!st) {
    bar.appendChild(el('span', { class: 'sitem', html: '<span class="spinner"></span> Connecting…' }));
    return;
  }
  const eng = st.engine || {};
  const data = st.data || {};
  if (!st.ready) {
    bar.appendChild(el('span', { class: 'sitem' }, [
      el('span', { class: eng.error ? 'dot bad' : 'spinner' }),
      document.createTextNode(eng.error
        ? ' The price history could not be loaded — see “About this tool” at the foot of the page.'
        : ' Loading the price history — about 10 seconds…'),
    ]));
  } else {
    const thru = monthYear(data.latest_letting);
    bar.appendChild(el('span', { class: 'sitem', text: thru
      ? 'Prices from public bids through ' + thru : 'Prices from public bids' }));
  }
  if (st.upstream && st.upstream.parser_available === false) {
    bar.appendChild(el('span', { class: 'sitem warnnote',
      text: 'Reading takeoffs works in a reduced mode — see “About this tool”.' }));
  }
  renderClaudeButton();
}

// "About this tool": what the status bar used to show, for debugging.
function aboutRows() {
  const st = S.status || {};
  const eng = st.engine || {};
  const est = st.estimator || {};
  const data = st.data || {};
  const up = st.upstream || {};
  const ocr = st.ocr || {};
  const co = st.corrections || {};
  const pdfOcr = up.pdf_ocr_engines || [];
  const nb = PUBLIC ? Object.keys(browserCorrections()).length : co.entries;
  return [
    ['Data', st.ready ? 'built ' + (data.master_panel_mtime || '?') + '; latest letting '
      + (data.latest_letting || '?') + '; calibration written ' + (data.calibration_mtime || '?')
      + '; comparables pools ' + Object.entries(data.pool_lettings || {}).map(([k, v]) => v + ' ' + k).join(', ')
      + ' public lettings' + (data.train_scope ? ' (' + data.train_scope + ')' : '')
      : (eng.error ? 'failed to load: ' + eng.error : 'loading (' + (eng.step || 'starting') + ')')],
    ['Estimator', 'src/estimate_low_bid.py md5 ' + (est.md5 || '?') + (est.rule ? ' · ' + est.rule : '')],
    ['Source files', Object.entries(est.md5_all || {}).map(([k, v]) => k + ' ' + v).join('; ') || '—'],
    ['Loaded', isNum(eng.load_s) ? (eng.load_s + (eng.history_s || 0)).toFixed(1) + ' s; memory '
      + (eng.rss_mb || '?') + ' MB, peak ' + (eng.peak_rss_mb || '?') + ' MB' : '—'],
    ['Parser', up.parser_available ? 'src/parse_bidtabs.py ' + (up.parser_version || '')
      + '; size buckets from ' + (up.size_bucket_source || '?')
      : 'UNAVAILABLE: ' + (up.parser_error || '?')],
    ['OCR', pdfOcr.length ? 'scanned PDFs read offline (' + pdfOcr.join(', ') + ')'
      + (ocr.real ? '; images too' : '') : (up.pdf_ocr_error || ocr.error || 'not installed')],
    ['Corrections', (nb || 0) + ' remembered' + (PUBLIC ? ' (in this browser only)'
      : co.file ? ' (' + co.file + ', local only)' : '')],
    ['Mode', PUBLIC ? 'public demo: uploads are processed in memory and not stored'
      : 'local: nothing leaves this machine'],
    ['Version', 'page ' + (st.version || '?')],
  ].filter(Boolean);
}

function openAbout() {
  const host = $('#modal-host');
  function close() { host.innerHTML = ''; document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  const back = el('div', { class: 'modal-back', onclick: e => { if (e.target === back) close(); } },
    el('div', { class: 'modal', role: 'dialog', 'aria-label': 'About this tool' }, [
      el('header', {}, [el('h3', { text: 'About this tool' }),
        el('button', { class: 'btn sm close', text: 'Close', onclick: close })]),
      el('div', { class: 'mbody' }, [
        el('p', { class: 'muted-note', text: 'The expected low bid and the recommended OPCC come from '
          + 'public bid tabulations only. These are the technical details behind the page, for '
          + 'checking and debugging.' }),
        el('dl', { class: 'kv about' }, aboutRows().flatMap(([k, v]) =>
          [el('dt', { text: k }), el('dd', { text: v })])),
      ]),
    ]));
  host.innerHTML = '';
  host.appendChild(back);
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
].join('\n');   // no contingency line: the worksheet adds yours (user, 2026-11-21: it would count twice)

function initInput() {
  $$('#input-tabs button').forEach(b => b.addEventListener('click', () => {
    $$('#input-tabs button').forEach(x => x.setAttribute('aria-selected', String(x === b)));
    $$('#sec-input .tabpane').forEach(p =>
      p.classList.toggle('active', p.id === 'pane-' + b.dataset.pane));
  }));
  $('#btn-sample').addEventListener('click', () => { $('#paste-text').value = SAMPLE; });
  $('#btn-parse-text').addEventListener('click', async () => {
    const text = $('#paste-text').value;
    // even an empty paste gets the server's plain "Couldn't read this"
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

  $('#btn-add-row').addEventListener('click', () => addRow());

  // Enter / arrows move down the same column, like a spreadsheet; Enter on
  // the last row adds one; Alt+Up / Alt+Down move the line itself.
  $('#review-table').addEventListener('keydown', e => {
    const t = e.target;
    if (!t.classList || !t.classList.contains('cellinput')) return;
    if (e.key !== 'Enter' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    if (t.tagName === 'TEXTAREA' && !e.altKey && e.key !== 'Enter') {
      const atStart = t.selectionStart === 0 && t.selectionEnd === 0;
      const atEnd = t.selectionStart === t.value.length && t.selectionEnd === t.value.length;
      if ((e.key === 'ArrowUp' && !atStart) || (e.key === 'ArrowDown' && !atEnd)) return;
    }
    e.preventDefault();
    const i = parseInt(t.dataset.i, 10);
    if (e.altKey && e.key !== 'Enter') {
      const l = S.lines[i];
      t.blur();
      if (l) moveLineBy(l, e.key === 'ArrowUp' ? -1 : 1);
      const back = l && $('#review-body tr[data-row="' + CSS.escape(l.row_id) + '"] [data-col="'
        + t.dataset.col + '"]');
      if (back) back.focus();
      return;
    }
    const col = t.dataset.col;
    const next = i + (e.key === 'ArrowUp' ? -1 : 1);
    t.blur();
    const target = $('#review-body [data-col="' + col + '"][data-i="' + next + '"]');
    if (target) { target.focus(); if (target.select) target.select(); }
    else if (e.key === 'Enter' && next === S.lines.length) addRow();
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

/* ----------------------------------------- optional: read a PDF with Claude */

// Opt-in, per document: nothing is sent until the user presses a button in
// the dialog for that file, and the server refuses without ?consent=yes.
function claudeStatus() { return (S.status && S.status.claude_reader) || {}; }
const CLAUDE_DEMO_OFF = 'Not available in this online demo. On your own computer it can read '
  + 'difficult PDFs with Claude (see the README).';

function renderClaudeButton() {
  const cr = claudeStatus();
  const btn = $('#btn-claude'), note = $('#claude-note');
  if (!btn) return;
  btn.classList.toggle('off', !cr.available);
  note.textContent = cr.available
    ? 'Optional, for scans and messy PDFs: ' + cr.label + '. Asks first; sends only that PDF.'
    : PUBLIC ? CLAUDE_DEMO_OFF
    : 'Off \u2014 to turn it on see ui/README.md, \u201cSetting up Claude reading\u201d '
      + '(brew install anthropics/tap/ant, then ant auth login; or ANTHROPIC_API_KEY from the '
      + 'Console; or sign in to the claude CLI).';
}

/* AI mode: the one place for reading with AI (user, 2026-11-21: "leave a
   window here"; it replaces "Read a PDF with Claude", and files still go in
   through the one drop area). Not built yet: the button opens the panel
   that says what it will do; nothing is read or sent. The Claude PDF reader
   behind the old button stays in the code for when this is built. */
function initAiMode() {
  const btn = $('#btn-ai-mode'), panel = $('#ai-mode-panel');
  if (!btn || !panel) return;
  btn.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
  });
}

function initClaude() {
  const btn = $('#btn-claude'), fi = $('#claude-input');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const cr = claudeStatus();
    if (!cr.available) {
      const st = $('#file-status');
      st.innerHTML = '';
      if (PUBLIC) {
        st.appendChild(notice('info', 'Reading with Claude', CLAUDE_DEMO_OFF));
        return;
      }
      st.appendChild(notice('info', 'Reading with Claude is not set up', (cr.reason || '')
        + ' Two ways: an Anthropic API credential (brew install anthropics/tap/ant, then '
        + 'ant auth login; or set ANTHROPIC_API_KEY from the Console), or the claude CLI signed '
        + 'in with your Claude plan. Details: ui/README.md, \u201cSetting up Claude reading\u201d.'));
      return;
    }
    fi.click();
  });
  fi.addEventListener('change', () => { if (fi.files[0]) claudePreview(fi.files[0]); fi.value = ''; });
}

async function claudeCall(path, file, consent) {
  const fd = new FormData();
  fd.append('file', file);
  return api(path + (consent ? '?consent=yes' : ''), { method: 'POST', body: fd });
}

async function claudePreview(file) {
  if (!/\.pdf$/i.test(file.name)) { toast('Only PDF files can be read with Claude.', 'err'); return; }
  let pv;
  try { pv = await claudeCall('/api/claude/preview', file, false); }
  catch (e) { toast('Could not open ' + file.name + ': ' + e.message, 'err'); return; }
  openClaudeDialog(file, pv);
}

function claudeEstimateText(pv, counted) {
  const e = pv.estimate || {};
  const k = v => (v < 10000 ? (v / 1000).toFixed(1) : Math.round(v / 1000)) + 'k';
  const tok = r => k(r[0]) + '\u2013' + k(r[1]);
  if (pv.backend === 'cli') {
    return 'About ' + tok(e.input_tokens) + ' input and ' + tok(e.output_tokens)
      + ' output tokens from the page count, counted against your Claude plan\u2019s usage limits '
      + '(no per-token bill).';
  }
  if (counted) {
    return counted.input_tokens.toLocaleString() + ' input tokens (counted by the API) + about '
      + tok(counted.output_tokens) + ' output tokens \u2248 ' + money(counted.usd[0], 2) + '\u2013'
      + money(counted.usd[1], 2) + ' at ' + pv.model + ' list prices (' + counted.price_date + ').';
  }
  return 'About ' + money(e.usd[0], 2) + '\u2013' + money(e.usd[1], 2) + ' (' + tok(e.input_tokens)
    + ' input, ' + tok(e.output_tokens) + ' output tokens, from the page count; '
    + 'press \u201cCount tokens\u201d for the exact input).';
}

function openClaudeDialog(file, pv) {
  const host = $('#modal-host');
  const est = el('dd', { text: claudeEstimateText(pv, null) });
  const msg = el('div', {});
  const readBtn = el('button', { class: 'btn primary', text: 'Read with Claude' });
  const countBtn = pv.can_count ? el('button', { class: 'btn', text: 'Count tokens (sends the PDF; free)' }) : null;
  const cancel = el('button', { class: 'btn ghost', text: 'Cancel' });
  function close() { host.innerHTML = ''; document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  cancel.addEventListener('click', close);
  if (countBtn) countBtn.addEventListener('click', () => withBusy(countBtn, async () => {
    msg.innerHTML = '';
    try {
      const c = await claudeCall('/api/claude/count', file, true);
      est.textContent = claudeEstimateText(pv, c);
    } catch (e) { msg.appendChild(notice('caution', 'Token count failed', claudeErr(e))); }
  }));
  readBtn.addEventListener('click', () => withBusy(readBtn, async () => {
    msg.innerHTML = '';
    msg.appendChild(el('div', { class: 'dim' }, [el('span', { class: 'spinner' }),
      document.createTextNode(' Claude is reading ' + Number(pv.pages) + ' page(s)'
        + (pv.requests > 1 ? ' in ' + Number(pv.requests) + ' parts' : '')
        + '\u2026 this takes a minute or two.')]));
    try {
      const r = await claudeCall('/api/claude/read', file, true);
      close();
      $('#file-status').innerHTML = '';
      await acceptParse(r.parse);
    } catch (e) {
      msg.innerHTML = '';
      msg.appendChild(notice('severe', 'Claude could not read it', claudeErr(e)));
    }
  }));
  host.appendChild(el('div', { class: 'modal-back', onclick: e => { if (e.target === host.firstChild) close(); } },
    el('div', { class: 'modal' }, [
      el('header', {}, [el('h3', { text: 'Read with Claude: ' + pv.file_name }),
        el('button', { class: 'btn sm close', text: 'Close', onclick: close })]),
      el('div', { class: 'mbody' }, [
        el('dl', { class: 'kv' }, [
          el('dt', { text: 'File' }), el('dd', { text: pv.file_name + ' \u00b7 '
            + (pv.bytes / 1048576).toFixed(1) + ' MB' }),
          el('dt', { text: 'Pages' }), el('dd', { text: pv.pages + (pv.requests > 1
            ? ' (sent in ' + pv.requests + ' parts)' : '') }),
          el('dt', { text: 'Reads with' }), el('dd', { text: pv.label + ' \u00b7 ' + pv.model }),
          el('dt', { text: 'Cost' }), est,
        ]),
        notice('info', 'Privacy', pv.consent + ' ' + pv.privacy),
        el('div', { class: 'muted-note', text: 'Claude only transcribes item numbers, descriptions, '
          + 'units and quantities \u2014 no prices. Every line lands in the grid tagged '
          + '\u201cread by Claude\u201d, with the checks it failed, for you to confirm.' }),
        msg,
        el('div', { class: 'row-actions', style: 'margin-top:10px' }, [readBtn, countBtn, cancel]),
      ]),
    ])));
}

function claudeErr(e) {
  const d = (e && e.body) || {};
  const hint = {
    consent_required: 'The server needs your consent for this document.',
    unavailable: PUBLIC ? CLAUDE_DEMO_OFF
      : 'Reading with Claude is not set up (ui/README.md, \u201cSetting up Claude reading\u201d).',
    rate_limited: 'Anthropic is rate limiting this account; try again in a minute.',
    refused: 'Claude declined this document; nothing was added.',
    bad_output: 'The answer was not usable; nothing was added. Try again, or paste the rows.',
    truncated: 'The answer was cut off; nothing was added.',
    timeout: 'No answer in time; try again.',
    auth: 'The credential was rejected.',
  }[d.code] || '';
  return String((e && e.message) || e) + (hint ? ' ' + hint : '');
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
    pricing_mode: '',
    amount: null,
    amount_src: null,     // 'file' | 'user'
    file_amount: isNum(l.amount) ? Number(l.amount) : null,
    file_unit_price: isNum(l.unit_price) ? Number(l.unit_price) : null,
    quote: null,
    cls: null,
    issues: l.issues || [],
    confidence: l.confidence || 'ok',
    origin: l.origin || '',        // 'claude' when the Claude reader read it
    page: l.page || null,
    family_src: '',                // '' | 'user' | 'remembered' | 'parser'
    remembered: null,
    assumed: Array.isArray(l.assumed) ? l.assumed.slice() : [],   // 'unit' | 'quantity'
  };
}

// The estimator typed over a value the reader assumed (a lump sum's LS / 1):
// it is theirs now, and the "assumed -- check" note goes with it.
function confirmAssumed(l, field) {
  if (!l.assumed || !l.assumed.includes(field)) return;
  l.assumed = l.assumed.filter(f => f !== field);
  if (!l.assumed.length) {
    l.issues = (l.issues || []).filter(i => !/^assumed/.test(i));
    if (!l.issues.length) l.confidence = 'ok';
  }
}

// A line the estimate cannot price yet: no quantity (and no owner-set amount
// or vendor quote that stands in for one). It is left out of the request,
// and the page says so, until the quantity is filled in.
function missingQuantity(l) {
  // a contingency line is a share of the rest or a fixed sum: no quantity
  // needed (estimateRequest sends 1 LS when it has none)
  if (isContingencyLine(l)) return false;
  return !(l.quantity > 0) && !(l.amount > 0) && !(l.amount_pct > 0) && !(l.quote > 0)
    && !(l.cls && l.cls.coverage_class);
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
    st.appendChild(notice('severe', 'Couldn\u2019t read ' + file.name, String(e.message || e)));
  }
}

async function acceptParse(p) {
  S.parse = p;
  // the takeoff template's project block (user, 2026-11-21): typed once, it
  // heads the worksheet and the exported OPCC
  const pj = p.project || {};
  if (pj.name) S.ws.name = pj.name;
  if (pj.project_no) S.ws.project_no = pj.project_no;
  if (pj.prepared_by) S.ws.prepared_by = pj.prepared_by;
  S.seq = 0;
  S.lines = (p.lines || []).map(l => newLine(Object.assign({}, l, { row_id: null })));
  S.bidders.mode = 'auto';
  clearResult();
  renderParseNotes();
  renderGrid();
  renderOwnerSet();
  if (!S.lines.length) {
    toast('Couldn\u2019t read this \u2014 see the note above the grid.', 'err');
    document.getElementById('parse-notes').scrollIntoView({ block: 'nearest' });
    return;
  }
  toast('Read ' + S.lines.length + ' line item(s)' + (p.n_unread ? '; ' + p.n_unread + ' line'
    + (p.n_unread === 1 ? '' : 's') + ' could not be read (see the note above the grid)' : '')
    + '. Check the classification column.', 'ok');
  await classifyAll();
  document.getElementById('sec-input').scrollIntoView({ behavior: 'smooth' });
}

function renderParseNotes() {
  const host = $('#parse-notes');
  host.innerHTML = '';
  const p = S.parse;
  if (!p) return;
  if (p.unreadable) {
    host.appendChild(notice('severe', 'Couldn\u2019t read this', el('div', {}, [
      el('div', { text: p.unreadable.reason || 'No line items could be read.' }),
      p.unreadable.hint ? el('div', { class: 'muted-note', style: 'margin-top:4px',
        text: p.unreadable.hint }) : null,
    ])));
  }
  if (p.n_unread) host.appendChild(unreadNote(p));
  if (p.mock) {
    host.appendChild(notice('severe', 'These items are MOCK data, not read from your file',
      'The OCR engine (src/ocr_extract.py) has not been delivered, so the image reader '
      + 'returned a stand-in. Nothing here may be priced. Paste the items as text instead.'));
  }
  if (p.notes && p.notes.length && !p.unreadable) {
    host.appendChild(el('details', { class: 'fold howcalc' }, [
      el('summary', { text: 'How \u201c' + String(p.source || 'input').slice(0, 80)
        + '\u201d was read (' + p.method + ')' }),
      el('ul', { class: 'tight howbody' }, p.notes.map(n => el('li', { text: n }))),
    ]));
  }
  const fold = $('#skipped-fold');
  fold.innerHTML = '';
  fold.hidden = !p.n_skipped;
  if (p.n_skipped) {
    fold.appendChild(el('summary', { text: 'Headings and totals (' + p.n_skipped
      + ') \u2014 not read as pay items' }));
    fold.appendChild(el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, [el('th', { text: 'Row' }), el('th', { text: 'Why' }),
        el('th', { text: 'Text' }), el('th', {})])),
      el('tbody', {}, (p.skipped || []).map(s => el('tr', {}, [
        el('td', { text: s.row || '\u2014' }), el('td', { text: s.reason }),
        el('td', { class: 'dim rawtext', text: clipText(s.raw, 120) }),
        el('td', {}, addToGridButton(s.raw)),
      ]))),
    ]));
  }
}

// at most n characters (whole code points), with an ellipsis when cut
function clipText(v, n) {
  const a = Array.from(String(v === null || v === undefined ? '' : v));
  return a.length > n ? a.slice(0, n - 1).join('') + '\u2026' : a.join('');
}

function addToGridButton(raw) {
  return el('button', { class: 'btn ghost sm', type: 'button', text: 'Add to the grid',
    title: 'Add this text as a line, to fix it in the grid',
    onclick: () => {
      addRow();
      const l = S.lines[S.lines.length - 1];
      l.description = String(raw || '').replace(/\s*\|\s*/g, ' ').trim().slice(0, 300);
      renderGrid();
      classifyOne(l);
    } });
}

// "N lines could not be read": what the reader could not take as a pay item
// (garbled text, symbols or numbers only, words with no unit or quantity),
// shown as written -- plain text, cut to 120 characters, wrapped anywhere --
// above the grid, folded. Nothing in it is estimated.
function unreadNote(p) {
  const n = p.n_unread;
  const list = p.unread || [];
  return el('details', { class: 'fold unread', id: 'unread-note' }, [
    el('summary', { text: n + ' line' + (n === 1 ? '' : 's') + ' could not be read' }),
    el('div', { class: 'muted-note', text: 'Shown as written; they are not in the estimate. '
      + 'If one is a pay item, add it to the grid and fill in its unit and quantity.' }),
    el('ul', { class: 'unread-list' }, list.map(s => el('li', {}, [
      el('span', { class: 'rawtext', text: clipText(s.raw, 120) }),
      el('span', { class: 'why', text: (s.row ? 'line ' + s.row + ' \u00b7 ' : '') + s.reason }),
      addToGridButton(s.raw),
    ]))),
    n > list.length ? el('div', { class: 'muted-note', text: 'and ' + (n - list.length) + ' more' }) : null,
  ]);
}

/* -------------------------------------------------------- classification */

function linePayload(l) {
  return { row_id: l.row_id, description: l.description, unit: l.unit, family: l.family || '',
    pricing_mode: l.pricing_mode || '',
    quantity: l.quantity, file_amount: l.file_amount, file_unit_price: l.file_unit_price,
    confidence: l.confidence, issues: l.issues, keep_parser: l.family_src === 'parser' };
}

// A correction remembered for this wording (ui/.local/corrections.json):
// the server classified the line with it; the grid adopts it as the line's
// family override, so the estimate request carries it like any other choice.
function adoptRemembered(l) {
  const c = l.cls;
  if (!c || !c.remembered || l.family || l.family_src === 'parser') return;
  l.family = c.remembered.family;
  l.family_src = 'remembered';
  l.remembered = c.remembered;
  if (!l.unit && c.remembered.unit) l.unit = c.remembered.unit;
}

// the status bar's "Remembered N corrections" after the memory changed
function refreshCorrectionsStatus(r) {
  if (!S.status) return;
  if (!PUBLIC && r && r.status) S.status.corrections = r.status;
  renderStatusBar();
}

async function rememberCorrection(l, family, unit, source) {
  if (!family || !String(l.description || '').trim()) return;
  try {
    const r = await postJSON('/api/corrections', { description: l.description, family,
      unit: unit || '', source: source || 'picker' });
    // public demo: the server only normalises the wording; this browser keeps it
    if (PUBLIC && r && r.key && r.saved) {
      const ents = browserCorrections();
      const old = ents[r.key] || {};
      const now = new Date().toISOString().slice(0, 19);
      ents[r.key] = { family: r.saved.family, unit: r.saved.unit || '', source: r.saved.source || 'picker',
        first: old.first || now, last: now, times: (old.times || 0) + 1 };
      if (!lsSet(CORR_KEY, { version: 1, entries: ents })) {
        toast('This browser does not allow storage, so the correction is not remembered.', 'err');
      }
    }
    refreshCorrectionsStatus(r);
  } catch (e) { toast('Could not remember the correction: ' + e.message, 'err'); }
}

async function forgetCorrection(l) {
  try {
    const r = await postJSON('/api/corrections/forget', { description: l.description });
    if (PUBLIC && r && r.key) {
      const ents = browserCorrections();
      if (ents[r.key]) { delete ents[r.key]; lsSet(CORR_KEY, { version: 1, entries: ents }); }
    }
    refreshCorrectionsStatus(r);
  } catch (e) { /* nothing remembered */ }
}

// The classify request; in the public demo it carries this browser's
// corrections (used for that request only, never stored on the server).
function classifyBody(lines) {
  const b = { market: S.market, lines, bid_date: ($('#in-biddate') || {}).value || null };
  if (PUBLIC) b.corrections = browserCorrections();
  return b;
}

// One click on a suggestion: its family (and its unit when the line has
// none), remembered for this wording.
function applySuggestion(l, sg) {
  const c = l.cls || {};
  const unitMissing = !c.unit || c.unit_known === false;
  const fill = unitMissing && sg.unit ? sg.unit : '';
  l.family = sg.family;
  l.pricing_mode = '';
  l.family_src = 'user';
  l.remembered = null;
  if (fill) l.unit = fill;
  markStale();
  classifyOne(l);
  rememberCorrection(l, sg.family, fill, 'suggestion');
  toast('Row ' + (l.item_no || '') + ': ' + sg.family_label + (fill ? ', unit ' + fill : '')
    + ' — remembered for this wording.', 'ok');
}

// An explicit decision for this line, separate from an automatic generic match.
function markNewItem(l) {
  l.family = 'other';
  l.family_src = 'user';
  l.pricing_mode = 'engineer';
  l.remembered = null;
  markStale();
  classifyOne(l);
  toast('New item / Other selected. Enter your unit price in the worksheet after estimating.', 'ok');
}

// An uploaded takeoff's amount / unit-price column is used for owner-set
// allowance lines only (the server decides: cls.import_amount), and never
// over an amount the user typed.
function adoptImportAmount(l) {
  const c = l.cls;
  if (ncTakeoffLine(l) && l.amount > 0 && !(l.quote > 0)) { l.quote = l.amount; l.amount = null; l.amount_pct = null; }
  if (!c || l.amount_src === 'user') return;
  if (c.owner_set_like && c.import_amount > 0) {
    l.amount = c.import_amount;
    l.amount_src = 'file';
    l.amount_pct = null;
  } else if (l.amount_src === 'file') {
    l.amount = null;
    l.amount_src = null;
  }
  // a contingency allowance is a share of the other lines (user, 2026-11-21:
  // "a contingency should be a percentage"): 10% unless the user or the file gave $
  if (isContingencyLine(l) && !(l.amount > 0) && !isNum(l.amount_pct)) l.amount_pct = DEFAULT_CONTINGENCY;
  if (!isContingencyLine(l) && l.amount_src !== 'user') l.amount_pct = null;
}

// A contingency allowance in the takeoff: owner-set money worded as a
// contingency, on a lump sum (LUMP_UNIT_RE). It has no item type and is not
// priced from history: its amount is a share of the other lines, picked from
// a list, or the owner's fixed amount.
function isContingencyLine(l) {
  if (!(l && l.cls && l.cls.owner_set_like && /contingenc/i.test(l.description || ''))) return false;
  return lumpLike(l.cls.unit || l.unit, l.quantity);
}

function lumpLike(unit, quantity) {
  const u = String(unit || '').trim();
  return LUMP_UNIT_RE.test(u) || (/^(?:EA|EACH)$/i.test(u) && !(quantity > 1));
}

// The same line in an estimate: owner-set money worded as a contingency, on a
// lump sum or given as a share of the other lines.
function isContingencyResult(l) {
  return !!(l && l.status === 'owner_set' && /contingenc/i.test(l.description || '')
    && (l.amount_from_pct || lumpLike(l.unit, l.quantity)));
}

// A worksheet line with nothing to look up: a contingency, or any owner-set
// line given as a share of the other lines. No item type, references,
// accuracy or price picks -- what it is and its amount.
function shareOrContingency(l) {
  // the server says so (below_subtotal); a worksheet saved before it did
  // falls back to the same rule here
  if (l && l.below_subtotal !== undefined) return !!l.below_subtotal;
  return isContingencyResult(l) || !!(l && l.status === 'owner_set' && l.amount_from_pct);
}

// A contingency line's amount, from a list: a share of the other lines, or
// the owner's fixed amount when the bid form prints one (then a $ box).
// ns: 'g' the takeoff grid, 'o' the owner-set box.
function contingencyControl(l, ns) {
  const fixed = l.amount > 0 || !!l.cont_fixed;
  const cur = Number(l.amount_pct);
  const pcts = LINE_CONTINGENCY_PCTS.slice();
  if (cur > 0 && !pcts.includes(cur)) { pcts.push(cur); pcts.sort((a, b) => a - b); }
  const none = !fixed && !(cur > 0);
  const done = () => { markStale(); afterInput(renderGrid); afterInput(renderOwnerSet); suggestBidders(); };
  const sel = el('select', { class: 'cpct', 'data-fk': focusKey(ns, l.row_id, 'cpct'),
    'aria-label': 'Contingency' + (l.item_no ? ', item ' + l.item_no : ''),
    title: 'Contingency: a share of the subtotal (every other line’s expected low bid), or the owner’s '
      + 'fixed amount from the bid form. Taken at face value, outside the competition discount.',
    onchange: e => {
      const v = e.target.value;
      if (!v) return;
      l.cont_fixed = v === 'fixed';
      l.amount_pct = l.cont_fixed ? null : Number(v);
      if (!l.cont_fixed) l.amount = null;
      l.amount_src = 'user';
      done();
    } }, (none ? [el('option', { value: '', text: 'pick…', selected: true })] : [])
      .concat(pcts.map(p => el('option', { value: String(p), selected: !fixed && cur === p, text: p + '%' })))
      .concat([el('option', { value: 'fixed', text: 'fixed $', selected: fixed })]));
  if (!fixed) return el('div', { class: 'cpctcell' }, [sel,
    el('span', { class: 'dim', text: 'of the subtotal' })]);
  return el('div', { class: 'cpctcell' }, [sel, el('input', { type: 'text',
    class: 'cellinput num' + (l.amount > 0 ? '' : ' hintfill'), placeholder: 'owner’s $',
    value: l.amount > 0 ? qtyFmt(l.amount) : '', 'data-fk': focusKey(ns, l.row_id, 'amount'),
    title: 'The owner’s fixed contingency amount, from the bid form',
    onchange: e => {
      const v = parseNum(e.target.value);
      l.amount = v > 0 ? v : null;
      l.amount_src = 'user';
      e.target.value = l.amount > 0 ? qtyFmt(l.amount) : '';
      done();
    } })]);
}

// "10%" or "10 %" -> {pct: 10}; "50,000" -> {amount: 50000}; anything else -> {}
function parseAmountOrPct(text) {
  const t = String(text || '').trim();
  const m = t.match(/^(\d+(?:\.\d+)?)\s*%$/);
  if (m) { const p = Number(m[1]); return p > 0 && p <= 100 ? { pct: p } : {}; }
  const v = parseNum(t);
  return v > 0 ? { amount: v } : {};
}

function amountCellText(l) {
  if (l.amount > 0) return qtyFmt(l.amount);
  if (isNum(l.amount_pct) && l.amount_pct > 0) return l.amount_pct + '%';
  return '';
}

async function classifyAll() {
  if (!S.options || !S.lines.length) { renderGrid(); return; }
  try {
    const r = await postJSON('/api/classify', classifyBody(S.lines.map(linePayload)));
    const by = {};
    (r.lines || []).forEach(c => { by[c.row_id] = c; });
    S.lines.forEach(l => { if (by[l.row_id]) { l.cls = by[l.row_id]; adoptRemembered(l); adoptImportAmount(l); } });
    // the history search loads after the estimator; ask again for the
    // suggestions once it is there
    if (S.lines.some(l => l.cls && l.cls.intake && l.cls.intake.suggestions_pending)) {
      clearTimeout(S.suggestTimer);
      S.suggestTimer = setTimeout(() => classifyAll(), 2500);
    }
  } catch (e) {
    toast('Classification failed: ' + e.message, 'err');
  }
  renderGrid();
  renderQuotes();
  renderOwnerSet();
  suggestBidders();
}

// The lines edited since the last classify request; one debounced request
// takes all of them. (One shared debounce per call used to drop every edited
// line but the last when the estimator moved quickly from row to row.)
const classifyPending = new Set();
const classifyFlush = debounce(async () => {
  const ls = Array.from(classifyPending).filter(l => S.lines.includes(l));
  classifyPending.clear();
  if (!S.options || !ls.length) return;
  const sent = ls.map(linePayload);
  try {
    const r = await postJSON('/api/classify', classifyBody(sent));
    const by = {};
    (r.lines || []).forEach(c => { by[c.row_id] = c; });
    ls.forEach((l, k) => {
      const c = by[l.row_id];
      const now = linePayload(l);
      // an answer for text the line no longer holds is dropped: the newer
      // text has its own request queued
      if (!c || now.description !== sent[k].description || now.unit !== sent[k].unit
          || now.family !== sent[k].family) return;
      l.cls = c; adoptRemembered(l); adoptImportAmount(l);
    });
  } catch (e) { toast('Classification failed: ' + e.message, 'err'); }
  renderGrid();
  renderQuotes();
  renderOwnerSet();
  suggestBidders();
}, 350);

function classifyOne(l) { classifyPending.add(l); classifyFlush(); }

/* ---------------------------------------------------------------- the grid */

// The grid is rebuilt wholesale on every change, and classification is
// async, so a rebuild can land while the estimator types somewhere else. The
// focused cell's own text and caret are carried across every rebuild
// (focusState / restoreFocus, keyed on each cell's data-fk).
function renderGrid() {
  const focus = focusState();
  const has = S.lines.length > 0;
  $('#grid-head').hidden = !has;
  $('#grid-wrap').hidden = !has;
  $('#addrow-bar').hidden = !has;
  const body = $('#review-body');
  body.innerHTML = '';
  S.lines.forEach((l, i) => body.appendChild(gridRow(l, i)));
  renderGridLegend(body);
  body.querySelectorAll('textarea.desc').forEach(growDesc);
  const n = S.lines.filter(l => l.include).length;
  const noq = S.lines.filter(l => l.include && missingQuantity(l)).length;
  const unc = S.lines.filter(l => l.include && l.cls && l.cls.coverage_class && !(l.quote > 0)).length;
  const chk = S.lines.filter(l => l.include && l.cls && l.cls.intake && l.cls.intake.low
    && !isContingencyLine(l)).length;
  const byc = S.lines.filter(l => l.origin === 'claude').length;
  $('#review-count').textContent = n + ' of ' + S.lines.length + ' lines included'
    + (chk ? ' · ' + chk + ' to check' : '')
    + (noq ? ' · ' + noq + ' with no quantity (left out of the estimate until filled in)' : '')
    + (unc ? ' · ' + unc + ' not covered' : '')
    + (byc ? ' · ' + byc + ' read by Claude' : '');
  restoreFocus(focus);
  refreshEstimateButton();
}

function renderGridLegend(body) {
  const labels = [
    ['lowc', 'low confidence', 'Lump-sum prices, such as mobilization, vary widely; review the reference range.'],
    ['manual', 'engineer input', 'No matching item type was chosen; enter your own worksheet price.'],
    ['check', 'check', 'Review the stated issue in this line before estimating.'],
    ['unc', 'not covered', 'Too little comparable bid history; enter a vendor quote.'],
    ['missing', 'allowance amount missing', "Enter the owner's stated allowance amount."],
    ['no', 'unit issue', 'Check the unit against the original schedule.'],
    ['uncl', 'unclassified', 'Use Change to choose an item type.'],
    ['allow', 'owner-set', "The owner's amount, used at face value."],
    ['remembered', 'remembered', 'Your earlier classification for this wording.'],
    ['info', 'your input', 'A classification you chose or a vendor quote you supplied.'],
    ['unith', 'unit: few bids', 'Few bids use this unit; review the unit suggestion on the line.'],
    ['claude', 'read by Claude', 'Read from a PDF by Claude; check it against the document.'],
  ].filter(([kind]) => body.querySelector('.badge.' + kind));
  $('#grid-legend-heading').hidden = !labels.length;
  $('#grid-legend').replaceChildren(...labels.map(([kind, label, meaning]) => el('li', {}, [
    el('span', { class: 'badge ' + kind, text: label }),
    el('span', { class: 'legend-meaning', text: meaning }),
  ])));
}

function claudeBadge(l) {
  return el('span', { class: 'badge claude',
    title: 'Transcribed by Claude from the PDF' + (l.page ? ' (page ' + l.page + ')' : '')
      + '. Check it against the document.' + ((l.issues || []).length ? '\n' + l.issues.join('\n') : ''),
    text: 'read by Claude' + (l.page ? ' · p.' + l.page : '') });
}

function clsCell(l) {
  const c = l.cls;
  if (!c) return el('span', { class: 'dim', text: l.description ? 'classifying…' : '—' });
  // a contingency has no item type: no type, unit, size, checks or
  // suggestions, only what it is (user, 2026-11-21)
  if (isContingencyLine(l)) return el('div', { class: 'clscell' }, [
    el('div', {}, el('span', { class: 'fam', text: 'Contingency',
      title: 'A contingency has no item type and is not priced from history. Its amount is a '
        + 'share of the subtotal, or the owner’s fixed amount, chosen in the next column.' })),
    l.origin === 'claude' ? el('div', { class: 'badges' }, claudeBadge(l)) : null,
  ]);
  const badges = [];
  // not covered: a reminder until you give a vendor quote, then just what it is
  if (c.coverage_class) badges.push(hintPop(l.quote > 0
    ? el('span', { class: 'badge info', text: 'not covered: ' + c.coverage_label + ' \u00b7 your quote ' + money(l.quote) })
    : el('span', { class: 'badge unc', text: 'not covered: ' + c.coverage_label }),
    () => ncHintBox(c.nc_hint, c.coverage_label)));
  if (c.lowconf_class) badges.push(el('span', { class: 'badge lowc',
    title: 'Low confidence: lump sums like this vary widely between bidders; see the '
      + 'low-confidence panel in the results.', text: 'low confidence: ' + c.lowconf_label }));
  if (c.owner_set_like && !(l.amount > 0) && !(l.amount_pct > 0)) badges.push(el('span', { class: 'badge missing',
    title: 'Owner-set money (allowance / contingency / force account): enter the amount from '
      + 'the bid form under Project details \u2192 Owner-set amounts (or in "Owner-set $"). '
      + 'Until then it is priced from history, which is not meaningful for an allowance.',
    text: 'allowance amount missing' }));
  if (l.amount > 0) badges.push(el('span', { class: 'badge allow',
    title: 'Owner-set money, taken at face value and kept out of the competition discount'
      + (l.amount_src === 'file' ? ' (amount read from the uploaded file)' : ''),
    text: 'owner-set ' + money(l.amount) + (l.amount_src === 'file' ? ' (file)' : '') }));
  if (l.pricing_mode === 'engineer') badges.push(el('span', { class: 'badge manual',
    text: 'engineer input', title: 'New item / Other: your worksheet price starts blank.' }));
  else if (c.family === 'other' || c.family === 'needs_review' || !c.family) badges.push(
    el('span', { class: 'badge uncl', title: 'No item type matched: priced from item '
      + 'words only, if at all. Use Change to pick an item type.', text: 'unclassified' }));
  const ia0 = c.intake || {};
  const unitChecked = (ia0.reasons || []).some(x => x.code === 'no_unit' || x.code === 'unknown_unit');
  if (!unitChecked) (c.issues || []).filter(x => /unit/.test(x)).forEach(x =>
    badges.push(el('span', { class: 'badge no', text: x })));
  if (l.origin === 'claude') badges.push(claudeBadge(l));
  if (l.remembered) badges.push(el('span', { class: 'badge remembered',
    title: 'You corrected this wording before (' + (l.remembered.last || '') + '): item type '
      + (l.remembered.family_label || l.remembered.family)
      + (l.remembered.unit ? ', unit ' + l.remembered.unit : '') + '. To forget it: Change \u2192 '
      + '\u201cNot sure \u2014 keep our reading\u201d.',
    text: 'remembered' }));
  const uh = unitHintText(l);
  if (uh) badges.push(el('span', { class: 'badge unith', text: 'unit: few bids' }));
  const ia = Object.assign({}, c.intake || {});
  if (l.pricing_mode === 'engineer') {
    ia.reasons = (ia.reasons || []).filter(x => !['unclassified', 'weak_match'].includes(x.code));
    ia.low = !!ia.low && ia.reasons.length > 0;
  }
  if (ia.low) badges.push(el('span', { class: 'badge check',
    title: 'Low confidence \u2014 check this line: ' + (ia.reasons || []).map(x => x.text).join('; ')
      + (ia.best_match !== null && ia.best_match !== undefined
        ? '\nBest match in the public price history: ' + Math.round(100 * ia.best_match) + '%' : ''),
    text: 'check: ' + (ia.reasons || []).map(x => x.code === 'weak_match' ? 'weak match'
      : x.code === 'no_quantity' ? 'no quantity' : x.code === 'no_unit' ? 'no unit'
      : x.code === 'unknown_unit' ? 'unit?' : x.code === 'unclassified' ? 'no item type'
      : x.code === 'odd_quantity' ? 'quantity?' : x.code === 'assumed' ? 'unit assumed (LS)'
      : x.code === 'cable_unit' ? 'confirm EA or LF for cabling'
      : x.code === 'unclear_description' ? 'unclear description'
      : x.code === 'reader_unsure' ? 'reader unsure' : x.code).join(', ') }));
  const sugg = (ia.low && (ia.suggestions || []).length) ? el('div', { class: 'suggest' }, [
    el('span', { class: 'dim', text: 'Did you mean' }),
  ].concat(ia.suggestions.map(sg => el('button', { class: 'chip one', type: 'button',
    title: 'Use this item type' + (sg.unit ? ' (' + sg.unit + ')' : '') + ': ' + sg.family_label
      + '\nEarlier public line item (' + sg.source + ', ' + sg.n_projects + ' project'
      + (sg.n_projects === 1 ? '' : 's') + '), match ' + Math.round(100 * sg.score) + '%\n' + sg.label
      + '\nRemembered for this wording.',
    onclick: () => applySuggestion(l, sg) }, [
      el('span', { class: 't', text: sg.label }),
      el('span', { class: 's', text: ' \u00b7 ' + sg.family_label + (sg.unit ? ' \u00b7 ' + sg.unit : '') }),
    ])))) : (ia.low && ia.suggestions_pending ? el('div', { class: 'suggest dim',
      text: 'suggestions: the history search is still loading…' }) : null);
  const newItem = ia.low && l.pricing_mode !== 'engineer' ? el('div', { class: 'suggest' }, [
    el('button', { class: 'chip one', type: 'button', 'data-action': 'new-item',
      text: 'New item / Other — Engineer input', onclick: () => markNewItem(l) }),
  ]) : null;
  return el('div', { class: 'clscell' }, [
    el('div', {}, [
      el('span', { class: 'fam' + (c.family_overridden ? ' over' : ''),
        title: 'Item type: ' + c.family_label + (c.family_overridden
          ? ' (your choice; we read ' + (c.family_parser_label || c.family_parser) + ')' : '')
          + '\nUnit: ' + (c.unit || 'none') + (c.size_bucket ? '\nSize: ' + c.size_bucket
          + ' (' + c.size_in + ')' : '') + '\nRead as: ' + c.desc_norm,
        text: l.pricing_mode === 'engineer' ? 'New item / Other' : c.family_label }),
      c.family_overridden ? el('span', { class: 'badge info', text: 'your pick' }) : null,
      c.size_bucket ? el('span', { class: 'size', text: c.size_bucket }) : null,
      el('span', { class: 'unitn', text: c.unit || 'no unit' }),
    ]),
    badges.length ? el('div', { class: 'badges' }, badges) : null,
    uh ? el('div', { class: 'unithint', text: uh }) : null,
    l.pricing_mode === 'engineer' ? null : sugg,
    newItem,
  ]);
}

// The unit hint (batch 3): this line's unit has few earlier bids while the
// same work in another unit of the same kind has plenty. The worksheet's
// text (with the converted price) once there is an estimate, else the
// takeoff's evidence. Reference only -- nothing is converted automatically.
function unitHintText(l) {
  const x = (S.ws && S.ws.refs) ? S.ws.refs[l.row_id] : null;
  if (x && x.unit_hint) return x.unit_hint.text;
  const h = l.cls && l.cls.unit_hint;
  if (!h) return '';
  return h.text + ' After you estimate, the worksheet offers a price converted from '
    + h.target + ' as a quick pick. Reference only \u2014 check it is the same kind of work.';
}

function capText(l) {
  const a = printedCapAmount(l);
  const cap = l.printed_cap;
  return 'The bid form caps this at ' + cap.text + ' of the '
    + (cap.basis === 'rest' ? 'rest of the bid' : 'total bid')
    + (a !== null ? ' = ' + money(a) + ' with your other amounts' : '')
    + '. Bidders usually price it at or just under the printed cap.';
}

// A printed cap on a mobilization line ("5% Max"): the amount it allows,
// from the user's own amounts on every other line. "of the total bid":
// c / (1 - c) x the rest; "of the rest": c x the rest.
function printedCapAmount(l) {
  const cap = l.printed_cap;
  if (!cap || !S.result) return null;
  let rest = 0;
  (S.result.lines || []).forEach(o => {
    if (o.row_id === l.row_id) return;
    const a = wsAmount(o, wsLine(o));
    if (a !== null) rest += a;
  });
  if (!(rest > 0)) return null;
  const c = Number(cap.pct);
  return cap.basis === 'rest' ? c * rest : c / (1 - c) * rest;
}

function gridRow(l, i) {
  const cont = isContingencyLine(l);
  const low = l.cls && l.cls.intake && l.cls.intake.low && !cont;
  const tr = el('tr', { class: (l.include ? '' : 'excluded') + (low ? ' checkrow' : ''),
    'data-row': l.row_id });
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    type: 'checkbox', checked: l.include, title: 'Include this line in the estimate',
    'data-fk': focusKey('g', l.row_id, 'include'),
    onchange: e => { l.include = e.target.checked; afterInput(renderGrid); afterInput(renderQuotes);
      afterInput(renderOwnerSet); markStale(); suggestBidders(); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput', type: 'text', value: l.item_no, 'data-col': 'item_no', 'data-i': i,
    'data-fk': focusKey('g', l.row_id, 'item_no'),
    oninput: e => { l.item_no = e.target.value; markStale(); },
  })));
  // a long description wraps onto as many lines as it needs (user,
  // 2026-11-21: "you must wrap"); Enter moves on, it never adds a line break
  tr.appendChild(el('td', { class: 'tight descc' }, el('textarea', {
    class: 'cellinput desc', rows: 1, text: l.description || '', spellcheck: 'false',
    'data-col': 'description', 'data-i': i, 'data-fk': focusKey('g', l.row_id, 'description'),
    oninput: e => {
      growDesc(e.target);
      l.description = e.target.value.replace(/\s*\n\s*/g, ' ');
      if (l.family_src === 'remembered') { l.family = ''; l.family_src = ''; l.remembered = null; }
      markStale(); classifyOne(l);
    },
  })));
  const assumedU = (l.assumed || []).includes('unit'), assumedQ = (l.assumed || []).includes('quantity');
  // a contingency line needs no unit or quantity: no "needs input" flags
  const needU = !l.unit && !cont, needQ = !(l.quantity > 0) && !cont;
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput' + (needU ? ' flag' : (l.unit && assumedU ? ' assumed' : '')), type: 'text', value: l.unit,
    'data-col': 'unit', 'data-i': i, placeholder: cont ? '' : 'needs input', 'data-fk': focusKey('g', l.row_id, 'unit'),
    title: l.unit && assumedU ? 'Assumed (a lump-sum wording with no unit) \u2014 check it' : needU ? 'Needs a unit' : '',
    'aria-invalid': needU ? 'true' : null,
    oninput: e => { l.unit = e.target.value; confirmAssumed(l, 'unit'); e.target.classList.remove('assumed');
      markStale(); classifyOne(l); },
  })));
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput num' + (needQ ? ' flag' : (l.quantity > 0 && assumedQ ? ' assumed' : '')), type: 'text',
    value: qtyFmt(l.quantity), 'data-col': 'quantity', 'data-i': i, placeholder: cont ? '' : 'needs input',
    'data-fk': focusKey('g', l.row_id, 'quantity'),
    title: l.quantity > 0 && assumedQ ? 'Assumed 1 (a lump sum) \u2014 check it' : needQ
      ? 'Needs a quantity: the line is left out of the estimate until it has one' : '',
    'aria-invalid': needQ ? 'true' : null,
    onchange: e => {
      l.quantity = parseNum(e.target.value);
      confirmAssumed(l, 'quantity');
      e.target.value = qtyFmt(l.quantity);
      e.target.classList.toggle('flag', !(l.quantity > 0) && !isContingencyLine(l));
      // the quantity is part of the line's checks (no quantity, a lump sum
      // with a quantity other than 1): classify it again; the grid is rebuilt
      // after the click that ended the edit, not during it
      markStale(); afterInput(renderGrid); classifyOne(l); suggestBidders();
    },
  })));
  tr.appendChild(el('td', {}, clsCell(l)));
  const allow = l.cls && l.cls.owner_set_like;
  tr.appendChild(el('td', { class: 'tight' }, cont ? contingencyControl(l, 'g') : ncTakeoffLine(l) ? ncQuoteInput(l, i) : el('input', {
    class: 'cellinput num' + (allow && !(l.amount > 0) && !(l.amount_pct > 0) ? ' hintfill' : ''), type: 'text',
    value: amountCellText(l), 'data-col': 'amount', 'data-i': i,
    'data-fk': focusKey('g', l.row_id, 'amount'),
    placeholder: allow ? 'owner’s $ or %' : '',
    title: 'Owner-set money only: the owner’s amount, taken at face value and kept out '
      + 'of the competition discount, or a share of the subtotal such as "10%". Leave '
      + 'blank for a line bidders price.',
    onchange: e => {
      const r = parseAmountOrPct(e.target.value);
      l.amount = r.amount || null;
      l.amount_pct = r.pct || null;
      l.amount_src = 'user';
      e.target.value = amountCellText(l);
      markStale(); afterInput(renderGrid); afterInput(renderOwnerSet); suggestBidders();
      afterInput(() => renderTopAlerts(S.result));
    },
  })));
  tr.appendChild(el('td', {}, el('div', { class: 'row-actions nowrap' }, [
    // a contingency has no item type to change
    cont ? null : el('button', { class: 'btn ghost sm', text: 'Change',
      title: 'Correct the item type, or look at earlier public line items like this one',
      onclick: () => openPicker(l) }),
    el('button', { class: 'btn ghost sm danger', text: '✕', title: 'Delete row',
      onclick: () => { S.lines = S.lines.filter(x => x !== l); renderGrid(); renderQuotes();
        renderOwnerSet(); markStale(); suggestBidders(); } }),
  ])));
  return tr;
}

/* ---------------------------------------------- reorder rows by dragging */

// Press on a row -- not on a field, a list, a link or a button -- and drag
// it: the row lifts and follows the pointer, the rows it passes slide out of
// its way at once, and on release it glides into the gap. It is an insert,
// not a swap: every other row keeps its order. A press that does not move
// more than DRAG_PX is a click (on the worksheet: the references). Touch: a
// long press starts the drag, so a plain swipe still scrolls the page.
// Keyboard: Alt+Up / Alt+Down. Only transforms change during a drag, one
// requestAnimationFrame per frame; prefers-reduced-motion moves instantly.
// (User requests, 2026-11-20.)
const DRAG_PX = 6, LONG_PRESS_MS = 350, SLIDE_MS = 180;
const reduceMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const NO_DRAG = 'input, select, textarea, button, a, label, .chip, details, summary';

// The pure insert: the item at `from` goes into the gap `gap` (0..n, counted
// in the original list); every other item keeps its relative order.
function moveTo(arr, from, gap) {
  const out = arr.slice();
  const [x] = out.splice(from, 1);
  out.splice(gap > from ? gap - 1 : gap, 0, x);
  return out;
}

function announce(msg) {
  const n = $('#sr-live');
  if (!n) return;
  n.textContent = '';
  setTimeout(() => { n.textContent = msg; }, 30);
}

function makeSortable(tbody, cfg) {
  let d = null;                 // the press / drag in progress
  let justDragged = false;
  const motion = () => !(reduceMotion && reduceMotion.matches);

  function setT(g, y, lift) {
    const t = y || lift ? 'translateY(' + y + 'px)' + (lift && motion() ? ' scale(1.02)' : '') : '';
    g.els.forEach(e => { e.style.transform = t; });
  }
  function begin() {
    const rows = cfg.rows();
    const top0 = tbody.getBoundingClientRect().top;
    const gs = rows.map(tr => ({ key: cfg.key(tr), els: cfg.group(tr).filter(Boolean), shift: 0 }));
    gs.forEach(g => {                       // one read of every row, then writes only
      const r0 = g.els[0].getBoundingClientRect();
      let bottom = r0.bottom;
      g.els.forEach(e => { const r = e.getBoundingClientRect(); if (r.height) bottom = Math.max(bottom, r.bottom); });
      g.top = r0.top - top0;
      g.h = bottom - r0.top;
    });
    const i = gs.findIndex(g => g.key === d.key);
    if (i < 0) { d = null; return; }
    Object.assign(d, { started: true, gs, i, g: gs[i], target: i });
    if (window.getSelection) { try { window.getSelection().removeAllRanges(); } catch (e) { /* ok */ } }
    tbody.classList.add('sorting');
    if (!motion()) tbody.classList.add('nomotion');
    d.g.els.forEach(e => e.classList.add('lifted'));
    document.documentElement.classList.add('rowdrag');
    setT(d.g, 0, true);
  }
  function autoscroll() {
    const edge = 56, vh = window.innerHeight;
    let v = 0;
    if (d.cy < edge) v = -Math.ceil((edge - d.cy) / 3);
    else if (d.cy > vh - edge) v = Math.ceil((d.cy - (vh - edge)) / 3);
    d.scrollV = v;
    if (!v) return;
    const box = tbody.closest('.tablewrap');
    if (box && box.scrollHeight > box.clientHeight + 1
        && ((v < 0 && box.scrollTop > 0) || (v > 0 && box.scrollTop + box.clientHeight < box.scrollHeight - 1))) {
      box.scrollTop += v;
    } else window.scrollBy(0, v);
  }
  function frame() {
    if (!d || !d.started) return;
    d.raf = 0;
    autoscroll();
    const dy = (d.cy - tbody.getBoundingClientRect().top) - d.yStart;
    setT(d.g, dy, true);
    const center = d.g.top + d.g.h / 2 + dy;
    let target = 0;
    d.gs.forEach((g, k) => { if (k !== d.i && g.top + g.h / 2 < center) target += 1; });
    if (target !== d.target) {               // the rows passed slide now
      d.target = target;
      d.gs.forEach((g, k) => {
        if (k === d.i) return;
        const s = (d.i < k && k <= target) ? -d.g.h : (target <= k && k < d.i) ? d.g.h : 0;
        if (s !== g.shift) { g.shift = s; setT(g, s, false); }
      });
    }
    if (d.scrollV) d.raf = requestAnimationFrame(frame);
  }
  function finish(commit) {
    const { g, gs, i: from } = d;
    const to = commit ? d.target : from;
    cancelAnimationFrame(d.raf);
    let y = 0;                               // where the row settles
    if (to > from) for (let k = from + 1; k <= to; k++) y += gs[k].h;
    else for (let k = to; k < from; k++) y -= gs[k].h;
    const keys = gs.map(x => x.key);
    const order = to !== from ? moveTo(keys, from, to > from ? to + 1 : to) : null;
    if (!order) gs.forEach(x => { if (x !== g && x.shift) { x.shift = 0; setT(x, 0, false); } });
    const done = () => {
      tbody.classList.remove('sorting', 'settling', 'nomotion');
      gs.forEach(x => x.els.forEach(e => { e.style.transform = ''; e.classList.remove('lifted'); }));
      document.documentElement.classList.remove('rowdrag');
      if (order) cfg.commit(order, keys[from], to);
    };
    if (motion()) {
      tbody.classList.add('settling');
      setT(g, y, false);
      setTimeout(done, SLIDE_MS + 30);
    } else done();
  }

  tbody.addEventListener('pointerdown', e => {
    if (d || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const tr = e.target.closest(cfg.rowSel);
    if (!tr || !tbody.contains(tr) || e.target.closest(NO_DRAG)) return;
    d = { key: cfg.key(tr), id: e.pointerId, x0: e.clientX, y0: e.clientY, cy: e.clientY,
      yStart: e.clientY - tbody.getBoundingClientRect().top, started: false, raf: 0,
      touch: e.pointerType !== 'mouse' };
    if (d.touch) {
      d.timer = setTimeout(() => {
        if (!d || d.started) return;
        begin();
        if (d && navigator.vibrate) { try { navigator.vibrate(10); } catch (err) { /* ok */ } }
      }, LONG_PRESS_MS);
    }
  });
  window.addEventListener('pointermove', e => {
    if (!d || e.pointerId !== d.id) return;
    d.cy = e.clientY;
    if (!d.started) {
      if (Math.abs(e.clientY - d.y0) <= DRAG_PX && Math.abs(e.clientX - d.x0) <= DRAG_PX) return;
      if (d.touch) { clearTimeout(d.timer); d = null; return; }   // a swipe: it scrolls
      begin();
      if (!d) return;
    }
    if (!d.raf) d.raf = requestAnimationFrame(frame);
  }, { passive: true });
  const end = (commit) => e => {
    if (!d || e.pointerId !== d.id) return;
    clearTimeout(d.timer);
    if (d.started) {
      justDragged = true;
      setTimeout(() => { justDragged = false; }, 0);
      finish(commit);
    }
    d = null;
  };
  window.addEventListener('pointerup', end(true));
  window.addEventListener('pointercancel', end(false));
  document.addEventListener('keydown', e => {
    if (d && d.started && e.key === 'Escape') { e.preventDefault(); finish(false); d = null; }
  });
  // the click that ends a drag is not a click on the row
  tbody.addEventListener('click', e => { if (justDragged) { e.stopPropagation(); e.preventDefault(); } }, true);
  // a drag begun by a long press: the page must not scroll under the finger
  tbody.addEventListener('touchmove', e => { if (d && d.started) e.preventDefault(); }, { passive: false });
  tbody.addEventListener('selectstart', e => { if (d && d.started) e.preventDefault(); });
  tbody.addEventListener('contextmenu', e => { if (d && d.touch) e.preventDefault(); });
}

// A new order of the takeoff's lines (row ids). The results, the worksheet,
// the exports and saved worksheets follow it without pricing again.
function reorderLines(keys) {
  const by = new Map(S.lines.map(l => [l.row_id, l]));
  const seen = new Set(keys);
  S.lines = keys.map(k => by.get(k)).filter(Boolean).concat(S.lines.filter(l => !seen.has(l.row_id)));
  renderGrid();
  followOrder();
}

// A new order of the worksheet's lines: they take the takeoff's slots of
// the priced lines, in that order; lines not in the estimate keep theirs.
function reorderFromWorksheet(keys) {
  const inRes = new Set(keys);
  const by = new Map(S.lines.map(l => [l.row_id, l]));
  const queue = keys.map(k => by.get(k)).filter(Boolean);
  S.lines = S.lines.map(l => (inRes.has(l.row_id) ? queue.shift() : l)).filter(Boolean).concat(queue);
  renderGrid();
  followOrder();
}

function followOrder() {
  if (!S.result) return;
  const pos = new Map(S.lines.map((l, i) => [l.row_id, i]));
  const at = x => (pos.has(x.row_id) ? pos.get(x.row_id) : 1e9);
  const ord = (a, b) => at(a) - at(b);
  (S.result.lines || []).sort(ord);
  if (S.resultReq && Array.isArray(S.resultReq.items)) S.resultReq.items.sort(ord);
  if (S.result.not_covered && Array.isArray(S.result.not_covered.lines)) S.result.not_covered.lines.sort(ord);
  renderWorksheet(S.result);
  renderNotCovered(S.result);
}

function rowName(l) { return ((l.item_no ? l.item_no + ' ' : '') + (l.description || 'new row')).trim(); }

// Alt+Up / Alt+Down on a line of the takeoff grid
function moveLineBy(l, step) {
  const i = S.lines.indexOf(l);
  const j = i + step;
  if (i < 0 || j < 0 || j >= S.lines.length) return;
  S.lines = moveTo(S.lines, i, step > 0 ? j + 1 : j);
  renderGrid();
  followOrder();
  announce(rowName(l) + ' moved to position ' + (j + 1) + ' of ' + S.lines.length + '.');
}

// "Renumber items": 1, 2, 3 ... in the order shown -- or on from the first
// line's own number (101, 102 ...). Only when asked; prices do not change.
function renumberItems() {
  if (!S.lines.length) return;
  const m = /^\s*(\d+)\s*$/.exec(String(S.lines[0].item_no || ''));
  const start = m ? parseInt(m[1], 10) : 1;
  const map = new Map();
  S.lines.forEach((l, k) => { l.item_no = String(start + k); map.set(l.row_id, l.item_no); });
  if (S.result) {
    (S.result.lines || []).forEach(x => { if (map.has(x.row_id)) x.item_no = map.get(x.row_id); });
    if (S.resultReq && Array.isArray(S.resultReq.items)) {
      S.resultReq.items.forEach(x => { if (map.has(x.row_id)) x.item_no = map.get(x.row_id); });
    }
    renderResult();
  }
  renderGrid();
  renderQuotes();
  renderOwnerSet();
  toast('Items renumbered ' + start + '–' + (start + S.lines.length - 1) + ' in the order shown.', 'ok');
}

// "+ Add row", under the last row: a new line at the end, in view, ready
function addRow(col) {
  let id;
  do { id = 'r' + (++S.seq); } while (S.lines.some(x => x.row_id === id));
  S.lines.push(newLine({ row_id: id }));
  renderGrid(); markStale();
  const row = $('#review-body tr[data-row="' + CSS.escape(id) + '"]');
  if (!row) return;
  row.scrollIntoView({ block: 'nearest', behavior: motion0() ? 'smooth' : 'auto' });
  const cell = $('[data-col="' + (col || 'description') + '"]', row);
  if (cell) cell.focus();
}

function motion0() { return !(reduceMotion && reduceMotion.matches); }

function initSortable() {
  makeSortable($('#review-body'), {
    rowSel: 'tr[data-row]',
    rows: () => $$('#review-body tr[data-row]'),
    key: tr => tr.dataset.row,
    group: tr => [tr],
    commit: (order, key, to) => {
      reorderLines(order);
      const l = S.lines.find(x => x.row_id === key);
      if (l) announce(rowName(l) + ' moved to position ' + (to + 1) + ' of ' + S.lines.length + '.');
    },
  });
  makeSortable($('#result-body'), {
    rowSel: 'tr[data-line]',
    rows: () => $$('#result-body tr[data-line]'),
    key: tr => tr.dataset.line,
    group: tr => [tr, $('#result-body tr[data-detail="' + CSS.escape(tr.dataset.line) + '"]')],
    commit: (order, key, to) => {
      reorderFromWorksheet(order);
      const l = S.lines.find(x => x.row_id === key);
      if (l) announce(rowName(l) + ' moved to position ' + (to + 1) + '.');
    },
  });
  // Alt+Up / Alt+Down on a worksheet line (the row itself or a field in it)
  $('#result-table').addEventListener('keydown', e => {
    if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    const tr = e.target.closest && e.target.closest('tr[data-line]');
    if (!tr || !S.result) return;
    e.preventDefault();
    const keys = (S.result.lines || []).map(x => x.row_id);
    const i = keys.indexOf(tr.dataset.line);
    const j = i + (e.key === 'ArrowUp' ? -1 : 1);
    if (i < 0 || j < 0 || j >= keys.length) return;
    reorderFromWorksheet(moveTo(keys, i, e.key === 'ArrowUp' ? j : j + 1));
    const back = $('#result-body tr[data-line="' + CSS.escape(tr.dataset.line) + '"]');
    if (back) back.focus();
    const l = S.lines.find(x => x.row_id === tr.dataset.line);
    if (l) announce(rowName(l) + ' moved to position ' + (j + 1) + '.');
  });
  $('#btn-renumber').addEventListener('click', renumberItems);
}

/* --------------------------------------------- classification picker modal */

function openPicker(l) {
  const host = $('#modal-host');
  const fams = (S.options && S.options.families) || [];
  const cur = l.cls ? l.cls.family : '';
  const famSel = el('select', {}, [el('option', { value: '', text:
    'Not sure \u2014 keep our reading' + (l.cls ? ' (' + (l.cls.family_parser_label
      || l.cls.family_parser) + ')' : '') }),
    el('option', { value: '__new_item__', text: 'New item / Other — Engineer input',
      selected: l.pricing_mode === 'engineer' })]
    .concat(fams.filter(f => !['other', 'needs_review'].includes(f.id)).map(f => el('option', { value: f.id, text: f.label,
      selected: l.pricing_mode !== 'engineer' && !!l.family && f.id === l.family }))));
  const list = el('div', {});
  const qinput = el('input', { type: 'text', value: l.description || '',
    placeholder: 'search earlier public line items…' });

  function apply(family, unit) {
    if (family === '__new_item__') { close(); markNewItem(l); return; }
    const c0 = l.cls || {};
    const unitMissing = !c0.unit || c0.unit_known === false;
    l.family = family || '';
    l.pricing_mode = '';
    l.remembered = null;
    if (family) {
      l.family_src = 'user';
      rememberCorrection(l, family, unit && unitMissing ? unit : '', 'picker');
    } else {
      l.family_src = 'parser';       // keep the parser's, and forget any memory
      forgetCorrection(l);
    }
    if (unit) l.unit = unit;
    close();
    markStale();
    classifyOne(l);
    const lab = family ? ((fams.find(f => f.id === family) || {}).label || family)
      : (c0.family_parser_label || 'our reading');
    toast('Row ' + (l.item_no || '') + ': item type set to ' + lab
      + (unit ? ', unit ' + unit : '') + '.', 'ok');
  }

  function paint(cands) {
    list.innerHTML = '';
    if (!cands.length) {
      list.appendChild(el('div', { class: 'muted-note', text: 'Nothing matched. Try the '
        + 'material and size, or choose New item / Other above to enter your own worksheet price.' }));
      return;
    }
    cands.forEach(c => list.appendChild(el('div', { class: 'pick' + (c.item_family === cur ? ' sel' : '') }, [
      el('span', { class: 'lab' }, [
        el('span', { class: 't', text: c.label || '(unnamed)' }),
        el('span', { class: 's', text: [c.family_label, c.unit, c.detail,
          c.median_price ? 'median ' + rate(c.median_price) + '/' + c.unit : ''].filter(Boolean).join(' · ') }),
      ]),
      el('span', { class: 'badge ' + (c.n_projects >= 5 ? 'ok' : c.n_projects >= 3 ? 'thin' : 'no'),
        text: c.n_projects >= 5 ? 'plenty of bids' : c.n_projects >= 3 ? 'few bids' : 'too few bids' }),
      el('button', { class: 'btn sm', text: 'Use this item type',
        title: 'Set this line’s item type to ' + c.family_label,
        onclick: () => apply(c.item_family, '') }),
      c.unit && c.unit !== (l.cls && l.cls.unit) ? el('button', { class: 'btn sm',
        text: 'Use this item type and its unit (' + c.unit + ')',
        onclick: () => apply(c.item_family, c.unit) }) : null,
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
        el('p', { class: 'modal-intro', text: 'Each line is matched to an item type (for example '
          + 'Excavation, Seeding and sod, Stone riprap). The price is compared with earlier bids for '
          + 'the same item type and unit. Change it only if the match is wrong. Normal item-type '
          + 'choices are remembered for this wording in this browser.' }),
        el('dl', { class: 'kv', style: 'margin-bottom:11px' }, [
          el('dt', { text: 'Our reading' }), el('dd', { text: c.family_parser_label || c.family_parser || '—' }),
          el('dt', { text: 'Unit' }), el('dd', { text: c.unit || '—' }),
          el('dt', { text: 'Size' }), el('dd', { text: c.size_bucket
            ? c.size_bucket + ' (' + c.size_in + ')' : 'none parsed' }),
          el('dt', { text: 'Read as' }), el('dd', { text: c.desc_norm || '' }),
        ]),
        el('label', { class: 'field' }, [
          el('span', { class: 'lab', text: 'Item type' }),
          el('div', { class: 'inline classification-type' }, [famSel,
            el('button', { class: 'btn sm primary', text: 'Apply',
              onclick: () => apply(famSel.value, '') })]),
          el('span', { class: 'sub', text: 'The item type decides which earlier bids this line is '
            + 'compared with (after lines worded alike). The unit is edited in the grid.' }),
        ]),
        el('label', { class: 'field' }, [
          el('span', { class: 'lab', text: 'Search earlier public line items' }),
          qinput,
          el('span', { class: 'sub', text: 'City bid tabs (DFW) and the TxDOT price book, '
            + 'public bids only. \u201cUse this item type\u201d takes that item\u2019s type. '
            + '\u201cPlenty of bids\u201d: 5 or more earlier projects; \u201cfew\u201d: 3\u20134; '
            + '\u201ctoo few\u201d: under 3.' }),
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
  $('#in-owner').addEventListener('input', onOwnerInput);
  $('#in-owner').addEventListener('change', () => { onOwnerInput(); markStale(); });
  $('#in-worktype').addEventListener('input', onWorkTypeInput);
  $('#in-worktype').addEventListener('change', () => { onWorkTypeInput(); markStale(); suggestBidders(); });
  $('#in-biddate').addEventListener('change', () => { markStale(); suggestBidders(); });
  $('#in-bidders').addEventListener('input', () => {
    const input = $('#in-bidders');
    S.bidders.mode = input.value.trim() || input.validity.badInput ? 'user' : 'auto';
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
    refreshEstimateButton();
  });
  $('#btn-estimate').addEventListener('click', () => withBusy($('#btn-estimate'), runEstimate));
  $('#btn-reestimate').addEventListener('click', () => withBusy($('#btn-reestimate'), runEstimate));
}


/* ---------------------------------------- owner / work type: type-ahead */

const OWNER_OTHER = 'Other — not in the list';
const WT_OTHER = 'Other / not listed';

function normName(x) { return String(x || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }

// Dice similarity on letter pairs, for "Did you mean ...?"
function similarity(a, b) {
  a = normName(a); b = normName(b);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const pairs = s => { const out = []; for (let i = 0; i < s.length - 1; i++) out.push(s.slice(i, i + 2)); return out; };
  const pa = pairs(a), pb = pairs(b);
  if (!pa.length || !pb.length) return 0;
  let hit = 0;
  const pool = pb.slice();
  pa.forEach(p => { const k = pool.indexOf(p); if (k >= 0) { hit++; pool.splice(k, 1); } });
  return 2 * hit / (pa.length + pb.length);
}

// {id, text, known, suggestion} for a typed / picked value of a list
function resolveChoice(value, list) {
  const v = String(value || '').trim();
  if (!v) return { id: '', text: '', known: false, suggestion: null };
  const n = normName(v);
  const hit = list.find(e => normName(e.label) === n || normName(e.id) === n
    || (e.ids || []).some(i => normName(i) === n));
  if (hit) return { id: hit.id, text: hit.label, known: true, suggestion: null };
  let best = null, bs = 0;
  list.forEach(e => {
    const sc = Math.max(similarity(v, e.label), normName(e.label).startsWith(n) && n.length >= 3 ? 0.9 : 0);
    if (sc > bs) { bs = sc; best = e; }
  });
  return { id: '', text: v, known: false, suggestion: bs >= 0.6 ? best : null };
}

function mkt() { return S.market === 'not_sure' ? 'municipal' : S.market; }
function ownerList() { return ((S.options || {}).owners || {})[mkt()] || []; }
function workTypeList() { return ((S.options || {}).work_types || {})[mkt()] || []; }

function renderChoiceNote(input, note, list, other, defaultText, unknownText) {
  const r = resolveChoice(input.value, list);
  // Unchanged value and list: leave the note alone. The change event that
  // fires when the field loses focus -- e.g. on the press of "Did you mean
  // ...?" itself -- used to rebuild the note and swallow that click.
  const key = mkt() + '\u0001' + list.length + '\u0001' + input.value;
  if (note.dataset.key === key) return r;
  note.dataset.key = key;
  note.innerHTML = '';
  if (!input.value.trim()) { note.textContent = defaultText; return r; }
  if (r.known) {
    const e = list.find(x => x.id === r.id);
    note.textContent = e ? e.label + ': ' + e.lettings + ' past job' + (e.lettings === 1 ? '' : 's')
      + ' in the price history' + ((e.ids || []).length > 1 ? ' (' + e.ids.length + ' sources merged)' : '') + '.'
      : '';
    return r;
  }
  note.appendChild(el('span', { class: 'warnnote', text: unknownText }));
  if (r.suggestion) {
    note.appendChild(document.createTextNode(' '));
    note.appendChild(el('button', { class: 'btn ghost sm', type: 'button',
      text: 'Did you mean ' + r.suggestion.label + '?',
      onclick: () => { input.value = r.suggestion.label; input.dispatchEvent(new Event('change')); } }));
  }
  return r;
}

function onOwnerInput() {
  const inp = $('#in-owner');
  if (inp.value === OWNER_OTHER) { inp.value = ''; inp.placeholder = 'type the owner’s name'; inp.focus(); }
  renderChoiceNote(inp, $('#owner-note'), ownerList(), OWNER_OTHER,
    'Choose from the list or type an owner.',
    'Not in the price history — no owner-specific weighting.');
}

function onWorkTypeInput() {
  const inp = $('#in-worktype');
  if (inp.value === WT_OTHER) { inp.value = ''; inp.placeholder = 'type the work type'; inp.focus(); }
  renderChoiceNote(inp, $('#worktype-note'), workTypeList(), WT_OTHER,
    'Choose from the list or type a work type.',
    'Not in the price history — bidder count uses market and size.');
}

function fillMarketLists() {
  const o = S.options;
  if (!o) return;
  const fill = (dl, rows, other) => {
    dl.innerHTML = '';
    rows.forEach(r => dl.appendChild(el('option', { value: r.label,
      label: r.lettings + ' past job' + (r.lettings === 1 ? '' : 's') + ' on record' })));
    dl.appendChild(el('option', { value: other }));
  };
  fill($('#owner-list'), ownerList(), OWNER_OTHER);
  fill($('#worktype-list'), workTypeList(), WT_OTHER);
  onOwnerInput();
  onWorkTypeInput();
}

function biddersValue() {
  const v = Number($('#in-bidders').value);
  return Number.isInteger(v) && v >= 1 && v <= 60 ? v : null;
}

const BIDDERS_HINT = 'Enter a whole number of bidders from 1 to 60, or clear the field to use the suggestion.';
function invalidBidders() { return S.bidders.mode === 'user' && biddersValue() === null; }

function renderDiscountPreview() {
  const box = $('#discount-preview');
  box.innerHTML = '';
  const o = S.options;
  const n = biddersValue();
  if (!o || !n) return;
  const d = (o.discount || {})[mkt()];
  if (!d || !d.rows.length) return;
  let row = null, kind = 'ok', msg;
  if (n <= 1) {
    kind = 'caution';
    msg = 'One bid is not a competition: no discount is applied (f = 1.00).';
  } else {
    row = d.rows.reduce((a, b) => Math.abs(b.n_bidders - n) < Math.abs(a.n_bidders - n) ? b : a);
    const top = d.rows.reduce((a, b) => (b.n_bidders > a.n_bidders ? b : a));
    if (o.clamp_above_top_bin && n > top.n_bidders) {
      // round 18: more bidders than any past job is priced off the top count
      kind = 'caution';
      msg = 'More bidders than any past job in the table: priced as ' + top.n_bidders
        + ' bidders, where the low bid is typically ' + top.f.toFixed(2) + ' \u00d7 the median bid ('
        + top.lettings + ' past jobs).';
    } else if (Math.abs(row.n_bidders - n) > (o.max_bin_distance || 2)) {
      kind = 'severe';
      msg = 'No past jobs with about ' + n + ' bidders: the estimator will not guess '
        + 'the competition effect, so there will be no estimate.';
    } else {
      msg = 'With ' + n + ' bidders the low bid is typically ' + row.f.toFixed(2)
        + ' \u00d7 the median bid (' + row.lettings + ' past jobs'
        + (row.n_bidders !== n ? ' with ' + row.n_bidders + ' bidders, the nearest count' : '') + ').'
        + (S.market === 'txdot' ? ' TxDOT jobs may use a size-aware discount instead.' : '');
    }
  }
  box.appendChild(el('div', { class: 'disc ' + kind, text: msg }));
}

const suggestBidders = debounce(async () => {
  if (!S.options || !S.lines.some(l => l.include && l.description)) return;
  const req = estimateRequest('auto');
  if (!req.items.length) return;       // every line still needs a quantity
  try {
    const sug = await postJSON('/api/bidders/suggest', req);
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
  $('#in-bidders').setAttribute('aria-invalid', String(invalidBidders()));
  if (invalidBidders()) {
    h.textContent = BIDDERS_HINT;
    return;
  }
  if (!s) {
    h.textContent = 'Prefilled from earlier jobs once the takeoff is read. Edit it if you know better.';
    return;
  }
  const src = s.cell === 'default' ? 'too few similar earlier jobs, so the usual 4'
    : 'the typical number on ' + s.history_lettings + ' earlier ' + s.cell_label + ', bid before ' + s.before;
  h.innerHTML = '';
  h.appendChild(el('span', { text: (S.bidders.mode === 'user' ? 'You entered '
    + biddersValue() + '. The suggestion is ' + s.n + ': ' : 'Suggested ' + s.n + ': ')
    + src + '. ' }));
  h.appendChild(el('span', { class: 'help', text: '?', title: 'Rule N1 (registered '
    + '2026-11-20): the median bidder count of earlier lettings of the same market × '
    + 'work type × size class (this schedule’s own model total, '
    + short$(s.size_total) + ' → ' + s.size_class + '); fallbacks: market × size, '
    + 'market, all markets (10 lettings each), else 4. ' + (s.note || '') }));
}

function renderQuotes() {
  const box = $('#quotes-box');
  const focus = focusState();
  renderQuotesInto(box);
  restoreFocus(focus);
}

function renderQuotesInto(box) {
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
    + 'P50 / P70. Enter a vendor quote for each to get a complete total.' }));
  unc.forEach(l => box.appendChild(el('div', { class: 'qrow' }, [
    el('div', { class: 'qdesc' }, [
      el('div', { text: (l.item_no ? l.item_no + '  ' : '') + l.description }),
      el('div', { class: 'dim', text: l.cls.coverage_label + ' · ' + qtyFmt(l.quantity)
        + ' ' + (l.cls.bid_form_unit || l.cls.unit || '') }),
    ]),
    el('input', { type: 'text', class: 'num', placeholder: 'quote $',
      value: l.quote > 0 ? qtyFmt(l.quote) : '', 'data-fk': focusKey('q', l.row_id),
      onchange: e => {
        // the same number as the line's amount cell and your worksheet price
        setNcQuote(l.row_id, parseNum(e.target.value));
        e.target.value = l.quote > 0 ? qtyFmt(l.quote) : '';
      } }),
  ])));
}

function renderOwnerSet() {
  const box = $('#ownerset-box');
  if (!box) return;
  const focus = focusState();
  renderOwnerSetInto(box);
  restoreFocus(focus);
}

function renderOwnerSetInto(box) {
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
    + 'A historical price is not meaningful for an allowance. A contingency is picked from '
    + 'the list: a share of the subtotal (10% unless you change it), or the owner\u2019s '
    + 'fixed amount when the bid form prints one.' }));
  al.forEach(l => {
    const cont = isContingencyLine(l);
    const given = l.amount > 0 || l.amount_pct > 0;
    const note = l.amount > 0 ? (l.amount_src === 'file' ? ' \u00b7 from the uploaded file' : '')
      : l.amount_pct > 0 ? ' \u00b7 ' + l.amount_pct + '% of the subtotal'
        + ' (worked out when you estimate)'
      : ' \u00b7 amount missing';
    box.appendChild(el('div', { class: 'qrow owner' + (given ? '' : ' missing') }, [
      el('div', { class: 'qdesc' }, [
        el('div', { text: (l.item_no ? l.item_no + '  ' : '') + l.description }),
        // a contingency: what it is, without a unit or quantity (the list
        // next to it says the share)
        el('div', { class: 'dim', text: cont ? 'Contingency' + (l.amount > 0 ? note
          : l.amount_pct > 0 ? ' · worked out when you estimate' : note)
          : qtyFmt(l.quantity) + ' ' + (l.cls.bid_form_unit || l.cls.unit || '') + note }),
      ]),
      cont ? contingencyControl(l, 'o') : el('input', { type: 'text', class: 'num', placeholder: 'amount $',
        title: 'The owner\u2019s amount from the bid form, or a share of the subtotal such as "10%"',
        value: amountCellText(l), 'data-fk': focusKey('o', l.row_id),
        onchange: e => {
          const r = parseAmountOrPct(e.target.value);
          l.amount = r.amount || null;
          l.amount_pct = r.pct || null;
          l.amount_src = 'user';
          e.target.value = amountCellText(l);
          markStale(); afterInput(renderGrid); afterInput(renderOwnerSet); suggestBidders();
          afterInput(() => renderTopAlerts(S.result));
        } }),
    ]));
  });
}

function estimateRequest(n) {
  const ow = resolveChoice($('#in-owner').value, ownerList());
  const wt = resolveChoice($('#in-worktype').value, workTypeList());
  return {
    market: S.market,
    owner: ow.id, owner_text: ow.known ? '' : ow.text,
    work_type: wt.id, work_type_text: wt.known ? '' : wt.text,
    bid_date: $('#in-biddate').value || null,
    n_bidders: n,
    // a line given as a share of the rest (isContingencyLine) with no unit
    // or quantity goes as the bid form writes one: 1 LS
    items: S.lines.filter(l => l.include && !missingQuantity(l)).map(l => {
      const share = isContingencyLine(l);
      return {
        row_id: l.row_id, item_no: l.item_no, description: l.description,
        unit: share && !String(l.unit || '').trim() ? 'LS' : l.unit,
        quantity: share && !(l.quantity > 0) ? 1 : l.quantity,
        family: l.family || '', amount: l.amount, quote: l.quote,
        pricing_mode: l.pricing_mode || '',
        amount_pct: l.amount > 0 ? null : (l.amount_pct > 0 ? l.amount_pct : null),
      };
    }),
  };
}

function leftOutText(ls) {
  return ls.length + ' line' + (ls.length === 1 ? ' has' : 's have') + ' no quantity and '
    + (ls.length === 1 ? 'is' : 'are') + ' left out of the estimate until you fill '
    + (ls.length === 1 ? 'it' : 'them') + ' in: '
    + ls.map(l => ((l.item_no ? l.item_no + ' ' : '') + l.description).trim()).join('; ') + '.';
}

function refreshEstimateButton() {
  const b = $('#btn-estimate');
  const inc = S.lines.filter(l => l.include);
  const out = inc.filter(missingQuantity);
  const n = inc.length - out.length;
  const ready = !!(S.status && S.status.ready && S.options);
  b.disabled = !(ready && n) || invalidBidders();
  $('#estimate-hint').textContent = !ready ? 'Waiting for the estimator to load\u2026'
    : invalidBidders() ? BIDDERS_HINT
    : !inc.length ? 'Read a takeoff first.'
      : !n ? 'Every line still needs a quantity.'
        : out.length ? leftOutText(out) : '';
}

function markStale() {
  if (!S.result) return;
  S.stale = true;
  $('#stale-banner').hidden = false;
}

/* ================================================================= RESULTS */

async function runEstimate() {
  if (invalidBidders()) {
    toast(BIDDERS_HINT, 'err', 9000);
    return;
  }
  if (S.parse && S.parse.mock) {
    toast('These line items are mock OCR output and will not be priced.', 'err', 9000);
    return;
  }
  const req = estimateRequest(S.bidders.mode === 'user' && biddersValue() ? biddersValue() : 'auto');
  const r = await postJSON('/api/estimate', req);
  const currentReq = estimateRequest(S.bidders.mode === 'user' && biddersValue() ? biddersValue() : 'auto');
  if (JSON.stringify(currentReq) !== JSON.stringify(req)) {
    markStale();
    toast('Inputs changed while estimating. Click Estimate again to use the latest items.', 'err', 9000);
    return;
  }
  const requested = new Map(req.items.map(l => [l.row_id, l]));
  const previous = new Map(((S.result || {}).lines || []).map(l => [l.row_id, l]));
  (r.lines || []).forEach(l => {
    l.pricing_mode = (requested.get(l.row_id) || {}).pricing_mode || '';
    const w = S.ws.lines[l.row_id];
    if (l.pricing_mode === 'engineer' && (previous.get(l.row_id) || {}).pricing_mode !== 'engineer'
        && w && !['you', 'quote', 'owner-set'].includes(w.src)) {
      w.src = ''; w.src_label = ''; w.your_unit = null; w.your_amount = null; w.raw = null; w.src_date = '';
    }
  });
  S.result = r;
  S.resultReq = req;
  S.leftOut = S.lines.filter(l => l.include && missingQuantity(l));
  S.ws.refs = null;
  S.ws.refsReq = null;
  S.ws.refsError = '';
  if (worksheetBasis() === 'history') Object.values(S.ws.lines).forEach(w => {
    if (w.src === 'history') {
      w.your_unit = null; w.raw = null; w.history_price = null; w.src_label = ''; w.src_date = '';
    }
  });
  S.stale = false;
  $('#stale-banner').hidden = true;
  if (S.bidders.mode === 'auto') {
    $('#in-bidders').value = r.competition.n_bidders;
    if (r.competition.suggestion) S.bidders.suggestion = r.competition.suggestion;
    renderBiddersHint();
    renderDiscountPreview();
  }
  renderResult();
  showRowHint();
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

// A not-covered line has your number once you gave one: a vendor quote
// (Project details) or a price of yours on the worksheet. The reminders --
// the headline note, the alert, the P50/P70 "not included" line, the panel's
// "needs a quote", the worksheet's "not covered" -- are for the lines still
// without one (user, 2026-11-21: "if I filled in a number, it should go").
function ncNumber(l) {
  if (!l) return null;
  if (l.quote > 0) return Number(l.quote);
  const w = S.ws && S.ws.lines ? S.ws.lines[l.row_id] : null;
  return w ? wsAmount(l, wsLine(l)) : null;
}

function ncOpen(r) {
  return ((r && r.lines) || []).filter(l => (l.status === 'not_covered' || l.status === 'not_covered_whole')
    && ncNumber(l) === null);
}

// A small box beside a badge, on hover or focus (a tap on a phone): one
// box for the page, placed in the viewport so no table clips it (user,
// 2026-11-21: past prices for a not-covered line in a box, not a line).
let hintAnchor = null;
function placeHint() {
  const box = $('#hint-pop');
  if (!box || box.hidden) return;
  const r = hintAnchor && document.body.contains(hintAnchor) ? hintAnchor.getBoundingClientRect() : null;
  if (!r || r.bottom < 0 || r.top > window.innerHeight) { box.hidden = true; hintAnchor = null; return; }
  const w = Math.min(360, window.innerWidth - 24);
  box.style.width = w + 'px';
  box.style.left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12) + 'px';
  box.style.top = (r.bottom + 6) + 'px';
  const h = box.getBoundingClientRect().height;
  if (r.bottom + 6 + h > window.innerHeight - 8) box.style.top = Math.max(8, r.top - h - 6) + 'px';
}
function hintPop(anchor, build) {
  anchor.classList.add('hashint');
  anchor.setAttribute('tabindex', '0');
  const show = () => {
    let box = $('#hint-pop');
    if (!box) { box = el('div', { id: 'hint-pop', role: 'tooltip' }); document.body.appendChild(box); }
    box.innerHTML = '';
    box.appendChild(build());
    hintAnchor = anchor;
    box.hidden = false;
    placeHint();
  };
  const hide = () => {
    if (hintAnchor !== anchor) return;
    const box = $('#hint-pop');
    if (box) box.hidden = true;
    hintAnchor = null;
  };
  anchor.addEventListener('mouseenter', show);
  anchor.addEventListener('mouseleave', hide);
  anchor.addEventListener('focus', show);
  anchor.addEventListener('blur', hide);
  anchor.addEventListener('click', e => { e.stopPropagation(); show(); });
  return anchor;
}
// the box follows its badge when the page scrolls, and goes when it leaves
document.addEventListener('scroll', placeHint, true);
window.addEventListener('resize', placeHint);
// a tap elsewhere closes it (phones have no hover)
document.addEventListener('click', e => {
  if (hintAnchor && !hintAnchor.contains(e.target)) { const box = $('#hint-pop'); if (box) box.hidden = true; hintAnchor = null; }
}, true);

// What past public jobs paid for a line like a not-covered one: a rough idea
// only, never in P50 / P70.
function ncHintBox(h, label) {
  const unit = (h && h.unit) || 'unit';
  const kids = [el('div', { class: 'hp-title', text: 'Past public bids: ' + (label || 'this kind of item') })];
  if (!h) {
    kids.push(el('div', { text: 'Still loading the past bids; try again in a moment.' }));
  } else if (!h.n_items) {
    kids.push(el('div', { text: 'No past public job priced a line like this per ' + unit + '.' }));
  } else {
    const y = h.years ? (h.years[0] === h.years[1] ? h.years[0] : h.years[0] + '\u2013' + h.years[1]) : '';
    kids.push(el('div', { text: h.n_items + ' line' + (h.n_items === 1 ? '' : 's') + ' in ' + h.n_jobs
      + ' past job' + (h.n_jobs === 1 ? '' : 's') + (y ? ', bid ' + y : '') + (h.escalated ? ', in ' + h.to_year + ' dollars' : '') + ':' }));
    kids.push(el('div', { class: 'hp-num', text: h.n_items >= 4
      ? 'middle half ' + money(h.p25) + ' \u2013 ' + money(h.p75) + ' per ' + unit + ', median ' + money(h.median)
      : money(h.low) + (h.high !== h.low ? ' \u2013 ' + money(h.high) : '') + ' per ' + unit }));
    if (h.examples && h.examples.length) kids.push(el('ul', { class: 'hp-ex' }, h.examples.map(e => el('li', {
      text: e.project + (e.year ? ' (' + String(e.year).slice(0, 4) + ')' : '') + ': \u201c' + e.item + '\u201d, '
        + (isNum(e.quantity) ? qtyFmt(e.quantity) + ' ' + unit + ' at ' : '') + money(e.unit_price) + ' per ' + unit }))));
  }
  kids.push(el('div', { class: 'hp-foot', text: 'A rough idea only, not in P50 / P70: size and scope vary a lot. Get a vendor quote.' }));
  return el('div', {}, kids);
}

// One number per not-covered line (user, 2026-11-21: "fill one, the other
// follows"): the vendor quote under Project details, the line's amount cell
// in the takeoff and your price on the worksheet are the same number, kept on
// the line as its quote. It stays outside P50 / P70, so a change needs no new
// estimate: the result and the request it came from take it too.
function ncTakeoffLine(l) {
  return !!(l && l.cls && l.cls.coverage_class && !l.cls.owner_set_like);
}
function ncAdopt(rowId, v) {
  const q = isNum(v) && v > 0 ? Math.round(Number(v) * 100) / 100 : null;
  const tl = S.lines.find(x => x.row_id === rowId);
  if (tl) { tl.quote = q; tl.amount = null; tl.amount_pct = null; }
  const rl = S.result ? (S.result.lines || []).find(x => x.row_id === rowId) : null;
  if (rl) rl.quote = q;
  const ri = S.resultReq ? (S.resultReq.items || []).find(x => x.row_id === rowId) : null;
  if (ri) { ri.quote = q; ri.amount = null; }
  const w = S.ws && S.ws.lines ? S.ws.lines[rowId] : null;
  if (w) {
    w.src = q ? 'quote' : ''; w.src_label = q ? 'vendor quote' : '';
    w.your_amount = q; w.your_unit = null; w.raw = null; w.src_date = '';
  }
  return q;
}
function setNcQuote(rowId, v) {
  ncAdopt(rowId, v);
  afterInput(renderGrid); afterInput(renderQuotes);
  if (S.result) afterInput(renderWsNow);
}
// the takeoff's amount cell on a not-covered line: the same quote
function ncQuoteInput(l, i) {
  return el('input', {
    class: 'cellinput num' + (l.quote > 0 ? '' : ' hintfill'), type: 'text',
    value: l.quote > 0 ? qtyFmt(l.quote) : '', 'data-col': 'amount', 'data-i': i,
    'data-fk': focusKey('g', l.row_id, 'quote'), placeholder: 'quote $',
    title: 'Not covered: your vendor quote for the whole line. The same number as under Project '
      + 'details and your price on the OPCC worksheet; it stays outside P50 / P70.',
    onchange: e => {
      setNcQuote(l.row_id, parseNum(e.target.value));
      e.target.value = l.quote > 0 ? qtyFmt(l.quote) : '';
    },
  });
}

// the parts of the result that speak of the not-covered lines, again (a
// worksheet price of yours can settle one)
function renderNcReminders() {
  const r = S.result;
  if (!r) return;
  renderHeadline(r);
  renderWarnings(r);
  renderNotCovered(r);
}

function renderResult() {
  const r = S.result;
  if (!r) return;
  const i = r.inputs;
  $('#result-sub').textContent = i.market_label + (i.market_note ? ' (market not given)' : '')
    + (i.owner_label ? ' \u00b7 ' + i.owner_label : '')
    + (i.work_type_label ? ' · ' + i.work_type_label : '')
    + ' · bid ' + (i.bid_date || 'date not given') + ' · ' + i.n_bidders + ' bidders ('
    + (i.bidders_source === 'rule N1' ? 'suggested' : 'yours') + ')';
  renderHeadline(r);
  renderWarnings(r);
  renderNotCovered(r);
  renderLowConf(r);
  renderLines(r);
  renderCompetition(r);
  renderSimilar(r);
}

// The alerts above the estimate follow what has been filled in since (user,
// 2026-11-21: "I filled the amount in and it is still there"): an owner-set
// allowance given its amount leaves the red alert at once, and a short note
// asks for Re-estimate to put it into P50 and P70.
function renderTopAlerts(r, box) {
  box = box || $('#r-top-alerts');
  if (!box || !r) return;
  box.innerHTML = '';
  const byId = new Map(S.lines.map(l => [l.row_id, l]));
  const given = x => { const l = byId.get(x.row_id); return !!(l && (l.amount > 0 || l.amount_pct > 0)); };
  (r.alerts || []).filter(a => a.placement === 'above_headline').forEach(a => {
    if (a.code === 'cable_unit') {
      box.appendChild(notice('caution', a.title, a.text));
      return;
    }
    if (a.code === 'allowance_missing') {
      const miss = (r.lines || []).filter(x => x.amount_missing);
      const still = miss.filter(x => !given(x));
      if (miss.length && !still.length) {
        box.appendChild(notice('info', null, 'Allowance amount' + (miss.length === 1 ? '' : 's')
          + ' entered. Press Re-estimate to add ' + (miss.length === 1 ? 'it' : 'them') + ' to P50 and P70.'));
        return;
      }
      if (still.length < miss.length) {
        a = Object.assign({}, a, { more: 0,
          title: still.length + ' owner-set allowance' + (still.length === 1 ? ' has' : 's have')
            + ' no amount, so the totals are too low.',
          items: still.map(x => rowName(byId.get(x.row_id) || x)) });
      }
    }
    box.appendChild(a.level === 'info' ? notice('info', null, a.text) : ackNotice(a));
  });
  // a worksheet line whose allowance now has its amount loses its red mark too
  document.querySelectorAll('#result-body tr.st-amount-missing').forEach(tr => {
    const x = (r.lines || []).find(y => y.row_id === tr.dataset.line);
    if (x && given(x)) tr.classList.remove('st-amount-missing');
  });
}

function renderHeadline(r) {
  const host = $('#r-headline');
  host.innerHTML = '';
  const top = el('div', { id: 'r-top-alerts' });
  host.appendChild(top);
  renderTopAlerts(r, top);
  const h = r.headline;
  const nc = r.not_covered;
  if (h.p50 === null || h.p50 === undefined) {
    host.appendChild(notice('severe', nc.as_a_whole ? 'No estimate: the schedule is not covered as a whole'
      : 'No estimate', nc.as_a_whole ? (nc.why || []).join('; ')
        + ' — price the job from vendor / specialty quotes.'
      : 'The estimator could not price this schedule; see the warnings below.'));
    return;
  }
  // Project totals are comparisons, not controls for repricing the worksheet.
  host.appendChild(el('div', { class: 'headline' }, [
    el('div', { class: 'bigcard', 'data-basis': 'p50' }, [
      el('div', { class: 'cap', text: 'Expected low bid (P50)' }),
      el('div', { class: 'big', text: money(h.p50) }),
    ]),
    el('div', { class: 'bigcard lead', 'data-basis': 'p70' }, [
      el('div', { class: 'cap', text: 'Recommended OPCC (P70)' }),
      el('div', { class: 'big', text: money(h.p70) }),
    ]),
  ]));
  if (!isNum(h.p70)) host.appendChild(notice('caution', 'P70 unavailable',
    'A P70 estimate is not available for this schedule. Review the P50 comparison and your worksheet prices.'));
}

// Alerts are decided on the result's keys by the server; the estimator's own
// warnings follow verbatim as notes (their wording is never parsed here).
function renderWarnings(r) {
  const host = $('#r-warnings');
  host.innerHTML = '';
  const lo = S.leftOut || [];
  if (lo.length) host.appendChild(ackNotice({ level: 'caution', code: 'left_out',
    title: lo.length + ' line' + (lo.length === 1 ? ' has' : 's have') + ' no quantity and '
      + (lo.length === 1 ? 'is' : 'are') + ' left out of the estimate.',
    action: 'Fill in a quantity in the takeoff.', items: lo.map(rowName) }));
  const open = ncOpen(r);
  (r.alerts || []).filter(a => a.placement !== 'above_headline')
    // the not-covered alert names only the lines still without your number
    .filter(a => a.code !== 'not_covered' || open.length)
    .forEach(a => {
      if (a.code === 'not_covered') {
        a = Object.assign({}, a, { more: 0,
          title: open.length + ' line' + (open.length === 1 ? ' has' : 's have') + ' no bid history and '
            + (open.length === 1 ? 'is' : 'are') + ' left out of P50 and P70.',
          items: open.map(l => String(l.description || l.item_no || '').slice(0, 60)) });
      }
      host.appendChild(a.level === 'info' ? notice('info', null, a.text) : ackNotice(a));
    });
  const ws = r.warnings || [];
  if (!ws.length) return;
  host.appendChild(el('details', { class: 'fold warnfold' }, [
    el('summary', { text: 'Calculation notes (' + ws.length + ')' }),
    el('ul', { class: 'warnlist' }, ws.map(w => el('li', { text: w }))),
  ]));
}

function renderNotCovered(r) {
  const nc = r.not_covered;
  const host = $('#r-notcovered');
  host.innerHTML = '';
  $('#nc-sub').textContent = nc.n ? nc.n + ' line' + (nc.n === 1 ? '' : 's') : 'none';
  if (!nc.n) {
    host.appendChild(el('div', { class: 'muted-note', text: 'No not-covered lines need a vendor quote.' }));
    return;
  }
  const open = ncOpen(r);
  host.appendChild(open.length
    ? notice('caution', 'The expected low bid and the recommended OPCC above EXCLUDE these lines.',
      'Enter a vendor quote under Project details or a unit price in the worksheet.')
    : el('div', { class: 'muted-note', text: 'Every not-covered line has your number. They stay outside '
      + 'P50 and P70 (no comparable bid history); your numbers are added in the worksheet.' }));
  const byId = new Map((r.lines || []).map(x => [x.row_id, x]));
  host.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, ['Item', 'Description', 'Class', 'Qty', 'Unit', 'Vendor quote']
      .map((h, k) => el('th', { class: k >= 3 && k !== 4 ? 'num' : '', text: h })))),
    el('tbody', {}, nc.lines.map(l => el('tr', {}, [
      el('td', { text: l.item_no }), el('td', { text: l.description }),
      el('td', {}, hintPop(el('span', { text: l.class_label }),
        () => ncHintBox((byId.get(l.row_id) || {}).nc_hint, l.class_label))),
      el('td', { class: 'num', text: qtyFmt(l.quantity) }),
      el('td', { text: l.unit }),
      // the line's one number (setNcQuote), as it stands now
      el('td', { class: 'num', text: ncNumber(byId.get(l.row_id)) !== null
        ? money(ncNumber(byId.get(l.row_id))) : 'needs a quote' }),
    ]))),
  ]));
  // the complete total from the same numbers, so it follows every change
  // without a new estimate (P50 / P70 leave these lines out)
  const nums = nc.lines.map(l => ncNumber(byId.get(l.row_id)));
  const have = nums.filter(v => v !== null);
  const h = r.headline || {};
  if (have.length && have.length === nums.length && isNum(h.p50)) {
    const qt = have.reduce((a, b) => a + b, 0);
    host.appendChild(el('div', { class: 'complete' }, [
      el('div', {}, [el('span', { class: 'cl', text: 'Complete expected low bid (P50 + quotes ' + money(qt) + ')' }),
        el('span', { class: 'cv', text: money(h.p50 + qt) })]),
      isNum(h.p70) ? el('div', {}, [el('span', { class: 'cl', text: 'Complete recommended OPCC (P70 + quotes)' }),
        el('span', { class: 'cv', text: money(h.p70 + qt) })]) : null,
    ]));
  } else {
    host.appendChild(el('div', { class: 'muted-note', text: have.length + ' of ' + nums.length
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
    'Review these lump sums against the earlier-bid ranges below and enter your own prices. ' }));
  const rows = lines.map(g => {
    const h = g.history;
    const pos = g.position;
    const its = (g.items || []).filter(x => x);
    return el('tr', {}, [
      el('td', {}, [el('div', { text: g.label }),
        its.length ? el('div', { class: 'dim', text: 'item ' + its.join(', ') }) : null]),
      el('td', { class: 'num', text: g.ours_p50 !== null ? money(g.ours_p50) : '—' }),
      el('td', { class: 'num', text: pct(g.share_of_p50, 1) }),
      el('td', { class: 'num', text: h ? pct(h.p25, 1) + ' – ' + pct(h.p75, 1) : '—',
        title: h ? 'median ' + pct(h.median, 1) + '; ' + h.lettings + ' earlier jobs, '
          + h.size_label + (h.scope === 'size' ? ' (rest of the job)' : '') : '' }),
      el('td', { class: 'num', text: g.implied_range ? money(g.implied_range[0]) + ' – '
        + money(g.implied_range[1]) : 'too few past jobs' }),
      el('td', {}, pos ? el('span', { class: 'badge ' + (pos === 'within' ? 'ok' : 'thin'),
        text: pos === 'within' ? 'within the range'
          : pos === 'above' ? 'above: likely too high here' : 'below: likely too low here' }) : null),
    ]);
  });
  host.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, ['Item', 'Ours (in the expected low bid)', 'Share of it',
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

/* ------------------------------------------------------ the OPCC worksheet */

// New worksheets start from selected recent public bid medians. Saved
// P50/P70/P80 worksheets retain their original interpretation and prices.
function worksheetBasis() {
  return ['history', 'p50', 'p70', 'p80'].includes(S.ws.basis) ? S.ws.basis : 'history';
}
function basisMultiplier(basis) {
  if (basis === 'history') return null;
  if (basis === 'p50') return 1;
  const h = (S.result && S.result.headline) || {};
  const multiplier = h[basis + '_multiplier'];
  if (isNum(multiplier) && multiplier > 0) return Number(multiplier);
  return isNum(h.p50) && h.p50 > 0 && isNum(h[basis]) && h[basis] > 0
    ? h[basis] / h.p50 : null;
}
function wsMult() { return basisMultiplier(worksheetBasis()); }
function basisLabel(basis = worksheetBasis()) { return basis === 'history' ? 'Recent bid median' : basis.toUpperCase(); }
function modelBasis() { return worksheetBasis() === 'history' ? 'p70' : worksheetBasis(); }
function modelLabel() { return modelBasis().toUpperCase(); }
function modelUnit(l) {
  const multiplier = basisMultiplier(modelBasis());
  return l && isNum(l.unit_price_p50) && isNum(multiplier) ? l.unit_price_p50 * multiplier : null;
}
function historyPrice(l) { return (wsRefs(l) || {}).history_price || null; }
function availableHistory(hp) {
  return hp && hp.status === 'available' && hp.n_projects === 3
    && Array.isArray(hp.references) && hp.references.length === 3
    && isNum(hp.unit_price) && hp.unit_price > 0;
}
function historyRate(value, raw = false) {
  return isNum(value) ? '$' + Number(value).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: raw ? 8 : 2,
  }) : '—';
}
function historySource(hp) {
  return 'Recent bid median' + (hp && hp.target_year ? ' adjusted to ' + hp.target_year + ' prices' : '')
    + ((hp && hp.references) || []).map(r => {
    const adjusted = isNum(r.adjusted_median) ? ' adjusted ' + historyRate(r.adjusted_median, true) : '';
    return ' · ' + [r.owner, r.project, r.date].filter(Boolean).join(' / ')
      + ': bid median ' + historyRate(r.median_unit, true) + (r.unit ? '/' + r.unit : '') + adjusted
      + (isNum(r.escalation) ? ' (×' + Number(r.escalation).toFixed(4) + ')' : '');
  }).join('');
}
function historyDate(hp) { return ((hp && hp.references) || []).map(r => r.date).filter(Boolean).sort().join('; '); }
function historyReason(l) {
  if (engineerInput(l)) return engineerInputLabel(l);
  if (S.ws.refsError) return 'Recent bids unavailable; enter your unit price.';
  if (!S.ws.refs) return 'Loading recent bids';
  const hp = historyPrice(l);
  return (hp && hp.reason) || 'No qualified recent bid median; enter your unit price.';
}
function historySummary(hp) {
  const refs = (hp && hp.references) || [];
  if (!refs.length) return '';
  return refs.length + ' recent project' + (refs.length === 1 ? '' : 's')
    + (availableHistory(hp) ? (hp.target_year ? ' · adjusted to ' + hp.target_year + ' prices' : '')
      : hp.status === 'engineer_review' ? ' · engineer review'
        : refs.length < 3 ? ' · 3 required' : ' · price adjustment unavailable');
}
function referenceRange(l) {
  const cell = el('td', { class: 'num money ref-range' });
  if (!S.ws.refs) {
    cell.appendChild(el('span', { class: 'dim', text: S.ws.refsError ? 'unavailable' : 'loading' }));
    return cell;
  }
  if (worksheetBasis() === 'history') {
    const hp = historyPrice(l);
    const refs = (hp && hp.references) || [];
    const values = refs.map(r => r.adjusted_median).filter(v => isNum(v) && v > 0).map(Number);
    cell.title = 'Selected recent projects’ bidder medians, adjusted to the target bid year. Open this line for original bids and price adjustments.';
    if (!refs.length) cell.appendChild(el('span', { class: 'dim', text: '—' }));
    else {
      cell.appendChild(el('div', { text: values.length === refs.length
        ? historyRate(Math.min(...values)) + ' – ' + historyRate(Math.max(...values)) : 'adjustment unavailable' }));
      cell.appendChild(el('div', { class: 'dim', text: availableHistory(hp)
        ? 'median ' + historyRate(hp.unit_price) + ' · ' + refs.length + ' projects'
        : refs.length + ' recent project' + (refs.length === 1 ? '' : 's') + (refs.length < 3 ? ' · 3 required' : '') }));
    }
    return cell;
  }
  const x = wsRefs(l);
  cell.title = x && x.range ? 'Median-bidder unit prices of ' + x.n_refs
    + ' reference project(s), escalated to ' + x.to_year + ' dollars' : '';
  if (x && x.range) cell.append(
    el('div', { text: rate(x.range.low) + ' – ' + rate(x.range.high) }),
    el('div', { class: 'dim', text: 'median ' + rate(x.range.median) + ' · ' + x.n_refs + ' ref' + (x.n_refs === 1 ? '' : 's') }));
  else cell.appendChild(el('span', { class: 'dim', text: x && x.special ? '—' : 'none' }));
  return cell;
}
// prices of yours, picks and engineer inputs: what a switch would replace
function wsChanged() {
  return ((S.result && S.result.lines) || []).filter(l => {
    const w = S.ws.lines[l.row_id];
    if (l.pricing_mode === 'engineer' && w && w.src === 'you') return false;
    return w && w.src && !['model', 'quote', 'owner-set'].includes(w.src);
  }).length;
}
function setBasis(b) {
  if (!S.result || !['p50', 'p70', 'p80'].includes(b) || worksheetBasis() === b) return;
  if (!isNum((S.result.headline || {})[b])) {
    toast(basisLabel(b) + ' is not available for this estimate. Re-estimate before choosing it.', 'err');
    return;
  }
  const n = wsChanged();
  if (n && !window.confirm('Start from the model\u2019s ' + basisLabel(b) + ' prices? '
      + n + ' price' + (n === 1 ? '' : 's') + ' you changed will be replaced.')) return;
  S.ws.basis = b;
  // every price goes back to the model's at this level; notes, vendor quotes
  // and owner-set amounts stay
  Object.entries(S.ws.lines).forEach(([rowId, w]) => {
    const l = (S.result.lines || []).find(x => x.row_id === rowId);
    if (l && l.pricing_mode === 'engineer' && w.src === 'you') return;
    if (!['quote', 'owner-set'].includes(w.src)) {
      w.src = ''; w.src_label = ''; w.your_unit = null; w.raw = null; w.src_date = '';
    }
  });
  afterInput(renderWsNow);
}

// YOUR unit price (default: the model's; quick picks from the references and
// the similar projects), your amount and a note. Your prices and the OPCC
// they add up to are output only: estimateRequest() never sends them.
function wsLine(l) {
  let w = S.ws.lines[l.row_id];
  if (!w) {
    w = S.ws.lines[l.row_id] = { your_unit: null, src: '', src_label: '', note: '', your_amount: null };
  }
  // an amount that came from the line itself goes when its source goes: a
  // vendor quote taken out, an owner-set amount removed (it used to stay in
  // Your OPCC, with the price field locked)
  if ((w.src === 'quote' && !(l.quote > 0)) || (w.src === 'owner-set' && l.status !== 'owner_set')) {
    w.src = ''; w.src_label = ''; w.your_amount = null; w.your_unit = null;
  }
  if (l.status === 'owner_set') {
    const sh = l.amount_from_pct;
    w.src = 'owner-set'; w.your_unit = null;
    w.src_label = sh ? sh.pct + '% of the subtotal' : 'owner-set amount';
    w.your_amount = sh ? null : l.fixed_amount;
  } else if ((l.status === 'not_covered' || l.status === 'not_covered_whole') && !(l.quote > 0)
             && w.src === 'you' && isNum(w.your_unit)) {
    // a price of yours from an older worksheet becomes the line's one number
    ncAdopt(l.row_id, w.your_unit * (Number(l.quantity) > 0 ? Number(l.quantity) : 1));
  } else if ((l.status === 'not_covered' || l.status === 'not_covered_whole') && l.quote > 0) {
    w.src = 'quote'; w.src_label = 'vendor quote'; w.your_amount = l.quote; w.your_unit = null;
  } else if (!w.src && engineerInput(l)) {
    // the engineer sets it: no default price of yours (the model's price
    // stays a quick pick)
    w.your_unit = null; w.raw = null; w.src_label = engineerInputLabel(l);
  } else if (worksheetBasis() === 'history' && (!w.src || w.src === 'history')) {
    const hp = historyPrice(l);
    if (availableHistory(hp) && !engineerInput(l)) {
      w.src = 'history'; w.raw = Number(hp.unit_price); w.history_price = hp;
      w.src_label = historySource(hp); w.src_date = historyDate(hp);
    } else {
      w.your_unit = null; w.raw = null; w.history_price = null;
      w.src_label = historyReason(l); w.src_date = '';
    }
  } else if (worksheetBasis() !== 'history' && (!w.src || w.src === 'model') && l.unit_price_p50) {
    w.src = 'model'; w.src_label = 'model ' + modelLabel() + ' price'; w.raw = modelUnit(l);
    if (!isNum(w.raw)) w.your_unit = null;
  }
  // a price taken from the model or a quick pick follows the rounding
  // setting (a printed cap is never rounded past the cap); a price you
  // typed is never rounded
  if (isNum(w.raw) && !['', 'you', 'quote', 'owner-set'].includes(w.src)) {
    w.your_unit = w.src === 'history' && S.ws.round === 'off'
      ? Math.round((Number(w.raw) + Number.EPSILON) * 100) / 100
      : nicePrice(w.raw, w.src === 'cap' ? (S.ws.round === 'off' ? 'off' : 'down') : S.ws.round);
  }
  return w;
}

// "Nice" prices for the OPCC (user's team, 2026-11-21: reviewers read $50
// at a glance and stumble on $48): no cents and no stray units digit, in a
// step that grows with the price -- $0.50 under $10, $5 to $100, $10 to
// $1,000, $100 to $10,000, $500 to $100,000, then $1,000. Mode 'up' (the
// default), 'nearest', 'down' or 'off'.
function niceStep(v) {
  const a = Math.abs(v);
  return a < 1 ? 0.05 : a < 10 ? 0.5 : a < 100 ? 5 : a < 1000 ? 10 : a < 10000 ? 100
    : a < 100000 ? 500 : 1000;
}

function nicePrice(v, mode) {
  if (!isNum(v) || !mode || mode === 'off') return isNum(v) ? Number(v) : v;
  const st = niceStep(Number(v)), k = Number(v) / st;
  const n = mode === 'nearest' ? Math.round(k) : mode === 'down' ? Math.floor(k + 1e-9) : Math.ceil(k - 1e-9);
  return Math.round(n * st * 100) / 100;
}

function priceText(v) {
  return !isNum(v) ? '' : Number.isInteger(Number(v)) ? String(Number(v)) : Number(v).toFixed(2);
}

// Lines the engineer prices, not the worksheet (user, 2026-11-21): a
// mobilization line (some bid forms have one, some do not; when it is there
// the engineer sets it) and temporary erosion control as a lump sum (it
// depends on the site -- a creek or a river, its size, how hard the work is).
// No default price of yours; the model's P50 still prices them, since the low
// bid includes them, and its price stays a quick pick.
function engineerInput(l) {
  if (!l) return false;
  if (l.pricing_mode === 'engineer' && l.status !== 'owner_set') return true;
  if (l.status !== 'priced' && l.status !== 'unpriced') return false;
  const lc = String(l.lowconf_class || '');
  if (l.family === 'mobilization' || lc === 'mobilization') return true;
  const u = String(l.unit || '').trim().toUpperCase();
  return (l.family === 'erosion_control' || /erosion/.test(lc)) && (u === 'LS' || u === '');
}

function engineerInputLabel(l) {
  if (l.pricing_mode === 'engineer') return 'engineer input: new item / no matching item type';
  return (l.family === 'mobilization' || l.lowconf_class === 'mobilization')
    ? 'engineer input: mobilization, if the job has it'
    : 'engineer input: depends on the site (creek or river), its size and difficulty';
}

// Your SUBTOTAL: your amounts for every line except a contingency, which
// sits below it (user, 2026-11-21: "first the subtotal, then the contingency
// below it, then the total"). Owner-set allowances are in it. A contingency
// given as a share is that share of it; the model's amount for that line
// (amount_p50) is the same share of the model's subtotal. worksheet.totals()
// does the same for the export.
function wsSubtotal() {
  let s = 0;
  ((S.result || {}).lines || []).forEach(o => {
    if (shareOrContingency(o)) return;
    const a = wsAmount(o, wsLine(o));
    if (a !== null) s += a;
  });
  return s;
}

function wsAmount(l, w) {
  if (l.status === 'owner_set') {
    const sh = l.amount_from_pct;
    if (sh && isNum(sh.pct)) return Number(sh.pct) / 100 * wsSubtotal();
    return isNum(l.fixed_amount) ? Number(l.fixed_amount) : null;
  }
  if (w.src === 'quote' && isNum(w.your_amount)) return Number(w.your_amount);
  return isNum(w.your_unit) && isNum(l.quantity) ? w.your_unit * l.quantity : null;
}

function wsRefs(l) { return (S.ws.refs || {})[l.row_id] || null; }

// Keep reference evidence separate from the model's item-accuracy history.
function wsJudgment(l) {
  const x = wsRefs(l);
  const history = worksheetBasis() === 'history';
  const why = [];
  if (worksheetBasis() === 'history' && S.ws.refs && !availableHistory(historyPrice(l))
      && !engineerInput(l) && !['owner_set', 'not_covered', 'not_covered_whole'].includes(l.status)) {
    why.push('no recent bid median');
  }
  if ((l.status === 'not_covered' || l.status === 'not_covered_whole') && wsAmount(l, wsLine(l)) === null) {
    why.push('not covered');
  }
  if (l.status === 'unpriced' && !(history && availableHistory(historyPrice(l)))) why.push('not priced');
  if (l.amount_missing) why.push('allowance without an amount');
  if (engineerInput(l)) why.push('engineer input');
  if (l.status === 'priced') {
    if (!history && x && x.n_refs < 3) why.push(x.n_refs + ' reference' + (x.n_refs === 1 ? '' : 's'));
    if ((!history && x && x.generic) || ['other', 'needs_review', ''].includes(l.family || '')) why.push('generic match');
    if (String(l.unit).toUpperCase() === 'LS') why.push('lump sum');
    if (l.lowconf_class) why.push('low confidence');
    else if (!history && l.accuracy && ['poor', 'none'].includes(l.accuracy.tier)) why.push('weak item accuracy');
  }
  return why;
}

function similarPick(l) {
  // the most recent similar project with a line matched to this one
  let best = null;
  (((S.result || {}).similar || {}).cards || []).forEach(c => (c.matched_lines || []).forEach(m => {
    if (m.row_id === l.row_id && isNum(m.their_low_unit) && (!best || c.date > best.c.date)) best = { c, m };
  }));
  return best;
}

function wsPicks(l) {
  const x = wsRefs(l);
  const yr = x && x.to_year ? x.to_year + ' $' : 'bid-year $';
  const out = [];
  const hp = historyPrice(l);
  if (availableHistory(hp) && !engineerInput(l)) out.push({ v: 'history', price: hp.unit_price,
    label: historySource(hp), short: 'Recent bid median · ' + historyRate(hp.unit_price),
    date: historyDate(hp), history_price: hp });
  const tagOf = pk => pk.owner + ' ' + String(pk.date).slice(0, 4) + ' ' + pk.project;
  // the newest reference worded exactly like this line comes first: the
  // most telling price there is (user's team, 2026-11-21)
  const ident = ((x && x.picks) || []).filter(pk => (pk.word_match || 0) >= 0.999
      && isNum(isNum(pk.mean_esc) ? pk.mean_esc : pk.median_esc))
    .sort((a, b) => String(b.date).localeCompare(String(a.date)))[0];
  if (ident) {
    const v = isNum(ident.mean_esc) ? ident.mean_esc : ident.median_esc;
    out.push({ v: 'ident', price: v, date: ident.date, label: 'Newest identical item: ' + ident.project
      + ' (' + ident.date + ') average ' + rate(v) + ' (' + yr + ')',
      short: 'Identical item ' + String(ident.date).slice(0, 7) + ' \u00b7 ' + rate(v),
      note: 'used ' + tagOf(ident) + ' average bid (identical item)' });
  }
  if (isNum(modelUnit(l))) out.push({ v: 'model', price: modelUnit(l), label: 'Model ' + modelLabel() + ' project-adjusted price ' + rate(modelUnit(l)),
    short: 'Model ' + modelLabel() + ' · ' + rate(modelUnit(l)) });
  if (x && x.range && isNum(x.range.mean)) out.push({ v: 'ref_mean', price: x.range.mean, label: 'Reference average ('
    + x.n_refs + ', ' + yr + ') ' + rate(x.range.mean), note: 'average bid of ' + x.n_refs + ' reference projects',
    short: 'Ref. average (' + x.n_refs + ') \u00b7 ' + rate(x.range.mean) });
  if (x && x.range) out.push({ v: 'ref_median', price: x.range.median, label: 'Reference median ('
    + x.n_refs + ', ' + yr + ') ' + rate(x.range.median), note: 'reference median of ' + x.n_refs + ' projects',
    short: 'Ref. median (' + x.n_refs + ') \u00b7 ' + rate(x.range.median) });
  const uh = x && x.unit_hint;
  if (uh && isNum(uh.unit_price)) out.push({ v: 'unit', price: uh.unit_price,
    label: 'Converted from ' + uh.target + ' bids: ' + rate(uh.unit_price) + '/' + l.unit
      + ' (= ' + rate(uh.unit_price_target) + '/' + uh.target + ')',
    note: 'converted from ' + uh.target + ' bids (' + uh.n_target + ' earlier lettings); check it is the same kind of work',
    short: 'Converted from ' + uh.target + ' bids: ' + rate(uh.unit_price) + '/' + l.unit });
  const capAmt = printedCapAmount(l);
  if (capAmt !== null && isNum(l.quantity) && l.quantity > 0) out.push({ v: 'cap', price: capAmt / l.quantity,
    label: 'Printed cap: ' + l.printed_cap.text + ' of the '
      + (l.printed_cap.basis === 'rest' ? 'rest of the bid' : 'total bid') + ' = ' + money(capAmt),
    note: 'printed cap ' + l.printed_cap.text + '; bidders usually price it at or just under the cap',
    short: 'Printed cap: ' + l.printed_cap.text + ' of the ' + (l.printed_cap.basis === 'rest' ? 'rest of the bid'
      : 'total bid') + ' = ' + money(capAmt) });
  const sp = similarPick(l);
  if (sp) out.push({ v: 'similar', price: sp.m.their_low_unit, date: sp.c.date, label: 'Most recent similar: '
    + sp.c.project + ' (' + sp.c.date + ') low ' + rate(sp.m.their_low_unit),
    note: 'used ' + sp.c.owner + ' ' + String(sp.c.date).slice(0, 4) + ' ' + sp.c.project + ' low bid',
    short: 'Similar job ' + String(sp.c.date).slice(0, 7) + ' low \u00b7 ' + rate(sp.m.their_low_unit) });
  ((x && x.picks) || []).forEach((pk, i) => {
    const tag = tagOf(pk);
    const who = (pk.owner || 'Past job') + ' ' + String(pk.date).slice(0, 7);
    if (isNum(pk.mean_esc)) out.push({ v: 'ref' + i + 'a', price: pk.mean_esc, date: pk.date, label: pk.project
      + ' (' + pk.date + ') average ' + rate(pk.mean_esc) + ' (' + yr + ')', note: 'used ' + tag + ' average bid',
      short: who + ' avg \u00b7 ' + rate(pk.mean_esc) });
    if (isNum(pk.median_esc)) out.push({ v: 'ref' + i + 'm', price: pk.median_esc, date: pk.date, label: pk.project
      + ' (' + pk.date + ') median ' + rate(pk.median_esc) + ' (' + yr + ')', note: 'used ' + tag + ' median bid',
      short: who + ' median \u00b7 ' + rate(pk.median_esc) });
    if (isNum(pk.low_esc)) out.push({ v: 'ref' + i + 'l', price: pk.low_esc, date: pk.date, label: pk.project
      + ' (' + pk.date + ') low ' + rate(pk.low_esc) + ' (' + yr + ')', note: 'used ' + tag + ' low bid',
      short: who + ' low \u00b7 ' + rate(pk.low_esc) });
  });
  return out;
}

function renderLines(r) { renderWorksheet(r); loadWsRefs(); }

function renderWorksheet(r) {
  r = r || S.result;
  if (!r) return;
  // the worksheet's own contingency starts at 10%, or at 0% when the schedule
  // already carries a contingency allowance (so it is not counted twice),
  // until the user sets it
  if (!S.ws.contTouched) S.ws.contingency = contingencyLines(r).length ? 0 : DEFAULT_CONTINGENCY;
  const focus = focusState();
  syncWsMeta(r);
  renderWorksheetRows(r);
  restoreFocus(focus);
}

// the worksheet's heading fields show what S.ws holds (a reopened worksheet)
function syncWsMeta(r) {
  const i = (r && r.inputs) || {};
  const set = (sel, v) => { const n = $(sel); if (n && document.activeElement !== n) n.value = v || ''; };
  set('#ws-project', S.ws.name);
  set('#ws-prepared', S.ws.prepared_by);
  set('#ws-projno', S.ws.project_no);
  const p = $('#ws-project');
  if (p) p.placeholder = [i.owner_label, i.work_type_label, i.bid_date].filter(Boolean).join(' ') || 'project name';
  const rs = $('#ws-round');
  if (rs) {
    rs.value = S.ws.round || 'off';
    const off = rs.querySelector('option[value="off"]');
    if (off) off.textContent = worksheetBasis() === 'history' ? 'cents only' : 'off (exact)';
  }
}

// Where a line's price came from, for the export's "Unit Cost Source" and
// "Date" columns: the reference projects behind it, as an OPCC writes them.
function wsSource(l, w) {
  const x = wsRefs(l);
  if (l.status === 'owner_set') return [w.src_label || 'owner-set amount', ''];
  if (w.src === 'quote') return ['Vendor quote', ''];
  if (w.src === 'you') return ['Engineer\u2019s price', ''];
  if (w.src === 'history') return [historySource(w.history_price), historyDate(w.history_price)];
  if (!w.src) return [engineerInput(l) ? 'Engineer input' : '', ''];
  if (w.src === 'model') {
    if (worksheetBasis() === 'history') return [w.src_label || 'Model ' + modelLabel() + ' price', w.src_date || ''];
    const pk = ((x && x.picks) || []).slice(0, 3).map(p => p.owner + ' ' + String(p.date).slice(0, 4) + ' avg '
      + rate(isNum(p.mean_esc) ? p.mean_esc : p.median_esc));
    return ['Model ' + modelLabel() + ' price' + (pk.length ? ' \u00b7 ' + pk.join('; ') : ''),
      (x && x.latest && x.latest.date) || ''];
  }
  return [w.src_label || w.src, w.src_date || ''];
}

function renderWorksheetRows(r) {
  const body = $('#result-body');
  body.innerHTML = '';
  (r.lines || []).forEach(l => {
    // a contingency is not an item: it goes below the subtotal, in the
    // table's foot (renderWsTotals)
    if (shareOrContingency(l)) return;
    const judg = wsJudgment(l);
    const open = !!S.ws.open[l.row_id];
    body.appendChild(resultRow(l, judg, open));
    const d = el('tr', { class: 'detail', 'data-detail': l.row_id, id: 'detail-' + l.row_id,
      style: open ? '' : 'display:none' },
      el('td', { colspan: 11 }, lineDetail(l)));
    body.appendChild(d);
    if (open) {
      const host = $('[data-refs="' + CSS.escape(l.row_id) + '"]', d);
      if (host) loadReferences(l, host);
    }
  });
  renderWsTotals(r);
}

// Your OPCC, laid out as an OPCC is (user, 2026-11-21): the items' SUBTOTAL,
// then the contingency below it -- the schedule's contingency lines and/or
// your own percentage, both on the subtotal -- then the TOTAL.
function wsTotals(r) {
  let sub = 0, missing = 0, own = 0, quotes = 0, nc = 0, below = 0;
  const contLines = [];
  (r.lines || []).forEach(l => {
    const w = wsLine(l);
    const a = wsAmount(l, w);
    if (shareOrContingency(l)) {
      contLines.push(l);
      if (a !== null) below += a;
      return;
    }
    if (a === null) { missing += 1; return; }
    sub += a;
    if (l.status === 'owner_set') own += a;
    else if (w.src === 'quote') quotes += a;
    if (l.status === 'not_covered' || l.status === 'not_covered_whole') nc += a;
  });
  const c = isNum(S.ws.contingency) ? Number(S.ws.contingency) : 0;
  const h = r.headline || {};
  const cont = sub * c / 100;
  // P50 / P70 leave the not-covered lines out; your OPCC has your amounts
  // for them (vendor quotes), so the comparison adds them to the model's
  // side too (worksheet.totals does the same for the export)
  const plus = m => (isNum(m) && m ? Number(m) + nc : m);
  const hc = h.contingency && h.contingency.p50;
  return { sub, missing, own, quotes, nc, c, below, contLines, cont, yours: sub + below + cont,
    p50: h.p50, p70: h.p70, p80: h.p80, m50: plus(h.p50), m70: plus(h.p70), m80: plus(h.p80),
    msub: plus(hc ? hc.subtotal : h.p50), mcont: hc ? hc.contingency : 0,
    suggest: h.p50 && h.p70 ? 100 * (h.p70 / h.p50 - 1) : null };
}

// "Contingency 10%", "Contingency (owner's fixed amount)", "Contingency 10% + $50,000"
function contingencyLabel(c) {
  if (!c) return 'Contingency';
  const p = c.pct ? +Number(c.pct).toFixed(2) + '%' : '';
  if (p && c.fixed) return 'Contingency ' + p + ' + ' + money(c.fixed);
  return p ? 'Contingency ' + p : 'Contingency (owner’s fixed amount)';
}

function renderWsTotals(r) {
  const host = $('#ws-totals');
  host.innerHTML = '';
  const t = wsTotals(r);
  const th = $('#th-model');
  if (th) th.innerHTML = worksheetBasis() === 'history'
    ? 'Bid median $/unit' : 'Model $/unit<br><span class="dim">' + basisLabel() + '</span>';
  if (th) th.title = worksheetBasis() === 'history'
    ? 'Median of three matching recent projects, adjusted to the bid year'
    : 'The model unit price at the saved ' + basisLabel() + ' level';
  const basis = el('div', { class: 'basis' }, [
    el('span', { class: 'lab', text: worksheetBasis() === 'history' ? 'Recent bid median'
      : 'Prices from ' + basisLabel() + ' (saved basis)' }),
    S.ws.refsError && worksheetBasis() === 'history'
      ? el('span', { class: 'warnnote', text: 'Recent bids could not be loaded; check missing prices.' }) : null,
  ]);
  const vs = (m) => {
    if (!m) return '\u2014';
    const d = t.yours - m;
    if (Math.abs(d) < 0.5) return 'the same';
    const sg = d > 0 ? '+' : '\u2212';
    return sg + money(Math.abs(d)) + ' (' + sg + Math.abs(100 * (t.yours / m - 1)).toFixed(1) + '%)';
  };
  // picked from a list (a saved worksheet's other value is kept in it)
  const curC = isNum(S.ws.contingency) ? Number(S.ws.contingency) : 0;
  const cpcts = WS_CONTINGENCY_PCTS.slice();
  if (!cpcts.includes(curC)) { cpcts.push(curC); cpcts.sort((a, b) => a - b); }
  const cin = el('select', { class: 'cpct', 'data-fk': 'ws:contingency', 'aria-label': 'Your contingency',
    title: 'Added to your line amounts; owner-set allowances are left out',
    onchange: e => { S.ws.contingency = Number(e.target.value); S.ws.contTouched = true; afterInput(renderWsTotalsNow); } },
    cpcts.map(p => el('option', { value: String(p), text: p ? p + '%' : 'none (0%)', selected: p === curC })));
  const plusNc = t.nc ? ' + not-covered ' : ' ';
  const cl = t.contLines;
  // subtotal + contingency = total, in words
  const parts = ['subtotal ' + money(t.sub) + (t.own ? ' (incl. owner-set allowances ' + money(t.own) + ')' : '')
    + (t.quotes ? ' (incl. vendor quotes ' + money(t.quotes) + ')' : '')];
  if (cl.length) parts.push(contingencyLabel(r.headline.contingency).toLowerCase() + ' ' + money(t.below));
  // always named, 0% included: changing the percentage changes numbers,
  // never the layout (user, 2026-11-21)
  parts.push((cl.length ? 'additional construction contingency ' : 'construction contingency ') + t.c + '% ' + money(t.cont));
  host.appendChild(el('div', { class: 'wstot' }, [
    el('div', { class: 'yours' }, [
      basis,
      el('div', { class: 'lab', text: 'Your OPCC (project total)' }),
      el('div', { class: 'big', text: money(t.yours) }),
      el('div', { class: 'dim', text: parts.join(' + ') }),
      t.missing ? el('div', { class: 'warnnote', text: t.missing + ' line' + (t.missing === 1 ? ' has' : 's have')
        + ' no price of yours and ' + (t.missing === 1 ? 'is' : 'are') + ' not in this total.' }) : null,
    ]),
    el('div', { class: 'cmp' }, [
      el('div', {}, [el('span', { class: 'lab', text: 'P50' + plusNc }), el('span', { class: 'v', text: money(t.m50) }),
        el('span', { class: 'dim', text: '  you ' + vs(t.m50) })]),
      el('div', {}, [el('span', { class: 'lab', text: 'P70' + plusNc }), el('span', { class: 'v', text: money(t.m70) }),
        el('span', { class: 'dim', text: '  you ' + vs(t.m70) })]),
      t.nc ? el('div', { class: 'dim small', text: 'P50 ' + money(t.p50) + ' and P70 ' + money(t.p70)
        + ' leave the not-covered lines out; your ' + money(t.nc) + ' for them is added to both here.' }) : null,
      el('div', { class: 'cont' }, [el('span', { class: 'lab', text: (cl.length ? 'Additional construction contingency '
        : 'Construction contingency ') }), cin, el('span', { class: 'dim', text: ' of the subtotal' })]),
      contingencyGuide(r, t),
    ]),
  ]));
  // the table's foot: SUBTOTAL, then the contingency below it, then TOTAL
  const foot = $('#result-foot');
  foot.innerHTML = '';
  const footRow = (cls, label, amt) => el('tr', { class: cls }, [
    el('td', { colspan: 9, class: 'num', text: label }),
    el('td', { class: 'num money p50', text: money(amt) }),
    el('td', {})]);
  foot.appendChild(footRow('wssub', 'Subtotal' + (t.missing ? ' (' + t.missing + ' line'
    + (t.missing === 1 ? '' : 's') + ' without a price)' : ''), t.sub));
  cl.forEach(l => foot.appendChild(contingencyRow(l)));
  foot.appendChild(footRow('wscont', (cl.length ? 'Additional construction contingency ('
    : 'Construction contingency (') + t.c + '% of the subtotal)', t.cont));
  foot.appendChild(footRow('wstotal', 'Project total (your OPCC)', t.yours));
}

function renderWsTotalsNow() { renderWsTotals(S.result); }

// How often an OPCC this high came in under the low bid on past jobs of the
// same market: the share of forward-scored lettings with m x P50 < low bid,
// read off the market's curve (context.under_curve, [[m, share], ...]).
function contingencyLines(r) {
  return ((r && r.lines) || []).filter(isContingencyResult);
}

function underAt(curve, m) {
  if (!curve || !curve.length || !isNum(m)) return null;
  if (m <= curve[0][0]) return curve[0][1];
  for (let i = 1; i < curve.length; i++) {
    const [m1, u1] = curve[i], [m0, u0] = curve[i - 1];
    if (m <= m1) return u0 + (u1 - u0) * (m - m0) / (m1 - m0);
  }
  return curve[curve.length - 1][1];
}

function contingencyGuide(r, t) {
  const ctx = r.context || {}, curve = ctx.under_curve;
  const mY = t.m50 ? t.yours / t.m50 : null;
  const cl = contingencyLines(r);
  const kids = [];
  if (cl.length) kids.push(el('div', { class: 'cnow', text: 'Your schedule already carries '
    + (cl.length === 1 ? 'a contingency allowance' : cl.length + ' contingency allowances') + ' ('
    + cl.map(l => (l.item_no ? 'item ' + l.item_no + ': ' : '') + (l.amount_from_pct
      ? l.amount_from_pct.pct + '% of the subtotal, ' : '') + money(wsAmount(l, wsLine(l)))).join('; ')
    + '). Add a further contingency only for scope not covered by these allowances.' }));
  if (worksheetBasis() !== 'history' && curve && mY) kids.push(el('div', { text: 'Your OPCC is ' + mY.toFixed(2)
    + ' \u00d7 ' + (t.nc ? '(P50 + not-covered prices)' : 'P50')
    + '. At this ratio, ' + pct(underAt(curve, mY)) + ' of past '
    + (ctx.market === 'txdot' ? 'TxDOT' : 'city / county') + ' jobs exceeded the OPCC.' }));
  return el('div', { class: 'cguide' }, kids);
}
function renderWsNow() { renderWorksheet(S.result); renderNcReminders(); }

async function loadWsRefs() {
  const req = S.resultReq;
  if (!req || S.ws.refsReq === req) return;
  if (S.ws.refsPendingReq === req && S.ws.refsPromise) return S.ws.refsPromise;
  S.ws.refsPendingReq = req; S.ws.refsLoading = true; S.ws.refsError = '';
  const pending = (async () => {
    for (let tries = 0; tries <= 20; tries++) {
      if (S.resultReq !== req) return;
      try {
        const x = await postJSON('/api/worksheet/refs', { request: req });
        if (S.resultReq !== req) return;
        S.ws.refs = {};
        (x.lines || []).forEach(v => { S.ws.refs[v.row_id] = v; });
        S.ws.refsReq = req; S.ws.refsLoading = false;
        renderWorksheet(S.result);
        if ((x.lines || []).some(v => v.unit_hint)) renderGrid();
        return;
      } catch (e) {
        if (S.resultReq !== req) return;
        if (e.status === 503 && tries < 20) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          continue;
        }
        S.ws.refsError = String(e.message || e); S.ws.refsLoading = false;
        renderWorksheet(S.result);
        toast('Recent bids could not be loaded. Enter missing prices or re-estimate.', 'err');
        return;
      }
    }
  })();
  S.ws.refsPromise = pending;
  try { await pending; }
  finally {
    if (S.ws.refsPendingReq === req) {
      S.ws.refsLoading = false; S.ws.refsPendingReq = null; S.ws.refsPromise = null;
    }
  }
}

function wsDoc() {
  const r = S.result || {};
  const t = wsTotals(r);
  return {
    version: 1, kind: 'opcc-worksheet',
    request: S.resultReq,
    worksheet: { contingency_pct: S.ws.contingency, project_name: S.ws.name || '',
      prepared_by: S.ws.prepared_by || '', project_no: S.ws.project_no || '', round: S.ws.round || 'off',
      basis: worksheetBasis(), basis_multiplier: wsMult(),
      // the user's own date for the OPCC (the server's clock is UTC)
      today: (d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-'
        + String(d.getDate()).padStart(2, '0'))(new Date()),
      lines: (r.lines || []).map(l => {
        const w = wsLine(l);
        const so = wsSource(l, w);
        return { row_id: l.row_id, your_unit: w.your_unit, your_amount: wsAmount(l, w), src: w.src,
          src_label: w.src_label, note: w.note, judgment: wsJudgment(l), raw: w.raw,
          src_date: w.src_date || '', source: so[0], source_date: so[1],
          history_price: w.src === 'history' ? w.history_price || null : null };
      }) },
    totals: { subtotal: t.sub, contingency_pct: t.c, your_opcc: t.yours, missing: t.missing },
    model: { p50: t.p50, p70: t.p70, p80: t.p80 },
  };
}

async function wsExport(kind) {
  if (!S.result) return;
  const req = S.resultReq;
  if (worksheetBasis() === 'history' && S.ws.refsReq !== req) await loadWsRefs();
  if (S.resultReq !== req) { toast('The estimate changed; export it again when the recent bids have loaded.', 'err'); return; }
  if (worksheetBasis() === 'history' && S.ws.refsError && (S.result.lines || []).some(l => {
    const w = wsLine(l);
    return !engineerInput(l) && (!w.src || w.src === 'history') && !isNum(wsAmount(l, w));
  })) { toast('Recent bids could not be loaded. Fill the missing prices before exporting.', 'err'); return; }
  const d = wsDoc();
  const r = await fetch('/api/worksheet/export/' + kind, { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ result: S.result, request: S.resultReq, worksheet: d.worksheet }) });
  if (!r.ok) { toast('Export failed: ' + r.status, 'err'); return; }
  const blob = await r.blob();
  const cd = r.headers.get('content-disposition') || '';
  const m = /filename="([^"]+)"/.exec(cd);
  saveBlob(blob, m ? m[1] : 'opcc_worksheet.' + kind);
}

function saveBlob(blob, name) {
  const a = el('a', { href: URL.createObjectURL(blob), download: name });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

// "Click a line to see the past bids behind its price": shown with each
// estimate until dismissed; the dismissal is remembered in this browser
// (storage may be blocked: then it is remembered for this page only).
const HINT_KEY = 'bid-estimator-hint-rows-dismissed';

function showRowHint() {
  const h = $('#ws-hint');
  if (!h) return;
  let gone = !!S.hintGone;
  try { gone = gone || window.localStorage.getItem(HINT_KEY) === '1'; } catch (e) { /* blocked */ }
  h.hidden = gone;
}

function initWorksheet() {
  $('#ws-hint-close').addEventListener('click', () => {
    S.hintGone = true;
    try { window.localStorage.setItem(HINT_KEY, '1'); } catch (e) { /* not remembered */ }
    $('#ws-hint').hidden = true;
  });
  // Enter / arrows move along the "your $/unit" column, as in the takeoff grid
  $('#result-table').addEventListener('keydown', e => {
    const t = e.target;
    if (!t.dataset || !t.dataset.wsrow) return;
    if (e.key !== 'Enter' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const all = $$('#result-body input.yourp:not([disabled])');
    const next = all[all.indexOf(t) + (e.key === 'ArrowUp' ? -1 : 1)];
    t.blur();
    if (next) { next.focus(); if (next.select) next.select(); }
  });
  // the export's heading and the rounding of picked prices
  $('#ws-project').addEventListener('change', e => { S.ws.name = e.target.value.trim(); });
  $('#ws-prepared').addEventListener('change', e => { S.ws.prepared_by = e.target.value.trim(); });
  $('#ws-projno').addEventListener('change', e => { S.ws.project_no = e.target.value.trim(); });
  $('#ws-round').addEventListener('change', e => { S.ws.round = e.target.value; afterInput(renderWsNow); });
}

// A click anywhere on a line opens (or closes) the reference projects
// behind its price -- except on the fields and controls inside it (your
// price, the quick picks, the note). Keyboard: the row takes focus; Enter or
// Space toggles. (QA 2026-11-20: the old toggle was a tiny dot nobody saw.)
const ROW_CONTROLS = 'input, select, textarea, button, a, label';

// a press that started in a field and ended elsewhere on the line clicks the
// line itself (the common ancestor): still no toggle
let downInControl = false;
document.addEventListener('pointerdown', e => {
  downInControl = !!(e.target && e.target.closest && e.target.closest(ROW_CONTROLS));
}, true);

function rowToggles(e) {
  if (downInControl && e.detail > 0) return false;     // a pointer click begun in a field
  return !(e.target && e.target.closest && e.target.closest(ROW_CONTROLS));
}

// A contingency in the worksheet (user, 2026-11-21: "it is just contingency"),
// in the table's foot below the subtotal: what it is and its amount. A share
// line is that share of your subtotal; the model's P50 carries the same share
// of the model's subtotal. No item type, references, accuracy badge or price
// picks.
function contingencyRow(l) {
  const w = wsLine(l);
  const sh = l.amount_from_pct;
  const amt = wsAmount(l, w);
  const what = isContingencyResult(l) ? 'Contingency' : 'Owner-set allowance';
  const tr = el('tr', { 'data-line': l.row_id, class: 'controw st-' + l.status,
    title: what + (sh ? ': ' + sh.pct + '% of your subtotal. In P50 it is ' + sh.pct + '% of the '
      + 'model\u2019s subtotal, ' + money(l.amount_p50) + '.' : ': the owner\u2019s fixed amount, at '
      + 'face value.') });
  tr.appendChild(el('td', { class: 'dim nowrap', text: l.item_no || '' }));
  tr.appendChild(el('td', { class: 'desccell' }, [
    el('span', { class: 'ldesc', text: l.description }),
    l.bid_form_unit ? el('div', { class: 'dim small',
      text: 'Bid form: ' + qtyFmt(l.bid_form_quantity) + ' × ' + l.bid_form_unit
        + ' = ' + qtyFmt(l.quantity) + ' LF. Worksheet prices are per LF.' }) : null,
    el('div', { class: 'dim small', text: what + ' \u00b7 ' + (sh ? sh.pct + '% of the subtotal'
      : 'the owner\u2019s fixed amount') }),
  ]));
  tr.appendChild(el('td', {}));
  tr.appendChild(el('td', {}));
  // the model's own amount for it: the same share of the model's subtotal
  tr.appendChild(el('td', { class: 'num money' }, [el('div', { text: sh ? sh.pct + '%' : 'fixed' }),
    el('div', { class: 'dim', text: 'P50 ' + money(l.amount_p50) })]));
  tr.appendChild(el('td', {}));
  tr.appendChild(el('td', {}));
  tr.appendChild(el('td', { class: 'num money dim', text: sh ? sh.pct + '%' : 'fixed' }));
  tr.appendChild(el('td', {}));                    // Reference
  tr.appendChild(el('td', { class: 'num money p50', text: amt === null ? '\u2014' : money(amt) }));
  tr.appendChild(el('td', {}, el('input', { class: 'cellinput note', type: 'text', value: w.note || '',
    placeholder: 'note', 'data-fk': focusKey('ws', l.row_id, 'note'),
    onchange: e => { w.note = e.target.value; } })));
  return tr;
}

function resultRow(l, judg, isOpen) {
  judg = judg || wsJudgment(l);
  const w = wsLine(l);
  const open0 = isOpen === undefined ? !!S.ws.open[l.row_id] : !!isOpen;
  const chev = el('span', { class: 'chev', 'aria-hidden': 'true', text: open0 ? '▾' : '▸' });
  const tr = el('tr', { 'data-line': l.row_id, class: 'clickrow st-' + l.status
    + (l.amount_missing ? ' st-amount-missing' : '') + (judg.length ? ' judgrow' : ''),
    tabindex: '0', 'aria-expanded': String(open0), 'aria-controls': 'detail-' + l.row_id,
    'data-fk': focusKey('ws', l.row_id, 'row'),
    title: 'Click to show or hide the past bids behind this price \u00b7 drag to move the line' });
  const toggle = () => {
    const d = $('tr[data-detail="' + CSS.escape(l.row_id) + '"]');
    if (!d) return;
    const open = d.style.display === 'none';
    d.style.display = open ? 'table-row' : 'none';
    S.ws.open[l.row_id] = open;
    tr.setAttribute('aria-expanded', String(open));
    chev.textContent = open ? '▾' : '▸';
    const host = $('[data-refs="' + CSS.escape(l.row_id) + '"]', d);
    if (open && host && !host.dataset.loaded && !host.dataset.loading) {
      host.dataset.loading = '1';
      loadReferences(l, host).finally(() => { delete host.dataset.loading; });
    }
  };
  tr.addEventListener('click', e => { if (rowToggles(e)) toggle(); });
  tr.addEventListener('keydown', e => {
    if (e.target !== tr || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    toggle();
  });
  const tags = [];
  // a not-covered line has one badge of its own (with the past bids on
  // hover); the judgment badge does not say it again
  const ncBadge = (l.status === 'not_covered' || l.status === 'not_covered_whole') && l.coverage_class;
  const shown = ncBadge ? judg.filter(j => j !== 'not covered') : judg;
  if (shown.length) tags.push(el('span', { class: 'badge judg', title: 'Review this price: ' + judg.join('; '),
    text: 'needs your judgment: ' + shown.join(', ') }));
  if (l.status === 'owner_set') tags.push(el('span', { class: 'badge allow', text: 'owner-set' }));
  if (ncBadge) {
    const has = ncNumber(l) !== null;
    tags.push(hintPop(el('span', { class: 'badge ' + (has ? 'info' : 'unc'),
      text: has ? 'not covered \u00b7 your number in' : 'not covered: needs a quote' }),
      () => ncHintBox(l.nc_hint, l.coverage_label)));
  }
  tr.appendChild(el('td', { class: 'dim nowrap' }, [chev, document.createTextNode(l.item_no || '')]));
  tr.appendChild(el('td', { class: 'desccell' }, [
    el('span', { class: 'ldesc', text: l.description }),
    l.bid_form_unit ? el('div', { class: 'dim small',
      text: 'Bid form: ' + qtyFmt(l.bid_form_quantity) + ' × ' + l.bid_form_unit
        + ' = ' + qtyFmt(l.quantity) + ' LF. Worksheet prices are per LF.' }) : null,
    el('div', { class: 'dim small', text: l.pricing_mode === 'engineer'
      ? 'New item / Other · Enter your unit price; the model price is a generic reference only.'
      : l.family_label + (l.size_bucket ? ' · ' + l.size_bucket : '')
        + (worksheetBasis() === 'history' ? (historySummary(historyPrice(l)) ? ' · ' + historySummary(historyPrice(l)) : '')
          : l.basis ? ' · ' + l.basis : '') }),
    tags.length ? el('span', { class: 'tags' }, tags) : null,
    // reference only: a price converted from another unit, a printed cap
    (wsRefs(l) || {}).unit_hint ? el('div', { class: 'unithint' }, [el('span', { class: 'badge unith', text: 'unit: few bids' }),
      document.createTextNode(' ' + wsRefs(l).unit_hint.text)]) : null,
    l.printed_cap ? el('div', { class: 'unithint' }, [el('span', { class: 'badge unith', text: 'printed cap' }),
      document.createTextNode(' ' + capText(l))]) : null]));
  tr.appendChild(el('td', { class: 'num money', text: qtyFmt(l.quantity) }));
  tr.appendChild(el('td', { text: l.unit }));
  const hp = historyPrice(l);
  const baseline = worksheetBasis() === 'history'
    ? (availableHistory(hp) && !engineerInput(l) ? hp.unit_price : null) : modelUnit(l);
  tr.appendChild(el('td', { class: 'num money', text: isNum(baseline)
    ? (worksheetBasis() === 'history' ? historyRate(baseline) : rate(baseline))
    : l.status === 'owner_set' ? 'owner-set' : '—' }));
  tr.appendChild(referenceRange(l));
  tr.appendChild(el('td', {}, accBadge(l.accuracy)));
  // your unit price
  // a not-covered line's price is its one number, the quote (setNcQuote):
  // shown and typed here per unit
  const ncl = l.status === 'not_covered' || l.status === 'not_covered_whole';
  const qn = Number(l.quantity) > 0 ? Number(l.quantity) : 1;
  const ncUnit = ncl && l.quote > 0 ? l.quote / qn : null;
  const locked = l.status === 'owner_set';
  const inp = el('input', { class: 'cellinput num yourp' + (isNum(w.your_unit) || isNum(ncUnit) || locked ? '' : ' flag'), type: 'text',
    value: locked ? '' : ncl ? (isNum(ncUnit) ? priceText(ncUnit) : '') : priceText(w.your_unit),
    placeholder: locked ? 'owner-set' : l.pricing_mode === 'engineer' ? 'engineer input' : ncl ? 'quote $/unit'
      : (engineerInput(l) ? 'engineer input' : 'your $/unit'),
    title: ncl ? 'Not covered: the same number as the vendor quote under Project details and the '
      + 'line\u2019s amount in the takeoff (per unit here)' : null,
    disabled: locked, 'data-fk': focusKey('ws', l.row_id, 'price'), 'data-wsrow': l.row_id,
    onchange: e => {
      const v = parseNum(e.target.value);
      if (e.target.value.trim() && v === null) {
        e.target.value = priceText(ncl ? ncUnit : w.your_unit);
        toast('Enter a valid unit price, such as 44.50; the previous price is kept.', 'err', 9000);
        return;
      }
      if (ncl) { setNcQuote(l.row_id, isNum(v) ? v * qn : null); return; }
      w.your_unit = isNum(v) ? v : null;
      w.raw = null; w.src_date = '';
      w.src = isNum(v) ? 'you' : '';
      w.src_label = isNum(v) ? 'your price' : '';
      afterInput(renderWsNow);
    } });
  // the Reference column (user, 2026-11-21): where this price comes from,
  // starts with recent bid medians; saved model bases keep their meaning.
  // A deliberate choice here changes the price and flashes once.
  const picks = wsPicks(l);
  const cur = picks.find(p => p.v === w.src) || null;
  let ref;
  if (locked) ref = el('span', { class: 'dim', text: 'owner-set' });
  else if (ncl) ref = el('span', { class: 'dim', text: l.quote > 0 ? 'vendor quote' : '\u2014' });
  else if (!picks.length) ref = el('span', { class: 'dim', text: w.src === 'you' ? 'your price'
    : w.src && w.src !== 'history' ? w.src_label || 'selected reference'
      : worksheetBasis() === 'history' ? historyReason(l) : '\u2014' });
  else {
    const lead = cur ? [] : [el('option', { value: '', selected: true,
      text: w.src === 'you' ? 'your price' : w.src && w.src !== 'history' ? w.src_label || 'selected reference'
        : engineerInput(l) ? 'engineer input' : worksheetBasis() === 'history' ? 'enter your price' : 'choose a reference' })];
    const select = el('select', { class: 'pick', title: cur ? cur.label : 'Where this price comes from: pick a reference',
      'aria-label': 'Reference for this price', 'data-fk': focusKey('ws', l.row_id, 'pick'),
      onchange: e => {
        const pk = picks.find(p => p.v === e.target.value);
        if (!pk) return;
        // the picked price, rounded per the worksheet's setting (wsLine)
        w.raw = pk.price;
        w.src = pk.v === 'model' ? 'model' : pk.v;
        w.src_label = pk.label;
        w.src_date = pk.date || '';
        w.history_price = pk.v === 'history' ? pk.history_price : null;
        S.ws.flash = l.row_id;
        if (pk.note && !w.note) w.note = pk.note;
        afterInput(renderWsNow);
      } }, lead.concat(picks.map(p => el('option', { value: p.v, text: p.short || p.label, title: p.label,
        selected: !!cur && p.v === cur.v }))));
    ref = el('div', { class: 'reference-choice' }, [
      el('div', { class: 'selected-source', text: cur ? cur.short || cur.label : lead[0].textContent }), select,
    ]);
  }
  if (S.ws.flash === l.row_id) { inp.classList.add('flash'); S.ws.flash = null; }
  // a price rounded from its source says so; an engineer-input line says what it needs
  const cents = w.src === 'history' && S.ws.round === 'off' && isNum(w.raw) && isNum(w.your_unit)
    && Math.abs(w.your_unit - w.raw) > 1e-9;
  const rounded = cur && w.src !== 'model' && isNum(w.raw) && isNum(w.your_unit) && Math.abs(w.your_unit - w.raw) > 0.004;
  const sub = cents ? 'rounded to cents' : rounded ? 'rounded ' + (S.ws.round === 'nearest' ? '' : S.ws.round === 'down' || w.src === 'cap' ? 'down ' : 'up ')
      + 'from ' + rate(w.raw)
    : ((!w.src || w.src === 'history') && w.src_label && !isNum(w.your_unit) ? w.src_label : '');
  tr.appendChild(el('td', {}, [el('div', { class: 'yourcell' }, [inp]),
    sub ? el('div', { class: 'dim small', text: sub }) : null]));
  tr.appendChild(el('td', { class: 'refcell' }, ref));
  const amt = wsAmount(l, w);
  tr.appendChild(el('td', { class: 'num money p50', text: amt === null ? '—' : money(amt) }));
  tr.appendChild(el('td', {}, el('input', { class: 'cellinput note', type: 'text', value: w.note || '',
    placeholder: 'note', 'data-fk': focusKey('ws', l.row_id, 'note'),
    onchange: e => { w.note = e.target.value; } })));
  return tr;
}

function historyEvidence(l) {
  if (worksheetBasis() !== 'history') return null;
  const hp = historyPrice(l);
  if (!hp) return el('div', { class: 'history-evidence' }, [
    el('h5', { text: 'Recent bid median' }), el('p', { text: historyReason(l) }),
  ]);
  const refs = hp.references || [];
  return el('div', { class: 'history-evidence' }, [
    el('h5', { text: 'Recent bid median' + (hp.target_year ? ' · adjusted to ' + hp.target_year + ' prices' : '') }),
    el('p', { text: hp.reason || (availableHistory(hp) ? 'Median of three matching recent projects.' : 'Enter your unit price.') }),
    (hp.evidence || []).length ? el('ul', {}, hp.evidence.map(text => el('li', { text }))) : null,
    el('ol', {}, refs.map(r => {
      const ix = r.index_evidence || {};
      const adjustment = isNum(r.adjusted_median) ? ' → ' + historyRate(r.adjusted_median, true)
        + ' adjusted' + (isNum(r.escalation) ? ' (×' + Number(r.escalation).toFixed(4) + ')' : '') : ' · adjustment unavailable';
      return el('li', {}, [
        el('strong', { text: [r.date, r.owner, r.project].filter(Boolean).join(' · ') }),
        el('div', { text: (r.item_no ? r.item_no + ' · ' : '') + (r.item || '') }),
        el('div', { text: 'Bidder median ' + historyRate(r.median_unit, true) + (r.unit ? '/' + r.unit : '') + adjustment }),
        ix.reason ? el('div', { class: 'dim', text: ix.reason }) : null,
        ...(ix.notes || []).map(text => el('div', { class: 'dim', text })),
        ...(r.match_basis || []).map(text => el('div', { class: 'dim', text })),
        srcLinks(r.source),
      ]);
    })),
  ]);
}

function lineDetail(l) {
  const ev = l.evidence || {};
  const kv = [
    ['Item type', (l.family_label || l.family) + ' \u00b7 ' + l.unit + (l.size_bucket ? ' \u00b7 ' + l.size_bucket : '')
      + (l.family_overridden ? ' (chosen by you)' : '')],
    ['Typical price, all bidders', l.market_unit_price ? rate(l.market_unit_price) + ' / ' + l.unit : '\u2014'],
    ['Evidence', (ev.n_projects ? ev.n_projects + ' earlier projects' : '\u2014')
      + (ev.qty_band ? ' \u00b7 quantities ' + ev.qty_band : '')
      + (ev.usable && ev.usable !== 'yes' ? ' \u00b7 ' + (ev.usable === 'unclassified'
        ? 'a catch-all group' : 'thin') : '')],
  ];
  if (l.fixed_amount) kv.push(['Owner-set amount', money(l.fixed_amount)]);
  if (l.quote) kv.push(['Vendor quote', money(l.quote) + ' (not in the expected low bid or the OPCC)']);
  const refsHost = el('div', { class: 'refs', 'data-refs': l.row_id },
    el('span', { class: 'dim', html: '<span class="spinner"></span> loading the references…' }));
  return el('div', { class: 'detailbox' }, [
    historyEvidence(l),
    el('h5', { text: worksheetBasis() === 'history' ? 'Other public bid references' : 'References behind this price' }),
    refsHost,
    el('details', { class: 'fold', style: 'margin-top:8px' }, [
      el('summary', { text: 'How this was calculated' }),
      el('div', { class: 'detailcols', style: 'margin-top:6px' }, [
        el('dl', { class: 'kv' }, kv.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })])),
        el('div', {}, [
          (l.why || []).length ? el('ul', { class: 'tight' }, l.why.map(w => el('li', { text: w })))
            : el('div', { class: 'muted-note', text: 'No further notes.' }),
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

/* ------------------------------------------ project sources and full bids */

// Under every project name: the public page it came from (http(s) only,
// opens in a new tab, host in the label, full URL in the tooltip), else "no
// public link recorded"; plus the local copy under data/external when the
// server has it.
function safeHttp(u) { return /^https?:\/\/[^\s]+$/i.test(String(u || '')) ? String(u) : ''; }
function safeGithub(u) { return /^https:\/\/github\.com\/[^\s]+$/.test(String(u || '')) ? String(u) : ''; }

// Links under a project name, in this order: (a) the original we hold,
// served from data/external; (b) the public page it came from, with its
// host; (c) its copy in the public GitHub data repository, when
// ui/public_mirror.json maps it.
function srcLinks(src) {
  src = src || {};
  const kids = [];
  if (src.local_path) {
    kids.push(el('a', { class: 'srclink local', target: '_blank', rel: 'noopener',
      href: '/api/source-file?path=' + encodeURIComponent(src.local_path),
      title: 'The original file' + (src.local_kind ? ' (' + src.local_kind + ')' : '')
        + ', as harvested: data/' + src.local_path,
      text: 'Open original (local copy)' }));
  }
  const url = safeHttp(src.url);
  if (url) {
    kids.push(el('a', { class: 'srclink', href: url, target: '_blank', rel: 'noopener noreferrer',
      title: url, text: 'Source page: ' + (src.host || 'public page') + (src.kind ? ' — ' + src.kind : '') }));
  } else {
    kids.push(el('span', { class: 'dim', text: 'no public link recorded' }));
  }
  const mirror = safeGithub(src.mirror_url);
  if (mirror) {
    kids.push(el('a', { class: 'srclink mirror', href: mirror, target: '_blank', rel: 'noopener noreferrer',
      title: mirror, text: 'Public mirror (GitHub)' }));
  }
  return el('div', { class: 'srclinks' }, kids);
}

const qClose = (a, b) => isNum(a) && isNum(b)
  && Math.abs(a - b) <= 0.005 * Math.max(Math.abs(a), Math.abs(b), 1e-9);

// marks: [{test(line) -> bool, ours: {item_no, description}, nums: {low, median, p50, unit}}]
function markLines(v, marks) {
  const out = new Map();
  (marks || []).forEach(m => {
    let hits = v.lines.map((ln, i) => [ln, i]).filter(([ln]) => m.test(ln, true));
    if (!hits.length) hits = v.lines.map((ln, i) => [ln, i]).filter(([ln]) => m.test(ln, false));
    hits.forEach(([, i]) => { if (!out.has(i)) out.set(i, []); out.get(i).push(m); });
  });
  return out;
}

function bidTabView(v, marks, opts) {
  opts = opts || {};
  const marked = markLines(v, marks);
  const bidders = v.bidders || [];
  const ee = v.engineer_estimate;
  const st = { only: false };
  const box = el('div', { class: 'bidtab' });
  const head = el('div', { class: 'bthead' }, [
    el('div', { class: 'btname', text: v.project }),
    v.title && v.title !== v.project ? el('div', { class: 'simtitle', title: v.title, text: v.title }) : null,
    srcLinks(v.source),
    el('div', { class: 'simsub', text: [v.owner, v.date, v.work_type || 'work type not given',
      v.n_bidders + ' bidder' + (v.n_bidders === 1 ? '' : 's'), v.lines.length + ' line items'].join(' \u00b7 ') }),
  ]);
  box.appendChild(head);
  box.appendChild(el('div', { class: 'bttotals' }, bidders.map((b, k) => el('span', {
    class: 'bttot' + (b.low ? ' low' : ''), title: b.priced_lines + ' priced line(s)' }, [
      el('span', { class: 'n', text: (k + 1) + '. ' + b.name }),
      el('span', { class: 'v', text: money(b.total) + (b.low ? ' \u00b7 low bid' : '') }),
    ])).concat(ee ? [el('span', { class: 'bttot ee', title: ee.label }, [
      el('span', { class: 'n', text: 'Engineer\u2019s estimate (printed; never an input)' }),
      el('span', { class: 'v', text: money(ee.total) }),
    ])] : [])));
  const tools = el('div', { class: 'row-actions bttools' }, [
    marked.size ? el('label', { class: 'chk' }, [
      el('input', { type: 'checkbox', onchange: e => { st.only = e.target.checked; paint(); } }),
      document.createTextNode(' matched lines only (' + marked.size + ')'),
    ]) : null,
    el('span', { class: 'dim', text: v.note || '' }),
  ]);
  box.appendChild(tools);
  const wrap = el('div', { class: 'btwrap' });
  box.appendChild(wrap);
  function paint() {
    wrap.innerHTML = '';
    const ths = [el('th', { text: '' }), el('th', { text: 'Item' }), el('th', { text: 'Description (as written)' }),
      el('th', { text: 'Unit' }), el('th', { class: 'num', text: 'Qty' })]
      .concat(bidders.map(b => el('th', { class: 'num' + (b.low ? ' lowcol' : ''), title: b.name }, [
        el('div', { class: 'bname', text: b.name }),
        el('div', { class: 'dim', text: money(b.total) }),
      ])))
      .concat([el('th', { class: 'num', text: 'Median $/unit' })])
      .concat(ee ? [el('th', { class: 'num eecol', title: ee.label, text: 'Engineer\u2019s est. (printed)' })] : []);
    const body = el('tbody', {});
    v.lines.forEach((ln, i) => {
      const ms = marked.get(i);
      if (st.only && !ms) return;
      const desc = el('td', { class: 'btdesc' }, [el('div', { text: ln.description })]);
      (ms || []).forEach(m => desc.appendChild(el('div', { class: 'btmatch' }, [
        el('span', { class: 'mk', text: '\u2194 your ' + (m.ours.item_no ? m.ours.item_no + ' ' : '')
          + (m.ours.description || '') }),
        el('span', { class: 'nums', text: ['their low ' + rate(m.nums.low), 'their median ' + rate(m.nums.median),
          'ours ' + rate(m.nums.p50)].join(' \u00b7 ') + (m.nums.unit ? ' /' + m.nums.unit : '') }),
      ])));
      const tds = [
        el('td', { class: 'btmk', text: ms ? '\u25b6' : '' }),
        el('td', { text: ln.item_no }), desc, el('td', { text: ln.unit }),
        el('td', { class: 'num', text: qtyFmt(ln.quantity) }),
      ].concat(ln.prices.map((p, k) => el('td', { class: 'num' + (bidders[k] && bidders[k].low ? ' lowcol' : '') },
        p ? [el('div', { text: rate(p.unit_price) }), el('div', { class: 'dim', text: money(p.amount) })] : '\u2014')))
        .concat([el('td', { class: 'num', text: rate(ln.median_unit) })])
        .concat(ee ? [el('td', { class: 'num eecol' }, ln.ee
          ? [el('div', { text: rate(ln.ee.unit_price) }), el('div', { class: 'dim', text: money(ln.ee.amount) })]
          : '\u2014')] : []);
      body.appendChild(el('tr', { class: ms ? 'hl' : '' }, tds));
    });
    const foot = el('tr', { class: 'tot' }, [el('td', {}), el('td', {}), el('td', { text: 'Total' }),
      el('td', {}), el('td', {})].concat(bidders.map(b => el('td', { class: 'num strong' + (b.low ? ' lowcol' : ''),
        text: money(b.total) }))).concat([el('td', {})]).concat(ee ? [el('td', { class: 'num eecol', text: money(ee.total) })] : []));
    wrap.appendChild(el('table', { class: 'mini btgrid' }, [el('thead', {}, el('tr', {}, ths)), body,
      el('tfoot', {}, foot)]));
    if (opts.onPaint) opts.onPaint();
  }
  paint();
  return box;
}

const BT = { cache: {} };
async function fetchBidTab(key, tries) {
  if (BT.cache[key]) return BT.cache[key];
  try {
    const v = await api('/api/letting?key=' + encodeURIComponent(key));
    BT.cache[key] = v;
    return v;
  } catch (e) {
    if (e.status === 503 && (tries || 0) < 10) {
      await new Promise(r => setTimeout(r, 1500));
      return fetchBidTab(key, (tries || 0) + 1);
    }
    throw e;
  }
}

async function showBidTab(host, key, marks, fallback) {
  host.innerHTML = '';
  host.appendChild(el('span', { class: 'dim', html: '<span class="spinner"></span> loading the full bid tabulation\u2026' }));
  try {
    const v = await fetchBidTab(key);
    host.innerHTML = '';
    host.appendChild(bidTabView(v, marks));
  } catch (e) {
    host.innerHTML = '';
    host.appendChild(notice('caution', 'Could not load the full bid', String(e.message || e)));
    if (fallback) host.appendChild(fallback());
  }
}

function openBidTab(key, title, marks) {
  const host = $('#modal-host');
  const inner = el('div', {});
  function close() { host.innerHTML = ''; document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);
  const back = el('div', { class: 'modal-back', onclick: e => { if (e.target === back) close(); } },
    el('div', { class: 'modal wide' }, [
      el('header', {}, [el('h3', { text: 'Full bid tabulation: ' + title }),
        el('button', { class: 'btn sm close', text: 'Close', onclick: close })]),
      el('div', { class: 'mbody' }, inner),
    ]));
  host.innerHTML = '';
  host.appendChild(back);
  showBidTab(inner, key, marks);
}

// the reference row's lines in that letting, as marks for the full bid
function refMarks(r, x) {
  const rep = r.reproduced || {};
  const p50 = rep.trace_price && r.scale_to_p50 ? rep.trace_price * r.scale_to_p50 : null;
  return (x.match || []).map(mt => ({
    test: (ln, strict) => ln.dn === mt.dn
      && (!strict || (String(ln.unit).toUpperCase() === String(mt.unit).toUpperCase()
        && (mt.quantity === null || qClose(ln.quantity, mt.quantity)))),
    ours: { item_no: r.item_no, description: r.description },
    nums: { low: x.low_unit, median: x.median_unit, p50, unit: r.unit },
  }));
}

// a similar card's matched lines, as marks for the full bid
function cardMarks(c) {
  return (c.matched_lines || []).map(m => ({
    test: (ln, strict) => ln.dn === m.their_dn && (!strict || (ln.item_no === m.their_item_no
      && String(ln.unit).toUpperCase() === String(m.their_unit).toUpperCase()
      && qClose(ln.quantity, m.their_quantity))),
    ours: { item_no: m.item_no, description: m.description },
    nums: { low: m.their_low_unit, median: m.their_median_unit, p50: m.our_unit_p50, unit: m.unit },
  }));
}

function projLink(text, onclick, title) {
  return el('a', { class: 'projlink', href: '#', title: title || 'Open the full bid tabulation',
    text, onclick: e => { e.preventDefault(); onclick(); } });
}

const REF_COLS = [
  ['date', 'Bid date', x => x.date],
  ['project', 'Project', null],
  ['bidders', 'Bidders', null],
  ['low_total', 'Project low bid', null],
  ['item', 'Item as written', null],
  ['quantity', 'Qty', x => x.quantity || 0],
  ['mean_unit', 'Average bid $/unit', x => x.mean_unit || 0],
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
  const rep = r.reproduced || {};
  const cell = r.cell || {};
  const what = { tok: 'similarly worded items', band: 'this kind of item at this size and quantity',
    fam: 'this kind of item' }[r.kind] || 'this kind of item';
  box.appendChild(el('div', { class: 'deriv', text: 'Our price ' + rate(r.p50_unit_price) + '/' + r.unit
    + ' (in the expected low bid) starts from the median bid on ' + (cell.lettings || '?')
    + ' earlier public project' + (cell.lettings === 1 ? '' : 's') + ' with ' + what
    + (cell.years ? ' (' + cell.years + ')' : '') + ': ' + rate(r.market_unit_price) + '/' + r.unit
    + (r.to_year ? ' in ' + r.to_year + ' dollars' : '') + ', then less the competition discount.' }));
  if (r.warning) box.appendChild(notice('caution', null, r.warning));
  box.appendChild(el('div', { class: 'muted-note repro ' + (rep.price_match && rep.rows_match ? 'ok' : 'bad'),
    text: (rep.price_match && rep.rows_match)
      ? '\u2713 Checked: these projects give exactly this price.'
      : '! These projects do not give exactly this price \u2014 see how it was calculated.' }));
  box.appendChild(el('details', { class: 'fold howcalc' }, [
    el('summary', { text: 'How this was calculated' }),
    el('div', { class: 'howbody' }, [
      el('div', { text: r.derivation }),
      el('div', { style: 'margin-top:4px', text: (rep.price_match && rep.rows_match)
        ? 'The median of these lettings\u2019 median bids is the estimator\u2019s cell price '
          + money(rep.published_cell_price, 2) + ', and with the factors above it gives exactly '
          + rate(rep.trace_price) + '/' + r.unit + '.'
        : 'Cell ' + money(rep.published_cell_price, 2) + ' vs ' + money(rep.cell_price_from_rows, 2)
          + ' from the rows; price ' + rate(rep.price) + ' vs ' + rate(rep.trace_price) + '.' }),
    ]),
  ]));
  if (r.note) box.appendChild(el('div', { class: 'muted-note', text: r.note }));
  const all = r.references || [];
  if (!all.length) {
    box.appendChild(el('div', { class: 'muted-note', text: 'None of these projects was bid before the bid date.' }));
    return box;
  }
  const state = { all: false, sort: null, dir: -1 };
  const tableHost = el('div', { class: 'refscroll' });
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
      class: (['bidders', 'low_total', 'quantity', 'mean_unit', 'median_unit', 'low_unit'].includes(k) ? 'num' : '')
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
        el('td', { title: x.title || '' }, [
          el('div', {}, projLink(x.project, () => openBidTab(x.key, x.project, refMarks(r, x)))),
          el('div', { class: 'dim', text: x.owner + (x.same_owner ? ' (same owner)' : '') }),
          srcLinks(x.source),
        ]),
        el('td', { class: 'num', text: x.bidders || '—' }),
        el('td', { class: 'num', text: money(x.low_total) }),
        el('td', { text: x.item }),
        el('td', { class: 'num', text: qtyFmt(x.quantity) }),
        // the average bid: what an OPCC's unit-cost source usually quotes
        el('td', { class: 'num' }, [
          el('div', { text: rate(x.mean_unit) }),
          x.escalated_mean ? el('div', { class: 'dim', text: '≈ ' + rate(x.escalated_mean)
            + ' in ' + r.to_year + ' $' }) : null,
        ]),
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
      : 'All ' + all.length) + ' earlier public project' + (all.length === 1 ? '' : 's')
    + ' bid before ' + r.bid_date + ' (most recent, most alike in wording and quantity, and the same '
    + 'owner first). The estimator uses their median bid.' }));
  box.appendChild(tableHost);
  box.appendChild(more);
  paint();
  return box;
}

function bidsTable(x) {
  return el('div', { class: 'bidsbox' }, [
    el('div', { class: 'dim', text: x.project + ' · ' + x.date + ' · '
      + (x.source && x.source.document ? x.source.document : '') }),
    srcLinks(x.source),
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
    host.appendChild(el('div', { class: 'muted-note', style: 'margin-bottom:6px', text: 'The past public '
      + 'jobs most like this one: the same kinds of items, a similar size, the same owner or work type, '
      + 'recent. The expected low bid weighs what their low bidders charged for matching lines. '
      + 'Click a card to see its whole bid.' }));
    host.appendChild(el('details', { class: 'fold howcalc', style: 'margin-bottom:9px' }, [
      el('summary', { text: 'How this was calculated' }),
      el('div', { class: 'howbody', text: s.how + ' Line-priced ' + money(s.line_price)
        + ' \u00b7 comparables ' + money(s.cmp_price) + ' \u2192 blend ' + money(s.blend)
        + (s.level_correction ? ' \u2192 \u00d7' + s.level_correction.toFixed(4) + ' centring' : '') + '.' }),
    ]));
  }
  const detail = el('div', { class: 'simdetail', id: 'sim-detail' });
  const grid = el('div', { class: 'simgrid' });
  cards.forEach(c => grid.appendChild(simCard(c, r, detail, grid)));
  host.appendChild(grid);
  host.appendChild(detail);
}

// The card's state, shown on its header (chevron, aria-expanded) and button.
function simCardState(card, open) {
  card.classList.toggle('sel', open);
  const head = $('.simhead', card), chev = $('.chev', card), btn = $('.simfoot button', card);
  if (head) head.setAttribute('aria-expanded', String(open));
  if (chev) chev.textContent = open ? '▾' : '▸';
  if (btn) btn.textContent = open ? 'Hide full bid' : 'Show full bid'
    + (btn.dataset.nm && btn.dataset.nm !== '0' ? ' (' + btn.dataset.nm + ' matched)' : '');
}

function simCard(c, r, detail, grid) {
  const b = c.bids || {};
  const sh = c.shares || {};
  const at = c.at_their_prices;
  const f = c.factors;
  const norm = x => String(x || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const showTitle = c.title && norm(c.title) !== norm(c.project);
  const simTip = f ? 'Similarity ' + c.similarity.toFixed(3) + ' = overlap '
    + pct(f.overlap) + ' × size ' + f.size.toFixed(2) + ' × same owner '
    + f.same_owner.toFixed(1) + ' × same work type ' + f.same_work_type.toFixed(1)
    + ' × age ' + f.age.toFixed(2) + ' (' + f.age_years.toFixed(1) + ' years before)' : '';
  const nm = (c.matched_lines || []).length;
  const btn = el('button', { class: 'btn sm', type: 'button', 'data-nm': String(nm),
    'aria-controls': 'sim-detail',
    title: 'Every line and every bidder of this letting, our matched lines highlighted' });
  // the header opens the whole bid below the cards, like a click on a line
  // of the worksheet opens its references
  const head = el('div', { class: 'simhead', tabindex: '0', role: 'button',
    'aria-expanded': 'false', 'aria-controls': 'sim-detail',
    title: 'Click to show or hide the whole bid, our matched lines highlighted' }, [
    el('div', { class: 'simtop' }, [
      el('span', { class: 'chev', 'aria-hidden': 'true', text: '\u25b8' }),
      el('span', { class: 'rank', text: c.rank }),
      el('span', { class: 'own', text: c.owner }),
      el('span', { class: 'dim', text: c.date }),
    ]),
    el('div', { class: 'simname', title: c.key, text: c.project }),
    showTitle ? el('div', { class: 'simtitle', title: c.title, text: c.title }) : null,
  ]);
  const card = el('div', { class: 'simcard' }, [
    head,
    el('div', { class: 'simsub', text: (c.work_type || 'work type not given') + ' · '
      + c.bidders + ' bidder' + (c.bidders === 1 ? '' : 's') }),
    el('dl', { class: 'kv simkv' }, [
      el('dt', { text: 'Low bid' }), el('dd', { class: 'strong', text: money(b.low) }),
      el('dt', { text: 'Other bids' }), el('dd', { text: '2nd ' + short$(b.second) + ' · median '
        + short$(b.median) + ' · high ' + short$(b.high) }),
      el('dt', { text: 'Size' }), el('dd', { text: isNum(c.size_vs_this_job)
        ? c.size_vs_this_job.toFixed(2) + '× this job' : '—',
        title: 'their low bid / our expected low bid' }),
      el('dt', { text: 'Overlap' }), el('dd', { title: simTip, text: 'shares ' + sh.types + ' of '
        + sh.of_types + ' item types, ' + pct(sh.dollar_share) + ' of the dollars' }),
      el('dt', { text: 'At their prices' }), el('dd', {
        title: at ? 'This job priced from this project\u2019s low-bid prices alone (adjusted to '
          + 'today\u2019s prices, the quantities and the competition); lines it has no match '
          + 'for stay at our own price. Owner-set money added at face value.' : '',
        text: at ? money(at.job) + ' (' + (isNum(at.vs_p50) ? at.vs_p50.toFixed(2) + '\u00d7 our expected low bid; ' : '')
          + 'their lines ' + pct(at.matched_share) + ')' : '\u2014' }),
    ]),
    el('div', { class: 'simsrc' }, [
      el('div', { class: 'dim', text: c.source.document || c.key }),
      srcLinks(c.source),
    ]),
    el('div', { class: 'simfoot' }, [btn,
      el('span', { class: 'dim', title: simTip, text: 'score ' + (isNum(c.similarity) ? c.similarity.toFixed(2) : '—') })]),
  ]);
  const toggle = () => {
    const open = card.classList.contains('sel');
    $$('.simcard', grid).forEach(x => simCardState(x, false));
    detail.innerHTML = '';
    if (open) return;
    simCardState(card, true);
    detail.appendChild(el('h5', { text: '#' + c.rank + ' ' + c.project + ' \u2014 the whole bid, '
      + 'our matched lines highlighted' }));
    const inner = el('div', {});
    detail.appendChild(inner);
    showBidTab(inner, c.key, cardMarks(c), () => matchedTable(c));
    // below the cards: bring it into view when it opened off screen
    const top = detail.getBoundingClientRect().top;
    if (top > window.innerHeight * 0.8) detail.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };
  btn.addEventListener('click', toggle);
  head.addEventListener('click', e => { if (rowToggles(e)) toggle(); });
  head.addEventListener('keydown', e => {
    if (e.target !== head || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    toggle();
  });
  simCardState(card, false);
  return card;
}

function matchedTable(c) {
  const rows = c.matched_lines || [];
  return el('div', {}, [
    el('h5', { text: '#' + c.rank + ' ' + c.project + ' — matched lines' }),
    el('div', { class: 'muted-note', style: 'margin-bottom:6px', text: 'Each of our lines with the '
      + 'closest line of this project (best word match, then the closest '
      + 'quantity). Their unit prices are as bid on ' + c.date + '; ours are in the expected low bid for '
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
    el('dt', { text: 'Bidders' }), el('dd', { text: c.n_bidders + (c.source === 'rule N1'
      ? '  (suggested)' : '  (entered by you)') }),
    s ? el('dt', { text: 'Suggested' }) : null,
    s ? el('dd', { text: s.n + '  \u2014 ' + (s.cell === 'default' ? 'too few similar earlier jobs, so the usual 4'
      : 'the typical number on ' + s.history_lettings + ' earlier ' + s.cell_label) }) : null,
    el('dt', { text: 'Competition discount' }), el('dd', { text: c.available ? 'the low bid is typically '
      + Number(c.f).toFixed(2) + ' \u00d7 the median bid' : 'none \u2014 no estimate' }),
    c.available ? el('dt', { text: 'Based on' }) : null,
    c.available ? el('dd', { text: c.lettings + ' past ' + (c.d1 ? 'TxDOT jobs (size-aware)'
      : 'jobs with ' + c.bin + ' bidders') + (c.extrapolated ? ' (the nearest count)' : '') }) : null,
    c.available && c.p25 ? el('dt', { text: 'Middle half of those jobs' }) : null,
    c.available && c.p25 ? el('dd', { text: Number(c.p25).toFixed(2) + ' \u2013 '
      + Number(c.p75).toFixed(2) + ' \u00d7 the median bid' }) : null,
  ]));
  if (c.note || c.table) host.appendChild(el('details', { class: 'fold howcalc' }, [
    el('summary', { text: 'How this was calculated' }),
    el('div', { class: 'howbody', text: 'Discount f(' + c.n_bidders + ') = \u00d7'
      + (c.available ? Number(c.f).toFixed(4) : '\u2014') + ' on the median bid'
      + (c.available ? ' \u00b7 ' + (c.d1 ? 'size-aware D1 discount (TxDOT T1)' : c.table + ' table, '
        + c.bin + '-bidder bin') : '') + (s ? ' \u00b7 bidders: rule N1, ' + (s.note || '') : '')
      + (c.note ? ' ' + c.note : '') }),
  ]));
}

/* ================================================================= EXPORT */

function initExport() {
  // the one export, here only (user, 2026-11-21): the OPCC workbook -- its
  // sheet in the delivered layout, a one-page summary next to the model's
  // P50 / P70, every line, and the past projects behind each price
  // Excel, and the OPCC as it prints as a PDF (user, 2026-11-21)
  ['xlsx', 'pdf'].forEach(kind => $('#btn-export-' + kind).addEventListener('click', () => {
    if (!S.result) return;
    if (S.stale) toast('Exporting the last estimate \u2014 inputs changed since; re-estimate first '
      + 'if you want them in.', 'warn', 7000);
    wsExport(kind);
  }));
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
  $('#btn-about').addEventListener('click', openAbout);
  initTheme();
  initInput();
  initSortable();
  initClaude();
  initAiMode();
  initWorksheet();
  initProject();
  initExport();
  renderStatusBar();
  renderQuotes();
  renderOwnerSet();
  refreshEstimateButton();
  pollStatus();
}

document.addEventListener('DOMContentLoaded', boot);
