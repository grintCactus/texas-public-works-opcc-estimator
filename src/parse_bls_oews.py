"""
BLS OEWS occupational wages + BLS CES hourly earnings -> construction labour-price features.

WHY THIS FILE EXISTS
--------------------
src/parse_bls_qcew.py produced a DFW construction labour-cost trend from QCEW
(average weekly wage, NAICS 237310, 2015-2025). That series has two documented
weaknesses, both stated in data/external/dol/qcew/README.md:

  (a) it is a wage BILL per worker, not a wage RATE -- QCEW collects no hours, so
      overtime and bonus swings look identical to rate rises;
  (b) it is composition-sensitive -- a shift toward operators and away from
      labourers raises it with no trade seeing a raise.

This file attacks both with two different BLS programmes:

  OEWS  (Occupational Employment and Wage Statistics) -- hourly wage BY TRADE for
        the DFW metro, with employment counts. Gives the trade-level price of
        labour and the occupational mix weights. CROSS-SECTION ONLY (see below).
  CES   (Current Employment Statistics) -- average HOURLY earnings and average
        WEEKLY HOURS for NAICS 2373 (highway, street and bridge construction).
        Monthly, true hourly, exact industry. National only (see below).

TWO HARD NEGATIVES, MEASURED 2026-11-18 -- do not re-litigate these without new
evidence, they each cost an hour:

  1. OEWS HAS NO USABLE HISTORY THROUGH ANY PROGRAMMATIC ROUTE.
     - The BLS time-series API holds ONLY the latest OEWS reference period. Asking
       for 2016-2024 on an OEWS series returns "No Data Available" for every year
       and a value for 2025 alone. This is deliberate: BLS does not treat OEWS as
       a time series (survey panels rotate, the SOC and area definitions change).
     - The historical OEWS files (www.bls.gov/oes/special-requests/oesm{yy}ma.zip)
       sit behind bot protection: HTTP 403 for an ordinary scripted GET, and
       download.bls.gov returns the "Access Denied / automated retrieval" page.
       BLS grants access to clients that put a contact e-mail in the User-Agent;
       that is the user's personal data, so it is NOT done here. Getting OEWS
       history therefore needs either (i) the user's own browser, or (ii) the
       user's explicit go-ahead to send a contact address to BLS.
     => OEWS here is ONE reference period (May 2025). It gives levels and mix.
        It CANNOT give a trend, and nothing in these outputs pretends otherwise.

  2. THERE IS NO DFW-LEVEL CONSTRUCTION HOURLY-EARNINGS SERIES IN CES.
     Verified: DFW total nonfarm employment exists (SMU48191000000000001), so the
     area code is right -- but every construction-detail series for the MSA and for
     both Dallas-Plano-Irving / Fort Worth-Arlington metropolitan divisions returns
     "Series does not exist". CES publishes construction hourly earnings for Texas
     as a whole (mining+logging+construction supersector) and for the nation by
     detailed NAICS, and that is all. So the hours-adjusted series below are
     national (NAICS 2373, exact industry) and statewide (Texas, broad sector);
     the DFW-specific series remains QCEW's, with its wage-bill caveat.

WHAT AN OEWS WAGE IS AND IS NOT
-------------------------------
IS: the mean / median hourly wage paid to that occupation in that area across ALL
    industries that employ it, from an establishment survey, for the May 2025
    reference period. Straight-time pay. Excludes overtime premium, bonuses,
    and all employer-paid benefits.
IS NOT: a union scale, a Davis-Bacon determination, a cost to the contractor
    (no fringe, no burden, no payroll tax), or anything industry-specific. A DFW
    "construction laborer" wage here is pooled over building, heavy, highway and
    specialty work -- it is not a highway-only figure.

GEOGRAPHY
---------
OEWS metro area 0019100 = Dallas-Fort Worth-Arlington TX MSA -- the same 13
counties as this project's DFW definition and the same area as QCEW's C1910.
Confirmed that OEWS publishes the combined MSA, NOT the two metropolitan
divisions (OEUM0019124... does not exist), so no division bridging is needed.
Comparators: OEUS4800000 = Texas, OEUN0000000 = United States.

SUPPRESSION / MISSING
---------------------
Unlike QCEW, the API omits a series that was not published rather than
zero-filling it. A missing series is recorded in the outputs as an empty cell plus
a row in the availability report -- never as a zero.

API BUDGET
----------
api.bls.gov public API v1 (no registration key) allows 25 QUERIES per day, 25
series per query, 10 years per query. The fetcher batches to stay inside that and
refuses to exceed MAX_QUERIES. Raw JSON responses are written to disk so the parse
side never needs the network again.

OUTPUTS (data/external/dol/oews/clean/)
---------------------------------------
  oews_dfw_trades_2025.csv        long: soc x area x measure, May 2025
  oews_dfw_vs_tx_us_2025.csv      wide per trade: DFW/TX/US hourly wage + gaps
  oews_mix_decomposition.csv      DFW-vs-US gap split into PAY and MIX
  ces_hourly_earnings_monthly.csv monthly CES/SMU hourly earnings and hours
  ces_naics2373_annual.csv        annual NAICS 2373 hourly earnings + index
  qcew_vs_ces_premium.csv         DFW QCEW weekly wage vs national 2373 weekly
  availability_report.csv         every series asked for, and whether it came back
  parse_summary.json              counters for this run
"""

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OEWS_RAW = ROOT / "data" / "external" / "dol" / "oews" / "raw"
CES_RAW = ROOT / "data" / "external" / "dol" / "ces" / "raw"
OUT = ROOT / "data" / "external" / "dol" / "oews" / "clean"
QCEW_INDEX = ROOT / "data" / "external" / "dol" / "qcew" / "clean" / "qcew_dfw_highway_wage_index.csv"
QCEW_ANNUAL = ROOT / "data" / "external" / "dol" / "qcew" / "clean" / "qcew_wages_annual.csv"

