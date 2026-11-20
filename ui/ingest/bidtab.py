"""Scenario B: preview a historical bid tabulation with the existing parser.

This calls src/parse_bidtabs.py -- it does not reimplement it. That parser
already handles per-owner column layouts, the engineer's-estimate column, the
two competing PDF strategies and the arithmetic self-consistency checks, and a
second implementation would drift from the one that produced the 1,100-file
data set.

Scope note: this previews and summarises the parse. It does NOT write to
data/clean. That directory is owned by the modelling session and the ETL
rewrites it; a UI that appended rows there would race the ETL and corrupt the
panel. The reviewed file is staged under ui/.inbox/ and the summary tells the
modelling session what to ingest.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import Counter
from typing import Any, Dict, List

import upstream

UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(UI_DIR, ".inbox")


def available() -> bool:
    return bool(upstream.PARSER_AVAILABLE)


def _new_stats() -> Dict[str, Any]:
    """The stats dict shape src/parse_bidtabs.py expects to be handed.

    Mirrors the literal its own main() uses:
        stats = {"cells_blank_price": 0, "sheets_no_header": []}
    Most keys are created with setdefault/.get, but these two are indexed
    directly (`stats["sheets_no_header"].append(...)`), so passing an empty
    dict raises KeyError partway through a PDF parse.
    """
    return {"cells_blank_price": 0, "sheets_no_header": []}


def parse(path: str, original_name: str = "") -> Dict[str, Any]:
    """Run the upstream parser and summarise what it found."""
    name = original_name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if not available():
        return {"ok": False,
                "error": "src/parse_bidtabs.py is unavailable (%s)."
                         % upstream.IMPORT_ERROR}

    stats = _new_stats()
    t0 = time.time()
    try:
        if ext in (".xlsx", ".xlsm"):
            recs, audit, _strings = upstream.read_xlsx(path, stats)
        elif ext == ".pdf":
            # Returns (recs, audit, strings), same as read_xlsx.
            recs, audit, _strings = upstream.read_pdf_best(path, stats)
        else:
            return {"ok": False,
                    "error": "Bid tabulations are read from .xlsx, .xlsm or "
                             ".pdf. Got %r." % ext}
    except KeyError as e:
        # The parser indexes some stats keys directly instead of using
        # setdefault, so the dict must arrive pre-seeded. If it grows a new
        # one upstream, say exactly what to add rather than showing a bare
        # KeyError.
        return {"ok": False,
                "error": "src/parse_bidtabs.py expects stats[%s] to exist. Add "
                         "it to _new_stats() in ui/ingest/bidtab.py (list for "
                         "an appended key, 0 for a counter)." % e,
                "stats": _jsonable(stats)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "error": "%s: %s" % (type(e).__name__, e),
                "stats": _jsonable(stats)}

    elapsed = time.time() - t0
    recs = list(recs or [])
    bidders = Counter(_g(r, "bidder") for r in recs if _g(r, "bidder"))
    units = Counter(_g(r, "unit") for r in recs if _g(r, "unit"))
    ee = sum(1 for r in recs if _truthy(_g(r, "is_engineer_estimate")))

    staged = _stage(path, name) if recs else ""

    return {
        "ok": True,
        "file": name,
        "n_rows": len(recs),
        "n_bidders": len(bidders),
        "bidders": [{"name": b, "rows": n} for b, n in bidders.most_common(40)],
        "units": dict(units.most_common(15)),
        "engineer_estimate_rows": ee,
        "elapsed_s": round(elapsed, 2),
        "audit": _jsonable(audit)[:20],
        "stats": _jsonable(stats),
        "sample": [_row_preview(r) for r in recs[:60]],
        "staged_path": staged,
        "note": (
            "Parsed with src/parse_bidtabs.py. Nothing was written to "
            "data/clean -- the ETL owns that directory and rewrites it. The "
            "file is staged in ui/.inbox/ for the next ETL run to pick up."
            if staged else
            "Parsed with src/parse_bidtabs.py. No rows extracted, so nothing "
            "was staged."),
    }


def _stage(path: str, name: str) -> str:
    os.makedirs(INBOX, exist_ok=True)
    safe = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name)
    dest = os.path.join(INBOX, "%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), safe))
    try:
        shutil.copy2(path, dest)
    except OSError as e:
        return "staging failed: %s" % e
    return dest


def inbox() -> List[dict]:
    if not os.path.isdir(INBOX):
        return []
    out = []
    for f in sorted(os.listdir(INBOX), reverse=True):
        p = os.path.join(INBOX, f)
        if os.path.isfile(p):
            st = os.stat(p)
            out.append({"name": f, "bytes": st.st_size,
                        "staged_at": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def _g(rec, key, default=None):
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _row_preview(r) -> dict:
    keys = ("item_no", "description", "unit", "quantity", "bidder",
            "unit_price", "extended", "is_engineer_estimate", "src_sheet")
    return {k: _jsonable(_g(r, k)) for k in keys}


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
