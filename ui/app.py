"""HTTP API and front end of the bid estimator -- local only.

Runs on the Python standard library (http.server.ThreadingHTTPServer). This
project's venv has no FastAPI / uvicorn, and a loopback-only, single-user
tool needs nothing they add, so there is no web framework to install. It
binds 127.0.0.1 by default; there is no auth because there is no network
surface -- do not change the host without adding one. No CDN, no external
calls: everything comes from data/clean, src/ and ui/static/.

Routes (JSON in, JSON out unless noted):
  GET  /api/status            engine state, data date, estimator md5, parser
  GET  /api/options           markets, owners, work types, families, units,
                              discount tables, accuracy context
  POST /api/takeoff/text      {text} -> parsed takeoff lines
  POST /api/takeoff/file      multipart field "file" (or a raw body with
                              ?name=) -> parsed takeoff lines
  POST /api/classify          {market, lines: [{row_id, description, unit,
                              family?, quantity?, confidence?, issues?,
                              keep_parser?}]} -> the parser's classification,
                              plus intake help per line: a remembered
                              correction, low-confidence reasons and the top-3
                              history suggestions (ui/intake.py)
  GET  /api/corrections       the corrections memory's file and size
  POST /api/corrections       {description, family, unit?, source?} ->
                              remember a correction (ui/.local/)
  POST /api/corrections/forget {description} -> forget it
  GET  /api/history/search    ?q=&unit=&limit= -> earlier public line items
  POST /api/bidders/suggest   {market, work_type, bid_date, items} -> N1
  POST /api/estimate          the production estimate; an OPCC / engineer's
                              estimate field is refused with 400
  POST /api/references        {request, row_id} -> the reference projects
                              behind one line's price (on demand)
  POST /api/export/csv        {result, request?} -> CSV file
  POST /api/export/xlsx       {result, request?} -> XLSX file (with the
                              request, also each line's top-3 references)
  POST /api/bidtab/parse      multipart "file" -> parse preview (staged in
                              ui/.inbox/ for the ETL)
  GET  /api/bidtab/inbox
  GET  /api/letting?key=      the full public bid tabulation of one letting
                              (every line, every bidder, totals; built from
                              master_panel source_set == external rows,
                              on demand, cached)
  GET  /api/letting/csv?key=  the same tab as a CSV file
  GET  /api/source-file?path= the local copy of a public letting's original
                              document -- only a file inside data/external/
                              that is the source of a public letting
  POST /api/worksheet/refs    {request} -> per line: the reference range
                              (low / median / high, escalated), the most
                              recent reference, the projects to pick from
  POST /api/worksheet/export/csv|xlsx  {result, request, worksheet} -> the
                              OPCC worksheet (the estimator's deliverable)
  GET  /api/worksheet/list    saved worksheets (ui/.local/worksheets/)
  GET  /api/worksheet/load?file=
  POST /api/worksheet/save    {name, worksheet} -> ui/.local/worksheets/
                              The worksheet's prices are OUTPUT ONLY: never
                              read by the estimator or written anywhere else.
  GET  /api/claude/status     the optional Claude reader: active backend
                              (api -> cli -> none), model, privacy text
  POST /api/claude/preview    multipart "file" (a PDF) -> pages, requests,
                              local cost estimate. Local only: nothing sent.
  POST /api/claude/count?consent=yes   multipart "file" -> exact input
                              tokens from the token-counting endpoint (API
                              backend; sends the document)
  POST /api/claude/read?consent=yes    multipart "file" -> lines read by
                              Claude, as a takeoff parse (sends the document)
  GET  /api/health            {ok, ready} -- for container health checks
Everything else is served from ui/static/ (uncached).

Public-demo mode (BIDEST_PUBLIC=1, ui/public_mode.py) turns off the Claude
reader, the bid-tab inbox, every route that writes on the server (the
corrections memory and saved worksheets move to the browser) and local
original files, and adds the access code, rate limits, the upload cap and
security headers. Off by default: nothing above changes without it.

`dispatch()` is the whole API as a plain function, so the test suite calls
it directly and also through a real socket.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import logging
import math
import mimetypes
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import engine_production as ep  # noqa: E402
import exporter  # noqa: E402
import intake  # noqa: E402
import public_mode as pm  # noqa: E402
import upstream  # noqa: E402
from ingest import bidtab, claude_reader, ocr as ocr_mod, takeoff  # noqa: E402

log = logging.getLogger("ui")

STATIC_DIR = os.path.join(_HERE, "static")
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
VERSION = "2.0.0"

ENGINE = ep.ProductionEngine()
#: The estimator's own classification corrections (ui/.local/, local only).
CORRECTIONS = intake.Corrections()

_matching_state: Dict[str, Any] = {"ready": False, "error": ""}

#: The OPCC worksheet's fields. The estimator's chosen prices, notes and
#: OPCC are OUTPUT ONLY: /api/estimate refuses a request that carries them.
WORKSHEET_FIELDS = ("worksheet", "your_opcc", "your_unit", "your_amount",
                    "contingency_pct", "src_label")


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def start_background_load() -> threading.Thread:
    """Load the estimator tables (about 7-9 s, once) and then the history
    search index, off the request path."""
    def run():
        ENGINE.load()
        # the per-line references (one more read of master_panel.csv);
        # estimates are served meanwhile
        ENGINE.load_references()
        # where each public letting sits in master_panel.csv, so a full bid
        # tabulation opens without reading the whole file
        try:
            ENGINE.tabs.ensure()
        except Exception as e:  # noqa: BLE001 - built again on first use
            log.warning("bid tabulation index failed: %s", e)
        try:
            import matching
            matching.index()
            _matching_state["ready"] = True
        except Exception as e:  # noqa: BLE001
            _matching_state["error"] = "%s: %s" % (type(e).__name__, e)
            log.exception("history search index failed")
    th = threading.Thread(target=run, name="engine-load", daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _clean(o):
    """JSON-safe: NaN / inf -> None, numpy scalars -> Python, tuples ->
    lists. A NaN in a response body breaks JSON.parse in the browser."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    if hasattr(o, "item"):
        try:
            return _clean(o.item())
        except Exception:  # noqa: BLE001
            pass
    return str(o)


