"""The single import point for the modelling session's code in src/.

Everything the UI borrows from src/ goes through here -- the bid-tab parser
(src/parse_bidtabs.py), the pipeline's size buckets (src/build_master.py) and
the production estimator (src/estimate_low_bid.py) -- so if a module is renamed
or reorganised upstream there is exactly one file to fix instead of a dozen
call sites.

Why borrow rather than reimplement: every reference table the estimator reads
was keyed by the parser's own functions -- desc_norm by norm_desc(), the family
by item_family(), the unit by norm_unit(), the size by extract_size_in() and
build_master.size_bucket(). If the UI classified a takeoff line with its own,
slightly different rules, every lookup would miss for reasons nobody could see.
`classify_line()` below runs the parser's per-line steps in the parser's order.

src/ is read-only for the UI -- nothing here writes to it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_UI_DIR)
SRC_DIR = os.path.join(_PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

#: True when src/parse_bidtabs.py imported cleanly. The UI shows a banner when
#: it did not, because classification silently degrades on the fallbacks.
PARSER_AVAILABLE = False
IMPORT_ERROR = ""

try:
    import parse_bidtabs as _pb  # type: ignore

    norm_desc = _pb.norm_desc
    norm_unit = _pb.norm_unit
    item_family = _pb.item_family
    extract_size_in = _pb.extract_size_in
    lf_width_size = _pb.lf_width_size
    unit_from_description = _pb.unit_from_description
    KNOWN_UNITS = set(_pb.KNOWN_UNITS)
    read_xlsx = _pb.read_xlsx
    read_pdf_best = _pb.read_pdf_best
    canon_bidder = _pb.canon_bidder
    PARSER_VERSION = getattr(_pb, "PARSER_VERSION", "")
    PARSER_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    IMPORT_ERROR = "%s: %s" % (type(e).__name__, e)
    PARSER_VERSION = ""
    log.error("src/parse_bidtabs.py did not import (%s); using degraded "
              "fallback normalisers. Classification WILL be worse.",
              IMPORT_ERROR)

    _FALLBACK_UNITS = {
        "ls": "LS", "lump sum": "LS", "lf": "LF", "sy": "SY", "sf": "SF",
        "cy": "CY", "cf": "CF", "ea": "EA", "each": "EA", "mo": "MO",
        "day": "DAY", "hr": "HR", "ton": "TON", "lb": "LB", "gal": "GAL",
        "ac": "AC", "sta": "STA", "allow": "ALLOW", "alw": "ALLOW",
        "lot": "LOT", "ft": "LF", "vf": "VF",
    }
    KNOWN_UNITS = set(_FALLBACK_UNITS.values())

    def norm_desc(d):  # type: ignore[misc]
        s = str(d or "").lower()
        s = s.replace('"', " in ")
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def norm_unit(v):  # type: ignore[misc]
        s = str(v or "").strip().lower().rstrip(".")
        if not s:
            return ""
        return _FALLBACK_UNITS.get(s, str(v).upper().strip())

    def item_family(desc_norm):  # type: ignore[misc]
        return "other"

    def extract_size_in(desc_norm):  # type: ignore[misc]
        m = re.search(r"(\d+(?:\.\d+)?)\s*in\b", desc_norm or "")
        return float(m.group(1)) if m else None

    def lf_width_size(r):  # type: ignore[misc]
        return r.get("size_in")

    def unit_from_description(unit_raw, desc):  # type: ignore[misc]
        return ""

    def canon_bidder(name):  # type: ignore[misc]
        return str(name or "").strip()

    def read_xlsx(path, stats):  # type: ignore[misc]
        raise RuntimeError(
            "src/parse_bidtabs.py is unavailable (%s); bid-tabulation import "
            "is disabled." % IMPORT_ERROR)

    def read_pdf_best(path, stats):  # type: ignore[misc]
        raise RuntimeError(
            "src/parse_bidtabs.py is unavailable (%s); bid-tabulation import "
            "is disabled." % IMPORT_ERROR)


# ---------------------------------------------------------------------------
# Size buckets -- build_master.size_bucket, the function that keyed the tables
# ---------------------------------------------------------------------------
#
# master_panel.size_bucket (and so every sized reference cell) is
# build_master.size_bucket(size_in). It is imported rather than copied; the
# copy below is used only if build_master itself fails to import, and the
# status endpoint says so.

SIZE_BUCKET_SOURCE = "src/build_master.py"
try:
    import build_master as _bm  # type: ignore

    def size_bucket(size_in):
        if size_in is None:
            return ""
        return _bm.size_bucket(size_in)
except Exception as e:  # noqa: BLE001
    SIZE_BUCKET_SOURCE = "fallback copy (build_master did not import: %s)" % e

    def size_bucket(size_in):  # type: ignore[misc]
        if size_in is None or size_in != size_in:
            return ""
        for lo, hi in [(0, 6), (6, 9), (9, 13), (13, 19), (19, 25), (25, 37),
                       (37, 1e9)]:
            if lo <= size_in < hi:
                return "%g-%g in" % (lo, hi) if hi < 1e9 else "37+ in"
        return ""


def classify_line(description, unit_raw):
    """One takeoff line, classified by the parser's own per-line steps.

    The order is the parser's (rows_from_grid, then its row post-pass):
      1. desc_norm = norm_desc(description)
      2. family    = item_family(desc_norm)
      3. size_in   = extract_size_in(desc_norm)
      4. unit      = norm_unit(raw unit); a bare YD / SQ that the row's own
                     words resolve (unit_from_description) takes that unit
      5. size_in   = lf_width_size(row) -- an LF sidewalk / trail line is
                     sized by its width in feet (after the unit, which it
                     depends on)
      6. size_bucket = build_master.size_bucket(size_in)
    Returns the fields the estimator's line dict needs, plus `unit_known`.
    """
    desc = str(description or "").strip()
    raw = str(unit_raw or "").strip()
    dn = norm_desc(desc)
    fam = item_family(dn)
    size_in = extract_size_in(dn)
    unit = norm_unit(raw)
    if raw and unit not in KNOWN_UNITS:
        u2 = unit_from_description(raw, desc)
        if u2:
            unit = u2
    size_in = lf_width_size({"unit": unit, "item_family": fam,
                             "description": desc, "size_in": size_in})
    return {
        "desc_norm": dn,
        "family": fam,
        "unit": unit,
        "unit_known": bool(unit) and unit in KNOWN_UNITS,
        "size_in": size_in,
        "size_bucket": size_bucket(size_in),
    }


def resize(description, unit, family):
    """size_in / size_bucket again after the user changed the family or unit
    (step 5 depends on both)."""
    desc = str(description or "").strip()
    size_in = extract_size_in(norm_desc(desc))
    size_in = lf_width_size({"unit": unit, "item_family": family,
                             "description": desc, "size_in": size_in})
    return size_in, size_bucket(size_in)


# ---------------------------------------------------------------------------
# The production estimator -- src/estimate_low_bid.py, read-only
# ---------------------------------------------------------------------------
#
# The UI prices ONLY through this module (ui/engine_production.py wraps it).
# It is imported, never patched: its switches stay at production's defaults.

ESTIMATOR_AVAILABLE = False
ESTIMATOR_ERROR = ""
estimator = None
comparables = None
try:
    import estimate_low_bid as estimator  # type: ignore  # noqa: F811
    import comparables  # type: ignore  # noqa: F811
    ESTIMATOR_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    ESTIMATOR_ERROR = "%s: %s" % (type(e).__name__, e)
    log.error("src/estimate_low_bid.py did not import (%s); nothing can be "
              "priced.", ESTIMATOR_ERROR)


def file_md5(path):
    """md5 of a file's bytes (as `md5` prints it), or "" when unreadable."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


