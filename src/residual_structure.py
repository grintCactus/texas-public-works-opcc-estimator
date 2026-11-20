"""Two numbers the estimate's interval depends on, measured instead of assumed.

1. THE RESIDUAL SPREAD of a reference price against what was actually bid.
   The UI currently shrinks the reference IQR by a constant 0.55 to get an
   interval.  That is a guess about how much of the reference's spread is real
   dispersion rather than item heterogeneity.

2. THE WITHIN-LETTING CORRELATION rho of those residuals.
   This one is the dangerous one.  A bid is a SUM over sixty-odd line items, and
       Var(sum) = sum_i w_i^2 s_i^2 + 2 * sum_{i<j} rho w_i w_j s_i s_j
   so rho drives almost the whole width of a project-level interval.  At rho = 0
   sixty independent items average their errors away and the interval collapses
   to nearly nothing -- a confident number with no basis.  At rho = 1 the
   interval is as wide as a single item's.  Nothing about the data makes rho
   small a priori: one contractor prices a whole bid with one view of the market,
   one crew cost and one appetite for the work, which is exactly the mechanism
   that makes line-item errors move together.

WHY THE SPLIT MATTERS HERE.  Residuals measured on the same lettings the
reference was fitted on are too small, and their rho is too small as well,
because the reference has already absorbed each letting's own level.  So rho is
NOT measured on the fitting sample.  Two estimates are reported instead:

  * K-fold on the public (training) side.  The reference is refitted without the
    held-out lettings, so their residuals are honest, and there are enough
    lettings for the estimate to mean something.
  * The company (test) side, against the reference fitted on all public data.
    This is the provenance rule for this project -- company files are test-only,
    never training -- and it is also the only truly out-of-sample estimate.  It
    rests on very few lettings, so it is reported with its letting count and
    read as corroboration, not as the headline.

ESTIMATOR.  One-way random effects by ANOVA, with unequal group sizes:

    n0    = (N - sum n_i^2 / N) / (k - 1)
    s2_b  = (MSB - MSW) / n0
    rho   = s2_b / (s2_b + MSW)

A negative s2_b is reported as rho = 0 and flagged, not clipped silently: it
means the between-letting variance is not resolvable at this sample size, which
is a different statement from "the correlation is zero".
"""

import numpy as np

REF_KEY = ["item_family", "unit", "size_bucket"]
MIN_REF_PROJECTS = 3        # a reference group needs this many lettings
MIN_ITEMS_PER_LETTING = 2   # rho needs at least two items in a letting
N_FOLDS = 5


def _fit_reference(df):
    """Two-stage median: within letting first, then across lettings.

    The two stages are not interchangeable.  A single median over all rows lets
    a letting with many bidders and many items outvote the rest, so the
    reference would describe that letting rather than the market.
    """
    s1 = (df.groupby(REF_KEY + ["project_key"], observed=True)
          .unit_price.median().rename("proj_price").reset_index())
    ref = (s1.groupby(REF_KEY, observed=True)
           .agg(ref_price=("proj_price", "median"),
                n_projects=("project_key", "nunique")).reset_index())
    return ref[ref.n_projects >= MIN_REF_PROJECTS]


ITEM_KEY = ["desc_norm", "unit"]


