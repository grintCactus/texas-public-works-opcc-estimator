"""Parse TxDOT "Bid Item Average Cost" Tableau exports into tidy CSVs.

Source dashboard
----------------
https://tableau.txdot.gov/views/BidItemAverageCost/BidItemAvgCostDashboard
Dashboard "Data Date" when scraped: 2026-11-17

Filters used for EVERY raw export in data/txdot/raw/
----------------------------------------------------
  Bidder Rank           = Low Bidder           <- low-bid unit prices only
  Actual Let Date Range = Last 12 months       -> 2025-12-01 .. 2026-11-30
  Standard Spec Code / Bid Item / Project Classification /
  Vendors / Project Size / Project Type = (All)
  Use Qty Filter        = NO

What differs between raw files is the geography ("scope"):
  dfw_counties..............  District=(All), County = the 12 DFW-metro counties
                              that had lettings (Collin, Dallas, Denton, Ellis,
                              Hood, Hunt, Johnson, Kaufman, Parker, Rockwall,
                              Tarrant, Wise). Somervell had no letting in the
                              window, so TxDOT's own filter list omits it.
  dallas....................  District = Dallas
  fortworth.................  District = Fort Worth
  dallas_fortworth_districts  District = Dallas + Fort Worth

Each scope was exported as two Tableau crosstab sheets, in Excel format because
the CSV crosstab rounds unit prices to whole dollars:
  "Bid History Avg Cost"  -> one row per bid item, aggregated over the window
  "Monthly History"       -> bid item x actual letting date matrix of unit price

Outputs (data/txdot/)
---------------------
  txdot_item_avg_lowbid.csv         scope x bid item summary
  txdot_item_lowbid_by_letting.csv  scope x bid item x letting date
  scrape_summary.json               provenance + row counts
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "txdot" / "raw"
OUT = ROOT / "data" / "txdot"

# file prefix -> (scope label, geography description)
SCOPES = {
    "dfw_counties": (
        "DFW_12_counties",
        "County in {Collin, Dallas, Denton, Ellis, Hood, Hunt, Johnson, "
        "Kaufman, Parker, Rockwall, Tarrant, Wise}",
    ),
    "dallas": ("Dallas_district", "TxDOT Dallas district"),
    "fortworth": ("FortWorth_district", "TxDOT Fort Worth district"),
    "dallas_fortworth_districts": (
        "Dallas+FortWorth_districts",
        "TxDOT Dallas district + Fort Worth district",
    ),
}

SUMMARY_COLS = {
    "BID CODE": "bid_code",
    "BID_ITEM_DSCR": "description",
    "MS_UNIT_CD": "unit",
    "Min. Bid Item Qty": "min_qty",
    "Max. Bid Item Qty": "max_qty",
    "Weighted Avg Unit Price": "wavg_unit_price",
    "Min. Unit Price": "min_unit_price",
    "Max. Unit Price": "max_unit_price",
    "Std. dev. of Unit Price": "sd_unit_price",
    "Total Sum Bid Quantities": "total_qty",
    "Distinct CCSJ": "n_projects",
}

TEXT_FIELDS = ("description", "unit")


def rows_of(path: Path) -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        return [tuple(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def num(v):
    """Tableau writes real numbers into the Excel crosstab; stay tolerant."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "")
    if s in ("", "-", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def split_bid_code(code: str) -> tuple[str, str]:
    """'360-6005' -> ('360', '6005'): TxDOT spec item + bid-item number."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", code or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def parse_date(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_summary(path: Path, scope: str) -> list[dict]:
    rows = rows_of(path)
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    missing = [c for c in SUMMARY_COLS if c not in idx]
    if missing:
        raise SystemExit(f"{path.name}: missing columns {missing}")

    out = []
    for r in rows[1:]:
        code = str(r[idx["BID CODE"]] or "").strip()
        if not code:
            continue
        spec, item = split_bid_code(code)
        rec = {"scope": scope, "bid_code": code, "spec_code": spec, "item_no": item}
        for col, name in SUMMARY_COLS.items():
            if name == "bid_code":
                continue
            v = r[idx[col]]
            rec[name] = (str(v).strip() if v is not None else "") if name in TEXT_FIELDS else num(v)
        out.append(rec)
    return out


def parse_monthly(path: Path, scope: str, meta: dict) -> list[dict]:
    """Crosstab of BID CODE (rows) x PROJECT ACTUAL LET DATE (columns)."""
    rows = rows_of(path)
    # TxDOT sometimes holds two separate lettings on the same calendar day, and
    # Tableau then emits two columns with the same header. let_seq keeps them
    # apart (1 = first column for that date, 2 = second, ...).
    dates = []
    seen: dict[str, int] = {}
    for i, v in enumerate(rows[1]):
        if i == 0:
            continue
        iso = parse_date(v)
        if iso:
            seen[iso] = seen.get(iso, 0) + 1
            dates.append((i, iso, seen[iso]))

    out = []
    for r in rows[2:]:
        code = str(r[0] or "").strip()
        if not code:
            continue
        info = meta.get(code, {})
        spec, item = split_bid_code(code)
        for i, iso, seq in dates:
            v = num(r[i]) if i < len(r) else None
            if v is None:
                continue
            out.append(
                {
                    "scope": scope,
                    "bid_code": code,
                    "spec_code": spec,
                    "item_no": item,
                    "description": info.get("description", ""),
                    "unit": info.get("unit", ""),
                    "let_date": iso,
                    "let_seq": seq,
                    "wavg_unit_price": v,
                }
            )
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    summary: list[dict] = []
    letting: list[dict] = []
    per_scope: dict[str, dict] = {}

    for prefix, (scope, geo) in SCOPES.items():
        s_path = RAW / f"{prefix}_bid_item_avg_low_bidder_12mo.xlsx"
        m_path = RAW / f"{prefix}_monthly_history_low_bidder_12mo.xlsx"
        s = parse_summary(s_path, scope)
        meta = {r["bid_code"]: r for r in s}
        m = parse_monthly(m_path, scope, meta)
        summary.extend(s)
        letting.extend(m)
        per_scope[scope] = {
            "geography": geo,
            "items": len(s),
            "letting_rows": len(m),
            "letting_dates": len({r["let_date"] for r in m}),
        }

    summary.sort(key=lambda r: (r["scope"], r["bid_code"]))
    letting.sort(key=lambda r: (r["scope"], r["bid_code"], r["let_date"], r["let_seq"]))

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(
        OUT / "txdot_item_avg_lowbid.csv",
        summary,
        ["scope", "bid_code", "spec_code", "item_no", "description", "unit",
         "min_qty", "max_qty", "wavg_unit_price", "min_unit_price",
         "max_unit_price", "sd_unit_price", "total_qty", "n_projects"],
    )
    write_csv(
        OUT / "txdot_item_lowbid_by_letting.csv",
        letting,
        ["scope", "bid_code", "spec_code", "item_no", "description", "unit",
         "let_date", "let_seq", "wavg_unit_price"],
    )

    lets = sorted({r["let_date"] for r in letting})
    stats = {
        "source": "https://tableau.txdot.gov/views/BidItemAverageCost/BidItemAvgCostDashboard",
        "scraped_on": date.today().isoformat(),
        "dashboard_data_date": "2026-11-17",
        "bidder_rank": "Low Bidder",
        "let_date_window": "2025-12-01..2026-11-30 (dashboard 'Last 12 months')",
        "scopes": per_scope,
        "summary_rows": len(summary),
        "letting_rows": len(letting),
        "letting_dates_overall": lets,
    }
    (OUT / "scrape_summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    for sc, v in per_scope.items():
        print(f"{sc:<28} items={v['items']:>5}  letting_rows={v['letting_rows']:>6}  dates={v['letting_dates']}")
    print(f"\nTOTAL summary_rows={len(summary)}  letting_rows={len(letting)}")
    print(f"letting dates {lets[0]} -> {lets[-1]}  ({len(lets)} distinct)")


if __name__ == "__main__":
    main()
