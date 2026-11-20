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

Usage:
    .venv/bin/python src/holdout_eval.py --selftest
    .venv/bin/python src/holdout_eval.py --decoy 7
    .venv/bin/python src/holdout_eval.py --confirm "确认 <决定>（登记在 reports/<file>.md）"
"""
import argparse
import bisect
import collections
import contextlib
import csv
import datetime
import hashlib
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
                 "yr": collections.Counter()})
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
    value), each with its schedule key and its own size (F1)."""
    items, keys = [], []
    lsz = lsz or {}
    for k in d["sched"]:
        fam, un, sz, bd, dn = k
        if e.is_owner_fixed(d["prices"].get(k, ()), fam, un, dn):
            items.append({"family": fam, "unit": un, "size_bucket": sz,
                          "quantity": d["sched"][k], "description": dn,
                          "amount": d["wamt"].get(k, 0.0), "key": k})
            keys.append(k)
            continue
        for qr in d["rows"][k]:
            items.append({"family": fam, "unit": un, "size_bucket": sz,
                          "quantity": qr, "description": dn, "key": k,
                          "size_in": lsz.get((pk,) + k)})
            keys.append(k)
    return items, keys


def price_forward(pks, lets, avail, tab, kinds, tok_inv, findex, fdisc, cov, audit=None,
                  lsz=None, size_cells=None, pools=None, targets=None):
    """estimate() on ForwardTables at each letting's own date, calibrate_forward's
    loop line for line.  Returns {key: record} for the lettings that pass.
    lsz / size_cells: F1's line sizes and size cells; pools: V2's comparables
    pools; targets: a held-out letting's own owner and work type."""
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
                                          pooled, muni, size_cells, pools)
        tgt = (targets or {}).get(pk) or {"key": pk}
        r = e.estimate(ft, items, n, mk, yr or None, target=tgt)
        if r.get("expected_low") is None:
            cov[pk] = "no_estimate"
            continue
        ok = collections.defaultdict(lambda: True)
        for k, t in zip(keys, r["trace"]):
            if not (t.get("fixed_amount") or t.get("price")):
                ok[k] = False
        pr = sum(1 for k in sched if ok[k])
        tot, fixed = r["market_total"], r["fixed_total"]
        if pr < 0.9 * len(sched) or tot <= 0:
            cov[pk] = "under_90pct_priced"
            continue
        fa = r["discount"]["f"]
        cov[pk] = "scored"
        cm = r.get("comparables") or {}
        if audit is not None:
            audit.priced(pk, D, r, size_cells)
        out[pk] = {"project_key": pk, "market": mk, "date": D, "n_bidders": n,
                   "bid_part": r["bid_part"], "owner_fixed": fixed, "actual_low": wt,
                   "n_items": len(sched), "bid_part_lines": tot * fa,
                   "cmp": cm.get("cmp"), "n_comps": cm.get("n_comps"),
                   "shape_items": sum(1 for t in r["trace"] if t.get("qty_factor")),
                   "shape_dollars": sum((t.get("price") or 0) * (t.get("quantity") or 0)
                                        for t in r["trace"] if t.get("qty_factor"))}
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
    step("训练参考格：%d 行价格（已去掉留出文件）" % len(train_pool))
    cells = e._reference_cells(train_pool, train_sizes, avail)
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
    step("按日期向前定价：训练 %d 个市政招标，测试 %d 个" % (len(train_ids), len(test_ids)))
    # a held-out schedule is not in the pool: its own owner and work type
    # (known when it is estimated) enter its similarity, nothing else
    wt_t = e.comparables.schedule_worktypes(e.CLEAN, test_ids, e._f) if pools else {}
    targets = {pk: {"key": pk, "owner": e.comparables.owner_id(pk, MARKET, ""),
                    "work_type": wt_t.get(pk, "")} for pk in test_ids}
    cov = {}
    recs = price_forward(train_ids + test_ids, lets, avail, tab, kinds, cells["tok_inv"],
                         findex, fdisc, cov, audit, lsz, cells["size"], pools, targets)
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
    rows = [("全部", tests)]
    for y in YEARS:
        rows.append((str(y), [r for r in tests if r["year"] == str(y)]))
    extra = sorted({r["year"] for r in tests} - {str(y) for y in YEARS})
    for y in extra:
        rows.append((y, [r for r in tests if r["year"] == y]))
    rows.append(("侵蚀/排水", [r for r in tests if r["erosion_drainage"]]))
    rows.append(("其他工程", [r for r in tests if not r["erosion_drainage"]]))
    out = []
    for label, rs in rows:
        m = metrics(rs)
        out.append({"group": label, "metrics": m,
                    "ci": bootstrap(rs, label) if m else {}})
    return out


