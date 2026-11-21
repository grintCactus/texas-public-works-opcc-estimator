"""Scenario A: a new project's bill of quantities / takeoff -> line items.

A takeoff is NOT a bid tabulation. It has description / unit / quantity and
usually no prices, the columns are in whatever order the estimator's workbook
uses, and the header may be absent, merged, or three rows tall. So the columns
are DETECTED here rather than assumed, and every extraction carries a
confidence and a list of issues.

Nothing here is trusted. The output goes to the confirmation grid for the
estimator to fix before anything is priced -- automatic extraction from a
scanned takeoff is wrong often enough that silently pricing it would be the
worst possible behaviour.

Supported inputs: .xlsx/.xlsm, .csv, .pdf (text layer), .docx, pasted text,
and images via ui/ingest/ocr.py once an OCR engine is available.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import upstream
from . import ocr as ocr_mod

# ---------------------------------------------------------------------------
# Header vocabulary
# ---------------------------------------------------------------------------

# "Description" and friends: unambiguous description headers.
# Header roles resolve in three tiers, because the word "item" appears in the
# spelling of BOTH the description column and the item-NUMBER column, and a
# first-match-wins rule gets it wrong on about half of real layouts. Getting it
# backwards takes the description from the number column, drops every row as
# "description too short", and returns an EMPTY takeoff with no error at all.
#
#   strong -- an unambiguous description word. Wins from any column position.
#   number -- explicitly a numbering column ("Bid Item No.", "Line #").
#   weak   -- ambiguous ("Item", "Bid Items", "Work"): the description only
#             when the row has no strong column, otherwise the item number.
#
# Strong is tested before number, so a combined "ITEM NO. & DESCRIPTION"
# column stays the description.
DESC_HEADERS = re.compile(
    r"\b(descriptions?|item\s*descr|descr|item\s*name|"
    r"(scope|nature|description)\s*of\s*work)\b", re.I)
#: Explicitly a numbering column: optional prefix, "item", then a number word.
#: Never the description, whatever else the row contains.
ITEMNO_STRICT = re.compile(
    r"\b((bid|pay|work|line|spec|contract)\s*)?items?\s*"
    r"(no|nos|num|nums|number|numbers|#|id)\b"
    r"|^\s*(no|nos|num|number|#|line|sl\.?\s*no|ref)\s*\.?\s*$", re.I)
#: Ambiguous, PREFIXED: "Bid Items", "Pay Item". `BID ITEMS | UNIT | QTY` is a
#: real layout in which this IS the description and nothing else is a
#: candidate, so it cannot just be discarded. Preferred over a bare "Item",
#: whose prefix is doing no descriptive work.
DESC_HEADERS_WEAK2 = re.compile(
    r"^\s*(bid|pay|work|contract|spec|line)\s*items?\s*$", re.I)
#: Ambiguous, BARE: in `Item | Description | Unit | Qty` this is the item
#: number; in `Item | Unit | Qty` it is the description.
DESC_HEADERS_WEAK = re.compile(r"^\s*(item|items|work|scope)\s*$", re.I)
UNIT_HEADERS = re.compile(
    r"\b(unit|uom|u/m|u\.o\.m|units|unit\s*of\s*measure|meas|meas\.|measure|"
    r"measurement|um)\b", re.I)
QTY_HEADERS = re.compile(
    r"\b(quantity|qty|qnty|est\.?\s*qty|estimated\s*quantity|bid\s*qty|"
    r"plan\s*qty|approx\.?\s*qty|count)\b", re.I)
#: Columns that must NOT be read as the quantity.
PRICE_HEADERS = re.compile(
    r"\b(unit\s*price|unit\s*cost|price|cost|amount|total|extended|extension|"
    r"bid\s*total|subtotal)\b", re.I)
ITEMNO_HEADERS = re.compile(r"\b(item\s*(no|#|num)|no\.?$|#$|line|sl\.?\s*no)\b", re.I)
#: Of the price-like columns: the line's money (qty x unit price) ...
AMOUNT_HEADERS = re.compile(
    r"\b(amount|total|extended|extension|ext\.?\s*(amount|price|cost))\b", re.I)
#: ... and its unit price. Read only for owner-set allowance lines.
UNIT_PRICE_HEADERS = re.compile(
    r"\bunit\s*(price|cost|rate)\b|^\s*(price|rate)\s*$", re.I)

#: Units we recognise. Anything else is kept verbatim and flagged.
KNOWN_UNITS = {
    "LS", "LF", "SY", "SF", "CY", "CF", "EA", "MO", "DAY", "HR", "TON", "LB",
    "GAL", "AC", "STA", "ALLOW", "ALW", "LOT", "VF", "HD", "CYC", "MI", "TGL",
    "MOS", "WEEK", "WK", "DAYS", "SET", "SETS", "TEST", "TESTS", "PAIR",
}

# Rows that are totals or headings rather than pay items. These matter: a
# "TOTAL BASE BID" row carries a project total in the price column, and
# letting one through as a pay item double-counts the whole job.
#
# Matching on "starts with total" is wrong -- "Total Station Survey Equipment"
# is a real surveying pay item, and "Bidder Supplied Fill Material" is real
# work. So instead: strip the words a total/heading row is MADE of, and see
# whether anything substantive is left. Nothing left => it is a total row.
# Something left => it is an item (or an ambiguous subtotal such as
# "Total Part A - Paving", which is kept and flagged for the estimator rather
# than dropped, because silently losing a pay item costs money and a spurious
# row is one click to delete).
_TOTAL_WORDS = {
    "total", "totals", "subtotal", "subtotals", "sub", "grand", "sum", "summary",
    "base", "bid", "bids", "bidding", "alt", "alternate", "alternates", "add",
    "deduct", "schedule", "items", "item", "part", "parts", "section",
    "division", "page", "continued", "amount", "words", "of", "the", "for", "in",
    "no", "and", "a", "b", "c", "d", "i", "ii", "iii", "iv", "v", "vi",
    "contract", "proposal", "project", "cost", "price", "prices", "only",
}
_TOTAL_HEAD = re.compile(
    r"\b(total|subtotal|sub\s*total|grand\s*total|summary|schedule\s+of|"
    r"bid\s*schedule|amount\s+in\s+words|continued)\b", re.I)


def _is_not_item(desc: str) -> bool:
    s = _txt(desc)
    if not s:
        return True
    if not _TOTAL_HEAD.search(s) and not re.match(
            r"^\s*(part|section|division|page)\s+[ivx\d]+\b", s, re.I):
        return False
    residue = [t for t in re.split(r"[^a-z0-9]+", s.lower())
               if t and t not in _TOTAL_WORDS and not t.isdigit()]
    return not residue


def _looks_like_subtotal(desc: str) -> bool:
    """Ambiguous: has total/heading wording but also real words."""
    s = _txt(desc)
    return bool(_TOTAL_HEAD.search(s)) and not _is_not_item(s)

NUM_RE = re.compile(r"^\(?-?\$?\s*[\d,]*\.?\d+\s*\)?$")


@dataclass
class TakeoffLine:
    row_id: str
    description: str
    unit: str
    quantity: Optional[float]
    item_no: str = ""
    confidence: str = "ok"          # ok | low
    issues: List[str] = field(default_factory=list)
    raw: str = ""
    #: An amount / total / extended column and a unit-price column, when the
    #: file has them. NEVER a price for estimating: the UI applies them only
    #: to owner-set allowance lines (face-value money nobody bids).
    amount: Optional[float] = None
    unit_price: Optional[float] = None
    #: Who read the line: "" for the local readers, "claude" for a line the
    #: optional Claude reader transcribed (ui/ingest/claude_reader.py) -- the
    #: grid tags those "read by Claude".
    origin: str = ""
    #: The PDF page the line was read from, when the reader knows it.
    page: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TakeoffParse:
    lines: List[TakeoffLine] = field(default_factory=list)
    source: str = ""
    method: str = ""
    notes: List[str] = field(default_factory=list)
    #: True when any line came from a stand-in rather than a real engine.
    mock: bool = False
    #: Rows the parser saw and rejected, so nothing disappears silently.
    skipped: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lines": [l.to_dict() for l in self.lines],
            "source": self.source,
            "method": self.method,
            "notes": self.notes,
            "mock": self.mock,
            "skipped": self.skipped[:80],
            "n_skipped": len(self.skipped),
        }


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _txt(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _num(v) -> Optional[float]:
    s = _txt(v)
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    if not s or not re.fullmatch(r"-?\d*\.?\d+", s):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _looks_numeric(v) -> bool:
    s = _txt(v)
    return bool(s) and bool(NUM_RE.fullmatch(s))


def _unit_score(v) -> bool:
    s = _txt(v)
    if not s or len(s) > 22:
        return False
    u = upstream.norm_unit(s).upper()
    return u in KNOWN_UNITS




# ---------------------------------------------------------------------------
# Grid parsing (xlsx / csv / pdf tables / OCR rows all become a grid)
# ---------------------------------------------------------------------------

def _find_header(grid: Sequence[Sequence[str]]) -> Tuple[int, Dict[str, int]]:
    """Locate the header row and map logical column -> index.

    Scored rather than matched on the first hit, because workbooks routinely
    have a title row that contains the word "Description" and a real header
    three rows further down.
    """
    best_row, best_map, best_score = -1, {}, 0
    for i, row in enumerate(grid[:60]):
        cells = [_txt(c) for c in row]
        if not any(cells):
            continue
        m: Dict[str, int] = {}
        score = 0
        weak_desc = None          # bare "Item"      -- last resort
        weak_desc2 = None         # "Bid Items" etc. -- preferred over bare
        for j, c in enumerate(cells):
            if not c or len(c) > 48:
                continue
            # Price headers are checked FIRST: "Unit Price" matches both the
            # unit and the price pattern, and it is a price column.
            if PRICE_HEADERS.search(c):
                m.setdefault("price", j)
                if UNIT_PRICE_HEADERS.search(c):
                    m.setdefault("unit_price", j)
                elif AMOUNT_HEADERS.search(c):
                    m.setdefault("amount", j)
                continue
            # Then an unambiguous description, from wherever it sits.
            if "desc" not in m and DESC_HEADERS.search(c):
                m["desc"] = j
                score += 3
                continue
            # Then an explicit numbering column, so "Bid Item No." can never
            # be mistaken for the description of the work.
            if "item_no" not in m and ITEMNO_STRICT.search(c):
                m["item_no"] = j
                score += 1
                continue
            if "qty" not in m and QTY_HEADERS.search(c):
                m["qty"] = j
                score += 3
                continue
            if "unit" not in m and UNIT_HEADERS.search(c):
                m["unit"] = j
                score += 2
                continue
            if weak_desc2 is None and DESC_HEADERS_WEAK2.match(c):
                weak_desc2 = j
                continue
            if weak_desc is None and DESC_HEADERS_WEAK.match(c):
                weak_desc = j
                continue
            if "item_no" not in m and ITEMNO_HEADERS.search(c):
                m["item_no"] = j
                score += 1

        # Resolve the ambiguous columns only now, with the whole row seen and
        # a real description column known to be present or absent.
        for weak, pts in ((weak_desc2, 2), (weak_desc, 2)):
            if weak is None:
                continue
            if "desc" not in m:
                m["desc"] = weak
                score += pts
            elif "item_no" not in m:
                m["item_no"] = weak
                score += 1

        if score > best_score and "desc" in m:
            best_row, best_map, best_score = i, m, score
    return best_row, best_map


def _infer_columns(body: Sequence[Sequence[str]]) -> Dict[str, int]:
    """No usable header: infer columns from the content of each one."""
    if not body:
        return {}
    width = max(len(r) for r in body)
    text_score = [0] * width
    unit_score = [0] * width
    num_score = [0] * width
    filled = [0] * width
    for row in body:
        for j in range(width):
            v = _txt(row[j]) if j < len(row) else ""
            if not v:
                continue
            filled[j] += 1
            if _unit_score(v):
                unit_score[j] += 1
            elif _looks_numeric(v):
                num_score[j] += 1
            elif len(v) >= 8 and re.search(r"[a-z]{3}", v, re.I):
                text_score[j] += 1

    cols: Dict[str, int] = {}
    if max(unit_score) >= max(2, 0.3 * max(1, max(filled))):
        cols["unit"] = unit_score.index(max(unit_score))
    if max(text_score) > 0:
        cols["desc"] = text_score.index(max(text_score))
    # The quantity is the numeric column closest to the right of the unit
    # column; failing that, the most-numeric column that is not the item no.
    if max(num_score) > 0:
        cand = [j for j in range(width) if num_score[j] > 0]
        if "unit" in cols:
            right = [j for j in cand if j > cols["unit"]]
            cols["qty"] = (right[0] if right
                           else max(cand, key=lambda j: num_score[j]))
        else:
            cols["qty"] = max(cand, key=lambda j: num_score[j])
    return cols


def _lines_from_grid(grid: Sequence[Sequence[str]], source: str,
                     method: str) -> TakeoffParse:
    out = TakeoffParse(source=source, method=method)
    # Blank rows are dropped for detection but their ORIGINAL positions are
    # kept, so "header found on row 4" means row 4 of the user's sheet. An
    # estimator checking a parse against their own file needs the number to
    # be the one they can see.
    numbered = [(i + 1, list(r)) for i, r in enumerate(grid)
                if any(_txt(c) for c in r)]
    if not numbered:
        out.notes.append("No non-empty rows found.")
        return out
    src_rows = [n for n, _ in numbered]
    grid = [r for _, r in numbered]

    hrow, cmap = _find_header(grid)
    if hrow >= 0 and "desc" in cmap:
        body = grid[hrow + 1:]
        body_rows = src_rows[hrow + 1:]
        out.notes.append(
            "Header found on row %d: %s." % (
                src_rows[hrow],
                ", ".join("%s=col %d" % (k, v + 1)
                          for k, v in sorted(cmap.items(), key=lambda kv: kv[1]))))
        if "qty" not in cmap:
            inferred = _infer_columns(body)
            if "qty" in inferred:
                cmap["qty"] = inferred["qty"]
                out.notes.append("No quantity header; inferred column %d."
                                 % (inferred["qty"] + 1))
        if "unit" not in cmap:
            inferred = _infer_columns(body)
            if "unit" in inferred:
                cmap["unit"] = inferred["unit"]
                out.notes.append("No unit header; inferred column %d."
                                 % (inferred["unit"] + 1))
    else:
        body = grid
        body_rows = src_rows
        cmap = _infer_columns(body)
        if "desc" not in cmap:
            out.notes.append(
                "No header row and no column looks like a description. "
                "Nothing extracted -- paste the items as text instead.")
            return out
        out.notes.append(
            "No header row recognised; columns inferred from content: %s."
            % ", ".join("%s=col %d" % (k, v + 1) for k, v in sorted(
                cmap.items(), key=lambda kv: kv[1])))

    di, ui, qi = cmap.get("desc"), cmap.get("unit"), cmap.get("qty")
    ii = cmap.get("item_no")
    ai, pi = cmap.get("amount"), cmap.get("unit_price")
    if ai is not None or pi is not None:
        out.notes.append(
            "An amount / unit-price column was found. It is read for owner-set "
            "allowance lines only (taken at face value); every other line is "
            "estimated, never read off the file.")
    if ui is None:
        out.notes.append("No unit column -- units left blank for you to fill in.")
    if qi is None:
        out.notes.append(
            "No quantity column found. Every line needs a quantity before it "
            "can be priced.")

    n = 0
    for k, row in enumerate(body):
        src_row = body_rows[k] if k < len(body_rows) else k + 1

        def cell(j):
            return _txt(row[j]) if (j is not None and j < len(row)) else ""
        desc = cell(di)
        unit = cell(ui)
        qty = _num(cell(qi))
        item_no = cell(ii)
        amount = _num(cell(ai)) if ai is not None else None
        unit_price = _num(cell(pi)) if pi is not None else None
        if qty is None or not unit:
            # quantity and unit in one cell: "1,745 SY", "1745SY"
            for j in ([qi] if qi is not None else []) + ([ui] if ui is not None else []):
                q2, u2 = _split_qty_unit(cell(j))
                if q2 is not None:
                    qty = qty if qty is not None else q2
                    unit = unit or u2
                    break

        if not desc:
            # A row with a quantity but no description is a wrapped line or a
            # broken parse; record it instead of dropping it.
            if qty is not None or unit:
                out.skipped.append({"row": src_row, "reason": "no description",
                                    "raw": " | ".join(_txt(c) for c in row)[:200]})
            continue
        if _is_not_item(desc):
            out.skipped.append({"row": src_row, "reason": "total/heading row",
                                "raw": desc[:200]})
            continue
        if len(desc) < 3:
            out.skipped.append({"row": src_row, "reason": "description too short",
                                "raw": desc[:200]})
            continue

        issues: List[str] = []
        if _looks_like_subtotal(desc):
            issues.append("may be a subtotal row, not a pay item -- "
                          "delete it if so")
        if qty is None:
            issues.append("no quantity found")
        if not unit:
            issues.append("no unit found")
        elif not _unit_score(unit):
            issues.append("unrecognised unit %r" % unit)
        n += 1
        out.lines.append(TakeoffLine(
            row_id="r%d" % n,
            description=desc,
            unit=upstream.norm_unit(unit).upper() if unit else "",
            quantity=qty,
            item_no=item_no,
            confidence="low" if issues else "ok",
            issues=issues,
            raw=" | ".join(_txt(c) for c in row)[:300],
            amount=amount if amount and amount > 0 else None,
            unit_price=unit_price if unit_price and unit_price > 0 else None,
        ))
    if not out.lines:
        out.notes.append("Columns were found but no row survived as a pay item.")
    return out


# ---------------------------------------------------------------------------
# Coordinate words (PDF text layer, OCR) -> grid
# ---------------------------------------------------------------------------

def grid_from_words(words: Sequence[dict], row_tol: float = 3.2,
                    col_gap: float = 11.0) -> List[List[str]]:
    """Group positioned words into rows, then split rows into columns on x-gaps.

    Shared by the PDF text layer and OCR, which is the whole reason the OCR
    contract is pdfplumber's word shape.
    """
    ws = [w for w in words if _txt(w.get("text"))]
    if not ws:
        return []
    ws.sort(key=lambda w: (float(w["top"]), float(w["x0"])))

    rows: List[List[dict]] = []
    cur: List[dict] = []
    cur_top = None
    for w in ws:
        t = float(w["top"])
        if cur_top is None or abs(t - cur_top) <= row_tol:
            cur.append(w)
            cur_top = t if cur_top is None else (cur_top * 0.7 + t * 0.3)
        else:
            rows.append(cur)
            cur, cur_top = [w], t
    if cur:
        rows.append(cur)

    grid: List[List[str]] = []
    for row in rows:
        row.sort(key=lambda w: float(w["x0"]))
        cells: List[str] = []
        buf = [row[0]["text"]]
        prev_x1 = float(row[0]["x1"])
        for w in row[1:]:
            if float(w["x0"]) - prev_x1 > col_gap:
                cells.append(" ".join(buf))
                buf = [w["text"]]
            else:
                buf.append(w["text"])
            prev_x1 = float(w["x1"])
        cells.append(" ".join(buf))
        grid.append([_txt(c) for c in cells])
    return grid


# ---------------------------------------------------------------------------
# Pasted text
# ---------------------------------------------------------------------------

def from_text(text: str, source: str = "pasted text") -> TakeoffParse:
    """Parse pasted lines. Delimiter is detected per paste, then per line."""
    raw_lines = [l for l in (text or "").splitlines()]
    nonempty = [l for l in raw_lines if l.strip()]
    if not nonempty:
        return TakeoffParse(source=source, method="text",
                            notes=["Nothing pasted."])

    # Tab- or pipe-separated pastes (straight out of Excel) are a grid.
    tabbed = sum(1 for l in nonempty if "\t" in l)
    piped = sum(1 for l in nonempty if "|" in l)
    if tabbed >= max(2, 0.6 * len(nonempty)):
        grid = [l.split("\t") for l in nonempty]
        p = _lines_from_grid(grid, source, "text/tab-separated")
        p.notes.insert(0, "Detected tab-separated columns.")
        return p
    if piped >= max(2, 0.6 * len(nonempty)):
        grid = [[c for c in l.split("|")] for l in nonempty]
        p = _lines_from_grid(grid, source, "text/pipe-separated")
        p.notes.insert(0, "Detected pipe-separated columns.")
        return p
    if _csv_like(nonempty):
        try:
            grid = list(csv.reader(io.StringIO("\n".join(nonempty))))
            p = _lines_from_grid(grid, source, "text/csv")
            p.notes.insert(0, "Detected comma-separated columns.")
            if p.lines:
                return p
        except csv.Error:
            pass

    # Free-form: one item per line, "desc ... UNIT ... QTY" in some order.
    out = TakeoffParse(source=source, method="text/freeform")
    out.notes.append(
        "No column structure detected; each line parsed as "
        "description + unit + quantity in any order.")
    n = joined = 0
    for k, raw in enumerate(nonempty):
        line = _parse_free_line(raw)
        if line is None:
            out.skipped.append({"row": k + 1, "reason": "no item found",
                                "raw": raw[:200]})
            continue
        if _HEADER_LINE_RE.fullmatch(line.description.lower()) and \
                line.quantity is None:
            out.skipped.append({"row": k + 1, "reason": "column header",
                                "raw": raw[:200]})
            continue
        # a wrapped description belongs to the line above it:
        #   item line (with unit and quantity) + a bare text line, or
        #   item line cut before its unit + the rest with unit and quantity
        prev = out.lines[-1] if out.lines else None
        if prev is not None and not line.item_no:
            # no quantity, and no unit -- or only a unit word inside running
            # text ("pipe, all depths": "all" reads as ALLOW)
            bare = line.quantity is None and (not line.unit
                                              or not _unit_at_end(raw))
            if bare and prev.quantity is not None:
                prev.description = _txt(prev.description + " " + line.description)
                joined += 1
                continue
            if (not bare and prev.quantity is None and not prev.unit
                    and prev.item_no):
                prev.description = _txt(prev.description + " " + line.description)
                prev.unit, prev.quantity = line.unit, line.quantity
                prev.issues = [i for i in line.issues]
                prev.confidence = line.confidence
                joined += 1
                continue
        n += 1
        line.row_id = "r%d" % n
        out.lines.append(line)
    if joined:
        out.notes.append("%d wrapped description line(s) joined to the line "
                         "above." % joined)
    if not out.lines:
        out.notes.append(
            "No line yielded a description. Expected something like "
            "'Remove Existing Riprap   SY   1250' per line.")
    return out


def _unit_at_end(raw: str) -> bool:
    toks = _txt(raw).split()
    return bool(toks) and _unit_score(toks[-1].strip(".,;:"))


def _csv_like(lines: Sequence[str]) -> bool:
    """Comma-separated columns: most lines have 2+ separating commas, and
    most of those the same number. Thousands separators ("1,745") and the
    odd comma in a description ("Block Sod, Bermuda") do not count -- free
    text full of them is not a CSV."""
    def seps(l):
        return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", l).count(",")
    counts = [seps(l) for l in lines]
    multi = [c for c in counts if c >= 2]
    if len(multi) < max(2, 0.6 * len(lines)):
        return False
    modal = max(multi.count(c) for c in set(multi))
    return modal >= 0.6 * len(lines)


def _parse_free_line(raw: str) -> Optional[TakeoffLine]:
    s = _txt(raw)
    if not s or _is_not_item(s):
        return None
    # Split on runs of 2+ spaces first -- that is how a copied column layout
    # survives a paste. Fall back to single spaces.
    parts = [p for p in re.split(r"\s{2,}", s) if p.strip()]
    if len(parts) < 2:
        parts = s.split()
    parts = [_txt(p) for p in parts]

    # quantity and unit glued together ("1745SY") -> two parts
    split_parts = []
    for p in parts:
        m = re.fullmatch(r"([\d,]*\.?\d+)([A-Za-z][A-Za-z.]{0,10})", p.strip())
        if m and _unit_score(m.group(2)):
            split_parts.extend([m.group(1), m.group(2)])
        else:
            split_parts.append(p)
    parts = [p for p in split_parts if p]

    item_no = ""
    # A leading short integer/code is an item number, not a quantity.
    if parts and re.fullmatch(r"\d{1,4}[.)]?|[A-Z]?\d{1,3}[-.]\d{1,4}", parts[0]):
        item_no, parts = parts[0].rstrip(".)"), parts[1:]

    unit, unit_i = "", -1
    for i in range(len(parts) - 1, -1, -1):
        if _unit_score(parts[i]):
            unit, unit_i = upstream.norm_unit(parts[i]).upper(), i
            break

    qty, qty_i = None, -1

    def pos(i):
        v = _num(parts[i])
        return v is not None and v > 0

    if unit_i >= 0:
        # desc, unit, qty [, prices] is a takeoff's usual order; a bid form
        # prints qty, unit, unit price, amount -- so a plain number right of
        # the unit wins, then the plain number right before the unit, and a
        # money-looking number ($ 48,000.00) only when there is nothing else
        after = [i for i in range(unit_i + 1, len(parts)) if pos(i)]
        plain = [i for i in after if not _money_like(parts[i])]
        b = unit_i - 1
        if plain:
            qty_i = plain[0]
        elif b >= 0 and pos(b) and not _money_like(parts[b]) and \
                not (b == 0 and item_no == "" and len(parts) <= 2):
            qty_i = b
        elif after:
            qty_i = after[0]
    if qty_i < 0:
        for i in range(len(parts) - 1, -1, -1):
            if i != unit_i and pos(i):
                qty_i = i
                break
    if qty_i >= 0:
        qty = _num(parts[qty_i])

    # numbers after the description are price / amount columns, not words
    first_num = min([i for i in (unit_i, qty_i) if i >= 0] or [len(parts)])
    desc_parts = [p for i, p in enumerate(parts) if i not in (unit_i, qty_i)
                  and not (i > first_num and (_money_like(p) or _num(p) is not None))
                  and not re.fullmatch(r"[$\s]+", p)]
    desc = _txt(" ".join(desc_parts))
    if len(desc) < 3:
        return None

    issues: List[str] = []
    if qty is None:
        issues.append("no quantity found")
    if not unit:
        issues.append("no unit found")
    return TakeoffLine(row_id="r0", description=desc, unit=unit, quantity=qty,
                       item_no=item_no,
                       confidence="low" if issues else "ok",
                       issues=issues, raw=s[:300])


_HEADER_LINE_RE = re.compile(
    r"(?:(?:bid|pay|line)?\s*items?\s*(?:no\.?|#|number)?|descriptions?|units?|"
    r"u/m|uom|quantity|qty|est\.?|estimated|approx\.?|unit\s+price|amount|"
    r"total|price|[\s.,/&-])+")
_QTY_UNIT_RE = re.compile(
    r"^\s*\(?\$?([\d,]*\.?\d+)\)?\s*([A-Za-z][A-Za-z./ ]{0,14}?)\.?\s*$")


def _split_qty_unit(s):
    """(quantity, unit) from one cell holding both ("1,745 SY", "1745SY"),
    else (None, "")."""
    m = _QTY_UNIT_RE.match(_txt(s))
    if not m:
        return None, ""
    q = _num(m.group(1))
    u = m.group(2).strip()
    if q is None or not u or not _unit_score(u):
        return None, ""
    return q, upstream.norm_unit(u).upper()


def _money_like(p):
    """A price / amount cell: '$1,234.50', '1,234.50', '(500.00)'."""
    t = _txt(p)
    return bool(re.fullmatch(r"\(?\$?\s*[\d,]*\.\d{2}\)?|\$\s*[\d,]+", t))


def _quality(p):
    """How usable a parse is: complete lines count most."""
    sc = 0.0
    for l in p.lines:
        sc += 1.0 if (l.quantity is not None and l.unit) else 0.3
        if l.item_no:
            sc += 0.2
        if l.confidence == "low":
            sc -= 0.5
    return sc


def _pdf_layout(path, name):
    """pdftotext -layout (tools/poppler) -> the text parser."""
    import shutil
    import subprocess
    exe = os.path.join(os.path.dirname(upstream.SRC_DIR), "tools", "poppler",
                       "bin", "pdftotext")
    if not os.path.exists(exe):
        exe = shutil.which("pdftotext")
    if not exe:
        return None
    out = subprocess.run([exe, "-layout", path, "-"], capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    p = from_text(out.stdout, name)
    p.method = "pdf/pdftotext-layout"
    return p


def _pdf_bidtab(path, name):
    """The pipeline's bid-tab reader (src/parse_bidtabs.read_pdf_best): one
    line per bid-form item, bidders' prices dropped."""
    if not upstream.PARSER_AVAILABLE or os.environ.get("TAKEOFF_NO_BIDTAB"):
        return None          # (the switch lets intake_eval score the rest alone)
    stats = {"cells_blank_price": 0, "sheets_no_header": []}
    got = upstream.read_pdf_best(path, stats)
    recs = got[0] if isinstance(got, tuple) else got
    out = TakeoffParse(source=name, method="pdf/bid-tab parser")
    seen = set()
    for r in recs or []:
        k = (r.get("item_no"), r.get("description"), r.get("unit"),
             r.get("quantity"))
        if k in seen or not r.get("description"):
            continue
        seen.add(k)
        issues = []
        if r.get("quantity") is None:
            issues.append("no quantity found")
        if not r.get("unit"):
            issues.append("no unit found")
        out.lines.append(TakeoffLine(
            row_id="r%d" % (len(out.lines) + 1),
            description=_txt(r.get("description")),
            unit=_txt(r.get("unit")), quantity=r.get("quantity"),
            item_no=_txt(r.get("item_no")),
            confidence="low" if issues else "ok", issues=issues))
    return out if out.lines else None


