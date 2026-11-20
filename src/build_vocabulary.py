"""
Build the item vocabulary: the join target for any external price source.

Two outputs:

  data/clean/item_vocabulary.csv
      Every distinct line item we have, with its normalised keys. When an
      external price table arrives (Dallas bid-item averages, another city's
      bid tab), it gets mapped ONTO this vocabulary. Without a shared
      vocabulary the external data cannot be joined at all, because every
      owner numbers its bid items differently: Rowlett uses 1..60, Garland
      uses 101 / 210.08 / 311.0406, Carrollton uses 1..116.

  data/clean/external_data_priorities.csv
      Which items to go get external prices FOR, ranked. Priority is high
      where the item carries a lot of money across our projects but we have
      thin internal evidence for it (few projects, or wide disagreement
      between bidders). That is where an external anchor buys the most.
"""
import os
import numpy as np
import pandas as pd

import dedup
import vintage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")


def main():
    # Deduped on the way in: this table publishes n_projects and
    # total_dollars, which a letting counted twice inflates directly.
    li = dedup.read_line_items(os.path.join(CLEAN, "line_items.csv"),
                               "build_vocabulary")
    li["is_engineer_estimate"] = li["is_engineer_estimate"].astype(str).str.lower().isin(
        ["true", "1", "yes"])
    for c in ("quantity", "unit_price", "extended_calc", "extended_reported", "size_in"):
        li[c] = pd.to_numeric(li[c], errors="coerce")
    li["ext"] = li.extended_calc.fillna(li.extended_reported)

    # Contractor prices only, base scope only. OPCC is never an input.
    # Same quality gate as the panel: a row whose own quantity x unit price does
    # not reproduce the extended amount printed in the source had one of those
    # three numbers misread out of the PDF, so it cannot be pooled into a price
    # reference. Excluded here too, or a single runaway value swamps the
    # dollar-weighted rollups.
    b = li[(~li.is_engineer_estimate)
           & (li.section_role == "base")
           & (li.ext_mismatch != "Y")
           & (li["unit_suspect"].fillna("") != "Y")].copy()

    # Market price for an item ON A PROJECT = median across that project's bidders.
    # Two-stage pooling: otherwise a 13-bidder project would outweigh a 2-bidder
    # one by 6x on an item they both priced.
    # `source_set` joins the key so provenance SURVIVES into per_proj.  It
    # cannot split any group: a file has exactly one source_set, and src_file is
    # already a key, so the row count is unchanged.  Without it the column dies
    # here and neither item_project_medians.csv nor item_vocabulary.csv can tell
    # the company's 8 files from the public corpus.
    per_proj = (b.groupby(["src_file", "source_set", "desc_norm", "item_family", "unit"])
                .agg(project_median_price=("unit_price", "median"),
                     project_min_price=("unit_price", "min"),
                     project_max_price=("unit_price", "max"),
                     n_bids=("unit_price", "size"),
                     quantity=("quantity", "median"),
                     size_in=("size_in", "median"),
                     material=("material", lambda s: s.dropna().astype(str).mode().iat[0]
                               if len(s.dropna()) and len(s.dropna().astype(str).mode()) else ""),
                     dollars=("ext", "sum"),
                     description=("description", "first"),
                     bid_date=("bid_date", "first"))
                .reset_index())
    per_proj["within_project_spread"] = (
        per_proj.project_max_price / per_proj.project_min_price.replace(0, np.nan))
    per_proj.to_csv(os.path.join(CLEAN, "item_project_medians.csv"),
                    index=False, encoding="utf-8-sig")

    # The VOCABULARY is a training artifact -- its pooled medians are a price
    # reference and its wordings are what new items get matched against -- so it
    # is built from public data only.  The filter sits here, not on `b` above,
    # so item_project_medians.csv keeps both sides (labelled) for the validation
    # work, and only the pooled rollup is restricted.
    vocab = (per_proj[per_proj.source_set == "external"]
             .groupby(["desc_norm", "item_family", "unit"])
             .agg(n_projects=("src_file", "nunique"),
                  n_bid_observations=("n_bids", "sum"),
                  pooled_median_price=("project_median_price", "median"),
                  price_p25=("project_median_price", lambda s: s.quantile(.25)),
                  price_p75=("project_median_price", lambda s: s.quantile(.75)),
                  median_within_project_spread=("within_project_spread", "median"),
                  # qty_median, not just the min/max that were here before: a
                  # range tells a consumer how wide the sample is but gives it
                  # no PIVOT to swing a quantity adjustment around, so every
                  # consumer matching against this table skipped the quantity
                  # stage and applied a factor of exactly 1.0, silently.  Named
                  # to match price_reference.csv and erosion_price_reference.csv
                  # so one code path serves all three.
                  qty_median=("quantity", "median"),
                  qty_min=("quantity", "min"), qty_max=("quantity", "max"),
                  size_in=("size_in", "median"),
                  material=("material", lambda s: s.dropna().astype(str).mode().iat[0]
                            if len(s.dropna()) and len(s.dropna().astype(str).mode()) else ""),
                  total_dollars=("dollars", "sum"),
                  example_description=("description", "first"))
             .reset_index()
             .sort_values("total_dollars", ascending=False))
    vocab["pct_of_all_dollars"] = vocab.total_dollars / vocab.total_dollars.sum() * 100
    vocab["cum_pct_dollars"] = vocab.pct_of_all_dollars.cumsum()
    # Same vintage rule and same module as price_reference.csv.  This table had
    # no vintage, so its consumers each derived one -- and the same quantity
    # derived three times in three places comes out three different ways with
    # nothing in the output saying which.  Weighted on the public side only, to
    # match the rows the pooled median is built from.
    vocab = vintage.attach(vocab, per_proj[per_proj.source_set == "external"],
                           ["desc_norm", "item_family", "unit"],
                           "bid_date", "dollars")
    vocab.to_csv(os.path.join(CLEAN, "item_vocabulary.csv"),
                 index=False, encoding="utf-8-sig")

    # ---- priorities ------------------------------------------------------
    # evidence_score: how well our own data already pins this item down.
    v = vocab.copy()
    v["evidence"] = (v.n_projects.clip(upper=4) / 4.0) * 0.6 + \
                    (np.log1p(v.n_bid_observations) / np.log1p(40)).clip(upper=1) * 0.4
    v["disagreement"] = v.median_within_project_spread.fillna(1).clip(1, 8)
    v["priority"] = (v.pct_of_all_dollars
                     * (1 - v.evidence)
                     * np.log(v.disagreement).clip(lower=0.05))
    pri = v.sort_values("priority", ascending=False)[
        ["desc_norm", "item_family", "unit", "example_description",
         "n_projects", "n_bid_observations", "pooled_median_price",
         "median_within_project_spread", "total_dollars", "pct_of_all_dollars",
         "priority"]]
    pri.to_csv(os.path.join(CLEAN, "external_data_priorities.csv"),
               index=False, encoding="utf-8-sig")

    # ---- console summary -------------------------------------------------
    print("item_vocabulary.csv: %d distinct items, %d item families"
          % (len(vocab), vocab.item_family.nunique()))
    n80 = int((vocab.cum_pct_dollars <= 80).sum()) + 1
    print("金额集中度: 前 %d 个子目(共 %d 个)= 80%% 的金额"
          % (n80, len(vocab)))
    print("\n=== 金额最大的 15 个子目(单价基准最该做准的) ===")
    show = vocab.head(15)[["item_family", "unit", "n_projects", "n_bid_observations",
                           "pooled_median_price", "pct_of_all_dollars",
                           "example_description"]]
    for r in show.itertuples(index=False):
        print("  %-18s %-5s proj=%d obs=%-3d $%-10.2f %4.1f%%  %s"
              % (r.item_family, r.unit, r.n_projects, r.n_bid_observations,
                 r.pooled_median_price, r.pct_of_all_dollars,
                 str(r.example_description)[:46]))

    print("\n=== 外部价格最该优先补的 20 个子目 ===")
    print("   (金额大 x 我们自己证据薄 x 投标人之间分歧大)")
    for r in pri.head(20).itertuples(index=False):
        print("  %-18s %-5s proj=%d obs=%-3d spread=%4.1fx  $%-9.0f %4.1f%%  %s"
              % (r.item_family, r.unit, r.n_projects, r.n_bid_observations,
                 r.median_within_project_spread if pd.notna(r.median_within_project_spread) else 0,
                 r.total_dollars, r.pct_of_all_dollars,
                 str(r.example_description)[:42]))

    print("\n-> data/clean/item_vocabulary.csv")
    print("-> data/clean/item_project_medians.csv")
    print("-> data/clean/external_data_priorities.csv")


if __name__ == "__main__":
    main()