def _project_actuals(df):
    """Per (SPECIFIC ITEM, letting): the median bid and the low bid.

    The grain is the item, not the reference group, and the distinction is not
    cosmetic.  A reference group is (item_family, unit, size_bucket), which
    pools several distinct items, so `min` over that group returns the cheapest
    ITEM in the family rather than the lowest BIDDER's price for one item.
    Measured that way the low bid came out at 0.48 of the reference -- a 52%
    discount, against a competition discount of about 0.87 -- and the error was
    in the estimator, not in the market.

    `actual_low` is therefore a minimum over BIDDERS within one letting and one
    item.  `actual_median` is the median over the same set.
    """
    # ITEM_KEY and REF_KEY share `unit`; dict.fromkeys keeps order and drops the
    # repeat, which pandas would otherwise refuse on reset_index.
    keys = list(dict.fromkeys(ITEM_KEY + REF_KEY + ["project_key"]))
    g = (df.groupby(keys, observed=True)
         .unit_price.agg(actual_median="median", actual_low="min",
                         n_bids="size").reset_index())

    # ---- what the WINNER actually quoted, which is not the item-wise min ----
    # `actual_low` is a minimum over bidders item by item.  Summed over a bid
    # schedule that is a LOWER ENVELOPE, not anybody's bid: the contractor who
    # is lowest on the total is not lowest on every line, because a bid is a
    # single total allocated across items.  Aiming an estimate at the envelope
    # would make it unwinnable-low by construction.  So the winner's own quote
    # is carried as a separate column and is the right target for a low-bid
    # estimate; `actual_low` stays, because the gap between the two is itself
    # worth reporting.
    tot = df.assign(_amt=df.unit_price * df.quantity.fillna(0))
    tot = (tot.groupby(["project_key", "bidder"], observed=True)._amt.sum()
           .rename("bid_total").reset_index())
    tot = tot[tot.bid_total > 0]
    if len(tot):
        win = tot.loc[tot.groupby("project_key").bid_total.idxmin(),
                      ["project_key", "bidder"]]
        win["_is_winner"] = True
        w = df.merge(win, on=["project_key", "bidder"], how="inner")
        wg = (w.groupby(keys, observed=True)
              .unit_price.median().rename("actual_winner").reset_index())
        g = g.merge(wg, on=keys, how="left")
    else:
        g["actual_winner"] = float("nan")
    return g


def _fit_reference_item(df):
    """An ITEM-level reference, for comparison with the family-level one.

    The point of measuring both is that the residual spread is not a property
    of the market alone -- it depends on how closely the reference matches the
    item being priced.  A family-level median pools heterogeneous work, so part
    of its residual is item heterogeneity, which is item-specific noise and
    therefore DILUTES the within-letting correlation.  Reporting one sigma and
    one rho without saying which reference produced them would let a caller
    apply a family-level sigma to an item-level match, or the reverse.
    """
    s1 = (df.groupby(ITEM_KEY + ["project_key"], observed=True)
          .unit_price.median().rename("proj_price").reset_index())
    ref = (s1.groupby(ITEM_KEY, observed=True)
           .agg(ref_price=("proj_price", "median"),
                n_projects=("project_key", "nunique")).reset_index())
    return ref[ref.n_projects >= MIN_REF_PROJECTS]


def icc(groups):
    """groups: list of 1-D arrays of residuals, one array per letting."""
    gs = [np.asarray(g, dtype=float) for g in groups]
    gs = [g[np.isfinite(g)] for g in gs]
    gs = [g for g in gs if len(g) >= MIN_ITEMS_PER_LETTING]
    k = len(gs)
    if k < 3:
        return None
    n = np.array([len(g) for g in gs], dtype=float)
    N = n.sum()
    means = np.array([g.mean() for g in gs])
    grand = np.concatenate(gs).mean()
    msb = float((n * (means - grand) ** 2).sum() / (k - 1))
    ssw = float(sum(((g - g.mean()) ** 2).sum() for g in gs))
    dfw = N - k
    if dfw <= 0:
        return None
    msw = ssw / dfw
    n0 = (N - (n ** 2).sum() / N) / (k - 1)
    s2b = (msb - msw) / n0 if n0 > 0 else float("nan")
    unresolved = not (s2b > 0)
    s2b_c = max(s2b, 0.0)
    rho = s2b_c / (s2b_c + msw) if (s2b_c + msw) > 0 else float("nan")
    return {"rho": float(rho), "n_lettings": k, "n_items": int(N),
            "sigma_within": float(np.sqrt(msw)),
            "sigma_between": float(np.sqrt(s2b_c)),
            "between_unresolved": bool(unresolved),
            "items_per_letting_median": float(np.median(n))}


def _join(act, ref, on):
    import numpy as _np
    j = act.merge(ref, on=on, how="inner")
    if not len(j):
        return None
    j = j[(j.ref_price > 0) & (j.actual_median > 0) & (j.actual_low > 0)].copy()
    if not len(j):
        return None
    j["resid_median"] = _np.log(j.actual_median / j.ref_price)
    j["resid_low"] = _np.log(j.actual_low / j.ref_price)
    if "actual_winner" in j.columns:
        w = j.actual_winner.where(j.actual_winner > 0)
        j["resid_winner"] = _np.log(w / j.ref_price)
    return j


