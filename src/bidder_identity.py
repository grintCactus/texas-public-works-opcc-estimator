"""One contractor, one identity.

Bidder names are read off column headers, so the same firm arrives spelled
several ways across the corpus: `Rebcon LLC` / `Rebcon, LLC`,
`HQS CONSTRUCTION LLC` / `HQS Construction LLC` / `HQS Construction, LLC` /
`HQS Construction, LLC.`, and in one file
`Jim Bo w m a n C o n s t r uction C o m p a n y , L . P` -- the
character-positioned text layer leaking into the header.  Measured on the live
corpus: 1,191 distinct names collapse to 1,077, with 114 groups merging.

Two things downstream depend on getting this right.

n_bidders, and therefore the COMPETITION DISCOUNT.  n_bidders is a count of
distinct bidder names in a file.  Two spellings of one firm in one document
count as two bidders, which moves that letting into a more competitive class
than it was -- and the discount is the whole second stage of the estimate.

PER-BIDDER fits.  qty_form.py fits a fixed/variable split per (item, bidder)
across projects; a firm split four ways has its evidence split four ways too,
and small-sample fits are exactly where that hurts.

WHAT IS DELIBERATELY NOT MERGED: a differing legal suffix.  The canonical key
keeps letters and digits, so `ABC Construction LLC` and `ABC Construction Inc`
stay apart -- they may be separate entities, and nothing in a bid tab settles
it.  Nor is a shorter name merged into a longer one: `Ed Bell Construction`
(341 rows) and `Ed Bell Construction Company` (481 rows) are almost certainly
one firm, but merging on a prefix is where false merges begin, and a false
merge silently removes a real competitor from a letting.  Both stay as they are
and the ambiguity is reported instead.
"""

import re
from collections import Counter, defaultdict

# A bidder cell holding only column-header text, carrying no company identity.
# Live corpus: 6 such names over 186 rows -- `Bid Amount`, `Item 15`, `each`.
# They must not be counted as bidders; they are a failed header parse.
PLACEHOLDER = re.compile(
    r"^\s*(?:item\s*\d+\.?\s*)?"
    r"(?:bid\s*(?:amount|price|total)?|total(?:\s*bid)?|unit\s*price|amount|"
    r"extended|cost|price|each|no\.?|\$)?\s*\$?\s*$",
    re.I)

# The item number bleeding into the bidder header, e.g.
# `Item 20. A T Construction, LLC`.  This one is not cosmetic: because the
# duplicate-letting signature carries the bidder name, it cost that firm's
# entire 37-row block from the comparison and dragged a pair that is in fact
# identical down to 0.805 coverage.
LEAD_ITEM = re.compile(r"^\s*item\s*\d+\.?\s*", re.I)
# A column label left glued to the front of the company name.  The number is
# OPTIONAL on purpose: one Keller tab writes the plural with no number at all,
# `BIDDERS Rebcon LLC`, and a pattern requiring digits walks straight past it.
# The cost of missing it is not a cosmetic name -- the canonical key keeps every
# alphanumeric, so `biddersrebconllc` never joins up with `rebconllc`, and
# Rebcon's 830 rows would split in two.
LEAD_LABEL = re.compile(r"^\s*(?:bidder|contractor|vendor)s?\s*#?\s*(\d*)[.:]?\s+",
                        re.I)


def _strip_lead_label(s):
    """Remove a leading column label, but not a company that starts with one.

    `Contractors Inc` and `Bidder Services LLC` are plausible company names, and
    a pattern that strips the word unconditionally turns the first into `Inc`.
    The signal that separates them in this corpus is CASE: a column label is
    printed in capitals while the company beside it is mixed case
    (`BIDDERS Rebcon LLC`), or the label carries a number (`BIDDER 3 Road
    Solutions, LLC`).  Neither holds for a company whose own name begins with
    the word, so both survive untouched.
    """
    m = LEAD_LABEL.match(s or "")
    if not m:
        return s
    label = s[m.start():m.end()]
    rest = s[m.end():]
    if m.group(1):                       # numbered -> unambiguously a label
        return rest
    bare = re.sub(r"[^A-Za-z]", "", label)
    if bare and bare.isupper() and any(c.islower() for c in rest):
        return rest
    return s
TRAIL_HDR = re.compile(
    r"\s*(?:bid\s*amount|unit\s*price|total\s*bid|totals?|bid\s*price|amount)"
    r"\s*\$?\s*$", re.I)


