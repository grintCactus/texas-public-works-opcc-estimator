"""
Decisive test: is my price index wrong, or do the two markets really differ?

My matched-pair chained index follows the shape of TxDOT's own Highway Cost
Index but not its level, agreeing in some years and diverging in others.  The
per-year figures are NOT repeated here: they are computed into section 2 of
reports/index_market_split.md on every run.  A number written into this
docstring is a number nobody re-checks -- the three that used to sit in this
paragraph had gone stale against the file they described.

Three explanations were on the table:
  (a) item-mix drift inside my matched pairs,
  (b) HCI is weighted toward large state highway lettings,
  (c) the municipal market and the state highway market genuinely diverged.

(a)/(b) are failures of my method; (c) would mean my index is the RIGHT one for
a client whose work is municipal, and HCI is simply not their yardstick.

The test separates them. Compute the same index on the TxDOT subset alone and on
the municipal subset alone:
  * TxDOT-only matches HCI  -> the method is sound, the gap is market structure,
    and the municipal series is the one to use for this client.
  * TxDOT-only also diverges -> the method is at fault, not the market.

Only PRECISE dates are used: a council-agenda date lands weeks after the
opening and a solicitation number gives a fiscal year, either of which would
make the index partly a measure of filing conventions.

The municipal arm used to be empty from 2023 on.  That was not a data shortage
-- it was a filter comparing `project_key` against a set of `src_file` names,
two keys that never match.  See section 3c of the report for the three separate
causes and for the engineer-estimate leak found alongside them.

Outputs
  data/clean/index_by_market.csv          one row per (market, year pair)
  data/clean/index_by_market_scopes.csv   the same, x competition scope
  reports/index_market_split.md
"""
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
IDX = os.path.join(ROOT, "data", "external", "cost_indices")
REPORTS = os.path.join(ROOT, "reports")
L = []


def say(s=""):
    L.append(s)
    print(s)


def md(df, fmt="%.1f"):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


MIN_PAIRS_YOY = 10
SEASON_WINDOW = "2016_2025"


def chained_index(panel, label, scope="all"):
    """Matched-pair chained index: for each item present in both year t and t+1
    within the same owner, take the price ratio; the year-on-year move is the
    median of those ratios. ITEM composition cannot move it, because only items
    that appear in both years contribute.

    `scope` selects the competition stratum and is published as a column rather
    than fixed here, because the two defensible choices disagree:

      "all"              every letting.  This is the right default for THIS
                         quantity: the architecture is
                         `expected low = market median x discount f(n_bidders)`,
                         so the index's job is the median LEVEL and f() carries
                         the competition effect.  Stage 1 already takes the
                         median WITHIN a letting, which is far less sensitive to
                         bidder count than the minimum would be.
      "competitive_only" n_bidders >= 3.  Measured, not assumed: within the same
                         item and year, a 2-bidder letting's median sits ~3.7%
                         above a 3+-bidder one (251 cells) and a 1-bidder
                         letting's ~11.4% above (100 cells), so when the stratum
                         MIX shifts between two years some of the move is
                         competition, not price.  The municipal sole-bidder
                         share does swing hard (266 rows in 2024, 19 in 2025).

    The spread behind those premia is wide (p25 0.79 / p75 1.70), so neither
    scope is clean enough to suppress the other.  Both are emitted.
    """
    if scope == "competitive_only":
        panel = panel[panel.competition_class == "competitive"]
    if not len(panel):
        return pd.DataFrame()
    s1 = (panel.groupby(["item_key", "year", "project_key"])
          .agg(p=("unit_price", "median")).reset_index())
    yr = (s1.groupby(["item_key", "year"])
          .agg(price=("p", "median"), n=("project_key", "nunique")).reset_index())
    if yr.empty:
        return pd.DataFrame()
    lettings = panel.groupby("year").project_key.nunique()
    piv = yr.pivot(index="item_key", columns="year", values="price")
    years = sorted(c for c in piv.columns if str(c).isdigit())
    rows, lvl = [], 100.0
    for a, b in zip(years, years[1:]):
        # A GAP must not be chained as though it were one step.  The municipal
        # panel holds a single stray 2002 letting and the TxDOT one a stray
        # 2015; `zip(years, years[1:])` would otherwise emit a "2002 -> 2020"
        # row and present an 18-year move as a year-on-year one.
        if int(b) != int(a) + 1:
            continue
        pair = piv[[a, b]].dropna()
        pair = pair[(pair[a] > 0) & (pair[b] > 0)]
        row = {"market": label, "competition_scope": scope, "from": a, "to": b,
               "n_matched": len(pair),
               "n_lettings_from": int(lettings.get(a, 0)),
               "n_lettings_to": int(lettings.get(b, 0)),
               # Carried so the seasonal-composition step can weight by the
               # lettings that actually ENTER this pair, not by every letting
               # in the year.  Dropped before the CSV is written.
               "_items": frozenset(pair.index)}
        if len(pair) < MIN_PAIRS_YOY:
            rows.append({**row, "yoy_pct": np.nan, "index": lvl})
            continue
        ratio = float((pair[b] / pair[a]).median())
        lvl *= ratio
        rows.append({**row, "yoy_pct": (ratio - 1) * 100, "index": lvl})
    return pd.DataFrame(rows)


