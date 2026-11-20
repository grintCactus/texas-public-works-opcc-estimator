"""Build official construction cost / price index time series for 2016-2026.

Purpose
-------
External validation anchors for the chained bid-price index computed from the
project's own bid data. Three sources, and the distinction between them is the
whole point:

  CONSTRUCTION COST INDICES (what actual winning bids cost -- includes
  competition, margin and market conditions). These are the ones that can be
  compared against a bid-derived index, and used as a time-adjustment factor:
    * TxDOT Highway Cost Index (HCI)  -- Texas, monthly, bid-based
    * FHWA  National Highway Construction Cost Index (NHCCI) -- US, quarterly,
      bid-based

  INPUT PRICE INDICES (what materials cost -- excludes competition and margin).
  Useful as explanatory variables for WHY costs moved. They must NOT be used
  directly as a time-adjustment factor:
    * BLS PPI "Net inputs to ... construction, goods" family
    * BLS PPI material series (ready-mix, asphalt mixtures, structural metal,
      concrete products, iron and steel)

Note on what does NOT exist
---------------------------
There is no BLS PPI specific to highway or street construction, in either
family. The "Inputs to Industries" breakdown stops at Construction Industries /
New Construction / Residential / Nonresidential / Other Nonresidential /
Maintenance and Repair. Verified against the BLS API and the FRED catalogue on
2026-11-18. The closest available proxy is Net Inputs to Nonresidential
Construction (WPUIP2312001), which contains highway work but is not specific to
it. Neither is a substitute for the HCI or the NHCCI.

Usage
-----
    python src/fetch_cost_indices.py

Re-running re-downloads the TxDOT PDF and the NHCCI extract into
data/external/cost_indices/raw/ and rebuilds the tidy CSVs.
"""
from __future__ import annotations

import csv
import json
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "external" / "cost_indices"
RAW = OUT / "raw"

START_YEAR, END_YEAR = 2016, 2026

TXDOT_HCI_PDF = "https://www.txdot.gov/content/dam/docs/division/cst/202608-hci-report.pdf"
NHCCI_CSV = "https://data.transportation.gov/resource/r94d-n4f9.csv?$limit=50000"
NHCCI_ABOUT = "https://data.transportation.gov/Research-and-Statistics/NHCCI/r94d-n4f9/about_data"
BLS_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# series_id -> (official BLS title, base period, kind)
BLS_SERIES = {
    "WPUIP2312001": ("Net Inputs to Nonresidential Construction, Goods", "Jun 2010=100", "input_price"),
    "WPUIP2300001": ("Net Inputs to Construction Industries, Goods", "Jun 1986=100", "input_price"),
    "WPUIP2310001": ("Net Inputs to New Construction, Goods", "Jun 1986=100", "input_price"),
    "WPUIP2320001": ("Net Inputs to Maintenance and Repair Construction, Goods", "Jun 1986=100", "input_price"),
    "WPUSI012011":  ("Special indexes: Construction materials", "1982=100", "input_price"),
    "PCU327320327320": ("Ready-Mix Concrete Manufacturing", "Jun 1981=100", "input_price"),
    "PCU324121324121": ("Asphalt Paving Mixture and Block Manufacturing", "Jun 1981=100", "input_price"),
    "PCU332312332312": ("Fabricated Structural Metal Manufacturing", "Dec 2003=100", "input_price"),
    "WPU133": ("Nonmetallic Mineral Products: Concrete Products", "1982=100", "input_price"),
    "WPU101": ("Metals and Metal Products: Iron and Steel", "1982=100", "input_price"),
}

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as r, dest.open("wb") as fh:
        fh.write(r.read())
    return dest


# ---------------------------------------------------------------- TxDOT HCI

