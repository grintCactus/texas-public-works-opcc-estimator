"""Build the public data bundle: the data/clean files the server reads, with
public (source_set == "external") rows only, plus a manifest.

    .venv/bin/python deploy/tools/build_bundle.py TRACE.json [--out deploy/bundle]

TRACE.json is deploy/tools/trace_reads.py's record of the files the UI and
the estimator open on the production tree; the bundle holds exactly those
data/clean files (nothing is guessed):

  * a file with a source_set column keeps its "external" rows only (company
    "internal" and locked-holdout "public_test" rows are dropped);
  * project_metadata.csv keeps the rows of shipped lettings' source files
    only (company files, which have no folder, and the locked holdout's
    files are dropped);
  * every other file is a derived table (no row-level provenance column) and
    is copied whole -- each is listed with why it holds public data only,
    and checked: no cell names a letting key or source file that is not
    external;
  * the harvest manifests' source links (data/external/**/_manifest*.csv,
    which do not ship) are written to source_links.json by the UI's own
    reader, ui/lettings.load_manifest_links(), for the public lettings only.

Then every cell is scanned (deploy/tools/scan_public.py). A hit is fixed,
never shipped:
  * the employer's name in a bidder field (engineer's-estimate rows only)
    -> "Design Engineer";
  * a company-pattern word in any text -> a neutral stand-in ("redacted",
    then "withheld", ...), the same stand-in for the same word in every file
    so keys stay keys and equal texts stay equal;
  * a company-pattern digit run inside a decimal number -> the same number
    written with the decimal point moved into the run (exponent notation),
    which parses to the identical float -- checked;
  * a link (URL) holding a hit -> dropped.
Rows that are unchanged are copied byte for byte. Nothing found is printed:
the manifest and the output give counts only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
import scan_public as sp  # noqa: E402
import public_p70  # noqa: E402

csv.field_size_limit(10 ** 9)
CLEAN = os.path.join(REPO, "data", "clean")

#: Why each derived table (no row-level source_set) holds public data only.
DERIVED = {
    "price_reference.csv": "build_master: two-stage family medians over the "
        "external price pool; train_scope = public_external_only; leak guard "
        "GATE HELD",
    "price_reference_band.csv": "build_master: band-keyed cells over the "
        "external pool; train_scope = public_external_only",
    "price_reference_tokens.csv": "build_master: token-keyed cells over the "
        "external pool; train_scope = public_external_only",
    "price_by_quantity_band_family.csv": "build_master: quantity-shape fit "
        "on the TxDOT and city training items (+ statewide archive shape); "
        "leak guard checks it",
    "index_by_market.csv": "index_by_market: chained price index over "
        "external lettings; leak guard checks it",
    "price_time_index.csv": "build_master: pooled index; train_scope = "
        "public_external_only",
    "competition_discount_pooled.csv": "competition_and_crosswalk; "
        "train_scope = public_external_only; leak guard GATE HELD",
    "competition_discount_municipal.csv": "competition_and_crosswalk; "
        "train_scope = public_external_only; leak guard GATE HELD",
    "qty_shape_borrow_sigma.csv": "build_master: borrow error over training "
        "items; leak guard checks it",
    "estimator_calibration.csv": "estimate_low_bid.calibrate_forward over the "
        "external pool; train_scope = public_external_only; leak guard GATE "
        "HELD (re-run on data/clean as it stands = external-only run)",
    "estimator_t1_txdot.csv": "calibrate_forward's TxDOT D1 records and model "
        "totals; TxDOT has no company or holdout lettings",
    "item_vocabulary.csv": "build_vocabulary: pooled over per-project rows "
        "with source_set == 'external' only",
    "price_book_txdot.csv": "TxDOT published average prices; train_scope = "
        "txdot_published_only",
    "txdot_project_totals.csv": "TxDOT public bid data (bid totals)",
    "txdot_archive_project_totals.csv": "TxDOT public bid archive (totals)",
}
EMPLOYER_STAND_IN = "Design Engineer"
STAND_INS = ["redacted", "withheld", "omitted", "removed", "elided", "masked"]
NUM = re.compile(r"^(-?)(\d*)\.(\d+)(?:[eE]([-+]?\d+))?$")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _style(word, stand_in):
    if word.isupper() and len(word) > 1:
        return stand_in.upper()
    if word[:1].isupper():
        return stand_in[:1].upper() + stand_in[1:]
    return stand_in


class Redactor:
    def __init__(self):
        self.words = {}
        self.counts = collections.Counter()

    def _word(self, w):
        k = w.lower()
        if k not in self.words:
            self.words[k] = STAND_INS[len(self.words) % len(STAND_INS)] + (
                "" if len(self.words) < len(STAND_INS)
                else "x" * (len(self.words) // len(STAND_INS)))
        return _style(w, self.words[k])

    @staticmethod
    def gate_hits(text):
        return [h for h in sp.find(text) if h[0] in sp.GATES]

    def _renumber(self, v, hits):
        """A decimal number whose hit is a digit run: the same value with
        the decimal point moved into each run (exponent notation)."""
        m = NUM.match(v)
        if not m:
            return None
        sign, ip, fp, ex = m.group(1), m.group(2), m.group(3), m.group(4)
        digits, e = ip + fp, int(ex or 0)
        # string position -> digit index
        pos = {}
        k = 0
        for i, ch in enumerate(v):
            if ch.isdigit() and (m.start(4) < 0 or i < m.start(4) - 1):
                pos[i] = k
                k += 1
        for cat, s, t in hits:
            ds = [pos[i] for i in range(s, t) if i in pos]
            if not ds or len(ds) != sum(1 for i in range(s, t)
                                        if v[i].isdigit()):
                return None
        cands = []
        for cat, s, t in hits:
            ds = sorted(pos[i] for i in range(s, t) if i in pos)
            if len(ds) < 2:
                return None
            cands.append(ds[0] + len(ds) // 2)
        for q in sorted(set(cands)):
            e_new = e - len(fp) + len(digits) - q
            new = "%s%s.%se%d" % (sign, digits[:q] or "0", digits[q:] or "0",
                                  e_new)
            if float(new) == float(v) and not self.gate_hits(new):
                return new
        return None

    def cell(self, v, where, url=False):
        """The cell with every gate hit fixed (or "" for a URL with one)."""
        if not sp.ANY.search(v):
            return v
        hits = self.gate_hits(v)
        if not hits:
            return v
        if url:
            self.counts[(where, "url dropped")] += 1
            return ""
        if all(v[s:t].isdigit() for _, s, t in hits):
            new = self._renumber(v, hits)
            if new is not None:
                self.counts[(where, "number re-written")] += 1
                return new
            if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", v):
                # a number is never changed: left as is, so the scan fails
                # and the build stops for a decision
                self.counts[(where, "UNFIXED number (left as is)")] += 1
                return v
        out, last = [], 0
        for cat, s, t in sorted(hits, key=lambda h: (h[1], -h[2])):
            if s < last:
                continue
            out.append(v[last:s])
            span = v[s:t]
            if cat == "employer":
                out.append(EMPLOYER_STAND_IN)
                self.counts[(where, "employer name replaced")] += 1
            else:
                rep = re.sub(r"[A-Za-z]+", lambda m: self._word(m.group(0)),
                             span)
                rep = re.sub(r"\d", lambda m: "1" if m.group(0) == "9"
                             else str(int(m.group(0)) + 1), rep)
                out.append(rep)
                self.counts[(where, "%s text replaced" % cat)] += 1
            last = t
        out.append(v[last:])
        new = "".join(out)
        if self.gate_hits(new):
            return self.cell(new, where, url)
        return new


def raw_rows(fh):
    """(row, raw bytes) for each record after the header; csv.reader asks for
    one physical line at a time, so the bytes are exactly the record's."""
    buf = []

    def lines():
        for b in fh:
            buf.append(b)
            yield b.decode("utf-8")
    for row in csv.reader(lines()):
        raw = b"".join(buf)
        buf.clear()
        yield row, raw


