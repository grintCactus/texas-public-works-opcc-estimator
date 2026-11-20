"""Read OCR text sidecars into line-item rows, in parse_bidtabs' record schema.

Why this exists
---------------
`src/ocr_scans.py` OCRs the bid tabulations that are image-only scans and
caches one text sidecar per PDF at data/external/_ocr/<relpath>.txt: `###`
header lines (MACHINE-READ notice, source, engine), then `### PAGE n` blocks
of column-aligned text rebuilt from OCR word boxes (rows_to_layout).
parse_bidtabs.py never read them, so every scan contributed 0 rows
(HANDOFF.md 2.2(d), 2.5 item 4).  parse_sidecar() returns the same per-file
records rows_from_grid / read_pdf_coords build for a PDF, plus OCR
provenance, so wiring it in is a call, not a second schema.

What a cell has to prove before it is emitted
---------------------------------------------
OCR damages digits, the part that matters.  As in src/ocr_verify.py nothing
is trusted because it looks right: a bidder's price is emitted only when its
own row proves it,

    printed quantity x unit price == printed extended total   (within 0.5%)

-- the identity and tolerance of parse_bidtabs' ext_mismatch.  The known
OCR confusions are undone BEFORE the test (ocr_verify.fix_number first),
which is safe because the test comes after: no repair makes a wrong reading
balance.  Undone, each offered as a reading the arithmetic must confirm:
    $41 ,500.oo  s 43,500.00  S14.40   ocr_verify's 'oo', 's' for '$', spaces
    $2.001,750.oo                      two cells glued: a value ends at its cents
    1 18,430.00                        a space inside the leading group
    148.050.00  78,000,00              ',' read as '.', '.' read as ','
    11200.00                           ',' read as '1' (= 1,200.00), alternative only
    1*000.00  201 t600  $ =1,171.00    ',' read as '*'/'t', debris after '$'
    000.00  ,200.oo                    lost leading digits: unreadable, kept in place
A lump sum whose quantity is printed in words ('Lump Sum LS') or misread
('4 Ls') takes quantity 1 (qty_source lump_sum_words / lump_sum_implied);
the check is then unit price == total.  Handwritten sheets (Farmers Branch
R2024-133: 'coo o', '3Oo') stay unread: nothing balances.

...and which bidder it belongs to
---------------------------------
OCR loses empty cells ('$ -' prints as a lone 's', or as nothing) and a lost
cell slides every later price one column left -- a shift the arithmetic
cannot see.  So each row's cells are ALIGNED to the page's bidder columns
(align(): every column takes 0-2 consecutive cells, blank / NO BID /
unreadable cells hold their column), and a cell is kept only when every best
alignment, under every equally good reading of the row, gives it the same
column and value.  Otherwise it is dropped as `ambiguous_column`.

Bidder names come only from the tabulation's header: the band of lines over
the price columns (a name may wrap onto 2-3 lines).  Where the page's rows
locate its price columns (slot_profile), each name is matched to the column
it is printed over -- a column with no name over it (an engineer's estimate
captioned elsewhere, an AVG column) is left unnamed and its cells dropped as
`no_bidder_names`.  Otherwise the k-th name goes to the k-th column, which
needs exactly as many names as columns.  A run of names OCR printed as one
('Reliable Paving SYBConstruction McMahon Contracting Tiseo Paving') is cut
only at firm words (parse_bidtabs' _FIRM_WORD_RE plus a few trade words),
preferring legal suffixes, and only when exactly one cut fits.  Names must
read as names (valid_name: no captions, form labels like 'CO-OP: Yes',
'Primary Award', or debris like 'Serle eee').  A page without recoverable
names borrows them from the facing page that prints the same header, or
from the previous page when it prints none (parse_bidtabs' fallback_names);
else its cells are left out.  Names are never made up.

Other layouts handled: item numbers glued to text or table rules ('102Right',
'201|Remove', '124" DR18' = item 1 by the sequence), suffixed or dotted items
('5A', 'A1', '1.10', '#0-2'), letter codes OCR read with letters for digits
('Di4', 'F1i0Connect' after D13 / F9: by the section's sequence), an item
number alone on the line beside its row, prices-only continuation pages
mapped row for row onto the item page (NRH 6885: 11 more bidders) when the
counts agree AND rows with quantity != 1 confirm it, an alternate block
under a SUBTOTAL ('A1', '5A' items).

Descriptions wrapped over several lines (assign_wrapped_lines): every text
line goes to an item by its vertical position -- the price row it sits
nearer to on a page of centred cells (Tarrant F2025096: two lines over the
numbers, three under), the row below it on a bottom-aligned page (Rowlett,
Stevens), the row above on a top-aligned one -- with the sentence itself as
evidence ('... equipment for' / 'structural concrete.' is one cell), item
lines that yield no row keeping their own text, and `### RULE` lines
(table rules ocr_scans.py found on the image) as hard cell edges.

Units: read beside the quantity also through a cell rule or a period ('120
|Hour', '2. EA') and in OCR's misreadings ('VE' = VF, 'LE' = LF), from the
page's unit column, or from the same pay item printed on another page.  A
row whose unit is still unread keeps unit '' with unit_note
'ocr_unit_unread': it stays in the letting's totals, and the empty unit
keeps it out of every unit-price table (build_master groups on unit;
estimate_low_bid skips a line without one) -- parse_bidtabs' own path for
'lump_sum_with_quantity'.  It is not unit_suspect, which would take its
money out of the panel and the totals.

Pages: a sidecar that OCR'd only some pages of a PDF marks the others
`### PAGE n (text layer)`; rows are read from the OCR'd pages only.

Marked so downstream code can exclude or down-weight it:
    text_source  "ocr"
    ocr_engine   the engine named in the sidecar header
    ocr_check    "qty_x_price"        quantity != 1: a real multiplication
                 "unit_equals_total"  quantity == 1: shows only that the two
                                      cells were read alike (weaker)
                 "columns_agree"      --implied-qty only (see below)

Usage
-----
    python src/parse_ocr_sidecars.py --selftest
    python src/parse_ocr_sidecars.py --report [--csv PATH]
    python src/parse_ocr_sidecars.py --show <path substring>   # one file, rows listed
    python src/parse_ocr_sidecars.py --report --implied-qty    # opt-in, see below

--implied-qty (OFF by default, and outside the gate as specified): where no
printed quantity balances a row, accept a quantity q* on which 2+ bidders'
(unit, total) pairs agree to the cent (qty_source implied_by_columns).

Read-only: reads data/external/_ocr/**, writes nothing unless --csv is given.
parse_bidtabs.py calls sidecar_for / parse_sidecar / ocr_date_lines;
ocr_scans.py ranks engine readings with yield_score.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import ocr_verify as ov          # noqa: E402  the documented OCR confusions
import parse_bidtabs as pb       # noqa: E402  the record schema's normalisers

ROOT = SRC.parent
EXTERNAL = ROOT / "data" / "external"
OCR_DIR = EXTERNAL / "_ocr"

TOL = 0.005            # parse_bidtabs' ext_mismatch tolerance, relative to the extension
MAX_READINGS = 64      # tokenisation alternatives tried per row
MAX_ALIGNMENTS = 4000  # optimal alignments enumerated per reading before giving up

DROP_REASONS = (
    "unbalanced",            # qty x unit price != printed total after repairs
    "row_fails_entirely",    # no cell of the row balances: quantity misread or lost
    "quantity_missing",      # no quantity printed/readable and not a lump sum
    "cell_missing",          # only one of the two cells (unit / total) was read
    "unreadable_amount",     # a cell OCR left in no readable money form
    "ambiguous_column",      # the cell balances, but which bidder it belongs to is not unique
    "ambiguous_reading",     # two OCR readings balance with different unit prices
    "no_bidder_names",       # the page's bidder names could not be recovered
    "continuation_unmatched",  # a prices-only page that does not line up with its item page
)


# --------------------------------------------------------------- the sidecar

_SRC_LINE = re.compile(r"^###\s*Source:\s*(?P<v>.+?)\s*$")
_ENG_LINE = re.compile(r"^###\s*Engine:\s*(?P<v>.+?)"
                       r"(?:,\s*page rendered at\s*(?P<dpi>\d+)\s*dpi)?\.?\s*$")
_PAGE_LINE = re.compile(r"^###\s*PAGE\s+(?P<n>\d+)(?:\s*\((?P<src>[^)]*)\))?\s*$")
# A horizontal table rule ocr_scans.py found on the rendered page, written
# between the text rows it separates.  Older sidecars have none.
RULE_LINE = "### RULE"


def read_sidecar(text):
    """(meta, pages): meta has source / engine / dpi / machine_read; pages is
    a list of (page_no, [lines])."""
    meta = {"source": "", "engine": "", "dpi": None, "machine_read": False,
            "page_source": {}}
    pages = []
    for raw in text.splitlines():
        ln = raw.replace("\t", " ").rstrip()
        m = _PAGE_LINE.match(ln)
        if m:
            n = int(m.group("n"))
            pages.append((n, []))
            # '### PAGE 2 (text layer)': a page ocr_scans.py copied from the
            # PDF's own text layer so the audit sees the whole document; only
            # OCR'd pages are read for rows here
            src = (m.group("src") or "").lower()
            meta["page_source"][n] = "text_layer" if "text" in src else "ocr"
            continue
        if ln.strip() == RULE_LINE:
            if pages:
                pages[-1][1].append(RULE_LINE)
            continue
        if ln.startswith("###"):
            if "MACHINE-READ" in ln:
                meta["machine_read"] = True
            ms, me = _SRC_LINE.match(ln), _ENG_LINE.match(ln)
            if ms:
                meta["source"] = ms.group("v")
            elif me:
                meta["engine"] = me.group("v").strip()
                if me.group("dpi"):
                    meta["dpi"] = int(me.group("dpi"))
            continue
        if pages:
            pages[-1][1].append(ln)
    return meta, pages


def sidecar_for(pdf_path):
    """data/external/_ocr/<path of the PDF relative to data/external>.txt, or
    None when the PDF is outside data/external or has no sidecar."""
    p = Path(pdf_path).resolve()
    try:
        rel = p.relative_to(EXTERNAL.resolve())
    except ValueError:
        return None
    sc = OCR_DIR / (rel.as_posix() + ".txt")
    return sc if sc.exists() else None


# ------------------------------------------------------ OCR digit repairs

_NUMISH = re.compile(r"[0-9oO.,]+")
_OO_ONLY = re.compile(r"[.,]?[oO]{1,3}[.,][oO]{2}|[.,][oO]{2,3}")


def deoh(line):
    """'o'/'O' -> '0' inside numbers ('$41 ,500.oo', '$11,ooo.oo', 'so.oo').

    Length-preserving, so column positions survive.  A run inside a word
    ('Wood', 'NOBID', '2:OOPM') is left alone: only a run that already holds
    a digit, or is the bare '.oo' / ',ooo.oo' shape, is a number.  A glued
    's'/'S' (the '$') or 't' (the ',') may touch the run.
    """
    out = list(line)
    for m in _NUMISH.finditer(line):
        run = m.group(0)
        if "o" not in run and "O" not in run:
            continue
        a, b = m.span()
        left = line[a - 1] if a else " "
        right = line[b] if b < len(line) else " "
        if (left.isalpha() and left not in "sSt") or (right.isalpha() and right not in "sS"):
            continue
        if not (any(c.isdigit() for c in run) or _OO_ONLY.fullmatch(run)):
            continue
        for k in range(a, b):
            if out[k] in "oO":
                out[k] = "0"
    return "".join(out)


_SEG_THOUS = re.compile(r"(\d{1,3})((?:,\d{3})+|(?:\.\d{3})+)\.(\d{2})")
_SEG_PLAIN = re.compile(r"(\d+)\.(\d{2})")


def seg_values(seg):
    """Candidate values of ONE money cell as OCR printed it, or None.

    ocr_verify.fix_number's rules (o->0 done by deoh, spaces beside a
    separator dropped, '.dd' cents) plus: ',' read as '.' when every
    separator is the same ('148.050.00'), '*' or 't' for ',', and -- as an
    ALTERNATIVE reading, never a replacement -- ',' read as '1' at the
    thousands position ('41473.00' may be 4,473.00).  The row's arithmetic
    decides between readings; a reading is never chosen by plausibility.
    """
    # ocr_verify's repairs first ('oo' for '00', a space beside a separator,
    # commas), for a value with its two cents digits; what they refuse as
    # ambiguous ('148.050.00', '1*000.00') falls through to the readings below
    base = seg.strip()
    # (not for ',200.oo': a value that opens with its separator lost its
    # leading digits, and ocr_verify would read it as 200.00)
    if (re.search(r"\.\s?[\doO]{2}$", base) and "*" not in base and "t" not in base
            and base[:1].isdigit()):
        v = ov.fix_number(base)
        if v is not None:
            ip = re.sub(r"\D", "", re.sub(r"\.\s?[\doO]{2}$", "", base.replace("o", "0").replace("O", "0")))
            if ip and not (len(ip) > 1 and ip.startswith("0")):
                vals = {v}
                if 5 <= len(ip) <= 7 and ip[-4] == "1" and "," not in base and "." not in base[:-3]:
                    vals.add(float(ip[:-4] + ip[-3:] + base[-3:].replace("o", "0").replace("O", "0")))
                return vals
    s = re.sub(r"\s*([,.*])\s*", r"\1", seg.strip())
    s = re.sub(r"(?<=\d)\s*t\s*(?=\d)", ",", s).replace("*", ",")
    if not s or " " in s:
        return None
    if "." not in s and re.search(r",\d\d$", s):
        s = s[:-3] + "." + s[-2:]      # the point read as a comma: '78,000,00'
    m = _SEG_THOUS.fullmatch(s)
    if m:
        if m.group(1).startswith("0"):
            return None
        return {float(m.group(1) + re.sub(r"[,.]", "", m.group(2)) + "." + m.group(3))}
    m = _SEG_PLAIN.fullmatch(s)
    if m:
        ip = m.group(1)
        if len(ip) > 1 and ip.startswith("0"):
            return None
        vals = {float(ip + "." + m.group(2))}
        if 5 <= len(ip) <= 7 and ip[-4] == "1":
            vals.add(float(ip[:-4] + ip[-3:] + "." + m.group(2)))
        return vals
    return None


def segmentations(run, cap=16):
    """Every way to read a numeric run as consecutive money cells.

    A value ends at its second decimal, so '$2.001,750.oo' may be '2.00' +
    '1,750.00'; it may also be one number.  Both are returned when both
    parse, and the row's arithmetic chooses.  [] when no reading parses.
    """
    t = run.strip()
    cuts = [m.end() for m in re.finditer(r"\.\d\d(?=[\d,.*t])", t)]
    out = []
    for r in range(len(cuts) + 1):
        for sub in itertools.combinations(cuts, r):
            pieces, a = [], 0
            for c in list(sub) + [len(t)]:
                pieces.append(t[a:c])
                a = c
            vals = [seg_values(p) for p in pieces]
            if all(vals):
                out.append(vals)
            if len(out) >= cap:
                return out
    if not out and re.search(r"\.\s?\d\d", t):
        # No reading parses ('000.006,100.00': the '$1,' of the first value
        # lost).  The cells are still there and still hold their columns:
        # split at every value end, keep what parses, mark the rest
        # unreadable (None) -- dropping them would slide every later price
        # one bidder to the left.
        pieces, a = [], 0
        for c in cuts + [len(t)]:
            pieces.append(t[a:c])
            a = c
        out.append([seg_values(p) for p in pieces])
    return out


_PLAIN_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _plain_number(t):
    t = t.strip()
    return float(t.replace(",", "")) if _PLAIN_NUM.fullmatch(t) else None


# ----------------------------------------------------------------- scanning

_NOBID = re.compile(r"(?i)(?:no\s*bid(?:\s*submitted)?|incomplete\s*bid|did\s*not\s*bid|"
                    r"n/a|non[\s-]?responsive)(?![A-Za-z])")


class Tok:
    """One scanned token: kind in word / num / amt / bad / cur / blank /
    nobid / dash / junk; a,b = column span."""
    __slots__ = ("kind", "a", "b", "text", "alts", "val", "join")

    def __init__(self, kind, a, b, text, alts=None, val=None):
        self.kind, self.a, self.b, self.text = kind, a, b, text
        self.alts, self.val, self.join = alts, val, None

    def __repr__(self):
        return "Tok(%s,%d,%r)" % (self.kind, self.a, self.text)


def _dollar_kind(s, i):
    """'cur' for a '$' (or 's'/'S' read for one) before an amount, 'blank'
    for one standing alone (an empty '$ -' cell), None inside a word."""
    c = s[i]
    if c not in "$sS":
        return None
    if c != "$":
        if i > 0 and s[i - 1].isalpha():
            return None
        if i + 1 < len(s) and s[i + 1].isalpha():
            return None
    j = i + 1
    while j < len(s) and s[j] == " ":
        j += 1
    k = j
    while k < len(s) and k - j < 3 and s[k] in "=~_\u2014|[]{}:;'\"":
        k += 1                       # '$ =1,171,929.00', '$ \u20142.000.00': debris before the digits
    for jj in (j, k):
        if jj < len(s) and (s[jj].isdigit() or (s[jj] in ".," and jj + 1 < len(s)
                                                  and s[jj + 1].isdigit())):
            return "cur"
    return "blank"


def _scan_run(s, i):
    """End of the numeric run starting at i.  A single space is crossed only
    beside a separator ('41 ,500', '5, 170', '11 .40') or before a 't' read
    for ',' ('201 t600'); any other space ends the number, which is what keeps
    two adjacent columns ('12.00 17,100.00') apart."""
    n, j = len(s), i
    while j < n:
        c = s[j]
        if c.isdigit() or c in ",.*":
            j += 1
            continue
        if c == "t" and j > i and s[j - 1].isdigit() and j + 1 < n and s[j + 1].isdigit():
            j += 1
            continue
        if c == " " and j + 1 < n and j > i:
            prev, nxt = s[j - 1], s[j + 1]
            if prev in ",." and nxt.isdigit():
                j += 1
                continue
            if prev.isdigit() and nxt in ",." and j + 2 < n and s[j + 2].isdigit():
                j += 1
                continue
            if prev.isdigit() and nxt == "t" and j + 2 < n and s[j + 2].isdigit():
                j += 2
                continue
        break
    while j > i and s[j - 1] in ",.* ":
        j -= 1
    return j


def scan(line):
    """Tokens of one (deoh'd) line, in order."""
    s, n, i, out = line, len(line), 0, []
    while i < n:
        c = s[i]
        if c == " ":
            i += 1
            continue
        m = _NOBID.match(s, i)
        if m and (i == 0 or not s[i - 1].isalpha()):
            out.append(Tok("nobid", i, m.end(), m.group(0)))
            i = m.end()
            continue
        dk = _dollar_kind(s, i)
        if dk:
            out.append(Tok(dk, i, i + 1, c))
            i += 1
            continue
        if c.isdigit() or (c in ".," and i + 1 < n and s[i + 1].isdigit()):
            j = _scan_run(s, i)
            if j <= i:
                j = i + 1
            t = s[i:j]
            alts = (segmentations(t) if (re.search(r"\.\s?\d\d", t)
                                         or re.search(r"^[\d,]*\d,\d\d$", t)) else [])
            if alts and not any(all(v is None for v in alt) for alt in alts):
                out.append(Tok("amt", i, j, t, alts=alts))
            elif alts:
                out.append(Tok("bad", i, j, t))
            else:
                v = _plain_number(t)
                out.append(Tok("num", i, j, t, val=v) if v is not None
                           else Tok("bad", i, j, t))
            i = j
            continue
        if c == "-" and (i + 1 == n or s[i + 1] in " -") and (i == 0 or s[i - 1] == " "):
            j = i
            while j < n and s[j] == "-":
                j += 1
            out.append(Tok("dash", i, j, s[i:j]))
            i = j
            continue
        m = re.compile(r"[^\s\d$]+").match(s, i)
        j = m.end() if m else i + 1
        w = s[i:j]
        out.append(Tok("word" if sum(ch.isalpha() for ch in w) >= 1 else "junk", i, j, w))
        i = j
    # A 1-2 digit group a space before an amount may be that amount's lost
    # leading digits: '1 18,430.00' = 118,430.00.  Offered as an extra
    # candidate value of the amount; the number itself stays a separate
    # token so it can still be read as the quantity.
    for a, b in zip(out, out[1:]):
        if (a.kind == "num" and re.fullmatch(r"\d{1,2}", a.text) and b.kind == "amt"
                and b.a == a.b + 1):
            m = re.match(r"(\d{1,3})\s?[,.]\s?\d{3}", b.text)
            if m and len(a.text) + len(m.group(1)) <= 3:
                b.join = a.text
    return out


# --------------------------------------------------------- cells and readings

class Cell:
    __slots__ = ("kind", "vals", "col")

    def __init__(self, kind, vals=(), col=0):
        self.kind, self.vals, self.col = kind, tuple(sorted(vals)), col

    def __repr__(self):
        return "%s%s" % (self.kind, list(self.vals) if self.vals else "")


def _units(toks):
    """The price region as units, each a list of alternative cell lists."""
    units = []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.kind == "cur":
            j = k + 1
            while j < len(toks) and toks[j].kind == "junk" and toks[j].a - toks[j - 1].b <= 2:
                j += 1                       # '$ =1,171,929.00': skip the debris
            if j > k + 1 and j < len(toks) and toks[j].kind in ("amt", "bad", "num"):
                k = j - 1
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if nxt is None or nxt.kind not in ("amt", "bad", "num"):
                units.append([[Cell("blank", col=t.a)]])
            elif nxt.kind == "num":          # '$ 1,000' without cents: a cell, not a qty
                units.append([[Cell("num", [nxt.val], col=t.a)]])
                k += 1
            k += 1
            continue
        if t.kind == "amt":
            alts = []
            for alt in t.alts:
                cells = []
                for ci, vals in enumerate(alt):
                    if vals is None:
                        cells.append(Cell("bad", col=t.a))
                        continue
                    vv = set(vals)
                    if ci == 0 and t.join:
                        # only the FIRST value of the run can carry the lost
                        # leading group, and only when it alone is that value
                        first = t.text if len(alt) == 1 else None
                        joined = seg_values(t.join + first.strip()) if first else None
                        if joined:
                            vv |= joined
                    cells.append(Cell("num", vv, col=t.a))
                alts.append(cells)
            units.append(alts)
        elif t.kind == "bad":
            units.append([[Cell("bad", col=t.a)]])
        elif t.kind == "blank":
            units.append([[Cell("blank", col=t.a)]])
        elif t.kind == "dash":
            prev = toks[k - 1] if k else None
            if not (prev is not None and prev.kind == "blank" and t.a - prev.b <= 3):
                units.append([[Cell("blank", col=t.a)]])
        elif t.kind == "nobid":
            units.append([[Cell("nobid", col=t.a)]])
        # num (a bare integer, no cents) and junk/word are not cells
        k += 1
    return units


def readings(units, cap=MAX_READINGS):
    """Cartesian product of the units' alternatives -> list of cell lists."""
    out = []
    for combo in itertools.product(*units):
        out.append([c for part in combo for c in part])
        if len(out) >= cap:
            break
    return out


def pair_readings(cu, ce, q):
    """(u, e) readings of a unit-price cell and a total cell with q*u = e."""
    if cu.kind != "num" or ce.kind != "num":
        return []
    out = []
    for u in cu.vals:
        if u <= 0:
            continue
        for e in ce.vals:
            if abs(q * u - e) <= TOL * max(abs(e), 1.0):
                out.append((u, e))
    return out


def align(cells, n_slots, q):
    """Best ordered assignments of a row's cells to its bidder columns.

    Each column takes 0, 1 or 2 consecutive cells (unit price, total); a cell
    may be skipped at a cost.  The score is (columns whose pair balances,
    -cells skipped).  Returns (score, kept, any_bal, example, n_paths):
      kept    {slot: (u, e)} -- on EVERY optimal alignment, same reading
      any_bal slots that balance on SOME optimal alignment
      example one optimal alignment, {slot: [cell indices]}
    kept is None when the optimal alignments could not all be enumerated.
    """
    m = len(cells)
    bal = {}
    for i in range(m - 1):
        prs = pair_readings(cells[i], cells[i + 1], q)
        us = {u for u, _ in prs}
        if len(us) == 1:
            u = prs[0][0]
            e = min((e for uu, e in prs if uu == u), key=lambda e: abs(q * u - e))
            bal[i] = (u, e)
        elif len(us) > 1:
            bal[i] = "ambiguous"
    memo = {}

    def best(i, k):
        key = (i, k)
        if key in memo:
            return memo[key]
        if k == n_slots:
            r = (0, -(m - i))
        elif i == m:
            r = (0, 0)
        else:
            s1, k1 = best(i + 1, k)
            cands = [(s1, k1 - 1), best(i, k + 1), best(i + 1, k + 1)]
            if i + 1 < m:
                s2, k2 = best(i + 2, k + 1)
                cands.append((s2 + (1 if isinstance(bal.get(i), tuple) else 0), k2))
            r = max(cands)
        memo[key] = r
        return r

    target = best(0, 0)
    sols, example, n_paths = [], None, [0]

    def walk(i, k, acc, assign):
        if n_paths[0] > MAX_ALIGNMENTS:
            raise OverflowError
        b = best(i, k)
        if k == n_slots or i == m:
            n_paths[0] += 1
            sols.append(dict(acc))
            nonlocal example
            if example is None:
                example = {kk: list(v) for kk, v in assign.items()}
                example["_bal"] = sorted(kk for kk, _ in acc)
            return
        s1, k1 = best(i + 1, k)
        if (s1, k1 - 1) == b:
            walk(i + 1, k, acc, assign)
        if best(i, k + 1) == b:
            walk(i, k + 1, acc, {**assign, k: []})
        if best(i + 1, k + 1) == b:
            walk(i + 1, k + 1, acc, {**assign, k: [i]})
        if i + 1 < m:
            s2, k2 = best(i + 2, k + 1)
            ok = isinstance(bal.get(i), tuple)
            if (s2 + (1 if ok else 0), k2) == b:
                walk(i + 2, k + 1, acc + ([(k, bal[i])] if ok else []),
                     {**assign, k: [i, i + 1]})

    try:
        walk(0, 0, [], {})
    except OverflowError:
        return target, None, set(), example, n_paths[0]
    kept = dict(sols[0]) if sols else {}
    any_bal = set()
    for s in sols:
        any_bal |= set(s)
        for k in list(kept):
            if s.get(k) != kept[k]:
                del kept[k]
    return target, kept, any_bal, example, n_paths[0]


# ---------------------------------------------------------- the left text

# Unit spellings: parse_bidtabs' vocabulary plus the dotted and spelled-out
# forms these tabs print ('L.S.', 'S.Y.', 'Ea.', 'Cubic Yards', 'Face FT').
# Matched only where a unit can stand -- beside the quantity -- never inside
# a description.
_UNIT_KEYS = sorted({k.strip() for k in pb.UNIT_MAP if len(k.strip()) >= 2}
                    | {"l.s", "s.y", "c.y", "l.f", "s.f", "ea", "face ft", "cubic yards",
                       "cubic yard", "square yards", "square feet", "linear feet", "tons",
                       "lumpsum", "lump sum", "each", "job"}, key=len, reverse=True)
_UNIT_ALT = "|".join(re.escape(k).replace(r"\ ", " ").replace(" ", r"\s*")
                     .replace(r"\.", r"\.\s?") for k in _UNIT_KEYS)
# a unit is followed by a space, the end, punctuation, an UPPERCASE letter
# (glued: 'S.Y.Remove', 'Cubic YardsTopsoil') or a lone 's' read for '$'.
_UNIT_AT = re.compile(r"(?i:(?P<u>" + _UNIT_ALT + r"))(?:\.|(?=$|[\s,;:)(\"'$]|[A-Z]|s(?![a-z])))")
# Units as OCR misreads them: F's foot lost (LF -> 'LE', VF -> 'VE':
# Midlothian's 2023-24 tab prints 'VE' for every vertical foot), S read as
# 5, Y as V.  Accepted only where a unit can stand -- right after the
# quantity -- and filed under the unit they stand for.
_OCR_UNIT = {"le": "LF", "l.e": "LF", "l.e.": "LF", "ve": "VF", "v.e": "VF", "v.e.": "VF",
             "5y": "SY", "5.y": "SY", "5.y.": "SY", "sv": "SY", "s.v": "SY", "s.v.": "SY",
             "5f": "SF", "5.f": "SF", "5.f.": "SF"}
_OCR_UNIT_AT = re.compile(r"(?i:(?P<u>" + "|".join(
    re.escape(k).replace(r"\.", r"\.\s?") for k in sorted(_OCR_UNIT, key=len, reverse=True))
    + r"))(?=$|[\s,;:)(\"'$|\]])")
# what may stand between a quantity and its unit: spaces, a cell rule read
# as '|' or '[', the period OCR puts after a number ('2. EA')
_UNIT_GAP = re.compile(r"[.\s|\[\]{}]*")
_LUMP_WORDS = re.compile(r"(?i)\blump\s*sum\b")
_QTY_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?!\d)")
_ITEM_CLEAN = re.compile(r"#?(\d{1,4})\.?")
_ITEM_DASHED = re.compile(r"#?\d{1,2}-\d{1,3}|\d{1,2}\.\d{1,3}")  # Frisco '#0-2'; Tarrant '1.10'
_ITEM_ALPHA = re.compile(r"([A-Z])-?(\d{1,3})")
_ITEM_OCR = re.compile(r"[IlO0-9]{1,3}\.?")
_ITEM_GLUED = re.compile(r"(\d{1,5})\.?(?=[A-Za-z\"'(\-])")
_ITEM_OCR_GLUED = re.compile(r"([Il]{1,2}|[Il][0O]|\d[Il]|[Il]\d)(?=[A-Z])")


