"""Prove that no test data reached a fitted number -- and prove it by
flipping the test rows on and off, not by reading a column.

The client's rule, stated three times: the files the company supplied are
TEST data.  Only publicly sourced data may train anything, and nothing may be
overfitted to the company's own eight tabulations.  Since 2026-11-19 a second
test set lives under the same rule: a locked public holdout
(data/test_holdout/public_holdout.csv, src/public_holdout.py) whose rows carry
source_set = 'public_test'.  So here EVERY source_set other than 'external' is
test data: the external-only recomputation leaves all of it out, and each test
set is proven on its own, with its own pass/fail line.

`provenance_check.py` guards the INPUT side: which files are company files
(structurally -- a path with no separator), and whether the same solicitation
sits on both sides.  This guards the OUTPUT side, which nothing did: whether a
test row contributed to a FITTED number.  Twelve of twenty fitted artefacts
declare nothing at all about what built them, so a reader cannot tell
price_reference.csv from a pooled table without reading build_master's source.

TWO CHECKS, AND NEITHER IS A COLUMN READ.

1. DIFFERENTIAL.  Recompute each fitted statistic twice -- once over the rows
   the gate admits (source_set == 'external'), once with ONE test set admitted
   as well -- and compare all three: the artefact on disk, the external-only
   recomputation, and the admitted one.

     artefact == external-only  and  != admitted   -> the gate demonstrably held
     artefact == admitted       and  != external   -> LEAK, test data is in it
     external == admitted                          -> no discriminating evidence:
                                                      the test rows would not
                                                      have moved this number, so
                                                      the test cannot see the gate

   The third outcome is reported as what it is.  Calling it a pass would be the
   same error as calling an untested claim verified.  So would passing a test
   set that has no rows: until the rebuild that labels the holdout, data/clean
   has no 'public_test' rows, and the check says so instead of passing.

2. SENSITIVITY, which is the overfitting question rather than the leakage one.
   For each fitted parameter, how far does it move when the eight company files
   ARE admitted?  A parameter that barely moves cannot be overfitted to them.
   A parameter that lurches is fitted on too little data to be trusted at all,
   whoever supplied the rows -- so the number is reported either way.  NOT for
   the holdout: how far the calibration moves when the held-out lettings are
   admitted is a function of how they score, and the holdout may only confirm a
   decision registered beforehand -- printing its movement would spend it.  Its
   section prints verdicts and cell counts, and values only for a leak.

A RECOMPUTATION THAT IS NOT THE PRODUCING CODE'S PROVES NOTHING.

A cell that reproduces under neither formula says this module has drifted, not
that the gate held -- and it drifted three times.  The municipal discount check
kept recomputing nine per-bidder-count medians after competition_and_crosswalk
had replaced them with the pooled table times one municipal shift: 0 of 10
cells reproduced, INCONCLUSIVE in every rebuild log from 2026-11-19 07:04 on.
The pooled check rebuilt TxDOT-only ratios from the panel and reproduced 0 of
11 cells, which the verdict order then called NO EVIDENCE.  And
estimator_calibration.csv -- the centring, the quantiles and the not-under
level every estimate is multiplied and divided by -- was listed as fitted and
never recomputed at all.

So where the producing code can be called, it is called, unchanged, on
COUNTERFACTUAL COPIES OF data/clean.  Every CSV there that labels its rows with
source_set is rewritten into a temporary directory per arm -- the 'external'
rows alone, or those plus ONE test set relabelled 'external', in the original
row order -- and every other file is a symlink.  The producing module's CLEAN
is pointed at the copy and the function runs as it is:
estimate_low_bid.calibrate_forward() in worker processes (all arms at once:
about 40 s a run on an idle machine, so the guard takes a minute or two), and
estimate_low_bid._ForwardDiscount for the two discount
tables (the copy of competition_and_crosswalk's fit that the forward
calibration repeats at every date; here over every letting).  DELETING the
test rows, rather than trusting the producer's own `source_set == 'external'`
filter, is what makes the external-only arm independent of the code under
test: run on data/clean itself, a dropped filter would reproduce its own leak
and call it external-only.

The forward calibration is run once more on data/clean AS IT STANDS.  That must
equal the external-only run cell for cell.  If it does not, rows labelled other
than 'external' reach the producing code by a path that ignores the label -- a
leak whatever the file on disk says.  If it does and the file still differs,
the file is older than the data or the code: re-run its producer.

What the copies cannot do is switch rows that do not say what they are.  A
producer that starts reading a file carrying test rows WITHOUT a source_set
column would be fed them in every arm and pass -- label the file instead
(municipal_letting_totals.csv carries source_set for this reason).  The
holdout is also held against its own registry, which reads file names only and
so does not open the test set: rows of a held-out file still labelled
'external' in master_panel.csv or municipal_letting_totals.csv are training
data that no differential can see.  Before the rebuild that labels the holdout
that is expected, and reported; once any row is labelled 'public_test', it is
a leak.

Exit code 1 on any leak, so this can sit in the pipeline rather than be
remembered; 2 when data/clean changed while the guard ran (the arms would then
compare different data, so neither a pass nor a leak would mean anything --
run it again); 0 otherwise.
"""
import collections
import csv
import math
import multiprocessing
import os
import statistics
import sys
import tempfile
import textwrap
import time
from concurrent.futures import ProcessPoolExecutor

SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SRC)
CLEAN = os.path.join(ROOT, "data", "clean")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import item_tokens  # noqa: E402  (the removal identity build_master keys on)

# Fitted artefacts: a number in one of these was ESTIMATED from the corpus, so
# a company row reaching it breaks the rule.  Anything not listed here is a
# description of the data (counts, audits, parse logs), not a fitted quantity.
FITTED = [
    "price_reference.csv", "price_by_quantity_band_family.csv",
    "qty_shape_borrow_sigma.csv", "competition_discount_pooled.csv",
    "competition_discount_municipal.csv", "competition_discount.csv",
    "competition_discount_by_year.csv", "competition_discount_by_market.csv",
    "estimator_calibration.csv", "municipal_letting_totals.csv",
    "price_time_index.csv", "index_by_market.csv",
    "price_time_index_by_basket.csv", "residual_structure.csv",
    "price_by_quantity_band.csv", "price_book_txdot.csv",
    "erosion_regional_factors.csv",
    "index_weighting.csv", "unbalanced_bidding.csv",
]

