/* Bid Estimator front end.
 *
 * Plain DOM, no framework and no build step: this is a local tool that has to
 * start with `python ui/run.py` on a Windows box with no npm and no network.
 *
 * State lives in `S`. The review grid is the only editable surface and it is
 * the single source of truth for what gets priced.
 */
'use strict';

const S = {
  lines: [],          // {row_id, description, unit, quantity, item_no, include,
                      //  issues, confidence, match: Candidate|null, candidates: []}
  parse: null,
  result: null,
  options: { owners: [], work_types: [], discount_curve: [], units: [] },
  status: null,
  view: 'input',
  seq: 0,
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

const money = (v, nd) => (v === null || v === undefined || isNaN(v)) ? '\u2014'
  : '$' + Number(v).toLocaleString('en-US',
      { minimumFractionDigits: nd === undefined ? 0 : nd,
        maximumFractionDigits: nd === undefined ? 0 : nd });

function rate(v) {
  if (v === null || v === undefined || isNaN(v)) return '\u2014';
  const a = Math.abs(v);
  return '$' + Number(v).toLocaleString('en-US',
    { minimumFractionDigits: a < 10 ? 2 : (a < 1000 ? 2 : 0),
      maximumFractionDigits: a < 10 ? 2 : (a < 1000 ? 2 : 0) });
}

const qtyFmt = (v) => (v === null || v === undefined || v === '' || isNaN(v))
  ? '' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });

const pct = (v, nd) => (v === null || v === undefined || isNaN(v)) ? '\u2014'
  : Number(v).toFixed(nd === undefined ? 1 : nd) + '%';

function parseNum(s) {
  if (s === null || s === undefined) return null;
  const t = String(s).replace(/[$,\s]/g, '');
  if (!t || !/^-?\d*\.?\d+$/.test(t)) return null;
  const f = parseFloat(t);
  return isFinite(f) ? f : null;
}

function scoreClass(x) { return x >= 0.60 ? 'good' : (x >= 0.34 ? 'mid' : 'weak'); }

/* ------------------------------------------------------------------- http */

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (!r.ok) {
    let msg = r.status + ' ' + r.statusText;
    if (ct.includes('json')) {
      try { const j = await r.json(); msg = j.detail || j.error || msg; } catch (e) { /* keep */ }
    }
    throw new Error(msg);
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

/* -------------------------------------------------------------- navigation */

function setView(v) {
  S.view = v;
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-' + v));
  $$('#steps button').forEach(b =>
    b.setAttribute('aria-current', String(b.dataset.view === v)));
  if (v === 'data') renderData();
  window.scrollTo(0, 0);
}

function enableSteps() {
  $('#steps button[data-view=review]').disabled = S.lines.length === 0;
  $('#steps button[data-view=result]').disabled = !S.result;
}

/* ====================================================================
   1. INPUT
   ==================================================================== */

const SAMPLE = [
  'Mobilization\tLS\t1',
  'Traffic Control\tMO\t6',
  'Unclassified Channel Excavation\tCY\t4,200',
  'Grouted Rock Riprap, 18 inch\tSY\t2,600',
  'Gabion Mattress, 12 inch thick\tSY\t880',
  '8 inch PVC SDR-26 Sanitary Sewer Pipe\tLF\t1,450',
  'Block Sod, Bermuda\tSY\t9,500',
  'Trench Safety System\tLF\t1,450',
  'Soil Nail Anchors, 20 ft\tEA\t64',
  'Remove and Replace Reinforced Concrete Curb and Gutter\tLF\t620',
].join('\n');

function initInput() {
  $$('#input-tabs button').forEach(b => b.addEventListener('click', () => {
    $$('#input-tabs button').forEach(x =>
      x.setAttribute('aria-selected', String(x === b)));
    $$('#view-input .tabpane').forEach(p =>
      p.classList.toggle('active', p.id === 'pane-' + b.dataset.pane));
  }));

  $('#btn-sample').addEventListener('click', () => {
    $('#paste-text').value = SAMPLE;
    if (!$('#in-bidders').value) $('#in-bidders').value = '4';
    onBidders();
  });

  $('#btn-parse-text').addEventListener('click', async () => {
    const text = $('#paste-text').value;
    if (!text.trim()) { toast('Nothing to parse \u2014 paste your items first.', 'err'); return; }
    await withBusy($('#btn-parse-text'), async () => {
      const p = await postJSON('/api/takeoff/text', { text });
      await acceptParse(p);
    });
  });

  // file upload
  const dz = $('#dropzone'), fi = $('#file-input');
  dz.addEventListener('click', () => fi.click());
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('over');
    if (e.dataTransfer.files[0]) uploadTakeoff(e.dataTransfer.files[0]);
  });
  fi.addEventListener('change', () => { if (fi.files[0]) uploadTakeoff(fi.files[0]); });

  $('#in-bidders').addEventListener('input', onBidders);

  // bid-tab (scenario B)
  const bz = $('#bidtab-drop'), bi = $('#bidtab-input');
  bz.addEventListener('click', () => bi.click());
  bz.addEventListener('dragover', e => { e.preventDefault(); bz.classList.add('over'); });
  bz.addEventListener('dragleave', () => bz.classList.remove('over'));
  bz.addEventListener('drop', e => {
    e.preventDefault(); bz.classList.remove('over');
    if (e.dataTransfer.files[0]) uploadBidtab(e.dataTransfer.files[0]);
  });
  bi.addEventListener('change', () => { if (bi.files[0]) uploadBidtab(bi.files[0]); });
}

async function withBusy(btn, fn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> working\u2026';
  try { await fn(); }
  catch (e) { toast(String(e.message || e), 'err'); }
  finally { btn.disabled = false; btn.innerHTML = old; }
}

