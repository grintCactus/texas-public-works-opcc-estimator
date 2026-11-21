"""The OPCC worksheet: the estimator's own prices, next to the model's.

The page is an assistant for preparing an OPCC. The model's P50 / P80 is one
input; the estimator picks a unit price per line (the model's, a reference
median, a similar project's, or their own), writes a note, adds an optional
contingency, and exports the result as their deliverable.

HARD RULE: what the estimator picks or types here is OUTPUT ONLY. It never
feeds back into the estimator, its tables, the training data or the
calibration. This module only
  * recomputes the worksheet's own totals from what the browser sends,
  * builds the export,
  * saves / loads worksheet files under ui/.local/worksheets/ (local to this
    machine, excluded from git) -- and nowhere else.
Nothing here imports or calls anything that writes data/, src/ or the
engine's tables; the estimate request the browser sends carries no user
price (test_ui.Worksheet checks both).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSHEET_DIR_ENV = "BID_UI_WORKSHEETS"
DEFAULT_DIR = os.path.join(HERE, ".local", "worksheets")
MAX_BYTES = 8 * 1024 * 1024
VERSION = 1


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

#: Line statuses outside P50 / P80 (not covered: a vendor quote's line).
NOT_COVERED = ("not_covered", "not_covered_whole")


def line_amount(line: dict, w: dict) -> Optional[float]:
    """The estimator's amount for one result line: owner-set money at face
    value, a vendor quote as given, else their unit price x the quantity."""
    if line.get("status") == "owner_set":
        return _f(line.get("fixed_amount"))
    if w.get("src") == "quote" and _f(w.get("your_amount")) is not None:
        return _f(w.get("your_amount"))
    u, q = _f(w.get("your_unit")), _f(line.get("quantity"))
    if u is None or q is None:
        return None
    return u * q


def totals(result: dict, ws: dict) -> dict:
    """Your OPCC from the worksheet lines, recomputed here (the browser's
    arithmetic is never trusted), and its distance to the model's P50/P80."""
    by = {str(w.get("row_id")): w for w in (ws.get("lines") or [])
          if isinstance(w, dict)}
    sub, missing, owner_set, quotes, not_covered = 0.0, [], 0.0, 0.0, 0.0
    rows = []
    for l in result.get("lines") or []:
        w = by.get(str(l.get("row_id"))) or {}
        a = line_amount(l, w)
        if a is None:
            missing.append(l.get("row_id"))
        else:
            sub += a
            if l.get("status") == "owner_set":
                owner_set += a
            elif w.get("src") == "quote":
                quotes += a
            if l.get("status") in NOT_COVERED:
                not_covered += a
        rows.append((l, w, a))
    c = _f(ws.get("contingency_pct"))
    cont = sub * c / 100.0 if c else 0.0
    yours = sub + cont
    h = result.get("headline") or {}
    p50, p80 = _f(h.get("p50")), _f(h.get("p80"))
    # P50 / P80 leave the not-covered lines out, your OPCC has your amounts
    # for them (vendor quotes): the comparison adds those amounts to the
    # model's side too, so it compares like with like
    m50 = p50 + not_covered if p50 else p50
    m80 = p80 + not_covered if p80 else p80

    def diff(m):
        return None if not m else {"amount": yours - m, "ratio": yours / m - 1}

    return {"subtotal": sub, "contingency_pct": c, "contingency": cont,
            "your_opcc": yours, "owner_set": owner_set, "quotes": quotes,
            "not_covered": not_covered,
            "missing": missing, "p50": p50, "p80": p80,
            "model_p50": m50, "model_p80": m80,
            "vs_p50": diff(m50), "vs_p80": diff(m80),
            "suggested_contingency_pct": (100.0 * (p80 / p50 - 1))
            if (p50 and p80) else None,
            "rows": rows}


# ---------------------------------------------------------------------------
# Save / load (ui/.local/worksheets only)
# ---------------------------------------------------------------------------