# Deliberately NOT fitted: these describe the whole corpus, company files
# included, because describing it is their job.  Holding them to the train/test
# gate would flag a file that is behaving correctly, and a guard that cries
# wolf is a guard people learn to skip.  They are listed so the distinction is
# a decision in the code rather than an omission, and they must still SAY so.
DESCRIPTIVE = ["item_family_stats.csv"]
SCOPE_COLS = ("train_scope", "scope", "source_set", "split", "market", "basis")
TOL = 1e-6

# The one training label.  Every other source_set value is TEST data.
TRAIN = "external"
# The test sets proven one at a time, each with its own verdict.  A label not
# listed here is still test data -- left out of every recomputation -- but has
# no differential of its own, and is reported as a warning.
TEST_SETS = (("internal", "company files", "company"),
             ("public_test", "locked public holdout", "holdout"))
TEST_LABELS = tuple(t[0] for t in TEST_SETS)
TITLE = {t[0]: t[1] for t in TEST_SETS}
NOUN = {t[0]: t[2] for t in TEST_SETS}
COMPANY, HOLDOUT = "internal", "public_test"
# The two arms that are not a test set: the 'external' rows alone, and
# data/clean exactly as it stands (the producing code's own filters at work).
EXT, AS_IS = "external_only", "as_is"
# A held-out file's rows still labelled 'external' here are training data.
HOLDOUT_KEY_FILES = ("master_panel.csv", "municipal_letting_totals.csv")
# The artefact under test stays out of the copies: the recomputation must
# neither read its own output nor write through a symlink onto it.
VIEW_EXCLUDE = ("estimator_calibration.csv",)


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _year(r):
    y = _f(r.get("year"))
    if y is not None:
        return str(int(y))
    return str(r.get("bid_date") or "")[:4]


def load_panel():
    csv.field_size_limit(10 ** 8)
    p = os.path.join(CLEAN, "master_panel.csv")
    out = []
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            q, u = _f(r.get("quantity")), _f(r.get("unit_price"))
            ss = r.get("source_set") or ""
            out.append({
                "set": ss,
                "ext": ss == TRAIN,
                "ee": (r.get("is_engineer_estimate") or "").lower() in (
                    "true", "1", "yes"),
                "comp": r.get("competition_class") == "competitive",
                "kind": r.get("project_kind"),
                "basis": r.get("price_basis"),
                "source": r.get("source"),
                "family": r.get("item_family"), "unit": r.get("unit"),
                # build_master files a removal-only row of the family and
                # band references under family + "#rm" (REMOVAL IDENTITY),
                # and an abandon-only one under family + "#ab" when round
                # 17's AB2 is on (item_tokens.ABANDON_FAMILY_KEY)
                "fam_ref": ((r.get("item_family") or "") + "#rm"
                            if item_tokens.REMOVAL_IDENTITY
                            and item_tokens.removal_only(r.get("desc_norm"))
                            else (r.get("item_family") or "") + "#ab"
                            if item_tokens.ABANDON_IDENTITY
                            and item_tokens.ABANDON_FAMILY_KEY
                            and item_tokens.abandon_only(r.get("desc_norm"))
                            else r.get("item_family")),
                "size": r.get("size_bucket") or "",
                "key": r.get("item_key") or "",
                "let": r.get("project_key"),
                "owner": r.get("owner") or "",
                # build_master groups lettings on the panel's own `year`
                # column; bid_date[:4] is only the fallback.
                "lyear": _year(r),
                "bidder": r.get("bidder_norm") or r.get("bidder"),
                "low": r.get("is_low_bidder"),
                "year": str(r.get("bid_date") or "")[:4],
                "q": q, "u": u})
    return out


def in_pool(r, admit=None):
    """build_master's own `comp` gate, with the train/test arm switchable:
    `admit` names ONE test set whose rows are let in as well, so the
    differential can be run with that set admitted and no other."""
    if not r["ext"] and r["set"] != admit:
        return False
    if r["ee"]:
        return False
    if not r["comp"] or r["kind"] != "construction":
        return False
    if r["basis"] == "uneven_proposal":
        return False
    if not r["u"] or r["u"] <= 0:
        return False
    return True


# ---- the recomputations -------------------------------------------------
def stat_price_reference(panel, admit):
    """Two-stage family median: within letting, then across lettings.

    A letting is build_master's stage-1 key -- (source, owner, project_key,
    year) -- not project_key alone: TxDOT reuses a project number when a job
    is re-let in a later year (A00193283 2024 and 2026, ...), and merging
    those two lettings moved one cell's median (traffic_control/DAY 169.5 ->
    169.493), which read as a holdout leak on 2026-11-19."""
    s1 = collections.defaultdict(list)
    for r in panel:
        if not in_pool(r, admit):
            continue
        s1[(r["fam_ref"], r["unit"], r["size"], r["source"], r["owner"],
            r["let"], r["lyear"])].append(r["u"])
    s2 = collections.defaultdict(list)
    for (fam, un, sz, _src, _own, _let, _yr), v in s1.items():
        s2[(fam, un, sz)].append(statistics.median(v))
    return {k: statistics.median(v) for k, v in s2.items() if len(v) >= 1}


def stat_unbalanced(panel, admit):
    """median_low_bidder / median_all_bids per family+unit, TxDOT only -- the
    arm build_master actually builds it from."""
    allb = collections.defaultdict(list)
    lowb = collections.defaultdict(list)
    for r in panel:
        if not in_pool(r, admit) or r["source"] != "txdot":
            continue
        if r["low"] not in ("True", "False"):
            continue
        allb[(r["family"], r["unit"])].append(r["u"])
        if r["low"] == "True":
            lowb[(r["family"], r["unit"])].append(r["u"])
    out = {}
    for k, v in allb.items():
        if len(v) < 200 or not lowb.get(k):
            continue
        out[k] = statistics.median(lowb[k]) / statistics.median(v)
    return out


def _on_panel(fn):
    """A recomputation over load_panel()'s rows, as a gate: it does its own
    source_set filtering, so it has no as-is arm."""
    def run(ctx, arm):
        if arm == AS_IS:
            return None
        return fn(ctx.panel, None if arm == EXT else arm)
    return run


def _estimator():
    """estimate_low_bid -- the producing code -- imported on first use."""
    import estimate_low_bid
    return estimate_low_bid


class _reading:
    """Point the producing module at directory `d` for the block."""

    def __init__(self, module, d):
        self.m, self.d = module, d

    def __enter__(self):
        self.old, self.m.CLEAN = self.m.CLEAN, self.d
        return self.m

    def __exit__(self, *exc):
        self.m.CLEAN = self.old
        return False


DISC_COLS = (("low_over_median", "f"), ("p25", "p25"), ("p75", "p75"),
             ("lettings", "lettings"))


