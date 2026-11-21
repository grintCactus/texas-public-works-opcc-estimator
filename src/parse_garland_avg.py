"""
Parse Garland's "Average Bid Price" reports.

These are not bid tabs: under each bid item the report lists ONE contract price
per project (quantity, unit cost, total cost, project name, date), then a TOTAL
row and two averages. The per-project detail lines are what matter here -- each
is a dated contract unit price, so parsing the detail rather than the published
average gives raw observations to pool with robust statistics, plus a real time
dimension (2023 report = 2023 dates, 2024 report = 2024 dates, no overlap).

Caveats carried from the source:
  * the two averages are quantity-weighted (total money / total quantity), NOT a
    mean of unit prices, and they are built from contract prices -- so they
    already embody the competitive outcome and must never be discounted again;
  * dates are written both 12/19/2024 and 2024/09/12 in the same report;
  * the TOTAL money is rounded (33,000+3,900+8,000+11,000+15,968 = 71,868 is
    printed as 71,867.50), so the detail lines are the trustworthy figures;
  * each item number is printed twice (once opening the block, once closing it).

Outputs
  data/clean/garland_item_prices.csv   one row per (item x project) observation
  data/clean/garland_item_totals.csv   the item-level published aggregates
"""
import os
import re
import sys

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_bidtabs import (norm_unit, norm_desc, item_family, extract_size_in,
                           MATERIAL_RE, KNOWN_UNITS, num, txt)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GDIR = os.path.join(ROOT, "data", "external", "garland")
OUT = os.path.join(ROOT, "data", "clean")

ITEM_RE = re.compile(r"^(\d{3}\.\d{4})\s*(.*)$")
DETAIL_RE = re.compile(
    r"^([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s+(.*?)\s+"
    r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}/\d{1,2}/\d{1,2})\s*$")
TOTAL_RE = re.compile(
    r"^TOTAL:\s+(\d+)\s+([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s*$")
AVG_VALUES_RE = re.compile(r"^\$([\d,]+(?:\.\d+)?)\s+\$([\d,]+(?:\.\d+)?)\s*$")
SKIP_RE = re.compile(r"^(City Of Garland|Average Bid Price|ITEM NO DESCRIPTION|"
                     r"AVERAGE COST|PER QUANTITY|PER PROJECT)", re.I)
# "Tuesday, January 7, 2025 Page 2 of 91" prints inside the description flow and
# ends up glued onto whatever item is open when the page breaks.
FOOTER_RE = re.compile(
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+\w+\s+\d{1,2},?\s+\d{4}"
    r"|page\s+\d+\s+of\s+\d+", re.I)


def iso_date(s):
    s = s.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return ""


def strip_unit_token(desc, unit):
    """The UNIT column value is interleaved into the description text flow, e.g.
    "TREES EA GREATER THAN 24 IN ... REMOVE & DISPOSE". Left in place it pushes
    the verb away from its subject and defeats item classification, so remove
    the standalone unit token."""
    if not unit:
        return desc
    return re.sub(r"(?<!\w)%s(?!\w)" % re.escape(unit), " ", desc, count=1).strip()


def find_unit(words):
    """The UNIT column value lands among the description words; pick the last
    token that is a recognised unit of measure."""
    for w in reversed(words):
        t = w.strip().upper().strip(".")
        if t and norm_unit(t) in KNOWN_UNITS and len(t) <= 5:
            return t
    return ""


def parse_report(path, report_year):
    obs, totals = [], []
    item_no, desc_parts, unit = None, [], ""
    pending = None            # last detail row, for wrapped project names

    def flush_item():
        return

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for raw in (page.extract_text() or "").split("\n"):
                line = txt(raw)
                if not line or SKIP_RE.match(line):
                    continue
                line = FOOTER_RE.sub(" ", line).strip()
                if not line:
                    continue

                m = TOTAL_RE.match(line)
                if m and item_no:
                    totals.append({
                        "src_file": os.path.basename(path),
                        "report_year": report_year,
                        "item_no": item_no,
                        "description": " ".join(desc_parts).strip(),
                        "unit_raw": unit,
                        "unit": norm_unit(unit),
                        "n_projects": int(m.group(1)),
                        "total_quantity": num(m.group(2)),
                        "sum_of_unit_costs": num(m.group(3)),
                        "total_amount": num(m.group(4)),
                    })
                    pending = None
                    continue

                if AVG_VALUES_RE.match(line):
                    v = AVG_VALUES_RE.match(line)
                    if totals:
                        totals[-1]["avg_cost_per_quantity"] = num(v.group(1))
                        totals[-1]["avg_cost_per_project"] = num(v.group(2))
                    continue

                m = DETAIL_RE.match(line)
                if m and item_no:
                    q, up, tc = num(m.group(1)), num(m.group(2)), num(m.group(3))
                    rec = {
                        "src_file": os.path.basename(path),
                        "report_year": report_year,
                        "item_no": item_no,
                        "description": " ".join(desc_parts).strip(),
                        "unit_raw": unit,
                        "unit": norm_unit(unit),
                        "quantity": q,
                        "unit_price": up,
                        "extended_reported": tc,
                        "project_name": m.group(4).strip(),
                        "bid_date": iso_date(m.group(5)),
                    }
                    obs.append(rec)
                    pending = rec
                    continue

                m = ITEM_RE.match(line)
                if m:
                    # An item number opens a block and is printed again to close
                    # it; only the opening one resets the description.
                    new_item = m.group(1)
                    rest = m.group(2).strip()
                    if new_item != item_no:
                        item_no = new_item
                        desc_parts = [rest] if rest else []
                        unit = find_unit(rest.split())
                    pending = None
                    continue

                if pending is not None:
                    # wrapped project name
                    pending["project_name"] = (pending["project_name"] + " " + line).strip()
                elif item_no:
                    desc_parts.append(line)
                    if not unit:
                        unit = find_unit(line.split())
    return obs, totals


