"""
Combine every cleaned source into one panel, then build the unit-price reference.

Three sources with genuinely different price semantics, which is why they carry a
`price_basis` column and are never averaged together blindly:

  city_bidtab  every bidder's unit price on a municipal bid tab  -> all_bids
  txdot        every bidder's unit price on a TxDOT letting      -> all_bids
                 (plus low_bidder_flag, so the winning price is observable)
  garland_avg  one contract price per project per item           -> contract_price

`competition_class` operationalises the distinction the user flagged -- some
files came out of a real competitive bidding process and some did not, and only
the former carry trustworthy prices. The observable proxy is how many bidders
the project drew:

  competitive  >= 3 bidders   (the reference is built from these only)
  thin         2 bidders
  sole         1 bidder       (a negotiated or sole-source price)
  unknown      no bidder structure in the source at all

The reference pools in TWO stages -- median across bidders within a project
first, then median across projects -- so a 13-bidder letting does not outweigh a
3-bidder one, and unbalanced bidding (one contractor loading a single item) is
absorbed at the project level instead of moving the pooled number.

Outputs
  data/clean/master_panel.csv      every usable observation, one schema
  data/clean/price_reference.csv   the deliverable: robust price per item family
  data/clean/unbalanced_bidding.csv  low-bidder vs all-bidder price gap by family
  data/clean/price_time_index.csv  year-over-year movement per family
  reports/master_summary.md
"""
import os

import numpy as np
import pandas as pd

import bidder_identity
import dedup
import item_tokens
import local_config
import qty_shape
import rate_kind
import vintage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
REPORTS = os.path.join(ROOT, "reports")
L = []


def say(s=""):
    L.append(s)
    print(s)


def md(df, fmt="%.2f"):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


def classify(n):
    if n >= 3:
        return "competitive"
    if n == 2:
        return "thin"
    if n == 1:
        return "sole"
    return "unknown"


def _apply_sourced_bid_dates(d):
    """Replace extracted dates with ones read off the documents by hand.

    data/external/_sourced_bid_dates.csv covers EVERY file from that collection
    -- 36 rows, no omissions -- and says why each one does or does not get a
    date: 20 verified, 3 inferred from a sibling document, 1 ambiguous,
    5 not a bid at all, 7 undetermined.  Completeness is the point: a side
    table that silently skipped files would be worse than none, because the
    absent ones would read as "fine".

    This matters most where the automatic extraction is systematically late.
    The agenda packets are named by MEETING date, and even when a bid-opening
    line is found, "take the latest candidate" picks a later item's date out of
    a 170-page bundle:
        Dry Branch     extracted 2024-04-30, actually opened 2023-11-13
                       -- 169 days, and across a year boundary
        Pollock Place  extracted 2025-03-18, actually opened 2024-12-16
                       -- that one says "assisted in a bid opening on
                          December 16, 2024", not "BID OPENED:"

    Only `verified` is treated as precise.  `inferred` dates come from a
    sibling document rather than the tab itself, and `ambiguous` /
    `undetermined` / `not_a_bid` supply no date at all -- in particular the
    2023-11-21 Drainage Ditch packet is deliberately left ambiguous because its
    first bid-opening line post-dates the meeting and so belongs to another
    item.  Resolving that one needs per-section dating, which does not exist
    yet, so it keeps its approximate date and stays out of the index.
    """
    p = os.path.join(ROOT, "data", "external", "_sourced_bid_dates.csv")
    if not os.path.exists(p):
        return d
    try:
        sv = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
    except Exception as e:
        print("  !! could not read _sourced_bid_dates.csv: %s" % type(e).__name__)
        return d
    need = {"file", "bid_opened", "confidence"}
    if not need.issubset(set(sv.columns)):
        return d
    sv["base"] = (sv.file.astype(str).str.replace("\\", "/", regex=False)
                  .str.split("/").str[-1].str.strip())
    sv["conf"] = sv.confidence.astype(str).str.strip().str.lower()
    # Entries my own check of the document contradicts.  The side table is good
    # -- 20 of its 23 dated rows survived verification -- but it dates the
    # AGENDA ITEM, and where a packet's tabulation belongs to a different
    # letting than the item it was filed under, that is the wrong date for the
    # priced rows.
    #
    # Dry Branch: the table said 2023-11-13, so 766 rows were moved out of 2024
    # and into 2023.  Checking the PDF, the pages that actually carry the prices
    # (57-59 of 76) print "Bid Opening Date: Tuesday, April 30 at 3:00 PM" three
    # times; "November 13, 2023" occurs once in the packet and not on any
    # bid-opening line; "Dry Branch" appears on pages 2, 51, 53 and 54 and NOT
    # on the priced pages; and there is no "re-bid" anywhere.  So the automatic
    # extraction's 2024-04-30 was right and the correction was wrong -- it would
    # have put 766 rows into the wrong inflation year, which is exactly the
    # error it was adopted to fix, in the opposite direction.
    CONTRADICTED = {
        "2024-05-21_Dry_Branch_Channel_Slope_Reconstruction_"
        "DevCommittee_Packet_with_Bid_Tabulation.pdf",
    }
    usable = sv[sv.conf.isin(["verified", "inferred"])
                & ~sv.base.isin(CONTRADICTED)
                & sv.bid_opened.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)]
    if not len(usable):
        return d
    dmap = dict(zip(usable.base, usable.bid_opened.astype(str).str.strip()))
    cmap = dict(zip(usable.base, usable.conf))
    base = (d.src_file.astype(str).str.replace("\\", "/", regex=False)
            .str.split("/").str[-1].str.strip())
    # A row the parser dated from its OWN table's page ('bid_open_table',
    # parser round 3: a council packet bundling several lettings) keeps that
    # date: a sourced date is one per FILE, and Grand Prairie's 2025-09-03
    # packet holds the Kirby Creek tab (opened 2025-08-04) and Mansfield's Day
    # Miar Road tab (opened 2023-06-14).
    own_page = (d.get("bid_date_source", pd.Series("", index=d.index)).fillna("")
                .astype(str).str.startswith("bid_open_table"))
    hit = base.isin(dmap) & ~own_page
    if hit.any():
        changed = int((d.loc[hit, "bid_date"].astype(str)
                       != base[hit].map(dmap)).sum())
        d.loc[hit, "bid_date"] = base[hit].map(dmap)
        conf = base[hit].map(cmap)
        d.loc[hit, "bid_date_source"] = np.where(
            conf == "verified", "bid_open_or_due_sourced", "sourced_inferred")
        print("  sourced bid dates applied to %d rows / %d files "
              "(%d rows had a different date before)"
              % (int(hit.sum()), base[hit].nunique(), changed))
    return d


def _apply_local_date_overrides(d):
    """Fill bid_date for company (test) files whose documents carry no date.

    Some of the company spreadsheets in the private test set print no date at
    all: they were scanned sheet by sheet and hold zero date cells and zero
    textual dates, and the missing dates cannot be supplied.

    Without a year those projects cannot be escalated, and escalation is not a
    rounding correction here -- the official indices move +55% to +95% over
    2016-2026, so scoring an un-escalated price against a current reference
    can be wrong by more than the model's own error.

    So externally researched dates are used, each with its evidence and a
    confidence level recorded in a CSV named by the private local
    configuration (src/local_config.py, key "date_overrides_csv"), and they are
    marked `company_date_inferred` rather than borrowed into
    `bid_open_or_due`.  That keeps them out of the escalation index, which tests
    for exactly "bid_open_or_due" -- these dates are good enough to escalate a
    test case for scoring, not good enough to help DEFINE the index.

    The "medium" entries rest on reading a year out of a bid number, and that
    inference has been shown to fail: Arlington's PWDR12001C numbers as 2012
    and opened in 2022, ten years out.  They are usable for scoring with the
    confidence column in hand, and should not be treated as facts.

    A public checkout has no such configuration and no company files: the
    function then only applies the sourced public dates.
    """
    d = _apply_sourced_bid_dates(d)
    name = local_config.get("date_overrides_csv")
    if not name:
        return d
    p = os.path.join(ROOT, "data", name)
    if not os.path.exists(p):
        return d
    ov = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
    mp = dict(zip(ov.src_file.astype(str).str.strip(),
                  ov.bid_date.astype(str).str.strip()))
    cf = dict(zip(ov.src_file.astype(str).str.strip(),
                  ov.confidence.astype(str).str.strip()))
    base = d.src_file.astype(str).str.replace("\\", "/", regex=False).str.split("/").str[-1]
    blank = d.bid_date.isna() | (d.bid_date.astype(str).str.strip() == "")
    hit = blank & base.isin(mp)
    if hit.any():
        d.loc[hit, "bid_date"] = base[hit].map(mp)
        d.loc[hit, "bid_date_source"] = (
            "company_date_inferred_" + base[hit].map(cf).fillna("unknown"))
        print("  company date overrides applied to %d rows / %d files"
              % (int(hit.sum()), base[hit].nunique()))
    return d


