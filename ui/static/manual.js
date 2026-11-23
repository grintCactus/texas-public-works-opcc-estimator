'use strict';

// Executed in the head before styles: follow the estimator's saved theme.
(function initManualTheme() {
  const key = 'bid-estimator-theme';
  const query = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function apply() {
    let mode = 'system';
    try {
      const stored = window.localStorage.getItem(key);
      if (stored === 'light' || stored === 'dark') mode = stored;
    } catch (e) { /* blocked storage: use the system preference */ }
    const dark = mode === 'dark' || (mode === 'system' && query && query.matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme-mode', mode);
  }
  apply();
  if (query) {
    if (query.addEventListener) query.addEventListener('change', apply);
    else if (query.addListener) query.addListener(apply);
  }
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply();
  });
})();

function manualNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function manualPercent(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? (100 * value).toFixed(1) + '%' : 'Not available';
}

function manualUnderAt(curve, multiplier) {
  if (!Array.isArray(curve)) return null;
  const points = curve.filter(point => Array.isArray(point) && point.length >= 2
    && typeof point[0] === 'number' && Number.isFinite(point[0])
    && typeof point[1] === 'number' && Number.isFinite(point[1])
    && point[1] >= 0 && point[1] <= 1).slice().sort((a, b) => a[0] - b[0]);
  if (!points.length || multiplier < points[0][0] || multiplier > points[points.length - 1][0]) return null;
  for (let i = 0; i < points.length; i++) {
    if (points[i][0] === multiplier) return points[i][1];
    if (points[i][0] > multiplier && i > 0) {
      const [x0, y0] = points[i - 1], [x1, y1] = points[i];
      return y0 + (y1 - y0) * (multiplier - x0) / (x1 - x0);
    }
  }
  return null;
}

function manualAccuracyCard(market, data) {
  const card = manualNode('div', null, 'manual-market');
  card.appendChild(manualNode('h3', market));
  if (!data || typeof data !== 'object') {
    card.appendChild(manualNode('p', 'Historical results for this market are not available from the current estimator.', 'manual-muted'));
    return card;
  }
  if (data.label) card.appendChild(manualNode('p', data.label, 'manual-muted'));
  const stats = manualNode('dl', null, 'manual-definitions');
  const count = typeof data.lettings === 'number' && Number.isFinite(data.lettings)
    ? data.lettings.toLocaleString('en-US') : 'Not available';
  [
    ['Earlier lettings scored', count],
    ['P50 median error', manualPercent(data.p50_median_error)],
    ['P80 baseline under rate', manualPercent(data.p80_under)],
    ['P80 baseline median overshoot', manualPercent(data.median_overshoot)],
  ].forEach(([label, value]) => {
    stats.appendChild(manualNode('dt', label));
    stats.appendChild(manualNode('dd', value));
  });
  card.appendChild(stats);
  const table = manualNode('table');
  table.appendChild(manualNode('caption', 'How often each amount fell below the low bid'));
  const head = manualNode('thead'), header = manualNode('tr');
  ['Amount', 'Under the low bid'].forEach(label => {
    const cell = manualNode('th', label);
    cell.setAttribute('scope', 'col');
    header.appendChild(cell);
  });
  head.appendChild(header);
  table.appendChild(head);
  const body = manualNode('tbody');
  [['P50', 1], ['P50 + 10%', 1.10], ['P50 + 15%', 1.15], ['P50 + 20%', 1.20]].forEach(([label, multiplier]) => {
    const row = manualNode('tr');
    row.appendChild(manualNode('td', label));
    row.appendChild(manualNode('td', manualPercent(manualUnderAt(data.under_curve, multiplier))));
    body.appendChild(row);
  });
  table.appendChild(body);
  card.appendChild(table);
  card.appendChild(manualNode('p', 'These are percentages above the model P50. They do not validate the recent-bid worksheet prices or simulate your edited OPCC and scope contingency.', 'manual-muted'));
  const date = String(data.as_of || '').match(/^\d{4}-\d{2}-\d{2}/);
  if (date) card.appendChild(manualNode('p', 'Results as of ' + date[0] + '.', 'manual-muted'));
  return card;
}

