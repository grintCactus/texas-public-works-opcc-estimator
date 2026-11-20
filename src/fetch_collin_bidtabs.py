"""
Harvest bid tabulations from Collin County Commissioners Court agendas.

Collin County's eAgenda exposes each agenda as
    /agenda_publish.cfm?...&dsp=ag&ag=<N>
and every attachment as
    /docs/<year>/CC/<yyyymmdd>_<meeting>/<docid>_<exact file name>.pdf

The document folder cannot be listed and the file name must match exactly, so
the agendas are the only way in: walk the `ag` sequence, scrape the attachment
links, and keep the ones whose file name looks like a bid tabulation.

Only meetings from --since onward are kept (default 2020-01-01).

Usage:
    python src/fetch_collin_bidtabs.py
    python src/fetch_collin_bidtabs.py --ag-from 2300 --ag-to 3100
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOST = "https://eagenda.collincountytx.gov"
AGENDA = HOST + "/agenda_publish.cfm?id=&mt=ALL&dsp=ag&ag={ag}"

# The hrefs are URL-encoded, so the extension often appears as "%2Epdf".
DOC_HREF = re.compile(
    r'href="(/docs/(\d{4})/CC/(\d{8})_\d+/[^"]+?(?:\.pdf|%2Epdf))"', re.I)

# File names that are actually bid tabulations / bid results.
NAME_KEEP = re.compile(
    r"bid[\s_%20-]*tab|tabulation|bid[\s_%20-]*recap|bid[\s_%20-]*summary|"
    r"bid[\s_%20-]*results?|low[\s_%20-]*bid|abstract[\s_%20-]*of[\s_%20-]*bids",
    re.I,
)
# Construction-relevant names (used only to prioritise, not to exclude).
WORKISH = re.compile(
    r"road|street|pav|asphalt|concrete|drainage|storm|culvert|creek|channel|"
    r"erosion|bridge|sidewalk|reconstruct|overlay|base|utilit", re.I)


def get(url: str, retries: int = 3, timeout: int = 90) -> bytes | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "bid-tab-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (attempt + 1))
    return None


def safe(s: str, n: int = 95) -> str:
    s = urllib.parse.unquote(s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s[:n] or "file"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ag-from", type=int, default=2200)
    ap.add_argument("--ag-to", type=int, default=3200)
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--outdir", default="data/external/collin_county")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    since = args.since.replace("-", "")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = outdir / "_manifest.csv"

    seen: set[str] = set()
    if manifest.exists():
        for r in csv.DictReader(manifest.open(encoding="utf-8-sig")):
            seen.add(r["url_path"])

    lock = threading.Lock()
    found: dict[str, tuple[str, str]] = {}   # path -> (meeting_date, filename)
    scanned = {"n": 0, "agendas_with_docs": 0}

    def scan(ag: int) -> None:
        body = get(AGENDA.format(ag=ag))
        with lock:
            scanned["n"] += 1
            if scanned["n"] % 100 == 0:
                print(f"  scanned {scanned['n']} agendas, "
                      f"{len(found)} candidate tabs", flush=True)
        if not body:
            return
        text = body.decode("utf-8", "replace")
        hits = 0
        for m in DOC_HREF.finditer(text):
            path, _year, mdate = m.group(1), m.group(2), m.group(3)
            if mdate < since:
                continue
            fname = html.unescape(path.rsplit("/", 1)[-1])
            if not NAME_KEEP.search(urllib.parse.unquote(fname)):
                continue
            hits += 1
            with lock:
                found.setdefault(path, (mdate, fname))
        if hits:
            with lock:
                scanned["agendas_with_docs"] += 1

    print(f"walking agenda ids {args.ag_from}..{args.ag_to}")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(as_completed([ex.submit(scan, ag)
                           for ag in range(args.ag_from, args.ag_to + 1)]))

    todo = {p: v for p, v in found.items() if p not in seen}
    print(f"\ncandidate bid tabs since {args.since}: {len(found)} "
          f"({len(todo)} new)")

    rows: list[dict] = []
    n_dl = 0

    def download(item) -> None:
        nonlocal n_dl
        path, (mdate, fname) = item
        blob = get(HOST + path)
        if not blob or len(blob) < 3000 or not blob[:5].startswith(b"%PDF"):
            return
        iso = f"{mdate[:4]}-{mdate[4:6]}-{mdate[6:]}"
        stem = re.sub(r"(?:\.pdf|%2Epdf)$", "", urllib.parse.unquote(fname), flags=re.I)
        dest = outdir / f"{iso}_{safe(stem)}.pdf"
        dest.write_bytes(blob)
        with lock:
            n_dl += 1
            rows.append({
                "meeting_date": iso,
                "file": dest.name,
                "size_kb": round(len(blob) / 1024),
                "looks_construction": bool(WORKISH.search(urllib.parse.unquote(fname))),
                "url_path": path,
                "url": HOST + path,
            })

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(as_completed([ex.submit(download, it) for it in todo.items()]))

    if rows:
        write_header = not manifest.exists()
        with manifest.open("a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if write_header:
                w.writeheader()
            w.writerows(rows)

    n_work = sum(1 for r in rows if r["looks_construction"])
    print(f"downloaded {n_dl} documents ({n_work} with construction-looking names)"
          f" -> {outdir}")
    print(f"manifest -> {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