API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
MAX_QUERIES = 14          # hard cap for one fetch run; v1 allows 25/day/IP
BATCH = 25                # series per query (v1 maximum)

OEWS_YEAR = "2025"        # the only reference period the API serves
CES_START, CES_END = "2016", "2025"
CES_TAIL_START, CES_TAIL_END = "2026", "2026"

AREAS = {
    "dfw": ("M", "0019100", "Dallas-Fort Worth-Arlington, TX MSA"),
    "tx": ("S", "4800000", "Texas"),
    "us": ("N", "0000000", "United States"),
}

# OEWS datatype codes. 01 employment, 03 hourly mean, 08 hourly median.
OEWS_DATATYPES = {"01": "employment", "03": "hourly_mean", "08": "hourly_median"}
# Comparator areas get the hourly mean only, to stay inside the query budget.
COMPARATOR_DATATYPES = ["03"]

# SOC codes that actually turn up on a highway / heavy civil job, plus the
# estimating and engineering roles that sit in a bid's indirect cost.
SOC = [
    ("000000", "All occupations"),
    ("470000", "Construction and extraction occupations (major group)"),
    ("471011", "First-line supervisors of construction trades and extraction workers"),
    ("472011", "Boilermakers"),
    ("472021", "Brickmasons and blockmasons"),
    ("472031", "Carpenters"),
    ("472051", "Cement masons and concrete finishers"),
    ("472061", "Construction laborers"),
    ("472071", "Paving, surfacing, and tamping equipment operators"),
    ("472072", "Pile driver operators"),
    ("472073", "Operating engineers and other construction equipment operators"),
    ("472081", "Drywall and ceiling tile installers"),
    ("472111", "Electricians"),
    ("472131", "Insulation workers, floor, ceiling, and wall"),
    ("472132", "Insulation workers, mechanical"),
    ("472141", "Painters, construction and maintenance"),
    ("472151", "Pipelayers"),
    ("472152", "Plumbers, pipefitters, and steamfitters"),
    ("472161", "Plasterers and stucco masons"),
    ("472171", "Reinforcing iron and rebar workers"),
    ("472181", "Roofers"),
    ("472211", "Sheet metal workers"),
    ("472221", "Structural iron and steel workers"),
    ("473011", "Helpers--brickmasons, blockmasons, stonemasons, tile/marble setters"),
    ("473012", "Helpers--carpenters"),
    ("473013", "Helpers--electricians"),
    ("473015", "Helpers--pipelayers, plumbers, pipefitters, and steamfitters"),
    ("473016", "Helpers--roofers"),
    ("473019", "Helpers, construction trades, all other"),
    ("474011", "Construction and building inspectors"),
    ("474031", "Fence erectors"),
    ("474041", "Hazardous materials removal workers"),
    ("474051", "Highway maintenance workers"),
    ("474061", "Rail-track laying and maintenance equipment operators"),
    ("474071", "Septic tank servicers and sewer pipe cleaners"),
    ("474099", "Construction and related workers, all other"),
    ("533032", "Heavy and tractor-trailer truck drivers"),
    ("533033", "Light truck drivers"),
    ("537021", "Crane and tower operators"),
    ("537051", "Industrial truck and tractor operators"),
    ("499071", "Maintenance and repair workers, general"),
    ("119021", "Construction managers"),
    ("131051", "Cost estimators"),
    ("172051", "Civil engineers"),
    ("173022", "Civil engineering technologists and technicians"),
    ("171022", "Surveyors"),
    ("173031", "Surveying and mapping technicians"),
]
SOC_TITLE = dict(SOC)

# Trades that actually price a highway bid. Used for the mix decomposition so the
# answer is not dominated by "all occupations" or by office roles.
#
# TWO baskets, deliberately, because the weights are the weak point. OEWS gives
# occupational employment for the WHOLE metro economy, not for NAICS 2373, and
# metro x industry x occupation is not published at all. So 53-3032 (heavy truck
# drivers) brings 55,670 DFW jobs into the basket -- about a third of it -- when
# most of those drive for freight carriers, not for highway contractors. The
# "trades_only" basket drops the two 53-* codes so the result is not hostage to
# that. Report both; prefer trades_only when quoting one number.
HIGHWAY_TRADES = [
    "471011", "472051", "472061", "472071", "472072", "472073", "472151",
    "472171", "472221", "474051", "474061", "533032", "537021",
]
BASKETS = {
    "highway_all": HIGHWAY_TRADES,
    "highway_trades_only": [s for s in HIGHWAY_TRADES if not s.startswith("53")],
}

