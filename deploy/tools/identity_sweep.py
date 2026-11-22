"""Broad identity check: price many public lettings' schedules through the
UI's estimator on the production tree and on the public package, and compare
every number.

    # 1. schedules from production's public lettings (read once, saved):
    .venv/bin/python deploy/tools/identity_sweep.py make PAYLOADS.json [--n 400]
    # 2. price them in a tree (the repository, or the public package):
    PY deploy/tools/identity_sweep.py run ROOT PAYLOADS.json OUT.json [--public]
    # 3. compare:
    .venv/bin/python deploy/tools/identity_sweep.py compare A.json B.json

A schedule is one public letting as bid: its lines (item no, description,
unit, quantity), market, bid date, bidder count, owner and work type, priced
as a new schedule on that date (comparables only from earlier lettings).
Lettings touched by the bundle's redactions (deploy/tools/build_bundle.py)
are left out -- their text differs by design. The comparison walks every
result: every number must be equal exactly; strings are compared unless
they carry a scan hit on the production side. Counts only are printed.

Three string fields may differ by design and do not fail the comparison:
source/local_path and source/local_kind (links to documents on this
machine, which the public package does not have) and engine/data (the
modification time of master_panel.csv: a bundle cut from an earlier,
byte-identical build carries an earlier time).
"""

from __future__ import annotations

import collections
import csv
import json
import os
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
csv.field_size_limit(10 ** 9)
VOLATILE = {"elapsed_s", "generated", "today", "stamp", "load_s",
            "history_s", "rss_mb", "peak_rss_mb"}
#: string fields that may differ between a working copy and the package
BY_DESIGN = ("/source/local_path", "/source/local_kind", "/result/engine/data")


