"""Clean checkout, alternate bundle and runtime package regression tests."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import assemble_space
import fetch_bundle
import public_p70


class PublicP70Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clean = self.root / "data" / "clean"
        self.clean.mkdir(parents=True)
        self.base = self.clean / "estimator_calibration.csv"
        self.base.write_bytes(b"base fixture\n")
        self.target = self.clean / public_p70.NAME
        self.valid_base = hashlib.sha256(self.base.read_bytes()).hexdigest()

    def test_sidecar_identity_and_safe_retries(self):
        with patch.object(public_p70, "BASE_SHA256", self.valid_base):
            with self.assertRaisesRegex(ValueError, "Missing"):
                public_p70.install(self.clean, check_only=True)
            metadata = public_p70.install(self.clean)
            self.assertEqual(metadata["rows"], 3)
            self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(),
                             public_p70.SIDECAR_SHA256)
            inode = self.target.stat().st_ino
            public_p70.install(self.clean)
            self.assertEqual(self.target.stat().st_ino, inode)
            self.target.write_bytes(b"local custom calibration")
            with self.assertRaisesRegex(ValueError, "preserved"):
                public_p70.install(self.clean)
            self.assertEqual(self.target.read_bytes(), b"local custom calibration")

    def test_unmatched_base_and_invalid_resource(self):
        self.assertIsNone(public_p70.install(self.clean))
        self.assertFalse(self.target.exists())
        with patch.object(public_p70, "BASE_SHA256", self.valid_base), \
                patch.object(public_p70, "RESOURCE", self.base):
            with self.assertRaisesRegex(ValueError, "registered"):
                public_p70.install(self.clean)
        self.assertFalse(self.target.exists())

    def archive_fixture(self, published_sidecar=None):
        key = "data/clean/estimator_calibration.csv"
        data = self.base.read_bytes()
        files = {key: {"rows": 1, "bytes": len(data), "md5": hashlib.md5(data).hexdigest()}}
        manifest = json.dumps({"files": files, "total_bytes": len(data)}).encode()
        archive = self.root / "test.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            entries = [(key, data), ("data/MANIFEST.json", manifest)]
            if published_sidecar is not None:
                entries.append(("data/clean/" + public_p70.NAME, published_sidecar))
            for path, blob in entries:
                entry = tarfile.TarInfo(path)
                entry.size = len(blob)
                tar.addfile(entry, io.BytesIO(blob))
        lock = self.root / "lock.json"
        lock.write_text(json.dumps({"sha256": fetch_bundle.sha256(archive)}))
        return archive, lock

    def test_fresh_fetch_and_package(self):
        archive, lock = self.archive_fixture()
        checkout, space = self.root / "fresh", self.root / "package"
        with patch.object(fetch_bundle, "LOCK", lock), \
                patch.object(public_p70, "BASE_SHA256", self.valid_base), \
                patch.object(assemble_space, "TOOLS", self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            fetch_args = ["--file", str(archive), "--dest", str(checkout)]
            self.assertEqual(fetch_bundle.main(fetch_args), 0)
            sidecar = checkout / "data/clean" / public_p70.NAME
            installed = sidecar.stat().st_ino
            self.assertEqual(fetch_bundle.main(fetch_args), 0)
            self.assertEqual(sidecar.stat().st_ino, installed)
            self.assertEqual(fetch_bundle.main(["--check", "--dest", str(checkout)]), 0)
            self.assertEqual(assemble_space.main(["--bundle", str(checkout), "--space", str(space)]), 0)
        self.assertEqual((checkout / "data/clean" / public_p70.NAME).read_bytes(),
                         (space / "data/clean" / public_p70.NAME).read_bytes())
        for name in ["history_prices.py", "history_inflation.py", "static/manual.html",
                     "static/manual.js", "static/manual.css"]:
            self.assertTrue((space / "ui" / name).is_file(), name)
        for name in ["item_tokens.py", "bidder_identity.py"]:
            self.assertTrue((space / "src" / name).is_file(), name)
        shipped = json.loads((space / "data/MANIFEST.json").read_text())
        self.assertIn("data/clean/" + public_p70.NAME, shipped["files"])
        self.assertEqual(shipped["total_bytes"], sum(entry["bytes"] for entry in shipped["files"].values()))

    def test_custom_conflict_preserves_the_entire_existing_installation(self):
        checkout = self.root / "existing"
        clean = checkout / "data" / "clean"
        clean.mkdir(parents=True)
        for path, content in ((clean / public_p70.NAME, b"custom P70 result"),
                              (clean / self.base.name, b"original local calibration"),
                              (clean.parent / "MANIFEST.json", b"original local manifest"),
                              (clean / "another.csv", b"sentinel")):
            path.write_bytes(content)
        def state():
            return {p.relative_to(checkout): p.read_bytes() for p in checkout.rglob("*") if p.is_file()}
        initial = state()
        for expected_base, archived_sidecar in ((self.valid_base, None),
                                                 ("another version", b"release sidecar"),
                                                 ("another version", None)):
            with self.subTest(expected_base=expected_base, sidecar=archived_sidecar):
                archive, lock = self.archive_fixture(archived_sidecar)
                with patch.object(fetch_bundle, "LOCK", lock), \
                        patch.object(public_p70, "BASE_SHA256", expected_base), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(fetch_bundle.main(["--dest", str(checkout), "--file", str(archive)]), 1)
                self.assertEqual(state(), initial)

if __name__ == "__main__":
    unittest.main()