def _pdf_ocr(path, name):
    """A scanned PDF: the pipeline's OCR engines, then the text parser."""
    pages, eng = upstream.ocr_pdf_pages(path)
    if not pages:
        return None
    text = "\n".join(t for _, t in sorted(pages.items()))
    p = from_text(text, name)
    p.method = "pdf/ocr (%s)" % eng
    p.notes.append("Scanned PDF read with OCR (%s engine, offline). Check "
                   "every quantity." % eng)
    return p


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def from_file(path: str, original_name: str = "") -> TakeoffParse:
    name = original_name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx"):
        return _from_xlsx(path, name)
    if ext == ".xls":
        return TakeoffParse(source=name, method="unsupported", notes=[
            "Legacy .xls is not supported. Open it in Excel and save as "
            ".xlsx, or paste the rows in as text."])
    if ext in (".csv", ".txt", ".tsv"):
        return _from_csv(path, name)
    if ext == ".pdf":
        return _from_pdf(path, name)
    if ext == ".docx":
        return _from_docx(path, name)
    if ext == ".doc":
        return TakeoffParse(source=name, method="unsupported", notes=[
            "Legacy .doc is not supported. Save as .docx, or paste the rows "
            "in as text."])
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
        return _from_image(path, name)
    return TakeoffParse(source=name, method="unsupported", notes=[
        "Unsupported file type %r. Supported: .xlsx, .csv, .pdf, .docx and "
        "images." % ext])


