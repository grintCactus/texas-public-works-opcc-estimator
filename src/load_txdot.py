"""
Load the TxDOT DFW item-level bid tabulations into the project's schema.

This file is already structured, so none of the PDF machinery applies -- but it
needs the same normalisation (units, item families, contractor names) to be
poolable with the city bid tabs, plus two things the PDFs cannot give:

  * `low_bidder_flag` and `bid_rank_sequence_number` -- so the LOW BID, which is
    what the estimate has to land on, is directly observable;
  * a real let date on every row, and enough projects to estimate the
    competition discount (E[low]/median as a function of bidder count) from
    hundreds of lettings instead of the eight city projects.

"Txdot Engineer's Estimate" appears as a vendor; it is an estimate, not a bid,
so it is flagged and excluded from every bid statistic.

Outputs
  data/clean/txdot_line_items.csv     one row per (letting x vendor x item)
  data/clean/txdot_project_totals.csv one row per (letting x vendor)
  data/clean/competition_discount.csv low/median by bidder count, measured
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_bidtabs import (norm_unit, norm_desc, item_family, extract_size_in,
                           canon_bidder, MATERIAL_RE, ENGINEER_RE, is_time_bid_item)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "external", "txdot", "txdot_bidtabs_dfw.csv")
OUT = os.path.join(ROOT, "data", "clean")

EE_RE = ENGINEER_RE


def main():
    if not os.path.exists(SRC):
        print("missing:", SRC)
        return
    df = pd.read_csv(SRC, encoding="utf-8-sig", low_memory=False)
    print("raw rows:", len(df))

    # A bare "engineer" substring also matches real contractors whose company
    # name contains the word.  Measured on this file: it hits 2,988 rows of
    # WG ENGINEERING & CONSTRUCTION, LLC across 32 lettings, and because those
    # rows are then dropped as estimates, 5 lettings have their low bid
    # replaced by the second-lowest (A00184729 +10.9%, A00193674 +8.3%,
    # A00184778 +5.3%, A00184368 +1.9%, A00193656 +0.7%) and one sole-bidder
    # letting (A00200313) vanishes entirely.  That corrupts the TARGET
    # VARIABLE, which makes it the single most damaging defect in the pipeline.
    #
    # ENGINEER_RE was already imported for exactly this and had gone unused.
    # It agrees with the authoritative rank field perfectly: on 209,104 rows,
    # XOR(rank == "EE", ENGINEER_RE) == 0, while XOR(rank, bare substring)
    # == 2,988.  Both signals are kept because they are independent and in
    # exact agreement; if they ever diverge the row count will say so.
    df["is_engineer_estimate"] = (
        df.vendor_name.fillna("").map(lambda s: EE_RE.search(s) is not None)
        | (df.bid_rank_sequence_number.astype(str).str.upper() == "EE"))
    df["unit_price"] = pd.to_numeric(df.bid_item_unit_price_amount, errors="coerce")
    df["quantity"] = pd.to_numeric(df.bid_item_quantity, errors="coerce")
    df["bid_total_amount"] = pd.to_numeric(df.bid_total_amount, errors="coerce")
    df["bid_date"] = pd.to_datetime(df.project_actual_let_date, errors="coerce").dt.strftime("%Y-%m-%d")
    df["is_low_bidder"] = df.low_bidder_flag.astype(str).str.lower().eq("true")
    df["bid_rank"] = pd.to_numeric(df.bid_rank_sequence_number, errors="coerce")

    # How many real bidders did each letting draw? This is the covariate the
    # low-bid target depends on, and the basis for the competitive/sole split.
    # A LETTING is (project_id, let date), not project_id.  7 of 697 project_ids
    # in this extract were let twice, and merging their bidders is not a rounding
    # error -- it corrupts the very field the price reference filters on:
    #   A00184778  counted 6 bidders; the two lettings drew 1 and 6
    #   A00214300  counted 15;        the two drew 11 and 10
    #   A00216266  counted 11;        the two drew 6 and 9
    # The first case turns a SOLE-bid letting into a "competitive" one, so a
    # price set without competition enters the reference as though it had been
    # competed.  The 15-bidder figure also lands in the sparse tail of the
    # competition-discount curve, where one letting carries real weight.
    real = df[~df.is_engineer_estimate]
    nb = (real.groupby(["project_id", "bid_date"])["vendor_name"].nunique()
          .rename("n_bidders"))
    df = df.merge(nb, left_on=["project_id", "bid_date"], right_index=True,
                  how="left")
    df["n_bidders"] = df.n_bidders.fillna(0).astype(int)
    df["competition_class"] = np.select(
        [df.n_bidders >= 3, df.n_bidders == 2, df.n_bidders == 1],
        ["competitive", "thin", "sole"], default="none")

    desc = df.bid_item_description.fillna("").astype(str)
    # The B half of an A+B bid (parse_bidtabs.is_time_bid_item): 800-7001 'NO.
    # OF WORKING DAYS', $/D, days x the daily road user cost.  It ranks the
    # bids and is not paid -- bid_total_amount already leaves it out -- so it
    # leaves txdot_line_items (every bidder total, low bid and unit-price table
    # built from it) and its amount is kept per bidder as time_bid_amount.
    # Tail audit 2026-11-20: A00066071 read Archer Western $96,052,232.47
    # against TxDOT's $93,868,232.48.
    df["is_time_bid"] = [is_time_bid_item(c, u, s) for c, u, s in
                         zip(df.bid_code, df.measurement_unit, desc)]
    df["time_bid_amount"] = np.where(df.is_time_bid, df.quantity * df.unit_price, 0.0)
    df["desc_norm"] = desc.map(norm_desc)
    df["item_family"] = df.desc_norm.map(item_family)
    df["size_in"] = df.desc_norm.map(extract_size_in)
    df["material"] = df.desc_norm.map(
        lambda d: (MATERIAL_RE.search(d).group(1).upper() if MATERIAL_RE.search(d) else ""))
    df["unit"] = df.measurement_unit.fillna("").map(norm_unit)
    df["bidder"] = df.vendor_name.fillna("").map(canon_bidder)

    keep = pd.DataFrame({
        "src_file": "external/txdot/txdot_bidtabs_dfw.csv",
        "source_set": "external",
        "owner": "txdot",
        "project_id": df.project_id,
        "project_name": df.project_name,
        "county": df.county,
        "district": df.district_division,
        "project_type": df.project_type,
        "project_classification": df.project_classification,
        "bid_date": df.bid_date,
        "item_no": df.bid_code,
        "description": desc,
        "desc_norm": df.desc_norm,
        "item_family": df.item_family,
        "size_in": df.size_in,
        "material": df.material,
        "unit_raw": df.measurement_unit,
        "unit": df.unit,
        "quantity": df.quantity,
        "bidder": df.bidder,
        "is_engineer_estimate": df.is_engineer_estimate,
        "is_low_bidder": df.is_low_bidder,
        "bid_rank": df.bid_rank,
        "n_bidders": df.n_bidders,
        "competition_class": df.competition_class,
        "unit_price": df.unit_price,
        "bid_total_amount": df.bid_total_amount,
    })
    keep = keep[keep.unit_price.notna() & (keep.unit_price > 0) & ~df.is_time_bid]
    print("A+B time items dropped (800-x001, $/D): %d rows in %d lettings"
          % (int(df.is_time_bid.sum()),
             df.loc[df.is_time_bid, ["project_id", "bid_date"]].drop_duplicates().shape[0]))
    keep.to_csv(os.path.join(OUT, "txdot_line_items.csv"), index=False, encoding="utf-8-sig")

    # ---- project x vendor totals ----------------------------------------
    tot = (df.groupby(["project_id", "project_name", "bid_date", "county",
                       "project_type", "project_classification", "vendor_name",
                       "is_engineer_estimate", "is_low_bidder", "n_bidders"],
                      dropna=False)
           .agg(bid_total=("bid_total_amount", "max"),
                n_items=("bid_code", "nunique"),
                # B of an A+B bid: ranked on, not in bid_total, not paid
                time_bid_amount=("time_bid_amount", "sum"))
           .reset_index())
    tot.to_csv(os.path.join(OUT, "txdot_project_totals.csv"), index=False, encoding="utf-8-sig")

    # ---- the competition discount, measured ------------------------------
    # Earlier this had to be inferred from eight city projects. Here it is
    # observed directly: for each letting, the winning total over the median
    # total, grouped by how many bidders turned up.
    t = tot[(~tot.is_engineer_estimate) & tot.bid_total.notna() & (tot.bid_total > 0)]
    rows = []
    # Grouped by LETTING, not project: see the n_bidders comment above.  A
    # re-let project pooled here would mix two different markets' bids into one
    # spread and one low/median ratio.
    for (pid, bdate), sub in t.groupby(["project_id", "bid_date"]):
        v = sub.drop_duplicates("vendor_name")
        if len(v) < 1:
            continue
        b = v.bid_total.values
        rows.append({"project_id": pid, "bid_date": bdate, "n_bidders": len(b),
                     "low": b.min(), "median": float(np.median(b)),
                     "mean": b.mean(), "high": b.max(),
                     "sigma_log": float(np.std(np.log(b), ddof=1)) if len(b) > 1 else np.nan,
                     "low_over_median": b.min() / float(np.median(b))})
    pj = pd.DataFrame(rows)
    disc = (pj.groupby("n_bidders")
            .agg(n_lettings=("project_id", "size"),
                 low_over_median=("low_over_median", "median"),
                 p25=("low_over_median", lambda s: s.quantile(.25)),
                 p75=("low_over_median", lambda s: s.quantile(.75)),
                 sigma_log=("sigma_log", "median"))
            .reset_index())
    disc["train_scope"] = "txdot_published_only"  # one public dataset; no
    # company rows exist on this path, so no train/test gate applies
    disc.to_csv(os.path.join(OUT, "competition_discount.csv"), index=False, encoding="utf-8-sig")
    pj.to_csv(os.path.join(OUT, "txdot_letting_spread.csv"), index=False, encoding="utf-8-sig")

    # ---- console ---------------------------------------------------------
    print("kept line items:", len(keep))
    print("lettings:", df.project_id.nunique(),
          "| vendors:", real.vendor_name.nunique(),
          "| date range:", keep.bid_date.min(), "->", keep.bid_date.max())
    print("\n=== 竞争强度分布 (按 letting) ===")
    print(pj.n_bidders.value_counts().sort_index().to_string())
    print("\n=== 实测竞争折扣 (最低价 / 中位价) ===")
    print(disc[disc.n_lettings >= 3].to_string(index=False))
    print("\n=== project_classification x 投标家数 ===")
    cc = (tot[~tot.is_engineer_estimate].drop_duplicates("project_id")
          .groupby("project_classification")
          .agg(lettings=("project_id", "size"), median_bidders=("n_bidders", "median"))
          .sort_values("lettings", ascending=False))
    print(cc.head(12).to_string())
    print("\n-> data/clean/txdot_line_items.csv, txdot_project_totals.csv,")
    print("   competition_discount.csv, txdot_letting_spread.csv")


if __name__ == "__main__":
    main()
