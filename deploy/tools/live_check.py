"""Ask a running site to price a takeoff and check its published percentiles.

    python deploy/tools/live_check.py https://129-146-109-44.sslip.io
    python deploy/tools/live_check.py http://127.0.0.1:8017 --expect-p50 765836.50 --expect-p70 887482.17 --expect-p80 1043089.75

It uploads the takeoff to POST /api/takeoff/file, classifies the lines with
POST /api/classify and prices them with POST /api/estimate: three requests,
nothing stored on the server. The default takeoff is the public fixture
ui/fixtures/takeoff_public_creek_bank.csv with the inputs of the
known-answer check in docs/HANDOFF.md. Each requested percentile must be
present, finite and within half a cent of its expected value.

Use it after a deploy: the live site and a local run of the same commit and
bundle must give the same numbers.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import time
import urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
FIXTURE = os.path.join(REPO, "ui", "fixtures", "takeoff_public_creek_bank.csv")


def _context():
    """The default TLS context, or the system CA file where the Python
    build ships none (python.org builds on macOS)."""
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


def post(base, path, body, ctype="application/json"):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": ctype,
                                          "User-Agent": "opcc-estimator-live-check"})
    ctx = _context() if base.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def multipart(name, data):
    boundary = "----opcclivecheck%d" % time.time_ns()
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; '
            'filename="%s"\r\nContent-Type: text/csv\r\n\r\n'
            % (boundary, name)).encode("utf-8")
    return (head + data + ("\r\n--%s--\r\n" % boundary).encode("ascii"),
            "multipart/form-data; boundary=%s" % boundary)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("site", help="base address, e.g. https://129-146-109-44.sslip.io")
    ap.add_argument("--takeoff", default=FIXTURE)
    ap.add_argument("--market", default="municipal")
    ap.add_argument("--bidders", type=int, default=5)
    ap.add_argument("--bid-date", default="2025-03-10")
    ap.add_argument("--expect-p50", type=float)
    ap.add_argument("--expect-p70", type=float)
    ap.add_argument("--expect-p80", type=float)
    a = ap.parse_args(argv)
    base = a.site.rstrip("/")
    with open(a.takeoff, "rb") as fh:
        body, ctype = multipart("takeoff" + os.path.splitext(a.takeoff)[1], fh.read())
    lines = post(base, "/api/takeoff/file", body, ctype)["lines"]
    cls = post(base, "/api/classify", json.dumps({
        "market": a.market,
        "lines": [{"row_id": l["row_id"], "description": l["description"],
                   "unit": l["unit"], "quantity": l["quantity"],
                   "file_amount": l.get("amount"),
                   "file_unit_price": l.get("unit_price")} for l in lines]}).encode())["lines"]
    amount = {c["row_id"]: c.get("import_amount") for c in cls}
    res = post(base, "/api/estimate", json.dumps({
        "market": a.market, "n_bidders": a.bidders, "bid_date": a.bid_date,
        "owner": "", "work_type": "",
        "items": [{"row_id": l["row_id"], "item_no": l["item_no"],
                   "description": l["description"], "unit": l["unit"],
                   "quantity": l["quantity"],
                   "amount": float(amount[l["row_id"]]) if amount.get(l["row_id"]) else None}
                  for l in lines]}).encode())
    print("%s: %d lines, market %s, %d bidders, bid date %s"
          % (base, len(lines), a.market, a.bidders, a.bid_date))
    expected = {"p50": a.expect_p50, "p70": a.expect_p70, "p80": a.expect_p80}
    bad = []
    for key, want in expected.items():
        got = res["headline"].get(key)
        valid = isinstance(got, (int, float)) and math.isfinite(got)
        print("%s  %s" % (key.upper(), "{:,.2f}".format(got) if valid else "unavailable"))
        if want is not None and (not valid or not math.isfinite(want) or abs(got - want) > 0.005):
            bad.append(key.upper())
    if bad:
        print("RESULT: DIFFERENT (%s)" % ", ".join(bad))
        return 1
    if any(value is not None for value in expected.values()):
        print("RESULT: AS EXPECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
