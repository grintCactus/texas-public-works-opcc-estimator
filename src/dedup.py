"""One definition of "the same letting recorded twice".

This lives apart from parse_bidtabs.py so that build_master.py can apply the
SAME rule to the assembled panel.  A duplicate that reaches the panel is worse
than one that only reaches line_items.csv: the panel is what the competition
discount, the residual dispersion and every reference price are fitted on, and
duplicating a letting leaves the median roughly where it was while TIGHTENING
the spread.  An item then reads as having a stable price when the stability is
just one letting counted twice -- the damage lands on sigma and on the
competition discount, not on the median, which is why a median-only sanity
check will not reveal it.

WHY THIS MODULE EXISTS AT ALL -- the bug it replaces:

The previous cross-file pass built an inverted index over signatures and
skipped any signature appearing in more than 8 sheets, on the correct ground
that a signature shared by many sheets is generic rather than evidence of a
duplicate.  But it then used the resulting PER-PAIR COUNTER as the measured
intersection.  The cap was designed to bound candidate GENERATION and was
silently also bounding the MEASUREMENT, so every generic signature a duplicate
pair happened to share pushed its coverage down.

Measured on the live corpus, farmersbranch R2025-072 and R2025-109 share seven
sheets whose exact overlap is 1.000 -- identical priced content -- and six of
the seven scored 0.786-0.899 under the capped count and so fell under the 0.90
threshold:

    sheet      capped  exact   cov_capped  cov_exact
    p4t1          143    161        0.888      1.000
    p5t0          124    144        0.861      1.000
    p2t1          110    130        0.846      1.000
    p2t0           98    109        0.899      1.000
    p4t0           88    103        0.854      1.000
    p6t0           44     56        0.786      1.000

819 rows of one 3.2M-dollar letting were counted twice.  The rule here keeps
the cap for candidate generation ONLY and always recomputes the exact
intersection from the full signature sets before judging a pair.
"""

import re
from collections import Counter, defaultdict

# Deliberately EXCLUDES the description: a republished tab is often re-typed or
# re-extracted, so wording drifts while the priced content is identical.
# (bidder, unit, quantity, price) is the real identity of a priced row.
SIG_FIELDS = ("bidder", "unit", "quantity", "unit_price")

SIG_FIELDS_ITEM = ("unit", "quantity", "unit_price")

# The full signature alone is enough at this level.
STRONG_COVER = 0.90
# Below STRONG_COVER the bidder-free signature must be this close AND the owner
# must match.  Set high on purpose: dropping the bidder from the signature makes
# it weaker, so the bar it has to clear is raised to compensate.
ITEM_COVER = 0.95
# Fewer signatures than this is not evidence either way -- a three-line sheet of
# round lump sums collides with anything.
MIN_SIGS = 6

# Bounds candidate generation only.  NEVER used as the measurement -- that
# conflation is the bug this module exists to fix.
INV_CAP = 40

_JUNK = re.compile(r"draft|copy|old|backup|working", re.I)
# A document that republishes a bid tab inside a larger package.  When two
# copies of one letting collide, the bid tabulation is the primary record made
# at letting time; the award package is a later re-publication.  "exhibit" is
# deliberately absent -- owners routinely attach the bid tab itself as
# "Exhibit A", so penalising it would demote a primary document.
_SECONDARY = re.compile(r"award|packet|recommendation|agenda|minutes|council", re.I)
_PRIMARY = re.compile(r"bid[_ -]?tab", re.I)


