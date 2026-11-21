"""The UI's only pricing path: the production estimator, src/estimate_low_bid.py.

    engine = ProductionEngine(); engine.load()        # once, at startup
    engine.classify([{row_id, description, unit, family?}])
    engine.estimate({market, n_bidders | "auto", owner, work_type, bid_date,
                     items: [{row_id, item_no, description, unit, quantity,
                              family?, amount?, quote?}]})

The estimator is imported through ui/upstream.py and called exactly as its
own entry point, run_schedule(), calls it: each line gets description,
desc_norm, family, unit, quantity, size_bucket, size_in, `amount` for
owner-set money and `quote` for a vendor quote; then

    tab = Tables(); tab.attach_v2f1f2()                      # once
    r = estimate(tab, items, n_bidders, market, to_year,
                 target={"date", "owner", "work_type"})

Nothing here changes a switch, a constant or a fitted number of the
estimator. Everything below turns its result into JSON the front end can draw
(and into English: a few labels in src are bilingual).

Line classification is the parser's own (upstream.classify_line). The user
may override a line's family; the server re-classifies every line from the
raw text on every request, so what is priced is always what the parser says
plus the user's explicit overrides.

Reference data: the estimator's reference tables and comparables pools use
public (source_set == "external") lettings only. This module reads
data/clean read-only and adds nothing from any other source.

ui/pricing.py, the UI's own P10/P50/P90 engine of 2026-11-18, is superseded:
it stays on disk and nothing imports it.
"""

from __future__ import annotations

import bisect
import collections
import csv
import datetime as _dt
import json
import logging
import math
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import lettings
import upstream

log = logging.getLogger("ui.engine")

UI_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(UI_DIR)
CLEAN = os.path.join(ROOT, "data", "clean")
ITEM_ACCURACY_FILE = os.path.join(UI_DIR, "item_accuracy.json")

MARKETS = ("municipal", "txdot")
MARKET_LABELS = {"municipal": "City / County (municipal)", "txdot": "TxDOT"}

#: Words the client may not send. The OPCC / engineer's estimate is a
#: backtest baseline, never an input (app.py answers 400).
FORBIDDEN_INPUTS = ("opcc", "opcc_total", "engineer_estimate",
                    "engineers_estimate", "ee", "our_estimate",
                    "target_price", "budget")

#: The forward accuracy quoted in the result's context line. Source: the
#: project README, "Where it stands (2026-11-20)", rows "City / municipal
#: (DFW), 2024-26" and "TxDOT (DFW counties), 2023-26" (bidder count as
#: bid). Update these together with that table whenever the main session
#: adopts an estimator change and re-scores.
MARKET_ACCURACY = {
    "municipal": {"label": "City / municipal (DFW) lettings, 2024-26",
                  "lettings": 224, "p50_median_error": 0.202,
                  "p80_under": 0.143, "median_overshoot": 0.458,
                  "as_of": "2026-11-21 (parser .33)"},
    "txdot": {"label": "TxDOT (DFW counties) lettings, 2023-26",
              "lettings": 807, "p50_median_error": 0.16,
              "p80_under": 0.16, "median_overshoot": 0.315,
              "as_of": "2026-11-21 (parser .33)"},
}

#: How often an OPCC set at m x P50 came in under the actual low bid, forward
#: in time (the share of scored lettings with m x P50 < low), m = 0.80 .. 2.00.
#: The page's contingency guide reads it. Source: the forward run behind
#: MARKET_ACCURACY (handoff/scratchpad/contingency/under_curve.py); regenerate it
#: with MARKET_ACCURACY after every data build.
UNDER_CURVE = {
    "municipal": [[0.8, 0.7009], [0.85, 0.6339], [0.9, 0.5759], [0.95, 0.5134], [1.0, 0.4375], [1.05, 0.3616], [1.1, 0.3036], [1.15, 0.2366], [1.2, 0.1875], [1.25, 0.1652], [1.3, 0.1384], [1.35, 0.1384], [1.4, 0.1116], [1.45, 0.0982], [1.5, 0.0759], [1.55, 0.058], [1.6, 0.0536], [1.65, 0.0536], [1.7, 0.0491], [1.75, 0.0491], [1.8, 0.0491], [1.85, 0.0491], [1.9, 0.0491], [1.95, 0.0491], [2.0, 0.0402]],
    "txdot": [[0.8, 0.7955], [0.85, 0.7261], [0.9, 0.6456], [0.95, 0.5725], [1.0, 0.4944], [1.05, 0.4362], [1.1, 0.3482], [1.15, 0.2639], [1.2, 0.2032], [1.25, 0.1512], [1.3, 0.1301], [1.35, 0.109], [1.4, 0.0805], [1.45, 0.0669], [1.5, 0.057], [1.55, 0.0496], [1.6, 0.0421], [1.65, 0.0372], [1.7, 0.0322], [1.75, 0.0285], [1.8, 0.0235], [1.85, 0.0198], [1.9, 0.0198], [1.95, 0.0186], [2.0, 0.0173]],
}
for _mk, _c in UNDER_CURVE.items():
    MARKET_ACCURACY[_mk]["under_curve"] = _c

COVERAGE_LABELS = {
    "pump_equipment": "Pumps / pump, lift or booster station",
    "generator_electrical_gear": "Generator / electrical gear",
    "prefab_or_pedestrian_bridge": "Prefab or pedestrian bridge",
    "tank_or_process": "Tank / process equipment",
    "building_or_structure": "Specialty building",
    "synthetic_turf_or_play": "Synthetic turf / play equipment",
    "meter_vault": "Meter vault",
}
LOWCONF_LABELS = {
    "mobilization": "Mobilization",
    "traffic_control": "Traffic control (lump sum)",
    "site_prep": "Site / right-of-way preparation (lump sum)",
    "swppp_erosion": "SWPPP / erosion control (lump sum)",
    "bonds_insurance": "Bonds / insurance (lump sum)",
}
WORK_TYPE_LABELS = {
    "pavement": "Pavement", "drainage": "Drainage",
    "erosion": "Creek erosion / streambank", "utility": "Water / sewer",
    "sidewalk": "Sidewalk / trail", "traffic": "Traffic",
    "building_other": "Building / other structure",
    "materials_supply": "Materials supply", "other": "Other",
}
# Readable owner names for ids the generic title-casing gets wrong.
OWNER_NAMES = {
    "carrolltontx": "Carrollton", "cityofkeller": "Keller",
    "cityoflewisville": "Lewisville", "farmersbranch": "Farmers Branch",
    "nrhtx": "North Richland Hills", "mckinney": "McKinney",
    "fortworth": "Fort Worth", "littleelm": "Little Elm", "desoto": "DeSoto",
    "trwd": "Tarrant Regional Water District", "kaufman_city": "Kaufman",
}
ESTIMATOR_FILES = ("estimate_low_bid.py", "comparables.py", "coverage.py",
                   "item_tokens.py", "qty_shape.py", "parse_bidtabs.py")
MAX_SIMILAR = 4      # similar past projects shown under an estimate
LOWCONF_GUIDE = ("A guide, not a band: in the back-tests the low bidder's own "
                 "amount fell inside this range in about a third of past jobs.")

_CJK = re.compile("[%s-%s%s-%s]" % (chr(0x3400), chr(0x9FFF), chr(0xFF00),
                                         chr(0xFFEF)))   # CJK + full-width forms


#: Safety caps on what one request may carry. A description is cut to
#: MAX_DESC_CHARS for classifying and pricing (the classifier is quadratic in
#: its length; real bid-form wordings are a few hundred characters at most).
MAX_DESC_CHARS = 1000
MAX_LINES = 2000


class EngineNotReady(RuntimeError):
    """The tables are still loading (or failed to)."""


class BadRequest(ValueError):
    """The request is malformed or not allowed; the API answers 400."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _iso_date(s):
    """'YYYY-MM-DD' or None."""
    s = str(s or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _mtime(path):
    try:
        return _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
            "%Y-%m-%d %H:%M")
    except OSError:
        return None


def rss_mb():
    """(current RSS, peak RSS) of this process in MB."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = peak / 1e6 if sys.platform == "darwin" else peak / 1e3
    cur = None
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5)
        cur = int(out.stdout.strip()) / 1e3
    except Exception:  # noqa: BLE001
        pass
    return cur, peak


def owner_label(oid):
    oid = str(oid or "")
    if oid.startswith("TX:"):
        c = oid[3:].strip().title()
        return "TxDOT, %s County" % c if c else "TxDOT"
    legi = oid.startswith("legistar/")
    slug = oid.split("/", 1)[1] if legi else oid
    name = OWNER_NAMES.get(slug) or slug.replace("_", " ").title()
    return name + (" (Legistar)" if legi else "")


def owner_name(oid):
    """The owner's display name, one per city: "legistar/coppell" and
    "coppell" are both "Coppell" (two document sources of one owner)."""
    return owner_label(oid).replace(" (Legistar)", "")


def _norm_name(x):
    return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())


def work_type_label(w):
    w = str(w or "")
    if not w:
        return "(not given)"
    return WORK_TYPE_LABELS.get(w) or w.replace("_", " ").title()


# Display names for families the README's accuracy tables do not list.
FAMILY_LABELS_EXTRA = {
    "pavers": "Pavers", "rock_anchor": "Rock / soil anchors",
    "gabion_wall": "Gabion walls / baskets", "surveying": "Surveying",
    "painting": "Painting", "needs_review": "Unclassified (needs review)",
    "gabion_mattress": "Gabion mattress", "manhole_extra_depth":
    "Manhole extra depth", "masonry_fascia": "Masonry / fascia",
    "video_survey": "Video (TV) inspection", "grading": "Grading",
    "bonds_insurance": "Bonds and insurance", "pipe_bursting": "Pipe bursting",
    "sawcut": "Saw cutting", "trm_temporary": "Temporary erosion mat",
    "dewatering": "Dewatering", "trm": "Turf reinforcement mat (TRM)",
    "trm_bundled": "TRM with seeding (bundled)",
    "trm_permanent": "Permanent TRM", "acb_mat": "Articulated concrete block",
    "arv_system": "Air release valve assembly",
}


def family_label(fam):
    """A family's display name: the README's group label (without its
    "(unit-priced)" note, which belongs to the accuracy group), else the
    extra names above, else the id."""
    import build_item_accuracy as bia  # the README's labels
    lab = bia.LABELS.get(fam) or FAMILY_LABELS_EXTRA.get(fam)
    if lab:
        return lab.replace(" (unit-priced)", "")
    s = str(fam or "").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else "(none)"


def project_title(pk, market):
    """A readable name for a letting key (the public file name it came
    from)."""
    pk = str(pk or "")
    if market == "txdot":
        tail = pk.split("::")[-1]
        return "TxDOT letting " + re.sub(r"\.txt$", "", tail, flags=re.I)
    base = pk.split("::")[0].rsplit("/", 1)[-1]
    base = re.sub(r"(\.(pdf|xlsx|xlsm|xls|csv|txt|zip))+$", "", base,
                  flags=re.I)
    base = re.sub(r"^\d{4}-\d{2}(-\d{2})?_", "", base)
    return re.sub(r"\s+", " ", base.replace("_", " ")).strip() or pk


def _E():
    if not upstream.ESTIMATOR_AVAILABLE:
        raise EngineNotReady("src/estimate_low_bid.py did not import: %s"
                             % upstream.ESTIMATOR_ERROR)
    return upstream.estimator


def english(s):
    """An estimator message with the estimator's own display labels
    (COVERAGE_STATUS, LOWCONF_LABEL, LOWCONF_CLASSES -- bilingual in src
    until they are translated there) replaced by the UI's English wording.

    The labels are read from the module at call time, never hard-coded, so
    this keeps working whatever src calls them; everything the UI decides
    on is keyed on dict keys and class ids, never on this text. A user's own
    description passes through as typed. The test suite asserts no CJK text
    is left on English input."""
    E = upstream.estimator
    s = str(s)
    if E is None:
        return s
    pairs = [(getattr(E, "COVERAGE_STATUS", None),
              "NOT COVERED -- price separately (vendor quote)"),
             (getattr(E, "LOWCONF_LABEL", None), "Low confidence")]
    for item in getattr(E, "LOWCONF_CLASSES", ()) or ():
        try:
            cid, lab = item
        except (TypeError, ValueError):
            continue
        pairs.append((lab, LOWCONF_LABELS.get(cid, cid)))
    for old, new in pairs:
        if old and isinstance(old, str) and old != new:
            s = s.replace(old, new)
    return s


_CODE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def plain_codes(s):
    """An estimator note with the internal ids it names -- not-covered
    classes, item types, lump-sum classes ("(pump_equipment)") -- replaced
    by their labels ("(Pumps / pump, lift or booster station)"). Only known
    ids are replaced; other words are left as written."""
    import build_item_accuracy as bia

    def sub(m):
        t = m.group(0)
        if t in COVERAGE_LABELS:
            return COVERAGE_LABELS[t]
        if t in LOWCONF_LABELS:
            return LOWCONF_LABELS[t]
        if t in bia.LABELS or t in FAMILY_LABELS_EXTRA:
            return family_label(t)
        return t
    return _CODE.sub(sub, str(s))


#: The estimator's own reasons for an unpriced line, in plain words.
_WHY_PLAIN = {
    "no price_reference entry": "no earlier bids for this item type in this unit",
    "no reference cell": "no earlier bids for this item type in this unit",
}


def plain_why(s):
    """An estimator's "why" for the page: known codes in plain words, the
    rest as english() gives it."""
    s = english(s)
    return _WHY_PLAIN.get(s.strip(), s)