def _discount_fit(ctx, arm):
    """Both discount tables over EVERY letting, as competition_and_crosswalk
    publishes them.  estimate_low_bid._ForwardDiscount reads that script's
    three intermediates (TxDOT archive totals, TxDOT current spread,
    municipal_letting_totals.csv) and its _fit is that script's arithmetic --
    pooled low/median by bidder count over bins of >= 10 lettings, municipal =
    pooled x one shift, n = 1 forced to 1.0 -- the fit the forward calibration
    repeats at every date.  Run on `arm`'s copy of data/clean."""
    if arm == AS_IS:
        return None
    key = ("discount", arm)
    if key not in ctx.memo:
        with _reading(_estimator(), ctx.views[arm]) as elb:
            fd = elb._ForwardDiscount({})
            ctx.memo[key] = fd._fit(fd.LD)
    return ctx.memo[key]


def _discount_cells(table):
    return {(n, col): float(row[k]) for n, row in table.items()
            for col, k in DISC_COLS if row.get(k) is not None}


def stat_discount_pooled(ctx, arm):
    """The pooled table has a municipal arm too, so test lettings in
    municipal_letting_totals.csv can move it.  The old recomputation rebuilt
    TxDOT-only ratios from the panel, which no test row can reach, and
    reproduced 0 of 11 cells."""
    fit = _discount_fit(ctx, arm)
    return None if fit is None else _discount_cells(fit[0])


def stat_discount_municipal(ctx, arm):
    """Read the SAME intermediate the producing script reads, through the
    producing arithmetic.  Nine per-bidder-count medians of the municipal
    ratios -- the formula this module kept after competition_and_crosswalk
    moved to pooled x one shift -- reproduced 0 of 10 cells."""
    fit = _discount_fit(ctx, arm)
    return None if fit is None else _discount_cells(fit[1])


def _forward_calibration(clean_dir):
    """Worker process: estimate_low_bid.calibrate_forward() as production runs
    it, reading `clean_dir` in place of data/clean and writing nothing.  Also
    returns what V2+F1+F2 fitted on the way (_fitted_summary)."""
    import estimate_low_bid
    estimate_low_bid.CLEAN = clean_dir
    t = time.time()
    pl, fx = [], {}
    rows, cov = estimate_low_bid.calibrate_forward(write=False, per_letting=pl,
                                                   fitted=fx)
    return rows, dict(cov), time.time() - t, _fitted_summary(fx, pl)


def _fitted_summary(fx, pl):
    """V2+F1+F2's fitted quantities, which live in no file: they are fitted
    in-process every time calibrate_forward() or a new estimate runs.  One
    {cell: value} map per quantity, comparable across arms by equality.
      F1 size cells   (family key, unit, bucket) -> (lettings, size_ref)
      F1 slope k      (family key, unit) -> k over every letting
      F1 lettings     every letting in a size cell
      CMP pool        market | letting -> (date, bidders, non-LS amount)
      CMP beta/gamma  market -> (beta, gamma) at the 2023-01-01 cutoff
      CMP chosen      scored letting -> its comparables
      T1 mob ratio    every TxDOT letting date D -> (r(D), lettings behind it):
                      the mobilization ratio, fitted under the coverage
                      scope with the uncovered lines out of its bids
                      (estimate_low_bid.MobRatio, 2026-11-20)
      MC cells        (table, cell) -> (lettings, median price) of the
                      municipal-only reference cells (round 10 CORE's MC;
                      calibrate_forward builds them whenever `fitted` is
                      asked for, so they are guarded while MUNI_CELLS is
                      still off)
      MC lettings     every letting in a municipal-only cell
      L market level  base family -> R over every letting (round 13 P_L's
                      municipal / TxDOT level of the both-market band
                      cells; calibrate_forward builds it whenever `fitted`
                      is asked for, so it is guarded while
                      MUNI_TXDOT_LEVEL is still off), "(all)" -> the
                      all-family R
      L lettings      every letting in a both-market band cell
    (round 10 CORE's other parts fit nothing new: E re-keys the comparables
    pool's line identity -- the CMP rows above -- F1P and T2 only choose
    among the cells above, and D reads the letting's own bids.)
    (size_ref and k at an earlier date D are prefixes of the same cells.)"""
    import comparables
    import statistics
    out = {}
    cm = fx.get("muni_cells")
    if cm is not None:
        out["MC municipal cells"] = {
            (kd, k): (len(by), statistics.median(e[0] for v in by.values() for e in v))
            for kd in ("tok", "band", "fam") for k, by in cm[kd].items()}
        out["MC municipal-cell lettings"] = {
            pk: 1 for kd in ("tok", "band", "fam") for by in cm[kd].values() for pk in by}
    ml = fx.get("market_level")
    if ml is not None:
        lv = {f: ml.ratio(f)[0] for f in sorted(ml.fam)}
        lv["(all)"] = ml._all(None)[0]
        out["L market level"] = lv
        out["L both-market lettings"] = {pk: 1 for pk in ml.lettings()}
    t1 = fx.get("t1")
    if t1 is not None:
        out["T1 mobilization ratio"] = {D: t1.mob.at(D) for D in
                                        sorted(set(t1.mob.lets)) + [None]}
    si = fx.get("size_cells")
    if si is not None:
        out["F1 size cells"] = {k: (len(si.cells[k][1]), si.size_ref(k))
                                for k in si.cells}
        out["F1 slope k"] = {fu: si.k_at(fu[0], fu[1])[0] for fu in si.sized}
        out["F1 size-cell lettings"] = {pk: 1 for pk in si.lettings()}
    pools = fx.get("comparables") or {}
    if pools:
        out["CMP pool"] = {(mk, Lr["pk"]): (Lr["date"], Lr["n"], Lr["nonls"])
                           for mk, pool in pools.items() for Lr in pool.ctx.L}
        out["CMP beta/gamma"] = {
            (mk, comparables.DEV_END): tuple(pool.bg.at(comparables.DEV_END)[:2])
            for mk, pool in pools.items()}
        out["CMP chosen comparables"] = {r["project_key"]: tuple(r.get("comps") or ())
                                         for r in pl}
    return out


def _keys_of(name, k):
    """The lettings a cell names (for the label check)."""
    if name == "CMP pool":
        return [k[1]]
    if name in ("F1 size-cell lettings", "MC municipal-cell lettings",
                "L both-market lettings"):
        return [k]
    return []


