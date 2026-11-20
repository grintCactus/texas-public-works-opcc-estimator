"""Item specifications read from bid-item DESCRIPTIONS -- text only.

HANDOFF 2.5 item 6.  The estimator prices a pay item by (family, unit,
size_bucket) plus a token match, and size_bucket comes from ONE number:
`extract_size_in` in parse_bidtabs takes the first "N in" of the normalised
description and calls it a pipe diameter.  That is right for most pipe rows
and meaningless for the client's core erosion items, where the number in the
text is a wall height, a riprap layer thickness, a stone size or a mattress
thickness -- or is lost entirely, because norm_desc turns the foot mark into a
space ("9'x6' RCB" -> "9 x6 rcb").  This module reads those dimensions with
their MEANING, from the raw description, so they can be tested as price keys.

The user agreed: text only, no drawings, no image recognition.

What it reads, by spec family (see `spec_family`):

    gabion_basket    basket width x height (ft), basket thickness (in),
                     wall height (ft), coating
    gabion_mattress  mattress thickness (in), coating
    riprap_stone     layer thickness (in); stone size: D50, a single stated
                     stone size, or a gradation range ("18-24 in", "18x24");
                     class/type (TxDOT Ty R / Ty F / common / protection,
                     NCTCOG Type A/B); grouted / dry
    riprap_conc      slab thickness (in)
    retaining_wall   wall height (ft): exact, range, "up to", "greater than"
    headwall_wingwall  structure height (HW=, H=) (ft)
    toe_wall_beam    wall depth / height (ft), thickness (in)
    storm_pipe       diameter (in, or a range), material, class
                     (RCP III/IV/V, SDR/DR, Sch), barrels, trench depth (ft),
                     open cut vs trenchless
    box_culvert      span x rise (ft), barrels, class
    channel_lining   concrete thickness (in)
    excavation       depth (ft), kind (channel / roadway / unclassified ...)
    water_sewer_pipe same as storm_pipe (reported, not a target family)
    trm, dewatering_diversion  -- classified for the dollar table; the text
                     carries no dimension for them (measured: see report)

HARD RULES this module keeps
----------------------------
1. Units carry no product information (README rule 11).  Nothing here looks
   at the pay unit, and nothing converts SY to CY by an assumed thickness.
   The only conversions are between the length marks written IN THE TEXT
   (48-inch wall height -> 4 ft; 1.5' depth -> 18 in).
2. A number is read only when the text says what it is: a unit mark (in, ",
   ft, '), a role word (thick, deep, high, dia, D50, HW=), or a pattern whose
   meaning is fixed by the family (a box culvert's "9x6" is span x rise in
   feet).  A riprap size whose meaning the text does not state is kept apart
   as `rr_nominal_in` -- "18 in rock riprap" is a layer thickness in one
   owner's tab and a stone size in another's (Arlington writes both
   "18-inch Rock Riprap (1.5' Depth)" and "18-inch Rock Riprap (2' Depth)").
3. Implausible values are DROPPED, not repaired: a pipe of 400 in, a box of
   8 in x 4 in, a riprap stone of 18 ft.  Precision first; the coverage
   table shows what that costs.

Run:
    python src/item_specs.py --selftest
    python src/item_specs.py --report   [--data DIR] [--out DIR]
    python src/item_specs.py --sample N  (writes the hand-check sample)

`--report` reads the frozen snapshot handoff/pc_data_clean_2026-11-19 by
default (data/clean was being rebuilt when this was written) and public rows
only: company rows (line_items src_file with no path separator,
master_panel source_set == internal) never enter a statistic.  Nothing in
the estimator imports this module; any use of it as a price key has to be
scored forward in time first.

Measured on that snapshot (reports/item_specs/README.md has the tables):
280 descriptions checked by hand, 309 values read, 1 wrong and 2 that the
text does not settle -- 99.0%.  The weak point is the FAMILY call, not the
numbers: on 42 descriptions that exist only in line_items (never seen while
this was written) 8 were put in a family they do not belong to (stone
veneer on a wall, site preparation at a flume, notes, OCR-garbled removals).
"""
import argparse
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "handoff", "pc_data_clean_2026-11-19")
OUTDIR = os.path.join(ROOT, "reports", "item_specs")

I = re.I

# ---------------------------------------------------------------- normalise

_UNI = [
    ("“", '"'), ("”", '"'), ("„", '"'), ("″", '"'),
    ("˝", '"'), ("‘", "'"), ("’", "'"), ("′", "'"),
    ("´", "'"), ("`", "'"), ("×", "x"), ("✕", "x"),
    ("–", "-"), ("—", "-"), ("−", "-"), ("‐", "-"),
    ("‑", "-"), ("⁄", "/"), (" ", " "), ("½", " 1/2"),
    ("¼", " 1/4"), ("¾", " 3/4"), ("°", " deg "),
]


def normalize(desc):
    """Unify marks and strip parse debris, KEEPING the inch and foot marks.

    parse_bidtabs.norm_desc deletes the foot mark, which is fatal here; this
    is a separate, lossless-for-dimensions normaliser.  Every repair below is
    a spelling seen in the public corpus:
      (cid:415)            pdfminer ligature for "ti" ("Exis(cid:415)ng")
      '' (two quotes)      an inch mark
      Class Ill / CL Ill   OCR of Class III
      12 ln / 1.S Ft       OCR of 12 in / 1.5 ft
      050 = 8"             OCR of D50 = 8"
      11/2-inch with the fraction slash  -> 1 1/2
      #0-74 4 inch ...     a Lewisville item number glued to the text
      303 18-inch ...      an Arlington item number left in the text
    """
    s = "" if desc is None else str(desc)
    if s.lower() in ("nan", "none"):
        return ""
    s = s.replace("(cid:415)", "ti")
    s = re.sub(r"\(cid:\d+\)", "", s)
    # "11⁄2-inch" (fraction slash, digits glued) is 1 1/2
    s = re.sub(r"(?<!\d)(\d)(\d)⁄(\d)", r"\1 \2/\3", s)
    for a, b in _UNI:
        s = s.replace(a, b)
    s = s.replace("''", '"')
    s = re.sub(r"\b(class|cl)\s*\.?\s*(ill|lll|iil|il1|1ii|i1i)\b", r"\1 III", s, flags=I)
    s = re.sub(r"(\d)\s*ln\b", r"\1 in", s)
    s = re.sub(r"(\d)\.S\b", r"\1.5", s)
    s = re.sub(r"(?<![\d.\w])0\s?50\s*=", "D50 =", s)
    # glued item numbers at the very start
    s = re.sub(r"^\s*#\d+-\d{2}(?=[^\s\d])", "", s)        # "#0-60Install"
    s = re.sub(r"^\s*#\d+-\d{2}(?=\d)", "", s)             # "#0-744 inch" -> "4 inch"
    s = re.sub(r"^\s*#\d+-\d+\s+", "", s)                   # "#1-152 1 1/4"
    s = re.sub(r"^\s*\d{3,4}\s+(?!(?:in|inch|inches|ft|foot|feet|x|lf|sy|cy|ea|psi)\b)"
               r"(?=[A-Za-z0-9(])", "", s, flags=I)
    s = re.sub(r"^\s*-\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- numbers

NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)"
IN_U = r'(?:\s*"|\s*-?\s*(?:inch(?:es)?|in)\b\.?)'
FT_U = r"(?:\s*'|\s*-?\s*(?:ft|foot|feet)\b\.?)"
# A number that belongs to a code ("PW-1", "SDR-26", "C-76", "TY4") is not a
# length.  x/X stay allowed in front: they are the times sign in "3'X1.5'".
_PRE = r"(?<![A-WYZa-wyz#])(?<![A-WYZa-wyz]-)(?<![A-WYZa-wyz]\s-)"


def _num(t):
    t = t.strip()
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", t)
    if m:
        return int(m.group(1)) + int(m.group(2)) / max(int(m.group(3)), 1)
    m = re.match(r"^(\d+)/(\d+)$", t)
    if m:
        return int(m.group(1)) / max(int(m.group(2)), 1)
    try:
        return float(t)
    except ValueError:
        return None


def _unit(u):
    if u is None or u == "":
        return None
    u = u.strip().lower()
    if u.startswith('"') or "in" in u:
        return "in"
    return "ft"


class M(object):
    """One dimension mention: a single length, a range, or an a x b (x c)."""
    __slots__ = ("s", "e", "kind", "vals", "units", "count", "role",
                 "qual", "secondary", "paren", "text")

    def __init__(self, s, e, kind, vals, units, text):
        self.s, self.e, self.kind = s, e, kind
        self.vals, self.units, self.text = list(vals), list(units), text
        self.count, self.role, self.qual = None, set(), None
        self.secondary, self.paren = False, False

    def unit(self):
        us = [u for u in self.units if u]
        return us[-1] if us else None

    def to_in(self, i=0):
        v, u = self.vals[i], self.units[i] or self.unit()
        if v is None:
            return None
        return v * 12.0 if u == "ft" else v if u == "in" else None

    def to_ft(self, i=0):
        v, u = self.vals[i], self.units[i] or self.unit()
        if v is None:
            return None
        return v / 12.0 if u == "in" else v if u == "ft" else None

    def __repr__(self):
        return "M(%s %s %s %s%s)" % (self.kind, self.vals, self.units,
                                     sorted(self.role),
                                     " SEC" if self.secondary else "")


_RX_FTIN = re.compile(_PRE + r"(?P<a>" + NUM + r")" + FT_U + r"\s*-?\s*(?P<b>" + NUM
                      + r")" + IN_U, I)
_RX_SINGLE = re.compile(_PRE + r"(?P<a>" + NUM + r")(?P<u>" + IN_U + r"|" + FT_U + r")", I)
_RX_BARE = re.compile(_PRE + r"(?<![\d.])(?P<a>\d+(?:\.\d+)?)(?![\d./])(?!\s*(?:%|psi|sack|"
                      r"cy|sy|lf|sf|ea|ls|gal|ton|mil|degree|deg|cm|mm|kv|lb|ga|gauge)\b)", I)
_SEP_RANGE = re.compile(r"^\s*(?:(?:dia\w*|diameter|thick|deep|high|tall|wide)\.?\s*)?"
                        r"(?:(?:-\s*)?(?:to|thru|through)\s*|-\s*)$", I)
_SEP_X = re.compile(r"^\s*[x×]\s*$|^\s+by\s+$", I)


def mentions(t):
    """All length mentions in a normalised description, left to right.

    Compound feet-inches first (6'-6", 2 Feet 8 Inch), then single lengths,
    then bare numbers (kept only where an x or a range glues them to a
    length, or where a box/riprap/gabion pattern gives them a unit).
    Adjacent mentions are merged: "18 - 24 in" -> range, "9' x 6'" -> pair,
    "3' x 3' x 3'" -> triple.
    """
    taken = [False] * (len(t) + 1)
    out = []

    def free(s, e):
        return not any(taken[s:e])

    def take(s, e):
        for i in range(s, e):
            taken[i] = True

    for m in _RX_FTIN.finditer(t):
        b = _num(m.group("b"))
        if b is None or b >= 12:
            continue
        out.append(M(m.start(), m.end(), "single", [_num(m.group("a")) + b / 12.0],
                     ["ft"], m.group(0)))
        take(m.start(), m.end())
    for m in _RX_SINGLE.finditer(t):
        if not free(m.start(), m.end()):
            continue
        out.append(M(m.start(), m.end(), "single", [_num(m.group("a"))],
                     [_unit(m.group("u"))], m.group(0)))
        take(m.start(), m.end())
    for m in _RX_BARE.finditer(t):
        if not free(m.start(), m.end()):
            continue
        out.append(M(m.start(), m.end(), "bare", [_num(m.group("a"))], [None],
                     m.group(0)))
    out.sort(key=lambda x: x.s)

    # merge neighbours: x -> pair/triple, range separators -> range
    def as_in(v, u):
        return None if v is None else v * 12.0 if u == "ft" else v

    merged = []
    i = 0
    count = None
    while i < len(out):
        cur = out[i]
        j = i + 1
        # "4-6'x4'", "3-10' x 5'", "6-6'X3'": a small bare integer, a dash,
        # then a PAIR is a barrel count, not the low end of a range
        if cur.kind == "bare" and j + 1 < len(out) and cur.vals[0] == int(cur.vals[0]) \
                and 1 <= cur.vals[0] <= 12 and re.match(r"^\s*-\s*$", t[cur.e:out[j].s]) \
                and _SEP_X.match(t[out[j].e:out[j + 1].s]):
            count = int(cur.vals[0])
            i = j
            continue
        while j < len(out):
            gap = t[cur.e:out[j].s]
            nxt = out[j]
            if _SEP_X.match(gap) and cur.kind in ("single", "bare", "pair") \
                    and nxt.kind in ("single", "bare"):
                kind = "pair" if cur.kind in ("single", "bare") else "triple"
                cur = M(cur.s, nxt.e, kind, cur.vals + nxt.vals,
                        cur.units + nxt.units, t[cur.s:nxt.e])
                j += 1
                continue
            nxt_starts_pair = j + 1 < len(out) and _SEP_X.match(t[nxt.e:out[j + 1].s])
            if _SEP_RANGE.match(gap) and cur.kind in ("single", "bare") \
                    and nxt.kind == "single" and len(cur.vals) == 1 and not nxt_starts_pair:
                u_lo = cur.units[0] or nxt.units[0]
                a, b = as_in(cur.vals[0], u_lo), as_in(nxt.vals[0], nxt.units[0])
                if a is None or b is None or a >= b:
                    break                   # "15" - 2 2/3" x 1/2"": not a range
                cur = M(cur.s, nxt.e, "range", cur.vals + nxt.vals,
                        [u_lo, nxt.units[0]], t[cur.s:nxt.e])
                rw = re.match(r"^\s*(dia\w*|diameter|thick|deep|high|tall|wide)", gap, I)
                if rw:
                    cur.role.add({"thick": "thick", "deep": "depth", "high": "height",
                                  "tall": "height", "wide": "width"}.get(rw.group(1).lower(),
                                                                        "dia"))
                j += 1
                continue
            break
        if count is not None and cur.kind in ("pair", "triple"):
            cur.count = count
        count = None
        merged.append(cur)
        i = j
    # a lone bare number means nothing; keep it only inside pair/triple
    res = [m for m in merged if m.kind != "bare"]
    for m in res:
        if m.kind in ("pair", "triple"):
            # a bare side inherits the unit of a side that has one
            us = [u for u in m.units if u]
            if us and len(set(us)) == 1:
                m.units = [u or us[0] for u in m.units]
    return res


