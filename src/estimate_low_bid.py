"""The estimator: market median -> expected low bid, with a calibrated interval.

Everything else in this project produces a piece of this.  price_reference.csv
gives the market level, price_by_quantity_band_family.csv the quantity shape,
price_time_index.csv / index_by_market.csv the escalation,
competition_discount_municipal.csv and _pooled.csv the competition discount.
This joins them, and the joining is where the mistakes live.

THE DISCOUNT GOES ON THE TOTAL, NOT ON EACH UNIT PRICE.

That is measured, not assumed.  The discount was calibrated as a project-level
order statistic -- the winner's total over the median bidder's total -- so
spreading it across unit prices assumes the winner discounts every line item
alike, and that assumption was tested rather than argued.

Two corrections to how this was first written, both found by adversarial
verification and both worth keeping on the record.  First, the two routes were
NOT given an identical price level: they differed by a median 2.16%, so the
original framing ("holding the price level exact") overstated the test.
Second, the item-level ratios once cited as the mechanism -- 0.24 for mowing
per mile, 0.61 for lump-sum mobilisation, 0.82 for subgrade stabilisation, out
of unbalanced_bidding.csv -- are COMPOSITION ARTEFACTS, not within-letting
discounts.  Measured inside a letting the same three are 0.951 / 0.875 / 0.945.
The dramatic numbers came from comparing a winner's items against a different
mix of other bidders' items.

The conclusion survives both corrections, re-run on the 831 lettings where
both routes are computable and fed the identical per-item price level:

    route                              lettings   est/actual   median |error|
    total x f(n_bidders)                    831       0.9861            6.5%
    unit price x family ratio               831       0.9956            8.4%
    no discount at all                    2,040       1.1560           15.6%

Total placement wins on 517 of those 831 lettings, sign test p = 1.9e-12, and
still wins when route B is re-armed with out-of-fold family ratios.  It also
wins on coverage: route B can fully price only 41% of lettings, and 7 of the
157 municipal ones, because the family ratios are TxDOT-derived and transfer
to municipal work at r = 0.211.  Leaving the discount out is not an option --
it costs a systematic +15.6%.  unbalanced_bidding.csv answers a different
question, which line items a winner loads, and is not an input here.

One thing the placement test cannot show: this advantage is measured with the
price level known.  End to end it is the PRICE LEVEL, not the placement, that
dominates -- see the interval section below.

MUNICIPAL WORK USES THE MUNICIPAL DISCOUNT -- FOR ITS CENTRE, NOT ITS SPREAD.

This claim was published wrong first and the correction is the interesting
part.  The original read: municipal table 0.9973 / 7.1% median error against
pooled 1.0175 / 7.6%, on 157 municipal lettings.  That comparison was circular.
156 of those 157 lettings were in the 278 the municipal table was fitted from,
and the headline was not even a measurement: with f_bin set to the median of
the same ratios it is scored against, median(f_bin / ratio_i) is exactly 1.0000
by construction.  The 0.9973 was a memorised 1.0.

Measured out of fold instead, each test letting removed from the fit:

    discount table                   est/actual   median |error|
    nine-bin municipal                   0.9975            7.09%
    pooled x one municipal shift         1.0016            7.59%
    pooled, unshifted                    1.0230            6.41%

So: the municipal table buys an UNBIASED CENTRE and costs about 0.7 points of
spread.  It does not buy accuracy -- the unshifted pooled table has the lowest
error of the three.  That trade is taken deliberately, because a bid estimate
that runs 2.3% high loses jobs it would have won, while a slightly wider spread
around the right centre does not.

The nine per-bidder-count bins are gone.  Over 400 half-splits the one-shift
version beat them on error in exactly 200 of 400 -- a coin flip -- so the nine
parameters, fitted on as few as 7 lettings each, bought nothing that one
parameter fitted on all 278 does not.  competition_discount_municipal.csv is
now the pooled table times a single measured factor (0.9775), with n=1 forced
to 1.0 because a sole bid has no order statistic to discount.

That the two markets differ at all is separately supported and survives: at
matched bidder counts the municipal ratio is lower in 9 of 9 bins (sign test
p = 0.0039; stratified Wilcoxon over 270 lettings p = 4.4e-07).  Municipal bids
are more dispersed (sigma 0.151 vs 0.143) and the low bid is an order
statistic, so more dispersion pushes the minimum further below the median.

THE INTERVAL IS THE WHOLE PIPELINE'S ERROR, NOT THE DISCOUNT'S.

Not an assembled sigma either: combining a price-level sigma with a discount
sigma would need their correlation and would double-count the part of the price
error that averages out across a schedule.  `calibrate()` measures realised
est/actual quantiles and `estimate()` inverts them, so the interval is
calibrated by construction and moves when the data moves.

The first version of this calibrated on the DISCOUNT ALONE -- it handed each
letting its own median bidder total as the price level, exact, and measured
only what f(n) did to it.  That gave 6.9% median error and an interval to
match, and the interval was wrong, because a real estimate does not know the
letting's own totals.  It substitutes family medians out of price_reference for
every line item, and that substitution carries far more error than the discount
does.  Running the full path on a real 144-item DFW stormwater letting returned
$2,949,114 against an actual winning total of $2,150,000 -- 37% high, outside
the 80% band the discount-only calibration had printed.

So `calibrate()` now runs the whole path: it rebuilds each real letting's
schedule from the panel, prices every item from price_reference exactly as a
user would, totals it, applies f(n), and compares against the identified
winner's total.  `mode="discount_only"` keeps the old measurement, because the
two together say something neither says alone -- the gap between them is the
price-level substitution error, which is where the accuracy is actually lost
and therefore where the next work belongs.

Both are IN-SAMPLE on the price level: a letting's own rows helped form the
family medians it is then priced against, so the full-path number is optimistic
too.  It is reported as a floor on the error, not as the error.

A price already on a winning or contract basis must never be discounted again.
price_reference.csv is built from `comp` in build_master.py, which is 598,326
rows all carrying price_basis='all_bids' -- no contract prices, so the discount
applies.  `assert_all_bids_basis()` re-checks that at load time instead of
trusting this comment.
"""
import bisect
import collections
import contextlib
import csv
import datetime as _dt
import hashlib
import math
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comparables  # noqa: E402
import coverage as coverage_rule  # noqa: E402  (src/coverage.py, the "not covered" rule)
import item_tokens  # noqa: E402
import qty_shape  # noqa: E402

assert hasattr(coverage_rule, "uncovered_class"), (
    "src/coverage.py is shadowed by another module named 'coverage'")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")

MUNICIPAL_SOURCES = {"city_bidtab"}
# A bidder count this far from the nearest calibrated bin is not covered.  The
# discount flattens above ~8 bidders, so extrapolating two bins is tolerable
# and extrapolating five is not.
MAX_BIN_DISTANCE = 2
# ROUND 16 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND16.md, queue
# M155): a bidder count ABOVE the table's highest bin is priced off that bin
# (marked extrapolated) instead of refused.  The discount is flat at the
# many-bidder end, and the refusal gave a 17-bidder city letting no estimate
# at all.  The n <= 1 rule and every other refusal -- below the lowest bin,
# a gap of more than MAX_BIN_DISTANCE inside the table, no table -- are
# unchanged.  OFF is production; pricing_rule(clamp_high_bidders=True) or
# its preset round16_c (holdout_eval --compare-switch round16_c) turns it on.
CLAMP_ABOVE_TOP_BIN = True         # C: ON in production since round 18
ROUND16_C = ("clamp_high_bidders",)  # the preset pricing_rule(round16_c=True)

# A letting is scored only when its schedule has at least this many items.  A
# one-line tab is a total-only or lump-sum award, not an item schedule, and the
# estimator's input IS an item schedule: the two public lettings that scored
# 119x and 31x out of fold (Mesquite 2022-062, Collin 2023-138 DRAFT) each
# parsed to a single item (reports/opcc_benchmark.md, section 6, item 14).
MIN_SCHEDULE_ITEMS = 3

# OWNER-SET MONEY: allowances, contingencies, force accounts.  The owner writes
# the amount into the bid form and every bidder copies it, so it is not a price
# anybody bid.  Measured on the public tabs: of 224 allowance-worded items quoted
# by two or more bidders, 200 carry one identical amount (185 of 202 in the
# parser's `contingency` family); most of the rest are percentage contingencies
# or ordinary items that merely say "allow".  Pricing such a line off OTHER
# projects' allowances returns some other owner's figure: one tab writes an
# allowance of N dollars as N x $1 under unit ALLOW, and pricing N units at the
# family median per ALLOW unit (over $11,000) made it hundreds of millions of
# dollars.  So an owner-set amount is taken at face
# value and kept OUT of the competition discount, and an allowance-like line
# with no amount is priced as before but flagged.  No rule reads a quantity as
# dollars: that convention occurs in 0 of the 72 public ALLOW items, so the
# amount has to be given, never guessed.
ALLOWANCE_UNITS = {"ALLOW", "ALLOWANCE", "ALW", "ALLW", "DOL"}
ALLOWANCE_RE = re.compile(
    r"\b(?:allowances?|contingenc\w*|force\s*accounts?|cash\s+allow\w*)\b",
    re.I)


def allowance_like(family, unit, description=""):
    """An item worded or filed as owner-set money."""
    return ((family or "") == "contingency"
            or (unit or "").strip().upper() in ALLOWANCE_UNITS
            or bool(ALLOWANCE_RE.search(description or "")))


def is_owner_fixed(unit_prices, family, unit, description=""):
    """Owner-set money recognised from the bids themselves: two or more bidders
    quoting one identical amount on an allowance-like line.  Used where the
    bids are known (the back-tests); a user's schedule says so by giving the
    amount instead."""
    ps = [p for p in unit_prices if p]
    return (len(ps) >= 2 and max(ps) - min(ps) <= 0.01
            and allowance_like(family, unit, description))


def owner_fixed_amount(item):
    """The face value of an owner-set line in a schedule, or None.

    Taken only when the schedule gives it: an `amount`, or a unit_price on a
    line that is allowance-like (or marked owner_fixed).  A unit price on an
    ordinary line is ignored -- prices are estimated, never read off the input.
    """
    amt = _f(item.get("amount"))
    if amt and amt > 0:
        return float(amt)
    up = _f(item.get("unit_price"))
    q = _f(item.get("quantity"), 0.0) or 0.0
    if up and up > 0 and q > 0 and (
            item.get("owner_fixed")
            or allowance_like(item.get("family"), item.get("unit"),
                              item.get("description") or "")):
        return float(up) * q
    return None


# REMOVAL IDENTITY in the band and family tables (item_tokens.REMOVAL_IDENTITY
# for the token table).  A removal-only row is filed under family + "#rm", so
# removal work and the same family's new work never share a cell: a removal
# line is priced only from removal history and new work only from new work.
# The quantity-shape table is still read by the original family.  A removal-
# only line whose own #rm cells are all empty is not priced as new
# construction: it gets the schedule's typical line amount (estimate()).
# Pre-registered as variant F2 of p50fix round 5 (handoff/scratchpad/p50fix/
# PREREGISTRATION_ROUND5.md, the rule of defects/PREREGISTRATION.md item 1)
# and chosen under its rule on the 2026-11-20 snapshot: municipal 2024-26
# median P80 overshoot 69.0% -> 60.4%, P80 under 15.5% -> 16.1%, mean |ln|
# 0.435 -> 0.420 (90% interval -0.028 to -0.003); TxDOT 2023-26 51.3% ->
# 48.8%, 16.3% -> 16.1%, 0.370 -> 0.362.
REMOVAL_SUFFIX = "#rm"
# ABANDONMENT IDENTITY, AB2 (round 17; item_tokens.ABANDON_FAMILY_KEY with
# ABANDON_IDENTITY, both OFF by default): an abandon-only row is filed under
# family + "#ab", so abandonment and the same family's new work never share a
# band or family cell; a line with no "#ab" cell of its own gets the typical
# line amount, as a removal-only one does.
ABANDON_SUFFIX = "#ab"


def family_key(family, description=""):
    """The family as the band and family tables key it."""
    if item_tokens.REMOVAL_IDENTITY and item_tokens.removal_only(
            description or ""):
        return (family or "") + REMOVAL_SUFFIX
    if item_tokens.ABANDON_IDENTITY and item_tokens.ABANDON_FAMILY_KEY and \
            item_tokens.abandon_only(description or ""):
        return (family or "") + ABANDON_SUFFIX
    return family


def _base_family(fk):
    """family_key() undone: the classifier's own family."""
    return fk.split("#")[0] if fk else fk


# V2+F1+F2 (adopted 2026-11-20).  Selected by p50fix round 5 on data
# snapshot 3 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND5.md with
# addendum R5-1; reports/p50_fix_round5_snap3.md) and confirmed on the locked
# public test set (Registration 3, data/test_holdout/REGISTRATIONS.md): P80
# under the low bid 19.6% (<= 20%), median overshoot 35.9% -> 31.0%, P50
# median error 18.4% -> 13.6%.  Three parts:
#   F2  removal identity (family_key above, item_tokens.REMOVAL_IDENTITY);
#   F1  size fallback (SIZE_FALLBACK, SizeCells, item_market_price);
#   V2  comparables blend (COMPARABLES_BLEND, comparables.py, estimate()).
# The switches exist so the rule before the change can be reproduced (the
# self-tests' control, and holdout_eval's decoy draw); production runs with
# all three on.
SIZE_FALLBACK = True
COMPARABLES_BLEND = True
SIZE_K_MIN_SIZES, SIZE_K_MIN_LETTINGS = 5, 20       # registered (round 5)
SIZE_K_ONE = ("water_pipe", "sanitary_pipe", "storm_pipe")   # k = 1, else 0

# T1 (TxDOT only): round 1's C4 -- an exact word-set cell with >= 5 lettings
# prices a line instead of the weighted median of every match, and
# mobilization is priced as a share of the rest of the schedule -- plus
# round 2's D1, the competition discount by bidder class x the model's own
# pre-discount size, also inside V2's competition adjustment (round 3's
# V2+C4+D1 rules, on top of V2+F1+F2).  Pre-registered and selected by p50fix
# round 6 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND6.md, md5
# ddc945cb...) on data snapshot 4, forward: TxDOT 2023-26 median P80
# overshoot 37.2% -> 31.3%, P80 under 16.7% -> 16.0%, mean |ln| 0.269 ->
# 0.242 (90% interval -0.034 to -0.020); municipal untouched.  The locked
# public test set is municipal only, so T1 has no locked-set confirmation;
# judge it on TxDOT lettings bid after 2026-11-20.  TxDOT bridge
# maintenance (72 lettings) went 20.8% -> 26.4% under.
TXDOT_C4D1 = True
C4_MIN_N = 5                       # exact cell: Jaccard 1 and >= 5 lettings
MOB_MIN_LETTINGS = 20              # mobilization ratio: >= 20 earlier lettings
D1_MIN = 10                        # D1 cell: >= 10 earlier scored lettings
D1_SIZE_EDGES = (250e3, 1e6, 5e6)
T1_FILE = "estimator_t1_txdot.csv"  # D1 records + model totals (calibrate_forward)

# COVERAGE SCOPE (user decision 2026-11-20; the line rule is src/coverage.py,
# md5 5d14769b..., fixed before any scoring under it).  The user:
# "If it is a pump, generator or prefab bridge -- something we have almost nothing comparable for --
# we call it not covered and flag it in the quote ... for those we give a price plus 'not covered', since
# any price for an uncovered item is unreasonable ... when judging ... take the uncovered part out and see how accurate the price is."
# With COVERAGE_SCOPE on, a line coverage.uncovered_class() names is NOT priced:
#   * no line price; it is out of the line total, the bid part, V2's
#     comparables match (the schedule's own lines) and T1's mobilization
#     share (the schedule's base, and the bids MobRatio fits the ratio on);
#   * estimate() lists it under `uncovered` (item no, description, class,
#     quantity, unit, "Not covered -- get a separate quote"), with uncovered_share_lines and a
#     warning put first; P50/P80 cover the other lines only;
#   * an amount the user gives for it (a vendor quote: `quote`, or `amount`
#     as owner_fixed_amount reads it) is taken at face value, outside the
#     discount and the centring; once every uncovered line has one, a
#     COMPLETE total (covered P50/P80 + the quotes) is shown beside them;
#   * an allowance-like line whose amount is given stays owner-set money
#     whatever its words (nobody bids it, so it needs no comparable).
# Back-tests (calibrate_forward, holdout_eval) score the covered estimate
# against the low bid minus the LOW BIDDER's own amounts on the uncovered
# lines (actual_low; the per-line median bidder amount is kept as
# actual_low_med for a sensitivity replay); the 90%-priced rule counts the
# covered lines; centring, P80 divisor and guard are fitted on those covered
# records; a not_covered letting is neither scored nor calibrated on.
#
# NOT COVERED AS A WHOLE -- one rule, fixed 2026-11-20 from the line
# descriptions alone, before any scoring under it.  The estimate is
# not_covered (no P50/P80 at all, only the warning) when
#   (a) the uncovered lines are MORE than half of the schedule's lines, or
#   (b) an uncovered line priced per LS IS the facility: its description
#       names a pump / lift / booster station, a treatment plant or a
#       storage tank (WHOLE_FACILITY_RE), and every other word in it is a
#       completeness or filler word (WHOLE_FACILITY_WORDS: "complete",
#       "in place", "construct", "new", "rehabilitation", "improvements",
#       "upgrade", "and substructure", numbers, ...).  "Lift station
#       complete in place", "complete pump station", "lift station
#       rehabilitation", "ground storage tank 1.0 mg" are the facility;
#       "lift station odor control", "pump station control and scada",
#       "lift station site work", "paint ... elevated storage tank" are not.
# Why: then the covered lines are only the periphery (mobilization, site
# work, bypass pumping) of a job whose core has no comparable, and a P50/P80
# of the periphery would be read as the price of the job -- the quote the
# user calls unreasonable.  (b) is kept narrow on purpose: a miss still
# leaves the covered estimate under the "not covered" list and the warning, while a
# false not_covered withholds a usable estimate; so a line that adds any
# other word (a component, a station's proper name, "site work") is not the
# facility, and specialty BUILDINGS are left out of (b) -- a restroom or an
# electrical building is one structure on a larger site.  On the snapshot-5
# descriptions (prices not read): 19 municipal and 5 TxDOT lettings carry
# uncovered lines; (a) catches none of them, (b) two municipal ones ("lift
# station complete in place", one LS line of 248; "lift station and
# substructure").  The rule counts lines, not dollars (no bid is known when
# a schedule is estimated), so a schedule whose few uncovered lines are most
# of its money stays covered -- with the "not covered" list and the warning.
COVERAGE_SCOPE = True
COVERAGE_STATUS = "Not covered -- get a separate quote"
LS_UNITS_COVERAGE = ("LS", "L.S.", "LUMP SUM")
WHOLE_FACILITY_RE = re.compile(
    r"\b(?:(?:pump|lift|booster(?:\s+pump)?)\s+stations?|"
    r"(?:water\s+|wastewater\s+)?treatment\s+plants?|wwtp|wtp|"
    r"(?:(?:elevated|ground)\s+(?:storage\s+)?|storage\s+)tanks?)\b")
WHOLE_FACILITY_WORDS = frozenset((
    "complete", "completed", "in", "place", "turnkey", "turn", "key", "all",
    "and", "the", "a", "an", "of", "for", "at", "to", "with", "including",
    "incl", "new", "construct", "construction", "constructed", "install",
    "installed", "installation", "furnish", "furnished", "rehabilitate",
    "rehabilitation", "rehab", "improvement", "improvements", "replace",
    "replacement", "upgrade", "upgrades", "existing", "proposed", "no",
    "number", "structure", "substructure", "appurtenances", "appurtenance",
    "mg", "mgd", "gal", "gallon", "gallons", "ls", "lump", "sum"))


def _fold_desc(d):
    """A description folded as the parser's desc_norm ends (lower case,
    a-z 0-9 / only, single spaces); desc_norm itself comes back unchanged."""
    s = re.sub(r"[^a-z0-9 /]", " ", str(d or "").lower())
    return re.sub(r"\s+", " ", s).strip()


_COV_MEMO = {}


def line_coverage(desc, family="", unit=""):
    """coverage.uncovered_class() on a line (its description folded), or
    None when the line is covered or COVERAGE_SCOPE is off.  Memoised."""
    if not COVERAGE_SCOPE:
        return None
    k = (desc or "", family or "", unit or "")
    c = _COV_MEMO.get(k, 0)
    if c == 0:
        c = _COV_MEMO[k] = coverage_rule.uncovered_class(
            _fold_desc(desc), family or "", unit or "")
    return c


# Rule (b) is OFF (main session, 2026-11-20 ~11:00, before the round-10 re-run
# under this scope): in the integration review it voided Mesquite 25-8014 --
# 248 lines, $34M -- for its single "lift station complete in place" line.  The
# user's principle -- price what can be priced, list the uncovered lines, judge
# the covered part -- holds for a facility-centred job too: its covered lines
# are priced, its station is listed as not covered with the warning on top.  Rule (a)
# (uncovered lines are more than half the schedule) stays.  is_whole_facility()
# is kept, and reported per uncovered line as `whole_facility`.
WHOLE_FACILITY_RULE = False


def is_whole_facility(desc, unit=""):
    """Rule (b) of NOT COVERED AS A WHOLE, for an uncovered line: priced
    per LS, names a station / plant / tank, and nothing else but
    completeness and filler words."""
    if (unit or "").upper().strip() not in LS_UNITS_COVERAGE:
        return False
    d = _fold_desc(desc).replace("/", " ")
    if not WHOLE_FACILITY_RE.search(d):
        return False
    rest = WHOLE_FACILITY_RE.sub(" ", d).split()
    return all(w in WHOLE_FACILITY_WORDS or w.isdigit() for w in rest)


def uncovered_quote(item):
    """A user's amount for an uncovered line (a vendor quote): `quote`, else
    what owner_fixed_amount() reads (`amount`, or a unit price on a line
    marked owner_fixed).  None when not given."""
    v = _f(item.get("quote"))
    if v and v > 0:
        return float(v)
    return owner_fixed_amount(item)


# MC, MUNICIPAL REFERENCE CELLS (municipal lines only).  The reference tables
# pool both markets, and per cell the median of per-letting medians follows
# whichever market has more lettings -- usually TxDOT -- so municipal lines
# were priced at TxDOT levels (reports/diag_worst_municipal_2026-11-20.md,
# section 0: seeding SY $0.30 vs municipal $2.50, backfill CY $318 vs $86,
# mobilization LS $120k vs $60k).  A municipal line now walks the same tiers
# in the same order, but wherever a cell is read it takes that key's
# MUNICIPAL-ONLY cell when it has >= MUNI_CELLS_NMIN municipal lettings
# (forward: before the letting's date), else the pooled cell as before.
# Which key a tier uses is still decided on the pooled table; the whole row
# (price, quantity median, vintage, counts) is the municipal one.  F1's
# size_ref and slope stay pooled.  TxDOT lines are untouched.
#
# ROUND 10 "CORE" = MC + E + F1P + T2 + D (municipal lines only; TxDOT
# untouched).  Pre-registered in handoff/scratchpad/p50fix/
# PREREGISTRATION_ROUND10.md (md5 0eccbdc5...); re-run as registered
# (Registration 5) on data snapshot 6 (master_panel ed742d89...) under the
# coverage scope (round10b/): MC, CORE and CORE+LS all passed (a)(b)(c) and
# the tie-break chose CORE.  Forward, municipal 2024-26 (142 lettings both
# score): median P80 overshoot 49.0% -> 42.0%, P80 under 18.3% -> 18.3%,
# P50 median error 24.1% -> 19.8%, mean |ln| -0.029 (90% interval -0.048 to
# -0.011); TxDOT identical.  EVERY SWITCH IS OFF until the locked-set
# confirmation (Registration 5, holdout_eval --compare-switch round10_core)
# passes; pricing_rule(round10_core=True) turns on exactly these five:
#   MC   MUNI_CELLS               municipal-only reference cells (above)
#   E    comparables.MUNI_REMOVAL_IDENTITY  the municipal comparables pool
#        and the municipal schedules carry F2's removal flag: a removal-only
#        line only matches removal lines, and no other line matches one
#   F1P  F1_PIPE_ONLY             on a municipal line F1's size fallback runs
#        only for pipe-like families (F1P_FAMILIES; pipe_fittings only when
#        its size is a diameter, _f1p_diameter); other lines are priced as
#        before F1 existed
#   T2   FAMILY_CELL_T2           on a municipal line a tier-2 family-table
#        cell backed by < T2_MIN lettings is not used (direct cell, unsized
#        fallback, F1 candidates); the line falls through, else stays unpriced
#   D    IDENTICAL_BID_OWNER_SET  back-tests only: a municipal schedule line
#        is owner-set money also when >= D_MIN_BIDDERS distinct bidders quote
#        one identical unit price (max - min <= 0.01) on an LS / allowance-like
#        line (identical_bid_owner_set)
MUNI_CELLS = False                 # MC: OFF until the confirmation passes
MUNI_CELLS_NMIN = 3                # municipal cell: >= 3 municipal lettings
F1_PIPE_ONLY = False               # F1P: OFF until the confirmation passes
F1P_FAMILIES = ("storm_pipe", "water_pipe", "sanitary_pipe", "encasement")
F1P_FITTINGS = "pipe_fittings"
F1P_DIAM_RE = re.compile(r"(?<![\d/.])(\d+(?:\.\d+)?)\s*(?:in\b|inch|\")")
F1P_PIPE_RE = re.compile(r"\b(?:pipe|main|tee|bend|elbow|reducer|cap|plug|"
                         r"cross|wye|sleeve|coupling|fittings?|dia|diameter)\b")
FAMILY_CELL_T2 = False             # T2: OFF until the confirmation passes
T2_MIN = 2                         # a family-table cell needs >= 2 lettings
IDENTICAL_BID_OWNER_SET = False    # D: OFF until the confirmation passes
D_MIN_BIDDERS = 3
# the preset: pricing_rule(round10_core=True) = these five switches on
ROUND10_CORE = ("muni_cells", "cmp_removal_identity", "f1_pipe_only",
                "family_cell_t2", "identical_bid_owner_set")
# ROUND 11 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND11.md, md5
# 2ffee091...): one multi-arm batch on data snapshot 7 (master_panel
# 9b7523eb...) selected P_CORE_noT2/own = round10_core WITHOUT T2 (MC + E +
# F1P + D) with its own forward P80 calibration (the production path).
# Forward, municipal 2024-26, 169 lettings both score: median P80 overshoot
# 51.4% -> 38.2% (90% interval -16.7 to -8.8 points), P80 under 16.6% ->
# 16.6% (2026: 19.0%, limit 20.2%), P50 median error 23.0% -> 19.4%; TxDOT
# identical.  NOT confirmed: the locked set is not reopened; the registered
# prospective check (>= 40 new DFW municipal lettings dated after
# 2026-11-20: P80 under <= 20% and median overshoot below B's on them)
# decides.  Until then the switches stay OFF; pricing_rule(
# round11_core_not2=True) turns on exactly these four
# (holdout_eval --compare-switch round11_core_not2).
ROUND11_CORE_NOT2 = ("muni_cells", "cmp_removal_identity", "f1_pipe_only",
                     "identical_bid_owner_set")
# ROUND 13 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND13.md, md5
# 1710c5e7...): one batch on data snapshot 9 (parser .29, master_panel
# ca92f1f8...) selected P_L/own -- the municipal price level of TxDOT-
# dominated family-keyed cells, with its own forward P80 calibration (the
# production path).  Forward, municipal 2024-26, 186 lettings both score:
# median P80 overshoot 43.1% -> 38.0% (90% interval -7.2 to -1.1 points),
# P80 under 16.1% -> 14.5% (2026: 17.2%, limit 21.8%), P50 median error
# 19.1% -> 19.4%, mean |ln| 0.313 -> 0.311; TxDOT identical.  NOT confirmed
# (own mode; the locked set is not reopened): the registered prospective
# check (>= 40 new DFW municipal lettings dated after 2026-11-20: P80 under
# <= 20% and median overshoot below B's on them) decides.  Until then the
# switch stays OFF; pricing_rule(round13_l=True) turns it on
# (holdout_eval --compare-switch round13_l).
#   P_L  MUNI_TXDOT_LEVEL  a municipal line priced from a band cell, a family
#        cell or F1's nearest sized cell is multiplied by R ** s (token picks
#        are left alone).  s = the TxDOT share of that cell's lettings before
#        D (0 for a municipal-only cell).  R = MarketLevel.ratio: exp(median
#        ln(median municipal entry / median TxDOT entry)) over the family's
#        BOTH-MARKET band cells at D (>= LEVEL_MIN_EACH municipal and >=
#        LEVEL_MIN_EACH TxDOT lettings, same family_key, unit, size bucket
#        and quantity band) when the family has >= LEVEL_MIN_CELLS_FAM of
#        them, else over every family's when >= LEVEL_MIN_CELLS_ALL, else no
#        change.  Not MC: no cell is swapped.
MUNI_TXDOT_LEVEL = False           # P_L: OFF until the prospective check passes
LEVEL_MIN_EACH = 3                 # both-market cell: >= 3 lettings per market
LEVEL_MIN_CELLS_FAM = 3            # a family's own ratio: >= 3 such cells
LEVEL_MIN_CELLS_ALL = 10           # the all-family ratio: >= 10 such cells
ROUND13_L = ("muni_txdot_level",)  # the preset pricing_rule(round13_l=True)
# ROUND 17 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND17.md): the
# abandonment identity, both markets (item_tokens.ABANDON_IDENTITY: the token
# flag and similar()'s refusal; item_tokens.ABANDON_FAMILY_KEY: also "#ab" in
# the band and family tables).  OFF is production; the presets
# pricing_rule(round17_ab=True) / (round17_ab2=True) turn them on
# (holdout_eval --compare-switch round17_ab / round17_ab2).  Switching either
# on in production needs the reference tables rebuilt (build_master), as F2's
# removal identity did.
ROUND17_AB = ("abandon_identity",)
ROUND17_AB2 = ("abandon_identity", "abandon_family_key")
# B80 (rounds 13-17's machinery; round 18 merges rounds 16 and 17): the P80
# divisor and the guard's level come from production B at each date
# (production_b(): every round-10/11/13/16/17 switch off, so B is production
# WITHOUT the clamp and the abandonment identity); the P50 and its centring
# stay this pass's own.  OFF is production; pricing_rule(p80_from_b=True), or
# a _b80 preset (round16_c_b80, round17_ab_b80, round18_x1_b80), turns it on.
P80_FROM_B = True                  # B80: ON in production since round 18
# what production_b() turns off (with comparables.MUNI_REMOVAL_IDENTITY and
# item_tokens.ABANDON_IDENTITY / ABANDON_FAMILY_KEY)
B_RULE_SWITCHES = ("MUNI_CELLS", "F1_PIPE_ONLY", "FAMILY_CELL_T2",
                   "IDENTICAL_BID_OWNER_SET", "MUNI_TXDOT_LEVEL",
                   "CLAMP_ABOVE_TOP_BIN", "P80_FROM_B")
ROUND17_AB_B80 = ("abandon_identity", "p80_from_b")
ROUND17_AB2_B80 = ("abandon_identity", "abandon_family_key", "p80_from_b")
# ROUND 18 (handoff/scratchpad/p50fix/PREREGISTRATION_ROUND18.md): rounds 16
# and 17 merged and re-scored on parser .33 data; the chosen arm X1/B80 =
# round 16's clamp + round 17's abandonment flag (AB) + B80 (P80_FROM_B).  AB
# needs the reference tables rebuilt (build_master: under AB only
# price_reference_tokens.csv changes); the clamp fits nothing.
ROUND16_C_B80 = ("clamp_high_bidders", "p80_from_b")  # round16_c_b80=True
ROUND18_PRESETS = {
    "round18_x1": ("clamp_high_bidders", "abandon_identity"),
    "round18_x1_b80": ("clamp_high_bidders", "abandon_identity",
                       "p80_from_b")}


def _municipal(market):
    return (market or "").strip().lower() == "municipal"


def _f1p_diameter(description, size_in):
    """F1P: pipe_fittings' size is a diameter -- an inch dimension equal to
    the line's parsed size (within 0.01) and a pipe / fitting word."""
    d = (description or "").lower()
    if size_in is None or not F1P_PIPE_RE.search(d):
        return False
    for m in F1P_DIAM_RE.finditer(d):
        try:
            if abs(float(m.group(1)) - size_in) <= 0.01:
                return True
        except ValueError:
            continue
    return False


def _f1p_ok(family, description, size_in):
    """F1P: may F1 price this (municipal) line?"""
    if (family or "") in F1P_FAMILIES:
        return True
    return (family or "") == F1P_FITTINGS and _f1p_diameter(description,
                                                             size_in)


def identical_bid_owner_set(prices, n_bidders, family, unit, description=""):
    """D: owner-set money recognised from >= D_MIN_BIDDERS distinct bidders
    quoting one identical unit price (max - min <= 0.01) on an LS /
    allowance-like line: unit LS or an allowance unit, allowance_like()
    wording or family, or an identical price of exactly 1.00 (a line whose
    quantity is dollars)."""
    ps = [p for p in prices if p]
    if n_bidders < D_MIN_BIDDERS or not ps or max(ps) - min(ps) > 0.01:
        return False
    un = (unit or "").strip().upper()
    return (un == "LS" or un in ALLOWANCE_UNITS
            or allowance_like(family, unit, description)
            or abs(min(ps) - 1.0) <= 0.005)


def schedule_owner_set(d, k):
    """(owner-set, by D) for schedule key k = (family, unit, size, band,
    description) of a back-test letting `d` as calibrate_forward and
    holdout_eval build it: production's is_owner_fixed, plus D's identical
    bids on a municipal letting when IDENTICAL_BID_OWNER_SET is on (d["nb"]:
    the distinct bidders per key, collected only then)."""
    fam, un, sz, bd, dn = k
    ps = d["prices"].get(k, ())
    if is_owner_fixed(ps, fam, un, dn):
        return True, False
    if IDENTICAL_BID_OWNER_SET and d.get("mk") == "municipal" and \
            identical_bid_owner_set(ps, len(d.get("nb", {}).get(k, ())), fam,
                                    un, dn):
        return True, True
    return False, False


class _MktMap:
    """MC: one reference table as a municipal line reads it -- the
    municipal-only cell when it qualifies (its own .get(): >=
    MUNI_CELLS_NMIN lettings), else the pooled cell.  items() is the pooled
    table's, so the tier logic chooses keys exactly as before (a qualifying
    municipal cell exists only where the pooled one does)."""

    def __init__(self, muni, pooled):
        self.muni, self.pooled = muni, pooled

    def get(self, k, default=None):
        r = self.muni.get(k)
        if r is not None:
            return r
        return self.pooled.get(k, default)

    def __getitem__(self, k):
        r = self.muni.get(k)
        if r is not None:
            return r
        return self.pooled[k]

    def items(self):
        return self.pooled.items()

    def __bool__(self):
        return bool(self.pooled)


