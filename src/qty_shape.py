"""Family-level quantity shape: how unit price moves with quantity, by family.

WHY A FAMILY TABLE AT ALL.  The per-item table (price_by_quantity_band.csv) is
the honest measurement, but it only covers items observed in three or more
projects in two or more bands, which is a small slice.  Everything else falls
back to a FITTED elasticity -- an extrapolation.  A family-level shape lets a
line item borrow the measured curve of its siblings instead, which is weaker
evidence than its own curve but stronger than a fitted slope.

WHY NOT A MEDIAN PRICE PER (family, band).  Because that is the composition
error, not a measurement.  `MOBILIZATION` and `MOBILIZATION (CALLOUT)` both sit
in mobilization/EA at opposite ends of the quantity range, so a family-band
median compares different work at each band and reports item heterogeneity as a
quantity effect.  Only the SHAPE transfers between siblings, never the level --
dividing one item's price by a sibling's median is an operation on two different
items and means nothing.

HOW THE SHAPE IS MEASURED.  Two-way fixed effects on log price:

    log p(item i, band j) = alpha_i + beta_j + e

alpha_i absorbs each item's own price level, so beta_j is what is left: the
common movement across bands.  beta is then exponentiated and anchored at 1.0
on the best-supported band, giving a multiplier a caller applies as
  shape[band of this quantity] / shape[band of the reference quantity].

This is the same reasoning as the matched-pair chained price index -- hold the
item fixed, measure the change -- generalised so that an item skipping a band
still contributes.  Adjacent-band chaining would have discarded it.

CONNECTIVITY IS CHECKED, NOT ASSUMED.  Two-way fixed effects only identify beta
within a CONNECTED component of the item-band graph.  If a family's small-band
items and large-band items share no item that spans both, the two groups of beta
are separately normalised and comparing across them is meaningless -- the figure
would still look like a number.  Only the largest connected component is
emitted, and `bands_in_component` records how much of the band range that is.
"""

import numpy as np

# Must match the bands used for the per-item table in build_master.py.
BANDS = [(0, 10), (10, 50), (50, 200), (200, 1000), (1000, 5000),
         (5000, 25000), (25000, np.inf)]
BAND_LABELS = ["0-10", "10-50", "50-200", "200-1000", "1000-5000",
               "5000-25000", "25000+"]
BAND_INDEX = {b: i for i, b in enumerate(BAND_LABELS)}

MIN_ITEMS = 3          # items spanning >= 2 bands needed for a family
MIN_SPAN_ITEMS = 2     # items that must cover a band before its beta is emitted
# A SHAPE MUST BEAT ITS OWN NOISE.  Every gate above asks whether there is
# enough data to IDENTIFY beta; none asks whether the beta that came out is
# distinguishable from no quantity effect at all.  A family whose fitted span
# (largest emitted multiplier over smallest, in log) is smaller than the
# residual spread it was fitted through has measured nothing, and publishing it
# is worse than publishing nothing: the estimator applies exactly 1.0 when a
# shape is absent, so an unidentified shape replaces a harmless 1.0 with a real
# multiplier drawn from noise.  Refused for the same reason the disconnected
# component is refused -- the figure would still look like a number.
#
# MEASURED on the live corpus before this was added (80 (family, unit) fits
# with the statewide shape arm on): the span/sigma quartiles are 2.85 / 5.38 /
# 9.24, and only six fits sit below 1.0.  Five of those six have spans of
# 1.8-8.4% (demo_structure|LF .026, traffic_control|HR .018,
# testing_inspection|EA .038, demo_structure|EA .050, storm_pipe|CY .081 in
# log), i.e. they were already reporting "no quantity effect" and dropping them
# changes an estimate by at most 8%.  The sixth, rock_anchor|SF, is the reason
# the gate is here: span .300 against sigma .429, and its three items disagree
# about the SIGN of the effect (one item 67.5 -> 22.0 across a band pair while
# another goes 44.3 -> 58.6), so it would have applied a 26% cut on noise.
# (Measured later: both of those pairs were cross-REGION -- the 5000-25000
# cell held DFW lettings only, the 25000+ cell statewide lettings only -- so
# the "noise" was the regional price gap leaking into beta through a shared
# item effect.  build_master.py now gives each region its own item effect;
# rock_anchor|SF then has 2 multi-band items and never reaches this gate.
# The gate still refuses four DFW-only fits: demo_structure|EA and |LF,
# storm_pipe|CY, testing_inspection|EA.)
MIN_SPAN_OVER_SIGMA = 1.0
MAX_ITER = 500
TOL = 1e-10