def v2f1f2_fitted(ctx, names):
    """The differential for V2+F1+F2's in-process fitted quantities: as-is
    (production on data/clean) vs external-only vs one test set admitted,
    plus a direct label check of every letting they hold.  Returns
    ({label: status}, failed)."""
    asis, ext = ctx.fitted(AS_IS), ctx.fitted(EXT)
    label = {}
    for r in ctx.panel:
        label.setdefault(r["let"], r["set"])
    status, failed = {}, False
    if not ext:
        print("  no V2+F1+F2 quantities came back from the external-only run "
              "(switches off?) -- nothing to check")
        return {lab: "NOT TESTED" for lab in TEST_LABELS}, False
    for q in sorted(ext):
        e_, a_ = ext[q], asis.get(q, {})
        diff = [k for k in set(e_) | set(a_) if e_.get(k) != a_.get(k)]
        if q == "CMP chosen comparables":
            lets = set(a_) | {c for comps in a_.values() for c in comps}
        else:
            lets = {pk for k in a_ for pk in _keys_of(q, k)}
        held = sorted(pk for pk in lets if label.get(pk, TRAIN) != TRAIN)
        line = "  %-26s %6d cells" % (q, len(e_))
        if diff or held:
            failed = True
            print(line + "  LEAK")
            if diff:
                print("     production on data/clean differs from the "
                      "external-only run on %d cells: rows labelled other than "
                      "'external' reach it" % len(diff))
            if held:
                print("     holds %d letting(s) labelled as test data, e.g. %s"
                      % (len(held), held[:2]))
            continue
        parts = []
        for lab in TEST_LABELS:
            if not ctx.present(lab):
                parts.append("%s: no rows" % NOUN[lab])
                continue
            adm = ctx.fitted(lab).get(q, {})
            mv = sum(1 for k in set(e_) | set(adm) if e_.get(k) != adm.get(k))
            parts.append("%s: %s" % (NOUN[lab], ("GATE HELD, %d cells would "
                                                  "move if admitted" % mv)
                                     if mv else "NO EVIDENCE"))
        print(line + "  as-is = external-only; " + "; ".join(parts))
    for lab in TEST_LABELS:
        if failed:
            status[lab] = "FAILED -- see section 1e"
        elif not ctx.present(lab):
            status[lab] = "NOT TESTED -- no %s rows present" % lab
        else:
            n = 0
            for q in ext:
                adm = ctx.fitted(lab).get(q, {})
                n += any(ext[q].get(k) != adm.get(k) for k in set(ext[q]) | set(adm))
            status[lab] = ("PASSED -- gate held on %d of %d quantities, no "
                           "discriminating evidence on %d"
                           % (n, len(ext), len(ext) - n))
    return status, failed


def stat_calibration(ctx, arm):
    """Every numeric cell of estimator_calibration.csv, keyed (market, column):
    centring, quantiles, the not-under divisor and the guard's level among
    them, from calibrate_forward() itself on `arm`'s copy of data/clean (on
    data/clean as it stands for AS_IS)."""
    out = {}
    for r in ctx.calibration(arm)[0]:
        for c, v in r.items():
            x = _f(v)
            if c != "market" and x is not None:
                out[(r.get("market"), c)] = x
    return out


Gate = collections.namedtuple("Gate", "artefact keys valcols fn note producer")
GATES = [
    # valcols: the columns compared; None = every numeric column.  A gate with
    # one column keys its cells as before; with several, (key..., column).
    Gate("price_reference.csv", ("item_family", "unit", "size_bucket"),
         ("price_median",), _on_panel(stat_price_reference),
         "two-stage family median over the price pool", "build_master.py"),
    Gate("unbalanced_bidding.csv", ("item_family", "unit"), ("low_over_all",),
         _on_panel(stat_unbalanced), "winner/all-bids item ratio, TxDOT arm",
         "build_master.py"),
    Gate("competition_discount_municipal.csv", ("n_bidders",),
         tuple(c for c, _ in DISC_COLS), stat_discount_municipal,
         "pooled x one municipal shift (_ForwardDiscount, every letting)",
         "competition_and_crosswalk.py"),
    Gate("competition_discount_pooled.csv", ("n_bidders",),
         tuple(c for c, _ in DISC_COLS), stat_discount_pooled,
         "low/median by bidder count, TxDOT + municipal arms",
         "competition_and_crosswalk.py"),
    Gate("estimator_calibration.csv", ("market",), None, stat_calibration,
         "calibrate_forward(): centring, quantiles, not-under guard",
         "estimate_low_bid.py"),
]
GATE_BY_NAME = {g.artefact: g for g in GATES}


# ---- counterfactual copies of data/clean ---------------------------------
def _csv_header(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f), [])


def _holdout_registry():
    """(is_holdout memoised per key, number of registered files), or (None, 0)
    without a registry.  Reads file NAMES only -- nothing of the held-out
    lettings' prices or scores -- so it does not open the test set."""
    try:
        import public_holdout
    except ImportError:
        return None, 0
    files = public_holdout.holdout_files()
    if not files:
        return None, 0
    memo = {}

    def test(key):
        r = memo.get(key)
        if r is None:
            r = memo[key] = public_holdout.is_holdout(key)
        return r
    return test, len(files)


def build_views(root, holdout=None):
    """One copy of data/clean per arm, differing from it ONLY in rows labelled
    other than 'external': `external_only` keeps the 'external' rows alone;
    `admit_<label>` keeps them plus that test set's rows relabelled
    'external', in the original order, so every `source_set == 'external'`
    filter in the producing code lets exactly that set through.  A CSV with no
    test rows, and every other file, is a symlink to the real one.

    Returns ({arm: directory}, {file: Counter of source_set labels},
    {file: rows labelled 'external' that belong to a held-out file})."""
    dirs = {EXT: os.path.join(root, EXT)}
    for lab in TEST_LABELS:
        dirs[lab] = os.path.join(root, "admit_" + lab)
    for d in dirs.values():
        os.makedirs(d)
    csv.field_size_limit(10 ** 8)
    census, held = {}, {}
    for name in sorted(os.listdir(CLEAN)):
        if name.startswith(".") or name in VIEW_EXCLUDE:
            continue
        src = os.path.join(CLEAN, name)
        head = (_csv_header(src) if name.lower().endswith(".csv")
                and os.path.isfile(src) else [])
        if "source_set" not in head:
            for d in dirs.values():
                os.symlink(src, os.path.join(d, name))
            continue
        i = head.index("source_set")
        kc = next((head.index(c) for c in ("src_file", "project_key",
                                           "letting_id") if c in head), None)
        labels = collections.Counter()
        n_held = 0
        outs = {a: open(os.path.join(d, name), "w", encoding="utf-8-sig",
                        newline="") for a, d in dirs.items()}
        try:
            ws = {a: csv.writer(f, lineterminator="\n")
                  for a, f in outs.items()}
            for w in ws.values():
                w.writerow(head)
            with open(src, encoding="utf-8-sig", newline="") as f:
                rd = csv.reader(f)
                next(rd, None)
                for row in rd:
                    lab = row[i] if i < len(row) else ""
                    labels[lab] += 1
                    if lab == TRAIN:
                        for w in ws.values():
                            w.writerow(row)
                        if (holdout is not None and kc is not None
                                and kc < len(row) and holdout(row[kc])):
                            n_held += 1
                    elif lab in TEST_LABELS:
                        row[i] = TRAIN
                        ws[lab].writerow(row)
                    # any other label is test data too: in no copy at all
        finally:
            for f in outs.values():
                f.close()
        census[name] = labels
        if n_held:
            held[name] = n_held
        if set(labels) <= {TRAIN}:
            for d in dirs.values():       # no test rows: every copy IS the file
                dst = os.path.join(d, name)
                os.remove(dst)
                os.symlink(src, dst)
    return dirs, census, held