def _json_body(body: bytes) -> dict:
    if not body:
        return {}
    if len(body) > MAX_JSON_BYTES:
        raise ApiError(413, "Request body too large.")
    try:
        d = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ApiError(400, "Body is not valid JSON: %s" % e)
    if not isinstance(d, dict):
        raise ApiError(400, "Body must be a JSON object.")
    return d


def _max_upload() -> int:
    """The upload cap: 64 MB locally, BIDEST_MAX_UPLOAD_MB (25) in public
    mode."""
    return pm.CFG.max_upload_bytes if pm.enabled() else MAX_UPLOAD_BYTES


def _uploaded_file(headers, query, body) -> Tuple[str, bytes]:
    """(file name, bytes) from a multipart/form-data body (field "file"), or
    from a raw body with ?name=."""
    if len(body) > _max_upload():
        raise ApiError(413, "File is larger than %d MB."
                       % (_max_upload() // (1024 * 1024)))
    ctype = headers.get("Content-Type") or ""
    if ctype.lower().startswith("multipart/form-data"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1") +
            b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        for part in msg.iter_parts():
            if part.get_param("name", header="content-disposition") == "file":
                data = part.get_payload(decode=True) or b""
                name = part.get_filename() or "upload"
                break
        else:
            raise ApiError(400, "No form field named 'file' in the upload.")
    else:
        name = (query.get("name") or ["upload"])[0]
        data = body
    if not data:
        raise ApiError(400, "Uploaded file is empty.")
    return os.path.basename(name) or "upload", data


def _with_temp(name: str, data: bytes, fn):
    """Run fn(path) on a temporary copy of an upload, deleted afterwards.
    In public mode tempfile's directory is RAM-backed when the machine has
    one (ui/public_mode.py), so the copy never reaches a disk there."""
    suffix = os.path.splitext(name)[1][:12]
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="takeoff_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return fn(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

ROUTES: Dict[Tuple[str, str], Any] = {}


def route(method: str, path: str):
    def deco(fn):
        ROUTES[(method, path)] = fn
        return fn
    return deco


@route("GET", "/api/status")
def r_status(req):
    st = ENGINE.status()
    out = {
        "ready": bool(st["state"]["ready"]),
        "version": VERSION,
        "engine": st["state"],
        "estimator": st["info"].get("estimator"),
        "references": st["references"],
        "data": st["info"].get("data"),
        "bidder_history": st["info"].get("bidder_history"),
        "item_accuracy": st["item_accuracy"],
        "upstream": upstream.status(),
        "ocr": ocr_mod.status(),
        "bidtab_import": pm.BIDTAB_OFF if pm.enabled() else bidtab.available(),
        "history_search": dict(_matching_state),
        "xlsx_export": exporter.xlsx_available(),
        "corrections": (_browser_corrections_status() if pm.enabled()
                        else CORRECTIONS.status()),
        "claude_reader": _claude_status(),
    }
    if pm.enabled():
        if isinstance(out["data"], dict):
            out["data"] = dict(out["data"], clean_dir="data/clean (public "
                                                      "bundle)")
        out["public"] = pm.status()
    return out


@route("GET", "/api/health")
def r_health(req):
    return {"ok": True, "ready": bool(ENGINE.state.get("ready")),
            "references": bool(ENGINE.refs_state.get("ready"))}


def _browser_corrections_status():
    return {"mode": "browser", "file": "", "entries": None, "error": ""}


def _claude_status():
    if pm.enabled():
        return dict(pm.CLAUDE_OFF)
    try:
        return claude_reader.status()
    except Exception as e:  # noqa: BLE001 - the page must load regardless
        return {"available": False, "backend": "none", "label": "off",
                "reason": "status check failed: %s" % type(e).__name__}


@route("GET", "/api/options")
def r_options(req):
    try:
        return ENGINE.options()
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))


# -- reading a takeoff: lines, or a plain "could not read" --------------------
# Whatever is pasted or uploaded, the answer is 200 with the lines read (each
# possibly flagged) or, when nothing could be read, the same shape with
# `unreadable` = {reason, hint}: never a 500, never a bare error the page
# cannot show (QA 2026-11-20, user: "if you can't read it, say so").

#: Limits on what is read at once, with a plain message past them.
MAX_TEXT_CHARS = 500_000
MAX_TAKEOFF_LINES = 2_000
READ_HINT = ("Paste a table with Description, Unit and Quantity columns -- "
             "one item per line, for example “Rock riprap   SY   1,200” -- "
             "or upload the takeoff as .xlsx, .csv or .pdf.")


def _unreadable(source, reason, hint=READ_HINT, method="unreadable"):
    return {"lines": [], "source": source, "method": method,
            "notes": [reason], "mock": False, "skipped": [], "n_skipped": 0,
            "unread": [], "n_unread": 0,
            "unreadable": {"reason": reason, "hint": hint}}


#: The verdict on input with no readable line item (garbage, prose, symbols
#: or numbers only, headings only): plain words, and READ_HINT.
NO_ITEMS = "Couldn't read any line items from this."
#: Readers whose note says why the FILE could not be opened at all (a legacy
#: .xls, a picture with no OCR engine): that note is the reason instead.
_FILE_LEVEL = ("unsupported", "image/ocr-unavailable", "image/ocr-contract-error")


def _verdict(p: dict) -> dict:
    """A parse with no readable line item gets `unreadable` -- NO_ITEMS, or
    for a file the page cannot open at all the reader's own reason -- and
    keeps the lines it could not read (p["unread"]) for the page to show;
    one with too many lines is refused."""
    n = len(p.get("lines") or [])
    if n > MAX_TAKEOFF_LINES:
        return _unreadable(p.get("source"), "This has %s line items; the page "
                           "reads up to %s at once." % ("{:,}".format(n),
                                                        "{:,}".format(MAX_TAKEOFF_LINES)),
                           "Split the takeoff into parts and estimate each.")
    if not n:
        notes = [x for x in (p.get("notes") or []) if x]
        reason = notes[-1] if (notes and p.get("method") in _FILE_LEVEL) else NO_ITEMS
        p = dict(p, unreadable={"reason": reason, "hint": READ_HINT})
    return p


@route("POST", "/api/takeoff/text")
def r_takeoff_text(req):
    text = req.json().get("text")
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    if not text.strip():
        return _unreadable("pasted text", "Nothing was pasted.")
    if len(text) > MAX_TEXT_CHARS:
        return _unreadable("pasted text", "This is %s characters long; the page "
                           "reads up to %s at once." % ("{:,}".format(len(text)),
                                                        "{:,}".format(MAX_TEXT_CHARS)),
                           "Paste the takeoff in parts, or upload it as a file.")
    rows = sum(1 for ln in text.splitlines() if ln.strip())
    if rows > MAX_TAKEOFF_LINES:
        return _unreadable("pasted text", "This has %s lines; the page reads up to "
                           "%s at once." % ("{:,}".format(rows),
                                            "{:,}".format(MAX_TAKEOFF_LINES)),
                           "Paste the takeoff in parts and estimate each.")
    try:
        return _verdict(takeoff.from_text(text).to_dict())
    except Exception as e:  # noqa: BLE001 - a reader bug must not reach the page
        log.warning("reading pasted text failed: %s: %s", type(e).__name__, e)
        return _unreadable("pasted text", "This text could not be read.")


@route("POST", "/api/takeoff/file")
def r_takeoff_file(req):
    try:
        name, data = _uploaded_file(req.headers, req.query, req.body)
    except ApiError as e:
        if e.status == 413:
            raise
        return _unreadable("upload", e.message,
                           "Choose the takeoff file again (.xlsx, .csv, .pdf or .docx).")

    def parse(path):
        try:
            return _verdict(takeoff.from_file(path, name).to_dict())
        except ModuleNotFoundError as e:
            return _unreadable(name, "%s cannot be read here: %s is not installed."
                               % (name, e.name),
                               "Save it as .xlsx or .csv, or paste the rows.")
        except Exception as e:  # noqa: BLE001 - damaged, encrypted, not a takeoff
            log.warning("reading %s failed: %s: %s", name, type(e).__name__, e)
            return _unreadable(name, "%s could not be read: it may be damaged, "
                               "password-protected or not a takeoff." % name,
                               "Open it and save it again as .xlsx or .csv, or "
                               "paste the rows as text.")
    return _with_temp(name, data, parse)


@route("POST", "/api/classify")
def r_classify(req):
    d = req.json()
    market = str(d.get("market") or "municipal")
    lines = d.get("lines") if isinstance(d.get("lines"), list) else []
    lines = [ln for ln in lines if isinstance(ln, dict)]
    if len(lines) > MAX_TAKEOFF_LINES:
        raise ApiError(400, "%d lines at once is more than the page handles "
                            "(%d). Split the takeoff into parts."
                       % (len(lines), MAX_TAKEOFF_LINES))
    try:
        out = ENGINE.classify(lines, market)
        # public mode: the corrections the browser keeps, for this request
        # only; locally: the memory under ui/.local/
        corr = (intake.BrowserCorrections(d.get("corrections"),
                                          _known_families())
                if pm.enabled() else CORRECTIONS)
        out = intake.annotate(ENGINE, lines, out, market, corr,
                              history_ready=bool(_matching_state["ready"]))
        out = ENGINE.add_unit_evidence(out, lines, d.get("bid_date"))
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    return {"lines": out}


@route("GET", "/api/corrections")
def r_corrections(req):
    if pm.enabled():
        return _browser_corrections_status()
    return CORRECTIONS.status()


def _known_families():
    try:
        return {f["id"] for f in ENGINE.options()["families"]}
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))


