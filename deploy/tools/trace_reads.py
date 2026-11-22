"""Trace every file the UI and the estimator open while serving a public
site's requests -- the bundle is built from this list, not from a guess.

    .venv/bin/python deploy/tools/trace_reads.py ROOT OUT.json [--public]
        [--samples DIR]      # takeoff files to upload (csv / xlsx / pdf)

ROOT is a tree with ui/, src/ and data/clean/ (the repository, or the public
package). An audit hook records each open() (and directory listing, and
subprocess) while this script, in one process:

  1. imports ui/app.py and loads everything the server loads in the
     background (estimator tables, reference index, letting index, history
     search index);
  2. calls every public route through app.dispatch(): status, options,
     takeoff text and file uploads, classify, history search, bidder
     suggestion, estimates for a municipal and a TxDOT schedule,
     references for every line, worksheet references and exports, the
     estimate exports, a full bid tabulation and its CSV;
  3. writes the files opened under ROOT (relative paths, with counts), the
     programs run, the project modules imported and the third-party
     packages imported.

Reads only. pyarrow / C-level reads do not pass the audit hook; the module
list says whether pyarrow was even imported.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root")
    ap.add_argument("out")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--samples", default="")
    a = ap.parse_args(argv)
    root = os.path.realpath(a.root)
    if a.public:
        os.environ["BIDEST_PUBLIC"] = "1"
    opened = collections.Counter()
    listed = collections.Counter()
    progs = collections.Counter()
    outside = collections.Counter()

    def hook(event, args):
        if event == "open":
            p = args[0]
            if isinstance(p, int):
                return
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
            p = os.path.realpath(os.path.join(os.getcwd(), str(p)))
            if p.startswith(root + os.sep):
                opened[os.path.relpath(p, root)] += 1
            elif "/site-packages/" not in p and "/lib/python3" not in p \
                    and not p.startswith(("/dev/", "/proc/", "/sys/")):
                outside[p] += 1
        elif event in ("os.listdir", "os.scandir", "glob.glob"):
            p = args[0] if args else "."
            if isinstance(p, (str, bytes)):
                p = p.decode() if isinstance(p, bytes) else p
                p = os.path.realpath(os.path.join(os.getcwd(), p or "."))
                if p.startswith(root):
                    listed[os.path.relpath(p, root)] += 1
        elif event == "subprocess.Popen":
            exe = args[0]
            progs[os.path.basename(str(exe))] += 1

    sys.addaudithook(hook)
    sys.path.insert(0, os.path.join(root, "ui"))
    t0 = time.time()
    import app  # noqa: E402
    import public_mode as pm  # noqa: E402
    pm.configure()
    import engine_production as ep  # noqa: E402

    E = app.ENGINE
    E.load()
    assert E.state["ready"], E.state["error"]
    E.load_references()
    E.tabs.ensure()
    import matching  # noqa: E402
    matching.index()
    app._matching_state["ready"] = True
    load_s = time.time() - t0

    def call(method, path, body=None, ctype="application/json"):
        data = (json.dumps(body).encode() if isinstance(body, dict)
                else (body or b""))
        st, ct, out, _ = app.dispatch(method, path, {"Content-Type": ctype},
                                      data)
        res = json.loads(out) if "json" in ct else out
        return st, res

    results = {}
    for p in ("/api/status", "/api/options", "/api/health",
              "/api/corrections", "/api/claude/status"):
        results[p] = call("GET", p)[0]
    st, parsed = call("POST", "/api/takeoff/text", {"text": "\n".join(
        "%s\t%s\t%s\t%s" % r[:4] for r in __import__("run").CHECK_SCHEDULE)})
    results["takeoff/text"] = st
    # uploads
    ups = {}
    if a.samples and os.path.isdir(a.samples):
        for fn in sorted(os.listdir(a.samples)):
            fp = os.path.join(a.samples, fn)
            with open(fp, "rb") as fh:
                blob = fh.read()
            boundary = "----trace%d" % time.time_ns()
            body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                     "filename=\"%s\"\r\nContent-Type: application/octet-stream"
                     "\r\n\r\n" % (boundary, fn)).encode() + blob +
                    ("\r\n--%s--\r\n" % boundary).encode())
            st, r = call("POST", "/api/takeoff/file", body,
                         "multipart/form-data; boundary=%s" % boundary)
            ups[fn] = {"status": st, "method": (r or {}).get("method")
                       if isinstance(r, dict) else None,
                       "lines": len((r or {}).get("lines") or [])
                       if isinstance(r, dict) else None}
    results["uploads"] = ups
    lines = parsed["lines"]
    st, cls = call("POST", "/api/classify", {"market": "municipal", "lines": [
        {"row_id": l["row_id"], "description": l["description"],
         "unit": l["unit"], "quantity": l["quantity"]} for l in lines]})
    results["classify"] = st
    results["history"] = call("GET", "/api/history/search?q=riprap&unit=SY")[0]
    import run  # noqa: E402
    items = [{"row_id": "r%s" % n, "item_no": n, "description": d,
              "unit": u, "quantity": q, "amount": am, "quote": qu}
             for n, d, u, q, am, qu in run.CHECK_SCHEDULE]
    est = {}
    for market, wt, owner in (("municipal", "erosion", ""),
                              ("txdot", "", "")):
        payload = {"market": market, "n_bidders": "auto", "owner": owner,
                   "work_type": wt, "bid_date": "2026-03-02",
                   "items": items}
        results["bidders/" + market] = call("POST", "/api/bidders/suggest",
                                            payload)[0]
        payload["n_bidders"] = 4
        st, r = call("POST", "/api/estimate", payload)
        est[market] = (payload, r)
        results["estimate/" + market] = st
        for it in items:
            results.setdefault("references", []).append(call(
                "POST", "/api/references",
                {"request": payload, "row_id": it["row_id"]})[0])
        st, wr = call("POST", "/api/worksheet/refs", {"request": payload})
        results["worksheet/refs/" + market] = st
        ws = {"lines": [], "contingency_pct": 10}
        for k in ("csv", "xlsx"):
            results["export/%s/%s" % (k, market)] = call(
                "POST", "/api/export/" + k, {"result": r, "request": payload})[0]
            results["worksheet/export/%s/%s" % (k, market)] = call(
                "POST", "/api/worksheet/export/" + k,
                {"result": r, "request": payload, "worksheet": ws})[0]
    # a full bid tabulation: the first similar card of the municipal estimate
    r = est["municipal"][1]
    keys = [c.get("key") for c in (r.get("similar") or {}).get("cards") or []
            if c.get("key")]
    if keys:
        from urllib.parse import quote
        results["letting"] = call("GET", "/api/letting?key=" + quote(keys[0]))[0]
        results["letting/csv"] = call("GET", "/api/letting/csv?key="
                                      + quote(keys[0]))[0]
    mods_project = sorted(
        os.path.relpath(os.path.realpath(m.__file__), root)
        for m in list(sys.modules.values())
        if getattr(m, "__file__", None)
        and os.path.realpath(m.__file__).startswith(root + os.sep))
    third = sorted({m.__name__.split(".")[0] for m in list(sys.modules.values())
                    if getattr(m, "__file__", None)
                    and "site-packages" in (m.__file__ or "")})
    rep = {"root": root, "public": pm.enabled(), "load_s": round(load_s, 1),
           "results": results,
           "opened": dict(sorted(opened.items())),
           "listed": dict(sorted(listed.items())),
           "outside_root": dict(sorted(outside.items())),
           "programs": dict(progs), "project_modules": mods_project,
           "third_party": third,
           "estimates": {m: {"p50": v[1]["headline"]["p50"],
                             "p80": v[1]["headline"]["p80"]}
                         for m, v in est.items()}}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    data_files = sorted(p for p in opened if p.startswith("data/"))
    print("loaded in %.1fs; public=%s" % (load_s, pm.enabled()))
    print("data files opened (%d):" % len(data_files))
    for p in data_files:
        print("   ", p)
    print("other non-code files opened:",
          sorted(p for p in opened if not p.startswith("data/")
                 and not p.endswith((".py", ".pyc"))))
    print("outside root:", len(outside), "| programs:", dict(progs))
    print("statuses:", {k: v for k, v in results.items()
                        if not isinstance(v, (list, dict))})
    print("upload results:", ups)
    print("estimates:", rep["estimates"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
