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

from exporter import P70_TEXT, recommended_basis

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSHEET_DIR_ENV = "BID_UI_WORKSHEETS"
DEFAULT_DIR = os.path.join(HERE, ".local", "worksheets")
MAX_BYTES = 8 * 1024 * 1024
VERSION = 1
HISTORY_TEXT = (
    "Each starting price is the median of three matching public projects' "
    "bidder medians within two years of the bid date, adjusted for inflation. "
    "References records the original prices, dates and index factors. "
    "Your rounding and edits remain in Your OPCC.")


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
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
    p50, p70, p80 = (_f(h.get(k)) for k in ("p50", "p70", "p80"))
    rec = recommended_basis(result)
    recommended = p70 if rec == "p70" else p80
    hc = h.get("contingency") or {}
    # Model figures leave the not-covered lines out; your OPCC has your amounts
    # for them (vendor quotes): the comparison adds those amounts to the
    # model's side too, so it compares like with like
    m50 = p50 + not_covered if p50 else p50
    m70 = p70 + not_covered if p70 else p70
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
            "missing": missing, "p50": p50, "p70": p70, "p80": p80,
            "p50_split": split("p50"), "p70_split": split("p70"), "p80_split": split("p80"),
            "model_p50": m50, "model_p70": m70, "model_p80": m80,
            "vs_p50": diff(m50), "vs_p70": diff(m70), "vs_p80": diff(m80),
            "recommended_basis": rec,
            "suggested_contingency_pct": (100.0 * (recommended / p50 - 1))
            if (p50 and recommended) else None,
            "rows": rows, "below": below}


def price_basis_label(result: dict, ws: dict) -> str:
    """Describe worksheet prices without reinterpreting saved model bases."""
    if ws.get("basis") == "history":
        return "recent matching public bid medians, adjusted for inflation"
    rec = recommended_basis(result)
    basis = ws.get("basis") if ws.get("basis") in ("p50", "p70", "p80") else rec
    if basis == "p50":
        return "the model's P50 (expected low bid)"
    m = _f(ws.get("basis_multiplier"))
    role = "recommended OPCC" if basis == rec else "baseline"
    return "the model's %s (%s%s)" % (
        basis.upper(), role, ", P50 x %.2f" % m if m and m > 1 else "")


def price_source(w: dict) -> str:
    """The saved selection is the audit trail, not today's reference order."""
    if w.get("src") != "history":
        return str(w.get("source") or w.get("src_label") or w.get("src") or "")
    h = w.get("history_price") or {}
    if h.get("status") != "available":
        return h.get("reason") or "No recent matching historical price available"
    refs = h.get("references") or []
    sources = "; ".join("%s (%s)" % (p.get("project") or p.get("owner")
                                    or "Public project", p.get("date") or "date unavailable")
                        for p in refs)
    label = "Median of %d matching public projects, adjusted for inflation" % len(refs)
    return label + (": " + sources if sources else "")


def _reference_identity(p: dict) -> tuple:
    """A project can contain several pay items; its key alone is not unique."""
    return tuple(str(p.get(k) if p.get(k) is not None else "")
                 for k in ("key", "item_no", "item", "unit", "quantity"))


def _export_references(w: dict, x: dict) -> list:
    """Keep saved selected pay items even if current reference loading fails."""
    h = w.get("history_price") or {}
    selected = h.get("references") or []
    use = ("Selected history median" if w.get("src") == "history"
           and h.get("status") == "available" else "History evidence, not selected")
    out, seen = [], set()
    for p in selected:
        key = _reference_identity(p)
        if key not in seen:
            out.append((p, use))
            seen.add(key)
    for p in x.get("picks") or []:
        key = _reference_identity(p)
        if key not in seen:
            out.append((p, "Other reference"))
            seen.add(key)
    return out


def _index_text(index: dict) -> str:
    if not index:
        return ""
    parts = ["Public matched-bid index", str(index.get("market") or ""),
             "Price years %s to %s" % (index["from_year"], index["target_year"])
             if index.get("from_year") is not None and index.get("target_year") is not None else "",
             "Available before %s" % index["as_of"] if index.get("as_of") else ""]
    for step in index.get("steps") or []:
        factor = _f(step.get("factor"))
        parts.append("%s to %s: %s matches%s" % (
            step.get("from_year"), step.get("to_year"), step.get("n_matched"),
            ", factor %.8g" % factor if factor is not None else ""))
    parts += [index.get("reason") or ""] + list(index.get("notes") or [])
    return "; ".join(p for p in parts if p)


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


