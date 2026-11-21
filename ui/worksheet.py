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


def share_pct(line: dict) -> Optional[float]:
    """The share of an owner-set line given as a share of the subtotal
    ("Contingency, 10%"), else None."""
    if line.get("status") != "owner_set":
        return None
    sh = line.get("amount_from_pct")
    return _f(sh.get("pct")) if isinstance(sh, dict) else None


#: The server's rule for a contingency (engine_production.contingency_worded),
#: for a result saved before it marked its lines `below_subtotal`.
_CONTINGENCY = re.compile(r"contingenc", re.I)
_LUMP = re.compile(r"^(?:|LS|L\.?\s*S\.?|L/S|LUMP(?:\s*SUM)?|ALLOW(?:ANCE)?"
                   r"|ALW|ALLW|DOL|\$)$", re.I)


def below_subtotal(line: dict) -> bool:
    """A contingency: shown below the subtotal, not among the items (user,
    2026-11-21: "first the subtotal, then the contingency, then the total")."""
    if "below_subtotal" in line:
        return bool(line.get("below_subtotal"))
    if line.get("status") != "owner_set":
        return False
    if share_pct(line) is not None:
        return True
    u = str(line.get("unit") or "").strip()
    lump = bool(_LUMP.match(u)) or (u.upper() in ("EA", "EACH")
                                    and not ((_f(line.get("quantity")) or 0) > 1))
    return lump and bool(_CONTINGENCY.search(line.get("description") or ""))


def line_amount(line: dict, w: dict,
                subtotal: Optional[float] = None) -> Optional[float]:
    """The estimator's amount for one result line: owner-set money at face
    value, a vendor quote as given, else their unit price x the quantity. A
    line given as a share is that share of `subtotal`, the estimator's own
    amounts for every line that is not a contingency -- in the model's P50 it
    is the same share of the model's subtotal."""
    if line.get("status") == "owner_set":
        pct = share_pct(line)
        if pct is not None and subtotal is not None:
            return pct / 100.0 * subtotal
        return _f(line.get("fixed_amount"))
    if w.get("src") == "quote" and _f(w.get("your_amount")) is not None:
        return _f(w.get("your_amount"))
    u, q = _f(w.get("your_unit")), _f(line.get("quantity"))
    if u is None or q is None:
        return None
    return u * q


def totals(result: dict, ws: dict) -> dict:
    """Your OPCC from the worksheet lines, recomputed here (the browser's
    arithmetic is never trusted), laid out as the page lays it out: the
    items' SUBTOTAL, then the contingency below it (the schedule's
    contingency lines, and your own percentage, both on the subtotal), then
    the TOTAL; and its distance to the model's P50/P80."""
    by = {str(w.get("row_id")): w for w in (ws.get("lines") or [])
          if isinstance(w, dict)}
    lines = result.get("lines") or []
    sub, missing, owner_set, quotes, not_covered = 0.0, [], 0.0, 0.0, 0.0
    for l in lines:
        if below_subtotal(l):
            continue
        w = by.get(str(l.get("row_id"))) or {}
        a = line_amount(l, w)
        if a is None:
            missing.append(l.get("row_id"))
            continue
        sub += a
        if l.get("status") == "owner_set":
            owner_set += a
        elif w.get("src") == "quote":
            quotes += a
        if l.get("status") in NOT_COVERED:
            not_covered += a
    rows, below = [], []
    sched = 0.0
    for l in lines:
        w = by.get(str(l.get("row_id"))) or {}
        if below_subtotal(l):
            a = line_amount(l, w, sub)
            sched += a or 0.0
            below.append((l, w, a))
        else:
            rows.append((l, w, line_amount(l, w)))
    c = _f(ws.get("contingency_pct"))
    cont = sub * c / 100.0 if c else 0.0
    yours = sub + sched + cont
    h = result.get("headline") or {}
    p50, p80 = _f(h.get("p50")), _f(h.get("p80"))
    hc = h.get("contingency") or {}
    # P50 / P80 leave the not-covered lines out, your OPCC has your amounts
    # for them (vendor quotes): the comparison adds those amounts to the
    # model's side too, so it compares like with like
    m50 = p50 + not_covered if p50 else p50
    m80 = p80 + not_covered if p80 else p80

    def diff(m):
        return None if not m else {"amount": yours - m, "ratio": yours / m - 1}

    def split(k):
        v = hc.get(k) or {}
        return (_f(v.get("subtotal")), _f(v.get("contingency"))) if v else (None, None)

    return {"subtotal": sub, "schedule_contingency": sched,
            "contingency_pct": c, "contingency": cont,
            "your_opcc": yours, "owner_set": owner_set, "quotes": quotes,
            "not_covered": not_covered,
            "missing": missing, "p50": p50, "p80": p80,
            "p50_split": split("p50"), "p80_split": split("p80"),
            "model_p50": m50, "model_p80": m80,
            "vs_p50": diff(m50), "vs_p80": diff(m80),
            "suggested_contingency_pct": (100.0 * (p80 / p50 - 1))
            if (p50 and p80) else None,
            "rows": rows, "below": below}


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


