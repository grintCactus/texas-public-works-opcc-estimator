"""Wiring check: one takeoff priced through the UI's HTTP API and through the
production estimator's own entry point must give identical totals.

    .venv/bin/python ui/integration_check.py TAKEOFF.csv \\
        --market municipal --bidders 5 --bid-date 2025-10-09 \\
        [--owner legistar/coppell] [--work-type erosion] \\
        [--amount 126=80000]          # an owner-set allowance, repeatable

1. UI path: starts the UI server in this process on a free loopback port,
   uploads the file to POST /api/takeoff/file, classifies the lines with
   POST /api/classify and prices them with POST /api/estimate -- real HTTP.
   Owner-set amounts come from --amount (by item number) or, as in the page,
   from the file's amount / unit-price column on allowance lines.
2. Production path: writes the same lines, with the classification the UI
   reported (family, unit, size, desc_norm) and the same owner-set amounts
   (amount + owner_fixed=yes, as run_schedule reads them), to a schedule CSV
   in a temporary
   directory and runs estimate_low_bid.run_schedule() on it -- the CLI's own
   code path (`estimate_low_bid.py --schedule`), which loads its own tables.
3. Compares P50, trial P70, the retained P80 baseline, the market total and
   the complete totals exactly.

A job whose own bids are already in the price history is a wiring check,
not an accuracy test. Nothing is written into the repository; the takeoff
file is read where it is.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _post(base, path, body, ctype="application/json"):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _multipart(name, data):
    boundary = "----bidestimator%d" % time.time_ns()
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; '
            'filename="%s"\r\nContent-Type: text/csv\r\n\r\n'
            % (boundary, name)).encode("utf-8")
    return (head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii"),
            "multipart/form-data; boundary=%s" % boundary)


def ui_path(path, market, n, bid_date, owner, work_type, amounts=None):
    """(result, lines, classification, owner-set amounts by row id) through
    the HTTP API. `amounts`: {item_no: amount}."""
    amounts = amounts or {}
    import app
    srv = app.make_server("127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        app.ENGINE.load()
        with open(path, "rb") as fh:
            body, ctype = _multipart("takeoff" + os.path.splitext(path)[1],
                                     fh.read())
        parsed = _post(base, "/api/takeoff/file", body, ctype)
        lines = parsed["lines"]
        cls = _post(base, "/api/classify", json.dumps(
            {"market": market,
             "lines": [{"row_id": l["row_id"], "description": l["description"],
                        "unit": l["unit"], "quantity": l["quantity"],
                        "file_amount": l.get("amount"),
                        "file_unit_price": l.get("unit_price")}
                       for l in lines]}).encode())["lines"]
        by = {c["row_id"]: c for c in cls}
        amt = {}
        for l in lines:
            a = amounts.get(l["item_no"])
            if a is None:
                a = by[l["row_id"]].get("import_amount")
            if a:
                amt[l["row_id"]] = float(a)
        payload = {"market": market, "n_bidders": n, "bid_date": bid_date,
                   "owner": owner, "work_type": work_type,
                   "items": [{"row_id": l["row_id"], "item_no": l["item_no"],
                              "description": l["description"],
                              "unit": l["unit"], "quantity": l["quantity"],
                              "amount": amt.get(l["row_id"])}
                             for l in lines]}
        res = _post(base, "/api/estimate", json.dumps(payload).encode())
        return res, lines, cls, amt
    finally:
        srv.shutdown()
        srv.server_close()


def production_path(lines, cls, market, n, bid_date, owner, work_type,
                    amounts=None):
    """run_schedule() on a schedule CSV holding the same classification and
    the same owner-set amounts ({row_id: amount}, written as amount +
    owner_fixed=yes)."""
    amounts = amounts or {}
    import upstream
    E = upstream.estimator
    by = {c["row_id"]: c for c in cls}
    d = tempfile.mkdtemp(prefix="ui_integration_")
    sched = os.path.join(d, "schedule.csv")
    try:
        with open(sched, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["item_no", "description", "desc_norm", "item_family",
                        "unit", "quantity", "size_bucket", "size_in",
                        "amount", "owner_fixed"])
            for l in lines:
                c = by[l["row_id"]]
                w.writerow([l["item_no"], l["description"], c["desc_norm"],
                            c["family"], c["unit"],
                            repr(float(l["quantity"] or 0.0)),
                            c["size_bucket"],
                            "" if c["size_in"] is None else repr(c["size_in"]),
                            repr(float(amounts[l["row_id"]]))
                            if amounts.get(l["row_id"]) else "",
                            "yes" if amounts.get(l["row_id"]) else ""])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = E.run_schedule(sched, n, market,
                               bid_date[:4] if bid_date else None, bid_date,
                               owner or None, work_type or None)
        return r, out.getvalue()
    finally:
        try:
            os.remove(sched)
            os.rmdir(d)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("takeoff")
    ap.add_argument("--market", default="municipal")
    ap.add_argument("--bidders", type=int, required=True)
    ap.add_argument("--bid-date", required=True)
    ap.add_argument("--owner", default="")
    ap.add_argument("--work-type", default="")
    ap.add_argument("--amount", action="append", default=[],
                    metavar="ITEM=AMOUNT",
                    help="an owner-set amount for the line with this item "
                         "number (repeatable)")
    ap.add_argument("--json", help="also write the comparison to this file")
    a = ap.parse_args(argv)

    amounts = {}
    for x in a.amount:
        k, _, v = x.partition("=")
        amounts[k.strip()] = float(v.replace(",", ""))
    t0 = time.time()
    res, lines, cls, amt = ui_path(a.takeoff, a.market, a.bidders,
                                   a.bid_date, a.owner, a.work_type, amounts)
    t1 = time.time()
    r, printout = production_path(lines, cls, a.market, a.bidders, a.bid_date,
                                  a.owner, a.work_type, amt)
    t2 = time.time()

    nc = res["not_covered"]
    pairs = [
        ("P50 (expected low bid)", res["headline"]["p50"], r.get("expected_low")),
        ("P70 (trial OPCC)", res["headline"].get("p70"), r.get("expected_low_p70")),
        ("P80 (retained baseline)", res["headline"]["p80"],
         r.get("expected_low_p80")),
        ("market total (all bids)", res["totals"]["market_total"],
         r.get("market_total")),
        ("owner-set money", res["totals"]["fixed_total"], r.get("fixed_total")),
        ("complete total at P50", nc["complete_total"], r.get("complete_total")),
        ("complete total at P70", nc.get("complete_total_p70"),
         r.get("complete_total_p70")),
        ("complete total at P80", nc["complete_total_p80"],
         r.get("complete_total_p80")),
    ]
    same = all(u == p for _, u, p in pairs)
    lines_same = [t.get("price") for t in r["trace"]] == \
        [l["market_unit_price"] if l["status"] == "priced" else None
         for l in res["lines"]]
    print("takeoff     : %s (%d lines)" % (os.path.basename(a.takeoff),
                                           len(lines)))
    print("inputs      : market %s, %d bidders, bid date %s, owner %r, work "
          "type %r" % (a.market, a.bidders, a.bid_date, a.owner, a.work_type))
    print("owner-set   : %s" % (", ".join(
        "%s = %s" % (l["item_no"], "{:,.2f}".format(amt[l["row_id"]]))
        for l in lines if l["row_id"] in amt) or "none"))
    print("%-26s %20s %20s" % ("", "UI (HTTP API)", "run_schedule()"))
    for lab, u, p in pairs:
        f = (lambda v: "-" if v is None else "{:,.6f}".format(v))
        print("%-26s %20s %20s  %s" % (lab, f(u), f(p),
                                        "same" if u == p else "DIFFERENT"))
    print("line prices : %s" % ("identical" if lines_same else "DIFFERENT"))
    print("time        : UI %.1fs, production %.1fs" % (t1 - t0, t2 - t1))
    print("RESULT      : %s" % ("IDENTICAL" if same and lines_same
                                else "MISMATCH"))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"pairs": pairs, "identical": same and lines_same,
                       "inputs": vars(a)}, fh, indent=1)
    return 0 if same and lines_same else 1


if __name__ == "__main__":
    sys.exit(main())
