"""Build ui/item_accuracy.json -- the per-line accuracy hint the UI shows.

Reads the line-accuracy outputs of the forward back-test and writes, per
(market, item group), how close the estimator's line price has been to the
median bid on the same line:

    half_within      half of the lines were within this of the median bid
    dollar_weighted  the same, weighted by dollars
    ratio            median of (our line price / median bid); > 1 runs high
    within_20        share of lines within +-20%
    low_vs_median    how far the low bidder typically is from the median
                     bidder on the same line (the bidders' own spread)
    lines, lettings, share_of_dollars

The groups, filters and statistics are the ones the project README's
"Item-level accuracy" tables use (handoff/scratchpad/line_accuracy/
gen_readme_items.py), so a badge in the UI quotes the figure the README
publishes for that item:

  * windows: municipal lettings 2024-26, TxDOT 2023-26 (forward in time);
  * lines with >= 3 bidders, not owner-set, not unpriced (uncovered lines
    are not in the input at all);
  * group = "mobilization", "LS:<class>" for a general-condition lump sum
    (traffic control, site prep, SWPPP / erosion control, bonds), else the
    item family -- estimate_low_bid.lowconf_class() decides, as the
    generator's klass() does;
  * a group is "published" (the README lists it, the UI quotes it) with
    >= 40 lines in >= 15 lettings.

Regenerate after every adopted estimator change or data rebuild, once the
main session has re-run the line-accuracy pass (fwd_rec.py, join_mob.py):

    .venv/bin/python ui/build_item_accuracy.py
    .venv/bin/python ui/build_item_accuracy.py LINES_CSV FWD_REC_JSON

The group labels are read from gen_readme_items.py itself (its LAB and
TX_LAB dicts, parsed, not executed), so a new family gets the README's name
without an edit here. After writing, every published group is checked
against the README's own item tables (label, lines, lettings and every
percentage as the README prints them); a mismatch is reported and the exit
code is 1.

Reads only; writes ui/item_accuracy.json and nothing else. The inputs hold
public (external) lettings only.
"""

from __future__ import annotations

import ast
import collections
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACC_DIR = os.path.join(ROOT, "handoff", "scratchpad", "line_accuracy", "out")
DEFAULT_LINES = os.path.join(ACC_DIR, "mob_lines_29.csv")
DEFAULT_FWD = os.path.join(ACC_DIR, "fwd_rec_29.json")
GEN_README = os.path.join(os.path.dirname(ACC_DIR), "gen_readme_items.py")
README = os.path.join(ROOT, "README.md")
OUT = os.path.join(HERE, "item_accuracy.json")

MIN_LINES, MIN_LETTINGS = 40, 15
WINDOW_START = {"municipal": "2024", "txdot": "2023"}
WINDOW_LABEL = {"municipal": "2024-26", "txdot": "2023-26"}

