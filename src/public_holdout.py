"""A LOCKED public test set: public lettings that never train anything.

The user's request (2026-11-19 21:xx): besides the company's own files, draw a
few dozen of the harvested public tabulations at random, keep them out of
training, and use them only as a pure test.  Why it matters: every experiment
on 2026-11-19 was judged on the same 2023-26 municipal lettings, so by the end
of the day they had been looked at too often to count as unseen.

What is held out
  * Unit: the SOURCE FILE (stable across parser versions; letting keys are not),
    with every copy of it -- md5-identical files (data/external/_duplicates.csv)
    and lettings the dedup pass matched (data/clean/duplicate_lettings.csv) --
    so no twin can carry a held-out letting back into training.
  * Population: public municipal lettings that the production estimator can
    score forward (keys and dates only; no price or error is read), dated
    2022-01-01 .. 2026-12-31.
  * Draw (version 2, 21:50): the same FRACTION of every year x work-type
    stratum (erosion/drainage vs other), with random.Random(SEED + year).
    Version 1 (21:15, never used, kept as public_holdout_v1_unused.csv) took
    half of each year's 12 from erosion/drainage: 30 of the ~43 recent
    erosion/drainage groups, leaving 13 in 2022-26 training and none in 2023
    or 2026 -- starving the estimator of exactly the work it is for.
How it is kept out
  * src/parse_bidtabs.py gives held-out files source_set = "public_test".
    Every fitted artefact admits source_set == "external" only (price pool,
    reference tables, index, discount, vocabulary, calibration and guard), so
    they drop out everywhere; the train/test guard proves it.
How it may be used
  * Only to CONFIRM a decision that was registered beforehand, and every
    opening is appended to data/test_holdout/access_log.csv (date, what was
    decided, result).  It must never be used to choose between variants or to
    tune anything -- that would spend it.

Usage:
    python src/public_holdout.py --select     # once; refuses if the lock exists
    python src/public_holdout.py --show       # list the held-out files
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REG_DIR = os.path.join(DATA, "test_holdout")
REG = os.path.join(REG_DIR, "public_holdout.csv")
LOG = os.path.join(REG_DIR, "access_log.csv")
SEED = 20261119
YEARS = (2022, 2023, 2024, 2025, 2026)
FRACTION = 0.22                     # of every year x work-type stratum (~60 groups)
ED_TYPES = {"erosion", "drainage"}
FORWARD_JSON = os.path.join(ROOT, "handoff", "scratchpad", "fwd", "mac_recheck", "out",
                            "forward_narrow.json")

_cache = None


def holdout_files():
    """Set of held-out source files, as line_items' src_file spells them
    ("external/<owner>/<name>")."""
    global _cache
    if _cache is None:
        _cache = set()
        if os.path.exists(REG):
            with open(REG, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    _cache.add(r["src_file"])
    return _cache


def is_holdout(src_file):
    """True for a held-out file however the caller spells its path:
    "external/<owner>/<name>", "<owner>/<name>" (relative to data/external),
    "data/external/...", an absolute path, or a letting key "<file>::<sheet>".
    A bare file name (the company's files) is never held out."""
    s = (src_file or "").replace("\\", "/").split("::")[0]
    if "/data/external/" in s:
        s = "external/" + s.split("/data/external/", 1)[1]
    elif s.startswith("data/external/"):
        s = s[len("data/"):]
    hs = holdout_files()
    return s in hs or ("/" in s and ("external/" + s) in hs)


def _groups(files):
    """Union-find over copies of the same letting: md5 groups and dedup pairs."""
    parent = {f: f for f in files}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    p = os.path.join(DATA, "external", "_duplicates.csv")
    if os.path.exists(p):
        by_md5 = collections.defaultdict(list)
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            by_md5[r["md5"]].append("external/" + r["file"])
        for fs in by_md5.values():
            for f in fs[1:]:
                union(fs[0], f)
    p = os.path.join(DATA, "clean", "duplicate_lettings.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            names = []
            for k in ("file", "sheet_a", "sheet_b", "dropped", "kept"):
                v = (r.get(k) or "").split("::")[0]
                if v.startswith("external/"):
                    names.append(v)
            for v in names[1:]:
                union(names[0], v)
    out = collections.defaultdict(set)
    for f in list(parent):
        out[find(f)].add(f)
    return {f: out[find(f)] for f in parent}


def select():
    if os.path.exists(REG):
        sys.exit("refusing: %s exists -- the test set is locked. Re-drawing it spends it; "
                 "only do that on purpose, by moving the file away and logging why." % REG)
    import pandas as pd
    pl = json.load(open(FORWARD_JSON))["per_letting"]
    muni = [r for r in pl if r["market"] == "municipal"]
    pm = pd.read_csv(os.path.join(DATA, "clean", "master_panel.csv"),
                     usecols=["project_key", "work_type", "owner", "source_set"], dtype=str).fillna("")
    meta = pm.drop_duplicates("project_key").set_index("project_key")
    files = collections.defaultdict(lambda: {"keys": [], "dates": [], "wt": set(), "owner": ""})
    for r in muni:
        k, d = r["project_key"], r["date"]
        f = k.split("::")[0]
        if not f.startswith("external/"):
            continue                                  # company files are test-only already
        e = files[f]
        e["keys"].append(k)
        e["dates"].append(d)
        if k in meta.index:
            e["wt"].add(meta.at[k, "work_type"])
            e["owner"] = meta.at[k, "owner"]
    grp = _groups(list(files))
    units = {}                                        # group root -> unit
    for f, e in files.items():
        g = min(grp.get(f, {f}))
        u = units.setdefault(g, {"files": set(), "keys": [], "dates": [], "wt": set(), "owner": e["owner"]})
        u["files"] |= grp.get(f, {f})
        u["keys"] += e["keys"]
        u["dates"] += e["dates"]
        u["wt"] |= e["wt"]
    chosen = []
    for y in YEARS:
        pool = sorted(g for g, u in units.items() if min(u["dates"])[:4] == str(y))
        rng = random.Random(SEED + y)
        ed = [g for g in pool if units[g]["wt"] & ED_TYPES]
        other = [g for g in pool if g not in ed]
        rng.shuffle(ed)
        rng.shuffle(other)
        take = ed[:round(FRACTION * len(ed))] + other[:round(FRACTION * len(other))]
        chosen += [(y, g) for g in take]
        print("  %d: erosion/drainage %d of %d, other %d of %d" % (
            y, round(FRACTION * len(ed)), len(ed), round(FRACTION * len(other)), len(other)))
    os.makedirs(REG_DIR, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    cols = ["src_file", "group", "year", "first_bid_date", "owner", "work_types",
            "erosion_drainage", "letting_keys", "selected_at", "seed"]
    rows = []
    for y, g in chosen:
        u = units[g]
        for f in sorted(u["files"]):
            rows.append({"src_file": f, "group": g, "year": y, "first_bid_date": min(u["dates"]),
                         "owner": u["owner"], "work_types": "|".join(sorted(u["wt"])),
                         "erosion_drainage": "Y" if u["wt"] & ED_TYPES else "",
                         "letting_keys": " || ".join(sorted(u["keys"])),
                         "selected_at": stamp, "seed": SEED})
    with open(REG, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    md5 = hashlib.md5(open(REG, "rb").read()).hexdigest()
    new_log = not os.path.exists(LOG)
    with open(LOG, "a", encoding="utf-8-sig" if new_log else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["when", "action", "decision_registered_in", "result"])
        w.writerow([stamp, "drawn and locked, version 2 (md5 %s); no letting of it has been scored"
                    % md5, "", ""])
    n_lettings = sum(len(units[g]["keys"]) for _, g in chosen)
    by_year = collections.Counter(y for y, _ in chosen)
    ed_n = sum(1 for _, g in chosen if units[g]["wt"] & ED_TYPES)
    print("locked %d groups (%d files, %d scorable lettings), by year %s, erosion/drainage %d; md5 %s"
          % (len(chosen), len(rows), n_lettings, dict(sorted(by_year.items())), ed_n, md5))
    return md5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.select:
        select()
    if a.show:
        for f in sorted(holdout_files()):
            print(f)


if __name__ == "__main__":
    main()