def _ocr_int(t):
    t = t.rstrip(".").replace("I", "1").replace("l", "1").replace("O", "0")
    return int(t) if t.isdigit() else None


_ITEM_ALPHA_OCR = re.compile(r"([A-Z])-?([0-9IilLtT|SsOo]{1,3})")
_ITEM_ALPHA_OCR_GLUED = re.compile(r"([A-Z])-?([0-9IilLtT|SsOo]{1,3})(?=[A-Z][a-z])")
_DIGIT_FOR = {"i": "1", "I": "1", "l": "1", "L": "1", "t": "1", "T": "1", "|": "1",
              "S": "5", "s": "5", "O": "0", "o": "0"}


def _alpha_ocr_readings(t):
    """Numbers an OCR'd item suffix can stand for: each confusable letter
    read as its digit, or as debris and dropped ('1i3' -> 113 or 13)."""
    outs = [""]
    for ch in t:
        if ch.isdigit():
            outs = [o + ch for o in outs]
        else:
            outs = [o + _DIGIT_FOR[ch] for o in outs] + [o for o in outs]
    seen = []
    for o in outs:
        if o and o.isdigit() and int(o) not in seen:
            seen.append(int(o))
    return seen


def item_candidates(left, prev, max_col=None, prev_alpha=None):
    """Readings of the item number at the start of the line, best first, as
    (item_no, rest_of_left, how).  The last reading is always 'no item'.
    An item number sits at the left edge: a first token further right than
    max_col is another column (Tarrant's TxDOT spec '462' when OCR lost the
    item number '101')."""
    m = re.match(r"(\s*)(\S+)", left)
    out = []
    if m and max_col is not None and m.start(2) > max_col:
        m = None
    if m and _ITEM_DASHED.fullmatch(m.group(2)):
        tok, start, end = m.group(2), m.start(2), m.end(2)
        rest = left[:start] + " " * len(tok) + left[end:]
        return [(tok.lstrip("#"), rest, "dashed"), ("", left, "none")]
    if m:
        tok, start, end = m.group(2), m.start(2), m.end(2)
        # a table rule OCR glued to the number ('201|Remove', '224}Pavement')
        mr = re.match(r"([A-Za-z]?-?\d{1,4}\.?)_*[|\[\]{}]", tok)
        if mr:
            tok, end = mr.group(1), start + len(mr.group(1))
        rest = left[:start] + " " * len(tok) + left[end:]
        tok = re.sub(r"^\u00a7", "5", tok)          # '\u00a708' = 508
        if re.fullmatch(r"\d{1,4}[A-Z]", tok):     # '5A': an alternate's copy of item 5
            return [(tok, rest, "suffixed"), ("", left, "none")]
        mc = _ITEM_CLEAN.fullmatch(tok)
        ma = _ITEM_ALPHA.fullmatch(tok)
        if mc:
            v = str(int(mc.group(1)))
            # a number that breaks the sequence where one misread digit
            # would continue it ('4106' after 105, '407' after 106)
            if prev is not None and int(v) != prev + 1:
                for alt in (v[1:], "1" + v[1:] if v[:1] in "47" else ""):
                    if alt and alt.isdigit() and int(alt) == prev + 1:
                        v = alt
                        break
            out.append((v, rest, "clean"))
        elif ma:
            v = ma.group(2)
            if prev_alpha and prev_alpha[0] == ma.group(1) and not (
                    prev_alpha[1] < int(v) <= prev_alpha[1] + 3):
                # 'F141' after F10: one misread digit too many ('F11')
                for k in range(len(v)):
                    alt = v[:k] + v[k + 1:]
                    if alt.isdigit() and int(alt) == prev_alpha[1] + 1:
                        v = alt
                        break
            out.append((ma.group(1) + str(int(v)), rest, "alpha"))
        elif _ITEM_ALPHA_OCR.fullmatch(tok) and prev_alpha:
            # an item code OCR read with letters for digits ('Di4', 'D1i3',
            # 'Ett', 'FS'): taken only where one reading continues the
            # section's numbering (its next number, or up to 2 rows lost)
            mo = _ITEM_ALPHA_OCR.fullmatch(tok)
            if mo.group(1) == prev_alpha[0]:
                for v in _alpha_ocr_readings(mo.group(2)):
                    if prev_alpha[1] < v <= prev_alpha[1] + 3:
                        out.append((mo.group(1) + str(v), rest, "alpha_ocr"))
                        break
        elif _ITEM_OCR.fullmatch(tok) and re.search(r"[IlO]", tok):
            v = _ocr_int(tok)
            if v is not None and (v == (prev or 0) + 1):
                out.append((str(v), rest, "ocr_letters"))
        else:
            mg = _ITEM_GLUED.match(tok)
            mo = _ITEM_OCR_GLUED.match(tok)
            mq = _ITEM_ALPHA_OCR_GLUED.match(tok) if prev_alpha else None
            if mq and mq.group(1) == prev_alpha[0]:
                # 'F1i0Connect': an OCR'd item code glued to its description
                for v in _alpha_ocr_readings(mq.group(2)):
                    if prev_alpha[1] < v <= prev_alpha[1] + 3:
                        k = mq.end()
                        rest = left[:start] + " " * k + left[start + k:]
                        out.append((mq.group(1) + str(v), rest, "alpha_ocr_glued"))
                        break
            elif mg:
                d, nxt = mg.group(1), tok[mg.end():mg.end() + 1]
                if tok[len(d):len(d) + 1] == ".":
                    nxt = "A"                    # '3.Structure': a period ends the number
                if nxt.isalpha() or nxt == "(":
                    pick = d if len(d) <= 4 else None
                else:
                    # digits glued to a description that itself opens with a
                    # number ('124" DR18 ...' = item 1, 24"): the item number
                    # is the prefix that continues the sequence.
                    pick = None
                    cands = [d[:k] for k in range(1, len(d)) if not (k > 1 and d[0] == "0")]
                    up = [c for c in cands if int(c) > (prev or 0)]
                    if up:
                        pick = min(up, key=int)
                if pick:
                    k = len(pick) + (1 if tok[len(pick):len(pick) + 1] == "." else 0)
                    rest = left[:start] + " " * k + left[start + k:]
                    out.append((str(int(pick)), rest, "glued"))
            elif mo:
                v = _ocr_int(mo.group(1))
                if v is not None and v == (prev or 0) + 1:
                    k = len(mo.group(1))
                    rest = left[:start] + " " * k + left[start + k:]
                    out.append((str(v), rest, "ocr_glued"))
    out.append(("", left, "none"))
    return out


_QTY_GLUED = re.compile(r"(?<=[A-Za-z])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?=\s*(?i:" + _UNIT_ALT
                        + r")(?:\.|\b))")
_QTY_AS_LETTER = re.compile(r"(?<![\w.])[iIl|](?=\s+(?i:" + _UNIT_ALT + r")(?:\.|\b))")


def qty_candidates(body):
    """(value, start, end) for every number in the row's body text, plus a
    number OCR glued to the word before its unit ('plans2   EA',
    'Pipe3,364 LF') and a '1' read as a letter before a unit ('i   EA').
    Every one is only a candidate: the row's arithmetic picks."""
    out = []
    for m in list(_QTY_NUM.finditer(body)) + list(_QTY_GLUED.finditer(body)):
        try:
            v = float(m.group(1).replace(",", "") + (m.group(2) or ""))
        except ValueError:
            continue
        if v > 0 and (v, m.start(), m.end()) not in out:
            out.append((v, m.start(), m.end()))
    for m in _QTY_AS_LETTER.finditer(body):
        out.append((1.0, m.start(), m.end()))
    return out


def unit_near(body, a, b):
    """(unit_raw, start, end) of the unit printed right after a quantity at
    [a, b) -- glued ('3,536S.Y.', '16EA'), spaced ('1   LS'), behind a cell
    rule or a period ('120 |Hour', '2. EA'), or in an OCR misreading of it
    ('6 VE', returned as 'VF') -- or an UPPERCASE unit code right before it
    ('PROJECTSIGN EA 2'); never a word before it ('Remove and Reset Post
    Mail Box  78' is no unit)."""
    if b is not None:
        g = _UNIT_GAP.match(body, b).end()
        for at in ((b, g) if g != b else (b,)):
            m = _UNIT_AT.match(body, at)
            if m and re.fullmatch(r"[A-Za-z]{3,}", m.group("u")) and \
                    re.match(r"[A-Z]", body[m.end():m.end() + 1]):
                m = None                 # 'HEAD' of '4 HEADWALL' is no unit
            if m:
                return m.group(0).strip(), m.start(), m.end()
        m = _OCR_UNIT_AT.match(body, g)
        if m:
            return _OCR_UNIT[re.sub(r"\s", "", m.group("u").lower())], m.start(), m.end()
    if a is not None:
        mb = re.search(r"(?<![A-Za-z.])(LS|EA|LF|SY|SF|CY|CF|TON|TN|AC|MO|HR|GAL|VF|DAY|MG)\.?\s+$",
                       body[:a])
        if mb:                           # an UPPERCASE code only: 'Mail Box  78' stays
            return mb.group(1), mb.start(1), mb.end(1)
    return "", None, None


def _strip_dollar_s(u):
    """'FTs' -> 'FT' (the 's' is the next cell's '$'); 'Ls', 'Tons' stay."""
    u = (u or "").strip()
    if (u.endswith("s") and len(u) > 1 and pb.norm_unit(u) not in pb.KNOWN_UNITS
            and pb.norm_unit(u[:-1]) in pb.KNOWN_UNITS | {"FACE FT"}):
        return u[:-1]
    return u


def trailing_unit(body):
    """A unit standing alone at the end of the body ('Lump Sum LS', 'EA')."""
    m = re.search(r"(?i:(?P<u>" + _UNIT_ALT + r"))\.?s?\s*$", body)
    if m and (m.start() == 0 or not body[m.start() - 1].isalpha()):
        return _strip_dollar_s(m.group(0)), m.start(), m.end()
    return "", None, None


def clean_desc(body, spans):
    """The body with the quantity/unit spans blanked, whitespace collapsed."""
    chars = list(body)
    for a, b in spans:
        if a is None:
            continue
        for k in range(a, min(b, len(chars))):
            chars[k] = " "
    d = pb.txt("".join(chars))
    d = re.sub(r"(?:\s+[sS$|])+$", "", d)               # a '$' read as a lone 's'
    d = re.sub(r"^(?:(?:[_|\[\]{}]+\s*)+|j(?=[A-Z]))", "", d)  # a cell rule read as a character
    d = re.sub(r"(?<=\d)(\s*-?\s*)[l1](?=nch)", r"\1I", d)  # '6-lnches', '5-1nch' = Inch
    return d.strip(" ;:|")


# ----------------------------------------------------------- one item row

class RowParse:
    """The best reading of one item line."""
    __slots__ = ("line_no", "item_no", "desc", "unit_raw", "qty", "qty_source", "n_slots",
                 "kept", "any_bal", "example", "cells", "score", "how", "overflow",
                 "money_col", "desc_col", "section", "page", "continuation_of", "note",
                 "had_qty", "unit_col", "qty_span", "unit_how", "line_text", "template_row",
                 "desc_conf")

    def __init__(self):
        for k in self.__slots__:
            setattr(self, k, None)


def _is_size(toks, k):
    """A number with an inch or foot mark glued to it ('36"', "4'") is a size
    in the description, never a quantity or a price."""
    t = toks[k]
    return (t.kind == "num" and k + 1 < len(toks) and toks[k + 1].a == t.b
            and toks[k + 1].text[:1] in ('"', "'", "\u201d", "\u2019"))


def _region_start(toks):
    """Index of the first token of the trailing price region: everything
    after the last real word (>= 3 letters, or a unit, or a total word) --
    or the last size ('36" TO 48"' ends a description, not its prices)."""
    last = -1
    for k, t in enumerate(toks):
        if t.kind == "word":
            letters = sum(ch.isalpha() for ch in t.text)
            if letters >= 3 or _UNIT_AT.fullmatch(t.text.strip()) or re.fullmatch(
                    r"(?i)[a-z]{2}\.?", t.text.strip()) and t.text.strip().upper() in pb.KNOWN_UNITS \
                    or t.text.strip().lower() in _OCR_UNIT:
                last = k
        elif _is_size(toks, k):
            last = k + 1                       # the number and its mark
    return last + 1


def _implied_configs(configs, n_slots):
    """Readings with the quantity IMPLIED by the row's own columns (opt-in,
    --implied-qty).  When no printed quantity balances a row, but two or
    more bidders' (unit price, total) pairs extend on one quantity q* to the
    cent, q* is offered as a reading.  It must beat every printed reading,
    balance at least two columns, and be the only such q*.  The cells it
    keeps are then proved by agreement BETWEEN bidders, not by a printed
    quantity, and are marked qty_source='implied_by_columns'."""
    best = max(c[0] for c in configs)
    seen_q = {c[4] for c in configs}
    out = []
    by_split = {}
    for c in configs:
        by_split.setdefault((c[12], c[1]), c)
    for (_body_end, _item), c in by_split.items():
        cells = c[8]
        support = defaultdict(set)
        for i in range(len(cells) - 1):
            a, b = cells[i], cells[i + 1]
            if a.kind != "num" or b.kind != "num":
                continue
            for u in a.vals:
                for e in b.vals:
                    if u > 0 and e > 0:
                        q = round(e / u, 2)
                        if q > 0 and abs(q * u - e) <= 0.011:
                            support[q].add(i)
        cands = [q for q, ii in support.items()
                 if len([i for i in ii if i - 1 not in ii]) >= 2 and q not in seen_q]
        scored = []
        for q in cands:
            score, kept, any_bal, example, _n = align(cells, n_slots, q)
            if score[0] >= 2 and score > best:
                scored.append((score, q, kept, any_bal, example))
        if scored:
            topq = max(x[0] for x in scored)
            win = [x for x in scored if x[0] == topq]
            if len(win) == 1:
                score, q, kept, any_bal, example = win[0]
                out.append((score, c[1], c[2], c[3], q, None, None, "implied_by_columns",
                            cells, kept, any_bal, example, c[12]))
    return out