def _from_xlsx(path: str, name: str) -> TakeoffParse:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    best: Optional[TakeoffParse] = None
    tried: List[str] = []
    for ws in wb.worksheets:
        grid = [[_txt(c) for c in row]
                for row in ws.iter_rows(values_only=True)]
        p = _lines_from_grid(grid, name, "xlsx/%s" % ws.title)
        tried.append("%s: %d item(s)" % (ws.title, len(p.lines)))
        if best is None or len(p.lines) > len(best.lines):
            best = p
    wb.close()
    if best is None:
        return TakeoffParse(source=name, method="xlsx",
                            notes=["Workbook has no sheets."])
    if len(tried) > 1:
        best.notes.append("Sheets scanned -- %s. Used the richest one."
                          % "; ".join(tried))
    return best


def _from_csv(path: str, name: str) -> TakeoffParse:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            grid = list(csv.reader(fh, dialect))
            method = "csv/sniffed"
        except csv.Error:
            fh.seek(0)
            return from_text(fh.read(), name)
    return _lines_from_grid(grid, name, method)


def _from_pdf(path: str, name: str) -> TakeoffParse:
    import pdfplumber
    grids: List[List[List[str]]] = []
    word_grid: List[List[str]] = []
    n_pages = 0
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for page in pdf.pages:
            for tbl in (page.extract_tables() or []):
                if tbl and len(tbl) >= 2:
                    grids.append([[_txt(c) for c in row] for row in tbl])
            try:
                words = page.extract_words() or []
            except Exception:  # noqa: BLE001
                words = []
            word_grid.extend(grid_from_words(words))

    candidates: List[TakeoffParse] = []
    if grids:
        merged: List[List[str]] = []
        for g in grids:
            merged.extend(g)
        candidates.append(_lines_from_grid(merged, name, "pdf/tables"))
    if word_grid:
        candidates.append(_lines_from_grid(word_grid, name, "pdf/coordinates"))
    # the pipeline's own readers: pdftotext -layout, the bid-tab parser, and
    # OCR when the page has no text layer -- all local, all deterministic
    readers = [_pdf_layout, _pdf_bidtab]
    if not word_grid:
        readers.append(_pdf_ocr)
    failed = []
    for fn in readers:
        try:
            c = fn(path, name)
        except Exception as e:  # noqa: BLE001 - one reader failing is fine
            failed.append("%s: %s" % (fn.__name__.lstrip("_"), e))
            c = None
        if c is not None:
            candidates.append(c)

    if not candidates:
        return TakeoffParse(source=name, method="pdf", notes=[
            "No text layer in this PDF (%d page(s)) and OCR read nothing -- "
            "paste the rows in as text." % n_pages] + failed)
    best = pick_pdf_candidate(candidates)
    best.notes.append(
        "PDF: %d page(s). Strategies tried -- %s. Kept %s."
        % (n_pages,
           "; ".join("%s=%d items" % (c.method, len(c.lines)) for c in candidates),
           best.method))
    if not best.lines:
        best.notes.append(
            "A text layer was found but no pay items could be read from it. "
            "Check the file, or paste the rows in as text.")
    return best


