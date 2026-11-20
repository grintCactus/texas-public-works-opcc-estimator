"""Export the schedule HEADER line of each file, as this side's extractor reads it.

Why only the header line
------------------------
The parsing side cannot get rows out of ~116 files that this index says carry a
line-item schedule. Three causes are diagnosed and together explain about a
dozen of them; for the rest, the quantity and unit words ARE present and the
parse still yields nothing, and the header is the one piece of evidence they
are missing.

They declined an offer of this side's full extracted text as a fallback source,
and the reason they gave is the right one: `nrhtx/2024-11-14_6222` comes out of
`pdftotext` here with the wrong unit and with figures welded in from an
adjacent row. Feeding a parser material that is known to misalign silently
would trade a visible failure ("no rows") for an invisible one ("wrong rows"),
and the expensive error is the silent one.

A header line cannot do that damage: it carries no quantity, no unit price and
no extended amount, so nothing downstream can price from it. If it is wrong,
the error stays in a diagnostic column.

What a row contains
-------------------
    header_line     the first line matching the schedule-header pattern
    header_prev     the line above it (column groups are often split in two)
    header_next     the line below it (continuation of the same header)
    first_priced    the first line that looks like a priced row, for contrast
    n_header_tokens how many of ITEM/DESC/QTY/UNIT/PRICE/TOTAL were found

`n_header_tokens` is the part to sort by: a file whose header has all six and
still parses to nothing is a different problem from one where the header never
resolved.

Usage:
    python src/export_header_lines.py                  # files with no parsed rows
    python src/export_header_lines.py --all            # every usable file
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_external import PRICED_ROW, read_pdf_text, read_xlsx_text  # noqa: E402
from index_external import SCHEDULE_HDR                               # noqa: E402

ROOT = Path("data/external")
INDEX = ROOT / "INDEX.csv"
LINE_ITEMS = Path("data/clean/line_items.csv")
OUT = ROOT / "_header_lines.csv"

USABLE = {"line_item", "likely_line_item", "unit_price_table",
          "unit_price_dataset"}

TOKENS = {
    "ITEM": re.compile(r"(?i)\bitem\b|\bno\.?\b|\b#"),
    "DESC": re.compile(r"(?i)descri|\bitems?\b"),
    "QTY": re.compile(r"(?i)\bqty\b|quantit|\best\.?\s*q"),
    "UNIT": re.compile(r"(?i)\bunit\b|\buom\b|measure"),
    "PRICE": re.compile(r"(?i)unit\s*(price|cost)|\bprice\b|\brate\b"),
    "TOTAL": re.compile(r"(?i)\btotal\b|extend|\bamount\b"),
}


# Why a file produces no parsed rows. The point of computing this here is that
# a raw count of "files with no rows" is NOT a count of failures: a file the
# de-duplicator correctly dropped has no rows, an award notice has no rows, and
# a blank unit-price form has no rows. Every one of those is the pipeline
# working. The parsing side reported 116 files / 4,600 rows missing and, after
# subtracting these, had 70 / 3,043 -- the largest single deduction being 29
# verified duplicates, i.e. evidence their de-duplication works.
#
# A failure count has to have the correct outcomes subtracted before it is a
# failure count, so the subtraction belongs in the export rather than in
# whoever reads it.
NOT_A_TABULATION = {"award_recommendation", "council_minutes",
                    "service_contract", "materials_supply"}
OWN_LOADER = re.compile(
    r"(?i)garland_average_bid_price|dwu_average_cost|sanantonio|"
    r"txdot_bidtabs_|_average_bid_price|price_manual|hci_")
BLANK_FORM = re.compile(r"\$\s*-(?!\d)")


def exclusion_reason(row: dict, prof: dict, dup_twin: dict) -> str:
    rel = row["file"]
    if rel in dup_twin:
        return "verified_duplicate"
    if row.get("project_kind") in NOT_A_TABULATION:
        return "not_a_tabulation:" + row["project_kind"]
    if OWN_LOADER.search(rel):
        return "own_loader"
    if row["scope_2020plus"] == "reference":
        return "reference_not_a_bid"
    fp = prof.get("first_priced") or ""
    if fp and len(BLANK_FORM.findall(fp)) >= 2:
        return "blank_bid_form"
    return "true_residual"


def text_of(p: Path) -> str:
    if p.suffix.lower() == ".pdf":
        return read_pdf_text(p)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return read_xlsx_text(p)
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:200000]
    except Exception:                                             # noqa: BLE001
        return ""


def squash(s: str, n: int = 300) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def profile(text: str) -> dict:
    lines = text.splitlines()
    hdr_i = None
    for i, ln in enumerate(lines):
        if SCHEDULE_HDR.search(ln):
            hdr_i = i
            break
    first_priced = ""
    for ln in lines:
        if PRICED_ROW.search(ln):
            first_priced = ln
            break
    if hdr_i is None:
        return {"header_line": "", "header_prev": "", "header_next": "",
                "n_header_tokens": 0, "header_tokens": "",
                "first_priced": squash(first_priced)}
    window = " ".join(lines[max(0, hdr_i - 1):hdr_i + 2])
    found = [k for k, pat in TOKENS.items() if pat.search(window)]
    return {
        "header_line": squash(lines[hdr_i]),
        "header_prev": squash(lines[hdr_i - 1]) if hdr_i else "",
        "header_next": squash(lines[hdr_i + 1]) if hdr_i + 1 < len(lines) else "",
        "n_header_tokens": len(found),
        "header_tokens": ",".join(sorted(found)),
        "first_priced": squash(first_priced),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="every usable file, not only those with no parsed rows")
    args = ap.parse_args()

    rows = list(csv.DictReader(INDEX.open(encoding="utf-8-sig")))
    parsed: set[str] = set()
    if LINE_ITEMS.exists():
        csv.field_size_limit(10 ** 7)
        for r in csv.DictReader(LINE_ITEMS.open(encoding="utf-8-sig")):
            src = (r.get("src_file") or "").replace("\\", "/")
            if src.startswith("external/"):
                src = src[len("external/"):]
            if src:
                parsed.add(src)
    else:
        print("note: line_items.csv missing; falling back to --all")
        args.all = True

    dup_twin: dict[str, str] = {}
    dl = Path("data/clean/duplicate_lettings.csv")
    if dl.exists():
        for r in csv.DictReader(dl.open(encoding="utf-8-sig")):
            d = (r.get("dropped") or "").split("::")[0].replace(chr(92), "/")
            k = (r.get("kept") or "").split("::")[0].replace(chr(92), "/")
            if d.startswith("external/"):
                d = d[len("external/"):]
            if k.startswith("external/"):
                k = k[len("external/"):]
            if d and k and d != k:
                dup_twin.setdefault(d, k)
        print("duplicate_lettings.csv: %d dropped files have a twin"
              % len(dup_twin))

    targets = [r for r in rows
               if r["classification"] in USABLE
               and r["scope_2020plus"] in ("in_scope", "reference")
               and (args.all or r["file"] not in parsed)]
    print("profiling %d files (%s)"
          % (len(targets), "all usable" if args.all else "no parsed rows"))

    out_rows = []
    for i, r in enumerate(targets, 1):
        p = ROOT / r["file"]
        if not p.exists():
            continue
        try:
            prof = profile(text_of(p))
        except Exception as exc:                                  # noqa: BLE001
            print("  ERR %s: %s" % (r["file"], exc))
            continue
        prof["exclusion_reason"] = exclusion_reason(r, prof, dup_twin)
        prof["duplicate_of"] = dup_twin.get(r["file"], "")
        prof.update({"file": r["file"], "owner": r["owner"],
                     "work_type": r["work_type"],
                     "classification": r["classification"],
                     "n_priced_rows": r["n_priced_rows"],
                     "text_source": r.get("text_source", ""),
                     "text_quality": r.get("text_quality", "")})
        out_rows.append(prof)
        if i % 40 == 0:
            print("  %d/%d" % (i, len(targets)), flush=True)

    cols = ["file", "owner", "work_type", "classification", "n_priced_rows",
            "exclusion_reason", "duplicate_of", "text_source", "text_quality",
            "n_header_tokens", "header_tokens", "header_prev", "header_line",
            "header_next", "first_priced"]
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(out_rows, key=lambda r: (-r["n_header_tokens"],
                                                    r["file"])))

    import collections
    print("\n=== why each file has no parsed rows ===")
    by = collections.Counter(r["exclusion_reason"] for r in out_rows)
    for k, v in by.most_common():
        print("  %-36s %3d" % (k, v))
    resid = [r for r in out_rows if r["exclusion_reason"] == "true_residual"]
    pr = sum(int(r["n_priced_rows"] or 0) for r in resid)
    print("\n  TRUE RESIDUAL: %d files, ~%d priced rows" % (len(resid), pr))
    print("  by work type:",
          dict(collections.Counter(r["work_type"] for r in resid).most_common()))

    print("\nheader tokens found (of ITEM/DESC/QTY/UNIT/PRICE/TOTAL):")
    for k, v in sorted(collections.Counter(
            r["n_header_tokens"] for r in out_rows).items(), reverse=True):
        print("  %d tokens: %3d files" % (k, v))
    none = [r for r in out_rows if not r["header_line"]]
    print("\n%d files where NO schedule-header line matched at all" % len(none))
    for r in none[:12]:
        print("    %s" % r["file"][-62:])
    print("\n-> %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