def parse_item_line(line, n_slots, prev_item, fixed=None, implied=False, prev_alpha=None):
    """Read one line as an item row with n_slots bidder columns.

    fixed: for a continuation page, (qty,) taken from the item page -- the
    line then carries prices only.  Returns a RowParse or None when the line
    has no price cells at all.
    """
    s = deoh(line)
    toks = scan(s)
    r0 = _region_start(toks) if fixed is None else 0
    while r0 < len(toks) and toks[r0].kind in ("word", "junk"):
        r0 += 1                      # a 2-letter fragment ('Sub de') stays in the body
    region_toks = toks[r0:]
    if not any(t.kind in _CELL_KINDS for t in region_toks):
        return None
    # The region may open with the quantity itself ('Grading 1425 12.00 ...')
    # or an amount-shaped quantity ('0.50 AC' with the unit lost), so the cut
    # between body and prices is tried at the first few tokens of the region.
    splits = [r0]
    for k in range(r0, min(len(toks), r0 + 3)):
        if toks[k].kind in ("num", "amt"):
            splits.append(k + 1)
    best = None
    configs = []
    for sp in sorted(set(splits)):
        body_end = toks[sp].a if sp < len(toks) else len(s)
        body = s[:body_end]
        units = _units(toks[sp:])
        if not units:
            continue
        rds = readings(units)
        if fixed is not None:
            item_opts = [("", body, "none")]
        else:
            mc = _first_cell_col(toks, r0)
            item_opts = item_candidates(body, prev_item, prev_alpha=prev_alpha,
                                        max_col=max(12, int(0.4 * mc)) if mc else None)
        for item_no, rest, how in item_opts:
            if fixed is not None:
                qcands = [(fixed[0], None, None, "template")]
            else:
                qcands = [(v, a, b, "") for v, a, b in qty_candidates(rest)]
                if sp > r0:
                    # a later cut only exists to let the region's first
                    # number be the quantity; with any other quantity it is
                    # the same reading with price cells thrown away
                    lo = toks[r0].a
                    qcands = [c for c in qcands if c[1] is not None and c[1] >= lo]
                tu = trailing_unit(rest)[0] or ""
                if not qcands or pb.norm_unit(tu) == "LS" or _LUMP_WORDS.search(rest):
                    if pb.norm_unit(tu) == "LS" or _LUMP_WORDS.search(rest):
                        qcands.append((1.0, None, None,
                                       "lump_sum_words" if _LUMP_WORDS.search(rest) else "lump_sum_implied"))
            for q, qa, qb, qsrc in qcands:
                for cells in rds:
                    score, kept, any_bal, example, npaths = align(cells, n_slots, q)
                    configs.append((score, item_no, rest, how, q, qa, qb, qsrc, cells,
                                    kept, any_bal, example, body_end))
    if not configs:
        return None
    if implied and fixed is None:
        configs += _implied_configs(configs, n_slots)
    top = max(c[0] for c in configs)
    # Prefer a reading that has an item number, then one whose quantity sits
    # beside a unit -- but only among readings that balance equally well.
    tied = [c for c in configs if c[0] == top]

    mc0 = _first_cell_col(toks, r0)

    def pref(c):
        has_item = 1 if c[1] else 0
        unit_ok = 1 if (c[5] is not None and unit_near(c[2], c[5], c[6])[0]) else 0
        # the same quantity twice ('5 HEADWALL, 2 BARRELL 36" TO 48"  2  $12,000.00'):
        # the one in the quantity column, with no number between it and the
        # prices, is the quantity; the other is part of the description
        last = 1 if (c[6] is not None and mc0 is not None
                     and not re.search(r"\d", s[c[6]:mc0])) else 0
        return (has_item, unit_ok, last, -c[12])
    tied.sort(key=pref, reverse=True)
    lead = tied[0]
    # A cell is kept only when every equally good reading keeps it, with the
    # same values: a quantity or a tokenisation that changes the answer is a
    # reading the arithmetic could not settle.
    overflow = any(c[9] is None for c in tied)
    kept = None if overflow else dict(lead[9])
    any_bal = set()
    for c in tied:
        any_bal |= set(c[10] or ())
        if kept is not None:
            for k in list(kept):
                if (c[9] or {}).get(k) != kept[k]:
                    del kept[k]
    rp = RowParse()
    (score, item_no, rest, how, q, qa, qb, qsrc, cells, _k, _ab, example, body_end) = lead
    rp.score, rp.item_no, rp.how, rp.qty, rp.cells = score, item_no, how, q, cells
    rp.kept = kept or {}
    rp.overflow = overflow
    rp.any_bal = any_bal
    rp.example = example or {}
    rp.n_slots = n_slots
    rp.money_col = _first_cell_col(toks, r0)
    if rp.money_col is None:
        rp.money_col = len(s)
    rp.qty_source = qsrc if qsrc not in ("", "template") else ""
    rp.had_qty = (qa is not None) or qsrc in ("lump_sum_words", "lump_sum_implied", "template",
                                              "implied_by_columns")
    if fixed is None:
        if qa is not None:
            unit_raw, ua, ub = unit_near(rest, qa, qb)
        else:
            unit_raw, ua, ub = trailing_unit(rest)
        spans = [(qa, qb), (ua, ub)]
        if qsrc == "lump_sum_implied" and ua is not None:
            mq = re.search(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s*$", rest[:ua])
            if mq:
                spans.append((mq.start(), ua))
        if qsrc == "lump_sum_words":
            for mm in _LUMP_WORDS.finditer(rest):
                spans.append(mm.span())
            if not unit_raw:
                unit_raw = "Lump Sum"
        unit_raw = _strip_dollar_s(unit_raw)             # 'FTs': the 's' is the next '$'
        rp.unit_raw = unit_raw
        rp.unit_col = ua
        rp.qty_span = (qa, qb)
        rp.line_text = s
        if unit_raw and ua is not None and pb.norm_unit(rest[ua:ub]) != pb.norm_unit(unit_raw):
            rp.unit_how = "ocr_confusion"           # 'VE' read, 'VF' filed
        rp.desc = clean_desc(rest, spans)
        mb = re.search(r"\S", rest)
        rp.desc_col = mb.start() if mb else None
        mi = re.match(r"\s*\S+", body) if item_no else None
        if mi and item_no:
            rp.desc_col = mi.end() + 1          # wrapped lines start right of the item number
        # a code column printed where the row's own description would be
        # (Tarrant's TxDOT spec '462') is not a description
        if re.fullmatch(r"[\d\s.,#-]*", rp.desc or ""):
            rp.desc = ""
    return rp


# -------------------------------------------------------------- page layout

_SECTION_X = re.compile(r"^\s*SECTION\s+[\"']?([A-Z0-9]{1,3})[\"']?\b", re.I)
_SUBTOTAL_ONLY = re.compile(r"(?i)^\W*sub[\s-]*total\W*$")
_ALT_AFTER_TOTAL = re.compile(r"(?i)^\W*(?:add(?:itive)?\s*|deduct(?:ive)?\s*|bid\s*)?alternate")
_TOTAL_ONLY = re.compile(r"(?i)^\W*total\W*$")
_NOT_MONEY_TOTAL = re.compile(r"(?i)time|days?\b|calend[ae]r|working|in\s*words")


def unglue_total(s):
    s = re.sub(r"(?i)(total)(?=[a-z])", r"\1 ", s)
    s = re.sub(r"(?i)(?<=[a-z])(total)", r" \1", s)
    s = re.sub(r"(?i)(alternate)(?=\d)", r"\1 ", s)
    s = re.sub(r"(?i)(?<=\d)(bid)", r" \1", s)
    return s


def total_caption(left):
    """The total caption of a line's left text, or ''."""
    t = pb.txt(unglue_total(left))
    if not t or _NOT_MONEY_TOTAL.search(t):
        return ""
    if pb.is_total_caption(t) or pb.AGG_ITEM_RE.match(t):
        return t
    return ""


def unglue_words(t):
    """'AddAlternateEast parkway' -> 'Add Alternate East parkway': OCR drops
    the spaces of small type, and section roles are judged on words."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t or "")


def banner_of(text):
    """A section banner on a price-less line: parse_bidtabs' banner shape,
    or 'SECTION A' (NRH)."""
    t = unglue_words(pb.txt(text))
    if not t:
        return ""
    b = pb.section_banner(t)
    if b:
        return b
    m = _SECTION_X.match(t)
    if m:
        return "SECTION " + m.group(1).upper()
    return ""


# Captions OCR glues together ('UNIT COSTTOTAL', 'Unit Bid PriceAmount Bid',
# 'EXTENDEDPRICEUNITPRICE', 'BIDOF') are word-broken against the caption
# vocabulary: a chunk made only of caption words names no firm.
_CAP_VOCAB = set(pb._CAPTION_WORDS) | {"bid", "bids", "of", "bidder", "bidders", "extension",
                                       "extended", "measure", "est", "uom", "approximate",
                                       "approx", "tabulation", "report", "estimate", "engineers",
                                       "engineer", "opening", "date", "time", "base", "alternate",
                                       "add", "deduct", "sheet", "item", "items", "no", "number"}


def _caption_glued(t):
    letters = re.sub(r"[^a-z]", "", t.lower())
    if not letters or len(letters) > 80:
        return False
    ok = [True] + [False] * len(letters)
    for i in range(1, len(letters) + 1):
        for j in range(max(0, i - 14), i):
            if ok[j] and letters[j:i] in _CAP_VOCAB:
                ok[i] = True
                break
    return ok[-1] and not pb.ENGINEER_RE.search(t)


_EMAIL = re.compile(r"@|\.com\b|www\.", re.I)
# Form fields printed in the bidder block ('CO-OP: Yes', 'HUB: No').
_FIELD_LABEL = re.compile(r"(?i)^\W*(?:co-?\s*op(?:erative)?s?|hub|dbe|mbe|wbe|sbe|"
                          r"bid\s*bond|addend(?:um|a)s?|vendor\s*(?:no|#|id)|"
                          r"contract\s*(?:no|#))\b\W*(?:[:\-]?\s*(?:yes|no|y|n|x))?\W*$")
_RANK_OFFER = re.compile(r"(?i)^\W*(?:the\s+)?(?:apparent\s+)?(?:(?:low(?:est)?|high(?:est)?|best|"
                         r"recommended|awarded|winning)\s+(?:bid|bidder|bids|offer|offeror|"
                         r"proposal|vendor|contractor)s?|(?:primary|secondary|recommended|apparent)?"
                         r"\s*(?:award(?:ed|ee)?|low\s*bidder))\W*$")
_DATEISH = re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d"
                      r"|\d{1,2}/\d{1,2}/\d{2,4}|\b\d{1,2}:\d{2}|\b\d{1,2}:[0O]{2}"
                      r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bdate\s*:|\btime\s*:")


_ADDRISH = re.compile(r"\bp\.?\s*o\.?\s*box\b|\b(?:suite|ste)\.?\s*#?\d"
                      r"|\d{1,6}\s?[A-Za-z][\w.'\-]*\s+(?:[\w.'\-]+\s+){0,3}"
                      + pb._ST_TYPE + r"\b", re.I)
_STATE_TAIL = re.compile(r",\s*(?:TX|Texas|OK|Oklahoma|AR|LA|NM|KS|MO|NE|Nebraska|WI|MN|AL|"
                         r"CO|AZ|CA|FL|GA|TN|MS|IL|OH|PA|NY|NC|SC|VA|UT|NV|IA)\b\.?(?:\s*\d{5})?")


def chunk_class(chunk):
    """'name' (could be part of a bidder's name), 'stop' (an address, e-mail,
    phone or owner line -- a contact block, not a name line) or 'ignore'
    (a date, time, amount or column caption, which may share a line with
    the names: Coppell prints 'BID OPENING TIME: 2:00 PM' left of them)."""
    t = pb.txt(chunk)
    if _EMAIL.search(t) or pb._PHONE_RE.search(t):
        return "stop"
    if pb.CITY_STATE_RE.match(t) or pb.OWNER_RE.match(t) or _STATE_TAIL.search(t):
        return "stop"
    if _ADDRISH.search(t) and not pb._FIRM_WORD_RE.search(pb.strip_address(t) or ""):
        return "stop"
    if pb.ADDRESS_RE.search(t) and not pb._FIRM_WORD_RE.search(pb.strip_address(t) or ""):
        return "stop"
    if sum(ch.isalpha() for ch in t) < 2 or _DATEISH.search(t):
        return "ignore"
    if re.search(r"\$|\d[\d,]*\.\d\d\b", t):
        return "ignore"
    if pb._caption_only(t) or _caption_glued(t) or _RANK_OFFER.match(t) or _FIELD_LABEL.match(t):
        return "ignore"
    if re.match(r"(?i)" + _FIELD_LABEL_AT, t):
        return "ignore"                 # 'Project: 2026 Annual Asphalt Rehabilitation Program'
    return "name" if pb.looks_like_bidder(t) else "ignore"


# Firm words glued to the word before them by OCR ('SYBConstruction',
# 'COLECONSTRUCTION,INC.') are re-spaced so the firm word is visible to the
# name split and to canon_bidder's alias table ('KIK Underground').
_GLUE_WORDS = ("construction", "constructors", "contracting", "contractors", "concrete",
               "infrastructure", "paving", "underground", "structures", "trucking", "company",
               "services", "landscaping", "utilities", "excavation", "solutions", "enterprises",
               "industries", "materials", "siteworks", "waterworks")
_NOT_PREFIX = {"sub", "pre", "non", "re", "de", "un", "re-", "co"}


def respace(name):
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        # a legal suffix glued on ('DONSMITHCONCRETELLC', 'RLMEARTHCOINC')
        for g in ("llc", "inc"):
            if lw.endswith(g) and len(lw) - len(g) >= 4 and w[-len(g):].isupper():
                return fix_word(w[:-len(g)]) + " " + w[-len(g):]
        return fix_word(w)

    def fix_word(w):
        lw = w.lower()
        for g in _GLUE_WORDS:
            if lw.endswith(g) and lw != g:
                pre = w[:-len(g)]
                if len(pre) >= 3 and pre.lower() not in _NOT_PREFIX:
                    return pre + " " + w[-len(g):]
        return w
    s = re.sub(r"[A-Za-z]+", fix, name)
    s = re.sub(r"\b(LLC|L\.L\.C\.|Inc\.?|INC\.?)(?=[A-Z][a-z])", r"\1 ", s)
    s = re.sub(r",(?=[A-Za-z])", ", ", s)
    s = re.sub(r"\s*&\s*", " & ", s)
    return pb.txt(s)


_CONNECTOR = re.compile(r"(?i)^(?:&|and|of|dba|d/b/a|the)$")

# Trade words that end firm names and that parse_bidtabs' _FIRM_WORD_RE does
# not list ('JMK Siteworks', 'HD Supply Waterworks', 'Ratliff Hardscape',
# 'Durable Specialties').  Used only here, to find and split OCR'd names.
_FIRM_EXTRA = re.compile(r"(?i)\b(?:siteworks?|waterworks|hardscapes?|specialties|"
                         r"telecommunications?|excavators?|welding|boring|drilling|"
                         r"dirtworks?|sitework)\b")


def is_firmish(t):
    return bool(pb._FIRM_WORD_RE.search(t or "") or _FIRM_EXTRA.search(t or "")
                or pb.ENGINEER_RE.search(t or ""))


def _firmword(w):
    w = w.strip(",.;:()")
    return bool(w) and bool(re.fullmatch(pb._FIRM_WORD_RE.pattern, w, re.I)
                            or _FIRM_EXTRA.fullmatch(w))


def split_packed(text, k):
    """All ways to cut a run of names into k firm names (see module doc)."""
    words = text.split()
    if k == 1:
        return [[text]]
    ok_cuts = [c for c in range(1, len(words))
               if _firmword(words[c - 1]) and not _firmword(words[c])
               and not _CONNECTOR.match(words[c]) and words[c][:1].isalnum()
               and (words[c][:1].isupper() or words[c][:1].isdigit())]
    out = []
    for cuts in itertools.combinations(ok_cuts, k - 1):
        segs, a = [], 0
        for c in list(cuts) + [len(words)]:
            segs.append(" ".join(words[a:c]))
            a = c
        if all(_firmword(sg.split()[-1]) and pb.looks_like_bidder(sg) for sg in segs):
            out.append(segs)
    return out


_LEGAL = re.compile(r"(?i)^(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|"
                    r"company|lp|l\.p|llp|pllc)$")


def _is_legal(w):
    return bool(_LEGAL.match(w.strip(",.;:()")))


def split_packed_best(text, k):
    """The one way to cut `text` into k firm names, or None.

    Of the cuts split_packed allows, those after a LEGAL suffix (LLC, Inc,
    Ltd, Co ...) are preferred: a legal suffix ends a firm's name, a trade
    word may sit inside one ('Pyramid Contracting of Texas, LLC').  The
    split must still be unique after that preference."""
    opts = split_packed(text, k)
    if not opts:
        return None
    def legal_cuts(segs):
        return sum(1 for sg in segs[:-1] if _is_legal(sg.split()[-1]))
    top = max(legal_cuts(o) for o in opts)
    best = [o for o in opts if legal_cuts(o) == top]
    return best[0] if len(best) == 1 else None


def valid_name(nm):
    """A string that can stand as a bidder's name (not a caption, a computed
    column, a lone firm word or OCR debris like 'Serle eee')."""
    t = pb.txt(nm)
    if not t or not pb.looks_like_bidder(t) or pb._caption_only(t) or _caption_glued(t):
        return False
    if (pb.PLACEHOLDER_RE.match(t) or pb.COMPUTED_COL_RE.match(t) or re.match(r"(?i)^\W*avg", t)
            or _FIELD_LABEL.match(t) or _RANK_OFFER.match(t)):
        return False
    if sum(ch.isalpha() for ch in t) < 3:
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&'.-]*", t)]
    distinct = [w for w in words if not _firmword(w) and not _CONNECTOR.match(w)
                and sum(ch.isalpha() for ch in w) >= 2]
    if (not distinct and len(re.findall(r"\b[A-Z]\b", t)) < 2 and not pb.ENGINEER_RE.search(t)
            and not re.search(r"\b(?:\d+[A-Z]+|[A-Z]+\d+)[A-Za-z\d]*\b", t)):
        return False                      # 'company', 'Construction LLC' -- but initials
                                          # name a firm ('A & B Construction, LLC', '5W
                                          # Contracting, LLC')
    if not re.search(r"[A-Z0-9]", t[:1]):
        return False                      # names open with a capital or a digit
    return True


def _names_credible(names, positional=False):
    """A page's names as a SET: every one valid, and at least half of them
    saying they are firms (or the engineer) -- a single bidder must say so.
    Names matched to their price columns by POSITION carry that placement
    as evidence of their own, so one firm-like name among them suffices
    (Kaufman's 'Beavers Contracting | ERS | Hanson Consruction')."""
    real = [n for n in names if n]
    if not real or not all(valid_name(n) for n in real):
        return False
    firmish = sum(1 for n in real if is_firmish(n))
    need = 1 if positional else max(1, (len(real) + 1) // 2)
    return firmish >= need


def _merge_same_column(chunks, prof):
    """Join chunks of one header line that OCR split inside a single name
    ('West  Texas Rebar Placers,', 'Humphrey  &Morton'): neighbours a few
    spaces apart that sit over the same price column, or a piece opening
    with '&'."""
    if not chunks:
        return chunks
    centers = {k: (a + b) / 2.0 for k, (a, b) in (prof or {}).items()}

    def slot_of(a, b):
        if not centers:
            return None
        c = (a + b) / 2.0
        return min(centers, key=lambda k: abs(centers[k] - c))
    out = [list(chunks[0])]
    for a, b, t in chunks[1:]:
        pa, pb_, pt = out[-1]
        gap = a - pb_
        same = (centers and slot_of(pa, pb_) == slot_of(a, b))
        # RapidOCR spreads one name's words across its cell ('Jackson
        # Construction,   Ltd.'): over the same price column, a wider gap
        if (same and gap <= 6) or (gap <= 3 and t.startswith("&")):
            out[-1] = [pa, b, pt + " " + t]
        else:
            out.append([a, b, t])
    return [tuple(x) for x in out]


_FIELD_LABEL_AT = r"(?:Project|Owner|Job|Contract|Location|Engineer)\s*(?:Name|No\.?|#|Title)?\s*:"
_LABEL_CUT = re.compile(r"\s(?=" + _FIELD_LABEL_AT + ")", re.I)
_PROJECT_LINE = re.compile(r"(?i)\b(?:project|contract|bid|solicitation|rfb|ifb|rfp|rfq|itb)\s*"
                           r"(?:no\.?|#|number)\s*:?\s*[\w#.-]*\d")


def name_band(lines, price_col, prof=None):
    """The band of header lines that print the bidder names over the price
    columns: the lowest line holding a firm-word name, plus the name lines
    directly above it (a name wraps upward: 'GARY SHIELDS' over
    'INFRASTRUCTURE, LLC') and at most one below; at most three lines.
    Returns (columns, band_text): columns are [{'a','b','text'}] in order."""
    info = []
    for li, ln in enumerate(lines):
        chunks = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", ln)]
        pa = [c for c in chunks if (c[0] + c[1]) / 2.0 >= price_col - 4]
        # a field label OCR set a single space after the last name
        # ('... Pavecon Public Works LP Project: 2026 Annual Asphalt ...')
        # ends the name; the label and its value are no part of the band
        cut = []
        for a, b, t in pa:
            m = _LABEL_CUT.search(t)
            if m and m.start() > 0:
                cut.append((a, a + m.start(), t[:m.start()]))
            elif not re.match(r"(?i)" + _FIELD_LABEL_AT, t):
                cut.append((a, b, t))
        pa = cut
        if _PROJECT_LINE.search(pb.txt(" ".join(c[2] for c in pa))):
            # a title line set over the price columns ('City Project No.
            # 23-P#-20-5710-4399-0001', Murphy) names no bidder; a project
            # number left of them ('Bid# 2204-052', Frisco) is no matter
            info.append(([], False, False))
            continue
        # pieces of one cell are joined BEFORE they are judged: RapidOCR
        # spreads 'Fort Worth, TX' and 'Primary Award' into separate chunks,
        # each of which alone passes for part of a name
        pa = _merge_same_column(pa, prof)
        kinds = [chunk_class(c[2]) for c in pa]
        names = [c for c, k in zip(pa, kinds) if k == "name"]
        firm = any(is_firmish(respace(c[2])) for c in names)
        captions = any(k == "ignore" and (pb._caption_only(c[2]) or _caption_glued(c[2]))
                       for c, k in zip(pa, kinds))
        # a caption line OCR garbled ('ESTOTUNITOO  UNII  EXTENDED') is not a
        # name line: captions with nothing firm-like beside them
        cand = bool(names) and "stop" not in kinds and not (captions and not firm)
        info.append((names, cand, cand and firm))
    seeds = [i for i, x in enumerate(info) if x[2]]
    if not seeds:
        return None, ""
    seed = seeds[-1]
    band = [seed]
    lo = seed
    while len(band) < 3 and lo - 1 >= 0 and info[lo - 1][1]:
        lo -= 1
        band.insert(0, lo)
    if len(band) < 3 and seed + 1 < len(info) and info[seed + 1][1]:
        band.append(seed + 1)
    # columns: anchor on the band line with the most chunks, then merge every
    # other chunk into the column whose span holds its centre
    order = sorted(band, key=lambda i: (-len(info[i][0]), i))
    cols = []
    for li in order:
        for a, b, t in info[li][0]:
            c = (a + b) / 2.0
            hit = [col for col in cols if col["a"] <= c < col["b"] or a <= col["c"] < b]
            if len(hit) == 1:
                col = hit[0]
                col["parts"].append((li, t))
                col["a"], col["b"] = min(col["a"], a), max(col["b"], b)
            elif not hit:
                cols.append({"a": a, "b": b, "c": c, "parts": [(li, t)]})
            else:
                return None, "a header chunk spans two name columns"
    cols.sort(key=lambda col: col["a"])
    out = []
    for col in cols:
        t = " ".join(p for _li, p in sorted(col["parts"]))
        t = respace(pb.strip_address(t) or t)
        # an award-rank caption left in front of the name when OCR split
        # 'Primary Award' across lines ('Primary RELIABLE PAVING INC.')
        m = re.match(r"(?i)^(?:primary|secondary|tertiary)\s+(?:award\s+)?(?=\S)", t)
        if m and valid_name(t[m.end():]):
            t = t[m.end():]
        out.append({"a": col["a"], "b": col["b"], "text": t})
    return out, " | ".join(c["text"] for c in out)


def slot_profile(rows, n_slots):
    """Where each bidder's price columns sit on the page, from the rows whose
    cells balanced: {slot: (left col, right col)}.  None when too few rows
    vouch for it.  Slots no row vouches for are placed by the spacing of the
    others (bid tabs space their bidder blocks evenly)."""
    import statistics
    left, right = defaultdict(list), defaultdict(list)
    for rp in rows:
        for k in rp.kept:
            idx = rp.example.get(k) or []
            if len(idx) == 2:
                left[k].append(rp.cells[idx[0]].col)
                right[k].append(rp.cells[idx[1]].col)
    known = {k: (statistics.median(left[k]), statistics.median(right[k]))
             for k in left if len(left[k]) >= 2}
    if len(known) < max(1, min(2, n_slots)):
        return None
    ks = sorted(known)
    if len(ks) >= 2:
        step = statistics.median([(known[b][0] - known[a][0]) / (b - a)
                                  for a, b in zip(ks, ks[1:])])
    else:
        step = (known[ks[0]][1] - known[ks[0]][0]) + 12
    if step <= 0:
        return None
    width = statistics.median([known[k][1] - known[k][0] for k in ks]) + 10
    prof = {}
    for k in range(n_slots):
        if k in known:
            prof[k] = (known[k][0], known[k][0] + width)
        else:
            near = min(ks, key=lambda j: abs(j - k))
            x = known[near][0] + (k - near) * step
            prof[k] = (x, x + width)
    return prof


def assign_by_position(cols, prof, n_slots):
    """Names to price columns by where they are printed.  Returns (names,
    how) -- names has None for a column no name sits over -- or (None, why)."""
    centers = {k: (a + b) / 2.0 for k, (a, b) in prof.items()}
    half = min(((prof[k][1] - prof[k][0]) / 2.0 for k in prof), default=8.0)
    names = [None] * n_slots
    packed = False
    for col in cols:
        cover = [k for k in range(n_slots)
                 if col["a"] - 0.6 * half <= centers[k] <= col["b"] + 0.6 * half]
        if not cover:
            c = (col["a"] + col["b"]) / 2.0
            k = min(centers, key=lambda j: abs(centers[j] - c))
            if abs(centers[k] - c) <= 1.2 * half:
                cover = [k]
        if not cover:
            continue                     # printed over no price column
        if len(cover) == 1:
            parts = [col["text"]]
        else:
            parts = split_packed_best(col["text"], len(cover))
            if parts is None:
                # A long name reaches past its neighbour's centre: it is
                # still ONE name when it reaches at most one neighbour, sits
                # centred over its own column, and holds at most one legal
                # suffix.  Anything wider is a run of names that does not
                # split, and the page is refused.
                c = (col["a"] + col["b"]) / 2.0
                dist = sorted((abs(centers[j] - c), j) for j in cover)
                k = dist[0][1]
                step = abs(centers[cover[1]] - centers[cover[0]]) if len(cover) == 2 else 0
                n_legal = sum(1 for w in col["text"].split() if _is_legal(w))
                if (len(cover) == 2 and n_legal <= 1 and valid_name(col["text"])
                        and (col["b"] - col["a"]) <= 1.5 * step
                        and dist[0][0] <= 0.6 * dist[1][0]):
                    cover, parts = [k], [col["text"]]
                else:
                    return None, ("%r sits over %d price columns and splits no single "
                                  "way at firm words" % (col["text"][:60], len(cover)))
            else:
                packed = True
        for k, nm in zip(cover, parts):
            if names[k] is not None:
                return None, "two header names over price column %d" % (k + 1)
            names[k] = nm
    return names, "header, matched to price columns by position" + (
        ", packed names split at firm words" if packed else "")


def header_names(lines, price_col, n_slots, prof=None):
    """Bidder names over the price columns of one page.

    Returns (names, how, band_text).  names is a list with one entry per
    price column (None for a column the header names no bidder over), or
    None when the names cannot be recovered -- `how` then says why.

    When the page's rows locate its price columns (slot_profile), names are
    matched to columns by position; otherwise by count, the k-th name for
    the k-th column, which needs exactly as many names as columns.
    """
    cols, band_text = name_band(lines, price_col, prof)
    if cols is None:
        return None, band_text or "no name line over the price columns", ""
    if prof:
        names, how = assign_by_position(cols, prof, n_slots)
        if names is not None and any(names) and _names_credible(names, positional=True):
            return [pb.txt(n) if n else None for n in names], how, band_text
    texts = [c["text"] for c in cols]
    if len(texts) > n_slots:
        texts = [t for t in texts if is_firmish(t)]
    if len(texts) == n_slots:
        names, how = texts, "header columns"
    elif len(texts) < n_slots:
        extra = n_slots - len(texts)
        options = []
        per_col = []
        for t in texts:
            opts = [(1, [t])]
            for kk in range(2, extra + 2):
                sp = split_packed_best(t, kk)
                if sp:
                    opts.append((kk, sp))
            per_col.append(opts)
        for combo in itertools.product(*per_col):
            if sum(kk - 1 for kk, _ in combo) == extra:
                options.append([nm for _kk, sp in combo for nm in sp])
        if len(options) != 1:
            return None, ("%d names in the header for %d price columns; %s"
                          % (len(texts), n_slots,
                             "no firm-word split fits" if not options
                             else "%d different splits fit" % len(options))), band_text
        names, how = options[0], "header, packed names split at firm words"
    else:
        return None, "%d name columns for %d price columns" % (len(texts), n_slots), band_text
    if not _names_credible(names):
        return None, "header text is not a set of bidder names: %s" % "; ".join(names), band_text
    return [pb.txt(n) for n in names], how, band_text


def _name_noise(name):
    """How damaged a name reads: characters no firm name is spelt with."""
    return len(re.findall(r"[^A-Za-z0-9 &,.'()/-]", name or "")) + (name or "").count("\ufffd") * 3


def _same_name(a, b):
    """Two OCR readings of one printed name ('SJ & J Construction, LLC' /
    'SJ & J Cons\ufffd-uction, LLC')."""
    import difflib
    x = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    y = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    return x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.8


def _similar(a, b):
    ta = set(re.findall(r"[a-z]{3,}", (a or "").lower()))
    tb = set(re.findall(r"[a-z]{3,}", (b or "").lower()))
    return bool(ta and tb) and len(ta & tb) / float(len(ta | tb)) >= 0.5


# ------------------------------------------------------------ the whole file

def _mode_hi(xs):
    """Most frequent value; on a tie, the largest."""
    if not xs:
        return None
    c = Counter(xs)
    top = max(c.values())
    return max(v for v, n in c.items() if n == top)


_CELL_KINDS = ("amt", "bad", "blank", "nobid", "cur", "dash")


def _first_cell_col(toks, r0):
    """Column of the first token at or after r0 that makes a price cell."""
    for t in toks[r0:]:
        if t.kind in _CELL_KINDS:
            return t.a
    return None


def natural_cells(line, fewest=False):
    """Cell count of a line's price region, glued cells split, and the
    column where its first price cell starts.  fewest: count each run of
    digits by its reading with the fewest cells ('$20.000.00' is $20,000.00
    before it is $20.00 glued to 0.00) -- the count a page's column number
    is read from."""
    s = deoh(line)
    toks = scan(s)
    r0 = _region_start(toks)
    units = _units(toks[r0:])
    n = 0
    for u in units:
        n += (min if fewest else max)(len(alt) for alt in u)
    return n, _first_cell_col(toks, r0), toks, r0


def classify_line(line):
    """'item' / 'total' / 'prices' (price cells only) / 'banner' / 'text' /
    'empty' and supporting data."""
    if not line.strip():
        return "empty", {}
    if line == RULE_LINE:
        return "rule", {}
    n, col, toks, r0 = natural_cells(line)
    left = deoh(line)[:col] if col is not None else deoh(line)
    words = [t for t in toks[:r0] if t.kind == "word" and sum(ch.isalpha() for ch in t.text) >= 2]
    if n >= 1:
        cap = total_caption(left)
        if cap:
            return "total", {"caption": cap, "cells": n, "col": col}
        if not words and not any(t.kind in ("num",) for t in toks[:r0]):
            if not any(t.kind in ("amt", "bad") for t in toks[r0:]):
                return "text", {}             # a lone '-' or '$' is no price row
            return "prices", {"cells": n, "col": col}
        if n >= 2:
            return "item", {"cells": n, "col": col, "fewest": natural_cells(line, True)[0]}
        return "text", {"col": col}
    b = banner_of(line)
    if b:
        return "banner", {"banner": b}
    return "text", {}


def _slot_reason(rp, k):
    """Why bidder column k of a parsed row yielded no record ('' = no bid)."""
    idx = rp.example.get(k, [])
    cells = [rp.cells[i] for i in idx]
    kinds = [c.kind for c in cells]
    if cells and all(kd in ("blank", "nobid") for kd in kinds):
        return ""                                        # NO BID / an empty '$ -' cell
    if cells and all(kd in ("num", "blank", "nobid") for kd in kinds) and all(
            v == 0 for c in cells for v in c.vals):
        return ""                                        # '$0.00': a zero bid, no price
    if not rp.had_qty:
        return "quantity_missing"
    if not cells:
        return "cell_missing"
    if "bad" in kinds:
        return "unreadable_amount"
    if kinds == ["num", "num"]:
        prs = pair_readings(cells[0], cells[1], rp.qty)
        if len({u for u, _ in prs}) > 1:
            return "ambiguous_reading"
        return "row_fails_entirely" if rp.score[0] == 0 else "unbalanced"
    return "cell_missing"


# A council packet's scanned pages include invoices and receipts -- priced
# lines, quantities, totals, a vendor's name at the top -- which are no bid
# tabulation (Grand Prairie's 2023-11-21 packet: an invoice for concrete
# sawing read as 'EDGE CONCRETE SAWING' bidding $1.44).
_INVOICE_WORDS = re.compile(r"(?i)invoice|remit\s*(?:to|payment|addr)|\bbill\s*to\b|"
                            r"\bship\s*-?\s*to\b|amount\s*due|balance\s*due|\breceipt\b|"
                            r"packing\s*(?:list|slip)")
_TAB_WORDS = re.compile(r"(?i)\bbid(?:s|der|ders|ding)?\b|tabulation|\bproposal\b|base\s*bid|"
                        r"engineer'?s?\s*estimate|unit\s*bid")


def invoice_page(text):
    """True for a page that reads as an invoice or receipt: two invoice
    words, or one on a page that never says 'bid' / 'tabulation'."""
    inv = len(_INVOICE_WORDS.findall(text))
    return inv >= 2 or (inv >= 1 and not _TAB_WORDS.search(text))


def parse_text(text, src_name="", stats=None, implied_qty=False):
    """Parse one sidecar's text.  Returns (recs, audit, strings, info)."""
    meta, pages = read_sidecar(text)
    engine = meta["engine"] or "unknown OCR engine"
    base = Path(meta["source"] or src_name).name or src_name
    info = {"source": meta["source"], "engine": engine, "pages": len(pages),
            "page_info": [], "kept": 0, "dropped": Counter(), "no_bid_cells": 0,
            "item_rows": 0, "bidders": [], "totals": [], "status": "", "notes": [],
            "kept_if_named": 0, "lines_not_parsed": []}
    strings = [ln.strip() for _n, lines in pages for ln in lines
               if ln.strip() and ln != RULE_LINE]
    if not meta["machine_read"]:
        info["notes"].append("no MACHINE-READ header: not an ocr_scans.py sidecar")

    # ---- pass 1: classify lines, find each page's item block and width
    pg = []
    info["text_layer_pages"] = [n for n, _l in pages if meta["page_source"].get(n) == "text_layer"]
    for pno, lines in pages:
        if meta["page_source"].get(pno) == "text_layer":
            # a page ocr_scans.py copied from the PDF's own text layer so the
            # audit sees the whole document: parse_bidtabs reads that layer
            # itself, so no rows are taken from it here
            cls = [("empty", {}) if not ln.strip() else ("text", {}) for ln in lines]
        elif invoice_page("\n".join(lines)):
            info["notes"].append("p%d: an invoice or receipt, not a bid tabulation -- not read"
                                 % pno)
            cls = [("empty", {}) if not ln.strip() else ("text", {}) for ln in lines]
        else:
            cls = [classify_line(ln) for ln in lines]
        items = [i for i, (k, _d) in enumerate(cls) if k == "item"]
        prices = [i for i, (k, _d) in enumerate(cls) if k == "prices"]
        # the page's bidder columns: half the cells of a typical row, rounded
        # up (a lost cell makes a row shorter, never longer); ties go to
        # the wider reading
        n_slots = _mode_hi([(cls[i][1]["fewest"] + 1) // 2 for i in items]) if items else None
        pinfo = {"page": pno, "lines": lines, "cls": cls, "items": items, "prices": prices,
                 "n_slots": n_slots, "names": None, "names_how": "", "band": "",
                 "rows": [], "totals": [], "cont": False}
        if not items and len(prices) >= 2:
            # a prices-only page: its width comes from its own price rows
            pe = [cls[i][1]["cells"] for i in prices if cls[i][1]["cells"] >= 2]
            pinfo["n_slots"] = _mode_hi([(c + 1) // 2 for c in pe]) if pe else None
            pinfo["cont"] = True
        block = items if items else (prices if pinfo["cont"] else [])
        pinfo["first"] = min(block) if block else None
        cols = [cls[i][1]["col"] for i in block if cls[i][1].get("col") is not None]
        pinfo["price_col"] = min(cols) if cols else None
        pg.append(pinfo)

    # ---- pass 2: rows (names are not needed to read them)
    prev_item = None
    prev_alpha = None            # the last 'D12'-style item number, as ('D', 12)
    section = "BASE BID"
    template = None              # the last page with item rows, for prices-only pages
    for p in pg:
        lines, cls = p["lines"], p["cls"]
        N = p["n_slots"]
        if p["cont"]:
            _continuation(p, template, info)
            continue
        if not p["items"]:
            continue
        first_tok = (lines[p["first"]].split() or [""])[0]
        if re.match(r"^[\[|{]?1(?:\D|$)", first_tok):
            prev_item = None     # this page's table starts its numbering again
        sub_open = False
        rows = []
        # what each line is to the description assignment: 'row', 'item' (an
        # item-shaped line that yields no row), 'mark' (total, banner),
        # 'rule', 'text', '' (empty)
        roles = [""] * len(lines)
        after_total = False
        for i, (kind, d) in enumerate(cls):
            ln = lines[i]
            if kind == "text" and after_total and _ALT_AFTER_TOTAL.match(pb.txt(ln)):
                # 'Add Alternate East parkway of S. Waterford Oaks Drive' --
                # a title parse_bidtabs' banner shape refuses, but printed
                # straight under a SUBTOTAL it can only open the next block
                kind, d = "banner", {"banner": unglue_words(pb.txt(ln)).upper()[:60]}
            if kind not in ("empty", "rule"):
                after_total = (kind == "total")
            if kind in ("text", "rule"):
                roles[i] = kind
                continue
            if kind == "banner":
                roles[i] = "mark"
                section = d["banner"]
                prev_item = None
                continue
            if kind == "total":
                roles[i] = "mark"
                cap = d["caption"]
                if _SUBTOTAL_ONLY.match(cap):
                    sub_open = True
                elif _TOTAL_ONLY.match(cap):
                    sub_open = False
                if i > p["first"]:
                    p["totals"].append((i, cap, section, _total_cells(ln)))
                continue
            if kind == "prices":
                roles[i] = "item"            # a package total, or a row's cells alone
                continue
            if kind != "item":
                continue
            roles[i] = "item"
            rp = parse_item_line(ln, N, prev_item, implied=implied_qty, prev_alpha=prev_alpha)
            if rp is None:
                continue
            rp.line_no, rp.page, rp.section = i, p["page"], section
            if rp.item_no or rp.unit_raw or rp.score[0] > 0:
                if rp.item_no and rp.item_no.isdigit():
                    prev_item = int(rp.item_no)
                ma = re.fullmatch(r"([A-Z])(\d{1,3})", rp.item_no or "")
                if ma:
                    prev_alpha = (ma.group(1), int(ma.group(2)))
                if sub_open:
                    rp.note = "after_subtotal"
                roles[i] = "row"
                rows.append(rp)
            else:
                info["lines_not_parsed"].append("p%d: %s" % (p["page"], ln.strip()[:100]))
        p["roles"] = roles
        _labels_beside_rows(lines, roles, rows)
        p["rows"] = rows
        if rows:
            template = p
    # wrapped description lines, page by page, against the document's
    # description width (the long end of the rows' own one-line texts)
    own = sorted(len(rp.desc) for p in pg if not p["cont"] for rp in p["rows"] if rp.desc)
    width = own[int(0.9 * (len(own) - 1))] if len(own) >= 8 else None
    for p in pg:
        if p["rows"] and not p["cont"]:
            p["desc_mode"] = assign_wrapped_lines(p["lines"], p["roles"], p["rows"], width)
    _alternates_after_subtotal(pg, info)
    _reconcile_descriptions(pg, info)
    for p in pg:
        _units_from_column(p)
    _reconcile_units(pg, info)

    # ---- pass 3: bidder names per page, from its header
    for p in pg:
        if p["n_slots"] and p["first"] is not None and p["price_col"] is not None:
            prof = slot_profile(p["rows"], p["n_slots"]) if p["rows"] else None
            p["prof"] = prof
            names, how, band = header_names([ln for ln in p["lines"][:p["first"]]
                                             if ln != RULE_LINE], p["price_col"],
                                            p["n_slots"], prof)
            p["names"], p["names_how"], p["band"] = names, how, band
    for i, p in enumerate(pg):
        if p["names"] or not p["n_slots"] or p["first"] is None:
            continue
        # the facing page that prints the same header, or the previous page
        # when this one prints no header at all
        for j in (i + 1, i - 1):
            if 0 <= j < len(pg) and pg[j]["names"] and pg[j]["n_slots"] == p["n_slots"] \
                    and p["band"] and _similar(p["band"], pg[j]["band"]):
                p["names"], p["names_how"] = list(pg[j]["names"]), \
                    "same header as page %d (%s)" % (pg[j]["page"], p["names_how"])
                break
        else:
            if (not p["band"] and i > 0 and pg[i - 1]["names"]
                    and pg[i - 1]["n_slots"] == p["n_slots"]):
                p["names"], p["names_how"] = list(pg[i - 1]["names"]), \
                    "no header on this page; carried from page %d" % pg[i - 1]["page"]
    # Facing pages of one bidder group print the same names; where OCR read
    # one copy worse ('SJ & J Cons�-uction, LLC' against 'SJ & J
    # Construction, LLC'), both pages take the cleaner reading, so one firm
    # is not split into two bidders.
    for a, b in zip(pg, pg[1:]):
        na, nb = a["names"], b["names"]
        if na and nb and len(na) == len(nb) and na != nb and all(
                (x is None) == (y is None) and (x is None or _same_name(x, y))
                for x, y in zip(na, nb)):
            best = [x if (x is None or _name_noise(x) <= _name_noise(y)) else y
                    for x, y in zip(na, nb)]
            a["names"], b["names"] = list(best), list(best)

    # ---- pass 4: records
    recs, audit = [], []
    for p in pg:
        names = p["names"]
        N = p["n_slots"]
        page_kept = 0
        for rp in p["rows"]:
            info["item_rows"] += 1
            ex_bal = set(rp.example.get("_bal", []))
            # A pair that balances on the best alignment but whose COLUMN is
            # not the same on every best alignment is one cell lost to
            # ambiguity -- counted once, not once per column it might be in.
            n_amb = len(ex_bal - set(rp.kept))
            if n_amb:
                info["dropped"]["no_bidder_names" if not names else "ambiguous_column"] += n_amb
            for k in range(rp.n_slots):
                nm = names[k] if names and k < len(names) else None
                if k in rp.kept:
                    if not nm:
                        info["dropped"]["no_bidder_names"] += 1
                        info["kept_if_named"] += 1
                        continue
                    u, e = rp.kept[k]
                    recs.append(_record(base, p["page"], rp, nm, u, e, engine))
                    page_kept += 1
                elif k in ex_bal:
                    continue                     # counted above as ambiguous
                else:
                    why = _slot_reason(rp, k)
                    if why == "":
                        info["no_bid_cells"] += 1
                    elif not nm:
                        info["dropped"]["no_bidder_names"] += 1
                    else:
                        info["dropped"][why] += 1
        audit.append({"src_file": base, "src_sheet": "ocr_p%d" % p["page"],
                      "header_row": (p["first"] or 0), "n_price_cols": N or 0,
                      "n_rows": page_kept, "bidders": [n for n in (names or []) if n]})
        info["page_info"].append({"page": p["page"], "n_slots": N, "item_rows": len(p["rows"]),
                                  "names": names, "names_how": p["names_how"],
                                  "band": p["band"], "kept": page_kept,
                                  "continuation": p["cont"]})
    info["kept"] = len(recs)
    # how each emitted row got its unit, and each page's cell alignment
    info["units"] = Counter("unread" if not r["unit"] else "read" for r in recs)
    info["unit_how"] = Counter()
    for p in pg:
        for rp in p["rows"]:
            n = sum(1 for k in range(rp.n_slots) if k in rp.kept)
            if n and rp.unit_how and rp.unit_raw:
                info["unit_how"][rp.unit_how] += n
    info["desc_modes"] = {p["page"]: p.get("desc_mode") for p in pg if p.get("desc_mode")}
    seen = []
    for r in recs:
        if r["bidder_raw"] not in seen:
            seen.append(r["bidder_raw"])
    info["bidders"] = seen
    info["totals"] = printed_total_check(pg, recs)
    if recs:
        info["status"] = "rows emitted"
    elif info["kept_if_named"]:
        info["status"] = "left out: bidder names not recoverable"
    elif info["item_rows"]:
        info["status"] = "left out: no row balances"
    else:
        info["status"] = "no line-item table (0 item rows)"
    if stats is not None:
        stats.setdefault("ocr_sidecars", {})[meta["source"] or base] = {
            "rows": len(recs), "dropped": dict(info["dropped"]),
            "no_bid_cells": info["no_bid_cells"], "status": info["status"],
            "engine": engine}
    return recs, audit, strings, info


def _total_cells(line):
    s = deoh(line)
    toks = scan(s)
    r0 = _region_start(toks)
    units = _units(toks[r0:])
    vals = []
    for u in units:
        alt = max(u, key=len)
        for c in alt:
            vals.append(c)
    return vals


# ------------------------------------------------- wrapped description lines
#
# Weights of assign_wrapped_lines' costs.  Only their order matters much: a
# sentence that plainly runs on (SEP_STRONG) outweighs any layout preference,
# and the layout preferences outweigh the no-cue split (SEP_NEUTRAL).
W_NEAR = 0.15       # centred cells: a cell one line taller on one side of its price row than the other
W_IMBAL = 0.6       # centred cells: per further line of difference between its lines above and below
W_SIDE = 2.0        # top/bottom-aligned cells: per line on the side the page's cells do not use
W_JITTER = 0.3      # ... except the first such line of a price row with no text of its own (OCR put
                    # the cell's last line a hair under the prices instead of beside them)
C_MARK = 0.6        # per description-shaped line left to a caption, total, banner or the page edge
C_EDGE = 1.5        # per line a row with its own text takes from across a caption, total or banner
C_BARE = 1.0        # a price row with no text on its line that is given no line at all
SEP_STRONG = 3.0    # cutting a sentence that runs on: next line opens in lowercase, or this one ends 'and' / ',' / 'for'
SEP_NEUTRAL = 0.5   # cutting between two lines with no cue either way
SEP_FULL = 1.5      # ... after a line that fills the column (the text wrapped, so it goes on)
SEP_SHORT = 0.2     # ... after a short line (a paragraph's last line is the short one)
EPS_DIST = 0.02     # centred cells: per line of distance between a line and its price row
MODE_MARGIN = 0.5   # top- or bottom-aligned must beat centred by this much on a page
_INF = 1e9

# a line ending in a word that needs more ('... and', '... for', ',') -- not
# a size ('30 IN', '6 FT'), nor a capital A ('(Type A')
_CONNECT_END = re.compile(
    r"(?:(?<!\d )(?<!\d)\b(?i:and|or|of|for|with|to|in|at|by|from|on|the|an|including|incl|"
    r"per|into|under|over|between|as|within|w/|plus|via|than|each|all|include|includes|shall)"
    r"|\ba|[,&\-/(:+;])\s*$")
# 'in place' closes a bid item's sentence even glued ('andinplace'); a bare
# 'complete' does not ('... Wingwalls, complete' / 'and in place')
_CLOSE_END = re.compile(r"(?i)in\s*place\s*\.?\s*$|\.\s*$")
_ABBR_END = re.compile(r"(?i)\b(?:no|ea|approx|min|max|dia|typ|incl|vs)\.\s*$")
_LOWER_START = re.compile(r"^[^A-Za-z0-9]*([a-z]{2,})")


def _lower_start(q):
    """A line that opens in lowercase continues the sentence above -- unless
    the rest of it is in capitals ('acelly STRUCTURE REPAIR (NO REBAR'),
    where the lowercase is OCR debris of a clipped word."""
    m = _LOWER_START.match(q)
    if not m:
        return False
    rest = [ch for ch in q[m.end():] if ch.isalpha()]
    return not rest or sum(ch.isupper() for ch in rest) < 0.6 * len(rest)


def _link(p, q, width=None):
    """Cost of giving two consecutive text lines to different items: none
    after a line that closes a sentence ('... complete in place.'), a lot
    when the sentence plainly runs on ('... equipment for' / 'structural
    concrete.', or a next line that opens in lowercase), a little otherwise
    -- more after a line that fills the column (the cell's text wrapped
    there: 'GROUND BOX, INSTALLED BY CONTRACTOR, PROVIDED' / 'BY ONCOR'),
    less after a short one (a description's last line is the short one).
    width: the page's full line length, in characters."""
    if not p or not q:
        return 0.0
    if _CLOSE_END.search(p) and not _ABBR_END.search(p):
        return 0.0
    if _CONNECT_END.search(p) or _lower_start(q):
        return SEP_STRONG
    if width:
        if len(p) >= 0.85 * width:
            return SEP_FULL
        if len(p) <= 0.5 * width:
            return SEP_SHORT
    return SEP_NEUTRAL


_TRAILING_MONEY = re.compile(r"(?:\s+[|\[\]{}]*\s*\$?\s?\d{1,3}(?:,\d{3})*\.\d{2}[|\]}]*)+\s*$")
_SECTION_HEAD = re.compile(r"^([A-Z])[a-z]?\s+([A-Z][A-Z0-9 &/,.'\"()-]{5,})$")
_ITEM_LED = re.compile(r"^\s*[\[|{]?(?:#?\d{1,4}(?:[.-]\d{1,3})?\.?|[A-Z]-?\d{1,3})\s+(?=\S)")


_LONE_LABEL = re.compile(r"^\s*[\[|{]?(#?\d{1,4}(?:\.\d{1,3})?|[A-Z]-?\d{1,3})\.?\s*$")


def _labels_beside_rows(lines, roles, rows):
    """An item number printed alone on the line above or below its row
    (the label centred in a tall cell while the prices sit on the cell's
    last line: Tarrant's 'F2023177', '2.3' under its prices) goes to that
    row when the row has none.  Only a label with exactly one numberless
    row beside it, and a row with exactly one label beside it."""
    by_line = {rp.line_no: rp for rp in rows}
    labels = {}
    for i, ln in enumerate(lines):
        if roles[i] == "text":
            m = _LONE_LABEL.match(deoh(ln).replace("O", "0") if re.search(r"\d", ln) else ln)
            if m:
                labels[i] = m.group(1).lstrip("#")
    claims = defaultdict(list)
    for i, lab in labels.items():
        near = [by_line[j] for j in (i - 1, i + 1) if j in by_line and not by_line[j].item_no]
        if len(near) == 1:
            claims[near[0].line_no].append(lab)
    for ln_no, labs in claims.items():
        if len(labs) == 1:
            by_line[ln_no].item_no = labs[0]


_TRACE = None       # a list to collect each page's anchors and splits (debugging)


def assign_wrapped_lines(lines, roles, rows, width=None):
    """Give every wrapped description line to an item by its vertical
    position, and join it into that row's description.  Returns the cell
    alignment used ('centre' / 'bottom' / 'top' / 'rules'), or ''.

    A bid tab prints a long description on several lines and the numbers on
    one line: the last of the cell (bottom-aligned, Rowlett), its middle
    (centred, Tarrant: two lines above, three below) or its first.  A text
    line between two price rows therefore belongs to the row it sits nearer
    to on a centred page, to the row below on a bottom-aligned one, to the
    row above on a top-aligned one.  Which of these a page uses is not
    printed, so every page is solved all three ways and the cheapest
    explanation wins (centred unless another beats it by MODE_MARGIN) --
    unless the text itself shows the alignment (a sentence running into a
    row's own text from above, on from it below, or across a price row
    with no text of its own):

      * the layout cost: a centred cell has as many lines above its price
        row as below, or one more on one side (W_NEAR, then W_IMBAL per
        further line); an aligned one has none on the other side (W_SIDE
        per line);
      * the sentence cost (_link): a cut between two lines of one sentence
        ('... and equipment for' / 'structural concrete.') is dear, a cut
        after a line that closes a sentence is free;
      * lines next to a caption, total or banner go to it (C_MARK each, and
        are dropped) unless the row beside it plainly needs them (C_EDGE);
        a price row with no text of its own is given some (C_BARE).

    Anchors are the rows, item-shaped lines that yield no row ('2  ...
    $195,925.00', a package total; a row OCR could not read) -- they keep
    their own lines, so a neighbour cannot take them -- and the marks
    (captions, totals, banners, notes left of the description column).  A
    '### RULE' line (a table rule ocr_scans.py found on the rendered page)
    is a hard edge: lines are never carried across it, so between rules the
    cell is exact.  Lines starting in the price columns (a note under a
    price, the bidder band) and fragments of under 3 letters are skipped.
    """
    import statistics
    cols = [rp.desc_col for rp in rows if rp.item_no and rp.desc_col is not None]
    if not cols:
        cols = [rp.desc_col for rp in rows if rp.desc_col is not None]
    if not cols:
        return ""
    lo = statistics.median(cols) - 3
    mcols = [rp.money_col for rp in rows if rp.money_col is not None]
    hi = statistics.median(mcols) if mcols else 10 ** 6
    by_line = {rp.line_no: rp for rp in rows}
    anchors = [["mark", -1, "", None]]          # the page's top edge
    cands = {}
    for i, ln in enumerate(lines):
        role = roles[i]
        if i in by_line:
            anchors.append(["row", i, by_line[i].desc or "", by_line[i]])
        elif role in ("item", "mark", "rule"):
            own = ""
            if role == "item":
                m = _ITEM_LED.match(deoh(ln))
                own = pb.txt(ln[m.end():] if m else "")
                own = "" if sum(ch.isalpha() for ch in own) < 3 else own
            anchors.append([role, i, own, None])
        elif role == "text":
            st = len(ln) - len(ln.lstrip())
            tx = pb.txt(ln)
            if not tx or sum(ch.isalpha() for ch in tx) < 3 or st >= hi:
                continue                                    # skipped: no part of a cell's text
            if (st < lo or total_caption(tx) or banner_of(tx) or pb._caption_only(tx)
                    or _caption_glued(tx)):
                m = _ITEM_LED.match(deoh(ln)) if st < lo else None
                if m and sum(ch.isalpha() for ch in ln[m.end():]) >= 3 and not total_caption(tx):
                    # an item number and its text, the prices lost: an item
                    anchors.append(["item", i, pb.txt(ln[m.end():]), None])
                else:
                    anchors.append(["mark", i, "", None])
                continue
            # an amount OCR set on a description line ('Rock Riprap 228,000.00',
            # the cell's total a hair above its row) is no part of the text
            tx = _TRAILING_MONEY.sub("", tx).strip() or tx
            tx = re.sub(r"^(?:[_|\[\]{}]+\s*)+", "", tx) or tx       # a cell rule read as '|'
            cands[i] = (st, tx)
    anchors.append(["mark", len(lines), "", None])  # the page's bottom edge
    starts = [len(rp.line_text) - len(rp.line_text.lstrip()) for rp in rows
              if rp.item_no and rp.line_text]
    icol = statistics.median(starts) if starts else None
    tcol = statistics.median(st for st, _tx in cands.values()) if cands else None
    row_lines = sorted(by_line)

    def next_row(i):
        k = bisect.bisect_right(row_lines, i)
        return by_line[row_lines[k]] if k < len(row_lines) else None
    # A section heading at the item column ('B  STEWART CREEK WEST REUSE PUMP
    # STATION', 'Cc PANTHER CREEK ...': a section letter and a title in
    # capitals) over a row that starts the section's numbering is no part
    # of that row's description (Frisco 2003-047 items B1, C1).
    for i, (st, tx) in sorted(cands.items()):
        m = _SECTION_HEAD.match(tx)
        nr = next_row(i)
        if (m and icol is not None and st <= icol + 2 and nr is not None
                and (nr.item_no or "") in ("", "1", m.group(1) + "1", m.group(1) + "-1")):
            del cands[i]
            anchors.append(["mark", i, "", None])
    # A line whose last token is a number standing in the rows' quantity
    # column is an item printed without prices (no bid, or prices OCR lost:
    # Tarrant 2022-031 'HEADWALL, 1 BARRELL 15" TO 30"  ...  10'), not
    # description: it keeps its text.
    qcols = [rp.qty_span[0] for rp in rows if rp.qty_span and rp.qty_span[0] is not None]
    if len(qcols) >= 3:
        qc = statistics.median(qcols)
        for i, (st, tx) in sorted(cands.items()):
            m = re.search(r"(?<![\w.,$])\d[\d,]*(?:\.\d+)?\s*$", deoh(lines[i]))
            if m and abs(m.start() - qc) <= 3 and sum(ch.isalpha() for ch in tx) >= 3:
                del cands[i]
                anchors.append(["item", i, pb.txt(deoh(lines[i])[:m.start()]), None])
        anchors.sort(key=lambda a: a[1])
    # A line that opens with an item number in the item column (left of
    # where wrapped text starts) begins an item's cell.  Over a row that has
    # no number of its own it is that row's first line, and gives the row
    # its number (Frisco: '1  DEMOLITION OF EXISTING STEWART CREEK WEST' over
    # the prices of item B1); otherwise it is an item of its own whose prices
    # are not on the page ('7  DRAINAGE & ROADWAY IMPROVEMENTS  1 EA', a
    # group heading) and keeps its text.  The column test keeps '5 workers x
    # 4 days' (a wrapped line that starts with a number, at the text
    # column) a description line.
    lead = {}
    for i, (st, tx) in sorted(cands.items()):
        m = _ITEM_LED.match(deoh(lines[i]))
        if not m or sum(ch.isalpha() for ch in lines[i][m.end():]) < 3:
            continue
        if not ((icol is not None and st <= icol + 2) or (tcol is not None and st < tcol - 2)):
            continue
        nr = next_row(i)
        if nr is not None and not nr.item_no and not any(i < k < nr.line_no for k in lead):
            lead[i] = m
        else:
            del cands[i]
            anchors.append(["item", i, pb.txt(lines[i][m.end():]), None])
    for i in list(lead):                         # two numbered lines over one row: the
        nr = next_row(i)                         # first is an item of its own
        if any(i < k < nr.line_no for k in lead):
            del cands[i]
            anchors.append(["item", i, pb.txt(lines[i][lead.pop(i).end():]), None])
    anchors.sort(key=lambda a: a[1])
    cands = {i: tx for i, (_st, tx) in cands.items()}
    M = len(anchors)
    owners = ("row", "item")
    gaps = []
    for j in range(M - 1):
        a, b = anchors[j][1], anchors[j + 1][1]
        if anchors[j][0] in owners or anchors[j + 1][0] in owners:
            gaps.append([(i, cands[i]) for i in range(a + 1, b) if i in cands])
        else:
            gaps.append([])                     # between two marks: no item's text
    if not any(gaps):
        return ""
    row_js = [j for j, a in enumerate(anchors) if a[0] == "row"]
    first_row, last_row = (row_js[0], row_js[-1]) if row_js else (0, M)
    # the description column's full line length: from the rows' own text and
    # the lines between rows (not a paragraph printed over the table)
    lo_i = anchors[first_row][1] if row_js else -1
    hi_i = anchors[last_row][1] if row_js else len(lines)
    if width is None:
        lens = sorted([len(rp.desc) for rp in rows if rp.desc]
                      + [len(t) for i, t in cands.items() if lo_i < i < hi_i])
        width = lens[int(0.9 * (len(lens) - 1))] if len(lens) >= 4 else None

    # An open parenthesis carries a sentence on to the line that closes it
    # ('(DR 9, 250 PSI' / 'HDPE, C906, ...; or DR 14' / 'Certa-Lok ... C900)'):
    # the running depth over the page's text in reading order (reset after a
    # sentence closes, never below 0).  Cutting between a line that leaves a
    # parenthesis open and the next line, which closes it, is SEP_STRONG --
    # an open one alone is no evidence: OCR loses closing parentheses
    # ('Pollution Prevention Plan (SW3P').
    seq = sorted([(i, t) for i, t in cands.items()]
                 + [(a[1], a[2]) for a in anchors if a[0] in owners and a[2]])
    open_after, closes = {}, set()
    depth = 0
    for i, t in seq:
        m = re.search(r"[()]", t)
        if depth > 0 and m and m.group(0) == ")":
            closes.add(i)                   # its first parenthesis closes the open one
        # one level at most: a parenthesis OCR lost the close of
        # ('(0'-6" (Pipe') must not hold the next item's line open
        depth = min(1, max(0, depth + t.count("(") - t.count(")")))
        if _CLOSE_END.search(t) and not _ABBR_END.search(t):
            depth = 0
        open_after[i] = depth > 0

    def link(p, q, pi=None, qi=None):
        if p and q and open_after.get(pi) and qi in closes:
            return SEP_STRONG
        return _link(p, q, width)

    def node(j, A, B, mode):
        kind, _idx, own, _rp = anchors[j]
        if kind == "rule":
            return _INF if (A or B) else 0.0
        if kind == "mark":
            return C_MARK * (A + B)
        if mode == "centre":
            d = abs(A - B)
            c = (W_NEAR + W_IMBAL * (d - 1)) if d else 0.0
        else:
            wrong = B if mode == "bottom" else A
            if wrong and not own:
                c = W_JITTER + W_SIDE * (wrong - 1)
            else:
                c = W_SIDE * wrong
        if kind == "row" and own:
            # across the table's edge (captions over the first row, notes
            # under the last); a total or heading inside the table is no edge
            if anchors[j - 1][0] == "mark" and j <= first_row:
                c += C_EDGE * A
            if anchors[j + 1][0] == "mark" and j >= last_row:
                c += C_EDGE * B
        elif kind == "row" and not (A or B):
            c += C_BARE                 # an item is printed with a description
        return c

    def around(j, A, B):
        kind, _idx, own, _rp = anchors[j]
        if kind not in owners:
            return 0.0
        P = gaps[j - 1][-1][1] if gaps[j - 1] else None
        Pi = gaps[j - 1][-1][0] if gaps[j - 1] else None
        Q = gaps[j][0][1] if gaps[j] else None
        Qi = gaps[j][0][0] if gaps[j] else None
        if own:
            return ((link(P, own, Pi, _idx) if (P and A == 0) else 0.0)
                    + (link(own, Q, _idx, Qi) if (Q and B == 0) else 0.0))
        if P and Q and not (A > 0 and B > 0):
            return link(P, Q, Pi, Qi)
        return 0.0

    def within(j, s, mode):
        L = gaps[j]
        c = 1e-4 * s                            # an exact tie: the row below
        for n, (i, _t) in enumerate(L):
            if i in lead and s != n:
                return _INF                     # a numbered line opens the cell below
        if 0 < s < len(L):
            c += link(L[s - 1][1], L[s][1], L[s - 1][0], L[s][0])
        if mode == "centre":
            a, b = anchors[j][1], anchors[j + 1][1]
            if anchors[j][0] in owners:
                c += EPS_DIST * sum(i - a for i, _t in L[:s])
            if anchors[j + 1][0] in owners:
                c += EPS_DIST * sum(b - i for i, _t in L[s:])
        return c

    def solve(mode):
        prev = {0: (0.0, ())}                   # s of the gap above -> (cost, splits so far)
        k_prev = 0
        for j in range(M):
            k = len(gaps[j]) if j < M - 1 else 0
            cur = {}
            for s_prev, (cost, path) in prev.items():
                A = k_prev - s_prev
                for s in range(k + 1):
                    c = cost + node(j, A, s, mode) + around(j, A, s)
                    if j < M - 1:
                        c += within(j, s, mode)
                    if s not in cur or c < cur[s][0]:
                        cur[s] = (c, path + (s,))
            prev, k_prev = cur, k
        return min(prev.values(), key=lambda v: v[0])

    res = {m: solve(m) for m in ("centre", "bottom", "top")}
    # The page's alignment, from text that plainly runs across a price row:
    # into a row's own text from the line over it (the cell reaches up), on
    # from it into the line under it (the cell reaches down), or across a
    # row with no text of its own (both).  Two or more rows reaching only
    # one way (4 to 1 at least) settle it; rows reaching both ways make the
    # page centred.  Without such evidence the cheapest explanation is
    # taken, centred unless another beats it by MODE_MARGIN.
    up = down = both = 0
    why = []
    for j, (kind, idx, own, _rp) in enumerate(anchors):
        if kind != "row":
            continue
        P = gaps[j - 1][-1] if gaps[j - 1] and gaps[j - 1][-1][0] >= idx - 2 else None
        Q = gaps[j][0] if gaps[j] and gaps[j][0][0] <= idx + 2 else None
        if own:
            if P and link(P[1], own, P[0], idx) == SEP_STRONG:
                up += 1
                why.append(("up", idx))
            if Q and link(own, Q[1], idx, Q[0]) == SEP_STRONG:
                down += 1
                why.append(("down", idx))
        elif P and Q and link(P[1], Q[1], P[0], Q[0]) == SEP_STRONG:
            both += 1
            why.append(("both", idx))
    if up >= 2 and up >= 4 * (down + both):
        mode = "bottom"       # one row whose text OCR split around its prices is no centring
    elif down >= 2 and down >= 4 * (up + both):
        mode = "top"
    elif (up and down) or both:
        mode = "centre"
    else:
        mode = "centre"
        alt = min(("bottom", "top"), key=lambda m: res[m][0])
        if res[alt][0] < res["centre"][0] - MODE_MARGIN:
            mode = alt
    splits = res[mode][1]
    if _TRACE is not None:
        _TRACE.append({"anchors": [(a[0], a[1], a[2][:40], (a[3].item_no if a[3] else None))
                                   for a in anchors], "evidence": (up, down, both), "why": why,
                       "page": rows[0].page if rows else None,
                       "gaps": gaps, "splits": splits, "mode": mode,
                       "costs": {m: round(v[0], 2) for m, v in res.items()}})
    above = defaultdict(list)
    below = defaultdict(list)
    for j in range(M - 1):
        L, s = gaps[j], splits[j]
        below[j] = [t for _i, t in L[:s]]
        above[j + 1] = [t for _i, t in L[s:]]
    # How decided each split was: what the next-best split of that gap would
    # cost more, the others held.  A row's description is as sure as the
    # less decided of its two gaps; _reconcile_descriptions prefers a copy
    # of the item whose lines were decided by the text over one that was a
    # tie ('5 workers x 4 days x 8 hrs' closes item 3.7 on the page where
    # OCR kept its period, and is a coin toss where it did not).
    def local(j, s):
        A_j = len(gaps[j - 1]) - splits[j - 1] if j > 0 else 0
        c = node(j, A_j, s, mode) + around(j, A_j, s) + within(j, s, mode)
        A_n = len(gaps[j]) - s
        B_n = splits[j + 1] if j + 1 < M - 1 else 0
        return c + node(j + 1, A_n, B_n, mode) + around(j + 1, A_n, B_n)
    margin = {}
    for j in range(M - 1):
        if gaps[j]:
            here = local(j, splits[j])
            alts = [local(j, s2) for s2 in range(len(gaps[j]) + 1) if s2 != splits[j]]
            margin[j] = min(alts) - here if alts else _INF
    for j, (kind, _idx, _own, rp) in enumerate(anchors):
        if kind == "row":
            rp.desc_conf = min(margin.get(j - 1, _INF), margin.get(j, _INF))
    first_line = {}
    for j in range(M - 1):
        L, s = gaps[j], splits[j]
        if L[s:]:
            first_line[j + 1] = L[s][0]
    for j, (kind, _idx, own, rp) in enumerate(anchors):
        if kind == "row" and (above[j] or below[j]):
            if above[j] and not rp.item_no and lead.get(first_line.get(j)):
                # the cell's first line carries its item number ('1  DEMOLITION
                # OF EXISTING ...' over the prices of item 1)
                m = lead[first_line[j]]
                rp.item_no = m.group(0).strip().strip("[|{").rstrip(".").lstrip("#")
                above[j][0] = pb.txt(deoh(lines[first_line[j]])[m.end():])
            rp.desc = " ".join(t for t in above[j] + [own] + below[j] if t)
    if any(a[0] == "rule" for a in anchors):
        return "rules"
    return mode


def _alternates_after_subtotal(pg, info):
    """Rowlett's Birkhoff template prints base items, SUBTOTAL, alternate
    item 'A1', TOTAL.  An item between a SUBTOTAL and the TOTAL is outside
    the base bid: it takes 'ALT n' from an 'A<n>' item number, and a row
    there whose item number OCR lost takes the number of the A-row with the
    same quantity and unit (the same item, priced by other bidders on
    another page).  Anything else after a subtotal is left in its section."""
    arows = {}
    for p in pg:
        for rp in p["rows"]:
            m = re.fullmatch(r"A-?(\d{1,2})", rp.item_no or "")
            if m and rp.note == "after_subtotal":
                arows[(rp.qty, pb.norm_unit(rp.unit_raw or ""))] = (rp.item_no, "ALT " + m.group(1))
    n = 0
    for p in pg:
        for rp in p["rows"]:
            if rp.note != "after_subtotal":
                continue
            m = re.fullmatch(r"A-?(\d{1,2})", rp.item_no or "")
            if m:
                rp.section = "ALT " + m.group(1)
                n += 1
            elif not rp.item_no:
                hit = arows.get((rp.qty, pb.norm_unit(rp.unit_raw or "")))
                if hit:
                    rp.item_no, rp.section = hit
                    n += 1
    if n:
        info["notes"].append("%d item row(s) between SUBTOTAL and TOTAL read as alternates" % n)


_SHORT_WORDS = {"of", "to", "in", "at", "by", "on", "or", "an", "a", "x", "w/", "&", "no", "ea"}


def _desc_quality(d):
    """How well a description survived OCR: real words count, debris costs --
    and so do the fragments a word leaves when OCR drops a letter of it
    ('Hi h', 'De th').  Words OCR glued together ('ConstructTxDOT') are
    judged apart."""
    t = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", d or "")
    good = bad = 0
    for tok in t.split():
        w = tok.strip("\"'(),.;:")
        if (re.fullmatch(r"[A-Za-z][a-z]{2,}|[A-Z]{2,}|[A-Z][a-z]*[A-Z][A-Za-z]*", w)
                or re.fullmatch(r"\d[\d,./-]*(?:[\"']|-?inch|-?in)?|[\d/.-]*\d[\"']?", w)
                or w.lower() in _SHORT_WORDS or tok in ("&", "-")):
            good += 1
        else:
            bad += 1
    return good - 2 * bad


CONF_DECIDED = 0.1    # a split this much cheaper than the next best was decided, not a tie


def _reconcile_descriptions(pg, info):
    """One pay item printed once per bidder group (Rowlett pages 1/3/5,
    Tarrant's two-bidder pages) is OCR'd differently each time, and its
    item_family can differ between copies ('Trafic Control' -> other).  Rows
    with the same item number, quantity, unit and section are the same pay
    item: all copies take the best-read description, so every bidder of the
    item is classified alike -- a copy whose wrapped lines the text decided
    (desc_conf) before one that was a tie, then the cleanest reading."""
    groups = defaultdict(list)
    for p in pg:
        for rp in p["rows"]:
            if rp.item_no and rp.continuation_of is None:
                groups[(rp.item_no, rp.qty, pb.norm_unit(rp.unit_raw or ""), rp.section)].append(rp)
    n = 0
    for grp in groups.values():
        if len({rp.page for rp in grp}) < 2:
            continue
        best = max(grp, key=lambda rp: ((rp.desc_conf is None or rp.desc_conf >= CONF_DECIDED),
                                        _desc_quality(rp.desc), len(rp.desc or "")))
        for rp in grp:
            if rp.desc != best.desc:
                rp.desc = best.desc
                n += 1
    if n:
        info["notes"].append("%d copies of a pay item printed on several pages took the "
                             "best-read description of that item" % n)


def _units_from_column(p):
    """A row whose unit was not found beside its quantity takes the token
    standing in the page's unit column -- where the other rows' units start,
    when at least three rows agree on it within 3 characters -- if that
    token is a unit (or an OCR misreading of one, _OCR_UNIT)."""
    import statistics
    rows = [rp for rp in p.get("rows", []) if rp.continuation_of is None]
    cols = [rp.unit_col for rp in rows if rp.unit_raw and rp.unit_col is not None]
    if len(cols) < 3:
        return
    uc = statistics.median(cols)
    if sum(1 for c in cols if abs(c - uc) <= 3) < max(3, 0.6 * len(cols)):
        return                                   # no single unit column on this page
    for rp in rows:
        if rp.unit_raw or not rp.line_text:
            continue
        s = rp.line_text
        qa, qb = rp.qty_span or (None, None)
        for m in re.finditer(r"[A-Za-z][A-Za-z.]{0,5}", s):
            if abs(m.start() - uc) > 3 or (rp.money_col is not None and m.start() >= rp.money_col):
                continue
            if qa is not None and qa <= m.start() < qb:
                continue
            tok = m.group(0)
            u = pb.norm_unit(tok)
            if u in pb.KNOWN_UNITS and _UNIT_AT.fullmatch(tok):
                rp.unit_raw, rp.unit_how = tok, "unit_column"
            elif tok.lower() in _OCR_UNIT:
                rp.unit_raw, rp.unit_how = _OCR_UNIT[tok.lower()], "unit_column"
            else:
                continue
            if rp.desc:
                rp.desc = pb.txt(re.sub(r"(?<!\S)" + re.escape(tok) + r"(?!\S)", " ", rp.desc, count=1))
            break


def _reconcile_units(pg, info):
    """Copies of one pay item printed for several bidder groups (same item
    number, quantity and section on other pages) carry the same unit: a copy
    whose unit OCR lost takes the unit the other copies agree on."""
    groups = defaultdict(list)
    for p in pg:
        for rp in p["rows"]:
            if rp.item_no and rp.continuation_of is None:
                groups[(rp.item_no, rp.qty, rp.section)].append(rp)
    n = 0
    for grp in groups.values():
        units = {pb.norm_unit(rp.unit_raw) for rp in grp if rp.unit_raw}
        if len(units) != 1:
            continue
        donor = next(rp for rp in grp if rp.unit_raw)
        for rp in grp:
            if not rp.unit_raw:
                rp.unit_raw, rp.unit_how = donor.unit_raw, "other_copy"
                n += 1
    if n:
        info["notes"].append("%d item row(s) without a readable unit took the unit of the same "
                             "pay item printed on another page" % n)
    for p in pg:
        for rp in p["rows"]:
            if rp.template_row is not None:      # a prices-only page follows its item page
                rp.desc, rp.unit_raw = rp.template_row.desc, rp.template_row.unit_raw
                rp.unit_how = rp.template_row.unit_how


def _continuation(p, template, info):
    """A prices-only page (NRH 6885 pp.2-4): the same item rows as the last
    item page, in the same order, for more bidders.  Mapped row for row only
    when the counts agree, and kept only when the rows' own arithmetic on
    the item page's quantities confirms the mapping."""
    lines, cls, N = p["lines"], p["cls"], p["n_slots"]
    if template is None or not N or len(template["rows"]) < 3:
        # not a continuation of a real item table (Lancaster's rotated
        # pages OCR as columns of loose amounts): noted, and its amounts are
        # NOT counted as dropped item cells -- nothing says they are items
        info["notes"].append("p%d: lines of amounts with no item table before them -- "
                             "not read as items" % p["page"])
        return
    item_like = [i for i in p["prices"] if cls[i][1]["cells"] > N]
    trows = template["rows"]
    if len(item_like) != len(trows):
        info["notes"].append("p%d: %d price rows vs %d item rows on p%d -- not mapped"
                             % (p["page"], len(item_like), len(trows), template["page"]))
        info["dropped"]["continuation_unmatched"] += len(item_like) * N
        return
    rows = []
    for i, trow in zip(item_like, trows):
        rp = parse_item_line(lines[i], N, None, fixed=(trow.qty,))
        if rp is None:
            continue
        rp.line_no, rp.page = i, p["page"]
        rp.item_no, rp.desc, rp.unit_raw = trow.item_no, trow.desc, trow.unit_raw
        rp.qty_source, rp.section = trow.qty_source, trow.section
        rp.continuation_of = template["page"]
        rp.template_row = trow
        rows.append(rp)
    # the mapping must be proved by rows whose quantity is not 1
    tested = [(rp, k) for rp in rows if rp.qty != 1 for k in range(N)]
    ok = sum(1 for rp, k in tested if k in rp.kept)
    if not tested or ok < 0.5 * len(tested):
        info["notes"].append("p%d: mapping to p%d not confirmed by arithmetic (%d/%d)"
                             % (p["page"], template["page"], ok, len(tested)))
        info["dropped"]["continuation_unmatched"] += len(rows) * N
        return
    p["rows"] = rows
    totals = [i for i in p["prices"] if cls[i][1]["cells"] <= N]
    ttot = template["totals"]
    for i, t in zip(totals, ttot if len(ttot) == len(totals) else []):
        p["totals"].append((i, t[1], t[2], _total_cells(lines[i])))
    info["notes"].append("p%d: prices-only page mapped row for row to p%d (%d/%d qty!=1 "
                         "cells confirm)" % (p["page"], template["page"], ok, len(tested)))


def _record(base, page, rp, name, u, e, engine):
    """One bidder's price for one item, as rows_from_grid builds it, plus the
    OCR provenance."""
    desc = rp.desc or ""
    dn = pb.norm_desc(desc)
    mat = pb.MATERIAL_RE.search(dn)
    bidder_raw, col_alt = pb.split_alternate_caption(name)
    section = rp.section or "BASE BID"
    alt = pb.ALT_ITEM_RE.match(rp.item_no or "")
    if alt:
        section = "ALT " + alt.group(1).upper()
    return {
        "src_file": base,
        "src_sheet": "ocr_p%d" % page,
        "bid_section": col_alt or section,
        "item_no": rp.item_no or "",
        "description": desc,
        "desc_norm": dn,
        "item_family": pb.item_family(dn),
        "size_in": pb.extract_size_in(dn),
        "material": (mat.group(1).upper() if mat else ""),
        "unit_raw": rp.unit_raw or "",
        "unit": pb.norm_unit(rp.unit_raw or ""),
        # no unit read: the row stays in the letting's totals, and the empty
        # unit keeps it out of every unit-price table (build_master groups on
        # unit, estimate_low_bid skips a line without one) -- the same path
        # as parse_bidtabs' 'lump_sum_with_quantity'
        "unit_note": "" if rp.unit_raw else "ocr_unit_unread",
        "quantity": float(rp.qty),
        "qty_source": rp.qty_source or "",
        "bidder_raw": bidder_raw,
        "bidder": pb.canon_bidder(bidder_raw),
        "is_engineer_estimate": pb.ENGINEER_RE.search(bidder_raw) is not None,
        "unit_price": float(u),
        "extended_reported": float(e),
        "text_source": "ocr",
        "ocr_engine": engine,
        "ocr_check": ("columns_agree" if rp.qty_source == "implied_by_columns"
                      else "unit_equals_total" if float(rp.qty) == 1.0 else "qty_x_price"),
    }


# ------------------------------------------------------------ printed totals

def printed_total_check(pg, recs):
    """Each bidder's rows against the totals the tab prints for that bidder.

    A total row is read against the bidder columns of its page; where it has
    one value per column the values are the bidders' in order, otherwise each
    bidder's sum is looked for among them.  Compared with the bidder's sum
    over the total's own section, over all sections so far, and over
    everything: whichever the printed value equals (to the cent, +-$1) is
    reported, else the closest, with how many of the bidder's item cells
    were dropped -- a short sum then has its explanation beside it.
    """
    by = defaultdict(lambda: defaultdict(float))
    allsum = defaultdict(float)
    order_sections = defaultdict(list)
    for r in recs:
        b = r["bidder_raw"]
        by[b][r["bid_section"]] += r["extended_reported"]
        allsum[b] += r["extended_reported"]
        if r["bid_section"] not in order_sections[b]:
            order_sections[b].append(r["bid_section"])
    out = []
    for p in pg:
        names = p["names"]
        if not names:
            continue
        prev_line = -1
        for _i, cap, section, cells in p["totals"]:
            # the block a printed total closes: the rows between it and the
            # total above it (Midlothian prints one total per bid form, and
            # OCR lost the forms' banners)
            block = defaultdict(float)
            for rp in p["rows"]:
                if prev_line < rp.line_no < _i:
                    for k, (u, e) in rp.kept.items():
                        block[k] += e
            prev_line = _i
            vals = [c for c in cells if c.kind in ("num", "bad", "blank", "nobid")]
            aligned = len(vals) == len(names)
            prof = p.get("prof")
            if aligned and prof and len(prof) == len(names) and len(vals) >= 2:
                # One value per column is not enough: each must sit under its
                # own column.  A total row's type is bolder, so its offset from
                # the item rows' columns DRIFTS smoothly across the page
                # (Coppell: -6, -10, -14, -19); a value that JUMPS against its
                # neighbours belongs to another column (Midlothian's form-H
                # total lost a value: -4, +9.5, +4.5, -13.5 ...).
                step = (prof[len(prof) - 1][0] - prof[0][0]) / max(1, len(prof) - 1)
                dev = [c.col - (prof[k][1] - 10) for k, c in enumerate(vals)]
                if (any(abs(d) > 1.2 * step for d in dev)
                        or any(abs(b - a) > 0.5 * step for a, b in zip(dev, dev[1:]))):
                    aligned = False
            for k, nm in enumerate(names):
                if not nm or nm not in allsum:
                    continue
                if aligned:
                    c = vals[k]
                    printed = sorted(c.vals)[0] if c.kind == "num" and c.vals else None
                    cand_vals = [printed] if printed is not None else []
                else:
                    cand_vals = [v for c in vals if c.kind == "num" for v in c.vals]
                    printed = None
                secs = order_sections[nm]
                cum = 0.0
                options = {}
                if block.get(k):
                    options["rows above this total"] = block[k]
                for s in secs:
                    cum += by[nm][s]
                    if s == section:
                        options["section " + s] = by[nm][s]
                        options["sections through " + s] = cum
                options["all rows"] = allsum[nm]
                match = None
                for v in cand_vals:
                    for lab, sv in options.items():
                        if abs(sv - v) <= 1.0:
                            match = (lab, sv, v)
                            break
                    if match:
                        break
                if match:
                    out.append({"page": p["page"], "caption": cap, "bidder": nm,
                                "printed": match[2], "sum": match[1], "basis": match[0],
                                "status": "match"})
                elif aligned and printed is not None:
                    # judged on the block the total closes when there is one,
                    # else on the nearest of the other bases
                    if "rows above this total" in options:
                        lab = "rows above this total"
                        sv = options[lab]
                    else:
                        lab, sv = min(options.items(), key=lambda kv: abs(kv[1] - printed))
                    out.append({"page": p["page"], "caption": cap, "bidder": nm,
                                "printed": printed, "sum": sv, "basis": lab,
                                "status": "short" if sv < printed else "over"})
                elif aligned:
                    out.append({"page": p["page"], "caption": cap, "bidder": nm,
                                "printed": None, "sum": options.get("section " + section,
                                                                    allsum[nm]),
                                "basis": "section " + section, "status": "not printed / unreadable"})
                else:
                    out.append({"page": p["page"], "caption": cap, "bidder": nm,
                                "printed": None, "sum": allsum[nm], "basis": "all rows",
                                "status": "not read (%d of %d values legible, none is this "
                                          "bidder's sum)" % (len(cand_vals), len(names))})
    return out


# --------------------------------------------------------- file-level fields

_DATE_LABEL = re.compile(r"(?i)\b(?:bid\s*)?date\s*:\s*\S")
_NOT_BID_DATE = re.compile(r"(?i)start|complet|finish|award|issue|addend|print|sign|expir|effective")


def ocr_date_lines(strings):
    """The OCR lines a bid date may be read from: those with a bid-opening
    cue (parse_bidtabs.BID_DATE_T1), or a bare 'Date:' label.  The weak
    cue alone ('date' anywhere) also fires on Tarrant's 'Earliest Start
    Date 1/1/2022' and dated a November 2021 letting 2022-01-01."""
    out = []
    for ln in strings or []:
        if pb.BID_DATE_T1.search(ln) or (_DATE_LABEL.search(ln) and not _NOT_BID_DATE.search(ln)):
            out.append(ln)
    return out


def file_fields(rel_pdf, strings):
    """bid_date / bid_date_source / owner / source_set as parse_bidtabs'
    _parse_one_file would set them for this PDF, plus where the date came
    from.  rel_pdf is relative to data/external."""
    f = "external/" + rel_pdf
    rel_dir = f.rsplit("/", 1)[0]
    owner = rel_dir.split("/")[1] if "/" in rel_dir else ""
    bd, src, _every = pb.find_dates(ocr_date_lines(strings))
    if bd:
        src = src + "_ocr"
    base = Path(f).name
    fn = re.match(r"^(\d{4})-(\d{2})-(\d{2})_", base)
    if fn and not bd:
        y, mo, dd = int(fn.group(1)), int(fn.group(2)), int(fn.group(3))
        if 1 <= mo <= 12 and 1 <= dd <= 31 and 2000 <= y <= 2035:
            bd = "%04d-%02d-%02d" % (y, mo, dd)
            src = "filename_agenda_date_approx"
    if not bd:
        bn = re.match(r"^(20\d{2})-\d{1,4}(?=[^0-9]|$)", base)
        if bn:
            bd = "%s-01-01" % bn.group(1)
            src = "bid_number_year_approx"
    return {"src_file": f, "source_set": "external", "owner": owner,
            "bid_date": bd, "bid_date_source": src}


def finalize_like_main(recs):
    """The per-row fields parse_bidtabs.main() adds after parsing
    (unit_suspect, extended_calc, ext_mismatch, section_role), for the
    standalone --csv output only; when wired in, main() computes them."""
    alt_re = re.compile(r"\balt", re.I)
    nonbase = re.compile(r"\bdeduct|contingenc|\bextra\s+work\b|owner.?s?\s+reserve|"
                         r"\bforce\s+account", re.I)
    for r in recs:
        u, raw = (r.get("unit") or ""), (r.get("unit_raw") or "")
        r["unit_suspect"] = "Y" if (pb.NUMERIC_UNIT_RE.match(raw.strip())
                                    or (raw and u not in pb.KNOWN_UNITS)) else ""
        q, up, ext = r["quantity"], r["unit_price"], r["extended_reported"]
        r["extended_calc"] = (q * up) if q is not None else None
        r["ext_mismatch"] = ""
        if r["extended_calc"] is not None and ext is not None:
            if abs(r["extended_calc"] - ext) / max(abs(ext), 1.0) > 0.005:
                r["ext_mismatch"] = "Y"
        s = r.get("bid_section") or ""
        r["section_role"] = ("alternate" if alt_re.search(s) else
                             "other" if nonbase.search(s) else "base")
    return recs


def yield_detail(text):
    """What one sidecar text yields: cells kept, cells kept WITH a unit,
    bidders named, and the usable-cell score yield_score sorts on."""
    recs, _audit, _strings, info = parse_text(text)
    with_unit = sum(1 for r in recs if r["unit"])
    return {"cells": len(recs), "with_unit": with_unit, "bidders": len(info["bidders"]),
            "usable": with_unit + (len(recs) - with_unit) / 2.0}


def yield_score(text):
    """(usable cells, bidders named) for one sidecar text -- a sort key for
    choosing between OCR engines' readings of one scan.  ocr_verify's score
    (rows with ANY balanced pair) cannot see a reading that loses bidder
    names or empty-cell markers: NRH 4690 keeps 43 cells from its Windows
    reading and 24 from the Vision one that ocr_scans.py chose.

    A cell counts in full when its row has a unit and half when it has none:
    a price without a unit stays in the letting's totals but never reaches a
    unit-price table.  Coppell 2023-7089's Windows reading keeps 340 cells,
    only 96 with a unit (the unit column printed too small for that engine),
    its Tesseract reading 320, all with units: 218 against 320."""
    d = yield_detail(text)
    return d["usable"], d["bidders"]


def parse_sidecar(path, stats=None, implied_qty=False):
    """Parse one sidecar file: (recs, audit, strings, info).  recs carry the
    PDF's basename in src_file, as read_pdf_coords' do; parse_bidtabs'
    _parse_one_file replaces it with the relative path."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_text(text, src_name=path.name[:-4] if path.name.endswith(".txt") else path.name,
                      stats=stats, implied_qty=implied_qty)


# ------------------------------------------------------------------ report

CSV_COLS = ["src_file", "source_set", "owner", "src_sheet", "bid_date", "bid_date_source",
            "bid_section", "section_role", "item_no", "description", "desc_norm", "item_family",
            "size_in", "material", "unit_raw", "unit", "quantity", "qty_source", "bidder_raw",
            "bidder", "is_engineer_estimate", "unit_price", "extended_reported", "extended_calc",
            "ext_mismatch", "unit_suspect", "unit_note", "text_source", "ocr_engine",
            "ocr_check"]


def _fmt_money(v):
    return "-" if v is None else "{:,.2f}".format(v)


def run_all(files=None, implied_qty=False):
    files = files or sorted(OCR_DIR.rglob("*.txt"))
    results = []
    for fp in files:
        rel = fp.relative_to(OCR_DIR).as_posix()[:-4]
        recs, audit, strings, info = parse_sidecar(fp, implied_qty=implied_qty)
        ff = file_fields(rel, strings)
        for r in recs:
            r.update(ff)
        finalize_like_main(recs)
        results.append((rel, recs, info, ff))
    return results


def report(csv_path=None, show=None, implied_qty=False):
    files = sorted(OCR_DIR.rglob("*.txt"))
    if show:
        files = [f for f in files if show in f.relative_to(OCR_DIR).as_posix()]
    if not files:
        print("no OCR sidecars found under %s" % OCR_DIR)
        return 1
    results = run_all(files, implied_qty=implied_qty)
    if implied_qty:
        print("** --implied-qty: rows whose quantity no printed reading balances may take a "
              "quantity implied by 2+ agreeing bidder columns (qty_source=implied_by_columns) **")
    allrecs = []
    print("OCR sidecars: %d   (gate: qty x unit price == printed total within %.1f%%)\n"
          % (len(results), TOL * 100))
    for rel, recs, info, ff in results:
        allrecs.extend(recs)
        print("=" * 100)
        print(rel)
        print("  engine: %s | pages %d | item rows %d | bid_date %s (%s)"
              % (info["engine"], info["pages"], info["item_rows"], ff["bid_date"] or "-",
                 ff["bid_date_source"]))
        print("  status: %s" % info["status"])
        dropped = sum(info["dropped"].values())
        print("  rows kept: %d | dropped: %d %s | no-bid cells (not drops): %d"
              % (info["kept"], dropped,
                 ("(" + ", ".join("%s %d" % (k, v) for k, v in info["dropped"].most_common()) + ")")
                 if dropped else "", info["no_bid_cells"]))
        if info["kept_if_named"]:
            print("  cells that balanced but have no recoverable bidder name: %d"
                  % info["kept_if_named"])
        for pi in info["page_info"]:
            if not (pi["item_rows"] or pi["names"] or pi["band"]):
                continue
            print("  p%-2d %2s cols, %3d item rows, %3d kept | names: %s"
                  % (pi["page"], pi["n_slots"] or "-", pi["item_rows"], pi["kept"],
                     ("; ".join(n or "(no name: column dropped)" for n in pi["names"])
                      + "  [" + pi["names_how"] + "]") if pi["names"]
                     else "NOT RECOVERED (%s)%s" % (pi["names_how"],
                                                   ("  band: " + pi["band"]) if pi["band"] else "")))
        if info["bidders"]:
            print("  bidders found (%d): %s" % (len(info["bidders"]), "; ".join(info["bidders"])))
        if info["totals"]:
            print("  printed-total check:")
            for t in info["totals"]:
                print("    p%-2d %-22s %-38s printed %14s  rows %14s  %s (%s)"
                      % (t["page"], t["caption"][:22], t["bidder"][:38], _fmt_money(t["printed"]),
                         _fmt_money(t["sum"]), t["status"], t["basis"]))
        if info["kept"]:
            print("  units: %d of %d cells have one%s; %d unread (unit '', unit_note "
                  "ocr_unit_unread)" % (
                      info["units"]["read"], info["kept"],
                      (" (" + ", ".join("%s %d" % kv for kv in info["unit_how"].most_common()) + ")")
                      if info["unit_how"] else "", info["units"]["unread"]))
        if info["desc_modes"]:
            print("  wrapped descriptions: %s" % ", ".join(
                "p%d %s" % kv for kv in sorted(info["desc_modes"].items())))
        for n in info["notes"]:
            print("  note: %s" % n)
        if show:
            for r in recs:
                print("    %-6s %-8s %-38s %10s %-6s %12s %14s  %s"
                      % (r["src_sheet"], r["item_no"], r["description"][:38], "%g" % r["quantity"],
                         r["unit_raw"][:6], _fmt_money(r["unit_price"]),
                         _fmt_money(r["extended_reported"]), r["bidder_raw"][:30]))
            for ln in info["lines_not_parsed"]:
                print("    (not an item row) %s" % ln)
    # summary table
    print("\n" + "=" * 100)
    print("%-62s %5s %5s %5s %7s  %s" % ("file", "kept", "drop", "nobid", "bidders", "status"))
    tk = td = 0
    reasons = Counter()
    for rel, recs, info, ff in results:
        d = sum(info["dropped"].values())
        tk += info["kept"]
        td += d
        reasons.update(info["dropped"])
        print("%-62s %5d %5d %5d %7d  %s" % (rel[-62:], info["kept"], d, info["no_bid_cells"],
                                             len(info["bidders"]), info["status"]))
    print("%-62s %5d %5d" % ("TOTAL", tk, td))
    print("dropped by reason: %s" % (", ".join("%s %d" % kv for kv in reasons.most_common()) or "-"))
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(allrecs)
        print("-> %s (%d rows)" % (csv_path, len(allrecs)))
    return 0


# ---------------------------------------------------------------- selftest

def _sidecar(*pages, engine="Windows.Media.Ocr (en-US)", src="test/x.pdf"):
    head = ("### MACHINE-READ (OCR) -- not a text layer from the PDF.\n"
            "### Source: %s\n### Engine: %s, page rendered at 300 dpi.\n"
            "### Digits here are NOT reliable enough to bid from directly.\n"
            "### Verify any unit price against the PDF by eye before use.\n\n" % (src, engine))
    return head + "\n\n".join("### PAGE %d\n%s" % (i + 1, p) for i, p in enumerate(pages))


def _selftest():
    ok = True
    fails = []

    def check(name, cond, extra=""):
        nonlocal ok
        if not cond:
            ok = False
            fails.append("FAIL %s %s" % (name, extra))

    # -- the confusions, one cell at a time
    def readings_of(t):
        """Each unit's alternative readings: [[cell values, ...], ...]."""
        units = _units(scan(deoh(t)))
        return [[[sorted(c.vals) for c in alt] for alt in u] for u in units]
    cases = [
        ("$41 ,500.oo", [[[[41500.0]]]]),          # oo, space before the comma
        ("s 43,500.00", [[[[43500.0]]]]),          # s for $
        ("$1 ,650.oo", [[[[1650.0]]]]),            # both
        ("S14.40", [[[[14.4]]]]),                  # S for $
        ("$11,ooo.oo", [[[[11000.0]]]]),
        ("so.oo", [[[[0.0]]]]),                    # $0.00
        ("1*000.00", [[[[1000.0]]]]),              # * for ,
        ("201 t600.oo", [[[[201600.0]]]]),         # ' t' for ,
        ("5, 170.00", [[[[5170.0]]]]),             # space after the comma
        ("11 .40", [[[[11.4]]]]),                  # space before the point
        # ',' read as '.': one number -- or two glued cells; both offered
        ("148.050.00", [[[[148050.0]], [[148.05], [0.0]]]]),
        # ',' read as '1': both values offered, the arithmetic picks
        ("11200.00", [[[[1200.0, 11200.0]]]]),
        # two cells glued: '$2.00' + '1,750.00' (no one-number reading parses)
        ("$2.001,750.oo", [[[[2.0], [1750.0]]]]),
        # two columns a space apart stay two cells
        ("12.00 17,100.00", [[[[12.0]]], [[[17100.0]]]]),
    ]
    for t, want in cases:
        got = readings_of(t)
        check("confusion %r" % t, got == want, "got %r" % got)
    j = readings_of("$130.00 1 18,430.00")         # lost leading digit group
    check("leading group join", j[-1] == [[[18430.0, 118430.0]]], "got %r" % j)
    check("handwritten garbage stays unread",
          seg_values("coo o") is None and seg_values("3Oo") is None
          and not readings_of("coo o      3Oo"))

    # -- a Rowlett-shaped page: names packed on one line, address lines, a
    #    blank-marked no-bid, a misread total, a handwritten-looking row
    page = "\n".join([
        "              TABULATION OF BIDS                               BID OF            BIDOF              BIDOF",
        "                       Date: March 26, 2021        Acme Paving, LLC    Texas Civil Construction AXIS Contracting, Inc.",
        "                                                   106 Hillside Drive  121 Alexander Lane   P.O. Box 360715",
        "                                                   Lewisville, Texas 75057 Royse City, Texas Dallas, Texas 75336",
        "     tem   Quantities Unit           Descri tion",
        "     1          1 L.S.Mobilization Including Bonds   $32,150.00   32,150.00 $43,500.00 s 43,500.00 $41 ,500.oo s 41,500.00",
        "     2      3,536S.Y.Remove & Dis ose of Concrete    $14.00 49,504.00 $12.00 s 42,432.00$16.00s   56,576.00",
        "                Furnish & Install Reinforced Concrete Curb",
        "     3        875 L.F.and Gutter                     $2.001,750.oo   $5.00s     4,375.00     $8.00     7,000.00",
        "     4         50 L.F.Remove & Reset Existing Fence   $15.00      750.00   $40.00   2,0O0.00    $10.00 1 ,500.oo",
        "     5        911 L.F.Furnish 21-Inch RCP            $130.00 1 18,430.00 $90.00 81,990.00  $75.00    68,325.00",
        "     6          1 L.S.Furnish & Implement SW3P        $1,200.00 s ,200.oo $1 ,650.oo   1,650.00  $4,808.00   4,808.00",
        "     7          coo o  Handwritten row                 coo o      3Oo",
        "                                          SUBTOTAL     203,784.00         s 175,947.00            s 178,709.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(page))
    names = info["page_info"][0]["names"]
    check("packed names split at firm words",
          names == ["Acme Paving, LLC", "Texas Civil Construction", "AXIS Contracting, Inc."],
          "got %r" % names)
    byk = {(r["item_no"], r["bidder_raw"]): r for r in recs}
    check("row 1 three bidders", all(("1", n) in byk for n in names or []))
    check("oo + space repaired", byk.get(("1", "AXIS Contracting, Inc."), {}).get("unit_price") == 41500.0)
    check("glued cells split", byk.get(("3", "Acme Paving, LLC"), {}).get("extended_reported") == 1750.0)
    check("wrapped description joined",
          byk.get(("3", "Acme Paving, LLC"), {}).get("description", "").startswith("Furnish & Install"))
    check("unit read beside qty", byk.get(("2", "Acme Paving, LLC"), {}).get("unit") == "SY")
    check("'1 18,430.00' read as 118,430.00",
          byk.get(("5", "Acme Paving, LLC"), {}).get("extended_reported") == 118430.0)
    check("lost '1' in ',200.oo' dropped, not repaired", ("6", "Acme Paving, LLC") not in byk)
    check("'2,0O0.00' repaired", byk.get(("4", "Texas Civil Construction"), {}).get("unit_price") == 40.0)
    check("handwritten row emits nothing", not any(r["item_no"] == "7" for r in recs))
    check("provenance marked", all(r["text_source"] == "ocr" and r["ocr_engine"].startswith("Windows")
                                   for r in recs))
    check("ocr_check split", byk.get(("1", "Acme Paving, LLC"), {}).get("ocr_check") == "unit_equals_total"
          and byk.get(("2", "Acme Paving, LLC"), {}).get("ocr_check") == "qty_x_price")
    check("drop reasons counted", info["dropped"].get("unreadable_amount", 0) >= 1, repr(info["dropped"]))
    tot = {t["bidder"]: t for t in info["totals"]}
    check("printed total checked", tot.get("Texas Civil Construction", {}).get("status") == "match",
          repr(tot.get("Texas Civil Construction")))

    # -- a lost empty cell must not shift prices onto the wrong bidder
    page2 = "\n".join([
        "                                    ALPHA CONSTRUCTION LLC   BETA PAVING INC   GAMMA CONCRETE LLC",
        "   DESCRIPTION            QTY UNIT    UNIT PRICE  TOTAL   UNIT PRICE  TOTAL   UNIT PRICE  TOTAL",
        "   1 Remove curb          100 LF      s           s        12.00 s  1,200.00   15.00 s  1,500.00",
        "   2 Remove walk          200 SF                           10.00    2,000.00   11.00    2,200.00",
        "   3 Sod                  300 SY       2.00      600.00     3.00      900.00    4.00    1,200.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(page2))
    byk = {(r["item_no"], r["bidder_raw"]): r for r in recs}
    check("blank markers hold the column",
          byk.get(("1", "BETA PAVING INC"), {}).get("unit_price") == 12.0
          and ("1", "ALPHA CONSTRUCTION LLC") not in byk)
    check("unmarked lost cells -> ambiguous, not shifted",
          not any(k[0] == "2" for k in byk) and info["dropped"].get("ambiguous_column", 0) == 2,
          repr(info["dropped"]))
    check("row 3 all bidders", sum(1 for k in byk if k[0] == "3") == 3)

    # -- names that cannot be split uniquely leave the file out
    page3 = "\n".join([
        "      DESCRIPTION              Atlas Utility Supply Co Consolidated Pipe and Supply North Texas Winwater",
        "      SECTION A     EST. QTY UOM UNIT BID PRICE AMOUNT BID UNIT BID PRICE AMOUNT BID UNIT BID PRICE AMOUNT BID",
        "        1 Water Pipe            40 LF    10.00   400.00   11.00   440.00   12.00   480.00",
        "        2 Sewer Pipe            20 LF    20.00   400.00   21.00   420.00   22.00   440.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(page3))
    check("unrecoverable names -> no rows", not recs and info["kept_if_named"] == 6
          and info["status"].startswith("left out"), repr(info["status"]))

    # -- glued item numbers resolved by the sequence; lump sum in words
    page4 = "\n".join([
        "                                   ONE CONSTRUCTION LLC        TWO PAVING INC",
        "   Item No. Item Description Quantity Unit  Unit Price Total Price Unit Price Total Price",
        "     1   Mobilization   Lump Sum LS        4,000.00   4,000.00   5,000.00   5,000.00",
        "    224\" RCP Storm Drain          40 LF     100.00   4,000.00   110.00   4,400.00",
        "    310\" Gate Valve                2 EA      500.00   1,000.00   550.00   1,100.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(page4))
    byk = {(r["item_no"], r["bidder_raw"]): r for r in recs}
    check("lump sum words -> qty 1", byk.get(("1", "ONE CONSTRUCTION LLC"), {}).get("quantity") == 1.0
          and byk.get(("1", "ONE CONSTRUCTION LLC"), {}).get("qty_source") == "lump_sum_words")
    check("glued item '224\"' = item 2, 24\"",
          byk.get(("2", "TWO PAVING INC"), {}).get("description", "").startswith('24"'),
          repr([(k, v["description"]) for k, v in byk.items()]))
    check("glued item '310\"' = item 3", ("3", "ONE CONSTRUCTION LLC") in byk)

    # -- a prices-only continuation page maps row for row, or not at all
    p1 = "\n".join([
        "                               AAA CONSTRUCTION LLC      BBB PAVING INC",
        "  Item No. Item Description Quantity Unit Unit Price Total Price Unit Bid Price Amount Bid",
        "    1    Excavation    1425 LF          12.00   17,100.00     6.45   9,191.25",
        "    2    Concrete Trail 1425 LF         77.00  109,725.00    95.30 135,802.50",
        "    3    Mobilization  1 LS          9,000.00    9,000.00  4,000.00   4,000.00",
        "  TOTAL-BASE BID                               135,825.00          148,993.75",
    ])
    p2 = "\n".join([
        "      CCC CONSTRUCTION, INC.        DDD CONCRETE LLC",
        "     Unit Bid PriceAmount Bid    Unit Bid PriceAmount Bid",
        "            4.78        6,811.50        10.52      14,991.00",
        "           86.72      123,576.00        79.70     113,572.50",
        "        5,000.00        5,000.00     3,000.00       3,000.00",
        "                      135,387.50                  131,563.50",
    ])
    recs, audit, strings, info = parse_text(_sidecar(p1, p2))
    byk = {(r["item_no"], r["bidder_raw"]): r for r in recs}
    check("continuation page mapped", byk.get(("2", "DDD CONCRETE LLC"), {}).get("unit_price") == 79.7
          and byk.get(("2", "DDD CONCRETE LLC"), {}).get("description") == "Concrete Trail",
          repr(sorted(byk)))
    tot = [t for t in info["totals"] if t["bidder"] == "CCC CONSTRUCTION, INC."]
    check("continuation totals checked", tot and tot[0]["status"] == "match", repr(tot))
    p2bad = p2.replace("           86.72      123,576.00        79.70     113,572.50\n", "")
    recs, audit, strings, info = parse_text(_sidecar(p1, p2bad))
    check("continuation with a row missing is refused",
          not any(r["src_sheet"] == "ocr_p2" for r in recs)
          and info["dropped"].get("continuation_unmatched", 0) > 0, repr(info["dropped"]))

    # -- Tesseract reads table rules as characters; its '1.0' can lose the
    #    point; a '$' can carry debris; a decimal point can read as a comma
    check("comma for decimal point", readings_of("$ 78,000,00") == [[[[78000.0]]]],
          repr(readings_of("$ 78,000,00")))
    check("'$ =1,171,929.00'", readings_of("$ =1,171,929.00") == [[[[1171929.0]]]],
          repr(readings_of("$ =1,171,929.00")))
    tess = "\n".join([
        "                                                 ALPHA PAVING LLC        BETA CONSTRUCTION INC",
        "   ITEM  DESCRIPTION                  QTY  UNIT  UNIT COST   TOTAL       UNIT COST   TOTAL",
        "    201|Remove & Dispose Pavement   11,844 sy   $12.50 | $ 148,050.00 $12.00] $ 142,128.00",
        "    202|Remove Sidewalk              3,224 sy    $7.50}$24,180.00 $8.00 | $ 25,792.00",
        "    203 [Unclassified Excavation     3,525 cy   $38.00 | $ 133,950.00 $30.00 | $ 105,750.00",
        "    204|Mobilization                    4  Ls   $5,000.00 | $ 5,000.00 $6,000.00 | $ 6,000.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(tess))
    byk = {(r["item_no"], r["bidder_raw"]): r for r in recs}
    check("item glued to a rule '201|Remove'", ("201", "ALPHA PAVING LLC") in byk
          and byk[("201", "ALPHA PAVING LLC")]["extended_reported"] == 148050.0, repr(sorted(byk)))
    check("'4 Ls' (1.0 LS misread) read as a lump sum of 1",
          byk.get(("204", "BETA CONSTRUCTION INC"), {}).get("quantity") == 1.0
          and byk.get(("204", "BETA CONSTRUCTION INC"), {}).get("description") == "Mobilization",
          repr(byk.get(("204", "BETA CONSTRUCTION INC"))))

    # -- names matched to price columns by position: the engineer's column
    #    has no name on the names line and is dropped, not shifted
    pos = "\n".join([
        "                                       ENGINEER EST        BID 1                BID 2",
        "   No  Work            Qty  Unit   Unit Price  Total   Unit Price  Total    Unit Price  Total",
        "                                                 Beavers Contracting           ERS",
        "    1 Clearing         1.0 LS   10,000.00 $ 10,000.00 82,000.00 $ 82,000.00 64,660.00 $ 64,660.00",
        "    2 Riprap       1,520.0 TON     200.00 $304,000.00    108.00 $164,160.00    160.00 $243,200.00",
        "    3 Earthfill    7,397.0 CY       15.00 $110,955.00     10.00 $ 73,970.00     27.00 $199,719.00",
        "    4 Seeding         12.2 AC    5,000.00 $ 61,000.00  4,000.00 $ 48,800.00  4,500.00 $ 54,900.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(pos))
    names = info["page_info"][0]["names"]
    check("positional names, unnamed first column",
          names == [None, "Beavers Contracting", "ERS"], repr(names))
    check("unnamed column's cells counted, not emitted",
          info["dropped"].get("no_bidder_names", 0) == 4 and len(recs) == 8,
          repr((info["dropped"], len(recs))))

    # -- an alternate block under a SUBTOTAL: glued banner, '5A' items
    alt = "\n".join([
        "                                              ONE PAVING LLC            TWO CONSTRUCTION INC",
        "   ITEM QTY UNIT DESCRIPTION                  UNIT PRICE  BID VALUE    UNIT PRICE  BID VALUE",
        "     1   1   LS  MOBILIZATION                 $5,000.00   $5,000.00    $6,000.00   $6,000.00",
        "     5   3   EA  RELOCATE IRRIGATION            $920.00   $2,760.00    $2,480.00   $7,440.00",
        "         SUBTOTAL AMOUNT-Base Bid:                         $7,760.00               $13,440.00",
        "   AddAlternateEast parkway of S.Waterford OaksDrive",
        "    5A   5   EA  RELOCATE IRRIGATION            $920.00   $4,600.00    $2,310.00  $11,550.00",
        "         SUBTOTAL AMOUNT-Add Alternate:                    $4,600.00               $11,550.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(alt))
    fin = {(r["item_no"], r["bidder_raw"]): r for r in finalize_like_main(recs)}
    check("alternate after a subtotal", fin.get(("5A", "ONE PAVING LLC"), {}).get("section_role")
          == "alternate" and fin.get(("5", "ONE PAVING LLC"), {}).get("section_role") == "base",
          repr([(k, v["bid_section"]) for k, v in fin.items()]))
    check("both subtotals reconcile", sum(1 for t in info["totals"] if t["status"] == "match") == 4,
          repr([(t["caption"], t["bidder"], t["status"]) for t in info["totals"]]))

    # -- a quantity OCR lost: dropped by default; implied only on request
    noq = "\n".join([
        "                                     AAA CONCRETE LLC     BBB PAVING INC     CCC CONSTRUCTION LLC",
        "   ITEM  DESCRIPTION          UNIT   UNIT   TOTAL        UNIT   TOTAL        UNIT    TOTAL",
        "    11   24IN RCP              LF    125.00  13,750.00   129.00  14,190.00   237.00  26,070.00",
        "    12   INLET                 EA  4,000.00   8,000.00 3,900.00   7,800.00 4,100.00   8,200.00",
        "    13   SOD                   SY      5.00     500.00     6.00     600.00     7.00     700.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(noq))
    check("no printed quantity -> nothing kept by default", not recs, repr(len(recs)))
    recs, audit, strings, info = parse_text(_sidecar(noq), implied_qty=True)
    r1 = [r for r in recs if r["item_no"] == "11"]
    check("--implied-qty: q* from agreeing columns, marked",
          len(r1) == 3 and all(r["quantity"] == 110.0 and r["qty_source"] == "implied_by_columns"
                               and r["ocr_check"] == "columns_agree" for r in r1), repr(r1[:1]))

    # -- debris that looks like names is refused, not emitted
    junk = "\n".join([
        "                              eBid                    a*DUNAWAY       company",
        "                              Serle eee",
        "   ITEM  DESCRIPTION   QTY UNIT   UNIT   TOTAL     UNIT    TOTAL",
        "     1   SOD            10  SY    5.00    50.00     6.00    60.00",
        "     2   SEED           10  SY    1.00    10.00     2.00    20.00",
        "     3   MULCH          10  SY    3.00    30.00     4.00    40.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(junk))
    check("OCR debris is no set of bidder names", not recs, repr(info["page_info"][0]["names"]))
    check("form labels are no names", not valid_name("Co-Op:") and not valid_name("CO-OP - Yes")
          and not valid_name("Primary Award") and valid_name("ERS") and valid_name("Ratliff Hardscape"))

    # -- descriptions by vertical position (assign_wrapped_lines)
    #    centred cells (Tarrant F2025096): two lines over the price row and
    #    two under it; a package total keeps its own lines; a line ending
    #    'for' runs on under its row
    centred = "\n".join([
        "                                                    ALPHA CONCRETE LLC     BETA BUILDERS INC",
        "   Line#  Description                       QTY UOM  UnitPrice  Extended  UnitPrice  Extended",
        "          Package 2",
        "          HEADWALLS FOR NEW CULVERTS",
        "     2                                                          $40,000.00            $50,000.00",
        "          Concrete installed under this item shall be",
        "          measured by the cubic yard in place.",
        "          Headwalls for Culvert #4 Crossing.",
        "          Unit price for concrete material and supplies",
        "   2.1                                        119 C.Y.  $100.00  $11,900.00  $150.00  $17,850.00",
        "          Examples are: walls, headwalls, junction boxes,",
        "          etc. that are not in other line items.",
        "          Labor Rate for Concrete Services",
        "   2.2                                        480 Hour   $50.00  $24,000.00   $60.00  $28,800.00",
        "          Regular Hourly Rate",
        "          Cost of: Payment Bond and Performance Bond for",
        "   2.3                                          1 LS  $3,700.00   $3,700.00 $4,000.00  $4,000.00",
        "          New Culverts at Tinsley Lane",
        "   2.4    Cost of: Maintenance Bond             1 LS  $1,900.00   $1,900.00 $2,000.00  $2,000.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(centred))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("centred cell: lines above and below", dd.get("2.1") ==
          "Headwalls for Culvert #4 Crossing. Unit price for concrete material and supplies "
          "Examples are: walls, headwalls, junction boxes, etc. that are not in other line items.",
          repr(dd.get("2.1")))
    check("centred cell: next item keeps its lines",
          dd.get("2.2") == "Labor Rate for Concrete Services Regular Hourly Rate", repr(dd.get("2.2")))
    check("a line ending 'for' runs on under its row",
          dd.get("2.3") == "Cost of: Payment Bond and Performance Bond for New Culverts at Tinsley Lane"
          and dd.get("2.4") == "Cost of: Maintenance Bond", repr((dd.get("2.3"), dd.get("2.4"))))
    #    an open parenthesis runs on to the line that closes it (Murphy 2023:
    #    '(DR 9, 250 PSI' / own text / '... PVC C900)' is one item)
    paren = "\n".join([
        "                                                      ALPHA LLC              BETA INC",
        "   ITEM  DESCRIPTION                       UNIT QTY  UNIT PRICE   TOTAL   UNIT PRICE   TOTAL",
        "    7    Block Sodding                      SY  900     $20.00 $18,000.00   $12.20 $10,980.00",
        "         8 inch Water Pipe by Pipe Bursting (DR 9, 250 PSI",
        "    8    HDPE C906; DR 14 Fusible PVC or DR 14 LF 4800 $120.00 $576,000.00  $87.00 $417,600.00",
        "         Certa-Lok Restrained Joint PVC C900)",
        "    9    Trench Safety                      LF  500      $4.00  $2,000.00   $10.00  $5,000.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(paren))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("an open parenthesis runs on to its close",
          dd.get("8", "").endswith("Certa-Lok Restrained Joint PVC C900)")
          and dd.get("9") == "Trench Safety", repr(dd))
    #    the same item on two bidders' pages: the copy whose wrapped lines the
    #    text decided beats a longer one that was a tie (F2025096 3.8: '5
    #    workers x 4 days x 8 hrs' closes 3.7 where OCR kept its period)
    def _copy(page, desc, conf):
        rp = RowParse()
        rp.item_no, rp.qty, rp.unit_raw, rp.section, rp.page = "3.8", 1.0, "LS", "BASE BID", page
        rp.desc, rp.desc_conf = desc, conf
        return rp
    decided = _copy(3, "Project Cost of: Payment Bond for Headwalls", 0.4)
    tie = _copy(4, "5 workers x 4 days x 8 hrs Project Cost of: Payment Bond for Headwalls", 0.0)
    _reconcile_descriptions([{"rows": [decided]}, {"rows": [tie]}], {"notes": []})
    check("a decided copy beats a tie", tie.desc == decided.desc
          and decided.desc.startswith("Project Cost of:"), repr(tie.desc))
    #    Markum 1.5/1.6: a price row with no text of its own sits between its
    #    two lines; the next row's own one-line text stays alone
    markum = "\n".join([
        "                                                 ALPHA PAVING LLC      BETA CIVIL INC",
        "     Line # Description                          QTY  UOM    Unit   Extended   Unit  Extended",
        "      1.4   Irrigation Allowance (NCTCOG 502.1)   1   LS  $10,000.00 $10,000.00 $9,000.00 $9,000.00",
        "            Temporary Erosion, Sedimentation and Storm Water Pollution Prevention",
        "      1.5                                         1   LS  $14,000.00 $14,000.00 $12,000.00 $12,000.00",
        "            Plan (SWPPP) (TxDOT 506)",
        "      1.6   Project Sign (NCTCOG 107.21)          2   EA     $600.00  $1,200.00   $700.00  $1,400.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(markum))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("own-less row takes the line under it",
          dd.get("1.5", "").endswith("Prevention Plan (SWPPP) (TxDOT 506)")
          and dd.get("1.6") == "Project Sign (NCTCOG 107.21)", repr(dd))
    #    bottom-aligned cells (Rowlett): the numbers sit on a cell's LAST
    #    line, so every wrapped line belongs to the row below it
    bottom = "\n".join([
        "                                                         ALPHA PAVING LLC     BETA CIVIL INC",
        "     Item  Qty   Unit Description",
        "      1      1  L.S. Mobilization Including Bonds       $5,000.00   5,000.00 $6,000.00   6,000.00",
        "                     Furnish & Install 6-Inch Flexible Base (TxDOT Item",
        "                     247) Over Mechanically Compacted",
        "      2  4,131  S.Y. Subgrade                            $16.50  68,161.50   $24.00  99,144.00",
        "                     Furnish & Install Reinforced Concrete Pavement",
        "      3  3,110  S.Y. 8-Inch Thick                        $77.00 239,470.00   $74.00 230,140.00",
        "      4     33  S.Y. Remove Sidewalk                     $12.00     396.00    $6.00     198.00",
        "                     Furnish & Install Concrete Driveway Pavement",
        "                     Six Inches Thick Over Two Inches of Flexible",
        "      5  1,365  S.Y. Base                                $79.00 107,835.00   $74.50 101,692.50",
    ])
    recs, audit, strings, info = parse_text(_sidecar(bottom))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("bottom-aligned page: wrapped lines go down",
          dd.get("2") == "Furnish & Install 6-Inch Flexible Base (TxDOT Item 247) Over Mechanically "
                         "Compacted Subgrade"
          and dd.get("3") == "Furnish & Install Reinforced Concrete Pavement 8-Inch Thick"
          and dd.get("4") == "Remove Sidewalk"
          and dd.get("5") == "Furnish & Install Concrete Driveway Pavement Six Inches Thick Over Two "
                             "Inches of Flexible Base"
          and dd.get("1") == "Mobilization Including Bonds", repr(dd))
    #    the item number alone on the line over its row (Tarrant F2023177),
    #    and a group heading with no prices that keeps its own text
    labels = "\n".join([
        "                                                       GROD CONSTRUCTION LLC   ACME LLC",
        "     Line#    Description                              QTY  UOM   Unit   Extended   Unit  Extended",
        "        1     Base Bid                                  1    EA",
        "        2     TxDot Spec 500 - Mobilization             1    LS  $27,000.00 $27,000.00 $30,000.00 $30,000.00",
        "              TxDot Spec 100 - Preparation of Right-of-Way, work fully",
        "        3",
        "              performed                                 1    LS  $24,000.00 $24,000.00 $25,000.00 $25,000.00",
        "        4     DRAINAGE AND ROADWAY IMPROVEMENTS          1    EA",
        "              TxDot Spec 110 - Unclassified Roadway Excavation,",
        "        5",
        "              work fully performed                    175    CY    $66.00 $11,550.00 $70.00 $12,250.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(labels))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("lone item label goes to its row; heading keeps its text",
          dd.get("3") == "TxDot Spec 100 - Preparation of Right-of-Way, work fully performed"
          and dd.get("5") == "TxDot Spec 110 - Unclassified Roadway Excavation, work fully performed",
          repr(dd))
    #    table rules found on the image are hard cell edges: no cue says
    #    where 'Haul-Off Included' belongs, the rule does
    rules = "\n".join([
        "                                                ALPHA LLC             BETA INC",
        "   ITEM  DESCRIPTION                  QTY UNIT  UNIT PRICE  TOTAL   UNIT PRICE  TOTAL",
        RULE_LINE,
        "    1   Excavation                    100 CY     10.00   1,000.00    11.00   1,100.00",
        "        Unsuitable Material",
        "        Haul-Off Included",
        RULE_LINE,
        "        Embankment",
        "    2   Compacted Fill                200 CY      5.00   1,000.00     6.00   1,200.00",
        RULE_LINE,
        "    3   Sod                           300 SY      2.00     600.00     3.00     900.00",
        RULE_LINE,
    ])
    recs, audit, strings, info = parse_text(_sidecar(rules))
    dd = {r["item_no"]: r["description"] for r in recs}
    check("rules are cell edges",
          dd.get("1") == "Excavation Unsuitable Material Haul-Off Included"
          and dd.get("2") == "Embankment Compacted Fill" and dd.get("3") == "Sod"
          and info["page_info"][0]["names"] == ["ALPHA LLC", "BETA INC"]
          and RULE_LINE not in strings, repr((dd, info["page_info"][0]["names"])))

    #    a section heading at the item column is no part of the next row;
    #    a cell's first line carries the item number (Frisco 2003-047)
    frisco = "\n".join([
        "                                                    ALPHA BUILDERS LLC       BETA WORKS INC",
        "    NO.  DESCRIPTION                     UNIT  QTY   UNIT PRICE   AMOUNT    UNIT PRICE   AMOUNT",
        "     A   GENERAL ITEMS",
        "     1   GENERAL SITE PREPARATION          LS    1   $ 2,000.00 $ 2,000.00 $ 6,500.00 $ 6,500.00",
        "     2   TRENCH SAFETY                     LF  2,867  $  5.00 $ 14,335.00  $  2.00 $ 5,734.00",
        "     B   STEWART CREEK WEST REUSE PUMP STATION",
        "     1   DEMOLITION OF EXISTING STEWART CREEK WEST",
        "         REUSE AND BOOSTER PUMP STATIONS   LS    1  $ 40,000.00 $ 40,000.00 $ 29,000.00 $ 29,000.00",
        "     2   CONCRETE PAVING                   SY  1,710  $ 90.00 $ 153,900.00  $ 91.00 $ 155,610.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(frisco))
    dd = {(r["item_no"], r["quantity"]): r["description"] for r in recs}
    check("section heading kept out; first line gives the item number",
          dd.get(("1", 1.0)) == "DEMOLITION OF EXISTING STEWART CREEK WEST REUSE AND BOOSTER PUMP STATIONS",
          repr(dd))
    #    letter-coded items OCR read with letters for digits, by sequence
    alpha = "\n".join([
        "                                          ALPHA LLC                BETA INC",
        "   Item No.  Description             QTY UNIT  Unit Price  Item Cost  Unit Price  Item Cost",
        "   D3   Gate Valve                    2  EA    $300.00    $600.00    $350.00    $700.00",
        "   D4   Fire Hydrant                  3  EA  $1,000.00  $3,000.00  $1,100.00  $3,300.00",
        "   DS   Water Service                 4  EA    $500.00  $2,000.00    $550.00  $2,200.00",
        "   D6   Sewer Service                 5  EA    $400.00  $2,000.00    $450.00  $2,250.00",
        "   Dt7  Pavement Repair               6  SY    $100.00    $600.00    $110.00    $660.00",
        "   Di8  Sidewalk Repair               7  SY     $90.00    $630.00     $95.00    $665.00",
        "   D49  Curb Repair                   8  LF     $30.00    $240.00     $35.00    $280.00",
        "   D1i0Concrete Repair                9  SY    $200.00  $1,800.00    $210.00  $1,890.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(alpha))
    items = sorted({(r["quantity"], r["item_no"]) for r in recs})
    check("OCR'd letter codes read by sequence",
          [it for _q, it in items] == ["D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"]
          and {r["description"] for r in recs if r["item_no"] == "D10"} == {"Concrete Repair"},
          repr(items))

    #    a size in the description is no quantity; of a number printed twice,
    #    the one in the quantity column is the quantity; 'HEAD' of HEADWALL
    #    is no unit; a line with a quantity but no prices is an item, not text
    sizes = "\n".join([
        "                                                          RELIABLE PAVING INC.",
        "      ITEM   DESCRIPTION                                     QTY      UNIT PRICE      TOTAL",
        "          HEADWALL,    1 BARRELL   15\" TO 30\"                10",
        "       2  HEADWALL,    1 BARRELL   36\" TO 48\"                10    $    7,800.00   $   78,000.00",
        "       3  HEADWALL,    2 BARRELL   36\" TO 48\"                 2    $   12,000.00   $   24,000.00",
        "       4  HEADWALL,    4 BARRELL   15\" TO 30\"                 4    $   16,800.00   $   67,200.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(sizes))
    dd = {r["item_no"]: (r["description"], r["unit"], r["quantity"]) for r in recs}
    check("sizes, the quantity column, glued words",
          dd.get("2") == ('HEADWALL, 1 BARRELL 36" TO 48"', "", 10.0)
          and dd.get("3") == ('HEADWALL, 2 BARRELL 36" TO 48"', "", 2.0)
          and dd.get("4") == ('HEADWALL, 4 BARRELL 15" TO 30"', "", 4.0), repr(dd))

    # -- names: a title with a project number over the price columns, a field
    #    label OCR set a single space after the last name, initials and a
    #    digit in a firm's name (Murphy 2023, Midlothian 2026)
    murphy = "\n".join([
        "                                                 City Project No. 23-P#-20-5710-4399",
        "                                             A & B Construction, LLC  5W Contracting, LLC Project: 2026 Annual Program",
        "   ITEM  DESCRIPTION            UNIT QTY        UNIT PRICE  TOTAL COST  UNIT PRICE  TOTAL COST",
        "    1    Mobilization            LS    1        $50,000.00  $50,000.00  $10,000.00  $10,000.00",
        "    2    Trench Safety           LF  500             $4.00   $2,000.00      $10.00   $5,000.00",
        "    3    Block Sodding           SY  900            $20.00  $18,000.00      $12.20  $10,980.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(murphy))
    check("names under a project title, before a field label",
          info["page_info"][0]["names"] == ["A & B Construction, LLC", "5W Contracting, LLC"],
          repr((info["page_info"][0]["names"], info["page_info"][0]["names_how"])))

    # -- units: OCR misreadings, a rule or period between quantity and unit,
    #    the unit column, another page's copy; none -> empty, with a note
    units = "\n".join([
        "                                            ALPHA LLC                 BETA INC",
        "   ITEM DESCRIPTION              QTY UNIT  UNIT PRICE    TOTAL    UNIT PRICE    TOTAL",
        "   1  Extra Depth Manhole          6 VE     $200.00   $1,200.00    $300.00   $1,800.00",
        "   2  Labor Rate                 120 |Hour$198.00    $23,760.00    $150.00  $18,000.00",
        "   3  Gate Valve                   2. EA$300.00      $600.00      $350.00     $700.00",
        "   4  Pipe                       520 LE      $25.00  $13,000.00     $30.00  $15,600.00",
        "   5  Guard Fence                 81         $100.00   $8,100.00    $120.00   $9,720.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(units))
    uu = {r["item_no"]: (r["unit"], r["unit_note"]) for r in finalize_like_main(recs)}
    check("OCR units: VE=VF, |Hour, '2. EA', LE=LF",
          uu.get("1") == ("VF", "") and uu.get("2") == ("HR", "") and uu.get("3") == ("EA", "")
          and uu.get("4") == ("LF", ""), repr(uu))
    check("no unit: empty unit, note, not unit_suspect",
          uu.get("5") == ("", "ocr_unit_unread")
          and all(r["unit_suspect"] == "" for r in recs if r["item_no"] == "5"), repr(uu.get("5")))
    copy2 = "\n".join([
        "                                            GAMMA LLC                 DELTA INC",
        "   ITEM DESCRIPTION              QTY UNIT  UNIT PRICE    TOTAL    UNIT PRICE    TOTAL",
        "   5  Guard Fence                 81 LF      $110.00   $8,910.00    $105.00   $8,505.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(units, copy2))
    uu = {(r["item_no"], r["bidder_raw"]): r["unit"] for r in recs}
    check("unit from the same pay item on another page",
          uu.get(("5", "ALPHA LLC")) == "LF" and uu.get(("5", "DELTA INC")) == "LF", repr(uu))
    ucol = "\n".join([
        "                                     ALPHA LLC              BETA INC",
        "   ITEM DESCRIPTION        UNIT   QTY   UNIT PRICE  TOTAL   UNIT PRICE  TOTAL",
        "   1  Sod                  SY     100    5.00    500.00    6.00    600.00",
        "   2  Seed                 SY     200    1.00    200.00    2.00    400.00",
        "   3  Mulch                SY     300    3.00    900.00    4.00  1,200.00",
        "   4  Topsoil              Cy      50   20.00  1,000.00   25.00  1,250.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(ucol))
    uu = {r["item_no"]: (r["unit"], r["description"]) for r in recs}
    check("unit read from the page's unit column", uu.get("4") == ("CY", "Topsoil")
          and uu.get("1") == ("SY", "Sod"), repr(uu))
    #    the engine-choice score counts a cell without a unit as half a cell
    no_units = units.replace(" VE ", "    ").replace("|Hour", "     ").replace(" EA", "   ") \
        .replace(" LE ", "    ")
    check("yield score prefers the reading with units",
          yield_score(_sidecar(units))[0] > yield_score(_sidecar(no_units))[0]
          and yield_detail(_sidecar(units))["cells"] == yield_detail(_sidecar(no_units))["cells"],
          repr((yield_detail(_sidecar(units)), yield_detail(_sidecar(no_units)))))

    # -- an invoice in a council packet is no bid tabulation
    invoice = "\n".join([
        "      EDGE CONCRETE SAWING                         INVOICE",
        "      BILL TO: City of Somewhere          Invoice No. 4471",
        "   ITEM  DESCRIPTION                  QTY UNIT  UNIT PRICE  TOTAL",
        "    1   6 Inch Demo Saw               200 LF      1.44     288.00",
        "    2   Curb Cuts                       2 EA      5.00      10.00",
        "    3   Minimum Trip                    1 LS      2.00       2.00",
        "                              AMOUNT DUE                   300.00",
    ])
    recs, audit, strings, info = parse_text(_sidecar(invoice))
    check("invoice page not read as a tabulation", not recs and any("invoice" in n for n in info["notes"])
          and not invoice_page(page) and not invoice_page(centred), repr(info["notes"]))

    # -- a page copied from the PDF's text layer is no OCR'd page
    mixed = _sidecar(page2, page2).replace("### PAGE 1\n", "### PAGE 1 (text layer)\n") \
        .replace("### PAGE 2\n", "### PAGE 2 (OCR)\n")
    recs, audit, strings, info = parse_text(mixed)
    check("text-layer pages give no rows, OCR pages do",
          recs and all(r["src_sheet"] == "ocr_p2" for r in recs) and info["text_layer_pages"] == [1]
          and sum(1 for x in strings if "ALPHA CONSTRUCTION LLC" in x) == 2, repr(len(recs)))

    # -- the record carries every field rows_from_grid emits
    need = {"src_file", "src_sheet", "bid_section", "item_no", "description", "desc_norm",
            "item_family", "size_in", "material", "unit_raw", "unit", "quantity", "bidder_raw",
            "bidder", "is_engineer_estimate", "unit_price", "extended_reported"}
    recs, *_ = parse_text(_sidecar(page))
    check("schema", recs and need <= set(recs[0]), repr(sorted(set(recs[0]) if recs else [])))
    for f in fails:
        print(f)
    print("selftest: %s" % ("OK" if ok else "%d FAILED" % len(fails)))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--show", default=None, help="one sidecar (substring of its path)")
    ap.add_argument("--csv", default=None, help="write the emitted rows here")
    ap.add_argument("--implied-qty", action="store_true",
                    help="opt-in: also accept a quantity implied by 2+ agreeing bidder "
                         "columns where no printed quantity balances (off by default)")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if _selftest() else 1
    if a.report or a.show or a.csv:
        return report(csv_path=a.csv, show=a.show, implied_qty=a.implied_qty)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
