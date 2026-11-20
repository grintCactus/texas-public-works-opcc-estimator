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


def tokens(desc):
    """The identity token set: content words plus bare numbers.

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

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
