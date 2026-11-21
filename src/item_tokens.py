"""One definition of "what words identify a pay item".

DFW municipal bid tabs defeat exact-description matching: 84.4% of their item
descriptions appear in exactly ONE letting, against 41.7% for TxDOT, which uses
standard pay-item text statewide.  Every city writes its own wording, so an
equality join finds nothing and the estimator falls back to a family median --
which is where almost all of its error lives.

But "appears once" is a fact about STRINGS.  These are the same item:

    furnish install 3ft x 3ft gabions pvc
    pvc coated 3 x 3 gabion basket furnish and in place

They agree on gabion, pvc, 3, 3.  So identity is a token SET and matching is a
Jaccard threshold, not equality.  Measured leave-one-letting-out against the
real winning totals, with the band-keyed reference as the fallback:

    pricer                 municipal   TxDOT
    band only (previous)       28.5%   30.9%
    token J>=0.35 then band    22.9%   21.7%

and coverage rises with it -- municipal lettings that can be fully priced go
from 96 to 120.

THRESHOLD = 0.35, and how it was chosen matters more than its value.  The
municipal curve is noisy (120 lettings, bootstrap 80% interval about +-8 pp)
and non-monotone, so its minimum alone is not evidence.  The TxDOT curve is
tight (1,860 lettings, +-1.6 pp) and flat: every threshold from 0.25 to 0.60 is
indistinguishable from the best.  The municipal curve, for all its noise, does
rule out the ends -- only 0.30, 0.35 and 0.40 fall inside the best one's
interval.  0.35 is the middle of what survives BOTH, not the bottom of either.

Matching is NOT restricted to item_family, deliberately.  The family classifier
is the thing being worked around, and `other` -- the catch-all that
price_reference marks unusable, carrying 23.7% of a TxDOT winning total at the
median and 2.2% of a DFW erosion one -- cannot be priced at all today.  An
`other` row whose tokens read "gabion basket pvc" should be priced from gabion
rows.  Unit stays a hard constraint: a price per SY and a price per EA are not
comparable whatever the words say.
"""
import re

THRESHOLD = 0.35

# REMOVAL IDENTITY.  remove / removal / replace / existing are stop words
# below, so "REMOVE EXISTING SANITARY SEWER PIPE" and "Install 10\" SDR-35 PVC
# Sanitary Sewer Pipe" shared {sanitary, sewer, pipe} and a removal price
# ($14/LF) could price new pipe bid at $82-155 (p50fix round 5, worst-line
# check).  In the 2024-26 municipal forward back-test removal-only lines
# summed to 1.42x their own median bids.  So a removal-only
# description (one that removes and builds nothing back) carries the flag
# token RM_FLAG, and similar() refuses a pair whose flags differ -- a hard
# constraint, like the dimensions.  The rule is the one registered for the
# defects experiment (handoff/scratchpad/defects/PREREGISTRATION.md item 1)
# and adopted after round 5 (PREREGISTRATION_ROUND5.md, variant F2).  The
# band and family tables split the same way (estimate_low_bid.family_key).
REMOVAL_IDENTITY = True
RM_FLAG = "zzremoval"
_RM = re.compile(r"\b(remov\w*|rem|demo|demol\w*|demolition)\b")
_RP = re.compile(r"\b(replac\w*|reconstruct\w*|install\w*|reset\w*|"
                 r"relocat\w*|new|rebuild\w*|reinstall\w*)\b")


def removal_only(desc):
    """A line that removes and builds nothing back: a removal word and no
    replace / reconstruct / install / reset / relocate / new / rebuild /
    reinstall word."""
    d = (desc or "").lower()
    return bool(_RM.search(d)) and not _RP.search(d)