async function uploadTakeoff(file) {
  const st = $('#file-status');
  st.innerHTML = '';
  st.appendChild(el('div', { class: 'notice info' }, [
    el('span', { class: 'ic', html: '<span class="spinner"></span>' }),
    el('div', { class: 'body-txt', text: 'Reading ' + file.name + '\u2026' }),
  ]));
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

function onBidders() {
  const n = parseInt($('#in-bidders').value, 10);
  const box = $('#discount-preview');
  const inp = $('#in-bidders');
  box.innerHTML = '';
  inp.classList.remove('invalid');
  if (!n || n < 1) { inp.classList.add('invalid'); return; }

  const curve = S.options.discount_curve || [];
  if (!curve.length) return;
  const ns = curve.map(r => r.n_bidders);
  const lo = Math.min(...ns), hi = Math.max(...ns);
  const used = Math.min(Math.max(n, lo), hi);
  const row = curve.reduce((a, b) =>
    Math.abs(b.n_bidders - used) < Math.abs(a.n_bidders - used) ? b : a);

  const bits = [
    el('dl', { class: 'kv' }, [
      el('dt', { text: 'Discount applied' }),
      el('dd', { text: '\u00d7' + row.low_over_median.toFixed(3) }),
      el('dt', { text: 'Measured over' }),
      el('dd', { text: row.lettings.toLocaleString('en-US') + ' lettings' }),
    ]),
  ];
  let kind = 'ok', title = 'Competition discount at ' + n + ' bidders';
  if (n < 2) {
    kind = 'severe';
    title = 'One bidder is not a competitive price';
    bits.push(el('div', { class: 'muted-note', text:
      'The table reads 1.00 at a single bidder because there is no competition to '
      + 'measure, so no competition adjustment is applied at all.' }));
  } else if (used !== n) {
    kind = 'caution';
    title = 'Outside the measured range (' + lo + '\u2013' + hi + ' bidders)';
    bits.push(el('div', { class: 'muted-note', text:
      'The discount at ' + used + ' bidders is used; it is not extrapolated.' }));
  }
  const nb = el('div', { class: 'notice ' + kind }, [
    el('span', { class: 'ic', text: kind === 'ok' ? '\u2713' : '!' }),
    el('div', { class: 'body-txt' }, [el('h4', { text: title })].concat(bits)),
  ]);
  box.appendChild(nb);
}

function notice(kind, title, body) {
  return el('div', { class: 'notice ' + kind }, [
    el('span', { class: 'ic', text: kind === 'severe' ? '\u2715'
      : kind === 'caution' ? '!' : kind === 'ok' ? '\u2713' : 'i' }),
    el('div', { class: 'body-txt' }, [
      title ? el('h4', { text: title }) : null,
      body ? el('div', { text: body }) : null,
    ]),
  ]);
}

/* ====================================================================
   2. REVIEW
   ==================================================================== */

async function acceptParse(p) {
  S.parse = p;
  S.result = null;
  S.lines = (p.lines || []).map(l => ({
    row_id: l.row_id || ('r' + (++S.seq)),
    description: l.description || '',
    unit: l.unit || '',
    quantity: l.quantity,
    item_no: l.item_no || '',
    include: true,
    issues: l.issues || [],
    confidence: l.confidence || 'ok',
    match: null,
    candidates: [],
    mock: !!p.mock,
  }));
  S.seq = Math.max(S.seq, S.lines.length);
  renderParseNotes();
  renderReview();
  enableSteps();
  if (!S.lines.length) {
    setView('review');
    toast('No line items could be read \u2014 see the notes.', 'err');
    return;
  }
  setView('review');
  toast('Parsed ' + S.lines.length + ' line item(s). Confirm them before pricing.', 'ok');
  await rematchAll();
}

function renderParseNotes() {
  const host = $('#parse-notes');
  host.innerHTML = '';
  const p = S.parse;
  if (!p) return;

  if (p.mock) {
    host.appendChild(notice('severe', 'These items are MOCK data, not read from your file',
      'The OCR engine (src/ocr_extract.py) has not been delivered yet, so the image '
      + 'reader returned a stand-in. Nothing here may be priced. Paste the items as '
      + 'text instead.'));
  }
  if (p.notes && p.notes.length) {
    host.appendChild(el('div', { class: 'notice info' }, [
      el('span', { class: 'ic', text: 'i' }),
      el('div', { class: 'body-txt' }, [
        el('h4', { text: 'How "' + (p.source || 'input') + '" was read ('
          + (p.method || '?') + ')' }),
        el('ul', { style: 'margin:4px 0 0; padding-left:18px' },
          p.notes.map(n => el('li', { text: n }))),
      ]),
    ]));
  }

  const fold = $('#skipped-fold');
  fold.innerHTML = '';
  if (p.n_skipped) {
    fold.appendChild(el('summary', { text: p.n_skipped
      + ' row(s) were seen and not treated as pay items \u2014 show them' }));
    const t = el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Row' }), el('th', { text: 'Why' }), el('th', { text: 'Content' })])),
      el('tbody', {}, (p.skipped || []).map(s => el('tr', {}, [
        el('td', { text: s.row }), el('td', { text: s.reason }),
        el('td', { class: 'dim', text: s.raw })]))),
    ]);
    fold.appendChild(el('div', { style: 'padding:8px 2px' }, t));
  }
}

// The grid is rebuilt wholesale on every change, and a re-match is async, so
// a rebuild can land in the middle of the estimator typing somewhere else. The
// description and unit cells write to the model on `input`, but the quantity
// cell only commits on `change` -- so its typed text lived in the DOM alone and
// a rebuild silently reverted it. Typing `12345` into row 3 while row 1's
// re-match was in flight put row 3 back to `880` with no sign anything had
// happened, and threw focus to <body>. Racy, silent, and in the one workflow
// this tool exists for, so the focused cell's own text and caret are carried
// across every rebuild.
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
  if (!cell) return;                      // row deleted; nothing to restore to
  // What they had typed is theirs, not ours to discard for a model value.
  if (cell.value !== st.value) cell.value = st.value;
  cell.focus();
  if (st.sel) {
    try { cell.setSelectionRange(st.sel[0], st.sel[1]); } catch (e) { /* ok */ }
  }
}

function renderReview() {
  const focus = gridFocusState();
  const body = $('#review-body');
  body.innerHTML = '';
  S.lines.forEach((ln, i) => body.appendChild(reviewRow(ln, i)));
  const n = S.lines.filter(l => l.include).length;
  const missing = S.lines.filter(l => l.include && !(l.quantity > 0)).length;
  $('#review-count').textContent = n + ' of ' + S.lines.length + ' included'
    + (missing ? ' \u00b7 ' + missing + ' with no quantity' : '');
  $('#btn-estimate').disabled = n === 0;
  restoreGridFocus(focus);
}

// A match the estimator picked by hand was chosen FOR a particular
// description. Edit that description and the pick no longer refers to
// anything, but `matchOne` used to bail out early on `userPicked` while the
// `oninput` handler had already cleared the match -- so the row sat on
// "matching\u2026" for ever, with no match and no score, and no way back
// except deleting the row. Releasing the pick is the honest reading: it
// belonged to text that no longer exists. Said out loud, because silently
// discarding a deliberate choice is its own kind of wrong.
function releasePick(ln) {
  if (!ln.userPicked) return;          // fires once, not once per keystroke
  ln.userPicked = false;
  toast('Row ' + (ln.item_no || String(ln.row_id).replace(/^r/, ''))
    + ': your chosen match was released because the description changed. '
    + 'Re-matching \u2014 use Change again if you still want it.', 'warn', 7000);
}

