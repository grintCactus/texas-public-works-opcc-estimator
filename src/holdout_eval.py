"""The LOCKED public test set, scored exactly as a NEW letting is scored in production.

THE ONE RULE.  The locked set (data/test_holdout/public_holdout.csv, read by
src/public_holdout.py) may only CONFIRM a decision that was registered before
it is opened, and every opening is logged.  So the real set is scored only with

    --confirm "<what decision this confirms, and where it was registered>"

The text must name an existing file (the registration).  A row is appended to
data/test_holdout/access_log.csv BEFORE anything is scored, and a second row
with the headline (or the error) when the run ends; the tables are printed
only after that row is written.  Data still in the pre-rebuild state (held-out
rows tagged "external") is refused unless --allow-pre-rebuild, because the
published shape table and vocabulary of that state were fitted with the
held-out files in them; the flag is written into the log.
Everything else -- development, testing, "just trying the script" -- runs on a
DECOY:

    --decoy SEED   60 file groups of NON-held-out public municipal lettings,
                   12 per year 2022-26, drawn the way public_holdout.select()
                   drew the real set (from the lettings production scores
                   forward; half erosion/drainage where the year still has
                   them -- the real draw took most, so a decoy gets ~11), and
                   held out in process.  The locked set's rows are dropped as
                   they are read; nothing about them is computed or printed,
                   and no log line is written.
    --selftest     on the decoy (about 2 minutes; writes only a temp dir):
                   0-1 the locked set is never read, decoy and locked set share
                       no file;
                   2   instrumented: every reference row, every median's
                       lettings, the index and discount prefixes (and a
                       refit from scratch at each test date) and each test
                       constant (against _not_under_guard / _centre) hold
                       only training lettings dated strictly before the
                       pricing date; 3 negative controls prove it can fail;
                   4   flip: training lettings up to the first decoy date are
                       unchanged by the hold-out, later ones move;
                   5-6 on a simulated rebuild (locked rows tagged
                       public_test): the pool, dates, discount and index
                       inputs equal production's readers, and with an EMPTY
                       test set the training side reproduces
                       calibrate_forward's municipal forward records and
                       constants exactly;
                   7   the post-rebuild reading (decoy rows tagged
                       public_test) gives identical scores;
                   8   the --confirm path end to end with the decoy standing
                       in for the locked set (registry and log in the temp
                       dir), and its refusals, which could not score even if
                       broken;
                   9   data/clean and the real access log are untouched.

HOW A HELD-OUT LETTING IS SCORED (the real set and the decoy alike)
  Training = public source_set "external" lettings outside the held-out
  files.  The unit held out is the SOURCE FILE with every copy of it, so a
  held-out file never reaches the reference cells, the escalation index, the
  competition discount or the constants.  Before the rebuild the held-out rows
  still say "external" and are dropped by public_holdout.is_holdout; after it
  they say "public_test" and are dropped by source_set as well.
  1. Tables at the test letting's date D: estimate_low_bid's own forward
     machinery -- reference cells (_reference_cells, _FwdKind), escalation
     (_ForwardIndex), discount (_ForwardDiscount's tables) -- fitted on
     training lettings dated strictly before D (ForwardTables), with the
     held-out rows removed before anything is fitted.
  2. The letting itself: built from the panel line for line as
     calibrate_forward builds one -- one identified winner, >= 2 bidders,
     >= 3 schedule items, >= 90% of items priced, owner-set money at face
     value -- and priced by estimate() on those tables.
  3. Constants: every training municipal letting is priced forward the same
     way.  The centring is _centre() over those dated before D; the P80
     divisor is the alpha-quantile of their centred ratios, alpha being the
     not-under guard's level at D, replayed exactly as _not_under_guard()
     does over training lettings only.  A test letting never moves the guard
     and never enters another test letting's constants.  As in the shipped
     replay, a letting with fewer than FORWARD_MIN_PRIOR earlier training
     lettings gets no constants and is not scored.
  P50 = (bid part x centring + owner-set money) / actual low;  P80 = P50 / divisor.
  Reported: n, P50 median ratio, P50 median |error|, mean |log P50|, P80
  under-share, P80/low in [1.00, 1.13], > 1.13, median overshoot, each with a
  90% bootstrap interval (resampling file groups), overall, by year and
  erosion/drainage vs other.

One known non-forward input, as in production's calibrate_forward: the
quantity-shape table (price_by_quantity_band_family.csv) is the published one.
The run reports how many test items it touched.

COVERAGE SCOPE (estimate_low_bid.COVERAGE_SCOPE, user decision 2026-11-20).
  Lines src/coverage.py calls "not covered" are not priced; a letting is scored on
  its covered part: P50 against the low bid minus the low bidder's own
  amounts on those lines, the 90%-priced rule over the covered lines, the
  constants fitted on covered training records, and a letting estimate()
  calls not_covered is not scored ("not covered as a whole").  A sensitivity line
  deducts each uncovered line's median bidder amount instead (constants
  refitted on those actuals).  --full-scope scores the old way (every line
  priced, the whole low bid).

PAIRED MODE  --compare-switch NAME  (NAME = any estimate_low_bid.pricing_rule
  switch, or the preset round10_core = round 10's MC + E + F1P + T2 + D, all
  five OFF in production until Registration 5 confirms them).  B = the switch
  OFF, the variant = the switch ON, every other switch as production runs
  it; both scored on the SAME held-out lettings (the decoy, or with
  --confirm the locked set) and compared paired: each metric with its own
  90% interval and the difference (variant - B) with a 90% interval from
  resampling file groups; overall, erosion/drainage, other work, and by
  year (the output of the retired p50fix/confirm/v2d1w36_eval.py).  With
  --decoy it is the rehearsal (no log line); with --confirm it logs before
  and after scoring exactly as a single --confirm does.

Usage:
    .venv/bin/python src/holdout_eval.py --selftest
    .venv/bin/python src/holdout_eval.py --decoy 7
    .venv/bin/python src/holdout_eval.py --decoy 7 --compare-switch round10_core
    .venv/bin/python src/holdout_eval.py --confirm "confirm <decision> (registered in reports/<file>.md)"
    .venv/bin/python src/holdout_eval.py --compare-switch round10_core --confirm "confirm <decision> (registered in ...)"

Some strings below stay in Chinese on purpose, because changing them would
change what the script does: the access-log action texts and the "(running)"
placeholder (previous_openings() finds the earlier openings in the log by
them, and the self-test checks them), the three split labels "all",
"erosion/drainage" and "other work" (dict keys, and the seeds of the
bootstrap draws, so renaming them would move every interval), and the Audit
counter keys (dict keys).  Each is commented in English where it appears.
"""
import argparse
import bisect
import collections
import contextlib
import csv
import datetime
import hashlib
import inspect
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import types
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import estimate_low_bid as e  # noqa: E402
import public_holdout as ph  # noqa: E402

REAL_CLEAN = e.CLEAN
SCRATCH = os.path.join(ROOT, "handoff", "scratchpad", "holdout_eval")
MARKET = "municipal"
YEARS = ph.YEARS                    # 2022 .. 2026
# The decoy copies the locked set's design (version 2): as many file groups
# in every year x work-type stratum as the locked set holds there.  Version 1
# (12 a year, half erosion/drainage) was retired before use -- see
# data/test_holdout/REGISTRATIONS.md, addendum of 2026-11-19 21:55.
ED_TYPES = ph.ED_TYPES
BAND_HI = 1.13                      # the user's band: P80 / low bid in [1.00, 1.13]
FLOOR = e.NOT_UNDER_FLOOR           # the user's floor: P80 under the low bid on <= 20%
BOOT_N = 2000
BOOT_SEED = 20261120
CI_LEVEL = 0.90
SELFTEST_SEED = 1
# every data/clean file the forward path reads (a simulated rebuild copies these)
INPUTS = ("master_panel.csv", "municipal_letting_totals.csv",
          "txdot_archive_project_totals.csv", "txdot_letting_spread.csv",
          "price_reference.csv", "price_reference_tokens.csv",
          "price_reference_band.csv", "price_by_quantity_band_family.csv",
          "index_by_market.csv", "price_time_index.csv",
          "competition_discount_pooled.csv", "competition_discount_municipal.csv",
          "qty_shape_borrow_sigma.csv", "estimator_calibration.csv",
          "txdot_project_totals.csv")

_T0 = time.time()


def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("  [%4.0fs] %s" % (time.time() - _T0, msg))


def _file_of(pk):
    return (pk or "").replace("\\", "/").split("::")[0]


@contextlib.contextmanager
def clean_dir(path):
    """Point estimate_low_bid (and this script) at another data/clean."""
    old = e.CLEAN
    e.CLEAN = path
    try:
        yield
    finally:
        e.CLEAN = old


def fingerprint(clean=None):
    clean = clean or e.CLEAN
    out = {}
    for n in INPUTS:
        p = os.path.join(clean, n)
        if os.path.exists(p):
            st = os.stat(p)
            out[n] = (st.st_size, st.st_mtime_ns)
    return out


# ---------------------------------------------------------------------------
# the panel, read once
class Panel:
    """master_panel.csv in one streamed pass: calibrate_forward's price pool
    (_reference_pool's rows and filters, external AND public_test rows), every
    key's availability date (_avail_date over all its rows, as _letting_dates),
    its first work type, and which keys belong to the locked set.

    A key is "real" (the locked set) when public_holdout lists its file or its
    rows say source_set == "public_test"; "decoy" when its file is in
    `decoy_files` (a public_test decoy row only exists in a simulated rebuild);
    else "train".  A registry file is always real, whatever a decoy says.
    Real rows are dropped as they are read unless read_real (--confirm only).
    """

    def __init__(self, read_real=False, decoy_files=()):
        self.read_real = read_real
        self.decoy_files = frozenset(decoy_files)
        csv.field_size_limit(10 ** 8)
        seen = {}
        intern = lambda x: seen.setdefault(x, x)  # noqa: E731
        dates = collections.defaultdict(set)
        precs = collections.defaultdict(set)
        self.pool, self.work, self.ss = [], {}, {}
        self.sizes = []                              # size_in, aligned with pool (F1)
        self.real = set()
        self.real_rows = collections.Counter()      # locked-set rows by source_set
        self.stray_public_test = set()               # public_test keys no registry file lists
        self.mixed = set()                           # keys with two source_set values
        role = {}
        with open(os.path.join(e.CLEAN, "master_panel.csv"), encoding="utf-8-sig",
                  newline="") as f:
            for r in csv.DictReader(f):
                ss = r.get("source_set")
                if ss not in ("external", "public_test"):
                    continue
                pk = r.get("project_key")
                ro = role.get(pk)
                if ro is None:
                    ro = role[pk] = self._role(pk, ss)
                    self.ss[pk] = ss
                    if ro == "real" and not ph.is_holdout(pk):
                        self.stray_public_test.add(pk)
                elif self.ss[pk] != ss:
                    self.mixed.add(pk)
                if ro == "real":
                    self.real.add(pk)
                    self.real_rows[ss] += 1
                    if not read_real:
                        continue
                pk = intern(pk)
                dates[pk].add(r.get("bid_date"))
                precs[pk].add(r.get("date_precision"))
                if pk not in self.work:
                    self.work[pk] = r.get("work_type") or ""
                # _reference_pool's filters, in its order
                if (r.get("is_engineer_estimate") or "").lower() in ("true", "1", "yes"):
                    continue
                if r.get("competition_class") != "competitive":
                    continue
                if r.get("project_kind") != "construction":
                    continue
                if r.get("price_basis") == "uneven_proposal":
                    continue
                u = e._f(r.get("unit_price"))
                if not u or u <= 0:
                    continue
                self.pool.append((
                    "municipal" if r.get("source") in e.MUNICIPAL_SOURCES else "txdot",
                    pk, intern(r.get("item_family") or ""), intern(r.get("unit") or ""),
                    intern(r.get("size_bucket") or ""), e._f(r.get("quantity")), u,
                    r.get("is_low_bidder") == "True",
                    intern(r.get("bidder_norm") or r.get("bidder")),
                    intern((r.get("desc_norm") or "").strip()),
                    intern(r.get("owner") or ""), intern(str(r.get("year") or ""))))
                self.sizes.append(e._f(r.get("size_in")))
        self.role = role
        self.avail = {pk: e._avail_date(dates[pk], precs[pk]) for pk in dates}

    def _role(self, pk, ss):
        if ph.is_holdout(pk):
            return "real"
        if _file_of(pk) in self.decoy_files:
            return "decoy"
        if ss == "public_test":
            return "real"
        return "train"


def make_excluded(panel, extra_files=()):
    """Is this key held out of training?  The locked set always; the extra
    (decoy) files when given.  Used on the pool, the index's stage-1 rows,
    the discount's letting totals and the constants alike."""
    files = frozenset(extra_files)
    real = panel.real
    memo = {}

    def excluded(pk):
        v = memo.get(pk)
        if v is None:
            v = memo[pk] = bool(pk in real or ph.is_holdout(pk) or _file_of(pk) in files)
        return v
    return excluded


# ---------------------------------------------------------------------------
# training-only versions of the forward index and discount
class TrainIndex(e._ForwardIndex):
    """estimate_low_bid._ForwardIndex with the held-out lettings' stage-1 rows
    removed.  Stage 1 is a per-(item, year, PROJECT) median, so removing a
    project's stage-1 rows is removing its panel rows; at(D) is the
    production method on what is left."""

    def __init__(self, base, excluded):
        self.np = base.np
        self.S1, self.SD, self.removed = {}, {}, {}
        for arm in self.ARMS:
            s1 = base.S1[arm]
            bad = [pk for pk in s1.project_key.unique() if excluded(pk)]
            keep = ~s1.project_key.isin(bad)
            f = s1[keep].reset_index(drop=True)
            self.S1[arm], self.SD[arm] = f, f.D.values
            self.removed[arm] = set(bad)
        self._sig, self._res = None, None