def folder() -> str:
    return os.environ.get(WORKSHEET_DIR_ENV) or DEFAULT_DIR


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())[:80]
    s = s.strip("._-")
    return s or "worksheet"


def _path(fname: str) -> Optional[str]:
    """A worksheet file in the folder: plain name, .json, no path parts."""
    if not isinstance(fname, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,90}\.json",
                                                      fname):
        return None
    d = os.path.realpath(folder())
    p = os.path.realpath(os.path.join(d, fname))
    return p if os.path.dirname(p) == d else None


def save(name: str, doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise ValueError("The worksheet must be a JSON object.")
    body = dict(doc, version=VERSION, name=str(name or "")[:120],
                saved_at=_dt.datetime.now().isoformat(timespec="seconds"),
                output_only=("The estimator's own prices and notes. Output "
                             "only: never read back into the estimator, its "
                             "tables, training or calibration."))
    data = json.dumps(body, indent=1, sort_keys=True).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("The worksheet is too large to save.")
    d = folder()
    os.makedirs(d, exist_ok=True)
    fname = _slug(name) + ".json"
    fd, tmp = tempfile.mkstemp(prefix=".ws_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(d, fname))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"file": fname, "saved_at": body["saved_at"],
            "folder": os.path.relpath(d, os.path.dirname(HERE))
            if d.startswith(os.path.dirname(HERE)) else d}


def listing() -> List[dict]:
    d = folder()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = _path(n)
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"file": n, "name": doc.get("name") or n[:-5],
                    "saved_at": doc.get("saved_at"),
                    "lines": len((doc.get("request") or {}).get("items") or []),
                    "your_opcc": _f((doc.get("totals") or {}).get("your_opcc"))})
    out.sort(key=lambda x: x.get("saved_at") or "", reverse=True)
    return out


def load(fname: str) -> dict:
    p = _path(fname)
    if not p or not os.path.isfile(p):
        raise FileNotFoundError("No saved worksheet %r." % fname)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _m(v):
    return "" if v is None else round(float(v), 2)


