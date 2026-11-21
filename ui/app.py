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
Everything else is served from ui/static/ (uncached).

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


def _uploaded_file(headers, query, body) -> Tuple[str, bytes]:
    """(file name, bytes) from a multipart/form-data body (field "file"), or
    from a raw body with ?name=."""
    if len(body) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "File is larger than %d MB."
                       % (MAX_UPLOAD_BYTES // (1024 * 1024)))
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
    return {
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
        "bidtab_import": bidtab.available(),
        "history_search": dict(_matching_state),
        "xlsx_export": exporter.xlsx_available(),
        "corrections": CORRECTIONS.status(),
        "claude_reader": _claude_status(),
    }


def _claude_status():
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


@route("POST", "/api/takeoff/text")
def r_takeoff_text(req):
    text = req.json().get("text") or ""
    if not text.strip():
        raise ApiError(400, "No text supplied.")
    return takeoff.from_text(text).to_dict()


@route("POST", "/api/takeoff/file")
def r_takeoff_file(req):
    name, data = _uploaded_file(req.headers, req.query, req.body)

    def parse(path):
        try:
            return takeoff.from_file(path, name).to_dict()
        except ModuleNotFoundError as e:
            raise ApiError(400, "%s cannot be read here: %s is not installed. "
                                "Save it as .xlsx or .csv, or paste the rows."
                           % (name, e.name))
    return _with_temp(name, data, parse)


@route("POST", "/api/classify")
def r_classify(req):
    d = req.json()
    market = str(d.get("market") or "municipal")
    lines = [ln for ln in (d.get("lines") or []) if isinstance(ln, dict)]
    try:
        out = ENGINE.classify(lines, market)
        out = intake.annotate(ENGINE, lines, out, market, CORRECTIONS,
                              history_ready=bool(_matching_state["ready"]))
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    return {"lines": out}


@route("GET", "/api/corrections")
def r_corrections(req):
    return CORRECTIONS.status()


@route("POST", "/api/corrections")
def r_corrections_save(req):
    d = req.json()
    fam = str(d.get("family") or "").strip()
    try:
        known = {f["id"] for f in ENGINE.options()["families"]}
    except ep.EngineNotReady as e:
        raise ApiError(503, str(e))
    if fam not in known:
        raise ApiError(400, "Unknown item family %r." % fam)
    unit = str(d.get("unit") or "").strip()
    if unit and upstream.norm_unit(unit).upper() not in upstream.KNOWN_UNITS:
        unit = ""
    try:
        entry = CORRECTIONS.remember(d.get("description"), fam, unit,
                                     str(d.get("source") or "picker"))
    except ValueError as err:
        raise ApiError(400, str(err))
    return {"saved": entry, "status": CORRECTIONS.status()}


@route("POST", "/api/corrections/forget")
def r_corrections_forget(req):
    d = req.json()
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
        out.append(d)
    return {"candidates": out}


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

    def _serve(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD_BYTES + 1024 * 1024:
            self._send(413, "application/json", json.dumps(
                {"detail": "Request body too large."}).encode(), {})
            self.close_connection = True
            return
        body = self.rfile.read(n) if n > 0 else b""
        hdrs = {k: v for k, v in self.headers.items()}
        st, ctype, data, extra = dispatch(method, self.path, hdrs, body)
        self._send(st, ctype, b"" if method == "HEAD" else data, extra,
                   length=len(data))

    def _send(self, status, ctype, data, extra, length=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length",
                         str(len(data) if length is None else length))
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
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def serve(host: str = "127.0.0.1", port: int = 8017) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: "
                               "%(message)s", datefmt="%H:%M:%S")
    srv = make_server(host, port)
    start_background_load()
    log.info("Bid Estimator on http://%s:%d/ (loading the estimator tables "
             "in the background)", host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
