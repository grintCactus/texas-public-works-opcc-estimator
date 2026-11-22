"""
Build a human- and machine-readable index of the external bid-tabulation corpus.

For every file under data/external it tries to recover:
    owner (agency)        - from the folder name
    project title         - from the document's own header lines
    bid date              - any date on the first page
    bidders               - contractor names appearing as column headers
    work type             - pavement / erosion / drainage / utility / sidewalk / other
    line-item status      - joined from _audit.csv (produced by audit_external.py)

Output: data/external/INDEX.csv  (+ a Markdown view at data/external/INDEX.md)

Run audit_external.py first; this script reads its _audit.csv.

Time window
-----------
Bids before --since-year are out of scope: pre-pandemic pricing was set on a
cost base that no longer applies. The default is 2021 rather than 2020 because
material cost inflation landed in 2021-2022 -- a bid opened in early 2020 was
still priced off 2019 costs.

Two cutoffs are in force, because the corpus is not evenly thick:
    everything           from --since-year        (2021)
    erosion / drainage   from --since-year-scarce (2020)
Paving data is plentiful, so dropping 2020 costs nothing. Channel-erosion data
is not: every municipal gabion / creek tabulation in the corpus is from 2020,
so a flat 2021 cutoff would remove the entire family. Those extra years are
kept on the condition that their prices are escalated to current dollars using
the TxDOT Highway Cost Index (data/external/cost_indices/). Each row records
which cutoff it was judged against in `window_from_year`.

The `scope_2020plus` column name and its `pre_2020` value are HISTORICAL: they
date from when the cutoff was 2020 and are kept so existing readers of
INDEX.csv do not silently change meaning. The live cutoff is carried in the
`window_from_year` column on every row, and `pre_2020` should be read as
"before the cutoff".
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lock import PipelineBusy, pipeline_lock  # noqa: E402

# Bids opened before this year are excluded. See "Time window" above.
DEFAULT_SINCE_YEAR = 2021

# Work families where comparables are so scarce that a slightly older bid is
# worth more than no bid at all. Channel-erosion repair is the core business and
# the thinnest part of the corpus: the only municipal gabion / creek tabs that
# exist anywhere in it are from 2020 (Mesquite's South Mesquite Creek Gabion
# Repair, Lucas Bridge Erosion Control, Concrete Channel Repair Stream 2J2), and
# there is not one after that year. A 2021 cutoff would delete all of them, so
# these families are held to an earlier cutoff by default.
#
# Prices from those extra years MUST be escalated to current dollars before use
# -- the TxDOT Highway Cost Index series in data/external/cost_indices/ covers
# 2016-2026 and is what makes keeping them defensible rather than sloppy.
SCARCE_WORK_TYPES = {"erosion", "drainage"}
DEFAULT_SINCE_YEAR_SCARCE = 2020

ROOT = Path("data/external")

# --------------------------------------------------------------- work typing
# Ordered: the first family whose pattern fires wins, so the more specific
# erosion/creek vocabulary is tested before generic drainage or paving words.
WORK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("erosion", re.compile(
        r"erosion|gabion|streambank|stream\s*bank|creek\s*(repair|restor|stabil|erosion)|"
        r"channel\s*(repair|lining|lined|maintenance|stabil)|riprap|rip-rap|"
        # (round 9) not an OUTFALL SEWER / main / line: "Baggett Branch Lift Station and Gravity
        # Outfall Sewer Improvements" is sewer work and was typed erosion
        r"outfall(?!\s*(?:gravity\s+|sanitary\s+|wastewater\s+|trunk\s+)?(?:sewer|main|line|interceptor|force))|"
        r"spillway|slope\s*(repair|protect|stabil)|rock\s*anchor|"
        # "bank stabilization" in any form. `creek\s*(stabil...)` above needs the
        # keyword directly after "creek", so "Fish Creek WEST BANK Stabilization"
        # fell through to `other` -- a 24-priced-row bank-stabilisation tabulation
        # kept out of the erosion basket, which is the one basket this corpus
        # exists to fill. Its award packet landed in `materials_supply`.
        #
        # Deliberately `bank\s*stabil` and not bare `stabiliz`: subgrade and
        # lime/cement stabilisation are PAVEMENT work, and this pattern is tried
        # first, so a loose form would pull paving jobs into erosion.
        r"bank\s*stabili|embankment\s*(repair|protect|stabili)|"
        r"retaining\s*wall|scour", re.I)),
    ("drainage", re.compile(
        r"drainage|storm\s*(water|drain|sewer)|culvert|box\s*culvert|detention|"
        r"pond\s*dredg|dredging|inlet|flume|headwall", re.I)),
    ("pavement", re.compile(
        r"paving|pavement|street\s*(recon|repair|resurfac|replac|improv|maint)|"
        r"roadway|road\s*recon|reclamation|mill\s*(and|&)?\s*overlay|overlay|"
        r"alley\s*(recon|pav|improv)|concrete\s*panel|curb\s*(and|&)?\s*gutter|"
        r"subdivision|residential\s*rebuild|crack\s*seal|asphalt", re.I)),
    ("sidewalk", re.compile(
        r"sidewalk|ada\s*ramp|barrier\s*free|safe\s*route|trail|pedestrian", re.I)),
    ("utility", re.compile(
        r"water\s*(main|line|supply)|sanitary\s*sewer|wastewater|sewer\s*rehab|"
        r"lift\s*station|pump\s*station|transmission\s*main|cipp|water\s*and\s*(sanitary\s*)?sewer|"
        r"\b(?:outfall|trunk)\s+sewers?\b|\binterceptors?\b",     # (round 9) an outfall / trunk sewer, an interceptor
        re.I)),
    ("traffic", re.compile(
        r"traffic\s*signal|signal\s*install|intersection\s*improv|pavement\s*marking|"
        r"illumination|street\s*light|signs?\s*and\s*(posts|accessories)", re.I)),
    ("materials_supply", re.compile(
        r"annual\s*(supply|purchase|contract|services|repair|inspection|mowing|"
        r"landscape|grinding|laboratory|wildlife)|purchase\s*of|oem\s*parts|"
        r"rebar|duplicating\s*paper|janitorial|petroleum|ammunition|pistol|rifle|"
        r"jerseys|batteries|brakes|transmissions|aviation\s*fuel|sand\s*and\s*loam|"
        r"crushed\s*concrete|fire\s*(hydrants|protective)|garbage\s*can", re.I)),
    ("building_other", re.compile(
        r"remodel|renovation|screen\s*wall|screening\s*wall|building|park:\s*construction|"
        r"restroom|generator|odor\s*control|elevator|tank\s*rehab|storage\s*tank", re.I)),
]

DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b"),
    re.compile(r"\b(January|February|March|April|May|June|July|August|September|"
               r"October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b", re.I),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# contractor-name signature: ends in a company suffix
CONTRACTOR = re.compile(
    # "&" must be allowed as a word of its own. Without it the pattern cannot
    # cross " & ", so `JB & Co, LLC` was captured as `Co, LLC` and
    # `Knight Engineering & Construction` was not captured at all. Both are
    # real DFW contractors in the competitor set, and a truncated name never
    # unifies with its full form in any bidder-identity key.
    # [ \t] and NOT \s between words. \s matches a newline, so this pattern was
    # assembling company names out of unrelated lines. On the JC-7 tabulation
    # the page reads
    #     Humphrey & Morton Construction
    #     BID OPENED: 02/23/2022 @2:00 P.M.
    #     Company, Inc
    # -- the real name is split around the bid-opening line -- and the matches
    # came out as `P.M.\nCompany, Inc` and `PROJECT No. PWDR17012C\nHumphrey &
    # Morton Construction`. Those are not names; they are fragments welded
    # across a line break. Confining a candidate to ONE line makes the worst
    # case a truncated real name instead of an invented one.
    r"\b([A-Z][A-Za-z&'.\-]*(?:[ \t]+(?:&|[A-Z0-9][A-Za-z0-9&'.\-]*)){0,5}"
    r"[ \t]*,?[ \t]*(?:L\.?L\.?C|L\.?P|L\.?T\.?D|Inc|Incorporated|Company|Co|Corp|"
    r"Corporation|Construction|Contracting|Paving|Enterprises|Services|"
    r"Partners|Group|Industries)\.?)\b"
)

NOISE = re.compile(
    # NOT a bare "engineer". That dropped every contractor with "Engineering"
    # in its name, including `Knight Engineering & Construction` -- the firm
    # that won Briar Hill, a DFW erosion contractor and part of exactly the
    # competitor set this corpus exists to measure. The term is meant to block
    # the engineer's-estimate COLUMN, so it now says so.
    #
    # `CONTRACTOR` already requires a corporate suffix, so a bare "ENGINEER"
    # column header cannot be admitted as a bidder by this change; and a
    # consultant like "Teague Nall & Perkins" is still excluded for the
    # separate and correct reason that it carries no such suffix.
    r"bid\s*tab|tabulation|city\s*of|engineer'?s?\s*(estimate|opinion|opc)|"
    r"\bestimate\b|opinion\s*of\s*probable|\bprobable\b|"
    r"total|subtotal|schedule|addendum|purchasing|department|project\s*no|"
    r"solicitation|certified|page\b", re.I)


def ocr_first_page(path: Path, max_chars: int = 9000) -> str:
    """Page one of the OCR sidecar, with the provenance banner removed.

    Needed because an image-only scan has no text layer, so pdfplumber and
    pdftotext both return nothing and every content-derived column in this
    index -- work_type, project_title, bidders_detected -- falls back to the
    filename. All 18 OCR'd files were in that state: the OCR recovery reached
    the AUDIT (classification, n_priced_rows, which read `read_pdf_text`) and
    never reached the INDEX, which reads this function instead.
    """
    try:
        from audit_external import ocr_sidecar
        side = ocr_sidecar(path)
        if side is None or not side.exists():
            return ""
        text = side.read_text(encoding="utf-8")
    except Exception:                                             # noqa: BLE001
        return ""
    # Keep only the first page, and drop the "###" banner lines -- one of them
    # says "Verify any unit price ..." which SCHEDULE_HDR matches.
    body = text.split("### PAGE 2", 1)[0]
    keep = [ln for ln in body.splitlines() if not ln.lstrip().startswith("###")]
    return "\n".join(keep).strip()[:max_chars]


def first_page_text(path: Path, max_chars: int = 9000) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    out = ""
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            if pdf.pages:
                out = (pdf.pages[0].extract_text() or "")[:max_chars]
    except Exception:
        try:
            import subprocess
            r = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(path), "-"],
                               capture_output=True, timeout=90)
            out = r.stdout.decode("utf-8", "replace")[:max_chars]
        except Exception:
            out = ""
    # An image-only scan yields nothing above; fall back to the cached OCR.
    if len(out.strip()) < 20:
        return ocr_first_page(path, max_chars)
    return out


# Line that marks the start of the pay-item schedule; nothing at or below it
# may be mistaken for the project title.
SCHEDULE_HDR = re.compile(
    r"item\s*(no|#)|unit\s*price|unit\s*cost|description\s+of\s+work|"
    r"\bqty\b|quantit(?:y|ies)\s+unit|extended\s+price|"
    r"bid\s*quantit(?:y|ies)", re.I)
# A pay-item row rather than a heading.
ITEMISH = re.compile(
    r"^\s*\d+(\.\d+)?\s|\$\s*[\d,]+|\b(LS|LF|SY|SF|CY|EA|TON|TN|MOS?|STA|GAL)\b\s*\d",
    re.I)


def header_lines(text: str, n: int = 22) -> list[str]:
    """Lines above the pay-item schedule.

    `###` lines are skipped: that is the provenance banner `src/ocr_scans.py`
    writes at the top of an OCR sidecar, and one of its lines reads "Verify any
    unit price against the PDF by eye before use." SCHEDULE_HDR matches "unit
    price" in it, so for every one of the 18 OCR'd files this function stopped
    at my own warning and returned nothing but the banner -- which is what
    guess_work() and guess_title() were then handed instead of the document's
    own heading. The banner has done its job elsewhere since `text_source`
    became a column, so it should not also be competing with the content.
    """
    out = []
    for ln in text.splitlines()[:n + 8]:
        if ln.lstrip().startswith("###"):
            continue
        if SCHEDULE_HDR.search(ln):
            break
        if ln.strip():
            out.append(ln.strip())
        if len(out) >= n:
            break
    return out


def guess_title(text: str, fallback: str) -> str:
    """Pick the most title-like line from ABOVE the pay-item schedule."""
    lines = header_lines(text)
    # Prefer an explicit "Bid Name:" / "Project:" label (may run onto the next line).
    for i, ln in enumerate(lines):
        m = re.search(r"(?:bid\s*name|project\s*(?:name|title)|solicitation\s*title)"
                      r"\s*:?\s*(.*)", ln, re.I)
        if m:
            val = m.group(1).strip()
            if len(val) < 5 and i + 1 < len(lines):
                val = lines[i + 1].strip()
            if len(val) > 4:
                return re.sub(r"\s+", " ", val)[:160]
    # Otherwise the longest heading-like line that is not boilerplate or an item row.
    cands = [ln for ln in lines
             if 12 <= len(ln) <= 160
             and not re.fullmatch(r"[\W\d]+", ln)
             and not ITEMISH.search(ln)
             and not re.match(r"^(city of|bid tabulation|bid tab|exhibit|"
                              r"certified|solicitation no)\b", ln, re.I)]
    if cands:
        return re.sub(r"\s+", " ", max(cands, key=len))[:160]
    return fallback.replace("_", " ")


FNAME_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
FNAME_YEARMONTH = re.compile(r"(20\d{2})-(\d{2})(?:\D|$)")
# Agency bid numbers that lead with a (fiscal) year: Mesquite "2020-075",
# Dallas County "2024-038-7025", Collin "2023-123".
FNAME_BIDNO_YEAR = re.compile(r"(?:^|/|_)(20\d{2})-\d{1,4}\b")
# Last resort: any 20xx anywhere in the path.
FNAME_BARE_YEAR = re.compile(r"(?:^|\D)(20[0-2]\d)(?:\D|$)")


def date_from_filename(rel: str) -> str:
    """Dates encoded in our own filenames are more reliable than OCR guesses."""
    m = FNAME_DATE.search(rel)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        if 2000 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def year_from_filename(rel: str) -> str:
    rel = rel.replace("\\", "/")
    for pat in (FNAME_BIDNO_YEAR, FNAME_YEARMONTH, FNAME_BARE_YEAR):
        m = pat.search(rel)
        if m and "2000" <= m.group(1) <= "2030":
            return m.group(1)
    return ""


def year_from_csv_dates(path: Path) -> str:
    """For a delimited dataset, take the EARLIEST let date present.

    These files are datasets, not single bids, so the earliest date is what
    decides whether any of their rows predate the 2020 cutoff.
    """
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as fh:
            rdr = csv.DictReader(fh)
            col = next((c for c in (rdr.fieldnames or [])
                        if "let_date" in c or "bid_date" in c), None)
            if not col:
                return ""
            years = set()
            for i, row in enumerate(rdr):
                v = (row.get(col) or "")[:4]
                if v.isdigit():
                    years.add(v)
                if i > 400000:
                    break
            return min(years) if years else ""
    except Exception:
        return ""


def guess_date(text: str) -> str:
    """The latest real date in the document, ignoring running footers.

    "Latest date wins" is the right rule for a tabulation -- the opening date
    is the newest thing on it -- but on its own it has a systematic failure:
    a report PRINT date is by definition later than everything it reports, and
    it sits in the page footer where it outranks the content.

    Both Garland average-bid-price books were dated 2025 this way. They carry
    "Tuesday, January 7, 2025" on every page, while the 2023 book's 440 entry
    dates are ALL in 2023 and the 2024 book's 389 are all in 2024. These are
    the reference unit-price tables this corpus recommends using first, so a
    two-year error in their date biases any escalation applied to them -- and
    because the number looked recent rather than absurd, nothing flagged it.

    A date printed on most of the pages is boilerplate, so it is discarded.
    The threshold is deliberately generous: an opening date legitimately
    repeats on each page of a tabulation (one packet here prints its opening
    date four times), so only a date appearing on a MAJORITY of pages is
    treated as a footer, and a short document can never trip the rule.
    """
    counts: dict[str, int] = {}
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                g = m.groups()
                if pat is DATE_PATTERNS[0]:
                    mo, d, y = int(g[0]), int(g[1]), int(g[2])
                    y = y + 2000 if y < 100 else y
                elif pat is DATE_PATTERNS[1]:
                    mo, d, y = MONTHS[g[0].lower()], int(g[1]), int(g[2])
                else:
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                if not (2000 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31):
                    continue
            except Exception:
                continue
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            counts[iso] = counts.get(iso, 0) + 1
    if not counts:
        return ""
    # pdftotext separates pages with a form feed.
    pages = text.count("\f") + 1
    footer_at = max(4, int(0.6 * pages))
    real = {iso: n for iso, n in counts.items() if n < footer_at}

    # A bid cannot have been opened in the future, and "latest date wins" walks
    # straight into any forward-looking table. The Hickory Creek bond packet
    # carries a debt-service schedule running to 2031, and this function
    # returned 2030-10-01 for it; only the opening date in its filename kept
    # that out of the index. Nothing would have reported it.
    today = _dt.date.today().isoformat()
    past = {iso: n for iso, n in real.items() if iso <= today}
    cand = past or real or counts

    # A lone stray year beats the page-majority test above, because the index
    # reads only PAGE ONE: a footer appears once there, so repetition cannot
    # tell it from content. (That is how the page-majority rule came to be
    # written and verified against full-document text while the code path in
    # use reads one page -- the rule was inert exactly where it was needed.)
    #
    # What does separate them on a single page is the YEAR. Both Garland
    # average-price books print "Tuesday, January 7, 2025" in the footer while
    # every priced entry on page one is from the book's own year. A date gap
    # test would not catch it either: in the 2024 book the footer is only 19
    # days after the latest real entry. But it is the only 2025 date on a page
    # otherwise entirely 2024.
    #
    # So: if the latest date is alone in its year while an earlier year has two
    # or more dates, that year is the document's and the straggler is dropped.
    # Kept deliberately narrow -- a tabulation advertised in December and opened
    # in January has one date in each year, no year reaches two, and the rule
    # stays out of the way.
    years: dict[str, int] = {}
    for iso, n in cand.items():
        years[iso[:4]] = years.get(iso[:4], 0) + n
    newest = max(cand)
    if years.get(newest[:4], 0) == 1:
        better = [y for y, n in years.items() if n >= 2 and y < newest[:4]]
        if better:
            keep = max(better)
            return max(iso for iso in cand if iso[:4] == keep)

    # Each narrowing falls back rather than returning nothing: no date at all
    # sends the file to `unknown_date`, which is worse than an imperfect one.
    return newest


# --- recurring-service contracts masquerading as construction lettings -------
#
# Some of what a city puts out to bid is not a project. An annual mowing
# contract or an hourly storm-drain-cleaning contract is competitively bid,
# tabulated on the same form, and awarded to the low bidder -- but its prices
# are service rates, not installed-work unit prices, and they must not reach a
# construction price basis.
#
# Three files leak through `work_type` alone as ordinary construction:
#   legistar/mckinney/..._Bid_Tab_25-46ITB.pdf  "State hourly rate to include
#       all services, labor & equipment", 1000 HR at $495/hr
#   mesquite/2021-050.pdf, mesquite/2023-053.pdf  "Annual Landscaping &
#       Property Preservation Maintenance", priced per Lot and per Acre
#
# Units that price TIME or a RECURRING VISIT rather than installed work.
# "AC" (acre) is deliberately NOT here: seeding and sodding on a construction
# job are priced per acre, and including it would convict the Tarrant County
# annual hydromulch contract, whose prices ARE construction unit prices.
#
# The left-hand boundary is a lookbehind, not \b: these tabs glue the unit to
# the quantity ("1000HR"), and \b finds no boundary between "0" and "H", so the
# McKinney contract scored 0% service units while every row on it was hourly.
SERVICE_UNITS = re.compile(
    r"(?i)(?<![A-Za-z])(HR|HRS|HOUR|HOURS|MO|MONTH|WK|WEEK|YR|YEAR|DAY|DAYS|"
    r"LOT|LOTS|SITE|SITES|VISIT|OCCURRENCE|EVENT|TRIP)(?![A-Za-z])")

# Phrases saying the SOLICITATION is a recurring service rather than a project.
SERVICE_PHRASE = re.compile(
    r"(?i)hourly[ \t]*rate|annual[ \t]+(?:contract|landscap|mow|maint)|"
    r"as[-‐ ]needed|on[-‐ ]call|term[ \t]+contract|"
    r"property[ \t]+preservation|janitorial|mowing|trimming|"
    r"maintenance[ \t]+for[ \t]+|inspection[ \t]+services|"
    r"per[ \t]+lot[ \t]+basis|janitor")

# Row numbers here include McKinney's "#0-1" with a U+2010 hyphen.
SERVICE_ROW = re.compile(r"^[ \t]*#?\d{1,4}(?:[-‐]\d+)?[ \t]", re.M)


def service_contract(text: str) -> tuple[str, str]:
    """(project_kind, evidence) -- is this a recurring service contract?

    BOTH halves are required: a phrase alone convicts a construction job that
    merely contains "Inspection Services", and a unit profile alone convicts
    one carrying force-account labour by the hour. Requiring a service phrase
    AND a schedule mostly priced by time or visit is what keeps the Tarrant
    County hydromulch contract on the construction side -- its header says
    "Annual Contract" but none of its rows are priced by time.

    This LABELS; it does not reject. Whether a service rate may inform a price
    basis is the modelling side's call, and some of these contracts do carry
    usable work (hydromulch by the acre). Handing down a verdict here would
    make that decision invisible to the side that has to live with it.
    """
    rows = [ln for ln in text.splitlines() if SERVICE_ROW.match(ln)]
    if not rows:
        return "", ""
    timed = sum(1 for ln in rows if SERVICE_UNITS.search(ln))
    frac = timed / len(rows)
    head = "\n".join(text.splitlines()[:14])
    in_head = bool(SERVICE_PHRASE.search(head))
    desc_hits = sum(1 for ln in rows if SERVICE_PHRASE.search(ln))
    if not ((in_head or desc_hits >= 2) and frac >= 0.30):
        return "", ""
    ev = []
    if in_head:
        ev.append("service phrase in header")
    if desc_hits >= 2:
        ev.append(f"{desc_hits} priced rows describe a service")
    ev.append(f"{100 * frac:.0f}% of rows priced by time/visit")
    return "service_contract", "; ".join(ev)


# --- award paperwork that carries no schedule of prices -----------------------
#
# The parsing side asked for an `award_notice` kind so a project total plus the
# winning bidder could anchor its own line-item sums from outside. Measuring
# the five candidates shows they are TWO shapes, and only one can serve that:
#
#   engineer's recommendation letter -- one project. "RE: Fish Creek West Bank
#       Stabilization / Bid Number 25087 / Engineer's Recommendation", then the
#       bid opening date, how many proposals arrived, and the bid totals. Its
#       17 large dollar amounts are one project's bid set, which IS an anchor.
#
#   city council minutes -- a whole meeting. The Briar Hill minutes carry 24
#       distinct large dollar amounts across unrelated agenda items. There is
#       no "the award amount" in that document to extract, and pulling one
#       would be a guess wearing the clothes of a fact.
#
# So they are labelled apart. Collapsing them into one `award_notice` would
# hand the modelling side a column it could read as a single-project total on
# documents where no such thing exists.
COUNCIL_MINUTES = re.compile(
    r"(?i)city\s+council\s+(?:meeting|minutes)|\bMINUTES\b[\s\S]{0,400}?"
    r"call\s+to\s+order")
AWARD_LETTER = re.compile(
    r"(?i)engineer'?s?\s+recommendation|recommendation\s+of\s+award|"
    r"recommend\s+(?:that\s+)?(?:the\s+)?(?:city\s+)?award")
BID_OPENING_NARRATIVE = re.compile(
    # \b after "for": without it this matched the prefix of "BID OPENING FORM",
    # and Mansfield's bid-opening forms were filed as recommendation letters.
    r"(?i)bid\s+opening\s+for\b|bids?\s+(?:were|was)\s+received|proposals\s+were"
    r"\s+received|were\s+received\s+and\s+publicly\s+opened")

# The narrative alone is NOT enough, because a tabulation narrates its own
# opening: Lewisville's totals sheet carries the footnote "Three additional
# bids were received however deemed nonresponsive", which fired the pattern on
# a document that is plainly a tabulation. What actually separates an
# engineer's letter from a tabulation is that a letter is addressed to
# someone -- "Re: Letter of Concurrence ... Dear Ms. Waggoner:". All three
# genuine letters here have both parts; neither false positive has either.
LETTER_FORM = re.compile(r"(?i)\bdear\s+(?:mr|mrs|ms|dr|miss)?\.?\s*[A-Z]")
BIG_MONEY = re.compile(r"\$\s?[\d,]{7,}(?:\.\d\d)?")


def award_paperwork(text: str, n_priced: int) -> tuple[str, str]:
    """(project_kind, evidence) for award paperwork with no price schedule.

    Requires the absence of a priced schedule: an engineer's recommendation
    attached to a real tabulation should stay classified by its tabulation,
    not demoted to paperwork.
    """
    if n_priced > 0:
        return "", ""
    n_amounts = len(set(BIG_MONEY.findall(text)))
    if COUNCIL_MINUTES.search(text):
        return ("council_minutes",
                f"council meeting minutes; {n_amounts} distinct large dollar "
                f"amounts across unrelated agenda items -- NOT a single-project "
                f"total")
    if LETTER_FORM.search(text) and (AWARD_LETTER.search(text)
                                     or BID_OPENING_NARRATIVE.search(text)):
        return ("award_recommendation",
                f"engineer's letter recommending award on one solicitation; "
                f"{n_amounts} distinct large dollar amounts (its bid set)")
    return "", ""


OPEN_CUE = re.compile(
    r"(?i)bid[ \t]*open|open(?:ed|ing)[ \t]*(?:date|at)?|"
    r"bids?[ \t]+(?:were[ \t]+)?receiv|read[ \t]+aloud|due[ \t]*(?:date|by)"
)


def date_on_opening_cue(text: str, iso: str, window: int = 160) -> bool:
    """Is this date printed next to a bid-opening cue?

    Used to tell a stated opening date from a stray one. The distinction
    matters in both directions: it clears nine Arlington rows whose documents
    say "BID OPENED: 9/4/2024" outright, and it convicts the Grand Prairie
    engineering SOQ, whose only date is `6/1/2022 5:55 PM` next to
    `1 of 2  I:\\47000s\\...` -- a plot-file footer timestamp, with no cue
    anywhere near it.
    """
    y, mo, d = iso.split("-")
    month = ("January February March April May June July August September "
             "October November December".split())[int(mo) - 1]
    pats = [
        r"\b0?%d/0?%d/%s\b" % (int(mo), int(d), y),
        r"\b%s-%s-%s\b" % (y, mo, d),
        r"\b%s\s+0?%d(?:st|nd|rd|th)?,?\s+%s\b" % (month, int(d), y),
    ]
    for pat in pats:
        for m in re.finditer(pat, text, re.I):
            lo = max(0, m.start() - window)
            if OPEN_CUE.search(text[lo:m.end() + 40]):
                return True
    return False


# Kimley-Horn's tabulation template puts a placeholder row above the real
# company names ("BIDDER 1 | BIDDER 2 | ..."), and the grid can glue it onto
# the name below it. The contractor pattern allows up to five leading
# capitalised words, so it happily captured
#     "BIDDER 3 BIDDER 4 Road Solutions, LLC"
# as a single company. 10 files in this corpus use that template.
# Matched anywhere, not just at the start: on the Mansfield tabulation the
# placeholder arrives mid-string, behind a bond number --
#     "No. 061088078 BIDDER 1 McClendon Const Co"
# -- so an anchored prefix strip leaves it in place. The real company name is
# always what FOLLOWS the placeholder, so everything up to and including the
# last one is cut.
# `BIDDERS?` with the number OPTIONAL: Keller's template writes a plural
# column header with no number at all ("BIDDERS Cole Construction Inc."),
# which a pattern requiring BIDDER+digits walks straight past. That file is
# not among the ten the parsing side identified -- it is an eleventh, found
# only because the index was re-checked for leftovers after the fix.
PLACEHOLDER_ANY = re.compile(r"(?i)^.*?(?:BIDDERS?\s*\d*\s*)+")

# Leading junk that sits on the SAME line as the company name, so confining
# matches to one line does not remove it: a bid-opening time ("@2:00 P.M.
# Stoic Civil Construction, Inc") or the tail of an address ("TX 75454
# Construction Company, Inc"). Stripped rather than rejected, for the same
# reason as the placeholder: the real name is still in there.
#
# A bare "AM"/"PM" is NOT stripped, only a clock time or the DOTTED form.
# `AM Construction LLC` and `PM Construction & Rehab LLC` are perfectly
# ordinary company names built on initials, and the first version of this
# pattern turned them into `Construction LLC` and `Construction & Rehab LLC`.
# A firm using initials writes "PM"; a bid-opening time writes "P.M." or
# "2:00 PM", so the dots or the clock are what distinguish them.
LEADING_JUNK = re.compile(
    r"(?i)^\d{1,2}:\d{2}\s*[AP]\.?M\.?[\s,]+|^[AP]\.M\.[\s,]+|"
    r"^[A-Z]{2}\.?[ \t]+\d{5}(?:-\d{4})?[\s,]+|^\d{5}(?:-\d{4})?[\s,]+")


def strip_placeholder(name: str) -> str:
    """Drop any "BIDDER n" run and all that precedes it, keeping the real name.

    Deliberately a strip, not a reject. Discarding the whole string because
    part of it is junk is exactly the failure that cost the parsing side 2,714
    rows: its address-stripper returned empty when it could not split a cell,
    the caller read empty as "not a bidder name", and real tabulations were
    dropped. A placeholder glued to a real name still contains the real name.
    """
    name = PLACEHOLDER_ANY.sub("", name)
    name = LEADING_JUNK.sub("", name)
    return name.strip(" ,.|")


def guess_bidders(text: str, limit: int = 12) -> list[str]:
    seen: list[str] = []
    for m in CONTRACTOR.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        name = strip_placeholder(name)
        if len(name) < 5 or NOISE.search(name):
            continue
        key = name.lower()
        if key not in [s.lower() for s in seen]:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


# (round 9, .30) Parks and recreation, read from the TITLE or the file name only.  "North Park Turf
# and Parking Improvements" (synthetic turf, lights, a parking lot) named nothing in WORK_PATTERNS, so
# its header lines decided -- "parking-lot drainage improvements" -- and it was typed drainage.  A
# title that names a pattern above still wins; in a header the words are too common (turf
# reinforcement mats, "no parking" signs) to be read.  Not bare "turf" (a tractor dealer's "Ag & Turf"),
# "parking lot" (often a paving job) or "park improvements" (a technology park's roads).
PARKS_TITLE_RE = re.compile(
    r"\b(?:synthetic|artificial)\s+turf\b|\bturf\s+(?:and|&)\s+parking\b|"
    r"\bturf\s+(?:fields?|replac\w*|install\w*|improv\w*)|athletic\s+fields?|ball\s?fields?|"
    r"sports?\s+(?:fields?|complex)|playgrounds?|pavilions?|tennis\s+courts?|pickleball|splash\s?pads?|"
    r"skate\s?parks?|dog\s+parks?", re.I)


def guess_work(title: str, text: str, filename: str = "") -> str:
    """Type the project from its title and filename, never from pay-item rows.

    The body of a bid tab lists every pay item, so words like "retaining wall"
    or "channel" appear inside ordinary paving jobs. Only the title, the
    filename, and the header lines above the item schedule may decide this.
    """
    stem = Path(filename).stem.replace("_", " ").replace("-", " ")
    for name, pat in WORK_PATTERNS:
        if pat.search(title) or pat.search(stem):
            return name
    if PARKS_TITLE_RE.search(title) or PARKS_TITLE_RE.search(stem):     # round 9
        return "building_other"
    head = "\n".join(header_lines(text))
    for name, pat in WORK_PATTERNS:
        if pat.search(head):
            return name
    return "other"


def load_date_overrides() -> dict[str, str]:
    """Dates established by hand, with the reasoning recorded alongside.

    `_date_overrides.csv` wins over anything inferred from the filename or the
    document text. A blank `bid_year` means "deliberately no date" (e.g. a
    specification manual rather than a bid).
    """
    path = ROOT / "_date_overrides.csv"
    if not path.exists():
        return {}
    out = {}
    for r in csv.DictReader(path.open(encoding="utf-8-sig")):
        f = (r.get("file") or "").strip().replace("\\", "/")
        if f:
            out[f] = (r.get("bid_year") or "").strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-year", type=int, default=DEFAULT_SINCE_YEAR,
                    help=f"earliest bid year kept in scope (default "
                         f"{DEFAULT_SINCE_YEAR})")
    ap.add_argument("--since-year-scarce", type=int,
                    default=DEFAULT_SINCE_YEAR_SCARCE,
                    help=f"earlier cutoff for the scarce work families "
                         f"({', '.join(sorted(SCARCE_WORK_TYPES))}) where "
                         f"comparables are too thin to discard (default "
                         f"{DEFAULT_SINCE_YEAR_SCARCE}); pass the same value as "
                         "--since-year to apply one cutoff to everything")
    ap.add_argument("--force", action="store_true",
                    help="break a pipeline lock left by a dead run")
    args = ap.parse_args()
    since = str(args.since_year)
    since_scarce = str(args.since_year_scarce) if args.since_year_scarce else since
    print(f"time window: bids from {since} onward are in scope")
    if since_scarce != since:
        print(f"  exception: {', '.join(sorted(SCARCE_WORK_TYPES))} kept from "
              f"{since_scarce} onward (escalate those to current dollars via "
              "the TxDOT HCI series)")

    audit_path = ROOT / "_audit.csv"
    if not audit_path.exists():
        print(f"missing {audit_path}; run: python src/audit_external.py", file=sys.stderr)
        return 1

    audit = {r["file"]: r for r in csv.DictReader(
        audit_path.open(encoding="utf-8-sig"))}
    overrides = load_date_overrides()
    if overrides:
        print(f"date overrides: {len(overrides)} (see _date_overrides.csv)")

    # Files whose extracted text has two columns woven together letter by
    # letter ("MCleoawniinngg, otrfi mtramshin,g" = "Mowing, trimming/" and
    # "Cleaning of trash,"). Measured by src/check_interleaved_text.py, which
    # compares two independent readers; the list is GENERATED, never
    # hand-maintained, so re-running it after a harvest keeps this current.
    #
    # Only the descriptions are affected -- quantities and prices sit in their
    # own columns and come out intact. That is precisely why it needs a label:
    # the row looks complete and prices correctly, while the text that decides
    # its item_family is unusable. Same hazard as an OCR row, different cause.
    interleaved: dict[str, float] = {}
    inter_path = ROOT / "_interleaved_text.csv"
    if inter_path.exists():
        for r in csv.DictReader(inter_path.open(encoding="utf-8-sig")):
            try:
                share = float(r.get("share") or 0)
            except ValueError:
                continue
            if share >= 0.40:
                interleaved[r["file"]] = share
    # Two independent detectors exist and they DISAGREE on 13 of 20 files, so
    # neither is authoritative:
    #   _interleaved_text.csv   long tokens one reader reports and the other
    #                           never sees -- catches benign disagreement too
    #   _extractor_choice.csv   long tokens that cannot be segmented into
    #                           corpus vocabulary -- but its absolute counts
    #                           track how well the vocabulary covers that
    #                           document's subject matter, so on a big prose
    #                           packet both readers score ~500 and the signal
    #                           drowns
    #
    # The UNION is taken on purpose. This drives a label meaning "do not infer
    # item_family from descriptions in this file", and the two errors are not
    # symmetric: a false positive costs one file a needs_review it did not
    # need, while a false negative silently assigns a wrong family to a row
    # whose price is correct. When the cheap error is visible and the dear one
    # is silent, over-flag.
    choice_path = ROOT / "_extractor_choice.csv"
    if choice_path.exists():
        for r in csv.DictReader(choice_path.open(encoding="utf-8-sig")):
            if r.get("pick") == "pymupdf":
                interleaved.setdefault(r["file"], -1.0)
    if interleaved:
        print(f"interleaved-text files: {len(interleaved)} (union of two "
              f"detectors; descriptions unreliable)")

    # Which files were read by OCR rather than from a text layer, and how far
    # each one's own arithmetic vouches for it. Absent file -> text layer.
    ocr_trust_map: dict[str, tuple[str, str]] = {}
    trust_path = ROOT / "_ocr_trust.csv"
    if trust_path.exists():
        for r in csv.DictReader(trust_path.open(encoding="utf-8-sig")):
            ocr_trust_map[r["file"]] = ("ocr", r["trust"])
        print(f"OCR-sourced files: {len(ocr_trust_map)} "
              f"({sum(1 for v in ocr_trust_map.values() if v[1] in ('high', 'partial'))}"
              f" with verified arithmetic; see _ocr_trust.csv)")

    # Same exclusions as the audit: these hold labour-cost data, raw archives,
    # quarantined pre-cutoff bids and cached OCR text.
    SKIP = ("dol", "txdot_archive/zips", "txdot_archive/probe", "_pre2020",
            "_ocr")
    # This tool's own outputs are not source data.
    OWN_OUTPUTS = {"INDEX.csv", "INDEX.md", "README.md"}

    def excluded(p: Path) -> bool:
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel in OWN_OUTPUTS:
            return True
        return any(rel == s or rel.startswith(s + "/") for s in SKIP)

    rows = []
    files = sorted(p for p in ROOT.rglob("*")
                   if p.is_file() and p.suffix.lower() in
                   {".pdf", ".xlsx", ".xls", ".csv"}
                   and not p.name.startswith("_") and not excluded(p))

    for i, p in enumerate(files, 1):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        a = audit.get(rel, {})
        text = first_page_text(p)
        title = guess_title(text, p.stem) if text else p.stem.replace("_", " ")
        bidders = guess_bidders(text) if text else []

        # Date precedence: our own filename > a date printed in the document >
        # the year encoded in an agency bid number.
        # Which rule produced the date is recorded, not just the date. The
        # consumer of this index needs it: a date the harvester put in the
        # filename and a date scraped out of page one are not equally strong,
        # and three separate bugs this corpus has produced (a source typo, a
        # report footer, a plot-file timestamp) were all "a date in the
        # document text" -- the weakest of the three sources and the only one
        # that has ever been wrong here.
        bid_date = date_from_filename(rel)
        bid_date_source = "filename" if bid_date else ""
        if not bid_date and text:
            bid_date = guess_date(text)
            if bid_date:
                bid_date_source = ("document_text_on_opening_cue"
                                   if date_on_opening_cue(text, bid_date)
                                   else "document_text")
        year = bid_date[:4] or year_from_filename(rel)
        if not bid_date and year:
            bid_date_source = "filename_year_only"
        if not year and p.suffix.lower() in (".csv", ".tsv"):
            year = year_from_csv_dates(p)
            if year:
                bid_date_source = "csv_let_dates"

        # Where two independent sources disagree, SAY SO rather than quietly
        # taking the winner. `rowlett/2022-18_Dalrock_South_Curve.pdf` prints
        # "BIDS OPENED: June 23, 2002" -- a typo for 2022 in the city's own
        # document. The precedence rule did the right thing (a printed date
        # outranks a year guessed from a bid number) and still produced 2002,
        # so the file was judged pre-cutoff and the quarantine step moved a
        # perfectly good 2022 line-item tabulation out of the corpus, with
        # nothing printed anywhere. Resolution still follows the precedence
        # rule, and an override in _date_overrides.csv still beats both; the
        # conflict is recorded so the next one is visible instead of silent.
        alt = year_from_filename(rel)
        date_conflict = ""
        if year and alt and abs(int(year) - int(alt)) > 1:
            # A date printed on an explicit "BID OPENED:" line settles it: that
            # is the document stating its own opening date, and no year guessed
            # from a filename outranks it.
            #
            # This is what nine of the eleven conflicts turned out to be. All of
            # them are Arlington, and Arlington's filenames carry the PROGRAM
            # year, not the bid year -- "2022 Outfall Improvements" was opened
            # 9/4/2024, "2017 Sanitary Sewer Stream Crossings Ph II" on
            # 9/19/2024. Nothing is wrong with those rows, so leaving them in an
            # open-conflict list is its own harm: eleven permanent entries that
            # never need action are how a report stops being read, and the two
            # that DO need action hide among them.
            #
            # The cue alone is NOT enough, and Rowlett is the proof: its
            # tabulation prints "BIDS OPENED: June 23, 2002" -- a typo for 2022
            # on an explicit opening line. Being printed next to the words "BID
            # OPENED" shows a date is PRESENTED as the opening date, not that it
            # is right, so a cue-only rule would have auto-dismissed the very
            # bug that started this.
            #
            # What separates the two is direction. A program year always comes
            # before the letting it funds, so an explained conflict must have
            # the document date LATER than the filename year -- true for all
            # nine Arlington rows. Rowlett's document year is twenty years
            # EARLIER than its filename, which the program-year story cannot
            # explain, so it stays open and needs a human (it has an override).
            if (bid_date and int(year) >= int(alt)
                    and date_on_opening_cue(text, bid_date)):
                date_conflict = ("explained: document prints 'BID OPENED' on "
                                 "%s, after the %s program year in the filename"
                                 % (bid_date, alt))
            else:
                date_conflict = "document says %s, filename implies %s" % (year,
                                                                           alt)

        is_reference = False
        if rel in overrides:                    # hand-established, wins outright
            if date_conflict:
                date_conflict += " -- resolved by _date_overrides.csv"
            year = overrides[rel]
            bid_date_source = "manual_override"
            if year and not bid_date.startswith(year):
                bid_date = ""                   # don't keep a contradicted date
            if not year:
                # An override that deliberately leaves the year blank means no
                # bid date applies (a spec/cost manual, not a tender), which is
                # different from a date we simply failed to establish.
                is_reference = True
                bid_date = ""
        # Pre-pandemic pricing is not comparable, so the corpus is scoped to
        # --since-year onward.
        #   in_scope     - bid in the window
        #   pre_2020     - BEFORE the cutoff (historical value name; see module
        #                  docstring) -> quarantine it
        #   reference    - no bid date applies (spec / cost manual), usable as is
        #   unknown_date - date NOT established; held out until someone records it
        work = guess_work(title, text, rel)
        kind, service_evidence = service_contract(text) if text else ("", "")
        if not kind and text:
            try:
                n_priced = int(float(a.get("n_priced_rows") or 0))
            except ValueError:
                n_priced = 0
            kind, service_evidence = award_paperwork(text, n_priced)
        project_kind = kind or ("materials_supply" if work == "materials_supply"
                                else "construction")
        cutoff = since_scarce if work in SCARCE_WORK_TYPES else since
        if is_reference:
            scope = "reference"
        elif not year:
            scope = "unknown_date"
        elif year >= cutoff:
            scope = "in_scope"
        else:
            scope = "pre_2020"

        rows.append({
            "file": rel,
            "owner": rel.split("/")[0],
            "project_title": title,
            "work_type": work,
            # What KIND of solicitation this is, independent of work_type. A
            # recurring service contract is bid and tabulated like a project
            # but prices labour-hours or site visits, so its rates must not
            # silently join a construction unit-price basis.
            "project_kind": project_kind,
            "service_evidence": service_evidence,
            "bid_date": bid_date,
            "bid_date_source": bid_date_source,
            "bid_year": year,
            "scope_2020plus": scope,     # historical column name; see docstring
            "window_from_year": cutoff,  # the cutoff THIS row was judged against
            # Provenance, a dimension independent of the time window: everything
            # indexed here was harvested from a public source, so it is the
            # TRAIN side. The company's own tabulations live in data/ root and
            # are TEST-only; they are deliberately NOT in this index. Kept as an
            # explicit column so "excluded because too old" can never be
            # confused with "held back as the test set".
            "data_role": "train",
            # How the text behind this row was obtained. Everything in the
            # corpus reads as text EXCEPT 18 files that are photographs of
            # printed tabulations; those were recovered by `src/ocr_scans.py`
            # and scored by `src/ocr_verify.py`. A price read by OCR is not the
            # same evidence as a price read from a text layer -- the engine
            # confuses 0/o and splits thousands separators -- so the row says so
            # instead of letting the two blend together downstream.
            # `ocr_trust` is that file's own arithmetic verdict: "high" and
            # "partial" mean its rows satisfy qty x unit price = total and may
            # be parsed; "unverifiable"/"low" mean the unit prices must be read
            # off the PDF by eye before use.
            "date_conflict": date_conflict,
            "text_source": ocr_trust_map.get(rel, ("text_layer", ""))[0],
            # "interleaved_columns" = the DESCRIPTION text of this file is
            # corrupted by the extractor; its numbers are fine. Do not infer
            # item_family from descriptions on these rows.
            "text_quality": ("interleaved_columns" if rel in interleaved
                             else "ok"),
            "ocr_trust": ocr_trust_map.get(rel, ("text_layer", ""))[1],
            "n_bidders_detected": len(bidders),
            "bidders_detected": "; ".join(bidders),
            "classification": a.get("classification", ""),
            "n_item_rows": a.get("n_item_rows", ""),
            "n_priced_rows": a.get("n_priced_rows", ""),
            "size_kb": a.get("size_kb", ""),
            "note": a.get("note", ""),
        })
        if i % 25 == 0 or i == len(files):
            print(f"  indexed {i}/{len(files)}", flush=True)

    # ---- sort so the most useful rows come first
    pri = {"line_item": 0, "unit_price_dataset": 0, "unit_price_table": 0,
           "likely_line_item": 1, "summary_only": 2, "unreadable": 3}
    workpri = {"erosion": 0, "drainage": 1, "pavement": 2, "sidewalk": 3,
               "utility": 4, "traffic": 5}
    scopepri = {"in_scope": 0, "reference": 1, "unknown_date": 2, "pre_2020": 3}
    rows.sort(key=lambda r: (scopepri.get(r["scope_2020plus"], 3),
                             pri.get(r["classification"], 9),
                             workpri.get(r["work_type"], 8),
                             r["owner"], r["file"]))

    out_csv = ROOT / "INDEX.csv"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---- Markdown view of just the usable, in-scope unit-price sources
    usable = {"line_item", "likely_line_item", "unit_price_table", "unit_price_dataset"}

    # An undated file is NOT assumed to be in scope. A file whose year cannot
    # be established could be pre-2020, and defaulting it in is exactly how old
    # pricing leaks into the active set (it happened once already, with files
    # dropped into a brand-new folder). Undated files stay in the index, flagged,
    # until a year is recorded in `_date_overrides.csv`.
    def is_good(r: dict) -> bool:
        return (r["classification"] in usable
                and r["scope_2020plus"] in ("in_scope", "reference"))

    good = [r for r in rows if is_good(r)]
    undated = [r for r in rows if r["classification"] in usable
               and r["scope_2020plus"] == "unknown_date"]
    md = ["# External bid-tabulation index",
          "",
          f"{len(rows)} files under `data/external/`.",
          "",
          f"- **{len(good)}** carry line-item unit prices AND are confirmed "
          f"{since} or later",
          f"- {len(undated)} carry line-item prices but their bid date could "
          "not be established -- **held out until dated** "
          "(record the year in `_date_overrides.csv`)",
          f"- {sum(1 for r in rows if r['classification'] in usable and r['scope_2020plus'] == 'pre_2020')}"
          f" carry line-item prices but predate {since}, so out of scope",
          f"- {sum(1 for r in rows if r['classification'] not in usable)}"
          " are project-total-only or unreadable",
          "",
          "Project totals alone are not comparable across different scopes, so "
          "only the line-item files below should feed unit-price work.",
          "",
          "Work types are ordered erosion/drainage first (the channel-erosion "
          "side of the business, where comparables are scarcest).",
          ""]
    for wt in ["erosion", "drainage", "pavement", "sidewalk", "utility", "traffic",
               "materials_supply", "building_other", "other"]:
        sel = [r for r in good if r["work_type"] == wt]
        if not sel:
            continue
        md += [f"## {wt}  ({len(sel)} files)", "",
               "| owner | project | bid date | bidders | priced rows | file |",
               "|---|---|---|---|---|---|"]
        for r in sel:
            t = r["project_title"].replace("|", "/")[:80]
            md.append(f"| {r['owner']} | {t} | {r['bid_date'] or '?'} | "
                      f"{r['n_bidders_detected']} | {r['n_priced_rows']} | "
                      f"`{r['file']}` |")
        md.append("")

    excl = [r for r in rows if not is_good(r)]
    if excl:
        md += [f"## Not used  ({len(excl)} files)", "",
               f"Kept for traceability. `pre_2020` = earlier than {since} "
               "(historical value name); "
               "`summary_only` = bidder totals with no unit-price schedule; "
               "`unreadable` = scanned image, would need OCR.", "",
               "| owner | project | reason | file |", "|---|---|---|---|"]
        for r in excl:
            t = r["project_title"].replace("|", "/")[:70]
            why = (r["scope_2020plus"] if r["scope_2020plus"] == "pre_2020"
                   else r["classification"])
            md.append(f"| {r['owner']} | {t} | {why} | `{r['file']}` |")

    (ROOT / "INDEX.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ---- console summary
    import collections
    print("\n=== in-scope (2020+) line-item files by work type ===")
    for k, v in collections.Counter(r["work_type"] for r in good).most_common():
        print(f"  {k:18s} {v}")
    print("\n=== in-scope line-item files by owner ===")
    for k, v in collections.Counter(r["owner"] for r in good).most_common():
        print(f"  {k:18s} {v}")
    print("\n=== scope breakdown (all files) ===")
    for k, v in collections.Counter(r["scope_2020plus"] for r in rows).most_common():
        print(f"  {k:18s} {v}")

    # Printed, not just stored. A date conflict decides whether a file is kept
    # or quarantined, so it must not sit unread in a column.
    clash = [r for r in rows if r.get("date_conflict")]
    if clash:
        def settled(r):
            c = r["date_conflict"]
            return c.startswith("explained:") or "resolved by" in c

        open_ = [r for r in clash if not settled(r)]
        done = [r for r in clash if settled(r)]
        print(f"\n=== date conflicts: {len(open_)} open, {len(done)} settled ===")
        if open_:
            print("  Two sources disagree and nothing in the document settles "
                  "it. Check each\n  against the file and record the answer in "
                  "_date_overrides.csv.")
            for r in open_:
                print(f"  {r['bid_year']}  {r['file']}")
                print(f"        {r['date_conflict']}  <-- NEEDS A HUMAN")
        # Settled rows stay listed but collapsed. They are kept visible because
        # the rule that settles them could itself be wrong; they are collapsed
        # because a list where nothing needs action stops being read.
        for r in done:
            print(f"  [settled] {r['bid_year']}  {r['file']}")

    if undated:
        print(f"\n!! {len(undated)} line-item file(s) have NO established bid date "
              "and are held OUT of the usable set.")
        print("   A file dropped into a new folder with no date in its name or "
              "text lands here --")
        print("   record its year in data/external/_date_overrides.csv, then "
              "re-run this script.")
        for r in undated:
            print(f"     {r['owner']:18s} {r['file']}")

    # Any out-of-window file still sitting in an active folder needs quarantining.
    stale = [r for r in rows if r["scope_2020plus"] == "pre_2020"]
    if stale:
        print(f"\n!! {len(stale)} file(s) predating {since} are still in active "
              "folders. Run: python src/quarantine_pre2020.py")

    print(f"\n-> {out_csv}\n-> {ROOT / 'INDEX.md'}")
    return 0


if __name__ == "__main__":
    # Single-writer: this pipeline shares its files with another session.
    try:
        with pipeline_lock("index", force="--force" in sys.argv):
            raise SystemExit(main())
    except PipelineBusy as exc:
        print(exc)
        raise SystemExit(2)