# ---------------------------------------------------------------- roles

_ROLE_RIGHT = [
    ("thick", re.compile(r"^\W{0,3}(?:total\s+)?(?:layer\s+)?(thi(?:ck\w*|c\w*|e\W?)|thk\b)|"
                         r"^[^()]{0,12}\blayer\b|^\W{0,3}(?:total\s+)?layer", I)),
    ("depth", re.compile(r"^[\s\W]{0,3}(?:in\s+)?(deep|depths?|dp)\b", I)),
    ("height", re.compile(r"^[\s\W]{0,3}(?:in\s+|average\s+|avg\.?\s+|max\.?\s+|min\.?\s+)?"
                          r"(high|tall|heights?|ht|hgt)\b|^[\s,]{0,3}measured\s+from", I)),
    ("width", re.compile(r"^[\s\W]{0,3}(wide|width|w)\b", I)),
    ("length", re.compile(r"^[\s\W]{0,3}(long|length|l)\b", I)),
    ("dia", re.compile(r"^[\s\W]{0,3}(?:avg\.?\s+|average\s+|max\.?\s+|maximum\s+|min\.?\s+|"
                       r"minimum\s+|nominal\s+)?(dia\w*|diam\w*)\b", I)),
    ("stone", re.compile(r"^[\s\W]{0,3}(?:avg\.?\s+|average\s+|max\.?\s+|maximum\s+)?"
                         r"(?:dia\w*\.?\s+)?(rock|stone|boulder)s?\b(?!\s*(?:rip\s*-?\s*rap|"
                         r"riprap|protection|common|type|ty\b))", I)),
]
_ROLE_LEFT = [
    ("thick", re.compile(r"\bthickness\s*(?:of|=|:)?\s*$", I)),
    ("depth", re.compile(r"\b(?:depth|deep)\s*(?:of|=|:)?\s*$", I)),
    ("height", re.compile(r"\b(?:height|ht|hw|h)\s*(?:of|=|:)?\s*$", I)),
    ("dia", re.compile(r"\b(?:dia\w*|diam\w*)\s*(?:of|=|:)?\s*$", I)),
    ("d50", re.compile(r"\bd\s?50\s*(?:=|:)?\s*$", I)),
    ("width", re.compile(r"\bwidth\s*(?:of|=|:)?\s*$", I)),
]
_QUAL_HI_L = re.compile(r"(up\s*to|upto|less\s+than|under|below|not\s+to\s+exceed|max(?:imum)?\.?|"
                        r"<=?|≤)\s*(?:a\s+depth\s+of\s+|a\s+height\s+of\s+)?$", I)
_QUAL_LO_L = re.compile(r"(greater\s+than(?:\s+or\s+equal(?:\s+to)?)?|more\s+than|over|"
                        r"min(?:imum)?\.?|>=?|≥|at\s+least|exceeding|above)\s*$", I)
_QUAL_HI_R = re.compile(r"^[^()]{0,25}\b(or\s+less|or\s+shorter|or\s+smaller|or\s+under|max)\b", I)
_QUAL_LO_R = re.compile(r"^\s*(\+|or\s+more|or\s+greater|or\s+larger|or\s+deeper|and\s+(?:over|up|"
                        r"greater|larger|deeper))", I)
_QUAL_AVG = re.compile(r"(avg\.?|average)\s*$|^\s*(avg\.?|average)\b", I)

# words naming a SECONDARY element: a number inside a clause that names one
# of these describes that element, not the pay item ("Concrete Riprap (5"
# Thick) (2' Toe)", "flume WITH 6" CURBS", "including 20-inch Steel Casing")
_SECONDARY = re.compile(
    r"\b(toe\s*-?\s*walls?|toewalls?|to\s?ewall|toe|curbs?|collars?|bands?|footings?|"
    r"caps?\b|casing|encasement|weep|plates?|risers?|riser|rails?|railing|fence|posts?|piers?|"
    r"cut\s*-?\s*off|key|lugs?|dissipat\w*|bedding|subgrade|base|foundation|footer)\b", I)
# "(up to 48-inch measured from top of curb)" names the curb as the DATUM of
# the wall height, not as a second element
_DATUM = re.compile(r"\bmeasured\s+from\b|\bfrom\s+(?:the\s+)?top\s+of\b", I)


