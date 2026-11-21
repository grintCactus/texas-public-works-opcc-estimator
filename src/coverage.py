"""Which schedule lines the estimator does NOT cover (user decision 2026-11-20).

Version 2 (2026-11-20 ~11:00): five meaning fixes, see the v2 notes below;
v1 was md5 5d14769b (handoff/scratchpad/coverage_scope/coverage_v1_5d14769b.py).

The user: "If we run into pumps, generators, prefab bridges -- types we have almost nothing
comparable for -- we say they are not covered, and we flag that part when we give the quote ...
any price you give for something not covered is unreasonable ...
when we judge ... take out the not-covered part and see how accurate the price is."

So a line in one of these classes is NOT priced at all:
  - the quote's P50/P80 cover the other lines only;
  - the uncovered lines are listed by name as "Not covered -- get a separate quote";
  - accuracy is judged on the covered part: the estimate of the covered lines
    against the low bid minus the low bidder's own amounts on the uncovered lines.

Why these classes: line-by-line diagnosis of the worst municipal estimates
(reports/diag_worst_municipal_2026-11-20.md) found about 1/6 of the low-tail
gap by dollars (26% equal-weighted) in truly novel specialty items with no bid
history: 250 HP vertical turbine pumps ($2.2k vs $450k each), a 2000 kW
standby generator ($33k vs $1.78M), 90-ft prefabricated pedestrian truss
bridges ($2.3k vs $419k), whole lift-station LS lines, synthetic turf,
meter vaults.  Pricing them from neighbouring descriptions (round 6, O1) did
not help.

The rule is fixed here, from the item classes, BEFORE any scoring under it.
It reads only what a schedule shows at estimate time: the normalised
description, the family and the unit.  Never the bids.

A line is UNCOVERED when
  1. its description names a specialty class below, and
  2. it reads as the equipment / structure itself, i.e. it is in a catch-all
     or equipment family, OR it is priced per LS / EA, and
  3. it is not an ordinary civil item AT such a facility (pad, foundation,
     slab, fence, paving, pipe, sidewalk, ...), not a pure removal / demolition,
     and not temporary bypass pumping or dewatering, which have bid history.
"""
import re

# ---- specialty classes ------------------------------------------------------
UNCOVERED_CLASSES = [
    ("pump_equipment",
     r"\bpumps?\b|\bpumping\s+(unit|equipment|system)s?\b|\bpump\s+station\b|"
     r"\blift\s+station\b|\bbooster\s+(pump\s+)?station\b|\bwet\s+well\b"),
    ("generator_electrical_gear",
     r"\bgenerators?\b|\bgenset\b|\bmotor\s+control\s+cent(er|re)s?\b|\bmcc\b|"
     r"\bvfds?\b|\bvariable\s+frequency\s+drives?\b|\bswitchgear\b|\bscada\b|"
     r"\bautomatic\s+transfer\s+switch\w*\b"),
    ("prefab_or_pedestrian_bridge",
     r"\b(prefab\w*|prefb|pre\s+fab\w*|pre\s+engineered|pedestrian|ped|steel\s+truss|"
     r"stl\s+truss|truss|foot)\s*bridges?\b|\bfootbridges?\b|"
     r"\bbridges?\s+(prefab\w*|prefb|pedestrian|truss)\b"),   # v2: adjacent only
    ("tank_or_process",
     r"\b(elevated|ground|storage|water)\s+(storage\s+)?tanks?\b|\bclarifiers?\b|"
     r"\b(aeration|process)\s+blowers?\b|\bblowers?\s+(buildings?|units?|systems?|packages?)\b|"
     r"\bdigesters?\b|\btreatment\s+plant\b|\bwwtp\b|\bwtp\b|"
     r"\bchlorin\w*\s+(system|station|feed\w*|building)\b"),
    ("building_or_structure",
     r"\bpre\s*engineered\s+(metal\s+)?buildings?\b|\brestroom\s+(building|facility)\b|"
     r"\b(control|electrical|pump)\s+buildings?\b"),
    ("synthetic_turf_or_play",
     r"\b(synthetic|artificial)\s+turf\b|\bplayground\s+equipment\b|\bplay\s+structures?\b"),
    ("meter_vault",
     r"\bmeter\s+vaults?\b"),
]
_CLASSES = [(n, re.compile(p)) for n, p in UNCOVERED_CLASSES]

