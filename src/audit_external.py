"""
Audit downloaded external bid-tabulation files and classify each one by how much
line-item information it actually contains.

Priority (per project requirement): LINE-ITEM unit-price tables >> project totals.
Project-total-only documents are kept but flagged `summary_only` so they can be
excluded from unit-price modelling.

Usage:
    python src/audit_external.py                 # audit data/external, write index
    python src/audit_external.py --root <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict, fields as dataclass_fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lock import PipelineBusy, pipeline_lock  # noqa: E402

# ---------------------------------------------------------------- text loading

UNIT_TOKENS = {
    "LS", "LF", "SY", "SF", "CY", "EA", "TON", "TN", "AC", "GAL", "MO",
    "HR", "DAY", "VF", "ALLOW", "HD", "MGAL", "CF", "MI",
}

UNIT_PRICE_HDR = re.compile(r"unit\s*(price|cost|bid)", re.I)
TOTAL_HDR = re.compile(r"(extended|total)\s*(price|cost|amount)", re.I)
ITEM_HDR = re.compile(r"\b(item\s*(no\.?|#|number)?|bid\s*item|line\s*item)\b", re.I)
# "QUANTITIES" (plural) is a real header spelling; r"quantity" misses it.
QTY_HDR = re.compile(
    r"\b(qty|quantit(?:y|ies)|est\.?\s*quantit(?:y|ies)|"
    r"bid\s*quantit(?:y|ies))\b", re.I)
MONEY = re.compile(r"\$\s*[\d,]+(?:\.\d{1,2})?|\b\d{1,3}(?:,\d{3})+\.\d{2}\b")
# a plausible line-item row: leading item number, description text, a unit token.
# Longest unit tokens first so e.g. TON wins over TN; re.M because these are per-line.
_UNITS_ALT = "|".join(sorted(UNIT_TOKENS, key=len, reverse=True))
# "<item no> <description> [<quantity>] <UNIT>" -- the quantity column sits
# between description and unit in some agencies' layouts, so allow it.
ITEM_ROW = re.compile(
    r"^[ \t]*(\d{1,4})(?:\.\d+)?(?:[.)\-]|[ \t])[ \t]*"
    r"([A-Za-z\"'(][^\n]{3,}?)"
    r"(?:[ \t]+[\d,]+(?:\.\d+)?)?"
    r"[ \t]+(" + _UNITS_ALT + r")\b",
    re.I | re.M,
)
# A priced row: a unit token on the line plus two or more money-like figures.
# The "$" is optional -- several agencies print bare numbers in money columns.
_MONEYISH = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}"
PRICED_ROW = re.compile(
    r"^[^\n]*?\b(" + _UNITS_ALT + r")\b[^\n]*?"
    r"(?:\$\s*)?(?:" + _MONEYISH + r")[^\n]*?"
    r"(?:\$\s*)?(?:" + _MONEYISH + r")",
    re.I | re.M,
)


OCR_ROOT = Path("data/external/_ocr")


def ocr_sidecar(path: Path) -> Path | None:
    """Where `src/ocr_scans.py` cached the OCR text for this PDF, if anywhere.

    Resolved against the corpus root rather than assembled from string
    fragments: the audit hands this function an absolute path, and splitting
    one on the literal "external" would also match a file whose own name
    contains it.
    """
    root = Path("data/external").resolve()
    try:
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return root / "_ocr" / (rel.as_posix() + ".txt")


def read_pdf_text(path: Path) -> str:
    """Extract layout-preserving text from a PDF.

    Three extractors are tried in order, and the order is deliberate:

    1. `pdftotext -layout`, first because it is a separate process -- over a
       corpus of this size an in-process pdfplumber loop accumulates page
       caches until the interpreter is killed, which silently truncated
       earlier passes;
    2. pdfplumber;
    3. PyMuPDF, re-laid-out from word boxes.

    The third exists because 14 files in this corpus return EMPTY from both of
    the first two and read perfectly in PyMuPDF (MuPDF repairs structures the
    other two give up on). With only two extractors those 14 were classified
    `unreadable`, which also left work_type as "other" -- one of them is a
    streambank-stabilization tabulation, exactly the work type being modelled.
    An empty extraction is indistinguishable from an empty document, so this
    kind of failure is silent; hence a third attempt rather than a warning.

    Ahead of all three sits the OCR cache (a sidecar that OCR'd only some
    pages is merged page by page with the text layer: merge_partial_sidecar).
    18 files have no text layer at all
    (they are photographs of printed tabulations); `src/ocr_scans.py` reads
    those with the OCR engine built into Windows and caches the result as a
    sidecar, which is preferred here because re-running OCR on every audit pass
    would be slow and would not give the same answer twice. The sidecar keeps
    its MACHINE-READ header deliberately: that header travels into the audit
    text, so a row sourced from OCR is identifiable downstream and its digits
    can be re-checked against the PDF before anyone bids off them.
    """
    sidecar = ocr_sidecar(path)
    if sidecar is not None and sidecar.exists():
        try:
            text = sidecar.read_text(encoding="utf-8")
            if text.strip():
                if _PARTIAL_PAGE.search(text):
                    return merge_partial_sidecar(path, text)
                return text
        except Exception:
            pass
    return read_pdf_text_layer(path)


# A sidecar that OCR'd only some pages of a PDF (ocr_scans.py reads the image
# pages of a file whose other pages have text) marks each page "(OCR)" or
# "(text layer)"; see merge_partial_sidecar.
_PARTIAL_PAGE = re.compile(r"^###\s*PAGE\s+\d+\s*\(text layer\)\s*$", re.M)
_SIDECAR_PAGE = re.compile(r"^###\s*PAGE\s+(\d+)(?:\s*\(([^)]*)\))?\s*$")


def merge_partial_sidecar(path: Path, text: str) -> str:
    """The document as the audit should see it when only some of its pages
    were OCR'd: the sidecar's header and OCR'd pages, and every other page
    read the way this audit reads any PDF (text_layer_pages: the same three
    extractors, page by page; the sidecar's own copy of that page only when
    all three give nothing).
    So a text page reads exactly as it did before the file had a sidecar,
    and a scanned page is no longer missing."""
    head, pages, cur = [], {}, None
    for ln in text.splitlines():
        m = _SIDECAR_PAGE.match(ln)
        if m:
            cur = int(m.group(1))
            pages[cur] = [(m.group(2) or "OCR").strip(), []]
            continue
        if cur is None:
            head.append(ln)
        else:
            pages[cur][1].append(ln)
    layer = text_layer_pages(path)
    blocks = ["\n".join(head).rstrip()]
    for n in sorted(pages):
        src, lines = pages[n]
        body = "\n".join(lines).strip("\n")
        if src == "text layer" and n - 1 < len(layer) and layer[n - 1].strip():
            body = layer[n - 1].rstrip()
        blocks.append("### PAGE %d (%s)\n%s" % (n, src, body))
    return "\n\n".join(blocks)


def text_layer_pages(path: Path) -> list:
    """The PDF's text layer page by page, with read_pdf_text_layer's order of
    extractors: pdftotext -layout (pages split at its form feeds), then
    pdfplumber, then PyMuPDF re-laid out.  [] when none reads anything."""
    try:
        import subprocess

        out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                             capture_output=True, timeout=180)
        pages = out.stdout.decode("utf-8", "replace").split("\f")
        if any(p.strip() for p in pages):
            return pages
    except Exception:
        pass
    try:
        import pdfplumber

        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text(layout=True) or "")
                page.flush_cache()
        if any(p.strip() for p in pages):
            return pages
    except Exception:
        pass
    try:
        return read_pdf_text_mupdf(path, by_page=True)
    except Exception:
        return []


def read_pdf_text_layer(path: Path) -> str:
    """The PDF's own text: pdftotext, then pdfplumber, then PyMuPDF."""
    try:
        import subprocess

        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, timeout=180,
        )
        text = out.stdout.decode("utf-8", "replace")
        if text.strip():
            return text
    except Exception:
        pass
    try:
        import pdfplumber

        chunks = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text(layout=True) or "")
                page.flush_cache()          # keep per-page caches from piling up
        text = "\n".join(chunks)
        if text.strip():
            return text
    except Exception:
        pass
    try:
        return read_pdf_text_mupdf(path)
    except Exception:
        return ""


