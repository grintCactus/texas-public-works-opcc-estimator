"""
For every scanned (`unreadable`) Legistar bid tab, look for a text-layer
sibling under the same council matter.

OCR is lossy; if the agency also attached an Excel sheet or a second, text-based
PDF to the same agenda item, re-fetching that is strictly better. This walks the
Legistar manifests, re-lists each affected matter's attachments, and reports
every sibling whose name suggests a bid tabulation -- flagging the ones that are
spreadsheets or that download with a real text layer.

Usage:
    python src/find_text_alternatives.py                 # report only
    python src/find_text_alternatives.py --download      # fetch the usable ones
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://webapi.legistar.com/v1"
ROOT = Path("data/external")
LEG = ROOT / "legistar"
OUT = ROOT / "legistar" / "_text_alternatives.csv"

TAB_ISH = re.compile(
    r"bid\s*(tab|tabulation|recap|summary|sheet|results?|form)|tabulation|"
    r"schedule\s*of\s*(bid\s*)?items|unit\s*price|itemized",
    re.I,
)
SPREADSHEET = re.compile(r"\.(xlsx?|xlsm|csv)(\?|$)", re.I)


def get_json(url: str, retries: int = 3, timeout: int = 90):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json",
                              "User-Agent": "bid-tab-research/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (attempt + 1))
    return None


def get_bytes(url: str, timeout: int = 180) -> bytes | None:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "bid-tab-research/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def pdf_has_text(blob: bytes) -> bool:
    """True if pdftotext finds a real text layer (i.e. not a pure scan)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(blob)
        tmp = fh.name
    try:
        out = subprocess.run(["pdftotext", "-layout", tmp, "-"],
                             capture_output=True, timeout=120)
        return len(out.stdout.decode("utf-8", "replace").strip()) > 200
    except Exception:  # noqa: BLE001
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()

    audit = ROOT / "_audit.csv"
    if not audit.exists():
        print("run src/audit_external.py first")
        return 1

    scanned = {r["file"] for r in csv.DictReader(audit.open(encoding="utf-8-sig"))
               if r["classification"] == "unreadable"
               and r["file"].startswith("legistar/")}
    print(f"scanned (unreadable) legistar files: {len(scanned)}")
    if not scanned:
        return 0

    # map file -> (client, matter_file, url) from the per-client manifests
    meta: dict[str, dict] = {}
    for man in sorted(LEG.glob("_manifest_*.csv")):
        for r in csv.DictReader(man.open(encoding="utf-8-sig")):
            meta[f"legistar/{r['file']}"] = r
    missing = [f for f in scanned if f not in meta]
    if missing:
        print(f"  ! {len(missing)} not found in any manifest (skipped)")

    rows = []
    n_alt = 0
    for i, f in enumerate(sorted(scanned & set(meta)), 1):
        m = meta[f]
        client, mfile = m["client"], m["matter_file"]
        # find the matter again by its file number
        q = {"$filter": f"MatterFile eq '{mfile}'", "$select": "MatterId,MatterFile"}
        found = get_json(f"{API}/{client}/matters?" + urllib.parse.urlencode(q))
        if not found:
            continue
        mid = found[0]["MatterId"]
        atts = get_json(f"{API}/{client}/matters/{mid}/attachments") or []
        sibs = [a for a in atts
                if (a.get("MatterAttachmentHyperlink") or "") != m["url"]
                and TAB_ISH.search(a.get("MatterAttachmentName") or "")]
        for a in sibs:
            name = a.get("MatterAttachmentName") or ""
            url = a.get("MatterAttachmentHyperlink") or ""
            is_sheet = bool(SPREADSHEET.search(
                a.get("MatterAttachmentFileName") or url))
            verdict, saved = "", ""
            if is_sheet:
                verdict = "SPREADSHEET - best case"
            else:
                blob = get_bytes(url)
                if blob and pdf_has_text(blob):
                    verdict = "PDF with text layer"
                    if args.download:
                        dest = (LEG / client /
                                f"ALT_{Path(m['file']).stem}__{re.sub(r'[^A-Za-z0-9._-]+','_',name)[:50]}.pdf")
                        dest.write_bytes(blob)
                        saved = str(dest.relative_to(ROOT)).replace("\\", "/")
                else:
                    verdict = "also scanned - no gain"
            if verdict != "also scanned - no gain":
                n_alt += 1
            rows.append({
                "scanned_file": f, "client": client, "matter_file": mfile,
                "alternative_name": name, "verdict": verdict,
                "saved_as": saved, "url": url,
            })
        print(f"  [{i}/{len(scanned & set(meta))}] {f} -> {len(sibs)} sibling(s)",
              flush=True)

    if rows:
        with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} sibling attachments found, {n_alt} usable")
        print(f"report -> {OUT}")
        if not args.download and n_alt:
            print("re-run with --download to fetch the usable ones")
    else:
        print("\nno sibling bid-tab attachments found -- OCR is the only route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
