"""The comparables method (CMP) of the V2 blend: price a schedule from the
most similar EARLIER lettings, then blend with the line-priced bid part.

    P50 bid part = sqrt(CMP x line-priced bid part)       (owner-set money last)

Adopted 2026-11-20 as part of V2+F1+F2: selected by p50fix round 5 on data
snapshot 3 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND5.md, R5-A7) and
confirmed once on the locked public test set (Registration 3,
data/test_holdout/REGISTRATIONS.md: P80 under the low bid 19.6% <= 20%,
median overshoot 35.9% -> 31.0%).  The rules are round 3's V2
(handoff/scratchpad/p50fix/PREREGISTRATION_ROUND3.md) = the comparables
pre-registration's main rule, ported line for line from
handoff/scratchpad/p50fix/round3/cmp_port.py (md5 ded388050d...):

  * candidates: same-market lettings that pass the forward population
    filters, available STRICTLY before the priced letting's date D, and share
    at least O >= 0.10 of the schedule's weight;
  * similarity S = O x size x owner x work type x age, k = 5 best;
  * line identity = item_tokens WITHOUT the removal flag (the identity V2
    was registered with; tokens(..., removal=False));
  * line transfer: candidate winner unit price x quantity factor (beta) x
    escalation x competition adjustment f(n_T)/f(n_C); LS lines scale by the
    non-LS amount (gamma_c);
  * beta / gamma_c fitted on the pool's lettings dated before min(D,
    2023-01-01); escalation and f(n) from the tables the line prices use;
  * unmatched lines keep the line price x f(n_T); no comparables at all ->
    CMP = the line-priced bid part (the blend then returns it).

Everything here is FITTED on the pool handed in (Pool(rows, ...)): the
caller decides which rows may train it (estimate_low_bid passes the
external-only price pool; holdout_eval its training rows).  train_test_guard
recomputes the pool, beta/gamma and the chosen comparables with test rows
switched on and off.
"""
import collections
import datetime as _dt
import math
import os
import re
import statistics

import numpy as np

import item_tokens as IT
import qty_shape

RM = re.compile(r"\b(remov\w*|rem|demo|demol\w*|demolition)\b")
RP = re.compile(r"\b(replac\w*|reconstruct\w*|install\w*|reset\w*|relocat\w*|"
                r"new|rebuild\w*|reinstall\w*)\b")
LS_UNITS = {"LS"}
DEV_END = "2023-01-01"
K_MAIN, O_MIN = 5, 0.10


# Round 10 E, part of the CORE preset (estimate_low_bid.pricing_rule(
# round10_core=True); handoff/scratchpad/p50fix/PREREGISTRATION_ROUND10.md):
# the MUNICIPAL pool's line identity and the municipal schedules' carry F2's
# removal flag, so similar() refuses a removal-only line against any other
# (remove-and-replace and new work never take a pure-removal price, nor the
# reverse).  TxDOT keeps the flag-free identity.  OFF until the locked-set
# confirmation (Registration 5) passes; off = production.
MUNI_REMOVAL_IDENTITY = False


def tokens(desc, market=None):
    """The registered line identity: item_tokens without the removal flag
    (round 10 E: with it, for the municipal market, when
    MUNI_REMOVAL_IDENTITY is on)."""
    return IT.tokens(desc, removal=bool(MUNI_REMOVAL_IDENTITY
                                        and market == "municipal"))


def removal_only(desc):
    d = (desc or "").lower()
    return bool(RM.search(d)) and not RP.search(d)


def owner_id(pk, market, county):
    if market == "txdot":
        return "TX:" + (county or "").strip().upper()
    parts = pk.split("/")
    if len(parts) > 2 and parts[1] == "legistar":
        return parts[1] + "/" + parts[2]
    return parts[1] if len(parts) > 1 else pk


def txdot_attrs(clean):
    import pandas as pd
    a = pd.read_csv(os.path.join(clean, "txdot_project_totals.csv"),
                    encoding="utf-8-sig", dtype=str, keep_default_na=False)
    b = pd.read_csv(os.path.join(clean, "txdot_archive_project_totals.csv"),
                    encoding="utf-8-sig", dtype=str, keep_default_na=False)
    out = {}
    for _, r in a.drop_duplicates("project_id").iterrows():
        out[r.project_id] = (r.county.strip().upper(),
                             r.project_type.strip().upper())
    for _, r in b.drop_duplicates("src_file").iterrows():
        out.setdefault(r.src_file, (r.county.strip().upper(),
                                    r.project_type.strip().upper()))
    return out