# The README's labels (gen_readme_items.py), so the UI and the README name a
# group the same way.
LABELS = {
    "barrier_free_ramp": "ADA curb ramps", "driveway": "Driveways",
    "project_sign": "Project signs", "inlet_structure": "Inlets",
    "sidewalk": "Sidewalk", "signing": "Signs",
    "conc_pavement": "Concrete pavement", "water_service": "Water services",
    "storm_pipe": "Storm pipe", "box_culvert": "Box culverts",
    "flex_base": "Flexible base", "embankment_fill": "Embankment / fill",
    "demo_pavement": "Pavement removal", "demo_sidewalk": "Sidewalk removal",
    "drill_shaft": "Drilled shafts", "riprap_stone": "Stone riprap",
    "valve_hydrant": "Valves and hydrants", "conc_beam": "Concrete beams",
    "excavation": "Excavation", "curb_gutter": "Curb and gutter",
    "subgrade_stab": "Subgrade stabilization", "tree_removal": "Tree removal",
    "erosion_control": "Erosion control (unit-priced)",
    "electrical_lighting": "Electrical and lighting",
    "traffic_signal": "Traffic signals",
    "testing_inspection": "Testing and inspection",
    "asphalt_pavement": "Asphalt pavement", "water_pipe": "Water pipe",
    "seeding_sod": "Seeding and sod", "demo_other": "Other removals",
    "concrete_misc": "Miscellaneous concrete",
    "encasement": "Casing / encasement", "sanitary_pipe": "Sanitary sewer pipe",
    "guardrail": "Guardrail", "manhole": "Manholes",
    "demo_utility": "Utility removal / abandonment",
    "traffic_control": "Traffic control (unit-priced)",
    "tree_install": "Tree planting", "fence": "Fence",
    "pipe_fittings": "Pipe fittings", "sewer_service": "Sewer services",
    "headwall": "Headwalls", "relocate_reset": "Relocate / reset",
    "trench_safety": "Trench safety",
    "contingency": "Contingency / allowance lines",
    "pavement_marking": "Pavement marking", "retaining_wall": "Retaining walls",
    "demo_structure": "Structure demolition", "other": "Unclassified lines",
    "LS:swppp_erosion": "SWPPP / temporary erosion control (lump sum)",
    "LS:site_prep": "Site preparation (lump sum)",
    "LS:traffic_control": "Traffic control (lump sum)",
    "LS:bonds_insurance": "Bonds and insurance (lump sum)",
    "irrigation": "Irrigation", "mobilization": "Mobilization",
    "construction_exit": "Construction exits",
    "conc_mow_strip": "Concrete mow strip",
    "construction_exit_remove": "Construction exit removal",
    "riprap_conc": "Concrete riprap", "flowable_backfill": "Flowable backfill",
    "railing": "Bridge railing", "lighting_assembly": "Lighting assemblies",
    "wingwall": "Wingwalls", "pavement_milling": "Pavement milling",
    "borrow_backfill": "Borrow / backfill",
    "safety_end_treatment": "Safety end treatments",
    "conc_joints": "Concrete joints", "conc_barrier": "Concrete barrier",
    "shoring": "Shoring", "surface_treatment": "Surface treatment",
    "mowing_vegetation": "Mowing", "crash_cushion": "Crash cushions",
    "cleaning_desilting": "Cleaning / desilting",
    "site_prep_clearing": "Site preparation / clearing",
}
LABELS.update({
    "geotextile": "Geotextile / filter fabric",
    "landscape_planting": "Landscape planting",
    "landscape_material": "Landscape materials (mulch, compost, stone)",
    "bypass_pumping": "Bypass pumping", "adjust_to_grade": "Adjust to grade",
    "concrete_repair": "Concrete repair", "bridge_concrete": "Bridge concrete",
    "fire_hydrant": "Fire hydrants", "valve_assembly": "Valve assemblies",
    "trm": "Turf reinforcement mat", "gabion_wall": "Gabion walls",
    "gabion_mattress": "Gabion mattresses",
    "rock_anchor": "Soil nails / rock anchors", "pipe_bursting": "Pipe bursting",
    "sawcut": "Saw cutting", "painting": "Painting", "pavers": "Pavers",
    "manhole_extra_depth": "Manhole extra depth",
    "flowable_fill": "Flowable fill", "grading": "Grading",
    "cleaning": "Cleaning", "railing_misc": "Railing", "mowing": "Mowing",
    "shoring_misc": "Shoring",
})
TX_LABELS = {"traffic_control": "Traffic control (per month)",
             "site_prep_clearing": "Preparing right of way / clearing"}


def readme_labels(path=GEN_README):
    """(LAB, TX_LAB) from the README generator, parsed with ast -- never
    executed (it reads sys.argv at import). None when it is not there."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)
    except (OSError, SyntaxError):
        return None
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and \
                node.targets[0].id in ("LAB", "TX_LAB"):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                return None
    if "LAB" not in found:
        return None
    return found["LAB"], found.get("TX_LAB", {})


_GEN = readme_labels()
if _GEN:
    LABELS = dict(LABELS, **_GEN[0])
    TX_LABELS = dict(TX_LABELS, **_GEN[1])


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _wmed(pairs):
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _rel(path):
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def group_of(klass, gc_sub, family):
    """The generator's group key for one recorded line."""
    if klass == "mobilization":
        return "mobilization"
    if klass == "gc_ls":
        return "LS:" + (gc_sub or "?")
    return family or "(none)"


def _stats(v, tot):
    a = [abs(x[0]) for x in v]
    lm = [abs(x[3]) for x in v if x[3] is not None]
    return {
        "lines": len(v),
        "lettings": len({x[2] for x in v}),
        "share_of_dollars": (sum(x[1] for x in v) / tot) if tot else None,
        "half_within": math.exp(st.median(a)) - 1,
        "dollar_weighted": math.exp(_wmed([(abs(x[0]), x[1]) for x in v])) - 1,
        "ratio": math.exp(st.median([x[0] for x in v])),
        "within_20": sum(1 for t in a if t <= math.log(1.2)) / len(a),
        "low_vs_median": (math.exp(st.median(lm)) - 1) if lm else None,
    }