function reviewRow(ln, i) {
  const tr = el('tr', { class: ln.include ? '' : 'excluded', 'data-row': ln.row_id });

  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    type: 'checkbox', checked: ln.include,
    title: 'Include this line in the estimate',
    onchange: e => { ln.include = e.target.checked; renderReview(); },
  })));

  tr.appendChild(el('td', { class: 'dim num', text: ln.item_no || String(i + 1) }));

  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput', type: 'text', value: ln.description,
    'data-col': 'description', 'data-i': i,
    oninput: e => {
      ln.description = e.target.value; ln.match = null; releasePick(ln);
    },
    onchange: () => matchOne(ln),
  })));

  const unitBad = !ln.unit;
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput' + (unitBad ? ' flag' : ''), type: 'text', value: ln.unit,
    'data-col': 'unit', 'data-i': i, placeholder: 'unit',
    oninput: e => { ln.unit = e.target.value.toUpperCase(); releasePick(ln); },
    onchange: e => {
      e.target.classList.toggle('flag', !ln.unit);
      matchOne(ln);
    },
  })));

  const qtyBad = !(ln.quantity > 0);
  tr.appendChild(el('td', { class: 'tight' }, el('input', {
    class: 'cellinput num' + (qtyBad ? ' flag' : ''), type: 'text',
    value: qtyFmt(ln.quantity), 'data-col': 'quantity', 'data-i': i,
    placeholder: 'required',
    onchange: e => {
      ln.quantity = parseNum(e.target.value);
      e.target.value = qtyFmt(ln.quantity);
      e.target.classList.toggle('flag', !(ln.quantity > 0));
      renderReview();
    },
  })));

  const m = ln.match;
  // Whose choice this was has to be legible on the row. Reviewing a 60-line
  // sheet later -- or handing it to a colleague -- there was otherwise no way
  // to tell a match the estimator made deliberately from one the matcher
  // guessed, which is exactly the distinction this tool exists to support.
  tr.appendChild(el('td', {}, m
    ? el('div', {}, [
        ln.userPicked ? el('span', { class: 'badge info',
          style: 'margin-right:5px', title: 'You chose this match by hand',
          text: 'your pick' }) : null,
        el('span', { text: m.label || '(unnamed)' }),
        el('br'),
        el('span', { class: 'dim', style: 'font-size:11px',
          text: m.kind + ' \u00b7 ' + m.detail }),
      ])
    : el('span', { class: 'dim', text: ln.description ? 'matching\u2026' : '\u2014' })));

  tr.appendChild(el('td', { class: 'num' }, m
    ? el('span', { class: 'score ' + scoreClass(m.score), text: m.score.toFixed(2) })
    : el('span', { class: 'dim', text: '\u2014' })));

  tr.appendChild(el('td', {}, el('div', { class: 'row-actions' }, [
    el('button', { class: 'btn ghost sm', text: 'Change',
      title: 'Pick a different historical item',
      onclick: () => openPicker(ln) }),
    el('button', { class: 'btn ghost sm danger', text: '\u2715', title: 'Delete row',
      onclick: () => { S.lines = S.lines.filter(x => x !== ln); renderReview(); enableSteps(); } }),
  ])));

  return tr;
}

function initReview() {
  $('#btn-add-row').addEventListener('click', () => {
    S.lines.push({ row_id: 'r' + (++S.seq), description: '', unit: '', quantity: null,
      item_no: '', include: true, issues: [], confidence: 'ok',
      match: null, candidates: [] });
    renderReview();
    enableSteps();
    const rows = $$('#review-body tr');
    const last = rows[rows.length - 1];
    if (last) $('input[data-col=description]', last).focus();
  });

  $('#btn-rematch').addEventListener('click', () =>
    withBusy($('#btn-rematch'), rematchAll));

  $('#btn-estimate').addEventListener('click', () =>
    withBusy($('#btn-estimate'), runEstimate));

  $('#btn-back-review').addEventListener('click', () => setView('review'));

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
}

async function rematchAll() {
  const items = S.lines
    .filter(l => (l.description || '').trim())
    .map(l => ({ row_id: l.row_id, description: l.description, unit: l.unit }));
  if (!items.length) { renderReview(); return; }
  const r = await postJSON('/api/match', { items });
  const by = {};
  (r.matches || []).forEach(m => { by[m.row_id] = m; });
  S.lines.forEach(l => {
    const m = by[l.row_id];
    if (m) { l.match = m.best; l.candidates = m.candidates || []; l.userPicked = false; }
  });
  renderReview();
}

async function matchOne(ln) {
  if (!(ln.description || '').trim()) { ln.match = null; renderReview(); return; }
  if (ln.userPicked) { renderReview(); return; }
  try {
    const r = await postJSON('/api/match', {
      items: [{ row_id: ln.row_id, description: ln.description, unit: ln.unit }] });
    const m = (r.matches || [])[0];
    if (m) { ln.match = m.best; ln.candidates = m.candidates || []; }
  } catch (e) { toast(String(e.message || e), 'err'); }
  renderReview();
}

/* ------------------------------------------------------------ match picker */

function openPicker(ln) {
  let cands = (ln.candidates || []).slice();
  const host = $('#modal-host');

  const list = el('div', {});
  const qinput = el('input', {
    type: 'text', value: ln.description || '', placeholder: 'search historical items\u2026',
  });

  function paint() {
    list.innerHTML = '';
    if (!cands.length) {
      list.appendChild(el('div', { class: 'muted-note',
        text: 'Nothing matched. Try different wording \u2014 e.g. the material and '
            + 'size ("18 inch rock riprap") rather than a project-specific label.' }));
      return;
    }
    cands.forEach(c => {
      list.appendChild(el('div', {
        class: 'pick' + (ln.match && ln.match.key === c.key ? ' sel' : ''),
        onclick: () => {
          ln.match = c;
          ln.userPicked = true;
          close();
          renderReview();
          toast('Match set to "' + (c.label || c.key) + '".', 'ok');
        },
      }, [
        el('span', { class: 'score ' + scoreClass(c.score), text: c.score.toFixed(2) }),
        el('span', { class: 'lab' }, [
          el('span', { class: 't', text: c.label || '(unnamed)' }),
          el('span', { class: 's', text: c.kind + ' \u00b7 ' + c.detail
            + (c.median_price ? ' \u00b7 median ' + rate(c.median_price) + '/' + c.unit : '') }),
        ]),
        el('span', { class: 'badge ' + (c.n_projects >= 5 ? 'ok'
          : c.n_projects >= 3 ? 'thin' : 'no'),
          text: c.n_projects >= 5 ? 'usable' : c.n_projects >= 3 ? 'thin' : 'too thin' }),
      ]));
    });
  }

  let timer = null;
  async function search() {
    const q = qinput.value.trim();
    if (!q) return;
    try {
      const r = await api('/api/match/search?q=' + encodeURIComponent(q)
        + '&unit=' + encodeURIComponent(ln.unit || '') + '&limit=30');
      cands = r.candidates || [];
      paint();
    } catch (e) { toast(String(e.message || e), 'err'); }
  }
  qinput.addEventListener('input', () => {
    clearTimeout(timer); timer = setTimeout(search, 260);
  });

  function close() { host.innerHTML = ''; document.removeEventListener('keydown', onKey); }
  function onKey(e) { if (e.key === 'Escape') close(); }
  document.addEventListener('keydown', onKey);

  const back = el('div', { class: 'modal-back', onclick: e => { if (e.target === back) close(); } },
    el('div', { class: 'modal' }, [
      el('header', {}, [
        el('h3', { text: 'Match: ' + (ln.description || '(blank)') }),
        el('button', { class: 'btn sm close', text: 'Close', onclick: close }),
      ]),
      el('div', { class: 'mbody' }, [
        el('label', { class: 'field' }, [
          el('span', { class: 'lab', text: 'Search the historical item vocabulary' }),
          qinput,
          el('span', { class: 'sub', text:
            'City bid tabs (DFW) and the TxDOT price book. "Family average" pools a '
            + 'whole family \u2014 broader support, less specific item.' }),
        ]),
        list,
      ]),
    ]));
  host.appendChild(back);
  paint();
  qinput.focus();
  qinput.select();
}

