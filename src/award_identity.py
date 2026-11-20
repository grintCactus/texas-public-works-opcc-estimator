"""Which bidder WON -- one definition, used everywhere.

The rule this project runs on: only a real transacted bid may train the model,
never a supplier's quote and never an estimate.  On the two TxDOT feeds the
awarded bidder is a column in the source data.  On the municipal bid
tabulations it is not -- a bid tab prints every bidder's schedule and nothing
on the page says who got the contract.  The winner has to be derived, and the
derivation has to refuse to answer when it cannot.

THE WINNER IS A BIDDER, NOT A PRICE.  Chosen once per letting on the sum of
quantity x unit_price; then every one of that bidder's unit prices is a winning
price.  Taking min(unit_price) item by item instead builds a LOWER ENVELOPE
that no single bidder ever quoted -- measured on this data at 23% below the
actual winner.  Self-test case 7 pins that.

Validated to the cent against an external anchor: Briar Hill, where Knight
Engineering's two sections sum to 3,210,137.55 + 432,927.70 = 3,643,065.25 and
the council minutes award exactly that.

WHAT A LETTING IS.  Neither src_file nor src_sheet is one.

  * One file can hold several lettings.  A development-committee packet carries
    every agenda item; summing per src_file made a 40-item letting's winner
    (Di-Pech, $1,059,420) come out as the winner of Briar Hill, a different
    project on a later page of the same packet.
  * One letting can span many sheets.  A tab too wide for the page prints its
    bidders in groups of three and repeats the schedule, so one 10-bidder /
    40-item letting arrived as 8 sheets of 1-3 bidders each.

So sheets are joined into lettings by union-find, with two generous rules
(share a priced item identity, or share two canonical bidders), because the two
failure directions are NOT symmetric:

  over-join  -> the fused bidder sets are disjoint, the basket they ALL quote is
                empty, and winner_of_letting returns undecidable.  Loud.
  under-join -> each page group looks like a complete small letting and gets
                its own "winner", of which all but one are false.  Silent.

Generous joining plus the common-basket guard turns the silent error into the
loud one.

THE PARTIAL-SCHEDULE TRAP.  Summing a half-parsed schedule makes the
worst-parsed bidder look cheapest, so the comparison has to be stated as a
proof, not a sort:

  U, the modal schedule, is the set of item identities quoted by at least half
  the bidders -- robust to one bidder's column being misparsed in either
  direction.  Each bidder's total is summed over items(b) INTERSECT U, so an
  item only one bidder quoted cannot inflate that bidder, and the total of an
  incomplete bidder is a LOWER BOUND on what it really bid.

  A winner is named only when
      (a) it quoted all of U, so its total is exact, and
      (b) every rival's lower bound is strictly above that total,
  because then no rival can get underneath it however its missing items were
  priced.  Otherwise the letting is returned undecidable rather than guessed,
  and says which of the two conditions failed.
"""
import collections
import math
import os
import re

# A margin this small is a tie, not a win: two bidders at the same total does
# happen on small lettings, and the tab itself does not say who the owner
# picked.
TIE_REL = 1e-9

# If restricting to the modal schedule throws away more than this share of some
# bidder's dollars, the bidders are not quoting the same job and no comparison
# between them means anything.  Item counts alone miss this: a schedule can
# agree on 9 of 10 line items and disagree on the one that carries the money.
DROP_MAX = 0.10


def _dsum(d):
    return sum(d.values())


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def keep_row(r, counter=None):
    """The same row filter master_panel.csv is built with.

    It MUST match load_city() in build_master.py.  Deriving the winner from
    unfiltered line_items.csv instead gave Farmers Branch 2025-07-08 a bid of
    $1,334,298,375 against a $2,080,605 award -- a shifted column where
    quantity, unit price and the reported extension disagreed.  The panel never
    saw those rows, because ext_mismatch caught them; the award derivation
    reading one row earlier in the pipeline did.  main() reconciles its own
    surviving count against the panel and says so if they drift apart.
    """
    def no(why):
        if counter is not None:
            counter[why] += 1
        return False

    try:
        if float(r["unit_price"]) <= 0:
            return no("unit_price<=0")
    except (TypeError, ValueError):
        return no("unit_price unparseable")
    if (r.get("ext_mismatch") or "") == "Y":
        return no("ext_mismatch")
    if (r.get("unit_suspect") or "") == "Y":
        return no("unit_suspect")
    if (r.get("section_role") or "") != "base":
        # The panel keeps base bids only.  An award that included alternates
        # therefore reads short here -- a known, bounded understatement, not a
        # misidentification: the alternates are offered by every bidder alike.
        return no("not_base_section")
    return True


def group_sheets_into_lettings(rows):
    """rows: dicts carrying src_file, src_sheet, item_no, desc_norm, unit,
    quantity, bidder (already canonical).  Returns {(src_file, src_sheet): id}.

    Joining happens only WITHIN a src_file -- two files are never one letting.
    """
    uf = _UF()
    by_item = collections.defaultdict(set)      # (file, item identity) -> sheets
    by_bidder = collections.defaultdict(set)    # (file, bidder) -> sheets
    sheets = set()
    for r in rows:
        f, sh = r["src_file"], r["src_sheet"]
        s = (f, sh)
        sheets.add(s)
        uf.find(s)
        ident = (r.get("item_no") or "", r.get("desc_norm") or "",
                 r.get("unit") or "", r.get("quantity"))
        # An item identity is only evidence if it is actually identified.
        if ident[1] or ident[0]:
            by_item[(f, ident)].add(s)
        b = r.get("bidder") or ""
        if b:
            by_bidder[(f, b)].add(s)

    for ss in by_item.values():
        ss = sorted(ss)
        for s in ss[1:]:
            uf.union(ss[0], s)

    # A single shared bidder is weak -- one contractor can bid two projects in
    # the same packet.  Two shared bidders between a pair of sheets is the
    # page-group signature.
    pair = collections.Counter()
    for ss in by_bidder.values():
        ss = sorted(ss)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                pair[(ss[i], ss[j])] += 1
    nbid = collections.defaultdict(set)
    for (f, b), ss in by_bidder.items():
        for s in ss:
            nbid[s].add(b)
    for (a, b), n in pair.items():
        # Two shared bidders, or one shared bidder who is the ONLY bidder on
        # both sheets -- a single-contractor document split across pages.
        # Arlington's PWDR23012 arrived as eight sheets of Klutz Construction
        # with item numbers running 101, 201, 301 ... 801: one bid, printed per
        # site.  Fusing them cannot create a false winner, since a letting with
        # one bidder is undecidable either way; leaving them apart just
        # miscounts one bid as eight lettings.
        if n >= 2 or (len(nbid[a]) == 1 and len(nbid[b]) == 1):
            uf.union(a, b)

    return {s: uf.find(s) for s in sheets}