class TrainDiscount(e._ForwardDiscount):
    """estimate_low_bid._ForwardDiscount with the held-out lettings removed
    from the municipal letting totals.  __init__ reads the same three files the
    same way (--selftest checks it against the production class); it keeps
    each municipal row's letting id so the removal can be audited.  at() and
    _fit() are the production methods."""

    def __init__(self, avail, excluded):
        import numpy as np
        import pandas as pd
        self.np = np
        lr = []
        a = pd.read_csv(os.path.join(e.CLEAN, "txdot_archive_project_totals.csv"),
                        encoding="utf-8-sig")
        for r in a.itertuples(index=False):
            b = e._parse_totals(r.bidder_totals)
            if len(b) < 1:
                continue
            lr.append(("archive", str(r.bid_date)[:10], len(b), min(b),
                       float(np.median(b)), ""))
        c = pd.read_csv(os.path.join(e.CLEAN, "txdot_letting_spread.csv"),
                        encoding="utf-8-sig")
        for r in c.itertuples(index=False):
            lr.append(("current", str(r.bid_date)[:10], int(r.n_bidders),
                       float(r.low), float(r.median), ""))
        mm = pd.read_csv(os.path.join(e.CLEAN, "municipal_letting_totals.csv"),
                         encoding="utf-8-sig", dtype={"year": str, "source_set": str})
        mm = mm[mm.source_set == "external"]
        mm = mm[(mm.low > 0) & (mm["median"] > 0) & (mm.n_bidders >= 2)]
        self.removed = []                          # (letting id, its date)
        for r in mm.itertuples(index=False):
            lid = str(r.letting_id)
            d = avail.get(r.letting_id)
            if d is None:
                d = (str(r.bid_date)[:10] if isinstance(r.bid_date, str)
                     and len(str(r.bid_date)) >= 10 else e.NEVER)
            if excluded(lid) or excluded(str(r.src_file)):
                self.removed.append((lid, d))
                continue
            lr.append(("municipal", d, int(r.n_bidders), float(r.low),
                       float(getattr(r, "median")), lid))
        ld = pd.DataFrame(lr, columns=["src", "D", "n_bidders", "low", "median",
                                       "letting_id"])
        ld["D"] = ld.D.where(ld.D.str.match(r"^\d{4}-\d\d-\d\d$"), e.NEVER)
        ld["lom"] = ld.low / ld["median"]
        self.LD = ld.sort_values("D", kind="mergesort").reset_index(drop=True)
        self.LDD = self.LD.D.values
        self._k, self._res = None, None


# ---------------------------------------------------------------------------
# lettings, built as calibrate_forward builds them
def build_lettings(pool, keep):
    lets = collections.defaultdict(
        lambda: {"bt": collections.defaultdict(float), "win": set(),
                 "sched": {}, "mk": "", "rows": collections.defaultdict(list),
                 "prices": collections.defaultdict(list), "wamt": {},
                 "yr": collections.Counter(), "bamt": {},
                 "nb": collections.defaultdict(set)})
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in pool:
        if q is None or q <= 0:
            continue
        if not keep(pk):
            continue
        d = lets[pk]
        d["mk"] = mk
        d["bt"][b] += q * u
        k = (fam, un, sz, e.qty_shape.band_of(q), dn)
        d["prices"][k].append(u)
        if e.IDENTICAL_BID_OWNER_SET:
            d["nb"][k].add(b)                  # round 10 D: distinct bidders
        if e.COVERAGE_SCOPE and e.line_coverage(dn, fam, un):
            ba = d["bamt"].setdefault(k, collections.defaultdict(float))
            ba[b] += q * u                 # every bidder's amount (sensitivity)
        if w:
            d["win"].add(b)
            d["sched"][k] = d["sched"].get(k, 0.0) + q
            d["rows"][k].append(q)
            d["wamt"][k] = d["wamt"].get(k, 0.0) + q * u
            d["yr"][yr] += 1
    return lets


def population_reason(d):
    """calibrate_forward's population filters: None when the letting is in."""
    if len(d["win"]) != 1 or len(d["bt"]) < 2 or not d["sched"]:
        return "not_one_winner_or_under_2_bidders"
    wt = d["bt"].get(next(iter(d["win"])), 0.0)
    if wt <= 0 or statistics.median(d["bt"].values()) <= 0:
        return "no_positive_total"
    if len(d["sched"]) < e.MIN_SCHEDULE_ITEMS:
        return "under_%d_items" % e.MIN_SCHEDULE_ITEMS
    return None


def schedule_of(d, pk=None, lsz=None):
    """calibrate_forward's items for one letting (owner-set money at face
    value -- estimate_low_bid.schedule_owner_set, round 10's D included when
    its switch is on), each with its schedule key and its own size (F1)."""
    items, keys = [], []
    lsz = lsz or {}
    for k in d["sched"]:
        fam, un, sz, bd, dn = k
        fx_k, by_d = e.schedule_owner_set(d, k)
        if fx_k:
            items.append({"family": fam, "unit": un, "size_bucket": sz,
                          "quantity": d["sched"][k], "description": dn,
                          "amount": d["wamt"].get(k, 0.0), "key": k})
            if by_d:
                items[-1]["identical_bids"] = True
            keys.append(k)
            continue
        for qr in d["rows"][k]:
            items.append({"family": fam, "unit": un, "size_bucket": sz,
                          "quantity": qr, "description": dn, "key": k,
                          "size_in": lsz.get((pk,) + k)})
            keys.append(k)
    return items, keys


def price_forward(pks, lets, avail, tab, kinds, tok_inv, findex, fdisc, cov, audit=None,
                  lsz=None, size_cells=None, pools=None, targets=None, muni_kinds=None):
    """estimate() on ForwardTables at each letting's own date, calibrate_forward's
    loop line for line.  Returns {key: record} for the lettings that pass.
    lsz / size_cells: F1's line sizes and size cells; pools: V2's comparables
    pools; targets: a held-out letting's own owner and work type; muni_kinds:
    MC's municipal-only cells (training rows only)."""
    out = {}
    ft_d, ft = None, None
    for pk in sorted(pks, key=lambda k: (avail.get(k, e.NEVER), k)):
        D = avail.get(pk, e.NEVER)
        if D == e.NEVER:
            cov[pk] = "no_date"
            continue
        d = lets[pk]
        mk, n, sched = d["mk"], len(d["bt"]), d["sched"]
        wt = d["bt"][next(iter(d["win"]))]
        items, keys = schedule_of(d, pk, lsz)
        yr = d["yr"].most_common(1)[0][0] if d["yr"] else ""
        if audit is not None:
            audit.cur_D = D
        if D != ft_d:
            pooled, muni = fdisc.at(D)
            ft_d, ft = D, e.ForwardTables(tab, kinds, tok_inv, D, findex.at(D),
                                          pooled, muni, size_cells, pools,
                                          muni_kinds=muni_kinds)
        tgt = (targets or {}).get(pk) or {"key": pk}
        r = e.estimate(ft, items, n, mk, yr or None, target=tgt)
        if r.get("not_covered"):
            cov[pk] = "not_covered"
            continue
        if r.get("expected_low") is None:
            cov[pk] = "no_estimate"
            continue
        ok = collections.defaultdict(lambda: True)
        unc = set()
        for k, t in zip(keys, r["trace"]):
            if t.get("uncovered"):
                unc.add(k)
                continue
            if not (t.get("fixed_amount") or t.get("price")):
                ok[k] = False
        ck = [k for k in sched if k not in unc]          # the covered lines
        pr = sum(1 for k in ck if ok[k])
        tot, fixed = r["market_total"], r["fixed_total"]
        if pr < 0.9 * len(ck) or tot <= 0:
            cov[pk] = "under_90pct_priced"
            continue
        cv = e.covered_actual(d, unc, wt)
        if cv is None:
            cov[pk] = "no_covered_low"
            continue
        fa = r["discount"]["f"]
        cov[pk] = "scored"
        cm = r.get("comparables") or {}
        if audit is not None:
            audit.priced(pk, D, r, size_cells)
        out[pk] = {"project_key": pk, "market": mk, "date": D, "n_bidders": n,
                   "bid_part": r["bid_part"], "owner_fixed": fixed,
                   "actual_low": cv["actual_low"],
                   "n_items": len(sched), "bid_part_lines": tot * fa,
                   "cmp": cm.get("cmp"), "n_comps": cm.get("n_comps"),
                   "shape_items": sum(1 for t in r["trace"] if t.get("qty_factor")),
                   "shape_dollars": sum((t.get("price") or 0) * (t.get("quantity") or 0)
                                        for t in r["trace"] if t.get("qty_factor"))}
        if e.COVERAGE_SCOPE:
            out[pk].update(cv)
    if audit is not None:
        audit.cur_D = None
    return out


# ---------------------------------------------------------------------------
# the constants a new letting dated D gets
class Constants:
    """Centring and P80 divisor for a letting dated D, from the training
    lettings priced forward before D: the guard is walked once, exactly as
    _not_under_guard() walks it, remembering its level before each date."""

    def __init__(self, recs, gamma):
        self.trip = [(r["date"], r["bid_part"], r["owner_fixed"], r["actual_low"],
                      r["project_key"]) for r in sorted(recs, key=lambda r: (r["date"],
                                                                             r["project_key"]))]
        self.dates = [t[0] for t in self.trip]
        self.gdates, self.galpha = [], []          # level in force when each date opens
        alpha, i, trip = e.GUARD_TARGET, 0, self.trip
        while i < len(trip):
            D = trip[i][0]
            j = i
            while j < len(trip) and trip[j][0] == D:
                j += 1
            self.gdates.append(D)
            self.galpha.append(alpha)
            if i >= e.FORWARD_MIN_PRIOR:
                prior = [(c, fx, w) for _, c, fx, w, _ in trip[:i]]
                lc = e._centre(prior)
                rs = sorted((c * lc + fx) / w for c, fx, w in prior)
                p = rs[int(alpha * (len(rs) - 1))]
                errs = [(c * lc + fx) / w / p < 1.0 for _, c, fx, w, _ in trip[i:j]]
                alpha = e._guard_step(alpha, errs, gamma, e.GUARD_TARGET)
            i = j
        self.final_alpha = alpha
        self._memo = {}

    def at(self, D):
        if D in self._memo:
            return self._memo[D]
        i = bisect.bisect_left(self.dates, D)          # lettings strictly before D
        g = bisect.bisect_left(self.gdates, D)
        alpha = self.galpha[g] if g < len(self.gdates) else self.final_alpha
        out = {"n_prior": i, "alpha": alpha, "lc": None, "p": None,
               "prior_keys": [t[4] for t in self.trip[:i]],
               "prior_last_date": self.trip[i - 1][0] if i else None}
        if i >= e.FORWARD_MIN_PRIOR:
            prior = [(c, fx, w) for _, c, fx, w, _ in self.trip[:i]]
            lc = e._centre(prior)
            rs = sorted((c * lc + fx) / w for c, fx, w in prior)
            out["lc"], out["p"] = lc, rs[int(alpha * (len(rs) - 1))]
        self._memo[D] = out
        return out


# ---------------------------------------------------------------------------
# one evaluation: training tables, training records, constants, test scores
def evaluate(panel, tab, base_index, excluded, test_keys, meta, audit=None):
    """Price every training municipal letting forward (for the constants) and
    every test letting (to score), on tables built from training rows only."""
    avail = panel.avail
    out = {"cov_train": collections.Counter(), "cov_test": collections.Counter()}
    keep = [i for i, r in enumerate(panel.pool) if not excluded(r[1])]
    train_pool = [panel.pool[i] for i in keep]
    train_sizes = ([panel.sizes[i] for i in keep] if e.SIZE_FALLBACK else None)
    out["train_pool_rows"] = len(train_pool)
    step("training reference cells: %d price rows (held-out files removed)" % len(train_pool))
    cells = e._reference_cells(train_pool, train_sizes, avail)
    # MC: municipal-only cells from the training rows only
    muni_kinds, muni_cells = e.muni_forward_kinds(train_pool, avail)
    if audit is not None and muni_cells is not None:
        muni_kinds = {kd: _AuditKindMuni(muni_cells[kd], kd, e.MUNI_CELLS_NMIN, avail, audit)
                      for kd in ("tok", "band", "fam")}
        audit.static_cells(muni_cells)
    out["muni_cells"] = muni_cells
    # V2: the comparables pool from the training rows only
    pools = ({MARKET: e.comparables.Pool(train_pool, avail, MARKET, e.CLEAN, e)}
             if e.COMPARABLES_BLEND else None)
    del train_pool, keep
    lsz = e._line_sizes(panel.pool, panel.sizes) if e.SIZE_FALLBACK else {}
    out["size_cells"], out["pools"] = cells["size"], pools
    if audit is not None:
        audit.fitted(cells["size"], pools)
    if audit is not None:
        kinds = {"tok": _AuditKind(cells["tok"], "tok", 2, avail, audit),
                 "band": _AuditKind(cells["band"], "band", 2, avail, audit),
                 "fam": _AuditKind(cells["fam"], "fam", 1, avail, audit)}
        findex = _AuditIndex(base_index, excluded, audit)
        fdisc = _AuditDiscount(avail, excluded, audit)
        audit.static_cells(cells)
    else:
        kinds = e._forward_kinds(cells, avail)
        findex = TrainIndex(base_index, excluded)
        fdisc = TrainDiscount(avail, excluded)
    out["index_removed"] = findex.removed
    out["discount_removed"] = fdisc.removed
    lets = build_lettings(panel.pool, keep=lambda pk: pk in test_keys or not excluded(pk))
    train_ids, test_ids = [], []
    for pk in sorted(lets):
        d = lets[pk]
        if pk in test_keys:
            why = "not_municipal" if d["mk"] != MARKET else population_reason(d)
            if why:
                out["cov_test"][why] += 1
            else:
                test_ids.append(pk)
        elif d["mk"] == MARKET and not excluded(pk):
            why = population_reason(d)
            if why:
                out["cov_train"][why] += 1
            else:
                train_ids.append(pk)
    for pk in test_keys:
        if pk not in lets:
            out["cov_test"]["no_priced_rows"] += 1
    step("pricing forward by date: %d training municipal lettings, %d test" % (len(train_ids), len(test_ids)))
    # a held-out schedule is not in the pool: its own owner and work type
    # (known when it is estimated) enter its similarity, nothing else
    wt_t = e.comparables.schedule_worktypes(e.CLEAN, test_ids, e._f) if pools else {}
    targets = {pk: {"key": pk, "owner": e.comparables.owner_id(pk, MARKET, ""),
                    "work_type": wt_t.get(pk, "")} for pk in test_ids}
    cov = {}
    recs = price_forward(train_ids + test_ids, lets, avail, tab, kinds, cells["tok_inv"],
                         findex, fdisc, cov, audit, lsz, cells["size"], pools, targets,
                         muni_kinds)
    for pk in train_ids:
        out["cov_train"][cov[pk]] += 1
    train_recs = sorted((recs[pk] for pk in train_ids if pk in recs),
                        key=lambda r: (r["date"], r["project_key"]))
    const = Constants(train_recs, e.GUARD_GAMMA[MARKET])
    tests = []
    for pk in test_ids:
        if cov[pk] != "scored":
            out["cov_test"][cov[pk]] += 1
            continue
        r = dict(recs[pk])
        c = const.at(r["date"])
        if c["p"] is None:
            out["cov_test"]["under_%d_prior_lettings" % e.FORWARD_MIN_PRIOR] += 1
            continue
        if audit is not None:
            audit.constants(r, c, train_recs, const)
        out["cov_test"]["scored"] += 1
        p50 = (r["bid_part"] * c["lc"] + r["owner_fixed"]) / r["actual_low"]
        g, ed = meta(pk)
        r.update({"year": r["date"][:4], "group": g, "erosion_drainage": ed,
                  "n_prior": c["n_prior"], "centring": c["lc"], "alpha": c["alpha"],
                  "divisor": c["p"], "p50": p50, "p80": p50 / c["p"]})
        tests.append(r)
    out.update({"train_recs": train_recs, "tests": tests, "const": const,
                "n_train_lettings": len(train_ids), "n_test_lettings": len(test_ids),
                "train_scored": len(train_recs)})
    out["tests_med"] = sensitivity_tests(train_recs, tests) if e.COVERAGE_SCOPE else None
    return out