@route("POST", "/api/corrections")
def r_corrections_save(req):
    d = req.json()
    fam = str(d.get("family") or "").strip()
    known = _known_families()
    if fam not in known:
        raise ApiError(400, "Unknown item family %r." % fam)
    unit = str(d.get("unit") or "").strip()
    if unit and upstream.norm_unit(unit).upper() not in upstream.KNOWN_UNITS:
        unit = ""
    if pm.enabled():
        # public mode writes nothing: the browser keeps the entry under the
        # parser's normalised wording, which only the server can compute
        try:
            entry = intake.BrowserCorrections.entry(
                d.get("description"), fam, unit,
                str(d.get("source") or "picker"))
        except ValueError as err:
            raise ApiError(400, str(err))
        return {"saved": entry, "key": entry["key"], "stored": "browser",
                "status": _browser_corrections_status()}
    try:
        entry = CORRECTIONS.remember(d.get("description"), fam, unit,
                                     str(d.get("source") or "picker"))
    except ValueError as err:
        raise ApiError(400, str(err))
    return {"saved": entry, "status": CORRECTIONS.status()}


@route("POST", "/api/corrections/forget")
def r_corrections_forget(req):
    d = req.json()
    if pm.enabled():
        return {"forgotten": None, "stored": "browser",
                "key": intake.Corrections.key(d.get("description")),
                "status": _browser_corrections_status()}
    return {"forgotten": CORRECTIONS.forget(d.get("description")),
            "status": CORRECTIONS.status()}