class Recompute:
    """Everything the gates recompute from, built once and shared: the panel,
    the counterfactual copies of data/clean, and calibrate_forward() replayed
    on each arm in worker processes -- the slow part, so it starts first."""

    def __init__(self):
        self.panel = None
        self.views = {AS_IS: CLEAN}
        self.census, self.held_external = {}, {}
        self.registry_files = 0
        self.memo = {}
        self._runs = {}
        self._tmp = self._pool = None

    def __enter__(self):
        try:
            holdout, self.registry_files = _holdout_registry()
            self._tmp = tempfile.TemporaryDirectory(prefix="train_test_guard_")
            self._pool = ProcessPoolExecutor(
                max_workers=2 + len(TEST_SETS),
                mp_context=multiprocessing.get_context("spawn"))
            # data/clean as it stands needs no copy, so it starts at once
            self._runs[AS_IS] = self._pool.submit(_forward_calibration, CLEAN)
            views, self.census, self.held_external = build_views(
                self._tmp.name, holdout)
            self.views.update(views)
            for arm in (EXT,) + TEST_LABELS:
                if arm == EXT or self.present(arm):
                    self._runs[arm] = self._pool.submit(_forward_calibration,
                                                        views[arm])
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
        return False

    def present(self, label):
        """Rows carrying `label`, over every labelled file in data/clean."""
        return sum(c.get(label, 0) for c in self.census.values())

    def calibration(self, arm):
        """(rows, coverage, seconds) of calibrate_forward() on `arm`; nothing
        for a test set with no rows, which is never replayed."""
        run = self._runs.get(arm)
        return run.result()[:3] if run is not None else ([], {}, 0.0)

    def fitted(self, arm):
        """V2+F1+F2's in-process fitted quantities from that same run."""
        run = self._runs.get(arm)
        return run.result()[3] if run is not None else {}

    def arm(self, gate, arm):
        key = (gate.artefact, arm)
        if key not in self.memo:
            self.memo[key] = gate.fn(self, arm)
        return self.memo[key]


# ---- the verdicts ---------------------------------------------------------
def _read(name):
    p = os.path.join(CLEAN, name)
    if not os.path.exists(p):
        return None, []
    with open(p, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd), (rd.fieldnames or [])


def _keyof(row, cols):
    out = []
    for c in cols:
        v = row.get(c)
        if c == "n_bidders":
            n = _f(v)
            out.append(int(n) if n is not None else None)
        else:
            out.append(v if v is not None else "")
    return tuple(out)


def _file_cells(g, rows):
    """(cell, value on disk) for every value the gate compares."""
    multi = g.valcols is None or len(g.valcols) > 1
    for r in rows:
        k = _keyof(r, g.keys)
        cols = (g.valcols if g.valcols is not None
                else [c for c in r if c not in g.keys])
        for c in cols:
            want = _f(r.get(c))
            if want is not None:
                yield (k + (c,) if multi else k), want


def _close(x, ref):
    return abs(x - ref) <= TOL * max(abs(ref), 1.0)


def code_diff(ext, asis):
    """Cells where the producing code on data/clean as it stands differs from
    its own run on the external-only copy."""
    out = []
    for k in sorted(set(ext) | set(asis), key=repr):
        e, a = ext.get(k), asis.get(k)
        if e is None or a is None or not _close(a, e):
            out.append((k, e, a))
    return out


def judge(g, rows, ext, adm, asis, name, others=()):
    """One gate, one test set: the file against the external-only and the
    admitted recomputation.  `others`: the admitted recomputations of the
    OTHER test sets present.  A file that holds one set's rows can land on
    another set's admitted value by coincidence -- one more letting in a bin
    is 608 whichever set supplied it -- so a cell is this set's leak only if
    no other set explains it as well."""
    tested = agree_e = agree_a = discriminating = decisive = neither = 0
    shared = 0
    leaks = []
    if ext is None or adm is None:
        ext, adm = {}, {}                 # no recomputation: NOT REPRODUCED
    for k, want in _file_cells(g, rows):
        if k not in ext or k not in adm:
            continue
        tested += 1
        e_ok = _close(ext[k], want)
        a_ok = _close(adm[k], want)
        same = _close(adm[k], ext[k])
        if not (e_ok or a_ok):
            neither += 1
        if not same:
            discriminating += 1
            if a_ok and not e_ok:
                if any(k in o and _close(o[k], want) for o in others):
                    shared += 1
                else:
                    leaks.append((k, want, ext[k], adm[k]))
            elif e_ok and not a_ok:
                decisive += 1
        agree_e += 1 if e_ok else 0
        agree_a += 1 if a_ok else 0
    note_shared = ("  (%d more cells match another test set's admitted value "
                   "as well, so they are not counted here)" % shared
                   if shared else "")
    # A cell matching NEITHER recomputation is not evidence of anything.  With
    # the producer's own run on data/clean in hand, it can at least be told
    # apart: if that run reproduces the external-only value, the FILE is stale.
    if asis is not None and not code_diff(ext, asis):
        drift = ("the producing code, run now on data/clean, gives the "
                 "external-only value and the file does not: the file is "
                 "older than the data or the code -- re-run src/%s"
                 % g.producer)
    else:
        drift = "this module's formula has drifted from the producing script"
    if not tested:
        v = "NOT REPRODUCED"
        detail = ("recomputation did not line up with the file's keys; "
                  "this check proves nothing here")
    elif leaks:
        v = "LEAK"
        detail = ("%d cells match the %s-admitted recomputation and NOT the "
                  "external-only one" % (len(leaks), name)) + note_shared
    elif neither == tested:
        v = "INCONCLUSIVE"
        detail = ("none of the %d cells reproduces under either "
                  "recomputation -- %s, so the gate is UNVERIFIED here, not "
                  "held" % (tested, drift))
    elif discriminating == 0:
        v = "NO EVIDENCE"
        detail = ("%s rows would not move any of the %d cells tested, so the "
                  "gate cannot be observed here" % (name, tested))
    elif decisive == 0:
        v = "INCONCLUSIVE"
        detail = ("%d cells would move if %s rows were admitted, but the file "
                  "lands on the external-only value on none of them (%d of %d "
                  "cells reproduce at all) -- %s, so the gate is UNVERIFIED "
                  "here, not held"
                  % (discriminating, name, tested - neither, tested,
                     drift)) + note_shared
    else:
        v = "GATE HELD"
        detail = ("%d of %d cells would move if %s rows were admitted; the "
                  "file matches the external-only value on %d of them and the "
                  "admitted value on none"
                  % (discriminating, tested, name, decisive))
        if neither:
            detail += ("  (%d cells reproduce under neither formula and "
                       "were not counted)" % neither)
        detail += note_shared
    return {"artefact": g.artefact, "verdict": v, "detail": detail,
            "tested": tested, "discriminating": discriminating,
            "decisive": decisive, "neither": neither,
            "matched_external": agree_e, "matched_admitted": agree_a,
            "note": g.note, "leaks": leaks[:5], "extra": []}