def sensitivity_tests(train_recs, tests):
    """Coverage scope, sensitivity version: every actual deducts each
    uncovered line's MEDIAN bidder amount (actual_low_med) instead of the low
    bidder's own; constants refitted on the training records so deducted.
    Lettings without a positive actual_low_med drop out of both sides."""
    def med(r):
        return dict(r, actual_low=r["actual_low_med"])
    tr = [med(r) for r in train_recs if r.get("actual_low_med")]
    const = Constants(tr, e.GUARD_GAMMA[MARKET])
    out = []
    for r in tests:
        if not r.get("actual_low_med"):
            continue
        c = const.at(r["date"])
        if c["p"] is None:
            continue
        p50 = (r["bid_part"] * c["lc"] + r["owner_fixed"]) / r["actual_low_med"]
        out.append(dict(r, actual_low=r["actual_low_med"], centring=c["lc"],
                        alpha=c["alpha"], divisor=c["p"], p50=p50, p80=p50 / c["p"]))
    return out


# ---------------------------------------------------------------------------
# metrics
def metrics(recs):
    n = len(recs)
    if not n:
        return None
    p50 = [r["p50"] for r in recs]
    p80 = [r["p80"] for r in recs]
    over = [x - 1 for x in p80 if x >= 1.0]
    return {"n": n,
            "p50_ratio": statistics.median(p50),
            "p50_abs": statistics.median([abs(x - 1) for x in p50]),
            "mean_abs_log": sum(abs(math.log(x)) for x in p50) / n,
            "under": sum(x < 1.0 for x in p80) / n,
            "in_band": sum(1.0 <= x <= BAND_HI for x in p80) / n,
            "above": sum(x > BAND_HI for x in p80) / n,
            "overshoot": statistics.median(over) if over else None}


def _pct(v, q):
    v = sorted(v)
    if not v:
        return None
    x = q * (len(v) - 1)
    lo = int(math.floor(x))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (x - lo)


def bootstrap(recs, label):
    """90% percentile intervals, resampling FILE GROUPS (the unit held out)."""
    groups = collections.defaultdict(list)
    for r in recs:
        groups[r["group"]].append(r)
    gl = [groups[g] for g in sorted(groups)]
    rng = random.Random("%d:%s" % (BOOT_SEED, label))
    draws = collections.defaultdict(list)
    for _ in range(BOOT_N):
        s = []
        for _ in range(len(gl)):
            s.extend(gl[rng.randrange(len(gl))])
        m = metrics(s)
        for k, v in m.items():
            if k != "n" and v is not None:
                draws[k].append(v)
    a = (1 - CI_LEVEL) / 2
    return {k: (_pct(v, a), _pct(v, 1 - a)) for k, v in draws.items()}


def summarize(tests):
    rows = [("全部", tests)]  # split labels kept (dict keys and bootstrap seeds): "all"
    for y in YEARS:
        rows.append((str(y), [r for r in tests if r["year"] == str(y)]))
    extra = sorted({r["year"] for r in tests} - {str(y) for y in YEARS})
    for y in extra:
        rows.append((y, [r for r in tests if r["year"] == y]))
    rows.append(("侵蚀/排水", [r for r in tests if r["erosion_drainage"]]))  # "erosion/drainage"
    rows.append(("其他工程", [r for r in tests if not r["erosion_drainage"]]))  # "other work"
    out = []
    for label, rs in rows:
        m = metrics(rs)
        out.append({"group": label, "metrics": m,
                    "ci": bootstrap(rs, label) if m else {}})
    return out


COLS = (("p50_ratio", "P50 median ratio", "r"), ("p50_abs", "P50 median error", "p"),
        ("mean_abs_log", "mean log error", "r"), ("under", "P80 under low bid", "p"),
        ("in_band", "P80 in [1.00,1.13]", "p"), ("above", "P80 >1.13", "p"),
        ("overshoot", "median overshoot when over", "p"))


def _cell(v, ci, kind):
    if v is None:
        return "-"
    if kind == "p":
        s = "%.1f%%" % (100 * v)
        if ci and ci[0] is not None:
            s += " [%.1f, %.1f]" % (100 * ci[0], 100 * ci[1])
    else:
        s = "%.3f" % v
        if ci and ci[0] is not None:
            s += " [%.3f, %.3f]" % ci
    return s


def table(summary):
    lines = ["| group | n | " + " | ".join(c[1] for c in COLS) + " |",
             "|---|---|" + "---|" * len(COLS)]
    for row in summary:
        m = row["metrics"]
        if not m:
            lines.append("| %s | 0 |" % row["group"] + " - |" * len(COLS))
            continue
        lines.append("| %s | %d | " % (row["group"], m["n"]) + " | ".join(
            _cell(m[k], row["ci"].get(k), kind) for k, _, kind in COLS) + " |")
    return "\n".join(lines)


def headline(summary):
    m = summary[0]["metrics"]
    ci = summary[0]["ci"]
    if not m:
        return "no letting was scored"
    under_n = round(m["under"] * m["n"])
    return ("n=%d; P80 under the low bid %d/%d=%s; P80 in [1.00,1.13] %s; P50 median ratio %s; "
            "P50 median error %s; median overshoot when over %s" % (
                m["n"], under_n, m["n"], _cell(m["under"], ci.get("under"), "p"),
                _cell(m["in_band"], ci.get("in_band"), "p"),
                _cell(m["p50_ratio"], ci.get("p50_ratio"), "r"),
                _cell(m["p50_abs"], ci.get("p50_abs"), "p"),
                _cell(m["overshoot"], ci.get("overshoot"), "p")))


GLOSSARY = """Glossary:
  P50 median ratio   = estimate P50 / actual low bid, the median; 1.000 is dead centre.
  P50 median error   = the median of |P50 / actual low bid - 1|.
  mean log error     = the mean of |ln(P50 / actual low bid)|; smaller is more accurate.
  P80 under low bid  = share of lettings where P80 is still below the actual low bid. The user's floor: at most 20%%.
  P80 in [1.00,1.13] = share over the low bid by no more than 13%%. Higher is better.
  P80 >1.13          = share over the low bid by more than 13%%.
  median overshoot when over = among the lettings with P80 >= the low bid, the median of the excess.
  brackets = 90%% interval (file groups resampled with replacement %d times). Each year has only about ten
             lettings, so the per-year intervals are wide; when a share is exactly 0%% or 100%%,
             the resampled interval can only be 0 or 100 too, which is not certainty.""" % BOOT_N


def shape_text(tests, what):
    n = sum(r["shape_items"] for r in tests)
    d = sum(r["shape_dollars"] for r in tests)
    tot = sum(r["bid_part"] for r in tests)
    return ("The published quantity-shape table (not fitted forward by date; production's forward calibration uses it the same way) is used on %d %s schedule items, "
            "about %.2f%% of these lettings' estimated dollars." % (n, what, 100.0 * d / tot if tot else 0.0))


def coverage_text(cov):
    names = {"scored": "scored", "not_municipal": "not municipal",
             "not_one_winner_or_under_2_bidders": "no single winner or fewer than 2 bidders",
             "no_positive_total": "total not positive",
             "under_%d_items" % e.MIN_SCHEDULE_ITEMS: "fewer than %d schedule items" % e.MIN_SCHEDULE_ITEMS,
             "no_date": "no date", "no_estimate": "no estimate possible",
             "under_90pct_priced": "priceable schedule items below 90%",
             "under_%d_prior_lettings" % e.FORWARD_MIN_PRIOR:
                 "fewer than %d earlier training lettings, no constants" % e.FORWARD_MIN_PRIOR,
             "no_priced_rows": "no usable price rows in the panel",
             "not_covered": "not covered as a whole (not scored)",
             "no_covered_low": "low bid not positive after taking out the uncovered lines"}
    return ", ".join("%s %d" % (names.get(k, k), v) for k, v in
                    sorted(cov.items(), key=lambda kv: (kv[0] != "scored", kv[0])))


# ---------------------------------------------------------------------------
# the decoy
def decoy_strata():
    """(year, erosion_drainage) -> file groups the locked set holds there.
    Reads the registry's group/year/type columns only -- which files are in
    it is already known to every gate; nothing is scored."""
    c, seen = collections.Counter(), set()
    with open(ph.REG, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["group"] in seen:
                continue
            seen.add(r["group"])
            c[(int(r["year"]), r["erosion_drainage"] == "Y")] += 1
    return c


def draw_decoy(seed, panel, scored):
    """public_holdout.select()'s draw on the lettings production scores
    forward, with the locked set removed and another seed: per year 2022-26
    (the group's earliest letting) and work type (erosion/drainage or
    other), as many file groups as the locked set holds in that stratum
    (decoy_strata), fewer where the pool runs short.  Uses keys, dates and
    work types only."""
    files = collections.defaultdict(lambda: {"keys": [], "dates": [], "wt": set()})
    for r in scored:
        pk = r["project_key"]
        if ph.is_holdout(pk) or pk in panel.real:
            continue
        ent = files[_file_of(pk)]
        ent["keys"].append(pk)
        ent["dates"].append(r["date"])
        ent["wt"].add(panel.work.get(pk, ""))
    grp = ph._groups(list(files))
    units = {}
    for f, ent in files.items():
        members = grp.get(f, {f})
        g = min(members)
        u = units.setdefault(g, {"files": set(), "keys": [], "dates": [], "wt": set()})
        u["files"] |= members
        u["keys"] += ent["keys"]
        u["dates"] += ent["dates"]
        u["wt"] |= ent["wt"]
    units = {g: u for g, u in units.items()
             if not any(ph.is_holdout(f) for f in u["files"])}
    target = decoy_strata()
    chosen = []
    short = {}
    for y in YEARS:
        pool = sorted(g for g, u in units.items() if min(u["dates"])[:4] == str(y))
        rng = random.Random("holdout_eval decoy %d %d" % (seed, y))
        ed = [g for g in pool if units[g]["wt"] & ED_TYPES]
        other = [g for g in pool if g not in ed]
        rng.shuffle(ed)
        rng.shuffle(other)
        take = ed[:target[(y, True)]] + other[:target[(y, False)]]
        if len(take) < target[(y, True)] + target[(y, False)]:
            short[y] = len(take)
        chosen += [(y, g) for g in take]
    return_target = target
    groups = {g: {"year": y, "files": sorted(units[g]["files"]),
                  "erosion_drainage": bool(units[g]["wt"] & ED_TYPES),
                  "keys": sorted(units[g]["keys"])} for y, g in chosen}
    files_all = frozenset(f for u in groups.values() for f in u["files"])
    return {"seed": seed, "groups": groups, "files": files_all, "short_years": short,
            "target": dict(return_target)}


def draw_population(panel, tab, base):
    """The lettings a decoy is drawn from: those the rule in force before
    V2+F1+F2 (2026-11-20) scores forward with an empty test set.  The locked
    set itself was drawn from a fixed forward pass of that rule
    (public_holdout.FORWARD_JSON); pinning the decoys' population the same
    way keeps a seed naming the same decoy files across pricing changes, so
    decoy runs before and after a change score the same lettings (F1's
    no-sized-cell rule and F2's typical line move a few lettings in or out of
    the scored set, which would otherwise reshuffle every year's draw).
    EVERY pricing_rule switch is off here -- the coverage scope (2026-11-20)
    and any switch added later included -- so the population stays that
    rule's (T1 acts on TxDOT only and never reaches a municipal letting)."""
    off = {p: False for p in inspect.signature(e.pricing_rule).parameters}
    with e.pricing_rule(**off):
        st0 = evaluate(panel, tab, base, make_excluded(panel), set(), lambda pk: (pk, False))
    return st0["train_recs"]


def decoy_meta(decoy):
    by_file = {}
    for g, u in decoy["groups"].items():
        for f in u["files"]:
            by_file[f] = (g, u["erosion_drainage"])

    def meta(pk):
        return by_file.get(_file_of(pk), (_file_of(pk), False))
    return meta


def run_decoy_core(seed, panel, tab, base, decoy=None):
    """Stage 1: the empty test set (training = everything outside the locked
    set), whose scored lettings the decoy is drawn from.  Stage 2: the decoy
    held out and scored.  `decoy` skips the draw (a simulated rebuild reuses
    the same decoy)."""
    st1 = None
    if decoy is None:
        step("stage 1: empty test set, pricing every training letting forward (for drawing the decoy)")
        st1 = evaluate(panel, tab, base, make_excluded(panel), set(), lambda pk: (pk, False))
        step("the pool the decoy is drawn from: training lettings scored forward by the rule in force before V2+F1+F2")
        decoy = draw_decoy(seed, panel, draw_population(panel, tab, base))
    excluded = make_excluded(panel, decoy["files"])
    test_keys = {pk for pk in panel.avail if _file_of(pk) in decoy["files"]}
    step("stage 2: decoy of %d groups, %d files held out; training tables rebuilt and scored"
         % (len(decoy["groups"]), len(decoy["files"])))
    st2 = evaluate(panel, tab, base, excluded, test_keys, decoy_meta(decoy))
    return st1, decoy, st2


def run_decoy(seed, json_path=None):
    say("[REHEARSAL - decoy set, seed %d] This is not the locked test set; nothing is written to the access log." % seed)
    say(scope_text())
    fp0 = fingerprint()
    step("reading master_panel (locked test set rows are dropped as they are read)")
    panel = Panel()
    tab = e.Tables()
    step("building the dated price index (held-out projects removed after the first grouping)")
    base = e._ForwardIndex(panel.avail)
    st1, decoy, st2 = run_decoy_core(seed, panel, tab, base)
    summary = summarize(st2["tests"])
    changed = fingerprint() != fp0
    say()
    say("=== Rehearsal result (decoy set, seed %d; not the test set) ===" % seed)
    n_ed = sum(u["erosion_drainage"] for u in decoy["groups"].values())
    say("Decoy set: %d file groups (as many in every year x work-type cell as the locked set), %d files; "
        "whole groups stay out of training." % (len(decoy["groups"]), len(decoy["files"])))
    say("Of them erosion/drainage: %d groups (by year %s)." % (n_ed, dict(sorted(collections.Counter(
        u["year"] for u in decoy["groups"].values() if u["erosion_drainage"]).items()))))
    if decoy["short_years"]:
        say("Note: these years have too few groups left; actually drawn per year: %s" % decoy["short_years"])
    say("Training: %d municipal lettings scored forward (%d with no decoy held out)."
        % (st2["train_scored"], st1["train_scored"]))
    say("Decoy lettings: %s." % coverage_text(st2["cov_test"]))
    if e.COVERAGE_SCOPE:
        say("Coverage scope: %s." % scope_counts(st2["tests"], st2["cov_test"]))
    say(shape_text(st2["tests"], "decoy"))
    say()
    say(table(summary))
    say()
    say("Headline (decoy set): " + headline(summary))
    if sensitivity_line(st2):
        say(sensitivity_line(st2))
    say()
    say(GLOSSARY)
    if changed:
        say("\nWARNING: data/clean input files changed during the run (someone may be rebuilding); this result cannot be trusted.")
    json_path = json_path or os.path.join(SCRATCH, "decoy_%d.json" % seed)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "decoy", "seed": seed,
                   "decoy": {"groups": decoy["groups"], "files": sorted(decoy["files"])},
                   "coverage_test": dict(st2["cov_test"]),
                   "coverage_train": dict(st2["cov_train"]),
                   "train_scored": st2["train_scored"],
                   "summary": summary, "per_letting": st2["tests"],
                   "coverage_scope": bool(e.COVERAGE_SCOPE),
                   "sensitivity_per_letting": st2.get("tests_med"),
                   "data_changed_during_run": changed}, f, ensure_ascii=False, indent=1,
                  default=str)
    say("\nPer-letting decoy results written to %s" % os.path.relpath(json_path, ROOT))
    return 0 if not changed else 1