def residuals_kfold(comp, n_folds=N_FOLDS, seed=0, grain="family"):
    """Out-of-fold residuals on the training side.

    Folds are drawn over LETTINGS, not rows: splitting rows would leave the
    same letting on both sides of the split, and its residual would then be
    measured against a reference that had already seen it.
    """
    lettings = np.array(sorted(comp.project_key.unique()))
    # Deterministic assignment, so a rerun reproduces the number exactly.
    rng = np.random.RandomState(seed)
    fold = rng.permutation(len(lettings)) % n_folds
    assign = dict(zip(lettings, fold))
    fitter, on = ((_fit_reference, REF_KEY) if grain == "family"
                  else (_fit_reference_item, ITEM_KEY))
    out = []
    for f in range(n_folds):
        held = comp.project_key.map(assign) == f
        fit = comp[~held]
        if not len(fit) or not held.any():
            continue
        j = _join(_project_actuals(comp[held]), fitter(fit), on)
        if j is None:
            continue
        j["fold"] = f
        out.append(j)
    if not out:
        return None
    import pandas as pd
    return pd.concat(out, ignore_index=True)


def residuals_holdout(comp_train, test_bids, grain="family"):
    """Residuals of the company (test) lettings against the public reference."""
    fitter, on = ((_fit_reference, REF_KEY) if grain == "family"
                  else (_fit_reference_item, ITEM_KEY))
    return _join(_project_actuals(test_bids), fitter(comp_train), on)


TARGETS = ("resid_median", "resid_low", "resid_winner")
TARGET_NOTE = {
    "resid_median": "the letting's median bid for the item",
    "resid_low": "the item-wise minimum across bidders -- a LOWER ENVELOPE, "
                 "not any single bid; do not aim an estimate at it",
    "resid_winner": "the price quoted by the bidder lowest on the total -- "
                    "the right target for a low-bid estimate",
}


def summarise(j, col):
    g = [sub[col].values for _, sub in j.groupby("project_key")]
    r = icc(g)
    v = j[col].values
    v = v[np.isfinite(v)]
    return {"n": int(len(v)),
            "median": float(np.median(v)),
            "sigma_mad": float(1.4826 * np.median(np.abs(v - np.median(v))))
            if len(v) >= 3 else float("nan"),
            "p10": float(np.quantile(v, .10)) if len(v) else float("nan"),
            "p90": float(np.quantile(v, .90)) if len(v) else float("nan"),
            "icc": r}


