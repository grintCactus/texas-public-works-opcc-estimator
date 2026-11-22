"""
Statewide erosion-protection items, with a measured regional adjustment.

Why statewide: gabion barely appears in DFW highway work. Filtering TxDOT to the
14 DFW-area counties left 114 gabion rows and led to the wrong conclusion that
the data does not exist. Statewide there are ~939 rows across 89 projects,
concentrated in the San Antonio, Austin and Odessa districts -- the work is real,
the county filter was cutting it out.

Why a regional adjustment: a San Antonio price is not a Dallas price. For every
item that appears BOTH in the Dallas/Fort Worth districts and elsewhere, this
measures DFW median / non-DFW median, checks whether that ratio is consistent
across items, and only then uses it to carry statewide evidence into a DFW
estimate. If the ratio is not consistent, the honest output is a wide interval,
not a point.

Two gaps this does NOT fill, and must not be papered over:
  * turf reinforcement mat -- TxDOT's "soil retention blanket" (Item 169) is a
    thin erosion blanket at about $2/SY, not a TRM, which prices an order of
    magnitude higher. Different product.
  * concrete beam / channel grade-control beam -- absent from TxDOT entirely.

Outputs
  data/clean/erosion_statewide_line_items.csv
  data/clean/erosion_regional_factors.csv
  data/clean/erosion_price_reference.csv
  reports/erosion_reference.md
"""
import os
import re
import sys

import numpy as np
import pandas as pd
import vintage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_bidtabs import (norm_unit, norm_desc, item_family, extract_size_in,
                           ENGINEER_RE)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "external", "txdot",
                   "txdot_bidtabs_erosion_statewide.csv")
CLEAN = os.path.join(ROOT, "data", "clean")
REPORTS = os.path.join(ROOT, "reports")
DFW_DISTRICTS = {"Dallas", "Fort Worth"}
L = []


def say(s=""):
    L.append(s)
    print(s)


def md(df, fmt="%.2f"):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