@route("GET", "/api/history/search")
def r_history_search(req):
    """Earlier public line items like the query -- to look at history and to
    pick a different family / unit for a line. Matching works on the item
    vocabulary (external lettings only) and the TxDOT price book."""
    import matching
    q = (req.query.get("q") or [""])[0].strip()
    unit = (req.query.get("unit") or [""])[0].strip()
    try:
        limit = max(1, min(50, int((req.query.get("limit") or ["25"])[0])))
    except ValueError:
        limit = 25
    if not q:
        return {"candidates": []}
    out = []
    for c in matching.search(q, unit, limit=limit):
        d = c.to_dict()
        fam = d.get("item_family") or ""
        if not fam:
            # TxDOT price-book rows carry no family: the parser names it
            fam = upstream.item_family(upstream.norm_desc(d.get("label")))
            d["family_from_parser"] = True
        d["item_family"] = fam
        d["family_label"] = ep.family_label(fam)
        if d.get("kind") == "family":           # plain words, no internal ids
            d["label"], d["detail"] = _family_average_label(fam, d)
        out.append(d)
    return {"candidates": out}


def _family_average_label(fam, d):
    """(label, detail) for an item type's average in the Change search:
    "All gabion mattresses (SY) — average of 19 earlier projects", not
    "gabion_mattress (SY) -- family average" (batch 3, 2026-11-20)."""
    unit = d.get("unit") or ""
    n = int(ep._f(d.get("n_projects")) or 0)
    if fam in ("other", "needs_review", "") or \
            "unclassified" in str(d.get("label") or "").lower():
        return ("Unclassified lines (%s) \u2014 no item type matched" % unit,
                "")
    lab = ep.family_label(fam)
    if len(lab) > 1 and lab[1:2].islower():     # "Gabion ..." -> "gabion ...",
        lab = lab[:1].lower() + lab[1:]          # "RCP ..." stays
    return ("All %s (%s) \u2014 average of %d earlier project%s"
            % (lab, unit, n, "" if n == 1 else "s"), "")