#: The deliverable's own sheet, laid out as an OPCC is (user, 2026-11-21,
#: showing one: items, Sub-Total, Construction Contingency (10%), Project
#: Total): the estimator's prices only, no model or reference columns.
OPCC_HEADER = ["Item No.", "Description", "Units", "Quantity", "Unit Cost",
               "Total Cost", "Date", "Unit Cost Source"]
OPCC_TITLE = "OPINION OF PROBABLE CONSTRUCTION COSTS"


def _txt(v, n=300) -> str:
    return ("" if v is None else str(v))[:n]


def _opcc_unit(l: dict, w: dict, a: Optional[float]) -> Optional[float]:
    """The line's unit cost in the OPCC: your price, or for a lump amount
    (owner-set money, a vendor quote) the amount over the quantity."""
    q = _f(l.get("quantity"))
    if l.get("status") == "owner_set" or w.get("src") == "quote":
        return (a / q) if (a is not None and q) else None
    return _f(w.get("your_unit"))


def _below_label(l: dict) -> str:
    pct = share_pct(l)
    return "%s (%s)" % (_txt(l.get("description"), 200) or "Contingency",
                        "%g%% of the sub-total" % pct if pct is not None
                        else "owner's fixed amount")


def _extra_label(t: dict) -> str:
    return "%s (%g%%)" % ("Additional Construction Contingency" if t["below"]
                          else "Construction Contingency",
                          t["contingency_pct"])


def opcc_rows(t: dict, ws: dict) -> list:
    """The OPCC as values (the CSV's first block)."""
    rows = []
    for l, w, a in t["rows"]:
        q = _f(l.get("quantity"))
        rows.append([_txt(l.get("item_no"), 20), _txt(l.get("description")),
                     _txt(l.get("unit"), 12), q if q is not None else "",
                     _m(_opcc_unit(l, w, a)), _m(a),
                     _txt(w.get("source_date"), 10),
                     _txt(w.get("source") or w.get("src_label"))])
    rows.append([""] * 8)
    rows.append(["", "Sub-Total", "", "", "", _m(t["subtotal"]), "", ""])
    for l, w, a in t["below"]:
        rows.append([_txt(l.get("item_no"), 20), _below_label(l), "", "", "",
                     _m(a), "", _txt(w.get("note"))])
    if t["contingency"]:
        rows.append(["", _extra_label(t), "", "", "", _m(t["contingency"]),
                     "", ""])
    rows.append(["", "Project Total", "", "", "", _m(t["your_opcc"]), "", ""])
    return rows