/* ====================================================================
   3. ESTIMATE
   ==================================================================== */

async function runEstimate() {
  const n = parseInt($('#in-bidders').value, 10);
  if (!n || n < 1) {
    setView('input');
    $('#in-bidders').classList.add('invalid');
    $('#in-bidders').focus();
    toast('Expected number of bidders is required \u2014 it moves the answer by more '
      + 'than 10 percentage points.', 'err', 8000);
    return;
  }
  if (S.parse && S.parse.mock) {
    toast('These line items are mock OCR output and will not be priced.', 'err', 9000);
    return;
  }

  const items = S.lines.filter(l => l.include).map(l => ({
    row_id: l.row_id,
    description: l.description,
    unit: l.unit,
    quantity: l.quantity,
    match_key: (l.userPicked && l.match) ? l.match.key : '',
    include: true,
  }));

  const payload = {
    items,
    n_bidders: n,
    work_type: $('#in-worktype').value,
    owner: $('#in-owner').value,
    bid_year: parseInt($('#in-bidyear').value, 10) || null,
  };
  S.result = await postJSON('/api/estimate', payload);
  S.resultMeta = payload;
  renderResult();
  enableSteps();
  setView('result');
}

function renderResult() {
  const r = S.result;
  const head = $('#result-head');
  head.innerHTML = '';
  if (!r) return;

  const ds = r.direct_subtotal || {}, pt = r.project_total || {};
  const meta = S.resultMeta || {};
  const spread = (pt.p50 && pt.p10 && pt.p90)
    ? ((pt.p90 - pt.p10) / 2 / pt.p50 * 100) : null;

  head.appendChild(el('div', { class: 'headline' }, [
    el('div', { class: 'bigcard lead' }, [
      el('div', { class: 'cap', text: 'Expected low bid \u2014 project total' }),
      el('div', { class: 'p50', text: money(pt.p50) }),
      el('div', { class: 'range', text: 'P10 ' + money(pt.p10) + '   \u2192   P90 ' + money(pt.p90) }),
      rangeBar(pt),
      el('div', { class: 'foot', text:
        'At ' + meta.n_bidders + ' expected bidders. '
        + (spread !== null ? 'Band is \u00b1' + spread.toFixed(0) + '% around P50. ' : '')
        + 'Includes the percentage-of-cost items below.' }),
    ]),
    el('div', { class: 'bigcard' }, [
      el('div', { class: 'cap', text: 'Direct (unit-priced) work' }),
      el('div', { class: 'p50', text: money(ds.p50) }),
      el('div', { class: 'range', text: 'P10 ' + money(ds.p10) + '   \u2192   P90 ' + money(ds.p90) }),
      el('div', { class: 'foot', text:
        (r.diagnostics || {}).n_priced + ' line(s) priced'
        + ((r.diagnostics || {}).n_unpriced ? ', ' + r.diagnostics.n_unpriced + ' not priced' : '')
        + '. ' + ((r.overhead || {}).lines || 0) + ' lump-sum item(s) grossed up at \u00d7'
        + Number((r.overhead || {}).gross_up || 1).toFixed(4) + '.' }),
    ]),
    el('div', { class: 'bigcard' }, [
      el('div', { class: 'cap', text: 'How the two stages landed' }),
      el('dl', { class: 'kv' }, [
        el('dt', { text: 'Competition discount' }),
        el('dd', { text: '\u00d7' + ((r.discount || {}).factor || 0).toFixed(3)
          + '  (' + (r.discount || {}).used + ' bidders)' }),
        el('dt', { text: 'Measured over' }),
        el('dd', { text: ((r.discount || {}).lettings || 0).toLocaleString('en-US') + ' lettings' }),
        el('dt', { text: 'Project log-\u03c3' }),
        el('dd', { text: ((r.diagnostics || {}).project_sigma || 0).toFixed(3)
          + ((r.diagnostics || {}).sigma_floor_applied ? '  (at floor)' : '') }),
        el('dt', { text: 'Overhead gross-up' }),
        el('dd', { text: '\u00d7' + Number((r.overhead || {}).gross_up || 1).toFixed(4)
          + '  (' + pct(((r.overhead || {}).total_share_median || 0) * 100) + ')' }),
      ]),
      el('div', { class: 'foot', text:
        'Interval floor is set by the industry benchmark: 13.8% median absolute error '
        + 'implies log-\u03c3 \u2248 0.205, so no total claims to be tighter.' }),
    ]),
  ]));

  const warns = $('#result-warnings');
  warns.innerHTML = '';
  (r.warnings || []).forEach(w =>
    warns.appendChild(notice(w.level, null, w.message)));

  const body = $('#result-body');
  body.innerHTML = '';
  (r.lines || []).forEach((l, i) => {
    body.appendChild(resultRow(l, i));
    body.appendChild(detailRow(l));
  });
}

function rangeBar(iv) {
  if (!iv || !iv.p10 || !iv.p90 || !iv.p50) return el('span');
  const span = iv.p90 - iv.p10;
  const at = span > 0 ? ((iv.p50 - iv.p10) / span * 100) : 50;
  return el('div', { class: 'rangebar' }, [
    el('div', { class: 'track' }, el('div', { class: 'mid', style: 'left:' + at.toFixed(1) + '%' })),
    el('div', { class: 'lbl' }, [
      el('span', { text: 'P10' }), el('span', { text: 'P50' }), el('span', { text: 'P90' })]),
  ]);
}

