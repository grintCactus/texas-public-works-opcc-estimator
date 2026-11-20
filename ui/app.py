"""FastAPI backend for the bid estimating tool.

Local-only by design: binds 127.0.0.1, no external calls, no CDN, no account.
Everything it reads comes from data/clean and src/ in this repo.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import traceback
from typing import Any, Dict, List, Optional

# ui/ must be importable as a flat package root so `import pricing` works the
# same whether the app is started by run.py or by `uvicorn app:app`.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pandas as pd
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import data_access as da
import matching
import pricing
import upstream
from ingest import bidtab, ocr as ocr_mod, takeoff

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("ui")

STATIC_DIR = os.path.join(_HERE, "static")
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

app = FastAPI(title="Bid Estimator", version="0.1.0", docs_url="/api/docs")
engine = pricing.PriceEngine()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

_warm_state: Dict[str, Any] = {"done": False, "step": "", "i": 0, "n": 0,
                               "error": "", "started": 0.0, "elapsed": 0.0}


@app.on_event("startup")
def _startup() -> None:
    _warm_state["started"] = time.time()

    def progress(i, n, name):
        _warm_state.update({"i": i, "n": n, "step": name})
        log.info("loading %s (%d/%d)", name, i + 1, n)

    try:
        da.warm(progress)
        matching.index()          # build the match index up front too
    except Exception as e:  # noqa: BLE001
        _warm_state["error"] = "%s: %s" % (type(e).__name__, e)
        log.exception("warm-up failed")
    _warm_state["done"] = True
    _warm_state["elapsed"] = round(time.time() - _warm_state["started"], 1)
    log.info("ready in %.1fs", _warm_state["elapsed"])


@app.exception_handler(Exception)
def _unhandled(request, exc):  # noqa: ANN001
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"error": "%s: %s" % (type(exc).__name__, exc),
                 "where": request.url.path,
                 "traceback": traceback.format_exc().splitlines()[-6:]})


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status() -> dict:
    report = da.load_report()
    tables = []
    for name, spec in da.TABLES.items():
        r = report.get(name, {})
        tables.append({
            "name": name, "label": spec.label, "file": spec.filename,
            "rows": r.get("rows"), "error": r.get("error"),
            "unexpected_columns": r.get("unexpected_columns", []),
        })
    panel = report.get("panel", {})
    return {
        "ready": _warm_state["done"],
        "clean_overlay": da.OVERLAY_DIR,
        "warm": dict(_warm_state),
        "tables": tables,
        "panel": {"rows": panel.get("rows"), "error": panel.get("error"),
                  "file": da.MASTER_PANEL_FILE,
                  "quality_filtered": panel.get("quality_filtered"),
                  "has_quality_flags": panel.get("has_quality_flags")},
        "upstream": upstream.status(),
        "ocr": ocr_mod.status(),
        "bidtab_import": bidtab.available(),
        "clean_dir": da.CLEAN_DIR,
        "calibration": {
            "default_qty_elasticity": pricing.DEFAULT_QTY_ELASTICITY,
            "ref_spread_shrink": pricing.REF_SPREAD_SHRINK,
            "line_error_correlation": pricing.LINE_ERROR_CORRELATION,
            "benchmark_project_sigma": pricing.BENCHMARK_PROJECT_SIGMA,
            "qty_factor_clamp": pricing.QTY_FACTOR_CLAMP,
        },
    }


@app.get("/api/options")
def options() -> dict:
    try:
        d = engine.discount_table
        discount = [
            {"n_bidders": int(r["n_bidders"]),
             "factor": _f(r.get("low_over_median")),
             "lettings": int(_f(r.get("lettings")) or 0),
             "sigma_log": _f(r.get("sigma_log"))}
            for _, r in d.iterrows()
        ]
    except Exception as e:  # noqa: BLE001
        discount = []
        log.error("discount table unavailable: %s", e)
    index_info: dict = {"available": False}
    try:
        b = engine.basket_index
        if len(b):
            ys = pd.to_numeric(b.get("to_year"), errors="coerce").dropna()
            index_info = {
                "available": True,
                "baskets": sorted({str(x) for x in b["basket"].dropna()}),
                "year_min": int(ys.min()) if len(ys) else None,
                "year_max": int(ys.max()) if len(ys) else None,
                "scope": sorted({str(x) for x in b.get(
                    "scope", pd.Series(dtype=str)).dropna()}),
            }
    except Exception as e:  # noqa: BLE001
        log.error("basket index unavailable: %s", e)
    return {
        "owners": da.owners()[:120],
        "work_types": da.work_types()[:60],
        "item_families": da.item_families(),
        "discount_curve": discount,
        "units": sorted(takeoff.KNOWN_UNITS),
        "price_index": index_info,
    }


def _f(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


# ---------------------------------------------------------------------------
# Scenario A: takeoff in
# ---------------------------------------------------------------------------

@app.post("/api/takeoff/text")
def takeoff_text(payload: dict = Body(...)) -> dict:
    text = payload.get("text") or ""
    if not text.strip():
        raise HTTPException(400, "No text supplied.")
    return takeoff.from_text(text).to_dict()


@app.post("/api/takeoff/file")
async def takeoff_file(file: UploadFile = File(...)) -> dict:
    path, name = await _save_upload(file)
    try:
        return takeoff.from_file(path, name).to_dict()
    finally:
        _cleanup(path)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@app.post("/api/match")
def match_lines(payload: dict = Body(...)) -> dict:
    """Automatic match for a batch of lines (used right after parsing)."""
    items = payload.get("items") or []
    out = []
    for it in items:
        desc = (it.get("description") or "").strip()
        unit = (it.get("unit") or "").strip()
        cands = matching.candidates(desc, unit, limit=6)
        out.append({
            "row_id": it.get("row_id"),
            "candidates": [c.to_dict() for c in cands],
            "best": cands[0].to_dict() if cands else None,
        })
    return {"matches": out,
            "weak_threshold": matching.WEAK_MATCH,
            "good_threshold": matching.GOOD_MATCH}


@app.get("/api/match/search")
def match_search(q: str, unit: str = "", limit: int = 25) -> dict:
    """Backs the 'change match' picker."""
    if not q.strip():
        return {"candidates": []}
    return {"candidates": [c.to_dict()
                           for c in matching.search(q, unit, limit=limit)]}


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------

@app.post("/api/estimate")
def estimate(payload: dict = Body(...)) -> dict:
    raw_items = payload.get("items") or []
    if not raw_items:
        raise HTTPException(400, "No line items supplied.")

    # Hard rule: the OPCC is not an input. If a client ever sends one, it is
    # rejected loudly rather than quietly ignored, because silently accepting
    # the field is how it ends up influencing the model later.
    for forbidden in ("opcc", "opcc_total", "engineer_estimate",
                      "our_estimate", "target_price", "budget"):
        if forbidden in payload:
            raise HTTPException(
                400,
                "'%s' is not an accepted input. The OPCC is neither a target "
                "nor a feature of this estimate -- it is only a backtest "
                "baseline. Feeding it in would recycle the very bias the tool "
                "exists to remove." % forbidden)

    try:
        n_bidders = int(payload.get("n_bidders") or 0)
    except (TypeError, ValueError):
        n_bidders = 0

    items: List[pricing.LineItemInput] = []
    for i, it in enumerate(raw_items):
        items.append(pricing.LineItemInput(
            row_id=str(it.get("row_id") or "r%d" % (i + 1)),
            description=str(it.get("description") or ""),
            unit=str(it.get("unit") or ""),
            quantity=_f(it.get("quantity")),
            match_key=str(it.get("match_key") or ""),
            include=bool(it.get("include", True)),
        ))

    req = pricing.EstimateRequest(
        items=items,
        n_bidders=n_bidders,
        work_type=str(payload.get("work_type") or ""),
        owner=str(payload.get("owner") or ""),
        bid_year=_int(payload.get("bid_year")),
    )
    t0 = time.time()
    result = engine.estimate(req)
    d = result.to_dict()
    d["elapsed_s"] = round(time.time() - t0, 3)
    return d


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@app.get("/api/comparables")
def comparables(key: str, unit: str = "", owner: str = "",
                limit: int = 25, competitive_only: bool = True,
                quantity: Optional[float] = None) -> dict:
    return engine.comparables(key, unit=unit, owner=owner, limit=limit,
                              competitive_only=competitive_only,
                              quantity=quantity)


@app.get("/api/discount_curve")
def discount_curve() -> dict:
    d = engine.discount_table
    return {"rows": [
        {"n_bidders": int(r["n_bidders"]),
         "low_over_median": _f(r.get("low_over_median")),
         "p25": _f(r.get("p25")), "p75": _f(r.get("p75")),
         "lettings": int(_f(r.get("lettings")) or 0),
         "sigma_log": _f(r.get("sigma_log"))}
        for _, r in d.iterrows()]}


# ---------------------------------------------------------------------------
# Scenario B: historical bid tabulation in
# ---------------------------------------------------------------------------

@app.post("/api/bidtab/parse")
async def bidtab_parse(file: UploadFile = File(...)) -> dict:
    path, name = await _save_upload(file)
    try:
        return bidtab.parse(path, name)
    finally:
        _cleanup(path)


@app.get("/api/bidtab/inbox")
def bidtab_inbox() -> dict:
    return {"files": bidtab.inbox(), "dir": bidtab.INBOX}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@app.post("/api/export/csv")
def export_csv(payload: dict = Body(...)) -> FileResponse:
    """Flat CSV of the estimate, for pasting into the estimator's workbook."""
    import csv as _csv

    result = payload.get("result") or {}
    lines = result.get("lines") or []
    meta = payload.get("meta") or {}
    # Handed the wrong shape -- an estimate REQUEST rather than a result, say --
    # this used to return a structurally perfect CSV containing a header row,
    # a blank subtotal and a blank total. That file opens cleanly in Excel and
    # looks like an estimate of nothing, which is worse than no file at all:
    # it is the kind of thing that gets pasted into a workbook unexamined.
    if not lines:
        raise HTTPException(
            400,
            "Nothing to export: the payload carried no priced line items. "
            "This endpoint takes the ESTIMATE RESULT, as "
            "{\"result\": <the /api/estimate response>, \"meta\": {...}} -- "
            "not an estimate request.")
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="estimate_")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.writer(fh)
        w.writerow(["# Bid estimate -- expected LOW bid, P10/P50/P90"])
        w.writerow(["# Expected bidders", meta.get("n_bidders", "")])
        w.writerow(["# Work type", meta.get("work_type", "")])
        w.writerow(["# Owner", meta.get("owner", "")])
        w.writerow(["# Generated", time.strftime("%Y-%m-%d %H:%M")])
        w.writerow(["# NOT a point estimate. Industry benchmark for a "
                    "continuously-bidding public owner is 13.8% median "
                    "absolute error against the low bid."])
        w.writerow([])
        w.writerow(["item_no", "description", "unit", "quantity", "treatment",
                    "unit_p10", "unit_p50", "unit_p90",
                    "total_p10", "total_p50", "total_p90",
                    "pct_of_cost_p50", "matched_item", "match_score",
                    "evidence_projects", "evidence_usable", "warnings"])
        for l in lines:
            up, tt = l.get("unit_price") or {}, l.get("total") or {}
            pc = l.get("pct_of_construction") or {}
            ev = l.get("evidence") or {}
            w.writerow([
                l.get("item_no", ""), l.get("description", ""),
                l.get("unit", ""), l.get("quantity", ""),
                l.get("treatment", ""),
                up.get("p10", ""), up.get("p50", ""), up.get("p90", ""),
                tt.get("p10", ""), tt.get("p50", ""), tt.get("p90", ""),
                pc.get("p50", ""),
                l.get("matched_label", ""), l.get("match_score", ""),
                ev.get("n_projects", ""), ev.get("usable", ""),
                " / ".join(wn.get("message", "")
                           for wn in (l.get("warnings") or [])),
            ])
        w.writerow([])
        ds = result.get("direct_subtotal") or {}
        pt = result.get("project_total") or {}
        w.writerow(["", "DIRECT WORK SUBTOTAL", "", "", "", "", "", "",
                    ds.get("p10", ""), ds.get("p50", ""), ds.get("p90", "")])
        w.writerow(["", "PROJECT TOTAL (incl. % -of-cost items)", "", "", "",
                    "", "", "",
                    pt.get("p10", ""), pt.get("p50", ""), pt.get("p90", "")])
    return FileResponse(path, filename="bid_estimate_%s.csv"
                        % time.strftime("%Y%m%d-%H%M"),
                        media_type="text/csv")


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

async def _save_upload(file: UploadFile):
    name = os.path.basename(file.filename or "upload")
    suffix = os.path.splitext(name)[1][:12]
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="takeoff_")
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413, "File is larger than %d MB."
                        % (MAX_UPLOAD_BYTES // (1024 * 1024)))
                fh.write(chunk)
    except HTTPException:
        _cleanup(path)
        raise
    if size == 0:
        _cleanup(path)
        raise HTTPException(400, "Uploaded file is empty.")
    return path, name


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Static front end (mounted last so /api/* wins)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _no_cache_static(request, call_next):  # noqa: ANN001
    """Serve the front end uncached.

    This is a local tool whose source sits next to the data it reads. Editing
    app.js and refreshing has to show the edit -- a cached bundle means the
    person is debugging code that is not the code running, which wasted real
    time during development. Nothing here is on a network where caching would
    buy anything.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
