"""Assemble deploy/space/ -- the Docker build context that is sent to the
server (and the layout of a Hugging Face Space) -- from this checkout and a
public data bundle, then check and scan every file in it.

    python deploy/tools/assemble_space.py [--space deploy/space] [--bundle DIR]

What goes in (the server's run-time files only):
  src/   the estimator and every src/ module the web server imports, found by
         following the imports from ui/ (so a new import cannot be forgotten);
  ui/    the server, its static page and item_accuracy.json -- not the tests,
         not ui/.cache, ui/.inbox or ui/.local;
  data/  the public data bundle: data/clean/*.csv, source_links.json and a
         public manifest;
  and the package files kept in deploy/package/ (Dockerfile, .dockerignore,
  .gitattributes, requirements.txt, README.md).

Where the bundle comes from (--bundle, else the first that exists):
  deploy/bundle/     written by deploy/tools/build_bundle.py on a working copy
                     whose data/clean also holds test rows;
  data/              a checkout whose data/clean IS the public bundle
                     (deploy/tools/fetch_bundle.py put it there).

Two checks stop a package that must not be published (exit code 1):
  * every bundle table that labels its rows with source_set holds "external"
    rows only -- test rows (a private company test set, the locked public
    test set) never leave the machine they are on;
  * deploy/tools/scan_public.py finds nothing in the package.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import shutil
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
import scan_public as sp  # noqa: E402
import public_p70  # noqa: E402

csv.field_size_limit(10 ** 9)

UI_COPY = ["app.py", "build_item_accuracy.py", "data_access.py",
           "engine_production.py", "exporter.py", "history_prices.py",
           "history_inflation.py", "intake.py",
           "integration_check.py", "lettings.py", "matching.py",
           "public_mode.py", "run.py", "upstream.py", "worksheet.py",
           "item_accuracy.json", "ingest/__init__.py", "ingest/bidtab.py",
           "ingest/claude_reader.py", "ingest/ocr.py", "ingest/takeoff.py",
           "static/index.html", "static/app.js", "static/style.css",
           "static/manual.html", "static/manual.css", "static/manual.js"]
UI_OPTIONAL = ["public_mirror.json"]
PACKAGE_FILES = ["Dockerfile", ".dockerignore", ".gitattributes",
                 "requirements.txt", "README.md"]


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def imported_names(path):
    """Top-level module names a Python file imports, anywhere in it."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def src_closure():
    """The src/ modules the server needs: every src/<name>.py imported from
    ui/, and every src/ module those import in turn."""
    src = os.path.join(REPO, "src")
    need, todo = set(), []
    for f in UI_COPY:
        if f.endswith(".py"):
            todo.append(os.path.join(REPO, "ui", f))
    while todo:
        p = todo.pop()
        for name in imported_names(p):
            q = os.path.join(src, name + ".py")
            if name not in need and os.path.exists(q):
                need.add(name)
                todo.append(q)
    return sorted(n + ".py" for n in need)