def parse_txdot_hci(pdf: Path) -> list[dict]:
    """Table 1 of the HCI report: Month / 1-month / 3-month / 12-month averages.

    Only the pages spanned by "Table 1" are read, so the later function and
    earthwork tables (same row shape, different meaning) cannot leak in.
    """
    import fitz

    doc = fitz.open(pdf)
    try:
        pages = [doc[i].get_text() for i in range(doc.page_count)]
    finally:
        doc.close()

    start = next((i for i, t in enumerate(pages) if "Table 1" in t), None)
    if start is None:
        raise SystemExit("HCI: 'Table 1' caption not found - report layout changed")
    end = next((i for i in range(start + 1, len(pages)) if "Table 2" in pages[i]), len(pages))

    row = re.compile(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\s*\n"
        r"\s*([\d,]+\.\d+)\s*\n\s*([\d,]+\.\d+)\s*\n\s*([\d,]+\.\d+)")

    seen: dict[str, dict] = {}
    for pno in range(start, end):
        for mon, yr, one, three, twelve in row.findall(pages[pno]):
            iso = f"{int(yr):04d}-{MONTHS[mon]:02d}-01"
            if iso in seen:          # keep first occurrence
                continue
            seen[iso] = {
                "date": iso,
                "one_month_avg": float(one.replace(",", "")),
                "three_month_avg": float(three.replace(",", "")),
                "twelve_month_avg": float(twelve.replace(",", "")),
                "page": pno,
            }
    rows = [seen[k] for k in sorted(seen)]
    print(f"  TxDOT HCI: {len(rows)} months from pages {start}-{end - 1}"
          f" ({rows[0]['date']} .. {rows[-1]['date']})" if rows else "  TxDOT HCI: no rows")
    return rows


# ---------------------------------------------------------------- FHWA NHCCI

