"""Install the registered three-row P70 calibration for its exact public base."""

import hashlib
import os
from pathlib import Path
import tempfile


NAME = "estimator_p70_calibration.csv"
RESOURCE = Path(__file__).resolve().parents[1] / "calibration" / NAME
BASE_SHA256 = "7154840a414904d9516b92e6d2031b63b7013a55f7fc46748642d7952b58af23"
SIDECAR_SHA256 = "593eef00d6fc7fe83d26c42de36908369cfce4bda632fde38b55ca43c7979dfc"


def ensure_sidecar(clean, payload, *, check_only=False):
    """Make a verified payload available without changing existing custom data."""
    target = Path(clean) / NAME
    if not target.exists() and not check_only:
        # Publish complete bytes without replacing another installer's file.
        with tempfile.NamedTemporaryFile(dir=clean) as temp:
            temp.write(payload)
            temp.flush()
            os.chmod(temp.name, 0o644)
            try:
                os.link(temp.name, target)
            except FileExistsError:
                pass
    if not target.is_file():
        raise ValueError("Missing public P70 calibration. Run deploy/tools/fetch_bundle.py.")
    if target.read_bytes() != payload:
        raise ValueError("Existing P70 calibration differs from the public calibration. "
                         "It was preserved. Use a separate destination for the public bundle.")


def install(clean, *, check_only=False):
    """Return manifest metadata for a matching base, or None for another version."""
    base = Path(clean) / "estimator_calibration.csv"
    if not base.is_file() or hashlib.sha256(base.read_bytes()).hexdigest() != BASE_SHA256:
        return None
    # Match the builder's UTF-8 BOM and CSV line endings on every checkout.
    payload = RESOURCE.read_text(encoding="utf-8-sig").replace("\n", "\r\n").encode("utf-8-sig")
    if hashlib.sha256(payload).hexdigest() != SIDECAR_SHA256:
        raise ValueError("The repository P70 calibration differs from the registered public result.")
    ensure_sidecar(clean, payload, check_only=check_only)
    return {"rows": 3, "bytes": len(payload), "md5": hashlib.md5(payload).hexdigest(),
            "rule": "Registered public P70 trial bound to the SHA-256 of the base calibration."}