def _paren_span(t, pos):
    """(start, end) of the innermost parenthesis group around pos, or None."""
    depth, start = 0, None
    for i in range(pos - 1, -1, -1):
        c = t[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    depth = 0
    for i in range(pos, len(t)):
        c = t[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return (start, i + 1)
            depth -= 1
    return (start, len(t))


_CLAUSE_START = re.compile(r"(,|;|\bwith\b|\bw/|\bincluding\b|\bincl\.?\b|\binc\.\s)", I)


def _clause(t, s, e):
    """The comma / with / including clause that holds [s, e)."""
    lo = 0
    for m in _CLAUSE_START.finditer(t, 0, s):
        lo = m.start()
    m2 = re.compile(r"(,|;|\bwith\b|\bw/|\bincluding\b|\(|\))", I).search(t, e)
    hi = m2.start() if m2 else len(t)
    return t[lo:hi], lo


def annotate(t, ms, own=None):
    """Give each mention its role words, qualifier, and secondary flag.

    `own` is a regex of the words naming the family's OWN element: in the
    toe-wall family "toe wall" is the pay item, not a secondary element.
    """
    for k, m in enumerate(ms):
        nxt = ms[k + 1].s if k + 1 < len(ms) else len(t)
        prv = ms[k - 1].e if k > 0 else 0
        right = t[m.e:min(nxt, m.e + 40)]
        left = t[max(prv, m.s - 40):m.s]
        for name, rx in _ROLE_RIGHT:
            if rx.search(right):
                m.role.add(name)
        for name, rx in _ROLE_LEFT:
            if rx.search(left):
                m.role.add(name)
        if _QUAL_HI_L.search(left[-25:]) or _QUAL_HI_R.search(right):
            m.qual = "max"
        elif _QUAL_LO_L.search(left[-25:]) or _QUAL_LO_R.search(right):
            m.qual = "min"
        elif _QUAL_AVG.search(left[-12:]) or _QUAL_AVG.search(right[:10]):
            m.qual = "avg"
        ps = _paren_span(t, m.s)
        m.paren = ps is not None
        scope = t[ps[0]:ps[1]] if ps else _clause(t, m.s, m.e)[0]
        if not ps:
            cl, lo = _clause(t, m.s, m.e)
            # only a clause OPENED by with/including/comma marks a secondary
            # element; the head clause of the description is the pay item
            scope = cl if lo > 0 else ""
        sec = _SECONDARY.search(scope)
        if sec and not (own is not None and own.search(sec.group(0))) \
                and not _DATUM.search(scope):
            m.secondary = True
    return ms


# ---------------------------------------------------------------- families

_REMOVAL = re.compile(r"\b(remov\w*|demolish\w*|demo\b|dispos\w*|abandon\w*|salvag\w*|"
                      r"relocat\w*)\b", I)
_BUILD = re.compile(r"\b(replac\w*|reconstruct\w*|install\w*|construct\w*|furnish\w*|"
                    r"new|r\s*&\s*r|(?<!in\s)place\w*|build|extend\w*)\b", I)

_BOX = re.compile(r"\b(rcbc?|cbc|mbc|box\s*culv\w*|concrete\s+box|reinforced\s+box|"
                  r"precast\s+box|box\s+(?:storm|section)|conc\s+box)\b|\bboxes\b.{0,15}culvert", I)
_PIPE = re.compile(r"\b(rcp|r\.\s?c\.\s?p|rc\s+pipe|rccp|reinforced\s+concrete\s+(?:storm\s+)?"
                   r"(?:pipe|culvert\s+pipe|storm\s+(?:drain|sewer))|reinf\w*\.?\s+conc\w*\.?\s+"
                   r"(?:pipe|storm)|concrete\s+pipe|hdpe|n-?12|polypropylene|polypopylene|"
                   r"hp[\s-]?storm|ads\b|cmp\b|corrugated\s+(?:metal|steel|polyethylene|"
                   r"polypropylene|pipe)|circular\s+culverts?|storm\s+(?:sewer|drain|pipe|conduit)"
                   r"|culvert\s+pipe|pipe\s+culvert|cipp|cured\s+in\s+place\s+pipe|"
                   r"(?:pvc|hdpe|steel|ductile|dip|vcp)\s+(?:storm\s+)?pipe|drain\s*pipe|"
                   r"slotted\s+drain|underdrain|pipe)\b", I)
# a pipe word that is a storm-drain product by itself, with no "storm" needed:
# "RC PIPE (CL IV) (36 IN)", "JACK BOR OR TUN PIPE(27 IN)(RC)(CL III)"
_STORM_STRONG = re.compile(r"\b(rcp|r\.\s?c\.\s?p|rc\s+pipe|reinforced\s+concrete\s+(?:storm\s+)?"
                           r"(?:pipe|culvert)|reinf\w*\.?\s+conc\w*\.?\s+pipe|hdpe|n-?12|"
                           r"polypropylene|polypopylene|hp[\s-]?storm|ads|cmp|corrugated|"
                           r"circular\s+culverts?|cipp)\b|\(rc\)", I)
_SAN_WAT = re.compile(r"\b(sanitary|sewer\s+(?:line|main|pipe)|wastewater|waste\s+water|water\s+"
                      r"(?:line|main|pipe)|force\s+main|ductile\s+iron|c-?900|c-?905|bar[\s-]?"
                      r"wrapped|rccp|potable|fire\s+line|irrigation|gas\s+line|ss|vcp|vitrified)\b|"
                      r"(?<!storm\s)(?<!storm-)\bwater\b", I)
# words that make a pipe-looking line something else.  Measured in the
# public municipal rows: storm_pipe held "FL BS (CMP IN PLACE)" (TxDOT flex
# base, "compacted in place"), "INL(CMP)" (an inlet, "complete"), HDPE
# communication conduit, "4-Foot Polypropylene Orange Safety Fencing", a
# "Tree Root Control Barrier (Polypropylene)" and SETP-CD end treatments.
_NOT_PIPE = re.compile(r"\b(fl\s+bs|flex\w*\s+base|inl\s*\(?cmp|fenc\w*|barriers?|spork|ducts?|"
                       r"conduits?|condt|innerducts?|communication|its|fiber|electric\w*|"
                       r"trench\s+(?:repair|protection|safety|excavation\s+protection|jacks?|"
                       r"box\w*)|shoring|pavement\s+repair|asphalt|pothole\w*|"
                       r"manholes?|inlets?|junction\s+box\w*|vaults?|headwalls?|hdwl|wingwalls?|"
                       r"wing\s+walls?|sets?|setp\S*|setb\S*|pset\S*|safety\s+end|sloped?\s+end|"
                       r"end\s+treatments?|meters?|valves?|hydrants?|services?|laterals?|sleeves?|"
                       r"signals?|luminaires?|poles?|signs?|sod|seed\w*|structural\s+excavation|"
                       r"footings?|abutments?)\b|^\W*(?:[\w\-\"']+\s+){0,4}casing\b", I)
_CHANNEL = re.compile(r"\b(flumes?|channel\s*(?:lining|liner|bottom|invert|slope|paving|/\s*slope)|"
                      r"lined\s+channel|trapezoidal\s+channel|concrete\s+channel|conc\w*\.?\s+channel|"
                      r"pilot\s+channel|channel\s+riprap|slope\s+paving)\b|"
                      r"\b\d+\W{0,3}(?:in|inch|\")\W{0,3}(?:thick\s+)?(?:reinf\w*\.?\s+)?"
                      r"(?:conc\w*\.?\s+)?channel\b", I)
_TOE = re.compile(r"\b(toe\s*-?\s*walls?|toewalls?|cut\s*-?\s*off\s+walls?|cutoff\s+walls?|"
                  r"grade\s+control\s+(?:walls?|structures?)|concrete\s+beams?|tie\s*-?\s*back\s+"
                  r"beams?|tied-back\s+beams?|anchor\s+beams?|concrete\s+toe\b|beam\s+and\s+strut)", I)
_WALL = re.compile(r"\b(retaining\s+walls?|retain\s+wall|modular\s+block\s+(?:retaining\s+)?walls?|"
                   r"segment\w*\s+(?:block\s+)?(?:gravity\s+)?(?:retaining\s+)?walls?|mse\s+walls?|"
                   r"\(mse\)|keystone\s+block|gravity\s+walls?|filled\s+bag\s+retaining)\b", I)
_WALL_NOT = re.compile(r"\b(shade\s+structure|wing\s+bracket|sidewalks?\s+for\s+retaining|shoring|"
                       r"(?:backfill|sod|embankment|veneer|cap\s*stone|form\s*liner|weep\s+holes?|"
                       r"rail\w*|handrail|footing|paint|bracing|landscape\s+wall|fence)\b.{0,60}"
                       r"retaining|retaining\s+walls?\s+(?:backfill|form\s*liner)|additional\s+cost)",
                       I)
_EXCAV_HEAD = re.compile(r"^\W*(?:[\w&/.'-]+\s+){0,5}?(?:excavat\w*|excav\b|dredg\w*|"
                         r"mass\s+grading)", I)
_EXCAV_NOT = re.compile(r"\b(protection|(?<!trench\s)safety|pothole|exploratory|vacu?um|hydro|hyd\b|"
                        r"backhoe|excavators?|survey\w*|excavated\s+(?:fill|native|material|wall|"
                        r"areas?)|adder|piers?|pits?|per\s+inch|conduits?)\b|install\w*\s+.{0,40}"
                        r"(?:sidewalk|flex\s*base|pavement)", I)
_DEWATER = re.compile(r"\b(dewater\w*|control\s+of\s+water|care\s+of\s+water|cofferdams?|coffer\s+dams?|"
                      r"(?:stream|creek|channel|water)\s+diversion|diversion\s+(?:channel|dam|"
                      r"pipe|berm)|temporary\s+creek\s+crossing|creek\s+bypass|stream\s+bypass)\b", I)
_TRM = re.compile(r"\b(turf\s+reinforc\w*|hp-?trm|trm\b|pyramat|landlok|vmax|p550|armormax|"
                  r"reinforced\s+vegetation)\b", I)
_HEAD_END = re.compile(r"\b(?:this\s+item\s+includes|including|includes|incl\b\.?|inc\.|with|w/|"
                       r"in\s+accordance)|;|:\s", I)
_GARBLE = re.compile(r"^\W*(utility\s+markings?|tree\s+protection|contingency|allowance|"
                     r"traffic|barricade|signs?)\b", I)


def head(low):
    """The pay item's own words: the text before with / including / a comma.

    "Rock Swale, including rock rip rap" is a swale; "10" PAVEMENT PANEL
    REPLACEMENT (... INCLUDES REMOVAL AND EXCAVATION)" is pavement.  The
    family keyword has to be in the head, not in the list of what the price
    covers.
    """
    m = _HEAD_END.search(low)
    h = low[:m.start()] if m else low
    if len(h.strip()) < 4:            # "- Removals - ..." style leaders
        return low
    return h


def spec_family(desc, item_family=None):
    """Which spec family a description belongs to, or None.

    item_family (parse_bidtabs' classifier) is used where it is reliable --
    removal rows, TRM products, the SET/headwall split -- and the TEXT
    decides the rest, because the classifier misses the client's items in
    measurable ways in the public municipal rows:
      * "Reinforced Concrete Pipe (48") (Class III)" is concrete_misc, and
        "9'x6' RCB", "18" ADS HDPE Pipe (N-12)" are `other`: no \\brcp\\b.
      * storm_pipe holds "FL BS (CMP IN PLACE)" -- TxDOT flexible base,
        "compacted in place" -- and "INL(CMP)": CMP is not a pipe there.
      * "TRENCH EXCAVATION PROTECTION" is `excavation`; it is trench safety.
      * box culverts sit inside storm_pipe; they are a different product,
        priced by span x rise, not diameter.
      * conc_beam pools flumes, channel bottoms and toe walls.
    The family keyword must sit in the item's head clause (see `head`).
    """
    t = normalize(desc)
    low = t.lower()
    fam = (item_family or "").strip()
    if not low:
        return None
    if fam in ("trm", "trm_permanent", "trm_temporary", "trm_bundled", "arv_system"):
        return "trm"
    if fam.startswith("demo_") or fam in ("construction_exit_remove", "safety_end_treatment",
                                          "grout_additive", "rock_anchor"):
        return None
    hd = head(low)
    if _GARBLE.search(hd):
        return None
    excav_head = bool(_EXCAV_HEAD.search(hd))
    removal = bool(_REMOVAL.search(low)) and not _BUILD.search(low)
    if removal and not excav_head:
        return None
    if re.search(r"\bsalvag\w*", hd):
        return None
    if excav_head and re.search(r"^\W*(?:\w+\s+){0,2}dredg", hd):
        return "excavation"
    if _DEWATER.search(hd) and not re.search(r"\b(sewer|sanitary|wastewater|manhole)\b", low):
        return "dewatering_diversion"
    if fam == "dewatering":
        return "dewatering_diversion"
    if fam == "trench_safety" and not (excav_head and not _EXCAV_NOT.search(hd)):
        return None
    if _TRM.search(hd):
        return "trm"
    # ---- gabions
    if "gabion" in hd:
        if re.search(r"gabion\s+(?:rock|stone|fill)\b", hd):
            return None
        if re.search(r"\b(tie\s*-?\s*backs?|anchors?|beams?|excavation\s+safety|grout\s+for)\b",
                     hd) or re.search(r"\b(headwall|junction\s+box)\b.*gabion", hd):
            if re.search(r"\bbeams?\b", hd):
                return "toe_wall_beam"
            return None
        if re.search(r"gabion.{0,40}\bmat(?:s|t?ress\w*|tres\w*)?\b|\bmat(?:s|t?ress\w*|tres\w*)?"
                     r"\b.{0,30}gabion|reno\s+mattress|gabion.{0,20}revetment", hd):
            return "gabion_mattress"
        return "gabion_basket"
    # ---- toe walls named before riprap: "INSTALL 8 IN WIDE X 4 FT DEEP
    # REINF. CONCRETE TOE WALLS AT GROUTED ROCK RIPRAP" is a toe wall
    rr = re.search(r"\brip\s*-?\s*rap|\briprap|\brock\s+rip", hd)
    toe = _TOE.search(hd)
    if toe and (not rr or toe.start() < rr.start()):
        if not re.search(r"\b(steel\s+plate|prestressed|tx\d\d|bridge)\b", hd):
            if re.search(r"\bflume|channel\s+(?:lining|bottom)", hd) and \
                    re.search(r"\bflume|channel", hd).start() < toe.start():
                return "channel_lining"
            return "toe_wall_beam"
    # ---- riprap
    hw_first = re.search(r"\bhead\s*walls?\b|\bwing\s*walls?\b|\bhdwl\b", hd)
    if rr and hw_first and hw_first.start() < rr.start():
        rr = None                       # "Concrete Headwall (...)(5" Concrete RipRap"
    if rr:
        # riprap named as an ACCESSORY of another pay item, or not riprap:
        # "Permanent Riprap Sediment Trap", "Inlet (Complete) ... With
        # Rip-Rap Apron", "RIPRAP (MOW STRIP)", "Riprap Bedding Material",
        # "PERMANENT ROAD CLOSED BARRICADE WITH ROCK RIPRAP (8")"
        if re.search(r"sediment\s+trap|mow\s*strip|grout\s+for|bedding|\bjoints?\b|barricade|"
                     r"\binlet\b", low[:rr.end() + 40]):
            return None
        conc = re.search(r"(concrete|conc\b|conc\.|cl\.?\s*[abc]\s+conc\w*|\brc\b)\W{0,4}"
                         r"(?:\w+\W{1,3}){0,2}rip\s*-?\s*rap|rip\s*-?\s*rap\W{0,4}(?:\(\s*)?"
                         r"(concrete|conc\b|conc\.|cl\.?\s*[abc]\b)|slope\s+paving|riprap\s+pavement|"
                         r"median\s+(?:and\s+)?rip\s*-?\s*rap", hd)
        stone = re.search(r"(rock|stone|boulder|limestone)\W{0,3}(?:\w+\W{1,3}){0,2}rip\s*-?\s*rap|"
                          r"rip\s*-?\s*rap\W{0,4}(?:\(\s*)?(stone|rock)|stone\s+protection|"
                          r"protection\s+stone|\bd\s?50\b", hd)
        conc_head = re.match(r"^\W*(?:\d+\S*\s+)?(?:reinforced\s+|reinf\.?\s+)?(concrete|conc)\b", hd)
        if (conc and not stone) or conc_head:
            return "riprap_conc"
        return "riprap_stone"
    # ---- walls
    hwm = re.search(r"\bhead\s*walls?\b|\bwing\s*walls?\b|\bhdwl\b", hd)
    wall = _WALL.search(hd)
    chan = _CHANNEL.search(hd)
    if hwm and not (chan and chan.start() < hwm.start()) and \
            not re.search(r"\b(bridge|abutments?)\b", low) and \
            not re.search(r"^\W*(?:[\w&,]+\s+){0,3}(veneer|stone\s+cap|rail\w*|fence|clean\w*|"
                          r"repair\w*|connect\w*|wrought)\b", hd):
        return "headwall_wingwall"
    if (wall or fam == "retaining_wall") and not (chan and wall and chan.start() < wall.start()):
        if _WALL_NOT.search(low) or not re.search(r"\bwalls?\b", low):
            return None
        return "retaining_wall"
    # ---- concrete channel lining
    if chan:
        if re.search(r"\b(steel\s+plate|plate\s+-|sod\b|seed|curlex|gravel|prestressed|tx\d\d|"
                     r"bridge|swppp)\b", hd):
            return None
        # "6-inch monolithic curb on street, alley, sidewalk, or channel
        # paving section"; "Compacted Earth Slope Fill ... for flumes"
        if re.search(r"^\W*(?:[\w\-\"',]+\s+){0,5}?(curb|fill|sidewalk|cleaning|street|saw\s*-?\s*cut\w*)"
                     r"\b", hd):
            return None
        return "channel_lining"
    # ---- pipes and boxes
    box = _BOX.search(hd)
    pipe = _PIPE.search(hd)
    sw = _SAN_WAT.search(hd)
    if (box or pipe) and (
            re.search(r"^\W*(?:\w+\s+){0,2}(backfill|structural\s+excavation|excavation|"
                      r"junction\s+box)", hd) or
            re.search(r"\b(encasement|casing)\s+(?:pipe|of|for)\b|^\W*(?:\S+\s+){0,3}"
                      r"(?:steel\s+|concrete\s+)?encasement\b|\bpipe\s+fittings\b", hd)):
        return None
    if (box or pipe) and not _NOT_PIPE.search(hd):
        first = min([x.start() for x in (box, pipe) if x])
        if re.search(r"\bsewer\b", hd) and not re.search(r"\bstorm\b", hd) and \
                not (box and box.start() <= first):
            return "water_sewer_pipe"
        if fam in ("sanitary_pipe", "water_pipe") or (sw and sw.start() <= first and
                                                      not re.search(r"\bstorm\b", hd)):
            return "water_sewer_pipe"
        if box and (not pipe or box.start() <= pipe.start()):
            return "box_culvert"
        if sw and not re.search(r"\bstorm\b|\bdrain", hd) and not _STORM_STRONG.search(hd):
            return "water_sewer_pipe"
        if _STORM_STRONG.search(low) or fam == "storm_pipe" or \
                re.search(r"\b(storm|drain\w*|culverts?|sewer)\b", low):
            return "storm_pipe"
        return None
    sized = re.search(r"\d\s*(?:\"|-?\s*in\b|-?\s*inch)", hd)
    if sized and not _NOT_PIPE.search(hd) and (
            fam in ("sanitary_pipe", "water_pipe") or
            (sw and re.search(r"\b(sewer|line|main|pipe)\b", hd)) or
            (re.search(r"\bsewer\b", hd) and not re.search(r"\bstorm\b", hd))):
        return "water_sewer_pipe"
    # ---- excavation
    if excav_head and not _EXCAV_NOT.search(hd) and not re.match(r"^\W*including\b", hd):
        return "excavation"
    return None


def element(desc, family):
    """Sub-type inside a pipe / box family: the run itself or an accessory.

    The price analysis compares RUNS of pipe (per LF); a connection, a plug,
    a wye, a TV inspection or a cleaning line is a different pay item even
    when it names the same diameter.  Read on the head clause, so "18" Class
    III RCP Storm Drain including CCTV Inspection" stays a pipe run.
    """
    low = normalize(desc).lower()
    if re.search(r"\b(delivery|deliver\w*\s+(?:of|to)|supply\s+only|materials?\s+only|"
                 r"stockpile)\b", low):
        return "supply"
    if family not in ("storm_pipe", "box_culvert", "water_sewer_pipe"):
        return "main"
    hd = head(low)
    core = hd.split(",")[0]
    if re.search(r"\b(pipe|line|main|sewer|rcp|culvert|drain|box|rcbc?)\b", core):
        hd = core
    if re.search(r"\b(transition|reducer)\b", hd):
        return "connect"
    if re.search(r"\b(clean\w*|inspect\w*|tv\b|televis\w*|cctv|video|point\s+repair|repair\w*|"
                 r"joints?|seal\w*|cipp|liner|lining|rehab\w*|jet\w*|grout\w*|cleanout)\b", hd):
        return "service"
    if re.search(r"\b(connect\w*|tie[\s-]?in|plugs?|cap\b|wyes?|tees?|collars?|bands?|"
                 r"risers?|end\s+cap|closure|stub\w*|outfall\s+gate|gate|"
                 r"manufactured\s+plug|access|fittings?|adapter|line\s+stops?|restraint)\b", hd) \
            or re.search(r"^\W*(?:\S+\s+){0,2}bends?\b", hd):
        return "connect"
    if re.search(r"^\W*(?:[\w-]+\s+){0,6}?(trench\w*|embedment|backfill\w*)\b", hd) and not \
            re.search(r"\b(pipe|rcp|hdpe|pvc)\b.{0,30}\b(trench\w*|embedment|backfill\w*)", hd) \
            or re.search(r"^\W*(?:\S+\s+){0,3}(trenching|embedment|backfill\w*)", hd):
        return "embedment"
    if re.search(r"\b(remove\s+(?:and|&)\s+replace|r\s*&\s*r|replace\w*|reset)\b", hd):
        return "rr"
    if re.search(r"\bsupplied\s+by\s+(?:owner|city)\b", low):
        return "install_only"
    return "main"


# ---------------------------------------------------------------- extractors

def _plaus(v, lo, hi):
    return v is not None and lo <= v <= hi


def _first(ms, pred):
    for m in ms:
        if pred(m):
            return m
    return None


def _range_vals(m, conv):
    """(lo, hi, point) of a mention after conversion, honouring qualifiers."""
    if m.kind == "range":
        a, b = conv(m, 0), conv(m, 1)
        if a is None or b is None:
            return None
        lo, hi = min(a, b), max(a, b)
        if m.qual == "max":
            return (lo, hi, hi)
        return (lo, hi, (lo + hi) / 2.0)
    v = conv(m, 0)
    if v is None:
        return None
    if m.qual == "max":
        return (None, v, v)
    if m.qual == "min":
        return (v, None, v)
    return (v, v, v)


def _ft(m, i):
    return m.to_ft(i)


def _in(m, i):
    return m.to_in(i)


_TXDOT_PAREN = re.compile(r"^\(\s*" + NUM + r"\s*(?:\"|in\b\.?|inch(?:es)?\b)\s*\)$", I)


def _lone_paren(t, m):
    """True if the mention is the only thing inside its parentheses: (18 IN)."""
    ps = _paren_span(t, m.s)
    if not ps:
        return False
    return bool(_TXDOT_PAREN.match(t[ps[0]:ps[1]].strip()))


def _grout(low):
    if re.search(r"\bungrout\w*|\bdry\b|\bloose\b|\bvoid\s+fil", low):
        return "dry"
    if re.search(r"\bgrout\w*", low):
        return "grouted"
    return None


def _coating(low):
    out = []
    if re.search(r"\bpvc\b|poly\w*\s*vinyl", low):
        out.append("pvc")
    if re.search(r"\bgalv\w*|galfan|zinc", low):
        out.append("galvanized")
    return "+".join(out) or None


_RR_CLASS = [
    (re.compile(r"\b(?:ty|type|tye)\.?\s*['\"]?\s*([rfab])\b['\"]?", I), "type %s"),
    (re.compile(r"\bclass\s*['\"]?\s*([ab1-4])\b['\"]?\s*(?:grouted|rock|stone|rip)", I), "class %s"),
    (re.compile(r"\bstone\s+common\b|\bcommon\s+(?:dry\s+)?stone\b|\(stone\s+common\)|"
                r"angular\s+stone\s+common", I), "common"),
    (re.compile(r"\bstone\s+protection\b|\bprotection\s+stone\b|\(protection\)|"
                r"protective\s+riprap|protection\s+riprap", I), "protection"),
]


def _rr_class(t):
    out = []
    for rx, fmt in _RR_CLASS:
        m = rx.search(t)
        if m:
            out.append(fmt % m.group(1).upper() if "%s" in fmt else fmt)
    return "; ".join(out) or None


def _pipe_material(low):
    mats = []
    pats = [
        ("RCP", r"\brcp\b|r\.\s?c\.\s?p\.?|\brc\s+pipe\b|reinforced\s+concrete\s+(?:storm\s+)?"
                r"(?:pipe|culvert\s+pipe|storm\s+(?:drain|sewer))|reinf\w*\.?\s+conc\w*\.?\s+"
                r"(?:pipe|storm)|\(rc\)|\brcp/"),
        ("BWCCP", r"bar[\s-]?wrapped|\brccp\b|concrete\s+cylinder"),
        ("PP", r"polypropylene|polypopylene|\bpp\b|hp[\s-]?storm|\bads\s+hp\b|\bhp\s+storm"),
        ("HDPE", r"\bhdpe\b|\bn-?12\b|corrugated\s+polyethylene|\bcpep\b|polyethylene|\bads\b"
                 r"(?!\s+hp)"),
        # SDR alone names no material: HDPE is sold by SDR too ("SDR-11 ...
        # HDPE DUCT", "HDPE SDR-9" water services)
        ("PVC", r"\bpvc\b|\bc-?900\b|\bc-?905\b|\bf679\b|\bd3034\b|\bfusible\b"),
        ("DIP", r"ductile\s+iron|\bdip\b|\bd\.i\.p|\bd\.?i\.?\s+(?:water|pipe|sewer|line|main)"),
        ("CMP", r"\bcmp\b|corrugated\s+(?:metal|steel)|\bcsp\b|galvanized\s+steel\s+pipe|"
                r"circular\s+culverts?\b.*\bgauge"),
        ("VCP", r"\bvcp\b|vitrified\s+clay"),
        ("CIPP", r"\bcipp\b|cured[\s-]+in[\s-]+place\s+pipe"),
        ("steel", r"\bsteel\s+(?:pipe|casing)\b"),
        ("concrete", r"\bconcrete\s+pipe\b"),
    ]
    for name, p in pats:
        m = re.search(p, low)
        if m:
            mats.append((m.start(), name))
    mats.sort()
    names = []
    for _, n in mats:
        if n not in names:
            names.append(n)
    if "RCP" in names and "concrete" in names:
        names.remove("concrete")
    return names


_ROMAN = {"i": "I", "ii": "II", "iii": "III", "iv": "IV", "v": "V", "1": "I", "2": "II",
          "3": "III", "4": "IV", "5": "V"}


def _pipe_class(low):
    m = re.search(r"\b(?:class|cl)\s*\.?\s*-?\s*(iii|iv|ii|v|3|4|5)\b(?!\s*(?:concrete|conc))",
                  low)
    if m:
        return _ROMAN[m.group(1)]
    m = re.search(r"\b(?:sdr|dr)\s*-?\s*(\d{1,2}(?:\.\d)?)\b", low)
    if m:
        return "SDR" + m.group(1) if "sdr" in m.group(0) else "DR" + m.group(1)
    m = re.search(r"\bsch(?:edule)?\.?\s*(\d{2})\b", low)
    if m:
        return "SCH" + m.group(1)
    m = re.search(r"\bps\s*-?\s*(\d{2,3})\b", low)
    if m:
        return "PS" + m.group(1)
    return None


_COUNT_WORDS = {"single": 1, "double": 2, "dual": 2, "twin": 2, "triple": 3,
                "quadruple": 4, "quad": 4, "quintuple": 5}


def _barrels(t, low, m=None):
    if m is not None and m.count:
        return m.count
    w = re.search(r"\b(single|double|dual|twin|triple|quadruple|quad|quintuple)\b", low)
    if w:
        return _COUNT_WORDS[w.group(1)]
    return None


def _count_prefix(t, m):
    """'4-6'x4'', '3-10' x 5'', '2-18"', '(2) 18"' -> barrel count."""
    left = t[max(0, m.s - 6):m.s]
    c = re.search(r"(?:^|[^\d.\w'\"])(\d{1,2})\s*-\s*$|\((\d)\)\s*$|(?:^|\s)(\d)\s*@\s*$", left)
    if c:
        n = int(next(g for g in c.groups() if g))
        if 1 <= n <= 12:
            return n
    return None


def _depth(ms, family):
    """Excavation / trench depth in ft, from mentions with a depth role."""
    m = _first(ms, lambda x: "depth" in x.role and not x.secondary
               and x.kind in ("single", "range"))
    if m is None:
        return None
    r = _range_vals(m, _ft)
    if r is None or not _plaus(r[2], 0.0, 60.0):
        return None
    return r


def _diameter(t, prim, lo=4, hi=144):
    """((lo, hi, point) in inches, barrel count) of the first pipe size.

    Order: a size the text calls a diameter, or the TxDOT-style lone
    "(18 IN)"; else the first inch length that no other role claims.  A
    "2-18"" in front of a pipe or headwall is TWO 18-inch barrels, not a
    2-to-18-inch range: a bare small integer, then a much larger size.
    """
    bare = re.match(r"^\W*(\d{1,3})\s+(?:pvc|rcp|hdpe|dip|di|c-?900|sdr|vcp|cmp|ads)\b", t, I)
    if bare and _plaus(float(bare.group(1)), lo, hi):
        # "8 PVC SDR-35 Wastewater Pipe": a pipe size written without its
        # inch mark, at the head of the item -- nominal pipe sizes are inches
        v = float(bare.group(1))
        return (v, v, v), None
    m = _first(prim, lambda x: x.kind == "single" and ("dia" in x.role or _lone_paren(t, x))
               and x.to_in() is not None)
    if m is None:
        m = _first(prim, lambda x: x.kind in ("single", "range") and x.unit() == "in"
                   and not (x.role & {"thick", "width", "length", "depth", "height"}))
    if m is None:
        p = _first(prim, lambda x: x.kind == "pair" and x.unit() == "in"
                   and _plaus(x.vals[0], lo, hi) and _plaus(x.vals[1], lo, hi))
        if p is not None:
            return (p.vals[0], p.vals[0], p.vals[0]), None
        return None, None
    if m.kind == "range":
        lo_has_unit = re.match(r"^\s*" + NUM + IN_U, m.text, I) is not None
        a, b = m.vals
        if not lo_has_unit and 1 <= a <= 6 and b >= 12 and a == int(a) and a * 2 <= b:
            return ((b, b, b) if _plaus(b, lo, hi) else None), int(a)
        r = _range_vals(m, _in)
        if r and _plaus(r[0], lo, hi) and _plaus(r[1], lo, hi):
            return r, None
        return None, None
    r = _range_vals(m, _in)
    if r and _plaus(r[2], lo, hi):
        return r, _count_prefix(t, m)
    return None, None


_HW_EQ = re.compile(r"\b(hw|h)\s*=\s*(" + NUM + r")\s*(-?\s*ft\b|'|\"|-?\s*in\b)?", I)


def extract(desc, family=None, item_family=None):
    """All specs the text supports, as a flat dict of None-or-value.

    family: a spec family (see spec_family); computed if not given.
    """
    t = normalize(desc)
    low = t.lower()
    fam = family if family is not None else spec_family(desc, item_family)
    out = {"spec_family": fam}
    if not fam or not t:
        return out
    own = {
        "toe_wall_beam": re.compile(r"toe|cut\s*-?\s*off|beam", I),
        "headwall_wingwall": re.compile(r"wing|head|toe", I),
        "channel_lining": re.compile(r"flume|channel", I),
        "box_culvert": re.compile(r"$^"),
    }.get(fam)
    ms = annotate(t, mentions(t), own)
    prim = [m for m in ms if not m.secondary]

    # ------------------------------------------------ heights (walls)
    if fam in ("retaining_wall", "headwall_wingwall", "gabion_basket", "toe_wall_beam"):
        h = None
        hw = _HW_EQ.search(t)
        if hw:
            v = _num(hw.group(2))
            u = (hw.group(3) or "'").strip().lower()
            v = v / 12.0 if (u.endswith("in") or u == '"') else v
            if _plaus(v, 0.5, 40):
                h = (v, v, v)
        if h is None:
            m = _first(prim, lambda x: "height" in x.role and x.kind in ("single", "range"))
            if m is None and fam == "toe_wall_beam":
                m = _first(prim, lambda x: "depth" in x.role and x.kind in ("single", "range"))
                if m is None:
                    # "4' X 8" Toe Walls": the foot side of a mixed pair is the depth
                    p = _first(prim, lambda x: x.kind == "pair" and set(x.units) == {"ft", "in"})
                    if p is not None:
                        k = p.units.index("ft")
                        h = (p.vals[k], p.vals[k], p.vals[k])
                    # "Toe Wall (3 Ft x 3 Ft)", "18" X 18" beam": square section;
                    # an unequal pair does not say which side is the depth
                    p = _first(prim, lambda x: x.kind == "pair" and len(set(x.units)) == 1
                               and x.unit() in ("ft", "in") and x.vals[0] == x.vals[1])
                    if h is None and p is not None:
                        v = p.to_ft(0)
                        h = (v, v, v)
            if m is None and fam == "retaining_wall":
                # an unlabeled RANGE on a wall is its height class:
                # "(0-1.5 ft)", "(2'-6')", "(8-in to 24-in)"; so is a bound,
                # "Retaining Wall (up to 48-inch)"
                m = _first(prim, lambda x: (x.kind == "range" or x.qual in ("max", "min"))
                           and not (x.role - {"height"}))
            if m is not None and h is None:
                h = _range_vals(m, _ft)
        if h is not None and (h[2] is None or not _plaus(h[2], 0.0, 40.0)):
            h = None
        if h is not None:
            out["height_ft_lo"], out["height_ft_hi"], out["height_ft"] = h
        if re.search(r"\bvariable\s+height|\bheight\s+var\w*|\bvar\w*\s+ht\b", low):
            out["height_note"] = "variable"

    # ------------------------------------------------ gabions
    if fam in ("gabion_basket", "gabion_mattress"):
        out["coating"] = _coating(low)
        dims = _first(prim, lambda x: x.kind in ("pair", "triple"))
        if dims is not None:
            us = set(u for u in dims.units if u)
            vals = dims.vals
            if us <= {"ft"} and all(_plaus(v, 0.25, 15) for v in vals):
                vs = sorted(vals)
                if dims.kind == "triple":
                    out["gab_len_ft"], out["gab_wid_ft"], out["gab_ht_ft"] = vs[2], vs[1], vs[0]
                else:
                    out["gab_wid_ft"], out["gab_ht_ft"] = vs[1], vs[0]
                if fam == "gabion_mattress":
                    out["thick_in"] = vs[0] * 12.0
        if out.get("thick_in") is None:
            m = _first(prim, lambda x: x.kind == "single" and x.unit() == "in"
                       and not (x.role & {"width", "length", "dia", "stone", "height"}))
            if m is not None and _plaus(m.vals[0], 4, 36):
                out["thick_in"] = m.vals[0]
            elif fam == "gabion_mattress":
                m = _first(prim, lambda x: x.kind == "single" and x.unit() == "ft"
                           and _plaus(x.vals[0], 0.5, 1.5))
                if m is not None:
                    out["thick_in"] = m.vals[0] * 12.0

    # ------------------------------------------------ riprap
    if fam == "riprap_stone":
        out["rr_class"] = _rr_class(t)
        out["rr_grout"] = _grout(low)
        thick, stone, d50, grad, nominal = None, None, None, None, None
        hw = re.search(r"\bd\s?50\s*[=:]?\s*(" + NUM + r")\s*(\"|in\b|inch\w*|'|ft\b)?", t, I)
        if hw:
            v = _num(hw.group(1))
            if hw.group(2) and hw.group(2).strip().lower() in ("'", "ft"):
                v = v * 12.0
            if _plaus(v, 1, 60):
                d50 = v
        # explicit words first: a stated thickness outranks the TxDOT
        # convention that a lone "(12 IN)" is the layer thickness --
        # 'TWELVE INCH (12") STONE TYPE 'B' ... 24" TOTAL LAYER THICKNESS'
        explicit = any(m.kind == "single" and m.role & {"thick", "depth"} for m in prim)
        for m in prim:
            if m.kind == "single":
                v = m.to_in()
                if v is None:
                    continue
                if m.role & {"thick", "depth"} or (_lone_paren(t, m) and not explicit):
                    if thick is None and _plaus(v, 2, 96):
                        thick = v
                elif "d50" in m.role:
                    continue
                elif m.role & {"stone", "dia"}:
                    if stone is None and _plaus(v, 1, 60):
                        stone = v
                        if m.qual == "avg" and d50 is None:
                            d50 = v
                elif not (m.role & {"width", "length", "height"}):
                    if nominal is None and _plaus(v, 2, 60):
                        nominal = v
            elif m.kind in ("range", "pair"):
                if m.role & {"thick", "depth"}:
                    r = _range_vals(m, _in)
                    if r and thick is None and _plaus(r[2], 2, 96):
                        thick = r[2]
                    continue
                if m.kind == "pair" and set(u for u in m.units if u) - {"in"}:
                    continue            # a 2' x 4' toe, not a gradation
                a = m.vals[0] if m.units[0] in ("in", None) else None
                b = m.vals[1] if m.units[1] in ("in", None) else None
                if a is not None and b is not None and grad is None and \
                        _plaus(a, 1, 60) and _plaus(b, 1, 60) and a < b:
                    grad = (a, b)
        if nominal is not None:
            if thick is not None and abs(nominal - thick) < 1e-6:
                nominal = None              # "30-inch Rock Riprap (2.5' Depth)"
            elif thick is not None and stone is None and grad is None and d50 is None:
                stone, nominal = nominal, None
            elif (stone is not None or grad is not None or d50 is not None) and thick is None:
                thick, nominal = nominal, None
        out["rr_thick_in"] = thick
        out["rr_stone_in"] = stone
        out["rr_d50_in"] = d50
        out["rr_grad_lo_in"], out["rr_grad_hi_in"] = grad if grad else (None, None)
        out["rr_nominal_in"] = nominal
        if re.search(r"\bvari\w*\s+depth|\bvaried\s+depth|\bvarif\w*\s+depth", low):
            out["thick_note"] = "variable"

    # ------------------------------------------------ concrete thickness
    if fam in ("riprap_conc", "channel_lining", "toe_wall_beam"):
        m = _first(prim, lambda x: x.kind == "single" and "thick" in x.role)
        if m is None and fam != "toe_wall_beam":
            m = _first(prim, lambda x: x.kind == "single" and x.unit() == "in"
                       and not (x.role & {"width", "length", "dia", "depth", "height", "stone"}))
        if m is not None:
            v = m.to_in()
            if _plaus(v, 2, 36):
                out["thick_in"] = v

    # ------------------------------------------------ pipes
    if fam in ("storm_pipe", "water_sewer_pipe"):
        mats = _pipe_material(low)
        out["material"] = mats[0] if mats else None
        out["material_all"] = "|".join(mats) if mats else None
        out["pipe_class"] = _pipe_class(low)
        dia, nb = _diameter(t, prim)
        if dia:
            out["dia_in_lo"], out["dia_in_hi"], out["dia_in"] = dia
        out["barrels"] = nb if nb else _barrels(t, low)
        d = _depth(prim, fam)
        if d:
            out["depth_ft_lo"], out["depth_ft_hi"], out["depth_ft"] = d
        elif re.search(r"\ball\s+depths?\b|regardless\s+of\s+depth|variable\s+depth", low):
            out["depth_note"] = "any"
        out["method"] = ("trenchless" if re.search(
            r"\b(bor\w*|jack\w*|tunnel\w*|trenchless|auger\w*|burst\w*|direction\w*\s+dril\w*|"
            r"microtunnel\w*|hdd|b\.?o\.?t\.?o\.?c\.?)\b|other\s+than\s+open\s*-?\s*cut", low) else
            "open_cut" if re.search(r"\bopen\s*-?\s*cut\b", low) else None)

    # ------------------------------------------------ boxes
    if fam == "box_culvert":
        out["pipe_class"] = _pipe_class(head(low))
        m = _first(prim, lambda x: x.kind == "pair")
        if m is None:
            # "PRECAST 12X12 BOX CULVERT": bare pair; mentions() keeps it
            pass
        if m is not None:
            a, b = m.vals
            ua = m.units[0] or m.unit()
            ub = m.units[1] or m.unit()
            if ua == "in" and ub == "in":
                a, b = (a / 12.0, b / 12.0) if (a >= 24 and b >= 24) else (None, None)
            elif ua == "in" or ub == "in":
                a = b = None
            if a is not None and _plaus(a, 2, 30) and _plaus(b, 1, 30):
                out["span_ft"], out["rise_ft"] = a, b
                out["barrels"] = m.count or _count_prefix(t, m) or _barrels(t, low)
        if out.get("barrels") is None:
            out["barrels"] = _barrels(t, low)
        d = _depth(prim, fam)
        if d:
            out["depth_ft_lo"], out["depth_ft_hi"], out["depth_ft"] = d

    # ------------------------------------------------ headwall: what it serves
    if fam == "headwall_wingwall":
        dia, nb = _diameter(t, prim, lo=12)
        if dia:
            out["dia_in"] = dia[2]
            out["barrels"] = nb if nb else _barrels(t, low)
        p = _first(prim, lambda x: x.kind == "pair" and x.unit() in ("ft", None))
        if p is not None and all(_plaus(v, 2, 30) for v in p.vals):
            out["span_ft"], out["rise_ft"] = p.vals
            out["barrels"] = p.count or _count_prefix(t, p) or _barrels(t, low)

    # ------------------------------------------------ excavation
    if fam == "excavation":
        d = _depth(prim, fam)
        if d is None:
            # "Excavation Depth and Shaping of Existing Ditch 24" to 36"":
            # the depth word heads the item, the range comes later
            if re.search(r"\bdepth\b", low):
                m = _first(prim, lambda x: x.kind in ("range", "single") and not x.role)
                if m is not None:
                    d = _range_vals(m, _ft)
                    if d is not None and not _plaus(d[2], 0.0, 60.0):
                        d = None
        if d is None and prim and prim[0].kind == "range" and prim[0].s <= 2 \
                and prim[0].unit() == "ft" and not prim[0].role:
            # "10-12' Unclassified excavation, including trench safety"
            d = _range_vals(prim[0], _ft)
        if d:
            out["depth_ft_lo"], out["depth_ft_hi"], out["depth_ft"] = d
        hd = head(low)
        chan = re.search(r"\bchannel|ditch|pond|creek|swale|detention", hd)
        road = re.search(r"\broadway|street|alley|pavement|parking|trail|driveway", hd)
        k = None
        if re.search(r"\brock\s+excav", hd):
            k = "rock"
        elif re.search(r"\bdredg", hd):
            k = "dredging"
        elif chan and road:
            k = "mixed"
        elif chan:
            k = "channel"
        elif road:
            k = "roadway"
        elif re.search(r"\bstruct\w*\s+excav", hd):
            k = "structural"
        elif re.search(r"\bun\w*ssif\w*|\buncl\b", hd):
            k = "unclassified"
        elif re.search(r"\bgeneral|\bsite\b|mass\s+grading|on-site", hd):
            k = "general"
        out["excav_kind"] = k
    return out


# Which specs belong to which family, for the coverage table.  `main` is the
# spec the reference-table question is about.
FAMILY_SPECS = {
    "gabion_basket": ["gab_wid_ft", "gab_ht_ft", "height_ft", "thick_in", "coating"],
    "gabion_mattress": ["thick_in", "coating"],
    "riprap_stone": ["rr_thick_in", "rr_any_stone", "rr_d50_in", "rr_grad", "rr_nominal_in",
                     "rr_class", "rr_grout", "rr_any_size"],
    "riprap_conc": ["thick_in"],
    "retaining_wall": ["height_ft"],
    "headwall_wingwall": ["height_ft", "dia_in", "span_ft"],
    "toe_wall_beam": ["height_ft", "thick_in"],
    "storm_pipe": ["dia_in", "material", "pipe_class", "depth_ft", "barrels", "method"],
    "box_culvert": ["span_rise", "barrels", "pipe_class", "depth_ft"],
    "channel_lining": ["thick_in"],
    "excavation": ["depth_ft", "excav_kind"],
    "water_sewer_pipe": ["dia_in", "material", "pipe_class", "depth_ft", "method"],
    "trm": [],
    "dewatering_diversion": [],
}

SPEC_COLUMNS = ["spec_family", "height_ft", "height_ft_lo", "height_ft_hi", "height_note",
                "gab_len_ft", "gab_wid_ft", "gab_ht_ft", "thick_in", "coating",
                "rr_thick_in", "rr_stone_in", "rr_d50_in", "rr_grad_lo_in", "rr_grad_hi_in",
                "rr_nominal_in", "rr_class", "rr_grout", "thick_note",
                "dia_in", "dia_in_lo", "dia_in_hi", "material", "material_all", "pipe_class",
                "barrels", "method", "span_ft", "rise_ft",
                "depth_ft", "depth_ft_lo", "depth_ft_hi", "depth_note", "excav_kind"]


def spec_present(row, spec):
    """Is `spec` found in an extract() row (dict or pandas row)?"""
    g = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))

    def ok(k):
        v = g(k, None)
        if v is None:
            return False
        try:
            if isinstance(v, float) and math.isnan(v):
                return False
        except TypeError:
            pass
        return v != ""

    if spec == "rr_any_stone":
        return ok("rr_stone_in") or ok("rr_d50_in") or ok("rr_grad_lo_in")
    if spec == "rr_grad":
        return ok("rr_grad_lo_in")
    if spec == "rr_any_size":
        return any(ok(k) for k in ("rr_thick_in", "rr_stone_in", "rr_d50_in", "rr_grad_lo_in",
                                   "rr_nominal_in"))
    if spec == "span_rise":
        return ok("span_ft") and ok("rise_ft")
    if spec in ("height_ft", "depth_ft", "dia_in"):
        return ok(spec) or ok(spec + "_lo") or ok(spec + "_hi")
    return ok(spec)