def parse_nhcci(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            q = (r.get("quarter") or "").strip()          # e.g. "2003 Q1"
            m = re.match(r"^(\d{4})\s*Q([1-4])$", q)
            if not m:
                continue
            yr, qq = int(m.group(1)), int(m.group(2))
            out.append({
                "date": f"{yr:04d}-{(qq - 1) * 3 + 1:02d}-01",
                "quarter": q,
                "nhcci": r.get("nhcci") or "",
                "nhcci_seasonally_adjusted": r.get("nhcci_seasonally_adjusted") or "",
            })
    out.sort(key=lambda r: r["date"])
    print(f"  FHWA NHCCI: {len(out)} quarters ({out[0]['quarter']} .. {out[-1]['quarter']})")
    return out


# ---------------------------------------------------------------- BLS PPI

def fetch_bls(series: list[str], y0: int, y1: int) -> dict[str, list[tuple[str, float]]]:
    """Unregistered BLS API v2: max 25 series and 10 years per request."""
    got: dict[str, list[tuple[str, float]]] = {s: [] for s in series}
    for lo in range(y0, y1 + 1, 10):
        hi = min(lo + 9, y1)
        req = urllib.request.Request(
            BLS_API,
            data=json.dumps({"seriesid": series, "startyear": str(lo), "endyear": str(hi)}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = json.load(r)
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise SystemExit(f"BLS API failed for {lo}-{hi}: {payload.get('message')}")
        for s in payload["Results"]["series"]:
            for d in s["data"]:
                if not d["period"].startswith("M") or d["period"] == "M13":
                    continue
                iso = f"{int(d['year']):04d}-{int(d['period'][1:]):02d}-01"
                got[s["seriesID"]].append((iso, float(d["value"])))
    for s in got:
        got[s] = sorted(set(got[s]))
    return got


# ---------------------------------------------------------------- outputs

def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    print("fetching raw sources ...")
    pdf = fetch(TXDOT_HCI_PDF, RAW / "txdot_hci_report_202608.pdf")
    nh = fetch(NHCCI_CSV, RAW / "fhwa_nhcci_raw.csv")

    print("parsing ...")
    hci = parse_txdot_hci(pdf)
    nhcci = parse_nhcci(nh)
    bls = fetch_bls(list(BLS_SERIES), START_YEAR, END_YEAR)

    lo, hi = f"{START_YEAR}-01-01", f"{END_YEAR}-12-31"

    # 1. TxDOT HCI ---------------------------------------------------------
    rows = []
    for r in hci:
        if not (lo <= r["date"] <= hi):
            continue
        for col, label in (("one_month_avg", "TxDOT HCI (2012 base), one-month average"),
                           ("three_month_avg", "TxDOT HCI (2012 base), three-month moving average"),
                           ("twelve_month_avg", "TxDOT HCI (2012 base), twelve-month moving average")):
            rows.append({"date": r["date"], "index_value": r[col], "series_name": label,
                         "series_id": f"TXDOT_HCI_{col}", "index_kind": "construction_cost",
                         "base_period": "2012=100", "frequency": "monthly",
                         "source_url": TXDOT_HCI_PDF})
    write_csv(OUT / "txdot_hci_monthly.csv", rows,
              ["date", "index_value", "series_name", "series_id", "index_kind",
               "base_period", "frequency", "source_url"])
    print(f"  -> txdot_hci_monthly.csv  {len(rows)} rows")

    # 2. FHWA NHCCI --------------------------------------------------------
    rows = []
    for r in nhcci:
        if not (lo <= r["date"] <= hi):
            continue
        for col, label in (("nhcci", "FHWA NHCCI, not seasonally adjusted"),
                           ("nhcci_seasonally_adjusted", "FHWA NHCCI, seasonally adjusted")):
            if r[col] == "":
                continue
            rows.append({"date": r["date"], "index_value": float(r[col]),
                         "series_name": label, "series_id": col.upper(),
                         "index_kind": "construction_cost", "base_period": "2003 Q1=1.0",
                         "frequency": "quarterly", "quarter": r["quarter"],
                         "source_url": NHCCI_ABOUT})
    write_csv(OUT / "fhwa_nhcci_quarterly.csv", rows,
              ["date", "quarter", "index_value", "series_name", "series_id",
               "index_kind", "base_period", "frequency", "source_url"])
    print(f"  -> fhwa_nhcci_quarterly.csv  {len(rows)} rows")

    # 3. BLS PPI -----------------------------------------------------------
    rows = []
    for sid, (title, base, kind) in BLS_SERIES.items():
        for iso, val in bls[sid]:
            rows.append({"date": iso, "index_value": val,
                         "series_name": f"PPI: {title}", "series_id": sid,
                         "index_kind": kind, "base_period": base, "frequency": "monthly",
                         "source_url": f"https://data.bls.gov/timeseries/{sid}"})
    rows.sort(key=lambda r: (r["series_id"], r["date"]))
    write_csv(OUT / "bls_ppi_monthly.csv", rows,
              ["date", "index_value", "series_name", "series_id", "index_kind",
               "base_period", "frequency", "source_url"])
    print(f"  -> bls_ppi_monthly.csv  {len(rows)} rows, {len(BLS_SERIES)} series")

    # provenance -----------------------------------------------------------
    meta = {
        "built_on": date.today().isoformat(),
        "window": f"{START_YEAR}-01-01..{END_YEAR}-12-31",
        "sources": {
            "txdot_hci": {"url": TXDOT_HCI_PDF, "kind": "construction_cost",
                          "frequency": "monthly", "base": "2012=100"},
            "fhwa_nhcci": {"url": NHCCI_ABOUT, "kind": "construction_cost",
                           "frequency": "quarterly", "base": "2003 Q1=1.0"},
            "bls_ppi": {"url": "https://api.bls.gov/publicAPI/v2/", "kind": "input_price",
                        "frequency": "monthly", "series": list(BLS_SERIES)},
        },
        "no_highway_specific_ppi": (
            "Verified 2026-11-18 against the BLS API and the FRED catalogue: BLS "
            "publishes no PPI specific to highway or street construction, as an "
            "output index or an inputs-to-industry index."),
    }
    (OUT / "build_summary.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