function worstLevel(warnings) {
  if ((warnings || []).some(w => w.level === 'severe')) return 'severe';
  if ((warnings || []).some(w => w.level === 'caution')) return 'caution';
  return null;
}

function resultRow(l, i) {
  const worst = worstLevel(l.warnings);
  const tr = el('tr', { 'data-line': l.row_id, style: 'cursor:pointer' });
  tr.addEventListener('click', e => {
    if (e.target.tagName === 'BUTTON' || e.target.tagName === 'A') return;
    toggleDetail(l.row_id);
  });

  tr.appendChild(el('td', { class: 'dim num', text: String(i + 1) }));

  const descCell = el('td', {}, [
    el('div', {}, [
      el('span', { text: l.description }),
      l.treatment === 'percent_of_cost'
        ? el('span', { class: 'badge pct', style: 'margin-left:7px',
            text: '% of cost' }) : null,
      l.treatment === 'unpriced'
        ? el('span', { class: 'badge no', style: 'margin-left:7px',
            text: 'not priced' }) : null,
      l.price_form === 'scale_dependent' && l.treatment === 'unit_price'
        ? el('span', { class: 'badge thin', style: 'margin-left:7px',
            title: 'Unit price moves strongly with project scale',
            text: 'scale-dependent' }) : null,
      worst ? el('span', { class: 'badge ' + (worst === 'severe' ? 'no' : 'thin'),
        style: 'margin-left:7px',
        text: worst === 'severe' ? '\u2715 unreliable' : '! check' }) : null,
    ]),
    el('div', { class: 'dim', style: 'font-size:11px' , text:
      (l.matched_label || '\u2014') + (l.match_score
        ? '  \u00b7  score ' + l.match_score.toFixed(2) : '') }),
  ]);
  tr.appendChild(descCell);

  tr.appendChild(el('td', { text: l.unit }));
  tr.appendChild(el('td', { class: 'num money', text: qtyFmt(l.quantity) }));

  if (l.treatment === 'percent_of_cost') {
    const p = l.pct_of_construction || {};
    tr.appendChild(el('td', { class: 'num dim', colspan: 3, style: 'text-align:center',
      text: pct(p.p10) + ' \u2013 ' + pct(p.p50) + ' \u2013 ' + pct(p.p90)
            + ' of construction cost' }));
  } else {
    const u = l.unit_price || {};
    tr.appendChild(el('td', { class: 'num money dim', text: rate(u.p10) }));
    tr.appendChild(el('td', { class: 'num money p50', text: rate(u.p50) }));
    tr.appendChild(el('td', { class: 'num money dim', text: rate(u.p90) }));
  }

  const t = l.total || {};
  tr.appendChild(el('td', { class: 'num money dim', text: money(t.p10) }));
  tr.appendChild(el('td', { class: 'num money p50', text: money(t.p50) }));
  tr.appendChild(el('td', { class: 'num money dim', text: money(t.p90) }));

  const ev = l.evidence || {};
  const u = String(ev.usable || '').toLowerCase();
  tr.appendChild(el('td', {}, [
    el('span', { class: 'badge ' + (u === 'yes' ? 'ok' : u === 'thin' ? 'thin' : 'no'),
      text: u === 'yes' ? (ev.n_projects + ' projects')
        : u === 'thin' ? ('thin \u00b7 ' + ev.n_projects)
        : (ev.n_projects ? 'too thin \u00b7 ' + ev.n_projects : '\u2014') }),
    el('div', { class: 'dim', style: 'font-size:10.5px',
      text: ev.source || '' }),
  ]));
  return tr;
}

function detailRow(l) {
  return el('tr', { class: 'detail', 'data-detail': l.row_id, style: 'display:none' },
    el('td', { colspan: 11 }, el('div', { class: 'detailbox',
      'data-box': l.row_id }, el('span', { class: 'dim', text: 'loading\u2026' }))));
}

async function toggleDetail(rowId) {
  const tr = $('tr[data-detail="' + cssEsc(rowId) + '"]');
  if (!tr) return;
  const open = tr.style.display !== 'none';
  tr.style.display = open ? 'none' : 'table-row';
  if (open) return;
  const box = $('.detailbox[data-box="' + cssEsc(rowId) + '"]');
  if (box.dataset.loaded) return;
  const line = (S.result.lines || []).find(x => x.row_id === rowId);
  if (!line) return;
  box.innerHTML = '';
  box.appendChild(buildDetail(line));
  box.dataset.loaded = '1';
  // Comparables are fetched lazily -- 547k rows is not something to pre-slice
  // for every row of every estimate.
  const host = $('.cmp-host', box);
  if (host && line.comparables_key) {
    try {
      const owner = (S.resultMeta || {}).owner || '';
      const c = await api('/api/comparables?key=' + encodeURIComponent(line.comparables_key)
        + '&unit=' + encodeURIComponent(line.unit || '')
        + '&owner=' + encodeURIComponent(owner)
        + (line.quantity > 0 ? '&quantity=' + encodeURIComponent(line.quantity) : '')
        + '&limit=25');
      host.innerHTML = '';
      host.appendChild(comparablesTable(c, line));
    } catch (e) {
      host.innerHTML = '';
      host.appendChild(notice('caution', 'Could not load comparables', String(e.message || e)));
    }
  }
}

