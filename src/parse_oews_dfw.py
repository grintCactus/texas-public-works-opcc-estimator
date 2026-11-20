"""Extract DFW construction-trade wages (SOC 47-*) from the OEWS MSA files, 2016-2025.

Source
------
BLS Occupational Employment and Wage Statistics, "Metropolitan and Nonmetropolitan
Area Occupational Employment and Wage Estimates", one archive per reference year:
https://www.bls.gov/oes/special-requests/oesm{yy}ma.zip
Downloaded 2026-11-18 into data/external/oews/raw/ (10 archives, 2016-2025).

These files are NOT reachable with a scripted fetch -- bls.gov returns 403 to curl.
They were retrieved through a real browser. The BLS time-series API is no substitute:
it serves only the latest reference period (May 2025); every earlier year returns
"No Data Available".

Geography: a trap worth knowing
-------------------------------
The OEWS MSA file does not carry the combined Dallas-Fort Worth-Arlington MSA
(19100). It publishes the two **metropolitan divisions** separately:

    19124  Dallas-Plano-Irving, TX Metropolitan Division
    23104  Fort Worth-Arlington, TX Metropolitan Division

(titles and codes are re-checked every year by this script, because OMB
re-delineations rename and renumber divisions -- 2023 is a known break point).

So "DFW" here is the two divisions combined. Employment adds exactly. **Medians do
not.** The combined hourly median in the output is an employment-weighted mean of
the two divisional medians -- an approximation, flagged as such in the column name
(`h_median_wavg_approx`). Use the per-division columns when exactness matters.

Suppression
-----------
OEWS marks cells: `*` not released, `**` not available, `#` wage >= $100/hr
(or >= $208,000/yr). These are blanked and counted, never coerced to zero.

Outputs (data/external/oews/clean/)
-----------------------------------
  oews_dfw_construction.csv   year x division x occupation, as published
  oews_dfw_combined.csv       year x occupation, two divisions combined
  oews_suppression_report.csv every suppressed cell kept
  parse_summary.json          codes used per year, row counts, caveats
"""
from __future__ import annotations

import csv
import json
import os
import re
import zipfile
from datetime import date
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "external" / "oews" / "raw"
OUT = ROOT / "data" / "external" / "oews" / "clean"
WORK = Path(os.environ.get("OEWS_WORK", ROOT / "data" / "external" / "oews" / "_work"))

SUPPRESSED = {"*": "not_released", "**": "not_available", "#": "wage_at_or_above_cap"}

# Column names drift across years; map everything to one vocabulary.
ALIASES = {
    "area": ("AREA",),
    "area_name": ("AREA_NAME", "AREA_TITLE"),
    "prim_state": ("PRIM_STATE",),
    "occ_code": ("OCC_CODE",),
    "occ_title": ("OCC_TITLE",),
    "occ_group": ("OCC_GROUP", "O_GROUP"),
    "tot_emp": ("TOT_EMP",),
    "h_mean": ("H_MEAN",),
    "h_median": ("H_MEDIAN",),
    "h_pct25": ("H_PCT25",),
    "h_pct75": ("H_PCT75",),
}


def num(v):
    """OEWS numerics arrive as str or float, with suppression markers mixed in."""
    if v is None:
        return None, None
    s = str(v).strip()
    if s in SUPPRESSED:
        return None, SUPPRESSED[s]
    s = s.replace(",", "").replace("$", "")
    if not s or s in ("-",):
        return None, None
    try:
        return float(s), None
    except ValueError:
        return None, f"unparsed:{s[:20]}"


def resolve(header: list[str]) -> dict[str, int]:
    up = {h.strip().upper(): i for i, h in enumerate(header) if h}
    out = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in up:
                out[key] = up[n]
                break
    missing = [k for k in ("area", "area_name", "occ_code", "tot_emp") if k not in out]
    if missing:
        raise SystemExit(f"OEWS layout changed, missing {missing}; header was {header[:12]}")
    return out