@route("POST", "/api/bidders/suggest")
def r_bidders(req):
    try:
        return ENGINE.suggest_bidders(req.json())
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    except ep.BadRequest as e:
        raise ApiError(400, str(e))


@route("POST", "/api/estimate")
def r_estimate(req):
    payload = req.json()
    # Hard rule: the OPCC is not an input. A client that sends one is
    # refused loudly rather than quietly ignored, because silently accepting
    # the field is how it ends up influencing the model later.
    for k in ep.FORBIDDEN_INPUTS:
        if k in payload:
            raise ApiError(
                400,
                "'%s' is not an accepted input. The OPCC is neither a target "
                "nor a feature of this estimate -- it is only a backtest "
                "baseline. Feeding it in would recycle the very bias the tool "
                "exists to remove." % k)
    # the OPCC worksheet's prices are output only: refused, never read
    bad = [k for k in WORKSHEET_FIELDS if k in payload] + sorted({
        k for it in (payload.get("items") or []) if isinstance(it, dict)
        for k in WORKSHEET_FIELDS if k in it})
    if bad:
        raise ApiError(400, "'%s' belongs to the OPCC worksheet: the "
                            "estimator's own prices are output only and never "
                            "an input to the estimate." % bad[0])
    try:
        return ENGINE.estimate(payload)
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    except ep.BadRequest as e:
        raise ApiError(400, str(e))


@route("POST", "/api/references")
def r_references(req):
    d = req.json()
    request = d.get("request") if isinstance(d.get("request"), dict) else None
    if request is None or not d.get("row_id"):
        raise ApiError(400, "Send {\"request\": <the /api/estimate request>, "
                            "\"row_id\": <a line's row_id>}.")
    for k in ep.FORBIDDEN_INPUTS:
        if k in request:
            raise ApiError(400, "'%s' is not an accepted input." % k)
    try:
        return ENGINE.references(request, d["row_id"])
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    except ep.BadRequest as e:
        raise ApiError(400, str(e))


def _top_references(request, result):
    """Each priced line's top-3 references, for the export (None when the
    request is absent or the reference index is not ready)."""
    if not isinstance(request, dict) or not ENGINE.refs_state.get("ready"):
        return None
    if any(k in request for k in ep.FORBIDDEN_INPUTS):
        return None
    try:
        return ENGINE.top_references(request)
    except Exception as e:  # noqa: BLE001 - the export goes out without them
        log.warning("references for the export failed: %s", e)
        return None


def _export(req, kind):
    d = req.json()
    result = d.get("result") if isinstance(d.get("result"), dict) else None
    refs = _top_references(d.get("request"), result) if result else None
    if refs is not None and result is not None:
        result = dict(result, top_references=refs)
    stamp = time.strftime("%Y%m%d-%H%M")
    try:
        if kind == "csv":
            data = exporter.to_csv(result)
            return FileReply(data, "text/csv; charset=utf-8",
                             "bid_estimate_%s.csv" % stamp)
        if not exporter.xlsx_available():
            raise ApiError(501, "XLSX export needs openpyxl, which is not "
                                "installed; use CSV.")
        data = exporter.to_xlsx(result)
        return FileReply(
            data, "application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.sheet", "bid_estimate_%s.xlsx" % stamp)
    except exporter.ExportError as e:
        raise ApiError(400, str(e))


@route("POST", "/api/export/csv")
def r_export_csv(req):
    return _export(req, "csv")


@route("POST", "/api/export/xlsx")
def r_export_xlsx(req):
    return _export(req, "xlsx")