def has_cjk(obj):
    """True when any string inside obj carries CJK text (for the tests)."""
    if isinstance(obj, str):
        return bool(_CJK.search(obj))
    if isinstance(obj, dict):
        return any(has_cjk(k) or has_cjk(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(has_cjk(v) for v in obj)
    return False


# ---------------------------------------------------------------------------
# item accuracy (ui/item_accuracy.json, built by ui/build_item_accuracy.py)
# ---------------------------------------------------------------------------

def accuracy_group(family, unit, desc_norm):
    """The README's item group of a line: mobilization, a general-condition
    lump sum (LS:<class>) or the family -- estimate_low_bid.lowconf_class
    decides, as the line-accuracy generator does."""
    E = upstream.estimator
    c = E.lowconf_class(family, unit, desc_norm) if E is not None else None
    if c == "mobilization":
        return "mobilization"
    if c:
        return "LS:" + c
    return family or "(none)"


#: Owner-set money by its words: estimate_low_bid.allowance_like() reads
#: allowances, contingencies, force accounts and cash allowances (plus the
#: contingency family and ALLOW-type units); the UI also counts lines worded
#: as the owner's money ("Owner's reserve", "owner directed work"), but not
#: owner-FURNISHED material, which bidders still price to install.
OWNER_SET_RE = re.compile(
    r"\b(?:allowances?|contingenc\w*|force\s*accounts?|cash\s+allow\w*)\b"
    r"|\bowner(?:'?s)?\b(?!\s*-?\s*(?:furnished|supplied|provided))", re.I)
ALLOWANCE_MISSING = ("Allowance amount missing \u2014 enter it from the bid "
                     "form; a historical price is not meaningful for an "
                     "allowance")


def owner_set_like(family, unit, description):
    """A line that is very likely owner-set money (an allowance the owner
    writes into the bid form), whose amount should be entered, not
    estimated."""
    E = upstream.estimator
    if E is not None and E.allowance_like(family, unit, description or ""):
        return True
    return (family or "") == "contingency" or bool(
        OWNER_SET_RE.search(description or ""))


#: A contingency sits BELOW the subtotal (user, 2026-11-21: "first the
#: subtotal, then the contingency below it, then the total"): owner-set money
#: worded as a contingency on a lump sum (LS, an allowance unit, no unit, one
#: EA), or any owner-set line given as a share of the other lines. A
#: "contingency item" measured in SY, CY, TON ... is a bid item bidders price
#: and stays in the subtotal. The page uses the same rule (app.js
#: isContingencyResult / shareOrContingency, via `below_subtotal`).
CONTINGENCY_WORD = re.compile(r"contingenc", re.I)
LUMP_UNIT = re.compile(r"^(?:|LS|L\.?\s*S\.?|L/S|LUMP(?:\s*SUM)?|ALLOW(?:ANCE)?"
                       r"|ALW|ALLW|DOL|\$)$", re.I)


def lump_like(unit, quantity) -> bool:
    u = str(unit or "").strip()
    return bool(LUMP_UNIT.match(u)) or (
        u.upper() in ("EA", "EACH") and not ((quantity or 0) > 1))


def contingency_worded(description, unit, quantity) -> bool:
    return bool(CONTINGENCY_WORD.search(description or "")) and \
        lump_like(unit, quantity)


def split_contingency(value, fixed, share):
    """(subtotal, contingency) of a headline figure that carries contingency
    lines below its subtotal: `fixed` dollars at face value plus `share` (a
    fraction) of the subtotal, so value = subtotal x (1 + share) + fixed.
    Exact for P50 by construction (_run prices a share on the subtotal). P80
    and the ranges are multiples of P50 in the estimator, so a share stays
    that share of their subtotal; a fixed amount stays at face value and the
    subtotal is the rest."""
    if value is None:
        return None, None
    sub = (value - fixed) / (1.0 + share)
    return sub, value - sub


class ItemAccuracy:
    """Per (market, item group) forward line accuracy, for the badge."""

    GOOD, FAIR = 0.25, 0.50     # half-within thresholds for the colour

    def __init__(self, path=ITEM_ACCURACY_FILE):
        self.path = path
        self.doc = {}
        self.error = ""
        try:
            with open(path, encoding="utf-8") as fh:
                self.doc = json.load(fh)
        except Exception as e:  # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)

    def meta(self):
        d = self.doc or {}
        return {"available": bool(d.get("markets")), "error": self.error,
                "generated": d.get("generated"), "source": d.get("source"),
                "thresholds": d.get("thresholds"), "method": d.get("method"),
                "windows": {m: v.get("window")
                            for m, v in (d.get("markets") or {}).items()}}

    @staticmethod
    def weak_kind(group):
        if group == "mobilization" or group.startswith("LS:"):
            return "lump_sum"
        if group in ("other", "needs_review", "(none)", ""):
            return "unclassified"
        if group == "contingency":
            return "allowance"
        return None

    def badge(self, market, group):
        mk = (self.doc.get("markets") or {}).get(market) or {}
        allg = mk.get("all") or {}
        g = (mk.get("groups") or {}).get(group)
        weak = self.weak_kind(group)
        window = mk.get("window") or ""
        mlabel = "city / municipal" if market == "municipal" else "TxDOT"
        base = {"group": group, "weak": weak, "window": window,
                "market_half_within": allg.get("half_within")}
        if not g or not g.get("published"):
            n = g.get("lines") if g else 0
            return dict(base, tier="none", text="no item figure",
                        tooltip=("Too few lines measured for this item in "
                                 "the %s forward test (%d line(s); the "
                                 "threshold is 40 lines in 15 past jobs). "
                                 "Across all %s lines, half were within "
                                 "±%.0f%% of the median bid."
                                 % (window, n or 0, mlabel,
                                    100 * (allg.get("half_within") or 0))))
        hw = g["half_within"]
        tier = ("good" if hw <= self.GOOD else "fair" if hw <= self.FAIR
                else "poor")
        r = g.get("ratio") or 1.0
        lean = ("runs %.0f%% high" % (100 * (r - 1)) if r >= 1.005 else
                "runs %.0f%% low" % (100 * (1 - r)) if r <= 0.995 else
                "is centred")
        tip = ("%s, %s forward test (%s): half of %d lines in %d past jobs "
               "were within ±%.0f%% of the median bid; our line price "
               "%s (median ratio %.2f); %.0f%% within ±20%%. Bidders "
               "themselves: the low bidder is typically %.0f%% from the "
               "median bidder." % (
                   g.get("label") or group, mlabel, window, g["lines"],
                   g["lettings"], 100 * hw, lean, r,
                   100 * (g.get("within_20") or 0),
                   100 * (g.get("low_vs_median") or 0)))
        if weak == "lump_sum":
            tip += (" Lump sum: priced at about the same dollar amount on "
                    "every job, while bidders price it as a share of the "
                    "job -- treat this line as weak.")
        elif weak == "unclassified":
            tip += (" Unclassified line: no item type matched, so this "
                    "figure is weak.")
        elif weak == "allowance":
            tip += (" Contingency / allowance line: enter the owner's "
                    "amount so it is taken at face value.")
        return dict(base, tier=tier, half_within=hw, ratio=r,
                    lines=g["lines"], lettings=g["lettings"],
                    label=g.get("label"),
                    text="typically ±%.0f%%" % (100 * hw), tooltip=tip)


# ---------------------------------------------------------------------------
# expected bidders: rule N1 (handoff/scratchpad/diag_twostage/NHAT_RULE.md)
# ---------------------------------------------------------------------------

N1_MIN = 10
N1_DEFAULT = 4
SIZE_EDGES = (5e5, 2e6)
SIZE_LABELS = ("under $0.5M", "$0.5M-2M", "$2M and over")


def size_class(x):
    return bisect.bisect_right(SIZE_EDGES, x or 0.0)


class PanelIndex:
    """What the UI reads from master_panel.csv itself, in one pass at load,
    PUBLIC (source_set == "external") rows only:

      * first_wt      the work type of each letting's first external row
                      (rule N1's definition);
      * for every letting of the production comparables pools (the only
        lettings shown or used as history), over the rows the price pool
        keeps (estimate_low_bid._reference_pool's filters):
          totals      every bidder's total, sorted (low, 2nd low, median,
                      high of the bids);
          lines       per bid-form line (item no, description, unit,
                      quantity): the low bidder's unit price, the median
                      bidder's unit price and the number of bids;
      * meta          public project names / source links: project_metadata
                      .csv (external files only) and TxDOT's project tables.

    Nothing here is priced from; it is what the similar-projects cards and
    the bidder-count history show and read."""

    def __init__(self, tab):
        pools = tab.comparables if isinstance(tab.comparables, dict) else {}
        self.lettings = {}
        for mk in MARKETS:
            pool = pools.get(mk)
            if pool is None:
                continue
            for Lc in pool.ctx.L:
                self.lettings[Lc["pk"]] = (mk, Lc["date"], int(Lc["n"]))
        self._pass(set(self.lettings))
        # names / links for every public letting with bids (references show
        # lettings outside the comparables pools too)
        self.meta = self._metadata(set(self.totals))

    def _pass(self, keys):
        csv.field_size_limit(10 ** 8)
        first_wt = {}
        bt = collections.defaultdict(lambda: collections.defaultdict(float))
        lines = collections.defaultdict(dict)
        need = ("source_set", "project_key", "work_type",
                "is_engineer_estimate", "competition_class", "project_kind",
                "price_basis", "unit_price", "quantity", "bidder_norm",
                "bidder", "item_no", "description", "desc_norm", "unit",
                "is_low_bidder", "bid_date", "date_precision")
        dates = collections.defaultdict(set)
        precs = collections.defaultdict(set)
        wt = collections.defaultdict(float)
        rawdesc = {}
        with open(os.path.join(CLEAN, "master_panel.csv"),
                  encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader(fh)
            h = next(rd)
            ix = {k: h.index(k) for k in need}
            i_s, i_k, i_w = ix["source_set"], ix["project_key"], ix["work_type"]
            i_bd, i_dp = ix["bid_date"], ix["date_precision"]
            for row in rd:
                if row[i_s] != "external":
                    continue
                pk = row[i_k]
                if pk not in first_wt:
                    first_wt[pk] = row[i_w] or ""
                # availability dates: estimate_low_bid._letting_dates' inputs
                dates[pk].add(row[i_bd])
                precs[pk].add(row[i_dp])
                if row[ix["is_engineer_estimate"]].lower() in ("true", "1",
                                                               "yes"):
                    continue
                if row[ix["competition_class"]] != "competitive":
                    continue
                if row[ix["project_kind"]] != "construction":
                    continue
                if row[ix["price_basis"]] == "uneven_proposal":
                    continue
                u, q = _f(row[ix["unit_price"]]), _f(row[ix["quantity"]])
                if not u or u <= 0:
                    continue
                dk = (pk, (row[ix["desc_norm"]] or "").strip())
                if dk not in rawdesc:
                    rawdesc[dk] = row[ix["description"]]
                if q is None or q <= 0:
                    continue
                b = row[ix["bidder_norm"]] or row[ix["bidder"]]
                bt[pk][b] += q * u
                if row[ix["is_low_bidder"]] == "True":
                    wt[pk] += q * u
                if pk not in keys:
                    continue
                lk = (row[ix["item_no"]], row[ix["desc_norm"]],
                      row[ix["unit"]], q)
                rec = lines[pk].get(lk)
                if rec is None:
                    rec = lines[pk][lk] = [row[ix["description"]], [], None]
                rec[1].append(u)
                if row[ix["is_low_bidder"]] == "True":
                    rec[2] = u
        self.first_wt = first_wt
        E = _E()
        self.avail = {pk: E._avail_date(dates[pk], precs[pk]) for pk in dates}
        self.winner_total = dict(wt)
        self.rawdesc = rawdesc
        self.totals = {pk: sorted(v.values()) for pk, v in bt.items() if v}
        self.median_total = {pk: statistics.median(v)
                             for pk, v in self.totals.items()}
        self.lines = {}
        self.by_price = {}
        for pk, d in lines.items():
            out, idx = [], collections.defaultdict(list)
            for (ino, dn, un, q), (desc, ps, low) in d.items():
                out.append((ino, desc, dn, un, q, low,
                            statistics.median(ps), len(ps)))
                if low is not None:
                    idx[(un, q, low)].append(len(out) - 1)
            self.lines[pk] = out
            self.by_price[pk] = dict(idx)

    @staticmethod
    def _metadata(keys):
        """Public names and source links, for the pool's lettings only."""
        meta = {}
        try:
            with open(os.path.join(CLEAN, "project_metadata.csv"),
                      encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    sf = r.get("src_file") or ""
                    if not sf.startswith("external/"):
                        continue
                    meta[sf] = {"title": (r.get("project_title") or "").strip(),
                                "url": (r.get("url") or "").strip(),
                                "source_group": r.get("source_group") or ""}
        except OSError:
            pass
        out = {}
        for pk in keys:
            m = meta.get(pk.split("::")[0])
            if m:
                out[pk] = m
        for name, idcol in (("txdot_project_totals.csv", "project_id"),
                            ("txdot_archive_project_totals.csv", "src_file")):
            try:
                with open(os.path.join(CLEAN, name), encoding="utf-8-sig",
                          newline="") as fh:
                    for r in csv.DictReader(fh):
                        pk = r.get(idcol) or ""
                        if pk not in keys or pk in out:
                            continue
                        bits = [r.get("project_name") or "",
                                ("CSJ " + r["csj"]) if r.get("csj") else "",
                                (r.get("project_type") or "").title()]
                        out[pk] = {"title": "; ".join(b for b in bits if b),
                                   "url": "",
                                   "county": (r.get("county") or "").title(),
                                   "source_group": "txdot"}
            except OSError:
                pass
        return out

    def matched_line(self, pk, unit, q, u, tokens=None):
        """The bid-form line of letting `pk` behind a comparables line (the
        winner's unit, quantity and unit price; tokens break a tie)."""
        hits = self.by_price.get(pk, {}).get((unit, q, u)) or []
        if not hits:
            return None
        if len(hits) > 1 and tokens is not None:
            C = upstream.comparables
            for i in hits:
                if C.tokens(self.lines[pk][i][2]) == tokens:
                    return self.lines[pk][i]
        return self.lines[pk][hits[0]]


class BidderHistory:
    """The history rule N1 reads, rebuilt from data/clean (read-only).

    N1: n-hat = the median bidder count of earlier lettings (dated strictly
    before the bid date) of the same market x work type x size class; the
    first cell with >= 10 lettings wins: (market, work type, size), then
    (market, size), then (market), then all markets; else 4. Rounded half
    up.

    Per history letting:
      * population, date and bidder count n: the production comparables pool
        of its market -- calibrate_forward's population (one identified
        winner, >= 2 bidders, >= 3 schedule lines, a date), n = its number of
        bidders;
      * work type: the work_type of its first external master_panel row (the
        rule's own definition; '' for every TxDOT letting);
      * size: the class of its model total T + owner-set money as the forward
        pass priced it. data/clean carries those totals for TxDOT only
        (estimator_t1_txdot.csv, written by calibrate_forward). For a
        municipal letting the UI uses its median bidder total instead -- the
        level T estimates -- and says so (a requested estimator change is to
        write the municipal totals too).
    The schedule being priced takes the class of its own T + owner-set money
    (market_total + fixed_total of the estimate, which n does not enter).
    """

    def __init__(self, tab, panel):
        E = _E()
        self.notes = []
        model_t = {}
        p = os.path.join(CLEAN, E.T1_FILE)
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("kind") == "model":
                        v = _f(r.get("value"))
                        if v and v > 0:
                            model_t[r["project_key"]] = v
        except OSError:
            self.notes.append("%s absent: TxDOT history sized by median bid"
                              % E.T1_FILE)
        lets = panel.lettings
        first_wt, med = panel.first_wt, panel.median_total
        H = []
        n_proxy = 0
        for pk, (mk, D, n) in lets.items():
            if mk == "txdot" and pk in model_t:
                tot, src = model_t[pk], "model"
            else:
                tot, src = med.get(pk), "median_bid"
            if not tot or tot <= 0:
                continue
            n_proxy += src == "median_bid"
            w = (first_wt.get(pk) or "") if mk == "municipal" else ""
            H.append((D, pk, mk, n, w, size_class(tot)))
        H.sort()
        self.H = H
        self.dates = [h[0] for h in H]
        self.n_proxy = n_proxy
        self.by_market = collections.Counter(h[2] for h in H)

    def suggest(self, market, work_type, total, date):
        """n-hat for a schedule of `market` / `work_type` whose own model
        total (T + owner-set money) is `total`, bid on `date`."""
        D = _iso_date(date) or _dt.date.today().isoformat()
        k = bisect.bisect_left(self.dates, D)
        prior = self.H[:k]
        w = (work_type or "") if market == "municipal" else ""
        s = size_class(total)
        pm = [h for h in prior if h[2] == market]
        mlab = "city / county" if market == "municipal" else "TxDOT"
        cells = [
            ("market, work type, size",
             "%s jobs with work type '%s' and size %s"
             % (mlab, work_type_label(w) if w else "(blank)", SIZE_LABELS[s]),
             [h[3] for h in pm if h[4] == w and h[5] == s]),
            ("market, size", "%s jobs of size %s" % (mlab, SIZE_LABELS[s]),
             [h[3] for h in pm if h[5] == s]),
            ("market", "%s jobs of any size" % mlab, [h[3] for h in pm]),
            ("all markets", "jobs of both markets",
             [h[3] for h in prior]),
        ]
        for key, label, v in cells:
            if len(v) >= N1_MIN:
                m = statistics.median(v)
                return {"n": int(math.floor(m + 0.5)), "rule": "N1",
                        "cell": key, "cell_label": label,
                        "history_lettings": len(v), "median": m,
                        "before": D, "size_total": total,
                        "size_class": SIZE_LABELS[s],
                        "work_type": w,
                        "note": self.basis_note(market)}
        return {"n": N1_DEFAULT, "rule": "N1", "cell": "default",
                "cell_label": "fewer than %d similar earlier jobs" % N1_MIN,
                "history_lettings": 0, "median": None, "before": D,
                "size_total": total, "size_class": SIZE_LABELS[s],
                "work_type": w, "note": self.basis_note(market)}

    @staticmethod
    def basis_note(market):
        if market == "municipal":
            return ("History sized by each earlier letting's median bidder "
                    "total (the forward model totals the rule names are not "
                    "in data/clean for municipal lettings).")
        return ("History sized by each earlier letting's forward model "
                "total (estimator_t1_txdot.csv).")


class ReferenceIndex:
    """The evidence behind every line price: the rows of the published
    reference cells, rebuilt from the production price pool.

    estimate_low_bid._reference_pool() returns the rows every published
    reference table (price_reference*.csv) is built from -- public
    (external), competitive, construction rows only -- and
    _reference_cells() groups them into the three tables' cells:

      fam   (family_key, unit, size_bucket)
      band  (family_key, unit, size_bucket, quantity band)
      tok   (item word set, unit, quantity band)

    with one entry per (letting, year): the median unit price of every bid
    in the cell (the letting's "median bid"), the median quantity, the year
    and the owner; a cell's price is the median of those entries. This index
    keeps the pool rows with their three keys precomputed by the estimator's
    own functions (family_key, item_tokens.tokens, qty_shape.band_of), so
    the cell behind any line can be read back in milliseconds."""

    def __init__(self):
        E = _E()
        t0 = time.time()
        pool = E._reference_pool()
        self.rows = pool
        n = len(pool)
        self.fk = [None] * n
        self.bd = [None] * n
        self.tok = [None] * n
        fkc, tokc = {}, {}
        by_unit = collections.defaultdict(list)
        band_of = E.qty_shape.band_of
        tokens = E.item_tokens.tokens
        for i, (mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr) in \
                enumerate(pool):
            if not un:
                continue                     # as _reference_cells skips them
            k = (fam, dn)
            f = fkc.get(k)
            if f is None:
                f = fkc[k] = E.family_key(fam, dn)
            self.fk[i] = f
            self.bd[i] = band_of(q) if q is not None else ""
            t = tokc.get(dn)
            if t is None:
                t = tokc[dn] = tokens(dn)
            self.tok[i] = t
            by_unit[un].append(i)
        self.by_unit = dict(by_unit)
        self.keys = {r[1] for r in pool}
        #: the unit hint's evidence (set by the engine: it needs the
        #: lettings' availability dates)
        self.unit_evidence = None
        self.load_s = time.time() - t0

    def members(self, kind, key):
        """Row indices of one cell."""
        if kind == "tok":
            t, un, bd = key
            return [i for i in self.by_unit.get(un, ())
                    if self.tok[i] == t and self.bd[i] == bd]
        if kind == "band":
            fk, un, sz, bd = key
            return [i for i in self.by_unit.get(un, ())
                    if self.fk[i] == fk and self.rows[i][4] == sz
                    and self.bd[i] == bd]
        fk, un, sz = key
        return [i for i in self.by_unit.get(un, ())
                if self.fk[i] == fk and self.rows[i][4] == sz]

    def entries(self, idx):
        """_reference_cells' entries of a cell: per (letting, year) the
        median unit price and quantity, plus what the page shows (the low
        bidder's unit price, the range, every bid, the item as written)."""
        g = collections.OrderedDict()
        for i in idx:
            mk, pk, fam, un, sz, q, u, w, b, dn, ow, yr = self.rows[i]
            e = g.get((pk, yr))
            if e is None:
                e = g[(pk, yr)] = {"key": pk, "market": mk, "year": yr,
                                   "owner_raw": ow, "us": [], "qs": [],
                                   "low": [], "bids": [], "dn": dn,
                                   "unit": un, "lines": set()}
            e["lines"].add((dn, q))
            e["us"].append(u)
            if q is not None:
                e["qs"].append(q)
            if w:
                e["low"].append(u)
            e["bids"].append((b, u, q, w))
        out = []
        for e in g.values():
            e["median_unit"] = statistics.median(e["us"])
            e["quantity"] = statistics.median(e["qs"]) if e["qs"] else None
            e["low_unit"] = statistics.median(e["low"]) if e["low"] else None
            e["min_unit"], e["max_unit"] = min(e["us"]), max(e["us"])
            out.append(e)
        return out


def _cell_label(kind, key):
    if kind == "tok":
        return ("item words {%s}, %s, quantity band %s"
                % (", ".join(sorted(w for w in key[0] if w != "zzremoval")),
                   key[1], key[2]))
    fk = key[0]
    rm = " (removal only)" if fk.endswith("#rm") else ""
    fam = family_label(fk.split("#")[0]) + rm
    size = ("size %s" % key[2]) if key[2] else "any size"
    if kind == "band":
        return "family %s, %s, %s, quantity band %s" % (fam, key[1], size,
                                                       key[3])
    return "family %s, %s, %s, any quantity" % (fam, key[1], size)


# ---------------------------------------------------------------------------
# unit hint (batch 3, 2026-11-20): presentation only, never an input
# ---------------------------------------------------------------------------
# The user (22:25): takeoffs come in CY or CF, SY or SF or AC, TON or LB.
# Pooling units and converting automatically were tried and NOT adopted
# (reports/unit_conversion_2026-11-20.md, U3: a rarer unit often marks a
# different product), so the conversion is offered as a reference the
# estimator chooses. The logic is U3's (handoff/scratchpad/p50fix/units/
# patch_u3/unit_fallback.diff: UnitEvidence, unit_factor, the target rule),
# reused here; src/ is not changed and P50 / P80 never see it.

UNIT_HINT_MIN = 5                  # a family cell is usable with 5 lettings
#: the results alert "thin_heavy_lines": a priced line with at least this
#: share of the expected low bid, backed by fewer earlier projects than this
THIN_HEAVY_SHARE = 0.15
THIN_HEAVY_MAX_PROJECTS = 3
UNIT_CANONICAL = {"length": "LF", "area": "SY", "volume": "CY",
                  "weight": "TON", "liquid": "GAL"}
#: unit -> (dimension, num, den): quantity in the dimension's canonical unit
#: = quantity x num / den
UNIT_CONVERSIONS = {
    "LF": ("length", 1, 1), "FT": ("length", 1, 1),
    "SY": ("area", 1, 1), "SF": ("area", 1, 9), "AC": ("area", 4840, 1),
    "CY": ("volume", 1, 1), "CF": ("volume", 1, 27),
    "TON": ("weight", 1, 1), "LB": ("weight", 1, 2000),
    "CWT": ("weight", 1, 20),
    "GAL": ("liquid", 1, 1), "MG": ("liquid", 1000, 1),
    "TGL": ("liquid", 1000, 1)}
#: never converted (outside the table): counts, lump sums, allowances, time,
#: vertical feet, stations, miles, a bare SQ (TxDOT's is per SY, a roofing
#: square is 100 SF) or YD
UNIT_NEVER_CONVERT = (
    "EA", "EACH", "LS", "L.S.", "LUMP SUM", "ALLOW", "ALLOWANCE", "ALW",
    "ALLW", "DOL", "LOT", "MO", "MONTH", "WK", "WEEK", "DAY", "HR", "HOUR",
    "YR", "CYC", "VF", "VLF", "STA", "MI", "LMI", "SQ", "YD", "YARD")
UNIT_FAMILIES_EXCLUDED = ("other", "needs_review", "")


def unit_spelling(unit):
    """A unit as UNIT_CONVERSIONS keys it: upper case, no dots or spaces."""
    return re.sub(r"[.\s]", "", str(unit or "").upper())


def unit_factor(unit, to_unit):
    """(num, den): quantity in `to_unit` = quantity in `unit` x num / den,
    for two units of one dimension in UNIT_CONVERSIONS; else None."""
    a = UNIT_CONVERSIONS.get(unit_spelling(unit))
    b = UNIT_CONVERSIONS.get(unit_spelling(to_unit))
    if a is None or b is None or a[0] != b[0]:
        return None
    return a[1] * b[2], a[2] * b[1]


def _canon_qty(q, num, den):
    """A converted quantity to 12 significant digits (999.9999999999 is
    1000), so a quantity band's edge does not move with the unit."""
    return float("%.12g" % (q * num / den))


class UnitEvidence:
    """Per (family key, unit): the availability dates of the distinct public
    lettings with price-pool rows of that family key in that unit (any size
    bucket). n(fk, unit, D) counts those available strictly before D."""

    def __init__(self, rows, fks, avail):
        lets = collections.defaultdict(set)
        for r, fk in zip(rows, fks):
            fam, un, pk = r[2], r[3], r[1]
            if not un or fk is None or (fam or "") in UNIT_FAMILIES_EXCLUDED:
                continue
            sp = unit_spelling(un)
            if sp in UNIT_CONVERSIONS:
                lets[(fk, sp)].add(pk)
        never = getattr(_E(), "NEVER", "9999-12-31")
        self.dates = {k: sorted(avail.get(pk) or never for pk in v)
                      for k, v in lets.items()}
        self.units = collections.defaultdict(set)   # (fk, dimension) -> units
        for fk, sp in self.dates:
            self.units[(fk, UNIT_CONVERSIONS[sp][0])].add(sp)

    def n(self, fk, unit, D=None):
        d = self.dates.get((fk, unit_spelling(unit)))
        if not d:
            return 0
        return len(d) if D is None else bisect.bisect_left(d, D)


def unit_hint_target(ev, family, unit, description, D, unpriced=False):
    """(target unit, num, den, own lettings, target lettings) when a line
    should be offered a converted price: its own unit has fewer than
    UNIT_HINT_MIN earlier lettings (or the line is unpriced) and a unit of
    the same dimension has at least UNIT_HINT_MIN; else None. The target is
    the same-dimension unit with the most lettings (ties: the canonical
    unit, then alphabetical) -- U3's rule."""
    if ev is None or (family or "") in UNIT_FAMILIES_EXCLUDED:
        return None
    sp = unit_spelling(unit)
    c = UNIT_CONVERSIONS.get(sp)
    if c is None:
        return None
    fk = _E().family_key(family, description or "")
    own = ev.n(fk, sp, D)
    if own >= UNIT_HINT_MIN and not unpriced:
        return None
    best = None
    for v in ev.units.get((fk, c[0]), ()):
        if v == sp:
            continue
        k = (-ev.n(fk, v, D), v != UNIT_CANONICAL[c[0]], v)
        if best is None or k < best:
            best = k
    if best is None or -best[0] < UNIT_HINT_MIN:
        return None
    v = best[2]
    num, den = unit_factor(sp, v)
    return v, num, den, own, -best[0]


def _lettings(n):
    return "%d earlier letting%s" % (n, "" if n == 1 else "s")


def _rate(v):
    """A unit price for a sentence: $0.97, $26.13, $1,234."""
    if v is None:
        return "?"
    return ("${:,.0f}".format(v) if abs(v) >= 1000 else
            "${:,.2f}".format(v) if abs(v) >= 0.1 else "${:,.4f}".format(v))


def unit_evidence_text(unit, target, own, n_target, unpriced=False):
    """"Only 1 earlier letting prices this in CF; 570 price it in CY." """
    if unpriced and own >= UNIT_HINT_MIN:
        head = ("This line found no price in %s (%s); %d price it in %s."
                % (unit, _lettings(own), n_target, target))
    elif own == 0:
        head = ("No earlier letting prices this in %s; %d price it in %s."
                % (unit, n_target, target))
    else:
        head = ("Only %s price%s this in %s; %d price it in %s."
                % (_lettings(own), "s" if own == 1 else "", unit, n_target,
                   target))
    return head


# ---------------------------------------------------------------------------
# a printed cap on mobilization (batch 3; reports/p50_fix_round14.md)
# ---------------------------------------------------------------------------
# A mobilization line that prints a cap ("5% Max", "Max 5% of Total Bid",
# "not to exceed 5%", "(5% max)") is priced by bidders at about 0.95 x the
# cap, while the model's price runs 3.9x on jobs under $2M and 0.38x on
# larger ones. The page offers the cap as a quick pick, computed live
# from the user's other amounts. Read from the line's description as
# written (desc_norm drops "%").
_PCT = r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:%|percent\b|pct\b)"
_CAP_RES = (
    re.compile(_PCT + r"\s*\)?\s*(?:max(?:imum)?\b\.?|cap\b)", re.I),
    re.compile(r"\b(?:max(?:imum)?\.?|not\s+to\s+exceed|nte|n\.t\.e\.?|up\s+to|"
               r"no\s+more\s+than|limited\s+to|cap(?:ped)?\s+at)\s*(?:of\s+)?\(?\s*"
               + _PCT, re.I),
)
_CAP_REST = re.compile(r"\bof\s+(?:the\s+)?(?:rest|remaining|remainder|"
                       r"(?:all\s+)?other\s+(?:bid\s+)?items)\b", re.I)


def printed_cap(description):
    """{"pct": 0.05, "basis": "total" | "rest", "text": "5%"} when the line
    prints a cap, else None. "of the rest" (or the remaining / other items)
    makes the cap a share of the other items; otherwise it is a share of the
    total bid / contract."""
    s = str(description or "")
    for rx in _CAP_RES:
        m = rx.search(s)
        if m:
            pct = float(m.group(1))
            if not 0 < pct < 50:
                return None
            return {"pct": pct / 100.0,
                    "basis": "rest" if _CAP_REST.search(s) else "total",
                    "text": ("%g%%" % pct)}
    return None


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class ProductionEngine:
    """Tables loaded once and reused; every estimate goes through
    estimate_low_bid.estimate()."""

    def __init__(self):
        self._lock = threading.RLock()
        self.tab = None
        self.panel = None
        self.history = None
        self.refs = None
        self.refs_state = {"ready": False, "loading": False, "error": "",
                           "load_s": None}
        self.accuracy = ItemAccuracy()
        #: public source pages and local copies of the lettings shown
        self.sources = lettings.SourceLinks()
        #: full bid tabulations, read from master_panel on demand
        self.tabs = lettings.LettingTabs()
        self.state = {"ready": False, "loading": False, "error": "",
                      "step": "", "started": None, "load_s": None,
                      "history_s": None, "rss_mb": None, "peak_rss_mb": None,
                      "rss_before_mb": None}
        self.info: Dict[str, Any] = {}
        self._owners: Dict[str, list] = {}
        self._work_types: Dict[str, list] = {}

    # -- loading ------------------------------------------------------------
    def load(self):
        """Read the production tables once (Tables + attach_v2f1f2) and the
        expected-bidder history. Safe to call from a background thread."""
        with self._lock:
            if self.state["ready"] or self.state["loading"]:
                return self
            self.state.update(loading=True, error="", started=time.time())
        try:
            E = _E()
            rss0, _ = rss_mb()
            self.state.update(step="reference tables", rss_before_mb=rss0)
            t0 = time.time()
            tab = E.Tables()
            if tab.missing:
                raise RuntimeError("estimator inputs missing from data/clean: "
                                   + ", ".join(tab.missing))
            self.state["step"] = ("comparables pools, size cells, T1 and "
                                  "low-confidence history (reads "
                                  "master_panel.csv)")
            tab.attach_v2f1f2()
            load_s = time.time() - t0
            self.state["step"] = ("bid totals, line prices and bidder-count "
                                  "history (public lettings)")
            t1 = time.time()
            panel = PanelIndex(tab)
            hist = BidderHistory(tab, panel)
            hist_s = time.time() - t1
            cur, peak = rss_mb()
            with self._lock:
                self.tab = tab
                self.panel = panel
                self.history = hist
                self._build_lists()
                self.info = self._describe(tab, hist)
                self.state.update(ready=True, loading=False, step="ready",
                                  load_s=round(load_s, 2),
                                  history_s=round(hist_s, 2),
                                  rss_mb=cur and round(cur),
                                  peak_rss_mb=round(peak))
            log.info("production estimator ready: tables %.1fs, history "
                     "%.1fs, RSS %s MB (peak %.0f MB)", load_s, hist_s,
                     cur and round(cur), peak)
        except Exception as e:  # noqa: BLE001
            log.exception("production estimator failed to load")
            with self._lock:
                self.state.update(ready=False, loading=False,
                                  error="%s: %s" % (type(e).__name__, e))
        return self

    def require(self):
        if not self.state["ready"]:
            raise EngineNotReady(self.state["error"] or
                                 "the estimator is still loading (%s)"
                                 % (self.state["step"] or "starting"))
        return _E()

    def _build_lists(self):
        pools = self.tab.comparables or {}
        for mk in MARKETS:
            pool = pools.get(mk)
            L = pool.ctx.L if pool is not None else []
            oc = collections.Counter(Lc["owner"] for Lc in L if Lc["owner"])
            # one entry per owner: ids of the same city merged under its
            # name; the estimator gets the id with the most history, so the
            # same-owner weighting still finds it
            groups = collections.defaultdict(list)
            for o, n in oc.items():
                groups[_norm_name(owner_name(o))].append((n, o))
            entries = []
            for g in groups.values():
                g.sort(key=lambda x: (-x[0], x[1]))
                entries.append({"id": g[0][1], "label": owner_name(g[0][1]),
                                "lettings": sum(n for n, _ in g),
                                "ids": sorted(o for _, o in g)})
            self._owners[mk] = sorted(entries,
                                      key=lambda x: (x["label"].lower(),
                                                     x["id"]))
            wc = collections.Counter(Lc["wtype"] for Lc in L)
            self._work_types[mk] = sorted(
                ({"id": w, "label": work_type_label(w), "lettings": n}
                 for w, n in wc.items() if w),
                key=lambda x: -x["lettings"])

    def _describe(self, tab, hist):
        E = _E()
        pools = tab.comparables or {}
        latest = max((Lc["date"] for p in pools.values() for Lc in p.ctx.L
                      if Lc["date"] and Lc["date"] < E.NEVER), default=None)
        sw = {
            "SIZE_FALLBACK": E.SIZE_FALLBACK,
            "COMPARABLES_BLEND": E.COMPARABLES_BLEND,
            "REMOVAL_IDENTITY": upstream.estimator.item_tokens.REMOVAL_IDENTITY,
            "TXDOT_C4D1": E.TXDOT_C4D1, "COVERAGE_SCOPE": E.COVERAGE_SCOPE,
            "WHOLE_FACILITY_RULE": E.WHOLE_FACILITY_RULE, "LOWCONF": E.LOWCONF,
            "MUNI_CELLS": E.MUNI_CELLS, "F1_PIPE_ONLY": E.F1_PIPE_ONLY,
            "FAMILY_CELL_T2": E.FAMILY_CELL_T2,
            "IDENTICAL_BID_OWNER_SET": E.IDENTICAL_BID_OWNER_SET,
            "CMP_MUNI_REMOVAL_IDENTITY":
                upstream.comparables.MUNI_REMOVAL_IDENTITY,
        }
        rule = []
        if sw["SIZE_FALLBACK"] and sw["COMPARABLES_BLEND"] \
                and sw["REMOVAL_IDENTITY"]:
            rule.append("V2+F1+F2")
        if sw["TXDOT_C4D1"]:
            rule.append("T1 on TxDOT")
        if sw["COVERAGE_SCOPE"]:
            rule.append("coverage scope")
        extra = [k for k in ("MUNI_CELLS", "F1_PIPE_ONLY", "FAMILY_CELL_T2",
                             "IDENTICAL_BID_OWNER_SET",
                             "CMP_MUNI_REMOVAL_IDENTITY") if sw[k]]
        if extra:
            rule.append("+ " + ", ".join(extra))
        md5 = {f: upstream.src_md5(f) for f in ESTIMATOR_FILES}
        calib = {}
        for mk in MARKETS + ("all",):
            c = tab.calib.get(mk) or {}
            calib[mk] = {"lettings": _f(c.get("n")),
                         "first_date": c.get("first_date"),
                         "last_date": c.get("last_date"),
                         "mode": c.get("mode"),
                         "train_scope": c.get("train_scope")}
        return {
            "estimator": {"file": "src/estimate_low_bid.py",
                          "md5": md5["estimate_low_bid.py"],
                          "md5_short": (md5["estimate_low_bid.py"] or "")[:8],
                          "rule": ", ".join(rule), "switches": sw,
                          "md5_all": md5},
            "data": {"clean_dir": CLEAN,
                     "master_panel_mtime": _mtime(
                         os.path.join(CLEAN, "master_panel.csv")),
                     "calibration_mtime": _mtime(
                         os.path.join(CLEAN, "estimator_calibration.csv")),
                     "latest_letting": latest,
                     "pool_lettings": {mk: len(p.ctx.L)
                                       for mk, p in pools.items()},
                     "calibration": calib,
                     "train_scope": "public (external) lettings only"},
            "bidder_history": {"lettings": dict(hist.by_market),
                               "sized_by_median_bid": hist.n_proxy,
                               "notes": hist.notes},
        }

    # -- lists for the front end ----------------------------------------------
    def options(self):
        E = self.require()
        tab = self.tab
        disc = {}
        for mk in MARKETS:
            table = tab.disc_muni if (mk == "municipal" and tab.disc_muni) \
                else tab.disc
            disc[mk] = {"table": "municipal" if table is tab.disc_muni
                        else "pooled",
                        "rows": [{"n_bidders": n, "f": v["f"],
                                  "p25": v["p25"], "p75": v["p75"],
                                  "lettings": v["lettings"]}
                                 for n, v in sorted(table.items())]}
        fams = sorted({k[0].split("#")[0] for k in tab.ref if k[0]})
        return {
            "markets": [{"id": m, "label": MARKET_LABELS[m]} for m in MARKETS],
            "owners": self._owners, "work_types": self._work_types,
            "families": [{"id": f, "label": family_label(f)} for f in fams],
            "units": sorted(upstream.KNOWN_UNITS),
            "discount": disc,
            "max_bin_distance": E.MAX_BIN_DISTANCE,
            "market_accuracy": MARKET_ACCURACY,
            "item_accuracy": self.accuracy.meta(),
            "coverage_labels": COVERAGE_LABELS,
            "lowconf_labels": LOWCONF_LABELS,
            "today": _dt.date.today().isoformat(),
        }

    # -- classification -------------------------------------------------------
    def classify_one(self, description, unit_raw, family_override="",
                     market="municipal"):
        """The parser's classification of one line (plus the user's family
        override), and what the estimator will make of it."""
        E = _E()
        description = str(description or "")[:MAX_DESC_CHARS]
        c = upstream.classify_line(description, unit_raw)
        fam_parser = c["family"]
        fo = str(family_override or "").strip()
        fam = fo or fam_parser
        if fam != fam_parser:
            c["size_in"], c["size_bucket"] = upstream.resize(
                description, c["unit"], fam)
        c["family"] = fam
        c["family_parser"] = fam_parser
        c["family_parser_label"] = family_label(fam_parser) if fam_parser else ""
        c["family_overridden"] = bool(fo) and fo != fam_parser
        c["family_label"] = family_label(fam)
        dn, unit = c["desc_norm"], c["unit"]
        cov = E.line_coverage(dn, fam, unit)
        c["coverage_class"] = cov
        c["coverage_label"] = COVERAGE_LABELS.get(cov, cov) if cov else None
        lc = E.lowconf_class(fam, unit, dn)
        c["lowconf_class"] = lc
        c["lowconf_label"] = LOWCONF_LABELS.get(lc) if lc else None
        c["allowance_like"] = bool(E.allowance_like(fam, unit,
                                                    str(description or "")))
        c["owner_set_like"] = owner_set_like(fam, unit, str(description or ""))
        g = accuracy_group(fam, unit, dn)
        c["accuracy_group"] = g
        c["accuracy"] = self.accuracy.badge(market, g)
        issues = []
        if not str(description or "").strip():
            issues.append("no description")
        if not unit:
            issues.append("no unit: the line cannot be priced")
        elif not c["unit_known"]:
            issues.append("unit %r is not one the parser knows" % unit)
        if fam in ("other", "needs_review", ""):
            issues.append("no item type matched (unclassified)")
        c["issues"] = issues
        return c

    def classify(self, lines, market="municipal"):
        _E()
        if market not in MARKETS:          # "not sure": the default market
            market = "municipal"
        out = []
        with self._lock:
            for ln in lines or []:
                c = self.classify_one(ln.get("description"), ln.get("unit"),
                                      ln.get("family"), market)
                c["row_id"] = ln.get("row_id")
                c["import_amount"] = self.import_amount(
                    c, ln.get("file_amount"), ln.get("file_unit_price"),
                    ln.get("quantity"))
                out.append(c)
        return out

    def add_unit_evidence(self, cls, lines, bid_date=None):
        """The unit hint's evidence on classified lines (after any remembered
        choice is applied): c["unit_hint"] = {unit, target, own, n_target,
        factor, text} when the line's unit is thin in the history and a unit
        of the same dimension is not. The converted price comes with the
        reference lines (unit_hints). Nothing when the reference index is not built
        yet."""
        ev = self.refs.unit_evidence if self.refs is not None else None
        if ev is None:
            return cls
        D = bid_date if (isinstance(bid_date, str)
                         and re.fullmatch(r"\d{4}-\d{2}-\d{2}", bid_date)) \
            else _dt.date.today().isoformat()
        for c, ln in zip(cls, lines):
            h = unit_hint_target(ev, c.get("family"), c.get("unit"),
                                 str(ln.get("description") or ""), D)
            if h is not None:
                tu, num, den, own, n_t = h
                c["unit_hint"] = {
                    "unit": c.get("unit"), "target": tu, "own": own,
                    "n_target": n_t, "factor": num / den,
                    "text": unit_evidence_text(c.get("unit"), tu, own, n_t)}
        return cls

    def unit_hints(self, q, items, cls, r, to_year, target, bidders):
        """{line index: hint} for the lines whose own unit is thin in the
        history (fewer than UNIT_HINT_MIN earlier lettings, or the line is
        unpriced) while a unit of the same dimension is not. A virtual copy
        of each such line, written in that unit (quantity x factor), is
        priced by one more estimate_low_bid.estimate() over the schedule --
        the production path, same bidders, market, date and target -- and its
        price comes back per the line's own unit. The result shown (P50, P80,
        every line) is the first run's and is not touched: the hint is a
        reference the estimator may pick on the page, never an input.
        Call with the lock held."""
        ev = self.refs.unit_evidence if self.refs is not None else None
        if ev is None:
            return {}
        E = _E()
        D = q["bid_date"] or _dt.date.today().isoformat()
        conv, items_v = {}, []
        for i, (it, c, t) in enumerate(zip(items, cls, r["trace"])):
            status = self.line_status(t, it)
            h = None
            if status in ("priced", "unpriced") and (it.get("quantity") or 0) > 0:
                h = unit_hint_target(ev, c.get("family"), it.get("unit"),
                                     it.get("description") or "", D,
                                     unpriced=(status == "unpriced"))
            if h is None:
                items_v.append(it)
                continue
            tu, num, den, own, n_t = h
            x = dict(it)
            x["unit"] = tu
            x["quantity"] = _canon_qty(_f(it.get("quantity"), 0.0), num, den)
            up = _f(it.get("unit_price"))
            if up is not None:
                x["unit_price"] = up * den / num
            conv[i] = (tu, num, den, own, n_t, status)
            items_v.append(x)
        if not conv:
            return {}
        rv = E.estimate(self.tab, items_v, bidders["n"], q["market"], to_year,
                        target)
        el = r.get("expected_low")
        tot = r.get("market_total") or 0.0
        fx = r.get("fixed_total") or 0.0
        s50 = (el - fx) / tot if (el is not None and tot > 0) else None
        out = {}
        for i, (tu, num, den, own, n_t, status) in conv.items():
            tv, xv, it = rv["trace"][i], items_v[i], items[i]
            if self.line_status(tv, xv) != "priced" or not tv.get("price"):
                continue
            f = num / den                     # target units per unit of the line
            qv = xv.get("quantity") or 0.0
            ratio = ((tv.get("quantity_effective") or qv) / qv) if qv else 1.0
            mkt_tu = tv["price"]              # the typical bid per target unit
            unit = it.get("unit") or ""
            p50_tu = mkt_tu * ratio * s50 if s50 is not None else None
            p50 = p50_tu * f if p50_tu is not None else None
            head = unit_evidence_text(unit, tu, own, n_t,
                                      unpriced=(status == "unpriced"))
            conv_txt = (("Converted: about %s/%s (= %s/%s) at the expected-low-"
                         "bid level; typical bid %s/%s." % (
                             _rate(p50), unit, _rate(p50_tu), tu,
                             _rate(mkt_tu * f), unit))
                        if p50 is not None else
                        ("Converted: typical bid about %s/%s (= %s/%s)."
                         % (_rate(mkt_tu * f), unit, _rate(mkt_tu), tu)))
            out[i] = {
                "unit": unit, "target": tu, "factor": f, "own": own,
                "n_target": n_t, "status": status,
                "unit_price": p50 if p50 is not None else mkt_tu * f,
                "unit_price_target": p50_tu if p50_tu is not None else mkt_tu,
                "market_unit_price": mkt_tu * f,
                "market_unit_price_target": mkt_tu,
                "level": "p50" if p50 is not None else "market",
                "n_projects": _f(tv.get("n_projects")),
                "text": head + " " + conv_txt + " Reference only \u2014 check it "
                        "is the same kind of work."}
        return out

    @staticmethod
    def import_amount(c, file_amount, file_unit_price, quantity):
        """The owner-set amount an uploaded takeoff gives a line: its amount
        / total column, else quantity x its unit-price column -- for an
        owner-set allowance line only. Every other line is estimated, never
        read off the file."""
        if not c.get("owner_set_like"):
            return None
        a = _f(file_amount)
        if a and a > 0:
            return a
        up, q = _f(file_unit_price), _f(quantity)
        if up and up > 0 and q and q > 0:
            return up * q
        return None

    # -- expected bidders -------------------------------------------------------
    def suggest_bidders(self, payload):
        """N1's n-hat for a schedule: its own model total T + owner-set money
        (which the bidder count does not enter) read off one estimate at a
        placeholder count, then the history cell."""
        E = self.require()
        payload = dict(payload, n_bidders=N1_DEFAULT)
        q = self._request(payload)
        with self._lock:
            items, _ = self.build_items(q["raw"], q["market"])
            to_year = q["bid_date"][:4] if q["bid_date"] else None
            target = {"date": q["bid_date"], "owner": q["owner"] or None,
                      "work_type": q["work_type"]}
            r0 = E.estimate(self.tab, items, N1_DEFAULT, q["market"],
                            to_year, target)
            tot = (r0.get("market_total") or 0.0) + \
                (r0.get("fixed_total") or 0.0)
            return self.history.suggest(q["market"], q["work_type"], tot,
                                        q["bid_date"])

    # -- the estimate -------------------------------------------------------------
    def _request(self, payload):
        for k in FORBIDDEN_INPUTS:
            if k in payload:
                raise BadRequest(
                    "'%s' is not an accepted input. The OPCC / engineer's "
                    "estimate is neither a target nor a feature of this "
                    "estimate -- it is only a backtest baseline. Feeding it "
                    "in would recycle the very bias the tool exists to "
                    "remove." % k)
        market = str(payload.get("market") or "municipal").strip().lower()
        market_note = ""
        if market in ("not_sure", "other", "unsure"):
            # "Not sure": City / County is the default market (the company's
            # work is mostly municipal) -- said in the result
            market = "municipal"
            market_note = ("Market not given: priced as City / County "
                           "(municipal), the default.")
        if market not in MARKETS:
            raise BadRequest("market must be 'municipal' or 'txdot', got %r"
                             % payload.get("market"))
        items_in = payload.get("items") or []
        if not isinstance(items_in, list) or not all(
                isinstance(it, dict) for it in items_in):
            # a malformed request is a 400 with a reason, not a 500
            raise BadRequest("items must be a list of objects "
                             "({description, unit, quantity, ...}).")
        raw = [it for it in items_in if it.get("include", True)]
        if not raw:
            raise BadRequest("No line items supplied.")
        if len(raw) > MAX_LINES:
            raise BadRequest("This takeoff has %d lines; the estimate takes up "
                             "to %d at once. Split it into parts."
                             % (len(raw), MAX_LINES))
        bid_date = _iso_date(payload.get("bid_date"))
        if payload.get("bid_date") and not bid_date:
            raise BadRequest("bid_date must be YYYY-MM-DD, got %r"
                             % payload.get("bid_date"))
        nb = payload.get("n_bidders")
        auto = nb in (None, "", "auto")
        n = None
        if not auto:
            try:
                n = int(nb)
            except (TypeError, ValueError):
                raise BadRequest("n_bidders must be a whole number or "
                                 "'auto', got %r" % (nb,))
            if n < 1 or n > 60:
                raise BadRequest("n_bidders must be between 1 and 60")
        # owner / work type: the estimator gets a known id or nothing; what
        # the user typed is kept for the results header and the export
        owner_in = str(payload.get("owner") or "").strip()
        owner_text = str(payload.get("owner_text") or "").strip()
        owner = self.resolve_owner(market, owner_in or owner_text)
        wt_in = str(payload.get("work_type") or "").strip()
        wt_text = str(payload.get("work_type_text") or "").strip()
        work_type = self.resolve_work_type(market, wt_in or wt_text)
        return {"market": market, "raw": raw, "bid_date": bid_date,
                "n": n, "auto": auto, "market_note": market_note,
                "owner": owner,
                "owner_text": "" if owner else (owner_text or owner_in),
                "work_type": work_type,
                "work_type_text": "" if work_type else (wt_text or wt_in)}

    def resolve_owner(self, market, text):
        """The estimator's owner id for a typed or picked owner name (or
        id), or "" when it is not in the price history."""
        t = _norm_name(text)
        if not t:
            return ""
        for e in self._owners.get(market, []):
            if t == _norm_name(e["label"]) or any(
                    t == _norm_name(i) for i in e["ids"]):
                return e["id"]
        return ""

    def resolve_work_type(self, market, text):
        """The estimator's work type for a typed or picked one, or "" (the
        generic work type) when it is not in the price history."""
        t = _norm_name(text)
        if not t:
            return ""
        for e in self._work_types.get(market, []):
            if t in (_norm_name(e["label"]), _norm_name(e["id"])):
                return e["id"]
        return ""

    def build_items(self, raw, market="municipal"):
        """(estimator line dicts, classifications) for the included lines --
        the dicts run_schedule() builds from a schedule CSV."""
        items, cls = [], []
        for i, it in enumerate(raw):
            desc = str(it.get("description") or "").strip()[:MAX_DESC_CHARS]
            c = self.classify_one(desc, it.get("unit"), it.get("family"),
                                  market)
            c["row_id"] = str(it.get("row_id") or "r%d" % (i + 1))
            amount = _f(it.get("amount"))
            quote = _f(it.get("quote"))
            # an owner-set line given as a share of the other lines ("Owner's
            # Contingency Allowance" = 10% of the rest), resolved in _run();
            # an entered amount wins over a share
            pct = _f(it.get("amount_pct"))
            pct = pct if (pct is not None and 0 < pct <= 100
                          and not (amount and amount > 0)) else None
            items.append({
                "item_no": str(it.get("item_no") or "").strip(),
                "description": desc,
                "desc_norm": c["desc_norm"],
                "family": c["family"],
                "unit": c["unit"],
                "quantity": _f(it.get("quantity"), 0.0),
                "size_bucket": c["size_bucket"],
                "size_in": c["size_in"],
                "amount": amount if amount and amount > 0 else None,
                "quote": quote if quote and quote > 0 else None,
                "unit_price": None,
                # an entered owner-set amount is passed as run_schedule passes
                # it: amount + owner_fixed=yes -> face value, no discount
                "owner_fixed": bool(amount and amount > 0),
                "amount_pct": pct,
            })
            cls.append(c)
        return items, cls

    def _run(self, payload):
        """One estimate, exactly as /api/estimate runs it: (request, items,
        classifications, estimator result, to_year, target, bidders). Call
        with the lock held."""
        E = self.require()
        q = self._request(payload)
        items, cls = self.build_items(q["raw"], q["market"])
        to_year = q["bid_date"][:4] if q["bid_date"] else None
        target = {"date": q["bid_date"], "owner": q["owner"] or None,
                  "work_type": q["work_type"]}
        bidders = {"source": "user", "n": q["n"], "suggestion": None}
        # Owner-set lines given as a share of the other lines are priced on
        # the schedule without them first: amount = share x the SUBTOTAL,
        # that schedule's expected low bid with its owner-set allowances but
        # without another contingency line, which sits below the subtotal
        # too (user, 2026-11-21: subtotal, then contingency, then total).
        # The amount then goes in at face value like any entered owner-set
        # amount, so the estimator (and run_schedule, given the same amount)
        # prices the rest unchanged.
        pct_items = [it for it in items if it.get("amount_pct")]
        base_items = [it for it in items if not it.get("amount_pct")] \
            if pct_items else items
        if q["auto"]:
            # T + owner-set money does not depend on n: price once at a
            # placeholder n to read it, then take N1's n-hat.
            r0 = E.estimate(self.tab, base_items, N1_DEFAULT, q["market"],
                            to_year, target)
            tot = (r0.get("market_total") or 0.0) + \
                (r0.get("fixed_total") or 0.0)
            sug = self.history.suggest(q["market"], q["work_type"], tot,
                                       q["bid_date"])
            bidders = {"source": "rule N1", "n": sug["n"],
                       "suggestion": sug}
        if pct_items:
            ra = E.estimate(self.tab, base_items, bidders["n"], q["market"],
                            to_year, target)
            el = ra.get("expected_low")
            below = sum(it["amount"] for it in base_items
                        if it.get("owner_fixed") and it.get("amount")
                        and contingency_worded(it["description"], it["unit"],
                                               it["quantity"]))
            base = (el - below) if el is not None else None
            for it in pct_items:
                if base and base > 0:
                    it["amount"] = round(it["amount_pct"] / 100.0 * base, 2)
                    it["owner_fixed"] = True
                    it["amount_from_pct"] = {"pct": it["amount_pct"],
                                             "base": round(base, 2)}
        r = E.estimate(self.tab, items, bidders["n"], q["market"], to_year,
                       target)
        if not q["auto"]:
            tot = (r.get("market_total") or 0.0) + \
                (r.get("fixed_total") or 0.0)
            bidders["suggestion"] = self.history.suggest(
                q["market"], q["work_type"], tot, q["bid_date"])
        return q, items, cls, r, to_year, target, bidders

    def estimate(self, payload):
        self.require()
        t0 = time.time()
        with self._lock:
            q, items, cls, r, to_year, target, bidders = self._run(payload)
            out = self._result(r, q, items, cls, to_year, target, bidders)
        out["elapsed_s"] = round(time.time() - t0, 3)
        return out

    # -- result -> JSON -----------------------------------------------------------
    def _result(self, r, q, items, cls, to_year, target, bidders):
        E = _E()
        market = q["market"]
        el = r.get("expected_low")
        tot = r.get("market_total") or 0.0
        fx = r.get("fixed_total") or 0.0
        s50 = (el - fx) / tot if (el is not None and tot > 0) else None
        esc = {"to_year": to_year,
               "arm": E.market_index_key(self.tab, market),
               "carried": bool(to_year) and not E._any_arm_has_year(
                   self.tab.index, to_year)}
        lines = []
        for c, it, t in zip(cls, items, r["trace"]):
            lines.append(self._line(c, it, t, s50, market, esc))
        unc = [ln for ln in lines if ln["status"] == "not_covered"]
        not_covered = {
            "lines": [{"row_id": ln["row_id"], "item_no": ln["item_no"],
                       "description": ln["description"],
                       "class": ln["coverage_class"],
                       "class_label": ln["coverage_label"],
                       "quantity": ln["quantity"], "unit": ln["unit"],
                       "quote": ln["quote"]} for ln in unc],
            "n": len(unc),
            "share_lines": r.get("uncovered_share_lines"),
            "quoted_n": r.get("uncovered_quoted_n",
                              sum(1 for ln in unc if ln["quote"])),
            "quoted_total": r.get("uncovered_quoted_total") or 0.0,
            "complete_total": r.get("complete_total"),
            "complete_total_p80": r.get("complete_total_p80"),
            "as_a_whole": bool(r.get("not_covered")),
            "why": [english(w) for w in (r.get("not_covered_why") or [])],
        }
        headline = {
            "p50": el, "p80": r.get("expected_low_p80"),
            "p80_multiplier": r.get("p80_multiplier"),
            "p50_band": list(r["p50_band"]) if r.get("p50_band") else None,
            "p80_band": list(r["p80_band"]) if r.get("p80_band") else None,
            "calibration_n": r.get("calibration_n"),
            "calibration_market": r.get("calibration_market"),
            "level_correction": r.get("level_correction"),
            "not_under_level": r.get("not_under_level"),
            "covered_only": bool(unc),
            "fixed_total": fx,
        }
        # contingency lines sit below the subtotal: each figure as subtotal
        # + contingency (split_contingency), for the page and the export
        below = [ln for ln in lines if ln["below_subtotal"]]
        if below:
            fixed = sum(ln["amount_p50"] or 0.0 for ln in below
                        if not ln["amount_from_pct"])
            share = sum(ln["amount_from_pct"]["pct"] for ln in below
                        if ln["amount_from_pct"]) / 100.0
            cont = {"row_ids": [ln["row_id"] for ln in below],
                    "pct": round(100.0 * share, 6) if share else None,
                    "fixed": fixed}
            for k, v in (("p50", el), ("p80", r.get("expected_low_p80"))):
                s, cv = split_contingency(v, fixed, share)
                cont[k] = None if s is None else {"subtotal": s,
                                                   "contingency": cv}
            headline["contingency"] = cont
        else:
            headline["contingency"] = None
        lc = r.get("low_confidence") or {}
        low_conf = {
            "history": english(lc.get("history") or ""),
            "headline_unchanged": lc.get("headline_unchanged", True),
            "guide": LOWCONF_GUIDE,
            "lines": [self._lowconf(g) for g in lc.get("lines") or []],
        }
        d = r.get("discount") or {}
        competition = {
            "n_bidders": bidders["n"], "source": bidders["source"],
            "suggestion": bidders["suggestion"],
            "f": d.get("f"), "table": d.get("table"), "bin": d.get("bin"),
            "lettings": d.get("lettings"), "p25": d.get("p25"),
            "p75": d.get("p75"), "extrapolated": d.get("extrapolated"),
            "d1": bool(d.get("d1")), "note": d.get("note"),
            "available": bool(d),
        }
        return {
            "ok": el is not None,
            "inputs": {"market": market, "market_label": MARKET_LABELS[market],
                       "market_note": q.get("market_note") or "",
                       "owner": q["owner"],
                       "owner_label": (owner_name(q["owner"]) if q["owner"]
                                       else ("%s (not in the price history)"
                                             % q["owner_text"])
                                       if q.get("owner_text") else ""),
                       "owner_in_history": bool(q["owner"]),
                       "work_type": q["work_type"],
                       "work_type_label": (work_type_label(q["work_type"])
                                           if q["work_type"] else
                                           ("%s (not in the price history; "
                                            "generic work type used)"
                                            % q["work_type_text"])
                                           if q.get("work_type_text") else ""),
                       "work_type_in_history": bool(q["work_type"]),
                       "bid_date": q["bid_date"], "to_year": to_year,
                       "comparables_before": (r.get("comparables") or {})
                       .get("date") or q["bid_date"],
                       "n_bidders": bidders["n"],
                       "bidders_source": bidders["source"]},
            "headline": headline,
            "context": dict(MARKET_ACCURACY[market], market=market),
            "alerts": self._alerts(r, lines, not_covered, competition,
                                   bidders["n"]),
            "warnings": [plain_codes(english(w)) for w in r.get("warnings") or []],
            "not_covered": not_covered,
            "low_confidence": low_conf,
            "lines": lines,
            "similar": self._similar(r, items, lines, market, target,
                                     to_year, bidders["n"]),
            "competition": competition,
            "totals": {"market_total": tot, "fixed_total": fx,
                       "n_fixed": r.get("n_fixed"), "n_items": r.get("n_items"),
                       "priced": r.get("priced"), "unpriced": r.get("unpriced"),
                       "bid_part_lines": r.get("bid_part_lines"),
                       "bid_part": r.get("bid_part"),
                       "degraded_dollar_share": r.get("degraded_dollar_share"),
                       "scale_to_p50": s50},
            "engine": {"md5": self.info.get("estimator", {}).get("md5"),
                       "rule": self.info.get("estimator", {}).get("rule"),
                       "data": self.info.get("data", {}).get(
                           "master_panel_mtime"),
                       "latest_letting": self.info.get("data", {}).get(
                           "latest_letting")},
        }

    @staticmethod
    def _alerts(r, lines, nc, comp, n_bidders):
        """What the reader must not miss, decided on the result's keys (the
        estimator's own warnings follow verbatim, as notes). Each alert is
        short (user, 2026-11-21: "shorter and clearer"): a `title` (what is
        wrong, one sentence), an `action` (what to do) and `items` (the lines
        it is about); `text` joins them for the exports."""
        out = []
        el = r.get("expected_low")

        def name(ln):
            return (ln.get("description") or ln.get("item_no") or "a line")[:60]

        def add(level, code, title, action="", items=(), **kw):
            items = list(items)
            text = " ".join(x for x in (title, action) if x)
            if items:
                text += " " + "; ".join(items[:6]) + ("; ..." if len(items) > 6 else "") + "."
            out.append(dict({"level": level, "code": code, "title": title,
                             "action": action, "items": items[:6],
                             "more": max(0, len(items) - 6), "text": text}, **kw))

        if nc["as_a_whole"]:
            add("severe", "not_covered_whole",
                "No estimate: this kind of job has no comparable bid history.",
                "Price it from vendor or specialty quotes.", nc["why"])
        elif el is None:
            if (r.get("market_total") or 0) <= 0:
                why = "not a single line could be priced"
            elif not comp["available"]:
                why = ("no past jobs with about %s bidders, so the "
                       "competition effect is unknown" % n_bidders)
            else:
                why = "see the estimator notes"
            add("severe", "no_estimate", "No estimate: %s." % why)
        unp = [ln for ln in lines if ln["status"] == "unpriced"]
        if unp and el is not None:
            add("severe", "lines_unpriced",
                "%d line%s could not be priced and %s left out of P50 and P80."
                % (len(unp), "" if len(unp) == 1 else "s",
                   "is" if len(unp) == 1 else "are"),
                "Price %s yourself in the OPCC worksheet." % ("it" if len(unp) == 1 else "them"),
                [name(ln) for ln in unp])
        if nc["n"] and not nc["as_a_whole"]:
            add("caution", "not_covered",
                "%d line%s no bid history and %s left out of P50 and P80."
                % (nc["n"], " has" if nc["n"] == 1 else "s have",
                   "is" if nc["n"] == 1 else "are"),
                "Add a vendor quote, or your own price in the OPCC worksheet.",
                [(l.get("description") or l.get("item_no") or "a line")[:60]
                 for l in nc["lines"]])
        uc = [ln for ln in lines if ln["status"] == "priced"
              and ln["evidence"]["usable"] == "unclassified"]
        if uc:
            add("caution", "unclassified_priced",
                "%d line%s priced from a mixed group of unlike items."
                % (len(uc), " is" if len(uc) == 1 else "s are"),
                "Check the item type with Change in the takeoff.",
                [name(ln) for ln in uc])
        al = [ln for ln in lines if ln["amount_missing"]]
        if al:
            add("severe", "allowance_missing",
                "%d owner-set allowance%s no amount, so the totals are too low."
                % (len(al), " has" if len(al) == 1 else "s have"),
                "Enter the amount from the bid form under Project details.",
                [name(ln) for ln in al], placement="above_headline")
        # The lines that carry much of the money on almost no evidence, named
        # (2026-11-21). Presentation only: P50 / P80 are unchanged. Example:
        # "Embankment Fill 48,600 CF" priced from the one earlier letting in
        # CF (a geofoam fill) carried $440k of an $842k expected low bid.
        if el:
            heavy = sorted(
                [ln for ln in lines if ln["status"] == "priced"
                 and (ln.get("amount_p50") or 0) >= THIN_HEAVY_SHARE * el
                 and 0 < (ln["evidence"].get("n_projects") or 0)
                 < THIN_HEAVY_MAX_PROJECTS],
                key=lambda ln: -(ln.get("amount_p50") or 0))
            if heavy:
                share = sum(ln["amount_p50"] for ln in heavy) / el
                add("caution", "thin_heavy_lines",
                    "%d big line%s on only 1\u2013%d past jobs (%.0f%% of the "
                    "expected low bid)." % (len(heavy), " rests" if len(heavy) == 1
                                            else "s rest",
                                            THIN_HEAVY_MAX_PROJECTS - 1,
                                            100 * share),
                    "Check %s references." % ("its" if len(heavy) == 1 else "their"),
                    ["%s \u2014 %s, %d past job%s" % (
                        name(ln), "${:,.0f}".format(ln["amount_p50"]),
                        ln["evidence"]["n_projects"],
                        "" if ln["evidence"]["n_projects"] == 1 else "s")
                     for ln in heavy])
        ds = r.get("degraded_dollar_share")
        if el is not None and ds and ds >= 0.5:
            add("caution", "thin_evidence",
                "%.0f%% of the total is priced from thin history (fewer than "
                "5 past jobs)." % (100 * ds),
                "Read the ranges, not just P50.")
        if comp.get("extrapolated"):
            add("info", "bin_extrapolated",
                "No past jobs with exactly %s bidders; the nearest count (%s) "
                "was used for the competition discount." % (n_bidders,
                                                            comp["bin"]))
        return out

    @staticmethod
    def line_status(t, it):
        """priced | owner_set | not_covered | not_covered_whole | unpriced"""
        q = it["quantity"] or 0.0
        price = t.get("price")
        qe = t.get("quantity_effective") or q
        if t.get("uncovered"):
            return "not_covered"
        if t.get("price_key") == "not_covered":
            return "not_covered_whole"
        if t.get("fixed_amount"):
            return "owner_set"
        if price and q > 0:
            return "priced"
        if price and qe > 0 and t.get("price_key") == "mobilization_ratio":
            return "priced"
        return "unpriced"

    def _line(self, c, it, t, s50, market, esc=None):
        q = it["quantity"] or 0.0
        price = t.get("price")
        qe = t.get("quantity_effective") or q
        why = [english(w) for w in t.get("why") or []]
        status = self.line_status(t, it)
        mkt_amount = price * qe if (status == "priced") else None
        amount_p50 = unit_p50 = None
        if status == "priced" and s50 is not None:
            amount_p50 = mkt_amount * s50
            unit_p50 = amount_p50 / q if q > 0 else None
        elif status == "owner_set":
            amount_p50 = t["fixed_amount"]
        basis, warns = self._basis(t, c, status, it, esc)
        acc = (c["accuracy"] if status == "priced" else
               dict(c["accuracy"], tier="na",
                    text={"owner_set": "owner-set",
                          "not_covered": "not covered",
                          "not_covered_whole": "not covered"}.get(status,
                                                                  "not priced"),
                    tooltip="No accuracy figure: this line is not priced by "
                            "the estimator."))
        return {
            "row_id": c["row_id"], "item_no": it["item_no"],
            "description": it["description"], "unit": it["unit"],
            "quantity": it["quantity"], "family": c["family"],
            "family_label": c["family_label"],
            "family_overridden": c["family_overridden"],
            "size_bucket": c["size_bucket"], "size_in": c["size_in"],
            "coverage_class": c["coverage_class"],
            "coverage_label": c["coverage_label"],
            "lowconf_class": c["lowconf_class"],
            "lowconf_label": c["lowconf_label"],
            "allowance_like": c["allowance_like"],
            "owner_set_like": c["owner_set_like"],
            "amount_missing": bool(status == "priced"
                                   and c["owner_set_like"]),
            "status": status,
            "market_unit_price": price if status == "priced" else None,
            "market_amount": mkt_amount,
            "unit_price_p50": unit_p50, "amount_p50": amount_p50,
            "fixed_amount": t.get("fixed_amount"),
            "amount_from_pct": it.get("amount_from_pct"),
            # a contingency: shown below the subtotal, not among the items
            "below_subtotal": bool(status == "owner_set" and (
                it.get("amount_from_pct") or contingency_worded(
                    it["description"], it["unit"], it["quantity"]))),
            "quote": t.get("quote") if status == "not_covered" else None,
            "basis": basis, "why": why,
            "price_key": t.get("price_key"),
            "evidence": {"usable": t.get("usable"),
                         "n_projects": _f(t.get("n_projects")),
                         "n_owners": _f(t.get("n_owners")),
                         "jaccard": t.get("token_match_jaccard"),
                         "candidates": t.get("token_candidates"),
                         "qty_band": t.get("qty_band"),
                         "vintage_year": _f(t.get("vintage_year")),
                         "escalation": t.get("escalation"),
                         "escalation_basis": t.get("escalation_basis")},
            "accuracy": acc, "warnings": warns,
            # a printed cap on a mobilization line ("5% Max"): the page
            # offers it as a quick pick, computed from the user's amounts
            "printed_cap": (printed_cap(it.get("description"))
                            if "mobilization" in (c.get("family"),
                                                  c.get("lowconf_class"))
                            else None),
        }

    @staticmethod
    def _basis(t, c, status, it, esc=None):
        """A short English basis for the line and its line-level flags.
        Decided on the trace's keys (price_key, usable, escalation,
        escalation_basis, ...), not on the wording of its notes."""
        esc = esc or {}
        pk = t.get("price_key")
        why = " | ".join(t.get("why") or [])
        n = _f(t.get("n_projects"))
        ev = (" · %d earlier project%s" % (n, "" if n == 1 else "s")) if n else ""
        warns = []
        if status == "not_covered":
            basis = ("Not covered (%s): no comparable bid history; get a "
                     "vendor quote" % (c["coverage_label"] or "specialty"))
            if t.get("quote"):
                basis += " -- quote taken at face value"
        elif status == "not_covered_whole":
            basis = "Not priced: the schedule as a whole is not covered"
        elif status == "owner_set":
            ap = it.get("amount_from_pct")
            basis = ("Contingency: %g%% of the subtotal (%s), below it at "
                     "face value" % (ap["pct"], "${:,.0f}".format(ap["base"]))
                     if ap else
                     "Owner-set amount, taken at face value (no discount)")
        elif pk == "token_jaccard":
            basis = "Priced from similarly worded items%s" % ev
            if t.get("token_exact_rule"):
                basis += " · same wording"
        elif pk == "family+unit+size+band":
            basis = "Priced from this kind of item at this size and quantity%s" % ev
        elif pk == "family+unit+size":
            basis = "Priced from the typical price of this kind of item%s" % ev
            if t.get("qty_factor"):
                basis += " · adjusted for quantity (×%.2f)" % t["qty_factor"]
        elif pk == "f1_nearest_size":
            f1 = t.get("f1") or {}
            basis = ("Priced from the nearest size of this item, scaled for "
                     "size (×%.2f)%s" % (f1.get("factor") or 1.0, ev))
        elif pk == "removal_typical_line":
            basis = ("No removal history: priced at this schedule's typical "
                     "line amount")
        elif pk == "mobilization_ratio":
            basis = ("Mobilization as a share of the rest of the job "
                     "(%.1f%%, as on TxDOT jobs)"
                     % (100 * (t.get("mobilization_ratio") or 0)))
        else:
            first = (t.get("why") or ["no reference cell"])[0]
            basis = "Not priced: %s" % plain_why(first)
        u = str(t.get("usable") or "")
        if status == "priced":
            if u == "unclassified":
                warns.append({"level": "severe",
                              "text": "unclassified: priced from a "
                                      "catch-all group of unlike items"})
            elif u == "no":
                warns.append({"level": "caution",
                              "text": "thin evidence (%s project%s)"
                                      % (int(n or 0), "" if n == 1 else "s")})
            elif u == "thin":
                warns.append({"level": "info",
                              "text": "thin evidence (%d projects)"
                                      % int(n or 0)})
            # escalation, from the trace's keys
            if esc.get("to_year") and not t.get("escalation"):
                warns.append({"level": "caution",
                              "text": "not brought to the bid year (no "
                                      "price index)"})
            elif (t.get("escalation_basis") and esc.get("arm")
                  and t["escalation_basis"] != esc["arm"]):
                warns.append({"level": "caution",
                              "text": "brought to the bid year with "
                                      "another market's price index (%s)"
                                      % t["escalation_basis"]})
            elif t.get("escalation") and esc.get("carried"):
                warns.append({"level": "info",
                              "text": "no price index for %s yet: the "
                                      "latest level is used" % esc["to_year"]})
            # best effort: the estimator records these only in its notes
            if "quantity effect NOT applied" in why:
                warns.append({"level": "info",
                              "text": "no quantity adjustment for this "
                                      "kind of item"})
            if "UNSIZED bucket" in why or "substituted the best" in why:
                warns.append({"level": "caution",
                              "text": "priced from a different size"})
            if c["owner_set_like"]:
                warns.append({"level": "severe", "code": "allowance_missing",
                              "text": ALLOWANCE_MISSING})
        elif status == "unpriced":
            if not (it.get("quantity") or 0) > 0:
                warns.append({"level": "severe",
                              "text": "no quantity: not priced"})
            else:
                warns.append({"level": "severe",
                              "text": "not priced: the total leaves it out"})
        if c["lowconf_class"] and status == "priced":
            warns.append({"level": "info",
                          "text": "low confidence: see the low-confidence "
                                  "panel"})
        if c["family"] in ("other", "needs_review", "") and status == "priced":
            warns.append({"level": "caution",
                          "text": "unclassified: no item type "
                                  "matched"})
        return basis, warns

    @staticmethod
    def _lowconf(g):
        h = g.get("history")
        rng = g.get("implied_p50_range")
        return {
            "class": g.get("class"),
            "label": LOWCONF_LABELS.get(g.get("class"), g.get("class")),
            "items": [x for x in g.get("items") or []],
            "descriptions": g.get("descriptions") or [],
            "ours_market": g.get("ours_market"),
            "ours_p50": g.get("ours_p50"), "ours_p80": g.get("ours_p80"),
            "share_of_p50": g.get("share_of_p50"),
            "rest_p50": g.get("rest_p50"), "unpriced": g.get("unpriced"),
            "history": ({"p25": h["p25"], "median": h["median"],
                         "p75": h["p75"], "lettings": h["n"],
                         "scope": h["scope"],
                         "size_label": (_E().LOWCONF_SIZE_LABELS[h["size_class"]]
                                        if h["scope"] == "size"
                                        else "all job sizes"),
                         "before": h.get("before")} if h else None),
            "implied_range": list(rng) if rng else None,
            "implied_median": g.get("implied_p50_median"),
            "position": g.get("position"),
        }

    # -- similar past projects (the comparables) -----------------------------
    def _cmp_view(self, r, items, market, target, to_year, n_bidders):
        """The schedule as comparables.price_target sees it -- the same rec /
        keys estimate_low_bid._comparables_price builds (covered lines;
        owner-set lines keep their key but carry no price) -- plus the
        overlap and similarity arithmetic of price_target, for display.
        None without a comparables blend."""
        C = upstream.comparables
        E = _E()
        cm = r.get("comparables")
        pool = (self.tab.comparables or {}).get(market)
        if pool is None or not cm:
            return None
        import numpy as np
        ctx = pool.ctx
        trace = r["trace"]
        fT = (r.get("discount") or {}).get("f") or 1.0
        D = cm.get("date")
        keys, kidx, rec_lines, rows = [], {}, [], []
        for i, t in enumerate(trace):
            if t.get("uncovered"):
                continue
            it = items[i]
            qv = _f(it.get("quantity"), 0.0) or 0.0
            k = (it.get("family") or "", it.get("unit") or "",
                 it.get("size_bucket") or "",
                 E.qty_shape.band_of(qv) if qv > 0 else "",
                 it.get("description") or "")
            ki = kidx.get(k)
            if ki is None:
                ki = kidx[k] = len(keys)
                keys.append(k)
            fx = t.get("fixed_amount") or 0.0
            rec_lines.append({"ki": ki, "q": qv, "p": t.get("price"), "fx": fx})
            if not fx:
                rows.append((ki, qv, t.get("price"), i))
        rec = {"key": None, "date": D, "year": to_year or "",
               "n_bidders": n_bidders, "f": fT, "lines": rec_lines}
        w_key = collections.defaultdict(float)
        priced = set()
        for ki, qv, p, _ in rows:
            if p:
                w_key[ki] += p * qv
                priced.add(ki)
        kis = sorted({ki for ki, _, _, _ in rows})
        wmed = statistics.median([w_key[k] for k in priced]) if priced else 1.0
        W = {ki: (w_key[ki] if ki in priced else wmed) for ki in kis}
        nL = len(ctx.L)
        num = np.zeros(nL)
        Wsum = sum(W.values())
        kinfo = {}
        for ki in kis:
            fam, un, sz, bd, dn = keys[ki]
            tk = C.tokens(dn, market)
            sm = ctx.sims(tk, un) if (tk and un) else []
            kinfo[ki] = {"t": tk, "rm": C.removal_only(dn),
                         "ls": (un or "").upper() in C.LS_UNITS,
                         "sims": dict(sm)}
            if not sm:
                continue
            best = np.zeros(nL)
            for iid, j in sm:
                idx = ctx.postings.get(iid)
                if idx is not None:
                    np.maximum.at(best, idx, j)
            num += W[ki] * best
        O = num / Wsum if Wsum > 0 else num
        S_T = sum((p or 0.0) * qv * fT for ki, qv, p, _ in rows
                  if p and not kinfo[ki]["ls"])
        own_T = target.get("owner")
        wt_T = target.get("work_type") or ""
        dD = _dt.date.fromisoformat(D)
        scores = {}
        for ci, Lc in enumerate(ctx.L):
            if not (Lc["date"] < D and O[ci] >= C.O_MIN):
                continue
            gs = (math.exp(-0.5 * abs(math.log(S_T / Lc["nonls"])))
                  if (S_T > 0 and Lc["nonls"] > 0)
                  else math.exp(-0.5 * math.log(10)))
            go = 1.5 if (own_T and Lc["owner"] == own_T) else 1.0
            gt = 1.5 if (wt_T and Lc["wtype"] == wt_T) else 1.0
            age = (dD - _dt.date.fromisoformat(Lc["date"])).days / 365.25
            ga = 2.0 ** (-age / 3.0)
            scores[Lc["pk"]] = {
                "S": float(O[ci] * gs * go * gt * ga), "O": float(O[ci]),
                "factors": {"overlap": float(O[ci]), "size": gs,
                            "same_owner": go, "same_work_type": gt,
                            "age_years": age, "age": ga}}
        return {"pool": pool, "ctx": ctx, "rec": rec, "keys": keys,
                "rows": rows, "W": W, "Wsum": Wsum, "kinfo": kinfo,
                "scores": scores, "D": D, "fT": fT}

    def comparable_scores(self, r, items, market, target, to_year=None,
                          n_bidders=None):
        """{letting key: {S, O, factors}} for every candidate comparables.py
        scored -- its own overlap x size x owner x work type x age, on the
        same inputs. Display only; the test suite checks that ranking by it
        gives estimate()'s own choice and order."""
        v = self._cmp_view(r, items, market, target, to_year, n_bidders)
        return v["scores"] if v else {}

    def price_at(self, view, market, target, only=None):
        """comparables.price_target itself on this schedule; with `only` (a
        letting key) every other candidate is hidden, so the figure is this
        job priced from that one project's low-bid lines (escalated,
        quantity- and competition-adjusted exactly as the blend does; lines
        it has no match for stay at our own line price x f). Returns the
        price_target result."""
        import copy
        import numpy as np
        C = upstream.comparables
        E = _E()
        pool, ctx, rec = view["pool"], view["ctx"], view["rec"]
        vctx = ctx
        if only is not None:
            vctx = copy.copy(ctx)
            mask = np.full(len(ctx.L), E.NEVER, dtype=ctx.dates.dtype)
            ci = ctx.pk_index[only]
            mask[ci] = ctx.dates[ci]
            vctx.dates = mask
        cut = min(rec["date"], C.DEV_END)
        beta, gamma = pool.bg.at(cut)[0], pool.bg.at(cut)[1]
        t1 = getattr(self.tab, "t1", None) if (E.TXDOT_C4D1
                                                and market == "txdot") else None
        fC = t1.f_candidate(self.tab, rec["date"]) if t1 is not None else None
        return C.price_target(vctx, rec, view["keys"], self.tab, beta, gamma,
                              E, target.get("owner"),
                              target.get("work_type") or "", fC)

    def _similar(self, r, items, lines, market, target, to_year, n_bidders):
        """Up to MAX_SIMILAR cards: comparables.py's own top picks, in its
        order, each with its bids, its size against this job, the overlap
        in words, this job at its prices and the matched lines side by
        side. Public lettings dated before the bid date only (the pool)."""
        cm = r.get("comparables")
        head = {"max": MAX_SIMILAR, "cards": [], "available": bool(cm)}
        if not cm:
            head["why_fewer"] = ("The comparables blend gave nothing here, so "
                                 "no similar past project is shown.")
            return head
        view = self._cmp_view(r, items, market, target, to_year, n_bidders)
        ctx = view["ctx"]
        D = view["D"]
        el = r.get("expected_low")
        fx = r.get("fixed_total") or 0.0
        comps = (cm.get("comps") or [])[:MAX_SIMILAR]
        n_types = len(view["kinfo"])
        cards = []
        for rank, pk in enumerate(comps, 1):
            ci = ctx.pk_index[pk]
            Lc = ctx.L[ci]
            sc = view["scores"].get(pk) or {}
            matched, mtypes, mdollars = [], set(), 0.0
            for ki, q, p, i in view["rows"]:
                info = view["kinfo"][ki]
                best = None
                for iid, j in info["sims"].items():
                    for li in Lc["by_iid"].get(iid, ()):
                        ln = Lc["lines"][li]
                        key = (-j, 0 if ln["rm"] == info["rm"] else 1,
                               abs(math.log(ln["q"]) - math.log(q))
                               if (ln["q"] > 0 and q > 0) else 99.0, li)
                        if best is None or key < best[0]:
                            best = (key, ln, j, iid)
                if best is None:
                    continue
                if ki not in mtypes:
                    mtypes.add(ki)
                    mdollars += view["W"][ki]
                _, ln, j, iid = best
                ours = lines[i]
                tl = self.panel.matched_line(
                    pk, ln["unit"], ln["q"], ln["u"],
                    ctx.id_list[iid][0] if iid is not None else None)
                matched.append({
                    "row_id": ours["row_id"], "item_no": ours["item_no"],
                    "description": ours["description"],
                    "quantity": ours["quantity"], "unit": ours["unit"],
                    "our_unit_p50": ours["unit_price_p50"],
                    "their_item_no": tl[0] if tl else "",
                    "their_description": tl[1] if tl else "",
                    "their_dn": tl[2] if tl else "",
                    "their_quantity": ln["q"], "their_unit": ln["unit"],
                    "their_low_unit": ln["u"],
                    "their_median_unit": tl[6] if tl else None,
                    "their_bids_on_line": tl[7] if tl else None,
                    "match": j})
            at = None
            try:
                pa = self.price_at(view, market, target, only=pk)
                at = {"bid_level": pa["cmp"], "matched": pa["matched"],
                      "job": pa["cmp"] + fx,
                      "matched_share": (pa["matched"] / pa["cmp"]
                                        if pa["cmp"] else None),
                      "vs_p50": ((pa["cmp"] + fx) / el) if el else None}
            except Exception as e:  # noqa: BLE001 - display only
                log.warning("price at %s failed: %s", pk, e)
            tots = self.panel.totals.get(pk) or []
            meta = self.panel.meta.get(pk) or {}
            low = Lc["low"]
            others = sorted(tots)
            cards.append({
                "rank": rank, "key": pk,
                "project": project_title(pk, market),
                "title": meta.get("title") or "",
                "owner_id": Lc["owner"], "owner": owner_name(Lc["owner"]),
                "date": Lc["date"],
                "work_type": (work_type_label(Lc["wtype"])
                              if market == "municipal"
                              else (Lc["wtype"] or "").title()),
                "bidders": Lc["n"],
                "bids": {"low": low,
                         "second": others[1] if len(others) > 1 else None,
                         "median": (statistics.median(others)
                                    if others else None),
                         "high": others[-1] if others else None,
                         "n": len(others)},
                "size_vs_this_job": (low / el) if el else None,
                "similarity": sc.get("S"), "overlap": sc.get("O"),
                "factors": sc.get("factors"),
                "shares": {"types": len(mtypes), "of_types": n_types,
                           "dollar_share": (mdollars / view["Wsum"]
                                            if view["Wsum"] else None)},
                "at_their_prices": at,
                "matched_lines": matched,
                "source": dict(self.sources.get(pk, meta),
                               county=meta.get("county") or ""),
            })
        nc = cm.get("n_candidates") or 0
        why = None
        if len(cards) < MAX_SIMILAR:
            why = ("Only %d earlier public job%s bid before %s share%s at "
                   "least %.0f%% of this schedule's dollars%s."
                   % (nc, "" if nc == 1 else "s", D, "s" if nc == 1 else "",
                      100 * upstream.comparables.O_MIN,
                      "" if nc else "; the expected low bid rests on the line "
                                    "prices alone"))
        head.update({
            "cards": cards, "why_fewer": why, "n_candidates": nc,
            "n_blended": cm.get("n_comps"), "before": D,
            "cmp_price": cm.get("cmp"), "line_price": r.get("bid_part_lines"),
            "blend": r.get("bid_part"),
            "level_correction": r.get("level_correction"),
            "how": ("P50 blends two prices: the line-priced estimate and the "
                    "comparables price, which transfers each line from the "
                    "%d most similar earlier public jobs (geometric mean, then "
                    "centred). The cards are those jobs, most similar first."
                    % (cm.get("n_comps") or 0)),
        })
        return head

    # -- the references behind a line price -----------------------------------
    def load_references(self):
        """Build the reference index: estimate_low_bid._reference_pool()
        reads master_panel.csv once more (about 5-8 s). Run in the
        background after the estimator is ready; estimates do not wait."""
        with self._lock:
            if self.refs_state["ready"] or self.refs_state["loading"]:
                return self
            self.refs_state.update(loading=True, error="")
        try:
            self.require()
            idx = ReferenceIndex()
            idx.unit_evidence = UnitEvidence(idx.rows, idx.fk, self.panel.avail)
            with self._lock:
                self.refs = idx
                self.refs_state.update(ready=True, loading=False,
                                       load_s=round(idx.load_s, 2),
                                       rows=len(idx.rows),
                                       lettings=len(idx.keys))
            log.info("reference index ready: %d rows, %d lettings, %.1fs",
                     len(idx.rows), len(idx.keys), idx.load_s)
        except Exception as e:  # noqa: BLE001
            log.exception("reference index failed")
            with self._lock:
                self.refs_state.update(ready=False, loading=False,
                                       error="%s: %s" % (type(e).__name__, e))
        return self

    def trace_cell(self, t, it, market):
        """The reference cell estimate_low_bid.item_market_price priced this
        line from, re-identified with its own selection rules on the same
        tables: {kind, key, row, ...} or None. The caller checks that the
        cell's price times the trace's factors gives the trace's price."""
        E = _E()
        tab = self.tab
        pk = t.get("price_key")
        family, unit = it.get("family") or "", it.get("unit") or ""
        q = _f(it.get("quantity"), 0.0) or 0.0
        sb = it.get("size_bucket") or ""
        desc = it.get("description") or ""
        fk = E.family_key(family, desc)
        if pk == "token_jaccard":
            qb0 = E.qty_shape.band_of(q) if q > 0 else ""
            tk = E.item_tokens.tokens(desc)
            cand = set()
            for w in tk:
                for k in tab.tok_inv.get(unit, {}).get(w, ()):
                    if k[2] == qb0:
                        cand.add(k)
            hits = []
            for k in cand:
                j = E.item_tokens.similar(tk, k[0])
                if j <= 0:
                    continue
                row = tab.ref_tok[k]
                pv = _f(row.get("price_median"))
                if pv and pv > 0 and (_f(row.get("n_projects")) or 0) >= 2:
                    hits.append((j, pv, row, k))
            if not hits:
                return None
            exact = None
            if E.TXDOT_C4D1 and market == "txdot":
                ex = [h for h in hits if h[0] >= 1.0 and
                      (_f(h[2].get("n_projects")) or 0) >= E.C4_MIN_N]
                exact = ex[0] if ex else None
            hits.sort(key=lambda x: (x[1], _f(x[2].get("vintage_year")) or 0.0,
                                     x[2].get("tok") or ""))
            tw = sum(h[0] for h in hits)
            acc = 0.0
            pick = hits[len(hits) // 2]
            for h in hits:
                acc += h[0]
                if acc >= tw / 2:
                    pick = h
                    break
            if exact is not None:
                pick = exact
            return {"kind": "tok", "key": pick[3], "row": pick[2],
                    "jaccard": pick[0], "candidates": len(hits),
                    "exact": exact is not None}
        if pk == "family+unit+size+band":
            qb = E.qty_shape.band_of(q) if q > 0 else ""
            key = (fk, unit, sb, qb)
            row = tab.ref_band.get(key)
            if row is None and not t.get("f1"):
                key = (fk, unit, "", qb)
                row = tab.ref_band.get(key)
            return {"kind": "band", "key": key, "row": row} if row else None
        if pk == "f1_nearest_size":
            key = tuple((t.get("f1") or {}).get("ref_key") or ())
            row = tab.ref.get(key) if key else None
            return {"kind": "fam", "key": key, "row": row,
                    "f1": t.get("f1")} if row else None
        if pk == "family+unit+size":
            key = (fk, unit, sb)
            row = tab.ref.get(key)
            sub = None
            if row is None:
                cands = [(k, v) for k, v in tab.ref.items()
                         if k[0] == fk and k[1] == unit]
                vetted = [(k, v) for k, v in cands
                          if v.get("usable") == "yes" and k[2]]
                pick = (max(vetted, key=lambda kv:
                            _f(kv[1].get("n_projects")) or 0)
                        if vetted else None)
                if pick is not None:
                    key, row, sub = pick[0], pick[1], "best-evidenced bucket"
                else:
                    key = (fk, unit, "")
                    row = tab.ref.get(key)
                    sub = "unsized bucket"
            return {"kind": "fam", "key": key, "row": row,
                    "substituted": sub} if row else None
        return None

    def references(self, payload, row_id):
        """The reference projects behind one line of an estimate request
        (the same request /api/estimate takes)."""
        self.require()
        if not self.refs_state["ready"]:
            raise EngineNotReady(self.refs_state["error"] or
                                 "the reference index is still loading")
        with self._lock:
            q, items, cls, r, to_year, target, bidders = self._run(payload)
            for i, c in enumerate(cls):
                if c["row_id"] == str(row_id):
                    return self._references_for(i, q, items, cls, r, to_year,
                                                target)
        raise BadRequest("no included line with row_id %r" % (row_id,))

    def top_references(self, request, top=3):
        """Every line's first `top` references (the export's sheet)."""
        self.require()
        out = []
        with self._lock:
            q, items, cls, r, to_year, target, bidders = self._run(request)
            for i, c in enumerate(cls):
                x = self._references_for(i, q, items, cls, r, to_year, target)
                out.append({"row_id": c["row_id"], "item_no": x["item_no"],
                            "description": x["description"],
                            "unit": x["unit"], "status": x["status"],
                            "text": x.get("text") or x.get("derivation"),
                            "warning": x.get("warning"),
                            "references": x["references"][:top]})
        return out

    def worksheet_refs(self, request, picks=6):
        """What the OPCC worksheet shows next to each line: the range of its
        reference projects' median-bidder unit prices escalated to the bid
        year (low / median / high, how many), the most recent one, and the
        projects to pick a price from. Read-only: nothing here is stored,
        and nothing the user picks or types ever comes back in. A line whose
        unit is thin in the history also gets `unit_hint` (unit_hints)."""
        self.require()
        out = []
        with self._lock:
            q, items, cls, r, to_year, target, bidders = self._run(request)
            hints = self.unit_hints(q, items, cls, r, to_year, target, bidders)
            for i, c in enumerate(cls):
                x = self._references_for(i, q, items, cls, r, to_year, target)
                refs = x.get("references") or []
                esc = [(rf.get("escalated_unit") or rf.get("median_unit"))
                       for rf in refs]
                esc = sorted(v for v in esc if v)
                am = [(rf.get("escalated_mean") or rf.get("mean_unit"))
                      for rf in refs]
                am = [v for v in am if v]
                latest = max(refs, key=lambda rf: rf["date"]) if refs else None

                def pick(rf):
                    f = rf.get("escalation") or None
                    return {"key": rf["key"], "project": rf["project"],
                            "owner": rf["owner"], "date": rf["date"],
                            "median_unit": rf.get("median_unit"),
                            "low_unit": rf.get("low_unit"),
                            "escalation": f,
                            "median_esc": (rf["median_unit"] * f)
                            if (f and rf.get("median_unit")) else rf.get("median_unit"),
                            "low_esc": (rf["low_unit"] * f)
                            if (f and rf.get("low_unit")) else rf.get("low_unit"),
                            "mean_unit": rf.get("mean_unit"),
                            "mean_esc": (rf["mean_unit"] * f)
                            if (f and rf.get("mean_unit")) else rf.get("mean_unit"),
                            "word_match": rf.get("word_match"),
                            "source": rf.get("source")}
                w = x.get("warning") or ""
                out.append({
                    "row_id": c["row_id"], "status": x.get("status"),
                    "special": x.get("special"), "to_year": to_year,
                    "n_refs": len(refs),
                    "range": ({"low": esc[0], "median": statistics.median(esc),
                               "high": esc[-1],
                               # the average of the references' average bids
                               "mean": (statistics.mean(am) if am else None)}
                              if esc else None),
                    "latest": pick(latest) if latest else None,
                    "picks": [pick(rf) for rf in refs[:picks]],
                    "generic": w.startswith("Generic match"),
                    "thin": w.startswith("Thin evidence"),
                    "warning": w or None,
                    "unit_hint": hints.get(i)})
        return {"lines": out, "to_year": to_year,
                "note": ("Reference range: the median-bidder unit prices of "
                         "the line's reference projects (public lettings "
                         "dated before the bid date), escalated to %s "
                         "dollars." % (to_year or "bid-year"))}

    def _references_for(self, i, q, items, cls, r, to_year, target):
        E = _E()
        t, it, c = r["trace"][i], items[i], cls[i]
        market = q["market"]
        el = r.get("expected_low")
        tot = r.get("market_total") or 0.0
        fx = r.get("fixed_total") or 0.0
        s50 = (el - fx) / tot if (el is not None and tot > 0) else None
        D = q["bid_date"] or _dt.date.today().isoformat()
        status = self.line_status(t, it)
        unit = it.get("unit") or ""
        base = {"row_id": c["row_id"], "item_no": it.get("item_no"),
                "description": it.get("description"), "unit": unit,
                "quantity": it.get("quantity"), "status": status,
                "bid_date": D, "to_year": to_year, "scale_to_p50": s50,
                "references": [], "top": 3}
        money = (lambda v: "${:,.2f}".format(v))
        if status == "owner_set":
            return dict(base, special="owner_set", text=(
                "Owner-set amount taken at face value (%s): the owner writes "
                "it into the bid form and every bidder copies it, so it is "
                "not estimated and no reference project lies behind it."
                % money(t["fixed_amount"])))
        if status in ("not_covered", "not_covered_whole"):
            return dict(base, special="not_covered", text=(
                "Not covered: %s has almost no comparable bid history, so it "
                "is not priced and not in P50 / P80. Get a vendor quote."
                % (c.get("coverage_label") or "this specialty item")))
        if status == "unpriced":
            return dict(base, special="unpriced", text=(
                "Not priced: %s. The totals leave this line out."
                % plain_why((t.get("why") or ["no reference cell"])[0])))
        pk = t.get("price_key")
        p50_unit = (t["price"] * s50) if s50 else None
        tail = (" At the P50 level the line is ×%.4f of that (the "
                "competition discount, the comparables blend and the "
                "centring, spread over the lines): %s/%s."
                % (s50, money(p50_unit), unit)) if s50 else ""
        if pk == "mobilization_ratio":
            rv, nl = (self.tab.t1.mob.at(D) if getattr(self.tab, "t1", None)
                      else (t.get("mobilization_ratio"), None))
            return dict(base, special="share", text=(
                "Priced as a share of the rest of this schedule (TxDOT rule "
                "T1): %.2f%% × %s, the schedule's other lines at market "
                "level; the share is the median over the bids of %s earlier "
                "TxDOT lettings.%s" % (100 * (t.get("mobilization_ratio") or 0),
                                       money(t.get("mobilization_base") or 0),
                                       nl if nl is not None else "the",
                                       tail)))
        if pk == "removal_typical_line":
            return dict(base, special="typical_line", text=(
                "Removal-only line with no removal history: priced at this "
                "schedule's typical line amount, %s (the median market "
                "amount of its directly priced lines), so no reference "
                "project lies behind it directly.%s"
                % (money(t["price"] * (it.get("quantity") or 1)), tail)))
        cell = self.trace_cell(t, it, market)
        if cell is None:
            return dict(base, special="no_cell", text=(
                "The estimator's reference cell for this line could not be "
                "identified; its notes: %s" % "; ".join(
                    english(w) for w in t.get("why") or [])))
        kind, key, row = cell["kind"], cell["key"], cell["row"]
        idx = self.refs.members(kind, key)
        ents = self.refs.entries(idx)
        published = _f(row.get("price_median"))
        from_rows = (statistics.median(e["median_unit"] for e in ents)
                     if ents else None)
        # the trace's own factors, in item_market_price's order
        price = published
        steps = []
        f1 = (cell.get("f1") or {}).get("factor")
        if f1:
            price *= f1
            steps.append("scaled ×%.3f for this line's size (%g vs the "
                         "cell's typical %.3g)" % (
                             f1, (cell["f1"] or {}).get("size") or 0,
                             (cell["f1"] or {}).get("size_ref") or 0))
        qf = t.get("qty_factor")
        if qf:
            price *= qf
            steps.append("×%.3f for quantity (this line's band %s vs "
                         "the cell's typical quantity)" % (qf, t.get(
                             "qty_band") or "?"))
        esc = t.get("escalation")
        vint = _f(t.get("vintage_year"))
        if esc:
            price *= esc
            steps.append("escalated ×%.3f from %s to %s dollars (%s "
                         "index)" % (esc, ("%.1f" % vint) if vint else "?",
                                     to_year, t.get("escalation_basis")))
        tp = t.get("price")
        ok_price = bool(tp and price and abs(price - tp) <= 1e-9 * abs(tp))
        ok_rows = bool(from_rows and published and
                       abs(from_rows - published) <= 1e-6 * abs(published))
        n_let = len({e["key"] for e in ents})
        years = sorted(int(float(e["year"])) for e in ents
                       if str(e["year"]).replace(".", "", 1).isdigit())
        span = (("%d" % years[0]) if years and years[0] == years[-1] else
                ("%d–%d" % (years[0], years[-1])) if years else "")
        what = {"tok": "items worded like this one",
                "band": "this item type in this quantity band",
                "fam": "this item type"}[kind]
        tier = {"tok": "word match" + (" (exact word set, rule T1)"
                                       if cell.get("exact") else ""),
                "band": "item type + size + quantity band",
                "fam": ("nearest sized cell, scaled for size" if f1 else
                        "item type median" + (" (%s)" % cell["substituted"]
                                           if cell.get("substituted")
                                           else ""))}[kind]
        head = ("%s/%s = the median of the median bids of %d earlier "
                "project%s for %s (%s), bid %s" % (
                    money(published), unit, n_let, "" if n_let == 1 else "s",
                    what, _cell_label(kind, key), span or "?"))
        if kind == "tok":
            head += ("; best of %d matching cell%s, word match %.2f"
                     % (cell["candidates"], "" if cell["candidates"] == 1
                        else "s", cell["jaccard"]))
        derivation = head + ("; " + "; ".join(steps) if steps else "") + \
            " = %s/%s at market level (all bids).%s" % (money(tp), unit, tail)
        warning = None
        if t.get("usable") == "unclassified" or \
                (it.get("family") or "") in ("other", "needs_review", ""):
            warning = ("Generic match: this line falls in a catch-all "
                       "group of unlike items, so these past prices say "
                       "little about it. Treat the price as a placeholder and "
                       "check the item type.")
        elif (_f(row.get("n_projects")) or 0) < 5:
            warning = ("Thin evidence: only %d earlier project%s behind this "
                       "price." % (n_let, "" if n_let == 1 else "s"))
        # the references, public lettings dated strictly before the bid date
        our_tok = E.item_tokens.tokens(it.get("description") or "")
        q0 = _f(it.get("quantity"), 0.0) or 0.0
        dD = _dt.date.fromisoformat(D)
        own = target.get("owner")
        esc_cache = {}
        refs, later, undated = [], 0, 0
        for e in ents:
            pkk = e["key"]
            date = self.panel.avail.get(pkk) or E.NEVER
            if date == E.NEVER:
                undated += 1
                continue
            if date >= D:
                later += 1
                continue
            y = e["year"]
            if y not in esc_cache:
                ee = (E.escalation(self.tab, y, to_year, market)
                      if (y and to_year) else None)
                esc_cache[y] = (ee or {}).get("factor")
            f = esc_cache[y]
            j = E.item_tokens.jaccard(our_tok, E.item_tokens.tokens(e["dn"]))
            age = max(0.0, (dD - _dt.date.fromisoformat(date)).days / 365.25)
            qe = e["quantity"]
            qclose = (math.exp(-0.5 * abs(math.log(qe / q0)))
                      if (qe and q0 > 0) else 0.5)
            oid = (upstream.comparables.owner_id(pkk, market, "")
                   if market == "municipal" else None)
            same_owner = bool(own and oid == own)
            score = (2.0 ** (-age / 3.0)) * (0.2 + 0.8 * j) * qclose * \
                (1.5 if same_owner else 1.0)
            meta = self.panel.meta.get(pkk) or {}
            tots = self.panel.totals.get(pkk) or []
            bu = [u for _b, u, _q, _w in e["bids"] if u and u > 0]
            mean_u = (sum(bu) / len(bu)) if bu else None
            refs.append({
                "key": pkk, "project": project_title(pkk, market),
                "title": meta.get("title") or "",
                "owner": (owner_name(oid) if oid else
                          (("TxDOT, %s County" % meta["county"])
                           if meta.get("county") else "TxDOT")),
                "same_owner": same_owner,
                "date": date, "year": y,
                "bidders": len(tots) or None,
                "low_total": self.panel.winner_total.get(pkk) or
                (tots[0] if tots else None),
                "item": self.panel.rawdesc.get((pkk, e["dn"])) or e["dn"],
                "quantity": qe, "median_unit": e["median_unit"],
                "low_unit": e["low_unit"], "min_unit": e["min_unit"],
                "max_unit": e["max_unit"], "n_bids": len(e["bids"]),
                # the average bid: what an OPCC's unit cost source usually
                # quotes (user's team, 2026-11-21); display only
                "mean_unit": mean_u,
                "escalation": f,
                "escalated_unit": (e["median_unit"] * f) if f else None,
                "escalated_mean": (mean_u * f) if (f and mean_u) else None,
                "word_match": j, "score": score,
                "bids": [{"bidder": (b or "").title(), "unit_price": u,
                          "quantity": qq, "low": bool(w)}
                         for b, u, qq, w in sorted(e["bids"],
                                                   key=lambda x: x[1])],
                "match": [{"dn": d, "unit": e["unit"], "quantity": qq}
                          for d, qq in sorted(e["lines"], key=lambda x: (
                              x[0], x[1] if x[1] is not None else -1))],
                "source": self.sources.get(pkk, meta)})
        refs.sort(key=lambda x: x["date"], reverse=True)    # newer first on ties
        refs.sort(key=lambda x: -x["score"])
        note = None
        if later or undated:
            note = ("The median price also counts %s, not listed here: the "
                    "price tables use every public bid, while this list shows "
                    "only projects dated before your bid date." % (
                        " and ".join(x for x in (
                            ("%d project%s bid on or after your bid date"
                             % (later, "" if later == 1 else "s"))
                            if later else "",
                            ("%d undated project%s" % (undated, "" if
                                                       undated == 1 else "s"))
                            if undated else "") if x)))
        return dict(base, tier=tier, kind=kind, derivation=derivation,
                    warning=warning, note=note,
                    cell={"label": _cell_label(kind, key),
                          "lettings": n_let,
                          "n_projects": _f(row.get("n_projects")),
                          "usable": row.get("usable"), "years": span},
                    reproduced={"published_cell_price": published,
                                "cell_price_from_rows": from_rows,
                                "rows_match": ok_rows,
                                "price": price, "trace_price": tp,
                                "price_match": ok_price},
                    market_unit_price=tp, p50_unit_price=p50_unit,
                    references=refs, hidden_later=later,
                    hidden_undated=undated)

    # -- the status block ----------------------------------------------------------
    def status(self):
        return {"state": dict(self.state), "info": self.info,
                "references": dict(self.refs_state),
                "item_accuracy": self.accuracy.meta()}