# ---------------------------------------------------------------------------
# paired mode: B = one pricing_rule switch OFF, the variant = it ON
def switch_names():
    """The switches estimate_low_bid.pricing_rule() takes."""
    return list(inspect.signature(e.pricing_rule).parameters)


def rule_kwargs(switch=None, on=True, full_scope=False):
    """pricing_rule() keywords: the compared switch set, the coverage scope
    off with --full-scope; every other switch as production runs it."""
    kw = {}
    if full_scope:
        kw["coverage_scope"] = False
    if switch is not None:
        kw[switch] = on
    return kw


def scope_text(full_scope=False):
    if full_scope or not e.COVERAGE_SCOPE:
        return "Scoring scope: every line, the whole low bid (the old scope, --full-scope)"
    return ("Scoring scope: the covered part -- uncovered lines are not priced; actual = low bid - the low bidder's own amounts on the uncovered lines; "
            "lettings not covered as a whole are not scored")


def scope_counts(tests, cov):
    n_u = sum(1 for r in tests if r.get("n_uncovered_lines"))
    return "%d scored as usual despite uncovered lines, %d not scored (not covered as a whole)" % (n_u, cov.get("not_covered", 0))


def mult(recs):
    return statistics.median([r["p80"] / r["p50"] for r in recs]) if recs else None


# judged first (P80 under, median overshoot), then the reported metrics
PMETRICS = (("under", "P80 under low bid", "p"), ("overshoot", "median overshoot when over", "p"),
            ("mean_abs_log", "mean \\|ln\\|", "r"), ("p50_abs", "P50 median error", "p"),
            ("in_band", "P80 in [1.00,1.13]", "p"), ("mult", "median P80 multiplier", "r"),
            ("p50_ratio", "P50 median ratio", "r"))
SPLITS = (("全部", lambda r: True), ("侵蚀/排水", lambda r: r["erosion_drainage"]),  # labels kept (dict keys, bootstrap seeds): "all", "erosion/drainage",
          ("其他工程", lambda r: not r["erosion_drainage"]))  # "other work"


def _file_groups(recs):
    g = collections.defaultdict(list)
    for r in recs:
        g[r["group"]].append(r)
    return [g[k] for k in sorted(g)]


def level_ci(recs, label):
    """One method's own 90% intervals: bootstrap() (file groups, its seed and
    label: B's equal what a single run prints), plus the P80 multiplier."""
    ci = dict(bootstrap(recs, label))
    gl = _file_groups(recs)
    rng = random.Random("%d:mult:%s" % (BOOT_SEED, label))
    draws = []
    for _ in range(BOOT_N):
        s = []
        for _ in range(len(gl)):
            s.extend(gl[rng.randrange(len(gl))])
        draws.append(mult(s))
    a = (1 - CI_LEVEL) / 2
    ci["mult"] = (_pct(draws, a), _pct(draws, 1 - a))
    return ci


def paired(b_tests, v_tests, label):
    """variant - B on the same lettings; 90% intervals resampling FILE GROUPS
    (pairs stay together)."""
    bv = {r["project_key"]: r for r in b_tests}
    pairs = [(bv[r["project_key"]], r) for r in v_tests]
    groups = collections.defaultdict(list)
    for b, v in pairs:
        groups[b["group"]].append((b, v))
    gl = [groups[g] for g in sorted(groups)]

    def stat(ps):
        mb, mv = metrics([b for b, _ in ps]), metrics([v for _, v in ps])
        d = {k: mv[k] - mb[k] for k in ("under", "in_band", "mean_abs_log", "p50_abs",
                                          "p50_ratio")}
        d["mult"] = mult([v for _, v in ps]) - mult([b for b, _ in ps])
        d["overshoot"] = (mv["overshoot"] - mb["overshoot"]
                          if mv["overshoot"] is not None and mb["overshoot"] is not None
                          else None)
        return d
    point = stat(pairs)
    rng = random.Random("%d:paired:%s" % (BOOT_SEED, label))
    draws = collections.defaultdict(list)
    for _ in range(BOOT_N):
        s = []
        for _ in range(len(gl)):
            s.extend(gl[rng.randrange(len(gl))])
        for k, v in stat(s).items():
            if v is not None:
                draws[k].append(v)
    a = (1 - CI_LEVEL) / 2
    return {k: {"diff": point[k], "lo": _pct(draws[k], a), "hi": _pct(draws[k], 1 - a)}
            for k in point}


def side_by_side(b_tests, v_tests, label="全部"):  # "all" split label kept (dict key, bootstrap seed)
    if not b_tests:
        return None
    mb, mv = metrics(b_tests), metrics(v_tests)
    mb["mult"], mv["mult"] = mult(b_tests), mult(v_tests)
    return {"label": label, "n": mb["n"], "B": mb, "V": mv,
            "ci_B": level_ci(b_tests, label), "ci_V": level_ci(v_tests, label),
            "paired": paired(b_tests, v_tests, label)}


def splits(b_tests, v_tests):
    """All, erosion/drainage, other: B and the variant on the lettings BOTH
    score (paired); lettings only one of them scores are counted."""
    vv = {r["project_key"]: r for r in v_tests}
    bk = {r["project_key"] for r in b_tests}
    common = [r for r in b_tests if r["project_key"] in vv]
    out = {}
    for label, keep in SPLITS:
        bt = [r for r in common if keep(r)]
        out[label] = side_by_side(bt, [vv[r["project_key"]] for r in bt], label)
    out["_sets"] = {"B": len(bk), "V": len(vv), "both": len(common),
                    "only_B": sorted(bk - set(vv)), "only_V": sorted(set(vv) - bk)}
    return out


def _fmt(v, kind, ci=None):
    if v is None:
        return "-"
    if kind == "p":
        s = "%.1f%%" % (100 * v)
        if ci and ci[0] is not None:
            s += " [%.1f, %.1f]" % (100 * ci[0], 100 * ci[1])
    else:
        s = "%.3f" % v
        if ci and ci[0] is not None:
            s += " [%.3f, %.3f]" % (ci[0], ci[1])
    return s


def ptable(sbs, vname):
    if sbs is None:
        return "(no letting in this group was scored)"
    L = ["| metric | B (%s off) [90%% interval] | variant (%s on) [90%% interval] | diff (variant - B) | 90%% interval of the diff |"
         % (vname, vname), "|---|---|---|---|---|"]
    for k, name, kind in PMETRICS:
        d = sbs["paired"].get(k)
        b = _fmt(sbs["B"].get(k), kind, sbs["ci_B"].get(k))
        v = _fmt(sbs["V"].get(k), kind, sbs["ci_V"].get(k))
        if not d or d["diff"] is None or d["lo"] is None:
            L.append("| %s | %s | %s | - | - |" % (name, b, v))
        elif kind == "p":
            L.append("| %s | %s | %s | %+.1f pts | %+.1f ~ %+.1f pts |" % (
                name, b, v, 100 * d["diff"], 100 * d["lo"], 100 * d["hi"]))
        else:
            L.append("| %s | %s | %s | %+.3f | %+.3f ~ %+.3f |" % (
                name, b, v, d["diff"], d["lo"], d["hi"]))
    L.append("(%s, n=%d; the brackets and the diff interval are 90%% intervals: file groups resampled with replacement %d times, paired)"
             % (sbs.get("label", "全部"), sbs["n"], BOOT_N))  # "all" split label kept
    return "\n".join(L)


def pheadline(sbs, vname):
    if sbs is None:
        return "no letting was scored by both"
    b, v, p = sbs["B"], sbs["V"], sbs["paired"]
    n = sbs["n"]

    def pc(x):
        return "-" if x is None else "%.1f%%" % (100 * x)

    def dp(k):
        d = p.get(k)
        if not d or d["diff"] is None or d["lo"] is None:
            return "-"
        return "%+.1f pts [%+.1f, %+.1f]" % (100 * d["diff"], 100 * d["lo"], 100 * d["hi"])
    return ("n=%d; B (%s off): P80 under the low bid %d/%d=%.1f%%, median overshoot %s, mean |ln| %.3f, "
            "median error %.1f%%, in band %.1f%%, multiplier %.3f; variant (%s on): under %d/%d=%.1f%%, "
            "median overshoot %s, mean |ln| %.3f, median error %.1f%%, in band %.1f%%, multiplier %.3f; "
            "under diff %s, median overshoot diff %s, mean |ln| diff %+.3f [%+.3f, %+.3f], in-band diff %s" % (
                n, vname, round(b["under"] * n), n, 100 * b["under"], pc(b["overshoot"]),
                b["mean_abs_log"], 100 * b["p50_abs"], 100 * b["in_band"], b["mult"], vname,
                round(v["under"] * n), n, 100 * v["under"], pc(v["overshoot"]),
                v["mean_abs_log"], 100 * v["p50_abs"], 100 * v["in_band"], v["mult"],
                dp("under"), dp("overshoot"), p["mean_abs_log"]["diff"],
                p["mean_abs_log"]["lo"] if p["mean_abs_log"]["lo"] is not None else 0.0,
                p["mean_abs_log"]["hi"] if p["mean_abs_log"]["hi"] is not None else 0.0,
                dp("in_band")))


def by_year(b_tests, v_tests):
    rows = []
    vv = {r["project_key"]: r for r in v_tests}
    bt_all = [r for r in b_tests if r["project_key"] in vv]
    for y in sorted({r["year"] for r in bt_all}):
        bt = [r for r in bt_all if r["year"] == y]
        vt = [vv[r["project_key"]] for r in bt]
        mb, mv = metrics(bt), metrics(vt)
        rows.append("| %s | %d | %s / %s | %s / %s | %s / %s | %.3f / %.3f | %.3f / %.3f |" % (
            y, len(bt), _fmt(mb["under"], "p"), _fmt(mv["under"], "p"),
            _fmt(mb["overshoot"], "p"), _fmt(mv["overshoot"], "p"), _fmt(mb["in_band"], "p"),
            _fmt(mv["in_band"], "p"), mb["mean_abs_log"], mv["mean_abs_log"], mult(bt),
            mult(vt)))
    return "\n".join(["| year | n | P80 under B / variant | median overshoot B / variant | in band B / variant | "
                      "mean \\|ln\\| B / variant | multiplier B / variant |",
                      "|---|---|---|---|---|---|---|"] + rows)