def make(out, n=400):
    sys.path.insert(0, TOOLS)
    import scan_public as sp
    sys.path.insert(0, os.path.join(REPO, "src"))
    import estimate_low_bid as E
    lets = collections.OrderedDict()
    tainted = set()
    with open(os.path.join(REPO, "data", "clean", "master_panel.csv"),
              encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["source_set"] != "external":
                continue
            pk = r["project_key"]
            if any(sp.ANY.search(r.get(c) or "") for c in (
                    "project_key", "owner", "description", "desc_norm",
                    "bidder", "bidder_norm", "item_key")):
                tainted.add(pk)
            L = lets.get(pk)
            if L is None:
                L = lets[pk] = {"source": r["source"], "owner": r["owner"],
                                "wt": r.get("work_type") or "", "dates": set(),
                                "bidders": set(), "lines": {}, "first": None}
            L["dates"].add(r["bid_date"])
            if (r.get("is_engineer_estimate") or "").lower() in ("true", "1",
                                                                 "yes"):
                continue
            b = r.get("bidder_norm") or r.get("bidder")
            L["bidders"].add(b)
            if L["first"] is None:
                L["first"] = b
            if b != L["first"]:
                continue
            try:
                q = float(r["quantity"])
            except (TypeError, ValueError):
                continue
            k = (r.get("item_no") or "", r["description"], r.get("unit") or "", q)
            L["lines"].setdefault(k, None)
    pool = []
    for pk, L in lets.items():
        if pk in tainted or len(L["bidders"]) < 2 or not (3 <= len(L["lines"]) <= 400):
            continue
        d = max((x for x in L["dates"] if x and len(x) == 10), default="")
        if not d or d < "2021-01-01":
            continue
        mk = "municipal" if L["source"] in E.MUNICIPAL_SOURCES else "txdot"
        pool.append((mk, d, pk, L))
    muni = [p for p in pool if p[0] == "municipal"]
    tx = [p for p in pool if p[0] == "txdot"]
    want_m, want_t = int(n * 0.65), n - int(n * 0.65)
    pick = (muni[::max(1, len(muni) // want_m)][:want_m] +
            tx[::max(1, len(tx) // want_t)][:want_t])
    payloads = []
    for mk, d, pk, L in pick:
        items = [{"row_id": "r%d" % i, "item_no": k[0], "description": k[1],
                  "unit": k[2], "quantity": k[3]}
                 for i, k in enumerate(L["lines"], 1)]
        payloads.append({"market": mk, "n_bidders": len(L["bidders"]),
                         "owner": L["owner"] if mk == "municipal" else "",
                         "work_type": L["wt"] if mk == "municipal" else "",
                         "bid_date": d, "items": items})
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payloads, fh)
    print("lettings %d, left out (redacted) %d, eligible %d (municipal %d, "
          "TxDOT %d); written %d schedules, %d lines"
          % (len(lets), len(tainted), len(pool), len(muni), len(tx),
             len(payloads), sum(len(p["items"]) for p in payloads)))


def run(root, payloads, out, public=False):
    if public:
        os.environ["BIDEST_PUBLIC"] = "1"
    sys.path.insert(0, os.path.join(root, "ui"))
    import app
    import public_mode as pm
    pm.configure()
    t0 = time.time()
    app.ENGINE.load()
    assert app.ENGINE.state["ready"], app.ENGINE.state["error"]
    load_s = time.time() - t0
    P = json.load(open(payloads, encoding="utf-8"))
    res, errs = [], 0
    t1 = time.time()
    for p in P:
        st, ct, body, _ = app.dispatch("POST", "/api/estimate",
                                       {"Content-Type": "application/json"},
                                       json.dumps(p).encode())
        r = json.loads(body)
        if st != 200:
            errs += 1
        res.append({"status": st, "result": r})
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"root": "public" if public else "production",
                   "public_mode": pm.enabled(), "load_s": round(load_s, 1),
                   "price_s": round(time.time() - t1, 1), "results": res}, fh)
    print("%s: %d schedules priced in %.1fs (load %.1fs), %d errors"
          % ("public" if public else "production", len(res),
             time.time() - t1, load_s, errs))


def compare(a, b):
    sys.path.insert(0, TOOLS)
    import scan_public as sp
    A = json.load(open(a, encoding="utf-8"))["results"]
    B = json.load(open(b, encoding="utf-8"))["results"]
    assert len(A) == len(B)
    c = collections.Counter()
    diffs = collections.Counter()

    def walk(x, y, path):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in set(x) | set(y):
                if k in VOLATILE:
                    continue
                if k not in x or k not in y:
                    diffs["key missing: " + path + "/" + k] += 1
                    continue
                walk(x[k], y[k], path + "/" + k)
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                diffs["list length: " + path] += 1
                return
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, path + "[]")
        elif isinstance(x, bool) or x is None:
            c["other"] += 1
            if x != y:
                diffs["value: " + path] += 1
        elif isinstance(x, (int, float)):
            c["numbers"] += 1
            if x != y:
                diffs["number: " + path] += 1
        elif isinstance(x, str):
            if sp.ANY.search(x):
                c["strings with a hit (skipped)"] += 1
                return
            c["strings"] += 1
            if x != y:
                diffs["string: " + path] += 1
        else:
            if x != y:
                diffs["other: " + path] += 1

    same_headline = 0
    for x, y in zip(A, B):
        walk(x, y, "")
        hx = (x["result"].get("headline") or {})
        hy = (y["result"].get("headline") or {})
        same_headline += (hx.get("p50") == hy.get("p50")
                          and hx.get("p80") == hy.get("p80"))
    print("schedules %d; P50 and P80 identical in %d" % (len(A), same_headline))
    print("leaves compared: %s" % dict(c))
    allowed = {k: n for k, n in diffs.items()
               if k.startswith("string: ") and k.endswith(BY_DESIGN)}
    real = {k: n for k, n in diffs.items() if k not in allowed}
    print("differences: %d (by design: %d)" % (sum(real.values()),
                                               sum(allowed.values())))
    for k, n in sorted(real.items(), key=lambda kv: -kv[1])[:25]:
        print("   %6d  %s" % (n, k))
    for k, n in sorted(allowed.items(), key=lambda kv: -kv[1]):
        print("   %6d  by design: %s" % (n, k[len("string: "):]))
    return 0 if not real and same_headline == len(A) else 1


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "make":
        n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 400
        make(sys.argv[2], n)
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3], sys.argv[4], "--public" in sys.argv)
    elif cmd == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
