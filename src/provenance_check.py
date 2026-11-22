"""
Guard the train/test boundary between company-provided and publicly-sourced data.

The split in force is STRUCTURAL, not a list of filenames:
    data/<file>            company-provided (no separator in the path) -> TEST
    data/<dir>/**          harvested from public sources               -> TRAIN

A hardcoded list is what let one company file slip
onto the training side once: the list had been derived from "company files that
produced line items", and that file has only site-level totals, so it fell out
of the definition. A path rule has no such hole, and it also covers public
directories that appear later -- `data/txdot/` showed up after this check was
first written, outside `data/external/`, and a check wired to `data/external/`
alone would silently never look at it.

The danger is not an ordinary duplicate. If one solicitation sits on both sides
-- the company's copy of a tabulation that is also published on the city's own
site -- then the model trains on the very bid it is later scored against, and
the test result is meaningless. That is train/test leakage, and it is invisible
to a byte-level duplicate check because the two copies are usually in different
formats (the company keeps .xlsx, the city publishes .pdf).

So this checks two ways:
  1. content hash, which catches an identical file filed on both sides;
  2. solicitation identity -- owner + bid number + project name + the line-item
     fingerprint -- which is what actually catches the cross-format case.

Usage:
    python src/provenance_check.py
    python src/provenance_check.py --json     # machine-readable
    python src/provenance_check.py --fitted-inputs-only [CLEAN_DIR]

A third check guards the INPUTS of V2+F1+F2's in-process fits (F1's size
cells and slope k, the comparables pool and beta/gamma; estimate_low_bid,
comparables.py), which read data/clean rather than the raw files:
  3. master_panel.csv: a row of a company file (a municipal bid-tab key whose
     file part has no separator; TxDOT keys are bid ids and have none either)
     or of a held-out file (data/test_holdout/public_holdout.csv)
     must not be labelled 'external' -- those fits admit exactly the
     'external' rows, so a mislabelled row would train them;
  4. txdot_project_totals.csv and txdot_archive_project_totals.csv carry no
     source_set column, so a label cannot keep anything out of the owner /
     work-type attributes the comparables read from them: they must hold no
     key that master_panel labels as test data, no company file and no
     held-out file.
This one reads data/clean only (never a company file) and writes nothing;
it runs after the raw-file checks, and alone with --fitted-inputs-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path

DATA = Path("data")
EXTERNAL = DATA / "external"
OUT = EXTERNAL / "_provenance_leakage.csv"

# Directories under data/ that hold neither side's bid tabulations: derived
# outputs, caches, labour-cost data, and raw archives.
# Kept deliberately short. Every exclusion is a place this check cannot look,
# and a filter added for speed is how the first version of this script managed
# to report zero leaks while one existed. Only derived outputs, caches and
# non-tabulation data are skipped; anything that might be a tabulation is
# scanned even when a match looks unlikely.
SKIP_DIRS = (
    "clean", ".cache",
    "external/dol",            # labour wage data, not bids
    "external/txdot_archive",  # zipped raw archives, scanned by their own tooling
)

STOP = {
    "bid", "bids", "tabulation", "tab", "tabs", "summary", "sheet", "copy", "of",
    "the", "and", "city", "project", "no", "final", "revised", "average", "line",
    "item", "items", "spreadsheet", "construction", "documents", "phase", "ph",
    "reconstruction", "improvements", "repair", "repairs", "erosion", "creek",
    "park", "street", "streets", "drive", "pdf", "xlsx", "with", "eopcc", "opcc",
}


def tokens(s: str) -> set[str]:
    """Distinctive words from a filename or title (bid numbers kept intact)."""
    s = re.sub(r"\.(pdf|xlsx?|xlsm)$", "", s, flags=re.I)
    raw = re.split(r"[^A-Za-z0-9\-]+", s)
    out = set()
    for w in raw:
        w = w.strip("-").lower()
        if not w or w in STOP or len(w) < 3:
            continue
        out.add(w)
    return out


def text_of(path: Path, pages: int = 2) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            r = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", str(pages),
                                str(path), "-"], capture_output=True, timeout=120)
            return r.stdout.decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(" ".join("" if c is None else str(c) for c in row))
                if i > 60:
                    break
            break
        wb.close()
        return "\n".join(rows)
    except Exception:
        return ""


def fingerprint(text: str, n: int = 14) -> list[str]:
    """First n line items as "<no>:<first words of description>".

    Only alphabetic words are kept. The same tabulation exported to .xlsx and
    published as .pdf lays its numeric columns out differently -- one run of
    text ends up as "1 Mobilization LS 1 10000 10000", the other as
    "1 Mobilization LS" -- so anything numeric in the key makes the two copies
    look like different documents, which is exactly how this check missed a
    real leak on its first attempt.
    """
    out = []
    for ln in text.splitlines():
        m = re.match(r"\s*(\d{1,3})[.)\s|]+(.{4,90})", ln)
        if not m:
            continue
        words = re.findall(r"[A-Za-z]{2,}", m.group(2))
        words = [w.lower() for w in words][:4]
        if len(words) >= 1:
            out.append(f"{int(m.group(1))}:{' '.join(words)}")
        if len(out) >= n:
            break
    return out


_QTY = re.compile(
    r"(?<![A-Za-z])(L\.?S\.?|S\.?Y\.?|C\.?Y\.?|L\.?F\.?|S\.?F\.?|V\.?F\.?|C\.?F\.?|EA\.?|EACH|"
    r"TONS?|ACRES?|AC|GAL|MG|MO|MONTHS?|DAYS?|HRS?|LBS?|UNITS?)(?![A-Za-z])\s+(\d[\d,]*(?:\.\d+)?)",
    re.I)


def item_quantities(text: str, n: int = 40) -> dict[int, float]:
    """{item number: bid quantity} read as "<unit> <quantity>" on the item's line.

    Used only to tell two lettings that share an item TEMPLATE apart from two
    copies of ONE letting.  A city's standard schedule (street and alley
    reconstructions that all start "Mobilization / ROW Preparation / Erosion
    Control / Traffic Control / Trench Safety ...") gives different lettings
    the same item names, but only copies of the same letting carry the same
    quantities.  Measured 2026-11-19: one company letting and a public letting
    of the same city, opened two years apart, matched on 71% of item names and
    0 of 6 quantities -- a template, not a twin.
    """
    out: dict[int, float] = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d{1,3})[.)\s|]+(.{4,})", ln)
        if not m:
            continue
        q = _QTY.search(m.group(2))
        if q:
            try:
                out.setdefault(int(m.group(1)), float(q.group(2).replace(",", "")))
            except ValueError:
                pass
        if len(out) >= n:
            break
    return out


def quantity_agreement(iq: dict, pq: dict, items) -> tuple[int, int]:
    """(agreeing, compared) over the matched item numbers where both sides read
    a quantity and at least one of them is more than 1 -- lump-sum items (1 LS
    on both sides) agree whatever the letting, so they prove nothing."""
    agree = compared = 0
    for k in items:
        a, b = iq.get(k), pq.get(k)
        if a is None or b is None or (a <= 1 and b <= 1):
            continue
        compared += 1
        if abs(a - b) <= 0.005 * max(a, b):
            agree += 1
    return agree, compared


def fitted_inputs(clean: Path | None = None) -> int:
    """Checks 3-4 (see the module docstring): the number of leaks found."""
    clean = clean or (DATA / "clean")
    csv.field_size_limit(10 ** 8)

    def file_of(k: str) -> str:
        return (k or "").replace("\\", "/").split("::")[0]

    reg = clean.parent / "test_holdout" / "public_holdout.csv"
    held = set()
    if reg.exists():
        with reg.open(encoding="utf-8-sig", newline="") as fh:
            held = {r["src_file"] for r in csv.DictReader(fh)}
    test_keys, bad_rows = set(), {}
    with (clean / "master_panel.csv").open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        h = next(rd)
        i_s, i_k, i_src = (h.index("source_set"), h.index("project_key"),
                           h.index("source"))
        for row in rd:
            k, ss = row[i_k], row[i_s]
            f = file_of(k)
            if ss != "external":
                test_keys.add(k)
            elif row[i_src] == "city_bidtab" and "/" not in f:
                bad_rows[k] = "company file labelled external"
            elif f in held:
                bad_rows[k] = "held-out file labelled external"
    n = 0
    print("V2+F1+F2 fitted inputs (data/clean: %s)" % clean)
    if bad_rows:
        n += len(bad_rows)
        print(f"!! LEAK : {len(bad_rows)} master_panel key(s) of test files "
              "labelled 'external' would train F1's size cells and the "
              "comparables pool, e.g. %s" % sorted(bad_rows.items())[:3])
    else:
        print("  master_panel.csv: no company or held-out file's row is "
              "labelled 'external'")
    for name, col in (("txdot_project_totals.csv", "project_id"),
                      ("txdot_archive_project_totals.csv", "src_file")):
        p = clean / name
        if not p.exists():
            print(f"  {name}: absent")
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            keys = {r.get(col) or "" for r in csv.DictReader(fh)}
        hit = sorted(k for k in keys if k in test_keys or file_of(k) in held
                     or (k and "/" not in file_of(k) and file_of(k).lower()
                         .endswith((".pdf", ".xlsx", ".xls"))))
        if hit:
            n += len(hit)
            print(f"!! LEAK : {name} ({col}, no source_set column) holds "
                  f"{len(hit)} test key(s), e.g. {hit[:3]}")
        else:
            print(f"  {name}: {len(keys)} keys, none of them test data "
                  "(it has no source_set column, so this is checked by key)")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fitted-inputs-only", nargs="?", const="", default=None,
                    metavar="CLEAN_DIR")
    args = ap.parse_args()
    if args.fitted_inputs_only is not None:
        d = Path(args.fitted_inputs_only) if args.fitted_inputs_only else None
        return 1 if fitted_inputs(d) else 0

    EXTS = {".pdf", ".xlsx", ".xls"}
    # Company side: files sitting directly in data/ (no directory component).
    internal = sorted(p for p in DATA.glob("*")
                      if p.is_file() and p.suffix.lower() in EXTS)
    # Public side: every bid tab in ANY subdirectory of data/, so a new public
    # folder is covered without editing this script.
    public = []
    for p in DATA.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        rel = p.relative_to(DATA).as_posix()
        if "/" not in rel:                       # that is the company side
            continue
        if any(rel == s or rel.startswith(s + "/") for s in SKIP_DIRS):
            continue
        public.append(p)

    roots = sorted({p.relative_to(DATA).as_posix().split("/")[0] for p in public})
    print(f"public directories covered: {', '.join(roots)}")

    # Say which public directories contributed NOTHING, and why. The line above
    # lists only directories that produced files, so a directory holding none
    # of EXTS simply never appears -- it reads as a complete coverage report
    # while silently omitting a whole tree. `data/oews/` (BLS wage data, all
    # .csv) was invisible this way. Nothing there can leak today, because the
    # company side is entirely .pdf/.xlsx, but "not scanned" must not look the
    # same as "scanned and clean".
    skipped = []
    for d in sorted(p for p in DATA.glob("*") if p.is_dir()):
        name = d.name
        if name in roots or any(name == s or s.startswith(name + "/")
                                for s in SKIP_DIRS):
            continue
        others = sorted({q.suffix.lower() for q in d.rglob("*")
                         if q.is_file() and q.suffix})
        skipped.append((name, others))
    if skipped:
        print("public directories NOT scanned (no .pdf/.xlsx/.xls in them):")
        for name, exts in skipped:
            print(f"  {name}/  holds {', '.join(exts) or 'nothing'}")
        print("  -> only a concern if company-side files ever arrive in these "
              "formats; the TEST side is .pdf/.xlsx today.")

    print(f"company-provided (TEST side): {len(internal)} files")
    print(f"public  (TRAIN side)        : {len(public)} files\n")

    # ---- 1. identical content on both sides
    ihash = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in internal}
    phash: dict[str, list[Path]] = {}
    for p in public:
        phash.setdefault(hashlib.md5(p.read_bytes()).hexdigest(), []).append(p)

    findings = []
    for ip, h in ihash.items():
        for pp in phash.get(h, []):
            findings.append({"severity": "LEAK", "basis": "identical file content",
                             "internal": ip.name,
                             "public": pp.relative_to(DATA).as_posix(),
                             "detail": f"md5 {h[:12]}"})

    # ---- 2. same solicitation in different formats
    #
    # This must NOT be gated on a shared filename token. The leakage that
    # actually exists looks like this:
    #     company : a file named by the firm's project number
    #     city    : a file named by the city's bid number
    # -- no word in common, same tabulation. The identity lives in the content
    # (the project title, the owner's name), so every public file
    # is fingerprinted once and then compared against all of the company files.
    print("fingerprinting the public side (one pass)...", flush=True)
    pub_fp: list[tuple[Path, set[str], str, dict]] = []
    for i, pp in enumerate(public, 1):
        t = text_of(pp)
        pub_fp.append((pp, set(fingerprint(t)), t[:1500], item_quantities(t)))
        if i % 250 == 0:
            print(f"  {i}/{len(public)}", flush=True)

    for ip in internal:
        itext = text_of(ip)
        ifp = set(fingerprint(itext))
        ihead = itext[:1500]
        iqty = item_quantities(itext)
        if not ifp:
            print(f"  ! no line-item fingerprint extracted from {ip.name}")
            continue
        for pp, pfp, phead, pqty in pub_fp:
            if not pfp:
                continue
            overlap = len(ifp & pfp) / min(len(ifp), len(pfp))
            if overlap < 0.3:
                continue
            # Corroborate with a distinctive phrase shared by the two headers,
            # so a generic item list (mobilization, traffic control, ...) alone
            # cannot raise a leak.
            shared_head = tokens(ihead) & tokens(phead)
            shared_head = {w for w in shared_head if len(w) > 4}
            matched = {int(k.split(":", 1)[0]) for k in ifp & pfp}
            q_ok, q_n = quantity_agreement(iqty, pqty, matched)
            qnote = f"; quantities agree on {q_ok}/{q_n} matched non-lump-sum items"
            if overlap >= 0.6 and shared_head and q_n >= 4 and q_ok / q_n < 0.5:
                # Same item names, different quantities: one item TEMPLATE used
                # for two lettings.  Fails SAFE: with fewer than 4 comparable
                # quantities (e.g. an export that drops them) it stays a LEAK.
                sev, basis = "REVIEW", (f"same item template ({overlap:.0%} of names) "
                                        f"but quantities differ ({q_ok}/{q_n} agree)")
            elif overlap >= 0.6 and shared_head:
                sev, basis = "LEAK", f"same line items ({overlap:.0%}) + shared header terms"
            elif overlap >= 0.6:
                sev, basis = "REVIEW", f"same line items ({overlap:.0%}) but no shared header term"
            else:
                sev, basis = "REVIEW", f"partial line-item match ({overlap:.0%})"
            findings.append({"severity": sev, "basis": basis,
                             "internal": ip.name,
                             "public": pp.relative_to(DATA).as_posix(),
                             "detail": "shared header terms: "
                                       + (",".join(sorted(shared_head)[:8]) or "none")
                                       + qnote})

    # A match is a TRAINING leak only where a parser reads the public copy.
    # data/raw_downloads/ is read by nothing: company_duplicates/ there is the
    # quarantine for public copies of company lettings (company_twin_scan.py),
    # so finding a twin there is the guard having WORKED, not a leak.  Before
    # this, a quarantined public copy was reported as "LEAK: 1"
    # after the 2026-11-19 rebuild while the exit code stayed 0 either way.
    for f in findings:
        if f["public"].startswith("raw_downloads/"):
            f["severity"] = ("QUARANTINED" if "/company_duplicates/" in f["public"]
                             else "NOT_PARSED")

    # de-duplicate, keeping the strongest finding per pair
    best: dict[tuple, dict] = {}
    rank = {"LEAK": 0, "REVIEW": 1, "QUARANTINED": 2, "NOT_PARSED": 3}
    for f in findings:
        k = (f["internal"], f["public"])
        if k not in best or rank[f["severity"]] < rank[best[k]["severity"]]:
            best[k] = f
    rows = sorted(best.values(), key=lambda r: (rank[r["severity"]], r["internal"]))

    if rows:
        with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["severity", "basis", "internal",
                                               "public", "detail"])
            w.writeheader()
            w.writerows(rows)

    leaks = [r for r in rows if r["severity"] == "LEAK"]
    print(f"{'!! ' if leaks else ''}LEAK   : {len(leaks)}  "
          "(same solicitation on both sides -- must be resolved)")
    print(f"REVIEW : {sum(1 for r in rows if r['severity'] == 'REVIEW')}  "
          "(worth a look, probably different solicitations)")
    print(f"QUARANTINED: {sum(1 for r in rows if r['severity'] == 'QUARANTINED')}  "
          "(public copy held in raw_downloads/company_duplicates/ -- read by no parser)")
    for r in rows:
        print(f"\n  [{r['severity']}] {r['basis']}")
        print(f"     TEST  data/{r['internal']}")
        print(f"     {'TRAIN' if r['severity'] in ('LEAK', 'REVIEW') else 'HELD '} data/{r['public']}")
    if rows:
        print(f"\n-> {OUT}")
    if args.json:
        print(json.dumps(rows, indent=2))
    print()
    n_fit = fitted_inputs()
    # Exit non-zero on a real leak, so `rebuild_all.sh` and the README's
    # `if ($?)` chain stop instead of carrying on past it.
    return 1 if (leaks or n_fit) else 0


if __name__ == "__main__":
    raise SystemExit(main())
