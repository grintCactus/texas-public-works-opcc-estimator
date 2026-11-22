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
import unicodedata
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
#: An item number as a cell holds it: 101, 2.01, 104-1, A-1, SP12, 7a, 3.
_ITEM_CODE = re.compile(r"[A-Za-z]{0,4}[-.]?\d{1,5}(?:[-.]\d{1,4}){0,2}"
                        r"[A-Za-z]?[.)]?")
#: A word that makes the number before it a size, not an item number:
#: "18 inch RCP", "24 in. riprap", "6 ft fence".
_SIZE_WORD = re.compile(r"(?:in(?:ch(?:es)?)?|ft|feet|foot|mm|cm|dia(?:meter)?)"
                        r"\.?,?|[\"'”″]", re.I)
#: Wordings a bid form almost always prices as one lump sum. A line worded
#: like this with no unit (and no quantity) is read as LS (and 1), marked
#: "assumed -- check" -- never silently.
_LS_WORDING = re.compile(
    r"\b(?:de)?mobili[sz]ation\b|\bbonds?\b|\binsurance\b|\bswppp\b|"
    r"storm\s*water\s+pollution|\btemporary\s+erosion\s+control\b|"
    r"\berosion\s+control\s+plan\b|\btraffic\s+control\b|\bstaking\b|"
    r"\bdewatering\b|right[\s-]+of[\s-]+way\s+prep", re.I)


def _assume_lump_sum(line: "TakeoffLine") -> None:
    """LS (and quantity 1) for a lump-sum wording that came with no unit --
    recorded in line.assumed and line.issues so the grid says "assumed"."""
    if line.unit or not _LS_WORDING.search(line.description or ""):
        return
    if line.quantity is not None and abs(line.quantity - 1.0) > 1e-9:
        return
    line.unit = "LS"
    line.assumed.append("unit")
    if line.quantity is None:
        line.quantity = 1.0
        line.assumed.append("quantity")
    line.issues = [i for i in line.issues
                   if i not in ("no unit found", "no quantity found")]
    line.issues.append("assumed %s -- check" % (
        "LS, quantity 1" if "quantity" in line.assumed else "unit LS"))
    line.confidence = "low"


def _plausible_row(cells, di, ui, qi, ii) -> bool:
    """Do this row's cells fit the columns found for the sheet? A row with a
    leading or an extra tab, or with cells missing in the middle, does not:
    its description column holds a number, its unit column a sentence."""
    def at(j):
        return _txt(cells[j]) if (j is not None and j < len(cells)) else ""
    d, u, q, it = at(di), at(ui), at(qi), at(ii)
    if not d or _looks_numeric(d) or _unit_score(d):
        return False
    if u and not _unit_score(u) and _split_qty_unit(u)[0] is None:
        return False
    if q and _num(q) is None and _split_qty_unit(q)[0] is None:
        return False
    if it and len(it) > 12 and not _ITEM_CODE.fullmatch(it):
        return False
    # nothing left over outside the four columns but empty cells or prices
    used = {j for j in (di, ui, qi, ii) if j is not None}
    for j, c in enumerate(cells):
        c = _txt(c)
        if j in used or not c:
            continue
        if _unit_score(c) or (not _looks_numeric(c) and len(c) >= 8):
            return False
    return True


