"""
Move out-of-window bid tabulations out of the active corpus.

The cutoff itself lives in `index_external.py` (`--since-year`, currently 2021);
this script just acts on whatever that marked. The script name, the `_pre2020/`
folder and the `pre_2020` scope value are HISTORICAL -- they date from when the
cutoff was 2020. They are deliberately not renamed: the parsing side excludes
`_pre2020/` by name, so renaming the folder would quietly re-admit every
quarantined file into that pipeline.

Pre-pandemic bids were priced on a cost base that no longer applies, so they are
not comparable with current work. They are moved to `data/external/_pre2020/`
rather than deleted: the source subfolder is preserved, and a log records where
each file came from, so any of it can be restored if a longer time series is
ever wanted.

The active audit/index skip `_pre2020/`, so quarantined files stop appearing in
INDEX.csv / INDEX.md.

Usage:
    python src/quarantine_pre2020.py --dry-run     # show what would move
    python src/quarantine_pre2020.py               # do it
    python src/quarantine_pre2020.py --restore     # put everything back
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lock import PipelineBusy, pipeline_lock  # noqa: E402

ROOT = Path("data/external")
QUAR = ROOT / "_pre2020"
LOG = QUAR / "_moved.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="break a pipeline lock left by a dead run")
    args = ap.parse_args()

    if args.restore:
        if not LOG.exists():
            print(f"nothing to restore: {LOG} not found")
            return 1
        n = 0
        for r in csv.DictReader(LOG.open(encoding="utf-8-sig")):
            src = QUAR / r["quarantined_as"]
            dst = ROOT / r["original_path"]
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                n += 1
        LOG.unlink(missing_ok=True)
        print(f"restored {n} files to their original paths")
        return 0

    index = ROOT / "INDEX.csv"
    if not index.exists():
        print("run src/index_external.py first (INDEX.csv is missing)")
        return 1

    all_rows = list(csv.DictReader(index.open(encoding="utf-8-sig")))
    pre = [r for r in all_rows if r["scope_2020plus"] == "pre_2020"]

    # More than one cutoff can be in force (scarce work families are held to an
    # earlier one), so report every cutoff present rather than just the first.
    windows: dict[str, set[str]] = {}
    for r in all_rows:
        w = r.get("window_from_year")
        if w:
            windows.setdefault(w, set()).add(r.get("work_type", "?"))
    print("cutoffs in force (from INDEX.csv):")
    for w in sorted(windows):
        kinds = sorted(windows[w])
        shown = ", ".join(kinds) if len(kinds) <= 4 else f"{len(kinds)} work types"
        print(f"  bids before {w} are out of scope  [{shown}]")
    if not pre:
        print("no out-of-window files in the active corpus")
        return 0

    usable = {"line_item", "likely_line_item", "unit_price_table",
              "unit_price_dataset"}
    n_line = sum(1 for r in pre if r["classification"] in usable)
    print(f"{len(pre)} out-of-window files ({n_line} of them with line-item prices)")

    rows = []
    for r in pre:
        rel = r["file"]
        src = ROOT / rel
        if not src.exists():
            continue
        dst = QUAR / rel                     # keep the owner subfolder
        print(f"  {'would move' if args.dry_run else 'move'}  {rel}"
              f"  [{r['bid_year'] or '?'}]")
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        rows.append({
            "original_path": rel,
            "quarantined_as": rel,
            "bid_year": r["bid_year"],
            "work_type": r["work_type"],
            "classification": r["classification"],
            "project_title": r["project_title"],
        })

    if args.dry_run:
        print(f"\ndry run: nothing moved. Re-run without --dry-run to move "
              f"{len(rows)} files.")
        return 0

    if not rows:
        # Every flagged file was already moved -- a re-run against a stale
        # INDEX.csv, which is a no-op, not a failure.
        print("\nnothing to move: the flagged files are already quarantined.")
        print("re-run src/index_external.py if INDEX.csv looks out of date.")
        return 0

    QUAR.mkdir(parents=True, exist_ok=True)
    FIELDS = ["original_path", "quarantined_as", "bid_year", "work_type",
              "classification", "project_title"]
    write_header = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)

    (QUAR / "README.md").write_text(
        "# `_pre2020/` — quarantined, not part of the active corpus\n\n"
        "Bids opened before the active cutoff (the folder name says 2020 for\n"
        "historical reasons; the cutoff is now 2021 and is recorded per row in\n"
        "INDEX.csv's `window_from_year`). Pre-pandemic pricing is not comparable with\n"
        "current work, so these are held out of the active dataset: the audit\n"
        "and index skip this folder, and nothing here appears in `INDEX.csv`.\n\n"
        "Kept rather than deleted. `_moved.csv` records every file's original\n"
        "path, year, work type and line-item classification. To put them back:\n\n"
        "```\npython src/quarantine_pre2020.py --restore\n```\n",
        encoding="utf-8")

    print(f"\nmoved {len(rows)} files -> {QUAR}")
    print(f"log -> {LOG}  (restore with: python src/quarantine_pre2020.py --restore)")
    print("now re-run: python src/audit_external.py && python src/index_external.py")
    return 0


if __name__ == "__main__":
    # Single-writer: this pipeline shares its files with another session.
    try:
        with pipeline_lock("quarantine", force="--force" in sys.argv):
            raise SystemExit(main())
    except PipelineBusy as exc:
        print(exc)
        raise SystemExit(2)