# the item is the equipment / structure itself only in these families or units
EQUIPMENT_FAMILIES = {"other", "", "needs_review", "testing_inspection",
                      "electrical_lighting", "lighting_assembly", "traffic_signal",
                      "mobilization", "general_conditions"}
EQUIPMENT_UNITS = {"LS", "L.S.", "LUMP SUM", "EA", "EACH"}

# ordinary civil work at such a facility stays covered
_CIVIL_AT_FACILITY = re.compile(
    r"\b(pads?|foundations?|slabs?|fenc\w*|paving|pavement|driveways?|access\s+road|"
    r"sidewalks?|curbs?|pipes?|piping|force\s+main|water\s+line|sewer\s+line|"
    r"manholes?|fittings?|valves?|conduit|duct\s+bank|grading|seeding|sod|"
    r"rip\s*rap|drilled\s+shafts?|piers?|abutments?|approach\w*|walls?|edging|"
    r"trench\s+safety|coordinat\w*|allowance|heat\s+pumps?|mowing)\b")
_TEMPORARY_PUMPING = re.compile(r"\bbypass\b|\bby\s+pass\b|\bdewater\w*\b|\btemporary\s+pump\w*\b")
# v2 (2026-11-20 ~11:00, from the integration review, before the round-10 re-run):
#  - a temporary / rental generator is a rented item with bid history, like bypass pumping;
#  - relocating / resetting / reinstalling existing equipment is not the equipment itself;
#  - contingency / unforeseen / allowance wording is owner-set money, not equipment;
#  - railing, painting or cleaning ON a bridge is ordinary work on it;
#  - HVAC / air conditioning inside a building is a component, not the building.
_TEMPORARY_GEAR = re.compile(r"\btemporary\b|\btemp\b|\brental\b|\brent\b")
_RELOCATE = re.compile(r"\brelocat\w*\b|\breset\w*\b|\bre\s?install\w*\b|\bsalvag\w*\b")
_OWNER_MONEY = re.compile(r"\bcontingenc\w*\b|\bcontignenc\w*\b|\bunforeseen\b|\bunforseen\b|\ballowances?\b")
_BRIDGE_WORK = re.compile(r"\brail\w*\b|\bpaint\w*\b|\bclean\w*\b|\bcoating\b|\bwash\w*\b")
_BUILDING_PART = re.compile(r"\bhvac\b|\bair\s+condition\w*\b")
_REMOVAL = re.compile(r"^(?:remov\w*|demolish\w*|demolition|abandon\w*|dispos\w*)\b|"
                      r"\b(?:remove|demolish)\s+(?:and\s+dispose\s+of\s+)?(?:the\s+)?existing\b")


def uncovered_class(desc_norm, family="", unit=""):
    """The specialty class name when the line is not covered, else None."""
    d = desc_norm.lower() if isinstance(desc_norm, str) else ""
    family = family if isinstance(family, str) else ""
    unit = unit if isinstance(unit, str) else ""
    if not d:
        return None
    hit = None
    for name, rx in _CLASSES:
        if rx.search(d):
            hit = name
            break
    if hit is None:
        return None
    fam = (family or "").lower()
    if fam.startswith("demo_") or _REMOVAL.search(d):
        return None
    if hit == "pump_equipment" and _TEMPORARY_PUMPING.search(d):
        return None
    if fam in ("contingency", "relocate_reset") or _OWNER_MONEY.search(d) or _RELOCATE.search(d):
        return None
    if hit == "generator_electrical_gear" and _TEMPORARY_GEAR.search(d):
        return None
    if hit == "prefab_or_pedestrian_bridge" and _BRIDGE_WORK.search(d):
        return None
    if hit == "building_or_structure" and _BUILDING_PART.search(d):
        return None
    u = (unit or "").upper().strip()
    if fam not in EQUIPMENT_FAMILIES and u not in EQUIPMENT_UNITS:
        return None
    if _CIVIL_AT_FACILITY.search(d):
        return None
    return hit


