# -*- coding: utf-8 -*-
"""Arithmetic self-consistency audit: where does quantity x price != printed total?

This is the RIGHT test for "did the parser read this row correctly", and it is
worth being explicit about why, because the obvious alternative is worse.

The tempting check is "does this unit price appear anywhere in the source PDF?".
That test is weaker and it misfires.  It was run against Briar Hill (Bid 26020)
and reported two unit prices as fabricated; both were real, printed on page 157
as item 122 "Concrete Channel Grade Control Walls", 36 CY.  A string search
cannot distinguish:

  * a REAL price that was filed under the wrong item (what actually happened --
    item 122's prices carried item 123's gabion description), from
  * a price CONCATENATED out of two different numbers by a mis-sliced cell.

The arithmetic can.  quantity x unit_price == printed extended is a constraint
the document imposes on itself: a spuriously glued number fails it, while a
correctly read price passes even when its description or family label is wrong.
1,417.25 x 36 = 51,021.00 exactly, which settles that row on its own.

So this report ranks by arithmetic failure, and separately counts a second
symptom the arithmetic cannot see: rows that HAVE a printed total but whose
quantity came back empty.  Those are column-boundary failures -- nothing is
checkable, so they pass every gate silently, which is exactly how they
accumulate.
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
REPORTS = os.path.join(ROOT, "reports")

TOL = 0.005          # 0.5%, same tolerance the parser's own gate uses
MIN_CHECKABLE = 8    # below this a rate is noise
OUT = []


def say(s=""):
    OUT.append(s)
    print(s)


def md(df, fmt="%.1f"):
    if df is None or not len(df):
        return "(none)"
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "| " + " | ".join("---" for _ in d.columns) + " |"
    body = ["| " + " | ".join(str(v) for v in r) + " |"
            for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


def main():
    p = os.path.join(CLEAN, "line_items.csv")
    if not os.path.exists(p):
        print("missing line_items.csv")
        return
    use = ["src_file", "src_sheet", "owner", "item_family", "unit", "quantity",
           "unit_price", "extended_reported", "extended_calc", "ext_mismatch",
           "bidder", "desc_norm", "section_role"]
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False,
                    usecols=lambda c: c in use)
    for c in ("quantity", "unit_price", "extended_reported"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")

    d["has_q"] = d.quantity.notna() & (d.quantity != 0)
    d["has_p"] = d.unit_price.notna()
    d["has_e"] = d.extended_reported.notna()
    d["checkable"] = d.has_q & d.has_p & d.has_e
    calc = d.quantity * d.unit_price
    d["arith_ok"] = (d.checkable &
                     ((calc - d.extended_reported).abs()
                      / d.extended_reported.abs().clip(lower=1.0) <= TOL))
    # The silent case: a printed total exists but the quantity does not, so the
    # row cannot be checked at all.
    d["unchecked_no_qty"] = d.has_e & d.has_p & ~d.has_q

    say("# Arithmetic consistency audit")
    say()
    say("The test is `quantity × unit price == printed extension`, **not** 'can this unit price be found in the source document'.")
    say()
    say("The latter is weaker and misjudges: it cannot tell 'real but filed under the wrong family' from 'really stitched together'. "
        "Briar Hill (Bid 26020) was misjudged once by a string search -- two unit prices reported as 'made up out of nothing' "
        "are real: they are printed on page 157, item 122 `Concrete Channel Grade Control Walls`, 36 CY, "
        "and `1,417.25 × 36 = 51,021.00` holds to the cent; this one piece of arithmetic settles it.")
    say()
    say("- Total rows **%d**" % len(d))
    say("- Checkable (quantity, unit price and extension all present) **%d** (%.1f%%)"
        % (d.checkable.sum(), 100.0 * d.checkable.mean()))
    if d.checkable.sum():
        say("- Consistent among the checkable rows **%.2f%%**"
            % (100.0 * d.loc[d.checkable, "arith_ok"].mean()))
    say("- **Extension present but quantity empty** (a column-boundary failure no gate can catch) **%d** (%.1f%%)"
        % (d.unchecked_no_qty.sum(), 100.0 * d.unchecked_no_qty.mean()))
    say()

    say("## 1. Files with the worst consistency rate")
    say()
    g = (d.groupby("src_file")
         .agg(rows=("unit_price", "size"),
              checkable=("checkable", "sum"),
              ok=("arith_ok", "sum"),
              no_qty=("unchecked_no_qty", "sum")).reset_index())
    g["ok_rate"] = 100.0 * g.ok / g.checkable.replace(0, np.nan)
    bad = g[(g.checkable >= MIN_CHECKABLE) & (g.ok_rate < 95)].sort_values("ok_rate")
    bad = bad.assign(src_file=bad.src_file.astype(str).str[-54:])
    say(md(bad.head(25)[["src_file", "rows", "checkable", "ok_rate", "no_qty"]],
           fmt="%.1f") if len(bad) else "No file is below 95%.")
    say()

    say("## 2. Files that cannot be checked at all (the whole quantity column is lost)")
    say()
    say("These files have printed extensions and unit prices, but the quantity column was not extracted. The arithmetic gate has **nothing to say** about them, "
        "so they never show up in the table above -- which is exactly why they could keep piling up.")
    say()
    nq = g[g.no_qty > 0].sort_values("no_qty", ascending=False)
    nq = nq.assign(src_file=nq.src_file.astype(str).str[-54:])
    say(md(nq.head(20)[["src_file", "rows", "checkable", "no_qty"]], fmt="%.0f")
        if len(nq) else "None.")
    say()

    say("## 3. By item family")
    say()
    fg = (d.groupby("item_family")
          .agg(rows=("unit_price", "size"),
               checkable=("checkable", "sum"),
               ok=("arith_ok", "sum"),
               no_qty=("unchecked_no_qty", "sum")).reset_index())
    fg["ok_rate"] = 100.0 * fg.ok / fg.checkable.replace(0, np.nan)
    fg = fg[fg.rows >= 20].sort_values("ok_rate")
    say(md(fg.head(20)[["item_family", "rows", "checkable", "ok_rate", "no_qty"]],
           fmt="%.1f"))
    say()

    say("## 4. Family-by-family check of the client's core families")
    say()
    say("These families have the thinnest samples, so a single bad row matters most here.")
    say()
    CORE = ["gabion_wall", "gabion_mattress", "trm", "rock_anchor", "conc_beam",
            "riprap_conc", "riprap_stone", "retaining_wall", "conc_mow_strip",
            "grout_additive"]
    core = fg[fg.item_family.isin(CORE)] if len(fg) else fg
    core2 = (d[d.item_family.isin(CORE)]
             .groupby(["item_family", "unit"])
             .agg(n=("unit_price", "size"),
                  median_price=("unit_price", "median"),
                  projects=("src_file", "nunique"),
                  arith_ok=("arith_ok", "mean")).reset_index())
    core2 = core2[core2.n >= 5].sort_values(["item_family", "n"], ascending=[True, False])
    core2["arith_ok"] = 100.0 * core2.arith_ok
    say(md(core2, fmt="%.2f") if len(core2) else "(no core-family rows)")
    say()

    d.loc[d.checkable & ~d.arith_ok].to_csv(
        os.path.join(CLEAN, "arith_failures.csv"), index=False, encoding="utf-8-sig")
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "arithmetic_audit.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(OUT) + "\n")
    print("\nwrote reports/arithmetic_audit.md + data/clean/arith_failures.csv")


if __name__ == "__main__":
    main()