# ---------------------------------------------------------------- selftest

def _selftest():
    ok = [True]
    n = [0]

    def chk(desc, fam, want, item_family=None):
        n[0] += 1
        got = extract(desc, None, item_family)
        if fam is not None and got.get("spec_family") != fam:
            ok[0] = False
            print("FAIL family %r: got %r want %r" % (desc, got.get("spec_family"), fam))
            return
        for k, v in want.items():
            g = got.get(k)
            if isinstance(v, float) or isinstance(g, float):
                good = (g is not None and v is not None and abs(g - v) < 1e-6) or (g is v is None)
            else:
                good = g == v
            if not good:
                ok[0] = False
                print("FAIL %r [%s]: got %r want %r   (%r)" % (desc, k, g, v, got))

    # --- normalisation
    assert normalize("#0-744 inch Concrete Riprap 0 - 50 s.y.").startswith("4 inch Concrete")
    assert normalize("303 18-inch Class III Reinforced Concrete Pipe").startswith("18-inch")
    assert normalize("18 Inch RCP") == "18 Inch RCP"
    assert "D50 = 8" in normalize('Furnish and Install 12" Thie~ Layer (050 = 8") Grouted')
    assert normalize("Class Ill RCP") == "Class III RCP"
    assert normalize("12″ RCP") == '12" RCP'
    # --- riprap: thickness vs stone size vs unstated
    chk('RIPRAP (STONE PROTECTION)(18 IN)', "riprap_stone",
        {"rr_thick_in": 18.0, "rr_nominal_in": None, "rr_class": "protection"})
    chk('RIPRAP (STONE TY R)(DRY)(18 IN)', "riprap_stone",
        {"rr_thick_in": 18.0, "rr_class": "type R", "rr_grout": "dry"})
    chk('24" Bank Stabilization Riprap (18" Diameter Stone)', "riprap_stone",
        {"rr_thick_in": 24.0, "rr_stone_in": 18.0})
    chk('RIPRAP (STONE PROTECTION) 24" DEEP D50=12" UNGROUTED, PER PLANS & SPECIFICATIONS',
        "riprap_stone", {"rr_thick_in": 24.0, "rr_d50_in": 12.0, "rr_grout": "dry"})
    chk('18" - 24" ROCK RIPRAP', "riprap_stone",
        {"rr_grad_lo_in": 18.0, "rr_grad_hi_in": 24.0, "rr_thick_in": None})
    chk("Rip Rap 18x24", "riprap_stone", {"rr_grad_lo_in": 18.0, "rr_grad_hi_in": 24.0})
    chk('18" Rock Rip Rap', "riprap_stone", {"rr_nominal_in": 18.0, "rr_thick_in": None,
                                             "rr_stone_in": None})
    chk('Grouted Rock Riprap (12" Rock Riprap, 18" Thick) (Type "A"), on filter fabric',
        "riprap_stone", {"rr_thick_in": 18.0, "rr_stone_in": 12.0, "rr_class": "type A",
                         "rr_grout": "grouted"})
    chk('18-inch Rock Riprap (2\' Depth)', "riprap_stone",
        {"rr_thick_in": 24.0, "rr_stone_in": 18.0})
    chk("30-inch Rock Riprap (2.5' Depth)", "riprap_stone",
        {"rr_thick_in": 30.0, "rr_stone_in": None, "rr_nominal_in": None})
    chk('4" IRRIGATION WATER LINE INSTALLED IN PLACE BY OTHER THAN OPEN CUT', "water_sewer_pipe",
        {"dia_in": 4.0, "method": "trenchless"}, item_family="water_pipe")
    chk("8 PVC SDR-35 Wastewater Pipe", "water_sewer_pipe",
        {"dia_in": 8.0, "pipe_class": "SDR35"}, item_family="sanitary_pipe")
    chk('REPAIR OF RUPTURED WATER MAINS 8" OR LESS', "water_sewer_pipe",
        {"dia_in_hi": 8.0, "dia_in_lo": None}, item_family="water_pipe")
    chk('Rock Rip Rap - 18" Dia. 24" deep', "riprap_stone",
        {"rr_thick_in": 24.0, "rr_stone_in": 18.0})
    chk('Furnish and Install 24" Thick Layer (D50 = 15") Grouted Rock Riprap With 36" Toewall',
        "riprap_stone", {"rr_thick_in": 24.0, "rr_d50_in": 15.0, "rr_nominal_in": None})
    chk('Furnish and Install 12" Thie~ Layer (050 = 8") Grouted Rock Riprap With 36" To ewall',
        "riprap_stone", {"rr_thick_in": 12.0, "rr_d50_in": 8.0, "rr_nominal_in": None})
    chk("STONE RIP RAP (0.4' AVG DIAMETER, 0.6' MAX DIAMETER) 48 112", "riprap_stone",
        {"rr_d50_in": 4.8})
    chk('- General - Type A Rock Rip-Rap - 12"-18" Dia - 24" Thick', "riprap_stone",
        {"rr_thick_in": 24.0, "rr_grad_lo_in": 12.0, "rr_grad_hi_in": 18.0})
    chk('RipRap (12" Rock)', "riprap_stone", {"rr_stone_in": 12.0, "rr_thick_in": None})
    chk("rock riprap 18 inch diameter to 24 inch diameter loose", "riprap_stone",
        {"rr_grad_lo_in": 18.0, "rr_grad_hi_in": 24.0})
    chk("Permanent Riprap Sediment Trap", None, {"spec_family": None})
    chk("INSTALL TWELVE INCH (12\") STONE TYPE 'B', CLASS 'B' GROUTED ROCK RIPRAP, 24\" TOTAL LAYER"
        " THICKNESS, WITH FILTER FABRIC", "riprap_stone", {"rr_thick_in": 24.0, "rr_stone_in": 12.0})
    chk("Furnish & Install Rock Swale, including rock rip rap", None, {"spec_family": None},
        item_family="riprap_stone")
    chk("Inlet (Complete)(Wye)(3' X 3') With Rip-Rap Apron", None, {"spec_family": None})
    # --- concrete riprap / channel lining
    chk('RIPRAP (CONC)(5 IN)', "riprap_conc", {"thick_in": 5.0})
    chk('#0-764 inch Concrete Riprap 251 + s.y.', "riprap_conc", {"thick_in": 4.0})
    chk('Riprap (Concrete)(5")(With 9" Thick, 3\' Deep Toe)', "riprap_conc", {"thick_in": 5.0})
    chk('Concrete Riprap (5" Thick) (2\' Toe)', "riprap_conc", {"thick_in": 5.0})
    chk('6-inch thick Class A rc median riprap pavement', "riprap_conc", {"thick_in": 6.0})
    chk('Concrete Lined Channel (6" Thick) (Class F) (4,000 PSI), furnish and install',
        "channel_lining", {"thick_in": 6.0})
    chk('3-Foot-Wide Concrete Flume with 6" Curbs (Class F) (4,000 PSI)', "channel_lining",
        {"thick_in": None})
    chk('INSTALL 6 IN. CONCRETE CURBED FLUME WITH 4 FT DEEP REINF. CONCRETE TOE WALLS',
        "channel_lining", {"thick_in": 6.0})
    chk('Furnish and install 12" wide 4" 4000 psi reinforced concrete flume per detail',
        "channel_lining", {"thick_in": 4.0})
    chk("Materials Cost per SY Reinforced Concrete Channel Lining Repair (nominal 6-Inch Thick)"
        " with Weep Holes", "channel_lining", {"thick_in": 6.0})
    # --- gabions
    chk("GABIONS (3'X1.5')(GALV)", "gabion_basket",
        {"gab_wid_ft": 3.0, "gab_ht_ft": 1.5, "coating": "galvanized"})
    chk("Placement of Gabion Structures (3 Ft x 1.S Ft)(PVC)", "gabion_basket",
        {"gab_wid_ft": 3.0, "gab_ht_ft": 1.5, "coating": "pvc"})
    chk("basket 12 feet x 3 feet x 1.5 feet PVC coated gabion", "gabion_basket",
        {"gab_len_ft": 12.0, "gab_wid_ft": 3.0, "gab_ht_ft": 1.5})
    chk("FURNISH & INSTALL 3FT X 3FT GABIONS (PVC)", "gabion_basket",
        {"gab_wid_ft": 3.0, "gab_ht_ft": 3.0})
    chk('Gabion Mattress (12" THICK)', "gabion_mattress", {"thick_in": 12.0})
    chk('12" GABION MAT', "gabion_mattress", {"thick_in": 12.0})
    chk("GABION MATTRESSES (PVC COATED) (18 IN), PER REACH 2 PLANS", "gabion_mattress",
        {"thick_in": 18.0, "coating": "pvc"})
    chk("Gabion Mattress (12 ln)(PVC)", "gabion_mattress", {"thick_in": 12.0})
    chk("12-INCH GABION MATTRESS 31 230", "gabion_mattress", {"thick_in": 12.0})
    chk("Gabion Basket Concrete Beams (Class F) (4,200 PSI)", "toe_wall_beam", {})
    chk("GABION BASKET TIEBACKS", None, {"spec_family": None})
    # --- walls
    chk("#0-61Install retaining wall (1.6 - 3 ft) with integral sidewalk", "retaining_wall",
        {"height_ft_lo": 1.6, "height_ft_hi": 3.0})
    chk("Retaining Wall (up to 48-inch measured from top of sidewalk)", "retaining_wall",
        {"height_ft_hi": 4.0, "height_ft_lo": None, "height_ft": 4.0})
    chk('Concrete Retaining Wall (Greater Than 36" Height)', "retaining_wall",
        {"height_ft_lo": 3.0, "height_ft_hi": None})
    chk('Concrete Retaining Wall (36" Height Or Less)', "retaining_wall", {"height_ft_hi": 3.0})
    chk("CONSTRUCT VARIABLE HEIGHT RETAINING WALL (2'-6')", "retaining_wall",
        {"height_ft_lo": 2.0, "height_ft_hi": 6.0, "height_note": "variable"})
    chk("Install TxDOT Retaining Wall RW (SFA) 2 Feet 8 Inch to 4 Feet in Height (Measured from"
        " The Surface", "retaining_wall", {"height_ft_hi": 4.0})
    chk('TXDOT Retaining Wall (24" Average Height)', "retaining_wall", {"height_ft": 2.0})
    chk("Retaining wall (0-3') with 4\" integral sidewalk 6.5 sack (4500 psi) 0-50+",
        "retaining_wall", {"height_ft_lo": 0.0, "height_ft_hi": 3.0})
    chk("Concrete Sidewalk Retaining Wall (8\" Thick)", "retaining_wall", {"height_ft": None})
    chk("Cantilevered Shade Structure, Complete In Place", None, {"spec_family": None},
        item_family="retaining_wall")
    chk("REINF CONC TYPE 6 RETAIN WALL", "retaining_wall", {"height_ft": None})
    chk("WINGWALL (PW - 1) (HW=13 FT)", "headwall_wingwall", {"height_ft": 13.0})
    chk("Wingwall (TXDOT)(PW-1)(HW=7.5')", "headwall_wingwall", {"height_ft": 7.5})
    chk("Concrete Wingwall With Flared Wings (FW-0) (HW=4) (5'x3')", "headwall_wingwall",
        {"height_ft": 4.0, "span_ft": 5.0, "rise_ft": 3.0})
    chk("Construct TxDot 2-18\" CH-PW-S Headwall", "headwall_wingwall",
        {"dia_in": 18.0, "barrels": 2})
    chk("4-Foot Deep Reinforced Concrete Toe Wall (Class F) (4,000 PSI)", "toe_wall_beam",
        {"height_ft": 4.0})
    chk("4' X 8\" Toe Walls Top and Bottom of Slope", "toe_wall_beam", {"height_ft": 4.0})
    chk("Construct Concrete Toe Wall (1' Height)", "toe_wall_beam", {"height_ft": 1.0})
    # --- pipes
    chk('RCP (Class III)(18")', "storm_pipe", {"dia_in": 18.0, "material": "RCP",
                                              "pipe_class": "III"})
    chk('RC PIPE (CL IV) (36 IN)', "storm_pipe", {"dia_in": 36.0, "pipe_class": "IV"})
    chk('Reinforced Concrete Pipe (48") (Class III)', "storm_pipe",
        {"dia_in": 48.0, "material": "RCP", "pipe_class": "III"}, item_family="concrete_misc")
    chk('Furnish & Install 21" Class Ill Reinforced Concrete Pipe', "storm_pipe",
        {"dia_in": 21.0, "pipe_class": "III"}, item_family="concrete_misc")
    chk('18" ADS HDPE Pipe (N-12)', "storm_pipe", {"dia_in": 18.0, "material": "HDPE"},
        item_family="other")
    chk('15-inch Corrugated Polypopylene Pipe', "storm_pipe", {"dia_in": 15.0, "material": "PP"},
        item_family="other")
    chk('Storm Sewer Point Repair (15-21" RCP)', "storm_pipe",
        {"dia_in_lo": 15.0, "dia_in_hi": 21.0})
    chk("Circular Culverts 15\" - 2 2/3\" x 1/2\" - 16 gauge", "storm_pipe",
        {"dia_in": 15.0, "material": "CMP"}, item_family="other")
    chk('30in Reinforced Concrete Pipe (RCP) (3ft to 9ft in depth)', "storm_pipe",
        {"dia_in": 30.0, "depth_ft_lo": 3.0, "depth_ft_hi": 9.0})
    chk('JACK BOR OR TUN PIPE(27 IN)(RC)(CL III)', "storm_pipe",
        {"dia_in": 27.0, "method": "trenchless", "pipe_class": "III", "material": "RCP"},
        item_family="other")
    chk('- RCP - Class III - 24" - Triple', "storm_pipe", {"dia_in": 24.0, "barrels": 3})
    chk("FL BS (CMP IN PLACE)(TY A GR 1-2)(8\")", None, {"spec_family": None},
        item_family="storm_pipe")
    chk("4-Foot Polypropylene Orange Safety Fencing", None, {"spec_family": None},
        item_family="storm_pipe")
    chk("12-Inch PVC Sanitary Sewer (SDR-26) Open Cut (10' to 15' Depth)", "water_sewer_pipe",
        {"dia_in": 12.0, "pipe_class": "SDR26", "depth_ft_lo": 10.0, "depth_ft_hi": 15.0,
         "method": "open_cut"}, item_family="sanitary_pipe")
    chk("8-inch PVC Water Line by Open Cut, including 20-inch Steel Casing Pipe",
        "water_sewer_pipe", {"dia_in": 8.0, "material": "PVC"}, item_family="water_pipe")
    # --- boxes
    chk("9'x6' RCB", "box_culvert", {"span_ft": 9.0, "rise_ft": 6.0}, item_family="other")
    chk("6-foot x 3-foot Reinforced Concrete Box Culvert", "box_culvert",
        {"span_ft": 6.0, "rise_ft": 3.0})
    chk("Reinforced Concrete Box Culvert (10' X 6')(Precast)", "box_culvert",
        {"span_ft": 10.0, "rise_ft": 6.0})
    chk("4-6'x4' Box Culvert", "box_culvert", {"span_ft": 6.0, "rise_ft": 4.0, "barrels": 4})
    chk("PRECAST 12X12 BOX CULVERT", "box_culvert", {"span_ft": 12.0, "rise_ft": 12.0})
    chk("6'X4' CLASS V CONCRETE BOX CULVERT", "box_culvert",
        {"span_ft": 6.0, "rise_ft": 4.0, "pipe_class": None})
    chk("6'x4' CL III RCB storm drain box, furnish and install complete in place", "box_culvert",
        {"span_ft": 6.0, "rise_ft": 4.0, "pipe_class": "III"})
    chk('8" X 4" RCBC PLUG', "box_culvert", {"span_ft": None})
    # --- excavation
    chk("Unclassified Channel Excavation", "excavation", {"excav_kind": "channel",
                                                         "depth_ft": None})
    chk('Excavation Depth and Shaping of Existing Ditch 24" to 36"', "excavation",
        {"depth_ft_lo": 2.0, "depth_ft_hi": 3.0})
    chk("TRENCH EXCAVATION PROTECTION", None, {"spec_family": None}, item_family="excavation")
    chk("10-12' Unclassified excavation, including trench safety", "excavation",
        {"depth_ft_lo": 10.0, "depth_ft_hi": 12.0, "excav_kind": "unclassified"},
        item_family="trench_safety")
    chk("Excavation (Roadway & Channel)", "excavation", {"excav_kind": "mixed"})
    chk("Retaining Wall (up to 48-inch)", "retaining_wall", {"height_ft_hi": 4.0})
    chk("Toe Wall (3 Ft x 3 Ft)", "toe_wall_beam", {"height_ft": 3.0})
    chk('Replace 12" VCP Sewer with 12" Sewer (HDPE or Fusible PVC) by Pipe Bursting',
        "water_sewer_pipe", {"dia_in": 12.0, "method": "trenchless"}, item_family="other")
    chk('Lay storm water pipe 0-30" RCP supplied by Contractor', "storm_pipe",
        {"dia_in": None, "barrels": None})
    chk('6\'x6\' Storm Drain Junction Box, furnish and install complete in place, including manhole'
        ' access, as per specifications. Custom Concrete Storm Drain Headwall for Gabion Basket', None,
        {"spec_family": None}, item_family="gabion_wall")
    chk('3-Foot-Wide Concrete Flume with 6" Curbs (Class F) (4,000 PSI), on 6" crushed rock '
        'foundation, on filter fabric', "channel_lining", {"thick_in": None})
    chk('4-ft x 3-ft RCBC W/ 48-in Class III RCP Circular Riser', "box_culvert",
        {"span_ft": 4.0, "rise_ft": 3.0, "pipe_class": None})
    # --- others
    chk("Control of Water", "dewatering_diversion", {})
    chk("Turf Reinforcement Mat", "trm", {}, item_family="trm")
    chk("Remove Existing 18\" RCP", None, {"spec_family": None}, item_family="demo_other")
    print("item_specs selftest: %d cases, %s" % (n[0], "PASS" if ok[0] else "FAIL"))
    return ok[0]


