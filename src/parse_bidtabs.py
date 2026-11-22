"""
Bid tabulation ETL.

Reads the heterogeneous bid-tab workbooks/PDFs in data/ and emits one tidy
long-format table: ONE ROW = one bidder's unit price for one line item.

Design notes
------------
* Layouts differ per city, so columns are DETECTED, not hard-coded. A price
  column is found as an adjacent header pair
  (unit price|unit cost|unit) -> (total|extended|...).
  This disambiguates the bid-price "Unit" column from the UOM "Unit" column,
  because UOM is never followed by a total column.
* The engineer's estimate (OPCC) is kept as just another "bidder" with
  is_engineer_estimate=True, so it can be compared like-for-like.
* Nothing is silently dropped. Every exclusion is counted and reported.
"""
import os, re, sys, csv, json, unicodedata
from collections import Counter, defaultdict

import openpyxl
import pdfplumber

import dedup
import local_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "clean")
REPORTS = os.path.join(ROOT, "reports")

# ---------------------------------------------------------------- normalizers

UNIT_MAP = {
    "ls": "LS", "l.s.": "LS", "lump-sum": "LS", "lump sum": "LS", "lumpsum": "LS",
    "lf": "LF", "linear foot/feet": "LF", "linear foot": "LF", "linear feet": "LF", "l.f.": "LF",
    "sy": "SY", "square yard": "SY", "square yards": "SY", "s.y.": "SY",
    "sf": "SF", "square foot": "SF", "square feet": "SF", "s.f.": "SF",
    "cy": "CY", "cubic yard": "CY", "cubic yards": "CY", "c.y.": "CY",
    "cf": "CF", "cubic foot": "CF", "cubic feet": "CF",
    "ea": "EA", "each": "EA",
    "mo": "MO", "month": "MO", "months": "MO", "mos": "MO", "mon": "MO",
    "day": "DAY", "days": "DAY", "hr": "HR", "hour": "HR", "hours": "HR",
    "ton": "TON", "tons": "TON", "tn": "TON",
    "lb": "LB", "lbs": "LB", "gal": "GAL", "ac": "AC", "acre": "AC", "acres": "AC",
    "sta": "STA", "station": "STA", "allow": "ALLOW", "allowance": "ALLOW",
    # 'B.A.' = bid allowance (Rockwall Lake Rockwall Estates prints five owner
    # allowances, $270,000 a bidder, in that unit; read as an unknown unit they
    # were unit_suspect and left every total) -- dotted form only, a bare 'BA'
    # is not claimed
    "b.a": "ALLOW", "bid allowance": "ALLOW",
    "lft": "LF", "sft": "SF", "cyd": "CY", "syd": "SY", "cft": "CF",
    "l.f": "LF", "s.y": "SY", "c.y": "CY", "sq yd": "SY", "cu yd": "CY",
    "al": "ALLOW", "alw": "ALLOW", "lot": "LOT",
    "cycles": "CYC", "cycle": "CYC", "cyc": "CYC",
    "ft": "LF", "feet": "LF", "foot": "LF",
    # Goods-procurement units. Mapped so they are recognised rather than looking
    # like a broken column, but the projects they belong to are filtered out as
    # non-construction anyway.
    "pair": "PAIR", "case": "CASE", "box": "BOX", "ream": "REAM",
    "gallon": "GAL", "gallons": "GAL", "thousand": "MIL", "dozen": "DOZ",
    "al ": "ALLOW",                # "AL" = allowance (sprinkler adjustments)
    "hd": "HD", "head": "HD",      # priced "per head" (sprinkler heads)
    "vf": "VF",                    # vertical foot (extra manhole depth)
    # THOUSAND GALLONS -- TxDOT item 168 VEGETATIVE WATERING is paid by the MG
    # (1,000 gal); owners print it as MG, TGL, or spelled out with the code in
    # parentheses ('THOUSAND GALLONS (TGL)', see norm_unit).  None of the three
    # was in this map, so every such row came back unit_suspect='Y' and the
    # panel filter dropped it FOR EVERY BIDDER: Collin County IFB 2023-303
    # lost item 168 ($1,215,119 for the low bidder and the estimate alike),
    # Lewisville 25-98-C, Farmers Branch 25-17 and 26-20 lost theirs too.
    # Measured on line_items.csv: 31 rows / 7 files carry MG, TGL or the
    # spelled-out form (8 of them CCOL 3B's 'MG 29,976', the quantity glued
    # on), every one of them vegetative watering; no other unit moves.
    # No key starts with a number: '1000 GAL' in a unit cell is as likely a
    # quantity glued to GAL (main()'s glued-quantity pass) as a unit, and
    # neither form occurs in the corpus.
    "mg": "MG", "tgl": "MG", "m gal": "MG", "mgal": "MG", "kgal": "MG",
    "thousand gallons": "MG", "thousand gallon": "MG",
    # 2026-11-19 (parser round 3): units the corpus prints that were not keys,
    # so every such row was unit_suspect and left the panel FOR EVERY BIDDER
    # (runs/before: 1,511 unit_suspect rows).  Only spellings whose meaning is
    # unambiguous: the Mesquite tabs' flashing-arrow-board item (10 weeks,
    # 'Week' / 'WEEK' / 'Wkly'; nine lettings' winners each lost $5,000-
    # $12,000), Richardson 35-25's 'PER LINEAR' (a cell too short for 'PER
    # LINEAR FOOT': -$346,579 on the low bid), 'SQ FT' / 'Sq. Ft.' / 'Sq.
    # Feet', 'SQ Yard', 'Hourly', 'VLF' (vertical linear foot), 'MI' (mile),
    # 'pound', 'LSUM', 'SFF' (square foot of wall face: stone veneer, form
    # liner, brick veneer in 8 files), and Richardson's 'ALL' (allowance: in
    # every one of its 36 rows a contingency or allowance amount, quantity 1;
    # Bid 05-24's low bid lost its $10,000 irrigation allowance).  A bare
    # 'YD' / 'YARD' (CY or SY?) or 'SQ' (a roofing square?) is NOT a key --
    # unit_from_description resolves it only where the row's own words say.
    "linear": "LF", "lin ft": "LF", "lin feet": "LF",
    "sq ft": "SF", "sq feet": "SF", "sq foot": "SF", "sqft": "SF", "sff": "SF",
    "sq yard": "SY", "sq yards": "SY", "sq yds": "SY", "squard yard": "SY",
    "week": "WK", "weeks": "WK", "wk": "WK", "wks": "WK", "wkly": "WK", "weekly": "WK",
    "hourly": "HR", "hrs": "HR", "daily": "DAY", "monthly": "MO",
    "vlf": "VF",
    "mi": "MI", "mile": "MI", "miles": "MI",
    "pound": "LB", "pounds": "LB", "lsum": "LS", "all": "ALLOW",
    # goods-procurement units, recognised like the ones above (pair, case, box)
    "bottle": "BTL", "bottles": "BTL", "pack": "PKG", "pkg": "PKG",
}

# Rows whose ITEM NO (not description) marks them as an aggregate, e.g.
# "Total Base Bid", "SUBTOTAL ALT #1". These carry a project total in the price
# column and must never enter a unit-price table.
AGG_ITEM_RE = re.compile(r"^\s*(total|subtotal|sub-total|sum|grand)\b", re.I)

# ---- an aggregate whose caption does not START with the total word --------
# AGG_ITEM_RE only sees a caption that BEGINS with it.  Rowlett prints
# 'BASE BID TOTAL' and 'ALTERNATE BID TOTAL' in the item column, Farmers Branch
# 'BRINCREST WATER MAIN REPLACEMENT TOTAL:', Mansfield 'PAVING SUBTOTAL',
# McKinney 'STREET REPAIRS: Totals', Grand Prairie 'PROJECT TOTAL BASE BID'.
# Each was kept as a line item whose "unit price" is the section total, which
# DOUBLES the section for every bidder it is kept for (Rowlett 2021-09's
# estimate read $1,336,866 against a printed $668,433).
# A total word alone is not enough -- 'Total Labor Cost Incorporated in
# Project', 'Mobilization (not to exceed 5% of the Grand Total Bid Amount)' are
# real pay items -- so an aggregate must ALSO be unmeasured: no quantity and
# no recognisable unit (a sum is not measured in anything), and its caption
# must not open with an item number ('1.2 Total Labor Cost ...').
#
# ... and the caption must BE a total caption, not merely contain the word.
# 'total' anywhere dropped real pay items whose tab has no unit or quantity
# column to protect them: 'Erosion control (total for all phases)', Collin
# 2023-138's mowing line '... approximately (212) total acres ...', Irving's
# 'Contractor Mobilization, not to exceed 3% of the total contract amount',
# Collin 2023-400's 'State total Cost for Project Management'.  A total
# caption OPENS with the total phrase ('Total Base Bid', 'TOTALS:',
# 'Subtotal', 'Grand Total', 'Base Bid Total', 'PROJECT TOTAL BASE BID',
# 'Category 1 Total ...') or CLOSES with it ('PAVING SUBTOTAL', 'BRINCREST
# WATER MAIN REPLACEMENT TOTAL:', 'Section A Total:', '... GRAND-TOTAL AMOUNT
# BASE BID (E21, E20 & E8):'), judged on the whole caption and on each of its
# colon-separated parts ('STREET REPAIRS: Totals', "Contractor's Submitted
# Bid Totals: April 14, 2023"); a colon-terminated label naming a total is
# one too ('NOT TO EXCEED TOTAL BASE BID AMOUNT:').
# The item-number test now also knows '#0-44' (Lewisville), 'COFB 01'
# (Farmers Branch), '(12)', 'Item 5', 'No. 5' -- but not a SECTION word with a
# number: 'UNIT 1 TOTAL', 'SITE 3 TOTAL', 'ALT 1 TOTAL' are totals, and read
# as numbered pay items they would double their section.
TOTAL_WORD_RE = re.compile(r"\b(?:sub-?)?totals?\b", re.I)
LEADING_ITEM_NO_RE = re.compile(
    r"^\s*(?:(?:item|no\.?)\s*#?\s*"
    r"|(?!(?:unit|site|part|alt|lot|area|zone|base|bid|add|ded|opt|sch|sec|sect|"
    r"grp|div|seg|ph|phs|cat|loc|blk)\s)[A-Za-z]{1,4}[-.\s]?)?"
    r"#?\s*\(?\d+(?:[.\-]\d+)*[A-Za-z]?[.):]?(?:\s|$)", re.I)
_TOTAL_PHRASE = r"(?:sub[\s.-]*|grand[\s.-]*)?totals?"
_TOTAL_OPEN_RE = re.compile(
    r"^\W*(?:(?:category|section|schedule|group|part|phase|unit|site|lot)\s*#?\s*"
    r"[\w.-]{1,4}\W+)?"
    r"(?:(?:base|alternates?|alt|additive|deductive|bid|project|contract|grand|sub)"
    r"\b\W*){0,3}" + _TOTAL_PHRASE + r"\b", re.I)
_TOTAL_CLOSE_RE = re.compile(
    r"\b" + _TOTAL_PHRASE + r"(?:\W+(?:amount|base|bids?|alternates?|alt|cost|"
    r"price|sum|project|contract|value))*\W*$", re.I)


def is_total_caption(lab):
    """'Total Base Bid', 'PAVING SUBTOTAL', 'STREET REPAIRS: Totals' -> True;
    'Erosion control (total for all phases)' -> False."""
    s = txt(lab)
    if not s or not TOTAL_WORD_RE.search(s):
        return False
    if re.search(r":\s*$", s) and TOTAL_WORD_RE.search(re.sub(r"\([^()]*\)", " ", s)):
        return True                      # a label naming a total, ending in ':'
    # a trailing parenthetical qualifies the caption and is not part of it:
    # 'BID GRAND TOTAL (BASE BID+ ALTERNATE 1):', 'Mobilization (not to
    # exceed 5% of the Grand Total Bid Amount)'
    core = re.sub(r"(?:\s*\([^()]*\))+\W*$", "", s)
    for seg in [core] + core.split(":"):
        if seg.strip() and (_TOTAL_OPEN_RE.match(seg) or _TOTAL_CLOSE_RE.search(seg)):
            return True
    return False


def is_total_label_row(item_no, desc, unit_raw, qty):
    if qty is not None or norm_unit(unit_raw) in KNOWN_UNITS:
        return False
    if LEADING_ITEM_NO_RE.match(item_no or ""):
        return False   # a numbered row is a pay item, whatever its text mentions
    for lab in (item_no, desc):
        if lab and is_total_caption(lab) and not LEADING_ITEM_NO_RE.match(lab):
            return True
    return False

# Several tabs encode the bid section in the item number: "ALT 1.1", "I.1".
ALT_ITEM_RE = re.compile(r"^\s*(?:alt|alternate)\.?\s*#?\s*([0-9ivx]+)", re.I)
# An item whose WORDS say it is an alternate, with no number to open a section
# by (GLUED_ALT_RE needs one): Addison's Surveyor tab lists 'A  Additive
# Alternative: Driveway with Porous Pavers - Adds to Bid Item 1' and 'D
# Deductive Alternative: Gravel Driveway - Replaces Bid Item 1' under the base
# items, and both went into the base bid.  That row alone is the alternate.
ROW_ALT_RE = re.compile(r"^\s*(?:additive|deductive|add|deduct)\s+alternat(?:e|ive)s?\b", re.I)

# bidder aliases -> canonical firm name (same firm spelled differently per city)
BIDDER_ALIASES = {
    "kik underground": "KIK Underground",
    "kik undergroundcom tom koble": "KIK Underground",
    "kik underground com tom koble": "KIK Underground",
    "kik": "KIK Underground",
    "jeske construction": "Jeske Construction",
    "jeske": "Jeske Construction",
    "estrada concrete": "Estrada Concrete",
    "estrada": "Estrada Concrete",
    "tri con services": "Tri-Con Services",
    "tri con": "Tri-Con Services",
    "syb construction": "SYB Construction",
    "syb": "SYB Construction",
    "tiseo paving": "Tiseo Paving",
    "tiseo": "Tiseo Paving",
    "axis contracting": "Axis Contracting",
    "axis": "Axis Contracting",
    "garret shields infrastructure": "Garret Shields Infrastructure",
    "garret shields": "Garret Shields Infrastructure",
    "a d paving": "A&D Paving",
    # The Garland "Bid Tab" header abbreviates names the Summary tab spells out.
    "aushill": "Aushill Construction",
    "camino": "Camino Construction",
    "hernandez": "Hernandez Underground Contracting",
    "iron t": "Iron T Construction",
    "vlex": "Vlex Construction",
    "tri con services": "Tri-Con Services",
    "3d paving contracting": "3D Paving & Contracting",
    "3d paving": "3D Paving & Contracting",
    "stoic civil construction": "Stoic Civil Construction",
    "stoic": "Stoic Civil Construction",
    "austin filter systems": "Austin Filter Systems",
    "vescorp construction": "Vescorp Construction",
    "vescorp": "Vescorp Construction",
    "reliable paving": "Reliable Paving",
    "ddm construction": "DDM Construction",
    "mcmahon contracting": "McMahon Contracting",
    "flow line construction": "Flow-Line Construction",
    "flowline construction": "Flow-Line Construction",
    "western municipal construction of texas": "Western Municipal Construction of TX",
    "willco underground": "WillCo Underground",
    "humphrey morton construction": "Humphrey & Morton Construction",
    "texas standard construction": "Texas Standard Construction",
    "quality excavation": "Quality Excavation",
    "xit paving construction": "XIT Paving & Construction",
    "grod construction": "GRod Construction",
    "jb": "J.B. & Co.",
    "ark contracting services": "Ark Contracting Services",
    "aushill construction": "Aushill Construction",
    "iron t construction": "Iron T Construction",
    "hernandez underground contracting": "Hernandez Underground Contracting",
    "vlex construction": "Vlex Construction",
    "jl construction": "J&L Construction",
    "camino construction": "Camino Construction",
    "ccgmg series b lexus": "CCGMG LLC Series B",
    "ccgmg": "CCGMG LLC Series B",
    # One tab's header row types this firm as "R&L"; its summary sheet names
    # it "J&L Construction, LLC." and the two totals agree exactly, so "R&L"
    # is a typo in the source.
    "r l": "J&L Construction",
    "treg erosion control specialists": "TREG Erosion Control Specialists",
    "bernal commercial construction": "Bernal Commercial Construction",
    "central north": "Central North",
    "c greenscapeing": "C. Greenscaping",
    "northrock": "Northrock",
    "nouveau": "Nouveau",
    "hqs": "HQS",
    "cole construction": "Cole Construction",
    "american landscape systems": "American Landscape Systems",
}

SUFFIX_RX = (r"\b(llc|l\.l\.c\.|inc|incorporated|lp|l\.p\.|ltd|co|company|"
             r"corporation|corp|series\s+b|the)\b")

# Extra labels that mark an engineer's-estimate column, from the private local
# configuration (src/local_config.py, key "engineer_label_patterns"); a public
# checkout has none.
_ENGINEER_LABEL_EXTRA = "".join(
    p + "|" for p in local_config.get("engineer_label_patterns", []))

ENGINEER_RE = re.compile(
    _ENGINEER_LABEL_EXTRA +
    # `\be?opcc\b`: Irving CIP tabs label the engineer column "EOPCC" (Engineer's
    # Opinion of Probable Construction Cost), e.g. external/irving 096D-20F
    # Oakdale-Rindie, where `\bopcc\b` cannot match because no word boundary sits
    # between the E and the O.  Measured on line_items.csv: of 1,393 distinct
    # bidder_raw values the `e?` flips 0 flags -- it only adds "EOPCC".
    r"\be?opcc\b|engineer.?s?\s*(opcc|estimate|opinion)|"
    r"engineers?\s*estimate|opinion\s+of\s+probable|enigneer|"
    # Consulting engineers whose ESTIMATE columns some tabs print under the
    # firm's own name with no "estimate" wording.  Found by the v2 model audit,
    # 2026-11-19: Grand Prairie 2024-11-26 Phase 3 Water Line ("Freese and
    # Nichols, Inc. ... Jane Doe, P.E.", prices = round numbers x 1.18)
    # and Coppell 2020-08-03 Airline ("Kimley-Horn and Associates, Inc.", 68
    # rows of round prices).  A design consultant does not bid construction,
    # and a P.E. seal in a bidder cell marks the engineer, so either means the
    # column is an estimate.  Measured over all 1,393 distinct bidder_raw values
    # in line_items.csv: these terms match exactly those two names and nothing
    # else.  (?<![.\w]) keeps "C.P.E. Construction" from matching.
    r"freese\s*(?:and|&)\s*nichols|kimley[\s-]*horn|halff\s+associates|"
    r"teague\s+nall|pacheco\s+koch|lockwood,?\s+andrews|huitt[\s-]*zollars|"
    r"binkley\s*(?:and|&)\s*barfield|dunaway\s+associates|pape[\s-]*dawson|"
    # The seal marks the engineer only in a cell that is not ALSO labelled as a
    # bidder's: Balch Springs Elam Rd's only bidder column read 'n, P.E.
    # 4/1/2025 BIDDER 1' (an overprinted signature line ran into its caption)
    # and went out as an estimate (2026-11-19 independent check).
    r"^(?![\s\S]*\b(?:bidder|contractor|vendor|proposer)s?\b)[\s\S]*?(?<![.\w])p\.\s?e\.(?=\s|,|$)|"
    # 2026-11-19 Mac verification (handoff/scratchpad/parser_verify): estimate
    # columns the words above missed, each read as a BIDDER in the rebuild --
    #   * a bare caption: Frisco 2510-007 Dallas Pkwy Ph 5 heads its estimate
    #     'Engineer' (200 rows, n_bidders 5 instead of 4); 'Estimate', 'OPC';
    #   * 'EOPC' (Engineer's Opinion of Probable Cost, Rowlett 2021-17) and an
    #     estimate whose label the text layer garbled ("Engineer's EstimRaotaed
    #     way", Cedar Hill 2022-02): the engineer's word followed by any
    #     estimate word, matched on its stem;
    #   * an estimate printed under the consultant's name only: Frisco
    #     2603-054 heads 'Engineer OPCC' over 'Westwood Professional Services'
    #     (169 rows).  Firms here design and estimate; they do not bid work.
    #     Firms named 'Consulting'/'Consultants' DO bid (Tellus Consulting won
    #     Colleyville's Preston Manor; Westra Consultants bid NRH 24-021), so
    #     no generic word is added -- only named design firms.
    r"^\W*(?:the\s+)?(?:engineer|engineers|engineer's|engineers'|engineering|estimate|"
    r"estimated\s+cost|eng\.?\s*est\.?|opc|e?opcc|eopc)\W*$|"
    r"\beopc\b|\bengineer'?s?'?\s*(?:e?opcc?|estim\w*|est\.(?:\s|$))|"
    r"\b(?:engineering|design|consultant'?s?|city'?s?|owner'?s?)\s+(?:estimate|est\.)(?=\W|$)|"
    r"westwood\s+professional|\bgarver\b|\blja\s+engineering|neel[\s-]*schaffer|\bstantec\b|"
    r"\baecom\b|jacobs\s+engineering|\bhdr\s+engineering|burns\s*(?:&|and)\s*mc\s*donnell|"
    r"\bparkhill\b|cobb[\s,]*fendley|\bkleinfelder\b|gorrondona|graham\s+associates|"
    r"birkhoff,?\s+hendricks|\bbge,?\s+inc|\bcp\s*&\s*y\b|wier\s*(?:&|and)\s*associates|"
    r"baird,?\s+hampton|lina\s+t\.?\s+ramey|peloton\s+land|plummer\s+associates|"
    r"\bcarollo\s+engineers|\bdannenbaum\b|gresham\s+smith|bowman\s+consulting|"
    r"shield\s+engineering|spiars\s+engineering|lee\s+engineering|criado\s*(?:&|and)\s*assoc|"
    r"pate\s+engineers|arredondo,?\s+zepeda|"
    # (round 10, .31) the engineer's caption MISSPELT: Kennedale's Briar Court tab heads its
    # estimate 'ENGINEER OPION OF COST' (771,110), which matched nothing above and went out as a
    # bidder -- the second-lowest 'bid'.  The engineer's word (any spelling: Enginer, Engneer) followed
    # by an 'o...' word and 'of (probable) (construction) cost'; an 'op...n of ... cost' opinion on
    # its own; and the engineer's 'probable / estimated cost'.  Measured over the 2026-11-21 corpus'
    # distinct bidder_raw values: these flip no name that bids.
    r"\beng\w{0,6}r'?s?'?\s+o\w{1,7}\s+of\s+(?:probable\s+)?(?:construction\s+|project\s+)?costs?\b|"
    r"\bop\w{0,6}n\s+of\s+(?:probable\s+)?(?:construction\s+|project\s+)?costs?\b|"
    r"\beng\w{0,6}r'?s?'?\s+(?:probable|estimated)\s+(?:construction\s+|project\s+)?costs?\b",
    re.I,
)
# The number is OPTIONAL: one Keller tab labels its columns `BIDDERS`,
# plural and unnumbered, and a pattern requiring digits treats the bare
# header word as a company name.  A cell holding only this word is a
# column label whether or not it was numbered.
PLACEHOLDER_RE = re.compile(r"^\s*(contractor|bidder|vendor)s?\s*#?\s*\d*\s*$", re.I)

# A computed column, not a bidder: must never enter the price table.
# A column the TAB computed from the other columns.  It is not a bid, and
# admitting one does a specific kind of damage: a synthetic observation sitting
# at the mean of the real bids leaves the median about where it was and pulls
# the spread IN, so the item reads as better agreed than the bidders agreed --
# the same damage as a duplicated letting, from a different cause.  It also
# adds one to n_bidders, which feeds the competition discount.
#
# The pattern used to be anchored on the bare word, so it caught `AVERAGE` and
# missed every real heading in this corpus.  Measured: 226 such rows were in
# the training pool -- `Average Bid` 86, `AVERAGE COST` 65, `Average of Bids`
# 48, `AVERAGE BID` 18, and 9 rows of `Variance (Estimate - Average)`, whose
# "price" is a DIFFERENCE and which survived only where the difference happened
# to be positive, so even the surviving subset is biased.
#
# `AVERAGE COST Engineers OPCC` (16 rows) was caught by ENGINEER_RE and tagged
# is_engineer_estimate, so it never trained -- but it is NOT an OPCC: its
# values are the bidders' mean in 11 of 11 testable rows (E. Bethel's real
# OPCC column is empty).  find_computed_columns below now drops it.
COMPUTED_COL_RE = re.compile(
    r"^\s*(?:the\s+)?"
    # ... and with the bidders named first: 'BIDDER AVERAGE' (Cedar Hill
    # PW18-0020, 30 rows read as a bidder in the 2026-11-19 rebuild), 'Bid
    # Average', "All Bidders' Average".
    r"(?:(?:all\s+)?(?:bidders?'?s?'?|bids?)\s+)?"
    r"(?:averages?|avg\.?|mean|median|low|high|min|max|range|std|std\.?\s*dev|"
    r"variance|varience|%\s*diff|difference|spread|delta)"
    # optional trailing words: "Average BID", "AVERAGE COST", "Average of Bids",
    # "Variance (Estimate - Average)", "Average Unit Price".
    r"(?:\s*(?:of|is|the)\b)?"
    r"(?:[\s\-(),/]*(?:bids?|cost|costs|price|prices|unit\s*price|amount|"
    r"amounts|value|total|totals|estimate|estimates|per\s*unit|each|"
    r"average|averages|low|high|all)\b)*"
    r"[\s\-(),/.]*$", re.I)

# A column DERIVED from a bid after the opening is not a bid either.  Richardson
# 38-23 (Apollo Road) prints 'BID Opening | Value Engineered | Explanation of
# Changes': the low bid as opened, then the price the city negotiated with that
# bidder afterwards.  Read as two more bidders, the first counts the low bid
# twice and the second adds a negotiated, non-competitive price list.
DERIVED_COL_RE = re.compile(
    r"^\W*(?:bid\s*opening|as\s*opened|value[\s-]*engineer(?:ed|ing)?|v\.?\s*e\.?\s+(?:price|bid|total)|"
    r"negotiated(?:\s+(?:price|bid|total|amount))?|explanation\s+of\s+changes?)\W*$", re.I)

# ---- a computed column found by its VALUES, not its header ----------------
# COMPUTED_COL_RE judges the LABEL, so it cannot see a computed column whose
# label is not the string the parser stored as its name.  Two shapes, both
# from Coppell:
#   * E. Bethel Bridge prints `BIDDER 1 | BIDDER 2 | BIDDER 3 | AVERAGE COST |
#     Engineers OPCC`.  The coordinate strategy fused the last two labels into
#     one name, `AVERAGE COST Engineers OPCC`; ENGINEER_RE matched it, so the
#     column of the bidders' AVERAGE was carried as the engineer's OPCC (16
#     line_items rows, 6 in the panel, 26 rows of bid_vs_opcc.csv comparing the
#     bids with their own mean).  The real OPCC column is empty (#REF!).
#   * South Royal Lane (2025-10-02) and Arbor Brook (2026-01-14) print
#     `Avg. Unit Price` BETWEEN the unit column and the first bidder, where a
#     bidder sits.  A parser that takes the first money column after the unit
#     as bidder 1 replaces that bidder's prices with the mean -- $828,637.82
#     for mobilization -- under a real contractor's name, which no header test
#     can ever catch.  Both files parse to 0 rows today, so here this is a
#     guard: on their printed numbers, laid out that way, the test below
#     finds the column 127/127 and 76/76.
# A computed column is a FUNCTION OF ITS NEIGHBOURS, and no wording hides
# that: row by row it equals the mean (or median) of the other columns.
#
# What counts as evidence -- one row TESTS the hypothesis only when a real
# bidder could not satisfy it by a tie or by coarse rounding:
#   * at least two other priced columns, and they DISAGREE by at least 20x the
#     tolerance.  An owner-set allowance ($25,000 from every bidder) agrees
#     with everything; so does a $0.06 printing price at cent granularity.
#   * tolerance half a cent or 0.05%, whichever is larger, so an average the
#     tab rounded to cents still matches.  An average rounded to whole
#     dollars below $1,000 does not (Airline's completion-DAYS rows are the
#     only measured case, and that column is caught by its header anyway).
#   * mean: tested only when the mean is not itself one of the other values.
#   * median: tested only when it is not the mean; either no other column sits
#     on it (the midpoint of two distinct middles) or exactly one does -- and
#     then the column must tie with at least two DIFFERENT partners across its
#     rows, or it is a duplicated column, which is another defect.
# The "others" are taken four ways, because the average is not always over
# every other column: all of them; minus engineer/header-computed columns;
# only the columns to the LEFT (Average printed after the bidders, beside an
# OPCC); only those to the RIGHT (Coppell's Avg. Unit Price before them).
#
# THRESHOLD, measured on the run that built line_items.csv (92,486 records
# before cleaning, 510 files with rows) plus the 32 files that arrived after
# it: 3,243 columns could be judged.
#   * per informative row, a REAL column equals the mean of the others 0.31%
#     of the time, the midpoint median 0.51%, a tied median 2.3%;
#   * per column, the highest agreement any real column reached on >= 5
#     informative rows is 0.40 (a sub-dollar printing tab);
#   * every computed column with >= 8 informative rows agrees 0.949-1.000:
#     the 10 whose header says so (Average Bid, AVERAGE COST, Averages, ...)
#     and the E. Bethel column above, 11 in all, 329 rows.
# The flagged set is the same 11 for any agreement threshold from 0.6 to 0.9
# and any minimum from 4 to 8 rows; 0.9 over >= 8 rows sits inside that empty
# gap.  Below 8 informative rows the column is NOT judged -- 793 columns,
# 3,444 rows, all still subject to the header test.  Tables of fewer than
# three columns cannot be judged at all (667 columns, 11,011 rows): beside a
# single bidder, an average is that bidder.
COMPUTED_MIN_ROWS = 8
COMPUTED_MIN_AGREE = 0.9
COMPUTED_SPREAD_MULT = 20


def _stat_tol(t):
    return max(0.0051, 0.0005 * abs(t))


def _row_stat_tests(pc, others):
    """The hypotheses ONE row can test about a column priced `pc`.

    `others` is [(col_key, price)] for the other columns of the row.  Returns
    {stat: (matched, tie_partner)} for each test this row is informative for.
    """
    out = {}
    if len(others) < 2:
        return out
    vals = [v for _, v in others]
    hi = max(vals)
    if hi - min(vals) < COMPUTED_SPREAD_MULT * _stat_tol(hi):
        return out

    def eq(a, b):
        return abs(a - b) <= _stat_tol(b)

    mu = sum(vals) / len(vals)
    if not any(eq(v, mu) for v in vals):
        out["mean"] = (eq(pc, mu), None)
    if len(vals) >= 3:
        s = sorted(vals)
        n = len(s)
        md = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
        if not eq(md, mu):
            tied = [k for k, v in others if eq(v, md)]
            if not tied:
                out["median"] = (eq(pc, md), None)
            elif len(tied) == 1:
                out["median_tied"] = (eq(pc, md), tied[0])
    return out


def _grid_rows(sheet_recs):
    """Rebuild one sheet's grid rows from extraction order.

    Every strategy emits a grid row's cells together, left to right, so a row
    is a run of records sharing (section, item, description, unit, quantity);
    a NAMED column seen twice starts the next row (two identical rows in a
    row).  A column is keyed (name, occurrence), so unnamed columns stay
    apart.  Records cached before a column index existed carry nothing else.
    """
    rows, cur, curkey, seen = [], None, None, None
    for r in sheet_recs:
        key = (r.get("bid_section"), r.get("item_no"), r.get("description"),
               r.get("unit_raw"), r.get("quantity"))
        name = r.get("bidder_raw") or ""
        if cur is None or key != curkey or (name and seen[name]):
            cur = []
            rows.append(cur)
            curkey, seen = key, Counter()
        cur.append(((name, seen[name]), r))
        seen[name] += 1
    return rows


def find_computed_columns(recs):
    """Mark every record of a column that is, row by row, the mean or median of
    the other columns of its own row: r["computed_by_value"] = the evidence,
    e.g. "left_mean 56/59".  Returns one finding per column.  Run it BEFORE any
    row is dropped -- an unnamed bidder column is still one of the columns the
    tab averaged over."""
    by_sheet = {}
    for r in recs:
        by_sheet.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    found = []
    for (f, sh), srecs in by_sheet.items():
        rows = _grid_rows(srecs)
        cols = []
        for row in rows:
            for k, _ in row:
                if k not in cols:
                    cols.append(k)
        if len(cols) < 3:
            continue
        for c in cols:
            tally = {}
            for row in rows:
                keys = [k for k, _ in row]
                if c not in keys:
                    continue
                ic = keys.index(c)
                pc = row[ic][1].get("unit_price")
                if pc is None:
                    continue
                priced = [(k, rr["unit_price"]) for k, rr in row
                          if k != c and rr.get("unit_price") is not None]
                variants = {
                    "all": priced,
                    "bids": [(k, v) for k, v in priced
                             if not ENGINEER_RE.search(k[0])
                             and not COMPUTED_COL_RE.match(k[0])],
                    "left": [(k, rr["unit_price"]) for k, rr in row[:ic]
                             if rr.get("unit_price") is not None],
                    "right": [(k, rr["unit_price"]) for k, rr in row[ic + 1:]
                              if rr.get("unit_price") is not None],
                }
                for vn, others in variants.items():
                    for stat, (hit, partner) in _row_stat_tests(pc, others).items():
                        t = tally.setdefault((vn, stat), [0, 0, set()])
                        t[0] += 1
                        if hit:
                            t[1] += 1
                            if partner is not None:
                                t[2].add(partner)
            best = None
            for (vn, stat), (n, k, partners) in tally.items():
                if n < COMPUTED_MIN_ROWS or k < COMPUTED_MIN_AGREE * n:
                    continue
                if stat == "median_tied" and len(partners) < 2:
                    continue
                if best is None or (k / n, n) > (best[2] / best[1], best[1]):
                    best = ("%s_%s" % (vn, stat), n, k)
            if best is None:
                continue
            ev = "%s %d/%d" % (best[0], best[2], best[1])
            n_rows = 0
            for row in rows:
                for k, rr in row:
                    if k == c:
                        rr["computed_by_value"] = ev
                        n_rows += 1
            found.append({"src_file": f, "src_sheet": sh, "column": c[0],
                          "evidence": ev, "rows": n_rows,
                          "header_says_computed": bool(
                              c[0] and COMPUTED_COL_RE.match(c[0]))})
    return found


# Mailing addresses sit directly under bidder names in several tabs; never a name.
ADDRESS_RE = re.compile(
    r"^\s*(p\s*\.?\s*o\s*\.?\s*box\b|\d+\s)|,\s*[A-Z]{2}\s*\d{5}|"
    r"\b\d+\s+[\w.]+\s+(dr|rd|blvd|st|ave|hwy|ln|trl|ste|suite|pkwy|cir|ct|way|pl)\b\.?",
    re.I)

# Column/section labels that look like text but are not bidder names.
NONBIDDER_RE = re.compile(
    r"^\s*(base\s*bid|total\s*price|total\s*cost|total|subtotal|as[- ]read|corrected|"
    r"estimated|est\.?\s*qty\.?|quantity|qty|uom|unit|units|description|item|item\s*no\.?|"
    r"line\s*#|bid\s*qty|mfgr|mfgno|additive\s*alternate.*|add\s*alternate.*|alternate.*|"
    r"alt\.?\s*#?\s*\d*|supplier\s*notes|manufacturer.*|price|extended.*|bid\s*no\..*|"
    r"bid\s*tabulation|summary|notes?|"
    # A BID STATUS printed where the company name goes.  Arlington's Lower
    # Johnson Creek pond dredging prints NONRESPONSIVE in four of its bidder
    # columns -- those firms were disqualified -- and the table strategy read
    # the word as the company.  It is not a template placeholder like
    # "BIDDER 1"; it is a real fact about a real bid, and the distinction
    # matters for what to do about it.
    #
    # Rejecting it here is enough, and the reason is the long format: each
    # bidder is its OWN ROW, so a disqualified column's rows drop while every
    # other bidder's price for the same item stays.  Nothing is lost but the
    # void bid, which was never a price.
    #
    # It also repairs the strategy choice for free.  read_pdf_best prefers the
    # extraction with bidders over the one without; with these words counted as
    # names, the table sheets looked like they had four bidders and won, and
    # the coordinate sheet -- which recovered 83 rows and the two RESPONSIVE
    # firms, Austin Filter Systems and The Fain Group -- lost. Once the words
    # stop counting, the existing preference picks the coordinate sheet itself.
    r"non[\s-]?responsive|non[\s-]?responsible|no\s*bid|did\s*not\s*bid|"
    r"disqualified|withdrawn|void|rejected|incomplete\s*bid)\s*$", re.I)

# ---- caption vocabulary of the price PAIR ----------------------------------
# A pair is only ever formed from a price caption IMMEDIATELY followed by a
# total caption (detect_price_pairs), so a word added here can only open a pair
# where it sits right after a unit-price column.  The 2026-11-19 zero-row census
# found whole templates lost for want of one spelling, each with item rows and
# every bidder's prices on the page:
#   'Unit Price | Subtotal'       Mesquite 2021-22 annual concrete tabs (5 lettings)
#   'UNIT COST | SUBTOTAL COST'   Frisco 2510-007 Dallas Parkway Ph 5 (1,158 cells)
#   'UNIT PRICE | BID TOTAL'      Rowlett 2022-07 Miller Rd paving/drainage
#   'Unit Price | Bid Value'      the 'Project Item Information' template (Keller
#                                 26-743, Mansfield 25-6447, NRH 6473, Fort Worth)
#   'Unit Price Bid | Amount Bid' Coppell 2025-8298 / 2026-0038
#   'UNIT $ | TOTAL $'            Coppell 2023-7379, Hickory Creek BID2024-03
#   'UNIT PRICE | TOTAL ITEM COST' McKinney 22-0149 (illumination, 7 bidders)
#   'UNIT PRICE | BID ESTIMATE'   Halff's split-box tabs (Seagoville PW2501): the
#                                 bidder's bid quantity x unit price, per bidder
# (a bare 'Cost' only completes a header: see detect_price_pairs)
# 'TOTAL UNIT PRICE' (Dallas County) is deliberately NOT a price caption: Farmers
# Branch prints 'Unit Price | Total Unit Price | Total' where the middle cell is
# one bidder's 'Total' run into the next one's 'Unit Price', and as a price
# caption it would pair with the wrong 'Total' and shift every column.
PRICE_HDR = re.compile(r"^\s*(unit\s*price|unit\s*cost|unit|price|bid\s*unit\s*price|"
                       r"unit\s*price\s*bid|unit\s*bid\s*price|unit\s*\$)\s*$", re.I)
TOTAL_HDR = re.compile(
    r"^\s*(total|total\s*cost|total\s*amount|total\s*price|extended|extended\s*cost|"
    r"extended\s*price|unit\s*total|amount|item\s*total|item\s*cost|line\s*total|"
    r"bid\s*amount|total\s*bid|extension|extended\s*amount|"
    r"sub-?\s*total(?:\s*(?:cost|price|amount))?|bid\s*total|bid\s*value|amount\s*bid|"
    r"total\s*\$|total\s*item\s*cost|bid\s*estimate)\s*$", re.I)
ITEM_HDR = re.compile(r"item|line\s*#|line\s*no", re.I)
# The description header comes in two strengths and they must not be treated
# alike.  "DESCRIPTION" is unambiguous.  A bare "ITEM"/"ITEMS"/"BID ITEMS" is a
# FALLBACK for tabs whose description column is headed "Bid Items" with no
# separate DESCRIPTION column -- but it also matches the plain "ITEM" column of
# a normal "ITEM | DESCRIPTION | UNIT | QTY" header.  When it did, detect_meta_cols
# bound desc to the item column, locked it (first hit wins), and never reached the
# real DESCRIPTION column, so every description read back as an item number.
# Measured blast radius: 17,503-23,545 rows across 158 projects and 7 owners,
# which then scored item_family='other' -> constr_share ~ 0 ->
# project_kind='non_construction' -> 41 genuine construction projects dropped
# out of price_reference entirely.  Keep both patterns, but let the strong one win.
# A bare "DESC" is an unambiguous description caption too, and r"descri" does
# not match it -- which left "ITEM | DESC | UOM | QTY" binding desc to the item
# column, the very failure this split exists to prevent.  parse_coords.find_header
# has always accepted DESCRIPTION / ITEMS / DESC; keep the two in step.
DESC_HDR_STRONG = re.compile(r"descri|^\s*desc\.?\s*$", re.I)
DESC_HDR_WEAK = re.compile(r"^\s*(bid\s*)?items?\s*$", re.I)
DESC_HDR = re.compile(r"descri|^\s*(bid\s*)?items?\s*$", re.I)  # kept for callers
UOM_HDR = re.compile(r"^\s*(uom|unit|units|unit\s*of\s*measure)\s*$", re.I)
# (round 11, .32) more spellings of the unit-of-measure caption: 'U/M' (Hickory Creek BID2024-03, Coppell
# 2023-7379: every row of both read without a unit), 'U.O.M.', 'Units of Measure', 'Unit Measure', and a
# bare 'Measure' (Collin County's 'Quantity | Measure | Unit Cost' tabs).  Matched as a whole cell, like
# UOM_HDR, and only where UOM_HDR finds no unit caption (detect_meta_cols).
UOM_HDR_MORE = re.compile(r"^\s*(?:u\s*[/.]\s*m\.?|u\s*[/.]\s*o\s*[/.]\s*m\.?|uom\.|units\s*of\s*measure(?:ment)?|"
                          r"unit\s*of\s*measurement|unit\s*measure|measure|meas\.)\s*$", re.I)
# "QUANTITIES" (plural) is a real header spelling and r"quantity" misses it.
QTY_HDR = re.compile(r"qty|quantit(?:y|ies)", re.I)

# 'Bid Alternate' is Farmers Branch's word order for 'Alternate Bid' (Bids
# 25-17, 26-02, 26-20 all use it); without it the alternate block read as base.
# 'ALT B:' / 'ALT-A:' -- an alternate LETTERED rather than numbered (Cedar
# Hill Complete Streets 2A: 'ALT-A: EDC PARKING LOT', 'ALT B: BROAD ST ...').
SECTION_RE = re.compile(
    r"^\s*(base\s*bid|additive\s*alternate|add\s*alternate|alternate|alt\.?\s*#?\s*\d+|"
    r"alt\.?\s*[#\-]?\s*[a-z]\b|"
    r"bid\s*alternates?\b|"
    r"deductive\s*alternate|site\s*\d+|part\s*[a-z0-9]+|group\s*[a-z0-9]+)", re.I)

# ---- a banner is a LABEL, not a sentence -----------------------------------
# SECTION_RE matches a PREFIX, so a price-less NOTE that merely begins with a
# section word opened a section: 'Bid alternates are priced on sheet 3 and are
# not part of this tab' above the base items made every base item after it an
# alternate, and the base bid disappeared.  A banner is the section word,
# optionally qualified ('Bid', 'Items', 'Schedule', 'Option', 'Total'),
# optionally numbered or lettered, and then nothing, a separator and a title
# (':', '-', '(', '+', '/', ','), or -- after a number -- a title.  Every
# banner the corpus prints has that shape: BASE BID, Bid Alternate, ALTERNATE
# BID ITEMS, Add Alternate # 1, ALTERNATE #1 PAVING ITEMS, BASE BID A -
# SECTION I - MISC., Alternate 1: Bowser Road & Windsong Trail, BID ALTERNATE
# #1 (5 ITEMS), Base Bid:, BASE BID + Alt #1, GROUP 1: TOTAL.  A sentence goes
# on in words after the section word, and is refused.
_SEC_KW = (r"(?:base\s*bids?|(?:additive|add|deductive|deduct)\s*alternates?|"
           r"bid\s*alternates?|alternates?|alt\.?|site|part|group)")
_SEC_QUAL = (r"(?:bids?|items?|schedules?|sections?|options?|deduct|additive|"
             r"sub\s*-?\s*totals?|totals?)")
BANNER_RE = re.compile(
    r"^\W*" + _SEC_KW + r"\b(?:\s+" + _SEC_QUAL + r"\b)*"
    r"(?:\s*(?:#|no\.|no\b|number\b)?\s*(?:\d{1,3}[a-z]?|[ivx]{1,5}|[a-z])\b"
    r"(?:\s+\S.*)?)?"
    r"\s*(?:[:\-(+/,].*)?$", re.I)


# (round 11, .32) the words of a NUMBERED group heading that open a section (rows_from_grid): the section
# word alone, numbered or lettered, with at most a title after ':' or '-' -- parse_coords.SECTION_BANNER_RE.
_NUMBERED_BANNER_RE = re.compile(
    r"^\W*(?:base\s+bid(?:\s+items?)?|(?:bid\s+|add(?:itive)?\s+|deduct(?:ive)?\s+)?alternates?"
    r"(?:\s+bid)?(?:\s*(?:#|no\.?)?\s*(?:\d{1,2}|[a-z]|[ivx]{1,4})\b)?(?:\s*[:\-].{0,60})?)\W*$", re.I)


def _one_edit_apart(a, b):
    """At most one insertion, deletion, substitution or adjacent swap."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    while i < min(la, lb) and a[i] == b[i]:
        i += 1
    if la == lb:
        return (a[i + 1:] == b[i + 1:] or
                (i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]))
    return a[i + 1:] == b[i:] if la > lb else a[i:] == b[i + 1:]


def _fix_alternate_typo(s):
    """A one-letter slip in 'alternate' -- North Richland Hills Bid 26-001
    prints its alternate as 'ALTERNAE BID #1' -- read as the word itself, so
    the section rules see it.  'Alternative' is not a slip (three edits)."""
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if lw in ("alternate", "alternates"):
            return w
        for t in ("alternate", "alternates"):
            if _one_edit_apart(lw, t):
                return t.upper() if w.isupper() else t.capitalize()
        return w
    return re.sub(r"\b[Aa][Ll][Tt][A-Za-z]{4,8}\b", fix, s or "")


# ... and two shapes a banner also takes (parser round 3).  A NUMBERED
# section whose title is itself a banner: NRH RFB 24-021 opens its alternate
# with 'SECTION VI - ALTERNATE BID #1' after 'SECTION I - GENERAL AND
# REMOVALS' .. 'SECTION V - CONTINGENCY', so the alternate's item A-1 stayed in
# section V.  And two banners in one cell, the block's caption over the first
# block's own: Cedar Hill Complete Streets 2A prints 'ALTERNATE ITEMS' over
# 'ALT-A: EDC PARKING LOT' in one merged cell, so ALT-A-1 and ALT-B-1 were read
# as base bid ($69,760 on the low bidder).  A numbered title that is not a
# banner ('SECTION I - GENERAL AND REMOVALS') still opens nothing here.
_NUMBERED_SECTION_RE = re.compile(
    r"^\s*(?:section|part|division|schedule)\s+(?:no\.?\s*)?(?:[ivxlc]{1,6}|\d{1,3}[a-z]?)"
    r"\s*[-:.\u2013]\s*(?P<title>\S.*)$", re.I)

# An alternate may be NAMED by its ordinal word: Cedar Hill PW23-0004 titles
# its two alternate tables 'FIRST ADD ALTERNATE' and 'SECOND ADD ALTERNATE'.
# The ordinal is passed over (only in front of an alternate word) and the
# banner judged on the rest; the label keeps it.
_ORDINAL_ALT_RE = re.compile(
    r"^\W*(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"\d{1,2}(?:st|nd|rd|th))\s+(?=(?:(?:additive|add|deductive|deduct|bid)\s+)?alternat)",
    re.I)


_ALT_IN_SECTION_TITLE_RE = re.compile(r"\b(?:(?:add(?:itive)?|deduct(?:ive)?|bid)\s+)?alternates?\b", re.I)


def section_banner(text):
    """The section a price-less banner opens -- 'BID ALTERNATE #1' -- or ''."""
    s = _fix_alternate_typo(txt(text))
    k = _ORDINAL_ALT_RE.sub("", s)
    if k and len(k) < 120 and SECTION_RE.match(k) and BANNER_RE.match(k):
        return s.upper()[:60]
    m = _NUMBERED_SECTION_RE.match(s or "")
    if s and len(s) < 120 and m and section_banner(m.group("title")):
        return s.upper()[:60]
    # A numbered section whose TITLE names an alternate anywhere, not only at
    # its start: Dallas County's Riverfront Blvd (2024) prints its add
    # alternates as 'SCHEDULE 13 -- LANDSCAPE AND IRRIGATION ADD ALTERNATE L1'
    # .. 'SCHEDULE 21 -- DRAINAGE ALTERNATE 2' after the base 'TOTAL BID:', and
    # all nine were read as base bid (+$4.8M on the low bidder, the low bid
    # 4.8% high; tail audit 2026-11-20).  A title that also says BASE BID is
    # left alone.  The label keeps an 'ALT' word inside the 60 characters
    # main()'s role rule reads.
    if (s and len(s) < 120 and m and _ALT_IN_SECTION_TITLE_RE.search(m.group("title"))
            and not re.search(r"\bbase\s+bid", m.group("title"), re.I)):
        lab = s.upper()
        return lab[:60] if re.search(r"\balt", lab[:60], re.I) else ("ALT " + lab)[:60]
    if s and len(s) < 120 and SECTION_RE.match(s):
        for sp in re.finditer(r"\s+", s):
            head, tail = s[:sp.start()], s[sp.end():]
            if (BANNER_RE.match(head) and SECTION_RE.match(tail) and BANNER_RE.match(tail)):
                return s.upper()[:60]
    return ""


# ---- the TIME half of a cost-plus-time (A+B) bid ---------------------------
# An A+B tab ranks bids on A (the priced work) plus B (days bid x a daily road
# user cost the owner sets); the contract is A.  Coppell's Airline Drive tab
# (TAX 2016B) prints 'BASE BID B - SECTION XI - TOTAL TIME TO COMPLETE
# PROJECT': four milestones at '1,500 | $ PER DAY' with each bidder's DAYS in
# the unit-price column, the engineer's OPCC $0.00, and the sum labelled
# 'TOTAL AMOUNT BIDS (A) + (B) Basis for Comparison' -- the contract amount is
# 'TOTAL AMOUNT - BASE BID A'.  Read as base, the B days became $600,000 of
# "work" on McMahon's $4,454,084.65.  A section naming a time bid, a time to
# complete or the contract time is not base scope (main(): role 'other').
# ... and the same B half in TxDOT's tabulations, as an ITEM.  TxDOT lets some
# jobs on A+B: item 800-x001 'NO. OF WORKING DAYS', unit '$/D', quantity = the
# daily road user cost, 'unit price' = the days the bidder bid.  It ranks the
# bids and is never paid: TxDOT's own bid_total_amount leaves it out, while the
# archive's printed BIDDER totals keep it in.  Summed as an item it put $2.18M
# on Archer Western's $93.87M (A00066071, 2026), and as a unit price it is a
# day count.  load_txdot and parse_txdot_archive drop it from every bidder
# total, the low bid and the item pool (tail audit 2026-11-20).
TIME_BID_ITEM_RE = re.compile(r"^\s*0*800-?\d001(?!\d)")


def is_time_bid_item(code, unit, desc):
    """True for TxDOT's A+B time item: code 800-x001 AND a per-day unit or
    'WORKING DAYS' in its description."""
    if not TIME_BID_ITEM_RE.match(str(code or "")):
        return False
    u = re.sub(r"\s+", "", str(unit or "")).upper()
    return u in ("$/D", "$/DAY", "$/DY", "DAY", "DAYS", "WD") or "WORKING DAY" in str(desc or "").upper()


TIME_BID_SEC_RE = re.compile(
    r"\btime\s+(?:bid|to\s+complete|of\s+completion|for\s+completion)\b|"
    r"\bcontract\s+time\b|\bbid\s*\(?b\)?\s*[-:(]?\s*time\b", re.I)


# ---- an alternate banner glued onto the first item of its block -----------
# When a table loses the rule under a banner row, the banner becomes the first
# LINE of the next item's description: NRH Bid 26-001 prints 'ALTERNAE BID
# #1' over its four alternate items and the table reads item 1 as 'ALTERNAE
# BID #1 / 6 Inch Thick Flex Base' -- a PRICED row, which the banner test
# never sees, so the alternate stayed in the base bid (+$294,147 on the low
# bidder).  A priced row whose description OPENS with an unmistakable
# alternate caption -- the word with an explicit '#' / 'No.' number, a
# one-letter slip in it allowed -- opens that alternate as a banner row
# would.  ('Alternate 8 inch PVC' has no '#' / 'No.' and is left alone.)
GLUED_ALT_RE = re.compile(
    r"^\s*(?P<banner>(?:(?:additive|add|deductive|deduct|bid)\s+)?alternates?"
    r"(?:\s+bid)?\s*(?:#|no\.|number)\s*\d{1,2}[a-z]?)\b", re.I)

# ---- ... and the BASE BID banner glued onto item 1 ---------------------------
# The same loss for the base banner.  Frisco's tabs prepared by Quiddity (3rd &
# Maple 2024, Research Rd 2024) print 'BASE BID' on the line above item 1 in
# the description cell, so the table reads item 1 as 'BASE BID Mobilization
# (5% Max of Base)' -- a PRICED row -- and main()'s aggregate filter (a
# description opening 'base bid' is a total) dropped it: every bidder lost its
# mobilization ($30,000-$90,809), the low bid read 4.6-5.0% short (tail audit
# 2026-11-20).  A priced row with a unit or a quantity whose description opens
# with the banner and goes on in an item's words keeps the words and opens the
# base section.  'BASE BID TOTAL', 'Base Bid Amount', 'Base Bid + Alt 1',
# 'Base Bid Items' alone are not items and are left to the rules that drop them.
GLUED_BASE_RE = re.compile(
    r"^\s*(?P<banner>base\s+bid(?:\s+items?)?)\s*[:\-\u2013]?\s+"
    r"(?P<rest>(?!(?:sub\s*-?\s*)?totals?\b|amounts?\b|bids?\b|prices?\b|with\b|plus\b|"
    r"and\b|items?\b|schedule\b|section\b|alt)[A-Za-z(].*)$", re.I)

DATE_NUM = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# (round 9, .30) A month name in ANY case, or abbreviated.  The old pattern took only "June 6,
# 2024": capitals ("BID OPENING: JULY 8, 2026", "BID OPENING 2:00 PM, THURSDAY, SEPTEMBER 2, 2021")
# and "Sept. 2, 2021" were no date at all, so the tab fell back to a council meeting date printed in
# Title Case two months later, or to its bid number's year (YYYY-01-01).  The month word must be
# one of MONTHS (full or three-letter, "sept"), so "Item 12, 2024" is still no date.
DATE_TXT = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})(?!\d)")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9


def txt(v):
    if v is None:
        return ""
    s = unicodedata.normalize("NFKC", str(v))
    for bad, good in (("‐", "-"), ("‑", "-"), ("–", "-"), ("—", "-"),
                      ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        s = s.replace(bad, good)
    return re.sub(r"\s+", " ", s).strip()


NUM_STATS = Counter()


def num(v):
    """Parse a money/qty cell -> float or None. Handles '$ 1 51,000.00' PDF artifacts.

    Gluing digits across whitespace is DELIBERATE: PDFs split a single number
    into several text runs, and refusing to rejoin them loses real prices.

    It was briefly changed to reject any cell holding more than one
    digit-bearing token, on a report that this pipeline had invented unit
    prices of 1,417.25 and 1,132.42 for Briar Hill (Bid 26020).  That premise
    was FALSE and the change is reverted.  Verified directly against the source:
    both values, and their extended amounts 51,021.00 and 40,767.12, appear on
    page 157 as item 122 "Concrete Channel Grade Control Walls", 36 CY --
    1,417.25 x 36 = 51,021.00 and 1,132.42 x 36 = 40,767.12, both exact.  The
    real defect there is MIS-ATTRIBUTION (item 122's true prices carrying item
    123's gabion description, with quantity lost), not fabrication.

    The lesson worth keeping: the guard against a glued-together wrong value is
    not refusing to glue, it is the arithmetic check that already exists
    (quantity x unit_price == printed extended, `ext_mismatch`).  A spuriously
    glued number fails it; a legitimately rejoined one passes.  Existence of a
    value in the source text is the WEAKER test -- it cannot tell a real price
    that was mis-filed from one that was concatenated -- so audit with the
    arithmetic, not with a string search.

    NUM_STATS counts multi-token cells so the exposure stays visible without
    discarding anything.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = txt(v)
    if not s:
        return None
    if len([t for t in re.split(r"\s+", s.strip())
            if any(c.isdigit() for c in t)]) > 1:
        NUM_STATS["multi_token_cells_glued"] += 1
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "").replace(" ", "")
    if s in ("-", "--", "", ".", "n/a", "na"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def norm_unit(v):
    s = txt(v).lower().rstrip(".")
    if not s:
        return ""
    if s in UNIT_MAP:
        return UNIT_MAP[s]
    s2 = re.sub(r"[^a-z/ ]", "", s).strip()
    if s2 in UNIT_MAP:
        return UNIT_MAP[s2]
    # A unit spelled out with its code in parentheses -- 'THOUSAND GALLONS
    # (TGL)', 'CUBIC YARD (CY)'.  Neither the whole string nor its letters-only
    # form is a key, so the unit read as unknown and the row as unit_suspect.
    # The text OUTSIDE the parentheses is the unit whenever it is one: a
    # parenthetical is as often a CONVERSION as a code -- 'MG (1000 GAL)',
    # 'TON (2000 LB)', 'CY (27 CF)', 'AC (43,560 SF)', 'SY (9 SF)',
    # 'MO (30 DAYS)', 'DAY (8 HR)' -- and trying the parenthetical first read
    # each of those as the unit it converts INTO, a 1,000x / 2,000x / 27x
    # error in every price per unit.  So the outside decides; the parenthetical
    # is read only when the outside is not a unit ('PIPE (LF)'), and a
    # parenthetical that is not one either ('LS (SEE NOTE 3)') falls through
    # to the raw string.  'THOUSAND GALLONS (TGL)' reads MG either way.
    m = re.match(r"^(.*?)\s*\(\s*([^()]{1,16}?)\s*\)\s*$", s)
    if m:
        for part in (m.group(1), m.group(2)):
            p = part.strip().rstrip(".")
            if p in UNIT_MAP:
                return UNIT_MAP[p]
            p2 = re.sub(r"[^a-z/ ]", "", p).strip()
            if p2 in UNIT_MAP:
                return UNIT_MAP[p2]
    # A unit written as a RATE -- 'PER LUMP SUM', 'PER SQUARE YARD', 'PER TON'
    # (Richardson Bid 35-25), '$ Per SY' (Mansfield 22-4816).  No row in the
    # 2026-11-19 line_items carries this form: every file that prints it was
    # rejected whole by read_pdf_best's unit gate, so it changes no parsed row.
    # Only a known unit after the 'per' counts ('Per truck' stays as printed).
    m = re.match(r"^\$?\s*per\s+(.+)$", s)
    if m:
        u = norm_unit(m.group(1))
        if u in KNOWN_UNITS:
            return u
    # ONE stray letter in front of a unit -- the last glyph of a description
    # that overflows into the unit cell, shown cut at the cell's edge (round 3:
    # Chalk Hill Trail's item 7767 'A ALW', the low bidder's $50,000 allowance
    # left the panel as unit_suspect; 'N LF', 'P SY', 'Y EA' in five more tabs).
    m = re.match(r"^[a-z]\s+(\S.*)$", s)
    if m:
        u = norm_unit(m.group(1))
        if u in KNOWN_UNITS:
            return u
    return txt(v).upper()


# ---- an ambiguous unit the row's own words resolve -------------------------
# A bare 'YD' / 'YARD' is a cubic or a square yard, a bare 'SQ' a square foot,
# a square yard or a roofing square, so UNIT_MAP keeps none of them and the
# row stays unit_suspect.  The row can say which: Lewisville 25-4410 prints
# 'Yard' for '#0-7 Remove 4 inch to 5 inch concrete sidewalk 0-50 s.y.' (22
# rows, 11 bidders).  Read only from an explicit square/cubic yard or square
# foot wording in the description, and only when it names exactly one.
_AMBIG_UNIT_RE = re.compile(r"^\s*(?:per\s+)?(?:yds?|yards?|sq)\s*\.?\s*$", re.I)
_DESC_UNIT_RES = (
    ("SY", re.compile(r"\bs\.\s?y\.|\bsq\.?\s*yds?\b|\bsquare\s+yards?\b", re.I)),
    ("CY", re.compile(r"\bc\.\s?y\.|\bcu\.?\s*yds?\b|\bcubic\s+yards?\b", re.I)),
    ("SF", re.compile(r"\bs\.\s?f\.|\bsq\.?\s*f(?:ee)?t\b|\bsquare\s+f(?:oo|ee)t\b", re.I)),
)


def unit_from_description(unit_raw, desc):
    """The unit a bare 'YD' / 'YARD' / 'SQ' stands for when the description
    names it ('... sidewalk 0-50 s.y.'), else ''."""
    if not _AMBIG_UNIT_RE.match(unit_raw or ""):
        return ""
    said = {u for u, rx in _DESC_UNIT_RES if rx.search(desc or "")}
    if len(said) != 1:
        return ""
    u = said.pop()
    # a yard is never a square foot; a bare 'SQ' is never a cubic yard
    if re.search(r"y", unit_raw, re.I) and u == "SF":
        return ""
    if re.search(r"sq", unit_raw, re.I) and u == "CY":
        return ""
    return u


# A caption printed in front of every firm: 'BID OF Atkins Bros. Equip. Co.,
# Inc.' (55 columns in the 2026-11-19 new files: Murphy, Rockwall).  Left on,
# the firm is two bidders downstream ('bidofatkins...' never joins 'atkins...').
# 'LOW BID' / 'Low Bidder' is NOT stripped here: it is the owner's statement of
# rank, read by bidder_identity.rank_of, which removes it itself.
_BID_OF_RE = re.compile(r"^\s*bid\s+(?:of|from|by)\s*[:\-]?\s+(?=\S{2})", re.I)


def canon_bidder(name):
    name = _BID_OF_RE.sub("", txt(name))
    s = txt(name)
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\.com\b", " ", s, flags=re.I)
    s = s.lower()
    s = re.sub(r"[,.&']", " ", s)
    s = re.sub(SUFFIX_RX, " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return BIDDER_ALIASES.get(s, txt(name))


def norm_desc(d):
    s = txt(d).lower()
    s = s.replace('"', " in ").replace("''", " in ")
    s = re.sub(r"\binch(es)?\b", " in ", s)
    s = re.sub(r"\brem\.?\b", "remove", s)
    s = re.sub(r"\bdisp\.?\b", "dispose", s)
    s = re.sub(r"\breinf\.?\b", "reinforced", s)
    s = re.sub(r"\bconc\.?\b", "concrete", s)
    s = re.sub(r"\basph\.?\b", "asphalt", s)
    s = re.sub(r"\bpvmt\.?\b", "pavement", s)
    # TxDOT truncates item names hard ("REMOV STR (PIPE)", "EXCAV (ROADWAY)",
    # "MTL W-BEAM GD FEN", "RC PIPE (CL III)"). Expanding the abbreviations here
    # fixes the vocabulary once for every classification rule downstream.
    for pat, rep in (
        (r"\bremov\b", "remove"), (r"\bexcav\b", "excavation"),
        (r"\bmtl\b", "metal"), (r"\bgd fen\b", "guard fence"),
        (r"\bstr\b", "structure"), (r"\bsler\b", "sealer"),
        (r"\brc pipe\b", "rcp"), (r"\bcl\b", "class"),
        (r"\bfurn\b", "furnish"), (r"\binstl\b", "install"),
        (r"\bfdn\b", "foundation"), (r"\bshldr\b", "shoulder"),
        (r"\bexist\b", "existing"), (r"\bagg\b", "aggregate"),
        (r"\bmbr\b", "member"), (r"\bsdwlk\b", "sidewalk"),
        (r"\bdrnge\b", "drainage"), (r"\bmanh\b", "manhole"),
        (r"\bbkfl\b", "backfill"), (r"\bcem\b", "cement"),
        (r"\bstabil\b", "stabilized"), (r"\bsurf\b", "surface"),
    ):
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9 /]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# .24c: what pavement milling is NOT -- "mill and overlay" / "mill & inlay"
# build an overlay; milled rumble strips are their own item; "Mill Creek" is a
# street.  Used inside the pavement_milling rule below, at the match position.
_MILL_NOT = (r"(?!.*\brumble\b)"
             r"(?!.*\bmill\w*\b.{0,40}(?:\band\b|\bwith\b|\bw/)\s?.{0,20}\b(?:overlay|inlay|repave|resurfac\w*|hmac|hot\s+mix)\b)"
             r"(?!.*\bmill\w*[\s/]+(?:overlay|inlay|pave|repave|replace|resurfac\w*)\b)")
_MILL_STREET = (r"(?!\s+(?:creek|st|street|rd|road|pond|run|lane|ln|dr|drive|branch|valley|ave|avenue|"
                r"park|ridge|trail|race|dam|crossing|place|pl)\b)")
# What a cleaning / desilting line cleans (round 6): a drainage conveyance or
# structure.  Used by the cleaning_desilting rule in FAMILY_RULES.
_CLEAN_NOUN = (r"(?:culverts?|culv|pipes?|piping|box(?:es)?|rcbc?s?|channels?|ditch\w*|inlets?|outfalls?|"
               r"flumes?|drains?|drainage|storm|sewers?|mains?|lines?|conduits?|swales?|ponds?|basins?|"
               r"detention|retention|creeks?|streams?|structures?|manholes?|junction|catch\s+basins?|"
               r"tanks?|wet\s?wells?|siphons?|crossings?)\b")

# (round 12, .33) A TRAIL / SIDEWALK named beside earthwork or planting words, with no surfacing material
# named, is WHERE the work is, not what it is.  The sidewalk rule matched "trail" and "sidewalk" anywhere
# and sits ahead of grading / excavation / embankment_fill / seeding_sod, so Keller's "Excavation (Trail)"
# (400 CY), Mesquite's "Unclassified Excavation (Trail)", Irving's "Unclassified Trail Excavation",
# Lewisville's "Trail Grading (Excavation and finish grading)", Farmers Branch's "Excavate clean soil for 12
# foot trail" and Frisco's "TRAIL EARTHWORK" were sidewalk -- the sidewalk|CY cell mixed earthwork with
# concrete sidewalk per CY -- and McKinney's
# "SIDEWALK CONSTRUCTION AND REPAIR: Bermuda grass - price to include 4 inch Topsoil" (16,625 SF) and Frisco's
# "Planting Bed Prep 2" Compost (Non Trail Areas)" were sidewalk too.  The sidewalk rule now yields when the
# text names earthwork or planting and no surfacing material: "Excavate soil and install new Class A concrete
# sidewalk" and "4" sidewalk including excavation" are still sidewalks (concrete is named; the head decides).
_PEDWAY_EARTH = (r"\b(?:excavat\w*|excav|embankment\w*|embank|borrow|earth\s?works?|(?:re)?grading|"
                 r"(?:fine|finish(?:ed)?|final|rough)\s+grade|fill|backfill\w*)\b")
_PEDWAY_PLANT = (r"\b(?:sod\w*|seed\w*|hydr[ao]\s?-?\s?mulch\w*|mulch\w*|turf\w*|grass\w*|top\s?soil|compost\w*|"
                 r"(?:bed|soil)\s+prep\w*|fertiliz\w*|irrigat\w*|planting)\b")
# (the material stems are read inside glued words too: "...Class A concretesidewalkmatchexisting...")
_PEDWAY_SURFACE = (r"(?:(?:concret|asphalt|hmac|pavement|paving|paver|reinforc|brick)|"
                   r"\b(?:conc|pav\w*|psi|flex\w*|base|gravel|granite|crushed|aggregate|boardwalks?|decks?|steps|"
                   r"ramps?)\b)")
_PEDWAY_YIELD = r"^(?!(?=.*" + _PEDWAY_EARTH + r"|.*" + _PEDWAY_PLANT + r")(?!.*" + _PEDWAY_SURFACE + r"))"
# (round 12, .33) What an ABANDONMENT plugs, caps, grouts or fills: a pipe, sewer, water line, service or
# utility structure, with the tabs' abbreviations ("Ex. 12" W.L.", "Ex 6" S.S.", "SAN SEWER").
_UTIL_NOUN = (r"(?:pipes?|piping|lines?|mains?|sewers?|water|wastewater|ww|ss|s\s+s|wl|w\s+l|san|sanitary|storm|"
              r"drains?|force\s+mains?|services?|laterals?|rcp|cmp|dip|pvc|hdpe|vcp|culverts?|manholes?|inlets?|"
              r"junction\s+box(?:es)?|structures?|\d+(?:\.\d+)?\s*in)")

FAMILY_RULES = [
    # ---- order matters -------------------------------------------------
    # Removal items must be classified BEFORE the thing being removed, or
    # "sawing, removal and disposal of existing concrete sidewalk" lands in
    # `sidewalk` (construction) instead of demolition. Likewise "curb inlet"
    # must reach `inlet_structure` before `curb_gutter` claims the word curb.
    # `move in` needs its word boundaries: unanchored it matched inside
    # "reMOVE INlet" and "remove including", and because this is the FIRST
    # rule, 17 descriptions (166 city rows, "REMOVE INLET" alone 77) were
    # filed as mobilization (round 3b).
    # Misspelt (round 5, reports/diag_worst_municipal_2026-11-20.md): seven
    # Frisco tabs print "MOBILIZAITON (INCLUDING PAYMENT AND PERFORMANCE
    # BONDS)", which missed this rule and was filed bonds_insurance by its
    # `bonds`.  mobili?[sz] keeps "mobilization", "demobilization" and
    # "mobilisation" and adds "mobilizaiton" / "mobilzation"; "mobile" and
    # "mobility" do not match (no s/z after "mobil(i)").
    # (round 7, .28) TxDOT's item 500 IS mobilization: "TxDOT item 500" or
    # its bid code 500-6001 ("500-6001 6001 EQUIPMENT ETC)", statewide) without
    # the word were `other`; so was "Mobiliization (MAX 5% OF BID TOTAL)"
    # (7 city rows), a doubled i.  Roanoke Bowie St's "MOBILIZATION
    # MOBILIZATION (TXDOT ITEM 500)" was already mobilization -- its $1.04M
    # was the price level of a small job's lump sum, not the family.  A bare
    # "item 500" (no TxDOT, no word) is some other numbering and stays.
    ("mobilization",        r"mobili{0,2}[sz]\w*|\bmoblization\b|\b(?:de)?mob\b|\bmove[- ]in\b|"
                            r"\btx\s?dot\s+(?:std\s+|standard\s+|spec\w*\s+)?item\s+(?:no\s+)?500\b|"
                            r"\b500\s6\d{3}\b"),
    # A masonry or paver BOND PATTERN is not a surety bond: "PAVER FIELDS
    # (RUNNING BOND)" (Frisco Main Street, 31,454 SF) and "Pavestone Concrete
    # Paver, Running Bond Pattern" were bonds_insurance, unpriced (round 5).
    ("bonds_insurance",     r"(?<!running )(?<!stack )(?<!basket )(?<!herringbone )(?<!flemish )"
                            r"(?<!english )\bbonds?\b(?!\s+(?:pattern|beam|breaker|course)s?\b)|"
                            r"insurance|general provisions"),
    # Owner-set money (estimate_low_bid.allowance_like reads this family).
    # Misspellings (round 5): Farmers Branch prints "UNFORSEEN ITEMS, AS
    # APPROVED BY THE CITY ENGINEER" (other) and Richardson "Contignency"
    # (other), so neither was recognised as an owner-set amount; "allowances"
    # is the plural the old \ballowance\b missed.
    ("contingency",         r"unfore?seen|\bconti(?:n?g|gn)\w{0,3}n[cs][iy]\w*|contingenc|"
                            r"additional quantit|\ballowances?\b"),
    # TxDOT writes highway work in its own abbreviations, which none of the
    # municipal phrasings reach: TMA, REFL PAV MRK, SEDMT CONT, PREPARING ROW.
    # Without these, two thirds of 160k TxDOT rows land in `other`.
    ("traffic_control",     r"traffic control|barricad|\btma\b|changeable message|"
                            r"traffic handling|arrow panel|arrow board|\bflagger|"
                            r"portable message|\bpcms\b"),
    # NOT "work zone": "WORK ZONE PAVEMENT MARKINGS" is a per-LF striping item,
    # and because traffic_control is matched first that token would pull $0.50/LF
    # stripes into the same family as $5,000/MO barricades.
    # "RE PM" is how the TxDOT archive abbreviates REFLECTORIZED PAVEMENT
    # MARKING: 11,267 archive rows at a $0.55 median were sitting in `other`
    # because the rules only knew "refl pav" and "pav mrk".
    ("pavement_marking",    r"refl\s*pav|pav\s*mrk|\bre pm\b|\bmrkr?s?\b|marking|"
                            r"striping|(?<!push )button|\brpms?\b|"   # (round 8) not push buttons; RPM
                            # (round 9) "Curb Address Painting" (LS, bids $600) / "PAINT HOUSE NUMBER ON
                            # CURB" (EA, $100) were `painting` at $157k / $31k -- tank and bridge coating
                            r"\bcurb\s+address\w*|\b(?:house|address)\s+(?:numbers?|numerals?)\b(?=.{0,30}\bcurbs?\b)|"
                            r"^(?:\S+\s+){0,2}?(?:house|address)\s+numbers?\s+replac\w*|"
                            r"thermoplastic|\bprefab pav mrk\b|raised pav|"
                            r"pavement sealer|\bsealer\b"),
    # (round 20, .34) Markings named by the LINE they paint, not by "marking" / "striping": "24" White Stop Bar"
    # (bids $18/LF) was `other`, priced from the `other` 19-25 in LF cell at $310/LF; "9999-0105 6" Double Yellow" at
    # $25/LF (bids $3); "4" Refl Raised Marker Ty II-C-R" at $67/EA (bids $9.50); "24" wide x 6' White Thermo
    # Intersection Crosswalk" at $6,032/EA (bids $264).  A crosswalk counts only beside a marking word (colour,
    # thermo, paint, stripe, bar, line, high-visibility, ladder / continental / zebra): a crosswalk ramp, signal,
    # light or stamped-concrete crosswalk is no marking.  White / yellow need a line word (solid, broken, dashed,
    # dotted, skip, stop / lane / edge / centre line) within 40 characters.
    ("pavement_marking",    r"\bstop\s*-?\s*bars?\b|"
                            r"\b(?:white|yellow|wht|ylw)\b.{0,40}\b(?:solid|broken|dashed|dotted|skip|"
                            r"stop\s+lines?|lane\s+lines?|edge\s+lines?|cent(?:er|re)\s?lines?)\b|"
                            r"\b(?:solid|broken|dashed|dotted|skip|double)\s+(?:\w+\s+){0,2}?(?:white|yellow|lines?|stripes?)\b|"
                            r"\bdouble\s+yellow\b|"
                            r"\braised\s+(?:pavement\s+|reflective\s+|refl\s+)?markers?\b|\brefl\w*\s+(?:raised\s+)?markers?\b|"
                            r"\bcrosswalks?\b(?=.*\b(?:white|yellow|thermo\w*|paint\w*|stripe\w*|bars?|lines?|"
                            r"high\s+visibility|ladder|continental|zebra)\b)|"
                            r"\b(?:white|yellow|thermo\w*|high\s+visibility|ladder|continental|zebra)\b.{0,40}\bcrosswalks?\b"),
    ("signing",             r"^(?!.*\bpush\s?-?\s?buttons?\b).*?(?:"   # (round 8) not APS push buttons with their signs
                            r"\bsn sup\b|roadside sign|sign support|aluminum sign|sign blank|"
                            r"\bsigns?\b.{0,20}\b(install|furnish|relocat|replac)|"
                            # verb first, "install traffic sign" (round 3b): the
                            # pattern above only knew "sign ... install"
                            r"\b(?:install\w*|furnish\w*|replac\w*|erect\w*)\b.{0,30}(?<!project )\bsigns?\b|"
                            r"\bin sm rd sn\b|\bd-?20\b|\bsigns?\b\s*\(|\bsigns?\s*ty\b)"),
    # (round 20, .34) A sign named by its MUTCD code or its panel size and no install verb: "Yield Sign R1-2 36"x36""
    # (bids $740) and "Keep Right R4-7 30"x30"" were `other`, priced from the `other` 25-37 in EA cell at $9,391 (a
    # 30" pipe's price); "Traffic Sign" (bids $2,961) at $285.  Not a signal, a project / monument / kiosk sign,
    # signage, a push-button sign, a removal, reset, relocation or covering (their own families or rules), nor an
    # illuminated, LED, solar, flashing or message sign (electrical work, several times a sign panel's price).
    ("signing",             r"^(?!.*\b(?:signals?|project|monument|kiosk|signage|push\s?-?\s?buttons?|remov\w*|"
                            r"relocat\w*|reset\w*|salvag\w*|demolish\w*|cover\w*|bag\w*|illum\w*|lighted|lit|led|"
                            r"solar|flash\w*|electronic|dynamic|blank\s?-?\s?out|message|ilsn)\b)(?:"
                            r".*\b(?:r|w|cw|s|d|m|om)\d{1,2}\s+\d{1,2}[a-z]{0,2}\b(?=.*\b(?:signs?|\d+\s*in\s*x\s*\d+\s*in)\b)|"
                            r".*\bsigns?\b.{0,40}\b\d+\s*in\s*x\s*\d+\s*in\b|.*\b\d+\s*in\s*x\s*\d+\s*in\b.{0,40}\bsigns?\b|"
                            r".*\b(?:yield|stop|street\s+name|traffic|regulatory|warning|guide|speed\s+limit|"
                            r"no\s+parking|one\s+way|do\s+not\s+enter)\s+signs?\b)"),
    # A mow strip is the narrow concrete/riprap edging beside a guardrail, sign
    # or structure so a mower can pass.  It is NOT channel work, whatever it is
    # made of, and it is numerous: 2,381 archive rows at $550/CY.
    #
    # It must be caught BEFORE mowing_vegetation (whose \bmow\b swallows it) and
    # before the riprap rules.  Left alone it damages two families at once -- it
    # would inflate a channel-repair riprap median, and sitting in
    # mowing_vegetation it puts $550/CY concrete into a family otherwise priced
    # per acre of mowing.  It was landing in mowing_vegetation only by accident
    # of rule order, which is fragile: reorder the families and 2,381 rows of
    # edging flood into the channel basket.
    #
    # REMOVING a mow strip is demolition, and it has to be caught here, ahead
    # of conc_mow_strip: the removal rules all sit further down, so
    # conc_mow_strip intercepted "removing concrete mow strip" (TxDOT 104) and
    # "remove concrete mowstrip" before any of them could look.  Measured on
    # all 706,841 panel rows: 507 removal rows (460 reaching price_reference)
    # were filed as conc_mow_strip, and they WERE its LF and SY references --
    # conc_mow_strip|LF, usable=yes at $10.50/LF over 63 projects, is a
    # removal price ($10.00 and $10.97 at the two removals' medians) against
    # $23.50-165/LF for the 16 gated rows that build one; |SY's $18.20 is the
    # same ($20.00 removal against $350 placed).  Both multi-band DFW items
    # of conc_mow_strip|LF were removals too, so its "one item short of a
    # quantity shape" was a shape of demolition.  They go to demo_other, the
    # generic removal family (the ^remov rule's own target) -- including the
    # 14 city rows that also name a screening wall or sidewalk, which a
    # removal family prices far better than a placement one.  Exactly those
    # 507 rows move (by source of the 460 gated: txdot 185, txdot_archive
    # 264, city_bidtab 11; 0 statewide); no other row in the corpus changes
    # family.
    ("demo_other",          r"\b(remov\w*|dispos\w*|demolish\w*)\b.{0,60}mow ?strip|"
                            r"mow ?strip.{0,30}\b(remov\w*|dispos\w*|demolish\w*)\b"),
    ("conc_mow_strip",      r"mow ?strip|mow\s+strip"),
    ("mowing_vegetation",   r"\bmowing\b|\bmow\b|litter removal|sweeping|vegetation management|"
                            r"\bherbicide\b|\btrimming\b"),
    # Four trades that were landing in `other` wholesale.  All are highway work
    # rather than the client's channel repair, but leaving ~22,000 rows
    # unclassified made `other` useless as a measure of real classifier failure
    # -- and these are exactly the families that dilute a pooled price index.
    # Counts and medians are from the archive, which is what identified them:
    #   crash cush  4,117 rows  $2,500      drill shaft  6,237 rows  $300
    #   port ctb    6,990 rows  $17.00      veh sig      4,450 rows  $315
    # Painting an existing element is a coating trade, not the element:
    # "Repaint Pedestrian Rail" (Frisco Bayberry, 864 LF at about $10) was
    # fence and priced at $243/LF; "Repaint piping and fittings in Lift
    # Station No. 7 valve vault" was valve_hydrant (round 5).  (Re)paint must
    # be one of the first three words; salvage / reset / removal stay with
    # their rules, and striping / marking is pavement_marking, matched above.
    ("painting",            r"^(?!.*\b(?:salvag\w*|reset\w*|relocat\w*|re\s?install\w*|remov\w*|"
                            r"construct\w*|install\w*|furnish\w*|replac\w*|new|"
                            r"strip\w*|marking\w*|traffic|legend\w*|arrows?)\b)"
                            r"(?:\S+\s+){0,2}?(?:re\s?)?paint(?:ing|ed)?\b"),
    ("crash_cushion",       r"crash cush|attenuat"),
    ("traffic_signal",      r"veh sig|signal head|\bped sig\b|signal cable|"
                            r"traffic signal|detector loop|\bvivds\b|"
                            # (round 9) rectangular rapid flashing beacons ("Single Sided RRFB Assembly
                            # and Foundation", bids $10,000) were `other` at $250
                            r"^(?!.*\b(?:remov\w*|relocat\w*|salvag\w*|demolish\w*|reset\w*)\b).*"
                            r"(?:\brrfbs?\b|\brectangular\s+rapid\s+flash\w*)"),
    # (round 8, .29) TxDOT writes signal work in abbreviations no rule knew: "INS TRF SIG PL AM (S) 1
    # ARM (48) LUM" (signal pole assemblies, $10k-38k), "INSTALL HWY TRF SIG (ISOLATED)" ($36k), "TEMP
    # TRAF SIGNALS", "PED POLE ASSEMBLY", "BBU SYSTEM", "RVDS", "INSTALL RDSD FLSH BCN ASSM" -- about
    # 18,000 TxDOT rows sat in `other`, where they set the other|EA cells a municipal bench, bollard
    # or planter is priced from.  Pedestrian push buttons (1,500 rows) were pavement_marking by
    # their word "button", and signal backplates (~2,000 rows) `other`.  A drilled shaft or pole
    # foundation stays where it was, signal cable stays with the cables, and a removal, relocation or
    # salvage goes to its own rule.
    ("traffic_signal",      r"^(?!.*\b(?:drill\w*|dril\w*|fnd|foundations?|cbl|cables?|condr|conductors?)\b)"
                            r"(?!.*\b(?:remov\w*|relocat\w*|salvag\w*|demolish\w*|reset\w*)\b)"
                            r"(?:.*\b(?:ins(?:t|tl|tall)?|install\w*)\s+(?:hwy\s+|hy\s+)?tra?f\s+sig\w*|"
                            r".*\btra?f\s+sig\w*\s+(?:pl|poles?|assm\w*|assembl\w*|isolated|dpt|cnt|cab\w*)\b|"
                            r".*\btemp\w*\s+traf\w*\s+signals?\b|.*\bped\w*\s+(?:pole\s+assembl\w*|detect\w*\s+"
                            r"(?:push|controller))|.*\bpush\s?-?\s?buttons?\b|.*\bback\s?-?\s?plates?\b|"
                            r".*\bbbu\s+system|.*\brvds\b|.*\b(?:radar|video|rpd)\s+(?:\w+\s+){0,2}?detect\w*|"
                            r".*\bfl(?:a)?sh(?:ing)?\s+(?:bcn|beacon)s?\b)"),
    # (round 20, .34) Beacons and signal hardware the two rules above miss.  "Furnish & Install Solar Powered
    # Crosswalk Lights" (Aubrey 2024, 27 EA, bids $6,850 each), "Install Solar Powered Beacon Assembly", "Roadside
    # Flashing Beacon Assembly (Solar) and Foundation" (its foundation word kept the rule above off), "Crosswalk
    # Warning Flasher", "School Zone Flasher", "Flasher Cabinet", "Pedestrian Hybrid Beacons" were `other`, priced
    # from the `other` EA cells at $280-1,160; so were "3441-1615 Furnish/Install Type 45 Signal Pole" and
    # "Mast Arm 52'-60'".  A beacon needs a beacon word beside it (flashing, solar, RRFB, rapid, hybrid, school,
    # warning, assembly), so "Beacon Hill" or a "beacon entry monument" stays out; a signal pole or mast arm with a
    # drilled shaft or foundation word stays with the foundations, a sign on a mast arm with the signs, a luminaire mast
    # arm with the lighting; removal, relocation, reset and salvage keep their own rules.
    ("traffic_signal",      r"^(?!.*\b(?:remov\w*|relocat\w*|salvag\w*|demolish\w*|reset\w*|re\s?erect\w*|"
                            r"monuments?|lum\w*\s+(?:mast\s+)?arms?)\b)(?:"
                            r".*\b(?:flash\w*|flsh|solar|rrfbs?|rapid|hybrid|school|warning|hawk)\s+(?:\w+\s+){0,2}?beacons?\b|"
                            r".*\bbeacons?\s+(?:assembl\w*|assm\w*|system|solar|poles?)\b|"
                            r".*\bflashers?\b|"
                            r".*\bcrosswalks?\s+(?:warning\s+)?(?:lights?|lighting\s+system|beacons?|signals?|flashers?)\b|"
                            r"(?!.*\b(?:drill\w*|dril\w*|shafts?|fnd|foundations?|footings?|signs?)\b)"
                            r".*\b(?:signal\s+poles?|mast\s+arms?)\b)"),
    ("drill_shaft",         r"drill(ed)? shaft"),
    # (round 8, .29) "Concrete pier (Auger Cast Pile) & Cap", "Placement of Concrete Piers, including
    # drilling", "24-inch concrete pier foundation", "3000 PSI rein. concrete piers 24" dia" (VF) were
    # concrete_misc.  Not a pier cap, a precast / driven pile, a pile encasement, a wall or fence
    # built on piers, a repair or a removal.
    ("drill_shaft",         r"^(?!.*\b(?:remov\w*|demolish\w*|dispos\w*|abandon\w*|repair\w*|jacket\w*|walls?|"
                            r"fenc\w*|screen\w*|precast|prestress\w*|driven|encase\w*)\b)(?!.*\bpier\s+caps?\b)"
                            r".*(?:\bdrilled\s+piers?\b|\b(?:concrete|drilled|auger\w*\s+cast)\s+(?:piers?|piles?)\b|"
                            r"\bpier\s+foundations?\b|\bauger\w*\s*-?\s*cast\b)"),
    ("conc_barrier",        r"port(able)? ctb|\bctb\b|concrete traffic barrier|"
                            r"portable concrete barrier"),
    # (round 8, .29) "Low Profile Concrete Barrier Installation & Moving" (900 LF, bids $63/LF) was
    # concrete_misc at $273/LF.
    ("conc_barrier",        r"^(?!.*\b(?:remov\w*|demolish\w*|dispos\w*)\b)"
                            r".*(?:\blow\s+profile\s+(?:concrete\s+)?barriers?\b|\bconcrete\s+(?:traffic\s+)?barriers?\b(?!\s+free)|"
                            r"\b(?:precast|single\s+slope|safety\s+shape)\s+(?:concrete\s+)?barriers?\b)"),
    ("guardrail",           r"\bmbgf\b|guard\s*rail|guardrail|guard fence|metal beam guard|"
                            r"\bw.?beam\b|thrie.?beam|\bterminal anchor\b|anchor terminal|"
                            r"terminal section|rumble strip|"
                            r"\bcable barrier\b|concrete traffic barrier|\bcrash cushion\b"),
    ("shoring",             r"\bshoring\b|sheet pil|\bcofferdam\b|temporary support"),
    ("surveying",           r"\bsurvey|\bstaking\b|construction stakes?"),
    # TV inspection of a pipe is inspection, whatever pipe it names (round 5):
    # McKinney 26-0958 "STORM SEWER (TV) INSPECTION" (4,061 LF, $4.64/LF) was
    # storm_pipe and "WASTEWATER (TV) INSPECTION" other; "Pre- Construction
    # TV Inspection" other; "Video Inspection of Newly Installed Box Culverts
    # and Storm Drain Pipe" video_survey (a pre-construction site video).
    ("testing_inspection",  r"^(?!.*\b\d+\s*in\s+(?:\w+\s+){0,3}?(?:pvc|dip|hdpe|c900|c\s900|ductile|rcp|cpvc)\b"
                            r".*\b(?:testing|tested|cctv|television|tv)\b)"   # (round 8) not a pipe that includes its test
                            r".*?(?:\bvacuum test\b|\btesting\b|geotechnical|\binvestigation\b|"
                            r"\bcctv\b|televis|\bcoring\b|density test|"
                            r"\b(?:tv|cctv)\s+inspect\w*|\b(?:pre|post)\s?-?\s?tv\b|"
                            r"\binspect\w*\s+(?:by|with|via|using)\s+(?:cctv|tv|video|camera)\b|"
                            r"\bvideo\s+inspect\w*\b(?=.*\b(?:pipes?|sewers?|storm|culverts?|lines?|mains?|drains?)\b)|"
                            r"\bhydrostatic\s+test\w*)"),
    # CLEANING / DESILTING (round 6, .27; handoff/scratchpad/diag_ed/, 2026-11-20):
    # sediment, silt or debris taken OUT of a culvert, pipe, channel, inlet,
    # pond or other drainage structure is maintenance, priced per CY / LF / EA
    # / LS of what comes out -- it is neither the removal of the structure nor
    # the structure.  Mansfield Brinsap Channel (2026) "REMOVE SEDIMENT FROM BOX
    # CULVERT", 370 + 150 + 80 CY bid at $45-180/CY, was demo_utility (by
    # "remove ... culvert") and priced at $2,084/CY; TxDOT's "CLEAN EXISTING
    # CULVERTS" (961 rows) was `other`, "STORM SEWER CLEANING PIPE" and
    # "... CLEAN (BOX CULV)" storm_pipe ($10/LF in a pipe family), "CLEANING
    # DRAINAGE FLUMES" conc_beam and "CLEAR DEBRIS FROM AERIAL WASTEWATER LINE"
    # sanitary_pipe ($161k against $5k).  It needs a drainage noun (or desilt /
    # dredge), so roadside "DEBRIS REMOVAL (SPOT DEBRIS)" per mile stays where
    # it was.  Not: CCTV with cleaning (testing_inspection, above), erosion
    # control devices ("remove sediment control fence", inlet protection),
    # clearing and grubbing, painting, a cleanout, and a line that also
    # builds, resets, adjusts or repairs something.
    ("cleaning_desilting",  r"^(?!.*\b(?:install\w*|construct\w*|furnish\w*|replac\w*|reset\w*|adjust\w*|"
                            r"repair\w*|paint\w*|pnt|reseal\w*|seal\w*|cctv|televis\w*|video|inspect\w*|"
                            r"clearing|grubbing|grub|demolish\w*|demolition|survey\w*|fenc\w*|sweep\w*|main\s?lanes?|"
                            r"girders?|bents?|abutments?|bearings?|decks?|"
                            r"erosion|swppp|silt\s+fenc\w*|sediment\s+(?:control|barrier|log|fenc|trap)\w*|"
                            r"inlet\s+protect\w*|rock\s+filter)\b)"
                            r"(?:.*\b(?:de\s?-?\s?silt\w*|dredg\w*)\b|"
                            r".*\b(?:remov\w*|clean(?:ing|ed|s)?|clear(?:ing|ed|s)?|excavat\w*|haul\w*)\s+"
                            r"(?:and\s+dispos\w*\s+(?:of\s+)?)?(?:[\w/]+\s+){0,3}?"
                            r"(?:sediment\w*|silt\w*|debris|accumulat\w*|deposits?|drift)\b"
                            r"(?=.{0,60}\b" + _CLEAN_NOUN + r")|"
                            r".*\b" + _CLEAN_NOUN + r"\s+(?:[\w/]+\s+){0,2}?(?:clean(?:ing|ed|s)?\b(?!\s*outs?\b)|desilt\w*|"
                            r"(?:silt|sediment|debris)\w*(?:/\w+)?\s+(?:and\s+\w+\s+)?remov\w*)|"
                            r".*\bclean(?:ing|ed|s)?\s+(?:out\s+)?(?:and\s+de\s?-?\s?silt\w*\s+)?(?:of\s+)?(?:the\s+)?"
                            r"(?:[\w/]+\s+){0,5}?" + _CLEAN_NOUN + r"|"
                            # (round 11, .32) a drainage conveyance's CLEANOUT is its cleaning out, not a
                            # sewer cleanout fitting: "Roadside drainage ditch improvements and culvert
                            # cleanouts" (Josephine, 11,720 LF) was sewer_service by its "cleanout".  A
                            # pipe, line, drain or sewer cleanout stays the fitting (sewer_service).
                            r".*\b(?:culverts?|culv|ditch\w*|channels?|swales?|ponds?|basins?|creeks?|streams?|"
                            r"inlets?|flumes?|outfalls?|rcbc?s?|detention|retention)\s+clean\s?-?\s?outs?\b)"),
    # TRENCHLESS REPLACEMENT -- pipe bursting, pipe reaming / splitting,
    # sliplining -- is a method, not open-cut pipe (round 5).  Priced as open
    # cut, "6" to 8" bursting" came out at $34 against $130/LF, and Frisco
    # Holly Hawthorne's 8" HDPE by bursting at $110 against $273.  Nine of the
    # corpus' bursting lines were `other` ("PIPE BURSTING", "8-inch HDPE DR17
    # Sewer Pipe (Trenchless Method by Pipe Bursting)") and the rest water_pipe
    # or sanitary_pipe.  A service reconnection for a bursting job stays a
    # service (sewer_service, below).
    ("pipe_bursting",       r"^(?!.*\bservices?\b)(?!(?:\S+\s+){0,2}?(?:connect\w*|reconnect\w*|tie\s+in)\b)"
                            r"(?!.*\bslip\s?-?\s?lin\w*\s+(?:the\s+)?(?:existing\s+)?(?:\w+\s+){0,2}?manholes?\b)"
                            r".*\b(?:pipe\s?-?\s?burst\w*|burst(?:ing)?\s+(?:method|existing|of|the)\b|"
                            # (round 9) the verb before the pipe's size: "Burst 15 in CI to 16 in HDPE"
                            # (bids $182/LF) was `other` at $6
                            r"burst(?:ing)?\s+(?:ex\s+|existing\s+)?\d+(?:\.\d+)?\s*in\b|"
                            r"(?:by|via|using)\s+(?:static\s+|pneumatic\s+|hydraulic\s+)?burst\w*|"
                            r"slip\s?-?\s?lin\w*|pipe\s+ream\w*|pipe\s+split\w*)"),
    # A rolled erosion-control product that NAMES itself is filed by the name,
    # even when the tab calls it an "erosion control blanket".  erosion_control
    # below claims any "erosion control" text, so these never reached the trm
    # block that already lists the very same brands.  Measured over every
    # master_panel and line_items description: 13 rows move, all from
    # erosion_control SY, and nothing else does --
    #   "Erosion Control Blanket (Pyramat 75 or equivalent)"  8 -> trm_permanent
    #   "Erosion Control Blanket (Curlex [II] ...)" 4, and "Temporary Erosion
    #   Control Blanket ... North American Green C125BN" 1    5 -> trm_temporary
    # Only one of them (a Curlex letting, 3 rows) is inside the price_reference
    # gate.  An UNBRANDED "erosion control blanket / matting" stays in
    # erosion_control: its words name no product, and the note on
    # construction_exit below says why its price cannot decide it either.
    ("trm_permanent",       r"\berosion\s+control\s+(?:blankets?|mat\w*)\b.{0,80}"
                            r"\b(?:pyramat|landlok|p550|hp.?trm)\b"),
    ("trm_temporary",       r"\berosion\s+control\s+(?:blankets?|mat\w*)\b.{0,80}"
                            r"\b(?:c125\w*|s150\w*|curlex)\b"),
    ("erosion_control",     r"erosion control|swppp|silt fence|inlet protection|\bnoi\b|"
                            r"storm water pollution|pollution prevention|"
                            r"erosn cont|sedmt cont|rock filter dam|sediment control|silt fenc\w*|"
                            r"\bsedimentation\b|\bsoil retention\b|\bsoil\s+ret\w*\s+bl\w*|"
                            # (round 8, .29) "TxDOT 169 SOIL RET BLKT (SL_MOD_CLAY_SHORT)" (bids $0.77/SY)
                            # was `other` at $64.55/SY; "INSTALL CONCRETE WASHOUT PIT" (bids $10) was
                            # concrete_misc at $266.
                            r"\bconcrete\s+(?:truck\s+)?wash\s?-?\s?outs?\b|"
                            # Added from a MEASURED scan of the `other` family,
                            # not from guesswork.  The scan is worth recording
                            # because it mostly said NO: of the erosion-shaped
                            # vocabulary I suspected was being missed, `slope`
                            # matched 2 rows that are both "3:1 Slope" in pipe
                            # end-treatment descriptions, and `turf.*mat`
                            # matched 14 rows that are all "Artificial Turf
                            # (Playground Grass)" -- both correctly `other`.
                            # `gabion` and `riprap` missed nothing at all.
                            # Only these two shapes were real, 21 rows between
                            # them out of 21,214 in `other`.
                            #
                            # And one near miss worth keeping in view: a loose
                            # `armor` rule, which looked obviously right for
                            # revetment, would have swept up 6 rows of Collin
                            # County BALLISTIC VESTS and armor inserts. The
                            # acb_mat rule names the product (armor-flex) for
                            # that reason.
                            r"check\s*dam|rock\s*berm|"
                            r"(erosion|sediment).{0,20}\blogs?\b"),
    # Construction exits -- TxDOT Item 506, the municipal "stabilized
    # construction entrance" -- are one temporary BMP sold as TWO pay items,
    # and the two must not share a family.  Measured on master_panel.csv
    # (2026-11-18 20:24 build): erosion_control SY held 5,885 rows, 5,547
    # (94%) of them construction exits -- 2,781 install, 2,766 remove -- so
    # its published median, $16.50/SY over 424 projects, was the midpoint of
    # two prices and the price of neither.  Gated project medians:
    #     install   407 projects   $27.50   (p25-p75 $22.00-35.54)
    #     remove    406 projects   $10.00   (p25-p75  $8.47-12.46)
    # Within a letting (405 lettings carry both) install/remove is 2.70x at
    # the median and 2.00x at p10; install <= remove in 1 of 405.  The words
    # separate them and the quantity band does not: both run from 10 to
    # 25,000 SY.  That blend is also the family's whole expensive tail -- of
    # the 1,056 gated rows above $30/SY, 1,009 are installs, 46 removals,
    # 1 compost and 0 mats -- so the tail is NOT turf reinforcement mat.
    #
    # Removal is the pay item only when nothing is installed or maintained:
    # "construction entrance/exit including installation maintenance and
    # removal" (EA) is the whole life cycle and files as construction_exit.
    # Both rules sit AFTER erosion_control, so an SWPPP lump sum that lists an
    # exit among silt fence and rock filter dams ("joint swppp and erosion
    # control ... construction exit silt fence") stays erosion_control.
    # `entrance` is the municipal word for the same BMP: 34 rows of it were
    # going to subgrade_stab (by `stabilized`) and `other`.  Measured over
    # every panel and line_items description, the two rules move 5,607 panel
    # rows and each one is a construction exit or entrance; 0 company rows.
    #
    # What erosion_control SY keeps is soil retention blankets, compost and
    # UNBRANDED erosion control blankets / matting: 29 gated projects at
    # $2.00/SY (p25-p75 $1.58-2.65).  The unbranded mats are not split
    # further because nothing measurable decides them.  Of the 32 mat/blanket
    # rows in erosion_control SY, 13 name a product (routed above) and 19 do
    # not; those 19 come from 5 lettings, one of them gated (3 rows, $3-4/SY
    # at 2,000 SY).  Price cannot decide it: the dearest, Arlington PWDR22005
    # at $44-134/SY, are 48-282 SY lots, and that same letting prices
    # TEMPORARY SEEDING at $33.90-37.70/SY on its 48 SY site against
    # $3.35-4.40 on its 473-575 SY sites -- quantity, not product.  So such a
    # line now prices as a temporary blanket, which is an order of magnitude
    # low if it is really a permanent mat; the tab does not say which.
    ("construction_exit_remove",
                            r"^(?!.*\b(?:install\w*|furnish\w*|maint\w*|place\w*)\b)"
                            r"(?=.*\b(?:remov\w*|dispos\w*)\b)"
                            r".*\bconstruction\s+(?:exit|entrance)s?\b"),
    ("construction_exit",   r"\bconstruction\s+(?:exit|entrance)s?\b"),
    ("trench_safety",       r"trench safety"),
    ("dewatering",          r"control of water|dewater"),
    # BYPASS PUMPING (round 8, .29), a new family.  Temporary sewage / sewer bypass pumping, a bypass
    # system or plan, wastewater flow control: 56 lettings, LS $10k-1.5M, filed `other` (and
    # sanitary_pipe when it named the sewer), so it was priced from the catch-all LS cell.  Not: a
    # bypass VALVE or by-pass piping assembly, a line stop, a water-service bypass, a fence or access
    # for it, and a line that only includes the pumping in a pipe / manhole price (the head of the
    # description decides).
    ("bypass_pumping",      r"^(?!.*\b(?:valves?|assembl\w*|line\s+stops?|services?|encasement|embedment|fenc\w*|"
                            r"access)\b)"
                            r"(?:.*\bby\s?-?\s?pass(?:ing)?\s+(?:pump\w*|system|plans?|operations?)\b|"
                            r".*\b(?:sewer|sewage|wastewater|sanitary|temporary|temp)\s+(?:sewer\s+)?by\s?-?\s?pass\b|"
                            r".*\b(?:wastewater|sewer|sewage|sanitary)\s+flow\s+control\b|"
                            r".*\bflow\s+control\b(?=.*\b(?:wastewater|sewer|sewage|sanitary)\b))"),
    ("video_survey",        r"videotape|video"),
    ("project_sign",        r"project sign|signage"),
    # (round 6, .27) "Preparing Right-Of-Way" / "Right of Way Preparation"
    # spelt out (norm_desc: "right of way") was `other` -- 95 STA, 137 LS and
    # 17 AC city rows -- while TxDOT's "PREPARING ROW" was already here.  A
    # right-of-way line about trees stays with the tree rules.
    ("site_prep_clearing",  r"clearing|grubbing|row preparation|site preparation|stump removal|"
                            r"preparing row|\bprep row\b|"
                            r"^(?!.*\btrees?\b).*(?:\bprep\w*\s+(?:of\s+)?(?:and\s+\w+\s+)?(?:the\s+)?right\s+of\s+way\b|"
                            r"\bright\s+of\s+way\s+prep\w*|"
                            # (round 8, .29) "PREPARING R.O.W." reaches norm_desc as "preparing r o w"
                            r"\bprep\w*\s+(?:of\s+)?(?:the\s+)?r\s?o\s?w\b|\br\s?o\s?w\s+prep\w*)"),
    # `tree` needs its word boundary: unanchored it matched inside "sTREEt",
    # so "Remove Existing Concrete Street Pavement" was tree_removal and
    # "Furnish and install New Street Sign" tree_install -- 46 descriptions,
    # 351 rows (round 3b).  A replaced tree is a planted one.
    ("tree_removal",        r"\btree removal|(remov\w*).{0,20}\btrees?\b"),
    ("tree_install",        r"(install|plant|transplant|replac\w*).{0,20}\btrees?\b|\btree (well|planter|protection|replacement)"),
    # (round 8, .29) a tree named by its caliper or as a canopy / shade / ornamental tree is a planted
    # tree: "4-INCH MINIMUM CALIPER TREES", "LARGE CANOPY TREE (4" CALIPER)", "Canopy Trees" were
    # `other`; TxDOT's "PLANT MATERIAL (MIN 3" CAL)(B&B)" too.  Not mulch rings, protection, pruning.
    ("tree_install",        r"^(?!.*\b(?:remov\w*|dispos\w*|protect\w*|prun\w*|trim\w*|relocat\w*|mulch\w*|rings?)\b)"
                            r".*(?:\b(?:cal|caliper)\b.{0,25}\btrees?\b|\btrees?\b.{0,30}\b(?:cal|caliper)\b|"
                            r"\b(?:canopy|shade|ornamental|street)\s+trees?\b|\bplant\s+material\b.{0,20}\b(?:cal|caliper)\b)"),
    # LANDSCAPE PLANTING (round 8, .29), a new family: container plants, shrubs, groundcover plants,
    # ornamental grasses, perennials, annual / seasonal colour.  TxDOT's "PLANT MATERIAL (1 GAL)"
    # ($9), "(5 GAL)" ($29) and the streetscape plant lists ("FLAME ACANTHUS, 1 GAL", "Annual Color
    # - 4" pot", bids $4-15) were `other`, priced from catch-all EA cells at $33-377 each.  A gallon
    # that measures a liquid is no plant; trees keep tree_install (above); soil, bed preparation,
    # sod, seed, mulch and landscape rock stay where they were.
    ("landscape_material",  r"^(?!.*\b(?:remov\w*|demolish\w*|dispos\w*|salvag\w*|relocat\w*|re\s?install\w*|rip\s?rap|"
                            r"riprap|gabions?|bedding|embedment|backfill\w*|driveways?|drives?|roads?|streets?|parking|"
                            r"pavement|weep|drain\w*|filter|channel|walls?|veneer|fabric)\b)"
                            r".*(?:\briver\s+(?:rock|stone|cobble\w*|boulders?)\b|"
                            r"\b(?:black|eagle|tejas|pea|decorative|landscape|landscaping|colored)\s+(?:\w+\s+)?(?:gravel|rock|stone|"
                            r"cobbles?)\b|\btejas\b|\b(?:decomposed|crushed)\s+granite\b|\bboulders?\b|"
                            r"\bloose\s+aggr\w*\s+(?:for\s+)?groundcover\b|\brock\s+(?:bed|mulch)\b|\bcobbles?\b)"),
    ("landscape_planting",  r"^(?!.*\b(?:remov\w*|demolish\w*|dispos\w*|relocat\w*|transplant\w*|trees?|"
                            r"water\w*|irrigat\w*|fuel|diesel|oil|drums?|buckets?|tanks?|paint\w*|seal\w*|coat\w*|"
                            r"asphalt\w*|emuls\w*|chemical\w*|herbicide\w*|price|aggr\w*|granite|rock|stone|gravel|"
                            r"mulch\w*|sod\w*|seed\w*|fertiliz\w*|topsoil|soil|compost|prep\w*)\b)"
                            r"(?:.*\b(?:shrubs?|ground\s?covers?|ornamental\s+grass\w*|perennials?|annual\s+colou?r|"
                            r"seasonal\s+colou?r|bedding\s+plants?|liriope|lily\s?turf|muhly|sedges?|"
                            r"wintercreeper|germander|bluestem|feather\s?grass|lantana|salvia)\b|"
                            r".*\b\d+\s*(?:gal|gallons?)\b(?!\s*/)|.*\b\d+\s*in\s+(?:pots?|cntr|containers?)\b|"
                            r".*\bplant\s+material\b)"),
    # "remove and reinstall" puts the same item back: a reset, like "remove
    # and reset" (fire hydrants, signs, mailboxes, fences).
    # "Remove & Reset": norm_desc has already turned the & into a space, so
    # `remove (and|&) reset` never saw it (round 3b).
    # ADJUST TO GRADE (round 8, .29), a new family.  Adjusting an existing valve box, valve stack,
    # hydrant, manhole, meter box, cleanout, ground / pull box, lid or inlet top: "adjust water valve
    # stack" ($500), "adjust existing valve box to final grade" ($800), "valve box adjustment" ($880),
    # TxDOT "ADJUSTING MANHOLES" ($1,800) / "ADJUSTING INLETS" ($2,500) -- about 3,300 rows.  They were
    # split between relocate_reset (when the words "to grade" followed within 30 characters) and the
    # families of the things adjusted (valve_hydrant, manhole, inlet_structure, water_service,
    # sewer_service, electrical_lighting), where they sat in the same unsized EA cells as $5,000-12,000
    # fire hydrants and new manholes.  A vertical adjustment of a MAIN or line (a lowering) is pipe
    # work and stays; irrigation / sprinkler adjustments stay irrigation.
    ("adjust_to_grade",     r"^(?!.*\b(?:mains?|lines?|pipes?|services?|irrigat\w*|sprinkler\w*)\b)"
                            r"(?:.*\badjust\w*\b(?=.{0,50}\b(?:valves?|box(?:es)?|stacks?|hydrants?|manholes?|mhs?|"
                            r"cleanouts?|meters?|lids?|covers?|frames?|risers?|vaults?|inlets?|castings?)\b)|"
                            r".*\b(?:valves?|box(?:es)?|stacks?|hydrants?|manholes?|mh|cleanouts?|meters?|lids?|covers?|"
                            r"frames?|castings?)\s+(?:box\s+)?adjust\w*)"),
    ("relocate_reset",      r"remove\s+(?:and\s+|&\s*)?reset|relocate|adjust.{0,30}to grade|salvage|\bre\s?install\w*|"
                            # (round 11) "Reset Existing Driveway Culvert": a culvert or pipe reset
                            r"\breset\w*\s+(?:(?:the|existing|ex)\s+)?(?:\w+\s+){0,3}?(?:culverts?|culv|pipes?)\b"),
    # CONCRETE REPAIR (round 8, .29), a new family split out of concrete_misc: TxDOT "CONCRETE
    # STRUCTURE REPAIR (VERTICAL & OVERHEAD)" / "(DECK REP PART DEPTH)" ($150-275/SF, 2,000 rows),
    # concrete crack repair, bridge joint repair, epoxy mortar, foundation repairs.  They made the
    # concrete_misc|SF cell a $200+/SF repair price, at which a city's valley gutter (bids $24.50/SF)
    # or a washout pit ($10/SF) was priced.  A pavement, sidewalk, curb, gutter or driveway repair is
    # not this (its own family decides).
    ("concrete_repair",     r"^(?!.*\b(?:remov\w*|demolish\w*|pav\w*|sidewalks?|curbs?|gutters?|driveways?|streets?|"
                            r"alleys?|ramps?|flatwork|valley|hmac|asphalt\w*|overlay)\b)"
                            r"(?:.*\bconcrete\s+(?:structure\s+|struct\s+|bridge\s+|deck\s+)?(?:repair\w*|repr|rep)\b|"
                            r".*\b(?:structure|deck|spall\w*|surface)\s+(?:repair\w*|repr|rep)\b(?=.*\bconcrete\b)|"
                            r".*\b(?:crck|crack\w*)\s+(?:repair\w*|repr|injection)\b(?=.*\bconcrete\b)|"
                            r".*\bconcrete\s+(?:crck|crack\w*)\s+(?:repair\w*|repr)\b|"
                            r".*\bbridge\s+joint\s+(?:repair\w*|replacement)|.*\bepoxy\s+(?:mortar|injection)\b|"
                            r".*\brepair\s+concrete\s+(?:foundation|structure|column|cap|abut\w*|deck|surface)s?\b)"),
    # ---- pavement milling and thin surface applications (.24c) ------------
    # TxDOT's "PLANE ASPH CONC PAV (0" TO 2")" is planing, i.e. milling, at
    # about $2.50/SY.  It matched asphalt_pavement's `asphalt ... pav`, so
    # about half of TxDOT's per-SY asphalt rows (2,143 planing rows) shared
    # cells with HMA paving (municipal median $45/SY).  The estimator then
    # priced "Furnish and install new 2 in HMAC surface, Type D" (95,000 SY)
    # at $2.92/SY against a $13.34 low bid, and "Asphalt repair 4 in Type D
    # HMAC on 12 in flexible base" at $4.11 against $123.  Milling and the
    # thin applications (tack / prime coat, seal coats, microsurfacing, crack
    # sealing, surface treatments) get families of their own.  Each match
    # starts at the operative word, so the pavement-layer tie-break ("the
    # layer named first is the item", _PAVEMENT_LAYERS) still works: "6 in
    # flex base ... and prime coat" is flex base.  "Milled asphalt" is a
    # material (cement-treated milled asphalt base), not milling.
    # (round 9) milling that says it REMOVES the surface: "3" Mill and Remove" (see p_r9.py d2)
    ("demo_pavement",       _MILL_NOT + r"\bmill(?:ing)?\s+(?:(?:and|&)\s+)?remov\w*"),
    ("pavement_milling",    r"\b(?:planed?|planing|plan\s+text|plane\s+text|cold\s+plan\w*)\b"
                            r"(?=.{0,12}\b(?:asphalt\w*|asph|conc|concrete|pav\w*)\b)"
                            r"|^plan\s+(?=(?:asphalt\w*|asph|conc|concrete)\b)"   # (round 8) TxDOT PLAN ASPH CONC PAV
                            r"|^(?!.*\b(?:overlay|inlay|repave|resurfac\w*)\b)(?:\S+\s+){0,2}?cold\s+milling\b"
                            r"|" + _MILL_NOT + r"\b(?:cold\s+)?mill(?:ing)?\b" + _MILL_STREET +
                            r"(?=.{0,40}\b(?:asphalt\w*|asph|pav\w*|overlay|surface|hmac|concrete)\b)"
                            r"|" + _MILL_NOT + r"\b(?:asphalt\w*|asph|pav\w*|surface|hmac|concrete)\b"
                            r"(?=.{0,30}\bmill(?:ing)?\b" + _MILL_STREET + r")"
                            # (round 9) milling named with its haul-off, or after its depth ("2" Asphalt
                            # Milling"); "mill and REMOVE" is demo_pavement (the rule above this one)
                            r"|" + _MILL_NOT + r"\bmill(?:ing)?\s+(?:and|&)\s+(?:haul\w*|dispos\w*)\b"
                            r"|" + _MILL_NOT + r"(?<![\w.])\d+(?:\.\d+)?\s*in\s+(?:asphalt\s+|hmac\s+|surface\s+)?"
                            r"mill(?:ing)?\b" + _MILL_STREET),
    ("pavement_milling",    r"^(?!.*\b(?:remov\w*|replac\w*|new|construct\w*)\b)"
                            r".*(?:\b(?:diamond\s+)?grind\w*|\bgroov\w*|\btextur\w*|\brubbl?i[sz]\w*)\b"
                            r"(?=.{0,40}\bconcrete\s+pav\w*)"),   # (round 8) surface operations on concrete pavement
    ("surface_treatment",   r"\b(?:seal\s*coat\w*|chip\s*seal\w*|fog\s*seal\w*|slurry\s*seal\w*|scrub\s*seal\w*|"
                            r"cape\s*seal\w*|micro\s*surfac\w*|crack\s*(?:seal\w*|fill\w*|pour\w*)|rejuvenat\w*|"
                            r"tack\s*coat\w*|prime\s*coat\w*|surf\s+treat\w*|surface\s+treatment\w*|"
                            # (round 8, .29) "Rout and Seal Concrete Cracks" (bids $4/LF) was
                            # concrete_misc at $266/LF
                            r"rout\w*\s+(?:and\s+)?seal\w*|seal\w*\s+(?:\w+\s+){0,2}?cracks?|"
                            r"(?:surface|pavement)\s+preserv\w*|preserv\w*\s+tr\w*t\w*)\b"),
    # A SAWCUT that removes nothing is its own LF pay item (round 5):
    # Lewisville 25-4369 "#0-71Saw cut full depth concrete" (3,500 LF at about
    # $4) was concrete_misc and priced at $199/LF, "Saw cut full depth
    # concrete sidewalk" sidewalk, "For Sawcutting of Existing Pavement"
    # other.  The item must OPEN with the sawcut ("full / partial depth" may
    # come first) -- "VEH LP DETECT (SAWCUT)" and "POTHOLE REPAIR (SAW-CUT)"
    # are a detector and a patch; a line that also removes, disposes of,
    # replaces or builds is left to the rules below.
    ("sawcut",              r"^(?!.*\b(?:remov\w*|demolish\w*|demolition|dispos\w*|replac\w*|"
                            r"reconstruct\w*|install\w*|construct\w*|recycl\w*|furnish\w*|demo|rmve?)\b)"
                            r"(?:for\s+(?:the\s+)?)?(?:(?:full|partial)(?:\s+t\s?/?\s?4)?\s+depth\s+)?"
                            r"(?:saw\s?-?\s?cut\w*|sawing)\b"),
    ("demo_pavement",       r"(remov\w*|sawing|saw cut|demolish\w*|dispos\w*).{0,40}(pavement|paving|asphalt|drive|median)"),
    ("demo_sidewalk",       r"(remov\w*|sawing|dispos\w*).{0,40}(sidewalk|curb|walk)"),
    # (round 12, .33) a plug VALVE is new work: "plug" here is the verb of plugging an existing pipe or valve
    ("demo_utility",        r"(remov\w*|abandon\w*|plug\w*(?!\s+valves?\b)).{0,40}(pipe|line|main|manhole|inlet|valve|hydrant|culvert|cleanout|structure|encasement)"),
    ("demo_structure",      r"(remov\w*|demolish\w*).{0,40}(wall|fence|gate|abutment|foundation|steps|bollard|sign|mailbox|meter)"),
    # also "Sawcut, remove and dispose of ..." and "For removal and disposal
    # of ...": the verb is still the head of the item (round 3b).
    ("demo_other",          r"^(?:for\s+(?:the\s+)?)?(?:(?:full\s+depth\s+)?(?:saw\s?cut\w*|sawing)\s+(?:and\s+)?)?"
                            r"(remov\w*|abandon\w*|demolish\w*|dispos\w*)\b"),
    # Noun-first phrasing: Garland writes "TREES 6 IN AND LARGER REMOVE & DISPOSE"
    # and "ASPHALTIC PAVING REMOVE & DISPOSE", with the verb trailing the subject.
    # Without these, such items skip the demolition families and get classified
    # as new construction, so a removal item would be priced as new work.
    ("tree_removal",        r"\b(trees?|shrubs?|bushes)\b.{0,60}\b(remov\w*|dispos\w*)\b"),
    ("tree_install",        r"\btrees?\b.{0,30}\b(protect|protection|trim|trimming|prune|plant|install)\b"),
    ("demo_pavement",       r"(pavement|paving|asphalt|asphaltic|driveway|median)\b.{0,30}\b(remov\w*|dispos\w*|grinding)\b"),
    # (round 8, .29) TxDOT "OBLITERATING ABANDONED ROAD" and "SALV, HAUL & STKPL RCL ASPH PV" (bids
    # $2-35/SY) were `other`.
    ("demo_pavement",       r"\bobliterat\w*\b|\bsalv\w*\s+haul\w*\b"),
    ("demo_sidewalk",       r"(sidewalk|curb|walk)\b.{0,30}\b(remov\w*|dispos\w*)\b"),
    ("demo_utility",        r"(pipe|line|main|manhole|inlet|valve|hydrant|culvert|cleanout|structure)\b.{0,30}\b(remov\w*|abandon\w*|plug\w*|dispos\w*)\b"),
    # `walls?`, not `wall`: the \b after the noun rejected a plural, so
    # Garland's "HEADWALLS & WING WALLS REMOVE & DISPOSE" (3 rows) skipped
    # demolition and was filed as headwall PLACEMENT.  Measured over every
    # panel and line_items description, the plural moves exactly those 3
    # rows and nothing else.
    ("demo_structure",      r"(walls?|fence|fencing|gates?|abutment|foundation|steps|bollard|sign|mailbox|meter|building)\b.{0,30}\b(remov\w*|demolish\w*|dispos\w*)\b"),
    # Removal of a channel-repair structure, caught BEFORE the families that
    # place one.  Removal is priced nothing like placement -- riprap removal runs
    # $27/SY against $118-202/SY placed -- so a removal row must never sit in the
    # family of the thing being removed.  This has to precede conc_beam, whose
    # \bflumes?\b otherwise claims "removing concrete flume".
    # "remov\w*" because the source says removing / removal / removed, none of
    # which \bremove\b matches.
    ("demo_other",          r"(rip ?rap|gabion|mattress|turf reinforc|concrete beam|"
                            r"toe wall|flume)\b.{0,30}"
                            r"\b(remov\w*|dispos\w*|demolish\w*)\b|"
                            r"\b(remov\w*|dispos\w*|demolish\w*)\b.{0,30}"
                            r"(rip ?rap|gabion|mattress|turf reinforc|concrete beam|"
                            r"toe wall|flume)\b"),
    ("site_prep_clearing",  r"\b(clear|clearing)\b.{0,20}\bgrub"),
    # ---- structures and lighting (round 3b) -------------------------------
    # A grade-separation structure has no family of its own, so it is
    # declared `other` -- unpriced -- rather than filed by a word inside it:
    # "Concrete Under-Crossing Trail System (including slab, walls, structural
    # excavation and backfill ...)" (Lewisville 25-4310, $600-915/LF) was
    # borrow_backfill by its `backfill`, and its head word `trail` would make
    # it a sidewalk.  Only when the structure is the item itself: not a
    # removal, and not "... near / at / for the pedestrian bridge".
    # (round 12, .33) A trail's BOARDWALK or FISHING PIER is the same kind of structure: Frisco Cottonwood
    # Branch's "Trail Boardwalk Metal Grating 8 ft width" and "Trail Fishing Pier Metal Grating ..." (LF)
    # were sidewalk by their head word `trail` (lf_width_size already leaves them unsized).
    ("other",               r"^(?!.*\b(?:remov\w*|demolish\w*|demolition)\b)"
                            r"(?:(?!\b(?:at|near|to|from|along|beside|for|on)\b).)*?"
                            r"\b(?:under\s?crossings?|pedestrian\s+bridges?|truss\s+bridges?|bridge\s+spans?|"
                            r"boardwalks?|fishing\s+piers?)\b"),
    # A lighting ASSEMBLY -- pole, arm and luminaire, TxDOT 610 "IN RD IL",
    # a decorative post-top pole -- is its own pay item at about $5,000-8,000
    # per EA, against about $1,200 for the ground boxes, conduit and
    # conductors in electrical_lighting EA, so it gets its own family.  137
    # descriptions (2,667 rows, 222 lettings) were `other`, e.g. ROADWAY
    # LIGHTING ASSEMBLY (TY 1) and IN RD IL (TY SA) 40T-8 (250W EQ) LED.
    # Kept out on purpose: the FOUNDATION of a light pole (electrical, the
    # first rule), a pedestrian SIGNAL pole ("PED POLE ASSEMBLY" names no
    # light), a fixture FOR light poles, and inspection or maintenance of
    # one.  Removal and relocation stay with the rules above.
    ("electrical_lighting", r"\b(?:(?:street\s?)?light\w*|lum(?:inaires?|s)?|illum\w*)\b"
                            r"(?:\s+(?!and\b|with\b|w/|incl\w*|plus\b)\S+){0,3}?\s+"
                            r"(?:foundation|fdn|footing)s?\b"),
    ("lighting_assembly",   r"^(?!.*\b(?:inspect\w*|maint\w*|repair\w*|clean\w*|test\w*)\b)"
                            r"(?:(?!\bfor\b).)*?"
                            r"(?:\b(?:\w*light(?:s|ing)?|lum(?:inaires?|s)?|illum\w*)\b"
                            r"(?:\s+(?!for\b)[a-z0-9/]+){0,2}?\s+(?:poles?|standards?|assembl\w*|assy|assm|asm)\b|"
                            r"\bpole\s+lights?\b|\bin\s+rd\s+il\b|"
                            r"\b(?:rd|rdwy|roadway)\s+(?:il|ill|illum\w*)\s+(?:assm?|asm|assy|assembl\w*)\b|"
                            r"\bhi(?:gh)?\s?m(?:a)?st\b.{0,20}\b(?:poles?|assembl\w*|assm|asm)\b)"),
    # Crash cushions and guardrail end treatments named only by their SYSTEM
    # (round 3b): TxDOT maintenance contracts write "REPAIR (TRACC) (BAY)",
    # "REMOVE AND REPLACE (SMTC) (N)", "REPAIR REACT (N) (CYLINDERS)",
    # QUADGUARD, TAU-II, BEAT-SSCC, CATGR, and "REPLACE SGT STRUT", "REMOVE &
    # REPLACE BLOCKOUT", X-LITE, DAT -- never "crash cushion" or "guardrail",
    # so about 120 descriptions (5,400 rows) were `other`, or demolition when
    # replaced.  Placed AFTER the removal rules: "REMOVE (NARROW TRACC)" and
    # "Removing SGT" are still demolition.
    ("crash_cushion",       r"\b(?:(?:wide\s+|short|fast)?tracc|shortracc|fastracc|quad\s?guard\w*|"
                            r"quad\s*-?\s*elite|narrow\s+quad|react(?:\s?350)?|tau(?:\s?ii)?|smtc|"
                            r"sci\s?100\w*|beat\s*-?\s*sscc|sscc|catgr)\b"),
    ("guardrail",           r"\bsgt\b|\bdat\b|\bx\s?-?\s?lite\b|(?<!concrete )\bblockouts?\b"),
    # ---- erosion / structural specialty (your highest-dollar work) ------
    # Permanent and temporary turf reinforcement mats are ~3x apart in price and
    # are different products: an anchored synthetic mat is permanent armouring,
    # a biodegradable mat is a temporary cover that rots away once vegetation
    # establishes.  Measured in ONE bid tab -- same project, same bidders, same
    # day, so nothing else varies (arlington/PWDR19010C Kee Branch Trib 1):
    #   318  Biodegradable TRM (C125BN)   350 SY   $12.00 / $18.60
    #   319  Anchored TRM (Pyramat 75-PDS) 250 SY  $40.00 / $43.20
    # Pooling them puts a 3x spread inside one median, the same defect the
    # riprap split fixed.  Product names are included because the spec sheet
    # often names the product instead of saying "permanent".
    # A TRM bundled with the seeding or sod it covers is a different pay item
    # from the mat alone, and this is checked FIRST because bundling destroys
    # comparability outright -- more decisively than the permanent/temporary
    # distinction does.  Two bundled items at nearly the same quantity:
    #   Hickory Creek  TRM + hydromulch  1,208 SY  median $9.54
    #   PWDR17011B     TRM + sod         1,040 SY         $29.00
    # 3x apart with quantity held almost constant, so the variable is WHAT IS
    # IN THE BUNDLE, not the mat.  Bundled prices cannot be compared with each
    # other, let alone with a bare mat at $40-60/SY.
    # An anchored reinforced vegetation system (ArmorMax and the like) is a mat
    # PLUS an engineered anchor array, and owners price it as its own pay item:
    # in 2022-014-6929 Wildlife Pkwy it is $35.00/$35.32 per SY against $24.00/
    # $24.09 for HIGH PERFORMANCE TRM on the same tab, same bidders, same day.
    # Checked before the trm rules so it cannot be absorbed by them, and it was
    # previously falling through to `other`.
    ("arv_system",          r"anchor\w*\s+reinforc\w*\s+vegetation|armormax|"
                            r"\barv\s*system\b"),
    ("trm_bundled",         r"turf reinforc.{0,30}(with|w/|and)\s*(sod|seed|hydromulch)|"
                            r"(sod|seed\w*|hydromulch).{0,30}(with|w/|and)?\s*"
                            r".{0,10}turf reinforc"),
    # "HIGH PERFORMANCE" is an industry category, not a vague adjective: HP-TRM
    # means a permanent anchored synthetic mat by definition.  It was being
    # missed because the keyword sits BEFORE "turf reinforc" while the first
    # alternative only looks after it.
    ("trm_permanent",       r"turf reinforc.{0,40}(anchor|permanent|\bhp\b|high.?perf)|"
                            r"(anchor|permanent|high.?perf\w*).{0,40}turf reinforc|"
                            r"\bhp.?trm\b|pyramat|landlok|\bp550\b|\bpds\b"),
    ("trm_temporary",       r"turf reinforc.{0,40}(biodegrad|temporary|coir|straw|jute)|"
                            r"(biodegrad|temporary).{0,40}turf reinforc|"
                            r"\bc125\w*|\bs150\w*|curlex"),
    # Unqualified "turf reinforcement mat" -- the tab did not say which.  Kept
    # separate rather than folded into either, because guessing would put a
    # $12 product and a $40 product in whichever bucket happened to be chosen.
    ("trm",                 r"turf reinforc"),
    # ---- "X for Y" is a pay item for X, not for Y ------------------------
    # This MUST precede every material family below it.  "GROUT FOR GABION
    # MATTRESS" is the grout alone -- no basket, no stone -- and it was being
    # claimed by `gabion_mattress` simply because that rule came first, which
    # put a grout price inside a gabion mattress distribution.  It sat four
    # positions too late: acb_mat/gabion_mattress/gabion_wall/conc_beam all
    # matched ahead of it, while riprap_conc/riprap_stone happened to come
    # after, so "grout for riprap" was already landing here correctly and the
    # gabion spelling of the same item was not.  Order, not vocabulary, was
    # the whole defect.
    #
    # Measured on all 706,841 panel rows, this placement moves exactly 10 of
    # them, all one description (grout for a gabion mattress), gabion_mattress
    # -> grout_additive.  Nothing else in the corpus changes family.  All 10
    # come from one company file (source_set internal), so build_master's train
    # gate had already kept every one of them out of price_reference: no
    # published gabion_mattress cell moves.  The damage was on the TEST side --
    # that line was looked up, and scored, as a gabion mattress.
    #
    # Anchored on "grout FOR", never on `grout`, and this is the point of the
    # rule.  A bare \bgrout\w* would also claim the 5 gated rows reading
    # "12 in grouted gabion mattress" -- which ARE gabion mattresses, grouted,
    # and are one of only 2 projects in gabion_mattress CY/9-13 in -- and the
    # 498 riprap_stone rows (460 gated) of TxDOT's "RIPRAP (STONE COMMON)
    # (GROUT)(12 IN)", which are grouted stone riprap, i.e. the riprap itself.
    # 86 panel rows say "grouted"; 46 of them are grouted rock riprap, a
    # documented riprap sub-type, and they must stay in riprap_stone.
    # `grout seal` / `seal grout` name the grout as the pay item the same way
    # "grout for" does: "grout seal bottom slab of special inlet to box
    # culvert" (6 rows, all gated) was an inlet_structure EA price and "wipe
    # seal grout existing 66/78 in rcp pipe joints" (4 rows) a storm_pipe one.
    #
    # Grout that FILLS AN ABANDONED PIPE is the same trap one level down: the
    # pay item is the grout, the pipe is only what it is for.  The
    # demo_utility rule below claims it, because that is where the corpus
    # already files the identical item ("grout water main to be abandoned",
    # "grout abandon existing sanitary sewer line", "grout for pipeline
    # abandonment"); these escaped because demo_utility's noun list has no
    # `sewer`/`drain` (or the tab abbreviates or misspells: "s s", "box
    # culv", "abondoned"), and "grouting" is not a removal verb.  Measured on
    # the panel (gated = rows that reach price_reference):
    #   "water line grouting"                water_pipe     40 rows, 35 gated
    #   "grout abandonment of 6/15 in
    #    sanitary sewer"                     sanitary_pipe  12 rows, 12 gated
    #   "(plug) grout and abandon .. storm
    #    drain", "flowable fill for storm
    #    drain abandonment"                  storm_pipe      8 rows,  0 gated
    #   the same item, unrecognised          other          44 rows, 44 gated
    # The first line was ALL 35 bid obs of the published water_pipe CY cell
    # ($596.46 over 4 TxDOT lettings, `thin`) -- that cell priced grout, not
    # pipe, and it disappears.  3 of those 4 lettings carry a WATER
    # ABANDONMENT PLUG or ABANDON WATER LINE item beside the grouting and the
    # 4th a 48 in PRESSURE PLUG, so it is abandonment fill.  sanitary_pipe LF
    # 13-19 in moves $202.50 -> $226.25 without its $27/LF abandonment
    # project; demo_utility CY moves $455 -> $470 (7 -> 11 projects), i.e.
    # the grout lands among its own prices.
    # It stays AFTER the demo rules so nothing they already claim can move.
    # (An earlier note here said demo_utility already claimed "plug grout and
    # abandon 36 in existing storm drain".  It did not: no `drain` in its noun
    # list, so those rows were storm_pipe.)
    ("demo_utility",        r"\bgrout\w*\b.{0,40}\bab[ao]nd[ao]n\w*|"
                            r"\bab[ao]nd[ao]n\w*\b.{0,60}\bgrout\w*|"
                            r"\bflowable\s+fill\b.{0,40}\bab[ao]nd[ao]n\w*|"
                            r"\b(water|sewer|sanitary|wastewater|storm)\s+"
                            r"(line|main|sewer|drain)\s+grouting\b"),
    # (round 12, .33) ABANDONMENT of an existing pipe, sewer, water line or service -- a plug or cap left in
    # it, grout or flowable fill in it -- is demo_utility, where the corpus files the rest of it.  The noun
    # lists above stop at pipe / line / main / manhole, so "8" Sanitary Sewer Abandonment Plug" and "Cut, Plug
    # & Abandon Existing Sanitary Sewer (Size Varies)" were sanitary_pipe (priced as new sewer), "Plug Existing
    # Storm Drain" / "Cut & Plug Ex. Storm Sewer" storm_pipe, "Water Service Abandonment" and "Cut and Plug
    # Existing PVC Sanitary Sewer Service" water_service / sewer_service, "Grout Existing 8" PVC Sanitary Sewer
    # Main" sanitary_pipe, and "Cut & Plug Ex. 12" W.L.", "16" WATER ABANDONMENT PLUG", "PLUG AND ABANDON SAN
    # SEWER 6IN" `other`.  Not abandonment, so left where they were: a plug VALVE; a plug "for future
    # connection" and a new pipe's own end plug ("Furnish & Install 42" RCP Plug", "12" Plug" -- nothing
    # existing is named); a plug set IN an existing structure; a plant plug; grout in a joint, crack or void
    # (a repair); and a line that installs
    # something new and abandons the old one on the side ("Install 1" Domestic Water Service ... Abandon Ex.").
    ("demo_utility",        r"^(?!.*\bplug\s+valves?\b)(?!.*\bfuture\b)"
                            r"(?!.*\b(?:joints?|repair\w*|seal\w*|voids?|cracks?)\b)(?:"
                            r".*\babandonment\s+(?:plugs?|caps?)\b|"
                            r".*\bab[ao]nd[ao]n\w*\b(?=.{0,60}\b" + _UTIL_NOUN + r"\b)|"
                            r"(?!.*\b(?:install\w*|furnish\w*|construct\w*|new)\b)"
                            r".*\b" + _UTIL_NOUN + r"\b.{0,40}\bab[ao]nd[ao]n\w*|"
                            r".*\b(?:cut\s+(?:and\s+|&\s*)?)?(?:cap(?:s|ped|ping)?|plug(?:s|ged|ging)?|"
                            r"grout(?:s|ed|ing)?)(?:\s+(?:and\s+|&\s*)?(?:cap(?:s|ped|ping)?|plug(?:s|ged|ging)?|"
                            r"ab[ao]nd[ao]n\w*|grout(?:s|ed|ing)?))*\s+(?:the\s+)?(?:existing|exist|ex)\b"
                            r"(?=.{0,40}\b" + _UTIL_NOUN + r"\b)|"
                            r".*\bflowable\s+fill\s+(?:in|into)\s+(?:the\s+)?(?:existing|exist|ex)\b"
                            r"(?=.{0,40}\b" + _UTIL_NOUN + r"\b))"),
    ("grout_additive",      r"\bgrout(ing)?\s+for\b|\bmortar\s+for\b|"
                            r"\bgrout\s+seal\w*\b|\bseal\s+grout\b"),
    # Bedding and filler carry the same "X for Y" shape but are NOT grout, so
    # they no longer ride the grout rule (which an earlier version did): a
    # granular bedding layer pooled into grout_additive CY would be the same
    # defect this block exists to remove.  There is no bedding family -- the
    # corpus files "bedding material 6/9 in" (37 rows, 33 gated) as `other`
    # -- so they are declared `other` on purpose: unpriced, rather than priced
    # as the material they sit under.  Measured: `bedding for` 0 rows,
    # `filler for` 0, and "riprap bedding" 5 rows (3 gated) that were inside
    # riprap_stone CY at a $195/CY project median; riprap_stone CY with no
    # size moves $462.50 -> $481.25 (13 -> 12 projects) without them.
    # "rock riprap with filter fabric gravel bedding" is riprap WITH its
    # bedding, and stays riprap_stone -- the pattern needs "riprap bedding".
    # Pipe bedding has no such case: every one of the 81 pipe/utility-family
    # rows naming embedment is a pipe or valve priced "including embedment".
    ("other",               r"\bbedding\s+for\b|\bfiller?\s+for\b|"
                            r"\brip\s?rap\s+bedding\b"),
    # Select fill FOR a structure is fill.  "install compacted earth slope
    # fill, select fill to be used as necessary for slope repair for
    # sidewalks, flumes, concrete walls and channel work" (Grand Prairie Dry
    # Branch, 4 rows, all gated) was conc_beam CY -- a client core family --
    # because conc_beam's \bflumes?\b is tested before borrow_backfill's
    # `select fill`.  conc_beam CY moves $672.60 -> $675.00 (112 -> 111
    # projects).  Guarded so the fill must come BEFORE any "including"/"with":
    # a bare `select fill` here would pull "construct gabion wall and all
    # subsidiary items including excavation select fill crushed stone" (12
    # rows) out of gabion_wall, where it belongs.  0 other rows move.
    ("borrow_backfill",     r"^(?:(?!\bincl\w*|\bwith\b|\bw/).)*?"
                            r"\bselect\s+fill\b.{0,60}\bfor\b"),
    # These two MUST precede `gabion`. "Gabion Basket Concrete Beams" is a
    # concrete beam inside a gabion wall system, priced per CY of CONCRETE
    # (~$848/CY) against ~$349/CY for the basket wall itself -- filed as gabion
    # it would inflate that distribution about 2.4x. Same for tieback anchors,
    # which are priced per LF of anchor, not per CY of basket.
    ("conc_beam",           r"(?<!prestr )(?<!prestressed )concrete beams?\b"),   # (round 8) not a bridge beam
    # `ground anchor` is the term the Denton County tabulation uses
    # (PERMANENT GROUND ANCHORS, 45 EA, four bidders) and it is standard
    # geotechnical usage for the same element as a rock anchor or soil nail.
    # It is the only PUBLIC source of rock_anchor prices outside TxDOT in the
    # corpus, and it was landing in `other`.  Anchored on the pair of words:
    # a bare `anchor` would sweep in anchor bolts and pipe anchors.
    # (round 6, .27) plural / spaced tie backs: Farmers Branch R2025-070's
    # "STEEL TIE BACKS" (245 EA at $600-2,500) was `other` at $63.
    ("rock_anchor",         r"rock anchor|soil nail|\brock\s+nails?\b|\btieback\b|tie.?back anchor|"
                            r"\btie\s?-?\s?backs?\b(?!\s+(?:beams?|walls?))|"
                            r"(ground|rock|soil|earth)\s+anchors?\b|"
                            r"anchors?\s+\(?(rock|soil|ground)"),
    # Mattress and basket wall are DIFFERENT STRUCTURES, not two ways of
    # measuring one item -- a mattress is a thin revetment laid on a bed or
    # slope, a basket wall is a stacked vertical retaining structure -- so they
    # must not share a family, and a SY series must never be converted into a
    # CY one by assuming a thickness.
    #
    # The product is told by the DESCRIPTION ONLY.  The unit carries no product
    # information, which is counter-intuitive and was wrong in an earlier
    # version of this comment.  Counter-example inside ONE tab
    # (arlington/PWDR23012_2023_Misc_Stormwater_Construction.pdf):
    #   PVC Coated Gabion Blocks      20 SY     <- "wall-ish" product, by AREA
    #   PVC Coated Gabion Mattress    40 CY     <- mattress, by VOLUME
    #   PVC Coated Gabion Structure    9 CY
    #   18"-24" Rock RipRap           90 SY  }  the SAME item name, both units,
    #   18"-24" Rock RipRap           27 CY  }  in the same document
    # So units are each owner's/designer's convention.  (family, unit) is still
    # the right GROUPING key -- $/SY cannot be pooled with $/CY -- but the unit
    # must never be read as evidence of which product or thickness it is.
    #
    # Mattress is tested first because "Gabion Mattresses" contains no
    # basket/wall keyword and would otherwise fall through to the rule below.
    # Articulated concrete block revetment -- a distinct erosion product, not a
    # kind of gabion and not "miscellaneous concrete".  Tested before gabion
    # because some tabs write "ACB mattress".  Brand names are in the pattern on
    # purpose: in this corpus the product is more often named by brand than by
    # description, and `ARMOR-FLEX-MAT` landed in `other` -- invisible -- while
    # `Articulated Concrete Block Mat` landed in `concrete_misc`, pooled with
    # sidewalk and flatwork.  Measured instance: 8 SY at a median $290/SY over
    # a $90.46-440 range, which is small-quantity territory and must not be
    # read as a general ACB price.
    ("acb_mat",             r"armor[\s-]?flex|armorflex|a[\s-]?jacks|petraflex|"
                            r"articulated\s+conc\w*\s+block|\bacb\b.{0,20}"
                            r"(mat|block|revet)|cable.?(tied|concrete).{0,20}"
                            r"(mat|block)"),
    ("gabion_mattress",     r"gabion.{0,30}mattress|mattress.{0,30}gabion|"
                            r"gabion.{0,20}revetment"),
    # Catch-all for every other gabion product: basket wall, blocks, structure,
    # baskets.  The name says "wall" for continuity with existing outputs, but
    # it is NOT all walls -- do not report this series as a wall price without
    # first checking the descriptions behind it.
    ("gabion_wall",         r"gabion"),
    # Municipal bid tabs do not say "beam" for a channel grade-control element.
    # The local names are toe wall, cut-off wall, grade control structure,
    # flume and riprap chute -- which is why searching for "concrete beam"
    # found nothing outside our own projects.
    ("conc_beam",           r"(?<!prestr )(?<!prestressed )concrete beams?\b|\btoe walls?\b|cut.?off walls?\b|"
                            r"grade control|\bflumes?\b|riprap chutes?|"
                            r"\bchannel (lining|liner|invert|bottom)\b"),
    # "Riprap" is three different pay items sharing one word, plus two
    # contaminants.  Measured on the 364 riprap rows actually in hand:
    #
    #   concrete riprap        CY  n=62  $624.00      <- 2.4x the stone prices
    #   grouted stone riprap   CY  n=35  $230.00
    #   dry / ungrouted stone  CY  n=14  $252.00
    #   stone, grouting unsaid CY  n=33  $267.44
    #   -- contaminants --
    #   riprap REMOVAL         SY  n=16  $ 30.50      <- demolition, not placement
    #   "grout for riprap"     CY  n=20  $443.50      <- the grout alone, no stone
    #
    # Pooled, the family returns $336/CY, which is wrong by about 46% in both
    # directions.  So concrete and stone must split.  Grouted vs dry stone
    # deliberately do NOT split: they sit within 16% of each other while
    # concrete sits 2.4x away, and three families of n=14-35 would make every
    # median noisier for no gain.  (Grouted reading cheaper than dry is not a
    # paradox -- where "grout for riprap" is its own pay item, the riprap line
    # covers only the stone.)  Thickness stays a feature in size_in, not a
    # family: it is a weaker axis than material.
    # `grout_additive` used to be declared HERE, which is why "grout for
    # riprap" worked and "grout for gabion mattress" did not -- this position
    # is ahead of the two riprap rules but behind gabion.  The rule now lives
    # above the whole material block; see the comment on it there.  The
    # "grout for riprap" contaminant measured in the table above is still
    # taken out of riprap_stone, by that rule.
    ("riprap_conc",         r"(rip ?rap|rock rip).{0,40}(concrete|conc\b)|"
                            r"(concrete|conc)\b.{0,40}(rip ?rap|rock rip)"),
    ("riprap_stone",        r"rip ?rap|rock rip"),
    ("retaining_wall",      r"retaining wall|modular block|cantilever"),
    # ---- paving ---------------------------------------------------------
    # Unit pavers (brick, concrete, stone) are hardscape paving, priced per
    # SF of paver field -- not masonry fascia, not miscellaneous concrete,
    # and never a surety bond (round 5; see bonds_insurance above).
    ("pavers",              r"\bpavers?\b|\bpavestones?\b|\bpaving\s+(?:stones?|bricks?|units?)\b|"
                            r"\bbrick\s+pav(?:ers?|ing)\b|\bunit\s+pav(?:ers?|ing)\b"),
    # BACKFILL SPLIT (round 5).  borrow_backfill held three different things:
    # cementitious backfill (flowable fill, CLSM, cement-stabilized backfill /
    # sand, TxDOT Item 401 FLOWABLE BACKFILL) at roughly $100-300/CY, soil
    # backfill / select fill at tens of dollars, and BORROW, earthwork at a
    # few dollars a CY.  Its CY cell came out at the flowable price: McAllen
    # Boeye's 663,848 CY "BORROW SITE EXCAVATION" (low bid $2.10/CY) was priced
    # at $314/CY, and Frisco Bayberry's wall backfill at $189 against $30.
    # Cementitious backfill now has its own family, borrow is embankment
    # earthwork (embankment_fill), and borrow_backfill keeps soil backfill and
    # select fill.  "Stabilized backfill" was subgrade_stab by its
    # `stabilized`; it is cement-stabilized backfill, not a stabilized subgrade.
    ("flowable_backfill",   r"^(?!.*\b(?:encase\w*|subgrade)\b)(?:.*?)(?:\bflowable\b|\bclsm\b|\bcontrolled\s+low\s+strength\b|"
                            r"\bcement\w*\s+(?:stabili[sz]ed|treated)\s+(?:sand|backfill|bkfl|fill|select\s+fill)\b|"
                            r"\bstabili[sz]ed\s+(?:sand\s+)?backfill\b|\bcement\s+sand\s+(?:backfill|slurry)\b|"
                            r"\bslurry\s+(?:back)?fill\b)"),
    # GEOTEXTILE (round 8, .29), a new family: filter / paving / weed-barrier fabric, geotextile,
    # poly moisture or vapour barrier sheeting -- a sheet laid under something, $1-5/SY.  "Paving
    # Fabric (Petromat)" (76,000 SY, bids $5), "HEAVY DUTY WEED BARRIER FILTER FABRIC" ($2.87),
    # "POLYSHEETING MOISTURE BARRIER" ($2) were `other` and priced from the catch-all SY cells at
    # $65-94/SY.  The fabric must be what the line is: not a riprap / gabion / rock / gravel / fence
    # line laid on or with fabric, nor fabric named after with / including / and.  Geogrid stays
    # subgrade_stab.
    ("geotextile",          r"^(?!.*\b(?:rip\s?rap|riprap|gabions?|mattress\w*|rock|stone|gravel|boulders?|granite|"
                            r"fence|fencing)\b)"
                            r"(?:(?!\b(?:with|w/|incl\w*|and|on|over|plus|under)\b).)*?"
                            r"\b(?:(?:filter|paving|geotextile|geo\s?textile|landscape|weed|stabiliz\w*|separation|"
                            r"drainage|woven|non\s?-?\s?woven)\s+fabric|geo\s?-?\s?text\w*|geo\s?fabric|petromat|"
                            r"weed\s+(?:barrier|block\w*)|(?:moisture|vapou?r)\s+(?:barrier|retarder)|"
                            r"poly\s?-?\s?sheet\w*|polyethylene\s+sheet\w*|visqueen|\d+\s+mil\s+(?:poly\w*|plastic))\b"),
    ("subgrade_stab",       r"\bsubgrade\b|stabilized|\blime\b|cement.{0,20}treat|geogrid|"
                            # (round 9) "Microcracking Pavement Base" (bids $0.85-1.03/SY, a roller pass
                            # over a cement-treated base) was `other` at $72/SY
                            r"\bmicro\s?-?\s?crack\w*|"
                            # (round 8, .29) full-depth reclamation, pulverizing / pulvermix, scarifying
                            # and remixing or reworking the existing base, cement treatment ("CEMENT TRT"),
                            # cement slurry stabilization and cement bought for the mixing: $3-30/SY
                            # (cement $200-340/TON), all `other` or asphalt_pavement, priced at $78-190/SY.
                            r"\bfull\s+depth\s+(?:asphalt\s+)?reclam\w*|\b(?:asphalt|pavement|roadway|road|street|base|"
                            r"treated)\s+reclam\w*|\breclam\w*\s+(?:of\s+)?(?:the\s+)?(?:existing\s+)?(?:asphalt|"
                            r"pavement|roadway|road|street|base)\b|\bfdr\b|\bpulver\w*|\bpulvi\w*|"
                            r"\bscarif\w*\b(?=.{0,60}\b(?:base|remix\w*|asphalt|subgrade|cement)\b)|"
                            r"\bremix\w*\b(?=.{0,40}\b(?:asphalt|base)\b)|\brework\w*\s+(?:and\s+scarif\w*\s+)?"
                            r"(?:the\s+)?(?:existing\s+)?(?:bs|base)\b|\bcement\s+trt\b|\bcement\s+slurry\s+stabil\w*|"
                            r"\bcement\s+for\s+(?:the\s+)?(?:mixing|stabil\w*)|^cement$"),
    ("embankment_fill",     r"\bborrow\b"),
    ("borrow_backfill",     r"\bbackfill\b|cement\s*stabilized\s*sand|select fill"),
    # TxDOT abbreviates flexible base "FL BS": "FL BS (CMP IN PLC)(TY D GR
    # 1-2)" -- compacted in place -- was storm_pipe by its `cmp` (60
    # descriptions, 2,402 rows) and "FL BS (RDWY DEL)" `other`; municipal
    # tabs write "Flexbase" as one word (round 3b).
    ("flex_base",           r"flex(ible)? base|\bflexbase\b|\bfl\s?bs\b|crushed stone base|"
                            # (round 8, .29) "8" CRUSHED CONCRETE" (bids $24.50/SY), "3" CRUSHED CONC."
                            # ($99/CY), "INSTALL UNIFORMLY 6" CRUSHED STONE" ($22/SY), "CRUSHED ROCK FOR
                            # PAVING REPAIR" were concrete_misc / `other` at $116-1,004.  The crushed
                            # material must be what the line places (not "... outside of crushed stone"),
                            # and not bedding, embedment, backfill, drains, filters, riprap, gabions or
                            # landscape rock.
                            r"^(?!.*\b(?:bedding|embedment|backfill\w*|bkfl|drain\w*|filter|gabions?|rip\s?rap|riprap|"
                            r"landscap\w*|decorative|river|boulders?|mulch|planting|beds?|pond|basket\w*)\b)"
                            r"(?!.*\b\d+(?:\s+\d+)?\s*in\s+(?:to\s+)?\d+(?:\s+\d+)?\s*in\s+crushed)"   # a gradation range
                            r"(?:\S+\s+){0,8}?(?<!\bof\s)(?<!\bwith\s)(?<!\band\s)"
                            r"crushed\s+(?:concrete|conc|limestone|stone|rock|aggregate)\b"),
    ("conc_pavement",       r"concrete (street )?pav(ement|ing)|reinforced concrete pav|alley pavement|street pav|class .{0,6}p1|"
                            # (round 8, .29) "12" REINFORCED CONCRETE ROADWAY PAVEMENT (4,000 PSI)" (72,779
                            # SY), "RC PAVEMENT CLASS C 3,600 PSI 8 IN ROADWAY", "6" Concrete Panel
                            # Replacement (Full Depth Repair)" were concrete_misc / `other`.
                            r"\bconcrete\s+(?:roadway|road|street|alley|parking|drive\s+lane)\s+pav\w*|"
                            r"\b(?:rc|r\s+c|pcc|p\s+c\s+c)\s+pav(?:ement|ing)\b|"
                            r"\b(?:concrete|pavement|pvmt)\s+(?:pavement\s+)?panels?\b(?:\s+\w+){0,2}?\s+replac\w*|"
                            r"\bpanel\s+replac\w*(?=.{0,60}\b(?:concrete|pavement|full\s+depth)\b)"),
    # ---- pipe end structures -------------------------------------------
    # A safety end treatment, a headwall and a wingwall are THREE different
    # pay items, and not one of them is a pipe or an inlet.  All three were
    # invisible in the price reference, each for a different reason:
    #   * there was no `headwall` family at all -- `headwall` was only a token
    #     inside the inlet_structure pattern, so 3,469 placement rows were
    #     filed as inlets;
    #   * storm_pipe's \brcp\b claimed 8,099 "SET (TY II)(24 IN)(RCP)(6:1)(P)"
    #     rows, i.e. the family was being decided by the MATERIAL token rather
    #     than by the product;
    #   * bare "WINGWALL (PW-1)(HW= 8 FT)" and the TY I box SETs name neither
    #     a pipe nor an inlet, so 4,924 + 1,632 rows fell through to `other`
    #     and were discarded unpriced.
    #
    # MEASURED on master_panel.csv under the price_reference gate (external,
    # competitive, construction; letting medians, then their median, as
    # build_master builds price_reference), holding unit AND size bucket fixed
    # so the only variable is the product:
    #       size       headwall           SET              ratio
    #       13-19 in   $ 4,050 (np  30)   $1,050 (np 229)  3.9x
    #       19-25 in   $ 5,625 (np  90)   $1,500 (np 251)  3.8x
    #       25-37 in   $ 7,906 (np 113)   $3,294 (np 192)  2.4x
    #       37+  in    $12,850 (np 107)   $6,100 (np  74)  2.1x
    # 2.1-3.9x apart with 30-251 projects behind every cell, so these must NOT
    # share a family: folding them together would repeat exactly the error the
    # riprap split fixed.  A headwall is cast in place; a SET is a precast
    # sloped end with safety pipe runners.  Wingwall sits 1.34x off headwall
    # at EA ($23,000 over 238 projects vs $17,209 over 11) and its price is
    # cleanly monotone in the HW= height the description carries -- $8,100 at
    # 3 ft rising to $92,500 at 14 ft, 12+ projects at every height -- which
    # is what says it is a real pay item and not a ragbag.  That same 11x span
    # is INSIDE one `wingwall EA` cell, because extract_size_in reads a pipe
    # diameter in inches and these items give a structure height in feet, so
    # the cell is not yet a flat unit price.  Do NOT fix that by mapping HW=
    # feet into size_in: size_in means pipe diameter, and an 8 ft tall
    # wingwall is not a 96 in pipe.  The same caveat applies to the unsized
    # `safety_end_treatment EA` cell (TY I box SETs, $5,500 at S=3/HW=2 to
    # $25,400 at S=10/HW=10, 4.6x).
    #
    # ORDER.  The block sits AFTER every demolition rule, so "REMOV STR
    # (HEADWALL)" (1,128 panel rows) and "REMOV STR (SET)" (1,328) keep
    # landing in demo_utility -- removal is priced nothing like placement --
    # and BEFORE inlet_structure, which used to own the word `headwall`, and
    # storm_pipe.  It also sits BEFORE asphalt_pavement, whose `type [bd]\b`
    # (TxDOT HMA types B and D) claimed every "TYPE B HEADWALL" -- 17 panel
    # rows, 23 in line_items -- while this block sat down below `manhole`.
    # Moving it up past driveway, barrier_free_ramp, pavement_marking,
    # encasement and manhole changes nothing else: none of them matched a
    # single end-structure description.  Rules ABOVE the block still win,
    # and the 16 rows that mention a headwall but are claimed by one of them
    # (riprap_conc 3, riprap_stone 3, conc_beam 3, gabion_wall 5,
    # site_prep_clearing 2) are composite lines -- riprap or an apron AT a
    # headwall, a toe wall, a fused junction-box line, clearing -- left where
    # they are rather than lifting the block above the erosion rules.
    #
    # Measured over every description in master_panel.csv, the block moves
    # 18,330 panel rows out of the families they had at the 2026-11-18 20:24
    # build: storm_pipe 8,103, other 4,930 + 1,676, inlet_structure 3,475,
    # concrete_misc 129, asphalt_pavement 17.  None is a removal.  14 are not
    # strictly the structure itself -- a pipe connection to an existing
    # headwall (5, EA), a channel slab at one (4, SY), a washout repair at
    # one (3, LS), a fence on one (2, LF) -- and only the first sits in an
    # EA cell.  The one removal description the block used to catch,
    # Garland's "HEADWALLS & WING WALLS REMOVE & DISPOSE" (3 rows), now goes
    # to demo_structure; see the `walls?` there.
    #
    # safety_end_treatment is tested BEFORE headwall because "SLOPING HEADWALL
    # (SET)(TY II)(42 IN)" names both and the SET designation is the specific
    # one.  The same holds when the TxDOT standard is named in place of the
    # word: "TYPE SETP-CD HEADWALL" and "SLOPING HEADWALL (PSET-SP)" are safety
    # end treatments by standard (35 rows in all reach SET this way rather
    # than headwall).  headwall precedes wingwall so a combined "Reinforced
    # Concrete Headwall/Wingwall" lands on the primary element.
    #
    # The SET pattern anchors on `set ty`, the SETP/SETB/PSET standard codes
    # and a size-then-SET ending ("18" SET", "30" 4:1 SET", "4'X3' 4:1 SET")
    # rather than a bare \bset\b ON PURPOSE: norm_desc strips parentheses, so
    # "SET (TY II) (24 IN)" arrives as "set ty ii 24 in", and a loose rule
    # would also swallow "VIDEO IMAGING AND RADAR SET-UP SYS" (14 rows),
    # "VIVDS SET-UP SYSTEM" (7), quick/rapid-set concrete (13) and "SET REMOVE
    # REINSTALL" (8).  `ty(pe)?(i{1,2})?` takes the unspaced "SET (TYI)";
    # `\bsafety end` with no tail takes "SAFETY ENDWALL" and the misspelt
    # "SAFETY END TYREATMENT".  wingwall takes the plural: "CL C CONC
    # (WINGWALLS)" is CY concrete for that element, exactly as "CLASS C
    # CONCRETE HEADWALL" already was under headwall.  Together these widenings
    # bring 109 more panel rows into the block -- SET 48 out of other and
    # storm_pipe, wingwall 61 out of concrete_misc and other -- and send the
    # 24 SETP-CD / PSET-SP rows from headwall to SET.  Checked over every panel
    # and line_items description: 0 false positives.
    ("safety_end_treatment", r"\bsafety end|sloped? end treat|"
                             r"\bsets?\s+ty(pe)?(i{1,2})?\b|\bset[pb]\s*[cp]d\b|"
                             r"\bpset\s*s[cp]\b|\bsets?\s*pd\b|"
                             r"\b(\d+ in|\d+ 1|x\d+)\s+sets?$"),
    ("headwall",            r"head\s*wall|\bhdwl\b"),     # HDWL: "Parallel Hdwl" (round 3b)
    ("wingwall",            r"\bwing\s*walls?\b|"
                            # (round 9) TxDOT box-culvert end treatments named only by their standard:
                            # "TxDOT FW-S (2-8'x5') (4:1) (HW=10')" (bids $72,000 each) was `other` at $870
                            r"\b(?:ch\s+)?(?:fw|pw|sw)\s+(?:0|s|1|15|30|45)\b(?=.{0,40}\bhw\b)"),
    # RAILING (round 6, .27): a pedestrian / bridge rail or handrail is
    # metalwork priced at $100-400/LF.  It had no family: "Pedestrian
    # Handrail" and "Railing (Type C411-Mod)" were `other` (priced $14-17/LF
    # against $156-260), "Pedestrian Rail (Trail)" sidewalk, "Pedestrian Handrail
    # Type B" asphalt_pavement (by "type b"), and TxDOT's "RAIL (TY T223)" and
    # the like fence (4,326 rows, $120/LF), where they also lifted farm fence:
    # Mesquite's "Temporary 5-Strand Wire Fence" ($5.35-25/LF) was priced at
    # $60.  Placed after guardrail, the removals, painting and retaining_wall;
    # not a railroad, a rail track or kit, a fence that names its rails, a
    # rail's foundation, or work done FOR a rail ("thickened edge for ped.
    # rail").
    # cleaning / water-blasting and painting an EXISTING rail is a coating
    # item ("Existing Bridge Pedestrian Rail Cleaning and Painting (System
    # 1-A)", "Water Blast Existing Bridge Rail Paint (TxDOT Item 446)"),
    # like the "(re)paint" lines above.
    ("painting",            r"^(?!.*\b(?:install\w*|furnish\w*|new|construct\w*|replac\w*|repair\w*)\b)"
                            r".*\b(?:rails?|railings?|handrails?)\b.{0,40}\b(?:paint\w*|coatings?)\b"),
    ("railing",             r"^(?!.*\b(?:rail\s?roads?|railways?|light\s+rail|foundations?|footings?|fenc\w*)\b)"
                            r"(?!.*\brails?\s+(?:track\w*|crossings?|spurs?|kits?)\b)"
                            r"(?!.*\bfor\s+(?:the\s+)?(?:\w+\s+){0,2}?(?:hand\s?)?rail\w*)(?!.*\bboardwalks?\b)"
                            r".*(?:\bhand\s?-?\s?rail\w*|\brailings?\b|"
                            r"\b(?:ped\w*|bridge|brdg|metal|steel|aluminum|alum|pipe|safety|trail|bicycle|bike|"
                            r"combination|comb)\s+rails?\b|^rails?\b|\brails?\s+(?:ty|type)\b)"),
    # (round 3b) "asphalt repair / patch / street" are asphalt work; HMA
    # "type B / D" is not a ramp's type ("Construct ADA Ramp, Type D").
    # (round 9) stone veneer is masonry ("Stone Veneer (ST-1/1A)", bids $33-77/SF, was `other` at $16, and
    # "Stone Veneer Type B 3" Cast Stone Veneer" asphalt_pavement by its "Type B"); a veneered retaining
    # wall or headwall keeps its structure's family (matched above)
    ("masonry_fascia",      r"\bstone\s+veneer\b|\bveneer\s+stone\b"),
    # (round 9) blasting and recoating a tank or steel is the coating trade: "Abrasive Blast Clean Exterior
    # (SSPC-SP6) ... Recoat Exterior of Tank" (LS, bids $296k) was `other` at $20k; not graffiti removal
    ("painting",            r"^(?!.*\bgraffiti\b).*(?:\babrasive\s+blast\w*|\bblast\w*\s+(?:clean\w*|surface)|"
                            r"\brecoat\w*\b)"),
    ("asphalt_pavement",    r"asphalt(ic)?\b.{0,25}\b(pav|overlay|surface|base|course|repair|patch\w*|street)|hmac|hot mix|"
                            r"\bmill\w*\b.{0,40}overlay|(?<!ramp )type [bd]\b|"
                            r"\bachm\b|\bhma\b|\bsmas?\b|"   # thin applications: surface_treatment (.24c)
                            # (round 8, .29) TxDOT "SP MIXES SP-C PG64-22" / "SUPERPAVE MIXTURES" (TON),
                            # "INTERSECTIONS (ACP)", "TURNOUTS (ACP)", "FLEXIBLE PAVEMENT STRUCTURE REPAIR
                            # (12")" and "POTHOLE REPAIR" were `other` (6,900 rows in the other|SY and
                            # other|TON cells); a driveway keeps driveway, a pothole to locate a utility
                            # is no repair.
                            r"\basphalt(?:ic)?\s+(?:transition|material\s+for|leveling|level\s+up)\b|"   # round 8
                            r"\bsp\s+mix\w*|\bsuperpave\b|^(?!.*\bdriveways?\b).*\b(?:intersections?|turnouts?)\s+acp\b|"
                            r"\bflex\w*\s+pav\w*\s+(?:str\w*\s+)?repair\w*|"
                            r"\bpot\s?holes?\s+(?:repair\w*|patch\w*)|\b(?:repair\w*|patch\w*)\s+(?:of\s+)?pot\s?holes?\b"),
    ("driveway",            r"driveway|\bdrive\s?approach"),       # round 3b: drive approach
    # Plural ramps (round 3b): TxDOT's "CURB RAMPS (TY 7)" was curb_gutter by
    # its `curb` (31 descriptions, 6,482 rows).  A bare plural is not taken:
    # "DEBRIS REMOVAL (ENTRANCE/EXIT RAMPS)" is a highway ramp.
    ("barrier_free_ramp",   r"barrier free ramp|\bada ramps?\b|truncated dome|detectable warning|\bramp\b|"
                            r"\b(?:curb|ada|accessible|pedestrian|wheelchair|handicap\w*|type\s+\w+)\s+ramps\b|^ramps\b"),
    ("pavement_marking",    r"marking|striping|button|reflective"),
    # ---- utilities ------------------------------------------------------
    # TRENCHLESS (round 6, .27): a carrier pipe or conduit put in by
    # horizontal directional drilling, boring, jack-and-bore, auger boring,
    # microtunnelling or "trenchless method" is a crossing priced per LF by
    # diameter, like the "other than open cut" and "bore" lines this family
    # already holds -- not open-cut pipe and not `other`.  Aubrey FM 428's
    # "Furnish & Install 18-Inch PVC Force Main Pipe by Horizontal Directional
    # Drilling" (8,133 LF, bid $185/LF) was `other` and priced at $6.50/LF;
    # Frisco's "24" Fusible PVC (AWWA C900, DR-18) Pipe by Directional Drill"
    # (1,105 LF, $800-1,030) water_pipe at $372; "42" Steel Casing by
    # Trenchless Construction" `other`.  A service line by bore stays a
    # service, and a soil / test boring is not a crossing.
    ("encasement",          r"encasement|\bbore\b|other than open cut|tunnel|"
                            r"^(?!.*\bservices?\b)(?!.*\b(?:box(?:es)?|rcbc?s?|cbc|sbc|mbc)\b)"
                            r"(?!.*\b(?:\d+\s?tb|tb|gb|seagate|ssd|nas|server|storage|raid)\b)(?!.*\b(?:soil|geotech\w*|test)\s+bor\w*)"
                            r".*(?:\bdirection(?:al|ally)?\s+(?:drill\w*|bor\w*)|\bhdd\b|\btrenchless\b|"
                            r"\bjack(?:ed|ing)?\s+(?:and\s+)?bor\w*|\bauger\s+bor\w*|\bmicro\s?-?\s?tunnel\w*|"
                            r"\bpipe\s+jack\w*|\b(?:by|via|using)\s+(?:the\s+)?(?:bor(?:e|ing|ed)|jack\w*)\b|\bbored\b|"
                            r"\bcasing\s+(?:and\s+)?bor\w*|\bboring\s+(?:under|beneath|across)\b)"),
    # EXTRA DEPTH of a manhole is priced per vertical foot (or per EA of one
    # foot) at a few hundred dollars, not as a manhole: Midlothian's "Extra
    # Depth" EA was priced at $16.6k against $450 (round 5).  The extra-depth
    # words must open the item (within the first six words), so a merged row whose text
    # runs "... Manhole (8 ft Deep or less) Extra Depth for ..." stays a
    # manhole (it has six words before them).
    ("manhole_extra_depth", r"^(?=.*\b(?:manholes?|mh|mhs|diameter|dia)\b)"
                            r"(?:\S+\s+){0,5}?(?:extra|additional|addl|add l|excess)\s+"
                            r"(?:vertical\s+)?(?:depth|f(?:oo|ee)t|v\s?f)\b"),
    ("manhole",             r"manhole|\bmh\b|"          # MH (round 3b)
                            # (round 9) "4' Dia. SSMH, Furnish & Install 5' Dia. SSMH" (bids $12,000) was
                            # `other` at $965; a pad / collar / apron AT a manhole is not one
                            r"^(?!.*\b(?:pads?|slabs?|collars?|aprons?|rings?)\b).*\bs[sd]?mh\b"),
    # The pipe end structures (safety_end_treatment, headwall, wingwall) are
    # NOT here any more: they sit above asphalt_pavement -- see the block there.
    # `headwall` removed from this pattern -- it now has its own family above.
    # `flume` is dead here: conc_beam's \bflumes?\b is matched many rules
    # earlier, so no row has ever reached inlet_structure on that word.
    ("inlet_structure",     r"inlet|junction box|grate|catch basin|flume|"
                            r"\barea\s+drains?\b"),   # (round 9) "5'x5' Area Drain" (bids $4,650) was `other`
    # VALVE vs ASSEMBLY (round 8, .29): valve_hydrant held three different things in one cell set --
    # an in-line valve priced by its diameter ($2,700 at 8", $20,000 at 16"), an air-release / air-
    # vacuum or blow-off ASSEMBLY ($10,000-60,000 at 1-8", with its vault or manhole), and a FIRE
    # HYDRANT assembly ($5,000-12,000, whose 6" lead / gate valve is no size of the item).  So a 3"
    # combination air valve assembly was priced from small gate valves ($1,574 against $18,390), a 6"
    # one from 8" gate valves ($2,917 against $60,320), and a hydrant "with 6" gate valve" at $2,227
    # against $9,000.  The two assemblies get families of their own; a hydrant carries no size
    # (extract_size_in).  Not: removal, relocation or adjustment of them (matched above), a hydrant
    # meter, marker, extension or lead pipe, bollards around one, a valve box alone.
    ("valve_assembly",      r"^(?!.*\b(?:remov\w*|abandon\w*|relocat\w*|adjust\w*|salvag\w*|demolition|demo|"
                            r"bollards?|markers?)\b)"
                            r".*(?:\b(?:combination\s+)?air\s+(?:release|relief|vac\w*|valves?)\b|\barvs?\b|\bcarv\b|"
                            r"\bblow\s?-?\s?off\b|\bflush(?:ing)?\s+(?:valve|hydrant|assembl\w*|point|station)s?\b)"),
    ("fire_hydrant",        r"^(?!.*\b(?:remov\w*|abandon\w*|relocat\w*|adjust\w*|salvag\w*|meters?|markers?|"
                            r"flushing|flush|paint\w*|extensions?)\b)"
                            r"(?!.*\b(?:pvc|c\s?900|dip|ductile)\b.*\bhydrant\s+leads?\b)"
                            r".*\b(?:fire\s+)?hydrants?\b"),
    ("valve_hydrant",       r"valve|hydrant|\barvs?\b"),     # ARV = air release valve (round 3b)
    ("water_service",       r"water service|water meter|water\s?line\s+service"),   # round 3b
    ("sewer_service",       r"sewer service|service conn|clean\s*out"),
    # "... Pipe and Fittings" is the PIPE, priced per LF with its fittings
    # (round 5): "Furnish & Install 12" Pipe C900 DR-18 PVC Pipe and Fittings"
    # (2,626 LF) and Frisco Research Rd's "30" AWWA C900, PVC Pipe (WH DI
    # Fittings)" (1,505 LF, $400/LF) were pipe_fittings.
    ("pipe_fittings",       r"^(?!.*\b(?:pipe|piping|main|line|waterline|sewer)s?\s+(?:and|with|wh|w|w/|incl\w*|including)\s+"
                            r"(?:all\s+|associated\s+|related\s+|necessary\s+|required\s+)?(?:\w+\s+){0,2}?fittings?\b)"
                            r"(?!.*\b\d+\s*in\s+(?:\w+\s+){0,5}?(?:pipe|piping|main|line|waterline)s?\s+fittings?\b)"   # round 8
                            r".*?(?:\btee\b|\bbend\b|\bfitting|\bcoupling\b|\bsleeve\b|\bcap\b.{0,12}\bpipe\b)"),
    # A box culvert is not a pipe: it is priced by span x rise in feet, not
    # by a diameter (item_specs.py reads both).  The abbreviations RCB /
    # RCBC / CBC / SBC / MBC were `other` -- "462 - 6'x1.5' RCB (CIP WITH
    # CLASS S TOP SLAB)" (Tarrant F2026123, about $600/LF) among them --
    # TxDOT's "CONC BOX CULV (5 FT X 3 FT)" was concrete_misc (6,399 rows),
    # and only the spelled-out "box culvert" reached storm_pipe.  One family
    # now holds all 331 descriptions (7,433 rows, 389 lettings).  A box
    # connection, end cap or extension is box work too; when a pipe is named
    # FIRST ("Connect 24" RCP to box culvert", "STORM SEWER CLEAN (BOX
    # CULV)") the item stays storm_pipe -- see _family_of.  (round 3b)
    ("box_culvert",         r"\b(?:rcbc?s?|cbc|sbc|mbc)\b|\bbox(?:es)?\s+culv\w*|"
                            r"\b(?:reinforced\s+)?concrete\s+box(?:es)?\b(?!\s+(?:inlet|junction|vault|meter|valve|beams?))|"
                            r"\bprecast\s+box(?:es)?\b(?!\s+(?:inlet|junction|vault|meter|valve))|"
                            r"\bbox\s+section\b"),
    # (round 3b) "18-inch Class III Reinforced Concrete Pipe" spelled out was
    # concrete_misc (115 descriptions, 1,291 rows); "storm water pipe" was
    # water_pipe; "CMP IN PLC" is compacted in place, not corrugated pipe.
    # "R.C.P." reaches norm_desc as "r c p" (round 5: Edinburg Brennaleen's
    # 24" and 18" R.C.P. were `other`).
    ("storm_pipe",          r"\bpipe\s+under\s?drains?\b|\bunder\s?drains?\s+pipe\b|"   # (round 9) TxDOT
                            r"\b\d+\s*in\s+(?:\w+\s+){0,2}?under\s?drains?\b|^under\s?drains?\b|"     # "PIPE UNDERDRAINS"
                            r"\brcp\b|\br c p\b|\br c pipe\b|storm (sewer|drain|pipe)|\bstorm\s?water\s+(?:pipe\w*|lines?|drains?|sewers?|conduits?)\b|"
                            r"polypropylene|\bcmp\b(?!\s+in\s+pl)|\bhp\s+storm\b|"   # (round 8) "24-Inch HP Storm"
                            r"\b(?:reinforced\s+)?concrete\s+(?:storm\s+(?:drain\s+|sewer\s+)?)?(?:culvert\s+)?pipes?\b"),
    # Garland writes "WASTEWATER 8 IN PVC OPEN CUT" and "WATER 12 IN PVC", i.e.
    # the service plus a size plus a material, with no word "pipe" at all.
    # `sdr` is a pipe class, not a service: "8" SDR-14 CLASS 200 PVC WATER"
    # is a water line (round 3b).
    # (round 6, .27) a FORCE MAIN is pressure sewer: "Furnish & Install 18-Inch
    # PVC Force Main Pipe by Open Cut" and "8" HDPE Force Main" were `other`,
    # TxDOT's "24" C-905 DR 18 PVC Force Main" and "... WW FM ..." water_pipe.
    ("sanitary_pipe",       r"sanitary sewer|san swr|san sewer|\bsdr\b(?!.*(?<!waste )(?<!storm )\bwater\b)|"
                            r"\bwastewater\b.{0,28}(pvc|dip|pipe|line|main|\d+\s*in)|"
                            r"\bforce\s?mains?\b|\bww\s+fm\b|"
                            r"(?<!storm )\b(?:pvc|hdpe|dip|vcp|pp)\s+(?:gravity\s+)?sewer\b(?!\s+(?:laterals?|services?))"),   # (round 8) "12 in PVC sewer"
    # (round 3b) "waterline" / "watermain" as one word (34 descriptions were
    # `other`, e.g. Mesquite 24-7401's 11,150 LF "Replace Existing Waterline
    # with 8" Waterline"), "16" DIP WATER", and never "storm water pipe".
    # (round 5) the noun must end there: "Furnish, Install, Fertilize, Water &
    # Maintain Solid Sod" (Rockwall, 11,788 SY) was water_pipe by "water
    # main(tain)" and priced at $117/SY against $15.70.
    ("water_pipe",          r"(?<!storm )(?<!storm)water\s?(?:pipe(?:lines?|s)?|piping|lines?|mains?)\b|\bdi\s+pipe\b|\b(?:pvc|dip|hdpe|c900|ductile iron)\s+water\b|\bdr.?18\b|\bc900\b|ductile iron|"
                            r"\bwater\b.{0,28}(pvc|dip|\d+\s*in)"),
    # (round 6, .27) cleaning or resealing the joints of a slab is joint work:
    # Grand Prairie Pollock Place's "CLEAN AND RESEAL JOINTS IN CONCRETE SLOPE
    # PROTECTION" (1,101 LF, bid $4-25) and "CLEAN VEGETATION FROM JOINTS ..."
    # were concrete_misc, priced at $237 and $273/LF.
    ("conc_joints",         r"butt joint|street header|joint seal|expansion joint|"
                            r"\b(?:re)?seal\w*\s+(?:\w+\s+){0,2}?joints?\b|"
                            r"\bclean\w*\s+(?:and\s+(?:re)?seal\w*\s+)?(?:\w+\s+){0,2}?joints?\b"),
    # ---- site / other ---------------------------------------------------
    # illuminat\w* plus the archive's own abbreviations: 5,791 rows of
    # "ILLUM FND TY A" / "RDWY ILL ASS TY 10" were in `other`.
    # (round 3b) plus the luminaire / fixture on its own ("REPLACE LUMINAIRE
    # W/LED (250W EQ)", 115 descriptions were `other`), high-mast work,
    # "streetlight" and "pullbox" as one word.  Assemblies: lighting_assembly.
    ("electrical_lighting", r"illuminat\w*|\billum\b|rdwy ill|street\s?lights?|\bcondt\b|conduit|condr|conductor|\belec\b|electrical|ground box|pull\s?box|elc srv|"
                            r"\bluminaires?\b|\blight(?:ing)?\s+fixtures?\b|\bled\s+(?:light(?:ing)?\s+)?fixtures?\b|"
                            r"\bhi(?:gh)?\s?m(?:a)?st\b"),
    ("curb_gutter",         r"curb (and|&) gutter|integral.{0,20}curb|\bcurb\b"),
    # (round 12, .33) a WALL named after the sidewalk or trail it stands beside is a retaining wall: "CONCRETE
    # SIDEWALK WALL" (McKinney, CY; Frisco Dominion Creek Trail Repair, 40 CY), "Construct 6" Thick Sidewalk
    # Wall (Height Varies) (w/ 2" form liner)" (Frisco Legacy Dr, 2,339 SF), "TxDOT TY C2 Sidewalk Wall w/
    # Form Liner", "Concrete Trail Wall (Class F)", "Construct sidewalk retaining (variable height) up to 36
    # inches" were sidewalk.  "Concrete sidewalk WITH wall" (TxDOT item 531) names the sidewalk first and stays
    # sidewalk, as "retaining wall with integral sidewalk" stays retaining_wall; a seat wall, a veneer, a
    # screen or parapet wall and a curb wall are not retaining walls.
    ("retaining_wall",      r"^(?!.*\b(?:seat|veneer|screen\w*|parapet\w*|curb\w*)\b)"
                            r".*\b(?:sidewalks?|trails?)\s+(?:retaining\b|walls?\b)"),
    ("sidewalk",            _PEDWAY_YIELD + r".*?(?:sidewalk|lead walk|concrete steps|"   # (round 12) yield
                            r"^(?!.*\b(?:sod\w*|seed\w*|hydromulch\w*|mulch\w*|turf\w*|irrigat\w*)\b).*\btrail\b)"),   # (round 8)
    ("fence",               r"fence|fencing|\brail\b|\bgates?\b"),     # fencing, gates (round 3b)
    ("masonry_fascia",      r"cultured stone|fascia|masonry|brick"),
    # (round 6, .27) bed / soil preparation for turf or planting is
    # landscape soil work: Mesquite 22-6351's "Turf Bed Preparation (Labor)",
    # 280,000 SF bid at $0.05-0.25, was `other` and priced at $28.34/SF.
    ("seeding_sod",         r"\bsod\b|sodding|hydromulch|seeding|turf establish|topsoil|top soil|"
                            r"vegetative watering|broadcast seed|cellulose fiber mulch|\bseed\b|"
                            r"\bmulch\b|\bfertiliz|\bbed\s+prep\w*|\bsoil\s+prep\w*|"
                            # (round 8, .29) "HYDRAMULCH (TCE, SE) (PKWY)" (107,057 SY, bids $1.19) and
                            # "Revegetation" were `other` at $73-90/SY; so were "Restoration of Parkways and
                            # Yards (No Irrigation)", "Restore parkways, medians and disturbed areas",
                            # "Furnish, Install, Water & Establish Vegetation", "Turfgrass Planting".
                            r"\bhydr[ao]\s?-?\s?mulch\w*|\breveg\w*|\bturf\s?grass\b|"
                            r"\bcompost\b(?!.*\b(?:socks?|logs?|berms?|filter\w*|blankets?)\b)|\bplant(?:ing)?\s+(?:bed\s+)?(?:soil|mix)\b|"
                            r"\b(?:soil|bed)\s+(?:mix|amend\w*)\b|"
                            r"\bestablish\w*\s+(?:of\s+)?(?:\w+\s+){0,2}?(?:vegetation|turf|grass\w*)\b|"
                            r"\bvegetat\w*\s+establish\w*|"
                            r"^(?!.*\b(?:sprinkler\w*|pavement|paving|sidewalks?|curbs?|driveways?|fenc\w*|walls?|"
                            r"pipes?|mains?|lines?|services?)\b)(?!.*(?<!\bno\s)\birrigat\w*)(?:.*\brestor\w*\s+(?:of\s+)?"
                            r"(?:the\s+)?(?:\w+\s+){0,3}?(?:parkways?|yards?|lawns?|disturbed|grass\w*|turf)\b|"
                            r".*\b(?:parkways?|yards?|lawns?)\s+(?:and\s+\w+\s+)?restor\w*)"),
    # IRRIGATION (round 6, .27): an irrigation system, temporary irrigation,
    # drip / spray irrigation or an irrigation service had no family and was
    # `other` (about 1,600 rows: 92 city lettings at LS, 30 at EA, 13 at SF;
    # TxDOT "IRRIGATION SYSTEM" LS and "... OPER AND MAINT" MO), where it set
    # the catch-all cells: Mesquite 22-6351's "Temporary Irrigation", 280,000
    # SF bid at $0.17-0.75, was priced from other|SF at $28.34.  Placed after
    # the utility and electrical families, so an irrigation valve, meter,
    # conduit, sleeve, bore or allowance stays where it was; "no irrigation"
    # is not one.
    ("irrigation",          r"^(?!.*\bno\s+irrigat\w*)"
                            r"(?!.*\b(?:under|beneath|across|cross\w*|protect\w*|around)\s+(?:the\s+)?(?:existing\s+|ex\s+)?irrigat\w*)"
                            r"(?!.*\b(?:fire|dry\s+pipe|wet\s+pipe|buildings?|risers?|trees?|conflict|"
                            r"demolition|demolish\w*|demo)\b)(?!.*\blandscap\w*\s+maint\w*)"
                            r"(?=.*\b(?:irrigat\w*|sprinkler\s+systems?)\b)"),
    # GRADING (fine / finish / final / rough / site grading, regrading) is
    # earth shaping priced in cents per SY, or per AC / STA (round 5): Corpus
    # Christi La Volla Creek's "FINE GRADING (POND AND SITE)", 373,000 SY at a
    # $0.17 low bid, was `other` and priced from the other|SY cell at $80.91.
    # Not when the line also excavates, fills or backfills (those rules,
    # below, keep it).
    # (round 6, .27) Stream-restoration shaping -- "Riffle Micro-Grading"
    # (Celina Ousley Park, $35-450/SY), pools, vanes, J-hooks, weirs -- is a
    # specialty structure, not cents-per-SY grading: it was one of the two
    # DFW lettings in grading|SY and set the $163/SY Azle's 770 SY of "Site
    # grading" (bid $11-25) was priced at.  A grading VERB on a ditch, swale,
    # channel, slope or berm ("Grade drainage swale and construct berm",
    # "Grade Ditch along Reagan Road", 19 LF rows in `other`) is grading.
    ("grading",             r"^(?!.*\b(?:excavat\w*|embankment|borrow|backfill\w*|fill|adjust\w*)\b)"
                            r"(?!.*\bto\s+(?:the\s+)?(?:finish\w*|final|existing)\s+grade\b)"
                            r"(?!.*\b(?:riffles?|micro\s?-?\s?grad\w*|pools?|thalweg|j\s?-?\s?hooks?|"
                            r"(?:rock|cross)\s+vanes?|weirs?)\b)"
                            r"(?:.*\bgrading\b|(?:\S+\s+){0,2}?(?:fine|finish(?:ed)?|final|rough|precision)\s+grade\b|"
                            r".*\bre\s?grad(?:e|ed|ing)\b|"
                            r"^(?:\S+\s+)?grade\s+(?:and\s+\w+\s+)?(?:the\s+)?(?:existing\s+|ex\s+|new\s+|proposed\s+)?"
                            r"(?:\w+\s+)?(?:ditch\w*|swales?|channels?|slopes?|berms?|site|areas?|shoulders?|"
                            r"parkways?|lots?|pads?|banks?)\b)"),
    ("excavation",          r"excavation|excavate"),
    ("embankment_fill",     r"\bfill\b|embankment|"
                            r"^import\s+and\s+place\b(?!.*\b(?:sod|topsoil|hmac|asphalt|concrete)\b)"),   # (round 9)
    # (round 12, .33) EARTHWORK said in one word is excavation: "EARTHWORK (NET CUT)" (32 rows, CY), "Earthwork"
    # (LS), "General Site Earthwork - Cut", "TRAIL EARTHWORK" were `other` (or sidewalk); a line that names
    # fill or embankment has already been taken by embankment_fill above.
    ("excavation",          r"\bearth\s?works?\b"),
    # Removing concrete is demolition even when the noun comes first
    # ("Concrete Alley Removal", "#0-5 Remove 6" to 8" thick reinforced
    # concrete alley"); without this it fell to concrete_misc below and was
    # priced as placement -- 41 descriptions (round 3b).  Not when anything
    # is also built or maintained.
    ("demo_other",          r"^(?!.*\b(?:install\w*|construct\w*|furnish\w*|maint\w*|plac\w*|pour\w*|"
                            r"build\w*|replac\w*|set\s?up|reinstall\w*)\b)(?=.*\bconcrete\b)"
                            r".*\b(?:remov\w*|demolish\w*|demolition|dispos\w*)\b"),
    # BRIDGE CONCRETE (round 8, .29), a new family split out of concrete_misc (and `other`):
    # prestressed girders / slab beams ($150-300/LF), class-C concrete for abutments, caps, columns,
    # bents and footings ($900-2,500/CY), HPC bridge deck slabs ($23-38/SF), approach slabs.  They
    # made concrete_misc's LF cell a girder price ($150-220/LF), at which a city's "CONCRETE FOOTING"
    # (bids $100/LF) or "Reinforced Concrete Header" ($25/LF) was priced.  A repair or removal of
    # them is not this.
    ("bridge_concrete",     r"^(?!.*\b(?:remov\w*|demolish\w*|demolition|demo|dispos\w*|repair\w*|repr|rep)\b)"
                            r"(?:.*\bprestr\w*\s+(?:concrete\s+)?(?:i\s?-?\s?)?(?:girders?|slab\s+beams?|box\s+beams?|"
                            r"x\s?-?\s?beams?|u\s?-?\s?beams?|beams?)\b|"
                            r".*\bgirders?\b.{0,12}\btx\s?\d{2}\b|.*\btx\s?\d{2}\b.{0,12}\bgirders?\b|"
                            r".*\bclass\s+[a-z]{1,2}\s+concrete\s+(?:abut\w*|caps?|columns?|bents?|piers?|footings?|decks?|"
                            r"slabs?)\b|"
                            r".*\breinforced\s+concrete\s+slab\b.{0,25}\b(?:hpc|bridge|slab\s+beam|box\s+beam|extend\w*)\b|"
                            r".*\bconcrete\s+slab\b.{0,10}\bhpc\b|.*\bapproach\s+slabs?\b)"),
    ("concrete_misc",       r"concrete"),
]
FAMILY_RULES = [(n, re.compile(p, re.I)) for n, p in FAMILY_RULES]

MATERIAL_RE = re.compile(r"\b(rcp|pvc|dip|hdpe|ductile iron|c900|sdr\s*\d+|cmp|frp)\b", re.I)


import functools


def _first_rule(text):
    """(family, match) of the first FAMILY_RULES pattern found in `text`, or
    (None, None) when none is."""
    for name, rx in FAMILY_RULES:
        m = rx.search(text)
        if m:
            return name, m
    return None, None


# ---- the MAIN noun phrase decides (round 3b) ---------------------------------
# FAMILY_RULES is first-match-wins over the whole text, so a word in a clause
# that only lists what the price INCLUDES could decide the family: '8" water
# main w/ fittings, trenching, trench safety protection, ...' (Glenn Heights
# RFB 2024-03, 5,000 LF at $105-166/LF) was trench_safety by rule order, a
# fire hydrant "INCLUDING ... BACKFILL" borrow_backfill, a curb "with 8-inch
# CTB" conc_barrier.  Now the rules are read on the HEAD -- the text before
# the first with / w/ / including / includes / incl / complete in place /
# over -- and the head's family wins when it names one.  The full text still
# decides when
#   * the head names nothing the rules know ("INSTALL (COMPLETE IN PLACE) 6'
#     SIDEWALK", "Right of Way Preparation (including Clearing and
#     Grubbing)"): the old reading stands rather than becoming `other`;
#   * the qualifier changes WHAT the item is (_QUALIFIER_FAMILIES): a TRM
#     "with sod" or "including anchors", an exit "including installation,
#     maintenance and removal", a line "by bore with steel casing", a sloping
#     headwall "with safety end treatment", an "(Allowance)".
# norm_desc has already dropped the parentheses, so a parenthetical counts
# only through its first word ("(including ...)", "(w/ ...)").
_QUALIFIER_RE = re.compile(
    r"\s(?:w/o\b|w/|with\b|without\b|including\b|includes?\b|incl\b|inclusive\s+of\b|"
    r"to\s+include\b|complete(?:d)?\s+(?:and\s+)?in\s*place\b|complete\s+inplace\b|over\b)")
_QUALIFIER_FAMILIES = {"trm_bundled", "trm_permanent", "trm_temporary", "construction_exit",
                       "construction_exit_remove", "encasement", "contingency",
                       "safety_end_treatment", "pipe_bursting"}   # round 5: "... with 8" by pipe bursting"
_REBUILD_RE = re.compile(r"\b(?:replac\w*|reconstruct\w*|rebuil\w*)\b")
_NOT_REBUILD_RE = re.compile(r"\b(?:to|will|shall)\s+be\s+(?:replac|reconstruct|rebuil)\w*")
_RESET_RE = re.compile(r"\b(?:salvag\w*|reset\w*|re\s?install\w*|relocat\w*|reuse\w*)\b")
_REMOVAL_WORD_RE = re.compile(r"\b(?:remov\w*|dispos\w*|demolish\w*|demolition|"
                              r"saw\s?cut\w*|sawing)\b")
_REPLACE_WITH_RE = re.compile(
    r"\breplac\w*\s+(?:(?:the\s+)?(?:existing|ex|old|damaged|deteriorated)\b(?:\s+\S+){0,8}?\s+)?"
    r"(?:with(?!out)|w/(?!o\b))\s*(\S.*)$|\bwith\s+new\s+(\S.*)$")
# MATERIAL SUPPLY is not construction (round 3b follow-up, .24b).  TxDOT
# maintenance supply contracts buy material delivered to a stockpile or
# dumped on the road -- "FL BS(TY A GR 4)(DEL)(ST1)", "ASPH PATCH MAT
# (DEL)(SITE 1)", "RIPRAP (DEL)(STONE PROT)(18 IN)(SITE 1)", "FL BS (RDWY
# DEL)(TY D GR 1-2)" (Item 247 roadway delivery: spread by TxDOT's own
# forces) -- and cities buy ready-mix or base at the plant ("4000 PSI
# CONCRETE, CITY TO PICKUP", "DELIVERED Zone 1 - Flexible Base").  Those are
# material prices, a fraction of the in-place price of the same family, so
# they stay `other`; that also keeps a supply-only letting out of the
# construction pool (round 3b's new vocabulary, "fl bs" and "asphalt patch",
# had moved 24 of them in).  "del" alone is also TxDOT's delineator ("INSTL
# DEL ASSM") and a Spanish place name, so the delivery marker needs a
# stockpile / site / roadway word beside it.  An item that also places,
# installs or removes something is work, not supply.
_SUPPLY_DEL_RE = re.compile(
    r"\bdel\b(?!\s+(?:ass\w*|rio|mar|valle|norte|sol|oro|prado|monte|lago|rey|paso|"
    r"webb|ray|carmen|ridge|park)\b)")
_SUPPLY_DEL_CTX_RE = re.compile(r"\b(?:st\s?\d+|site\s?\d*|rdwy|supersack|stockpile)\b|"
                                r"\bdel\s+st\b|\bdel$")
_SUPPLY_OTHER_RE = re.compile(
    r"\bdelivered\b|\bdeliver\b|"
    r"\b(?:pick\s?up|pickup)\b(?=.*\b(?:plant|vendor|yard|your)\b)|"
    r"\b(?:city|county)\s+(?:to\s+)?(?:pick\s?up|pickup)\b|"
    r"\b(?:plant|vendor)\s+(?:pick\s?up|pickup)\b|"
    r"\b(?:pick\s?up|pickup)\s+(?:at|from)\s+(?:the\s+)?(?:plant|vendor)\b|"
    r"\b(?:pick\s?up|pickup|delivery)(?:\s+(?:st\s?\d+|site\s?\d*))?$|"
    r"\b(?:material|materials|furnish|supply)\s+only\b")
_SUPPLY_WORK_RE = re.compile(r"\b(?:plac\w*|install\w*|spread\w*|compact\w*|appl\w*|"
                             r"construct\w*|mobiliz\w*|remov\w*|demoli\w*|salvag\w*|"
                             r"haul\w*\s+and\s+plac\w*|in\s+place)\b")


# (round 9, .30) Apparel.  A county uniform purchase ("Sizes XS-XL, District DM130 Perfect Tri Tee",
# "Men's Harriton Short Sleeve Polo") read as pipe fittings by "tee" and "sleeve": 40 of its 155 rows
# were a construction family, so the purchase passed the project_kind gate as `mixed`.
_APPAREL_RE = re.compile(
    r"\b(?:polos?|t\s?shirts?|tee\s+shirts?|sweat\s?shirts?|hoodies?|hooded\s+sweat\w*|embroider\w*|"
    r"apparel|garments?)\b|\b(?:short|long)\s+sleeves?\b|"
    r"\bsizes?\s+(?:xs|xxs|lt|[2-6]?x{1,3}l[t]?)\b")


def is_apparel(desc_norm):
    """True for clothing (see _APPAREL_RE)."""
    return bool(_APPAREL_RE.search(desc_norm or ""))


def is_material_supply(desc_norm):
    """True for a material-supply line (see _SUPPLY_DEL_RE above)."""
    dn = desc_norm or ""
    if _SUPPLY_WORK_RE.search(dn):
        return False
    if _SUPPLY_DEL_RE.search(dn) and _SUPPLY_DEL_CTX_RE.search(dn):
        return True
    return bool(_SUPPLY_OTHER_RE.search(dn))


_END_STRUCT_RE = re.compile(r"\bsafety\s+end|\bsets?\s+ty|\bset[pb]\b|\bpset\b|"
                            r"head\s*walls?|\bhdwl\b|\bwing\s*walls?\b")
_END_STRUCT = ("safety_end_treatment", "headwall", "wingwall")
_EROSION_PEERS = {"riprap_conc", "riprap_stone", "gabion_wall", "gabion_mattress",
                  "conc_beam", "acb_mat"}
_PAVEMENT_LAYERS = ("asphalt_pavement", "conc_pavement", "flex_base", "subgrade_stab",
                    "pavement_milling", "surface_treatment")   # .24c


def _earliest(text, names):
    """(family, start) of the earliest match in `text` among the rules of
    `names`, or (None, None)."""
    best = (None, None)
    for name, rx in FAMILY_RULES:
        if name in names:
            m = rx.search(text)
            if m and (best[1] is None or m.start() < best[1]):
                best = (name, m.start())
    return best


def _family_of(text):
    """FAMILY_RULES on the head of `text` (see above), then the first-named
    tie-breaks between peers:
      * an end structure named before an erosion product is the item --
        "466 - HEADWALLS, WINGWALLS AND RIPRAP APRON" (Tarrant F2026123) was
        riprap_stone only because the riprap rules come first;
      * a storm pipe named before a box culvert keeps storm_pipe;
      * among pavement layers (asphalt, concrete, flexible base, subgrade)
        the first named is the item: "2" HMAC on 6" Flexible Base Course",
        "Hot Mix Asphaltic Concrete Pavement" (which `concrete pav` took)."""
    full, m = _first_rule(text)
    fam, scope = full, text
    q = _QUALIFIER_RE.search(text)
    if q and full not in _QUALIFIER_FAMILIES:
        head = text[:q.start()]
        hf, hm = _first_rule(head)
        if hf is not None:
            fam, m, scope = hf, hm, head
    if fam is None:
        return "other"
    # peers: the one NAMED FIRST is the item
    if fam in _EROSION_PEERS:
        e = _END_STRUCT_RE.search(scope[:m.start()])
        if e:
            for name, rx in FAMILY_RULES:
                if name in _END_STRUCT and rx.search(scope[e.start():]):
                    return name
    elif fam == "box_culvert":
        sp, pos = _earliest(scope, ("storm_pipe",))
        if sp and pos < m.start():
            return sp
    elif fam in _PAVEMENT_LAYERS:
        lay, pos = _earliest(scope, _PAVEMENT_LAYERS)
        if lay and pos < m.start():
            return lay
    return fam


# (round 8, .29) Abbreviations and misspellings the family rules should read as words.  Applied
# to the text the rules see, never to desc_norm (tokens, keys and dedup are unchanged):
#   rmv -> remove      TxDOT "RMV (0"-12") TRT/UNTRT BASE & ASPH PAV" (1,033 rows) was
#                      asphalt_pavement at $10-20/SY and, with the planing above, set the price of
#                      2" HMAC at large quantities to $3-8/SY; "RMV/REPL" (remove AND replace) stays
#   embank -> embankment          "EMBANK (FNL)(DC)(TY C)" (1,210 TxDOT rows) was `other`
#   jct box / jctbox -> junction box   "JCTBOX (COMPL)(PJB)(4FTX4FT)" (1,043 rows) was `other`
#   botoc -> by other than open cut    DWU's "16" WATER BOTOC" (trenchless; .27 put trenchless
#                                      crossings in encasement)
#   concreate -> concrete, treatement -> treatment, hydra mulch -> hydromulch
_FAMILY_ALIASES = [(re.compile(p), r) for p, r in (
    (r"\brmv\b(?!\s*/?\s*(?:and\s+)?repl)", "remove"),
    (r"\bembank\b", "embankment"),
    (r"\bjct\s?box\b|\bjctbox\b", "junction box"),
    (r"\bbotoc\b", "by other than open cut"),
    (r"\bconcreate\b", "concrete"),
    (r"\btreatement\b", "treatment"),
    (r"\bhydra\s?-?\s?mulch", "hydromulch"),
    # (round 11, .32) a LOCATION word before "culvert" says where the culvert is, not what it is:
    # "15" RCP for driveway culvert" and "CONSTRUCT 12" CMP DRIVEWAY CULVERT" were driveway, "Remove
    # Driveway Culvert 18"" and TxDOT's "REMOV STR (DRIVEWAY CULVERT)" (173 TxDOT rows) demo_pavement by
    # their "drive" -- 68 municipal rows in all.  Read without the location word they are the pipe
    # (storm_pipe), a box (box_culvert), its removal (demo_utility), its cleaning or its reset.
    (r"\b(?:driveways?|drive|roads?|roadways?|streets?|alleys?|alleyways?|side\s?roads?|cross|field|farm|"
     r"private|residential|entrance|access|median)\s+(?=culv)", ""),
    # (round 12, .33) TRAIL that abbreviates TRAILER: TxDOT's "TRAIL MNT SOL PRD RAD SPEED CNT MONITOR"
    # (trailer-mounted solar radar speed monitor, 11 rows) and a county's tyre list "ST235/80R16 LRE CAR RAD
    # TRAIL HD" (radial trailer tyre) were sidewalk by the word `trail`.
    (r"\btrail\s+(?=(?:mnt|mntd|mtd|mounted)\b)", "trailer "),
    (r"\brad\s+trail\b", "radial trailer"),
)]


def family_text(desc_norm):
    """desc_norm with _FAMILY_ALIASES applied: what the family rules read (round 8)."""
    t = desc_norm or ""
    for rx, r in _FAMILY_ALIASES:
        t = rx.sub(r, t)
    return t


@functools.lru_cache(maxsize=1 << 17)
def item_family(desc_norm):
    """Family of a normalised description.

    REMOVE-AND-REPLACE (round 3b).  An item that removes something and
    replaces / reconstructs / rebuilds it is the thing BUILT, not
    demolition: "Remove and Replace 8" Thick, 4,000 PSI Reinforced Concrete
    Pavement, Street ..." (Mesquite 24-7401, 43,000 SY at $90-120/SY) was
    demo_other, priced from a removal cell at about $9/SY.  The checker that
    found it counted 787 such items in 213 lettings, 97% filed as
    demolition.  So the removal verbs are dropped and the rest classified;
    "replace [existing X] with Y" is Y unless Y is only a material ("... with
    6" reinforced concrete" keeps X).  A pure removal is untouched, and so is
    a reset (salvage / reinstall / relocate), which relocate_reset owns.
    "To be replaced" says what happens next, not what this item builds.

    MATERIAL SUPPLY (.24b) -- delivered / picked-up material with no placing
    -- is `other`; see is_material_supply."""
    dn = desc_norm or ""
    if is_material_supply(dn) or is_apparel(dn):      # round 9: apparel
        return "other"
    dn = family_text(dn)                 # round 8: abbreviations read as words
    q = _QUALIFIER_RE.search(dn)
    head = dn[:q.start()] if q else dn
    probe = _NOT_REBUILD_RE.sub(" ", head)
    if not _REBUILD_RE.search(probe) or _RESET_RE.search(probe):
        return _family_of(dn)
    built = _REMOVAL_WORD_RE.sub(" ", dn) if _REMOVAL_WORD_RE.search(probe) else dn
    w = _REPLACE_WITH_RE.search(built)
    if w:
        f = _family_of(w.group(1) or w.group(2))
        if f not in ("other", "concrete_misc"):
            return f
        if f == "concrete_misc" and _family_of(built[:w.start()]) == "asphalt_pavement":
            # "remove asphalt pavement and replace with 8" reinforced
            # concrete": the pavement rebuilt in concrete
            return "conc_pavement"
    return _family_of(built)


_SIZE_IN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*in\b")
_DECIMAL_LEAD_RE = re.compile(r"(?:^|\s)0\s+$")
SIZE_IN_MAX = 144.0      # no pipe, shaft or casing in the corpus is larger
# A box culvert is sized by span x rise in feet (item_specs.py reads the same
# pair).  norm_desc has dropped the foot marks, so "6'x4'" arrives as "6 x4",
# "(6'-0" x 4'-0")" as "6 0 in x 4 0 in", TxDOT's "(5 FT X 3 FT)" as "5 ft x 3
# ft" and "(5' Span x 3' Rise)" as "5 span x 3 rise".
_BOX_FTIN_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(\d{1,2})\s*in\s*x\s*(\d{1,2})\s+(\d{1,2})\s*in\b")
_BOX_DIM_RE = re.compile(r"(?<![\d/])(\d{1,3}(?:\.\d+)?)\s*(?:ft|foot|feet)?\s*(?:span\s*)?x\s*"
                         r"(\d{1,3}(?:\.\d+)?)(?![\d/])(\s*(?:in\b|inch))?")
_BOX_AREA_RE = re.compile(r"(?<!\d)(\d{1,3})(?:\s+(?:to\s+)?(\d{1,3}))?\s*(?:sf|sq\s*ft|square\s+f\w+)\s+(?:effective\s+)?opening")


def box_culvert_size(desc_norm):
    """Opening area, span x rise in SQUARE FEET, of a box culvert line, or None.

    This is what size_in carries for the box_culvert family (round 5), so that
    build_master.size_bucket separates box sizes on its existing edges -- 4x3
    = 12 sf (9-13), 5x4 = 20 (19-25), 6x4 = 24 (19-25), 8x5 = 40 (37+) --
    instead of one unsized cell holding every size (Frisco 4x3 RCB at $421
    and $473 was priced from a cell at $843-851).  It is NOT a diameter: a
    box cell is only ever compared with other box cells.  Dimensions in
    inches on both sides ("72 x 48") are converted; an explicit inch mark
    ("8 in x 4 in RCBC plug") is no box size."""
    dn = desc_norm or ""
    m = _BOX_FTIN_RE.search(dn)
    if m:
        a = int(m.group(1)) + int(m.group(2)) / 12.0
        b = int(m.group(3)) + int(m.group(4)) / 12.0
    else:
        a = b = None
        for m in _BOX_DIM_RE.finditer(dn):
            if m.group(3) or re.search(r"\bin\s*$", dn[:m.start()]):
                continue
            a, b = float(m.group(1)), float(m.group(2))
            break
        if a is None:
            # the opening stated as an area: "(111-120 SF Effective Opening)"
            m = _BOX_AREA_RE.search(dn)
            if not m:
                return None
            lo = float(m.group(1))
            hi = float(m.group(2)) if m.group(2) else lo
            return round((lo + hi) / 2.0, 2) if 1 <= lo <= hi <= 400 else None
    if 1 <= a <= 20 and 1 <= b <= 20:
        return round(a * b, 2)
    if 24 <= a <= 240 and 24 <= b <= 240:          # both written in inches
        return round(a * b / 144.0, 2)
    return None


_THICK_AFTER_RE = re.compile(r"\s*(?:thick\w*|thk)\b")


def size_is_thickness(desc_norm):
    """True when the size extract_size_in reads is a THICKNESS ("4'x4' 4" Thick Reinf. Conc. Pad")."""
    dn = desc_norm or ""
    for m in _SIZE_IN_RE.finditer(dn):
        if len(m.group(1)) >= 3 and _DECIMAL_LEAD_RE.search(dn[:m.start()]):
            continue
        if float(m.group(1)) <= SIZE_IN_MAX:
            return bool(_THICK_AFTER_RE.match(dn, m.end()))
    return False


def extract_size_in(desc_norm):
    """The first "N in" of the normalised description that can be a size.

    Round 5: a value above SIZE_IN_MAX is no size -- an item number glued to
    one ("724 in" is item 7's 24" R.C.P., Edinburg Brennaleen, priced at
    $1,184/LF), or a decimal whose point norm_desc dropped ("T=0.281"" reads
    "0 281 in"); so is a three-digit "0 NNN in" ("0.125"").  "0 6 in" stays a
    size: it is TxDOT's range "(0-6")".  The next candidate is taken instead.
    A box culvert's size is its opening area in square feet
    (box_culvert_size)."""
    dn = desc_norm or ""
    if "x" in dn and item_family(dn) == "box_culvert":
        return box_culvert_size(dn)
    if "hydrant" in dn and item_family(dn) == "fire_hydrant":
        return None                      # round 8: a hydrant assembly is not sized by its lead
    for m in _SIZE_IN_RE.finditer(dn):
        if len(m.group(1)) >= 3 and _DECIMAL_LEAD_RE.search(dn[:m.start()]):
            continue
        v = float(m.group(1))
        if v <= SIZE_IN_MAX:
            return v
    return None


# Tier 1 = the bid opening/closing event itself (what we want to date the prices by).
# Tier 2 = weaker signals (advertised, issued, received) used only as a fallback.
BID_DATE_T1 = re.compile(
    r"bids?\s*open|opened|bid\s*due|due\s*date|close\s*date|closing|bid\s*opening|"
    r"solicitation\s*due|bids?\s*received|tabulat", re.I)
BID_DATE_T2 = re.compile(r"issue\s*date|received|advertis|addend|date|"
                         # (round 9, .30) the opening line of a tab that names no event: a weekday
                         # ("Thursday, May 19, 2022 @ 11:00 A.M."), a clock time ("2:00 PM on Monday,
                         # August 11, 2025") or the engineer's "Certified on: June 6, 2024"
                         r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|"
                         # (an opening is set on the quarter hour; "4:59 PM", "11:18 AM" or a time with
                         # seconds, "12:05:04 PM", is a print / plot stamp)
                         r"(?<![:\d])\d{1,2}:(?:00|15|30|45)(?!:\d)\s*[ap]\.?\s?m\b|\bcertif\w*\s+(?:on|date)\b", re.I)
# A 'date' line that dates the WORK, not the bid (parser round 3): Tarrant
# County F2025208 (Off-System Bridge Repairs) prints only each bidder's
# 'Earliest Possible Start Date 11/2/2025 12/1/2025 11/3/2025', and the latest
# of them, 2025-12-01, became the bid date -- three weeks after the
# 2025-11-11 award.  Such a line is no weak bid date (a line that also names
# the opening, BID_DATE_T1, still counts).
NOT_BID_DATE_RE = re.compile(
    r"start(?:ing)?\s*date|date\s*(?:of|to)\s*start|completion\s*date|"
    r"notice\s*to\s*proceed|substantial(?:ly)?\s*complet|"
    # (round 9) a print or plot stamp is no bid date: "Printed ...", a page footer "Page 1 of 12
    # 6/27/2025 4:59 PM" (a day after the opening), a CAD plot "5/19/2022 12:05:04 PM ...\EXH-PLAN.dgn"
    r"\bprint(?:ed)?\b|\bgenerated\b|\bpage\s+\d+\s+of\s+\d+\b|\.(?:dgn|dwg|dxf|xlsx?|docx?|pdf)\b|"
    r"\b[a-z]:\\", re.I)


def _dates_in(s):
    out = []
    for m in DATE_NUM.finditer(s):
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yy <= 2035:
            out.append("%04d-%02d-%02d" % (yy, mm, dd))
    for m in DATE_TXT.finditer(s):
        mm = MONTHS.get(m.group(1).lower())
        if mm:
            dd, yy = int(m.group(2)), int(m.group(3))
            if 1 <= dd <= 31 and 2000 <= yy <= 2035:
                out.append("%04d-%02d-%02d" % (yy, mm, dd))
    return out


def find_dates(strings):
    """Return (bid_date, source, all dates seen).

    Strings must be whole ROWS, not single cells: spreadsheets routinely put the
    label ("Solicitation Due Date:") and the value ("6/16/2026 at 2:00pm") in
    adjacent cells, so per-cell scanning finds the date with no context.
    Where several opening dates appear (an addendum extended the deadline) the
    LATEST is the real one.
    """
    t1, t1_tab, t2, every = [], [], [], []
    for s in strings:
        ds = _dates_in(s)
        if not ds:
            continue
        every.extend(ds)
        if BID_DATE_T1.search(s):
            # A line that says only 'tabulation' dates the TAB -- often the
            # day it was printed, in the page footer beside the workbook's
            # name ('6/19/2023 Page 7 of 14 Day-Miar-Bid-Bid-Tabulation-
            # 20230619.xlsx' under 'Bid Opening Date: June 14, 2023', Mansfield
            # 23-5444).  It counts only where no line names the opening
            # itself (_PAGE_OPENING_RE) -- parser round 3.
            (t1 if _PAGE_OPENING_RE.search(s) else t1_tab).extend(ds)
        elif BID_DATE_T2.search(s) and not NOT_BID_DATE_RE.search(s):
            t2.extend(ds)
    # Taking the LATEST candidate is right for ONE solicitation -- an addendum
    # that extends the deadline makes the later date the real one.  It is wrong
    # for a council AGENDA PACKET, which bundles many unrelated documents: the
    # latest opening-ish date in a 170-page bundle belongs to some other item,
    # or to a later tabulation or award memo.
    #
    # Measured: 115 files hold T1 candidates spanning more than 60 days, 95 of
    # them spanning more than a YEAR.  Grand Prairie's packets are the extreme
    # case -- Kirby Creek has 15 distinct dates spanning 8,353 days.  Verified
    # damage: Dry Branch Channel Slope Reconstruction was dated 2024-04-30
    # against a true bid opening of 2023-11-13, which puts a 2023 letting into
    # 2024 and therefore into the wrong inflation year.
    #
    # The real fix is to resolve the date PER SECTION, from the nearest
    # preceding "BID OPENING" header above each table.  Until that exists, a
    # wide spread is marked in the SOURCE rather than silently trusted: the
    # downstream `date_precision` guard tests for exactly "bid_open_or_due", so
    # this suffix automatically keeps these rows out of the escalation index
    # while leaving their prices usable for everything date-insensitive.
    def _span_days(ds):
        try:
            xs = sorted({d[:10] for d in ds})
            if len(xs) < 2:
                return 0
            import datetime
            a = datetime.date.fromisoformat(xs[0])
            b = datetime.date.fromisoformat(xs[-1])
            return (b - a).days
        except Exception:
            return 0

    # A bid cannot open in the future, so a future candidate is something else
    # the document happens to mention -- a contract end date, a warranty expiry,
    # a debt-service schedule.  Combined with "take the latest", such a date
    # always wins.  Measured: 134 rows across 2 collin_county files, one dated
    # 2028-12-31 from a maintenance contract's end date (102 rows parked in a
    # year that does not exist yet).  Dropping them lets the date fall back to a
    # weaker but real tier rather than staying wrong -- a graded fallback, never
    # an empty date, because no date at all pushes the file into unknown_date
    # and that is worse than an imperfect one.
    import datetime as _dt
    _today = _dt.date.today().isoformat()
    if not [x for x in t1 if x[:10] <= _today]:
        t1 = t1 + t1_tab
    t1 = [x for x in t1 if x[:10] <= _today]
    t2 = [x for x in t2 if x[:10] <= _today]

    AMBIG_DAYS = 60
    if t1:
        suffix = "_ambiguous_span" if _span_days(t1) > AMBIG_DAYS else ""
        return max(t1), "bid_open_or_due" + suffix, sorted(set(every))
    if t2:
        suffix = "_ambiguous_span" if _span_days(t2) > AMBIG_DAYS else ""
        return max(t2), "weak_issue_or_received" + suffix, sorted(set(every))
    return "", "MISSING", sorted(set(every))

# ----------------------------------------------------------------- core parse


def merged_price_total_caption(cell):
    """'UNIT TOTAL PRICE AMOUNT' -- the two-line captions 'UNIT PRICE' and
    'TOTAL AMOUNT' of two adjacent columns, read as ONE cell when the table
    lost the rule between them (line by line: UNIT TOTAL / PRICE AMOUNT) --
    or the same two captions side by side ('UNIT PRICE TOTAL COST')."""
    w = txt(cell).split()
    # line-interleaved: line 1 holds the first word of each caption, line 2
    # the second ('UNIT TOTAL / PRICE AMOUNT', 'Unit Total / Price')
    if (len(w) == 4 and PRICE_HDR.match(w[0] + " " + w[2])
            and TOTAL_HDR.match(w[1] + " " + w[3])):
        return True
    if len(w) == 3 and PRICE_HDR.match(w[0] + " " + w[2]) and TOTAL_HDR.match(w[1]):
        return True
    # side by side ('UNIT PRICE TOTAL COST', 'UNITCOST TOTAL')
    for k in (1, 2):
        if (0 < k < len(w) and PRICE_HDR.match(" ".join(w[:k]))
                and TOTAL_HDR.match(" ".join(w[k:]))):
            return True
    return False


# The total captions added 2026-11-19 pair only with an EXPLICIT price
# caption, never with a bare 'UNIT': that word is as often the unit-of-measure
# column.  Richardson 86-23 (Cottonwood Park pedestrian bridge) heads its
# names row 'ITEM NO. | DESCRIPTION | UNIT | BID TOTAL | Dean Construction ...',
# where 'BID TOTAL' captions the QUANTITY column; read as a pair, the names
# row became a one-pair header and all 133 priced rows of the letting were
# lost.  The captions that were already in TOTAL_HDR keep their old behaviour.
_TOTAL_HDR_ADDED = re.compile(
    r"^\s*(sub-?\s*total(?:\s*(?:cost|price|amount))?|bid\s*total|bid\s*value|amount\s*bid|"
    r"total\s*\$|total\s*item\s*cost|bid\s*estimate)\s*$", re.I)
_BARE_UNIT_HDR = re.compile(r"^\s*unit\s*$", re.I)
# A bare 'Cost' is no total caption of its own (see detect_price_pairs).
_BARE_COST_HDR = re.compile(r"^\s*cost\s*$", re.I)
_EXPLICIT_PRICE_HDR = re.compile(
    r"^\s*(unit\s*price|unit\s*cost|bid\s*unit\s*price|unit\s*price\s*bid|unit\s*bid\s*price)\s*$",
    re.I)


def _is_price_pair(a, b):
    return bool(PRICE_HDR.match(a) and TOTAL_HDR.match(b)
                and not (_BARE_UNIT_HDR.match(a) and _TOTAL_HDR_ADDED.match(b)))


def detect_price_pairs(header_cells):
    pairs = []
    for i in range(len(header_cells) - 1):
        if _is_price_pair(header_cells[i], header_cells[i + 1]):
            pairs.append((i, i + 1))
    # ---- a bidder column whose two captions merged into one cell ----------
    # North Richland Hills Bid 26-001 heads its first bidder (DDM, the low
    # bid) 'UNIT TOTAL / PRICE AMOUNT' in ONE cell with an empty cell beside
    # it, while every other bidder has 'UNIT PRICE' | 'TOTAL AMOUNT'; the data
    # rows still put the unit price under the merged cell and the extension
    # beside it.  Read only where the row already holds ordinary pairs -- the
    # merged caption then completes a header that was found, it never makes
    # a header row of its own -- and only into an empty right-hand cell.
    if pairs:
        taken = {c for p in pairs for c in p}
        for i in range(len(header_cells) - 1):
            if (i not in taken and (i + 1) not in taken and not header_cells[i + 1]
                    and merged_price_total_caption(header_cells[i])):
                pairs.append((i, i + 1))
                taken.update((i, i + 1))
            # ... and a bare 'Cost' after an explicit unit-price caption, in a row
            # that already holds ordinary pairs: NRH RFB 24-021 heads bidders 2
            # and 4 of its five 'Unit Price | Cost' (the others 'Unit Price | Item
            # Cost'), and both were lost.  Never a header on its own: Grand
            # Prairie's Kirby Creek packet has a cost-participation table 'UNIT
            # PRICE | COST', Rowlett's tank tab 'UNIT / PRICE | COST' captions.
            elif (i not in taken and (i + 1) not in taken
                  and _EXPLICIT_PRICE_HDR.match(header_cells[i])
                  and _BARE_COST_HDR.match(header_cells[i + 1])):
                pairs.append((i, i + 1))
                taken.update((i, i + 1))
        pairs.sort()
    # ---- a price caption spanning two columns ------------------------------
    # North Richland Hills 6222 gives every bidder THREE columns, 'Unit Bid
    # Price | (blank) | Amount Bid': the price caption is merged over two
    # columns, the price sits under its first half and the extension two
    # columns on.  No adjacent pair exists in such a row, so it was no header.
    # Read only then, only for a multi-word price caption (never a bare
    # 'Unit', which is as often the UOM column), and only as a SERIES of two or
    # more such blocks -- one bidder's gap is not a template.
    if not pairs:
        gapped = [(i, i + 2) for i in range(len(header_cells) - 2)
                  if len(header_cells[i].split()) >= 2 and PRICE_HDR.match(header_cells[i])
                  and not header_cells[i + 1] and TOTAL_HDR.match(header_cells[i + 2])]
        if len(gapped) >= 2:
            pairs = gapped
    return pairs


def detect_meta_cols(header_cells, price_start):
    cols = {"item": None, "desc": None, "unit": None, "qty": None}
    limit = price_start if price_start else len(header_cells)
    # An explicit "DESCRIPTION" always beats the bare-"ITEM" fallback, whatever
    # order they appear in.  Without this, "ITEM | DESCRIPTION" binds desc to
    # the item column and the real descriptions are never read -- see DESC_HDR_WEAK.
    # A tab headed "BID ITEMS | UNIT | QTY" has no DESCRIPTION column at all, so
    # desc and item pointing at the same column is CORRECT there and is left alone.
    desc_strong = False
    for i, h in enumerate(header_cells[:limit]):
        if not h:
            continue
        # 'UNI T', 'DESCRIPTIO N': a caption broken by a stray space is read
        # squeezed as well (_squeeze_letters) -- for the item, description and
        # unit columns.  NOT for the quantity: on Crowley's TNP pump-station
        # tab a squeezed 'QUANTIT Y' gave the table parse (12 of 42 rows) the
        # only checkable arithmetic, and read_pdf_best's "evidence beats no
        # evidence" rule then threw away the fuller coordinate parse.
        hs = _squeeze_letters(h) or h
        if cols["item"] is None and (ITEM_HDR.search(h) or ITEM_HDR.search(hs)):
            cols["item"] = i
        if DESC_HDR_STRONG.search(h) or DESC_HDR_STRONG.search(hs):
            if not desc_strong:
                cols["desc"] = i
                desc_strong = True
        elif not desc_strong and cols["desc"] is None and (DESC_HDR_WEAK.match(h)
                                                            or DESC_HDR_WEAK.match(hs)):
            cols["desc"] = i
        if cols["unit"] is None and (UOM_HDR.match(h) or UOM_HDR.match(hs)):
            cols["unit"] = i
        # A caption cell that also named the ITEM and the DESCRIPTION columns
        # is a merged caption ('Line # Description Mfg QTY' over Lancaster RFP
        # 2022-12's item numbers, legible once the clipped text over it is
        # gone): it is not the quantity column, whose numbers stand elsewhere.
        if cols["qty"] is None and QTY_HDR.search(h) and not (i == cols["item"] == cols["desc"]):
            cols["qty"] = i
    if cols["unit"] is None:
        # (round 11) the other unit captions (UOM_HDR_MORE), never the item, description or quantity column
        for i, h in enumerate(header_cells[:limit]):
            if h and i not in (cols["item"], cols["desc"], cols["qty"]) and \
                    (UOM_HDR_MORE.match(h) or UOM_HDR_MORE.match(_squeeze_letters(h) or h)):
                cols["unit"] = i
                break
    return cols


CITY_STATE_RE = re.compile(
    r"^[A-Z][A-Za-z.\-' ]*,\s*(TX|OK|AR|LA|NM|KS|MO|CO|AZ|CA|FL|GA|TN|MS|AL|IL|OH|"
    r"PA|NY|NC|SC|VA|UT|NV|IA|NE|Texas|Oklahoma|Arkansas|Louisiana)"
    r"\.?([,\s]+\d{5}(-\d{4})?)?$", re.I)     # 'Dallas, TX, 75204': a comma before the ZIP too
# The letting authority printed above the bidder columns is not a bidder.
OWNER_RE = re.compile(r"^(city|town|county|village|borough)\s+of\s+[A-Za-z .'\-]+$", re.I)

# Unanchored versions, for finding where an address STARTS inside a cell that
# glued the name and the address together with single spaces.  ADDRESS_RE and
# CITY_STATE_RE are anchored (`^`) because they are used to judge a whole
# segment; these are used to locate a cut point, which is a different question.
# FM / CR: a Texas Farm-to-Market or County Road names the road BEFORE its
# number ('1017 S. FM 157'), so without them the cut fell at the city and
# left 'SYB CONSTRUCTION CO., INC 1017 S. FM 157' as the bidder (NRH 26-001).
_ST_TYPE = (r"(?:dr|rd|blvd|st|ave|hwy|ln|trl|ste|suite|pkwy|cir|ct|way|pl|"
            r"road|drive|street|avenue|highway|lane|court|place|boulevard|"
            r"circle|parkway|trail|freeway|fwy|expressway|expwy|fm|cr)")
_ADDR_ANY_RE = re.compile(
    # "P.O. Box 999"
    r"\bp\s*\.?\s*o\s*\.?\s*box\b"
    # "4006 Belt Line Road", "5112 Sun Valley Dr.", "3934 S. Hwy 287" -- the
    # street name runs up to three words, which is why a one-word pattern cut
    # "DDM Construction Corp. 4006 Belt Line Road" in the wrong place.
    r"|\b\d{1,6}\s+(?:[A-Za-z.'\-]+\s+){0,3}" + _ST_TYPE + r"\b\.?"
    # "Addison, TX 75001"
    r"|\b[A-Z][A-Za-z.\-' ]{1,24},\s*(?:TX|OK|AR|LA|NM|KS|MO|CO|AZ|CA|FL|GA|TN|"
    r"MS|AL|IL|OH|PA|NY|NC|SC|VA|UT|NV|IA|NE)\b", re.I)
_PHONE_RE = re.compile(r"\b\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}\b")

# ---- a firm name running straight into its city ----------------------------
# CITY_STATE_RE judges a WHOLE segment, and '[A-Z][A-Za-z.\-' ]*,' happily
# swallows a firm in front of the city when nothing separates them:
# Arlington's Lower Johnson Creek pond dredging heads its only responsive
# bidder 'Dredgit Corporation Houston, Texas' in one single-spaced cell, the
# cell read as a city line, the name was discarded with it, and all 20 of the
# letting's priced rows dropped as no_bidder_name.  The firm ends at its legal
# suffix, so a segment that is <firm ending in Inc/LLC/Corporation/...> +
# <a city line> keeps the firm.  Greedy, so 'Renda Co Inc Roanoke, Texas'
# keeps 'Renda Co Inc'; a bare city line has no suffix and still goes.
_FIRM_THEN_CITY_RE = re.compile(
    r"^(?P<firm>.*\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|"
    r"company|lp|l\.p|llp|pllc)\b\.?)[\s,]+(?P<city>\S.*)$", re.I)


def _firm_before_city(p):
    """'Dredgit Corporation Houston, Texas' -> 'Dredgit Corporation'; '' for a
    segment that is not a suffixed firm followed by a city line."""
    m = _FIRM_THEN_CITY_RE.match(p or "")
    if m and len(m.group("firm").strip()) >= 3 and CITY_STATE_RE.match(m.group("city")):
        return m.group("firm").strip(" ,")
    # A firm with no legal suffix, ending in its trade word, followed by a full
    # city line with its ZIP: Arlington PWSW21001 (Safe Route to School) heads
    # its bidders 'Palmer Hall Construction / Dallas, TX, 75204' and 'Fain Group
    # / Fort Worth, TX, 76161'.  The city must be one to three capitalised
    # words and the ZIP must be there, so no firm is cut inside its own name.
    m = _FIRM_TRADE_THEN_CITY_RE.match(p or "")
    if m and len(m.group("firm").strip()) >= 3:
        return m.group("firm").strip(" ,")
    return ""


_FIRM_TRADE_THEN_CITY_RE = re.compile(
    r"^(?P<firm>.*\b(?:construction|constructors?|contracting|contractors?|services?|group|"
    r"enterprises?|industries|paving|utilit(?:y|ies)|concrete|excavation|excavating|builders?|"
    r"infrastructure|development|sitework|civil|underground|pipeline|trucking|erosion|"
    r"environmental|structures|demolition|grading|earthworks?|landscap\w*)\b\.?),?\s+"
    r"(?P<city>[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,2},\s*(?:TX|Texas|OK|Oklahoma|"
    r"LA|Louisiana|AR|Arkansas|NM)\.?[,\s]+\d{5}(?:-\d{4})?)$", re.I)


# ... and into its street address, on one single-spaced line: Mansfield
# 21-4017 heads every bidder 'Ed Bell Constructin Co. 10605 Harry Hines
# Dallas, Texas 75220 214-358-6581'.  The split in strip_address needs a line
# break or a double space, the anchored address patterns test a segment from
# its start, so the whole line was kept as the name.  The address begins at a
# street number (or P.O. Box) whose remainder reaches ', <state>' without
# passing a legal suffix -- 'Excel 4 Construction, LLC 5733 Hart Street Fort
# Worth, Texas' cuts at 5733, not at the 4 inside the firm's name.
_STATE_NAME = (r"(?:TX|Texas|OK|Oklahoma|AR|Arkansas|LA|Louisiana|NM|KS|MO|CO|AZ|CA|FL|GA|"
               r"TN|MS|AL|IL|OH|PA|NY|NC|SC|VA|UT|NV|IA|NE|Nebraska)")
_ADDR_TAIL_RE = re.compile(
    r"^(?:\d{1,6}\b|p\s*\.?\s*o\s*\.?\s*box\b)(?:(?!\b(?:inc|llc|l\.l\.c|corp|ltd|l\.p|lp)\b).)*?"
    r",\s*" + _STATE_NAME + r"\b", re.I)
# ... or a street address that never reaches a comma before its state: Dallas
# County IFB 2026-006-7091 heads Vendor No. 3 'CGC General Contractors, 3212
# Friendly Lane Haltom City TX 76117 PH: 682-730-6933' in one cell, and the
# whole line went out as the firm.  A street number followed by at most three
# words and a street type ('3212 Friendly Lane', '4006 Belt Line Road') starts
# an address as well.
_STREET_START_RE = re.compile(
    r"^\d{1,6}\s+(?:[A-Za-z.'\-]+\s+){0,3}" + _ST_TYPE + r"\b\.?", re.I)
# A legal suffix 'Co.' after a comma is the firm's, not Colorado's: Keller 25-346
# names 'Bernal Commercial Construction, Co.', which CITY_STATE_RE took for a
# city line, so the name went out as 'Bernal'.  Only after a trade or legal
# word (_FIRM_WORD_RE) -- 'Denver, CO' stays a city.
_FIRM_CO_RE = re.compile(r",\s*co\.?\s*$", re.I)


def _firm_comma_co(p):
    m = _FIRM_CO_RE.search(p or "")
    return bool(m and _FIRM_WORD_RE.search(p[:m.start()]))


def _firm_before_address(p):
    """'Ed Bell Constructin Co. 10605 Harry Hines Dallas, Texas 75220' ->
    'Ed Bell Constructin Co.'; '' when the segment holds no such address, or
    when what precedes it is no firm (an address-only cell stays as before)."""
    for m in re.finditer(r"(?<![\w-])(?:\d{1,6}\b|p\s*\.?\s*o\s*\.?\s*box\b)", p or "", re.I):
        tail = (_ADDR_TAIL_RE.match(p[m.start():]) or _STREET_START_RE.match(p[m.start():])
                # (round 10) a P.O. Box starts an address whatever follows it: Cockrell Hill's
                # 'Saber Development Corp. P . O . Box 540186 Dallas,' breaks before the state
                or _PO_BOX_START_RE.match(p[m.start():])
                if m.start() >= 3 else None)
        # an address names no trade: 'Item 12. Ed Bell Construction 10605
        # Harry Hines Blvd. Dallas, Texas' starts its address at 10605, not
        # at the agenda number 12 (Grand Prairie Kirby Creek)
        if not tail or _FIRM_WORD_RE.search(tail.group(0)):
            continue
        head = p[:m.start()].strip(" ,;-")
        if (len(head) >= 3 and re.search(r"[A-Za-z]{2}", head) and not head[0].isdigit()
                and not _ADDR_ANY_RE.search(head) and not OWNER_RE.match(head)
                and not _caption_only(head)):
            return head
        return ""
    return ""


# ... or into a STREET address with no city or state after it: Grand Prairie's
# Phase 3 Water Line tab heads the engineer's column 'Freese and Nichols, Inc.
# 801 Cherry Street, Suite 2800 Jane Doe, P.E. (555)555-0100
# jane.doe@example.com' in one cell, and the whole cell was kept as the
# name.  The firm ends at its legal suffix; what follows must open with a
# street number and a street type within four words ('801 Cherry Street').
_SUFFIX_END_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"lp|l\.p|llp|pllc)\b\.?,?$", re.I)
_STREET_START_RE = re.compile(r"^\d{1,6}\s+(?:[A-Za-z.'\-]+\s+){0,3}" + _ST_TYPE + r"\b", re.I)


def _firm_before_street(p):
    """'Freese and Nichols, Inc. 801 Cherry Street, Suite 2800 ...' ->
    'Freese and Nichols, Inc.'; '' when no suffixed firm runs into a street."""
    for m in re.finditer(r"(?<![\w-])\d{1,6}\b", p or ""):
        head = p[:m.start()].strip(" ,;-")
        if (len(head) >= 3 and _SUFFIX_END_RE.search(head)
                and _STREET_START_RE.match(p[m.start():])):
            return head.rstrip(",")
    return ""


# (round 10, see strip_address) a contact line, a firm named with a leading number, a spaced P.O. Box,
# a template's unfilled address placeholders ('Contractor 7 / Address Line 1 / Address Line 2' over
# Kennedale's Briar Court seventh column)
_CONTACT_LINE_RE = re.compile(
    r"^\s*(?:contact(?:\s+(?:person|name))?|attn|attention|representative|submitted\s+by|signed\s+by)"
    r"\s*[:.\-]", re.I)
_NUMBERED_FIRM_RE = re.compile(r"^\s*\d{1,5}(?:st|nd|rd|th)?\s+\d{0,3}[A-Za-z]")
_PO_BOX_START_RE = re.compile(r"p\s*\.?\s*o\s*\.?\s*box\s*#?\s*\d+", re.I)
_TEMPLATE_ADDR_ANY_RE = re.compile(
    r"\baddress\s+line\s*\d\b|\bcity\s*,\s*state(?:\s*,?\s*zip(?:\s*code)?)?\b", re.I)


def strip_address(v):
    """Return the company name with any postal address glued to it removed.

    Bid tabs routinely put the bidder's name and its address in ONE cell, often
    on separate lines.  looks_like_bidder used to reject the whole cell because
    it contained an address, discarding the NAME along with it -- and a row with
    no bidder name is dropped outright, which made this the largest single
    contributor to the 11,158 rows lost to `no_bidder_name`.

    Splits on line breaks and on runs of 2+ spaces after a word character,
    which is how these cells are laid out, then drops the address-shaped and
    owner-shaped pieces and keeps the rest.
    """
    raw = str(v or "")
    # (round 10) a template's unfilled address placeholders run into the name on one line
    # ('Contractor 7 Address Line 1 Address Line 2'): cut out wherever they stand
    raw = _TEMPLATE_ADDR_ANY_RE.sub("   ", raw)
    parts = re.split(r"[\r\n]+|(?<=[A-Za-z.)])\s{2,}", raw)
    keep = []
    for p in parts:
        p = p.strip(" ,;\t")
        if not p:
            continue
        # A firm with its city on the same line keeps the firm: 'Dredgit
        # Corporation Houston, Texas' (read whole, a city line), and 'SYB
        # Construction Company, Inc. Irving, Texas' -- 1,979 rows of the
        # 2026-11-19 line_items (34 names) carry the city in the bidder name,
        # which splits one contractor into several for bidder identity.
        firm = _firm_before_city(p) or _firm_before_address(p) or _firm_before_street(p)
        if firm:
            keep.append(firm)
            continue
        if _firm_comma_co(p) and not ADDRESS_RE.search(p):
            keep.append(p)                    # 'Bernal Commercial Construction, Co.'
            continue
        # (round 10) a contact line is no part of a name: Dallas County's Cockrell Hill tab prints
        # 'Contact: Mike Atkins, Pres.' under each firm (four bidders went out under their contact
        # lines).  (A template's unfilled 'Address Line 1' is cut out above, _TEMPLATE_ADDR_ANY_RE.)
        if _CONTACT_LINE_RE.match(p):
            continue
        # (round 10) ... and a firm whose NAME starts with a number is no street address:
        # Anna's 2022 parking tab names '308 Construction' over its first column, ADDRESS_RE
        # took the leading number for a street number, and its rows went unnamed (10 there, 34
        # in the 2023 re-bid).  A number, a trade or legal word, and no street type, city line
        # or phone number ('7 2L Construction LLC': a rank number over a firm, see bidder_names_for).
        if (_NUMBERED_FIRM_RE.match(p) and _FIRM_WORD_RE.search(p) and not _ADDR_ANY_RE.search(p)
                and not _PHONE_RE.search(p) and not CITY_STATE_RE.match(p)):
            keep.append(p)
            continue
        if ADDRESS_RE.search(p) or CITY_STATE_RE.match(p) or OWNER_RE.match(p):
            continue
        keep.append(p)
    if keep:
        return " ".join(keep).strip()

    # ---- the cell could not be SPLIT, so nothing survived ----------------
    # The splitter above needs a line break or a run of 2+ spaces.  Some
    # templates emit the merged header cell with SINGLE spaces:
    #
    #   'DDM Construction Corp. 4006 Belt Line Road, Ste. 230 Addison, TX 75001
    #    972-322-2021'
    #
    # which splits into one part, that one part contains an address, and the
    # loop above therefore discarded the company name along with it and
    # returned "".  looks_like_bidder judges what this function returns, so the
    # cell stopped looking like a bidder, the search walked PAST it to the
    # 'BIDDER 1' / 'BIDDER 2' column labels one row higher -- which do look like
    # names -- and every row was then dropped as a placeholder bidder.  2,714
    # rows corpus-wide, 708 of them from one North Richland Hills letting.
    #
    # The layout is always NAME first, ADDRESS after, so cut at the earliest
    # address marker and keep the prefix.  This changes nothing for the
    # splittable forms: it only runs when the old path produced nothing.
    s = raw.strip()
    if not s:
        return ""
    cuts = []
    for rx in (_ADDR_ANY_RE, _PHONE_RE):
        m = rx.search(s)
        if m and m.start() > 0:
            cuts.append(m.start())
    if cuts:
        head = s[:min(cuts)].strip(" ,;\t-")
        # A one- or two-character head is punctuation, not a company.
        if len(head) >= 3 and not OWNER_RE.match(head):
            return head
    return ""


# ---- a bidder-ORDINAL caption printed in the same cell as the name ---------
# Rowlett's Jones|Carter template heads each bidder block with ONE merged cell:
#     'BID NO. 1 Texas Civil Construction 121 Alexander Lane Royse City, TX 75189'
# NONBIDDER_RE rejects any cell that STARTS with "bid no." -- that alternative
# is there for the solicitation-number label ('Bid No. 2021-09') -- so the
# whole cell, company name included, was thrown away, bidder_names_for walked
# on up and found nothing, and every price under it dropped as
# no_bidder_name.  Only the engineer's column, whose cell carries no caption,
# survived: Rowlett 2021-09 Alley Group G reached the panel as an estimate with
# zero bids (152 of 171 rows lost, 8 of 8 bidders).
# The caption is an ORDINAL -- one or two digits -- with more text after it.
# A solicitation number is not stripped ('Bid No. 2021-09', 'Bid No. 25-17':
# the digits run on into a hyphen), and a caption standing alone ('BIDDER 3')
# is left whole for PLACEHOLDER_RE to drop.
#
# Two limits keep the caption from manufacturing bidders:
# * what follows it must be a FIRM, not a note on the column -- stripped,
#   'Contractor 1 (low bid)' left '(low bid)', which passed for a name;
# * an ordinal numbers a SERIES of bidder columns (BID NO. 1 ... BID NO. 8).
#   A lone 'BID NO. 12 Main Street Paving' over an unnamed column is the
#   solicitation number glued to the project title, and stripped it became
#   a bidder called 'Main Street Paving'.  So a caption is honoured only when
#   the header carries two or more distinct ordinals, or a lone No. 1 (a
#   one-bidder tab: Rowlett 2023-24's 'Contractor 1 - FNH Construction') --
#   see ordinal_series_ok, applied by bidder_names_for and read_pdf_coords.
BIDDER_CAPTION_RE = re.compile(
    r"^\s*(?:bid(?:der)?|contractor|vendor|respondent|proposer|offeror)s?"
    r"\s*(?:no\.?|number|#)?\s*#?\s*(?P<ord>\d{1,2})\s*[:.)\-]?\s+(?=\S*[A-Za-z])", re.I)
_GENERIC_NAME_RE = re.compile(
    r"^\W*(?:the\s+)?(?:apparent\s+)?(?:low(?:est)?|high(?:est)?|base|alternates?|alt|"
    r"totals?|estimates?|engineer'?s?|opcc|bids?|bidders?|prices?|amounts?|"
    r"unit\s+prices?|responsive|non-?responsive|awarded|recommended|selected|"
    r"winning|first|second|third)\b"
    r"(?:\s+(?:bids?|bidders?|prices?|amounts?|estimates?|totals?))?\W*$", re.I)


# ... and a RANK caption in front of the firm: Frisco 2412-042 (prepared by
# Quiddity) heads its first column 'BID NO. 1 - LOWEST BASE BID FLOW-LINE
# CONSTRUCTION, INC.', and the bidder read 'LOWEST BASE BID FLOW-LINE ...'
# (tail audit 2026-11-20).  The rank words go when a firm follows them; a rank
# caption standing alone ('Low Bidder') is left for _RANK_CAPTION_RE.
_RANK_PREFIX_RE = re.compile(
    r"^\W*(?:the\s+)?(?:apparent\s+)?(?:low(?:est)?|recommended|awarded|winning)\s+"
    r"(?:base\s+)?(?:bid(?:der)?s?|vendor|contractor|offer(?:or)?)\b\s*[:\-\u2013]?\s*", re.I)


def strip_rank_caption(s):
    """'LOWEST BASE BID FLOW-LINE CONSTRUCTION, INC.' -> 'FLOW-LINE
    CONSTRUCTION, INC.'; 'Low Bidder' alone comes back whole."""
    s = s or ""
    m = _RANK_PREFIX_RE.match(s)
    if not m:
        return s
    tail = s[m.end():]
    if not re.match(r"\s*[A-Za-z0-9&]", tail) or _GENERIC_NAME_RE.match(strip_address(tail) or tail):
        return s
    return tail


def strip_bidder_caption(v):
    """'BID NO. 1 Texas Civil Construction ...' -> 'Texas Civil Construction
    ...'; a caption followed by anything but a firm ('Contractor 1 (low bid)')
    comes back whole.  A rank caption before the firm goes too
    (strip_rank_caption)."""
    s = v or ""
    m = BIDDER_CAPTION_RE.match(s)
    if not m:
        return strip_rank_caption(s)
    rest = s[m.end():]
    if not re.match(r"\s*[A-Za-z0-9&]", rest):
        return s                         # '(low bid)', '- see note': an annotation
    if _GENERIC_NAME_RE.match(strip_address(rest) or rest):
        return s                         # 'apparent low bidder', 'base bid', 'estimate'
    return strip_rank_caption(rest)


def caption_ordinal(v):
    """The ordinal of a caption strip_bidder_caption would remove, else None."""
    m = BIDDER_CAPTION_RE.match(v or "")
    if not m or strip_bidder_caption(v) == (v or ""):
        return None
    return int(m.group("ord"))


def ordinal_series_ok(ords):
    """Two or more distinct ordinals, or a lone No. 1, number bidder columns."""
    s = {o for o in ords if o is not None}
    return len(s) >= 2 or s == {1}


# ---- an ALTERNATE caption printed in the bidder-name cell ------------------
# Some tabs price an alternate in a column block of its own and say so in the
# NAME cell instead of a section banner:
#     'HomeRun Construction, LLC Alternate# 1'   (Mesquite RFP 2022-062, p.2)
#     'Valvoline (Alternate)'   'Centerline Supply, Inc. ALT'
#     'Estrada Concrete Company LLC Alternate (Item 8-12)'
# Read as a name, each is a NEW BIDDER in the BASE bid.  Mesquite's Latimore
# Park reached the panel with ten base bidders, five of them its Alternate #1
# page, and its "low bid" became HomeRun's $750 alternate instead of its
# $350,130 base bid: the 119x out-of-fold outlier.  The caption must END the
# cell (an optional parenthetical may follow it) or be a parenthetical of its
# own, so a firm that merely has the word in its name ('Alternative Energy
# Services LLC') is not split.  The column then belongs to section ALT <n>
# (or ALT) and its bidder is the firm, which already bids the base.
#
# The caption must also be UNMISTAKABLE, because a wrong split takes a real
# base bid out of the base -- the low one, if the firm is low.  'Alternative'
# is never a caption ('Green Alternative', 'The Paving Alternative'), nor is
# a bare 'Alt'/'Alternate' ending a name ('Smith & Alt'), nor a parenthetical
# that merely STARTS with the word ('Acme Paving (Alternate bid included)' is
# a base bid that includes the alternate).  A caption is:
#   * 'Alternate' / 'Alt' with its NUMBER: 'Alternate# 1', 'Alternate 1',
#     'ALT #2', 'Alternate Bid No. 3' (a parenthetical may follow);
#   * exactly '(Alternate)', '(Alternate Bid)', '(Alternate #2)', '(Alt. 1)';
#   * 'Alternate (Item 8-12)' -- the item range stands for the number;
#   * a bare 'ALT' / 'Alternate' AFTER the firm's legal suffix, where no firm
#     name goes on: 'Centerline Supply, Inc. ALT'.
_ALT_CAP = r"(?:alternates?|alt\.?)"
NAME_ALT_TAIL_RE = re.compile(
    r"(?:[\s,;\-]*\(\s*" + _ALT_CAP + r"(?:\s*bid)?(?:\s*(?:no\.?|#))?\s*"
    r"(?P<pnum>\d{1,2})?\s*\)"
    r"|[\s,;\-]+" + _ALT_CAP + r"(?:\s*bid)?(?:\s*(?:no\.?|#))?\s*(?P<num>\d{1,2})\b\.?"
    r"(?:\s*\([^()]*\))?"
    r"|[\s,;\-]+" + _ALT_CAP + r"\s*\(\s*items?\s*\d[\d\s,&\-]*\))\s*$", re.I)
_SUFFIX_ALT_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|lp|l\.p|"
    r"llp|pllc)\.?\s*[,;\-]?\s*(?P<cap>" + _ALT_CAP + r")\s*$", re.I)


def split_alternate_caption(name):
    """('HomeRun Construction, LLC Alternate# 1') -> ('HomeRun Construction,
    LLC', 'ALT 1'); a name without such a tail comes back unchanged with ''."""
    s = name or ""
    m = NAME_ALT_TAIL_RE.search(s)
    if m:
        cut, num = m.start(), (m.group("num") or m.group("pnum") or "").strip(" #")
    else:
        m = _SUFFIX_ALT_RE.search(s)
        if not m:
            return s, ""
        cut, num = m.start("cap"), ""
    head = s[:cut].strip(" ,;-")
    if len(head) < 3 or not looks_like_bidder(head):
        return s, ""
    if re.search(r"(?:&|\+|\band|\bof|\bthe)\s*$", head, re.I):
        return s, ""                     # 'Smith & Alt 2': the word is the name's
    return head, ("ALT " + num.upper()) if num else "ALT"


# ---- a caption word broken by a stray space --------------------------------
# pdfplumber splits a word that wraps inside a narrow header cell: Mansfield
# 22-5047 (Pond Street) heads its quantity column 'QUANTIT Y'.  The anchored
# caption tests cannot see it, so the walk up from the price header took it as
# the first bidder's NAME (21 rows of the low bidder, McClendon, filed under
# 'QUANTIT Y' while the next pages named him), and the quantity column itself
# went unread.  A cell with a lone letter beside a longer word is also tested
# with its spaces squeezed out -- only ever to REJECT it as a name or to
# recognise a header, so a real firm with a lone initial ('A T Construction')
# is unaffected: squeezed, it matches no caption.
_LONE_LETTER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])")


def _squeeze_letters(s):
    """'QUANTIT Y' -> 'QUANTITY'; '' for a cell with no lone letter."""
    s = txt(s)
    if not s or not _LONE_LETTER_RE.search(s) or not re.search(r"[A-Za-z]{2}", s):
        return ""
    return re.sub(r"\s+", "", s)


def looks_like_bidder(v, captions=True):
    """Reject headers, section labels, addresses, numbers and computed columns."""
    # Judge the name, not an ordinal caption glued in front of it -- unless
    # the caller has found the caption to be no ordinal (captions=False).
    if captions:
        v = strip_bidder_caption(v)
    if not v or len(v) < 3:
        return False
    if re.match(r"^[\d.,$\s%/-]+$", v) or not re.search(r"[A-Za-z]", v):
        return False                      # no letter: '1~ 1 0.00' (OCR'd check marks)
    if PRICE_HDR.match(v) or TOTAL_HDR.match(v) or NONBIDDER_RE.match(v):
        return False
    sq = _squeeze_letters(v)
    if sq and (PRICE_HDR.match(sq) or TOTAL_HDR.match(sq) or NONBIDDER_RE.match(sq)):
        return False
    # Judge what is LEFT after the address is removed, not the raw cell: a cell
    # holding "ACME Paving, Inc. / 1234 Main St, Dallas, TX 75201" is a bidder.
    s = strip_address(v)
    if len(s) < 3:
        return False                      # the cell was nothing but an address
    if OWNER_RE.match(s) or NONBIDDER_RE.match(s):
        return False
    # (round 10) a form answer, an award remark or a legend printed over a bidder's column
    # (Dallas County's Cockrell Hill tab: 'Yes' / 'No' rows, 'Recommended for Award' over the
    # first firm, 'light gray shading - line item error' over the third)
    if _FORM_ANSWER_RE.match(s) or _REMARK_RE.search(s):
        return False
    return True


# (round 10, see looks_like_bidder)
_FORM_ANSWER_RE = re.compile(
    r"^\W*(?:yes|no|n/?a|none|y|n|x|true|false|ok|received|acknowledged|included|submitted|"
    r"provided|attached)\W*$", re.I)
_REMARK_RE = re.compile(
    r"^\W*(?:recommended|selected|approved)\s+(?:for\s+)?(?:award|contract|approval)\b|"
    # a RANK caption over a firm: 'Low Bidder', 'LOWEST BIDDER', Rockwall FM 549's '2nd LOWEST' ...
    # '9th LOWEST' (seven columns went out under their ranks)
    r"^\W*(?:(?:\d{1,2}(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
    r"tenth)\s+(?:low(?:est)?|high(?:est)?)|(?:the\s+)?(?:apparent\s+)?(?:low(?:est)?|high(?:est)?))"
    r"(?:\s+(?:responsive\s+|responsible\s+)*(?:bid(?:der)?|vendor|offer(?:or)?))?\W*$|"
    # a bid-form answer row: 'Bid Bond: 5% G.A.B', 'Addendum #1: Acknowledged (5/28/2021)'
    r"^\W*(?:bid\s+bond|bond|addend(?:um|a)[^:]{0,12})\s*:|"
    r"\b(?:shad(?:ing|ed)|highlighted|denotes?)\b|"
    r"\bline\s+item\s+errors?\b|\b(?:math(?:ematical)?|calculation|extension|arithmetic)\s+errors?\b",
    re.I)


# ---- a column CAPTION is not a firm ----------------------------------------
# The paths added for the 2026-11-19 zero-row files read names from rows the
# old walk never reached -- the real caption row above an inferred header, the
# rows under a header, the text above a ruled table -- and those rows are full
# of captions that looks_like_bidder lets through: Dallas County 2026-030 heads
# every bidder 'Total Unit Price', Collin County 2025-101 'NET PRICE PER UNIT'
# and 'PERCENT DISCOUNT (ENTER NEGATIVE VALUE)'.  A cell made of nothing but
# caption words names no firm.  Applied on those new paths only: the 147 rows
# line_items.csv already files under such a name ('Bid Amount', 'Item 13',
# 'each' -- Grand Prairie Fish Creek and Kirby Creek, Mesquite) come from a
# REAL header's walk, which this does not touch; they are reported, not moved.
_CAPTION_WORDS = frozenset("""
unit units price prices pricing cost costs amount amounts total totals subtotal sub
extended extension ext bid bids value values quantity quantities qty est estimated item
items no description desc uom of measure net per catalog current percent discount enter
negative vendor sku number annual usage line spec specification section required approx
approximate approximately plan contract schedule pay name in figures words and or the
each lump sum
""".split())


# ... and neither is a column's ordinal or rank label.  Frisco 2102-050 heads
# every page 'Low Bidder' over the first column, then 'BIDDER 1 ... BIDDER 5',
# then the firms: read from the rows under the inferred header, the labels
# named four columns 'BIDDER 2'..'BIDDER 5' and 832 priced rows dropped as
# placeholders.  (An engineer's estimate is NOT a label -- it is kept.)
_RANK_CAPTION_RE = re.compile(
    r"^\W*(?:the\s+)?(?:apparent\s+)?(?:low(?:est)?|high(?:est)?|first|second|third|"
    r"recommended|awarded|winning)\s+(?:bid|bidder|bids|vendor|contractor)\W*$", re.I)


def _caption_run(w):
    """True when the letters w ('quantity', 'unitprice') split into caption
    words of two or more letters."""
    if not w:
        return False
    ok = [True] + [False] * len(w)
    for i in range(1, len(w) + 1):
        ok[i] = any(ok[j] and w[j:i] in _CAPTION_WORDS for j in range(max(0, i - 14), i - 1))
    return ok[len(w)]


def _caption_only(s):
    """'Total Unit Price', 'NET PRICE PER UNIT', 'BIDDER 2', 'Low Bidder',
    'QUANTIT Y' -> True; any firm (or an engineer's estimate) -> False."""
    s = txt(s)
    if s and (PLACEHOLDER_RE.match(s) or _RANK_CAPTION_RE.match(s)):
        return True
    ws = re.findall(r"[a-z]+", s.lower())
    if bool(ws) and all(w in _CAPTION_WORDS for w in ws):
        return True
    sq = _squeeze_letters(s)
    return bool(sq) and _caption_run(re.sub(r"[^a-z]", "", sq.lower()))


def _money_caption(c):
    """'Unit Price', 'TOTAL', 'UNIT PRICE (NUMBER ONLY)' -> True."""
    s = re.sub(r"\([^()]*\)", " ", txt(c)).strip()
    return bool(s) and bool(PRICE_HDR.match(s) or TOTAL_HDR.match(s))


def bidder_names_for(grid, header_row, pairs, lookback=8, skip_captions=False, rank_key=None):
    """Walk up from the header row; a bidder name sits at (or just left of) its price col.

    Keeps walking past addresses / 'BASE BID' / 'Total Price' style labels rather than
    stopping at the first non-empty cell, which is what makes this robust across tabs.
    skip_captions: walk past caption-only cells too (_caption_only) -- set for an
    INFERRED header, whose real caption row may sit above it.
    rank_key: the file whose rank series a lone rank number may continue (round 11).
    """
    grid = _strip_rank_numbers(grid, header_row, pairs, lookback, rank_key)    # (round 10, 11)
    skip = set()
    if skip_captions:
        # Above an inferred header, a row with a PRICE caption over a money
        # column is the table's own caption row -- one whose pair went
        # unrecognised ('Unit Price | Value of Completed Work' in Grand
        # Prairie's materials-testing pay estimates) -- and its other cells
        # are captions too: 'Approved Quantity' and 'Value of Completed Work'
        # were read as two bidders of the Dry Branch and Briar Hill packets.
        money = {c for p in pairs for c in p}
        for r in range(max(0, header_row - lookback), header_row):
            if any(c < len(grid[r]) and _money_caption(grid[r][c]) for c in money):
                skip.add(r)

    first = min(p for p, _ in pairs) if pairs else 0

    def lone_code(row, c):
        # a CODE alone in the bidder columns' part of its row (see _CODE_TOKEN_RE)
        return bool(_CODE_TOKEN_RE.match(txt(row[c]))) and not any(
            txt(row[k]) for k in range(max(0, first - 1), len(row)) if k != c)

    # (round 10) A FORM row under the firms -- every cell over the bidder columns a form answer
    # ('Yes' / 'No'), an award remark, a legend or a contact line -- does not count against
    # `lookback`: Dallas County's Cockrell Hill tab prints seven of them between its firms and
    # the caption row (contact, bid bond, addenda, MWBE, taxpayer, a shading legend, 'Recommended
    # for Award'), and the firms sat one row past the walk.  Only such rows widen the window; an
    # empty row still counts, as before.
    def form_row(row):
        cells = [txt(row[k]) for k in range(max(0, first - 1), len(row)) if txt(row[k])]
        return bool(cells) and all(_FORM_ANSWER_RE.match(c) or _REMARK_RE.search(c)
                                   or _CONTACT_LINE_RE.match(c) for c in cells)

    def walk(pc, captions):
        budget, r = lookback, header_row
        while budget > 0:
            r -= 1
            if r < 0:
                break
            row = grid[r]
            if not form_row(row):
                budget -= 1
            if r in skip:
                continue
            for c in (pc, pc - 1):
                if (0 <= c < len(row) and looks_like_bidder(row[c], captions)
                        and not (skip_captions and _caption_only(row[c]))
                        and not (c != pc and lone_code(row, c))
                        and not (c != pc and pc < len(row) and re.search(r"\d", txt(row[pc])))):
                    # (the last: a LABEL left of the bidder's value in its own
                    # column -- 'Selected ($) | $ 0', 'Selected # | 1- 1 0.00'
                    # in Richardson 70-25 -- is no name)
                    return r, row[c]
        return None, ""

    # ---- a CAPTION row between the names and the header ------------------
    # Templates print acknowledgement rows under the bidder names -- 'Bid
    # Bond Received: Yes | Yes', 'Addendums Returned: Yes | Yes', 'Addendum
    # 1 | Addendum 1 | ...', 'Cooperative | Cooperative' -- and the walk took
    # the nearest of them for every column: Dallas County 2022-014-6929 put
    # both its bidders' 616 rows under one bidder called 'Yes', NRH RFB 24-021
    # all of its under 'Addendum 2'.  Names DIFFER from column to column; a
    # row that gives every column it names the same text is a caption row,
    # and the walk goes on past it.
    found = {pc: walk(pc, True) for pc, _ in pairs}
    for _ in range(lookback):
        by_row = {}
        for r, cell in found.values():
            if r is not None:
                by_row.setdefault(r, []).append(txt(cell).lower())
        caps = {r for r, vals in by_row.items() if len(vals) >= 2 and len(set(vals)) == 1}
        if not caps:
            break
        skip |= caps
        found = {pc: walk(pc, True) for pc, _ in pairs}
    cells = {pc: cell for pc, (r, cell) in found.items()}
    cut_rows = set()
    for pc, tc in pairs:
        r = found[pc][0]
        if r is not None and txt(cells[pc]) == txt(grid[r][pc]) and tc < len(grid[r]):
            joined = _name_split_at_rule(cells[pc], grid[r][tc])
            if joined != cells[pc]:
                cells[pc] = joined
                cut_rows.add(r)
    # A row the rules cut mid-word cuts every name in it, between words too:
    # Cedar Hill PW23-0009 page 2 also prints 'Engineer's | Estimate'.
    for pc, tc in pairs:
        r = found[pc][0]
        if (r in cut_rows and txt(cells[pc]) == txt(grid[r][pc]) and tc < len(grid[r])
                and _name_rest_at_rule(grid[r][tc])):
            cells[pc] = txt(cells[pc]) + " " + txt(grid[r][tc])
    # A 'BID NO. n' caption is an ordinal only as one of a series across the
    # bidder columns (ordinal_series_ok); a lone 'BID NO. 12 Main Street
    # Paving' is not, and its column is searched again without captions.
    ords = {pc: caption_ordinal(c) for pc, c in cells.items()}
    series = ordinal_series_ok(ords.values())
    if not series:
        for pc, o in ords.items():
            if o is not None:
                cells[pc] = walk(pc, False)[1]
    names = {}
    for pc, cell in cells.items():
        # Store the NAME, not the name-plus-address cell, so the same
        # contractor in two tabs canonicalises to one bidder.  Nor the
        # 'BID NO. 1' caption in front of it.
        if series:
            cell = strip_bidder_caption(cell)
        cell = _AGENDA_STAMP_RE.sub("", cell) if cell else cell
        names[pc] = _block_label_strip(strip_address(cell) or cell) if cell else ""
    # ---- an ESTIMATE caption over the pair's own column -------------------
    # The walk takes the cell just LEFT of a pair's price column when its own
    # is empty.  An engineer's estimate printed as the last column under a
    # caption on the row above the firms, with no firm of its own, then takes
    # its neighbour's name: Dallas County IFB 2026-006-7091 heads its columns
    # 'Vendor No. 1 .. Vendor No. 9 | ENGINEER's ESTIMATE' over a row of firms
    # that ends with 'Dredge Heavy Construction' (Vendor No. 9, non-responsive,
    # its prices blank), and the estimate's $3,813,475.43 went out as Dredge's
    # bid.  A pair whose name did not come from its own column, and whose own
    # column carries an estimate caption anywhere above the header, is the
    # estimate.
    for pc, _tc in pairs:
        if ENGINEER_RE.search(names.get(pc) or ""):
            continue
        if cells.get(pc) and any(pc < len(row) and row[pc] == cells[pc]
                                 for row in grid[:header_row]):
            continue                          # named from its own column
        for r in range(header_row - 1, -1, -1):
            v = txt(grid[r][pc]) if pc < len(grid[r]) else ""
            if (v and len(v) <= 40 and ENGINEER_RE.search(v) and _EST_CAPTION_RE.search(v)
                    and not re.search(r"\$|\d[\d,]*\.\d\d\b", v)):
                names[pc] = v
                break
    for pc, (r, _cell) in found.items():
        cap = _estimate_caption_above(grid, r, pc, pairs, lookback) if names.get(pc) else ""
        if cap and not ENGINEER_RE.search(names[pc]):
            # a name the walk took from the column to the LEFT, under a caption
            # in the pair's own column, is the neighbour's: the caption alone
            borrowed = pc < len(grid[r]) and not txt(grid[r][pc])
            own = any(pc < len(grid[rr]) and txt(grid[rr][pc]) == cap
                      for rr in range(max(0, r - lookback), r))
            names[pc] = cap if (borrowed and own) else cap + " " + names[pc]
    # ---- (round 10) a bidder FORM: labels in the price column, values in the total column ---------
    # Forney's tabs head each bidder 'Contractor | S-Co Incorporated', 'Address | 2329 County Road 348',
    # 'City | Jewett' ...; the label rows repeat over every pair and the walk skips them as caption rows,
    # so the firms went unnamed (or, one form merged into one cell -- 'Contractor Address City, State |
    # Triad Service Group, LLC 519 ...' -- the labels were the name).  A pair whose price column holds
    # the firm label ('Contractor', 'Bidder Name', ... followed by address labels only) with a firm
    # in its total column on the same row takes that firm.
    for pc, tc in pairs:
        for r in range(header_row - 1, max(-1, header_row - 1 - lookback), -1):
            row = grid[r]
            lab = txt(row[pc]) if pc < len(row) else ""
            val = txt(row[tc]) if tc < len(row) else ""
            if lab and val and _FORM_FIRM_LABEL_RE.match(lab) and looks_like_bidder(val):
                firm = strip_address(val) or val
                if _name_candidate(firm) and not _FORM_FIRM_LABEL_RE.match(firm):
                    names[pc] = firm
                break
    return names


# ---- (round 10) a RANK number over each firm, read into its name cell ------------------------------
# Watauga's tabs print '1' / 'Matcon LLC', '2' / 'Jeske Construction Company' over the pairs (lowest
# first, two bidders a page), and the table reads each as ONE cell, '1 Matcon LLC'.  Taken for a street
# number, '7 2L Construction LLC' and '5 Urban Ifraconstruction, LCC.' named nobody, and their pages
# took the previous page's names -- 2L's and Urban's prices went out under Westhill's and Tiseo's.  On a
# row above the header where two or more pair columns open with distinct, consecutive small numbers,
# the numbers are a rank series and are cut before the walk; a lone number stays ('308 Construction'
# is a firm, and so is the lone rank of a page with one bidder).
# (round 11, .32) the rank series a file's earlier tables were stripped of (read_pdf resets it per file):
# Watauga 26-003 prints its eleven firms two to a page, each with its rank ('1 Matcon LLC', '2 Jeske
# ...'), and the last page holds ONE firm, '11 Capko Concrete Structures LLC' -- one number is no series,
# so it stayed in the name.  A lone rank number that continues the series stripped on the file's earlier
# pages (10 seen, this one 11) is stripped too; anywhere else a lone leading number stays ('4 Star ...').
_RANK_SERIES = {}


def _strip_rank_numbers(grid, header_row, pairs, lookback, rank_key=None):
    cols = sorted({c for pc, _ in pairs for c in (pc, pc - 1) if c >= 0})
    out = None
    seen = _RANK_SERIES.get(rank_key) if rank_key is not None else None
    for r in range(max(0, header_row - lookback), header_row):
        row = grid[r]
        lead = {c: _LEAD_RANK_RE.match(txt(row[c])) for c in cols if c < len(row) and txt(row[c])}
        lead = {c: m for c, m in lead.items() if m}
        nums = sorted(int(m.group(1)) for m in lead.values())
        series = len(nums) >= 2 and len(set(nums)) == len(nums) and nums[-1] - nums[0] == len(nums) - 1
        lone = (len(nums) == 1 and seen is not None and nums[0] >= 2 and (nums[0] - 1) in seen)
        if series or lone:
            if out is None:
                out = list(grid)
            new = list(row)
            for c, m in lead.items():
                new[c] = txt(row[c])[m.end():].strip()
            out[r] = new
            if rank_key is not None:
                _RANK_SERIES.setdefault(rank_key, set()).update(nums)
    return out if out is not None else grid


# (round 10, see bidder_names_for) a bidder form's firm label; a rank number in front of a firm
_FORM_FIRM_LABEL_RE = re.compile(
    r"^\W*(?:contractor|bidder|vendor|company|firm)(?:'?s)?(?:\s+name)?"
    r"(?:\W+(?:address|city|state|zip|phone|fax|e-?mail|name))*\W*$", re.I)
_LEAD_RANK_RE = re.compile(r"^\s*(\d{1,2})\s+(?=\S*[A-Za-z])(?=.*[A-Za-z]{2})")


# ---- a council packet's agenda-item STAMP in a name cell (parser round 3) ---
# A council packet stamps every page in a corner with its agenda item ('Item
# 12.'), and a tab printed edge to edge takes the stamp into its top-right cell,
# above the last firm: Grand Prairie's 2025-09-03 committee packet read Day
# Miar Road's 'Ed Bell Construction' as 'Item 12. Ed Bell Construction', and
# its Fish Creek award packet 'A T Construction, LLC' as 'Item 20. A T
# Construction, LLC' -- the same firm as two bidders downstream.  An opening
# 'Item <number>.' (with its period, in front of more text) is the stamp.
_AGENDA_STAMP_RE = re.compile(r"^\s*item\s*#?\s*\d{1,3}[a-z]?\.\s+(?=\S*[A-Za-z])", re.I)


# ---- 'Contractor | firm', 'Address | ...': a bidder block's own LABELS -------
# Forney's tabs head every bidder's block with a small form, labels in the left
# column of the pair and values in the right: 'Contractor | Estrada Concrete
# Company, LLC', 'Address | 6150 Skyline Drive', 'City, State | ...', 'Phone |
# ...'.  The pair's name cell then reads 'Contractor Jeske Construction
# Company Address' (CIP 252, all 49 sheets' names, the same firm twice
# downstream).  A leading 'Contractor' / 'Bidder' / 'Vendor' (+ 'Name') label
# in front of more words, and a trailing 'Address' / 'Phone' / 'City, State'
# label, are no part of the firm.  ('... General Contractor' at the END of a
# name is the firm's own word and stays.)
_BLOCK_LABEL_HEAD_RE = re.compile(
    r"^\s*(?:(?:contractor|bidder|vendor|respondent|company|firm)(?:\s+name)?|"
    # (round 10) one city's 'Bidders Name & Address' over every firm (the names
    # went out as 'Bidders Name & Address Texas Standard Construction Ltd'); 'Name of Bidder'.  The
    # plural / possessive only before 'Name': 'Contractors Plus Roofing & Construction' is a firm.
    r"(?:contractor|bidder|vendor|respondent|company|firm)(?:'?s'?)?\s+name(?:\s*(?:&|and|/)\s*address)?|"
    r"name\s+of\s+(?:bidder|contractor|vendor|firm|company))"
    r"\s*[:\-]?\s+(?=\S)",
    re.I)
_BLOCK_LABEL_TAIL_RE = re.compile(
    r"\s+(?:address|phone|fax|e-?mail|city\s*,?\s*state(?:\s*,?\s*zip)?)\s*:?\s*$", re.I)


_CONTACT_LABEL_RE = re.compile(
    r"^\s*(?:address|phone|fax|e-?mail|city\s*,?\s*state(?:\s*,?\s*zip)?|contractor|bidder|"
    r"vendor)\b", re.I)


def _block_label_strip(cell):
    s = txt(cell)
    t = _BLOCK_LABEL_TAIL_RE.sub("", _BLOCK_LABEL_HEAD_RE.sub("", s)).strip()
    if not re.search(r"[A-Za-z]{2}", t) or _CONTACT_LABEL_RE.fullmatch(t):
        return s
    return t


# ---- a name printed across its pair's two columns --------------------------
# A firm's name is centred over its 'Unit Price | Amount' pair; where the
# table rules the names row through the middle of the pair, the name is cut
# at the rule mid-word: Cedar Hill PW23-0009 page 2 reads 'EAR
# Telecommunicat | ions, LLC DBA EARTC', 'Texas Standard C | onstruction,
# Ltd.', 'Durable Spe | cialties, Inc.' -- three firms page 1 names in full.
# The right half is joined back only when it CONTINUES a word (it opens in
# lower case after a letter); a second cell that starts a word of its own is
# something else and is left alone.  And the walk's look one column to the
# left never takes a CODE for a name ('CDH24307', the project number cut at
# the same rule, sat left of the first pair): one token, letters and three or
# more digits, alone in the bidder columns' part of its row (a row of codes,
# one per bidder -- Collin County 2025-259's 'Quote No.' row -- is left to
# the caption-row logic).
_CODE_TOKEN_RE = re.compile(r"^(?=\S*[A-Za-z])(?=(?:\S*\d){3})\S+$")


def _name_split_at_rule(left, right):
    a, b = txt(left), txt(right)
    if a and b and a[-1].isalpha() and b[0].islower():
        return a + b
    return left


def _name_rest_at_rule(right):
    """The right half of a name cut between words, in a row cut mid-word
    elsewhere: words, no money or number."""
    b = txt(right)
    return (bool(b) and b[0].isalpha() and num(b) is None and not ADDRESS_RE.search(b)
            and not _ADDR_ANY_RE.search(b))


# ---- an ESTIMATE caption over the engineer's firm name ---------------------
# (Where the estimate's own name cell is blank and the walk took the name of
# the column to its left -- Dallas County 2026-006-7091 prints 'ENGINEER's
# ESTIMATE' over a pair whose name cell is empty, beside non-responsive
# 'Vendor No. 9 / Dredge Heavy Construction' -- the column is the caption
# alone: it was read as a bidder named for the vendor.)
# A caption row can label each column above the firm row: NRH RFB 24-021
# (Amundson Drive) prints 'ENGINEER'S ESTIMATE | BIDDER 1 | BIDDER 2 | ...'
# over 'WESTRA CONSULTANTS, LLC | TEX-PRO CONSTRUCTION, LLC | ...' (and 'BID
# BOND NA' under the estimate only).  The walk stops at the firm, so the
# column went out as a bidder named for the consultant -- the second-lowest
# 'bid'.  Where the nearest text above a column's name is an estimate caption
# and the same row labels another pair's column with something else ('BIDDER
# 1'), the caption is kept in front of the name, as the corpus already prints
# it in one cell ("ENGINEER'S ESTIMATE Kimley-Horn and Associates, Inc"), so
# ENGINEER_RE flags the column.
def _estimate_caption_above(grid, r, pc, pairs, lookback=8):
    if r is None:
        return ""
    for rr in range(r - 1, max(-1, r - 1 - lookback), -1):
        row = grid[rr]
        here = [txt(row[c]) for c in (pc, pc - 1) if 0 <= c < len(row) and txt(row[c])]
        if not here:
            continue
        cap = here[0]
        if not (ENGINEER_RE.search(cap) and len(cap) <= 60):
            return ""
        others = [txt(row[p]) for p, _ in pairs if p != pc and p < len(row) and txt(row[p])]
        return cap if any(not ENGINEER_RE.search(o) for o in others) else ""
    return ""


_EST_CAPTION_RE = re.compile(r"estimat|\be?opcc?\b|opinion\s+of\s+probable", re.I)


# ---- the row of names printed UNDER the caption row ------------------------
# bidder_names_for only walks UP.  Some templates print the captions first and
# the firms on the next row: Frisco 2203-047 Meadow Hill Drive heads its five
# bidders 'Unit Price | Extended Amount' and names them one row lower (605
# priced cells), Coppell 2021-5552 Cambria/Wise Way prints 'Unit Price |
# Extended Amount' at the very top of the table with QUALITY EXCAVATION, JESKE,
# TISEO and AXIS on the row beneath (228 cells) -- every one dropped as
# no_bidder_name.  Read only for a pair that found no name above, only from the
# one or two rows directly under the header, and only while those rows carry
# no number in ANY money column (the first priced row is data, and the search
# stops there).  The names must be a ROW of them -- two or more columns named,
# and not all alike ('Yes | Yes' is a caption row) -- unless the tab has a
# single bidder.  The row itself has no prices, so it never yields a record.
# A firm says what it is: a legal suffix or a trade word.  Asked of a name
# that has no second column to vouch for it (_name_candidate, lone=True).
_FIRM_WORD_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|companies|"
    r"lp|l\.p|llp|pllc|dba|construction|constructors?|contracting|contractors?|services?|"
    r"group|enterprises?|industries|paving|utilit(?:y|ies)|concrete|excavation|excavating|"
    r"builders?|solutions|supply|systems|partners|associates|engineering|engineers|"
    r"landscap\w*|equipment|materials|electric\w*|plumbing|infrastructure|development|works|"
    r"sitework|civil|underground|pipeline|trucking|erosion|environmental|structures|"
    r"holdings|technolog\w*|consult\w*|mechanical|irrigation|fenc\w*|roofing|asphalt|"
    r"striping|traffic|demolition|dredg\w*|marine|restoration|grading|earthworks?|sawing|"
    r"coatings?)\b", re.I)


def _name_candidate(v, lone=False):
    """A cell the NEW name paths (names_below_header, names_above_table) may
    read as a firm: bidder-shaped, not a caption, two letters running (a blank
    bid form's '$ _________' price blanks are no names -- NRH 21-011), and no
    amount in it (Grand Prairie's Headwall Creek '25% Contingency $3,318.75';
    Arlington's Lakewood Dr tab, whose interleaved text gave 'e$m b e d m e n
    2 t,7 &0. 0b0ac').  A LONE name -- the only column named -- must also say
    what it is (_FIRM_WORD_RE): read from one column, the row under a header is
    as often another table's caption row -- 'Predecessors' (a Gantt chart in
    Grand Prairie's Briar Hill packet), 'Numeric' (Lewisville's LLELA bid
    form), 'Coded Values' -- or a person ('Claire Yancey')."""
    t = txt(v)
    if not (t and looks_like_bidder(t) and not _caption_only(t)
            and re.search(r"[A-Za-z]{2}", t) and not re.search(r"\$|\d[\d,]*\.\d\d\b", t)):
        return False
    # nor a contact line ('Phone 972-907-1133' over Forney CIP 252's
    # Boardwalk column, the line nearest the table that 'named' two pairs)
    if _PHONE_RE.search(t) or re.match(
            r"^\s*(?:address|phone|fax|e-?mail|city\s*,?\s*state)\b", t, re.I):
        return False
    return not lone or bool(_FIRM_WORD_RE.search(t))


def names_below_header(grid, header_row, pairs, max_rows=2):
    """{price col: name} from a names row directly under the header, or {}."""
    money = [c for p in pairs for c in p]
    for r in range(header_row + 1, min(len(grid), header_row + 1 + max_rows)):
        row = grid[r]
        if any(num(row[c]) is not None for c in money if c < len(row)):
            return {}
        got = {pc: row[pc] for pc, _ in pairs
               if pc < len(row) and row[pc] and _name_candidate(row[pc])}
        if len(got) == 1 and not _name_candidate(next(iter(got.values())), lone=True):
            got = {}
        if len(got) >= min(2, len(pairs)):
            if len(got) >= 2 and len({txt(v).lower() for v in got.values()}) == 1:
                continue                     # a caption repeated over every column
            return {pc: (strip_address(v) or v) for pc, v in got.items()}
    return {}


KNOWN_UNITS = set(UNIT_MAP.values())


def infer_headerless_layout(grid):
    """Some bid tabs carry NO header row at all -- the first row holds the bidder
    names and every row after it is data. Infer the columns from content instead:

        item_no | quantity | unit | description | price | total | price | total ...

    Note the column ORDER varies (quantity before description here, after it
    elsewhere), so each role is identified by what its cells look like, not by
    position. Returns a synthetic header row, or None if the shape is unclear.
    """
    body = [r for r in grid[1:] if any(r)]
    if len(body) < 4:
        return None
    width = max(len(r) for r in body)
    if width < 6:
        return None

    def col(i):
        return [(r[i] if i < len(r) else "") for r in body]

    numeric_frac, unit_frac, textlen = [], [], []
    for i in range(width):
        vals = col(i)
        nonempty = [v for v in vals if v] or [""]
        numeric_frac.append(sum(num(v) is not None for v in nonempty) / len(nonempty))
        unit_frac.append(sum(norm_unit(v) in KNOWN_UNITS and v != "" for v in nonempty)
                         / len(nonempty))
        textlen.append(sum(len(v) for v in nonempty) / len(nonempty))

    # The money block is the run of numeric columns at the right edge.  An
    # EMPTY spacer column between two bidders' blocks -- Dallas County prints
    # one after every 'Total Unit Price | Extension' pair -- used to end the
    # run, so only the right-most bidder was read.  A spacer (no number in any
    # row, at most one filled cell) is now passed over; a pair may not straddle
    # one, and when the run had any, EVERY pair must prove itself below
    # (quantity x price == extension), not only the first.
    def spacer(i):
        vals = [v for v in col(i) if v]
        return len(vals) <= 1 and not any(num(v) is not None for v in vals)

    # (Only BETWEEN money columns: an empty column at the right edge still ends
    # the run, as before -- Arlington WU Oak St's non-responsive fourth bidder
    # leaves two such columns, and passing them made the inferred table one
    # bidder narrower than the header the next table inherits.)
    money = []
    i = width - 1
    while i >= 0 and (numeric_frac[i] >= 0.5 or (money and spacer(i))):
        if numeric_frac[i] >= 0.5:
            money.append(i)
        i -= 1
    money.reverse()
    # An ODD run: the quantity column sits flush against the money block and
    # is just as numeric, so the run ends one column too far left.  Mansfield
    # 21-4436 (US 287 utility relocation, dry riprap, hydromulch: ITEM | SPEC |
    # DESCRIPTION | UNIT | QUANTITY | 7 x (UNIT PRICE | AMOUNT)) has a 15-column
    # run and was refused outright.  The leftmost column of an odd run is taken
    # for the quantity only when it PROVES to be one -- quantity x the first
    # unit price == the first extension, the same test as below -- and the
    # table is refused, as before, when it does not.
    odd_qty = None
    if len(money) >= 3 and len(money) % 2:
        odd_qty, money = money[0], money[1:]
    n_money = len(money)
    if n_money < 2 or n_money % 2:
        return None
    money_pairs = [(money[k], money[k + 1]) for k in range(0, n_money, 2)]
    if any(t != p + 1 for p, t in money_pairs):
        return None                       # a spacer inside a pair: not price | total
    spaced = n_money != money[-1] - money[0] + 1   # a spacer between the pairs
    first_money = money[0]
    if first_money < 3:
        return None

    unit_col = max(range(first_money), key=lambda j: unit_frac[j])
    if unit_frac[unit_col] < 0.5:
        return None
    cand = [j for j in range(first_money) if j != unit_col]
    desc_col = max(cand, key=lambda j: textlen[j])
    qty_cands = [j for j in cand if j != desc_col]

    # The item-number column and the quantity column are BOTH ~100% numeric, so
    # max(numeric_frac) is a tie and Python's max() returns the FIRST maximum --
    # the leftmost, which is the item number.  The quantity then read as an item
    # number and vice versa (mesquite 23-6622 item 39 "Concrete Sidewalk" 6,200
    # SY became item_no='6,200' / quantity=39), producing 2,381 arithmetically
    # impossible rows: 87.9% of every ext_mismatch in the library.
    #
    # Break the tie on evidence instead of column order: the real quantity is
    # the column that satisfies quantity x unit_price == extended against the
    # first bidder's money pair.  39 x 65 = 2,535 fails; 6,200 x 65 = 403,000
    # matches the printed total.
    def qty_consistency(j):
        ok = tot = 0
        for r in body:
            q = num(r[j]) if j < len(r) else None
            p = num(r[first_money]) if first_money < len(r) else None
            e = num(r[first_money + 1]) if first_money + 1 < len(r) else None
            if q is None or p is None or e is None:
                continue
            tot += 1
            if abs(q * p - e) / max(abs(e), 1.0) <= 0.005:
                ok += 1
        return (ok / tot) if tot else 0.0

    qty_col = None
    if qty_cands:
        scored = sorted(((qty_consistency(j), j) for j in qty_cands), reverse=True)
        if scored and scored[0][0] >= 0.5:
            qty_col = scored[0][1]
        elif odd_qty is None:
            # No column reproduces the printed totals; fall back to the old
            # numeric-density rule rather than refuse the file outright.
            qty_col = max(qty_cands, key=lambda j: numeric_frac[j])
    if odd_qty is not None and qty_col != odd_qty:
        return None           # the odd column is no proven quantity: refuse, as before
    if qty_col is None or numeric_frac[qty_col] < 0.5:
        return None
    if spaced:
        # every pair across a spacer must extend on the quantity column
        for p, t in money_pairs:
            ok = tot = 0
            for r in body:
                q = num(r[qty_col]) if qty_col < len(r) else None
                pv = num(r[p]) if p < len(r) else None
                e = num(r[t]) if t < len(r) else None
                if q is None or pv is None or e is None:
                    continue
                tot += 1
                ok += abs(q * pv - e) / max(abs(e), 1.0) <= 0.005
            if not tot or ok / tot < 0.5:
                return None
    item_col = next((j for j in cand if j not in (desc_col, qty_col)), None)

    synth = [""] * width
    if item_col is not None:
        synth[item_col] = "Item"
    synth[desc_col] = "Description"
    synth[unit_col] = "Unit"
    synth[qty_col] = "Quantity"
    for p, t in money_pairs:
        synth[p] = "Unit Price"
        synth[t] = "Total"
    return synth


def _synth_header_at(grid, synth):
    """Index at which rows_from_grid inserts the inferred header row.

    It used to go after grid[0] unconditionally, on the assumption that the
    first row holds the bidder names.  Two shapes break that assumption:
      * a CONTINUATION table -- every row is an item and the names are
        inherited -- had its first item read as the names row and silently
        lost (Frisco 2510-007 Dallas Parkway: P1 Mobilization, all six columns);
      * a title row above the row that names the bidders (Frisco 2109-116
        Upper Reuse: 'Bid Tab - Upper Reuse ...' then 'Item Description | Spec
        Item | ... | R Construction | | Belt Construction | ...'): the names
        row became data and the walk up found only the title.
    So the header goes directly above the first PRICED row (a number in some
    unit-price column): at 0 when that is grid[0]; above row k when every row
    between grid[0] and it is a multi-cell row and none is a section banner;
    otherwise after grid[0] as before -- a banner ('ALTERNATE #1') or a
    one-cell sub-project label must stay on the data side of the header, where
    it opens its section."""
    price_cols = [k for k, h in enumerate(synth) if h == "Unit Price"]

    def priced(row):
        return any(num(row[k]) is not None for k in price_cols if k < len(row))

    first = next((i for i, row in enumerate(grid) if priced(row)), None)
    if first is None or first == 1:
        return 1
    if first == 0:
        return 0
    for row in grid[1:first]:
        filled = [c for c in row if c]
        # An EMPTY spacer row is neither a banner nor a one-cell label.  It
        # used to stop the move: Rowlett 2024-02 (pump-station tank, 16
        # bidders on 6 pages) prints a firm title on grid[0], the names on
        # grid[1] and a blank ruled row above the first item, so on pages 1,
        # 3 and 6 the names row became data, those 7 bidders went unnamed,
        # and page 3 took page 2's three names instead.
        if not filled:
            continue
        if len(filled) < 2 or any(section_banner(c) for c in filled):
            return 1
    # Moved down, the header lets the walk see every row above it, and the walk
    # takes the NEAREST name-like cell: Collin County 2025-020 prints the firms
    # on grid[0] and the purchasing co-op ('Choice Partners', 'BuyBoard') on
    # the row under them, and 2025-03-31's goods sheet has the firm on grid[0]
    # and a 'Package' caption on the row under it -- both read the wrong row.
    # So the header moves down only when grid[0], the old names row, names
    # nobody: then (Upper Reuse) the names are on a lower row.
    pairs = detect_price_pairs(synth)
    if any(bidder_names_for([grid[0], synth], 1, pairs, skip_captions=True).values()):
        return 1
    return first


# ---- captions over the wrong cells: QUANTITY | UNIT above 'LS' | '1' -------
# Mansfield 21-4092 (paving + drainage: RCP, headwalls) heads its columns
# 'QUANTITY | UNIT' and prints 'LS | 1', 'CY | 400', 'LF | 54' beneath them;
# Frisco 2205-061 does the same under 'Quantity | Units'.  Every row then read
# a number as its unit, read_pdf_best's unit gate (rightly) refused the whole
# parse, and both lettings yielded nothing.  The cells prove which is which:
# when at least 80% of the priced rows show a NUMBER under the unit caption and
# a known UNIT under the quantity caption (and at most 20% the other way), the
# two columns are swapped.  A tab whose captions fit its cells never meets it.
def _swap_unit_qty_by_content(grid, header_row, pairs, meta, stats=None):
    u, q = meta.get("unit"), meta.get("qty")
    if u is None or q is None or u == q:
        return meta
    tot = num_u = unit_q = unit_u = num_q = 0
    for row in grid[header_row + 1:]:
        if not any(pc < len(row) and num(row[pc]) is not None for pc, _ in pairs):
            continue                                 # item rows only
        a = row[u] if u < len(row) else ""
        b = row[q] if q < len(row) else ""
        if not a and not b:
            continue
        tot += 1
        num_u += num(a) is not None
        unit_u += bool(a) and norm_unit(a) in KNOWN_UNITS
        unit_q += bool(b) and norm_unit(b) in KNOWN_UNITS
        num_q += num(b) is not None
    if (tot >= 3 and num_u >= 0.8 * tot and unit_q >= 0.8 * tot
            and unit_u <= 0.2 * tot and num_q <= 0.2 * tot):
        if stats is not None:
            stats["unit_qty_columns_swapped"] = stats.get("unit_qty_columns_swapped", 0) + 1
        return dict(meta, unit=q, qty=u)
    return meta


# ---- the DESCRIPTION column under a caption that does not say so -----------
# North Richland Hills heads its item text 'Spec. Item' (RFB 24-033, Tributary
# CB-1: 'Item No. | Spec. Item | Est. Quantity | Unit'), 'Spec.' (Bid 6347) or
# 'NAME OF PAY ITEM WITH UNIT PRICE IN WORDS' (Bid 6443), and no caption is
# 'DESCRIPTION': every row of those lettings went out with an EMPTY
# description, although the words sit in their own column on every row.  A
# header with no description column at all takes it from the cells: among the
# columns left of the money block that are not the item, unit or quantity
# column, the one whose cells are WORDS (two or more letters running, not a
# unit code, not a number) on at least 60% of the priced rows, the wordiest
# (most letters per cell) where several are.  'Spec Item' columns holding
# 'NCTCOG 203.1' next to a 'Name of Pay Item' column (Bid 6123) lose to the
# names.  A header that names its description column is never touched, and
# only a header that names its item, unit and quantity columns is completed:
# without them the columns are not understood -- Collin County's goods quotes
# ('| | Qty | Price | Extended |' under a block of quote numbers) and NRH
# 3321's second street (a caption row of prices only) would have gone out
# with quote numbers for bidders and no quantities.
def _split_merged_item_caption(grid, header_row, hdr, pairs, meta, stats=None):
    """Round 7 (.28): ONE caption cell 'ITEM NO. ITEM DESCRIPTION' (or 'Item No.
    Item Description Quantity Unit') printed over two or more columns binds the
    item AND the description to its first column, and every row's description
    is then its item number ("P01", "A-4": Frisco 2603-054, Trophy Club Bobcat
    Blvd and Forest Hill Dr, Highland Park Hackberry Creek, Kaufman Crestview).
    When that column holds codes in the priced rows (80%) and a column to its
    right with an EMPTY caption holds words (60%), that column is the
    description.  ONLY the description: a quantity or unit the merged caption
    also names ('Item No. Item Description Quantity Unit', Hackberry Creek) is
    left to the rules that find them now -- remapping those changes the
    arithmetic and unit checks read_pdf_best chooses a strategy on, and in
    the corpus it flipped 11 files between the table and coordinate readings
    (measured in handoff/scratchpad/parser_r7: mostly better, not all), which is a
    separate change to verify on its own."""
    c0 = meta.get("item")
    if c0 is None or meta.get("desc") != c0 or not pairs:
        return meta
    cap = txt(hdr[c0]) if c0 < len(hdr) else ""
    caps = cap + " " + (_squeeze_letters(cap) or "")
    if not DESC_HDR_STRONG.search(caps):
        return meta
    rows = [row for row in grid[header_row + 1:]
            if any(pc < len(row) and num(row[pc]) is not None for pc, _ in pairs)]
    if len(rows) < 3:
        return meta
    filled = [txt(row[c0]) for row in rows if c0 < len(row) and txt(row[c0])]
    if not filled or sum(1 for v in filled if _ITEM_CODE_RE.match(v)) < 0.8 * len(filled):
        return meta
    taken = {meta.get("unit"), meta.get("qty")}
    free = [c for c in range(c0 + 1, pairs[0][0])
            if not txt(hdr[c] if c < len(hdr) else "") and c not in taken]

    def share(c, test):
        cells = [txt(row[c]) if c < len(row) else "" for row in rows]
        return sum(1 for v in cells if v and test(v)) / float(len(rows))
    dc = next((c for c in free if share(c, lambda v: not desc_is_code_only(v) and num(v) is None
                                         and norm_unit(v) not in KNOWN_UNITS) >= 0.6), None)
    if dc is None:
        return meta
    out = dict(meta, desc=dc)
    if stats is not None:
        stats["merged_item_caption_split"] = stats.get("merged_item_caption_split", 0) + 1
    return out


def _desc_column_by_content(grid, header_row, pairs, meta):
    """The column index holding the item descriptions, or None."""
    if meta.get("desc") is not None or not pairs:
        return None
    if any(meta.get(k) is None for k in ("item", "unit", "qty")):
        return None
    taken = {meta.get(k) for k in ("item", "unit", "qty")}
    cand = [c for c in range(pairs[0][0]) if c not in taken]
    if not cand:
        return None
    rows = [row for row in grid[header_row + 1:]
            if any(pc < len(row) and num(row[pc]) is not None for pc, _ in pairs)]
    if len(rows) < 3:
        return None
    best, best_letters = None, 0.0
    for c in cand:
        cells = [txt(row[c]) if c < len(row) else "" for row in rows]
        wordy = [s for s in cells if s and re.search(r"[A-Za-z]{2}", s) and num(s) is None
                 and norm_unit(s) not in KNOWN_UNITS]
        if len(wordy) < 0.6 * len(rows):
            continue
        letters = sum(sum(ch.isalpha() for ch in s) for s in wordy) / len(rows)
        if letters > best_letters:
            best, best_letters = c, letters
    return best


# ---- (round 11, .32) the UNIT column under a caption that does not say so ---------------------------
# A unit column the header does not name was never read: Highland Park's Hackberry Creek tab prints ONE
# merged caption 'Item No. Item Description Quantity Unit' over four columns, Keller's and Mansfield's
# 'PAY ITEM QTY UNIT ITEM DESCRIPTION' template stacks it over the first column, Bedford's 'Unit of
# Measure' sits on the row above the price captions, Justin's and Mansfield's 2025 drainage tab leave the
# unit column's caption blank, Frisco's bid-system export garbles it ('eUnitof') -- every row of those
# lettings went out with no unit, so none could be priced or trained on.  The units are printed, one per
# row, in their own column.  Where the header names no unit column, a column left of the money block that
# is not the item, description or quantity column is the unit column when its cells on the priced rows
# are units (UNIT_MAP) -- at least three, at least 80% of the column's non-empty cells and at least half
# of the priced rows; the column with the most such cells where several qualify.  Only cells that ARE a
# unit are read from it (rows_from_grid): a word or a number in it is no unit and the row stays without
# one, so nothing is guessed.
def _unit_column_by_content(grid, header_row, pairs, meta):
    """The column index holding the units of measure, or None (see above)."""
    if meta.get("unit") is not None or not pairs:
        return None
    rows = [row for row in grid[header_row + 1:]
            if any(pc < len(row) and num(row[pc]) is not None for pc, _ in pairs)]
    if len(rows) < 3:
        return None
    taken = {meta.get(k) for k in ("item", "desc", "qty")}
    best, best_n = None, 0
    for c in range(pairs[0][0]):
        if c in taken:
            continue
        cells = [txt(row[c]) if c < len(row) else "" for row in rows]
        filled = [v for v in cells if v]
        units = [v for v in filled if norm_unit(v) in KNOWN_UNITS and not NUMERIC_UNIT_RE.match(v)]
        if len(units) >= 3 and len(units) >= 0.8 * len(filled) and len(units) >= 0.5 * len(rows) \
                and len(units) > best_n:
            best, best_n = c, len(units)
    return best


# ---- (round 11, .32) a QUANTITY cell that carries its unit -------------------------------------------
# Some tabs print the unit in the quantity cell -- '154.83 AC', '1 LS', '124,600 CY' (the Village
# Estates development proposal tab, Denton County) -- and num() reads no number from it: the row lost
# its quantity and its unit.  A cell that is exactly a number and a unit of measure (UNIT_MAP), in that
# order, is both.  Only where the row has no unit cell of its own.
_QTY_UNIT_CELL_RE = re.compile(r"^\s*(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z][A-Za-z.]{0,7})\s*$")


def split_quantity_unit(cell):
    """(quantity, unit as printed) of a quantity cell '154.83 AC', or None."""
    m = _QTY_UNIT_CELL_RE.match(txt(cell))
    if not m:
        return None
    u = m.group(2)
    if norm_unit(u) not in KNOWN_UNITS or norm_unit(u) in ("LOT", "CASE", "BOX", "PAIR", "REAM", "DOZ", "MIL",
                                                          "BTL", "PKG", "HD", "CYC"):
        return None
    q = num(m.group(1))
    return (q, u) if q is not None else None


# ---- two QUANTITY columns: the one the extensions are computed on -----------
# detect_meta_cols takes the first quantity caption, and a tab may print two:
# Frisco Bid 2402-066 (Hillcrest Road) heads 'Unit | Plan Quantity | Bid
# Quantity | Unit' and every bidder's total is the BID quantity x unit price
# (P05 message signs: plan 2, bid 4; P06 temporary asphalt: plan 0, bid 400).
# Read on the plan quantity, 28 rows a bidder failed quantity x price =
# extension and left the panel ($1.86M of KG Traffic's $5,504,378.39 low bid).
# Where the header holds two or more quantity captions left of the money, the
# column whose numbers reproduce MORE of the printed extensions is the
# quantity; a tie keeps the first, as before.
def _qty_column_by_arithmetic(grid, header_row, hdr, pairs, meta, stats=None):
    q0 = meta.get("qty")

    def agree(c, checked=False):
        n = m = 0
        for row in grid[header_row + 1:]:
            q = num(row[c]) if c < len(row) else None
            if q is None:
                continue
            for pc, tc in pairs:
                up = num(row[pc]) if pc < len(row) else None
                ext = num(row[tc]) if tc < len(row) else None
                if up and ext:
                    m += 1
                    if abs(q * up - ext) <= _QTY_TOL * max(abs(ext), 1.0):
                        n += 1
        return (n, m) if checked else n
    if q0 is not None and q0 in (meta.get("item"), meta.get("desc")):
        # ONE header cell naming the item (or description) and the quantity
        # -- 'Line # Description MfgQTY UOM' over Lancaster RFP 2022-12's whole
        # meta block -- gives that column as both, and its numbers (the item
        # numbers) contradict most printed extensions: no quantity column
        # (recover_quantity_from_extensions finds them where columns agree).
        # Where nothing can be checked (no extensions) it is left alone.
        n, m = agree(q0, checked=True)
        if m and 2 * n < m:
            return dict(meta, qty=None)
        return meta
    cands = [i for i, h in enumerate(hdr[:pairs[0][0]]) if h and QTY_HDR.search(h)
             and (i == q0 or i not in (meta.get("item"), meta.get("desc"), meta.get("unit")))]
    if q0 is None or q0 not in cands or len(cands) < 2:
        return meta
    score = {c: agree(c) for c in cands}
    best = max(cands, key=lambda c: (score[c], -cands.index(c)))
    if score[best] > score[q0]:
        if stats is not None:
            stats["qty_column_by_arithmetic"] = stats.get("qty_column_by_arithmetic", 0) + 1
        return dict(meta, qty=best)
    return meta


# ---- a stacked bidder block: a caption row repeated under NEW names ---------
# Keller 25-346 (Bear Creek Parkway bridge erosion protection) prints ten
# bidders as three blocks of four 'UNIT COST | TOTAL' pairs, each block under
# its own row of names and its own copy of the caption row, and pdfplumber
# returned the first two blocks as ONE table.  The names were read once, above
# the first caption row, so Cole, FNH, Mann Robinson and Precision Road's 15
# items each were filed under AMS, Bernal, Black Fox and C. Green Scaping:
# AMS 'bid' $1,043,513 (its $513,213 plus Cole's $530,300) and lost the
# letting it won.  A row that repeats the header's price pairs exactly is a
# caption row; the names above it -- read the same way as the first block's,
# from the rows after the last row that carried an amount -- name every pair
# below it.  Only a names row that names a pair differently starts a new
# block, and it names every pair (a pair it leaves unnamed has no bidder in
# that block: Keller's third block prints '-' over its two empty pairs).  A
# caption row repeated under the same names, or under no names, changes
# nothing -- and neither does one under the SAME firms again: a new block
# names new bidders, each once.  Cedar Hill's US 67 Green Ribbon tab repeats
# its caption row on page 5 under 'A&B | C. GREEN | CENTRAL NORTH | JB & CO
# | JB & CO' -- a stale copy of the names in another order, Pacheco Koch's
# estimate left out -- while every column total it prints follows the names
# above the first caption row.
def _block_names(grid, start, r, pairs, current=None):
    """{price col: name} for a caption row grid[r] repeated inside a table,
    read from grid[start:r] only; {} when those rows name no new firm."""
    sub = grid[start:r + 1]
    fresh = bidder_names_for(sub, len(sub) - 1, pairs)
    if not any(fresh.get(pc) and _name_candidate(fresh[pc]) and not PLACEHOLDER_RE.match(fresh[pc])
               for pc, _ in pairs):
        return {}

    def key(v):
        return re.sub(r"[^a-z0-9]", "", txt(v).lower())
    got = [key(fresh[pc]) for pc, _ in pairs if fresh.get(pc) and key(fresh[pc])]
    old = {key(v) for v in (current or {}).values() if v and key(v)}
    if len(set(got)) != len(got) or old & set(got):
        return {}
    # ... and they are firms: a caption over the price columns ('Numeric' in
    # Lewisville's e-bid export of its kayak-launch letting) names nobody.
    if not all(_FIRM_WORD_RE.search(fresh[pc]) for pc, _ in pairs if fresh.get(pc)):
        return {}
    return {pc: (fresh.get(pc) or "") for pc, _ in pairs}


# ---- an item's PRICES ruled into a row of their own, above it --------------
# A tall item row whose numbers are set higher than its words can come out of
# pdfplumber as two rows: the prices alone, then item number, description,
# quantity and unit with empty price cells.  Mesquite 21-5712 (Bid 2022-009)
# prints item 11 'REMOVE AND REPLACE CONCRETE STEPS ...  10 CY' that way on
# every bidder page after the first; its prices row carried only hidden
# text ('MOBILIZATION', clipped by the page) and item 11 was read as a
# 'MOBILIZATION' row with no number, quantity or unit -- or, once hidden text
# is not read, lost.  The two rows are one item when the first holds money in
# the bidders' columns and nothing else, the second holds the item and no
# money there, and the second's quantity times each price is the extension
# beside it, in every column (two or more).
def merge_price_row_above_item(grid, header_row, pairs, meta, stats=None):
    money = {c for p in pairs for c in p}
    keys = [meta.get(k) for k in ("item", "desc", "unit", "qty")]
    if meta.get("qty") is None or meta.get("unit") is None:
        return grid
    out = [list(r) for r in grid]
    for r in range(header_row + 1, len(out) - 1):
        a, b = out[r], out[r + 1]
        if any(txt(a[c]) for c in range(len(a)) if c not in money):
            continue
        if any(i is not None and i < len(b) and txt(b[i]) == "" for i in keys[2:]):
            continue
        if not any(i is not None and i < len(b) and txt(b[i]) for i in keys[:2]):
            continue
        if any(c < len(b) and txt(b[c]) for c in money):
            continue
        if meta["qty"] >= len(b) or meta["unit"] >= len(b):
            continue
        q = num(b[meta["qty"]])
        if not q or norm_unit(txt(b[meta["unit"]])) not in KNOWN_UNITS:
            continue
        ok = 0
        for pc, tc in pairs:
            up = num(a[pc]) if pc < len(a) else None
            ext = num(a[tc]) if tc < len(a) else None
            if up is None and ext is None:
                continue
            if up is None or ext is None or abs(q * up - ext) > _QTY_TOL * max(abs(ext), 1.0):
                ok = -1
                break
            ok += 1
        if ok < min(2, len(pairs)):
            continue
        for c in money:
            if c < len(a) and c < len(b):
                b[c] = a[c]
        out[r] = [""] * len(a)
        if stats is not None:
            stats["price_rows_merged_down"] = stats.get("price_rows_merged_down", 0) + 1
    return out


# ---- a unit price and its extension in ONE cell ------------------------------
# A rule missing between a bidder's UNIT PRICE and TOTAL columns hands
# pdfplumber both amounts in the unit-price cell and an empty total cell:
# Arlington PWDR22005 prints Alternate A (199A Green Cement, 1 LS) for its
# second bidder as '$ 1.10 | $ 1.10', read '$ 1.10 $ 1.10' | '' -- num() makes
# nothing of the pair and that bidder's alternate line was lost.  A cell of
# exactly two '$' amounts beside an EMPTY total cell is split into the two,
# only when the quantity proves it (quantity -- 1 when none is printed --
# times the first is the second, to the cent), and only when another bidder's
# pair on the same row reads whole -- a unit price and a total in their own
# cells -- so the row's columns are right and the glue is one missing rule.
# Without that proof it stays: Collin County's supplier tabs glue EVERY
# bidder's pair on some rows, and there the glued row is as often the
# document's 'Bid Grand Total' line (2021-173) as an item.
_TWO_MONEY_RE = re.compile(r"^\$\s*(\d[\d, ]*\.\d{2})\s*\$\s*(\d[\d, ]*\.\d{2})$")


def split_glued_price_total(row, pairs, qty):
    """The row with each 'unit $ total' cell beside an empty total cell split
    into its two columns where the quantity proves it; the row itself if none."""
    if not any(pc < len(row) and tc < len(row) and num(row[pc]) is not None
               and num(row[tc]) is not None for pc, tc in pairs):
        return row
    out = None
    for pc, tc in pairs:
        if pc >= len(row) or tc >= len(row) or txt(row[tc]):
            continue
        m = _TWO_MONEY_RE.match(txt(row[pc]))
        if not m:
            continue
        a, b = num(m.group(1)), num(m.group(2))
        if a is None or b is None or abs((1.0 if qty is None else qty) * a - b) > 0.011:
            continue
        out = out or list(row)
        out[pc], out[tc] = m.group(1), m.group(2)
    return out or row


def lump_sum_amount_prices(item_no, desc, unit_raw, qty, row, pairs, prices):
    """The prices of a lump-sum row printed as its amounts only (see
    rows_from_grid), or None when the row is not one.

    An ALLOWANCE is one lump amount too: Mesquite 25-7575 (Bid 2025-013)
    prints item 12 'Irrigation system repair allowance  -  AL' with an empty
    unit-price cell and $2,500.00 in every bidder's extension, so the row read
    as a banner and every bidder's total came out $2,500 short of the printed
    TOTAL BASE.  (Its quantity '-' reads as none; recover_quantity_from_
    extensions then gives it 1, which every column's amount / price agrees on.)"""
    if not ((item_no or desc) and norm_unit(unit_raw) in ("LS", "ALLOW")
            and (qty is None or qty == 1)):
        return None
    if all(v is None for _, v in prices):
        if all(tc < len(row) and (num(row[tc]) or 0) > 0 for _, tc in pairs):
            return [(pc, num(row[tc])) for (pc, tc) in pairs]
        return None
    # ... and a lump sum that SOME bidders print as the amount only (parser
    # round 3): Farmers Branch Creek Erosion Repair Reach 3 (Bid 2021-12) prints
    # the owner's $80,000 'UNFORSEEN ITEMS' allowance with Stoic's unit price
    # and amount, and only the amount for the five others, so their row was
    # read without a price and lost: JB&Co parsed $671,799 against a printed
    # grand total of $751,799 and 'beat' the printed low bid ($750,000).  For
    # one lump sum a column with no unit price and a positive amount bid that
    # amount; a column with neither, or with no amount, stays unpriced.
    # Not a deduction: 'Minus Base Bid Item# 13' prints its amounts in
    # parentheses, which num() does not always see behind a '$' (Richardson
    # 05-22), and a credit is no amount bid.
    if re.match(r"^\W*(?:minus|less|deduct|credit)\b", desc or "", re.I):
        return None
    out, filled = [], False
    for (pc, tc), (_pc, v) in zip(pairs, prices):
        a = num(row[tc]) if tc < len(row) else None
        if tc < len(row) and "(" in txt(row[tc]):
            a = None
        if v is None and a is not None and a > 0:
            out.append((pc, a))
            filled = True
        else:
            out.append((pc, v))
    return out if filled else None


# ---- the banner text of a row, without the column captions beside it -------
# A banner is read from the cells LEFT of the money block, and some templates
# print a column caption in those cells on the same row: Cedar Hill's
# Parkerville EST Overflow tab heads each block 'No. | Base Bid' and 'No. |
# Alternate Bid' (the item column's caption beside the banner), so the row
# read 'No. Alternate Bid', was no banner, and the alternate's items stayed in
# the section before it ('BASE BID TOTAL', role base: +$6,216 on a $229,260
# bid).  Grand Prairie's Fish Creek tab repeats 'BASE BID (Section 2-
# Wastewater) | QUANTITY | UNIT' as a caption row.  Cells that are column
# captions only (_caption_only) are left out of the banner text; a row of
# nothing but captions is judged whole, as before.
def _banner_left(row, first):
    cells = [x for x in row[:first] if x]
    kept = [x for x in cells if not _caption_only(x)]
    return " ".join(kept or cells)


# ---- a banner printed ABOVE the table's own header -------------------------
# The rows above the header were read for bidder names only, so a section title
# the table prints over its header was never seen and the table opened in
# 'BASE BID': Cedar Hill PW23-0004 heads its alternate tables 'FIRST ADD
# ALTERNATE' / 'SECOND ADD ALTERNATE' (20 items read as base, +$390,947 on the
# low bidder), Arlington PWDR22005 its last table 'ALTERNATE A'.  The nearest
# banner above the header (section_banner, judged on the cells left of the
# money block) opens the table's section.  Only for a header the table found
# itself: rows lent from another table (+inherited_hdr) are that table's.
def banner_above_header(grid, header_row, pairs):
    first = pairs[0][0] if pairs else 0
    for r in range(header_row - 1, -1, -1):
        lab = section_banner(_banner_left(grid[r], first))
        if lab:
            return lab
    return ""


# ---- (round 10, .31) a price caption stacked over TWO header rows ------------------------------
# Mansfield's 2023 Brinsap bridge repair tab (riprap, gabions) captions each bidder's price 'UNIT'
# over 'COST' beside 'AMOUNT', and the item column 'ITEM' over 'NO.'.  No single row is a header
# ('COST | AMOUNT' is no price pair), so the table was read by inference, whose synthetic header
# went in under the title, and the firms named right over the captions -- 'GROD CONSTRUCTION',
# 'KLUTZ CONSTRUTION' -- were never reached: 24 priced rows, both bids, dropped unnamed.  Two
# adjacent rows read cell by cell as one caption ('UNIT COST', 'ITEM NO.') make the header when
# the joined row is one (a price pair and a description caption, or two pairs), the lower row is
# none on its own, and the upper row holds no number (it is a caption row, not data or names over
# money).
def stacked_caption_header(grid, limit=40):
    """(row index, joined caption row, pairs) or None."""
    for r in range(1, min(len(grid), limit)):
        up, low = grid[r - 1], grid[r]
        if not any(up) or not any(low) or detect_price_pairs(low):
            continue
        if any(re.search(r"\d", txt(c)) for c in up if c):
            continue
        width = max(len(up), len(low))
        joined = []
        for k in range(width):
            a = txt(up[k]) if k < len(up) else ""
            b = txt(low[k]) if k < len(low) else ""
            joined.append((a + " " + b) if (a and b) else (a or b))
        p = detect_price_pairs(joined)
        if not p:
            continue
        # every price caption of the pairs must be STACKED (a word above and a word below): a
        # caption whole in one row would have made that row a header of its own
        if not all(txt(up[pc]) if pc < len(up) else "" for pc, _ in p):
            continue
        if any(DESC_HDR.search(c) for c in joined if c) or len(p) >= 2:
            return r, joined, p
    return None


# ---- (round 10, .31) a price column proved by its arithmetic, in per-bidder blocks -------------
# Josephine's roadside-ditch tab gives EVERY bidder its own 'Unit | Qty | Estimated Quanity | Total'
# block -- the price column mislabelled 'Estimated Quanity' -- two bidders a page, one on the last.
# No pair caption exists, inference took only the right-most block (the quantity column next to it
# is numeric too), named nobody, and all 15 rows read were dropped.  Where a header row puts a
# TOTAL caption at the end of each block, the block also captions its quantity, and the column
# just left of the total is no unit / quantity column, that column is the unit price when the
# rows prove it: quantity x it == total on 80% or more of 3+ checkable rows, in every block.
# Several blocks must print the same quantities and units (90%), so one quantity serves the row.
# The header is rewritten with 'Unit Price | Total' captions (later blocks' unit and quantity
# captions blanked) and each block's firm, printed over the block's first column, is moved over
# its price column, where bidder_names_for looks.
def proved_price_blocks(grid, limit=40):
    """A rewritten grid (see above) or None."""
    def tol_ok(q, p, t):
        return abs(q * p - t) / max(abs(t), 1.0) <= 0.005

    for h in range(min(len(grid), limit)):
        row = [txt(c) for c in grid[h]]
        tots = [k for k, c in enumerate(row) if c and TOTAL_HDR.match(c)]
        if not tots:
            continue
        blocks, start = [], None
        for t in tots:
            lo = (blocks[-1]["t"] + 1) if blocks else 0
            qc = [k for k in range(lo, t - 1) if row[k] and QTY_HDR.search(row[k])
                  and not PRICE_HDR.match(row[k])]
            uc = [k for k in range(lo, t - 1) if row[k] and UOM_HDR.match(row[k])]
            pc = t - 1
            if (not qc or not row[pc] or pc in qc or pc in uc or PRICE_HDR.match(row[pc])
                    or TOTAL_HDR.match(row[pc])):
                blocks = None
                break
            first_col = min([qc[-1]] + ([uc[-1]] if uc else []))
            blocks.append({"q": qc[-1], "u": uc[-1] if uc else None, "p": pc, "t": t,
                           "s": first_col})
        if not blocks:
            continue
        body = [grid[r] for r in range(h + 1, len(grid))]

        def val(r, k):
            return num(r[k]) if k < len(r) else None
        ok_all = True
        for b in blocks:
            n = good = 0
            for r in body:
                q, p, t = val(r, b["q"]), val(r, b["p"]), val(r, b["t"])
                if q is None or p is None or t is None:
                    continue
                n += 1
                good += tol_ok(q, p, t)
            if n < 3 or good < 0.8 * n:
                ok_all = False
                break
        if not ok_all:
            continue
        b0 = blocks[0]
        for b in blocks[1:]:
            n = same = 0
            for r in body:
                q0, qk = val(r, b0["q"]), val(r, b["q"])
                if q0 is None and qk is None:
                    continue
                n += 1
                u0 = txt(r[b0["u"]]) if b0["u"] is not None and b0["u"] < len(r) else ""
                uk = txt(r[b["u"]]) if b["u"] is not None and b["u"] < len(r) else ""
                same += (q0 == qk and norm_unit(u0) == norm_unit(uk))
            if not n or same < 0.9 * n:
                ok_all = False
                break
        if not ok_all:
            continue
        hdr = list(row)
        above = [txt(c) for c in grid[h - 1]] if h > 0 else []
        for k in range(b0["s"]):
            if not hdr[k] and k < len(above) and (DESC_HDR.search(above[k]) or ITEM_HDR.search(above[k])):
                hdr[k] = above[k]
        for i, b in enumerate(blocks):
            if i == 0:
                hdr[b["q"]] = "Quantity"
                if b["u"] is not None:
                    hdr[b["u"]] = "Unit"
            else:
                hdr[b["q"]] = ""
                if b["u"] is not None:
                    hdr[b["u"]] = ""
            hdr[b["p"]], hdr[b["t"]] = "Unit Price", "Total"
        out = [list(r) for r in grid]
        out[h] = hdr
        # each block's firm over its price column: the nearest row above (3 at most) with text in
        # the block's span and none at the price column itself
        for b in blocks:
            for r in range(h - 1, max(-1, h - 4), -1):
                cells = [(k, txt(out[r][k])) for k in range(b["s"], b["t"] + 1)
                         if k < len(out[r]) and txt(out[r][k])]
                if not cells:
                    continue
                if len(cells) == 1 and cells[0][0] != b["p"] and looks_like_bidder(cells[0][1]):
                    k = cells[0][0]
                    while len(out[r]) <= b["p"]:
                        out[r].append("")
                    out[r][b["p"]], out[r][k] = out[r][k], ""
                break
        return out
    return None


def rows_from_grid(grid, src_file, src_sheet, stats, allow_headerless=True,
                   fallback_names=None, section0="BASE BID"):
    recs = []
    header_row, pairs = None, []
    for r, row in enumerate(grid[:40]):
        p = detect_price_pairs(row)
        if p and any(DESC_HDR.search(c) for c in row if c):
            header_row, pairs = r, p
            break
    if header_row is None:
        for r, row in enumerate(grid[:40]):
            p = detect_price_pairs(row)
            if len(p) >= 2:
                header_row, pairs = r, p
                break
    if header_row is None:
        # (round 10) a price caption stacked over two header rows ('UNIT' over 'COST')
        st = stacked_caption_header(grid)
        if st is not None:
            at, joined, p = st
            grid = list(grid[:at]) + [joined] + list(grid[at + 1:])
            header_row, pairs = at, p
            stats["stacked_caption_headers"] = stats.get("stacked_caption_headers", 0) + 1
    if header_row is None:
        # (round 10) a price column under a caption that is no price caption, proved by
        # quantity x price == total, in one or several per-bidder Unit / Qty / Price / Total blocks
        pv = proved_price_blocks(grid)
        if pv is not None:
            stats["price_columns_by_arithmetic"] = stats.get("price_columns_by_arithmetic", 0) + 1
            return rows_from_grid(pv, src_file, src_sheet, stats,
                                  allow_headerless=False, fallback_names=fallback_names,
                                  section0=section0)
    if header_row is None and allow_headerless:
        synth = infer_headerless_layout(grid)
        if synth:
            stats["headerless_tables_inferred"] = \
                stats.get("headerless_tables_inferred", 0) + 1
            at = _synth_header_at(grid, synth)
            r2, a2 = rows_from_grid(list(grid[:at]) + [synth] + list(grid[at:]),
                                    src_file, src_sheet + "+inferred_cols", stats,
                                    allow_headerless=False,
                                    fallback_names=fallback_names, section0=section0)
            if a2 is not None:
                a2["inferred"] = True     # its header_row indexes a grid with the synth row
                # what a short continuation may borrow (read_pdf): the row
                # above the inferred header and the header itself
                a2["inferred_prefix"] = [list(grid[at - 1]) if at > 0 else [""] * len(synth),
                                         list(synth)]
            return r2, a2
    if header_row is None:
        # setdefault, not stats[...]: these helpers are called directly by the
        # UI layer with a fresh dict, and a bare subscript made them raise
        # KeyError('sheets_no_header') mid-parse -- an API that demands the
        # caller pre-seed counter keys it never documented.
        stats.setdefault("sheets_no_header", []).append(src_file + "::" + src_sheet)
        return recs, None

    hdr = list(grid[header_row])
    meta = detect_meta_cols(hdr, pairs[0][0])

    # Some owners split the header across TWO rows: the upper row carries
    # Item / Description / Units / Bid Quantity (and the bidder names), the lower
    # row only the repeated "Unit Price | Total" pairs. Detection locks onto the
    # lower row because that is where the price pairs are, so the meta columns
    # come back empty and every item would parse as blank. Merge the upper row in.
    if header_row > 0 and meta["desc"] is None and meta["qty"] is None:
        up = grid[header_row - 1]
        merged = list(hdr)
        for i in range(min(len(up), pairs[0][0])):
            if not merged[i] and up[i]:
                merged[i] = up[i]
        m2 = detect_meta_cols(merged, pairs[0][0])
        if m2["desc"] is not None or m2["qty"] is not None:
            hdr, meta = merged, m2
            stats["two_row_headers_merged"] = stats.get("two_row_headers_merged", 0) + 1

    meta = _split_merged_item_caption(grid, header_row, hdr, pairs, meta, stats)
    meta = _swap_unit_qty_by_content(grid, header_row, pairs, meta, stats)
    meta = _qty_column_by_arithmetic(grid, header_row, hdr, pairs, meta, stats)
    dc = _desc_column_by_content(grid, header_row, pairs, meta)
    if dc is not None:
        meta = dict(meta, desc=dc)
        stats["desc_column_by_content"] = stats.get("desc_column_by_content", 0) + 1
    grid = merge_price_row_above_item(grid, header_row, pairs, meta, stats)
    # (round 11) a unit column the header does not name, read by its cells (_unit_column_by_content);
    # after the passes above, so none of them sees a column it did not see before
    unit_by_content = False
    uc = _unit_column_by_content(grid, header_row, pairs, meta)
    if uc is not None:
        meta = dict(meta, unit=uc)
        unit_by_content = True
        stats["unit_column_by_content"] = stats.get("unit_column_by_content", 0) + 1

    names = bidder_names_for(grid, header_row, pairs,
                             skip_captions=src_sheet.endswith("+inferred_cols"), rank_key=src_file)
    if any(not names.get(pc) for pc, _ in pairs):
        below = names_below_header(grid, header_row, pairs)
        for pc, nm in below.items():
            if not names.get(pc):
                names[pc] = nm
                stats["bidder_names_below_header"] = \
                    stats.get("bidder_names_below_header", 0) + 1

    # A multi-page bid tab repeats the "Unit Price | Total" header on every page
    # but prints the contractor names only on page 1, so later pages parse with
    # no bidder and every price on them is lost. Carry the names forward -- but
    # ONLY when the column layout is identical, because inheriting a shifted
    # layout would silently file prices under the wrong contractor.
    if fallback_names and len(fallback_names) == len(pairs):
        filled = 0
        for idx, (pc, _) in enumerate(pairs):
            if not names.get(pc) and fallback_names[idx]:
                names[pc] = fallback_names[idx]
                filled += 1
        if filled:
            stats["bidder_names_inherited"] = stats.get("bidder_names_inherited", 0) + filled

    # An alternate caption in a name cell makes its whole column that
    # alternate (split_alternate_caption).  Split for the RECORDS only and keep
    # `names` raw: the "bidders" returned below are what the next page's table
    # inherits, and an inherited caption must split there as well.
    col_name, col_alt = {}, {}
    for pc, _ in pairs:
        col_name[pc], col_alt[pc] = split_alternate_caption(names.get(pc, ""))
        if col_alt[pc]:
            stats["alternate_columns_by_name"] = \
                stats.get("alternate_columns_by_name", 0) + 1

    section = section0           # 'BASE BID' unless the caller read a page title
    if section0 == "BASE BID" and "+inherited_hdr" not in src_sheet:
        section = banner_above_header(grid, header_row, pairs) or section0
        if section != section0:
            stats["banner_above_header"] = stats.get("banner_above_header", 0) + 1
    money_cols = [c for p in pairs for c in p]
    last_money = header_row      # the last row that carried an amount (a block's end)
    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        if not any(row):
            continue
        # A caption row repeated under a new names row opens a new bidder
        # block (_block_names); the caption row itself is no item.
        if detect_price_pairs(row) == pairs:
            fresh = _block_names(grid, last_money + 1, r, pairs, names)
            if fresh and any(fresh[pc] != names.get(pc, "") for pc, _ in pairs):
                names = dict(fresh)
                for pc, _ in pairs:
                    col_name[pc], col_alt[pc] = split_alternate_caption(names[pc])
                stats["bidder_blocks_renamed"] = stats.get("bidder_blocks_renamed", 0) + 1
                continue
        if any(c < len(row) and num(row[c]) is not None for c in money_cols):
            last_money = r

        def cell(key):
            i = meta[key]
            return row[i] if (i is not None and i < len(row)) else ""

        item_no = txt(cell("item"))
        desc = txt(cell("desc"))
        unit_raw = txt(cell("unit"))
        if unit_by_content and (norm_unit(unit_raw) not in KNOWN_UNITS or NUMERIC_UNIT_RE.match(unit_raw)):
            unit_raw = ""                 # (round 11) only a unit is read from a column found by its cells
        qty = num(cell("qty"))
        if qty is None and not unit_raw and meta["qty"] is not None:
            qu = split_quantity_unit(cell("qty"))      # (round 11) '154.83 AC' in the quantity cell
            if qu is not None:
                qty, unit_raw = qu
                stats["quantity_cell_with_unit"] = stats.get("quantity_cell_with_unit", 0) + 1
        row = split_glued_price_total(row, pairs, qty)
        # An item cell that holds the number AND the words, beside an empty
        # description cell, is both: '1 Mobilization' (Bedford SWIFT 21-09,
        # once spread_stacked_cells has given each row its own line).
        if item_no and not desc and meta["desc"] is not None:
            mt = _ITEM_TEXT_RE.match(item_no)
            if mt:
                item_no, desc = mt.group(1), mt.group(2)

        prices = [(pc, num(row[pc]) if pc < len(row) else None) for pc, _ in pairs]
        # A LUMP SUM printed as its amount only.  Addison's Surveyor Chloramine
        # tab prints items 2, 3, 5, 6 and 7 (1 LS each: $370,000-$661,000
        # buildings and equipment, mobilization, bonds, the $90,000 allowance)
        # with the bid amount and an empty unit-price cell for every bidder, so
        # each row read as price-less (a banner), the letting kept items 1 and
        # 4 ($16,950 of a $1,053,400 low bid) and the wrong firm 'won'.  For one
        # lump sum the unit price IS the amount, so where EVERY priced column
        # of a row leaves the unit price blank and prints a positive amount,
        # and the row is one lump sum (unit LS, quantity 1 or none) with an
        # item number or words, the amount is taken as the price.
        lp = lump_sum_amount_prices(item_no, desc, unit_raw, qty, row, pairs, prices)
        if lp is not None:
            prices = lp
            stats["lump_sum_amount_only"] = stats.get("lump_sum_amount_only", 0) + len(pairs)
        if all(v is None for _, v in prices):
            # A price-less row is either a section banner (BASE BID, ALT #1, Site 3A)
            # or a sub-project banner (a bare street/park name in the first column).
            filled = [x for x in row if x]
            joined = " ".join(filled)
            # The banner is what sits LEFT of the money block.  Farmers Branch
            # prints 'Bid Alternate' in the first cell of a row that repeats
            # every bidder's NAME over the price columns.  Judged whole, that
            # row is the label plus the names: with six bidders it runs past
            # the 120-character guard and is never examined, and with three it
            # becomes a section called 'BID ALTERNATE AXIS CONTRACTING, INC.
            # REBCON, ...'.  (SECTION_RE also lacked the 'Bid Alternate' word
            # order.)  Bid 25-17 folded its 19 alternate items -- $670,793 for
            # the low bidder -- into the base bid, and Bid 26-20 its
            # maintenance-bond alternate.
            # Either way it must be banner-SHAPED (section_banner): a note
            # that opens with a section word is not a banner, and neither is
            # it a sub-project name -- 'Alternates not awarded' would make
            # every later base item an alternate by its word alone.
            left = _banner_left(row, pairs[0][0])
            lab = section_banner(left) if left else ""
            if not lab and left and qty is None and not unit_raw:
                # (round 11) a NUMBERED group heading that is a banner ('11 ALTERNATES', Alvarado's '27
                # ADDITIVE ALTERNATE', Celina's '2 Alternate'; parse_coords reads the same heading of Frisco's
                # bid-system export): the words after the number must be a banner by parse_coords' stricter
                # test (_NUMBERED_BANNER_RE) -- never an item line with a quantity or unit, nor a product
                # line that only opens with 'ALT 1' ('17 ALT 1 Navi-Guard Premium Fleet 15W40 660 quart')
                mg = re.match(r"^\s*\d{1,2}\s+(\S.*)$", left)
                lab = section_banner(mg.group(1)) if mg and _NUMBERED_BANNER_RE.match(mg.group(1)) else ""
            if lab:
                section = lab
            elif joined and len(joined) < 120:
                lab = section_banner(joined)
                if lab:
                    section = lab
                elif (len(filled) == 1 and len(joined) < 45 and not any(ch.isdigit() for ch in joined)
                        and not NONBIDDER_RE.match(joined) and row and row[0] == filled[0]
                        and not SECTION_RE.match(_fix_alternate_typo(joined))):
                    section = txt(joined).upper()[:60]
            continue
        # An alternate banner glued onto this item's description (GLUED_ALT_RE)
        # opens its section.  The description is left as printed: what follows
        # the caption can itself read as an aggregate ('Alternate No. 1 -
        # Total Cost for additional piping ...', Collin 2025-02-10) and would
        # then be dropped by main()'s description filter.
        gm = GLUED_ALT_RE.match(_fix_alternate_typo(desc))
        if gm:
            section = txt(gm.group("banner")).upper()[:60]
            stats["glued_alternate_banners"] = stats.get("glued_alternate_banners", 0) + 1
        # A BASE BID banner glued onto item 1 (GLUED_BASE_RE): the words are
        # the item, the banner opens the base section.
        gb = None if gm else GLUED_BASE_RE.match(desc)
        if gb and (unit_raw or qty is not None):
            desc = txt(gb.group("rest"))
            section = "BASE BID"
            stats["glued_base_banners"] = stats.get("glued_base_banners", 0) + 1
        # Carrollton marks alternates with an in-list MARKER row: the description
        # is literally "Alternative 1", quantity 1 EA, every price 0. It carries
        # no price of its own but everything after it belongs to that alternate.
        mk = re.match(r"^\s*alternat(?:e|ive)\s*(?:bid)?\s*#?\s*([0-9ivx]+)\s*$", desc, re.I)
        if mk and len(desc) < 30 and all((v is None or v == 0) for _, v in prices):
            section = "ALT " + mk.group(1).upper()
            continue

        if not desc and not item_no:
            continue
        if AGG_ITEM_RE.match(item_no):
            stats["agg_rows_skipped"] = stats.get("agg_rows_skipped", 0) + len(pairs)
            continue
        if is_total_label_row(item_no, desc, unit_raw, qty):
            stats["agg_label_rows_skipped"] = \
                stats.get("agg_label_rows_skipped", 0) + len(pairs)
            continue

        row_section = section
        alt = ALT_ITEM_RE.match(item_no)
        if alt:
            row_section = "ALT " + alt.group(1).upper()
        elif ROW_ALT_RE.match(desc):
            row_section = "ALT " + (item_no.upper() or "1")

        dn = norm_desc(desc)
        fam = item_family(dn)
        mat = MATERIAL_RE.search(dn)
        for (pc, tc), (_, up) in zip(pairs, prices):
            if up is None:
                stats["cells_blank_price"] = stats.get("cells_blank_price", 0) + 1
                continue
            ext = num(row[tc]) if tc < len(row) else None
            bidder_raw = col_name[pc]
            recs.append({
                "src_file": src_file,
                "src_sheet": src_sheet,
                "bid_section": col_alt[pc] or row_section,
                "item_no": item_no,
                "description": desc,
                "desc_norm": dn,
                "item_family": fam,
                "size_in": extract_size_in(dn),
                "material": (mat.group(1).upper() if mat else ""),
                "unit_raw": unit_raw,
                "unit": norm_unit(unit_raw),
                "quantity": qty,
                "bidder_raw": bidder_raw,
                "bidder": canon_bidder(bidder_raw),
                "is_engineer_estimate": ENGINEER_RE.search(bidder_raw) is not None,
                "unit_price": up,
                "extended_reported": ext,
            })
    return recs, {"header_row": header_row + 1, "n_price_cols": len(pairs),
                  "bidders": [names.get(pc, "") for pc, _ in pairs],
                  "pairs": list(pairs)}


# ---- an Office file published under a .pdf name ----------------------------
# Legistar serves some attachments with a .pdf name and Office content: of the
# 2026-11-19 corpus, 13 files under legistar/ are ZIP packages -- 3 Excel
# workbooks (Mansfield 23-5642 Exhibit A, a three-bidder unit-price tab; NRH
# 5788; Farmers Branch 24-18) and 10 Word documents (Lewisville bid tabs,
# Keller 26-809).  pdfminer raised 'No /Root object! - Is this really a PDF?'
# on every one, main() printed FAILED and the file contributed nothing.  The
# CONTENT decides: a workbook goes to read_xlsx; a Word document has no reader
# here (those ten print bidder totals, or one vendor's prices, not a tab) and
# is recorded as skipped instead of failing.
def sniff_office(path):
    """'xlsx' / 'docx' for an Office (OOXML) package, whatever the file is
    called; '' for anything else."""
    import zipfile
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"PK\x03\x04":
                return ""
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile):
        return ""
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "word/document.xml" in names:
        return "docx"
    return ""


def read_xlsx(path, stats):
    recs, audit, strings = [], [], []
    base = os.path.basename(path)
    # Through a handle, not the path: openpyxl refuses a path whose extension
    # is not .xlsx, and a workbook can arrive named .pdf (sniff_office).  The
    # handle stays open for the whole read -- read_only reads lazily from it.
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, data_only=True, read_only=True)
        try:
            for ws in wb.worksheets:
                grid = []
                for row in ws.iter_rows(values_only=True):
                    grid.append([txt(c) for c in row])
                    joined = " | ".join(x for x in grid[-1] if x)
                    if joined:
                        strings.append(joined)
                r, a = rows_from_grid(grid, base, ws.title, stats)
                recs.extend(r)
                if a:
                    a.update({"src_file": base, "src_sheet": ws.title, "n_rows": len(r)})
                    audit.append(a)
        finally:
            wb.close()
    return recs, audit, strings


NUMERIC_UNIT_RE = re.compile(r"^[\d.,]+$")


def _unit_valid_rate(recs):
    """Share of rows whose unit field actually looks like a unit of measure.

    This is a SECOND, independent quality signal, and it exists because the
    arithmetic check has a blind spot: when a column mapping slips so far that
    the quantity column ends up empty, there is nothing to verify and the bad
    parse passes silently. One Dallas County template produced 354 rows with
    item_no="100-6002 PREPARING ROW" and unit="STA 109.80" -- every field
    shifted, no arithmetic to contradict it. A unit that parses as a number is
    the tell.
    """
    tot = ok = 0
    for r in recs:
        u = (r.get("unit") or "").strip()
        raw = (r.get("unit_raw") or "").strip()
        if not u and not raw:
            continue
        tot += 1
        if u in KNOWN_UNITS and not NUMERIC_UNIT_RE.match(raw):
            ok += 1
    return ((ok / tot) if tot else None), tot


def _arith_ok_rate(recs):
    """Share of rows whose own quantity x unit price reproduces the printed
    extended amount. This is the objective score used to choose between the two
    PDF strategies -- it needs no ground truth beyond the page itself."""
    ok = bad = missing_e = have_e = 0
    for r in recs:
        q, u, e = r.get("quantity"), r.get("unit_price"), r.get("extended_reported")
        if e is not None:
            have_e += 1
        if q is None or u is None:
            continue
        if e is None:
            missing_e += 1
            continue
        if abs(q * u - e) / max(abs(e), 1.0) <= 0.005:
            ok += 1
        else:
            bad += 1
    # None, not 0.0, when there is nothing to check: a source with no extended
    # column yields no evidence either way, and scoring that as 0% made such a
    # parse always lose to one that merely had a total column to agree with.
    if have_e == 0:
        return None, 0
    # PERVERSE INCENTIVE, now closed: a total cell we could not read used to be
    # dropped from the denominator, so the more cells a broken parse mangled the
    # FEWER rows were checked and the closer the score crept to 1.00.  Arlington
    # PWSW22001 scored 1.0 on 40 checkable rows out of 62 while an entire
    # bidder's $150,000 LS row was missing; mckinney 2021-03-24 scored 1.0 with
    # most of 6 of 10 bidders' prices absent.  An unreadable extended cell in a
    # template that plainly HAS an extended column is evidence of damage.
    #
    # Counted UNCONDITIONALLY once the column is known to exist, so the score
    # falls monotonically as damage rises and cannot be gamed by mangling more
    # cells.  A share threshold here would reinstate the same perversity in
    # milder form: with 6 of 10 totals unreadable the populated share drops
    # below the threshold and the penalty switches itself off exactly when the
    # damage is worst.
    #
    # Over-penalising a genuinely sparse template is not a hazard, because this
    # score is only ever used to compare the TWO strategies on the SAME file.
    # Both meet the same template, so a sparse total column lowers both scores
    # equally and the comparison is unaffected.
    bad += missing_e
    return ((ok / (ok + bad)) if (ok + bad) else None), ok + bad


def _pages_of(recs):
    """Page numbers a parse actually produced rows for.

    Both strategies encode the page in src_sheet -- tables as "p3t0", coords as
    "coords_p3" -- so one pattern reads either.
    """
    out = set()
    for r in recs:
        m = re.search(r"p(\d+)", str(r.get("src_sheet") or ""))
        if m:
            out.add(int(m.group(1)))
    return out


def _bidders_of(recs):
    """Distinct non-empty bidder names in a parse.

    This is the coverage signal the chooser was missing.  Arithmetic
    self-consistency is a property of the rows that SURVIVED, so a parse that
    kept one bidder column out of four and computed it perfectly scores 1.00 --
    which is how the chooser came to prefer a parse missing 80% of the bids and
    stamp it objectively best.

    Counted through looks_like_bidder, not raw.  A strategy that read a BID
    STATUS where the company name goes -- Arlington's Lower Johnson Creek pond
    dredging prints NONRESPONSIVE in four columns -- otherwise scores four
    bidders and wins, while the coordinate strategy that recovered the two
    RESPONSIVE firms and 83 rows loses.  Counting a string the downstream
    accept test would reject is not coverage; it is the same mistake this
    function was written to fix, one level further in.
    """
    out = set()
    for r in recs:
        s = str(r.get("bidder_raw") or "").strip()
        if s and looks_like_bidder(s):
            out.add(s)
    return out


# ---- a quantity printed ROUNDED --------------------------------------------
# A tab can hold a fractional quantity and DISPLAY it rounded.  Lewisville RFB
# 25-98-C prints item #0-13 RIPRAP (MOW STRIP)(5 IN) as 9 CY, but every
# column's extension is 9.24 x its own unit price (600 -> 5,544.00, 1,650 ->
# 15,246.00, 1,785 -> 16,493.40, the OPCC's 750 -> 6,930.00).  Every cell then
# failed quantity x unit_price == extended, was flagged ext_mismatch and left
# the panel for every bidder at once: $5,544 off the low bid.  North Richland
# Hills prints 23.5 LF of 27" RCP as 24 (Bid Tab 4180) and 0.5 AC of clearing
# and grubbing as 1 (Bid Tab 5428) the same way.
#
# The quantity cell is SHARED by every column of its grid row, so the row can
# prove the display itself: when EVERY priced column's own extension agrees on
# one quantity q*, and q* shows as the printed number at the printed precision
# (within half a unit of its last printed digit), the printed number is q*
# rounded for display.  A column that disagrees with the others says something
# about that column -- a bidder's arithmetic, or an estimate built on its own
# quantity (Lewisville's OPCC extends item #0-10 on 49.7 CY where the form and
# all three bidders say 50) -- not about the shared cell, so such a row is left
# alone and stays flagged.  The test is the identity ext_mismatch audits with,
# used as proof.  It must also exclude every OTHER story a common factor can
# tell (see the guard in the function): a lump sum or a quantity of 1 is
# never "rounded", q* needs at most two decimals and must match every column
# to the cent, and it must sit strictly inside half a unit of the last
# printed digit -- or exactly half a unit BELOW it, which is how a spreadsheet
# displays a half (3.5 as 4; parser round 3, 2026-11-19).  Measured by
# replaying every external file: the guard leaves Lewisville's #0-13 (4 rows)
# as the one recovery; NRH 5428's and Farmers Branch R2022-074's 0.5 printed 1
# (a quantity of 1) stay flagged.  NRH 4180's 23.5 LF printed 24 (exactly on
# the half) was refused too until the half-up display was admitted.
_QTY_TOL = 0.005          # the ext_mismatch tolerance, relative to the extension


def _shown_decimals(q):
    """Decimals the printed quantity carried, read back from its float."""
    s = ("%.6f" % q).rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def recover_display_rounded_quantities(recs, stats=None):
    """Set quantity = q* (qty_source 'display_rounded_verified') on every cell
    of a grid row whose priced columns ALL extend on one q* that the printed
    quantity is a rounded display of.  Returns (grid rows, records) moved."""
    groups = {}
    for r in recs:
        q = r.get("quantity")
        if q is None or q <= 0:
            continue
        k = (r.get("src_file"), r.get("src_sheet"), r.get("bid_section"),
             r.get("item_no"), r.get("description"), r.get("unit_raw"), q)
        groups.setdefault(k, []).append(r)
    moved = n_recs = 0
    for k, grp in groups.items():
        q = k[-1]
        # Display rounding is the ONLY explanation this test may accept, and
        # a common factor in every column's extension looks just like it:
        # printed 5 with every extension 1.1 x 5 x price "recovers" 5.5, a
        # lump sum extended at 1.4 x price (a two-phase sheet) "recovers"
        # 1.4.  So: never a lump sum or a quantity of 1 (there is nothing to
        # round, and a factor is the likelier story); q* must reproduce EVERY
        # column to the cent with at most two decimals (no tolerance-based
        # fallback); and q* must lie strictly within half a unit of the
        # printed quantity's last printed digit -- 5.5 shown as 5 is not a
        # rounding (the exact half: see below).
        # (This drops Farmers Branch R2022-074's and NRH 5428's 0.5 printed as
        # 1; they stay flagged ext_mismatch.)
        if q == 1 or norm_unit(k[5]) == "LS":
            continue
        cells = [r for r in grp if (r.get("unit_price") or 0) > 0
                 and (r.get("extended_reported") or 0) > 0]
        if len(cells) < 2:
            continue                # one column proves nothing about a shared cell

        def fits(qq, cents=False):
            return all(abs(qq * r["unit_price"] - r["extended_reported"])
                       <= (0.0101 if cents else
                           _QTY_TOL * max(abs(r["extended_reported"]), 1.0))
                       for r in cells)
        if fits(q):
            continue                # the printed quantity explains the row
        implied = sorted(r["extended_reported"] / r["unit_price"] for r in cells)
        mid = implied[len(implied) // 2]
        # The fewest decimals (at most two) that reproduce every extension to
        # the cent.
        qstar = next((c for c in (round(mid, d) for d in range(3))
                      if c > 0 and fits(c, cents=True)), None)
        if qstar is None or qstar == q:
            continue
        # The exact half counts only the way a spreadsheet displays it, half
        # AWAY from zero: Cedar Hill PW22-0010 prints item 5 (ductile iron
        # fittings) as 4 TON while all seven columns extend 3.5 TON to the
        # cent ($68,786.55 = 3.5 x $19,653.30 for the low bidder) -- 3.5 shows
        # as 4.  A q* half a unit ABOVE the printed number (4.5 printed 4)
        # is no display of it, and stays refused.
        half = 0.5 * 10 ** -_shown_decimals(q)
        if abs(qstar - q) > half + 1e-9 or (abs(abs(qstar - q) - half) <= 1e-9
                                            and abs(qstar) > abs(q)):
            continue                # not a rounding of the printed number
        for r in grp:
            r["quantity"] = qstar
            r["qty_source"] = "display_rounded_verified"
        moved += 1
        n_recs += len(grp)
    if stats is not None and moved:
        stats["display_rounded_quantity"] = {"grid_rows": moved, "rows": n_recs}
    return moved, n_recs


# ---- a quantity that was never read, given back by the row's own arithmetic --
# A row can carry every bidder's unit price and extension and no quantity: the
# quantity column was not found (Mansfield 22-5047 page 1 prints its 21 paving
# items with the item column holding the descriptions and the quantity column
# unread), or the template has none.  Downstream needs quantity x price, so
# every such row was dropped from the winner's total and the price pool --
# 14,567 rows in 117 files on the 2026-11-19 20:10 build, and 22-5047 reached
# the estimator as $559,053 against a printed $1,158,841.  The quantity is
# extension / unit price.  It is taken only where the row proves it: two or
# more priced columns of the same grid row (bidders, the estimate, unnamed
# columns -- all of them), none with a quantity, every column with a price
# also printing an extension, and ONE quantity of at most two decimals that
# reproduces every column's extension within the ext_mismatch tolerance.  One
# column proves nothing (its "extension" may be another column), and columns
# that imply different quantities are left alone.  qty_source
# 'implied_by_columns', the name parse_ocr_sidecars gives the same proof.
def recover_quantity_from_extensions(recs, stats=None):
    """Set quantity (qty_source 'implied_by_columns') on every cell of a grid
    row that has none, when 2+ priced columns agree on extension / price.
    Returns (grid rows, records) recovered."""
    by_sheet = {}
    for r in recs:
        by_sheet.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    moved = n_recs = 0
    for srecs in by_sheet.values():
        for row in _grid_rows(srecs):
            cells = [r for _, r in row]
            if any(r.get("quantity") is not None for r in cells):
                continue
            priced = [r for r in cells if (r.get("unit_price") or 0) > 0]
            if len(priced) < 2 or any((r.get("extended_reported") or 0) <= 0 for r in priced):
                continue
            implied = sorted(r["extended_reported"] / r["unit_price"] for r in priced)
            mid = implied[len(implied) // 2]

            def fits(qq):
                return all(abs(qq * r["unit_price"] - r["extended_reported"])
                           <= max(0.0101, _QTY_TOL * abs(r["extended_reported"]))
                           for r in priced)
            qstar = next((c for c in (round(mid, d) for d in range(3))
                          if c > 0 and fits(c)), None)
            if qstar is None:
                continue
            for r in cells:
                r["quantity"] = qstar
                r["qty_source"] = "implied_by_columns"
            moved += 1
            n_recs += len(cells)
    if stats is not None and moved:
        stats["quantity_from_extensions"] = {"grid_rows": moved, "rows": n_recs}
    return moved, n_recs


# ---- (round 11, .32) a quantity GLUED to the number printed after it -----------------------------------
# Rockwall's FM 549 utility relocation tab (CIP2020-004, page 1, read with inferred columns) prints each
# quantity hard against the first bidder's unit price, and the cell came back as one number: "1" and
# "15000" as 115,000 (mobilization), "1778" and "37" as 177,837 SY of sod, "2970" and "119" as 2,970,119
# LF of 12" water line -- every column's quantity x price missed its printed extension by orders of
# magnitude.  Frisco's 2023 downtown parking tab glued "475" to "186.53" (475,186.53 TON of asphalt), and
# a decimal point the text layer drops does the same ("20.00" read 2000 LF, "1.00" LS read 100).  The
# quantity is taken back where the row proves it: two or more priced columns of the grid row, each
# printing an extension, agree on ONE quantity q* (at most two decimals, within the ext_mismatch
# tolerance) that the printed quantity does not reproduce, and the printed quantity's digits BEGIN with
# q*'s digits and run on by one number (at most nine more digits: q* = 1778, printed 177837).  Anything
# else -- a factor, a rounding, a disagreement between columns, a whole packed column of numbers -- is
# left as printed and flagged by ext_mismatch.  qty_source 'glued_quantity_prefix'.
def _qty_digits(q):
    s = ("%.2f" % q).rstrip("0").rstrip(".")
    return s.replace(".", "")


def recover_glued_quantity_prefix(recs, stats=None):
    """See above.  Returns (grid rows, records) given back their quantity."""
    by_sheet = {}
    for r in recs:
        by_sheet.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    moved = n_recs = 0
    for srecs in by_sheet.values():
        for row in _grid_rows(srecs):
            cells = [r for _, r in row]
            qs = {r.get("quantity") for r in cells}
            if len(qs) != 1:
                continue
            q = qs.pop()
            if q is None or q <= 0:
                continue
            priced = [r for r in cells if (r.get("unit_price") or 0) > 0]
            if len(priced) < 2 or any((r.get("extended_reported") or 0) <= 0 for r in priced):
                continue

            def fits(qq):
                return all(abs(qq * r["unit_price"] - r["extended_reported"])
                           <= max(0.0101, _QTY_TOL * abs(r["extended_reported"]))
                           for r in priced)
            if fits(q):
                continue
            implied = sorted(r["extended_reported"] / r["unit_price"] for r in priced)
            mid = implied[len(implied) // 2]
            qstar = next((c for c in (round(mid, d) for d in range(3)) if c > 0 and fits(c)), None)
            if qstar is None or qstar >= q:
                continue
            dq, ds = _qty_digits(q), _qty_digits(qstar)
            if len(dq) <= len(ds) or not dq.startswith(ds):
                continue
            if len(ds) == 1 and len(dq) == 2:
                continue                  # 1 under a printed 10-19: too short to tell a glue from a factor
            if len(dq) - len(ds) > 9:
                continue                  # more than one number glued on: a packed column, not a cell
            for r in cells:
                r["quantity"] = qstar
                r["qty_source"] = "glued_quantity_prefix"
            moved += 1
            n_recs += len(cells)
    if stats is not None and moved:
        stats["glued_quantity_prefix"] = {"grid_rows": moved, "rows": n_recs}
    return moved, n_recs


# ---- a numbered line that is the SUBTOTAL of its sub-lines (parser round 3) --
# Some e-bid exports number a group's lines N.1, N.2, ... and print the group
# itself as line N, priced with the group's total: Allen 2025-6-51 (Watters
# Trail, Ionwave 'BidTab by Supplier') prints line 2 'Add-Alternative #1',
# 1 LS, $210,000 over 2.1 (2 EA x $20,000) and 2.2 ($170,000), and line 3
# 'Add-Alternative #2', $521,725, over 3.1-3.29.  Read as items, both groups
# counted twice: 2L Construction summed to $1,871,061.65 against a printed
# $1,139,336.65.  A line N of one bidder is such a subtotal when that bidder has
# two or more lines N.k in the file and their amounts add up to line N's
# (to the dollar, or 0.1%); it is dropped, its sub-lines kept.  And where the
# group line names an alternate -- it opens with the word: 'Add-Alternative
# #1', 'Additive Alternate No. 2', Celina's 2024 street maintenance 'Alternate
# - Dove Creek Street' -- its sub-lines are that alternate (bid_section), so
# the base bid does not take them.  A line N whose sub-lines do not add up to
# it is an item and stays.
_GROUP_ALT_RE = re.compile(
    r"^\s*(?:(?:add(?:itive)?|deduct(?:ive)?)[\s\-]*)?alternat(?:e|ive)s?\b", re.I)


def subtotal_parent_lines(recs, stats=None):
    """Drop group lines N that are the subtotal of their N.k lines (see above);
    returns the records kept."""
    def amt(r):
        e, q, u = r.get("extended_reported"), r.get("quantity"), r.get("unit_price")
        if e is not None:
            return e
        if u is None:
            return None
        return u * (q if q is not None else 1.0)

    groups = defaultdict(list)
    for i, r in enumerate(recs):
        # per bidder over the whole file: a group runs on across a page break
        # (Celina 2024-108's 'Alternate - CR 132' prints 6.1-6.3 on page 1 and
        # 6.4-6.7 on page 2); the sum test is what ties them together
        groups[(r.get("src_file"), r.get("bidder_raw"))].append(i)
    drop, alt_of = set(), {}
    for key, idx in groups.items():
        by_no = defaultdict(list)
        for i in idx:
            by_no[(recs[i].get("item_no") or "").strip()].append(i)
        for no, parents in by_no.items():
            if not re.fullmatch(r"\d{1,3}", no) or len(parents) != 1:
                continue
            kids = [i for i in idx if re.fullmatch(re.escape(no) + r"\.\d{1,3}",
                                                   (recs[i].get("item_no") or "").strip())]
            if len(kids) < 2:
                continue
            pa = amt(recs[parents[0]])
            ka = [amt(recs[i]) for i in kids]
            if pa is None or pa <= 0 or any(a is None for a in ka):
                continue
            if abs(sum(ka) - pa) > max(1.0, 0.001 * pa):
                continue
            drop.add(parents[0])
            d = txt(recs[parents[0]].get("description"))
            if _GROUP_ALT_RE.match(d):
                for i in kids:
                    alt_of[i] = d.upper()[:60]
    if not drop:
        return recs
    for i, lab in alt_of.items():
        recs[i]["bid_section"] = lab
    if stats is not None:
        stats["subtotal_group_lines_dropped"] = stats.get("subtotal_group_lines_dropped", 0) + len(drop)
        stats["group_alternate_lines"] = stats.get("group_alternate_lines", 0) + len(alt_of)
    return [r for i, r in enumerate(recs) if i not in drop]


CACHE_DIR = os.path.join(ROOT, "data", ".cache", "parse")
# Bump whenever parsing behaviour changes, or a re-run silently serves results
# produced by the OLD parser -- the cache is keyed on file content, which does
# not change when the code does.
#
# Do NOT bump it for a change to FAMILY_RULES alone.  cached_parse() below
# re-derives item_family / size_in / material / unit from the cached text on
# every cache hit, precisely so a family fix lands without re-extraction, so a
# family-only change can never serve a stale classification -- the condition
# above is not met.  Bumping anyway invalidates all 10,129 cache entries
# (253 MB) and buys a full re-parse of the PDF corpus for an output that is
# byte-identical.  The 2026-11-18 grout_additive reordering was family-only and
# deliberately left this string alone.
# 2026-11-19.18: one bump for the overnight parse fixes, which change parsing
# logic, not just family rules -- bidder ordinal captions, alternate banners
# and captions, total-caption rows, MG/TGL units, display-rounded quantities,
# merged price/total captions (NRH 7122), newline-packed rows (Coppell 5639),
# and consulting-engineer estimate columns (ENGINEER_RE).
PARSER_VERSION = "2026-11-21.34b-r20b-families"  # .34b: p50fix round 20 addendum (reports/p50_fix_round20.md): lines priced from a mixture cell for want of a family -- flashing beacons, flashers, crosswalk lights, signal poles and mast arms are traffic_signal, markings named by their line (stop bar, solid / broken / double yellow, raised markers, a marked crosswalk) are pavement_marking, a sign named by its MUTCD code or panel size is signing; .33: parser round 12 (reports/parser_round12.md): the place does not decide the family -- a trail / sidewalk named beside earthwork or planting words with no surfacing material named yields to grading / excavation / embankment_fill / seeding_sod ("Excavation (Trail)", "Trail Grading (Excavation and finish grading)", "Sidewalk Construction and Repair: Bermuda grass - price to include 4 inch Topsoil"), bare "earthwork" is excavation, a sidewalk / trail WALL is a retaining wall, a trail boardwalk or fishing pier is a structure (`other`, like a pedestrian bridge), TRAIL MNT / RAD TRAIL is a trailer; abandonment of an existing pipe, sewer, water line or service (abandonment plug, cut / cap / plug / grout existing, flowable fill in existing, "water service abandonment") is demo_utility, not new pipe; .32: parser round 11 (reports/parser_round11.md): units the documents print but the parse did not read -- a unit column under no unit caption read by its cells (_unit_column_by_content), the captions U/M, Measure and Units of Measure, a unit column stacked into one cell, a quantity cell that carries its unit ("154.83 AC"), the unit (and quantity) read into the end of the description (units_at_end_of_description), the unit and quantity the item-cell words dropped; a location word before "culvert" (driveway culverts are pipe, box, removal or reset), a drainage conveyance's cleanout is cleaning_desilting; a turned page's upright stamp words dropped (parse_coords), a quantity glued to the number printed after it (recover_glued_quantity_prefix), a lone rank number that continues the file's series; .31: parser round 10 (reports/parser_round10.md): held tabs that read nothing and names that are not bidders -- the engineer's column under a misspelt caption ("ENGINEER OPION OF COST") or named for the firm whose letterhead heads the tab (mark_preparer_columns); a form answer, award remark, rank caption ("2nd LOWEST"), legend, contact line, "Bidders Name & Address" caption or template "Address Line 1" never a name, and such form rows not counted against the names walk; a firm named with a leading number ("308 Construction") and a spaced "P . O . Box" read right; a price caption stacked over two header rows ("UNIT" over "COST": stacked_caption_header); a price column under a wrong caption proved by quantity x price = total, in per-bidder Unit / Qty / Price / Total blocks (proved_price_blocks); coordinate strategy (parse_coords): a section banner anywhere on its line and in any case, an item number printed under its prices, a description column right of the quantity and unit columns; .30: parser round 9 (reports/parser_round9.md): letting labels and the generic small-job line defects -- month names in any case / abbreviated ("BID OPENING: JULY 8, 2026" was no date), a weekday / clock time / "certified on" line as a weak bid date, a lone uncued document date before the bid-number year or the agenda date, a January opening printed with last year (reconcile_bid_date), annual / as-needed / work-order contracts as on-call (not a project total), apparel never a pipe fitting, mill-and-remove as pavement_milling, microcracking as subgrade_stab, "burst 6\" CI to 8\" HDPE" as pipe_bursting, RRFB as traffic_signal, SSMH / SDMH as manhole, painted curb address numbers as pavement_marking, no size from a thickness on an EA / LS line; .29: parser round 8 (reports/parser_round8.md): municipal lines ranked by dollars mispriced -- new families geotextile / landscape_planting / bypass_pumping / adjust_to_grade / concrete_repair / bridge_concrete / fire_hydrant / valve_assembly (valve vs assembly; a hydrant carries no size), FDR / pulverizing / reworking base / cement treatment as subgrade_stab, crushed concrete or stone as flex_base, hydromulch spellings / revegetation / parkway restoration as seeding_sod, concrete roadway / RC / panel-replacement pavement, TxDOT abbreviations (RMV, PLAN ASPH, EMBANK, JCTBOX, TRF SIG, SP MIXES, SOIL RET BLKT, ACP, R.O.W.) read as words, push buttons / backplates as traffic_signal, the item number and quantity read into the end of a description; .28: parser round 7 (reports/harvest_city_2026-11-20.md and harvest_city2_2026-11-20.md section 4): linear EA items -- tracer / locator wire, warning / detection tape, bedding / embedment -- lose an EA unit read by the hundred (ea_quantity_suspect, money kept); descriptions that are only a code: a merged ITEM NO. / DESCRIPTION caption split by content (_split_merged_item_caption), words read into the item cell and wrapped tails restored from the same item elsewhere in the file (recover_code_only_descriptions), the rest unit-less with unit_note desc_code_only; the width in feet as the size of an LF sidewalk / trail (width_ft); "TxDOT item 500" / 500-6001 / "mobiliization" as mobilization; .27: fixes from the erosion / drainage line diagnosis (handoff/scratchpad/diag_ed/, 2026-11-20): the families cleaning_desilting / irrigation / railing, joint cleaning / resealing as conc_joints, trenchless crossings (HDD, directional drill, bored, jack and bore, trenchless) as encasement, force mains as sanitary_pipe, stream-restoration shaping out of grading and grading verbs on ditches / swales in, bed preparation as seeding_sod, right-of-way preparation spelt out as site_prep_clearing, tie backs as rock_anchor, hydrostatic tests as testing_inspection, conduit / cable EA x 500+ unit_suspect (ea_quantity_suspect), a lump sum printed with a measure unit as LS (measure_qty1_lump_sums); .26: fixes from the worst-lettings diagnosis and the statewide integration test (reports/diag_worst_municipal_2026-11-20.md, reports/statewide_integration_test.md): item numbers printed into descriptions (strip_leading_item_numbers), sizes above 144 in or read from a dropped decimal point, box culverts sized by opening area (box_culvert_size), a quantity stated in an LS description (apply_ls_quantity_in_description), EA x 500+ on a pipe line unit_suspect (ea_quantity_suspect), and the families flowable_backfill / grading / pipe_bursting / sawcut / painting / pavers / manhole_extra_depth plus misspelt mobilization and contingency, TV inspection, borrow as embankment, "pipe and fittings", "water maintain", R.C.P.; .25: fixes from the 2026-11-20 tail audit (reports/tail_audit_2026-11-20.md): a BASE BID banner glued to item 1 (GLUED_BASE_RE), a rank caption in a bidder name (strip_rank_caption), a unit cell that repeats the quantity (unit_cell_repeats_quantity), alternate-titled schedules (section_banner), an item every bidder's printed total leaves out (items_outside_printed_total), on-call hints in doc_facts (contract_hint); .24c: pavement_milling (TxDOT planing, cold milling) and surface_treatment (tack / prime coat, seal coats, microsurfacing, crack sealing) out of asphalt_pavement, whose per-SY cells they were pulling to $2-4/SY; .24b: material supply (delivered / picked-up material, TxDOT (DEL)/(RDWY DEL), city plant pickup) stays `other`, so supply-only lettings stay out of the construction pool; .24: item-family round 3b (the main noun phrase decides, remove-and-replace is what it builds, box_culvert / lighting_assembly and missing vocabulary; _family_rules_selftest) -- family-only, but bumped on request so the whole corpus is re-parsed and every artefact re-derived in one consistent pass; .23: parser round 3 (reports/parser_round3.md): multi-line / split / merged headers, hidden text, names, packets (per-page dates, agenda stamps), quantity columns, alternates, lump sums; .20: fixes from the Mac verification of the zero-row rules (reports/parser_fix_verification.md); .21: image-only scans read from their OCR sidecars (src/parse_ocr_sidecars.py); .22: quantity from extensions, LS rows kept in totals, document facts for the completeness gate, lump sums printed as amounts, P.E./BID OF/section totals, locked public test set (reports/parser_fix_verification.md section 14)


def _cache_key(path):
    """Content hash, not mtime: the agents feeding this project re-download and
    re-touch files, which would invalidate an mtime-keyed cache for no reason."""
    import hashlib
    h = hashlib.sha1()
    h.update(PARSER_VERSION.encode("ascii"))
    h.update(os.path.basename(path).encode("utf-8", "ignore"))
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def cached_parse(path, stats, parse_fn):
    """Parsing 500+ PDFs twice over (table strategy AND coordinate strategy)
    takes about an hour, and more files keep arriving. Cache per file so a
    re-run only touches what is new or changed."""
    import pickle
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        key = _cache_key(path)
    except OSError:
        return parse_fn(path, stats)
    cf = os.path.join(CACHE_DIR, key + ".pkl")
    if os.path.exists(cf):
        try:
            with open(cf, "rb") as fh:
                recs, audit, strings, sub = pickle.load(fh)
            for k, v in (sub or {}).items():
                if isinstance(v, int):
                    stats[k] = stats.get(k, 0) + v
                elif isinstance(v, dict):
                    stats.setdefault(k, {}).update(v)
            stats["cache_hits"] = stats.get("cache_hits", 0) + 1
            # Re-derive the CLASSIFICATION from the cached text rather than
            # trusting what was cached. Item-family rules keep gaining
            # vocabulary -- municipal tabs call a channel grade beam a "toe
            # wall", TxDOT writes "REMOV STR (PIPE)" -- and without this every
            # such fix would mean re-parsing 1,100 PDFs for an hour. Extraction
            # is what is expensive and what is cached; classification is cheap
            # and now always current.
            for r in recs:
                dn = r.get("desc_norm") or ""
                r["item_family"] = item_family(dn)
                r["size_in"] = extract_size_in(dn)
                m = MATERIAL_RE.search(dn)
                r["material"] = (m.group(1).upper() if m else "")
                r["unit"] = norm_unit(r.get("unit_raw") or "")
            return recs, audit, strings
        except Exception:
            pass
    before = {k: v for k, v in stats.items() if isinstance(v, (int, dict))}
    out = parse_fn(path, stats)
    sub = {}
    for k, v in stats.items():
        if isinstance(v, int) and k != "cache_hits":
            d = v - before.get(k, 0)
            if d:
                sub[k] = d
        elif isinstance(v, dict) and k == "pdf_strategy":
            b = os.path.basename(path)
            if b in v:
                sub[k] = {b: v[b]}
    try:
        with open(cf, "wb") as fh:
            pickle.dump((out[0], out[1], out[2], sub), fh,
                        protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass
    stats["cache_misses"] = stats.get("cache_misses", 0) + 1
    return out


# ---- (round 9, .30) the bid date read against what the FILE NAME says -------------------------------
# The plans pilot (reports/plans_pilot_2026-11-20.md section 8) checked lettings against the owners'
# own records and found three generic date faults:
#   * a January opening printed with LAST year: "Date: January 22,2024" on a tab filed with the
#     council agenda of 2025-02-17 (its sibling item, same agenda, prints January 22, 2025).  A bid is
#     not awarded 13 months after it opens; one year later lands three weeks before the agenda.
#   * a tab whose only date has no cue word -- "Certified on: June 6, 2024", "Thursday, May 19,2022 @
#     11:00A.M." -- was dated by its bid number's YEAR (YYYY-01-01, scored as mid-year) or by the
#     agenda date weeks later.  The lone date of the tab itself is the better reading when it agrees
#     with what the name says (within UNCUED_BEFORE_FN_DAYS before the agenda date, or within a year
#     of the bid number's year) and the document's dates agree with each other (UNCUED_SPAN_DAYS).
#   * (DATE_TXT) capitals: "BID OPENING: JULY 8, 2026" was no date.
# Every corrected date stays approximate (its source is not one of build_master's precise ones), so
# none of them enters the escalation index.
_FN_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_")
_FN_BIDNO_YEAR_RE = re.compile(r"^(20\d{2})-\d{1,4}(?=[^0-9]|$)")
YEARFIX_GAP_DAYS = 300        # an opening this long before the agenda date is no opening of it ...
YEARFIX_WINDOW_DAYS = 120     # ... but one year later it lands this close before the agenda date
UNCUED_BEFORE_FN_DAYS = 120   # a tab's lone date: at most this long before the agenda date
UNCUED_SPAN_DAYS = 31         # and all of the document's dates within a month of each other
WEAK_AFTER_FN_DAYS = 30       # a weak date this long after the file name's date is no opening


def _filename_date(base):
    m = _FN_DATE_RE.match(base or "")
    if not m:
        return ""
    import datetime as _dt
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def reconcile_bid_date(bd, src, every, base, lines=None):
    """(bid_date, source) after the round-9 checks against the file name (see above).

    bd, src, every: find_dates' result; base: the file's name; lines (optional): the text lines find_dates
    read, so a stamp line's date never counts as the document's own.  A date is changed only
      * '_yearfix': a January / February date more than YEARFIX_GAP_DAYS before the file name's date,
        when the document prints no later date and the same day one year later falls within
        YEARFIX_WINDOW_DAYS before it;
      * 'doc_date_uncued_approx': no cued date at all, and the document's own dates agree (see above);
      * a weak date more than WEAK_AFTER_FN_DAYS after the file name's date is dropped (the caller
        then takes the file name's date)."""
    import datetime as _dt
    D = _dt.date.fromisoformat
    today = _dt.date.today().isoformat()
    fdate = _filename_date(base)
    bn = None if fdate else _FN_BIDNO_YEAR_RE.match(base or "")
    if lines is not None:
        # the document's dates, less those on a line that dates something else (a print / plot stamp,
        # a page footer, a start or completion date: NOT_BID_DATE_RE)
        every = [d for s in lines if s and not NOT_BID_DATE_RE.search(s) for d in _dates_in(s)]
    seen = sorted({d[:10] for d in (every or ()) if d[:10] <= today})
    if bd and fdate and src != "MISSING" and "ambiguous" not in src:
        try:
            d, f = D(bd[:10]), D(fdate)
        except ValueError:
            d = f = None
        if d is not None and d.month <= 2 and (f - d).days > YEARFIX_GAP_DAYS \
                and all((f - D(x)).days > YEARFIX_GAP_DAYS for x in seen):
            try:
                d1 = d.replace(year=d.year + 1)
            except ValueError:                       # 29 February
                d1 = d.replace(year=d.year + 1, day=28)
            if 0 <= (f - d1).days <= YEARFIX_WINDOW_DAYS:
                return d1.isoformat(), src + "_yearfix"
    if bd and fdate and src.startswith("weak") and (D(bd[:10]) - D(fdate)).days > WEAK_AFTER_FN_DAYS:
        # a WEAK date (no opening cue) more than a month after the date the file is filed under is a
        # meeting, award, expiry or print date ("Meeting Date - September 1, 2026" on a tab opened July
        # 8; "Cooperative Contract Expiration Date 5/31/2025"): the file name's date is nearer.  Not
        # within a month: some harvests name a file by an agenda item created BEFORE the opening
        # (measured: cued openings 1-13 days after the file's date in two folders), and a cued
        # opening date after it always stays (an addendum can move the opening).
        return "", "MISSING"
    if not bd and seen:
        c = seen
        if fdate:
            c = [x for x in c if 0 <= (D(fdate) - D(x)).days <= UNCUED_BEFORE_FN_DAYS]
        elif bn:
            y = int(bn.group(1))
            c = [x for x in c if y - 1 <= int(x[:4]) <= y + 1]
        if c and (D(c[-1]) - D(c[0])).days <= UNCUED_SPAN_DAYS and len(c) == len(seen):
            return c[-1], "doc_date_uncued_approx"
    return bd, src


# ---- (round 10, .31) the engineer's column named for the firm that PREPARED the tab -------------
# A tabulation is prepared by the owner or its design engineer, never by a bidder.  Mansfield's Debbie
# Lane tab is a 'Bid Tabulation Report' under the letterhead 'Hanna Survey and Engineering LLC /
# Consulting Engineers', and its first price column is headed by that same firm: the engineer's
# estimate (project subtotal 392,800), which no name rule can see (firms named '... Engineering &
# Construction' DO bid: WG Engineering, Knight Engineering).  A bidder column whose name is, word for
# word, a letterhead line of the document -- one of its first PREPARER_HEAD_LINES text lines, a firm
# alone on its line, carrying a design-profession word itself or on the line right under it -- is
# the preparer's estimate.
PREPARER_HEAD_LINES = 6
_PROFESSION_RE = re.compile(
    r"\bengineer(?:s|ing)\b|\bsurvey(?:ing|ors?)\b|\barchitect(?:s|ure)\b|\bplanners\b", re.I)
_PROFESSION_LINE_RE = re.compile(
    r"^\W*(?:consulting\s+|professional\s+|civil\s+|design\s+)?(?:engineers?|engineering|surveyors?|"
    r"surveying|architects?|planners)\b", re.I)
_FIRM_SUFFIX_KEY_RE = re.compile(r"\b(?:llc|l l c|inc|incorporated|lp|l p|ltd|co|company|corp|"
                                 r"corporation|pllc|p l l c|llp|l l p|pc|p c)\b")


def _firm_key(s):
    """'Hanna Survey and Engineering LLC' -> 'hanna survey and engineering'."""
    s = re.sub(r"[^a-z0-9& ]+", " ", txt(s).lower())
    return " ".join(_FIRM_SUFFIX_KEY_RE.sub(" ", s).split())


def preparer_firms(lines, head=PREPARER_HEAD_LINES):
    """{firm key} of the letterhead lines naming the document's preparer (see above)."""
    ls = [txt(x) for x in (lines or [])[:head + 1]]
    out = set()
    for i, s in enumerate(ls[:head]):
        if (not s or len(s) > 80 or re.search(r"\$|\d{3,}", s) or not _FIRM_WORD_RE.search(s)
                or _PROFESSION_LINE_RE.match(s)):
            continue                      # (a line that opens with the profession names no firm)
        nxt = ls[i + 1] if i + 1 < len(ls) else ""
        if _PROFESSION_RE.search(s) or (nxt and _PROFESSION_LINE_RE.match(nxt)):
            k = _firm_key(s)
            if len(k) >= 6:
                out.add(k)
    return out


def mark_preparer_columns(recs, lines, stats=None):
    """Flag the rows of a bidder column named for the document's preparer as the engineer's
    estimate (preparer_firms); returns the number of rows flagged."""
    prep = preparer_firms(lines)
    if not prep:
        return 0
    n = 0
    for r in recs:
        if not r.get("is_engineer_estimate") and _firm_key(r.get("bidder_raw")) in prep:
            r["is_engineer_estimate"] = True
            n += 1
    if n and stats is not None:
        stats["engineer_column_by_letterhead"] = stats.get("engineer_column_by_letterhead", 0) + n
    return n


def _parse_one_file(job):
    """Parse ONE input file start to finish.

    Module-level and self-contained so it can run in a worker process: it owns
    its own stats dict and returns it for the parent to merge, rather than
    mutating shared state. Parsing is per-file independent and CPU-bound in
    Python (pdfplumber walks content streams), so this is where the parallelism
    lives -- the work is embarrassingly parallel and was running on one core.
    """
    f, path, source_set, owner = job
    stats = {"cells_blank_price": 0, "sheets_no_header": []}
    try:
        # The extension names the reader only for a real PDF: an Office file
        # under a .pdf name is read by what it contains (sniff_office).
        kind = "xlsx" if f.lower().endswith(".xlsx") else sniff_office(path)
        if kind == "xlsx":
            recs, audit, strings = cached_parse(path, stats, read_xlsx)
            if not f.lower().endswith(".xlsx"):
                stats.setdefault("office_files_by_content", []).append(f + " (xlsx)")
        elif kind == "docx":
            recs, audit, strings = [], [], []
            stats.setdefault("office_files_by_content", []).append(f + " (docx: not read)")
        else:
            recs, audit, strings = cached_parse(path, stats, read_pdf_best)
    except Exception as e:
        return {"f": f, "error": "%s: %s" % (type(e).__name__, e), "stats": stats}

    # An image-only scan has no text layer, so every reader above returns
    # nothing.  src/ocr_scans.py caches an OCR reading of it as a sidecar
    # (data/external/_ocr/<rel>.txt); parse_ocr_sidecars turns that into rows
    # in this same schema, keeping only rows whose qty x unit price = total and
    # whose bidder column is printed in the header.  Public files only: the
    # company's test files are never OCR'd into training anyway.  Not routed
    # through cached_parse, whose key is the PDF's bytes -- those do not change
    # when a scan is re-OCR'd.  Dates read from OCR text are tagged "_ocr" so
    # they never count as precise (build_master's date_precision).
    ocr_dates = False
    doc_lines = strings              # the text the document facts are read from
    if not recs and source_set in ("external", "public_test"):
        try:
            import parse_ocr_sidecars as pos          # lazy: it imports this module
            sc = pos.sidecar_for(path)
            if sc is not None:
                recs, audit, ocr_strings, _info = pos.parse_sidecar(sc, stats=stats)
                strings = pos.ocr_date_lines(ocr_strings)
                doc_lines = ocr_strings
                ocr_dates = True
        except Exception as e:                                    # noqa: BLE001
            stats.setdefault("ocr_sidecar_errors", []).append(
                "%s: %s: %s" % (f, type(e).__name__, e))
    # (round 10) a column named for the firm that prepared the tab is its estimate
    mark_preparer_columns(recs, doc_lines, stats)

    bd, src, every = find_dates(strings)
    bd, src = reconcile_bid_date(bd, src, every, os.path.basename(f), strings)   # round 9
    if bd and ocr_dates:
        src += "_ocr"
    # The Legistar harvest encodes the council-agenda date in the filename
    # (<intro_date>_<matter>_<attachment>.pdf). Use it only when the PDF itself
    # gives nothing, and label it honestly: an agenda date falls WEEKS AFTER the
    # bid opening, so it is an upper bound -- fine for a year-level index, not a
    # bid date.
    base = os.path.basename(f)
    fn = re.match(r"^(\d{4})-(\d{2})-(\d{2})_", base)
    if fn and not bd:
        y, mo, dd = int(fn.group(1)), int(fn.group(2)), int(fn.group(3))
        if 1 <= mo <= 12 and 1 <= dd <= 31 and 2000 <= y <= 2035:
            bd = "%04d-%02d-%02d" % (y, mo, dd)
            src = "filename_agenda_date_approx"
    if not bd:
        # Last resort: the solicitation number often leads the filename
        # ("2021-07_Lakeland_Heights..."). That gives a YEAR, but it is
        # the FISCAL year of the solicitation, not the opening date -- one
        # Mesquite file numbered 2020-015 was actually opened in 2019. So it is
        # recorded as approximate and must stay out of the escalation index,
        # which requires a precise source. It is still worth having for coverage
        # reporting and for recency weighting at year granularity.
        # NOT \b after the digits: underscore counts as a word character in
        # regex, so "2021-07_Lakeland..." would not match.
        bn = re.match(r"^(20\d{2})-\d{1,4}(?=[^0-9]|$)", base)
        if bn:
            bd = "%s-01-01" % bn.group(1)
            src = "bid_number_year_approx"
    for r in recs:
        r["src_file"] = f            # relative path, so the owner stays visible
        r["source_set"] = source_set
        r["owner"] = owner
        r["bid_date"] = bd
        r["bid_date_source"] = src
        pdt = r.pop("page_bid_date", "")
        if pdt and src.startswith("bid_open_or_due_ambiguous_span"):
            # the table's own page prints its opening date (page_opening_date)
            r["bid_date"] = pdt
            r["bid_date_source"] = "bid_open_table" + ("_ocr" if ocr_dates else "")
    for a in audit:
        a["src_file"] = f
    try:
        st = parse_summary_totals(path, f, owner)
    except Exception:
        st = []
    return {"f": f, "recs": recs, "audit": audit, "stats": stats,
            "summary": st, "bd": bd, "src": src, "every": every[:15],
            "source_set": source_set, "doc": document_facts(doc_lines)}


def input_source_set(rel):
    """Provenance of one input, by its path under data/:
       'internal'    a company file (top level of data/, no folder) -- test only;
       'public_test' a public file in the LOCKED public test set
                     (data/test_holdout/public_holdout.csv, src/public_holdout.py);
       'external'    every other public file -- the only set that trains.
    Every fitted artefact admits source_set == 'external' only, so marking a
    held-out file here keeps it out of the price pool, tables, index,
    discount, vocabulary and calibration in one place."""
    rel = (rel or "").replace("\\", "/")
    if "/" not in rel:
        return "internal"
    import public_holdout                 # not optional: a silent miss would train on the test set
    return "public_test" if public_holdout.is_holdout(rel) else "external"


def _worker_count():
    """Deliberately below the core count: other agent sessions are working in
    this same repo, and only ~6 GB of RAM is free while pdfplumber is memory
    hungry. Override with BIDTAB_WORKERS."""
    env = os.environ.get("BIDTAB_WORKERS")
    if env and env.isdigit():
        return max(1, int(env))
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 3))


def _merge_stats(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, int):
            dst[k] = dst.get(k, 0) + v
        elif isinstance(v, list):
            dst.setdefault(k, []).extend(v)
        elif isinstance(v, set):
            dst.setdefault(k, set()).update(v)
        elif isinstance(v, dict):
            dst.setdefault(k, {}).update(v)


def read_pdf_coords(path, stats):
    """Column-geometry strategy (see parse_coords). Produces the same records as
    the table strategy so the two are directly comparable."""
    from parse_coords import parse_pdf_by_coords
    base = os.path.basename(path)
    recs = []
    found = list(parse_pdf_by_coords(path))
    # An ordinal caption is honoured only as one of a series over the file's
    # bidder columns (ordinal_series_ok), as in bidder_names_for.
    captions = ordinal_series_ok(caption_ordinal(txt(b)) for b in
                                 set(r["bidder_raw"] or "" for r in found))
    for r in found:
        up = num(r["unit_price_raw"])
        if up is None:
            continue
        desc = txt(r["description"])
        item_no = txt(r["item_no"])
        dn = norm_desc(desc)
        mat = MATERIAL_RE.search(dn)
        section = r["bid_section"]
        alt = ALT_ITEM_RE.match(item_no)
        if alt:
            section = "ALT " + alt.group(1).upper()
        elif ROW_ALT_RE.match(desc):
            section = "ALT " + (item_no.upper() or "1")
        unit_raw = txt(r["unit_raw"])
        qty = num(r["quantity_raw"])
        if qty is None and not unit_raw:
            qu = split_quantity_unit(r["quantity_raw"])   # (round 11) '154.83 AC' in the quantity column
            if qu is not None:
                qty, unit_raw = qu
                stats["quantity_cell_with_unit"] = stats.get("quantity_cell_with_unit", 0) + 1
        # The same three rules rows_from_grid applies: an unmeasured total
        # caption is not an item, an ordinal caption is not part of the name,
        # and an alternate caption in the name makes the column an alternate.
        if is_total_label_row(item_no, desc, unit_raw, qty):
            stats["agg_label_rows_skipped"] = \
                stats.get("agg_label_rows_skipped", 0) + 1
            continue
        b = txt(r["bidder_raw"])
        bidder_raw, alt_sec = split_alternate_caption(
            strip_bidder_caption(b) if captions else b)
        recs.append({
            "src_file": base,
            "src_sheet": r["src_sheet"],
            "bid_section": alt_sec or section,
            "item_no": item_no,
            "description": desc,
            "desc_norm": dn,
            "item_family": item_family(dn),
            "size_in": extract_size_in(dn),
            "material": (mat.group(1).upper() if mat else ""),
            "unit_raw": unit_raw,
            "unit": norm_unit(unit_raw),
            "quantity": qty,
            "bidder_raw": bidder_raw,
            "bidder": canon_bidder(bidder_raw),
            "is_engineer_estimate": ENGINEER_RE.search(bidder_raw) is not None,
            "unit_price": up,
            "extended_reported": num(r["extended_raw"]),
        })
    split_glued_item_numbers(recs, stats)
    return recs


# ---- an item number glued to the description -------------------------------
# The coordinate strategy finds no item column on some templates and reads
# '101 Mobilization & Bonds', '201 8-inch High Early Strength Reinforced
# Concrete Paving' as the description with item_no empty (Arlington PWSM26007,
# PWDR12001C Matthews Ct; Mansfield 22-4816).  The number then sits in
# desc_norm, where the token matcher's size rule treats it as a dimension that
# must agree -- the 2026-11-19 out-of-fold run priced PWSM26007 at 0.405 of its
# low bid, 1.045 before the coordinate parse won.  The number is split back out
# only where a PAGE proves the column: at least three distinct descriptions
# with no item number, 80% or more of them opening with a number token, those
# numbers distinct (an item number is unique) and rising down the page.  A
# number followed by a unit word ('8 in. PVC', '2 Year Maintenance Bond',
# '12 LF ...') is a size or a count, never split.
_GLUED_ITEM_RE = re.compile(
    r"^\s*(#?\d{1,4}(?:[.\-]\d{1,3}){0,2}[A-Za-z]?)[.)]?\s+"
    r"(?!(?:in|in\.|inch|inches|ft|feet|foot|lf|l\.f\.|sy|sf|cy|ea|each|mm|yr|yrs|year|years|"
    r"month|months|day|days|week|weeks|lb|lbs|ton|tons|gal|psi|ksi|mil|sack|x|%|ac|acre|acres|"
    r"hr|hrs|hour|hours|mo|cf|ls|mg|kva|kw|amp|amps|v|volt)\b)(?=\S)", re.I)


def split_glued_item_numbers(recs, stats=None):
    """Move a leading item number out of the description into item_no, page by
    page, where the page proves it (see above).  Returns rows changed."""
    by_page = defaultdict(list)
    for r in recs:
        by_page[r.get("src_sheet")].append(r)
    moved = 0
    for _pg, rows in by_page.items():
        cand = [r for r in rows if not txt(r.get("item_no"))]
        descs = list(dict.fromkeys(txt(r.get("description")) for r in cand))
        if len(descs) < 3:
            continue
        hits = {}
        for d in descs:
            m = _GLUED_ITEM_RE.match(d)
            if m and d[m.end():].strip():
                hits[d] = (m.group(1).lstrip("#"), d[m.end():].strip())
        if len(hits) < 0.8 * len(descs):
            continue
        toks = [hits[d][0] for d in descs if d in hits]
        if len(set(toks)) < 0.8 * len(toks):
            continue

        def key(t):
            return tuple(int(x) for x in re.findall(r"\d+", t))
        rising = sum(1 for p, q in zip(toks, toks[1:]) if key(q) > key(p))
        if rising < 0.8 * (len(toks) - 1):
            continue
        for r in cand:
            h = hits.get(txt(r.get("description")))
            if not h:
                continue
            r["item_no"], r["description"] = h
            dn = norm_desc(h[1])
            mat = MATERIAL_RE.search(dn)
            r.update({"desc_norm": dn, "item_family": item_family(dn),
                      "size_in": extract_size_in(dn),
                      "material": (mat.group(1).upper() if mat else "")})
            moved += 1
    if stats is not None and moved:
        stats["item_numbers_split_from_description"] = \
            stats.get("item_numbers_split_from_description", 0) + moved
    return moved


# ---- the document's own totals as a tie-break -------------------------------
# Where the two strategies are equally clean and close in coverage, the table
# the document PRINTS settles it: a parse whose bidder totals (base bid, or
# all sections) appear among the amounts printed in the file has read that
# file's columns.  Arlington PWST19007 Center Street, PWST Entertainment
# District Sidewalks Ph3 and McKinney 22-0343 each tie on arithmetic; the
# coordinate parse totals to the printed TOTAL BASE BID AMOUNT for every
# bidder, the table parse to nothing (it reads the alternates or a borrowed
# header's rows into the base bid) -- and the tie went to the tables.
_PRINTED_AMOUNT_RE = re.compile(
    r"\(?-?\$?\s*-?\d[\d ,]*\.\d{1,10}(?!\d)\)?|\$\s*\d{1,3}(?:\s?,\s?\d{3})+(?![\d.])")


def printed_amounts(strings):
    """Every amount in the text lines, in cents ('$ 1 6,966,028.50' -> 1696602850)."""
    got = set()
    for ln in strings or []:
        for t in _PRINTED_AMOUNT_RE.findall(ln or ""):
            s = t.replace("$", "").replace("(", "").replace(")", "").replace("-", "").strip()
            for v in {s.replace(" ", "").replace(",", "")} | {
                    q.replace(",", "") for q in s.split() if "." in q}:
                try:
                    got.add(int(round(float(v) * 100)))
                except ValueError:
                    pass
    return got


def printed_total_hits(recs, cents):
    """How many bidders' base-bid (or all-sections) totals are printed.

    Only a column of TWO OR MORE rows counts.  A one-row column's total is its
    own extension, which the page prints on that row, so it 'matches' without
    proving anything: Mesquite 2022-062's coordinate parse split two bidders
    into one-row columns per page, every split column 'matched', and the tie
    went to the parse that named five firms seven ways (2026-11-19 Mac
    verification)."""
    base, allsec = defaultdict(float), defaultdict(float)
    n = Counter()
    for r in recs:
        b = r.get("bidder_raw")
        if not b or r.get("unit_price") is None:
            continue
        n[b] += 1
        e = r.get("extended_reported")
        if e is None:
            e = r["unit_price"] * (r["quantity"] if r.get("quantity") is not None else 1.0)
        allsec[b] += e
        if not ALT_ITEM_RE.match(r.get("item_no") or "") and not re.search(
                r"\balt", r.get("bid_section") or "", re.I):
            base[b] += e

    def seen(v):
        c = int(round(v * 100))
        return c > 0 and any(c + d in cents for d in (-1, 0, 1))
    return sum(1 for b in allsec
               if n[b] >= 2 and (seen(base.get(b, 0.0)) or seen(allsec[b])))


# ... and only a GRAND total line may decide it.  A section subtotal is a total
# of part of a column, so a parse that read only that part matches it:
# Arlington's Green Oaks pond dredging prints 'SUBTOTAL MOBILIZATION, BONDS &
# SWPPP : $205,000.00' for both bidders, the table parse read Dredgit's three
# mobilization rows and nothing else, its $205,000 'matched', and that tie-break
# threw away the coordinate parse that held both bidders' 17 rows.  The amounts
# rule 15 compares against come from lines that say TOTAL and are not a
# SUBTOTAL ('TOTAL BASE BID AMOUNT', 'Total Bid:', 'GRAND TOTAL').
_TOTAL_LINE_RE = re.compile(r"total", re.I)
_SUBTOTAL_LINE_RE = re.compile(r"sub[\s.\-]*totals?", re.I)
# A section's total is a total of part of a column too, however it is spelt:
# 'SECTION A - PAVING - TOTAL', 'TOTAL SECTION II', 'Total for Section C:',
# 'Total Group 5' (the 2026-11-19 corpus prints all four).  A line that NAMES
# one section, schedule, part, phase, division or group is that section's
# total -- unless it says it is the base bid, a grand total, a sum ('+') or a
# range of sections ('SECTION I-II', 'Sections 1 and 2').
_SECTION_TOTAL_RE = re.compile(
    r"^\W*(?:total\s+(?:for\s+)?)?(?:section|schedule|part|phase|division|group)\s+"
    r"(?:no\.?\s*)?[A-Z0-9]{1,4}\b(?!\s*(?:-|\u2013|through|thru|to|and|&)\s*[A-Z0-9]{1,4}\b)",
    re.I)
_COMBINED_TOTAL_RE = re.compile(r"base\s+bid|grand|\+|\ball\b|sections|schedules", re.I)


def section_total_line(s):
    """True for a line that prints ONE section's total (see above)."""
    return bool(_SECTION_TOTAL_RE.search(s or "")) and not _COMBINED_TOTAL_RE.search(s or "")


def total_line_amounts(strings):
    """printed_amounts of the lines that print a grand total, in cents."""
    return printed_amounts([s for s in (strings or [])
                            if s and _TOTAL_LINE_RE.search(s) and not _SUBTOTAL_LINE_RE.search(s)
                            and not section_total_line(s)])


# ---- what the document itself holds, for the completeness gate ---------------
# award_identity names a winner only for a letting that is shown complete
# against the DOCUMENT, not just against the other parsed bidders: a parse that
# lost the same items for every bidder (Kee Branch: 5 of 24 items, Cedar Hill
# PW18-0020: 30 of 80, Addison Surveyor: 2 items, OCR Capricorn: 2 of 17) looks
# complete to a comparison between bidders.  So the parser records, per file,
# from whatever text it read (text layer, workbook rows, or the OCR sidecar):
# the printed TOTAL amounts, and how many distinct item lines the text holds --
# a line that opens with an item number, has words, and two or more numbers
# after them; distinct by (item number, first word), so a schedule reprinted
# for each group of bidders counts once and sections that restart their
# numbering do not collapse.
_DOC_ITEM_RE = re.compile(
    r"^\s*#?\s*([A-Za-z]{0,3}[-.]?\d{1,4}(?:[.\-]\d{1,3}){0,2}[A-Za-z]?)[.):]?\s+(.*)$")
_DOC_NUM_RE = re.compile(r"(?<![\w.])\$?\s?\d[\d,]*(?:\.\d+)?(?![\w])")
_DOC_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def doc_item_key(item, words):
    """(normalised item number, first word) -- the identity both sides count."""
    t = (item or "").strip().upper().lstrip("#").strip()
    t = t.split()[0] if t.split() else ""
    t = t.rstrip(".):")
    if t.isdigit():
        t = str(int(t))
    w = _DOC_WORD_RE.search(words or "")
    return (t, w.group(0).upper()[:6] if w else "")


_CELL_NUMBER_RE = re.compile(r"(?<![\w.,])\d{4,}(?:\.\d{1,2})?(?![\w,]|\.\d)")


def document_facts(lines):
    """{'items': distinct item lines, 'totals': amounts printed OFF the item
    lines (cents)}.

    The amounts a winner's total may be found among are every amount the
    document prints except on an item line (an item's own extension), a
    SUBTOTAL line or one section's total line (a part of a column).  Not only
    TOTAL lines: a recommendation prints 'Klutz Construction, LLC 672,774.00'
    or 'in the amount not to exceed $672,774.00', and a summary row prints the
    bidders' totals under a label on the line above ('$ 1,059,420.00
    $ 1,631,700.00 ...', Grand Prairie's Briar Hill packet).  A workbook row
    ('a | b | c') prints its numbers bare ('2954810'), so there a bare number
    of four or more digits counts too."""
    keys, cents = set(), set()
    for s in lines or []:
        s = s or ""
        m = _DOC_ITEM_RE.match(s.replace("|", " "))
        if m:
            rest = m.group(2)
            if len(re.findall(r"[A-Za-z]", rest)) >= 3 and len(_DOC_NUM_RE.findall(rest)) >= 2:
                keys.add(doc_item_key(m.group(1), rest))
                continue
        if _SUBTOTAL_LINE_RE.search(s) or section_total_line(s):
            continue
        cents |= printed_amounts([s])
        if " | " in s:
            cents |= {int(round(float(x) * 100)) for x in _CELL_NUMBER_RE.findall(s)}
    # ... and, apart, the amounts on its TOTAL lines (total_line_amounts): a
    # document that prints its bidders' totals is checked against them
    tl = {c for c in total_line_amounts(lines) if c > 0}
    tl |= {c for c in total_label_amounts(lines) if c > 0}
    return {"items": len(keys), "totals": sorted({c for c in cents if c > 0} | tl),
            "total_lines": sorted(tl), "contract_hint": contract_hint(lines)}


# A TOTAL label printed on a line of its own, its amounts on the line above
# (or below): Dallas County's Riverfront Blvd prints '$99,196,865.85
# $103,984,035.18 $114,401,072.10' over a bare 'TOTAL BID:', so the bidders'
# own totals never reached total_lines and the completeness gate compared the
# winner with whatever amount happened to be near it (tail audit 2026-11-20).
# For the completeness gate only (document_facts); the parse-strategy choice
# (total_line_amounts) is left as it was.
_AMOUNTS_ONLY_RE = re.compile(r"^\s*(?:\(?-?\$\s*-?\d[\d,]*\.\d{2}\)?\s*){2,}$")


def total_label_amounts(strings):
    """printed_amounts of an amounts-only line next to a bare TOTAL label."""
    ls = [s or "" for s in (strings or [])]
    got = set()
    for i, s in enumerate(ls):
        if (len(s) < 60 and _TOTAL_LINE_RE.search(s) and not _SUBTOTAL_LINE_RE.search(s)
                and not section_total_line(s) and not printed_amounts([s])):
            for j in (i - 1, i + 1):
                if 0 <= j < len(ls) and _AMOUNTS_ONLY_RE.match(ls[j]):
                    got |= printed_amounts([ls[j]])
                    break
    return got


# ---- a tab whose quantities are not a project ------------------------------
# An on-call / annual / as-needed contract tabulates estimated or notional
# quantities (Frisco's 2025 On-Call Concrete Repair: tiered quantity bands),
# so its unit prices are real bids but its 'total' is not the price of a job.
# The title says so, at the top of the document.  award_identity reads the
# hint (with the file name) and keeps such a letting out of letting-level
# scoring; its unit prices stay in the pool.  Tail audit 2026-11-20.
# Measured on the 2026-11-20 corpus: 'as needed' is an item note ('sod
# replacement, as needed') and a delivery term ('DELIVERY: As needed'), and
# 'IDIQ' a firm's name ('Basic IDIQ, Inc.'), so neither is a title.  Only the
# title lines count: a council packet lists other agenda items ('5. Annual
# contract for the purchase of irrigation ...') further down its first page.
CONTRACT_HINT_RE = re.compile(
    r"\bon[\s-]?call\b|\bindefinite[\s-]+(?:delivery|quantit)|"
    r"\b(?:annual|term)\s+(?:contract|agreement|requirements?)\b|"
    r"\bunit[\s-]+price\s+(?:contract|agreement)\b|\bprice\s+agreement\b|"
    r"\brequirements?\s+contract\b|"
    # (round 9, .30) "Annual as Needed Street and Alley Maintenance Contract", "Annual Concrete
    # Contract", "Work Order Generated Concrete Repairs": the quantities are notional and the work
    # comes by work order, so the bid total is no project's price
    r"\bannual\s+as[\s-]?needed\b|\bas[\s-]?needed\s+(?:\w+\s+){0,4}?(?:contract|agreement|services?|basis)\b|"
    r"\bannual\s+(?:\w+\s+){1,4}?(?:contract|agreement)s?\b|"
    r"\bwork[\s-]+orders?\s+(?:generated|based|contracts?)\b", re.I)


def contract_hint(lines, head=8):
    """'on_call' when the document's title lines (its first `head` non-blank
    lines) call it so, else ''."""
    top = [s for s in (lines or []) if (s or "").strip()][:head]
    return "on_call" if CONTRACT_HINT_RE.search(" ".join(top)) else ""


def firm_names(recs):
    """Distinct bidder names of a parse that say they are firms (_FIRM_WORD_RE)."""
    return sum(1 for b in {r.get("bidder_raw") for r in recs if r.get("bidder_raw")}
               if _FIRM_WORD_RE.search(b))


# ---- the opening date a TABLE'S OWN PAGE prints (parser round 3) ----------
# find_dates dates a whole file, and for a council packet that bundles several
# lettings it can only flag the spread ('_ambiguous_span') -- the comment there
# names the real fix: date each table from the bid-opening line printed with
# it.  Grand Prairie's 2025-09-03 committee packet holds the Kirby Creek tab
# (opened Monday August 4, 2025) and, as an exhibit to another agenda item,
# Mansfield's Day Miar Road tab, whose seven pages each print 'Bid Opening
# Date: June 14, 2023'; every Day Miar row was dated 2025-08-04, two years and
# an inflation step late.  read_pdf keeps, per page, the latest opening-type
# date (BID_DATE_T1, not in the future) that page prints; read_pdf_best hands it
# to the page's rows; _parse_one_file uses it only in a file whose dates span
# more than the ambiguity window, labelled 'bid_open_table' (as precise as
# 'bid_open_or_due': it is that line, read on the table's own page).
_PAGE_DATES = {}


# BID_DATE_T1 without its 'tabulat': a tab's page FOOTER prints the date the
# sheet was printed beside the workbook's name ('6/19/2023 Page 7 of 14
# Day-Miar-Bid-Bid-Tabulation-20230619.xlsx'), five days after the opening.
_PAGE_OPENING_RE = re.compile(
    r"bids?\s*open|opened|bid\s*opening|bid\s*due|due\s*date|close\s*date|closing|"
    r"solicitation\s*due|bids?\s*received", re.I)


def page_opening_date(lines):
    """The latest bid-opening date printed on one page's lines, or ''."""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    ds = [d for ln in lines or () if ln and _PAGE_OPENING_RE.search(ln) for d in _dates_in(ln)
          if d[:10] <= today]
    return max(ds) if ds else ""


_SHEET_PAGE_RE = re.compile(r"^(?:coords_)?p(\d+)")


def read_pdf_best(path, stats):
    """_read_pdf_best, with each row's page opening date (page_opening_date)
    attached as page_bid_date ('' when the page prints none)."""
    recs, audit, strings = _read_pdf_best(path, stats)
    dates = _PAGE_DATES.pop(path, {}) or {}
    for r in recs:
        m = _SHEET_PAGE_RE.match(r.get("src_sheet") or "")
        r["page_bid_date"] = dates.get(int(m.group(1)), "") if m else ""
    return recs, audit, strings


def _read_pdf_best(path, stats):
    """Run both PDF strategies and keep the one the page itself vouches for.

    pdfplumber's table detection wins on some templates and collapses several
    data rows into one cell on others (Arlington), where column geometry wins.
    Rather than hard-coding which owner gets which parser, score both by
    arithmetic self-consistency and take the better one.
    """
    tbl, audit, strings = read_pdf(path, stats)
    try:
        crd = read_pdf_coords(path, stats)
    except Exception as e:
        stats.setdefault("coord_parser_errors", []).append(
            "%s: %s" % (os.path.basename(path), type(e).__name__))
        crd = []

    t_rate, t_n = _arith_ok_rate(tbl)
    c_rate, c_n = _arith_ok_rate(crd)
    t_unit, _tu = _unit_valid_rate(tbl)
    c_unit, _cu = _unit_valid_rate(crd)

    # A parse whose unit column is not units is structurally broken, whatever
    # its arithmetic says, so that gate is applied before the comparison.
    had_rows = bool(crd) or bool(tbl)
    gated = False
    if c_unit is not None and c_unit < 0.5:
        crd = []
        gated = True
    if t_unit is not None and t_unit < 0.5:
        tbl = []
        gated = True
    # The two cases are counted apart, because the old single counter asserted
    # a cause it had not established.  It incremented whenever BOTH strategies
    # ended empty, which includes every PDF that was never a bid tabulation and
    # produced nothing to begin with -- so its 515 read as "515 files rejected
    # for bad units" when most of them had no rows for the gate to reject.
    # A counter whose name names a cause has to be scoped to that cause.
    if not crd and not tbl:
        if had_rows and gated:
            stats["parses_rejected_bad_units"] = (
                stats.get("parses_rejected_bad_units", 0) + 1)
            stats.setdefault("rejected_bad_units_files", []).append(
                "%s (table_unit=%s coord_unit=%s)"
                % (os.path.basename(path),
                   None if t_unit is None else round(t_unit, 3),
                   None if c_unit is None else round(c_unit, 3)))
        else:
            stats["parses_empty_both_strategies"] = (
                stats.get("parses_empty_both_strategies", 0) + 1)

    # ---- coverage, judged before cleanliness -----------------------------
    # Arithmetic self-consistency only describes the rows that survived, so it
    # cannot see a parse that threw rows away.  Bidder coverage can.
    t_bid, c_bid = _bidders_of(tbl), _bidders_of(crd)
    t_pages, c_pages = _pages_of(tbl), _pages_of(crd)

    # When the two strategies covered DISJOINT pages, neither is "better" and
    # both score well on what they did read -- collin 58471 had coords on page 1
    # and tables on pages 2-3, each scoring 1.00, so any choice threw away half
    # the file.  Merging is the only correct action, and it cannot duplicate a
    # row because a row belongs to exactly one page.
    if tbl and crd and t_pages and c_pages and not (t_pages & c_pages):
        merged = list(tbl) + list(crd)
        stats["pdf_strategy_merged"] = stats.get("pdf_strategy_merged", 0) + 1
        stats.setdefault("pdf_strategy", {})[os.path.basename(path)] = {
            "chosen": "merged_disjoint_pages",
            "table_rows": len(tbl), "coord_rows": len(crd),
            "table_pages": sorted(t_pages), "coord_pages": sorted(c_pages),
            "table_arith_ok": (round(t_rate, 4) if t_rate is not None else None),
            "coord_arith_ok": (round(c_rate, 4) if c_rate is not None else None),
        }
        aud = list(audit) + [{
            "src_file": os.path.basename(path), "src_sheet": "coords",
            "header_row": 0, "n_price_cols": 0, "n_rows": len(crd),
            "bidders": sorted(c_bid)}]
        return merged, aud, strings

    # Prefer coordinates when they are materially cleaner, or when they recover
    # rows the table strategy missed entirely.
    pick_coords = False
    if crd:
        if not tbl:
            pick_coords = True
        elif t_rate is None and c_rate is not None:
            pick_coords = True                      # only coords has evidence
        elif c_rate is None and t_rate is not None:
            pick_coords = False
        elif t_rate is None and c_rate is None:
            pick_coords = len(crd) > len(tbl) * 1.1  # no evidence: take fuller
        elif c_rate >= t_rate + 0.02:
            pick_coords = True
        elif abs(c_rate - t_rate) < 0.02 and len(crd) > len(tbl) * 1.1:
            pick_coords = True

    # Coverage override: a parse that found materially fewer bidders has lost
    # whole price columns, and no arithmetic score redeems that.  The threshold
    # is deliberately coarse (strictly more than half again as many bidders) so
    # it fires only on real column loss, not on a name that failed to read.
    #
    # ZERO bidders is handled first and separately.  A parse with no bidder name
    # at all is not merely worse -- every one of its rows is discarded later by
    # the no_bidder_name filter, so choosing it means choosing nothing.  The
    # earlier version of this guard required `len(t_bid) and len(c_bid)`, i.e.
    # BOTH sides to have bidders, which excluded precisely the clearest case.
    # Observed on arlington/PWDR19010C (Kee Branch Trib 1): tables scored 25
    # rows / 0 bidders / arith 1.00, coords 10 rows / 2 bidders / arith 1.00,
    # the chooser took tables, and all 25 rows were then dropped -- a file that
    # looked parsed and contributed nothing.  It is the only confirmed
    # permanent-anchored-TRM observation in capital channel work.
    if tbl and crd:
        if len(c_bid) and not len(t_bid):
            pick_coords = True
        elif len(t_bid) and not len(c_bid):
            pick_coords = False
        elif len(t_bid) and len(c_bid):
            if len(c_bid) > len(t_bid) * 1.5:
                pick_coords = True
            elif len(t_bid) > len(c_bid) * 1.5:
                pick_coords = False
            elif (t_rate is None and c_rate is None) or (
                    t_rate is not None and c_rate is not None and abs(c_rate - t_rate) < 0.02):
                # Equally clean and close in coverage: take the parse with
                # more NAMED rows -- the rows that survive main()'s no-name
                # filter.  The 2026-11-19 repairs made the table strategy name
                # more of its columns, and on Arlington PWSM24005 it then tied
                # with the coordinate parse and won while still missing one
                # bidder's name ('Ken-Do Construction Waxahachie, Texas' reads
                # as a city line): 50 named rows became 40.
                # Only where the other parse leaves rows UNNAMED, though: when
                # every table row has its name, a few more coordinate rows are
                # not coverage.  Mesquite 2020-095 / 2021-001 / 23-6666 and
                # Collin 58112 name every row in both parses; the coordinate
                # parse had 2-9 more rows (total lines read as items) and
                # wrapped name words filed under the wrong column ('JB & Co,
                # LLC Ark', 'TREG Iron T Construction, Inc'), and taking it
                # cost every one of their printed-total matches (7/7 -> 0/7).
                n_t = sum(1 for r in tbl if looks_like_bidder(str(r.get("bidder_raw") or "")))
                n_c = sum(1 for r in crd if looks_like_bidder(str(r.get("bidder_raw") or "")))
                cents = total_line_amounts(strings)          # grand totals only
                h_t, h_c = printed_total_hits(tbl, cents), printed_total_hits(crd, cents)
                f_t, f_c = firm_names(tbl), firm_names(crd)
                # the document's own totals decide -- unless the parse they
                # favour names fewer firms: Grand Prairie's Trinity Blvd packet
                # coordinate parse hit four page subtotals under 'Item 11',
                # 'Item 15', 'LUMIN8' while the table parse had the bidders
                if h_c != h_t and (f_c >= f_t if h_c > h_t else f_t >= f_c):
                    pick_coords = h_c > h_t
                    stats["pdf_strategy_by_printed_totals"] = \
                        stats.get("pdf_strategy_by_printed_totals", 0) + 1
                elif n_c > n_t and n_t < len(tbl):
                    pick_coords = True
                elif n_t > n_c and n_c < len(crd):
                    pick_coords = False

    stats.setdefault("pdf_strategy", {})[os.path.basename(path)] = {
        "chosen": "coords" if pick_coords else "tables",
        "table_rows": len(tbl),
        "table_arith_ok": (round(t_rate, 4) if t_rate is not None else None),
        "table_checkable": t_n,
        "coord_rows": len(crd),
        "coord_arith_ok": (round(c_rate, 4) if c_rate is not None else None),
        "coord_checkable": c_n,
        "table_unit_valid": (round(t_unit, 3) if t_unit is not None else None),
        "coord_unit_valid": (round(c_unit, 3) if c_unit is not None else None),
        "table_bidders": len(t_bid),
        "coord_bidders": len(c_bid),
        "table_pages": sorted(t_pages),
        "coord_pages": sorted(c_pages),
    }
    if pick_coords:
        aud = [{"src_file": os.path.basename(path), "src_sheet": "coords",
                "header_row": 0, "n_price_cols": 0, "n_rows": len(crd),
                "bidders": sorted(set(r["bidder_raw"] for r in crd if r["bidder_raw"]))}]
        return crd, aud, strings
    return tbl, audit, strings


# ---- many data rows packed into ONE table row ------------------------------
# Where a tab draws no rules between its items, pdfplumber returns the whole
# item list as ONE row whose every cell holds one line per item: Coppell's
# 2021-04-19 tab (Bid Tab - Ranked) came back as a single row of 22
# newline-joined cells -- 86 items x 9 bidders -- and txt() glued each column
# into one cell, so the letting yielded nothing but its TOTALS line.  The row
# is split back into its lines only where the cells PROVE the alignment: four
# or more cells hold nothing but numbers/amounts on three or more lines, and
# EVERY non-empty cell of the row has exactly that many lines (a trailing
# blank or '$ -' line aside).  Text that merely wraps never meets this -- a
# wrapped description has more lines than the amounts beside it -- and such a
# row is left as it was.  Only rows UNDER a header row of the same table are
# split, so the split rows are read against columns the header names; a
# header-less continuation table is left to the paths that already handle it
# (on Collin 2025-01-13's uniform tab, splitting those made the header-less
# inference read an item's description as a bidder's name).
_PACK_MONEY_LINE_RE = re.compile(r"^\s*\(?-?\s*\$?\s*-?\s*[\d,]*\.?\d*\s*\)?\s*$")
_PACK_BLANK_LINE_RE = re.compile(r"^\s*\$?\s*-*\s*$")


# ---- the TEXT of several rows stacked in the first row's cell -------------
# The mirror image of a packed row: every number sits on its own row, but the
# words of several items landed in ONE cell of the first, and the cells below
# it are empty.  Bedford SWIFT 21-09 / 21-13 (Joiner Acres, Bell Manor) prints
# items 23-27 that way: the description cell of item 23 holds five lines --
# 'Transfer existing water meter ...', 'Tie into existing water line',
# 'Furnish and install 20" x 8" tapping sleeve ...', 'Cut and plug existing
# water main', 'Furnish and install Ductile Iron Fittings ...' -- while items
# 24-27 carry their quantities and prices and no words.  txt() then glued the
# five lines into one description priced at item 23's $100 per meter, and the
# four rows below kept prices with no description (also items 29-30, 37-38,
# 51-53; and items 1-2, where the ITEM cell holds '1 Mobilization' over '2
# Furnish and install 20-inch PVC water pipe ...' and item 2's row, with no
# number or words left, was dropped).  The lines are given back one entry per
# row, only where the count proves it: the cell's row holds numbers, the k
# rows right below it hold numbers AND a unit of measure (a total line has no
# unit: given the tail of a wrapped description it became an item priced at
# the column total -- Frisco 2610-001, Farmers Branch R2021-074 in the first
# replay), those k rows have nothing in that column, and the cell splits into
# exactly k + 1 entries.  A cell in capitals only is one entry.  An
# entry starts at a line that opens with an item number when two or more lines
# do; otherwise at a line that opens with a capital, unless the line before
# ends in a comma, a hyphen or a joining word (a description wrapped onto a
# second line stays one entry).  Anything else is left as printed.
_ITEM_LINE_RE = re.compile(r"^\s*\d{1,4}[A-Za-z]?[.)]?\s+[A-Za-z(\"'#]")
_ITEM_TEXT_RE = re.compile(r"^(\d{1,4}[A-Za-z]?)[.)]?\s+([A-Za-z(\"'#].*)$")
_WRAP_END_RE = re.compile(r"(?:[,\-/&(]|\b(?:and|or|of|with|for|to|the|in|on|at|by|per|a|an))\s*$",
                          re.I)


def _cell_entries(s):
    lines = [ln.strip() for ln in str(s).split("\n") if ln.strip()]
    if len(lines) < 2:
        return [" ".join(lines)] if lines else []
    groups = []
    numbered = sum(1 for ln in lines if _ITEM_LINE_RE.match(ln)) >= 2
    if not numbered and not re.search(r"[a-z]", " ".join(lines)):
        return [" ".join(lines)]          # all capitals: a capital opens nothing
    for ln in lines:
        if not groups:
            groups.append([ln])
        elif numbered:
            if _ITEM_LINE_RE.match(ln):
                groups.append([ln])
            else:
                groups[-1].append(ln)
        elif (ln[0].isupper() or ln[0].isdigit()) and not _WRAP_END_RE.search(groups[-1][-1]):
            groups.append([ln])
        else:
            groups[-1].append(ln)
    return [" ".join(g) for g in groups]


# ... and the NUMBERS of several rows stacked the same way: Arlington PWDR22005
# prints the quantities of items 301-317 as seventeen lines of item 301's QTY
# cell, the sixteen cells below it empty.  Glued into one number (1.83e41) the
# quantity failed item 301's arithmetic and the row left the panel ($27,720 of
# the low bid).  A cell of plain numbers only, one per line and no '$', is
# split by the same count proof, one line per row -- from three lines up.  Two
# lines are weaker evidence (one number broken across lines reads the same),
# and in the 2026-11-19 replay a two-line stack (Richardson 01-22 Jupiter
# Road, '7 / 3') sat in a table parse that loses a bidder's whole column:
# cleaned up, that parse tied the coordinate one and won the strategy choice.
_STACKED_NUMBER_LINE_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _stacked_numbers(s):
    lines = [ln.strip() for ln in str(s).split("\n") if ln.strip()]
    if len(lines) >= 3 and all(_STACKED_NUMBER_LINE_RE.match(ln) for ln in lines):
        return lines
    return []


# (round 11, .32) a UNIT column stacked into one cell.  Coppell's dog-park tab (2025-7987) hands
# pdfplumber its whole unit column as ONE cell on the first item row -- 'LS\nLS\nLS\nLS\nLF ...', 46
# units for 46 items -- beside 45 item rows with an empty unit cell, so the first item read the 46 units
# as its unit and the others none.  The rows below hold no unit (they are all in the stack), so the
# item_row test the stacked numbers rely on cannot pass; a stack of nothing but units (UNIT_MAP, three
# or more) is spread over the data rows below it with an empty cell in its column -- counted up to the
# first total line -- under the same count proof: exactly one unit per row.
def _stacked_units(s):
    lines = [ln.strip() for ln in str(s).split("\n") if ln.strip()]
    if len(lines) >= 3 and all(norm_unit(ln) in KNOWN_UNITS and not NUMERIC_UNIT_RE.match(ln) for ln in lines):
        return lines
    return []


def spread_stacked_cells(table, stats=None):
    """Rows of a raw pdfplumber table with stacked text cells handed back one
    entry per row (see above)."""
    rows = [list(r) for r in (table or [])]

    def data_row(r):
        return sum(1 for c in r if c and num(txt(c)) is not None) >= 2

    def item_row(r):
        # a priced ITEM: numbers and a unit of measure.  A total line has
        # money and no unit -- given the last line of a wrapped description
        # it would turn into an item priced at the column total.
        return data_row(r) and any(c and norm_unit(txt(c)) in KNOWN_UNITS
                                   and not NUMERIC_UNIT_RE.match(txt(c)) for c in r)

    width = max((len(r) for r in rows), default=0)
    moved = 0
    for c in range(width):
        i = 0
        while i < len(rows):
            s = rows[i][c] if c < len(rows[i]) else None
            s = str(s) if s else ""
            flat = re.sub(r"\s", "", s)
            stacked = _stacked_numbers(s) if "\n" in s else []
            units = _stacked_units(s) if "\n" in s and not stacked else []
            if "\n" in s and flat and (stacked or sum(ch.isalpha() for ch in flat) > 0.5 * len(flat)) \
                    and data_row(rows[i]):
                j = i + 1
                while (j < len(rows) and not (rows[j][c] if c < len(rows[j]) else None)
                       and (item_row(rows[j]) or
                            (units and data_row(rows[j])
                             and not any(x and TOTAL_WORD_RE.search(txt(x)) for x in rows[j])))):
                    j += 1
                k = j - i - 1
                ents = (stacked or units or _cell_entries(s)) if k else []
                if k and len(ents) == k + 1:
                    for n, e in enumerate(ents):
                        row = rows[i + n]
                        while len(row) <= c:
                            row.append(None)
                        row[c] = e
                    moved += k
                    i = j
                    continue
            i += 1
    if stats is not None and moved:
        stats["stacked_cells_spread"] = stats.get("stacked_cells_spread", 0) + moved
    return rows


def split_packed_rows(table, stats=None):
    out, header_seen = [], False
    for row in table:
        if not header_seen:
            header_seen = bool(detect_price_pairs([txt(c) for c in row]))
            out.append(row)
            continue
        cells = ["" if c is None else str(c) for c in row]
        lines = []
        for c in cells:
            ls = c.split("\n") if c else []
            while len(ls) > 1 and _PACK_BLANK_LINE_RE.match(ls[-1]):
                ls.pop()
            lines.append(ls)
        money = [i for i, c in enumerate(cells)
                 if len(lines[i]) >= 3 and any(ch.isdigit() for ch in c)
                 and all(_PACK_MONEY_LINE_RE.match(x) for x in lines[i])]
        n = len(lines[money[0]]) if money else 0
        if len(money) < 4 or any(c and len(lines[i]) != n for i, c in enumerate(cells)):
            out.append(row)
            continue
        for k in range(n):
            out.append([lines[i][k] if cells[i] else row[i] for i in range(len(row))])
        if stats is not None:
            stats["packed_rows_split"] = stats.get("packed_rows_split", 0) + 1
    return out


# ---- bidder names printed ABOVE the ruled table ----------------------------
# pdfplumber's table is the ruled area; a template can print the firm names
# (and the captions) above the rules, outside every cell.  Mansfield 21-4436
# (US 287 utility relocation: dry riprap, hydromulch, sewer) prints ENGINEER'S
# ESTIMATE, Blackrock Construction, FM Utilities, ... over its seven column
# pairs above a table that holds only item rows: 546 priced cells, every one
# dropped as no_bidder_name.  The page's words know where they sit, so the
# names are read from the nearest text line above the table whose fragments,
# cut to each pair's x-range (the pair's two columns as the table ruled them),
# look like firms -- caption lines ('UNIT', 'PRICE AMOUNT') are passed over.
# It must be a ROW of names: two or more pairs named from the same line (a
# one-column table is never named this way -- any line above it would do),
# not one word repeated over every pair, no amount in a name, and no word may
# run across a pair boundary -- a title centred over the money block is text
# that crosses columns, and is refused.  Only within 72 pt of the table, and
# only for pairs the table itself left unnamed.
def names_above_table(page, table, pairs, band=72.0):
    """[name or '' per pair] from the text line just above a pdfplumber Table,
    or None when no such line names the pairs."""
    cols = getattr(table, "columns", None)
    if not cols or not pairs:
        return None
    rng, tops = [], []
    for pc, tc in pairs:
        if max(pc, tc) >= len(cols):
            return None
        a = [b for b in cols[pc].cells if b]
        z = [b for b in cols[tc].cells if b]
        if not a or not z:
            return None
        rng.append((min(b[0] for b in a), max(b[2] for b in z)))
        tops.append(min(b[1] for b in a + z))
    bounds = sorted({x for r in rng for x in r})
    # Above the pairs' OWN first cells, not the table's top edge: a table whose
    # right-hand columns are ruled higher than the rest (a title cell over the
    # last firms) has its top there, and the names over the lower columns sit
    # inside the table's box but in no cell.  Grand Prairie's 2025-09-03 packet
    # (Day Miar Road, pages 77-83) rules 'Texas Sterling' and 'Ed Bell
    # Construction' into cells and prints 'McMahon Contracting' (the low
    # bidder, $13,293,899.33) and 'Reliable Paving, Inc.' over cells that
    # start 90 pt lower; both columns were dropped as nameless.  Where every
    # pair starts at the table's top this is the table's top, as before.
    top = max(tops) if tops else table.bbox[1]
    words = [w for w in page.extract_words()
             if w["bottom"] <= top + 1 and w["top"] >= top - band]
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        for ln in lines:
            if abs(ln[0] - w["top"]) <= 2.0:
                ln[1].append(w)
                break
        else:
            lines.append([w["top"], [w]])
    for _t, ws in sorted(lines, key=lambda l: -l[0]):        # nearest the table first
        ws.sort(key=lambda w: w["x0"])
        if _caption_only(" ".join(w["text"] for w in ws)):
            continue                                    # 'UNIT' / 'PRICE AMOUNT'
        crosses = any(w["x0"] + 1 < b < w["x1"] - 1 for w in ws for b in bounds) or any(
            w1["x1"] <= b <= w2["x0"] and w2["x0"] - w1["x1"] < 3.5
            for w1, w2 in zip(ws, ws[1:]) for b in bounds)
        if crosses:
            continue
        texts = [" ".join(w["text"] for w in ws if x0 <= (w["x0"] + w["x1"]) / 2 < x1)
                 for x0, x1 in rng]

        firm = _name_candidate
        named = [t for t in texts if firm(t)]
        if len(named) < 2 or len({txt(t).lower() for t in named}) == 1:
            continue
        return [_block_label_strip(strip_address(t) or t) if firm(t) else "" for t in texts]
    return None


# ---- an item row printed UNDER the ruled table, cut at the page foot ------
# pdfplumber's table is the area the rules close; a row whose bottom rule is
# not drawn -- the last row of a page, cut by the page break -- is in no cell.
# Cedar Hill Complete Streets 2A ends page 1 with 'B-1 MOBILIZATION (5% OF
# CONTRACT PRICE) 1 LS' and every column's price under the ruled area (only
# the shaded estimate cell closes, as a one-row table of its own), so B-1 was
# lost for every bidder: Axis Contracting's base read $3,493,710.17 against a
# printed $3,613,710.17.  The page's words know where they sit: the text lines
# directly under the table (each within two row heights of the one above), cut
# to the table's columns, are read as rows when each is an ITEM -- two or more
# numbers, a unit of measure, words -- and no word crosses a column rule or
# lies in another table of the page (a fragment of a few columns, like the
# estimate cell, excepted).  A total line (no unit), a footer or a note ends
# the search.
def rows_below_table(page, tables, ti, max_rows=3):
    """Grid rows (one text per column) for the item lines printed right under
    pdfplumber Table tables[ti] outside its rules, or []."""
    t = tables[ti]
    # (a table stitch_side_tables put together has no rows of its own)
    if getattr(t, "rows", None) is None or len(t.rows) < 3:
        return []
    best = max(t.rows, key=lambda r: sum(c is not None for c in r.cells))
    if any(c is None for c in best.cells):
        return []
    bounds = [(c[0], c[2]) for c in best.cells]
    hs = sorted(r.bbox[3] - r.bbox[1] for r in t.rows)
    rh = hs[len(hs) // 2]
    walls = [o.bbox for j, o in enumerate(tables)
             if j != ti and (getattr(o, "columns", None) is None
                             or 2 * len(o.columns) >= len(bounds))]
    floor = t.bbox[3]
    if floor + 1 >= page.bbox[3]:
        return []
    band = page.within_bbox((page.bbox[0], max(page.bbox[1], floor - 1), page.bbox[2],
                             min(page.bbox[3], floor + (3 * max_rows + 1) * rh)))
    lines = []
    for w in sorted((w for w in band.extract_words() if w["top"] >= floor - 1),
                    key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0] - w["top"]) <= 2.0:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    out = []
    for top, ws in lines:
        if top - floor > 2 * rh or len(out) >= max_rows:
            break
        cells = [""] * len(bounds)
        for w in sorted(ws, key=lambda w: w["x0"]):
            cx = (w["x0"] + w["x1"]) / 2
            k = next((i for i, (a, b) in enumerate(bounds) if a <= cx < b), None)
            if (k is None or w["x0"] < bounds[k][0] - 2 or w["x1"] > bounds[k][1] + 2
                    or any(o[0] <= cx <= o[2] and o[1] <= w["top"] < o[3] for o in walls)):
                return out
            cells[k] = (cells[k] + " " + w["text"]).strip()
        nums = sum(1 for c in cells if c and num(c) is not None)
        unit = any(c and norm_unit(c) in KNOWN_UNITS and not NUMERIC_UNIT_RE.match(c)
                   for c in cells)
        words = any(c and num(c) is None and norm_unit(c) not in KNOWN_UNITS
                    and re.search(r"[A-Za-z]{3}", c) for c in cells)
        if nums < 2 or not unit or not words:
            break
        out.append(cells)
        floor = max(w["bottom"] for w in ws)
    return out


# ---- text the page DRAWS but does not SHOW ---------------------------------
# pdfplumber reads every glyph in the content stream; a viewer shows only those
# inside the clip path and not painted over later.  Two layouts put hidden
# text on top of what is printed, letter by letter:
#   * painted over -- Cedar Hill PW23-0009 (N Joe Wilson Rd signalization)
#     draws its page-2 rows twice, the first copy shifted six rows down and
#     then covered by a white rectangle.  Interleaved with the visible copy,
#     every cell of the page read 'ICLOSNN F(ILREMD)A' / '$$ 6 , 280205..0000',
#     and the page's 32 items ($159,051.84 of Texas Standard's printed
#     $522,726.54) were lost.  Balch Springs Elam Rd's 'two tabs overprinted'
#     (failed_arithmetic_files) is the same: 4,189 covered characters.
#   * clipped away -- Excel clips the text of a cell at its border: Cedar Hill
#     Complete Streets 2A repeats 'EDC PARKING LOT' under page 2's first row
#     (B-2 read 'EDC BP NTGRA FLFOIC TCONTROL'), and McKinney 21-0275's
#     descriptions run on under the unit column ('sidewalk or R.O.W.' woven
#     into 'SY' gave unit 'eSwYalk or R.O': 27 rows unit_suspect).
# MuPDF knows both: its TEXT_CLIP extraction drops the clipped glyphs, and its
# text trace and drawing list share one paint order, so a glyph is covered when
# an OPAQUE filled rectangle painted after it contains its box (invisible OCR
# text -- render mode 3 -- is never taken out).  Those glyphs are matched to
# pdfplumber's characters (same letter, centre within a point) and the page is
# read without them.  If the two readers do not agree where the glyphs are
# (fewer than 80% matched), the page is read as before.
def hidden_text_chars(fz_page):
    """[(char, x-centre, y-centre)] in pdfplumber's page coordinates of the
    glyphs a PyMuPDF page draws but does not show."""
    import fitz

    def spans(flags):
        for b in fz_page.get_text("rawdict", flags=flags)["blocks"]:
            for ln in b.get("lines", ()):
                for sp in ln["spans"]:
                    yield [(ch["c"], tuple(round(v, 1) for v in ch["bbox"])) for ch in sp["chars"]]
    shown = Counter(k for sp in spans(fitz.TEXT_CLIP) for k in sp)
    hid = Counter()
    for sp in spans(0):
        vis = []
        for k in sp:
            vis.append(shown[k] > 0)
            if shown[k] > 0:
                shown[k] -= 1
        letters = [i for i, k in enumerate(sp) if k[0].strip()]
        word = [0] * len(sp)            # the word (run of non-spaces) of each glyph
        w = 0
        for i, k in enumerate(sp):
            if not k[0].strip():
                w += 1
            word[i] = w
        numeric_shown = {word[i] for i, k in enumerate(sp) if vis[i] and k[0].strip()} - {
            word[i] for i, k in enumerate(sp) if k[0].isalpha()}
        for i, k in enumerate(sp):
            if vis[i]:
                continue
            # a NUMBER the page shows in part keeps its clipped glyphs: dropped,
            # they would make another number of what the page prints (Glenn
            # Heights FY24's overprinted '$$05,,000000..0000' turned into
            # $50,000,000,000).  A number clipped whole goes, as words do ('0-50
            # square yards' run on under McKinney 21-0275's unit column).
            if word[i] in numeric_shown:
                continue
            # a clipped SPACE between two shown letters of its span still
            # separates them ('Traffic Control[ ]Implementation', a wrap
            # point in Mesquite 24-7401): dropped, the words would merge
            if not k[0].strip():
                left = [j for j in letters if j < i]
                right = [j for j in letters if j > i]
                if left and right and vis[left[-1]] and vis[right[0]]:
                    continue
            hid[k] += 1
    hid = {k: n for k, n in hid.items() if n > 0}
    fills = []
    for g in fz_page.get_drawings():
        if g.get("fill") is None or g.get("type") not in ("f", "fs"):
            continue
        if g.get("fill_opacity") is not None and g["fill_opacity"] < 1.0:
            continue
        for it in g["items"]:
            if it[0] == "re" and it[1].width >= 3 and it[1].height >= 3:
                fills.append((g["seqno"], fitz.Rect(it[1])))
    if fills:
        last = max(s for s, _ in fills)
        for t in fz_page.get_texttrace():
            if t.get("type") == 3 or t["seqno"] >= last:
                continue
            later = [r for s, r in fills if s > t["seqno"]]
            for ch in t["chars"]:
                c, bb = chr(ch[0]), fitz.Rect(ch[3])
                if (not bb.is_empty and any(
                        r.x0 - .5 <= bb.x0 and bb.x1 <= r.x1 + .5
                        and r.y0 - .5 <= bb.y0 and bb.y1 <= r.y1 + .5 for r in later)):
                    k = (c, tuple(round(v, 1) for v in ch[3]))
                    hid[k] = max(hid.get(k, 0), 1)
    rot = fz_page.rotation_matrix
    out = []
    for (c, bb), n in hid.items():
        r = fitz.Rect(bb) * rot
        out.extend([(c, (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)] * n)
    return out


def visible_text_page(page, fz_page, stats=None):
    """The pdfplumber page without the characters it does not show (see
    hidden_text_chars), or the page itself."""
    if fz_page is None:
        return page
    try:
        hid = hidden_text_chars(fz_page)
    except Exception:                                         # noqa: BLE001
        return page
    if not hid:
        return page
    cells = defaultdict(list)
    for c, x, y in hid:
        cells[(c, int(x // 2), int(y // 2))].append([x, y, False])
    drop, dropped = set(), []
    for ch in page.chars:
        x, y = (ch["x0"] + ch["x1"]) / 2, (ch["top"] + ch["bottom"]) / 2
        gx, gy = int(x // 2), int(y // 2)
        hit = next((e for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                    for e in cells.get((ch["text"], gx + dx, gy + dy), ())
                    if not e[2] and abs(e[0] - x) <= 1.0 and abs(e[1] - y) <= 1.0), None)
        if hit is not None:
            hit[2] = True
            drop.add(id(ch))
            dropped.append(ch["text"])
    # spaces need not be glyphs in both readers: the agreement is counted on letters
    if sum(1 for e in dropped if e.strip()) < 0.8 * sum(1 for c, _x, _y in hid if c.strip()):
        if stats is not None:
            stats["hidden_text_unmatched_pages"] = stats.get("hidden_text_unmatched_pages", 0) + 1
        return page
    if stats is not None:
        stats["hidden_text_chars_dropped"] = stats.get("hidden_text_chars_dropped", 0) + len(drop)
    return page.filter(lambda o: id(o) not in drop)


def _open_fitz(path):
    """The PDF opened with PyMuPDF for visible_text_page, or None."""
    try:
        import fitz
        return fitz.open(path)
    except Exception:                                         # noqa: BLE001
        return None


# ... and the page TITLE above such a table can say which bid it is.  Mansfield
# 21-4436 prints two full tabulations of the same 39 items: page 1 'BASE BID
# TABULATION', page 2 'ALTERNATIVE BID TABULATION' (another pipe scheme, priced
# in full).  Read as BASE BID, the alternative doubled every bidder's base
# total (Blackrock 4,681,171 against a printed 2,262,003).  Where the names
# came from above the ruled area, the title over it is read too: a line that
# names an alternate/alternative bid, tabulation or schedule and does not say
# 'base' opens the table in that section.
_ALT_TITLE_RE = re.compile(
    r"\balternat(?:e|es|ive|ives)\b.*\b(?:bids?|tabulation|schedule|proposal)\b|"
    r"\b(?:bid|schedule)\s+alternat(?:e|es|ive|ives)\b", re.I)


def _misfit_row(r):
    """A row read under a borrowed header whose cells sit in the wrong columns:
    an amount or a count where the description goes (unless it is just the
    item number again -- Collin 54857 prints 'Line # Description' in ONE cell,
    so item and description share a column on every page, its own included),
    a unit code there ('HR', 'LS': Grand Prairie's ditch-maintenance packet
    lent a bid header to a contractor's labour/equipment cost breakdown), or
    a dollar amount where the item number goes ('$374,852.00' -- the same
    packet's project-budget box)."""
    d, it = txt(r.get("description")), txt(r.get("item_no"))
    if num(d) is not None and d != it:
        return True
    # the whole cell a unit ('HR', 'LS', '1 day'), not norm_unit's reading of
    # a trailing '(LF)' -- 'Furnish 8-inch PVC pipe (LF)' is a description
    s = d.lower().rstrip(".")
    s2 = re.sub(r"[^a-z/ ]", "", s).strip()
    if (s and s in UNIT_MAP) or (s2 and s2 in UNIT_MAP):
        return True
    return bool(re.match(r"^\(?\$", it))


def inherited_parse_fits(recs):
    """False when a table read under a BORROWED header shows the columns do not
    line up (see read_pdf): most rows are misfits (_misfit_row: '$30,600.00'
    as a description), or most checkable rows fail quantity x price ==
    extension (three or more)."""
    if recs and sum(_misfit_row(r) for r in recs) * 2 > len(recs):
        return False
    rate, n_chk = _arith_ok_rate(recs)
    return rate is None or n_chk < 3 or rate >= 0.5


def header_prefix(grid, a, inherited):
    """The header rows (names row + caption row) a later headerless table may
    borrow from this one, or None to keep lending the previous ones.

    An INFERRED header lends nothing: its header_row indexes the grid with the
    synthetic row inserted, so the slice of the real grid is two DATA rows.
    Dallas County 2024-038-7025 (Riverfront Blvd) lent its page-3 item rows
    that way -- the real page-1 header was overwritten and page 8 (57 priced
    rows) had nothing left to inherit -- and Mansfield 21-4017 lent its first
    item row, which the next page then read again, once per bidder."""
    if not a or inherited or a.get("inferred"):
        return None
    h = a["header_row"] - 1
    return [row[:] for row in grid[max(0, h - 1):h + 1]]


def alternate_title_above(page, table, band=90.0):
    """'ALTERNATIVE BID TABULATION' from the text just above a table, or ''."""
    top = table.bbox[1]
    lines = {}
    for w in page.extract_words():
        if w["bottom"] <= top + 1 and w["top"] >= top - band:
            lines.setdefault(round(w["top"]), []).append(w)
    for _t, ws in sorted(lines.items(), key=lambda kv: -kv[0]):
        s = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))
        # a line with an amount is the previous table's TOTAL line, not this
        # one's title: NRH 4180 prints 'TOTAL BID ALTERNATE #1 - SIDEWALK AND
        # CURB RAMPS $24,000.00' right above its Alternate #2 table
        if re.search(r"\$|\d[\d,]*\.\d\d\b", s):
            continue
        if _ALT_TITLE_RE.search(s) and not re.search(r"\bbase\b", s, re.I):
            return txt(s).upper()[:60]
    return ""


def may_borrow_header(r, a, prefix_cols, prefix_n, page, prefix_page, width):
    """Whether a table is read again under the previous table's header (see
    read_pdf): always when it found no header of its own; when inference read
    it but named no column, only if that header comes from a table just as
    wide (the same template continued -- Grand Prairie's Dry Branch packet
    follows its 10-column bid tab with 8-column materials-testing pay
    estimates, whose rows must not take the bid tab's names) and has two or
    more pairs or was vouched for within a page (a one-bidder header is lent
    no further, as lend_names)."""
    if not prefix_cols:
        return False
    if a is None:
        return True
    nameless = bool(r and a.get("inferred") and not any(a["bidders"]))
    return (nameless and prefix_cols == width
            and (prefix_n >= 2 or page - prefix_page <= 1))


def borrows_inferred_header(r, a, inherited, grid, inf_prefix, inf_page, page):
    """Whether a table that read nothing is read again under the INFERRED
    header of a table above it (see read_pdf): it found no header of its own,
    has fewer than four rows below its first (too few to infer from), is as
    wide as that header, sits on its page or the next, and its first row
    REPEATS the row printed over that header -- two or more of its cells, in
    the same columns (Fish Creek: 'QUANTITY | UNIT | UNIT PRICE (NUMBER
    ONLY) | ...'), so it is the same template continued.  A short table
    without that proof is left alone: Colleyville's generator tab follows its
    base items with a two-row 'Deduct Alternate Bid #1' table whose title is
    printed outside the rules, and read under the base header the deduct went
    into the base bid."""
    if not (not r and (a is None or inherited) and inf_prefix is not None
            and page - inf_page <= 1
            and max((len(row) for row in grid), default=0) == len(inf_prefix[-1])
            and sum(1 for row in grid[1:] if any(row)) < 4):
        return False
    first = next((row for row in grid if any(row)), [])
    return sum(1 for x, y in zip(first, inf_prefix[0])
               if x and y and txt(x).lower() == txt(y).lower()) >= 2


def lend_names(last_names, page, names_page):
    """The bidder names a table on `page` may inherit (see read_pdf): only
    within a page of the table that vouched for them, however many there are
    (rows_from_grid still requires the list's length to match the table's
    pairs).

    A list of two or more used to be lent at any distance, on the argument that
    an equal column count is evidence of its own.  It is not, in a council
    packet: Grand Prairie's Kirby Creek packet lent the three bidders of its
    page-84 tab (whose own rows never parsed) across 14 pages to a contractor's
    labour/equipment/material cost sheet on page 98, and its Pollock Place /
    Trinity Blvd packet lent page 66's three bidders to pages 80-81, another
    letting with three columns of its own (75 rows under the wrong firms).  A
    multi-page tab keeps its names, because every page that yields rows
    vouches for them again (names_pg in read_pdf)."""
    if last_names and page - names_page <= 1:
        return last_names
    return None


# ---- one tab drawn as SEPARATE boxes: the items, then one box per bidder ---
# Halff's tabs (Seagoville PW2501, East Malloy Bridge Road sewer relocation)
# draw the item list (ITEM NO. | DESCRIPTION | UNIT | PLAN QTY. | BID QTY.) as
# one ruled box and every bidder as a box of its own to the right of it (the
# firm over 'UNIT PRICE | BID ESTIMATE'), with gaps between the boxes.
# pdfplumber returns one table per box: the item table has no price caption
# and the bidder tables have no items, so neither was read and the letting --
# eight bidders, 18 items -- produced no row at all.  The boxes are one table
# split by its gutters, and the rows prove it: where a page has exactly one
# box with an item header and no price caption, and to its right one or more
# boxes that carry a price-pair caption row and no item header, whose spans
# overlap the item box's, and whose every row lines up with a row of the
# item box or of each other (half the shorter row's height in common), the
# boxes are put back side by side, row by row, into one table.  A row only one
# box has (the names above the item header) is kept with empty cells for the
# others.  Anything less regular -- two rows of one box on one line, a bidder
# box overlapping another -- is left as pdfplumber read it.
class _StitchedTable(object):
    """What read_pdf asks of a pdfplumber Table for a stitched one: its bbox.
    (No columns: names_above_table reads ruled columns, which a stitched
    table does not have.)"""
    columns = None

    def __init__(self, bbox):
        self.bbox = bbox


def _box_role(rows):
    """'items' for a box headed ITEM/DESCRIPTION/QTY with no price caption,
    'money' for one with a price-pair caption row and no description caption,
    '' for anything else."""
    head = [[txt(c) for c in row] for row in rows[:6]]
    if any(detect_price_pairs(row) for row in head):
        if any(DESC_HDR_STRONG.search(c) for row in head for c in row if c):
            return ""
        return "money" if max((len(r) for r in rows), default=0) <= 4 else ""
    for row in head:
        m = detect_meta_cols(row, 0)
        if m["desc"] is not None and (m["qty"] is not None or m["unit"] is not None):
            return "items"
    return ""


def stitch_side_tables(page, tables, stats=None):
    """(tables, table objects) with the boxes of one split tab joined, or
    None when the page holds no such tab (see above)."""
    if len(tables) < 2:
        return None
    roles = [_box_role(t or []) for t in tables]
    items = [i for i, r in enumerate(roles) if r == "items"]
    money = [i for i, r in enumerate(roles) if r == "money"]
    if len(items) != 1 or not money:
        return None
    objs = page.find_tables()
    if len(objs) != len(tables):
        return None
    it = objs[items[0]]
    ix0, itop, ix1, ibot = it.bbox
    side = []
    for i in money:
        x0, top, x1, bot = objs[i].bbox
        if min(bot, ibot) - max(top, itop) < 0.5 * (ibot - itop):
            continue                            # another table on the page
        if x0 < ix1 - 2:
            return None                         # a bidder box over the items: no split tab
        side.append(i)
    side.sort(key=lambda i: objs[i].bbox[0])
    if not side or any(objs[a].bbox[2] > objs[b].bbox[0] + 2 for a, b in zip(side, side[1:])):
        return None
    parts = [items[0]] + side
    bands = []                                  # [top, bottom, {part: row index}]
    for p in parts:
        rows = objs[p].rows
        if len(rows) != len(tables[p]):
            return None
        for k, row in enumerate(rows):
            top, bot = row.bbox[1], row.bbox[3]
            best, best_ov = None, 0.0
            for b in bands:
                ov = min(bot, b[1]) - max(top, b[0])
                if ov > best_ov:
                    best, best_ov = b, ov
            if best is not None and best_ov >= 0.5 * min(bot - top, best[1] - best[0]):
                if p in best[2]:
                    return None                 # two rows of one box on one line
                best[2][p] = k
            else:
                bands.append([top, bot, {p: k}])
    bands.sort(key=lambda b: b[0])
    width = {p: max((len(r) for r in tables[p]), default=0) for p in parts}
    grid = []
    for _top, _bot, got in bands:
        row = []
        for p in parts:
            cells = list(tables[p][got[p]]) if p in got else []
            row.extend((cells + [None] * width[p])[:width[p]])
        grid.append(row)
    box = (min(objs[p].bbox[0] for p in parts), min(objs[p].bbox[1] for p in parts),
           max(objs[p].bbox[2] for p in parts), max(objs[p].bbox[3] for p in parts))
    out_t, out_o = [], []
    for i, t in enumerate(tables):
        if i == items[0]:
            out_t.append(grid)
            out_o.append(_StitchedTable(box))
        elif i not in side:
            out_t.append(t)
            out_o.append(objs[i])
    if stats is not None:
        stats["side_tables_stitched"] = stats.get("side_tables_stitched", 0) + len(side)
    return out_t, out_o


def read_pdf(path, stats):
    """PDFs split one bid tab into several tables; the continuation tables
    (the additive-alternate blocks) carry no header of their own. Those inherit
    the previous table's bidder-name + header rows, otherwise their items -- and
    therefore the alternates -- would be silently lost."""
    recs, audit, strings = [], [], []
    base = os.path.basename(path)
    _RANK_SERIES.pop(base, None)          # (round 11) a file's rank series starts afresh
    prefix, last_names = None, None
    # The page that last vouched for last_names: a table that read its own
    # header, or one that yielded rows.  A SINGLE name is lent only to the same
    # page or the next one.  One column is no evidence that a table continues
    # another: Grand Prairie's Briar Hill packet carried 'Macia Specialty
    # Contracting' from its page-132 bid tab through 30 empty borrowed headers
    # to a page-170 materials-testing pay estimate and filed its 51 lab-test
    # prices under that bidder.  A list of two or more names must still match
    # the table's pair count (rows_from_grid), which is evidence of its own.
    names_pg = -9
    prefix_n, prefix_pg = 0, -9       # pairs of the lendable header, page that vouched for it
    fz = _open_fitz(path)             # to read only the text the page shows
    inf_prefix, inf_pg = None, -9     # the last INFERRED header and its page (see below)
    page_dates = {}                   # page number -> the opening date that page prints
    _PAGE_DATES[path] = page_dates
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            page = visible_text_page(page, fz[pi] if fz is not None and pi < len(fz) else None,
                                     stats)
            page_lines = (page.extract_text() or "").split("\n")
            strings.extend(page_lines)
            pdt = page_opening_date(page_lines)
            if pdt:
                page_dates[pi + 1] = pdt
            # page.find_tables(): what extract_tables() extracts, kept so a
            # table's rules can be read too (rows_below_table, names_above_table)
            ruled = page.find_tables()
            tset = pdfplumber.table.TableSettings.resolve(None).text_settings or {}
            tables = [t.extract(**tset) for t in ruled]
            stitched = stitch_side_tables(page, tables, stats)
            if stitched is not None:
                tables, ruled = stitched
            for ti, table in enumerate(tables):
                grid = [[txt(c) for c in row] for row in
                        spread_stacked_cells(split_packed_rows(table, stats), stats)]
                below = rows_below_table(page, ruled, ti)
                if below:
                    grid += below
                    stats["rows_below_table"] = stats.get("rows_below_table", 0) + len(below)
                sheet = "p%dt%d" % (pi + 1, ti)
                snap = {k: v for k, v in stats.items() if isinstance(v, int)}

                def parse(names, sec="BASE BID"):
                    r, a = rows_from_grid(grid, base, sheet, stats, fallback_names=names,
                                          section0=sec)
                    # A table the old code could not read at all borrowed the
                    # previous header; one that inference now reads WITHOUT a
                    # single name borrows it too, as before.  Farmers Branch
                    # R2025-011 page 2 (4 money pairs, 2 names lent) was read by
                    # inference, named nobody and lost the 44 rows its borrowed
                    # header had always named.  Here only a header of two or more
                    # pairs, or one vouched for within a page, is lent (lend_names).
                    if may_borrow_header(r, a, len(prefix[-1]) if prefix else 0, prefix_n, pi,
                                         prefix_pg, max((len(row) for row in grid), default=0)):
                        r2, a2 = rows_from_grid(prefix + grid, base, sheet + "+inherited_hdr",
                                                stats, fallback_names=names, section0=sec)
                        # An inherited header must FIT the table it is lent to.
                        # North Richland Hills 6473 splits the right-hand money
                        # cells of its lower rows into an 8-column table; read
                        # under the 21-column header above it, quantities and
                        # prices fell in the wrong columns and two bidders
                        # "bid" $229M and $289M.  When most checkable rows fail
                        # quantity x price == extension, the loan is refused.
                        if a2 and not inherited_parse_fits(r2):
                            stats["inherited_header_refused"] = \
                                stats.get("inherited_header_refused", 0) + 1
                            r2, a2 = [], None
                        if a is None or (a2 and any(a2["bidders"])):
                            if a2:
                                stats["tables_with_inherited_header"] = \
                                    stats.get("tables_with_inherited_header", 0) + 1
                            return r2, a2, True
                    return r, a, False

                lend = lend_names(last_names, pi, names_pg)
                r, a, inherited = parse(lend)
                # A continuation too short to infer its own columns (fewer than
                # four rows: infer_headerless_layout) and with no header to
                # borrow, because the tables above it were read by INFERENCE,
                # whose headers header_prefix does not lend.  Grand Prairie's
                # Fish Creek tab (Bid 25087) captions every column 'UNIT PRICE
                # (NUMBER ONLY)', so each table is inferred; page 1 ends in a
                # two-row table -- the Section 2 caption row and item 37, 8"
                # PVC wastewater pipe, $48,000-$170,148 per bidder -- which read
                # nothing, and every bidder's total came out one item short.
                # Such a table is read under the inferred header of the table
                # above it when it repeats that table's caption row
                # (borrows_inferred_header), with an alternate title printed
                # over it honoured, and kept only when the reading fits
                # (inherited_parse_fits).
                if borrows_inferred_header(r, a, inherited, grid, inf_prefix, inf_pg, pi):
                    if ruled is None:
                        ruled = page.find_tables()
                    tobj = ruled[ti] if ti < len(ruled) else None
                    r3, a3 = rows_from_grid(
                        inf_prefix + grid, base, sheet + "+inherited_hdr", stats,
                        fallback_names=lend,
                        section0=(alternate_title_above(page, tobj) if tobj is not None
                                  else "") or "BASE BID")
                    if r3 and a3 and inherited_parse_fits(r3):
                        r, a, inherited, inf_pg = r3, a3, True, pi
                        stats["inferred_header_borrowed"] = \
                            stats.get("inferred_header_borrowed", 0) + 1
                # The ruled area left something to the text above it: pairs it
                # left unnamed (names_above_table), or a header it never had
                # (an inferred one), whose page title may say the table is an
                # alternate (alternate_title_above).  The table is then read
                # again with what was found, from the counters as they stood,
                # so nothing counts twice.
                need_names = bool(a and r and not all(a["bidders"]))
                inferred = bool(a and r and "+inferred_cols" in (r[0].get("src_sheet") or ""))
                if need_names or inferred:
                    if ruled is None:
                        ruled = page.find_tables()
                    tobj = ruled[ti] if ti < len(ruled) else None
                    above = (names_above_table(page, tobj, a["pairs"])
                             if (need_names and tobj is not None) else None)
                    fill = ([b or n for b, n in zip(a["bidders"], above)]
                            if above and any(n and not b for n, b in zip(above, a["bidders"]))
                            else None)
                    sec = (alternate_title_above(page, tobj)
                           if (tobj is not None and (fill or inferred)) else "")
                    if fill or sec:
                        for k, v in list(stats.items()):
                            if isinstance(v, int):
                                stats[k] = snap.get(k, 0)
                        r, a, inherited = parse(fill or lend, sec or "BASE BID")
                        if fill:
                            stats["bidder_names_above_table"] = \
                                stats.get("bidder_names_above_table", 0) + 1
                        if sec:
                            stats["alternate_title_above_table"] = \
                                stats.get("alternate_title_above_table", 0) + 1
                lent = header_prefix(grid, a, inherited)
                if lent is not None:
                    prefix, prefix_n, prefix_pg = lent, len(a["pairs"]), pi
                elif inherited and r:
                    prefix_pg = pi
                if a and r and a.get("inferred_prefix"):
                    inf_prefix, inf_pg = a["inferred_prefix"], pi
                if a and any(a["bidders"]):
                    last_names = list(a["bidders"])
                    if r or not inherited:
                        names_pg = pi
                recs.extend(r)
                if a:
                    a.update({"src_file": base, "src_sheet": sheet, "n_rows": len(r)})
                    audit.append(a)
    if fz is not None:
        fz.close()
    return recs, audit, strings

# --------------------------------------------------------------------- driver


SUMMARY_HDR_RE = re.compile(
    r"(responding\s+(contractor|supplier|vendor)|respondent|bidder)s?\b.*"
    r"(total|base\s*bid|amount)", re.I)
MONEY_LINE_RE = re.compile(r"^(.{3,80}?)\s+\$\s*([\d, ]+\.?\d{0,2})\s*$")


def parse_summary_totals(path, rel_name, owner):
    """Many solicitations publish only a SUMMARY tab: each responding contractor
    and its total bid, with no line items. Those files yield zero unit prices but
    are still valuable -- a project total plus a bidder count is exactly what the
    competition discount (E[low]/median as a function of N) is estimated from.
    """
    rows, strings = [], []
    try:
        kind = sniff_office(path)          # an Office file under a .pdf name
        if kind == "docx":
            return []
        if path.lower().endswith(".pdf") and not kind:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages[:4]:
                    strings.extend((page.extract_text() or "").split("\n"))
        else:
            with open(path, "rb") as fh:
                wb = openpyxl.load_workbook(fh, data_only=True, read_only=True)
                try:
                    for ws in wb.worksheets:
                        for r in ws.iter_rows(values_only=True):
                            cells = [txt(c) for c in r]
                            if any(cells):
                                strings.append(" ".join(x for x in cells if x))
                finally:
                    wb.close()
    except Exception:
        return []

    title = ""
    for s in strings[:12]:
        if len(s) > 10 and not re.match(r"^(city of|bid tabulation|solicitation no)", s, re.I):
            title = title or s[:90]
    bd, _src, _all = find_dates(strings)

    start = next((i for i, s in enumerate(strings) if SUMMARY_HDR_RE.search(s)), None)
    if start is None:
        return []
    for s in strings[start + 1:]:
        m = MONEY_LINE_RE.match(s.strip())
        if not m:
            if rows:
                break          # the contractor block has ended
            continue
        name, amt = m.group(1).strip(), num(m.group(2))
        if amt is None or amt < 1000:
            continue
        if NONBIDDER_RE.match(name) or not looks_like_bidder(name):
            continue
        rows.append({"src_file": rel_name, "owner": owner, "project_title": title,
                     "bid_date": bd, "bidder_raw": name,
                     "bidder": canon_bidder(name),
                     "is_engineer_estimate": ENGINEER_RE.search(name) is not None,
                     "total_bid": amt})
    if len(rows) < 2:
        return []
    bids = [r["total_bid"] for r in rows if not r["is_engineer_estimate"]]
    for r in rows:
        r["n_bidders"] = len(bids)
        r["low_bid"] = min(bids) if bids else None
        r["rank"] = (sorted(bids).index(r["total_bid"]) + 1
                     if (not r["is_engineer_estimate"] and r["total_bid"] in bids) else "")
    return rows


def parse_site_totals_tab(path):
    """This tab has no line items -- only Site x Bidder totals, twice over
    ('Bid Total' using as-bid quantities, 'Plan Quantity Total' using plan
    quantities). Both versions are kept and labelled."""
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for ti, table in enumerate(page.extract_tables()):
                grid = [[txt(c) for c in r] for r in table]
                hdr_i = next((i for i, r in enumerate(grid)
                              if sum(1 for c in r if looks_like_bidder(c)) >= 2), None)
                if hdr_i is None:
                    continue
                names = {i: c for i, c in enumerate(grid[hdr_i]) if looks_like_bidder(c)}
                basis = "plan_quantity" if ti == 1 else "as_bid"
                for r in grid[hdr_i + 1:]:
                    label = txt(r[0]) if r else ""
                    if not re.match(r"^site\s", label, re.I):
                        continue
                    for ci, nm in names.items():
                        v = num(r[ci]) if ci < len(r) else None
                        if v is None or v <= 0:
                            continue
                        rows.append({
                            "src_file": os.path.basename(path),
                            "quantity_basis": basis,
                            "site": label,
                            "bidder_raw": nm,
                            "bidder": canon_bidder(nm),
                            "is_engineer_estimate": ENGINEER_RE.search(nm) is not None,
                            "site_total": v,
                        })
    return rows


def failed_arithmetic_files(recs, min_rows=10, share=0.9):
    """Files whose checkable rows (quantity, price and extension all read)
    number min_rows or more and fail quantity x price = extension in `share`
    or more of them (see main())."""
    arith = defaultdict(lambda: [0, 0])
    for r in recs:
        if r.get("extended_calc") is not None and r.get("extended_reported") is not None:
            arith[r["src_file"]][0] += 1
            arith[r["src_file"]][1] += 1 if r.get("ext_mismatch") == "Y" else 0
    return {f for f, (n, bad) in arith.items() if n >= min_rows and bad >= share * n}


# ---- a "non-scope" section the printed BASE total includes -----------------
# main() gives a CONTINGENCY section the role 'other' (out of the base bid),
# but several tabs total it INTO the base: NRH RFB 24-021 prints 'TOTAL BASE
# BID (SECTIONS I THROUGH V)' with section V the $10,000 project contingency
# (Tex-Pro $100,815, the low bid; read without it, $90,815), NRH 4627 'TOTAL -
# BASE BID' = 'SUBTOTAL - BASE BID' + 'SUBTOTAL - CONTINGENCY', NRH 6123 the
# same; while Colleyville's Roberts Road prints its $180,000 contingency AFTER
# the base, under 'Grand Total Including Contingency'.  So the document
# decides, file by file: the section counts as base when, for the bidders who
# price it, base + section is an amount on the document's TOTAL lines
# (total_line_amounts: no subtotal, no one section's total) and base alone is
# not, for at least one bidder and against none.
def nonbase_sections_in_printed_base(recs, doc_facts):
    """{(src_file, bid_section)} of role-'other' sections inside the printed
    base total."""
    by_file = defaultdict(list)
    for r in recs:
        if r.get("section_role") in ("base", "other"):
            by_file[r["src_file"]].append(r)
    out = set()
    for f, rs in by_file.items():
        secs = {r["bid_section"] for r in rs if r["section_role"] == "other"}
        tl = set((doc_facts.get(f) or {}).get("total_lines") or ())
        if not secs or not tl:
            continue
        base, other = defaultdict(float), defaultdict(float)
        for r in rs:
            e = r.get("extended_reported")
            if e is None and r.get("unit_price") is not None:
                e = r["unit_price"] * (r["quantity"] if r.get("quantity") is not None else 1.0)
            if e is not None:
                (base if r["section_role"] == "base" else other)[r.get("bidder_raw")] += e

        def printed(v):
            c = int(round(v * 100))
            return c > 0 and any(c + d in tl for d in (-1, 0, 1))
        yes = sum(1 for b in other if printed(base[b] + other[b]) and not printed(base[b]))
        no = sum(1 for b in other if printed(base[b]))
        if yes and not no:
            out |= {(f, s) for s in secs}
    return out


# ---- item numbers printed into the description (round 5) ---------------------
# Some readers leave the item number glued to the description: Edinburg
# Brennaleen (coords reader, item_no empty) gives "1Mobilization/Bonds/
# Insurance", "2Traffic Control", ... "724" R.C.P. Class III" -- item 7, a 24"
# pipe, read as a 724 in pipe and priced at $1,184/LF against $87 -- and
# Lewisville 25-4369 / 25-4410 give "#0-71Saw cut full depth concrete" and
# "#0-146" (3750 p.s.i.) concrete for pavement" (item #0-14, 6").  The glued
# number also breaks the family rules ("13stabilized backfill", "71saw cut").
# A leading number is an item number only when the SHEET shows the pattern:
# its descriptions open with a running number (n, n+1, ..., gaps of up to 3,
# restarting at 1 for a new schedule; the same "#D-" section prefix), at
# least three of them with a word right after the number, a run of three
# consecutive numbers, and half the sheet's distinct descriptions in all.  A
# number followed by a unit mark (", ', in, ft, x) is a size and never taken.
_LEAD_CODE_RE = re.compile(r"^\s*(#\s?)?((?:\d{1,2}\s?-\s?)?)(\d{1,3})")
_AFTER_CODE_UNIT_RE = re.compile(
    r"^\s*(?:[\"'\u201d\u2033\u2032]|in\b|inch|ft\b|foot|feet|mm\b|x\s?\d|-\s?(?:in|inch|ft|foot)\b|%|"
    r"(?:st|nd|rd|th)\b)", re.I)


def _lead_code(desc, expect, span):
    """(code, n, section prefix, rest) when `desc` opens with item number n in
    expect .. expect+span-1, else None."""
    m = _LEAD_CODE_RE.match(desc or "")
    if not m:
        return None
    hashp, sect, digits = (m.group(1) or "").strip(), re.sub(r"\s", "", m.group(2) or ""), m.group(3)
    for n in range(expect, expect + span):
        s = str(n)
        if digits.startswith(s):
            rest = digits[len(s):] + desc[m.end():]
            if _AFTER_CODE_UNIT_RE.match(rest):
                return None
            return hashp + sect + s, n, sect, rest
    return None


def strip_leading_item_numbers(recs, stats=None):
    """Move item numbers printed into the description out of it (see above).
    Recomputes desc_norm, item_family, size_in and material of every row it
    changes and fills an empty item_no.  Returns the number of rows changed."""
    sheets = {}
    for r in recs:
        sheets.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    changed = items_n = 0
    for rows in sheets.values():
        order, seen = [], set()
        for r in rows:
            d = r.get("description") or ""
            if d and d not in seen:
                seen.add(d)
                order.append(d)
        if len(order) < 3:
            continue
        plan, expect, sect0, prev, run, best, words = {}, 1, None, None, 0, 0, 0
        for d in order:
            c = None
            # an unambiguous number -- a word follows it ("#0-12Remove ...",
            # "#0-39Barrier free ramps") -- (re)starts the count there: the
            # sheet's first numbered item, or after a gap in the numbering
            m0 = _LEAD_CODE_RE.match(d)
            whole = None
            if m0 and re.match(r"[\s.):\-]*[A-Za-z]", d[m0.end():]) \
                    and not _AFTER_CODE_UNIT_RE.match(d[m0.end():]):
                whole = int(m0.group(3))
            if prev is None and whole is not None:
                c = _lead_code(d, whole, 1)
            if c is None:
                c = _lead_code(d, expect, 4)
            if c is not None and sect0 is not None and c[2] != sect0 and c[1] != 1:
                c = None
            if c is None and whole is not None and prev is not None and whole > prev:
                c = _lead_code(d, whole, 1)
                if c is not None and sect0 is not None and c[2] != sect0:
                    c = None
            if c is None and expect != 1:
                c = _lead_code(d, 1, 1)                    # a new schedule
            if c is None:
                continue
            code, n, sect, rest = c
            if sect0 is None or n == 1:
                sect0 = sect
            run = run + 1 if prev is not None and n == prev + 1 else 1
            best = max(best, run)
            prev, expect = n, n + 1
            rest = re.sub(r"^[\s.):\-]+", "", rest)
            if re.match(r"[A-Za-z]{3}", rest):          # a word, not a code ("101S-B")
                words += 1
            plan[d] = (code, rest)
        if words < 3 or best < 3 or 2 * len(plan) < len(order):
            continue
        items_n += len(plan)
        for r in rows:
            p = plan.get(r.get("description") or "")
            if p is None or not p[1]:
                continue
            code, rest = p
            r["description"] = rest
            if not (r.get("item_no") or "").strip():
                r["item_no"] = code
            dn = norm_desc(rest)
            r["desc_norm"] = dn
            r["item_family"] = item_family(dn)
            r["size_in"] = extract_size_in(dn)
            m = MATERIAL_RE.search(dn)
            r["material"] = (m.group(1).upper() if m else "")
            changed += 1
    if stats is not None and changed:
        stats["item_number_in_description"] = {"rows": changed, "items": items_n}
    return changed


# ---- a lump sum whose description states its quantity (round 5) --------------
# Mesquite writes the plan quantity into LS earthwork items: "Excavation
# (Roadway) (28,731 Cy)" (25-8014, LS at $1.16M), "Embankment (Final) (Ty D)
# (Density Control) (63,906 Cy)".  Priced as one lump sum from the LS cell
# that line came out at $31.8k.  The stated quantity and unit, in brackets,
# are the line's real quantity and unit: quantity := N, unit := that unit,
# unit_price := the lump sum / N, so quantity x unit_price is still the lump
# sum and no money moves (unit_note 'ls_quantity_in_description', qty_source
# 'ls_description'; unit_raw keeps the printed LS).  Exactly one bracketed
# quantity, of a measured unit, above 1; never a per / max / min figure.
_LS_QTY_RE = re.compile(
    r"\(\s*(?:approx(?:\.|imately)?\s*|est(?:\.|imated)?\s*|plan\s+quantity\s*[:=-]?\s*)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(c\.?\s?y\.?|cu\.?\s?yds?\.?|cubic\s+yards?|s\.?\s?y\.?|sq\.?\s?yds?\.?|square\s+yards?|"
    r"l\.?\s?f\.?|lin\.?\s?ft\.?|linear\s+f(?:ee|oo)t|s\.?\s?f\.?|sq\.?\s?ft\.?|square\s+f(?:ee|oo)t|"
    r"tons?|acres?|ac\.?|gal(?:lons?)?\.?)\s*\)", re.I)


_LS_QTY_QUALIFIED_RE = re.compile(r"\b(?:max(?:imum)?|min(?:imum)?|up\s+to|not\s+to\s+exceed|per|each|every)\b\.?\s*\d", re.I)


def _ls_unit(u):
    u = re.sub(r"[^a-z]", "", u.lower())
    if u.startswith(("cy", "cuyd", "cubic")):
        return "CY"
    if u in ("sy", "sqyd", "sqyds") or u.startswith("squareyard"):
        return "SY"
    if u.startswith(("lf", "linft", "linear")):
        return "LF"
    if u in ("sf", "sqft") or u.startswith("squarefe") or u.startswith("squarefo"):
        return "SF"
    if u.startswith("ton"):
        return "TON"
    if u.startswith("ac"):
        return "AC"
    if u.startswith("gal"):
        return "GAL"
    return None


def ls_quantity_in_description(desc):
    """(quantity, unit) stated in brackets in an LS description, or None."""
    hits = set()
    for m in _LS_QTY_RE.finditer(desc or ""):
        u = _ls_unit(m.group(2))
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if u:
            hits.add((v, u))
    if len(hits) != 1 or _LS_QTY_QUALIFIED_RE.search(desc or ""):
        return None
    v, u = hits.pop()
    return (v, u) if v > 1 else None


_TRAIL_ITEM_QTY_RE = re.compile(r"\s+(\d{1,4})\s+(\d[\d,]*(?:\.\d+)?)\s*$")


def strip_trailing_item_quantity(recs, stats=None):
    """Parser round 8 (.29): the item number and the quantity read into the END of the description
    cell -- Frisco 2406-104's "INSTALL 48-INCH DIP CLASS 200 WATER PIPE BY OPEN CUT 14 4105" (item
    #0-14, 4,105 LF), "MOBILIZATION (5% MAX) 1 1", 70 items in one tab.  A trailing pair is removed
    when its first number is the row's own item number (the last integer of item_no) and its second
    the row's own quantity, and only on a sheet where at least three descriptions show it (one
    coincidence is no layout).  desc_norm, item_family, size_in and material are recomputed.
    Returns the number of rows changed."""
    sheets = {}
    for r in recs:
        sheets.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    changed = 0
    for rows in sheets.values():
        plan = {}
        for r in rows:
            d = r.get("description") or ""
            m = _TRAIL_ITEM_QTY_RE.search(d)
            q = r.get("quantity")
            if not m or q is None:
                continue
            nums = re.findall(r"\d+", r.get("item_no") or "")
            if not nums or nums[-1].lstrip("0") != m.group(1).lstrip("0"):
                continue
            try:
                qq = float(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if abs(qq - q) > 1e-6 * max(1.0, abs(q)):
                continue
            rest = d[:m.start()].rstrip()
            if re.search(r"[A-Za-z]{3}", rest):
                plan[(d, r.get("item_no"))] = rest
        if len({k[0] for k in plan}) < 3:
            continue
        for r in rows:
            rest = plan.get((r.get("description") or "", r.get("item_no")))
            if rest is None:
                continue
            r["description"] = rest
            dn = norm_desc(rest)
            r["desc_norm"] = dn
            r["item_family"] = item_family(dn)
            r["size_in"] = extract_size_in(dn)
            m = MATERIAL_RE.search(dn)
            r["material"] = (m.group(1).upper() if m else "")
            changed += 1
    if stats is not None and changed:
        stats["item_quantity_at_end_of_description"] = changed
    return changed


# ---- (round 11, .32) the UNIT (and quantity) read into the end of the description -------------------
# Where the header names no unit or quantity column the reader can place, the description takes the
# columns after it: Frisco's bid-system export ('Quan(ti)ty Required | Unit of Measure' read as no
# caption) gives "MOBILIZATION (5% MAX) 017113 1 LS" (Frisco on the Green, 1,279 rows; Buffalo Ridge Park,
# Bid 2507-105 traffic signals), Mansfield's 'Project Item Information' template "Mobilization LS 1"
# (Debbie Lane), Northlake's trunk-line tab "8" SDR 26 PVC Wastewater Line (Open Cut) LF", Collin
# County's supplier tabs "... 2 each" -- the units are printed, but every such row went out without one.
# A description that ENDS with a unit of measure, alone ("... LF") or beside a number ("... 1 LS",
# "... LS 1"), gives the row that unit when:
#   * the row has no unit of its own;
#   * the unit is a measure unit (LS, LF, SY, SF, CY, CF, EA / each, MO, TON, AC, STA, GAL, LB, HR, DAY,
#     WK, MG, VF, ALLOW), never FT / IN (a dimension: '6 FT', '12 IN'), never a goods unit (box, case,
#     lot, head: words), never after 'per' or '/' (a rate in words), and a unit alone only as an
#     abbreviation (LS, LF, SY, CY, EA, STA, ...), not a word;
#   * the number beside it is the row's own quantity, or -- the row having none -- number x unit price is
#     the printed extension (the quantity is then taken too); with neither a quantity nor an extension the
#     number proves nothing and only the unit is read;
#   * on the row's sheet at least three descriptions, and at least half of those without a unit, end
#     this way (one coincidence is no column).
# The unit and the number are removed from the description (desc_norm, item_family, size_in and material
# recomputed) and the row is marked unit_note 'unit_in_description'.  A unit and quantity that
# recover_code_only_descriptions removed with the words it took from the item cell ('#1-1 Project Sign
# 15813 EA 1', Frisco Bid 2503-062) are read the same way (r['_desc_tail']).
_TAIL_UNITS = {"LS", "LF", "SY", "SF", "CY", "CF", "EA", "MO", "TON", "AC", "STA", "GAL", "LB", "HR", "DAY",
               "WK", "MG", "VF", "ALLOW"}
_TAIL_UNIT_WORD = (r"(?:l\.?\s?s\.?|lump\s+sum|lsum|l\.?\s?f\.?|lin\.?\s?ft\.?|s\.?\s?y\.?|sq\.?\s?yds?\.?|"
                   r"s\.?\s?f\.?|sq\.?\s?ft\.?|c\.?\s?y\.?|cu\.?\s?yds?\.?|c\.?\s?f\.?|ea\.?|each|mo\.?|mos\.?|"
                   r"months?|tons?|ac\.?|acres?|sta\.?|gal\.?|gallons?|lbs?\.?|hrs?\.?|hours?|days?|wks?\.?|"
                   r"weeks?|mg|v\.?\s?f\.?|vlf|allow(?:ance)?|al|alw)")
_TAIL_ABBR = {"LS", "L.S.", "LF", "L.F.", "SY", "S.Y.", "SF", "S.F.", "CY", "C.Y.", "CF", "EA", "EA.", "MO",
              "MOS", "AC", "STA", "STA.", "GAL", "HR", "HRS", "VF", "VLF", "LSUM", "LBS", "WK", "MG"}
_TAIL_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_TAIL_QU_RE = re.compile(r"^(.*?)(?:(?<=[\s)\]])|^)" + _TAIL_NUM + r"\s*(" + _TAIL_UNIT_WORD + r")\s*$", re.I)
_TAIL_UQ_RE = re.compile(r"^(.*?)\s(" + _TAIL_UNIT_WORD + r")\s+" + _TAIL_NUM + r"\s*$", re.I)
_TAIL_U_RE = re.compile(r"^(.*?)\s(" + _TAIL_UNIT_WORD + r")\s*$", re.I)
_TAIL_RATE_RE = re.compile(r"(?:\bper|/)\s*$", re.I)


def _tail_unit(text):
    """(text before, number or None, unit as printed) of a description ending with a unit, or None."""
    s = txt(text)
    for rx, kind in ((_TAIL_QU_RE, "qu"), (_TAIL_UQ_RE, "uq"), (_TAIL_U_RE, "u")):
        m = rx.match(s)
        if not m:
            continue
        if kind == "qu":
            pre, n, u = m.group(1), m.group(2), m.group(3)
        elif kind == "uq":
            pre, u, n = m.group(1), m.group(2), m.group(3)
        else:
            pre, u, n = m.group(1), m.group(2), None
        if norm_unit(u) not in _TAIL_UNITS:
            continue
        if kind == "u" and (u.upper().replace(" ", "") not in _TAIL_ABBR or re.search(r"\d\s*$", pre)):
            continue                      # a word, or a number beside it the forms above refused
        if _TAIL_RATE_RE.search(pre) or not re.search(r"[A-Za-z]{3}", pre):
            continue
        if kind == "qu" and re.search(r"\d\s*(?:x|/|-|\+)\s*$", pre, re.I):
            continue                      # '2 x 4 LF', '1/2 LS': the number is part of a dimension
        try:
            q = float(n.replace(",", "")) if n is not None else None
        except ValueError:
            continue
        return pre.rstrip(" ,;:-"), q, u
    return None


def units_at_end_of_description(recs, stats=None):
    """See above.  Returns the number of rows given a unit."""
    sheets = {}
    for r in recs:
        sheets.setdefault((r.get("src_file"), r.get("src_sheet")), []).append(r)
    changed = qset = 0
    for rows in sheets.values():
        plan, votes, bad, nounit = {}, {}, set(), set()
        for r in rows:
            tail_txt = r.get("_desc_tail")
            if (r.get("unit_raw") or "").strip() or r.get("unit_price") is None:
                continue
            d = r.get("description") or ""
            nounit.add(d)
            key = (d, r.get("item_no"))
            if tail_txt:
                got = _tail_unit("words " + tail_txt)      # the tail removed with the item cell's words
                got = (d, got[1], got[2]) if got else None
            else:
                got = _tail_unit(d)
            if got is None:
                bad.add(key)                  # the same item read without a unit tail: leave it
                continue
            pre, n, u = got
            prev = plan.get(key)
            if prev is not None and prev != (pre, n, u):
                bad.add(key)                  # the same item read two ways: leave it
                continue
            plan[key] = (pre, n, u)
            # does the number beside the unit say this row's quantity?  (the row's own quantity, or
            # number x unit price = the printed extension); a row with neither says nothing
            v = votes.setdefault(key, [0, 0])
            q, up, ext = r.get("quantity"), r.get("unit_price"), r.get("extended_reported")
            if n is None:
                continue
            if q is not None:
                v[0 if abs(n - q) <= 1e-6 * max(1.0, abs(q)) else 1] += 1
            elif up is not None and ext is not None:
                v[0 if abs(n * up - ext) <= _QTY_TOL * max(abs(ext), 1.0) else 1] += 1
        for k in list(plan):
            pre, n, u = plan[k]
            agree, disagree = votes.get(k, [0, 0])
            if k in bad or (n is not None and disagree and agree <= disagree):
                plan.pop(k)               # the number is not the item's quantity: no column
        hits = {k[0] for k in plan}
        if len(hits) < 3 or len(hits) < 0.5 * len(nounit):
            continue
        for r in rows:
            if (r.get("unit_raw") or "").strip():
                continue
            key = (r.get("description") or "", r.get("item_no"))
            p = plan.get(key)
            if not p:
                continue
            pre, n, u = p
            if r.get("_desc_tail"):
                pre = r.get("description") or ""
            if pre != (r.get("description") or ""):
                _set_description(r, pre)
            r["unit_raw"] = u
            r["unit"] = norm_unit(u)
            r["unit_note"] = "unit_in_description"
            if n is not None and r.get("quantity") is None and votes.get(key, [0, 0])[0] > 0:
                # the item's printed quantity, proved on its other rows: a row whose own extension
                # disagrees keeps it and is flagged by ext_mismatch, like any column read
                r["quantity"] = n
                r["qty_source"] = "desc_tail_verified"
                qset += 1
            changed += 1
    for r in recs:
        r.pop("_desc_tail", None)
    if stats is not None and changed:
        stats["unit_in_description"] = {"rows": changed, "quantities": qset}
    return changed


def apply_ls_quantity_in_description(r):
    """See _LS_QTY_RE.  Returns True when the row was converted."""
    if r.get("unit") != "LS" or r.get("unit_suspect") == "Y" or r.get("ext_mismatch") == "Y":
        return False
    q, up = r.get("quantity"), r.get("unit_price")
    if (q is not None and abs(q - 1.0) > 1e-9) or not up:
        return False
    got = ls_quantity_in_description(r.get("description"))
    if got is None:
        return False
    n, unit = got
    amount = up * (q if q is not None else 1.0)
    r["quantity"] = n
    r["unit_price"] = amount / n
    r["unit"] = unit
    r["extended_calc"] = amount
    r["unit_note"] = "ls_quantity_in_description"
    r["qty_source"] = "ls_description"
    return True


# ---- EA by the hundred on a pipe line (round 5) -----------------------------
# A pipe, casing or grout-abandonment line billed "EA" with a quantity of 500
# or more is almost certainly linear feet: Mesquite 26-8209 prints "EA" x
# 1,145 for grouting an abandoned 6" water line ($12.50 each), priced at
# $1,469/EA from the EA cell; a 24" DIP "EA" x 943 was priced at $4,400
# against $492.  The unit is not guessed: the row loses its unit (unit_note
# 'ea_quantity_suspect') so no EA cell prices from it, but its money stays in
# the bidders' totals (main()).
_EA_PIPE_FAMILIES = {"storm_pipe", "sanitary_pipe", "water_pipe", "pipe_bursting",
                     "box_culvert", "encasement"}
_GROUT_ABANDON_RE = re.compile(r"\bgrout\w*\b.{0,60}\bab[ao]nd[ao]n\w*|\bab[ao]nd[ao]n\w*\b.{0,60}\bgrout\w*|"
                               r"\bflowable\s+fill\b.{0,40}\bab[ao]nd[ao]n\w*|"
                               r"\b(?:water|sewer|sanitary|wastewater|storm)\s+(?:line|main|sewer|drain)\s+grouting\b")
EA_SUSPECT_MIN_QTY = 500


# (round 6, .27) conduit, duct and cable are linear too: Highland Park's
# Wycliffe Ave (2026) prints "CONDT (PVC) (SCHD 40) (6")" as EA x 1,240 and
# the 3" as EA x 355, bid at $56 and $34 each (per-foot prices), and the
# sized electrical_lighting|EA cells (ground boxes, foundations) priced them
# at $1,559 and $841 -- $2.2M on two lines.  For these the bar is 100: no
# conduit or cable run is bought by the hundred pieces, while splices,
# terminations, connectors, risers, fittings and boxes are EA items and stay.
_EA_LINEAR_ELEC_RE = re.compile(r"\b(?:conduits?|condt|ducts?|duct\s?banks?|innerducts?|cables?|"
                                r"conductors?|condr|wires?|wiring)\b")
_EA_LINEAR_ELEC_NOT_RE = re.compile(r"\b(?:splices?|terminat\w*|connectors?|risers?|fittings?|boxe?s?|"
                                    r"markers?|tags?|plugs?|caps?|lugs?|clamps?|straps?|sweeps?|elbows?|"
                                    r"couplings?|adapters?|bushings?|heads?|poles?|bases?|foundations?)\b")
_EA_ELEC_FAMILIES = {"electrical_lighting", "traffic_signal"}
EA_SUSPECT_MIN_QTY_LINEAR_ELEC = 100
# (round 7, .28) the same bar, in ANY family, for the utility runs that are
# linear by name: Azle's Walnut Creek force main (2024) prints "Tracer Wire" as
# EA x 4,530 (bid $0.60 each, a per-foot price; `other`, so the conduit rule
# above never saw it) and was priced at $28/EA.  Tracer / locator / toning
# wire and warning / marking / detection / identification tape only -- a roll
# of electrical or Teflon tape is a supply item, and a tracer-wire test
# station, box or splice kit is bought by the piece.
_EA_LINEAR_UTIL_RE = re.compile(r"\b(?:tracer|tracing|trace|locat\w*|toning|toner)\s+wires?\b|"
                                r"\b(?:warning|marking|marker|detect\w*|identification|caution|locat\w*)\s+tapes?\b")
_EA_LINEAR_UTIL_NOT_RE = re.compile(r"\b(?:boxe?s?|stations?|splices?|kits?|connectors?|lugs?|clamps?|"
                                    r"terminals?|anodes?|access\s+points?|rolls?)\b")
# Bedding / embedment AS THE ITEM is pipe-zone material bought by the foot,
# the yard or the ton, never by the piece: White Settlement's Rumfield & Gibbs
# (2022) prints "Cement Stabilized Sand Embedment" as EA x 40 (bid $75 each;
# the sister letting Rowland St prints it LF at $80) and it was priced at
# $1,354/EA.  Only in the bedding-material families (a pipe, service, valve or
# hydrant line "including embedment" is not this item) and only with a
# granular / cement / pipe word (not "BEDDING AND TOWELS: BLANKET").  The bar is
# 20: no bedding is placed in twenty separate pieces.
_EA_BEDDING_FAMILIES = {"flowable_backfill", "borrow_backfill", "embankment_fill", "subgrade_stab", "other"}
_EA_BEDDING_RE = re.compile(r"^(?!.*\b(?:includ\w*|incl|w/|with|towels?|blankets?|pillow\w*|mattress\w*|linens?)\b)"
                            r"(?=.*\b(?:sand|gravel|rock|stone|cement\w*|crushed|aggregate|granular|select|pipe|"
                            r"flowable|clsm|cls)\b)(?:\S+\s+){0,5}?(?:bedding|embedment)\b")
EA_SUSPECT_MIN_QTY_BEDDING = 20


def ea_quantity_suspect(r):
    if (r.get("unit") or "") != "EA":
        return False
    q = r.get("quantity")
    if q is None or q < min(EA_SUSPECT_MIN_QTY, EA_SUSPECT_MIN_QTY_LINEAR_ELEC,
                            EA_SUSPECT_MIN_QTY_BEDDING):
        return False
    dn = r.get("desc_norm") or ""
    if (r.get("item_family") in _EA_ELEC_FAMILIES and _EA_LINEAR_ELEC_RE.search(dn)
            and not _EA_LINEAR_ELEC_NOT_RE.search(dn)):
        return q >= EA_SUSPECT_MIN_QTY_LINEAR_ELEC
    if _EA_LINEAR_UTIL_RE.search(dn) and not _EA_LINEAR_UTIL_NOT_RE.search(dn):
        return q >= EA_SUSPECT_MIN_QTY_LINEAR_ELEC
    if r.get("item_family") in _EA_BEDDING_FAMILIES and _EA_BEDDING_RE.search(dn):
        return q >= EA_SUSPECT_MIN_QTY_BEDDING
    # (round 8, .29) trench safety is priced per LF: "Trench Safety for Storm Drain Systems" EA x 1,514
    # at $1.07 was priced from the EA cell (a $1,500 trench-safety plan) at $6,342 each
    if r.get("item_family") == "trench_safety" and q >= EA_SUSPECT_MIN_QTY_LINEAR_ELEC:
        return True
    if q < EA_SUSPECT_MIN_QTY:
        return False
    return (r.get("item_family") in _EA_PIPE_FAMILIES) or bool(_GROUT_ABANDON_RE.search(dn))


# ---- a lump sum printed with a measure unit (round 6, .27) -------------------
# One "SY" of site grading at $3,000-17,000 is a lump sum: Richardson 05-22
# (2021) prints "Provide site grading improvements ..." as SY x 1, and that row
# was half of grading|SY (the other half riffle micro-grading), which priced
# Azle's 770 SY of site grading at $163/SY.  Per ITEM (every bidder's row of it,
# engineer included): a measure unit (SY / SF / LF / CY), quantity 1 on every
# row, a family whose measure price cannot physically reach $1,000, and a
# median unit price of $1,000 or more -> unit LS, money unchanged
# (unit_note 'measure_qty1_lump_sum').
MEASURE_QTY1_UNITS = {"SY", "SF", "LF", "CY"}
MEASURE_QTY1_MIN_PRICE = 1000.0
MEASURE_QTY1_FAMILIES = {
    "grading", "seeding_sod", "erosion_control", "excavation", "embankment_fill", "borrow_backfill",
    "flowable_backfill", "site_prep_clearing", "sidewalk", "curb_gutter", "barrier_free_ramp",
    "driveway", "flex_base", "subgrade_stab", "asphalt_pavement", "conc_pavement", "pavement_milling",
    "surface_treatment", "sawcut", "pavement_marking", "cleaning_desilting", "demo_pavement",
    "demo_sidewalk", "demo_other", "trm", "trm_permanent", "trm_temporary", "irrigation",
    "mowing_vegetation"}


def measure_qty1_lump_sums(rows):
    """Item keys (see _measure_item_key) of the rows that are lump sums
    printed with a measure unit; the caller sets their unit to LS."""
    by = {}
    for r in rows:
        if (r.get("unit") or "") not in MEASURE_QTY1_UNITS:
            continue
        by.setdefault(_measure_item_key(r), []).append(r)
    out = set()
    for k, rs in by.items():
        if any((r.get("item_family") or "") not in MEASURE_QTY1_FAMILIES for r in rs):
            continue
        if any(r.get("quantity") is None or abs(r["quantity"] - 1.0) > 1e-9 for r in rs):
            continue
        ups = sorted(r["unit_price"] for r in rs
                     if r.get("unit_price") is not None and not r.get("is_engineer_estimate"))
        if not ups:
            continue
        med = ups[len(ups) // 2] if len(ups) % 2 else 0.5 * (ups[len(ups) // 2 - 1] + ups[len(ups) // 2])
        if med >= MEASURE_QTY1_MIN_PRICE:
            out.add(k)
    return out


def _measure_item_key(r):
    return (r.get("src_file"), r.get("src_sheet"), r.get("bid_section"), r.get("item_no"),
            r.get("desc_norm"), r.get("unit"))


# ---- a description that is only a code (round 7, .28) -----------------------
# Some tabs reach the parser with no word in the description cell:
#   - the item number again ("P01", "A-4", "A2-1"): ONE caption cell 'ITEM NO.
#     ITEM DESCRIPTION' printed over two columns bound the item AND the
#     description to the first (Frisco 2603-054, Trophy Club Bobcat Blvd,
#     Highland Park Hackberry Creek, Kaufman Crestview) -- see
#     _split_merged_item_caption in the table reader;
#   - the words in the item cell and a spec reference, a quantity or nothing
#     in the description cell ("MOBILIZATION | 17113", Frisco 2203-037;
#     "1. Mobilization | ''", Mansfield MAN25008; "B-1 Reinforced Concrete Pipe
#     (Class III) | (18")", Mansfield 21-4441);
#   - the tail of a wrapped cell whose first line the PDF never printed:
#     "P2)" of '8" REINFORCED CONCRETE PAVEMENT W/ INTERGRAL CURB & GUTTER (CL
#     P1 OR P2)' (Kimley-Horn tabs, Highland Park Beverly Dr and Potomac /
#     Auburndale 2025), filed `other` and priced from other|SY at about $22
#     against $90-107.
# recover_code_only_descriptions restores the words where the page has them
# (the item cell; the same bidder's same item -- item number, unit, quantity
# -- printed in full elsewhere in the file, whose description ENDS with the
# fragment).  Nothing is guessed: a line still without a word keeps its money
# and unit_raw but loses its unit (unit_note 'desc_code_only', main()), so no
# unit-price cell trains on it and the estimator, which refuses a line with no
# unit, treats it as unknown instead of pricing it from a catch-all cell.
_ITEM_CODE_RE = re.compile(r"^[#(\[]*[A-Za-z]{0,5}[-.]?\d{1,6}[A-Za-z]?(?:[-.]\d{1,6}[A-Za-z]?)*[.):\]]*$")
_DESC_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_DESC_STOPWORDS = {"alt", "alts", "add", "item", "items", "bid", "ref", "spec", "specs", "sect",
                   "section", "page", "see"}
_ITEM_WORDS_RE = re.compile(r"^\s*#?\s*((?:[A-Za-z]{1,5}[-.]?)?\d{1,4}[A-Za-z]?(?:[-.]\d{1,4}[A-Za-z]?)*)[.):]?\s+"
                            r"(?:\d{5,6}\s+)?(?=.*[A-Za-z]{3})(.+)$")
_TRAILING_UNIT_QTY_RE = re.compile(r"\s+(?:tx\s?dot\s+)?(?:\d{2,6}\s+)?(?:LS|L\.S\.|EA|EACH|SY|SF|LF|CY|TON|TONS|"
                                   r"GAL|AC|MO|HR|DAY|STA|VF|LB)\s+[\d,.]+\s*$", re.I)
_MULTI_ITEM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,5}(?:-[A-Za-z0-9]{1,3})*-\d{1,3}(?!\d)")


def desc_is_code_only(desc):
    """True when a description holds no word: blank, an item number or code
    ("P01", "A2-1", "[02513]", "321723"), a size ('9" X 42"') or a wrapped
    tail ("P2)").  'alt', 'item', 'bid', 'spec' and the like are no words."""
    s = txt(desc)
    if not s:
        return True
    for w in _DESC_WORD_RE.findall(s):
        if w.lower() not in _DESC_STOPWORDS:
            return False
    return True


def _split_item_words(item_no, desc):
    """(item_no, description) for an item cell that holds the words, or None.
    "1. Mobilization" -> ("1", "Mobilization"); "#1-2 Mobilization TX DOT LS 1"
    -> ("1-2", "Mobilization"); "MOBILIZATION" beside "17113" (a spec number)
    -> ("", "MOBILIZATION"); "B-1 Reinforced Concrete Pipe (Class III)" beside
    '(18")' -> ("B-1", 'Reinforced Concrete Pipe (Class III) (18")').  A bare
    number beside the words never becomes the item number: Frisco Dominion
    Trail's "329223 SODDING" stands beside its quantity, 1642.  Not a cell that
    glues two items ("ALT-A-1 ... ALT-A-2 ...")."""
    s = txt(item_no)
    if desc_is_code_only(s) or len(_MULTI_ITEM_RE.findall(s)) >= 2:
        return None
    code, words = "", s
    m = _ITEM_WORDS_RE.match(s)
    if m:
        code, words = m.group(1), m.group(2)
    words = _TRAILING_UNIT_QTY_RE.sub("", words).strip()
    if desc_is_code_only(words):
        return None
    old = txt(desc)
    if old and not re.fullmatch(r"[\d,.\s]+", old) and not _ITEM_CODE_RE.match(old):
        words = words + " " + old            # a continuation: '(18")'
    elif old and not code and _ITEM_CODE_RE.match(old) and re.search(r"[A-Za-z]", old):
        code = old                            # a lettered code ('P05'); a bare number may be the quantity
    return code, words


def _set_description(r, desc):
    r["description"] = desc
    dn = norm_desc(desc)
    r["desc_norm"] = dn
    r["item_family"] = item_family(dn)
    r["size_in"] = extract_size_in(dn)
    m = MATERIAL_RE.search(dn)
    r["material"] = (m.group(1).upper() if m else "")


def _same_item_key(r):
    q = r.get("quantity")
    return (r.get("src_file"), (r.get("bidder_raw") or "").strip(), txt(r.get("item_no")).upper(),
            norm_unit(r.get("unit_raw") or ""), None if q is None else round(abs(q), 4))


def recover_code_only_descriptions(recs, stats=None):
    """Round 7 (.28): give a row whose description is only a code
    (desc_is_code_only) the words the page holds for it -- (a) its own item
    cell (_split_item_words), (b) the same bidder's same item (item number,
    unit, |quantity|) printed in full elsewhere in the file, when that is ONE
    description and it ends with the fragment (Beverly Dr's base-bid "P10 P2)"
    is the alternate's "P10 8" REINFORCED CONCRETE PAVEMENT ... (CL P1 OR
    P2)", -3,460 SY at the same $90).  Returns (rows from the item cell, rows
    from the same item)."""
    n_item = n_same = 0
    for r in recs:
        if not desc_is_code_only(r.get("description")):
            continue
        got = _split_item_words(r.get("item_no"), r.get("description"))
        if got is None:
            continue
        # (round 11) the unit and quantity _split_item_words drops from the words ('#1-1 Project Sign
        # 15813 EA 1'): units_at_end_of_description reads them for a row with no unit
        if not (r.get("unit_raw") or "").strip():
            s_ = txt(r.get("item_no"))
            m_ = _ITEM_WORDS_RE.match(s_)
            t_ = _TRAILING_UNIT_QTY_RE.search(m_.group(2) if m_ else s_)
            if t_:
                r["_desc_tail"] = t_.group(0)
        r["item_no"] = got[0]
        _set_description(r, got[1])
        n_item += 1
    full = {}
    for r in recs:
        if txt(r.get("item_no")) and not desc_is_code_only(r.get("description")):
            full.setdefault(_same_item_key(r), set()).add(r["description"])
    for r in recs:
        if not txt(r.get("item_no")) or not desc_is_code_only(r.get("description")):
            continue
        cands = full.get(_same_item_key(r))
        if not cands or len(cands) != 1:
            continue
        cand = next(iter(cands))
        frag = norm_desc(r.get("description"))
        if txt(r.get("description")).upper() == txt(r.get("item_no")).upper():
            frag = ""                         # the item number again: no tail to check
        if frag and not norm_desc(cand).endswith(frag):
            continue
        _set_description(r, cand)
        n_same += 1
    if stats is not None and (n_item or n_same):
        stats["code_only_description_recovered"] = {"from_item_cell": n_item, "from_same_item": n_same}
    return n_item, n_same


# ---- the WIDTH of a sidewalk or trail bought by the foot (round 7, .28) ------
# A sidewalk or trail priced per LF is priced by its width: Azle's "6' Concrete
# Sidewalk" (2023, 1,685 LF, bid $45/LF) was priced at $7.90 from sidewalk|LF,
# an unsized cell of 4-5 ft walks, sidewalk lugs and footings ($14-17/LF).  For
# an LF line of the sidewalk family (sidewalk, trail) size_in is the WIDTH IN
# FEET -- like box_culvert's opening area in square feet, a sidewalk|LF cell is
# only ever compared with other sidewalk|LF cells -- so build_master.size_bucket
# splits 4-5 ft (0-6), 6-8 ft (6-9), 10-12 ft (9-13) and 14-18 ft (13-19).  An
# LF sidewalk line that prints no width is unsized: its "4 in" is a thickness,
# which sets no price per foot.  Read from the description AS PRINTED, because
# norm_desc drops the foot mark ("6' Concrete Sidewalk" -> "6 concrete
# sidewalk").  SY / SF lines keep their thickness.
_FT = r"(?:'|\u2019|\u2032|ft\b\.?|foot\b|feet\b)"
_WIDTH_RES = (
    re.compile(r"(?<![\d./x])(\d{1,2}(?:\.\d)?)\s*-?\s*" + _FT + r"\s*-?\s*(?:in\s+)?(?:wide|width)", re.I),
    re.compile(r"(?:\b|(?<=[a-z]))width\s*(?:of\s*|=\s*|:\s*)?(\d{1,2}(?:\.\d)?)\s*" + _FT, re.I),
    re.compile(r"(?<![\d./x])(\d{1,2}(?:\.\d)?)\s*-?\s*" + _FT + r"\s*-?\s*"
               r"(?:(?!(?:tall|high|deep|depth|long|length|height|radius|dia\w*|thick\w*)\b)[a-z&/\-]+\s+){0,3}?"
               r"(?:sidewalks?|trails?|walks?|walkways?|paths?)\b", re.I),
)
_WIDTH_IN_RE = re.compile(r"(?<![\d./x])(\d{2,3})\s*-?\s*(?:\"|\u201d|in\b\.?|inch(?:es)?\b)\s*-?\s*(?:wide|width)", re.I)
WIDTH_FT_MIN, WIDTH_FT_MAX = 3.0, 20.0
WIDTH_LF_FAMILIES = {"sidewalk"}
# A boardwalk, pier or bridge on a trail is a structure at $700-1,500/LF
# (Celina Ousley Park's "Trail - Boardwalk - Metal Grating 8 ft Width"), not a
# walk of that width: it stays unsized rather than join the 6-9 ft walks.
_WIDTH_NOT_WALK_RE = re.compile(r"\b(?:boardwalks?|piers?|bridges?|gratings?|decks?)\b", re.I)


def width_ft(description):
    """The width in feet a description prints ("6' wide", "5-foot-wide",
    "5-ft in width", "width 6ft", "6' Concrete Sidewalk", '60" wide'), or
    None; 3-20 ft only."""
    s = txt(description)
    for rx in _WIDTH_RES:
        m = rx.search(s)
        if m:
            v = float(m.group(1))
            if WIDTH_FT_MIN <= v <= WIDTH_FT_MAX:
                return v
    m = _WIDTH_IN_RE.search(s)
    if m:
        v = float(m.group(1)) / 12.0
        if WIDTH_FT_MIN <= v <= WIDTH_FT_MAX:
            return round(v, 2)
    return None


def lf_width_size(r):
    """size_in of an LF sidewalk / trail row: its width in feet, or None."""
    if (r.get("unit") or "") == "LF" and r.get("item_family") in WIDTH_LF_FAMILIES:
        if _WIDTH_NOT_WALK_RE.search(txt(r.get("description"))):
            return None
        return width_ft(r.get("description"))
    return r.get("size_in")


def unit_cell_repeats_quantity(r):
    """True for a row whose UNIT cell prints the quantity again ('58 | 58')
    and whose arithmetic proves it: quantity x unit price = the printed
    extension.  Mesquite 24-7401 (2024) prints items 37-43 that way, and as
    unit_suspect rows they left every total -- ANA's low bid read $9,287,190
    against the $9,991,724 printed and awarded (tail audit 2026-11-20).  Such a
    row keeps its money in the totals and loses only its unit (main()), like
    clear_lump_sum_with_quantity.  A numeric unit cell that is NOT the quantity
    is a shifted column and stays suspect."""
    raw = (r.get("unit_raw") or "").strip()
    if not raw or not NUMERIC_UNIT_RE.match(raw):
        return False
    try:
        n = float(raw.replace(",", ""))
    except ValueError:
        return False
    q, up, ext = r.get("quantity"), r.get("unit_price"), r.get("extended_reported")
    if q is None or up is None or ext is None or r.get("ext_mismatch") == "Y":
        return False
    if abs(n - q) > 1e-6 * max(1.0, abs(q)):
        return False
    return abs(q * up - ext) <= max(0.01, 0.005 * abs(ext))


# ---- an item EVERY bidder's printed grand total leaves out -----------------
# The document's own total formula decides what the base bid is.  Southlake's
# TW King pump station (2026) prints 'C Total Base Bid with Extra Work Items
# (Sum of A and B-01)': B-02, a 1250 kW generator, is priced by every bidder
# and left out of the total the city awarded on, and read as base it put
# $1.03M-$1.52M on every bidder (Taknek $9,501,245 against the $8,471,944
# printed; the 3rd and 4th bidders swapped; tail audit 2026-11-20).  Proved,
# not guessed, per FILE: no bidder's base total is on the document's TOTAL
# lines (total_line_amounts), and there is exactly ONE item whose removal puts
# every bidder's total there.  That item's rows take the role 'other'.  Not per
# sheet: a page that holds an alternate block and one base line can, less that
# line, equal the alternate's printed total for every bidder (Richardson's
# Jupiter Rd bridge, 2021, would have lost its $25,000 contingency that way).
def items_outside_printed_total(recs, doc_facts):
    """{(src_file, item_no, desc_norm)} of base items outside the printed
    grand total of every bidder in the file."""
    by_file = defaultdict(list)
    for r in recs:
        if r.get("section_role") == "base" and not r.get("is_engineer_estimate"):
            by_file[r["src_file"]].append(r)
    out = set()
    for f, rs in by_file.items():
        tl = set((doc_facts.get(f) or {}).get("total_lines") or ())
        if len(tl) < 2:
            continue
        tot = defaultdict(float)
        per = defaultdict(lambda: defaultdict(float))
        for r in rs:
            e = r.get("extended_reported")
            if e is None and r.get("unit_price") is not None:
                e = r["unit_price"] * (r["quantity"] if r.get("quantity") is not None else 1.0)
            if e is None:
                continue
            b = r.get("bidder_raw")
            tot[b] += e
            per[(r.get("item_no") or "", r.get("desc_norm") or "")][b] += e
        bidders = [b for b in tot if tot[b] > 0]
        if len(bidders) < 2:
            continue

        def printed(v):
            c = int(round(v * 100))
            return c > 0 and any(c + d in tl for d in (-1, 0, 1))
        if any(printed(tot[b]) for b in bidders):
            continue
        hits = [k for k, pb in per.items()
                if all(pb.get(b, 0) > 0 and printed(tot[b] - pb[b]) for b in bidders)]
        if len(hits) == 1:
            out.add((f,) + hits[0])
    return out


def clear_lump_sum_with_quantity(r):
    """A row with unit LS and a quantity above one that is not already flagged
    ext_mismatch loses the unit LS (unit_note 'lump_sum_with_quantity', see
    main()).  It is NOT marked unit_suspect: its quantity x price is the money
    the bidder bid, so it stays in the letting's totals; only its unit is
    unknown, and a row without a unit is left out of every unit-price table.
    Returns True when it did."""
    q = r.get("quantity")
    if r.get("unit") == "LS" and q is not None and q > 1 and r.get("ext_mismatch") != "Y":
        r["unit"] = ""
        r["unit_note"] = "lump_sum_with_quantity"
        return True
    return False


# ---- which files the site-level reader is for --------------------------------
# One company tab (test data only) prints Site x Bidder totals and no line
# items, so it has its own reader.  main() used to call it for every input
# whose name merely CONTAINED one word of that file's name and kept only the
# last result: a public tab with the same word in a road name sorted after the
# company file, read 0 site rows and wiped the rows already read -- and
# site_totals.csv, written only when rows exist, went stale without a word.
# The reader now runs for exactly the files named in the private local
# configuration (src/local_config.py, key "site_level_files"), top-level only,
# and its rows are collected per file, never overwritten.  A public checkout
# names no such file.
SITE_LEVEL_FILES = frozenset(local_config.get("site_level_files", []))


def site_level_inputs(files, names=None):
    """The inputs the site-level reader runs on: exact top-level names only."""
    names = SITE_LEVEL_FILES if names is None else names
    return [f for f in files if f in names]


# ---- a file whose bidder NAMES are mostly garbage --------------------------
# A text layer made by bad OCR reads every cell, names included, as noise:
# Richardson 02-22 Custer Road ('AXIS (Qml\ACTJNG l~C', "DOM Ccmn'RUCT10fll
# COftPOAATIO!ol", "MiUltOSW.tU;'(ltA r..SONS DC") parsed to 755 rows with
# bidder totals of $0.7-2,900 billion, 470 of them in the panel's price pool.
# When the names are noise the numbers beside them are too.  A name TOKEN is
# garbled when it holds a character no firm name carries (\ ~ ; ! | { } < > ^),
# letters on both sides of digits ('RUCT10fll') or digits on both sides of
# letters ('1x9c'), or flips from lower to upper case twice inside the word
# ('MiUltOSW'; 'McMahon' flips once).  A NAME is garbled when at least a third
# of its tokens are.  A FILE whose bidder columns are garbled two times in
# three or more (at least two columns) is dropped whole and listed; on the
# 2026-11-19 corpus that is the Custer Road tab alone (one other garbled name,
# 'Au,nm C4notructTon, LLC', sits among clean ones and stays).
_GARBLE_CHAR_RE = re.compile(r"[\\~;!|{}<>^]")
_GARBLE_MIX_RE = re.compile(r"[A-Za-z]\d+[A-Za-z]|\d[A-Za-z]+\d")


def _garbled_token(t):
    if _GARBLE_CHAR_RE.search(t):
        return True
    core = t.strip("()[]\"'.,:-")
    if _GARBLE_MIX_RE.search(core):
        return True
    return sum(1 for p, q in zip(core, core[1:]) if p.islower() and q.isupper()) >= 2


def garbled_name(name):
    toks = (name or "").split()
    g = sum(1 for t in toks if _garbled_token(t))
    return bool(toks) and g >= 1 and 3 * g >= len(toks)


def garbled_files(recs):
    """{src_file: (garbled columns, columns)} for files whose non-estimate
    bidder columns are garbled two times in three or more (2+ columns)."""
    cols = defaultdict(set)
    for r in recs:
        b = r.get("bidder_raw")
        # judged over the columns main() would keep as bids: not an estimate,
        # a computed or derived column, or a placeholder (the Custer Road tab's
        # fifth column is its 'AVERAGES', which made 3 of 5 instead of 3 of 4)
        if (b and not r.get("is_engineer_estimate") and not r.get("computed_by_value")
                and not (COMPUTED_COL_RE.match(b) or DERIVED_COL_RE.match(b)
                         or PLACEHOLDER_RE.match(b))):
            cols[r["src_file"]].add(b)
    out = {}
    for f, names in cols.items():
        g = sum(1 for n in names if garbled_name(n))
        if len(names) >= 2 and 3 * g >= 2 * len(names):
            out[f] = (g, len(names))
    return out


def identical_file_groups(files, path_of):
    """Lists of two or more of `files` whose bytes are identical (md5)."""
    import hashlib
    by_hash = defaultdict(list)
    for f in sorted(files):
        try:
            with open(path_of[f], "rb") as fh:
                by_hash[hashlib.md5(fh.read()).hexdigest()].append(f)
        except (OSError, KeyError):
            continue
    return [g for g in by_hash.values() if len(g) >= 2]


# ---- an estimate column reprinted beside a later page's bidders ------------
# A tab too wide for one page prints its last bidders on a second page and
# repeats the engineer's estimate beside them.  main()'s sheet dedup compares
# (bidder, unit, quantity, price), so a reprint under the same caption goes;
# Cedar Hill's Parkerville EST Overflow page 2 overprints 'Roadway Solutions,
# Inc.' (no bid) onto its 'Engineer's Estimate' caption, reads "Engineer's
# EstimRaotaed way", and its 24 estimate rows stayed as a second estimate -- a
# letting of their own with no bidder.  A sheet of nothing but estimate rows
# whose rows (description, unit, quantity, unit price) the estimate column of
# another sheet of the same file -- one with bidders -- already holds is that
# column reprinted, whatever its caption reads.
def reprinted_estimate_sheets(by_sheet):
    """{sheet key: key of the sheet whose estimate column it repeats}."""
    def esig(rows):
        return {(norm_desc(r.get("description") or ""), r.get("unit") or "",
                 None if r.get("quantity") is None else round(float(r["quantity"]), 4),
                 round(float(r["unit_price"]), 2))
                for r in rows if r.get("is_engineer_estimate") and r.get("unit_price") is not None}
    est = {k: esig(v) for k, v in by_sheet.items()}
    out = {}
    for k in sorted(by_sheet):
        if len(est[k]) < 3 or not all(r.get("is_engineer_estimate") for r in by_sheet[k]):
            continue
        for k2 in sorted(by_sheet):
            if (k2 != k and k2[0] == k[0] and est[k] <= est[k2]
                    and any(not r.get("is_engineer_estimate") for r in by_sheet[k2])):
                out[k] = k2
                break
    return out


_STRIP_CASES = [
    # Measured on the Kimley-Horn template (North Richland Hills, Coppell,
    # Keller, Mansfield, Farmers Branch).  The merged header cell glues the
    # company name to its street, city, state and phone with SINGLE spaces, so
    # strip_address could not split it, judged the whole string as an address
    # and returned "" -- which made looks_like_bidder reject the name row and
    # walk up to the 'BIDDER 1' column labels instead.  2,714 rows were then
    # dropped as placeholder bidders.
    ("DDM Construction Corp. 4006 Belt Line Road, Ste. 230 Addison, TX 75001 "
     "972-322-2021", "DDM Construction Corp."),
    ("Jackson Construction, Ltd. 5112 Sun Valley Dr. Fort Worth, TX 76119 "
     "817-572-3303", "Jackson Construction, Ltd."),
    ("McClendon Construction Co., Inc. P.O. Box 999 Burleson, TX 76097 "
     "817-295-0066", "McClendon Construction Co., Inc."),
    ("McMahon Contracting, LP 3019 Roy Orr Blvd. Grand Prairie, TX 75050 "
     "972-263-6907", "McMahon Contracting, LP"),
    ("Quality Excavation, LLC 5580 US Hwy 377 Aubrey, TX 76227 940-365-0800",
     "Quality Excavation, LLC"),
    ("XIT Paving and Construction, Inc. 3934 S. Hwy 287 Waxahachie, TX 75165 "
     "972-351-9906", "XIT Paving and Construction, Inc."),
    # The forms that already worked must keep working: this path only runs when
    # the splittable path yields nothing.
    ("ACME Paving, Inc.\n1234 Main St\nDallas, TX 75201", "ACME Paving, Inc."),
    ("ACME Paving, Inc.  1234 Main St  Dallas, TX 75201", "ACME Paving, Inc."),
    ("ACME Paving, Inc.", "ACME Paving, Inc."),
    # Cells that are ONLY an address, or the letting authority, must still come
    # back empty -- otherwise an address becomes a bidder.
    ("1234 Main St, Dallas, TX 75201", ""),
    ("P.O. Box 999 Burleson, TX 76097", ""),
    ("City of North Richland Hills", ""),
]


def _selftest():
    bad = 0
    for raw, want in _STRIP_CASES:
        got = strip_address(raw)
        if got != want:
            bad += 1
            print("FAIL strip_address\n  in   %r\n  got  %r\n  want %r"
                  % (raw, got, want))
    print("strip_address: %d cases, %d failures" % (len(_STRIP_CASES), bad))
    return ((bad == 0) & _computed_column_selftest() & _parse_rules_selftest()
            & _r3c_selftest() & _scope_rules_selftest() & _r3_packet_selftest()
            & _family_rules_selftest() & _audit_fixes_selftest() & _diag_fixes_selftest()
            & _ed_fixes_selftest() & _units_codes_selftest() & _round8_selftest()
            & _round9_selftest() & _round10_selftest() & _round11_selftest() & _round12_selftest()
            & _round20_selftest())


def _round20_selftest():
    """Round 20 addendum 1 (.34b; P2-P4 of .34): lines the family rules left `other` (or filed with the wrong product) and so priced from a
    mixture cell -- screening walls, flashing beacons and signal hardware, markings named by their line, signs named
    by their code or panel -- and walls that are not sized by their height (reports/p50_fix_round20.md).  Each rule
    with the look-alikes it must leave alone."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    fam = lambda s: item_family(norm_desc(s))
    size = lambda s: extract_size_in(norm_desc(s))
    # .34b (addendum 1): no screening_wall family and no wall-size rule -- these stay as in .33
    for s, w in (("Install 6 foot tall screening wall (RhinoRock)", "other"),
                 ("6' Masonry Screen Wall", "masonry_fascia"),
                 ("SOUND WALL", "other"),
                 ("Screening wall entry signs", "other")):
        check("screening wall stays: %s" % s, fam(s), w)
    for s, w in (("Furnish & Install Solar Powered Crosswalk Lights", "traffic_signal"),
                 ("Install Solar Powered Beacon Assembly (Install Only)", "traffic_signal"),
                 ("Roadside Flashing Beacon Assembly (Solar) and Foundation", "traffic_signal"),
                 ("Crosswalk Warning Flasher (Single Sided)", "traffic_signal"),
                 ("Remove and replace school zone flasher incl. foundation", "traffic_signal"),
                 ("Install new school zone flasher pole & school zone signs", "traffic_signal"),
                 ("Pedestrian Hybrid Beacons", "traffic_signal"),
                 ("INSTALL OF FLASHER CABINET", "traffic_signal"),
                 ("In-Roadway Crosswalk Lighting System", "traffic_signal"),
                 ("3441-1615 Furnish/Install Type 45 Signal Pole", "traffic_signal"),
                 ("3441-1625 Furnish/Install Mast Arm 52'-60'", "traffic_signal"),
                 ("INSTALL RDSD FLASH BEACON ASSEMBLY", "traffic_signal"),
                 ("Remove/Replace the Beacon Entry Monument", "other"),
                 ("Relocate existing school flasher", "relocate_reset"),
                 ("Remove salvage solar powered flashing beacon", "relocate_reset"),
                 ("REMOVE RDSD FLASH BEACON ASSEMBLY", "demo_other"),
                 ("3441-1705 Ty 5 Signal Foundation", "other"),
                 ("Beacon Hill Rd 8\" water line", "water_pipe"),
                 ("INSTALL OF LUMINAIRE MAST ARMS", "electrical_lighting"),
                 ("Install second luminaire arm fixture on existing signal pole", "electrical_lighting"),
                 ("Mast arm signs (new)", "other"),
                 ("Signal pole mast arm 36 ft (lum, ILSN)", "traffic_signal")):
        check("beacon / signal: %s" % s, fam(s), w)
    for s, w in (("24\" White Stop Bar", "pavement_marking"),
                 ("White Stop Bar", "pavement_marking"),
                 ("9999-0105 6\" Double Yellow", "pavement_marking"),
                 ("9999-0105 6\" Yellow Solid", "pavement_marking"),
                 ("4\" White Broken", "pavement_marking"),
                 ("Red 10\" wide continuous solid fire lane line with 4\" lettering", "pavement_marking"),
                 ("White 6\" wide continuous solid line (parking space paint)", "pavement_marking"),
                 ("4\" Refl Raised Marker Ty II-C-R", "pavement_marking"),
                 ("24\" wide x 6' White Thermo Intersection Crosswalk", "pavement_marking"),
                 ("High Visibility Crosswalk", "pavement_marking"),
                 ("Stamped colored concrete crosswalk", "concrete_misc"),
                 ("Crosswalk signal", "traffic_signal"),
                 ("Solid block sod", "seeding_sod"),
                 ("Double wall HDPE pipe 4\"", "other"),
                 ("Double head lights", "other")):
        check("marking: %s" % s, fam(s), w)
    for s, w in (("Yield Sign R1-2 36\"x36\"", "signing"),
                 ("Keep Right R4-7 30\"x30\"", "signing"),
                 ("Two Way Traffic CW6-3 30\" x 30\"", "signing"),
                 ("Right lane must turn right sign R3-7R 30\" x 30\"", "signing"),
                 ("Traffic Sign", "signing"),
                 ("Street name sign", "signing"),
                 ("Traffic signal", "traffic_signal"),
                 ("Project sign", "project_sign"),
                 ("Remove sign", "demo_structure"),
                 ("Relocate sign", "relocate_reset"),
                 ("Barricades, signs and traffic handling", "traffic_control"),
                 ("Kiosk signage at trailheads", "project_sign"),
                 ("Illuminated street name sign (ILSN)", "electrical_lighting"),
                 ("Solar powered LED warning sign", "other"),
                 ("Trail flood warning sign", "signing")):
        check("sign: %s" % s, fam(s), w)
    for s, w in (("TxDOT Retaining Wall (24\" Average Height)", 24.0),
                 ("24\" RCP", 24.0),
                 ("6\" Concrete Sidewalk", 6.0)):
        check("size as in .33: %s" % s, size(s), w)
    print("round 20 addendum (.34b) self-test: %d checks, %d failures" % (n[0], len(fails)))
    for f in fails:
        print("  FAIL " + f)
    return not fails


def _round12_selftest():
    """Round 12 (.33): the place does not decide the family -- earthwork and planting worded with a trail or
    sidewalk, sidewalk / trail walls, trail boardwalks and fishing piers, TRAIL meaning trailer -- and
    abandonment of an existing pipe, sewer, water line or service is demo_utility, not new pipe
    (reports/parser_round12.md).  Each rule with the look-alikes it must leave alone."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    fam = lambda s: item_family(norm_desc(s))
    # ---- earthwork worded with a trail / sidewalk
    for s, w in (("Excavation (Trail)", "excavation"),
                 ("Unclassified Excavation (Trail)", "excavation"),
                 ("Unclassified Trail Excavation", "excavation"),
                 ("Trail Grading (Excavation and finish grading)", "excavation"),
                 ("Excavate clean soil for 12 foot trail", "excavation"),
                 ("TRAIL EARTHWORK", "excavation"),
                 ("EMBANKMENT (FINAL)(DENS CONT)(TY C) (TRAIL)", "embankment_fill"),
                 ("EMBANK (FNL)(DC)(TY C) TRAIL", "embankment_fill"),
                 ("Borrow for trail", "embankment_fill"),
                 ("Sidewalk Excavation", "excavation"),
                 ("Fine grading for trail", "grading"),
                 ("EARTHWORK (NET CUT)", "excavation"),
                 ("Earthwork", "excavation"),
                 ("Cut and fill earthwork", "embankment_fill"),
                 ("Earthwork - Site Grading", "grading")):
        check("earthwork: %s" % s, fam(s), w)
    # ---- a sidewalk / trail that names earthwork or planting AND a surface is still the sidewalk
    for s in ("Excavate soil and install new Class A concrete sidewalk, width 6 ft at minimum depth of four inches",
              "4\" Reinforced Concrete Sidewalk, including excavation and backfill",
              "Reinforced concrete sidewalk all thickness including necessary unclassified excavation",
              "12' Concrete Trail", "Construct 12' trail", "Sidewalk w/ retaining wall fill",
              "Concrete sidewalk remove and replace 8' wide to include up to 2\" of fill complete in place",
              "SIDEWALK CONSTRUCTION AND REPAIR: 0 to 500 Square feet, 4 inch thick",
              "Trail Stone Seat Wall, Lueders Limestone Block", "CONCRETE SIDEWALK WITH WALL",
              "Concrete Sidewalk with Retaining Wall"):
        check("still a sidewalk: %s" % s, fam(s), "sidewalk")
    # ---- planting worded with a trail / sidewalk
    for s, w in (("Planting Bed Prep 2\" Compost (Non Trail Areas)", "seeding_sod"),
                 ("SIDEWALK CONSTRUCTION AND REPAIR: Bermuda grass - price to include 4 inch Topsoil", "seeding_sod"),
                 ("SIDEWALK CONSTRUCTION AND REPAIR - Zoysia Grass - price to include 4 inch Topsoil", "seeding_sod"),
                 ("Trail Sod", "seeding_sod"),
                 ("Furnish and install irrigation along trail", "irrigation")):
        check("planting: %s" % s, fam(s), w)
    # ---- a sidewalk / trail WALL
    for s, w in (("CONCRETE SIDEWALK WALL", "retaining_wall"),
                 ("Construct 6\" Thick Sidewalk Wall (Height Varies) (w/ 2\" form liner)", "retaining_wall"),
                 ("TxDOT TY C2 Sidewalk Wall w/ Form Liner (MCPSWMD-23)", "retaining_wall"),
                 ("Concrete Trail Wall (Class F) (4,000 PSI), furnish and install complete in place.", "retaining_wall"),
                 ("Construct sidewalk retaining (variable height) up to 36 inches", "retaining_wall"),
                 ("Remove & Replace Exist Sidewalk Wall (Approx. 3' tall)", "retaining_wall"),
                 ("Decorative Stone Veneer Sidewalk Wall 2\"", "masonry_fascia"),
                 ("6\" Sidewalk Curb Wall", "curb_gutter"),
                 ("Remove existing sidewalk wall", "demo_sidewalk")):
        check("wall: %s" % s, fam(s), w)
    # ---- structures and trailers
    for s, w in (("Trail Boardwalk Metal Grating 8 ft width", "other"),
                 ("Trail Fishing Pier Metal Grating 15' Fishing Area", "other"),
                 ("Prefabricated Pedestrian Bridge", "other"),
                 ("TRAIL MNT SOL PRD RAD SPEED CNT MONITOR", "other"),
                 ("ST235/80R16 LRE CAR RAD TRAIL HD", "other")):
        check("structure / trailer: %s" % s, fam(s), w)
    # ---- abandonment of an existing utility
    for s in ("8\" Sanitary Sewer Abandonment Plug",
              "Cut, Plug & Abandon Existing Sanitary Sewer (Size Varies)",
              "Cut & Plug Ex. Sanitary Sewer", "Plug Existing 6\" Sanitary Sewer (Site 2)",
              "Grout Existing 8\" PVC Sanitary Sewer Main", "PLUG AND ABANDON SAN SEWER 6IN",
              "Plug Existing Storm Drain", "Cut and Plug Existing 24\" RCP", "Cut & Plug Ex. 12\" W.L.",
              "Cut & Plug Ex. 6\" S.S.", "16\" WATER ABANDONMENT PLUG", "2\"-12\" Abandonment Plug",
              "Water Service Abandonment", "Cut and Plug Existing PVC Sanitary Sewer Service",
              "Cap and plug existing 6\" sewer service, fully grout",
              "Plug existing water service at saddle on 36\" water main", "Cut and plug existing 8\"",
              "F&I Flowable Fill in Existing 6\" Force Main", "ABONDON/REMOVE EXISTING 8\" WASTEWATER",
              "Grout existing junction box", "Abandon existing 8\" water line in place"):
        check("abandonment: %s" % s, fam(s), "demo_utility")
    # ---- plugs and pipe lines that are not abandonment
    for s, w in (("Furnish and Install 42\" RCP Plug", "storm_pipe"),
                 ("36\" RCP Plug for Future Connection", "storm_pipe"),
                 ("Plug RCP for future connection (all sizes)", "storm_pipe"),
                 ("12\" Plug", "other"), ("48\" PRESSURE PLUG", "other"),
                 ("8\" Plug Valve", "valve_hydrant"),
                 ("Install 1\" Domestic Water Service (Poly) by Open Cut, Abandon Ex.", "water_service"),
                 ("Temporary 2\" PVC water service including abandonment/removal", "water_service"),
                 ("Install 60\" Plug in Existing Junction Box Complete in Place", "inlet_structure"),
                 ("Coreopsis tinctoria (Dwarf Red Plains Coreopsis) plug", "other"),
                 ("8\" PVC SDR-26 Sanitary Sewer", "sanitary_pipe"),
                 ("12\" PVC sewer open cut with modified flowable fill as backfill", "sanitary_pipe"),
                 ("18\" dia PVC carrier sanitary sewer pipe with annular grout", "sanitary_pipe"),
                 ("8\" Cleanout with Plug", "sewer_service")):
        check("not abandonment: %s" % s, fam(s), w)
    check("grouting the joints of an existing pipe is no abandonment",
          fam("Grout existing pipe joints") != "demo_utility", True)

    for f in fails:
        print("FAIL round-12 rule: " + f)
    print("round-12 rules (.33): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _round11_selftest():
    """Round 11 (.32): units the documents print but the parse did not read, location words before
    'culvert', a drainage conveyance's cleanout, and three small letting defects (reports/
    parser_round11.md).  Synthetic grids and rows shaped like the tabs that needed each rule, with the
    look-alikes each rule must leave alone."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    E = ""
    # ---- the unit caption spellings
    check("U/M, Measure, Units of Measure are unit captions",
          [detect_meta_cols(h, 4)["unit"] for h in (
              ["ITEM #", "DESCRIPTION", "U/M", "QTY", "UNIT $", "TOTAL $"],
              ["Item", "Description", "Quantity", "Measure", "Unit Cost", "Total"],
              ["Item", "Description", "Units of Measure", "Qty", "Unit Price", "Total"],
              ["Item", "Description", "UOM", "Qty", "Unit Price", "Total"])],
          [2, 3, 2, 2])
    check("a header without one names no unit column",
          detect_meta_cols(["Item", "Description", "Quantity", "Spec", "Unit Price", "Total"], 4)["unit"], None)

    # ---- a unit column the header does not name, read by its cells
    g = [[E, E, E, E, "Acme Paving", E, "Beta Builders", E],
         ["Item No. Item Description Quantity Unit", E, E, E, "Unit Price", "Item Cost", "Unit Price", "Item Cost"],
         ["P01", "MOBILIZATION", "1", "LS", "$10,000.00", "$10,000.00", "$12,000.00", "$12,000.00"],
         ["P02", "SEDIMENT CONTROL FENCE", "4,400", "LF", "$4.79", "$21,076.00", "$5.00", "$22,000.00"],
         ["P03", "TOPSOIL (4\")", "1,584", "SY", "$9.15", "$14,493.60", "$5.00", "$7,920.00"],
         ["P04", "BLOCK SOD", "1,584", "SY", "$17.96", "$28,448.64", "$8.00", "$12,672.00"],
         ["P05", "INLET PROTECTION", "12", "EA", "$150.00", "$1,800.00", "$100.00", "$1,200.00"],
         ["P06", "TREE PROTECTION", "1", "SEE NOTE", "$900.00", "$900.00", "$800.00", "$800.00"]]
    r, _a = rows_from_grid(g, "t.pdf", "p1t0", {})
    check("merged caption: the units read from their column, a non-unit cell left blank",
          [(x["description"], x["unit_raw"]) for x in r if x["bidder_raw"] == "Acme Paving"],
          [("MOBILIZATION", "LS"), ("SEDIMENT CONTROL FENCE", "LF"), ('TOPSOIL (4")', "SY"), ("BLOCK SOD", "SY"),
           ("INLET PROTECTION", "EA"), ("TREE PROTECTION", "")])
    words = [list(row) for row in g]
    for row in words[2:]:
        row[3] = "see plan"
    r, _a = rows_from_grid(words, "t.pdf", "p1t0", {})
    check("a column of words is no unit column", sorted({x["unit_raw"] for x in r}), [""])
    two = [list(row) for row in g[:4]]
    r, _a = rows_from_grid(two, "t.pdf", "p1t0", {})
    check("two rows prove no column", sorted({x["unit_raw"] for x in r}), [""])

    # ---- a quantity cell that carries its unit
    check("quantity cells with their unit",
          [split_quantity_unit(s) for s in ("154.83 AC", "1 LS", "124,600 CY", "12 BOX", "3 lot", "LS", "1,000")],
          [(154.83, "AC"), (1.0, "LS"), (124600.0, "CY"), None, None, None, None])

    # ---- the unit (and quantity) at the end of the description
    check("description tails", [_tail_unit(s) for s in (
        "MOBILIZATION (5% MAX) 017113 1 LS", "Mobilization LS 1", "Construction Staking LS",
        "TEMP FENCE (6' TALL) FURNISH/INSTALL/REMOVE)1 LS", "Helmet Hut, Black equal 12 each",
        "Total Labor Cost Incorporated in Project 1 lump sum", "Asphalt pavement per SY",
        "Chain link fence 6 FT", "Lumber 2 x 4 LF", "Remove junction box", "Paving for parking lot",
        "1 LS", "Sprinkler head", '24" STORM DRAIN x 262 LF')],
          [("MOBILIZATION (5% MAX) 017113", 1.0, "LS"), ("Mobilization", 1.0, "LS"),
           ("Construction Staking", None, "LS"), ("TEMP FENCE (6' TALL) FURNISH/INSTALL/REMOVE)", 1.0, "LS"),
           ("Helmet Hut, Black equal", 12.0, "each"), ("Total Labor Cost Incorporated in Project", 1.0, "lump sum"),
           None, None, None, None, None, None, None, ('24" STORM DRAIN x', 262.0, "LF")])

    def row(desc, q, up, ext, bidder="A", sheet="coords_p1", item="1"):
        return {"src_file": "t.pdf", "src_sheet": sheet, "item_no": item, "description": desc,
                "desc_norm": norm_desc(desc), "unit_raw": "", "unit": "", "quantity": q, "unit_price": up,
                "extended_reported": ext, "bidder_raw": bidder}
    recs = [row("MOBILIZATION (5% MAX) 017113 1 LS", 1.0, 105655.45, 105655.45, item="1-1"),
            row("SILT FENCE 312500 2450 LF", None, 8.0, 19600.0, item="1-5"),
            row("SILT FENCE 312500 2450 LF", None, 7.5, 18375.0, bidder="B", item="1-5"),
            row("SILT FENCE 312500 2450 LF", None, 9.0, 99.0, bidder="C", item="1-5"),
            row("INLET PROTECTION 312500 12 EA", 12.0, 550.0, 6600.0, item="1-6"),
            row("PROJECT SIGN 015813 1 EA", 2.0, 1600.0, 3200.0, item="1-8"),
            row("CONCRETE PAVING", 100.0, 10.0, 1000.0, item="2-1")]
    got = units_at_end_of_description(recs)
    check("description tails on a sheet",
          (got, [(x["description"], x["unit"], x["quantity"], x.get("unit_note", "")) for x in recs]),
          (5, [("MOBILIZATION (5% MAX) 017113", "LS", 1.0, "unit_in_description"),
               ("SILT FENCE 312500", "LF", 2450.0, "unit_in_description"),
               ("SILT FENCE 312500", "LF", 2450.0, "unit_in_description"),
               ("SILT FENCE 312500", "LF", 2450.0, "unit_in_description"),
               ("INLET PROTECTION 312500", "EA", 12.0, "unit_in_description"),
               ("PROJECT SIGN 015813 1 EA", "", 2.0, ""),           # the number is not its quantity
               ("CONCRETE PAVING", "", 100.0, "")]))
    few = [row("MOBILIZATION 1 LS", 1.0, 5.0, 5.0, item="1"), row("SIGN 2 EA", 2.0, 5.0, 10.0, item="2"),
           row("PAVING", 9.0, 5.0, 45.0, item="3"), row("CURB", 9.0, 5.0, 45.0, item="4")]
    check("two tails on a sheet are no column", units_at_end_of_description(few), 0)

    # ---- a unit column stacked into one cell
    t = [["Bid Quantity", "Unit", "Description", "Acme", E],
         [None, None, None, "Unit Price", "Total Price"],
         [None, "LS\nLF\nEA", "Mobilization", "$1,000.00", "$1,000.00"],
         [None, None, "Storm pipe 18\"", "$50.00", "$5,000.00"],
         [None, None, "Headwall", "$2,000.00", "$4,000.00"],
         [None, None, None, "Tabulation Total", "$10,000.00"]]
    sp = spread_stacked_cells(t)
    check("a stacked unit column spread one unit a row", [sp[k][1] for k in (2, 3, 4, 5)], ["LS", "LF", "EA", None])
    t2 = [list(x) for x in t]
    t2[2][1] = "LS\nLF"
    check("a stack that does not count out stays", spread_stacked_cells(t2)[2][1], "LS\nLF")

    # ---- families: a location word before 'culvert'; a drainage conveyance's cleanout
    check("location words before culvert", [item_family(norm_desc(s)) for s in (
        '15" RCP for driveway culvert', 'CONSTRUCT 12" CMP DRIVEWAY CULVERT', 'Remove Driveway Culvert 18"',
        "REMOV STR (DRIVEWAY CULVERT)", 'Remove and replace 12" CMP driveway culvert with appurtenances',
        "Reset Existing Driveway Culvert")],
          ["storm_pipe", "storm_pipe", "demo_utility", "demo_utility", "storm_pipe", "relocate_reset"])
    check("a driveway is still a driveway", [item_family(norm_desc(s)) for s in (
        '6" Reinforced Concrete Driveway', "Remove concrete driveway", "Drive approach",
        "Driveway culvert headwall")], ["driveway", "demo_pavement", "driveway", "headwall"])
    check("cleanouts", [item_family(norm_desc(s)) for s in (
        "Roadside drainage ditch improvements and culvert cleanouts", "Channel clean-out",
        '4" Sanitary Sewer Service with Cleanout', "Sanitary sewer main cleanout", "sub drain cleanout",
        "Relocate existing sanitary sewer cleanout")],
          ["cleaning_desilting", "cleaning_desilting", "sewer_service", "sewer_service", "sewer_service",
           "relocate_reset"])

    # ---- a quantity glued to the number printed after it
    def grow(q, ups, exts, item="3"):
        return [{"src_file": "t.pdf", "src_sheet": "p1t0", "bid_section": "BASE BID", "item_no": item,
                 "description": "Place Block Sod", "unit_raw": "SY", "quantity": q, "unit_price": u,
                 "extended_reported": e, "bidder_raw": b} for b, u, e in zip("AB", ups, exts)]
    g1 = grow(177837.0, [15.0, 8.51], [26670.0, 15130.78])
    g2 = grow(115000.0, [50000.0, 70155.0], [50000.0, 70155.0], item="1")
    g3 = grow(12.0, [100.0, 90.0], [100.0, 90.0], item="2")             # 1 under a printed 12: left
    g4 = grow(500.0, [10.0, 12.0], [600.0, 600.0], item="4")             # columns disagree: left
    g5 = grow(6088391029136088.0, [140.0, 135.0], [852320.0, 821880.0], item="5")   # a packed column: left
    got = recover_glued_quantity_prefix(g1 + g2 + g3 + g4 + g5)
    check("a quantity glued to the next number",
          (got, [x["quantity"] for x in g1 + g2 + g3 + g4 + g5]),
          ((2, 4), [1778.0, 1778.0, 1.0, 1.0, 12.0, 12.0, 500.0, 500.0, 6088391029136088.0, 6088391029136088.0]))

    # ---- a numbered group heading that is a banner opens its section
    ng = [[E, E, E, E, "Acme Paving", E],
          ["Item No.", "Description", "Unit", "Qty", "Unit Price", "Total Cost"],
          ["10", "SIGNAGE", E, E, E, E],
          ["#10-1", "PARK SIGN", "EA", "2", "$500.00", "$1,000.00"],
          ["12", "ALT 1 Mobil Special 10W30", "QUART", "60", "No Bid", "No Bid"],
          ["17 ALT 1 Navi-Guard Premium Fleet 15W40 660 quart", E, E, E, "No Bid", "No Bid"],
          ["#10-2", "BENCH", "EA", "4", "$800.00", "$3,200.00"],
          ["11", "ALTERNATES", E, E, E, E],
          ["#11-1", "SHADE STRUCTURE", "EA", "1", "$9,000.00", "$9,000.00"]]
    r, _a = rows_from_grid(ng, "t.pdf", "p1t0", {})
    check("a numbered group heading opens its section, a numbered row with a quantity does not",
          [(x["item_no"], x["bid_section"]) for x in r],
          [("#10-1", "BASE BID"), ("#10-2", "BASE BID"), ("#11-1", "ALTERNATES")])

    # ---- a lone rank number continuing the file's series
    _RANK_SERIES.pop("rk.pdf", None)
    rg = [[E, E, E, E, "1 Matcon LLC", E, "2 Jeske Construction", E],
          ["ITEM", "DESCRIPTION", "UNIT", "QTY", "UNIT PRICE", "TOTAL", "UNIT PRICE", "TOTAL"]]
    one = [[E, E, E, E, "3 Capko Concrete Structures LLC", E],
           ["ITEM", "DESCRIPTION", "UNIT", "QTY", "UNIT PRICE", "TOTAL"]]
    star = [[E, E, E, E, "4 Star Electric", E], ["ITEM", "DESCRIPTION", "UNIT", "QTY", "UNIT PRICE", "TOTAL"]]
    a1 = bidder_names_for(rg, 1, [(4, 5), (6, 7)], rank_key="rk.pdf")
    a2 = bidder_names_for(one, 1, [(4, 5)], rank_key="rk.pdf")
    a3 = bidder_names_for(one, 1, [(4, 5)], rank_key="rk.pdf")          # read again: same answer
    a4 = bidder_names_for(star, 1, [(4, 5)], rank_key="other.pdf")      # no series in its file
    check("a lone rank number continues the file's series",
          [a1[4], a1[6], a2[4], a3[4], a4[4]],
          ["Matcon LLC", "Jeske Construction", "Capko Concrete Structures LLC", "Capko Concrete Structures LLC",
           "4 Star Electric"])
    _RANK_SERIES.pop("rk.pdf", None)

    for f in fails:
        print("FAIL round-11 rule: " + f)
    print("round-11 rules (.32): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _round9_selftest():
    """Round 9 (.30): letting labels (dates, on-call contracts, apparel) and the generic small-job line
    defects (reports/parser_round9.md)."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    def fam(d):
        return item_family(norm_desc(d))

    # ---- dates: month names in any case, abbreviated; weak cues; reconciliation with the file name
    check("capital month", _dates_in("BID OPENING: JULY 8, 2026"), ["2026-07-08"])
    check("capital month with weekday and time",
          _dates_in("BID OPENING 2:00 PM, THUSDAY, SEPTEMBER 2, 2021"), ["2021-09-02"])
    check("abbreviated month", _dates_in("Bids due Sept. 2, 2021"), ["2021-09-02"])
    check("three-letter month", _dates_in("OPENED: DEC 14, 2023"), ["2023-12-14"])
    check("ordinal day", _dates_in("Bid Opening June 6th, 2024"), ["2024-06-06"])
    check("no space after the comma", _dates_in("Thursday, May 19,2022 @ 11:00A.M."), ["2022-05-19"])
    check("title-case month unchanged", _dates_in("Meeting Date - September 1, 2026"), ["2026-09-01"])
    check("an item is no month", _dates_in("Item 12, 2024 Pay Items"), [])
    check("a word is no month", _dates_in("Section 3, 2025 Specs"), [])
    check("opening in capitals beats a later meeting date",
          find_dates(["Meeting Date - September 1, 2026", "BID OPENING: JULY 8, 2026"])[:2],
          ("2026-07-08", "bid_open_or_due"))
    check("certified on is a weak date", find_dates(["Certified on:   June 6, 2024"])[:2],
          ("2024-06-06", "weak_issue_or_received"))
    check("weekday line is a weak date", find_dates(["Thursday, May 19,2022 @ 11:00A.M. (CST)"])[:2],
          ("2022-05-19", "weak_issue_or_received"))
    check("clock time line is a weak date", find_dates(["2:00 PM on Monday, August 11, 2025"])[:2],
          ("2025-08-11", "weak_issue_or_received"))
    check("a print stamp is no date", find_dates(["Printed 6/19/2023 3:45 PM"])[:2], ("", "MISSING"))
    check("a page footer stamp is no date", find_dates(["Page 1 of 12 6/27/2025 4:59 PM"])[:2], ("", "MISSING"))
    check("a CAD plot stamp is no date",
          find_dates(["5/19/2022 12:05:04 PM ah3581 I:\\47000s\\47512\\CADD\\EXH-PLAN-47512.dgn"])[:2], ("", "MISSING"))
    check("a time with seconds is no opening time", find_dates(["5/19/2022 12:05:04 PM"])[:2], ("", "MISSING"))
    check("an odd-minute clock time is a print stamp", find_dates(["2/20/2020 11:18 AM"])[:2], ("", "MISSING"))
    check("a report-generated stamp is no date",
          find_dates(["Report Generated: Thursday, February 27, 2025"])[:2], ("", "MISSING"))
    check("a quarter-hour opening time is", find_dates(["Wednesday, August 13, 2025 1:30 p.m."])[:2],
          ("2025-08-13", "weak_issue_or_received"))
    check("a typo'd weekday still has its opening time",
          find_dates(["2:00PM (CST) THRURSDAY, OCTOBER 31, 2024"])[:2], ("2024-10-31", "weak_issue_or_received"))
    check("start date is still no bid date", find_dates(["Earliest Possible Start Date 11/2/2025"])[:2],
          ("", "MISSING"))
    rb = reconcile_bid_date
    check("January opening printed with last year",
          rb("2024-01-22", "weak_issue_or_received", ["2024-01-22"], "2025-02-17_25-7575_Bid_Tabulation.pdf"),
          ("2025-01-22", "weak_issue_or_received_yearfix"))
    check("year fix: a later date in the document keeps it",
          rb("2024-01-22", "weak_issue_or_received", ["2024-01-22", "2025-01-30"],
             "2025-02-17_25-7575_Bid_Tabulation.pdf"), ("2024-01-22", "weak_issue_or_received"))
    check("year fix: not a March opening",
          rb("2024-03-22", "bid_open_or_due", ["2024-03-22"], "2025-04-17_x.pdf"),
          ("2024-03-22", "bid_open_or_due"))
    check("year fix: one year later must land before the agenda",
          rb("2024-01-22", "bid_open_or_due", ["2024-01-22"], "2025-01-10_x.pdf"),
          ("2024-01-22", "bid_open_or_due"))
    check("year fix: never on a packet's ambiguous span",
          rb("2024-01-22", "bid_open_or_due_ambiguous_span", ["2024-01-22"], "2025-02-17_x.pdf"),
          ("2024-01-22", "bid_open_or_due_ambiguous_span"))
    check("year fix: an opening weeks before the agenda is untouched",
          rb("2025-01-22", "bid_open_or_due", ["2025-01-22"], "2025-02-17_x.pdf"),
          ("2025-01-22", "bid_open_or_due"))
    check("lone uncued date before a bid number's year",
          rb("", "MISSING", ["2024-06-06"], "2024-09_Street_Resurfacing.pdf"),
          ("2024-06-06", "doc_date_uncued_approx"))
    check("lone uncued date weeks before the agenda date",
          rb("", "MISSING", ["2022-05-19"], "2022-06-14_4324_Bid_Tab.pdf"),
          ("2022-05-19", "doc_date_uncued_approx"))
    check("uncued dates that disagree are not used",
          rb("", "MISSING", ["2022-01-03", "2022-05-19"], "2022-06-14_x.pdf"), ("", "MISSING"))
    check("an uncued date after the agenda date is not used",
          rb("", "MISSING", ["2022-07-19"], "2022-06-14_x.pdf"), ("", "MISSING"))
    check("an uncued date far from the bid number's year is not used",
          rb("", "MISSING", ["2025-05-01"], "2021-13_x.pdf"), ("", "MISSING"))
    check("no file-name date: a lone uncued date",
          rb("", "MISSING", ["2023-03-01"], "Bid_Tab.pdf"), ("2023-03-01", "doc_date_uncued_approx"))
    check("a weak date months after the file's date gives way to it",
          rb("2026-09-01", "weak_issue_or_received", ["2026-09-01"], "2026-07-08_x.pdf"), ("", "MISSING"))
    check("a weak date days after the file's date stays",
          rb("2025-05-23", "weak_issue_or_received", ["2025-05-23"], "2025-05-21_x.pdf"),
          ("2025-05-23", "weak_issue_or_received"))
    check("a cued opening after the file's date stays (addendum)",
          rb("2025-05-23", "bid_open_or_due", ["2025-05-23"], "2025-05-21_x.pdf"),
          ("2025-05-23", "bid_open_or_due"))
    check("uncued: a plot stamp is not the document's date",
          rb("", "MISSING", ["2022-06-01"], "2024_Engineering_SOQ.pdf",
             ["6/1/2022 5:55 PM 1 of 2 I:\\47000s\\47512\\EXH-PLAN.dgn"]), ("", "MISSING"))
    check("uncued: a lone plain date still is",
          rb("", "MISSING", ["2023-03-01"], "Bid_Tab.pdf", ["3/1/2023"]), ("2023-03-01", "doc_date_uncued_approx"))
    check("a cued date is never replaced by uncued ones",
          rb("2024-06-01", "bid_open_or_due", ["2024-06-01"], "2024-09_x.pdf"), ("2024-06-01", "bid_open_or_due"))

    # ---- annual / as-needed / work-order contracts are no project total
    for lines, want in (
            (["CITY OF MESQUITE", "BID TABULATION",
              "Bid Name: Annual as Needed Street and Alley Maintenance Contract"], "on_call"),
            (["#25-80-A Work Order Generated Concrete Repairs"], "on_call"),
            (["ITB 26-014 Annual Concrete Contract - Repair of Sidewalk and Streets"], "on_call"),
            (["As-Needed Storm Sewer Repair Services"], "on_call"),
            (["2024 Annual Street Maintenance (ASM), Project ASM2024R"], ""),
            (["2020-21 Annual Asphalt Rehabilitation Program"], ""),
            (["BID TABULATION", "1 Sod replacement, as needed 2 EA"], ""),
            (["3RD STREET AND MAPLE STREET PAVING"], ""),
            (["Contract 2024-352: Purchase of Uniform and Clothing Items"], "")):
        check("contract hint %r" % lines[-1][:40], contract_hint(lines), want)

    # ---- families
    for d, want in (
            ('3" Mill and Remove', "demo_pavement"),      # removal words: priced from removal history (F2)
            ("Mill & Remove Existing Asphalt", "demo_pavement"),
            ("Mill and Haul Off Existing Asphalt", "pavement_milling"),
            ('2" Asphalt Milling', "pavement_milling"),
            ("Microcracking Pavement Base", "subgrade_stab"),
            ("Micro-Cracking of Cement Treated Base", "subgrade_stab"),
            ('Burst 15" CI to 16" HDPE (0\'-8\' Deep)', "pipe_bursting"),
            ('Burst Ex. 6" CI to 8" Fusible PVC (0\'-8\' Deep)', "pipe_bursting"),
            ('Burst 6" CI to 8" Fusible 8" PVC', "pipe_bursting"),
            ("Single Sided RRFB Assembly and Foundation", "traffic_signal"),
            ("Rectangular Rapid Flashing Beacon", "traffic_signal"),
            ("4' Dia. SSMH, Furnish & Install 5' Dia. SSMH", "manhole"),
            ("Install 4' SDMH", "manhole"),
            ('Constr. 4\'x4\' 4" Thick Reinf. Conc. Pad @ SSMH', "concrete_misc"),
            ("Curb Address Painting", "pavement_marking"),
            ("PAINT HOUSE NUMBER ON CURB (MATCH EXISTING)", "pavement_marking"),
            ("Address Number Replacement", "pavement_marking"),
            ("Furnish and Install Address Numbers on Curb", "pavement_marking"),
            ("Minor miscellaneous work, price per hour: fence maintenance, address numbers, graffiti removal",
             "fence"),
            ("Remove and Salvage RRFB", "relocate_reset"),
            ("Sizes XS-XL, District DM130 Perfect Tri Tee (or County approved equivalent)", "other"),
            ("Men's Harriton Short Sleeve Polo M211 in Black - Must Include Embroidery", "other"),
            ("Size 2XL, Port & Company PC90H Essential Fleece Pullover Hooded Sweatshirt", "other"),
            ("TxDOT FW-S (2-8'x5') (4:1) (HW=10') (30 SKEW) W/ FORM LINER", "wingwall"),
            ("WINGWALL (PW - 1) (HW=8 FT)", "wingwall"),
            ("HEADWALL (CH - PW - 0) (DIA= 36 IN)", "headwall"),
            ('6" PVC Under Drain', "storm_pipe"),
            ('PIPE UNDERDRAINS (TY 6)(6")', "storm_pipe"),
            ("5'x5' Area Drain", "inlet_structure"),
            ("Stone Veneer (ST-1/1A)", "masonry_fascia"),
            ('Stone Veneer Type B 3" Cast Stone Veneer', "masonry_fascia"),
            ("Stone Veneer Retaining Wall", "retaining_wall"),
            ("Abrasive Blast Clean Exterior (SSPC-SP6), Surface Preparation, and Recoat Exterior of Tank", "painting"),
            ("Full Blast Surface Preparation and Recoat Interior of Tank", "painting"),
            ("Graffiti Removal (Blast Cleaning)", "other"),
            ("Import and Place at Grade", "embankment_fill"),
            ("Import and Place Sod", "seeding_sod"),
            ('8" x 6" MJ Tee', "pipe_fittings"),
            ('12" Solid Sleeve', "pipe_fittings")):
        check("family %r" % d[:44], fam(d), want)
    for d in ("2\" Mill and Overlay", "Mill Creek Drive Paving", "Cold Mill 2\" and Overlay 2\" HMAC"):
        check("milling unchanged %r" % d, fam(d) == "pavement_milling", False)
    check("milling unchanged: plain milling of asphalt", fam("Milling Asphalt Pavement (2\")"), "pavement_milling")
    check("a street named SW 15th is no wingwall", fam("SW 15th Street Paving") == "wingwall", False)
    check("SW 0 without a headwall height is no wingwall", fam("SW 0 Sign Post") == "wingwall", False)

    # ---- a thickness is no size on an EA / LS line (the main() pass reads size_is_thickness)
    check("pad thickness", size_is_thickness(norm_desc('Constr. 4\'x4\' 4" Thick Reinf. Conc. Pad @ SSMH')), True)
    check("valve size", size_is_thickness(norm_desc('8" Gate Valve and Box')), False)
    check("pavement thickness (kept per SY)", size_is_thickness(norm_desc('6" Thick Concrete Pavement')), True)
    check("no size", size_is_thickness(norm_desc("Concrete Pad")), False)

    for f in fails:
        print("FAIL round-9 rule: " + f)
    print("round-9 rules (.30): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _round10_selftest():
    """Round 10 (.31): held tabs that read nothing and names that are not bidders (reports/
    parser_round10.md).  Synthetic grids shaped like the tabs that needed each rule, with the
    look-alikes each rule must leave alone."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    # ---- the engineer's column: a misspelt caption ...
    check("misspelt engineer captions", [bool(ENGINEER_RE.search(s)) for s in (
        "ENGINEER OPION OF COST", "Engineer's Opinion of Cost", "Enginer Opinon of Probable Cost",
        "Opinion of Probable Construction Cost", "Engineer's Probable Cost",
        "Engineers Estimated Construction Cost")], [True] * 6)
    check("firms that bid stay bidders", [bool(ENGINEER_RE.search(s)) for s in (
        "WG Engineering & Construction, LLC", "Knight Engineering & Construction", "Opinion Construction",
        "Cost Plus Construction", "JABG Construction & Engineer (Thomas 11 Development LLC)",
        "Hanna Survey and Engineering LLC")], [False] * 6)
    # ... and a column named for the firm whose letterhead heads the tab
    head = ["Hanna Survey and Engineering LLC", "Consulting Engineers Bid Tabulation Report",
            "Bid Opening Date: 08/12/2025 Intersection Design and Improvements",
            "Project Item Information Hanna Survey and Engineering LLC Palmer Hall Construction LLC XIT"]
    recs = [{"bidder_raw": b, "is_engineer_estimate": False} for b in (
        "Hanna Survey and Engineering LLC", "Palmer Hall Construction LLC", "Farier Consulting LLC")]
    check("letterhead firm's column is the estimate",
          (mark_preparer_columns(recs, head), [r["is_engineer_estimate"] for r in recs]), (1, [True, False, False]))
    check("letterhead over a profession line",
          preparer_firms(["BIRKHOFF, HENDRICKS & CARTER, L.L.P.", "PROFESSIONAL ENGINEERS", "Dallas, Texas"]),
          {"birkhoff hendricks & carter"})
    check("a names line is no letterhead",
          preparer_firms(["Harry McKillop Illumination City of McKinney", "CIP PROJECT TR2021",
                          "BEAN ELECTRICAL DURABLE SPECIALTIES GROVES ELECTRICAL MELS ELECTRIC WG ENGINEERING"]),
          set())
    check("a contractor's own name line is no letterhead",
          preparer_firms(["ACME Paving, Inc.", "Bid Tabulation", "Engineering Department"]), set())
    check("no letterhead beyond the first lines",
          preparer_firms(["CITY OF X", "BID TABULATION", "Project 12", "Bid date", "Notes", "Page 1",
                          "Halff Engineering LLC", "Consulting Engineers"]), set())

    # ---- names: form answers, award remarks, legends, contact lines, captions, placeholders
    check("not names", [looks_like_bidder(s) for s in (
        "Recommended for Award", "light gray shading - line item error", "Yes", "No", "N/A",
        "Contact: Mike Atkins, Pres.", "Apparent Low Bidder", "Lowest Responsive Bidder",
        "* denotes a mathematical error", "2nd LOWEST", "LOWEST BIDDER", "Bid Bond: 5% G.A.B",
        "Addendum #1: Acknowledged (5/28/2021)")], [False] * 13)
    check("names", [looks_like_bidder(s) for s in (
        "Aushill Construction, LLC", "Legend Construction", "Highland Paving Co.", "Contact Construction LLC",
        "Low Water Crossing Builders, Inc.", "308 Construction", "Bond Construction Co.",
        "Highest Quality Paving", "Second Street Builders")], [True] * 9)
    check("contact lines and template placeholders leave the name", [strip_address(s) for s in (
        "Aushill Construction, LLC\nContact: Chuka Anumele, Pres.",
        "Contractor 7 Address Line 1 Address Line 2",
        "Saber Development Corp. P . O . Box 540186 Dallas,",
        "Address City, State",
        "308 Construction",
        "1704 Keller Pkwy",
        "4999 Keller Haslet Rd.",
        "100 Industrial Services Hwy")],
          ["Aushill Construction, LLC", "Contractor 7", "Saber Development Corp.", "Address", "308 Construction",
           "", "", ""])        # (a bare 'Address' label stays: caption rows are found by such labels)
    check("a bidder block's caption", [_block_label_strip(s) for s in (
        "Bidders Name & Address Texas Standard Construction Ltd", "Bidder's Name and Address ACME Paving",
        "Name of Bidder: Bravo Construction", "Contractor Jeske Construction Company Address",
        "Bidders Name & Address", "Contractors Plus Roofing & Construction, LLC")],
          ["Texas Standard Construction Ltd", "ACME Paving", "Bravo Construction",
           "Jeske Construction Company", "Bidders Name & Address", "Contractors Plus Roofing & Construction, LLC"])
    # a bidder form: labels in the price column, values in the total column (Forney)
    E = ""
    fg = [["OWNER:", "CITY OF X", E, E, "Contractor", "S-Co Incorporated", "Contractor", "ALLTEX SITE SERVICES, LLC"],
          ["PROJECT:", "WATER CONNECTION", E, E, "Address", "2329 COUNTY ROAD 348", "Address", "10553 FM 1390"],
          [E, E, E, E, "City", "JEWETT", "City", "SCURRY"],
          ["ENGINEER:", "Some Engineers, Inc.", E, E, "State", "TEXAS, 75846", "State", "TEXAS, 75158"],
          ["BID DATE:", "8-Aug-23", E, E, "Phone", "903-390-6733", "Phone", "214-803-7988"],
          ["ITEM NO.", "DESCRIPTION", "QUANTITY", "UNIT", "UNIT PRICE", "TOTAL AMOUNT", "UNIT PRICE", "TOTAL AMOUNT"],
          ["P.1", "Mobilization", "1", "LS", "20,800.00", "$20,800.00", "$23,250.00", "$23,250.00"]]
    check("a bidder form's firms", bidder_names_for(fg, 5, [(4, 5), (6, 7)]),
          {4: "S-Co Incorporated", 6: "ALLTEX SITE SERVICES, LLC"})
    # a rank number over each firm, read into the name cell (Watauga)
    rg = [[E, E, E, E, "Engineer's Estimate", E, "7 2L Construction LLC", E, E, "8 AT Construction LLC", E, E],
          ["ITEM NO.", "DESCRIPTION OF ITEM", "UNIT", "BID QUANTITY", "UNIT PRICE", "AMOUNT", "UNIT PRICE", "AMOUNT",
           "Ratio to Engineer", "UNIT PRICE", "AMOUNT", "Ratio to Engineer"],
          ["1", "Remove Existing Asphalt Pavement", "SY", "7,922", "$ 1.50", "$ 11,883.00", "$ 14.00", "$ 110,908.00",
           "9.33", "$ 20.00", "$ 158,440.00", "13.33"]]
    check("rank numbers over the firms", bidder_names_for(rg, 1, [(4, 5), (6, 7), (9, 10)]),
          {4: "Engineer's Estimate", 6: "2L Construction LLC", 9: "AT Construction LLC"})
    one = [list(r) for r in rg]
    one[0][6], one[0][9] = "308 Construction", "Beta Paving LLC"
    check("a lone number is the firm's", bidder_names_for(one, 1, [(4, 5), (6, 7), (9, 10)]),
          {4: "Engineer's Estimate", 6: "308 Construction", 9: "Beta Paving LLC"})
    # the firms above a block of form rows (Dallas County's Cockrell Hill tab)
    E = ""
    g = [[E, E, E, E, "Vendor No. 1 - Lowest Bidder #1", E, "Vendor No. 2 - #2 Bidder", E],
         [E, "PROJECT: X", E, E, "Aushill Construction, LLC 2919 Commerce Street, Suite 700", E,
          "Atkins Brothers Equipment Co.", E],
         [E] * 8,
         [E, E, E, E, "Contact: Chuka Anumele, Pres.", E, "Contact: Mike Atkins, Pres.", E],
         [E, "Bid Bond Received", E, E, "Yes", E, "Yes", E],
         [E, "Addendums 1 - 3 Acknowledged", E, E, "Yes", E, "Yes", E],
         [E, "MWBE / SBE Firm", E, E, "Yes", E, "No", E],
         [E, "Dallas County TaxPayer", E, E, "Yes", E, "No", E],
         [E, E, E, E, E, E, "light gray shading - line item error", E],
         [E, E, E, E, "Recommended for Award", E, E, E],
         ["ITEM", "DESCRIPTION", "UNIT", "QUANTITY", "UNIT PRICE", "EXTENSION", "UNIT PRICE", "EXTENSION"],
         ["100", "Preparing Right-Of-Way", "STA", "24.0", "$ 250.00", "$ 6,000.00", "$ 10.00", "$ 240.00"],
         ["104", "Removing Concrete", "SY", "2,700", "$ 10.00", "$ 27,000.00", "$ 2.00", "$ 5,400.00"]]
    r, _a = rows_from_grid(g, "t.pdf", "p1t0", {})
    check("firms above a block of form rows",
          sorted({x["bidder_raw"] for x in r}), ["Atkins Brothers Equipment Co.", "Aushill Construction, LLC"])
    # ... the window widens only by form rows: eight empty rows still end it
    g2 = [g[1]] + [[E] * 8 for _ in range(8)] + g[10:]
    r2, _a = rows_from_grid(g2, "t.pdf", "p1t0", {}, allow_headerless=False)
    check("empty rows still count against the walk", sorted({x["bidder_raw"] for x in r2}), [""])

    # ---- a price caption stacked over two header rows (Mansfield's Brinsap bridge repair tab)
    g = [["BID TABULATION", E, E, E, E, E, E, E],
         ["2023 BRIDGE REPAIRS", E, E, E, E, E, E, E],
         ["DATE:", "FEBRUARY 28, 2025", E, E, E, E, E, E],
         [E, E, E, E, "GROD CONSTRUCTION", E, "KLUTZ CONSTRUTION", E],
         ["ITEM", E, E, E, "UNIT", E, "UNIT", E],
         ["NO.", "DESCRIPTION", "UNITS", "QUANTITY", "COST", "AMOUNT", "COST", "AMOUNT"],
         ["101", "MOBILIZATION", "LS", "1", "$20,000.00", "$20,000.00", "$46,985.00", "$46,985.00"],
         ["104", "INSTALL TYPE R STONE RIPRAP", "CY", "35", "$300.00", "$10,500.00", "$195.00", "$6,825.00"],
         ["109", "INSTALL 12\" GABION MATTRESS", "SY", "270", "$175.00", "$47,250.00", "$215.00", "$58,050.00"]]
    r, _a = rows_from_grid(g, "t.pdf", "p1t0", {})
    check("stacked price captions", (len(r), sorted({x["bidder_raw"] for x in r}),
                                     sorted({(x["item_no"], x["quantity"], x["unit"]) for x in r})[:1]),
          (6, ["GROD CONSTRUCTION", "KLUTZ CONSTRUTION"], [("101", 1.0, "LS")]))
    check("a caption whole on one row is no stack",
          stacked_caption_header([["ITEM", "DESCRIPTION", "UNIT", "QTY", "UNIT PRICE", "TOTAL"],
                                  ["1", "Mobilization", "LS", "1", "$5.00", "$5.00"]]), None)
    check("a data row is never stacked onto",
          stacked_caption_header([["1", "Mobilization", "LS", "1", "UNIT", "5"],
                                  ["NO.", "DESCRIPTION", "UNITS", "QTY", "COST", "AMOUNT"]]), None)

    # ---- a price column proved by its arithmetic, in per-bidder blocks (Josephine's ditch tab)
    g = [["ITEM #", "ITEM DESCRIPTION", "Anchor Contracting, LLC Sulphur Springs, TX", E, E, E,
          "Akin Excavation, LLC Terrell, TX", E, E, E],
         [E, E, "Unit", "Qty", "Estimated Quanity", "Total", "Unit", "Qty", "Estimated Quanity", "Total"],
         ["1", "Roadside drainage ditch", "LF", "11,720", "$6.00", "$70,320.00", "LF", "11,720", "$18.77",
          "$219,984.40"],
         ["2", "Remove and replace 12\" CMP", "EA", "34", "$1,900.00", "$64,600.00", "EA", "34", "$693.60",
          "$23,582.40"],
         ["3", "Remove and replace 15\" CMP", "EA", "3", "$2,500.00", "$7,500.00", "EA", "3", "$830.10",
          "$2,490.30"],
         ["5", "Mobilization", "LS", "1", "$16,000.00", "$16,000.00", "LS", "1", "$28,000.00", "$28,000.00"],
         [E, "TOTAL OF ALL BID ITEMS", E, E, E, "$169,220.00", E, E, E, "$277,546.54"]]
    st = {}
    r, _a = rows_from_grid(g, "t.pdf", "p1t0", st)
    tot = {}
    for x in r:
        tot[x["bidder_raw"]] = round(tot.get(x["bidder_raw"], 0) + x["quantity"] * x["unit_price"], 2)
    check("per-bidder blocks, price proved by arithmetic", (len(r), tot),
          (8, {"Anchor Contracting, LLC": 158420.0, "Akin Excavation, LLC": 274057.1}))
    one = [[E, E, "HD Cooks Rock Solid, INC. Roanoke, TX", E, E, E]] + [row[:6] for row in g[1:]]
    one[0][0], one[0][1] = "ITEM #", "ITEM DESCRIPTION"
    r, _a = rows_from_grid(one, "t.pdf", "p3t0", {})
    check("one block", (len(r), sorted({x["bidder_raw"] for x in r})), (4, ["HD Cooks Rock Solid, INC."]))
    bad = [list(row) for row in g]
    for row in bad[2:6]:
        row[4] = "$1.00"
    check("no proof, no price column", proved_price_blocks(bad), None)
    own = [list(row) for row in g]
    own[2][7] = "9,999"
    own[3][7] = "99"
    check("bidder-specific quantities are not one row", proved_price_blocks(own), None)

    for f in fails:
        print("FAIL round-10 rule: " + f)
    print("round-10 rules (.31): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _round8_selftest():
    """Round 8 (.29): families and sizes from the dollar-ranked municipal diagnosis
    (reports/parser_round8.md), and the item number + quantity read into the end of a description."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    def fam(d):
        return item_family(norm_desc(d))

    for d, want in (
            # geotextile -- and what stays where it was
            ("Paving Fabric (Petromat)", "geotextile"),
            ("HEAVY DUTY WEED BARRIER FILTER FABRIC", "geotextile"),
            ("POLYSHEETING MOISTURE BARRIER", "geotextile"),
            ("Moisture Barrier (10 mil poly sheeting)", "geotextile"),
            ("10 mil Poly Moisture Barrier", "geotextile"),
            ("Geotextile", "geotextile"),
            ("TYPE 2 FILTER FABRIC", "geotextile"),
            ("Installation of Paving Fabric (Petromat) to include tack coat, materials", "geotextile"),
            ('12" Grouted Rock Riprap on Filter Fabric and Gravel Bedding', "riprap_stone"),
            ('Colorado River Rock 5" Deep 2-4" Avg Dia incl Filter Fabric', "landscape_material"),   # the rock, fabric included
            ('1" Limestone Gravel and 5 oz Geotextile Weed Barrier', "other"),
            ("Install 6 foot chain link construction fencing with filter fabric", "fence"),
            ("Geogrid", "subgrade_stab"),
            ("Silt Fence", "erosion_control"),
            # seeding / sod
            ("HYDRAMULCH (TCE, SE) (PKWY)", "seeding_sod"),
            ("Hydro-Mulch", "seeding_sod"),
            ("Revegetation", "seeding_sod"),
            ("Restoration of Parkways and Yards (No Irrigation)", "seeding_sod"),
            ("RESTORE PARKWAYS, MEDIANS, AND DISTURBED AREAS", "seeding_sod"),
            ("Furnish, Install, Water & Establish Vegetation for Disturbed Area", "seeding_sod"),
            ("Turfgrass Planting", "seeding_sod"),
            ("Restore Irrigation System in Parkway", "irrigation"),
            ("Restoration of Parkway/Yards Landscape/Irrigation", "irrigation"),
            # subgrade / base / pavement layers
            ('Full-Depth Reclamation - 8" depth, 22\' width', "subgrade_stab"),
            ("FULL DEPTH RECLAMATION", "subgrade_stab"),
            ('Pulvermix 14" Depth', "subgrade_stab"),
            ("Rework and Scarify Existing Base", "subgrade_stab"),
            ('REWORK BS MATL (TY C)(8")(ORD COMP)', "subgrade_stab"),
            ('CEMENT TRT (EXISTING MATL)(NEW BASE)(6")', "subgrade_stab"),
            ('CEMENT SLURRY STABILIZATION (RATE OF 40LBS @ 8")', "subgrade_stab"),
            ("Furnish Cement For Mixing (36 lb./S.Y.)", "subgrade_stab"),
            ("CEMENT", "subgrade_stab"),
            ("Scarify & Remix Existing Asphalt & Base Material to a depth of 6-inches of Cement Treated "
             "Material", "subgrade_stab"),
            ('8" CRUSHED CONCRETE', "flex_base"),
            ('INSTALL UNIFORMLY 6" CRUSHED STONE', "flex_base"),
            ("CRUSHED ROCK FOR PAVING REPAIR", "flex_base"),
            ("Hydromulch disturbed areas outside of crushed stone", "seeding_sod"),
            ("Crushed Stone Bedding for Pipe", "other"),
            ('12" REINFORCED CONCRETE ROADWAY PAVEMENT (4,000 PSI)', "conc_pavement"),
            ("RC Pavement Class C 3,600 PSI 8 in Roadway", "conc_pavement"),
            ('6" Concrete Panel Replacement (Full Depth Repair)(Sawcut Included)', "conc_pavement"),
            ('PLAN ASPH CONC PAV(0" TO 2-1/4")', "pavement_milling"),
            ("Cold Milling 6 Feet Width and 2 Inches deep.", "pavement_milling"),
            ("Cold milling and overlay", "asphalt_pavement"),
            ('RMV (0"-12") TRT/UNTRT BASE & ASPH PAV', "demo_pavement"),
            ("RMV/REPL VIA SAND FILL PLASTIC BARRELS", "embankment_fill"),
            ("SP MIXES (SP-C)(SAC-B)(PG64-22)", "asphalt_pavement"),
            ('FLEXIBLE PAVEMENT STRUCTURE REPAIR (12")', "asphalt_pavement"),
            ("INTERSECTIONS (ACP)", "asphalt_pavement"),
            ("DRIVEWAYS (ACP)", "driveway"),
            ("POTHOLE REPAIR (STANDARD)", "asphalt_pavement"),
            ("Pothole Existing Utilities", "other"),
            ("OBLITERATING ABANDONED ROAD", "demo_pavement"),
            ('SALV, HAUL & STKPL RCL ASPH PV (2")', "demo_pavement"),
            ("Rout and Seal Concrete Cracks", "surface_treatment"),
            ("Penetrating Concrete Surface Treatement", "surface_treatment"),
            ("DRIVEWAYS (SURF TREAT)", "driveway"),
            # valve vs assembly, hydrants, adjustments
            ("3-inch Combination Air Valve Assembly", "valve_assembly"),
            ('Combination Air Release Valve (6")', "valve_assembly"),
            ("8-INCH BLOW OFF VALVE", "valve_assembly"),
            ('Furnish & Install 2" Manual Flush Point', "valve_assembly"),
            ("Demolition Blow Off Assembly", "other"),   # no removal rule reads "demolition"; not an assembly
            ("18-inch Gate Valve", "valve_hydrant"),
            ('Butterfly Valve (36") & Vault', "valve_hydrant"),
            ("Fire Hydrant Assembly", "fire_hydrant"),
            ('Fire Hydrant with 6" Gate Valve', "fire_hydrant"),
            ('Remove and replace fire hydrant assembly, including 6" valve, tee and lead', "fire_hydrant"),
            ("Install Fire Hydrant Extension", "valve_hydrant"),
            ('Furnish & Install 6" C-900 PVC Fire Hydrant Lead Including Connections', "valve_hydrant"),
            ("Relocate Existing Fire Hydrant", "relocate_reset"),
            ("Remove Existing Fire Hydrant", "demo_utility"),
            ("Adjust Water Valve Stack, Complete in Place", "adjust_to_grade"),
            ("Adjust Existing Valve Box to Final Grade", "adjust_to_grade"),
            ("Valve Box Adjustment", "adjust_to_grade"),
            ("ADJUSTING MANHOLES", "adjust_to_grade"),
            ("Adjust Manhole to Grade", "adjust_to_grade"),
            ("Adjust Existing Water Meter Box to Finished Grade", "adjust_to_grade"),
            ('Existing Water Main Vertical Adjustment 4" to 8"', "water_pipe"),
            ("Adjust Existing Irrigation", "irrigation"),
            ("Remove and Reset Mailbox", "relocate_reset"),
            # traffic signal, markings
            ("INS TRF SIG PL AM (S) 1 ARM (48) LUM", "traffic_signal"),
            ("INSTALL HWY TRF SIG (ISOLATED)", "traffic_signal"),
            ("DRILL SHAFT (TRF SIG POLE) (36 IN)", "drill_shaft"),
            ("TRF SIG CBL (TY A)(14 AWG)(7 CONDR)", "electrical_lighting"),
            ("PED DETECT PUSH BUTTON (APS)", "traffic_signal"),
            ("REMOVAL OF PEDESTRIAN PUSH BUTTONS", "demo_other"),
            ("BACKPLATE W/REF BRDR(3 SEC)(VENT)ALUM", "traffic_signal"),
            ("INSTALL RDSD FLSH BCN ASSM (SOLAR PWRD)", "traffic_signal"),
            ('4" RPM TY II C-R', "pavement_marking"),
            ('REFL PAV MRK TY I (W)24"(SLD)(100MIL)', "pavement_marking"),
            ("Traffic Button / RPM", "pavement_marking"),
            # landscape, trees
            ("FLAME ACANTHUS, 1 GAL (ORNAMENTAL GRASS)", "landscape_planting"),
            ("PLANT MATERIAL (5 GAL)", "landscape_planting"),
            ('Furnish & Install Annual Color - 4"pot', "landscape_planting"),
            ("SEASONAL COLOR (IN POTS)", "landscape_planting"),
            ("Pink Muhly Grass", "landscape_planting"),
            ("PLANT MATERIAL (45 GAL TREE)", "tree_install"),
            ("4-INCH MINIMUM CALIPER TREES", "tree_install"),
            ('LARGE CANOPY TREE (4" CALIPER)', "tree_install"),
            ('Hardwood Mulch Shade Tree Rings, Planting Area 2" Deep', "seeding_sod"),
            ("Prime Coat (MC-30) 0.20 Gal/SY", "surface_treatment"),
            ("Bed Prep Shrubs/Grasses Complete and in Place", "seeding_sod"),
            ("LOOSE AGGR FOR GROUNDCOVER (TYPE I)", "landscape_material"),
            # bypass pumping
            ("Bypass Pumping", "bypass_pumping"),
            ("Temporary Sewage Bypass Pumping for Gravity Outfall Sewer Improvements", "bypass_pumping"),
            ("WASTEWATER FLOW CONTROL", "bypass_pumping"),
            ("Sanitary Sewer Bypass Pumping", "bypass_pumping"),
            ('16" Vertical Gate Valve w/o By-Pass', "valve_hydrant"),
            ('Line Stop w/Bypass at 24" RCCP', "other"),
            ("Furnish & Install 5' Diameter Cast in Place Manhole, Including All Bypass Pumping", "manhole"),
            ("R&R Wooden Fence for Bypass Pumping Access", "fence"),
            # concrete splits
            ("CONCRETE STRUCTURE REPAIR (VERTICAL & OVERHEAD)", "concrete_repair"),
            ("BRIDGE JOINT REPAIR (ARMOR)", "concrete_repair"),
            ("Concrete Pavement Repair", "conc_pavement"),
            ('Install 2" HMAC compacted along concrete repairs', "asphalt_pavement"),
            ("PRESTR CONC GIRDER (TX46)", "bridge_concrete"),
            ("PRESTR CONC BOX BEAM (5B20)", "bridge_concrete"),
            ("Prestressed Concrete Beams (TX46)", "bridge_concrete"),
            ("CL C CONC (CAP)(HPC)", "bridge_concrete"),
            ("REINF CONC SLAB (HPC) (BRIDGE)", "bridge_concrete"),
            ("APPROACH SLAB (HPC)", "bridge_concrete"),
            ("CL C CONC (MISC)", "concrete_misc"),
            ("CONCRETE FOOTING", "concrete_misc"),
            ("Concrete Beam (Grade Control)", "conc_beam"),
            ("8'x5' Reinforced Concrete Box Culvert", "box_culvert"),
            ("Concrete pier (Augger Cast Pile) & Cap", "drill_shaft"),
            ("24-inch concrete pier foundation", "drill_shaft"),
            ("Concrete Pier Cap Class A", "concrete_misc"),
            ("Remove Ex. Concrete Pier", "demo_other"),
            ("Low Profile Concrete Barrier Installation & Moving (TYPE 1)", "conc_barrier"),
            ("Barrier Free Ramp", "barrier_free_ramp"),
            ("INSTALL CONCRETE WASHOUT PIT", "erosion_control"),
            ("Repair Washout Area for Existing Headwall", "headwall"),
            # abbreviations
            ("EMBANK (FNL)(DC)(TY C)", "embankment_fill"),
            ("JCTBOX (COMPL)(PJB)(4FTX4FT)", "inlet_structure"),
            ('16" WATER BOTOC (DWU)', "encasement"),
            ('36" Reinforced Concreate Pipe Class III', "storm_pipe"),
            ("24-Inch HP Storm", "storm_pipe"),
            ("TxDOT 169 7001 SOIL RET BLKT (SL_MOD_CLAY_SHORT)", "erosion_control"),
            ("PREPARING R.O.W.", "site_prep_clearing"),
            ("Prepare and Restore Right-of-Way", "site_prep_clearing"),
            ("ROCK NAIL ANCHORS", "rock_anchor"),
            # a pipe that includes its own test; PVC sewer; concrete-pavement grinding; sodding for a trail
            ('8" PVC DR-14 WATER PIPE, FITTINGS, BLOCKING, TESTING, EMBEDMENT', "water_pipe"),
            ("12 in PVC sewer open cut w/CCTV", "sanitary_pipe"),
            ("DUCTILE IRON PIPE FITTINGS", "pipe_fittings"),
            ("Storm Sewer Television Inspection", "testing_inspection"),
            ("CCTV Inspection Storm Sewer (42 in - 72 in)", "testing_inspection"),
            ("Manhole Vacuum Testing", "testing_inspection"),
            ("Water Line Testing", "testing_inspection"),
            ('8" SDR 26 PVC Sanitary Sewer', "sanitary_pipe"),
            ("CONTINUOUS DIAMOND GRINDING CONCRETE PAVEMENT", "pavement_milling"),
            ("TEXTURING CONCRETE PAVEMENT", "pavement_milling"),
            ('8" Reinforced Concrete Pavement', "conc_pavement"),
            ("SOLID BLOCK SODDING FOR TRAIL REPLACEMENT", "seeding_sod"),
            # landscape material, compost, temporary asphalt
            ('2" EAGLE BLACK GRAVEL (4" DEPTH)', "landscape_material"),
            ('Colorado River Rock 5" Deep 2-4" Avg Dia', "landscape_material"),
            ("TEJAS BLACK GRAVEL (1.5\" DIAMETER)", "landscape_material"),
            ("Decomposed Granite", "landscape_material"),
            ("LOOSE AGGR FOR GROUNDCOVER (TYPE II)", "landscape_material"),
            ("Landscaping Boulder (1.5 Ton Min)", "landscape_material"),
            ("Remove River Rock", "demo_other"),
            ("Limestone Boulder Retaining Walls", "retaining_wall"),
            ('12" MIX COMPOST AT A PLANTING AREA', "seeding_sod"),
            ("General Use Compost", "seeding_sod"),
            ("Compost Filter Sock", "other"),
            ("ASPHALT MATERIAL FOR TRAFFIC MAINTENANCE", "asphalt_pavement"),
            ('ASPHALT TRANSITION (3") TY B', "asphalt_pavement"),
            # from the 60-case audit
            ("APS PUSH BUTTONS AND SIGN ASSEMBLIES (FURNISH & INSTALL)", "traffic_signal"),
            ("Furnish and Install Traffic Sign", "signing"),
            ('3"-5" Crushed Limestone Rock', "other"),
            ('6" Crushed Limestone Base', "flex_base"),
            ("FURNISH AND INSTALL IRRIGATION ALONG TRAIL", "irrigation"),
            ('10\' Concrete Trail (6" Thick)', "sidewalk")):
        check("family %r" % d, fam(d), want)
    # a fire hydrant has no size; a gate valve keeps its diameter; a box culvert its opening area
    check("hydrant unsized", extract_size_in(norm_desc('Fire Hydrant with 6" Gate Valve')), None)
    check("gate valve sized", extract_size_in(norm_desc('8" Gate Valve and Valve Box')), 8.0)
    check("air valve sized", extract_size_in(norm_desc('3" Combination Air Release Valve')), 3.0)
    check("box culvert area", extract_size_in(norm_desc("8'x5' Reinforced Concrete Box Culvert")), 40.0)
    # family_text reads abbreviations; desc_norm is unchanged
    check("rmv alias", family_text("rmv 2 in 6 in trt/untrt base asphalt pav"),
          "remove 2 in 6 in trt/untrt base asphalt pav")
    check("rmv/repl kept", family_text("rmv/repl via sand fill plastic barrels"),
          "rmv/repl via sand fill plastic barrels")
    check("desc_norm unchanged", norm_desc("RMV STAB BS & ASPH PAV"), "rmv stab bs asphalt pav")

    # the item number and quantity read into the end of a description
    def row(item, d, q, sheet="p1"):
        return {"src_file": "f.pdf", "src_sheet": sheet, "item_no": item, "description": d,
                "quantity": q, "desc_norm": norm_desc(d), "item_family": fam(d)}
    rs = [row("#0-1", "MOBILIZATION (5% MAX) 1 1", 1.0),
          row("#0-7", "SILT FENCE 7 9856", 9856.0),
          row("#0-14", "INSTALL 48-INCH DIP CLASS 200 WATER PIPE BY OPEN CUT 14 4105", 4105.0),
          row("#0-14", "INSTALL 48-INCH DIP CLASS 200 WATER PIPE BY OPEN CUT 14 4105", 4105.0),
          row("#0-20", "18 in RCP Class III 20 20", 20.0),
          row("#0-21", "Concrete Collar 21 3", 4.0),
          row("#0-22", "12 in Gate Valve 2", 2.0)]
    check("trailing item / qty: rows changed", strip_trailing_item_quantity(rs), 5)
    check("trailing item / qty: text", [r["description"] for r in rs],
          ["MOBILIZATION (5% MAX)", "SILT FENCE", "INSTALL 48-INCH DIP CLASS 200 WATER PIPE BY OPEN CUT",
           "INSTALL 48-INCH DIP CLASS 200 WATER PIPE BY OPEN CUT", "18 in RCP Class III",
           "Concrete Collar 21 3", "12 in Gate Valve 2"])
    check("trailing item / qty: family recomputed", rs[0]["item_family"], "mobilization")
    two = [row("#0-1", "MOBILIZATION 1 1", 1.0, "q"), row("#0-2", "BONDS 2 1", 1.0, "q")]
    check("trailing item / qty: two on a sheet is no layout", strip_trailing_item_quantity(two), 0)
    # trench safety (per LF) printed EA by the hundred loses its unit; a trench-safety plan (EA x 1) does not
    check("trench safety EA x 1514 suspect", ea_quantity_suspect({"unit": "EA", "quantity": 1514.0,
          "item_family": "trench_safety", "desc_norm": "trench safety for storm drain systems"}), True)
    check("trench safety plan EA x 1 kept", ea_quantity_suspect({"unit": "EA", "quantity": 1.0,
          "item_family": "trench_safety", "desc_norm": "trench safety plan"}), False)

    for f in fails:
        print("FAIL round-8 rule: " + f)
    print("round-8 rules (.29): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _units_codes_selftest():
    """Round 7 (.28): linear EA items, code-only descriptions, LF sidewalk
    widths, TxDOT item 500 (reports/harvest_city_2026-11-20.md and
    harvest_city2_2026-11-20.md section 4)."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    def fam(d):
        return item_family(norm_desc(d))

    # 1. linear EA: tracer wire, tape, bedding (and what stays EA)
    for d, f, q, want in (
            ("Tracer Wire", None, 4530.0, True),
            ("Furnish and Install #12 Tracer Wire", None, 150.0, True),
            ("Locator Wire for PVC Water Line", None, 2400.0, True),
            ("Detectable Warning Tape", None, 1200.0, True),
            ("Tracer Wire", None, 60.0, False),
            ("Tracer Wire Test Station", None, 120.0, False),
            ("Tracer Wire Access Box", None, 150.0, False),
            ("Electrical Tape", None, 300.0, False),
            ("Teflon Tape", None, 200.0, False),
            ("Cement Stabilized Sand Embedment", None, 40.0, True),
            ("Cement Stabilized Sand Bedding", None, 350.0, True),
            ("Cement Stabilized Sand Embedment", None, 12.0, False),
            ("BEDDING AND TOWELS: BLANKET: BED 62'' x 80'' WHITE OR GRAY", None, 400.0, False),
            ('1" SDR-9 Class 250 Poly Water Service & Water Meter, Including Fittings & Embedment', None, 38.0, False),
            ("FURNISH & INSTALL NEW FIRE HYDRANT. INCLUDING 6\" FH LEADS, CONNECTIONS, FITTINGS, EMBEDMENT, BACKFILL",
             None, 120.0, False),
            ('CONDT (PVC) (SCHD 40) (6")', None, 1240.0, True),
            ('24" DIP Water Line', None, 943.0, True),
            ("Flowable Fill Collar", None, 40.0, False)):
        dn = norm_desc(d)
        r = {"unit": "EA", "quantity": q, "desc_norm": dn, "item_family": f or item_family(dn)}
        check("EA x %g %r suspect" % (q, d), ea_quantity_suspect(r), want)
    check("linear EA in LF stays", ea_quantity_suspect({"unit": "LF", "quantity": 4530.0,
          "desc_norm": "tracer wire", "item_family": "other"}), False)

    # 4. mobilization: TxDOT item 500, its bid code, a doubled i
    for d, want in (
            ("TxDOT Item 500", "mobilization"),
            ("TXDOT ITEM 500 (5% MAX)", "mobilization"),
            ("Item 500 Mobilization", "mobilization"),
            ("ITEM NO. 500", fam("ITEM NO. 500")),
            ("MOBILIZATION MOBILIZATION (TXDOT ITEM 500)", "mobilization"),
            ("500-6001 6001 EQUIPMENT ETC)", "mobilization"),
            ("Mobiliization (MAX 5% OF BID TOTAL)", "mobilization"),
            ("SW3P IMPLEMENTATION (TXDOT ITEM 506)", fam("SW3P IMPLEMENTATION (TXDOT ITEM 506)")),
            ("Item 500.1 Sidewalk", fam("Item 500.1 Sidewalk")),
            ("Mobile Odor Control Units", "other"),
            ("Automobile allowance", "contingency"),
            ("500 LF of 8\" PVC", fam("500 LF of 8\" PVC"))):
        check("family %r" % d, fam(d), want)
    check("item 500.1 is not mobilization", fam("Item 500.1 Sidewalk") != "mobilization", True)
    check("a bare item 500 is not mobilization", fam("ITEM NO. 500") != "mobilization", True)
    check("SW3P item 506 is not mobilization", fam("SW3P IMPLEMENTATION (TXDOT ITEM 506)") != "mobilization", True)

    # 3. LF sidewalk / trail width in feet
    for d, want in (
            ("6' Concrete Sidewalk", 6.0),
            ("6\u2019 Concrete Sidewalk", 6.0),
            ("5-foot-wide Concrete Sidewalk", 5.0),
            ("4-foot-Wide Concrete Sidewalk", 4.0),
            ("5 ft Sidewalk", 5.0),
            ("12' wide Concrete Hike and Bike Trail / Pedestrian paving, 6\" thick concrete", 12.0),
            ("Construct 4-inch concrete sidewalk, 5-ft in width", 5.0),
            ("Excavate soil and install New class \"A\" Concrete sidewalkwidth6ft.atminimumdepth", 6.0),
            ("10' Concrete Trail", 10.0),
            ('60" wide concrete sidewalk', 5.0),
            ("Trail - Boardwalk - Metal Grating 8 ft Width", 8.0),
            ("Sidewalk Lug", None),
            ("4\" Thick Reinforced Concrete Sidewalk with Curb", None),
            ("6' tall retaining wall integral with sidewalk", None),
            ("Trail - Fishing Pier - Metal Grating 15' Fishing Area", None),
            ("4' x 6' Concrete Pad", None),
            ("matchexisting widthupto5 ft", None),
            ("2' Concrete Sidewalk", None),
            ("Sidewalk 6' W/ curb", None)):
        check("width_ft %r" % d, width_ft(d), want)
    check("LF sidewalk row: width", lf_width_size({"unit": "LF", "item_family": "sidewalk",
          "description": "6' Concrete Sidewalk", "size_in": None}), 6.0)
    check("LF sidewalk row without width: unsized", lf_width_size({"unit": "LF", "item_family": "sidewalk",
          "description": "4\" Thick Reinforced Concrete Sidewalk", "size_in": 4.0}), None)
    check("SY sidewalk row keeps its thickness", lf_width_size({"unit": "SY", "item_family": "sidewalk",
          "description": "6' wide 4\" sidewalk", "size_in": 4.0}), 4.0)
    check("LF boardwalk on a trail: unsized", lf_width_size({"unit": "LF", "item_family": "sidewalk",
          "description": "Trail - Boardwalk - Metal Grating 8 ft Width", "size_in": None}), None)
    check("LF curb keeps its size", lf_width_size({"unit": "LF", "item_family": "curb_gutter",
          "description": "6' wide 6\" curb", "size_in": 6.0}), 6.0)

    # 2. code-only descriptions
    for d, want in (("P2)", True), ("P01", True), ("A2-1", True), ("[02513]", True), ("321723", True),
                    ('9" X 42"', True), ("", True), ("ALT-A-1", True), ("(6)\" 321650", True),
                    ("SOD", False), ("Mobilization", False), ('18" RCP', False), ("BLOCK SOD", False)):
        check("desc_is_code_only %r" % d, desc_is_code_only(d), want)
    for it, d, want in (
            ("1. Mobilization", "", ("1", "Mobilization")),
            ("#1-2 Mobilization TX DOT LS 1", "", ("1-2", "Mobilization")),
            ("#1-1 Project Sign 15813 EA 1", "", ("1-1", "Project Sign")),
            ("MOBILIZATION", "17113", ("", "MOBILIZATION")),
            ("329223 SODDING", "1642", ("", "329223 SODDING")),
            ("REMOVE TREES", "P05", ("P05", "REMOVE TREES")),
            ("B-1 Reinforced Concrete Pipe (Class III)", '(18")', ("B-1", 'Reinforced Concrete Pipe (Class III) (18")')),
            ("1-1 024100 GENERAL SITE PREPARATION", "1", ("1-1", "GENERAL SITE PREPARATION")),
            ("Mobilization, Bonds, Insurance and all Permitting", "", ("", "Mobilization, Bonds, Insurance and all Permitting")),
            ("P10", "P2)", None),
            ("ALT-A-1 JOHNSTON PRODUCTS CUSTOM FENCE PANEL 120 LF ALT-A-2 AMERISTAR ORNAMENTAL FENCE", "", None)):
        check("_split_item_words %r | %r" % (it, d), _split_item_words(it, d), want)

    def row(item, desc, unit, q, bidder="A", f="f.pdf", sheet="p1t0"):
        dn = norm_desc(desc)
        return {"src_file": f, "src_sheet": sheet, "item_no": item, "description": desc, "desc_norm": dn,
                "item_family": item_family(dn), "size_in": extract_size_in(dn), "material": "",
                "unit_raw": unit, "unit": norm_unit(unit), "quantity": q, "bidder_raw": bidder}
    full = '8" REINFORCED CONCRETE PAVEMENT W/ INTERGRAL CURB & GUTTER (CL P1 OR P2)'
    recs = [row("P10", "P2)", "SY", 3460.0), row("P10", "P2)", "SY", 3460.0, bidder="B"),
            row("P10", full, "SY", -3460.0, sheet="p1t1"), row("P10", full, "SY", -3460.0, bidder="B", sheet="p1t1"),
            row("ALT03", "P2)", "SY", 3460.0),
            row("P11", "P2)", "SY", 3665.0), row("P11", "8\" PAVEMENT (CL P1 OR P3)", "SY", -3665.0, sheet="p1t1"),
            row("1. Mobilization", "", "LS", 1.0), row("P05", "P05", "LS", 1.0),
            row("P05", "GENERAL SITE PREPARATION", "LS", 1.0, f="g.pdf"),
            row("S01", "S01", "SY", 10.0, f="h.pdf"), row("S01", "RETAINING WALL", "SF", 10.0, f="h.pdf", sheet="c1")]
    got = recover_code_only_descriptions(recs)
    check("recovered (item cell, same item)", got, (1, 2))
    check("Beverly P10 base row restored", (recs[0]["description"], recs[0]["item_family"]), (full, "conc_pavement"))
    check("Beverly P10 second bidder restored", recs[1]["description"], full)
    check("ALT03 P2) stays a code (no same item)", recs[4]["description"], "P2)")
    check("P11 tail does not match: stays", recs[5]["description"], "P2)")
    check("item cell split", (recs[7]["item_no"], recs[7]["description"], recs[7]["item_family"]),
          ("1", "Mobilization", "mobilization"))
    check("same item number in ANOTHER file is not used", recs[8]["description"], "P05")
    check("another unit is not the same item", recs[10]["description"], "S01")

    # merged 'ITEM NO. ITEM DESCRIPTION' caption over two columns
    hdr = ["ITEM NO. ITEM DESCRIPTION", "", "UNIT", "QUANTITY", "UNIT PRICE", "TOTAL AMOUNT"]
    grid = [hdr,
            ["P01", "MOBILIZATION (5% MAX)", "LS", "1", "$750,000.00", "$750,000.00"],
            ["P02", "BARRICADES, SIGNS, AND TRAFFIC HANDLING", "LS", "1", "$150,000.00", "$150,000.00"],
            ["P03", "PROJECT SIGN", "EA", "2", "$1,000.00", "$2,000.00"],
            ["P06", "REMOVE ASPHALT PAVEMENT AND BASE", "SY", "333", "$20.00", "$6,660.00"]]
    pairs = [(4, 5)]
    meta = detect_meta_cols(hdr, 4)
    check("merged caption binds item and desc to one column", (meta["item"], meta["desc"]), (0, 0))
    m2 = _split_merged_item_caption(grid, 0, hdr, pairs, meta)
    check("merged caption split: description column", (m2["item"], m2["desc"], m2["unit"], m2["qty"]), (0, 1, 2, 3))
    hdr2 = ["Item No. Item Description Quantity Unit", "", "", "", "Unit Price", "Item Cost"]
    grid2 = [hdr2,
             ["P01", "MOBILIZATION", "1", "LS", "$10,909.41", "$10,909.41"],
             ["P04", "REMOVAL (EXISTING PIPE)", "5", "LF", "$230.31", "$1,151.55"],
             ["P06", "REMOVAL (RETAINING WALL)", "1,295", "LF", "$293.34", "$379,875.30"],
             ["P07", "TOPSOIL (4\")", "1,584", "SY", "$9.15", "$14,493.60"]]
    m3 = _split_merged_item_caption(grid2, 0, hdr2, pairs, detect_meta_cols(hdr2, 4))
    check("merged caption naming quantity and unit: only the description moves",
          (m3["item"], m3["desc"], m3["qty"], m3["unit"]), (0, 1, None, None))
    hdr3 = ["BID ITEMS", "UNIT", "QTY", "", "UNIT PRICE", "TOTAL"]
    grid3 = [hdr3, ["Mobilization", "LS", "1", "", "$5.00", "$5.00"], ["Sod", "SY", "10", "", "$5.00", "$50.00"],
             ["Seeding", "SY", "10", "", "$1.00", "$10.00"]]
    m4 = detect_meta_cols(hdr3, 4)
    check("words in the item column: left alone", _split_merged_item_caption(grid3, 0, hdr3, pairs, m4), m4)
    rs, _a = rows_from_grid(grid, "f.pdf", "p1t0", {})
    check("rows_from_grid reads the descriptions", sorted({(r["item_no"], r["description"]) for r in rs})[:2],
          [("P01", "MOBILIZATION (5% MAX)"), ("P02", "BARRICADES, SIGNS, AND TRAFFIC HANDLING")])

    for f_ in fails:
        print("FAIL units-codes", f_)
    print("units-codes (round 7): %d cases, %d failures" % (n[0], len(fails)))
    return not fails


def _ed_fixes_selftest():
    """Round 6 (.27): the erosion / drainage line diagnosis
    (handoff/scratchpad/diag_ed/, 2026-11-20) -- cleaning / desilting, trenchless
    crossings, force mains, grading, bed preparation, irrigation, railing,
    right-of-way preparation, tie backs, conduit EA x 500+, measure-unit lump
    sums."""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))

    def fam(d):
        return item_family(norm_desc(d))
    cases = (
        # 1. cleaning / desilting (and what must NOT become it)
        ("REMOVE SEDIMENT FROM BOX CULVERT", "cleaning_desilting"),
        ("REMOVE SEDIMENT FROM BOX CULVERTS (ASSUMED 1 FOOT DEEP) IN ACCORDANCE WITH THE PLANS", "cleaning_desilting"),
        ("REMOVE DEBRIS FROM CULVERT & CHANNEL", "cleaning_desilting"),
        ("CLEAN EXISTING CULVERTS", "cleaning_desilting"),
        ("STORM SEWER CLEANING (PIPE) (12\" - 18\" DIA)", "cleaning_desilting"),
        ("STORM SEWER CLEAN (BOX CULV) (24 - 48 SF)", "cleaning_desilting"),
        ("CLEANING DRAINAGE FLUMES", "cleaning_desilting"),
        ("Clean & Desilt Existing Culverts (Various Sizes)", "cleaning_desilting"),
        ("Light Cleaning 72\" RCP Storm Conduit", "cleaning_desilting"),
        ("MISD Detention Pond Pilot Channel Silt/Sediment Removal", "cleaning_desilting"),
        ("Clear Debris from Aerial Wastewater Line", "cleaning_desilting"),
        ("Remove and dispose of all sediment in bottom of existing tank", "cleaning_desilting"),
        ("Channel Debris Removal", "cleaning_desilting"),
        ("Hydraulic Dredging, Mechanical Dewatering and Removal", "cleaning_desilting"),
        ("Clean Inside Culvert", "cleaning_desilting"),
        ("FENCE LINE CLEANING - SITE 1", "fence"),
        ("CLEANING STRUCTURE (BENT)", "other"),
        ("Seagate 16TB HDD Seagate ST16000NM001G", "other"),
        ("CLEAN / SWEEP-OUTSIDE MAIN LANE-AREA(1)", "other"),
        ("Clean Out Existing 10' x 5' Box Culvert", "cleaning_desilting"),
        ("DITCH CLEANING AND RESHAPING (FOOT)", "cleaning_desilting"),
        ("Remove Sanitary Sewer Clean Out", "demo_other"),
        ("SPOT CLEAN & PNT EXT STRUCTURE (SPL PROT SYS)", "other"),
        ("Remove Temporary Sediment Control Fence", "erosion_control"),
        ("Remove Temporary Sediment Control Inlet Protection", "erosion_control"),
        ("DEBRIS REMOVAL (SPOT DEBRIS)", "other"),
        ("Pre-Construction Clean & CCTV Sanitary Sewer", "testing_inspection"),
        ("Clean and Paint Existing Structure (System II)", "painting"),
        ("Reset and Clean Existing Grate Inlet", "inlet_structure"),
        ('4" Sanitary Sewer Service with Cleanout to HDPE Sewer Main', "sewer_service"),
        ("Clearing and Grubbing including removal of debris within channel", "site_prep_clearing"),
        ("Remove Existing 24\" RCP", "demo_other"),
        ("Pre-Dredging Bathymetric Survey", "surveying"),
        ("Hydrostatic Test Existing 20\" Force Main", "testing_inspection"),
        # 2. trenchless crossings
        ('Furnish & Install 18-Inch PVC Force Main Pipe by Horizontal Directional Drilling with Restrained Joints',
         "encasement"),
        ('24" Fusible PVC (AWWA C900, DR-18, Purple) Pipe by Directional Drill', "encasement"),
        ('16" DR 18 PVC Water Line via Trenchless Method', "encasement"),
        ('42" Steel Casing by Trenchless Construction (0.25" Min Thick)', "encasement"),
        ('24" Class III RCP Installed by Boring', "encasement"),
        ("Jack and Bore 30 in Steel Casing", "encasement"),
        ("JACK BOR OR TUN PIPE (24 IN RC CLASS III)", "encasement"),
        ("JACK BOR TUN BOX CULV (6 FT X 4 FT)", "box_culvert"),
        ('4" PVC Conduit Bored', "encasement"),
        ('54" X 0.50" STEEL CASING AND BORING UNDER EXISTING IRRIGATION LINE. TO INCLUDE ALL EXCAVATION, BORING '
         'PITS, DEWATERING', "encasement"),
        ("OUTLET STRUCTURE #1 - 5'X4' RCB BY TRENCHLESS", "box_culvert"),
        ("1\" Bored Water Service", "water_service"),
        ("Soil Borings and Geotechnical Report", "testing_inspection"),
        ('8" PVC Water Line (Open Cut)', "water_pipe"),
        # 3. force mains
        ('8" HDPE Force Main', "sanitary_pipe"),
        ('Furnish & Install 18-Inch PVC Force Main Pipe by Open Cut with Class "B" Embedment', "sanitary_pipe"),
        ('24" C-905 DR 18 PVC Force Main', "sanitary_pipe"),
        ('Cut, Plug and Abandon Existing 12" Force Main', "demo_utility"),
        ("Connect 12\" Force Main to Existing Sanitary Sewer Manhole", "manhole"),
        # 4. grading
        ("Riffle Micro-Grading", "other"),
        ("Site Grading", "grading"), ("ROW Grading", "grading"), ("Fine Grading (berm)", "grading"),
        ("Grade Drainage Swale and Construct Berm", "grading"), ("Grade Ditch along Reagan Road", "grading"),
        ("Grade Ex. Ditch to Drain", "grading"), ("Type A Grade 1 Flex Base", "flex_base"),
        ("Adjust Manhole to Grade", "adjust_to_grade"),   # round 8: adjust_to_grade (was relocate_reset)
        ("Unclassified Excavation and Grading", "excavation"),
        # 5. bed preparation, 6. irrigation
        ("Turf Bed Preparation (Labor)", "seeding_sod"), ("Plant Bed Preparation", "seeding_sod"),
        ("Temporary Irrigation", "irrigation"), ("Irrigation System", "irrigation"),
        ("Drip Irrigation", "irrigation"), ('2" Irrigation Service', "irrigation"),
        ("Sod and Temporary Irrigation", "seeding_sod"), ("Irrigation Repair Allowance", "contingency"),
        ("Irrigation Valves and associated connection to the irrigation controller", "valve_hydrant"),
        ("Restoration of Parkways and Yards (No Irrigation)", "seeding_sod"),   # round 8 (was other)
        ("Option 1: Grand total for replacing the existing dry pipe sprinkler system", "other"),
        ("Repair/Adjustment of Sprinkler Systems", "irrigation"),
        ("Tree Relocation, complete in place including irrigation.", "other"),
        # 7. railing
        ("Furnish & Install Pedestrian Handrail", "railing"), ("Railing (Type C411-Mod)", "railing"),
        ("Pedestrian Rail (Trail)", "railing"), ("Pedestrian Handrail Type B", "railing"),
        ("RAIL (TY T223)(HPC)", "railing"), ("Pedestrian Rail (Bridge) Including Seven Stone Columns", "railing"),
        ("Temporary 5-Strand Wire Fence W/Metal Posts", "fence"),
        ("6' Tall Classic Point 2-Rail Ornamental Aluminum Fence", "fence"),
        ("Repaint Pedestrian Rail", "painting"),
        ("Existing Bridge Pedestrian Rail Cleaning and Painting (System 1-A), Two Bridges", "painting"),
        ("Water Blast Existing Bridge Rail Paint (TXDOT Item 446)", "painting"),
        ("Pedestrian Rail Repair (Including Paint)", "railing"),
        ("LANDSCAPE MAINTENANCE FOR SCH 9 LANDSCAPE AND IRRIGATION (BASE BID)", "other"),
        ("Irrigation System Maintenance, work fully performed and in place", "irrigation"), ("Remove Existing Steel Ped Railing", "demo_other"),
        ("Pedestrian Guardrail Safety Railing 42 in", "guardrail"), ("In Grade Rail Track", "fence"),
        ("Concrete Foundation for Pedestrian Handrail Ty E", "concrete_misc"),
        ("Concrete Sidewalk Thickened Edge For Ped. Rail", "sidewalk"),
        ("Trail - Boardwalk - Metal Grating and Handrails 8 ft Width", "other"),   # round 12: a structure (was sidewalk)
        # 8. right-of-way preparation
        ("Preparing Right-Of-Way", "site_prep_clearing"), ("Right of Way Preparation", "site_prep_clearing"),
        ("Preparing Right of Way (Tree Removal 0\" to 12\" Dia)", "tree_removal"),
        # 9. tie backs
        ("CLEAN AND RESEAL JOINTS IN CONCRETE SLOPE PROTECTION", "conc_joints"),
        ("CLEAN VEGETATION FROM JOINTS IN CONCRETE SLOPE PROTECTION", "conc_joints"),
        ("Crack Sealing", "surface_treatment"),
        ("STEEL TIE BACKS", "rock_anchor"), ("Tiebacks (Bonded) including steel plates with anchor nuts", "rock_anchor"),
        ("Reinforced Concrete Gabion Tie Back Beam", "gabion_wall"),
    )
    for d, want in cases:
        check("family(%r)" % d, fam(d), want)
    # 10. conduit EA x 500+
    for d, q, want in (('CONDT PVC SCHD 40 (6")', 1240.0, True), ('CONDT (PVC) (SCHD 40) (3")', 355.0, True),
                       ('CONDT PVC SCHD 40 (6")', 12.0, False), ("Cable Splice Kit", 150.0, False),
                       ("Ground Box Type A", 900.0, False), ("Remove tree", 900.0, False),
                       ('24" DIP Water Line', 300.0, False)):
        dn = norm_desc(d)
        check("EA x %g %r suspect" % (q, d), ea_quantity_suspect(
            {"unit": "EA", "quantity": q, "desc_norm": dn, "item_family": item_family(dn)}), want)
    # 11. a lump sum printed with a measure unit
    def row(fam_, unit, q, up, ee=False, item="46"):
        return {"src_file": "f", "src_sheet": "s", "bid_section": "", "item_no": item, "desc_norm": "d",
                "unit": unit, "quantity": q, "unit_price": up, "item_family": fam_, "is_engineer_estimate": ee}
    rs = [row("grading", "SY", 1.0, p) for p in (3000.0, 9000.0, 17000.0)] + [row("grading", "SY", 1.0, 50.0, True)]
    check("grading SY x 1 at $3k-17k is a lump sum", len(measure_qty1_lump_sums(rs)), 1)
    check("grading SY x 770 is not", len(measure_qty1_lump_sums([row("grading", "SY", 770.0, 25.0)])), 0)
    check("grading SY x 1 at $40 is not", len(measure_qty1_lump_sums([row("grading", "SY", 1.0, 40.0)])), 0)
    check("box_culvert LF x 1 at $13,750 is not (family)",
          len(measure_qty1_lump_sums([row("box_culvert", "LF", 1.0, 13750.0)])), 0)
    check("mixed quantities are not", len(measure_qty1_lump_sums(
        [row("grading", "SY", 1.0, 9000.0), row("grading", "SY", 2.0, 4500.0)])), 0)
    for f in fails:
        print("FAIL ed-fix", f)
    print("ed-fix (round 6): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _audit_fixes_selftest():
    """The generic rules from the 2026-11-20 tail audit (PARSER_VERSION .25)."""
    ok = True
    n = 0

    def check(name, got, want):
        nonlocal ok, n
        n += 1
        if got != want:
            ok = False
            print("FAIL audit-fix %s\n  got  %r\n  want %r" % (name, got, want))

    # a. a BASE BID banner glued onto item 1 keeps item 1 (Frisco / Quiddity)
    grid = [["", "", "", "", "BID NO. 1 KING GEORGE TRAFFIC SOLUTIONS", "", "BID NO. 2 GROD CONSTRUCTION, LLC", ""],
            ["ITEM NO.", "DESCRIPTION", "UNIT", "PLAN QUANTITY", "UNIT PRICE", "TOTAL AMOUNT",
             "UNIT PRICE", "TOTAL AMOUNT"],
            ["1.", "BASE BID Mobilization (5% Max of Base)", "L.S.", "1", "$30,000.00", "$30,000.00",
             "$48,000.00", "$48,000.00"],
            ["2.", "General Site Preparation", "L.S.", "1", "$5,371.71", "$5,371.71",
             "$27,000.00", "$27,000.00"],
            ["", "TOTAL BASE BID", "", "", "", "$35,371.71", "", "$75,000.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "glued-base", {})
    got = sorted((r["bidder_raw"], r["description"], r["bid_section"]) for r in recs
                 if r["description"].startswith("Mobil"))
    check("glued BASE BID keeps item 1", got,
          [("GROD CONSTRUCTION, LLC", "Mobilization (5% Max of Base)", "BASE BID"),
           ("KING GEORGE TRAFFIC SOLUTIONS", "Mobilization (5% Max of Base)", "BASE BID")])
    check("glued BASE BID: 2 items per bidder", len(recs), 4)
    for s, want in (("BASE BID Mobilization", "Mobilization"), ("Base Bid: Clearing and Grubbing", "Clearing and Grubbing"),
                    ("BASE BID TOTAL", None), ("Base Bid Amount", None), ("Base Bid + Alt 1", None),
                    ("BASE BID ITEMS", None), ("Base bid with alternates", None)):
        m = GLUED_BASE_RE.match(s)
        check("GLUED_BASE_RE %r" % s, m.group("rest") if m else None, want)
    # b. a rank caption in front of the firm goes; alone it stays
    for s, want in (("BID NO. 1 - LOWEST BASE BID FLOW-LINE CONSTRUCTION, INC.", "FLOW-LINE CONSTRUCTION, INC."),
                    ("Apparent Low Bidder: Texas Civil Construction", "Texas Civil Construction"),
                    ("BID NO. 2 WILSON CONTRACTOR SERVICES, LLC", "WILSON CONTRACTOR SERVICES, LLC"),
                    ("Low Bidder", "Low Bidder"), ("Lowest Bid", "Lowest Bid"),
                    ("Low Bid Construction LLC", "Construction LLC")):
        check("strip_bidder_caption %r" % s, strip_bidder_caption(s), want)
    # c. a unit cell that repeats the quantity, proved by the extension
    def row(u, q, p, e, **k):
        return dict({"unit_raw": u, "quantity": q, "unit_price": p, "extended_reported": e}, **k)
    check("unit cell 58 = qty 58, 58 x 3700 = 214600", unit_cell_repeats_quantity(row("58", 58.0, 3700.0, 214600.0)), True)
    check("unit cell 17,250 = qty", unit_cell_repeats_quantity(row("17,250", 17250.0, 3.0, 51750.0)), True)
    check("unit cell not the quantity (shifted column)", unit_cell_repeats_quantity(row("12", 58.0, 3700.0, 214600.0)), False)
    check("no extension to prove it", unit_cell_repeats_quantity(row("58", 58.0, 3700.0, None)), False)
    check("arithmetic disagrees", unit_cell_repeats_quantity(row("58", 58.0, 3700.0, 21460.0)), False)
    check("a word unit is not this rule", unit_cell_repeats_quantity(row("EA", 58.0, 3700.0, 214600.0)), False)
    # d. a numbered schedule titled as an alternate opens an alternate
    for s, alt in (("SCHEDULE 13 -- LANDSCAPE AND IRRIGATION ADD ALTERNATE L1", True),
                   ("SCHEDULE 20 -- DRAINAGE ALTERNATE 1", True),
                   ("SCHEDULE 19 -- ONCOR UG NETWORK LINE DUCT BANK PROTECTION ADD ALTERNATE", True),
                   ("SCHEDULE 9 - LANDSCAPE AND IRRIGATION - BASE BID", False),
                   ("SECTION II - PAVING", False),
                   ("SCHEDULE 2 - BASE BID WITH ALTERNATE POLES", False)):
        lab = section_banner(s)
        check("alternate-titled schedule %r" % s, bool(lab) and bool(re.search(r"\balt", lab, re.I)), alt)
    # e. the one item every bidder's printed total leaves out (Southlake B-02)
    def it(b, ino, e, sh="p4t0"):
        return {"src_file": "f", "src_sheet": sh, "section_role": "base", "bidder_raw": b,
                "item_no": ino, "desc_norm": ino, "extended_reported": e, "unit_price": e,
                "quantity": 1.0}
    rs = ([it("T", "A", 6695240.0), it("T", "B-01", 1776704.0), it("T", "B-02", 1029301.0),
           it("I", "A", 7401058.0), it("I", "B-01", 1861186.0), it("I", "B-02", 1517530.0)])
    facts = {"f": {"total_lines": sorted({847194400, 926224400, 669524000, 740105800})}}
    check("item outside every printed total", items_outside_printed_total(rs, facts),
          {("f", "B-02", "B-02")})
    # ... per FILE: a page holding an alternate block and one base line must
    # not match the alternate's printed total less that line (Jupiter Rd)
    jr = ([it("M", "1", 289548.28, "p1"), it("M", "129", 25000.0, "p2"), it("M", "A1", 123027.02, "p2"),
           it("I", "1", 341918.0, "p1"), it("I", "129", 25000.0, "p2"), it("I", "A1", 118600.0, "p2")])
    facts_j = {"f": {"total_lines": sorted({31454828, 36691800, 12302702, 11860000, 43757530, 48551800})}}
    check("a sheet's part-sum is not the file's total", items_outside_printed_total(jr, facts_j), set())
    facts2 = {"f": {"total_lines": sorted({950124500, 1077977400})}}      # totals printed as read
    check("totals already printed: nothing moves", items_outside_printed_total(rs, facts2), set())
    facts3 = {"f": {"total_lines": sorted({847194400, 123})}}              # only one bidder fits
    check("one bidder fitting is not proof", items_outside_printed_total(rs, facts3), set())
    # f. the on-call hint
    check("on-call title", contract_hint(["City of Frisco", "On-Call Concrete Repair", "Bid 2506-096"]), "on_call")
    check("annual contract title", contract_hint(["ANNUAL CONTRACT FOR STREET REPAIRS"]), "on_call")
    check("'as needed' in an item is not a title",
          contract_hint(["BID TABULATION", "1 Sod replacement, as needed 2 EA"]), "")
    check("a packet's later agenda item is not this tab's title",
          contract_hint(["AGENDA"] + ["item %d" % i for i in range(10)]
                        + ["5. Annual contract for the purchase of irrigation"]), "")
    check("a project title", contract_hint(["3RD STREET AND MAPLE STREET PAVING"]), "")
    # f2. a bare TOTAL label takes the amounts printed above it (Riverfront)
    check("bare TOTAL label, amounts above",
          sorted(total_label_amounts(["$99,196,865.85 $103,984,035.18 $114,401,072.10", "TOTAL BID:",
                                      "SCHEDULE 13 -- LANDSCAPE ADD ALTERNATE L1"])),
          [9919686585, 10398403518, 11440107210])
    check("a TOTAL label with its own amounts is total_line_amounts' job",
          sorted(total_label_amounts(["$5.00 $6.00", "TOTAL BID $1,000.00 $2,000.00"])), [])
    check("an item line above a bare label is not its amounts",
          sorted(total_label_amounts(["12 Sod LF 100 $5.00 $500.00", "TOTAL"])), [])
    # g. TxDOT's A+B time item, in both feeds' spellings
    check("A+B item, Socrata", is_time_bid_item("800-7001", "$/D", "NO. OF WORKING DAYS"), True)
    check("A+B item, archive", is_time_bid_item("8006001", "$/D", "NO. OF WORKING DAYS"), True)
    check("800 code, other unit", is_time_bid_item("800-6001", "LS", "MOBILIZATION"), False)
    check("a different item", is_time_bid_item("500-6001", "$/D", "MOBILIZATION"), False)
    check("800-60011 is not 800-6001", is_time_bid_item("80060011", "$/D", ""), False)
    print("audit-fix rules (2026-11-20): %d checks, %d failure(s)" % (n, 0 if ok else 1))
    return ok


# (description as printed, family it must get).  Round 3b (reports: parser
# round 3b): the MAIN noun phrase decides, a remove-and-replace item is what
# it builds, and the vocabulary each rule was missing -- with the look-alikes
# each rule must leave alone.
_FAMILY_CASES = (
    # 1. a subordinate clause does not decide the family
    ('8" water main w/ fittings, trenching, trench safety protection, embedment, '
     'backfilling, and surface restoration', "water_pipe"),
    ('12" water main including fittings, trenching, trench safety protection, embedment, '
     'backfilling, and surface restoration', "water_pipe"),
    ("Concrete Under-Crossing Trail System (including slab, walls, structural excavation "
     "and backfill, and any required saw joints and expansion joints)", "other"),
    ("Unclassified Excavation (including existing HMAC)", "excavation"),
    ('FURNISH & INSTALL NEW FIRE HYDRANT INCLUDING 6" FIRE HYDRANT LEADS, CONNECTIONS, '
     "FITTINGS, EMBEDMENT, BACKFILL", "fire_hydrant"),   # round 8: fire_hydrant (was valve_hydrant)
    ('18" Class III RCP Storm Drain including CCTV Inspection', "storm_pipe"),
    ('Riprap (Concrete)(6")(Including Toe Wall)', "riprap_conc"),
    ("Inlet (Complete)(Wye)(3' X 3') With Rip-Rap Apron", "inlet_structure"),
    ('4" Concrete Sidewalk with Integral Curb', "sidewalk"),
    ("8-inch Reinforced Concrete Access Ramp over 8-inch Flexbase", "barrier_free_ramp"),
    #    ... but a qualifier that changes WHAT the item is still counts
    ("Turf Reinforcement Mat with Sod", "trm_bundled"),
    ("Placement of Turf Reinforcing Mat, including percussion driven anchors", "trm_permanent"),
    ("Construction Entrance including installation, maintenance and removal",
     "construction_exit"),
    ('FURNISH & INSTALL 8" SDR-14 CLASS 200 PVC WATER BY JACK & BORE WITH STEEL CASING '
     "INCLUDING FITTINGS & TESTING", "encasement"),
    ("Sloping Headwall with Safety End Treatment for 24in (RCP)", "safety_end_treatment"),
    ("Sod Restoration Including Topsoil (Allowance)", "contingency"),
    #    ... and a head naming nothing the rules know keeps the old reading
    ("INSTALL (COMPLETE IN PLACE) 6' SIDEWALK", "sidewalk"),
    ("Right of Way Preparation (including Clearing and Grubbing)", "site_prep_clearing"),
    # 2. remove-and-replace is the thing built; a pure removal stays demolition
    ('Remove and Replace 8" Thick, 4,000 PSI Reinforced Concrete Pavement, Street, '
     'Includes 6" Integral Curb, Complete in Place', "conc_pavement"),
    ('Remove & Replace 7" Thick Concrete Pavement', "conc_pavement"),
    ("Reinforced Concrete Pavement (8\"-10\") (Remove and Replace)", "conc_pavement"),
    ("Remove and replace 4' sidewalk", "sidewalk"),
    ("Remove & Replace Reinforced Concrete Curb & Gutter", "curb_gutter"),
    ("Remove and Replace Inlet top", "inlet_structure"),
    ("Remove & Replace ADA Compliant Barrier Free Ramp (TxDOT PED-18)", "barrier_free_ramp"),
    ("Remove and Replace Existing Trees", "tree_install"),
    ("REMOVE AND RECONSTRUCT HEADWALL", "headwall"),
    ("Remove Existing Sidewalk Replace With 4-Inch Thick Reinforced Concrete Sidewalk "
     "(Including 2-Inch Thick Compacted Cushion Sand)", "sidewalk"),
    ("Replace Existing Cleanout with 4' Dia Manhole", "manhole"),
    ('Remove and replace 8" thick concrete pavement, street with 6" monolithic curb, '
     "complete in place", "conc_pavement"),
    ("Remove and replace 4-inch concrete sidewalk associated with the retaining wall "
     "(optional)", "sidewalk"),
    ('Remove and Replace Curb and Gutter with 6" thick 30" wide 4,000 psi reinforced '
     "concrete, complete in place", "curb_gutter"),
    ("REMOVE AND REPLACE (TRACC)", "crash_cushion"),
    ("REPAIR (SMTC) (N)", "crash_cushion"),
    ("REMOVE & REPLACE BLOCKOUT", "guardrail"),
    ("CAT 8A-R ROAD SALT (DEL)(SITE 1)", "other"),
    # material supply (.24b): delivered / picked-up material is not built work
    ("FL BS(TY A GR 4)(DEL)(ST1)", "other"),
    ("FL BS (SITE DEL) (TY A GR 4)", "other"),
    ("FL BS (RDWY DEL)(TY D GR 1-2)(FNAL POS)", "other"),
    ("ASPH PATCH MAT (DEL)(SITE 1)", "other"),
    ("RIPRAP (DEL)(STONE PROT)(18 IN)(SITE 1)", "other"),
    ('RIPRAP(STN CMN DRY)(3"-5")(DEL)(ST 2)', "other"),
    ("DELIVERED Zone 1 - Flexible Base (Grade 1, Type A)", "other"),
    ("PICKUP AT PLANT SITE - Flexible Base (Grade 1, Type A)", "other"),
    ("4000 PSI CONCRETE, CITY TO PICKUP, ALL ORDER SIZES", "other"),
    ("3000 P.S.I. Ready Mix Concrete (Plant Pickup by the City)", "other"),
    ("Cement Treated Base (CTB)- Price Per Ton - Pick up from vendor site", "other"),
    # pavement milling / surface treatment (.24c)
    ('PLANE ASPH CONC PAV(0" TO 2")', "pavement_milling"),
    ("PLAN & TEXT ASPH CONC PAV (0-2\")", "pavement_milling"),
    ('PLANE CONC PAV(2")', "pavement_milling"),
    ("Milling of 2 in thick asphaltic concrete overlay", "pavement_milling"),
    ("2 in asphalt surface milling", "pavement_milling"),
    ("Mill Asphalt Concrete Pavement 0 in to 2 in", "pavement_milling"),
    ('Milling and Disposing of Existing Asphalt Overlay (2")', "pavement_milling"),
    ("Mill and Overlay 2 in HMAC Type D", "asphalt_pavement"),
    ("Mill & Overlay Asphalt Pavement", "asphalt_pavement"),
    ("Asphalt Pavement Milling and Overlay", "asphalt_pavement"),
    ('Wedge Mill Edges of Pavement and 4" Overlay', "asphalt_pavement"),
    ('8" Cement Treated Milled Asphalt (Conc Pavement)', "subgrade_stab"),
    ("MILL RUMBLE STRIPS (ASPH SHOULDER)", "guardrail"),
    ("Furnish and install new 2 in HMAC surface, Type D", "asphalt_pavement"),
    ("Concrete Pavement Repair on Mill Creek Dr", "conc_pavement"),
    ("TACK COAT", "surface_treatment"),
    ("FOG SEAL (CSS-1H)", "surface_treatment"),
    ("SRM 12 Asphalt Prime Coat AEP or MC 30 for Chip Seal", "surface_treatment"),
    ("Microsurfacing Type III 24-26 Pounds per Square Yard", "surface_treatment"),
    ("Crack Sealing", "surface_treatment"),
    ("SEAL COAT (ONE COURSE SURFACE TREATMENT)", "surface_treatment"),
    ('Hot Mix Asphalt Type D, 2" including tack coat', "asphalt_pavement"),
    ('6" flex base (compact in place) (type D) (grade 1-2) and prime coat', "flex_base"),
    ("D-GR HMA TY D PG 64-22 (PICKUP)", "other"),
    ("D-GR HMA(SQ) TY C SAC A PG70-28(DEL)", "other"),
    ("D-GR HMA(SQ) TY D PG 64-22 (DELIVERY)", "other"),
    ("MOBILIZATION (INCLUDING DELIVERY)", "mobilization"),
    ('FL BS (CMP IN PLC)(TY D GR 1-2) (12")', "flex_base"),
    ("INSTL DEL ASSM (D-SW)SZ 1(FLX)GND(BI)", "other"),
    ('For four inch (4") top soil delivered, placed and compacted', "seeding_sod"),
    ("REMOVE (NARROW TRACC)", "demo_other"),
    ("REM & RESET SGT IMPACT HEAD", "relocate_reset"),
    ("Remove & Reset Existing Sign", "relocate_reset"),
    ("Ex Sign Remove & Replace (Various Material)", "signing"),
    ('FULL DEPTH SAWCUT, REMOVE & DISPOSE EXIST. FULL DEPTH ASPHALT PAVEMENT & REPLACE WITH '
     '8" THICK REINFORCED CONCRETE, FOR SERVICE LINES, IF USED', "conc_pavement"),
    ("REMOVE AND REPLACE ASPHALT PAVEMENT", "asphalt_pavement"),
    ("Remove, Salvage and Replace Rock Rip Rap Bank Protection", "relocate_reset"),
    ("Remove and Reinstall Fire Hydrant", "relocate_reset"),
    ("Remove Existing Concrete Pavement", "demo_pavement"),
    ('Remove existing 18" RCP', "demo_other"),
    ('Remove Existing Concrete, Subgrade or Subbase (To be replaced with Asphalt) (8")',
     "demo_other"),
    ("Remove Concrete Drainage Flume with Curb Wall", "demo_other"),
    ("Sawcut, Remove and Dispose of Concrete (Alley)", "demo_other"),
    ("Concrete Alley Removal (Including Ex. Curb)", "demo_other"),
    ("REMOVE LUMINAIRE POLE", "demo_other"),
    ("REMOV STR (BOX CULVERT)", "demo_utility"),
    # 3. vocabulary: box culverts, lighting, waterline, end structures
    ("462 - 6'x1.5' RCB (CIP WITH CLASS S TOP SLAB)", "box_culvert"),
    ("CONC BOX CULV (5 FT X 3 FT)", "box_culvert"),
    ("5'x3' Reinforced Concrete Box Culvert", "box_culvert"),
    ("6' X 4' SBC", "box_culvert"),
    # (round 6, .27) cleaning a box culvert is cleaning, not the pipe it names
    ("STORM SEWER CLEAN (BOX CULV)(24-<48 SF)", "cleaning_desilting"),
    ("Headwall and Wingwall at 3-10' x 5' CBC", "headwall"),
    ("HAPCO YORK LIGHT POLE (14 FT) W/ HAGERSTOWN TX03-C POST TOP", "lighting_assembly"),
    ("ROADWAY LIGHTING ASSEMBLY (TY 1)", "lighting_assembly"),
    ("IN RD IL (TY SA) 40T-8 (250W EQ) LED", "lighting_assembly"),
    ("Luminaire Pole (Dual Arm) (30' with 4' Arms)", "lighting_assembly"),
    ("Install street light pole", "lighting_assembly"),
    ("Roadway Lighting Assembly (Led Rectangular) Foundation", "electrical_lighting"),
    ("REPLACE LUMINAIRE W/LED (250W EQ)", "electrical_lighting"),
    ("90W LED FIXTURE FOR LIGHT POLES", "electrical_lighting"),
    ("ILLUM FND TY A", "electrical_lighting"),
    ("DRILL SHAFT (RDWY ILL POLE) (30 IN)", "drill_shaft"),
    ("RELOCATE RD IL ASM (TRANS-BASE)", "relocate_reset"),
    ("PED POLE ASSEMBLY", "traffic_signal"),   # round 8: a pedestrian signal pole (was other)
    ('Replace Existing Waterline with 8" Waterline by Open Cut', "water_pipe"),
    ('8" C-900 PVC Waterline', "water_pipe"),
    ('3/4" Waterline Service Including New Meter Can and Adjustment (Short Side)',
     "water_service"),
    ('8" SDR-14 CLASS 200 PVC WATER BY OPEN CUT INCLUDING FITTINGS & TESTING', "water_pipe"),
    ('8" SDR 26 Sanitary Sewer', "sanitary_pipe"),
    ("Remove and replace storm water pipe 36 in", "storm_pipe"),
    ('Stormwater Pipeline - 12" Dia. HDPE, Complete In Place', "storm_pipe"),
    ("466 - HEADWALLS, WINGWALLS AND RIPRAP APRON", "headwall"),
    ("Rock Riprap Apron at Headwall", "riprap_stone"),
    ("Drive Approach Parallel Hdwl (4-6'x4' Box Culvert)", "headwall"),
    ('4" ARV AND LEAD LINE (INCLUDING TO EX. MH)', "valve_assembly"),   # round 8 (was valve_hydrant)
    # 4. old patterns that fired inside other words
    ("REMOVE INLET", "demo_utility"),
    ("Remove Existing Concrete Street Pavement", "demo_pavement"),
    ("Furnish and install New Street Sign", "signing"),
    ("Furnish and Install Project Sign", "project_sign"),
    ("Mobilization", "mobilization"),
    ('FL BS (CMP IN PLC)(TY D GR 1-2) (6")', "flex_base"),
    ('24" CMP', "storm_pipe"),
    ("18-inch Class III Reinforced Concrete Pipe", "storm_pipe"),
    ("CURB RAMPS (TY 7)", "barrier_free_ramp"),
    ("DEBRIS REMOVAL (ENTRANCE/EXIT RAMPS)", "other"),
    ("Construct ADA Ramp, Type D", "barrier_free_ramp"),
    ('2" HMAC Surface Course (Type D)', "asphalt_pavement"),
    ("GATE (TY 1)", "fence"),
    ("Gate Valve (8 In)", "valve_hydrant"),
    ("Silt Fencing", "erosion_control"),
    ('Temporary Asphalt Paving Repair (2" HMAC on 6" Flexible Base Course)',
     "asphalt_pavement"),
    ('6" Flexible Base under Concrete Pavement', "flex_base"),
    ('Hot Mix Asphaltic Concrete Pavement (Type "D") (2" Thick)', "asphalt_pavement"),
    ("Furnish & Install 6\" Concrete Drive Approach", "driveway"),
)


def _diag_fixes_selftest():
    """The generic rules of parser round 5 (PARSER_VERSION .26), from
    reports/diag_worst_municipal_2026-11-20.md (2, 4) and
    reports/statewide_integration_test.md (4, 6-7)."""
    fails = []
    n = [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    fam = lambda s: item_family(norm_desc(s))
    size = lambda s: extract_size_in(norm_desc(s))
    # 1. item numbers in descriptions; sizes that cannot be sizes
    rows = []
    for i, d in enumerate(["1Mobilization/Bonds/Insurance", "2Traffic Control", "3Manhole",
                           "4Inlet Type A", "5Inlet Type CC", "6Type A Inlet w/ Extension",
                           '724" R.C.P. Class III Rubber Gasket', '818" R.C.P. Class III Rubber Gasket',
                           '918" R.C.P. Safety End Treatment', "10SW3P Plan & Implementation",
                           "13Stabilized Backfill", "1Cut & Restore", "2Remove Curb & Gutter"]):
        for b in ("A", "B"):
            dn = norm_desc(d)
            rows.append({"src_file": "t", "src_sheet": "s", "item_no": "", "description": d,
                         "desc_norm": dn, "item_family": item_family(dn), "size_in": extract_size_in(dn),
                         "material": "", "bidder_raw": b})
    strip_leading_item_numbers(rows)
    got = {r["item_no"]: (r["description"], r["item_family"], r["size_in"]) for r in rows if r["bidder_raw"] == "A"}
    check("glued item 7: 24 in storm pipe", got.get("7"),
          ('24" R.C.P. Class III Rubber Gasket', "storm_pipe", 24.0))
    check("glued item 9: 18 in safety end", got.get("9"),
          ('18" R.C.P. Safety End Treatment', "safety_end_treatment", 18.0))
    check("glued item 13: stabilized backfill", got.get("13"),
          ("Stabilized Backfill", "flowable_backfill", None))
    check("glued item 1: mobilization", got.get("1")[0] if got.get("1") else None, "Cut & Restore")
    codes = [{"src_file": "t", "src_sheet": "c", "item_no": d, "description": d, "desc_norm": norm_desc(d),
              "item_family": "", "size_in": None, "material": "", "bidder_raw": "A"}
             for d in ["101S-B", "102S-C", "103S-A", "104S-G", "110S-B"]]
    check("spec codes are no glued numbers", strip_leading_item_numbers(codes), 0)
    lew = [{"src_file": "t", "src_sheet": "l", "item_no": "", "description": d, "desc_norm": norm_desc(d),
            "item_family": "", "size_in": None, "material": "", "bidder_raw": "A"}
           for d in ['#0-12Remove 6" curb', '#0-13Remove sidewalk', '#0-146" (3750 p.s.i.) concrete for pavement',
                     '#0-15Saw cut full depth concrete', '#0-16Barrier free ramp']]
    strip_leading_item_numbers(lew)
    check("hash code glued to a size", [(r["item_no"], r["size_in"], r["item_family"]) for r in lew][2:4],
          [("#0-14", 6.0, "concrete_misc"), ("#0-15", None, "sawcut")])
    sizes = [{"src_file": "t", "src_sheet": "p", "item_no": str(i + 1), "description": d,
              "desc_norm": norm_desc(d), "item_family": "", "size_in": None, "material": "",
              "bidder_raw": "A"} for i, d in enumerate(['12" RCP', '24" RCP', '36" RCP', '48" RCP', "4 Each Inlet"])]
    strip_leading_item_numbers(sizes)
    check("a sheet of sizes keeps its sizes", [r["description"] for r in sizes],
          ['12" RCP', '24" RCP', '36" RCP', '48" RCP', "4 Each Inlet"])
    check("size > 144 in is no size", size('EXTEND 184" RCP W/45 DEGREE BEND'), None)
    check("decimal read as integer skipped", size('Bore for 12-in PVC Pipe (T=0.281")'), 12.0)
    check("ordinary size kept", size('8" PVC Water Line'), 8.0)
    check("0-6 in range kept", size('REMOVING STAB BASE & ASPH PAV (0-6")'), 6.0)
    check("three-place decimal skipped", size('Steel Casing (T=0.125") for 12" Pipe'), 12.0)
    gap = [{"src_file": "t", "src_sheet": "g", "item_no": "", "description": d, "desc_norm": norm_desc(d),
            "item_family": "", "size_in": None, "material": "", "bidder_raw": "A"}
           for d in ["#0-1 Remove curb", "#0-2 Remove sidewalk", "#0-3 Remove drive", '#0-46" curb',
                     "#0-12Barrier free ramp", '#0-1324 inch curb and gutter', "#0-14Sidewalk curb"]]
    strip_leading_item_numbers(gap)
    check("numbering resynchronised after a gap", [(r["item_no"], r["size_in"]) for r in gap][4:6],
          [("#0-12", None), ("#0-13", 24.0)])
    # 2. backfill split
    for d, want in (("BORROW SITE EXCAVATION", "embankment_fill"), ("Borrow (Minimum Bid $12/CY)", "embankment_fill"),
                    ("Flowable Backfill", "flowable_backfill"), ("Flowable Fill", "flowable_backfill"),
                    ("CLSM", "flowable_backfill"), ("CEM STABIL BKFL", "flowable_backfill"),
                    ("Cement Stabilized Sand", "flowable_backfill"), ("Trench Backfill", "borrow_backfill"),
                    ("Select Fill", "borrow_backfill"), ("Flowable fill for storm drain abandonment", "demo_utility"),
                    ("6 in Cement Stabilized Subgrade", "subgrade_stab")):
        check("family(%r)" % d, fam(d), want)
    # 3. grading
    for d, want in (("FINE GRADING (POND AND SITE)", "grading"), ("Final Grading", "grading"),
                    ("Unclassified Excavation and Grading", "excavation"), ("Grade Control Structure", "conc_beam")):
        check("family(%r)" % d, fam(d), want)
    # 4. box culverts by opening area (sq ft)
    for d, want in (("Reinforced Concrete Box Culvert (6'-0\" x 4'-0\")", 24.0), ("6'x4' RCB", 24.0),
                    ("RCB 6X4", 24.0), ("CONC BOX CULV (5 FT X 3 FT)", 15.0),
                    ("Reinforced Concrete Box (5' Span x 4' Rise), All Depths", 20.0),
                    ("4-6'x4' Box Culvert", 24.0), ("Precast 12X12 Box Culvert", 144.0),
                    ("(10'-6\" x 5'-0\") RCB", 52.5),
                    ("Precast Reinforced Concrete Box Culvert (111-120 SF Effective Opening)", 115.5), ('8" X 4" RCBC PLUG', None), ("Box Culvert Extension", None)):
        check("box size(%r)" % d, size(d), want)
    # 5. TV inspection
    for d, want in (("STORM SEWER (TV) INSPECTION", "testing_inspection"),
                    ("WASTEWATER (TV) INSPECTION", "testing_inspection"),
                    ("Pre- Construction TV Inspection", "testing_inspection"),
                    ('18" Class III RCP Storm Drain including CCTV Inspection', "storm_pipe"),
                    ("Video Recording Construction", "video_survey")):
        check("family(%r)" % d, fam(d), want)
    # 6. trenchless replacement
    for d, want in (("PIPE BURSTING", "pipe_bursting"), ('INSTALL 8" HDPE WATER LINE BY PIPE BURSTING', "pipe_bursting"),
                    ("8-inch HDPE DR17 Sewer Pipe (Trenchless Method by Pipe Bursting)", "pipe_bursting"),
                    ('Replace 6" sanitary sewer with 8" by pipe bursting', "pipe_bursting"),
                    ("Sanitary Sewer Service Installation (Pipe Bursting)", "sewer_service"),
                    ("Sliplining 24 in sewer", "pipe_bursting"), ('8" PVC Sanitary Sewer (Open Cut)', "sanitary_pipe")):
        check("family(%r)" % d, fam(d), want)
    # 7. quantity stated in an LS description
    r = {"unit": "LS", "quantity": 1.0, "unit_price": 1160000.0, "description": "Excavation (Roadway) (28,731 Cy)",
         "extended_reported": 1160000.0}
    check("LS (28,731 CY) converted", (apply_ls_quantity_in_description(r), r["unit"], r["quantity"],
                                       round(r["unit_price"] * r["quantity"], 2), r.get("unit_note")),
          (True, "CY", 28731.0, 1160000.0, "ls_quantity_in_description"))
    for d in ("Mobilization (5% max)", "Traffic Control (2 phases)", "Clearing (max 200 SY) (100 SY)",
              "Storm Water Pollution Prevention"):
        check("LS %r left alone" % d, apply_ls_quantity_in_description(
            {"unit": "LS", "quantity": 1.0, "unit_price": 5000.0, "description": d}), False)
    check("LS q > 1 left to clear_lump_sum_with_quantity", apply_ls_quantity_in_description(
        {"unit": "LS", "quantity": 3.0, "unit_price": 5.0, "description": "Fill (300 CY)"}), False)
    # 8. EA by the hundred on a pipe line
    for d, q, want in (('Grout and abandon existing 6" water line', 1145.0, True),
                       ('24" DIP Water Line', 943.0, True), ('24" DIP Water Line', 12.0, False),
                       ("Remove tree", 900.0, False)):
        dn = norm_desc(d)
        check("EA x %g %r suspect" % (q, d), ea_quantity_suspect(
            {"unit": "EA", "quantity": q, "desc_norm": dn, "item_family": item_family(dn)}), want)
    # 9. family misclassifications
    for d, want in (("PAVER FIELDS (RUNNING BOND)", "pavers"),
                    ("For Furnishing and Installation of Pavestone Concrete Paver, Running Bond Pattern", "pavers"),
                    ("Payment and Performance Bonds", "bonds_insurance"),
                    ("Furnish, Install, Fertilize, Water & Maintain Solid Sod", "seeding_sod"),
                    ('8" Water Main', "water_pipe"), ("12 in Waterline", "water_pipe"),
                    ("Saw cut full depth concrete", "sawcut"), ("For Sawcutting of Existing Pavement", "sawcut"),
                    ("Sawcut, Remove and Dispose Existing Asphalt", "demo_pavement"),
                    ("Repaint Pedestrian Rail", "painting"), ("Salvage & Repaint Light Pole and Fixtures", "relocate_reset"),
                    ("Extra Depth for 4' Dia Sanitary Sewer Manhole (Over 8 ft Deep)", "manhole_extra_depth"),
                    ("5' Extra Depth Manhole", "manhole_extra_depth"),
                    ("4' Sanitary Sewer Manhole Extra Depth", "manhole_extra_depth"),
                    ('30" AWWA C900, PVC Pipe (WH DI Fittings)', "water_pipe"),
                    ('30" AWWA C150, DI Pipe (WH DI Fittings)', "water_pipe"),
                    ("Roadway Subgrade (Varies 1-6 Inch) Cement Stabilized Sand", "subgrade_stab"),
                    ("PREDL 4' Dia Sanitary Sewer Drop Manhole (8 ft Deep or less) Extra Depth for 4' Dia", "manhole"),
                    ('Furnish & Install 12" Pipe C900 DR-18 PVC Pipe and Fittings', "water_pipe"),
                    ("Furnish and Install Ductile Iron Fittings", "pipe_fittings"),
                    ("MOBILIZAITON (INCLUDING PAYMENT AND PERFORMANCE BONDS)", "mobilization"),
                    ("Demobilization", "mobilization"), ("Automobile allowance", "contingency"),
                    ('24" R.C.P. Class III Rubber Gasket', "storm_pipe")):
        check("family(%r)" % d, fam(d), want)
    # 10. owner-set money, misspelt
    for d in ("Contignency", "Contingency", "UNFORSEEN ITEMS, AS APPROVED BY THE CITY ENGINEER",
              "Unforeseen items", "Construction Allowance", "Cash Allowances"):
        check("family(%r)" % d, fam(d), "contingency")
    for f in fails:
        print("FAIL diag-fix", f)
    print("diag-fix (round 5): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _family_rules_selftest():
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    for raw, want in _FAMILY_CASES:
        check("item_family(%r)" % raw, item_family(norm_desc(raw)), want)
    for f in fails:
        print("FAIL", f)
    print("family rules: %d checks, %d failure(s)" % (len(_FAMILY_CASES), len(fails)))
    return not fails


def _computed_column_selftest():
    """find_computed_columns on two synthetic tabs.  Tab A has Coppell's
    layout: the mean of four bidders printed BEFORE them, under a real
    contractor's name -- it must be found.  Tab B has no computed column, but
    one real bidder lands exactly on the mean of the other three in 3 of 14
    rows, and every column prices an owner-set allowance identically -- no
    column may be flagged.  (In tab A those 3 rows are not evidence either:
    there the mean of the others IS a real bidder's price, a tie.)"""
    a = [120.0, 18.25, 3500.0, 7.75, 640.0, 52.0, 1500.0, 33.0, 210.0, 88.0,
         4.5, 975.0, 61.0, 15000.0]
    b = [95.5, 22.0, 4100.0, 9.0, 585.0, 61.0, 1250.0, 29.5, 260.0, 74.0,
         6.25, 1100.0, 48.0, 12500.0]
    c = [140.0, 19.5, 2800.0, 12.0, 700.0, 47.5, 1800.0, 41.0, 199.0, 93.0,
         5.0, 890.0, 72.5, 18250.0]
    recs = []
    for tab in ("A", "B"):
        for i in range(len(a)):
            m3 = round((a[i] + b[i] + c[i]) / 3, 2)
            d = m3 if i in (1, 4, 8) else round(m3 * 1.13, 2)
            cells = [("Ed Bell", a[i]), ("Estrada", b[i]), ("McMahon", c[i]),
                     ("Tiseo", d)]
            if tab == "A":
                cells.insert(0, ("Axis Contracting",
                                 round((a[i] + b[i] + c[i] + d) / 4, 2)))
            for name, p in cells:
                recs.append({"src_file": "selftest", "src_sheet": tab,
                             "bid_section": "BASE BID", "item_no": str(i + 1),
                             "description": "item %d" % (i + 1), "unit_raw": "EA",
                             "quantity": 1.0, "bidder_raw": name, "unit_price": p})
        for name in ["Axis Contracting"] * (tab == "A") + ["Ed Bell", "Estrada",
                                                           "McMahon", "Tiseo"]:
            recs.append({"src_file": "selftest", "src_sheet": tab,
                         "bid_section": "BASE BID", "item_no": "11",
                         "description": "allowance", "unit_raw": "LS",
                         "quantity": 1.0, "bidder_raw": name,
                         "unit_price": 25000.0})
    got = sorted((f["src_sheet"], f["column"]) for f in find_computed_columns(recs))
    ok = got == [("A", "Axis Contracting")]
    print("find_computed_columns: %s  flagged %r" % ("ok" if ok else "FAIL", got))
    return ok



def _r3c_selftest():
    """Parser round 3 (agent C): rows that left the totals for want of a unit,
    a quantity column, a name, a section -- each rule on a small tab shaped
    like the public letting that broke, with the look-alikes it must leave."""
    fails = []
    E = ""

    def check(name, got, want):
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    # 1. unit spellings the corpus prints, and the ambiguous ones left as printed
    for raw, want in (("PER LINEAR", "LF"), ("PER LINEAR FOOT", "LF"), ("SQ FT", "SF"),
                      ("Sq. Ft.", "SF"), ("Sq. Feet", "SF"), ("SQ Yard", "SY"),
                      ("Squard Yard", "SY"), ("Week", "WK"), ("WEEK", "WK"), ("Wkly", "WK"),
                      ("weekly", "WK"), ("Hourly", "HR"), ("Hrs.", "HR"), ("VLF", "VF"),
                      ("MI", "MI"), ("pound", "LB"), ("LSUM", "LS"), ("SFF", "SF"),
                      ("ALL", "ALLOW"), ("monthly", "MO"), ("daily", "DAY"),
                      ("SQ", "SQ"), ("YD", "YD"), ("Yard", "YARD"), ("E", "E"), ("LT", "LT"),
                      ("TOTAL", "TOTAL"), ("Per truck", "PER TRUCK")):
        check("norm_unit(%r)" % raw, norm_unit(raw), want)
    for (raw, desc), want in ((("Yard", "#0-7 Remove 4 inch to 5 inch concrete sidewalk "
                                        "0-50 s.y."), "SY"),
                              (("YD", "Topsoil, 4 inch, c.y."), "CY"),
                              (("SQ", "Metal deck renovation per sq. ft."), "SF"),
                              (("YD", "Remove and replace existing driveway"), ""),
                              (("SQ", "Sod per square yard or per square foot"), ""),
                              (("SQ", "Excavation per cu yd"), ""),
                              (("YD", "Membrane, 10 sq ft laps"), ""),
                              (("LF", "Sidewalk 0-50 s.y."), "")):
        check("unit_from_description(%r, %r)" % (raw, desc), unit_from_description(raw, desc), want)

    # 2. Frisco 2402-066: 'Plan Quantity | Bid Quantity', the totals on the BID
    #    quantity; a tab whose two quantity columns agree keeps the first
    def tab(plan, bid):
        return [["Item", "Description", "Unit", "Plan Quantity", "Bid Quantity",
                 "Unit Price", "Total", "Unit Price", "Total"],
                [E, E, E, E, E, "KG Traffic", E, "McMahon", E],
                ["P05", "Portable Changeable Message Sign", "EA", plan[0], bid[0],
                 "$21,048.64", "$84,194.56", "$16,813.00", "$67,252.00"],
                ["P06", "Temporary 4\" Asphalt (Allowance)", "SY", plan[1], bid[1],
                 "$78.37", "$31,348.00", "$122.10", "$48,840.00"],
                ["P08", "Low Profile Concrete Barrier", "LF", plan[2], bid[2],
                 "$113.12", "$13,574.40", "$99.80", "$11,976.00"]]
    grid = [[E, E, E, E, E, "KG Traffic", E, "McMahon", E]] + tab(("2", "0", "120"),
                                                                  ("4", "400", "120"))[:1] \
        + tab(("2", "0", "120"), ("4", "400", "120"))[2:]
    recs, _a = rows_from_grid(grid, "selftest", "plan-bid", {})
    check("two quantity columns: the one the extensions use",
          sorted({(r["item_no"], r["quantity"]) for r in recs}),
          [("P05", 4.0), ("P06", 400.0), ("P08", 120.0)])
    check("the item column is never the quantity column",
          _qty_column_by_arithmetic([["Line # Description MfgQTY UOM", "", "Unit", "Extended"],
                                     ["1", "Tree Removal", "$500.00", "$4,000.00"]], 0,
                                    ["Line # Description MfgQTY UOM", "", "Unit", "Extended"],
                                    [(2, 3)], {"item": 0, "desc": 0, "unit": None, "qty": 0}),
          {"item": 0, "desc": 0, "unit": None, "qty": None})
    mh = ["Line # Description MfgQTY UOM", "", "", "Unit", "Extended"]
    mm_ = {"item": 0, "desc": 0, "unit": None, "qty": 0}
    check("merged header: item numbers that contradict the extensions are no quantity; "
          "with nothing to check the header is left alone",
          [_qty_column_by_arithmetic([mh, ["1", "Tree Removal", "8", "$500.00", "$4,000.00"],
                                      ["2", "Mobilization", "1", "$105,000.00", "$105,000.00"]],
                                     0, mh, [(3, 4)], mm_)["qty"],
           _qty_column_by_arithmetic([mh, ["1.1", "4\" Broken, White", "1", "$0.65", ""]],
                                     0, mh, [(3, 4)], mm_)["qty"]],
          [None, 0])
    check("quantity column choice leaves equal columns alone",
          _qty_column_by_arithmetic([["Qty", "Quantity", "Unit Price", "Total"],
                                     ["5", "5", "$2.00", "$10.00"]], 0,
                                    ["Qty", "Quantity", "Unit Price", "Total"], [(2, 3)],
                                    {"qty": 0}), {"qty": 0})

    # 2b. Mesquite 21-5712: an item's prices ruled into a row of their own
    #     above it; a price row whose arithmetic fails is left alone
    hdr = ["Item Number", "Description", "Qty", "Unit", "Unit Price", "Extended", "Unit Price",
           "Extended"]
    names = [E, E, E, E, "HQS Construction, LLC", E, "Jeske Construction Company", E]
    for q, want in (("10", [("11", 10.0, 500.0), ("11", 10.0, 550.0), ("12", 200.0, 22.0),
                            ("12", 200.0, 26.0)]),
                    ("12", [("12", 200.0, 22.0), ("12", 200.0, 26.0)])):
        grid = [names, hdr, [E, E, E, E, "500.00", "5,000.00", "550.00", "5,500.00"],
                ["11", "REMOVE AND REPLACE CONCRETE STEPS", q, "CY", E, E, E, E],
                ["12", "FURNISH AND INSTALL FLEXIBLE BASE", "200", "TON", "22.00", "4,400.00",
                 "26.00", "5,200.00"]]
        recs, _a = rows_from_grid(grid, "selftest", "price-row-above", {})
        check("price row above its item (qty %s)" % q,
              sorted((r["item_no"], r["quantity"], r["unit_price"]) for r in recs
                     if r.get("item_no")), sorted(want))

    # 3. Mesquite 25-7575: an allowance printed as its amount only ('-  AL')
    grid = [[E, E, E, E, "Niyam Construction", E, "Estrada Concrete", E],
            ["ITEM", "DESCRIPTION", "QTY", "UNIT", "UNIT", "EXTENDED", "UNIT", "EXTENDED"],
            ["7", "Construct 4-inch concrete sidewalk", "340", "LF", "$37.50", "$12,750.00",
             "$40.00", "$13,600.00"],
            ["12", "Irrigation system repair allowance", "-", "AL", E, "$2,500.00", E,
             "$2,500.00"],
            ["", "Sod note", "-", "SY", E, "$2,500.00", E, "$2,500.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "allowance", {})
    recover_quantity_from_extensions(recs)
    check("allowance amount-only row priced, quantity 1 by its columns",
          sorted((r["item_no"], r["bidder_raw"], r["unit_price"], r["quantity"]) for r in recs),
          [("12", "Estrada Concrete", 2500.0, 1.0), ("12", "Niyam Construction", 2500.0, 1.0),
           ("7", "Estrada Concrete", 40.0, 340.0), ("7", "Niyam Construction", 37.5, 340.0)])

    # 4. NRH RFB 24-021: 'Unit Price | Cost' is a pair (a bare 'PRICE | COST'
    #    or 'UNIT | COST' is not: the tank test in _parse_rules_selftest), and
    #    an estimate caption over the engineer's firm flags its column
    check("price pairs with a bare 'Cost'",
          [detect_price_pairs(["Unit", "Unit Price", "Item Cost", "Unit Price", "Cost"]),
           detect_price_pairs(["Unit", "Unit Price", "Cost"]),
           detect_price_pairs(["Qty", "Unit", "Cost"]),
           detect_price_pairs(["Qty", "PRICE", "COST"])], [[(1, 2), (3, 4)], [], [], []])
    grid = [[E, E, E, E, "ENGINEER'S ESTIMATE", E, "BIDDER 1", E, "BIDDER 2", E],
            [E, E, E, E, "WESTRA CONSULTANTS, LLC", E, "TEX-PRO CONSTRUCTION, LLC", E,
             "HUMPRHEY & MORTON CONSTRUCTION", E],
            [E, E, E, E, "BID BOND", "NA", "BID BOND", "Yes", "BID BOND", "Yes"],
            ["Item No.", "Item Description", "Quantity", "Unit", "Unit Price", "Item Cost",
             "Unit Price", "Item Cost", "Unit Price", "Cost"],
            ["1", "GENERAL SITE PREPERATION", "1", "LS", "$15,000.00", "$15,000.00",
             "$3,000.00", "$3,000.00", "$3,167.00", "$3,167.00"]]
    recs, a = rows_from_grid(grid, "selftest", "nrh-24-021", {})
    check("estimate caption over the consultant's name",
          [(r["bidder_raw"], r["is_engineer_estimate"]) for r in recs],
          [("ENGINEER'S ESTIMATE WESTRA CONSULTANTS, LLC", True),
           ("TEX-PRO CONSTRUCTION, LLC", False), ("HUMPRHEY & MORTON CONSTRUCTION", False)])
    check("an estimate caption over a blank name cell: not the neighbour's firm",
          bidder_names_for([[E, E, "Vendor No. 1", E, "Vendor No. 9", "ENGINEER's ESTIMATE", E],
                            [E, E, "Acme Paving", E, "Dredge Heavy Construction", E, E],
                            ["ITEM", "DESCRIPTION", "UNIT PRICE", "EXTENSION", "Non-Responsive",
                             "UNIT PRICE", "EXTENSION"]], 2, [(2, 3), (5, 6)]),
          {2: "Acme Paving", 5: "ENGINEER's ESTIMATE"})
    grid2 = [[E, E, E, E, "ENGINEER'S ESTIMATE", E, E, E, E, E]] + grid[1:]
    recs, a = rows_from_grid(grid2, "selftest", "title-row", {})
    check("a lone estimate title over one column flags nothing",
          [r["is_engineer_estimate"] for r in recs], [False, False, False])

    # 5. a firm's name cut at the rule between its pair's columns
    check("name split mid-word at the pair's rule",
          [_name_split_at_rule("EAR Telecommunicat", "ions, LLC DBA EARTC"),
           _name_split_at_rule("Texas Standard C", "onstruction, Ltd."),
           _name_split_at_rule("Engineer's", "Estimate"),
           _name_split_at_rule("Acme Paving", "Dallas, TX")],
          ["EAR Telecommunications, LLC DBA EARTC", "Texas Standard Construction, Ltd.",
           "Engineer's", "Acme Paving"])
    hdr = ["Item", "Description", "Unit Price", "Extended Amount", "Unit Price",
           "Extended Amount", "Unit Price", "Extended Amount"]
    cut = [E, E, "EAR Telecommunicat", "ions, LLC DBA EARTC", "Texas Standard C",
           "onstruction, Ltd.", "Engineer's", "Estimate"]
    whole = [E, E, "Acme Paving", "Dallas Office", "Beta Builders", "Fort Worth", "Engineer's",
             "Estimate"]
    check("a names row cut at the rules: every name joined, between words too",
          [bidder_names_for([cut, hdr], 1, [(2, 3), (4, 5), (6, 7)]),
           bidder_names_for([whole, hdr], 1, [(2, 3), (4, 5), (6, 7)])],
          [{2: "EAR Telecommunications, LLC DBA EARTC", 4: "Texas Standard Construction, Ltd.",
            6: "Engineer's Estimate"},
           {2: "Acme Paving", 4: "Beta Builders", 6: "Engineer's"}])
    check("a lone project code left of the first pair is no name",
          bidder_names_for([[E, E, E, "Acme Paving", E, "Beta Builders", E],
                            ["Design Professional:", "Freese and Nichols", "CDH24307", E, E, E, E],
                            ["Item", "Description", "Qty", "Unit Price", "Total", "Unit Price",
                             "Total"]], 2, [(3, 4), (5, 6)]),
          {3: "Acme Paving", 5: "Beta Builders"})
    check("a cell with no letter is no name", [looks_like_bidder(x) for x in
                                               ("1~ 1 0.00", "J5 Construction", "3D Paving")],
          [False, True, True])
    check("a label left of the bidder's value is no name",
          bidder_names_for([[E, E, E, "HQS Construction LLC", E, "Onyx Paving & Utility, LLC", E],
                            [E, E, "Selected #", "- - 1 0.00", E, "1~ 1 0.00", E],
                            [E, E, "Selected ($)", "$ 0", E, "$ 0", E],
                            ["#", "Items", "Unit of Measure", "Unit Price", "Total Cost",
                             "Unit Price", "Total Cost"]], 3, [(3, 4), (5, 6)]),
          {3: "HQS Construction LLC", 5: "Onyx Paving & Utility, LLC"})
    check("a code is no name", [bool(_CODE_TOKEN_RE.match(x)) for x in
                                ("H24307", "CDH24307", "PW23-0009", "A1", "Acme Paving",
                                 "J5 Construction")],
          [True, True, True, False, False, False])

    check("a comma before the ZIP: 'Dallas, TX, 75204' is the city line",
          [strip_address("Palmer Hall Construction\nDallas, TX, 75204"),
           strip_address("Fain Group\nFort Worth, TX, 76161")],
          ["Palmer Hall Construction", "Fain Group"])

    # 6. section banners: a numbered section whose title is a banner, two
    #    banners in one cell, a lettered alternate
    for txt_, want in (("SECTION VI - ALTERNATE BID #1", "SECTION VI - ALTERNATE BID #1"),
                       ("SECTION I - GENERAL AND REMOVALS", ""),
                       ("SECTION V - CONTINGENCY", ""),
                       ("ALTERNATE ITEMS ALT-A: EDC PARKING LOT",
                        "ALTERNATE ITEMS ALT-A: EDC PARKING LOT"),
                       ("ALT B: BROAD ST (PARKING LOT)", "ALT B: BROAD ST (PARKING LOT)"),
                       ("BASE BID Alternates are priced separately", ""),
                       ("Altamira Paving", ""), ("EDC PARKING LOT", "")):
        check("section_banner(%r)" % txt_, section_banner(txt_), want)

    # 7. a contingency section inside the printed base total (NRH 24-021: the
    #    TOTAL BASE BID line includes it) -- not one printed after the base
    #    (Colleyville: 'Grand Total Including Contingency'), nor where neither
    #    sum is printed
    def recs_of(f, base_amt, cont_amt):
        return [{"src_file": f, "bid_section": "SECTION I", "section_role": "base",
                 "bidder_raw": "Tex-Pro", "unit_price": base_amt, "quantity": 1.0,
                 "extended_reported": base_amt},
                {"src_file": f, "bid_section": "SECTION V - CONTINGENCY",
                 "section_role": "other", "bidder_raw": "Tex-Pro", "unit_price": cont_amt,
                 "quantity": 1.0, "extended_reported": cont_amt}]
    rs = recs_of("in.pdf", 90815.0, 10000.0) + recs_of("out.pdf", 832016.10, 180000.0) \
        + recs_of("none.pdf", 500.0, 10.0)
    facts = {"in.pdf": {"total_lines": [10081500]}, "out.pdf": {"total_lines": [83201610]},
             "none.pdf": {"total_lines": [99999]}}
    check("contingency section inside the printed base total",
          nonbase_sections_in_printed_base(rs, facts), {("in.pdf", "SECTION V - CONTINGENCY")})

    # 8. an item row under the ruled table's last rule (Cedar Hill 2A B-1)
    class _Row:
        def __init__(self, cells, bbox):
            self.cells, self.bbox = cells, bbox

    class _Tab:
        def __init__(self, xs, top, n, h):
            self.rows = [_Row([(a, top + i * h, b, top + (i + 1) * h) for a, b in xs],
                              (xs[0][0], top + i * h, xs[-1][1], top + (i + 1) * h))
                         for i in range(n)]
            self.columns = xs
            self.bbox = (xs[0][0], top, xs[-1][1], top + n * h)

    class _Page:
        bbox = (0, 0, 612, 792)

        def __init__(self, words):
            self.words = words

        def within_bbox(self, bb):
            sub = _Page([w for w in self.words if bb[1] <= w["top"] and w["bottom"] <= bb[3]])
            return sub

        def extract_words(self):
            return list(self.words)

    def w(x0, x1, top, t):
        return {"x0": x0, "x1": x1, "top": top, "bottom": top + 7, "text": t}
    xs = [(18, 54), (54, 243), (243, 266), (266, 329), (329, 404), (404, 446), (446, 494)]
    t0 = _Tab(xs, 100, 10, 9)
    foot = [w(33, 40, 192, "B-1"), w(55, 85, 192, "MOBILIZATION"), w(262, 265, 192, "1"),
            w(296, 300, 192, "LS"), w(336, 359, 192, "$92,000.00"), w(418, 444, 192, "$120,000.00"),
            w(467, 492, 192, "$120,000.00"), w(560, 600, 230, "Page 1 of 3")]
    check("item row under the ruled table",
          rows_below_table(_Page(foot), [t0], 0),
          [["B-1", "MOBILIZATION", "1", "LS", "$92,000.00", "$120,000.00", "$120,000.00"]])
    total = [w(55, 120, 192, "TOTAL"), w(336, 359, 192, "$92,000.00"),
             w(418, 444, 192, "$120,000.00")]
    check("a total line under the table is no item", rows_below_table(_Page(total), [t0], 0), [])

    # 9. text a page draws but does not show: a word painted over by a later
    #    white rectangle is not read, the word printed beside it is
    try:
        import fitz
        import io
        doc = fitz.open()
        pg = doc.new_page(width=300, height=200)
        pg.insert_text((40, 60), "HIDDEN", fontsize=12)
        pg.draw_rect(fitz.Rect(30, 40, 120, 70), color=None, fill=(1, 1, 1))
        pg.insert_text((40, 120), "SHOWN", fontsize=12)
        data = doc.tobytes()
        fzd = fitz.open("pdf", data)
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            vis = visible_text_page(pdf.pages[0], fzd[0])
            check("painted-over text is not read", "".join(c["text"] for c in vis.chars), "SHOWN")
        # clipped text: words run on past the clip go; a space between two
        # shown words stays; a number shown in part keeps every digit
        doc = fitz.open()
        pg = doc.new_page(width=300, height=200)
        pg.insert_text((10, 50), "x", fontsize=10)
        font = pg.get_fonts()[0][4]
        doc.update_stream(pg.get_contents()[0], (
            "q 0 0 300 200 re W n BT /%s 10 Tf 1 0 0 1 20 150 Tm (Traffic Control "
            "Implementation) Tj ET Q\nq 20 0 60 200 re W n BT /%s 10 Tf 1 0 0 1 20 100 Tm "
            "(Total $5,000.00) Tj ET Q\nq 0 0 60 200 re W n BT /%s 10 Tf 1 0 0 1 20 60 Tm "
            "(Sidewalk 0-50 sq yd) Tj ET Q\n" % (font, font, font)).encode())
        check("clipped glyphs dropped", "".join(c for c, _x, _y in
                                                hidden_text_chars(fitz.open("pdf", doc.tobytes())[0])),
              " 0-50 sq yd")
    except ImportError:
        print("  note: PyMuPDF is not installed; the hidden-text case was not exercised")

    for f in fails:
        print("FAIL round-3 rule: " + f)
    print("round-3 rules (agent C): %d failure(s)" % len(fails))
    return not fails


def _r3_packet_selftest():
    """Parser round 3 (coordinator): council packets that bundle lettings --
    the agenda stamp in a name cell, names over a pair's own first cell, the
    opening date of a table's own page, a work start date that is no bid date
    -- and amounts printed for SOME bidders of a lump sum, and a group line
    that is the subtotal of its N.k lines."""
    fails, n = [], [0]
    E = ""

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    # 1. an agenda-item stamp in front of a firm is no part of its name
    check("agenda stamp", [_AGENDA_STAMP_RE.sub("", x) for x in (
        "Item 12. Ed Bell Construction", "Item 20. A T Construction, LLC", "Item 12.",
        "Item Construction LLC", "Itemized Paving Co", "Item 5 Paving Co")],
        ["Ed Bell Construction", "A T Construction, LLC", "Item 12.",
         "Item Construction LLC", "Itemized Paving Co", "Item 5 Paving Co"])

    check("block labels", [_block_label_strip(x) for x in (
        "Contractor Jeske Construction Company Address", "Contractor La Banda, LLC Address",
        "Rose & Two Thorns Landscaping & General Contractor", "Contractor",
        "Bidder Name: Acme Paving LLC", "Company Address")],
        ["Jeske Construction Company", "La Banda, LLC",
         "Rose & Two Thorns Landscaping & General Contractor", "Contractor",
         "Acme Paving LLC", "Company Address"])
    check("a stray letter before a unit", [norm_unit(x) for x in ("A ALW", "N LF", "e EA", "A B", "LF")],
          ["ALLOW", "LF", "EA", "A B", "LF"])
    check("a contact line is no name", [_name_candidate(x) for x in (
        "Phone 972-907-1133", "City, State Garland, TX", "Boardwalk Paving & Construction")],
          [False, False, True])

    # 2. names over the pair's OWN first cell (a table ruled higher on the right)
    class _Col(object):
        def __init__(self, cells):
            self.cells = cells

    class _Tab(object):
        def __init__(self, bbox, columns):
            self.bbox, self.columns = bbox, columns

    class _Page(object):
        def __init__(self, words):
            self.words = words

        def extract_words(self):
            return self.words

    def wd(t, x0, top):
        return {"text": t, "x0": float(x0), "x1": float(x0) + 6.0 * len(t), "top": float(top),
                "bottom": float(top) + 9.0}
    low, high = 119.0, 30.0
    cols = [_Col([None, (0, low, 60, low + 20)]), _Col([None, (60, low, 300, low + 20)]),
            _Col([None, (400, low, 470, low + 20)]), _Col([None, (470, low, 540, low + 20)]),
            _Col([None, (540, low, 610, low + 20)]), _Col([None, (610, low, 680, low + 20)]),
            _Col([(680, high, 830, low), (680, low, 750, low + 20)]),
            _Col([None, (750, low, 830, low + 20)])]
    tab = _Tab((0, high, 830, 400), cols)
    words = [wd("McMahon", 405, 64), wd("Contracting", 455, 64), wd("Reliable", 545, 64),
             wd("Paving,", 600, 64), wd("Ed", 690, 64), wd("Bell", 705, 64),
             wd("3019", 405, 86), wd("Roy", 435, 86), wd("1903", 545, 86), wd("Peyco", 575, 86)]
    check("names over the pairs' own first cells",
          names_above_table(_Page(words), tab, [(2, 3), (4, 5), (6, 7)]),
          ["McMahon Contracting", "Reliable Paving", "Ed Bell"])
    flat = [_Col([(c.cells[1][0], high, c.cells[1][2], low)] + c.cells[1:]) for c in cols]
    check("a table ruled level on top reads above its top, as before",
          names_above_table(_Page(words), _Tab((0, high, 830, 400), flat), [(2, 3), (4, 5), (6, 7)]),
          None)

    # 3. the opening date of a table's own page; a printed-on footer is none
    check("page opening date", [page_opening_date(x) for x in (
        ["Bid Opening Date: June 14, 2023; 2:00pm",
         "6/19/2023 Page 7 of 14 Day-Miar-Bid-Bid-Tabulation-20230619.xlsx"],
        ["6/19/2023 Page 7 of 14 Day-Miar-Bid-Bid-Tabulation-20230619.xlsx"],
        ["BIDS OPENED: 1/5/2099"], [])], ["2023-06-14", "", "", ""])

    # 4. a work start date is no bid date; an opening line beside it still is
    check("start date is no bid date", find_dates(
        ["Earliest Possible Start Date 11/2/2025 12/1/2025 11/3/2025"])[:2], ("", "MISSING"))
    check("opening line kept", find_dates(
        ["Bids Opened: 6/3/2021", "Earliest Possible Start Date 7/1/2021"])[:2],
        ("2021-06-03", "bid_open_or_due"))
    check("issue date still weak", find_dates(["Issue Date: 3/1/2024"])[:2],
          ("2024-03-01", "weak_issue_or_received"))
    check("a tab's print date loses to its opening line", find_dates(
        ["Bid Opening Date: June 14, 2023; 2:00pm",
         "6/19/2023 Page 7 of 14 Day-Miar-Bid-Bid-Tabulation-20230619.xlsx"])[:2],
          ("2023-06-14", "bid_open_or_due"))
    check("a tabulation line alone still dates the tab", find_dates(
        ["BID TABULATION 6/19/2023"])[:2], ("2023-06-19", "bid_open_or_due"))

    # 5. a lump sum some bidders print as its amount only
    pr = [(4, 5), (6, 7), (8, 9)]
    row = ["1", "UNFORSEEN ITEMS", "LS", "1", "$80,000.00", "$80,000.00", E, "$80,000.00", E, E]
    check("lump sum: amount-only columns take the amount",
          lump_sum_amount_prices("1", "UNFORSEEN ITEMS", "LS", 1.0, row, pr,
                                 [(4, 80000.0), (6, None), (8, None)]),
          [(4, 80000.0), (6, 80000.0), (8, None)])
    check("lump sum: not for a measured item",
          lump_sum_amount_prices("1", "PIPE", "LF", 10.0, row, pr,
                                 [(4, 80000.0), (6, None), (8, None)]), None)
    check("lump sum: a deduction is no amount bid",
          lump_sum_amount_prices("", "Minus Base Bid Item# 13", "LS", 1.0,
                                 ["", "Minus Base Bid Item# 13", "LS", "1", "$ (18,000.00)",
                                  "$ (18,000.00)", E, "$ (13,500.00)", E, E], pr,
                                 [(4, 18000.0), (6, None), (8, None)]), None)
    check("lump sum: every column priced, nothing to do",
          lump_sum_amount_prices("1", "X", "LS", 1.0, row, pr,
                                 [(4, 80000.0), (6, 7.0), (8, 9.0)]), None)
    check("lump sum: all amount-only, as before",
          lump_sum_amount_prices("12", "allowance", "AL", None,
                                 ["12", "allowance", "AL", "-", E, "$2,500.00", E, "$2,500.00",
                                  E, "$2,500.00"], pr, [(4, None), (6, None), (8, None)]),
          [(4, 2500.0), (6, 2500.0), (8, 2500.0)])

    # 6. a group line that is the subtotal of its N.k lines
    def rec(no, desc, q, u, e=None, sh="Lines", b="2L"):
        return {"src_file": "f", "src_sheet": sh, "bidder_raw": b, "item_no": no,
                "description": desc, "quantity": q, "unit_price": u, "extended_reported": e,
                "bid_section": "BASE BID"}
    recs = [rec("1.1", "Mobilization", 1.0, 80000.0), rec("1.2", "Testing", 1.0, 10000.0),
            rec("2", "Add-Alternative #1", 1.0, 210000.0, 210000.0),
            rec("2.1", "ADD: abutments", 2.0, 20000.0), rec("2.2", "ADD: bridge", 1.0, 170000.0),
            rec("3", "Retaining wall", 1.0, 50000.0), rec("3.1", "Wall A", 1.0, 20000.0),
            rec("3.2", "Wall B", 1.0, 20000.0), rec("4", "Sidewalk", 350.0, 40.0, 14000.0)]
    out = subtotal_parent_lines([dict(r) for r in recs])
    check("group subtotal dropped, sub-lines alternate",
          [(r["item_no"], r["bid_section"]) for r in out],
          [("1.1", "BASE BID"), ("1.2", "BASE BID"), ("2.1", "ADD-ALTERNATIVE #1"),
           ("2.2", "ADD-ALTERNATIVE #1"), ("3", "BASE BID"), ("3.1", "BASE BID"),
           ("3.2", "BASE BID"), ("4", "BASE BID")])
    check("another bidder's lines are not a group",
          len(subtotal_parent_lines([dict(r, bidder_raw=("X" if r["item_no"] != "2" else "2L"))
                                     for r in recs])), len(recs))

    for f in fails:
        print("FAIL packet rule: " + f)
    print("round-3 packet rules (coordinator): %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _scope_rules_selftest():
    """Parser round 3 (scope: extra rows, alternates, time bids, missing rows),
    each rule on a small tab shaped like the public original that broke it --
    Coppell Airline Drive (A+B time bid), Cedar Hill PW23-0004 (alternate
    titles over the header), Arlington PWDR22005 (stacked quantities), Cedar
    Hill Parkerville (a banner beside the item caption, a reprinted estimate),
    Grand Prairie Fish Creek (a short continuation under an inferred header)
    and Phase 3 Water Line (a firm and its street) -- with the look-alikes
    each rule must leave alone."""
    fails, n = [], [0]
    E = ""

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    # 1. the time half of an A+B bid is not base scope
    check("TIME_BID_SEC_RE",
          [bool(TIME_BID_SEC_RE.search(s)) for s in (
              "BASE BID B - SECTION XI - TOTAL TIME TO COMPLETE PROJECT", "TIME BID (B)",
              "BID B - TIME", "CONTRACT TIME", "BASE BID A - SECTION VI - PAVING",
              "OVERTIME LABOR", "TIME AND MATERIALS", "PART-TIME INSPECTION")],
          [True, True, True, True, False, False, False, False])
    # 2. an alternate named by its ordinal word is a banner; other ordinals,
    #    and a sentence, are not
    check("section_banner: ordinal alternates",
          [section_banner(s) for s in ("FIRST ADD ALTERNATE", "Second Additive Alternate",
                                        "3rd Alternate", "FIRST PHASE",
                                        "First alternates are priced on page 3")],
          ["FIRST ADD ALTERNATE", "SECOND ADDITIVE ALTERNATE", "3RD ALTERNATE", "", ""])

    def read(grid, sheet="t", names=("Klutz", "Humphrey")):
        recs, _a = rows_from_grid(grid, "selftest", sheet, {}, fallback_names=list(names))
        return [(r["item_no"], r["bidder_raw"], r["unit_price"], r["extended_reported"],
                 r["bid_section"]) for r in recs]

    # 3. Arlington PWDR22005: 'ALTERNATE A' over the table's own header; a
    #    title is no banner, and a banner in rows borrowed from another table
    #    (+inherited_hdr) is that table's, not this one's
    hdr = ["ITEM", "DESCRIPTION", "QTY", "UNITS", "UNIT PRICE", "TOTAL COST", "UNIT PRICE",
           "TOTAL COST"]
    alt = ["199A", "Green Cement", "1", "LS", "$ 1 .00", "$ 1 .00", "$ 1.10", "$ 1.10"]
    check("a banner over the header opens the section",
          read([["ALTERNATE A"] + [E] * 7, hdr, alt]),
          [("199A", "Klutz", 1.0, 1.0, "ALTERNATE A"), ("199A", "Humphrey", 1.1, 1.1, "ALTERNATE A")])
    check("look-alikes: a title above the header, a borrowed banner",
          [read([["BID TABULATION - PROJ. PW23-0004"] + [E] * 7, hdr, alt]),
           read([["ALTERNATE A"] + [E] * 7, hdr, alt], "t+inherited_hdr")],
          [[("199A", "Klutz", 1.0, 1.0, "BASE BID"), ("199A", "Humphrey", 1.1, 1.1, "BASE BID")]] * 2)
    # 4. Cedar Hill PW23-0004: the ordinal title over a two-row header whose
    #    upper row names the firms
    nr = [["FIRST ADD ALTERNATE"] + [E] * 7,
          ["ITEM NO.", "ITEM DESCRIPTION", "UNIT", "TOTAL QUANTITY", "J&L CONSTRUCTION, LLC", E,
           "JESKE CONSTRUCTION COMPANY", E],
          [E, E, E, E, "UNIT COST", "TOTAL COST", "UNIT COST", "TOTAL COST"],
          ["4", "UNCLASSIFIED ALLEY EXCAVATION", "CY", "200", "$ 69.34", "$ 13,868.00", "$ 40.00",
           "$ 8,000.00"]]
    check("an ordinal alternate title over a two-row header",
          read(nr), [("4", "J&L CONSTRUCTION, LLC", 69.34, 13868.0, "FIRST ADD ALTERNATE"),
                     ("4", "JESKE CONSTRUCTION COMPANY", 40.0, 8000.0, "FIRST ADD ALTERNATE")])
    # 5. Cedar Hill Parkerville: 'No. | Alternate Bid' -- the item column's
    #    caption beside the banner -- after a 'Base Bid Total' line
    pk = [[E, E, E, E, "Engineer's Estimate", E, "Quality Contractors", E],
          [E, "Description", "Unit of Measure", "Bid Quantity", "Unit Price", "Bid Value",
           "Unit Price", "Bid Value"],
          ["No.", "Base Bid", E, E, E, E, E, E],
          [E, "Temporary Pavement Repair", "SY", "46", E, "$ -", "$ 249.00", "$ 11,454.00"],
          ["Base Bid Total", E, E, E, E, "$ 145,043.80", E, E],
          ["No.", "Alternate Bid", E, E, E, E, E, E],
          [E, "6' Masonry Screen Wall", "LF", "42", "$ 150.00", "$ 6,300.00", "$ 148.00",
           "$ 6,216.00"]]
    check("a banner beside the item column's caption",
          read(pk), [(E, "Quality Contractors", 249.0, 11454.0, "BASE BID"),
                     (E, "Engineer's Estimate", 150.0, 6300.0, "ALTERNATE BID"),
                     (E, "Quality Contractors", 148.0, 6216.0, "ALTERNATE BID")])
    # 6. Arlington PWDR22005: the quantities of three items stacked in the
    #    first item's cell; a count that does not match, and money lines, stay
    st = [["ITEM", "DESCRIPTION", "QTY", "UNITS", "UNIT PRICE", "TOTAL COST"],
          ["301", "Site Preparation", "1\n830\n25", "LS", "$ 27,720.00", "$ 27,720.00"],
          ["302", "Unclassified Channel Excavation", None, "CY", "$ 93.00", "$ 77,190.00"],
          ["303", "Reinforced Concrete Flume", None, "LF", "$ 265.00", "$ 6,625.00"],
          [None, "SUBTOTAL", None, None, None, "$ 111,535.00"]]
    bad = [list(r) for r in st]
    bad[1][2] = "1\n830"
    mon = [list(r) for r in st]
    mon[1][2], mon[1][4] = "1", "$ 1.00\n$ 2.00\n$ 3.00"
    mon[2][4] = mon[3][4] = None
    two = [list(r) for r in st[:3]]                 # two lines, one empty row: too weak
    two[1][2] = "7\n3"
    check("stacked numbers spread one per row",
          [[r[2] for r in spread_stacked_cells(st)], [r[2] for r in spread_stacked_cells(bad)],
           [r[4] for r in spread_stacked_cells(mon)], [r[2] for r in spread_stacked_cells(two)]],
          [["QTY", "1", "830", "25", None], ["QTY", "1\n830", None, None, None],
           ["UNIT PRICE", "$ 1.00\n$ 2.00\n$ 3.00", None, None, None], ["QTY", "7\n3", None]])
    # 7. Grand Prairie Phase 3: a firm, its street and its contact in one cell
    check("a firm running into a street address with no city",
          [strip_address(s) for s in (
              "Freese and Nichols, Inc. 801 Cherry Street, Suite 2800 Jane Doe, P.E. "
              "(555)555-0100 jane.doe@example.com",
              "Excel 4 Construction, LLC", "Route 66 Paving Co.")]
          + [_firm_before_street("ACME Paving 12 Main Street")],
          ["Freese and Nichols, Inc.", "Excel 4 Construction, LLC", "Route 66 Paving Co.", ""])
    # 8. Cedar Hill Parkerville: the estimate column reprinted on page 2 under
    #    a garbled caption; not a changed price, another file's, or a pair of
    #    estimate-only sheets with no bidder beside either
    its = [("Mobilization (10%)", "LS", 1.0, 15900.0), ('24" HDPE (H20 Loading)', "LF", 129.0, 85.0),
           ("Trench Safety", "LF", 129.0, 2.0)]

    def est(f, sheet, bidder, ee, k=1.0, last=None):
        rows = [{"src_file": f, "src_sheet": sheet, "bidder_raw": bidder,
                 "is_engineer_estimate": ee, "description": d, "unit": u, "quantity": q,
                 "unit_price": p * k} for d, u, q, p in its]
        if last is not None:
            rows[-1]["unit_price"] = last
        return rows
    sheets = {("x.pdf", "p1t0"): est("x.pdf", "p1t0", "Engineer's Estimate", True)
              + est("x.pdf", "p1t0", "Texas Standard", False, 1.1),
              ("x.pdf", "p2t1"): est("x.pdf", "p2t1", "Engineer's EstimRaotaed way", True),
              ("x.pdf", "p3t0"): est("x.pdf", "p3t0", "Engineer's Estimate", True, last=2.5),
              ("y.pdf", "p2t1"): est("y.pdf", "p2t1", "Engineer's Estimate", True),
              ("z.pdf", "p1t0"): est("z.pdf", "p1t0", "Engineer's Estimate", True),
              ("z.pdf", "p2t0"): est("z.pdf", "p2t0", "Engineer's Estimate", True)}
    check("reprinted_estimate_sheets", reprinted_estimate_sheets(sheets),
          {("x.pdf", "p2t1"): ("x.pdf", "p1t0")})
    # 9. Grand Prairie Fish Creek: a two-row continuation (the Section 2
    #    caption row and item 37) under the INFERRED header of the table above
    cap = ["NO.", "DESCRIPTION", "QUANTITY", "UNIT"] + ["UNIT PRICE (NUMBER ONLY)",
                                                        "EXTENDED PRICE (NUMBER ONLY)"] * 2
    fc = [cap, [E, "BASE BID (SECTION 1 )"] + [E] * 6,
          ["1", "Mobilization", "1", "LS", "$ 123,310.00", "$ 123,310.00", "$ 23,000.00",
           "$ 23,000.00"],
          ["5", "Silt Fence", "1250", "LF", "$ 5.13", "$ 6,412.50", "$ 3.50", "$ 4,375.00"],
          ["15", "Unclassified Channel Excavation", "1150", "CY", "$ 95.52", "$ 109,848.00",
           "$ 32.50", "$ 37,375.00"],
          ["17", "Free-Draining Borrow Material", "750", "CY", "$ 148.68", "$ 111,510.00",
           "$ 65.00", "$ 48,750.00"]]
    cont = [[E, "BASE BID (Section 2- Wastewater)"] + cap[2:],
            ["37", '8" PVC Wastewater Pipe', "240", "LF", "$ 708.95", "$ 170,148.00", "$ 260.00",
             "$ 62,400.00"]]
    names = ["MCMAHON CONTRACTING, L.P.", "Klutz Construction LLC"]
    r1, a1 = rows_from_grid(fc, "selftest", "p1t0", {}, fallback_names=names)
    r0, a0 = rows_from_grid(cont, "selftest", "p1t1", {}, fallback_names=names)
    pre = (a1 or {}).get("inferred_prefix")
    deduct = [["1", "Deduct cost for furnishing and installing gas service", "1", "LS",
               "$ 6,500.00", "$ 6,500.00", "$ 3,500.00", "$ 3,500.00"],
              [E, "Sub Total Deduct Alternate Bid #1", E, E, E, "$ 6,500.00", E, "$ 3,500.00"]]
    lend = [borrows_inferred_header(r0, a0, False, cont, pre, 0, 0),
            borrows_inferred_header(r0, a0, False, cont, pre, 0, 2),        # two pages on
            borrows_inferred_header(r0, a0, False, cont + [fc[2]] * 3, pre, 0, 0),  # 4 rows
            borrows_inferred_header([], None, False, deduct, pre, 0, 0)]    # no caption row
    r2 = read((pre or []) + cont, "p1t1+inherited_hdr", names) if pre else []
    check("a short continuation under the inferred header above it",
          (len(r1), r0, lend, r2),
          (8, [], [True, False, False, False],
           [("37", "MCMAHON CONTRACTING, L.P.", 708.95, 170148.0, "BASE BID (SECTION 2- WASTEWATER)"),
            ("37", "Klutz Construction LLC", 260.0, 62400.0, "BASE BID (SECTION 2- WASTEWATER)")]))

    # 10. Arlington PWDR22005 Alternate A: the second bidder's unit price and
    #     total in one cell, its total cell empty; not split when the quantity
    #     disproves it, when the total cell holds something, without '$', or
    #     when no other pair of the row reads whole (Collin County 2021-173's
    #     'Bid Grand Total' row, every bidder's pair glued)
    gl = [hdr, ["199A", "Green Cement", "1", "LS", "$ 1 .00", "$ 1 .00", "$ 1.10 $ 1.10", E]]
    pr = [(4, 5), (6, 7)]
    okr = ["7", "Pipe", "3", "LF", "$ 2.00", "$ 6.00"]
    gt = ["1", "Bid Grand Total", "1", "job", "$43,426.72 $43,426.72", E, "$54,000.00 $54,000.00", E]
    check("a unit price and its total glued into one cell",
          [read(gl),
           split_glued_price_total(okr + ["$ 2.00 $ 5.00", E], pr, 3.0),
           split_glued_price_total(okr + ["$ 2.00 $ 6.00", "x"], pr, 3.0),
           split_glued_price_total(okr + ["2.00 6.00", E], pr, 3.0),
           split_glued_price_total(gt, pr, 1.0),
           split_glued_price_total(okr + ["$ 2 .00 $ 6.00", E], pr, 3.0)],
          [[("199A", "Klutz", 1.0, 1.0, "BASE BID"), ("199A", "Humphrey", 1.1, 1.1, "BASE BID")],
           okr + ["$ 2.00 $ 5.00", E], okr + ["$ 2.00 $ 6.00", "x"], okr + ["2.00 6.00", E], gt,
           okr + ["2 .00", "6.00"]])

    for f in fails:
        print("FAIL scope rule: " + f)
    print("scope rules: %d checks, %d failure(s)" % (n[0], len(fails)))
    return not fails


def _parse_rules_selftest():
    """The rules that repaired public lettings checked against their printed
    originals -- Rowlett 2021-09, Farmers Branch 25-17 / 26-20, Collin County
    2023-303, Lewisville 25-98-C, Mesquite 2022-062, North Richland Hills
    26-001, Coppell 2021-04-19, Dallas County 2022-014 -- each on a small tab
    shaped like the original that broke it and run through the REAL code path
    (rows_from_grid, read_pdf_coords, recover_display_rounded_quantities,
    split_packed_rows), with the look-alikes each rule must leave alone."""
    fails = []
    E = ""

    def check(name, got, want):
        if got != want:
            fails.append("%s\n  got  %r\n  want %r" % (name, got, want))

    # 1. thousand gallons, bare or spelled out with its code in parentheses --
    #    and a CONVERSION in parentheses never beats the unit outside them
    for raw, want in (("MG", "MG"), ("TGL", "MG"), ("THOUSAND GALLONS (TGL)", "MG"),
                      ("CUBIC YARD (CY)", "CY"), ("LS (SEE NOTE 3)", "LS"),
                      ("GAL", "GAL"), ("EACH", "EA"), ("PIPE (LF)", "LF"),
                      ("MG (1000 GAL)", "MG"), ("TON (2000 LB)", "TON"), ("CY (27 CF)", "CY"),
                      ("AC (43,560 SF)", "AC"), ("SY (9 SF)", "SY"), ("MO (30 DAYS)", "MO"),
                      ("DAY (8 HR)", "DAY")):
        check("norm_unit(%r)" % raw, norm_unit(raw), want)
    check("'MG' is a known unit", "MG" in KNOWN_UNITS, True)

    # 2. an UNMEASURED caption that IS a total caption is a sum, not a pay
    #    item; 'total' elsewhere in a pay item's text is not, and neither is a
    #    row numbered '#0-44', 'COFB 01', '(12)' or 'Item 5'
    for args, want in ((("BASE BID TOTAL", E, E, None), True),
                       ((E, "ALTERNATE BID TOTAL", E, None), True),
                       ((E, "PAVING SUBTOTAL", E, None), True),
                       ((E, "TOTAL BASE BID", E, None), True),
                       ((E, "STREET REPAIRS: Totals", E, None), True),
                       ((E, "Contractor's Submitted Bid Totals: April 14, 2023", E, None), True),
                       (("NOT TO EXCEED TOTAL BASE BID AMOUNT:", E, E, None), True),
                       (("FARMERS BRANCH CREEK EROSION CONTROL GRAND-TOTAL AMOUNT BASE BID "
                         "(E21, E20 & E8):", E, E, None), True),
                       (("UNIT 1 TOTAL", E, E, None), True),
                       (("ALT 1 TOTAL", E, E, None), True),
                       (("1.2", "Total Labor Cost Incorporated in Project", E, None), False),
                       ((E, "1.2 Total Labor Cost Incorporated in Project", E, None), False),
                       ((E, "Mobilization (not to exceed 5% of the Grand Total Bid Amount)",
                         "LS", 1.0), False),
                       ((E, "Mobilization (not to exceed 5% of the Grand Total Bid Amount)",
                         E, None), False),
                       (("7", "Concrete Sidewalk", E, None), False),
                       ((E, "Erosion control (total for all phases)", E, None), False),
                       ((E, "Price Per Acre -- Mowing soil conservation structures, "
                            "approximately (212) total acres 1 acre", E, None), False),
                       ((E, "State total Cost for Project Management", E, None), False),
                       (("#0-44", "Total station survey and staking", E, None), False),
                       (("COFB 01", "Subtotal of traffic control", E, None), False),
                       (("(12)", "Total station survey and staking", E, None), False),
                       (("Item 5", "Total station survey and staking", E, None), False)):
        check("is_total_label_row%r" % (args,), is_total_label_row(*args), want)

    # 3. a bidder ORDINAL caption glued to the name; a solicitation number is
    #    not one, and what follows the caption must be a firm
    check("strip_bidder_caption",
          strip_bidder_caption("BID NO. 1 Texas Civil Construction 121 Alexander Lane"),
          "Texas Civil Construction 121 Alexander Lane")
    for s in ("Bid No. 2021-09", "Bid No. 25-17 Westside Art Trail", "BIDDER 3",
              "Contractor 1 (low bid)", "Contractor 2 apparent low bidder", "Bidder 3 Base Bid"):
        check("strip_bidder_caption(%r)" % s, strip_bidder_caption(s), s)
    check("looks_like_bidder('Bid No. 2021-09')", looks_like_bidder("Bid No. 2021-09"), False)
    for ords, want in (([1, 2, 3], True), ([None, 1], True), ([12, None], False), ([2], False),
                       ([], False)):
        check("ordinal_series_ok(%r)" % ords, ordinal_series_ok(ords), want)

    # 4. an ALTERNATE caption ending a name cell; a firm merely named so is not
    #    one -- never 'Alternative', never a bare 'Alt' that may be the name's
    #    own, never a parenthetical that merely starts with the word
    for s, want in (("HomeRun Construction, LLC Alternate# 1",
                     ("HomeRun Construction, LLC", "ALT 1")),
                    ("Valvoline (Alternate)", ("Valvoline", "ALT")),
                    ("Estrada Concrete Company LLC Alternate (Item 8-12)",
                     ("Estrada Concrete Company LLC", "ALT")),
                    ("North Rock Contractors, Inc Alternate 1", ("North Rock Contractors, Inc",
                                                                 "ALT 1")),
                    ("Centerline Supply, Inc. ALT", ("Centerline Supply, Inc.", "ALT")),
                    ("Acme Paving LLC (Alt. 3)", ("Acme Paving LLC", "ALT 3")),
                    ("Alternative Energy Services LLC", ("Alternative Energy Services LLC", "")),
                    ("Texas Alternate Paving Co.", ("Texas Alternate Paving Co.", "")),
                    ("Alternate", ("Alternate", "")),
                    ("Green Alternative", ("Green Alternative", "")),
                    ("The Paving Alternative", ("The Paving Alternative", "")),
                    ("Smith & Alt", ("Smith & Alt", "")),
                    ("Smith & Alt 2", ("Smith & Alt 2", "")),
                    ("Acme Paving (Alternate bid included)",
                     ("Acme Paving (Alternate bid included)", "")),
                    ("Pollock Paper Distributors (ALTERNATIVE)",
                     ("Pollock Paper Distributors (ALTERNATIVE)", ""))):
        check("split_alternate_caption(%r)" % s, split_alternate_caption(s), want)

    # 4b. a section banner is a LABEL; a note that opens with a section word
    #     is not one.  A one-letter slip in 'alternate' is still the word.
    for s, want in (("BASE BID", "BASE BID"), ("Bid Alternate", "BID ALTERNATE"),
                    ("ALTERNATE #1 PAVING ITEMS", "ALTERNATE #1 PAVING ITEMS"),
                    ("Add Alternate # 1", "ADD ALTERNATE # 1"),
                    ("Alternate 1: Bowser Road & Windsong Trail",
                     "ALTERNATE 1: BOWSER ROAD & WINDSONG TRAIL"),
                    ("ALTERNAE BID #1", "ALTERNATE BID #1"),
                    ("Bid alternates are priced on sheet 3 and are not part of this tab", ""),
                    ("BID ALTERNATES ON SHEET 3", ""), ("Alternates not awarded", ""),
                    ("Alternate bid items are listed below", ""),
                    ("participating as a signage partner", ""), ("Alternative 1", "")):
        check("section_banner(%r)" % s, section_banner(s), want)
    for w, want in (("ALTERNAE", "ALTERNATE"), ("Altenate", "Alternate"),
                    ("ALTERANTE", "ALTERNATE"), ("Alternative", "Alternative"),
                    ("Alternates", "Alternates"), ("Altamesa", "Altamesa")):
        check("_fix_alternate_typo(%r)" % w, _fix_alternate_typo(w), want)
    for s, want in (("UNIT TOTAL PRICE AMOUNT", True), ("Unit Total Price Cost", True),
                    ("UNIT PRICE TOTAL COST", True), ("Unit Price", False),
                    ("Total Unit Price", False), ("DESCRIPTION", False)):
        check("merged_price_total_caption(%r)" % s, merged_price_total_caption(s), want)
    check("strip_address(FM road)", strip_address(
        "SYB CONSTRUCTION CO., INC 1017 S. FM 157 VENUS, TX 76084"), "SYB CONSTRUCTION CO., INC")

    def totals(recs):
        t = {}
        for r in recs:
            k = (r["bidder_raw"], r["bid_section"])
            amt = r["extended_reported"]
            t[k] = round(t.get(k, 0.0) + (r["unit_price"] if amt is None else amt), 2)
        return sorted(t.items())

    # 5. Rowlett 2021-09's template: 'BID NO. n' glued to each name and
    #    address, and BASE BID TOTAL / ALTERNATE BID TOTAL rows whose sums sit
    #    in the unit-price column.  Every bid used to drop as no_bidder_name
    #    and the estimate's base read double.
    grid = [
        ["OWNER: City of Rowlett Bid No. 2021-09", E, E, E, "Engineers Estimate", E,
         "BID NO. 1 Texas Civil Construction 121 Alexander Lane Royse City, TX 75189", E,
         "BID NO. 2 Estrada Concrete Company, LLC 309 Nichols Dr. Hutchins, TX 75141", E],
        ["ITEM NO.", "DESCRIPTION", "UNIT", "APPROX. QUANTITY", "UNIT PRICE",
         "TOTAL AMOUNT", "UNIT PRICE", "TOTAL AMOUNT", "UNIT PRICE", "TOTAL AMOUNT"],
        ["BASE BID"] + [E] * 9,
        ["1", "Mobilization (5% Maximum)", "LS", "1", "$40,000.00", "$40,000.00",
         "$25,000.00", "$25,000.00", "$22,000.00", "$22,000.00"],
        ["4", "Remove Alley Pavement", "SY", "2,900", "$24.00", "$69,600.00",
         "$10.00", "$29,000.00", "$15.00", "$43,500.00"],
        ["BASE BID TOTAL", E, E, E, "$109,600.00", E, "$54,000.00", E, "$65,500.00", E],
        ["ALTERNATE BID"] + [E] * 9,
        ["1", "Geogrid for Base", "SY", "3,686", "$3.50", "$12,901.00", "$2.00",
         "$7,372.00", "$5.00", "$18,430.00"],
        ["ALTERNATE BID TOTAL", E, E, E, "$12,901.00", E, "$7,372.00", E, "$18,430.00", E],
        ["TOTAL (BASE BID + ALTERNATE BID)", E, E, E, "$122,501.00", E, "$61,372.00", E,
         "$83,930.00", E]]
    recs, _a = rows_from_grid(grid, "selftest", "rowlett", {})
    check("rowlett-shaped tab", totals(recs), sorted({
        ("Engineers Estimate", "BASE BID"): 109600.0,
        ("Texas Civil Construction", "BASE BID"): 54000.0,
        ("Estrada Concrete Company, LLC", "BASE BID"): 65500.0,
        ("Engineers Estimate", "ALTERNATE BID"): 12901.0,
        ("Texas Civil Construction", "ALTERNATE BID"): 7372.0,
        ("Estrada Concrete Company, LLC", "ALTERNATE BID"): 18430.0}.items()))

    # 6. Farmers Branch's alternate banner: 'Bid Alternate' in the first cell
    #    of a row that repeats all seven names (past the 120-character guard),
    #    after a 'Notes:' line that opens a section of its own.
    names7 = ["American Traffic Construction LLC", "Meca Construction",
              "A&D Paving LLC", "Texas Standard Construction", "JR West Texas Concrete",
              "Axis Contracting Inc.", "Engineer OPCC"]

    def fb_row(lead, cells):
        return list(lead) + [x for c in cells for x in (c, E)]
    grid = [fb_row(["Valley View Lane at Mercer Parkway Intersection Improvements",
                    E, E, E, E], names7),
            ["Item", "Spec Item", "Description", "Unit", "Bid Qty"]
            + ["Unit Price", "Total"] * 7,
            ["71", "NCTCOG 107.21", "PROJECT SIGN", "EA", "2"]
            + ["$ 300.00", "$ 600.00"] * 7,
            fb_row(["Total Base Bid for Valley View Lane at Mercer Parkway:",
                    E, E, E, E], ["$600.00"] * 7),
            ["Notes:"] + [E] * 18,
            fb_row(["Bid Alternate", E, E, E, E], names7),
            ["72", "COFB 6000", "MAINTENANCE BOND", "LS", "1"]
            + ["$ 15,000.00", "$ 15,000.00"] * 7]
    recs, _a = rows_from_grid(grid, "selftest", "farmers", {})
    check("farmers-branch-shaped tab",
          sorted(set((r["item_no"], r["bid_section"]) for r in recs)),
          [("71", "BASE BID"), ("72", "BID ALTERNATE")])
    check("farmers-branch bidders", len(set(r["bidder_raw"] for r in recs)), 7)

    # 7. Mesquite 2022-062's alternate page names each column '<firm>
    #    Alternate# 1'; read as names they were five new BASE bidders.
    grid = [[E, E, E, E, "HomeRun Construction, LLC Alternate# 1", E,
             "A & C Construction, Inc. Alternate# 1", E],
            ["ITEM", "DESCRIPTION", "QTY", "UNIT", "UNIT", "EXTENDED", "UNIT", "EXTENDED"],
            ["1.", "Change hydro-mulched areas to sod", "1", "LS", "$750.00", "$750.00",
             "$5,000.00", "$5,000.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "mesquite", {})
    check("mesquite-shaped tab", totals(recs),
          [(("A & C Construction, Inc.", "ALT 1"), 5000.0),
           (("HomeRun Construction, LLC", "ALT 1"), 750.0)])

    # 8. the coordinate strategy applies rules 2-4 to its own records -- the
    #    ordinal caption only as one of a series over the file's columns
    import parse_coords

    def coords(bidders):
        fake = [dict(src_sheet="coords_p1", bid_section="BASE BID", item_no="1",
                     description="Mobilization", unit_raw="LS", quantity_raw="1",
                     bidder_raw=b, unit_price_raw=p, extended_raw=p) for b, p in bidders]
        fake.append(dict(src_sheet="coords_p1", bid_section="BASE BID", item_no=E,
                         description="BASE BID TOTAL", unit_raw=E, quantity_raw=E,
                         bidder_raw="Engineers Estimate", unit_price_raw="109,600.00",
                         extended_raw=E))
        saved = parse_coords.parse_pdf_by_coords
        try:
            parse_coords.parse_pdf_by_coords = lambda path: [dict(x) for x in fake]
            return read_pdf_coords("selftest.pdf", {})
        finally:
            parse_coords.parse_pdf_by_coords = saved
    recs = coords((("BID NO. 1 Texas Civil Construction", "25,000.00"),
                   ("BID NO. 2 Estrada Concrete Company, LLC", "22,000.00"),
                   ("HomeRun Construction, LLC Alternate# 1", "750.00")))
    check("coordinate records", [(r["bidder_raw"], r["bid_section"]) for r in recs],
          [("Texas Civil Construction", "BASE BID"),
           ("Estrada Concrete Company, LLC", "BASE BID"),
           ("HomeRun Construction, LLC", "ALT 1")])
    recs = coords((("BID NO. 12 Main Street Paving", "25,000.00"),
                   ("Bravo Construction", "22,000.00")))
    check("coordinate records, lone 'BID NO. 12' is no ordinal",
          [r["bidder_raw"] for r in recs], ["BID NO. 12 Main Street Paving", "Bravo Construction"])
    #  8b. the coordinate reader's own layout rules (parse_coords.selftest_checks)
    for name, got, want in parse_coords.selftest_checks():
        check("coords: " + name, got, want)

    # 9. Lewisville 25-98-C #0-13: 9.24 CY printed as 9, every column agreeing
    def cells(sheet, q, priced):
        return [{"src_file": "selftest", "src_sheet": sheet, "bid_section": "BASE BID",
                 "item_no": "#0-13", "description": "RIPRAP (MOW STRIP)(5 IN)",
                 "unit_raw": "CUBIC YARD", "quantity": q, "bidder_raw": b,
                 "unit_price": up, "extended_reported": e} for b, up, e in priced]
    rounded = cells("lew", 9.0, [("Garret Shields Infrastructure", 600.0, 5544.0),
                                 ("Axis Contracting", 1650.0, 15246.0),
                                 ("Ed Bell Construction Company", 1785.0, 16493.4),
                                 ("OPCC", 750.0, 6930.0)])
    # ... and the look-alikes that must NOT move: one column off on its own
    # (the OPCC's 49.7 CY of #0-10 against the form's 50), a bidder writing
    # 25,500 against a 25,000 lump sum, a row with one priced column.
    alike = (cells("one-col-off", 50.0, [("Garret Shields Infrastructure", 1000.0, 50000.0),
                                         ("Axis Contracting", 616.0, 30800.0),
                                         ("OPCC", 2000.0, 99400.0)])
             + cells("lump-sum", 1.0, [("Accell Construction", 25000.0, 25500.0),
                                       ("Engineer OPCC", 30000.0, 30000.0)])
             + cells("single", 9.0, [("Garret Shields Infrastructure", 600.0, 5544.0)]))
    got = recover_display_rounded_quantities(rounded + alike)
    check("display-rounded quantity recovered", (got, sorted(set(
        (r["quantity"], r.get("qty_source")) for r in rounded))),
        ((1, 4), [(9.24, "display_rounded_verified")]))
    check("display-rounding look-alikes untouched", [r["quantity"] for r in alike],
          [50.0] * 3 + [1.0] * 2 + [9.0])
    # ... nor any OTHER explanation of a common factor: every extension at
    # 1.1 x q x price (5 -> 5.5), a lump sum at 1.4 x price, 0.5 AC printed 1,
    # 23.5 LF printed 23 (the half ABOVE the printed number: a spreadsheet
    # would show 24), a q* that needs three decimals.
    other = (cells("factor", 5.0, [("A", 100.0, 550.0), ("B", 120.0, 660.0), ("C", 90.0, 495.0)])
             + [dict(r, unit_raw="LS") for r in
                cells("ls-factor", 1.0, [("A", 10000.0, 14000.0), ("B", 12000.0, 16800.0)])]
             + [dict(r, unit_raw="AC") for r in
                cells("half-acre", 1.0, [("A", 4000.0, 2000.0), ("B", 3000.0, 1500.0)])]
             + cells("half-above", 23.0, [("A", 100.0, 2350.0), ("B", 90.0, 2115.0)])
             + cells("3-decimals", 12.3, [("A", 200.0, 2469.0), ("B", 100.0, 1234.5)]))
    check("display rounding refuses other explanations",
          (recover_display_rounded_quantities(other), [r["quantity"] for r in other]),
          ((0, 0), [5.0] * 3 + [1.0] * 4 + [23.0] * 2 + [12.3] * 2))
    # ... but the exact half BELOW the printed number is a spreadsheet's display
    # of it: Cedar Hill PW22-0010 item 5, 3.5 TON printed 4 (all columns agree)
    half = [dict(r, unit_raw="TON") for r in
            cells("half-up", 4.0, [("Engineer's Estimate", 4000.0, 14000.0),
                                   ("FM Utilities, LLC", 19653.3, 68786.55),
                                   ("Gra-Tex Utilities", 14600.0, 51100.0)])]
    check("display rounding: the exact half shown half-up",
          (recover_display_rounded_quantities(half), sorted({r["quantity"] for r in half})),
          ((1, 3), [3.5]))

    # 10. rule 2 in a tab with no unit or quantity column: a pay item whose
    #     text mentions a total stays, the total line goes
    grid = [[E, "Acme Paving LLC", E, "Bravo Construction", E],
            ["Description", "Unit Price", "Total", "Unit Price", "Total"],
            ["Mobilization", "$5,000.00", "$5,000.00", "$7,000.00", "$7,000.00"],
            ["Erosion control (total for all phases)", "$1,200.00", "$1,200.00",
             "$900.00", "$900.00"],
            ["Total", "$6,200.00", "$6,200.00", "$7,900.00", "$7,900.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "no-unit", {})
    check("total word inside a pay item", totals(recs),
          [(("Acme Paving LLC", "BASE BID"), 6200.0), (("Bravo Construction", "BASE BID"), 7900.0)])

    # 11. a price-less NOTE opening with a section word is not a banner: the
    #     base items under it stay base; a real banner still opens its section
    hdr = ["Item", "Description", "Unit", "Qty", "Unit Price", "Total", "Unit Price", "Total"]
    base_rows = [["1", "Mobilization", "LS", "1", "$5,000.00", "$5,000.00", "$7,000.00",
                  "$7,000.00"],
                 ["2", "Concrete pavement 8 in", "SY", "100", "$80.00", "$8,000.00", "$75.00",
                  "$7,500.00"]]
    for note in ("Bid alternates are priced on sheet 3 and are not part of this tab",
                 "Alternates not awarded"):
        grid = ([[E, E, E, E, "Acme Paving LLC", E, "Bravo Construction", E], hdr,
                 [note] + [E] * 7] + base_rows
                + [["Bid Alternate", E, E, E, E, E, E, E],
                   ["3", "Geogrid", "SY", "100", "$3.00", "$300.00", "$2.00", "$200.00"]])
        recs, _a = rows_from_grid(grid, "selftest", "note", {})
        check("note %r is no banner" % note,
              sorted(set((r["item_no"], r["bid_section"]) for r in recs)),
              [("1", "BASE BID"), ("2", "BASE BID"), ("3", "BID ALTERNATE")])

    # 12. a lone 'BID NO. 12 <project title>' over an unnamed column is no
    #     bidder; a SERIES of ordinals still is
    grid = [[E, E, E, E, "BID NO. 12 Main Street Paving", E, "Bravo Construction", E],
            hdr] + base_rows
    recs, a = rows_from_grid(grid, "selftest", "lone-ordinal", {})
    check("lone ordinal caption", a["bidders"], ["", "Bravo Construction"])
    grid = [[E, E, E, E, "BID NO. 1 Acme Paving LLC 100 Main St Dallas, TX 75201", E,
             "BID NO. 2 Bravo Construction", E], hdr] + base_rows
    recs, a = rows_from_grid(grid, "selftest", "ordinal-series", {})
    check("ordinal caption series", a["bidders"], ["Acme Paving LLC", "Bravo Construction"])

    # 13. an acknowledgement row repeated over every column ('Yes | Yes',
    #     'Addendum 1 | Addendum 1') is not the names row
    grid = [[E, E, E, E, "Harper Brothers Construction", E, "Ragel, Inc.", E],
            ["Addendums Returned", E, E, E, "Yes", E, "Yes", E],
            ["Addendum 1", E, E, E, "Addendum 1", E, "Addendum 1", E], hdr] + base_rows
    recs, a = rows_from_grid(grid, "selftest", "ack-rows", {})
    check("acknowledgement rows are not names", a["bidders"],
          ["Harper Brothers Construction", "Ragel, Inc."])

    # 14. North Richland Hills Bid 26-001 (Harmonson Road): the low bidder's
    #     captions merged into one cell 'UNIT TOTAL PRICE AMOUNT' beside an
    #     empty one, an FM-road address, and the misspelt 'ALTERNAE BID #1'
    #     glued onto the first alternate item.  Printed: TOTAL BASE BID DDM
    #     2,503,707.00 / SYB 2,535,798.50; Alt #1 subtotal DDM 74,799.00.
    grid = [["OWNER: THE CITY OF NORTH RICHLAND HILLS BID NO. 26-001", E, E, E,
             "BID NO. 1 DDM CONSTRUCTION CORPORATION 306 W. OVELY DRIVE LAKE DALLAS, TX 75065",
             E, "BID NO. 2 SYB CONSTRUCTION CO., INC 1017 S. FM 157 VENUS, TX 76084", E],
            ["ITEM NO.", "DESCRIPTION", "UNIT", "PLAN QUANTITY", "UNIT TOTAL PRICE AMOUNT", E,
             "UNIT PRICE", "TOTAL AMOUNT"],
            [E, "UNIT 1", E, E, E, E, E, E],
            ["1. 2.", "General Items & Removals General Site Preparation", "L.S.", "1",
             "$2,428,707.00", "$2,428,707.00", "$2,457,798.50", "$2,457,798.50"],
            [E, "Excavation", "CY", "1,004", "$70.00", "$70,280.00", "$35.00", "$35,140.00"],
            [E, "Irrigation Restoration Allowance", "EA.", "1", "$4,720.00", "$4,720.00",
             "$42,860.00", "$42,860.00"],
            [E, "TOTAL BASE BID", E, E, E, "$2,503,707.00", E, "$2,535,798.50"],
            [E, "UNIT 2", E, E, E, E, E, E],
            ["1. 2.", "ALTERNAE BID #1 6 Inch Thick Flex Base", "L.S.", "7,987", "$19.00",
             "$151,753.00", "$31.00", "$247,597.00"],
            [E, "6 Inch Lime Stabilized Subgrade", "L.F.", "7,987", "$12.00", "-$95,844.00",
             "$8.50", "-$67,889.50"],
            [E, "Lime", "L.S.", "129", "$400.00", "-$51,600.00", "$425.00", "-$54,825.00"],
            ["SUBTOTAL BID UNIT 2 ITEMS 1 THRU 4", E, E, E, E, "$4,309.00", E,
             "$124,882.50"]]
    recs, a = rows_from_grid(grid, "selftest", "nrh-26-001", {})
    check("nrh 26-001 bidders", a["bidders"],
          ["DDM CONSTRUCTION CORPORATION", "SYB CONSTRUCTION CO., INC"])
    check("nrh 26-001 totals", totals(recs),
          [(("DDM CONSTRUCTION CORPORATION", "ALTERNATE BID #1"), 4309.0),
           (("DDM CONSTRUCTION CORPORATION", "BASE BID"), 2503707.0),
           (("SYB CONSTRUCTION CO., INC", "ALTERNATE BID #1"), 124882.5),
           (("SYB CONSTRUCTION CO., INC", "BASE BID"), 2535798.5)])

    # 15. Coppell 2021-04-19: every item packed into ONE table row of
    #     newline-joined cells (a trailing '$ -' line in one of them) is split
    #     back into its rows; a row whose text merely wraps is left whole
    table = [[None, None, None, None, "DDM Construction Corp.", None, "Ragel, Inc.", None],
             ["ITEM NO.", "DESCRIPTION OF ITEMS", "QUANTITY", "UNIT", "UNIT COST", "TOTAL",
              "UNIT COST", "TOTAL"],
             ["1\n2\n3", "Mobilization\nProject Signage\nUnclassified Excavation", "1\n2\n100",
              "LS\nEA\nCY", "$260,000.00\n$1,000.00\n$30.00",
              "$ 260,000.00\n$ 2,000.00\n$ 3,000.00", "$274,000.00\n$5,000.00\n$25.00",
              "$ 274,000.00\n$ 10,000.00\n$ 2,500.00\n$ -"],
             ["4", "Remove and Dispose Reinforced Concrete Pavement,\nConcrete Curb, Concrete\n"
              "Sidewalk", "10,750", "SY", "$12.00", "$129,000.00", "$14.00", "$150,500.00"]]
    split = split_packed_rows(table)
    check("packed row split", [r[0] for r in split[2:]], ["1", "2", "3", "4"])
    recs, _a = rows_from_grid([[txt(c) for c in r] for r in split], "selftest", "coppell", {})
    check("coppell-shaped tab", totals(recs),
          [(("DDM Construction Corp.", "BASE BID"), 394000.0),
           (("Ragel, Inc.", "BASE BID"), 437000.0)])

    # ---- the 2026-11-19 zero-row repairs (reports/zero_row_files.md) -------
    # 16. total captions the pair vocabulary lacked -- Mesquite 'Subtotal',
    #     Frisco 'SUBTOTAL COST', Rowlett 'BID TOTAL', the 'Bid Value'
    #     template, Coppell 'Unit Price Bid | Amount Bid' and 'UNIT $ | TOTAL $'
    #     -- and the look-alikes that must still pair with nothing
    for cells, want in ((["Unit Price", "Subtotal"], [(0, 1)]),
                        (["UNIT COST", "SUBTOTAL COST"], [(0, 1)]),
                        (["UNIT PRICE", "BID TOTAL"], [(0, 1)]),
                        (["Unit Price", "Bid Value"], [(0, 1)]),
                        (["Unit Price Bid", "Amount Bid"], [(0, 1)]),
                        (["UNIT $", "TOTAL $"], [(0, 1)]),
                        (["Unit Price", "Total Unit Price", "", "Total"], []),
                        (["Total Unit Price", "Extension"], []),
                        (["Subtotal", "$1,000.00"], []),
                        (["Avg. Unit Price Bid", "Unit Price Bid", "Amount Bid"], [(1, 2)]),
                        (["UNIT PRICE", "TOTAL ITEM COST"], [(0, 1)]),
                        # NRH 6222: 'Unit Bid Price | (blank) | Amount Bid' x N
                        (["Unit", "Unit Bid Price", E, "Amount Bid", "Unit Bid Price", E,
                          "Amount Bid"], [(1, 3), (4, 6)]),
                        # ... but one gapped block, a bare 'Unit', or a row that
                        # already has an adjacent pair, is not that template
                        (["Unit Bid Price", E, "Amount Bid"], []),
                        (["Unit", E, "Total", "Unit", E, "Total"], []),
                        (["Unit Price", "Total", "Unit Bid Price", E, "Amount Bid",
                          "Unit Bid Price", E, "Amount Bid"], [(0, 1)])):
        check("detect_price_pairs(%r)" % cells, detect_price_pairs(cells), want)
    grid = [[E, E, E, E, "GRod Construction LLC", E, E, "Infra Construction", E, E],
            ["BASE BID"] + [E] * 9,
            ["Item No.", "Spec. Item", "Est. Quantity", "Unit", "Unit Bid Price", E,
             "Amount Bid", "Unit Bid Price", E, "Amount Bid"],
            ["101", "GENERAL SITE PREPARATION", "1", "LS", "$ 60,000.00", E, "$ 60,000.00",
             "$ 12,396.00", E, "$ 12,396.00"],
            ["104", "TREE PROTECTION FENCING", "3", "EA", "$ 600.00", E, "$ 1,800.00",
             "$ 300.00", E, "$ 900.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "nrh-6222", {})
    check("price caption over two columns", totals(recs),
          [(("GRod Construction LLC", "BASE BID"), 61800.0),
           (("Infra Construction", "BASE BID"), 13296.0)])
    grid = [[E, E, E, E, "Donny P Brown, Inc dba B & B Concrete Sawing", E,
             "Ken-Do Contracting LP", E],
            ["Item No", "Item Description", "Units", "Bid Qty", "Unit Price", "Subtotal",
             "Unit Price", "Subtotal"],
            ["1", "Remove and Replace Concrete Curb and Gutter", "LF", "1000", "$ 40.00",
             "$ 40,000.00", "$ 54.00", "$ 54,000.00"],
            ["2", "Remove and Replace 4\" Concrete Sidewalk", "SF", "2000", "$ 9.00",
             "$ 18,000.00", "$ 8.50", "$ 17,000.00"]]
    recs, _a = rows_from_grid(grid, "selftest", "mesquite-subtotal", {})
    check("mesquite 'Subtotal' tab", totals(recs),
          [(("Donny P Brown, Inc dba B & B Concrete Sawing", "BASE BID"), 58000.0),
           (("Ken-Do Contracting LP", "BASE BID"), 71000.0)])

    # 17. Coppell 2021-5552 / Frisco 2203-047: the firms named on the row
    #     UNDER the captions; a caption row ('Yes | Yes'), a priced first row
    #     and names already found above are all left as they were
    hdr8 = ["ITEM NO.", "DESCRIPTION", "UNIT", "BID QTY", "Unit Price", "Extended Amount",
            "Unit Price", "Extended Amount"]
    items8 = [["101", "Mobilization (Not to Exceed 5%)", "LS", "1", "$52,000.00", "$52,000.00",
               "$25,600.00", "$25,600.00"],
              ["102", "Furnish, Install & Maintain Project Signs", "EA", "2", "$700.00",
               "$1,400.00", "$800.00", "$1,600.00"]]
    recs, a = rows_from_grid([hdr8, ["PAVING IMPROVEMENTS", E, E, E, "QUALITY EXCAVATION, LLC",
                                     E, "JESKE CONSTRUCTION COMPANY", E]] + items8,
                             "selftest", "names-below", {})
    check("names under the header", totals(recs),
          [(("JESKE CONSTRUCTION COMPANY", "BASE BID"), 27200.0),
           (("QUALITY EXCAVATION, LLC", "BASE BID"), 53400.0)])
    recs, a = rows_from_grid([hdr8, [E, E, E, E, "Yes", E, "Yes", E]] + items8,
                             "selftest", "caption-below", {})
    check("a caption row under the header names nobody", a["bidders"], ["", ""])
    recs, a = rows_from_grid([hdr8, ["11", "Remove and replace concrete", "SF", "0",
                                     "$ _________", "$ _____________", "$ _________",
                                     "$ _____________"]] + items8,
                             "selftest", "blank-form", {})
    check("a blank bid form's price blanks name nobody", a["bidders"], ["", ""])
    recs, a = rows_from_grid([hdr8] + items8 + [[E, E, E, E, "Delta Paving LLC", E,
                                                 "Echo Construction", E]],
                             "selftest", "priced-first", {})
    check("names are not read past the first priced row", a["bidders"], ["", ""])
    recs, a = rows_from_grid([[E, E, E, E, "Acme Paving LLC", E, "Bravo Construction", E], hdr8,
                              ["PAVING", E, E, E, "Delta Paving LLC", E, "Echo Construction", E]]
                             + items8, "selftest", "names-above", {})
    check("names above win over a row below", a["bidders"],
          ["Acme Paving LLC", "Bravo Construction"])

    # 18. a header-less table whose QUANTITY column abuts the money block
    #     (Mansfield 21-4436: an odd 15-column numeric run), and the first
    #     row of a continuation table, which used to be read as the names row
    def m_row(i, d, u, q, prices):
        return [str(i), "SS 9%d0" % i, d, u, q] + [x for p in prices
                                                  for x in ("$%.2f" % p, "$%.2f" % (p * float(q)))]
    cont = [m_row(1, "Mobilization/Demobilization", "LS", "1", (251900.0, 226000.25)),
            m_row(2, "Right-Of-Way Preparation", "LS", "1", (126000.0, 35000.0)),
            m_row(3, "Dry Riprap (Type A)", "SY", "520", (105.0, 145.0)),
            m_row(4, "Hydromulching", "SY", "1151", (2.5, 3.0)),
            m_row(5, "12\" PVC Sewer - Open Cut w/CCTV", "LF", "795", (160.0, 130.0))]
    synth = infer_headerless_layout(cont)
    check("odd money run: quantity found by arithmetic", synth,
          ["Item", E, "Description", "Unit", "Quantity", "Unit Price", "Total", "Unit Price",
           "Total"])
    recs, a = rows_from_grid(cont, "selftest", "mansfield-odd", {},
                             fallback_names=["Engineer's Estimate", "Blackrock Construction"])
    check("continuation table keeps its first row",
          sorted(set(r["item_no"] for r in recs)), ["1", "2", "3", "4", "5"])
    bad_q = [r[:4] + [str(7 * (k + 3))] + r[5:] for k, r in enumerate(cont)]
    check("odd money run whose extra column is no quantity is refused",
          infer_headerless_layout(bad_q), None)
    title = ["Bid Tab - Upper Reuse 24-Inch Pipeline Phase 2"] + [E] * 8
    names_row = ["Item Description", "Spec Item", "Quantity Required", "Unit of Measure", E,
                 "R Construction", E, "Belt Construction", E]
    upper = [title, names_row] + [[r[2], r[1], r[4], r[3], E] + r[5:] for r in cont]
    recs, a = rows_from_grid(upper, "selftest", "upper-reuse", {})
    check("names in the row the inferred header replaces", a and a["bidders"],
          ["R Construction", "Belt Construction"])
    banner = [names_row, ["ALTERNATE #1", E, E, E, E, E, E, E, E]] + upper[2:]
    recs, a = rows_from_grid(banner, "selftest", "banner-kept", {})
    check("a banner above the first priced row still opens its section",
          sorted(set(r["bid_section"] for r in recs)), ["ALTERNATE #1"])
    # Collin County 2025-020: firms on grid[0], the purchasing co-op under them
    coop = [[E, E, E, E, E, "United Rentals, Inc.", E, "Clifford Power Systems", E],
            [E, E, E, E, E, "Choice Partners", "22/052SG-03", "BuyBoard", "740-24"]] + upper[2:]
    recs, a = rows_from_grid(coop, "selftest", "coop-row", {})
    check("names on grid[0] beat a co-op row under them", a and a["bidders"],
          ["United Rentals, Inc.", "Clifford Power Systems"])
    # Frisco 2102-050 page 2: 'Low Bidder', then 'BIDDER 1 | BIDDER 2', then
    # the firms, then items -- labels are not names, the firms are
    labels = [[E, E, E, E, E, "Low Bidder", E, E, E],
              [E, E, E, E, E, "BIDDER 1", E, "BIDDER 2", E],
              [E, E, E, E, E, "McMahon Contracting, LP", E, "Mario Sinacola & Sons", E]]
    recs, a = rows_from_grid(labels + upper[2:], "selftest", "rank-labels", {})
    check("rank and ordinal labels are not names", a and a["bidders"],
          ["McMahon Contracting, LP", "Mario Sinacola & Sons"])
    check("label cells", [_caption_only(s) for s in (
        "Low Bidder", "BIDDER 2", "Apparent Low Bidder", "ENGINEER'S ESTIMATE",
        "Engineers Estimate", "Lowe Construction LLC")],
        [True, True, True, False, False, False])

    # 19. Mansfield 21-4092: 'QUANTITY | UNIT' printed over 'LS | 1'; a tab
    #     whose captions fit, and one with a single odd row, are left alone
    hdr_q = ["ITEM NO.", "DESCRIPTION OF ITEMS", "QUANTITY", "UNIT", "UNIT COST", "TOTAL",
             "UNIT COST", "TOTAL"]
    swapped = [["A1", "Right of Way Preparation", "LS", "1", "$12,500.00", "$12,500.00",
                "$32,000.00", "$32,000.00"],
               ["A2", "Unclassified Street Excavation", "CY", "400", "$40.00", "$16,000.00",
                "$40.00", "$16,000.00"],
               ["A3", "7\" Reinforced Concrete Pavement", "SY", "611", "$76.00", "$46,436.00",
                "$78.00", "$47,658.00"],
               ["B1", "18\" Class III Reinforced Concrete Pipe", "LF", "54", "$130.00",
                "$7,020.00", "$155.00", "$8,370.00"]]
    names_q = [E, E, E, E, "GROD Construction, LLC", E, "McClendon Construction Co., Inc.", E]
    recs, a = rows_from_grid([names_q, hdr_q] + swapped, "selftest", "swapped", {})
    check("unit and quantity swapped by content",
          sorted(set((r["item_no"], r["unit"], r["quantity"]) for r in recs)),
          [("A1", "LS", 1.0), ("A2", "CY", 400.0), ("A3", "SY", 611.0), ("B1", "LF", 54.0)])
    fit = [r[:2] + [r[3], r[2]] + r[4:] for r in swapped]
    recs, a = rows_from_grid([names_q, hdr_q] + fit, "selftest", "fit", {})
    check("captions that fit their cells are not swapped",
          sorted(set((r["unit_raw"], r["quantity"]) for r in recs))[:2],
          [("CY", 400.0), ("LF", 54.0)])
    mixed = fit[:3] + swapped[3:]
    recs, a = rows_from_grid([names_q, hdr_q] + mixed, "selftest", "mixed", {})
    check("one odd row does not swap a tab", sorted(set(r["unit_raw"] for r in recs)),
          ["54", "CY", "LS", "SY"])

    # 20. a firm running straight into its city (Arlington Lower Johnson Creek)
    for raw, want in (("Dredgit Corporation Houston, Texas", "Dredgit Corporation"),
                      ("Dredgit Corporation Houston, TX 77001", "Dredgit Corporation"),
                      ("Renda Co Inc Roanoke, Texas", "Renda Co Inc"),
                      ("SYB Construction Company, Inc. Irving, Texas",
                       "SYB Construction Company, Inc."),
                      ("Grod Construction, LLC Boyd, TX", "Grod Construction, LLC"),
                      ("McMahon Contracting, LP Grand Prairie, Texas", "McMahon Contracting, LP"),
                      ("Houston, Texas", ""), ("Fort Worth, TX 76106", ""),
                      ("Colleyville, TX", "")):
        check("strip_address(%r)" % raw, strip_address(raw), want)
    check("looks_like_bidder(firm + city)",
          looks_like_bidder("Dredgit Corporation Houston, Texas"), True)

    # 21. a unit written as a rate (Richardson 35-25, Mansfield 22-4816)
    for raw, want in (("PER LUMP SUM", "LS"), ("PER SQUARE YARD", "SY"), ("PER TON", "TON"),
                      ("$ Per SY", "SY"), ("Per truck", "PER TRUCK")):
        check("norm_unit(%r)" % raw, norm_unit(raw), want)

    # 23. Mansfield 21-4436: the firm names printed above the ruled table, over
    #     each column pair, with the 'UNIT' / 'PRICE AMOUNT' captions between;
    #     a title running across the pairs is not a names row
    class _Col:
        def __init__(self, x0, x1):
            self.cells = [(x0, 130.0, x1, 140.0), (x0, 140.0, x1, 150.0)]

    class _Table:
        bbox = (36.0, 124.0, 765.0, 555.0)
        columns = [_Col(36, 56), _Col(56, 240), _Col(240, 523), _Col(523, 546),
                   _Col(546, 583), _Col(583, 634), _Col(634, 682), _Col(682, 721),
                   _Col(721, 766)]

    class _Page:
        def __init__(self, lines):
            self.lines = lines

        def extract_words(self):
            return [{"text": t, "x0": x0, "x1": x1, "top": top, "bottom": top + 6}
                    for top, ws in self.lines for t, x0, x1 in ws]
    m_pairs = [(5, 6), (7, 8)]           # x-ranges 583-682 and 682-766
    captions = [(109.0, [("UNIT", 602, 616), ("UNIT", 695, 709)]),
                (116.0, [("PRICE", 600, 619), ("AMOUNT", 655, 680), ("PRICE", 693, 712),
                         ("AMOUNT", 740, 765)])]
    page = _Page([(102.0, [("ENGINEER'S", 600, 634), ("ESTIMATE", 636, 670),
                           ("Blackrock", 692, 719), ("Construction", 721, 760)])] + captions)
    check("names above the ruled table", names_above_table(page, _Table(), m_pairs),
          ["ENGINEER'S ESTIMATE", "Blackrock Construction"])
    page = _Page([(100.0, [("City", 600, 614), ("of", 617, 624), ("Mansfield", 627, 660),
                           ("Utility", 672, 690), ("Relocation", 693, 730)])] + captions)
    check("a title across the pairs names nobody",
          names_above_table(page, _Table(), m_pairs), None)
    check("caption lines alone name nobody",
          names_above_table(_Page(captions), _Table(), m_pairs), None)
    #     ... nor an amount line, nor anything over a one-column table
    page = _Page([(102.0, [("25%", 600, 612), ("Contingency", 614, 660),
                           ("$3,318.75", 692, 730), ("Blackrock", 735, 760)])] + captions)
    check("a line of amounts names nobody", names_above_table(page, _Table(), m_pairs), None)
    page = _Page([(102.0, [("ENGINEER'S", 600, 634), ("ESTIMATE", 636, 670)])] + captions)
    check("a one-column table is not named from above",
          names_above_table(page, _Table(), [(5, 6)]), None)
    #     ... and the page title over the table says which bid it is
    for title, want in ((["ALTERNATIVE", "BID", "TABULATION"], "ALTERNATIVE BID TABULATION"),
                        (["BASE", "BID", "TABULATION"], ""),
                        (["BASE", "BID", "AND", "ALTERNATE", "BID", "TABULATION"], ""),
                        (["TOTAL", "BID", "ALTERNATE", "#1", "-", "SIDEWALK", "$24,000.00"], ""),
                        (["US", "HWY", "287", "Utility", "Relocation"], "")):
        words = [(t, 300 + 60 * k, 350 + 60 * k) for k, t in enumerate(title)]
        check("alternate_title_above(%r)" % " ".join(title),
              alternate_title_above(_Page([(88.0, words)] + captions), _Table()), want)
    recs, a = rows_from_grid([hdr8] + items8, "selftest", "alt-page", {},
                             fallback_names=["Acme Paving LLC", "Bravo Construction"],
                             section0="ALTERNATIVE BID TABULATION")
    check("a table opened in the page title's section",
          sorted(set(r["bid_section"] for r in recs)), ["ALTERNATIVE BID TABULATION"])

    # 24. Dallas County 2026-030 (Simonds Rd bridge): title, names, then a
    #     caption row 'Total Unit Price | Extension' above the items.  The
    #     header is inferred, the walk passes the captions and reaches the
    #     firms; Collin County 2025-101's 'NET PRICE PER UNIT' names nobody.
    dc = [["Final SCHEDULE 1 -- PAVING & DRAINAGE", E, E, E, E, E, E, E, E, E],
          [E, E, E, E, E, "Grey Sky Construction LLC", E, E, "Ragle Inc", E],
          [E, "Item No.", "Cost Category Description", "Unit", "Quantity", "Total Unit Price",
           "Extension", E, "Total Unit Price", "Extension"]]
    dc_items = [["1", "100-7001", "PREPARING ROW", "AC", "0.50", "$100,000.00", "$50,000.00", E,
                 "$94,000.00", "$47,000.00"],
                ["2", "110-7003", "EXCAVATION (SPECIAL)", "CY", "29.00", "$100.00", "$2,900.00",
                 E, "$204.00", "$5,916.00"],
                ["3", "164-7019", "HYDRO MULCH (PERM_RURAL_SAND)", "SY", "2430.00", "$4.00",
                 "$9,720.00", E, "$1.25", "$3,037.50"],
                ["4", "420-7060", "CL C CONC (PILE ENCASEMENT)", "CY", "41.80", "$2,000.00",
                 "$83,600.00", E, "$1,240.00", "$51,832.00"]]
    recs, a = rows_from_grid(dc + dc_items, "selftest", "dallas-county", {})
    check("a caption row above an inferred header is walked past", totals(recs),
          [(("Grey Sky Construction LLC", "BASE BID"), 146220.0),
           (("Ragle Inc", "BASE BID"), 107785.5)])
    check("caption-only cells", [_caption_only(s) for s in (
        "Total Unit Price", "NET PRICE PER UNIT", "PERCENT DISCOUNT (ENTER NEGATIVE VALUE)",
        "Bid Amount", "Grey Sky Construction LLC", "Unit Construction Co", "")],
        [True, True, True, True, False, False, False])

    # 25. NRH 6473: a borrowed 21-column header over an 8-column money table
    #     reads garbage that fails its own arithmetic and is refused; a
    #     continuation that extends is kept, as is one with nothing to check
    def rq(q, p, e, d="Remove and replace curb"):
        return {"quantity": q, "unit_price": p, "extended_reported": e, "description": d}
    check("inherited_parse_fits", [
        inherited_parse_fits([rq(45.0, 27000.0, None, "$30,600.00"),
                              rq(55.0, 6050.0, None, "$3,740.00"),
                              rq(23.0, 18975.0, None, "$3,300.00")]),
        inherited_parse_fits([rq(45.0, 27000.0, 9.0), rq(55.0, 6050.0, 8.0),
                              rq(23.0, 18975.0, 7.0), rq(13.0, 18200.0, 3.0)]),
        inherited_parse_fits([rq(600.0, 51.0, 30600.0), rq(110.0, 34.0, 3740.0),
                              rq(825.0, 4.0, 3300.0)]),
        inherited_parse_fits([rq(None, 51.0, None), rq(None, 34.0, None)])],
        [False, False, True, True])

    # 26. read_pdf_best: equally clean, close in coverage -> more NAMED rows
    #     wins (Arlington PWSM24005: tables named 4 of 5 bidders, coords 5)
    def fake(bidders, n=10):
        return [{"src_sheet": "p1t0", "bidder_raw": b, "quantity": 2.0, "unit_price": 5.0,
                 "extended_reported": 10.0, "unit": "LS", "unit_raw": "LS"}
                for b in bidders for _ in range(n)]
    five = ["Meca Construction LLC", "Ken-Do Construction", "Thomas II Development LLC",
            "Albros Sheet Metal LLC", "Don Smith Concrete LLC"]
    g = globals()
    saved = (g["read_pdf"], g["read_pdf_coords"])
    try:
        g["read_pdf"] = lambda path, st: (fake(five[:1] + [""] + five[2:]), [], [])
        g["read_pdf_coords"] = lambda path, st: fake(five)
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked = st["pdf_strategy"]["selftest.pdf"]["chosen"]
        g["read_pdf"] = lambda path, st: (fake(five), [], [])
        g["read_pdf_coords"] = lambda path, st: fake(five[:1] + [""] + five[2:])
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked2 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
        # Mesquite 2020-095: every table row named, a few more coordinate
        # rows -- no named-row tie-break; the table parse stays
        g["read_pdf"] = lambda path, st: (fake(five), [], [])
        g["read_pdf_coords"] = lambda path, st: fake(five, n=11)
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked3 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
        # ... and before either, the totals the document prints (Arlington
        # PWST19007): every bidder's coordinate total is printed -> coords;
        # the table totals are -> tables
        g["read_pdf"] = lambda path, st: (fake(five), [], ["TOTAL BASE BID AMOUNT $ 1 10.00"])
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked4 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
        g["read_pdf"] = lambda path, st: (fake(five), [], ["TOTAL BASE BID AMOUNT $100.00"])
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked5 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
        # ... but not for a parse that names fewer firms (Grand Prairie Trinity
        # Blvd: page subtotals under 'Item 11', 'Item 15', 'LUMIN8')
        junk = ["Item 11", "Item 15", "LUMIN8", "Item 16", "Item 17"]
        g["read_pdf"] = lambda path, st: (fake(five), [], ["TOTAL BASE BID AMOUNT $ 1 10.00"])
        g["read_pdf_coords"] = lambda path, st: fake(junk, n=11)
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked6 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
    finally:
        g["read_pdf"], g["read_pdf_coords"] = saved
    check("tie broken by named rows", (picked, picked2, picked3), ("coords", "tables", "tables"))
    check("tie broken by printed totals", (picked4, picked5, picked6), ("coords", "tables", "tables"))
    check("printed_amounts", sorted(printed_amounts(["TOTAL $ 1 6,966,028.50 and $1,200",
                                                     "(3.25)"])), [325, 120000, 696602850, 1696602850])

    # 27. corrections found by re-measuring the corpus with rules 16-26
    #  a. Richardson 86-23: 'UNIT | BID TOTAL' on the names row is no pair
    #     (a quantity column); 'Unit Cost | Total' under it is the header
    rich = [["ITEM NO.", "DESCRIPTION", "UNIT", "BID TOTAL", "Dean Construction", E,
             "Homerun LLC", E],
            [E, E, E, E, "Unit Cost", "Total", "Unit Cost", "Total"],
            ["1", "Mobilization, Bonds, Insurance and all Permitting", "L.S.", "1",
             "$17,866.00", "$17,866.00", "$20,750.00", "$20,750.00"],
            ["4", "Temporary Orange Fencing for Tree Protection", "EA.", "9", "$134.00",
             "$1,206.00", "$200.00", "$1,800.00"]]
    check("detect_price_pairs(UNIT | BID TOTAL)", detect_price_pairs(rich[0]), [])
    check("detect_price_pairs(UNIT | TOTAL) as before",
          detect_price_pairs(["UNIT", "TOTAL"]), [(0, 1)])
    recs, a = rows_from_grid(rich, "selftest", "richardson-86-23", {})
    check("a quantity column captioned BID TOTAL", totals(recs),
          [(("Dean Construction", "BASE BID"), 19072.0), (("Homerun LLC", "BASE BID"), 22550.0)])
    #  b. a LONE name under a header must say it is a firm; interleaved text
    #     with amounts in it names nobody
    one = [["ITEM", "DESCRIPTION", "UNIT", "QTY", "Unit Price", "Total"]]
    it1 = [["1", "Kayak launch", "LS", "1", "$5,000.00", "$5,000.00"]]
    check("a lone caption under the header names nobody",
          names_below_header(one + [[E, E, E, E, "Predecessors", E]] + it1, 0, [(4, 5)]), {})
    check("a lone firm under the header is read",
          names_below_header(one + [[E, E, E, E, "North Rock Construction, LLC", E]] + it1, 0,
                             [(4, 5)]), {4: "North Rock Construction, LLC"})
    check("interleaved text names nobody", names_below_header(
        [hdr8, [E, E, E, E, "e$m b e d m e n 2 t,7 &0. 0b0ac", E,
                "th$ic c u r b , i n c1 l7u0d.in0g0", E]] + items8, 0, [(4, 5), (6, 7)]), {})
    #  c. above an inferred header, the table's own caption row is no names
    #     row (Grand Prairie's materials-testing pay estimates)
    pay = [["ITEM NO.", "DESCRIPTION OF ITEM", "Unit of Measure", "Approved Quantity",
            "Unit Price", "Value of Completed Work"],
           [E, "Concrete", E, E, E, E],
           ["1", "Compression Test Cylinders", "EACH", "240", "$17.00", "$4,080.00"],
           ["2", "Air Content, Slump, Temperature", "HOUR", "200", "$55.00", "$11,000.00"],
           ["3", "Next Day Cylinder pickup", "HOUR", "60", "$55.00", "$3,300.00"],
           ["4", "Vehicle charge", "TRIP", "120", "$35.00", "$4,200.00"]]
    recs, a = rows_from_grid(pay, "selftest", "pay-estimate", {})
    check("a pay estimate's captions name nobody", (a or {}).get("bidders"), [""])
    #  d. a borrowed header over rows that do not fit it is refused; one whose
    #     description column repeats the item number (Collin 54857) is kept
    def ri(it, d, p):
        return {"item_no": it, "description": d, "quantity": None, "unit_price": p,
                "extended_reported": None}
    check("inherited_parse_fits (misfit rows)", [
        inherited_parse_fits([ri("38", "38", 139.3), ri("39", "39", 1264.71),
                              ri("40", "40", 1592.0)]),
        inherited_parse_fits([ri("FOREMAN / SUPT", "HR", 6487.2), ri("FUEL TRUCK", "HR", 1563.68),
                              ri("HAUL TRUCK W/ LOW BOY", "HR", 1540.68)]),
        inherited_parse_fits([ri("$374,852.00", "Proposed New Funding:", 89146.0)]),
        inherited_parse_fits([ri("7", "Furnish 8-inch PVC pipe (LF)", 51.0),
                              ri("8", "Pipe bedding", 4.0)])],
        [True, False, False, True])
    #  e. an inferred table lends no header to the next one; a read one does
    g3 = [["names"], ["captions"], ["data"]]
    check("header_prefix", [header_prefix(g3, {"header_row": 2, "inferred": True}, False),
                            header_prefix(g3, {"header_row": 2}, True),
                            header_prefix(g3, {"header_row": 2}, False)],
          [None, None, [["names"], ["captions"]]])
    #     ... and a single name is lent no further than the next page
    x1 = [{"item_no": "47"}]
    nl = {"inferred": True, "bidders": ["", ""]}
    check("may_borrow_header", [
        may_borrow_header([], None, 12, 1, 5, 1, 9),               # no header: as before
        may_borrow_header(x1, nl, 12, 2, 2, 1, 12),                # Farmers Branch R2025-011
        may_borrow_header(x1, nl, 6, 1, 170, 133, 6),              # one bidder, 37 pages on
        may_borrow_header(x1, dict(nl, bidders=["Acme Paving LLC", ""]), 12, 2, 2, 1, 12),
        may_borrow_header(x1, {"bidders": ["", ""]}, 12, 2, 2, 1, 12),
        may_borrow_header(x1, nl, 12, 4, 61, 59, 8),               # Dry Branch pay estimate
        may_borrow_header([], None, 0, 0, 2, -9, 9)],
        [True, True, False, False, False, False, False])
    # (2026-11-19 Mac verification: a LIST of names now obeys the same one-page
    # reach as a single name -- the third case used to expect the list back.)
    check("lend_names", [lend_names(["Macia Specialty Contracting"], 169, 132),
                         lend_names(["Macia Specialty Contracting"], 133, 132),
                         lend_names(["Acme Paving LLC", "Bravo Construction"], 169, 132),
                         lend_names(None, 1, 0)],
          [None, ["Macia Specialty Contracting"], None, None])
    #  f. a firm glued to its street address on one line (Mansfield 21-4017)
    for raw, want in (("Ed Bell Constructin Co. 10605 Harry Hines Dallas, Texas 75220 "
                       "214-358-6581", "Ed Bell Constructin Co."),
                      ("McMahon Contracting L.P. 3019 Roy Orr Blvd. Grand Prairie, Texas 75050",
                       "McMahon Contracting L.P."),
                      ("BID OF Excel 4 Construction, LLC 5733 Hart Street Fort Worth, Texas 76112",
                       "BID OF Excel 4 Construction, LLC"),
                      ("Gra-Tex Utilities Inc. PO Box 1038 Kennedale, TX", "Gra-Tex Utilities Inc."),
                      ("Item 12. Ed Bell Construction 10605 Harry Hines Blvd. Dallas, Texas 75220",
                       "Item 12. Ed Bell Construction"),
                      ("Rebcon, LLC 400 109th", "Rebcon, LLC 400 109th")):
        check("strip_address(%r)" % raw, strip_address(raw), want)

    # 22. a workbook / a Word document published under a .pdf name
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        wbp = os.path.join(td, "Exhibit_A_-_Bid_Tabulation.pdf")
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in ([None, None, None, "H&H Concrete", None, "Custom Crete", None],
                    ["Item", "Quantity", "Unit", "Unit Price", "Total", "Unit Price", "Total"],
                    ['Class "A" 3,000 psi - 5 sack', 250, "Cubic Yard", 173, 43250, 180.9, 45225],
                    ['Class "C" 3,600 psi - 6 sack', 50, "Cubic Yard", 183, 9150, 187.9, 9395]):
            ws.append(row)
        wb.save(wbp)
        docp = os.path.join(td, "Bid_Tab.pdf")
        with zipfile.ZipFile(docp, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", "<document/>")
        pdfp = os.path.join(td, "real.pdf")
        with open(pdfp, "wb") as fh:
            fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        check("sniff_office", [sniff_office(p) for p in (wbp, docp, pdfp)], ["xlsx", "docx", ""])
        recs, _aud, _s = read_xlsx(wbp, {})
        check("a workbook named .pdf is read", totals(recs),
              [(("Custom Crete", "BASE BID"), 54620.0), (("H&H Concrete", "BASE BID"), 52400.0)])

    # 28. fixes from the Mac verification of rules 16-27 (2026-11-19;
    #     reports/parser_fix_verification.md)
    #  a. estimate columns and columns derived from the bids are no bidders
    check("ENGINEER_RE, estimates", [ENGINEER_RE.search(s) is not None for s in (
        "Engineer", "ENGINEER'S", "Engineer's EOPC", "Engineer's EstimRaotaed way", "OPC",
        "Westwood Professional Services", "ENGINEERING ESTIMATE", "City Estimate",
        "Garver OPCC", "Neel-Schaffer, Inc.")], [True] * 10)
    check("ENGINEER_RE, firms that bid", [ENGINEER_RE.search(s) is not None for s in (
        "WG Engineering & Construction, LLC", "Knight Engineering & Construction",
        "Engineered Solutions LLC", "Rebcon, Inc. (estimating@example.com)",
        "Tellus Consulting", "WESTRA CONSULTANTS, LLC", "Roy-Comark Consulting Services",
        "JABG Construction & Engineer (Thomas 11 Development LLC)",
        "Jim Bowman Construction", "Value Paving LLC")], [False] * 10)
    check("COMPUTED_COL_RE, bidders named first", [COMPUTED_COL_RE.match(s) is not None for s in (
        "BIDDER AVERAGE", "Bid Average", "All Bidders' Average", "Average Bid",
        "Low Bid Construction", "Bidder 1")], [True, True, True, True, False, False])
    check("DERIVED_COL_RE", [DERIVED_COL_RE.match(s) is not None for s in (
        "BID Opening", "Value Engineered", "Value Paving LLC", "Opening Day Builders")],
        [True, True, False, False])
    eng = [["ITEM", "DESCRIPTION", "UNIT", "QTY", "Engineer", E, "Tiseo Paving", E],
           [E, E, E, E, "Unit Price", "Total", "Unit Price", "Total"],
           ["P1", "Mobilization (5% Max)", "LS", "1", "$800,000.00", "$800,000.00",
            "$790,500.00", "$790,500.00"],
           ["P2", "General Site Preparation", "LS", "1", "$300,000.00", "$300,000.00",
            "$895,000.00", "$895,000.00"]]
    recs, a = rows_from_grid(eng, "selftest", "frisco-2510-007", {})
    check("a column headed 'Engineer' is the estimate",
          sorted({(r["bidder_raw"], r["is_engineer_estimate"]) for r in recs}),
          [("Engineer", True), ("Tiseo Paving", False)])
    #  b. rule 15 counts columns of two or more rows, on grand-total lines only
    one_row = [{"bidder_raw": "Acme Paving LLC", "unit_price": 5000.0, "quantity": 1.0,
                "extended_reported": 5000.0, "item_no": "1", "bid_section": "BASE BID"}]
    check("a one-row column proves nothing", printed_total_hits(one_row, {500000}), 0)
    check("total_line_amounts", sorted(total_line_amounts([
        "SUBTOTAL MOBILIZATION, BONDS & SWPPP : $ 205,000.00 $ 205,000.00",
        "Sub-Total $ 1,000.00", "201 Site Preparation 1 LS $ 28,500.00 $ 28,500.00",
        "TOTAL BASE BID AMOUNT $ 2,598,011.95 $ 4,384,356.00"])),
        [259801195, 438435600])

    def fake2(spec):
        return [{"src_sheet": "p1t0", "bidder_raw": b, "quantity": 1.0, "unit_price": amt,
                 "extended_reported": amt, "unit": "LS", "unit_raw": "LS",
                 "item_no": str(i + 1), "bid_section": "BASE BID"}
                for b, n, amt in spec for i in range(n)]
    g = globals()
    saved = (g["read_pdf"], g["read_pdf_coords"])
    try:
        # Green Oaks: the table parse holds 3 rows of one bidder (= a printed
        # SUBTOTAL) and 15 of the other; the coordinate parse 17 of each
        g["read_pdf"] = lambda path, st: (
            fake2([("Dredgit Corporation", 3, 68333.33), ("Paragon Integrated LLC", 15, 1000.0)]),
            [], ["SUBTOTAL MOBILIZATION, BONDS & SWPPP : $ 204,999.99 $ 205,000.00",
                 "TOTAL BASE BID AMOUNT $ 2,598,011.95 $ 4,384,356.00"])
        g["read_pdf_coords"] = lambda path, st: fake2(
            [("Dredgit Corporation", 17, 1000.0), ("Paragon Integrated LLC", 17, 1000.0)])
        st = {}
        read_pdf_best("selftest.pdf", st)
        picked7 = st["pdf_strategy"]["selftest.pdf"]["chosen"]
    finally:
        g["read_pdf"], g["read_pdf_coords"] = saved
    check("a section subtotal does not beat a fuller parse", picked7, "coords")
    #  c. a list of names reaches the next page, no further
    check("lend_names, a list on the next page",
          lend_names(["Acme Paving LLC", "Bravo Construction"], 133, 132),
          ["Acme Paving LLC", "Bravo Construction"])
    #  d. a caption broken by a stray space is still a caption
    check("QUANTIT Y is no name", [looks_like_bidder("QUANTIT Y"), _caption_only("QUANTIT Y"),
                                   looks_like_bidder("A T Construction, LLC"),
                                   _caption_only("C Green Scaping LP")],
          [False, True, True, False])
    check("UNI T is the unit column (the quantity caption is not squeezed)",
          [detect_meta_cols(["ITEM NO.", "DESCRIPTION OF ITEMS", "UNI T", "QTY"], 4),
           detect_meta_cols(["ITEM NO.", "DESCRIPTION OF ITEMS", "UNIT", "QUANTIT Y"], 4)],
          [{"item": 0, "desc": 1, "unit": 2, "qty": 3}, {"item": 0, "desc": 1, "unit": 2, "qty": None}])
    pond = [[E, E, E, E, "McClendon ConstructionCo., Inc.", E, "Urban Infraconstruction LLC", E],
            ["ITEM NO.", "DESCRIPTION OF ITEMS", "UNIT", "QUANTIT Y", E, E, E, E],
            ["PAVING IMPROVEMENTS", E, E, E, "UNIT COST", "TOTAL", "UNIT COST", "TOTAL"],
            ["A1", "Right of Way Preparation", "LS", "1", "$52,000.00", "$52,000.00",
             "$90,000.00", "$90,000.00"],
            ["A2", "Unclassified Street Excavation", "CY", "870", "$40.00", "$34,800.00",
             "$50.00", "$43,500.00"]]
    recs, a = rows_from_grid(pond, "selftest", "mansfield-22-5047", {})
    check("Pond Street: the firm, not 'QUANTIT Y'",
          (a["bidders"], len(recs)),
          (["McClendon ConstructionCo., Inc.", "Urban Infraconstruction LLC"], 4))
    #  e. an empty spacer row does not pin the inferred header under a title
    tank = [["NEEL-SCHAFFER - BID TABULATION", E, E, E, E, E, "Low Bidder", E],
            ["OWNER: CITY OF ROWLETT", E, E, E,
             "I&S Tank Services LLC\n8041 Telegraph Rd\nQuinlan, TX 75474", E,
             "O&J Coatings, Inc.\n1720 Cynthia Ln\nHurst, TX 76054", E],
            ["ITEM", E, E, "PLAN", "UNIT", E, "UNIT", E],
            ["NO.", "ITEM DESCRIPTION", "UNIT", "QTY.", "PRICE", "COST", "PRICE", "COST"],
            [E, E, E, E, E, E, E, E],
            ["1", "Mobilization / Demobilization", "LS", "1", "$57,180.00", "$57,180.00",
             "$15,000.00", "$15,000.00"],
            ["2", "Paint interior coating system", "LS", "1", "$230,150.00", "$230,150.00",
             "$330,000.00", "$330,000.00"],
            ["3", "Paint exterior coating system", "LS", "1", "$225,170.00", "$225,170.00",
             "$300,000.00", "$300,000.00"]]
    recs, a = rows_from_grid(tank, "selftest", "rowlett-2024-02", {})
    check("names under a title row and over a blank row are read",
          [b for b in (a or {}).get("bidders", [])],
          ["I&S Tank Services LLC", "O&J Coatings, Inc."])
    #  f. a leading item number is split out where the page proves the column
    def crec(d, b="Meca Construction LLC"):
        return {"src_sheet": "coords_p1", "item_no": "", "description": d, "bidder_raw": b}
    pg = [crec(d, b) for b in ("Meca Construction LLC", "Ken-Do Contracting LP") for d in (
        "101 Mobilization & Bonds", "102 Construction Contingency for Public Works Items",
        "201 8-inch High Early Strength Reinforced Concrete Paving",
        "202 Remove & Replace Reinforced Concrete Pavement")]
    split_glued_item_numbers(pg)
    check("item numbers split from coordinate descriptions",
          [(r["item_no"], r["description"]) for r in pg[:3]],
          [("101", "Mobilization & Bonds"),
           ("102", "Construction Contingency for Public Works Items"),
           ("201", "8-inch High Early Strength Reinforced Concrete Paving")])
    sizes = [crec(d) for d in ("8 in. PVC Water Pipe", "12 in. PVC Water Pipe",
                               "15 in. RCP Storm Pipe", "2 Year Maintenance Bond")]
    few = [crec(d) for d in ("101 Mobilization", "102 Traffic Control")]
    split_glued_item_numbers(sizes)
    split_glued_item_numbers(few)
    check("sizes, counts and short pages are left alone",
          [r["item_no"] for r in sizes + few], [""] * 6)
    #  g. dedup: the leftover pages of a file already proven to repeat another
    rowsA1 = [{"bidder": "B&B Concrete", "unit": "LS", "quantity": 1, "unit_price": float(p)}
              for p in range(100, 110)]
    rowsA2 = [{"bidder": "B&B Concrete", "unit": "EA", "quantity": 25, "unit_price": float(p)}
              for p in (150, 151, 152, 153)]
    data = {("a.pdf", "p1"): rowsA1, ("a.pdf", "p2"): rowsA2,
            ("b.pdf", "p1"): [dict(x) for x in rowsA1], ("b.pdf", "p2"): [dict(x) for x in rowsA2]}
    sg = {k: set(dedup.sig_of(x) for x in v) for k, v in data.items()}
    dr, _ov = dedup.find_duplicate_sheets(sg, {k: set(s[1:] for s in v) for k, v in sg.items()},
                                          lambda k: data[k], lambda k: "mesquite")
    check("a short page of a proven copy goes too", sorted(dr), [("b.pdf", "p1"), ("b.pdf", "p2")])
    # two files that each won one page keep both survivors
    rowsB1 = [{"bidder": "Ed Bell", "unit": "SY", "quantity": 40, "unit_price": float(p)}
              for p in range(200, 210)]
    data2 = {("a.pdf", "p1"): rowsA1, ("a.pdf", "p2"): rowsB1,
             ("b.pdf", "p1"): [dict(x) for x in rowsA1],
             ("b.pdf", "p2"): [dict(x) for x in rowsB1] + [
                 {"bidder": "Ed Bell", "unit": "SY", "quantity": 40, "unit_price": 999.0}]}
    sg2 = {k: set(dedup.sig_of(x) for x in v) for k, v in data2.items()}
    dr2, _ov2 = dedup.find_duplicate_sheets(sg2, {k: set(s[1:] for s in v) for k, v in sg2.items()},
                                            lambda k: data2[k], lambda k: "mesquite")
    check("mixed winners keep one copy of each page", len(dr2), 2)
    with tempfile.TemporaryDirectory() as td:
        pth = {}
        for nm, body in (("x.pdf", b"%PDF-1.4 same"), ("y.pdf", b"%PDF-1.4 same"),
                         ("z.pdf", b"%PDF-1.4 other")):
            pth[nm] = os.path.join(td, nm)
            with open(pth[nm], "wb") as fh:
                fh.write(body)
        check("identical_file_groups", identical_file_groups(sorted(pth), pth), [["x.pdf", "y.pdf"]])
    #  h. the site-level reader runs on its exact file only
    check("site_level_inputs", site_level_inputs([
        "Example Creek Bid Tabulation.pdf",
        "external/cedar_hill/2025-04-25_PW22-0011_Bear_Creek_Rd_and_S_Example_Rd_"
        "Intersection_Improvements_Bid_Tabulation.pdf",
        "external/example/Example Creek Bid Tabulation.pdf"],
        frozenset({"Example Creek Bid Tabulation.pdf"})),
        ["Example Creek Bid Tabulation.pdf"])
    #  i. a file whose bidder names are mostly noise
    check("garbled_name", [garbled_name(s) for s in (
        "AXIS (Qml\\ACTJNG l~C", "DOM Ccmn'RUCT10fll COftPOAATIO!ol",
        "MiUltOSW.tU;'(ltA r..SONS DC", "McMahon Contracting, L.P.",
        "Orteg;; Land Clearing, LLC", "3D CONTRACTORS, LLC", "R e y e s G r o u p, Ltd.")],
        [True, True, True, False, False, False, False])
    gr = [{"src_file": f, "bidder_raw": b, "is_engineer_estimate": False}
          for f, b in (("bad.pdf", "AXIS (Qml\\ACTJNG l~C"),
                       ("bad.pdf", "DOM Ccmn'RUCT10fll COftPOAATIO!ol"),
                       ("bad.pdf", "MiUltOSW.tU;'(ltA r..SONS DC"),
                       ("bad.pdf", "TIH'O ,A'UING\" CO"), ("bad.pdf", "AVERAGES"),
                       ("ok.pdf", "Au,nm C4notructTon, LLC"), ("ok.pdf", "Jeske Construction"),
                       ("ok.pdf", "Tiseo Paving Company"))]
    check("garbled_files", garbled_files(gr), {"bad.pdf": (3, 4)})
    #  j. a lump sum is one unit: LS with a quantity above one that extends per
    #     unit loses the LS (Mansfield 22-4735 pipe bursting, 1,596 'LS') -- and
    #     only the LS: it is not unit_suspect, so its money stays in the totals
    lsr = [{"unit": "LS", "quantity": 1596.0, "ext_mismatch": ""},
           {"unit": "LS", "quantity": 1.0, "ext_mismatch": ""},
           {"unit": "LS", "quantity": 12.0, "ext_mismatch": "Y"},
           {"unit": "LF", "quantity": 1596.0, "ext_mismatch": ""}]
    check("clear_lump_sum_with_quantity",
          ([clear_lump_sum_with_quantity(r) for r in lsr],
           [(r["unit"], r.get("unit_suspect"), r.get("unit_note")) for r in lsr]),
          ([True, False, False, False],
           [("", None, "lump_sum_with_quantity"), ("LS", None, None), ("LS", None, None),
            ("LF", None, None)]))
    #  k. the words of several items stacked in the first item's cell
    stacked = [["23", "Transfer existing water meter to relocated box\nTie into existing water line\n"
                      "Furnish and install 20\" x 8\" tapping sleeve and tapping gate valve",
                "274", "EA", "$100", "$ 27,400.00"],
               ["24", None, "30", "EA", "$3,650", "$ 109,500.00"],
               ["25", "", "1", "EA", "$19,250", "$ 19,250.00"],
               ["26", "Cut and plug existing water main", "16", "EA", "$750", "$ 12,000.00"]]
    sp = spread_stacked_cells(stacked)
    check("stacked descriptions spread one per row", [r[1] for r in sp],
          ["Transfer existing water meter to relocated box", "Tie into existing water line",
           "Furnish and install 20\" x 8\" tapping sleeve and tapping gate valve",
           "Cut and plug existing water main"])
    items12 = [["1 Mobilization\n2 Furnish and install 20-inch PVC water pipe AWWA C905\n"
                "(DR-18) by open cut, including testing", "", "1", "LS", "$104,441", "$ 104,441.00"],
               ["", "", "10", "LF", "$384", "$ 3,840.00"]]
    check("stacked item cells spread by their numbers", [r[0] for r in spread_stacked_cells(items12)],
          ["1 Mobilization", "2 Furnish and install 20-inch PVC water pipe AWWA C905 (DR-18) by "
                             "open cut, including testing"])
    wrapped = [["5", "Furnish and install 8-inch PVC water pipe by open cut,\nincluding embedment",
                "866", "LF", "$67", "$ 58,022.00"],
               ["6", "", "11", "LF", "$73", "$ 803.00"]]
    check("a wrapped description is one entry, so nothing moves",
          [r[1] for r in spread_stacked_cells(wrapped)],
          ["Furnish and install 8-inch PVC water pipe by open cut,\nincluding embedment", ""])
    # the row below is the TOTAL line (money, no unit): it never takes the
    # last line of a wrapped description (Collin 59077 'Stand' / 'MFS22, ...')
    over_total = [["7", "Dell Pro Micro All-In-One Stand\nMFS22, 452-BDUB", "1", "EA", "$600", "$600.00"],
                  ["", None, "", "", "", "$135,470.00"],
                  ["", "", "", "", "$1,200", "$135,470.00"]]
    check("a wrapped description above a total line stays whole",
          [r[1] for r in spread_stacked_cells(over_total)],
          ["Dell Pro Micro All-In-One Stand\nMFS22, 452-BDUB", None, ""])
    caps = [["409", "12\" STONE ROCK RIPRAP, 24\" TOTAL LAYER THICKNESS\nCONCRETE TOE WALLS",
             "500", "SY", "$95", "$47,500.00"],
            ["410", "", "20", "SY", "$40", "$800.00"]]
    check("a description in capitals only is one entry",
          [r[1] for r in spread_stacked_cells(caps)],
          ["12\" STONE ROCK RIPRAP, 24\" TOTAL LAYER THICKNESS\nCONCRETE TOE WALLS", ""])
    hdr_b =[["ITEM", "DESCRIPTION", "BID QTY", "UNIT", "UNIT PRICE", "TOTAL AMOUNT"]]
    recs, a = rows_from_grid([hdr_b[0]] + [[txt(c) for c in r] for r in spread_stacked_cells(items12)],
                             "selftest", "bedford", {}, fallback_names=["SYB Construction Co., INC"])
    check("'1 Mobilization' in the item cell is item 1, 'Mobilization'",
          [(r["item_no"], r["description"][:22], r["quantity"]) for r in recs],
          [("1", "Mobilization", 1.0), ("2", "Furnish and install 20", 10.0)])

    # 29. second round (reports/parser_fix_verification.md section 14)
    #  a. a quantity never read, given back by extension / price when every
    #     priced column of the grid row agrees (Mansfield 22-5047 page 1)
    def qrow(item, cells, q=None, sh="p1t0", unit="SY"):
        return [{"src_file": "f.pdf", "src_sheet": sh, "bid_section": "BASE BID",
                 "item_no": item, "description": "item " + item, "unit_raw": unit,
                 "quantity": q, "bidder_raw": b, "unit_price": u, "extended_reported": e}
                for b, u, e in cells]
    qr = (qrow("1", [("Acme", 7528.07, 22584.21), ("Bravo", 7211.0, 21633.0),
                     ("Engineer's Estimate", 7000.0, 21000.0)])          # 3, prices rounded
          + qrow("2", [("Acme", 0.19, 29108.0), ("Bravo", 0.28, 42896.0)])   # 153,200
          + qrow("3", [("Acme", 10.0, 210.0), ("Bravo", 10.0, 250.0)])       # 21 vs 25
          + qrow("4", [("Acme", 50.0, 1000.0)])                              # one column
          + qrow("5", [("Acme", 50.0, 1000.0), ("Bravo", 40.0, None)])       # one unextended
          + qrow("6", [("Acme", 50.0, 1000.0), ("Bravo", 40.0, 800.0)], q=20.0)
          + qrow("7", [("Acme", 204955.9, 204955.9), ("Bravo", 250000.0, 250000.0)],
                 unit="LS"))
    check("recover_quantity_from_extensions",
          (recover_quantity_from_extensions(qr),
           [(r["item_no"], r["quantity"], r.get("qty_source")) for r in qr[::2]]),
          ((3, 7), [("1", 3.0, "implied_by_columns"), ("1", 3.0, "implied_by_columns"),
                    ("2", 153200.0, "implied_by_columns"), ("3", None, None),
                    ("5", None, None), ("6", 20.0, None), ("7", 1.0, "implied_by_columns")]))
    #  b. a P.E. seal marks the engineer, but not in a cell labelled a bidder's
    check("P.E. in a bidder's caption is not an estimate",
          [bool(ENGINEER_RE.search(s)) for s in (
              "n, P.E. 4/1/2025 BIDDER 1", "Jane Doe, P.E.",
              "Freese and Nichols, Inc. Jane Doe, P.E. (555)555-0100",
              "C.P.E. Construction LLC", "Contractor: Smith Paving (P.E. on staff)")],
          [False, True, True, False, False])
    #  c. 'BID OF' is a caption, not part of the firm; 'LOW BID' stays (rank_of)
    check("canon_bidder drops 'BID OF'",
          [canon_bidder(s) for s in ("BID OF Atkins Bros. Equip. Co., Inc.",
                                     "Bid of: Kitching & Co., LLC", "LOW BID Atkins Bros.",
                                     "Bidwell Construction", "BID OF")],
          ["Atkins Bros. Equip. Co., Inc.", "Kitching & Co., LLC", "LOW BID Atkins Bros.",
           "Bidwell Construction", "BID OF"])
    #  d. a section's own total is not a grand total (rule 15 and the gate)
    check("section_total_line",
          [section_total_line(s) for s in (
              "SECTION A - PAVING - TOTAL $ 1,234.00", "TOTAL SECTION II $ 5.00",
              "Total for Section C: $ 9.00", "Total Group 5 $ 3.00",
              "TOTAL SECTION I-II $ 6.00", "Total Bid Amount BASE BID (SECTION 1) $ 7.00",
              "TOTAL AMOUNT OF BASE BID + SECTION I $ 8.00", "GRAND TOTAL $ 9.00",
              "Total Sections 1 and 2 $ 10.00")],
          [True, True, True, True, False, False, False, False, False])
    check("total_line_amounts skips section totals",
          sorted(total_line_amounts(["SECTION A - PAVING - TOTAL $ 1,234.00",
                                     "TOTAL BASE BID $ 4,346,532.63", "SUBTOTAL $ 99.00"])),
          [434653263])
    #  e. what the document holds: distinct item lines, once per (number, word)
    dl = ["BID TABULATION 2026", "101 Mobilization & Bonds 1 LS $ 21,000.00 $ 21,000.00",
          "102 Storm Water Pollution Prevention Plan 1 LS $ 7,000.00 $ 7,000.00",
          "101 Mobilization & Bonds 1 LS $ 20,000.00 $ 20,000.00",    # next bidder's page
          "1 Clearing 1 LS 500.00 500.00", "1 Excavation 10 CY 5.00 50.00",   # section B restarts
          "2. Approve the contract in the amount of $1,234.56",       # one number: not an item
          "TOTAL BASE BID $ 28,000.00 $ 27,000.00"]
    check("document_facts", document_facts(dl),
          {"items": 4, "totals": [123456, 2700000, 2800000],   # the approval line's amount counts
           "total_lines": [2700000, 2800000], "contract_hint": ""})
    check("document_facts: amounts off the item lines, bare workbook numbers",
          document_facts(["Klutz Construction, LLC 672,774.00",
                          "1.01 Mobilization, Insurance, and Bonding 1 LS $14,256.00 $14,256.00",
                          "Schedule 1 - General Conditions $14,256.00 $20,000.00",
                          "SUBTOTAL PAVING $ 5,000.00",
                          "Total Amount Base Bid | 4218650.75 | 2954810 | 2026"])["totals"],
          [202600, 67277400, 295481000, 421865075])           # a bare '2026' in a row counts too
    #  g. a lump sum printed as its amount only (Addison Surveyor), and an item
    #     whose words say it is an alternate
    gl = [["ITEM", "DESCRIPTION", "QUANTITY", "UNIT", "UNIT PRICE", "BID AMOUNT",
           "UNIT PRICE", "BID AMOUNT"],
          ["1", "Paving repair", "250", "SY", "63", "$ 15,750.00", "$ 100.00", "$ 25,000.00"],
          ["2", "Sitework and dosing building", "1", "LS", "", "$ 645,000.00", "",
           "$ 661,000.00"],
          ["5", "Mobilization", "1", "LS", "", "$ 42,000.00", "$ 2,800.00", "$ 2,800.00"],
          ["A", "Additive Alternative: Porous pavers", "1", "LS", "", "$ 33,000.00", "",
           "$ 24,600.00"],
          ["", "BASE BID", "", "", "", "", "", ""],
          ["6", "Trench safety", "40", "LF", "", "", "", ""]]
    recs, a = rows_from_grid(gl, "selftest", "addison", {},
                             fallback_names=["Austin Bridge", "Dake Construction"])
    check("a lump sum's amount is its price; an 'Additive Alternative' row is an alternate",
          [(r["item_no"], r["bidder_raw"], r["unit_price"], r["bid_section"]) for r in recs],
          [("1", "Austin Bridge", 63.0, "BASE BID"), ("1", "Dake Construction", 100.0, "BASE BID"),
           ("2", "Austin Bridge", 645000.0, "BASE BID"),
           ("2", "Dake Construction", 661000.0, "BASE BID"),
           # round 3: a column that prints a lump sum's amount only bid that
           # amount even where another column prints its unit price too
           # (Farmers Branch Reach 3's $80,000 allowance; lump_sum_amount_prices)
           ("5", "Austin Bridge", 42000.0, "BASE BID"),
           ("5", "Dake Construction", 2800.0, "BASE BID"),
           ("A", "Austin Bridge", 33000.0, "ALT A"), ("A", "Dake Construction", 24600.0, "ALT A")])
    #  h. a reading whose checkable rows nearly all fail arithmetic
    fa = ([{"src_file": "bad.pdf", "extended_calc": 1.0, "extended_reported": 9.0,
            "ext_mismatch": "Y"}] * 10
          + [{"src_file": "ok.pdf", "extended_calc": 1.0, "extended_reported": 9.0,
              "ext_mismatch": "Y"}] * 8
          + [{"src_file": "ok.pdf", "extended_calc": 9.0, "extended_reported": 9.0,
              "ext_mismatch": ""}] * 4
          + [{"src_file": "few.pdf", "extended_calc": 1.0, "extended_reported": 9.0,
              "ext_mismatch": "Y"}] * 5)
    check("failed_arithmetic_files", failed_arithmetic_files(fa), {"bad.pdf"})
    #  i. a bid allowance's unit
    check("norm_unit reads 'B.A.' as an allowance", [norm_unit(x) for x in ("B.A.", "b.a", "BA", "Bid Allowance")],
          ["ALLOW", "ALLOW", "BA", "ALLOW"])            # a bare 'BA' is left as printed
    #  f. the locked public test set: held-out public files are 'public_test',
    #     other public files 'external', top-level company files 'internal'
    import public_holdout
    held = sorted(public_holdout.holdout_files())
    check("input_source_set",
          [input_source_set(x) for x in (held[:1] or ["external/x/not_listed.pdf"])
           + ["external/arlington/not_in_the_test_set.pdf",
              "Example Creek Bid Tabulation.pdf"]],
          (["public_test"] if held else ["external"]) + ["external", "internal"])
    if not held:
        print("  note: data/test_holdout/public_holdout.csv lists no file; the held-out "
              "case was not exercised")
    _split_layout_selftest(check, totals)

    for f in fails:
        print("FAIL parse rule: " + f)
    print("parse rules: %d failure(s)" % len(fails))
    return not fails


def _split_layout_selftest(check, totals):
    """Parser round 3 (helper B): tabs whose table is split, stacked or
    captioned unusually -- Seagoville PW2501, Keller 25-346, Dallas County
    2026-006-7091, North Richland Hills RFB 24-033, Bedford Parkwood Drive --
    each on a small tab shaped like the original, with its look-alikes."""
    E = ""
    #  a. a caption row repeated under NEW names starts a new bidder block
    #     (Keller 25-346); under the same names it changes nothing
    hdr = ["ITEM NO.", "DESCRIPTION OF ITEMS", "QUANTITY", "UNIT", "UNIT COST", "TOTAL",
           "UNIT COST", "TOTAL"]
    blk1 = [[E, E, E, E, "AMS Land Company LLC", E, "Black Fox Services LLC", E], hdr,
            ["1", "Mobilization", "1", "LS", "$68,820.00", "$68,820.00", "$56,690.84",
             "$56,690.84"],
            ["2", "Channel Excavation", "641", "CY", "$35.00", "$22,435.00", "$12.60",
             "$8,076.60"],
            [E, "PROTECTION TOTAL", E, E, E, "$91,255.00", E, "$64,767.44"]]
    blk2 = [[E, E, E, E, "BIDDERS", E, E, E],
            [E, E, E, E, "Cole Construction Inc.", E, "FNH Construction LLC", E], hdr,
            ["1", "Mobilization", "1", "LS", "$35,080.16", "$35,080.16", "$80,000.00",
             "$80,000.00"],
            ["2", "Channel Excavation", "641", "CY", "$59.88", "$38,383.08", "$110.00",
             "$70,510.00"]]
    recs, a = rows_from_grid(blk1 + blk2, "selftest", "keller", {})
    check("stacked bidder blocks", totals(recs), sorted({
        ("AMS Land Company LLC", "BASE BID"): 91255.0,
        ("Black Fox Services LLC", "BASE BID"): 64767.44,
        ("Cole Construction Inc.", "BASE BID"): 73463.24,
        ("FNH Construction LLC", "BASE BID"): 150510.0}.items()))
    check("stacked blocks lend the last block's names", a["bidders"],
          ["Cole Construction Inc.", "FNH Construction LLC"])
    same = blk1[:4] + [hdr] + blk1[3:4]
    recs, _a = rows_from_grid(same, "selftest", "repeat", {})
    check("a caption row repeated under no names", sorted({r["bidder_raw"] for r in recs}),
          ["AMS Land Company LLC", "Black Fox Services LLC"])
    stale = blk1[:4] + [[E, E, E, E, "Black Fox Services LLC", E, "AMS Land Company LLC", E],
                        hdr] + blk1[3:4]          # the same firms again (Cedar Hill US 67)
    recs, _a = rows_from_grid(stale, "selftest", "stale", {})
    check("a caption row repeated under the same firms", totals(recs), sorted({
        ("AMS Land Company LLC", "BASE BID"): 113690.0,
        ("Black Fox Services LLC", "BASE BID"): 72844.04}.items()))
    cap = blk1[:4] + [[E, E, E, E, "Numeric", E, "Numeric Value", E], hdr] + blk1[3:4]
    recs, _a = rows_from_grid(cap, "selftest", "caption", {})
    check("a caption row repeated under captions, not firms",
          sorted({r["bidder_raw"] for r in recs}),
          ["AMS Land Company LLC", "Black Fox Services LLC"])

    #  b. no DESCRIPTION caption: the column of words is the description (NRH
    #     RFB 24-033 'Spec. Item'); the wordier column wins ('Spec Item'
    #     codes beside 'Name of Pay Item'); a named one is never moved
    g = [[E, E, E, E, "GRod Construction LLC", E, E, "Klutz Construction", E, E],
         ["Item No.", "Spec. Item", "Est. Quantity", "Unit", "Unit Bid Price", E, "Amount Bid",
          "Unit Bid Price", E, "Amount Bid"],
         ["101", "GENERAL SITE PREPARATION", "1", "LS", "$ 60,000.00", E, "$ 60,000.00",
          "$ 9,925.00", E, "$ 9,925.00"],
         ["102", "PROJECT SIGN", "1", "EA", "$ 675.00", E, "$ 675.00", "$ 550.00", E,
          "$ 550.00"],
         ["104", "TREE PROTECTION FENCING", "3", "EA", "$ 600.00", E, "$ 1,800.00",
          "$ 720.00", E, "$ 2,160.00"]]
    recs, _a = rows_from_grid(g, "selftest", "nrh", {})
    check("description column by content", sorted({(r["item_no"], r["description"])
                                                   for r in recs}),
          [("101", "GENERAL SITE PREPARATION"), ("102", "PROJECT SIGN"),
           ("104", "TREE PROTECTION FENCING")])
    g2 = [[E, E, E, E, E, "C. GREEN SCAPING, LP", E, "PATCON SERVICES, LLC", E],
          ["Item No.", "Spec Item", "Name of Pay Item", "Est Quantity", "Unit",
           "Unit Bid Price", "Amount Bid", "Unit Bid Price", "Amount Bid"],
          ["1", "NCTCOG 203.1", "GENERAL SITE PREPERATION", "1", "LS", "$ 2,850.00",
           "$ 2,850.00", "$ 11,538.39", "$ 11,538.39"],
          ["2", "TXDOT 500", "MOBILIZATION", "1", "LS", "$ 7,000.00", "$ 7,000.00",
           "$ 18,988.32", "$ 18,988.32"],
          ["3", "CITY DETAIL", "PROJECT SIGN", "2", "EA", "$ 800.00", "$ 1,600.00",
           "$ 1,691.53", "$ 3,383.06"]]
    recs, _a = rows_from_grid(g2, "selftest", "nrh2", {})
    check("the wordier column is the description", sorted({r["description"] for r in recs}),
          ["GENERAL SITE PREPERATION", "MOBILIZATION", "PROJECT SIGN"])
    g3 = [list(g2[0]), ["Item No.", "Spec Item", "Description", "Est Quantity", "Unit",
                        "Unit Bid Price", "Amount Bid", "Unit Bid Price", "Amount Bid"]] + \
        [[r[0], r[2], r[1]] + r[3:] for r in g2[2:]]
    recs, _a = rows_from_grid(g3, "selftest", "named", {})
    check("a captioned description column is kept", sorted({r["description"] for r in recs}),
          ["CITY DETAIL", "NCTCOG 203.1", "TXDOT 500"])

    #  c. two quantity columns: the one the extensions are computed on
    #     (Seagoville PW2501 'PLAN QTY. | BID QTY.'); a tie keeps the first
    g = [[E, E, E, E, E, "Atkins Bros. Equipment Co., Inc.", E, "RT&E Rural Water, LLC", E],
         ["ITEM NO.", "DESCRIPTION", "UNIT", "PLAN QTY.", "BID QTY.", "UNIT PRICE",
          "BID ESTIMATE", "UNIT PRICE", "BID ESTIMATE"],
         ["1", "Mobilization", "LS", "1", "1", "$ 12,000.00", "$ 12,000.00", "$ 6,500.00",
          "$ 6,500.00"],
         ["9", "Trench Safety System", "LF", "242", "250", "$ 1.00", "$ 2 50.00", "$ 6.40",
          "$ 1,600.00"],
         ["17", "Sodding to Match Existing", "SY", "552", "560", "$ 30.00", "$ 16,800.00",
          "$ 3.95", "$ 2,212.00"]]
    recs, _a = rows_from_grid(g, "selftest", "seagoville", {})
    check("quantity column by extensions", sorted({(r["item_no"], r["quantity"]) for r in recs}),
          [("1", 1.0), ("17", 560.0), ("9", 250.0)])
    g[3][4], g[4][4] = "242", "552"           # every printed extension on PLAN QTY.
    g[3][6], g[3][8], g[4][6], g[4][8] = "$ 242.00", "$ 1,548.80", "$ 16,560.00", "$ 2,180.40"
    recs, _a = rows_from_grid(g, "selftest", "plan", {})
    check("the first quantity caption keeps a tie", sorted({r["quantity"] for r in recs}),
          [1.0, 242.0, 552.0])
    check("a merged caption cell naming the item is no quantity column",
          [detect_meta_cols(["Line # Description Mfg QTY", "", "", "", "Unit"], 5)["qty"],
           detect_meta_cols(["Line #", "Description", "QTY", "Unit"], 4)["qty"]],
          [None, 2])
    check("'BID ESTIMATE' closes a price pair only after a price caption",
          [_is_price_pair("UNIT PRICE", "BID ESTIMATE"), _is_price_pair("UNIT", "BID ESTIMATE")],
          [True, False])

    #  d. a tab drawn as separate boxes (Seagoville PW2501): the item box and
    #     one box per bidder, rows lined up, are read as one table
    class _R(object):
        def __init__(self, top, bot):
            self.bbox = (0, top, 0, bot)

    class _T(object):
        def __init__(self, x0, x1, bands):
            self.rows = [_R(t, b) for t, b in bands]
            self.bbox = (x0, bands[0][0], x1, bands[-1][1])

    class _P(object):
        def __init__(self, objs):
            self.objs = objs

        def find_tables(self):
            return self.objs
    bands = [(150, 165), (165, 183), (183, 199)]
    items = [["ITEM NO.", "DESCRIPTION", "UNIT", "PLAN QTY.", "BID QTY."],
             ["1", "Mobilization and Demobilization", "LS", "1", "1"],
             ["9", "Trench Safety System", "LF", "242", "250"]]
    box1 = [["Atkins Bros. Equipment Co., Inc.", None], ["UNIT PRICE", "BID ESTIMATE"],
            ["$ 12,000.00", "$ 12,000.00"], ["$ 1.00", "$ 2 50.00"]]
    box2 = [["GRod Construction, LLC", None], ["UNIT PRICE", "BID ESTIMATE"],
            ["$ 25,000.00", "$ 25,000.00"], ["$ 16.00", "$ 4,000.00"]]
    page = _P([_T(327, 420, [(132, 150)] + bands), _T(424, 517, [(132, 150)] + bands),
               _T(72, 321, bands)])
    st = stitch_side_tables(page, [box1, box2, items])
    check("split boxes stitched", st and [[txt(c) for c in r] for r in st[0][0]],
          [[E, E, E, E, E, "Atkins Bros. Equipment Co., Inc.", E, "GRod Construction, LLC", E],
           ["ITEM NO.", "DESCRIPTION", "UNIT", "PLAN QTY.", "BID QTY.", "UNIT PRICE",
            "BID ESTIMATE", "UNIT PRICE", "BID ESTIMATE"],
           ["1", "Mobilization and Demobilization", "LS", "1", "1", "$ 12,000.00",
            "$ 12,000.00", "$ 25,000.00", "$ 25,000.00"],
           ["9", "Trench Safety System", "LF", "242", "250", "$ 1.00", "$ 2 50.00", "$ 16.00",
            "$ 4,000.00"]])
    recs, _a = rows_from_grid([[txt(c) for c in r] for r in st[0][0]], "selftest", "boxes", {})
    check("stitched boxes read", totals(recs), sorted({
        ("Atkins Bros. Equipment Co., Inc.", "BASE BID"): 12250.0,
        ("GRod Construction, LLC", "BASE BID"): 29000.0}.items()))
    page = _P([_T(300, 420, [(132, 150)] + bands), _T(424, 517, [(132, 150)] + bands),
               _T(72, 321, bands)])                          # a box overlapping the items
    check("overlapping boxes are not stitched", stitch_side_tables(page, [box1, box2, items]),
          None)
    page = _P([_T(327, 420, [(132, 150), (150, 158), (158, 165), (165, 183)]),
               _T(424, 517, [(132, 150)] + bands), _T(72, 321, bands)])
    check("two rows of one box on one item row are not stitched",
          stitch_side_tables(page, [box1, box2, items]), None)

    #  e. an ESTIMATE caption over a pair whose own name cell is empty
    #     (Dallas County 2026-006-7091); a pair named in its own column keeps it
    g = [[E, E, E, E, "Vendor No. 1", E, "Vendor No. 2", E, "Vendor No. 8", "Vendor No. 9",
          "ENGINEER's ESTIMATE", E],
         [E, E, E, E, "Macias Specialty Contracting 2418 Cales Dr Arlington, TX 76013", E,
          "C Green Scaping LP 2401 Handley Edervilled Ft Worth, TX 76118", E,
          "GeoTex Engineering, LLC 14805 Trinity Blvd Fort Worth, Texas, 76155",
          "Dredge Heavy Construction 5676 N Bayou Black Drive Gibson, Louisiana, 70356", E, E],
         ["Bid Bond Received", E, E, E, "Yes", E, "Yes", E, "Non-Responsive", E, E, E]] + \
        [[E] * 12 for _ in range(6)] + \
        [["ITEM", "DESCRIPTION", "UNIT", "QUANTITY"] + ["UNIT PRICE", "EXTENSION"] * 4,
         ["100-7002", "PREPARING ROW", "STA", "46.50", "$132.50", "$6,161.25", "$624.00",
          "$29,016.00", "Non-Responsive", "Non-Responsive", "$11,527.68", "$536,037.12"]]
    recs, _a = rows_from_grid(g, "selftest", "dallas", {})
    check("estimate caption above an unnamed pair",
          sorted((r["bidder_raw"], r["is_engineer_estimate"]) for r in recs),
          [("C Green Scaping LP", False), ("ENGINEER's ESTIMATE", True),
           ("Macias Specialty Contracting", False)])
    g[1][10] = "Kimley-Horn and Associates"
    recs, _a = rows_from_grid(g, "selftest", "dallas2", {})
    check("a pair named in its own column keeps its name",
          sorted(r["bidder_raw"] for r in recs),
          ["C Green Scaping LP", "Kimley-Horn and Associates", "Macias Specialty Contracting"])

    #  f. a firm ending in ', Co.' is no Colorado city line, and a street
    #     address with no comma before its state is cut off
    for raw, want in (("Bernal Commercial Construction, Co.", "Bernal Commercial Construction, Co."),
                      ("Denver, CO 80202", ""),
                      ("CGC General Contractors, 3212 Friendly Lane Haltom City TX 76117 "
                       "PH: 682-730-6933", "CGC General Contractors"),
                      ("Excel 4 Construction, LLC 5733 Hart Street Fort Worth, Texas",
                       "Excel 4 Construction, LLC"),
                      ("Palmer Hall Construction Dallas, TX, 75204", "Palmer Hall Construction"),
                      ("Fain Group Fort Worth, TX, 76161", "Fain Group"),
                      ("Dallas, TX, 75204", ""),
                      ("CCGMG LLC Series B\nBalch Springs, TX, 75180", "CCGMG LLC Series B"),
                      ("Smith Group of Companies", "Smith Group of Companies")):
        check("strip_address(%r)" % raw, strip_address(raw), want)

    #  h. the coordinate reader: a centred description caption over a
    #     left-aligned column, a description printed around its price line,
    #     a title block over the names (Bedford Parkwood); a single caption
    #     line is no title block
    import parse_coords as pcm

    def W(t, x0, top, h=7.0):
        return {"text": t, "x0": float(x0), "x1": float(x0) + 5.0 * len(t), "top": float(top),
                "bottom": float(top) + h, "upright": True}
    words = [W("BID", 400, 10), W("TABULATION", 420, 10), W("REPORT", 475, 10),
             W("CLIENT:", 390, 22), W("CITY", 430, 22), W("OF", 455, 22), W("BEDFORD", 468, 22),
             W("BID", 40, 40), W("TIME:", 60, 40), W("GROD", 360, 40), W("CONSTRUCTION,", 382, 40),
             W("LLC", 452, 40), W("JR", 490, 40), W("WEST", 503, 40), W("TEXAS", 528, 40),
             W("BIDDERS", 640, 33),
             W("ITEM", 10, 60), W("DESCRIPTION", 100, 60), W("QUANTITY", 220, 60),
             W("UNIT", 270, 60), W("UNIT", 350, 60), W("COST", 375, 60), W("TOTAL", 420, 60),
             W("UNIT", 480, 60), W("COST", 505, 60), W("TOTAL", 550, 60),
             W("100", 12, 80), W("CONSTRUCTION", 40, 80), W("CONTINGENCY", 108, 80),
             W("1", 232, 80), W("LS", 272, 80), W("$100.00", 352, 80), W("$100.00", 422, 80),
             W("$90.00", 482, 80), W("$90.00", 552, 80),
             W("101", 12, 90), W("R.O.W.", 40, 90), W("PREPARATION", 72, 90), W("1", 232, 90),
             W("LS", 272, 90), W("$20.00", 352, 90), W("$20.00", 422, 90), W("$30.00", 482, 90),
             W("$30.00", 552, 90),
             W("8\"", 40, 96), W("4000", 52, 96), W("PSI", 75, 96), W("PAVEMENT", 92, 96),
             W("110", 12, 100), W("2", 232, 100), W("SY", 272, 100), W("$5.00", 352, 100),
             W("$10.00", 422, 100), W("$6.00", 482, 100), W("$12.00", 552, 100),
             W("MONOLITHIC", 40, 104), W("CURB", 95, 104),
             W("109", 12, 110), W("CEMENT", 40, 110), W("SLURRY", 75, 110), W("3", 232, 110),
             W("TON", 270, 110), W("$1.00", 352, 110), W("$3.00", 422, 110), W("$2.00", 482, 110),
             W("$6.00", 552, 110),
             W("PARKWOOD", 5, 120), W("DRIVE", 50, 120), W("SCHEDULE", 80, 120), W("B:", 125, 120),
             W("DRAINAGE", 140, 120), W("IMPROVEMENTS", 190, 120)]
    lines = pcm.group_lines(words)
    hdr = next(h for h in (pcm.find_header(l) for l in lines) if h)
    ranges = pcm.build_ranges(hdr, 700.0)
    pcm.refine_description_start(lines, hdr["top"], ranges)
    check("coords: names under a title block",
          pcm.extract_bidder_names(lines, hdr, ranges),
          ["GROD CONSTRUCTION, LLC", "JR WEST TEXAS"])
    banner = [W("TANK" if k == 9 else "STRETCH", 5 + 38 * k, 50) for k in range(10)]
    check("coords: the short tail of a banner run from the margin is no name",
          pcm.extract_bidder_names(pcm.group_lines(words + banner), hdr, ranges),
          ["GROD CONSTRUCTION, LLC", "JR WEST TEXAS"])
    one = pcm.group_lines(words[3:])                  # 'CLIENT: CITY OF BEDFORD' alone
    check("coords: a single caption line is no title block",
          pcm.extract_bidder_names(one, hdr, ranges),
          ["CLIENT: CITY OF GROD CONSTRUCTION, LLC", "BEDFORD JR WEST TEXAS"])
    # (the merged reader moves the item / description boundary with
    # refine_description_start; descriptions printed around their price line
    # are given to their item by attach_wrapped_lines -- parse_coords'
    # selftest_checks covers both)
    check("coords: item column ends where the words start",
          [pcm.spaced(l, ranges["item"]) for l in lines if 79 < l["top"] < 81]
          + [pcm.spaced(l, ranges["desc"]) for l in lines if 79 < l["top"] < 81],
          ["100", "CONSTRUCTION CONTINGENCY"])



def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    stats = {"cells_blank_price": 0, "sheets_no_header": []}
    all_recs, all_audit, file_dates, summary_totals = [], [], {}, []
    doc_facts = {}            # per file: what the document itself holds (document_facts)

    # Not bid tabulations, so not this script's job -- published average-price
    # reports, item-code dictionaries and bid schedules have their own parsers.
    # Left in, they burn minutes of PDF work and emit meaningless "bids".
    not_a_bidtab = re.compile(
        r"average_bid_price|bid_item_descriptions|bid_schedule|average_cost_manual|"
        r"wage_determination|permtracker|pw_disclosure", re.I)

    # data/            -> our own projects        (source_set "internal")
    # data/external/<owner>/ -> everyone else's    (source_set "external",
    #                           or "public_test" for the locked public test set)
    inputs = []
    for f in sorted(os.listdir(DATA)):
        if f.lower().endswith((".xlsx", ".pdf")) and not f.startswith("~$"):
            inputs.append((f, os.path.join(DATA, f), input_source_set(f), "internal"))
    # Walk the tree, not just one level: some owners nest a second folder
    # (external/legistar/mckinney/...), and a one-level scan silently skipped
    # every file in them.
    # Folders that are deliberately NOT part of the active set:
    #  _pre2020/  quarantined 2019-and-earlier bids -- the user's scope is 2020+,
    #             and pre-COVID prices sit on a different cost regime. Not
    #             deleted; the long history is still used for the time index and
    #             the specialty-item trend, just not for the price level.
    #  dol/       H-1B/PERM wage cases, not construction prices.
    #  txdot_*    handled by their own parsers (load_txdot, parse_txdot_archive).
    EXCLUDE_DIRS = re.compile(
        r"(^|/)(_pre2020|dol|txdot|txdot_archive|other_tx_reference|nrcs_ewp|"
        # Hill County EWP: five 2017 flood-dam abstracts collected after the
        # pre-2020 quarantine ran, so they sit outside _pre2020 but are the same
        # vintage. They currently parse to nothing, which hid the problem --
        # fixing their header vocabulary would have quietly pulled 2017 prices
        # into the 2020+ reference.
        r"hill_county_ewp)(/|$)")

    ext_root = os.path.join(DATA, "external")
    if os.path.isdir(ext_root):
        for dirpath, _dirs, fnames in os.walk(ext_root):
            rel_dir = os.path.relpath(dirpath, DATA).replace("\\", "/")
            if rel_dir == "external":
                continue
            if EXCLUDE_DIRS.search(rel_dir):
                stats.setdefault("skipped_dirs", set()).add(rel_dir)
                continue
            owner = rel_dir.split("/")[1] if "/" in rel_dir else ""
            for f in sorted(fnames):
                if not f.lower().endswith((".xlsx", ".pdf")) or f.startswith("~$"):
                    continue
                rel = "%s/%s" % (rel_dir, f)
                if not_a_bidtab.search(f):
                    stats.setdefault("skipped_not_bidtab", []).append(rel)
                    continue
                inputs.append((rel, os.path.join(dirpath, f), input_source_set(rel), owner))

    files = [i[0] for i in inputs]
    workers = _worker_count()
    print("  (%d input files, %d parallel processes)" % (len(inputs), workers), flush=True)
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        import concurrent.futures.process as _cfp
        # Before building the pool, concurrent.futures probes
        # os.sysconf("SC_SEM_NSEMS_MAX").  Claude Code's Bash sandbox on the Mac
        # denies that call (PermissionError) although the semaphores themselves
        # work (measured 2026-11-19), so the probe alone would stop the parse.
        # Skip the probe only when it is the probe that is refused.
        try:
            os.sysconf("SC_SEM_NSEMS_MAX")
        except PermissionError:
            _cfp._check_system_limits = lambda: None
        except (AttributeError, ValueError, OSError):
            pass
        # Worker recycling can DEADLOCK: on 2026-11-19 06:11 a full re-parse
        # with 2 workers and max_tasks_per_child=20 wrote exactly 40 cache
        # entries (2 x 20), both workers exited to be replaced, no replacement
        # came up, and the parent waited an hour with no error.  So recycling
        # is off unless BIDTAB_TASKS_PER_CHILD asks for it (0 or unset = off).
        tpc = os.environ.get("BIDTAB_TASKS_PER_CHILD", "")
        tpc = int(tpc) if tpc.isdigit() and int(tpc) > 0 else None
        try:
            ex = (ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=tpc)
                  if tpc else ProcessPoolExecutor(max_workers=workers))
        except TypeError:          # max_tasks_per_child needs 3.11+
            ex = ProcessPoolExecutor(max_workers=workers)
        # NOTE: iterate the map generator, do not list() it. map keeps input
        # order so the log stays deterministic, but materialising it first means
        # nothing prints until every file is done -- which makes a healthy long
        # run indistinguishable from a hung one.
        results = ex.map(_parse_one_file, inputs, chunksize=1)
    else:
        ex = None
        results = (_parse_one_file(j) for j in inputs)

    done = 0
    for res in results:
        done += 1
        f = res["f"]
        _merge_stats(stats, res.get("stats"))
        if res.get("error"):
            print("  !! FAILED %s: %s" % (f, res["error"]), file=sys.stderr)
            continue
        recs, audit, st = res["recs"], res["audit"], res["summary"]
        bd, src = res["bd"], res["src"]
        file_dates[f] = {"bid_date": bd, "source": src,
                         "all_dates_seen": res["every"]}
        summary_totals.extend(st)
        all_recs.extend(recs)
        all_audit.extend(audit)
        if res.get("doc") is not None:
            doc_facts[f] = res["doc"]
        print("  %-58s %5d rows  %-8s %s bid_date=%s"
              % (f[-58:], len(recs), res["source_set"],
                 ("tot=%d" % len(st)) if st else "      ", bd or "** MISSING **"))
        if done % 100 == 0:
            print("  ---- %d/%d files, %d rows so far ----"
                  % (done, len(inputs), len(all_recs)), flush=True)

    if ex is not None:
        ex.shutdown(wait=True)

    # ---- site-level only tab (no line items) -----------------------------
    # Exact names only, rows collected per file (site_level_inputs).
    site_rows = []
    for f in site_level_inputs(files):
        got = parse_site_totals_tab(os.path.join(DATA, f))
        site_rows.extend(got)
        print("  %-62s %5d site-total rows (no line items in file)" % (f[:62], len(got)))
    site_rows_n = len(site_rows)

    # ---- item numbers printed into the description (round 5) -------------
    # Before every pass that reads a description (aggregate rows, families,
    # sizes): see strip_leading_item_numbers.
    n_lead = strip_leading_item_numbers(all_recs, stats)
    # ---- a description that is only a code (round 7) ----------------------
    # The words from the item cell or from the same item printed in full
    # elsewhere in the file (recover_code_only_descriptions); what stays a
    # code loses its unit below (unit_note 'desc_code_only').
    n_ci, n_cs = recover_code_only_descriptions(all_recs, stats)
    if n_ci or n_cs:
        print("  code-only descriptions recovered: %d row(s) from the item cell, %d from the "
              "same item elsewhere in the file" % (n_ci, n_cs))
    if n_lead:
        print("  item numbers printed into the description: %d row(s), %d item(s) "
              "moved to item_no" % (n_lead, stats["item_number_in_description"]["items"]))

    # ---- computed columns, found by their VALUES -------------------------
    # COMPUTED_COL_RE (in the cleaning pass) reads the header; this reads the
    # numbers -- see find_computed_columns.  It runs HERE, before the glued-
    # quantity pass and before any drop: that pass fills `quantity` on some
    # cells of a grid row and not others, which would split the row this test
    # rebuilds, and an unnamed bidder column is still one the tab averaged.
    computed_found = find_computed_columns(all_recs)
    stats["computed_columns_by_value"] = computed_found
    if computed_found:
        n_hdr = sum(1 for c in computed_found if c["header_says_computed"])
        print("  computed columns by value: %d column(s), %d row(s); %d also "
              "labelled so, %d found ONLY by value"
              % (len(computed_found), sum(c["rows"] for c in computed_found),
                 n_hdr, len(computed_found) - n_hdr))
        for c in computed_found:
            if not c["header_says_computed"]:
                # The label stored as this column's name does not say
                # "computed", so the labels of this table are not where the
                # parser thinks they are -- worth a look at the other columns.
                print("    !! %s :: %s  %r is %s"
                      % (c["src_file"], c["src_sheet"], c["column"],
                         c["evidence"]))

    # ---- the quantity that leaked into the unit cell ---------------------
    # 631 rows carry a unit cell like '1hour', '2Each', '1000HR': the quantity
    # column was never separated from the unit, so `quantity` is empty on 595 of
    # them.  An empty quantity is quietly expensive -- the row cannot enter any
    # quantity-dependent analysis, and `extended_calc` is None so the
    # arithmetic audit cannot see it either.
    #
    # The number is right there in the cell, but it is NOT always a quantity.
    # In a materials catalogue the same shape is a PART number:
    #     unit_raw='40080LF'  quantity=10  '3/4" X CLOSE MNPT BRASS NIPPLE'
    # so 40080 is a brass-nipple part code, and taking it for a quantity would
    # be wrong by four orders of magnitude.
    #
    # So the row has to PROVE it: recover the number only when the row's own
    # arithmetic confirms it, number x unit_price == extended_reported.  That is
    # a verification, not a heuristic, and it is the same identity the rest of
    # this file is audited with.  Measured: 534 of the 595 prove out, 1
    # contradicts, 60 have no extended column to check against and are left
    # empty -- refused rather than guessed.  Rows that already have a quantity
    # are never touched, which is what keeps the part numbers out.
    GLUED_UNIT_RE = re.compile(r"^([\d,]+)\s*([A-Za-z]{1,6})\.?$")
    qrec = Counter()
    for r in all_recs:
        if r.get("quantity") is not None:
            continue
        m = GLUED_UNIT_RE.match((r.get("unit_raw") or "").strip())
        if not m:
            continue
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        up, ext = r.get("unit_price"), r.get("extended_reported")
        if up is None or ext is None or n <= 0:
            qrec["no_extended_to_verify"] += 1
            continue
        if abs(n * up - ext) / max(abs(ext), 1.0) <= 0.005:
            r["quantity"] = n
            r["qty_source"] = "glued_unit_verified"
            qrec["recovered"] += 1
        else:
            qrec["contradicted"] += 1
    if qrec:
        print("  quantity glued to the unit cell: %d recovered by arithmetic, "
              "%d contradicted, %d unverifiable (left empty)"
              % (qrec["recovered"], qrec["contradicted"],
                 qrec["no_extended_to_verify"]))
        stats["glued_quantity"] = dict(qrec)

    # ---- the quantity printed rounded ------------------------------------
    # Before ext_mismatch is computed, and over every column of the row (the
    # estimate's and an unnamed one's too): a row recovers only when ALL its
    # priced columns extend on the same unrounded quantity -- see
    # recover_display_rounded_quantities.
    dr_rows, dr_recs = recover_display_rounded_quantities(all_recs, stats)
    if dr_rows:
        print("  quantity printed rounded: %d grid row(s), %d row(s) recovered "
              "by arithmetic" % (dr_rows, dr_recs))
    # ---- the quantity never read: extension / price, agreed by 2+ columns --
    ie_rows, ie_recs = recover_quantity_from_extensions(all_recs, stats)
    if ie_rows:
        print("  quantity not read: %d grid row(s), %d row(s) given extension / "
              "unit price, agreed by every priced column" % (ie_rows, ie_recs))
    # ---- (round 11) the quantity glued to the number printed after it --------------------------------
    gq_rows, gq_recs = recover_glued_quantity_prefix(all_recs, stats)
    if gq_rows:
        print("  quantity glued to the next number: %d grid row(s), %d row(s) given back the quantity every "
              "priced column extends on" % (gq_rows, gq_recs))
    # ---- the item number and quantity read into the END of the description (round 8) --
    # After the quantity passes above: the row's own quantity is what proves the trailing
    # pair (strip_trailing_item_quantity); families and sizes are recomputed there.
    n_trail = strip_trailing_item_quantity(all_recs, stats)
    if n_trail:
        print("  item number and quantity at the end of the description: %d row(s)" % n_trail)
    # ---- (round 11) the unit (and quantity) read into the end of the description --------------------
    # After the quantity passes (the row's own quantity proves the number beside the unit) and before the
    # cleaning passes, which judge every unit the same way (units_at_end_of_description).
    n_tailu = units_at_end_of_description(all_recs, stats)
    if n_tailu:
        print("  unit read into the end of the description: %d row(s) given their printed unit (%d quantities)"
              % (n_tailu, stats["unit_in_description"]["quantities"]))

    # ---- cleaning passes -------------------------------------------------
    drops = Counter()
    no_bidder_by_sheet = Counter()
    # A bidder "name" that is a dollar amount or a bare number means the
    # table's columns are misaligned.  2026-11-19 regression diagnosis:
    # Richardson Custer Road read '$ 5,027_00' as a firm and a ground box at
    # $307k-340k, which priced a Carrollton street-light foundation at
    # $164,182/EA (ratio 9.6).  Measured on the rebuilt line_items: 6 such
    # names, 308 rows, 4 files (Custer Road, Richardson 69-25 '0 0', Frisco
    # 2510-010 two bid totals as names, Frisco 2502-052 '539').  The WHOLE
    # sheet goes, not just those rows: its other columns were mapped by the
    # same broken header, and a letting scored against a wrong total is worse
    # than a missing one.  Runs on cache hits, so no PARSER_VERSION bump.
    money_name_re = re.compile(r"^\s*\$\s*\d|^\s*\d[\d,._ ]*\s*$")
    bad_sheets = {(r["src_file"], r["src_sheet"]) for r in all_recs
                  if r["bidder_raw"] and money_name_re.search(r["bidder_raw"])}
    if bad_sheets:
        print("  %d sheet(s) dropped whole: a bidder name is a dollar amount or "
              "a number (misaligned columns): %s"
              % (len(bad_sheets), "; ".join(sorted("%s::%s" % k
                                                   for k in bad_sheets))[:600]))
    # A file whose bidder names are mostly noise goes whole (garbled_files).
    bad_files = garbled_files(all_recs)
    for f, (g, n) in sorted(bad_files.items()):
        print("  file dropped whole: %d of %d bidder names garbled (bad text layer): %s"
              % (g, n, f))
    stats["files_dropped_garbled_names"] = sorted(bad_files)
    kept = []
    for r in all_recs:
        if r["src_file"] in bad_files:
            drops["file_with_garbled_bidder_names"] += 1
            continue
        if (r["src_file"], r["src_sheet"]) in bad_sheets:
            drops["sheet_with_money_named_bidder"] += 1
            continue
        if not r["bidder_raw"]:
            drops["no_bidder_name"] += 1
            no_bidder_by_sheet[r["src_file"] + "::" + r["src_sheet"]] += 1
            continue
        if PLACEHOLDER_RE.match(r["bidder_raw"]):
            drops["placeholder_bidder"] += 1
            continue
        if COMPUTED_COL_RE.match(r["bidder_raw"]):
            drops["computed_column_not_a_bidder"] += 1
            continue
        if DERIVED_COL_RE.match(r["bidder_raw"]):
            drops["derived_column_not_a_bidder"] += 1
            continue
        # Checked AFTER the header test, so this counter holds only the
        # columns the header missed.  Measured: 16 rows, E. Bethel Bridge.
        if r.get("computed_by_value"):
            drops["computed_column_by_value"] += 1
            continue
        if r["unit_price"] is None or r["unit_price"] <= 0:
            drops["nonpositive_price"] += 1
            continue
        if re.match(r"^(subtotal|total|base bid|bid total|grand)", r["description"], re.I):
            drops["aggregate_row"] += 1
            continue
        kept.append(r)

    for r in kept:
        # Row-level version of the unit gate: a unit that is a number means this
        # row's columns are offset, and the arithmetic check cannot see it
        # because the quantity landed somewhere else.
        u, raw = (r.get("unit") or ""), (r.get("unit_raw") or "")
        if raw and u not in KNOWN_UNITS and unit_from_description(raw, r.get("description")):
            u = r["unit"] = unit_from_description(raw, r.get("description"))
        r["unit_suspect"] = "Y" if (NUMERIC_UNIT_RE.match(raw.strip())
                                    or (raw and u not in KNOWN_UNITS)) else ""
        # "ocr" rows come from parse_ocr_sidecars with their engine and check
        r.setdefault("text_source", "text_layer")
        q, up, ext = r["quantity"], r["unit_price"], r["extended_reported"]
        r["extended_calc"] = (q * up) if (q is not None) else None
        r["ext_mismatch"] = ""
        if r["extended_calc"] is not None and ext is not None:
            if abs(r["extended_calc"] - ext) / max(abs(ext), 1.0) > 0.005:
                r["ext_mismatch"] = "Y"
        # A LUMP SUM is one unit.  A row that says LS and has a quantity above
        # one is either priced PER UNIT (quantity x price = extension: the unit
        # is wrong -- Mansfield 22-4735's 12" HDPE pipe bursting prints 'LS',
        # 1,596 and $110, which is $175,560 of LF pipe, and priced as 1,596
        # lump sums it came to about $216M downstream) or its quantity cell
        # holds something else (then the extension already disagrees and the
        # row is flagged ext_mismatch).  Measured on the 2026-11-19 corpus:
        # 125 LS rows with q > 1 extend per unit, 6 have no extension to check.
        # They keep the printed unit_raw but lose the unit LS (unit_note says
        # why), so no price is filed or multiplied as a lump sum.  They are NOT
        # unit_suspect: marking them so (2026-11-19.20) took real money out of
        # the letting totals -- Mansfield 22-4735's low bid lost $175,560
        # (12.8%), Richardson 35-25 its $160,000 allowance, and six lettings
        # stopped matching their printed totals (reports/
        # parser_fixes_independent_check.md).  Without a unit the row stays in
        # the totals and out of every unit-price table (build_master groups on
        # unit; estimate_low_bid refuses a line with no unit).
        # A lump sum whose description states its quantity, "(28,731 CY)":
        # that quantity and unit, the money unchanged (round 5).
        if apply_ls_quantity_in_description(r):
            stats["ls_quantity_in_description"] = stats.get("ls_quantity_in_description", 0) + 1
        if clear_lump_sum_with_quantity(r):
            stats["lump_sum_unit_cleared"] = stats.get("lump_sum_unit_cleared", 0) + 1
        # A unit cell that repeats the quantity (unit_cell_repeats_quantity):
        # the money stays in the totals, the unit is unknown.
        if r["unit_suspect"] == "Y" and unit_cell_repeats_quantity(r):
            r["unit_suspect"] = ""
            r["unit"] = ""
            r["unit_note"] = "unit_cell_repeats_quantity"
            stats["unit_cell_repeats_quantity"] = stats.get("unit_cell_repeats_quantity", 0) + 1
        # EA by the hundred on a pipe or grout-abandonment line (round 5).
        # Main session 2026-11-20: the money stays in the totals and only the
        # unit is cleared, like clear_lump_sum_with_quantity and
        # unit_cell_repeats_quantity above -- marking the row unit_suspect took
        # $9k-$58k out of four lettings' totals and gated Mesquite 25-7561,
        # the failure mode the .20 verification warned about.
        if r["unit_suspect"] != "Y" and ea_quantity_suspect(r):
            r["unit"] = ""
            r["unit_note"] = "ea_quantity_suspect"
            stats["ea_quantity_suspect"] = stats.get("ea_quantity_suspect", 0) + 1
        # A description that is still only a code (round 7, desc_is_code_only):
        # the money stays in the totals, the unit goes, so no cell trains on
        # the line and no catch-all cell prices it.
        if r["unit_suspect"] != "Y" and desc_is_code_only(r.get("description")):
            if r.get("unit"):
                r["unit"] = ""
                r["unit_note"] = "desc_code_only"
                stats["desc_code_only"] = stats.get("desc_code_only", 0) + 1
            elif not r.get("unit_note"):
                r["unit_note"] = "desc_code_only"
                stats["desc_code_only"] = stats.get("desc_code_only", 0) + 1
    # A lump sum printed with a measure unit (round 6, measure_qty1_lump_sums):
    # unit LS, quantity 1 and money unchanged.
    lump = measure_qty1_lump_sums([r for r in kept if r.get("unit_suspect") != "Y"])
    for r in kept:
        if r.get("unit_suspect") != "Y" and (r.get("unit") or "") in MEASURE_QTY1_UNITS \
                and _measure_item_key(r) in lump:
            r["unit"] = "LS"
            r["unit_note"] = "measure_qty1_lump_sum"
            stats["measure_qty1_lump_sum"] = stats.get("measure_qty1_lump_sum", 0) + 1
    # The width of an LF sidewalk / trail as its size, in FEET (round 7,
    # lf_width_size); after every unit rule, because it depends on the unit.
    for r in kept:
        if (r.get("unit") or "") == "LF" and r.get("item_family") in WIDTH_LF_FAMILIES:
            w = lf_width_size(r)
            if w != r.get("size_in"):
                r["size_in"] = w
                stats["lf_width_size"] = stats.get("lf_width_size", 0) + 1
    # (round 9, .30) A thickness is no size on a piece priced EACH or as a lump: "Constr. 4'x4' 4"
    # Thick Reinf. Conc. Pad @ SSMH" (EA, bids $1,000) took size 4 in, and F1 scaled the nearest sized
    # concrete_misc EA cell to $16,887.  Per SY / SF / CY the thickness IS the size and stays.
    for r in kept:
        if (r.get("unit") or "") in ("EA", "LS") and r.get("size_in") is not None \
                and r.get("item_family") not in ("box_culvert",) and size_is_thickness(r.get("desc_norm")):
            r["size_in"] = None
            stats["ea_thickness_size"] = stats.get("ea_thickness_size", 0) + 1

    # ---- a numbered group line that is its sub-lines' subtotal -------------
    # After the cleaning passes, so a sub-line that is itself a total caption
    # ('1.1 Total Material Cost ...', '1.2 Total Labor Cost ...' under Collin
    # County 2024-282's '1 Project Bid Line Total', dropped as aggregate rows)
    # never makes its line a subtotal.
    n0 = len(kept)
    kept = subtotal_parent_lines(kept, stats)
    if len(kept) != n0:
        drops["group_line_subtotal_of_its_sublines"] += n0 - len(kept)
        print("  group lines that are the subtotal of their N.k lines: %d dropped, %d "
              "sub-lines given their alternate" % (n0 - len(kept),
                                                  stats.get("group_alternate_lines", 0)))

    # ---- a reading whose arithmetic fails almost everywhere ---------------
    # A row that fails quantity x price = extension is already ext_mismatch
    # and never reaches the panel; the rows of the SAME reading that print no
    # extension cannot be checked at all, and they came out of the same broken
    # text.  Balch Springs Elam Rd's text layer is two tabs overprinted: every
    # one of its 68 checkable rows fails, and its 58 uncheckable ones (codes
    # read as quantities, $1,500 'PREPARING ROW') were filed as prices.  So in
    # a file with ten or more checkable rows of which 90% or more fail, the
    # uncheckable rows go too.  (2026-11-19 corpus: 11 files; outside Balch
    # Springs they hold 8 uncheckable rows.)
    failed_reading = failed_arithmetic_files(kept)
    if failed_reading:
        before = len(kept)
        kept = [r for r in kept if r["src_file"] not in failed_reading
                or (r.get("extended_calc") is not None and r.get("extended_reported") is not None)]
        drops["uncheckable_row_in_failed_reading"] += before - len(kept)
        stats["files_with_failed_arithmetic"] = sorted(failed_reading)
        print("  %d file(s) whose checkable rows nearly all fail arithmetic: %d "
              "uncheckable row(s) dropped" % (len(failed_reading), before - len(kept)))

    # ---- duplicate SHEETS, not duplicate rows ----------------------------
    # Several workbooks republish the same tab (Waxahachie "Internal Bid Tab",
    # the 2700/8700 fund splits). Row-level dedup is wrong here: the same
    # contractor legitimately bids the same item at the same price on different
    # streets within one tab. So compare whole sheets and drop the redundant one.
    by_sheet = {}
    for r in kept:
        by_sheet.setdefault((r["src_file"], r["src_sheet"]), []).append(r)

    # The signature and the preference order both come from src/dedup.py, which
    # is also what build_master.py applies to the panel.  They used to be
    # defined here and nowhere else; having ONE definition is the point, because
    # the failure this pass exists to fix was two code paths disagreeing about
    # what counts as the same row.  dedup.sig_of additionally canonicalises
    # numbers, so a missing quantity no longer fails to match its own copy.
    sig_of = dedup.sig_of

    def sheet_pref(key):
        return dedup.sheet_pref(key, by_sheet[key])

    sigs = {k: set(sig_of(x) for x in v) for k, v in by_sheet.items()}
    dropped_sheets, sheet_overlaps = {}, []
    keys = sorted(by_sheet)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            if a[0] != b[0] or a in dropped_sheets or b in dropped_sheets:
                continue
            sa, sb = sigs[a], sigs[b]
            if not sa or not sb:
                continue
            inter = len(sa & sb)
            cover = inter / min(len(sa), len(sb))
            if cover >= 0.9:
                winner, loser = (a, b) if sheet_pref(a) >= sheet_pref(b) else (b, a)
                dropped_sheets[loser] = {
                    "kept_instead": winner[1], "overlap": round(cover, 3),
                    "rows_dropped": len(by_sheet[loser])}
                sheet_overlaps.append({"file": a[0], "sheet_a": a[1], "sheet_b": b[1],
                                       "overlap": round(cover, 3),
                                       "dropped": loser[1], "kept": winner[1]})
    # ... and an estimate column reprinted on a later page under a caption
    # the text layer garbled (reprinted_estimate_sheets)
    for loser, winner in reprinted_estimate_sheets(
            {k: v for k, v in by_sheet.items() if k not in dropped_sheets}).items():
        dropped_sheets[loser] = {"kept_instead": winner[1], "overlap": 1.0,
                                 "basis": "reprinted_estimate",
                                 "rows_dropped": len(by_sheet[loser])}
        sheet_overlaps.append({"file": loser[0], "sheet_a": loser[1], "sheet_b": winner[1],
                               "overlap": 1.0, "basis": "reprinted_estimate",
                               "dropped": loser[1], "kept": winner[1]})

    # ---- the same tab published under two FILE names ---------------------
    # The loop above opens with `if a[0] != b[0]: continue`, so by construction
    # it only ever compared sheets WITHIN one file and cross-file duplicates
    # were invisible.  Confirmed instance in the ACTIVE set (not _pre2020):
    # farmersbranch 2025-04-24 and 2025-06-18 are MD5-identical, 1,351 rows
    # each, both fully ingested -- one $3.2M project counted twice.
    #
    # Double-counting does not just inflate the sample.  The worse effect is
    # that it ARTIFICIALLY NARROWS the distribution: duplicating a letting's
    # bids leaves the median where it was but tightens the IQR, so an item
    # reads as having a stable price when the stability is just the same bids
    # counted again.
    #
    # CAUTION on the threshold: a RE-BID of the same project, or a revised tab
    # that changed only a few items, is a legitimate independent observation and
    # must NOT be deleted as a duplicate.  The signature deliberately ignores the
    # description, which makes it possible for two sections of one project, or a
    # revision, to look identical.  A byte-level census of the active corpus
    # found 23 duplicate groups / 31 redundant copies (2.6% of 1,192 PDFs), all
    # traceable to collectors de-duplicating by URL rather than by content.  If
    # this pass flags materially more than that, the excess is content-SIMILAR
    # rather than content-IDENTICAL: tighten the threshold or put the description
    # back into the signature, do not drop the rows.  Live corpus: 2 file pairs,
    # 9 sheets, 1,034 rows -- well under the census, so the rule is not reaching.
    #
    # The implementation moved to src/dedup.py, which build_master.py also calls.
    # The version that lived here measured each pair's overlap with the COUNTER
    # built by the inverted index, and that counter was capped at 8 sheets per
    # signature for efficiency.  The cap was meant to bound candidate
    # GENERATION; using its output as the MEASUREMENT meant every generic
    # signature a duplicate pair happened to share pushed that pair's coverage
    # down.  Seven byte-identical farmersbranch sheets scored 0.786-0.899 that
    # way and six of them survived the 0.90 threshold -- 819 rows of one $3.2M
    # letting counted twice.  See src/dedup.py for the measured figures.
    #
    # Only sheets that SURVIVED the within-file pass are offered here.  A sheet
    # already dropped as a within-file duplicate could otherwise be picked as
    # the winner of a cross-file pair and take its rival down with it, leaving
    # neither copy of that letting in the output.
    sigs_live = {k: s for k, s in sigs.items() if k not in dropped_sheets}
    x_dropped, x_overlaps = dedup.find_duplicate_sheets(
        sigs_live,
        {k: set(sg[1:] for sg in s) for k, s in sigs_live.items()},
        lambda k: by_sheet[k],
        lambda k: (by_sheet[k][0].get("owner") or "") if by_sheet.get(k) else "",
        cross_file_only=True)
    dropped_sheets.update(x_dropped)
    for o in x_overlaps:
        sheet_overlaps.append({"file": o["file_a"] + " | " + o["file_b"],
                               "sheet_a": o["sheet_a"], "sheet_b": o["sheet_b"],
                               "overlap": o["overlap"],
                               "overlap_bidderfree": o["overlap_bidderfree"],
                               "basis": o["basis"],
                               "dropped": o["dropped"], "kept": o["kept"]})

    # ---- the same FILE twice, byte for byte -------------------------------
    # Content comparison works sheet by sheet and can leave a sheet of a copy
    # behind (see dedup's leftover pass); a file whose bytes equal another's
    # is the same document, whatever its sheets look like.  The copy with the
    # most surviving rows stays (ties: dedup's sheet preference, then the
    # path); every surviving sheet of the others goes.  On the 2026-11-19
    # corpus, 19 byte-identical groups exist and content dedup already left
    # rows in only one member of each -- this is the guarantee, not a repair.
    live_rows = Counter(k[0] for k, v in by_sheet.items() if k not in dropped_sheets
                        for _ in v)
    path_of = {rel: p for rel, p, _s, _o in inputs}
    for files_same in identical_file_groups(
            [f for f in live_rows if f in path_of], path_of):
        keep = max(files_same, key=lambda f: (
            live_rows[f],
            max((sheet_pref(k) for k in by_sheet if k[0] == f and k not in dropped_sheets),
                default=()),
            [-ord(ch) for ch in f]))
        for k in sorted(by_sheet):
            if k[0] in files_same and k[0] != keep and k not in dropped_sheets:
                dropped_sheets[k] = {"kept_instead": keep + "::*", "overlap": 1.0,
                                     "basis": "identical_file",
                                     "rows_dropped": len(by_sheet[k]), "cross_file": True}
                sheet_overlaps.append({"file": k[0] + " | " + keep, "sheet_a": k[1],
                                       "sheet_b": "*", "overlap": 1.0,
                                       "basis": "identical_file",
                                       "dropped": k[0] + "::" + k[1], "kept": keep + "::*"})

    deduped = [r for r in kept
               if (r["src_file"], r["src_sheet"]) not in dropped_sheets]
    dup_by_sheet = {k[0] + "::" + k[1]: v for k, v in dropped_sheets.items()}

    # ---- base vs alternate -----------------------------------------------
    # Section labels are the owner's wording, not a schema: Rowlett writes
    # "BASE BID", another owner names the section after the site.  Normalise to a role so
    # downstream work can compare like with like (a project total means the base
    # scope; alternates are priced but may not be awarded).
    # Role is decided by what the section IS, not by which section happens to be
    # the biggest.  The previous rule took the largest non-alternate section per
    # file as base and demoted the rest to "other" -- and five downstream filters
    # drop section_role != "base" outright (build_master, build_vocabulary,
    # build_project_metadata, opcc_analysis, qc_report).  A bid tab with several
    # funding sections therefore lost all but its biggest: 1,273 of arlington's
    # 7,465 rows (17.1%), 32 of 55 files having more than one non-alternate
    # section. Every one of the 3,332 "other" rows in the library was checked and
    # all were legitimate base scope (SITE WORK, HARDSCAPE, WATER IMPROVEMENTS,
    # SECTION IV PAVEMENT MARKING, PAVING IMPROVEMENTS); not one was a real
    # alternate. The loss was also directionally biased: in paving-heavy files
    # it removed DRAINAGE / WATER / SANITARY SEWER / STREETLIGHTS & FIBER /
    # TREES & LANDSCAPING, suppressing exactly those item_family samples while
    # leaving paving intact.
    #
    # So: an alternate is recognised explicitly, a short list of non-scope
    # sections is recognised explicitly, and everything else is base.
    ALT_SEC_RE = re.compile(r"\balt", re.I)
    NONBASE_SEC_RE = re.compile(
        r"\bdeduct|contingenc|\bextra\s+work\b|owner.?s?\s+reserve|\bforce\s+account",
        re.I)
    for r in deduped:
        s = r["bid_section"] or ""
        if ALT_SEC_RE.search(s):
            r["section_role"] = "alternate"
        elif NONBASE_SEC_RE.search(s) or TIME_BID_SEC_RE.search(s):
            r["section_role"] = "other"
        else:
            r["section_role"] = "base"
    # ... unless the document's own base total includes the section (see
    # nonbase_sections_in_printed_base)
    in_base = nonbase_sections_in_printed_base(deduped, doc_facts)
    for r in deduped:
        if r["section_role"] == "other" and (r["src_file"], r["bid_section"]) in in_base:
            r["section_role"] = "base"
    stats["nonbase_sections_in_printed_base"] = sorted("%s::%s" % k for k in in_base)
    # ... and an item every bidder's printed total leaves out is not base
    # either (items_outside_printed_total)
    outside = items_outside_printed_total(deduped, doc_facts)
    for r in deduped:
        if (r["section_role"] == "base"
                and (r["src_file"], r.get("item_no") or "", r.get("desc_norm") or "") in outside):
            r["section_role"] = "other"
    stats["items_outside_printed_total"] = sorted("%s::%s" % k[:2] for k in outside)

    # ---- write -----------------------------------------------------------
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(REPORTS, exist_ok=True)

    cols = ["src_file", "source_set", "owner", "src_sheet", "bid_date", "bid_date_source",
            "bid_section", "section_role", "item_no",
            "description", "desc_norm", "item_family", "size_in", "material",
            "unit_raw", "unit", "quantity",
            # Empty for a quantity read from its own column; set when the
            # quantity had leaked into the unit cell and was recovered only
            # because the row's own arithmetic proved the number.  Carried so a
            # consumer can exclude recovered quantities if it wants to, instead
            # of having to trust them blind.
            "qty_source",
            "bidder_raw", "bidder",
            "is_engineer_estimate", "unit_price", "extended_reported",
            "extended_calc", "ext_mismatch", "unit_suspect",
            # why a printed unit was set aside ('lump_sum_with_quantity')
            "unit_note",
            # provenance of the text: "text_layer", or "ocr" for a scan read
            # from its OCR sidecar, with the engine and the check it passed
            # ("qty_x_price" or the weaker "unit_equals_total")
            "text_source", "ocr_engine", "ocr_check"]
    with open(os.path.join(OUT, "line_items.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)

    if summary_totals:
        keys = ["src_file", "owner", "project_title", "bid_date", "bidder_raw", "bidder",
                "is_engineer_estimate", "total_bid", "rank", "n_bidders", "low_bid"]
        with open(os.path.join(OUT, "project_totals.csv"), "w", newline="",
                  encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(summary_totals)

    # What each document holds, for award_identity's completeness gate: the
    # distinct item lines its text prints and its printed TOTAL amounts
    # (document_facts).  Always written, one row per file read.
    with open(os.path.join(OUT, "doc_facts.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["src_file", "doc_items", "printed_totals", "total_line_amounts",
                    "contract_hint"])
        for f in sorted(doc_facts):
            d = doc_facts[f]
            w.writerow([f, d["items"], " ".join("%.2f" % (c / 100.0) for c in d["totals"]),
                        " ".join("%.2f" % (c / 100.0) for c in d.get("total_lines", ())),
                        d.get("contract_hint", "")])

    # Always written, so a run that finds no site rows cannot leave an older
    # run's file behind looking current (it did: 50 rows from the PC's 09:10
    # run survived the Mac rebuild that read none).
    site_cols = ["src_file", "quantity_basis", "site", "bidder_raw", "bidder",
                 "is_engineer_estimate", "site_total"]
    with open(os.path.join(OUT, "site_totals.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=site_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(site_rows)

    with open(os.path.join(OUT, "parse_audit.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["src_file", "src_sheet", "header_row", "n_price_cols", "n_rows", "bidders"])
        for a in all_audit:
            w.writerow([a["src_file"], a["src_sheet"], a["header_row"], a["n_price_cols"],
                        a["n_rows"], " || ".join(a["bidders"])])

    summary = {
        "files_seen": files,
        "file_dates": file_dates,
        "rows_raw": len(all_recs),
        "rows_after_drop": len(kept),
        "rows_final": len(deduped),
        "drops": dict(drops),
        "duplicate_sheets_dropped": dup_by_sheet,
        "sheet_overlap_detail": sheet_overlaps,
        "blank_price_cells": stats["cells_blank_price"],
        "aggregate_rows_skipped": stats.get("agg_rows_skipped", 0),
        "pdf_tables_with_inherited_header": stats.get("tables_with_inherited_header", 0),
        "two_row_headers_merged": stats.get("two_row_headers_merged", 0),
        "headerless_tables_inferred": stats.get("headerless_tables_inferred", 0),
        "bidder_names_inherited": stats.get("bidder_names_inherited", 0),
        "no_bidder_name_top_sheets": no_bidder_by_sheet.most_common(25),
        "summary_total_rows": len(summary_totals),
        "summary_total_files": len(set(r["src_file"] for r in summary_totals)),
        "sheets_no_header": stats["sheets_no_header"],
        "cache_hits": stats.get("cache_hits", 0),
        "cache_misses": stats.get("cache_misses", 0),
        "skipped_not_bidtab": stats.get("skipped_not_bidtab", []),
        "skipped_dirs": sorted(stats.get("skipped_dirs", set())),
        "parses_rejected_bad_units": stats.get("parses_rejected_bad_units", 0),
        "pdf_strategy": stats.get("pdf_strategy", {}),
        "coord_parser_errors": stats.get("coord_parser_errors", []),
        "site_level_rows": site_rows_n,
        # every column the value test called computed, header-named or not
        "computed_columns_by_value": stats.get("computed_columns_by_value", []),
        "display_rounded_quantity": stats.get("display_rounded_quantity", {}),
        # files named .pdf that are Office packages (sniff_office), and the
        # zero-row repairs of 2026-11-19 (see reports/zero_row_files.md)
        "office_files_by_content": sorted(stats.get("office_files_by_content", [])),
        "bidder_names_below_header": stats.get("bidder_names_below_header", 0),
        "bidder_names_above_table": stats.get("bidder_names_above_table", 0),
        "alternate_title_above_table": stats.get("alternate_title_above_table", 0),
        "inherited_header_refused": stats.get("inherited_header_refused", 0),
        "unit_qty_columns_swapped": stats.get("unit_qty_columns_swapped", 0),
        # the 2026-11-19 Mac-verification fixes (reports/parser_fix_verification.md)
        "pdf_strategy_by_printed_totals": stats.get("pdf_strategy_by_printed_totals", 0),
        "item_numbers_split_from_description":
            stats.get("item_numbers_split_from_description", 0),
        "files_dropped_garbled_names": stats.get("files_dropped_garbled_names", []),
        "lump_sum_unit_cleared": stats.get("lump_sum_unit_cleared", 0),
        # parser round 3 (reports/parser_round3.md)
        "subtotal_group_lines_dropped": stats.get("subtotal_group_lines_dropped", 0),
        "group_alternate_lines": stats.get("group_alternate_lines", 0),
        # the 2026-11-19 second round (reports/parser_fix_verification.md section 14)
        "quantity_from_extensions": stats.get("quantity_from_extensions", {}),
        "files_with_failed_arithmetic": stats.get("files_with_failed_arithmetic", []),
        "stacked_cells_spread": stats.get("stacked_cells_spread", 0),
        "site_level_files": site_level_inputs(files),
        # scans read from their OCR sidecars: per file, rows kept / dropped
        "ocr_sidecars": stats.get("ocr_sidecars", {}),
        "ocr_sidecar_errors": stats.get("ocr_sidecar_errors", []),
    }
    # ---- the duplicate decisions, as a TABLE and not only as JSON --------
    # They were only in parse_summary.json under `duplicate_sheets_dropped`,
    # while data/clean/duplicate_lettings.csv was written by build_master.py
    # alone -- so the three Mesquite pairs this stage dropped at source were
    # absent from the table, and a consumer joining against it kept reporting
    # them as "parsed but lost downstream".  That is the exact false signal the
    # table exists to prevent.  One table, both stages, with `stage` saying
    # which pass made the call.
    if sheet_overlaps:
        dcols = ["stage", "file", "sheet_a", "sheet_b", "overlap",
                 "overlap_bidderfree", "basis", "dropped", "kept"]
        with open(os.path.join(OUT, "duplicate_lettings.csv"), "w",
                  newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=dcols, extrasaction="ignore")
            w.writeheader()
            for o in sheet_overlaps:
                row = dict(o)
                row["stage"] = "etl"
                w.writerow(row)
        print("  -> data/clean/duplicate_lettings.csv  (%d sheet drop(s))"
              % len(sheet_overlaps))

    with open(os.path.join(OUT, "parse_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print(json.dumps({k: v for k, v in summary.items() if k != "files_seen"},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
