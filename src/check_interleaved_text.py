"""Find PDFs whose text comes out with two columns interleaved letter-by-letter.

The defect
----------
`mesquite/2023-053.pdf` extracts a description as

    MCleoawniinngg, otrfi mtramshin,g d/eebdrgisin, ga nodf srtuabnb...

which is "Mowing, trimming/edging of standard..." and "Cleaning of trash,
debris, and rubbish..." -- two adjacent text runs woven together one letter at
a time. It is a property of the PDF, not of one reader: `pdftotext` (with and
without -layout) and `pdfplumber` (ditto) all produce it.

PyMuPDF does not, because this project reads it through a word-box rebuild --
words are grouped into visual rows by y and laid out by x, so two runs that
overlap in the content stream still land in separate places.

Why it matters more than it looks
---------------------------------
A mangled description is not cosmetic here. `item_family` is assigned from the
description, and family plus unit is the grouping key for every unit-price
basket. A row whose description is unreadable gets no family, and a family
rule with a "drop what cannot be classified" branch will discard the row
silently -- price and all.

Why this is measured rather than detected by pattern
----------------------------------------------------
Text-only signatures do not hold up. Interleaving often produces doubled
letters ("...iinngg"), but not always: `srtuabnbdiashrd` has none. Long
consonant runs catch that one, but "RECONSTRUCTION" contains "NSTR" and this
corpus is full of it. Rather than tune a heuristic until it fits the two
examples in hand, this compares the two extractors directly: a long token that
`pdftotext` reports and PyMuPDF does not is an artifact of the reader, not a
word in the document.

Usage:
    python src/check_interleaved_text.py
    python src/check_interleaved_text.py --min-len 12
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_external import read_pdf_text_mupdf                   # noqa: E402

ROOT = Path("data/external")
OUT = ROOT / "_interleaved_text.csv"
SKIP = ("dol", "txdot_archive", "_pre2020", "_ocr")



def pdftotext(path: Path) -> str:
    try:
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                           capture_output=True, timeout=180)
        return r.stdout.decode("utf-8", "replace")
    except Exception:                                             # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-len", type=int, default=12)
    args = ap.parse_args()
    word = re.compile(r"[A-Za-z]{%d,}" % args.min_len)

    files = []
    for p in sorted(ROOT.rglob("*.pdf")):
        rel = p.relative_to(ROOT).as_posix()
        if p.name.startswith("_") or any(rel == s or rel.startswith(s + "/")
                                         for s in SKIP):
            continue
        files.append(p)
    print("comparing two extractors over %d PDFs" % len(files))

    rows = []
    for i, p in enumerate(files, 1):
        rel = p.relative_to(ROOT).as_posix()
        a = pdftotext(p)
        if not a.strip():
            continue
        try:
            b = read_pdf_text_mupdf(p)
        except Exception:                                         # noqa: BLE001
            continue
        if not b.strip():
            continue
        ta = {w.lower() for w in word.findall(a)}
        tb = {w.lower() for w in word.findall(b)}
        only = sorted(ta - tb)
        # A handful of differences is normal: the two readers break lines and
        # hyphens differently, so a long token can legitimately exist in one
        # and not the other. Interleaving produces them in bulk.
        if len(only) >= 5:
            rows.append({"file": rel, "n_pdftotext_only": len(only),
                         "n_long_tokens": len(ta),
                         "share": round(len(only) / max(1, len(ta)), 3),
                         "examples": " ".join(only[:6])})
        if i % 150 == 0:
            print("  %d/%d" % (i, len(files)), flush=True)

    rows.sort(key=lambda r: -r["share"])
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "n_pdftotext_only",
                                           "n_long_tokens", "share",
                                           "examples"])
        w.writeheader()
        w.writerows(rows)

    print("\n%d files where pdftotext invents 5+ long tokens PyMuPDF never "
          "sees." % len(rows))
    print("High `share` is the interleaving signature; a low share is usually "
          "just line-breaking.\n")
    for r in rows[:25]:
        print("  share=%.2f  %-62s" % (r["share"], r["file"][-62:]))
        print("        %s" % r["examples"][:110])
    print("\n-> %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