# Some owners label the columns by standing, not by name: `Low Bidder ANA Site
# Construction, LLC`, `Second Low Bidder WillCo Underground`, `Third Low Bidder
# FM Utilities LLC`.  That label is the OWNER'S OWN STATEMENT of who came in
# where -- stronger evidence than any total we derive -- so it is read off and
# kept (`rank_of`) rather than merely deleted.  Leaving it glued on also splits
# the firm: `lowbidderanasiteconstructionllc` never joins `anasiteconstruction`.
_ORD = {"low": 1, "lowest": 1, "first": 1, "1st": 1,
        "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5, "sixth": 6, "6th": 6}
LEAD_RANK = re.compile(
    r"^\s*(low|lowest|first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
    r"sixth|6th)\s+(?:low\s+)?(?:bidder|bid)\b[.:#]?\s*", re.I)
# `High Bidder` says only "not the winner", so it carries no usable ordinal.
LEAD_HIGH = re.compile(r"^\s*(high|highest)\s+(?:bidder|bid)\b[.:#]?\s*", re.I)


def rank_of(name):
    """The standing the document itself asserts, or None.

    1 means the owner printed this column as the low bid.  That is a statement
    of fact by the owner, not an inference from arithmetic, so it overrides a
    derived winner wherever the two disagree.
    """
    m = LEAD_RANK.match(name or "")
    if not m:
        return None
    return _ORD.get(m.group(1).lower())


def _strip_lead_rank(s):
    s = LEAD_RANK.sub("", s or "")
    return LEAD_HIGH.sub("", s)


def is_placeholder(name):
    """True for a cell holding header text instead of a firm.

    A rank label with nothing after it (`Low Bidder`, `High Bidder` -- 21 rows
    in the live corpus) is exactly that: the tab labelled the column but the
    firm's name landed elsewhere, so there is no bidder here to count.
    """
    s = (name or "")
    if PLACEHOLDER.match(s):
        return True
    return not _strip_lead_rank(s).strip(" .,:-#\t")


def strip_header_noise(name):
    """Remove the parts that came from the column header, not the firm."""
    s = LEAD_ITEM.sub("", name or "")
    s = _strip_lead_rank(s)
    s = _strip_lead_label(s)
    s = TRAIL_HDR.sub("", s)
    return s.strip(" .,:-\t")


def key_of(name):
    """Canonical key: letters and digits of the cleaned name, lowercased.

    Dropping punctuation and spaces is what unifies `Rebcon LLC` with
    `Rebcon, LLC` and repairs the letter-spaced header in one step.  It is also
    the whole of the merging: nothing fuzzier is applied.
    """
    s = strip_header_noise(name)
    if not s or is_placeholder(name):
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def build_map(names):
    """{raw name -> display form} over an iterable of raw names.

    The display form is the spelling that appears on the most rows, so the
    canonical name is one a reader will recognise rather than a slug.  Pass a
    Counter to weight by rows; a plain iterable weights every spelling equally.
    """
    counts = names if isinstance(names, Counter) else Counter(names)
    groups = defaultdict(list)
    for n, c in counts.items():
        k = key_of(n)
        if k:
            groups[k].append((c, n))
    out = {}
    for k, members in groups.items():
        # Most rows wins; ties broken by the longer spelling, then
        # alphabetically, so the result does not depend on dict order.
        best = sorted(members, key=lambda t: (-t[0], -len(t[1]), t[1]))[0][1]
        for _, n in members:
            out[n] = best
    return out


def normalise_series(series):
    """pandas Series of raw names -> Series of canonical names.

    A placeholder becomes the empty string: it is a failed parse, and treating
    it as a bidder is what inflates n_bidders.
    """
    counts = Counter(x for x in series.fillna("").astype(str) if x)
    m = build_map(counts)
    return series.fillna("").astype(str).map(lambda n: m.get(n, ""))


# --------------------------------------------------------------------------
# Pinned cases.  The rank labels below are live corpus values; the four firms
# after them are the adversarial half -- real companies whose names begin with
# the very words the rank patterns look for.  A pattern that strips
# unconditionally turns `Lowery Construction LLC` into `ry Construction LLC`
# and splits that firm's rows in two.
_CASES = [
    ("Low Bidder ANA Site Construction, LLC", "ANA Site Construction, LLC", 1),
    ("Second Low Bidder WillCo Underground", "WillCo Underground", 2),
    ("Third Low Bidder FM Utilities LLC", "FM Utilities LLC", 3),
    ("Fourth Low Bidder Flow-Line Construction, Inc..",
     "Flow-Line Construction, Inc", 4),
    ("Lowest Bid Tiseo Paving", "Tiseo Paving", 1),
    ("Low Bidder", "", 1),            # label only -> placeholder, not a bidder
    ("High Bidder", "", None),
    ("Bid Amount", "", None),
    ("Lowery Construction LLC", "Lowery Construction LLC", None),
    ("First Choice Contracting", "First Choice Contracting", None),
    ("Highland Builders, Inc.", "Highland Builders, Inc", None),
    ("Second Street Paving Co", "Second Street Paving Co", None),
    ("BIDDERS Rebcon LLC", "Rebcon LLC", None),
    ("Contractors Inc", "Contractors Inc", None),
    ("Bidder Services LLC", "Bidder Services LLC", None),
    ("Item 20. A T Construction, LLC", "A T Construction, LLC", None),
]


def _selftest():
    ok = True
    for raw, want, rank in _CASES:
        got = strip_header_noise(raw).rstrip(".")
        gr = rank_of(raw)
        ph = is_placeholder(raw)
        good = (got == want.rstrip(".") and gr == rank and ph == (want == ""))
        if not good:
            ok = False
        print("%s %-48r -> %-30r rank=%-5s placeholder=%s"
              % ("pass" if good else "FAIL", raw, got, gr, ph))
    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