def muni_worktype(clean):
    """The winner rows' work_type with the most dollars (external rows)."""
    import pandas as pd
    m = pd.read_csv(os.path.join(clean, "master_panel.csv"),
                    encoding="utf-8-sig", low_memory=False, dtype=str,
                    keep_default_na=False,
                    usecols=["source_set", "project_key", "work_type",
                             "is_low_bidder", "quantity", "unit_price"])
    m = m[(m.source_set == "external") & (m.is_low_bidder == "True")]
    q = pd.to_numeric(m.quantity, errors="coerce")
    u = pd.to_numeric(m.unit_price, errors="coerce")
    m = m.assign(a=q * u)
    m = m[m.a > 0]
    out = {}
    g = m.groupby(["project_key", "work_type"]).a.sum().reset_index()
    for pk, gg in g.groupby("project_key"):
        gg = gg.copy()
        gg["wt"] = gg.work_type.replace("", "(blank)")
        gg = gg.groupby("wt").a.sum()
        out[pk] = gg.idxmax()
    return out


def schedule_worktypes(clean, keys, f=None):
    """The same rule for the schedules being PRICED (a schedule's own work
    type is known when it is estimated), reading the rows of any source_set
    given in `sets` -- a held-out letting's rows say public_test after the
    rebuild.  Plain accumulation, as the confirmation tool read it."""
    import csv
    keys = set(keys)
    acc = collections.defaultdict(lambda: collections.defaultdict(float))
    f = f or (lambda v: _num(v))
    csv.field_size_limit(10 ** 8)
    with open(os.path.join(clean, "master_panel.csv"), encoding="utf-8-sig",
              newline="") as fh:
        rd = csv.reader(fh)
        h = next(rd)
        ix = {c: h.index(c) for c in ("source_set", "project_key", "work_type",
                                      "is_low_bidder", "quantity", "unit_price")}
        for row in rd:
            pk = row[ix["project_key"]]
            if pk not in keys or row[ix["source_set"]] not in ("external",
                                                                "public_test"):
                continue
            if row[ix["is_low_bidder"]] != "True":
                continue
            q, u = f(row[ix["quantity"]]), f(row[ix["unit_price"]])
            if q is None or u is None or q * u <= 0:
                continue
            acc[pk][row[ix["work_type"]] or "(blank)"] += q * u
    out = {}
    for pk, d in acc.items():
        w = max(sorted(d), key=lambda k: d[k])
        out[pk] = "" if w == "(blank)" else w
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def build_lettings(rows, market):
    lets = collections.defaultdict(
        lambda: {"bt": collections.defaultdict(float), "win": set(),
                 "sched": {}, "mk": "", "rows": collections.defaultdict(list),
                 "prices": collections.defaultdict(list), "wamt": {},
                 "yr": collections.Counter()})
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in rows:
        if q is None or q <= 0 or mk != market:
            continue
        d = lets[pk]
        d["mk"] = mk
        d["bt"][b] += q * u
        k = (fam, un, sz, qty_shape.band_of(q), dn)
        d["prices"][k].append(u)
        if w:
            d["win"].add(b)
            d["sched"][k] = d["sched"].get(k, 0.0) + q
            d["rows"][k].append(q)
            d["wamt"][k] = d["wamt"].get(k, 0.0) + q * u
            d["yr"][yr] += 1
    return lets


def eligible(d, min_items):
    if len(d["win"]) != 1 or len(d["bt"]) < 2 or not d["sched"]:
        return False
    wt = d["bt"].get(next(iter(d["win"])), 0.0)
    if wt <= 0 or statistics.median(d["bt"].values()) <= 0:
        return False
    return len(d["sched"]) >= min_items


def ols_within(groups):
    sxy = sxx = 0.0
    n = 0
    for g in groups:
        if len(g) < 2:
            continue
        mx = statistics.fmean(x for x, _ in g)
        my = statistics.fmean(y for _, y in g)
        for x, y in g:
            sxy += (x - mx) * (y - my)
            sxx += (x - mx) ** 2
            n += 1
    return (sxy / sxx if sxx > 0 else None), n