def sets_text(S, show_keys):
    s = S["_sets"]
    t = ("B scored %d, the variant scored %d, both scored %d (the tables all use these %d, paired)"
         % (s["B"], s["V"], s["both"], s["both"]))
    if s["only_B"] or s["only_V"]:
        t += "; only B scored %d, only the variant scored %d" % (len(s["only_B"]), len(s["only_V"]))
        if show_keys:
            t += " (only B: %s; only the variant: %s)" % (s["only_B"], s["only_V"])
    return t + "."


def print_paired(S, b_tests, v_tests, vname):
    say(ptable(S["全部"], vname))  # "all" split
    for label, _ in SPLITS[1:]:
        say("\n%s:" % label)
        say(ptable(S[label], vname))
    say()
    say(by_year(b_tests, v_tests))


def sensitivity_line(res, who=""):
    tm = res.get("tests_med")
    if tm is None:
        return ""
    if not tm:
        return "Sensitivity (deducting the median bidder) %s: no letting was scored" % who
    m = metrics(tm)
    n_u = sum(1 for r in tm if r.get("n_uncovered_lines"))
    return ("Sensitivity (uncovered lines deducted at each line's median bidder amount, constants refitted to match) %s: n=%d (%d of them with uncovered lines); "
            "P80 under the low bid %.1f%%, median overshoot %s, P50 median error %.1f%%"
            % (who, m["n"], n_u, 100 * m["under"],
               "-" if m["overshoot"] is None else "%.1f%%" % (100 * m["overshoot"]),
               100 * m["p50_abs"]))


def run_compare_core(panel, tab, base, excluded, test_keys, meta, switch, full_scope=False):
    """B and the variant on the same held-out lettings."""
    step("B: %s off (every other switch as in production)" % switch)
    with e.pricing_rule(**rule_kwargs(switch, False, full_scope)):
        resB = evaluate(panel, tab, base, excluded, test_keys, meta)
    step("variant: %s on" % switch)
    with e.pricing_rule(**rule_kwargs(switch, True, full_scope)):
        resV = evaluate(panel, tab, base, excluded, test_keys, meta)
    return resB, resV, splits(resB["tests"], resV["tests"])


def check_switch(switch, full_scope=False):
    names = switch_names()
    if switch not in names:
        say("REFUSED: --compare-switch %r is not a switch of estimate_low_bid.pricing_rule() (there are: %s)."
            % (switch, ", ".join(names)))
        return False
    if full_scope and switch == "coverage_scope":
        say("REFUSED: --full-scope and --compare-switch coverage_scope cannot be used together.")
        return False
    return True


def run_compare_decoy(seed, switch, json_path=None, full_scope=False):
    if not check_switch(switch, full_scope):
        return 2
    say("[REHEARSAL - decoy set, seed %d - paired: B = %s off, variant = %s on] Not the locked test set; nothing is written to the access log."
        % (seed, switch, switch))
    say(scope_text(full_scope))
    fp0 = fingerprint()
    step("reading master_panel (locked test set rows are dropped as they are read)")
    panel = Panel()
    tab = e.Tables()
    base = e._ForwardIndex(panel.avail)
    step("drawing the decoy (fixed pool: training lettings scored forward with every switch off)")
    decoy = draw_decoy(seed, panel, draw_population(panel, tab, base))
    excluded = make_excluded(panel, decoy["files"])
    test_keys = {pk for pk in panel.avail if _file_of(pk) in decoy["files"]}
    resB, resV, S = run_compare_core(panel, tab, base, excluded, test_keys,
                                     decoy_meta(decoy), switch, full_scope)
    changed = fingerprint() != fp0
    say("\n=== Decoy set (seed %d): B (%s off) and the variant (%s on) side by side, the same decoy lettings, paired ==="
        % (seed, switch, switch))
    say("Decoy: %d groups, %d files. B's decoy lettings: %s; the variant's: %s." % (
        len(decoy["groups"]), len(decoy["files"]), coverage_text(resB["cov_test"]),
        coverage_text(resV["cov_test"])))
    say("Scored on the training side: B %d, variant %d." % (resB["train_scored"], resV["train_scored"]))
    if not full_scope and e.COVERAGE_SCOPE:
        say("Coverage scope: B %s; variant %s." % (scope_counts(resB["tests"], resB["cov_test"]),
                                       scope_counts(resV["tests"], resV["cov_test"])))
    say(sets_text(S, True))
    say()
    print_paired(S, resB["tests"], resV["tests"], switch)
    say("\nHeadline (decoy set): " + pheadline(S["全部"], switch))  # "all" split
    for res, who in ((resB, "B"), (resV, "variant")):
        t = sensitivity_line(res, who)
        if t:
            say(t)
    if changed:
        say("\nWARNING: data/clean input files changed during the run; this result cannot be trusted.")
    json_path = json_path or os.path.join(SCRATCH, "decoy_compare_%s_%d.json" % (switch, seed))
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "decoy_compare", "seed": seed, "switch": switch,
                   "full_scope": bool(full_scope),
                   "decoy": {"groups": decoy["groups"], "files": sorted(decoy["files"])},
                   "coverage_test_B": dict(resB["cov_test"]),
                   "coverage_test_V": dict(resV["cov_test"]),
                   "train_scored_B": resB["train_scored"], "train_scored_V": resV["train_scored"],
                   "splits": S, "per_letting_B": resB["tests"], "per_letting_V": resV["tests"],
                   "sensitivity_B": resB.get("tests_med"), "sensitivity_V": resV.get("tests_med"),
                   "data_changed_during_run": changed}, f, ensure_ascii=False, indent=1,
                  default=str)
    say("\nPer-letting decoy results (B and the variant) written to %s" % os.path.relpath(json_path, ROOT))
    return 0 if not changed else 1


# ---------------------------------------------------------------------------
# the real set
def registration_files(text):
    """Existing files named in the --confirm text (absolute, ~, or relative to
    the project), with their md5 -- the registration must exist before opening."""
    found = []
    for tok in re.split(r"[\s,;，；、（）()【】\[\]<>「」\"'“”‘’：:]+", text or ""):  # regex kept: it also splits on full-width punctuation
        tok = tok.strip(".。")  # also strips a full-width full stop
        if not tok or ("/" not in tok and "." not in tok):
            continue
        for cand in (os.path.expanduser(tok), os.path.join(ROOT, tok)):
            if os.path.isfile(cand):
                found.append(os.path.abspath(cand))
                break
    out = []
    for p in sorted(set(found)):
        st = os.stat(p)
        out.append({"path": p, "md5": hashlib.md5(open(p, "rb").read()).hexdigest(),
                    "modified": datetime.datetime.fromtimestamp(st.st_mtime).astimezone()
                    .isoformat(timespec="seconds")})
    return out


def data_state():
    """Keys and source_set only: is the locked set still tagged "external"
    (before the rebuild) or "public_test" (after it)?"""
    csv.field_size_limit(10 ** 8)
    by = collections.Counter()
    stray = set()
    with open(os.path.join(e.CLEAN, "master_panel.csv"), encoding="utf-8-sig",
              newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_s, i_k = h.index("source_set"), h.index("project_key")
        memo = {}
        for row in rd:
            pk = row[i_k]
            v = memo.get(pk)
            if v is None:
                v = memo[pk] = ph.is_holdout(pk)
            if v:
                by[row[i_s]] += 1
            elif row[i_s] == "public_test":
                stray.add(pk)
    return {"rows_by_source_set": dict(by), "pre_rebuild": by.get("external", 0) > 0,
            "stray_public_test_keys": len(stray)}


def _md5_short(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()[:10]


def log_row(action, decision, result):
    path = ph.LOG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    if not new:
        with open(path, "rb") as f:
            f.seek(0, 2)
            if f.tell():
                f.seek(-1, 2)
                if f.read(1) not in (b"\n", b"\r"):
                    with open(path, "ab") as g:
                        g.write(b"\r\n")
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["when", "action", "decision_registered_in", "result"])
        w.writerow([stamp, action, decision, result])
        f.flush()
        os.fsync(f.fileno())


def previous_openings():
    if not os.path.exists(ph.LOG):
        return []
    with open(ph.LOG, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)
                if "holdout_eval --confirm 开始" in (r.get("action") or "")]  # matches the "start" rows of earlier openings, kept as logged