def pick_pdf_candidate(candidates: List[TakeoffParse]) -> TakeoffParse:
    """The validated bid-tab reader wins when it read a complete schedule
    (>= 3 lines, >= 80% with both a quantity and a unit); otherwise the
    reader whose lines are most complete (_quality), first one on a tie."""
    bt = [c for c in candidates if c.method == "pdf/bid-tab parser"]
    if bt and len(bt[0].lines) >= 3 and sum(
            1 for l in bt[0].lines if l.quantity is not None and l.unit
    ) >= 0.8 * len(bt[0].lines):
        return bt[0]
    return max(candidates, key=_quality)


def _from_docx(path: str, name: str) -> TakeoffParse:
    import docx  # python-docx
    d = docx.Document(path)
    candidates: List[TakeoffParse] = []
    for ti, tbl in enumerate(d.tables):
        grid = [[_txt(c.text) for c in row.cells] for row in tbl.rows]
        if len(grid) >= 2:
            candidates.append(_lines_from_grid(grid, name, "docx/table%d" % (ti + 1)))
    paras = "\n".join(_txt(p.text) for p in d.paragraphs if _txt(p.text))
    if paras:
        candidates.append(from_text(paras, name))
    if not candidates:
        return TakeoffParse(source=name, method="docx",
                            notes=["Document has no tables and no text."])
    best = max(candidates, key=lambda p: len(p.lines))
    best.notes.append("Word document: %d table(s). Kept %s."
                      % (len(d.tables), best.method))
    return best


def _from_image(path: str, name: str) -> TakeoffParse:
    try:
        words = ocr_mod.extract_words(path)
    except ocr_mod.OCRUnavailable as e:
        return TakeoffParse(source=name, method="image/ocr-unavailable",
                            notes=[str(e)])
    except ocr_mod.OCRContractError as e:
        return TakeoffParse(source=name, method="image/ocr-contract-error",
                            notes=["The OCR engine returned an unexpected "
                                   "shape: %s" % e])
    is_mock = any(w.get("mock") for w in words)
    grid = grid_from_words(words)
    p = _lines_from_grid(grid, name, "image/ocr")
    p.mock = is_mock
    if is_mock:
        p.notes.insert(0, "MOCK OCR -- these line items are a stand-in, not "
                          "read from your image. They cannot be priced.")
    else:
        p.notes.insert(0, "Read by OCR. Check every row: OCR confuses digits "
                          "and loses column alignment on crooked scans.")
    return p
