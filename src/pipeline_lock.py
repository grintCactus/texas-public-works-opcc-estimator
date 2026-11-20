"""
A single-writer lock for the data/external pipeline.

Two sessions work this corpus at once. The audit, the index and the quarantine
all write shared state (`_audit.csv`, `INDEX.csv`, and the files themselves),
and running two of them together has already caused real damage twice:

  * two audits writing `_audit.csv` at the same time left the index reading a
    half-written file -- 54 Mesquite and 17 Rowlett files silently vanished
    from the results, with no error raised;
  * the quarantine moves files while a parser is reading them, which hands the
    parser paths that no longer exist.

Neither failure announces itself, so the lock is the guard: whoever is second
is told who holds it and exits instead of interleaving.

Usage:
    from pipeline_lock import pipeline_lock

    with pipeline_lock("index"):
        ...

Pass --force to a caller to break a lock left behind by a crashed run; stale
locks (older than STALE_AFTER) are reported so that choice is informed.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LOCK_PATH = Path("data/external/_pipeline.lock")
STALE_AFTER = 60 * 60  # an hour; the longest full pass measured is ~15 min


class PipelineBusy(RuntimeError):
    """Raised when another writer holds the lock."""


def _read() -> dict | None:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _describe(info: dict) -> str:
    age = time.time() - float(info.get("epoch", 0))
    stale = " (STALE -- the run that took it probably died)" if age > STALE_AFTER else ""
    return (f"held by {info.get('stage', '?')} "
            f"pid {info.get('pid', '?')} on {info.get('host', '?')} "
            f"since {info.get('started', '?')}, {age / 60:.0f} min ago{stale}")


@contextmanager
def pipeline_lock(stage: str, force: bool = False):
    """Hold the pipeline lock for the duration of the block."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read() if LOCK_PATH.exists() else None
    if existing and not force:
        raise PipelineBusy(
            f"data/external pipeline is busy: {_describe(existing)}.\n"
            f"Wait for it, or re-run with --force if you are sure that run is dead."
        )
    if existing and force:
        print(f"  !! breaking lock: {_describe(existing)}")

    LOCK_PATH.write_text(json.dumps({
        "stage": stage,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": time.time(),
    }, indent=2), encoding="utf-8")
    try:
        yield
    finally:
        # Only clear it if it is still ours, so a forced takeover by someone
        # else does not get wiped by this process on its way out.
        cur = _read()
        if cur and cur.get("pid") == os.getpid():
            LOCK_PATH.unlink(missing_ok=True)
