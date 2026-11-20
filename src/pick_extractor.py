"""Decide which text extractor to trust per PDF, by segmentability.

The problem this solves
-----------------------
35 files in this corpus come out of `pdftotext` with two text columns woven
together letter by letter -- "MCleoawniinngg, otrfi mtramshin,g" is "Mowing,
trimming/" and "Cleaning of trash," interleaved. PyMuPDF's word-box rebuild
reads them correctly. But PyMuPDF has its own defect: it welds adjacent words
("companydallas", "CITYOFMESQUITE"). So neither reader is universally better,
and two earlier attempts at an automatic choice both failed:

  * "prefer the reader with fewer unique long tokens" -- picked the INTERLEAVED
    text for 4 of 6 confirmed files, because welded words are unique tokens too;
  * "prefer the one with fewer doubled-letter runs" -- scored 0 on most
    confirmed files, because `curbelaincehinlet` ("curb inlet" + "each") has no
    doubled letters at all.

Difference is not nonsense. What separates the two defects is that welding is
REVERSIBLE and interleaving is not:

    companydallas      -> company + dallas          both real words
    curbelaincehinlet  -> no split into real words

So the test is segmentability against a dictionary, and the dictionary is built
from this corpus's own text -- bid tabulations use a closed vocabulary
(mobilization, riprap, gabion, headwall, subgrade) that no general word list
covers.

Avoiding the circularity
------------------------
The dictionary must NOT be built from the files it will judge, or the corrupted
tokens become vocabulary and vouch for themselves. It is therefore built only
from files absent from `_interleaved_text.csv`.

The parsing side's `item_vocabulary.csv` was offered for this and is clean
today -- none of the corrupted tokens appear in it. It is not used, because it
is built from parsed line items: once the interleaved files' rows land in
`line_items.csv`, their descriptions would enter that vocabulary and the check
would start approving the very tokens it exists to catch. Being clean today is
not the same as being safe to depend on.

Usage:
    python src/pick_extractor.py --selftest     # the confirmed cases
    python src/pick_extractor.py                # score the whole corpus
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_external import read_pdf_text_mupdf                   # noqa: E402

ROOT = Path("data/external")
INTER = ROOT / "_interleaved_text.csv"
VOCAB = ROOT / "_text_vocabulary.csv"
OUT = ROOT / "_extractor_choice.csv"
SKIP = ("dol", "txdot_archive", "_pre2020", "_ocr")

# Two-letter words are collected deliberately. Welded tokens join real words
# across short ones -- "cityofcarrollton" is city+of+carrollton -- and a
# segmenter that only allows 3+ letter pieces cannot split it, so it scores
# welding as nonsense. That is how PyMuPDF came out looking WORSE than the
# interleaved text on one Carrollton file. This corpus also uses many genuine
# two-letter tokens (of, in, at, st, rd, ea, ls, sy, cy, lf).
TOKEN = re.compile(r"[A-Za-z]{2,}")
LONG = re.compile(r"[A-Za-z]{12,}")
MIN_VOCAB_HITS = 3          # a word must recur, so typos do not become words


def pdftotext(path: Path) -> str:
    try:
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                           capture_output=True, timeout=180)
        return r.stdout.decode("utf-8", "replace")
    except Exception:                                             # noqa: BLE001
        return ""


def corpus_files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.pdf")):
        rel = p.relative_to(ROOT).as_posix()
        if p.name.startswith("_") or any(rel == s or rel.startswith(s + "/")
                                         for s in SKIP):
            continue
        out.append(p)
    return out


def suspect_files() -> set[str]:
    bad = set()
    if INTER.exists():
        for r in csv.DictReader(INTER.open(encoding="utf-8-sig")):
            bad.add(r["file"])
    return bad


def build_vocabulary(limit: int = 400) -> dict[str, int]:
    """Word frequencies from files NOT suspected of interleaving."""
    bad = suspect_files()
    counts: dict[str, int] = {}
    n = 0
    for p in corpus_files():
        if p.relative_to(ROOT).as_posix() in bad:
            continue
        t = pdftotext(p)
        if not t.strip():
            continue
        for w in TOKEN.findall(t):
            w = w.lower()
            counts[w] = counts.get(w, 0) + 1
        n += 1
        if n >= limit:
            break
    vocab = {w: c for w, c in counts.items() if c >= MIN_VOCAB_HITS}
    with VOCAB.open("w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["word", "count"])
        for w, c in sorted(vocab.items(), key=lambda kv: -kv[1]):
            wr.writerow([w, c])
    print("vocabulary: %d words from %d clean files -> %s" % (len(vocab), n,
                                                              VOCAB))
    return vocab


def load_vocabulary() -> dict[str, int]:
    if not VOCAB.exists():
        return build_vocabulary()
    out = {}
    for r in csv.DictReader(VOCAB.open(encoding="utf-8-sig")):
        out[r["word"]] = int(r["count"])
    return out


def make_segmenter(vocab: dict[str, int]):
    words = set(vocab)

    @lru_cache(maxsize=200_000)
    def segmentable(tok: str) -> bool:
        """Can tok be split entirely into dictionary words of 2+ letters?"""
        n = len(tok)
        reach = [False] * (n + 1)
        reach[0] = True
        for i in range(n):
            if not reach[i]:
                continue
            for j in range(i + 2, n + 1):
                if not reach[j] and tok[i:j] in words:
                    reach[j] = True
        return reach[n]

    return segmentable


def nonsense_count(text: str, segmentable) -> tuple[int, list[str]]:
    bad = []
    for w in set(LONG.findall(text)):
        lw = w.lower()
        if lw in ("",):
            continue
        if not segmentable(lw):
            bad.append(lw)
    return len(bad), sorted(bad)[:4]


def choose(path: Path, segmentable) -> dict:
    a, b = pdftotext(path), read_pdf_text_mupdf(path)
    na, exa = nonsense_count(a, segmentable)
    nb, exb = nonsense_count(b, segmentable)
    # Only switch on a clear margin: the two readers also disagree for benign
    # reasons, and a one-token difference is noise, not evidence.
    if nb + 2 <= na:
        pick = "pymupdf"
    else:
        pick = "pdftotext"
    return {"file": path.relative_to(ROOT).as_posix(), "pick": pick,
            "pdftotext_nonsense": na, "pymupdf_nonsense": nb,
            "pdftotext_examples": " ".join(exa),
            "pymupdf_examples": " ".join(exb)}


SELFTEST = [
    # (file, expected pick) -- confirmed by eye earlier in this work
    ("mesquite/2023-053.pdf", "pymupdf"),
    ("legistar/carrolltontx/2022-02-09_5574_Bid_Tabulation_22-012.pdf", "pymupdf"),
    ("legistar/mesquite/2026-06-15_26-8289_Bid_Tabulation.pdf", "pymupdf"),
    ("legistar/carrolltontx/2025-09-09_7369_Bid_Tabulation.pdf", "pymupdf"),
    # clean files must NOT switch
    ("arlington/PWDR17012C_JC-7_Channel_Repair_gabion_bid_2022-02-23.pdf",
     "pdftotext"),
    ("arlington/PWDR19010C_Kee_Branch_Trib1_Erosion_Repair_bid_2022-09-07.pdf",
     "pdftotext"),
    ("rowlett/2022-18_Dalrock_South_Curve.pdf", "pdftotext"),
    ("mesquite/2021-050.pdf", "pdftotext"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rebuild-vocab", action="store_true")
    args = ap.parse_args()

    vocab = build_vocabulary() if args.rebuild_vocab else load_vocabulary()
    seg = make_segmenter(vocab)

    if args.selftest:
        bad = 0
        for rel, want in SELFTEST:
            p = ROOT / rel
            if not p.exists():
                print("  MISSING %s" % rel)
                continue
            r = choose(p, seg)
            ok = r["pick"] == want
            bad += not ok
            print("%s want=%-9s got=%-9s pt_junk=%-4d mu_junk=%-4d %s"
                  % ("ok  " if ok else "FAIL", want, r["pick"],
                     r["pdftotext_nonsense"], r["pymupdf_nonsense"], rel[-46:]))
            if not ok:
                print("        pdftotext: %s" % r["pdftotext_examples"])
                print("        pymupdf  : %s" % r["pymupdf_examples"])
        print("\n%d/%d correct" % (len(SELFTEST) - bad, len(SELFTEST)))
        return 1 if bad else 0

    rows = []
    files = corpus_files()
    for i, p in enumerate(files, 1):
        try:
            rows.append(choose(p, seg))
        except Exception as exc:                                  # noqa: BLE001
            print("  ERR %s: %s" % (p.name, exc))
        if i % 150 == 0:
            print("  %d/%d" % (i, len(files)), flush=True)

    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "pick", "pdftotext_nonsense",
                                           "pymupdf_nonsense",
                                           "pdftotext_examples",
                                           "pymupdf_examples"])
        w.writeheader()
        w.writerows(rows)
    switch = [r for r in rows if r["pick"] == "pymupdf"]
    print("\n%d of %d files read better with PyMuPDF." % (len(switch), len(rows)))
    for r in sorted(switch, key=lambda r: -r["pdftotext_nonsense"])[:30]:
        print("  pt=%-4d mu=%-4d %s" % (r["pdftotext_nonsense"],
                                        r["pymupdf_nonsense"], r["file"][-58:]))
    print("\n-> %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
