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
    r"bid\s*date|prepared\s*by|certified", re.I)

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
AGG_RE = re.compile(r"\bsub-?totals?\b|^(total|grand|sum)\b|\btotal\s+(bid|base|cost|amount)\b",
                    re.I)
# Column captions used instead of names: "BIDDER 1", "CONTRACTOR 2", "BID 3".
BIDDER_LABEL_RE = re.compile(r"^(bidder|contractor|vendor|bid|respondent)\s*#?\s*\d+$", re.I)
MONEYISH = re.compile(r"^[\$\(]?[\d,]*\.?\d*[\)]?$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
CITYLINE_RE = re.compile(r"^[A-Z][A-Za-z.\- ]+,$")


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
        for k in sorted(rows, reverse=True):
            for w in sorted(rows[k], key=lambda w: -w["top"]):
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
            return fixed
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
        return None

    return {
        "top": line["top"],
        "item_x": first_x("ITEM", "ITEM NO.", "NO.", "NO"),
        "desc_x": desc_x(),
        "qty_x": first_x("QTY", "QTY.", "QUANTITY"),
        "unit_x": uom_x(),
        "price_x": unit_price_x,
        "total_x": total_x[:len(unit_price_x)],
    }


def build_ranges(hdr, page_width):
    """Turn header anchors into [lo, hi) x ranges for every field."""
    px, tx = hdr["price_x"], hdr["total_x"]
    n = len(px)
    blocks = []
    for k in range(n):
        lo = px[k] - MARGIN
        hi = (px[k + 1] - MARGIN) if k + 1 < n else page_width
        mid = tx[k] - MARGIN
        blocks.append({"lo": lo, "hi": hi, "price": (lo, mid), "total": (mid, hi)})
    left = px[0] - MARGIN
    unit_lo = (hdr["unit_x"] - MARGIN) if hdr["unit_x"] else left
    qty_lo = (hdr["qty_x"] - MARGIN) if hdr["qty_x"] else unit_lo
    desc_lo = (hdr["desc_x"] - MARGIN) if hdr["desc_x"] else 0.0
    return {
        "item": (0.0, desc_lo),
        "desc": (desc_lo, qty_lo),
        "qty": (qty_lo, unit_lo),
        "unit": (unit_lo, left),
        "blocks": blocks,
        "left_edge": left,
    }


def words_in(line, rng):
    lo, hi = rng
    return [w for w in line["words"] if lo <= w["x0"] < hi]


def joined(line, rng):
    """Join a column's words with NO separator: a single value is often split
    into several words ("$", "6", "0,000.00"), and only concatenation restores it."""
    return "".join(w["text"] for w in words_in(line, rng)).strip()


def spaced(line, rng):
    return " ".join(w["text"] for w in words_in(line, rng)).strip()


def extract_bidder_names(lines, hdr, ranges):
    """Contractor names sit above the header, centred over their whole block and
    often wrapped across two lines. Lines that also carry something in the left
    label margin are page furniture (BID OPENED / PROJECT No. / city, ST) and
    are skipped."""
    names = [""] * len(ranges["blocks"])
    left_label = ranges["desc"][0]
    cand = []
    for l in lines:
        if l["top"] >= hdr["top"] - 1 or not l["words"]:
            continue
        cand.append(l)

    # A page title spans the full width, so its words also land inside the name
    # band. Flag those lines rather than discarding them: discarding cost 15
    # files their names outright, because some templates do put the name band on
    # the same visual line as a left-margin label.
    spans_left = {id(l): (min(w["x0"] for w in l["words"]) < left_label) for l in cand}

    for k, b in enumerate(ranges["blocks"]):
        parts = []
        for l in cand:
            toks = [w["text"] for w in l["words"] if b["lo"] <= w["x0"] < b["hi"]]
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
            if len(frag) <= 2 and not frag.isalpha():
                continue
            # a banner running across the page ("ARLINGTON CITY LIMITS ...")
            if spans_left.get(id(l)) and frag.upper() == frag and len(frag) > 12:
                continue
            parts.append((l["top"], frag))
        parts.sort()
        nm = re.sub(r"\s+", " ", " ".join(p[1] for p in parts)).strip(" ,.")
        # a trailing city/state can still ride along on the same visual line
        nm = re.sub(r",?\s*[A-Z][A-Za-z.\-' ]*,\s*(%s)\.?(\s+\d{5})?$" % STATES, "", nm).strip(" ,.")
        names[k] = nm
    return names


def looks_like_name_set(names):
    """Accept a page's names only if they actually look like company names --
    otherwise a malformed page would overwrite good names carried from page 1."""
    good = [n for n in names if n]
    if not good:
        return False
    return all(len(n) <= 60 and not NOT_A_NAME_RE.search(n) for n in good)


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
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            try:
                words = extract_words_robust(page)
            except Exception:
                continue
            lines = group_lines(words)
            headers = [(i, find_header(l)) for i, l in enumerate(lines)]
            headers = [(i, h) for i, h in headers if h]
            if headers:
                hdr0 = headers[0][1]
                ranges = build_ranges(hdr0, float(page.width))
                prev_ranges = ranges
                header_tops = [h["top"] for _, h in headers]
                top_limit = hdr0["top"]
                page_names = extract_bidder_names(lines, hdr0, ranges)
                if looks_like_name_set(page_names):
                    names = page_names
            elif prev_ranges is not None:
                # A continuation page: the table carries on with the same column
                # geometry but the caption row is not reprinted.  read_pdf has
                # inherited headers across pages for exactly this reason; this
                # parser did not, so continuation pages were dropped whole --
                # collin 58471 lost items 13-37 (the 42"-120" culvert price
                # gradient, i.e. the large-diameter end of the curve) and
                # mesquite 2024-013 lost pages 2-4, 105 priced items down to 33.
                ranges = prev_ranges
                header_tops = []
                top_limit = None
            else:
                continue

            # Never skip a page because the names could not be read: the prices
            # are still correct and the name can be repaired separately. Dropping
            # the rows would silently lose whole files.
            cur = list(names) if names else []
            cur = (cur + [""] * len(ranges["blocks"]))[:len(ranges["blocks"])]

            last = None
            for l in lines:
                if header_tops and any(abs(l["top"] - ht) <= LINE_TOL
                                       for ht in header_tops):
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
                desc = spaced(l, ranges["desc"])
                qty_s = joined(l, ranges["qty"])
                unit_s = spaced(l, ranges["unit"])
                money = [(joined(l, b["price"]), joined(l, b["total"]))
                         for b in ranges["blocks"]]
                has_money = any(p for p, _ in money)

                if AGG_RE.search(item_no) or AGG_RE.search(desc):
                    continue
                if not has_money:
                    if above_header:
                        # Title block / bidder-name band, not a banner.
                        continue
                    # banner (section title) or a wrapped description line
                    txt_left = (item_no + " " + desc).strip()
                    if not txt_left:
                        continue
                    if ITEM_RE.match(item_no):
                        continue
                    letters = re.sub(r"[^A-Za-z]", "", txt_left)
                    if (letters and letters.upper() == letters and len(txt_left) > 4
                            and not qty_s):
                        section = txt_left[:60].upper()
                        last = None
                    elif last and desc:
                        # a wrapped description line belongs to EVERY bidder row
                        # of the item above it, not just the first
                        for g in last:
                            g["description"] = (g["description"] + " " + desc).strip()
                    continue
                if not ITEM_RE.match(item_no) and not desc:
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
                recs.extend(group)
                last = group or None
    return recs