# ---------------------------------------------------------------- report
#
# Everything below reads the FROZEN public snapshot and writes CSVs under
# reports/item_specs/.  Public rows only, at every step:
#   master_panel  source == city_bidtab, source_set == external, and a path
#                 separator in project_key (garland_avg is a contract-price
#                 report and TxDOT is a different market, both left out)
#   line_items    src_file WITH a path separator (README: no separator =
#                 company file); used for the raw-text coverage check
# Engineer estimates are not bids and are dropped.

TARGETS = ["gabion_basket", "gabion_mattress", "riprap_stone", "riprap_conc", "retaining_wall",
           "headwall_wingwall", "toe_wall_beam", "storm_pipe", "box_culvert", "channel_lining",
           "excavation", "trm", "dewatering_diversion", "water_sewer_pipe"]
_TOTAL_ROW = re.compile(r"\b(?:grand\s+total|base\s+bid\s+total|total\s+base\s+bid|sub\s*-?\s*total|"
                        r"total\s+bid|bid\s+total|total\s+amount)\b", I)
PANEL_COLS = ["source", "price_basis", "source_set", "owner", "project_key", "bid_date", "item_no",
              "description", "item_family", "size_in", "size_bucket", "work_type", "unit",
              "quantity", "bidder", "is_engineer_estimate", "is_low_bidder", "n_bidders",
              "unit_price", "competition_class", "year", "project_kind"]


