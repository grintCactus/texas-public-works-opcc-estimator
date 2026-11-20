"""OCR adapter.

The OCR engine is owned by the modelling session. The agreed interface is

    extract_words(path_or_image) -> list[dict]

with each dict carrying ``text, x0, x1, top`` -- the same shape pdfplumber's
``extract_words()`` returns, so the coordinate-based column splitting written
for PDFs is reusable on scans without a second implementation.

Until ``src/ocr_extract.py`` lands this module reports unavailable and the UI
refuses image input. It does NOT fabricate line items: a made-up takeoff that
flows into a priced estimate is worse than a clear error.
"""

from __future__ import annotations

from typing import Dict, List

import upstream

#: Set True only to exercise the downstream column-splitting code without a
#: real engine. Everything produced under this flag is tagged mock=True and
#: the UI refuses to price it.
MOCK_ENABLED = False

_MOCK_WORDS = [
    # A deliberately crooked three-column scan: desc / unit / qty.
    {"text": "Remove",        "x0": 40,  "x1": 95,  "top": 100},
    {"text": "Existing",      "x0": 98,  "x1": 160, "top": 100},
    {"text": "Riprap",        "x0": 163, "x1": 215, "top": 101},
    {"text": "SY",            "x0": 400, "x1": 420, "top": 100},
    {"text": "1,250",         "x0": 500, "x1": 545, "top": 100},
    {"text": "Gabion",        "x0": 40,  "x1": 95,  "top": 124},
    {"text": "Mattress",      "x0": 98,  "x1": 168, "top": 124},
    {"text": "SY",            "x0": 400, "x1": 420, "top": 124},
    {"text": "860",           "x0": 505, "x1": 545, "top": 125},
]


def available() -> bool:
    return bool(upstream.OCR_AVAILABLE) or MOCK_ENABLED


def status() -> Dict[str, object]:
    return {
        "available": available(),
        "real": bool(upstream.OCR_AVAILABLE),
        "mock": MOCK_ENABLED and not upstream.OCR_AVAILABLE,
        "error": upstream.OCR_IMPORT_ERROR,
        "interface": "src/ocr_extract.py :: extract_words(path_or_image) "
                     "-> list[{text, x0, x1, top}]",
    }


def extract_words(path: str) -> List[dict]:
    """Words with coordinates from an image or scanned page."""
    if upstream.OCR_AVAILABLE and upstream.extract_words is not None:
        words = upstream.extract_words(path)
        return [_coerce(w) for w in words]
    if MOCK_ENABLED:
        return [dict(w, mock=True) for w in _MOCK_WORDS]
    raise OCRUnavailable(
        "No OCR engine. %s. The agreed interface is "
        "src/ocr_extract.py :: extract_words(path_or_image) -> "
        "list[{text, x0, x1, top}]; drop that file in and image input starts "
        "working with no other change." % upstream.OCR_IMPORT_ERROR)


def _coerce(w) -> dict:
    """Accept the pdfplumber shape and anything close to it."""
    if not isinstance(w, dict):
        raise OCRContractError(
            "extract_words() returned %s, expected dict with text/x0/x1/top"
            % type(w).__name__)
    missing = [k for k in ("text", "x0", "x1", "top") if k not in w]
    if missing:
        raise OCRContractError(
            "extract_words() returned a word missing %s; got keys %s"
            % (", ".join(missing), ", ".join(sorted(w))))
    return {"text": str(w["text"]), "x0": float(w["x0"]),
            "x1": float(w["x1"]), "top": float(w["top"])}


class OCRUnavailable(RuntimeError):
    pass


class OCRContractError(RuntimeError):
    pass