def prepare(rows, avail, market, clean, never, min_items):
    """Candidates, identities and postings of one market, from `rows` (a
    price pool) and `avail` (availability dates)."""
    lets = build_lettings(rows, market)
    el = sorted(pk for pk, d in lets.items()
                if eligible(d, min_items) and avail.get(pk, never) != never)
    txa = txdot_attrs(clean) if market == "txdot" else {}
    wtm = muni_worktype(clean) if market == "municipal" else {}
    win_rows = collections.defaultdict(list)
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in rows:
        if mk != market or not w or q is None or q <= 0 or u is None or u <= 0:
            continue
        win_rows[pk].append((fam, un, sz, q, u, dn, yr))
    ident = {}
    L = []
    for pk in el:
        d = lets[pk]
        lines = []
        nonls = 0.0
        for fam, un, sz, q, u, dn, yr in win_rows[pk]:
            t = tokens(dn, market)
            iid = ident.setdefault((t, un), len(ident)) if (t and un) else -1
            isls = (un or "").upper() in LS_UNITS
            if not isls:
                nonls += q * u
            lines.append({"iid": iid, "unit": un, "q": q, "u": u,
                          "rm": removal_only(dn), "ls": isls})
        low = d["bt"][next(iter(d["win"]))]
        county, ptype = txa.get(pk, ("", "")) if market == "txdot" else ("", "")
        wt = wtm.get(pk, "(blank)") if market == "municipal" else ptype
        L.append({"pk": pk, "date": avail[pk],
                  "year": d["yr"].most_common(1)[0][0] if d["yr"] else "",
                  "n": len(d["bt"]), "low": low, "nonls": nonls,
                  "owner": owner_id(pk, market, county),
                  "wtype": "" if wt in ("", "(blank)") else wt,
                  "lines": lines})
    post = collections.defaultdict(set)
    for li, Lr in enumerate(L):
        m = collections.defaultdict(list)
        for j, ln in enumerate(Lr["lines"]):
            if ln["iid"] >= 0:
                post[ln["iid"]].add(li)
                m[ln["iid"]].append(j)
        Lr["by_iid"] = dict(m)
    postings = {iid: np.array(sorted(s), dtype=np.int32)
                for iid, s in post.items()}
    id_list = [None] * len(ident)
    for k, v in ident.items():
        id_list[v] = k
    inv = collections.defaultdict(lambda: collections.defaultdict(list))
    for iid, (t, un) in enumerate(id_list):
        for w in t:
            inv[un][w].append(iid)
    return {"market": market, "L": L, "id_list": id_list,
            "inv": {u: dict(v) for u, v in inv.items()},
            "postings": postings}


class BetaGamma:
    """beta (quantity) / gamma_c (LS scale) on candidates dated before a cutoff."""

    def __init__(self, L):
        self.bq, self.gl = [], []
        nonls_of = {Lr["pk"]: Lr["nonls"] for Lr in L}
        for Lr in L:
            for ln in Lr["lines"]:
                if ln["iid"] < 0:
                    continue
                if ln["ls"]:
                    if Lr["nonls"] > 0:
                        self.gl.append((Lr["date"], ln["iid"], Lr["pk"],
                                        math.log(ln["q"] * ln["u"])))
                else:
                    self.bq.append((Lr["date"], ln["iid"], Lr["pk"],
                                    math.log(ln["q"]), math.log(ln["u"])))
        self.nonls_of = nonls_of
        self.memo = {}

    def at(self, cutoff):
        if cutoff in self.memo:
            return self.memo[cutoff]
        gq = collections.defaultdict(lambda: collections.defaultdict(list))
        for d, iid, pk, lq, lu in self.bq:
            if d < cutoff:
                gq[iid][pk].append((lq, lu))
        gl = collections.defaultdict(lambda: collections.defaultdict(list))
        for d, iid, pk, la in self.gl:
            if d < cutoff:
                gl[iid][pk].append(la)
        groups_b = []
        for iid, byl in gq.items():
            if len(byl) < 2:
                continue
            groups_b.append([(statistics.median(x for x, _ in v),
                              statistics.median(y for _, y in v))
                             for v in byl.values()])
        beta_raw, _ = ols_within(groups_b)
        groups_g = []
        for iid, byl in gl.items():
            if len(byl) < 2:
                continue
            groups_g.append([(math.log(self.nonls_of[pk]), statistics.median(v))
                             for pk, v in byl.items()])
        gamma_raw, _ = ols_within(groups_g)
        beta = min(0.0, max(-0.5, beta_raw)) if beta_raw is not None else 0.0
        gamma = min(1.2, max(0.0, gamma_raw)) if gamma_raw is not None else 0.0
        self.memo[cutoff] = (beta, gamma, beta_raw, gamma_raw,
                             len(groups_b), len(groups_g))
        return self.memo[cutoff]

    def keys_before(self, cutoff):
        """The lettings whose lines entered beta / gamma at `cutoff`."""
        return ({pk for d, _, pk, _, _ in self.bq if d < cutoff}
                | {pk for d, _, pk, _ in self.gl if d < cutoff})


