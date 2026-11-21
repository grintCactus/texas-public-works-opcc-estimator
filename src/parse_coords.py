"""
Coordinate-level PDF bid-tab parser.

pdfplumber's table detection fails on the Arlington template: it merges several
data rows into one cell, so quantities come out as digits of four rows glued
together (qty=11181150215004999918187791...) and the arithmetic check rejects
~12% of rows. The page itself is perfectly regular though -- the header line

    ITEM@11  DESCRIPTION@40  QTY@393  UNITS@430  UNIT PRICE@476  TOTAL COST@546
             UNIT PRICE@620  TOTAL COST@689  UNIT PRICE@764 ...

gives exact column anchors, and every value can be assigned to a column by its
x position instead. That also repairs the "$ 6 0,000.00" artifact: those are
three separate words inside one column, and joining a column's words before
parsing restores "$60,000.00".

Returns records with the same schema as the table-based parser in
parse_bidtabs.py, so the two feed one pipeline.
"""
import collections
import os
import re

import pdfplumber

# Extraction-health counters, read by the caller for the run summary.  See
# extract_words_robust for why the distribution and not just a flag.
WORD_STATS = collections.Counter()

LINE_TOL = 1.8          # words within this many points of each other share a line
MARGIN = 12.0           # a value may start slightly left of its header anchor

# Page furniture printed in the left margin at the same height as the contractor
# names. A line carrying any of this is not a name line.
FURNITURE_RE = re.compile(
    r"bid\s*opened|bid\s*tabulation|project\s*no|solicitation|addend|page\s*\d|"
    r"bid\s*date|prepared\s*by|certified|"
    # a vendor quote's or invoice's form furniture (round 3: Grand Prairie's
    # Briar Hill packet, p.110 'Created By: ... Quote Name: ...', p.83
    # 'Issued: ____ Invoice: ____' read as bidder names)
    r"created\s*by|quote\s*(?:name|no\b|number|#)|\bissued\s*:|\binvoice\s*:", re.I)

# "Irving, TX" / "Kennedale, TX 76060" / "Fort Worth, Texas" -- an address, not a name.
STATES = (r"TX|OK|AR|LA|NM|KS|MO|CO|AZ|CA|FL|GA|TN|MS|AL|IL|OH|PA|NY|NC|SC|VA|UT|NV|IA|NE"
          r"|Texas|Oklahoma|Arkansas|Louisiana|New Mexico|Kansas|Missouri|Colorado")
CITY_STATE_RE = re.compile(
    r"^[A-Z][A-Za-z.\-' ]*,\s*(%s)\.?(\s+\d{5}(-\d{4})?)?$" % STATES, re.I)

# A name fragment never contains money or a long run of digits. This is what
# stops a page whose header sits BELOW some data from treating those data lines
# as the contractor-name block.
NOT_A_NAME_RE = re.compile(r"[\$]|\d{3,}")

ITEM_RE = re.compile(r"^[A-Z]?[0-9][0-9.\-]*[A-Za-z]?$")
# "Paving Subtotal" puts the word at the END, so this has to search, not match.
# 'Sub Total' is spelled with a space too (Frisco 2103-057).
AGG_RE = re.compile(r"\bsub[\s.\-]*totals?\b|^(total|grand|sum)\b|\btotal\s+(bid|base|cost|amount)\b",
                    re.I)