def sections(result: dict, ws: dict, refs: Optional[dict]) -> list:
    """(title, header, rows) for the CSV / XLSX export -- the estimator's
    deliverable: their prices, notes and totals, the model's P50 / P80 for
    comparison, and a references appendix with source links."""
    t = totals(result, ws)
    inp = result.get("inputs") or {}
    out = []
    head_rows = [
        ["Project", ws.get("project_name") or ""],
        ["Owner", inp.get("owner_label") or ""],
        ["Work type", inp.get("work_type_label") or ""],
        ["Market", inp.get("market_label") or inp.get("market") or ""],
        ["Bid date", inp.get("bid_date") or ""],
        ["Prepared", _dt.date.today().isoformat()],
        [],
        ["Your line amounts", _m(t["subtotal"])],
        ["  of which owner-set allowances", _m(t["owner_set"])],
        ["  of which vendor quotes", _m(t["quotes"])],
        ["Contingency %", "" if t["contingency_pct"] is None else t["contingency_pct"]],
        ["Contingency", _m(t["contingency"])],
        ["YOUR OPCC", _m(t["your_opcc"])],
        [],
        ["Model P50 (expected low bid)", _m(t["p50"])],
        ["Model P80", _m(t["p80"])],
    ]
    nc = t["not_covered"]
    if nc:
        head_rows += [
            ["  your amounts for the not-covered lines (outside P50 / P80)",
             _m(nc)],
            ["Model P50 + not-covered lines", _m(t["model_p50"])],
            ["Model P80 + not-covered lines", _m(t["model_p80"])],
        ]
    vs = " + not-covered lines" if nc else ""
    head_rows += [
        ["Your OPCC vs P50" + vs, _m((t["vs_p50"] or {}).get("amount")),
         "" if not t["vs_p50"] else "%+.1f%%" % (100 * t["vs_p50"]["ratio"])],
        ["Your OPCC vs P80" + vs, _m((t["vs_p80"] or {}).get("amount")),
         "" if not t["vs_p80"] else "%+.1f%%" % (100 * t["vs_p80"]["ratio"])],
        ["Lines without your price", len(t["missing"])],
        [],
        ["Note", "Your prices and notes are output only; they never feed back "
                 "into the estimator, its tables, training or calibration."],
    ]
    out.append(("OPCC worksheet", None, head_rows))
    rmap = {x["row_id"]: x for x in ((refs or {}).get("lines") or [])}
    rows = []
    for l, w, a in t["rows"]:
        x = rmap.get(l.get("row_id")) or {}
        rg = x.get("range") or {}
        acc = l.get("accuracy") or {}
        rows.append([l.get("item_no"), l.get("description"), l.get("quantity"),
                     l.get("unit"), _m(l.get("unit_price_p50")),
                     _m(l.get("amount_p50")), _m(rg.get("low")),
                     _m(rg.get("median")), _m(rg.get("high")),
                     x.get("n_refs", ""), acc.get("text") or acc.get("tier") or "",
                     _m(w.get("your_unit")) if l.get("status") != "owner_set"
                     else "", _m(a), w.get("src_label") or w.get("src") or "",
                     w.get("note") or "", "; ".join(w.get("judgment") or [])])
    out.append(("OPCC lines",
                ["Item", "Description", "Qty", "Unit", "Model $/unit (P50)",
                 "Model amount (P50)", "Reference low $/unit",
                 "Reference median $/unit", "Reference high $/unit",
                 "References", "Typical accuracy", "Your $/unit",
                 "Your amount", "Your price from", "Note",
                 "Needs your judgment"], rows))
    rows = []
    for l, w, a in t["rows"]:
        x = rmap.get(l.get("row_id")) or {}
        for k, pk in enumerate(x.get("picks") or [], 1):
            src = pk.get("source") or {}
            rows.append([l.get("item_no"), l.get("description"), k,
                         pk.get("project"), pk.get("owner"), pk.get("date"),
                         _m(pk.get("median_unit")), _m(pk.get("low_unit")),
                         _m(pk.get("median_esc")), src.get("document") or "",
                         src.get("url") or "", src.get("mirror_url") or ""])
    out.append(("References appendix",
                ["Item", "Description", "Rank", "Project", "Owner",
                 "Letting date", "Median bid $/unit", "Low bid $/unit",
                 "Median bid in %s $" % ((refs or {}).get("to_year") or "bid-year"),
                 "Source document", "source_url", "mirror_url"],
                rows or [["(none)"]]))
    return out


def to_csv(result, ws, refs) -> bytes:
    import csv
    import io
    from exporter import safe_row      # text is never run as a formula
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# OPCC worksheet -- the estimator's prices next to the "
                "model's P50 / P80"])
    for title, head, rows in sections(result, ws, refs):
        w.writerow([])
        w.writerow(["## " + title])
        if head:
            w.writerow(safe_row(head))
        for r in rows:
            w.writerow(safe_row(r))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(result, ws, refs) -> bytes:
    import io
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    first = True
    bold = Font(bold=True)
    for title, head, rows in sections(result, ws, refs):
        sh = wb.active if first else wb.create_sheet()
        first = False
        sh.title = {"OPCC worksheet": "OPCC", "OPCC lines": "Lines",
                    "References appendix": "References"}[title]
        sh.append([title])
        sh.cell(row=1, column=1).font = bold
        if head:
            sh.append(head)
            for c in sh[sh.max_row]:
                c.font = bold
        for r in rows:
            sh.append(list(r))
            if r and r[0] == "YOUR OPCC":
                for c in sh[sh.max_row]:
                    c.font = bold
        for col in sh.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None),
                        default=8)
            sh.column_dimensions[col[0].column_letter].width = min(60, max(9, width + 1))
    from exporter import xlsx_text_only    # "=..." notes stay text
    xlsx_text_only(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