# Specialty groups of creek-erosion work. Deliberately narrower than item_family:
# these are the items the creek-erosion practice lives on.
SPECIALTY = [
    # ---- not-a-placement rules, ahead of EVERY material family below ----
    # Both of these say "this row is not the material it names", so both must
    # win over the material families, and that is the only reason they sit
    # first.  They used to sit after gabion_mattress/gabion_wall, which made
    # `grout_additive` unreachable for the gabion spelling of a grout item:
    # "GROUT FOR GABION MATTRESS" is the grout alone and was being classified
    # gabion_mattress, putting a grout price inside the gabion mattress
    # series.  "grout for riprap" escaped only because the riprap rules happen
    # to come later -- the rule was right and its POSITION was wrong, the same
    # defect, in the same list, as the removal fix recorded below.
    #
    # Measured before moving them: over all 36,219 rows / 201 distinct
    # descriptions in erosion_statewide_line_items.csv this reordering changes
    # the specialty of ZERO rows, so erosion_price_reference.csv does not move.
    # TxDOT has no grout-only pay item in this corpus -- its 284 grout rows all
    # read "RIPRAP STONE COMMON GROUT n IN" or "RIPRAP SPECIAL GROUTED", which
    # are grouted stone riprap, the riprap itself, and stay in riprap_stone.
    # The fix is therefore latent: it closes the hole before a grout-only row
    # arrives, on a file whose comment below records it being missed once
    # already when parse_bidtabs.py was fixed.
    #
    # Anchored on "grout FOR", never on `grout`: a bare \bgrout\w* here would
    # claim all 284 of those grouted-riprap rows -- 2.6% of the arm -- and
    # wreck riprap_stone/CY (n=908 projects, $300.00).
    # Removal stays ahead of grout so "removing grout for riprap" reads as
    # removal, matching parse_bidtabs.py where the demo rules precede
    # grout_additive.
    #
    # "remov\w*", not "(remove|removal)".  The source text says REMOVING, and
    # "remove" is not a substring of "removing" -- so every "REMOVING CONCRETE
    # RIPRAP" row fell past this rule into riprap_conc and was published as a
    # PLACEMENT price.  Measured effect: all 31 rows behind
    # erosion_price_reference's `riprap_conc / SY` median of $13.40 are removals.
    # A 4-inch concrete riprap SY is 0.111 CY, so against the $750/CY placement
    # median the honest SY figure is about $83 -- the published number was 6.2x
    # low, on the client's core family.  The same one-token fix was already
    # applied in parse_bidtabs.py and this file was missed.
    ("riprap_removal",    r"rip\s?rap.{0,30}(remov\w*|dispos\w*)|"
                          r"(remov\w*|dispos\w*)\b.{0,30}rip\s?rap"),
    # Kept in step with FAMILY_RULES in parse_bidtabs.py: "X for Y" is a pay
    # item for X.  Grout that fills an ABANDONED PIPE is utility demolition
    # (parse_bidtabs files it demo_utility), and bedding/filler are not grout
    # (parse_bidtabs files them `other`), so both are named "" here -- no
    # specialty, never priced -- rather than riding the grout rule or falling
    # through to riprap_stone / gabion_*.  Measured on this arm's 36,219 rows
    # and the 82,917-row archive cut: every pattern in these three rules
    # changes the specialty of 0 rows today.
    ("",                  r"\bgrout\w*\b.{0,40}\bab[ao]nd[ao]n\w*|"
                          r"\bab[ao]nd[ao]n\w*\b.{0,60}\bgrout\w*|"
                          r"\bflowable\s+fill\b.{0,40}\bab[ao]nd[ao]n\w*|"
                          r"\b(water|sewer|sanitary|wastewater|storm)\s+"
                          r"(line|main|sewer|drain)\s+grouting\b"),
    ("grout_additive",    r"\bgrout(ing)?\s+for\b|\bmortar\s+for\b|"
                          r"\bgrout\s+seal\w*\b|\bseal\s+grout\b"),
    ("",                  r"\bbedding\s+for\b|\bfiller?\s+for\b|"
                          r"\brip\s?rap\s+bedding\b"),
    # Mattress BEFORE wall: a mattress is a thin horizontal revetment priced by
    # area, a basket wall is a stacked vertical structure priced by volume. They
    # are different structures, so the two series must never be read as
    # cross-validating each other, and converting between SY and CY by a
    # thickness would compare revetment against wall.
    ("gabion_mattress",   r"gabion.{0,30}mattress|mattress.{0,30}gabion|\bmattress\b"),
    ("gabion_wall",       r"\bgabions?\b"),
    # Same split as the municipal side, for the same measured reason: concrete
    # riprap runs ~2.6x stone riprap per CY ($624 vs $242 on the 364 municipal
    # rows), so one "riprap" median is wrong by ~46% for both products.
    # Removal and grout-only pay items are excluded outright -- they are not
    # placements, and both of those rules now sit at the TOP of this list so
    # they beat the material families; see the comment there.  Grouted and dry
    # stone stay pooled (within 16% of each other).
    ("riprap_conc",       r"(rip\s?rap).{0,40}(concrete|conc\b)|"
                          r"(concrete|conc)\b.{0,40}(rip\s?rap)"),
    ("riprap_stone",      r"rip\s?rap"),
    ("soil_rock_nail",    r"soil nail|rock nail|rock anchor"),
    ("retaining_wall",    r"retaining wall|\bmse\b|\bmsw\b"),
    ("rock_filter_dam",   r"rock filter dam"),
    ("soil_ret_blanket",  r"soil retention blanket|erosion control blanket"),
    ("concrete_channel",  r"conc(rete)? (lin|chan)|channel lin"),
]
SPECIALTY = [(n, re.compile(p, re.I)) for n, p in SPECIALTY]


def specialty(desc_norm):
    for n, rx in SPECIALTY:
        if rx.search(desc_norm):
            return n
    return ""