def is_uncovered(desc_norm, family="", unit=""):
    return uncovered_class(desc_norm, family, unit) is not None


_CASES = [
    # (desc_norm, family, unit, expected class or None)
    ("furnish and install 250 hp vertical turbine pumps", "other", "EA", "pump_equipment"),
    ("complete tw king pump and electrical improvements", "electrical_lighting", "LS", "pump_equipment"),
    ("lift station rehabilitation", "other", "LS", "pump_equipment"),
    ("2000 kw diesel standby generator procurement and installation", "other", "LS",
     "generator_electrical_gear"),
    ("scada system integration", "other", "LS", "generator_electrical_gear"),
    ("prefb ped stl truss bridge span 90 ft", "other", "EA", "prefab_or_pedestrian_bridge"),
    ("prefabricated pedestrian bridge 196 ft", "other", "EA", "prefab_or_pedestrian_bridge"),
    ("install synthetic turf w/ dual layer backing", "other", "SY", "synthetic_turf_or_play"),
    ("48 in water meter vault", "other", "LS", "meter_vault"),
    ("ground storage tank 1 0 mg", "other", "LS", "tank_or_process"),
    # stays covered
    ("bypass pumping", "other", "LS", None),
    ("temporary bypass pumping for sewer", "other", "LS", None),
    ("dewatering", "other", "LS", None),
    ("remove existing generator", "demo_other", "LS", None),
    ("generator pad concrete", "concrete_misc", "EA", None),
    ("pump station site paving 8 in concrete", "conc_pavement", "SY", None),
    ("24 in water line to pump station", "water_pipe", "LF", None),
    ("chain link fence at lift station", "fence", "LF", None),
    ("bridge rail ty t221", "guardrail", "LF", None),
    ("pedestrian bridge drilled shaft 36 in", "other", "LF", None),
    ("8 in pvc sanitary sewer", "sanitary_pipe", "LF", None),
    ("mobilization", "mobilization", "LS", None),
    ("limestone boulder retaining walls at pavilion for amphitheater", "retaining_wall", "LS", None),
    ("mowing trimming of an alley includes use of blower to clear alley", "needs_review", "EA", None),
    ("1 0 mg elevated storage tank trench safety", "trench_safety", "LS", None),
    ("allowance for scada system integration", "contingency", "LS", None),
    ("replace full hvac system with heat pump system", "other", "", None),
    ("paint interior wet coating system for 1 500 000 gallon elevated storage tank", "other", "LS",
     "tank_or_process"),
    ("aeration blowers", "other", "EA", "tank_or_process"),
    # v2 exemptions
    ("clean and paint existing bridge railing both sides per city pedestrian standards", "other", "LS", None),
    ("temporary generator", "other", "LS", None),
    ("relocate existing generator and transfer switch", "relocate_reset", "LS", None),
    ("relocate existing truss bridge", "other", "LS", None),
    ("lift station contingency", "other", "LS", None),
    ("electrical building hvac", "other", "LS", None),
    ("bridge prefabricated pedestrian 120 ft", "other", "EA", "prefab_or_pedestrian_bridge"),
]


def selftest():
    fails = 0
    for d, f, u, want in _CASES:
        got = uncovered_class(d, f, u)
        if got != want:
            fails += 1
            print("FAIL %r (%s, %s): got %r want %r" % (d, f, u, got, want))
    print("coverage selftest: %d cases, %d failure(s)" % (len(_CASES), fails))
    return fails == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
