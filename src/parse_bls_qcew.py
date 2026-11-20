"""Clean BLS QCEW county/metro construction wage data into tidy annual series.

Source
------
Agency   : U.S. Bureau of Labor Statistics (an agency of the U.S. Department of
           Labor), Quarterly Census of Employment and Wages (QCEW).
Endpoint : https://data.bls.gov/cew/data/api/{year}/a/industry/{naics}.csv
           Open, no API key, no account. The `a` path segment selects ANNUAL
           averages (as opposed to a quarter number).
Vintage  : annual averages for 2015-2025, downloaded 2026-11-18.
           2026 is NOT yet published (the endpoint 404s); QCEW annual averages
           require all four quarters, so 2026 will not appear until mid-2027.
           2025 is the most recent year and is the most likely to be revised.
Raw files: data/external/dol/qcew/raw/qcew_{year}_annual_industry_{naics}.csv
           plus area_titles.csv and industry_titles.csv from
           https://data.bls.gov/cew/doc/titles/{area,industry}/...

Why this dataset exists in this project
---------------------------------------
Labour is a large and regionally variable share of construction unit prices, so
the bid-price model needs a labour-cost series for DFW. Two other candidates were
investigated and rejected first; both rejections are recorded here so nobody
repeats them:

  * DOL prevailing-wage disclosure data (ETA-9141 / H-1B / PERM), cleaned by
    src/parse_dol_wages.py, is the WRONG STATUTE. It is an immigration wage floor
    for mostly white-collar roles: DFW construction trades (SOC 47-*) amount to
    only 111 wage observations across 109 cases.
  * Davis-Bacon general wage determinations are the right statute but have NO
    TIME DIMENSION. Their rates are frozen at WHD survey dates: the DFW "Heavy"
    determinations issued in 2026 still carry rates from 1990/1991 surveys, with
    a common labourer at $7.25, the federal minimum wage. Usable as a
    cross-sectional legal floor, useless as a trend.

QCEW is the first of the three that is an actual annual market-wage series at
county grain.

What a wage here IS and IS NOT
------------------------------
`annual_avg_wkly_wage` is total wages paid, divided by average monthly
employment, divided by 52. That means it IS:
  * a wage BILL per covered employee per week, including overtime, bonuses,
    commissions, and the taxable value of stock options;
  * a measure of what employers in this industry actually paid out.
and it IS NOT:
  * an hourly wage rate (QCEW collects no hours, so no hourly rate can be
    derived from it);
  * composition-constant. If contractors shift toward more supervisors or fewer
    labourers, this average rises with no change in any individual's pay. Read
    year-over-year moves as "cost per worker", not "wage rate growth".
  * worksite-based. QCEW reports by ESTABLISHMENT location, so a Dallas-based
    highway contractor crewing a job in Ellis County is counted under Dallas.
    County-level detail is employer location, not where the work happened. This
    is the same class of caveat as the worksite problem in the ETA-9141 data.

Geography
---------
`C1910` is the Dallas-Fort Worth-Arlington, TX MSA, which is EXACTLY the 13
counties this project treats as DFW (Collin, Dallas, Denton, Ellis, Hood, Hunt,
Johnson, Kaufman, Parker, Rockwall, Somervell, Tarrant, Wise). Hood and Somervell
joined the MSA effective 2013, so the composition is stable across the whole
2015-2025 window and the MSA series needs no splicing.

Suppression is the main hazard
------------------------------
QCEW withholds any cell that would disclose an individual employer. A suppressed
row is marked `disclosure_code == "N"` AND its numeric fields are written as
literal ZERO, not blank. Treating those zeros as data would drag every average
toward nothing. This parser therefore:
  * emits an empty string, never 0, for every measure of a suppressed cell;
  * sets `suppressed` to 1 on that row so it can never be silently averaged;
  * writes every suppressed cell to qcew_suppression_report.csv with its reason.

For NAICS 237310 private in 2023, only 5 of the 13 counties publish (Dallas,
Tarrant, Collin, Denton, Johnson). They nonetheless hold 9,019 of the MSA's 9,557
private employees, i.e. 94.4%.

The MSA series alone does not reach the present, which is the whole point of the
exercise, so it has to be bridged:
  * 2015-2023  the MSA aggregate is published and is used as-is.
  * 2024       the MSA aggregate is SUPPRESSED (disclosure_code N: 196
               establishments are reported, the wage is withheld).
  * 2025       there is no MSA aggregation level in the file at all. Only
               national (agglvl 18), state (58) and county (78) are present;
               MSA-level annual data lags the county data by a further cycle.

The bridge is an EMPLOYMENT-WEIGHTED AGGREGATE of whichever DFW counties publish
in that year: sum(employment x weekly wage) / sum(employment). It is not a
substitute for the MSA figure and is never presented as one; every row records
which basis produced it, how many counties went into it, and what share of known
MSA employment they covered. The method is validated on the nine years where the
MSA figure also exists: the weighted aggregate lands within 0.7% of the published
MSA wage in every one of them, biased very slightly high (it over-weights the
dense urban counties, which pay more than the suppressed rural ones).

Read `series_basis` before using a year. Do not mix the two bases in a growth
rate without noting it; the 2023->2024 change in particular straddles them.

Assumptions made here
---------------------
  * Industry scope is the three levels downloaded: 237310 (highway, street and
    bridge construction), 237 (heavy and civil engineering construction) and 23
    (construction, all). 2373 and 237310 carry an identical BLS title; only
    237310 is published in these files.
  * Ownership is kept as a column, never collapsed. For bid-price work the
    relevant series is own_code 5 (private); local government (3) is retained
    because some DFW counties publish it and it is a useful contrast.
  * Area scope is the 13 DFW counties, the DFW MSA, Texas statewide and the
    national total. The national and state rows are kept as benchmarks so a DFW
    move can be judged against them, not because the model needs them.
  * The wage index is rebased to 2016 = 100 because the project's bid tabulations
    begin in 2016. Raw levels are kept alongside so any other base can be used.
  * Year-over-year change is RECOMPUTED here and cross-checked against BLS's own
    `oty_annual_avg_wkly_wage_pct_chg`; disagreements are counted and reported
    rather than silently preferred one way or the other.
  * Nothing is dropped silently. Every excluded or suppressed row is counted and
    appears in the summary and in the suppression report.

Outputs (data/external/dol/qcew/clean/)
---------------------------------------
  qcew_wages_annual.csv          tidy long: one row per area x ownership x
                                 industry x year
  qcew_dfw_highway_wage_index.csv the deliverable series: DFW MSA private 237310
                                 by year, with yoy change and 2016=100 index
  qcew_suppression_report.csv    every suppressed cell, with reason
  parse_summary.json             provenance and counts
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "external" / "dol" / "qcew" / "raw"
OUT = ROOT / "data" / "external" / "dol" / "qcew" / "clean"

YEARS = list(range(2015, 2026))
INDUSTRIES = ["237310", "237", "23"]

INDEX_BASE_YEAR = 2016

# The 13 counties this project treats as DFW; identical to CBSA 19100.
DFW_COUNTIES = {
    "48085": "Collin",
    "48113": "Dallas",
    "48121": "Denton",
    "48139": "Ellis",
    "48221": "Hood",
    "48231": "Hunt",
    "48251": "Johnson",
    "48257": "Kaufman",
    "48367": "Parker",
    "48397": "Rockwall",
    "48425": "Somervell",
    "48439": "Tarrant",
    "48497": "Wise",
}

DFW_MSA = "C1910"
TEXAS = "48000"
NATION = "US000"

KEEP_AREAS = set(DFW_COUNTIES) | {DFW_MSA, TEXAS, NATION}

# QCEW ownership codes. Only 2, 3 and 5 appear in these industry files.
OWNERSHIP = {
    "0": "total_covered",
    "1": "federal_government",
    "2": "state_government",
    "3": "local_government",
    "4": "international_government",
    "5": "private",
    "8": "total_government",
    "9": "total_ui_covered",
}

# Aggregation levels present in the annual industry files.
AGGLVL = {
    "18": "national_by_industry",
    "48": "msa_by_industry",
    "58": "state_by_industry",
    "78": "county_by_industry",
}

# Measures that QCEW zero-fills on a suppressed row. Each must become blank.
MEASURES = [
    "annual_avg_estabs",
    "annual_avg_emplvl",
    "total_annual_wages",
    "taxable_annual_wages",
    "annual_contributions",
    "annual_avg_wkly_wage",
    "avg_annual_pay",
]


def num(v):
    """Tolerant number parse. Returns None rather than raising."""
    if v is None:
        return None
    s = str(v).strip().strip('"')
    if s in ("", "-", "N/A", "NA", "null", "None"):
        return None
    s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def text(v) -> str:
    if v is None:
        return ""
    return " ".join(str(v).split()).strip('"')


def load_titles(path: Path, key: str, val: str) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[text(row.get(key))] = text(row.get(val))
    return out


def area_label(fips: str, titles: dict[str, str]) -> str:
    if fips in DFW_COUNTIES:
        return f"{DFW_COUNTIES[fips]} County, TX"
    return titles.get(fips, fips)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def parse_rows(stats: dict, area_titles: dict, industry_titles: dict) -> list[dict]:
    """Read every raw file and return tidy rows for the areas we care about."""
    rows: list[dict] = []
    for year in YEARS:
        for ind in INDUSTRIES:
            src = RAW / f"qcew_{year}_annual_industry_{ind}.csv"
            if not src.exists():
                stats["missing_raw_files"].append(src.name)
                continue
            stats["raw_files_read"] += 1
            with src.open(newline="", encoding="utf-8-sig") as f:
                for raw in csv.DictReader(f):
                    stats["raw_rows_scanned"] += 1
                    fips = text(raw.get("area_fips"))
                    if fips not in KEEP_AREAS:
                        stats["rows_outside_area_scope"] += 1
                        continue
                    own = text(raw.get("own_code"))
                    disc = text(raw.get("disclosure_code"))
                    suppressed = disc.upper() == "N"

                    row = {
                        "area_fips": fips,
                        "area_title": area_label(fips, area_titles),
                        "area_kind": AGGLVL.get(text(raw.get("agglvl_code")), ""),
                        "is_dfw_county": 1 if fips in DFW_COUNTIES else 0,
                        "dfw_county": DFW_COUNTIES.get(fips, ""),
                        "own_code": own,
                        "ownership": OWNERSHIP.get(own, ""),
                        "industry_code": text(raw.get("industry_code")),
                        "industry_title": industry_titles.get(
                            text(raw.get("industry_code")), ""
                        ),
                        "year": year,
                        "disclosure_code": disc,
                        "suppressed": 1 if suppressed else 0,
                    }

                    # A suppressed cell is zero-filled by BLS. Blank it out so it
                    # can never be mistaken for a real zero.
                    for m in MEASURES:
                        row[m] = "" if suppressed else (num(raw.get(m)) if num(raw.get(m)) is not None else "")

                    row["bls_oty_wkly_wage_pct_chg"] = (
                        "" if suppressed else (num(raw.get("oty_annual_avg_wkly_wage_pct_chg")) or "")
                    )

                    if suppressed:
                        stats["suppressed_cells"] += 1
                        stats["suppressed_by_area"][row["area_title"]] = (
                            stats["suppressed_by_area"].get(row["area_title"], 0) + 1
                        )
                    rows.append(row)
                    stats["rows_kept"] += 1
    return rows


def _isnum(v) -> bool:
    return isinstance(v, (int, float))


def build_index(rows: list[dict], stats: dict) -> list[dict]:
    """One row per year for DFW private NAICS 237310.

    Uses the published MSA aggregate where it exists, and an employment-weighted
    aggregate of the publishable DFW counties where it does not. Every row states
    which basis produced it; see the module docstring.
    """
    msa_by_year = {
        r["year"]: r
        for r in rows
        if r["area_fips"] == DFW_MSA
        and r["own_code"] == "5"
        and r["industry_code"] == "237310"
    }

    recs = []
    for year in YEARS:
        msa = msa_by_year.get(year)
        msa_wage = msa["annual_avg_wkly_wage"] if msa and not msa["suppressed"] else ""
        msa_empl = msa["annual_avg_emplvl"] if msa and not msa["suppressed"] else ""
        if _isnum(msa_wage):
            msa_state = "published"
        elif msa:
            msa_state = "suppressed"
            stats["msa_suppressed_years"].append(year)
        else:
            msa_state = "absent_from_source"
            stats["msa_absent_years"].append(year)

        parts = [
            r
            for r in rows
            if r["is_dfw_county"]
            and r["own_code"] == "5"
            and r["industry_code"] == "237310"
            and r["year"] == year
            and not r["suppressed"]
            and _isnum(r["annual_avg_emplvl"])
            and _isnum(r["annual_avg_wkly_wage"])
            and r["annual_avg_emplvl"] > 0
            and r["annual_avg_wkly_wage"] > 0
        ]
        esum = sum(r["annual_avg_emplvl"] for r in parts)
        wtd = (
            round(
                sum(r["annual_avg_emplvl"] * r["annual_avg_wkly_wage"] for r in parts)
                / esum,
                1,
            )
            if esum
            else ""
        )
        coverage = (
            round(esum / msa_empl * 100.0, 1) if _isnum(msa_empl) and msa_empl else ""
        )
        deviation = (
            round((wtd / msa_wage - 1.0) * 100.0, 2)
            if _isnum(wtd) and _isnum(msa_wage) and msa_wage
            else ""
        )
        if _isnum(deviation):
            stats["bridge_validation"].append({"year": year, "deviation_pct": deviation})

        if _isnum(msa_wage):
            preferred, basis = msa_wage, "msa_published"
        elif _isnum(wtd):
            preferred, basis = wtd, "county_weighted"
            stats["bridge_years"].append(year)
        else:
            preferred, basis = "", "unavailable"
            stats["years_unavailable"].append(year)

        recs.append(
            {
                "year": year,
                "series_basis": basis,
                "wkly_wage": preferred,
                "msa_state": msa_state,
                "msa_wkly_wage": msa_wage,
                "msa_annual_avg_emplvl": msa_empl,
                "msa_annual_avg_estabs": (
                    msa["annual_avg_estabs"] if msa and not msa["suppressed"] else ""
                ),
                "county_weighted_wkly_wage": wtd,
                "counties_published": len(parts),
                "county_names": ";".join(
                    r["dfw_county"]
                    for r in sorted(parts, key=lambda x: -x["annual_avg_emplvl"])
                ),
                "county_emplvl_sum": esum or "",
                "county_coverage_pct_of_msa": coverage,
                "county_vs_msa_deviation_pct": deviation,
            }
        )

    # Growth and index are computed on the preferred series, in year order.
    base = next(
        (r["wkly_wage"] for r in recs if r["year"] == INDEX_BASE_YEAR and _isnum(r["wkly_wage"])),
        None,
    )
    prev_wage = None
    prev_basis = None
    for r in recs:
        w = r["wkly_wage"]
        if _isnum(w) and _isnum(prev_wage) and prev_wage:
            r["wkly_wage_yoy_pct"] = round((w / prev_wage - 1.0) * 100.0, 2)
            r["yoy_crosses_basis_change"] = 1 if prev_basis != r["series_basis"] else 0
        else:
            r["wkly_wage_yoy_pct"] = ""
            r["yoy_crosses_basis_change"] = ""
        r[f"wkly_wage_index_{INDEX_BASE_YEAR}_100"] = (
            round(w / base * 100.0, 2) if _isnum(w) and base else ""
        )
        # Cross-check our recomputed growth against BLS's own figure where the
        # MSA row carries one and both years are on the MSA basis.
        msa_row = msa_by_year.get(r["year"])
        if (
            r["series_basis"] == "msa_published"
            and prev_basis == "msa_published"
            and msa_row
            and _isnum(msa_row.get("bls_oty_wkly_wage_pct_chg"))
            and _isnum(r["wkly_wage_yoy_pct"])
            and abs(r["wkly_wage_yoy_pct"] - msa_row["bls_oty_wkly_wage_pct_chg"]) > 0.15
        ):
            stats["yoy_disagreements"] += 1
            stats["yoy_disagreement_detail"].append(
                {
                    "year": r["year"],
                    "recomputed": r["wkly_wage_yoy_pct"],
                    "bls": msa_row["bls_oty_wkly_wage_pct_chg"],
                }
            )
        if _isnum(w):
            prev_wage, prev_basis = w, r["series_basis"]

    return recs


def build_eci_comparison(index_rows: list[dict], stats: dict) -> list[dict]:
    """Join the DFW series to the national construction ECI, both rebased.

    ECI is composition-controlled (fixed employment weights); QCEW's
    annual_avg_wkly_wage is not. A DFW-minus-national gap is therefore part pay
    growth and part occupational mix, and cannot be decomposed from these two
    series alone. See raw/fred_ECICONWAG_annual.PROVENANCE.txt.
    """
    src = RAW / "fred_ECICONWAG_annual.csv"
    if not src.exists():
        stats["missing_raw_files"].append(src.name)
        return []
    eci = {}
    with src.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            y, v = num(row.get("year")), num(row.get("value"))
            if y is not None and v is not None:
                eci[int(y)] = v

    eci_base = eci.get(INDEX_BASE_YEAR)
    dfw_base = next(
        (r["wkly_wage"] for r in index_rows
         if r["year"] == INDEX_BASE_YEAR and _isnum(r["wkly_wage"])),
        None,
    )
    out = []
    for r in index_rows:
        y = r["year"]
        if y not in eci or not _isnum(r["wkly_wage"]) or not eci_base or not dfw_base:
            continue
        dfw_idx = round(r["wkly_wage"] / dfw_base * 100.0, 2)
        eci_idx = round(eci[y] / eci_base * 100.0, 2)
        out.append(
            {
                "year": y,
                "dfw_wkly_wage": r["wkly_wage"],
                "dfw_series_basis": r["series_basis"],
                f"dfw_index_{INDEX_BASE_YEAR}_100": dfw_idx,
                "national_eci_level": eci[y],
                f"national_eci_index_{INDEX_BASE_YEAR}_100": eci_idx,
                "dfw_minus_national_index_pts": round(dfw_idx - eci_idx, 2),
                "dfw_yoy_pct": r["wkly_wage_yoy_pct"],
            }
        )
    for i, row in enumerate(out):
        prev = out[i - 1]["national_eci_level"] if i else None
        row["national_eci_yoy_pct"] = (
            round((row["national_eci_level"] / prev - 1.0) * 100.0, 2) if prev else ""
        )
    return out


def build_suppression_report(rows: list[dict]) -> list[dict]:
    return [
        {
            "area_fips": r["area_fips"],
            "area_title": r["area_title"],
            "ownership": r["ownership"],
            "industry_code": r["industry_code"],
            "year": r["year"],
            "disclosure_code": r["disclosure_code"],
            "reason": "BLS withholds cells that would disclose an individual employer; "
            "numeric fields arrive as literal 0 and have been blanked",
        }
        for r in rows
        if r["suppressed"]
    ]


def main() -> int:
    if not RAW.exists():
        print(f"raw directory missing: {RAW}", file=sys.stderr)
        return 1

    stats = {
        "raw_files_read": 0,
        "missing_raw_files": [],
        "raw_rows_scanned": 0,
        "rows_outside_area_scope": 0,
        "rows_kept": 0,
        "suppressed_cells": 0,
        "suppressed_by_area": {},
        "yoy_disagreements": 0,
        "yoy_disagreement_detail": [],
        "msa_suppressed_years": [],
        "msa_absent_years": [],
        "bridge_years": [],
        "years_unavailable": [],
        "bridge_validation": [],
    }

    area_titles = load_titles(RAW / "area_titles.csv", "area_fips", "area_title")
    industry_titles = load_titles(
        RAW / "industry_titles.csv", "industry_code", "industry_title"
    )

    print(f"[1/3] reading {len(YEARS)} years x {len(INDUSTRIES)} industries ...", flush=True)
    rows = parse_rows(stats, area_titles, industry_titles)

    print("[2/3] building DFW highway wage index ...", flush=True)
    index_rows = build_index(rows, stats)

    print("[3/3] writing outputs ...", flush=True)
    long_fields = [
        "area_fips", "area_title", "area_kind", "is_dfw_county", "dfw_county",
        "own_code", "ownership", "industry_code", "industry_title", "year",
        "disclosure_code", "suppressed", "annual_avg_estabs", "annual_avg_emplvl",
        "total_annual_wages", "taxable_annual_wages", "annual_contributions",
        "annual_avg_wkly_wage", "avg_annual_pay", "bls_oty_wkly_wage_pct_chg",
    ]
    rows.sort(key=lambda r: (r["industry_code"], r["area_fips"], r["own_code"], r["year"]))
    n_long = write_csv(OUT / "qcew_wages_annual.csv", rows, long_fields)

    idx_fields = list(index_rows[0].keys()) if index_rows else ["year"]
    n_idx = write_csv(OUT / "qcew_dfw_highway_wage_index.csv", index_rows, idx_fields)

    sup = build_suppression_report(rows)
    n_sup = write_csv(
        OUT / "qcew_suppression_report.csv",
        sup,
        ["area_fips", "area_title", "ownership", "industry_code", "year",
         "disclosure_code", "reason"],
    )

    eci_rows = build_eci_comparison(index_rows, stats)
    n_eci = write_csv(
        OUT / "qcew_dfw_vs_national_eci.csv",
        eci_rows,
        list(eci_rows[0].keys()) if eci_rows else ["year"],
    )

    summary = {
        "source": "BLS QCEW annual averages",
        "endpoint": "https://data.bls.gov/cew/data/api/{year}/a/industry/{naics}.csv",
        "downloaded": "2026-11-18",
        "years": [YEARS[0], YEARS[-1]],
        "industries": INDUSTRIES,
        "index_base_year": INDEX_BASE_YEAR,
        "outputs": {
            "qcew_wages_annual.csv": n_long,
            "qcew_dfw_highway_wage_index.csv": n_idx,
            "qcew_suppression_report.csv": n_sup,
            "qcew_dfw_vs_national_eci.csv": n_eci,
        },
        **stats,
    }
    (OUT / "parse_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n=== OUTPUT ROW COUNTS ===")
    for name, n in summary["outputs"].items():
        p = OUT / name
        mb = p.stat().st_size / 1024 / 1024 if p.exists() else 0
        print(f"  {name:36s} {n:>7,d} rows   {mb:>6.2f} MB")

    print("\n=== QCEW ===")
    print(f"  raw files read        : {stats['raw_files_read']} of {len(YEARS)*len(INDUSTRIES)}")
    if stats["missing_raw_files"]:
        print(f"  MISSING raw files     : {stats['missing_raw_files']}")
    print(f"  raw rows scanned      : {stats['raw_rows_scanned']:,}")
    print(f"  outside area scope    : {stats['rows_outside_area_scope']:,} (dropped by design)")
    print(f"  rows kept             : {stats['rows_kept']:,}")
    print(f"  suppressed cells kept : {stats['suppressed_cells']:,} (blanked, flagged, never zeroed)")
    print(f"  yoy vs BLS mismatches : {stats['yoy_disagreements']}")

    if index_rows:
        print("\n=== DFW private highway/street/bridge wages (NAICS 237310) ===")
        print(f"  {'year':5s} {'$/week':>8s} {'yoy%':>6s} {'idx'+str(INDEX_BASE_YEAR):>8s} "
              f"{'basis':<16s} {'cty':>3s} {'cover%':>7s} {'dev%':>6s}")
        for r in index_rows:
            print(f"  {r['year']:<5d} {str(r['wkly_wage']):>8} {str(r['wkly_wage_yoy_pct']):>6} "
                  f"{str(r[f'wkly_wage_index_{INDEX_BASE_YEAR}_100']):>8} "
                  f"{r['series_basis']:<16s} {str(r['counties_published']):>3} "
                  f"{str(r['county_coverage_pct_of_msa']):>7} "
                  f"{str(r['county_vs_msa_deviation_pct']):>6}")
        print(f"\n  MSA published      : {sum(1 for r in index_rows if r['series_basis']=='msa_published')} years")
        print(f"  MSA suppressed     : {stats['msa_suppressed_years'] or 'none'}")
        print(f"  MSA absent         : {stats['msa_absent_years'] or 'none'}")
        print(f"  bridged by counties: {stats['bridge_years'] or 'none'}")
        if stats["years_unavailable"]:
            print(f"  NO DATA AT ALL     : {stats['years_unavailable']}")
        devs = [abs(d["deviation_pct"]) for d in stats["bridge_validation"]]
        if devs:
            print(f"  bridge validation  : {len(devs)} overlap years, "
                  f"max |deviation| {max(devs):.2f}%, mean {sum(devs)/len(devs):.2f}%")

    if eci_rows:
        print("\n=== DFW vs national construction ECI (both 2016=100) ===")
        print(f"  {'year':5s} {'DFW':>7s} {'natl':>7s} {'gap_pts':>8s} "
              f"{'DFW yoy%':>9s} {'natl yoy%':>10s}")
        for r in eci_rows:
            print(f"  {r['year']:<5d} {r[f'dfw_index_{INDEX_BASE_YEAR}_100']:>7} "
                  f"{r[f'national_eci_index_{INDEX_BASE_YEAR}_100']:>7} "
                  f"{r['dfw_minus_national_index_pts']:>8} "
                  f"{str(r['dfw_yoy_pct']):>9} {str(r['national_eci_yoy_pct']):>10}")

    if stats["suppressed_by_area"]:
        print("\n  suppressed cells by area:")
        for a, n in sorted(stats["suppressed_by_area"].items(), key=lambda kv: -kv[1]):
            print(f"    {a:34s} {n}")

    print(f"\nwrote {OUT / 'parse_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
