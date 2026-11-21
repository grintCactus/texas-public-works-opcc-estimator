"""Help with messy takeoffs: which lines to check, what they probably are,
and the corrections the estimator has made before.

Three pieces, all local and all deterministic:

  assess()        marks a line LOW CONFIDENCE when it was not read or
                  classified well: no item family matched ("other"), the
                  wording matches nothing in the public price history well
                  (best history score below matching.WEAK_MATCH), no unit, no
                  quantity, or the reader itself was unsure (an OCR'd or
                  Claude-read line it flagged).
  suggestions()   the top 3 earlier public line items for such a line -- the
                  same unit when the line has one, ranked by the history
                  search's token similarity -- for a one-click choice of
                  family (and unit, when the line has none).
  Corrections     the families (and missing units) the estimator picked,
                  remembered per description in ui/.local/corrections.json
                  and applied the next time the same wording comes in,
                  tagged "remembered". Never quantities, never prices.

Nothing here changes how a line is priced on its own: a remembered or
suggested family reaches the estimator only as the line's family override,
which the browser sends with the estimate request like any other choice --
so an estimate is still fully determined by its request.

The corrections file holds the estimator's own takeoff wording, so it stays
on this machine: ui/.local/ is listed in .git/info/exclude, and nothing here
prints its contents. Set BID_UI_CORRECTIONS to use another file (the test
suite does, so it never touches the real one).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from typing import Dict, List, Optional

import upstream

HERE = os.path.dirname(os.path.abspath(__file__))
CORRECTIONS_ENV = "BID_UI_CORRECTIONS"
DEFAULT_CORRECTIONS = os.path.join(HERE, ".local", "corrections.json")

#: Families that say "the parser did not recognise this", not a kind of work.
UNCLASSIFIED = ("other", "needs_review", "")

#: How many suggestions a low-confidence line gets.
N_SUGGEST = 3

#: Reader issues that the unit / quantity checks already report.
_COVERED_ISSUES = ("no quantity found", "no unit found", "unrecognised unit")


# ---------------------------------------------------------------------------
# Corrections memory
# ---------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class Corrections:
    """{normalised description: the family (and unit) the estimator chose}.

    Keyed on the parser's own normalisation (upstream.norm_desc), so case,
    spacing and the abbreviations the parser expands do not matter. A unit is
    stored only when the line had none (or one the parser does not know)
    when it was corrected: units differ from job to job for the same wording,
    so a remembered unit fills a gap and never overwrites what a file says.
    """

    VERSION = 1

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get(CORRECTIONS_ENV) or DEFAULT_CORRECTIONS
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}
        self._stamp = None          # (mtime, size) of the file last read
        self.error = ""

    # -- storage -------------------------------------------------------------
    def _load(self) -> None:
        """(Re)read the file when it changed on disk. A missing file is an
        empty memory; an unreadable one is reported, never fatal."""
        try:
            st = os.stat(self.path)
        except OSError:
            self._entries, self._stamp, self.error = {}, None, ""
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            ent = d.get("entries") if isinstance(d, dict) else None
            if not isinstance(ent, dict):
                raise ValueError("no 'entries' object")
            self._entries = {str(k): v for k, v in ent.items()
                             if isinstance(v, dict) and v.get("family")}
            self.error = ""
        except (OSError, ValueError) as e:
            # keep the file as it is (it may be hand-edited); work without it
            self._entries = {}
            self.error = "%s could not be read (%s); corrections are not " \
                         "applied until it is fixed or removed." % (
                             self.rel_path(), type(e).__name__)
        self._stamp = stamp

    def _save(self) -> None:
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        body = {"version": self.VERSION,
                "about": "Classification corrections made in the bid "
                         "estimator UI. Local to this machine; not committed.",
                "entries": self._entries}
        fd, tmp = tempfile.mkstemp(prefix=".corrections_", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        st = os.stat(self.path)
        self._stamp = (st.st_mtime_ns, st.st_size)
        self.error = ""

    def rel_path(self) -> str:
        try:
            return os.path.relpath(self.path, os.path.dirname(HERE))
        except ValueError:
            return self.path

    # -- API -----------------------------------------------------------------
    @staticmethod
    def key(description) -> str:
        return upstream.norm_desc(str(description or "")).strip()

    def get(self, description) -> Optional[dict]:
        k = self.key(description)
        if not k:
            return None
        with self._lock:
            self._load()
            e = self._entries.get(k)
            return dict(e) if e else None

    def remember(self, description, family: str, unit: str = "",
                 source: str = "picker") -> dict:
        k = self.key(description)
        if not k:
            raise ValueError("A correction needs the line's description.")
        family = str(family or "").strip()
        if not family or family in UNCLASSIFIED:
            raise ValueError("A correction needs a real item family.")
        unit = upstream.norm_unit(str(unit or "")).upper() if unit else ""
        with self._lock:
            self._load()
            old = self._entries.get(k) or {}
            e = {"family": family, "unit": unit,
                 "source": str(source or "picker")[:20],
                 "first": old.get("first") or _now(), "last": _now(),
                 "times": int(old.get("times") or 0) + 1}
            self._entries[k] = e
            self._save()
            return dict(e)

    def forget(self, description) -> bool:
        k = self.key(description)
        with self._lock:
            self._load()
            if k not in self._entries:
                return False
            del self._entries[k]
            self._save()
            return True

    def status(self) -> dict:
        with self._lock:
            self._load()
            return {"file": self.rel_path(), "entries": len(self._entries),
                    "error": self.error}


class BrowserCorrections:
    """Public-demo mode (ui/public_mode.py): the corrections the visitor's
    browser keeps in localStorage, sent with each /api/classify request and
    used for that request only. Nothing is read from or written to the
    server.

    `entries`: {Corrections.key(description): {family, unit?, last?, times?}}
    as the browser sent it. Malformed entries, unknown families and anything
    past MAX_ENTRIES are dropped rather than trusted."""

    MAX_ENTRIES = 2000

    def __init__(self, entries, known_families=None):
        self._entries: Dict[str, dict] = {}
        self.error = ""
        if not isinstance(entries, dict):
            return
        for k, v in list(entries.items())[:self.MAX_ENTRIES]:
            if not isinstance(k, str) or not isinstance(v, dict) \
                    or not k.strip() or len(k) > 400:
                continue
            fam = str(v.get("family") or "").strip()
            if not fam or fam in UNCLASSIFIED or (
                    known_families is not None and fam not in known_families):
                continue
            unit = str(v.get("unit") or "").strip()
            unit = upstream.norm_unit(unit).upper() if unit else ""
            if unit and unit not in upstream.KNOWN_UNITS:
                unit = ""
            try:
                times = max(1, min(10 ** 6, int(v.get("times") or 1)))
            except (TypeError, ValueError):
                times = 1
            self._entries[k.strip()] = {
                "family": fam, "unit": unit,
                "source": str(v.get("source") or "browser")[:20],
                "last": str(v.get("last") or "")[:25], "times": times}

    @staticmethod
    def entry(description, family: str, unit: str = "",
              source: str = "picker") -> dict:
        """What the browser stores for one correction, under `key` -- the
        parser's normalised wording, which only the server can compute."""
        k = Corrections.key(description)
        if not k:
            raise ValueError("A correction needs the line's description.")
        family = str(family or "").strip()
        if not family or family in UNCLASSIFIED:
            raise ValueError("A correction needs a real item family.")
        unit = upstream.norm_unit(str(unit or "")).upper() if unit else ""
        return {"key": k, "family": family, "unit": unit,
                "source": str(source or "picker")[:20], "last": _now()}

    def get(self, description) -> Optional[dict]:
        k = Corrections.key(description)
        e = self._entries.get(k) if k else None
        return dict(e) if e else None

    def status(self) -> dict:
        return {"mode": "browser", "file": "", "entries": len(self._entries),
                "error": ""}


