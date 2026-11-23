"""Score issued project forecasts on the covered non-lump-sum work in a public bid tab."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median


QUANTILES = ("p50", "p70", "p80")
SCOPE = "covered_non_ls_v1"
UNCOVERED = {"not_covered", "not_covered_whole"}
STATUSES = UNCOVERED | {"owner_set", "priced", "unpriced"}
UNKNOWN_UNITS = {"", "?", "N/A", "UNKNOWN", "TBD", "UNSPECIFIED"}
LS_UNITS = {"LS", "LSUM", "LSM", "LPSM", "LSUNIT", "LUMP", "LUMPS", "LUMPSUM",
            "LUMPSUMS", "LUMPSUMUNIT", "LUMPSUMPRICE"}
EXPLICIT_LS = re.compile(r"\b(?:pay(?:ment)?\s+unit|unit\s+of\s+(?:payment|measurement))"
                         r"\s*(?:is|=|:)\s*(?:a\s+)?lump[\s-]*sum\b", re.I)


def lump_sum(row):
    explicit_unit = False
    for unit in (row.get("unit_raw"), row.get("unit")):
        outside = str(unit or "").partition("(")[0]
        letters = re.sub(r"[^A-Z]", "", outside.upper()).removeprefix("PER")
        if letters in LS_UNITS:
            return True
        explicit_unit |= outside.strip().upper() not in UNKNOWN_UNITS
    return not explicit_unit and bool(EXPLICIT_LS.search(row.get("description") or ""))


def money(value):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value))


def project_score(lines, headline):
    """Return a scored receipt or an explicit hold. All monetary fields are dollars.

    The caller supplies the complete original schedule, its frozen status flags,
    and extensions from one verified original low bidder aligned by row identity.
    Engineer amounts are optional. Owner-fixed money remains at face value.
    """
    fixed, variable, eligible_fixed, eligible_variable = [], [], [], []
    retained, engineer, seen, reasons = {}, [], set(), set()
    counts = dict(total=0, lump_sum=0, not_covered=0, both=0, excluded=0, eligible=0)
    for row in lines:
        counts["total"] += 1
        row_id = str(row.get("row_id", "")).strip()
        if row.get("row_id") is None or not row_id or row_id in seen:
            reasons.add("missing_or_duplicate_row_id")
        seen.add(row_id)
        ls = lump_sum(row)
        status = row.get("status")
        uncovered = status in UNCOVERED
        eligible = not (ls or uncovered)
        counts["lump_sum"] += ls
        counts["not_covered"] += uncovered
        counts["both"] += ls and uncovered
        counts["excluded"] += not eligible
        counts["eligible"] += eligible
        if status not in STATUSES:
            reasons.add("unknown_model_status")
            continue
        if uncovered:
            continue
        if status == "owner_set" or status == "priced":
            is_fixed = status == "owner_set"
            amount = row.get("fixed_amount" if is_fixed else "market_amount")
            if not money(amount) or amount < 0:
                reasons.add("missing_model_allocation")
            else:
                (fixed if is_fixed else variable).append(amount)
                if eligible:
                    (eligible_fixed if is_fixed else eligible_variable).append(amount)
        elif eligible:
            reasons.add("unpriced_eligible_line")
        if eligible:
            if all(str(row.get(key) or "").strip().upper() in UNKNOWN_UNITS
                   for key in ("unit", "unit_raw")):
                reasons.add("unknown_eligible_unit")
            amount = row.get("winner_amount")
            if not money(amount):
                reasons.add("missing_winner_extension")
            else:
                retained[row_id] = amount
            if money(row.get("engineer_amount")):
                engineer.append(row["engineer_amount"])

    if not counts["eligible"]:
        reasons.add("no_eligible_lines")
    actual = math.fsum(retained.values())
    if counts["eligible"] and len(retained) == counts["eligible"] and actual <= 0:
        reasons.add("nonpositive_actual")
    total_fixed, total_variable = math.fsum(fixed), math.fsum(variable)
    part_fixed, part_variable = math.fsum(eligible_fixed), math.fsum(eligible_variable)
    estimates, missing_quantiles = {}, {}
    for q in QUANTILES:
        issued = headline.get(q)
        if not money(issued) or issued < total_fixed - 0.005:
            missing_quantiles[q] = "unavailable_forecast"
        elif total_variable:
            estimates[q] = part_fixed + (issued - total_fixed) * (part_variable / total_variable)
        elif math.isclose(issued, total_fixed, rel_tol=0, abs_tol=0.005):
            estimates[q] = part_fixed
        else:
            missing_quantiles[q] = "unallocatable_forecast"
    if not estimates:
        reasons.update(missing_quantiles.values())
    if reasons:
        return {"status": "held", "counts": counts, "reasons": sorted(reasons)}
    # Bind paired comparisons to the identical source positions AND winning amounts.
    pairs = sorted((key, float(value).hex()) for key, value in retained.items())
    signature = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()
    return {"status": "scored", "counts": counts, "scope_sha256": signature, "actual": actual,
            "engineer": math.fsum(engineer) if len(engineer) == counts["eligible"] else None,
            "predictions": estimates, "missing_quantiles": missing_quantiles}


def _metrics(pairs):
    errors = [(p - actual) / actual for p, actual in pairs]
    # Handle binary rounding on mathematically equal costs; sub-cent changes
    # otherwise keep their sign and magnitude in the error statistics.
    covered = [error for (p, a), error in zip(pairs, errors)
               if p >= a or math.isclose(p, a, rel_tol=0, abs_tol=1e-8)]
    return {"n": len(errors), "at_or_above": len(covered),
            "not_under_rate": len(covered) / len(errors) if errors else None,
            "median_signed_error": median(errors) if errors else None,
            "median_absolute_error": median(map(abs, errors)) if errors else None,
            "median_overshoot_when_not_under": median(max(v, 0) for v in covered) if covered else None}


def summarize(results):
    results = list(results)
    scored = [r for r in results if r["status"] == "scored"]
    held = Counter(reason for r in results if r["status"] == "held" for reason in r["reasons"])
    engineer = [r for r in scored if r["engineer"] is not None]
    def models(rows):
        return {q: _metrics([(r["predictions"][q], r["actual"]) for r in rows
                             if q in r["predictions"]]) for q in QUANTILES}
    quantiles = models(scored)
    p80 = quantiles["p80"]
    return {"scope": SCOPE, "projects": len(results), "scored": len(scored),
            "held": len(results) - len(scored), "hold_reasons": dict(held),
            "quantiles": quantiles,
            "observed_p80_meets_floor": (5 * p80["at_or_above"] >= 4 * p80["n"]
                                         if p80["n"] else None),
            "engineer": _metrics([(r["engineer"], r["actual"]) for r in engineer]),
            "models_on_engineer_projects": models(engineer)}


def paired(baseline, candidate):
    matched, unmatched, missing_quantiles = {"baseline": [], "candidate": []}, {}, {}
    for key in sorted(baseline.keys() | candidate.keys()):
        a, b = baseline.get(key), candidate.get(key)
        if not a or not b or a["status"] != "scored" or b["status"] != "scored":
            unmatched[key] = "missing_or_held_project"
        elif a["scope_sha256"] != b["scope_sha256"]:
            unmatched[key] = "different_rows_or_winning_amounts"
        else:
            common = a["predictions"].keys() & b["predictions"].keys()
            if not common:
                unmatched[key] = "no_common_forecast"
                continue
            if len(common) != len(QUANTILES):
                missing_quantiles[key] = sorted(set(QUANTILES) - common)
            for arm, receipt in (("baseline", a), ("candidate", b)):
                matched[arm].append(dict(receipt, predictions={
                    q: receipt["predictions"][q] for q in common}))
    return {"matched": len(matched["baseline"]), "unmatched": unmatched,
            "missing_paired_quantiles": missing_quantiles,
            **{arm: summarize(records) for arm, records in matched.items()}}


def read_receipts(path):
    scores = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            project = json.loads(line)
            key = project["project_key"]
            if not isinstance(key, str) or not key.strip() or key in scores:
                raise ValueError("Project keys must be nonempty and unique within an arm")
            scores[key] = project_score(project["lines"], project["headline"])
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path, help="one verified public project per JSON line")
    parser.add_argument("--compare", type=Path, help="candidate receipts to compare on identical rows")
    args = parser.parse_args()
    scores = read_receipts(args.receipts)
    report = {"summary": summarize(scores.values()), "per_project": scores}
    if args.compare:
        report["paired"] = paired(scores, read_receipts(args.compare))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