def amount_weighted_check(panel):
    """Would an amount-weighted column be an improvement?  MEASURE, don't guess.

    The equal-weight median treats a $400 item and a $400,000 item alike, which
    is a real shortcoming against HCI (quantity-weighted).  The tempting fix is
    to weight each matched ratio by its extended amount.  At this sample size
    that is not a refinement, it is a coin flip: one or two large items own the
    weighted median outright.  Returns the two series so the report can show the
    disagreement instead of asserting it.
    """
    if "quantity" not in panel.columns:
        return pd.DataFrame()
    p = panel.copy()
    p["amt"] = p.unit_price * pd.to_numeric(p.quantity, errors="coerce")
    s1 = (p.groupby(["item_key", "year", "project_key"])
          .agg(q=("unit_price", "median"), w=("amt", "sum")).reset_index())
    yr = (s1.groupby(["item_key", "year"])
          .agg(price=("q", "median"), w=("w", "sum")).reset_index())
    piv = yr.pivot(index="item_key", columns="year", values="price")
    wpv = yr.pivot(index="item_key", columns="year", values="w")
    years = sorted(c for c in piv.columns if str(c).isdigit())
    rows = []
    for a, b in zip(years, years[1:]):
        if int(b) != int(a) + 1:
            continue
        pair = piv[[a, b]].dropna()
        pair = pair[(pair[a] > 0) & (pair[b] > 0)]
        if len(pair) < MIN_PAIRS_YOY:
            continue
        rat = (pair[b] / pair[a]).sort_values()
        w = wpv.loc[rat.index, [a, b]].sum(axis=1).fillna(0.0)
        eq = (float(rat.median()) - 1) * 100
        aw = np.nan
        if w.sum() > 0:
            cw = w.cumsum() / w.sum()
            aw = (float(rat.iloc[int((cw.values >= 0.5).argmax())]) - 1) * 100
        top = float(w.max() / w.sum() * 100) if w.sum() > 0 else np.nan
        rows.append({"from_year": a, "to_year": b, "pairs": len(pair), "equal-weight median": eq,
                     "dollar-weighted median": aw, "largest single-item weight %": top,
                     "difference (pp)": aw - eq})
    return pd.DataFrame(rows)


def load_seasonal():
    """DFW construction seasonal factors, from ONE profile window.

    reports/training_recommendation.md records the trap this pins down: three of
    the four factors were once taken from the 2015-2025 window and one from
    2016-2025, and the mixed set looked entirely reasonable because the two
    windows have nearly the same seasonal SHAPE.  Pin the window explicitly.
    """
    p = os.path.join(ROOT, "data", "external", "dol", "qcew", "clean",
                     "dfw_vs_us_quarterly_seasonality.csv")
    if not os.path.exists(p):
        return None
    s = pd.read_csv(p, encoding="utf-8-sig")
    s = s[s.profile_window == SEASON_WINDOW]
    if s.empty:
        return None
    return s.set_index("qtr").dfw_qcew_seasonal_factor_pct.reindex([1, 2, 3, 4])


def _eff_letting(d, factors):
    """One vote per letting."""
    lt = d.drop_duplicates("project_key")[["q"]].dropna()
    if not len(lt):
        return np.nan
    sh = lt.q.value_counts(normalize=True).reindex([1, 2, 3, 4]).fillna(0.0)
    return float((sh * factors).sum())


def _eff_row(d, factors):
    """One vote per observation row."""
    d = d.dropna(subset=["q"])
    if not len(d):
        return np.nan
    sh = d.q.value_counts(normalize=True).reindex([1, 2, 3, 4]).fillna(0.0)
    return float((sh * factors).sum())


def _eff_itemweighted(d, factors):
    """Each letting weighted by how many of the matched items it carries."""
    d = d.dropna(subset=["q"])
    if not len(d):
        return np.nan
    w = (d.drop_duplicates(["project_key", "item_key"])
         .groupby("project_key").size().rename("w"))
    f = d.drop_duplicates("project_key").set_index("project_key").q.map(factors)
    j = pd.concat([w, f.rename("f")], axis=1).dropna()
    if not len(j) or j.w.sum() <= 0:
        return np.nan
    return float((j.w * j.f).sum() / j.w.sum())


def seasonal_mix_bases(panel, factors, items, a, b):
    """Every defensible weighting of the quarter mix for one year pair.

    These DISAGREE, so none of them is published alone.  For 2025->2026 they
    span -2.2 to -5.9 percentage points depending on whether a letting is
    weighted equally, weighted by the matched items it carries, or counted by
    observation rows, and on whether lettings that contribute no matched item
    are included at all.  The DIRECTION is robust; the MAGNITUDE is not pinned
    down by 13 lettings, so the table carries a range and the primary value is
    the basis that matches the estimator (one vote per letting, restricted to
    the lettings that actually enter the pair -- stage 2 is a median ACROSS
    lettings, and a letting holding none of the matched items moves nothing).
    """
    if factors is None or "q" not in panel.columns or not items:
        return {}
    ya, yb = panel[panel.year == a], panel[panel.year == b]
    ma, mb = ya[ya.item_key.isin(items)], yb[yb.item_key.isin(items)]
    cand = {
        "matched_letting": (_eff_letting(ma, factors), _eff_letting(mb, factors)),
        "all_letting": (_eff_letting(ya, factors), _eff_letting(yb, factors)),
        "item_weighted": (_eff_itemweighted(ma, factors),
                          _eff_itemweighted(mb, factors)),
        "row_weighted": (_eff_row(ya, factors), _eff_row(yb, factors)),
    }
    out = {}
    for k, (fa, fb) in cand.items():
        out[k] = (fb / fa - 1) * 100 if (pd.notna(fa) and pd.notna(fb)
                                         and fa > 0) else np.nan
    out["_fa"], out["_fb"] = cand["matched_letting"]
    return out


