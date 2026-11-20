"""Detect PDFs that contain MORE THAN ONE solicitation.

Why this matters
----------------
The parsing pipeline treats one file as one letting: it takes a single bid date
per file and counts bidders per file. A council packet breaks that assumption.
`hickory_creek/2024-10-07_BID2024-03_..._Council_Packet_with_Bid_Tabulation.pdf`
holds two independent lettings:

    BID 2024-02   opened 2024-09-19   8 bidders
    BID 2024-03   opened 2024-09-26   6 bidders

Collapsed into one file-level record that becomes "14 bidders on one project",
which is not a real observation. `competition_class` is derived from the bidder
count and is then used to *select* which rows set the unit-price basis, so a
fabricated 14-bidder project does not merely add noise -- it silently moves the
benchmark. That is the same shape of error as a filter that matches nothing: the
number still looks reasonable, so nothing complains.

This flags those files so they can be held out of competition calibration until
a section-aware parser exists. It does not split them; splitting is the parser's
job, and a wrong split would be worse than a flag.

How it decides
--------------
Three structural signals, none of which relies on the filename or on a project
number (project numbers in this corpus run up to ten years behind the actual
letting -- `PWDR12001C` was opened in 2022 -- so anything keyed on them is
wrong by construction):

  1. two or more DISTINCT solicitation identifiers appear in the body, far
     enough apart to bracket separate tables;
  2. the bid-opening dates attached to them differ by at least a week;
  3. the bidder sets around them are substantially different.

(1) alone is not enough: a tabulation legitimately cites the previous phase's
bid number, and an addendum cites the original. So (1) only proposes segments,
and the file is flagged only when (2) or (3) confirms that the segments are
genuinely different competitions.

Dates are only believed near an opening cue. This file also contains a bond
debt-service schedule running from 2026 to 2031; a plain "latest and earliest
date" rule would read those as two lettings five years apart.

Usage:
    python src/multi_letting.py                   # whole corpus
    python src/multi_letting.py --file <relpath>  # one file, verbose
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_external import (PRICED_ROW, read_pdf_text,            # noqa: E402
                            read_xlsx_text)
from index_external import CONTRACTOR, NOISE                      # noqa: E402

ROOT = Path("data/external")
OUT = ROOT / "_multi_letting.csv"
SKIP = ("dol", "txdot_archive", "_pre2020", "_ocr")
OWN = {"INDEX.csv", "INDEX.md", "README.md"}

# "BID# 2024-02", "Bid No. 2024-03", "RFB 25-023", "IFB 7381", "CSP 2022-19".
SOLICITATION = re.compile(
    r"(?i)\b(?:BID|RFB|IFB|ITB|CSP|RFP|RFQ)\b[ \t]*"
    r"(?:NO\.?|NUMBER|#)?[ \t]*"
    r"(\d{2,4}-\d{1,4}[A-Z]?|\d{4,6})\b"
)

MONTHS = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December")
DATE = re.compile(r"(?i)\b(" + MONTHS + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b")
DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")

# A date is only treated as a bid opening if one of these sits near it. Without
# this, a bond amortisation table (2026-2031) reads as lettings years apart.
OPEN_CUE = re.compile(
    r"(?i)bid[ \t]*open|open(?:ed|ing)?[ \t]*(?:date|at)|bids?[ \t]+"
    r"(?:were[ \t]+)?receiv|tabulat|due[ \t]*(?:date|by)|read[ \t]+aloud|"
    r"submitt?al[ \t]*deadline"
)
CUE_WINDOW = 400          # characters either side of the date

MONTH_NO = {m.lower(): i + 1 for i, m in enumerate(
    "January February March April May June July August September October "
    "November December".split())}


def opening_dates(text: str):
    """[(offset, date)] for dates that sit near an opening cue."""
    cues = [m.start() for m in OPEN_CUE.finditer(text)]
    if not cues:
        return []
    out = []
    for m in DATE.finditer(text):
        try:
            d = dt.date(int(m.group(3)), MONTH_NO[m.group(1).lower()],
                        int(m.group(2)))
        except ValueError:
            continue
        if any(abs(c - m.start()) <= CUE_WINDOW for c in cues):
            out.append((m.start(), d))
    for m in DATE_NUM.finditer(text):
        try:
            d = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if any(abs(c - m.start()) <= CUE_WINDOW for c in cues):
            out.append((m.start(), d))
    return sorted(out)


def bidders_between(text: str, lo: int, hi: int) -> set[str]:
    out = set()
    for m in CONTRACTOR.finditer(text):
        if not (lo <= m.start() < hi):
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        if len(name) < 5 or NOISE.search(name):
            continue
        out.add(name.lower())
    return out


MIN_BIDDERS = 3        # a segment with fewer names is a memo, not a tabulation
MIN_PRICED_ROWS = 3    # ...and a real tabulation prices line items

# Bidder counts below are an UPPER BOUND. The contractor-name pattern also
# picks up the engineering firm, address lines and the awarded contractor
# named in a cover memo, so a section with 6 real bidders can report 18
# names. That is fine for deciding "is there a second competition in here",
# which is what this script is for, and it is why the count is reported as
# `bidder_names_seen` rather than as a bidder count: nothing downstream
# should take it for one.


def analyse(text: str):
    """Return a verdict dict for one document's text.

    A letting is identified by its TABULATION HEADER, where the solicitation
    number and the opening date are printed next to each other:

        Bid No. 2024-02   ...   September 19, 2024
        Bid No. 2024-03   ...   September 26, 2024

    Pairing the two is what makes this reliable, and the two earlier attempts
    show why neither half works alone:

      * anchoring on where each bid number FIRST appears fails, because a
        council packet opens with an agenda naming every item together -- in
        the Hickory Creek packet the two bid numbers sit 120 characters apart
        on the cover, so both boundaries land on page 1 and one segment
        swallows the document;
      * anchoring on the dates alone fails too. That packet also carries a
        council-meeting date of 2024-10-02 which happens to sit near the word
        "tabulation", so it scored as a third competition -- in a file that
        holds exactly two. It survived a "memos have no prices" filter because
        the mis-cut segment had 22 priced rows in it.

    So each solicitation number is dated by the opening date nearest to it, and
    duplicates collapse: 2024-03 pairs with 09-26 at 32 characters and with
    10-02 at 130, so 09-26 wins and 10-02 stops being a letting of its own.

    Nothing here reads a year out of a project number. In this corpus a project
    number can trail its letting by a decade (`PWDR12001C` was opened in 2022),
    so any rule keyed on one is wrong by construction.
    """
    hits = [(m.start(), m.group(1).upper()) for m in SOLICITATION.finditer(text)]
    spans: dict[str, list[int]] = {}
    for off, sid in hits:
        spans.setdefault(sid, []).append(off)

    dates = opening_dates(text)
    uniq = sorted({d for _o, d in dates})
    verdict = {"n_solicitation_ids": len(spans),
               "ids": ";".join(sorted(spans, key=lambda s: min(spans[s]))[:6]),
               "n_opening_dates": len(uniq),
               "opening_dates": ";".join(d.isoformat() for d in uniq)[:120],
               "multi_letting": False, "n_lettings": 1 if dates else 0,
               "ids_with_tables": 0, "basis": "", "segments": "",
               "confidence": ""}
    if len(uniq) < 2:
        verdict["basis"] = ("one opening date" if uniq
                            else "no opening date found near a bid-opening cue")
        return verdict

    # Pair each solicitation-number occurrence with the nearest opening date,
    # then keep, per number, its closest pairing.
    PAIR_WINDOW = 400
    best: dict[str, tuple[int, int, dt.date]] = {}      # id -> (dist, off, date)
    for off, sid in hits:
        near = min(((abs(o - off), o, d) for o, d in dates),
                   default=None, key=lambda t: t[0])
        if near is None or near[0] > PAIR_WINDOW:
            continue
        if sid not in best or near[0] < best[sid][0]:
            best[sid] = (near[0], off, near[2])

    if len(best) >= 2:
        anchors = sorted((off, sid, d) for sid, (_dist, off, d) in best.items())
        verdict["confidence"] = "header_paired"
    else:
        # No two dated headers. Fall back to segmenting on the dates themselves,
        # so a file whose two tables share one number series is still caught.
        #
        # The fallback must still pass the SAME structural test. An earlier
        # version simply flagged any file whose opening dates spanned a week or
        # more, and that is worthless: a legal advertisement runs two or three
        # times on consecutive weeks, every insertion says "bids will be
        # received", so one ordinary letting produces three dates near a cue.
        # On this corpus that rule flagged 22 files of which 2 were real -- and
        # it reported a letting count straight from the number of dates, so the
        # Kerr County notice (advertised 3 June, 10 June, 17 June) came out as
        # "5 lettings". A count nobody verified is worse than no count.
        first: dict[dt.date, int] = {}
        for off, d in dates:
            first.setdefault(d, off)
        anchors = sorted((off, "", d) for d, off in first.items())
        verdict["confidence"] = "date_anchored"
    if len(anchors) < 2:
        verdict["basis"] = "only one dated section"
        return verdict
    bounds = [0]
    for i in range(len(anchors) - 1):
        bounds.append((anchors[i][0] + anchors[i + 1][0]) // 2)
    bounds.append(len(text))

    segs = []
    for i, (_off, sid, d) in enumerate(anchors):
        lo, hi = bounds[i], bounds[i + 1]
        ids = [sid] if sid else [s2 for s2, offs in spans.items()
                                 if any(lo <= o < hi for o in offs)]
        segs.append({"date": d, "lo": lo, "hi": hi, "ids": ids,
                     "bidders": bidders_between(text, lo, hi),
                     "priced": len(PRICED_ROW.findall(text[lo:hi]))})

    # A competition needs both a list of names and priced line items, so a
    # cover memo or an award letter cannot pass as one.
    lettings = [s for s in segs
                if len(s["bidders"]) >= MIN_BIDDERS
                and s["priced"] >= MIN_PRICED_ROWS]
    verdict["ids_with_tables"] = len({sid for s in lettings for sid in s["ids"]})
    verdict["segments"] = " | ".join(
        "%s/%s@%d-%d names=%d priced_rows=%d"
        % (",".join(s["ids"][:2]) or "?", s["date"].isoformat(), s["lo"],
           s["hi"], len(s["bidders"]), s["priced"])
        for s in segs[:8])

    if len(lettings) < 2:
        verdict["basis"] = ("%d dated headers, %d of them with both names "
                            "and priced rows" % (len(segs), len(lettings)))
        return verdict

    # Both tests must agree. Either one alone has a benign explanation: two
    # dates a week apart can be an advertisement and its opening, and two
    # different name sets can be the bidders in one table and the sub-consultants
    # listed in the award memo for the same job. Two sections opened weeks apart
    # by largely different fields of bidders is a second competition.
    reasons = []
    ds = sorted({s["date"] for s in lettings})
    dated_apart = len(ds) >= 2 and (ds[-1] - ds[0]).days >= 7
    a, b = lettings[0]["bidders"], lettings[1]["bidders"]
    shared = len(a & b)
    names_differ = shared / max(1, len(a | b)) < 0.5
    if dated_apart:
        reasons.append("opening dates %s are %d days apart"
                       % (",".join(d.isoformat() for d in ds),
                          (ds[-1] - ds[0]).days))
    if names_differ:
        reasons.append("bidder name sets differ (%d and %d names, %d shared)"
                       % (len(a), len(b), shared))

    if dated_apart and names_differ:
        verdict["multi_letting"] = True
        verdict["n_lettings"] = len(lettings)
        verdict["basis"] = "; ".join(reasons)
    else:
        verdict["basis"] = (
            "%d priced sections but only one competition: %s"
            % (len(lettings),
               "; ".join(reasons) if reasons
               else "same date window and overlapping bidders")
            + " -- re-read, addendum or advertisement")
    return verdict


def text_of(p: Path) -> str:
    if p.suffix.lower() == ".pdf":
        return read_pdf_text(p)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return read_xlsx_text(p)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    args = ap.parse_args()

    if args.file:
        p = ROOT / args.file
        v = analyse(text_of(p))
        for k, val in v.items():
            print("%-20s %s" % (k, val))
        return 0

    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".xlsx", ".xlsm",
                                                       ".xls"):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if p.name in OWN or p.name.startswith("_"):
            continue
        if any(rel == s or rel.startswith(s + "/") for s in SKIP):
            continue
        files.append(p)
    files.sort()
    print("scanning %d files for multiple solicitations" % len(files))

    rows = []
    for i, p in enumerate(files, 1):
        rel = p.relative_to(ROOT).as_posix()
        try:
            v = analyse(text_of(p))
        except Exception as exc:                                  # noqa: BLE001
            print("  ERR %s: %s" % (rel, exc))
            continue
        v["file"] = rel
        rows.append(v)
        if v["multi_letting"]:
            print("  [%d/%d] MULTI (%d) %s\n          %s"
                  % (i, len(files), v["n_lettings"], rel, v["basis"]))
        if i % 200 == 0:
            print("  ...%d/%d" % (i, len(files)), flush=True)

    cols = ["file", "multi_letting", "n_lettings", "confidence",
            "n_solicitation_ids", "ids_with_tables", "ids", "n_opening_dates",
            "opening_dates", "basis", "segments"]
    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (not r["multi_letting"],
                                                r["file"])))

    multi = [r for r in rows if r["multi_letting"]]
    print("\n%d of %d files contain more than one solicitation." % (len(multi),
                                                                    len(rows)))
    print("Hold these out of any per-file bidder count or competition class:")
    for r in multi:
        print("  %-2d  %s" % (r["n_lettings"], r["file"]))
    print("\n-> %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