def differential(ctx, label, name):
    """The core check for one test set: artefact vs external-only vs that set
    admitted."""
    verdicts = []
    for g in GATES:
        rows, _ = _read(g.artefact)
        if rows is None:
            verdicts.append({"artefact": g.artefact, "verdict": "MISSING",
                             "detail": "file absent", "extra": []})
            continue
        others = [ctx.arm(g, o) for o in TEST_LABELS
                  if o != label and ctx.present(o)]
        verdicts.append(judge(g, rows, ctx.arm(g, EXT), ctx.arm(g, label),
                              ctx.arm(g, AS_IS), name,
                              [o for o in others if o]))
    return verdicts


def calibration_notes(ctx, label, name):
    """What the forward replay saw, for the calibration gate's verdict."""
    _, ce, se = ctx.calibration(EXT)
    _, ca, sa = ctx.calibration(label)
    with _reading(_estimator(), CLEAN) as elb:
        tab = elb.Tables()
    n_pub = len(tab.ref_tok) + len(tab.ref_band) + len(tab.ref)
    me = ce.get("reference_rebuild_mismatch", 0)
    ma = ca.get("reference_rebuild_mismatch", 0)
    return [
        "forward replay (calibrate_forward, %.0f s a run, arms in parallel): "
        "lettings scored external-only %d, %s-admitted %d"
        % (max(se, sa), ce.get("scored", 0), name, ca.get("scored", 0)),
        "reference tables it prices from (price_reference / _band / _tokens, "
        "rebuilt by _reference_cells): the external-only rebuild reproduces "
        "%d of %d published rows; admitting %s rows would change %d of them"
        % (n_pub - me, n_pub, name, ma)]


def print_verdicts(vs, name):
    for v in vs:
        print("  %-38s %s" % (v["artefact"], v["verdict"]))
        print("     %s" % v["detail"])
        if v.get("tested"):
            print("     cells tested %d | matches external-only %d | matches "
                  "%s-admitted %d" % (v["tested"], v["matched_external"],
                                      name, v["matched_admitted"]))
        for k, want, e, a in v.get("leaks", []):
            print("     LEAK %r  file=%.6g external=%.6g admitted=%.6g"
                  % (k, want, e, a))
        for line in v.get("extra", []):
            print("     %s" % line)


def set_status(vs, name):
    """(verdict line for one test set, failed?)"""
    by = collections.defaultdict(list)
    for v in vs:
        by[v["verdict"]].append(v["artefact"])
    if by["LEAK"]:
        return ("FAILED -- %d fitted artefact(s) contain %s-derived numbers: "
                "%s" % (len(by["LEAK"]), name, ", ".join(by["LEAK"])), True)
    unv = by["INCONCLUSIVE"] + by["NOT REPRODUCED"] + by["MISSING"]
    s = ("PASSED -- gate held on %d of %d artefacts, no discriminating "
         "evidence on %d" % (len(by["GATE HELD"]), len(vs),
                             len(by["NO EVIDENCE"])))
    if unv:
        s += "; UNVERIFIED (proves nothing either way): " + ", ".join(unv)
    return s, False


def sensitivity(ctx, label):
    """How far would each fitted parameter move if this test set were
    admitted?  This is the overfitting question, not the leakage one."""
    out = []
    for g in GATES:
        ext, allr = ctx.arm(g, EXT), ctx.arm(g, label)
        if not ext or not allr:
            continue
        both = set(ext) & set(allr)
        if not both:
            continue
        rel = []
        for k in both:
            if ext[k]:
                rel.append(abs(allr[k] / ext[k] - 1))
        if not rel:
            continue
        rel.sort()
        # Name the worst cells.  A cell that would move several hundred percent
        # on eight files is not a leakage problem -- the gate keeps those rows
        # out -- it is a cell whose fitted value rests on almost no evidence,
        # and that is the other half of what overfitting means here.
        worst = sorted(((abs(allr[k] / ext[k] - 1), k) for k in both
                        if ext[k]), key=lambda t: t[0], reverse=True)[:5]
        out.append({"artefact": g.artefact, "cells": len(both),
                    "moved": sum(1 for x in rel if x > TOL),
                    "median_move": statistics.median(rel),
                    "max_move": rel[-1], "worst": worst})
    return out


# The constants estimate() reads from estimator_calibration.csv, and how a
# move in each is best stated: relative, in points of a share, or a count.
CALIB_SHOWN = (("level_correction", "rel"), ("p10", "rel"), ("p25", "rel"),
               ("p75", "rel"), ("p90", "rel"), ("p_not_under", "rel"),
               ("not_under_level", "pts"), ("n", "count"),
               ("guard_under_share", "pts"))


