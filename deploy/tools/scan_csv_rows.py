"""Count scan hits inside a data/clean CSV, per column and per pattern index,
over the rows that would be published (source_set == "external" when the
file has that column). Prints counts only -- never a value or a pattern.

    .venv/bin/python deploy/tools/scan_csv_rows.py FILE.csv [FILE.csv ...]
"""
import collections, csv, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_public as sp
csv.field_size_limit(10 ** 9)

def run(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        h = next(rd)
        i_s = h.index("source_set") if "source_set" in h else None
        by_col = collections.Counter(); by_pat = collections.Counter()
        rows_hit = 0; rows = 0
        cache = {}
        for r in rd:
            if i_s is not None and r[i_s] != "external":
                continue
            rows += 1
            hit = False
            for j, v in enumerate(r):
                if not v:
                    continue
                res = cache.get(v)
                if res is None:
                    res = []
                    for k, rx in enumerate(sp.EMPLOYER):
                        if rx.search(v): res.append("employer#%d" % k)
                    for k, rx in enumerate(sp.COMPANY):
                        if rx.search(v): res.append("company#%d" % k)
                    for cat, rx in sp.FIXED.items():
                        if rx.search(v): res.append(cat)
                    if sp.EMAIL.search(v): res.append("email")
                    if len(cache) < 2_000_000:
                        cache[v] = res
                for c in res:
                    by_col[(h[j], c.split("#")[0])] += 1
                    by_pat[c] += 1
                    hit = True
            rows_hit += hit
    hh = [x for x in h]
    print("%s: %d published rows, %d with a hit" % (os.path.basename(path), rows, rows_hit))
    for (col, cat), n in sorted(by_col.items(), key=lambda x: -x[1]):
        print("    column %-28s %-10s %8d" % (col, cat, n))
    if by_pat:
        print("    by pattern index:", dict(sorted(by_pat.items())))

for p in sys.argv[1:]:
    run(p)