function cssEsc(s) { return String(s).replace(/["\\]/g, '\\$&'); }

function buildDetail(l) {
  const ev = l.evidence || {};
  const qr = ev.quantity_route || {};
  const sp = ev.sigma_parts || {};

  const how = [];
  if (l.treatment === 'unit_price') {
    how.push(['Market median (at this qty)', rate(l.market_median_unit) + ' / ' + l.unit]);
    if (l.qty_adjust_factor !== null && l.qty_adjust_factor !== undefined) {
      how.push(['Quantity adjustment', '\u00d7' + Number(l.qty_adjust_factor).toFixed(3)
        + '  (' + (qr.route === 'observed_band'
          ? 'observed ' + qr.band + ' band for this item'
          : qr.route === 'family_band'
            ? 'observed ' + qr.band + ' vs ' + qr.band_ref + ' band across '
              + (qr.band_rows || 0) + ' item(s) in this family'
          : qr.route === 'elasticity'
            ? 'elasticity ' + (l.elasticity_used !== null && l.elasticity_used !== undefined
                ? Number(l.elasticity_used).toFixed(3) : '?')
              + ' vs ref qty ' + qtyFmt(qr.qty_ref)
              + (qr.qty_ref_source === 'panel_observed_median'
                  ? ' (observed median of ' + (qr.qty_ref_n_obs || 0).toLocaleString('en-US')
                    + ' ' + (qr.qty_ref_family || '') + ' lines'
                    + (qr.qty_ref_replaced !== undefined && qr.qty_ref_replaced !== null
                        ? ', replacing a 1‑2 project value of '
                          + qtyFmt(qr.qty_ref_replaced) : '')
                    + ')'
                  : '')
            : qr.route === 'no_reference_quantity'
              ? 'no reference quantity — NOT adjusted for your quantity'
              : qr.route || 'none') + ')']);
    }
    const esc = ev.escalation || {};
    if (esc.applied) {
      how.push(['Escalation to bid year',
        '\u00d7' + Number(l.escalation_factor || 1).toFixed(4)
        + '  (' + Number(esc.vintage_year).toFixed(1) + ' \u2192 ' + esc.bid_year
        + ', ' + (esc.basket || '?') + ', index '
        + Number(esc.index_from).toFixed(1) + '\u2192' + Number(esc.index_to).toFixed(1)
        + (esc.scope ? ', ' + esc.scope + ' scope' : '')
        + (esc.years_uncovered >= 1
            ? '; ' + Math.round(esc.years_uncovered) + 'y NOT covered' : '')
        + ')']);
    } else if (esc.reason === 'no_bid_year') {
      how.push(['Escalation to bid year',
        'none \u2014 no bid year entered, so this rate is at its sample midpoint'
        + (esc.vintage_year ? ' (' + Number(esc.vintage_year).toFixed(1) + ')' : '')]);
    }
    how.push(['Competition discount', '\u00d7' + Number(l.discount_applied || 0).toFixed(3)]);
    how.push(['= Expected low unit price', rate((l.unit_price || {}).p50) + ' / ' + l.unit]);
    how.push(['Line log-\u03c3', Number(l.sigma || 0).toFixed(3)]);
  } else if (l.treatment === 'percent_of_cost') {
    const sh = ev.share || {};
    how.push(['Share of construction cost', pct((l.pct_of_construction || {}).p50)
      + '   (p25 ' + pct((sh.p25 || 0) * 100) + ', p75 ' + pct((sh.p75 || 0) * 100) + ')']);
    how.push(['Measured over', (sh.n_bids || 0).toLocaleString('en-US') + ' bids']);
    how.push(['Applied to project total', money((l.total || {}).p50)]);
  }

  const sigmaRows = Object.entries(sp).filter(([, v]) => v > 0);

  return el('div', { class: 'detailcols' }, [
    el('div', {}, [
      el('h5', { text: 'Historical comparables' }),
      el('div', { class: 'cmp-host' },
        el('span', { class: 'dim', html: '<span class="spinner"></span> loading\u2026' })),
    ]),
    el('div', {}, [
      el('h5', { text: 'How this number was built' }),
      el('dl', { class: 'kv' }, how.flatMap(([k, v]) =>
        [el('dt', { text: k }), el('dd', { text: v })])),

      sigmaRows.length ? el('div', {}, [
        el('h5', { style: 'margin-top:11px', text: 'Where the uncertainty comes from' }),
        el('dl', { class: 'kv' }, sigmaRows.flatMap(([k, v]) =>
          [el('dt', { text: k.replace(/_/g, ' ') }), el('dd', { text: Number(v).toFixed(3) })])),
      ]) : null,

      el('h5', { style: 'margin-top:11px', text: 'Source' }),
      el('div', { class: 'muted-note', text: ev.source_label || ev.source || '\u2014' }),
      ev.median_p25 ? el('div', { class: 'muted-note', text:
        'Reference spread: p25 ' + rate(ev.median_p25) + ' \u00b7 p75 ' + rate(ev.median_p75) }) : null,

      (l.warnings || []).length ? el('div', { style: 'margin-top:11px' },
        [el('h5', { text: 'Flags' })].concat(
          l.warnings.map(w => notice(w.level, null, w.message)))) : null,
    ]),
  ]);
}

function comparablesTable(c, line) {
  const rows = c.rows || [];
  if (!rows.length) {
    return notice('caution', 'No comparables found',
      'Nothing in the panel matches family "' + ((c.filters || {}).item_family || '?')
      + '" at unit ' + ((c.filters || {}).unit || '?')
      + ' among competitive construction lettings. The price above rests on the '
      + 'reference table only \u2014 treat it with caution.');
  }
  const f = c.filters || {};
  const lump = line && line.treatment === 'percent_of_cost';
  return el('div', {}, [
    el('div', { class: 'muted-note', style: 'margin-bottom:6px', text:
      'Showing ' + rows.length + ' of ' + c.n_total.toLocaleString('en-US')
      + ' competitive construction observations for ' + (f.item_family || '?')
      + ' / ' + (f.unit || '?') + '. Ranked by ' + (c.ranked_by || 'relevance')
      + (f.owner ? ', preferring ' + f.owner : '') + '. '
      + (c.n_local ? c.n_local.toLocaleString('en-US')
          + ' of them are DFW city bid tabs (marked DFW below); the rest are TxDOT.'
        : 'None are DFW city bid tabs — this item is supported by TxDOT '
          + 'lettings only.') }),
    lump ? el('div', { class: 'muted-note', style: 'margin-bottom:6px', text:
      'This is a lump-sum item, so the "Unit $" column below is each bidder’s '
      + 'whole price for it on that job — not a rate. That is exactly why it is '
      + 'priced here as a share of construction cost instead.' }) : null,
    el('table', { class: 'mini' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: 'Date' }), el('th', { text: 'Owner' }),
        el('th', { text: 'Item' }),
        el('th', { class: 'num', text: 'Qty' }),
        el('th', { class: 'num', text: lump ? 'Lump $' : 'Unit $' }),
        el('th', { text: 'Bidder' }),
        el('th', { class: 'num', text: 'Bidders' }),
      ])),
      el('tbody', {}, rows.map(r => el('tr', { class: r.is_low_bidder ? 'low' : '' }, [
        el('td', { class: 'nowrap', text: (r.bid_date || '').slice(0, 10) }),
        el('td', {}, [
          r.scope === 'local'
            ? el('span', { class: 'badge info', style: 'margin-right:5px',
                title: 'DFW city bid tab', text: 'DFW' }) : null,
          document.createTextNode(r.owner || ''),
        ]),
        el('td', { title: r.project, text: (r.description || '').slice(0, 54) }),
        el('td', { class: 'num', text: qtyFmt(r.quantity) }),
        el('td', { class: 'num', text: rate(r.unit_price) }),
        el('td', {}, [
          // An awarded contract amount is already the outcome of competition,
          // so it is not a draw from the same distribution as a bid.
          r.price_basis && r.price_basis !== 'all_bids'
            ? el('span', { class: 'badge warn', style: 'margin-right:5px',
                title: 'Awarded contract amount, not one bidder’s bid',
                text: 'contract' }) : null,
          document.createTextNode((r.bidder || '').slice(0, 26)),
        ]),
        el('td', { class: 'num', text: r.n_bidders || '' }),
      ]))),
    ]),
    el('div', { class: 'muted-note', style: 'margin-top:5px', text:
      'Green rows are the low bidder on that letting. Only competitive lettings '
      + '(3 or more bidders) are shown, because only those went into the reference.' }),
  ]);
}