COLS = (("p50_ratio", "P50 中位比", "r"), ("p50_abs", "P50 误差中位", "p"),
        ("mean_abs_log", "平均对数误差", "r"), ("under", "P80 低于低价", "p"),
        ("in_band", "P80 在[1.00,1.13]", "p"), ("above", "P80 >1.13", "p"),
        ("overshoot", "高估时中位超出", "p"))


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
    lines = ["| 分组 | 个数 | " + " | ".join(c[1] for c in COLS) + " |",
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
        return "没有打出分的招标"
    under_n = round(m["under"] * m["n"])
    return ("n=%d；P80 低于低价 %d/%d=%s；P80 在[1.00,1.13] %s；P50 中位比 %s；"
            "P50 误差中位 %s；高估时中位超出 %s" % (
                m["n"], under_n, m["n"], _cell(m["under"], ci.get("under"), "p"),
                _cell(m["in_band"], ci.get("in_band"), "p"),
                _cell(m["p50_ratio"], ci.get("p50_ratio"), "r"),
                _cell(m["p50_abs"], ci.get("p50_abs"), "p"),
                _cell(m["overshoot"], ci.get("overshoot"), "p")))


GLOSSARY = """说明：
  P50 中位比    = 估价 P50 ÷ 实际最低价，取中位数；1.000 表示正中。
  P50 误差中位  = |P50 ÷ 实际最低价 − 1| 的中位数。
  平均对数误差  = |ln(P50 ÷ 实际最低价)| 的平均值，越小越准。
  P80 低于低价  = P80 仍低于实际最低价的比例。用户底线：不超过 20%%。
  P80 在[1.00,1.13] = 高估、但不超过 13%% 的比例。越高越好。
  P80 >1.13     = 高估超过 13%% 的比例。
  高估时中位超出 = P80 ≥ 低价的那些招标里，超出部分的中位数。
  方括号 = 90%% 区间（按文件组有放回重抽 %d 次）。每年只有十来个招标，分年的区间很宽；
          某个比例正好是 0%% 或 100%% 时，重抽出来的区间也只能是 0 或 100，这不等于有把握。""" % BOOT_N


def shape_text(tests, what):
    n = sum(r["shape_items"] for r in tests)
    d = sum(r["shape_dollars"] for r in tests)
    tot = sum(r["bid_part"] for r in tests)
    return ("发布的数量形状表（不是按日期向前拟合的，生产的向前校准也这样用）用在 %d 个%s清单项上，"
            "约占这些招标估价金额的 %.2f%%。" % (n, what, 100.0 * d / tot if tot else 0.0))


def coverage_text(cov):
    names = {"scored": "打出分", "not_municipal": "不是市政",
             "not_one_winner_or_under_2_bidders": "没有唯一中标人或不足 2 家投标",
             "no_positive_total": "总价不为正",
             "under_%d_items" % e.MIN_SCHEDULE_ITEMS: "少于 %d 个清单项" % e.MIN_SCHEDULE_ITEMS,
             "no_date": "没有日期", "no_estimate": "无法估价",
             "under_90pct_priced": "能定价的清单项不足 90%",
             "under_%d_prior_lettings" % e.FORWARD_MIN_PRIOR:
                 "之前的训练招标不足 %d 个，没有常数" % e.FORWARD_MIN_PRIOR,
             "no_priced_rows": "面板里没有可用价格行"}
    return "，".join("%s %d" % (names.get(k, k), v) for k, v in
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
    the scored set, which would otherwise reshuffle every year's draw)."""
    with e.pricing_rule(size_fallback=False, removal_identity=False, comparables=False):
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
        step("第 1 步：测试集为空，给全部训练招标向前定价（抽诱饵集用）")
        st1 = evaluate(panel, tab, base, make_excluded(panel), set(), lambda pk: (pk, False))
        step("抽诱饵用的招标池：V2+F1+F2 之前的规则向前打出分的训练招标")
        decoy = draw_decoy(seed, panel, draw_population(panel, tab, base))
    excluded = make_excluded(panel, decoy["files"])
    test_keys = {pk for pk in panel.avail if _file_of(pk) in decoy["files"]}
    step("第 2 步：诱饵集 %d 组 %d 个文件留出，重建训练表并打分"
         % (len(decoy["groups"]), len(decoy["files"])))
    st2 = evaluate(panel, tab, base, excluded, test_keys, decoy_meta(decoy))
    return st1, decoy, st2


def run_decoy(seed, json_path=None):
    say("【演练 · 诱饵集，种子 %d】这不是锁定的测试集；不写访问日志。" % seed)
    fp0 = fingerprint()
    step("读取 master_panel（锁定测试集的行读到就丢掉）")
    panel = Panel()
    tab = e.Tables()
    step("建按日期的价格指数（首轮分组后去掉留出项目）")
    base = e._ForwardIndex(panel.avail)
    st1, decoy, st2 = run_decoy_core(seed, panel, tab, base)
    summary = summarize(st2["tests"])
    changed = fingerprint() != fp0
    say()
    say("=== 演练结果（诱饵集，种子 %d；不是测试集）===" % seed)
    n_ed = sum(u["erosion_drainage"] for u in decoy["groups"].values())
    say("诱饵集：%d 个文件组（每个“年份×工程类型”格子和锁定集一样多），%d 个文件；"
        "它们整组不进训练。" % (len(decoy["groups"]), len(decoy["files"])))
    say("其中侵蚀/排水 %d 组（按年 %s）。" % (n_ed, dict(sorted(collections.Counter(
        u["year"] for u in decoy["groups"].values() if u["erosion_drainage"]).items()))))
    if decoy["short_years"]:
        say("注意：这些年份剩下的组不够，按年实际抽到：%s" % decoy["short_years"])
    say("训练：%d 个市政招标向前打出分（不留诱饵时 %d 个）。"
        % (st2["train_scored"], st1["train_scored"]))
    say("诱饵招标：%s。" % coverage_text(st2["cov_test"]))
    say(shape_text(st2["tests"], "诱饵"))
    say()
    say(table(summary))
    say()
    say("总述（诱饵集）：" + headline(summary))
    say()
    say(GLOSSARY)
    if changed:
        say("\n警告：运行期间 data/clean 的输入文件变了（可能有人在重建），这次结果不可信。")
    json_path = json_path or os.path.join(SCRATCH, "decoy_%d.json" % seed)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "decoy", "seed": seed,
                   "decoy": {"groups": decoy["groups"], "files": sorted(decoy["files"])},
                   "coverage_test": dict(st2["cov_test"]),
                   "coverage_train": dict(st2["cov_train"]),
                   "train_scored": st2["train_scored"],
                   "summary": summary, "per_letting": st2["tests"],
                   "data_changed_during_run": changed}, f, ensure_ascii=False, indent=1,
                  default=str)
    say("\n逐个诱饵招标的结果已写入 %s" % os.path.relpath(json_path, ROOT))
    return 0 if not changed else 1


