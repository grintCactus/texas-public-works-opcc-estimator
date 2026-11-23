"""Get the public data bundle and unpack it into data/, so a fresh checkout
can run the web app and build the server package.

    python deploy/tools/fetch_bundle.py              # download, verify, unpack
    python deploy/tools/fetch_bundle.py --file X.tar.gz   # unpack a local copy
    python deploy/tools/fetch_bundle.py --check      # verify what is in data/

The bundle is the set of data/clean tables the server reads, with public
lettings only (source_set == "external"): about 350 MB unpacked, 21 MB as a
.tar.gz. It is published as a release asset of this repository;
deploy/bundle.lock.json names the release, the asset and its SHA-256, so a
checkout always gets the bundle its code was verified with.

Nothing is unpacked unless the archive's SHA-256 matches the lock, every
member is one of data/MANIFEST.json or data/clean/<plain file name>, and
every unpacked table matches the md5 in the manifest.

It refuses to run in a working copy that has built its own tables
(data/clean/line_items.csv exists): those are not to be overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TOOLS))
LOCK = os.path.join(REPO, "deploy", "bundle.lock.json")
sys.path.insert(0, TOOLS)
import public_p70  # noqa: E402


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download(url, dest):
    """Fetch url to dest. urllib first (with the system CA file where the
    Python build ships none), then curl."""
    tmp = dest + ".part"
    errors = []
    for ctx in (None, "/etc/ssl/cert.pem"):
        try:
            if ctx and not os.path.exists(ctx):
                continue
            context = ssl.create_default_context(cafile=ctx) if ctx else None
            req = urllib.request.Request(url, headers={"User-Agent": "opcc-estimator-fetch"})
            with urllib.request.urlopen(req, timeout=120, context=context) as r, \
                    open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh, 1 << 20)
            os.replace(tmp, dest)
            return
        except Exception as ex:  # noqa: BLE001 - try the next way
            errors.append("%s: %s" % (type(ex).__name__, ex))
    if shutil.which("curl"):
        rc = subprocess.call(["curl", "-fL", "--retry", "3", "-o", tmp, url])
        if rc == 0:
            os.replace(tmp, dest)
            return
        errors.append("curl exit %d" % rc)
    raise SystemExit("STOP: could not download %s\n  %s" % (url, "\n  ".join(errors)))


def check(dest):
    """Compare data/clean with data/MANIFEST.json. Returns the problem count."""
    mpath = os.path.join(dest, "data", "MANIFEST.json")
    if not os.path.exists(mpath):
        print("no data/MANIFEST.json under %s" % dest)
        return 1
    with open(mpath, encoding="utf-8") as manifest:
        man = json.load(manifest)
    bad = 0
    for k, v in sorted(man["files"].items()):
        p = os.path.join(dest, k)
        if not os.path.exists(p):
            print("  missing   %s" % k)
            bad += 1
        elif v.get("md5") and md5(p) != v["md5"]:
            print("  different %s" % k)
            bad += 1
    print("bundle built %s: %d files, %s" % (
        man.get("built"), len(man["files"]),
        "all present and identical to the manifest" if not bad
        else "%d problem(s)" % bad))
    return bad


def check_existing_p70(tf, members, clean):
    """Check an existing trial against the incoming archive before replacing any table."""
    target = os.path.join(clean, public_p70.NAME)
    if not os.path.lexists(target):
        return
    if not os.path.isfile(target):
        raise ValueError("The existing P70 path is not a regular file. Use a separate destination.")
    current = sha256(target)
    by_name = {m.name: m for m in members if m.isfile()}
    incoming_sidecar = by_name.get("data/clean/" + public_p70.NAME)
    if incoming_sidecar:
        with tf.extractfile(incoming_sidecar) as stream:
            if current == hashlib.sha256(stream.read()).hexdigest():
                return
    else:
        incoming_base = by_name.get("data/clean/estimator_calibration.csv")
        if incoming_base:
            with tf.extractfile(incoming_base) as stream:
                base_sha = hashlib.sha256(stream.read()).hexdigest()
            if base_sha == public_p70.BASE_SHA256:
                if current == public_p70.SIDECAR_SHA256:
                    return
            else:
                # An older bundled trial is ignored by the model on a new
                # base. A custom trial still belongs to its own base.
                old_base = os.path.join(clean, "estimator_calibration.csv")
                if (current == public_p70.SIDECAR_SHA256
                        or (os.path.isfile(old_base) and sha256(old_base) == base_sha)):
                    return
    raise ValueError("The incoming bundle conflicts with the existing P70 calibration. "
                     "The installed tables were preserved. Use a separate destination.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=REPO, help="folder that gets data/ (default: this checkout)")
    ap.add_argument("--file", help="a bundle .tar.gz already on disk")
    ap.add_argument("--check", action="store_true", help="only verify data/ against its manifest")
    a = ap.parse_args(argv)
    dest = os.path.abspath(a.dest)
    if a.check:
        if check(dest):
            return 1
        try:
            public_p70.install(os.path.join(dest, "data", "clean"), check_only=True)
        except (ValueError, OSError) as exc:
            print("STOP: %s" % exc)
            return 1
        return 0
    with open(LOCK, encoding="utf-8") as lock_file:
        lock = json.load(lock_file)
    if os.path.exists(os.path.join(dest, "data", "clean", "line_items.csv")):
        print("STOP: %s has its own built tables (data/clean/line_items.csv). "
              "The bundle is for a fresh checkout; use --dest for another folder."
              % dest)
        return 2
    arc = a.file
    if not arc:
        cache = os.path.join(dest, "data", ".download")
        os.makedirs(cache, exist_ok=True)
        arc = os.path.join(cache, lock["asset"])
        if not (os.path.exists(arc) and sha256(arc) == lock["sha256"]):
            print("downloading %s (%.1f MB)" % (lock["url"], lock["bytes"] / 1e6))
            download(lock["url"], arc)
    got = sha256(arc)
    if got != lock["sha256"]:
        print("STOP: %s has SHA-256 %s, the lock expects %s" % (arc, got, lock["sha256"]))
        return 1
    with tarfile.open(arc, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            parts = m.name.split("/")
            ok = m.isdir() and m.name in ("data", "data/clean") or m.isfile() and (
                m.name == "data/MANIFEST.json"
                or (len(parts) == 3 and parts[:2] == ["data", "clean"]
                    and parts[2] not in ("", ".", "..")))
            if not ok:
                print("STOP: unexpected member in the archive: %r" % m.name)
                return 1
        try:
            check_existing_p70(tf, members, os.path.join(dest, "data", "clean"))
        except (OSError, ValueError) as exc:
            print("STOP: %s" % exc)
            return 1
        os.makedirs(os.path.join(dest, "data", "clean"), exist_ok=True)
        for m in members:
            if m.isfile():
                out = os.path.join(dest, *m.name.split("/"))
                if m.name == "data/clean/" + public_p70.NAME:
                    try:
                        with tf.extractfile(m) as src:
                            public_p70.ensure_sidecar(os.path.dirname(out), src.read())
                    except (OSError, ValueError) as exc:
                        print("STOP: %s" % exc)
                        return 1
                    continue
                with tf.extractfile(m) as src, open(out + ".part", "wb") as fh:
                    shutil.copyfileobj(src, fh, 1 << 20)
                os.replace(out + ".part", out)
    bad = check(dest)
    if not bad:
        try:
            sidecar = public_p70.install(os.path.join(dest, "data", "clean"))
        except (ValueError, OSError) as exc:
            print("STOP: %s" % exc)
            return 1
        if sidecar:
            print("P70 calibration matches the registered public result.")
        print("ready: start the app with  python ui/run.py  (http://127.0.0.1:8017/)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