def copy_csv(src, dst, red, keep=None, url_cols=(), nonext=None, stats=None):
    """Copy a CSV keeping rows where keep(row_dict_by_index) is true, fixing
    scan hits cell by cell. Returns the file's statistics."""
    name = os.path.basename(src)
    st = {"rows_in": 0, "rows_out": 0, "rows_changed": 0,
          "dropped_by_rule": collections.Counter(), "nonexternal_refs": 0}
    with open(src, "rb") as fh, open(dst, "wb") as out:
        first = fh.readline()
        out.write(first)
        header = next(csv.reader([first.decode("utf-8-sig")]))
        term = "\r\n" if first.endswith(b"\r\n") else "\n"
        ix = {h: i for i, h in enumerate(header)}
        uc = {ix[c] for c in url_cols if c in ix}
        for row, raw in raw_rows(fh):
            st["rows_in"] += 1
            if keep is not None:
                why = keep(row, ix)
                if why:
                    st["dropped_by_rule"][why] += 1
                    continue
            new = [red.cell(v, "%s:%s" % (name, header[i] if i < len(header)
                                          else i), url=i in uc)
                   if v else v for i, v in enumerate(row)]
            if nonext:
                st["nonexternal_refs"] += sum(1 for v in new if v in nonext)
            if new != row:
                st["rows_changed"] += 1
                b = io.StringIO()
                csv.writer(b, lineterminator=term).writerow(new)
                out.write(b.getvalue().encode("utf-8"))
            else:
                out.write(raw)
            st["rows_out"] += 1
    st["dropped_by_rule"] = dict(st["dropped_by_rule"])
    return st