async function loadManualAccuracy() {
  const host = document.getElementById('manual-accuracy');
  if (!host) return;
  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = controller ? window.setTimeout(() => controller.abort(), 15000) : null;
  try {
    const response = await fetch('/api/options', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (!response.ok) throw new Error('Historical results unavailable');
    const options = await response.json();
    const accuracy = options && options.market_accuracy;
    if (!accuracy || typeof accuracy !== 'object') throw new Error('Historical results unavailable');
    host.replaceChildren(
      manualAccuracyCard('City / County', accuracy.municipal),
      manualAccuracyCard('TxDOT', accuracy.txdot)
    );
  } catch (e) {
    host.replaceChildren(manualNode('p', 'Historical results could not be loaded. The rest of this manual is still available. Refresh this page after the estimator has finished loading to see the current figures.', 'manual-callout'));
  } finally {
    if (timer !== null) window.clearTimeout(timer);
    host.setAttribute('aria-busy', 'false');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const charts = document.getElementById('p50p70-charts');
  if (charts) charts.innerHTML = p50p70Picture();
  loadManualAccuracy();
});

// Textbook illustration retained from the estimator; it is not fitted to a job.
function p50p70Picture() {
  const s = 0.36, x0 = 0.45, x1 = 2.15, W = 340, H = 200, L = 34, R = 12, T = 26, B = 36;
  const p70 = Math.exp(0.5244005127 * s);
  const pdf = x => Math.exp(-Math.pow(Math.log(x), 2) / (2 * s * s)) / (x * s * Math.sqrt(2 * Math.PI));
  const erf = z => {           // Abramowitz and Stegun 7.1.26
    const t = 1 / (1 + 0.3275911 * Math.abs(z));
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
      + 0.254829592) * t * Math.exp(-z * z);
    return z >= 0 ? y : -y;
  };
  const cdf = x => 0.5 * (1 + erf(Math.log(x) / (s * Math.SQRT2)));
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - R);
  const ymax = 1.25;
  const Yd = v => H - B - v / ymax * (H - B - T);
  const Yc = v => H - B - v * (H - B - T);
  const f = n => n.toFixed(1);
  const pts = (fn, a, b, Y) => {
    const out = [];
    for (let k = 0; k <= 90; k++) { const x = a + (b - a) * k / 90; out.push(f(X(x)) + ',' + f(Y(fn(x)))); }
    return out;
  };
  const area = (a, b, cls) => '<polygon class="' + cls + '" points="' + f(X(a)) + ',' + f(Yd(0)) + ' '
    + pts(pdf, a, b, Yd).join(' ') + ' ' + f(X(b)) + ',' + f(Yd(0)) + '"/>';
  const vline = (x, y, cls) => '<line class="' + cls + '" x1="' + f(X(x)) + '" y1="' + f(y) + '" x2="'
    + f(X(x)) + '" y2="' + f(H - B) + '"/>';
  const txt = (x, y, t, cls, anchor) => '<text class="' + (cls || '') + '" x="' + f(x) + '" y="' + f(y)
    + '" text-anchor="' + (anchor || 'middle') + '">' + t + '</text>';
  const axis = '<line class="pp-axis" x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - R) + '" y2="' + (H - B) + '"/>';
  // left: how likely each low bid is
  const left = '<svg class="pp" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="'
    + 'A curve of how likely each low bid is. The P50 line splits the area in half; 70% of the area is left of the P70 line.">'
    + area(x0, 1, 'pp-r50') + area(1, p70, 'pp-r30') + area(p70, x1, 'pp-r20')
    + '<polyline class="pp-curve" points="' + pts(pdf, x0, x1, Yd).join(' ') + '"/>'
    + vline(1, T - 6, 'pp-l50') + vline(p70, T - 6, 'pp-l80') + axis
    + txt(X(1), T - 10, 'P50', 'pp-t50') + txt(X(p70), T - 10, 'P70', 'pp-t80')
    + txt(X(0.78), Yd(0.35), '50%', 'pp-big') + txt(X((1 + p70) / 2), Yd(0.35), '20%', 'pp-big')
    + txt(X(1.62), Yd(0.04), '30%', 'pp-big')
    + txt(L, H - B + 16, 'lower', '', 'start') + txt(W - R, H - B + 16, 'higher', '', 'end')
    + txt((L + W - R) / 2, H - 6, 'the low bid', '')
    + txt(L, 14, 'how likely', '', 'start') + '</svg>';
  // right: the running total
  const c50 = 0.5, c70 = 0.7;
  const hguide = (v, x, cls) => '<polyline class="' + cls + '" points="' + L + ',' + f(Yc(v)) + ' '
    + f(X(x)) + ',' + f(Yc(v)) + ' ' + f(X(x)) + ',' + f(H - B) + '"/>';
  const right = '<svg class="pp" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="'
    + 'The running total: the share of jobs with the low bid at or under each amount reaches 50% at P50 and 70% at P70.">'
    + '<line class="pp-axis" x1="' + L + '" y1="' + T + '" x2="' + L + '" y2="' + (H - B) + '"/>' + axis
    + hguide(c50, 1, 'pp-g50') + hguide(c70, p70, 'pp-g80')
    + '<polyline class="pp-curve" points="' + pts(cdf, x0, x1, Yc).join(' ') + '"/>'
    + txt(L - 4, Yc(1) + 4, '100%', '', 'end') + txt(L - 4, Yc(c70) + 4, '70%', 'pp-t80', 'end')
    + txt(L - 4, Yc(c50) + 4, '50%', 'pp-t50', 'end') + txt(L - 4, Yc(0) + 4, '0%', '', 'end')
    + txt(X(1), H - B + 16, 'P50', 'pp-t50') + txt(X(p70), H - B + 16, 'P70', 'pp-t80')
    + txt((L + W - R) / 2, H - 6, 'the low bid', '')
    + txt(L, 14, 'share of jobs at or under it', '', 'start') + '</svg>';
  return left + right;
}
