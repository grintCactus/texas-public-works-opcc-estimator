"""Start the estimator locally.

    python ui/run.py            # starts on 127.0.0.1:8017 and opens a browser
    python ui/run.py --port 9000 --no-browser
    python ui/run.py --check    # load everything, run one estimate, exit

Binds the loopback interface only. There is no auth because there is no
network surface -- do not change the host without adding one.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_PORT = 8017


def check() -> int:
    """Load every table and price a known line. Exit non-zero on failure."""
    import data_access as da
    import matching
    import pricing
    import upstream

    print("python     :", sys.version.split()[0])
    print("clean dir  :", da.CLEAN_DIR)
    t0 = time.time()
    report = da.warm(lambda i, n, name: print("  loading %-22s %d/%d" % (name, i + 1, n)))
    print("loaded in  : %.1fs" % (time.time() - t0))

    failed = []
    for name, info in report.items():
        if info.get("error"):
            failed.append(name)
            print("  FAIL %-22s %s" % (name, info["error"][:200]))
        else:
            print("  ok   %-22s %s rows" % (name, info.get("rows", "-")))

    print("parser     :", "linked" if upstream.PARSER_AVAILABLE
          else "UNAVAILABLE (%s)" % upstream.IMPORT_ERROR)
    print("ocr        :", "available" if upstream.OCR_AVAILABLE
          else "not installed (%s)" % upstream.OCR_IMPORT_ERROR)

    t0 = time.time()
    matching.index()
    print("match index: %.1fs" % (time.time() - t0))

    engine = pricing.PriceEngine()
    req = pricing.EstimateRequest(
        items=[
            pricing.LineItemInput("r1", "Mobilization", "LS", 1),
            pricing.LineItemInput("r2", "Unclassified Channel Excavation", "CY", 4200),
            pricing.LineItemInput("r3", "Grouted Rock Riprap, 18 inch", "SY", 2600),
        ],
        n_bidders=4, work_type="erosion_creek")
    t0 = time.time()
    res = engine.estimate(req)
    print("estimate   : %.2fs" % (time.time() - t0))
    for line in res.lines:
        print("  %-34s %-15s P50 %s" % (
            line.description[:34], line.treatment,
            ("%.2f/%s" % (line.unit_price.p50, line.unit)
             if line.unit_price.p50 else
             ("%.2f%% of cost" % line.pct_of_construction.p50
              if line.pct_of_construction.p50 else "-"))))
    pt = res.project_total
    if pt.p50:
        print("  project    P10 %,.0f  P50 %,.0f  P90 %,.0f".replace(",", "")
              % (pt.p10, pt.p50, pt.p90))
    else:
        print("  project    NOT PRICED")
        failed.append("estimate")

    # The comparables path is what makes the tool trustworthy, so check it too.
    keyed = [l for l in res.lines if l.comparables_key]
    if keyed:
        c = engine.comparables(keyed[-1].comparables_key, unit=keyed[-1].unit, limit=3)
        print("comparables: %d shown of %d total" % (len(c["rows"]), c["n_total"]))
        if not c["rows"]:
            print("  WARNING no comparables for %s" % keyed[-1].comparables_key)

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
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="load data, price a sample, run the tests, exit")
    ap.add_argument("--no-tests", action="store_true",
                    help="with --check, skip the regression suite")
    ap.add_argument("--test", action="store_true",
                    help="run the regression suite only")
    ap.add_argument("--reload", action="store_true", help="auto-reload on edit")
    args = ap.parse_args()

    if args.test:
        return run_tests()
    if args.check:
        rc = check()
        if rc or args.no_tests:
            return rc
        print("\n" + "-" * 60 + "\nregression suite\n" + "-" * 60)
        return run_tests()

    import uvicorn

    url = "http://%s:%d/" % (args.host, args.port)
    if not args.no_browser:
        def open_later():
            # Data load takes a few seconds on a cold cache; give it a head start
            # so the first paint is not an error toast.
            time.sleep(2.5)
            webbrowser.open(url)
        threading.Thread(target=open_later, daemon=True).start()

    print("Bid Estimator -> %s   (ctrl-c to stop)" % url)
    uvicorn.run("app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info",
                app_dir=HERE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