class _MuniView:
    """MC: the tables a municipal line reads -- every attribute is the
    underlying tables' except the three reference tables."""

    def __init__(self, tab):
        self._tab = tab
        self.ref_tok = _MktMap(tab.ref_tok_m, tab.ref_tok)
        self.ref_band = _MktMap(tab.ref_band_m, tab.ref_band)
        self.ref = _MktMap(tab.ref_m, tab.ref)

    def __getattr__(self, a):
        return getattr(self._tab, a)


def muni_forward_kinds(pool, avail):
    """MC: the municipal-only reference cells of a price pool (the rows that
    may train), read forward like the pooled ones; None when MUNI_CELLS is
    off.  Returns (kinds, cells)."""
    if not MUNI_CELLS:
        return None, None
    cm = _reference_cells([r for r in pool if r[0] == "municipal"])
    return ({kd: _FwdKind(cm[kd], kd, MUNI_CELLS_NMIN, avail,
                          market="municipal") for kd in ("tok", "band", "fam")},
            cm)


@contextlib.contextmanager
def pricing_rule(size_fallback=True, removal_identity=True, comparables=True,
                 txdot_c4d1=True, coverage_scope=True, muni_cells=False,
                 cmp_removal_identity=False, f1_pipe_only=False,
                 family_cell_t2=False, identical_bid_owner_set=False,
                 round10_core=False, round11_core_not2=False,
                 muni_txdot_level=False, round13_l=False,
                 abandon_identity=True, abandon_family_key=False,
                 round17_ab=False, round17_ab2=False,
                 p80_from_b=True, round17_ab_b80=False,
                 round17_ab2_b80=False,
                 clamp_high_bidders=True, round16_c=False,
                 round16_c_b80=False,
                 round18_x1=False, round18_x1_b80=False):
    """Run a block under another combination of the switches; the defaults
    are production's.
    coverage_scope=False: the full-total scope before 2026-11-20 (every line
    priced, back-tests scored against the whole low bid).
    muni_cells, cmp_removal_identity, f1_pipe_only, family_cell_t2,
    identical_bid_owner_set: round 10's MC, E, F1P, T2, D (default OFF);
    round10_core=True is the preset that turns on exactly those five
    (ROUND10_CORE) -- holdout_eval --compare-switch round10_core.
    round11_core_not2=True: round 11's selected arm, the same without
    family_cell_t2 (ROUND11_CORE_NOT2).
    muni_txdot_level: round 13's P_L (default OFF); round13_l=True is its
    preset (ROUND13_L) -- holdout_eval --compare-switch round13_l.
    abandon_identity, abandon_family_key: round 17's AB flag and AB2's "#ab"
    family key (item_tokens.ABANDON_IDENTITY / ABANDON_FAMILY_KEY, default
    OFF; the family key acts only with the flag); presets round17_ab
    (ROUND17_AB) and round17_ab2 (ROUND17_AB2).
    p80_from_b: B80, the P80 divisor and guard level from production B
    (production_b(), the nested pass in calibrate_forward; default OFF);
    round17_ab_b80=True turns on the abandonment flag and B80 together
    (ROUND17_AB_B80, round 17's chosen change); round17_ab2_b80 the same
    with the family key (ROUND17_AB2_B80).
    clamp_high_bidders: round 16's C, a bidder count above the discount
    table's top bin priced off that bin (default OFF); presets round16_c
    (ROUND16_C) and round16_c_b80 (the clamp and B80, ROUND16_C_B80).
    round18_x1 / round18_x1_b80: round 18's chosen arm (ROUND18_PRESETS):
    the clamp + the abandonment flag, and the same with p80_from_b (THE
    CHANGE: round18_x1_b80).  Since round 18's install the defaults of
    clamp_high_bidders, abandon_identity and p80_from_b are True (production);
    name one False to turn it off."""
    g = globals()
    cmp_mod = g["comparables"]          # the module (the argument shadows it)
    old = (g["SIZE_FALLBACK"], item_tokens.REMOVAL_IDENTITY,
           g["COMPARABLES_BLEND"], g["TXDOT_C4D1"], g["COVERAGE_SCOPE"],
           g["MUNI_CELLS"], cmp_mod.MUNI_REMOVAL_IDENTITY, g["F1_PIPE_ONLY"],
           g["FAMILY_CELL_T2"], g["IDENTICAL_BID_OWNER_SET"])
    old13 = g["MUNI_TXDOT_LEVEL"]
    old17 = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY,
             g["P80_FROM_B"])
    old18 = g["CLAMP_ABOVE_TOP_BIN"]
    pre = set()                         # the switches the presets name
    for nm, on in (("round18_x1", round18_x1),
                   ("round18_x1_b80", round18_x1_b80)):
        if on:
            pre.update(ROUND18_PRESETS[nm])
    core = bool(round10_core)
    c11 = bool(round11_core_not2)       # round 11: round10_core without T2
    g["SIZE_FALLBACK"], g["COMPARABLES_BLEND"] = size_fallback, comparables
    g["TXDOT_C4D1"] = txdot_c4d1
    g["COVERAGE_SCOPE"] = coverage_scope
    g["MUNI_CELLS"] = bool(muni_cells) or core or c11
    cmp_mod.MUNI_REMOVAL_IDENTITY = bool(cmp_removal_identity) or core or c11
    g["F1_PIPE_ONLY"] = bool(f1_pipe_only) or core or c11
    g["FAMILY_CELL_T2"] = bool(family_cell_t2) or core
    g["IDENTICAL_BID_OWNER_SET"] = bool(identical_bid_owner_set) or core or c11
    g["MUNI_TXDOT_LEVEL"] = bool(muni_txdot_level) or bool(round13_l)
    item_tokens.REMOVAL_IDENTITY = removal_identity
    item_tokens.ABANDON_IDENTITY = (bool(abandon_identity) or bool(round17_ab)
                                    or bool(round17_ab2)
                                    or bool(round17_ab_b80)
                                    or bool(round17_ab2_b80)
                                    or "abandon_identity" in pre)
    item_tokens.ABANDON_FAMILY_KEY = (bool(abandon_family_key)
                                      or bool(round17_ab2)
                                      or bool(round17_ab2_b80))
    g["P80_FROM_B"] = (bool(p80_from_b) or bool(round17_ab_b80)
                       or bool(round17_ab2_b80) or bool(round16_c_b80)
                       or "p80_from_b" in pre)
    g["CLAMP_ABOVE_TOP_BIN"] = (bool(clamp_high_bidders) or bool(round16_c)
                                or bool(round16_c_b80)
                                or "clamp_high_bidders" in pre)
    try:
        yield
    finally:
        g["SIZE_FALLBACK"], item_tokens.REMOVAL_IDENTITY, \
            g["COMPARABLES_BLEND"], g["TXDOT_C4D1"], g["COVERAGE_SCOPE"], \
            g["MUNI_CELLS"], cmp_mod.MUNI_REMOVAL_IDENTITY, \
            g["F1_PIPE_ONLY"], g["FAMILY_CELL_T2"], \
            g["IDENTICAL_BID_OWNER_SET"] = old
        g["MUNI_TXDOT_LEVEL"] = old13
        item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY, \
            g["P80_FROM_B"] = old17
        g["CLAMP_ABOVE_TOP_BIN"] = old18


class SizeCells:
    """F1's fitted quantities: the size structure of the family table.

    The family table's sized cells are (family_key, unit, size_bucket); a
    bucket is a range, so a line whose own bucket has no band cell used to
    fall back to the UNSIZED cell -- rows whose size did not parse, about
    $45/LF of mostly trench repair and paving -- while still saying "size
    match" (Frisco 2406-104, a 48-inch main, priced at 0.037x).  F1 prices
    such a line from the NEAREST sized cell of its family+unit instead,
    scaled by (size / size_ref) ** k.

      entry          one (cell, letting, year) group, as _reference_cells
                     groups the family table: (median unit price, median
                     size_in of the group's rows)
      size_ref(c, D) median entry size (> 0) of cell c's lettings available
                     strictly before D (production's prefix order)
      k(f, u, D)     OLS slope of ln(entry price) on ln(entry size) over
                     every sized cell of (f, u), lettings before D; used with
                     >= 5 distinct sizes (0.01 in) and >= 20 lettings, else 1
                     for pipes (SIZE_K_ONE, by the classifier's family) and 0

    D = None (the published tables) means every letting.  Built from the
    rows handed to _reference_cells, so whoever decides which rows may train
    the reference cells decides it for these too (external rows only in
    production, training rows only in holdout_eval).  Ported from p50fix
    round5/r5lib.py SizeIndex (the code that was scored and confirmed)."""

    def __init__(self, rows, sizes, avail):
        grp = collections.defaultdict(lambda: ([], []))
        for (mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr), s in zip(rows, sizes):
            if not un or not sz:
                continue
            fk = family_key(fam, dn)
            g = grp[((fk, un, sz), pk, yr)]
            g[0].append(u)
            if s is not None:
                g[1].append(s)
        by = collections.defaultdict(lambda: collections.defaultdict(list))
        for (key, pk, yr), (us, ss) in grp.items():
            by[key][pk].append((statistics.median(us),
                                statistics.median(ss) if ss else None))
        self.cells = {}            # key -> (dates, pks, entries per pk)
        self.sized = collections.defaultdict(list)   # (fam_key, unit) -> keys
        fit = collections.defaultdict(list)
        for key, bp in by.items():
            lst = sorted(bp, key=lambda pk: (avail.get(pk, NEVER), pk))
            self.cells[key] = ([avail.get(pk, NEVER) for pk in lst], lst, bp)
            self.sized[(key[0], key[1])].append(key)
            for pk, ents in bp.items():
                for pr, sm in ents:
                    if pr and pr > 0 and sm and sm > 0:
                        fit[(key[0], key[1])].append(
                            (avail.get(pk, NEVER), pk, math.log(sm),
                             math.log(pr), round(sm, 2)))
        for v in self.sized.values():
            v.sort()
        self.fit = {}              # cumulative sums per (fam_key, unit), date order
        for fu, pts in fit.items():
            pts.sort()
            d, n, sx, sy, sxx, sxy, ns, nl = [], [], [], [], [], [], [], []
            a = [0, 0.0, 0.0, 0.0, 0.0]
            seen_s, seen_l = set(), set()
            for D, pk, x, y, sm in pts:
                a[0] += 1
                a[1] += x
                a[2] += y
                a[3] += x * x
                a[4] += x * y
                seen_s.add(sm)
                seen_l.add(pk)
                d.append(D)
                n.append(a[0]); sx.append(a[1]); sy.append(a[2])  # noqa: E702
                sxx.append(a[3]); sxy.append(a[4])                 # noqa: E702
                ns.append(len(seen_s)); nl.append(len(seen_l))      # noqa: E702
            self.fit[fu] = (d, n, sx, sy, sxx, sxy, ns, nl, [p[1] for p in pts])
        self.memo_ref, self.memo_k = {}, {}

    def lettings(self):
        """Every letting in any size cell (the leak guards' view)."""
        return {pk for _, lst, _ in self.cells.values() for pk in lst}

    def size_ref(self, key, D=None):
        c = self.cells.get(key)
        if c is None:
            return None
        L = len(c[0]) if D is None else bisect.bisect_left(c[0], D)
        if (key, L) not in self.memo_ref:
            ss = [s for pk in c[1][:L] for _, s in c[2][pk]
                  if s is not None and s > 0]
            self.memo_ref[(key, L)] = statistics.median(ss) if ss else None
        return self.memo_ref[(key, L)]

    def ref_lettings(self, key, D=None):
        c = self.cells.get(key)
        if c is None:
            return []
        L = len(c[0]) if D is None else bisect.bisect_left(c[0], D)
        return list(c[1][:L])

    def k_at(self, fam, unit, D=None):
        default = 1.0 if _base_family(fam or "") in SIZE_K_ONE else 0.0
        F = self.fit.get((fam, unit))
        if F is None:
            return default, {"src": "default", "n": 0, "sizes": 0, "lettings": 0}
        i = len(F[0]) if D is None else bisect.bisect_left(F[0], D)
        hit = self.memo_k.get((fam, unit, i))
        if hit is not None:
            return hit
        if i == 0:
            out = (default, {"src": "default", "n": 0, "sizes": 0, "lettings": 0})
        else:
            j = i - 1
            n, sx, sy, sxx, sxy = F[1][j], F[2][j], F[3][j], F[4][j], F[5][j]
            den = n * sxx - sx * sx
            info = {"n": n, "sizes": F[6][j], "lettings": F[7][j]}
            if F[6][j] >= SIZE_K_MIN_SIZES and F[7][j] >= SIZE_K_MIN_LETTINGS \
                    and den > 1e-12:
                info["src"] = "fit"
                out = ((n * sxy - sx * sy) / den, info)
            else:
                info["src"] = "default"
                out = (default, info)
        self.memo_k[(fam, unit, i)] = out
        return out

    def k_lettings(self, fam, unit, D=None):
        F = self.fit.get((fam, unit))
        if F is None:
            return set()
        i = len(F[0]) if D is None else bisect.bisect_left(F[0], D)
        return set(F[8][:i])


def _line_sizes(rows, sizes):
    """A schedule line's own size: the median size_in of the low bidder's
    rows of that schedule key (q > 0), keyed (letting, family, unit, size
    bucket, quantity band, description) -- part of the schedule, like its
    quantities."""
    line = collections.defaultdict(list)
    for (mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr), s in zip(rows, sizes):
        if w and q is not None and q > 0:
            line[(pk, fam, un, sz, qty_shape.band_of(q), dn)].append(s)
    out = {}
    for k, v in line.items():
        v = [x for x in v if x is not None]
        out[k] = statistics.median(v) if v else None
    return out


def _band_has_size(tab, fk, unit, sz):
    """A usable band cell (production's .get(): >= 2 earlier lettings) for
    this size bucket in ANY quantity band."""
    for b in qty_shape.BAND_LABELS:
        if tab.ref_band.get((fk, unit, sz, b)) is not None:
            return True
    return False


def d1_n_class(n):
    """D1's bidder class: 0 = 1-2, 1 = 3-4, 2 = 5-6, 3 = 7+ bidders."""
    return 0 if n <= 2 else 1 if n <= 4 else 2 if n <= 6 else 3


def d1_s_class(x):
    """D1's size class of a pre-discount model total: 0 < 250k, 1 250k-1M,
    2 1M-5M, 3 >= 5M."""
    return bisect.bisect_right(D1_SIZE_EDGES, x)


# LOW-CONFIDENCE LINES (user, 2026-11-20 ~14:2x: "mobilization is a bit of a black art to begin with;
# if it cannot be estimated well, we can say so").  PRESENTATION ONLY: nothing here moves
# market_total, bid_part, expected_low, the P80 or any fitted constant.
# estimate() adds a `low_confidence` block after the estimate is complete,
# run_schedule() prints it, and calibrate_forward() records five report-only
# fields per letting for the "mobilization set aside" scoring view
# (handoff/scratchpad/diag_mob/mob_aside_report.py).
# Lines: mobilization (any unit), and the general-condition LS lines --
# traffic control, site / right-of-way preparation, SWPPP / erosion control,
# bonds / insurance (lowconf_class).  Each class is one line marked "Low confidence",
# with: our amount at the P50 level (line amount x (P50 - owner-set money) /
# market total, so the lines of a schedule add up to its P50); the share of
# their own bid the LOW bidders of earlier lettings of the same market gave
# that class (p25 / median / p75 over lettings dated strictly before the
# schedule's bid date; size class = the rest of the bid, < $0.5M / $0.5-2M /
# > $2M, all sizes when that class has < LOWCONF_MIN_LETTINGS); and the
# dollar range those shares imply for this schedule's rest.
# Why (handoff/scratchpad/diag_mob, 2026-11-20; municipal 2024-26 forward,
# production): a mobilization line's own price is nearly size-blind (median
# $77k / $80k / $84k on jobs < $0.5M / $0.5-2M / > $2M where the low bidders
# gave $13.5k / $38k / $193k), so ours runs a median 4.5x the low bidder's on
# small jobs and 0.43x on large ones; yet the low bidders' share of their own
# bid is steady (median 4.8%, p25-p75 3.6-5.8%), and inside one letting the
# bidders' mobilization amounts vary about as much as any other line (CV
# 0.43 vs 0.45).  Forward, the implied median for mobilization is off the
# low bidder's own amount by a median factor of ~1.5 (|ln| 0.40) against
# ~2.9 (1.06) for our line price, and the low bidder's amount falls inside
# the implied p25-p75 range in about a third of lettings: a guide, not a
# band.  Nothing is priced from it (a share rule did not lower the total
# error: diag_mob decomp.txt).
LOWCONF = True
LOWCONF_LABEL = "Low confidence"
LOWCONF_MIN_LETTINGS = 15          # a history cell needs >= 15 earlier lettings
LOWCONF_SIZE_EDGES = (5e5, 2e6)    # size of the REST of the bid
LOWCONF_SIZE_LABELS = ("< $0.5M", "$0.5-2M", "> $2M")
LOWCONF_CLASSES = (
    ("mobilization", "Mobilization"),
    ("traffic_control", "Traffic control (lump sum)"),
    ("site_prep", "Site / right-of-way preparation (lump sum)"),
    ("swppp_erosion", "SWPPP / erosion control (lump sum)"),
    ("bonds_insurance", "Bonds / insurance (lump sum)"))
_LC_LS = ("LS", "L.S.", "LUMP SUM")
_LC_MOB = re.compile(r"\bmobili[sz]", re.I)
_LC_GC = (
    ("traffic_control", re.compile(
        r"\btraffic\s+(?:control|handling)|\bbarricad|\bmaintenance\s+of\s+"
        r"traffic", re.I)),
    ("site_prep", re.compile(
        r"\bsite\s+prep|\bright\s+of\s+way\s*(?:/\s*easement\s+|and\s+"
        r"easement\s+)?prep|\brow\s+prep|\bclearing\s+and\s+grubbing", re.I)),
    ("swppp_erosion", re.compile(
        r"\bswppp|\bstorm\s*water\s+pollution|\berosion|\bsediment|"
        r"\bpollution\s+prevent", re.I)),
    ("bonds_insurance", re.compile(r"\bbonds?\b|\bbonding|\binsurance",
                                   re.I)))
_LC_FAMS = {"traffic_control": "traffic_control",
            "site_prep_clearing": "site_prep",
            "erosion_control": "swppp_erosion",
            "bonds_insurance": "bonds_insurance"}


def lowconf_class(family, unit, description=""):
    """A line's low-confidence class, or None: "mobilization" (the mobilization
    family, or an unclassified LS line worded mobiliz-), else a general-
    condition LS line by its family or, unclassified, by its words."""
    fam = family or ""
    u = (unit or "").strip().upper()
    d = description or ""
    if fam == "mobilization" or (fam in ("other", "needs_review", "")
                                 and u in _LC_LS and _LC_MOB.search(d)):
        return "mobilization"
    if u not in _LC_LS:
        return None
    if fam in _LC_FAMS:
        return _LC_FAMS[fam]
    if fam in ("other", "needs_review", ""):
        for name, rx in _LC_GC:
            if rx.search(d):
                return name
    return None


def lowconf_size_class(x):
    return bisect.bisect_right(LOWCONF_SIZE_EDGES, x)


class LowConfShare:
    """The history behind the low-confidence ranges: for every letting of the pool
    with one identified low bidder, two or more bidders and >=
    MIN_SCHEDULE_ITEMS lines, the LOW bidder's amount on each low-confidence
    class as a share of its own total -- owner-set lines (is_owner_fixed on
    the letting's bids) and uncovered lines (line_coverage) out of both, as
    the P50 leaves them out -- keyed by market, class, the letting's
    available date and the size class of the rest of that bid.  at() reads
    only lettings dated strictly before D.  Fitted on the pool handed in (the
    external price pool), like MobRatio; it prices nothing."""

    def __init__(self, pool, avail):
        per = collections.defaultdict(
            lambda: {"mk": "", "prices": collections.defaultdict(list),
                     "low": collections.defaultdict(float), "win": set(),
                     "bidders": set()})
        for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in pool:
            if q is None or q <= 0:
                continue
            d = per[pk]
            d["mk"] = mk
            k = (fam, un, sz, qty_shape.band_of(q), dn)
            d["prices"][k].append(u)
            d["bidders"].add(b)
            if w:
                d["win"].add(b)
                d["low"][k] += q * u
        hist = collections.defaultdict(list)
        for pk, d in per.items():
            D = avail.get(pk, NEVER)
            if (D == NEVER or len(d["win"]) != 1 or len(d["bidders"]) < 2
                    or len(d["low"]) < MIN_SCHEDULE_ITEMS):
                continue
            tot = 0.0
            cls = collections.defaultdict(float)
            for k, a in d["low"].items():
                if is_owner_fixed(d["prices"][k], k[0], k[1], k[4]):
                    continue
                if COVERAGE_SCOPE and line_coverage(k[4], k[0], k[1]):
                    continue
                tot += a
                c = lowconf_class(k[0], k[1], k[4])
                if c:
                    cls[c] += a
            for c, a in cls.items():
                if 0 < a < tot:
                    hist[(d["mk"], c)].append(
                        (D, lowconf_size_class(tot - a), a / tot))
        self.hist = {}
        for key, v in hist.items():
            v.sort(key=lambda t: t[0])
            self.hist[key] = ([t[0] for t in v], [(t[1], t[2]) for t in v])

    def at(self, market, cls, rest, D=None):
        """{p25, median, p75, n, size_class, scope, before} of the low
        bidders' share for `cls` in lettings before D whose rest is in the
        size class of `rest` (all sizes when that class has fewer than
        LOWCONF_MIN_LETTINGS), or None when even all sizes have fewer."""
        h = self.hist.get((market, cls))
        if not h:
            return None
        dates, vals = h
        k = len(dates) if D is None else bisect.bisect_left(dates, D)
        sc = lowconf_size_class(rest)
        v = sorted(s for c, s in vals[:k] if c == sc)
        scope = "size"
        if len(v) < LOWCONF_MIN_LETTINGS:
            v, scope = sorted(s for _, s in vals[:k]), "all_sizes"
            if len(v) < LOWCONF_MIN_LETTINGS:
                return None
        qq = lambda p: v[int(round(p * (len(v) - 1)))]  # noqa: E731
        return {"p25": qq(.25), "median": qq(.5), "p75": qq(.75),
                "n": len(v), "size_class": sc, "scope": scope, "before": D}


class MobRatio:
    """T1's mobilization ratio for one market: r(D) = the median, over every
    bid of every letting available strictly before D, of (that bid's
    mobilization amount / the same bid's amount on every other line), bids
    with both > 0, owner-set lines (is_owner_fixed on the letting's bids)
    out of both; with the number of such lettings.  Ported from p50fix
    p50_variants.ForwardRatios (round 1, spec "c1", no size classes).
    Coverage scope: uncovered lines (line_coverage) are out of both too, as
    estimate() leaves them out of the base the ratio multiplies."""

    def __init__(self, pool, avail, market="txdot"):
        per = collections.defaultdict(
            lambda: {"rows": [], "prices": collections.defaultdict(list)})
        for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in pool:
            if mk != market or q is None or q <= 0:
                continue
            d = per[pk]
            k = (fam, un, sz, qty_shape.band_of(q), dn)
            d["prices"][k].append(u)
            d["rows"].append((b, k, q * u))
        bids, lets = [], []
        for pk, d in per.items():
            D = avail.get(pk, NEVER)
            if D == NEVER:
                continue
            fixed = {k for k, ps in d["prices"].items()
                     if is_owner_fixed(ps, k[0], k[1], k[4])}
            acc = collections.defaultdict(
                lambda: collections.defaultdict(float))
            for b, k, a in d["rows"]:
                if k in fixed:
                    continue
                if COVERAGE_SCOPE and line_coverage(k[4], k[0], k[1]):
                    continue
                acc[b]["mob" if k[0] == "mobilization" else "base"] += a
            rs = [v["mob"] / v["base"] for v in acc.values()
                  if v.get("mob", 0) > 0 and v.get("base", 0) > 0]
            if not rs:
                continue
            bids.extend((D, x) for x in rs)
            lets.append(D)
        bids.sort(key=lambda t: t[0])
        self.dates = [t[0] for t in bids]
        self.vals = [t[1] for t in bids]
        self.lets = sorted(lets)
        self.memo = {}

    def at(self, D=None):
        """(r, lettings behind it) from the lettings before D (None: all)."""
        k = len(self.dates) if D is None else bisect.bisect_left(self.dates, D)
        nl = len(self.lets) if D is None else bisect.bisect_left(self.lets, D)
        if k not in self.memo:
            self.memo[k] = statistics.median(self.vals[:k]) if k else None
        return self.memo[k], nl

    def lettings(self, D=None):
        return self.lets if D is None else self.lets[:bisect.bisect_left(self.lets, D)]


class MarketLevel:
    """P_L (MUNI_TXDOT_LEVEL): the municipal / TxDOT price level of pooled
    reference cells, from the rows that may train.

      share(kind, key, D)  the TxDOT share of the lettings (available
                           strictly before D; D None: all) of the reference
                           cell `key` of table `kind` ("tok", "band", "fam":
                           _reference_cells' keys); None if the cell is empty
      ratio(base_family, D) (R, scope, cells): over the family's BOTH-MARKET
                           band cells at D -- a (family_key, unit, size
                           bucket, quantity band) cell with >= LEVEL_MIN_EACH
                           municipal and >= LEVEL_MIN_EACH TxDOT lettings
                           before D -- R = exp(median over cells of ln(median
                           municipal entry price / median TxDOT entry price)),
                           when >= LEVEL_MIN_CELLS_FAM cells ("family");
                           else the same over every family's both-market
                           cells when >= LEVEL_MIN_CELLS_ALL ("all"); else
                           (None, "none", n).  An entry is one letting's
                           median unit price in the cell (the tables' grain).
    The family of a cell is _base_family(family_key) (a removal-only cell
    counts with its family)."""

    def __init__(self, cells, pkmk, avail):
        self.cells, self.mk, self.av = cells, pkmk, avail
        self.fam = collections.defaultdict(list)
        self.all = []
        for (fk, un, sz, bd), lets in cells["band"].items():
            m, t = [], []
            for pk, es in lets.items():
                a = avail.get(pk, NEVER)
                for e in es:
                    (m if pkmk.get(pk) == "municipal" else t).append(
                        (a, pk, e[0]))
            if len({x[1] for x in m}) >= LEVEL_MIN_EACH and \
                    len({x[1] for x in t}) >= LEVEL_MIN_EACH:
                m.sort()
                t.sort()
                c = (len(self.all), m, [x[0] for x in m], t, [x[0] for x in t])
                self.fam[_base_family(fk or "")].append(c)
                self.all.append(c)
        self.memo_c, self.memo_r, self.memo_a = {}, {}, {}

    def lettings(self):
        """Every letting in a both-market cell (the leak guards' view)."""
        return {x[1] for c in self.all for x in c[1] + c[3]}

    def _lnr(self, c, D):
        cid, m, md, t, td = c
        Lm = len(m) if D is None else bisect.bisect_left(md, D)
        Lt = len(t) if D is None else bisect.bisect_left(td, D)
        hit = self.memo_c.get((cid, Lm, Lt), 0)
        if hit != 0:
            return hit
        mm, tt = m[:Lm], t[:Lt]
        r = None
        if len({x[1] for x in mm}) >= LEVEL_MIN_EACH and \
                len({x[1] for x in tt}) >= LEVEL_MIN_EACH:
            r = (math.log(statistics.median([x[2] for x in mm]))
                 - math.log(statistics.median([x[2] for x in tt])))
        self.memo_c[(cid, Lm, Lt)] = r
        return r

    def _all(self, D):
        if D not in self.memo_a:
            v = [x for x in (self._lnr(c, D) for c in self.all)
                 if x is not None]
            self.memo_a[D] = ((math.exp(statistics.median(v)), "all", len(v))
                              if len(v) >= LEVEL_MIN_CELLS_ALL
                              else (None, "none", len(v)))
        return self.memo_a[D]

    def ratio(self, base_family, D=None):
        key = (base_family, D)
        if key not in self.memo_r:
            v = [x for x in (self._lnr(c, D)
                             for c in self.fam.get(base_family, ()))
                 if x is not None]
            if len(v) >= LEVEL_MIN_CELLS_FAM:
                self.memo_r[key] = (math.exp(statistics.median(v)), "family",
                                    len(v))
            else:
                self.memo_r[key] = self._all(D)
        return self.memo_r[key]

    def share(self, kind, key, D=None):
        by = self.cells.get(kind, {}).get(key)
        if not by:
            return None
        pks = [pk for pk in by if D is None or self.av.get(pk, NEVER) < D]
        if not pks:
            return None
        return sum(1 for pk in pks if self.mk.get(pk) != "municipal") / len(pks)


class T1State:
    """T1's D1 discount, the model totals its comparables adjustment reads,
    and the mobilization ratio, for one pricing pass.

      d1 records  one per forward-scored TxDOT letting (calibrate_forward's
                  population and 90%-priced rule): its date, bidder class,
                  the class of its own pre-discount model total, and low /
                  median bidder total -- fed in date order by the pass
      model       every priced TxDOT letting's pre-discount model total
      d1(n, x, D) the median of the cell's records dated strictly before D
                  when there are >= D1_MIN of them, else None (f(n) stays)

    calibrate_forward feeds it as it prices; for a NEW schedule
    (Tables.attach_v2f1f2) the records come back from T1_FILE, written by
    the last calibrate_forward(write=True).  Ported from p50fix
    round2/p50_round2.D1Table and round3/cmp_port.d1_factory."""

    def __init__(self, pool, avail):
        self.mob = MobRatio(pool, avail, "txdot")
        self.cells = collections.defaultdict(lambda: ([], []))
        self.model = {}
        self.memo = {}
        self.loaded = None

    def note_priced(self, pk, total):
        self.model[pk] = total

    def note_scored(self, D, n_bidders, total, low, median_total):
        if total <= 0 or median_total <= 0:
            return
        dates, vals = self.cells[(d1_n_class(n_bidders), d1_s_class(total))]
        assert not dates or dates[-1] <= D, "D1 records must come in date order"
        dates.append(D)
        vals.append(low / median_total)

    def d1(self, n_bidders, total, D):
        key = (d1_n_class(n_bidders), d1_s_class(total))
        c = self.cells.get(key)
        if c is None:
            return None
        k = len(c[0]) if D is None else bisect.bisect_left(c[0], D)
        if k < D1_MIN:
            return None
        m = self.memo.get((key, k))
        if m is None:
            m = self.memo[(key, k)] = statistics.median(c[1][:k])
        return m

    def f_candidate(self, tab, D):
        """f(C) for V2's competition adjustment c = f(T) / f(C): C's D1 cell
        at D (C's bidder count, the class of C's own model total), else
        production f(n_C) at D; a letting never priced takes f(n_C)."""
        def f(n, pk):
            m = self.model.get(pk)
            if m is not None and m > 0:
                v = self.d1(n, m, D)
                if v is not None:
                    return v
            dC = discount(tab, n, "txdot")
            return dC["f"] if dC else None
        return f

    def write(self, path):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["kind", "project_key", "date", "n_class", "s_class",
                         "value"])
            for (nc, sc), (dates, vals) in sorted(self.cells.items()):
                for D, v in zip(dates, vals):
                    wr.writerow(["d1", "", D, nc, sc, repr(v)])
            for pk, m in sorted(self.model.items()):
                wr.writerow(["model", pk, "", "", "", repr(m)])

    def load(self, path):
        self.loaded = os.path.exists(path)
        if not self.loaded:
            return self
        recs = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r["kind"] == "d1":
                    recs.append((r["date"], int(r["n_class"]),
                                 int(r["s_class"]), float(r["value"])))
                elif r["kind"] == "model":
                    self.model[r["project_key"]] = float(r["value"])
        for D, nc, sc, v in sorted(recs, key=lambda x: x[0]):
            dates, vals = self.cells[(nc, sc)]
            dates.append(D)
            vals.append(v)
        return self


def _centre(trip):
    """The level correction: the factor on the competitively bid part of each
    estimate that puts the median of (part x factor + owner-set money) / actual
    at exactly 1.  With no owner-set money it is 1 / median ratio, the old
    definition; with it, the face-value amounts are left alone, because no
    correction belongs on money nobody estimated.  `trip` is a list of
    (discounted_estimate, owner_fixed, actual)."""
    lo, hi = 0.05, 20.0
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        m = statistics.median([(c * mid + fx) / w for c, fx, w in trip])
        if m < 1.0:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


@contextlib.contextmanager
def production_b():
    """B80: production B -- every round-10/11/13/16/17 switch off (MC, E, F1P,
    T2, D, P_L, round 16's clamp, round 17's abandonment identity and family
    key, and P80_FROM_B itself), every other switch as it stands.  (Rounds
    13-17's, with the merged list of round 18.)"""
    g = globals()
    cmp_mod = g["comparables"]
    old = tuple(g[n] for n in B_RULE_SWITCHES)
    old_e = cmp_mod.MUNI_REMOVAL_IDENTITY
    old_ab = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY)
    for n in B_RULE_SWITCHES:
        g[n] = False
    cmp_mod.MUNI_REMOVAL_IDENTITY = False
    item_tokens.ABANDON_IDENTITY = item_tokens.ABANDON_FAMILY_KEY = False
    try:
        yield
    finally:
        for n, v in zip(B_RULE_SWITCHES, old):
            g[n] = v
        cmp_mod.MUNI_REMOVAL_IDENTITY = old_e
        item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY = old_ab


class GuardPath:
    """B80: the constants a letting dated D gets from one market's forward
    records (production B's): centring and the P80 divisor from the records
    dated strictly before D (>= FORWARD_MIN_PRIOR of them), the divisor at the
    guard's level as it stood when D opened -- forward_scores()' walk under
    production's rule, remembered per date.  at(D) -> (alpha, divisor,
    centring) or None.  (Round 11-16's GuardPath, unchanged.)"""

    def __init__(self, recs, gamma, guard=True):
        self.trip = sorted((r["date"], r["bid_part"], r["owner_fixed"],
                            r["actual_low"], r["project_key"]) for r in recs)
        self.dates = [t[0] for t in self.trip]
        self.gdates, self.galpha = [], []
        alpha, i, trip = GUARD_TARGET, 0, self.trip
        while i < len(trip):
            D = trip[i][0]
            j = i
            while j < len(trip) and trip[j][0] == D:
                j += 1
            self.gdates.append(D)
            self.galpha.append(alpha)
            if i >= FORWARD_MIN_PRIOR:
                prior = [(c, fx, w) for _, c, fx, w, _ in trip[:i]]
                lc = _centre(prior)
                rs = sorted((c * lc + fx) / w for c, fx, w in prior)
                a = alpha if guard else GUARD_TARGET
                p = rs[int(a * (len(rs) - 1))]
                errs = [(c * lc + fx) / w / p < 1.0
                        for _, c, fx, w, _ in trip[i:j]]
                if guard:
                    alpha = _guard_step(alpha, errs, gamma, GUARD_TARGET)
            i = j
        self.final_alpha, self.guard, self._memo = alpha, guard, {}

    def at(self, D):
        if D in self._memo:
            return self._memo[D]
        i = bisect.bisect_left(self.dates, D)
        out = None
        if i >= FORWARD_MIN_PRIOR:
            g = bisect.bisect_left(self.gdates, D)
            alpha = self.galpha[g] if g < len(self.gdates) else self.final_alpha
            a = alpha if self.guard else GUARD_TARGET
            prior = [(c, fx, w) for _, c, fx, w, _ in self.trip[:i]]
            lc = _centre(prior)
            rs = sorted((c * lc + fx) / w for c, fx, w in prior)
            out = (a, rs[int(a * (len(rs) - 1))], lc)
        self._memo[D] = out
        return out