def _opcc_sheet(sh, t: dict, ws: dict) -> list:
    """The OPCC itself, laid out the way an OPCC is delivered (user's team,
    2026-11-21), and generic: no firm, no names, no house style.
    - A title, the project block (date, prepared by, project no.) and
      ESTIMATED QUANTITIES.
    - The items in a bordered table with wrapped text and their unit cost
      source.
    - Sub-Total, the contingency and Project Total.
    - A blank interim-review block for the engineer.
    Returns the (cell, formula) pairs to write after the text-only pass, so
    the totals stay live in Excel."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    thin = Side(style="thin", color="BFBFBF")
    dark = Side(style="medium", color="595959")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="D9D9D9")
    total_fill = PatternFill("solid", fgColor="F2F2F2")
    bold = Font(bold=True)
    sh.sheet_view.showGridLines = False
    for col, wd in zip("ABCDEFGH", (9, 46, 8, 12, 13, 15, 11, 50)):
        sh.column_dimensions[col].width = wd
    # title and project block
    sh.merge_cells("A1:H1")
    sh["A1"] = OPCC_TITLE
    sh["A1"].font = Font(bold=True, size=14)
    sh["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sh.row_dimensions[1].height = 26
    sh.merge_cells("A3:D5")
    sh["A3"] = _txt(ws.get("project_name"), 120)
    sh["A3"].font = Font(bold=True, size=12)
    sh["A3"].alignment = Alignment(wrap_text=True, vertical="center")
    for r, lab, val in ((3, "DATE:", _dt.date.today().isoformat()),
                        (4, "PREPARED BY:", _txt(ws.get("prepared_by"), 60)),
                        (5, "PROJECT NO.:", _txt(ws.get("project_no"), 60))):
        c = sh.cell(row=r, column=5, value=lab)
        c.font = bold
        c.alignment = Alignment(horizontal="right")
        sh.merge_cells(start_row=r, start_column=6, end_row=r, end_column=8)
        sh.cell(row=r, column=6, value=val).alignment = Alignment(horizontal="left")
    sh["A7"] = "ESTIMATED QUANTITIES"
    sh["A7"].font = Font(bold=True, size=11)
    # the table
    head = 8
    for j, name in enumerate(OPCC_HEADER, 1):
        c = sh.cell(row=head, column=j, value=name)
        c.font = bold
        c.fill = head_fill
        c.border = box
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sh.row_dimensions[head].height = 20
    align = {1: Alignment(horizontal="center", vertical="top"),
             2: Alignment(wrap_text=True, vertical="top"),
             3: Alignment(horizontal="center", vertical="top"),
             4: Alignment(horizontal="right", vertical="top"),
             5: Alignment(horizontal="right", vertical="top"),
             6: Alignment(horizontal="right", vertical="top"),
             7: Alignment(horizontal="center", vertical="top"),
             8: Alignment(wrap_text=True, vertical="top")}
    small = Font(size=10, color="404040")
    formulas = []
    first = r = head + 1
    for l, w, a in t["rows"]:
        q = _f(l.get("quantity"))
        unit = _opcc_unit(l, w, a)
        vals = {1: _txt(l.get("item_no"), 20), 2: _txt(l.get("description")),
                3: _txt(l.get("unit"), 12), 4: q, 5: unit, 6: None,
                7: _txt(w.get("source_date"), 10),
                8: _txt(w.get("source") or w.get("src_label"))}
        # full precision in the cell (the format shows whole dollars, or
        # cents when there are any), so Excel's totals equal the page's
        if unit is not None and q is not None:
            formulas.append(("F%d" % r, "=D%d*E%d" % (r, r)))
        elif a is not None:
            vals[6] = a
        for j in range(1, 9):
            c = sh.cell(row=r, column=j, value=vals[j] if vals[j] != "" else None)
            c.border = box
            c.alignment = align[j]
        sh.cell(row=r, column=8).font = small
        qc = sh.cell(row=r, column=4)
        qc.number_format = "#,##0" if (q is None or float(q).is_integer()) else "#,##0.00"
        ec = sh.cell(row=r, column=5)
        cents = isinstance(ec.value, float) and abs(ec.value - round(ec.value)) > 0.004
        ec.number_format = '"$"#,##0.00' if cents else '"$"#,##0'
        sh.cell(row=r, column=6).number_format = '"$"#,##0'
        r += 1
    last = r - 1
    # the totals, below a blank row
    sub = r + 1

    def total_row(row, label, strong=False):
        for j in range(1, 9):
            c = sh.cell(row=row, column=j)
            c.border = Border(top=dark if row == sub else thin, bottom=thin,
                              left=thin if j == 1 else None, right=thin if j == 8 else None)
            if strong:
                c.fill = total_fill
        lc = sh.cell(row=row, column=2, value=label)
        lc.font = Font(bold=True, size=12 if strong else 11)
        fc = sh.cell(row=row, column=6)
        fc.number_format = '"$"#,##0'
        fc.alignment = Alignment(horizontal="right")
        fc.font = Font(bold=True, size=12 if strong else 11)

    total_row(sub, "Sub-Total")
    formulas.append(("F%d" % sub, ("=SUM(F%d:F%d)" % (first, last))
                     if last >= first else "=0"))
    conts, r = [], sub + 1
    for l, w, a in t["below"]:
        pct = share_pct(l)
        total_row(r, _below_label(l))
        sh.cell(row=r, column=2).font = Font(size=11)
        sh.cell(row=r, column=6).font = Font(size=11)
        sh.cell(row=r, column=1, value=_txt(l.get("item_no"), 20)).alignment = align[1]
        if pct is not None:
            formulas.append(("F%d" % r, "=F%d*%r" % (sub, pct / 100.0)))
        elif a is not None:
            sh.cell(row=r, column=6, value=a)
        sh.cell(row=r, column=8, value=_txt(w.get("note"))).font = small
        conts.append(r)
        r += 1
    if t["contingency"]:
        total_row(r, _extra_label(t))
        sh.cell(row=r, column=2).font = Font(size=11)
        sh.cell(row=r, column=6).font = Font(size=11)
        formulas.append(("F%d" % r, "=F%d*%r" % (sub, t["contingency_pct"] / 100.0)))
        conts.append(r)
        r += 1
    tot = r
    total_row(tot, "Project Total", strong=True)
    for j in range(1, 9):
        c = sh.cell(row=tot, column=j)
        c.border = Border(top=dark, bottom=Side(style="double", color="595959"),
                          left=thin if j == 1 else None, right=thin if j == 8 else None)
    formulas.append(("F%d" % tot, "=" + "+".join("F%d" % k for k in [sub] + conts)))
    sh.row_dimensions[tot].height = 20
    # a blank interim-review block: the standard wording, no names
    r = tot + 3
    lines = ("INTERIM REVIEW ONLY",
             "Document incomplete: not intended for permit or construction.",
             "Engineer: ______________________    P.E. No.: ____________    Date: ____________")
    for k, text in enumerate(lines):
        sh.merge_cells(start_row=r + k, start_column=2, end_row=r + k, end_column=6)
        c = sh.cell(row=r + k, column=2, value=text)
        c.font = Font(bold=(k == 0), size=11 if k == 0 else 10)
        c.alignment = Alignment(horizontal="center" if k == 0 else "left", vertical="center")
        for j in range(2, 7):
            sh.cell(row=r + k, column=j).border = Border(
                left=dark if j == 2 else None, right=dark if j == 6 else None,
                top=dark if k == 0 else None, bottom=dark if k == len(lines) - 1 else None)
        sh.row_dimensions[r + k].height = 18
    # freeze the header; print landscape, one page wide, the header on every page
    sh.freeze_panes = "A%d" % first
    sh.print_title_rows = "%d:%d" % (head, head)
    sh.page_setup.orientation = "landscape"
    sh.page_setup.fitToWidth = 1
    sh.page_setup.fitToHeight = 0
    sh.sheet_properties.pageSetUpPr.fitToPage = True
    sh.page_margins.left = sh.page_margins.right = 0.4
    sh.page_margins.top = sh.page_margins.bottom = 0.5
    sh.oddFooter.center.text = "Page &P of &N"
    return formulas


def _tidy_sheet(sh, title_row=1, header_row=2):
    """The other sheets of the export (Summary, Lines, References): a bold
    title, a gray header that stays on screen, sensible widths with wrapped
    long text, and money / quantities shown as numbers."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    thin = Side(style="thin", color="BFBFBF")
    sh.cell(row=title_row, column=1).font = Font(bold=True, size=12)
    has_head = header_row and sh.max_row >= header_row and any(
        c.value not in (None, "") for c in sh[header_row])
    if has_head:
        for c in sh[header_row]:
            if c.value in (None, ""):
                continue
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9D9D9")
            c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sh.freeze_panes = "A%d" % (header_row + 1)
    for col in range(1, sh.max_column + 1):
        letter = get_column_letter(col)
        cells = [sh.cell(row=r, column=col) for r in range((header_row or title_row) + 1, sh.max_row + 1)]
        longest = max((len(str(c.value)) for c in cells if c.value is not None), default=8)
        head_len = len(str(sh.cell(row=header_row, column=col).value or "")) if has_head else 0
        width = min(46, max(9, longest + 2, min(head_len, 22) + 2))
        sh.column_dimensions[letter].width = width
        for c in cells:
            v = c.value
            if isinstance(v, bool):
                continue
            if isinstance(v, float):
                c.number_format = "#,##0.00"
            elif isinstance(v, int):
                c.number_format = "#,##0"
            elif isinstance(v, str) and len(v) > width:
                c.alignment = Alignment(wrap_text=True, vertical="top")