# ---------------------------------------------------------------------------
# Low confidence and suggestions
# ---------------------------------------------------------------------------

def _matching():
    import matching
    return matching


def history_candidates(description: str, unit: str):
    """The history search's ranked candidates for a line (cached there)."""
    m = _matching()
    return m.candidates(str(description or ""), str(unit or ""), limit=15)


def best_match_score(cands) -> Optional[float]:
    """The best score of a specific earlier line item (vocabulary / TxDOT),
    ignoring the family-average fallback, which is always offered."""
    sc = [c.score for c in cands if c.kind != "family"]
    return max(sc) if sc else 0.0


def suggestions(cands, unit: str, n: int = N_SUGGEST) -> List[dict]:
    """Top-n earlier public line items: same unit when the line has one,
    a real family, one per (family, unit, wording), in score order."""
    import engine_production as ep
    u = upstream.norm_unit(unit).upper() if unit else ""
    out, seen = [], set()
    for c in cands:
        if c.kind == "family":
            continue
        cu = str(c.unit or "").upper()
        if u and cu != u:
            continue
        fam = c.item_family or upstream.item_family(upstream.norm_desc(c.label))
        if fam in UNCLASSIFIED:
            continue
        k = (fam, cu, c.label.strip().lower())
        if k in seen:
            continue
        seen.add(k)
        out.append({"label": c.label, "unit": cu, "family": fam,
                    "family_label": ep.family_label(fam),
                    "source": "TxDOT" if c.kind == "txdot" else "city bid tabs",
                    "n_projects": int(c.n_projects or 0),
                    "score": round(float(c.score), 2)})
        if len(out) >= n:
            break
    return out