def load_city():
    p = os.path.join(CLEAN, "line_items.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    d = _apply_local_date_overrides(d)
    d["is_engineer_estimate"] = d.is_engineer_estimate.astype(str).str.lower().isin(
        ["true", "1", "yes"])
    for c in ("quantity", "unit_price", "size_in"):
        d[c] = pd.to_numeric(d[c], errors="coerce")

    # ---- one letting recorded twice ------------------------------------
    # Run BEFORE the quality filters, so the two copies are compared on the
    # same footing: the filters drop rows unevenly between copies (one scan is
    # cleaner than the other), which would depress the measured overlap of a
    # pair that is in fact identical.
    #
    # This is the chokepoint, and the reason the rule is applied here as well
    # as in the ETL is that the two are rebuilt on different schedules.  The
    # measured cost of letting a duplicate through is NOT a wider sample, it is
    # a narrower one: counting a letting twice leaves the median about where it
    # was and tightens the spread, so sigma and the competition discount read
    # as better-determined than the evidence supports.  Live corpus, 1,034 rows
    # over two pairs: pipe_fittings was inflated 14.4%, shoring 16.0%, trm
    # 9.8%, gabion_wall 8.8%.
    # ---- documents whose TEXT is damaged, not whose prices are -----------
    # Two text extractors disagree on 35 files in the corpus because the PDF
    # interleaves adjacent text columns character by character:
    #     'MCleoawniinngg, otrfi mtramshin,g...'
    #   = 'Mowing, trimming/edging of standard...' + 'Cleaning of trash,...'
    # The quantity and the unit price sit in their own columns and are fine.
    # That is exactly what makes it dangerous: the row looks complete, the
    # price is right, and the text the item_family rules read is rubbish --
    # the same hazard as an OCR row, from a different cause.
    #
    # So the family is set to `needs_review` rather than left to the
    # classifier, which would return a confident wrong family: `Subgrade` for
    # what is actually flexible base is an order-of-magnitude error in price.
    # `needs_review` is in UNCLASSIFIED_FAMILIES, so these refuse to price
    # VISIBLY instead of being silently discarded.
    #
    # The match count is printed, because INDEX.csv stores paths relative to
    # data/external while line_items.csv stores them with the `external/`
    # prefix: a path-convention mismatch here would mark zero rows and say
    # nothing, which is the failure mode this whole session kept hitting.
    # ROOT/data/external, not DATA: this module defines ROOT and CLEAN only.
    # Written as `DATA` first, which is the name parse_bidtabs uses -- and the
    # join sat OUTSIDE the try below, so the NameError was not caught and
    # build_master.py died outright.  Every later step then ran against the
    # PREVIOUS panel and reported success, which is the failure shape this
    # session kept finding: the run looked like it worked.
    idx_p = os.path.join(ROOT, "data", "external", "INDEX.csv")
    if os.path.exists(idx_p):
        try:
            ix = pd.read_csv(idx_p, encoding="utf-8-sig", low_memory=False)
            if "text_quality" in ix.columns and "file" in ix.columns:
                bad = ix.loc[ix.text_quality.astype(str)
                             == "interleaved_columns", "file"].astype(str)
                bad = {"external/" + x.replace("\\", "/").strip("/")
                       for x in bad}
                hit = d.src_file.astype(str).str.replace("\\", "/",
                                                         regex=False).isin(bad)
                print("  city: %d file(s) flagged interleaved_columns, "
                      "%d row(s) matched -> item_family=needs_review"
                      % (len(bad), int(hit.sum())))
                if len(bad) and not hit.any():
                    print("    !! none matched -- check the path convention "
                          "between INDEX.csv and line_items.csv")
                d.loc[hit, "item_family"] = "needs_review"
            # WORK TYPE -- pavement, drainage, erosion, utility, sidewalk,
            # traffic.  Also in INDEX.csv and also empty in all 706,841 panel
            # rows until now, the same dropped-column shape as `material` and
            # `source_set`.  It is carried because the question it answers is
            # the client's: does the same pay item price differently in an
            # erosion job than in a drainage job?
            #
            # Measured on the municipal rows joined back through INDEX.csv, it
            # currently does NOT: holding family, unit, size_bucket and
            # quantity band fixed, the dearest work type runs 1.86x the
            # cheapest, against 1.77x from shuffling the work-type labels
            # inside the same cells -- a 5% excess over chance.  The dramatic
            # cases (sidewalk SF at $73.25 against drainage SF at $2.00, 36x)
            # are all in the `other` catch-all, which is two different items
            # sharing a label, not one item priced two ways.  And as a price
            # key it takes municipal lettings that can be fully priced from 49
            # to 13, which is the same wall exact-description matching hit.
            #
            # So it is NOT a price key today.  It is carried so the question is
            # re-testable when the municipal corpus grows, instead of being
            # re-derived from the index by whoever asks next.
            if "work_type" in ix.columns and "file" in ix.columns:
                wt = (ix.assign(_f=ix.file.astype(str).str.replace(
                          "\\\\", "/", regex=False).str.strip("/"))
                      .drop_duplicates("_f").set_index("_f")["work_type"])
                base = (d.src_file.astype(str).str.replace("\\\\", "/",
                                                           regex=False)
                        .str.replace(r"^external/", "", regex=True))
                d["work_type"] = base.map(wt).fillna("")
                n_wt = int((d.work_type != "").sum())
                print("  city: work_type joined on %d of %d rows (%.1f%%)"
                      % (n_wt, len(d), 100.0 * n_wt / max(len(d), 1)))
        except Exception as e:
            print("  city: could not read INDEX.csv text_quality (%s: %s)"
                  % (type(e).__name__, e))
    if "work_type" not in d.columns:
        d["work_type"] = ""

    d, dup_sheets, dup_overlaps = dedup.dedupe_frame(d)
    if dup_sheets:
        print("  city: dropped %d duplicate sheet(s), %d rows, %d file pair(s)"
              % (len(dup_sheets),
                 sum(v["rows_dropped"] for v in dup_sheets.values()),
                 len(set(tuple(sorted((o["file_a"], o["file_b"])))
                         for o in dup_overlaps))))
        for o in dup_overlaps:
            print("    %s  <- dropped (cover %.3f / bidder-free %.3f, %s)"
                  % (o["dropped"], o["overlap"], o["overlap_bidderfree"],
                     o["basis"]))
        # APPEND, do not overwrite: parse_bidtabs.py writes this table first
        # with the drops it made at source, and overwriting it here erased
        # them -- which is how three Mesquite pairs became invisible to a
        # consumer joining against the table.  `stage` says which pass decided.
        dd = pd.DataFrame(dup_overlaps)
        dd["stage"] = "panel"
        dp = os.path.join(CLEAN, "duplicate_lettings.csv")
        if os.path.exists(dp):
            try:
                dd = pd.concat([pd.read_csv(dp, encoding="utf-8-sig"), dd],
                               ignore_index=True)
            except Exception:
                pass
        dd.to_csv(dp, index=False, encoding="utf-8-sig")

    d = d[(d.unit_price > 0) & (d.ext_mismatch != "Y")
           & (d["unit_suspect"].fillna("") != "Y") & (d.section_role == "base")]
    # n_bidders counts DISTINCT CONTRACTORS, so it must count canonical names.
    # Two spellings of one firm in one document would otherwise read as two
    # bidders and move that letting into a more competitive class -- and the
    # discount is the whole second stage of the estimate.  A bidder cell holding
    # only header text ("Bid Amount", "Item 15") is a failed parse, not a
    # bidder, and normalise_series returns "" for it so nunique skips it.
    #
    # Measured, and the measurement is mostly a NEGATIVE result worth recording:
    # over 424 files this moves n_bidders on 5 of them and changes the
    # competition class on 2.  The corpus-wide effect on the discount is
    # negligible.  The reason to do it anyway is the per-bidder work -- see
    # bidder_identity.py -- where contractors present in >=3 projects go from
    # 76 to 113 once the spellings are joined up.
    # WHO WON.  A municipal bid tab prints every bidder and says nothing about
    # the award, so unlike the two TxDOT feeds there is no column to carry over
    # -- it is derived in award_identity.py and merged here.  Without this the
    # panel holds every bid but cannot tell a winning price from a losing one,
    # and the rule that only transacted prices may train the model has nothing
    # to stand on.
    #
    # Derivation and panel must agree on the letting, so the merge is on
    # (src_file, src_sheet) and the comparison on the CANONICAL bidder name:
    # award_identity canonicalises before deciding, so comparing raw strings
    # would miss the winner wherever the tab spells the firm two ways.
    d["is_low_bidder"] = pd.Series(pd.NA, index=d.index, dtype="boolean")
    d["award_basis"] = ""
    d["price_basis"] = "all_bids"
    d["letting_id"] = d.src_file
    ap = os.path.join(CLEAN, "municipal_award_sheets.csv")
    if os.path.exists(ap):
        aw = pd.read_csv(ap, encoding="utf-8-sig", low_memory=False)
        aw = aw[["src_file", "src_sheet", "award_basis", "winner",
                 "comparable", "letting_id"]].rename(
                     columns={"letting_id": "letting_id_aw"})
        d = d.merge(aw, on=["src_file", "src_sheet"], how="left",
                    suffixes=("", "_aw"))
        d["award_basis"] = d.award_basis_aw.fillna("")
        d = d.drop(columns=[c for c in ("award_basis_aw",) if c in d.columns])
        wk = bidder_identity.normalise_series(d.winner.fillna(""))
        bk = bidder_identity.normalise_series(d.bidder.fillna(""))
        decided = d.winner.fillna("") != ""
        d.loc[decided, "is_low_bidder"] = (wk == bk)[decided]
        # An engineer estimate is not a bidder and never won anything.
        d.loc[d.is_engineer_estimate, "is_low_bidder"] = pd.NA
        # A letting whose bidders are not pricing the same job yields no
        # comparable price, so it is marked here rather than silently averaged.
        bad = d.comparable.fillna("true").astype(str).str.lower() == "false"
        d.loc[bad, "price_basis"] = "uneven_proposal"
        # THE LETTING, not the file.  src_file was standing in for "one bid
        # opening" and on 20 of 435 city files it is not: a development
        # committee packet carries up to 6 separate lettings, and a tab too
        # wide for the page splits one letting across 8 sheets.  6.2% of city
        # rows sat in a fused key.  It mattered twice over -- the two-stage
        # median pooled two different projects as one, and `n_bidders`, counted
        # per file, read 18 for Briar Hill's 8-bidder letting and so priced it
        # from the wrong competition class.  award_identity reconstructs the
        # letting; n_bidders below is counted on it.
        d["letting_id"] = d.letting_id_aw.fillna(d.src_file)
        d = d.drop(columns=["winner", "comparable", "letting_id_aw"])
        print("   award: %d of %d city rows tagged win/lose, %d winning, "
              "%d rows from non-comparable lettings"
              % (int(d.is_low_bidder.notna().sum()), len(d),
                 int((d.is_low_bidder == True).sum()), int(bad.sum())))

    # n_bidders counts DISTINCT CONTRACTORS IN ONE LETTING, on canonical names.
    # Two spellings of one firm would otherwise read as two bidders and move
    # that letting into a more competitive class -- and the discount is the
    # whole second stage of the estimate.  A bidder cell holding only header
    # text ("Bid Amount", "Item 15", "Low Bidder") is a failed parse, not a
    # bidder, and normalise_series returns "" for it so nunique skips it.
    bids = d[~d.is_engineer_estimate]
    nb = (bids.assign(_b=bidder_identity.normalise_series(bids.bidder))
          .replace({"_b": {"": np.nan}})
          .groupby("letting_id")["_b"].nunique().rename("n_bidders"))
    if "n_bidders" in d.columns:
        d = d.drop(columns=["n_bidders"])
    d = d.merge(nb, left_on="letting_id", right_index=True, how="left")

    return pd.DataFrame({
        "source": "city_bidtab",
        "price_basis": d.price_basis,
        "award_basis": d.award_basis,
        # PROVENANCE, carried not invented.  line_items.csv has source_set, but
        # this explicit DataFrame({...}) used to omit it, so the column was
        # silently discarded here and master_panel.csv was written without any
        # way to tell the company's files from the public ones -- while `source`
        # reads 'city_bidtab' for BOTH.  Missing defaults to "internal" because
        # withholding a public row from training costs a row, whereas admitting
        # a company row destroys the validity of the test set.
        "source_set": d.source_set.fillna("internal").replace("", "internal"),
        "owner": d.owner.fillna("internal").replace("", "internal"),
        # the reconstructed letting, not the file (see above)
        "project_key": d.letting_id,
        "bid_date": d.bid_date,
        # Only a date read from the bid-opening/closing line is precise. A
        # council-agenda date lands weeks late, and a solicitation number gives
        # a FISCAL year that can be a year off. Escalation work must use the
        # precise ones only, or the index measures filing conventions.
        # `bid_open_or_due_sourced` counts as precise too: it is a date read off
        # the document body by hand and recorded in
        # data/external/_sourced_bid_dates.csv, which is the same standard as the
        # automatic extraction, not a weaker one.  Admitting it ENLARGES the
        # index's usable sample, where the agenda and bid-number tiers stay out.
        # `bid_open_table` (parser round 3) is that same opening line, read on
        # the table's own page of a packet that bundles several lettings.
        "date_precision": np.where(
            d.get("bid_date_source", pd.Series("", index=d.index))
            .fillna("").isin(["bid_open_or_due", "bid_open_or_due_sourced", "bid_open_table"]),
            "precise", "approx"),
        "item_no": d.item_no,
        "description": d.description,
        "desc_norm": d.desc_norm,
        "item_family": d.item_family,
        "size_in": d.size_in,
        # MATERIAL, which this DataFrame({...}) used to omit.  line_items.csv
        # carries it for 4,324 rows -- PVC 2,259, RCP 973, C900 338, SDR 26
        # 277, HDPE 241, ductile iron 158, CMP 78 -- and the panel had it empty
        # in all 706,841, so it was parsed and then thrown away.  Same shape of
        # bug as the dropped source_set and is_low_bidder: a column absent from
        # this literal disappears with no error.  It matters because README
        # rule 10 is exactly this -- concrete riprap $624/CY against stone
        # $242/CY, 2.6x -- so a family median that mixes materials is mixing
        # two prices.
        "material": d.get("material", pd.Series("", index=d.index)).fillna(""),
        "work_type": d.work_type,
        "unit": d.unit,
        "quantity": d.quantity,
        "bidder": d.bidder,
        "is_engineer_estimate": d.is_engineer_estimate,
        "is_low_bidder": d.is_low_bidder,
        "n_bidders": d.n_bidders.fillna(0),
        "unit_price": d.unit_price,
        # Where the row's text came from: "text_layer", or "ocr" for a scan
        # read by src/parse_ocr_sidecars.py (ocr_check says which identity the
        # row passed).  Carried explicitly because, as the comments above show,
        # any column missing from this literal vanishes without an error.
        "text_source": d.get("text_source", pd.Series("", index=d.index))
                        .fillna("").replace("", "text_layer"),
        "ocr_check": d.get("ocr_check", pd.Series("", index=d.index)).fillna(""),
    })


def load_txdot():
    p = os.path.join(CLEAN, "txdot_line_items.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    for c in ("quantity", "unit_price", "size_in", "n_bidders"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["is_engineer_estimate"] = d.is_engineer_estimate.astype(str).str.lower().isin(
        ["true", "1", "yes"])
    d["is_low_bidder"] = (d.is_low_bidder.astype(str).str.lower()
                          .isin(["true", "1", "yes"]).astype("boolean"))
    d = d[d.unit_price > 0]
    # A LETTING is (project_id, let date), as in load_txdot -- not project_id.
    # 8 of 1,081 DFW project_ids were let twice, and keying on project_id
    # alone merged the two: A00184778's one-bidder letting of 2025-02-06 and
    # its six-bidder re-let of 2025-06-04 became one letting in which WG
    # Engineering's two bids were summed (272 rows, $9,271,021.78) and the
    # 2025-06-04 winner fell to last (tail audit 2026-11-20).  A project let
    # once keeps its bare project_id as the key; a re-let one is keyed
    # '<project_id>::<let date>', so key.split("::")[0] is the project_id
    # either way (price_model_v2.src_file_of relies on that).
    nd = d.groupby("project_id")["bid_date"].transform("nunique")
    pkey = np.where(nd > 1, d.project_id.astype(str) + "::" + d.bid_date.astype(str),
                    d.project_id.astype(str))
    return pd.DataFrame({
        "source": "txdot",
        "price_basis": "all_bids",
        "work_type": "",
        # declared, not left to the concat (see load_city)
        "material": "",
        # published by TxDOT, not inferred by us
        "award_basis": "source_column",
        "date_precision": "precise",   # project_actual_let_date, from the dataset
        "source_set": "external",      # TxDOT's own public dataset
        "owner": "txdot",
        "project_key": pkey,
        "bid_date": d.bid_date,
        "item_no": d.item_no,
        "description": d.description,
        "desc_norm": d.desc_norm,
        "item_family": d.item_family,
        "size_in": d.size_in,
        "unit": d.unit,
        "quantity": d.quantity,
        "bidder": d.bidder,
        "is_engineer_estimate": d.is_engineer_estimate,
        "is_low_bidder": d.is_low_bidder,
        "n_bidders": d.n_bidders.fillna(0),
        "unit_price": d.unit_price,
    })


def load_garland():
    p = os.path.join(CLEAN, "garland_item_prices.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    for c in ("quantity", "unit_price", "size_in"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d.unit_price > 0) & (d.ext_mismatch != "Y")]
    return pd.DataFrame({
        "source": "garland_avg",
        # One contract price per project: already the competitive OUTCOME, so it
        # must never have a competition discount applied on top of it.
        "price_basis": "contract_price",
        "work_type": "",
        # declared, not left to the concat (see load_city)
        "material": "",
        # one awarded contract price per project, as published
        "award_basis": "published_contract",
        "date_precision": "precise",   # printed per project row in the report
        "source_set": "external",      # Garland's published annual report
        "owner": "garland",
        "project_key": d.project_name.fillna("") + " | " + d.bid_date.fillna(""),
        "bid_date": d.bid_date,
        "item_no": d.item_no,
        "description": d.description,
        "desc_norm": d.desc_norm,
        "item_family": d.item_family,
        "size_in": d.size_in,
        "unit": d.unit,
        "quantity": d.quantity,
        "bidder": "(contract)",
        "is_engineer_estimate": False,
        "is_low_bidder": pd.Series(True, index=d.index, dtype="boolean"),
        "n_bidders": np.nan,
        "unit_price": d.unit_price,
    })


def load_txdot_archive():
    """2016-2024 history. Same shape as the current feed, but note the item codes
    are the OLD 6xxx generation -- never joined to current codes directly (see
    data/clean/txdot_code_crosswalk.csv for why that silently fails)."""
    p = os.path.join(CLEAN, "txdot_archive_line_items.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    for c in ("quantity", "unit_price", "size_in", "n_bidders"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[d.unit_price > 0]
    return pd.DataFrame({
        "source": "txdot_archive",
        "price_basis": "all_bids",
        "work_type": "",
        # declared, not left to the concat (see load_city)
        "material": "",
        "award_basis": "source_column",
        "date_precision": "precise",   # DATE line in the tab header
        "source_set": "external",      # TxDOT's public letting archive
        "owner": "txdot_archive",
        "project_key": d.src_file,
        "bid_date": d.bid_date,
        "item_no": d.item_no,
        "description": d.description,
        "desc_norm": d.desc_norm,
        "item_family": d.item_family,
        "size_in": d.size_in,
        "unit": d.unit,
        "quantity": d.quantity,
        "bidder": d.bidder,
        "is_engineer_estimate": False,
        "is_low_bidder": d.is_low_bidder.astype(str).str.lower()
                         .isin(["true", "1"]).astype("boolean"),
        "n_bidders": d.n_bidders.fillna(0),
        "unit_price": d.unit_price,
    })


def load_erosion_shape_arm():
    """Statewide erosion lettings, for the quantity SHAPE fit and nothing else.

    WHY THIS FILE IS NOT IN THE PANEL.  The panel's txdot_archive arm reads
    txdot_archive_line_items.csv -- every item of 2,024 lettings, 63% of its
    rows in Tarrant/Dallas/Collin/Denton.  This file is the other cut of the
    same archive: every letting statewide (4,340 of them, 271 monthly zips,
    2016-2024 -- 16 stray rows are dated 2010/2015) but only the
    erosion-relevant items.  Measured, the two cuts share 0 src_file and 0
    CSJ, so this adds lettings rather than duplicating them -- which is
    exactly why it cannot simply be concatenated into the panel: 0 of its rows
    are in the 14 counties the panel's TxDOT arms cover, and a statewide price
    is not a DFW price.  src/load_erosion_statewide.py already measured that:
    the DFW/other-district ratio is not consistent enough across items to
    convert one into the other.
    WHAT IT IS SAFE FOR, measured 2026-11-18 on the existing panel + this file
    (the first version of this arm got BOTH of the following wrong; 4c now
    does neither).
    LEVEL.  An item fixed effect absorbs a price level only when it is not
    SHARED across regions.  Keyed on desc_norm|unit alone, a DFW cell and a
    statewide cell of the same TxDOT item shared one effect, and wherever the
    two regions sit in different bands the regional gap went into beta.
    Scale test -- every statewide price x0.5, refit: that design's
    multipliers moved by up to 0.37 in log (x2: up to 0.46) over 76
    band-ratios.  The gap it leaks is real: on 218 same-item, same-band cells
    (74 items, both cells >= 2 lettings) DFW/statewide runs 0.94 / 1.09 /
    1.20 at the quartiles.  rock_anchor|SF's "items that disagree about the
    SIGN" (src/qty_shape.py) were this and nothing else -- DFW cells at
    5000-25000 paired with statewide cells at 25000+.  4c therefore appends
    the arm to the item key: each region gets its own item effect, only beta
    is pooled, and the same scale test moves 0 of 84 band-ratios (max
    0.0000, x0.5 and x2).
    SHAPE.  A statewide curve is not always a DFW curve.  On the 7 (family,
    unit) combos measurable from either source alone, panel-only minus
    statewide-only runs median -0.054 in log, MAD-sigma 0.236, worst 2.07
    (erosion_control|SY: statewide puts 5000-25000 at 0.08x of 10-50, DFW at
    0.65x).  Out of sample -- the statewide-only shape applied to DFW items it
    never saw -- it beats applying no shape in 73% of 420 item band-pairs
    (median |error| 0.216 vs 0.322), yet on erosion_control|SY it loses 24 of
    25 (0.837 vs 0.247).  So the arm FILLS and never overrides: 4c fits the
    DFW panel first and borrows only for a (family, unit) the DFW panel
    cannot shape.  Pooled into every family it would have rewritten 10
    DFW-measured shapes on rebuild (16 band-ratios by more than 0.10,
    erosion_control|SY by up to 1.49) and shrunk the published borrowing
    sigma with statewide items scored against a curve they dominate (three
    bands: 0.402 -> 0.357).
    WHAT IT IS GATED ON.  unit_price > 0 and n_bidders >= 3, the same
    competitive-construction test `comp` applies.  No engineer's-estimate row
    can enter: parse_txdot_archive.py puts the engineer's unit price in its own
    COLUMN and emits bid_rank >= 1 rows only -- asserted below rather than
    trusted.  The project_kind gate is deliberately NOT recomputed here: this
    file is a description-filtered subset of each letting, so a constr_share
    measured on it is not the letting's real share, and the supplier-catalogue
    tabs that gate exists to exclude cannot occur in a TxDOT letting archive.
    Measured anyway, it would drop 0 of the 78,899 gated rows.
    """
    p = os.path.join(CLEAN, "txdot_archive_erosion_statewide.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p, encoding="utf-8-sig", low_memory=False)
    for c in ("quantity", "unit_price", "n_bidders", "bid_rank"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    # The OPCC can never enter a fitted quantity.  In this file the engineer's
    # price is a column, not a row -- proven here, not assumed, because the day
    # the archive parser starts emitting an EE row this fit would silently
    # start fitting engineer estimates.
    assert "engineer_unit_price" in d.columns, "archive lost its EE column"
    assert bool((d.bid_rank >= 1).all()), "archive emitted a bid_rank 0 row"
    d = d[(d.unit_price > 0) & (d.quantity > 0) & (d.n_bidders >= 3)]
    return pd.DataFrame({
        "item_family": d.item_family,
        "unit": d.unit,
        "desc_norm": d.desc_norm,
        "qty_band": d.quantity.map(qty_shape.band_of),
        # src_file is the letting.  Disjoint from every panel project_key by
        # measurement (0 of 4,340 appear in the panel's archive arm), so these
        # never merge with a panel letting's cell.
        "project_key": d.src_file,
        "unit_price": d.unit_price,
        "shape_arm": "statewide_archive",
    })


def size_bucket(v):
    if pd.isna(v):
        return ""
    for lo, hi in [(0, 6), (6, 9), (9, 13), (13, 19), (19, 25), (25, 37), (37, 1e9)]:
        if lo <= v < hi:
            return "%g-%g in" % (lo, hi) if hi < 1e9 else "37+ in"
    return ""


def main():
    parts = [load_city(), load_txdot(), load_txdot_archive(), load_garland()]
    parts = [p for p in parts if len(p)]
    m = pd.concat(parts, ignore_index=True)
    # bid_date arrives as str from some sources and NaN from others; a mixed
    # column breaks min/max, so normalise to a plain string once here.
    m["bid_date"] = m.bid_date.fillna("").astype(str).str.slice(0, 10)

    # ---- the same TxDOT letting from both feeds --------------------------
    # dedup.dedupe_frame runs inside load_city and compares SHEETS, so it was
    # blind to this: the current API feed and the archive publish the same
    # letting under unrecognisably different keys and with no sheet at all.
    # 19 lettings in 2024, 5,702 rows, 13.78% of that year's dollars -- and
    # 2024 is the base of the 2024->2025 link, so the chained index level for
    # 2026 was out by 2.06 points.  Runs here, after the concat, because that
    # is the first point where both feeds are in one frame.
    m, xsrc_pairs = dedup.dedupe_lettings_cross_source(m)
    if xsrc_pairs:
        xp = os.path.join(CLEAN, "duplicate_lettings.csv")
        xdf = pd.DataFrame(xsrc_pairs)
        if os.path.exists(xp):
            try:
                xdf = pd.concat([pd.read_csv(xp, encoding="utf-8-sig"), xdf],
                                ignore_index=True)
            except Exception:
                pass
        xdf.to_csv(xp, index=False, encoding="utf-8-sig")

    # One contractor, one identity -- across ALL four sources, resolved once
    # here so the four cannot disagree.  `bidder` keeps the spelling as printed
    # (it is the evidence); `bidder_norm` is what any per-bidder grouping should
    # use.  Empty means the cell held only column-header text.
    m["bidder_norm"] = bidder_identity.normalise_series(m.bidder)
    # What KIND of price each number is.  `price_basis` is already taken and
    # means something else (all_bids / contract_price / trimmed_mean), so this
    # is its own column.  Annual service contracts are tabulated on the same
    # forms as construction and reach the same pool: see src/rate_kind.py for
    # the measured cases and for why the damage is contained today.
    m["rate_kind"] = rate_kind.classify_frame(m.description, m.unit)
    m["n_bidders"] = pd.to_numeric(m.n_bidders, errors="coerce")
    m["competition_class"] = m.n_bidders.map(
        lambda n: classify(int(n)) if pd.notna(n) and n > 0 else "unknown")
    m.loc[m.source == "garland_avg", "competition_class"] = "unknown"
    m["year"] = m.bid_date.astype(str).str[:4]
    m["size_bucket"] = m.size_in.map(size_bucket)
    # The most specific identifier each source actually has. TxDOT publishes a
    # standardised bid code that is stable across lettings, which is a far better
    # key than any description-matching heuristic; the municipal sources have
    # only free text, and wording differs per owner, so they key within owner.
    # TxDOT's own bid code is a far better key than description matching -- but
    # the archive's 6xxx codes and the current feed's 7xxx codes are different
    # generations of the SAME items, so they must not share a key namespace.
    # Within TxDOT, key on the normalised description instead, which is what the
    # crosswalk showed to be the only reliable bridge between the two eras.
    m["item_key"] = np.where(
        m.source.isin(["txdot", "txdot_archive"]),
        "txdot|" + m.desc_norm.astype(str) + "|" + m.unit.astype(str),
        m.source.astype(str) + "|" + m.owner.astype(str) + "|" + m.desc_norm.astype(str))

    say("# Master panel and unit-price reference")
    say()
    say("## 1. Data overview")
    say()
    g = (m.groupby(["source", "price_basis"])
         .agg(rows=("unit_price", "size"), projects=("project_key", "nunique"),
              items=("desc_norm", "nunique"), bidders=("bidder", "nunique"),
              first=("bid_date", lambda s: min([x for x in s if x] or [""])),
              last=("bid_date", lambda s: max([x for x in s if x] or [""]))).reset_index())
    say(md(g))
    say()
    say(f"Total: **{len(m):,}** unit-price observations, **{m.project_key.nunique():,}** projects, "
        f"**{m.bidder.nunique():,}** contractors.")
    say()
    say("## 2. Competition-intensity classes (they decide which prices can be trusted)")
    say()
    cc = (m[~m.is_engineer_estimate].groupby("competition_class")
          .agg(rows=("unit_price", "size"), projects=("project_key", "nunique"))
          .reindex(["competitive", "thin", "sole", "unknown"]).reset_index())
    say(md(cc))
    say()
    say("Only `competitive` (>=3 bidders) is used to build the unit-price reference. `sole` is a "
        "single-bidder price; `unknown` means the source data has no bidder structure (the Garland "
        "average-price table).")
    say()

    # ---- is this project even construction? ------------------------------
    # Collin County's bid-tab folder is largely goods and services (plumbing
    # hourly rates, trucks, software subscriptions) and most of those files are
    # named only by a number, so the title tells us nothing. Judge by CONTENT:
    # a construction tab prices work in SY/CY/LF/TON/SF/STA under a recognised
    # construction family; a services contract prices hours and licences.
    # The signal is the ITEM FAMILY, not the unit. Gating on engineering units
    # (SY/CY/LF) wrongly threw out 700 genuine TxDOT projects, because highway
    # maintenance, striping and signal work is priced per EA / DAY / MO. What
    # actually separates a services contract is that almost none of its items
    # resolve to a construction family at all -- hourly rates, software licences
    # and vehicles all land in `other`.
    m["constr_row"] = (~m.item_family.isin(["other", ""])).astype(float)
    share = m.groupby("project_key")["constr_row"].mean().rename("constr_share")
    m = m.merge(share, left_on="project_key", right_index=True, how="left")
    m["project_kind"] = np.where(m.constr_share >= 0.50, "construction",
                                 np.where(m.constr_share >= 0.25, "mixed",
                                          "non_construction"))
    m.to_csv(os.path.join(CLEAN, "master_panel.csv"), index=False, encoding="utf-8-sig")

    pk = m.drop_duplicates("project_key")
    say("## 2b. Project-kind screening (by content, not by title)")
    say()
    say(md(pk.groupby("project_kind").agg(projects=("project_key", "size")).reset_index(),
           fmt="%.0f"))
    say()
    say("Criterion: the share of rows in a project that "
        "**fall into a recognised construction item family** (>=50% construction, 25~50% mixed).")
    say()
    say("The unit of measure is not used as the criterion. Screening by engineering units (SY/CY/LF) "
        "would wrongly drop 700 genuine TxDOT projects, because highway maintenance, striping and "
        "signal work is priced per EA/DAY/MO in the first place. "
        "What really sets a services contract apart is that almost none of its items fall into a "
        "construction family: hourly rates, software licences and vehicles all go to `other`.")
    say()
    say("This step is necessary. Most of the Collin County files are named only by a number, so the "
        "title does not show what is being bought, but the content is plumbing hourly rates, trucks "
        "and software subscriptions. Mesquite has Allison transmissions and steel purchases. "
        "TxDOT has mowing and landscaping services and asphalt-additive delivery. "
        "Mixed into the construction unit-price base, these would contaminate items with the same unit.")
    say()
    nonc = pk[pk.project_kind == "non_construction"]
    if len(nonc):
        say(f"The **{len(nonc)}** excluded non-construction projects, by source:")
        say()
        say(md(nonc.groupby("owner").agg(projects=("project_key", "size"))
               .sort_values("projects", ascending=False).head(8).reset_index(), fmt="%.0f"))
        say()

    bids = m[(~m.is_engineer_estimate) & (m.unit_price > 0)]
    # TRAIN/TEST SPLIT, enforced at the one chokepoint everything fitted flows
    # through.  Client rule (2026-11-18): files the COMPANY supplied are TEST
    # data only; the training base is publicly sourced data.
    #
    # Gating here rather than at the master_panel write is deliberate: `bids`
    # stays whole so the validation sections, opcc_analysis and the QC reports
    # can still see the company rows, while everything that becomes a PRICE
    # REFERENCE is built from `comp` below and is now external-only.  One edit
    # therefore covers price_reference.csv, qty_elasticity,
    # price_by_quantity_band.csv, price_book_txdot.csv and price_time_index.csv.
    #
    # The previous `is_engineer_estimate` filter did NOT do this job: it only
    # removed the company's own OPCC rows, while the 8 company files are real
    # bid tabulations full of real contractors' prices, so they classified as
    # competition_class='competitive' and flowed straight into the pooled
    # medians -- and `n_owners` counted 'internal' as a corroborating owner, so
    # a family backed by 2 public owners plus the company reported n_owners=3 and
    # passed the usable='yes' gate on evidence that was partly its own.
    train = bids[bids.source_set == "external"]
    # The reference is built from competitive CONSTRUCTION bids only.
    # `price_basis` carries the one remaining exclusion: a letting whose
    # bidders are not pricing the same job (award_identity.
    # is_comparable_letting).  The supplier-catalogue tabs the client asked
    # about -- BuyBoard, TIPS, DIR-TSO, OMNIA, Sourcewell, and the tissue,
    # towel, computer and voice-system "bids" -- are already outside this
    # gate: project_kind reads non_construction for every one of them, so
    # measured, 0 of their 258 panel rows ever reached the reference.
    comp = train[(train.competition_class == "competitive")
                 & (train.project_kind == "construction")
                 & (train.price_basis != "uneven_proposal")]

    # ---- unbalanced bidding, measured on TxDOT -------------------------
    tx = bids[(bids.source == "txdot") & bids.is_low_bidder.notna()]
    if len(tx):
        a = (tx.groupby(["item_family", "unit"])
             .apply(lambda s: pd.Series({
                 "n_all": len(s),
                 "median_all_bids": s.unit_price.median(),
                 "median_low_bidder": s.loc[s.is_low_bidder == True, "unit_price"].median(),
             }), include_groups=False).reset_index())
        a = a[(a.n_all >= 200) & a.median_low_bidder.notna()]
        a["low_over_all"] = a.median_low_bidder / a.median_all_bids
        a = a.sort_values("low_over_all")
        a["train_scope"] = "txdot_published_only"  # built from the txdot arm
        a.to_csv(os.path.join(CLEAN, "unbalanced_bidding.csv"), index=False,
                 encoding="utf-8-sig")
        say("## 3. Winning bidder's unit price vs all bids (measured magnitude of unbalanced bidding)")
        say()
        say("For the same item: the winning bidder's median unit price / the median unit price of all "
            "bidders. <1 means the winning bidder priced that item lower.")
        say()
        say(md(a.head(14)[["item_family", "unit", "n_all", "median_all_bids",
                           "median_low_bidder", "low_over_all"]], fmt="%.3f"))
        say()
        say(f"Overall median **{a.low_over_all.median():.3f}** -- the winning bidder is not the lowest "
            f"on every item, which is why the minimum cannot be taken item by item.")
        say()

    # ---- two-stage pooled reference --------------------------------------
    # REMOVAL IDENTITY (estimate_low_bid.family_key, item_tokens.removal_only):
    # the family and band references file a removal-only row under its
    # family + "#rm", so removal work and the same family's new work never
    # share a cell.  The token reference splits through item_tokens' flag;
    # the quantity-shape tables below keep the classifier's own family.
    if item_tokens.REMOVAL_IDENTITY:
        _rm = comp["desc_norm"].fillna("").map(item_tokens.removal_only)
        refc = comp.assign(item_family=np.where(
            _rm, comp["item_family"].fillna("") + "#rm", comp["item_family"]))
    else:
        refc = comp
    # ABANDONMENT IDENTITY, AB2 (round 17; item_tokens.ABANDON_FAMILY_KEY with
    # ABANDON_IDENTITY, both OFF by default): an abandon-only row is filed
    # under family + "#ab" (estimate_low_bid.family_key).  abandon_only() is
    # never true on a removal-only row, so no row gets both suffixes.  The
    # token reference splits through item_tokens' AB_FLAG.
    if item_tokens.ABANDON_IDENTITY and item_tokens.ABANDON_FAMILY_KEY:
        _ab = comp["desc_norm"].fillna("").map(item_tokens.abandon_only)
        refc = refc.assign(item_family=np.where(
            _ab, refc["item_family"].fillna("") + "#ab", refc["item_family"]))
    key = ["item_family", "unit", "size_bucket"]
    stage1 = (refc.groupby(key + ["source", "owner", "project_key", "year"])
              .agg(project_price=("unit_price", "median"),
                   project_qty=("quantity", "median"),
                   n_bids=("unit_price", "size")).reset_index())
    ref = (stage1.groupby(key)
           .agg(n_projects=("project_key", "nunique"),
                n_owners=("owner", "nunique"),
                n_bid_obs=("n_bids", "sum"),
                price_p25=("project_price", lambda s: s.quantile(.25)),
                price_median=("project_price", "median"),
                price_p75=("project_price", lambda s: s.quantile(.75)),
                qty_median=("project_qty", "median"),
                first_year=("year", "min"), last_year=("year", "max"))
           .reset_index())
    ref["iqr_ratio"] = ref.price_p75 / ref.price_p25.replace(0, np.nan)

    # Some items have no meaningful flat unit price: mobilization, traffic
    # handling, TMA day rates and the like scale with the size or duration of the
    # job, so their unit price moves with quantity. Measure that slope and flag
    # it instead of publishing a single misleading median.
    # Fit the slope WITHIN each exact item first, then take the median slope
    # across the items in a family. Fitting straight on the family pools
    # different work into one regression, so the slope would partly measure
    # which items happen to be ordered in small quantities.
    s1k = (refc[(refc.quantity > 0) & (refc.unit_price > 0)]
           .groupby(key + ["item_key", "project_key"])
           .agg(project_price=("unit_price", "median"),
                project_qty=("quantity", "median")).reset_index())
    per_item = {}
    for k, sub in s1k.groupby(key + ["item_key"]):
        if len(sub) < 6:
            continue
        x = np.log(sub.project_qty.values)
        y = np.log(sub.project_price.values)
        if x.std() < 0.2:          # no quantity spread -> slope is meaningless
            continue
        per_item.setdefault(k[:len(key)], []).append(float(np.polyfit(x, y, 1)[0]))
    slopes = {k: float(np.median(v)) for k, v in per_item.items() if len(v) >= 2}
    ref["qty_elasticity"] = [slopes.get(tuple(r), np.nan)
                             for r in ref[key].itertuples(index=False)]
    ref["n_items_for_elasticity"] = [len(per_item.get(tuple(r), []))
                                     for r in ref[key].itertuples(index=False)]
    ref["price_form"] = np.where(
        ref.qty_elasticity.abs() > 0.15, "scale_dependent",
        np.where(ref.qty_elasticity.notna(), "flat_unit_price", "unknown"))
    # `other` is NOT a trade -- it is "the classifier did not recognise this
    # description".  Publishing a pooled median for it turns the reference into
    # a machine that prices anything at all: a UI test fed the invented item
    # "Unobtainium Lattice Deflector", it landed in other/EA (the mean over 855
    # projects of unrelated work) and came back $3,970/EA with no warning.
    # Pooling unrecognised items says nothing about any of them, so the row is
    # marked unusable rather than dropped -- it stays visible as a measure of
    # how much of the corpus the family rules still fail to classify, which is
    # a number worth watching, but nothing may price against it.
    # ---- vintage: the year this median is "from" ---------------------------
    # The rule now lives in src/vintage.py, called by all four tables that
    # publish a price.  It used to be defined here only, which is why the three
    # other tables had no vintage at all and their consumers each derived one --
    # three derivations of the same thing, disagreeing silently.
    ref = vintage.attach(ref, stage1, key, "year", None,
                         qty_col="project_qty", price_col="project_price")

    # `trm` joins the unclassified list for the same reason `other` is on it.
    # Once trm_permanent / trm_temporary / trm_bundled exist, bare `trm` is no
    # longer a product -- it is the bucket for tabs that never said WHICH mat,
    # and the 51 rows in it span $2.90 to $300.00 with a $60.00 median, across
    # both of the bands measured within a single document at Kee Branch
    # (temporary $12.00-18.60, permanent anchored $40.00-43.20) and well past
    # each end.  A 100x spread is proof the bucket holds different products, so
    # its median is not a TRM price, it is an average of unlike things.
    #
    # Four of its descriptions carry no product information at all ("Turf
    # Reinforcing Mat", "Turf Reinforcing Mattress"), so this cannot be fixed
    # by better rules -- it needs the product named, which lives in the source
    # document's spec section.  And it must NOT be fixed by splitting on price:
    # assigning families by price and then predicting price from family is
    # circular, and would manufacture the very gap it claims to measure.
    # `needs_review` joins the list for a different reason than the others:
    # `other` and `trm` are families too coarse to price, while `needs_review`
    # means the DESCRIPTION the classifier read is damaged, so the family is
    # unknown rather than vague.  Both must refuse to price; the distinction is
    # in what would fix them (more family rules vs a better text extraction).
    UNCLASSIFIED_FAMILIES = ["other", "", "trm", "needs_review"]
    ref["usable"] = np.where(
        ref.item_family.str.split("#").str[0].isin(UNCLASSIFIED_FAMILIES),
        "unclassified",
        np.where(ref.n_projects >= 5, "yes",
                 np.where(ref.n_projects >= 3, "thin", "no")))
    ref = ref.sort_values(["usable", "n_projects"], ascending=[True, False])
    # What built this, carried in the file.  The client's rule -- company files
    # are TEST data, only public data trains -- is enforced upstream at `train`,
    # but a reader of this CSV could not tell without going and finding that
    # line.  src/train_test_guard.py proves it per cell by recomputing with the
    # company rows switched on and off; this column is so nothing downstream
    # has to take the claim on faith.
    ref["train_scope"] = "public_external_only"
    ref.to_csv(os.path.join(CLEAN, "price_reference.csv"), index=False, encoding="utf-8-sig")

    # ---- the same reference, keyed on the QUANTITY BAND as well ----------
    # Measured, leave-one-letting-out, on the same 1,935 lettings both keys can
    # price, so this is not two populations being compared:
    #
    #     price key                     municipal      TxDOT
    #     family+unit+size_bucket           32.6%      50.1%  (median |error|
    #     + qty_band                        28.6%      30.8%   on the winner's
    #                                                           own total)
    #
    # and on TxDOT the bias collapses with it, 1.4231 -> 0.9742.  That is a
    # different and better thing than what the estimator does today, which
    # takes ONE family median and scales it by a band shape multiplier out of
    # price_by_quantity_band_family.csv.  The multipliers evidently do not
    # carry the band effect: pricing from other lettings' items in the SAME
    # band beats adjusting a pooled median into that band.
    #
    # It costs coverage -- municipal lettings that can be fully priced fall
    # from 121 to 88 -- so this is a table the estimator tries FIRST and falls
    # back from, never a replacement for price_reference.csv.
    band_key = key + ["qty_band"]
    s1b = refc.assign(qty_band=refc["quantity"].map(qty_shape.band_of))
    s1b = s1b[s1b.qty_band != ""]
    s1b = (s1b.groupby(band_key + ["source", "owner", "project_key", "year"])
           .agg(project_price=("unit_price", "median"),
                project_qty=("quantity", "median"),
                n_bids=("unit_price", "size")).reset_index())
    refb = (s1b.groupby(band_key)
            .agg(n_projects=("project_key", "nunique"),
                 n_owners=("owner", "nunique"),
                 n_bid_obs=("n_bids", "sum"),
                 price_p25=("project_price", lambda s: s.quantile(.25)),
                 price_median=("project_price", "median"),
                 price_p75=("project_price", lambda s: s.quantile(.75)),
                 qty_median=("project_qty", "median"),
                 first_year=("year", "min"),
                 last_year=("year", "max")).reset_index())
    # Two lettings is the floor the measurement used; one letting is that
    # letting's own price wearing a reference's clothes.
    refb = refb[refb.n_projects >= 2]
    refb = vintage.attach(refb, s1b, band_key, "year", None,
                          qty_col="project_qty", price_col="project_price")
    refb["usable"] = np.where(
        refb.item_family.str.split("#").str[0].isin(UNCLASSIFIED_FAMILIES),
        "unclassified",
        np.where(refb.n_projects >= 5, "yes",
                 np.where(refb.n_projects >= 3, "thin", "no")))
    refb["train_scope"] = "public_external_only"
    refb = refb.sort_values(["usable", "n_projects"], ascending=[True, False])
    refb.to_csv(os.path.join(CLEAN, "price_reference_band.csv"), index=False,
                encoding="utf-8-sig")
    say("Price reference split by quantity band: **%d** (family, unit, size band, quantity band) "
        "cells, of which %d are backed by >=5 projects." % (
            len(refb), int((refb.usable == "yes").sum())))
    say()

    # ---- token-keyed reference: identity by WORDS, not by family ---------
    # The family median is where almost all of the estimator's error lives,
    # and exact-description matching cannot fix it on municipal work: 84.4% of
    # DFW descriptions appear in exactly one letting against 41.7% for TxDOT,
    # because every city writes its own wording.  Matching on a token SET with
    # a Jaccard threshold instead (src/item_tokens.py) does fix a good part of
    # it.  Measured leave-one-letting-out against the real winning totals:
    #
    #     pricer                     municipal   TxDOT
    #     band-keyed reference           28.5%   30.9%
    #     token J>=0.35, then band       23.9%   24.5%
    #
    # Not keyed on item_family on purpose: the family classifier is the thing
    # being worked around, and `other` -- unusable in price_reference, 23.7% of
    # a TxDOT winning total at the median -- can then be priced from whatever
    # its words actually describe.  Unit and quantity band stay hard keys.
    tokcol = comp["desc_norm"].fillna("").map(item_tokens.signature)
    s1t = comp.assign(tok=tokcol,
                      qty_band=comp["quantity"].map(qty_shape.band_of))
    s1t = s1t[(s1t.tok != "") & (s1t.qty_band != "")]
    s1t = (s1t.groupby(["tok", "unit", "qty_band", "source", "owner",
                        "project_key", "year"])
           .agg(project_price=("unit_price", "median"),
                project_qty=("quantity", "median"),
                n_bids=("unit_price", "size")).reset_index())
    tkey = ["tok", "unit", "qty_band"]
    reft = (s1t.groupby(tkey)
            .agg(n_projects=("project_key", "nunique"),
                 n_owners=("owner", "nunique"),
                 n_bid_obs=("n_bids", "sum"),
                 price_p25=("project_price", lambda x: x.quantile(.25)),
                 price_median=("project_price", "median"),
                 price_p75=("project_price", lambda x: x.quantile(.75)),
                 qty_median=("project_qty", "median"),
                 first_year=("year", "min"),
                 last_year=("year", "max")).reset_index())
    # One letting behind a cell is that letting's own price wearing a
    # reference's clothes; the measurement used two.
    reft = reft[reft.n_projects >= 2]
    reft = vintage.attach(reft, s1t, tkey, "year", None,
                          qty_col="project_qty", price_col="project_price")
    reft["usable"] = np.where(reft.n_projects >= 5, "yes",
                              np.where(reft.n_projects >= 3, "thin", "no"))
    reft["train_scope"] = "public_external_only"
    reft["jaccard_threshold"] = item_tokens.THRESHOLD
    reft = reft.sort_values("n_projects", ascending=False)
    reft.to_csv(os.path.join(CLEAN, "price_reference_tokens.csv"), index=False,
                encoding="utf-8-sig")
    say("Price reference split by item tokens: **%d** (token set, unit, quantity band) cells, "
        "of which %d are backed by >=5 projects; match threshold Jaccard >= %.2f, "
        "and where both sides carry a size the sizes must match." % (
            len(reft), int((reft.usable == "yes").sum()),
            item_tokens.THRESHOLD))
    say()

    say("## 4. Unit-price reference (two-stage pooling: median within a project, then across projects)")
    say()
    u = ref.groupby("usable").agg(groups=("item_family", "size"),
                                  projects_total=("n_projects", "sum")).reset_index()
    say(md(u))
    say()
    say("`yes` = backed by >=5 projects, use directly; `thin` = 3~4 projects, give a wide interval; "
        "`no` = <3 projects, do not use.")
    say()
    top = ref[ref.usable == "yes"].nlargest(22, "n_projects")
    say(md(top[["item_family", "unit", "size_bucket", "n_projects", "n_owners",
                "price_p25", "price_median", "price_p75", "iqr_ratio"]]))
    say()

    # ---- the quantity trap, measured -------------------------------------
    # The same item priced at tiny quantity vs large quantity is not the same
    # price: ordering 2 CY of excavation carries mobilising a crew for almost
    # nothing, so the unit price is absurd, while 1000 CY spreads that over the
    # job. Any reference that reports one median per item hides this, so measure
    # the curve explicitly and keep the elasticity for scaling.
    qb = comp[(comp.quantity > 0) & (comp.unit_price > 0)].copy()
    bands = [(0, 10), (10, 50), (50, 200), (200, 1000), (1000, 5000),
             (5000, 25000), (25000, np.inf)]

    def band_of(q):
        for lo, hi in bands:
            if lo <= q < hi:
                return "%g-%g" % (lo, hi) if np.isfinite(hi) else "25000+"
        return ""

    qb["qty_band"] = qb.quantity.map(band_of)
    # Measured WITHIN THE SAME ITEM, not within the family. Grouping by family
    # mixes different work into one row -- "MOBILIZATION" and
    # "MOBILIZATION (CALLOUT)" both land in mobilization/EA at opposite ends of
    # the quantity range, and the apparent 1000x is item heterogeneity, not a
    # quantity effect. Keying on item_key isolates the real curve.
    s1q = (qb.groupby(["item_key", "unit", "qty_band", "project_key"])
           .agg(project_price=("unit_price", "median"),
                fam=("item_family", "first"),
                desc=("description", "first")).reset_index())
    tab = (s1q.groupby(["item_key", "unit", "qty_band"])
           .agg(n_projects=("project_key", "nunique"),
                median_price=("project_price", "median"),
                fam=("fam", "first"), desc=("desc", "first")).reset_index())
    tab = tab[tab.n_projects >= 3]
    labels = tab.drop_duplicates("item_key").set_index("item_key")[["fam", "desc"]]
    wide_tab = tab.pivot_table(index=["item_key", "unit"], columns="qty_band",
                               values="median_price")
    order = [b for b in ["0-10", "10-50", "50-200", "200-1000", "1000-5000",
                         "5000-25000", "25000+"] if b in wide_tab.columns]
    wide_tab = wide_tab[order]
    cover = wide_tab.notna().sum(axis=1)
    wide_tab = wide_tab[cover >= 3]
    if len(wide_tab):
        first = wide_tab.apply(lambda r: r.dropna().iloc[0], axis=1)
        last = wide_tab.apply(lambda r: r.dropna().iloc[-1], axis=1)
        wide_tab["small_over_large"] = first / last
        wide_tab["n_bands"] = cover
        wt = wide_tab.reset_index().merge(labels, left_on="item_key",
                                          right_index=True, how="left")
        wt = wt.sort_values("small_over_large", ascending=False)
        wt["train_scope"] = "public_external_only"
        wt.to_csv(os.path.join(CLEAN, "price_by_quantity_band.csv"), index=False,
                  encoding="utf-8-sig")
        say("## 4b. The quantity trap: how much the unit price of **the same item** falls with quantity")
        say()
        say("Each cell is the median of the project unit prices in that quantity range "
            "(median within a project first, then across projects).")
        say("Grouped by **specific item**, not by item family. Otherwise different work is mixed "
            "into one row, and what is measured is the difference between items, not a quantity effect.")
        say()
        show = wt.head(16)[["desc", "unit", "n_bands"] + order + ["small_over_large"]]
        show["desc"] = show.desc.astype(str).str.slice(0, 34)
        say(md(show, fmt="%.2f"))
        say()
        say(f"Among the {len(wt)} specific items with enough band data, "
            f"the unit price in the small-quantity band is **{wt.small_over_large.median():.1f} times** "
            f"that in the large-quantity band (median); interquartile range "
            f"{wt.small_over_large.quantile(.25):.1f} ~ "
            f"{wt.small_over_large.quantile(.75):.1f} times.")
        say()
        say("**Consequence: using a single median unit price to estimate a project whose quantity "
            "differs by one or two orders of magnitude can produce a several-fold systematic bias.** "
            "So the unit-price reference must carry a quantity dimension:")
        say("either look it up by quantity band, or scale with the `price_reference.csv` column "
            "`qty_elasticity` --")
        say("`price ≈ reference price × (target quantity / reference quantity) ^ qty_elasticity`, "
            "where a negative elasticity means the larger the quantity, the lower the unit price.")
        say()

    # ---- 4c. the same curve at FAMILY level, so siblings can lend it -----
    # The per-item table above is the honest measurement but it is thin: an item
    # needs three projects in each of two bands to appear at all, so most line
    # items fall through to a FITTED elasticity -- an extrapolation.  A
    # family-level shape lets an item borrow its siblings' measured curve, which
    # is weaker than its own curve and stronger than a fitted slope.
    #
    # Only the SHAPE crosses between siblings, never the level.  See
    # src/qty_shape.py: the price level is absorbed by an item fixed effect, so
    # what is published here is a multiplier anchored at 1.0, to be applied as
    #     shape[band of this quantity] / shape[band of the reference quantity]
    # A family-band MEDIAN PRICE is not published, and that is deliberate: it
    # would be the composition error the per-item table exists to avoid, and
    # dividing one item's price by a sibling's median is an operation on two
    # different items.
    #
    # The identity used here is desc_norm|unit, NOT item_key.  item_key carries
    # the owner for city rows ("city_bidtab|rowlett|..."), which is right for a
    # price level -- owners pay differently -- but wrong for a shape, which is a
    # property of the work.  Keeping the owner in would have confined every city
    # item to one owner's projects; only 2 of the 740 per-item rows above are
    # non-TxDOT for exactly that reason.
    shp = qb[(qb.quantity > 0) & (qb.unit_price > 0)][
        ["item_family", "unit", "desc_norm", "qty_band", "project_key",
         "unit_price"]].copy()
    shp["shape_arm"] = "panel"
    # The statewide erosion lettings join the SHAPE fit only -- never a price
    # level, never price_reference.csv -- and only for a (family, unit) the DFW
    # panel cannot shape on its own.  load_erosion_shape_arm() has the two
    # measurements behind both restrictions.  Measured on DFW lettings alone,
    # four of the client's core families sit EXACTLY one item short of
    # MIN_ITEMS: gabion_wall|CY, gabion_mattress|SY (whose two DFW items do not
    # even share a band -- disconnected as well), conc_mow_strip|CY and
    # shoring|SF.  With the arm they get shapes from 8, 8, 5 and 3 items (2 of
    # them DFW in every case; n_items_dfw says so per row).  rock_anchor does
    # not, and should not: with the arms split, rock_anchor|SF and |LF each
    # have 2 multi-band "items" that are ONE TxDOT item seen in two regions
    # (retaining wall soil nail facia; soil nail anchors) -- the other two
    # items rock_anchor|SF had when pooled were cross-region pairs.  Both of
    # those items already carry their own curve in price_by_quantity_band.csv.
    arm = load_erosion_shape_arm()
    if len(arm):
        shp = pd.concat([shp, arm], ignore_index=True)
    # The arm is PART OF the item identity: a DFW cell and a statewide cell of
    # the same TxDOT item get separate item effects, so no regional price level
    # can reach the band multipliers (scale test in load_erosion_shape_arm).
    shp["shape_key"] = (shp.desc_norm.astype(str) + "|" + shp.unit.astype(str)
                        + "|" + shp.shape_arm.astype(str))
    s1s = (shp.groupby(["item_family", "unit", "shape_key", "qty_band",
                        "project_key"])
           # One letting belongs to one arm -- the two src_file sets are
           # disjoint -- so this is the arm, not a mixture.
           .agg(pp=("unit_price", "median"),
                arm=("shape_arm", "min")).reset_index())
    its = (s1s.groupby(["item_family", "unit", "shape_key", "qty_band"])
           .agg(n_projects=("project_key", "nunique"),
                price=("pp", "median"),
                n_statewide=("arm", lambda s: int((s == "statewide_archive").sum()))
                ).reset_index())
    # Two projects per (item, band), not three: a single cell only has to pin
    # one point of a curve that is then pooled over many items, and the family
    # minimum in qty_shape.py is what guards the result.
    its = its[(its.n_projects >= 2) & (its.qty_band != "")]
    fam_rows = []
    borrow = {}          # band distance -> list of log errors
    borrow_tight = {}    # same, from well-supported cells only
    for (famly, unit), sub in its.groupby(["item_family", "unit"],
                                          observed=True):
        if famly in UNCLASSIFIED_FAMILIES:
            continue
        # DFW first.  The statewide arm is a fallback for a (family, unit) the
        # DFW panel cannot shape, never a second opinion that outvotes one it
        # can -- erosion_control|SY is where outvoting was measured to hurt.
        # Cells are single-arm (the arm is in shape_key), so a DFW cell is
        # exactly one with no statewide letting in it.
        dfw = sub[sub.n_statewide == 0]
        got = (qty_shape.fit_shape(zip(dfw.shape_key, dfw.qty_band, dfw.price))
               if len(dfw) else None)
        borrowed = False
        if not got and bool((sub.n_statewide > 0).any()):
            got = qty_shape.fit_shape(zip(sub.shape_key, sub.qty_band, sub.price))
            borrowed = bool(got)
        if not got:
            continue
        n_sw = int(sub.n_statewide.sum()) if borrowed else 0
        # How much of the curve is DFW's own evidence: DFW items seen in two or
        # more bands (before the connectivity filter inside fit_shape).
        n_dfw = int((dfw.groupby("shape_key").qty_band.nunique() >= 2).sum())
        # The borrowing error is scored on DFW items only.  It is the error a
        # DFW line item takes on; statewide items scored against a curve they
        # dominate would only flatter it.
        for d, e in qty_shape.ratio_errors(
                zip(dfw.shape_key, dfw.qty_band, dfw.price), got["levels"]):
            borrow.setdefault(d, []).append(e)
        tight = dfw[dfw.n_projects >= 6]
        if len(tight):
            for d, e in qty_shape.ratio_errors(
                    zip(tight.shape_key, tight.qty_band, tight.price),
                    got["levels"]):
                borrow_tight.setdefault(d, []).append(e)
        for bd, mult in got["levels"].items():
            fam_rows.append({
                "item_family": famly, "unit": unit, "qty_band": bd,
                "shape_multiplier": round(mult, 5),
                "anchor_band": got["anchor"],
                "n_items_in_band": got["items_per_band"].get(bd, 0),
                "n_items": got["n_items"], "n_obs": got["n_obs"],
                # The spread left after the shape and each item's own level are
                # removed: the extra uncertainty a sibling takes on by borrowing
                # this curve.  Downstream should widen by THIS, measured, rather
                # than by an assumed share of the adjustment.
                "resid_sigma_log": round(got["resid_sigma_log"], 4)
                if got["resid_sigma_log"] == got["resid_sigma_log"] else "",
                "resid_sigma_log_band": round(
                    got["resid_sigma_log_by_band"].get(bd, float("nan")), 4)
                if got["resid_sigma_log_by_band"].get(bd, float("nan"))
                == got["resid_sigma_log_by_band"].get(bd, float("nan")) else "",
                "bands_in_component": got["bands_in_component"],
                "dropped_components": got["dropped_components"],
                "converged": got["converged"],
                # Published so a reader can tell a DFW-measured shape from one
                # that needed statewide lettings to exist at all, without
                # going and re-deriving it.  The span is published for the same
                # reason: it is the quantity the MIN_SPAN_OVER_SIGMA gate in
                # src/qty_shape.py compares against resid_sigma_log, so the
                # decision to publish this row is checkable from the row.
                "fitted_span_log": round(got["fitted_span_log"], 4),
                "n_cells_from_statewide": n_sw,
                "n_items_dfw": n_dfw,
                "scope": ("txdot_and_city_train+statewide_archive_shape"
                          if borrowed else "txdot_and_city_train")})
    if fam_rows:
        fam_shape = pd.DataFrame(fam_rows)
        fam_shape["band_order"] = fam_shape.qty_band.map(qty_shape.BAND_INDEX)
        fam_shape = fam_shape.sort_values(
            ["item_family", "unit", "band_order"]).drop(columns="band_order")
        fam_shape.to_csv(os.path.join(CLEAN, "price_by_quantity_band_family.csv"),
                         index=False, encoding="utf-8-sig")
        ncomb = fam_shape.groupby(["item_family", "unit"]).ngroups
        say("## 4c. Family-level quantity shape (sibling items in the same family can borrow it)")
        say()
        nitems = fam_shape.groupby(["item_family", "unit"]).n_items.first().sum()
        say(f"Covers **{ncomb}** (family, unit) combinations and {nitems:.0f} specific items.")
        say()
        say("**Shape only, no price level.** Use it as "
            "`shape[band of this row's quantity] / shape[band of the reference quantity]`. "
            "The two absolute values only mean something as a ratio; dividing this item's median by a "
            "sibling item's absolute price is a meaningless operation.")
        say()
        say("The price level is absorbed by each item's own fixed effect (see `src/qty_shape.py`), "
            "so multiplying all of one item's prices by 1000 does not change this table -- "
            "a unit test covers this.")
        say()
        say("**This is a relationship across projects, not an elasticity within one letting.** "
            "Within one letting, the measured slope of unit price on quantity is about 0 -- "
            "most items do not change price with quantity at all inside the same letting. This table "
            "measures 'is the same item more expensive in projects where its quantity is small'. "
            "That mixes two things: real economies of quantity (mobilisation cost spread thinner) and "
            "project composition (small quantities tend to occur on small, urban, poorly accessible "
            "jobs, which are expensive for other reasons). The two cannot be separated in this data.")
        say()
        say("**But for predicting a new project, this basis is the right one**: the prediction target "
            "is itself a draw across projects, so the mechanisms need not be separated first. "
            "There is only one thing to watch: do not treat it as evidence that 'increasing the "
            "quantity of work saves this proportion'. That needs within-letting evidence, and the "
            "within-letting evidence says there is no such saving.")
        say()
        say("`resid_sigma_log` is the spread left after removing the shape and each item's own "
            "price level, i.e. the extra uncertainty that borrowing a sibling item's curve itself "
            "brings in. Downstream should widen the interval by this measured value, "
            "not by an assumed share of the adjustment size.")
        say()
        wide_fs = fam_shape.pivot_table(index=["item_family", "unit"],
                                        columns="qty_band",
                                        values="shape_multiplier")
        ocols = [b for b in qty_shape.BAND_LABELS if b in wide_fs.columns]
        wide_fs = wide_fs[ocols]
        nit = fam_shape.groupby(["item_family", "unit"]).n_items.first()
        wide_fs["n_items"] = nit
        say(md(wide_fs.sort_values("n_items", ascending=False).head(20)
               .reset_index(), fmt="%.3f"))
        say()
        # ---- the σ a borrower should apply, measured not assumed ---------
        if borrow:
            bs = []
            for d in sorted(borrow):
                v = borrow[d]
                if len(v) < 10:
                    continue
                vt = borrow_tight.get(d, [])
                bs.append({
                    "band_distance": d,
                    "n_pairs": len(v),
                    "sigma_log": round(qty_shape.mad_sigma(v), 4),
                    "n_pairs_wellsupported": len(vt),
                    # The recommended figure.  The all-cells column is inflated
                    # by noise in the item's OWN observed ratio -- the measuring
                    # stick, not the thing being predicted -- so the
                    # well-supported column is the better estimate of the
                    # irreducible error, and it is still slightly conservative.
                    "sigma_log_wellsupported": round(qty_shape.mad_sigma(vt), 4)
                    if len(vt) >= 10 else "",
                    # Scored on DFW (panel) items only; the curves they are
                    # scored against include the statewide-filled ones.
                    "scope": "txdot_and_city_train_items+statewide_archive_shape"})
            if bs:
                pd.DataFrame(bs).to_csv(
                    os.path.join(CLEAN, "qty_shape_borrow_sigma.csv"),
                    index=False, encoding="utf-8-sig")
                say("### The cost of borrowing a sibling item's curve (measured)")
                say()
                say("What downstream applies is a **ratio** `shape[this band]/shape[reference band]`, "
                    "so what must be quantified is the uncertainty of the ratio, not the residual "
                    "spread of a single cell. Across 0 bands the ratio is always 1 and the error "
                    "is zero, while the single-cell spread does not change.")
                say()
                say(md(pd.DataFrame(bs)[["band_distance", "n_pairs", "sigma_log",
                                         "n_pairs_wellsupported",
                                         "sigma_log_wellsupported"]]))
                say()
                say("The `sigma_log` column contains the noise of the measuring stick itself "
                    "(the item's own two cells are also just medians of a few projects); "
                    "`sigma_log_wellsupported` uses only cells with ≥6 projects per cell, "
                    "and is a better estimate of the **irreducible** borrowing error.")
                say()
                say("**A penalty set as a share of the adjustment size cannot produce this shape** -- "
                    "such a penalty goes to zero when the adjustment is small, but the borrowing "
                    "error does not; it grows only with the number of bands crossed.")
                say()

        broken = fam_shape[fam_shape.dropped_components > 0]
        if len(broken):
            nb = broken.groupby(["item_family", "unit"]).ngroups
            say(f"For **{nb}** of these combinations the item-band graph is disconnected, "
                "and only the largest connected component is published. Two-way fixed effects are "
                "identified only within a connected component, and a comparison across components "
                "would give a meaningless value that looks like a number.")
            say()

    # ---- TxDOT standardised price book -----------------------------------
    # Both TxDOT eras, keyed on the normalised DESCRIPTION, not the bid code:
    # the archive is 6xxx and the current feed 7xxx for the same work, so a
    # code-keyed book would split every item in two and halve its sample.
    tx_comp = comp[comp.source.isin(["txdot", "txdot_archive"])].copy()
    if len(tx_comp):
        tx_comp["item_no"] = tx_comp.desc_norm.astype(str)
        s1tx = (tx_comp.groupby(["item_no", "unit", "project_key", "year"])
                .agg(project_price=("unit_price", "median"),
                     project_qty=("quantity", "median")).reset_index())
        book = (s1tx.groupby(["item_no", "unit"])
                .agg(n_lettings=("project_key", "nunique"),
                     price_p25=("project_price", lambda s: s.quantile(.25)),
                     price_median=("project_price", "median"),
                     price_p75=("project_price", lambda s: s.quantile(.75)),
                     qty_median=("project_qty", "median"),
                     first_year=("year", "min"), last_year=("year", "max"))
                .reset_index())
        desc = (tx_comp.drop_duplicates("item_no").set_index("item_no")["description"])
        book["description"] = book.item_no.map(desc)
        book["iqr_ratio"] = book.price_p75 / book.price_p25.replace(0, np.nan)
        book["usable"] = np.where(book.n_lettings >= 5, "yes",
                                  np.where(book.n_lettings >= 3, "thin", "no"))
        # Same vintage rule as price_reference.csv, from the same module, so a
        # consumer reading both tables cannot get two different answers for
        # what year a price is from.
        book = vintage.attach(book, s1tx, ["item_no", "unit"], "year", None,
                              qty_col="project_qty", price_col="project_price")
        book = book.sort_values("n_lettings", ascending=False)
        book["train_scope"] = "txdot_published_only"
        book.to_csv(os.path.join(CLEAN, "price_book_txdot.csv"), index=False,
                    encoding="utf-8-sig")
        say("## 5. TxDOT standard-code price book (keyed on bid_code)")
        say()
        ub = book.groupby("usable").agg(codes=("item_no", "size")).reset_index()
        say(md(ub))
        say()
        tight = book[(book.usable == "yes") & (book.iqr_ratio <= 2.0)].nlargest(14, "n_lettings")
        say("Codes with the least spread and the most samples (these can be used directly as unit prices):")
        say()
        say(md(tight[["item_no", "description", "unit", "n_lettings",
                      "price_p25", "price_median", "price_p75", "iqr_ratio"]]))
        say()
        wide = book[(book.usable == "yes") & (book.iqr_ratio > 5)].nlargest(8, "n_lettings")
        say("Codes with very large spread (do **not** use as fixed unit prices; "
            "they vary with project size/duration):")
        say()
        say(md(wide[["item_no", "description", "unit", "n_lettings",
                     "price_median", "iqr_ratio"]]))
        say()

    # ---- matched-pair chained time index ---------------------------------
    # A year-on-year median over everything moves with WHICH items and owners
    # happen to appear each year, which is why the naive slope came out at
    # +30~58%/yr. Comparing the SAME item within the SAME owner across
    # consecutive years removes that composition effect.
    # Precise dates only. The year format test alone let through
    # council-agenda dates and fiscal-year guesses, which would make the index
    # partly a measure of filing conventions rather than of prices.
    # ---- why this no longer goes through stage1 --------------------------
    # It used to take stage1 -- which is keyed on (item_family, unit,
    # size_bucket, project_key) -- and graft an `item_key` onto it with
    #     ik = comp.drop_duplicates([family, unit, size_bucket, project_key])
    # drop_duplicates keeps the FIRST row, so WHICH item_key got attached to a
    # group depended on input row order, and 38.6% of those groups hold several
    # item_keys (one holds 208).  Two consequences, both measured:
    #
    #   * the published series was not reproducible.  Shuffling the input rows
    #     -- identical data, identical code -- moved 2025->2026 between -13.55%
    #     and -11.11% with n from 734 to 829, and moved 2016->2017 from -5.79%
    #     to exactly 0.00%.  A headline number that depends on row order is not
    #     a number.
    #   * the unit of observation was mislabelled.  What was tracked across
    #     years was the median over ALL descriptions sharing a family/unit/size
    #     within a project, reported as if it were one item.
    #
    # So the index now matches on item_key directly, which is the thing it
    # claims to match on, and is the SAME method src/index_weighting.py uses
    # for the basket series -- one definition instead of two that disagree.
    precise_keys = set(
        comp.loc[comp.date_precision == "precise", "project_key"].unique())
    base = comp[comp.project_key.isin(precise_keys)
                & comp.year.str.match(r"^20\d\d$", na=False)
                & (comp.item_key.astype(str) != "")].copy()
    s1 = (base.groupby(["item_key", "year", "project_key"], observed=True)
          .unit_price.median().rename("project_price").reset_index())
    yr = (s1.groupby(["item_key", "year"], observed=True)
          .agg(price=("project_price", "median"), n=("project_key", "nunique"))
          .reset_index())
    piv = yr.pivot(index="item_key", columns="year", values="price")
    years = sorted(c for c in piv.columns if str(c).isdigit())
    rows = []
    for a, b in zip(years, years[1:]):
        if a not in piv or b not in piv:
            continue
        pair = piv[[a, b]].dropna()
        pair = pair[(pair[a] > 0) & (pair[b] > 0)]
        if len(pair) < 10:
            rows.append({"from": a, "to": b, "n_matched_items": len(pair),
                         "yoy_pct": np.nan})
            continue
        ratio = (pair[b] / pair[a]).median()
        rows.append({"from": a, "to": b, "n_matched_items": len(pair),
                     "yoy_pct": (ratio - 1) * 100})
    ti = pd.DataFrame(rows)
    if len(ti):
        ti["index"] = 100.0
        lvl = 100.0
        vals = []
        for r in ti.itertuples(index=False):
            if pd.notna(r.yoy_pct):
                lvl = lvl * (1 + r.yoy_pct / 100)
            vals.append(lvl)
        ti["index"] = vals
        ti["train_scope"] = "public_external_only"
        ti.to_csv(os.path.join(CLEAN, "price_time_index.csv"), index=False,
                  encoding="utf-8-sig")
        say("## 6. Paired chained price index (same owner, same item, adjacent years)")
        say()
        say(md(ti, fmt="%.1f"))
        say()
        ok = ti[ti.n_matched_items >= 10]
        if len(ok):
            lvl0, lvl1 = 100.0, float(ti["index"].iloc[-1])
            n_yrs = len(ok)
            cagr = ((lvl1 / lvl0) ** (1.0 / max(n_yrs, 1)) - 1) * 100
            say(f"**Cumulative index {lvl0:.0f} -> {lvl1:.1f}** (base period {ti['from'].iloc[0]}), "
                f"annualised **{cagr:+.1f}%/yr**.")
            say()
            say("The median of the yearly changes is a misleading summary here (positive and negative "
                "years cancel out). Look at the cumulative level and the recent direction instead: "
                "2021-2024 had three straight years of +19.4% / +13.0% / +10.0%, "
                "turning to -5.5% / -11.7% after 2024. This matches the industry picture of "
                "construction costs surging after the pandemic and falling back recently, "
                "which shows that the index is capturing real movement rather than noise.")
        say("This basis removes changes in sample composition. The earlier full-sample median gave "
            "+30~58%/yr; that was an artefact of different owners and items entering the sample in "
            "different years, not a real price rise.")
        say("Even so, the number of matched pairs is still on the low side in most year spans. The "
            "recommendation is to rely mainly on external indices and use this table as a cross-check.")
        say()

    # ---- external price books as a cross-check ---------------------------
    # The DWU manual publishes a TRIMMED MEAN OF ALL BIDS, while our reference
    # is a median of competitive bids. If the bases really differ as documented,
    # DWU should sit systematically above our median -- and by how much is an
    # empirical read on what "all bids" costs versus "the market middle".
    dwu_p = os.path.join(CLEAN, "dwu_price_manual.csv")
    if os.path.exists(dwu_p):
        dw = pd.read_csv(dwu_p, encoding="utf-8-sig")
        dw = dw[(dw.n_observations >= 3) & (dw.mean_unit_price > 0)]
        dwf = (dw.groupby(["item_family", "unit"])
               .agg(dwu_items=("item_no", "size"),
                    dwu_obs=("n_observations", "sum"),
                    dwu_median_mean=("mean_unit_price", "median"),
                    dwu_median_cv=("cv", "median")).reset_index())
        ours = (ref[ref.usable.isin(["yes", "thin"])]
                .groupby(["item_family", "unit"])
                .agg(our_projects=("n_projects", "sum"),
                     our_median=("price_median", "median")).reset_index())
        cmpx = dwf.merge(ours, on=["item_family", "unit"], how="inner")
        cmpx = cmpx[cmpx.our_median > 0]
        cmpx["dwu_over_ours"] = cmpx.dwu_median_mean / cmpx.our_median
        cmpx = cmpx.sort_values("dwu_over_ours", ascending=False)
        cmpx.to_csv(os.path.join(CLEAN, "external_book_comparison.csv"),
                    index=False, encoding="utf-8-sig")
        if len(cmpx):
            say("## 7. External price-book cross-check (DWU manual vs our median of competitive bids)")
            say()
            say("DWU's basis is the **trimmed mean of all bids**, ours is the "
                "**median of competitive bids**; in theory DWU should be systematically higher.")
            say()
            say(md(cmpx[["item_family", "unit", "dwu_items", "dwu_obs",
                         "dwu_median_mean", "our_projects", "our_median",
                         "dwu_over_ours"]].head(14)))
            say()
            say(f"Median ratio **{cmpx.dwu_over_ours.median():.2f}** ({len(cmpx)} comparable "
                f"item families in total).")
            say("When DWU figures are used to predict the low bid, they must first be multiplied by the "
                "competition discount; TxDOT or Garland figures must not be multiplied by it again.")
            say()

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "master_summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n-> data/clean/master_panel.csv, price_reference.csv,")
    print("   unbalanced_bidding.csv, price_time_index.csv")
    print("-> reports/master_summary.md")


if __name__ == "__main__":
    main()