def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x else default


def _read(path, **kw):
    p = os.path.join(CLEAN, path)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Tables:
    """Everything the estimator reads, loaded once, with what is missing said
    out loud rather than silently defaulted."""

    def __init__(self):
        self.missing = []
        self.notes = []

        self.ref = {}
        for r in _read("price_reference.csv"):
            k = (r["item_family"], r["unit"], r.get("size_bucket") or "")
            self.ref[k] = r
        if not self.ref:
            self.missing.append("price_reference.csv")

        # The band-keyed reference, tried FIRST where it has support.  Measured
        # leave-one-letting-out on the 1,935 lettings both keys can price:
        # median |error| 32.6% -> 28.6% municipal, 50.1% -> 30.8% TxDOT, and on
        # TxDOT the bias goes 1.4231 -> 0.9742.  It is a fallback chain and not
        # a replacement because it costs coverage: municipal lettings that can
        # be fully priced drop from 121 to 88.
        # Token-keyed reference, tried before the band one.  Identity is a
        # word set with a Jaccard threshold, because 84.4% of municipal
        # descriptions appear in exactly one letting and an equality join
        # therefore finds nothing.  Measured leave-one-letting-out: municipal
        # 28.5% -> 23.9%, TxDOT 30.9% -> 24.5%.
        self.ref_tok = {}
        self.tok_inv = collections.defaultdict(
            lambda: collections.defaultdict(list))
        for r in _read("price_reference_tokens.csv"):
            t = frozenset((r.get("tok") or "").split())
            if not t:
                continue
            k = (t, r.get("unit") or "", r.get("qty_band") or "")
            self.ref_tok[k] = r
            for w in t:
                self.tok_inv[k[1]][w].append(k)
        if not self.ref_tok:
            self.notes.append(
                "price_reference_tokens.csv absent -- items will be priced "
                "from family medians, which measured 4.6 points worse on "
                "municipal work and 6.4 on TxDOT")

        self.ref_band = {}
        for r in _read("price_reference_band.csv"):
            k = (r["item_family"], r["unit"], r.get("size_bucket") or "",
                 r.get("qty_band") or "")
            self.ref_band[k] = r
        if not self.ref_band:
            self.notes.append(
                "price_reference_band.csv absent -- every item will be priced "
                "from the pooled family median and scaled by a band shape "
                "multiplier, which measured 4 to 19 points worse")

        self.shape = {}
        for r in _read("price_by_quantity_band_family.csv"):
            self.shape[(r["item_family"], r["unit"], r["qty_band"])] = r
        if not self.shape:
            self.missing.append("price_by_quantity_band_family.csv")

        # Escalation.  index_by_market.csv is preferred because the two markets
        # do not escalate alike -- statewide fell 12.5% in 2025->2026 while DFW
        # municipal fell 7.3% -- but its municipal arm has been missing the
        # recent years, so the fallback is explicit and reported, never silent.
        self.index = collections.defaultdict(dict)
        # index_by_market.csv carries a competition_scope column, written by
        # another script.  Reading it without filtering means two rows can
        # share a (market, to) key and the index becomes whichever one was
        # written last -- silently, and the two arms do not agree.  Prefer the
        # scope that matches the price pool, say which was taken, and never
        # overwrite a key.
        rows_ix = _read("index_by_market.csv")
        scopes = {(r.get("competition_scope") or "") for r in rows_ix}
        pick = next((w for w in ("competitive_only", "competitive", "all", "")
                     if w in scopes), None)
        if pick is None and scopes:
            pick = sorted(scopes)[0]
        self.index_scope = pick
        if len([w for w in scopes if w]) > 1:
            self.notes.append("index_by_market.csv has scopes %r; took %r"
                              % (sorted(scopes), pick))
        for r in rows_ix:
            if (r.get("competition_scope") or "") != pick:
                continue
            y = str(r.get("to") or "").strip()
            v = _f(r.get("index"))
            mk = r.get("market") or "?"
            if not (y and v):
                continue
            if y in self.index[mk] and abs(self.index[mk][y] - v) > 1e-9:
                self.notes.append(
                    "index_by_market.csv: duplicate (%s, %s) with different "
                    "values %.4f and %.4f -- kept the first, the file needs a "
                    "unique key" % (mk, y, self.index[mk][y], v))
                continue
            self.index[mk][y] = v
        for r in _read("price_time_index.csv"):
            y = str(r.get("to") or "").strip()
            v = _f(r.get("index"))
            if y and v:
                self.index["_default"][y] = v
        if not self.index:
            self.missing.append("price_time_index.csv / index_by_market.csv")

        self.disc = {}
        self.disc_muni = {}
        for name, into in (("competition_discount_pooled.csv", self.disc),
                           ("competition_discount_municipal.csv",
                            self.disc_muni)):
            for r in _read(name):
                n = _f(r.get("n_bidders"))
                v = _f(r.get("low_over_median"))
                if n and v:
                    into[int(n)] = {
                        "f": v, "p25": _f(r.get("p25")), "p75": _f(r.get("p75")),
                        "lettings": int(_f(r.get("lettings"), 0) or 0)}
            if not into:
                self.missing.append(name)

        self.borrow_sigma = {}
        for r in _read("qty_shape_borrow_sigma.csv"):
            d = _f(r.get("band_distance"))
            s = _f(r.get("sigma_log"))
            if d is not None and s is not None:
                self.borrow_sigma[int(d)] = s

        self.calib = {}
        for r in _read("estimator_calibration.csv"):
            self.calib[r["market"]] = r
        # The optional P70 trial leaves published P50/P80 fields untouched.
        trial = _read("estimator_p70_calibration.csv")
        if trial:
            with open(os.path.join(CLEAN, "estimator_calibration.csv"), "rb") as f:
                base_sha = hashlib.sha256(f.read()).hexdigest()
            for r in trial:
                c = self.calib.get(r.get("market"))
                p30 = _f(r.get("p30"))
                if (c is not None and r.get("base_calibration_sha256") == base_sha
                        and r.get("train_scope") == "public_external_only"
                        and p30 and math.isfinite(p30) and p30 > 0):
                    c["p30"] = p30
                    c["p70_calibration_n"] = r.get("n")
                else:
                    self.notes.append("P70 trial calibration is unavailable or does not match the base tables")

        # F1 / V2 / T1 for a NEW schedule: attach_v2f1f2() loads them
        self.size_cells = None
        self.comparables = None
        self.t1 = None
        self.D = None
        # MC for a NEW schedule: attach_v2f1f2() builds the municipal cells
        self.ref_tok_m = self.ref_band_m = self.ref_m = None

    def attach_v2f1f2(self):
        """F1's size cells and V2's comparables pools for pricing a NEW
        schedule on the published tables, fitted on the external price pool
        as it stands (size cells over every letting, like the published
        reference cells; a comparable must still be dated before the
        schedule's own bid date -- estimate()'s target["date"], else today).
        Reads master_panel.csv once."""
        sizes = [] if SIZE_FALLBACK else None
        pool = _reference_pool(sizes)
        avail = _letting_dates()
        self.size_cells = (SizeCells(pool, sizes, avail)
                           if sizes is not None else None)
        self.comparables = comparables_pools(pool, avail)
        # T1: the mobilization ratio from the pool; D1 records and model
        # totals from the last calibrate_forward(write=True)
        self.t1 = (T1State(pool, avail).load(os.path.join(CLEAN, T1_FILE))
                   if TXDOT_C4D1 else None)
        # MC: municipal-only cells over every letting, as the published
        # tables are (a cell counts with >= MUNI_CELLS_NMIN lettings)
        self.ref_tok_m = self.ref_band_m = self.ref_m = None
        if MUNI_CELLS:
            cm = _reference_cells([r for r in pool if r[0] == "municipal"])
            self.ref_tok_m = _MuniMap(cm["tok"], None, MUNI_CELLS_NMIN, "tok", {})
            self.ref_band_m = _MuniMap(cm["band"], None, MUNI_CELLS_NMIN,
                                       "band", {})
            self.ref_m = _MuniMap(cm["fam"], None, MUNI_CELLS_NMIN, "fam", {})
        # low confidence: the earlier low bidders' share per class (display only)
        self.lowconf = LowConfShare(pool, avail) if LOWCONF else None
        # P_L (round 13): the market levels over every letting (forward
        # reads still stop before the schedule's own bid date)
        self.market_level = (MarketLevel(_reference_cells(pool),
                                         {r[1]: r[0] for r in pool}, avail)
                             if MUNI_TXDOT_LEVEL else None)
        return self

    # -- the basis check, run rather than asserted in a comment -------------
    def assert_all_bids_basis(self):
        """price_reference must be built from all-bids rows only.  If a
        contract or winning price got into the pool, the discount below is
        applied to a number that already contains it."""
        seen = collections.Counter()
        p = os.path.join(CLEAN, "master_panel.csv")
        if not os.path.exists(p):
            self.notes.append("master_panel.csv absent; basis unverified")
            return None
        csv.field_size_limit(10 ** 8)
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("source_set") != "external":
                    continue
                if (r.get("is_engineer_estimate") or "").lower() in (
                        "true", "1", "yes"):
                    continue
                if r.get("competition_class") != "competitive":
                    continue
                if r.get("project_kind") != "construction":
                    continue
                if r.get("price_basis") == "uneven_proposal":
                    continue
                seen[r.get("price_basis") or ""] += 1
        bad = {k: v for k, v in seen.items() if k != "all_bids"}
        if bad:
            self.notes.append("PRICE BASIS CONTAMINATED: %r -- the discount "
                              "would be applied twice on those rows" % bad)
        return seen


def discount(tab, n_bidders, market):
    """f(n), with the table chosen by market and the choice reported."""
    table = tab.disc_muni if market == "municipal" and tab.disc_muni else tab.disc
    which = ("municipal" if table is tab.disc_muni else "pooled")
    if not table:
        return None
    if n_bidders is not None and n_bidders <= 1:
        # One bid is not a competition and there is no order statistic to
        # discount.  The municipal table has no 1-bidder row, so letting
        # MAX_BIN_DISTANCE reach the 2-bidder bin invented a 7.4% discount on
        # a sole bid.
        return {"f": 1.0, "p25": 1.0, "p75": 1.0, "bin": 1,
                "table": which, "lettings": 0, "extrapolated": False,
                "note": "single bid: no competition, no discount"}
    k = min(table, key=lambda x: abs(x - n_bidders))
    if abs(k - n_bidders) > MAX_BIN_DISTANCE:
        if not (CLAMP_ABOVE_TOP_BIN and n_bidders > max(table)):
            return None
        k = max(table)          # C: above the top bin -> the top bin
    row = table[k]
    return {"f": row["f"], "p25": row["p25"], "p75": row["p75"],
            "bin": k, "table": which, "lettings": row["lettings"],
            "extrapolated": k != n_bidders}


def market_index_key(tab, market):
    """Which arm of index_by_market.csv belongs to this market.

    Not a hardcoded "Municipal": the market labels in that file are written by
    another script, and if a label changes, a hardcoded lookup misses silently
    and the estimator borrows the statewide escalation for municipal work.  The
    two are not interchangeable -- statewide fell 12.5% in 2025->2026 while DFW
    municipal fell 7.3% -- so the match is made on the labels actually present
    and a miss is reported rather than absorbed.
    """
    want = "municipal" if market == "municipal" else "txdot"
    for k in tab.index:
        if k.startswith("_"):
            continue
        if k.strip().lower() == want:
            return k
    return None


def _index_at(d, year):
    """The index at a possibly FRACTIONAL year.

    vintage_year is dollar-weighted, so 516 of 761 price_reference rows (67.8%)
    carry a fraction -- 2023.82 means the dollars behind that price sit four
    fifths of the way through 2023.  Truncating it to "2023" escalated every
    such price from the start of its vintage year and so billed it an extra
    fraction of a year of index movement: median +4.06% per row, worst +24.97%,
    and on the full-path back-test over 2,034 real lettings the median
    est/actual sat at 1.1766 instead of 1.1001 and the median error at 35.2%
    instead of 33.4%.  Interpolated geometrically, because an index compounds.
    """
    try:
        # Rounded first: pandas writes a cell whose dollars all sit in 2021
        # as 2020.9999999999998, which floors to 2020 -- and on an arm with
        # no 2020 entry (Municipal) that silently switches the escalation to
        # the _default index.
        y = round(float(year), 6)
    except (TypeError, ValueError):
        return None
    lo = int(math.floor(y))
    frac = y - lo
    a = d.get(str(lo))
    if a is None or a <= 0:
        return None
    if frac <= 1e-9:
        return a
    b = d.get(str(lo + 1))
    if b is None or b <= 0:
        # No next year to interpolate into: use the floor and let the caller
        # see the shortfall rather than inventing a rate.
        return a
    return a * (b / a) ** frac


def _carry_to(d, to_year):
    """The arm `d` with its last level carried FLAT to `to_year` when the
    index stops before that year, plus the last year it really has (None
    when nothing was carried).

    Without this, a year the index has no row for got NO escalation at all:
    every item was priced at its vintage and years of index movement
    silently dropped out.  Replayed forward -- each letting priced only from
    lettings dated before it -- that hit the first TxDOT lettings of every
    January (P50 at 0.68 of the low bid), and it would hit EVERY letting
    priced to 2027 until the index has a 2027 row.  Flat, because a level
    nobody has measured yet is not a trend anybody can defend.  Every arm is
    carried, so municipal work stays on the municipal arm instead of jumping
    to _default the day a statewide letting opens the new year.  Measured
    forward on the 2024-26 lettings: TxDOT P80 misses 21.8% -> 21.4%,
    municipal unchanged (22.4%).

    NARROW since 2026-11-19 (see escalation): it is applied only when NO arm
    of the index has to_year at all.
    """
    try:
        y = int(math.floor(round(float(to_year), 6)))
    except (TypeError, ValueError):
        return d, None
    yrs = [int(k) for k in d if str(k).isdigit()]
    if not yrs or max(yrs) >= y:
        return d, None
    last = max(yrs)
    out = dict(d)
    for yy in range(last + 1, y + 1):
        out[str(yy)] = d[str(last)]
    return out, last


def _carry_note(esc, to_year):
    return ("index carried flat from %s to %s: the %s arm has no later row"
            % (esc["carried_from"], to_year, esc["basis"]))


def _any_arm_has_year(index, to_year):
    try:
        y = str(int(math.floor(round(float(to_year), 6))))
    except (TypeError, ValueError):
        return False
    return any(isinstance(d, dict) and y in d for d in (index or {}).values())


def escalation(tab, from_year, to_year, market):
    """index(to) / index(from), market-specific where the market has it.

    from_year may be fractional; to_year is the calendar year being priced.

    Carrying a year the index lacks (_carry_to) is NARROW: only when no arm of
    the index has to_year at all -- e.g. every letting priced to 2027 before
    the index has a 2027 row, which otherwise got no escalation whatsoever.
    When any arm has the year, the arms are used as they are (the market arm
    if it has the year, else _default), exactly as before the carry existed.
    Why narrow (forward-floor verification, 2026-11-19): carrying every arm
    that stops early, which the pre-registered remedy did, cost 4.0 points of
    in-band share against the guard alone on the same 2024-26 municipal
    lettings (90% CI -8.0 to -0.8) without improving the floor; on those
    lettings the narrow carry changes only one letting and scores exactly as
    the guard alone (19.2% under, 8.8% in band), and it changes the same 12
    TxDOT lettings as the full carry.  It was proposed AFTER seeing those
    results, so it is registered for an out-of-sample check on lettings from
    2027-01 on (reports/forward_floor_integration.md).  The result says when
    it carried, in `carried_from`.
    """
    if from_year in (None, "") or not to_year:
        return None
    key = market_index_key(tab, market)
    carry = not _any_arm_has_year(tab.index, to_year)
    tried = []
    for src in ([key] if key else []) + ["_default"]:
        if carry:
            d, carried = _carry_to(tab.index.get(src) or {}, to_year)
        else:
            d, carried = tab.index.get(src) or {}, None
        a = _index_at(d, from_year)
        b = _index_at(d, to_year)
        if a and b and a > 0:
            return {"factor": b / a, "basis": src, "borrowed": src != key,
                    "market_arm_found": key is not None,
                    "from_year": from_year, "carried_from": carried}
        tried.append(src)
    return {"factor": None, "basis": None, "borrowed": None,
            "market_arm_found": key is not None, "tried": tried,
            "available_markets": sorted(k for k in tab.index
                                        if not k.startswith("_"))}


def _r13_level(tab, out, kind, key, fk, market):
    """P_L (MUNI_TXDOT_LEVEL; round 13): a priced municipal line from a
    "band" cell or a "fam" cell (the family cell, the substituted or unsized
    bucket, or F1's nearest sized cell) x R ** s (MarketLevel): s = that
    cell's TxDOT share at D, 0 for a municipal-only (MC) cell; R = the
    municipal / TxDOT level of the line's family.  Token picks are not
    passed here.  Writes txdot_level into the line's trace."""
    if not _municipal(market) or not out.get("price"):
        return
    ml = getattr(tab, "market_level", None)
    if ml is None:
        return
    D = getattr(tab, "D", None)
    sc = 0.0 if out.get("muni_cell") else ml.share(kind, key, D)
    if not sc:
        return
    R, scope, nc = ml.ratio(_base_family(fk or ""), D)
    if R is None:
        return
    fct = R ** sc
    out["price"] *= fct
    out["txdot_level"] = {"share": sc, "R": R, "scope": scope, "cells": nc,
                          "factor": fct}
    out["why"].append(
        "P_L: TxDOT share %.2f of the cell's lettings; municipal/TxDOT level "
        "%.4f (%s, %d cells): x %.4f" % (sc, R, scope, nc, fct))


