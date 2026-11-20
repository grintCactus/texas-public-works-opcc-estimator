"""
BLS QCEW quarterly average weekly wage -- DFW highway construction, 2015Q1-2026Q1.

WHY THIS FILE EXISTS
--------------------
src/parse_bls_qcew.py built the same series on ANNUAL averages. The modelling
session has 25,673 bid rows carrying exact letting dates, and an annual average
cannot be joined to a letting date without smearing a year of wage movement across
it. This file is the quarterly version, built to the same conventions so the two
can be compared line for line.

It deliberately imports its geography and ownership constants from
src/parse_bls_qcew.py rather than restating them, so the two series can never
drift apart on what "DFW" means.

Source   : BLS Quarterly Census of Employment and Wages (QCEW), open CSV access
Endpoint : https://data.bls.gov/cew/data/api/{year}/{1-4}/industry/{naics}.csv
           -- the quarter number replaces the 'a' used for annual averages.
           NOTE this is NOT api.bls.gov. It carries no registration key and no
           25-queries-per-day limit; the limit on the TIME-SERIES API (api.bls.gov,
           used for OEWS/CES in src/parse_bls_oews.py) does not apply here.
Vintage  : 2015Q1 through 2026Q1, downloaded 2026-11-18. 2026Q2 onward 404s and
           will until BLS publishes them (about two quarters in arrears).
Raw files: data/external/dol/qcew/raw/quarterly/qcew_{year}q{q}_industry_237310.csv

INDUSTRY IS FIXED AT 237310 AND NEVER FALLS BACK
------------------------------------------------
237310 = highway, street and bridge construction. If a quarter cannot be built at
237310 -- not from the MSA row, not from the counties -- it is left EMPTY and
flagged. It is never backfilled from 237 (heavy and civil engineering) or 23 (all
construction). A series whose industry definition changes partway through is worse
than a series with holes, because nothing downstream can tell which rows are which.
The annual series holds this property too; it is worth keeping.

WHAT THE WAGE IS
----------------
avg_wkly_wage = total_qtrly_wages / (mean of the three monthly employment counts)
/ 13. A wage BILL per worker for that quarter, not an hourly rate: QCEW collects no
hours. It includes overtime, bonuses and severance.

SEASONALITY -- THE BIG DIFFERENCE FROM THE ANNUAL FILE
------------------------------------------------------
Quarterly QCEW wages are strongly seasonal, and Q1 and Q4 are the worst offenders:
annual bonuses, profit-sharing and exercised stock land in specific quarters, and
in construction so does winter short-time. A quarter-on-quarter change is therefore
mostly seasonal noise and is published here only with a warning column.

Use, in this order of preference:
  1. yoy_pct              -- same quarter one year earlier. Seasonally self-cancelling.
  2. wkly_wage_4q_rolling -- trailing 4-quarter mean. Smooth, comparable to the
                             annual series, but lags turning points by ~2 quarters.
  3. qoq_pct              -- published for completeness. SEASONAL. Not a trend.

SUPPRESSION
-----------
Same trap as the annual files and worse here: a withheld cell has
disclosure_code == 'N' AND its numeric fields written as literal 0. Quarterly cells
are suppressed MORE often than annual ones because the underlying counts are
smaller. Every suppressed measure is blanked, the row is flagged, and all of them
are logged to qcew_suppression_report_quarterly.csv. Nothing is ever read as a zero.

THE COUNTY BRIDGE, AT QUARTERLY GRAIN
-------------------------------------
Where the MSA aggregate C1910 is suppressed or absent for a quarter, the wage is
rebuilt as an employment-weighted mean over the DFW counties that DID publish:

    sum(county_employment * county_avg_wkly_wage) / sum(county_employment)

series_basis records msa_published / county_weighted / unavailable FOR THAT
QUARTER, and county_coverage_pct_of_msa is computed per quarter, not per year.
Where both bases exist in the same quarter the deviation is recorded, which is what
makes the bridge auditable rather than asserted.

OUTPUTS (data/external/dol/qcew/clean/)
---------------------------------------
  qcew_dfw_highway_wage_quarterly.csv   the series, one row per quarter
  qcew_wages_quarterly.csv              long: every kept area x quarter
  qcew_suppression_report_quarterly.csv every suppressed cell
  parse_summary_quarterly.json          counters, bridge validation, disagreements
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_bls_qcew import (  # noqa: E402  -- shared geography, deliberately
    DFW_COUNTIES,
    DFW_MSA,
    KEEP_AREAS,
    NATION,
    OWNERSHIP,
    TEXAS,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "external" / "dol" / "qcew" / "raw" / "quarterly"
OUT = ROOT / "data" / "external" / "dol" / "qcew" / "clean"
ANNUAL_INDEX = OUT / "qcew_dfw_highway_wage_index.csv"
# Built by src/parse_bls_oews.py from the BLS CES time-series API.
CES_MONTHLY = (ROOT / "data" / "external" / "dol" / "oews" / "clean"
               / "ces_hourly_earnings_monthly.csv")

# NAICS 2373 (highway, street and bridge construction) has exactly ONE 6-digit
# child, 237310, so the national CES industry and the DFW QCEW industry are the
# same industry -- no concordance needed. Datatypes: 03 average hourly earnings of
# ALL employees, 07 average weekly hours, 11 average weekly earnings, 01
# employment in thousands.
#
# THE PREFIX IS THE SEASONAL ADJUSTMENT, AND IT IS EASY TO MISS:
#     CES... national, SEASONALLY ADJUSTED      CEU... national, NOT adjusted
#     SMS... state/metro, SEASONALLY ADJUSTED   SMU... state/metro, NOT adjusted
# The series below are CES, i.e. ADJUSTED. That makes them the right choice for a
# trend or a year-on-year comparison and the WRONG choice for anything about
# seasonality: BLS has already removed the seasonality, so their seasonal factors
# come out near 100 by construction. The giveaway when this was caught: national
# weekly HOURS showed factors of 100.00 / 99.95 / 99.94 / 100.11, which no real
# construction hours series does.
# The unadjusted twins (CEU2023730003 etc.) are what a seasonal decomposition
# needs. They are NOT in this build: the api.bls.gov 25-queries-per-day limit was
# exhausted on the day it was attempted. See the README.
CES_2373 = {
    "CES2023730003": "hourly_earnings",
    "CES2023730007": "weekly_hours",
    "CES2023730011": "weekly_earnings",
    "CES2023730001": "employment_thousands",
}
# Seasonal profiles are published over BOTH windows, because the choice moves the
# Q1 factor by half a point and nobody should have to guess which was used. 2015
# exists only to give 2016 a year-ago base, and this project's bid data starts in
# 2016, so 2016_2025 is the better default.
PROFILE_WINDOWS = {"2016_2025": range(2016, 2026), "2015_2025": range(2015, 2026)}

INDUSTRY = "237310"          # fixed; see the module docstring
BASE_PERIOD = (2016, 1)      # index base
PRIVATE = "5"

# Quarter boundaries, for joining to letting dates.
QTR_START = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
QTR_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}

MEASURES = [
    "qtrly_estabs", "month1_emplvl", "month2_emplvl", "month3_emplvl",
    "total_qtrly_wages", "taxable_qtrly_wages", "qtrly_contributions",
    "avg_wkly_wage",
]


def num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N", "NA"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f == int(f) else f


def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def period_label(y, q):
    return f"{y}Q{q}"


def load(stats):
    """Read every raw quarterly file; keep only the areas in scope."""
    rows = []
    files = sorted(RAW.glob(f"qcew_*_industry_{INDUSTRY}.csv"))
    if not files:
        sys.exit(f"no raw files in {RAW}")
    required = {"area_fips", "own_code", "industry_code", "agglvl_code", "year",
                "qtr", "disclosure_code", "avg_wkly_wage", "month1_emplvl",
                "month2_emplvl", "month3_emplvl", "total_qtrly_wages"}
    for path in files:
        with path.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            missing = required - set(rd.fieldnames or [])
            if missing:
                sys.exit(f"{path.name}: missing expected columns {sorted(missing)}")
            for raw in rd:
                stats["raw_rows"] += 1
                if raw["area_fips"] not in KEEP_AREAS:
                    stats["dropped_out_of_scope"] += 1
                    continue
                if raw["industry_code"] != INDUSTRY:
                    stats["dropped_other_industry"] += 1
                    continue
                suppressed = (raw.get("disclosure_code") or "").strip().upper() == "N"
                # SECOND zero-fill trap, distinct from disclosure suppression and
                # not present in the annual files (checked: zero occurrences).
                # For the newest quarters BLS emits a structurally present but
                # UNPOPULATED row: disclosure_code '-' with establishments,
                # employment, wages and avg_wkly_wage all at literal 0. Verified on
                # C1910 in 2025Q3 and 2025Q4. Read literally that is an average
                # weekly wage of $0 on a "published" row, which silently destroyed
                # the 2025 series (-53.7% against the annual file) before this
                # guard existed. An establishment count of 0 with a wage of 0 is
                # not data; it is an empty slot.
                vals = {m: num(raw.get(m)) for m in MEASURES}
                placeholder = not suppressed and all(
                    (vals.get(k) or 0) == 0 for k in (
                        "qtrly_estabs", "month1_emplvl", "month2_emplvl",
                        "month3_emplvl", "total_qtrly_wages", "avg_wkly_wage")
                )
                usable = not (suppressed or placeholder)
                y, q = int(raw["year"]), int(raw["qtr"])
                rec = {
                    "period": period_label(y, q),
                    "year": y,
                    "qtr": q,
                    "period_start": f"{y}-{QTR_START[q]}",
                    "period_end": f"{y}-{QTR_END[q]}",
                    "area_fips": raw["area_fips"],
                    "area_name": DFW_COUNTIES.get(
                        raw["area_fips"],
                        {DFW_MSA: "Dallas-Fort Worth-Arlington MSA",
                         TEXAS: "Texas", NATION: "United States"}.get(
                             raw["area_fips"], raw["area_fips"])),
                    "area_level": (
                        "county" if raw["area_fips"] in DFW_COUNTIES else
                        "msa" if raw["area_fips"] == DFW_MSA else
                        "state" if raw["area_fips"] == TEXAS else "nation"),
                    "dfw_county": DFW_COUNTIES.get(raw["area_fips"], ""),
                    "own_code": raw["own_code"],
                    "ownership": OWNERSHIP.get(raw["own_code"], raw["own_code"]),
                    "industry_code": raw["industry_code"],
                    "agglvl_code": raw["agglvl_code"],
                    "disclosure_code": (raw.get("disclosure_code") or "").strip(),
                    "suppressed": 1 if suppressed else 0,
                    "placeholder_zero": 1 if placeholder else 0,
                    "row_state": ("suppressed" if suppressed else
                                  "empty_placeholder" if placeholder else "published"),
                }
                # A suppressed or placeholder cell is zero-filled by BLS. Blank it
                # so it can never be mistaken for a real zero.
                for m in MEASURES:
                    v = vals[m]
                    # A wage of exactly 0 is impossible for a reporting unit; treat
                    # it as missing even on a row that looks otherwise populated.
                    if m == "avg_wkly_wage" and v == 0:
                        v = None
                    rec[m] = "" if not usable else ("" if v is None else v)
                    if suppressed:
                        stats["suppressed_cells"] += 1
                    elif placeholder:
                        stats["placeholder_cells"] += 1
                # mean monthly employment -> the quarter's employment weight
                emps = [rec[f"month{i}_emplvl"] for i in (1, 2, 3)]
                rec["avg_emplvl"] = (
                    round(sum(emps) / 3.0, 1) if all(isnum(e) for e in emps) else ""
                )
                rec["bls_oty_wkly_wage_pct_chg"] = num(
                    raw.get("oty_avg_wkly_wage_pct_chg")
                ) if usable else None
                rows.append(rec)
    stats["raw_files"] = len(files)
    stats["kept_rows"] = len(rows)
    return rows


def build_series(rows, stats):
    """One row per quarter: MSA where published, county bridge otherwise."""
    priv = [r for r in rows if r["own_code"] == PRIVATE]
    periods = sorted({(r["year"], r["qtr"]) for r in priv})

    by_period = {}
    for r in priv:
        by_period.setdefault((r["year"], r["qtr"]), []).append(r)

    recs = []
    for (y, q) in periods:
        here = by_period[(y, q)]
        msa = next((r for r in here if r["area_fips"] == DFW_MSA), None)
        msa_ok = msa is not None and msa["row_state"] == "published"
        msa_wage = msa["avg_wkly_wage"] if msa_ok else ""
        msa_empl = msa["avg_emplvl"] if msa_ok else ""
        msa_state = "absent" if msa is None else msa["row_state"]

        parts = [
            r for r in here
            if r["area_fips"] in DFW_COUNTIES
            and r["row_state"] == "published"
            and isnum(r["avg_wkly_wage"]) and r["avg_wkly_wage"] > 0
            and isnum(r["avg_emplvl"]) and r["avg_emplvl"] > 0
        ]
        esum = sum(r["avg_emplvl"] for r in parts)
        wtd = (
            round(sum(r["avg_emplvl"] * r["avg_wkly_wage"] for r in parts) / esum, 1)
            if esum else ""
        )
        coverage = (
            round(esum / msa_empl * 100.0, 1) if isnum(msa_empl) and msa_empl else ""
        )
        deviation = (
            round((wtd / msa_wage - 1.0) * 100.0, 2)
            if isnum(wtd) and isnum(msa_wage) and msa_wage else ""
        )
        if isnum(deviation):
            stats["bridge_validation"].append(
                {"period": period_label(y, q), "deviation_pct": deviation}
            )

        if isnum(msa_wage):
            preferred, basis = msa_wage, "msa_published"
        elif isnum(wtd):
            preferred, basis = wtd, "county_weighted"
            stats["bridged_periods"].append(period_label(y, q))
        else:
            # Never substituted from 237 or 23. See the module docstring.
            preferred, basis = "", "unavailable"
            stats["unavailable_periods"].append(period_label(y, q))

        recs.append({
            "period": period_label(y, q),
            "year": y,
            "qtr": q,
            "period_start": f"{y}-{QTR_START[q]}",
            "period_end": f"{y}-{QTR_END[q]}",
            "industry_code": INDUSTRY,
            "series_basis": basis,
            "wkly_wage": preferred,
            "msa_state": msa_state,
            "msa_wkly_wage": msa_wage,
            "msa_avg_emplvl": msa_empl,
            "msa_qtrly_estabs": msa["qtrly_estabs"] if msa_ok else "",
            "county_weighted_wkly_wage": wtd,
            "counties_published": len(parts),
            "county_names": ";".join(
                r["dfw_county"] for r in sorted(parts, key=lambda x: -x["avg_emplvl"])
            ),
            "county_emplvl_sum": esum or "",
            "county_coverage_pct_of_msa": coverage,
            "county_vs_msa_deviation_pct": deviation,
            "_bls_oty": msa["bls_oty_wkly_wage_pct_chg"] if msa else None,
        })

    idx = {r["period"]: i for i, r in enumerate(recs)}
    base = next(
        (r["wkly_wage"] for r in recs
         if (r["year"], r["qtr"]) == BASE_PERIOD and isnum(r["wkly_wage"])), None
    )

    for i, r in enumerate(recs):
        w = r["wkly_wage"]

        # --- year-on-year: SAME QUARTER one year back. The seasonal-safe measure.
        prior = recs[idx[period_label(r["year"] - 1, r["qtr"])]] if period_label(
            r["year"] - 1, r["qtr"]) in idx else None
        if prior and isnum(w) and isnum(prior["wkly_wage"]) and prior["wkly_wage"]:
            r["yoy_pct"] = round((w / prior["wkly_wage"] - 1.0) * 100.0, 2)
            r["yoy_crosses_basis_change"] = (
                1 if prior["series_basis"] != r["series_basis"] else 0
            )
            r["yoy_compared_with"] = prior["period"]
            # county_coverage_pct_of_msa is uncomputable on a bridged quarter --
            # the denominator is exactly the figure BLS withheld. These two columns
            # are the substitute that matters: a bridged yoy is only clean if BOTH
            # quarters were built from the SAME set of counties. When the set moves,
            # part of the change is a change of population, not of pay.
            # Only meaningful when the bridge actually supplied one of the two
            # values. On two msa_published quarters the county set is irrelevant
            # -- the wage came from the MSA row -- so the column is left blank
            # rather than raising a false alarm (the set flickers constantly as
            # small counties cross the disclosure threshold).
            bridged_pair = "county_weighted" in (
                r["series_basis"], prior["series_basis"])
            if bridged_pair:
                r["county_set_same_as_yoy_comparison"] = (
                    1 if set(filter(None, r["county_names"].split(";")))
                    == set(filter(None, prior["county_names"].split(";"))) else 0
                )
                r["county_emplvl_sum_yoy_pct"] = (
                    round((r["county_emplvl_sum"] / prior["county_emplvl_sum"] - 1.0)
                          * 100.0, 2)
                    if isnum(r["county_emplvl_sum"])
                    and isnum(prior["county_emplvl_sum"])
                    and prior["county_emplvl_sum"] else ""
                )
            else:
                r["county_set_same_as_yoy_comparison"] = ""
                r["county_emplvl_sum_yoy_pct"] = ""
        else:
            r["yoy_pct"] = ""
            r["yoy_crosses_basis_change"] = ""
            r["yoy_compared_with"] = ""
            r["county_set_same_as_yoy_comparison"] = ""
            r["county_emplvl_sum_yoy_pct"] = ""

        # --- quarter-on-quarter: published, but seasonal. Flagged as such.
        prev = recs[i - 1] if i else None
        if prev and isnum(w) and isnum(prev["wkly_wage"]) and prev["wkly_wage"]:
            r["qoq_pct_SEASONAL"] = round((w / prev["wkly_wage"] - 1.0) * 100.0, 2)
            r["qoq_crosses_basis_change"] = (
                1 if prev["series_basis"] != r["series_basis"] else 0
            )
        else:
            r["qoq_pct_SEASONAL"] = ""
            r["qoq_crosses_basis_change"] = ""

        # --- trailing 4-quarter mean: only when all four quarters exist.
        window = recs[i - 3:i + 1]
        vals = [x["wkly_wage"] for x in window] if len(window) == 4 else []
        if vals and all(isnum(v) for v in vals):
            r["wkly_wage_4q_rolling"] = round(sum(vals) / 4.0, 1)
            bases = {x["series_basis"] for x in window}
            r["rolling4_mixes_bases"] = 1 if len(bases) > 1 else 0
            r["rolling4_window"] = f'{window[0]["period"]}..{window[3]["period"]}'
        else:
            r["wkly_wage_4q_rolling"] = ""
            r["rolling4_mixes_bases"] = ""
            r["rolling4_window"] = ""

        r[f"wkly_wage_index_{BASE_PERIOD[0]}q{BASE_PERIOD[1]}_100"] = (
            round(w / base * 100.0, 2) if isnum(w) and base else ""
        )

        # --- cross-check our yoy against BLS's own oty figure, MSA basis only.
        if (
            r["series_basis"] == "msa_published"
            and prior is not None
            and prior["series_basis"] == "msa_published"
            and isnum(r["_bls_oty"])
            and isnum(r["yoy_pct"])
            and abs(r["yoy_pct"] - r["_bls_oty"]) > 0.15
        ):
            stats["yoy_disagreements"] += 1
            stats["yoy_disagreement_detail"].append(
                {"period": r["period"], "recomputed": r["yoy_pct"], "bls": r["_bls_oty"]}
            )

    for r in recs:
        r.pop("_bls_oty", None)
    return recs


def build_suppression_report(rows):
    out = []
    for r in rows:
        if r["row_state"] == "published":
            continue
        reason = r["row_state"]
        note = ("withheld by BLS for disclosure; raw file contains a literal 0 here"
                if reason == "suppressed" else
                "row present but unpopulated (disclosure_code '-', all counts and "
                "wages literal 0); not yet published, NOT a disclosure suppression")
        for m in MEASURES:
            out.append({
                "reason": reason,
                "note_detail": note,
                "period": r["period"],
                "area_fips": r["area_fips"],
                "area_name": r["area_name"],
                "area_level": r["area_level"],
                "ownership": r["ownership"],
                "industry_code": r["industry_code"],
                "measure": m,
                "disclosure_code": r["disclosure_code"],
            })
    return out


def compare_with_annual(series, stats):
    """Sanity bridge to the annual file: mean of a year's four quarters should sit
    close to the published annual average. Not an identity -- the annual figure is
    a 12-month employment-weighted construction, not a mean of four quarters."""
    if not ANNUAL_INDEX.exists():
        stats["annual_comparison"] = "annual index not found"
        return []
    ann = {}
    with ANNUAL_INDEX.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            v = num(r.get("wkly_wage"))
            if v is not None:
                ann[int(r["year"])] = (v, r.get("series_basis", ""))
    by_year = {}
    for r in series:
        by_year.setdefault(r["year"], []).append(r)
    out = []
    for y in sorted(by_year):
        qs = by_year[y]
        vals = [q["wkly_wage"] for q in qs if isnum(q["wkly_wage"])]
        if y not in ann or len(vals) != 4:
            continue
        qmean = sum(vals) / 4.0
        a, abasis = ann[y]
        out.append({
            "year": y,
            "quarters_present": len(vals),
            "quarterly_mean_wkly_wage": round(qmean, 1),
            "annual_file_wkly_wage": a,
            "annual_series_basis": abasis,
            "quarterly_bases": ";".join(sorted({q["series_basis"] for q in qs})),
            "diff_pct": round((qmean / a - 1.0) * 100.0, 2),
        })
    if out:
        devs = [abs(r["diff_pct"]) for r in out]
        stats["annual_comparison"] = {
            "years": len(out),
            "max_abs_diff_pct": round(max(devs), 2),
            "mean_abs_diff_pct": round(sum(devs) / len(devs), 2),
        }
    return out


def build_national_quarterly(stats):
    """CES national NAICS 2373, monthly -> calendar quarters.

    A quarter is the mean of its three months and is blank unless all three are
    present; months_in_quarter records it, so a partial quarter can never be
    mistaken for a full one.

    WHAT THIS SERIES IS, AND WHY IT IS THE RIGHT FOIL FOR THE DFW ONE
    -----------------------------------------------------------------
    CES average hourly earnings is payroll divided by HOURS PAID, and it excludes
    bonuses that are not earned and paid regularly each pay period. QCEW's
    avg_wkly_wage divides total wages by headcount with no hours at all, and it
    INCLUDES lump-sum bonuses, profit-sharing and severance.

    The two series therefore differ in exactly the two things at issue: hours and
    irregular pay. The national weekly-earnings seasonal profile shows what HOURS
    seasonality alone does to weekly pay; the DFW profile shows hours plus bonus
    timing plus occupational mix. The difference between the profiles estimates the
    bonus-and-mix effect at quarterly grain.

    It is an estimate, not an identity: the gap also absorbs any genuine
    DFW-versus-national difference in seasonality. Said again in the output.
    """
    if not CES_MONTHLY.exists():
        stats["national_quarterly"] = "ces monthly file missing; run parse_bls_oews"
        return [], {}
    monthly = {}
    with CES_MONTHLY.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["series_id"] not in CES_2373:
                continue
            v = num(r["value"])
            if v is None:
                continue
            q = (int(r["month"]) - 1) // 3 + 1
            key = (CES_2373[r["series_id"]], int(r["year"]), q)
            monthly.setdefault(key, []).append(v)

    periods = sorted({(y, q) for (_, y, q) in monthly})
    recs = []
    for (y, q) in periods:
        rec = {"period": period_label(y, q), "year": y, "qtr": q,
               "period_start": f"{y}-{QTR_START[q]}",
               "period_end": f"{y}-{QTR_END[q]}",
               "industry": "NAICS 2373 (national)",
               "seasonal_adjustment": "seasonally adjusted (CES prefix)"}
        nmon = len(monthly.get(("hourly_earnings", y, q), []))
        rec["months_in_quarter"] = nmon
        rec["partial_quarter"] = 1 if 0 < nmon < 3 else 0
        for measure in CES_2373.values():
            vals = monthly.get((measure, y, q), [])
            rec[measure] = round(sum(vals) / len(vals), 3) if len(vals) == 3 else ""
        recs.append(rec)

    by = {r["period"]: r for r in recs}
    base = next((r["hourly_earnings"] for r in recs
                 if (r["year"], r["qtr"]) == BASE_PERIOD
                 and isnum(r["hourly_earnings"])), None)
    for r in recs:
        prior = by.get(period_label(r["year"] - 1, r["qtr"]))
        for measure, col in (("hourly_earnings", "hourly_earnings_yoy_pct"),
                             ("weekly_earnings", "weekly_earnings_yoy_pct")):
            cur = r[measure]
            prev = prior[measure] if prior else ""
            r[col] = (round((cur / prev - 1.0) * 100.0, 2)
                      if isnum(cur) and isnum(prev) and prev else "")
        r["hourly_earnings_index_2016q1_100"] = (
            round(r["hourly_earnings"] / base * 100.0, 2)
            if isnum(r["hourly_earnings"]) and base else "")
    stats["national_quarterly_periods"] = len(recs)
    return recs, by


def seasonal_profile(values_by_year, window):
    """Mean of (quarter value / that year's four-quarter mean), per quarter.

    Only COMPLETE years inside the window contribute, so a partial year cannot
    tilt a factor. Returns the four factors as percentages, plus the year count.
    """
    acc = {q: [] for q in (1, 2, 3, 4)}
    years_used = []
    for y in window:
        qs = values_by_year.get(y, {})
        if len(qs) != 4 or not all(isnum(v) for v in qs.values()):
            continue
        mean = sum(qs.values()) / 4.0
        if not mean:
            continue
        years_used.append(y)
        for q, v in qs.items():
            acc[q].append(v / mean)
    if not years_used:
        return None, 0
    return ({q: round(sum(a) / len(a) * 100.0, 2) for q, a in acc.items()},
            len(years_used))


def build_seasonality_comparison(series, national, rows, stats):
    """DFW seasonal profile against TWO national comparators, only one of which
    may be differenced against it.

    QCEW national private 237310 (us_qcew_* columns) -- VALID. Same programme,
    same measure (average weekly wage = the wage bill per worker), same industry,
    and no seasonal adjustment on either side. DFW minus this is a real statement
    about DFW, and it is the column to use.

    CES national NAICS 2373 (us_ces_* columns) -- DIAGNOSTIC ONLY. These are
    CES-prefixed, i.e. seasonally ADJUSTED, so their factors sit near 100 by
    construction and differencing them against DFW would return, almost exactly,
    the whole of DFW's own seasonality dressed up as a decomposition. They are
    published because seeing the factors pinned at 100 is the evidence for why
    that route is blocked, because weekly HOURS exists only here (QCEW records no
    hours at all), and because a future run with the unadjusted CEU twins can
    slot straight in where they sit.

    WHAT IS STILL NOT HERE. Splitting the Q4 bulge into an hours part and a bonus
    part needs unadjusted national HOURS, which only CES publishes and only the
    CEU prefix leaves unadjusted. QCEW has no hours, so it can say whether DFW's
    Q4 bulge differs from the industry's, but not what the bulge is made of."""
    dfw_by_year, us_by_year, hrs_by_year, usq_by_year = {}, {}, {}, {}
    usq = {}
    for r in rows:
        if (r["area_fips"] == NATION and r["own_code"] == PRIVATE
                and r["row_state"] == "published" and isnum(r["avg_wkly_wage"])
                and r["avg_wkly_wage"] > 0):
            usq[r["period"]] = r["avg_wkly_wage"]
            usq_by_year.setdefault(r["year"], {})[r["qtr"]] = r["avg_wkly_wage"]
    usq_yoy = {}
    for period, v in usq.items():
        year, qtr = int(period[:4]), int(period[5])
        prior = usq.get("%dQ%d" % (year - 1, qtr))
        if prior:
            usq_yoy[period] = round((v / prior - 1.0) * 100.0, 2)

    for r in series:
        if isnum(r["wkly_wage"]):
            dfw_by_year.setdefault(r["year"], {})[r["qtr"]] = r["wkly_wage"]
    for r in national:
        if isnum(r["weekly_earnings"]):
            us_by_year.setdefault(r["year"], {})[r["qtr"]] = r["weekly_earnings"]
        if isnum(r["weekly_hours"]):
            hrs_by_year.setdefault(r["year"], {})[r["qtr"]] = r["weekly_hours"]

    rows = []
    for name, window in PROFILE_WINDOWS.items():
        dfw_p, dfw_n = seasonal_profile(dfw_by_year, window)
        us_p, us_n = seasonal_profile(us_by_year, window)
        hrs_p, _ = seasonal_profile(hrs_by_year, window)
        usq_p, usq_n = seasonal_profile(usq_by_year, window)
        if not (dfw_p and us_p):
            continue
        for q in (1, 2, 3, 4):
            rows.append({
                "profile_window": name,
                "qtr": q,
                "dfw_complete_years": dfw_n,
                "us_complete_years": us_n,
                "us_qcew_complete_years": usq_n,
                "dfw_qcew_seasonal_factor_pct": dfw_p[q],
                "us_qcew_seasonal_factor_pct": usq_p[q] if usq_p else "",
                "dfw_minus_us_qcew_pct_points": (
                    round(dfw_p[q] - usq_p[q], 2) if usq_p else ""),
                "us_ces_weekly_earnings_seasonal_factor_pct": us_p[q],
                "us_ces_weekly_hours_seasonal_factor_pct": hrs_p[q] if hrs_p else "",
                "dfw_adjustment": "not seasonally adjusted",
                "us_qcew_adjustment": "not seasonally adjusted (same measure as DFW)",
                "us_ces_adjustment": "SEASONALLY ADJUSTED (CES prefix)",
                "qcew_comparison_valid": 1,
                "ces_comparison_valid": 0,
                "note": ("Difference DFW against us_qcew only: same programme, same "
                         "measure, neither side adjusted. The us_ces factors sit near "
                         "100 because that series is already seasonally adjusted -- a "
                         "diagnostic, not a comparator. Splitting the bulge into hours "
                         "vs bonus still needs unadjusted CEU hours, which QCEW cannot "
                         "supply at all."),
            })
        if name == "2016_2025":
            stats["seasonality_2016_2025"] = {
                "dfw_qcew_not_seasonally_adjusted": dfw_p,
                "us_qcew_not_seasonally_adjusted": usq_p,
                "dfw_minus_us_qcew_pct_points": (
                    dict((q, round(dfw_p[q] - usq_p[q], 2)) for q in (1, 2, 3, 4))
                    if usq_p else None),
                "us_ces_weekly_earnings_SEASONALLY_ADJUSTED": us_p,
                "us_ces_weekly_hours_SEASONALLY_ADJUSTED": hrs_p,
                "hours_vs_bonus_split": "BLOCKED - needs unadjusted CEU hours",
            }

    joined = []
    nat = {r["period"]: r for r in national}
    for r in series:
        n_ = nat.get(r["period"])
        if not n_ or not isnum(r["wkly_wage"]) or not isnum(n_["weekly_earnings"]):
            continue
        joined.append({
            "period": r["period"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "dfw_series_basis": r["series_basis"],
            "dfw_qcew_wkly_wage": r["wkly_wage"],
            "us_ces_2373_weekly_earnings": n_["weekly_earnings"],
            "us_ces_2373_hourly_earnings": n_["hourly_earnings"],
            "us_ces_2373_weekly_hours": n_["weekly_hours"],
            "us_qcew_237310_wkly_wage": usq.get(r["period"], ""),
            "us_qcew_yoy_pct": usq_yoy.get(r["period"], ""),
            "yoy_gap_vs_us_qcew_pct_points": (
                round(r["yoy_pct"] - usq_yoy[r["period"]], 2)
                if isnum(r["yoy_pct"]) and r["period"] in usq_yoy else ""),
            "which_gap_is_same_source": "yoy_gap_vs_us_qcew_pct_points",
            "dfw_over_us_ratio": round(r["wkly_wage"] / n_["weekly_earnings"], 4),
            "dfw_yoy_pct": r["yoy_pct"],
            "us_weekly_earnings_yoy_pct": n_["weekly_earnings_yoy_pct"],
            "yoy_gap_pct_points": (
                round(r["yoy_pct"] - n_["weekly_earnings_yoy_pct"], 2)
                if isnum(r["yoy_pct"]) and isnum(n_["weekly_earnings_yoy_pct"])
                else ""),
        })
    stats["joined_quarters"] = len(joined)
    for label, key in (("vs_us_qcew_same_source", "yoy_gap_vs_us_qcew_pct_points"),
                       ("vs_us_ces_adjustment_mismatch", "yoy_gap_pct_points")):
        g = [j[key] for j in joined if j[key] != ""]
        if len(g) > 1:
            mean = sum(g) / len(g)
            sd = (sum((x - mean) ** 2 for x in g) / (len(g) - 1)) ** 0.5
            ranked = sorted([(j["period"], j[key]) for j in joined if j[key] != ""],
                            key=lambda t: -t[1])
            stats["yoy_gap_" + label] = {
                "n": len(g), "mean_pct_points": round(mean, 3),
                "sd_pct_points": round(sd, 3),
                "min": round(min(g), 2), "max": round(max(g), 2),
                "dfw_ahead_most": ["%s %+.2f" % (p, v) for p, v in ranked[:4]],
                "dfw_behind_most": ["%s %+.2f" % (p, v) for p, v in ranked[-3:]],
            }
    return rows, joined


def main():
    stats = {
        "raw_files": 0, "raw_rows": 0, "dropped_out_of_scope": 0,
        "dropped_other_industry": 0, "kept_rows": 0, "suppressed_cells": 0,
        "placeholder_cells": 0,
        "bridged_periods": [], "unavailable_periods": [], "bridge_validation": [],
        "yoy_disagreements": 0, "yoy_disagreement_detail": [],
    }
    rows = load(stats)
    series = build_series(rows, stats)
    supp = build_suppression_report(rows)
    annual_cmp = compare_with_annual(series, stats)
    national, _ = build_national_quarterly(stats)
    seasonality, joined = build_seasonality_comparison(
        series, national, rows, stats)

    long_fields = [
        "period", "year", "qtr", "period_start", "period_end", "area_fips",
        "area_name", "area_level", "dfw_county", "own_code", "ownership",
        "industry_code", "agglvl_code", "disclosure_code", "row_state",
        "suppressed", "placeholder_zero", "avg_emplvl",
    ] + MEASURES
    series_fields = [
        "period", "year", "qtr", "period_start", "period_end", "industry_code",
        "series_basis", "wkly_wage", "yoy_pct", "yoy_compared_with",
        "yoy_crosses_basis_change", "county_set_same_as_yoy_comparison",
        "county_emplvl_sum_yoy_pct", "wkly_wage_4q_rolling", "rolling4_window",
        "rolling4_mixes_bases", f"wkly_wage_index_{BASE_PERIOD[0]}q{BASE_PERIOD[1]}_100",
        "qoq_pct_SEASONAL", "qoq_crosses_basis_change", "msa_state", "msa_wkly_wage",
        "msa_avg_emplvl", "msa_qtrly_estabs", "county_weighted_wkly_wage",
        "counties_published", "county_names", "county_emplvl_sum",
        "county_coverage_pct_of_msa", "county_vs_msa_deviation_pct",
    ]

    written = {
        "qcew_wages_quarterly.csv": write_csv(
            OUT / "qcew_wages_quarterly.csv", rows, long_fields),
        "qcew_dfw_highway_wage_quarterly.csv": write_csv(
            OUT / "qcew_dfw_highway_wage_quarterly.csv", series, series_fields),
        "qcew_suppression_report_quarterly.csv": write_csv(
            OUT / "qcew_suppression_report_quarterly.csv", supp,
            ["period", "reason", "area_fips", "area_name", "area_level",
             "ownership", "industry_code", "measure", "disclosure_code",
             "note_detail"]),
        "qcew_quarterly_vs_annual.csv": write_csv(
            OUT / "qcew_quarterly_vs_annual.csv", annual_cmp,
            ["year", "quarters_present", "quarterly_mean_wkly_wage",
             "annual_file_wkly_wage", "annual_series_basis", "quarterly_bases",
             "diff_pct"]),
        "ces_naics2373_quarterly.csv": write_csv(
            OUT / "ces_naics2373_quarterly.csv", national,
            ["period", "year", "qtr", "period_start", "period_end", "industry",
             "seasonal_adjustment", "months_in_quarter", "partial_quarter",
             "hourly_earnings",
             "weekly_hours", "weekly_earnings", "employment_thousands",
             "hourly_earnings_yoy_pct", "weekly_earnings_yoy_pct",
             "hourly_earnings_index_2016q1_100"]),
        "dfw_vs_us_quarterly_seasonality.csv": write_csv(
            OUT / "dfw_vs_us_quarterly_seasonality.csv", seasonality,
            ["profile_window", "qtr", "dfw_complete_years", "us_complete_years",
             "us_qcew_complete_years", "dfw_qcew_seasonal_factor_pct",
             "us_qcew_seasonal_factor_pct", "dfw_minus_us_qcew_pct_points",
             "us_ces_weekly_earnings_seasonal_factor_pct",
             "us_ces_weekly_hours_seasonal_factor_pct", "dfw_adjustment",
             "us_qcew_adjustment", "us_ces_adjustment", "qcew_comparison_valid",
             "ces_comparison_valid", "note"]),
        "dfw_vs_us_quarterly_wages.csv": write_csv(
            OUT / "dfw_vs_us_quarterly_wages.csv", joined,
            ["period", "period_start", "period_end", "dfw_series_basis",
             "dfw_qcew_wkly_wage", "us_ces_2373_weekly_earnings",
             "us_ces_2373_hourly_earnings", "us_ces_2373_weekly_hours",
             "us_qcew_237310_wkly_wage", "dfw_over_us_ratio", "dfw_yoy_pct",
             "us_weekly_earnings_yoy_pct", "yoy_gap_pct_points",
             "us_qcew_yoy_pct", "yoy_gap_vs_us_qcew_pct_points",
             "which_gap_is_same_source"]),
    }

    devs = [b["deviation_pct"] for b in stats["bridge_validation"]]
    if devs:
        stats["bridge_max_abs_deviation_pct"] = round(max(abs(d) for d in devs), 2)
        stats["bridge_mean_abs_deviation_pct"] = round(
            sum(abs(d) for d in devs) / len(devs), 2)
        stats["bridge_overlap_periods"] = len(devs)
    stats["files_written"] = written
    stats["parsed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (OUT / "parse_summary_quarterly.json").write_text(
        json.dumps(stats, indent=2), encoding="utf-8")

    print(f"raw files {stats['raw_files']}, rows scanned {stats['raw_rows']}, "
          f"kept {stats['kept_rows']}, suppressed cells {stats['suppressed_cells']}, "
          f"placeholder cells {stats['placeholder_cells']}")
    for k, v in written.items():
        print(f"  {k:40s} {v:6d} rows")
    print(f"  MSA published    : "
          f"{sum(1 for r in series if r['series_basis'] == 'msa_published')} quarters")
    print(f"  bridged          : {len(stats['bridged_periods'])} quarters "
          f"{stats['bridged_periods']}")
    print(f"  UNAVAILABLE      : {stats['unavailable_periods'] or 'none'}")
    if devs:
        print(f"  bridge validation: {stats['bridge_overlap_periods']} overlap quarters, "
              f"max |dev| {stats['bridge_max_abs_deviation_pct']}%, "
              f"mean {stats['bridge_mean_abs_deviation_pct']}%")
    print(f"  yoy vs BLS oty   : {stats['yoy_disagreements']} disagreements")
    print(f"  annual cross-chk : {stats.get('annual_comparison')}")
    sz = stats.get("seasonality_2016_2025")
    if sz:
        print("  seasonal factors (2016-2025 complete years, % of own-year mean):")
        print("    DFW QCEW wage bill   NSA  "
              + "  ".join(f"Q{q} {sz['dfw_qcew_not_seasonally_adjusted'][q]:6.2f}"
                          for q in (1, 2, 3, 4)))
        print("    US  QCEW wage bill   NSA  "
              + "  ".join(f"Q{q} {sz['us_qcew_not_seasonally_adjusted'][q]:6.2f}"
                          for q in (1, 2, 3, 4)))
        d = sz.get("dfw_minus_us_qcew_pct_points")
        if d:
            print("    DFW minus US  VALID      "
                  + "  ".join(f"Q{q} {d[q]:+6.2f}" for q in (1, 2, 3, 4)))
        print("    US CES weekly earn   SA   "
              + "  ".join(
                  f"Q{q} {sz['us_ces_weekly_earnings_SEASONALLY_ADJUSTED'][q]:6.2f}"
                  for q in (1, 2, 3, 4))
              + "   <- diagnostic only")
        print("    US CES weekly hours  SA   "
              + "  ".join(
                  f"Q{q} {sz['us_ces_weekly_hours_SEASONALLY_ADJUSTED'][q]:6.2f}"
                  for q in (1, 2, 3, 4))
              + "   <- diagnostic only")
        print("    hours-vs-bonus split: BLOCKED. QCEW records no hours, and the "
              "CES hours series is already")
        print("                          seasonally adjusted; needs the unadjusted "
              "CEU hours twin.")
    print(f"  joined quarters  : {stats.get('joined_quarters')}")
    for label, title in (("vs_us_qcew_same_source", "vs US QCEW (same source) "),
                         ("vs_us_ces_adjustment_mismatch", "vs US CES (mismatched)")):
        g = stats.get("yoy_gap_" + label)
        if g:
            print(f"  yoy gap {title}: n={g['n']:2d} mean {g['mean_pct_points']:+.3f} "
                  f"sd {g['sd_pct_points']:.3f} range {g['min']:+.2f}..{g['max']:+.2f}")
            print(f"      DFW ahead most: {', '.join(g['dfw_ahead_most'])}")


if __name__ == "__main__":
    main()