CES_SERIES = {
    "CES2023730003": "US NAICS 2373 highway/street/bridge - avg hourly earnings, all employees",
    "CES2023730007": "US NAICS 2373 highway/street/bridge - avg weekly hours, all employees",
    "CES2023730011": "US NAICS 2373 highway/street/bridge - avg weekly earnings, all employees",
    "CES2023730001": "US NAICS 2373 highway/street/bridge - all employees (thousands)",
    # The UNADJUSTED twins of the four series above. The prefix is the seasonal
    # adjustment and nothing else differs: CES/SMS are adjusted, CEU/SMU are not.
    # The adjusted ones are right for a trend or a yoy rate; only these can be
    # used for anything about seasonality, because BLS has already removed the
    # seasonality from the CES versions.
    "CEU2023730003": "US NAICS 2373 highway/street/bridge - avg hourly earnings, NOT seasonally adjusted",
    "CEU2023730007": "US NAICS 2373 highway/street/bridge - avg weekly hours, NOT seasonally adjusted",
    "CEU2023730011": "US NAICS 2373 highway/street/bridge - avg weekly earnings, NOT seasonally adjusted",
    "CEU2023730001": "US NAICS 2373 highway/street/bridge - all employees (thousands), NOT seasonally adjusted",
    "CES2023700003": "US NAICS 237 heavy and civil engineering - avg hourly earnings",
    "CES2023700001": "US NAICS 237 heavy and civil engineering - all employees (thousands)",
    "CES2000000003": "US construction (supersector 20) - avg hourly earnings",
    "CES2000000007": "US construction (supersector 20) - avg weekly hours",
    "SMU48000002000000003": "Texas mining+logging+construction - avg hourly earnings",
    "SMU48000002000000011": "Texas mining+logging+construction - avg weekly earnings",
    "SMU48000002000000001": "Texas mining+logging+construction - all employees (thousands)",
    "SMU48191000000000001": "DFW MSA total nonfarm - all employees (thousands)",
}

MONTHS = {f"M{i:02d}": i for i in range(1, 13)}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def oews_id(areatype, areacode, soc, datatype):
    return "OEU" + areatype + areacode + "000000" + soc + datatype