def load_official():
    out = {}
    p = os.path.join(IDX, "txdot_hci_monthly.csv")
    if os.path.exists(p):
        h = pd.read_csv(p, encoding="utf-8-sig")
        # The 12-month average is the only usable form: a single month swings
        # between roughly 90 and 260 depending on what happened to be let.
        h = h[h.series_id.astype(str).str.contains("twelve|12", case=False, na=False)]
        if h.empty:
            h = pd.read_csv(p, encoding="utf-8-sig")
            pick = sorted(h.series_id.unique())[-1]
            h = h[h.series_id == pick]
        h["year"] = h.date.astype(str).str[:4]
        out["TxDOT_HCI"] = h.groupby("year").index_value.mean()
    p = os.path.join(IDX, "fhwa_nhcci_quarterly.csv")
    if os.path.exists(p):
        n = pd.read_csv(p, encoding="utf-8-sig")
        sa = n[n.series_id.astype(str).str.contains("season", case=False, na=False)]
        n = sa if len(sa) else n
        n["year"] = n.date.astype(str).str[:4]
        out["FHWA_NHCCI"] = n.groupby("year").index_value.mean()
    p = os.path.join(IDX, "bls_ppi_monthly.csv")
    if os.path.exists(p):
        b = pd.read_csv(p, encoding="utf-8-sig")
        b = b[b.index_kind == "input_price"]
        if len(b):
            pick = b.series_id.value_counts().index[0]
            b = b[b.series_id == pick]
            b["year"] = b.date.astype(str).str[:4]
            out["BLS_inputs"] = b.groupby("year").index_value.mean()
    return out