def item_market_price(tab, family, unit, quantity, size_bucket="",
                      to_year=None, market="municipal",
                      description="", size_in=None):
    """The ALL-BIDS market unit price for one pay item, escalated and
    quantity-adjusted.  No discount here -- that belongs on the total.
    size_in: the line's own parsed size (F1, see SizeCells)."""
    out = {"family": family, "unit": unit, "quantity": quantity,
           "price": None, "why": [], "usable": None, "price_key": None}
    # the band and family tables key a removal-only line apart (family_key)
    fk = family_key(family, description)

    # Tier 0: token identity.  Jaccard-weighted median over the reference
    # cells whose words match this item's, in the same unit and quantity band,
    # with dimensions as a hard constraint (18-inch pipe is not 24-inch pipe
    # however many trade words they share).
    qb0 = qty_shape.band_of(quantity) if quantity and quantity > 0 else ""
    if qb0 and unit and tab.ref_tok:
        t = item_tokens.tokens(description or "")
        if t:
            cand = set()
            for w in t:
                for k in tab.tok_inv.get(unit, {}).get(w, ()):
                    if k[2] == qb0:
                        cand.add(k)
            hits = []
            for k in cand:
                j = item_tokens.similar(t, k[0])
                if j <= 0:
                    continue
                row = tab.ref_tok[k]
                pv = _f(row.get("price_median"))
                np_ = _f(row.get("n_projects")) or 0
                if pv and pv > 0 and np_ >= 2:
                    hits.append((j, pv, row))
            if hits:
                # T1 (C4, TxDOT): an exact word-set cell (Jaccard 1; at most
                # one: the cells are keyed by word set, unit and band) with
                # >= C4_MIN_N lettings prices the line by itself
                exact = None
                if TXDOT_C4D1 and (market or "").strip().lower() == "txdot":
                    ex = [h for h in hits if h[0] >= 1.0 and
                          (_f(h[2].get("n_projects")) or 0) >= C4_MIN_N]
                    exact = ex[0] if ex else None
                # Equal-price cells can carry different vintages, and a sort
                # on price alone left the pick to the candidate SET's order,
                # which moves with PYTHONHASHSEED: 37-38 of 241 public
                # municipal lettings changed total between runs with another
                # hash seed, by up to 0.85%.
                hits.sort(key=lambda x: (x[1],
                                         _f(x[2].get("vintage_year")) or 0.0,
                                         x[2].get("tok") or ""))
                tw = sum(h[0] for h in hits)
                acc = 0.0
                pick = hits[len(hits) // 2]
                for h in hits:
                    acc += h[0]
                    if acc >= tw / 2:
                        pick = h
                        break
                if exact is not None:
                    pick = exact
                    out["token_exact_rule"] = True
                p = pick[1]
                row = pick[2]
                if row.get("cell_market") == "municipal":
                    out["muni_cell"] = True        # MC
                out["price_key"] = "token_jaccard"
                out["token_match_jaccard"] = round(pick[0], 4)
                out["token_candidates"] = len(hits)
                out["usable"] = row.get("usable")
                out["n_projects"] = _f(row.get("n_projects"))
                out["n_owners"] = _f(row.get("n_owners"))
                out["qty_band"] = qb0
                v = row.get("vintage_year")
                esc = escalation(tab, v, to_year, market) if to_year else None
                if esc and esc.get("factor"):
                    p *= esc["factor"]
                    out["escalation"] = esc["factor"]
                    out["escalation_basis"] = esc["basis"]
                    if esc.get("borrowed"):
                        out["why"].append("escalation BORROWED from %s"
                                          % esc["basis"])
                    if esc.get("carried_from"):
                        out["why"].append(_carry_note(esc, to_year))
                elif to_year:
                    out["why"].append("no index path %s -> %s" % (v, to_year))
                out["vintage_year"] = v
                out["price"] = p
                out["why"].append(
                    "matched on item words, Jaccard %.2f over %d candidate "
                    "cells" % (pick[0], len(hits)))
                return out

    # Tier 1: the band-keyed reference, where it has >= 2 lettings behind it.
    # Priced from other lettings' items in the SAME quantity band rather than
    # from a pooled median scaled into that band -- measured 4 points better on
    # municipal and 19 on TxDOT, matched population, leave-one-letting-out.
    # F1 trigger: a sized line whose family+unit has NO usable band cell for
    # its size bucket in any quantity band never takes the unsized cell.
    f1 = False
    si = getattr(tab, "size_cells", None) if SIZE_FALLBACK else None
    if si is not None and size_bucket and unit and family:
        if size_in is not None and size_in > 0 \
                and not _band_has_size(tab, fk, unit, size_bucket):
            if F1_PIPE_ONLY and _municipal(market) \
                    and not _f1p_ok(family, description, size_in):
                out["f1p_blocked"] = True           # F1P: not pipe-like
            else:
                f1 = True
                out["f1"] = {"size": size_in}
    qb = qty_shape.band_of(quantity) if quantity and quantity > 0 else ""
    rb_row = None
    if qb and tab.ref_band:
        rb_row = tab.ref_band.get((fk, unit, size_bucket, qb))
        _kb13 = (fk, unit, size_bucket, qb)
        if rb_row is None and not f1:          # F1: never the unsized cell
            rb_row = tab.ref_band.get((fk, unit, "", qb))
            _kb13 = (fk, unit, "", qb)
        if rb_row is not None and (_f(rb_row.get("n_projects")) or 0) < 2:
            rb_row = None
    if rb_row is not None:
        p = _f(rb_row.get("price_median"))
        if p and p > 0:
            if rb_row.get("cell_market") == "municipal":
                out["muni_cell"] = True            # MC
            out["price_key"] = "family+unit+size+band"
            out["usable"] = rb_row.get("usable")
            out["n_projects"] = _f(rb_row.get("n_projects"))
            out["n_owners"] = _f(rb_row.get("n_owners"))
            out["qty_band"] = qb
            v = rb_row.get("vintage_year")
            esc = escalation(tab, v, to_year, market) \
                if to_year else None
            if esc and esc.get("factor"):
                p *= esc["factor"]
                out["escalation"] = esc["factor"]
                out["escalation_basis"] = esc["basis"]
                if esc.get("borrowed"):
                    out["why"].append("escalation BORROWED from %s"
                                      % esc["basis"])
                if esc.get("carried_from"):
                    out["why"].append(_carry_note(esc, to_year))
            elif to_year:
                out["why"].append("no index path %s -> %s" % (v, to_year))
            out["vintage_year"] = v
            out["price"] = p
            # No band shape adjustment here, on purpose: the band is already
            # in the key, so applying a multiplier on top would move the price
            # out of the band it was just measured in.
            if MUNI_TXDOT_LEVEL:
                _r13_level(tab, out, "band", _kb13, fk, market)
            return out

    # Tier 2: the pooled family median, scaled into this item's band.
    if not unit:
        # Distinguish a parse failure from a coverage gap: "no price_reference
        # entry" sends the reader hunting for missing data when the line item
        # simply has no unit on it.
        out["why"].append("no unit on this line item -- cannot be priced")
        return out
    if not family:
        out["why"].append("no item_family on this line item -- cannot be "
                          "priced")
        return out
    f1_factor = None
    if f1:
        # F1: the nearest sized cell of the same family+unit, nearest in
        # |ln(size / size_ref)| (ties: more lettings, then bucket label),
        # scaled by (size / size_ref) ** k; then quantity shape and
        # escalation exactly as below.
        D = getattr(tab, "D", None)
        # chosen on the pooled table; MC takes the chosen cell's municipal
        # row below when it qualifies; T2 drops cells under T2_MIN lettings
        sel = getattr(tab.ref, "pooled", tab.ref)
        t2 = FAMILY_CELL_T2 and _municipal(market)
        cands, t2_drop = [], []
        for key in si.sized.get((fk, unit), ()):
            r_ = sel.get(key)
            if r_ is None:
                continue
            sr = si.size_ref(key, D)
            if not sr or sr <= 0:
                continue
            pm = _f(r_.get("price_median"))
            if not pm or pm <= 0:
                continue
            c_ = (abs(math.log(size_in / sr)),
                  -(_f(r_.get("n_projects")) or 0), key[2], key, r_, sr)
            if t2 and (_f(r_.get("n_projects")) or 0) < T2_MIN:
                t2_drop.append(c_)                  # T2: under 2 lettings
                continue
            cands.append(c_)
        if t2_drop and (not cands or min(t2_drop) < min(cands)):
            out["t2_skipped"] = True
        if not cands:
            # no sized cell: the unsized cell is not used, and the token tier
            # (tried first) found nothing -- the line stays unpriced
            out["f1"]["no_sized_cell"] = True
            out["why"].append("F1: no sized cell of this family+unit; the "
                              "unsized cell is not used and the token tier "
                              "found nothing")
            if fk != family and tab.ref.get((fk, unit, "")) is None:
                out["removal_no_cell"] = True
                if fk.endswith(ABANDON_SUFFIX):
                    out["abandon_no_cell"] = True        # AB2 (round 17)
            return out
        c = min(cands)
        _k13 = c[3]
        row, sr = c[4], c[5]
        row = tab.ref.get(c[3], row)          # MC: municipal row if it qualifies
        k, kinfo = si.k_at(fk, unit, D)
        f1_factor = (size_in / sr) ** k
        out["f1"].update({"ref_key": c[3], "size_ref": sr, "k": k,
                          "k_src": kinfo["src"], "k_n": kinfo.get("n"),
                          "k_sizes": kinfo.get("sizes"),
                          "k_lettings": kinfo.get("lettings"),
                          "factor": f1_factor})
        out["why"].append("F1: size %g not in any band; nearest sized cell %r "
                          "(size %.3g) x (%g/%.3g)^%.3f" % (
                              size_in, c[3][2], sr, size_in, sr, k))
    else:
        row = tab.ref.get((fk, unit, size_bucket))
        _k13 = (fk, unit, size_bucket)
        if row is not None and FAMILY_CELL_T2 and _municipal(market) \
                and (_f(row.get("n_projects")) or 0) < T2_MIN:
            row = None                              # T2: fall through
            out["t2_skipped"] = True
    if row is None and not f1:
        # "" is not a neutral bucket, it is the leftovers: rows whose size was
        # not parsed. Prefer the best-evidenced vetted bucket for this
        # family+unit over it, and say which was substituted.
        cands = [(k, v) for k, v in tab.ref.items()
                 if k[0] == fk and k[1] == unit]
        vetted = [(k, v) for k, v in cands
                  if (v.get("usable") == "yes" and k[2])]
        pick = max(vetted, key=lambda kv: _f(kv[1].get("n_projects")) or 0)             if vetted else None
        if pick is not None:
            row = tab.ref.get(pick[0], pick[1])   # MC: municipal row if it qualifies
            _k13 = pick[0]
            out["why"].append("size_bucket %r not in the reference; "
                              "substituted the best-evidenced bucket %r (%s "
                              "projects)" % (size_bucket, pick[0][2],
                                             pick[1].get("n_projects")))
        else:
            row = tab.ref.get((fk, unit, ""))
            _k13 = (fk, unit, "")
            if row is not None and FAMILY_CELL_T2 and _municipal(market) \
                    and (_f(row.get("n_projects")) or 0) < T2_MIN:
                row = None                          # T2: fall through
                out["t2_skipped"] = True
            if row is not None:
                out["why"].append(
                    "size_bucket %r not in the reference; fell back to the "
                    "UNSIZED bucket, which is the rows whose size could not "
                    "be parsed" % (size_bucket,))
    if row is None:
        out["why"].append("no price_reference entry")
        if fk != family:
            # a removal-only line with no removal cell: estimate() gives it
            # the schedule's typical line, never a new-work price
            out["removal_no_cell"] = True
            if fk.endswith(ABANDON_SUFFIX):
                out["abandon_no_cell"] = True            # AB2 (round 17)
        return out
    out["price_key"] = "f1_nearest_size" if f1 else "family+unit+size"
    if row.get("cell_market") == "municipal":
        out["muni_cell"] = True                    # MC
    base = _f(row.get("price_median"))
    if not base or base <= 0:
        out["why"].append("price_median missing")
        return out
    out["usable"] = row.get("usable")
    out["n_projects"] = _f(row.get("n_projects"))
    out["n_owners"] = _f(row.get("n_owners"))
    p = base
    if f1_factor is not None:
        p *= f1_factor

    # quantity shape, relative to the band the reference price sits in
    ref_qty = _f(row.get("qty_median"))
    rb = qty_shape.band_of(ref_qty) if ref_qty and ref_qty > 0 else ""
    if qb and rb and qb != rb:
        a = tab.shape.get((family, unit, qb))
        b = tab.shape.get((family, unit, rb))
        ma, mb = _f(a.get("shape_multiplier")) if a else None, \
            _f(b.get("shape_multiplier")) if b else None
        if ma and mb and mb > 0:
            p *= ma / mb
            out["qty_factor"] = ma / mb
            dist = abs(qty_shape.BAND_INDEX.get(qb, 0)
                       - qty_shape.BAND_INDEX.get(rb, 0))
            out["qty_sigma"] = tab.borrow_sigma.get(dist)
            out["why"].append("quantity band %s vs reference %s" % (qb, rb))
        else:
            out["why"].append("no shape multiplier for band %s; quantity "
                              "effect NOT applied" % qb)
    out["qty_band"] = qb

    v = row.get("vintage_year")
    esc = escalation(tab, v, to_year, market) if to_year else None
    if esc and esc.get("factor"):
        p *= esc["factor"]
        out["escalation"] = esc["factor"]
        out["escalation_basis"] = esc["basis"]
        if esc.get("borrowed"):
            # Loud, because the two markets do not escalate alike and this is
            # the step that silently substitutes one for the other.
            out["why"].append(
                "escalation BORROWED from %s%s -- DFW municipal and statewide "
                "do not move together" % (esc["basis"],
                                          "" if esc.get("market_arm_found")
                                          else "; no municipal arm in the "
                                               "index at all"))
        if esc.get("carried_from"):
            out["why"].append(_carry_note(esc, to_year))
    elif to_year:
        extra = ""
        if esc and not esc.get("market_arm_found"):
            extra = " (index has arms %r, none matching this market)" % (
                esc.get("available_markets"),)
        out["why"].append("no index path %s -> %s; price left at vintage%s"
                          % (v, to_year, extra))
    out["vintage_year"] = v
    out["price"] = p
    if MUNI_TXDOT_LEVEL:
        _r13_level(tab, out, "fam", _k13, fk, market)
    return out


def estimate(tab, items, n_bidders, market="municipal", to_year=None,
             target=None):
    """items: [{family, unit, quantity, size_bucket?}]  ->  expected low bid.

    Returns the market total, the discount actually used, the point estimate
    and a calibrated interval -- plus a per-item trace, because an estimate
    nobody can take apart is an estimate nobody can defend in a bid review.

    Coverage scope (COVERAGE_SCOPE): lines coverage.py names are not priced
    and come back under `uncovered`; P50/P80 cover the rest; not_covered
    (the rule above COVERAGE_SCOPE) gives no estimate at all.  An item may
    carry `desc_norm` (else its description is folded) and `quote`.
    """
    # The market argument picks the discount table, so a typo in it silently
    # swaps DFW municipal work onto the statewide highway discount.  Validate
    # rather than default.
    mk = (market or "").strip().lower()
    if mk not in ("municipal", "txdot"):
        raise ValueError(
            "market must be 'municipal' or 'txdot', got %r -- it selects the "
            "competition discount table and a wrong value changes the "
            "estimate" % (market,))
    market = mk
    # MC: a municipal line reads the municipal-only cells first
    ptab = (_MuniView(tab) if (MUNI_CELLS and market == "municipal"
                               and getattr(tab, "ref_m", None) is not None)
            else tab)

    # coverage: which lines are not covered (an allowance-like line whose
    # amount is given stays owner-set money), and is the whole schedule?
    ucls = [None] * len(items)
    if COVERAGE_SCOPE:
        for i, it in enumerate(items):
            dn = it.get("desc_norm")
            c = line_coverage(it.get("description") if dn is None else dn,
                              it.get("family"), it.get("unit"))
            if c and owner_fixed_amount(it) and allowance_like(
                    it.get("family"), it.get("unit"),
                    it.get("description") or ""):
                c = None
            ucls[i] = c
    n_unc = sum(1 for c in ucls if c)
    whole = [i for i, c in enumerate(ucls) if c and is_whole_facility(
        items[i].get("description") if items[i].get("desc_norm") is None
        else items[i].get("desc_norm"), items[i].get("unit"))]
    not_covered = bool(n_unc) and (n_unc > len(items) / 2.0
                                   or (WHOLE_FACILITY_RULE and bool(whole)))
    uncov, uidx, quoted = [], {}, 0.0
    for i, it in enumerate(items):
        if not ucls[i]:
            continue
        qa = uncovered_quote(it)
        uidx[i] = {"item": it.get("item_no") or "",
                   "description": it.get("description") or "",
                   "class": ucls[i], "quantity": _f(it.get("quantity")),
                   "unit": it.get("unit") or "", "status": COVERAGE_STATUS,
                   "quote": qa, "whole_facility": i in whole}
        uncov.append(uidx[i])
        quoted += qa or 0.0

    trace = []
    total = 0.0
    fixed_total = 0.0
    priced = unpriced = n_fixed = 0
    for i, it in enumerate(items):
        q = _f(it.get("quantity"), 0.0) or 0.0
        if ucls[i] or not_covered:
            u = uidx.get(i)
            trace.append({
                "item": it.get("item_no") or it.get("description") or "",
                "family": it.get("family") or "", "unit": it.get("unit") or "",
                "quantity": q, "price": None,
                "price_key": "uncovered" if ucls[i] else "not_covered",
                "uncovered": ucls[i], "quote": u["quote"] if u else None,
                "usable": "uncovered", "allowance_like": False,
                "why": [("%s (%s): no comparable history, not priced%s"
                         % (COVERAGE_STATUS, ucls[i],
                            "; quote %.2f at face value" % u["quote"]
                            if u and u["quote"] else ""))
                        if ucls[i] else
                        "not priced: the schedule as a whole is not covered"]})
            continue
        fx = owner_fixed_amount(it)
        if fx:
            # Owner-set money is copied into every bid, not bid: face value,
            # outside the competition discount (see ALLOWANCE_UNITS).
            trace.append({
                "item": it.get("item_no") or it.get("description") or "",
                "family": it.get("family") or "", "unit": it.get("unit") or "",
                "quantity": q, "price": None, "fixed_amount": fx,
                "price_key": "owner_fixed", "usable": "yes",
                "allowance_like": True,
                "why": ["owner-set amount %.2f taken at face value; not "
                        "estimated and not discounted" % fx]})
            if it.get("identical_bids"):
                trace[-1]["identical_bids"] = True  # D (back-tests)
            fixed_total += fx
            priced += 1
            n_fixed += 1
            continue
        r = item_market_price(ptab, it["family"], it["unit"], q,
                              it.get("size_bucket") or "", to_year, market,
                              it.get("description") or "",
                              _f(it.get("size_in")))
        r["item"] = it.get("item_no") or it.get("description") or ""
        r["allowance_like"] = allowance_like(it.get("family"), it.get("unit"),
                                             it.get("description") or "")
        trace.append(r)
        if r["price"] and q > 0:
            total += r["price"] * q
            priced += 1
        else:
            unpriced += 1

    # A removal-only line whose own (#rm) cells are all empty gets this
    # schedule's TYPICAL line amount: the median market-level amount of its
    # directly priced, non-LS, non-owner-set lines (the rule registered with
    # the removal identity; see family_key).  Pre-bid: it uses only this
    # schedule's own estimated lines.
    if not_covered:
        return _not_covered_out(items, trace, uncov, quoted, n_unc, whole)
    todo = [t for t in trace if t.get("removal_no_cell") and not t.get("price")
            and (t.get("quantity") or 0) > 0]
    if todo:
        amts = [t["price"] * t["quantity"] for t in trace
                if t.get("price") and (t.get("quantity") or 0) > 0
                and not t.get("fixed_amount")
                and not t.get("typical_line_imputed")
                and (t.get("unit") or "").strip().upper() != "LS"]
        if amts:
            a = statistics.median(amts)
            for t in todo:
                t["price"] = a / t["quantity"]
                t["typical_line_imputed"] = True
                t["price_key"] = "removal_typical_line"
                t["why"].append("%s line with no %s reference "
                                "cell: priced at this schedule's typical line "
                                "amount %.2f" % ((
                                    ("abandon-only", "abandonment")
                                    if t.get("abandon_no_cell") else
                                    ("removal-only", "removal")) + (a,)))
                priced += 1
                unpriced -= 1
            total = 0.0
            for t in trace:
                if t.get("fixed_amount"):
                    continue
                if t.get("price") and (t.get("quantity") or 0) > 0:
                    total += t["price"] * t["quantity"]

    # T1 (C4, TxDOT): mobilization priced as a share of the rest of the
    # schedule: r(D) x S split evenly over the mobilization lines, S = the
    # priced non-owner-set, non-mobilization lines (all-bids level,
    # escalated, before the discount), r from >= MOB_MIN_LETTINGS lettings
    # before D.  D = the schedule's bid date (target["date"]), else the
    # tables' date, else today -- the date the comparables use.
    t1 = getattr(tab, "t1", None) if (TXDOT_C4D1 and market == "txdot") \
        else None
    D_t1 = ((target or {}).get("date") or getattr(tab, "D", None)
            or _dt.date.today().isoformat())
    if t1 is not None:
        S, mob = 0.0, []
        for i, (it, t) in enumerate(zip(items, trace)):
            if t.get("fixed_amount") or t.get("uncovered"):
                continue
            if it.get("family") == "mobilization":
                mob.append(i)
                continue
            q = _f(it.get("quantity"), 0.0) or 0.0
            if t.get("price") and q > 0:
                S += t["price"] * q
        rv, nl = t1.mob.at(D_t1) if (S > 0 and mob) else (None, 0)
        if rv is not None and nl >= MOB_MIN_LETTINGS:
            amt = rv * S / len(mob)
            for i in mob:
                q = _f(items[i].get("quantity"), 0.0) or 0.0
                qe = q if q > 0 else 1.0
                t = trace[i]
                if not (t.get("price") and q > 0):
                    priced += 1
                    unpriced -= 1
                t["price_before_ratio"] = t.get("price")
                t["price"] = amt / qe
                t["quantity_effective"] = qe
                t["price_key"] = "mobilization_ratio"
                t["mobilization_ratio"] = rv
                t["mobilization_base"] = S
                t["why"].append("T1: mobilization = %.4f x %.2f (the rest of "
                                "the schedule) over %d line(s); ratio from %d "
                                "earlier lettings" % (rv, S, len(mob), nl))
            total = 0.0
            for it, t in zip(items, trace):
                if t.get("fixed_amount"):
                    continue
                q = t.get("quantity_effective") or (
                    _f(it.get("quantity"), 0.0) or 0.0)
                if t.get("price") and q > 0:
                    total += t["price"] * q

    out = {"market_total": total, "fixed_total": fixed_total,
           "n_fixed": n_fixed, "n_items": len(items), "priced": priced,
           "unpriced": unpriced, "trace": trace, "expected_low": None,
           "warnings": []}
    if COVERAGE_SCOPE:
        out.update({"not_covered": False, "uncovered": uncov,
                    "uncovered_share_lines": (n_unc / len(items)
                                              if items else 0.0),
                    "uncovered_quoted_total": quoted})
        if uncov:
            out["warnings"].append(_uncovered_warning(uncov, len(items)))
    # Nothing priced is a refusal, not a bid of zero.  A total of 0.00 printed
    # under "EXPECTED LOW BID" is the worst possible failure mode here.  Owner-
    # set amounts alone are not an estimate either: nobody bid them.
    if total <= 0:
        out["warnings"].append(
            "NOT A SINGLE item could be priced -- there is no estimate here, "
            "and the market total of %.2f is not one%s"
            % (total, (" (owner-set amounts of %.2f alone are not a bid)"
                       % fixed_total) if fixed_total else ""))
        return out
    if unpriced:
        out["warnings"].append(
            "%d of %d %sitems could not be priced; the total covers only the "
            "rest and is NOT a project estimate"
            % (unpriced, len(items) - n_unc, "covered " if n_unc else ""))

    # EVERY per-item degradation has to reach the caller.  `usable` was being
    # recorded in the trace and never checked, and the trace's own `why` notes
    # never reached `warnings`: over the 2,034-letting back-test, 1,972
    # lettings returned warnings == [] while 37.9% of the estimated dollars
    # came off reference rows the file itself flags thin / no / unclassified.
    # An estimate that looks clean while a third of its money rests on rows
    # marked unusable is worse than one that refuses.
    def _share(pred):
        d = sum((t["price"] or 0) * (t["quantity"] or 0)
                for t in trace if t.get("price") and pred(t))
        return d, (100.0 * d / total if total > 0 else 0.0)

    bad_d, bad_p = _share(lambda t: t.get("usable") != "yes")
    if bad_d > 0:
        n_bad = sum(1 for t in trace
                    if t.get("price") and t.get("usable") != "yes")
        out["warnings"].append(
            "%d items / %.1f%% of the money (%.2f) is priced off reference "
            "rows marked %s -- not usable='yes'. Treat this as an order of "
            "magnitude, not a bid."
            % (n_bad, bad_p, bad_d,
               "/".join(sorted({str(t.get("usable")) for t in trace
                                if t.get("price")
                                and t.get("usable") != "yes"}))))
    noshape_d, noshape_p = _share(
        lambda t: any("quantity effect NOT applied" in w or
                      "no shape multiplier" in w for w in t["why"]))
    if noshape_p >= 1.0:
        out["warnings"].append(
            "%.1f%% of the money had NO quantity adjustment applied -- the "
            "family has no band table, and unit price moves with quantity by "
            "more than it moves with anything else here" % noshape_p)
    noesc_d, noesc_p = _share(
        lambda t: any(w.startswith("no index path") for w in t["why"]))
    if noesc_p >= 1.0:
        out["warnings"].append(
            "%.1f%% of the money was NOT escalated -- no index path from its "
            "vintage to %s, so it is priced at its vintage"
            % (noesc_p, to_year))
    carry_d, carry_p = _share(
        lambda t: any(w.startswith("index carried flat") for w in t["why"]))
    if carry_p >= 1.0:
        out["warnings"].append(
            "%.1f%% of the money was escalated to %s on an index whose last "
            "row is earlier; that level was carried flat, so any move since "
            "is NOT in this estimate" % (carry_p, to_year))
    borrow_d, borrow_p = _share(
        lambda t: any("BORROWED" in w for w in t["why"]))
    if borrow_p >= 1.0:
        out["warnings"].append(
            "%.1f%% of the money was escalated on ANOTHER market's index; "
            "DFW municipal and statewide do not move together" % borrow_p)
    sub_d, sub_p = _share(
        lambda t: any("UNSIZED bucket" in w or "substituted the best" in w
                      for w in t["why"]))
    if sub_p >= 1.0:
        out["warnings"].append(
            "%.1f%% of the money is priced from a different size bucket than "
            "the item's own" % sub_p)
    out["degraded_dollar_share"] = round(bad_p / 100.0, 4)
    al_d, al_p = _share(lambda t: t.get("allowance_like"))
    if al_d > 0:
        n_al = sum(1 for t in trace
                   if t.get("price") and t.get("allowance_like"))
        out["warnings"].append(
            "%d item(s), %.1f%% of the money (%.2f), look like owner-set "
            "allowances or contingencies and were priced off OTHER projects' "
            "amounts -- give each one's amount (an 'amount' column, or the "
            "unit price on the line) so it is taken at face value"
            % (n_al, al_p, al_d))
    d = discount(tab, n_bidders, market)
    if not d:
        out["warnings"].append(
            "no competition discount within %d bins of %s bidders -- refusing "
            "to extrapolate; the market total above has NO discount in it"
            % (MAX_BIN_DISTANCE, n_bidders))
        return out
    if t1 is not None and total > 0:
        # T1 (D1, TxDOT): the discount of the earlier scored lettings with
        # this bidder class and this model size class, >= D1_MIN of them
        fv = t1.d1(n_bidders, total, D_t1)
        if fv is not None:
            d = dict(d, f=fv, d1=True)
        elif t1.loaded is False:
            out["warnings"].append(
                "T1: no D1 records (%s missing; calibrate_forward(write=True) "
                "writes it) -- the production f(n) is used" % T1_FILE)
    out["discount"] = d
    if d["extrapolated"]:
        out["warnings"].append("bidder count %s priced off the %d-bidder bin"
                               % (n_bidders, d["bin"]))
    if d["lettings"] < 20:
        out["warnings"].append("that discount bin rests on %d lettings"
                               % d["lettings"])
    # V2: the P50 bid part is the geometric mean of the comparables price
    # and the line-priced bid part; owner-set money still goes on last.
    out["bid_part_lines"] = total * d["f"]
    out["bid_part"] = out["bid_part_lines"]
    if n_unc:                  # coverage: the uncovered lines stay out of V2
        keep = [i for i, t in enumerate(trace) if not t.get("uncovered")]
        cm = _comparables_price(tab, [items[i] for i in keep],
                                [trace[i] for i in keep], n_bidders, market,
                                to_year, d, target)
    else:
        cm = _comparables_price(tab, items, trace, n_bidders, market, to_year,
                                d, target)
    if cm is not None:
        out["comparables"] = cm
        out["bid_part"] = math.sqrt(cm["cmp"] * out["bid_part_lines"])
        if not cm["n_comps"]:
            out["warnings"].append(
                "no comparable earlier letting (overlap >= %.2f) -- the bid "
                "part is the line-priced one" % comparables.O_MIN)
    out["expected_low"] = out["bid_part"]

    c = tab.calib.get(market) or tab.calib.get("all")
    if c:
        lc = _f(c.get("level_correction"))
        if lc and lc > 0 and abs(lc - 1.0) > 1e-9:
            out["expected_low"] *= lc
            out["level_correction"] = lc
            out["warnings"].append(
                "centred by x%.4f, a factor measured out of fold because the "
                "uncorrected path runs %s on this market; it moves the centre "
                "only and does not narrow the interval"
                % (lc, "low" if lc > 1 else "high"))
    # Owner-set money goes on last: no discount and no centring belong on an
    # amount nobody estimated or bid.
    if fixed_total:
        out["expected_low"] += fixed_total
    if c and total > 0:
        # calibration holds quantiles of est/actual, so actual = est / ratio
        for lo_k, hi_k, name in (("p25", "p75", "p50_band"),
                                 ("p10", "p90", "p80_band")):
            lo, hi = _f(c.get(lo_k)), _f(c.get(hi_k))
            if lo and hi and lo > 0 and hi > 0:
                if c.get("quantiles_basis") == "centred":
                    # quantiles of the centred ratio, the one the estimate
                    # above actually has: invert them directly
                    out[name] = (out["expected_low"] / hi,
                                 out["expected_low"] / lo)
                else:
                    # older files hold quantiles of the UNCORRECTED ratio, so
                    # the centring factor has to come back out of them or the
                    # band would be shifted twice
                    lcf = out.get("level_correction") or 1.0
                    out[name] = (out["expected_low"] / (hi * lcf),
                                 out["expected_low"] / (lo * lcf))
        # "Better high than low": the user's rule is that an estimate must not come in under
        # the low bid -- at least 80% of the time -- and should stay close when
        # over.  The figure that meets the first half is the estimate divided
        # by a low quantile of the centred est/actual ratio.  A file written by
        # calibrate_forward() carries that quantile as p_not_under, at the
        # level its not-under guard chose (never above the 20th percentile);
        # an older file has only p20.  How close it stays is the calibration's
        # spread, reported beside it, not hidden in it.
        p20 = _f(c.get("p_not_under")) or _f(c.get("p20"))
        if c.get("quantiles_basis") == "centred" and p20 and p20 > 0:
            out["p80_multiplier"] = 1.0 / p20
            out["expected_low_p80"] = out["expected_low"] / p20
            lvl = _f(c.get("not_under_level"))
            if lvl:
                out["not_under_level"] = lvl
        # P70 uses the empirical p30, never a blend of P50/P80 amounts.
        # The nominal 30% under target does not use the P80 guard.
        p30 = _f(c.get("p30"))
        if (c.get("quantiles_basis") == "centred" and p30
                and math.isfinite(p30) and p30 > 0):
            out["p70_multiplier"] = 1.0 / p30
            out["expected_low_p70"] = out["expected_low"] / p30
            out["p70_calibration_n"] = _f(c.get("p70_calibration_n") or c.get("n"))
        out["calibration_n"] = _f(c.get("n"))
        out["calibration_market"] = c.get("market")
    else:
        out["warnings"].append("no calibration for market %r; point estimate "
                               "only, no interval" % market)
    if uncov and out.get("expected_low") is not None:
        # the uncovered lines' quotes, at face value, beside the covered
        # P50/P80 -- a complete total only when every one of them has one
        n_q = sum(1 for u in uncov if u["quote"])
        out["uncovered_quoted_n"] = n_q
        if n_q == len(uncov):
            out["complete_total"] = out["expected_low"] + quoted
            if out.get("expected_low_p70"):
                out["complete_total_p70"] = out["expected_low_p70"] + quoted
            if out.get("expected_low_p80"):
                out["complete_total_p80"] = out["expected_low_p80"] + quoted
        elif n_q:
            out["warnings"].append(
                "only %d of %d uncovered lines have a quote (%.2f) -- no "
                "complete total until every one has" % (n_q, len(uncov),
                                                        quoted))
    if LOWCONF:                # low confidence: presentation only, reads `out`
        _attach_low_confidence(out, tab, items, market, target)
    return out


def _attach_low_confidence(out, tab, items, market, target):
    """estimate()'s low-confidence block (LOWCONF).  Presentation only: it reads the
    finished estimate and writes out["low_confidence"], never a figure the
    estimate already has.  One entry per low-confidence class present in the
    covered, non-owner-set lines: our amount (market level, P50 level = x
    (P50 - owner-set) / market total, P80 level), its share of the P50, the
    earlier low bidders' share of contract for that class (tab.lowconf, a
    LowConfShare; absent on bare Tables, then no range) and the dollar range
    it implies for the rest of this schedule's P50, with where ours sits."""
    trace = out.get("trace") or []
    tot = out.get("market_total") or 0.0
    el = out.get("expected_low")
    fx = out.get("fixed_total") or 0.0
    labels = dict(LOWCONF_CLASSES)
    groups = collections.OrderedDict()
    for it, t in zip(items, trace):
        if (t.get("uncovered") or t.get("fixed_amount")
                or t.get("price_key") == "not_covered"):
            continue
        dn = it.get("desc_norm")
        c = lowconf_class(it.get("family"), it.get("unit"),
                          it.get("description") if dn is None else dn)
        if not c:
            continue
        q = t.get("quantity_effective") or (_f(it.get("quantity"), 0.0)
                                            or 0.0)
        g = groups.setdefault(c, {
            "class": c, "label": labels[c], "status": LOWCONF_LABEL,
            "items": [], "descriptions": [], "ours_market": 0.0,
            "unpriced": 0})
        g["items"].append(it.get("item_no") or "")
        g["descriptions"].append(it.get("description") or "")
        if t.get("price") and q > 0:
            g["ours_market"] += t["price"] * q
        else:
            g["unpriced"] += 1
    s50 = (el - fx) / tot if (el is not None and tot > 0) else None
    m80 = out.get("p80_multiplier")
    hist = getattr(tab, "lowconf", None)
    D = ((target or {}).get("date") or getattr(tab, "D", None)
         or _dt.date.today().isoformat())
    for g in groups.values():
        if s50 is None:
            continue
        g["ours_p50"] = g["ours_market"] * s50
        g["ours_p80"] = g["ours_p50"] * m80 if m80 else None
        g["share_of_p50"] = g["ours_p50"] / el if el else None
        rest = el - fx - g["ours_p50"]
        g["rest_p50"] = rest
        h = (hist.at(market, g["class"], rest, D)
             if (hist is not None and rest > 0) else None)
        g["history"] = h
        if h:
            rng = tuple(rest * x / (1.0 - x)
                        for x in (h["p25"], h["median"], h["p75"]))
            g["implied_p50_range"] = (rng[0], rng[2])
            g["implied_p50_median"] = rng[1]
            g["position"] = ("above" if g["ours_p50"] > rng[2] else
                             "below" if g["ours_p50"] < rng[0] else "within")
    out["low_confidence"] = {
        "label": LOWCONF_LABEL, "headline_unchanged": True,
        "history": ("low bidders of %s lettings before %s"
                    % (market, D)) if hist is not None else None,
        "lines": list(groups.values())}


def _uncovered_warning(uncov, n_items):
    """The warning an estimate with uncovered lines leads with."""
    cls = sorted({u["class"] for u in uncov})
    q = sum(u["quote"] or 0.0 for u in uncov)
    return ("%s: %d of %d lines (%s) are specialty items with almost no "
            "comparable bid history -- NOT priced and NOT in P50/P80, which "
            "cover the other lines only; price them separately (vendor "
            "quotes) and add them: %s%s"
            % (COVERAGE_STATUS, len(uncov), n_items, ", ".join(cls),
               "; ".join("%s %s" % (u["item"] or "-", u["description"][:60])
                         for u in uncov[:8])
               + (" ..." if len(uncov) > 8 else ""),
               ("  [quotes given: %.2f]" % q) if q else ""))


def _not_covered_out(items, trace, uncov, quoted, n_unc, whole):
    """estimate()'s answer for a schedule that is not covered as a whole:
    no market total, no P50/P80 -- the uncovered list and the warning."""
    why = []
    if n_unc > len(items) / 2.0:
        why.append("%d of its %d lines are uncovered (more than half)"
                   % (n_unc, len(items)))
    if whole:
        why.append("line %s describes the whole facility (%r, LS)"
                   % (items[whole[0]].get("item_no") or whole[0] + 1,
                      (items[whole[0]].get("description") or "")[:60]))
    w = ("%s AS A WHOLE (not_covered): %s -- the covered lines are only the "
         "periphery of a job we have no comparable for, so NO P50/P80 is "
         "given; price the job from vendor / specialty quotes. Uncovered: %s"
         % (COVERAGE_STATUS, " and ".join(why),
            "; ".join("%s %s" % (u["item"] or "-", u["description"][:60])
                      for u in uncov[:8]) + (" ..." if len(uncov) > 8 else "")))
    return {"market_total": 0.0, "fixed_total": 0.0, "n_fixed": 0,
            "n_items": len(items), "priced": 0, "unpriced": 0, "trace": trace,
            "expected_low": None, "warnings": [w], "not_covered": True,
            "not_covered_why": why, "uncovered": uncov,
            "uncovered_share_lines": n_unc / len(items) if items else 0.0,
            "uncovered_quoted_total": quoted}


def _comparables_price(tab, items, trace, n_bidders, market, to_year, d,
                       target):
    """CMP for this schedule (comparables.py), or None when the blend is off
    or the tables carry no comparables pool for this market.  `target`:
    {"key": the letting's key if it is in the pool, "date": its bid date
    (default: the tables' date D, else today), "owner", "work_type"}."""
    if not COMPARABLES_BLEND:
        return None
    pools = getattr(tab, "comparables", None)
    if callable(pools):
        pools = pools()
    pool = pools.get(market) if pools else None
    if pool is None:
        return None
    tgt = target or {}
    D = tgt.get("date") or getattr(tab, "D", None) \
        or _dt.date.today().isoformat()
    keys, kidx, lines = [], {}, []
    for it, t in zip(items, trace):
        q = _f(it.get("quantity"), 0.0) or 0.0
        k = it.get("key")
        if k is None:
            k = (it.get("family") or "", it.get("unit") or "",
                 it.get("size_bucket") or "",
                 qty_shape.band_of(q) if q > 0 else "",
                 it.get("description") or "")
        k = tuple(k)
        ki = kidx.get(k)
        if ki is None:
            ki = kidx[k] = len(keys)
            keys.append(k)
        lines.append({"ki": ki, "q": q, "p": t.get("price"),
                      "fx": t.get("fixed_amount") or 0.0})
    rec = {"key": tgt.get("key"), "date": D, "year": to_year or "",
           "n_bidders": n_bidders, "f": d["f"], "lines": lines}
    t1 = getattr(tab, "t1", None) if (TXDOT_C4D1 and market == "txdot") \
        else None
    r = pool.price(rec, keys, tab, tgt.get("owner"), tgt.get("work_type") or "",
                   t1.f_candidate(tab, D) if t1 is not None else None)
    r["date"] = D
    return r


# ---------------------------------------------------------------------------
def _letting_totals(keep_items=False):
    """Real lettings with an identified winner: (market, n, winner_total,
    median_total[, schedule]).  The back-test population.

    With keep_items, each letting also carries the winner's own schedule --
    family, unit, size_bucket, quantity and the bid year -- so the full
    estimator path can be run against it.
    """
    csv.field_size_limit(10 ** 8)
    rows = collections.defaultdict(list)
    sched = collections.defaultdict(dict)
    # every bidder's unit price per item, to recognise owner-set money, and
    # the winner's own amount on each item, to take it at face value
    prices = collections.defaultdict(lambda: collections.defaultdict(list))
    win_amt = collections.defaultdict(dict)
    years = {}
    p = os.path.join(CLEAN, "master_panel.csv")
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("source_set") != "external":
                continue
            if (r.get("is_engineer_estimate") or "").lower() in (
                    "true", "1", "yes"):
                continue
            if r.get("competition_class") != "competitive":
                continue
            if r.get("project_kind") != "construction":
                continue
            if r.get("price_basis") == "uneven_proposal":
                continue
            if r.get("is_low_bidder") not in ("True", "False"):
                continue
            q, u = _f(r.get("quantity")), _f(r.get("unit_price"))
            if not q or not u or q <= 0 or u <= 0:
                continue
            mk = ("municipal" if r.get("source") in MUNICIPAL_SOURCES
                  else "txdot")
            key = (mk, r.get("project_key"))
            rows[key].append(
                (r.get("bidder_norm") or r.get("bidder"), q * u,
                 r.get("is_low_bidder") == "True"))
            if keep_items:
                ik = (r.get("item_key") or "", r.get("item_family") or "",
                      r.get("unit") or "", r.get("size_bucket") or "",
                      (r.get("desc_norm") or "").strip())
                prices[key][ik].append(u)
            if keep_items and r.get("is_low_bidder") == "True":
                # The winner's schedule IS the letting's schedule; one row per
                # item key so a repeated item number does not double the
                # quantity.
                sched[key][ik] = sched[key].get(ik, 0.0) + q
                win_amt[key][ik] = win_amt[key].get(ik, 0.0) + q * u
                years[key] = str(r.get("bid_date") or "")[:4]
    out = []
    for (mk, pk), rs in rows.items():
        bt = collections.defaultdict(float)
        win = set()
        for b, v, w in rs:
            bt[b] += v
            if w:
                win.add(b)
        if len(win) != 1 or len(bt) < 2:
            continue
        w = next(iter(win))
        if bt.get(w, 0.0) <= 0:
            continue
        med = statistics.median(bt.values())
        if med <= 0:
            continue
        if keep_items:
            items = []
            for ik, q in sched[(mk, pk)].items():
                _, f_, u_, sb, dn = ik
                it = {"family": f_, "unit": u_, "size_bucket": sb,
                      "quantity": q, "description": dn}
                # owner-set money is known before the bids open, so passing
                # its face value in is what a user's schedule would do too
                if is_owner_fixed(prices[(mk, pk)].get(ik, ()), f_, u_, dn):
                    it["amount"] = win_amt[(mk, pk)].get(ik, 0.0)
                items.append(it)
            out.append((mk, len(bt), bt[w], med, items,
                        years.get((mk, pk), "")))
        else:
            out.append((mk, len(bt), bt[w], med))
    return out


def calibrate_full_path(write=True, to_year=None, limit=None):
    """The honest calibration: run the WHOLE estimator against real lettings.

    Each letting's schedule is rebuilt from the winner's own line items, every
    item is priced out of price_reference exactly as a user's schedule would
    be, the total is discounted by f(n), and the result is compared with the
    winner's actual total.  This is what the estimator's error really is; the
    discount-only figure is what it would be if the price level were known.

    Escalation is deliberately run to each letting's OWN year, not to today:
    the question is whether the estimator reproduces that letting's price, and
    escalating it to 2026 would be scoring it against a market it never saw.
    """
    tab = Tables()
    tab.calib = {}                      # no interval needed while calibrating
    lets = _letting_totals(keep_items=True)
    if limit:
        lets = lets[:limit]
    per = collections.defaultdict(list)
    cov = collections.Counter()
    for mk, n, wt, med, items, yr in lets:
        if not items:
            continue
        if len(items) < MIN_SCHEDULE_ITEMS:
            cov["under_%d_items" % MIN_SCHEDULE_ITEMS] += 1
            continue
        r = estimate(tab, items, n, mk, to_year or yr or None)
        if not r.get("expected_low"):
            cov["no_estimate"] += 1
            continue
        # A schedule we could only half price cannot be scored against a full
        # total -- that would read as the estimator being cheap.
        if r["priced"] < 0.9 * max(r["n_items"], 1):
            cov["under_90pct_priced"] += 1
            continue
        cov["scored"] += 1
        per[mk].append(r["expected_low"] / wt)
        per["all"].append(r["expected_low"] / wt)
    rows = []
    for mk, v in sorted(per.items()):
        if len(v) < 30:
            continue
        v.sort()
        q = lambda p: v[int(p * (len(v) - 1))]
        err = sorted(abs(x - 1) for x in v)
        rows.append({"market": mk, "n": len(v),
                     "median_ratio": round(statistics.median(v), 6),
                     "median_abs_error": round(statistics.median(err), 6),
                     "p10": round(q(.10), 6), "p25": round(q(.25), 6),
                     "p75": round(q(.75), 6), "p90": round(q(.90), 6),
                     "discount_table": ("municipal" if mk == "municipal"
                                        and tab.disc_muni else "pooled"),
                     "mode": "full_path",
                     # _letting_totals admits source_set == 'external' only,
                     # so the company's eight tabulations are not in this
                     # calibration -- they are what it will be scored against.
                     "train_scope": "public_external_only"})
    if write and rows:
        with open(os.path.join(CLEAN, "estimator_calibration.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    return rows, cov


def calibrate_out_of_fold(write=True, per_letting=None):
    """The interval, measured with every letting held out of its own price.

    calibrate_full_path() still reads price_reference.csv, which was built from
    all the lettings including the one being scored, so its quantiles are
    optimistic -- measured: the stated 50% band actually holds 45.5% of
    held-out lettings and the 80% band 76.9%, and median absolute error runs
    32.4% in-sample against 34.6% leave-one-out.  A band that claims 50% and
    delivers 45% is the kind of number a bid gets lost on.

    So the three reference tables are rebuilt here at their stage-1 grain
    from exactly build_master's pool, the letting under test is left out of
    every cell, and the letting is priced by estimate() ITSELF on those
    held-out tables (HeldOutTables).  It used to be priced by a re-written
    three-tier rule that differed from item_market_price in four places --
    token hits counted per letting instead of per cell, no unsized-band
    fallback, a 2-letting floor and no size substitution or shape on the
    family tier, and NO ESCALATION -- plus a held-out discount instead of the
    table and a slightly different row pool (q > 0 only, unit-less rows
    kept).  Measured on the 216 public municipal lettings (reports/
    segment_and_company_test.md, section 3): the missing escalation alone
    moved the centring factor from 1.06 to 1.10, so the published constants
    put production's own P50 about 4% high and its P80 under the low bid on
    14% of held-out lettings, not 20%.  The index and the discount table are
    fitted on all lettings; one letting barely moves either, and escalating
    on the TxDOT arm, which no municipal letting helped fit, keeps most of
    the escalation's gain.
    """
    tab = Tables()
    pool = _reference_pool()
    cells = _reference_cells(pool)
    cov = collections.Counter()
    cov["reference_rebuild_mismatch"] = _check_reference_cells(tab, cells)
    if cov["reference_rebuild_mismatch"]:
        print("WARNING: %d published reference rows are not reproduced by "
              "the held-out builder -- build_master and _reference_cells "
              "have drifted, and this calibration no longer measures the "
              "tables estimate() reads" % cov["reference_rebuild_mismatch"])

    # The scored population is unchanged: q > 0 rows, one identified
    # winner, at least two bidders.  The winner's schedule keeps each row's
    # own quantity, because item_market_price bands on the line's quantity.
    lets = collections.defaultdict(
        lambda: {"bt": collections.defaultdict(float), "win": set(),
                 "sched": {}, "mk": "", "rows": collections.defaultdict(list),
                 "prices": collections.defaultdict(list), "wamt": {},
                 "yr": collections.Counter()})
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in pool:
        if q is None or q <= 0:
            continue
        d = lets[pk]
        d["mk"] = mk
        d["bt"][b] += q * u
        k = (fam, un, sz, qty_shape.band_of(q), dn)
        d["prices"][k].append(u)
        if w:
            d["win"].add(b)
            d["sched"][k] = d["sched"].get(k, 0.0) + q
            d["rows"][k].append(q)
            d["wamt"][k] = d["wamt"].get(k, 0.0) + q * u
            d["yr"][yr] += 1
    del pool

    trips = collections.defaultdict(list)
    for pk, d in lets.items():
        if len(d["win"]) != 1 or len(d["bt"]) < 2 or not d["sched"]:
            continue
        wt = d["bt"].get(next(iter(d["win"])), 0.0)
        if wt <= 0 or statistics.median(d["bt"].values()) <= 0:
            continue
        mk, n, sched = d["mk"], len(d["bt"]), d["sched"]
        if len(sched) < MIN_SCHEDULE_ITEMS:
            cov["under_%d_items" % MIN_SCHEDULE_ITEMS] += 1
            continue
        items, keys = [], []
        for k in sched:
            fam, un, sz, bd, dn = k
            if is_owner_fixed(d["prices"].get(k, ()), fam, un, dn):
                # known before the bids open: face value, as a user's
                # schedule passes it
                items.append({"family": fam, "unit": un, "size_bucket": sz,
                              "quantity": sched[k], "description": dn,
                              "amount": d["wamt"].get(k, 0.0)})
                keys.append(k)
                cov["owner_fixed_items"] += 1
                continue
            for qr in d["rows"][k]:
                items.append({"family": fam, "unit": un, "size_bucket": sz,
                              "quantity": qr, "description": dn})
                keys.append(k)
        yr = d["yr"].most_common(1)[0][0] if d["yr"] else ""
        # THE production path, on tables that never saw this letting:
        # same tiers, same fallbacks, same escalation to the letting's own
        # year, same discount table
        r = estimate(HeldOutTables(tab, cells, pk), items, n, mk, yr or None)
        if r.get("expected_low") is None:
            cov["no_estimate"] += 1
            continue
        ok = collections.defaultdict(lambda: True)
        for k, t in zip(keys, r["trace"]):
            if not (t.get("fixed_amount") or t.get("price")):
                ok[k] = False
        pr = sum(1 for k in sched if ok[k])
        tot, fixed = r["market_total"], r["fixed_total"]
        if pr < 0.9 * len(sched) or tot <= 0:
            cov["under_90pct_priced"] += 1
            continue
        fa = r["discount"]["f"]
        cov["scored"] += 1
        if fixed:
            cov["lettings_with_owner_fixed"] += 1
        trips[mk].append((tot * fa, fixed, wt))
        trips["all"].append((tot * fa, fixed, wt))
        # optional per-letting record for analysis (the user's target is
        # scored per letting); pass a list to receive it
        if per_letting is not None:
            per_letting.append({"project_key": pk, "market": mk,
                                "n_bidders": n, "bid_part": tot * fa,
                                "owner_fixed": fixed, "actual_low": wt,
                                "n_items": len(sched)})

    rows_out = []
    for mk, trip in sorted(trips.items()):
        if len(trip) < 30:
            continue
        raw = sorted((c + fx) / w for c, fx, w in trip)
        med = statistics.median(raw)
        # One measured parameter that moves the CENTRE and nothing else.  Out
        # of fold the uncorrected path runs low on municipal work and about
        # right on TxDOT, and a bid estimate that is systematically low wins
        # jobs at a loss -- the mirror of the +2.3% problem the discount shift
        # fixes.  Fitted on held-out lettings, so it is not the estimator
        # marking its own homework; it does NOT make the estimate more
        # accurate.  It is solved on the bid part only (_centre), so owner-set
        # money stays at face value.
        lc = _centre(trip)
        cen = sorted((c * lc + fx) / w for c, fx, w in trip)
        q = lambda pp: cen[int(pp * (len(cen) - 1))]
        rows_out.append({"market": mk, "n": len(trip),
                         "median_ratio": round(med, 6),
                         # error of the UNcentred path, comparable with the
                         # figures published before centring existed
                         "median_abs_error": round(statistics.median(
                             [abs(x - 1) for x in raw]), 6),
                         # error of what estimate() actually prints
                         "median_abs_error_centred": round(statistics.median(
                             [abs(x - 1) for x in cen]), 6),
                         "over_share_centred": round(
                             sum(1 for x in cen if x >= 1.0) / len(cen), 6),
                         "p10": round(q(.10), 6), "p20": round(q(.20), 6),
                         "p25": round(q(.25), 6), "p75": round(q(.75), 6),
                         "p80": round(q(.80), 6), "p90": round(q(.90), 6),
                         # estimate() inverts p10..p90 directly when they are
                         # quantiles of the centred ratio
                         "quantiles_basis": "centred",
                         "level_correction": round(lc, 6),
                         "lettings_with_owner_fixed": sum(
                             1 for _, fx, _ in trip if fx),
                         "min_schedule_items": MIN_SCHEDULE_ITEMS,
                         "discount_table": "production_table",
                         "mode": "out_of_fold_production_path",
                         "train_scope": "public_external_only"})
    if write and rows_out:
        with open(os.path.join(CLEAN, "estimator_calibration.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows_out[0]))
            wr.writeheader()
            wr.writerows(rows_out)
    return rows_out, cov


_UNCLASSIFIED = ("other", "", "trm", "needs_review")


def _year(v):
    y = _f(v)
    return y if y is not None and 1990 <= y <= 2100 else None


def _reference_pool(sizes=None):
    """build_master's `comp`, read back from the panel: the rows every
    published reference table is built from.  Quantity is NOT filtered (the
    family table keeps rows without one) and a row without a unit is dropped,
    as pandas drops a NaN group key.  `sizes` (a list): each kept row's
    size_in is appended to it, aligned with the rows (F1)."""
    csv.field_size_limit(10 ** 8)
    seen = {}
    intern = lambda x: seen.setdefault(x, x)  # noqa: E731
    rows = []
    with open(os.path.join(CLEAN, "master_panel.csv"), encoding="utf-8-sig",
              newline="") as f:
        for r in csv.DictReader(f):
            if r.get("source_set") != "external":
                continue
            if (r.get("is_engineer_estimate") or "").lower() in (
                    "true", "1", "yes"):
                continue
            if r.get("competition_class") != "competitive":
                continue
            if r.get("project_kind") != "construction":
                continue
            if r.get("price_basis") == "uneven_proposal":
                continue
            u = _f(r.get("unit_price"))
            if not u or u <= 0:
                continue
            rows.append((
                "municipal" if r.get("source") in MUNICIPAL_SOURCES
                else "txdot", intern(r.get("project_key")),
                intern(r.get("item_family") or ""), intern(r.get("unit") or ""),
                intern(r.get("size_bucket") or ""), _f(r.get("quantity")), u,
                r.get("is_low_bidder") == "True",
                intern(r.get("bidder_norm") or r.get("bidder")),
                intern((r.get("desc_norm") or "").strip()),
                intern(r.get("owner") or ""), intern(str(r.get("year") or ""))))
            if sizes is not None:
                sizes.append(_f(r.get("size_in")))
    return rows


def _reference_cells(rows, sizes=None, avail=None):
    """The three published tables at their stage-1 grain -- per cell, per
    letting: (median price, median quantity, year, owner), grouped by (cell,
    letting, year) as build_master groups -- so any one letting can be left
    out of every cell."""
    s1 = {"fam": {}, "band": {}, "tok": {}}
    tokc = {}
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in rows:
        if not un:
            continue
        bd = qty_shape.band_of(q) if q is not None else ""
        t = tokc.get(dn)
        if t is None:
            t = tokc[dn] = item_tokens.tokens(dn)
        fk = family_key(fam, dn)
        for name, key, ok in (("fam", (fk, un, sz), True),
                              ("band", (fk, un, sz, bd), bool(bd)),
                              ("tok", (t, un, bd), bool(t and bd))):
            if not ok:
                continue
            g = s1[name].get((key, pk, yr))
            if g is None:
                g = s1[name][(key, pk, yr)] = ([], [], ow)
            g[0].append(u)
            if q is not None:
                g[1].append(q)
    cells = {}
    for name, st in s1.items():
        out = cells[name] = {}
        for (key, pk, yr), (us, qs, ow) in st.items():
            out.setdefault(key, {}).setdefault(pk, []).append(
                (statistics.median(us),
                 statistics.median(qs) if qs else None, _year(yr), ow))
    inv = collections.defaultdict(lambda: collections.defaultdict(list))
    for k, by in cells["tok"].items():
        if len(by) >= 2:             # fewer can never reach the table
            for w in k[0]:
                inv[k[1]][w].append(k)
    cells["tok_inv"] = inv
    # F1's size cells, from the same rows (None without sizes)
    cells["size"] = (SizeCells(rows, sizes, avail or {})
                     if sizes is not None else None)
    return cells


def _held_out_row(by, drop, family=None, tok=None):
    """One table row, as build_master would write it without `drop`."""
    es = [e for pk, v in by.items() if pk != drop for e in v]
    if not es:
        return None
    n = len(by) - (1 if drop in by else 0)
    yw = [(e[2], e[1] * e[0]) for e in es
          if e[2] is not None and e[1] is not None and e[1] * e[0] > 0]
    vint = None
    if yw:
        # dollar-weighted (src/vintage.py), exact when the years agree
        y0 = min(y for y, _ in yw)
        vint = y0 + (math.fsum((y - y0) * w for y, w in yw)
                     / math.fsum(w for _, w in yw))
    qs = [e[1] for e in es if e[1] is not None]
    return {"n_projects": n, "n_owners": len({e[3] for e in es}),
            "price_median": statistics.median([e[0] for e in es]),
            "qty_median": statistics.median(qs) if qs else None,
            "vintage_year": vint, "tok": tok or "",
            "usable": ("unclassified" if _base_family(family) in _UNCLASSIFIED
                       else "yes" if n >= 5 else "thin" if n >= 3 else "no")}


class _HeldOutMap:
    """A reference table without one letting: .get() answers as the
    published table would (a cell under `minn` lettings is absent), [k]
    returns the row whatever its count (item_market_price indexes token
    cells directly and checks n_projects itself)."""

    def __init__(self, cells, drop, minn, kind, full):
        self._c, self._d, self._m, self._kind = cells, drop, minn, kind
        self._full = full               # rows with nothing held out, shared
        self._memo = {}

    def __getitem__(self, k):
        by = self._c.get(k)
        if by is None:
            raise KeyError(k)
        memo = self._full if self._d not in by else self._memo
        r = memo.get(k)
        if r is None and k not in memo:
            r = memo[k] = _held_out_row(
                by, self._d,
                family=None if self._kind == "tok" else k[0],
                tok=" ".join(sorted(k[0])) if self._kind == "tok" else None)
        if r is None:
            raise KeyError(k)
        return r

    def get(self, k, default=None):
        try:
            r = self[k]
        except KeyError:
            return default
        return r if r["n_projects"] >= self._m else default

    def items(self):
        rows = []
        for k in self._c:
            r = self.get(k)
            if r is not None:
                rows.append((k, r))
        # the published CSV's order (usable, then n_projects descending),
        # with the key as a stable last resort
        rows.sort(key=lambda kv: (kv[1]["usable"], -kv[1]["n_projects"],
                                  tuple(map(str, kv[0]))))
        return rows

    def __bool__(self):
        return bool(self._c)


class _MuniMap(_HeldOutMap):
    """MC on the published tables (Tables.attach_v2f1f2): a municipal-only
    table over every letting, rows marked as municipal."""

    def __getitem__(self, k):
        r = _HeldOutMap.__getitem__(self, k)
        r["cell_market"] = "municipal"
        return r


class HeldOutTables:
    """Tables, with one letting left out of every reference cell.  estimate()
    and item_market_price() run on it UNCHANGED, so the calibration prices
    each item by exactly the rule production uses -- tiers, fallbacks,
    shape, escalation and the discount table included."""

    def __init__(self, tab, cells, drop):
        self.missing, self.notes = [], []
        for a in ("shape", "index", "disc", "disc_muni", "borrow_sigma",
                  "index_scope"):
            setattr(self, a, getattr(tab, a, None))
        self.calib = {}                 # no interval while calibrating
        full = cells.setdefault("_full", {"tok": {}, "band": {}, "fam": {}})
        self.ref_tok = _HeldOutMap(cells["tok"], drop, 2, "tok", full["tok"])
        self.ref_band = _HeldOutMap(cells["band"], drop, 2, "band",
                                    full["band"])
        self.ref = _HeldOutMap(cells["fam"], drop, 1, "fam", full["fam"])
        self.tok_inv = cells["tok_inv"]


def _check_reference_cells(tab, cells):
    """Nothing held out, the rebuilt cells must BE the published tables;
    if build_master's grouping drifts from this copy, say so."""
    view = HeldOutTables(tab, cells, None)
    bad = 0
    for pub, mine in ((tab.ref_tok, view.ref_tok),
                      (tab.ref_band, view.ref_band), (tab.ref, view.ref)):
        for k, r in pub.items():
            m = mine.get(k)
            if m is None or int(_f(r.get("n_projects"), 0)) != m["n_projects"] \
                    or abs(_f(r.get("price_median")) / m["price_median"]
                           - 1) > 1e-9:
                bad += 1
    return bad


# ---------------------------------------------------------------------------
# FORWARD CALIBRATION AND THE NOT-UNDER GUARD
#
# calibrate_out_of_fold() prices each letting on tables that hold every OTHER
# letting -- including the ones bid AFTER it -- with an index and a discount
# fitted on all years.  A new letting is never priced that way: everything it
# is priced from is older than it.  Replayed honestly forward (reference
# cells, all three index arms and the discount table refitted on lettings
# dated strictly before each letting, constants fitted on the earlier
# forward-scored lettings only), the 125 public municipal lettings of
# 2024-26 came out (stage-2 remedies, pre-registered, fwd/remedies/):
#
#   constants                              P80 under low   P80/low in [1,1.13]
#   fixed 20th percentile, expanding            22.4%             13.6%
#   + not-under guard                           19.2%              8.8%
#   + guard + flat carry (_carry_to)            19.2%              4.8%
#   (TxDOT, 632 lettings: 21.8% / 12.0% -> 18.8% / 12.0% with both)
#
# The fixed 20th percentile let 22.4% of new lettings come in over the
# not-under figure; holding the floor costs in-band share, and no fixed level
# did it more cheaply (the 17th percentile was the first to hold, at 6.4%).
# The carry costs the guarded rule 4 points of in-band share here: it moved
# five January 2025 municipal lettings from the _default arm to the
# municipal arm (4-13% lower), one P80 from 1.07 to 0.93 of its low bid, and
# that miss lowered the guard's level for the rest of 2025.  Leaving any one
# other letting out of the guard's updates does not close the gap (4.0-8.8%
# against 7.2-9.6%).  It stays because without it every letting priced to
# 2027 gets NO escalation until the index has a 2027 row; carrying only
# where no arm has the year is the narrower fix, not yet tested.

NEVER = "9999-12-31"
FORWARD_MIN_PRIOR = 40       # constants need this many earlier lettings
FORWARD_MIN_PAIRS = 10       # index_by_market.MIN_PAIRS_YOY; build_master's
                             # price_time_index uses 10 as well
NOT_UNDER_FLOOR = 0.20       # the user's floor: P80 under the low bid on <= 20% of lettings
# What the guard AIMS at, below the floor.  An adaptive rule aimed exactly at
# 20% sits on the line and is over it about half the time: on the final Mac
# data (2026-11-19 20:19 rebuild) it gave municipal 2024-26 21.0% under (2026:
# 25.0%) after 19.1% on the data an hour earlier.  Aiming at 16% leaves a
# margin of about one year's sampling error (n ~ 60, se ~ 5 points).  Chosen
# AFTER seeing that run -- the first level at which every year of both
# markets stayed at or under 20% -- so it is registered for a check on the
# 2027 lettings (reports/forward_floor_integration.md).
GUARD_TARGET = 0.16
GUARD_GAMMA = {"municipal": 0.02, "txdot": 0.004}   # ~1 / scored lettings
GUARD_ALPHA_MIN = 0.02                               # per year


def _avail_date(dates, precs):
    """One date per letting key: when it could first have been seen.

    The LATEST bid_date it carries (a key is complete only once all its parts
    are bid, and the later date can only withhold data, never leak it); an
    'approx' YYYY-01-01 is a year-only placeholder and counts as mid-year,
    YYYY-07-01; no date -> NEVER (never a reference, never scored)."""
    ds = sorted(d for d in dates if d and len(d) == 10)
    if not ds:
        return NEVER
    d = ds[-1]
    if "approx" in precs and "precise" not in precs and d[5:] == "01-01":
        return d[:4] + "-07-01"
    return d


def _letting_dates():
    """_avail_date of every external key, one streamed pass."""
    csv.field_size_limit(10 ** 8)
    dates = collections.defaultdict(set)
    precs = collections.defaultdict(set)
    with open(os.path.join(CLEAN, "master_panel.csv"), encoding="utf-8-sig",
              newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_s, i_k, i_d, i_p = (h.index(k) for k in (
            "source_set", "project_key", "bid_date", "date_precision"))
        for row in rd:
            if row[i_s] != "external":
                continue
            dates[row[i_k]].add(row[i_d])
            precs[row[i_k]].add(row[i_p])
    return {pk: _avail_date(dates[pk], precs[pk]) for pk in dates}


def _chain(s1f, adjacent):
    """Stage 2 of index_by_market.chained_index (adjacent=True) and of
    build_master's price_time_index (False), on stage-1 project medians."""
    if not len(s1f):
        return {}
    yr = (s1f.groupby(["item_key", "year"])
          .agg(price=("p", "median"), n=("project_key", "nunique"))
          .reset_index())
    if yr.empty:
        return {}
    piv = yr.pivot(index="item_key", columns="year", values="price")
    years = sorted(c for c in piv.columns if str(c).isdigit())
    out, lvl = {}, 100.0
    for a, b in zip(years, years[1:]):
        if adjacent and int(b) != int(a) + 1:
            continue
        pair = piv[[a, b]].dropna()
        pair = pair[(pair[a] > 0) & (pair[b] > 0)]
        if len(pair) < FORWARD_MIN_PAIRS:
            out[str(b)] = lvl
            continue
        lvl *= float((pair[b] / pair[a]).median())
        out[str(b)] = lvl
    return out


class _ForwardIndex:
    """The three escalation arms Tables loads, refitted on the lettings
    dated strictly before D: Municipal and TxDOT as index_by_market.py
    builds them, _default as build_master.py builds price_time_index.csv.
    Stage 1 is a per-(item, year, project) median and a project is wholly
    before D or not, so it is computed once and cut per date.  Checked
    against the published files on all data and against chained_index() on
    a truncated panel (fwd/build/s2_index_disc.py)."""

    ARMS = ("Municipal", "TxDOT", "_default")

    def __init__(self, avail):
        import numpy as np
        import pandas as pd
        self.np = np
        use = ["source", "source_set", "project_key", "year", "item_key",
               "unit_price", "competition_class", "project_kind",
               "date_precision", "is_engineer_estimate", "price_basis"]
        m = pd.read_csv(os.path.join(CLEAN, "master_panel.csv"),
                        encoding="utf-8-sig", low_memory=False,
                        usecols=lambda c: c in use,
                        dtype={"year": str, "item_key": str,
                               "project_key": str})
        ee = m.is_engineer_estimate.fillna(False).astype(bool)
        precise = m.date_precision == "precise"
        cols = ["item_key", "year", "project_key", "unit_price"]
        # index_by_market.main()'s filters
        yr = m.year.fillna("").astype(str).str.replace(r"\.0$", "",
                                                         regex=True)
        im = (yr.str.match(r"^20\d\d$", na=False) & (m.unit_price > 0)
              & ~ee & (m.project_kind == "construction")
              & (m.price_basis == "all_bids"))
        is_tx = m.source.isin(["txdot", "txdot_archive"])
        muni = m.loc[im & ~is_tx & precise & (m.source_set == "external"),
                     cols].assign(year=yr)
        tx = m.loc[im & is_tx & precise, cols].assign(year=yr)
        # build_master's price_time_index base
        bm = (~ee & (m.unit_price > 0) & (m.source_set == "external")
              & (m.competition_class == "competitive")
              & (m.project_kind == "construction")
              & (m.price_basis != "uneven_proposal"))
        pkeys = set(m.loc[bm & precise, "project_key"].unique())
        base = m.loc[bm & m.project_key.isin(pkeys)
                     & m.year.str.match(r"^20\d\d$", na=False)
                     & (m.item_key.astype(str) != ""), cols]
        del m, ee, precise, yr, im, is_tx, bm
        self.S1, self.SD = {}, {}
        for arm, df in (("Municipal", muni), ("TxDOT", tx),
                        ("_default", base)):
            s1 = (df.groupby(["item_key", "year", "project_key"])
                  .agg(p=("unit_price", "median")).reset_index())
            s1["D"] = s1.project_key.map(avail).fillna(NEVER)
            s1.sort_values("D", inplace=True, kind="mergesort")
            s1.reset_index(drop=True, inplace=True)
            self.S1[arm], self.SD[arm] = s1, s1.D.values
        self._sig, self._res = None, None

    def at(self, D):
        """{arm: {to_year: level}} from projects dated strictly before D."""
        sig = tuple(int(self.np.searchsorted(self.SD[a], D, side="left"))
                    for a in self.ARMS)
        if sig != self._sig:
            res = {}
            for arm, k in zip(self.ARMS, sig):
                o = _chain(self.S1[arm].iloc[:k], adjacent=(arm != "_default"))
                if o:
                    res[arm] = o
            self._sig, self._res = sig, res
        return self._res


def _parse_totals(s):
    out = []
    for part in str(s).split(";"):
        if ":" not in part:
            continue
        try:
            f = float(part.split(":", 1)[1])
        except ValueError:
            continue
        if f > 0:
            out.append(f)
    return out


class _ForwardDiscount:
    """competition_and_crosswalk.py's two discount tables, refitted on the
    lettings dated strictly before D: the pooled low/median by bidder count
    (bins with >= 10 lettings) and the municipal table = pooled x one shift
    (median municipal ratio over the nearest pooled bin within 2; n = 1
    forced to 1.0; only once >= 20 municipal and >= 20 TxDOT lettings exist).
    Checked against both published CSVs on all data."""

    def __init__(self, avail):
        import numpy as np
        import pandas as pd
        self.np = np
        lr = []
        a = pd.read_csv(os.path.join(CLEAN, "txdot_archive_project_totals.csv"),
                        encoding="utf-8-sig")
        for r in a.itertuples(index=False):
            b = _parse_totals(r.bidder_totals)
            if len(b) < 1:
                continue
            lr.append(("archive", str(r.bid_date)[:10], len(b), min(b),
                       float(np.median(b))))
        c = pd.read_csv(os.path.join(CLEAN, "txdot_letting_spread.csv"),
                        encoding="utf-8-sig")
        for r in c.itertuples(index=False):
            lr.append(("current", str(r.bid_date)[:10], int(r.n_bidders),
                       float(r.low), float(r.median)))
        mm = pd.read_csv(os.path.join(CLEAN, "municipal_letting_totals.csv"),
                         encoding="utf-8-sig",
                         dtype={"year": str, "source_set": str})
        mm = mm[mm.source_set == "external"]
        mm = mm[(mm.low > 0) & (mm["median"] > 0) & (mm.n_bidders >= 2)]
        for r in mm.itertuples(index=False):
            d = avail.get(r.letting_id)
            if d is None:
                d = (str(r.bid_date)[:10] if isinstance(r.bid_date, str)
                     and len(str(r.bid_date)) >= 10 else NEVER)
            lr.append(("municipal", d, int(r.n_bidders), float(r.low),
                       float(getattr(r, "median"))))
        ld = pd.DataFrame(lr, columns=["src", "D", "n_bidders", "low",
                                       "median"])
        ld["D"] = ld.D.where(ld.D.str.match(r"^\d{4}-\d\d-\d\d$"), NEVER)
        ld["lom"] = ld.low / ld["median"]
        self.LD = ld.sort_values("D", kind="mergesort").reset_index(drop=True)
        self.LDD = self.LD.D.values
        self._k, self._res = None, None

    def at(self, D):
        """(pooled, municipal) tables from lettings dated strictly before D."""
        k = int(self.np.searchsorted(self.LDD, D, side="left"))
        if k != self._k:
            self._k, self._res = k, self._fit(self.LD.iloc[:k])
        return self._res

    def _fit(self, d):
        np = self.np
        pooled = {}
        for n, g in d.groupby("n_bidders"):
            if len(g) < 10:
                continue
            v = g.lom.values
            pooled[int(n)] = {"f": float(np.median(v)),
                              "p25": float(np.quantile(v, .25)),
                              "p75": float(np.quantile(v, .75)),
                              "lettings": int(len(g))}
        mu = d[d.src == "municipal"]
        t = d[d.src != "municipal"]
        muni = {}
        if len(mu) >= 20 and len(t) >= 20 and pooled:
            def fp(n):
                kk = min(pooled, key=lambda x: abs(x - n))
                return pooled[kk]["f"] if abs(kk - n) <= 2 else None
            rel = [r.lom / fp(r.n_bidders) for r in mu.itertuples(index=False)
                   if fp(r.n_bidders)]
            shift = float(np.median(rel)) if rel else 1.0
            for n, row in pooled.items():
                if n <= 1:
                    muni[n] = {"f": 1.0, "p25": 1.0, "p75": 1.0,
                               "lettings": row["lettings"]}
                else:
                    muni[n] = {"f": row["f"] * shift,
                               "p25": row["p25"] * shift,
                               "p75": row["p75"] * shift,
                               "lettings": row["lettings"]}
        return pooled, muni


_FWD_HEAVY = frozenset(("price_median", "qty_median", "vintage_year",
                        "n_owners"))
_FWD_EMPTY = {"n_projects": 0, "n_owners": 0, "price_median": None,
              "qty_median": None, "vintage_year": None, "tok": "",
              "usable": "no"}


class _LazyRow(dict):
    """A reference row whose cheap fields exist at once and whose medians are
    computed on first access: the family-table scan (size substitution) reads
    only n_projects/usable of every row."""
    __slots__ = ("_fill",)

    def __init__(self, cheap, fill):
        super().__init__(cheap)
        self._fill = fill

    def _ensure(self):
        f = self._fill
        if f is not None:
            self._fill = None
            dict.update(self, f())

    def get(self, k, default=None):
        if k in _FWD_HEAVY:
            self._ensure()
        return dict.get(self, k, default)

    def __getitem__(self, k):
        if k in _FWD_HEAVY:
            self._ensure()
        return dict.__getitem__(self, k)


def _fwd_heavy(by, pks):
    """_held_out_row's medians, owners and vintage over the lettings pks."""
    es = [e for pk in pks for e in by[pk]]
    yw = [(e[2], e[1] * e[0]) for e in es
          if e[2] is not None and e[1] is not None and e[1] * e[0] > 0]
    vint = None
    if yw:
        y0 = min(y for y, _ in yw)
        vint = y0 + (math.fsum((y - y0) * w for y, w in yw)
                     / math.fsum(w for _, w in yw))
    qs = [e[1] for e in es if e[1] is not None]
    return {"n_owners": len({e[3] for e in es}),
            "price_median": statistics.median([e[0] for e in es]),
            "qty_median": statistics.median(qs) if qs else None,
            "vintage_year": vint}


class _FwdKind:
    """One reference table over all cutoffs: per cell the lettings sorted by
    availability date, so the cell at D is a prefix (the last prefix per
    cell is memoised; cutoffs are visited in date order)."""

    def __init__(self, cells_kind, kind, minn, avail, market=None):
        self.c, self.kind, self.minn, self.av = cells_kind, kind, minn, avail
        self.order, self.last = {}, {}
        self.market = market        # MC: "municipal" marks its rows

    def row(self, k, D):
        by = self.c.get(k)
        if by is None:
            raise KeyError(k)
        o = self.order.get(k)
        if o is None:
            lst = sorted(by, key=lambda pk: (self.av.get(pk, NEVER), pk))
            o = self.order[k] = ([self.av.get(pk, NEVER) for pk in lst], lst)
        L = bisect.bisect_left(o[0], D)
        hit = self.last.get(k)
        if hit is not None and hit[0] == L:
            return hit[1]
        if L == 0:
            r = None
        else:
            fam = None if self.kind == "tok" else _base_family(k[0])
            tok = " ".join(sorted(k[0])) if self.kind == "tok" else None
            cheap = {"n_projects": L, "tok": tok or "",
                     "usable": ("unclassified" if fam in _UNCLASSIFIED
                                else "yes" if L >= 5 else "thin" if L >= 3
                                else "no")}
            if self.market:
                cheap["cell_market"] = self.market
            r = _LazyRow(cheap, (lambda by=by, o=o, L=L:
                                 _fwd_heavy(by, o[1][:L])))
        self.last[k] = (L, r)
        return r


class _FwdMap:
    """One _FwdKind at one cutoff, with _HeldOutMap's interface."""

    def __init__(self, fk, D):
        self.fk, self.D = fk, D
        self._items = None

    def __getitem__(self, k):
        r = self.fk.row(k, self.D)
        return _FWD_EMPTY if r is None else r

    def get(self, k, default=None):
        try:
            r = self.fk.row(k, self.D)
        except KeyError:
            return default
        if r is None or r["n_projects"] < self.fk.minn:
            return default
        return r

    def items(self):
        if self._items is None:
            rows = []
            for k in self.fk.c:
                r = self.get(k)
                if r is not None:
                    rows.append((k, r))
            rows.sort(key=lambda kv: (kv[1]["usable"], -kv[1]["n_projects"],
                                      tuple(map(str, kv[0]))))
            self._items = rows
        return self._items

    def __bool__(self):
        return bool(self.fk.c)


def _forward_kinds(cells, avail):
    return {"tok": _FwdKind(cells["tok"], "tok", 2, avail),
            "band": _FwdKind(cells["band"], "band", 2, avail),
            "fam": _FwdKind(cells["fam"], "fam", 1, avail)}


class ForwardTables:
    """Tables as they stood on date D: reference cells holding only lettings
    available strictly before D (the letting under test, dated D, is out of
    its own cells, and so is everything after it), the escalation index and
    the discount refitted the same way.  The quantity-shape table stays the
    published one (it moves no 2022-26 municipal dollar).  estimate() runs
    on it unchanged."""

    def __init__(self, tab, kinds, tok_inv, D, index, disc_pooled,
                 disc_muni, size_cells=None, comparables=None, t1=None,
                 muni_kinds=None):
        self.missing, self.notes = [], []
        self.shape = getattr(tab, "shape", None) or {}
        self.borrow_sigma = getattr(tab, "borrow_sigma", None) or {}
        self.index_scope = getattr(tab, "index_scope", None)
        self.index = {k: dict(v) for k, v in index.items()}
        self.disc = dict(disc_pooled)
        self.disc_muni = dict(disc_muni)
        self.calib = {}
        self.ref_tok = _FwdMap(kinds["tok"], D)
        self.ref_band = _FwdMap(kinds["band"], D)
        self.ref = _FwdMap(kinds["fam"], D)
        self.tok_inv = tok_inv
        self.D = D
        self.size_cells = size_cells      # F1 (SizeCells), read at D
        self.comparables = comparables    # V2: {market: comparables.Pool}
        self.t1 = t1                      # T1 (T1State), fed by the pass
        # MC: the municipal-only cells at D (muni_forward_kinds)
        self.ref_tok_m = self.ref_band_m = self.ref_m = None
        if muni_kinds is not None:
            self.ref_tok_m = _FwdMap(muni_kinds["tok"], D)
            self.ref_band_m = _FwdMap(muni_kinds["band"], D)
            self.ref_m = _FwdMap(muni_kinds["fam"], D)


def _guard_step(alpha, errs, gamma, target=GUARD_TARGET,
                lo=GUARD_ALPHA_MIN, hi=NOT_UNDER_FLOOR):
    """One update of the not-under guard after a letting date closes: down
    by gamma x (1 - target) per letting the P80 came in under, up by
    gamma x target per letting it did not, kept in [lo, hi].

    It AIMS at `target` (16%) but may relax back up to the plain 20th
    percentile (`hi`), never beyond: the not-under figure is never narrower
    than the plain P80.  Capping at the target as well cost 6.8 points of
    in-band share on the 2026-11-19 Mac data (municipal 2024-26 8.0% against
    14.8%) for under-shares of 16.5% against 17.0%, both inside the floor."""
    s = 0.0
    for e in errs:
        s += target - (1.0 if e else 0.0)
    return min(hi, max(lo, alpha + gamma * s))


def _not_under_guard(trip, gamma, target=GUARD_TARGET):
    """The quantile level the not-under figure divides by, chosen online.

    Adaptive conformal inference (Gibbs & Candes 2021), one-sided: walking
    the forward-scored lettings in date order, each date's lettings get
    P80 = P50 / (the alpha-quantile of the centred ratios of every earlier
    letting), centring fitted on those same earlier lettings; after the date
    closes, alpha moves down for each P80 that came in under the low bid and
    back up for each that did not (_guard_step).  Capped at the 20th
    percentile, so the guard only ever WIDENS the fixed rule, and only after
    misses.  Uncapped (two-sided), the low-miss year 2023 narrowed it and
    2024 then missed 25.5%.  gamma ~ 1 / lettings scored per year, so a
    year running one point over the floor moves alpha about one point.
    `trip`: (date, discounted bid part, owner-set money, actual low) sorted
    by date.  Returns the level after the last letting, and what it did."""
    alpha = target
    n = under = 0
    i = 0
    while i < len(trip):
        D = trip[i][0]
        j = i
        while j < len(trip) and trip[j][0] == D:
            j += 1
        if i >= FORWARD_MIN_PRIOR:
            prior = [(c, fx, w) for _, c, fx, w in trip[:i]]
            lc = _centre(prior)
            rs = sorted((c * lc + fx) / w for c, fx, w in prior)
            p = rs[int(alpha * (len(rs) - 1))]
            errs = [(c * lc + fx) / w / p < 1.0 for _, c, fx, w in trip[i:j]]
            n += len(errs)
            under += sum(errs)
            alpha = _guard_step(alpha, errs, gamma, target)
        i = j
    return {"alpha": alpha, "n_scored": n,
            "under_share": (under / n) if n else None}


def calibrate_forward(write=True, per_letting=None, fitted=None):
    """The interval, measured as a NEW letting meets it: every letting priced
    by estimate() itself on ForwardTables at its own date (reference cells,
    index and discount from lettings dated strictly before it), centring and
    quantiles fitted on all of those forward-scored lettings, and the
    not-under figure's quantile chosen by the guard.  The population and the
    filters are calibrate_out_of_fold's, line for line.  This is what main()
    writes to estimator_calibration.csv.

    V2+F1+F2: the line prices carry F1 and F2, the bid part of every letting
    is the comparables blend (estimate(), from the lettings before it), and
    the centring, quantiles and guard are fitted on those blended records.
    per_letting also gets each letting's forward P50/P80 (forward_scores).
    fitted (a dict): receives F1's size cells, the comparables pools, T1's
    state (its mobilization ratio) and MC's municipal-only cells, for the
    leak guards.

    MC: municipal lines read the municipal-only cells (muni_forward_kinds)
    where they have >= MUNI_CELLS_NMIN earlier municipal lettings.

    Coverage scope (COVERAGE_SCOPE): estimate() leaves the uncovered lines
    unpriced; the record's actual_low is the low bid minus the low bidder's
    own amounts on them (actual_low_full keeps the whole low bid,
    actual_low_med deducts each line's median bidder amount instead -- the
    sensitivity version); >= 90% of the COVERED lines must be priced; a
    not_covered letting is not scored and the constants never see it.  T1's
    D1 records keep the whole low / median bid (the competition discount is
    the bids' order statistic, not a scope)."""
    tab = Tables()
    avail = _letting_dates()
    findex = _ForwardIndex(avail)
    fdisc = _ForwardDiscount(avail)
    sizes = [] if SIZE_FALLBACK else None
    pool = _reference_pool(sizes)
    cells = _reference_cells(pool, sizes, avail)
    lsz = _line_sizes(pool, sizes) if sizes is not None else {}
    # P_L (round 13): the market levels from the training rows; built for
    # the leak guards whenever `fitted` is asked for, priced from only when
    # MUNI_TXDOT_LEVEL is on
    ml13 = (MarketLevel(cells, {r[1]: r[0] for r in pool}, avail)
            if (MUNI_TXDOT_LEVEL or fitted is not None) else None)
    pools = comparables_pools(pool, avail)
    t1 = T1State(pool, avail) if TXDOT_C4D1 else None
    kinds_m, cells_m = muni_forward_kinds(pool, avail)      # MC
    lch = LowConfShare(pool, avail) if LOWCONF else None   # low confidence (display)
    if fitted is not None:
        if cells_m is None:
            # the leak guards check MC's cells even while MUNI_CELLS is off
            # (nothing is priced from them then)
            cells_m = _reference_cells([r for r in pool if r[0] == "municipal"])
        fitted.update({"size_cells": cells["size"], "comparables": pools,
                       "t1": t1, "muni_cells": cells_m, "market_level": ml13})
    cov = collections.Counter()
    cov["reference_rebuild_mismatch"] = _check_reference_cells(tab, cells)
    kinds = _forward_kinds(cells, avail)
    lets = collections.defaultdict(
        lambda: {"bt": collections.defaultdict(float), "win": set(),
                 "sched": {}, "mk": "", "rows": collections.defaultdict(list),
                 "prices": collections.defaultdict(list), "wamt": {},
                 "yr": collections.Counter(), "bamt": {},
                 "nb": collections.defaultdict(set)})
    for mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr in pool:
        if q is None or q <= 0:
            continue
        d = lets[pk]
        d["mk"] = mk
        d["bt"][b] += q * u
        k = (fam, un, sz, qty_shape.band_of(q), dn)
        d["prices"][k].append(u)
        if IDENTICAL_BID_OWNER_SET:
            d["nb"][k].add(b)                  # D: distinct bidders per key
        if COVERAGE_SCOPE and line_coverage(dn, fam, un):
            ba = d["bamt"].setdefault(k, collections.defaultdict(float))
            ba[b] += q * u                 # every bidder's amount (sensitivity)
        if w:
            d["win"].add(b)
            d["sched"][k] = d["sched"].get(k, 0.0) + q
            d["rows"][k].append(q)
            d["wamt"][k] = d["wamt"].get(k, 0.0) + q * u
            d["yr"][yr] += 1
    del pool
    todo = []
    for pk, d in lets.items():
        if len(d["win"]) != 1 or len(d["bt"]) < 2 or not d["sched"]:
            continue
        wt = d["bt"].get(next(iter(d["win"])), 0.0)
        if wt <= 0 or statistics.median(d["bt"].values()) <= 0:
            continue
        if len(d["sched"]) < MIN_SCHEDULE_ITEMS:
            cov["under_%d_items" % MIN_SCHEDULE_ITEMS] += 1
            continue
        todo.append(pk)
    todo.sort(key=lambda pk: (avail.get(pk, NEVER), pk))
    trips = collections.defaultdict(list)
    unc_mk, nc_mk = collections.Counter(), collections.Counter()  # coverage
    ft_d, ft = None, None
    for pk in todo:
        D = avail.get(pk, NEVER)
        if D == NEVER:
            cov["no_date"] += 1
            continue
        d = lets[pk]
        mk, n, sched = d["mk"], len(d["bt"]), d["sched"]
        wt = d["bt"][next(iter(d["win"]))]
        items, keys = [], []
        for k in sched:
            fam, un, sz, bd, dn = k
            fx_k, by_d = schedule_owner_set(d, k)
            if fx_k:
                items.append({"family": fam, "unit": un, "size_bucket": sz,
                              "quantity": sched[k], "description": dn,
                              "amount": d["wamt"].get(k, 0.0), "key": k})
                if by_d:
                    items[-1]["identical_bids"] = True
                keys.append(k)
                continue
            for qr in d["rows"][k]:
                items.append({"family": fam, "unit": un, "size_bucket": sz,
                              "quantity": qr, "description": dn, "key": k,
                              "size_in": lsz.get((pk,) + k)})
                keys.append(k)
        yr = d["yr"].most_common(1)[0][0] if d["yr"] else ""
        if D != ft_d:
            pooled, muni = fdisc.at(D)
            ft_d, ft = D, ForwardTables(tab, kinds, cells["tok_inv"], D,
                                        findex.at(D), pooled, muni,
                                        cells["size"], pools, t1,
                                        muni_kinds=kinds_m)
            ft.lowconf = lch
            ft.market_level = ml13 if MUNI_TXDOT_LEVEL else None   # P_L
        r = estimate(ft, items, n, mk, yr or None, target={"key": pk})
        if r.get("not_covered"):
            cov["not_covered"] += 1
            nc_mk[mk] += 1
            continue
        if r.get("expected_low") is None:
            cov["no_estimate"] += 1
            continue
        if t1 is not None and mk == "txdot":
            t1.note_priced(pk, r["market_total"])
        ok = collections.defaultdict(lambda: True)
        unc = set()
        for k, t in zip(keys, r["trace"]):
            if t.get("uncovered"):
                unc.add(k)
                continue
            if not (t.get("fixed_amount") or t.get("price")):
                ok[k] = False
        ck = [k for k in sched if k not in unc]      # the covered lines
        pr = sum(1 for k in ck if ok[k])
        tot, fixed = r["market_total"], r["fixed_total"]
        if pr < 0.9 * len(ck) or tot <= 0:
            cov["under_90pct_priced"] += 1
            continue
        cv = covered_actual(d, unc, wt)
        if cv is None:
            cov["no_covered_low"] += 1
            continue
        fa = r["discount"]["f"]
        bp = r["bid_part"]                 # V2: the blend (= tot * fa without it)
        cov["scored"] += 1
        if unc:
            cov["scored_with_uncovered"] += 1
            unc_mk[mk] += 1
        if t1 is not None and mk == "txdot":
            t1.note_scored(D, n, tot, wt, statistics.median(d["bt"].values()))
        trips[mk].append((D, bp, fixed, cv["actual_low"]))
        trips["all"].append((D, bp, fixed, cv["actual_low"]))
        if per_letting is not None:
            cm = r.get("comparables") or {}
            rec = {"project_key": pk, "market": mk, "date": D,
                   "n_bidders": n, "bid_part": bp,
                   "owner_fixed": fixed, "actual_low": cv["actual_low"],
                   "n_items": len(sched),
                   "bid_part_lines": tot * fa,
                   "cmp": cm.get("cmp"),
                   "n_comps": cm.get("n_comps"),
                   "comps": cm.get("comps")}
            if LOWCONF:
                # report-only (the "mobilization set aside" view): our
                # market-level amounts and the low bidder's own amounts on
                # the covered, non-owner-set low-confidence lines
                lcl = (r.get("low_confidence") or {}).get("lines") or []
                fk = {k for k, it in zip(keys, items) if it.get("amount")}
                ml = gl = 0.0
                for k in sched:
                    if k in unc or k in fk:
                        continue
                    c = lowconf_class(k[0], k[1], k[4])
                    if c == "mobilization":
                        ml += d["wamt"].get(k, 0.0)
                    elif c:
                        gl += d["wamt"].get(k, 0.0)
                rec.update({
                    "market_total": tot,
                    "mob_market": sum(g["ours_market"] for g in lcl
                                      if g["class"] == "mobilization"),
                    "gc_ls_market": sum(g["ours_market"] for g in lcl
                                        if g["class"] != "mobilization"),
                    "mob_low": ml, "gc_ls_low": gl})
            if COVERAGE_SCOPE:
                rec.update(cv)
            per_letting.append(rec)

    rows_out = []
    for mk, trip in sorted(trips.items()):
        if len(trip) < 30:
            continue
        trip.sort(key=lambda t: t[0])           # stable: (date, key) order
        plain = [(c, fx, w) for _, c, fx, w in trip]
        raw = sorted((c + fx) / w for c, fx, w in plain)
        lc = _centre(plain)
        cen = sorted((c * lc + fx) / w for c, fx, w in plain)
        q = lambda pp: cen[int(pp * (len(cen) - 1))]
        g = (_not_under_guard(trip, GUARD_GAMMA[mk]) if mk in GUARD_GAMMA
             else None)
        lvl = g["alpha"] if g else GUARD_TARGET
        rows_out.append({"market": mk, "n": len(trip),
                         "median_ratio": round(statistics.median(raw), 6),
                         "median_abs_error": round(statistics.median(
                             [abs(x - 1) for x in raw]), 6),
                         "median_abs_error_centred": round(statistics.median(
                             [abs(x - 1) for x in cen]), 6),
                         "over_share_centred": round(
                             sum(1 for x in cen if x >= 1.0) / len(cen), 6),
                         "p10": round(q(.10), 6), "p20": round(q(.20), 6),
                         "p25": round(q(.25), 6), "p30": round(q(.30), 6),
                         "p75": round(q(.75), 6),
                         "p80": round(q(.80), 6), "p90": round(q(.90), 6),
                         "quantiles_basis": "centred",
                         "level_correction": round(lc, 6),
                         # the not-under figure divides by this
                         "p_not_under": round(q(lvl), 6),
                         "not_under_level": round(lvl, 6),
                         "not_under_rule": "guard" if g else "fixed_p20",
                         "guard_gamma": GUARD_GAMMA.get(mk, ""),
                         "guard_scored": g["n_scored"] if g else "",
                         "guard_under_share": (round(g["under_share"], 6)
                                               if g and g["under_share"]
                                               is not None else ""),
                         "first_date": trip[0][0], "last_date": trip[-1][0],
                         "lettings_with_owner_fixed": sum(
                             1 for _, _, fx, _ in trip if fx),
                         "min_schedule_items": MIN_SCHEDULE_ITEMS,
                         "discount_table": "forward_refit",
                         "mode": "forward_production_path",
                         "train_scope": "public_external_only"})
        if COVERAGE_SCOPE:
            # the actual is the covered low bid (COVERAGE_SCOPE); how many
            # lettings it moved and how many it dropped as not covered
            rows_out[-1].update({
                "scope": "covered_lines",
                "lettings_with_uncovered": (sum(unc_mk.values())
                                            if mk == "all" else unc_mk[mk]),
                "lettings_not_covered": (sum(nc_mk.values())
                                         if mk == "all" else nc_mk[mk])})
    b_pl = None
    if P80_FROM_B:
        # B80: production B's own forward pass gives the P80 divisor and the
        # guard's level (per date for the records; its final ones for the
        # file); the centring stays this pass's
        b_pl = []
        with production_b():
            b_rows, _ = calibrate_forward(write=False, per_letting=b_pl)
        bmk = {r["market"]: r for r in b_rows}
        for row in rows_out:
            br = bmk.get(row["market"])
            if br is None:
                continue
            for f_ in ("p_not_under", "not_under_level", "not_under_rule",
                       "guard_gamma", "guard_scored", "guard_under_share"):
                row[f_] = br[f_]
            row["p80_rule"] = "p80_from_b"
    if per_letting is not None:
        forward_scores(per_letting, b_records=b_pl)
    if write and t1 is not None:
        t1.write(os.path.join(CLEAN, T1_FILE))
    if write and rows_out:
        with open(os.path.join(CLEAN, "estimator_calibration.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows_out[0]))
            wr.writeheader()
            wr.writerows(rows_out)
    return rows_out, cov


def covered_actual(d, unc, low):
    """The covered actual of one letting (`d` as calibrate_forward builds
    it, `unc` its uncovered schedule keys, `low` its low bid): low minus the
    low bidder's own amounts on those keys; the sensitivity version deducts
    each key's median amount over the bidders who priced it.  With
    COVERAGE_SCOPE off (or nothing uncovered) the actual is the low bid.
    None when nothing positive is left."""
    if not COVERAGE_SCOPE:
        return {"actual_low": low}
    ded = sum(d["wamt"].get(k, 0.0) for k in unc)
    med = 0.0
    for k in unc:
        v = [a for a in d.get("bamt", {}).get(k, {}).values() if a > 0]
        med += statistics.median(v) if v else d["wamt"].get(k, 0.0)
    if low - ded <= 0:
        return None
    return {"actual_low": low - ded, "actual_low_full": low,
            "uncovered_low": ded, "uncovered_med": med,
            "actual_low_med": (low - med) if low - med > 0 else None,
            "n_uncovered_lines": len(unc)}


def comparables_pools(pool, avail, markets=("municipal", "txdot")):
    """V2's comparables pool per market, from a price pool (the rows that may
    train: the external rows here, training rows in holdout_eval)."""
    if not COMPARABLES_BLEND:
        return None
    me = sys.modules[__name__]
    return {mk: comparables.Pool(pool, avail, mk, CLEAN, me) for mk in markets}


def forward_scores(per_letting, guard=True, b_records=None):
    """Each forward-scored letting's P50 and P80 as ratios to its low bid,
    with the constants a NEW letting dated D gets: centring and the P80
    divisor fitted on the same market's lettings dated before D (at least
    FORWARD_MIN_PRIOR of them), the divisor at the guard's level as it stood
    when D opened.  The replay every forward score of the project uses
    (handoff/scratchpad/fwd/mac_recheck/score_new_data.py replay(), same
    order).  Adds p50, p80, alpha, centring, divisor and n_prior to each
    record that has constants; returns {market: [records with them]}.
    b_records (B80): production B's records -- the divisor and the guard's
    level at D are B's (GuardPath), the centring stays these records'."""
    out = {}
    bpath = None
    if b_records is not None:
        bpath = {mk: GuardPath([r for r in b_records if r["market"] == mk],
                               GUARD_GAMMA.get(mk, GUARD_GAMMA["municipal"]),
                               guard)
                 for mk in {r["market"] for r in b_records}}
    for market in sorted({r["market"] for r in per_letting}):
        by = {r["project_key"]: r for r in per_letting if r["market"] == market}
        trip = sorted((r["date"], r["bid_part"], r["owner_fixed"],
                       r["actual_low"], r["project_key"]) for r in by.values())
        gamma = GUARD_GAMMA.get(market, GUARD_GAMMA["municipal"])
        alpha, i, got = GUARD_TARGET, 0, []
        while i < len(trip):
            D = trip[i][0]
            j = i
            while j < len(trip) and trip[j][0] == D:
                j += 1
            if i >= FORWARD_MIN_PRIOR:
                prior = [(c, fx, w) for _, c, fx, w, _ in trip[:i]]
                lc = _centre(prior)
                rs = sorted((c * lc + fx) / w for c, fx, w in prior)
                a = alpha if guard else GUARD_TARGET
                p = rs[int(a * (len(rs) - 1))]
                bc = None
                if bpath is not None:
                    bp_ = bpath.get(market)
                    bc = bp_.at(D) if bp_ is not None else None
                    if bc is None:          # B80: B has no constants at D
                        i = j
                        continue
                    a, p = bc[0], bc[1]
                errs = []
                for d, c, fx, w, k in trip[i:j]:
                    p50 = (c * lc + fx) / w
                    by[k].update({"p50": p50, "p80": p50 / p, "alpha": a,
                                  "centring": lc, "divisor": p, "n_prior": i})
                    if bc is not None:
                        by[k]["p80_from_b"] = True
                    got.append(by[k])
                    errs.append(p50 / p < 1.0)
                if guard:
                    alpha = _guard_step(alpha, errs, gamma, GUARD_TARGET)
            i = j
        out[market] = got
    return out


def calibrate(write=True):
    """Measure the realised est/actual distribution and write the interval
    table the estimator inverts.

    The price level is held EXACT here -- the letting's own median bidder total
    stands in for the market median -- so what is being calibrated is the
    discount's own spread, which is the part that does not average out over a
    schedule.  An estimate built from price_reference instead would fold in
    family-level reference error and make the interval market-specific in a way
    none of this data supports.
    """
    tab = Tables()
    lets = _letting_totals()
    per = collections.defaultdict(list)
    for mk, n, wt, med in lets:
        d = discount(tab, n, mk)
        if not d:
            continue
        per[mk].append((med * d["f"]) / wt)
        per["all"].append((med * d["f"]) / wt)
    rows = []
    for mk, v in sorted(per.items()):
        if len(v) < 30:
            continue
        v.sort()
        q = lambda p: v[int(p * (len(v) - 1))]
        err = sorted(abs(x - 1) for x in v)
        rows.append({"market": mk, "n": len(v),
                     "median_ratio": round(statistics.median(v), 6),
                     "median_abs_error": round(statistics.median(err), 6),
                     "p10": round(q(.10), 6), "p25": round(q(.25), 6),
                     "p75": round(q(.75), 6), "p90": round(q(.90), 6),
                     "discount_table": ("municipal" if mk == "municipal"
                                        and tab.disc_muni else "pooled")})
    if write and rows:
        with open(os.path.join(CLEAN, "estimator_calibration.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    return rows


def _selftest():
    ok = True

    def chk(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("FAIL %-44s got %r want %r" % (name, got, want))
        else:
            print("pass %-44s %r" % (name, got))

    t = Tables.__new__(Tables)
    t.missing, t.notes = [], []
    t.ref = {("gabion_wall", "CY", ""): {
        "price_median": "400", "qty_median": "245", "vintage_year": "2025",
        "usable": "yes", "n_projects": "9", "n_owners": "4"}}
    t.shape = {("gabion_wall", "CY", "200-1000"): {"shape_multiplier": "1.0"},
               ("gabion_wall", "CY", "1000-5000"): {"shape_multiplier": "0.8"}}
    t.index = {"Municipal": {"2025": 140.3, "2026": 130.0},
               "TxDOT": {"2025": 159.8, "2026": 139.8}, "_default": {}}
    t.disc = {4: {"f": 0.872, "p25": 0.796, "p75": 0.926, "lettings": 488}}
    t.disc_muni = {4: {"f": 0.826, "p25": 0.773, "p75": 0.896, "lettings": 32}}
    t.borrow_sigma = {1: 0.2638}
    t.calib = {"municipal": {"market": "municipal", "n": "155", "p10": "0.85",
                             "p25": "0.928", "p75": "1.053", "p90": "1.15"}}
    t.ref_band = {}
    t.ref_tok = {}
    t.tok_inv = collections.defaultdict(
        lambda: collections.defaultdict(list))

    # 1 the municipal market must draw the municipal table
    chk("1 municipal uses municipal table",
        discount(t, 4, "municipal")["table"], "municipal")
    chk("1 txdot uses pooled table",
        discount(t, 4, "txdot")["table"], "pooled")

    # 2 a bidder count far from any bin is refused, not extrapolated
    with pricing_rule(clamp_high_bidders=False):
        d2 = discount(t, 12, "municipal")
    chk("2 refuses distant bidder count (clamp off)", d2, None)
    chk("2 allows 2 bins out", discount(t, 6, "municipal")["bin"], 4)

    # 3 escalation is market-specific, and says when it is borrowed
    e = escalation(t, "2025", "2026", "municipal")
    chk("3 municipal escalation", round(e["factor"], 4),
        round(130.0 / 140.3, 4))
    chk("3 not borrowed", e["borrowed"], False)
    e2 = escalation(t, "2025", "2026", "txdot")
    chk("3 txdot escalation differs", round(e2["factor"], 4),
        round(139.8 / 159.8, 4))
    # 3b if another script renames the market arm, the miss must be REPORTED,
    #    not absorbed by silently borrowing the statewide index
    t3 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t3, k, v)
    t3.index = {"Statewide": {"2025": 159.8, "2026": 139.8},
                "_default": {"2025": 159.8, "2026": 139.8}}
    chk("3b missing municipal arm detected",
        market_index_key(t3, "municipal"), None)
    e3 = escalation(t3, "2025", "2026", "municipal")
    chk("3b borrow is flagged", e3["borrowed"], True)
    chk("3b and says the arm is absent", e3["market_arm_found"], False)
    r3b = estimate(t3, [{"family": "gabion_wall", "unit": "CY",
                         "quantity": 245}], 4, "municipal", "2026")
    chk("3b the trace says BORROWED out loud",
        any("BORROWED" in w for w in r3b["trace"][0]["why"]), True)
    # a renamed arm must not change which DISCOUNT table is used
    chk("3b discount table unaffected by index naming",
        r3b["discount"]["table"], "municipal")

    # 3b0 the token tier runs BEFORE the band tier, matches on words, and
    #     refuses a dimension mismatch however many trade words are shared
    t0 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t0, k, v)
    tok18 = frozenset(item_tokens.tokens("18 in rcp storm sewer pipe").
                      __iter__())
    tok24 = frozenset(item_tokens.tokens("24 in rcp storm sewer pipe").
                      __iter__())
    t0.ref_tok = {
        (tok18, "LF", "200-1000"): {
            "price_median": "120", "n_projects": "6", "usable": "yes",
            "n_owners": "3", "vintage_year": "2025"}}
    t0.tok_inv = collections.defaultdict(lambda: collections.defaultdict(list))
    for w in tok18:
        t0.tok_inv["LF"][w].append((tok18, "LF", "200-1000"))
    # same words, same dimension -> priced off the token cell
    r_a = item_market_price(t0, "storm_pipe", "LF", 500, "", "2026",
                            "municipal", "storm sewer pipe rcp 18 inch")
    chk("3b0 token tier prices a reworded match",
        r_a["price_key"], "token_jaccard")
    chk("3b0 and records the similarity",
        r_a["token_match_jaccard"] is not None, True)
    # 24-inch must NOT borrow the 18-inch price
    r_b = item_market_price(t0, "storm_pipe", "LF", 500, "", "2026",
                            "municipal", "storm sewer pipe rcp 24 inch")
    chk("3b0 a dimension mismatch is refused, not priced",
        r_b["price_key"] != "token_jaccard", True)
    # and a cell with one letting behind it is not a reference
    t0.ref_tok[(tok18, "LF", "200-1000")]["n_projects"] = "1"
    r_c = item_market_price(t0, "storm_pipe", "LF", 500, "", "2026",
                            "municipal", "storm sewer pipe rcp 18 inch")
    chk("3b0 one letting is not a token reference",
        r_c["price_key"] != "token_jaccard", True)

    # 3c the band-keyed reference is tried FIRST, and when it answers, the
    #    band shape multiplier must NOT also be applied -- the band is already
    #    in the key, so scaling on top moves the price out of the band it was
    #    measured in.
    t4 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t4, k, v)
    t4.ref_band = {("gabion_wall", "CY", "", "200-1000"): {
        "price_median": "390", "qty_median": "245", "vintage_year": "2025",
        "usable": "yes", "n_projects": "6", "n_owners": "3"}}
    r3c = item_market_price(t4, "gabion_wall", "CY", 245, "", "2026",
                            "municipal")
    chk("3c band tier used when available", r3c["price_key"],
        "family+unit+size+band")
    chk("3c no shape multiplier on top", "qty_factor" in r3c, False)
    chk("3c band price, escalated once", round(r3c["price"], 4),
        round(390 * (130.0 / 140.3), 4))
    #    a band with too little behind it must fall back, not be used
    t4.ref_band = {("gabion_wall", "CY", "", "200-1000"): {
        "price_median": "390", "n_projects": "1", "usable": "no"}}
    r3d = item_market_price(t4, "gabion_wall", "CY", 245, "", "2026",
                            "municipal")
    chk("3d one letting is not a reference", r3d["price_key"],
        "family+unit+size")

    # 3e a lone bidder gets no discount, and is not extrapolated into the
    #    2-bidder bin
    d1 = discount(t, 1, "municipal")
    chk("3e single bid is not discounted", (d1["f"], d1["extrapolated"]),
        (1.0, False))

    # 3f fractional vintage_year must be honoured, not truncated.  2025.8
    #    against an index falling 140.3 -> 130.0 is only 0.2 of a year of
    #    decline away from 2026, not a whole one.
    e_frac = escalation(t, "2025.8", "2026", "municipal")
    e_floor = escalation(t, "2025", "2026", "municipal")
    chk("3f fraction gives less escalation than the floor",
        round(e_frac["factor"], 6) > round(e_floor["factor"], 6), True)
    chk("3f and it interpolates geometrically",
        round(e_frac["factor"], 6),
        round(130.0 / (140.3 * (130.0 / 140.3) ** 0.8), 6))

    # 3g an unusable reference row must raise a DOLLAR-WEIGHTED warning, not
    #    sit silently in the trace
    t5 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t5, k, v)
    t5.ref = {("gabion_wall", "CY", ""): {
        "price_median": "400", "qty_median": "245", "vintage_year": "2025",
        "usable": "thin", "n_projects": "3", "n_owners": "2"}}
    t5.ref_band = {}
    r5 = estimate(t5, [{"family": "gabion_wall", "unit": "CY",
                        "quantity": 245}], 4, "municipal", "2026")
    chk("3g unusable row warns, with the dollar share",
        any("not usable='yes'" in w for w in r5["warnings"]), True)
    chk("3g and the share is reported", r5["degraded_dollar_share"], 1.0)

    # 3h nothing priceable is a refusal, never a bid of 0.00
    r6 = estimate(t, [{"family": "unobtainium", "unit": "EA", "quantity": 1}],
                  4, "municipal", "2026")
    chk("3h nothing priced -> no estimate", r6["expected_low"], None)
    chk("3h and says so", any("NOT A SINGLE item" in w
                              for w in r6["warnings"]), True)

    # 3i the market argument: case and whitespace are normalised to the right
    #    table (helpful), while a value that is not a market RAISES rather than
    #    quietly falling through to the pooled discount (dangerous).
    r8 = estimate(t, [{"family": "gabion_wall", "unit": "CY",
                       "quantity": 245}], 4, "  Municipal ", "2026")
    chk("3i 'Municipal' normalises to the municipal table",
        r8["discount"]["table"], "municipal")
    try:
        estimate(t, [{"family": "gabion_wall", "unit": "CY",
                      "quantity": 245}], 4, "city", "2026")
        chk("3i an unknown market raises", "accepted", "raised")
    except ValueError:
        chk("3i an unknown market raises", "raised", "raised")

    # 3j a line item with no unit is a parse failure, not a coverage gap
    r7 = item_market_price(t, "gabion_wall", "", 245, "", "2026", "municipal")
    chk("3j blank unit gets its own reason",
        any("no unit on this line item" in w for w in r7["why"]), True)

    # 4 the discount lands on the TOTAL exactly once
    items = [{"family": "gabion_wall", "unit": "CY", "quantity": 245}]
    r = estimate(t, items, 4, "municipal", "2026")
    base = 400 * (130.0 / 140.3)
    chk("4 market total has no discount in it",
        round(r["market_total"], 4), round(base * 245, 4))
    chk("4 expected low = total x f once",
        round(r["expected_low"], 4), round(base * 245 * 0.826, 4))

    # 5 two items: one discount, not two
    items2 = items + [{"family": "gabion_wall", "unit": "CY", "quantity": 245}]
    r2 = estimate(t, items2, 4, "municipal", "2026")
    chk("5 discount applied once for two items",
        round(r2["expected_low"] / r["expected_low"], 6), 2.0)

    # 6 an unpriceable item must not be silently dropped from a total that is
    #   then presented as the project estimate
    r3 = estimate(t, items + [{"family": "unobtainium", "unit": "EA",
                               "quantity": 1}], 4, "municipal", "2026")
    chk("6 unpriced item counted", (r3["priced"], r3["unpriced"]), (1, 1))
    chk("6 and warned about",
        any("could not be priced" in w for w in r3["warnings"]), True)

    # 7 quantity band shifts the price, in the measured direction
    r4 = estimate(t, [{"family": "gabion_wall", "unit": "CY",
                       "quantity": 2000}], 4, "municipal", "2026")
    chk("7 bigger quantity prices lower per unit",
        round(r4["trace"][0]["qty_factor"], 4), 0.8)
    chk("7 and carries the borrow sigma",
        r4["trace"][0]["qty_sigma"], 0.2638)

    # 8 the interval inverts the calibration: actual = est / ratio
    chk("8 interval from calibration",
        tuple(round(x / r["expected_low"], 4) for x in r["p50_band"]),
        (round(1 / 1.053, 4), round(1 / 0.928, 4)))
    chk("8 band straddles the estimate",
        r["p50_band"][0] < r["expected_low"] < r["p50_band"][1], True)

    # 9 no discount table -> no estimate, and a warning, never a bare total
    t2 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t2, k, v)
    t2.disc, t2.disc_muni = {}, {}
    r5 = estimate(t2, items, 4, "municipal", "2026")
    chk("9 refuses without a discount", r5["expected_low"], None)
    chk("9 and says the total is undiscounted",
        any("NO discount" in w for w in r5["warnings"]), True)

    # 10 owner-set money: recognised narrowly, taken at face value, kept out
    #    of the discount, and never inferred from a quantity
    chk("10 contingency family is allowance-like",
        allowance_like("contingency", "LS", ""), True)
    chk("10 an ALLOW unit is allowance-like",
        allowance_like("other", "ALLOW", "repair/adjustment of sprinkler"), True)
    chk("10 'to allow' in a description is not an allowance",
        allowance_like("needs_review", "EA",
                       "inlet top adjustment including core to allow flex "
                       "base to drain"), False)
    chk("10 an amount is face value", owner_fixed_amount({"amount": "20000"}),
        20000.0)
    chk("10 unit price on an ALLOW line is face value",
        owner_fixed_amount({"family": "other", "unit": "ALLOW",
                            "quantity": 20000, "unit_price": 1}), 20000.0)
    chk("10 a unit price on an ordinary line is ignored",
        owner_fixed_amount({"family": "storm_pipe", "unit": "LF",
                            "quantity": 100, "unit_price": 50}), None)
    chk("10 an ALLOW line with no amount is not given one",
        owner_fixed_amount({"family": "other", "unit": "ALLOW",
                            "quantity": 20000}), None)
    r10 = estimate(t, items + [{"family": "other", "unit": "ALLOW",
                                "quantity": 20000, "unit_price": 1,
                                "description": "sprinkler repair allowance"}],
                   4, "municipal", "2026")
    chk("10 face value added once and undiscounted",
        round(r10["expected_low"] - r["expected_low"], 4), 20000.0)
    chk("10 and the market total does not contain it",
        round(r10["market_total"], 4), round(r["market_total"], 4))
    t6 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t6, k, v)
    t6.ref = dict(t.ref)
    t6.ref[("contingency", "LS", "")] = {
        "price_median": "50000", "qty_median": "1", "vintage_year": "2025",
        "usable": "yes", "n_projects": "9", "n_owners": "4"}
    r11 = estimate(t6, items + [{"family": "contingency", "unit": "LS",
                                 "quantity": 1,
                                 "description": "construction contingency"}],
                   4, "municipal", "2026")
    chk("10 an allowance priced off other projects is flagged",
        any("owner-set allowances" in w for w in r11["warnings"]), True)
    chk("10 bids prove owner-set money when identical",
        is_owner_fixed([50000, 50000, 50000], "contingency", "LS",
                       "construction contingency"), True)
    chk("10 not when the amounts differ (a percentage contingency)",
        is_owner_fixed([219097.5, 320000], "contingency", "LS",
                       "construction contingency"), False)
    chk("10 not on a single bid", is_owner_fixed([50000], "contingency", "LS",
                                                 ""), False)
    chk("10 not on an ordinary line that happens to tie",
        is_owner_fixed([100, 100], "storm_pipe", "LF", "18 in rcp"), False)

    # 11 the centring factor: 1/median without owner-set money, and with it the
    #    median of the centred ratio lands on 1 while the money stays put
    chk("11 centre is 1/median with no owner-set money",
        round(_centre([(0.9, 0.0, 1.0), (1.1, 0.0, 1.0), (1.2, 0.0, 1.0)]), 6),
        round(1 / 1.1, 6))
    trip = [(0.5, 0.4, 1.0), (0.7, 0.0, 1.0), (1.3, 0.1, 1.0)]
    lcx = _centre(trip)
    chk("11 with owner-set money the centred median is 1",
        round(statistics.median([(c * lcx + fx) / w for c, fx, w in trip]), 6),
        1.0)

    # 12 a centred calibration file is inverted directly, not de-centred twice
    t7 = Tables.__new__(Tables)
    for k, v in vars(t).items():
        setattr(t7, k, v)
    t7.calib = {"municipal": {"market": "municipal", "n": "132",
                              "p10": "0.8", "p25": "0.9", "p75": "1.1",
                              "p90": "1.25", "level_correction": "1.08",
                              "quantiles_basis": "centred"}}
    r12 = estimate(t7, items, 4, "municipal", "2026")
    chk("12 centred quantiles invert directly",
        tuple(round(x / r12["expected_low"], 4) for x in r12["p50_band"]),
        (round(1 / 1.1, 4), round(1 / 0.9, 4)))
    #    no p20 in the file -> no not-under figure is invented
    chk("12 no p20, no not-under estimate", "expected_low_p80" in r12, False)
    t7.calib["municipal"]["p20"] = "0.8"
    r13 = estimate(t7, items, 4, "municipal", "2026")
    chk("13 not-under estimate = estimate / p20",
        round(r13["expected_low_p80"] / r13["expected_low"], 6),
        round(1 / 0.8, 6))

    # P70 is independent of the guarded P80 baseline and absent without p30.
    chk("13 no p30, no invented P70", "expected_low_p70" in r13, False)
    t7.calib["municipal"]["p30"] = "0.9"
    r13p70 = estimate(t7, items, 4, "municipal", "2026")
    chk("13 P70 divides by the centred p30",
        round(r13p70["expected_low_p70"] / r13p70["expected_low"], 6),
        round(1 / 0.9, 6))
    chk("13 P70 leaves P50/P80 unchanged",
        (r13p70["expected_low"], r13p70["expected_low_p80"]),
        (r13["expected_low"], r13["expected_low_p80"]))

    # 14 a year past the index's last row is carried flat -- never priced
    #    at its vintage with the escalation silently dropped -- and said so
    e14 = escalation(t, "2025", "2027", "municipal")
    chk("14 a year past the index is carried flat",
        round(e14["factor"], 6), round(130.0 / 140.3, 6))
    chk("14 and the result says from which year", e14["carried_from"], 2026)
    chk("14 a year the index has carries nothing",
        escalation(t, "2025", "2026", "municipal")["carried_from"], None)
    chk("14 the market arm is kept, not borrowed", e14["borrowed"], False)
    r14 = estimate(t, items, 4, "municipal", "2027")
    chk("14 the trace says the level was carried",
        any(w.startswith("index carried flat")
            for w in r14["trace"][0]["why"]), True)
    chk("14 and the estimate warns in dollars",
        any("carried flat" in w for w in r14["warnings"]), True)
    chk("14 same money as the last year's level",
        round(r14["market_total"], 4),
        round(estimate(t, items, 4, "municipal", "2026")["market_total"], 4))

    # 15 the not-under guard: widens after a miss, recovers after a hit,
    #    never narrower than its target quantile, never below 0.02
    T15 = GUARD_TARGET
    a15 = round(T15 - 0.02 * (1 - T15), 6)
    chk("15 a miss lowers the level by gamma x (1 - target)",
        round(_guard_step(T15, [True], 0.02), 6), a15)
    chk("15 a hit raises it by gamma x target",
        round(_guard_step(a15, [False], 0.02), 6), round(a15 + 0.02 * T15, 6))
    chk("15 after hits it relaxes above the target",
        round(_guard_step(T15, [False, False], 0.02), 6),
        round(T15 + 2 * 0.02 * T15, 6))
    chk("15 but never above the plain 20th percentile",
        round(_guard_step(NOT_UNDER_FLOOR, [False, False], 0.02), 6),
        round(NOT_UNDER_FLOOR, 6))
    chk("15 the target sits below the user's 20% floor",
        GUARD_TARGET < NOT_UNDER_FLOOR, True)
    chk("15 never below 0.02", round(_guard_step(0.03, [True] * 5, 0.02), 6),
        0.02)
    #    end to end: 40 calibration lettings, then three misses and a hit
    trip15 = [("2020-%02d-%02d" % (1 + i // 28, 1 + i % 28),
               0.8 + 0.4 * i / 39, 0.0, 1.0) for i in range(40)]
    trip15 += [("2021-01-0%d" % (i + 1), c, 0.0, 1.0)
               for i, c in enumerate((0.5, 0.5, 0.5, 3.0))]
    g15 = _not_under_guard(trip15, 0.02)
    chk("15 three misses and a hit", round(g15["alpha"], 6),
        round(T15 - 3 * 0.02 * (1 - T15) + 0.02 * T15, 6))
    chk("15 scored only once 40 earlier lettings exist",
        (g15["n_scored"], round(g15["under_share"], 6)), (4, 0.75))
    chk("15 a hit after the 40th letting relaxes it by gamma x target",
        round(_not_under_guard(trip15[:40] + [("2021-01-01", 3.0, 0.0, 1.0)],
                               0.02)["alpha"], 6), round(T15 + 0.02 * T15, 6))

    # 16 a forward calibration file: estimate() divides by its p_not_under
    t7.calib["municipal"]["p_not_under"] = "0.7"
    t7.calib["municipal"]["not_under_level"] = "0.17"
    r16 = estimate(t7, items, 4, "municipal", "2026")
    chk("16 not-under estimate = estimate / p_not_under",
        round(r16["expected_low_p80"] / r16["expected_low"], 6),
        round(1 / 0.7, 6))
    chk("16 and the guard's level is reported", r16["not_under_level"], 0.17)

    # 17 forward tables hold only lettings dated strictly before D
    c17 = {"fam": {("gabion_wall", "CY", ""): {
        "A": [(100.0, 50.0, 2024.0, "o1")],
        "B": [(300.0, 50.0, 2025.0, "o2")]}}, "band": {}, "tok": {}}
    k17 = _forward_kinds(c17, {"A": "2024-03-01", "B": "2025-03-01"})
    key17 = ("gabion_wall", "CY", "")

    def at17(D):
        r_ = ForwardTables(t, k17, {}, D, t.index, t.disc,
                           t.disc_muni).ref.get(key17)
        return None if r_ is None else (r_["n_projects"], r_["price_median"])
    chk("17 before every letting: no reference", at17("2024-01-01"), None)
    chk("17 a later letting is not seen", at17("2025-01-01"), (1, 100.0))
    chk("17 nor one dated D itself", at17("2025-03-01"), (1, 100.0))
    chk("17 and once both are earlier, both count", at17("2025-06-01"),
        (2, 200.0))

    # 18 F1's size cells: size_ref and the slope k, forward, with the
    # registered defaults (k = 1 for pipes, 0 otherwise, until >= 5 sizes
    # and >= 20 lettings support a fit)
    rows18, sizes18, av18 = [], [], {}
    for i in range(20):
        pk = "L%02d" % i
        av18[pk] = "%d-%02d-01" % (2020 + i // 12, i % 12 + 1)
        s_ = (6.0, 8.0, 10.0, 12.0, 16.0)[i % 5]
        rows18.append(("municipal", pk, "water_pipe", "LF",
                       "4-12" if s_ <= 12 else "12-24", 100.0, 10.0 * s_, True,
                       "b", "%g in water line" % s_, "o", "2020"))
        sizes18.append(s_)
    si18 = SizeCells(rows18, sizes18, av18)
    chk("18 size_ref = median entry size of the cell", si18.size_ref(
        ("water_pipe", "LF", "4-12")), 9.0)
    chk("18 nothing before the first letting",
        si18.size_ref(("water_pipe", "LF", "4-12"), "2019-01-01"), None)
    chk("18 k before 20 lettings: the pipe default 1",
        si18.k_at("water_pipe", "LF", "2021-01-01")[0], 1.0)
    chk("18 k on 20 lettings, 5 sizes: fitted (price ~ size -> 1)",
        (round(si18.k_at("water_pipe", "LF")[0], 9),
         si18.k_at("water_pipe", "LF")[1]["src"]), (1.0, "fit"))
    chk("18 k default for a non-pipe family is 0",
        si18.k_at("gabion_wall", "CY")[0], 0.0)

    # 19 F1 in item_market_price: a sized line whose size has no band cell
    # no longer takes the UNSIZED cell
    t19 = Tables.__new__(Tables)
    t19.missing, t19.notes = [], []
    t19.ref = {("water_pipe", "LF", ""): {"price_median": "45", "qty_median":
                                          "100", "usable": "yes",
                                          "n_projects": "30"},
               ("water_pipe", "LF", "4-12"): {"price_median": "90", "qty_median":
                                              "100", "usable": "thin",
                                              "n_projects": "4"},
               ("water_pipe", "LF", "12-24"): {"price_median": "160",
                                               "qty_median": "100",
                                               "usable": "thin",
                                               "n_projects": "4"}}
    t19.ref_band, t19.ref_tok, t19.shape, t19.borrow_sigma = {}, {}, {}, {}
    t19.tok_inv = collections.defaultdict(
        lambda: collections.defaultdict(list))
    t19.index, t19.calib, t19.D = {}, {}, None
    t19.size_cells = si18
    p19 = item_market_price(t19, "water_pipe", "LF", 100.0, "36-48", None,
                            "municipal", "48 in water main")
    chk("19 without its size the line keeps the unsized fallback",
        (p19["price"], p19["price_key"]), (45.0, "family+unit+size"))
    p19 = item_market_price(t19, "water_pipe", "LF", 100.0, "36-48", None,
                            "municipal", "48 in water main", 48.0)
    k19 = si18.k_at("water_pipe", "LF")[0]
    chk("19 with it, F1: nearest sized cell x (48 / 16) ** k",
        (round(p19["price"], 6), p19["price_key"], p19["f1"]["ref_key"]),
        (round(160.0 * (48.0 / 16.0) ** k19, 6), "f1_nearest_size",
         ("water_pipe", "LF", "12-24")))
    with pricing_rule(size_fallback=False):
        p19o = item_market_price(t19, "water_pipe", "LF", 100.0, "36-48",
                                 None, "municipal", "48 in water main", 48.0)
    chk("19 switched off, the rule before F1", p19o["price"], 45.0)
    t19.size_cells = SizeCells([], [], {})
    p19n = item_market_price(t19, "water_pipe", "LF", 100.0, "36-48", None,
                             "municipal", "48 in water main", 48.0)
    chk("19 no sized cell at all: unpriced, never the unsized cell",
        (p19n["price"], p19n["f1"].get("no_sized_cell")), (None, True))

    # 20 F2: a removal-only line keys apart, and with no removal cell of its
    # own it gets the schedule's typical line amount
    chk("20 removal-only lines key apart", (
        family_key("water_pipe", "remove existing 8 in water line"),
        family_key("water_pipe", "remove and replace 8 in water line")),
        ("water_pipe#rm", "water_pipe"))
    t20 = Tables.__new__(Tables)
    t20.__dict__.update({k: v for k, v in t19.__dict__.items()})
    t20.size_cells = None
    t20.disc_muni = {4: {"f": 0.826, "p25": 0.8, "p75": 0.9, "lettings": 32}}
    t20.disc = dict(t20.disc_muni)
    items20 = [{"family": "water_pipe", "unit": "LF", "quantity": 100.0,
                "description": "8 in water line"},
               {"family": "water_pipe", "unit": "LF", "quantity": 300.0,
                "description": "12 in water line"},
               {"family": "water_pipe", "unit": "LF", "quantity": 50.0,
                "description": "remove existing water line"}]
    r20 = estimate(t20, items20, 4, "municipal")
    chk("20 typical line amount = median of the other lines' amounts",
        (r20["trace"][2]["price_key"], round(r20["trace"][2]["price"] * 50, 6)),
        ("removal_typical_line", round(statistics.median([4500.0, 13500.0]), 6)))

    # 21 V2: sqrt(CMP x line-priced bid part); no comparable -> the line bid
    P21 = {"market": "municipal", "L": [], "id_list": [], "inv": {},
           "postings": {}}
    pool21 = comparables.Pool.__new__(comparables.Pool)
    pool21.market, pool21.E = "municipal", sys.modules[__name__]
    pool21.ctx, pool21.bg = comparables.Ctx(P21), comparables.BetaGamma([])
    t20.comparables = {"municipal": pool21}
    it21 = [{"family": "water_pipe", "unit": "LF", "quantity": 100.0,
             "description": "8 in water line"}]
    r21 = estimate(t20, it21, 4, "municipal", "2026",
                   {"date": "2026-01-15"})
    chk("21 no comparable: the bid part is the line-priced one",
        (r21["comparables"]["n_comps"], round(r21["bid_part"], 9)),
        (0, round(r21["bid_part_lines"], 9)))
    tk = comparables.tokens("8 in water line")
    L21 = [{"pk": "C1", "date": "2025-01-01", "year": "2025", "n": 4,
            "low": 7000.0, "nonls": 6000.0, "owner": "x", "wtype": "",
            "lines": [{"iid": 0, "unit": "LF", "q": 100.0, "u": 60.0,
                       "rm": False, "ls": False}], "by_iid": {0: [0]}}]
    pool21.ctx = comparables.Ctx({"market": "municipal", "L": L21,
                                  "id_list": [(tk, "LF")],
                                  "inv": {"LF": {w: [0] for w in tk}},
                                  "postings": {0: comparables.np.array([0])}})
    r21 = estimate(t20, it21, 4, "municipal", "2026",
                   {"date": "2026-01-15"})
    chk("21 one earlier comparable: sqrt(CMP x line bid), CMP = its price",
        (r21["comparables"]["comps"], round(r21["comparables"]["cmp"], 9),
         round(r21["bid_part"], 9)),
        (["C1"], 6000.0, round(math.sqrt(6000.0 * r21["bid_part_lines"]), 9)))
    r21b = estimate(t20, it21, 4, "municipal", "2026",
                    {"date": "2024-06-01"})
    chk("21 a comparable dated after the schedule is never used",
        r21b["comparables"]["n_comps"], 0)
    with pricing_rule(comparables=False):
        r21o = estimate(t20, it21, 4, "municipal", "2026",
                        {"date": "2026-01-15"})
    chk("21 switched off: no blend", (r21o.get("comparables"),
                                      r21o["bid_part"] == r21o["bid_part_lines"]),
        (None, True))
    chk("21 the switches are restored", (SIZE_FALLBACK, COMPARABLES_BLEND,
                                         item_tokens.REMOVAL_IDENTITY),
        (True, True, True))

    # 22 T1 (TxDOT): D1 classes; the mobilization ratio, forward; D1 cells
    # with >= D1_MIN records before D; the C4 exact rule; estimate() with
    # T1 on a TxDOT schedule; municipal and the switch untouched
    chk("22 D1 bidder classes", [d1_n_class(n) for n in (2, 3, 4, 5, 6, 7)],
        [0, 1, 1, 2, 2, 3])
    chk("22 D1 size classes", [d1_s_class(x) for x in (
        249999.0, 250000.0, 999999.0, 1e6, 5e6)], [0, 1, 1, 2, 3])
    pool22 = []
    for pk, D_, mob_, base_ in (("T1", "2024-01-10", (5.0, 8.0), (100.0, 100.0)),
                                ("T2", "2024-05-10", (30.0, 30.0), (100.0, 300.0)),
                                ("M1", "2024-02-10", (50.0, 50.0), (100.0, 100.0))):
        mk22 = "municipal" if pk.startswith("M") else "txdot"
        for b, (m_, s_) in enumerate(zip(mob_, base_)):
            pool22.append((mk22, pk, "mobilization", "LS", "", 1.0, m_,
                           b == 0, "b%d" % b, "mobilization", "o", "2024"))
            pool22.append((mk22, pk, "excavation", "CY", "", 10.0, s_ / 10.0,
                           b == 0, "b%d" % b, "excavation", "o", "2024"))
    av22 = {"T1": "2024-01-10", "T2": "2024-05-10", "M1": "2024-02-10"}
    mr22 = MobRatio(pool22, av22, "txdot")
    chk("22 mobilization ratio before T2: T1's bids only, municipal out",
        mr22.at("2024-03-01"), (statistics.median([0.05, 0.08]), 1))
    chk("22 mobilization ratio: same-date letting blind, then both",
        (mr22.at("2024-05-10")[1], mr22.at(None)),
        (1, (statistics.median([0.05, 0.08, 0.3, 0.1]), 2)))
    st22 = T1State([], {})
    for i in range(D1_MIN):
        st22.note_scored("2024-01-%02d" % (i + 1), 4, 300000.0, 0.9 + i / 100,
                         1.0)
    chk("22 D1 cell: < D1_MIN records before D -> None",
        st22.d1(4, 300000.0, "2024-01-10"), None)
    chk("22 D1 cell: D1_MIN records -> their median",
        round(st22.d1(3, 900000.0, "2024-02-01"), 9),
        round(statistics.median([0.9 + i / 100 for i in range(D1_MIN)]), 9))
    chk("22 D1 cell: another class has none", st22.d1(7, 300000.0,
                                                      "2024-02-01"), None)
    q22 = "reflective pavement marker yellow"
    t22 = Tables.__new__(Tables)
    t22.__dict__.update({k: v for k, v in t19.__dict__.items()})
    t22.ref_tok = {}
    for dsc, pr, npj in ((q22, "10", 6), ("reflective pavement marker", "4", 9),
                         ("reflective pavement marker white", "5", 9)):
        t22.ref_tok[(item_tokens.tokens(dsc), "EA", "0-10")] = {
            "price_median": pr, "n_projects": npj, "vintage_year": None,
            "usable": "yes", "tok": dsc}
    t22.tok_inv = collections.defaultdict(lambda: collections.defaultdict(list))
    for k in t22.ref_tok:
        for w in k[0]:
            t22.tok_inv["EA"][w].append(k)
    p22t = item_market_price(t22, "other", "EA", 5.0, "", None, "txdot", q22)
    p22m = item_market_price(t22, "other", "EA", 5.0, "", None, "municipal",
                             q22)
    with pricing_rule(txdot_c4d1=False):
        p22o = item_market_price(t22, "other", "EA", 5.0, "", None, "txdot",
                                 q22)
    chk("22 C4: TxDOT takes the exact cell; municipal and off the median",
        (p22t["price"], p22t.get("token_exact_rule"), p22m["price"],
         p22o["price"]), (10.0, True, 5.0, 5.0))
    t22.disc = {4: {"f": 0.9, "p25": 0.8, "p75": 1.0, "lettings": 40}}
    t22.disc_muni = {4: {"f": 0.8, "p25": 0.7, "p75": 0.9, "lettings": 40}}
    t22.comparables, t22.size_cells, t22.D = None, None, "2024-06-01"
    t22.t1 = T1State(pool22, av22)
    for i in range(D1_MIN):
        t22.t1.note_scored("2024-01-%02d" % (i + 1), 4, 100.0, 0.7, 1.0)
    it22 = [{"family": "water_pipe", "unit": "LF", "quantity": 100.0,
             "description": "8 in water line"},
            {"family": "mobilization", "unit": "LS", "quantity": 1.0,
             "description": "mobilization"}]
    r22 = estimate(t22, it22, 4, "txdot")
    S22 = r22["trace"][0]["price"] * 100.0
    r_mob = t22.t1.mob.at("2024-06-01")
    chk("22 TxDOT: no T1 ratio with < MOB_MIN_LETTINGS lettings; D1 f",
        (r_mob[1] < MOB_MIN_LETTINGS, r22["trace"][1].get("price_key")
         == "mobilization_ratio", r22["discount"]["f"]), (True, False, 0.7))
    t22.t1.mob.lets = ["2024-01-01"] * MOB_MIN_LETTINGS
    r22 = estimate(t22, it22, 4, "txdot")
    chk("22 TxDOT: mobilization = r x S, total and bid part follow",
        (r22["trace"][1]["price_key"], round(r22["trace"][1]["price"], 9),
         round(r22["market_total"], 9), round(r22["bid_part"], 9)),
        ("mobilization_ratio", round(r_mob[0] * S22, 9),
         round(S22 + r_mob[0] * S22, 9), round((S22 + r_mob[0] * S22) * 0.7, 9)))
    r22m = estimate(t22, it22, 4, "municipal")
    with pricing_rule(txdot_c4d1=False):
        r22o = estimate(t22, it22, 4, "txdot")
    chk("22 municipal and switched off: no ratio, production f(n)",
        (r22m["discount"]["f"], r22m["trace"][1].get("price_key"),
         r22o["discount"]["f"], r22o["trace"][1].get("price_key")),
        (0.8, None, 0.9, None))
    chk("22 the switch is restored", TXDOT_C4D1, True)

    # 23 coverage scope: an uncovered line is not priced, out of the total,
    # listed with its class; a quote is face value outside P50/P80 and gives
    # the complete total; an allowance with its amount stays owner-set;
    # not_covered by majority and by a whole-facility LS line; the switch
    # off prices every line; MobRatio and covered_actual
    t23 = Tables.__new__(Tables)
    t23.__dict__.update({k: v for k, v in t22.__dict__.items()})
    t23.t1, t23.comparables = None, None
    t23.calib = {"municipal": {"level_correction": "1", "quantiles_basis":
                               "centred", "p10": "0.8", "p90": "1.2",
                               "p25": "0.9", "p75": "1.1", "p20": "0.85"}}
    base23 = [{"family": "water_pipe", "unit": "LF", "quantity": 100.0,
               "description": "8 in water line", "item_no": "1"},
              {"family": "mobilization", "unit": "LS", "quantity": 1.0,
               "description": "mobilization", "item_no": "2"}]
    pump23 = {"family": "other", "unit": "EA", "quantity": 2.0, "item_no": "3",
              "description": "furnish and install 250 hp vertical turbine pumps"}
    r23 = estimate(t23, base23 + [pump23], 4, "municipal")
    with pricing_rule(coverage_scope=False):
        r23o = estimate(t23, base23 + [pump23], 4, "municipal")
    r23b = estimate(t23, base23, 4, "municipal")
    chk("23 the uncovered pump line: not priced, listed, outside the total",
        (r23["trace"][2]["price"], r23["trace"][2]["uncovered"],
         [(u["item"], u["class"], u["status"]) for u in r23["uncovered"]],
         round(r23["uncovered_share_lines"], 6), r23["market_total"]
         == r23b["market_total"], round(r23["expected_low"], 9)
         == round(r23b["expected_low"], 9),
         r23["unpriced"] == r23b["unpriced"],
         r23["warnings"][0].startswith(COVERAGE_STATUS), r23["not_covered"]),
        (None, "pump_equipment", [("3", "pump_equipment", COVERAGE_STATUS)],
         round(1 / 3, 6), True, True, True, True, False))
    chk("23 switched off: the pump line is priced (or counted unpriced) as "
        "before, nothing uncovered",
        (r23o["trace"][2].get("uncovered"), "uncovered" in r23o,
         r23o["trace"][2]["price_key"] != "uncovered"), (None, False, True))
    q23 = dict(pump23, quote=900000.0)
    r23q = estimate(t23, base23 + [q23], 4, "municipal")
    chk("23 a vendor quote: face value outside P50/P80, complete total",
        (round(r23q["expected_low"], 6) == round(r23["expected_low"], 6),
         round(r23q["complete_total"] - r23q["expected_low"], 6),
         round(r23q["complete_total_p80"] - r23q["expected_low_p80"], 6),
         r23q["uncovered"][0]["quote"]), (True, 900000.0, 900000.0, 900000.0))
    al23 = {"family": "contingency", "unit": "LS", "quantity": 1.0,
            "description": "lift station contingency", "amount": 50000.0}
    r23a = estimate(t23, base23 + [al23], 4, "municipal")
    chk("23 an allowance-like uncovered line with its amount stays owner-set",
        (r23a["trace"][2].get("fixed_amount"), r23a["uncovered"]),
        (50000.0, []))
    r23w = estimate(t23, base23 + [{"family": "other", "unit": "LS",
                                    "quantity": 1.0, "item_no": "9",
                                    "description": "Lift Station - Complete "
                                                   "in Place"}],
                    4, "municipal")
    r23m = estimate(t23, [base23[0], pump23, dict(pump23, item_no="4")], 4,
                    "municipal")
    if WHOLE_FACILITY_RULE:
        chk("23 not_covered: a whole-facility LS line; uncovered lines > half; "
            "no P50/P80, only the warning",
            (r23w["not_covered"], r23w["expected_low"], len(r23w["warnings"]),
             r23m["not_covered"], r23m["expected_low"], r23m.get("expected_low_p80"),
             r23["not_covered"]), (True, None, 1, True, None, None, False))
    else:
        chk("23 not_covered: uncovered lines > half (rule (a)); a whole-facility "
            "LS line alone is listed, flagged, and does not void the estimate",
            (r23w["not_covered"], r23w["expected_low"] is not None,
             [u["whole_facility"] for u in r23w["uncovered"]],
             r23m["not_covered"], r23m["expected_low"], r23m.get("expected_low_p80"),
             r23["not_covered"]), (False, True, [True], True, None, None, False))
    chk("23 the whole-facility rule (b) reads the description only",
        [is_whole_facility(d, u) for d, u in (
            ("lift station complete in place", "LS"),
            ("complete pump station", "LS"),
            ("lift station rehabilitation", "L.S."),
            ("ground storage tank 1 0 mg", "LS"),
            ("lift station complete in place", "EA"),
            ("lift station odor control", "LS"),
            ("lift station site work", "LS"),
            ("electrical building", "LS"),
            ("paint interior wet coating system for 1 500 000 gallon "
             "elevated storage tank", "LS"))],
        [True, True, True, True, False, False, False, False, False])
    pool23 = []
    for b, (mob_, pmp_) in enumerate(((10.0, 1000.0), (20.0, 3000.0))):
        for fam_, un_, q_, u_, dn_ in (
                ("mobilization", "LS", 1.0, mob_, "mobilization"),
                ("excavation", "CY", 10.0, 10.0, "excavation"),
                ("other", "EA", 1.0, pmp_, "furnish and install pumps")):
            pool23.append(("txdot", "TP", fam_, un_, "", q_, u_, b == 0,
                           "b%d" % b, dn_, "o", "2024"))
    av23 = {"TP": "2024-01-10"}
    with pricing_rule(coverage_scope=False):
        mr23o = MobRatio(pool23, av23, "txdot").at(None)
    mr23 = MobRatio(pool23, av23, "txdot").at(None)
    chk("23 MobRatio: uncovered lines out of the base under the coverage scope",
        (mr23, mr23o), ((statistics.median([0.1, 0.2]), 1),
                        (statistics.median([10.0 / 1100.0, 20.0 / 3100.0]),
                         1)))
    k23 = ("other", "EA", "", "0-10", "furnish and install pumps")
    d23 = {"wamt": {k23: 1000.0}, "bamt": {k23: {"b0": 1000.0, "b1": 3000.0,
                                                 "b2": 2000.0}}}
    cv23 = covered_actual(d23, {k23}, 5000.0)
    with pricing_rule(coverage_scope=False):
        cv23o = covered_actual(d23, {k23}, 5000.0)
    chk("23 covered actual: low minus the low bidder's own uncovered amount; "
        "the median bidder's for the sensitivity; the whole low bid when off",
        (cv23["actual_low"], cv23["actual_low_med"], cv23["actual_low_full"],
         cv23o), (4000.0, 3000.0, 5000.0, {"actual_low": 5000.0}))
    chk("23 the switch is restored", COVERAGE_SCOPE, True)

    # 24 MC: a municipal line reads the municipal-only cell of the same key
    # (token, band and family tiers) when it has >= MUNI_CELLS_NMIN lettings,
    # else the pooled cell; TxDOT lines and the switch off read the pooled
    # cell; forward, a letting dated D and later ones never count.  MC is OFF
    # by default (round 10 CORE), so these run under pricing_rule(muni_cells=
    # True)
    chk("24 MC is off in production", MUNI_CELLS, False)
    pool23, av23 = [], {}
    n_mu = MUNI_CELLS_NMIN
    for i in range(n_mu + 7):                      # TxDOT first, then municipal
        mk23 = "txdot" if i < 7 else "municipal"
        pk = "%s%d" % (mk23[0].upper(), i)
        av23[pk] = ("2023-%02d-01" % (i + 1)) if i < 7 else (
            "2024-%02d-01" % (i - 6))
        u23 = 6.0 if mk23 == "municipal" else 2.0
        for b in range(2):
            pool23.append((mk23, pk, "zz_test", "CY", "", 50.0, u23, b == 0,
                           "b%d" % b, "special widget fill", "o", "2024"))
            pool23.append((mk23, pk, "zz_band", "CY", "", 50.0, u23 * 10, b == 0,
                           "b%d" % b, "", "o", "2024"))
    cells23 = _reference_cells(pool23)
    t23 = Tables.__new__(Tables)
    t23.__dict__.update({k: v for k, v in t19.__dict__.items()})
    t23.ref_tok = _HeldOutMap(cells23["tok"], None, 2, "tok", {})
    t23.ref_band = _HeldOutMap(cells23["band"], None, 2, "band", {})
    t23.ref = _HeldOutMap(cells23["fam"], None, 1, "fam", {})
    t23.tok_inv = cells23["tok_inv"]
    t23.size_cells, t23.D, t23.comparables, t23.t1 = None, None, None, None
    t23.disc = {4: {"f": 0.9, "p25": 0.8, "p75": 1.0, "lettings": 40}}
    t23.disc_muni = dict(t23.disc)
    with pricing_rule(muni_cells=True):
        kinds23, cm23 = muni_forward_kinds(pool23, av23)
    chk("24 MC off: no municipal cells are built",
        muni_forward_kinds(pool23, av23), (None, None))
    t23.ref_tok_m = _MuniMap(cm23["tok"], None, MUNI_CELLS_NMIN, "tok", {})
    t23.ref_band_m = _MuniMap(cm23["band"], None, MUNI_CELLS_NMIN, "band", {})
    t23.ref_m = _MuniMap(cm23["fam"], None, MUNI_CELLS_NMIN, "fam", {})
    it23 = [{"family": "zz_test", "unit": "CY", "quantity": 50.0,
             "description": "special widget fill"},              # token tier
            {"family": "zz_band", "unit": "CY", "quantity": 50.0,
             "description": ""},                                 # band tier
            {"family": "zz_band", "unit": "CY", "quantity": 5000.0,
             "description": ""}]                                 # family tier
    pr23 = lambda r: [(t["price"], t["price_key"], bool(t.get("muni_cell")))  # noqa: E731
                      for t in r["trace"]]
    with pricing_rule(muni_cells=True):
        r23m = estimate(t23, it23, 4, "municipal")
        r23t = estimate(t23, it23, 4, "txdot")
    with pricing_rule(muni_cells=False):
        r23o = estimate(t23, it23, 4, "municipal")
    pooled23 = statistics.median([6.0] * n_mu + [2.0] * 7)
    chk("24 MC: municipal line -> municipal cells (token, band, family)",
        pr23(r23m), [(6.0, "token_jaccard", True),
                     (60.0, "family+unit+size+band", True),
                     (60.0, "family+unit+size", True)])
    chk("24 MC: TxDOT and switched off -> the pooled cells",
        (pr23(r23t), pr23(r23o)),
        ([(pooled23, "token_jaccard", False),
          (pooled23 * 10, "family+unit+size+band", False),
          (pooled23 * 10, "family+unit+size", False)],) * 2)
    t23.ref_tok_m = _MuniMap(cm23["tok"], None, n_mu + 1, "tok", {})
    with pricing_rule(muni_cells=True):
        r23n = estimate(t23, it23[:1], 4, "municipal")
    chk("24 MC: a municipal cell under the minimum is not used",
        pr23(r23n), [(pooled23, "token_jaccard", False)])
    last_m = max(d for k, d in av23.items() if k.startswith("M"))
    ft23 = ForwardTables(t23, _forward_kinds(cells23, av23),
                         cells23["tok_inv"], last_m, {}, t23.disc,
                         t23.disc_muni, muni_kinds=kinds23)
    ft23b = ForwardTables(t23, _forward_kinds(cells23, av23),
                          cells23["tok_inv"], "2025-01-01", {}, t23.disc,
                          t23.disc_muni, muni_kinds=kinds23)
    with pricing_rule(muni_cells=True):
        f23 = (estimate(ft23, it23[:1], 4, "municipal")["trace"][0]["price"],
               estimate(ft23b, it23[:1], 4, "municipal")["trace"][0]["price"])
    chk("24 MC forward: on the last municipal letting's own date only the "
        "earlier ones count (pooled cell); after it, the municipal cell",
        f23, (statistics.median([2.0] * 7 + [6.0] * (n_mu - 1)), 6.0))
    chk("24 the switch is restored (off)", MUNI_CELLS, False)

    # 25 round 10 CORE = MC + E + F1P + T2 + D, every switch OFF by default:
    # the preset round10_core turns on exactly those five and is undone after
    # its block; F1P's pipe-like rule; T2 on a municipal family cell backed by
    # one letting; D's identical-bid rule and its back-test schedule hook;
    # E's municipal removal identity on the comparables side
    cmp25 = globals()["comparables"]
    sw25 = lambda: (MUNI_CELLS, cmp25.MUNI_REMOVAL_IDENTITY,  # noqa: E731
                    F1_PIPE_ONLY, FAMILY_CELL_T2, IDENTICAL_BID_OWNER_SET)
    off25 = sw25()
    with pricing_rule(round10_core=True):
        on25 = sw25()
    with pricing_rule(muni_cells=True):
        mc25 = sw25()
    chk("25 production: the five off; round10_core turns on exactly the five; "
        "muni_cells alone only MC; restored after",
        (off25, on25, mc25, sw25(), len(ROUND10_CORE)),
        ((False,) * 5, (True,) * 5, (True, False, False, False, False),
         (False,) * 5, 5))
    chk("25 F1P: pipe-like families; pipe_fittings only when its size is a "
        "diameter (not the 4 of '2 1/4 in')",
        [_f1p_ok(f, d, z) for f, d, z in (
            ("storm_pipe", "24 in rcp", 24.0),
            ("curb_gutter", "6 in curb", 6.0),
            ("pipe_fittings", "12 in gate valve tee", 12.0),
            ("pipe_fittings", "anchor sleeve 2 1/4 in", 4.0),
            ("pipe_fittings", "ductile iron fittings", 8.0))],
        [True, False, True, False, False])
    pool25 = [("municipal", "M25", "zz_one", "CY", "", 50.0, 7.0, b == 0,
               "b%d" % b, "", "o", "2024") for b in range(2)]
    cells25 = _reference_cells(pool25)
    t25 = Tables.__new__(Tables)
    t25.__dict__.update({k: v for k, v in t23.__dict__.items()})
    t25.ref_tok = _HeldOutMap(cells25["tok"], None, 2, "tok", {})
    t25.ref_band = _HeldOutMap(cells25["band"], None, 2, "band", {})
    t25.ref = _HeldOutMap(cells25["fam"], None, 1, "fam", {})
    t25.tok_inv = cells25["tok_inv"]
    t25.ref_tok_m = t25.ref_band_m = t25.ref_m = None
    it25 = [{"family": "zz_one", "unit": "CY", "quantity": 50.0,
             "description": ""}]
    with pricing_rule(family_cell_t2=True):
        r25m = estimate(t25, it25, 4, "municipal")
        r25t = estimate(t25, it25, 4, "txdot")
    r25o = estimate(t25, it25, 4, "municipal")
    chk("25 T2: a municipal family cell backed by one letting is not used; "
        "TxDOT and the switch off use it",
        (r25m["trace"][0]["price"], bool(r25m["trace"][0].get("t2_skipped")),
         r25t["trace"][0]["price"] is not None,
         r25o["trace"][0]["price"] == r25t["trace"][0]["price"]),
        (None, True, True, True))
    chk("25 D: >= 3 distinct bidders, one identical price, LS or "
        "allowance-like (or exactly 1.00)",
        [identical_bid_owner_set(ps, n, f, u, d) for ps, n, f, u, d in (
            ([5000.0] * 3, 3, "traffic_control", "LS", "traffic control"),
            ([5000.0] * 2, 2, "traffic_control", "LS", "traffic control"),
            ([5000.0, 5000.0, 5100.0], 3, "traffic_control", "LS",
             "traffic control"),
            ([12.0] * 4, 4, "excavation", "CY", "excavation"),
            ([1.0] * 3, 3, "other", "EA", "testing"),
            ([250.0] * 3, 3, "other", "ALLOW", "material testing"))],
        [True, False, False, False, True, True])
    k25 = ("traffic_control", "LS", "", "0-10", "traffic control")
    d25 = {"mk": "municipal", "prices": {k25: [5000.0] * 3},
           "nb": {k25: {"a", "b", "c"}}}
    sos_off = schedule_owner_set(d25, k25)
    with pricing_rule(identical_bid_owner_set=True):
        sos_on = schedule_owner_set(d25, k25)
        sos_tx = schedule_owner_set(dict(d25, mk="txdot"), k25)
    chk("25 D in the back-test schedule: municipal lettings, switch on only",
        (sos_off, sos_on, sos_tx), ((False, False), (True, True),
                                    (False, False)))
    rm25 = "remove existing sanitary sewer pipe"
    rr25 = "remove and replace 6 in concrete pavement"
    e_off = item_tokens.RM_FLAG in cmp25.tokens(rm25, "municipal")
    with pricing_rule(cmp_removal_identity=True):
        e_on = (item_tokens.RM_FLAG in cmp25.tokens(rm25, "municipal"),
                item_tokens.RM_FLAG in cmp25.tokens(rm25, "txdot"),
                item_tokens.RM_FLAG in cmp25.tokens(rr25, "municipal"))
    chk("25 E: the municipal comparables identity carries the removal flag "
        "(not TxDOT, not remove-and-replace, not with the switch off)",
        (e_off,) + e_on, (False, True, False, False))
    chk("25 the switches are restored (off)", sw25(), (False,) * 5)

    # 26 round 11's selected arm: the preset round11_core_not2 turns on
    # exactly MC, E, F1P and D (not T2), and is undone after its block
    with pricing_rule(round11_core_not2=True):
        on26 = sw25()
    chk("26 round11_core_not2 = round10_core without T2; restored after",
        (on26, sw25(), ROUND11_CORE_NOT2,
         tuple(s for s in ROUND10_CORE if s != "family_cell_t2")),
        ((True, True, True, False, True), (False,) * 5, ROUND11_CORE_NOT2,
         ROUND11_CORE_NOT2))

    # 27 low confidence (LOWCONF): classes, the forward history, and the block is
    # presentation only -- the same schedule gives the same P50/P80 with it
    # on and off
    chk("27 low-confidence classes",
        [lowconf_class(f, u, dsc) for f, u, dsc in (
            ("mobilization", "LS", "mobilization"),
            ("mobilization", "EA", "mobilization bonds"),
            ("other", "LS", "mobilizaton and demobilization"),
            ("other", "LS", "Mobilization (5% max)"),
            ("bonds_insurance", "LS", "bonds and insurance"),
            ("traffic_control", "LS", "traffic control"),
            ("traffic_control", "MO", "traffic control"),
            ("erosion_control", "LS", "swppp"),
            ("erosion_control", "LF", "silt fence"),
            ("site_prep_clearing", "LS", "general site preparation"),
            ("other", "LS", "right of way/easement preparation"),
            ("other", "LS", "general site preperation"),
            ("other", "LS", "storm water pollution prevention plan"),
            ("other", "LS", "scada"),
            ("needs_review", "LS", "mobilization max 5 of total bid"),
            ("storm_pipe", "LF", "24 in rcp"))],
        ["mobilization", "mobilization", "mobilization", "mobilization",
         "bonds_insurance", "traffic_control", None, "swppp_erosion", None,
         "site_prep", "site_prep", "site_prep", "swppp_erosion", None,
         "mobilization", None])
    # a pool of 20 + 20 small lettings (rest $100k, mobilization 5% / 3%
    # of the low bid) dated 2024, and 20 large ones (rest $5M, 4%)
    pool27, avail27 = [], {}
    for i in range(60):
        pk = "lc27_%02d" % i
        big = i >= 40
        rest = 5e6 if big else 1e5
        sh = 0.04 if big else (0.05 if i < 20 else 0.03)
        mob = rest * sh / (1 - sh)
        avail27[pk] = "2024-%02d-%02d" % (1 + i // 28, 1 + i % 28)
        for b, mult, w in (("A", 1.0, True), ("B", 1.2, False)):
            for fam, un, q, u, dn in (
                    ("mobilization", "LS", 1.0, mob * mult, "mobilization"),
                    ("storm_pipe", "LF", 100.0, rest / 300 * mult, "24 rcp"),
                    ("excavation", "CY", 100.0, rest / 300 * mult, "exc"),
                    ("sidewalk", "SY", 100.0, rest / 300 * mult, "walk")):
                pool27.append(("municipal", pk, fam, un, "", q, u, w, b, dn,
                               "own", "2024"))
    lch27 = LowConfShare(pool27, avail27)
    h_small = lch27.at("municipal", "mobilization", 1e5, "2025-01-01")
    h_early = lch27.at("municipal", "mobilization", 1e5, "2024-01-10")
    h_big = lch27.at("municipal", "mobilization", 4e6, "2025-01-01")
    h_mid = lch27.at("municipal", "mobilization", 1e6, "2025-01-01")
    chk("27 history: same size class, forward in time, all sizes when thin",
        (h_small["n"], h_small["scope"], round(h_small["p25"], 4),
         round(h_small["p75"], 4), h_early, h_big["n"],
         round(h_big["median"], 4), h_mid["scope"], h_mid["n"],
         lch27.at("municipal", "traffic_control", 1e5, "2025-01-01"),
         lch27.at("txdot", "mobilization", 1e5, "2025-01-01")),
        (40, "size", 0.03, 0.05, None, 20, 0.04, "all_sizes", 60, None,
         None))
    # presentation only: estimate() with the block on vs off
    t27 = Tables.__new__(Tables)
    t27.__dict__.update(t.__dict__)
    t27.ref = dict(t.ref)
    t27.ref[("mobilization", "LS", "")] = {
        "price_median": "80000", "qty_median": "1", "vintage_year": "2025",
        "usable": "yes", "n_projects": "30", "n_owners": "9"}
    t27.calib = {"municipal": {"market": "municipal", "n": "155",
                               "p10": "0.85", "p25": "0.928", "p75": "1.053",
                               "p90": "1.15", "quantiles_basis": "centred",
                               "level_correction": "1.02",
                               "p_not_under": "0.80"}}
    t27.lowconf = lch27
    s27 = [{"item_no": "1", "family": "mobilization", "unit": "LS",
            "quantity": 1, "description": "Mobilization (5% max)"},
           {"item_no": "2", "family": "gabion_wall", "unit": "CY",
            "quantity": 300, "description": "gabion"},
           {"item_no": "3", "family": "contingency", "unit": "LS",
            "quantity": 1, "amount": 10000.0,
            "description": "construction contingency"}]
    with pricing_rule(comparables=False):
        on27 = estimate(t27, s27, 4, "municipal", "2025",
                        target={"date": "2025-06-01"})
        g = globals()
        g["LOWCONF"] = False
        try:
            off27 = estimate(t27, s27, 4, "municipal", "2025",
                             target={"date": "2025-06-01"})
        finally:
            g["LOWCONF"] = True
    lc27 = (on27.get("low_confidence") or {}).get("lines") or [{}]
    m27 = lc27[0]
    pm27 = sum(tt["price"] * tt["quantity"] for tt in on27["trace"]
               if tt.get("price"))
    chk("27 P50/P80 identical with the block on and off; no block when off",
        (on27["expected_low"] == off27["expected_low"],
         on27["expected_low_p80"] == off27["expected_low_p80"],
         on27["market_total"] == off27["market_total"],
         "low_confidence" in off27, len(lc27)),
        (True, True, True, False, 1))
    rest27 = on27["expected_low"] - 10000.0 - m27.get("ours_p50", 0)
    chk("27 the block: ours at the P50 level, the size class of the rest, "
        "the implied range and where ours sits",
        (m27.get("class"), m27.get("status"), m27.get("items"),
         round(m27.get("ours_p50", 0), 2),
         round(m27.get("ours_p80", 0), 2),
         (m27.get("history") or {}).get("scope"),
         (m27.get("history") or {}).get("size_class"),
         tuple(round(x, 2) for x in m27.get("implied_p50_range", ())),
         m27.get("position")),
        ("mobilization", LOWCONF_LABEL, ["1"],
         round(80000.0 * (on27["expected_low"] - 10000.0) / pm27, 2),
         round(80000.0 * (on27["expected_low"] - 10000.0) / pm27
               / 0.80, 2),
         "size" if rest27 < 5e5 else "all_sizes",
         lowconf_size_class(rest27),
         tuple(round(rest27 * x / (1 - x), 2) for x in (
             lch27.at("municipal", "mobilization", rest27,
                      "2025-06-01")["p25"],
             lch27.at("municipal", "mobilization", rest27,
                      "2025-06-01")["p75"])),
         "above"))
    t27b = Tables.__new__(Tables)
    t27b.__dict__.update(t27.__dict__)
    del t27b.__dict__["lowconf"]
    with pricing_rule(comparables=False):
        nb27 = estimate(t27b, s27, 4, "municipal", "2025",
                        target={"date": "2025-06-01"})
    nl27 = nb27["low_confidence"]["lines"][0]
    chk("27 bare tables (no history): ours shown, no range, same P50",
        (nl27.get("history"), "implied_p50_range" in nl27,
         nb27["expected_low"] == on27["expected_low"]),
        (None, False, True))

    # 29. round 13 P_L (synthetic): the market level, the share, the hook,
    # the preset.
    av29 = {"m1": "2023-01-01", "m2": "2023-06-01", "t1": "2023-02-01",
            "t2": "2023-03-01", "t3": "2023-04-01", "m3": "2024-01-01",
            "m4": "2023-07-01", "t4": "2023-08-01"}
    mk29 = {k: ("municipal" if k.startswith("m") else "txdot") for k in av29}
    fam29 = {("pipe", "LF", "12"): {"m1": [(100.0, 50.0, 2023.0, "a")],
                                    "t1": [(80.0, 500.0, 2023.0, "b")]},
             ("pipe", "LF", "24"): {"m2": [(220.0, 40.0, 2023.0, "c")],
                                    "t2": [(180.0, 400.0, 2023.0, "d")],
                                    "m3": [(999.0, 10.0, 2024.0, "e")]},
             ("pipe", "LF", ""): {"t3": [(30.0, 100.0, 2023.0, "f")]}}
    band29 = {}
    for i in range(3):
        band29[("conc", "SY", "", "b%d" % i)] = {
            "m1": [(120.0, 1.0, 2023.0, "a")], "m2": [(120.0, 1.0, 2023.0, "a")],
            "m4": [(120.0, 1.0, 2023.0, "a")],
            "t1": [(100.0, 1.0, 2023.0, "b")], "t2": [(100.0, 1.0, 2023.0, "b")],
            "t3": [(100.0, 1.0, 2023.0, "b")]}
    band29[("conc#rm", "SY", "", "b9")] = {"m1": [(10.0, 1.0, 2023.0, "a")]}
    ml29 = MarketLevel({"band": band29, "fam": fam29, "tok": {}}, mk29, av29)
    r29 = ml29.ratio("conc", "2023-12-31")
    chk("29 market level: family scope, 3 both-market cells",
        (round(r29[0], 6), r29[1], r29[2]), (1.2, "family", 3))
    chk("29 market level: too few lettings before D -> none (all < 10)",
        ml29.ratio("conc", "2023-06-15")[1:], ("none", 0))
    chk("29 market level: other family falls back to all (< 10 -> none)",
        ml29.ratio("pipe", "2023-12-31")[1:], ("none", 3))
    chk("29 share: TxDOT share of the cell's lettings before D",
        (ml29.share("fam", ("pipe", "LF", "24"), "2023-12-31"),
         ml29.share("fam", ("pipe", "LF", "24"), "2025-01-01"),
         ml29.share("fam", ("nope", "LF", ""), "2025-01-01")),
        (0.5, 1.0 / 3.0, None))

    class _T29:
        pass
    t29 = _T29()
    t29.D, t29.shape = "2023-12-31", {}
    t29.market_level = ml29
    old29 = MUNI_TXDOT_LEVEL
    g29 = globals()
    g29["MUNI_TXDOT_LEVEL"] = True
    o29e = {"price": 100.0, "n_projects": 3, "why": []}
    _r13_level(t29, o29e, "band", ("conc", "SY", "", "b0"), "conc",
               "municipal")
    chk("29 P_L: x R ** s (s 3/6 = 0.5 at D)",
        round(o29e["price"], 6), round(100.0 * 1.2 ** 0.5, 6))
    o29f = {"price": 100.0, "n_projects": 3, "why": [], "muni_cell": True}
    _r13_level(t29, o29f, "band", ("conc", "SY", "", "b0"), "conc",
               "municipal")
    chk("29 P_L: a municipal-only (MC) cell -> s 0, unchanged",
        o29f["price"], 100.0)
    o29d = {"price": 100.0, "n_projects": 3, "why": []}
    _r13_level(t29, o29d, "band", ("conc", "SY", "", "b0"), "conc", "txdot")
    chk("29 P_L: a TxDOT line is left alone", o29d["price"], 100.0)
    g29["MUNI_TXDOT_LEVEL"] = old29
    with pricing_rule(round13_l=True):
        on29 = MUNI_TXDOT_LEVEL
    chk("29 preset round13_l sets / restores", (on29, MUNI_TXDOT_LEVEL),
        (True, False))

    # 34 round 17: the abandonment identity (AB token flag; AB2 "#ab" key).
    # The token cells of the brief's case: the abandon line's own words and
    # two NEW 8 in sanitary pipe cells (the production pick was a new pipe)
    def t34_tab():
        t = Tables.__new__(Tables)
        t.missing, t.notes = [], []
        t.ref, t.ref_band, t.shape, t.borrow_sigma = {}, {}, {}, {}
        t.index, t.calib, t.D, t.size_cells = {}, {}, None, None
        t.ref_tok = {}
        t.tok_inv = collections.defaultdict(
            lambda: collections.defaultdict(list))
        for d, p in (("abandon existing sanitary sewer line", "22.5"),
                     ("8 in sanitary sewer pvc pipe sdr 26 by open cut",
                      "132.35"),
                     ("8 in pvc sdr 26 astm 3034 sanitary sewer pipe", "142.5")):
            k = (item_tokens.tokens(d), "LF", "200-1000")
            t.ref_tok[k] = {"price_median": p, "n_projects": "2",
                            "usable": "no", "vintage_year": "2024",
                            "tok": " ".join(sorted(k[0]))}
            for w in k[0]:
                t.tok_inv["LF"][w].append(k)
        return t
    ab34 = "Abandon existing 8 inch sanitary sewer pipe in place"
    nw34 = "8 in pvc sdr 26 sanitary sewer pipe by open cut"
    with pricing_rule(abandon_identity=False):
        p34 = item_market_price(t34_tab(), "demo_utility", "LF", 500.0,
                                "6-9 in", None, "municipal", ab34, 8.0)
    chk("34 off: the abandon line takes a new-pipe cell",
        (p34["price"], p34["price_key"]), (132.35, "token_jaccard"))
    with pricing_rule(round17_ab=True):
        p34a = item_market_price(t34_tab(), "demo_utility", "LF", 500.0,
                                 "6-9 in", None, "municipal", ab34, 8.0)
        p34n = item_market_price(t34_tab(), "sanitary_pipe", "LF", 500.0,
                                 "6-9 in", None, "municipal", nw34, 8.0)
        on34 = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY,
                family_key("water_pipe", "abandon existing 8 in water line"))
    chk("34 AB: only the abandon cell (22.5); new pipe never the abandon cell",
        (p34a["price"], p34a["token_candidates"], p34n["price"] > 100),
        (22.5, 1, True))
    chk("34 preset round17_ab: the flag only, no family key; restored",
        (on34, item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY),
        ((True, False, "water_pipe"), True, False))
    with pricing_rule(round17_ab2=True):
        on34b = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY,
                 family_key("water_pipe", "abandon existing 8 in water line"),
                 family_key("water_pipe", "remove existing 8 in water line"),
                 family_key("water_pipe", "8 in water line"))
        t34b = Tables.__new__(Tables)
        t34b.__dict__.update({k: v for k, v in t20.__dict__.items()})
        items34 = [dict(x) for x in items20[:2]] + [
            {"family": "water_pipe", "unit": "LF", "quantity": 50.0,
             "description": "abandon existing water line"}]
        r34b = estimate(t34b, items34, 4, "municipal")
    chk("34 preset round17_ab2: flag + #ab key (#rm first); restored",
        (on34b, item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY),
        ((True, True, "water_pipe#ab", "water_pipe#rm", "water_pipe"),
         True, False))
    chk("34 AB2: an abandon line with no #ab cell gets the typical line",
        (r34b["trace"][2]["price_key"],
         round(r34b["trace"][2]["price"] * 50, 6),
         r34b["trace"][2]["why"][-1].startswith("abandon-only line")),
        ("removal_typical_line", round(statistics.median([4500.0, 13500.0]), 6),
         True))
    with pricing_rule(abandon_identity=False):
        r34o = estimate(t34b, items34, 4, "municipal")
    chk("34 off: the same line priced from the family's new-work cell",
        (r34o["trace"][2]["price_key"], r34o["trace"][2]["price"]),
        ("family+unit+size", 45.0))

    # 35. round 17 B80 (synthetic; round 16's group 33 without the clamp):
    # forward_scores(b_records) takes the divisor and the guard's level from
    # B's records at each date and the P50 (and its centring) from its own;
    # production_b() and the presets.
    import random as _rnd35
    r35 = _rnd35.Random(33)
    pl35 = []
    for k in range(70):
        pl35.append({"project_key": "m%03d" % k, "market": "municipal",
                     "date": "%04d-%02d-%02d" % (2021 + k // 24,
                                                 1 + (k % 24) // 2,
                                                 1 + (k % 2) * 14),
                     "bid_part": r35.uniform(0.6, 1.6) * 1e6,
                     "owner_fixed": (r35.uniform(0, 5e4) if k % 5 == 0
                                     else 0.0),
                     "actual_low": 1e6})
    b35 = [dict(r) for r in pl35]
    forward_scores(b35)
    v35 = [dict(r, bid_part=r["bid_part"] * (1.1 if k % 3 == 0 else 1.0))
           for k, r in enumerate(pl35)]
    own35 = [dict(r) for r in v35]
    forward_scores(own35)
    x35 = [dict(r) for r in v35]
    forward_scores(x35, b_records=b35)
    sc35 = [(a, b, c) for a, b, c in zip(b35, own35, x35) if "p50" in a]
    chk("35 B80: P50 and centring its own, divisor and alpha B's, P80 = own "
        "P50 / B's divisor, flagged",
        (len(sc35) > 0, all(c["p50"] == b["p50"] and
                            c["centring"] == b["centring"] and
                            c["divisor"] == a["divisor"] and
                            c["alpha"] == a["alpha"] and
                            c["p80"] == c["p50"] / a["divisor"] and
                            c.get("p80_from_b") for a, b, c in sc35),
         any(b["divisor"] != a["divisor"] for a, b, _ in sc35)),
        (True, True, True))
    with pricing_rule(round17_ab_b80=True):
        on35 = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY,
                P80_FROM_B)
        with production_b():
            pb35 = (item_tokens.ABANDON_IDENTITY,
                    item_tokens.ABANDON_FAMILY_KEY, P80_FROM_B,
                    MUNI_TXDOT_LEVEL)
        back35 = (item_tokens.ABANDON_IDENTITY, P80_FROM_B)
    with pricing_rule(round17_ab2_b80=True):
        on35b = (item_tokens.ABANDON_IDENTITY, item_tokens.ABANDON_FAMILY_KEY,
                 P80_FROM_B)
    chk("35 presets round17_ab_b80 / _ab2_b80; production_b clears them; "
        "restored",
        (on35, pb35, back35, on35b, (item_tokens.ABANDON_IDENTITY,
                                     item_tokens.ABANDON_FAMILY_KEY,
                                     P80_FROM_B)),
        ((True, False, True), (False, False, False, False), (True, True),
         (True, True, True), (True, False, True)))

    # 32. round 16 C (synthetic): a bidder count above the table's top bin
    # is priced off that bin, marked extrapolated; every other refusal and
    # the n <= 1 rule are production's; the switch and its preset.
    t32 = Tables.__new__(Tables)
    t32.disc = {2: {"f": 0.93, "p25": 0.84, "p75": 0.97, "lettings": 50},
                3: {"f": 0.90, "p25": 0.82, "p75": 0.96, "lettings": 40},
                10: {"f": 0.83, "p25": 0.78, "p75": 0.87, "lettings": 12}}
    t32.disc_muni = {}
    t32b = Tables.__new__(Tables)
    t32b.disc = {6: {"f": 0.84, "p25": 0.78, "p75": 0.90, "lettings": 30},
                 7: {"f": 0.83, "p25": 0.76, "p75": 0.89, "lettings": 20}}
    t32b.disc_muni = {6: {"f": 0.80, "p25": 0.75, "p75": 0.86, "lettings": 30},
                      7: {"f": 0.81, "p25": 0.74, "p75": 0.87, "lettings": 20}}
    with pricing_rule(clamp_high_bidders=False):
        off32 = (discount(t32, 17, "txdot"), discount(t32b, 12, "municipal"))
    with pricing_rule(clamp_high_bidders=True):
        hi32 = discount(t32, 17, "txdot")
        in32 = discount(t32, 12, "txdot")
        gap32 = discount(t32, 6, "txdot")
        low32 = discount(t32b, 2, "municipal")
        one32 = discount(t32b, 1, "municipal")
        mu32 = discount(t32b, 12, "municipal")
    chk("32 C: above the top bin -> the top bin, marked extrapolated",
        (hi32["bin"], hi32["f"], hi32["p25"], hi32["extrapolated"],
         hi32["table"], hi32["lettings"]),
        (10, 0.83, 0.78, True, "pooled", 12))
    chk("32 C: the municipal table's own top bin",
        (mu32["bin"], mu32["f"], mu32["table"], mu32["extrapolated"]),
        (7, 0.81, "municipal", True))
    chk("32 C: within 2 bins as production; a gap > 2 inside the table and "
        "below the lowest bin still refused; n <= 1 not discounted",
        (in32["bin"], in32["extrapolated"], gap32, low32, one32["f"],
         one32["extrapolated"]),
        (10, True, None, None, 1.0, False))
    chk("32 C off: above the top bin is refused", off32,
        (None, None))
    it32 = [{"family": "gabion_wall", "unit": "CY", "quantity": 245}]
    with pricing_rule(clamp_high_bidders=False):
        r32o = estimate(t, it32, 12, "municipal", "2026")
    with pricing_rule(clamp_high_bidders=True):
        r32 = estimate(t, it32, 12, "municipal", "2026")
        r32n = estimate(t, it32, 4, "municipal", "2026")
    chk("32 C: estimate() prices 12 bidders off the 4-bidder bin (= the "
        "4-bidder estimate), says so, no refusal; off refuses",
        (r32.get("expected_low") == r32n.get("expected_low") is not None,
         r32["discount"]["bin"], r32["discount"]["extrapolated"],
         any("priced off the 4-bidder bin" in w for w in r32["warnings"]),
         any("refusing" in w for w in r32["warnings"]),
         r32o.get("expected_low"),
         any("refusing" in w for w in r32o["warnings"])),
        (True, 4, True, True, False, None, True))
    with pricing_rule(round16_c=True):
        on32 = CLAMP_ABOVE_TOP_BIN
    chk("32 preset round16_c sets / restores", (on32, CLAMP_ABOVE_TOP_BIN),
        (True, True))
    with pricing_rule(round16_c_b80=True):
        on32b = (CLAMP_ABOVE_TOP_BIN, P80_FROM_B)
        with production_b():
            pb32 = (CLAMP_ABOVE_TOP_BIN, P80_FROM_B)
    chk("32 preset round16_c_b80 sets both; production_b clears them; "
        "restored", (on32b, pb32, (CLAMP_ABOVE_TOP_BIN, P80_FROM_B)),
        ((True, True), (False, False), (True, True)))

    # 36. round 18: the X1 presets set exactly their switches;
    # production_b() clears every non-production switch; all restored.
    def sw36():
        return (CLAMP_ABOVE_TOP_BIN, item_tokens.ABANDON_IDENTITY,
                item_tokens.ABANDON_FAMILY_KEY, P80_FROM_B)
    got36, pb36 = {}, {}
    for nm in ("round18_x1", "round18_x1_b80"):
        with pricing_rule(**{nm: True}):
            got36[nm] = sw36()
            with production_b():
                pb36[nm] = sw36() + (MUNI_TXDOT_LEVEL, MUNI_CELLS,
                                     comparables.MUNI_REMOVAL_IDENTITY)
    chk("36 round 18 presets set exactly their switches",
        got36, {"round18_x1": (True, True, False, True),
                "round18_x1_b80": (True, True, False, True)})
    chk("36 production_b clears every one of them",
        set(pb36.values()), {(False,) * 7})
    chk("36 restored (production: ON)", sw36(), (True, True, False, True))
    with pricing_rule(clamp_high_bidders=False, abandon_identity=False,
                      p80_from_b=False):
        off36 = sw36()
    chk("36 the three named False = production before round 18", off36,
        (False,) * 4)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


def main():
    tab = Tables()
    if tab.missing:
        print("MISSING INPUTS:", tab.missing)
    seen = tab.assert_all_bids_basis()
    if seen:
        print("price pool basis:", dict(seen))
    for n in tab.notes:
        print("NOTE:", n)
    def show(title, rows):
        print("=== %s ===" % title)
        print("%-12s %6s %12s %14s %8s %8s %8s %8s  %s"
              % ("market", "n", "median", "median|err|", "p10", "p25", "p75",
                 "p90", "discount"))
        for r in rows:
            print("%-12s %6d %12.4f %13.1f%% %8.3f %8.3f %8.3f %8.3f  %s"
                  % (r["market"], r["n"], r["median_ratio"],
                     100 * r["median_abs_error"], r["p10"], r["p25"],
                     r["p75"], r["p90"], r["discount_table"]))
        print()

    print()
    # The discount's own spread, with the price level handed to it exact.  Not
    # the estimator's error -- kept because the GAP between the two is the
    # price-level substitution error, and that gap is the thing worth working
    # on next.
    d_only = calibrate(write=False)
    show("discount only, price level held exact (NOT the estimator's error)",
         d_only)

    # these two score against the WHOLE low bid, so they run in the
    # full-total scope (pricing_rule(coverage_scope=False)); the forward
    # calibration below is the coverage scope's
    with pricing_rule(coverage_scope=False):
        fp, cov = calibrate_full_path(write=False)
    show("FULL PATH, in-sample (reads the reference it helped build; "
         "full-total scope)", fp)
    print("scoring coverage:", dict(cov))
    print()
    with pricing_rule(coverage_scope=False):
        rows, cov2 = calibrate_out_of_fold(write=False)
    show("OUT OF FOLD (leave-one-out; NOT written -- later lettings inform "
         "each price; full-total scope)", rows)
    print("scoring coverage:", dict(cov2))
    print()
    rows_f, cov3 = calibrate_forward()
    show("FORWARD (written): every letting priced only from lettings dated "
         "before it%s" % ("; COVERED lines vs the covered low bid"
                          if COVERAGE_SCOPE else ""), rows_f)
    print("scoring coverage:", dict(cov3))
    for r in rows_f:
        if r.get("not_under_rule") == "guard":
            print("  %s not-under guard: level %.3f, divide by %.4f (the "
                  "plain 20th percentile is %.4f); %d forward lettings "
                  "scored, %.1f%% came in under their P80"
                  % (r["market"], r["not_under_level"], r["p_not_under"],
                     r["p20"], r["guard_scored"],
                     100 * r["guard_under_share"]))
    a = next((r for r in fp if r["market"] == "all"), None)
    b = next((r for r in rows if r["market"] == "all"), None)
    if a and b:
        print()
        print("in-sample optimism: %.1f%% -> %.1f%% median error once a "
              "letting cannot see itself. The interval below is written from "
              "the OUT-OF-FOLD quantiles."
              % (100 * a["median_abs_error"], 100 * b["median_abs_error"]))
    if d_only and rows:
        a = next((r for r in d_only if r["market"] == "all"), None)
        b = next((r for r in rows if r["market"] == "all"), None)
        if a and b:
            print()
            print("the price level, not the discount, is where the accuracy "
                  "goes: %.1f%% -> %.1f%% median error once family medians "
                  "replace the letting's own totals."
                  % (100 * a["median_abs_error"],
                     100 * b["median_abs_error"]))
    print()
    print("-> data/clean/estimator_calibration.csv  (FORWARD quantiles and "
          "the not-under guard; the interval estimate() prints comes from "
          "these)")
    return rows_f


def run_schedule(path, n_bidders, market="municipal", to_year=None,
                 bid_date=None, owner=None, work_type=None):
    """Price a real bid schedule from a CSV and print the estimate with its
    whole derivation, item by item.

    Columns read: item_family, unit, quantity, and optionally item_no,
    description, size_bucket, size_in (the line's parsed size in inches,
    which F1 needs for a sized line) -- plus `amount` (or `unit_price`, or
    owner_fixed=yes) for owner-set money: an allowance, contingency or force
    account is taken at that face value and kept out of the discount.  A
    unit_price on an ordinary line is ignored; prices are estimated, not read.
    An estimate nobody can take apart is an estimate nobody can defend in a bid
    review, so every item prints what happened to it, including the items that
    could not be priced and why.

    Coverage scope: lines src/coverage.py calls not covered (pumps, generators,
    prefab bridges, ...) are listed FIRST, are not priced and are not in
    P50/P80; a `quote` column (or `amount`) gives a line's vendor quote, and
    once every such line has one a complete total is printed too.  The
    description is folded as the parser's desc_norm (or give `desc_norm`).
    """
    try:
        from parse_bidtabs import norm_desc as _norm_desc
    except Exception:                                     # noqa: BLE001
        _norm_desc = _fold_desc
    with open(path, encoding="utf-8-sig", newline="") as f:
        items = []
        for r in csv.DictReader(f):
            desc = (r.get("description") or "").strip()
            items.append({
                "item_no": (r.get("item_no") or "").strip(),
                "description": desc,
                "desc_norm": ((r.get("desc_norm") or "").strip()
                              or _norm_desc(desc)),
                "family": (r.get("item_family") or "").strip(),
                "unit": (r.get("unit") or "").strip(),
                "quantity": _f(r.get("quantity"), 0.0),
                "size_bucket": (r.get("size_bucket") or "").strip(),
                "size_in": _f(r.get("size_in")),
                "amount": _f(r.get("amount")),
                "quote": _f(r.get("quote") or r.get("vendor_quote")),
                "unit_price": _f(r.get("unit_price")),
                "owner_fixed": (r.get("owner_fixed") or "").strip().lower()
                in ("1", "true", "yes", "y")})
    tab = Tables()
    if tab.missing:
        print("MISSING INPUTS:", tab.missing)
    tab.attach_v2f1f2()
    target = {"date": bid_date, "owner": owner, "work_type": work_type or ""}
    r = estimate(tab, items, n_bidders, market, to_year, target)
    print_uncovered(r)

    print("%-5s %-26s %-8s %12s %12s %14s  %s"
          % ("item", "family", "unit", "quantity", "unit price", "extended",
             "notes"))
    for t in r["trace"]:
        if t.get("fixed_amount"):
            ext, ps = t["fixed_amount"], "OWNER-SET"
        elif t.get("uncovered") or t.get("price_key") == "not_covered":
            ext = t.get("quote")
            ps = "NOT COVERED" if t.get("uncovered") else "NOT PRICED"
        else:
            ext = ((t["price"] * t["quantity"])
                   if t["price"] and t["quantity"] else None)
            ps = ("%.2f" % t["price"]) if t["price"] else "NOT PRICED"
        print("%-5s %-26s %-8s %12s %12s %14s  %s"
              % (str(t.get("item") or "")[:5], t["family"][:26], t["unit"][:8],
                 ("%.2f" % t["quantity"]) if t["quantity"] else "-",
                 ps, ("%.2f" % ext) if ext else "-",
                 "; ".join(t["why"])[:72]))
    print()
    print("items priced %d of %d" % (r["priced"], r["n_items"]))
    if not r.get("not_covered"):
        print("market total (all-bids level, NO discount)   %18.2f"
              % r["market_total"])
    if r.get("fixed_total"):
        print("owner-set amounts (face value, no discount)  %18.2f   [%d items]"
              % (r["fixed_total"], r.get("n_fixed", 0)))
    d = r.get("discount")
    if d:
        print("competition discount  f(%s) = %.4f   [%s table, %d-bidder bin, "
              "%d lettings]"
              % (n_bidders, d["f"], d["table"], d["bin"], d["lettings"]))
    cov_note = ("   [COVERED LINES ONLY -- %d uncovered line(s) not in it]"
                % len(r["uncovered"]) if r.get("uncovered") else "")
    if r["expected_low"] is not None:
        print("EXPECTED LOW BID                             %18.2f%s"
              % (r["expected_low"], cov_note))
        if r.get("expected_low_p70"):
            print("P70 TRIAL OPCC (nominal 70%% target)          %18.2f   [x%.3f]%s"
                  % (r["expected_low_p70"], r["p70_multiplier"], cov_note))
        if r.get("expected_low_p80"):
            print("NOT-UNDER ESTIMATE (low bid at or below it in 80%% of "
                  "lettings)  %12.2f   [x%.3f]%s"
                  % (r["expected_low_p80"], r["p80_multiplier"], cov_note))
        for k, lab in (("p50_band", "50% of lettings land in"),
                       ("p80_band", "80% of lettings land in")):
            if r.get(k):
                print("  %-28s %16.2f  ~ %.2f" % (lab, r[k][0], r[k][1]))
        if r.get("calibration_n"):
            print("  interval calibrated on %d real lettings (%s market)"
                  % (r["calibration_n"], r.get("calibration_market")))
        if r.get("complete_total") is not None:
            print("COMPLETE TOTAL = covered P50 + quotes %.2f      %18.2f"
                  % (r["uncovered_quoted_total"], r["complete_total"]))
            if r.get("complete_total_p80") is not None:
                print("COMPLETE TOTAL = covered P80 + quotes          %18.2f"
                      % r["complete_total_p80"])
    elif r.get("not_covered"):
        print("NO ESTIMATE: NOT COVERED AS A WHOLE -- %s"
              % "; ".join(r.get("not_covered_why") or ()))
    print_low_confidence(r)
    for w in r["warnings"]:
        print("WARNING: %s" % w)
    return r


def print_uncovered(r):
    """The "not covered" block a quote printout opens with (coverage scope)."""
    unc = r.get("uncovered") or []
    if not unc:
        return
    bar = "=" * 78
    print(bar)
    if r.get("not_covered"):
        print("NOT COVERED AS A WHOLE -- NO P50/P80 IS GIVEN")
        for w in r.get("not_covered_why") or ():
            print("  because %s" % w)
    print("NOT COVERED: %d of %d lines (%.0f%% of the lines) -- "
          "price separately (get a vendor quote)"
          % (len(unc), r["n_items"], 100 * r.get("uncovered_share_lines", 0)))
    print("These are specialty items with almost no comparable bid history. "
          "They are NOT priced\nand NOT in P50/P80%s."
          % ("" if r.get("not_covered") else
             ", which cover the other lines only"))
    print("  %-6s %-46s %-26s %10s %-5s %14s"
          % ("item", "description", "class", "quantity", "unit", "quote"))
    for u in unc:
        print("  %-6s %-46s %-26s %10s %-5s %14s"
              % (str(u["item"] or "-")[:6], (u["description"] or "")[:46],
                 u["class"][:26],
                 ("%.2f" % u["quantity"]) if u["quantity"] else "-",
                 (u["unit"] or "")[:5],
                 ("%.2f" % u["quote"]) if u["quote"] else "get a quote"))
    print(bar)
    print()


def print_low_confidence(r):
    """The low-confidence block of a quote printout (LOWCONF): mobilization and the
    general-condition LS lines, each with our amount, its share of the P50,
    the earlier low bidders' share range and the dollar range it implies.
    The P50/P80 above are not changed by it."""
    lc = r.get("low_confidence") or {}
    lines = lc.get("lines") or []
    if not lines:
        return
    bar = "-" * 78
    print(bar)
    print("%s -- mobilization / general-condition LS lines, shown on their own; the P50/P80 above do not change because of them"
          % LOWCONF_LABEL)
    print("Bidders' prices for these lines differ widely and our unit prices do not scale with the job, so the amounts are for reference only; "
          "below is the share the low bidders of earlier lettings\ngave this class (p25-p75, same size class) "
          "and the dollar range it implies for this schedule.\nThe range is a guide, not a guarantee: in back-tests the actual amount "
          "fell inside it about a third of the time.")
    for g in lines:
        print("  [%s] %s  -- items %s"
              % (g["status"], g["label"],
                 ", ".join(str(x) for x in g["items"][:6] if x) or "-"))
        if g.get("ours_p50") is None:
            print("      ours: market level %.2f (no P50 to place it in)"
                  % g["ours_market"])
            continue
        print("      ours in P50  %14.2f   (%.1f%% of P50; P80 level %s; "
              "market level %.2f%s)"
              % (g["ours_p50"], 100 * (g.get("share_of_p50") or 0),
                 ("%.2f" % g["ours_p80"]) if g.get("ours_p80") else "-",
                 g["ours_market"],
                 "; %d line(s) NOT PRICED" % g["unpriced"]
                 if g["unpriced"] else ""))
        h = g.get("history")
        if not h:
            print("      history: fewer than %d earlier lettings with this "
                  "class -- no range" % LOWCONF_MIN_LETTINGS)
            continue
        lo, hi = g["implied_p50_range"]
        print("      history      share of contract p25-p75 %.1f%%-%.1f%% "
              "(median %.1f%%), %d earlier lettings, %s"
              % (100 * h["p25"], 100 * h["p75"], 100 * h["median"], h["n"],
                 ("rest of job %s" % LOWCONF_SIZE_LABELS[h["size_class"]])
                 if h["scope"] == "size" else "all job sizes"))
        print("      implied      %14.2f  ~ %.2f   (median %.2f)"
              % (lo, hi, g["implied_p50_median"]))
        print("      -> %s"
              % {"above": "ours is ABOVE the historical range (%.1fx its "
                          "median): the P50 is likely on the high side"
                          % (g["ours_p50"] / g["implied_p50_median"]),
                 "below": "ours is BELOW the historical range (%.1fx its "
                          "median): the P50 is likely on the low side"
                          % (g["ours_p50"] / g["implied_p50_median"]),
                 "within": "ours is within the historical range"}[
                     g["position"]])
    print(bar)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if "--schedule" in sys.argv:
        a = sys.argv
        sched = a[a.index("--schedule") + 1]
        nb = int(a[a.index("--bidders") + 1]) if "--bidders" in a else 4
        mk = a[a.index("--market") + 1] if "--market" in a else "municipal"
        yr = a[a.index("--year") + 1] if "--year" in a else None
        bd = a[a.index("--bid-date") + 1] if "--bid-date" in a else None
        if "--full-scope" in a:
            with pricing_rule(coverage_scope=False):
                run_schedule(sched, nb, mk, yr, bd)
        else:
            run_schedule(sched, nb, mk, yr, bd)
        sys.exit(0)
    if not _selftest():
        print("\nself-tests failed -- not calibrating")
        sys.exit(1)
    print()
    main()