def _pd():
    import numpy as np
    import pandas as pd
    return np, pd


def load_panel(data):
    np, pd = _pd()
    p = os.path.join(data, "master_panel.csv")
    df = pd.read_csv(p, usecols=PANEL_COLS, encoding="utf-8-sig", low_memory=False,
                     dtype={"item_no": str, "description": str, "project_key": str, "bidder": str,
                            "owner": str, "work_type": str, "size_bucket": str})
    n_all = len(df)
    pub = (df.source == "city_bidtab") & (df.source_set == "external") & \
        df.project_key.astype(str).str.contains(r"[/\\]", regex=True)
    company = int((df.source_set == "internal").sum())
    df = df[pub].copy()
    ee = df.is_engineer_estimate.astype(str).str.lower().isin(["true", "1", "yes"])
    df = df[~ee & (df.unit_price > 0) & (df.quantity > 0)].copy()
    df["description"] = df.description.fillna("")
    df["size_bucket"] = df.size_bucket.fillna("")
    df["ext"] = df.quantity * df.unit_price
    meta = {"panel_rows": n_all, "panel_company_rows_excluded": company,
            "public_municipal_bid_rows": len(df)}
    return df, meta


def load_line_items(data):
    np, pd = _pd()
    li = pd.read_csv(os.path.join(data, "line_items.csv"), dtype=str, encoding="utf-8-sig")
    sep = li.src_file.astype(str).str.contains(r"[/\\]", regex=True)
    company = int((~sep).sum())
    li = li[sep & (li.source_set == "external")].copy()
    ee = li.is_engineer_estimate.astype(str).str.lower().isin(["true", "1", "yes"])
    li = li[~ee].copy()
    li["description"] = li.description.fillna("")
    return li, {"line_items_company_rows_excluded": company, "line_items_public_rows": len(li)}