# ---------------------------------------------------------------------------
# the real set
def registration_files(text):
    """Existing files named in the --confirm text (absolute, ~, or relative to
    the project), with their md5 -- the registration must exist before opening."""
    found = []
    for tok in re.split(r"[\s,;，；、（）()【】\[\]<>「」\"'“”‘’：:]+", text or ""):
        tok = tok.strip(".。")
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
                if "holdout_eval --confirm 开始" in (r.get("action") or "")]


def run_confirm(text, allow_pre_rebuild=False, per_letting=False, out_dir=None):
    """Score the locked set.  Returns {"code": exit code, "tests": scored
    lettings or None}; the selftest rehearses it with a decoy standing in."""
    text = (text or "").strip()
    regs = registration_files(text)
    if not text or not regs:
        say("拒绝运行：--confirm 必须写清楚这次要确认的决定，以及它事先登记在哪个文件里"
            "（要写出一个已经存在的文件路径，例如 reports/xxx.md）。没找到这样的文件。")
        return {"code": 2, "tests": None}
    if e.CLEAN != REAL_CLEAN:
        say("拒绝运行：--confirm 只能读真正的 data/clean。")
        return {"code": 2, "tests": None}
    prev = previous_openings()
    say("【锁定测试集】这次运行会打开它，并写入 %s。" % os.path.relpath(ph.LOG, ROOT))
    say("要确认的决定：%s" % text)
    for r in regs:
        say("登记文件：%s（md5 %s，修改于 %s）" % (r["path"], r["md5"][:10], r["modified"]))
    say("此前用本脚本打开过 %d 次（日志里的行）。" % len(prev))
    step("检查数据状态（只看 source_set 和键）")
    st = data_state()
    say("留出文件的行：%s；没登记却标成 public_test 的键：%d"
        % (st["rows_by_source_set"], st["stray_public_test_keys"]))
    if st["pre_rebuild"] and not allow_pre_rebuild:
        say("拒绝运行：数据还没重建——留出文件的行在 master_panel 里仍标为 external。\n"
            "重建后再打开测试集：重建会让发布的数量形状表、词表等也去掉这些文件。\n"
            "训练端本脚本已经按 public_holdout.is_holdout 去掉了它们；如确要现在运行，"
            "加 --allow-pre-rebuild（会写进日志）。没有打开测试集，没有写日志。")
        return {"code": 2, "tests": None}
    code = "holdout_eval.py %s, estimate_low_bid.py %s" % (
        _md5_short(os.path.abspath(__file__)), _md5_short(os.path.join(ROOT, "src",
                                                                         "estimate_low_bid.py")))
    flags = []
    if st["pre_rebuild"]:
        flags.append("重建前的数据（--allow-pre-rebuild）")
    if per_letting:
        flags.append("含逐个招标明细（--per-letting）")
    decision = text + " | 登记文件：" + "；".join(
        "%s md5 %s" % (os.path.relpath(r["path"], ROOT) if r["path"].startswith(ROOT)
                       else r["path"], r["md5"]) for r in regs)
    log_row("holdout_eval --confirm 开始打分（%s%s）" % (code, ("；" + "；".join(flags))
                                                     if flags else ""),
            decision, "（运行中）")
    fp0 = fingerprint()
    try:
        step("读取 master_panel（含留出文件的行）")
        panel = Panel(read_real=True)
        tab = e.Tables()
        step("建按日期的价格指数（首轮分组后去掉留出项目）")
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
        res = evaluate(panel, tab, base, excluded, set(panel.real), meta)
        summary = summarize(res["tests"])
        changed = fingerprint() != fp0
    except BaseException as ex:
        log_row("holdout_eval --confirm 中断", decision, "出错：%s: %s" % (type(ex).__name__, ex))
        raise
    head = headline(summary)
    result = head + "；覆盖：" + coverage_text(res["cov_test"])
    if changed:
        result = "运行中 data/clean 被改动，结果作废。" + result
    log_row("holdout_eval --confirm 完成", decision, result)
    say()
    say("=== 锁定测试集结果（已写入访问日志）===")
    say("留出招标：%s。" % coverage_text(res["cov_test"]))
    got = {r["project_key"] for r in res["tests"]}
    say("抽样时登记的 %d 个招标键：这次打出分 %d 个；打出分但不在登记键里的 %d 个"
        "（重建后键名可能变了）。" % (len(reg_keys), len(got & reg_keys), len(got - reg_keys)))
    if panel.stray_public_test:
        say("注意：%d 个键标成 public_test 但登记表里没有它们的文件，也按留出处理。"
            % len(panel.stray_public_test))
    say("训练：%d 个市政招标向前打出分。" % res["train_scored"])
    say(shape_text(res["tests"], "留出"))
    say()
    say(table(summary))
    say()
    say("总述：" + head)
    say()
    say(GLOSSARY)
    if per_letting:
        say("\n逐个招标：")
        for r in sorted(res["tests"], key=lambda r: (r["date"], r["project_key"])):
            say("  %s  %s  %s  P50/低价 %.3f  P80/低价 %.3f" % (
                r["date"], "侵蚀/排水" if r["erosion_drainage"] else "其他",
                r["project_key"], r["p50"], r["p80"]))
    if changed:
        say("\n警告：运行期间 data/clean 的输入文件变了（可能有人在重建），这次结果作废。")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_dir or SCRATCH
    path = os.path.join(out_dir, "confirm_%s.json" % stamp)
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mode": "confirm", "confirm": text, "registration": regs,
                   "data_state": st, "coverage_test": dict(res["cov_test"]),
                   "train_scored": res["train_scored"], "summary": summary,
                   "per_letting": res["tests"] if per_letting else "not written",
                   "data_changed_during_run": changed}, f, ensure_ascii=False, indent=1,
                  default=str)
    say("\n汇总已写入 %s" % os.path.relpath(path, ROOT))
    return {"code": 0 if not changed else 1, "tests": res["tests"]}


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
        self.n["失败"] += 1
        if len(self.fails) < 25:
            self.fails.append(msg)

    def static_cells(self, cells):
        for kind in ("tok", "band", "fam"):
            for k, by in cells[kind].items():
                self.n["检查的参考格"] += 1
                leak = [pk for pk in by if self.held_out(pk)]
                if leak:
                    self.bad("参考格 %s %r 含留出招标 %s" % (kind, k, leak[:3]))

    def ref_row(self, kind, k, by, D, r):
        self.n["发出的参考行"] += 1
        if self.cur_D is not None and D != self.cur_D:
            self.bad("参考表的日期 %s 不是定价日期 %s" % (D, self.cur_D))
        st = self._cells.get((kind, k))
        if st is None:
            st = self._cells[(kind, k)] = sorted(self.avail.get(pk, e.NEVER) for pk in by)
        want = bisect.bisect_left(st, D)
        got = r["n_projects"] if r is not None else 0
        if got != want:
            self.bad("参考格 %s %r 在 %s：用了 %d 个招标，%s 之前只有 %d 个"
                     % (kind, k, D, got, D, want))

    def heavy(self, pks):
        self.n["参考中位数（逐个招标核对）"] += 1
        D = self.cur_D
        for pk in pks:
            self.n["核对的参考招标"] += 1
            if D is None or self.avail.get(pk, e.NEVER) >= D:
                self.bad("参考价用了 %s（%s），定价日期 %s"
                         % (pk, self.avail.get(pk), D))
            if self.held_out(pk):
                self.bad("参考价用了留出招标 %s" % pk)

    def fitted(self, size_cells, pools):
        """V2+F1+F2's fitted sets, as built: no held-out letting in any F1
        size cell, in a comparables pool or in its beta/gamma rows."""
        if size_cells is not None:
            self.n["F1 尺寸格"] += len(size_cells.cells)
            leak = sorted(pk for pk in size_cells.lettings() if self.held_out(pk))
            if leak:
                self.bad("F1 尺寸格含留出招标 %s" % leak[:3])
        for mk, pool in sorted((pools or {}).items()):
            keys = pool.keys()
            self.n["可比池招标"] += len(keys)
            leak = [pk for pk in keys if self.held_out(pk)]
            if leak:
                self.bad("可比池 %s 含留出招标 %s" % (mk, leak[:3]))
            leak = sorted(pk for pk in pool.bg.keys_before(e.NEVER) if self.held_out(pk))
            if leak:
                self.bad("可比池 %s 的 beta/gamma 含留出招标 %s" % (mk, leak[:3]))

    def priced(self, pk, D, r, size_cells):
        """One scored letting: its chosen comparables, the lettings behind
        beta/gamma at its cutoff, and every F1 line's reference size and
        slope k come from training lettings dated strictly before D."""
        cm = r.get("comparables")
        if cm is not None:
            self.n["核对的可比法定价"] += 1
            for c, cd in zip(cm["comps"], cm["comp_dates"]):
                self.n["核对的可比招标"] += 1
                if self.held_out(c):
                    self.bad("%s 的可比招标是留出招标 %s" % (pk, c))
                if cd >= D or self.avail.get(c, e.NEVER) >= D:
                    self.bad("%s 的可比招标 %s 不早于定价日期 %s" % (pk, c, D))
            if cm.get("date") != D or cm.get("bg_cutoff", D) > D:
                self.bad("%s 的可比法日期 %s / beta-gamma 截断 %s 不对（定价日期 %s）"
                         % (pk, cm.get("date"), cm.get("bg_cutoff"), D))
        for t in r["trace"]:
            f = t.get("f1")
            if not f or "ref_key" not in f:
                continue
            self.n["核对的 F1 子目"] += 1
            rk = tuple(f["ref_key"])
            used = set(size_cells.ref_lettings(rk, D)) | size_cells.k_lettings(rk[0], rk[1], D)
            bad = [q for q in used if self.held_out(q) or self.avail.get(q, e.NEVER) >= D]
            if bad:
                self.bad("%s 的 F1 子目 %r 的参照尺寸/k 用了 %s" % (pk, rk, sorted(bad)[:3]))

    def constants(self, rec, c, train_recs, const):
        self.n["核对的测试常数"] += 1
        D = rec["date"]
        prior = [r for r in train_recs if r["date"] < D]          # filtered afresh
        if len(prior) != c["n_prior"] or set(r["project_key"] for r in prior) != set(
                c["prior_keys"]):
            self.bad("常数的训练集不对：%s" % rec["project_key"])
        late = [k for k in c["prior_keys"] if self.avail.get(k, e.NEVER) >= D]
        if late:
            self.bad("常数用了日期不早于 %s 的招标 %s" % (D, late[:3]))
        leak = [k for k in c["prior_keys"] if self.held_out(k)]
        if leak:
            self.bad("常数用了留出招标 %s" % leak[:3])
        trip4 = [(r["date"], r["bid_part"], r["owner_fixed"], r["actual_low"]) for r in prior]
        g = e._not_under_guard(trip4, e.GUARD_GAMMA[MARKET])
        if abs(g["alpha"] - c["alpha"]) > 1e-12:
            self.bad("守门水平不一致 %s：%r vs 生产 %r" % (rec["project_key"], c["alpha"],
                                                    g["alpha"]))
        lc = e._centre([t[1:] for t in trip4])
        if abs(lc - c["lc"]) > 1e-12:
            self.bad("居中系数不一致 %s" % rec["project_key"])
        cen = sorted((cc * lc + fx) / w for _, cc, fx, w in trip4)
        if abs(cen[int(g["alpha"] * (len(cen) - 1))] - c["p"]) > 1e-12:
            self.bad("P80 除数不一致 %s" % rec["project_key"])