# A schedule this uneven is not one job being priced by several contractors.
UNEVEN_SPREAD = 2.0


def is_comparable_letting(rows):
    """False when the bidders are not pricing the same job, so no cross-bidder
    statistic from this letting means anything.

    Two conditions, both required, both measured:

      no row carries a quantity  AND  the busiest bidder lists more than
      UNEVEN_SPREAD times as many items as the thinnest.

    Neither alone is enough, and the corpus shows why.  Missing quantities
    alone catch four real Mansfield and Collin County bid tabulations whose
    quantity COLUMN simply failed to parse -- their bidders list 39, 39, 39 ...
    and 135 x 8 items, pricing one schedule each.  An uneven item count alone
    catches ordinary lettings where one bidder left a few items blank.
    Together they describe Village Estates Krum, a developer's `Contractor
    Proposal Tab` whose eight respondents list 8, 8, 11, 11, 17, 19, 22 and 56
    items with no quantity anywhere: eight different proposed scopes, not eight
    bids on one scope.

    Measured first on the single-intersection Jaccard, which put Krum at 0.000
    -- alongside two files that turned out to be a real construction letting
    (mesquite/2022-067, where seven genuine contractors all price item 58
    `storm drain manhole 8 x 8`) and one broken parse whose bidder cells hold
    title text.  One bad column collapses an all-bidders intersection to zero,
    so that metric could not tell the three apart and is not the rule.
    """
    per = collections.Counter()
    anyq = False
    for r in rows:
        b = r.get("bidder") or ""
        if not b:
            continue
        per[b] += 1
        try:
            if float(r["quantity"]) > 0:
                anyq = True
        except (TypeError, ValueError):
            pass
    if anyq or len(per) < 2:
        return True
    c = sorted(per.values())
    return not (c[0] > 0 and c[-1] / c[0] > UNEVEN_SPREAD)