def main():
    import csv
    files = []
    for f in sorted(os.listdir(GDIR)):
        m = re.search(r"Average_Bid_Price_(\d{4})", f, re.I)
        if m and f.lower().endswith(".pdf"):
            files.append((os.path.join(GDIR, f), m.group(1)))
    if not files:
        print("no Garland Average Bid Price reports found")
        return

    all_obs, all_tot = [], []
    for path, yr in files:
        obs, tot = parse_report(path, yr)
        print("  %-44s %4d detail lines  %4d items" % (os.path.basename(path), len(obs), len(tot)))
        all_obs.extend(obs)
        all_tot.extend(tot)

    for r in all_obs:
        r["description"] = strip_unit_token(r["description"], r.get("unit_raw", ""))
        dn = norm_desc(r["description"])
        r["desc_norm"] = dn
        r["item_family"] = item_family(dn)
        r["size_in"] = extract_size_in(dn)
        mat = MATERIAL_RE.search(dn)
        r["material"] = mat.group(1).upper() if mat else ""
        q, up, tc = r["quantity"], r["unit_price"], r["extended_reported"]
        r["extended_calc"] = (q * up) if (q is not None and up is not None) else None
        r["ext_mismatch"] = ""
        if r["extended_calc"] is not None and tc:
            if abs(r["extended_calc"] - tc) / max(abs(tc), 1.0) > 0.01:
                r["ext_mismatch"] = "Y"

    ocols = ["src_file", "report_year", "item_no", "description", "desc_norm",
             "item_family", "size_in", "material", "unit_raw", "unit", "quantity",
             "unit_price", "extended_reported", "extended_calc", "ext_mismatch",
             "project_name", "bid_date"]
    with open(os.path.join(OUT, "garland_item_prices.csv"), "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=ocols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_obs)

    tcols = ["src_file", "report_year", "item_no", "description", "unit_raw", "unit",
             "n_projects", "total_quantity", "sum_of_unit_costs", "total_amount",
             "avg_cost_per_quantity", "avg_cost_per_project"]
    with open(os.path.join(OUT, "garland_item_totals.csv"), "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=tcols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_tot)

    bad = sum(1 for r in all_obs if r["ext_mismatch"] == "Y")
    years = sorted(set(r["bid_date"][:4] for r in all_obs if r["bid_date"]))
    nodate = sum(1 for r in all_obs if not r["bid_date"])
    nounit = sum(1 for r in all_obs if not r["unit"])
    print("\nDetail lines in total %d | items %d | years %s" % (len(all_obs), len(all_tot), years))
    print("Arithmetic mismatches %d (%.1f%%) | no date %d | no unit %d"
          % (bad, bad / max(len(all_obs), 1) * 100, nodate, nounit))

    # verify the published quantity-weighted average against the detail lines
    from collections import defaultdict
    agg = defaultdict(lambda: [0.0, 0.0])
    for r in all_obs:
        if r["quantity"] and r["unit_price"]:
            k = (r["src_file"], r["item_no"])
            agg[k][0] += r["quantity"] * r["unit_price"]
            agg[k][1] += r["quantity"]
    ok = off = 0
    for t in all_tot:
        k = (t["src_file"], t["item_no"])
        pub = t.get("avg_cost_per_quantity")
        if k in agg and agg[k][1] and pub:
            mine = agg[k][0] / agg[k][1]
            if abs(mine - pub) / max(pub, 1) <= 0.02:
                ok += 1
            else:
                off += 1
    print("Quantity-weighted average price recomputed: match %d / no match %d  (check basis = sum of amounts / sum of quantities)" % (ok, off))
    print("\n-> data/clean/garland_item_prices.csv, garland_item_totals.csv")


if __name__ == "__main__":
    main()
