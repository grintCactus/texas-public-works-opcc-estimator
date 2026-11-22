"""Start the server the way the container does and measure start-up time and
memory (RSS) at each loading stage, then under a few requests.

    PY deploy/tools/measure_server.py ROOT OUT.json [--public] [--port N]

Stages (from /api/status, polled twice a second): listening, estimator
ready (tables + comparables pools + bidder history), reference index ready,
history search ready; then 20 estimates, references for every line and a
full bid tabulation. RSS is sampled with ps every poll.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request


def rss_mb(pid):
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    root, out = sys.argv[1], sys.argv[2]
    public = "--public" in sys.argv
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv \
        else free_port()
    env = dict(os.environ, PORT=str(port), PYTHONDONTWRITEBYTECODE="1")
    if public:
        env["BIDEST_PUBLIC"] = "1"
    else:
        env.pop("BIDEST_PUBLIC", None)
    args = [sys.executable, "ui/run.py", "--port", str(port), "--no-browser",
            "--host", "127.0.0.1"] + (["--public"] if public else [])
    t0 = time.time()
    p = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    marks, samples, peak = {}, [], 0.0

    def status():
        with urllib.request.urlopen(base + "/api/status", timeout=10) as r:
            return json.loads(r.read())
    try:
        while time.time() - t0 < 600:
            r = rss_mb(p.pid)
            if r:
                peak = max(peak, r)
                samples.append((round(time.time() - t0, 2), round(r)))
            try:
                st = status()
            except OSError:
                time.sleep(0.5)
                continue
            el = round(time.time() - t0, 2)
            marks.setdefault("listening", (el, round(r or 0)))
            if st.get("ready"):
                marks.setdefault("estimator_ready", (el, round(r or 0)))
            if (st.get("references") or {}).get("ready"):
                marks.setdefault("references_ready", (el, round(r or 0)))
            if (st.get("history_search") or {}).get("ready"):
                marks.setdefault("history_ready", (el, round(r or 0)))
                break
            time.sleep(0.5)
        time.sleep(2)
        idle = rss_mb(p.pid)
        # a little use
        sys.path.insert(0, os.path.join(root, "ui"))
        import run as runmod
        items = [{"row_id": "r%s" % n, "item_no": n, "description": d,
                  "unit": u, "quantity": q, "amount": a, "quote": qu}
                 for n, d, u, q, a, qu in runmod.CHECK_SCHEDULE]
        t1 = time.time()
        res = None
        for i in range(20):
            payload = {"market": "municipal" if i % 2 == 0 else "txdot",
                       "n_bidders": 4, "owner": "", "work_type": "",
                       "bid_date": "2026-03-02", "items": items}
            req = urllib.request.Request(
                base + "/api/estimate", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            for it in items:
                req = urllib.request.Request(
                    base + "/api/references", data=json.dumps(
                        {"request": payload, "row_id": it["row_id"]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(req, timeout=60).read()
            r2 = rss_mb(p.pid)
            peak = max(peak, r2 or 0)
        est_s = (time.time() - t1) / 20
        keys = [c.get("key") for c in (res.get("similar") or {}).get("cards")
                or [] if c.get("key")]
        if keys:
            from urllib.parse import quote
            urllib.request.urlopen(base + "/api/letting?key=" + quote(keys[0]),
                                   timeout=60).read()
        busy = rss_mb(p.pid)
        peak = max(peak, busy or 0)
        rep = {"root": root, "public": public, "marks_s_mb": marks,
               "idle_after_load_mb": round(idle or 0),
               "after_use_mb": round(busy or 0), "peak_sampled_mb": round(peak),
               "estimate_plus_refs_s": round(est_s, 3),
               "python": sys.version.split()[0],
               "samples": samples}
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "samples"},
                     indent=1))


if __name__ == "__main__":
    main()