/* ------------------------------------------------------------------ export */

function initExport() {
  $('#btn-export').addEventListener('click', async () => {
    if (!S.result) return;
    const r = await fetch('/api/export/csv', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ result: S.result, meta: S.resultMeta || {} }),
    });
    if (!r.ok) { toast('Export failed: ' + r.status, 'err'); return; }
    const blob = await r.blob();
    const a = el('a', { href: URL.createObjectURL(blob),
      download: 'bid_estimate.csv' });
    document.body.appendChild(a); a.click(); a.remove();
    toast('Estimate exported.', 'ok');
  });
}

/* ====================================================================
   i. DATA & METHOD
   ==================================================================== */

function renderData() {
  const st = S.status;
  const host = $('#data-tables');
  host.innerHTML = '';
  if (!st) { host.textContent = 'loading\u2026'; return; }

  const rows = (st.tables || []).map(t => el('tr', {}, [
    el('td', {}, [
      el('div', { text: t.label || t.name }),
      el('div', { class: 'dim', style: 'font-size:10.5px', text: t.file }),
    ]),
    el('td', { class: 'num', text: t.rows !== null && t.rows !== undefined
      ? t.rows.toLocaleString('en-US') : '\u2014' }),
    el('td', {}, t.error
      ? el('span', { class: 'badge no', text: 'error' })
      : el('span', { class: 'badge ok', text: 'loaded' })),
    el('td', { class: 'dim', style: 'font-size:10.5px',
      text: t.error ? t.error.slice(0, 150)
        : (t.unexpected_columns || []).length
          ? '+' + t.unexpected_columns.length + ' new column(s): '
            + t.unexpected_columns.join(', ')
          : '' }),
  ]));
  rows.unshift(el('tr', {}, [
    el('td', {}, [el('div', { text: 'Observation panel' }),
      el('div', { class: 'dim', style: 'font-size:10.5px', text: st.panel.file })]),
    el('td', { class: 'num', text: (st.panel.rows || 0).toLocaleString('en-US') }),
    el('td', {}, st.panel.error ? el('span', { class: 'badge no', text: 'error' })
      : el('span', { class: 'badge ok', text: 'cached' })),
    el('td', { class: 'dim', style: 'font-size:10.5px',
      text: st.panel.error ? st.panel.error
        : st.panel.has_quality_flags
          ? ((st.panel.quality_filtered || 0).toLocaleString('en-US')
             + ' misread row(s) dropped (ext_mismatch / unit_suspect)')
          : 'no quality flags in this build of the panel' }),
  ]));

  host.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, [
      el('th', { text: 'Table' }), el('th', { class: 'num', text: 'Rows' }),
      el('th', { text: 'State' }), el('th', { text: 'Notes' })])),
    el('tbody', {}, rows),
  ]));
  host.appendChild(el('div', { class: 'muted-note', style: 'margin-top:9px', text:
    'Read from ' + st.clean_dir + ', by column name only, read-only. Extra columns '
    + 'added by the ETL are tolerated; a missing required column fails loudly at '
    + 'startup rather than silently mispricing.' }));

  // --- method
  const m = $('#data-method');
  m.innerHTML = '';
  const cal = st.calibration || {};
  m.appendChild(el('div', {}, [
    el('p', { style: 'margin:0 0 9px', html:
      '<b>expected low unit price = market median \u00d7 f(bidder count)</b>, '
      + 'with the median first moved to the line\u2019s own quantity.' }),
    el('dl', { class: 'kv' }, [
      el('dt', { text: 'Stage 1' }),
      el('dd', { text: 'median for the matched item, adjusted by observed quantity band '
        + 'where one exists, else by fitted elasticity (default '
        + cal.default_qty_elasticity + ', capped at \u00d7' + cal.qty_factor_clamp + ')' }),
      el('dt', { text: 'Stage 2' }),
      el('dd', { text: 'low-over-median discount at the expected bidder count, '
        + 'from competition_discount_pooled.csv' }),
      el('dt', { text: 'Line interval' }),
      el('dd', { text: 'lognormal; \u03c3 from the reference IQR (shrunk \u00d7'
        + cal.ref_spread_shrink + '), the discount\u2019s own \u03c3, evidence depth, '
        + 'and any quantity extrapolation' }),
      el('dt', { text: 'Project interval' }),
      el('dd', { text: 'lines aggregated with correlation \u03c1='
        + cal.line_error_correlation + ', floored at \u03c3='
        + cal.benchmark_project_sigma + ' (the 13.8% industry benchmark)' }),
      el('dt', { text: 'Lump-sum items' }),
      el('dd', { text: 'share of construction cost computed from this panel, '
        + 'then T = D / (1 \u2212 \u03a3 shares)' }),
    ]),
    el('div', { class: 'hr' }),
    el('div', { class: 'muted-note', html:
      'The shrink factor, the correlation and the quantity cap are <b>assumptions</b>, '
      + 'not fitted values \u2014 they are collected in the CALIBRATION block at the top '
      + 'of <code class="inline">ui/pricing.py</code> for the modelling work to replace. '
      + 'The elasticity, the discount curve, the medians and the lump-sum shares are '
      + 'all measured from the data.' }),
  ]));

  // --- discount curve
  const d = $('#data-discount');
  d.innerHTML = '';
  const curve = S.options.discount_curve || [];
  d.appendChild(el('table', { class: 'mini' }, [
    el('thead', {}, el('tr', {}, [
      el('th', { class: 'num', text: 'Bidders' }),
      el('th', { class: 'num', text: 'Low / median' }),
      el('th', { class: 'num', text: 'Lettings' }),
      el('th', { text: '' })])),
    el('tbody', {}, curve.map(r => el('tr', {}, [
      el('td', { class: 'num', text: r.n_bidders }),
      el('td', { class: 'num', text: r.factor === null ? '\u2014' : r.factor.toFixed(3) }),
      el('td', { class: 'num dim', text: (r.lettings || 0).toLocaleString('en-US') }),
      el('td', {}, el('div', { style:
        'height:8px;border-radius:2px;background:#4f9cf0;width:'
        + Math.max(2, ((r.factor || 0) - 0.78) / 0.24 * 100).toFixed(0) + '%' })),
    ]))),
  ]));
  d.appendChild(el('div', { class: 'muted-note', style: 'margin-top:7px', text:
    'The low bid is the minimum of N bids, so more bidders means a lower expected '
    + 'minimum. This is why bidder count is a required input and not a setting.' }));
}