def non_external_rows(path):
    """{source_set: rows} for every value other than 'external' in a CSV
    that has a source_set column; None when it has no such column."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        try:
            head = next(rd)
        except StopIteration:
            return None
        if "source_set" not in head:
            return None
        i = head.index("source_set")
        bad = {}
        for row in rd:
            v = row[i] if i < len(row) else ""
            if v != "external":
                bad[v] = bad.get(v, 0) + 1
        return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--space", default=os.path.join(REPO, "deploy", "space"))
    ap.add_argument("--bundle", default=None,
                    help="folder holding data/clean of the public bundle")
    a = ap.parse_args(argv)
    space = os.path.abspath(a.space)
    bundle = a.bundle
    if bundle is None:
        own = os.path.join(REPO, "deploy", "bundle")
        bundle = own if os.path.isdir(os.path.join(own, "data", "clean")) \
            else REPO
    bundle = os.path.abspath(bundle)
    bd = os.path.join(bundle, "data", "clean")
    if not os.path.isdir(bd):
        print("STOP: no data/clean under %s -- run deploy/tools/fetch_bundle.py "
              "(a fresh checkout) or deploy/tools/build_bundle.py (a working "
              "copy) first" % bundle)
        return 2
    pkg = os.path.join(REPO, "deploy", "package")
    for n in PACKAGE_FILES:
        if not os.path.exists(os.path.join(pkg, n)):
            print("STOP: deploy/package/%s is missing" % n)
            return 2

    # which data files ship: the bundle's manifest says (only the tables the
    # server reads); a folder without a manifest is not a bundle
    mpath = next((p for p in (os.path.join(bundle, "MANIFEST.json"),
                              os.path.join(bundle, "data", "MANIFEST.json"))
                  if os.path.exists(p)), None)
    if mpath is None:
        print("STOP: no MANIFEST.json for the bundle under %s" % bundle)
        return 2
    with open(mpath, encoding="utf-8") as manifest:
        bman = json.load(manifest)
    files = sorted(os.path.basename(k) for k in bman["files"])
    missing = [f for f in files if not os.path.exists(os.path.join(bd, f))]
    if missing:
        print("STOP: the bundle lacks %s" % ", ".join(missing))
        return 2

    # check 1: public rows only
    for f in files:
        if f.endswith(".csv"):
            bad = non_external_rows(os.path.join(bd, f))
            if bad:
                print("STOP: %s holds rows that are not public (%s). This is "
                      "a working copy's data/clean: build the public bundle "
                      "with deploy/tools/build_bundle.py and pass it with "
                      "--bundle." % (f, ", ".join(
                          "%s: %d" % kv for kv in sorted(bad.items()))))
                return 1

    if os.path.isdir(space):
        shutil.rmtree(space)
    os.makedirs(space)
    rep = {"built": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "src": {}, "ui": {},
           "bundle": os.path.relpath(bundle, REPO)}
    for n in PACKAGE_FILES:
        shutil.copy2(os.path.join(pkg, n), os.path.join(space, n))

    os.makedirs(os.path.join(space, "src"))
    for f in src_closure():
        d = os.path.join(space, "src", f)
        shutil.copy2(os.path.join(REPO, "src", f), d)
        rep["src"][f] = md5(d)

    for f in UI_COPY + [x for x in UI_OPTIONAL
                        if os.path.exists(os.path.join(REPO, "ui", x))]:
        d = os.path.join(space, "ui", f)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(REPO, "ui", f), d)
        rep["ui"][f] = md5(d)

    dd = os.path.join(space, "data", "clean")
    os.makedirs(dd)
    for f in files:
        try:                       # same bytes, no second copy on disk
            os.link(os.path.join(bd, f), os.path.join(dd, f))
        except OSError:
            shutil.copy2(os.path.join(bd, f), os.path.join(dd, f))
    public_manifest = {
        "about": "The public data bundle of the Texas Public Works OPCC "
                 "Estimator: the data/clean files the server reads, with "
                 "public lettings only (source_set == 'external').",
        "built": bman.get("built"),
        "files": {k: {"rows": v.get("rows_out", v.get("rows")),
                      "bytes": v.get("bytes"), "md5": v.get("md5"),
                      "rule": v.get("rule")}
                  for k, v in bman["files"].items()},
        "total_bytes": bman.get("total_bytes")}
    try:
        sidecar = public_p70.install(dd)
    except (OSError, ValueError) as exc:
        print("STOP: %s" % exc)
        return 1
    if sidecar and public_p70.NAME not in files:
        public_manifest["files"]["data/clean/" + public_p70.NAME] = sidecar
        public_manifest["total_bytes"] = sum(entry["bytes"] for entry in public_manifest["files"].values())
        files.append(public_p70.NAME)
    with open(os.path.join(space, "data", "MANIFEST.json"), "w",
              encoding="utf-8") as fh:
        json.dump(public_manifest, fh, indent=1)

    # check 2: scan everything that would be published
    scan = sp.scan([space])
    rep["scan"] = {k: scan[k] for k in ("files", "bytes", "hits",
                                        "files_with_hits", "gate_hits",
                                        "clean")}
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(space) for f in fs)
    rep["bytes"] = size
    with open(os.path.join(TOOLS, "assemble_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    print("space: %d files, %.1f MB; src %d, ui %d, data %d; scan %s %s"
          % (scan["files"], size / 1e6, len(rep["src"]), len(rep["ui"]),
             len(files), "CLEAN" if scan["clean"] else "HITS", scan["hits"]))
    if not scan["clean"]:
        for cat in sp.GATES:
            for f, k in (scan["files_with_hits"].get(cat) or {}).items():
                print("   %s: %d in %s" % (cat, k, f))
    return 0 if scan["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