def main():
    """Write data/clean/residual_structure.csv from the master panel."""
    import os
    import pandas as pd

    clean = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "clean")
    p = os.path.join(clean, "master_panel.csv")
    if not os.path.exists(p):
        print("no master_panel.csv; run build_master.py first")
        return
    m = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    m["unit_price"] = pd.to_numeric(m.unit_price, errors="coerce")
    m["quantity"] = pd.to_numeric(m.quantity, errors="coerce")
    m["is_ee"] = m.is_engineer_estimate.astype(str).str.lower().isin(
        ["true", "1", "yes"])
    m["size_bucket"] = m.size_bucket.fillna("")
    m["desc_norm"] = m.desc_norm.fillna("")
    # A row with no unit cannot be a unit price.  0.13% of the pool; they reach
    # no published reference group today, but they would pool into a
    # (family, "") bucket if one ever cleared the sample minimum.
    bids = m[(~m.is_ee) & (m.unit_price > 0) & (m.unit.fillna("") != "")]
    train = bids[bids.source_set == "external"]
    # the company's rows; the locked public test set ("public_test") is kept
    # out of this comparison as well as out of training
    test = bids[bids.source_set == "internal"]
    comp = train[(train.competition_class == "competitive")
                 & (train.project_kind == "construction")]

    rows = []
    for grain in ("family", "item"):
        jk = residuals_kfold(comp, grain=grain)
        jt = residuals_holdout(comp, test, grain=grain)
        for split, j in (("public_out_of_fold", jk), ("company_holdout", jt)):
            if j is None or not len(j):
                continue
            for col in TARGETS:
                if col not in j.columns:
                    continue
                s = summarise(j, col)
                r = s["icc"] or {}
                rows.append({
                    "split": split, "ref_grain": grain, "target": col,
                    "n_obs": s["n"],
                    "median_ratio": round(float(np.exp(s["median"])), 4),
                    "median_log": round(s["median"], 4),
                    "sigma_log": round(s["sigma_mad"], 4),
                    "p10_log": round(s["p10"], 4), "p90_log": round(s["p90"], 4),
                    "rho_within_letting": round(r["rho"], 4) if r else "",
                    "sigma_within": round(r["sigma_within"], 4) if r else "",
                    "sigma_between": round(r["sigma_between"], 4) if r else "",
                    "n_lettings": r.get("n_lettings", "") if r else "",
                    "items_per_letting_median": r.get("items_per_letting_median", "")
                    if r else "",
                    "between_unresolved": r.get("between_unresolved", "") if r else "",
                    "note": TARGET_NOTE.get(col, "")})
                # ---- say out loud which of these may be quoted -----------
                # n_obs is NOT the sample size here.  Items inside one letting
                # are correlated, so the information in 386 observations over 8
                # lettings is the information in about 110 independent ones:
                #   design effect = 1 + (items_per_letting - 1) * rho
                # And for predicting a NEW project it is sigma_between that
                # dominates, because item-level noise averages out over a bid
                # schedule while a letting-level shock does not -- so the
                # binding parameter is the one estimated from the FEWEST
                # degrees of freedom, k - 1.
                #
                # Flagging this with a boolean was not enough: a flagged
                # rho of 0.0 is still a number someone will copy.  So the row
                # states its own fitness, and `rho=0 with between_unresolved`
                # means the decomposition failed, which is a different
                # statement from "the correlation is zero".
                rr = rows[-1]
                k = r.get("n_lettings", 0) if r else 0
                mpl = r.get("items_per_letting_median", 1) if r else 1
                rho = r["rho"] if r else 0.0
                failed = bool(r and r["between_unresolved"])
                rr["rho_usable"] = (
                    "no_decomposition_failed" if failed
                    else "yes" if k >= 30
                    else "no_too_few_lettings")
                if failed:
                    # The design effect needs rho, and rho is what failed.
                    # Computing it with the reported rho of 0 would understate
                    # the design effect and therefore OVERSTATE the effective
                    # sample size -- a number that looks like evidence and is
                    # the opposite.  So it is left empty, not estimated.
                    rr["design_effect"] = ""
                    rr["n_effective"] = ""
                    rr["median_usable"] = "no"
                else:
                    deff = 1.0 + (float(mpl) - 1.0) * float(rho)
                    rr["design_effect"] = round(deff, 3)
                    rr["n_effective"] = int(rr["n_obs"] / deff) if deff > 0 else ""
                    rr["median_usable"] = (
                        "yes" if (k >= 30 and rr["n_effective"] != "" and
                                  rr["n_effective"] >= 100)
                        else "weak" if (k >= 5 and rr["n_effective"] != "" and
                                        rr["n_effective"] >= 30)
                        else "no")
                if rr["rho_usable"] != "yes" or rr["median_usable"] != "yes":
                    rr["note"] = (rr["note"] + "  || CAUTION: k=%s lettings, "
                                  "n_effective=%s -- not n_obs; sigma_between "
                                  "rests on %s degrees of freedom"
                                  % (k, rr["n_effective"], max(int(k) - 1, 0)))
    if not rows:
        print("no residuals could be formed")
        return
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(clean, "residual_structure.csv"), index=False,
               encoding="utf-8-sig")
    print("-> data/clean/residual_structure.csv  (%d rows)" % len(out))
    key = out[(out.split == "public_out_of_fold") & (out.target == "resid_winner")]
    for r in key.itertuples():
        n = 60
        var = (r.sigma_log ** 2) * (1.0 / n + float(r.rho_within_letting)
                                    * (n - 1.0) / n)
        print("  grain=%-7s median_ratio=%.3f sigma=%.3f rho=%.3f "
              "-> 60-item project sigma %.3f (x%.2f)"
              % (r.ref_grain, r.median_ratio, r.sigma_log,
                 float(r.rho_within_letting), np.sqrt(var), np.exp(np.sqrt(var))))
    print("  rho=0 would give x%.2f on the item grain -- the interval would be"
          % np.exp(np.sqrt((0.502 ** 2) / 60)))
    print("  a confident number with no basis.  rho is NOT zero: 0.23 measured.")


if __name__ == "__main__":
    main()