def src_md5(name):
    return file_md5(os.path.join(SRC_DIR, name))


# ---------------------------------------------------------------------------
# OCR -- owned by the modelling session, not yet delivered.
# ---------------------------------------------------------------------------
#
# Agreed interface:  extract_words(path_or_image) -> list[dict]
# each dict with keys: text, x0, x1, top   (same shape as pdfplumber's
# extract_words(), so the coordinate column-splitting logic is reusable).
#
# Until src/ocr_extract.py lands, OCR_AVAILABLE is False and the UI refuses to
# produce an estimate from an image rather than inventing line items. See
# ui/ingest/ocr.py.

OCR_AVAILABLE = False
OCR_IMPORT_ERROR = "src/ocr_extract.py not delivered yet"
extract_words = None

try:
    import ocr_extract as _ocr  # type: ignore

    if hasattr(_ocr, "extract_words"):
        extract_words = _ocr.extract_words
        OCR_AVAILABLE = True
        OCR_IMPORT_ERROR = ""
    else:
        OCR_IMPORT_ERROR = ("src/ocr_extract.py imported but has no "
                            "extract_words(path_or_image) function")
except Exception as e:  # noqa: BLE001
    OCR_IMPORT_ERROR = "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# OCR for scanned PDFs -- the pipeline's own engines (src/ocr_scans.py)
# ---------------------------------------------------------------------------
#
# Apple Vision, RapidOCR (in .venv) and Tesseract (tools/), whichever this
# machine has, all offline. ocr_pages() renders each page and lays the words
# out as text rows; ui/ingest/takeoff.py parses those rows.

PDF_OCR_ENGINES = []
PDF_OCR_ERROR = ""
try:
    import ocr_scans as _ocrs  # type: ignore
    PDF_OCR_ENGINES = list(_ocrs.available_engines())
except Exception as e:  # noqa: BLE001
    PDF_OCR_ERROR = "%s: %s" % (type(e).__name__, e)


def _tesseract_turns(path):
    """{page: degrees} that set each page upright, by Tesseract's own
    orientation detection (the pipeline's ocr_scans.osd_turn). Apple Vision
    and RapidOCR find a page's orientation themselves; Tesseract reads a
    sideways scan as noise (a 46-page sideways bid tab: 1.8k characters
    read without the turns, 89k with them)."""
    import fitz
    turns = {}
    doc = fitz.open(str(path))
    try:
        for i, page in enumerate(doc):
            turns[i + 1] = _ocrs.osd_turn(page) or 0
    finally:
        doc.close()
    return turns


def ocr_pdf_pages(path, dpi=300, engine=None):
    """({page: laid-out text}, engine) for a scanned PDF, or ({}, "")."""
    if not PDF_OCR_ENGINES:
        return {}, ""
    import tempfile
    from pathlib import Path
    eng = engine or PDF_OCR_ENGINES[0]
    rots = _tesseract_turns(path) if eng.startswith("tess") else None
    with tempfile.TemporaryDirectory(prefix="ui_ocr_") as tmp:
        return _ocrs.ocr_pages(Path(path), dpi, Path(tmp), eng,
                               rots=rots), eng


def status() -> dict:
    return {
        "pdf_ocr_engines": PDF_OCR_ENGINES,
        "pdf_ocr_error": PDF_OCR_ERROR,
        "parser_available": PARSER_AVAILABLE,
        "parser_error": IMPORT_ERROR,
        "parser_version": PARSER_VERSION,
        "size_bucket_source": SIZE_BUCKET_SOURCE,
        "estimator_available": ESTIMATOR_AVAILABLE,
        "estimator_error": ESTIMATOR_ERROR,
        "ocr_available": OCR_AVAILABLE,
        "ocr_error": OCR_IMPORT_ERROR,
    }