def main():
    if not os.path.exists(SRC):
        print("missing:", SRC)
        return
    d = pd.read_csv(SRC, encoding="utf-8-sig", low_memory=False)
    # Not a bare "engineer" substring: that classifies the real contractor
    # WG ENGINEERING & CONSTRUCTION, LLC as an engineer's estimate and throws
    # its bids away (2,988 rows / 32 lettings in the DFW extract).  ENGINEER_RE
    # agrees exactly with the authoritative bid_rank_sequence_number == "EE".
    d["is_ee"] = (
        d.vendor_name.fillna("").map(lambda s: ENGINEER_RE.search(s) is not None)
        | (d.get("bid_rank_sequence_number", pd.Series("", index=d.index))
           .astype(str).str.upper() == "EE"))
    d["unit_price"] = pd.to_numeric(d.bid_item_unit_price_amount, errors="coerce")
    d["quantity"] = pd.to_numeric(d.bid_item_quantity, errors="coerce")
    d["eng_unit"] = pd.to_numeric(d.engineer_s_estimate_unit, errors="coerce")
    d["bid_date"] = pd.to_datetime(d.project_actual_let_date, errors="coerce").dt.strftime("%Y-%m-%d")
    d["is_low_bidder"] = d.low_bidder_flag.astype(str).str.lower().eq("true")
    desc = d.bid_item_description.fillna("").astype(str)
    d["desc_norm"] = desc.map(norm_desc)
    d["item_family"] = d.desc_norm.map(item_family)
    d["specialty"] = d.desc_norm.map(specialty)
    d["unit"] = d.measurement_unit.fillna("").map(norm_unit)
    d["size_in"] = d.desc_norm.map(extract_size_in)
    d["region"] = np.where(d.district_division.isin(DFW_DISTRICTS), "DFW", "other_TX")

    real = d[(~d.is_ee) & (d.unit_price > 0)].copy()
    nb = real.groupby("project_id")["vendor_name"].nunique().rename("n_bidders")
    real = real.merge(nb, left_on="project_id", right_index=True, how="left")
    real["competition_class"] = np.where(real.n_bidders >= 3, "competitive",
                                         np.where(real.n_bidders == 2, "thin", "sole"))

    keep = real[["project_id", "project_name", "county", "district_division", "region",
                 "bid_date", "bid_code", "bid_item_description", "desc_norm",
                 "item_family", "specialty", "unit", "size_in", "quantity",
                 "vendor_name", "is_low_bidder", "n_bidders", "competition_class",
                 "unit_price", "eng_unit"]]
    keep.to_csv(os.path.join(CLEAN, "erosion_statewide_line_items.csv"),
                index=False, encoding="utf-8-sig")

    say("# Channel erosion specialty unit price reference (TxDOT statewide + regional calibration)")
    say()
    say("Gabion barely appears in DFW highway bids -- filtering to the 14 DFW counties leaves only "
        "114 rows, which leads to the wrong conclusion that 'the data does not exist'. "
        "Statewide there are 939 rows in 89 projects, concentrated in the San Antonio, Austin and "
        "Odessa districts. **The work is real; it was the filter that cut it out.**")
    say()
    say("But a San Antonio price is not a Dallas price, so a regional factor is first measured below "
        "and tested for consistency across items -- if it is not consistent, only a wide range can "
        "be given, not a point value.")
    say()

    say("## 1. Data coverage")
    say()
    g = (real[real.specialty != ""].groupby("specialty")
         .agg(rows=("unit_price", "size"), bidders=("vendor_name", "nunique"),
              projects=("project_id", "nunique"),
              dfw_rows=("region", lambda s: (s == "DFW").sum()),
              year_from=("bid_date", "min"), year_to=("bid_date", "max"))
         .sort_values("rows", ascending=False).reset_index())
    say(md(g, fmt="%.0f"))
    say()
    say("The `dfw_rows` column is the problem: with only the DFW subset, the gabion sample drops "
        "from 939 rows to a few dozen.")
    say()

    comp = real[(real.competition_class == "competitive") & (real.specialty != "")]

    # ---- regional factor, measured --------------------------------------
    # The project QUANTITY is carried alongside the price, because a price
    # without the quantity it was bid at cannot be adjusted for a new quantity.
    # Only price_reference.csv had a qty_median, so every consumer that matched
    # against this table (the erosion specialties are the first source a channel
    # job hits) silently skipped the quantity stage and applied a factor of
    # exactly 1.0 -- a 12 SY spot repair and a 2,600 SY reconstruction were
    # quoted the same unit price, using the quantity the user had been required
    # to enter.
    s1 = (comp.groupby(["specialty", "desc_norm", "unit", "region", "project_id"])
          .agg(p=("unit_price", "median"),
               q=("quantity", "median"),
               # Carried so the vintage can be dollar-weighted at the PROJECT
               # level, the same stage price_reference.csv weights at.  Taking
               # it from the row level instead would weight by bidder count as
               # well, and the two tables would then disagree about what year a
               # price is from -- which is the whole reason src/vintage.py
               # exists.  bid_date is constant inside a project_id group.
               yr=("bid_date", "first")).reset_index())
    byreg = (s1.groupby(["specialty", "desc_norm", "unit", "region"])
             .agg(n_proj=("project_id", "nunique"), med=("p", "median")).reset_index())
    piv = byreg.pivot_table(index=["specialty", "desc_norm", "unit"],
                            columns="region", values="med")
    cnt = byreg.pivot_table(index=["specialty", "desc_norm", "unit"],
                            columns="region", values="n_proj")
    both = piv.dropna()
    if "DFW" in both.columns and "other_TX" in both.columns:
        both = both.join(cnt, rsuffix="_n")
        both = both[(both.get("DFW_n", 0) >= 2) & (both.get("other_TX_n", 0) >= 2)]
        both["dfw_over_other"] = both.DFW / both.other_TX
    else:
        both = pd.DataFrame()

    say("## 2. Regional factor (same item, same unit, DFW districts vs other districts)")
    say()
    if len(both):
        b = both.reset_index()
        # Built from TxDOT's own statewide archive, which contains no
        # company-supplied rows, so the train/test gate never arises here --
        # said plainly rather than borrowing the gated label.
        b["train_scope"] = "txdot_published_only"
        b.to_csv(os.path.join(CLEAN, "erosion_regional_factors.csv"), index=False,
                 encoding="utf-8-sig")
        # Show the best-evidenced items, not the most extreme ratios. Sorting by
        # ratio puts every high value on screen and reads as "DFW is dearer",
        # when those are precisely the rows with 2 DFW projects behind them.
        say("Sorted by DFW sample size (the largest samples are the most reliable), not by ratio:")
        say()
        say(md(b.nlargest(14, "DFW_n")[
            ["specialty", "desc_norm", "unit", "DFW", "other_TX",
             "DFW_n", "other_TX_n", "dfw_over_other"]], fmt="%.2f"))
        say()
        f = b.dfw_over_other
        say(f"**{len(b)}** items can be paired; median DFW/other-district ratio **{f.median():.2f}**, "
            f"interquartile range **{f.quantile(.25):.2f} ~ {f.quantile(.75):.2f}**, "
            f"full range {f.min():.2f} ~ {f.max():.2f}.")
        say()
        hi = b[b.DFW_n >= 10].dfw_over_other
        if len(hi) >= 5:
            say(f"Looking only at the {len(hi)} items with >=10 DFW projects, the median ratio is "
                f"**{hi.median():.2f}** (quartiles {hi.quantile(.25):.2f} ~ "
                f"{hi.quantile(.75):.2f}) -- the larger the sample, the closer the ratio is to 1.")
        say()
        if f.quantile(.75) / max(f.quantile(.25), 1e-9) > 2.0:
            say("**The ratio is very inconsistent across items** (the quartile span exceeds 2x) -- "
                "so there is no single reliable regional factor.")
            say("Conclusion: the statewide sample can only establish the "
                "**order of magnitude and range of prices**; it cannot be converted into a DFW point "
                "value with one factor. When quoting, give a range and prefer the DFW subset (even if "
                "its sample is small) as the central value.")
        else:
            say("The ratio is fairly consistent across items and can be used as a regional adjustment "
                "factor, but still treat it as a multiplier with uncertainty, not an exact conversion.")
    else:
        say("Too few items are observed in both DFW and other districts to measure a regional factor.")
        say("Treat the statewide data only as an order-of-magnitude price reference, "
            "and reflect the regional uncertainty in the quote.")
    say()

    # ---- the reference ---------------------------------------------------
    ref = (s1.groupby(["specialty", "unit"])
           .agg(n_projects=("project_id", "nunique"),
                p25=("p", lambda s: s.quantile(.25)),
                median=("p", "median"),
                p75=("p", lambda s: s.quantile(.75)),
                # The quantity this median was bid at -- the pivot any
                # quantity adjustment has to swing around.  Named to match
                # price_reference.csv so a consumer needs one code path.
                qty_median=("q", "median"),
                qty_p25=("q", lambda s: s.quantile(.25)),
                qty_p75=("q", lambda s: s.quantile(.75))).reset_index())
    # Same vintage rule and same module as price_reference.csv.  Without it a
    # consumer of this table had to derive its own, and three consumers
    # deriving the same thing produce three different answers.
    ref = vintage.attach(ref, s1, ["specialty", "unit"], "yr", None,
                         qty_col="q", price_col="p")
    dfw_only = (s1[s1.region == "DFW"].groupby(["specialty", "unit"])
                .agg(dfw_projects=("project_id", "nunique"),
                     dfw_median=("p", "median")).reset_index())
    ref = ref.merge(dfw_only, on=["specialty", "unit"], how="left")
    ref = ref[ref.n_projects >= 3].sort_values(["specialty", "n_projects"],
                                               ascending=[True, False])
    ref.to_csv(os.path.join(CLEAN, "erosion_price_reference.csv"), index=False,
               encoding="utf-8-sig")
    say("## 3. Specialty unit price reference (competitive bids, >=3 bidders)")
    say()
    say(md(ref, fmt="%.2f"))
    say()
    say("`dfw_median` is the median of the DFW-district sample alone: fewer projects, but regionally "
        "comparable -- the two columns side by side show whether the statewide data runs high or low.")
    say()

    # ---- 2016-2026 series, archive + current -----------------------------
    # The current Socrata extract is a rolling 24 months, so on its own the
    # specialty items have no history. The 2016-2024 archive, filtered by item
    # DESCRIPTION statewide rather than by county, supplies the rest.
    ap = os.path.join(CLEAN, "txdot_archive_erosion_statewide.csv")
    if os.path.exists(ap):
        a = pd.read_csv(ap, encoding="utf-8-sig", low_memory=False)
        a["unit_price"] = pd.to_numeric(a.unit_price, errors="coerce")
        a["n_bidders"] = pd.to_numeric(a.n_bidders, errors="coerce")
        a["specialty"] = a.desc_norm.fillna("").map(specialty)
        a = a[(a.unit_price > 0) & (a.n_bidders >= 3) & (a.specialty != "")]
        a["year"] = a.bid_date.astype(str).str[:4]
        a["project_id"] = a.src_file
        cur = comp.copy()
        cur["year"] = cur.bid_date.astype(str).str[:4]
        cols = ["specialty", "desc_norm", "unit", "year", "project_id",
                "unit_price", "quantity"]
        both = pd.concat([
            a[cols].assign(src="archive"),
            cur[cols].assign(src="current"),
        ], ignore_index=True)
        both["quantity"] = pd.to_numeric(both.quantity, errors="coerce")
        both = both[both.year.str.match(r"^20\d\d$", na=False)]
        s1b = (both.groupby(["specialty", "unit", "year", "project_id"])
               .agg(p=("unit_price", "median"), q=("quantity", "median")).reset_index())

        # Quantity-control the series. A raw annual median conflates price
        # movement with a drift in how large the jobs are, and gabion has both:
        # the within-item elasticity is about -0.12, and later years happen to
        # carry SMALLER quantities (median 266 SY in 2024 -> 86 SY in 2026).
        # Smaller quantity means higher unit price, so part of the apparent
        # "increase" is job size, not escalation. Each observation is rescaled
        # to a common reference quantity before taking the annual median:
        #     p_adj = p * (q_ref / q) ** beta        (beta < 0)
        adj_rows = []
        for (sp, un), sub in s1b.groupby(["specialty", "unit"]):
            s = sub[(sub.p > 0) & (sub.q > 0)].copy()
            if len(s) >= 8 and np.log(s.q).std() > 0.3:
                beta = float(np.polyfit(np.log(s.q), np.log(s.p), 1)[0])
                q_ref = float(s.q.median())
                s["p_adj"] = s.p * (q_ref / s.q) ** beta
            else:
                beta, q_ref = np.nan, float(s.q.median()) if len(s) else np.nan
                s["p_adj"] = s.p
            s["elasticity"] = beta
            s["q_ref"] = q_ref
            adj_rows.append(s)
        s1b = pd.concat(adj_rows, ignore_index=True) if adj_rows else s1b

        ser = (s1b.groupby(["specialty", "unit", "year"])
               .agg(projects=("project_id", "nunique"), median=("p", "median"),
                    median_qty=("q", "median"),
                    median_adj=("p_adj", "median"),
                    elasticity=("elasticity", "first"),
                    q_ref=("q_ref", "first"))
               .reset_index())
        ser = ser[ser.projects >= 3]
        ser.to_csv(os.path.join(CLEAN, "erosion_price_by_year.csv"), index=False,
                   encoding="utf-8-sig")
        say("## 3b. 2016–2026 annual trend (archive + current, spliced)")
        say()
        say("The current Socrata extract is only a rolling 24 months, so the specialty items have no "
            "history of their own; the archive, filtered **statewide by item description** "
            "(not by county), fills in 2016–2024.")
        say()
        for key in [("gabion_mattress", "SY"), ("gabion_wall", "CY"),
                    ("gabion_wall", "SY"), ("riprap_stone", "CY"),
                    ("riprap_stone", "SY"), ("riprap_conc", "CY"),
                    ("riprap_conc", "SY"), ("retaining_wall", "SF")]:
            sub = ser[(ser.specialty == key[0]) & (ser.unit == key[1])].sort_values("year")
            if len(sub) < 3:
                continue
            say(f"**{key[0]} / {key[1]}**")
            say()
            say(md(sub[["year", "projects", "median_qty", "median", "median_adj"]],
                   fmt="%.2f"))
            say()
            first, last = sub.iloc[0], sub.iloc[-1]
            yrs = int(last.year) - int(first.year)
            if yrs > 0 and first["median"] > 0:
                raw = ((last["median"] / first["median"]) ** (1 / yrs) - 1) * 100
                line = (f"{first.year} -> {last.year}: raw median "
                        f"${first['median']:.2f} -> ${last['median']:.2f},"
                        f" annualized {raw:+.1f}%/year")
                if pd.notna(first.get("median_adj")) and first["median_adj"] > 0:
                    adj = ((last["median_adj"] / first["median_adj"]) ** (1 / yrs) - 1) * 100
                    line += (f"; **quantity-adjusted annualized {adj:+.1f}%/year**"
                             f" (elasticity {first.get('elasticity', float('nan')):.3f}, "
                             f"reference quantity {first.get('q_ref', float('nan')):.0f})")
                say(line + ".")
                say()
                if (pd.notna(first.get("median_adj")) and first["median_adj"] > 0
                        and abs(adj - raw) > 1.0):
                    say(f"  The two differ by {abs(adj - raw):.1f} percentage points -- "
                        f"{abs(raw - adj) / max(abs(raw), 1e-9) * 100:.0f}% "
                        f"of the 'increase' in the raw series actually comes from a change in "
                        f"project size, not in price. Use the **adjusted** trend when quoting.")
                    say()
        say("Usage: when quoting, take the value from this series at the target year; that is more "
            "accurate than a single median -- but note that each year has few projects, so the annual "
            "median itself has sampling noise. Prefer the weighted median of the last three years "
            "over the last year's point value.")
        say()

    say("## 4. Gaps that still cannot be filled")
    say()
    say("- **Turf reinforcement mat (TRM)**: TxDOT's `soil retention blanket` (Item 169), median "
        "about $2/SY, is a thin erosion-control blanket, **not a TRM**; the TRM in our own projects, "
        "median about $62/SY, is a completely different product. The blanket is not a substitute.")
    say("- **Concrete beam / channel grade-control beam**: no variant can be found in TxDOT statewide.")
    say()
    say("These two items can only rely on internal projects + the comparable-case method; "
        "a quote must state that the sample sources are extremely few.")
    say()

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "erosion_reference.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n-> data/clean/erosion_statewide_line_items.csv")
    print("-> data/clean/erosion_regional_factors.csv, erosion_price_reference.csv")
    print("-> reports/erosion_reference.md")


if __name__ == "__main__":
    main()