def dfw_rows(xlsx: Path, year: int):
    """Yield SOC 47-* rows for the Texas Dallas / Fort Worth divisions."""
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        idx = resolve([str(h) if h is not None else "" for h in next(it)])
        areas, out, supp = {}, [], []
        for r in it:
            name = str(r[idx["area_name"]] or "")
            state = str(r[idx["prim_state"]] or "") if "prim_state" in idx else ""
            # Texas Dallas / Fort Worth divisions only -- guard against
            # Arlington VA and Arlington Heights IL, which also match on name.
            if not (("Dallas" in name or "Fort Worth" in name) and ("TX" in name or state == "TX")):
                continue
            occ = str(r[idx["occ_code"]] or "").strip()
            if not (occ.startswith("47-") or occ == "00-0000"):
                continue
            area = str(r[idx["area"]] or "").strip().lstrip("0") or "0"
            areas[area] = name
            rec = {
                "year": year, "area": area, "area_name": name,
                "occ_code": occ,
                "occ_title": str(r[idx["occ_title"]] or "").strip() if "occ_title" in idx else "",
                "occ_group": str(r[idx["occ_group"]] or "").strip() if "occ_group" in idx else "",
            }
            for f in ("tot_emp", "h_mean", "h_median", "h_pct25", "h_pct75"):
                v, flag = num(r[idx[f]]) if f in idx else (None, None)
                rec[f] = v
                if flag:
                    supp.append({"year": year, "area": area, "occ_code": occ,
                                 "field": f, "reason": flag})
            out.append(rec)
        return out, areas, supp
    finally:
        wb.close()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    rows, supp, per_year = [], [], {}

    for zp in sorted(RAW.glob("oesm*ma.zip")):
        yy = re.search(r"oesm(\d\d)ma", zp.name).group(1)
        year = 2000 + int(yy)
        with zipfile.ZipFile(zp) as z:
            member = next(n for n in z.namelist()
                          if re.search(r"/MSA_M\d{4}_dl\.xlsx$", n))
            xlsx = Path(z.extract(member, WORK))
        r, areas, s = dfw_rows(xlsx, year)
        rows.extend(r)
        supp.extend(s)
        per_year[year] = {"areas": areas, "rows": len(r), "suppressed_cells": len(s)}
        print(f"{year}: {len(r):>4} rows  areas={sorted(areas)}  suppressed={len(s)}")
        xlsx.unlink(missing_ok=True)

    rows.sort(key=lambda x: (x["year"], x["area"], x["occ_code"]))
    fields = ["year", "area", "area_name", "occ_code", "occ_title", "occ_group",
              "tot_emp", "h_mean", "h_median", "h_pct25", "h_pct75"]
    with (OUT / "oews_dfw_construction.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # combine the two divisions: employment sums exactly, medians do not
    comb = {}
    for r in rows:
        k = (r["year"], r["occ_code"])
        c = comb.setdefault(k, {"year": r["year"], "occ_code": r["occ_code"],
                                "occ_title": r["occ_title"], "tot_emp": 0.0,
                                "_wm": 0.0, "_wmean": 0.0, "_w": 0.0,
                                "divisions": 0, "divisions_with_median": 0})
        c["divisions"] += 1
        if r["tot_emp"]:
            c["tot_emp"] += r["tot_emp"]
            if r["h_median"]:
                c["_wm"] += r["h_median"] * r["tot_emp"]
                c["_w"] += r["tot_emp"]
                c["divisions_with_median"] += 1
            if r["h_mean"]:
                c["_wmean"] += r["h_mean"] * r["tot_emp"]
    out2 = []
    for c in comb.values():
        out2.append({
            "year": c["year"], "occ_code": c["occ_code"], "occ_title": c["occ_title"],
            "tot_emp": round(c["tot_emp"]) if c["tot_emp"] else None,
            "h_median_wavg_approx": round(c["_wm"] / c["_w"], 2) if c["_w"] else None,
            "h_mean_wavg": round(c["_wmean"] / c["tot_emp"], 2) if c["tot_emp"] else None,
            "divisions": c["divisions"],
            "divisions_with_median": c["divisions_with_median"],
        })
    out2.sort(key=lambda x: (x["year"], x["occ_code"]))
    with (OUT / "oews_dfw_combined.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out2[0]))
        w.writeheader()
        w.writerows(out2)

    with (OUT / "oews_suppression_report.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["year", "area", "occ_code", "field", "reason"])
        w.writeheader()
        w.writerows(supp)

    (OUT / "parse_summary.json").write_text(json.dumps({
        "built_on": date.today().isoformat(),
        "source": "BLS OEWS MSA files oesm{yy}ma.zip, 2016-2025",
        "retrieved": "2026-11-18 via browser (bls.gov returns 403 to scripted fetch)",
        "geography": "Dallas and Fort Worth TX metropolitan divisions, combined",
        "rows": len(rows), "suppressed_cells": len(supp),
        "per_year": {str(k): v for k, v in per_year.items()},
        "caveat_median": "h_median_wavg_approx is an employment-weighted mean of two "
                         "divisional medians, not a true combined median.",
    }, indent=2), encoding="utf-8")
    print(f"\nrows={len(rows)}  suppressed_cells={len(supp)}  -> {OUT}")


if __name__ == "__main__":
    main()