def run_confirm(text, allow_pre_rebuild=False, per_letting=False, out_dir=None,
                compare=None, full_scope=False):
    """Score the locked set.  Returns {"code": exit code, "tests": scored
    lettings or None}; the selftest rehearses it with a decoy standing in.
    compare: a pricing_rule switch -> the paired mode (B = it off, the
    variant = it on; "vtests" holds the variant's lettings).  full_scope:
    the old full-total scope."""
    text = (text or "").strip()
    if compare is not None and not check_switch(compare, full_scope):
        return {"code": 2, "tests": None}
    regs = registration_files(text)
    if not text or not regs:
        say("REFUSED: --confirm must state the decision being confirmed and the file it was registered in beforehand "
            "(name a file path that exists, e.g. reports/xxx.md). No such file was found.")
        return {"code": 2, "tests": None}
    if e.CLEAN != REAL_CLEAN:
        say("REFUSED: --confirm may only read the real data/clean.")
        return {"code": 2, "tests": None}
    prev = previous_openings()
    say("[LOCKED TEST SET] This run opens it and writes to %s." % os.path.relpath(ph.LOG, ROOT))
    say("Decision to confirm: %s" % text)
    if compare is not None:
        say("Paired mode: B = %s off, variant = %s on (every other switch as in production)." % (compare, compare))
    say(scope_text(full_scope))
    for r in regs:
        say("Registration file: %s (md5 %s, modified %s)" % (r["path"], r["md5"][:10], r["modified"]))
    say("Opened with this script %d time(s) before (rows in the log)." % len(prev))
    step("checking the data state (source_set and keys only)")
    st = data_state()
    say("Rows of the held-out files: %s; keys tagged public_test but not registered: %d"
        % (st["rows_by_source_set"], st["stray_public_test_keys"]))
    if st["pre_rebuild"] and not allow_pre_rebuild:
        say("REFUSED: the data has not been rebuilt -- the held-out files' rows are still tagged external in master_panel.\n"
            "Open the test set after the rebuild: the rebuild also takes these files out of the published quantity-shape table, the vocabulary, etc.\n"
            "On the training side this script already drops them through public_holdout.is_holdout; to run now anyway, "
            "add --allow-pre-rebuild (it is written into the log). The test set was not opened and nothing was logged.")
        return {"code": 2, "tests": None}
    code = "holdout_eval.py %s, estimate_low_bid.py %s" % (
        _md5_short(os.path.abspath(__file__)), _md5_short(os.path.join(ROOT, "src",
                                                                         "estimate_low_bid.py")))
    flags = []
    if compare is not None:
        flags.append("配对：B = %s 关 / 变体 = %s 开（--compare-switch）" % (compare, compare))  # log action texts kept as logged (the self-test matches the paired one): "paired: B = off / variant = on"
    if full_scope:
        flags.append("旧口径：全部子目、整张最低价（--full-scope）")  # "old scope: every line, the whole low bid"
    elif e.COVERAGE_SCOPE:
        flags.append("覆盖口径（扣掉未覆盖子目）")  # "coverage scope (uncovered lines taken out)"
    if st["pre_rebuild"]:
        flags.append("重建前的数据（--allow-pre-rebuild）")  # "data before the rebuild"
    if per_letting:
        flags.append("含逐个招标明细（--per-letting）")  # "with the per-letting detail"
    decision = text + " | registration file: " + "; ".join(
        "%s md5 %s" % (os.path.relpath(r["path"], ROOT) if r["path"].startswith(ROOT)
                       else r["path"], r["md5"]) for r in regs)
    log_row("holdout_eval --confirm 开始打分（%s%s）" % (code, ("；" + "；".join(flags))  # action text kept as logged: "start scoring (...)"
                                                     if flags else ""),
            decision, "（运行中）")  # result placeholder kept as logged: "(running)"
    fp0 = fingerprint()
    try:
        step("reading master_panel (with the held-out files' rows)")
        panel = Panel(read_real=True)
        tab = e.Tables()
        step("building the dated price index (held-out projects removed after the first grouping)")
        base = e._ForwardIndex(panel.avail)
        excluded = make_excluded(panel)
        reg, reg_keys = {}, set()
        with open(ph.REG, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                reg[r["src_file"]] = (r["group"], r["erosion_drainage"] == "Y")
                reg_keys |= {k.strip() for k in (r.get("letting_keys") or "").split("||")
                             if k.strip()}

        def meta(pk):
            f = _file_of(pk)
            if f in reg:
                return reg[f]
            return f, bool({panel.work.get(pk, "")} & ED_TYPES)
        resV = S = None
        if compare is None:
            with (e.pricing_rule(**rule_kwargs(None, True, True)) if full_scope
                  else contextlib.nullcontext()):
                res = evaluate(panel, tab, base, excluded, set(panel.real), meta)
        else:
            res, resV, S = run_compare_core(panel, tab, base, excluded, set(panel.real),
                                            meta, compare, full_scope)
        summary = summarize(res["tests"])
        changed = fingerprint() != fp0
    except BaseException as ex:
        log_row("holdout_eval --confirm 中断", decision, "error: %s: %s" % (type(ex).__name__, ex))  # action text kept as logged: "aborted"
        raise
    head = headline(summary) if compare is None else pheadline(S["全部"], compare)  # "all" split
    result = head + "; coverage: " + coverage_text(res["cov_test"])
    if compare is not None:
        s_ = S["_sets"]
        result += "; variant coverage: %s; scored by both %d (only B %d, only the variant %d)" % (
            coverage_text(resV["cov_test"]), s_["both"], len(s_["only_B"]), len(s_["only_V"]))
    if changed:
        result = "data/clean changed during the run; result void. " + result
    log_row("holdout_eval --confirm 完成", decision, result)  # action text kept as logged: "done"
    say()
    say("=== Locked test set result (written to the access log) ===")
    say("Held-out lettings: %s." % coverage_text(res["cov_test"]))
    got = {r["project_key"] for r in res["tests"]}
    say("Of the %d letting keys registered at the draw, %d were scored this time; %d scored keys are not among the registered ones "
        "(key names may have changed in a rebuild)." % (len(reg_keys), len(got & reg_keys), len(got - reg_keys)))
    if panel.stray_public_test:
        say("Note: %d keys are tagged public_test but their files are not in the registry; they are held out as well."
            % len(panel.stray_public_test))
    say("Training: %d municipal lettings scored forward." % res["train_scored"])
    if compare is not None:
        say("Variant held-out lettings: %s. Variant training side: %d." % (coverage_text(resV["cov_test"]),
                                          resV["train_scored"]))
        say(sets_text(S, per_letting))
    if e.COVERAGE_SCOPE and not full_scope:
        say("Coverage scope: %s." % scope_counts(res["tests"], res["cov_test"]))
    say(shape_text(res["tests"], "held-out"))
    say()
    if compare is None:
        say(table(summary))
    else:
        print_paired(S, res["tests"], resV["tests"], compare)
    say()
    say("Headline: " + head)
    for rr, who in ((res, "" if compare is None else "B"), (resV, "variant")):
        if rr is not None and sensitivity_line(rr, who):
            say(sensitivity_line(rr, who))
    say()
    say(GLOSSARY)
    if per_letting:
        say("\nPer letting:")
        vv = {r["project_key"]: r for r in (resV["tests"] if resV else [])}
        for r in sorted(res["tests"], key=lambda r: (r["date"], r["project_key"])):
            v = vv.get(r["project_key"])
            say("  %s  %s  %s  P50/low %.3f  P80/low %.3f%s" % (
                r["date"], "erosion/drainage" if r["erosion_drainage"] else "other",
                r["project_key"], r["p50"], r["p80"],
                ("  | variant %.3f %.3f" % (v["p50"], v["p80"])) if v else ""))
    if changed:
        say("\nWARNING: data/clean input files changed during the run (someone may be rebuilding); this result is void.")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_dir or SCRATCH
    path = os.path.join(out_dir, "confirm_%s.json" % stamp)
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if S is not None and not per_letting:
            S = dict(S, _sets={k: (v if isinstance(v, int) else len(v))
                               for k, v in S["_sets"].items()})
        json.dump({"mode": "confirm" if compare is None else "confirm_compare",
                   "switch": compare, "full_scope": bool(full_scope),
                   "confirm": text, "registration": regs,
                   "data_state": st, "coverage_test": dict(res["cov_test"]),
                   "coverage_test_V": dict(resV["cov_test"]) if resV else None,
                   "train_scored": res["train_scored"], "summary": summary,
                   "splits": S,
                   "per_letting": ((res["tests"] if resV is None else
                                    {"B": res["tests"], "V": resV["tests"]})
                                   if per_letting else "not written"),
                   "data_changed_during_run": changed}, f, ensure_ascii=False, indent=1,
                  default=str)
    say("\nSummary written to %s" % os.path.relpath(path, ROOT))
    return {"code": 0 if not changed else 1, "tests": res["tests"],
            "vtests": resV["tests"] if resV else None, "splits": S}


# ---------------------------------------------------------------------------
# instrumentation (--selftest)
class Audit:
    """Every reference row, index level, discount table and constant handed to
    a pricing must come from training lettings dated strictly before the
    pricing date: checked where each is handed out, not by re-reading a column."""

    def __init__(self, excluded, avail, test_keys, full_dates=()):
        self.excluded, self.avail, self.test_keys = excluded, avail, set(test_keys)
        # dates at which the index and discount are also refitted from scratch
        # (None = every pricing date)
        self.full_dates = None if full_dates is None else set(full_dates)
        self.cur_D = None
        self.n = collections.Counter()
        self.fails = []
        self._cells, self._idx_done, self._disc_done = {}, set(), set()

    def held_out(self, pk):
        return self.excluded(pk) or pk in self.test_keys

    def bad(self, msg):
        self.n["失败"] += 1  # the counter keys of self.n are kept as they are (dict keys); this one = failures
        if len(self.fails) < 25:
            self.fails.append(msg)

    def static_cells(self, cells):
        for kind in ("tok", "band", "fam"):
            for k, by in cells[kind].items():
                self.n["检查的参考格"] += 1  # reference cells checked
                leak = [pk for pk in by if self.held_out(pk)]
                if leak:
                    self.bad("reference cell %s %r holds held-out lettings %s" % (kind, k, leak[:3]))

    def ref_row(self, kind, k, by, D, r):
        self.n["发出的参考行"] += 1  # reference rows handed out
        if self.cur_D is not None and D != self.cur_D:
            self.bad("reference table date %s is not the pricing date %s" % (D, self.cur_D))
        st = self._cells.get((kind, k))
        if st is None:
            st = self._cells[(kind, k)] = sorted(self.avail.get(pk, e.NEVER) for pk in by)
        want = bisect.bisect_left(st, D)
        got = r["n_projects"] if r is not None else 0
        if got != want:
            self.bad("reference cell %s %r at %s: used %d lettings, but before %s there are only %d"
                     % (kind, k, D, got, D, want))

    def heavy(self, pks):
        self.n["参考中位数（逐个招标核对）"] += 1  # reference medians (checked letting by letting)
        D = self.cur_D
        for pk in pks:
            self.n["核对的参考招标"] += 1  # reference lettings checked
            if D is None or self.avail.get(pk, e.NEVER) >= D:
                self.bad("reference price used %s (%s), pricing date %s"
                         % (pk, self.avail.get(pk), D))
            if self.held_out(pk):
                self.bad("reference price used held-out letting %s" % pk)

    def fitted(self, size_cells, pools):
        """V2+F1+F2's fitted sets, as built: no held-out letting in any F1
        size cell, in a comparables pool or in its beta/gamma rows."""
        if size_cells is not None:
            self.n["F1 尺寸格"] += len(size_cells.cells)  # F1 size cells
            leak = sorted(pk for pk in size_cells.lettings() if self.held_out(pk))
            if leak:
                self.bad("F1 size cells hold held-out lettings %s" % leak[:3])
        for mk, pool in sorted((pools or {}).items()):
            keys = pool.keys()
            self.n["可比池招标"] += len(keys)  # comparables-pool lettings
            leak = [pk for pk in keys if self.held_out(pk)]
            if leak:
                self.bad("comparables pool %s holds held-out lettings %s" % (mk, leak[:3]))
            leak = sorted(pk for pk in pool.bg.keys_before(e.NEVER) if self.held_out(pk))
            if leak:
                self.bad("the beta/gamma of comparables pool %s hold held-out lettings %s" % (mk, leak[:3]))

    def priced(self, pk, D, r, size_cells):
        """One scored letting: its chosen comparables, the lettings behind
        beta/gamma at its cutoff, and every F1 line's reference size and
        slope k come from training lettings dated strictly before D."""
        cm = r.get("comparables")
        if cm is not None:
            self.n["核对的可比法定价"] += 1  # comparables pricings checked
            for c, cd in zip(cm["comps"], cm["comp_dates"]):
                self.n["核对的可比招标"] += 1  # comparable lettings checked
                if self.held_out(c):
                    self.bad("a comparable of %s is held-out letting %s" % (pk, c))
                if cd >= D or self.avail.get(c, e.NEVER) >= D:
                    self.bad("a comparable of %s, %s, is not earlier than the pricing date %s" % (pk, c, D))
            if cm.get("date") != D or cm.get("bg_cutoff", D) > D:
                self.bad("%s: comparables date %s / beta-gamma cutoff %s is wrong (pricing date %s)"
                         % (pk, cm.get("date"), cm.get("bg_cutoff"), D))
        for t in r["trace"]:
            f = t.get("f1")
            if not f or "ref_key" not in f:
                continue
            self.n["核对的 F1 子目"] += 1  # F1 lines checked
            rk = tuple(f["ref_key"])
            used = set(size_cells.ref_lettings(rk, D)) | size_cells.k_lettings(rk[0], rk[1], D)
            bad = [q for q in used if self.held_out(q) or self.avail.get(q, e.NEVER) >= D]
            if bad:
                self.bad("%s: the reference size / k of F1 line %r used %s" % (pk, rk, sorted(bad)[:3]))

    def constants(self, rec, c, train_recs, const):
        self.n["核对的测试常数"] += 1  # test constants checked
        D = rec["date"]
        prior = [r for r in train_recs if r["date"] < D]          # filtered afresh
        if len(prior) != c["n_prior"] or set(r["project_key"] for r in prior) != set(
                c["prior_keys"]):
            self.bad("wrong training set for the constants: %s" % rec["project_key"])
        late = [k for k in c["prior_keys"] if self.avail.get(k, e.NEVER) >= D]
        if late:
            self.bad("the constants used lettings dated on or after %s: %s" % (D, late[:3]))
        leak = [k for k in c["prior_keys"] if self.held_out(k)]
        if leak:
            self.bad("the constants used held-out lettings %s" % leak[:3])
        trip4 = [(r["date"], r["bid_part"], r["owner_fixed"], r["actual_low"]) for r in prior]
        g = e._not_under_guard(trip4, e.GUARD_GAMMA[MARKET])
        if abs(g["alpha"] - c["alpha"]) > 1e-12:
            self.bad("guard level differs %s: %r vs production %r" % (rec["project_key"], c["alpha"],
                                                    g["alpha"]))
        lc = e._centre([t[1:] for t in trip4])
        if abs(lc - c["lc"]) > 1e-12:
            self.bad("centring factor differs %s" % rec["project_key"])
        cen = sorted((cc * lc + fx) / w for _, cc, fx, w in trip4)
        if abs(cen[int(g["alpha"] * (len(cen) - 1))] - c["p"]) > 1e-12:
            self.bad("P80 divisor differs %s" % rec["project_key"])


class _AuditKind(e._FwdKind):
    def __init__(self, cells_kind, kind, minn, avail, audit):
        super().__init__(cells_kind, kind, minn, avail)
        self.audit = audit

    def row(self, k, D):
        r = super().row(k, D)
        self.audit.ref_row(self.kind, k, self.c[k], D, r)
        return r


class _AuditKindMuni(_AuditKind):
    """MC's municipal-only cells under the audit: rows marked municipal, and
    audited under their own cell keys (apart from the pooled cells')."""

    def __init__(self, cells_kind, kind, minn, avail, audit):
        super().__init__(cells_kind, kind, minn, avail, audit)
        self.market = "municipal"

    def row(self, k, D):
        r = e._FwdKind.row(self, k, D)
        self.audit.ref_row("muni_" + self.kind, k, self.c[k], D, r)
        return r


class _AuditIndex(TrainIndex):
    def __init__(self, base, excluded, audit):
        super().__init__(base, excluded)
        self.audit = audit
        for arm in self.ARMS:
            leak = [pk for pk in self.S1[arm].project_key.unique() if audit.held_out(pk)]
            if leak:
                audit.bad("index arm %s holds held-out lettings %s" % (arm, leak[:3]))
            sd = self.SD[arm]
            if len(sd) > 1 and not (sd[:-1] <= sd[1:]).all():
                audit.bad("index arm %s is not sorted by date" % arm)

    def at(self, D):
        res = super().at(D)
        a = self.audit
        a.n["指数调用"] += 1  # counter key kept: index calls
        if a.cur_D is not None and D != a.cur_D:
            a.bad("index date %s is not the pricing date %s" % (D, a.cur_D))
        for arm, k in zip(self.ARMS, self._sig):
            sd = self.SD[arm]
            if (k > 0 and not sd[k - 1] < D) or (k < len(sd) and not sd[k] >= D):
                a.bad("index arm %s: wrong cutoff at %s" % (arm, D))
        if (a.full_dates is None or D in a.full_dates) and D not in a._idx_done:
            a._idx_done.add(D)
            a.n["从头重算的指数"] += 1  # counter key kept: indexes refitted from scratch
            for arm in self.ARMS:
                s1 = self.S1[arm]
                again = e._chain(s1[s1.D < D], adjacent=(arm != "_default"))
                if (again or None) != res.get(arm):
                    a.bad("index arm %s at %s: the refit from scratch differs" % (arm, D))
        return res


class _AuditDiscount(TrainDiscount):
    def __init__(self, avail, excluded, audit):
        super().__init__(avail, excluded)
        self.audit = audit
        leak = [x for x in self.LD.letting_id if x and audit.held_out(x)]
        if leak:
            audit.bad("discount table holds held-out lettings %s" % leak[:3])
        if len(self.LDD) > 1 and not (self.LDD[:-1] <= self.LDD[1:]).all():
            audit.bad("discount table is not sorted by date")

    def at(self, D):
        res = super().at(D)
        a = self.audit
        a.n["折扣调用"] += 1  # counter key kept: discount calls
        if a.cur_D is not None and D != a.cur_D:
            a.bad("discount date %s is not the pricing date %s" % (D, a.cur_D))
        k, sd = self._k, self.LDD
        if (k > 0 and not sd[k - 1] < D) or (k < len(sd) and not sd[k] >= D):
            a.bad("discount: wrong cutoff at %s" % D)
        if (a.full_dates is None or D in a.full_dates) and D not in a._disc_done:
            a._disc_done.add(D)
            a.n["从头重算的折扣"] += 1  # counter key kept: discounts refitted from scratch
            if self._fit(self.LD[self.LD.D < D]) != res:
                a.bad("discount at %s: the refit from scratch differs" % D)
        return res


@contextlib.contextmanager
def audit_medians(audit):
    """Route every lazily computed reference median through the audit: the
    lambda inside _FwdKind.row looks _fwd_heavy up in estimate_low_bid at call
    time, so the lettings whose prices actually enter a median are seen."""
    orig = e._fwd_heavy

    def wrapped(by, pks):
        audit.heavy(pks)
        return orig(by, pks)
    e._fwd_heavy = wrapped
    try:
        yield
    finally:
        e._fwd_heavy = orig


def simulate_rebuild(dst, retag, drop=None):
    """A data/clean as the rebuild leaves it for the held-out files: the files
    the forward path reads, copied (never linked), with source_set set to
    "public_test" on master_panel and municipal_letting_totals rows whose key
    retag() accepts, and rows whose key drop() accepts left out altogether."""
    os.makedirs(dst, exist_ok=True)
    n = collections.Counter()
    for name in INPUTS:
        src = os.path.join(REAL_CLEAN, name)
        if name in ("master_panel.csv", "municipal_letting_totals.csv"):
            keycol = "project_key" if name == "master_panel.csv" else "letting_id"
            with open(src, "rb") as fb:
                first = fb.readline()
            term = "\r\n" if first.endswith(b"\r\n") else "\n"
            with open(src, encoding="utf-8-sig", newline="") as fi, \
                    open(os.path.join(dst, name), "w", encoding="utf-8-sig", newline="") as fo:
                rd = csv.reader(fi)
                wr = csv.writer(fo, lineterminator=term)
                h = next(rd)
                wr.writerow(h)
                i_s, i_k = h.index("source_set"), h.index(keycol)
                for row in rd:
                    if drop is not None and drop(row[i_k]):
                        n[name + " dropped"] += 1
                        continue
                    if row[i_s] == "external" and retag(row[i_k]):
                        row[i_s] = "public_test"
                        n[name] += 1
                    wr.writerow(row)
        else:
            shutil.copyfile(src, os.path.join(dst, name))
    return n


def _same_rec(a, b):
    if a["date"] != b["date"] or a["n_bidders"] != b["n_bidders"] \
            or a["n_items"] != b["n_items"]:
        return False
    for k in ("bid_part", "owner_fixed", "actual_low"):
        x, y = a[k], b[k]
        if abs(x - y) > 1e-9 * max(1.0, abs(x), abs(y)):
            return False
    return True


def _selftest(seed):
    ok = [True]

    def chk(name, good, detail=""):
        if not good:
            ok[0] = False
        say("%s  %s%s" % ("PASS" if good else "FAIL", name, ("  -- " + detail) if detail else ""))

    say("[SELF-TEST] Runs on the decoy set (seed %d); the locked test set is not opened and nothing is written to the access log." % seed)
    fp0 = fingerprint()
    log0 = open(ph.LOG, "rb").read() if os.path.exists(ph.LOG) else None
    step("reading master_panel (locked test set rows are dropped as they are read)")
    panel = Panel()
    tab = e.Tables()
    base = e._ForwardIndex(panel.avail)
    chk("0 the locked test set is dropped when the panel is read", not any(ph.is_holdout(r[1]) for r in panel.pool)
        and not any(ph.is_holdout(k) for k in panel.avail),
        "dropped %d rows, %d keys" % (sum(panel.real_rows.values()), len(panel.real)))

    # ---- stage 1 + the decoy, then stage 2 with every hand-out instrumented
    step("stage 1: empty test set")
    st1 = evaluate(panel, tab, base, make_excluded(panel), set(), lambda pk: (pk, False))
    decoy = draw_decoy(seed, panel, draw_population(panel, tab, base))
    excluded = make_excluded(panel, decoy["files"])
    test_keys = {pk for pk in panel.avail if _file_of(pk) in decoy["files"]}
    audit = Audit(excluded, panel.avail, test_keys,
                  full_dates={panel.avail[k] for k in test_keys})
    with audit_medians(audit):
        step("stage 2: decoy held out, every hand-out point instrumented")
        st2 = evaluate(panel, tab, base, excluded, test_keys, decoy_meta(decoy), audit)

    reg_files = ph.holdout_files()
    chk("1 the decoy and the locked test set share no file", not (decoy["files"] & reg_files)
        and not any(ph.is_holdout(f) for f in decoy["files"]),
        "%d groups, %d files" % (len(decoy["groups"]), len(decoy["files"])))
    by_year = collections.Counter(u["year"] for u in decoy["groups"].values())
    strata = collections.Counter((u["year"], u["erosion_drainage"])
                                 for u in decoy["groups"].values())
    ed = sum(u["erosion_drainage"] for u in decoy["groups"].values())
    tgt = decoy["target"]
    chk("1 every year x work-type cell of the decoy has as many groups as the locked set (except cells whose pool runs short)",
        all(strata[k] == n or (k[0] in decoy["short_years"] and strata[k] <= n)
            for k, n in tgt.items()) and sum(strata.values()) >= 0.8 * sum(tgt.values()),
        "by year %s, erosion/drainage %d groups; locked set %d groups" % (
            dict(sorted(by_year.items())), ed, sum(tgt.values())))
    chk("1 most decoy lettings are scored", len(st2["tests"]) >= 50, coverage_text(st2["cov_test"]))
    a = audit
    chk("2 instrumented: no held-out letting, and no letting dated on or after the pricing date, enters the prices or the constants",
        a.n["失败"] == 0, "; ".join(a.fails[:5]) if a.fails else "")  # counter key kept: failures
    say("      checks made: %s" % ", ".join("%s %d" % kv for kv in sorted(a.n.items())
                                    if kv[0] != "失败"))  # counter key kept: failures
    chk("2 every V2+F1+F2 fitted quantity is instrumented: no held-out letting in the comparables pool, beta/gamma or F1 size cells; "
        "every scored letting's chosen comparables and every F1 line's reference size and k come only from training lettings before the pricing date",
        a.n["可比池招标"] > 0 and a.n["F1 尺寸格"] > 0 and a.n["核对的可比法定价"] > 0  # counter keys kept (see Audit)
        and a.n["核对的可比招标"] > 0 and a.n["失败"] == 0,
        "comparables pool %d lettings, size cells %d; comparables pricings %d, comparable lettings %d, F1 lines %d" % (
            a.n["可比池招标"], a.n["F1 尺寸格"], a.n["核对的可比法定价"],
            a.n["核对的可比招标"], a.n["核对的 F1 子目"]))
    sizes_ok = st2["size_cells"] is not None and not (
        st2["size_cells"].lettings() & set(test_keys)) and not any(
        excluded(k) for k in st2["size_cells"].lettings())
    pool_ok = all(not excluded(k) and k not in test_keys
                  for p_ in (st2["pools"] or {}).values() for k in p_.keys())
    chk("2 no decoy-file letting in the training side's comparables pool or F1 size cells (rechecked by file name)",
        sizes_ok and pool_ok and bool(st2["pools"]),
        "comparables pool %d, size-cell lettings %d" % (
            sum(len(p_.keys()) for p_ in (st2["pools"] or {}).values()),
            len(st2["size_cells"].lettings()) if st2["size_cells"] else 0))
    chk("2 every decoy letting's constants match a recomputation by the production functions (_not_under_guard / _centre)",
        a.n["核对的测试常数"] == len(st2["tests"]), "%d" % a.n["核对的测试常数"])  # counter key kept: test constants checked
    tdates = {r["date"] for r in st2["tests"]}
    chk("2 at every decoy date the index and the discount were refitted from scratch on date-filtered rows and equal the cut prefix",
        tdates <= a._idx_done and tdates <= a._disc_done,
        "decoy dates %d; index refitted %d times, discount %d times" % (
            len(tdates), a.n["从头重算的指数"], a.n["从头重算的折扣"]))  # counter keys kept
    dk_idx = {arm: len({k for k in v if _file_of(k) in decoy["files"]})
              for arm, v in st2["index_removed"].items()}
    dk_disc = [lid for lid, _ in st2["discount_removed"] if _file_of(lid) in decoy["files"]]
    chk("2 the decoy projects really are removed from the index and the discount",
        sum(dk_idx.values()) > 0 and len(dk_disc) > 0,
        "decoy projects removed from the index %s; decoy lettings removed from the discount %d" % (dk_idx, len(dk_disc)))
    shape = sum(r["shape_items"] for r in st2["tests"])
    say("      the published quantity-shape table (not forward; production's forward calibration uses it the same way) is used on %d decoy schedule items." % shape)

    # negative controls: the instrument must be able to fail
    neg = Audit(lambda pk: pk == "LEAK", {"A": "2024-01-01", "LEAK": "2023-01-01",
                                         "LATE": "2025-06-01"}, {"T"})
    neg.cur_D = "2025-01-01"
    neg.static_cells({"tok": {}, "band": {}, "fam": {("x", "EA", ""): {"LEAK": [], "A": []}}})
    n1 = neg.n["失败"]  # counter key kept: failures
    neg.heavy(["A", "LATE"])
    n2 = neg.n["失败"]  # counter key kept: failures
    neg.ref_row("fam", ("x", "EA", ""), {"A": [], "LATE": []}, "2025-01-01", {"n_projects": 2})
    chk("3 negative control: the instrument catches planted leaks", n1 == 1 and n2 == 2 and neg.n["失败"] == 3,
        "held-out cell %d, late date %d, extra lettings counted %d" % (n1, n2 - n1, neg.n["失败"] - n2))  # counter key kept: failures
    # the same for V2+F1+F2's fitted quantities: a planted held-out letting
    # in a size cell, in the comparables pool and its beta/gamma, a chosen
    # comparable dated after D, and an F1 line resting on a late letting
    rows_ = [("municipal", pk, "water_pipe", "LF", "8-12", 10.0, 50.0, True, "b",
              "8 in water line", "o", "2020") for pk in ("A", "LEAK", "LATE")]
    si_ = e.SizeCells(rows_, [8.0, 10.0, 12.0], {"A": "2024-01-01", "LEAK": "2023-01-01",
                                                  "LATE": "2025-06-01"})
    pool_ = types.SimpleNamespace(keys=lambda: ["A", "LEAK"], bg=types.SimpleNamespace(
        keys_before=lambda c: {"A", "LEAK"}))
    neg.fitted(si_, {"municipal": pool_})
    n3 = neg.n["失败"]  # counter key kept: failures
    neg.priced("T", "2025-01-01", {"comparables": {"comps": ["A", "LATE"],
                                                  "comp_dates": ["2024-01-01", "2025-06-01"],
                                                  "date": "2025-01-01", "bg_cutoff": "2023-01-01"},
                                  "trace": [{"f1": {"ref_key": ("water_pipe", "LF", "8-12")}}]},
               si_)
    n4 = neg.n["失败"]  # counter key kept: failures
    chk("3 negative control: the V2+F1+F2 instruments also catch planted leaks (size cells, comparables pool, beta/gamma, "
        "late comparables, F1 references)", n3 - 3 == 3 and n4 - n3 == 2,
        "size cells / comparables pool / beta-gamma %d, comparables and F1 lines %d" % (n3 - 3, n4 - n3))

    # flip: stage 1 vs stage 2 on the training side
    s1 = {r["project_key"]: r for r in st1["train_recs"]}
    s2 = {r["project_key"]: r for r in st2["train_recs"]}
    d_min = min([panel.avail[pk] for pk in test_keys]
                + [d for lid, d in st2["discount_removed"] if _file_of(lid) in decoy["files"]])
    early = [k for k in s1 if s1[k]["date"] <= d_min and _file_of(k) not in decoy["files"]]
    later = [k for k in s2 if s2[k]["date"] > d_min and k in s1]
    moved = [k for k in later if not _same_rec(s1[k], s2[k])]
    gone_decoy = [k for k in s1 if _file_of(k) in decoy["files"]]
    gone_other = [k for k in s1 if k not in s2 and _file_of(k) not in decoy["files"]]
    chk("4 flip control: the training lettings up to the first decoy date %s are identical before and after the hold-out" % d_min,
        all(k in s2 and _same_rec(s1[k], s2[k]) for k in early), "%d" % len(early))
    chk("4 later training lettings do change (the hold-out really acts)", len(moved) > 0,
        "of %d, %d changed; another %d could no longer be scored for lack of reference prices"
        % (len(later), len(moved), len(gone_other)))
    chk("4 every letting of the decoy files has left the training side",
        not any(_file_of(k) in decoy["files"] for k in s2),
        "%d were on the training side before" % len(gone_decoy))

    # ---- a simulated rebuild: production code on it
    mdisc = TrainDiscount(panel.avail, make_excluded(panel))   # real data, is_holdout route
    midx = TrainIndex(base, make_excluded(panel))              # real data, is_holdout route
    with tempfile.TemporaryDirectory(prefix="holdout_eval_sim_") as tmp:
        simA = os.path.join(tmp, "A")
        step("simulated rebuild A: locked test set rows tagged public_test (temp dir, removed at the end)")
        nA = simulate_rebuild(simA, ph.is_holdout)
        chk("5 simulated rebuild A retags every locked-set row still tagged external",
            nA["master_panel.csv"] == panel.real_rows.get("external", 0),
            "%s; locked-set rows in the panel %s" % (dict(nA), dict(panel.real_rows)))
        with clean_dir(simA):
            step("production code on A: _reference_pool / _letting_dates / _ForwardDiscount / "
                 "_ForwardIndex")
            prod_pool = e._reference_pool()
            chk("5 the price pool this script reads = production's _reference_pool (row by row, column by column)",
                prod_pool == panel.pool, "%d vs %d rows" % (len(prod_pool), len(panel.pool)))
            del prod_pool
            prod_avail = e._letting_dates()
            chk("5 letting dates = production's _letting_dates", prod_avail == panel.avail,
                "%d keys" % len(prod_avail))
            pdisc = e._ForwardDiscount(prod_avail)
            cols = ["src", "D", "n_bidders", "low", "median", "lom"]
            chk("5 discount input (removed by the registry on the real data) = production _ForwardDiscount (removed by source_set on A)",
                pdisc.LD[cols].equals(mdisc.LD[cols]), "%d rows" % len(pdisc.LD))
            pidx = e._ForwardIndex(prod_avail)
            chk("5 index first pass (removed by the registry on the real data) = production _ForwardIndex (removed by source_set on A)",
                all(pidx.S1[arm].equals(midx.S1[arm]) for arm in pidx.ARMS),
                ", ".join("%s %d rows" % (arm, len(pidx.S1[arm])) for arm in pidx.ARMS))
            del pidx, pdisc
            step("production calibrate_forward(write=False) on A (prices every market's lettings forward; slower)")
            pl = []
            rows_f, _ = e.calibrate_forward(write=False, per_letting=pl)
        prod = {r["project_key"]: r for r in pl if r["market"] == MARKET}
        mine = {r["project_key"]: r for r in st1["train_recs"]}
        diff = [k for k in prod if k not in mine or not _same_rec(prod[k], mine[k])]
        extra = [k for k in mine if k not in prod]
        chk("6 with an empty test set, the training side = calibrate_forward's municipal forward records (letting by letting)",
            not diff and not extra and len(prod) > 0,
            "%d lettings; %d differ, %d extra" % (len(prod), len(diff), len(extra)))
        rowm = next((r for r in rows_f if r["market"] == MARKET), None)
        c_end = st1["const"].at("9999-12-30")
        g_end = e._not_under_guard([(r["date"], r["bid_part"], r["owner_fixed"],
                                     r["actual_low"]) for r in st1["train_recs"]],
                                   e.GUARD_GAMMA[MARKET])
        chk("6 the constants after all the data = the municipal constants calibrate_forward writes",
            rowm is not None and round(c_end["lc"], 6) == rowm["level_correction"]
            and round(c_end["alpha"], 6) == rowm["not_under_level"]
            and round(c_end["p"], 6) == rowm["p_not_under"]
            and g_end["n_scored"] == rowm["guard_scored"],
            "centring %.6f / %s, level %.6f / %s, divisor %.6f / %s" % (
                c_end["lc"], rowm and rowm["level_correction"], c_end["alpha"],
                rowm and rowm["not_under_level"], c_end["p"], rowm and rowm["p_not_under"]))

        # ---- the post-rebuild path for test lettings, on the decoy
        simB = os.path.join(tmp, "B")
        step("simulated rebuild B: locked-set and decoy-file rows both tagged public_test")
        nB = simulate_rebuild(simB, lambda k: ph.is_holdout(k) or _file_of(k) in decoy["files"])
        with clean_dir(simB):
            pB = Panel(decoy_files=decoy["files"])
            baseB = e._ForwardIndex(pB.avail)
            _, _, stB = run_decoy_core(seed, pB, tab, baseB, decoy=decoy)
        a2 = {r["project_key"]: r for r in st2["tests"]}
        b2 = {r["project_key"]: r for r in stB["tests"]}
        same = set(a2) == set(b2) and all(
            _same_rec(a2[k], b2[k]) and abs(a2[k]["p80"] - b2[k]["p80"]) < 1e-12
            and abs(a2[k]["p50"] - b2[k]["p50"]) < 1e-12 for k in a2)
        chk("7 the post-rebuild reading (decoy rows tagged public_test) gives identical decoy scores", same,
            "%d vs %d; B retagged %s" % (len(a2), len(b2), dict(nB)))
        t2 = {r["project_key"]: r for r in st2["train_recs"]}
        tB = {r["project_key"]: r for r in stB["train_recs"]}
        chk("7 the post-rebuild reading also gives an identical training side", set(t2) == set(tB) and all(
            _same_rec(t2[k], tB[k]) for k in t2), "%d" % len(t2))
        # ---- the --confirm path end to end, the decoy standing in for the locked set
        simC = os.path.join(tmp, "C")
        step("simulation C: locked-set rows deleted outright; decoy rows tagged public_test, standing in for the locked set")
        nC = simulate_rebuild(simC, lambda k: _file_of(k) in decoy["files"], drop=ph.is_holdout)
        reg_path = os.path.join(tmp, "registry_decoy.csv")
        log_path = os.path.join(tmp, "access_log_decoy.csv")
        with open(reg_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["src_file", "group", "year", "first_bid_date", "owner", "work_types",
                        "erosion_drainage", "letting_keys", "selected_at", "seed"])
            for g, u in sorted(decoy["groups"].items()):
                for fl in u["files"]:
                    w.writerow([fl, g, u["year"], "", "", "", "Y" if u["erosion_drainage"]
                                else "", " || ".join(u["keys"]), "selftest", seed])
        with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["when", "action", "decision_registered_in", "result"])
        saved = (ph.REG, ph.LOG, ph._cache, REAL_CLEAN)
        buf = io.StringIO()
        step("scoring the stand-in locked set through the --confirm path (registry and log in the temp dir)")
        try:
            ph.REG, ph.LOG, ph._cache = reg_path, log_path, None
            globals()["REAL_CLEAN"] = simC
            with clean_dir(simC), contextlib.redirect_stdout(buf):
                out_c = run_confirm("self-test rehearsal: decoy standing in for the locked set (registered in %s)" % reg_path, out_dir=tmp)
        finally:
            ph.REG, ph.LOG, ph._cache = saved[0], saved[1], saved[2]
            globals()["REAL_CLEAN"] = saved[3]
        with open(log_path, encoding="utf-8-sig", newline="") as f:
            lrows = list(csv.DictReader(f))
        c2 = {r["project_key"]: r for r in (out_c["tests"] or [])}
        same_c = set(c2) == set(a2) and all(
            abs(c2[k]["p80"] - a2[k]["p80"]) < 1e-12 and abs(c2[k]["p50"] - a2[k]["p50"]) < 1e-12
            and c2[k]["group"] == a2[k]["group"]
            and c2[k]["erosion_drainage"] == a2[k]["erosion_drainage"] for k in a2)
        chk("8 the --confirm path end to end: scores identical to the decoy rehearsal", out_c["code"] == 0 and same_c,
            "%d; C deleted %d rows, retagged %d rows" % (len(c2), nC["master_panel.csv dropped"],
                                         nC["master_panel.csv"]))
        chk("8 --confirm writes the 'start' row first and the result row at the end, with the headline in it",
            len(lrows) == 2 and "开始打分" in lrows[0]["action"] and lrows[0]["result"] ==  # log texts kept as logged: "start scoring", "(running)", "done"
            "（运行中）" and "完成" in lrows[1]["action"]
            and lrows[1]["result"].startswith("n=") and "registry_decoy.csv" in
            lrows[0]["decision_registered_in"], "log %d rows" % len(lrows))
        out_txt = buf.getvalue()
        chk("8 --confirm prints the tables only after the log is written", out_txt.find("written to the access log") > 0
            and out_txt.find("| 全部 |") > out_txt.find("written to the access log"))  # the "all" row of the table (split label kept)
        say("      (rehearsal headline: %s)" % lrows[-1]["result"][:120] if lrows else "")
        buf2 = io.StringIO()
        r_none = r_pre = None

        def _must_not_run(*a, **k):
            raise RuntimeError("refusal failed: scoring was reached")
        g = globals()
        saved_fn = (g["Panel"], g["evaluate"])
        try:
            # a broken refusal could not score anything here: Panel and evaluate
            # raise, and the registry and the log are the decoy's, in tmp
            g["Panel"], g["evaluate"] = _must_not_run, _must_not_run
            ph.REG, ph.LOG, ph._cache = reg_path, log_path, None
            with contextlib.redirect_stdout(buf2):
                r_none = run_confirm("no registration file")
                with clean_dir(REAL_CLEAN):     # the decoy rows still say "external" here
                    r_pre = run_confirm("self-test: must refuse before the rebuild (registered in %s)" % reg_path)
        finally:
            g["Panel"], g["evaluate"] = saved_fn
            ph.REG, ph.LOG, ph._cache = saved[0], saved[1], saved[2]
            globals()["REAL_CLEAN"] = saved[3]
        with open(log_path, encoding="utf-8-sig", newline="") as f:
            n_after = len(list(csv.DictReader(f)))
        chk("8 refuses, and writes no log row, without a registration file or before the rebuild (the decoy is still external in the real data)",
            r_none["code"] == 2 and r_pre is not None and r_pre["code"] == 2 and n_after == 2)
        # ---- 10 the paired mode (--compare-switch) through the same --confirm
        # path: a switch that never reaches a municipal letting (T1 is TxDOT
        # only) must give B == variant == the single run, letting by letting
        buf3 = io.StringIO()
        step("10 paired mode (--compare-switch txdot_c4d1, a no-op switch for municipal) through the --confirm path")
        try:
            ph.REG, ph.LOG, ph._cache = reg_path, log_path, None
            globals()["REAL_CLEAN"] = simC
            with clean_dir(simC), contextlib.redirect_stdout(buf3):
                out_p = run_confirm("self-test rehearsal: paired mode (registered in %s)" % reg_path, out_dir=tmp,
                                    compare="txdot_c4d1")
                r_bad = run_confirm("self-test: unknown switch (registered in %s)" % reg_path, out_dir=tmp,
                                    compare="no_such_switch")
        finally:
            ph.REG, ph.LOG, ph._cache = saved[0], saved[1], saved[2]
            globals()["REAL_CLEAN"] = saved[3]
        with open(log_path, encoding="utf-8-sig", newline="") as f:
            lrows3 = list(csv.DictReader(f))
        pb = {r["project_key"]: r for r in (out_p["tests"] or [])}
        pv = {r["project_key"]: r for r in (out_p["vtests"] or [])}
        same_p = (set(pb) == set(pv) == set(a2) and all(
            abs(pb[k]["p80"] - a2[k]["p80"]) < 1e-12 and abs(pv[k]["p80"] - a2[k]["p80"]) < 1e-12
            and abs(pb[k]["p50"] - a2[k]["p50"]) < 1e-12 and abs(pv[k]["p50"] - a2[k]["p50"]) < 1e-12
            for k in a2))
        pz = out_p["splits"]["全部"]["paired"] if out_p.get("splits") else {}  # "all" split
        chk("10 paired mode: with a no-op switch B = variant = the single run (letting by letting), every paired diff 0",
            out_p["code"] == 0 and same_p and all(
                (d["diff"] or 0.0) == 0.0 for d in pz.values()),
            "%d lettings" % len(pb))
        out3 = buf3.getvalue()
        chk("10 paired mode writes the 'start' and 'done' rows (the action names the paired mode) and prints the paired table only after the log; an unknown switch is refused and logs nothing",
            len(lrows3) == 4 and "配对" in lrows3[2]["action"] and "开始打分" in lrows3[2]["action"]  # log texts kept as logged: "paired", "start scoring"
            and "完成" in lrows3[3]["action"] and lrows3[3]["result"].startswith("n=")  # "done"
            and out3.find("written to the access log") > 0
            and out3.find("diff (variant - B)") > out3.find("written to the access log")
            and r_bad["code"] == 2, "log %d rows" % len(lrows3))
    # ---- 11 the coverage scope on the decoy: covered actual = low bid minus
    # the low bidder's own uncovered amounts; the sensitivity replay ran
    if e.COVERAGE_SCOPE:
        wu = [r for r in st2["tests"] + st2["train_recs"] if r.get("n_uncovered_lines")]
        chk("11 coverage scope: for lettings with uncovered lines, actual = low bid - the low bidder's amounts on the uncovered lines; "
            "for the others, actual = the low bid; the sensitivity version ran",
            all(abs(r["actual_low"] + r["uncovered_low"] - r["actual_low_full"]) < 1e-6
                for r in wu) and all(r["actual_low"] == r["actual_low_full"]
                                     for r in st2["tests"] if not r.get("n_uncovered_lines"))
            and st2.get("tests_med") is not None,
            "with uncovered lines %d (training + decoy); not covered as a whole: decoy %d, training %d; sensitivity %d" % (
                len(wu), st2["cov_test"].get("not_covered", 0),
                st2["cov_train"].get("not_covered", 0), len(st2.get("tests_med") or ())))
    chk("9 data/clean was not changed during the run", fingerprint() == fp0)
    chk("9 the real access log was not touched",
        (open(ph.LOG, "rb").read() if os.path.exists(ph.LOG) else None) == log0)
    say("\n" + ("ALL PASS" if ok[0] else "FAILURES ABOVE"))
    return ok[0]


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Score the locked public test set (exactly as production scores a new letting). "
                    "The real test set can only be opened with --confirm, which writes to the access log; "
                    "for development and testing use --decoy or --selftest.")
    ap.add_argument("--confirm", metavar="TEXT",
                    help="open the locked test set: state the decision being confirmed and the file it was registered in beforehand")
    ap.add_argument("--decoy", type=int, metavar="SEED",
                    help="rehearsal: 60 random groups of non-held-out public municipal lettings are held out instead; the test set is not touched and nothing is logged")
    ap.add_argument("--selftest", action="store_true",
                    help="instrumented self-test on the decoy set (default seed %d; change it with --decoy)" % SELFTEST_SEED)
    ap.add_argument("--allow-pre-rebuild", action="store_true",
                    help="(with --confirm only) run even though the data has not been rebuilt; written into the log")
    ap.add_argument("--per-letting", action="store_true",
                    help="(with --confirm only) also print every held-out letting; written into the log")
    ap.add_argument("--json", metavar="PATH", help="(with --decoy only) write the result here")
    ap.add_argument("--compare-switch", metavar="SWITCH",
                    help="paired mode: B = this pricing_rule switch off, variant = on, on the same held-out lettings "
                         "(with --decoy as a rehearsal, or with --confirm)")
    ap.add_argument("--full-scope", action="store_true",
                    help="old scope: price every line and score against the whole low bid (coverage_scope off)")
    a = ap.parse_args(argv)
    modes = sum(bool(x) for x in (a.confirm is not None, a.selftest,
                                  a.decoy is not None and not a.selftest))
    if a.confirm is not None and (a.decoy is not None or a.selftest):
        say("REFUSED: --confirm cannot be combined with --decoy / --selftest.")
        return 2
    if modes == 0:
        say("REFUSED: scoring the locked test set needs --confirm \"<the decision confirmed, and the file it is registered in>\"; "
            "for development and testing use --decoy SEED or --selftest.")
        return 2
    if a.selftest:
        return 0 if _selftest(a.decoy if a.decoy is not None else SELFTEST_SEED) else 1
    if a.compare_switch is not None and a.decoy is not None:
        return run_compare_decoy(a.decoy, a.compare_switch, a.json, a.full_scope)
    if a.decoy is not None:
        if a.full_scope:
            with e.pricing_rule(coverage_scope=False):
                return run_decoy(a.decoy, a.json)
        return run_decoy(a.decoy, a.json)
    return run_confirm(a.confirm, a.allow_pre_rebuild, a.per_letting,
                       compare=a.compare_switch, full_scope=a.full_scope)["code"]


if __name__ == "__main__":
    sys.exit(main())