@route("POST", "/api/bidtab/parse")
def r_bidtab_parse(req):
    name, data = _uploaded_file(req.headers, req.query, req.body)
    return _with_temp(name, data, lambda path: bidtab.parse(path, name))


@route("GET", "/api/bidtab/inbox")
def r_bidtab_inbox(req):
    return {"files": bidtab.inbox(), "dir": bidtab.INBOX}


# -- the OPCC worksheet (output only) -------------------------------------------

def _plain_request(request):
    """The estimate request, refused if it carries an OPCC input."""
    if not isinstance(request, dict):
        raise ApiError(400, "Send the /api/estimate request as \"request\".")
    for k in ep.FORBIDDEN_INPUTS:
        if k in request:
            raise ApiError(400, "'%s' is not an accepted input." % k)
    return request


@route("POST", "/api/worksheet/refs")
def r_worksheet_refs(req):
    d = req.json()
    try:
        return ENGINE.worksheet_refs(_plain_request(d.get("request")))
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    except ep.BadRequest as e:
        raise ApiError(400, str(e))


def _worksheet_export(req, kind):
    import worksheet
    d = req.json()
    result = d.get("result") if isinstance(d.get("result"), dict) else None
    ws = d.get("worksheet") if isinstance(d.get("worksheet"), dict) else None
    if result is None or ws is None:
        raise ApiError(400, "Send {result, request, worksheet}.")
    refs = None
    if isinstance(d.get("request"), dict) and ENGINE.refs_state.get("ready"):
        try:
            refs = ENGINE.worksheet_refs(_plain_request(d["request"]))
        except (ep.BadRequest, ep.EngineNotReady) as e:
            log.warning("worksheet references failed: %s", e)
    stamp = time.strftime("%Y%m%d-%H%M")
    if kind == "csv":
        return FileReply(worksheet.to_csv(result, ws, refs),
                         "text/csv; charset=utf-8", "opcc_worksheet_%s.csv" % stamp)
    if not exporter.xlsx_available():
        raise ApiError(501, "XLSX export needs openpyxl; use CSV.")
    return FileReply(worksheet.to_xlsx(result, ws, refs),
                     "application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet", "opcc_worksheet_%s.xlsx" % stamp)


@route("POST", "/api/worksheet/export/csv")
def r_worksheet_csv(req):
    return _worksheet_export(req, "csv")


@route("POST", "/api/worksheet/export/xlsx")
def r_worksheet_xlsx(req):
    return _worksheet_export(req, "xlsx")


@route("GET", "/api/worksheet/list")
def r_worksheet_list(req):
    import worksheet
    return {"worksheets": worksheet.listing(),
            "folder": "ui/.local/worksheets (local; not in git)"}


@route("GET", "/api/worksheet/load")
def r_worksheet_load(req):
    import worksheet
    try:
        return worksheet.load((req.query.get("file") or [""])[0])
    except FileNotFoundError as e:
        raise ApiError(404, str(e))
    except ValueError:
        raise ApiError(400, "That worksheet file is not valid JSON.")


@route("POST", "/api/worksheet/save")
def r_worksheet_save(req):
    import worksheet
    d = req.json()
    try:
        return worksheet.save(d.get("name") or "worksheet", d.get("worksheet"))
    except ValueError as e:
        raise ApiError(400, str(e))


# -- full bid tabulations and original documents ------------------------------

def _letting_key(req) -> str:
    key = (req.query.get("key") or [""])[0].strip()
    if not key:
        raise ApiError(400, "Send ?key=<letting key>.")
    return key


def _letting_view(req):
    key = _letting_key(req)
    if not ENGINE.state.get("ready"):
        raise ApiError(503, "The estimator is still loading.")
    try:
        v = ENGINE.tabs.view(key, ENGINE)
    except OSError as e:
        raise ApiError(503, "master_panel.csv could not be read: %s" % e)
    if v is None:
        raise ApiError(404, "No public letting with that key.")
    return v


@route("GET", "/api/letting")
def r_letting(req):
    return _letting_view(req)


@route("GET", "/api/letting/csv")
def r_letting_csv(req):
    import lettings
    v = _letting_view(req)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in (v.get("project") or "letting"))[:60]
    return FileReply(lettings.to_csv(v), "text/csv; charset=utf-8",
                     "bid_tab_%s.csv" % safe)


