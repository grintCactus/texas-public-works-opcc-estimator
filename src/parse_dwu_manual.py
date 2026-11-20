"""
Parse the Dallas Water Utilities "Average Costs Manual for Pipeline
Construction Cost Estimation" (Sept 2021).

This is a published price book, not a bid tab: one row per DWU item code with
mean, standard deviation and observation count. That makes it unusually
informative -- most sources give no dispersion at all -- but its PRICE BASIS is
different again, and mixing bases silently is the main way a price reference
goes wrong:

  TxDOT dashboard / Garland report : low-bid or contract price (competitive
                                     outcome already baked in)
  DWU manual                       : MEAN of competitive bids, after trimming
                                     anything outside cumulative probability
                                     0.1-0.9 (the manual states this method)

A trimmed mean of all bids sits ABOVE the low bid, so a DWU number used as a
low-bid predictor needs the competition discount applied, whereas a TxDOT number
does not. The `price_basis` column carries that distinction downstream.

Output
  data/clean/dwu_price_manual.csv
"""
import csv
import os
import re
import sys

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_bidtabs import (norm_unit, norm_desc, item_family, extract_size_in,
                           MATERIAL_RE, KNOWN_UNITS, num, txt)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "external", "dallas_wu",
                   "DWU_Average_Cost_Manual_2021.pdf")
OUT = os.path.join(ROOT, "data", "clean")

UNIT_ALT = "|".join(sorted(KNOWN_UNITS, key=len, reverse=True))
ROW_RE = re.compile(
    r"^([0-9][0-9A-Za-z]*(?:x[0-9A-Za-z]+)?)\s+"      # item code, e.g. 3100J, 5064HxH
    r"(.+?)\s+"                                        # description
    r"(" + UNIT_ALT + r")\s+"                          # unit
    r"\$([\d,]+(?:\.\d{2})?)\s+"                       # mean
    r"\$([\d,]+(?:\.\d{2})?)\s+"                       # std dev
    r"(\d+)\s*$",                                      # count
    re.I)


def main():
    if not os.path.exists(SRC):
        print("missing:", SRC)
        return
    rows, seen = [], set()
    with pdfplumber.open(SRC) as pdf:
        for pi, page in enumerate(pdf.pages):
            for raw in (page.extract_text() or "").split("\n"):
                line = txt(raw)
                m = ROW_RE.match(line)
                if not m:
                    continue
                code = m.group(1)
                if code in seen:
                    continue
                seen.add(code)
                desc = m.group(2).strip()
                unit_raw = m.group(3)
                dn = norm_desc(desc)
                mat = MATERIAL_RE.search(dn)
                mean = num(m.group(4))
                sd = num(m.group(5))
                rows.append({
                    "src_file": os.path.basename(SRC),
                    "owner": "dallas_water_utilities",
                    "manual_year": "2021",
                    "price_basis": "trimmed_mean_all_bids",
                    "item_no": code,
                    "description": desc,
                    "desc_norm": dn,
                    "item_family": item_family(dn),
                    "size_in": extract_size_in(dn),
                    "material": (mat.group(1).upper() if mat else ""),
                    "unit_raw": unit_raw,
                    "unit": norm_unit(unit_raw),
                    "mean_unit_price": mean,
                    "sd_unit_price": sd,
                    "n_observations": int(m.group(6)),
                    "cv": (sd / mean) if (mean and sd is not None) else None,
                    "page": pi + 1,
                })

    cols = ["src_file", "owner", "manual_year", "price_basis", "item_no", "description",
            "desc_norm", "item_family", "size_in", "material", "unit_raw", "unit",
            "mean_unit_price", "sd_unit_price", "n_observations", "cv", "page"]
    with open(os.path.join(OUT, "dwu_price_manual.csv"), "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    import collections
    fam = collections.Counter(r["item_family"] for r in rows)
    thin = sum(1 for r in rows if r["n_observations"] < 3)
    print("解析出 %d 个 item" % len(rows))
    print("样本数 <3 的 item: %d (%.0f%%) —— 这些均值不可靠"
          % (thin, thin / max(len(rows), 1) * 100))
    print("样本数中位数: %d" % sorted(r["n_observations"] for r in rows)[len(rows) // 2])
    cvs = sorted(r["cv"] for r in rows if r["cv"] is not None)
    if cvs:
        print("变异系数中位数: %.2f" % cvs[len(cvs) // 2])
    print("\n前 10 族:")
    for k, v in fam.most_common(10):
        print("  %-20s %d" % (k, v))
    print("\n-> data/clean/dwu_price_manual.csv")


if __name__ == "__main__":
    main()