class _AuditKind(e._FwdKind):
    def __init__(self, cells_kind, kind, minn, avail, audit):
        super().__init__(cells_kind, kind, minn, avail)
        self.audit = audit

    def row(self, k, D):
        r = super().row(k, D)
        self.audit.ref_row(self.kind, k, self.c[k], D, r)
        return r


class _AuditIndex(TrainIndex):
    def __init__(self, base, excluded, audit):
        super().__init__(base, excluded)
        self.audit = audit
        for arm in self.ARMS:
            leak = [pk for pk in self.S1[arm].project_key.unique() if audit.held_out(pk)]
            if leak:
                audit.bad("指数 %s 臂含留出招标 %s" % (arm, leak[:3]))
            sd = self.SD[arm]
            if len(sd) > 1 and not (sd[:-1] <= sd[1:]).all():
                audit.bad("指数 %s 臂没按日期排好" % arm)

    def at(self, D):
        res = super().at(D)
        a = self.audit
        a.n["指数调用"] += 1
        if a.cur_D is not None and D != a.cur_D:
            a.bad("指数日期 %s 不是定价日期 %s" % (D, a.cur_D))
        for arm, k in zip(self.ARMS, self._sig):
            sd = self.SD[arm]
            if (k > 0 and not sd[k - 1] < D) or (k < len(sd) and not sd[k] >= D):
                a.bad("指数 %s 臂在 %s 的截断不对" % (arm, D))
        if (a.full_dates is None or D in a.full_dates) and D not in a._idx_done:
            a._idx_done.add(D)
            a.n["从头重算的指数"] += 1
            for arm in self.ARMS:
                s1 = self.S1[arm]
                again = e._chain(s1[s1.D < D], adjacent=(arm != "_default"))
                if (again or None) != res.get(arm):
                    a.bad("指数 %s 臂在 %s 从头重算不一致" % (arm, D))
        return res


