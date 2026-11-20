"""
Find byte-identical duplicates in the active corpus and write them out as a
known-truth list.

Why this exists: the harvesters dedupe by URL, not by content, and the same
tabulation legitimately reaches us by several routes --

  * Collin County attaches one document to several agenda items, each with its
    own document id, and re-attaches it at later meetings;
  * a Legistar bid tab is attached to the matter that awarded it AND to the
    matter that later amended it (different attachment urls, same file);
  * Mesquite tabs were harvested twice, once from the purchasing web archive
    and once from Legistar, by two scripts that do not know about each other;
  * a few were fetched by hand before the harvester covered that source.

A duplicate that reaches the price pool is worse than it looks: it does not
just inflate the sample, it narrows the distribution artificially. The same
bid's prices counted twice leaves the median where it was but pulls the
quartiles in, so an item looks more stable than it is -- and the quoted range
is the thing that actually gets used.

This script only REPORTS. It does not move or delete anything, because the
parsing side does its own content-level dedupe; this list is there to validate
that dedupe against, and to make the duplicate routes visible.

Output: data/external/_duplicates.csv

Usage:
    python src/duplicate_census.py
    python src/duplicate_census.py --ext .pdf,.xlsx
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import re
from pathlib import Path

ROOT = Path("data/external")
OUT = ROOT / "_duplicates.csv"
# Raw archives, labour data and quarantined files are not part of the price pool.
SKIP = ("dol", "txdot_archive", "_pre2020", "cost_indices")

# Lower score = better copy to keep. A "change order" or "revised" tabulation is
# a later restatement; a name marked copy/draft/internal is a working file.
DEMOTE = [
    (re.compile(r"change[_\s-]*order", re.I), 40),
    (re.compile(r"revised", re.I), 20),
    (re.compile(r"\b(copy|draft|internal|old|backup|working)\b", re.I), 30),
    (re.compile(r"quote", re.I), 10),
    (re.compile(r"comments?removed|_v\d", re.I), 5),
]


def keep_score(rel: str) -> tuple:
    name = Path(rel).name
    penalty = sum(w for pat, w in DEMOTE if pat.search(name))
    # Prefer the earliest dated filename (the meeting that first carried it),
    # then the shorter name, then alphabetical for determinism.
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    return (penalty, m.group(1) if m else "9999-99-99", len(name), rel)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", default=".pdf,.xlsx,.xls")
    args = ap.parse_args()
    exts = {e.strip().lower() for e in args.ext.split(",") if e.strip()}

    by_hash: dict[str, list[str]] = collections.defaultdict(list)
    n = 0
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in SKIP):
            continue
        n += 1
        by_hash[hashlib.md5(p.read_bytes()).hexdigest()].append(rel)

    dups = {h: sorted(v, key=keep_score) for h, v in by_hash.items() if len(v) > 1}

    rows = []
    for h, files in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        for i, rel in enumerate(files):
            rows.append({
                "md5": h,
                "group_size": len(files),
                "role": "keep" if i == 0 else "redundant",
                "file": rel,
                "owner": rel.split("/")[0],
                "route": "/".join(rel.split("/")[:2]),
            })

    if rows:
        with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    redundant = sum(len(v) - 1 for v in dups.values())
    cross = sum(1 for v in dups.values()
                if len({"/".join(f.split("/")[:2]) for f in v}) > 1)
    print(f"files scanned            : {n}")
    print(f"distinct contents        : {len(by_hash)}")
    print(f"duplicate groups         : {len(dups)}")
    print(f"redundant copies         : {redundant} "
          f"({redundant / n * 100:.1f}% of scanned)")
    print(f"  groups spanning routes : {cross}")
    print(f"  groups within one route: {len(dups) - cross}")
    print("\nby owner (redundant copies):")
    owner = collections.Counter(
        r["owner"] for r in rows if r["role"] == "redundant")
    for k, v in owner.most_common():
        print(f"  {k:18s} {v}")
    if rows:
        print(f"\n-> {OUT}  (role=keep is the copy worth parsing)")
    else:
        print("\nno byte-identical duplicates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
