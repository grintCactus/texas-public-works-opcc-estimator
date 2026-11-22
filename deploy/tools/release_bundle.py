"""Pack the public data bundle and (optionally) publish it as a release asset.

    python deploy/tools/release_bundle.py [--tag TAG]             # pack + write the lock
    python deploy/tools/release_bundle.py [--tag TAG] --publish   # ... and upload it

Run on the owner's working copy after deploy/tools/build_bundle.py and
deploy/tools/check_package.sh. It

  1. checks deploy/bundle again: every table with a source_set column holds
     "external" rows only, and deploy/tools/scan_public.py finds nothing;
  2. writes deploy/bundle/<asset>.tar.gz with data/MANIFEST.json (the public
     manifest: rows, bytes, md5 and the rule for each table) and
     data/clean/<table>, file names only, in sorted order;
  3. writes deploy/bundle.lock.json: tag, asset, URL, SHA-256, size -- the
     file deploy/tools/fetch_bundle.py reads. Commit it;
  4. with --publish: creates the GitHub release TAG (or adds the asset to it)
     with the GitHub CLI (`gh`, logged in with push rights).

The default tag is bundle-<date the bundle was built>. A published tag is
never reused for different data: pass a new --tag instead.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
import assemble_space  # noqa: E402  (non_external_rows)
import scan_public as sp  # noqa: E402

GITHUB = "grintCactus/texas-public-works-opcc-estimator"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bundle", default=os.path.join(REPO, "deploy", "bundle"))
    ap.add_argument("--tag")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args(argv)
    bundle = os.path.abspath(a.bundle)
    bd = os.path.join(bundle, "data", "clean")
    man = json.load(open(os.path.join(bundle, "MANIFEST.json"), encoding="utf-8"))
    files = sorted(os.path.basename(k) for k in man["files"])

    # 1. the bundle is public data only, and clean
    for f in files:
        if f.endswith(".csv"):
            bad = assemble_space.non_external_rows(os.path.join(bd, f))
            if bad:
                print("STOP: %s holds rows that are not public: %s" % (f, bad))
                return 1
    scan = sp.scan([bd])
    if not scan["clean"]:
        print("STOP: the scan of %s is not clean: %s" % (bd, scan["hits"]))
        return 1

    built = str(man.get("built") or "")
    tag = a.tag or "bundle-" + (re.match(r"\d{4}-\d{2}-\d{2}", built).group(0)
                                if re.match(r"\d{4}-\d{2}-\d{2}", built) else "undated")
    asset = "opcc-public-" + tag + ".tar.gz"
    src = open(os.path.join(REPO, "src", "parse_bidtabs.py"), encoding="utf-8").read()
    pv = re.search(r'^PARSER_VERSION = "([^"]+)"', src, re.M)
    public_manifest = {
        "about": "The public data bundle of the Texas Public Works OPCC "
                 "Estimator: the data/clean files the server reads, with "
                 "public lettings only (source_set == 'external').",
        "tag": tag,
        "built": built,
        "parser_version": pv.group(1) if pv else None,
        "files": {"data/clean/" + os.path.basename(k):
                  {"rows": v.get("rows_out"), "bytes": v.get("bytes"),
                   "md5": v.get("md5"), "rule": v.get("rule")}
                  for k, v in sorted(man["files"].items())},
        "total_bytes": man.get("total_bytes")}

    # 2. the archive: plain members, no owner names, no extended attributes
    out = os.path.join(bundle, asset)

    def clean(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o644
        return ti
    with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        body = json.dumps(public_manifest, indent=1).encode("utf-8")
        ti = tarfile.TarInfo("data/MANIFEST.json")
        ti.size = len(body)
        ti.mtime = int(os.path.getmtime(os.path.join(bundle, "MANIFEST.json")))
        tf.addfile(clean(ti), io.BytesIO(body))
        for f in files:
            tf.add(os.path.join(bd, f), arcname="data/clean/" + f, filter=clean)
    digest, size = sha256(out), os.path.getsize(out)

    # 3. the lock
    lock = {"about": "The public data bundle this checkout uses. Read by "
                     "deploy/tools/fetch_bundle.py; written by "
                     "deploy/tools/release_bundle.py.",
            "tag": tag, "asset": asset,
            "url": "https://github.com/%s/releases/download/%s/%s" % (GITHUB, tag, asset),
            "sha256": digest, "bytes": size, "built": built,
            "parser_version": public_manifest["parser_version"],
            "tables": len(files), "unpacked_bytes": man.get("total_bytes")}
    with open(os.path.join(REPO, "deploy", "bundle.lock.json"), "w", encoding="utf-8") as fh:
        json.dump(lock, fh, indent=1)
        fh.write("\n")
    print("packed %s: %.1f MB, sha256 %s" % (os.path.relpath(out, REPO), size / 1e6, digest))
    print("wrote deploy/bundle.lock.json (tag %s) -- commit it" % tag)

    # 4. publish
    if a.publish:
        gh = shutil.which("gh") or os.path.join(REPO, "tools", "gh", "bin", "gh")
        if not os.path.exists(gh):
            print("STOP: the GitHub CLI (gh) was not found")
            return 2
        notes = ("Public data bundle for the estimator and the web app: the "
                 "data/clean tables the server reads, built from public bid "
                 "tabulations only (%d tables, %.0f MB unpacked; parser %s). "
                 "Get it with `python deploy/tools/fetch_bundle.py`."
                 % (len(files), (man.get("total_bytes") or 0) / 1e6,
                    public_manifest["parser_version"]))
        have = subprocess.run([gh, "release", "view", tag, "-R", GITHUB],
                              capture_output=True).returncode == 0
        cmd = ([gh, "release", "upload", tag, out, "-R", GITHUB] if have else
               [gh, "release", "create", tag, out, "-R", GITHUB, "--title",
                "Public data bundle " + tag.replace("bundle-", ""), "--notes", notes])
        rc = subprocess.call(cmd)
        if rc:
            print("STOP: gh exited with %d" % rc)
            return rc
        print("published: %s" % lock["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