def _num(v, nd=2):
    """Canonical number, or None when absent or unreadable.

    Both halves of the canonicalisation are load-bearing.

    MISSING MUST BECOME None, not NaN.  NaN != NaN, so a row with no quantity
    can never match its own copy in another file -- and those rows are the badly
    laid-out ones, exactly where duplicates are hardest to spot by eye.  The
    frame path read quantities through pandas (missing -> NaN) while the row
    path read them through csv (missing -> ""), so the SAME rule scored the Fish
    Creek pair 0.969 and 1.000 on the same data, and 30 rows of one letting were
    kept or dropped by that difference alone.

    ROUNDING unifies "1" and "1.0", which is how a republished tab usually
    differs from its original.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        s = str(v).strip().replace(",", "")
        try:
            f = float(s)
        except ValueError:
            return str(v).strip() or None
    if f != f:                      # NaN
        return None
    return round(f, nd)


def sig_of(row):
    """Signature of one priced row.  `row` is a dict or anything .get()-able."""
    unit = row.get("unit")
    if unit is None or unit != unit:
        unit = ""
    bidder = row.get("bidder")
    if bidder is None or bidder != bidder:
        bidder = ""
    return (str(bidder), str(unit), _num(row.get("quantity"), 4),
            _num(row.get("unit_price"), 2))


def _is_company(src_file):
    """A path with no separator is a company-supplied file (see provenance.py)."""
    return 0 if "/" in (src_file or "").replace("\\", "/") else 1


def sheet_pref(key, rows):
    """Higher is better.  Decides which copy of a letting survives.

    The company flag comes first and is not a tie-break: the company's copy is
    held-out TEST data, so dropping it in favour of a public duplicate would
    quietly move that project into the training set -- the contamination the
    train/test split exists to prevent, reached from the other direction.
    """
    f, sh = key
    company = _is_company(f)
    sh = str(sh or "")
    f = f or ""
    penalty = (1 if _JUNK.search(sh) else 0) + (1 if _JUNK.search(f) else 0)
    # Document role: prefer the primary bid tabulation over an award package.
    role = (1 if _PRIMARY.search(f) else 0) - (1 if _SECONDARY.search(f) else 0)
    # A text-layer copy of a letting beats an OCR reading of the same letting:
    # OCR digits are the weaker evidence (src/parse_ocr_sidecars.py).
    ocr = 1 if (sh.startswith("ocr_") or any(r.get("text_source") == "ocr" for r in rows)) else 0
    return (company, -ocr, -penalty, role, len(rows),
            len(set(r.get("bidder") for r in rows)))


def same_letting(a, b, cover, cover_item, n_item, owner_of):
    """Is this pair two recordings of one letting, or two real observations?

    A RE-BID of the same project, and a revised tab that changed a few items,
    are legitimate independent observations and must not be deleted.

    At >= STRONG_COVER the full signature settles it on its own.  Below that it
    does not, so it must be corroborated -- and the corroborating field must not
    be `bid_date`.  Measured on the live corpus, the Fish Creek bid tabulation
    and its award package carry 2025-03-10 and 2025-03-27, from
    `weak_issue_or_received` and `bid_open_or_due_ambiguous_span`: a date guard
    fails precisely where the metadata is weakest, which is where duplicates are
    hardest to spot by eye.

    The corroborating measure is instead the BIDDER-FREE coverage, and the
    reason is a measured one.  Because the full signature carries the bidder
    name, ONE misparsed bidder header removes that bidder's entire row set from
    the intersection -- not a few rows, all of them.  In the Fish Creek award
    package `A T Construction, LLC` parsed as `Item 20. A T Construction, LLC`
    (the item number bled into the bidder header), which cost exactly that one
    bidder's 37 rows and dragged sheet coverage from 1.000 down to 0.805.  The
    bidder-free coverage of the same pair is 1.000 on 189 of 189 signatures: the
    award package contains the bid tab's priced content entire.  So a single
    header misparse must not be able to disguise a republication.

    The owner must still match, and there must be enough signatures to mean
    anything: (unit, quantity, price) alone collides easily on small sheets of
    round lump sums.  A different project cannot match on quantities as well as
    prices, and a genuine re-bid moves prices -- that is the point of re-bidding.
    """
    if n_item < MIN_SIGS:
        return False, ""
    if cover >= STRONG_COVER:
        return True, "content"
    if cover_item >= ITEM_COVER and owner_of(a) and owner_of(a) == owner_of(b):
        return True, "content_bidderfree+owner"
    return False, ""


def _candidates(index, cross_file_only):
    """Pairs sharing at least one signature, via an inverted index.

    A full O(sheet^2) sweep over a few thousand sheets is tens of millions of
    set intersections; shared signatures are rare enough that candidate pairs
    are few.  INV_CAP skips signatures so common that they are generic rather
    than evidence -- and it bounds THIS step only.
    """
    cand = set()
    for sg, ks in index.items():
        if len(ks) < 2 or len(ks) > INV_CAP:
            continue
        ks = sorted(ks)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = ks[i], ks[j]
                if cross_file_only and a[0] == b[0]:
                    continue
                cand.add((a, b))
    return cand


def find_duplicate_sheets(sigs, sigs_item, rows_of, owner_of,
                          cross_file_only=True):
    """Return (dropped, overlaps).

    sigs:      {key: set(full signature)}  key is normally (src_file, src_sheet)
    sigs_item: {key: set(bidder-free signature)}
    rows_of:   key -> list of rows
    owner_of:  key -> owner string
    """
    inv, inv_item = defaultdict(list), defaultdict(list)
    for k, s in sigs.items():
        for sg in s:
            inv[sg].append(k)
    for k, s in sigs_item.items():
        for sg in s:
            inv_item[sg].append(k)

    # Candidates come from BOTH indexes.  Generating from the full signature
    # alone would miss a republication whose every bidder header was re-typed:
    # its full-signature intersection can be empty, so the pair would never be
    # looked at.  Bidder-free signatures collide far more, but INV_CAP discards
    # exactly the ones that collide most, so the extra candidates stay bounded.
    cand = _candidates(inv, cross_file_only) | _candidates(inv_item,
                                                           cross_file_only)

    def measure(a, b):
        sa, sb = sigs.get(a, set()), sigs.get(b, set())
        ia, ib = sigs_item.get(a, set()), sigs_item.get(b, set())
        if not ia or not ib:
            return None
        # THE FIX: exact intersections from the full sets, never a counter.
        m = min(len(sa), len(sb))
        cover = (len(sa & sb) / m) if m else 0.0
        n_item = min(len(ia), len(ib))
        cover_item = len(ia & ib) / n_item
        return (cover, cover_item, n_item, len(sa & sb), a, b)

    by_file = defaultdict(list)
    for k in sigs:
        by_file[k[0]].append(k)

    dropped, overlaps = {}, []

    def judge(pairs):
        """Judge a batch of pairs, best overlap first.  Returns new file pairs."""
        order = [m for m in (measure(a, b) for a, b in pairs) if m]
        # Most-overlapping first, so that when three copies of one letting exist
        # the copy that is kept is chosen against its best rival.
        order.sort(key=lambda t: (-max(t[0], t[1]), -t[3], t[4], t[5]))
        hit = set()
        for cover, cover_item, n_item, inter, a, b in order:
            if a in dropped or b in dropped:
                continue
            ok, why = same_letting(a, b, cover, cover_item, n_item, owner_of)
            if not ok:
                continue
            pa = sheet_pref(a, rows_of(a))
            pb = sheet_pref(b, rows_of(b))
            winner, loser = (a, b) if pa >= pb else (b, a)
            dropped[loser] = {"kept_instead": winner[0] + "::" + str(winner[1]),
                              "overlap": round(cover, 3),
                              "overlap_bidderfree": round(cover_item, 3),
                              "basis": why,
                              "rows_dropped": len(rows_of(loser)),
                              "cross_file": True}
            overlaps.append({"file_a": a[0], "sheet_a": a[1],
                             "file_b": b[0], "sheet_b": b[1],
                             "shared_sigs": inter, "overlap": round(cover, 3),
                             "overlap_bidderfree": round(cover_item, 3),
                             "sigs_bidderfree": n_item, "basis": why,
                             "dropped": loser[0] + "::" + str(loser[1]),
                             "kept": winner[0] + "::" + str(winner[1])})
            hit.add(tuple(sorted((a[0], b[0]))))
        return hit

    implicated = judge(cand)

    # ---- escalate to an exhaustive comparison between implicated files ----
    # INV_CAP is legitimate for candidate GENERATION -- a signature shared by
    # dozens of sheets is generic.  But it still has a blind spot, and the blind
    # spot is measurable: reading quantities as numbers rather than strings makes
    # round values like ("LS", 1.0, 5000.0) collide corpus-wide, pushing them
    # over the cap, and the Fish Creek continuation sheet coords_p103 then never
    # became a candidate at all.  The string-typed run dropped it; the
    # numeric-typed run did not, from the same data -- 30 rows of one letting
    # kept or discarded by a detail of parsing.
    #
    # So once a file pair is known to share one duplicate sheet, stop trusting
    # the index for that pair and compare EVERY sheet of one against every sheet
    # of the other.  Implicated pairs are few and their sheets number in the
    # dozens, so this is cheap, and unlike a threshold it is not luck.
    seen_pairs = set()
    while implicated:
        batch = set()
        for fa, fb in implicated:
            if (fa, fb) in seen_pairs:
                continue
            seen_pairs.add((fa, fb))
            for a in by_file.get(fa, ()):
                for b in by_file.get(fb, ()):
                    batch.add((a, b) if a < b else (b, a))
        implicated = judge(batch) - seen_pairs if batch else set()

    # ---- the LEFTOVERS of a file already proven to repeat another ----------
    # Pairwise, a sheet is judged against one sheet at a time, and two things
    # hide a copy that way.  A short page (fewer than MIN_SIGS signatures) is
    # no evidence on its own: Mesquite's 22-6233 tab was published four times
    # (22-6242, 2022-138, 2022-139), every page-1 copy was dropped, and the
    # three 5-row page-2 copies all stayed -- the same rows counted four times.
    # And a page that breaks at a different row in the two copies covers no
    # single page of the other: Mesquite 2022-067 page 3 shares 0.884 of its
    # signatures with 22-6351 page 3 (the rest are on 22-6351's page 2), so
    # its 269 rows stayed while pages 1, 2 and 4 went.  Once a file has lost a
    # sheet to another file, each of its remaining sheets is measured against
    # EVERYTHING the winning file still holds: a short sheet goes when all of
    # it is there, a longer one at the usual thresholds.  Only the surviving
    # sheets of the winner count, so two files that each won some pages
    # cannot take each other's survivors down.
    proven = defaultdict(set)              # losing file -> files it lost to
    for k, info in dropped.items():
        wf = str(info.get("kept_instead", "")).split("::")[0]
        if wf and wf != k[0]:
            proven[k[0]].add(wf)
    for lf in sorted(proven):
        for wf in sorted(proven[lf]):
            win = [k for k in by_file.get(wf, ()) if k not in dropped]
            if not win:
                continue
            union = set().union(*(sigs.get(k, set()) for k in win))
            union_i = set().union(*(sigs_item.get(k, set()) for k in win))
            for k in sorted(by_file.get(lf, ())):
                if k in dropped:
                    continue
                s, si = sigs.get(k, set()), sigs_item.get(k, set())
                if not si:
                    continue
                cover = (len(s & union) / len(s)) if s else 0.0
                cover_i = len(si & union_i) / len(si)
                if len(si) < MIN_SIGS:
                    ok = cover >= 1.0
                else:
                    ok = cover >= STRONG_COVER or (
                        cover_i >= ITEM_COVER and owner_of(k) and owner_of(k) == owner_of(win[0]))
                if not ok:
                    continue
                best = max(win, key=lambda w: len(sigs_item.get(w, set()) & si))
                dropped[k] = {"kept_instead": best[0] + "::" + str(best[1]),
                              "overlap": round(cover, 3),
                              "overlap_bidderfree": round(cover_i, 3),
                              "basis": "leftover_of_proven_pair",
                              "rows_dropped": len(rows_of(k)),
                              "cross_file": True}
                overlaps.append({"file_a": k[0], "sheet_a": k[1],
                                 "file_b": best[0], "sheet_b": best[1],
                                 "shared_sigs": len(s & union), "overlap": round(cover, 3),
                                 "overlap_bidderfree": round(cover_i, 3),
                                 "sigs_bidderfree": len(si),
                                 "basis": "leftover_of_proven_pair",
                                 "dropped": k[0] + "::" + str(k[1]),
                                 "kept": best[0] + "::" + str(best[1])})

    return dropped, overlaps


def dedupe_rows(rows, cross_file_only=True):
    """Adapter for a list of row dicts (parse_bidtabs, build_master panel).

    Rows without a src_sheet are passed through untouched: the TxDOT archive and
    the published price books are keyed by letting upstream and have no sheets
    to compare.
    """
    by_sheet = defaultdict(list)
    passthrough = []
    for r in rows:
        sh = r.get("src_sheet")
        if sh is None or sh == "" or sh != sh:
            passthrough.append(r)
        else:
            by_sheet[(r.get("src_file"), sh)].append(r)

    sigs = {k: set(sig_of(x) for x in v) for k, v in by_sheet.items()}
    sigs_item = {k: set(sig_of(x)[1:] for x in v) for k, v in by_sheet.items()}
    owners = {k: (v[0].get("owner") or "") for k, v in by_sheet.items()}
    dropped, overlaps = find_duplicate_sheets(
        sigs, sigs_item, lambda k: by_sheet[k], lambda k: owners.get(k, ""),
        cross_file_only=cross_file_only)

    kept = passthrough + [r for k, v in by_sheet.items() if k not in dropped
                          for r in v]
    return kept, dropped, overlaps


def dedupe_frame(df):
    """Adapter for a pandas frame (build_master's city loader).

    Applied at the CHOKEPOINT rather than only in the ETL, because the ETL and
    the panel are rebuilt on different schedules: a dedup rule that only runs
    inside parse_bidtabs.py leaves every already-written line_items.csv
    duplicated until the next full reparse, and the panel is what the
    competition discount and the dispersion are fitted on.  Running it in both
    places is deliberate redundancy, not an oversight -- the second pass is a
    no-op when the first has already done the work.

    Returns (kept_frame, dropped, overlaps).  A frame without src_sheet is
    returned untouched: the TxDOT archive and the price books are keyed by
    letting upstream.
    """
    need = ("src_file", "src_sheet", "owner", "bidder", "unit", "quantity",
            "unit_price")
    if any(c not in df.columns for c in need):
        return df, {}, []

    by_sheet = defaultdict(list)
    sheet_series = df["src_sheet"].fillna("").astype(str)
    cols = [df["src_file"], sheet_series, df["owner"], df["bidder"],
            df["unit"], df["quantity"], df["unit_price"]]
    for i, (sf, sh, ow, bd, un, qt, up) in enumerate(zip(*cols)):
        if not sh:
            continue
        by_sheet[(sf, sh)].append((i, ow, bd, un, qt, up))

    def _sig(t):
        _, _, bd, un, qt, up = t
        return sig_of({"bidder": bd, "unit": un, "quantity": qt,
                       "unit_price": up})

    sigs = {k: set(_sig(t) for t in v) for k, v in by_sheet.items()}
    sigs_item = {k: set(_sig(t)[1:] for t in v) for k, v in by_sheet.items()}
    owners = {k: (v[0][1] if v[0][1] == v[0][1] else "") or ""
              for k, v in by_sheet.items()}

    # sheet_pref only needs len() and .get("bidder") per row.
    def rows_of(k):
        return [{"bidder": t[2]} for t in by_sheet[k]]

    dropped, overlaps = find_duplicate_sheets(
        sigs, sigs_item, rows_of, lambda k: owners.get(k, ""),
        cross_file_only=True)
    if not dropped:
        return df, dropped, overlaps

    drop_idx = set()
    for k in dropped:
        for t in by_sheet[k]:
            drop_idx.add(t[0])
    # Positional, not label-based: the frame's index may repeat after a concat,
    # and a label mask would then drop the wrong rows.
    import numpy as _np
    mask = _np.ones(len(df), dtype=bool)
    mask[list(drop_idx)] = False
    return df.iloc[mask].copy(), dropped, overlaps


def read_line_items(path, label="", verbose=True):
    """Read data/clean/line_items.csv and apply the duplicate rule on the way in.

    WHY EVERY CONSUMER AND NOT JUST THE PANEL.  build_master.py is the
    chokepoint for the panel, which protects the panel -- and nothing else.
    Five scripts in this project read line_items.csv directly, so they saw the
    duplicated rows whatever build_master did; one peer session's own output
    turned out to carry 13 duplicated quantity pairs for exactly this reason.
    A chokepoint only covers what flows through it.

    It REPORTS rather than silently repairing.  A stale line_items.csv quietly
    corrected is a file that stays stale, and the next reader who forgets this
    helper is back where we started.  If this prints anything, the ETL needs to
    be re-run; after a current ETL it prints nothing, because the rule is
    idempotent.
    """
    import pandas as pd

    d = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    before = len(d)
    d, dropped, overlaps = dedupe_frame(d)
    if dropped and verbose:
        pairs = sorted({tuple(sorted((o["file_a"], o["file_b"])))
                        for o in overlaps})
        print("  !! %sline_items.csv still holds duplicated lettings: "
              "%d rows over %d sheet(s), %d file pair(s). Re-run "
              "src/parse_bidtabs.py."
              % (label + ": " if label else "", before - len(d),
                 len(dropped), len(pairs)))
        for a, b in pairs:
            print("     %s  ||  %s" % (a, b))
    return d


def dedupe_lettings_cross_source(df, verbose=True):
    """The same TxDOT letting arriving under two project_keys, one per feed.

    A DIFFERENT shape from dedupe_frame, which compares SHEETS inside a file.
    These rows have no src_sheet at all: the current TxDOT API feed and the
    downloaded archive both publish the same letting, under keys that look
    nothing alike --

        A00138908                     (txdot, current feed)
        sc202412.zip::03DE3002.TXT    (txdot_archive)

    -- same bid_date, same row count, same total to the dollar, and on the
    largest pair all 381 item_no values overlap with 100.0% of per-item unit
    prices exactly equal.  Measured: every one of the 19 txdot lettings in 2024
    is already in the panel from the archive -- 5,702 rows, 13.78% of 2024
    dollars, and the chained index level for 2026 is wrong by 2.06 points
    because 2024 is the base of that link.

    Why dedupe_frame could not see it: it keys on (src_file, src_sheet) and
    passes rows without a sheet straight through, which is correct for its job
    and blind here.  Calling it from load_city only -- as the code did -- means
    the de-duplication rule protected the municipal side of the panel and no
    other side.  A chokepoint only covers what flows through it.

    The ARCHIVE copy is kept and the current-feed copy dropped, so that each
    year stays single-source: the archive supplies 95%, 86% and 76% of
    2022-2024 dollars, and dropping the archive instead would split 2024 across
    both feeds for no gain.  The dropped rows are identical, so nothing is lost
    but the double count.
    """
    need = ("source", "project_key", "bid_date", "item_no", "unit_price",
            "quantity")
    if any(c not in df.columns for c in need):
        return df, []
    import numpy as _np
    import pandas as _pd

    cur = df.source.astype(str) == "txdot"
    arc = df.source.astype(str) == "txdot_archive"
    if not cur.any() or not arc.any():
        return df, []

    def fp(sub):
        """letting -> (bid_date, frozenset of (item_no, rounded price))."""
        out = {}
        d = sub[["project_key", "bid_date", "item_no", "unit_price"]]
        price = _pd.to_numeric(d.unit_price, errors="coerce").round(2)
        for pk, bd, it, p in zip(d.project_key, d.bid_date, d.item_no, price):
            if p != p:
                continue
            k = str(pk)
            if k not in out:
                out[k] = [str(bd)[:10], set()]
            out[k][1].add((str(it), float(p)))
        return out

    a_fp, c_fp = fp(df[arc]), fp(df[cur])
    # Index the archive by date so each current letting is compared only with
    # the archive lettings that could possibly be it.
    by_date = defaultdict(list)
    for k, (bd, s) in a_fp.items():
        by_date[bd].append((k, s))

    drop_keys, pairs = set(), []
    for ck, (cbd, cs) in c_fp.items():
        if not cs or not cbd or cbd == "nan":
            continue
        for ak, asx in by_date.get(cbd, ()):
            if not asx:
                continue
            cover = len(cs & asx) / min(len(cs), len(asx))
            if cover >= 0.95 and min(len(cs), len(asx)) >= 8:
                drop_keys.add(ck)
                pairs.append({"stage": "cross_source", "file": "txdot",
                              "sheet_a": ck, "sheet_b": ak,
                              "overlap": round(cover, 3),
                              "overlap_bidderfree": round(cover, 3),
                              "basis": "same bid_date + >=95% of (item_no, unit_price)",
                              "dropped": ck, "kept": ak})
                break
    if not drop_keys:
        return df, []
    mask = ~(cur & df.project_key.astype(str).isin(drop_keys))
    out = df[_np.asarray(mask)].copy()
    if verbose:
        print("  txdot: %d letting(s) already present from txdot_archive, "
              "%d rows dropped" % (len(drop_keys), len(df) - len(out)))
    return out, pairs