def annotate_frame(df):
    """spec_family, element and every spec, for each distinct description."""
    np, pd = _pd()
    u = df[["description", "item_family"]].drop_duplicates().reset_index(drop=True)
    recs = []
    for d, f in zip(u.description, u.item_family):
        fam = spec_family(d, f if isinstance(f, str) else None)
        r = extract(d, fam) if fam else {"spec_family": None}
        r["element"] = element(d, fam) if fam else None
        recs.append(r)
    sp = pd.DataFrame(recs)
    for c in SPEC_COLUMNS + ["element"]:
        if c not in sp.columns:
            sp[c] = None
    u = pd.concat([u, sp[SPEC_COLUMNS + ["element"]]], axis=1)
    return df.merge(u, on=["description", "item_family"], how="left")


def broken_lettings(df, data, min_items=8, log10_cut=1.0):
    """Lettings whose prices sit a factor of 10 off the public reference.

    Measured, not assumed: Richardson Bid 02-22 (Custer Road) parses with
    the decimal point lost -- $7,450/SY concrete pavement, $20,750/LF 12 in
    water line -- and alone carries $632M, more than every other erosion or
    drainage letting together.  A letting is flagged when the median, over
    at least `min_items` of its items, of log10(unit price / price_reference
    median for the same family, unit and size bucket) exceeds `log10_cut`.
    """
    np, pd = _pd()
    ref = pd.read_csv(os.path.join(data, "price_reference.csv"), encoding="utf-8-sig",
                      dtype={"size_bucket": str})
    ref["size_bucket"] = ref.size_bucket.fillna("")
    ref = ref[ref.n_projects >= 5][["item_family", "unit", "size_bucket", "price_median"]]
    k = ["project_key", "item_no", "description", "unit", "quantity"]
    it = df.groupby(k, dropna=False).agg(item_family=("item_family", "first"),
                                         size_bucket=("size_bucket", "first"),
                                         up=("unit_price", "median")).reset_index()
    it = it.merge(ref, on=["item_family", "unit", "size_bucket"], how="inner")
    it["lr"] = np.log10(it.up / it.price_median)
    s = it.groupby("project_key").lr.agg(["count", "median"])
    bad = s[(s["count"] >= min_items) & (s["median"].abs() > log10_cut)]
    return set(bad.index), bad


def items_frame(df):
    """One row per letting item: median bid, median extended amount."""
    np, pd = _pd()
    k = ["project_key", "item_no", "description", "unit", "quantity"]
    agg = dict(item_family=("item_family", "first"), spec_family=("spec_family", "first"),
               element=("element", "first"), work_type=("work_type", "first"),
               year=("year", "first"), owner=("owner", "first"),
               competition_class=("competition_class", "first"),
               project_kind=("project_kind", "first"), price_basis=("price_basis", "first"),
               size_bucket=("size_bucket", "first"), n_bids=("unit_price", "size"),
               up=("unit_price", "median"), ext=("ext", "median"))
    for c in SPEC_COLUMNS:
        if c != "spec_family":
            agg[c] = (c, "first")
    it = df.groupby(k, dropna=False).agg(**agg).reset_index()
    low = df[df.is_low_bidder == True].groupby(k, dropna=False).ext.sum().rename("ext_low")
    return it.merge(low, left_on=k, right_index=True, how="left")


def dollar_share(items):
    """Share of item dollars by spec family (target) or item_family (rest)."""
    np, pd = _pd()
    it = items.copy()
    it["group"] = np.where(it.spec_family.notna(), it.spec_family, "other:" + it.item_family.fillna(""))
    tot = it.ext.sum()
    tot_low = it.ext_low.sum()
    lt = it.groupby("project_key").ext.sum().rename("letting_total")
    it = it.join(lt, on="project_key")
    it["share_in_letting"] = it.ext / it.letting_total
    n_let = it.project_key.nunique()
    g = it.groupby("group").agg(items=("ext", "size"), lettings=("project_key", "nunique"),
                                usd=("ext", "sum"), usd_low=("ext_low", "sum"))
    g["share_usd"] = g.usd / tot
    g["share_usd_lowbidder"] = g.usd_low / tot_low
    mean_share = it.groupby(["project_key", "group"]).share_in_letting.sum().unstack(fill_value=0)
    g["mean_share_per_letting"] = mean_share.mean().reindex(g.index)
    g["lettings_pct"] = g.lettings / n_let
    g = g.sort_values("usd", ascending=False)
    g["cum_share"] = g.share_usd.cumsum()
    return g.reset_index(), {"lettings": n_let, "items": len(it), "usd_total": tot}


def coverage(items, rows):
    """Share of bid rows and of item dollars, per family and spec, with the spec found."""
    np, pd = _pd()
    out = []
    for fam in TARGETS:
        specs = FAMILY_SPECS.get(fam, [])
        for scope_el in ("all", "main"):
            it = items[items.spec_family == fam]
            rw = rows[rows.spec_family == fam]
            if scope_el == "main":
                it = it[it.element == "main"]
                rw = rw[rw.element == "main"]
            if not len(it):
                continue
            if not specs:
                out.append(dict(spec_family=fam, element=scope_el, spec="(none in text)",
                                rows=len(rw), items=len(it), lettings=it.project_key.nunique(),
                                usd=it.ext.sum(), share_rows=np.nan, share_usd=np.nan))
                continue
            for sp in specs:
                f_it = it.apply(lambda r: spec_present(r, sp), axis=1)
                f_rw = rw.apply(lambda r: spec_present(r, sp), axis=1)
                out.append(dict(spec_family=fam, element=scope_el, spec=sp, rows=len(rw),
                                items=len(it), lettings=it.project_key.nunique(), usd=it.ext.sum(),
                                share_rows=f_rw.mean() if len(rw) else np.nan,
                                share_usd=(it.ext[f_it].sum() / it.ext.sum()) if it.ext.sum() else
                                np.nan))
    return pd.DataFrame(out)


# ----- price effects ---------------------------------------------------------

NUMERIC_KEYS = {
    "dia_in": "dia_in", "span_rise": None, "height_ft": "height_ft", "thick_in": "thick_in",
    "rr_thick_in": "rr_thick_in", "rr_stone": None, "rr_nominal_in": "rr_nominal_in",
    "gab_ht_ft": "gab_ht_ft", "depth_ft": "depth_ft",
}
CAT_KEYS = ["material", "pipe_class", "barrels", "method", "rr_class", "rr_grout", "coating",
            "excav_kind"]


def _level(items, spec):
    """The level of a spec on each item row, as a string key (None if absent)."""
    np, pd = _pd()

    def fmt(v):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else \
            ("%g" % v if isinstance(v, (int, float)) else str(v))
    if spec == "span_rise":
        return items.apply(lambda r: None if fmt(r.span_ft) is None or fmt(r.rise_ft) is None
                           else "%sx%s" % (fmt(r.span_ft), fmt(r.rise_ft)), axis=1)
    if spec == "rr_stone":
        def st(r):
            if fmt(r.rr_d50_in):
                return "d50 " + fmt(r.rr_d50_in)
            if fmt(r.rr_stone_in):
                return "stone " + fmt(r.rr_stone_in)
            if fmt(r.rr_grad_lo_in):
                return "grad %s-%s" % (fmt(r.rr_grad_lo_in), fmt(r.rr_grad_hi_in))
            return None
        return items.apply(st, axis=1)
    if spec in ("height_ft", "depth_ft", "dia_in"):
        def rg(r):
            lo, hi, v = r.get(spec + "_lo"), r.get(spec + "_hi"), r.get(spec)
            if fmt(lo) and fmt(hi) and fmt(lo) != fmt(hi):
                return "%s-%s" % (fmt(lo), fmt(hi))
            if fmt(hi) and not fmt(lo):
                return "<=" + fmt(hi)
            if fmt(lo) and not fmt(hi):
                return ">=" + fmt(lo)
            return fmt(v)
        return items.apply(rg, axis=1)
    return items[spec].map(fmt)


def _numeric(items, spec):
    np, pd = _pd()
    if spec == "span_rise":
        return items.span_ft * items.rise_ft   # opening area, ft2
    if spec in items.columns:
        return pd.to_numeric(items[spec], errors="coerce")
    return pd.Series(np.nan, index=items.index)


def lolo_mae(cell, keys, min_other=2):
    """Leave-one-letting-out median absolute log error of a key hierarchy.

    `keys` is a list of level arrays, most specific first.  An item is
    predicted by the median log price of its level in the FIRST key whose
    level is carried by at least `min_other` OTHER lettings, else by the
    cell median of the other lettings.  Its own letting never contributes.
    Returns (mae, share of items predicted by keys[0]).
    """
    np, pd = _pd()
    lp = np.log(cell.up.values)
    lets = cell.project_key.values
    errs, hit = [], 0
    for i in range(len(cell)):
        other = lets != lets[i]
        pred, used = None, False
        for j, k in enumerate(keys):
            if k[i] is None or (isinstance(k[i], float) and math.isnan(k[i])):
                continue
            same = other & (k == k[i])
            if len(set(lets[same])) >= min_other:
                pred, used = np.median(lp[same]), j == 0
                break
        if pred is None:
            pred = np.median(lp[other])
        hit += used
        errs.append(abs(lp[i] - pred))
    return float(np.median(errs)), hit / max(len(cell), 1)


