"""One definition of "what year is this price from".

Escalation needs a "from when" for every reference price.  No reference table
carried one, so each consumer inferred it, and they inferred it differently --
three consumers deriving a vintage from the same panel produce three different
vintages, and nothing in the output says which one a number came from.  So the
rule lives here once and every table that publishes a price calls it:

    price_reference.csv            (build_master)
    price_book_txdot.csv           (build_master)
    erosion_price_reference.csv    (load_erosion_statewide)
    item_vocabulary.csv            (build_vocabulary)

DOLLAR-WEIGHTED, not counted.  A median dominated by one large recent letting
genuinely sits nearer that letting's year than a plain mean of years would say,
because that letting is most of what the median is made of.  Counting lettings
equally would pull the vintage toward whichever year happened to have many
small jobs.

The weight is quantity x price, i.e. the line item's own extended amount, not
the project total: the question is which year THIS PRICE is from, so what
matters is where this item's dollars sit, not how big the jobs were.
"""

import numpy as np
import pandas as pd

COL = "vintage_year"


def year_of(s):
    """Year as a number, from a year column or from a yyyy-mm-dd date."""
    v = pd.to_numeric(s, errors="coerce")
    if v.notna().any():
        return v
    return pd.to_numeric(s.astype(str).str.slice(0, 4), errors="coerce")


def dollar_weighted_year(df, key, year_col, dollar_col):
    """Return a frame of `key` + vintage_year, or None if nothing qualifies."""
    d = pd.DataFrame({
        "_yr": year_of(df[year_col]),
        "_dol": pd.to_numeric(df[dollar_col], errors="coerce"),
    })
    for k in key:
        d[k] = df[k].values
    d = d[d._yr.notna() & d._dol.notna() & (d._dol > 0)]
    # A year outside the corpus is a parse failure, not a vintage.  Without
    # this the 2028 dates that the parser used to admit would have pulled a
    # whole group's vintage four years into the future, and a vintage is
    # exactly the kind of number nobody re-reads.
    d = d[(d._yr >= 1990) & (d._yr <= 2100)]
    if not len(d):
        return None
    return (d.groupby(key, observed=True)
            .apply(lambda g: float((g._yr * g._dol).sum() / g._dol.sum()),
                   include_groups=False)
            .rename(COL).reset_index())


def attach(ref, src, key, year_col, dollar_col, qty_col=None, price_col=None):
    """Merge a vintage onto `ref`.  Always leaves the column present.

    When `src` has no dollar column, pass qty_col and price_col and the
    extended amount is formed here -- so a caller never has to decide how to
    weight, which is the whole point of this module.
    """
    s = src
    if dollar_col is None:
        s = src.copy()
        s["_ext"] = (pd.to_numeric(src[qty_col], errors="coerce")
                     * pd.to_numeric(src[price_col], errors="coerce"))
        dollar_col = "_ext"
    vt = dollar_weighted_year(s, key, year_col, dollar_col)
    if vt is None:
        ref = ref.copy()
        ref[COL] = np.nan
        return ref
    out = ref.merge(vt, on=key, how="left")
    if COL not in out.columns:
        out[COL] = np.nan
    return out