@route("GET", "/api/source-file")
def r_source_file(req):
    """The local copy of a public letting's original document: only a file
    that resolves inside data/external/ AND is the source of a public
    letting. Top-level data/ files, ui/.inbox and ui/.cache are never
    reachable."""
    import lettings
    rel = (req.query.get("path") or [""])[0]
    try:
        ENGINE.tabs.ensure()
        known = ENGINE.tabs.files
    except OSError:
        raise ApiError(503, "The letting index is not available.")
    full = lettings.resolve_source(rel, known)
    if full is None:
        raise ApiError(404, "Not a public letting's original document.")
    ctype, inline = lettings.content_type(full)
    with open(full, "rb") as fh:
        data = fh.read()
    return FileReply(data, ctype, os.path.basename(full), inline=inline,
                     headers={"X-Content-Type-Options": "nosniff",
                              "Cache-Control": "no-store"})


# -- the optional Claude reader (opt-in, per document) -------------------------

def _consent(req) -> bool:
    return (req.query.get("consent") or [""])[0].strip().lower() in (
        "yes", "true", "1")


def _claude(fn, req):
    """Run one Claude-reader call on the uploaded PDF; typed errors become
    HTTP errors with their code. The upload is a temporary file outside
    ui/ and is deleted afterwards."""
    name, data = _uploaded_file(req.headers, req.query, req.body)
    if not name.lower().endswith(".pdf"):
        raise ApiError(400, "Only PDF files can be read with Claude here.",
                       {"code": "not_pdf"})

    def run(path):
        try:
            return fn(path, name)
        except claude_reader.ClaudeReaderError as e:
            d = e.to_dict()
            d.pop("detail", None)
            raise ApiError(e.http, e.message, d)
    return _with_temp(name, data, run)


@route("GET", "/api/claude/status")
def r_claude_status(req):
    if pm.enabled():
        return dict(pm.CLAUDE_OFF)
    return claude_reader.status(refresh=True)


@route("POST", "/api/claude/preview")
def r_claude_preview(req):
    return _claude(lambda path, name: claude_reader.preview(path, name), req)


@route("POST", "/api/claude/count")
def r_claude_count(req):
    consent = _consent(req)
    return _claude(lambda path, name: claude_reader.count_tokens(
        path, consent=consent, name=name), req)


@route("POST", "/api/claude/read")
def r_claude_read(req):
    consent = _consent(req)
    return _claude(lambda path, name: claude_reader.read_pdf(
        path, consent=consent, name=name), req)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class FileReply:
    def __init__(self, data: bytes, ctype: str, filename: str,
                 inline: bool = False, headers: Optional[dict] = None):
        self.data, self.ctype, self.filename = data, ctype, filename
        self.inline = inline
        self.headers = headers or {}


class Request:
    def __init__(self, method, path, query, headers, body):
        self.method, self.path, self.query = method, path, query
        self.headers, self.body = headers, body
        self._json = None

    def json(self) -> dict:
        if self._json is None:
            self._json = _json_body(self.body)
        return self._json


def dispatch(method: str, raw_path: str, headers=None, body: bytes = b"",
             ) -> Tuple[int, str, bytes, Dict[str, str]]:
    """The API as a function: (status, content type, body, extra headers)."""
    headers = headers or {}
    u = urllib.parse.urlsplit(raw_path)
    path = u.path
    query = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if path.startswith("/api/"):
        if pm.enabled():
            off = pm.blocked(method, path)
            if off:
                return _json_reply(404, {"detail": "%s is not available in "
                                                   "the public demo." % off,
                                         "public": True})
        fn = ROUTES.get((method, path))
        if fn is None:
            known = {p for (m, p) in ROUTES}
            st = 405 if path in known else 404
            return _json_reply(st, {"detail": "%s %s is not an API route"
                                              % (method, path)})
        try:
            out = fn(Request(method, path, query, headers, body or b""))
        except ApiError as e:
            return _json_reply(e.status, dict(e.extra, detail=e.message))
        except Exception as e:  # noqa: BLE001
            if pm.enabled():
                # no exception text or traceback to the public: logged here
                eid = "%08x" % (time.time_ns() & 0xFFFFFFFF)
                log.error("unhandled error %s on %s: %s\n%s", eid, path, e,
                          traceback.format_exc())
                return _json_reply(500, {"detail": "Internal error (%s)."
                                                   % eid, "where": path})
            log.error("unhandled error on %s: %s", path, e)
            return _json_reply(500, {
                "error": "%s: %s" % (type(e).__name__, e), "where": path,
                "traceback": traceback.format_exc().splitlines()[-6:]})
        if isinstance(out, FileReply):
            fn = out.filename.replace('"', "").replace("\r", "").replace("\n", "")
            return (200, out.ctype, out.data, dict(
                out.headers, **{"Content-Disposition": '%s; filename="%s"' % (
                    "inline" if out.inline else "attachment",
                    fn.encode("ascii", "replace").decode("ascii"))}))
        return _json_reply(200, out)
    if method not in ("GET", "HEAD"):
        return _json_reply(405, {"detail": "method not allowed"})
    return _static(path)