async function uploadBidtab(file) {
  const out = $('#bidtab-result');
  out.innerHTML = '';
  out.appendChild(notice('info', null, 'Parsing ' + file.name + ' with src/parse_bidtabs.py\u2026'));
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await api('/api/bidtab/parse', { method: 'POST', body: fd });
    out.innerHTML = '';
    if (!r.ok) { out.appendChild(notice('severe', 'Parse failed', r.error)); return; }
    out.appendChild(notice('ok', r.file + ': ' + r.n_rows.toLocaleString('en-US')
      + ' line-item rows from ' + r.n_bidders + ' bidder(s)', r.note));
    out.appendChild(el('dl', { class: 'kv', style: 'margin-bottom:9px' }, [
      el('dt', { text: "Engineer's estimate rows" }),
      el('dd', { text: r.engineer_estimate_rows }),
      el('dt', { text: 'Units seen' }),
      el('dd', { text: Object.entries(r.units || {}).map(([k, v]) => k + ' (' + v + ')').join(', ') }),
      el('dt', { text: 'Parse time' }), el('dd', { text: r.elapsed_s + ' s' }),
      el('dt', { text: 'Staged at' }),
      el('dd', { style: 'overflow-wrap:anywhere', text: r.staged_path || '\u2014' }),
    ]));
    const f = el('details', { class: 'fold' }, [
      el('summary', { text: 'Bidders found and a sample of the parsed rows' }),
      el('div', { style: 'padding:7px 0' }, [
        el('div', { class: 'muted-note', text: (r.bidders || [])
          .map(b => b.name + ' (' + b.rows + ')').join('  \u00b7  ') }),
        el('table', { class: 'mini', style: 'margin-top:7px' }, [
          el('thead', {}, el('tr', {}, ['Item', 'Description', 'Unit', 'Qty', 'Bidder', 'Unit $']
            .map(h => el('th', { class: (h === 'Qty' || h === 'Unit $') ? 'num' : '', text: h })))),
          el('tbody', {}, (r.sample || []).map(s => el('tr', {}, [
            el('td', { text: s.item_no }),
            el('td', { text: String(s.description || '').slice(0, 60) }),
            el('td', { text: s.unit }),
            el('td', { class: 'num', text: qtyFmt(s.quantity) }),
            el('td', { text: String(s.bidder || '').slice(0, 24) }),
            el('td', { class: 'num', text: rate(s.unit_price) }),
          ]))),
        ]),
      ]),
    ]);
    out.appendChild(f);
  } catch (e) {
    out.innerHTML = '';
    out.appendChild(notice('severe', 'Parse failed', String(e.message || e)));
  }
}

/* ------------------------------------------------------------- status bar */

function renderStatusBar() {
  const st = S.status;
  const bar = $('#statusbar');
  bar.innerHTML = '';
  if (!st) {
    bar.appendChild(el('span', { html: '<span class="spinner"></span> starting\u2026' }));
    return;
  }
  const errs = (st.tables || []).filter(t => t.error).length + (st.panel.error ? 1 : 0);
  bar.appendChild(el('span', {}, [
    el('span', { class: 'dot ' + (errs ? 'bad' : 'ok') }),
    document.createTextNode(errs ? errs + ' data file(s) failed to load'
      : 'data loaded \u00b7 ' + (st.panel.rows || 0).toLocaleString('en-US')
        + ' price observations'),
  ]));
  bar.appendChild(el('span', {}, [
    el('span', { class: 'dot ' + (st.upstream.parser_available ? 'ok' : 'bad') }),
    document.createTextNode('bid-tab parser ' + (st.upstream.parser_available
      ? 'linked' : 'unavailable')),
  ]));
  bar.appendChild(el('span', {}, [
    el('span', { class: 'dot ' + (st.ocr.real ? 'ok' : (st.ocr.mock ? 'warn' : 'bad')) }),
    document.createTextNode('OCR ' + (st.ocr.real ? 'available'
      : st.ocr.mock ? 'MOCK only' : 'not installed \u2014 images unsupported')),
  ]));
  // An overlay directory shadows data/clean per file. It exists to try a
  // candidate ETL output, or a consumer written ahead of its file, so a
  // number produced under one must never pass as a real estimate.
  if (st.clean_overlay) {
    bar.appendChild(el('span', { title: st.clean_overlay }, [
      el('span', { class: 'dot warn' }),
      document.createTextNode('DATA OVERLAY ACTIVE - some tables are not the '
        + 'real data/clean'),
    ]));
  }
  bar.appendChild(el('span', { class: 'dim', style: 'margin-left:auto',
    text: 'local only \u00b7 nothing leaves this machine' }));
}

/* ------------------------------------------------------------------- boot */

async function boot() {
  initInput();
  initReview();
  initExport();
  $$('#steps button').forEach(b =>
    b.addEventListener('click', () => { if (!b.disabled) setView(b.dataset.view); }));

  renderStatusBar();
  try {
    const [st, op] = await Promise.all([api('/api/status'), api('/api/options')]);
    S.status = st;
    S.options = op;
    renderStatusBar();

    const wt = $('#in-worktype');
    (op.work_types || []).forEach(w => wt.appendChild(el('option', { value: w, text: w })));
    // The bid-year field only appears once an index exists to escalate with;
    // otherwise it would take an input the engine has no way to honour.
    const px = op.price_index || {};
    const yf = $('#field-bidyear');
    if (yf && px.available) {
      yf.hidden = false;
      const sub = $('#bidyear-sub');
      if (sub) {
        sub.textContent = 'Reference prices sit at the midpoint of their own '
          + 'sample, not at your letting. Index covers ' + px.year_min + '–'
          + px.year_max
          + (px.scope && px.scope.length ? ' at ' + px.scope.join('/') + ' scope' : '')
          + '. Leave blank and nothing is escalated.';
      }
    }
    const ow = $('#in-owner');
    (op.owners || []).forEach(o => ow.appendChild(el('option', { value: o, text: o })));

    const bad = (st.tables || []).filter(t => t.error);
    if (bad.length) {
      toast(bad.length + ' reference file(s) failed to load \u2014 see Data & method.',
        'err', 12000);
    }
    if (!st.upstream.parser_available) {
      toast('src/parse_bidtabs.py did not import; matching is running on degraded '
        + 'fallbacks.', 'err', 14000);
    }
    onBidders();
  } catch (e) {
    toast('Backend not reachable: ' + (e.message || e), 'err', 20000);
  }
  enableSteps();
}

document.addEventListener('DOMContentLoaded', boot);