def num(v):
    """Numeric or None. BLS writes '-' and '' for withheld values."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "(n)", "*"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def fmt(v, nd=2):
    return "" if v is None else round(v, nd)


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

def post(seriesids, startyear, endyear):
    """One API query via curl. urllib is avoided: custom headers get rejected by
    some BLS/SAM endpoints, and curl's defaults are what was verified working."""
    payload = json.dumps(
        {"seriesid": list(seriesids), "startyear": startyear, "endyear": endyear}
    )
    proc = subprocess.run(
        ["curl", "-s", "--max-time", "120", "-X", "POST", API,
         "-H", "Content-Type: application/json", "--data-binary", "@-"],
        input=payload, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("curl failed: " + proc.stderr[:300])
    return json.loads(proc.stdout)


CEU_SERIES = [
    "CEU2023730003", "CEU2023730007", "CEU2023730011", "CEU2023730001",
]


def fetch_ceu():
    """Exactly TWO queries: the unadjusted national NAICS 2373 twins, 2016-2025
    and the 2026 tail. Kept apart from fetch() so the seasonality series can be
    topped up without spending the whole daily budget on OEWS again.

    If a query is refused, this STOPS. The api.bls.gov v1 limit is 25 queries per
    day per IP and it is shared by everyone behind the same address, so a refusal
    means the window is gone, not that a retry might squeeze through. Retrying is
    what turns one exhausted budget into a blocked one."""
    CES_RAW.mkdir(parents=True, exist_ok=True)
    jobs = [(f"ceu_{CES_START}_{CES_END}_00", CES_START, CES_END),
            (f"ceu_{CES_TAIL_START}_00", CES_TAIL_START, CES_TAIL_END)]
    print(f"{len(jobs)} queries planned ({len(CEU_SERIES)} series each)")
    for label, y0, y1 in jobs:
        resp = post(CEU_SERIES, y0, y1)
        status = resp.get("status")
        got = sum(1 for x in resp.get("Results", {}).get("series", []) if x.get("data"))
        if status != "REQUEST_SUCCEEDED":
            msg = "; ".join(resp.get("message", []))[:300]
            print(f"  {label:22s} {status}")
            print("    !! " + msg)
            sys.exit("STOPPING, not retrying: the daily query window is spent. "
                     "Nothing was written. Re-run after it resets.")
        path = CES_RAW / f"{label}.json"
        path.write_text(json.dumps(resp, indent=1), encoding="utf-8")
        print(f"  {label:22s} {status:18s} asked={len(CEU_SERIES)} returned={got} -> {path.name}")
    print("done. now re-run: python src/parse_bls_oews.py parse")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch():
    OEWS_RAW.mkdir(parents=True, exist_ok=True)
    CES_RAW.mkdir(parents=True, exist_ok=True)

    jobs = []  # (label, dest_dir, seriesids, startyear, endyear)

    for area_key, (atype, acode, _) in AREAS.items():
        dts = OEWS_DATATYPES if area_key == "dfw" else {
            k: v for k, v in OEWS_DATATYPES.items() if k in COMPARATOR_DATATYPES
        }
        ids = [oews_id(atype, acode, soc, dt) for soc, _ in SOC for dt in dts]
        for i, part in enumerate(chunks(ids, BATCH)):
            jobs.append((f"oews_{area_key}_{i:02d}", OEWS_RAW, part, OEWS_YEAR, OEWS_YEAR))

    ces_ids = sorted(CES_SERIES)
    for i, part in enumerate(chunks(ces_ids, BATCH)):
        jobs.append((f"ces_{CES_START}_{CES_END}_{i:02d}", CES_RAW, part, CES_START, CES_END))
        jobs.append((f"ces_{CES_TAIL_START}_{i:02d}", CES_RAW, part, CES_TAIL_START, CES_TAIL_END))

    print(f"{len(jobs)} queries planned ({sum(len(j[2]) for j in jobs)} series); cap {MAX_QUERIES}")
    if len(jobs) > MAX_QUERIES:
        sys.exit(f"ABORT: {len(jobs)} queries exceeds MAX_QUERIES={MAX_QUERIES}. Trim the lists.")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for label, dest, ids, y0, y1 in jobs:
        resp = post(ids, y0, y1)
        status = resp.get("status")
        got = sum(1 for s in resp.get("Results", {}).get("series", []) if s.get("data"))
        path = dest / f"{label}.json"
        path.write_text(json.dumps(resp, indent=1), encoding="utf-8")
        print(f"  {label:26s} {status:18s} asked={len(ids):3d} returned={got:3d} -> {path.name}")
        if status != "REQUEST_SUCCEEDED":
            print("    !! " + "; ".join(resp.get("message", []))[:300])

    for dest, what in ((OEWS_RAW, "OEWS"), (CES_RAW, "CES")):
        (dest / "PROVENANCE.txt").write_text(
            f"{what} pulled from {API} (public API v1, no registration key)\n"
            f"fetched_utc: {stamp}\n"
            "fetcher: src/parse_bls_oews.py fetch\n"
            "Raw JSON exactly as returned. Re-fetching is not needed to rebuild the\n"
            "clean outputs: python src/parse_bls_oews.py parse reads these files only.\n"
            "v1 limits: 25 queries/day/IP, 25 series/query, 10 years/query.\n",
            encoding="utf-8",
        )
    print("fetch done")


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #

def load_raw(folder):
    """seriesID -> list of observation dicts, merged across all batch files."""
    series = {}
    asked = set()
    files = sorted(folder.glob("*.json"))
    for path in files:
        resp = json.loads(path.read_text(encoding="utf-8"))
        for s in resp.get("Results", {}).get("series", []):
            sid = s["seriesID"]
            asked.add(sid)
            series.setdefault(sid, []).extend(s.get("data", []))
    return series, asked, files


def parse_oews(series, stats):
    """Long rows: one per (soc, area, measure). Missing stays empty."""
    rows = []
    values = {}  # (area_key, soc, measure) -> float
    for area_key, (atype, acode, area_label) in AREAS.items():
        dts = OEWS_DATATYPES if area_key == "dfw" else {
            k: v for k, v in OEWS_DATATYPES.items() if k in COMPARATOR_DATATYPES
        }
        for soc, title in SOC:
            for dt, measure in dts.items():
                sid = oews_id(atype, acode, soc, dt)
                obs = series.get(sid, [])
                val = num(obs[0]["value"]) if obs else None
                if val is not None:
                    values[(area_key, soc, measure)] = val
                else:
                    stats["oews_missing"] += 1
                rows.append({
                    "reference_period": "May " + OEWS_YEAR,
                    "area_key": area_key,
                    "area_code": acode,
                    "area_name": area_label,
                    "soc_code": soc[:2] + "-" + soc[2:],
                    "soc_title": title,
                    "measure": measure,
                    "value": fmt(val),
                    "series_id": sid,
                    "available": "1" if val is not None else "0",
                })
    stats["oews_rows"] = len(rows)
    stats["oews_available"] = sum(1 for r in rows if r["available"] == "1")
    return rows, values


def parse_oews_wide(values, stats):
    """One row per trade: DFW / TX / US hourly mean, DFW employment and share."""
    dfw_47_emp = values.get(("dfw", "470000", "employment"))
    rows = []
    for soc, title in SOC:
        d_mean = values.get(("dfw", soc, "hourly_mean"))
        d_med = values.get(("dfw", soc, "hourly_median"))
        t_mean = values.get(("tx", soc, "hourly_mean"))
        u_mean = values.get(("us", soc, "hourly_mean"))
        emp = values.get(("dfw", soc, "employment"))
        rows.append({
            "soc_code": soc[:2] + "-" + soc[2:],
            "soc_title": title,
            "in_highway_basket": "1" if soc in HIGHWAY_TRADES else "0",
            "dfw_employment": fmt(emp, 0),
            "dfw_share_of_soc47_pct": fmt(100.0 * emp / dfw_47_emp, 2)
            if (emp is not None and dfw_47_emp) else "",
            "dfw_hourly_mean": fmt(d_mean),
            "dfw_hourly_median": fmt(d_med),
            "tx_hourly_mean": fmt(t_mean),
            "us_hourly_mean": fmt(u_mean),
            "dfw_vs_tx_pct": fmt(100.0 * (d_mean / t_mean - 1.0), 2)
            if (d_mean and t_mean) else "",
            "dfw_vs_us_pct": fmt(100.0 * (d_mean / u_mean - 1.0), 2)
            if (d_mean and u_mean) else "",
            "dfw_annual_at_2080h": fmt(d_mean * 2080.0, 0) if d_mean else "",
        })
    stats["oews_trades"] = len(rows)
    return rows


def parse_mix_decomposition(values, stats):
    """
    Split the DFW-vs-US hourly wage gap for the highway basket into a PAY part and
    a MIX part. This is a cross-sectional (area) decomposition, NOT a decomposition
    over time -- one OEWS reference period cannot give the latter.

      W_dfw = sum(w_dfw * p_dfw)      actual DFW basket wage
      W_us  = sum(w_us  * p_us)       actual US basket wage
      W_cf  = sum(w_dfw * p_us)       US pay at DFW mix  (counterfactual)

      pay_effect = W_dfw - W_cf       same mix, different pay
      mix_effect = W_cf  - W_us       same pay, different mix

    Only trades where DFW employment, DFW wage and US wage are all published enter
    the basket; the basket is renormalised over those, and the row records how much
    of DFW SOC-47 employment it covers so the reader can judge the coverage.
    """
    dfw_47 = values.get(("dfw", "470000", "employment"))
    rows = []
    for name, socs in BASKETS.items():
        basket, dropped = [], 0
        for soc in socs:
            w = values.get(("dfw", soc, "employment"))
            p_dfw = values.get(("dfw", soc, "hourly_mean"))
            p_us = values.get(("us", soc, "hourly_mean"))
            p_tx = values.get(("tx", soc, "hourly_mean"))
            if w is None or p_dfw is None or p_us is None:
                dropped += 1
                continue
            basket.append((soc, w, p_dfw, p_us, p_tx))

        tot_w = sum(b[1] for b in basket)
        if not basket or not tot_w:
            continue

        w_dfw_pay = sum(b[1] * b[2] for b in basket) / tot_w
        w_us_at_dfw_mix = sum(b[1] * b[3] for b in basket) / tot_w
        tx_w = sum(b[1] for b in basket if b[4] is not None)
        w_tx_at_dfw_mix = (
            sum(b[1] * b[4] for b in basket if b[4] is not None) / tx_w if tx_w else None
        )

        stats["mix_trades_dropped"] += dropped
        rows.append({
            "basket": name,
            "trades_in_basket": len(basket),
            "trades_dropped_unpublished": dropped,
            "basket_dfw_employment": fmt(tot_w, 0),
            "basket_share_of_dfw_soc47_pct": fmt(100.0 * tot_w / dfw_47, 2) if dfw_47 else "",
            "dfw_wage_at_dfw_mix": fmt(w_dfw_pay),
            "us_wage_at_dfw_mix": fmt(w_us_at_dfw_mix),
            "tx_wage_at_dfw_mix": fmt(w_tx_at_dfw_mix),
            "dfw_minus_us_same_mix_usd": fmt(w_dfw_pay - w_us_at_dfw_mix),
            "dfw_minus_us_same_mix_pct": fmt(100.0 * (w_dfw_pay / w_us_at_dfw_mix - 1.0), 2),
            "dfw_minus_tx_same_mix_pct": fmt(100.0 * (w_dfw_pay / w_tx_at_dfw_mix - 1.0), 2)
            if w_tx_at_dfw_mix else "",
            "note": "fixed-weight (DFW metro employment) basket; pay-only comparison, mix held constant",
        })
        stats[f"mix_{name}_dfw_wage"] = round(w_dfw_pay, 4)
        stats[f"mix_{name}_us_wage_at_dfw_mix"] = round(w_us_at_dfw_mix, 4)
    return rows


def build_adjustment_validation(ces_annual_rows, stats):
    """Does building the annual NAICS 2373 table on SEASONALLY ADJUSTED monthlies
    distort the annual trend?

    The honest answer needs the unadjusted CEU twins, and they are not available
    (api.bls.gov daily limit; FRED carries no national 2373 earnings series at
    all). What IS available is a source with no seasonal adjustment anywhere in
    it: QCEW national private 237310 average weekly wage, an administrative
    census of every covered employer rather than a survey. If adjustment were
    moving the annual figures, the CES annual trend would drift against it.

    WHAT THE GAP CONTAINS. Three things at once, not just adjustment:
      1. seasonal adjustment on the CES side (what is being tested),
      2. QCEW counts the whole wage bill including bonuses and severance, CES
         average weekly earnings excludes bonuses not paid every pay period,
      3. census versus sample survey.
    So the measured gap is an UPPER BOUND on the adjustment's own contribution:
    adjustment cannot be distorting the trend by more than the total, and the
    other two terms are known to be nonzero. A near-zero gap therefore bounds
    the thing asked about; it does not isolate it.
    """
    qcew = {}
    if QCEW_ANNUAL.exists():
        with QCEW_ANNUAL.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if (r["area_fips"] == "US000" and r["industry_code"] == "237310"
                        and r["own_code"] == "5" and r["annual_avg_wkly_wage"]):
                    qcew[int(r["year"])] = float(r["annual_avg_wkly_wage"])
    else:
        stats["adjustment_validation"] = "SKIPPED: qcew_wages_annual.csv not built"
        return []

    ces = {int(r["year"]): r for r in ces_annual_rows}
    rows = []
    for year in sorted(set(ces) | set(qcew)):
        c = ces.get(year, {})
        cw = num(str(c.get("naics2373_weekly_earnings", "")))
        qw = qcew.get(year)
        pc = ces.get(year - 1, {})
        pcw = num(str(pc.get("naics2373_weekly_earnings", "")))
        pqw = qcew.get(year - 1)
        cy = (cw / pcw - 1) * 100 if (cw and pcw) else None
        qy = (qw / pqw - 1) * 100 if (qw and pqw) else None
        partial = str(c.get("partial_year", "")) == "1"
        rows.append({
            "year": year,
            "ces_weekly_earnings": fmt(cw, 2),
            "ces_seasonal_adjustment": "SEASONALLY ADJUSTED (CES prefix)" if cw else "",
            "ces_basis": c.get("basis", ""),
            "ces_months_in_average": c.get("months_in_average", ""),
            "ces_partial_year": 1 if partial else 0,
            "qcew_weekly_wage": fmt(qw, 2),
            "qcew_seasonal_adjustment": "none (administrative census)" if qw else "",
            "qcew_series": "QCEW US private NAICS 237310 annual avg weekly wage" if qw else "",
            "ces_yoy_pct": fmt(cy, 2),
            "qcew_yoy_pct": fmt(qy, 2),
            "yoy_gap_pct_points": fmt(cy - qy, 2) if (cy is not None and qy is not None
                                                      and not partial) else "",
            "gap_contains": ("seasonal adjustment + QCEW bonus/severance inclusion "
                             "+ census vs survey; an UPPER BOUND on adjustment alone"),
        })

    full = [r for r in rows if r["yoy_gap_pct_points"] != ""]
    both = [r for r in rows if r["ces_weekly_earnings"] and r["qcew_weekly_wage"]
            and r["ces_partial_year"] == 0]
    if full and both:
        gaps = [float(r["yoy_gap_pct_points"]) for r in full]
        a, b = both[0], both[-1]
        cg = (float(b["ces_weekly_earnings"]) / float(a["ces_weekly_earnings"]) - 1) * 100
        qg = (float(b["qcew_weekly_wage"]) / float(a["qcew_weekly_wage"]) - 1) * 100
        stats["adjustment_validation"] = {
            "question": "does using ADJUSTED monthlies distort the annual 2373 trend",
            "benchmark": "QCEW US private 237310 annual avg weekly wage (census, unadjusted)",
            "window": f"{a['year']}-{b['year']}",
            "ces_total_growth_pct": round(cg, 2),
            "qcew_total_growth_pct": round(qg, 2),
            "total_growth_gap_pct_points": round(cg - qg, 2),
            "yoy_gap_years": len(gaps),
            "yoy_gap_mean_pct_points": round(sum(gaps) / len(gaps), 3),
            "yoy_gap_max_abs_pct_points": round(max(abs(g) for g in gaps), 2),
            "verdict": ("annual trend VERIFIED against an unadjusted census: the whole "
                        "discrepancy, adjustment plus two known definitional terms, is "
                        f"{round(cg - qg, 2)} pts over {b['year'] - a['year']} years. The "
                        "exact SA-minus-NSA annual difference is still unmeasured and "
                        "needs the CEU series."),
            "not_testable_here": ("weekly HOURS: QCEW records no hours, so the adjusted "
                                  "hours column has no unadjusted benchmark on disk"),
        }
    return rows


def parse_ces(series, stats):
    """Monthly long rows for every CES/SMU series. M13 (annual avg) kept separately."""
    rows = []
    annual = {}   # (series, year) -> annual value from M13 if BLS published one
    monthly = {}  # (series, year) -> [values]
    for sid, title in sorted(CES_SERIES.items()):
        obs = series.get(sid, [])
        if not obs:
            stats["ces_missing"] += 1
            continue
        seen = set()
        for o in obs:
            key = (o["year"], o["period"])
            if key in seen:
                continue   # the 2016-2025 and 2026 batches can overlap
            seen.add(key)
            val = num(o["value"])
            if val is None:
                continue
            if o["period"] == "M13":
                annual[(sid, int(o["year"]))] = val
                continue
            if o["period"] not in MONTHS:
                continue
            monthly.setdefault((sid, int(o["year"])), []).append(val)
            rows.append({
                "series_id": sid,
                "series_title": title,
                "year": o["year"],
                "month": MONTHS[o["period"]],
                "date": f"{o['year']}-{MONTHS[o['period']]:02d}",
                "value": val,
                "preliminary": "1" if any(
                    f.get("code") == "P" for f in o.get("footnotes", []) if f
                ) else "0",
            })
    rows.sort(key=lambda r: (r["series_id"], r["year"], r["month"]))
    stats["ces_rows"] = len(rows)
    stats["ces_series_with_data"] = len({r["series_id"] for r in rows})
    return rows, annual, monthly


def parse_ces_annual(annual, monthly, stats):
    """
    Annual NAICS 2373 hourly earnings, with an index and yoy. Where BLS publishes
    an M13 annual average it is used verbatim; otherwise the calendar-year mean of
    the published months is computed and the row says so, including how many months
    went into it (2026 is a partial year).
    """
    ahe = "CES2023730003"
    hours = "CES2023730007"
    weekly = "CES2023730011"
    emp = "CES2023730001"
    hcivil = "CES2023700003"
    allcon = "CES2000000003"

    def value(sid, year):
        if (sid, year) in annual:
            return annual[(sid, year)], "bls_m13", 12
        vals = monthly.get((sid, year))
        if vals:
            return sum(vals) / len(vals), "computed_mean", len(vals)
        return None, "unavailable", 0

    years = sorted({y for (_, y) in list(annual) + list(monthly)})
    base_year = 2016
    base = value(ahe, base_year)[0]

    # Index and yoy are computed from the ROUNDED published figure, not from the
    # full-precision mean, so that every derived column in this file reconciles
    # exactly from the columns next to it. The precision given up is under 0.02%.
    base = round(base, 2) if base else None

    rows = []
    prev = None
    for y in years:
        v, basis, n = value(ahe, y)
        h, _, _ = value(hours, y)
        wk, _, _ = value(weekly, y)
        e, _, _ = value(emp, y)
        hc, _, _ = value(hcivil, y)
        ac, _, _ = value(allcon, y)
        v = round(v, 2) if v is not None else None
        rows.append({
            "year": y,
            "naics2373_hourly_earnings": fmt(v),
            "basis": basis,
            "months_in_average": n,
            "partial_year": "1" if 0 < n < 12 else "0",
            "naics2373_weekly_hours": fmt(h),
            "naics2373_weekly_earnings": fmt(wk),
            # Annual AHE x annual hours does NOT equal the annual weekly-earnings
            # average: each is a mean of monthly values, and the mean of products
            # is not the product of means. This column states the gap so nobody
            # reconstructs weekly pay from the two columns to its left and then
            # wonders why it is off. Use naics2373_weekly_earnings as published.
            "weekly_vs_ahe_x_hours_pct": fmt(100.0 * (wk / (v * h) - 1.0), 2)
            if (wk and v and h) else "",
            "naics2373_employment_thousands": fmt(e, 1),
            "naics237_hourly_earnings": fmt(hc),
            "all_construction_hourly_earnings": fmt(ac),
            "index_2016_100": fmt(100.0 * v / base, 2) if (v and base) else "",
            "yoy_pct": fmt(100.0 * (v / prev - 1.0), 2) if (v and prev) else "",
        })
        if v:
            prev = v
    stats["ces_annual_years"] = len(rows)
    return rows


def parse_premium(ces_annual, stats):
    """
    DFW QCEW weekly wage (all of 237310, wage bill, no hours) against the national
    NAICS 2373 weekly earnings from CES (hours-adjusted, same industry).

    The ratio is a DFW-vs-nation premium on comparable WEEKLY money. Its change
    over time is the part of DFW growth that is not explained by the national
    industry's own hourly-earnings and hours path. It is a diagnostic, not an
    identified decomposition: QCEW includes bonuses and all occupations, CES
    production-and-nonsupervisory concepts differ, and neither controls mix.
    """
    if not QCEW_INDEX.exists():
        stats["premium_skipped"] = "qcew index not found"
        return []
    qcew = {}
    with QCEW_INDEX.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            # qcew_dfw_highway_wage_index.csv: 'wkly_wage' is the preferred series
            # (MSA published where available, county-weighted bridge otherwise).
            v = num(r.get("wkly_wage"))
            if v is not None:
                qcew[int(r["year"])] = (v, r.get("series_basis", ""))

    ces_wk = {r["year"]: num(r["naics2373_weekly_earnings"]) for r in ces_annual}
    rows = []
    base = None
    for y in sorted(qcew):
        dfw, basis = qcew[y]
        nat = ces_wk.get(y)
        if not nat:
            continue
        ratio = dfw / nat
        if base is None:
            base = ratio
            base_year = y
        rows.append({
            "year": y,
            "dfw_qcew_237310_weekly_wage": fmt(dfw),
            "dfw_series_basis": basis,
            "us_ces_2373_weekly_earnings": fmt(nat),
            "dfw_over_us_ratio": fmt(ratio, 4),
            "dfw_premium_pct": fmt(100.0 * (ratio - 1.0), 2),
            "ratio_index_first_year_100": fmt(100.0 * ratio / base, 2),
        })
    if rows:
        stats["premium_years"] = len(rows)
        stats["premium_base_year"] = base_year
    return rows


def parse_availability(oews_rows, series, stats, ces_asked=None):
    """One row per series that produced no usable value.

    A series with no data has two very different reasons, and collapsing them is
    how a temporary outage gets written down as a permanent fact. "Series does not
    exist" may only be said of a series BLS actually answered about. A series that
    no successful response even covers was never asked -- here, because the daily
    query limit refused the request -- and saying it does not exist would tell the
    next reader to stop trying."""
    rows = []
    for r in oews_rows:
        if r["available"] == "0":
            rows.append({
                "programme": "OEWS",
                "series_id": r["series_id"],
                "what": f'{r["area_key"]} {r["soc_code"]} {r["measure"]}',
                "description": r["soc_title"],
                "status": "not published for this area/occupation",
            })
    asked = ces_asked if ces_asked is not None else set(series)
    for sid, title in sorted(CES_SERIES.items()):
        if not series.get(sid):
            if sid in asked:
                status = "BLS answered but published no data for this series"
            else:
                status = ("NOT FETCHED - no successful response covers it. The request "
                          "was refused by the api.bls.gov daily query limit, NOT by BLS "
                          "lacking the series. Re-run 'fetch-ceu' after the limit resets")
            rows.append({
                "programme": "CES",
                "series_id": sid,
                "what": "monthly series",
                "description": title,
                "status": status,
            })
    stats["unavailable_series"] = len(rows)
    return rows


def main_parse():
    stats = {
        "oews_missing": 0, "ces_missing": 0, "mix_trades_dropped": 0,
        "oews_raw_files": 0, "ces_raw_files": 0,
    }
    oews_series, _, oews_files = load_raw(OEWS_RAW)
    ces_series, ces_asked, ces_files = load_raw(CES_RAW)
    stats["oews_raw_files"] = len(oews_files)
    stats["ces_raw_files"] = len(ces_files)
    if not oews_files or not ces_files:
        sys.exit("no raw files; run: python src/parse_bls_oews.py fetch")

    oews_rows, values = parse_oews(oews_series, stats)
    wide = parse_oews_wide(values, stats)
    mix = parse_mix_decomposition(values, stats)
    ces_rows, annual, monthly = parse_ces(ces_series, stats)
    ces_annual = parse_ces_annual(annual, monthly, stats)
    premium = parse_premium(ces_annual, stats)
    adjval = build_adjustment_validation(ces_annual, stats)
    avail = parse_availability(oews_rows, ces_series, stats, ces_asked)

    written = {}
    written["oews_dfw_trades_2025.csv"] = write_csv(
        OUT / "oews_dfw_trades_2025.csv", oews_rows,
        ["reference_period", "area_key", "area_code", "area_name", "soc_code",
         "soc_title", "measure", "value", "available", "series_id"])
    written["oews_dfw_vs_tx_us_2025.csv"] = write_csv(
        OUT / "oews_dfw_vs_tx_us_2025.csv", wide,
        ["soc_code", "soc_title", "in_highway_basket", "dfw_employment",
         "dfw_share_of_soc47_pct", "dfw_hourly_mean", "dfw_hourly_median",
         "tx_hourly_mean", "us_hourly_mean", "dfw_vs_tx_pct", "dfw_vs_us_pct",
         "dfw_annual_at_2080h"])
    written["oews_mix_decomposition.csv"] = write_csv(
        OUT / "oews_mix_decomposition.csv", mix,
        ["basket", "trades_in_basket", "trades_dropped_unpublished",
         "basket_dfw_employment", "basket_share_of_dfw_soc47_pct",
         "dfw_wage_at_dfw_mix", "us_wage_at_dfw_mix", "tx_wage_at_dfw_mix",
         "dfw_minus_us_same_mix_usd", "dfw_minus_us_same_mix_pct",
         "dfw_minus_tx_same_mix_pct", "note"])
    written["ces_hourly_earnings_monthly.csv"] = write_csv(
        OUT / "ces_hourly_earnings_monthly.csv", ces_rows,
        ["series_id", "series_title", "date", "year", "month", "value", "preliminary"])
    written["ces_naics2373_annual.csv"] = write_csv(
        OUT / "ces_naics2373_annual.csv", ces_annual,
        ["year", "naics2373_hourly_earnings", "basis", "months_in_average",
         "partial_year", "naics2373_weekly_hours", "naics2373_weekly_earnings",
         "weekly_vs_ahe_x_hours_pct", "naics2373_employment_thousands",
         "naics237_hourly_earnings",
         "all_construction_hourly_earnings", "index_2016_100", "yoy_pct"])
    written["qcew_vs_ces_premium.csv"] = write_csv(
        OUT / "qcew_vs_ces_premium.csv", premium,
        ["year", "dfw_qcew_237310_weekly_wage", "dfw_series_basis",
         "us_ces_2373_weekly_earnings", "dfw_over_us_ratio", "dfw_premium_pct",
         "ratio_index_first_year_100"])
    written["ces_adjustment_validation.csv"] = write_csv(
        OUT / "ces_adjustment_validation.csv", adjval,
        ["year", "ces_weekly_earnings", "ces_seasonal_adjustment", "ces_basis",
         "ces_months_in_average", "ces_partial_year", "qcew_weekly_wage",
         "qcew_seasonal_adjustment", "qcew_series", "ces_yoy_pct",
         "qcew_yoy_pct", "yoy_gap_pct_points", "gap_contains"])
    written["availability_report.csv"] = write_csv(
        OUT / "availability_report.csv", avail,
        ["programme", "series_id", "what", "description", "status"])

    stats["files_written"] = written
    stats["parsed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (OUT / "parse_summary.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    print("wrote:")
    for k, v in written.items():
        print(f"  {k:34s} {v:6d} rows")
    print(f"OEWS series available {stats['oews_available']}/{stats['oews_rows']}"
          f"  (missing {stats['oews_missing']})")
    print(f"CES monthly rows {stats['ces_rows']} across "
          f"{stats['ces_series_with_data']}/{len(CES_SERIES)} series")
    print(f"unavailable series logged: {stats['unavailable_series']}")
    av = stats.get("adjustment_validation")
    if isinstance(av, dict):
        print("adjusted-vs-unadjusted annual trend check "
              f"({av['window']}, benchmark = QCEW census):")
        print(f"  CES(adjusted) {av['ces_total_growth_pct']:+.2f}%   "
              f"QCEW(unadjusted census) {av['qcew_total_growth_pct']:+.2f}%   "
              f"gap {av['total_growth_gap_pct_points']:+.2f} pts")
        print(f"  yoy gap over {av['yoy_gap_years']} years: mean "
              f"{av['yoy_gap_mean_pct_points']:+.3f} pts, max abs "
              f"{av['yoy_gap_max_abs_pct_points']:.2f} pts")
        print("  that gap also contains the QCEW bonus term and census-vs-survey, "
              "so it is an UPPER bound on adjustment alone")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "parse"
    if mode == "fetch":
        fetch()
    elif mode == "fetch-ceu":
        fetch_ceu()
    elif mode == "parse":
        main_parse()
    else:
        sys.exit("usage: python src/parse_bls_oews.py [fetch|fetch-ceu|parse]")