def _json_reply(status, obj):
    data = json.dumps(_clean(obj), allow_nan=False).encode("utf-8")
    return status, "application/json; charset=utf-8", data, {}


def _static(path: str):
    rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
    full = os.path.realpath(os.path.join(STATIC_DIR, rel))
    root = os.path.realpath(STATIC_DIR)
    if not (full == root or full.startswith(root + os.sep)):
        return 404, "text/plain; charset=utf-8", b"not found", {}
    if os.path.isdir(full):
        full = os.path.join(full, "index.html")
    if not os.path.isfile(full):
        return 404, "text/plain; charset=utf-8", b"not found", {}
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",):
        ctype += "; charset=utf-8"
    with open(full, "rb") as fh:
        data = fh.read()
    if pm.enabled() and os.path.basename(full) == "index.html":
        data = pm.mark_page(data)
    # Uncached: a local tool whose source sits next to the data it reads;
    # editing app.js and refreshing has to show the edit.
    return 200, ctype, data, {"Cache-Control": "no-store, must-revalidate",
                              "Pragma": "no-cache"}


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BidEstimator/" + VERSION
    protocol_version = "HTTP/1.1"

    def version_string(self):
        if pm.enabled():
            return self.server_version     # no Python version to the public
        return super().version_string()

    def setup(self):
        if pm.enabled():
            # an idle or slow connection is dropped instead of holding a
            # thread (StreamRequestHandler applies it to the socket)
            self.timeout = pm.CFG.socket_timeout
        super().setup()

    def _serve(self, method):
        if pm.enabled():
            early = pm.before(method, self.path, self.headers,
                              self.client_address[0], self.rfile.read)
            if early is not None:
                st, ctype, data, extra = early
                self._send(st, ctype, b"" if method == "HEAD" else data,
                           extra, length=len(data))
                # a body left unread would be taken for the next request
                if (self.headers.get("Content-Length") or "0").strip() \
                        not in ("", "0"):
                    self.close_connection = True
                return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > _max_upload() + 1024 * 1024:
            self._send(413 if n > 0 else 400, "application/json", json.dumps(
                {"detail": "Request body too large." if n > 0
                 else "Bad Content-Length."}).encode(), {})
            self.close_connection = True
            return
        body = self.rfile.read(n) if n > 0 else b""
        hdrs = {k: v for k, v in self.headers.items()}
        if pm.enabled():
            path = urllib.parse.urlsplit(self.path).path
            with pm.heavy_slot(path) as ok:
                if not ok:
                    st, ctype, data, extra = _json_reply(
                        503, {"detail": "The server is busy; try again in a "
                                        "moment."})
                    extra = {"Retry-After": "5"}
                else:
                    st, ctype, data, extra = dispatch(method, self.path, hdrs,
                                                      body)
        else:
            st, ctype, data, extra = dispatch(method, self.path, hdrs, body)
        self._send(st, ctype, b"" if method == "HEAD" else data, extra,
                   length=len(data))

    def _send(self, status, ctype, data, extra, length=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length",
                         str(len(data) if length is None else length))
        if pm.enabled():
            for k, v in pm.security_headers().items():
                if k not in extra:
                    self.send_header(k, v)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._serve("GET")

    def do_HEAD(self):  # noqa: N802
        self._serve("HEAD")

    def do_POST(self):  # noqa: N802
        self._serve("POST")

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            log.info("%s %s", self.command, fmt % args)


def make_server(host: str = "127.0.0.1", port: int = 8017) -> ThreadingHTTPServer:
    if pm.enabled():
        srv = pm.BoundedThreadingHTTPServer((host, port), Handler,
                                            pm.CFG.max_connections)
    else:
        srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def serve(host: str = "127.0.0.1", port: int = 8017) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: "
                               "%(message)s", datefmt="%H:%M:%S")
    srv = make_server(host, port)
    start_background_load()
    if pm.enabled():
        log.info("PUBLIC DEMO MODE: Claude reader, bid-tab inbox, server-side "
                 "writes and local original files are off; access code %s; "
                 "uploads up to %d MB, temp copies in %s (%s)",
                 "required" if pm.CFG.access_code else "not set",
                 pm.CFG.max_upload_mb, pm.CFG.tmpdir or "the system temp "
                 "directory", pm.CFG.tmp_kind or "?")
    log.info("Bid Estimator on http://%s:%d/ (loading the estimator tables "
             "in the background)", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