# ABANDONMENT IDENTITY (p50fix round 17, handoff/scratchpad/p50fix/
# PREREGISTRATION_ROUND17.md).  For pipes, "remove" and "abandon" are
# different work: removal digs the pipe up and hauls it away; abandonment
# leaves it in place, plugs the ends or fills it with grout / flowable fill.
# The removal flag above covers remove-words only, so an abandon line used to
# match NEW installation in the token path ("abandon existing 8 in sanitary
# sewer pipe in place" priced from two new 8 in SDR-26 pipe cells).  An
# abandon-only description carries AB_FLAG, and similar() refuses a pair whose
# abandon flags differ -- RM_FLAG's mechanics.  ABANDON_FAMILY_KEY (AB2) also
# keys such rows apart in the band and family tables (family + "#ab",
# estimate_low_bid.family_key, build_master, train_test_guard).  Both OFF:
# production; estimate_low_bid.pricing_rule(round17_ab=True) /
# (round17_ab2=True) turn them on.
ABANDON_IDENTITY = True            # AB: ON in production since round 18
ABANDON_FAMILY_KEY = False         # AB2 (with ABANDON_IDENTITY): OFF
AB_FLAG = "zzabandon"
_AB = re.compile(r"\bab[ao]ndon\w*")            # abandon*, and the misspelling abondon*
_AB_FILL = re.compile(r"\b(?:plug\w*|grout\w*|flowable)\b")
_AB_EXIST = re.compile(r"\b(?:ex|ext|exis\w*|exiting)\b")
_AB_CUTPLUG = re.compile(r"\bcut\w*\s+(?:and\s+)?plug\w*")   # an existing line is cut
_AB_OBJ = re.compile(r"\b(?:pipe\w*|lines?|waterlines?|mains?|manholes?|mh|"
                     r"structures?|sewers?|services?|culverts?|rcp|cmp|"
                     r"box(?:es)?|inlets?|cleanouts?|vaults?|drains?|wl|ss|sd|"
                     r"w\s+l|s\s+s)\b")
_AB_NOT = re.compile(r"\b(?:plug\s*valves?|rip\s*rap\w*|riprap\w*|"
                     r"obliterat\w*|(?:re)?connect\w*)\b")


def abandon_only(desc):
    """A line that abandons existing work in place and builds nothing back:
    not removal-only (a removal-only line keeps F2's flag; the two never
    meet), no replace / reconstruct / install / reset / relocate / new /
    rebuild / reinstall word, no plug valve, riprap, obliteration or
    (re)connection word; and either an abandon word (abandon*, abondon*) or
    plug / grout / flowable-fill wording about an EXISTING pipe, line, main,
    manhole, structure, sewer, service, culvert, box, inlet, cleanout, vault
    or drain ("existing" / "ex" / "ext" / "exis*" / "exiting", or "cut (and)
    plug", which cuts an existing line)."""
    d = (desc or "").lower()
    if removal_only(d) or _RP.search(d) or _AB_NOT.search(d):
        return False
    if _AB.search(d):
        return True
    return bool(_AB_FILL.search(d)
                and (_AB_EXIST.search(d) or _AB_CUTPLUG.search(d))
                and _AB_OBJ.search(d))

# Words that appear on every other bid item and so carry no identity.  Units
# are in here too: the unit is matched exactly and separately, so repeating it
# in the token set would inflate every Jaccard score by the same amount.
STOP = set("""
furnish furnished furnishing install installed installing installation
place placed placing complete completed completely
remove removal removing replace replacement replacing
provide provided providing construct construction constructed
new existing misc miscellaneous kind size type class
std standard detail details plan plans spec specs
specification specifications required req shall
item items unit price total sum lump allowance contingency
work works job labor material materials equipment
and or the per each all any for with without
including include included
lin ft foot feet sq yard yards cubic
sy cy lf sf ls ea gal ton tons day days mo month hr hour
site as is are be a an on at to by from of in
""".split())
_NUM = re.compile(r"\d+(?:\.\d+)?$")


