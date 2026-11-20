"""
Pull the TxDOT / Texas Open Data "Bid Tabulations" dataset (Socrata de7b-7dna)
for the DFW-area counties and write one tidy CSV.

Why this source matters: it is LINE-ITEM level. Every row is one bidder's unit
price for one bid item on one project, and the engineer's estimate is carried on
the same row. That is the shape needed for unit-price modelling -- project totals
alone are not comparable across different scopes.

Rolling window: the dataset only keeps the previous 24 months, so re-run it
periodically to accumulate history.

Usage:
    python src/fetch_txdot_bidtabs.py
    python src/fetch_txdot_bidtabs.py --out data/external/txdot/txdot_bidtabs_dfw.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATASET = "de7b-7dna"
BASE = f"https://data.texas.gov/resource/{DATASET}.json"

# Counties making up the DFW metroplex / its immediate ring.
DFW_COUNTIES = [
    "Dallas", "Tarrant", "Collin", "Denton", "Ellis", "Rockwall",
    "Johnson", "Parker", "Kaufman", "Hunt", "Wise", "Hood",
    "Somervell", "Navarro",
]

# Channel-erosion / scour-repair pay items, pulled STATEWIDE rather than by
# county. Rationale: gabions, rock anchors and bank protection barely appear in
# DFW highway lettings -- that work lives in the Hill Country and South Texas
# districts -- so a DFW-only filter makes these items look unavailable when
# hundreds of observations exist. Unit prices for these items travel better
# across districts than paving does, and each row still carries its county and
# district so a regional adjustment can be applied later.
ITEM_FAMILY_RULES = [
    # Checked in order; the first match wins.
    #
    # Pulling items by keyword is necessary (there is no "channel repair" flag
    # in the data) but it drags in three kinds of row that must NOT price a
    # gabion wall or a bank armouring job:
    #   * temporary construction sediment control (rock filter dams, sandbags,
    #     silt fence) -- regulatory "erosion control", structurally unrelated,
    #     and an order of magnitude cheaper;
    #   * removal/salvage items, which are demolition prices, not install;
    #   * riprap mow strips, a small-quantity landscaping detail along guardrail
    #     and structures. At ~$700/CY these were inflating the riprap median
    #     while having nothing to do with a creek bank.
    # Each row is therefore labelled, and `channel_repair_usable` says whether
    # it belongs in a channel-repair price basket.
    ("removal",           r"\bREMOV|\(REMOVE\)|SALVAG"),
    ("temporary_sw3p",    r"ROCK FILTER DAM|SANDBAG|SILT FENCE|CONSTR EXIT|"
                          r"SEDIMENT|BIODEG|EROSION CONTROL LOG|CURB INLET PROT"),
    ("mow_strip",         r"MOW STRIP"),
    ("gabion",            r"GABION|MATTRESS"),
    ("stone_riprap",      r"STONE PROTECTION|STONE COMMON"),
    ("conc_riprap",       r"RIPRAP"),
    ("retaining_wall",    r"RETAINING WALL"),
    ("rock_anchor",       r"SOIL NAIL|ROCK NAIL"),
    ("flume",             r"FLUME"),
    ("vegetation",        r"\bSOD|SEED|SOIL RETENTION"),
    ("channel_earthwork", r"CHANNEL"),
]
# Families that can price channel-erosion repair work.
STRUCTURAL_FAMILIES = {
    "gabion", "stone_riprap", "conc_riprap", "retaining_wall",
    "rock_anchor", "flume",
}


def item_family(description: str) -> str:
    d = (description or "").upper()
    for name, pat in ITEM_FAMILY_RULES:
        if re.search(pat, d):
            return name
    return "other"


EROSION_ITEM_TERMS = [
    "GABION",            # gabion baskets / walls
    "MATTRESS",          # gabion mattresses
    "RIPRAP",            # concrete / stone / dry riprap
    "ROCK FILTER",       # rock filter dams
    "SOIL RETENTION",    # soil retention blanket (the TRM analogue)
    "RETAINING WALL",
    "SOIL NAIL",         # rock-anchor analogues
    "ROCK NAIL",
    "EROSION",
    "CHANNEL",
    "FLUME",
    "BLOCK SOD",
    "FILTER FABRIC",
]

FIELDS = [
    # project identity
    "project_id", "project_number", "project_name", "short_description",
    "control_section_job_csj", "county", "district_division", "highway",
    "project_type", "project_classification",
    "project_limits_from", "project_limits_to",
    "project_actual_let_date", "project_estimated_let_date",
    "maximum_number_of_working",
    # bidder identity
    "vendor_name", "bid_rank_sequence_number", "bid_submit_sequence_number",
    "low_bidder_flag", "bid_total_amount",
    # the line item itself
    "bid_item_sequence_number", "bid_code", "bid_item_description",
    "measurement_unit", "bid_item_quantity", "bid_item_unit_price_amount",
    # engineer's estimate on the same row
    "engineer_s_estimate_unit", "sealed_engineer_s_estimate",
    "specification_code", "specification_description", "spec_book_year",
]


def soda_get(params: dict, retries: int = 4, timeout: int = 180) -> list[dict]:
    url = BASE + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json",
                              "User-Agent": "bid-tab-research/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"SODA request failed after {retries} tries: {last}\n{url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--page", type=int, default=25000)
    ap.add_argument("--counties", default=",".join(DFW_COUNTIES))
    ap.add_argument(
        "--mode", choices=["dfw", "erosion_items"], default="dfw",
        help="dfw = every bid item in the DFW counties; "
             "erosion_items = channel-erosion pay items STATEWIDE",
    )
    ap.add_argument("--terms", default=",".join(EROSION_ITEM_TERMS),
                    help="item-description keywords for --mode erosion_items")
    args = ap.parse_args()

    if args.mode == "dfw":
        counties = [c.strip() for c in args.counties.split(",") if c.strip()]
        quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in counties)
        where = f"county in ({quoted})"
        default_out = "data/external/txdot/txdot_bidtabs_dfw.csv"
        print(f"mode     : dfw counties ({', '.join(counties)})")
    else:
        terms = [t.strip().upper() for t in args.terms.split(",") if t.strip()]
        clauses = [
            "upper(bid_item_description) like '%{}%'".format(t.replace("'", "''"))
            for t in terms
        ]
        where = "(" + " or ".join(clauses) + ")"
        default_out = "data/external/txdot/txdot_bidtabs_erosion_statewide.csv"
        print(f"mode     : erosion items statewide ({len(terms)} terms)")

    out_path = args.out or default_out
    n_total = int(float(soda_get({"$select": "count(1)", "$where": where})[0]["count_1"]))
    print(f"rows     : {n_total:,}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    offset = 0
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        out_fields = FIELDS + ["item_family", "channel_repair_usable"]
        w = csv.DictWriter(fh, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        while offset < n_total:
            rows = soda_get({
                "$select": ",".join(FIELDS),
                "$where": where,
                "$order": "project_actual_let_date DESC, project_id, "
                          "vendor_name, bid_item_sequence_number",
                "$limit": args.page,
                "$offset": offset,
            })
            if not rows:
                break
            for r in rows:
                fam = item_family(r.get("bid_item_description", ""))
                r["item_family"] = fam
                r["channel_repair_usable"] = fam in STRUCTURAL_FAMILIES
                w.writerow(r)
            written += len(rows)
            offset += args.page
            print(f"  {written:>7,} / {n_total:,}", flush=True)

    print(f"\nwrote {written:,} rows -> {out}")

    # quick profile so the file is immediately interpretable
    import collections
    units = collections.Counter()
    years = collections.Counter()
    vendors = collections.Counter()
    with out.open(encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            units[r["measurement_unit"]] += 1
            d = (r["project_actual_let_date"] or "")[:7]
            if d:
                years[d] += 1
            v = r["vendor_name"]
            if v and "Engineer" not in v:
                vendors[v] += 1
    print("\ntop units      :", units.most_common(12))
    print("let-date range :", min(years) if years else "?", "->",
          max(years) if years else "?")
    print("distinct bidders:", len(vendors))
    print("top bidders    :", [v for v, _ in vendors.most_common(10)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