def assess(line: dict, c: dict, best: Optional[float]) -> dict:
    """Why a classified line is low confidence (an empty list: it is not).

    `best` is the best history match score, or None when the history index
    is not loaded yet (the weak-match test is then skipped, not failed).
    """
    m = _matching()
    reasons = []
    if c.get("coverage_class") or c.get("owner_set_like"):
        # not covered (a vendor quote) or owner-set money (the owner's
        # amount): neither is priced from history, and each has its own flag
        return {"low": False, "reasons": [], "best_match": None
                if best is None else round(float(best), 2)}
    chosen = bool(c.get("family_overridden") or c.get("remembered")
                  or line.get("family"))
    if c.get("family") in UNCLASSIFIED:
        reasons.append({"code": "unclassified",
                        "text": "no item family matched"})
    if (best is not None and best < m.WEAK_MATCH and not chosen
            and not c.get("owner_set_like") and not c.get("coverage_class")):
        reasons.append({"code": "weak_match",
                        "text": "nothing in the public price history matches "
                                "this wording well (best %d%%)"
                                % round(100 * best)})
    if not c.get("unit"):
        reasons.append({"code": "no_unit", "text": "no unit"})
    elif not c.get("unit_known", True):
        reasons.append({"code": "unknown_unit",
                        "text": "unit %r is not one the parser knows"
                                % c.get("unit")})
    q = line.get("quantity")
    try:
        qok = q is not None and float(q) > 0
    except (TypeError, ValueError):
        qok = False
    if not qok:
        reasons.append({"code": "no_quantity", "text": "no quantity"})
    if str(line.get("confidence") or "") == "low":
        # the reader's own notes, minus the missing unit / quantity already
        # reported above
        why = [str(x) for x in (line.get("issues") or [])
               if not str(x).startswith(_COVERED_ISSUES)][:2]
        if why:
            reasons.append({"code": "reader_unsure",
                            "text": "the reader was unsure: " + "; ".join(why)})
    return {"low": bool(reasons), "reasons": reasons,
            "best_match": None if best is None else round(float(best), 2)}


def annotate(engine, lines: List[dict], classified: List[dict], market: str,
             corrections: Optional[Corrections], history_ready: bool) -> List[dict]:
    """Remembered corrections, low-confidence reasons and suggestions for
    the lines /api/classify just classified (same order)."""
    out = []
    for ln, c in zip(lines or [], classified):
        ln = ln or {}
        # a correction remembered for this wording, unless the browser already
        # sends a family or the estimator explicitly kept the parser's
        if corrections is not None and not ln.get("family") \
                and not ln.get("keep_parser"):
            e = corrections.get(ln.get("description"))
            if e and e.get("family"):
                fill_unit = bool(e.get("unit")) and not upstream.norm_unit(
                    str(ln.get("unit") or "")).upper()
                if e["family"] != c.get("family_parser") or fill_unit:
                    ln2 = dict(ln, family=e["family"])
                    if fill_unit:
                        ln2["unit"] = e["unit"]
                    c2 = engine.classify([ln2], market)[0]
                    c2["row_id"] = c.get("row_id")
                    c2["remembered"] = {
                        "family": e["family"], "family_label": c2.get("family_label"),
                        "unit": e["unit"] if fill_unit else "",
                        "last": e.get("last"), "times": e.get("times")}
                    c = c2
        best, sugg = None, None
        if history_ready and str(ln.get("description") or "").strip():
            unit = c.get("unit") or ""
            cands = history_candidates(ln.get("description"), unit)
            best = best_match_score(cands)
            a = assess(ln, c, best)
            if a["low"]:
                sugg = suggestions(cands, unit)
        else:
            a = assess(ln, c, None)
        a["suggestions"] = sugg
        a["suggestions_pending"] = bool(a["low"] and not history_ready)
        c["intake"] = a
        out.append(c)
    return out