def calibration_moves(ctx, label):
    """(market, constant, on disk, external-only, admitted, kind) for each
    constant estimate() reads, plus the not-under estimate's own multiplier,
    level_correction / p_not_under."""
    rows, _ = _read("estimator_calibration.csv")
    g = GATE_BY_NAME["estimator_calibration.csv"]
    ext, adm = ctx.arm(g, EXT), ctx.arm(g, label)

    def mult(lc, pn):
        return lc / pn if lc and pn else None
    out = []
    for r in rows or []:
        mk = r.get("market")
        for c, kind in CALIB_SHOWN:
            d, e, a = _f(r.get(c)), ext.get((mk, c)), adm.get((mk, c))
            if e is not None and a is not None:
                out.append((mk, c, d, e, a, kind))
        e = mult(ext.get((mk, "level_correction")),
                 ext.get((mk, "p_not_under")))
        a = mult(adm.get((mk, "level_correction")),
                 adm.get((mk, "p_not_under")))
        if e and a:
            out.append((mk, "P80 multiplier",
                        mult(_f(r.get("level_correction")),
                             _f(r.get("p_not_under"))), e, a, "rel"))
    return out


def _move(e, a, kind):
    if kind == "count":
        return "%+d" % round(a - e)
    if kind == "pts":
        return "%+.2f pts" % (100 * (a - e))
    return ("%+.2f%%" % (100 * (a / e - 1))) if e else "n/a"


def scope_declarations():
    missing = []
    for name in FITTED + DESCRIPTIVE:
        rows, cols = _read(name)
        if rows is None:
            continue
        if not any(c.lower() in SCOPE_COLS for c in cols):
            missing.append((name, len(rows)))
    return missing


def _snapshot():
    out = {}
    for name in os.listdir(CLEAN):
        if name.startswith("."):
            continue
        try:
            st = os.stat(os.path.join(CLEAN, name))
        except OSError:
            continue
        out[name] = (st.st_size, st.st_mtime_ns)
    return out


def _bar(title):
    print("=" * 76)
    print(title)
    print("=" * 76)


def _wrap(text, indent="  "):
    print(textwrap.fill(text, width=76, initial_indent=indent,
                        subsequent_indent=indent))


