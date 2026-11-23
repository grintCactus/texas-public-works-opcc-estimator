"""Read-only time adjustment for worksheet references, independent of P70.

The caller supplies public, non-engineer construction bid rows as
(market, project_key, available_date, date_precision, item_key, year, price).
Only precise dates strictly before the requested bid date participate. Each
project has one median per item/year, then each item has one median per year.
An adjacent year's change is the median of matched items' price ratios.
No data, calibration, parser, or project estimate is changed by this module.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict
from functools import lru_cache


METHOD = "prior_public_matched_bid_index_v1"
MIN_MATCHED_ITEMS = 10


class HistoryInflation:
    def __init__(self, rows):
        projects = defaultdict(list)
        for market, project, available, precision, item, year, value in rows:
            if precision != "precise" or not item or not project:
                continue
            try:
                date = dt.date.fromisoformat(str(available))
                yr = int(year)
                price = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if (isinstance(value, bool) or not math.isfinite(price)
                    or price <= 0 or yr != date.year):
                continue
            projects[(market, str(project), date.isoformat(), str(item), yr)].append(price)
        self.rows = [(*key, statistics.median(prices))
                     for key, prices in projects.items()]

    @lru_cache(maxsize=16)
    def _annual(self, market, before):
        items = defaultdict(list)
        for mk, _project, date, item, year, value in self.rows:
            if mk == market and date < before:
                items[(year, item)].append(value)
        annual = defaultdict(dict)
        for (year, item), values in items.items():
            annual[year][item] = statistics.median(values)
        return dict(annual)

    def adjust(self, market, from_year, target_date):
        out = {"status": "unavailable", "factor": None, "method": METHOD,
               "market": market, "from_year": None, "target_year": None,
               "as_of": str(target_date), "steps": [], "reason": "", "notes": []}
        try:
            target = dt.date.fromisoformat(str(target_date))
            source = int(from_year)
            if isinstance(from_year, bool) or float(from_year) != source:
                raise ValueError("invalid year")
        except (TypeError, ValueError, OverflowError):
            out["reason"] = "The bid date or reference year is missing; enter your unit price."
            return out
        out.update(from_year=source, target_year=target.year)
        if market not in ("municipal", "txdot") or source > target.year:
            out["reason"] = "No matching market/year index is available; enter your unit price."
            return out
        if source == target.year:
            out.update(status="available", factor=1.0,
                       reason="Same bid year: no annual adjustment is applied.")
            out["notes"] = ["This annual method does not estimate price changes within the year."]
            return out
        annual = self._annual(market, target.isoformat())
        factor = 1.0
        for first in range(source, target.year):
            a, b = annual.get(first, {}), annual.get(first + 1, {})
            shared = a.keys() & b.keys()
            ratios = [b[item] / a[item] for item in shared]
            ratios = [x for x in ratios if math.isfinite(x) and x > 0]
            step = {"from_year": first, "to_year": first + 1,
                    "n_matched": len(ratios), "factor": None}
            out["steps"].append(step)
            if len(ratios) < MIN_MATCHED_ITEMS:
                out["reason"] = ("Only %d matched items support the %d to %d price adjustment "
                                 "(%d required); enter your unit price."
                                 % (len(ratios), first, first + 1, MIN_MATCHED_ITEMS))
                return out
            change = statistics.median(ratios)
            step["factor"] = change
            factor *= change
            if not math.isfinite(factor) or factor <= 0:
                out["reason"] = "The price adjustment cannot be calculated; enter your unit price."
                return out
        out.update(status="available", factor=factor,
                   reason="Adjusted using earlier public bids from the same market.")
        out["notes"] = [
            "Index inputs are dated strictly before %s." % target.isoformat(),
            "%d is a partial-year index using only those earlier bids." % target.year,
            "The bid-price index reflects measured increases or decreases, not a guaranteed inflation rate.",
        ]
        return out
