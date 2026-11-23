"""Prepare public calibration inputs and build the registered P70 trial sidecar.

Nothing here replaces a production table. ``prepare`` only reconstructs two
missing input tables and compares their full-period discount fit with the
published tables. ``calibrate`` is a separate, explicitly registered forward
run; it refuses to publish a sidecar unless the original calibration repeats.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CLEAN = ROOT / "data" / "clean"
DEFAULT_OUT = ROOT / "ui" / ".local" / "p70-trial"
PUBLIC_TABLES = (
    "estimator_calibration.csv", "qty_shape_borrow_sigma.csv",
    "price_reference_tokens.csv", "price_reference.csv",
    "estimator_t1_txdot.csv", "competition_discount_municipal.csv",
    "price_book_txdot.csv", "project_metadata.csv", "price_reference_band.csv",
    "txdot_archive_project_totals.csv", "price_by_quantity_band_family.csv",
    "competition_discount_pooled.csv", "index_by_market.csv",
    "price_time_index.csv", "master_panel.csv", "item_vocabulary.csv",
    "txdot_project_totals.csv",
)
DERIVED_TABLES = ("txdot_letting_spread.csv", "municipal_letting_totals.csv")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_new(path, content):
    """Keep existing evidence; a changed result needs a fresh output directory."""
    path = Path(path)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    if path.exists() or path.is_symlink():
        if path.is_file() and path.read_bytes() == payload:
            return
        raise RuntimeError(f"Refusing to replace existing output: {path.name}; use --out.")
    with path.open("xb") as f:
        f.write(payload)


def write_csv(path, fields, rows):
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    write_new(path, b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))


def write_report(path, report):
    write_new(path, json.dumps(report, indent=2, sort_keys=True) + "\n")


def modules():
    sys.path.insert(0, str(ROOT / "src"))
    import award_identity
    import bidder_identity
    import estimate_low_bid
    return award_identity, bidder_identity, estimate_low_bid


@contextlib.contextmanager
def clean_directory(engine, path):
    original = engine.CLEAN
    engine.CLEAN = str(path)
    try:
        yield
    finally:
        engine.CLEAN = original


def derive_txdot(out):
    """The spread construction in load_txdot.main, without its other writes."""
    import numpy as np
    import pandas as pd
    totals = pd.read_csv(PUBLIC_CLEAN / "txdot_project_totals.csv",
                         encoding="utf-8-sig")
    is_ee = totals.is_engineer_estimate.astype(str).str.lower().isin(
        ("true", "1", "yes"))
    totals = totals[(~is_ee) & totals.bid_total.notna() & (totals.bid_total > 0)]
    rows = []
    for (project_id, bid_date), group in totals.groupby(["project_id", "bid_date"]):
        bids = group.drop_duplicates("vendor_name").bid_total.values
        if not len(bids):
            continue
        rows.append({
            "project_id": project_id, "bid_date": bid_date,
            "n_bidders": len(bids), "low": bids.min(),
            "median": float(np.median(bids)), "mean": bids.mean(),
            "high": bids.max(),
            "sigma_log": float(np.std(np.log(bids), ddof=1)) if len(bids) > 1 else "",
            "low_over_median": bids.min() / float(np.median(bids)),
        })
    fields = ("project_id", "bid_date", "n_bidders", "low", "median", "mean",
              "high", "sigma_log", "low_over_median")
    write_csv(out / DERIVED_TABLES[0], fields, rows)
    return {"lettings": len(rows)}


def derive_municipal(out, award, bidder):
    """Re-use proven winners and reconstruct the original modal-basket totals.

    The panel already carries the document-completeness and letting-identity
    decisions. Do not re-admit rejected winners or filter this discount input
    by the narrower construction/reference-price population. Owner-declared
    winners were absent from the original totals: that branch returns no modal
    totals. The final discount comparison is mandatory because the panel has
    already passed deduplication and cannot restore any rows it removed.
    """
    groups = collections.defaultdict(list)
    winners = collections.defaultdict(set)
    bases = collections.defaultdict(set)
    meta = {}
    counts = collections.Counter()
    fields = ("item_no", "desc_norm", "unit", "quantity", "unit_price")
    with (PUBLIC_CLEAN / "master_panel.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["source_set"] != "external":
                raise RuntimeError("The input panel is not the public-only bundle.")
            if row["source"] != "city_bidtab":
                continue
            if row["is_engineer_estimate"].lower() in ("true", "1", "yes"):
                continue
            if row["price_basis"] == "uneven_proposal":
                continue
            if row["award_basis"] not in ("full_schedule", "modal_schedule"):
                continue
            key = row["project_key"]
            name = bidder.key_of(row["bidder"])
            if not name:
                continue
            item = {field: row[field] for field in fields}
            item["bidder"] = name
            groups[key].append(item)
            bases[key].add(row["award_basis"])
            meta.setdefault(key, (row["owner"], row["bid_date"]))
            if row["is_low_bidder"].lower() in ("true", "1", "yes"):
                winners[key].add(name)
    rows = []
    for key, items in sorted(groups.items()):
        counts["candidate_lettings"] += 1
        result = award.winner_of_letting(items)
        if (len(winners[key]) != 1 or result["winner"] not in winners[key]
                or bases[key] != {result["basis"]}):
            counts["winner_or_basis_mismatch"] += 1
            continue
        if not award.is_comparable_letting(items):
            counts["noncomparable"] += 1
            continue
        totals = result["totals"]
        values = [v for v in totals.values() if v > 0]
        if result["winner"] not in totals or len(values) < 2:
            counts["invalid_totals"] += 1
            continue
        owner, bid_date = meta[key]
        rows.append({
            "letting_id": key, "src_file": key.split("::", 1)[0],
            "source_set": "external", "owner": owner, "bid_date": bid_date,
            "year": bid_date[:4], "n_bidders": len(values),
            "winner": result["winner"], "low": round(totals[result["winner"]], 2),
            "median": round(statistics.median(values), 2),
            "sigma_log": round(statistics.stdev(math.log(v) for v in values), 6),
            "award_basis": result["basis"], "n_modal_items": result["n_modal_items"],
            "basket": "modal",
        })
    fields = ("letting_id", "src_file", "source_set", "owner", "bid_date", "year",
              "n_bidders", "winner", "low", "median", "sigma_log", "award_basis",
              "n_modal_items", "basket")
    write_csv(out / DERIVED_TABLES[1], fields, rows)
    counts["lettings"] = len(rows)
    return dict(counts)


def compare_discount(expected, actual):
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    counts = collections.Counter()
    largest = collections.defaultdict(float)
    for n in expected.keys() & actual.keys():
        for field in ("f", "lettings", "p25", "p75"):
            a, b = expected[n].get(field), actual[n].get(field)
            if a is None or b is None:
                different = a != b
            else:
                gap = abs(float(a) - float(b))
                largest[field] = max(largest[field], gap)
                different = gap > (0 if field == "lettings" else 1e-12)
            if different:
                counts[field] += 1
    return {"missing_bidder_bins": missing, "extra_bidder_bins": extra,
            "field_mismatches": dict(counts), "max_absolute_difference": dict(largest),
            "identical": not (missing or extra or counts)}


def prepare(out):
    out.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_TABLES:
        source, target = PUBLIC_CLEAN / name, out / name
        if not source.is_file():
            raise RuntimeError(f"Missing public input: {name}")
        if target.exists() or target.is_symlink():
            if not target.is_symlink() or target.resolve() != source.resolve():
                raise RuntimeError(f"Existing output is not the expected public symlink: {name}")
        else:
            target.symlink_to(source)
    award, bidder, engine = modules()
    report = {"kind": "input_reconstruction_only", "created_utc": timestamp(),
              "txdot": derive_txdot(out),
              "municipal": derive_municipal(out, award, bidder)}
    with clean_directory(engine, out):
        original = engine.Tables()
        forward = engine._ForwardDiscount(engine._letting_dates())
        pooled, municipal = forward.at("9999-12-31")
    report["discount_comparison"] = {
        "pooled": compare_discount(original.disc, pooled),
        "municipal": compare_discount(original.disc_muni, municipal),
    }
    report["inputs_sha256"] = {name: sha256(out / name)
                               for name in PUBLIC_TABLES + DERIVED_TABLES}
    report["identical"] = (all(v["identical"] for v in report["discount_comparison"].values())
                           and not report["municipal"].get("winner_or_basis_mismatch"))
    write_report(out / "prepare-report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "inputs_sha256"}, indent=2))
    return 0 if report["identical"] else 2


def baseline_differences(expected, actual):
    """The stored calibration has six decimal places; compare its full schema."""
    differences = collections.Counter()
    by_market = {r["market"]: r for r in actual}
    for row in expected:
        other = by_market.get(row["market"], {})
        for field, wanted in row.items():
            found = other.get(field, "")
            try:
                same = abs(float(wanted) - float(found)) <= 1e-9
            except (ValueError, TypeError):
                same = str(wanted) == str(found)
            if not same:
                differences[field] += 1
    if set(by_market) != {r["market"] for r in expected}:
        differences["market_population"] += 1
    return dict(differences)


def calibrate(out, registration):
    """Run only after registration and a code freeze; never called by prepare."""
    registration = registration.resolve()
    if not registration.is_relative_to(ROOT) or not registration.is_file():
        raise RuntimeError("Supply an existing project registration with --registration.")
    prepared = json.loads((out / "prepare-report.json").read_text())
    if not prepared.get("identical"):
        raise RuntimeError("Input reconstruction did not repeat the published discount tables.")
    for name, digest in prepared["inputs_sha256"].items():
        if sha256(out / name) != digest:
            raise RuntimeError(f"Prepared input changed: {name}")
    code = sorted((ROOT / "src").glob("*.py")) + [Path(__file__).resolve()]
    before = {str(path.relative_to(ROOT)): sha256(path) for path in code}
    _, _, engine = modules()
    records = []
    with clean_directory(engine, out):
        calibration, coverage = engine.calibrate_forward(write=False, per_letting=records)
    if before != {str(path.relative_to(ROOT)): sha256(path) for path in code}:
        raise RuntimeError("Source code changed during calibration; no sidecar was published.")
    with (out / "estimator_calibration.csv").open(encoding="utf-8-sig", newline="") as f:
        existing = list(csv.DictReader(f))
    differences = baseline_differences(existing, calibration)
    report = {"created_utc": timestamp(), "registration": str(registration.relative_to(ROOT)),
              "registration_sha256": sha256(registration), "code_sha256": before,
              "baseline_field_mismatches": differences, "coverage": dict(coverage),
              "forward_records": len(records)}
    write_report(out / "forward-records.json", records)
    write_report(out / "calibration-report.json", report)
    if differences:
        print(json.dumps({"baseline_field_mismatches": differences}, indent=2))
        return 3
    groups = collections.defaultdict(list)
    for record in records:
        trip = (record["bid_part"], record["owner_fixed"], record["actual_low"])
        groups[record["market"]].append(trip)
        groups["all"].append(trip)
    rows = []
    base_hash = sha256(out / "estimator_calibration.csv")
    base_by_market = {row["market"]: row for row in existing}
    for market, triples in sorted(groups.items()):
        # The trial divides the existing published P50; its centre is the
        # published (six-decimal) factor, not a newly fitted replacement.
        centre = float(base_by_market[market]["level_correction"])
        residuals = sorted((bid * centre + fixed) / actual
                           for bid, fixed, actual in triples)
        rows.append({"market": market, "p30": residuals[int(.30 * (len(residuals) - 1))],
                     "n": len(residuals), "base_calibration_sha256": base_hash,
                     "train_scope": "public_external_only"})
    write_csv(out / "estimator_p70_calibration.csv",
              ("market", "p30", "n", "base_calibration_sha256", "train_scope"), rows)
    print(json.dumps({"baseline": "IDENTICAL", "sidecar_rows": rows}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "calibrate"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--registration", type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(ROOT / "ui" / ".local"):
        parser.error("--out must be an isolated directory under ui/.local.")
    if args.command == "calibrate" and args.registration is None:
        parser.error("calibrate requires --registration; it is a forward scoring run.")
    try:
        return prepare(out) if args.command == "prepare" else calibrate(out, args.registration)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