def sections(result: dict, ws: dict, refs: Optional[dict]) -> list:
    """(title, header, rows) for the CSV / XLSX export -- the estimator's
    deliverable: their prices, notes and totals, the model's P50 / P80 for
    comparison, and a references appendix with source links."""
    t = totals(result, ws)
    inp = result.get("inputs") or {}
    out = [("OPCC estimate", OPCC_HEADER, opcc_rows(t, ws))]
    head_rows = [
        ["Project", ws.get("project_name") or ""],
        ["Owner", inp.get("owner_label") or ""],
        ["Work type", inp.get("work_type_label") or ""],
        ["Market", inp.get("market_label") or inp.get("market") or ""],
        ["Bid date", inp.get("bid_date") or ""],
        ["Prepared", _dt.date.today().isoformat()],
        [],
        # subtotal, then the contingency below it, then the total
        ["Subtotal (your line amounts)", _m(t["subtotal"])],
        ["  of which owner-set allowances", _m(t["owner_set"])],
        ["  of which vendor quotes", _m(t["quotes"])],
    ]
    for l, w, a in t["below"]:
        pct = share_pct(l)
        head_rows.append([
            "Contingency: %s%s" % (l.get("description") or "",
                                   " (%g%% of the subtotal)" % pct
                                   if pct is not None else
                                   " (the owner's fixed amount)"), _m(a)])
    extra = ("Additional construction contingency" if t["below"]
             else "Construction contingency")
    head_rows += [
        [extra + " %", "" if t["contingency_pct"] is None else t["contingency_pct"]],
        [extra + " (on the subtotal)", _m(t["contingency"])],
        ["YOUR OPCC (project total)", _m(t["your_opcc"])],
        [],
        ["Model P50 (expected low bid)", _m(t["p50"])],
    ]
    s50, c50 = t["p50_split"]
    if s50 is not None:
        head_rows += [["  P50 subtotal", _m(s50)], ["  P50 contingency", _m(c50)]]
    head_rows.append(["Model P80", _m(t["p80"])])
    s80, c80 = t["p80_split"]
    if s80 is not None:
        head_rows += [["  P80 subtotal", _m(s80)], ["  P80 contingency", _m(c80)]]
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
    # below the items: SUBTOTAL, the contingency, TOTAL (your amounts and
    # the model's P50 side by side)
    blank = [""] * 16

    def foot(label, model, yours, src=""):
        r = list(blank)
        r[1], r[5], r[12], r[13] = label, _m(model), _m(yours), src
        return r
    s50 = t["p50_split"][0] if t["p50_split"][0] is not None else t["p50"]
    rows.append(list(blank))
    rows.append(foot("SUBTOTAL", s50, t["subtotal"]))
    for l, w, a in t["below"]:
        pct = share_pct(l)
        r = foot(l.get("description") or "Contingency", l.get("amount_p50"), a,
                 "%g%% of the subtotal" % pct if pct is not None
                 else "owner's fixed amount")
        r[0], r[4], r[14] = l.get("item_no") or "", ("%g%%" % pct) if pct is not None else "", w.get("note") or ""
        rows.append(r)
    if t["contingency"]:
        rows.append(foot("%s (%g%% of the subtotal)" % (
            "Additional construction contingency" if t["below"]
            else "Construction contingency", t["contingency_pct"]), None,
            t["contingency"], "yours only: not in P50 / P80"))
    rows.append(foot("PROJECT TOTAL", t["p50"], t["your_opcc"]))
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
    formulas = []
    for title, head, rows in sections(result, ws, refs):
        sh = wb.active if first else wb.create_sheet()
        first = False
        sh.title = {"OPCC estimate": "OPCC", "OPCC worksheet": "Summary",
                    "OPCC lines": "Lines",
                    "References appendix": "References"}[title]
        if title == "OPCC estimate":
            formulas = _opcc_sheet(sh, totals(result, ws), ws)
            continue
        sh.append([title])
        if head:
            sh.append(head)
        for r in rows:
            sh.append(list(r))
            if r and (str(r[0]).startswith("YOUR OPCC") or (len(r) > 1 and r[1] in (
                    "Sub-Total", "Project Total", "SUBTOTAL", "PROJECT TOTAL"))):
                for c in sh[sh.max_row]:
                    c.font = bold
        _tidy_sheet(sh, 1, 2 if head else 0)
    from exporter import xlsx_text_only    # "=..." notes stay text
    xlsx_text_only(wb)
    # then the OPCC's own totals, as live formulas (written by this module,
    # never taken from the browser)
    for ref, f in formulas:
        wb["OPCC"][ref] = f
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