# Column captions used instead of names: "BIDDER 1", "CONTRACTOR 2", "BID 3".
BIDDER_LABEL_RE = re.compile(r"^(bidder|contractor|vendor|bid|respondent)\s*#?\s*\d+$", re.I)
MONEYISH = re.compile(r"^[\$\(]?[\d,]*\.?\d*[\)]?$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
CITYLINE_RE = re.compile(r"^[A-Z][A-Za-z.\- ]+,$")

# The words column captions are made of.  A line made of nothing else is a
# caption line: a header printed over two or three lines ('BID / NO.',
# 'UNIT / PRICE', 'Unit / of / Measure') is a band of such lines, and a name
# fragment made of nothing else ('Bid Amount' under each firm) names no firm.
CAPTION_WORDS = frozenset("""
item items no number # bid pay spec code description desc of work qty quantity quantities
est estimated approx approximate required unit units uom measure price prices cost costs
total totals amount amounts extended extension ext with in words figures only and or per
each line
""".split())
# An item number as the first word of a row: '1.01', '100', '#1-1', '199A'.
ITEM_TOKEN_RE = re.compile(r"^#?[A-Z]?\d[\d.\-]*[A-Z]?[.):]?$", re.I)
# A complete dollar amount with cents: '$1,300.00', '27.00'.
AMOUNT_RE = re.compile(r"\$?\d[\d,]*\.\d{2}")
# 'Item 12.' is a council packet's agenda-item stamp, never a firm.
ITEM_LABEL_RE = re.compile(r"^item\s*#?\s*\d+[A-Z]?\.?$", re.I)
# A caption over a group of items ('Bid Schedule 1.00:', 'Section 2 - Paving'):
# a label, never the first line of the description below it.
GROUP_LABEL_RE = re.compile(
    r":\s*$|^(?:bid\s+)?(?:schedule|section|part|phase|division|group|alternates?|alt\.?|"
    r"base\s+bid|additive|deductive|add\s+alt)\b", re.I)

# (round 10, .31) a SECTION banner standing alone on its line, in any case and anywhere on it.  Celina's
# Public Works Renovation Phase 2 tab (13 bidders, a page 2,555 pt wide) centres 'Alternates' over the
# money columns between its base bid's total line and items 2.1-2.4, so the banner sat in no item or
# description cell and the four alternate items were read as base bid; its Ownsby Parkway tab prints
# 'Alternate 1' ... 'Alternate 7' in mixed case at the left margin, which only an all-capitals banner
# used to open.  The whole line must be the banner: the section word, optionally numbered or
# lettered, optionally a title after ':' or '-'.
SECTION_BANNER_RE = re.compile(
    r"^\W*(?:base\s+bid(?:\s+items?)?|(?:bid\s+|add(?:itive)?\s+|deduct(?:ive)?\s+)?alternates?"
    r"(?:\s+bid)?(?:\s*(?:#|no\.?)?\s*(?:\d{1,2}|[a-z]|[ivx]{1,4})\b)?(?:\s*[:\-].{0,60})?)\W*$", re.I)


# (round 11, .32) a NUMBERED group heading that is a section banner.  Frisco's bid-system export numbers
# its groups ('1 MOBILIZATION', '2 DEMO', ... '11 ALTERNATES') and items within them ('#11-1'); the
# alternates' group (#11-1 ... #11-22 of Frisco on the Green, $2.8M of A & C's $5.5M) was read as base
# bid, so every bidder's base was its total with alternates.  A bare group number (one or two digits) in
# the item column beside a description that is a section banner (SECTION_BANNER_RE), with no quantity,
# unit or price on the line, opens that section; '1 MOBILIZATION' and an item without its prices stay.
def numbered_banner(item_no, desc, qty_s, unit_s):
    """The section label of a numbered group heading ('11', 'ALTERNATES'), or None."""
    if (re.fullmatch(r"\d{1,2}", (item_no or "").strip()) and desc and not qty_s and not unit_s
            and SECTION_BANNER_RE.match(desc)):
        return desc.strip()[:60].upper()
    return None


DASHES = {"‐": "-", "‑": "-", "–": "-", "—": "-",
          "−": "-", " ": " "}


def group_lines(words, tol=LINE_TOL):
    """Cluster words into visual lines by their vertical position."""
    for w in words:
        t = w.get("text") or ""
        if any(ch in t for ch in DASHES):
            # Some tabs print item numbers with a Unicode hyphen (2019‐010‐01),
            # which defeats every ASCII pattern downstream.
            for bad, good in DASHES.items():
                t = t.replace(bad, good)
            w["text"] = t
    out = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for ln in out:
            if abs(ln["top"] - w["top"]) <= tol:
                ln["words"].append(w)
                ln["top"] = (ln["top"] * (len(ln["words"]) - 1) + w["top"]) / len(ln["words"])
                break
        else:
            out.append({"top": w["top"], "words": [w]})
    for ln in out:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["x0"] = ln["words"][0]["x0"]
        ln["text"] = " ".join(w["text"] for w in ln["words"])
    return sorted(out, key=lambda l: l["top"])


def extract_words_robust(page):
    """Words from a page, repairing text layers that come out one letter per word.

    Some bid tabs (Grand Prairie's council packets among them) carry a text
    layer whose glyphs are placed individually.  pdfplumber's default
    `use_text_flow=False` re-sorts the characters and then fails to regroup
    them, returning one word per letter even though the characters are adjacent
    (measured gaps of 0.00): "Turf Reinforcement Mat" arrives as
    'T','u','r','f',...  Nothing downstream can match a description like that,
    so those rows score item_family='other' and drop out of every basket --
    which is how the largest gabion item in the whole corpus (2,900 CY on Briar
    Hill, 8 bidders) came to be absent from the modelling data.

    Raising x_tolerance does NOT help, because the gaps are already zero.
    `use_text_flow=True` does: it groups by the order the characters were drawn
    rather than by re-sorted position, and the same line comes back as 7 proper
    words.

    Text flow is not used unconditionally -- for normal tabs the positional sort
    is the more reliable basis for column slicing -- so it is applied only when
    the default output is visibly shattered: a page where most words are a
    single character.  (Repairing this at the matching stage instead, by
    stripping whitespace before comparing, would be worse than useless: these
    same pages merge text ACROSS pay items, so a de-spaced description can
    contain two items' words and would assign one item's price to the other's
    family.)
    """
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return words

    # ---- rotated text layers --------------------------------------------
    # A wide bid tabulation is often drawn on a portrait page with the whole
    # table turned 90 degrees.  `page.rotation` is 0 -- the PAGE is not rotated,
    # the TEXT is -- and every word comes back with upright=False.  pdfplumber
    # then groups characters by x while the characters actually run along y, so
    # each word arrives reversed and the rows are scattered across what looks
    # like hundreds of lines:
    #
    #     'RUOCS'  'EGDIRB'  'NOITAZILIBATS'   = SCOUR, BRIDGE, STABILIZATION
    #     '59.303,548'                         = 845,303.95
    #
    # Both strategies returned ZERO rows on such files, which is why Denton
    # County's gabion/mattress/rock-anchor tabulation -- the one letting in the
    # corpus that prices all three with four bidders -- was absent.  Measured:
    # 12 of 70 files that the index calls a bid schedule and that I get nothing
    # from have pages like this.
    #
    # The repair is to put the words back on the axes they were drawn on: group
    # into rows by x0, order each row by descending top, and reverse each word's
    # characters.  Verified against two externally published figures from the
    # Denton tab -- 845,303.95 (the award) and 413.20 (a bidder's alternative
    # riprap price) -- both of which come back exactly.
    up = sum(1 for w in words if w.get("upright", True))
    if up / len(words) < 0.5:
        WORD_STATS["pages_rotated_text"] += 1
        rows = {}
        for w in words:
            rows.setdefault(round(w["x0"] / 4.0), []).append(w)
        # The two axes SWAP; they are not re-laid-out.  Writing the words back
        # at running offsets (x += width) was the first attempt and it read
        # correctly while destroying the column geometry -- the descriptions
        # came back as words but the column detector then sliced unit prices
        # into the unit column, because nothing lined up across rows any more.
        # Column detection needs the true position on the rotated axis, so the
        # old top/bottom becomes the new x-extent and the old x-extent becomes
        # the new top/bottom, flipped so reading order is preserved and shifted
        # to stay positive.
        H = float(getattr(page, "height", 0) or 0) or max(
            w["bottom"] for w in words)
        W = float(getattr(page, "width", 0) or 0) or max(
            w["x1"] for w in words)
        fixed = []
        # (round 11, .32) the few words a turned page prints UPRIGHT are no part of its table: an
        # agenda packet's page stamp ('Packet Pg. 415', '7.C.a') put on after the table was turned.
        # Turned with the table they landed in its item column, reversed ('.gP 59'), and Celina's
        # Ownsby Parkway item 59 (2 EA, $6,375 / $5,000 / $3,915 ...) was no item.  Left out only
        # where they are a sliver of the page's words (under 5%): a page of mixed orientation keeps all.
        stray = up and up < 0.05 * len(words)
        if stray:
            WORD_STATS["upright_stamp_words_dropped"] += up
        for k in sorted(rows, reverse=True):
            for w in sorted(rows[k], key=lambda w: -w["top"]):
                if stray and w.get("upright", True):
                    continue
                # Horizontal axis IS flipped (reading order within a row runs
                # against the original top), the vertical axis is NOT.  Both
                # flipped put the page upside down: the column header landed at
                # top~495 and the `Vendor ---->` row at top~515, i.e. BELOW the
                # item rows, so bidder_names_for -- which walks UPWARD from the
                # header -- searched the data and found no names, and all 119
                # recovered rows would have dropped as `no_bidder_name`.
                fixed.append({**w, "text": w["text"][::-1], "upright": True,
                              "x0": H - w["bottom"], "x1": H - w["top"],
                              "top": w["x0"], "bottom": w["x1"],
                              "doctop": w["x0"]})
        if fixed:
            return join_touching_words(fixed)
        WORD_STATS["rotated_repair_failed"] += 1

    frac = sum(1 for w in words if len(w["text"]) == 1) / len(words)
    # Observability for the KNOWN BLIND SPOT of this threshold: a page that is
    # only partly shattered (say 20-25% single characters) stays below it, stays
    # damaged, and raises no signal.  The threshold is deliberately left alone
    # rather than lowered -- it is a performance guard, not the correctness one.
    # Correctness is enforced downstream by the arithmetic check (quantity x
    # unit_price == printed extended), which a badly extracted page fails
    # whether or not this repair fired.  Recording the distribution costs
    # nothing and makes the 25-35% band visible instead of invisible.
    WORD_STATS["pages"] += 1
    WORD_STATS["singles_bucket_%d0pct" % int(min(frac, 0.99) * 10)] += 1
    # The single-character share is only a cheap PRE-FILTER, to avoid a second
    # extraction on obviously clean pages.  The decision is the improvement
    # ratio below, because the share turned out to be a poor discriminator:
    # damage is not uniform within a document.  Briar Hill's bid tab alternates
    # across consecutive pages -- p126/128/130 sit above 35% while the pages
    # interleaved with them, p127/129/131, sit at 31-33% -- so any share
    # threshold cuts through the middle of one continuous table.
    if frac < 0.15:
        return words
    flowed = page.extract_words(use_text_flow=True, keep_blank_chars=False)
    if not flowed:
        return words
    before = sum(len(w["text"]) for w in words) / len(words)
    after = sum(len(w["text"]) for w in flowed) / len(flowed)
    # 1.08, not 1.3.  Measured: a healthy page returns EXACTLY 1.00x (text flow
    # and positional sort agree completely), so anything above ~1.05 is real
    # improvement; while the partly-damaged pages only reach 1.15-1.17x and a
    # 1.3 cut-off rejected precisely the rows worth recovering.  The guard still
    # refuses pages text flow makes worse -- Briar Hill p60 scores 0.97x and
    # keeps the positional grouping.
    if after > before * 1.08:
        WORD_STATS["repaired_with_text_flow"] += 1
        return flowed
    WORD_STATS["screened_but_repair_rejected"] += 1
    return words


def join_touching_words(words):
    """Rejoin the pieces of words a rotated text layer shatters.

    On a table drawn turned 90 degrees pdfplumber can return one or two letters
    per word -- Grand Prairie's Kirby Creek tab (2025) reads 'C','o','n','d',
    'it','i','o','n' for 'Condition': the characters sit edge to edge, a real
    word break being an explicit space glyph about a point wide.  After the
    axes are swapped, pieces on one line whose gap is under 0.3 pt are one word.
    No column is ever that close to the next, and the upright path already
    joins characters 3 pt apart, so this is the more conservative rule."""
    out = []
    for w in words:
        p = out[-1] if out else None
        if (p is not None and abs(p["top"] - w["top"]) < 0.5
                and -0.5 < w["x0"] - p["x1"] < 0.3):
            out[-1] = {**p, "text": p["text"] + w["text"], "x1": max(p["x1"], w["x1"]),
                       "bottom": max(p["bottom"], w["bottom"])}
            WORD_STATS["rotated_pieces_joined"] += 1
            continue
        out.append(w)
    return out


def find_header(line):
    """If this line is a column header, return its anchor x positions."""
    ws = line["words"]
    txts = [w["text"].upper().strip(":") for w in ws]
    # The description column is not always called "Description" -- Mesquite
    # heads it "Bid Items". Requiring the literal word cost us whole files.
    if not ({"DESCRIPTION", "ITEMS", "DESC"} & set(txts)):
        return None
    # Owners name these columns differently: "UNIT PRICE / TOTAL COST",
    # "UNIT COST / TOTAL COST", "UNIT PRICE / ITEM COST", "UNIT PRICE / EXTENDED".
    unit_price_x, total_x = [], []
    # A word taken as the SECOND half of a two-word caption must not be examined
    # again as the first half of the next pair.  On the repeating
    # "Unit | Extended" template (one such pair per bidder), the EXTENDED that
    # was already consumed by "UNIT EXTENDED" also matched the bare-EXTENDED
    # branch on the following step, so every total x landed in the list twice:
    #   total_x = [t0, t0, t1, t1, t2, t2]   vs  unit_price_x = [u0, u1, u2]
    # The length check below passes (6 >= 3), then build_ranges pairs u1 with t0
    # and produces lo > hi -- an inverted range that yields no cells, so bidders
    # 2..N vanish with no error.  Measured on mesquite: 534 of 665 recoverable
    # bidder rows lost (~80%), 31 of 104 PDFs affected, and the survivor column
    # is internally consistent, so the arithmetic gate scored the broken parse
    # 1.00 and preferred it over the correct one.
    consumed = set()
    for i in range(len(ws) - 1):
        if i in consumed:
            continue
        a, b = txts[i], txts[i + 1]
        # Some owners label the columns just "Unit | Extended" -- the price
        # column header is the bare word "Unit" and the total is "Extended".
        # Requiring the two-word "UNIT PRICE" missed that template entirely.
        if a == "UNIT" and b in ("PRICE", "COST", "BID"):
            unit_price_x.append(ws[i]["x0"])
            consumed.add(i + 1)
        elif a == "UNIT" and b in ("EXTENDED", "EXT", "TOTAL", "AMOUNT"):
            unit_price_x.append(ws[i]["x0"])
            total_x.append(ws[i + 1]["x0"])
            consumed.add(i + 1)
        elif a in ("TOTAL", "EXTENDED", "EXTENSION", "ITEM", "LINE") \
                and b in ("COST", "PRICE", "AMOUNT", "TOTAL"):
            total_x.append(ws[i]["x0"])
            consumed.add(i + 1)
        elif a == "EXTENSION":
            # Dallas County heads the total column "EXTENSION" on its own.
            total_x.append(ws[i]["x0"])
        elif a == "TOTAL" and b not in ("COST", "PRICE", "AMOUNT"):
            total_x.append(ws[i]["x0"])
        elif a == "EXTENDED" and b not in ("COST", "PRICE", "AMOUNT"):
            total_x.append(ws[i]["x0"])
        elif a == "AMOUNT" and i > 0 and (i - 1) in consumed \
                and txts[i - 1] in ("PRICE", "COST", "BID"):
            # 'UNIT PRICE | AMOUNT' repeated per bidder (Southlake's North
            # Park tab, 2026): the bare AMOUNT right after a unit-price caption
            # is that bidder's total.  Only the LAST one was taken (the tail
            # rule below), 1 total for 3 prices, and the page read nothing.
            total_x.append(ws[i]["x0"])
    # Defence in depth: the same anchor must never appear twice in either list,
    # whatever the caption vocabulary does next.
    unit_price_x = list(dict.fromkeys(unit_price_x))
    total_x = list(dict.fromkeys(total_x))
    # The pair loop stops one short, so a header ending in a bare "Extended" or
    # "Total" never gets examined. That left headers like
    #   Item Number | Item Description | Qty | Unit | Unit Price Extended x3
    # with 3 price columns but only 2 total columns, failing the check below and
    # silently costing every file on that template.
    if (txts and txts[-1] in ("EXTENDED", "EXTENSION", "TOTAL", "AMOUNT", "COST", "PRICE")
            and (len(ws) - 1) not in consumed
            and ws[-1]["x0"] not in total_x
            and len(total_x) < len(unit_price_x)):
        total_x.append(ws[-1]["x0"])
    if len(unit_price_x) < 1 or len(total_x) < len(unit_price_x):
        return None

    def first_x(*names):
        for i, t in enumerate(txts):
            if t in names:
                return ws[i]["x0"]
        return None

    def desc_x():
        """Where the description COLUMN starts, not where its caption word
        starts. "Item Description" and "Bid Items" are two-word captions, so the
        column begins at the first word; anchoring on the second leaves the item
        column swallowing the first half of every description."""
        for i, t in enumerate(txts):
            if t not in ("DESCRIPTION", "ITEMS", "DESC"):
                continue
            x = ws[i]["x0"]
            if i > 0 and txts[i - 1] in ("ITEM", "BID", "PAY", "WORK", "LINE", "NO."):
                if abs(ws[i]["x0"] - ws[i - 1]["x1"]) < 8:
                    x = ws[i - 1]["x0"]
            return x
        return None

    def uom_x():
        """The unit-of-measure column. "UNIT" is ambiguous -- it also starts
        "UNIT PRICE" -- so take a UNIT/UNITS/UOM token that is NOT followed by
        PRICE / COST / BID, and only one left of the money block."""
        for i, t in enumerate(txts):
            if t not in ("UNIT", "UNITS", "UOM", "UNITS."):
                continue
            nxt = txts[i + 1] if i + 1 < len(txts) else ""
            if nxt in ("PRICE", "COST", "BID"):
                continue
            if ws[i]["x0"] < unit_price_x[0]:
                return ws[i]["x0"]
        # (round 11, .32) 'U/M' (the coordinate reading of Hickory Creek's and Coppell's 'ITEM # |
        # DESCRIPTION | U/M | QTY' tabs) and a bare 'MEASURE' caption (Collin County's 'Quantity | Measure
        # | Unit Cost') name the unit column too, where no UNIT caption does
        for i, t in enumerate(txts):
            if t in ("U/M", "U.M.", "UOM.", "U/O/M", "MEASURE") and ws[i]["x0"] < unit_price_x[0]:
                if t == "MEASURE" and i > 0 and txts[i - 1] in ("OF", "UNIT", "UNITS"):
                    continue          # 'UNIT OF MEASURE' is read at its UNIT above
                return ws[i]["x0"]
        return None

    return {
        "top": line["top"],
        "item_x": first_x("ITEM", "ITEM NO.", "NO.", "NO"),
        "desc_x": desc_x(),
        "qty_x": first_x("QTY", "QTY.", "QUANTITY"),
        "unit_x": uom_x(),
        "price_x": unit_price_x,
        "total_x": total_x[:len(unit_price_x)],
        # (round 10) the caption words left of the money block, for a layout whose description
        # column stands right of the quantity / unit columns (build_ranges)
        "caption_words": [(w["x0"], w["x1"], txts[k]) for k, w in enumerate(ws)
                          if w["x0"] < unit_price_x[0] - MARGIN],
    }


# ---- a header printed over several lines ------------------------------------
# Spreadsheet tabs wrap their captions inside tall header cells, so one column's
# caption is spread over two or three lines:
#
#     BID    DESCRIPTION                 UNIT         UNIT    TOTAL       (Southlake,
#                                   UNIT  QTY                              Timarron
#     NO.    (with unit price in words)               PRICE   PRICE        Creek 2022)
#
# find_header reads ONE line, so it either finds nothing (Frisco 2103-057:
# 'Unit / Price', 'Total / Cost', 'Quantity / Required', 'Unit / of / Measure'
# -- both strategies read zero rows) or a partial header with no quantity or
# unit column, so the description column swallowed 'LS 1' and the middle line
# 'UNIT QTY' became a section banner.  Here the caption lines next to each
# other are read as one band: words stacked over each other form one column's
# caption, and each column is classified by its whole caption.

def _height(words):
    hs = sorted(w["bottom"] - w["top"] for w in words)
    return hs[len(hs) // 2] if hs else 0.0


def caption_line(l):
    """A line made only of column-caption words (no digits)."""
    if any(re.search(r"\d", w["text"]) for w in l["words"]):
        return False
    toks = [t for t in (re.sub(r"[^a-z#]", "", w["text"].lower()) for w in l["words"]) if t]
    return bool(toks) and all(t in CAPTION_WORDS for t in toks)


def phrases(l, gap):
    """Split a line into runs of words no more than `gap` apart."""
    out = []
    for w in l["words"]:
        if out and w["x0"] - out[-1]["x1"] <= gap:
            out[-1]["words"].append(w)
            out[-1]["x1"] = max(out[-1]["x1"], w["x1"])
        else:
            out.append({"x0": w["x0"], "x1": w["x1"], "top": l["top"], "words": [w]})
    return out


def _caption_kind(text, has_description):
    s = re.sub(r"\([^)]*\)", " ", text.upper())
    s = " ".join(re.sub(r"[^A-Z#/ ]", " ", s).split())
    ws = s.split()
    if not ws:
        return "other"
    if re.match(r"^(?:BID )?UNIT (?:PRICE|COST|BID)\b", s):
        return "price"
    if re.match(r"^(?:TOTAL|EXTENDED|EXTENSION|EXT|AMOUNT|ITEM TOTAL|LINE TOTAL|BID AMOUNT)"
                r"(?: (?:PRICE|COST|AMOUNT|TOTAL|BID))?$", s):
        return "total"
    if "DESCRIPTION" in ws or "DESC" in ws:
        return "description"
    if {"QTY", "QUANTITY", "QUANTITIES"} & set(ws):
        return "qty"
    if s in ("UNIT", "UNITS", "UOM", "UNIT OF MEASURE", "UNIT MEASURE", "U/M"):
        return "unit"
    if s in ("#", "NO", "ITEM", "ITEM NO", "ITEM #", "ITEM NUMBER", "BID NO", "BID ITEM",
             "PAY ITEM", "NUMBER", "LINE", "LINE NO"):
        return "item"
    if s in ("ITEMS", "BID ITEMS") and not has_description:
        return "description"           # Mesquite heads the description 'Bid Items'
    return "other"


def stacked_header(lines, i):
    """Header anchors from the band of caption lines around lines[i], or None."""
    if not caption_line(lines[i]):
        return None
    h = _height(lines[i]["words"]) or 6.0
    band = [i]
    j = i - 1
    while j >= 0 and caption_line(lines[j]) and \
            lines[band[0]]["top"] - lines[j]["top"] <= 1.3 * h:
        band.insert(0, j)
        j -= 1
    j = i + 1
    while j < len(lines) and caption_line(lines[j]) and \
            lines[j]["top"] - lines[band[-1]]["top"] <= 1.3 * h:
        band.append(j)
        j += 1
    if len(band) < 2:
        return None
    rows = [phrases(lines[k], 0.6 * h) for k in band]
    cols = []                          # each: list of phrases, stacked top to bottom
    for row in rows:
        for ph in row:
            hit = [c for c in cols if any(min(ph["x1"], q["x1"]) - max(ph["x0"], q["x0"]) > 0
                                          for q in c)]
            if len(hit) > 1:
                return None            # a caption over two columns: not a plain band
            if hit:
                if any(q["top"] == ph["top"] for q in hit[0]):
                    return None        # two runs of one line under one caption
                hit[0].append(ph)
            else:
                cols.append([ph])
    out = []
    for c in cols:
        text = " ".join(w["text"] for ph in sorted(c, key=lambda q: q["top"]) for w in ph["words"])
        out.append({"x0": min(q["x0"] for q in c), "x1": max(q["x1"] for q in c), "text": text})
    out.sort(key=lambda c: c["x0"])
    has_desc = any("DESCRIPTION" in c["text"].upper() or "DESC" in c["text"].upper().split()
                   for c in out)
    for c in out:
        c["kind"] = _caption_kind(c["text"], has_desc)
    prices = [c for c in out if c["kind"] == "price"]
    if not prices:
        return None
    total_x = []
    for k, p in enumerate(prices):
        nxt = prices[k + 1]["x0"] if k + 1 < len(prices) else float("inf")
        t = [c for c in out if c["kind"] == "total" and p["x0"] < c["x0"] < nxt]
        if not t:
            return None
        total_x.append(t[0]["x0"])
    left = [c for c in out if c["x1"] <= prices[0]["x0"] + 1.0]

    def first(kind):
        return next((c for c in left if c["kind"] == kind), None)
    desc, item = first("description"), first("item")
    if desc is None:
        return None
    # A caption column between the item number and the description (Frisco's
    # spec-section code 'Items' -- '017133', 'PK313519') is neither: the item
    # range stops where it starts.
    other = next((c for c in left if c["kind"] == "other" and item is not None
                  and item["x0"] < c["x0"] < desc["x0"]), None)
    qty, unit = first("qty"), first("unit")
    return {
        "top": lines[band[0]]["top"],
        "band": [lines[k]["top"] for k in band],
        "item_x": item["x0"] if item else None,
        "item_hi_x": other["x0"] if other else None,
        "desc_x": desc["x0"],
        "qty_x": qty["x0"] if qty else None,
        "unit_x": unit["x0"] if unit else None,
        "price_x": [p["x0"] for p in prices],
        "total_x": total_x,
    }


def page_headers(lines):
    """[(line index, header)] for a page: one-line headers, completed from the
    caption band around them when the line alone lacks the quantity or unit
    column, and band headers where no single line is a header."""
    out = []
    for i, l in enumerate(lines):
        hd = find_header(l)
        if hd is None:
            continue
        if hd["qty_x"] is None or hd["unit_x"] is None:
            st = stacked_header(lines, i)
            if st is not None and len(st["price_x"]) == len(hd["price_x"]):
                WORD_STATS["header_bands_completed"] += 1
                hd = st
        out.append((i, hd))
    if out:
        return out
    seen = set()
    for i, l in enumerate(lines):
        if not any(w["text"].upper().strip(":") in ("DESCRIPTION", "DESC", "ITEMS")
                   for w in l["words"]):
            continue
        st = stacked_header(lines, i)
        if st is not None and st["top"] not in seen:
            seen.add(st["top"])
            WORD_STATS["header_bands_read"] += 1
            out.append((i, st))
    return out


def build_ranges(hdr, page_width):
    """Turn header anchors into [lo, hi) x ranges for every field."""
    px, tx = hdr["price_x"], hdr["total_x"]
    n = len(px)
    blocks = []
    for k in range(n):
        lo = px[k] - MARGIN
        hi = (px[k + 1] - MARGIN) if k + 1 < n else page_width
        mid = tx[k] - MARGIN
        blocks.append({"lo": lo, "hi": hi, "price": (lo, mid), "total": (mid, hi),
                       "tx": tx[k]})
    left = px[0] - MARGIN
    unit_lo = (hdr["unit_x"] - MARGIN) if hdr["unit_x"] else left
    qty_lo = (hdr["qty_x"] - MARGIN) if hdr["qty_x"] else unit_lo
    desc_lo = (hdr["desc_x"] - MARGIN) if hdr["desc_x"] else 0.0
    # The item range stops at a caption column printed between the item number
    # and the description (see stacked_header); that column is read as neither.
    item_hi, glued = desc_lo, None
    if hdr.get("item_hi_x") is not None:
        item_hi = min(desc_lo, hdr["item_hi_x"] - MARGIN)
        glued = (item_hi, hdr["desc_x"] - 1.0)
    if (hdr.get("caption_words") and hdr["desc_x"] is not None and hdr["item_x"] is None
            and any(hdr[k] is not None and hdr[k] < hdr["desc_x"] for k in ("qty_x", "unit_x"))):
        return reordered_ranges(hdr, blocks, left)
    if hdr["unit_x"] and hdr["qty_x"] and hdr["unit_x"] < hdr["qty_x"]:
        # 'UNIT | QTY' -- the unit printed LEFT of the quantity (Southlake's
        # tabs).  The ranges below assume QTY first, which made the quantity
        # range end before it began: no quantity, and 'LS 1' read as the unit.
        return {
            "item": (0.0, item_hi),
            "desc": (desc_lo, unit_lo),
            "unit": (unit_lo, qty_lo),
            "qty": (qty_lo, left),
            "blocks": blocks,
            "left_edge": left,
            "glued": glued,
        }
    return {
        "item": (0.0, item_hi),
        "desc": (desc_lo, qty_lo),
        "qty": (qty_lo, unit_lo),
        "unit": (unit_lo, left),
        "blocks": blocks,
        "left_edge": left,
        "glued": glued,
    }


# ---- (round 10, .31) the description column RIGHT of the quantity and unit columns ----------------
# Celina's Ownsby Parkway tab (Bonfire export, 9 pages, 5 bidders, $11.5M) heads its columns 'Pkg Line # |
# Pkg Line Type | Qty | UoM | Description | Note to Bidder | Unit Price | Extended Price ...'.  The
# ranges above assume the description comes first, so the description range came out inverted
# (empty) and the unit range ran from the UoM caption to the money -- 'LS MOBILIZATION (5% MAX)
# REFER TO CITY OF CELINA STANDARD SPECIFICATIONS' -- 79% of units failed read_pdf_best's unit gate
# and all 1,230 priced cells (arithmetic 1,225 / 1,225) were thrown away.  With no item caption and
# the description right of the quantity / unit captions: the item column ends before the first
# caption word of the header line (its own caption sits on the band line above: 'Pkg Line / #'),
# the quantity and unit columns each run to the next caption, and the description region runs from
# the unit column to the money.  A caption that is no field ('Pkg Line Type', 'Note to Bidder')
# names a column read as nothing: words of the description region are taken in runs (split at a
# column gap), and a run goes to the description only when its centre is nearer the description
# caption's centre than any other caption's (the centred notes 'REFER TO ... SPECIFICATIONS').
_FIELD_WORDS = frozenset("""item items no no. # description desc qty qty. quantity quantities unit units uom
est est. estimated approx approximate of measure bid""".split())


def reordered_ranges(hdr, blocks, left):
    anchors = sorted((x, k) for k, x in (("qty", hdr["qty_x"]), ("unit", hdr["unit_x"]),
                                         ("desc", hdr["desc_x"])) if x is not None)
    words = hdr["caption_words"]
    foreign = [(x0, x1) for x0, x1, t in words if t.lower() not in _FIELD_WORDS]
    first = min([anchors[0][0]] + [x0 for x0, _ in foreign if x0 < anchors[0][0]])
    out = {"item": (0.0, first - MARGIN), "blocks": blocks, "left_edge": left, "glued": None,
           "reordered": True}
    for j, (x, k) in enumerate(anchors):
        nxt = anchors[j + 1][0] if j + 1 < len(anchors) else left + MARGIN
        if k != "desc":
            stop = min([nxt] + [x0 for x0, _ in foreign if x < x0 < nxt])
            out[k] = (x - MARGIN, stop - MARGIN)
    for k in ("qty", "unit"):
        out.setdefault(k, (left, left))
    lo = max(out["qty"][1], out["unit"][1])
    out["desc"] = (lo, left)
    dw = [(x0, x1) for x0, x1, t in words if t.lower() in ("description", "desc", "items")]
    out["desc_center"] = (dw[0][0] + dw[0][1]) / 2.0
    # captions right of the description's: the centre of each run of caption words
    others, run = [], []
    for x0, x1 in sorted(f for f in foreign if f[0] > dw[0][1]):
        if run and x0 - run[-1][1] > 2 * MARGIN:
            others.append((run[0][0] + run[-1][1]) / 2.0)
            run = []
        run.append((x0, x1))
    if run:
        others.append((run[0][0] + run[-1][1]) / 2.0)
    out["other_centers"] = others
    WORD_STATS["description_right_of_quantity"] += 1
    return out


def _gutter(lines, top, a, b):
    """The middle of the widest x-interval inside [a, b] that no word of a line below `top` covers
    (3 pt or wider), or None."""
    spans = sorted((w["x0"], w["x1"]) for l in lines if l["top"] > top + LINE_TOL
                   for w in l["words"] if w["x1"] > a and w["x0"] < b)
    best, x = None, a
    for x0, x1 in spans + [(b, b)]:
        if x0 > x and (best is None or x0 - x > best[1] - best[0]):
            best = (x, min(x0, b))
        x = max(x, x1)
    if best is None or best[1] - best[0] < 3.0:
        return None
    return (best[0] + best[1]) / 2.0


def refine_reordered(lines, top, ranges):
    """Move the quantity / unit / description boundaries of a reordered layout (reordered_ranges)
    to the gutters the rows leave between the captions' columns."""
    order = sorted((ranges[k][0], k) for k in ("qty", "unit") if ranges[k][1] > ranges[k][0])
    cols = [k for _, k in order] + ["desc"]
    for k1, k2 in zip(cols, cols[1:]):
        a = ranges[k1][0] + MARGIN
        b = (ranges["desc_center"] if k2 == "desc" else ranges[k2][0] + MARGIN)
        g = _gutter(lines, top, a, b)
        if g is not None:
            ranges[k1] = (ranges[k1][0], g)
            ranges[k2] = (g, ranges[k2][1])
            WORD_STATS["reordered_column_gutters"] += 1


def desc_words(line, ranges):
    """The description words of a line: words_in the description range, less the runs nearer
    another caption (reordered_ranges)."""
    d = words_in(line, ranges["desc"])
    if not d or not ranges.get("other_centers"):
        return d
    gap = 1.5 * (_height(d) or 6.0)
    runs, cur = [], [d[0]]
    for w in d[1:]:
        if w["x0"] - cur[-1]["x1"] > gap:
            runs.append(cur)
            cur = []
        cur.append(w)
    runs.append(cur)
    keep = []
    for r in runs:
        c = (r[0]["x0"] + r[-1]["x1"]) / 2.0
        if abs(c - ranges["desc_center"]) <= min(abs(c - o) for o in ranges["other_centers"]):
            keep.extend(r)
    return keep


def refine_description_start(lines, top, ranges):
    """Move the item/description boundary to where the descriptions start when
    the DESCRIPTION caption is centred over a wide column.

    build_ranges puts the boundary a margin left of the caption.  A caption
    centred over the column (Southlake, Arlington's PWDR19010C, Grand Prairie's
    Kirby Creek) sits 60-170 pt right of where the text starts, so the item
    range swallowed most of every description: '301 Site Preparation' was read
    as the item number with no description, failed the item pattern and the
    row was dropped -- Arlington's Kee Branch Trib 1 kept 5 of its 23 items.
    The rows themselves say where the column starts: on the lines below the
    header that open with an item number, the next word starts the
    description.  Moved only when most such lines (3 or more) show it, the
    item numbers are distinct (an item number is unique: four description
    lines opening '6 sack mix' are no item rows), the starts line up on
    their median (a left-aligned column, not codes of varying width; a
    section line like '100 Miscellaneous Items' may start elsewhere), the
    text starts well left of the caption (2 margins), and never past an item
    number."""
    lo, hi = ranges["item"]
    if hi <= 0 or hi != ranges["desc"][0]:
        return False
    item_x, starts, toks, rests = [], [], [], []
    for l in lines:
        if l["top"] <= top + LINE_TOL:
            continue
        ws = [w for w in l["words"] if w["x0"] < hi]
        if not ws or not ITEM_TOKEN_RE.match(ws[0]["text"]):
            continue
        item_x.append(ws[0]["x0"])
        toks.append(ws[0]["text"])
        if len(ws) > 1 and re.search(r"[A-Za-z]", ws[1]["text"]):
            starts.append(ws[1]["x0"])
            rests.append([w["x0"] for w in ws[2:]])
    if len(item_x) < 3 or len(starts) < 0.5 * len(item_x) or len(set(toks)) < 0.8 * len(toks):
        return False
    s = sorted(starts)[len(starts) // 2]
    if (max(item_x) >= s - 1.0 or hi - s < 2 * MARGIN
            or sum(1 for x in starts if abs(x - s) <= 3.0) < 0.6 * len(starts)):
        return False
    # A row whose second word starts between the item numbers and that column
    # while a later word starts AT the column has a code printed beside its
    # number ('103 SP Paving'): the column there is no plain description
    # column, so it is left as it was.  (A section line such as '100
    # Miscellaneous Items' starts left of the column too, but nothing of it
    # lines up with the column.)
    if any(x < s - MARGIN / 2.0 and any(abs(y - s) <= 3.0 for y in rest)
           for x, rest in zip(starts, rests)):
        return False
    cut = max(max(item_x) + 1.0, s - MARGIN / 2.0)
    ranges["item"] = (lo, cut)
    ranges["desc"] = (cut, ranges["desc"][1])
    WORD_STATS["description_start_from_rows"] += 1
    return True


# ---- the description column starts where the WORDS start ------------------
# A description caption centred over a left-aligned column puts the column's
# start too far right: Bedford's Parkwood Drive tab captions it 'DESCRIPTION OF
# ITEMS' at x=137 while every description starts at x=77, right after the item
# number, so the item cell read '100 CONSTRUCTION', '109 CEMENT SLURRY' --
# items whose description fitted there had none left and were dropped (11 of
# 66), the rest lost their first words.  Where three or more priced lines, and
# at least half of those with anything in the item column, print an item
# number followed by words there, the description column starts at the
# leftmost of those words.  (Parser round 3: the fallback when
# refine_description_start declines -- Parkwood's words start 23.5 pt left of the
# boundary, under its 2-margin floor -- with the same evidence asked of the rows.)
def refine_item_column(lines, ranges, top_limit=None):
    """ranges with the item/description boundary moved left to where the
    descriptions start, or ranges unchanged."""
    item_lo, item_hi = ranges["item"]
    starts, ends, n = [], [], 0
    for l in lines:
        if top_limit is not None and l["top"] <= top_limit + LINE_TOL:
            continue
        if not any(joined(l, b["price"]) for b in ranges["blocks"]):
            continue
        ws = words_in(l, ranges["item"])
        if not ws:
            continue
        n += 1
        if (len(ws) >= 2 and ITEM_RE.match(ws[0]["text"])
                and re.search(r"[A-Za-z]{2}", " ".join(w["text"] for w in ws[1:]))):
            starts.append(ws[1]["x0"])
            ends.append(ws[0]["x1"])
    if len(starts) < 3 or len(starts) < 0.5 * n:
        return ranges
    x = min(starts)
    if x <= max(ends) or x >= item_hi:
        return ranges
    out = dict(ranges)
    out["item"] = (item_lo, x - 0.5)
    out["desc"] = (x - 0.5, ranges["desc"][1])
    return out


def words_in(line, rng):
    lo, hi = rng
    return [w for w in line["words"] if lo <= w["x0"] < hi]


def joined(line, rng):
    """Join a column's words with NO separator: a single value is often split
    into several words ("$", "6", "0,000.00"), and only concatenation restores it."""
    return "".join(w["text"] for w in words_in(line, rng)).strip()


def spaced(line, rng):
    return " ".join(w["text"] for w in words_in(line, rng)).strip()


def desc_and_qty(line, ranges):
    """(description, quantity) text of a line.  A description running into the
    quantity column's margin keeps its last words: a word in the quantity
    range that is no number and follows the description at word spacing is
    the description's ('... complete in' + '1' read as quantity 'in1' on Kirby
    Creek item 117, and the 4' of '... Riprap with 4'' lost from 115).  A
    word of letters run into digits ('in1', 'wit747': the description cut
    by its cell and printed against the quantity, Little Elm 2023-05) is
    left where it was."""
    d = desc_words(line, ranges)
    q = words_in(line, ranges["qty"])
    if d and q and ranges["desc"][1] == ranges["qty"][0]:
        gap = 0.6 * (_height(d) or 6.0)
        while q and q[0]["x0"] - d[-1]["x1"] <= gap and \
                not re.fullmatch(r"[\d,]*\.?\d+", q[0]["text"]) and \
                not re.fullmatch(r"[A-Za-z]+[\d,.]*\d", q[0]["text"]):
            d.append(q.pop(0))
            WORD_STATS["description_overflow_words"] += 1
    return (" ".join(w["text"] for w in d).strip(),
            "".join(w["text"] for w in q).strip())


def money_cells(line, b):
    """(unit price, total) text of one bidder block on a line.

    The split is a margin left of the total column's caption; a NUMBER lying
    wholly left of that caption is still the unit price.  In a tab printed in
    a 4-5 pt font the margin is wider than a short price: Frisco 2103-057's
    '1.80' ends at x=337, the 'Total' caption starts at 340.6, the price was
    read as the total and the row dropped as unpriced (7 of 61 items, for
    every bidder).  Only when a number follows it under the caption, though:
    a total printed right-aligned and ending left of its own caption, with
    nothing after it, is the total (Addison 22-164's estimate column on its
    alternates page read '$1,300.00$1,300.00' as the unit price)."""
    p, t = [], []
    ws = words_in(line, (b["lo"], b["hi"]))
    for i, w in enumerate(ws):
        if w["x0"] < b["price"][1] or (
                not t and "tx" in b and w["x1"] <= b["tx"] + 0.5 and re.search(r"\d", w["text"])
                and any(re.search(r"\d", v["text"]) and v["x1"] > b["tx"] + 0.5 for v in ws[i + 1:])):
            p.append(w["text"])
        else:
            t.append(w["text"])
    if not t and len(p) >= 2:
        # two complete amounts in the price cell and none in the total cell:
        # the second is the total (a total column whose values start left of
        # the split -- Addison 22-164's estimate on its alternates page)
        for k in range(len(p) - 1, 0, -1):
            if AMOUNT_RE.fullmatch("".join(p[:k])) and AMOUNT_RE.fullmatch("".join(p[k:])):
                p, t = p[:k], p[k:]
                WORD_STATS["price_cell_amount_pairs_split"] += 1
                break
    return "".join(p).strip(), "".join(t).strip()


def split_glued_words(lines, page, lo, cut):
    """Split a word that starts in [lo, cut) and runs past `cut` at `cut`, by
    its characters.  A narrow code column printed edge to edge with the
    description ('PK329200' + 'Sod' -> 'PK329200Sod', 1.6 pt apart in a 4.8 pt
    font, under the 3 pt join tolerance) comes out as ONE word starting in the
    code column, and the description lost its first word."""
    chars = [c for c in getattr(page, "chars", []) if c.get("upright", True)]
    n = 0
    for l in lines:
        out = []
        for w in l["words"]:
            if not (lo <= w["x0"] < cut < w["x1"] - 1.0):
                out.append(w)
                continue
            cs = sorted((c for c in chars if w["x0"] - 0.5 <= c["x0"] < w["x1"]
                         and c["top"] < w["bottom"] and c["bottom"] > w["top"]
                         and c["text"].strip()), key=lambda c: c["x0"])
            a = [c for c in cs if c["x0"] < cut]
            b = [c for c in cs if c["x0"] >= cut]
            if not a or not b or "".join(c["text"] for c in a + b) != w["text"]:
                out.append(w)
                continue
            out.append({**w, "text": "".join(c["text"] for c in a), "x1": a[-1]["x1"]})
            out.append({**w, "text": "".join(c["text"] for c in b), "x0": b[0]["x0"]})
            n += 1
        l["words"] = out
        l["text"] = " ".join(w["text"] for w in out)
    WORD_STATS["glued_words_split"] += n
    return n


def extract_bidder_names(lines, hdr, ranges, by_phrase=True):
    """Contractor names sit above the header, centred over their whole block and
    often wrapped across two lines. Lines that also carry something in the left
    label margin are page furniture (BID OPENED / PROJECT No. / city, ST) and
    are skipped.  by_phrase=False files each word by its own x0 (the reading
    before name_words_by_block; see page_bidder_names)."""
    names = [""] * len(ranges["blocks"])
    left_label = ranges["desc"][0]
    cand = []
    for l in lines:
        if l["top"] >= hdr["top"] - 1 or not l["words"]:
            continue
        # a line with a form's fill-in blank ('Issued: ____ Invoice: ____',
        # 'CHECKED BY: ____') is form furniture, none of it a name (round 3:
        # Grand Prairie's Briar Hill packet p.83, an invoice, went out as a
        # bidder named '_____________')
        if "_____" in l.get("text", ""):
            continue
        cand.append(l)

    # A page title spans the full width, so its words also land inside the name
    # band. Flag those lines rather than discarding them: discarding cost 15
    # files their names outright, because some templates do put the name band on
    # the same visual line as a left-margin label.
    spans_left = {id(l): (min(w["x0"] for w in l["words"]) < left_label) for l in cand}
    by_block = {id(l): (name_words_by_block(l, ranges["blocks"]) if by_phrase else
                        [[w for w in l["words"] if b["lo"] <= w["x0"] < b["hi"]]
                         for b in ranges["blocks"]]) for l in cand}

    # ---- the TITLE block above the names ----------------------------------
    # Bedford's Parkwood Drive tab (Huitt-Zollars) centres 'BID TABULATION
    # REPORT', 'CLIENT: CITY OF BEDFORD' and 'DESCRIPTION: PARKWOOD DRIVE
    # RECONSTRUCTION (PAVING, DRAINAGE, ...)' over the money block, above the
    # firms.  Cut into the bidder blocks, the titles became the first words of
    # every name ('REPORT (PAVING, DRAINAGE, WATER, SANITARY BIDDERS FORT WORTH
    # CIVIL CONSTRUCTORS, LLC'), the set was refused as too long, and all 654
    # rows of the coordinate parse went out with no bidder.  A title is one
    # continuous run of words (no gap wider than the words are tall) that
    # crosses into two or more bidder blocks and reads as a title: a caption
    # with a colon, or page furniture ('BID TABULATION').  Width alone proves
    # nothing: Collin County prints its firm names so close together that
    # one 'run' crosses three blocks.  And it takes a title BLOCK, two or more
    # such lines: a single caption line ('Prepared by Halff Associates, Inc.'
    # in Frisco 2410-003, 'IFB: 2022-055, PRINTING: ...' in Collin County
    # 51844) is left as before.  The lowest title line and everything above it
    # are not names.
    titles = [l["top"] for l in cand if _title_run(l["words"], ranges["blocks"])]
    if len(titles) >= 2:
        title_top = max(titles)
        cand = [l for l in cand if l["top"] > title_top + LINE_TOL]

    for k, b in enumerate(ranges["blocks"]):
        parts = []
        for l in cand:
            toks = [w["text"] for w in by_block[id(l)][k]]
            if not toks:
                continue
            frag = re.sub(r"\s+", " ", " ".join(toks)).strip()
            if not frag or len(frag) > 60:
                continue
            # Page furniture is judged on the FRAGMENT, never the whole line:
            # several templates print the contractor names on the same visual
            # line as "BID OPENED: 07/30/2024 @ 2:00 P.M.", and rejecting that
            # line threw away every name in 15 files.
            if NOT_A_NAME_RE.search(frag) or CITY_STATE_RE.match(frag):
                continue
            if FURNITURE_RE.search(frag) or BIDDER_LABEL_RE.match(frag):
                continue
            if ITEM_LABEL_RE.match(frag) or caption_only(frag):
                continue
            if len(frag) <= 2 and not frag.isalpha():
                continue
            # a banner running across the page ("ARLINGTON CITY LIMITS ...") --
            # and the tail of a title that starts over the item/description
            # columns and runs into this block without a break: firms are
            # printed over the money blocks ('... ELEVATED STORAGE TANK SITE'
            # over Frisco 2205-058's only firm went out as 'TANK SITE Landmark
            # Structures').  A firm name that merely overhangs the first block
            # ('SYB Construction Company, Inc' in Irving ITB-163D-20F) starts
            # right of the description column and is no title.
            if frag.upper() == frag and (
                    (spans_left.get(id(l)) and len(frag) > 12)
                    or _runs_from_margin(l["words"], b["lo"], ranges["desc"][1])):
                continue
            # the bare caption over the bidder columns ('BIDDERS', 'VENDORS')
            if BIDDERS_CAPTION_RE.match(frag):
                continue
            parts.append((l["top"], frag))
        parts.sort()
        nm = re.sub(r"\s+", " ", " ".join(p[1] for p in parts)).strip(" ,.")
        # a trailing city/state can still ride along on the same visual line
        nm = re.sub(r",?\s*[A-Z][A-Za-z.\-' ]*,\s*(%s)\.?(\s+\d{5})?$" % STATES, "", nm).strip(" ,.")
        names[k] = nm
    return names


def caption_only(frag):
    """'Bid Amount', 'UNIT PRICE (NUMBER ONLY)' -- caption words and nothing else."""
    toks = re.findall(r"[a-z#]+", frag.lower())
    return bool(toks) and all(t in CAPTION_WORDS for t in toks)


def name_words_by_block(l, blocks):
    """The words of a name-band line over each bidder block.

    Judged by whole runs of words, not word by word: a name centred over its
    two columns starts left of its block's first anchor, and word by word its
    first word was filed under the bidder to the left -- Southlake's Timarron
    Creek tab read 'PROJECT ERS of MS, Inc. DAVID-TEHOUNGUE' and 'Ltd. Co'.
    Each run goes to the block it overlaps most; a run lying mostly outside
    every block is a title or a margin label ('... EROSION PROTECTION
    PROJECT' ending over the first block) and goes nowhere.  A run straddling
    two blocks about evenly is split word by word, by each word's centre."""
    out = [[] for _ in blocks]
    if not blocks:
        return out
    h = _height(l["words"]) or 6.0
    lo_all, hi_all = blocks[0]["lo"], blocks[-1]["hi"]
    for ph in phrases(l, 0.8 * h):
        width = max(ph["x1"] - ph["x0"], 0.1)
        if min(ph["x1"], hi_all) - max(ph["x0"], lo_all) < 0.5 * width:
            continue
        ov = [max(0.0, min(ph["x1"], b["hi"]) - max(ph["x0"], b["lo"])) for b in blocks]
        best = max(range(len(blocks)), key=lambda k: ov[k])
        if len(ov) > 1 and sorted(ov)[-2] >= 0.3 * width:
            for w in ph["words"]:
                c = (w["x0"] + w["x1"]) / 2.0
                for k, b in enumerate(blocks):
                    if b["lo"] <= c < b["hi"]:
                        out[k].append(w)
                        break
            continue
        out[best].extend(ph["words"])
    return out


# 'CONTRACTOR' is no such caption: it ends wrapped firm names ('Rose & Two
# Thorns Landscaping & General / Contractor', Collin County 2023-138).
BIDDERS_CAPTION_RE = re.compile(r"^(bidders?|vendors?|respondents?)\W*$", re.I)


def _runs(words):
    """Runs of words on one line with no gap wider than the words are tall."""
    out = []
    for w in sorted(words, key=lambda w: w["x0"]):
        h = max(w["bottom"] - w["top"], 1.0)
        if out and w["x0"] - out[-1][-1]["x1"] <= h:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def _title_run(words, blocks):
    """True when one run of these words reads as a page title across the
    bidder blocks (see extract_bidder_names)."""
    for run in _runs(words):
        hit = {k for w in run for k, b in enumerate(blocks) if b["lo"] <= w["x0"] < b["hi"]}
        if len(hit) < 2:
            continue
        text = " ".join(w["text"] for w in run)
        if ":" in text or FURNITURE_RE.search(text):
            return True
    return False


def _runs_from_margin(words, lo, start):
    """True when one run of these words starts left of x=start (over the
    item/description columns) and reaches x=lo (a title written into a block)."""
    for run in _runs(words):
        if run[0]["x0"] < start and any(w["x0"] >= lo for w in run):
            return True
    return False


def looks_like_name_set(names):
    """Accept a page's names only if they actually look like company names --
    otherwise a malformed page would overwrite good names carried from page 1."""
    good = [n for n in names if n]
    if not good:
        return False
    return all(len(n) <= 60 and not NOT_A_NAME_RE.search(n) for n in good)


def page_bidder_names(lines, hdr, ranges):
    """The page's bidder names (judged by runs of words), or None.

    Judging runs puts a word in the right block more often, and so makes some
    names longer than the word-by-word reading did: Collin County 2022-324's
    'Vulcan Construction Materials LLC dba Vulcan Materials Company' (62
    characters; word by word its 'Vulcan' was lost to the left), a name read
    letter by letter, or one followed by a contact person -- and
    looks_like_name_set refused the whole page for it, so every bidder lost
    its name.  When the run-judged set fails that check but the word-by-word
    set passes it, the run-judged set is still taken if no name is longer
    than 60 characters once spaces are ignored; otherwise the word-by-word
    set is.  Judging by runs never costs a page its names."""
    names = extract_bidder_names(lines, hdr, ranges)
    if looks_like_name_set(names):
        return names
    old = extract_bidder_names(lines, hdr, ranges, by_phrase=False)
    if not looks_like_name_set(old):
        return None
    if any(names) and all(len(re.sub(r"\s+", "", n)) <= 60 and not NOT_A_NAME_RE.search(n)
                          for n in names if n):
        WORD_STATS["long_run_names_kept"] += 1
        return names
    WORD_STATS["names_by_word_fallback"] += 1
    return old


def parse_pdf_by_coords(path):
    """Yield one dict per (line item x bidder). Empty list if no header found."""
    recs = []
    base = os.path.basename(path)
    names = None
    prev_ranges = None
    # A funding section runs until the next banner, and banners are printed once.
    # Re-initialising this inside the page loop relabelled every continuation
    # page as "BASE BID": 5,036 of arlington's 7,465 rows ended up in a
    # fabricated BASE BID bucket, mixing MOBILIZATION / PAVING / BOND /
    # STORMWATER / SANITARY, which have completely different funding.
    section = "BASE BID"
    prev_shape = None
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            rotated_before = WORD_STATS["pages_rotated_text"]
            try:
                words = extract_words_robust(page)
            except Exception:
                continue
            shape = (round(float(page.width)), round(float(page.height)),
                     WORD_STATS["pages_rotated_text"] > rotated_before)
            lines = group_lines(words)
            headers = page_headers(lines)
            inherited = False
            if headers:
                hdr0 = headers[0][1]
                # The last bidder's block runs to the right edge of the TEXT: on
                # a table turned 90 degrees the words run past page.width, and
                # Kirby Creek's right-most bidder (McMahon) lost its name and
                # its extended prices to the page box.
                ranges = build_ranges(hdr0, max([float(page.width)]
                                                + [w["x1"] + 1.0 for w in words]))
                if ranges.get("reordered"):
                    # (round 10) its item / description columns are not adjacent; its centred
                    # columns are split where the rows leave a gutter
                    refine_reordered(lines, hdr0["top"], ranges)
                elif not refine_description_start(lines, hdr0["top"], ranges):
                    ranges = refine_item_column(lines, ranges, hdr0["top"])
                prev_ranges, prev_shape = ranges, shape
                header_tops = [t for _, h in headers for t in h.get("band", [h["top"]])]
                top_limit = hdr0["top"]
                page_names = page_bidder_names(lines, hdr0, ranges)
                if page_names:
                    names = page_names
            elif prev_ranges is not None and shape == prev_shape:
                # A continuation page: the table carries on with the same column
                # geometry but the caption row is not reprinted.  read_pdf has
                # inherited headers across pages for exactly this reason; this
                # parser did not, so continuation pages were dropped whole --
                # collin 58471 lost items 13-37 (the 42"-120" culvert price
                # gradient, i.e. the large-diameter end of the curve) and
                # mesquite 2024-013 lost pages 2-4, 105 priced items down to 33.
                #
                # ... but only while the pages in between ARE the table: same
                # page shape, and every inherited page yielding item rows (an
                # item number or a quantity).  Inherited without that check,
                # Grand Prairie's 115-page council packet read the Kirby Creek
                # tab's columns onto council checklists, RFQ score sheets, fee
                # proposals and pump data sheets 8-58 pages later, as rows
                # under the Kirby bidders' names.
                ranges = prev_ranges
                header_tops = []
                top_limit = None
                inherited = True
            else:
                prev_ranges = None
                continue

            # Never skip a page because the names could not be read: the prices
            # are still correct and the name can be repaired separately. Dropping
            # the rows would silently lose whole files.
            cur = list(names) if names else []
            cur = (cur + [""] * len(ranges["blocks"]))[:len(ranges["blocks"])]

            if ranges.get("glued"):
                split_glued_words(lines, page, *ranges["glued"])
            lines = merge_split_rows(lines, ranges, header_tops, top_limit)
            events = []
            page_recs = []
            h_page = _height([w for l in lines for w in l["words"]]) or 6.0
            numbered_rows = sum(1 for l in lines if ITEM_RE.match(spaced(l, ranges["item"]))
                                and any(p for p, _ in (money_cells(l, b) for b in ranges["blocks"])))
            numbered = None     # (line, item number, description) of an unpriced item line
            consumed = set()    # (round 10) lines given to the priced line above (number_from_line_below)
            for li, l in enumerate(lines):
                pending, numbered = numbered, None
                if header_tops and any(abs(l["top"] - ht) <= LINE_TOL
                                       for ht in header_tops):
                    events.append(("stop", l))
                    continue
                # Rows ABOVE the first header are not all furniture.  Dropping
                # them outright cost 1,379 priced rows across 24 arlington PDFs
                # (~765 actually lost after strategy selection), including the
                # whole BOND FUNDED IMPROVEMENTS section of PWSM20001 -- 5.9% of
                # that project's base bid, and high-frequency comparable work
                # (4" sidewalk $68 vs $84.50/SY, ADA ramps, retaining wall).
                # So: keep a row above the header if it carries money, and skip
                # it only when it does not, so a title band cannot be mistaken
                # for a section banner.  Decided after `money` is computed.
                above_header = top_limit is not None and l["top"] < top_limit

                item_no = spaced(l, ranges["item"])
                desc, qty_s = desc_and_qty(l, ranges)
                unit_s = spaced(l, ranges["unit"])
                money = [money_cells(l, b) for b in ranges["blocks"]]
                has_money = any(p for p, _ in money)

                if AGG_RE.search(item_no) or AGG_RE.search(desc):
                    events.append(("stop", l))
                    continue
                if li in consumed:
                    continue                  # (round 10) a number line given to the priced line above
                whole = " ".join(w["text"] for w in l["words"]).strip()
                if (not above_header and not qty_s and not unit_s and whole
                        and SECTION_BANNER_RE.match(whole)):
                    # (round 10) a banner anywhere on its line -- over the money columns, where
                    # money_cells reads any text as a cell -- and in any case (SECTION_BANNER_RE)
                    section = whole[:60].upper()
                    WORD_STATS["section_banners_any_case_or_column"] += 1
                    events.append(("stop", l))
                    continue
                if not has_money:
                    if above_header:
                        # Title block / bidder-name band, not a banner.
                        continue
                    # banner (section title) or a wrapped description line
                    txt_left = (item_no + " " + desc).strip()
                    if not txt_left:
                        continue
                    nb = numbered_banner(item_no, desc, qty_s, unit_s)
                    if nb:
                        section = nb                  # (round 11) '11 ALTERNATES'
                        WORD_STATS["numbered_section_banners"] += 1
                        events.append(("stop", l))
                        continue
                    if ITEM_RE.match(item_no):
                        numbered = (l, item_no, desc)
                        continue
                    if (pending is not None and not item_no and desc and numbered_rows >= 2
                            and li + 1 < len(lines)
                            and adoptable_priced_line(lines[li + 1], l, ranges, h_page)):
                        # one more description line between the number and the
                        # prices (Addison 22-164 alternates items 8 and 14: '8' /
                        # 'CEMENT TREATED (NEW BASE WITH' / the priced line): it
                        # is that item's, not a section banner
                        numbered = (l, pending[1], (pending[2] + " " + desc).strip())
                        continue
                    letters = re.sub(r"[^A-Za-z]", "", txt_left)
                    # (round 10) in a reordered layout (reordered_ranges) the centred description
                    # wraps above and below its prices in capitals: a line opens a section only
                    # from the item column, as its 'Base Bid' / 'Alternate 1' banners do
                    if (letters and letters.upper() == letters and len(txt_left) > 4
                            and not qty_s and not (ranges.get("reordered") and not item_no)):
                        # (round 11) a group number glued to its title ('9ALTERNATES', Frisco's Buffalo
                        # Ridge Park tab) is spaced, so the title reads as the section it names
                        section = re.sub(r"^(\d{1,2})(?=[A-Z]{3})", r"\1 ", txt_left[:60].upper())
                        events.append(("stop", l))
                    elif desc:
                        # a wrapped description line -- which item it belongs to
                        # is decided once the page is read (attach_wrapped_lines)
                        events.append(("wrap", l, desc,
                                       desc_words(l, ranges)[0]["x0"]))
                    continue
                item_no, desc = number_from_line_above(pending, l, item_no, desc,
                                                       qty_s or unit_s, h_page, numbered_rows)
                if not item_no and not desc:
                    nb = number_from_line_below(lines, li, ranges, qty_s or unit_s, h_page,
                                                numbered_rows)
                    if nb is not None:
                        item_no, desc = nb
                        consumed.add(li + 1)
                if not ITEM_RE.match(item_no) and not desc:
                    # a priced line that is no item: a total under another caption
                    events.append(("stop", l))
                    continue

                group = []
                for k, (p, t) in enumerate(money):
                    if not p:
                        continue
                    group.append({
                        "src_file": base,
                        "src_sheet": "coords_p%d" % (pi + 1),
                        "bid_section": section,
                        "item_no": item_no,
                        "description": desc,
                        "unit_raw": unit_s,
                        "quantity_raw": qty_s,
                        "bidder_raw": cur[k],
                        "unit_price_raw": p,
                        "extended_raw": t,
                    })
                page_recs.extend(group)
                dw = desc_words(l, ranges)
                events.append(("row", l, group, dw[0]["x0"] if dw else None)
                              if group else ("stop", l))
            attach_wrapped_lines(events, lines, centred=bool(ranges.get("reordered")))
            if inherited and not item_rows(page_recs) and not repeats_price_captions(lines, ranges):
                WORD_STATS["inherited_pages_refused"] += bool(page_recs)
                prev_ranges = None
                continue
            recs.extend(page_recs)
    return recs


def item_rows(recs):
    """True when some record of a page carries a money-shaped price AND an item
    number, a quantity or a unit -- what a page continuing a bid table has
    and a letter or memo does not (a year landing in the quantity column of a
    memo whose 'prices' are words is no item row).  A unit may be priced per
    unit in its own cell (Mansfield's '$ Per SY') or glued to the quantity
    (Collin County 2024-346's '9ACRE' in the quantity cell, Lewisville
    25-4410's '150Cubic Yard' in the unit cell)."""
    return any(re.fullmatch(r"\(?-?\$?[\d,]*\.?\d+\)?\$?", (r["unit_price_raw"] or "").replace(" ", ""))
               and (ITEM_TOKEN_RE.match(r["item_no"] or "")
                    or re.fullmatch(r"[\d,]*\.?\d+(?:[A-Za-z]{1,5}\.?)?", r["quantity_raw"] or "")
                    or re.fullmatch(r"(?:\$\s*|[\d,]*\.?\d+\s*)?[A-Za-z][A-Za-z$ ./]{0,15}",
                                    r["unit_raw"] or ""))
               for r in recs)


def number_from_line_above(pending, l, item_no, desc, has_qty_or_unit, h, numbered_rows):
    """(item number, description) of a priced line, taken from the unpriced
    line just above it when the priced line has neither but has a quantity or
    unit, and the line above (pending: (line, item number, description)) is
    within two line heights.  The item's number and first description line
    are printed on the line ABOVE its prices: a top-aligned cell beside cells
    centred on a three-line row (Addison 22-164 items 9, 19 and 28: '9 CEMENT
    TREATED (NEW BASE WITH LIQUID' over 'TX-275-6.1 S.Y. 210 $65.00 ...').
    The priced line was dropped as a total line, and the item with it.
    Only where such rows are the exception: the page must also have priced
    lines carrying their own item number (numbered_rows, 2 or more).  A tab
    printing EVERY item's prices on a line of their own under a description
    of several lines (Little Elm 2023-08) is another layout, which this rule
    reads only in part."""
    if (not item_no and not desc and has_qty_or_unit and pending is not None
            and numbered_rows >= 2 and 0 < l["top"] - pending[0]["top"] <= 2.0 * h):
        WORD_STATS["item_number_from_line_above"] += 1
        return pending[1], pending[2]
    return item_no, desc


def number_from_line_below(lines, li, ranges, has_qty_or_unit, h, numbered_rows):
    """(round 10, .31) (item number, description) for the priced line lines[li] that has neither, from
    the unpriced line just BELOW it: an item whose description wraps around its prices, the number
    printed with the description's LAST line.  Celina's Public Works Renovation Phase 2 tab prints
    item 1.38 as 'Reinforced Concrete Slab for Storage' / '1 LS $125,000.00 ...' / '1.38 Building';
    the priced line was dropped as a total and its first line went to item 1.37, so every one of the
    13 bidders lost $125,000 from the base bid.  The line below must be within two line heights,
    carry no money and an item number; the line after it must not be a priced line without a number
    (that one would take the number: number_from_line_above); and, as there, the page must have
    numbered priced lines of its own (numbered_rows, 2 or more)."""
    if not has_qty_or_unit or numbered_rows < 2 or li + 1 >= len(lines):
        return None
    l, nxt = lines[li], lines[li + 1]
    if not 0 < nxt["top"] - l["top"] <= 2.0 * h:
        return None
    if any(p for p, _ in (money_cells(nxt, b) for b in ranges["blocks"])):
        return None
    item = spaced(nxt, ranges["item"])
    if not ITEM_RE.match(item):
        return None
    d2, q2 = desc_and_qty(nxt, ranges)
    if q2 or spaced(nxt, ranges["unit"]):
        return None
    if li + 2 < len(lines) and adoptable_priced_line(lines[li + 2], nxt, ranges, h):
        return None
    WORD_STATS["item_number_from_line_below"] += 1
    return item, d2


def adoptable_priced_line(l2, l, ranges, h):
    """True when line l2 (just below line l) is a priced line with no item
    number and no description but a quantity or unit -- the line
    number_from_line_above gives a number to."""
    if l2["top"] - l["top"] > 2.0 * h or spaced(l2, ranges["item"]):
        return False
    d2, q2 = desc_and_qty(l2, ranges)
    return (not d2 and bool(q2 or spaced(l2, ranges["unit"]))
            and any(p for p, _ in (money_cells(l2, b) for b in ranges["blocks"])))


def repeats_price_captions(lines, ranges):
    """True when a line of the page reprints a unit-price caption inside every
    inherited bidder block ('Qty | Unit Price | Extended Price' per bidder,
    the item and description captions left off): the table goes on even where
    its rows have no item-number, quantity or unit cell -- Collin County's
    Cisco maintenance tab (2025) prints part numbers and prices on page 2,
    and item_rows alone refused the page."""
    blocks = ranges["blocks"]
    if len(blocks) < 2:
        return False
    for l in lines:
        ws = l["words"]
        t = [w["text"].upper().strip(":") for w in ws]
        xs = [ws[i]["x0"] for i in range(len(t))
              if t[i] == "UNITPRICE" or (t[i] == "UNIT" and i + 1 < len(t)
                                         and t[i + 1] in ("PRICE", "COST"))]
        if len(xs) >= len(blocks) and all(any(b["lo"] <= x < b["hi"] for x in xs)
                                          for b in blocks):
            return True
    return False


def merge_split_rows(lines, ranges, header_tops, top_limit):
    """Put back together a row whose cells were drawn at different heights.

    A line holding only an item number, quantity and/or unit -- no price, no
    description -- within a fraction of a line of a priced line that has none
    of those fields is the same row.  On Grand Prairie's Kirby Creek tab (a
    table turned 90 degrees, its fonts of two sizes) '102  1  LS' sits 2 pt
    from the line carrying item 102's prices, beyond LINE_TOL, so the item had
    no number, quantity or unit; on Southlake's North Park tab item 85's
    quantity sits on a line of its own between the two lines of its
    description."""
    if not lines:
        return lines
    h = _height([w for l in lines for w in l["words"]]) or 6.0
    near = max(2.5, 0.75 * h)

    def fields(l):
        return ([joined(l, b["price"]) for b in ranges["blocks"]],
                spaced(l, ranges["item"]), spaced(l, ranges["desc"]),
                joined(l, ranges["qty"]), spaced(l, ranges["unit"]))
    info = [fields(l) for l in lines]
    banned = set()
    for i, l in enumerate(lines):
        if any(abs(l["top"] - t) <= LINE_TOL for t in header_tops):
            banned.add(i)
    out = list(lines)
    gone = set()
    for i, l in enumerate(lines):
        prices, item, desc, qty, unit = info[i]
        if i in banned or any(prices) or desc or not (item or qty or unit):
            continue
        if top_limit is not None and l["top"] < top_limit:
            continue
        if item and not ITEM_TOKEN_RE.match(item):
            continue
        if qty and not re.fullmatch(r"[\d,]*\.?\d+", qty):
            continue
        if unit and (len(unit) > 12 or re.search(r"\d", unit)):
            continue
        best = None
        for j in (i - 2, i - 1, i + 1, i + 2):
            if not 0 <= j < len(lines) or j in gone or j in banned:
                continue
            p2, it2, d2, q2, u2 = info[j]
            if not any(p2) or AGG_RE.search(it2) or AGG_RE.search(d2):
                continue
            if (item and it2) or (qty and q2) or (unit and u2):
                continue
            d = abs(lines[j]["top"] - l["top"])
            if d <= near and (best is None or d < best[0]):
                best = (d, j)
        if best is None:
            continue
        j = best[1]
        ws = sorted(out[j]["words"] + l["words"], key=lambda w: w["x0"])
        out[j] = {**out[j], "words": ws, "x0": ws[0]["x0"],
                  "text": " ".join(w["text"] for w in ws)}
        info[j] = fields(out[j])
        gone.add(i)
        WORD_STATS["split_rows_merged"] += 1
    return [l for i, l in enumerate(out) if i not in gone]


def attach_wrapped_lines(events, lines, centred=False):
    """Give each wrapped description line to the item it belongs to.

    It used to go to the item ABOVE it.  Spreadsheet tabs centre single-line
    cells vertically, so a description of several lines starts ABOVE its own
    item line: Southlake's Timarron Creek 1.07 ('Gravel Fill for Engineered
    Bank Stabilization System &' / '1.07 ... CY 102' / 'Pyramattress'),
    Arlington's Kee Branch 313 and every item of Grand Prairie's Kirby Creek
    tab gave their first lines to the item above.  For a run of such lines
    between two items, the widest vertical gap in the run is the boundary
    between the two cells: lines before it belong to the item above, lines
    after it to the item below.  Where no gap stands out (rows spaced like
    wrapped lines) everything stays with the item above, as before.  A line
    after a total, banner or header line can only belong to the item below,
    and one before such a line only to the item above.  A line given to the
    item BELOW must start where the descriptions start and must not be a
    group caption ('Bid Schedule 1.00:'): a title centred in the column ('2.
    Field Lighting Upgrades' after a subtotal) is nobody's description and is
    dropped -- it used to be glued to the item above the subtotal, as was a
    footnote under a total line ('*Denotes correction by Engineer')."""
    h = _height([w for l in lines for w in l["words"]]) or 6.0
    starts = sorted(e[3] for e in events if e[0] == "row" and e[3] is not None)
    ref = starts[len(starts) // 2] if starts else None
    i, n = 0, len(events)
    while i < n:
        if events[i][0] != "wrap":
            i += 1
            continue
        j = i
        while j < n and events[j][0] == "wrap":
            j += 1
        run = events[i:j]
        above = events[i - 1] if i > 0 and events[i - 1][0] == "row" else None
        below = events[j] if j < n and events[j][0] == "row" else None
        split = len(run)                      # run[:split] -> above, the rest -> below
        if above is None:
            split = 0
        elif below is not None:
            tops = [above[1]["top"]] + [e[1]["top"] for e in run] + [below[1]["top"]]
            gaps = [b - a for a, b in zip(tops, tops[1:])]
            k = max(range(len(gaps)), key=lambda x: gaps[x])
            rest = [g for x, g in enumerate(gaps) if x != k]
            if not rest or gaps[k] >= 1.25 * max(rest):
                split = k
        if above is not None:
            for e in run[:split]:
                for g in above[2]:
                    g["description"] = (g["description"] + " " + e[2]).strip()
        # (round 10) centred: a description column centred under its caption (reordered_ranges)
        # starts each line where its width puts it, so the start is not asked of it
        fwd = [e[2] for e in run[split:]
               if below is not None and ref is not None
               and (centred or abs(e[3] - ref) <= max(8.0, h))
               and not GROUP_LABEL_RE.search(e[2])]
        if fwd:
            WORD_STATS["wrapped_lines_to_item_below"] += len(fwd)
            for g in below[2]:
                g["description"] = (" ".join(fwd) + " " + g["description"]).strip()
        i = j



# ---- self-test ----------------------------------------------------------------
def _sw(text, x0, top, x1=None, h=8.0):
    """A synthetic word, the shape pdfplumber returns."""
    return {"text": text, "x0": float(x0), "x1": float(x1 if x1 is not None else x0 + 4.0 * len(text)),
            "top": float(top), "bottom": float(top) + h, "upright": True}


def selftest_checks():
    """(name, got, want) for the coordinate reader's layout rules, each on a
    small synthetic page shaped like the tab that needed it, with the
    look-alikes it must leave alone.  Run by parse_bidtabs --selftest."""
    out = []

    def check(name, got, want):
        out.append((name, got, want))

    # 1. 'UNIT PRICE | AMOUNT' per bidder: every bare AMOUNT is a total
    h1 = find_header(group_lines([_sw(t, x, 60, h=4.4) for t, x in (
        ("ITEM", 19), ("NO.", 30), ("DESCRIPTION", 138), ("UNIT", 271), ("QTY", 297),
        ("UNIT", 327), ("PRICE", 338), ("AMOUNT", 375), ("UNIT", 419), ("PRICE", 429),
        ("AMOUNT", 466), ("UNIT", 510), ("PRICE", 520), ("AMOUNT", 557))])[0])
    check("bare AMOUNT after UNIT PRICE", (h1["price_x"], h1["total_x"], h1["unit_x"], h1["qty_x"]),
          ([327.0, 419.0, 510.0], [375.0, 466.0, 557.0], 271.0, 297.0))
    r1 = build_ranges(h1, 612.0)
    check("UNIT left of QTY: the quantity range follows the unit range",
          (r1["unit"], r1["qty"]), ((259.0, 285.0), (285.0, 315.0)))

    # 2. a header over three lines (Timarron Creek): band completes the anchors,
    #    '(with unit price in words)' under DESCRIPTION is no price caption
    tim = [_sw(t, x, 136.7, x1, h=11.0) for t, x, x1 in (
        ("BID", 81, 96), ("DESCRIPTION", 204, 264), ("UNIT", 462, 485), ("TOTAL", 530, 559),
        ("UNIT", 604, 626), ("TOTAL", 671, 700))]
    tim += [_sw(t, x, 143.3, x1, h=11.0) for t, x, x1 in (("UNIT", 368, 391), ("QTY", 409, 428))]
    tim += [_sw(t, x, 150.5, x1, h=11.0) for t, x, x1 in (
        ("NO.", 81, 98), ("(with", 178, 201), ("unit", 203, 221), ("price", 224, 246),
        ("in", 249, 257), ("words)", 259, 290), ("PRICE", 461, 486), ("PRICE", 531, 557),
        ("PRICE", 602, 628), ("PRICE", 672, 698))]
    tim += [_sw("1.01", 80, 178.1, 100, h=11.0), _sw("Mobilization", 110, 178.1, 166, h=11.0),
            _sw("LS", 375, 178.1, 384, h=11.0), _sw("1", 416, 178.1, 421, h=11.0),
            _sw("35,000.00", 458, 178.1, 503, h=11.0), _sw("35,000.00", 529, 178.1, 574, h=11.0)]
    lines = group_lines(tim)
    hs = page_headers(lines)
    hd = hs[0][1] if hs else {}
    check("three-line header", (len(hs), hd.get("price_x"), hd.get("total_x"), hd.get("qty_x"),
                                hd.get("unit_x"), hd.get("desc_x"), len(hd.get("band", []))),
          (1, [461.0, 602.0], [530.0, 671.0], 409.0, 368.0, 178.0, 3))
    r2 = build_ranges(hd, 792.0)
    l1 = [l for l in lines if abs(l["top"] - 178.1) < 1][0]
    check("three-line header: unit and quantity read",
          (spaced(l1, r2["unit"]), desc_and_qty(l1, r2)[1]), ("LS", "1"))
    # a wide caption over two columns of the next line is no plain band
    wide = group_lines([_sw("TOTAL", 300, 50, 400, h=6.0), _sw("DESCRIPTION", 100, 56, 150, h=6.0),
                        _sw("UNIT", 300, 56, 320, h=6.0), _sw("PRICE", 322, 56, 340, h=6.0),
                        _sw("AMOUNT", 360, 56, 390, h=6.0)])
    check("caption over two columns: no band", stacked_header(wide, 1), None)

    # 3. no single-line header at all (Frisco 2103-057), with a spec-code
    #    column ('Items') between the item number and the description
    fr = [_sw(t, x, 60.9, x1, h=4.8) for t, x, x1 in (
        ("#", 40, 42), ("Items", 61, 72), ("Item", 82, 91), ("Unit", 264, 270))]
    fr += [_sw(t, x, 63.8, x1, h=4.8) for t, x, x1 in (
        ("Quantity", 242, 255), ("of", 264, 266), ("Unit", 300, 306), ("Total", 341, 349),
        ("Unit", 391, 397), ("Total", 422, 429))]
    fr += [_sw(t, x, 68.4, x1, h=4.8) for t, x, x1 in (
        ("Description", 82, 105), ("Required", 242, 255), ("Measure", 264, 276), ("Price", 299, 306),
        ("Cost", 341, 347), ("Price", 390, 397), ("Cost", 421, 427))]
    hs = page_headers(group_lines(fr))
    hd = hs[0][1] if hs else {}
    check("header band with no single-line header",
          (len(hs), hd.get("item_x"), hd.get("item_hi_x"), hd.get("desc_x"), hd.get("qty_x"),
           hd.get("unit_x"), hd.get("price_x"), hd.get("total_x")),
          (1, 40.0, 61.0, 82.0, 242.0, 264.0, [299.0, 390.0], [341.0, 421.0]))
    r3 = build_ranges(hd, 792.0)
    check("spec column: the item range stops before it", r3["item"], (0.0, 49.0))
    b0 = r3["blocks"][0]
    lp = group_lines([_sw("$", 300, 173.6, 302.7, h=4.8), _sw("1.80", 328.7, 173.6, 337.1, h=4.8),
                      _sw("$", 342.1, 173.6, 344.6, h=4.8),
                      _sw("37,827.00", 365.9, 173.6, 385.1, h=4.8)])[0]
    check("a short unit price inside the total's margin is the price", money_cells(lp, b0),
          ("$1.80", "$37,827.00"))
    lt = group_lines([_sw("$", 331, 10, 334), _sw("12.00", 346, 10, 368)])[0]
    check("a total's '$' left of the caption stays the total's", money_cells(lt, b0), ("", "$12.00"))
    l2 = group_lines([_sw("$1,300.00", 290.0, 20, 318.0, h=4.8), _sw("$1,300.00", 312.0, 20, 338.0, h=4.8)])[0]
    check("two amounts in the price cell, none in the total: the second is the total",
          money_cells(l2, b0), ("$1,300.00", "$1,300.00"))
    la = group_lines([_sw("$5.00", 300.0, 20, 318.0, h=4.8), _sw("40.00", 330.0, 20, 340.5, h=4.8)])[0]
    check("a total ending left of its caption with nothing after it stays the total",
          money_cells(la, b0), ("$5.00", "40.00"))

    # 4. a DESCRIPTION caption centred over a wide column (North Park)
    rows = [_sw("DESCRIPTION", 138, 60, 163)]
    for k, (n, t) in enumerate((("0", "Mobilization"), ("1", "Provide"), ("2", "Implement"),
                                ("3", "Remove"))):
        rows += [_sw(n, 27, 66 + 6 * k, 29), _sw(t, 38.4, 66 + 6 * k)]
    rr = {"item": (0.0, 126.0), "desc": (126.0, 285.0)}
    refine_description_start(group_lines(rows), 60.0, rr)
    check("centred DESCRIPTION caption: descriptions start at the text", (rr["item"], rr["desc"]),
          ((0.0, 32.4), (32.4, 285.0)))
    codes = [_sw("DESCRIPTION", 120, 60)]
    for k, ws in enumerate((("101", "Mobilization"), ("102", "Excavation"), ("103", "SP", "Paving"),
                            ("104", "Grading"))):
        codes += [_sw(ws[0], 20, 66 + 6 * k)]
        codes += [_sw(t, 40 if len(ws) == 3 and j == 0 else 60, 66 + 6 * k)
                  for j, t in enumerate(ws[1:])]
    rc = {"item": (0.0, 108.0), "desc": (108.0, 285.0)}
    check("codes of varying width beside the item number: no move",
          (refine_description_start(group_lines(codes), 60.0, rc), rc["item"]), (False, (0.0, 108.0)))
    secs = [_sw("Description", 148, 60)]
    for k, ws in enumerate((("100", "Miscellaneous", "Items"), ("101", "General", "Site"),
                            ("102", "Mobilization"), ("103", "Tree", "Protection"),
                            ("200", "Demolition", "Items"), ("201", "Demolish", "and"))):
        xs = (21, 35, 87) if ws[0].endswith("00") else (30, 54, 87)
        secs += [_sw(t, xs[j], 66 + 6 * k) for j, t in enumerate(ws)]
    rs = {"item": (0.0, 136.8), "desc": (136.8, 280.0)}
    check("section lines starting left of the column do not stop the move",
          (refine_description_start(group_lines(secs), 60.0, rs), rs["item"]), (True, (0.0, 48.0)))

    # 5. a description running into the quantity column's margin
    rq = {"desc": (92.0, 192.6), "qty": (192.6, 224.9)}
    lq = group_lines([_sw("complete", 180, 428, 194.7, h=3.65), _sw("in", 195.7, 428, 198.5, h=3.65),
                      _sw("1", 212.3, 428, 214.3, h=3.65)])[0]
    check("description overflow into the quantity margin", desc_and_qty(lq, rq), ("complete in", "1"))
    lq2 = group_lines([_sw("complete", 180, 428, 194.7, h=3.65),
                       _sw("12", 195.7, 428, 199.5, h=3.65)])[0]
    check("a number in the quantity margin stays the quantity", desc_and_qty(lq2, rq), ("complete", "12"))
    lq3 = group_lines([_sw("furnished", 170, 428, 186.7, h=3.65), _sw("and", 187.5, 428, 192.5, h=3.65),
                       _sw("in1", 193.6, 428, 199.5, h=3.65)])[0]
    check("a word run into the quantity's digits stays where it was", desc_and_qty(lq3, rq),
          ("furnished and", "in1"))

    # 6. names centred over their two columns; a title ending over the first
    blocks = [{"lo": 449.0, "hi": 591.0}, {"lo": 591.0, "hi": 792.0}]
    nl = group_lines([_sw(t, x, 69.6, x1, h=11.0) for t, x, x1 in (
        ("ERS", 475, 492), ("of", 494, 504), ("MS,", 506, 524), ("Inc.", 526, 543),
        ("DAVID-TEHOUNGUE,", 583, 679), ("Ltd.", 682, 699), ("Co.", 702, 716))])[0]
    check("a name is judged by its whole run of words",
          [" ".join(w["text"] for w in ws) for ws in name_words_by_block(nl, blocks)],
          ["ERS of MS, Inc.", "DAVID-TEHOUNGUE, Ltd. Co."])
    tl = group_lines([_sw(t, x, 44.9, x1, h=11.0) for t, x, x1 in (
        ("TIMARRON", 277, 314), ("CREEK", 316, 336), ("AND", 338, 353), ("BB-8", 355, 370),
        ("TRIBUTARY", 372, 408), ("EROSION", 410, 440), ("PROTECTION", 442, 484),
        ("PROJECT", 486, 515))])[0]
    check("a title ending over the first block names nobody",
          name_words_by_block(tl, blocks), [[], []])
    check("caption-only and agenda-stamp fragments",
          [caption_only("Bid Amount"), caption_only("UNIT PRICE (NUMBER ONLY)"),
           caption_only("Iron T Construction, Inc."), bool(ITEM_LABEL_RE.match("Item 12.")),
           bool(ITEM_LABEL_RE.match("Item 12. Ed Bell Construction"))],
          [True, True, False, True, False])

    # 7. a row drawn at two heights (item number 2 pt off its prices)
    rk = {"item": (0.0, 92.0), "desc": (92.0, 192.6), "qty": (192.6, 224.9), "unit": (224.9, 253.0),
          "blocks": [{"lo": 253.0, "hi": 407.5, "price": (253.0, 324.6), "total": (324.6, 407.5),
                      "tx": 336.6}]}
    kl = group_lines([_sw("Erosion", 98, 159.8, 110, h=3.65), _sw("$", 258, 159.8, 262, h=3.65),
                      _sw("13,167.09", 296, 159.8, 323, h=3.65),
                      _sw("13,167.09", 380, 159.8, 407, h=3.65),
                      _sw("102", 82, 161.8, 88, h=3.65), _sw("1", 212, 161.8, 214, h=3.65),
                      _sw("LS", 239, 161.8, 243, h=3.65),
                      _sw("specifications,", 98, 164.1, 121, h=3.65)])
    mk = merge_split_rows(kl, rk, [], None)
    check("an item number drawn off its priced line is merged into it",
          (len(kl), len(mk), spaced(mk[0], rk["item"]), desc_and_qty(mk[0], rk)[1],
           spaced(mk[0], rk["unit"])),
          (3, 2, "102", "1", "LS"))

    # 8. wrapped description lines: centred cells, top-aligned cells, after a
    #    total line, a centred title, a group caption
    def grp(desc):
        return [{"description": desc}, {"description": desc}]

    def ln(top):
        return {"top": float(top), "words": [_sw("x", 0, top, h=11.0)]}

    a, b, c = grp("Remove 13\"-24\" Tree"), grp(""), grp("Seed")
    ev = [("row", ln(238.2), a, 110.0), ("wrap", ln(250.4), "Gravel Fill for Engineered", 110.0),
          ("row", ln(257.6), b, None), ("wrap", ln(264.9), "Pyramattress", 110.0),
          ("row", ln(277.1), c, 110.0)]
    attach_wrapped_lines(ev, [ln(250.4)])
    check("centred cells: the line above an item is its first line",
          [a[1]["description"], b[0]["description"], c[0]["description"]],
          ['Remove 13"-24" Tree', "Gravel Fill for Engineered Pyramattress", "Seed"])
    a, b = grp("Furnish and install"), grp("Grading")
    attach_wrapped_lines([("row", ln(100), a, 40.0), ("wrap", ln(108), "pipe complete", 40.0),
                          ("row", ln(120), b, 40.0)], [ln(100)])
    check("top-aligned cells: a wrapped line stays with the item above",
          [a[0]["description"], b[0]["description"]], ["Furnish and install pipe complete", "Grading"])
    a, b = grp("Furnish"), grp("Grading")
    attach_wrapped_lines([("row", ln(100), a, 40.0), ("wrap", ln(106), "and install", 40.0),
                          ("row", ln(112), b, 40.0)], [ln(100)])
    check("no gap stands out: the item above, as before",
          [a[0]["description"], b[0]["description"]], ["Furnish and install", "Grading"])
    b = grp("Item 20")
    attach_wrapped_lines([("stop", ln(628)),
                          ("wrap", ln(639.4), "Turf Option A: Install Turf in Lieu of", 38.0),
                          ("row", ln(645.3), b, 38.0)], [ln(628)])
    check("after a total line: the item below", b[0]["description"],
          "Turf Option A: Install Turf in Lieu of Item 20")
    b, c = grp("Remove Existing Flood Light"), grp("Mobilization")
    attach_wrapped_lines([("stop", ln(530)), ("wrap", ln(536), "2. Field Lighting Upgrades", 196.0),
                          ("row", ln(542), b, 38.0)], [ln(530)])
    attach_wrapped_lines([("stop", ln(150)), ("wrap", ln(165), "Bid Schedule 1.00:", 110.0),
                          ("row", ln(178), c, 110.0)], [ln(150)])
    check("a centred title or a group caption is nobody's description",
          [b[0]["description"], c[0]["description"]], ["Remove Existing Flood Light", "Mobilization"])

    # 9. rotated text shattered into letters; a page that continues no table
    pieces = [_sw(t, x0, 147.1, x1, h=3.65) for t, x0, x1 in (
        ("C", 98.1, 101.8), ("o", 101.8, 103.8), ("n", 103.8, 106.0), ("d", 106.0, 108.3),
        ("it", 108.3, 110.5), ("ion", 110.5, 115.2), ("S", 116.2, 118.6), ("urvey,", 118.6, 129.4),
        ("1", 212.3, 214.3))]
    check("rotated pieces edge to edge are one word",
          [w["text"] for w in join_touching_words(pieces)], ["Condition", "Survey,", "1"])
    check("a continuation page needs an item number or a quantity",
          [item_rows([{"item_no": "", "quantity_raw": "", "unit_raw": "", "unit_price_raw": "$445,510.00"}]),
           item_rows([{"item_no": "Bernal", "quantity_raw": "$445,510.00", "unit_raw": "",
                       "unit_price_raw": "Sheet"}]),
           item_rows([{"item_no": "plumbing", "quantity_raw": "2022", "unit_raw": "Cost",
                       "unit_price_raw": "forthe"}]),
           item_rows([{"item_no": "#1-1", "quantity_raw": "", "unit_raw": "", "unit_price_raw": "$50,000.00"}]),
           item_rows([{"item_no": "", "quantity_raw": "1,250", "unit_raw": "", "unit_price_raw": "35,000.00$"}]),
           item_rows([{"item_no": "", "quantity_raw": "", "unit_raw": "$ Per SY",
                        "unit_price_raw": "$68.00"}])],
          [False, False, False, True, True, True])
    check("'Sub Total' is a total line", bool(AGG_RE.search("Sub Total")), True)

    # 10. names judged by runs that come out over 60 characters: kept when
    #     short without spaces and the word-by-word set passes; the
    #     word-by-word set when they are long even so (a signature line)
    nb = {"desc": (50.0, 280.0), "blocks": [{"lo": 319.0, "hi": 424.0}, {"lo": 424.0, "hi": 525.0}]}
    vl = group_lines([_sw(t, x0, top, x1) for t, x0, x1, top in (
        ("Vulcan", 316, 339, 60), ("Construction", 341, 383, 60), ("Materials", 385, 417, 60),
        ("Green", 424, 445, 60), ("Dream", 447, 469, 60), ("International", 471, 515, 60),
        ("LLC", 325, 336, 66), ("dba", 338, 350, 66), ("Vulcan", 352, 375, 66),
        ("Materials", 377, 408, 66), ("LLC", 464, 475, 66), ("Company", 351, 382, 72))])
    check("a long name judged by runs is kept",
          (extract_bidder_names(vl, {"top": 100.0}, nb, by_phrase=False)[0],
           page_bidder_names(vl, {"top": 100.0}, nb)),
          ("Construction Materials LLC dba Vulcan Materials Company",
           ["Vulcan Construction Materials LLC dba Vulcan Materials Company",
            "Green Dream International LLC"]))
    sb = {"desc": (50.0, 280.0), "blocks": [{"lo": 319.0, "hi": 424.0}, {"lo": 424.0, "hi": 560.0},
                                           {"lo": 560.0, "hi": 700.0}]}
    sl = group_lines([_sw(t, x0, top, x1) for t, x0, x1, top in (
        ("CHECKED", 500, 530, 54), ("BY:", 532, 542, 54), ("_" * 50, 544, 690, 54),
        ("Acme", 330, 350, 66), ("Paving", 352, 380, 66), ("Beta", 440, 460, 66),
        ("Builders", 462, 500, 66), ("Texas", 580, 600, 66), ("Standard", 602, 640, 66),
        ("Construction", 642, 690, 66))])
    check("names judged by runs too long even without spaces: word by word",
          page_bidder_names(sl, {"top": 100.0}, sb), ["Acme Paving", "Beta Builders",
                                                       "Texas Standard Construction"])

    # 11. a continuation page that reprints the price captions per bidder
    cb = {"blocks": [{"lo": 300.0, "hi": 450.0}, {"lo": 450.0, "hi": 600.0}]}
    cap = group_lines([_sw(t, x, 50) for t, x in (("Qty", 290), ("Unit", 312), ("Price", 332),
                                                    ("Extended", 380), ("Price", 420), ("Qty", 440),
                                                    ("Unit", 462), ("Price", 482), ("Extended", 530))])
    one = group_lines([_sw(t, x, 50) for t, x in (("Unit", 312), ("Price", 332), ("Total", 380))])
    check("price captions reprinted over every block continue the table",
          [repeats_price_captions(cap, cb), repeats_price_captions(one, cb)], [True, False])
    check("a quantity glued to its unit is an item row",
          [item_rows([{"item_no": "", "quantity_raw": "9ACRE", "unit_raw": "",
                       "unit_price_raw": "$50.00"}]),
           item_rows([{"item_no": "", "quantity_raw": "9ACRE", "unit_raw": "",
                       "unit_price_raw": "Hours"}]),
           item_rows([{"item_no": "", "quantity_raw": "", "unit_raw": "150Cubic Yard",
                       "unit_price_raw": "$30.00"}])], [True, False, True])

    # 12. the item number on the line above its prices
    above = ({"top": 300.0}, "9", "CEMENT TREATED (NEW BASE WITH LIQUID")
    check("item number and description from the line above",
          [number_from_line_above(above, {"top": 308.0}, "", "", True, 6.0, 26),
           number_from_line_above(above, {"top": 308.0}, "", "", False, 6.0, 26),
           number_from_line_above(above, {"top": 330.0}, "", "", True, 6.0, 26),
           number_from_line_above(above, {"top": 308.0}, "10", "", True, 6.0, 26),
           number_from_line_above(None, {"top": 308.0}, "", "", True, 6.0, 26),
           number_from_line_above(above, {"top": 308.0}, "", "", True, 6.0, 0)],
          [("9", "CEMENT TREATED (NEW BASE WITH LIQUID"), ("", ""), ("", ""), ("10", ""),
           ("", ""), ("", "")])

    # 13. (round 10) a section banner of any case, standing alone on its line
    check("section banners of any case",
          [bool(SECTION_BANNER_RE.match(t)) for t in (
              "Alternates", "Alternate 1", "Base Bid", "Add Alternate #2", "ALTERNATE NO. 3: LANDSCAPING",
              "Alternate pavement repair at five locations", "Base Bid Total", "1.38 Building",
              "Alternative Energy Services")],
          [True, True, True, True, True, False, False, False, False])

    # 14. (round 10) the item number on the line BELOW its prices (Celina Public Works Renovation 1.38)
    hb = {"price_x": [300.0], "total_x": [360.0], "unit_x": 200.0, "qty_x": 170.0, "desc_x": 40.0,
          "item_x": 6.0}
    rb = build_ranges(hb, 612.0)
    lb = group_lines([_sw("1.37", 6, 100), _sw("Truss", 40, 100), _sw("Building", 64, 100),
                      _sw("1", 172, 100), _sw("LS", 202, 100), _sw("$161,353.21", 300, 100),
                      _sw("$161,353.21", 360, 100),
                      _sw("Reinforced", 40, 110), _sw("Slab", 82, 110),
                      _sw("1", 172, 116), _sw("LS", 202, 116), _sw("$125,000.00", 300, 116),
                      _sw("$125,000.00", 360, 116),
                      _sw("1.38", 6, 122), _sw("Building", 40, 122),
                      _sw("1.39", 6, 132), _sw("Bin", 40, 132), _sw("Blocks", 56, 132),
                      _sw("1", 172, 132), _sw("LS", 202, 132), _sw("$54,928.75", 300, 132),
                      _sw("$54,928.75", 360, 132)])
    ib = [i for i, l in enumerate(lb) if abs(l["top"] - 116) < 1][0]
    check("item number from the line below its prices",
          [number_from_line_below(lb, ib, rb, True, 8.0, 3),
           number_from_line_below(lb, ib, rb, True, 8.0, 0),
           number_from_line_below(lb, ib, rb, False, 8.0, 3)],
          [("1.38", "Building"), None, None])
    # ... never when the line after the number is a priced line wanting it (number_from_line_above)
    lb2 = group_lines([_sw("1", 172, 116), _sw("LS", 202, 116), _sw("$9.00", 300, 116),
                       _sw("$9.00", 360, 116), _sw("7", 6, 122), _sw("Fence", 40, 122),
                       _sw("10", 172, 128), _sw("LF", 202, 128), _sw("$5.00", 300, 128),
                       _sw("$50.00", 360, 128)])
    check("a number line the next priced line wants stays with it",
          number_from_line_below(lb2, 0, rb, True, 8.0, 3), None)

    # 15. (round 10) the description column right of the quantity and unit columns (Celina Ownsby
    #     Parkway): 'Pkg Line Type | Qty | UoM | Description | Note to Bidder | Unit Price | Extended Price'
    ho = find_header(group_lines([_sw(t, x, 76, x1) for t, x, x1 in (
        ("Pkg", 41, 53), ("Line", 55, 69), ("Type", 71, 87), ("Qty", 112, 124), ("UoM", 156, 172),
        ("Description", 242, 280), ("Note", 409, 426), ("to", 428, 435), ("Bidder", 437, 459),
        ("Unit", 546, 560), ("Price", 562, 580), ("Extended", 603, 633), ("Price", 636, 654))])[0])
    ro = build_ranges(ho, 700.0)
    lo = group_lines([_sw(t, x, 117, x1) for t, x, x1 in (
        ("1", 16, 20), ("Con1", 56, 72), ("18", 113, 121), ("MO", 158, 170),
        ("BARRICADES,", 205, 248), ("SIGNS", 250, 271), ("REFER", 345, 366), ("TO", 368, 377),
        ("CITY", 379, 393), ("$10,250.00", 546, 590), ("$184,500.00", 603, 650))])
    refine_reordered(lo, 76.0, ro)
    check("description right of quantity and unit",
          (ro.get("reordered"), spaced(lo[0], ro["item"]), desc_and_qty(lo[0], ro),
           spaced(lo[0], ro["unit"]), [w["text"] for w in desc_words(lo[0], ro)]),
          (True, "1", ("BARRICADES, SIGNS", "18"), "MO", ["BARRICADES,", "SIGNS"]))
    check("the usual order keeps the usual ranges",
          build_ranges(hb, 612.0).get("reordered"), None)

    # 16a. (round 11) a numbered group heading that is a section banner
    check("numbered group headings", [numbered_banner(*a) for a in (
        ("11", "ALTERNATES", "", ""), ("9", "Alternates", "", ""), ("1", "MOBILIZATION", "", ""),
        ("11", "ALTERNATES", "1", "LS"), ("#11-1", "ALTERNATES", "", ""), ("2", "Alternate 1: Bowser Road", "", ""),
        ("12", "Alternate items are priced on sheet 3", "", ""))],
          ["ALTERNATES", "ALTERNATES", None, None, None, "ALTERNATE 1: BOWSER ROAD", None])

    # 16. (round 11) 'U/M' and a bare 'MEASURE' name the unit column; 'UNIT OF MEASURE' still at its UNIT
    def hdr(words):
        return find_header(group_lines([_sw(t, x, 60, x1) for t, x, x1 in words])[0])
    hu = hdr((("ITEM", 10, 30), ("DESCRIPTION", 60, 120), ("U/M", 300, 315), ("QTY", 330, 345),
              ("UNIT", 370, 385), ("PRICE", 387, 410), ("TOTAL", 420, 445), ("PRICE", 447, 470)))
    hm = hdr((("Item", 10, 30), ("Description", 60, 120), ("Quantity", 300, 330), ("Measure", 340, 370),
              ("Unit", 390, 405), ("Cost", 407, 425), ("Total", 450, 470)))
    hn = hdr((("Item", 10, 30), ("Description", 60, 120), ("Quantity", 300, 330), ("Unit", 335, 350),
              ("of", 352, 360), ("Measure", 362, 392), ("Unit", 400, 415), ("Cost", 417, 435),
              ("Total", 450, 470)))
    hx = hdr((("Item", 10, 30), ("Description", 60, 120), ("Quantity", 300, 330),
              ("Unit", 390, 405), ("Cost", 407, 425), ("Total", 450, 470)))
    check("U/M and MEASURE are unit captions",
          [hu["unit_x"], hm["unit_x"], hn["unit_x"], hx["unit_x"]], [300.0, 340.0, 335.0, None])

    # 17. (round 11) a turned page: the few UPRIGHT words (an agenda packet's page stamp) are no part of
    #     its table; a page whose upright words are many keeps them all
    class _Page(object):
        def __init__(self, ws):
            self.ws, self.width, self.height = ws, 792.0, 1224.0

        def extract_words(self, **kw):
            return [dict(w) for w in self.ws]
    turned = [dict(_sw("x%d" % k, 40 + 12 * (k % 10), 30 + 20 * (k // 10), 50 + 12 * (k % 10)), upright=False)
              for k in range(60)]
    stamp = [dict(_sw(t, x, 1209, x + 20), upright=True) for t, x in (("Packet", 713), ("Pg.", 748), ("415", 766))]
    got = [w["text"] for w in extract_words_robust(_Page(turned + stamp))]
    check("upright stamp words on a turned page are dropped",
          (len(got), any(t in got for t in ("tekcaP", ".gP", "514", "Packet", "415"))), (60, False))
    many = [dict(_sw("u%d" % k, 40 + 12 * k, 600, 50 + 12 * k), upright=True) for k in range(20)]
    got2 = extract_words_robust(_Page(turned + many))
    check("a turned page with many upright words keeps them", len(got2), 80)
    return out