def band_of(q):
    for (lo, hi), lab in zip(BANDS, BAND_LABELS):
        if lo <= q < hi:
            return lab
    return ""


def _components(pairs):
    """Union-find over (item, band) pairs; returns {item: root}, {band: root}."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for it, bd in pairs:
        union(("i", it), ("b", bd))
    return {k: find(k) for k in parent}


def fit_shape(obs):
    """obs: iterable of (item, band_label, price).  Returns a dict or None.

    {"levels": {band: multiplier}, "n_items": int, "n_obs": int,
     "bands_in_component": int, "items_per_band": {band: int},
     "converged": bool, "dropped_components": int}
    """
    rows = [(it, bd, float(p)) for it, bd, p in obs
            if bd in BAND_INDEX and p is not None and float(p) > 0]
    if not rows:
        return None

    # Only items seen in two or more bands carry information about beta: a
    # single-band item is absorbed entirely by its own alpha.
    seen = {}
    for it, bd, _ in rows:
        seen.setdefault(it, set()).add(bd)
    multi = {it for it, bs in seen.items() if len(bs) >= 2}
    rows = [r for r in rows if r[0] in multi]
    if len(multi) < MIN_ITEMS:
        return None

    roots = _components((it, bd) for it, bd, _ in rows)
    counts = {}
    for it, bd, _ in rows:
        counts[roots[("i", it)]] = counts.get(roots[("i", it)], 0) + 1
    main = max(counts, key=lambda r: counts[r])
    dropped = len(counts) - 1
    rows = [r for r in rows if roots[("i", r[0])] == main]
    if not rows:
        return None

    items = sorted({r[0] for r in rows})
    bands = sorted({r[1] for r in rows}, key=lambda b: BAND_INDEX[b])
    ii = {it: k for k, it in enumerate(items)}
    bi = {bd: k for k, bd in enumerate(bands)}
    y = np.array([np.log(r[2]) for r in rows])
    ai = np.array([ii[r[0]] for r in rows])
    bj = np.array([bi[r[1]] for r in rows])

    # Alternating projection.  Within one connected component this converges to
    # the least-squares two-way fixed-effects solution.
    alpha = np.zeros(len(items))
    beta = np.zeros(len(bands))
    converged = False
    for _ in range(MAX_ITER):
        resid = y - beta[bj]
        new_a = np.bincount(ai, weights=resid, minlength=len(items)) / \
            np.bincount(ai, minlength=len(items))
        resid = y - new_a[ai]
        new_b = np.bincount(bj, weights=resid, minlength=len(bands)) / \
            np.bincount(bj, minlength=len(bands))
        shift = new_b[0]            # keep the gauge fixed so the test is real
        new_b = new_b - shift
        new_a = new_a + shift
        if max(np.abs(new_a - alpha).max(), np.abs(new_b - beta).max()) < TOL:
            alpha, beta = new_a, new_b
            converged = True
            break
        alpha, beta = new_a, new_b

    # Anchor on the best-supported band, so the multiplier is 1.0 where the
    # evidence is strongest rather than at an arbitrary end of the range.
    per_band = {bd: len({r[0] for r in rows if r[1] == bd}) for bd in bands}
    anchor = max(bands, key=lambda b: (per_band[b], -BAND_INDEX[b]))
    beta = beta - beta[bi[anchor]]

    levels = {bd: float(np.exp(beta[bi[bd]])) for bd in bands
              if per_band[bd] >= MIN_SPAN_ITEMS}
    if anchor not in levels or len(levels) < 2:
        return None

    # HOW WELL the family shape describes its own members, in log units.  A
    # caller borrowing a sibling's curve is entitled to know this: it is the
    # spread that remains after the shape and each item's level are taken out,
    # so it is the extra uncertainty the borrowing itself introduces.  Reporting
    # it means the widening applied downstream can be a measurement instead of
    # an assumed share.
    resid = y - alpha[ai] - beta[bj]
    def _mad_sigma(v):
        if len(v) < 3:
            return float("nan")
        # Median absolute deviation, scaled to a Gaussian sigma.  Robust on
        # purpose: a handful of items carry these fits and one bad extraction
        # would dominate a plain standard deviation.
        return float(1.4826 * np.median(np.abs(v - np.median(v))))
    sig_band = {}
    for bd in bands:
        sig_band[bd] = _mad_sigma(resid[bj == bi[bd]])

    # The signal-vs-noise gate (see MIN_SPAN_OVER_SIGMA).  Measured on the
    # EMITTED bands only, because those are the multipliers a caller can
    # actually apply; a band dropped for thin support is not part of the claim.
    # LARGEST over SMALLEST emitted multiplier, not first band over last.
    # First-over-last silently understates a shape that is not monotone, and
    # these shapes often are not: traffic_control/HR runs 1.00, 0.75, 0.68,
    # 0.98 across its four bands, so first-over-last reads 0.018 -- which would
    # have refused a real 32% band effect as noise -- while largest-over-
    # smallest reads 0.388 against a sigma of 0.099 and keeps it.
    sigma = _mad_sigma(resid)
    span = abs(np.log(max(levels.values()) / min(levels.values())))
    if sigma == sigma and span < sigma * MIN_SPAN_OVER_SIGMA:
        return None
    return {"levels": levels, "anchor": anchor, "n_items": len(items),
            "n_obs": len(rows), "bands_in_component": len(bands),
            "items_per_band": per_band, "converged": converged,
            "dropped_components": dropped,
            "resid_sigma_log": sigma,
            "fitted_span_log": float(span),
            "resid_sigma_log_by_band": sig_band}


def ratio_errors(obs, levels):
    """How wrong is this family's shape when applied to ONE of its own items?

    Yields (band_distance, log_error) for every item observed in two bands:
    log(own ratio) - log(family ratio).  This, not the per-cell residual
    spread, is the uncertainty a caller takes on, because what a caller applies
    is a RATIO -- at zero band distance the ratio is exactly 1 and carries no
    borrowing error, while the per-cell spread is unchanged.

    MEASURED on the live corpus, and it does not match either naive guess.
    Treating an item's deviations as independent across bands predicts
    sigma*sqrt(2) = 0.223 flat; the measurement rises with distance instead:

        band distance      1      2      3      4
        cells >= 2 proj  0.265  0.340  0.407  0.469
        cells >= 4 proj  0.212  0.280  0.317  0.349
        cells >= 6 proj  0.189  0.249  0.288  0.364

    The fall across those rows is noise in the MEASURING STICK -- the item's own
    observed ratio -- not in the thing being predicted, so the irreducible
    borrowing error is the bottom row: about 0.19 at one band, 0.25 at two,
    0.29 at three.  A penalty computed as a fraction of the adjustment's own
    size cannot reproduce that shape: it goes to zero for a small adjustment,
    whereas the borrowing error does not.
    """
    price = {}
    for it, bd, p in obs:
        if bd in levels and p and float(p) > 0:
            price[(it, bd)] = float(p)
    items = sorted({k[0] for k in price})
    out = []
    for it in items:
        bds = sorted((b for b in levels if (it, b) in price),
                     key=lambda b: BAND_INDEX[b])
        for i in range(len(bds)):
            for j in range(i + 1, len(bds)):
                b1, b2 = bds[i], bds[j]
                own = np.log(price[(it, b1)] / price[(it, b2)])
                fam = np.log(levels[b1] / levels[b2])
                out.append((abs(BAND_INDEX[b1] - BAND_INDEX[b2]), own - fam))
    return out


def mad_sigma(v):
    v = np.asarray(list(v), dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 3:
        return float("nan")
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def _selftest():
    """A known shape must be recovered exactly, and a disconnected family
    must be refused rather than answered with a plausible-looking number."""
    ok = True
    true_shape = {"0-10": 2.0, "10-50": 1.5, "50-200": 1.2, "200-1000": 1.0,
                  "1000-5000": 0.9}
    obs = []
    for k in range(8):                      # 8 items, each at its own level
        level = 10.0 * (1.7 ** k)
        for bd, mult in true_shape.items():
            obs.append(("item%d" % k, bd, level * mult))
    r = fit_shape(obs)
    got = {b: round(v, 6) for b, v in r["levels"].items()}
    want = {b: round(v / true_shape[r["anchor"]], 6) for b, v in true_shape.items()}
    if got != want:
        print("FAIL exact recovery\n  got  %s\n  want %s" % (got, want))
        ok = False
    elif not r["converged"]:
        print("FAIL did not converge")
        ok = False
    else:
        print("pass: exact recovery of a known shape (anchor %s)" % r["anchor"])

    # Price LEVEL must not leak into the shape: scaling one item must not move
    # the answer.  This is the composition-immunity claim, tested.
    obs2 = [(it, bd, p * (1000.0 if it == "item3" else 1.0)) for it, bd, p in obs]
    r2 = fit_shape(obs2)
    if {b: round(v, 6) for b, v in r2["levels"].items()} != got:
        print("FAIL composition immunity")
        ok = False
    else:
        print("pass: rescaling one item does not move the shape")

    # Disconnected: items A,B only in small bands; C,D only in large bands.
    obs3 = []
    for k, bds in ((0, ["0-10", "10-50"]), (1, ["0-10", "10-50"]),
                   (2, ["5000-25000", "25000+"]), (3, ["5000-25000", "25000+"]),
                   (4, ["0-10", "10-50"])):
        for bd in bds:
            obs3.append(("it%d" % k, bd, 100.0 * true_shape.get(bd, 1.0)))
    r3 = fit_shape(obs3)
    if r3 is None or r3["dropped_components"] < 1:
        print("FAIL disconnected case: %r" % (r3 and r3["dropped_components"],))
        ok = False
    else:
        print("pass: disconnected component dropped (%d dropped, kept bands %s)"
              % (r3["dropped_components"], sorted(r3["levels"], key=lambda b: BAND_INDEX[b])))

    # Too few items must refuse outright, not return a one-item "shape".
    if fit_shape([("only", "0-10", 5.0), ("only", "10-50", 4.0)]) is not None:
        print("FAIL should refuse a single-item family")
        ok = False
    else:
        print("pass: refuses a single-item family")

    # A shape that cannot beat its own noise must be refused, not published as
    # a multiplier.  Four items, both bands well covered, every gate above
    # satisfied -- but the items disagree about the direction, so the fitted
    # span is far smaller than the scatter it was fitted through.
    noisy = []
    for k, (p1, p2) in enumerate([(100.0, 40.0), (100.0, 250.0),
                                  (100.0, 45.0), (100.0, 230.0)]):
        noisy.append(("nz%d" % k, "0-10", p1))
        noisy.append(("nz%d" % k, "10-50", p2))
    r4 = fit_shape(noisy)
    if r4 is not None:
        print("FAIL should refuse a shape smaller than its own sigma: "
              "span %.3f sigma %.3f" % (r4["fitted_span_log"],
                                        r4["resid_sigma_log"]))
        ok = False
    else:
        print("pass: refuses a shape that cannot beat its own residual spread")

    # ...and the same gate must NOT refuse a real shape: the known-shape case
    # above is fitted exactly, so its residual spread is zero.
    if fit_shape(obs) is None:
        print("FAIL the sigma gate rejected the exact-recovery case")
        ok = False
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    _selftest()
