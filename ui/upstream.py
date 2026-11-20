"""The single import point for the modelling session's code in src/.

Everything the UI borrows from src/parse_bidtabs.py goes through here, so if
that module is renamed or reorganised upstream there is exactly one file to
fix instead of a dozen call sites.

Why borrow rather than reimplement: item_vocabulary.desc_norm and
master_panel.desc_norm were produced BY norm_desc(), and master_panel.unit by
norm_unit(). If the UI normalised user input with its own slightly different
rules, every lookup against those columns would miss for reasons no one could
see. Matching user text to the vocabulary REQUIRES using the same normaliser
that built it.

src/ is read-only for the UI -- nothing here writes to it.
"""

from __future__ import annotations

import logging
import os
import re
import sys

log = logging.getLogger(__name__)

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_UI_DIR)
_SRC = os.path.join(_PROJECT_ROOT, "src")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

#: True when src/parse_bidtabs.py imported cleanly. The UI shows a banner when
#: it did not, because matching quality silently degrades on the fallbacks.
PARSER_AVAILABLE = False
IMPORT_ERROR = ""

try:
    import parse_bidtabs as _pb  # type: ignore

    norm_desc = _pb.norm_desc
    norm_unit = _pb.norm_unit
    item_family = _pb.item_family
    extract_size_in = _pb.extract_size_in
    read_xlsx = _pb.read_xlsx
    read_pdf_best = _pb.read_pdf_best
    canon_bidder = _pb.canon_bidder
    PARSER_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    IMPORT_ERROR = "%s: %s" % (type(e).__name__, e)
    log.error("src/parse_bidtabs.py did not import (%s); using degraded "
              "fallback normalisers. Matching WILL be worse.", IMPORT_ERROR)

    _FALLBACK_UNITS = {
        "ls": "LS", "lump sum": "LS", "lf": "LF", "sy": "SY", "sf": "SF",
        "cy": "CY", "cf": "CF", "ea": "EA", "each": "EA", "mo": "MO",
        "day": "DAY", "hr": "HR", "ton": "TON", "lb": "LB", "gal": "GAL",
        "ac": "AC", "sta": "STA", "allow": "ALLOW", "alw": "ALLOW",
        "lot": "LOT", "ft": "LF", "vf": "VF",
    }

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


def status() -> dict:
    return {
        "parser_available": PARSER_AVAILABLE,
        "parser_error": IMPORT_ERROR,
        "ocr_available": OCR_AVAILABLE,
        "ocr_error": OCR_IMPORT_ERROR,
    }