class _AuditDiscount(TrainDiscount):
    def __init__(self, avail, excluded, audit):
        super().__init__(avail, excluded)
        self.audit = audit
        leak = [x for x in self.LD.letting_id if x and audit.held_out(x)]
        if leak:
            audit.bad("折扣表含留出招标 %s" % leak[:3])
        if len(self.LDD) > 1 and not (self.LDD[:-1] <= self.LDD[1:]).all():
            audit.bad("折扣表没按日期排好")

    def at(self, D):
        res = super().at(D)
        a = self.audit
        a.n["折扣调用"] += 1
        if a.cur_D is not None and D != a.cur_D:
            a.bad("折扣日期 %s 不是定价日期 %s" % (D, a.cur_D))
        k, sd = self._k, self.LDD
        if (k > 0 and not sd[k - 1] < D) or (k < len(sd) and not sd[k] >= D):
            a.bad("折扣在 %s 的截断不对" % D)
        if (a.full_dates is None or D in a.full_dates) and D not in a._disc_done:
            a._disc_done.add(D)
            a.n["从头重算的折扣"] += 1
            if self._fit(self.LD[self.LD.D < D]) != res:
                a.bad("折扣在 %s 从头重算不一致" % D)
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
        say("%s  %s%s" % ("通过" if good else "失败", name, ("  —— " + detail) if detail else ""))

    say("【自检】在诱饵集（种子 %d）上运行；不打开锁定测试集，不写访问日志。" % seed)
    fp0 = fingerprint()
    log0 = open(ph.LOG, "rb").read() if os.path.exists(ph.LOG) else None
    step("读取 master_panel（锁定测试集的行读到就丢掉）")
    panel = Panel()
    tab = e.Tables()
    base = e._ForwardIndex(panel.avail)
    chk("0 读面板时丢掉了锁定测试集", not any(ph.is_holdout(r[1]) for r in panel.pool)
        and not any(ph.is_holdout(k) for k in panel.avail),
        "丢掉 %d 行，%d 个键" % (sum(panel.real_rows.values()), len(panel.real)))

    # ---- stage 1 + the decoy, then stage 2 with every hand-out instrumented
    step("第 1 步：测试集为空")
    st1 = evaluate(panel, tab, base, make_excluded(panel), set(), lambda pk: (pk, False))
    decoy = draw_decoy(seed, panel, draw_population(panel, tab, base))
    excluded = make_excluded(panel, decoy["files"])
    test_keys = {pk for pk in panel.avail if _file_of(pk) in decoy["files"]}
    audit = Audit(excluded, panel.avail, test_keys,
                  full_dates={panel.avail[k] for k in test_keys})
    with audit_medians(audit):
        step("第 2 步：诱饵集留出，所有发放点都插桩核对")
        st2 = evaluate(panel, tab, base, excluded, test_keys, decoy_meta(decoy), audit)

    reg_files = ph.holdout_files()
    chk("1 诱饵集和锁定测试集没有共同文件", not (decoy["files"] & reg_files)
        and not any(ph.is_holdout(f) for f in decoy["files"]),
        "%d 组，%d 个文件" % (len(decoy["groups"]), len(decoy["files"])))
    by_year = collections.Counter(u["year"] for u in decoy["groups"].values())
    strata = collections.Counter((u["year"], u["erosion_drainage"])
                                 for u in decoy["groups"].values())
    ed = sum(u["erosion_drainage"] for u in decoy["groups"].values())
    tgt = decoy["target"]
    chk("1 诱饵集每个“年份×工程类型”格子的组数和锁定集一样（池子不够的格子除外）",
        all(strata[k] == n or (k[0] in decoy["short_years"] and strata[k] <= n)
            for k, n in tgt.items()) and sum(strata.values()) >= 0.8 * sum(tgt.values()),
        "按年 %s，侵蚀/排水 %d 组；锁定集 %d 组" % (
            dict(sorted(by_year.items())), ed, sum(tgt.values())))
    chk("1 诱饵招标大多打出分", len(st2["tests"]) >= 50, coverage_text(st2["cov_test"]))
    a = audit
    chk("2 插桩：没有留出招标、也没有不早于定价日期的招标进入价格和常数",
        a.n["失败"] == 0, "；".join(a.fails[:5]) if a.fails else "")
    say("      核对次数：%s" % "，".join("%s %d" % kv for kv in sorted(a.n.items())
                                    if kv[0] != "失败"))
    chk("2 V2+F1+F2 的拟合量都插桩核对过：可比池、beta/gamma、F1 尺寸格不含留出招标；"
        "每个打分招标选中的可比招标、每个 F1 子目的参照尺寸和 k 都只来自定价日期之前的训练招标",
        a.n["可比池招标"] > 0 and a.n["F1 尺寸格"] > 0 and a.n["核对的可比法定价"] > 0
        and a.n["核对的可比招标"] > 0 and a.n["失败"] == 0,
        "可比池 %d 个招标，尺寸格 %d 个；可比法定价 %d 次、可比招标 %d 个、F1 子目 %d 个" % (
            a.n["可比池招标"], a.n["F1 尺寸格"], a.n["核对的可比法定价"],
            a.n["核对的可比招标"], a.n["核对的 F1 子目"]))
    sizes_ok = st2["size_cells"] is not None and not (
        st2["size_cells"].lettings() & set(test_keys)) and not any(
        excluded(k) for k in st2["size_cells"].lettings())
    pool_ok = all(not excluded(k) and k not in test_keys
                  for p_ in (st2["pools"] or {}).values() for k in p_.keys())
    chk("2 训练端的可比池和 F1 尺寸格里没有诱饵文件的招标（按文件名重查）",
        sizes_ok and pool_ok and bool(st2["pools"]),
        "可比池 %d 个，尺寸格招标 %d 个" % (
            sum(len(p_.keys()) for p_ in (st2["pools"] or {}).values()),
            len(st2["size_cells"].lettings()) if st2["size_cells"] else 0))
    chk("2 每个诱饵招标的常数都和生产函数（_not_under_guard / _centre）重算一致",
        a.n["核对的测试常数"] == len(st2["tests"]), "%d 个" % a.n["核对的测试常数"])
    tdates = {r["date"] for r in st2["tests"]}
    chk("2 每个诱饵日期的指数和折扣都按日期筛选从头重算过，和截断前缀一致",
        tdates <= a._idx_done and tdates <= a._disc_done,
        "诱饵日期 %d 个；重算指数 %d 次，折扣 %d 次" % (
            len(tdates), a.n["从头重算的指数"], a.n["从头重算的折扣"]))
    dk_idx = {arm: len({k for k in v if _file_of(k) in decoy["files"]})
              for arm, v in st2["index_removed"].items()}
    dk_disc = [lid for lid, _ in st2["discount_removed"] if _file_of(lid) in decoy["files"]]
    chk("2 诱饵项目确实从指数和折扣里去掉了",
        sum(dk_idx.values()) > 0 and len(dk_disc) > 0,
        "指数去掉诱饵项目 %s；折扣去掉诱饵招标 %d 个" % (dk_idx, len(dk_disc)))
    shape = sum(r["shape_items"] for r in st2["tests"])
    say("      发布的数量形状表（非向前，生产的向前校准也这样用）用在 %d 个诱饵清单项上。" % shape)

    # negative controls: the instrument must be able to fail
    neg = Audit(lambda pk: pk == "LEAK", {"A": "2024-01-01", "LEAK": "2023-01-01",
                                         "LATE": "2025-06-01"}, {"T"})
    neg.cur_D = "2025-01-01"
    neg.static_cells({"tok": {}, "band": {}, "fam": {("x", "EA", ""): {"LEAK": [], "A": []}}})
    n1 = neg.n["失败"]
    neg.heavy(["A", "LATE"])
    n2 = neg.n["失败"]
    neg.ref_row("fam", ("x", "EA", ""), {"A": [], "LATE": []}, "2025-01-01", {"n_projects": 2})
    chk("3 反向对照：插桩能抓到故意放进去的泄漏", n1 == 1 and n2 == 2 and neg.n["失败"] == 3,
        "留出格 %d，晚日期 %d，多数了招标 %d" % (n1, n2 - n1, neg.n["失败"] - n2))
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
    n3 = neg.n["失败"]
    neg.priced("T", "2025-01-01", {"comparables": {"comps": ["A", "LATE"],
                                                  "comp_dates": ["2024-01-01", "2025-06-01"],
                                                  "date": "2025-01-01", "bg_cutoff": "2023-01-01"},
                                  "trace": [{"f1": {"ref_key": ("water_pipe", "LF", "8-12")}}]},
               si_)
    n4 = neg.n["失败"]
    chk("3 反向对照：V2+F1+F2 的插桩也能抓到故意放进去的泄漏（尺寸格、可比池、beta/gamma、"
        "晚日期的可比招标、F1 参照）", n3 - 3 == 3 and n4 - n3 == 2,
        "尺寸格/可比池/beta-gamma %d，可比招标和 F1 子目 %d" % (n3 - 3, n4 - n3))

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
    chk("4 翻转对照：最早诱饵日期 %s 及以前的训练招标，留出前后结果完全一样" % d_min,
        all(k in s2 and _same_rec(s1[k], s2[k]) for k in early), "%d 个" % len(early))
    chk("4 之后的训练招标有变化（留出确实起作用）", len(moved) > 0,
        "%d 个里 %d 个变了；另有 %d 个因少了参考价而打不出分"
        % (len(later), len(moved), len(gone_other)))
    chk("4 诱饵文件里的招标全部离开了训练端",
        not any(_file_of(k) in decoy["files"] for k in s2),
        "原在训练端 %d 个" % len(gone_decoy))

    # ---- a simulated rebuild: production code on it
    mdisc = TrainDiscount(panel.avail, make_excluded(panel))   # real data, is_holdout route
    midx = TrainIndex(base, make_excluded(panel))              # real data, is_holdout route
    with tempfile.TemporaryDirectory(prefix="holdout_eval_sim_") as tmp:
        simA = os.path.join(tmp, "A")
        step("模拟重建 A：锁定测试集的行标成 public_test（临时目录，结束自动清掉）")
        nA = simulate_rebuild(simA, ph.is_holdout)
        chk("5 模拟重建 A 把锁定测试集仍标 external 的行都改标了",
            nA["master_panel.csv"] == panel.real_rows.get("external", 0),
            "%s；面板里锁定测试集的行 %s" % (dict(nA), dict(panel.real_rows)))
        with clean_dir(simA):
            step("生产代码在 A 上：_reference_pool / _letting_dates / _ForwardDiscount / "
                 "_ForwardIndex")
            prod_pool = e._reference_pool()
            chk("5 本脚本读的价格池 = 生产的 _reference_pool（逐行逐列）",
                prod_pool == panel.pool, "%d vs %d 行" % (len(prod_pool), len(panel.pool)))
            del prod_pool
            prod_avail = e._letting_dates()
            chk("5 招标日期 = 生产的 _letting_dates", prod_avail == panel.avail,
                "%d 个键" % len(prod_avail))
            pdisc = e._ForwardDiscount(prod_avail)
            cols = ["src", "D", "n_bidders", "low", "median", "lom"]
            chk("5 折扣输入（真实数据上按登记表去掉）= 生产 _ForwardDiscount（A 上按 source_set 去掉）",
                pdisc.LD[cols].equals(mdisc.LD[cols]), "%d 行" % len(pdisc.LD))
            pidx = e._ForwardIndex(prod_avail)
            chk("5 指数第一轮（真实数据上按登记表去掉）= 生产 _ForwardIndex（A 上按 source_set 去掉）",
                all(pidx.S1[arm].equals(midx.S1[arm]) for arm in pidx.ARMS),
                "，".join("%s %d 行" % (arm, len(pidx.S1[arm])) for arm in pidx.ARMS))
            del pidx, pdisc
            step("生产的 calibrate_forward(write=False) 在 A 上（给所有市场的招标向前定价，较慢）")
            pl = []
            rows_f, _ = e.calibrate_forward(write=False, per_letting=pl)
        prod = {r["project_key"]: r for r in pl if r["market"] == MARKET}
        mine = {r["project_key"]: r for r in st1["train_recs"]}
        diff = [k for k in prod if k not in mine or not _same_rec(prod[k], mine[k])]
        extra = [k for k in mine if k not in prod]
        chk("6 测试集为空时，训练端 = calibrate_forward 的市政向前记录（逐个招标）",
            not diff and not extra and len(prod) > 0,
            "%d 个招标；不同 %d，多出 %d" % (len(prod), len(diff), len(extra)))
        rowm = next((r for r in rows_f if r["market"] == MARKET), None)
        c_end = st1["const"].at("9999-12-30")
        g_end = e._not_under_guard([(r["date"], r["bid_part"], r["owner_fixed"],
                                     r["actual_low"]) for r in st1["train_recs"]],
                                   e.GUARD_GAMMA[MARKET])
        chk("6 全部数据之后的常数 = calibrate_forward 写出的市政常数",
            rowm is not None and round(c_end["lc"], 6) == rowm["level_correction"]
            and round(c_end["alpha"], 6) == rowm["not_under_level"]
            and round(c_end["p"], 6) == rowm["p_not_under"]
            and g_end["n_scored"] == rowm["guard_scored"],
            "居中 %.6f / %s，水平 %.6f / %s，除数 %.6f / %s" % (
                c_end["lc"], rowm and rowm["level_correction"], c_end["alpha"],
                rowm and rowm["not_under_level"], c_end["p"], rowm and rowm["p_not_under"]))

        # ---- the post-rebuild path for test lettings, on the decoy
        simB = os.path.join(tmp, "B")
        step("模拟重建 B：锁定测试集和诱饵文件的行都标成 public_test")
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
        chk("7 重建后的读法（诱饵行标成 public_test）诱饵分数完全一样", same,
            "%d vs %d 个；B 改标 %s" % (len(a2), len(b2), dict(nB)))
        t2 = {r["project_key"]: r for r in st2["train_recs"]}
        tB = {r["project_key"]: r for r in stB["train_recs"]}
        chk("7 重建后的读法训练端也完全一样", set(t2) == set(tB) and all(
            _same_rec(t2[k], tB[k]) for k in t2), "%d 个" % len(t2))
        # ---- the --confirm path end to end, the decoy standing in for the locked set
        simC = os.path.join(tmp, "C")
        step("模拟 C：锁定测试集的行整行删掉；诱饵行标成 public_test，冒充锁定集")
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
        step("用 --confirm 的流程给“冒充的锁定集”打分（登记表、日志都在临时目录）")
        try:
            ph.REG, ph.LOG, ph._cache = reg_path, log_path, None
            globals()["REAL_CLEAN"] = simC
            with clean_dir(simC), contextlib.redirect_stdout(buf):
                out_c = run_confirm("自检演练：诱饵冒充锁定集（登记在 %s）" % reg_path, out_dir=tmp)
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
        chk("8 --confirm 流程端到端：分数和诱饵演练完全一样", out_c["code"] == 0 and same_c,
            "%d 个；C 删掉 %d 行、改标 %d 行" % (len(c2), nC["master_panel.csv dropped"],
                                         nC["master_panel.csv"]))
        chk("8 --confirm 先写“开始”行、结束再写结果行，结果行里有总述",
            len(lrows) == 2 and "开始打分" in lrows[0]["action"] and lrows[0]["result"] ==
            "（运行中）" and "完成" in lrows[1]["action"]
            and lrows[1]["result"].startswith("n=") and "registry_decoy.csv" in
            lrows[0]["decision_registered_in"], "日志 %d 行" % len(lrows))
        out_txt = buf.getvalue()
        chk("8 --confirm 写完日志才打印表", out_txt.find("已写入访问日志") > 0
            and out_txt.find("| 全部 |") > out_txt.find("已写入访问日志"))
        say("      （演练的总述：%s）" % lrows[-1]["result"][:120] if lrows else "")
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
                r_none = run_confirm("没有登记文件")
                with clean_dir(REAL_CLEAN):     # the decoy rows still say "external" here
                    r_pre = run_confirm("自检：重建前应当拒绝（登记在 %s）" % reg_path)
        finally:
            g["Panel"], g["evaluate"] = saved_fn
            ph.REG, ph.LOG, ph._cache = saved[0], saved[1], saved[2]
            globals()["REAL_CLEAN"] = saved[3]
        with open(log_path, encoding="utf-8-sig", newline="") as f:
            n_after = len(list(csv.DictReader(f)))
        chk("8 没写登记文件、或数据还没重建（诱饵在真实数据上仍是 external）时拒绝，不写日志",
            r_none["code"] == 2 and r_pre is not None and r_pre["code"] == 2 and n_after == 2)
    chk("9 运行期间 data/clean 没被改动", fingerprint() == fp0)
    chk("9 真正的访问日志没被碰过",
        (open(ph.LOG, "rb").read() if os.path.exists(ph.LOG) else None) == log0)
    say("\n" + ("全部通过" if ok[0] else "有失败，见上"))
    return ok[0]


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="给锁定的公开测试集打分（和生产里给新招标打分完全一样）。"
                    "真正的测试集只能用 --confirm 打开，并写入访问日志；"
                    "开发和测试请用 --decoy 或 --selftest。")
    ap.add_argument("--confirm", metavar="说明",
                    help="打开锁定测试集：写清楚要确认的决定，以及它事先登记在哪个文件里")
    ap.add_argument("--decoy", type=int, metavar="种子",
                    help="演练：随机 60 组没留出的公开市政招标当作留出；不碰测试集，不写日志")
    ap.add_argument("--selftest", action="store_true",
                    help="在诱饵集上插桩自检（默认种子 %d，可用 --decoy 改）" % SELFTEST_SEED)
    ap.add_argument("--allow-pre-rebuild", action="store_true",
                    help="（只配合 --confirm）数据还没重建也运行；会写进日志")
    ap.add_argument("--per-letting", action="store_true",
                    help="（只配合 --confirm）也打印逐个留出招标；会写进日志")
    ap.add_argument("--json", metavar="路径", help="（只配合 --decoy）结果写到这里")
    a = ap.parse_args(argv)
    modes = sum(bool(x) for x in (a.confirm is not None, a.selftest,
                                  a.decoy is not None and not a.selftest))
    if a.confirm is not None and (a.decoy is not None or a.selftest):
        say("拒绝运行：--confirm 不能和 --decoy / --selftest 一起用。")
        return 2
    if modes == 0:
        say("拒绝运行：要给锁定测试集打分，必须用 --confirm \"<确认的决定，以及登记在哪个文件>\"；"
            "开发和测试请用 --decoy 种子 或 --selftest。")
        return 2
    if a.selftest:
        return 0 if _selftest(a.decoy if a.decoy is not None else SELFTEST_SEED) else 1
    if a.decoy is not None:
        return run_decoy(a.decoy, a.json)
    return run_confirm(a.confirm, a.allow_pre_rebuild, a.per_letting)["code"]


if __name__ == "__main__":
    sys.exit(main())
