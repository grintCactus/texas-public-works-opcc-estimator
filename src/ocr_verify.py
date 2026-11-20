"""Score how far each OCR'd tabulation can be trusted, using its own arithmetic.

The problem
-----------
`src/ocr_scans.py` recovers text from 18 scans that have no text layer, but OCR
gets digits wrong in exactly the place that matters. Observed damage, all of it
systematic rather than random:

    $41 ,500.oo     "00" read as "oo", and a space inserted after the thousands
    s 43,500.00     "$" read as "s"
    $1 ,650.oo      both at once
    coo o / 3Oo     a photographed HANDWRITTEN sheet -- unrecoverable

A blanket "check these by eye" warning is not good enough: it puts the burden on
whoever uses the data later, and it gives no way to tell the Rowlett tabulation
(clean laser print, every row checkable) apart from the Farmers Branch pricing
sheet (biro on paper, every digit a guess).

The check
---------
These tabulations print quantity, unit price AND extended total on the same row,
so each row carries its own proof:

    qty  x  unit_price  ==  total

That identity is independent of the OCR. So this script normalises the known
confusions, then tests the identity on every row it can parse. Normalisation is
safe precisely *because* it is tested afterwards: if turning "41 ,500.oo" into
41500.00 makes 1 x 41500.00 = 41500.00 come out true, the reading was right. If
a file's rows do not balance, its digits were not recovered, whatever the text
looks like.

What comes out is a per-file pass rate, which is a usable trust score:

    high rate  -> the digits survived; rows may be parsed and modelled
    low rate   -> OCR failed on this file; it is summary evidence at best, and
                  the unit prices must be read off the PDF by a human

Report only. Nothing is deleted and no price is rewritten -- the sidecars stay
as the OCR produced them, and `_ocr_trust.csv` records the verdict.

Usage:
    python src/ocr_verify.py
    python src/ocr_verify.py --show <relpath>   # print the parsed rows
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

ROOT = Path("data/external")
OCR_DIR = ROOT / "_ocr"
OUT = ROOT / "_ocr_trust.csv"

# Unit tokens as these tabulations write them, including the dotted forms
# ("L.S.", "S.Y.") that DFW consulting engineers use.
UNIT = (r"L\.?S\.?|S\.?Y\.?|C\.?Y\.?|L\.?F\.?|S\.?F\.?|EA\.?|Ea\.?|AC\.?|"
        r"TON|TN|GAL|HR|MO|DAY|LB|CF|VF|MGAL|STA")

# A number as OCR leaves it: digits, with "o"/"O" standing in for a zero, and a
# stray space permitted ONLY immediately beside a comma or decimal point, which
# is where OCR actually puts one ("41 ,500.oo", "1 ,650.oo").
#
# It must not be the looser `[\d][\d,. oO]*`. That allows a run of spaces inside
# a number, so it reaches straight across the gap between two columns and reads
# "32,150.00   32,150.00" as a single value -- which then has two decimal points,
# is rejected as unparseable, and disappears. The effect is a file whose
# arithmetic balances perfectly scoring 4%: the digits were fine, the pattern
# was eating the column boundary.
_D = r"[\doO]"
_NUM = _D + r"+(?:[ \t]?[,.][ \t]?" + _D + r"+)*"


def fix_number(raw: str):
    """Undo the systematic OCR confusions, or return None if unrecoverable.

    Only substitutions that cannot change a correctly-read number are applied:
    "o"/"O" are not digits, and a space is not valid inside a number. Anything
    still ambiguous after that is rejected rather than guessed at.
    """
    s = raw.strip()
    s = s.replace("o", "0").replace("O", "0")
    s = re.sub(r"\s+", "", s)            # "41 ,500" -> "41,500"
    s = s.replace(",", "")
    if s.count(".") > 1:                 # e.g. "1.650.00" -- cannot disambiguate
        return None
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# A schedule row starts with an item number. Column ORDER then varies by the
# engineering firm that drew the sheet, and both orders occur in this corpus:
#     Rowlett (Birkhoff Hendricks): item  qty  unit  description  prices...
#     Coppell / North Richland Hills: item  description  qty  unit  prices...
# So the quantity-and-unit pair is located anywhere in the line rather than at
# a fixed position. Assuming one order is what made this script report zero
# checkable rows for 17 of 18 files on its first run -- which would have been
# published as "the OCR is untrustworthy" when the truth was "the regex did not
# fire". The failure mode is the same one that has bitten every other stage
# here: a filter that silently matches nothing looks exactly like real absence.
ITEM_NO = re.compile(r"^[ \t]*(\d{1,4})\b")
QTY_UNIT = re.compile(r"\b(" + _NUM + r")[ \t]*(" + UNIT + r")(?![A-Za-z])")
# Every money-looking run, with or without a recognisable "$" ("s" is a common
# OCR reading of "$").
MONEY = re.compile(r"(?:\$|s)?[ \t]*(" + _NUM + r")")

# A total must be at least this multiple of the unit price to be a candidate,
# guarding against pairing two unit prices from adjacent bidder columns.
TOL = 0.02


# A lump-sum row writes its quantity in words ("Mobilization Lump Sum LS"), so
# no number parses out of it even though the quantity is, by definition, 1.
LUMP = re.compile(r"\b(?:LUMP[ \t]*SUM|L\.?[ \t]?S\.?)(?![A-Za-z])", re.I)


def verify_row(line: str):
    """(kind, balanced) for one schedule line.

    kind is "" (not a checkable row), "mult" (a genuine qty x price = total
    test) or "ls" (a lump-sum row, where qty is 1 so the test degenerates to
    "unit price equals extended total").

    The two are reported separately because they are not equally strong
    evidence. A multiplicative check is hard to pass by accident; an identity
    check only shows that two numbers printed side by side were read the same
    way twice. Rolling them into one pass rate would let a file of lump-sum
    rows claim the same confidence as a fully priced schedule.

    A bid tab repeats (unit price, extended total) once per bidder across the
    row, so one correct reading anywhere on the line proves the digits in that
    row survived OCR. The test is therefore per ROW, not per pair: descriptions
    contain numbers too ("8-inch", "TxDOT Item 247"), and counting every
    unbalanced pair as a failure would penalise a clean file for the noise in
    its own item descriptions.
    """
    if not ITEM_NO.match(line):
        return "", False
    qm = QTY_UNIT.search(line)
    if qm:
        qty = fix_number(qm.group(1))
        kind, tail = "mult", line[qm.end():]
        if qty is None or qty <= 0:
            return "", False
    else:
        lm = LUMP.search(line)
        if not lm:
            return "", False
        qty, kind, tail = 1.0, "ls", line[lm.end():]
    nums = [fix_number(x) for x in MONEY.findall(tail)]
    nums = [n for n in nums if n is not None and n > 0]
    if len(nums) < 2:
        return "", False
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            if b + 1e-9 < a:
                continue
            if abs(qty * a - b) <= max(TOL * b, 1.0):
                return kind, True
    return kind, False


def verify_file(text: str) -> dict:
    """Per-file tally, keeping multiplicative and lump-sum checks apart."""
    t = {"mult": 0, "mult_ok": 0, "ls": 0, "ls_ok": 0, "failed": []}
    for line in text.splitlines():
        kind, bal = verify_row(line)
        if not kind:
            continue
        t[kind] += 1
        if bal:
            t[kind + "_ok"] += 1
        elif len(t["failed"]) < 3:
            t["failed"].append(line.strip()[:90])
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", default=None)
    args = ap.parse_args()

    files = sorted(OCR_DIR.rglob("*.txt"))
    if not files:
        print("no OCR sidecars found; run src/ocr_scans.py first")
        return 1

    if args.show:
        p = OCR_DIR / (args.show + ".txt")
        if not p.exists():
            p = OCR_DIR / args.show
        for line in p.read_text(encoding="utf-8").splitlines():
            kind, bal = verify_row(line)
            if kind:
                print("%-4s %-4s %s" % (kind, "OK" if bal else "FAIL",
                                        line.strip()[:140]))
        return 0

    rows = []
    for p in files:
        rel = p.relative_to(OCR_DIR).as_posix()[:-4]
        t = verify_file(p.read_text(encoding="utf-8"))
        checked = t["mult"] + t["ls"]
        passed = t["mult_ok"] + t["ls_ok"]
        rate = (passed / checked) if checked else 0.0
        # The verdict leans on multiplicative checks. A file carried only by
        # lump-sum identity checks is held to "partial" at best: those show the
        # two columns were read alike, not that the digits are right.
        if checked < 5:
            trust = "unverifiable"
            why = "too few checkable rows (%d)" % checked
        elif rate < 0.40:
            trust = "low"
            why = "only %d/%d rows balance" % (passed, checked)
        elif t["mult"] >= 5 and rate >= 0.80:
            trust = "high"
            why = "%d/%d rows balance (%d by qty x price)" % (
                passed, checked, t["mult_ok"])
        else:
            trust = "partial"
            why = "%d/%d rows balance, only %d multiplicative" % (
                passed, checked, t["mult_ok"])
        rows.append({"file": rel, "trust": trust, "checks": checked,
                     "balanced": passed, "checks_mult": t["mult"],
                     "balanced_mult": t["mult_ok"], "checks_lumpsum": t["ls"],
                     "balanced_lumpsum": t["ls_ok"], "rate": round(rate, 3),
                     "basis": why, "unbalanced_examples": " | ".join(t["failed"])})

    order = {"high": 0, "partial": 1, "unverifiable": 2, "low": 3}
    rows.sort(key=lambda r: (order[r["trust"]], -r["rate"]))

    with OUT.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "file", "trust", "checks", "balanced", "checks_mult",
            "balanced_mult", "checks_lumpsum", "balanced_lumpsum", "rate",
            "basis", "unbalanced_examples"])
        w.writeheader()
        w.writerows(rows)

    for t in ("high", "partial", "unverifiable", "low"):
        sel = [r for r in rows if r["trust"] == t]
        if not sel:
            continue
        print("\n=== %s (%d) ===" % (t.upper(), len(sel)))
        for r in sel:
            print("  %-5s %3d checks (%3d mult) %s"
                  % ("%.0f%%" % (100 * r["rate"]), r["checks"],
                     r["checks_mult"], r["file"]))
    hi = sum(1 for r in rows if r["trust"] in ("high", "partial"))
    print("\n%d/%d OCR'd files prove their own arithmetic and can be parsed."
          % (hi, len(rows)))
    print("The rest are summary evidence only -- read their unit prices off the "
          "PDF by eye. Most carry no quantity column at all, so there is no")
    print("identity to test; that is a property of the document, not a verdict "
          "on the OCR.")
    print("-> %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