def slope(cell, x, nboot=300, seed=7):
    """log price ~ log x (+ log quantity + year), cluster bootstrap by letting."""
    np, pd = _pd()
    d = cell.assign(x=x).dropna(subset=["x"])
    d = d[(d.x > 0) & (d.quantity > 0)]
    if d.project_key.nunique() < 6 or d.x.nunique() < 3:
        return None
    y = np.log(d.up.values)
    lx = np.log(d.x.values)
    lq = np.log(d.quantity.values)
    yr = pd.to_numeric(d.year, errors="coerce").fillna(d.year.median() if len(d) else 2022).values

    def fit(idx, ctrl):
        X = [np.ones(len(idx)), lx[idx]]
        if ctrl:
            X += [lq[idx], yr[idx] - 2022.0]
        X = np.column_stack(X)
        b, *_ = np.linalg.lstsq(X, y[idx], rcond=None)
        return b[1]
    allidx = np.arange(len(d))
    b0 = fit(allidx, False)
    b1 = fit(allidx, True)
    rng = np.random.default_rng(seed)
    lets = d.project_key.values
    ul = np.unique(lets)
    groups = {u: np.where(lets == u)[0] for u in ul}
    bs = []
    for _ in range(nboot):
        pick = rng.choice(ul, size=len(ul), replace=True)
        idx = np.concatenate([groups[u] for u in pick])
        if len(np.unique(lx[idx])) < 2:
            continue
        bs.append(fit(idx, True))
    lo, hi = (np.percentile(bs, 5), np.percentile(bs, 95)) if bs else (np.nan, np.nan)
    rho = pd.Series(lx).corr(pd.Series(y), method="spearman")
    return dict(slope_raw=b0, slope_ctrl=b1, slope_ctrl_p05=lo, slope_ctrl_p95=hi,
                spearman=rho, n_items_x=len(d), n_lettings_x=d.project_key.nunique())


PRICE_SPECS = {
    "storm_pipe": ["dia_in", "material", "pipe_class", "barrels", "method", "depth_ft"],
    "water_sewer_pipe": ["dia_in", "material", "pipe_class", "method", "depth_ft"],
    "box_culvert": ["span_rise", "barrels"],
    "riprap_stone": ["rr_thick_in", "rr_stone", "rr_nominal_in", "rr_class", "rr_grout"],
    "riprap_conc": ["thick_in"],
    "gabion_basket": ["gab_ht_ft", "coating", "thick_in"],
    "gabion_mattress": ["thick_in", "coating"],
    "retaining_wall": ["height_ft"],
    "headwall_wingwall": ["height_ft", "dia_in", "span_rise"],
    "toe_wall_beam": ["height_ft"],
    "channel_lining": ["thick_in"],
    "excavation": ["excav_kind", "depth_ft"],
}


def price_effects(items, min_lettings=4):
    """Within (family, unit), does the spec explain unit-price dispersion?

    Items are letting-item medians (median over bidders) from competitive
    public construction lettings -- the price_reference gate -- main pay
    items only, all municipal work types (a pipe is priced the same in a
    drainage and a paving letting; the work-type split leaves too few).
    Per (family, unit, spec), on the items that CARRY the spec, four
    leave-one-letting-out errors (median |log error|):
        mae_cell      the cell median
        mae_existing  the estimator's existing key, size_bucket (the first
                      "N in" of the text), falling back to the cell
        mae_spec      the spec level, falling back to the cell
        mae_both      size_bucket x spec, then size_bucket, then the cell
    gain_vs_cell = 1 - mae_spec / mae_cell says whether the spec explains
    dispersion at all; gain_over_existing = 1 - mae_both / mae_existing
    says whether it adds anything to the key the estimator already uses.
    Numeric specs also get the log-log slope, raw and with log quantity and
    year controls, with a 90% cluster-bootstrap interval (by letting).
    """
    np, pd = _pd()
    g = items[(items.competition_class == "competitive") & (items.project_kind == "construction")
              & (items.price_basis != "uneven_proposal") & (items.element == "main")
              & items.spec_family.notna()].copy()
    rows, levels = [], []
    for (fam, unit), cell in g.groupby(["spec_family", "unit"]):
        if cell.project_key.nunique() < min_lettings or fam not in PRICE_SPECS:
            continue
        cell = cell.copy()
        for spec in PRICE_SPECS[fam]:
            lev = _level(cell, spec)
            have = lev.notna()
            if have.sum() == 0:
                continue
            sub = cell[have].copy()
            lv_s = lev[have].astype(object).values
            # an empty size_bucket is a key level of its own in price_reference
            # (every unsized row of the family and unit pools there)
            lv_b = sub.size_bucket.fillna("").astype(object).values
            lv_bs = np.array(["%s|%s" % (b, v) for b, v in zip(lv_b, lv_s)], dtype=object)
            r = dict(spec_family=fam, unit=unit, spec=spec, cell_items=len(cell),
                     cell_lettings=cell.project_key.nunique(), items_with_spec=int(have.sum()),
                     lettings_with_spec=sub.project_key.nunique(), levels=len(set(lv_s)),
                     share_items_with_size_bucket=float(np.mean([b != "" for b in lv_b])))
            if sub.project_key.nunique() >= min_lettings:
                none = np.array([None] * len(sub), dtype=object)
                m_cell, _ = lolo_mae(sub, [none])
                m_ex, _ = lolo_mae(sub, [lv_b])
                m_sp, used = lolo_mae(sub, [lv_s])
                m_both, _ = lolo_mae(sub, [lv_bs, lv_b])
                r.update(mae_cell=m_cell, mae_existing=m_ex, mae_spec=m_sp, mae_both=m_both,
                         share_items_level_used=used,
                         gain_vs_cell=(m_cell - m_sp) / m_cell if m_cell else np.nan,
                         gain_over_existing=(m_ex - m_both) / m_ex if m_ex else np.nan)
            if spec in NUMERIC_KEYS or spec == "span_rise":
                sl = slope(sub, _numeric(sub, spec if spec != "rr_stone" else "rr_d50_in"))
                if sl:
                    r.update(sl)
            rows.append(r)
            lv = sub.assign(level=lv_s).groupby("level").agg(
                items=("up", "size"), lettings=("project_key", "nunique"),
                median_price=("up", "median"), p25=("up", lambda q: q.quantile(.25)),
                p75=("up", lambda q: q.quantile(.75)),
                median_qty=("quantity", "median"), median_year=("year", "median")).reset_index()
            lv.insert(0, "spec", spec)
            lv.insert(0, "unit", unit)
            lv.insert(0, "spec_family", fam)
            levels.append(lv)
    return pd.DataFrame(rows), (pd.concat(levels) if levels else pd.DataFrame())


def _sample(rows, n, seed=20261119, tail_per_family=7, unseen=None):
    """A stratified random sample of distinct descriptions for hand-checking.

    Strata are the target families; each gets max(8, share by sqrt(rows)).
    A description is drawn with probability proportional to its bid rows
    inside its family, so the sample reflects what the money and the
    coverage numbers rest on, not the long tail of one-off spellings.
    """
    np, pd = _pd()
    u = rows[rows.spec_family.isin(TARGETS)].groupby(["description", "item_family"],
                                                     dropna=False).agg(
        rows=("unit_price", "size"), spec_family=("spec_family", "first"),
        element=("element", "first")).reset_index()
    w = u.groupby("spec_family").rows.sum() ** 0.5
    alloc = (w / w.sum() * n).round().clip(lower=8).astype(int)
    rng = np.random.default_rng(seed)
    out = []
    for fam, k in alloc.items():
        f = u[u.spec_family == fam]
        k = min(k, len(f))
        idx = rng.choice(f.index.values, size=k, replace=False,
                         p=(f.rows / f.rows.sum()).values)
        out.append(u.loc[idx])
    smp = pd.concat(out).assign(stratum="row_weighted")
    # the long tail: distinct spellings of the specialty families, drawn
    # uniformly, so the hard one-off wordings are checked too
    tail_fams = ["riprap_stone", "riprap_conc", "gabion_basket", "gabion_mattress",
                 "retaining_wall", "headwall_wingwall", "toe_wall_beam", "channel_lining",
                 "box_culvert"]
    rest = u[u.spec_family.isin(tail_fams) & ~u.index.isin(smp.index)]
    tail = []
    for fam in tail_fams:
        f = rest[rest.spec_family == fam]
        k = min(tail_per_family, len(f))
        tail.append(f.loc[rng.choice(f.index.values, size=k, replace=False)])
    parts = [smp] + [t.assign(stratum="uniform_tail") for t in tail]
    if unseen is not None and len(unseen):
        # every target-family description that exists ONLY in line_items
        # (rows the panel dropped): text the extractor was never tuned on
        parts.append(unseen.assign(stratum="line_items_only"))
    smp = pd.concat(parts).reset_index(drop=True)
    ex = pd.DataFrame([extract(d, f) for d, f in zip(smp.description, smp.spec_family)])
    for c in SPEC_COLUMNS:
        if c not in ex.columns:
            ex[c] = None
    smp = pd.concat([smp, ex[[c for c in SPEC_COLUMNS if c != "spec_family"]]], axis=1)
    smp.insert(0, "sample_id", range(1, len(smp) + 1))
    return smp


def run_report(data, out, sample_n=0):
    np, pd = _pd()
    os.makedirs(out, exist_ok=True)
    df, meta = load_panel(data)
    li, meta2 = load_line_items(data)
    meta.update(meta2)
    df = annotate_frame(df)
    bad, badtab = broken_lettings(df, data)
    meta["broken_lettings"] = sorted(bad)
    tot = df.description.str.contains(_TOTAL_ROW)
    meta["total_rows_dropped"] = int(tot.sum())
    df = df[~df.project_key.isin(bad) & ~tot].copy()
    items = items_frame(df)
    # erosion / drainage CONSTRUCTION lettings (project_kind as build_master
    # sets it; a supply or service tab labelled drainage is not a letting
    # of the client's kind)
    ed = items[items.work_type.isin(["erosion", "drainage"]) & (items.project_kind == "construction")]
    ed_rows = df[df.work_type.isin(["erosion", "drainage"]) & (df.project_kind == "construction")]

    share, m3 = dollar_share(ed)
    meta.update({"erosion_drainage_" + k: v for k, v in m3.items()})
    # the two work types separately: creek-erosion lettings and storm-drain
    # lettings buy very different things
    for wt in ("erosion", "drainage"):
        sw, mw = dollar_share(ed[ed.work_type == wt])
        meta.update({wt + "_" + k: v for k, v in mw.items()})
        sw = sw.set_index("group")[["lettings", "share_usd", "mean_share_per_letting"]]
        sw.columns = [c + "_" + wt for c in sw.columns]
        share = share.merge(sw, left_on="group", right_index=True, how="left")
    share.to_csv(os.path.join(out, "family_dollar_share.csv"), index=False)
    cw = (ed.assign(spec_family=ed.spec_family.fillna("(none)"))
          .groupby(["spec_family", "item_family"]).agg(items=("ext", "size"), usd=("ext", "sum"))
          .reset_index().sort_values(["spec_family", "usd"], ascending=[True, False]))
    cw.to_csv(os.path.join(out, "family_crosswalk.csv"), index=False)

    cov_ed = coverage(ed, ed_rows).assign(scope="erosion_drainage")
    cov_all = coverage(items, df).assign(scope="all_municipal")
    pd.concat([cov_ed, cov_all]).to_csv(os.path.join(out, "coverage.csv"), index=False)

    # raw-text coverage on public line items (incl. rows the panel drops)
    lu = li[["description", "item_family"]].drop_duplicates()
    lu = annotate_frame(lu.assign(_one=1)).drop(columns=["_one"])
    lrows = li[["description", "item_family"]].merge(lu, on=["description", "item_family"],
                                                     how="left")
    lc = []
    for fam in TARGETS:
        r = lrows[(lrows.spec_family == fam) & (lrows.element == "main")]
        for sp in FAMILY_SPECS.get(fam, []):
            lc.append(dict(spec_family=fam, spec=sp, line_item_rows=len(r),
                           share_rows=r.apply(lambda x: spec_present(x, sp), axis=1).mean()
                           if len(r) else np.nan))
    pd.DataFrame(lc).to_csv(os.path.join(out, "coverage_line_items.csv"), index=False)

    eff, lev = price_effects(items)
    eff.to_csv(os.path.join(out, "price_effects.csv"), index=False)
    lev.to_csv(os.path.join(out, "price_by_spec_level.csv"), index=False)

    # size_in agreement for pipes: the existing key vs the extracted diameter
    pip = items[(items.spec_family == "storm_pipe") & (items.element == "main")]
    rows_si = df[(df.spec_family == "storm_pipe") & (df.element == "main")]
    agree = rows_si.dropna(subset=["dia_in"])
    meta["storm_pipe_main_rows"] = len(rows_si)
    meta["storm_pipe_rows_size_in_equals_dia"] = float((agree.size_in == agree.dia_in).mean()) \
        if len(agree) else None
    meta["storm_pipe_rows_dia_found_size_in_missing"] = int(
        (rows_si.dia_in.notna() & rows_si.size_in.isna()).sum())
    meta["storm_pipe_items"] = len(pip)
    badtab.to_csv(os.path.join(out, "excluded_lettings_scale.csv"))
    if sample_n:
        allp = set(pd.read_csv(os.path.join(data, "master_panel.csv"), usecols=["description"],
                               dtype=str, encoding="utf-8-sig").description.fillna("").unique())
        un = lrows[~lrows.description.isin(allp) & lrows.spec_family.isin(TARGETS)]
        un = un.groupby(["description", "item_family"], dropna=False).agg(
            rows=("description", "size"), spec_family=("spec_family", "first"),
            element=("element", "first")).reset_index()
        smp = _sample(df, sample_n, unseen=un)
        smp.to_csv(os.path.join(out, "precision_sample_unjudged.csv"), index=False)
    import json
    with open(os.path.join(out, "run_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=1, default=str)
    print(json.dumps({k: v for k, v in meta.items() if k != "broken_lettings"}, indent=1,
                     default=str))
    return meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--sample", type=int, default=0,
                    help="also write a hand-check sample of N descriptions")
    ap.add_argument("--data", default=SNAPSHOT,
                    help="folder with master_panel.csv, line_items.csv, price_reference.csv")
    ap.add_argument("--out", default=OUTDIR)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    if a.report or a.sample:
        if os.path.abspath(a.data) == os.path.abspath(os.path.join(ROOT, "data", "clean")):
            print("note: reading data/clean -- only do this when no rebuild is running")
        run_report(a.data, a.out, a.sample)
        sys.exit(0)
    ap.print_help()
