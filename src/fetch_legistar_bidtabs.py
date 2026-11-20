"""
Harvest bid tabulations from DFW cities that publish council agendas through
Legistar (webapi.legistar.com).

How it works
    1. Pull every council "matter" introduced on/after --since for each client.
    2. Keep the matters whose title looks like a construction bid award
       (paving, street, alley, drainage, creek/erosion, water/sewer ...).
    3. For each kept matter, list its attachments and download the ones whose
       name looks like a bid tabulation / bid recap / bid summary.

Only matters from --since onward are fetched (default 2020-01-01): earlier,
pre-pandemic pricing is not comparable.

Usage:
    python src/fetch_legistar_bidtabs.py
    python src/fetch_legistar_bidtabs.py --clients mckinney,plano --since 2022-01-01
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://webapi.legistar.com/v1"

# Legistar clients confirmed reachable for DFW-area cities.
CLIENTS = [
    "cityofdallas", "mckinney", "plano", "mesquite", "coppell",
    "farmersbranch", "mansfield", "grandprairie",
]

# Matter titles worth opening: construction work in the families we price.
TITLE_KEEP = re.compile(
    r"pav(e|ing|ement)|street|roadway|road\s|alley|sidewalk|curb|gutter|"
    r"reconstruct|rehabilit|resurfac|overlay|reclamation|concrete|"
    r"drainage|storm\s*(water|sewer|drain)|culvert|channel|creek|erosion|"
    r"gabion|outfall|detention|dredg|slope|retaining\s*wall|riprap|"
    r"water\s*(main|line)|sanitary\s*sewer|wastewater|utility|utilities|"
    r"intersection|traffic\s*signal|bridge|trail|park(ing)?\s*lot",
    re.I,
)
# ... but skip obvious non-construction procurement.
TITLE_SKIP = re.compile(
    r"\b(insurance|software|license|subscription|audit|legal\s*services|"
    r"ambulance|uniform|janitorial|fuel\s*card|banking|investment|"
    r"health\s*plan|staffing|advertis|appraisal\s*district|election)\b",
    re.I,
)

# Attachment names that are actually bid tabulations.
ATTACH_KEEP = re.compile(
    r"bid\s*(tab|tabulation|recap|summary|sheet|results?|form)|tabulation|"
    r"bid\s*opening|schedule\s*of\s*(bid\s*)?items|unit\s*price",
    re.I,
)


def get_json(url: str, retries: int = 3, timeout: int = 90, quiet: bool = False):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json",
                              "User-Agent": "bid-tab-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    if not quiet:
        print(f"    ! failed: {last}")
    return None


def get_bytes(url: str, retries: int = 3, timeout: int = 180) -> bytes | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "bid-tab-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (attempt + 1))
    return None


def fetch_matters(client: str, since: str, page: int = 1000) -> list[dict]:
    """Page through matters introduced on/after `since`."""
    out: list[dict] = []
    skip = 0
    while True:
        q = {
            "$select": "MatterId,MatterFile,MatterTitle,MatterIntroDate,MatterTypeName",
            "$filter": f"MatterIntroDate ge datetime'{since}'",
            "$orderby": "MatterIntroDate asc",
            "$top": page,
            "$skip": skip,
        }
        url = f"{API}/{client}/matters?" + urllib.parse.urlencode(q)
        rows = get_json(url)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page:
            break
        skip += page
        if skip > 60000:
            break
    return out


def safe(s: str, n: int = 90) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")
    return s[:n] or "file"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", default=",".join(CLIENTS))
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--outdir", default="data/external/legistar")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for client in [c.strip() for c in args.clients.split(",") if c.strip()]:
        print(f"\n=== {client} ===", flush=True)
        cdir = outdir / client
        cdir.mkdir(exist_ok=True)
        # one manifest per client, so parallel runs never clash
        manifest_path = outdir / f"_manifest_{client}.csv"

        seen_urls: set[str] = set()
        if manifest_path.exists():
            for r in csv.DictReader(manifest_path.open(encoding="utf-8-sig")):
                seen_urls.add(r["url"])

        matters = fetch_matters(client, args.since)
        print(f"  matters since {args.since}: {len(matters)}")
        if not matters:
            continue

        kept = [m for m in matters
                if TITLE_KEEP.search(m.get("MatterTitle") or "")
                and not TITLE_SKIP.search(m.get("MatterTitle") or "")]
        print(f"  construction-looking: {len(kept)}", flush=True)

        lock = threading.Lock()
        rows: list[dict] = []
        counts = {"scanned": 0, "dl": 0}

        def handle(m: dict) -> None:
            mid = m["MatterId"]
            atts = get_json(f"{API}/{client}/matters/{mid}/attachments", quiet=True)
            with lock:
                counts["scanned"] += 1
                if counts["scanned"] % 100 == 0:
                    print(f"    scanned {counts['scanned']}/{len(kept)}, "
                          f"{counts['dl']} files", flush=True)
            if not atts:
                return
            for a in atts:
                name = a.get("MatterAttachmentName") or ""
                url = a.get("MatterAttachmentHyperlink") or ""
                if not url or not ATTACH_KEEP.search(name):
                    continue
                with lock:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                blob = get_bytes(url)
                if not blob or len(blob) < 3000:
                    continue
                intro = (m.get("MatterIntroDate") or "")[:10]
                fn = f"{intro}_{safe(m.get('MatterFile',''),18)}_{safe(name,40)}.pdf"
                (cdir / fn).write_bytes(blob)
                with lock:
                    counts["dl"] += 1
                    rows.append({
                        "client": client,
                        "matter_file": m.get("MatterFile", ""),
                        "intro_date": intro,
                        "matter_title": (m.get("MatterTitle") or "").replace("\n", " ")[:400],
                        "attachment_name": name,
                        "file": f"{client}/{fn}",
                        "size_kb": round(len(blob) / 1024),
                        "url": url,
                    })

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(as_completed([ex.submit(handle, m) for m in kept]))

        if rows:
            write_header = not manifest_path.exists()
            with manifest_path.open("a", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                if write_header:
                    w.writeheader()
                w.writerows(rows)
        print(f"  downloaded {counts['dl']} new -> {cdir}")

    print(f"\ndone -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