_INVALID_XML_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _export_text(value):
    """Copy export inputs, dropping only characters XML cannot represent.

    User metadata and notes can bypass the takeoff reader's text cleanup.
    Keep printable Unicode, tabs and line breaks, and leave numbers alone.
    """
    if isinstance(value, str):
        return _INVALID_XML_TEXT.sub("", value)
    if isinstance(value, dict):
        return {key: _export_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_export_text(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_export_text(item) for item in value)
    return value


#: The deliverable's own sheet, laid out as an OPCC is (user, 2026-11-21,
#: showing one: items, Sub-Total, Construction Contingency (10%), Project
#: Total): the estimator's prices only, no model or reference columns.
OPCC_HEADER = ["Item No.", "Description", "Units", "Quantity", "Unit Cost",
               "Total Cost", "Unit Cost Source", "Notes"]
OPCC_TITLE = "OPINION OF PROBABLE CONSTRUCTION COSTS"
#: The interim-review box at the bottom right, line for line as on the
#: team's printed OPCC (user, 2026-11-21): the standard Texas legend and
#: blanks for the engineer, P.E. serial no., date, company and firm
#: registration no. Generic: "Company" stands where the firm's name goes.
INTERIM_LINES = ("INTERIM REVIEW ONLY",
                 "Document Incomplete: Not Intended",
                 "For Permit or Construction.",
                 "Engineer : " + "_" * 27,
                 "P.E. Serial No : " + "_" * 22,
                 "Date : " + "_" * 30,
                 "Company",
                 "TBPE Firm Registration No. " + "_" * 12)


def _today(ws: dict) -> "_dt.date":
    """The OPCC's date: the user's own day, sent by the page (the server runs
    on UTC, a day ahead of Texas every evening); the server's date otherwise."""
    v = str((ws or {}).get("today") or "")[:10]
    try:
        d = _dt.date.fromisoformat(v)
    except ValueError:
        return _dt.date.today()
    return d if 2000 <= d.year <= 2100 else _dt.date.today()


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


def show_extra(t: dict) -> bool:
    """Your own contingency always gets its row, 0% included (shown as 0%,
    $0), as on the page: the percentage changes numbers, not the layout."""
    return True


def _extra_label(t: dict) -> str:
    return "%s (%g%%)" % ("Additional Construction Contingency" if t["below"]
                          else "Construction Contingency",
                          t["contingency_pct"] or 0)


def opcc_rows(t: dict, ws: dict) -> list:
    """The OPCC as values (the CSV's first block)."""
    rows = []
    for l, w, a in t["rows"]:
        q = _f(l.get("quantity"))
        rows.append([_txt(l.get("item_no"), 20), _txt(l.get("description")),
                     _txt(l.get("unit"), 12), q if q is not None else "",
                     _m(_opcc_unit(l, w, a)), _m(a),
                     _txt(price_source(w), 2000),
                     _txt(w.get("note"))])
    rows.append([""] * 8)
    rows.append(["", "Sub-Total", "", "", "", _m(t["subtotal"]), "", ""])
    for l, w, a in t["below"]:
        rows.append([_txt(l.get("item_no"), 20), _below_label(l), "", "", "",
                     _m(a), "", _txt(w.get("note"))])
    if show_extra(t):
        rows.append(["", _extra_label(t), "", "", "", _m(t["contingency"] or 0.0),
                     "", ""])
    rows.append(["", "Project Total", "", "", "", _m(t["your_opcc"]), "", ""])
    return rows


def _keep_border(c, **sides):
    """Add border sides to a cell, keeping the sides it already has."""
    from openpyxl.styles import Border
    b = c.border
    c.border = Border(left=sides.get("left", b.left), right=sides.get("right", b.right),
                      top=sides.get("top", b.top), bottom=sides.get("bottom", b.bottom))


def _grid(sh, r1: int, r2: int, c1: int, c2: int, color: str = "000000"):
    """Thin lines on every side of every cell in the block: rows AND columns
    (user, 2026-11-21: "only horizontal lines is not good"), in any viewer
    and on paper (gridlines do not print, and are off on these sheets)."""
    from openpyxl.styles import Border, Side
    t = Side(style="thin", color=color)
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            sh.cell(row=r, column=c).border = Border(left=t, right=t, top=t, bottom=t)


#: Arial / Helvetica advance widths (1/1000 em), to size wrapped rows.
_AW: Dict[str, int] = {}
for _chars, _w in (("ijl'", 222), ("|", 260), (" !,./:;I[\\]ft", 278),
                   ("()-`r", 333), ("{}", 334), ('"', 355), ("*", 389),
                   ("^", 469), ("Jcksvxyz", 500),
                   ("#$0123456789?L_abdeghnopqu", 556), ("+<=>~", 584),
                   ("FTZ", 611), ("&ABEKPSVXY", 667), ("CDHNRUw", 722),
                   ("GOQ", 778), ("Mm", 833), ("%", 889), ("W", 944),
                   ("@", 1015)):
    for _c in _chars:
        _AW[_c] = _w


def text_lines(text, width: float, size: float = 10.0) -> int:
    """How many lines `text` takes wrapped in a column `width` characters
    wide (Excel's unit) at `size` points of Arial, erring on more. openpyxl
    cannot measure text, and a viewer that does not size rows itself (Quick
    Look, phone and mail previews) cuts off what does not fit, so every
    wrapped row is given its height."""
    room = (7.0 * width - 1) * 0.94            # px, with room for font differences
    em = size * 96.0 / 72.0 / 1000.0
    space = _AW[" "] * em
    n = 0
    for para in str(text).split("\n"):
        n += 1
        used = 0.0
        for word in para.split(" "):
            w = sum(_AW.get(ch, 600) for ch in word) * em
            if used and used + space + w > room:
                n += 1
                used = w
            else:
                used += (space if used else 0.0) + w
            while used > room:                 # a word longer than a line
                n += 1
                used -= room
    return max(1, n)


def _opcc_sheet(sh, t: dict, ws: dict) -> list:
    """The OPCC itself, laid out the way an OPCC is delivered (user's team,
    2026-11-21: plain and readable, one total), and generic: no firm, no
    names. The printed block is columns A-F inside one frame:
    - a title and the project block (date, prepared by, project no.);
    - ESTIMATED QUANTITIES: item, description, units, quantity, unit cost,
      total cost, one line per item;
    - Sub-Total, the contingency and Project Total;
    - a blank interim-review box.
    Columns G-H, outside the print area, are the working notes: the source of
    each unit cost and the estimator's note on the line (user, 2026-11-21:
    notes go with the source, outside the OPCC table; no date column). The
    frame and the interim box are heavy black lines and ESTIMATED QUANTITIES
    a gray band, as on the team's OPCC.
    It prints as the team's printed OPCC does (user, 2026-11-21, with their
    printout: "this feel", on Letter): no lines inside the table and none
    under the totals, the title and project block in a bold serif, the rest
    Arial 10; on screen the sheet's own gridlines show both ways (they do not
    print). Every wrapped row is sized to its text.
    Returns (cell, formula, value) for the totals, written after the
    text-only pass so they stay live in Excel; the value is cached in the
    file too, so a viewer that does not calculate shows the right number."""
    from openpyxl.styles import Alignment, Font, Side
    def F(size=10, bold=False, color=None):
        return Font(name="Arial", size=size, bold=bold, color=color)

    def T(size, bold=True):                  # the printout's serif headings
        return Font(name="Times New Roman", size=size, bold=bold)
    med = Side(style="medium", color="000000")
    sh.sheet_view.showGridLines = True       # on screen only; the page prints clean
    widths = dict(zip("ABCDEFGH", (7, 62, 8, 11, 12, 14, 50, 50)))
    for col, wd in widths.items():
        sh.column_dimensions[col].width = wd
    sh.merge_cells("A1:F1")
    sh["A1"] = OPCC_TITLE
    sh["A1"].font = T(14)
    sh["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sh.row_dimensions[1].height = 24
    for col in range(1, 7):
        _keep_border(sh.cell(row=1, column=col), bottom=med)
    name = _txt(ws.get("project_name"), 160)
    sh.merge_cells("A3:C5")
    sh["A3"] = name
    sh["A3"].font = T(16)
    sh["A3"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    n_name = text_lines(name, sum(widths[c] for c in "ABC"), 16)
    if n_name > 2:                           # three rows hold two lines at 16 pt
        for r in (3, 4, 5):
            sh.row_dimensions[r].height = round(n_name * 19.5 / 3 + 1, 1)
    for r, lab, val in ((3, "DATE:", _today(ws)),
                        (4, "PREPARED BY:", _txt(ws.get("prepared_by"), 60)),
                        (5, "PROJECT NO.:", _txt(ws.get("project_no"), 60))):
        sh.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
        sh.cell(row=r, column=4, value=lab).font = T(10)
        c = sh.cell(row=r, column=6, value=val or None)
        c.font = F(10)
        c.alignment = Alignment(horizontal="right", shrink_to_fit=True)
        if r == 3:
            c.number_format = "mm/dd/yyyy"
    sh.merge_cells("A7:C7")
    sh["A7"] = "ESTIMATED QUANTITIES"
    sh["A7"].font = F(10, True)
    from openpyxl.styles import PatternFill
    for col in range(1, 7):                 # a gray band, as on the team's OPCC
        sh.cell(row=7, column=col).fill = PatternFill("solid", fgColor="BFBFBF")
    head = 8
    for j, name_ in enumerate(OPCC_HEADER, 1):
        c = sh.cell(row=head, column=j, value=name_)
        c.font = F(10, True, color="595959" if j > 6 else None)
        c.alignment = Alignment(horizontal="center", vertical="bottom")
    sh.freeze_panes = "A%d" % (head + 1)
    sh.print_title_rows = "%d:%d" % (head, head)
    formulas = []
    first = r = head + 1
    sub_value = 0.0
    for l, w, a in t["rows"]:
        q = _f(l.get("quantity"))
        unit = _opcc_unit(l, w, a)
        desc = _txt(l.get("description"))
        note = _txt(w.get("note"))      # a working note, beside the source
        src = _txt(price_source(w), 2000)
        if unit is None and a is None:
            src = "No price yet" + (" (%s)" % src if src else "")
        cells = [(1, _txt(l.get("item_no"), 20), "center"), (2, desc, "left"),
                 (3, _txt(l.get("unit"), 12), "center"), (4, q, "right"),
                 (5, unit, "right"), (6, None, "right"),
                 (7, src, "left"), (8, note, "left")]
        for j, v, h in cells:
            c = sh.cell(row=r, column=j, value=v if v != "" else None)
            c.font = F(10) if j <= 6 else F(9, color="595959")
            c.alignment = Alignment(horizontal=h, vertical="top", wrap_text=(j == 2))
        n = text_lines(desc, widths["B"], 10)
        if n > 1:                    # a long description wraps; the row is sized for it
            sh.row_dimensions[r].height = 12.75 * n + 2
        # full precision in the cell (the format shows whole dollars, or
        # cents when there are any), so Excel's totals equal the page's
        if unit is not None and q is not None:
            formulas.append(("F%d" % r, "=D%d*E%d" % (r, r), q * unit))
            sub_value += q * unit
        elif a is not None:
            sh.cell(row=r, column=6, value=a)
            sub_value += a
        sh.cell(row=r, column=4).number_format = "#,##0" if (q is None or float(q).is_integer()) else "#,##0.00"
        cents = isinstance(unit, float) and abs(unit - round(unit)) > 0.004
        sh.cell(row=r, column=5).number_format = '"$"#,##0.00' if cents else '"$"#,##0'
        sh.cell(row=r, column=6).number_format = '"$"#,##0'
        if unit is None and a is None:          # printed as such, never read as $0
            c = sh.cell(row=r, column=5, value="No price yet")
            c.font = Font(name="Arial", size=9, italic=True, color="595959")
            c.alignment = Alignment(horizontal="right", vertical="top")
        r += 1
    last = r - 1
    sub = r + 1

    def total_row(row, label, strong=False, size=10):
        lc = sh.cell(row=row, column=2, value=label)
        lc.font = F(size, strong)
        fc = sh.cell(row=row, column=6)
        fc.font = F(size, strong)
        fc.number_format = '"$"#,##0'
        fc.alignment = Alignment(horizontal="right")

    total_row(sub, "Sub-Total", True)
    formulas.append(("F%d" % sub, ("=SUM(F%d:F%d)" % (first, last))
                     if last >= first else "=0", sub_value))
    conts, total_value, r = [], sub_value, sub + 1
    for l, w, a in t["below"]:
        pct = share_pct(l)
        total_row(r, _below_label(l))
        c = sh.cell(row=r, column=1, value=_txt(l.get("item_no"), 20))
        c.font = F(10)
        c.alignment = Alignment(horizontal="center")
        if pct is not None:
            v = sub_value * pct / 100.0
            formulas.append(("F%d" % r, "=F%d*%r" % (sub, pct / 100.0), v))
            total_value += v
        elif a is not None:
            sh.cell(row=r, column=6, value=a)
            total_value += a
        note = _txt(w.get("note"))
        if note:
            sh.cell(row=r, column=8, value=note).font = F(9, color="595959")
        conts.append(r)
        r += 1
    if show_extra(t):
        total_row(r, _extra_label(t))
        pct = t["contingency_pct"] or 0.0
        v = sub_value * pct / 100.0
        formulas.append(("F%d" % r, "=F%d*%r" % (sub, pct / 100.0), v))
        total_value += v
        conts.append(r)
        r += 1
    tot = r + 1
    total_row(tot, "Project Total", True, 11)
    formulas.append(("F%d" % tot, "=" + "+".join("F%d" % k for k in [sub] + conts),
                     total_value))
    # no lines drawn inside the table or under the totals: the printout has
    # none, and the gridlines show the cells on screen
    # the interim-review box at the bottom right, from Units to Total Cost
    # (C-F), laid out line for line as on the team's printout (user,
    # 2026-11-21): left-aligned, the lines close together, about twice as
    # wide as it is tall -- the seal goes there when the OPCC is submitted
    box = tot + 2
    lines = INTERIM_LINES
    for k, text in enumerate(lines):
        r = box + k
        sh.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
        c = sh.cell(row=r, column=3, value=text)
        c.font = F(11, True) if k == 0 else F(10)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        sh.row_dimensions[r].height = 18 if k == 0 else 16
        for j in range(3, 7):
            sides = {}
            if k == 0:
                sides["top"] = med
            if k == len(lines) - 1:
                sides["bottom"] = med
            if j == 3:
                sides["left"] = med
            if j == 6:
                sides["right"] = med
            if sides:
                _keep_border(sh.cell(row=r, column=j), **sides)
    end = box + len(lines) - 1            # the box closes the frame's corner
    # one heavy frame around the printed block (A-F), as on the team's OPCC
    # (user, 2026-11-21); Excel, Google Sheets and print draw it as a frame
    for row in range(1, end + 1):
        _keep_border(sh.cell(row=row, column=1), left=med)
        _keep_border(sh.cell(row=row, column=6), right=med)
    for col in range(1, 7):
        _keep_border(sh.cell(row=1, column=col), top=med)
        _keep_border(sh.cell(row=end, column=col), bottom=med)
    sh.print_area = "A1:F%d" % end
    sh.page_setup.paperSize = sh.PAPERSIZE_LETTER     # US Letter, as printed (user)
    sh.page_setup.orientation = "portrait"
    sh.page_setup.fitToWidth = 1
    sh.page_setup.fitToHeight = 0
    sh.sheet_properties.pageSetUpPr.fitToPage = True
    sh.print_options.gridLines = False
    sh.page_margins.left = sh.page_margins.right = 0.5
    sh.oddFooter.center.text = "Page &P of &N"
    return formulas


def _arial(sh, first_row: int = 1):
    """Arial on every cell with a value, keeping bold: Calibri, the default,
    is missing on a Mac without Office, where previews fall back to a serif."""
    from openpyxl.styles import Font
    for row in sh.iter_rows(min_row=first_row):
        for c in row:
            if c.value is not None:
                c.font = Font(name="Arial", size=10, bold=c.font.b)
    sh.cell(row=1, column=1).font = Font(name="Arial", size=12, bold=True)


def _summary_page(sh, result: dict, ws: dict, t: dict):
    """The Summary sheet: one page in the OPCC sheet's style (user,
    2026-11-21: a plain list read as unusable in Google Sheets) -- the
    project, your OPCC (Sub-Total, contingency, Project Total) and how it
    compares with the model's P50 / P80. Arial, no gridlines, dollars, one
    frame. The CSV keeps the plain list (sections())."""
    from openpyxl.styles import Alignment, Border, Font, Side
    def F(size=10, bold=False, color=None, italic=False):
        return Font(name="Arial", size=size, bold=bold, color=color, italic=italic)
    med = Side(style="medium", color="000000")
    thin = Side(style="thin", color="000000")
    hair = Side(style="thin", color="D9D9D9")
    sh.sheet_view.showGridLines = False
    widths = {"A": 46, "B": 17, "C": 17, "D": 11}
    for col, wd in widths.items():
        sh.column_dimensions[col].width = wd
    inp = result.get("inputs") or {}
    sh.merge_cells("A1:D1")
    sh["A1"] = "OPCC SUMMARY"
    sh["A1"].font = F(14, True)
    sh["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sh.row_dimensions[1].height = 24
    for col in range(1, 5):
        _keep_border(sh.cell(row=1, column=col), bottom=med)
    r = 3

    def kv(label, value, fmt=None):
        nonlocal r
        sh.cell(row=r, column=1, value=label).font = F(10, True)
        sh.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        c = sh.cell(row=r, column=2, value=value if value not in ("", None) else None)
        c.font = F(10)
        c.alignment = Alignment(horizontal="left")
        if fmt:
            c.number_format = fmt
        r += 1

    kv_first = r
    kv("Project", _txt(ws.get("project_name"), 160))
    kv("Owner", inp.get("owner_label") or "")
    kv("Work type", inp.get("work_type_label") or "")
    kv("Market", inp.get("market_label") or inp.get("market") or "")
    kv("Bid date", inp.get("bid_date") or "")
    kv("Prepared", _today(ws), "mm/dd/yyyy")
    kv("Prepared by", _txt(ws.get("prepared_by"), 60))
    kv("Project no.", _txt(ws.get("project_no"), 60))
    rec = t["recommended_basis"]
    kv("Prices start from", price_basis_label(result, ws))
    blocks = [(kv_first, r - 1)]
    r += 1

    def head(text, cols=None):
        nonlocal r
        for j in range(1, 5):
            c = sh.cell(row=r, column=j, value=(cols or {}).get(j))
            c.font = F(10, True)
            c.border = Border(bottom=thin)
            if j > 1:
                c.alignment = Alignment(horizontal="right")
        sh.cell(row=r, column=1, value=text)
        r += 1

    money = '"$"#,##0'
    signed = '+"$"#,##0;-"$"#,##0;"$"0'

    def row(label, v, bold=False, indent=0, total=False, vs=None):
        nonlocal r
        a = sh.cell(row=r, column=1, value=label)
        a.font = F(10, bold)
        a.alignment = Alignment(indent=indent)
        b = sh.cell(row=r, column=2, value=v)
        b.font = F(10, bold)
        b.number_format = money
        b.alignment = Alignment(horizontal="right")
        if vs:
            c = sh.cell(row=r, column=3, value=vs["amount"])
            c.font = F(10)
            c.number_format = signed
            d = sh.cell(row=r, column=4, value=vs["ratio"])
            d.font = F(10)
            d.number_format = "+0.0%;-0.0%;0.0%"
        for j in range(1, 5):
            _keep_border(sh.cell(row=r, column=j), bottom=hair)
        if total:
            _keep_border(b, top=thin, bottom=Side(style="double", color="000000"))
        r += 1

    def note(text, italic=False):
        nonlocal r
        sh.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        c = sh.cell(row=r, column=1, value=text)
        c.font = F(9, color="595959", italic=italic)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        n = text_lines(text, sum(widths.values()), 9)
        if n > 1:
            sh.row_dimensions[r].height = 11.5 * n + 3
        r += 1

    head("YOUR OPCC")
    b1 = r - 1
    row("Sub-Total (your line amounts)", t["subtotal"], bold=True)
    if t["owner_set"]:
        row("of which owner-set allowances", t["owner_set"], indent=2)
    if t["quotes"]:
        row("of which vendor quotes", t["quotes"], indent=2)
    for l, w, a in t["below"]:
        row(_below_label(l), a)
    if show_extra(t):
        row(_extra_label(t), t["contingency"] or 0.0)
    row("Project Total", t["your_opcc"], bold=True, total=True)
    total_row = r - 1
    blocks.append((b1, total_row))
    if t["missing"]:
        n = len(t["missing"])
        note("%d line%s no price of yours and %s not in this total."
             % (n, " has" if n == 1 else "s have", "is" if n == 1 else "are"))
    r += 1
    head("COMPARED WITH THE MODEL", {2: "Amount", 3: "Your OPCC vs it", 4: ""})
    b2 = r - 1
    nc = " + not-covered lines" if t["not_covered"] else ""
    row("Expected low bid (P50)" + nc, t["model_p50"], vs=t["vs_p50"])
    row("Recommended OPCC (%s)" % rec.upper() + nc,
        t["model_" + rec], vs=t["vs_" + rec])
    if rec == "p70":
        row("P80 baseline" + nc, t["model_p80"], vs=t["vs_p80"])
    blocks.append((b2, r - 1))
    if t["not_covered"]:
        note("The model figures leave the not-covered lines out; your amounts for them "
             "(%s) are added to each here." % ("$" + format(round(t["not_covered"]), ",")))
    r += 1
    head("NOTES")
    if ws.get("basis") == "history":
        note(HISTORY_TEXT)
    note("P50 is the expected low bid: as likely to come in under it as over. "
         + ("P70 is " + P70_TEXT + "." if rec == "p70" else
            "P80 is the cautious figure: on past jobs like this the low bid came in "
            "at or under it about 4 times in 5."))
    note("Your prices and notes are output only: they never feed back into the "
         "estimator, its tables, training or calibration.", italic=True)
    end = r
    for a, b in blocks:
        _grid(sh, a, b, 1, 4)
    _keep_border(sh.cell(row=total_row, column=2), bottom=Side(style="double", color="000000"))
    for rr in range(1, end + 1):          # a thin frame, as on the OPCC
        _keep_border(sh.cell(row=rr, column=1), left=thin)
        _keep_border(sh.cell(row=rr, column=4), right=thin)
    for col in range(1, 5):
        _keep_border(sh.cell(row=1, column=col), top=thin)
        _keep_border(sh.cell(row=end, column=col), bottom=thin)
    sh.print_area = "A1:D%d" % end
    sh.page_setup.paperSize = sh.PAPERSIZE_LETTER
    sh.page_setup.orientation = "portrait"
    sh.page_setup.fitToWidth = 1
    sh.page_setup.fitToHeight = 0
    sh.sheet_properties.pageSetUpPr.fitToPage = True


def _tidy_sheet(sh, title_row=1, header_row=2):
    """The other sheets of the export (Lines, References): a bold title, a
    gray header that stays on screen with the item and description, widths
    that fit, long text wrapped with its row sized for it, every cell
    top-aligned so a wrapped row reads across, money in dollars and
    quantities as numbers, Arial throughout."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    thin = Side(style="thin", color="BFBFBF")
    _arial(sh)
    has_head = header_row and sh.max_row >= header_row and any(
        c.value not in (None, "") for c in sh[header_row])
    heads = {}
    if has_head:
        for c in sh[header_row]:
            if c.value in (None, ""):
                continue
            heads[c.column] = str(c.value).lower()
            c.font = Font(name="Arial", size=10, bold=True)
            c.fill = PatternFill("solid", fgColor="D9D9D9")
            c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sh.freeze_panes = "%s%d" % ("C" if heads.get(2) == "description" else "A",
                                    header_row + 1)
    first = (header_row or title_row) + 1
    widths = {}
    for col in range(1, sh.max_column + 1):
        letter = get_column_letter(col)
        cells = [sh.cell(row=r, column=col) for r in range(first, sh.max_row + 1)]
        longest = max((len(str(c.value)) for c in cells if c.value is not None), default=8)
        head_len = len(heads.get(col, ""))
        width = min(46, max(9, longest + 2, min(head_len, 18) + 2))
        sh.column_dimensions[letter].width = width
        widths[col] = width
        h = heads.get(col, "")
        mid = h in ("item", "unit", "rank", "references", "letting date", "their unit", "bids")
        for c in cells:
            v = c.value
            wrap = False
            if isinstance(v, bool):
                pass
            elif isinstance(v, (int, float)):
                if h == "inflation factor":
                    c.number_format = "0.000000"
                elif "amount" in h:
                    c.number_format = '"$"#,##0'
                elif "$" in h:
                    c.number_format = '"$"#,##0.00'
                elif isinstance(v, float) and not v.is_integer():
                    c.number_format = "#,##0.00"
                else:
                    c.number_format = "#,##0"
            elif isinstance(v, str):
                # words wrap; a link or file name (no spaces) stays on one line
                wrap = len(v) > width and " " in v.strip()
            c.alignment = Alignment(wrap_text=wrap, vertical="top",
                                    horizontal="center" if mid else None)
    if has_head:
        sh.row_dimensions[header_row].height = 12.75 * max(
            text_lines(sh.cell(row=header_row, column=k).value, widths[k], 10)
            for k in heads) + 4
    for r in range(first, sh.max_row + 1):
        n = max((text_lines(c.value, widths[c.column], 10) for c in sh[r]
                 if isinstance(c.value, str) and c.alignment.wrap_text), default=1)
        if n > 1:
            sh.row_dimensions[r].height = 12.75 * n + 2


def _group_rule(sh, first_row: int):
    """A rule above each line's first reference, so the projects behind one
    line read as a group."""
    from openpyxl.styles import Side
    rule = Side(style="thin", color="7F7F7F")
    prev = object()
    for r in range(first_row, sh.max_row + 1):
        key = (sh.cell(row=r, column=1).value, sh.cell(row=r, column=2).value)
        if key != prev and r > first_row:
            for c in sh[r]:
                _keep_border(c, top=rule)
        prev = key


def _cache_values(xlsx: bytes, sheet_path: str, values: dict) -> bytes:
    """openpyxl saves a formula with no result, which a viewer that does not
    calculate (Quick Look, phone and mail previews) shows as $0. Put in each
    result, as Excel itself saves it; Excel still recalculates on open
    (the workbook asks for a full calculation on load)."""
    import io
    import zipfile
    pat = re.compile(r'(<c r="([A-Z]+[0-9]+)"[^>]*><f>[^<]*</f>)(?:<v\s*/>|<v></v>)')

    def fill(m):
        v = values.get(m.group(2))
        return m.group(0) if v is None else "%s<v>%r</v>" % (m.group(1), float(v))
    zin = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == sheet_path:
                data = pat.sub(fill, data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)
    return out.getvalue()


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
        ["Prepared", _today(ws).isoformat()],
        ["Prices start from", price_basis_label(result, ws)],
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
    trial = t["recommended_basis"] == "p70"
    if trial:
        head_rows.append(["Model P70 (recommended OPCC, trial)", _m(t["p70"])])
        s70, c70 = t["p70_split"]
        if s70 is not None:
            head_rows += [["  P70 subtotal", _m(s70)], ["  P70 contingency", _m(c70)]]
        head_rows.append(["P70 note", P70_TEXT])
    head_rows.append(["Model P80" + (" (baseline)" if trial else ""), _m(t["p80"])])
    s80, c80 = t["p80_split"]
    if s80 is not None:
        head_rows += [["  P80 subtotal", _m(s80)], ["  P80 contingency", _m(c80)]]
    nc = t["not_covered"]
    if nc:
        head_rows += [
            ["  your amounts for the not-covered lines (outside the model figures)",
             _m(nc)],
            ["Model P50 + not-covered lines", _m(t["model_p50"])],
        ]
        if trial:
            head_rows.append(["Model P70 + not-covered lines", _m(t["model_p70"])])
        head_rows.append(["Model P80 + not-covered lines"
                          + (" (baseline)" if trial else ""), _m(t["model_p80"])])
    vs = " + not-covered lines" if nc else ""
    head_rows += [
        ["Your OPCC vs P50" + vs, _m((t["vs_p50"] or {}).get("amount")),
         "" if not t["vs_p50"] else "%+.1f%%" % (100 * t["vs_p50"]["ratio"])],
    ]
    if trial:
        head_rows.append(["Your OPCC vs P70" + vs, _m((t["vs_p70"] or {}).get("amount")),
                          "" if not t["vs_p70"] else "%+.1f%%" % (100 * t["vs_p70"]["ratio"])])
    head_rows += [
        ["Your OPCC vs P80" + vs + (" (baseline)" if trial else ""),
         _m((t["vs_p80"] or {}).get("amount")),
         "" if not t["vs_p80"] else "%+.1f%%" % (100 * t["vs_p80"]["ratio"])],
        ["Lines without your price", len(t["missing"])],
        [],
        ["Note", "Your prices and notes are output only; they never feed back "
                 "into the estimator, its tables, training or calibration."],
    ]
    if ws.get("basis") == "history":
        head_rows.append(["Historical starting prices", HISTORY_TEXT])
    out.append(("OPCC worksheet", None, head_rows))
    rmap = {x["row_id"]: x for x in ((refs or {}).get("lines") or [])}
    history_columns = (ws.get("basis") == "history"
                       or any(w.get("history_price") for l, w, a in t["rows"]))
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
                     (_f(w.get("your_unit")) if w.get("src") == "history"
                      else _m(w.get("your_unit"))) if l.get("status") != "owner_set"
                     else "", _m(a), price_source(w),
                     w.get("note") or "", "; ".join(w.get("judgment") or [])])
        if history_columns:
            history = w.get("history_price") or {}
            rows[-1] += [_f(history.get("unit_price")),
                         "; ".join(history.get("evidence") or [])
                         or history.get("reason") or ""]
    # below the items: SUBTOTAL, the contingency, TOTAL (your amounts and
    # the model's P50 side by side)
    blank = [""] * (18 if history_columns else 16)

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
    if show_extra(t):
        rows.append(foot("%s (%g%% of the subtotal)" % (
            "Additional construction contingency" if t["below"]
            else "Construction contingency", t["contingency_pct"] or 0), None,
            t["contingency"] or 0.0, "yours only: not in the model figures"))
    rows.append(foot("PROJECT TOTAL", t["p50"], t["your_opcc"]))
    headers = ["Item", "Description", "Qty", "Unit", "Model $/unit (P50)",
               "Model amount (P50)", "Reference low $/unit",
               "Reference median $/unit", "Reference high $/unit",
               "References", "Model P50 historical accuracy", "Your $/unit",
               "Your amount", "Your price from", "Note", "Needs your judgment"]
    if history_columns:
        headers += ["Historical starting $/unit", "History evidence"]
    out.append(("OPCC lines", headers, rows))
    # each line's reference projects: the project, and its own item as it
    # was bid -- the words, unit, quantity and prices (user, 2026-11-21)
    rows = []
    for l, w, a in t["rows"]:
        x = rmap.get(l.get("row_id")) or {}
        for k, (pk, use) in enumerate(_export_references(w, x), 1):
            src = pk.get("source") or {}
            q = _f(pk.get("quantity"))
            index = pk.get("index_evidence") or {}
            # An unavailable history adjustment remains blank, never replaced
            # by the older model reference's adjustment or its original price.
            adjusted = (pk.get("adjusted_median") if "adjusted_median" in pk
                        else pk.get("median_esc"))
            target_year = index.get("target_year") or (refs or {}).get("to_year")
            rows.append([l.get("item_no"), l.get("description"), k,
                         pk.get("project"), pk.get("owner"), pk.get("date"),
                         pk.get("item") or "", pk.get("unit") or "",
                         q if q is not None else "",
                         _f(pk.get("low_unit")), _f(pk.get("mean_unit")),
                         _f(pk.get("median_unit")), _f(adjusted),
                         pk.get("n_bids") or "", use, pk.get("item_no") or "",
                         target_year or "", _f(pk.get("escalation")),
                         _index_text(index), "; ".join(pk.get("match_basis") or []),
                         pk.get("key") or "", src.get("document") or "",
                         src.get("url") or "", src.get("mirror_url") or ""])
    out.append(("References appendix",
                ["Item", "Description", "Rank", "Project", "Owner",
                 "Letting date", "Their item (as bid)", "Their unit",
                 "Their quantity", "Low bid $/unit", "Average bid $/unit",
                 "Median bid $/unit", "Inflation-adjusted median $/unit",
                 "Bids", "Reference use", "Their item no.", "Adjusted price year",
                 "Inflation factor", "Index evidence", "Match evidence",
                 "Reference key", "Source document", "source_url", "mirror_url"],
                rows or [["(none)"]]))
    return out


def to_csv(result, ws, refs) -> bytes:
    import csv
    import io
    from exporter import safe_row      # text is never run as a formula
    result, ws, refs = _export_text((result, ws, refs))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# OPCC worksheet -- the estimator's prices next to the "
                "model's P50 / %s" % recommended_basis(result).upper()])
    for title, head, rows in sections(result, ws, refs):
        w.writerow([])
        w.writerow(["## " + title])
        if head:
            w.writerow(safe_row(head))
        for r in rows:
            w.writerow(safe_row(r))
    return ("﻿" + buf.getvalue()).encode("utf-8")


_PDF_SUBS = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"',
             "”": '"', "…": "...", "•": "-", "≤": "<=", "≥": ">=",
             "′": "'", "″": '"', " ": " "}


def _latin1(v) -> str:
    """The PDF's core font is Latin-1: common punctuation is mapped, the rest
    replaced, so no description can stop the export."""
    s = "" if v is None else str(v)
    for a, b in _PDF_SUBS.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _usd(v, cents=False) -> str:
    if v is None:
        return ""
    return ("$" + format(v, ",.2f")) if cents else ("$" + format(round(v), ","))


def to_pdf(result, ws, refs=None) -> bytes:
    """The OPCC as printed: the Excel export's first sheet, as a PDF (user,
    2026-11-21: "the Excel's printed first page"). The title and project
    block, ESTIMATED QUANTITIES as a gray band, the items, Sub-Total, the
    contingency, Project Total, and the blank interim-review box at the
    bottom right; a heavy frame on every page, the header row repeated,
    page numbers. Letter, portrait. As on the team's printout (user,
    2026-11-21: "this feel", without a logo or project specifics): the title
    and project block in a bold serif, no lines inside the table. The
    working notes (unit cost source, notes) stay out, as they stay outside
    the Excel print area."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    result, ws, refs = _export_text((result, ws, refs))
    t = totals(result, ws)

    class _P(FPDF):
        frame_bottom = None              # set on the last page: the box's bottom

        def footer(self):
            # the frame: down to the margin, or on the last page round the
            # form only, closing under the interim-review box
            bottom = self.frame_bottom or (self.h - self.b_margin)
            self.set_line_width(1.6)
            self.set_draw_color(0, 0, 0)
            self.rect(self.l_margin, self.t_margin, self.epw, bottom - self.t_margin)
            self.set_line_width(0.5)
            self.set_y(-self.b_margin + 6)
            self.set_font("helvetica", size=8)
            self.cell(0, 10, "Page %d of {nb}" % self.page_no(), align="C")

    pdf = _P(orientation="P", unit="pt", format="Letter")
    pdf.set_margins(36, 36, 36)
    pdf.set_auto_page_break(True, margin=46)
    pdf.alias_nb_pages()
    pdf.add_page()
    W, x0 = pdf.epw, pdf.l_margin
    # title, with a heavy rule under it
    pdf.set_font("times", "B", 14)
    pdf.set_xy(x0, pdf.t_margin + 4)
    pdf.cell(W, 22, OPCC_TITLE, align="C")
    y = pdf.t_margin + 30
    pdf.set_line_width(1.6)
    pdf.line(x0, y, x0 + W, y)
    pdf.set_line_width(0.5)
    # the project block: the name on the left, date / prepared by / no. right
    y += 10
    pdf.set_font("times", "B", 16)
    pdf.set_xy(x0 + 6, y)
    pdf.multi_cell(W * 0.56, 19, _latin1(_txt(ws.get("project_name"), 160)) or " ", align="C",
                   max_line_height=19)
    labels = (("DATE:", _today(ws).strftime("%m/%d/%Y")),
              ("PREPARED BY:", _txt(ws.get("prepared_by"), 60)),
              ("PROJECT NO.:", _txt(ws.get("project_no"), 60)))
    for k, (lab, val) in enumerate(labels):
        pdf.set_xy(x0 + W * 0.60, y + k * 14)
        pdf.set_font("times", "B", 10)
        pdf.cell(W * 0.18, 14, lab)
        pdf.set_font("helvetica", size=9)
        pdf.cell(W * 0.22 - 6, 14, _latin1(val), align="R")
    y = max(pdf.get_y(), y + 3 * 14) + 10
    # ESTIMATED QUANTITIES, a gray band
    pdf.set_xy(x0, y)
    pdf.set_fill_color(191, 191, 191)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(W, 15, "  ESTIMATED QUANTITIES", fill=True)
    pdf.set_fill_color(255, 255, 255)        # the table's cells stay white
    pdf.set_y(y + 15)
    # the items and the totals: the OPCC's six columns
    num = (38, 58, 62, 66)                   # Units, Quantity, Unit Cost, Total Cost
    cw = (38, W - 38 - sum(num)) + num
    bold = FontFace(emphasis="BOLD")
    pdf.set_font("helvetica", size=8.5)
    with pdf.table(col_widths=cw, width=W, align="LEFT", line_height=10.5,
                   text_align=("CENTER", "LEFT", "CENTER", "RIGHT", "RIGHT", "RIGHT"),
                   headings_style=FontFace(emphasis="BOLD", fill_color=(255, 255, 255)),
                   borders_layout="NONE", padding=(2, 7, 2, 7), repeat_headings=1,
                   num_heading_rows=1) as table:
        table.row([_latin1(h) for h in OPCC_HEADER[:6]])
        for l, w, a in t["rows"]:
            q = _f(l.get("quantity"))
            unit = _opcc_unit(l, w, a)
            cents = isinstance(unit, float) and abs(unit - round(unit)) > 0.004
            qtxt = "" if q is None else (format(q, ",.0f") if float(q).is_integer() else format(q, ",.2f"))
            table.row([_latin1(_txt(l.get("item_no"), 20)), _latin1(_txt(l.get("description"))),
                       _latin1(_txt(l.get("unit"), 12)), qtxt,
                       _usd(unit, cents) if unit is not None else ("" if a is not None else "No price yet"),
                       _usd(a)])
        table.row(["", "", "", "", "", ""])
        r = table.row()
        for v, st in (("", None), ("Sub-Total", bold), ("", None), ("", None), ("", None),
                      (_usd(t["subtotal"]), bold)):
            r.cell(v, style=st)
        for l, w, a in t["below"]:
            table.row([_latin1(_txt(l.get("item_no"), 20)), _latin1(_below_label(l)), "", "", "", _usd(a)])
        if show_extra(t):
            table.row(["", _latin1(_extra_label(t)), "", "", "", _usd(t["contingency"] or 0.0)])
        table.row(["", "", "", "", "", ""])
        r = table.row()
        for v, st in (("", None), ("Project Total", bold), ("", None), ("", None), ("", None),
                      (_usd(t["your_opcc"]), bold)):
            r.cell(v, style=st)
    # the interim-review box at the bottom right, in a heavy line: from the
    # Units column to Total Cost, the lines left-aligned and close together,
    # as on the team's printout (user, 2026-11-21); the seal goes there
    bw = sum(num)
    bh = 4 + 14 + 12.5 * (len(INTERIM_LINES) - 1) + 8
    if pdf.get_y() + 14 + bh > pdf.h - pdf.b_margin:
        pdf.add_page()
    bx, by = x0 + W - bw, pdf.get_y() + 14
    pdf.set_line_width(1.6)
    pdf.rect(bx, by, bw, bh)
    pdf.set_line_width(0.5)
    y = by + 4
    for k, text in enumerate(INTERIM_LINES):
        pdf.set_font("helvetica", "B" if k == 0 else "", 10 if k == 0 else 9)
        pdf.set_xy(bx + 5, y)
        pdf.cell(bw - 10, 14 if k == 0 else 12.5, text)
        y += 14 if k == 0 else 12.5
    pdf.frame_bottom = by + bh
    return bytes(pdf.output())


def to_xlsx(result, ws, refs) -> bytes:
    import io
    import openpyxl
    from openpyxl.styles import Font
    result, ws, refs = _export_text((result, ws, refs))
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
        if title == "OPCC worksheet":
            _summary_page(sh, result, ws, totals(result, ws))
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
        if title == "References appendix":
            _group_rule(sh, 3)
    from exporter import xlsx_text_only    # "=..." notes stay text
    xlsx_text_only(wb)
    # then the OPCC's own totals, as live formulas (written by this module,
    # never taken from the browser), with their results cached for viewers
    # that do not calculate
    for ref, f, v in formulas:
        wb["OPCC"][ref] = f
    for sh in wb.worksheets:               # every sheet prints on US Letter (user)
        sh.page_setup.paperSize = sh.PAPERSIZE_LETTER
    buf = io.BytesIO()
    wb.save(buf)
    path = "xl/worksheets/sheet%d.xml" % (wb.sheetnames.index("OPCC") + 1)
    return _cache_values(buf.getvalue(), path, {ref: v for ref, f, v in formulas})