def build(lines_csv, fwd_json):
    fwd = json.load(open(fwd_json, encoding="utf-8"))
    market_of = {r["project_key"]: r["market"] for r in fwd["per_letting"]}
    groups = collections.defaultdict(list)
    csv.field_size_limit(10 ** 9)
    with open(lines_csv, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            m = market_of.get(r["project_key"])
            if m not in WINDOW_START:
                continue
            if r["date"][:4] < WINDOW_START[m]:
                continue
            o, md = _f(r["ours_mkt"]), _f(r["med_amt"])
            lo, nb = _f(r["low_amt"]), _f(r["n_bidders_line"])
            if not o or not md or o <= 0 or md <= 0 or (nb or 0) < 3:
                continue
            if _f(r["fixed"]) or (r["unpriced"] not in ("", "0", "0.0",
                                                        "False")):
                continue
            g = group_of(r["klass"], r["gc_sub"], r["family"])
            x = (math.log(o / md), md, r["project_key"],
                 math.log(lo / md) if lo and lo > 0 else None)
            groups[(m, g)].append(x)
            groups[(m, "ALL")].append(x)

    markets = {}
    for m in ("municipal", "txdot"):
        allv = groups.get((m, "ALL")) or []
        if not allv:
            continue
        tot = sum(x[1] for x in allv)
        out_g = {}
        for (mm, g), v in sorted(groups.items()):
            if mm != m or g == "ALL":
                continue
            s = _stats(v, tot)
            s["label"] = ((TX_LABELS.get(g) if m == "txdot" else None)
                          or LABELS.get(g, g))
            s["published"] = (s["lines"] >= MIN_LINES
                              and s["lettings"] >= MIN_LETTINGS)
            out_g[g] = s
        a = _stats(allv, tot)
        a["label"] = "All lines"
        markets[m] = {"window": WINDOW_LABEL[m], "all": a, "groups": out_g}

    return {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": {
            "lines_csv": _rel(lines_csv), "lines_csv_md5": _md5(lines_csv),
            "fwd_rec_json": _rel(fwd_json), "fwd_rec_json_md5": _md5(fwd_json),
            "estimator_md5": fwd.get("md5_estimator"),
            "master_panel_md5": fwd.get("md5_master_panel"),
        },
        "method": (
            "Forward in time: every line of every scored letting priced from "
            "earlier lettings only, at market level (before the comparables "
            "blend and the competition discount), against the MEDIAN "
            "bidder's amount on the same line; lines with >= 3 bidders, not "
            "owner-set, not unpriced, not uncovered."),
        "thresholds": {"min_lines": MIN_LINES, "min_lettings": MIN_LETTINGS},
        "markets": markets,
    }


def _pct(v):
    """The README's percentage format (gen_readme_items.pct)."""
    return ">1000%" if v > 10 else "%.0f%%" % (100 * v)


def check_readme(doc, readme=README):
    """Every published group against the README's item tables: the same
    groups, and per group the label, lines, lettings and every figure as the
    README prints it. Returns a list of differences (empty: they match)."""
    try:
        text = open(readme, encoding="utf-8").read()
    except OSError as e:
        return ["README not readable: %s" % e]
    diffs = []
    for m, head in (("municipal", "City / municipal (DFW)"),
                    ("txdot", "TxDOT (DFW counties)")):
        mm = re.search(r"^#+ %s, [^\n]*\n(.*?)(?=^#|\Z)" % re.escape(head),
                       text, re.S | re.M)
        if not mm:
            diffs.append("%s: no table in the README" % m)
            continue
        rows = {}
        for ln in mm.group(1).splitlines():
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) == 9 and cells[1].isdigit():
                rows[cells[0]] = cells[1:]
        d = (doc["markets"].get(m) or {}).get("groups", {})
        mine = {}
        for g in d.values():
            if g["published"]:
                mine[g["label"]] = [
                    str(g["lines"]), str(g["lettings"]),
                    "%.1f%%" % (100 * g["share_of_dollars"]),
                    _pct(g["half_within"]), _pct(g["dollar_weighted"]),
                    "%.2f" % g["ratio"], _pct(g["within_20"]),
                    _pct(g["low_vs_median"]) if g["low_vs_median"] is not None
                    else ""]
        for lab in sorted(set(rows) | set(mine)):
            if lab not in rows:
                diffs.append("%s: %r is published here, not in the README" % (m, lab))
            elif lab not in mine:
                diffs.append("%s: %r is in the README, not published here" % (m, lab))
            elif rows[lab] != mine[lab]:
                diffs.append("%s: %r README %s vs here %s" % (m, lab, rows[lab], mine[lab]))
    return diffs


def main(argv):
    lines_csv = argv[1] if len(argv) > 2 else DEFAULT_LINES
    fwd_json = argv[2] if len(argv) > 2 else DEFAULT_FWD
    for p in (lines_csv, fwd_json):
        if not os.path.exists(p):
            print("missing input: %s" % p)
            return 1
    doc = build(lines_csv, fwd_json)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, OUT)
    for m, d in doc["markets"].items():
        pub = sum(1 for g in d["groups"].values() if g["published"])
        print("%-9s %s: %d lines, %d groups (%d published), all lines: half "
              "within %.0f%%" % (m, d["window"], d["all"]["lines"],
                                 len(d["groups"]), pub,
                                 100 * d["all"]["half_within"]))
    print("-> %s" % _rel(OUT))
    diffs = check_readme(doc)
    if diffs:
        print("README item tables differ in %d place(s):" % len(diffs))
        for x in diffs[:40]:
            print("  " + x)
        return 1
    n = sum(1 for d in doc["markets"].values() for g in d["groups"].values()
            if g["published"])
    print("README check: all %d published groups match the README's item "
          "tables" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
