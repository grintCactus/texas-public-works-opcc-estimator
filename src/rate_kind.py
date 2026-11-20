"""What KIND of price is this number -- a unit rate, or something else.

`price_basis` already exists and means something different (all_bids vs
contract_price vs trimmed_mean), so this is a separate column: `rate_kind`.

The need is measured.  Annual service and maintenance contracts are let the
same way as construction and tabulated on the same forms, so they arrive in the
same pool, and the prices in them are not unit prices for work:

    legistar/mckinney/2025-09-08_25-3234
      "Storm drain cleaning. State hourly rate to include all services"  $495
      -> item_family = storm_pipe, project_kind = construction
    mesquite/2021-050, mesquite/2023-053
      mowing and trimming, priced per residential lot and per hour

Today the damage is contained, and by luck rather than by design: those rows
carry no unit at all, so they land in `storm_pipe/""` -- 91 rows -- while the
real `storm_pipe/LF` (10,478 rows, median $120/LF) is untouched, because every
reference table is keyed on (family, UNIT).  791 such rows are in the fitting
pool, 0.13% of it, and none reaches a published reference group.  So this
column is prophylactic: it records WHY a row is not a unit rate, instead of
leaving a future reader to rediscover it when a group finally clears the
sample minimum.

WHAT IS DELIBERATELY NOT CLASSIFIED AS A SERVICE RATE: a unit that merely
happens to be a period.  "Traffic Control, per month" and "Dewatering, per
day" are ordinary construction items that are priced by time; so is
"Trench Safety, per LF".  The unit alone therefore decides nothing -- the
description has to say it is a rate for labour or for a recurring service.

A CASE THIS MODULE CANNOT DECIDE, stated rather than papered over:

    "Mowing, trimming/edging of standard vacant residential lot"   unit LOT

That is a line in a term maintenance contract -- mow every fortnight for a
year -- but an identical line appears in construction tabs as site
restoration, mown once.  Nothing IN THE LINE separates the two; what separates
them is the contract, which is a project-level fact and belongs to whatever
classifies project_kind, not here.  A rule keyed on unit LOT would be guessing
from a unit used by 45 rows in 4 files, three of them the same mowing
contracts, so it would encode those files rather than a principle.  Left as
`unit_rate`, and the exposure is small for an unrelated reason: mowing has its
own item_family, so a service-contract mowing price pools only with other
mowing prices, never with construction work.
"""

import re

UNIT_RATE = "unit_rate"
LUMP_SUM = "lump_sum"
HOURLY = "hourly_rate"
PERIODIC = "periodic_service"
PERCENT = "pct_of_cost"
NO_UNIT = "no_unit"

HOUR_UNITS = {"HR", "HOUR", "HOURS", "HRS", "MH", "MANHOUR"}
LUMP_UNITS = {"LS", "LSUM", "LUMP"}
PCT_UNITS = {"PCT", "%"}
# A period unit is only a signal when the description agrees; see the module
# docstring for why the unit cannot decide on its own.
PERIOD_UNITS = {"MO", "MON", "MONTH", "WK", "WEEK", "DAY", "CYC", "CYCLE",
                "YR", "YEAR"}

_HOURLY_RE = re.compile(
    r"hourly\s+rate|rate\s+per\s+hour|per\s+hour|/\s*hr\b|price\s+per\s+hour"
    r"|straight\s+time|overtime\s+rate|labor\s+rate|labour\s+rate", re.I)
_SERVICE_RE = re.compile(
    r"per\s+(?:month|week|day|cycle|visit|occurrence|call|lot|each\s+lot)"
    r"|per\s+mowing|monthly\s+rate|annual\s+(?:contract|rate)|as[-\s]?needed"
    r"|on[-\s]?call|call[-\s]?out\s+rate|unit\s+price\s+contract", re.I)
_PCT_RE = re.compile(r"percent\s+of|%\s*of\s+(?:cost|contract|bid)", re.I)


def classify(description, unit):
    """Return one of the constants above.  Order matters: the explicit
    statements in the description outrank the unit, because a unit is a label
    somebody typed into a column header and the description is the scope."""
    d = description or ""
    u = (unit or "").strip().upper()

    if _PCT_RE.search(d) or u in PCT_UNITS:
        return PERCENT
    if u in HOUR_UNITS or _HOURLY_RE.search(d):
        return HOURLY
    if _SERVICE_RE.search(d):
        return PERIODIC
    if u in LUMP_UNITS:
        return LUMP_SUM
    if not u:
        # No unit means no unit price, whatever the row looks like otherwise.
        # Kept distinct from the service kinds: it is a missing field, not a
        # statement about the work, and the two should not be confused when
        # someone later decides what to exclude.
        return NO_UNIT
    if u in PERIOD_UNITS:
        # Only with corroboration from the description, which the branches
        # above already tested; reaching here means there is none, so this is
        # an ordinary construction item priced by time.
        return UNIT_RATE
    return UNIT_RATE


def classify_frame(desc, unit):
    """Vectorised enough for the panel: a plain map over two Series."""
    return [classify(d, u) for d, u in zip(desc.fillna("").astype(str),
                                           unit.fillna("").astype(str))]


_CASES = [
    # Measured on the live corpus -- these are real descriptions, not invented.
    ("Storm drain cleaning. State hourly rate to include all services", "",
     HOURLY),
    ("Inspection services. State hourly rate to include all services", "",
     HOURLY),
    ("Minor Miscellaneous work on residential or commercial lot, price per hour",
     "HR", HOURLY),
    ("Construction Staking, 5 percent of contract", "LS", PERCENT),
    ("Mobilization", "LS", LUMP_SUM),
    # Period units with no corroborating description: ordinary construction
    # items that happen to be priced by time.  If these ever come back as a
    # service kind, a unit-only rule has crept in.
    ("Traffic Control", "MO", UNIT_RATE),
    ("Dewatering", "DAY", UNIT_RATE),
    ("Trench Safety", "LF", UNIT_RATE),
    ("Grouted Rock Riprap", "CY", UNIT_RATE),
    ("PVC Coated Gabion Baskets", "CY", UNIT_RATE),
    ("Turf Reinforcement Mat with Sod", "SY", UNIT_RATE),
    # The case this module cannot decide; see the docstring.  Pinned as
    # unit_rate on purpose, so that a later unit-LOT rule shows up as a test
    # change and has to be argued for rather than slipping in.
    ("Mowing, trimming/edging of standard vacant residential or commercial lot",
     "LOT", UNIT_RATE),
    ("Mowing of detention pond, per mowing cycle", "CYC", PERIODIC),
    ("Hauling, as-needed", "TON", PERIODIC),
    ("Some item with no unit at all", "", NO_UNIT),
]


def _selftest():
    bad = 0
    for d, u, want in _CASES:
        got = classify(d, u)
        if got != want:
            bad += 1
            print("FAIL unit=%-6s got=%-17s want=%-17s %s"
                  % (u or "(none)", got, want, d[:52]))
    print("%d cases, %d failures" % (len(_CASES), bad))
    return bad == 0


if __name__ == "__main__":
    _selftest()