def nonexternal_keys():
    """Letting keys and their source files that are NOT external (company
    and holdout lettings): no shipped cell may equal one."""
    keys = set()
    with open(os.path.join(CLEAN, "master_panel.csv"), encoding="utf-8-sig",
              newline="") as fh:
        rd = csv.reader(fh)
        h = next(rd)
        i_s, i_k = h.index("source_set"), h.index("project_key")
        ext = set()
        for r in rd:
            if r[i_s] == "external":
                ext.add(r[i_k])
            else:
                keys.add(r[i_k])
    files = {k.split("::")[0] for k in keys}
    ext_files = {k.split("::")[0] for k in ext}
    return (keys | files) - ext - ext_files, ext


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("trace")
    ap.add_argument("--out", default=os.path.join(REPO, "deploy", "bundle"))
    a = ap.parse_args(argv)
    t0 = time.time()
    tr = json.load(open(a.trace, encoding="utf-8"))
    opened = sorted(p for p in tr["opened"] if p.startswith("data/"))
    # The public trial is supplied by the versioned, base-bound calibration
    # at installation. A local custom trial never enters a release archive.
    clean = sorted(p for p in opened if p.startswith("data/clean/")
                   and p != "data/clean/" + public_p70.NAME)
    other = sorted(p for p in opened if not p.startswith("data/clean/"))
    manifests = [p for p in other if os.path.basename(p).startswith("_manifest")]
    unexpected = [p for p in other if p not in manifests]
    if unexpected:
        print("STOP: the server read data files outside data/clean that are "
              "not harvest manifests: %d (see the trace)" % len(unexpected))
        return 2
    out_clean = os.path.join(a.out, "data", "clean")
    if os.path.isdir(os.path.join(a.out, "data")):
        shutil.rmtree(os.path.join(a.out, "data"))
    os.makedirs(out_clean)
    red = Redactor()
    nonext, ext_keys = nonexternal_keys()
    ext_files = {k.split("::")[0] for k in ext_keys}
    files = {}
    for rel in clean:
        name = os.path.basename(rel)
        src = os.path.join(REPO, rel)
        dst = os.path.join(out_clean, name)
        with open(src, "rb") as fh:
            head = next(csv.reader([fh.readline().decode("utf-8-sig")]))
        if "source_set" in head:
            rule = "source_set == external"
            keep = (lambda row, ix: None if row[ix["source_set"]] == "external"
                    else "source_set " + (row[ix["source_set"]] or "(blank)"))
        elif name == "project_metadata.csv":
            rule = ("rows of the source files of shipped (external) lettings "
                    "only: company files (no folder) and locked-holdout files "
                    "are dropped")
            keep = (lambda row, ix: None
                    if row[ix["src_file"]] in ext_files
                    else ("company file" if "/" not in row[ix["src_file"]]
                          else "holdout or unused file"
                          if row[ix["src_file"]] in nonext
                          else "not a shipped letting"))
        elif name in DERIVED:
            rule = "derived table, copied whole: " + DERIVED[name]
            keep = None
        else:
            print("STOP: %s is read by the server but its provenance is not "
                  "listed in build_bundle.DERIVED -- review it first" % name)
            return 2
        st = copy_csv(src, dst, red, keep, url_cols=("url", "source_url"),
                      nonext=nonext)
        # keep the rebuild's time stamp: the page shows master_panel's mtime
        # as "data built"
        s0 = os.stat(src)
        os.utime(dst, ns=(s0.st_atime_ns, s0.st_mtime_ns))
        st.update(rule=rule, bytes=os.path.getsize(dst), md5=md5(dst),
                  columns=len(head), source_md5=md5(src))
        files["data/clean/" + name] = st
        print("%-36s %9d -> %9d rows  %8.1f MB  changed %d  dropped %s"
              % (name, st["rows_in"], st["rows_out"], st["bytes"] / 1e6,
                 st["rows_changed"], st["dropped_by_rule"] or "-"))

    # the harvest manifests' links, by the UI's own reader, public lettings only
    sys.path.insert(0, os.path.join(REPO, "ui"))
    import lettings  # noqa: E402
    links = lettings.load_manifest_links()
    files_needed = {k.split("::")[0] for k in ext_keys
                    if k.startswith("external/")}
    bases = {"#base:%s/%s" % (f.split("/")[1], f.rsplit("/", 1)[-1])
             for f in files_needed if f.count("/") >= 2}
    keep_links, dropped = {}, collections.Counter()
    for k, v in links.items():
        if k not in files_needed and k not in bases:
            dropped["not a shipped letting"] += 1
            continue
        url = v.get("url") or ""
        if red.gate_hits(url):
            dropped["link holds a scan hit"] += 1
            continue
        kk = red.cell(k, "source_links.json:key")
        keep_links[kk] = {"url": url, "kind": v.get("kind") or ""}
    sl = os.path.join(out_clean, "source_links.json")
    with open(sl, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "about": "Public source pages of the public "
                   "lettings, from the harvest manifests (built by "
                   "deploy/tools/build_bundle.py with "
                   "ui/lettings.load_manifest_links)",
                   "links": dict(sorted(keep_links.items()))}, fh, indent=0,
                  sort_keys=False)
    files["data/clean/source_links.json"] = {
        "rule": "harvest-manifest links of shipped public lettings only",
        "rows_out": len(keep_links), "dropped": dict(dropped),
        "manifests_read": len(manifests),
        "bytes": os.path.getsize(sl), "md5": md5(sl)}
    print("%-36s %9d links (dropped %s)" % ("source_links.json",
                                            len(keep_links), dict(dropped)))

    scan = sp.scan([os.path.join(a.out, "data")])
    fixes = collections.Counter()
    for (where, what), n in red.counts.items():
        fixes["%s | %s" % (where, what)] += n
    total = sum(v["bytes"] for v in files.values())
    man = {"built": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
           "builder": "deploy/tools/build_bundle.py",
           "trace": os.path.basename(a.trace),
           "rule": "public (source_set == 'external') rows only; derived "
                   "tables built from external rows only",
           "files": files, "total_bytes": total,
           "fixes": dict(sorted(fixes.items())),
           "stand_in_words": len(red.words),
           "nonexternal_key_matches": sum(v.get("nonexternal_refs", 0)
                                          for v in files.values()),
           "scan": {k: scan[k] for k in ("files", "hits", "gate_hits",
                                         "clean")}}
    with open(os.path.join(a.out, "MANIFEST.json"), "w",
              encoding="utf-8") as fh:
        json.dump(man, fh, indent=1)
    print("fixes:")
    for k, n in sorted(fixes.items()):
        print("   %6d  %s" % (n, k))
    print("cells equal to a non-external letting key or file: %d"
          % man["nonexternal_key_matches"])
    print("bundle: %d files, %.1f MB; scan %s (%s); %.0fs"
          % (len(files), total / 1e6, "CLEAN" if scan["clean"] else "HITS",
             scan["hits"], time.time() - t0))
    return 0 if scan["clean"] and not man["nonexternal_key_matches"] else 1


if __name__ == "__main__":
    sys.exit(main())