def _cells_by_content(cells):
    """(item_no, description, unit, quantity) from one row's cells by what
    they hold, not where they sit: an item code first, a unit token, a plain
    number for the quantity (after the unit, else after the description),
    and the remaining text for the description."""
    cs = [_txt(c) for c in cells]
    nz = [i for i, c in enumerate(cs) if c]
    used = set()
    unit, qty, item = "", None, ""
    unit_i = desc_i = -1
    for i in nz:                                    # "1,745 SY" in one cell
        q2, u2 = _split_qty_unit(cs[i])
        if q2 is not None and not _looks_numeric(cs[i]):
            qty, unit, unit_i = q2, u2, i
            used.add(i)
            break
    if not unit:
        for i in reversed(nz):
            if _unit_score(cs[i]):
                unit, unit_i = upstream.norm_unit(cs[i]).upper(), i
                used.add(i)
                break
    nums = [i for i in nz if i not in used and _num(cs[i]) is not None]
    texts = [i for i in nz if i not in used and _num(cs[i]) is None]
    if nz and nz[0] in nums and _ITEM_CODE.fullmatch(cs[nz[0]]) and texts \
            and nz[0] < texts[0] and (len(nums) >= 2 or not unit or qty is not None):
        item = cs[nz[0]].rstrip(".)")
        used.add(nz[0])
        nums = [i for i in nums if i != nz[0]]
    elif nz and nz[0] in texts and _ITEM_CODE.fullmatch(cs[nz[0]]) and \
            len(texts) >= 2:
        item = cs[nz[0]].rstrip(".)")             # "A-1", "2.01a"
        used.add(nz[0])
        texts = texts[1:]
    if texts:
        desc_i = texts[0]
    if qty is None and nums:
        plain = [i for i in nums if not _money_like(cs[i])] or nums
        after_u = [i for i in plain if unit_i >= 0 and i > unit_i]
        after_d = [i for i in plain if i > desc_i]
        pick = (after_u or after_d or plain)[0]
        qty = _num(cs[pick])
        used.add(pick)
    desc = _txt(" ".join(cs[i] for i in texts))
    return item, desc, unit, qty


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
    #: Fields the reader filled in rather than read ("unit", "quantity"): a
    #: lump-sum wording with no unit and no quantity becomes LS / 1, shown in
    #: the grid as "assumed -- check".
    assumed: List[str] = field(default_factory=list)

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
    #: Headings, totals and column headers: rows that are not pay items,
    #: kept so nothing disappears silently.
    skipped: List[dict] = field(default_factory=list)
    #: Lines that could not be read as pay items -- garbled text, symbols or
    #: numbers only, words with no unit and no quantity -- with their text,
    #: so the page can show them ({row, reason, raw}).
    unread: List[dict] = field(default_factory=list)
    #: A takeoff filled in the template's layout carries its project block
    #: (name, project no., prepared by), so the worksheet and the exported
    #: OPCC are headed without typing it again (user, 2026-11-21).
    project: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "lines": [l.to_dict() for l in self.lines],
            "source": self.source,
            "method": self.method,
            "notes": self.notes,
            "mock": self.mock,
            "skipped": self.skipped[:80],
            "n_skipped": len(self.skipped),
            "unread": self.unread[:80],
            "n_unread": len(self.unread),
            "project": self.project,
        }


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cleaning and judging text (QA 2026-11-20: "what about garbled input?" -- garbage must come
# back as "could not read", never as line items)
# ---------------------------------------------------------------------------

#: Non-ASCII characters real schedules use. clean_text keeps them, and they
#: do not count as foreign when a line is judged: degree, diameter, O-slash,
#: fractions, dashes, times, plus-minus, curly quotes, feet / inch primes,
#: squared / cubed, section sign, bullet.
SCHEDULE_CHARS = frozenset(
    "\u00b0\u2300\u00d8\u00f8\u00bd\u00bc\u00be\u2153\u2154\u215b"
    "\u2013\u2014\u00d7\u00b1\u2018\u2019\u201c\u201d\u2032\u2033"
    "\u00b2\u00b3\u00a7\u2022")
#: The two "other symbols" (Unicode So) a schedule uses; every other So --
#: emoji, pictographs, box drawing, (c), (r), U+FFFD -- is dropped.
_SO_KEEP = frozenset("\u00b0\u2300")
#: Characters str.splitlines() treats as line breaks: kept as line breaks.
_BREAKS = str.maketrans({c: "\n" for c in "\x0b\x0c\x1c\x1d\x1e\x85"})
_ASCII_CTRL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


def _dropped(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
        return ch not in "\t\n\r"
    if cat == "So":
        return ch not in _SO_KEEP
    o = ord(ch)
    return (0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF   # variation selectors
            or o == 0x20E3 or 0x1F3FB <= o <= 0x1F3FF)            # keycap, skin tones


def clean_text(s: str) -> str:
    """What no schedule holds and what can break a page, removed: control
    characters (tabs and line breaks stay -- they carry the layout), format
    characters (zero-width space, joiners, bidi overrides such as U+202E,
    BOM, soft hyphen), U+FFFD, private-use and unassigned code points,
    emoji and other pictographs. Schedule characters (", ', °, Ø, ½, –, ×,
    ±, curly quotes) stay. "Rip\u200brap" reads "Riprap"."""
    if not s:
        return s
    s = s.translate(_BREAKS)
    if s.isascii():
        return _ASCII_CTRL.sub("", s)
    out = []
    for ch in s:
        if ch < "\x80":
            if (ch >= " " and ch != "\x7f") or ch in "\t\n\r":
                out.append(ch)
        elif not _dropped(ch):
            out.append(ch)
    return "".join(out)


def _txt(v) -> str:
    if v is None:
        return ""
    s = clean_text(str(v)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


_WORD = re.compile(r"[A-Za-z]{3,}")
#: Two-letter words a description can be made of (besides the units).
_SHORT_WORDS = frozenset({"AC", "HP", "MH", "DI", "CI", "RC", "PC", "SS", "WL",
                          "SD", "FH", "GV", "BV", "PE", "CO", "TV"})


def foreign_share(s: str) -> float:
    """Share of the non-space characters that are neither printable ASCII
    nor a schedule character."""
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    n = sum(1 for c in chars if not ("!" <= c <= "~") and c not in SCHEDULE_CHARS)
    return n / len(chars)


def _has_word(s: str) -> bool:
    if _WORD.search(s):
        return True
    return any(t.upper() in KNOWN_UNITS or t.upper() in _SHORT_WORDS
               for t in re.split(r"[^A-Za-z]+", s) if t)


def garbage_reason(text: str) -> Optional[str]:
    """Why this is not a pay item's description, in plain words -- or None
    when it reads as one: a real word (3+ letters running, or a known unit
    / short word) and at most 30% characters that are not plain English."""
    s = _txt(text)
    if foreign_share(s) > 0.3:
        return "garbled or not English"
    if not _has_word(s):
        if re.search(r"[A-Za-z]", s):
            return "no words"
        return "numbers only" if re.search(r"\d", s) else "symbols only"
    return None


#: Wordings of a section heading ("BASE BID", "Alternate No. 2", "Part A").
_HEADING_WORDS = re.compile(
    r"^(base\s+bid|((add(itive)?|deduct(ive)?)\s+)?alternates?\b|"
    r"alt\.?\s*(no\.?)?\s*\d|bid\s+schedule|schedule\s+[a-z0-9]{1,3}\b|"
    r"(part|section|division|phase|unit)\s+[a-z0-9]{1,4}\b)", re.I)
#: A description that ends like this goes on on the next line.
_GOES_ON = re.compile(r"([,&/+-]|\b(and|or|of|with|for|in|to|at|by|per|"
                      r"incl\.?|including|w/))\s*$", re.I)


def _heading_like(desc: str) -> bool:
    """A section heading rather than a pay item: a line with no unit and no
    quantity that is all capitals ("STORM DRAINAGE"), ends with a colon, or
    is worded like one ("Base Bid", "Alternate No. 2")."""
    s = _txt(desc)
    words = s.split()
    if not words or len(words) > 8 or len(s) > 80 or max(len(w) for w in words) > 25:
        return False
    if s.endswith(":") or _HEADING_WORDS.match(s):
        return True
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


_TEXTY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                   " ,.-/()'\"&#:;") | SCHEDULE_CHARS


def _texty(s: str) -> bool:
    """Reads like words (a wrapped description), not a run of symbols: at
    least 85% letters, digits, spaces and ordinary punctuation, and no
    "word" longer than 30 characters."""
    s = _txt(s)
    return bool(s) and sum(1 for c in s if c in _TEXTY) >= 0.85 * len(s) and \
        max(len(w) for w in s.split()) <= 30


def _strong_heading(desc: str, prev_desc: str) -> bool:
    """A heading line between items that must not be read as the wrapped end
    of the description above it: all capitals, at most four words, no digits,
    and the line above did not stop mid-phrase."""
    s = _txt(desc)
    if not s or len(s.split()) > 4 or re.search(r"\d", s) or _GOES_ON.search(prev_desc or ""):
        return False
    if s.endswith(":") or _HEADING_WORDS.match(s):
        return True
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and \
        not re.match(r"(and|or|of|with|for|in|to|incl|including)\b", s, re.I)


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
    # An item-number column: short codes (101, 2.01, A-1) in most filled
    # rows, left of the description. Without it a header-less paste (an
    # Excel block, the page's own example) loses every item number.
    item = None
    if "desc" in cols:
        for j in range(cols["desc"]):
            if j == cols.get("unit") or not filled[j]:
                continue
            codes = sum(1 for row in body if j < len(row)
                        and _ITEM_CODE.fullmatch(_txt(row[j])))
            if codes and codes >= 0.6 * filled[j]:
                item = j
                break
    # The quantity is the numeric column closest to the right of the unit
    # column; failing that, the most-numeric column that is not the item no.
    if max(num_score) > 0:
        cand = [j for j in range(width) if num_score[j] > 0]
        if "unit" in cols:
            right = [j for j in cand if j > cols["unit"]]
            cols["qty"] = (right[0] if right
                           else max(cand, key=lambda j: num_score[j]))
        else:
            rest = [j for j in cand if j != item] or cand
            cols["qty"] = max(rest, key=lambda j: num_score[j])
    if item is not None and item not in (cols.get("qty"), cols.get("unit")):
        cols["item_no"] = item
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
        # the columns are read off the rows that have several cells: a line
        # pasted with spaces instead of tabs is one cell and says nothing
        # about the columns
        multi = [r for r in body if sum(1 for c in r if _txt(c)) >= 2]
        cmap = _infer_columns(multi or body)
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

    # Typed or pasted grids (text, CSV, spreadsheets, Word tables) are read
    # row by row: a row whose cells do not fit the columns -- a leading or an
    # extra tab, a missing cell -- is read by what its cells hold, and a row
    # typed with spaces in one cell is read as a free-text line. Nothing is
    # shifted into the wrong column (QA 2026-11-20). PDF and OCR grids keep
    # their own column logic.
    by_row = not method.startswith(("pdf/", "image/"))
    n = 0
    n_content = 0
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
        filled = [_txt(c) for c in row if _txt(c)]
        free = None
        if by_row and len(filled) == 1 and len(filled[0].split()) >= 2:
            free = _parse_free_line(filled[0])
            if free is not None and (free.unit or free.quantity is not None):
                item_no, desc, unit, qty = (free.item_no, free.description,
                                            free.unit, free.quantity)
                n_content += 1
            else:
                free = None
        if by_row and free is None and len(filled) >= 2 and \
                not _plausible_row(row, di, ui, qi, ii):
            item_no, desc, unit, qty = _cells_by_content(row)
            n_content += 1
        if by_row and free is None and len(filled) == 1 and not desc and \
                _num(filled[0]) is None:
            # a lone cell of text outside the description column (a heading,
            # a note, garbage): judged below, never dropped without a word
            desc = filled[0]
        if qty is None or not unit:
            # quantity and unit in one cell: "1,745 SY", "1745SY"
            for j in ([qi] if qi is not None else []) + ([ui] if ui is not None else []):
                q2, u2 = _split_qty_unit(cell(j))
                if q2 is not None:
                    qty = qty if qty is not None else q2
                    unit = unit or u2
                    break

        raw_row = " | ".join(filled)
        if not desc:
            # A row with a quantity but no description is a wrapped line or a
            # broken parse: shown as not read instead of dropped.
            if qty is not None or unit:
                out.unread.append({"row": src_row, "reason": "no description",
                                   "raw": raw_row[:200]})
            continue
        if _is_not_item(desc):
            out.skipped.append({"row": src_row, "reason": "total/heading row",
                                "raw": desc[:200]})
            continue
        if qty is None and _HEADER_LINE_RE.fullmatch(raw_row.lower().replace("|", " ")):
            out.skipped.append({"row": src_row, "reason": "column header",
                                "raw": raw_row[:200]})
            continue
        why = garbage_reason(desc) or ("too short" if len(desc) < 3 else None)
        if why:
            out.unread.append({"row": src_row, "reason": why, "raw": raw_row[:200]})
            continue
        # the item number stands in a cell of its own: a takeoff row with
        # cells missing ("needs input"), not a stray line of text
        structured = bool(item_no) and free is None and len(filled) >= 2

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
        line = TakeoffLine(
            row_id="",
            description=desc,
            unit=upstream.norm_unit(unit).upper() if unit else "",
            quantity=qty,
            item_no=item_no,
            confidence="low" if issues else "ok",
            issues=issues,
            raw=" | ".join(_txt(c) for c in row)[:300],
            amount=amount if amount and amount > 0 else None,
            unit_price=unit_price if unit_price and unit_price > 0 else None,
        )
        if by_row:
            _assume_lump_sum(line)
        # no unit and no quantity: a section heading, or a line that cannot
        # be read as a pay item -- unless it is a structured takeoff row
        if not _has_unit(line.unit) and line.quantity is None and not structured:
            if _heading_like(desc):
                out.skipped.append({"row": src_row, "reason": "section heading",
                                    "raw": desc[:200]})
            else:
                out.unread.append({"row": src_row, "reason": "no unit or quantity",
                                   "raw": raw_row[:200]})
            continue
        n += 1
        line.row_id = "r%d" % n
        out.lines.append(line)
    if n_content:
        out.notes.append("%d row(s) did not line up with the columns and were "
                         "read by what their cells hold." % n_content)
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

#: A description longer than this is cut (and flagged): bid-form wordings
#: are far shorter, and the classifier's time grows with the square of the
#: length (50,000 characters took 36 s).
MAX_DESC = 300


def _cap_descriptions(p: "TakeoffParse") -> "TakeoffParse":
    for l in p.lines:
        if len(l.description or "") > MAX_DESC:
            l.description = l.description[:MAX_DESC].rstrip()
            l.issues.append("description cut to %d characters -- check" % MAX_DESC)
            l.confidence = "low"
    return p


def _has_unit(u) -> bool:
    """A unit, recognised or a short unit-like word kept as written."""
    u = _txt(u)
    return bool(u) and (_unit_score(u) or bool(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9./ ]{0,7}", u)))


def _screen(p: "TakeoffParse") -> "TakeoffParse":
    """The last word on every reader's lines: cleaned text, and only lines
    that read as pay items -- a real word in the description and a unit or
    a quantity (or an item number of their own, from a structured reader).
    The rest go to p.unread (or p.skipped for headings) with their text."""
    kept = []
    for l in p.lines:
        l.description, l.unit, l.item_no = _txt(l.description), _txt(l.unit), _txt(l.item_no)
        raw = (_txt(l.raw) or l.description)[:200]
        why = garbage_reason(l.description)
        if why is None and not _has_unit(l.unit) and l.quantity is None and not l.item_no:
            if _heading_like(l.description):
                p.skipped.append({"row": None, "reason": "section heading", "raw": raw})
                continue
            why = "no unit or quantity"
        if why:
            p.unread.append({"row": None, "reason": why, "raw": raw})
            continue
        kept.append(l)
    if len(kept) != len(p.lines):
        p.lines = kept
        for i, l in enumerate(p.lines, 1):
            l.row_id = "r%d" % i
    return p


def from_text(text: str, source: str = "pasted text") -> TakeoffParse:
    """Parse pasted lines (cleaned, screened, long descriptions cut and
    flagged)."""
    return _cap_descriptions(_screen(_from_text(text, source)))


def _from_text(text: str, source: str = "pasted text") -> TakeoffParse:
    """Parse pasted lines. Delimiter is detected per paste, then per line."""
    raw_lines = [l for l in clean_text(text or "").splitlines()]
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
            t = _txt(raw)
            if t and _is_not_item(t):
                out.skipped.append({"row": k + 1, "reason": "total/heading row",
                                    "raw": t[:200]})
            elif t:
                out.unread.append({"row": k + 1, "reason": garbage_reason(t)
                                   or "too short", "raw": t[:200]})
            continue
        if _HEADER_LINE_RE.fullmatch(line.description.lower()) and \
                line.quantity is None:
            out.skipped.append({"row": k + 1, "reason": "column header",
                                "raw": raw[:200]})
            continue
        why = garbage_reason(line.description)
        if why:              # never an item, and never glued to the one above
            out.unread.append({"row": k + 1, "reason": why, "raw": _txt(raw)[:200]})
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
            if bare and prev.quantity is not None and _texty(raw) and \
                    not _strong_heading(line.description, prev.description):
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
        line._row = k + 1
        out.lines.append(line)
    if joined:
        out.notes.append("%d wrapped description line(s) joined to the line "
                         "above." % joined)
    kept = []
    for line in out.lines:          # after joining: a wrapped line may carry
        _assume_lump_sum(line)      # the unit and quantity
        if not _has_unit(line.unit) and line.quantity is None:
            row = getattr(line, "_row", None)
            if _heading_like(line.description):
                out.skipped.append({"row": row, "reason": "section heading",
                                    "raw": line.description[:200]})
            else:
                out.unread.append({"row": row, "reason": "no unit or quantity",
                                   "raw": (line.raw or line.description)[:200]})
            continue
        n += 1
        line.row_id = "r%d" % n
        kept.append(line)
    out.lines = kept
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

    # a unit written in two words ("Lump Sum", "Sq. Yd.", "Cubic Yard",
    # "Linear Feet") -> one part, so it is read as the unit instead of
    # being left in the description
    def _nu(x):
        return upstream.norm_unit(x).upper()

    merged = []
    for p in parts:
        prev = merged[-1] if merged else ""
        pair = prev + " " + p
        if prev and not _looks_numeric(prev) and _unit_score(pair) and (
                not _unit_score(prev) or _nu(prev) == _nu(p) == _nu(pair)):
            merged[-1] = pair
        else:
            merged.append(p)
    parts = merged

    item_no = ""
    # A leading short integer/code is an item number, not a quantity --
    # unless a size word follows it ("18 inch RCP", "6 ft fence").
    if parts and re.fullmatch(r"\d{1,4}[.)]?|[A-Z]?\d{1,3}[-.]\d{1,4}", parts[0]) \
            and not (len(parts) > 1 and _SIZE_WORD.fullmatch(parts[1])):
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
    """Read an uploaded takeoff (cleaned, screened, long descriptions cut
    and flagged)."""
    return _cap_descriptions(_screen(_from_file(path, original_name)))


def _from_file(path: str, original_name: str = "") -> TakeoffParse:
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
    best_rows: list = []
    tried: List[str] = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        grid = [[_txt(c) for c in row] for row in rows]
        p = _lines_from_grid(grid, name, "xlsx/%s" % ws.title)
        tried.append("%s: %d item(s)" % (ws.title, len(p.lines)))
        if best is None or len(p.lines) > len(best.lines):
            best, best_rows = p, rows
    wb.close()
    if best is not None:
        best.project = _template_project(best_rows)
    if best is None:
        return TakeoffParse(source=name, method="xlsx",
                            notes=["Workbook has no sheets."])
    if len(tried) > 1:
        best.notes.append("Sheets scanned -- %s. Used the richest one."
                          % "; ".join(tried))
    return best


def _template_project(rows: list) -> dict:
    """The project block of a takeoff filled in the template's (OPCC) layout:
    the title in A1, DATE: / PREPARED BY: / PROJECT NO.: in D3:D5, the name in
    A3 and the values in F4:F5. {} for any other layout."""
    def cell(r, c):
        try:
            v = rows[r][c]
        except (IndexError, TypeError):
            return ""
        return "" if v is None else str(v)
    if "PROBABLE CONSTRUCTION COST" not in cell(0, 0).upper():
        return {}
    labels = [_txt(cell(r, 3)).upper() for r in (2, 3, 4)]
    if labels != ["DATE:", "PREPARED BY:", "PROJECT NO.:"]:
        return {}
    name = " \u2013 ".join(x.strip() for x in cell(2, 0).splitlines() if x.strip())
    if name.strip("() ").lower() == "project name":
        name = ""
    out = {"name": _txt(name)[:160], "prepared_by": _txt(cell(3, 5))[:60], "project_no": _txt(cell(4, 5))[:60]}
    return {k: v for k, v in out.items() if v}


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