def read_pdf_text_mupdf(path: Path, by_page: bool = False):
    """PyMuPDF fallback, re-laid out from word boxes.

    PyMuPDF's own get_text() emits one block per text line and loses column
    alignment, but the line-item patterns need description, unit and price on
    one line, so words are regrouped into visual rows by y and padded out by
    x -- pdftotext -layout, reconstructed.  by_page: a list of page texts
    instead of one string.
    """
    import fitz

    doc = fitz.open(str(path))
    try:
        pages = []
        for page in doc:
            out = []
            words = page.get_text("words")
            words.sort(key=lambda w: (w[1], w[0]))
            rows, cur, cur_y = [], [], None
            for w in words:
                y0 = w[1]
                if cur_y is None or abs(y0 - cur_y) <= 3.0:
                    cur.append(w)
                    cur_y = y0 if cur_y is None else (cur_y + y0) / 2
                else:
                    rows.append(cur)
                    cur, cur_y = [w], y0
            if cur:
                rows.append(cur)
            for r in rows:
                r.sort(key=lambda w: w[0])
                line = ""
                for x0, _y0, _x1, _y1, txt, *_ in r:
                    col = int(x0 / 5.0)      # ~5pt per char at a 10pt font
                    if col < len(line):
                        col = len(line) + 1
                    line += " " * (col - len(line)) + txt
                out.append(line.rstrip())
            pages.append("\n".join(out))
        if by_page:
            return pages
        return "\n".join(p for p in pages if p)
    finally:
        doc.close()


