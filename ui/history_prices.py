"""Recent public pay-item bid medians for worksheet defaults, never the model.

Selection is fixed before verification: compatible specifications and units,
three distinct projects within two calendar years, the preceding year first,
then the existing transparent recency/word/quantity/owner relevance score.
Inflation is supplied separately; unavailable index evidence leaves a blank.
"""

from __future__ import annotations

import calendar
import collections
import csv
import datetime as dt
import math
import re
import statistics

import upstream  # Link the repository's shared item and bidder identity modules.
import bidder_identity
import item_tokens


METHOD = "recent_matching_bid_medians_v1"
PRICE_BASIS = "bid_date_adjusted"
MIN_WORD_MATCH = 0.5
REQUIRED_PROJECTS = 3
NEVER = "9999-12-31"
_TRUE = {"true", "1", "yes"}
_MATERIAL = re.compile(
    r"\b(rcp|pvc|dip|hdpe|ductile\s+iron|c900|cmp|frp|concrete|asphalt|"
    r"steel|stone|rock|timber|wood)\b", re.I)
_LABEL = re.compile(r"\b(class|type|grade|sdr)\s*[-:]?\s*([a-z0-9]+)\b", re.I)


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def years_before(date, years):
    year = max(1, date.year - years)
    return date.replace(year=year, day=min(date.day, calendar.monthrange(year, date.month)[1]))


def _spec(description, material=""):
    text = str(description or "")
    tokens = item_tokens.tokens(text)
    material_text = text + " " + str(material or "")
    material_text = re.sub(r"\bductile\s+iron\b", "dip", material_text, flags=re.I)
    material_text = re.sub(r"\breinforced\s+concrete\s+(?:storm\s+)?(?:sewer\s+)?pipe\b",
                           "rcp", material_text, flags=re.I)
    materials = frozenset(re.sub(r"\s+", " ", match.lower())
                          for match in _MATERIAL.findall(material_text))
    if "rcp" in materials:
        materials -= {"concrete"}  # generic material metadata does not contradict RCP
    if "c900" in materials:
        materials = (materials - {"c900"}) | {"pvc"}
    labels = frozenset((name.lower(), value.lower()) for name, value in _LABEL.findall(text))
    return tokens, item_tokens.numbers(tokens), materials, labels