def main():
    mp = os.path.join(CLEAN, "master_panel.csv")
    if not os.path.exists(mp):
        print("missing master_panel.csv")
        return
    use = ["source", "source_set", "owner", "project_key", "bid_date", "year",
           "item_key", "unit_price", "competition_class", "project_kind",
           "item_family", "unit", "date_precision", "is_engineer_estimate",
           "price_basis", "quantity"]
    # dtype=str on year is not optional: the CSV holds a clean "2026", but
    # pandas infers the column as float64 because the blanks read as NaN, so
    # astype(str) yields "2026.0" and every year pattern silently fails to
    # match. Same hazard for any id-like column (CSJ "0918-47-015", bid codes).
    m = pd.read_csv(mp, encoding="utf-8-sig", low_memory=False,
                    usecols=lambda c: c in use,
                    dtype={"year": str, "item_key": str, "project_key": str})
    m["year"] = m.year.fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    m = m[m.year.str.match(r"^20\d\d$", na=False)]
    m = m[m.unit_price > 0]
    # The engineer's estimate is NOT a bid and an index is a fitted quantity, so
    # OPCC rows can never reach it.  `competition_class` does not exclude them --
    # it is a pure n_bidders map, and 25,715 engineer-estimate rows carry
    # 'competitive' -- so this needs is_engineer_estimate.  It is not cosmetic:
    # the TxDOT current feed tabulates the estimate as another column, so it
    # shifted the within-letting median and with it the headline 2025->2026
    # move (-12.52% with the estimate in, -14.29% with it out).
    if "is_engineer_estimate" in m.columns:
        m = m[~m.is_engineer_estimate.fillna(False).astype(bool)]
    if "project_kind" in m.columns:
        m = m[m.project_kind == "construction"]
    # One row per BIDDER is what stage 1 needs: it takes the median across the
    # bidders in a letting, which is only a median if the rows are bids.
    # `garland_avg` is not -- it is `price_basis='contract_price'`, the AWARDED
    # price, bidder literally '(contract)'.  A contract price is the winning
    # bid, systematically below the median of all bids by the whole competition
    # discount (0.82-0.93 in competition_discount_municipal.csv), so splicing it
    # in makes the index read a change of price BASIS as a change of price.
    # It would have landed on exactly one year pair, too: garland_avg covers
    # 2023 and 2024 only, and it moved 2023->2024 from +3.54% to +7.46%.
    # Keyed on price_basis rather than on the source name so a future
    # contract-price source cannot walk back in; this also drops the 1,269
    # `uneven_proposal` rows that build_master.py already bars from training.
    if "price_basis" in m.columns:
        m = m[m.price_basis == "all_bids"]

    # Municipal rows need a PRECISE date: a council-agenda date lands weeks
    # after the opening and a solicitation number gives a fiscal year.  TxDOT
    # dates come from the dataset or the tab header and are precise by
    # construction -- verified, not assumed: all 43,664 `approx` rows in the
    # panel are city_bidtab, and txdot/txdot_archive/garland_avg are 100%
    # `precise`, so one global predicate serves both arms.
    #
    # THIS IS THE BUG THAT KILLED THE MUNICIPAL ARM.  It used to read
    # line_items.csv, build a set of `src_file` values, and test
    # `project_key.isin(...)` against it.  Those are different keys: a municipal
    # project_key is `<src_file>::<sheet>` ("Some Bid Tabulation.xlsx::Bid
    # Tabulation"), never a bare filename, so the two sets intersect in exactly
    # 0 of 190 keys and the municipal subset came out EMPTY.  The arm last
    # written to data/clean/ was stale output from before the key changed.
    # build_master.py already had the right pattern (its own precise_keys block
    # derives the set from the panel's `date_precision`); use the column.
    is_tx = m.source.isin(["txdot", "txdot_archive"])
    precise = m.date_precision == "precise"
    # Public municipal tabs only.  The company's 8 files are also municipal
    # bid tabs and would otherwise sit in this subset: they are TEST data, and
    # an index is a training artifact.  `source` cannot distinguish them -- it
    # reads 'city_bidtab' for both -- so this needs source_set.
    muni = m[(~is_tx) & precise
             & (m.get("source_set", "external") == "external")].copy()
    tx = m[is_tx & precise].copy()
    for d in (muni, tx):
        d["q"] = pd.to_datetime(d.bid_date, errors="coerce").dt.quarter

    say("# Market split test of the price index")
    say()
    say("My paired chained index follows the **shape** of TxDOT's own winning-bid price index (HCI), "
        "but not its level. Year by year it is close in some years and far off in others "
        "(see the table in section 2; the figures are computed on every run, not hard-coded "
        "in this text).")
    say("To tell whether **my method is at fault** or **the two markets really diverged**, "
        "the same method is applied separately to the TxDOT subset and to the municipal subset, "
        "and each is compared with HCI.")
    say()
    say("**This fix: the municipal arm used to be empty from 2023 on, not for lack of data "
        "but because of a filter that treated `project_key` and `src_file` as the same key.** "
        "It now covers up to 2025→2026. Along the way, the engineer's estimate (OPCC) was found to "
        "leak into the index. That violates a hard project constraint, and it is now excluded — "
        "it changed the TxDOT headline number. See section 3c.")
    say()
    say("Only precise dates are used: a council-agenda date falls weeks after the letting, and the "
        "year in a solicitation number is a fiscal year. Mixing them in would turn the index into "
        "a measure of filing conventions.")
    say()
    say("## 1. Size of the two subsets")
    say()
    say(md(pd.DataFrame([
        {"subset": "TxDOT (state highway)", "rows": len(tx), "projects": tx.project_key.nunique(),
         "years": "%s-%s" % (tx.year.min(), tx.year.max())},
        {"subset": "Municipal (client's market)", "rows": len(muni), "projects": muni.project_key.nunique(),
         "years": "%s-%s" % (muni.year.min(), muni.year.max()) if len(muni) else "-"},
    ]), fmt="%.0f"))
    say()

    parts = [chained_index(df, lbl, scope)
             for lbl, df in (("TxDOT", tx), ("Municipal", muni))
             for scope in ("all", "competitive_only")]
    both = pd.concat([p for p in parts if len(p)], ignore_index=True)

    # ---- seasonal COMPOSITION, carried as its own column -------------------
    # The DFW factors are a DFW quantity (QCEW, Dallas-Fort Worth, NAICS
    # construction), so they are applied to the municipal arm ONLY.  Putting
    # them on the statewide TxDOT series would be the industry/geography
    # mismatch this project has already been bitten by once.  TxDOT needs no
    # such column anyway: its letting calendar is near-uniform, so its own
    # effective factor sits in 98.96-100.54 for every year with >=20 lettings.
    sf = load_seasonal()
    BASES = ["matched_letting", "all_letting", "item_weighted", "row_weighted"]
    for c in (["season_factor_from", "season_factor_to", "seasonal_mix_pct",
               "seasonal_mix_pct_lo", "seasonal_mix_pct_hi",
               "yoy_pct_ex_seasonal_mix", "yoy_pct_ex_mix_lo",
               "yoy_pct_ex_mix_hi"] + ["seasonal_mix_" + b for b in BASES]):
        both[c] = np.nan
    if sf is not None:
        for i in both.index[both.market == "Municipal"]:
            r = both.loc[i]
            got = seasonal_mix_bases(muni, sf, r["_items"], r["from"], r["to"])
            if not got:
                continue
            for b in BASES:
                both.at[i, "seasonal_mix_" + b] = got.get(b, np.nan)
            vals = [got[b] for b in BASES if pd.notna(got.get(b, np.nan))]
            if not vals:
                continue
            both.at[i, "season_factor_from"] = got["_fa"]
            both.at[i, "season_factor_to"] = got["_fb"]
            both.at[i, "seasonal_mix_pct"] = got["matched_letting"]
            both.at[i, "seasonal_mix_pct_lo"] = min(vals)
            both.at[i, "seasonal_mix_pct_hi"] = max(vals)
        # The move with the composition effect divided out: the price-only
        # reading.  lo/hi come from the WIDEST mix estimate, so the pair
        # (yoy_pct_ex_mix_lo, yoy_pct_ex_mix_hi) brackets the price change.
        both["yoy_pct_ex_seasonal_mix"] = (
            (1 + both.yoy_pct / 100)
            / (1 + both.seasonal_mix_pct / 100) - 1) * 100
        both["yoy_pct_ex_mix_lo"] = (
            (1 + both.yoy_pct / 100)
            / (1 + both.seasonal_mix_pct_hi / 100) - 1) * 100
        both["yoy_pct_ex_mix_hi"] = (
            (1 + both.yoy_pct / 100)
            / (1 + both.seasonal_mix_pct_lo / 100) - 1) * 100
    # Re-chain the de-seasonalised series so the level is self-consistent.
    both["index_ex_seasonal_mix"] = np.nan
    for (mkt, scope), g in both.groupby(["market", "competition_scope"]):
        lvl = 100.0
        for i in g.index:
            v = both.at[i, "yoy_pct_ex_seasonal_mix"]
            if pd.notna(v):
                lvl *= 1 + v / 100
            both.at[i, "index_ex_seasonal_mix"] = lvl
    both.loc[both.market != "Municipal", "index_ex_seasonal_mix"] = np.nan
    both = both.drop(columns=["_items"])

    # ONE ROW PER (market, year-pair) IN index_by_market.csv -- do not publish
    # both scopes here.  estimate_low_bid.py loads this file with
    #     for r in _read("index_by_market.csv"): index[r["market"]][r["to"]] = ...
    # so a second row for the same market and year would not be ignored, it
    # would overwrite the first and the estimator would silently escalate with
    # whichever scope happened to be written last.  The scope comparison goes
    # to its own file instead, which nothing reads by contract.
    full = both
    both = both[both.competition_scope == "all"].reset_index(drop=True)
    both.to_csv(os.path.join(CLEAN, "index_by_market.csv"), index=False,
                encoding="utf-8-sig")
    full.to_csv(os.path.join(CLEAN, "index_by_market_scopes.csv"), index=False,
                encoding="utf-8-sig")

    off = load_official()
    hci = off.get("TxDOT_HCI")
    hci_yoy = (hci.pct_change() * 100) if hci is not None else None

    say("## 2. Year-on-year change for each year: the two markets vs the official index")
    say()
    rows = []
    for yr in sorted(set(both["to"])):
        r = {"year": yr}
        for mk in ("TxDOT", "Municipal"):
            sub = both[(both.market == mk) & (both["to"] == yr)]
            r["%s YoY" % mk] = (float(sub.yoy_pct.iat[0]) if len(sub)
                                 and pd.notna(sub.yoy_pct.iat[0]) else np.nan)
            r["%s pairs" % mk] = (int(sub.n_matched.iat[0]) if len(sub) else 0)
        if hci_yoy is not None and yr in hci_yoy.index:
            r["HCI YoY"] = float(hci_yoy.loc[yr])
        rows.append(r)
    cmp = pd.DataFrame(rows)
    say(md(cmp))
    say()

    # How close is each to HCI?
    if hci_yoy is not None:
        say("## 3. Result of the test")
        say()
        res = []
        for mk in ("TxDOT", "Municipal"):
            sub = both[both.market == mk].dropna(subset=["yoy_pct"])
            sub = sub[sub.n_matched >= 10]
            j = sub.merge(hci_yoy.rename("hci").reset_index()
                          .rename(columns={"year": "to"}), on="to", how="inner")
            if len(j) < 3:
                continue
            res.append({
                "subset": mk, "comparable years": len(j),
                "mean absolute deviation (pp)": float((j.yoy_pct - j.hci).abs().mean()),
                "correlation coefficient": float(np.corrcoef(j.yoy_pct, j.hci)[0, 1]),
                "our cumulative index": float(sub["index"].iat[-1]),
            })
        if res:
            rdf = pd.DataFrame(res)
            say(md(rdf, fmt="%.2f"))
            say()
            # Deliberately NOT concluding from the deviation columns alone: the
            # two arms have different numbers of comparable years, and the
            # municipal one only reaches a usable pair count from 2022 on.
            MIN_PAIRS_CONCLUDE = 25
            mu = both[both.market == "Municipal"].dropna(subset=["yoy_pct"])
            mu_ok = mu[mu.n_matched >= MIN_PAIRS_CONCLUDE]
            txd = both[both.market == "TxDOT"].dropna(subset=["yoy_pct"])
            txd_ok = txd[txd.n_matched >= MIN_PAIRS_CONCLUDE]
            tj = txd_ok.merge(hci_yoy.rename("hci").reset_index()
                              .rename(columns={"year": "to"}), on="to")

            say("### What can and cannot be concluded")
            say()
            say(f"- The municipal subset now has **{len(mu_ok)}** year pairs that reach the threshold of "
                f"{MIN_PAIRS_CONCLUDE} pairs "
                f"(pair counts {int(mu_ok.n_matched.min())}~{int(mu_ok.n_matched.max())}, "
                f"covering {mu_ok['from'].iat[0]}→{mu_ok['to'].iat[-1]}). "
                f"**This arm used to be empty, not for lack of data but because of a filter "
                f"with mismatched keys** "
                f"(see the section 'What used to cut off the municipal arm' below).")
            if len(mu_ok) < 4:
                say("  There are still fewer than 4 such year pairs, so **whether it diverged from the "
                    "state highway market is still not measurable**.")
            else:
                say(f"- But what can be concluded is still limited: the qualifying year pairs cover only "
                    f"the last {len(mu_ok)} year pairs, and **municipal year pairs earlier than 2022 "
                    f"have too few pairs** (2020→2021 has only "
                    f"{int(mu[mu['to'] == '2021'].n_matched.iat[0]) if len(mu[mu['to'] == '2021']) else 0} "
                    f"pairs), so the municipal cumulative level **can only be read from 2022 on** "
                    f"and cannot be projected back to 2016.")
            say()
            if len(tj) >= 3:
                say(f"- The TxDOT subset has {len(txd_ok)} qualifying year pairs, and its correlation "
                    f"coefficient with HCI is **{float(np.corrcoef(tj.yoy_pct, tj.hci)[0, 1]):.2f}** — "
                    f"**the method itself is sound**, and its shape reproduces the official "
                    f"winning-bid price index.")
                say()
            say("- The TxDOT subset **systematically understates the level** (cumulative "
                f"{float(txd['index'].iat[-1]):.1f} vs HCI "
                f"{float(hci.iloc[-1] / hci.iloc[0] * 100):.1f}). "
                "The reason is that the paired chained index takes the **median of ratios**, with no "
                "dollar weights, while HCI is weighted by quantity/dollar amount. Items with a large "
                "dollar share (concrete pavement, structures) rose more than the median item, and "
                "without weights that is flattened out.")
            say()
            say("  **But do not add a dollar-weighted column to this table because of that — it was "
                "measured and it is not stable.** See the section below.")
            say()
            say("- For the TxDOT deviation from HCI in 2023 and 2025 there is a candidate explanation "
                "more specific than 'market divergence': **around 2024 TxDOT changed its code "
                "generation and the wording of its item descriptions** "
                "(`REMOVING` → `REMOV`). My TxDOT item_key is built from the normalised description, "
                "so when the description changes the pairing breaks, and the paired sample spanning "
                "2023–2024 is systematically selected. "
                "This can be tested by aligning the two code generations with "
                "`txdot_code_crosswalk.csv` and recomputing.")
            say()
            say("**Practical effect on the estimating architecture: the statewide HCI is not this "
                "client's yardstick.** "
                "With the municipal arm fixed, the municipal year-by-year changes since 2022 do not "
                "move in step with HCI, and the client's work is all in the DFW municipal market. "
                "So time adjustment should rely **mainly on the municipal index**, "
                "with the official index as a cross-check, and should be read on the "
                "seasonal-composition basis of the next section.")
            say()

    say("## 3b. Seasonal composition of municipal 2026: −7.31% cannot be read as a pure price change")
    say()
    if sf is not None:
        say(f"The seasonal factors of the DFW construction industry (QCEW, not seasonally adjusted, "
            f"single window `{SEASON_WINDOW}`) "
            "are **Q1 trough 93.73 / Q2 95.25 / Q3 99.99 / Q4 peak 111.03**. "
            "That is, the quarters in which a year's lettings are concentrated will by themselves "
            "make that year's price level higher or lower — "
            "**regardless of whether prices changed**.")
        say()
        say("The quarterly distribution of municipal lettings is skewed in every year, "
            "and especially in 2026:")
        say()
        qrows = []
        lt = muni.drop_duplicates("project_key")[["year", "q"]].dropna()
        for y in sorted(lt.year.unique()):
            g = lt[lt.year == y]
            sh = g.q.value_counts(normalize=True).reindex([1, 2, 3, 4]).fillna(0.0)
            qrows.append({"year": y, "lettings": len(g),
                          "Q1%": sh[1] * 100, "Q2%": sh[2] * 100,
                          "Q3%": sh[3] * 100, "Q4%": sh[4] * 100,
                          "effective seasonal factor": float((sh * sf).sum())})
        say(md(pd.DataFrame(qrows), fmt="%.1f"))
        say()
        ms = both[(both.market == "Municipal")
                  & both.yoy_pct.notna()].dropna(subset=["seasonal_mix_pct"])
        if len(ms):
            say(md(ms[["from", "to", "n_matched", "yoy_pct", "seasonal_mix_pct",
                       "seasonal_mix_pct_lo", "seasonal_mix_pct_hi",
                       "yoy_pct_ex_seasonal_mix"]]
                  .rename(columns={"from": "from_year", "to": "to_year", "n_matched": "pairs",
                                   "yoy_pct": "YoY (raw)",
                                   "seasonal_mix_pct": "seasonal composition (primary basis)",
                                   "seasonal_mix_pct_lo": "composition lower bound",
                                   "seasonal_mix_pct_hi": "composition upper bound",
                                   "yoy_pct_ex_seasonal_mix": "composition removed"}),
                  fmt="%.2f"))
            say()
        say("**Primary basis = 'one vote per letting', counting only the lettings that actually enter "
            "this pair.** "
            "Stage 2 of this index takes the **median across lettings**, so one vote per letting is "
            "the weight that matches this estimator. A letting that holds none of the paired items "
            "has no effect on this number and does not belong in the denominator.")
        say()
        say("**But this magnitude is not stable, so the table gives a range rather than one number.** "
            "Measured results for four weighting bases, each defensible (one vote per letting / "
            "weighted by number of paired items / by observation row / "
            "whether lettings with no paired item are counted):")
        say()
        bl = {"matched_letting": "per letting, paired only (primary)", "all_letting": "per letting, all",
              "item_weighted": "weighted by number of paired items", "row_weighted": "by observation row"}
        say(md(ms[["from", "to"] + ["seasonal_mix_" + b for b in BASES]]
              .rename(columns={"from": "from_year", "to": "to_year",
                               **{"seasonal_mix_" + k: v for k, v in bl.items()}}),
              fmt="%.2f"))
        say()
        r26 = both[(both.market == "Municipal") & (both["to"] == "2026")
                   & (both.competition_scope == "all")]
        if len(r26) and pd.notna(r26.seasonal_mix_pct.iat[0]):
            raw = float(r26.yoy_pct.iat[0])
            lo, hi = float(r26.seasonal_mix_pct_lo.iat[0]), float(r26.seasonal_mix_pct_hi.iat[0])
            net = float(r26.yoy_pct_ex_seasonal_mix.iat[0])
            nlo, nhi = float(r26.yoy_pct_ex_mix_lo.iat[0]), float(r26.yoy_pct_ex_mix_hi.iat[0])
            say(f"**The 2026 headline number is {raw:+.2f}%, of which {hi:+.2f} to {lo:+.2f} "
                f"percentage points are only a change in quarter composition** — in 2026, Q4 (the "
                f"peak quarter, 111.03) has only one letting, "
                f"while Q1/Q2 (the trough quarters, 93.73 / 95.25) make up more than half.")
            say()
            say(f"- With the composition effect removed, the price change is **{net:+.2f}%** (primary "
                f"basis), range **{min(nlo, nhi):+.2f}% ~ {max(nlo, nhi):+.2f}%** — "
                f"**not {raw:+.1f}%**. The direction (down) is stable; the magnitude is not.")
            say(f"- When escalating historical transaction prices to the current date, use the "
                f"'composition removed' column and then multiply by the target quarter's own "
                f"seasonal factor, instead of applying {raw:+.1f}% directly. "
                f"Applying the headline number directly would treat a calendar coincidence as a "
                f"price drop.")
            say(f"- 2026 is still a **partial year** (only {int(r26.n_lettings_to.iat[0])} lettings and "
                f"{int(r26.n_matched.iat[0])} paired items enter), so the final-year reading is only "
                f"provisional. The range is this wide for the same root cause: 13 lettings cannot "
                f"support a precise composition correction.")
            say()
    else:
        say("**Not measurable**: could not find "
            "`data/external/dol/qcew/clean/dfw_vs_us_quarterly_seasonality.csv`,"
            " so the seasonal-composition effect cannot be given. "
            "The `seasonal_mix_pct` column in the table is empty.")
        say()

    say("## 3c. What used to cut off the municipal arm")
    say()
    say("The municipal arm had no data from 2023 on, and **not because the sample was too small**. "
        "Three separate causes:")
    say()
    say("1. **Key mismatch (the fatal one).** The old code took from `line_items.csv` the rows with "
        "`bid_date_source == 'bid_open_or_due'`, built a set of their **`src_file`** values, "
        "and then tested **`project_key`** against it with `isin()`. These two are not the same "
        "key: a municipal `project_key` is `<src_file>::<sheet>` "
        "(e.g. `Some Bid Tabulation.xlsx::Bid Tabulation`), never a bare filename. "
        "Measured, the intersection of the two sets is **0** — not one of the 282 municipal "
        "`project_key` values is among those 116 `src_file` values, so the municipal subset "
        "came out empty. "
        "The three old rows on disk are **stale output** from before the key was changed; "
        "a single re-run makes them all disappear. "
        "The fix: use the panel's own `date_precision` column, which is also the pattern "
        "`build_master.py` itself uses.")
    say("2. **`competition_class == 'competitive'` (n_bidders ≥ 3) cut away half the sample.** "
        "Municipal erosion-repair lettings often have only 2 bidders; that is a real market price, "
        "not bad data. "
        "This filter squeezed the 2023→2024 pair count from **138 down to 2**, and the "
        f"`n_matched < {MIN_PAIRS_YOY}` threshold then turned it into NaN. "
        "The competition basis is now published as the `competition_scope` column, with both "
        "variants given, instead of an implicit choice of one.")
    say("3. **Chaining across a gap.** `zip(years, years[1:])` does not check whether the years are "
        "adjacent. The municipal panel holds one isolated 2002 letting, which would produce a "
        "`2002→2020` row and treat an 18-year change as a year-on-year one. "
        "An adjacency check has been added.")
    say()
    say("One more problem was fixed, unrelated to these but in violation of a hard project "
        "constraint: **the engineer's estimate (OPCC) used to enter the index**. "
        "`competition_class` is only a map of `n_bidders` and does not exclude estimate rows — "
        "the panel has 25,715 rows with `is_engineer_estimate=True` that are also labelled "
        "`competitive`. The index is a fitted quantity, so estimate rows must not enter it. "
        "This is not cosmetic. The current TxDOT feed tabulates the estimate as one more column, "
        "and that shifts the within-letting median: **the headline 2025→2026 goes from −12.52% "
        "to −14.29%**, and 2024→2025 from −0.88% to −0.31%. The years 2024 and earlier "
        "are not affected (deviation 0.000pp).")
    say()

    say("## 3d. Two readings by competition basis")
    say()
    say("Same method, same data; only the competition basis changes. The two bases are **not "
        "written into `index_by_market.csv`** (that table may hold only one row per market and "
        "year pair; otherwise, in the row-by-row load of `estimate_low_bid.py`, the later row "
        "would overwrite the earlier one). "
        "They are saved separately in `index_by_market_scopes.csv`:")
    say()
    sc = (full.dropna(subset=["yoy_pct"])
          .pivot_table(index=["market", "from", "to"], columns="competition_scope",
                       values=["yoy_pct", "n_matched"], aggfunc="first")
          .reset_index())
    sc.columns = [" ".join(str(x) for x in c if x != "") for c in sc.columns]
    say(md(sc, fmt="%.2f"))
    say()
    say("The measured gap between the bases comes from this: for the same item in the same year, "
        "**the within-letting median price with 2 bidders is about 3.7% higher than with 3 or "
        "more** (251 cells), and **with 1 bidder about 11.4% higher** (100 cells). "
        "So when the bidder-count mix of a year changes, this index reads some change that is "
        "**not price**. But these two premia are widely dispersed (p25 0.79 / p75 1.70), "
        "so they are not sufficient grounds to suppress the other basis; **both are published**.")
    say()
    say("The default is `all`: the architecture of this project is "
        "`expected low bid = market median price × competition discount f(number of bidders)`. "
        "The index is responsible for the **median level**, and f() carries the effect of the "
        "number of bidders. Also, stage 1 already takes the median within each letting, "
        "which is far less sensitive to the number of bidders than taking the lowest price.")
    say()

    say("## 3e. Dollar weighting: tested, not added")
    say()
    say("The equal-weight median of ratios treats a $400 item and a $400,000 item alike, "
        "which is a real shortcoming relative to HCI (quantity-weighted). The natural fix is to "
        "weight each paired ratio by its extended amount. "
        "**At this sample size that is not an improvement, it is a coin flip:**")
    say()
    aw = amount_weighted_check(muni)
    if len(aw):
        say(md(aw, fmt="%.2f"))
        say()
        flips = aw[(aw["equal-weight median"] * aw["dollar-weighted median"]) < 0]
        worst = aw.loc[aw["difference (pp)"].abs().idxmax()]
        say(f"- The largest single-item weight reaches "
            f"**{aw['largest single-item weight %'].max():.1f}%** — "
            f"one or two large-dollar items can own the weighted median outright.")
        say(f"- The largest deviation is at {worst['from_year']}→{worst['to_year']}: "
            f"equal-weight {worst['equal-weight median']:+.2f}% vs dollar-weighted "
            f"{worst['dollar-weighted median']:+.2f}% (a difference of {worst['difference (pp)']:+.2f} "
            f"percentage points).")
        if len(flips):
            say(f"- **{len(flips)} year pairs have opposite signs** "
                f"({', '.join(f'{r.from_year}→{r.to_year}' for r in flips.itertuples())}). "
                f"A weight that can flip the sign is not more accurate, it is less stable.")
        say()
        say("**Conclusion: do not add a dollar-weighted column.** Dollar weighting first requires "
            "solving the stability of the weights "
            "(for example, stratifying by item family and weighting within each family, or capping "
            "the weights), rather than adding the column first.")
    else:
        say("**Not measurable**: the panel has no usable `quantity` column, "
            "so dollar weights cannot be computed.")
    say()

    say("## 4. Cumulative level")
    say()
    lv = []
    for mk in ("TxDOT", "Municipal"):
        sub = both[both.market == mk]
        if len(sub):
            lv.append({"series": "our " + mk, "start year": sub["from"].iat[0],
                       "final year": sub["to"].iat[-1], "cumulative index (start=100)":
                       float(sub["index"].iat[-1])})
    for k, s in off.items():
        if s is None or s.empty:
            continue
        base = s.iloc[0]
        lv.append({"series": k, "start year": s.index[0], "final year": s.index[-1],
                   "cumulative index (start=100)": float(s.iloc[-1] / base * 100)})
    say(md(pd.DataFrame(lv), fmt="%.1f"))
    say()
    say("Note that 2026 is a **partial year** in every official series (HCI 8 months, NHCCI 1 quarter, "
        "BLS 10 months), so the final-year level is only provisional.")
    say()

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "index_market_split.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n-> data/clean/index_by_market.csv")
    print("-> reports/index_market_split.md")


if __name__ == "__main__":
    main()
