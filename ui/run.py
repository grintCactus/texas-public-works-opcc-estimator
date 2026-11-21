"""Start the estimator locally.

    .venv/bin/python ui/run.py                 # 127.0.0.1:8017, opens a browser
    .venv/bin/python ui/run.py --port 9000 --no-browser
    .venv/bin/python ui/run.py --check         # load, price a sample through
                                               # the API, then run the tests
    .venv/bin/python ui/run.py --test          # the regression suite only
    BIDEST_PUBLIC=1 .venv/bin/python ui/run.py # public demo (or --public):
                                               # 0.0.0.0:$PORT (7860)

Locally it binds the loopback interface only. There is no auth because there
is no network surface -- do not change the host without public mode, which
adds the protections a public site needs (ui/public_mode.py: no server-side
writes, no Claude reader, no local files, optional access code, rate limits,
upload cap). The server is the Python standard library (see app.py); nothing
needs installing beyond the project's own venv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_PORT = 8017

#: A small creek job used by --check: a mobilization lump sum, unit-priced
#: channel work, an owner-set allowance with its amount, and a pump -- a
#: not-covered specialty line, with a vendor quote.
CHECK_SCHEDULE = [
    ("1", "Mobilization", "LS", 1, None, None),
    ("2", "Unclassified Channel Excavation", "CY", 4200, None, None),
    ("3", "Grouted Rock Riprap, 18 inch", "SY", 2600, None, None),
    ("4", "Gabion Mattress, 12 inch", "SY", 880, None, None),
    ("5", "Block Sod, Bermuda", "SY", 9500, None, None),
    ("6", "Owner's Contingency Allowance", "LS", 1, 50000.0, None),
    ("7", "Submersible Pump, 15 HP", "EA", 2, None, 84000.0),
]


def check() -> int:
    """Load the production estimator, price a known schedule through the
    API and directly, and check the two agree. Exit non-zero on failure."""
    import app
    import engine_production as ep
    import upstream

    failed = []
    print("python      :", sys.version.split()[0])
    print("data/clean  :", ep.CLEAN)
    st = upstream.status()
    print("parser      :", ("linked (%s)" % st["parser_version"])
          if st["parser_available"] else "UNAVAILABLE (%s)" % st["parser_error"])
    print("size bucket :", st["size_bucket_source"])
    print("estimator   :", "src/estimate_low_bid.py md5 %s"
          % upstream.src_md5("estimate_low_bid.py")
          if st["estimator_available"]
          else "UNAVAILABLE (%s)" % st["estimator_error"])
    if not (st["parser_available"] and st["estimator_available"]):
        failed.append("upstream")
    print("ocr         :", "available" if st["ocr_available"]
          else "not installed (%s)" % st["ocr_error"])

    t0 = time.time()
    app.ENGINE.load()
    s = app.ENGINE.state
    if not s["ready"]:
        print("\nFAILED: estimator did not load: %s" % s["error"])
        return 1
    info = app.ENGINE.info
    print("loaded in   : %.1fs (tables %.1fs + bidder history %.1fs); RSS "
          "%s MB, peak %s MB" % (time.time() - t0, s["load_s"],
                                 s["history_s"], s["rss_mb"],
                                 s["peak_rss_mb"]))
    print("rule        :", info["estimator"]["rule"])
    print("data        : master_panel.csv %s, latest letting %s, pools %s"
          % (info["data"]["master_panel_mtime"], info["data"]["latest_letting"],
             info["data"]["pool_lettings"]))
    acc = app.ENGINE.accuracy.meta()
    print("item acc.   :", ("item_accuracy.json %s" % acc["generated"])
          if acc["available"] else "MISSING (%s)" % acc["error"])
    if not acc["available"]:
        failed.append("item_accuracy.json")

    items = [{"row_id": "r%s" % n, "item_no": n, "description": d,
              "unit": u, "quantity": q, "amount": a, "quote": qu}
             for n, d, u, q, a, qu in CHECK_SCHEDULE]
    payload = {"market": "municipal", "n_bidders": 4, "owner": "",
               "work_type": "erosion", "bid_date": "2026-03-02",
               "items": items}
    code, _, body, _ = app.dispatch(
        "POST", "/api/estimate", {"Content-Type": "application/json"},
        json.dumps(payload).encode())
    if code != 200:
        print("\nFAILED: /api/estimate answered %d: %s" % (code, body[:300]))
        return 1
    r = json.loads(body)
    h = r["headline"]
    print("estimate    : P50 %s  P80 %s  (%d lines, %.3fs)"
          % ("{:,.0f}".format(h["p50"]) if h["p50"] else "-",
             "{:,.0f}".format(h["p80"]) if h["p80"] else "-",
             len(r["lines"]), r["elapsed_s"]))
    for ln in r["lines"]:
        print("  %-3s %-36s %-11s %14s  %s"
              % (ln["item_no"], ln["description"][:36], ln["status"],
                 "{:,.2f}".format(ln["amount_p50"]) if ln["amount_p50"]
                 else "-", ln["accuracy"]["text"]))
    if not (h["p50"] and h["p80"] and h["p80"] > h["p50"]):
        failed.append("headline")
    s_lines = sum(ln["amount_p50"] or 0 for ln in r["lines"])
    if abs(s_lines - h["p50"]) > 1e-6 * h["p50"]:
        failed.append("lines do not add up to P50")
    nc = r["not_covered"]
    print("not covered : %d line(s), complete total %s"
          % (nc["n"], "{:,.0f}".format(nc["complete_total"])
             if nc["complete_total"] else "-"))
    if nc["n"] != 1 or nc["complete_total"] is None:
        failed.append("not-covered block")
    print("low conf.   : %s" % ", ".join(g["label"] for g in
                                         r["low_confidence"]["lines"]))
    print("similar     : %d past project(s)" % len(r["similar"]["cards"]))

    # the same input straight into estimate_low_bid.estimate()
    E = upstream.estimator
    q = app.ENGINE._request(payload)
    its, _ = app.ENGINE.build_items(q["raw"], "municipal")
    d = E.estimate(app.ENGINE.tab, its, 4, "municipal", "2026",
                   {"date": "2026-03-02", "owner": None, "work_type": "erosion"})
    same = (d["expected_low"] == h["p50"] and d["expected_low_p80"] == h["p80"])
    print("direct call : P50 %s  P80 %s  -> %s"
          % ("{:,.2f}".format(d["expected_low"]),
             "{:,.2f}".format(d["expected_low_p80"]),
             "identical" if same else "DIFFERENT"))
    if not same:
        failed.append("API vs direct estimate")

    t0 = time.time()
    app.ENGINE.load_references()
    if not app.ENGINE.refs_state["ready"]:
        print("references  : FAILED to load (%s)" % app.ENGINE.refs_state["error"])
        failed.append("references")
    else:
        ok = n = 0
        for it in items:
            rr = app.ENGINE.references(payload, it["row_id"])
            if rr.get("special"):
                continue
            n += 1
            rp = rr["reproduced"]
            ok += bool(rp["price_match"] and rp["rows_match"])
        print("references  : index %.1fs; %d of %d priced lines reproduce "
              "the estimator's price from their listed lettings"
              % (time.time() - t0, ok, n))
        if ok != n:
            failed.append("references")

    code, _, body, _ = app.dispatch("POST", "/api/estimate",
                                    {"Content-Type": "application/json"},
                                    json.dumps(dict(payload, opcc=1)).encode())
    print("OPCC input  : refused with %d" % code)
    if code != 400:
        failed.append("OPCC rejection")

    if failed:
        print("\nFAILED: %s" % ", ".join(failed))
        return 1
    print("\nall checks passed")
    return 0


def run_tests() -> int:
    """The regression suite. Kept in one command with --check."""
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Bid Estimator (local)")
    ap.add_argument("--port", type=int, default=None,
                    help="default %d; in public mode $PORT or 7860"
                         % DEFAULT_PORT)
    ap.add_argument("--host", default=None,
                    help="default 127.0.0.1; in public mode 0.0.0.0")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--public", action="store_true",
                    help="public-demo mode (same as BIDEST_PUBLIC=1)")
    ap.add_argument("--check", action="store_true",
                    help="load data, price a sample, run the tests, exit")
    ap.add_argument("--no-tests", action="store_true",
                    help="with --check, skip the regression suite")
    ap.add_argument("--test", action="store_true",
                    help="run the regression suite only")
    args = ap.parse_args()

    if args.test:
        return run_tests()
    if args.check:
        rc = check()
        if rc or args.no_tests:
            return rc
        print("\n" + "-" * 60 + "\nregression suite\n" + "-" * 60)
        return run_tests()

    import public_mode
    if args.public:
        os.environ["BIDEST_PUBLIC"] = "1"
        public_mode.configure()
    if public_mode.enabled():
        args.host = args.host or "0.0.0.0"
        args.port = args.port or public_mode.CFG.port
        args.no_browser = True
    args.host = args.host or "127.0.0.1"
    args.port = args.port or DEFAULT_PORT

    import app

    url = "http://%s:%d/" % (args.host, args.port)
    if not args.no_browser:
        def open_later():
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=open_later, daemon=True).start()

    print("Bid Estimator -> %s   (ctrl-c to stop)" % url)
    app.serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