def _fold(w):
    """Fold a plural, without mangling a word that already ends in -ss.

    `mattresses` -> `mattress` but `mattress` -> `mattres` would make the same
    word fail to match itself written two ways, which is exactly what the
    module is for.  Self-test 7 pins it.
    """
    # The sibilant plurals take -es and must lose both letters, or
    # `mattresses` folds to `mattresse` and never meets `mattress`.  Order
    # matters: -sses is checked before -ss, or the first rule never fires.
    if len(w) > 4 and w[-4:] in ("sses", "xes", "ches", "shes"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("ss"):
        return w
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def tokens(desc, removal=None):
    """The identity token set: content words plus bare numbers.

    `removal` (default: REMOVAL_IDENTITY) adds RM_FLAG to a removal-only
    description.  The comparables method (comparables.py) matches lines on
    the identity it was registered with, which has no removal flag, so it
    passes removal=False.  AB_FLAG (an abandon-only description) follows
    ABANDON_IDENTITY by default; removal=False gives no abandon flag either,
    removal=True gives it when ABANDON_IDENTITY is on.

    Numbers are kept -- they are the dimensions, and 18-inch pipe is not
    24-inch pipe.  `3.0` folds to `3`, and a glued dimension is split at the
    letter/digit boundary so `3ft` meets a plain `3`: without that split the
    two gabion wordings in self-test 1 score 0.333 and miss the threshold by a
    hair, on nothing but a unit suffix.  Words under three characters go: they
    are almost all noise, and the ones that are not (a bare unit) are matched
    separately.
    """
    out = set()
    raw = re.split(r"[^A-Za-z0-9.]+", (desc or "").lower())
    for part in raw:
        if not part:
            continue
        # split a glued dimension: 3ft -> 3, ft ; 18in -> 18, in
        for w in re.findall(r"\d+(?:\.\d+)?|[a-z]+", part):
            if not w:
                continue
            if _NUM.match(w):
                try:
                    f = float(w)
                except ValueError:
                    continue
                out.add(str(int(f)) if f == int(f) else w)
                continue
            if len(w) < 3 or w in STOP:
                continue
            w = _fold(w)
            if w and w not in STOP:
                out.add(w)
    # the flag joins only a set that already has content words: a bare
    # "remove existing" must not become an identity of its own
    if (REMOVAL_IDENTITY if removal is None else removal) and out \
            and removal_only(desc):
        out.add(RM_FLAG)
    if ABANDON_IDENTITY and (removal is None or removal) and out \
            and abandon_only(desc):
        out.add(AB_FLAG)
    return frozenset(out)


def numbers(t):
    """The dimension tokens inside a token set."""
    return frozenset(x for x in t if _NUM.match(x))


def signature(desc):
    """A stable, writable form of the token set, for a CSV key."""
    return " ".join(sorted(tokens(desc)))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    n = len(a & b)
    return n / (len(a) + len(b) - n) if n else 0.0


def similar(a, b, thresh=THRESHOLD):
    """Jaccard, with DIMENSIONS as a hard constraint.

    Plain Jaccard cannot say that the number matters more than the other words,
    and on this corpus that is fatal: "18 in rcp storm sewer pipe" against
    "24 in rcp storm sewer pipe" scores 0.667 on four shared trade words and
    would be priced as the same item, when a 24-inch pipe is not an 18-inch
    one.  So when both descriptions carry dimensions, at least one must agree.
    A side with no number at all is not blocked -- plenty of real items have
    none, and refusing those would just push them back to the family median.
    """
    na, nb = numbers(a), numbers(b)
    if na and nb and not (na & nb):
        return 0.0
    if REMOVAL_IDENTITY and ((RM_FLAG in a) != (RM_FLAG in b)):
        return 0.0
    if ABANDON_IDENTITY and ((AB_FLAG in a) != (AB_FLAG in b)):
        return 0.0
    return jaccard(a, b) if jaccard(a, b) >= thresh else 0.0


def _selftest():
    ok = True

    def chk(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("FAIL %-46s got %r want %r" % (name, got, want))
        else:
            print("pass %-46s %r" % (name, got))

    # the pair this whole module exists for
    a = tokens("furnish install 3ft x 3ft gabions pvc")
    b = tokens("pvc coated 3 x 3 gabion basket furnish and in place")
    chk("1 the two gabion wordings clear the threshold",
        similar(a, b) > 0, True)
    chk("1 and they are not identical", a == b, False)

    # dimensions must NOT be folded away: these are different items
    c = tokens("18 in rcp storm sewer pipe")
    d = tokens("24 in rcp storm sewer pipe")
    chk("2 18in vs 24in pipe is refused on the dimension",
        (round(jaccard(c, d), 3), similar(c, d)), (0.667, 0.0))
    chk("2 3.0 folds to 3", "3" in tokens("3.0 ft gabion"), True)

    # plurals and boilerplate
    chk("3 plurals fold", tokens("gabions") == tokens("gabion"), True)
    chk("4 boilerplate alone yields nothing",
        tokens("furnish and install complete in place per plans"), frozenset())
    chk("5 the unit is not a token", "sy" in tokens("riprap sy"), False)

    # genuinely different trades must not meet
    e = tokens("hydromulch seeding bermuda")
    f = tokens("pvc coated gabion basket")
    chk("6 unrelated trades do not match", similar(e, f), 0.0)

    # a real pair from the corpus that SHOULD match
    g = tokens("gabion mattresses galv 18 in")
    h = tokens("galvanized gabion mattress 18 inch thick")
    chk("7 mattress wordings match", similar(g, h) > 0, True)

    # and one that should not: same family, different material
    i = tokens("gabion mattresses galv 12 in")
    j = tokens("gabion mattresses pvc coated wire 12 in")
    chk("8 galv vs pvc on the same item still matches (same work, "
        "different coating)", similar(i, j) > 0, True)

    # the fold rule, pinned directly: both of the earlier bugs were here
    for a_, b_ in (("mattresses", "mattress"), ("mattress", "mattress"),
                   ("gabions", "gabion"), ("gabion", "gabion"),
                   ("boxes", "box"), ("box", "box"),
                   ("houses", "house"), ("house", "house"),
                   ("trenches", "trench"), ("trench", "trench"),
                   ("class", "class")):
        chk("fold %-11s -> %s" % (a_, b_), _fold(a_), b_)

    chk("9 signature is sorted and stable",
        signature("PVC coated 3 x 3 gabion basket"),
        " ".join(sorted(tokens("basket gabion 3 3 coated pvc"))))

    # removal identity: removal-only work never prices new work, nor back
    k_ = tokens("remove existing sanitary sewer pipe")
    m_ = tokens("install 10 in sdr 35 pvc sanitary sewer pipe")
    chk("10 removal-only line carries the flag", RM_FLAG in k_, True)
    chk("10 removal price refused for new pipe (was 0.43)",
        similar(m_, k_), 0.0)
    chk("10 two removal wordings still match",
        similar(k_, tokens("removal of existing sanitary sewer pipe")) > 0,
        True)
    chk("10 remove-and-replace is not removal-only",
        RM_FLAG in tokens("remove and replace 6 in concrete pavement"), False)
    chk("10 a bare removal phrase stays empty", tokens("remove existing"),
        frozenset())
    chk("10 removal=False gives the flag-free identity (comparables)",
        RM_FLAG in tokens("remove existing sanitary sewer pipe", removal=False),
        False)

    # 11 abandonment identity (round 17): the rule on the data's own wordings
    # (desc_norm of the external price pool; round17/prereg_explore), then the
    # flag and the refusal.  ABANDON_IDENTITY is switched on here and restored.
    global ABANDON_IDENTITY
    old11 = ABANDON_IDENTITY
    pos11 = ("abandon existing manhole", "cut plug abandon existing water line",
             "abandon existing 8 in water line", "grout abandonment of ex 6 in s s",
             "8 in sanitary sewer abandonment plug", "16 in water abandonment plug",
             "abandon existing sanitary sewer pipe", "cut plug existing water line",
             "cut and plug existing 12 in water line", "cut plug ex 12 in w l",
             "cut plug ex 8 in s s", "plug existing rcp all sizes",
             "grout existing 8 in pvc sanitary sewer main",
             "grout existing junction box",
             "f i flowable fill in existing 6 in force main",
             "cap and plug existing 6 in sewer service fully grout",
             "plug existing 15 in sd line", "cut and plug water main",
             "grouting sewer main to be abondoned cip",
             "site preparation abandon existing water lines cut plug and grout "
             "fill 10 in",
             # the brief's lines (raw wording)
             "Abandon existing 8 inch sanitary sewer pipe in place",
             "Grout fill and abandon existing 8 inch water line",
             "Plug existing 8 inch pipe")
    neg11 = ("obliterating abandoned road", "riprap stone common grout 12 in",
             "18 in grouted rock riprap", "12 in plug", "24 in pressure plug",
             "36 in rcp plug for future connection", "furnish install 12 in plug",
             "plug proposed 8 in water line", "flowable fill", "flowable backfill",
             "12 in pvc water pipe with fittings and modified flowable fill as "
             "backfill open cut",
             "12 in dia pvc water carrier pipe with fittings and annular grout",
             "water line grouting", "remove existing sanitary sewer pipe",
             "abandon/remove existing 24 in water line",
             "install 1 in domestic water service poly by open cut abandon ex",
             "furnish labor material to connect to existing force main and "
             "abandoned existing force main in place",
             "abandon backfill wastewater manhole install standard 4 foot "
             "diameter manhole 8 ft deep",
             "8 in pvc sdr 26 sanitary sewer pipe by open cut",
             "class g concrete cap for existing 12 in water main",
             "cut plug ex 6 in 8 in water",
             # the brief: a plug valve is new work (no such wording in the pool)
             '8" Plug Valve', "8 in plug valve")
    chk("11 abandon-only: the %d positive wordings" % len(pos11),
        [d for d in pos11 if not abandon_only(d)], [])
    chk("11 not abandon-only: the %d negative wordings" % len(neg11),
        [d for d in neg11 if abandon_only(d)], [])
    ABANDON_IDENTITY = False
    try:
        off11 = AB_FLAG in tokens(
            "abandon existing 8 in sanitary sewer pipe in place")
    finally:
        ABANDON_IDENTITY = old11
    chk("11 off: no abandon flag", off11, False)
    ABANDON_IDENTITY = True
    try:
        ab11 = tokens("abandon existing 8 in sanitary sewer pipe in place")
        nw11 = tokens("8 in pvc sdr 26 sanitary sewer pipe by open cut")
        chk("11 on: the abandon line carries the flag, new pipe does not",
            (AB_FLAG in ab11, AB_FLAG in nw11), (True, False))
        chk("11 on: abandon price refused for new pipe and back (was 0.4)",
            (similar(ab11, nw11), similar(nw11, ab11)), (0.0, 0.0))
        chk("11 on: two abandon wordings still match", similar(
            ab11, tokens("abandon existing sanitary sewer line")) > 0, True)
        chk("11 on: a removal-only line keeps RM, never AB", (
            RM_FLAG in tokens("abandon/remove existing 24 in water line"),
            AB_FLAG in tokens("abandon/remove existing 24 in water line")),
            (True, False))
        chk("11 on: removal=False (comparables) has no abandon flag",
            AB_FLAG in tokens("abandon existing manhole", removal=False), False)
        chk("11 on: 'abandon existing' = the word abandon + the flag",
            tokens("abandon existing"), frozenset({"abandon", AB_FLAG}))
    finally:
        ABANDON_IDENTITY = old11

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
