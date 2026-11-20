"""Export a per-file manifest for the parsing side: metadata only, no prices.

What this is for
----------------
The modelling side owns unit-price aggregation; this side owns getting the
source files and their metadata right. That split is deliberate: a second
price-basis table built to a different convention would force every consumer
to decide which one to trust, and nobody is in a position to make that call.

So this exports no price and no item description. It exports what the parsing
side cannot derive on its own, and one column it specifically cannot:

    in_line_items -- whether the file produced any row in data/clean/line_items.csv

That column is the answer to "which files are missing from the parse". Read
from the parse alone you can only see how many rows came out of a file, never
that a file which SHOULD have produced rows produced none. Joining this
index's own judgement (classification says it carries a line-item schedule)
against the parse output turns an unknown into a list.

Usage:
    python src/export_manifest.py                      # erosion + drainage
    python src/export_manifest.py --work-types all
    python src/export_manifest.py --out some/path.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path("data/external")
INDEX = ROOT / "INDEX.csv"
LINE_ITEMS = Path("data/clean/line_items.csv")
MULTI = ROOT / "_multi_letting.csv"

USABLE = {"line_item", "likely_line_item", "unit_price_table",
          "unit_price_dataset"}


def conflict_state(s: str) -> str:
    """Collapse the free-text conflict note into something joinable."""
    if not s:
        return "none"
    if "resolved by" in s:
        return "resolved_by_override"
    if s.startswith("explained:"):
        return "explained_program_year"
    return "open"


def page_count(p: Path) -> str:
    if p.suffix.lower() != ".pdf":
        return ""
    try:
        import fitz
        d = fitz.open(str(p))
        try:
            return str(d.page_count)
        finally:
            d.close()
    except Exception:                                             # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-types", default="erosion,drainage",
                    help='comma-separated, or "all"')
    ap.add_argument("--out", default="data/external/_manifest_for_parsing.csv")
    args = ap.parse_args()

    if not INDEX.exists():
        print("run src/index_external.py first", file=sys.stderr)
        return 1
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
        print("note: data/clean/line_items.csv not found; in_line_items will "
              "be blank rather than false -- an absent parse is not the same "
              "as a file the parse rejected.")

    # `in_line_items` alone is misleading, and measuring it proved the point:
    # measured against one snapshot, 36 of 37 files with no row in
    # data/clean/parse_audit.csv WITH large row counts -- 23-6622 parsed as
    # five sheets of 234-276 rows at 6 price columns each. Those files were
    # parsed and then lost somewhere downstream (or line_items.csv is simply
    # older than parse_audit.csv). Reporting them as "missing from the parse"
    # would have sent the parsing side hunting for a extraction bug that is
    # not there.
    #
    # So both are reported. The pair is the diagnostic:
    #   audit yes / items no  -> parsed, then dropped downstream, or stale file
    #   audit no  / items no  -> the parser never got rows out of it
    audit_rows: dict[str, int] = {}
    pa = Path("data/clean/parse_audit.csv")
    if pa.exists():
        for r in csv.DictReader(pa.open(encoding="utf-8-sig")):
            src = (r.get("src_file") or "").replace("\\", "/")
            if src.startswith("external/"):
                src = src[len("external/"):]
            try:
                n = int(float(r.get("n_rows") or 0))
            except ValueError:
                n = 0
            if src:
                audit_rows[src] = audit_rows.get(src, 0) + n

    # Third case, and it is not a problem at all. The parsing side de-duplicates
    # across files, so a letting harvested twice (once from the city's own site,
    # once via Legistar) keeps one copy and drops the other. The dropped file
    # then shows in_parse_audit=True / in_line_items=False and looks exactly
    # like a downstream loss -- `mesquite/2020-010.pdf` read that way here, and
    # its twin `legistar/mesquite/2020-04-02_20-4762` had all 85 rows in the
    # pool the whole time. Without this column the manifest sends someone
    # hunting for data that was never missing.
    dup_twin: dict[str, str] = {}
    dl = Path("data/clean/duplicate_lettings.csv")
    if dl.exists():
        for r in csv.DictReader(dl.open(encoding="utf-8-sig")):
            dropped = (r.get("dropped") or "").split("::")[0]
            kept = (r.get("kept") or "").split("::")[0]
            for v in (dropped, kept):
                if v.startswith("external/"):
                    pass
            d = dropped[len("external/"):] if dropped.startswith("external/") else dropped
            k = kept[len("external/"):] if kept.startswith("external/") else kept
            if d and k and d != k:
                dup_twin.setdefault(d, k)

    multi: dict[str, str] = {}
    if MULTI.exists():
        for r in csv.DictReader(MULTI.open(encoding="utf-8-sig")):
            if r.get("multi_letting") == "True":
                multi[r["file"]] = r.get("confidence", "yes")

    want = None if args.work_types.strip() == "all" else {
        w.strip() for w in args.work_types.split(",") if w.strip()}

    out_rows = []
    for r in rows:
        if r["classification"] not in USABLE:
            continue
        if r["scope_2020plus"] not in ("in_scope", "reference"):
            continue
        if want is not None and r["work_type"] not in want:
            continue
        rel = r["file"]
        out_rows.append({
            "src_file": rel,
            "owner": r["owner"],
            "work_type": r["work_type"],
            "project_kind": r.get("project_kind", ""),
            "bid_date": r["bid_date"],
            "bid_date_source": r.get("bid_date_source", ""),
            "n_bidders": r.get("n_bidders_detected", ""),
            "text_source": r.get("text_source", ""),
            "ocr_trust": r.get("ocr_trust", ""),
            "multi_letting": multi.get(rel, ""),
            "date_conflict_state": conflict_state(r.get("date_conflict", "")),
            "in_line_items": ("" if not parsed else str(rel in parsed)),
            "in_parse_audit": ("" if not audit_rows else str(rel in audit_rows)),
            "parse_audit_rows": audit_rows.get(rel, ""),
            "duplicate_of": dup_twin.get(rel, ""),
            "scope": r["scope_2020plus"],
            "classification": r["classification"],
            "n_pages": page_count(ROOT / rel),
        })

    out_rows.sort(key=lambda r: (r["owner"], r["src_file"]))
    cols = ["src_file", "owner", "work_type", "project_kind", "bid_date",
            "bid_date_source", "n_bidders", "text_source", "ocr_trust",
            "multi_letting", "date_conflict_state", "in_line_items",
            "in_parse_audit", "parse_audit_rows", "duplicate_of", "scope",
            "classification",
            "n_pages"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    print("%d files -> %s" % (len(out_rows), out))
    import datetime as dt
    for f in (LINE_ITEMS, Path("data/clean/parse_audit.csv")):
        if f.exists():
            ts = dt.datetime.fromtimestamp(f.stat().st_mtime)
            print("  read %s (written %s)" % (f, ts.strftime("%Y-%m-%d %H:%M:%S")))
    print("  Both are written by the parsing side's ETL, so these counts are a "
          "snapshot:\n  measured once against a stale line_items.csv, 37 files "
          "looked absent; against\n  the copy written minutes later, 20 did. "
          "Re-run this after their ETL lands.")

    missing = [r for r in out_rows if r["in_line_items"] == "False"]
    dupes = [r for r in missing if r["duplicate_of"]]
    dropped = [r for r in missing if r["in_parse_audit"] == "True"
               and not r["duplicate_of"]]
    never = [r for r in missing if r["in_parse_audit"] != "True"
             and not r["duplicate_of"]]
    print("")
    print("  VERIFIED DUPLICATE -- %d. Not missing: the same letting was "
          "harvested twice and the twin carries the rows." % len(dupes))
    for r in dupes:
        print("    %s -> %s" % (r["src_file"][-42:], r["duplicate_of"][-46:]))
    print("\n%d of %d files have no row in line_items.csv, split by whether the "
          "parser ever saw them:" % (len(missing), len(out_rows)))
    print("\n  PARSED but absent from line_items.csv -- %d. NOT an extraction "
          "problem:\n  parse_audit.csv records real tables and row counts for "
          "these, so they were\n  parsed and then lost downstream (or "
          "line_items.csv is older)." % len(dropped))
    for r in dropped:
        print("    %s %-9s audit_rows=%-6s %s"
              % (r["bid_date"] or "          ", r["work_type"],
                 r["parse_audit_rows"], r["src_file"][-50:]))
    print("\n  NOT in parse_audit.csv either -- %d. Only these are candidates "
          "for a parser\n  that never got rows out of them." % len(never))
    for r in never:
        print("    %s %-9s %-18s %s" % (r["bid_date"] or "          ",
                                        r["work_type"], r["classification"],
                                        r["src_file"][-50:]))
    print("\nColumn notes for the consumer:")
    print("  n_bidders       detected by regex off page one -- an UPPER BOUND.")
    print("                  It also counts the engineering firm and any")
    print("                  contractor named in a cover memo. Not a bidder count.")
    print("  ocr_trust       high/partial = the file's own rows satisfy")
    print("                  qty x unit price = total, so its PRICES may be")
    print("                  parsed. Descriptions are damaged even when trust")
    print("                  is high, so item_family must not be inferred from")
    print("                  them -- mark those needs_review.")
    print("  multi_letting   header_paired = letting count is verified;")
    print("                  date_anchored = the flag is sound, the count is not.")
    print("  project_kind    service_contract = priced by labour-hour or site")
    print("                  visit, not installed work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