def wmedian(pairs):
    pairs = sorted(pairs)
    tw = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tw / 2:
            return v
    return pairs[-1][0]


class Ctx:
    def __init__(self, P):
        self.__dict__.update(P)
        self.dates = np.array([Lr["date"] for Lr in self.L])
        self.pk_index = {Lr["pk"]: i for i, Lr in enumerate(self.L)}
        self.sim_cache = {}

    def sims(self, t, un):
        key = (t, un)
        s = self.sim_cache.get(key)
        if s is None:
            cand = set()
            for w in t:
                cand.update(self.inv.get(un, {}).get(w, ()))
            s = []
            for iid in cand:
                j = IT.similar(t, self.id_list[iid][0])
                if j > 0:
                    s.append((iid, j))
            self.sim_cache[key] = s
        return s


def price_target(ctx, rec, keys_full, tab, beta, gamma, E, owner=None,
                 wtype="", fC=None):
    """CMP for one schedule (main rule, k = 5).  rec: key, date D, year, n
    (bidders), f (the discount the line prices use) and lines [{ki, q, p,
    fx}]; keys_full[ki] = (family, unit, size_bucket, band, description).
    E = estimate_low_bid (escalation, discount).  A schedule that is not in
    the pool (a new one, or a held-out letting) gives its own owner and work
    type; one in the pool uses the pool's."""
    mk = ctx.market
    D, yrT, nT = rec["date"], rec["year"], rec["n_bidders"]
    fT = rec["f"]
    rows = []
    w_key = collections.defaultdict(float)
    priced_key = set()
    for ln in rec["lines"]:
        if ln["fx"]:
            continue
        ki, q, p = ln["ki"], ln["q"], ln["p"]
        rows.append((ki, q, p))
        if p:
            w_key[ki] += p * q
            priced_key.add(ki)
    kis = sorted({ki for ki, _, _ in rows})
    wmed = (statistics.median([w_key[ki] for ki in priced_key])
            if priced_key else 1.0)
    W = {ki: (w_key[ki] if ki in priced_key else wmed) for ki in kis}
    allfb = sum((p or 0.0) * q * fT for _, q, p in rows)
    nL = len(ctx.L)
    num = np.zeros(nL)
    Wsum = sum(W.values())
    kinfo = {}
    for ki in kis:
        fam, un, sz, bd, dn = keys_full[ki]
        t = tokens(dn, mk)
        sm = ctx.sims(t, un) if (t and un) else []
        kinfo[ki] = {"t": t, "un": un, "rm": removal_only(dn),
                     "ls": (un or "").upper() in LS_UNITS, "sims": dict(sm)}
        if not sm:
            continue
        best = np.zeros(nL)
        for iid, j in sm:
            idx = ctx.postings.get(iid)
            if idx is not None:
                np.maximum.at(best, idx, j)
        num += W[ki] * best
    O = num / Wsum if Wsum > 0 else num
    S_T = sum((p or 0.0) * q * fT for ki, q, p in rows
              if p and not kinfo[ki]["ls"])
    me = ctx.pk_index.get(rec["key"])
    Lme = ctx.L[me] if me is not None else None
    own_T = Lme["owner"] if Lme else owner
    wt_T = Lme["wtype"] if Lme else (wtype or "")
    cand = np.where((ctx.dates < D) & (O >= O_MIN))[0] if nL else []
    dD = _dt.date.fromisoformat(D)
    scored = []
    for ci in cand:
        Lc = ctx.L[ci]
        if Lc["pk"] == rec["key"]:
            continue
        gs = (math.exp(-0.5 * abs(math.log(S_T / Lc["nonls"])))
              if (S_T > 0 and Lc["nonls"] > 0) else math.exp(-0.5 * math.log(10)))
        go = 1.5 if (own_T and Lc["owner"] == own_T) else 1.0
        gt = 1.5 if (wt_T and Lc["wtype"] == wt_T) else 1.0
        age = (dD - _dt.date.fromisoformat(Lc["date"])).days / 365.25
        ga = 2.0 ** (-age / 3.0)
        S = O[ci] * gs * go * gt * ga
        scored.append((S, Lc["date"], Lc["pk"], int(ci), float(O[ci])))
    full_rank = sorted(scored, key=lambda x: (-x[0], tuple(-ord(ch) for ch in x[1]),
                                              x[2]))
    esc_cache, comp_cache = {}, {}

    def e_of(Lc):
        y = Lc["year"]
        if y not in esc_cache:
            r = E.escalation(tab, y, yrT, mk) if (y and yrT) else None
            esc_cache[y] = (r or {}).get("factor") or 1.0
        return esc_cache[y]

    dT = E.discount(tab, nT, mk)

    def c_of(Lc):
        if fC is not None:              # T1 (TxDOT): f(C) from D1, per letting
            if Lc["pk"] not in comp_cache:
                v = fC(Lc["n"], Lc["pk"])
                comp_cache[Lc["pk"]] = (fT / v) if v else 1.0
            return comp_cache[Lc["pk"]]
        n = Lc["n"]
        if n not in comp_cache:
            dC = E.discount(tab, n, mk)
            comp_cache[n] = ((dT["f"] / dC["f"]) if (dT and dC and dC["f"])
                             else 1.0)
        return comp_cache[n]

    def run(comps):
        amt_nonls = 0.0
        out = []
        ls_pending = []
        for ki, q, p in rows:
            info = kinfo[ki]
            cands = []
            for sc in comps:
                S, _, _, ci, Oc = sc
                Lc = ctx.L[ci]
                best = None
                for iid, j in info["sims"].items():
                    for li in Lc["by_iid"].get(iid, ()):
                        ln = Lc["lines"][li]
                        key = (-j, 0 if ln["rm"] == info["rm"] else 1,
                               abs(math.log(ln["q"]) - math.log(q))
                               if (ln["q"] > 0 and q > 0) else 99.0, li)
                        if best is None or key < best[0]:
                            best = (key, ln, j)
                if best is not None:
                    cands.append((S, Lc, best[1], best[2]))
            if not cands:
                a = (p or 0.0) * q * fT
                out.append((ki, q, a, False))
                if not info["ls"]:
                    amt_nonls += a
                continue
            if info["ls"]:
                ls_pending.append((ki, q, cands))
                continue
            pv = []
            for S, Lc, ln, j in cands:
                a = (min(2.0, max(0.5, (q / ln["q"]) ** beta))
                     if (q > 0 and ln["q"] > 0) else 1.0)
                pv.append((ln["u"] * a * e_of(Lc) * c_of(Lc), S * j))
            a = wmedian(pv) * q
            out.append((ki, q, a, True))
            amt_nonls += a
        for ki, q, cands in ls_pending:
            pv = []
            for S, Lc, ln, j in cands:
                R = (amt_nonls / Lc["nonls"]
                     if (Lc["nonls"] > 0 and amt_nonls > 0) else 1.0)
                a = min(3.0, max(1.0 / 3.0, R ** gamma))
                pv.append((ln["u"] * a * e_of(Lc) * c_of(Lc), S * j))
            out.append((ki, q, wmedian(pv) * q, True))
        bid = sum(a for _, _, a, _ in out)
        matched = sum(a for _, _, a, m in out if m)
        return bid, matched

    comps = full_rank[:K_MAIN]
    if comps:
        bid, mat = run(comps)
    else:
        bid, mat = allfb, 0.0
    return {"allfb": allfb, "cmp": bid, "matched": mat,
            "n_comps": len(comps), "n_candidates": len(scored),
            "comps": [x[2] for x in comps],
            "comp_dates": [x[1] for x in comps]}


class Pool:
    """The comparables pool of ONE market at one data state: every letting of
    `rows` that passes the forward population filters and has a date, its
    winner lines and identities, and beta/gamma.  A letting is a candidate
    for a schedule dated D only if its own date is before D."""

    def __init__(self, rows, avail, market, clean, E):
        P = prepare(rows, avail, market, clean, E.NEVER, E.MIN_SCHEDULE_ITEMS)
        self.market = market
        self.ctx = Ctx(P)
        self.bg = BetaGamma(P["L"])
        self.E = E

    def keys(self):
        return [Lr["pk"] for Lr in self.ctx.L]

    def price(self, rec, keys_full, tab, owner=None, wtype="", fC=None):
        """fC: T1's f(C) for the competition adjustment (TxDOT), or None
        for production f(n_C)."""
        cut = min(rec["date"], DEV_END)
        beta, gamma = self.bg.at(cut)[0], self.bg.at(cut)[1]
        r = price_target(self.ctx, rec, keys_full, tab, beta, gamma, self.E,
                         owner, wtype, fC)
        r["beta"], r["gamma_c"], r["bg_cutoff"] = beta, gamma, cut
        return r