MAX_XLSX_BYTES = 40 * 1024 * 1024  # openpyxl loads these fully; skip monsters


def read_xlsx_text(path: Path) -> str:
    if path.stat().st_size > MAX_XLSX_BYTES:
        return ""
    try:
        import openpyxl

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            rows.append(f"### SHEET {ws.title}")
            for r in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in r]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
        wb.close()
        return "\n".join(rows)
    except Exception:
        return ""


def load_text(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".pdf":
        return read_pdf_text(path)
    if suf in (".xlsx", ".xlsm", ".xls"):
        return read_xlsx_text(path)
    if suf in (".csv", ".tsv"):
        # Only the header plus a row count is needed; these files can be huge.
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as fh:
                header = fh.readline()
                n = sum(1 for _ in fh)
            return header + "\n" * n
        except Exception:
            return ""
    if suf in (".txt", ".htm", ".html"):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
    return ""


# ------------------------------------------------------------------ scoring

@dataclass
class Audit:
    source: str            # folder name = owner/agency
    file: str
    ext: str
    size_kb: int
    n_unit_price_hdr: int  # "UNIT PRICE" / "UNIT COST" header hits
    n_item_rows: int       # rows that look like "<n> <description> <UNIT>"
    n_priced_rows: int     # rows carrying a unit token and >=2 dollar figures
    n_money: int           # dollar figures found
    n_units: int           # distinct unit tokens seen
    has_qty_col: bool
    has_item_col: bool
    classification: str    # line_item | likely_line_item | summary_only | unreadable
    note: str

    USABLE = ("line_item", "likely_line_item", "unit_price_table", "unit_price_dataset")

    @property
    def usable(self) -> bool:
        return self.classification in self.USABLE


# Tabular unit-price *datasets* (CSV/API extracts) rather than per-project tabs.
CSV_UNIT_PRICE_COLS = re.compile(
    r"bid_item_unit_price_amount|unit_price_amount|unit[_ ]price", re.I
)
# A city-maintained average/reference unit-price table (one row per bid item,
# aggregated across projects) rather than a single project's tabulation.
REF_TABLE = re.compile(
    r"average\s+(bid\s+)?(price|cost)|average\s+cost\s+per\s+quantity|"
    r"average\s+unit\s+(bid\s+)?price",
    re.I,
)


def classify(text: str, ext: str = "") -> tuple[str, str]:
    if not text.strip():
        return "unreadable", "no extractable text (likely a scanned image; needs OCR)"

    # ---- dataset-shaped sources: only real delimited files, judged on their
    # header row alone (a PDF full of commas must not land here).
    if ext in ("csv", "tsv", "txt"):
        header = text.splitlines()[0] if text.splitlines() else ""
        if CSV_UNIT_PRICE_COLS.search(header) and header.count(",") >= 5:
            n_rows = text.count("\n")
            return "unit_price_dataset", (
                f"delimited dataset, ~{n_rows:,} rows, header carries a "
                "unit-price column -- one row per bidder per bid item"
            )

    # ---- city-maintained average/reference unit-price tables
    if REF_TABLE.search(text) and len(UNIT_PRICE_HDR.findall(text)) >= 3:
        return "unit_price_table", (
            "reference table of average unit prices aggregated across projects "
            "(not a single project's tabulation)"
        )

    n_up = len(UNIT_PRICE_HDR.findall(text))
    n_rows = len(ITEM_ROW.findall(text))
    n_priced = len(PRICED_ROW.findall(text))
    n_money = len(MONEY.findall(text))
    units_seen = {
        m.group(0).upper()
        for m in re.finditer(r"\b(" + "|".join(UNIT_TOKENS) + r")\b", text)
    }
    has_qty = bool(QTY_HDR.search(text))
    has_item = bool(ITEM_HDR.search(text))

    # Strongest signal: an explicit UNIT PRICE column plus many item-looking rows.
    if n_up >= 1 and (n_rows >= 8 or n_priced >= 8):
        return "line_item", (
            f"unit-price header x{n_up}, {n_rows} item rows, {n_priced} priced rows"
        )
    # No explicit header but clearly a priced schedule of items.
    if (n_rows >= 12 or n_priced >= 12) and n_money >= 20 and len(units_seen) >= 3:
        return "line_item", (
            f"{n_rows} item rows / {n_priced} priced rows, {n_money} money figures, "
            f"units={sorted(units_seen)[:6]}"
        )
    if n_up >= 1 and (n_rows >= 3 or n_priced >= 3):
        return "likely_line_item", (
            f"unit-price header x{n_up}, {n_rows} item rows, {n_priced} priced rows"
        )
    if n_rows >= 5 or n_priced >= 5:
        return "likely_line_item", (
            f"no unit-price header, but {n_rows} item rows / {n_priced} priced rows"
        )
    # Totals-only tabulation: money present, but no per-item schedule.
    if n_money >= 2:
        return "summary_only", (
            f"{n_money} money figures but only {n_rows} item rows / {n_priced} priced rows "
            "-> project/bidder totals, not a unit-price schedule"
        )
    return "summary_only", "no priced item schedule detected"


def audit_file(path: Path, root: Path) -> Audit:
    text = load_text(path)
    ext = path.suffix.lower().lstrip(".")
    cls, note = classify(text, ext)
    units_seen = {
        m.group(0).upper()
        for m in re.finditer(r"\b(" + "|".join(UNIT_TOKENS) + r")\b", text)
    }
    rel = path.relative_to(root)
    return Audit(
        source=rel.parts[0] if len(rel.parts) > 1 else "(root)",
        file=str(rel).replace("\\", "/"),
        ext=path.suffix.lower().lstrip("."),
        size_kb=round(path.stat().st_size / 1024),
        n_unit_price_hdr=len(UNIT_PRICE_HDR.findall(text)),
        n_item_rows=len(ITEM_ROW.findall(text)),
        n_priced_rows=len(PRICED_ROW.findall(text)),
        n_money=len(MONEY.findall(text)),
        n_units=len(units_seen),
        has_qty_col=bool(QTY_HDR.search(text)),
        has_item_col=bool(ITEM_HDR.search(text)),
        classification=cls,
        note=note,
    )


def _selftest() -> bool:
    """merge_partial_sidecar on a 2-page PDF made here: the text page is
    re-read from the PDF, the OCR'd page comes from the sidecar; a sidecar
    that OCR'd the whole file is returned as it is."""
    import tempfile

    import fitz

    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "t.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "TEXT LAYER PAGE ONE  1 Sod 100 SY", fontsize=11)
        doc.new_page()
        doc.save(str(pdf))
        doc.close()
        sc = ("### MACHINE-READ (OCR) -- not a text layer from the PDF.\n"
              "### OCR pages: 2 (image); every other page is the PDF's own text layer.\n\n"
              "### PAGE 1 (text layer)\nstale copy\n\n### PAGE 2 (OCR)\nOCR PAGE TWO\n### RULE")
        out = merge_partial_sidecar(pdf, sc)
        ok = ("TEXT LAYER PAGE ONE" in out and "stale copy" not in out and "OCR PAGE TWO" in out
              and out.index("TEXT LAYER") < out.index("OCR PAGE TWO")
              and _PARTIAL_PAGE.search(sc) is not None
              and _PARTIAL_PAGE.search("### PAGE 1\nA\n### PAGE 2\nB") is None)
    print("audit_external selftest:", "OK" if ok else "FAILED\n" + out)
    return ok


def main() -> int:
    if sys.argv[1:2] == ["--selftest"]:
        return 0 if _selftest() else 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/external")
    ap.add_argument("--out", default=None, help="CSV index path (default <root>/_audit.csv)")
    ap.add_argument(
        "--exclude",
        default="dol,txdot_archive/zips,txdot_archive/probe,_pre2020,_ocr",
        help="comma-separated path prefixes (relative to root) to skip; these hold "
             "labour-cost data, raw archives, quarantined pre-cutoff bids and "
             "cached OCR text -- not bid tabulations in their own right",
    )
    ap.add_argument("--fresh", action="store_true",
                    help="re-audit everything instead of resuming from _audit.csv")
    ap.add_argument("--force", action="store_true",
                    help="break a pipeline lock left by a dead run")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 1

    exts = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".htm", ".html"}
    skips = [s.strip().replace("\\", "/") for s in args.exclude.split(",") if s.strip()]
    # This tool's own outputs are not source data; auditing them would make the
    # corpus self-referential (INDEX.csv looks like a priced table of its own).
    OWN_OUTPUTS = {"INDEX.csv", "INDEX.md", "README.md"}

    def excluded(p: Path) -> bool:
        rel = str(p.relative_to(root)).replace("\\", "/")
        if rel in OWN_OUTPUTS:
            return True
        return any(rel == s or rel.startswith(s + "/") for s in skips)

    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
        and not p.name.startswith("_") and not excluded(p)
    )
    if skips:
        print(f"excluding: {', '.join(skips)}")

    out = Path(args.out) if args.out else root / "_audit.csv"
    fields = [f.name for f in dataclass_fields(Audit)]

    # Resume: keep rows already audited, so a long run can be restarted safely.
    done: dict[str, dict] = {}
    if out.exists() and not args.fresh:
        for r in csv.DictReader(out.open(encoding="utf-8-sig")):
            if r.get("file"):
                done[r["file"]] = r
        print(f"resuming: {len(done)} files already audited "
              f"(use --fresh to redo everything)")

    # Keyed by file, seeded with whatever a previous pass already established.
    # An incremental flush must write THIS WHOLE MAP, not just the files reached
    # so far: writing only the processed prefix would drop the resumed rows for
    # files later in the sort order and shrink the CSV on every crash.
    acc: dict[str, dict] = dict(done)
    order: list[str] = [str(p.relative_to(root)).replace("\\", "/") for p in files]

    def flush() -> None:
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            # Known files first, in corpus order; then anything left over from an
            # older pass whose file has since moved (e.g. into _pre2020/).
            seen = set()
            for rel in order:
                if rel in acc:
                    w.writerow(acc[rel])
                    seen.add(rel)
            for rel, row in acc.items():
                if rel not in seen:
                    w.writerow(row)

    todo = 0
    for i, p in enumerate(files, 1):
        rel = order[i - 1]
        if rel in acc:
            continue
        a = asdict(audit_file(p, root))
        acc[rel] = a
        todo += 1
        print(f"[{i}/{len(files)}] {a['classification']:17s} {a['file']}", flush=True)
        if todo % 20 == 0:
            flush()

    flush()

    # Summarise only the files that are actually in the corpus right now.
    results = [acc[rel] for rel in order if rel in acc]

    counts: dict[str, int] = {}
    by_source: dict[str, dict[str, int]] = {}
    for a in results:
        cls, src = a["classification"], a["source"]
        counts[cls] = counts.get(cls, 0) + 1
        by_source.setdefault(src, {}).setdefault(cls, 0)
        by_source[src][cls] += 1

    summary = {"root": str(root), "n_files": len(results),
               "by_classification": counts, "by_source": by_source}
    (root / "_audit_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n=== by classification ===")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:17s} {v}")
    print("\n=== by source ===")
    for src, d in sorted(by_source.items()):
        li = sum(d.get(k, 0) for k in Audit.USABLE)
        print(f"  {src:18s} usable(unit-price)={li:3d}  "
              f"summary_only={d.get('summary_only', 0):3d}  "
              f"unreadable={d.get('unreadable', 0):3d}")
    print(f"\nindex -> {out}")
    return 0


if __name__ == "__main__":
    # Single-writer: this pipeline shares its files with another session.
    try:
        with pipeline_lock("audit", force="--force" in sys.argv):
            raise SystemExit(main())
    except PipelineBusy as exc:
        print(exc)
        raise SystemExit(2)
