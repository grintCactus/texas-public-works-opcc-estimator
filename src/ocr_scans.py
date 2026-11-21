"""OCR the bid tabulations that are image-only scans, and cache the text.

Why this stage exists
---------------------
18 files in the corpus carry no text layer at all: every text extractor
(pdftotext, pdfplumber, PyMuPDF) returns an empty string, because the page is
a photograph of a printed tabulation. They are not junk -- their names are
`Bid Tab`, `Bid Tabulation`, `Unit Prices` -- so leaving them as `unreadable`
silently drops real line-item tables out of the dataset.

Engines.  Every available engine reads the file, and the sidecar keeps the
reading in which the most schedule rows BALANCE (qty x unit price = extended
total, `ocr_verify.verify_file`).  That identity does not depend on the
engine, and a reading cannot gain balanced rows by being damaged, so it is a
fair referee.  Measured 2026-11-19 on the 18 scans that already had a Windows
sidecar (balanced / checkable rows): Windows 169/170, RapidOCR 207/224,
Tesseract psm 4 161/166, psm 6 152/157 -- and no engine won every file
(Coppell 2023-7089: Tesseract 66, Windows 13; Rowlett 2021-14: Windows 94,
Tesseract 4), hence best-of rather than one engine.
  * `rapid`  RapidOCR (PaddleOCR models on onnxruntime; pip package
             rapidocr-onnxruntime).  Returns text segments; their words are
             spread across the segment's width.
  * `tess4`, `tess6`  Tesseract 5 (tools/tesseract, conda-forge) with page
             segmentation 4 (columns) and 6 (one block); word boxes from TSV.
  * `vision` macOS Vision `VNRecognizeTextRequest` through pyobjc.  It fails
             inside Claude Code's Bash sandbox (measured 2026-11-19), and is
             then skipped for the rest of the run.
  * `windows` Windows.Media.Ocr through the PowerShell bridge `_win_ocr.ps1`
             (only on Windows; it made the 18 original sidecars, which are
             kept unless another engine balances strictly more rows --
             copies in handoff/ocr_windows_sidecars/).
The chosen engine and every engine's score are written into the sidecar's
header and into data/external/_ocr_engine_choice.csv.

Which files, which pages (2026-11-19, round 2).  The audit's `unreadable`
class sees a whole FILE, so a scan inside a file with any text at all was
never OCR'd: Murphy's South Maxwell Creek and Southlake's Florence Tank tabs
are image pages behind a text-layer cover letter, the audit called both
`summary_only`, and they came out of the parse with 0 rows.  So the pages are
chosen one by one, from the PDF itself (page_survey / pages_to_ocr):
  * an IMAGE page -- under 30 characters of text and an image over 30% of
    the page -- is OCR'd;
  * an OVERPRINTED page -- over 15% of its glyphs drawn on a spot another,
    different glyph already holds (two text layers on top of each other) --
    is OCR'd too: what the eye sees there is not what the text layer says;
  * a VECTOR-GLYPH page -- under 200 text characters, no scan image over 30%
    of the page, and 200+ filled curve paths -- is OCR'd too: its words are
    drawn as outlines ("Microsoft: Print To PDF" of a spreadsheet, CAD
    exports), so it has neither a text layer nor an image (added 2026-11-21:
    NRH RFP 25-022's tab carried only its digital signature as text, 69
    characters, so the audit called the file readable and the image rule
    never fired; no sidecar, 0 rows);
  * every other page keeps its text layer.
In a file whose other pages have text, only target pages that look like a
bid tabulation are transcribed (table_pages: TABLE_MONEY amounts on the fast
engine's reading, words of a tabulation, no invoice): a council packet's
scanned letters, maps, invoices and signed forms (a 1295 certificate names
its signer) add nothing the parse can use.  A file with
no such page gets no sidecar.  A file that is scans throughout is read
whole, every page, as before.  A scanned page that carries its
    scanner's invisible OCR layer is read through that layer as before (212
    such pages measured, the worst still 55% clean words); parse_bidtabs
    reads a sidecar only for a file whose text gives it no rows.
Files the audit could not read at all are OCR'd whole, as before.  In a
file with both kinds, the sidecar marks each page: `### PAGE n (OCR)` or
`### PAGE n (text layer)` (the text layer re-laid out from PyMuPDF's word
boxes), so the audit, which prefers a sidecar, still sees the whole document
(audit_external re-reads the text-layer pages with its own extractor), and
parse_ocr_sidecars reads rows from the OCR'd pages only.  Long council
packets carry dozens of scanned pages that are letters, maps and signatures:
each target page is first read with one fast engine (TRIAGE_ENGINES), and
only pages that look like a price table (TABLE_MONEY amounts or more) are
read by every engine.

Sideways scans.  A target page whose fast reading has few amounts is read
again turned 90 and 270 degrees; a turn that reads far more amounts is the
page's orientation for every engine (Midlothian's 2023 one-page tabs are
landscape scans set on portrait pages).

Table rules.  Each OCR'd page is also searched for horizontal table rules
(find_rules: a dark run over a quarter of the page width), written into the
page as `### RULE` lines between the text rows they separate.  A cell's
wrapped description lines then belong to the price row between the same two
rules; parse_ocr_sidecars falls back to the rows' vertical positions where a
page has none (older sidecars, unruled tabs).

The output is a cached sidecar per PDF under `data/external/_ocr/`, NOT a
rewritten PDF. Reasons:
  * OCR is slow and varies between engine versions; caching it keeps every
    later audit run reproducible and free;
  * the original files stay byte-identical, so the duplicate census and the
    provenance/leakage check still see what the city actually published;
  * `_ocr/` is excluded from the audit's own file walk, so the sidecars are
    never mistaken for source documents.

`audit_external.read_pdf_text` looks for the sidecar before trying anything
else, so once this has run the rest of the pipeline needs no special casing.

OCR text is approximate, and digits are its weak point ("5"/"S", "0"/"O",
"1"/"l") -- which is exactly the part that matters in a price table. So every
sidecar carries a header saying it is machine-read, and that header travels
with the text into the audit, so a number sourced from one can be traced back
and re-checked by eye before it is used in a bid.

Usage:
    python src/ocr_scans.py                 # OCR every image page not yet read
    python src/ocr_scans.py --list          # just show what would be done
    python src/ocr_scans.py --dpi 400       # re-render higher and re-OCR
    python src/ocr_scans.py --file <rel>    # one file
    python src/ocr_scans.py --rescore       # also re-read files that HAVE a
                                            # sidecar; replace it only when an
                                            # engine balances strictly more rows
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("data/external")
OCR_DIR = ROOT / "_ocr"
CHOICE_CSV = ROOT / "_ocr_engine_choice.csv"
TARGETS_CSV = ROOT / "_ocr_page_targets.csv"     # the pages the last survey would OCR
PS1 = Path(__file__).resolve().parent / "_win_ocr.ps1"
TESSERACT = Path(__file__).resolve().parent.parent / "tools" / "tesseract" / "bin" / "tesseract"

ENGINE_NAMES = {
    "vision": "Apple Vision VNRecognizeTextRequest (accurate, en-US, no language correction)",
    "rapid": "RapidOCR (rapidocr-onnxruntime, PaddleOCR models)",
    "tess4": "Tesseract 5 (eng, page segmentation 4)",
    "tess6": "Tesseract 5 (eng, page segmentation 6)",
    "windows": "Windows.Media.Ocr (en-US)",
}

HEADER = (
    "### MACHINE-READ (OCR) -- not a text layer from the PDF.\n"
    "### Source: {src}\n"
    "### Engine: {engine}, page rendered at {dpi} dpi.\n"
    "### Digits here are NOT reliable enough to bid from directly.\n"
    "### Verify any unit price against the PDF by eye before use.\n"
)


RULE_LINE = "### RULE"     # a horizontal table rule between two text rows


def rows_to_layout(words, char_w: float, row_tol: float, rules=None) -> str:
    """Rebuild a column-aligned page from OCR word boxes.

    The line-item patterns need description, unit and price on one line, so
    words are clustered into visual rows by y and padded out by x. char_w is
    in pixels here (the page is rendered at `dpi`), not points.

    rules: y positions (same pixels) of horizontal table rules on the page;
    each is written as a RULE_LINE between the two text rows it lies between
    (below the top of the one, above the top of the next).
    """
    if not words:
        return ""
    words = sorted(words, key=lambda w: (w[2], w[1]))
    rows, cur, cur_y = [], [], None
    for w in words:
        y = w[2]
        if cur_y is None or abs(y - cur_y) <= row_tol:
            cur.append(w)
            cur_y = y if cur_y is None else (cur_y + y) / 2
        else:
            rows.append(cur)
            cur, cur_y = [w], y
    if cur:
        rows.append(cur)
    rules = sorted(rules or [])
    out = []
    k = 0
    for r in rows:
        r.sort(key=lambda w: w[1])
        top = min(w[2] for w in r)
        n = 0
        while k < len(rules) and rules[k] < top:
            k += 1
            n += 1
        if n and out:
            out.append(RULE_LINE)
        line = ""
        for w in r:
            txt, x = w[0], w[1]
            col = int(x / char_w)
            if col < len(line):
                col = len(line) + 1
            line += " " * (col - len(line)) + txt
        out.append(line.rstrip())
    if k < len(rules) and out:
        out.append(RULE_LINE)
    return "\n".join(out)


def render(page, dpi: int, rot: int = 0, gray: bool = False):
    """The page as an image at `dpi`, turned `rot` degrees (a tabulation
    scanned sideways: Midlothian's 2023 one-page tabs are landscape scans
    on portrait pages)."""
    import fitz
    m = fitz.Matrix(dpi / 72.0, dpi / 72.0).prerotate(rot)
    return page.get_pixmap(matrix=m, colorspace=fitz.csGRAY if gray else fitz.csRGB)


def find_rules(page, dpi: int, min_frac: float = 0.25, rot: int = 0):
    """y positions, in pixels at `dpi`, of the page's horizontal rules: a
    pixel row with a dark run (gaps under 4 px bridged) over min_frac of
    the page width.  A skewed scan's rules break into short runs and are
    not found -- the parser then falls back to positions."""
    import numpy as np
    pix = render(page, dpi, rot, gray=True)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    dark = a < 140
    k = 4
    w4 = dark.shape[1] // k
    if w4 == 0:
        return []
    d4 = dark[:, :w4 * k].reshape(dark.shape[0], w4, k).any(axis=2).astype(np.int8)
    need = min_frac * w4
    ys = []
    for y in np.nonzero(d4.sum(axis=1) >= need)[0]:
        e = np.diff(np.concatenate(([0], d4[y], [0])))
        st, en = np.nonzero(e == 1)[0], np.nonzero(e == -1)[0]
        if len(st) and (en - st).max() >= need:
            ys.append(int(y))
    out = []
    for y in ys:                                  # a rule is several pixels thick
        if out and y - out[-1][-1] <= 2:
            out[-1].append(y)
        else:
            out.append([y])
    return [sum(g) / len(g) for g in out]


def ocr_page_vision(png_bytes: bytes, width: int, height: int):
    """Word boxes from Apple Vision as (text, x_left, y_top) in page pixels.

    Vision gives one observation per text line, with a normalised box whose
    origin is the image's BOTTOM-left.  Each word's own box comes from
    boundingBoxForRange on that word's span of the line; NSString ranges count
    UTF-16 units, hence the encode() below.  Language correction is off: it
    "fixes" item codes and abbreviations into dictionary words, and the digits
    are the part that matters here.
    """
    import Vision
    from Foundation import NSData, NSMakeRange

    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(["en-US"])
    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError("Vision failed: %s" % err)

    def u16(s, i):
        return len(s[:i].encode("utf-16-le")) // 2

    words = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        rt = cands[0]
        line = str(rt.string())
        for m in re.finditer(r"\S+", line):
            a, b = u16(line, m.start()), u16(line, m.end())
            box, _err = rt.boundingBoxForRange_error_(NSMakeRange(a, b - a), None)
            if box is None:
                continue
            bb = box.boundingBox()
            x = bb.origin.x * width
            y = (1.0 - bb.origin.y - bb.size.height) * height
            words.append((m.group(0), x, y))
    return words


def ocr_page(png: Path):
    """Word boxes from Windows.Media.Ocr through the PowerShell bridge."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(PS1), str(png)],
        capture_output=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:400])
    words = []
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        parts = ln.split("\t")
        if len(parts) < 5:
            continue
        try:
            words.append((parts[0], float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return words


_RAPID = None


def ocr_page_rapid(png: Path):
    """Word boxes from RapidOCR.  It returns text SEGMENTS (often one table
    cell, sometimes a run of cells); the words of a segment are spread across
    its width in proportion to their length, which is what rows_to_layout
    needs to put each number in its column."""
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID = RapidOCR()
    res, _ = _RAPID(str(png))
    words = []
    for box, text, _score in res or []:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        x0, x1, y0 = min(xs), max(xs), min(ys)
        parts = text.split()
        total = sum(len(w) for w in parts) + len(parts) - 1
        pos = 0
        for w in parts:
            words.append((w, x0 + (x1 - x0) * pos / max(total, 1), y0))
            pos += len(w) + 1
    return words


def ocr_page_tesseract(png: Path, psm: str, dpi: int):
    """Word boxes from Tesseract's TSV output (level 5 = word)."""
    import os
    env = dict(os.environ, OMP_THREAD_LIMIT="1",
               TESSDATA_PREFIX=str(TESSERACT.parent.parent / "share" / "tessdata"))
    r = subprocess.run([str(TESSERACT), str(png), "-", "--psm", psm, "--dpi", str(dpi), "tsv"],
                       capture_output=True, env=env, timeout=600)
    words = []
    for ln in r.stdout.decode("utf-8", "replace").splitlines()[1:]:
        p = ln.split("\t")
        if len(p) < 12 or p[0] != "5" or not p[11].strip():
            continue
        words.append((p[11].strip(), float(p[6]), float(p[7])))
    if not words and r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    return words


def osd_turn(page, dpi: int = 150):
    """The turn (0, 90 or 270 degrees, as render() takes it) that sets a
    page's print upright, by Tesseract's orientation detection: each turn is
    rendered and asked, and the one Tesseract reads as upright ('Rotate: 0')
    with the most confidence wins.  None without Tesseract's osd data."""
    import os
    osd = TESSERACT.parent.parent / "share" / "tessdata" / "osd.traineddata"
    if not (TESSERACT.exists() and osd.exists()):
        return None
    env = dict(os.environ, OMP_THREAD_LIMIT="1",
               TESSDATA_PREFIX=str(TESSERACT.parent.parent / "share" / "tessdata"))
    best, best_conf = 0, -1.0
    with tempfile.TemporaryDirectory() as td:
        png = Path(td).resolve() / "osd.png"
        for rot in (0, 90, 270):
            render(page, dpi, rot, gray=True).save(str(png))
            r = subprocess.run([str(TESSERACT), str(png), "-", "--psm", "0"],
                               capture_output=True, env=env, timeout=120)
            out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
            m_rot = re.search(r"Rotate:\s*(\d+)", out)
            m_conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
            if m_rot and m_conf and int(m_rot.group(1)) == 0 and float(m_conf.group(1)) > best_conf:
                best, best_conf = rot, float(m_conf.group(1))
    return best


def available_engines():
    """Engines this machine can run, in tie-break order (earlier wins a tie)."""
    out = []
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401
            out.append("vision")
        except ImportError:
            pass
    if sys.platform == "win32" and PS1.exists():
        out.append("windows")
    try:
        import rapidocr_onnxruntime  # noqa: F401
        out.append("rapid")
    except ImportError:
        pass
    if TESSERACT.exists():
        out += ["tess4", "tess6"]
    return out


def ocr_page_words(page, i: int, dpi: int, tmp: Path, engine: str, rot: int = 0):
    """Word boxes of one rendered page from one engine."""
    pix = render(page, dpi, rot)
    if engine == "vision":
        return ocr_page_vision(pix.tobytes("png"), pix.width, pix.height)
    png = tmp / ("p%03d.png" % i)
    pix.save(str(png))
    try:
        if engine == "windows":
            return ocr_page(png)
        if engine == "rapid":
            return ocr_page_rapid(png)
        if engine in ("tess4", "tess6"):
            return ocr_page_tesseract(png, engine[-1], dpi)
        raise ValueError("unknown OCR engine %r" % engine)
    finally:
        png.unlink(missing_ok=True)


def ocr_pages(pdf: Path, dpi: int, tmp: Path, engine: str, pages=None, rules=None,
              rots=None) -> dict:
    """{page number: laid-out OCR text} for `pages` (1-based; None = all).
    rules: {page number: rule y positions} to write in as RULE lines;
    rots: {page number: degrees} to turn a page before reading it."""
    import fitz

    # Leptonica (inside Tesseract) rewrites any path starting with "/tmp" to
    # its own temp dir, so a page under /tmp/... is "not found"; the real path
    # (/private/tmp/... on macOS) is left alone.
    tmp = Path(tmp).resolve()
    doc = fitz.open(str(pdf))
    try:
        out = {}
        for i, page in enumerate(doc):
            if pages is not None and (i + 1) not in pages:
                continue
            words = ocr_page_words(page, i, dpi, tmp, engine, (rots or {}).get(i + 1, 0))
            # char width and row tolerance scale with render resolution
            out[i + 1] = rows_to_layout(words, char_w=5.0 * dpi / 72.0,
                                        row_tol=12.0 * dpi / 300.0,
                                        rules=(rules or {}).get(i + 1))
        return out
    finally:
        doc.close()


def ocr_pdf(pdf: Path, dpi: int, tmp: Path, engine: str) -> str:
    """Every page of a PDF OCR'd, as sidecar page blocks (the whole-file form)."""
    texts = ocr_pages(pdf, dpi, tmp, engine)
    return "\n\n".join("### PAGE %d\n%s" % (n, t) for n, t in sorted(texts.items()))


def text_layer_page(page) -> str:
    """One page's own text layer, re-laid out from PyMuPDF's word boxes the
    way audit_external.read_pdf_text_mupdf does (points, ~5 pt a character)."""
    words = [(w[4], w[0], w[1]) for w in page.get_text("words")]
    return rows_to_layout(words, char_w=5.0, row_tol=3.0)


# ---------------------------------------------------------------- which pages

IMAGE_MAX_CHARS = 30      # a page with fewer text characters than this ...
IMAGE_MIN_COVER = 0.30    # ... and an image over this share of it is a scan
OVERPRINT_SHARE = 0.15    # glyphs drawn where a different glyph already is
OUTLINE_MAX_CHARS = 200   # a page with fewer text characters than this ...
OUTLINE_MIN_PATHS = 200   # ... and this many filled curve paths draws its words


def _glyph_paths(page) -> int:
    """Filled paths with a curve segment: letters and digits drawn as vector
    outlines.  Table rules and cell boxes are straight; chart lines are
    stroked, not filled.  Measured 2026-11-21 on data/external: outlined tabs
    281-3,194 per page (NRH, Carrollton, Frisco, Richardson), a CAD motor drawing 168,
    the TxDOT index binder's charts 102."""
    n = 0
    for d in page.get_drawings():
        if d.get("fill") is not None and any(it[0] == "c" for it in d.get("items", ())):
            n += 1
    return n


def _overprint(page) -> float:
    """Share of a page's glyphs drawn on the spot (to 1 pt) of a different
    glyph: two text layers printed over each other.  A glyph drawn twice in
    place (fake bold) is the same glyph and does not count."""
    seen, dup, n = {}, 0, 0
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                for c in sp.get("chars", []):
                    ch = c["c"]
                    if not ch.strip():
                        continue
                    n += 1
                    k = (round(c["bbox"][0]), round(c["bbox"][1]))
                    if k in seen and seen[k] != ch:
                        dup += 1
                    seen.setdefault(k, ch)
    return dup / max(1, n)


def page_survey(pdf: Path, max_pages: int = 400):
    """Per page: text characters, image cover (share of the page), the
    overprinted-glyph share, and (near-empty pages only) the filled curve
    paths that draw outlined words."""
    import fitz
    out = []
    doc = fitz.open(str(pdf))
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            n_chars = len("".join(page.get_text("text").split()))
            area = abs(page.rect) or 1.0
            cover = 0.0
            try:
                for im in page.get_image_info():
                    cover += abs(fitz.Rect(im["bbox"]) & page.rect)
            except Exception:                                     # noqa: BLE001
                pass
            cover = min(1.0, cover / area)
            glyphs = 0
            if n_chars < OUTLINE_MAX_CHARS and cover <= IMAGE_MIN_COVER:
                try:   # only near-empty pages pay for get_drawings()
                    glyphs = _glyph_paths(page)
                except Exception:                                 # noqa: BLE001
                    glyphs = 0
            out.append({"page": i + 1, "chars": n_chars, "img_cover": cover,
                        "overprint": _overprint(page) if n_chars >= 80 else 0.0,
                        "glyphs": glyphs})
    finally:
        doc.close()
    return out


def pages_to_ocr(survey) -> dict:
    """{page number: reason} of the pages a reader can only get by OCR."""
    out = {}
    for s in survey:
        if s["chars"] < IMAGE_MAX_CHARS and s["img_cover"] > IMAGE_MIN_COVER:
            out[s["page"]] = "image"
        elif s["overprint"] > OVERPRINT_SHARE:
            out[s["page"]] = "overprinted text layer"
        elif (s["chars"] < OUTLINE_MAX_CHARS and s["img_cover"] <= IMAGE_MIN_COVER
              and s.get("glyphs", 0) >= OUTLINE_MIN_PATHS):
            out[s["page"]] = "vector glyphs"
    return out


def _survey_one(path):
    try:
        return path, pages_to_ocr(page_survey(Path(path))), ""
    except Exception as exc:                                      # noqa: BLE001
        return path, {}, str(exc)[:80]


_MONEY = re.compile(r"\$?\d{1,3}(?:,\d{3})*\.\d{2}\b")
WIN_BACKUP = Path("handoff/ocr_windows_sidecars")   # the PC's Windows readings


def score(full_text: str):
    """Sort key for one reading of a scan (header included).

    First what the sidecar parser can actually USE (parse_ocr_sidecars.
    yield_score): cells it keeps -- each passed qty x price = total and sits
    unambiguously under a named bidder -- counted in full when the row has a
    unit and half when it has none (a price without a unit stays in the
    letting's totals but never reaches a unit-price table; Coppell 2023-7089's
    Windows reading keeps 340 cells, 96 with a unit, its Tesseract reading
    320, all with units), and the bidders it names.  Then balanced
    multiplicative rows, balanced lump-sum rows, money tokens read.  The
    balanced-row count alone, used first on 2026-11-19, cannot see a reading
    that loses bidder names or the empty-cell markers that keep prices in
    their columns: NRH 4690 keeps 43 cells from its Windows reading and 24
    from the Vision one that count preferred.  No part of the key can rise
    because a reading got worse."""
    import ocr_verify
    t = ocr_verify.verify_file(full_text)
    try:
        import parse_ocr_sidecars as pos
        d = pos.yield_detail(full_text)
    except Exception:                                             # noqa: BLE001
        d = {"cells": 0, "with_unit": 0, "bidders": 0, "usable": 0.0}
    t.update(d)
    return (d["usable"], d["bidders"], t["mult_ok"], t["ls_ok"],
            len(_MONEY.findall(full_text))), t


def read_all(pdf: Path, dpi: int, engines, dead: set, pages=None, rules=None, rots=None):
    """{engine: {page: text}} from every live engine; {engine: Exception}
    where one failed (Vision is marked dead after its first failure)."""
    out = {}
    for eng in engines:
        if eng in dead:
            continue
        try:
            with tempfile.TemporaryDirectory() as td:
                out[eng] = ocr_pages(pdf, dpi, Path(td), eng, pages, rules, rots)
        except Exception as exc:                                  # noqa: BLE001
            if eng == "vision":
                dead.add(eng)      # sandboxed: fails every time, don't retry
            out[eng] = exc
    return out


def _fmt(t):
    return "%dc(%du)/%db %d/%d" % (t["cells"], t["with_unit"], t["bidders"], t["mult_ok"], t["mult"])


SKIP_DIRS = ("_ocr", "_pre2020", "dol", "txdot_archive")
TRIAGE_ENGINES = ("tess4", "rapid", "tess6", "vision", "windows")   # first available reads first
TABLE_MONEY = 6           # amounts on a page that make it worth every engine


def targets(survey: bool = True, only=None):
    """{relative path: {page: reason} or None (= every page)}.

    Files the audit could not read are OCR'd whole.  Read from _audit.csv
    rather than a hardcoded list: the set shrinks as the text extractors
    improve.  The existence check is not cosmetic: _audit.csv keeps rows for
    files the quarantine step moved out of the corpus (177 at the time of
    writing), and OCR would otherwise spend its time on excluded bids.

    Every other PDF is surveyed page by page (pages_to_ocr): a file whose
    image or overprinted pages the audit never looked at -- because some
    OTHER page had text -- gets those pages OCR'd.
    """
    out = {}
    ap = ROOT / "_audit.csv"
    if ap.exists():
        for r in csv.DictReader(ap.open(encoding="utf-8-sig")):
            if (r.get("classification") in ("unreadable", "") and r["file"].lower().endswith(".pdf")
                    and (ROOT / r["file"]).exists()):
                out[r["file"]] = None
    else:
        print("note: _audit.csv is missing (run src/audit_external.py); surveying pages only")
    if not survey:
        return out
    from concurrent.futures import ProcessPoolExecutor
    files = [p for p in sorted(ROOT.rglob("*.pdf"))
             if not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)]
    files = [str(p) for p in files if p.relative_to(ROOT).as_posix() not in out]
    if only:
        files = [f for f in files if only in f]
    with ProcessPoolExecutor(max_workers=8) as ex:
        for path, pages, err in ex.map(_survey_one, files, chunksize=4):
            if pages:
                out[Path(path).relative_to(ROOT).as_posix()] = pages
    if not only:
        # every page a reader can only get by OCR, for qc_report's alarm on
        # scanned pages that were never read (qc_report.scans_not_read)
        with TARGETS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file", "page", "reason"])
            for rel in sorted(out):
                if out[rel] is None:
                    w.writerow([rel, "all", "audit: unreadable"])
                else:
                    for pg, why in sorted(out[rel].items()):
                        w.writerow([rel, pg, why])
    return out


def table_pages(texts: dict) -> set:
    """Pages of a fast OCR reading that look like a bid tabulation: TABLE_MONEY
    amounts or more, not an invoice or receipt, and words of a tabulation
    ('bid', 'tabulation', 'proposal', 'unit bid') on the page -- or on the
    page before, which this one continues."""
    import parse_ocr_sidecars as pos
    out, prev = set(), None
    for pg in sorted(texts):
        t = texts[pg]
        ok = (len(_MONEY.findall(t)) >= TABLE_MONEY and not pos.invoice_page(t)
              and (pos._TAB_WORDS.search(t) is not None or prev == pg - 1))
        if ok:
            out.add(pg)
            prev = pg
    return out


def _sidecar_text(rel, dpi, engine_label, n_pages, ocr_texts, layer_texts, reasons, choice=""):
    """A sidecar: the MACHINE-READ header, then every page -- OCR'd pages
    from `ocr_texts`; in a file read only in part, the others from
    `layer_texts`, marked as the PDF's own text layer."""
    head = HEADER.format(src=rel, dpi=dpi, engine=engine_label) + choice
    partial = len(ocr_texts) < n_pages
    if partial:
        by = {}
        for pg, why in sorted((reasons or {}).items()):
            by.setdefault(why, []).append(str(pg))
        head += ("### OCR pages: %s; every other page is the PDF's own text layer.\n"
                 % "; ".join("%s (%s)" % (", ".join(v), k) for k, v in by.items()))
    blocks = []
    for n in range(1, n_pages + 1):
        if n in ocr_texts:
            blocks.append("### PAGE %d%s\n%s" % (n, " (OCR)" if partial else "", ocr_texts[n]))
        elif partial:
            blocks.append("### PAGE %d (text layer)\n%s" % (n, layer_texts.get(n, "")))
    return head + "\n" + "\n\n".join(blocks)


def _selftest() -> bool:
    """Page targeting, table triage, rule detection and the sidecar layout,
    on synthetic pages; nothing under data/ is read or written."""
    import fitz
    fails = []

    def check(name, cond, extra=""):
        if not cond:
            fails.append("FAIL %s %s" % (name, extra))

    # which pages: an image page, an overprinted page, a text page, a
    # scanned page under its scanner's text layer (kept)
    survey = [{"page": 1, "chars": 3, "img_cover": 0.95, "overprint": 0.0},
              {"page": 2, "chars": 900, "img_cover": 0.0, "overprint": 0.4},
              {"page": 3, "chars": 900, "img_cover": 0.0, "overprint": 0.0},
              {"page": 4, "chars": 1200, "img_cover": 0.98, "overprint": 0.0},
              {"page": 5, "chars": 0, "img_cover": 0.1, "overprint": 0.0},
              {"page": 6, "chars": 69, "img_cover": 0.01, "overprint": 0.0, "glyphs": 1346},
              {"page": 7, "chars": 20, "img_cover": 0.0, "overprint": 0.0, "glyphs": 168},
              {"page": 8, "chars": 900, "img_cover": 0.0, "overprint": 0.0, "glyphs": 5000}]
    check("pages_to_ocr", pages_to_ocr(survey) == {1: "image", 2: "overprinted text layer",
                                                   6: "vector glyphs"},
          repr(pages_to_ocr(survey)))
    # outlined words (filled curves) are counted; straight table rules and
    # stroked curves are not
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    for k in range(40):
        x, y = 40 + (k % 10) * 50, 60 + (k // 10) * 30
        sh = pg.new_shape()
        sh.draw_bezier((x, y), (x + 5, y - 8), (x + 10, y - 8), (x + 12, y))
        sh.finish(fill=(0, 0, 0), color=None, closePath=True)
        sh.commit()
    for y in (300, 320, 340):
        pg.draw_line((40, y), (560, y), width=1.0)
    pg.draw_bezier((40, 500), (100, 450), (200, 550), (300, 500), color=(0, 0, 0))
    check("glyph paths", _glyph_paths(pg) == 40, repr(_glyph_paths(pg)))
    doc.close()
    # triage: a tabulation page, its continuation, an invoice, a letter
    tab = "BID TABULATION\n1 Sod 100 SY 5.00 500.00 6.00 600.00 7.00 700.00\n" \
          "2 Seed 10 SY 1.00 10.00 2.00 20.00 3.00 30.00"
    cont = "3 Mulch 10 SY 3.00 30.00 4.00 40.00 5.00 50.00\n4 Rock 2 CY 9.00 18.00 8.00 16.00"
    inv = "INVOICE  Bill To: City\n1 Saw 200 LF 1.44 288.00\n2 Cut 2 EA 5.00 10.00\n" \
          "Amount Due 298.00 1.00 2.00 3.00 4.00"
    letter = "Dear Council, the low bid of $1,234.56 is recommended."
    got = table_pages({3: tab, 4: cont, 6: inv, 7: letter, 9: cont})
    check("table_pages", got == {3, 4}, repr(got))
    # rules on a rendered page; rows_to_layout writes them between rows
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    for y in (100, 160, 220):
        pg.draw_line((50, y), (560, y), width=1.2)
    pg.insert_text((60, 130), "1 Excavation   100 CY   10.00   1,000.00", fontsize=10)
    pg.insert_text((60, 145), "  unsuitable material", fontsize=10)
    pg.insert_text((60, 190), "2 Sod          300 SY    2.00     600.00", fontsize=10)
    ys = find_rules(pg, 150)
    check("find_rules", len(ys) == 3 and all(abs(a - b * 150 / 72) < 4 for a, b in
                                              zip(ys, (100, 160, 220))), repr(ys))
    words = [(w[4], w[0] * 150 / 72, w[1] * 150 / 72) for w in pg.get_text("words")]
    lay = rows_to_layout(words, 5.0 * 150 / 72, 12.0 * 150 / 300, rules=ys).splitlines()
    check("rules between rows", [ln == RULE_LINE for ln in lay] == [False, False, True, False, True],
          repr(lay))
    doc.close()
    # a table scanned sideways: its rules are vertical as the page stands,
    # horizontal once the page is turned
    doc = fitz.open()
    pg = doc.new_page(width=612, height=792)
    for x in (100, 160, 220):
        pg.draw_line((x, 50), (x, 740), width=1.2)
    check("sideways rules found only when turned",
          find_rules(pg, 100) == [] and len(find_rules(pg, 100, rot=90)) == 3
          and len(find_rules(pg, 100, rot=270)) == 3,
          repr((find_rules(pg, 100), find_rules(pg, 100, rot=90))))
    doc.close()
    # a sidecar of a file read in part marks every page's source
    sc = _sidecar_text("x/y.pdf", 300, "Tesseract", 3, {2: "OCR TWO"}, {1: "ONE", 3: "THREE"},
                       {2: "image"})
    check("partial sidecar", "### PAGE 1 (text layer)\nONE" in sc and "### PAGE 2 (OCR)\nOCR TWO" in sc
          and "### OCR pages: 2 (image)" in sc, sc)
    whole = _sidecar_text("x/y.pdf", 300, "Tesseract", 2, {1: "A", 2: "B"}, {}, None)
    check("whole-file sidecar unchanged", "### PAGE 1\nA" in whole and "PAGE 1 (OCR)" not in whole,
          whole)
    for f in fails:
        print(f)
    print("ocr_scans selftest: %s" % ("OK" if not fails else "%d FAILED" % len(fails)))
    return not fails


def main() -> int:
    import fitz
    if sys.argv[1:2] == ["--selftest"]:
        return 0 if _selftest() else 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--file", default=None,
                    help="one path relative to data/external")
    ap.add_argument("--redo", action="store_true",
                    help="re-OCR even if cached, and replace the sidecar whatever it scores")
    ap.add_argument("--rescore", action="store_true",
                    help="also re-read files that have a sidecar (every sidecar under _ocr/); "
                         "replace one only when an engine scores strictly higher")
    ap.add_argument("--no-survey", action="store_true",
                    help="only the files the audit could not read (the pre-2026-11-19 behaviour)")
    args = ap.parse_args()

    if args.file:
        found = targets(survey=True, only=args.file)
        todo = {args.file: found.get(args.file)}
        if args.file not in found:
            # asked for by name: its image pages, else (no image page) every page
            pdf = ROOT / args.file
            pg = pages_to_ocr(page_survey(pdf)) if pdf.exists() else {}
            todo[args.file] = pg or None
    else:
        todo = targets(survey=not args.no_survey)
        if args.rescore:
            for p in OCR_DIR.rglob("*.txt"):
                rel = str(p.relative_to(OCR_DIR))[:-4]
                if (ROOT / rel).exists() and rel not in todo:
                    todo[rel] = None
    if not todo:
        print("nothing to OCR")
        return 0

    if args.list:
        judged = set()
        if CHOICE_CSV.exists():
            judged = {r["file"] for r in csv.DictReader(CHOICE_CSV.open(encoding="utf-8-sig"))
                      if r.get("chosen") == "none"}
        for rel in sorted(todo):
            sc = OCR_DIR / (rel + ".txt")
            pages = todo[rel]
            what = ("every page" if pages is None else
                    "pages " + ", ".join("%d %s" % kv for kv in sorted(pages.items())))
            state = "cached" if sc.exists() else "no tab" if rel in judged else "to do "
            print("  %s  %s  [%s]" % (state, rel, what))
        print("\n%d files" % len(todo))
        return 0

    engines = available_engines()
    print("engines: %s" % ", ".join(engines))
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    dead, log = set(), []
    done = kept = skipped = failed = 0
    # files a previous run found no price table on (chosen 'none') are not
    # read again unless asked (--redo / --rescore / --file)
    judged = set()
    if CHOICE_CSV.exists() and not (args.redo or args.rescore or args.file):
        judged = {r["file"] for r in csv.DictReader(CHOICE_CSV.open(encoding="utf-8-sig"))
                  if r.get("chosen") == "none"}
    for n, rel in enumerate(sorted(todo), 1):
        pdf = ROOT / rel
        sidecar = OCR_DIR / (rel + ".txt")
        old = sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
        if (old is not None or rel in judged) and not (args.redo or args.rescore):
            skipped += 1
            continue
        if not pdf.exists():
            print("[%d/%d] MISSING  %s" % (n, len(todo), rel))
            failed += 1
            continue
        reasons = todo[rel]
        with fitz.open(str(pdf)) as doc:
            n_pages = doc.page_count
            want = set(range(1, n_pages + 1)) if reasons is None else set(reasons)
            rules = {i + 1: find_rules(doc[i], args.dpi) for i in range(n_pages) if (i + 1) in want}
            layer = ({} if reasons is None else
                     {i + 1: text_layer_page(doc[i]) for i in range(n_pages)})
        # one fast engine first; only pages that look like a price table go
        # to every engine (a council packet's scanned letters do not)
        tri_eng = next((e for e in TRIAGE_ENGINES if e in engines and e not in dead), None)
        tri = read_all(pdf, args.dpi, [tri_eng], dead, want, rules).get(tri_eng) if tri_eng else None
        if not isinstance(tri, dict):
            tri = {}
        # a page whose reading has few amounts may be a tabulation scanned
        # sideways: it is read turned a quarter each way, and the turn that
        # reads the most amounts (TABLE_MONEY at least, twice the upright
        # reading's) is the page's orientation for every engine
        rots = {}
        # (a page that reads as text upright -- a letter, a form -- is not
        # turned: sideways print reads as debris, not as 40 real words)
        weak = [pg for pg in sorted(want) if len(_MONEY.findall(tri.get(pg, ""))) < TABLE_MONEY
                and len(re.findall(r"\b[A-Za-z][a-z]{2,}\b", tri.get(pg, ""))) < 40]
        # A page the PDF itself turns (/Rotate) may hold a scan set sideways
        # that reads as amounts anyway, column by column (Midlothian's 7th
        # Street tab): Tesseract's orientation detection decides those.
        with fitz.open(str(pdf)) as doc:
            for pg in sorted(want):
                if pg in weak or doc[pg - 1].rotation == 0:
                    continue
                turn = osd_turn(doc[pg - 1])
                if turn:
                    rrules = {pg: find_rules(doc[pg - 1], args.dpi, rot=turn)}
                    alt = read_all(pdf, args.dpi, [tri_eng], dead, {pg}, rrules,
                                   {pg: turn}).get(tri_eng) if tri_eng else None
                    if isinstance(alt, dict) and pg in alt:
                        tri[pg], rots[pg], rules[pg] = alt[pg], turn, rrules[pg]
        if weak and tri_eng:
            with fitz.open(str(pdf)) as doc:
                for rot in (90, 270):
                    rrules = {pg: find_rules(doc[pg - 1], args.dpi, rot=rot) for pg in weak}
                    alt = read_all(pdf, args.dpi, [tri_eng], dead, set(weak), rrules,
                                   {pg: rot for pg in weak}).get(tri_eng)
                    if not isinstance(alt, dict):
                        continue
                    for pg in weak:
                        n_alt = len(_MONEY.findall(alt.get(pg, "")))
                        n_cur = len(_MONEY.findall(tri.get(pg, "")))
                        if n_alt >= TABLE_MONEY and n_alt >= 2 * max(1, n_cur):
                            tri[pg], rots[pg], rules[pg] = alt[pg], rot, rrules[pg]
        table = table_pages(tri)
        whole = reasons is None or len(want) == n_pages
        if whole:
            # a file with no text elsewhere: every engine reads every page
            table = set(want)
        if not whole:
            # In a file that has text elsewhere, only its scanned price
            # tables are transcribed: a signed form, a letter or a map page
            # adds nothing the parse can use (and a 1295 form's signer is no
            # business of this data); no table at all -> no sidecar.
            if not table:
                print("[%d/%d] no table %s  [%s: no page with %d+ amounts]" % (
                    n, len(todo), rel, " ".join("%d:%s" % kv for kv in sorted(reasons.items())),
                    TABLE_MONEY))
                log.append({"file": rel, "chosen": "none", "dpi": args.dpi,
                            "pages": " ".join("%d:%s" % kv for kv in sorted(reasons.items())),
                            "table_pages": "", "scores": "no price table on the target pages",
                            "replaced": "no"})
                skipped += 1
                continue
            want = set(table)
            reasons = {pg: why for pg, why in reasons.items() if pg in want}
        others = [e for e in engines if e != tri_eng]
        bodies = read_all(pdf, args.dpi, others, dead, table, rules, rots) if table else {}
        if tri:
            bodies[tri_eng] = tri
        cands = {}
        for e, b in bodies.items():
            if not isinstance(b, dict):
                continue
            texts = {pg: (b.get(pg) if pg in table else tri.get(pg, "")) for pg in want}
            if any(v is None for v in texts.values()):
                continue
            cands[e] = _sidecar_text(rel, args.dpi, ENGINE_NAMES[e], n_pages, texts, layer, reasons)
        if not args.redo:
            if old is not None:
                cands["existing"] = old
            wb = WIN_BACKUP / (rel + ".txt")
            if wb.exists():
                wtxt = wb.read_text(encoding="utf-8")
                if wtxt != old:
                    cands["windows_backup"] = wtxt
        if not cands:
            print("[%d/%d] FAILED   %s: no engine could read it: %s" % (
                n, len(todo), rel, {e: str(b)[:80] for e, b in bodies.items()}))
            failed += 1
            continue
        scored = {k: score(v) for k, v in cands.items()}
        order = ["existing", "windows_backup"] + engines        # tie-break: keep what is there
        best = max(cands, key=lambda k: (scored[k][0], -order.index(k)))
        pages_s = ("all" if reasons is None else
                   " ".join("%d:%s" % (pg, why) for pg, why in sorted(reasons.items())))
        if rots:
            pages_s += " | turned " + " ".join("%d:%d" % kv for kv in sorted(rots.items()))
        rec = {"file": rel, "chosen": best, "dpi": args.dpi, "pages": pages_s,
               "table_pages": " ".join(str(pg) for pg in sorted(table)),
               "scores": "; ".join(["%s %s" % (k, _fmt(scored[k][1])) for k in cands]
                                   + ["%s error" % e for e, b in bodies.items()
                                      if not isinstance(b, dict)])}
        if old is not None:
            rec["existing"] = _fmt(score(old)[1])
        if best == "existing":
            rec["replaced"] = "no"
            log.append(rec)
            print("[%d/%d] kept     %s  (%s)" % (n, len(todo), rel, rec["scores"]))
            kept += 1
            continue
        rec["replaced"] = "yes" if old is not None else "new"
        if best == "windows_backup":
            text_out = cands[best]                      # the PC's sidecar, as it was
        else:
            choice = ("### Engine choice: most usable cells (sidecar parser; a cell without a "
                      "unit counts half), then balanced rows, of: %s\n" % rec["scores"])
            b = bodies[best]
            texts = {pg: (b.get(pg) if pg in table else tri.get(pg, "")) for pg in want}
            text_out = _sidecar_text(rel, args.dpi, ENGINE_NAMES[best], n_pages, texts, layer,
                                     reasons, choice)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(text_out, encoding="utf-8")
        log.append(rec)
        print("[%d/%d] %-14s %s  %s  [%s]" % (n, len(todo), best, _fmt(scored[best][1]), rel,
                                              pages_s))
        done += 1

    if log:
        old_rows = []
        if CHOICE_CSV.exists():
            old_rows = [r for r in csv.DictReader(CHOICE_CSV.open(encoding="utf-8-sig"))
                        if r["file"] not in {x["file"] for x in log}]
        cols = ["file", "chosen", "dpi", "pages", "table_pages", "scores", "existing", "replaced"]
        with CHOICE_CSV.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in old_rows + log:
                w.writerow({c: r.get(c, "") for c in cols})

    print("\nOCR'd %d, kept existing %d, already cached %d, failed %d" % (done, kept, skipped, failed))
    print("sidecars -> %s   engine choices -> %s" % (OCR_DIR, CHOICE_CSV))
    if done:
        print("now re-run: python src/audit_external.py && "
              "python src/index_external.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