def winner_of_letting(rows, declared=None):
    """rows: dicts with bidder, item_no/desc_norm/unit (identity), quantity,
    unit_price.  Engineer-estimate and non-bidder rows must already be gone.

    declared: {bidder: rank} read off the document by bidder_identity.rank_of.
    Where the owner printed a column as the low bid, that is the owner stating
    who won, and it outranks anything summed from the schedule -- including a
    schedule too thin for arithmetic to decide.

    Returns a dict that always states its own basis -- never a bare name.
    """
    per = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        try:
            q = float(r["quantity"])
            u = float(r["unit_price"])
        except (TypeError, ValueError):
            continue
        if q <= 0 or u <= 0:
            continue
        b = r.get("bidder") or ""
        if not b:
            continue
        ik = (r.get("item_no") or "", r.get("desc_norm") or "",
              r.get("unit") or "")
        # += , not = : a schedule legitimately repeats an item number across
        # sections, and overwriting would drop one of them.
        per[b][ik] += q * u

    out = {"winner": None, "basis": "", "n_bidders": len(per),
           "n_modal_items": 0, "margin": None, "n_complete": 0,
           "item_coverage": None, "dropped_share": None, "totals": {},
           "raw_totals": {}}
    out["raw_totals"] = {b: _dsum(v) for b, v in per.items()}

    # The owner's own label first.  Only an unambiguous single rank-1 column
    # counts: two columns both labelled "low bidder" is a parse, not a fact.
    if declared:
        firsts = [b for b in per if declared.get(b) == 1]
        if len(firsts) == 1:
            out["winner"] = firsts[0]
            out["basis"] = "owner_declared"
            out["declared_ranks"] = {b: declared[b] for b in per
                                     if declared.get(b)}
            return out

    if len(per) < 2:
        out["basis"] = "too_few_bidders"
        return out

    # U: the modal schedule.  An item one bidder alone quoted is far more often
    # that bidder's column bleeding into a neighbour than a real pay item the
    # others all declined to price.  With only two bidders there is no majority
    # to appeal to, so both must have quoted it.
    seen = collections.Counter()
    for v in per.values():
        for k in v:
            seen[k] += 1
    need = max(2, (len(per) + 1) // 2)
    U = {k for k, c in seen.items() if c >= need}
    out["n_modal_items"] = len(U)
    if not U:
        out["basis"] = "no_modal_schedule"
        return out

    tot = {b: sum(x for k, x in v.items() if k in U) for b, v in per.items()}
    out["totals"] = tot

    # The dollar guard, before any ranking: how much of a bidder's own bid did
    # restricting to U discard?
    raw = out["raw_totals"]
    drop = {b: (raw[b] - tot[b]) / raw[b] for b in per if raw[b] > 0}
    out["dropped_share"] = max(drop.values()) if drop else None
    if out["dropped_share"] is not None and out["dropped_share"] > DROP_MAX:
        out["basis"] = "schedule_disagreement"
        return out

    cov = {b: len(set(v) & U) / len(U) for b, v in per.items()}
    complete = [b for b in per if cov[b] >= 1.0]
    out["n_complete"] = len(complete)
    out["item_coverage"] = min(cov.values())
    if not complete:
        out["basis"] = "no_complete_schedule"
        return out

    # (a) the cheapest EXACT total
    lo = min(complete, key=lambda b: tot[b])
    # (b) every rival's lower bound
    rivals = sorted((tot[b], b) for b in per if b != lo)
    gap = rivals[0][0] - tot[lo]
    out["margin"] = gap / tot[lo] if tot[lo] > 0 else None
    if gap <= TIE_REL * max(tot[lo], 1.0):
        # Either a true tie, or a rival whose partial total already sits at or
        # below the cheapest complete bid -- in both cases the tab does not
        # settle who won.
        out["basis"] = ("tie" if cov[rivals[0][1]] >= 1.0
                        else "rival_lower_bound_below")
        return out

    out["winner"] = lo
    out["basis"] = ("full_schedule" if out["n_complete"] == len(per)
                    else "modal_schedule")
    return out


# --------------------------------------------------------------------------
# THE DOCUMENT, NOT JUST THE OTHER BIDDERS.  winner_of_letting proves a winner
# against the bidders that were PARSED; a parse that lost the same items for
# every bidder passes that proof with a fraction of the job.  The 2026-11-19
# independent check found four such winners: Kee Branch (5 of 24 items read,
# 'won' at $25,410 against $875,000 printed), Cedar Hill PW18-0020 (30 of 80),
# Addison Surveyor (2 items, and the wrong firm), OCR Capricorn (2 of 17).  So
# a winner also has to be shown complete against the document itself, whatever
# its text came from (text layer, workbook or OCR): the winner's total is one
# of the TOTAL amounts the document prints, or the busiest bidder's column
# holds at least COVER_MIN of the distinct item lines the document prints
# (parse_bidtabs.document_facts, data/clean/doc_facts.csv).  Neither: the
# letting is `partial_schedule` -- no winner, so nothing scores it.  A
# document whose text shows fewer than DOC_MIN_ITEMS item lines (no item
# numbers, or a summary page) cannot be counted against, and is let through as
# `unverifiable`; a file without facts at all is not gated.
#
# GATED SINCE 2026-11-20 (tail audit, reports/tail_audit_2026-11-20.md): a
# letting whose items are all read but whose winner's total is not among the
# amounts the document prints on its TOTAL lines.  The 2026-11-19 count was
# 157 decided lettings, left ungated because most were a few tenths of a
# percent off; the audit of the extreme tails then found that all 5 tail
# lettings marked covers_document were parse errors (Celina Ousley Park,
# Frisco 3rd & Maple and Research Rd, Mesquite 24-7401, Southlake TW King:
# alternates or an extra-work item summed in, item 1 or seven items lost), and
# the Cedar Hill Complete Streets 2A wrong winner above is one of the 157 --
# SYB sums lowest at $3,540,098.54 over 147 rows each while the tab prints
# TOTAL BASE BID $3,613,710.17 for Axis and $3,628,082.94 for SYB.  Item
# counts cannot see a schedule that has the right number of lines and the
# wrong lines.
#
# And a match must be the bidder's OWN total, not any amount on the page.
# Dallas County's Riverfront Blvd passed as printed_total because Harper's
# sum ($103,991,724.50, alternates summed in) fell within 0.05% of RAGLE's
# printed total ($103,984,035.18); Harper's own was $99,196,865.85.  The
# document does not say which column an amount sits under, so the proof is
# that the winner's total is printed TO THE DOLLAR, or -- for a match that is
# only near -- that the RUNNER-UP's total is printed too, at a different
# amount: a parse that is right prints every bidder's total, and two
# coincidences in one letting are not plausible.  A bidder's total may be its base bid or its
# base plus every section it priced (a 'TOTAL BASE BID' that includes the
# alternates, as Dallas County's Rowlett Rd prints).
#
# So, where the document prints its bidders' totals (two or more amounts on
# TOTAL lines): the winner's and the runner-up's totals both printed ->
# printed_total; anything else -> total_mismatch, GATED.  Where it does not,
# as before: a printed amount (a recommendation letter's award) proves the
# winner, else item coverage decides (covers_document / partial).
COVER_MIN = 0.8
DOC_MIN_ITEMS = 3
TOTAL_TOL = 0.0005          # of the printed amount, never under one dollar


# the statuses that remove the winner (and with it every letting-level use)
GATED = ("partial", "total_mismatch", "unit_price_agreement", "on_call_quantities")


def completeness(winner_totals, items_read, doc, runner_up_totals=None):
    """(status, coverage) for a decided letting.  doc: {'items': n,
    'totals': [amounts printed off the item lines, dollars],
    'total_lines': [amounts on TOTAL lines]} or None.  winner_totals /
    runner_up_totals: the candidate totals of the winner and of the
    second-lowest bidder.  status is 'printed_total', 'covers_document',
    'unverifiable', 'no_document_facts', or one of GATED ('partial',
    'total_mismatch')."""
    if doc is None:
        return "no_document_facts", None
    n = doc.get("items") or 0
    cov = (items_read / n) if n >= DOC_MIN_ITEMS else None
    printed = list(doc.get("totals") or ()) + list(doc.get("total_lines") or ())

    def near(t, p):
        return abs(t - p) <= max(1.0, TOTAL_TOL * p)

    def hit(ts, avoid=None):
        for t in ts or ():
            if t and t > 0:
                for p in printed:
                    if near(t, p) and (avoid is None or not near(avoid, p)):
                        return p
        return None
    pw = hit(winner_totals)
    if len(set(doc.get("total_lines") or ())) >= 2:
        # the document prints its bidders' totals: each must be its own.  To
        # the dollar is proof on its own; only NEAR (within TOTAL_TOL, as
        # Harper's $7,689 off Ragle's printed total) needs the runner-up's
        # total printed too, at a different amount.
        exact = pw is not None and any(t and abs(t - pw) <= 1.0 for t in winner_totals)
        if pw is not None and (exact or runner_up_totals is None
                               or hit(runner_up_totals, avoid=pw) is not None):
            return "printed_total", cov
        return "total_mismatch", cov
    if pw is not None:
        return "printed_total", cov
    if cov is None:
        return "unverifiable", None
    return ("covers_document" if cov >= COVER_MIN else "partial"), cov


# ---- a letting whose total is not the price of a project -------------------
# Its unit prices are real bids; its sum is not a job, so it must not be
# scored or calibrated as a letting (tail audit 2026-11-20).  Two kinds:
#
#  * a UNIT-PRICE AGREEMENT: every quantity 1 on measured units.  Coppell
#    Q-0325-02 (pavement panel replacement) prints 1 LF / 1 SY / 1 CY on every
#    line and the council awarded a not-to-exceed $2.5M; its 'total' ($11,504
#    low) is a sum of unit prices.  Detected conservatively: at least
#    UPA_MIN_ITEMS items, at least UPA_ONE_SHARE of them at quantity 1, and at
#    least half of those on a measured unit (not LS / EA / allowance), so a
#    lump-sum pump station (8 lines, mostly 1 LS) is not one.
#  * ON-CALL / annual / as-needed contracts with notional quantities (Frisco
#    2025 On-Call Concrete Repair, tiered quantity bands): the title says so
#    (parse_bidtabs.contract_hint, or the file name), or the owner's record
#    does (NOT_A_PROJECT_TOTAL: NRH 6347 prints only 'UTILITY STREET CUT &
#    CONCRETE REPAIRS'; council item 6347 awards two non-exclusive annual
#    on-call contracts, NTE $350,000 a year each).
# The existing gate does the rest: no winner, so no is_low_bidder, so nothing
# letting-level reads it (municipal_letting_totals, the back-tests), while
# every unit price stays in the all-bids pool.
UPA_MIN_ITEMS = 6
UPA_ONE_SHARE = 0.9
_UPA_LUMP_UNITS = {"", "LS", "EA", "ALLOW", "ALW", "LOT", "JOB", "AL", "LSUM"}
ON_CALL_RE = re.compile(
    r"\bon[\s-]?call\b|\bindefinite[\s-]+(?:delivery|quantit)|"
    r"\b(?:annual|term)\s+(?:contract|agreement|requirements?)\b|"
    r"\bunit[\s-]+price\s+(?:contract|agreement)\b|\bprice\s+agreement\b|"
    r"\brequirements?\s+contract\b", re.I)       # = parse_bidtabs.CONTRACT_HINT_RE
NOT_A_PROJECT_TOTAL = {
    "external/legistar/nrhtx/2025-01-07_6347_BId_Tab.pdf":
        ("on_call_quantities", "NRH council item 6347: two non-exclusive annual on-call "
                               "street-cut repair contracts, NTE $350,000/yr each"),
}


def unit_price_agreement(rows):
    """True for a schedule of measured items all at quantity 1 (see above).
    rows: dicts with item_no, desc_norm, unit, quantity."""
    q = collections.defaultdict(list)
    unit = {}
    for r in rows:
        k = (r.get("item_no") or "", r.get("desc_norm") or "")
        try:
            v = float(r.get("quantity"))
        except (TypeError, ValueError):
            continue
        if v != v:
            continue
        q[k].append(v)
        unit[k] = (r.get("unit") or "").upper()
    if len(q) < UPA_MIN_ITEMS:
        return False
    ones = [k for k, v in q.items() if sorted(v)[len(v) // 2] == 1.0]
    if len(ones) < UPA_ONE_SHARE * len(q):
        return False
    measured = [k for k in ones if unit.get(k, "") not in _UPA_LUMP_UNITS]
    return len(measured) >= 0.5 * len(ones)


def not_a_project_total(src_file, rows, doc):
    """'unit_price_agreement', 'on_call_quantities' or '' for a letting."""
    f = (src_file or "").replace("\\", "/")
    if f in NOT_A_PROJECT_TOTAL:
        return NOT_A_PROJECT_TOTAL[f][0]
    if (doc or {}).get("contract_hint") == "on_call" or ON_CALL_RE.search(
            re.sub(r"[_]+", " ", os.path.basename(f))):
        return "on_call_quantities"
    if unit_price_agreement(rows):
        return "unit_price_agreement"
    return ""


def _r(b, ino, q, u, f="f", sh="s", d=None):
    return {"src_file": f, "src_sheet": sh, "bidder": b, "item_no": ino,
            "desc_norm": d if d is not None else "item" + str(ino),
            "unit": "LF", "quantity": q, "unit_price": u}


def _selftest():
    ok = True

    def chk(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print("FAIL %-36s got %r want %r" % (name, got, want))
        else:
            print("pass %-36s %r" % (name, got))

    # 1 even schedule, clean win
    rs = [_r("A", 1, 10, 5), _r("A", 2, 10, 5),
          _r("B", 1, 10, 9), _r("B", 2, 10, 9),
          _r("C", 1, 10, 7), _r("C", 2, 10, 7)]
    w = winner_of_letting(rs)
    chk("1 clean winner", (w["winner"], w["basis"]), ("A", "full_schedule"))

    # 2 a rival's PARTIAL total already sits below the cheapest complete bid,
    #   so what it did not quote could still win it -- must refuse.  B quoted
    #   only item 1, for 90, against A's complete 100.
    rs2 = [r for r in rs if not (r["bidder"] == "B" and r["item_no"] == 2)]
    w = winner_of_letting(rs2)
    chk("2 rival lower bound below", (w["winner"], w["basis"]),
        (None, "rival_lower_bound_below"))

    # 2b same shape, but the partial rival is already dearer than the winner's
    #    whole bid, so its missing items cannot save it -- decidable
    rs2b = [_r("A", 1, 10, 5), _r("A", 2, 10, 5),
            _r("B", 1, 10, 30),
            _r("C", 1, 10, 7), _r("C", 2, 10, 7)]
    w = winner_of_letting(rs2b)
    chk("2b partial rival cannot catch up", (w["winner"], w["basis"]),
        ("A", "modal_schedule"))

    # 3 the item only one bidder quoted carries almost all of that bidder's
    #   money.  Dropping it to form U would rank A ($50) under B ($60) while
    #   A's real bid is $500,050 -- the dollar guard must refuse.
    rs3 = [_r("A", 1, 10, 5), _r("B", 1, 10, 6),
           _r("A", 2, 1000, 500), _r("B", 3, 1, 1)]
    w = winner_of_letting(rs3)
    chk("3 refuses on schedule disagreement", w["basis"],
        "schedule_disagreement")
    chk("3 names nobody", w["winner"], None)
    chk("3 reports how much was at stake",
        round(w["dropped_share"], 4), 0.9999)

    # 4 exact tie
    w = winner_of_letting([_r("A", 1, 10, 5), _r("B", 1, 10, 5)])
    chk("4 tie", (w["winner"], w["basis"]), (None, "tie"))

    # 5 a lone bidder is not a competition
    w = winner_of_letting([_r("A", 1, 10, 5)])
    chk("5 single bidder", w["basis"], "too_few_bidders")

    # 6 a repeated item number must SUM, not overwrite
    w = winner_of_letting([_r("A", 1, 10, 5), _r("A", 1, 10, 5),
                           _r("B", 1, 10, 7), _r("B", 1, 10, 7)])
    chk("6 repeated item summed", w["raw_totals"]["A"], 100.0)

    # 7 the lower-envelope trap.  A is cheap on item 1, B on item 2, and they
    #    tie on the total.  The envelope (10*5 + 10*5 = 100) is below BOTH
    #    bidders' totals (140 each).  A winner, if named, must never total
    #    below the envelope -- that would mean we went back to min-per-item.
    rs7 = [_r("A", 1, 10, 5), _r("A", 2, 10, 9),
           _r("B", 1, 10, 9), _r("B", 2, 10, 5)]
    w = winner_of_letting(rs7)
    env = 10 * 5 + 10 * 5
    chk("7 tie, not an envelope", (w["basis"], w["winner"]), ("tie", None))
    chk("7 both totals above envelope",
        all(v > env for v in w["raw_totals"].values()), True)

    # 7b and when one bidder IS cheaper overall, the named winner's own total
    #     still sits above the envelope
    rs7b = rs7 + [_r("A", 3, 10, 1), _r("B", 3, 10, 50)]
    w = winner_of_letting(rs7b)
    env_b = 10 * 5 + 10 * 5 + 10 * 1
    chk("7b winner above envelope",
        (w["winner"], w["raw_totals"][w["winner"]] > env_b), ("A", True))

    # 13 a UNIFORM restriction -- the same item gone from every bidder, which
    #    is what keeping base sections only does -- must still decide, and must
    #    name the same winner.  A DIFFERENTIAL one must not (cases 2, 2b).
    full = [_r("A", 1, 10, 5), _r("A", 2, 10, 4), _r("A", 3, 10, 3),
            _r("B", 1, 10, 9), _r("B", 2, 10, 7), _r("B", 3, 10, 8),
            _r("C", 1, 10, 7), _r("C", 2, 10, 6), _r("C", 3, 10, 10)]
    w_full = winner_of_letting(full)
    w_base = winner_of_letting([r for r in full if r["item_no"] != 3])
    chk("13 uniform restriction still decides",
        (w_full["basis"], w_base["basis"]), ("full_schedule", "full_schedule"))
    chk("13 same winner either way",
        (w_full["winner"], w_base["winner"]), ("A", "A"))
    #    and it is a real restriction, not a no-op: the ratio does move, which
    #    is why the report measures the size of it instead of claiming it away
    def _ratio(w):
        v = sorted(w["totals"].values())
        return w["totals"][w["winner"]] / v[len(v) // 2]
    chk("13 ratio moves, so it is measured not assumed",
        round(_ratio(w_full), 4) != round(_ratio(w_base), 4), True)

    # 8 grouping: one letting printed as two page groups of disjoint bidders
    #   over the same schedule -> must FUSE (under-join is the silent error)
    g = group_sheets_into_lettings(
        [_r("A", 1, 10, 5, sh="p1"), _r("B", 1, 10, 9, sh="p1"),
         _r("C", 1, 10, 7, sh="p2"), _r("D", 1, 10, 8, sh="p2")])
    chk("8 page groups fused", len(set(g.values())), 1)

    # 9 grouping: two different lettings in one packet -> must stay apart
    g = group_sheets_into_lettings(
        [_r("A", 1, 10, 5, sh="p1", d="pipe"),
         _r("B", 1, 10, 9, sh="p1", d="pipe"),
         _r("C", 1, 10, 7, sh="p9", d="trail"),
         _r("D", 1, 10, 8, sh="p9", d="trail")])
    chk("9 distinct lettings kept apart", len(set(g.values())), 2)

    # 10 grouping: same bidders, continued item numbers -> fuse on 2 shared
    g = group_sheets_into_lettings(
        [_r("A", 1, 10, 5, sh="p1"), _r("B", 1, 10, 9, sh="p1"),
         _r("A", 24, 10, 5, sh="p2"), _r("B", 24, 10, 9, sh="p2")])
    chk("10 continued items fused", len(set(g.values())), 1)

    # 11 grouping never crosses files
    g = group_sheets_into_lettings(
        [_r("A", 1, 10, 5, f="x"), _r("B", 1, 10, 9, f="x"),
         _r("A", 1, 10, 5, f="y"), _r("B", 1, 10, 9, f="y")])
    chk("11 files never fused", len(set(g.values())), 2)

    # 12 one shared bidder alone must NOT fuse: a contractor bidding two
    #    projects in one packet is ordinary
    g = group_sheets_into_lettings(
        [_r("A", 1, 10, 5, sh="p1", d="pipe"),
         _r("B", 1, 10, 9, sh="p1", d="pipe"),
         _r("A", 1, 10, 7, sh="p9", d="trail"),
         _r("C", 1, 10, 8, sh="p9", d="trail")])
    chk("12 one shared bidder not enough", len(set(g.values())), 2)

    # 14 completeness against the document (the 2026-11-19 second round)
    doc = {"items": 24, "totals": [875000.00, 892540.00]}
    chk("14a printed total proves it", completeness([875000.40], 5, doc)[0], "printed_total")
    chk("14b five of 24 items is partial", completeness([25410.0], 5, doc),
        ("partial", 5 / 24))
    chk("14c 23 of 24 items covers it", completeness([800000.0], 23, doc)[0],
        "covers_document")
    chk("14d two item lines cannot be counted against",
        completeness([100.0], 2, {"items": 2, "totals": []}), ("unverifiable", None))
    chk("14e no facts, no gate", completeness([100.0], 2, None), ("no_document_facts", None))
    chk("14f all items read, printed totals other numbers: gated since 2026-11-20",
        completeness([3540098.54], 147, {"items": 150, "totals": [3613710.17, 3628082.94],
                                         "total_lines": [3613710.17, 3628082.94]})[0],
        "total_mismatch")
    # 14h Riverfront Blvd: the winner's sum sits on the RUNNER-UP's printed
    #     total and the runner-up's on nothing -- not proof, gated
    rf = {"items": 300, "totals": [99196865.85, 103984035.18, 99979953.78, 105272603.97],
          "total_lines": [99196865.85, 103984035.18, 99979953.78, 105272603.97]}
    chk("14h a total that is another bidder's is not proof",
        completeness([103991724.50], 290, rf, [109640704.53])[0], "total_mismatch")
    chk("14i both own totals printed proves it",
        completeness([99196865.85], 290, rf, [103984035.18])[0], "printed_total")
    chk("14j runner-up on the winner's amount is not a second proof",
        completeness([99196600.00], 290, rf, [99196800.00])[0], "total_mismatch")
    chk("14j2 the winner's own total to the dollar is proof alone",
        completeness([99196865.85], 290, rf, [101000000.00])[0], "printed_total")
    chk("14k base + alternates printed as the total counts",
        completeness([1.0, 99979953.78], 290, rf, [105272603.97])[0], "printed_total")
    chk("14l no TOTAL lines: an award amount alone still proves it",
        completeness([672774.0], 20, {"items": 21, "totals": [672774.0], "total_lines": []},
                     [700000.0])[0], "printed_total")
    chk("14m no TOTAL lines, nothing printed: coverage decides as before",
        completeness([500.0], 20, {"items": 21, "totals": [], "total_lines": [672774.0]},
                     [700000.0])[0], "covers_document")
    # 15 not a project total
    upa = [{"item_no": str(i), "desc_norm": "d%d" % i, "unit": u, "quantity": 1}
           for i, u in enumerate(["SY", "LF", "CY", "SY", "LF", "EA", "SY", "LF"])]
    chk("15a quantity 1 on measured units is a unit-price agreement",
        unit_price_agreement(upa), True)
    pump = [{"item_no": "A-%02d" % i, "desc_norm": "p%d" % i, "unit": u, "quantity": q}
            for i, (u, q) in enumerate([("LS", 1), ("LF", 144), ("LS", 1), ("LS", 1),
                                        ("EA", 4), ("LS", 1), ("LS", 1), ("LS", 1)])]
    chk("15b a lump-sum pump station is not", unit_price_agreement(pump), False)
    lumps = [{"item_no": str(i), "desc_norm": "l%d" % i, "unit": "LS", "quantity": 1}
             for i in range(10)]
    chk("15c all lump sums is a lump-sum job, not an agreement", unit_price_agreement(lumps), False)
    chk("15d five items is too few to say", unit_price_agreement(upa[:5]), False)
    chk("15e on-call by file name",
        not_a_project_total("external/frisco/2025-08-21_Bid_2506-096_On_Call_Concrete_Repair_Bid_Tabulation.pdf",
                            pump, {}), "on_call_quantities")
    chk("15f on-call by the document's title", not_a_project_total("x/y.pdf", pump,
                                                                  {"contract_hint": "on_call"}),
        "on_call_quantities")
    chk("15g a project is none of these", not_a_project_total("x/y.pdf", pump, {}), "")
    chk("14g a TOTAL-line amount alone proves it",
        completeness([3613710.17], 30, {"items": 150, "totals": [],
                                        "total_lines": [3613710.17]})[0], "printed_total")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return ok


def main():
    """Derive the winner for every municipal letting and report, per letting,
    the basis on which it was or was not decided.  Writes
    data/clean/municipal_award.csv -- one row per letting, never a bare name.
    """
    import csv
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bidder_identity
    csv.field_size_limit(10 ** 8)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    clean = os.path.join(root, "data", "clean")

    raw = []
    rejected = collections.Counter()
    # rows READ per sheet and bidder, every section and every row with a price
    # (keep_row not applied): the completeness gate counts what the parse
    # holds against what the document prints, alternates included
    read_rows = collections.defaultdict(collections.Counter)
    # ... and the dollars READ per sheet and bidder over every section, so a
    # bidder's total can be its base plus what it priced besides (completeness)
    read_amt = collections.defaultdict(lambda: collections.defaultdict(float))
    with open(os.path.join(clean, "line_items.csv"), encoding="utf-8-sig",
              newline="") as f:
        for r in csv.DictReader(f):
            if (r["is_engineer_estimate"] or "").lower() in ("true", "1", "yes"):
                rejected["engineer_estimate"] += 1
                continue
            try:
                priced = float(r.get("unit_price") or 0) > 0
            except ValueError:
                priced = False
            bk = bidder_identity.key_of(r.get("bidder") or "") if priced else ""
            if bk:
                read_rows[(r["src_file"], r["src_sheet"])][bk] += 1
                try:
                    e = float(r.get("extended_reported") or "nan")
                except ValueError:
                    e = float("nan")
                if not (e == e):
                    try:
                        e = float(r.get("quantity") or "nan") * float(r["unit_price"])
                    except (TypeError, ValueError):
                        e = float("nan")
                if e == e and r.get("ext_mismatch") != "Y":
                    read_amt[(r["src_file"], r["src_sheet"])][bk] += e
            if not keep_row(r, rejected):
                continue
            raw.append(r)
    # what each document itself holds (parse_bidtabs.document_facts)
    facts = None
    fp = os.path.join(clean, "doc_facts.csv")
    if os.path.exists(fp):
        facts = {}
        with open(fp, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                facts[r["src_file"]] = {
                    "items": int(r.get("doc_items") or 0),
                    "totals": [float(x) for x in (r.get("printed_totals") or "").split()],
                    "total_lines": [float(x) for x in
                                    (r.get("total_line_amounts") or "").split()],
                    "contract_hint": r.get("contract_hint") or ""}
    else:
        print("!! %s is missing -- the completeness gate is OFF for this run" % fp)
    print("rows rejected before any comparison:", dict(rejected.most_common()))
    # One canonical name per real contractor, or the same company spelt two
    # ways becomes two bidders and the modal schedule falls apart.
    cmap = bidder_identity.build_map([r["bidder"] for r in raw])
    rows = []
    drop_ph = 0
    # (src_file, src_sheet) -> {canonical bidder: rank the document asserts}
    declared = collections.defaultdict(dict)
    for r in raw:
        if bidder_identity.is_placeholder(r["bidder"]):
            drop_ph += 1
            continue
        b = cmap.get(r["bidder"], r["bidder"])
        if not b:
            drop_ph += 1
            continue
        rk = bidder_identity.rank_of(r["bidder"])
        if rk:
            declared[(r["src_file"], r["src_sheet"])][b] = rk
        r["bidder"] = b
        rows.append(r)
    print("competitive municipal rows: %d  (dropped %d placeholder/blank)"
          % (len(rows), drop_ph))

    # Reconcile against the panel: if keep_row has drifted from load_city, the
    # award derivation is answering about a different population than the one
    # that trains the model, and every number below is quietly off.
    pp = os.path.join(clean, "master_panel.csv")
    if os.path.exists(pp):
        pn = 0
        with open(pp, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r["source"] == "city_bidtab" and \
                        (r["is_engineer_estimate"] or "").lower() not in (
                            "true", "1", "yes"):
                    pn += 1
        gap = len(raw) - pn
        print("panel city_bidtab non-EE rows: %d   this derivation: %d   "
              "difference: %+d%s" % (pn, len(raw), gap,
                                     "" if abs(gap) <= 0.01 * max(pn, 1)
                                     else "   <-- FILTERS HAVE DRIFTED"))
        if abs(gap) > 0.01 * max(pn, 1):
            # This step runs BEFORE build_master, so the panel it just read is
            # the PREVIOUS run's.  After a parser change the first pass will
            # always disagree, and that is not drift.  Run build_master and
            # then this again; a gap that survives the second pass is real.
            print("   (this runs before build_master, so the panel above is "
                  "from the previous pass -- re-run this step after "
                  "build_master before believing the gap)")

    gid = group_sheets_into_lettings(rows)
    lets = collections.defaultdict(list)
    for r in rows:
        lets[gid[(r["src_file"], r["src_sheet"])]].append(r)
    print("sheets: %d  ->  lettings: %d" % (len(gid), len(lets)))

    # A letting's declared ranks are the union over its sheets: the label is
    # printed once per bidder column, on whichever sheet that column fell on.
    decl = collections.defaultdict(dict)
    for (f, sh), d in declared.items():
        if (f, sh) in gid:
            decl[gid[(f, sh)]].update(d)

    res = {}
    basis = collections.Counter()
    gate = collections.Counter()
    for k, rs in lets.items():
        w = winner_of_letting(rs, decl.get(k))
        w.update({"completeness": "", "doc_coverage": None, "items_read": None,
                  "doc_items": None})
        # a letting whose total is not a project (not_a_project_total)
        npt = not_a_project_total(k[0], rs, (facts or {}).get(k[0])) if w["winner"] else ""
        if npt:
            w.update({"completeness": npt, "winner": None, "basis": "not_a_project_total"})
            gate[npt] += 1
        if w["winner"] and facts is not None:
            per = collections.Counter()
            for sk in {(r["src_file"], r["src_sheet"]) for r in rs}:
                per.update(read_rows.get(sk, {}))
            items_read = max(per.values()) if per else 0
            # the winner's total as the document would print it: the printed
            # extensions where they exist, quantity x price otherwise
            ext_tot = 0.0
            for r in rs:
                if r["bidder"] != w["winner"]:
                    continue
                try:
                    e = float(r.get("extended_reported") or "nan")
                except ValueError:
                    e = float("nan")
                if e == e and e > 0:
                    ext_tot += e
                else:
                    try:
                        ext_tot += float(r["quantity"]) * float(r["unit_price"])
                    except (TypeError, ValueError):
                        pass
            doc = facts.get(k[0])
            # every bidder's dollars over every section of the letting's sheets
            allsec = collections.defaultdict(float)
            for sk in {(r["src_file"], r["src_sheet"]) for r in rs}:
                for bk, v in read_amt.get(sk, {}).items():
                    allsec[bk] += v
            # the runner-up: the second-lowest total the winner was proved against
            tt = w["totals"] or w["raw_totals"]
            ru = sorted((v, b) for b, v in tt.items() if b != w["winner"])
            ru_tot = None
            if ru:
                rb = ru[0][1]
                ru_ext = 0.0
                for r in rs:
                    if r["bidder"] != rb:
                        continue
                    try:
                        e = float(r.get("extended_reported") or "nan")
                    except ValueError:
                        e = float("nan")
                    if e == e and e > 0:
                        ru_ext += e
                    else:
                        try:
                            ru_ext += float(r["quantity"]) * float(r["unit_price"])
                        except (TypeError, ValueError):
                            pass
                ru_tot = [tt.get(rb), w["raw_totals"].get(rb), ru_ext,
                          allsec.get(bidder_identity.key_of(rb))]
            st, cov = completeness(
                [(w["totals"] or {}).get(w["winner"]), w["raw_totals"].get(w["winner"]),
                 ext_tot, allsec.get(bidder_identity.key_of(w["winner"]))], items_read, doc,
                ru_tot)
            w.update({"completeness": st, "doc_coverage": cov, "items_read": items_read,
                      "doc_items": (doc or {}).get("items")})
            gate[st] += 1
            if st in GATED:
                w["winner"] = None
                w["basis"] = "partial_schedule" if st == "partial" else st
        res[k] = w
        basis[w["basis"]] += 1
    if gate:
        print("completeness against the document, decided lettings:", dict(gate.most_common()))

    dec = sum(v for b, v in basis.items()
              if b in ("full_schedule", "modal_schedule"))
    print()
    print("%-26s %7s %s" % ("basis", "lettings", ""))
    for b, v in basis.most_common():
        print("  %-26s %5d  %5.1f%%" % (b, v, 100.0 * v / len(lets)))
    print("  %-26s %5d  %5.1f%%" % ("-- WINNER IDENTIFIED", dec,
                                    100.0 * dec / len(lets)))

    # dollars, not just lettings: a decided letting is worth more than a count
    won_rows = 0
    all_rows = 0
    for k, rs in lets.items():
        w = res[k]
        for r in rs:
            all_rows += 1
            if w["winner"] and r["bidder"] == w["winner"]:
                won_rows += 1
    print()
    print("priced rows belonging to an identified winner: %d of %d (%.1f%%)"
          % (won_rows, all_rows, 100.0 * won_rows / max(all_rows, 1)))

    out = os.path.join(clean, "municipal_award.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["letting_id", "src_file", "n_sheets", "n_bidders",
                     "n_modal_items", "n_complete", "item_coverage",
                     "dropped_share", "margin", "basis", "winner",
                     "winner_total", "second_total",
                     # the completeness gate (see completeness())
                     "completeness", "items_read", "doc_items", "doc_coverage"])
        for k in sorted(res):
            w = res[k]
            sheets = {r["src_sheet"] for r in lets[k]}
            tot = w["totals"] or w["raw_totals"]
            srt = sorted(tot.values())
            wr.writerow([
                "%s::%s" % (k[0], k[1]), k[0], len(sheets), w["n_bidders"],
                w["n_modal_items"], w["n_complete"],
                "" if w["item_coverage"] is None else round(w["item_coverage"], 4),
                "" if w["dropped_share"] is None else round(w["dropped_share"], 4),
                "" if w["margin"] is None else round(w["margin"], 6),
                w["basis"], w["winner"] or "",
                round(tot.get(w["winner"], 0.0), 2) if w["winner"] else "",
                round(srt[1], 2) if len(srt) > 1 else "",
                w.get("completeness") or "",
                "" if w.get("items_read") is None else w["items_read"],
                "" if w.get("doc_items") is None else w["doc_items"],
                "" if w.get("doc_coverage") is None else round(w["doc_coverage"], 4)])
    print("wrote", out)

    # The join table build_master uses: one row per sheet, so load_city can
    # merge on (src_file, src_sheet) and then compare its own canonical bidder
    # to `winner`.  Sheet-level, not row-level, keeps it small and makes the
    # letting reconstruction visible to anything downstream.
    sp = os.path.join(clean, "municipal_award_sheets.csv")
    with open(sp, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["src_file", "src_sheet", "letting_id", "award_basis",
                     "winner", "n_bidders", "n_modal_items", "margin",
                     "comparable"])
        ncomp = 0
        for (sf, sh), k in sorted(gid.items()):
            w = res.get(k)
            if not w:
                continue
            cmpble = is_comparable_letting(lets[k])
            ncomp += 0 if cmpble else 1
            wr.writerow([sf, sh, "%s::%s" % (k[0], k[1]), w["basis"],
                         w["winner"] or "", w["n_bidders"], w["n_modal_items"],
                         "" if w["margin"] is None else round(w["margin"], 6),
                         "true" if cmpble else "false"])
    print("wrote %s  (%d sheets marked not comparable)" % (sp, ncomp))

    # ---- letting totals, for the competition discount --------------------
    # The discount is low/median over the bidders of one letting.  TxDOT
    # publishes its own per-bidder totals, so that arm just reads them; the
    # municipal arm has to build them, and building them wrong is the same
    # partial-schedule trap as before.  A bidder whose column lost five line
    # items sums short, looks cheapest, drags the median down and distorts the
    # ratio -- so the totals written here are summed over the MODAL BASKET,
    # the one `winner_of_letting` already decided the winner on, and never over
    # whatever each bidder happened to have parsed.
    #
    # Only lettings with an identified winner are written.  Using min(totals)
    # where the derivation refused would put exactly the bias back: the refusal
    # usually means some bidder's partial total is the lowest number present.
    #
    # The base-section restriction (keep_row) drops the SAME items from every
    # bidder in a letting, since the alternates are offered to all of them.
    # That makes it an unbiased restriction, not a harmless one: it favours no
    # bidder, unlike a partial parse, which drops DIFFERENT items from
    # different bidders and hands the win to whoever lost the most.  It does
    # still move the measured ratio, by whatever the alternates' relative
    # pricing was, in an unsigned direction.  Self-test 13 pins the distinction;
    # the size of the effect is measured against the unrestricted totals in the
    # report rather than assumed.
    import statistics
    lp = os.path.join(clean, "municipal_letting_totals.csv")
    meta = {}
    for r in rows:
        k = gid[(r["src_file"], r["src_sheet"])]
        if k not in meta:
            meta[k] = (r.get("owner") or "", str(r.get("bid_date") or ""),
                       r.get("source_set") or "internal")
    nw = 0
    nint = 0
    ntest = 0
    with open(lp, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        # source_set travels WITH the totals.  The competition discount is the
        # one structural parameter the whole estimate rests on, so a company
        # file reaching it is a train/test leak however small its effect -- and
        # it was small, 7 of 285 lettings moving the median from 0.8592 to
        # 0.8587.  Size is not the test; those 7 lettings ARE the test set, and
        # a parameter fitted partly on them cannot be checked against them.
        # The file stays complete so the test side can be measured separately;
        # the consumer gates on this column and prints what it dropped.
        wr.writerow(["letting_id", "src_file", "source_set", "owner",
                     "bid_date", "year", "n_bidders", "winner", "low",
                     "median", "sigma_log", "award_basis", "n_modal_items",
                     "basket"])
        for k in sorted(res):
            w = res[k]
            if not w["winner"] or not is_comparable_letting(lets[k]):
                continue
            t = w["totals"]
            if w["winner"] not in t or len(t) < 2:
                continue
            vals = [v for v in t.values() if v > 0]
            if len(vals) < 2 or t[w["winner"]] <= 0:
                continue
            own, bd, sset = meta.get(k, ("", "", "internal"))
            # "internal" is the company's; "public_test" the locked public
            # test set (src/public_holdout.py).  Neither may train; they are
            # counted apart because they are different things.
            if sset == "internal":
                nint += 1
            elif sset != "external":
                ntest += 1
            wr.writerow([
                "%s::%s" % (k[0], k[1]), k[0], sset, own, bd, bd[:4],
                # the bidders actually in the total, which is what TxDOT's
                # len(bidder_totals) counts too -- not the panel's n_bidders,
                # or the two arms would be binned on different definitions
                len(vals), w["winner"], round(t[w["winner"]], 2),
                round(statistics.median(vals), 2),
                round(statistics.stdev([math.log(v) for v in vals]), 6)
                if len(vals) > 1 else "",
                w["basis"], w["n_modal_items"], "modal"])
            nw += 1
    print("wrote %s  (%d lettings with an identified winner, of which %d are "
          "company files and %d the locked public test set; neither may train "
          "anything)" % (lp, nw, nint, ntest))

    # ---- external anchor: the council minutes -----------------------------
    ap = os.path.join(clean, "award_reconciliation.csv")
    if os.path.exists(ap):
        anchor = {}
        with open(ap, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    anchor[r["src_file"].replace("\\", "/")] = float(
                        r["award_amount"])
                except (ValueError, TypeError, KeyError):
                    pass
        by_file = collections.defaultdict(float)
        for k, w in res.items():
            if w["winner"]:
                t = (w["totals"] or w["raw_totals"]).get(w["winner"], 0.0)
                by_file[k[0].replace("\\", "/")] += t
        hits = [(f, by_file[f], anchor[f]) for f in by_file if f in anchor]
        print()
        print("=== against the council-minute award amounts ===")
        print("lettings decided in a file that also has an award amount:",
              len(hits))
        close = [h for h in hits if h[2] > 0 and abs(h[1] / h[2] - 1) <= 0.005]
        print("within 0.5%% of the awarded amount: %d of %d" % (len(close),
                                                                len(hits)))
        for f, got, want in sorted(hits, key=lambda h: -abs(
                h[1] / h[2] - 1 if h[2] else 0))[:8]:
            print("   derived %14s  awarded %14s  ratio %6.3f  %s"
                  % ("{:,.0f}".format(got), "{:,.0f}".format(want),
                     got / want if want else 0, f[-54:]))
    return res


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    if not _selftest():
        print("\nself-tests failed -- not running on real data")
        sys.exit(1)
    print()
    main()