class HistoryPriceIndex:
    """One actual pay item, one equally weighted quote per distinct bidder.

    ``index_rows`` is the public-only input for HistoryInflation:
    (market, project_key, available_date, date_precision, item_key, year,
    unit_price). It retains older years for measuring the index; only the
    worksheet's candidate projects are subject to the two-year window.
    """

    def __init__(self, rows, availability):
        groups = {}
        item_identities = collections.defaultdict(set)
        self.index_rows = []
        self.by_family_unit = collections.defaultdict(list)
        self.excluded = collections.Counter()
        for row in rows:
            if row.get("source_set") != "external":
                continue
            if row.get("source") not in ("city_bidtab", "txdot", "txdot_archive"):
                continue
            if str(row.get("is_engineer_estimate") or "").lower() in _TRUE:
                continue
            if row.get("competition_class") != "competitive" or row.get("project_kind") != "construction":
                continue
            if row.get("price_basis") == "uneven_proposal":
                continue
            price = _number(row.get("unit_price"))
            if price is None or price <= 0:
                continue
            key = row.get("project_key") or ""
            market = "municipal" if row.get("source") == "city_bidtab" else "txdot"
            available = availability.get(key) or NEVER
            precision = str(row.get("date_precision") or "")
            quantity = _number(row.get("quantity"))
            bidder = bidder_identity.key_of(row.get("bidder_norm") or row.get("bidder") or "")
            unit, family = row.get("unit") or "", row.get("item_family") or ""
            item_no = row.get("item_no") or ""
            description = row.get("desc_norm") or ""
            if not (key and item_no and unit and description and bidder and quantity and quantity > 0):
                continue
            identity = (key, item_no, description, unit)
            item_identities[(key, item_no)].add((description, unit))
            group = groups.get(identity)
            if group is None:
                group = groups[identity] = {
                    "key": key, "item_no": item_no, "item": row.get("description") or description,
                    "description": description, "unit": unit, "market": market,
                    "available_date": available, "bidder_quotes": collections.defaultdict(set),
                    "families": set(), "sizes": set(), "buckets": set(), "materials": set(),
                    "dates": set(), "precisions": set(), "index_keys": set(),
                }
            group["bidder_quotes"][bidder].add((quantity, price))
            group["families"].add(family)
            group["sizes"].add(_number(row.get("size_in")))
            group["buckets"].add(row.get("size_bucket") or "")
            group["materials"].add(str(row.get("material") or "").strip().lower())
            group["dates"].add(row.get("bid_date") or "")
            group["precisions"].add(precision)
            index_key = str(row.get("item_key") or "").strip()
            group["index_keys"].add((index_key + "|unit=" + unit if index_key else "",
                                     row.get("year") or ""))
        for group in groups.values():
            if len(item_identities[(group["key"], group["item_no"])]) != 1:
                self.excluded["ambiguous_item_identity"] += 1
                continue
            quotes = group.pop("bidder_quotes")
            if any(len(values) != 1 for values in quotes.values()):
                self.excluded["conflicting_bidder_quotes"] += 1
                continue
            if len(quotes) < 2:
                self.excluded["fewer_than_two_bidders"] += 1
                continue
            pairs = [next(iter(values)) for values in quotes.values()]
            quantities = {q for q, _ in pairs}
            if len(quantities) != 1 or any(len(group[k]) != 1 for k in
                    ("families", "sizes", "buckets", "materials", "dates", "index_keys")):
                self.excluded["ambiguous_pay_item"] += 1
                continue
            if group["precisions"] != {"precise"}:
                self.excluded["imprecise_date"] += 1
                continue
            date = next(iter(group.pop("dates")))
            if _date(date) is None or _date(group["available_date"]) is None:
                self.excluded["missing_date"] += 1
                continue
            index_key, year = next(iter(group.pop("index_keys")))
            if index_key:
                for _, price in pairs:
                    self.index_rows.append((group["market"], group["key"], group["available_date"],
                                            "precise", index_key, year, price))
            family = next(iter(group.pop("families")))
            group.update(family=family, size_in=next(iter(group.pop("sizes"))),
                         size_bucket=next(iter(group.pop("buckets"))),
                         material=next(iter(group.pop("materials"))), date=date,
                         quantity=next(iter(quantities)), n_bids=len(pairs),
                         median_unit=statistics.median(price for _, price in pairs),
                         min_unit=min(price for _, price in pairs),
                         max_unit=max(price for _, price in pairs), quantity_spread=1.0)
            group.pop("precisions")
            group["spec"] = _spec(group["description"], group["material"])
            self.by_family_unit[(family, group["unit"])].append(group)

    @classmethod
    def from_csv(cls, path, availability):
        with open(path, encoding="utf-8-sig", newline="") as file:
            return cls(csv.DictReader(file), availability)

    def select(self, item, *, bid_date, market, status, pricing_mode="", owner="",
               owner_key=None, adjust=None):
        target_date = _date(bid_date)
        result = {
            "method": METHOD, "price_basis": PRICE_BASIS, "status": "ineligible",
            "unit_price": None, "n_projects": 0, "references": [], "reason": "",
            "evidence": [], "bid_date": bid_date,
            "target_year": target_date.year if target_date else None,
            "market": market,
            "required_projects": REQUIRED_PROJECTS,
        }
        family, unit = item.get("family") or "", item.get("unit") or ""
        quantity = _number(item.get("quantity"))
        if pricing_mode == "engineer":
            result["reason"] = "New item: enter an engineer's price."
            return result
        if status != "priced":
            result["reason"] = "This line needs its owner-set amount, vendor quote, or engineer's price."
            return result
        if family in ("", "other", "needs_review"):
            result["reason"] = "Check the item type before choosing a historical price."
            return result
        if unit.upper() == "LS" or family == "mobilization":
            result["reason"] = "Lump sums and mobilization need an engineer's price for this job."
            return result
        if target_date is None or quantity is None or quantity <= 0 or market not in ("municipal", "txdot"):
            result["reason"] = "Enter a valid bid date and positive quantity."
            return result
        result["status"] = "insufficient"
        earliest, recent = years_before(target_date, 2), years_before(target_date, 1)
        result["window_start"] = earliest.isoformat()
        tokens, numbers, materials, labels = _spec(item.get("description"), item.get("material"))
        size = _number(item.get("size_in"))
        bucket = item.get("size_bucket") or ""
        candidates = []
        for ref in self.by_family_unit.get((family, unit), ()):
            if ref["market"] != market:
                continue
            date = _date(ref["date"])
            if not (earliest <= date < target_date and ref["available_date"] < bid_date):
                continue
            their_tokens, their_numbers, their_materials, their_labels = ref["spec"]
            if (numbers != their_numbers or materials != their_materials or labels != their_labels
                    or size != ref["size_in"] or bucket != ref["size_bucket"]):
                continue
            match = item_tokens.similar(tokens, their_tokens, thresh=MIN_WORD_MATCH)
            if not match:
                continue
            age = (target_date - date).days / 365.25
            ratio = ref["quantity"] / quantity
            same_owner = bool(owner and owner_key and owner_key(ref["key"], ref["market"]) == owner)
            score = (2.0 ** (-age / 3.0)) * (0.2 + 0.8 * match) * math.exp(-0.5 * abs(math.log(ratio)))
            score *= 1.5 if same_owner else 1.0
            candidate = {k: v for k, v in ref.items() if k not in ("spec", "description")}
            candidate.update(word_match=match, score=score, age_years=age,
                             quantity_ratio=ratio, same_owner=same_owner,
                             within_12_months=date >= recent,
                             match_basis=["Same item type and unit; matching stated size, material, and specification tokens.",
                                          "Removal and abandonment identity checked.",
                                          "Same owner is a relevance proxy, not a distance measurement."])
            candidates.append(candidate)
        candidates.sort(key=lambda r: (-int(r["within_12_months"]), -r["score"],
                                        -_date(r["date"]).toordinal(), r["key"],
                                        r["item_no"], r["item"], r["quantity"]))
        selected, seen = [], set()
        for ref in candidates:
            if ref["key"] in seen:
                continue
            selected.append(ref)
            seen.add(ref["key"])
            if len(selected) == REQUIRED_PROJECTS:
                break
        result.update(n_projects=len(selected), references=selected)
        result["evidence"] = [
            "Only public projects bid in the preceding two calendar years; the preceding year ranks first.",
            "Automatic references use the same municipal or TxDOT market as this estimate.",
            "One matching pay item per project; its median gives each distinct bidder one vote.",
            "No measured accuracy percentage is assigned to this worksheet default.",
        ]
        for ref in selected:
            index = (adjust(ref["market"], int(ref["date"][:4]), bid_date) if adjust else {
                "status": "unavailable", "factor": None,
                "reason": "The bid-price index is not available.", "notes": []})
            factor = _number(index.get("factor"))
            available = index.get("status") == "available" and factor is not None and factor > 0
            ref.update(index_evidence=index, escalation=factor if available else None,
                       adjusted_median=ref["median_unit"] * factor if available else None)
        if selected:
            qs = [ref["quantity"] for ref in selected]
            result["quantity_spread"] = max(qs) / min(qs)
        if len(selected) < REQUIRED_PROJECTS:
            result["reason"] = ("Only %d of 3 matching public projects within two years; enter an engineer's price."
                                % len(selected))
            return result
        if any(ref["adjusted_median"] is None for ref in selected):
            result["reason"] = "A selected project's bid-price index is unavailable; enter an engineer's price."
            return result
        result.update(status="available", unit_price=statistics.median(
            ref["adjusted_median"] for ref in selected),
            reason="Median of three recent projects' bidder medians, adjusted with the public bid-price index.")
        result["evidence"].append("Annual matched bid-price adjustment; no assumed annual inflation rate.")
        return result
