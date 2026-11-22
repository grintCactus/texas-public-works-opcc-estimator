"""Scan a tree that is meant to go public for company / personal traces.

    .venv/bin/python deploy/tools/scan_public.py <dir> [<dir> ...] [--json OUT]
        [--detail]          # per hit: file, category, line number, token kind
                            # (never the matched text)

What counts as a hit (case-insensitive):
  employer   the patterns under "employer" in .git/scan_private.json
  company    every pattern in .git/company_patterns.txt (read as regexes, the
             way the pre-commit guard reads them; never printed)
  users_path a home-directory path (under the macOS or Linux home root)
  personal   the patterns under "personal" in .git/scan_private.json
  email      any e-mail address (reported separately, not a gate by itself)

The pattern files are private and live in .git/, so they are never committed.
A checkout without them (a fresh clone) scans for paths and e-mail addresses
only: it holds no company file to leak, and nothing in this repository names
one.

Every file is scanned, hidden files included, and its relative path too.
Parquet files are decoded (column names, schema metadata and every value
rendered as text), because compressed pages hide strings from a byte scan.
.xlsx / .zip / .gz are opened and their members scanned. Output is counts
only: no matched text, no row content.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATTERNS_FILE = os.path.join(REPO, ".git", "company_patterns.txt")
PRIVATE_FILE = os.path.join(REPO, ".git", "scan_private.json")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def company_patterns():
    """The company patterns, [] in a checkout that has none."""
    try:
        with open(PATTERNS_FILE, encoding="utf-8") as fh:
            pats = [l.strip() for l in fh if l.strip()]
    except FileNotFoundError:
        return []
    return [re.compile(p, re.I) for p in pats]


def _private(key):
    try:
        with open(PRIVATE_FILE, encoding="utf-8") as fh:
            pats = json.load(fh).get(key) or []
    except FileNotFoundError:
        return []
    return [re.compile(p, re.I) for p in pats]


EMPLOYER = _private("employer")
COMPANY = company_patterns()
PERSONAL = _private("personal")
# a home directory; "/home/" inside a web address (site.org/home/page) is not one
FIXED = {"users_path": re.compile("/" + r"Users/|(?<![A-Za-z0-9._-])/" + r"home/[a-z_][a-z0-9_-]*/")}
GATES = ("employer", "company", "users_path", "personal")
#: every gate pattern in one alternation: one pass says whether a text has
#: any gate hit at all (the per-pattern passes run only when it does)
ANY = re.compile("|".join("(?:%s)" % rx.pattern for rx in
                          EMPLOYER + COMPANY + PERSONAL + list(FIXED.values())), re.I)


def find(text):
    """[(category, start, end)] for every hit in `text`."""
    out = []
    if not ANY.search(text):
        return [("email", m.start(), m.end()) for m in EMAIL.finditer(text)]
    for rx in EMPLOYER:
        out += [("employer", m.start(), m.end()) for m in rx.finditer(text)]
    for rx in COMPANY:
        out += [("company", m.start(), m.end()) for m in rx.finditer(text)]
    for rx in PERSONAL:
        out += [("personal", m.start(), m.end()) for m in rx.finditer(text)]
    for cat, rx in FIXED.items():
        out += [(cat, m.start(), m.end()) for m in rx.finditer(text)]
    out += [("email", m.start(), m.end()) for m in EMAIL.finditer(text)]
    return out


def _parquet_texts(path):
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    md = pf.schema_arrow.metadata or {}
    yield "schema", " ".join(pf.schema_arrow.names)
    for k, v in md.items():
        yield "metadata", k.decode("utf-8", "replace") + " " + \
            v.decode("utf-8", "replace")
    t = pf.read()
    for name in t.column_names:
        col = t.column(name).to_pandas()
        vals = col.dropna().astype(str).unique()
        yield "column:" + name, "\n".join(vals)


def _texts(path, data=None, name=None):
    """(part, text) pairs for one file (or archive member)."""
    name = name or path
    low = name.lower()
    if low.endswith(".parquet") and data is None:
        yield from _parquet_texts(path)
        return
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    if low.endswith((".xlsx", ".xlsm", ".docx", ".zip")):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.namelist():
                    yield from _texts(None, z.read(m), name + "!" + m)
            return
        except zipfile.BadZipFile:
            pass
    if low.endswith(".gz"):
        try:
            yield from _texts(None, gzip.decompress(data), name[:-3])
            return
        except OSError:
            pass
    yield "bytes", data.decode("utf-8", "replace")
    # UTF-16 text would hide from the decode above
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        yield "utf16", data.decode("utf-16", "replace")


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _token_kinds(path, hits):
    """For a .py file: the token type (COMMENT / STRING / NAME / ...) of each
    hit, so a scrub can tell comments from code."""
    import tokenize
    with open(path, "rb") as fh:
        toks = list(tokenize.tokenize(fh.readline))
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    starts = [0]
    for ln in io.StringIO(text, newline="").readlines():
        starts.append(starts[-1] + len(ln))

    def off(rc):
        r, c = rc
        return starts[r - 1] + c
    spans = [(off(t.start), off(t.end), tokenize.tok_name[t.type])
             for t in toks if t.type not in (tokenize.ENCODING,)]
    out = []
    for cat, s, e in hits:
        kind = "?"
        for a, b, k in spans:
            if a <= s < b:
                kind = k
                break
        out.append(kind)
    return out


def scan(roots, detail=False):
    report = {"files": 0, "bytes": 0, "hits": {}, "files_with_hits": {},
              "detail": []}
    for root in roots:
        root = os.path.abspath(root)
        walk = (os.walk(root) if os.path.isdir(root) else
                [(os.path.dirname(root), [], [os.path.basename(root)])])
        if not os.path.isdir(root):
            root = os.path.dirname(root)
        for dp, dns, fns in walk:
            for fn in fns:
                p = os.path.join(dp, fn)
                if os.path.islink(p):
                    rel = os.path.relpath(p, root)
                    report["detail"].append({"file": rel, "category": "symlink"})
                    continue
                rel = os.path.relpath(p, os.path.dirname(root))
                report["files"] += 1
                report["bytes"] += os.path.getsize(p)
                file_hits = []
                for cat, s, e in find(rel):
                    file_hits.append((cat, "path", None, None))
                try:
                    for part, text in _texts(p):
                        hs = find(text)
                        kinds = (_token_kinds(p, hs) if detail and hs and
                                 p.endswith(".py") and part == "bytes"
                                 else [None] * len(hs))
                        for (cat, s, e), k in zip(hs, kinds):
                            file_hits.append((cat, part,
                                              _line_of(text, s)
                                              if part in ("bytes", "utf16")
                                              else None, k))
                except Exception as ex:  # noqa: BLE001 - unreadable is a finding
                    file_hits.append(("unreadable", type(ex).__name__, None, None))
                for cat, part, line, kind in file_hits:
                    report["hits"][cat] = report["hits"].get(cat, 0) + 1
                    report["files_with_hits"].setdefault(cat, {})
                    report["files_with_hits"][cat][rel] = \
                        report["files_with_hits"][cat].get(rel, 0) + 1
                    if detail:
                        report["detail"].append({"file": rel, "category": cat,
                                                 "part": part, "line": line,
                                                 "token": kind})
    report["gate_hits"] = sum(report["hits"].get(c, 0) for c in GATES) + \
        report["hits"].get("unreadable", 0) + \
        sum(1 for d in report["detail"] if d.get("category") == "symlink")
    report["clean"] = report["gate_hits"] == 0
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args(argv)
    r = scan(a.roots, a.detail)
    print("files scanned : %d (%.1f MB)" % (r["files"], r["bytes"] / 1e6))
    for cat in GATES + ("email", "unreadable"):
        fw = r["files_with_hits"].get(cat, {})
        print("%-13s : %d hit(s) in %d file(s)" % (cat, r["hits"].get(cat, 0),
                                                   len(fw)))
        for f, n in sorted(fw.items()):
            print("    %5d  %s" % (n, f))
    if a.detail:
        for d in r["detail"]:
            if d["category"] in GATES + ("symlink", "unreadable"):
                print("  detail: %s | %s | %s | line %s | %s"
                      % (d["file"], d["category"], d.get("part"),
                         d.get("line"), d.get("token")))
    print("RESULT        : %s" % ("CLEAN" if r["clean"] else
                                  "HITS (%d)" % r["gate_hits"]))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=1)
    return 0 if r["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