def main():
    t0 = time.time()
    before = _snapshot()
    names = TITLE
    status, failed = {}, False
    with Recompute() as ctx:
        ctx.panel = panel = load_panel()
        sets = collections.Counter(r["set"] for r in panel)
        print("panel rows %d: external %d | %s"
              % (len(panel), sets.get(TRAIN, 0), " | ".join(
                  "%s %d" % (names[lab], sets.get(lab, 0))
                  for lab in TEST_LABELS)))
        pool_e = sum(1 for r in panel if in_pool(r))
        parts = ["external-only %d" % pool_e]
        for lab in TEST_LABELS:
            if not sets.get(lab):
                parts.append("%s: no rows" % names[lab])
                continue
            pool_a = sum(1 for r in panel if in_pool(r, lab))
            parts.append("%s admitted %d (+%d)"
                         % (names[lab], pool_a, pool_a - pool_e))
            if pool_a == pool_e:
                print("WARNING: admitting the %s changes the pool by 0 rows, "
                      "so the differential below has nothing to discriminate "
                      "with -- check that those rows survive the other gates "
                      "at all" % names[lab])
        print("price pool: " + " | ".join(parts))
        print()
        print("test rows per file, switched on ONE SET AT A TIME in temporary "
              "copies of data/clean")
        print("(the external-only copy has none of them):")
        for f, c in sorted(ctx.census.items()):
            t = sorted((k, v) for k, v in c.items() if k != TRAIN)
            if t:
                print("   %-36s %s" % (f, ", ".join(
                    "%s %d" % (k or "(blank)", v) for k, v in t)))
        odd = sorted({k for c in ctx.census.values() for k in c}
                     - {TRAIN} - set(TEST_LABELS))
        if odd:
            print("WARNING: rows labelled %s are test data by this guard's "
                  "rule (every label but 'external') and are in no "
                  "recomputation, but no differential is run for them -- add "
                  "the label to TEST_SETS" % ", ".join(repr(x) for x in odd))
        print()

        # 1, 1b. one differential per test set, each with its own verdict
        held_key = {f: n for f, n in ctx.held_external.items()
                    if f in HOLDOUT_KEY_FILES}
        for idx, (lab, name, noun) in enumerate(TEST_SETS):
            _bar("1%s. DIFFERENTIAL, %s (source_set '%s'): artefact vs "
                 "external-only vs admitted"
                 % (("", "b", "c", "d")[idx], name.upper(), lab))
            if lab == HOLDOUT:
                if ctx.registry_files:
                    print("  registry data/test_holdout/public_holdout.csv: "
                          "%d held-out files" % ctx.registry_files)
                    if ctx.held_external:
                        print("  rows of them still labelled 'external': %s"
                              % ", ".join("%s %d" % kv for kv
                                          in sorted(ctx.held_external.items())))
                else:
                    print("  no registry at "
                          "data/test_holdout/public_holdout.csv")
            if not ctx.present(lab):
                print("  no %s rows present in data/clean -- there is nothing "
                      "to switch on, so NOTHING is proven for the %s yet."
                      % (lab, name))
                if lab == HOLDOUT and held_key:
                    print("  The held-out files above are still TRAINING data "
                          "in every fitted artefact until the rebuild that "
                          "labels them '%s'." % lab)
                status[lab] = "NOT TESTED -- no %s rows present" % lab
                print()
                continue
            vs = differential(ctx, lab, noun)
            if lab != HOLDOUT:
                for v in vs:
                    if (v["artefact"] == "estimator_calibration.csv"
                            and v.get("tested")):
                        v["extra"] = calibration_notes(ctx, lab, noun)
            print_verdicts(vs, noun)
            status[lab], bad = set_status(vs, noun)
            if lab == HOLDOUT:
                if held_key:
                    bad = True
                    status[lab] = (
                        "FAILED -- rows of held-out files are still labelled "
                        "'external' (%s): they train the model, and no "
                        "differential can see them" % ", ".join(
                            "%s %d" % kv for kv in sorted(held_key.items())))
                    print("  LEAK: %s" % status[lab][len("FAILED -- "):])
                print("  (values are printed for a leak only: the holdout's "
                      "movement is not shown -- see the docstring)")
            failed |= bad
            print()

        # 1c. the producing code itself, on the real files
        _bar("1%s. THE PRODUCING CODE ON data/clean AS IT STANDS (every test "
             "set present)" % "bcdefg"[len(TEST_SETS) - 1])
        code_bad = []
        for g in GATES:
            asis = ctx.arm(g, AS_IS)
            if asis is None:
                continue
            diff = code_diff(ctx.arm(g, EXT), asis)
            if diff:
                code_bad.append(g.artefact)
                print("  %-38s LEAK" % g.artefact)
                print("     the producing code on data/clean differs from its "
                      "own run on the external-only copy on %d cells: rows "
                      "labelled other than 'external' reach it by a path that "
                      "ignores the label" % len(diff))
                for k, e, a in diff[:5]:
                    print("     %r  external-only=%s  as-is=%s" % (k, e, a))
            else:
                print("  %-38s HELD" % g.artefact)
                print("     %s on data/clean equals its run on the "
                      "external-only copy on all %d cells: no row labelled "
                      "other than 'external' reaches it, by any path"
                      % (g.note.split(":")[0], len(asis)))
        status[AS_IS] = ("FAILED -- %s" % ", ".join(code_bad) if code_bad else
                         "PASSED -- equals the external-only run on every "
                         "cell")
        failed |= bool(code_bad)
        print()

        # 1e. V2+F1+F2: quantities fitted in-process, in no file
        _bar("1e. V2+F1+F2 FITTED IN-PROCESS: F1 size cells and slope k, the "
             "comparables pool, beta/gamma, chosen comparables, T1 "
             "mobilization ratio (coverage scope), MC municipal cells (round "
             "10 CORE, guarded while its switch is off)")
        _wrap("No file holds these: calibrate_forward() and every new "
              "estimate fit them from the price pool.  So the producing "
              "code's own run on data/clean stands in for the file: it must "
              "equal the external-only run cell for cell, hold no letting "
              "labelled as test data, and differ from the run with a test "
              "set admitted where those rows could reach it.")
        st_v, bad_v = v2f1f2_fitted(ctx, names)
        status["v2f1f2"] = st_v
        failed |= bad_v
        print()

        # 2. sensitivity: the company files only
        _bar("2. SENSITIVITY  (how far a parameter moves if company data IS "
             "let in)")
        print("  %-38s %7s %7s %12s %12s"
              % ("artefact", "cells", "moved", "median move", "worst move"))
        sens = sensitivity(ctx, COMPANY)
        for s in sens:
            print("  %-38s %7d %7d %11.4f%% %11.4f%%"
                  % (s["artefact"], s["cells"], s["moved"],
                     100 * s["median_move"], 100 * s["max_move"]))
        print()
        print("  A parameter that barely moves cannot be overfitted to the eight")
        print("  company files.  One that lurches is fitted on too little data to")
        print("  trust whoever supplied the rows -- so the worst cells are named:")
        print()
        for s in sens:
            if s["artefact"] == "estimator_calibration.csv":
                continue            # constant by constant below
            big = [(m, k) for m, k in s.get("worst", []) if m > 0.10]
            if not big:
                continue
            print("  %s" % s["artefact"])
            for m, k in big:
                print("     %+9.1f%%  %s"
                      % (100 * m, " / ".join(str(x) for x in k)))
        print()
        print("  Those cells are NOT contaminated -- the gate keeps the company")
        print("  rows out of every one of them. They are cells whose fitted value")
        print("  would be overturned by a handful of rows, which is the same")
        print("  thinness showing from the other side. Do not quote them.")
        print()
        if ctx.arm(GATE_BY_NAME["estimator_calibration.csv"], COMPANY):
            print("  estimator_calibration.csv, each constant estimate() reads, "
                  "if company rows were admitted:")
            print("     %-10s %-17s %10s %14s %10s %11s"
                  % ("market", "constant", "on disk", "external-only",
                     "admitted", "move"))

            def fmt(x, kind):
                return ("-" if x is None else "%d" % round(x)
                        if kind == "count" else "%.6f" % x)
            for mk, c, d, e, a, kind in calibration_moves(ctx, COMPANY):
                print("     %-10s %-17s %10s %14s %10s %11s"
                      % (mk, c, fmt(d, kind), fmt(e, kind), fmt(a, kind),
                         _move(e, a, kind)))
            elb = _estimator()
            print()
            _wrap("P80 multiplier = level_correction / p_not_under: what the "
                  "not-under estimate multiplies the discounted total by, so "
                  "its move is the move in the figure a bid is built from.  "
                  "not_under_level is the online guard: each scored letting "
                  "moves it down by gamma x (1 - %.2f) on a miss and up by "
                  "gamma x %.2f on a hit (%s), so a few recent lettings move "
                  "it by design.  estimate() reads the municipal and txdot "
                  "rows; 'all' is only its fallback."
                  % (elb.GUARD_TARGET, elb.GUARD_TARGET, "; ".join(
                      "%s %.2f / %.2f pts" % (mk, 100 * gm * (
                          1 - elb.GUARD_TARGET), 100 * gm * elb.GUARD_TARGET)
                      for mk, gm in sorted(elb.GUARD_GAMMA.items()))))
        print()
        _wrap("The %s's movement is deliberately not printed: it is a "
              "function of how the held-out lettings score, and reading it "
              "would spend the test set." % names[HOLDOUT])
        print()

    _bar("3. SCOPE DECLARATIONS  (can a reader tell what built this?)")
    miss = scope_declarations()
    if not miss:
        print("  every fitted artefact declares its scope")
    else:
        print("  %d fitted artefacts declare NOTHING about what built them:"
              % len(miss))
        for n, k in miss:
            print("     %-40s %d rows" % (n, k))
        print("  Not a leak by itself -- but nothing downstream can tell these")
        print("  are train-only without reading the producing script.")
    print()

    after = _snapshot()
    changed = sorted(n for n in set(before) | set(after)
                     if before.get(n) != after.get(n))
    _bar("VERDICT  (%.0f s)" % (time.time() - t0))
    for lab in TEST_LABELS:
        print("  %-48s %s" % ("%s (source_set '%s'):" % (names[lab], lab),
                              status[lab]))
    print("  %-48s %s" % ("producing code on data/clean as it stands:",
                          status[AS_IS]))
    for lab in TEST_LABELS:
        print("  %-48s %s" % ("V2+F1+F2 in-process fits, %s:" % NOUN[lab],
                              status.get("v2f1f2", {}).get(lab, "not run")))
    print()
    if changed:
        print("UNDETERMINED: data/clean changed while the guard ran (%s%s), so "
              "the arms compared different data -- run it again."
              % (", ".join(changed[:5]),
                 " and %d more" % (len(changed) - 5) if len(changed) > 5
                 else ""))
        return 2
    if failed:
        print("FAILED: test data reached a fitted artefact -- see above.")
        return 1
    print("PASSED: no fitted artefact matched a test-set-admitted "
          "recomputation in preference to the external-only one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
