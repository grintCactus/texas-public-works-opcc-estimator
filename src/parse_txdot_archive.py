"""
Parse the TxDOT historical bid-tabulation archive (2015-2024).

303 monthly zips, one fixed-width cp1252 text file per project, 13,956 tabs.
This is the history that sits before the Socrata feed's rolling 24 months, so it
is what makes any escalation or market-cycle work possible at all.

Three things in this format will corrupt results silently if mishandled:

  1. BIDDERS WRAP IN THREES. Only bidders 1-3 sit on the item row; 4-6 and 7-8
     follow on "BIDDERS n - m" continuation rows. Reading just the item row
     drops every bidder past the third -- which would bias the competition
     discount exactly where it matters, since that curve is estimated from
     high-bidder-count lettings.
  2. ITEM CODES CHANGED GENERATION. The archive is 7 digits, no hyphen, and
     2015-2023 is entirely 6xxx while the current feed is 7xxx: 1046001 is
     "104-6001", today's equivalent is "104-7001". Joining the two eras on code
     matches almost nothing and raises no error. This script emits the code in
     both forms and builds a description-based crosswalk instead.
  3. cp1252, not UTF-8.

Outputs
  data/clean/txdot_archive_line_items.csv
  data/clean/txdot_archive_project_totals.csv
  data/clean/txdot_code_crosswalk.csv       6xxx <-> 7xxx, matched on desc+unit
"""
import csv
import glob
import io
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_bidtabs import (norm_unit, norm_desc, item_family, extract_size_in,
                           canon_bidder, MATERIAL_RE, num, is_time_bid_item)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIPS = os.path.join(ROOT, "data", "external", "txdot_archive", "zips")
OUT = os.path.join(ROOT, "data", "clean")

DFW14 = {"DALLAS", "TARRANT", "COLLIN", "DENTON", "KAUFMAN", "ELLIS", "NAVARRO",
         "HUNT", "PARKER", "JOHNSON", "WISE", "ROCKWALL", "HOOD", "SOMERVELL"}
# Navarro and Somervell sit in the TxDOT Dallas district but outside the
# DFW-Arlington MSA; kept, flagged, and left for the analysis to choose.
MSA12 = DFW14 - {"NAVARRO", "SOMERVELL"}

COUNTY_RE = re.compile(r"^COUNTY\s+(.+?)(?:\s{2,}CONTROL\s+([\d\-]+))?\s*$")
TYPE_RE = re.compile(r"^TYPE\s+(.+?)\s{2,}DATE\s+(\d{1,2}/\d{1,2}/\d{2,4})")
EST_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s+\*+\s*ESTIMATE")
BIDDER_RE = re.compile(r"^\s*BIDDER\s+(\d+)\s+\$\s*([\d,]+(?:\.\d+)?)\s+(.+?)\s*$")
# The archive item code is ITM + DES + SP, where ITM is 3 OR 4 digits, DES is 4,
# and SP is absent or 3 -- so the field is 7, 8, 10 or 11 digits, not always 7.
# Demanding exactly 7 rejected 180,625 of 567,846 item rows (31.8%) across
# 12,569 of 13,956 tabs, and the rejects were not a random subset: every 4-digit
# ITM (3077 Superpave, 6001 PCMS, 6049 curb, 6185 TMA, 7100 utilities) and every
# special-provision code (316-6410-002, 506-6xxx-003, 666-6xxx-007) was dropped.
ITEM_RE = re.compile(r"^\s{0,8}(\d{7,11})\s+(.+?)\s*$")
# Alternate-section rows carry a "1X".."5X" prefix in columns 0-1.  They used to
# match nothing, which was far worse than being skipped: their continuation rows
# then landed on whatever item was last open, so an alternate's lump-sum amount
# ($190,682.95) could be written in as the unit price of a $1,750 EA repair item.
ALT_ITEM_RE = re.compile(r"^\s{0,4}(\d{1,2})X\s+(\d{7,11})\s+(.+?)\s*$")
# The second ordinal is optional: bidders wrap in groups of 3, so the final
# group of a 7-bidder letting prints "BIDDERS 7   4,000.00" with no range, and
# requiring the range silently dropped that bidder's price.
#
# The plural is REQUIRED, and that is the whole safety margin here.  Surveyed
# across 240 tabs in 6 zips, the two row types never overlap:
#   continuation : plural "BIDDERS", never contains "$"   ("BIDDERS 4 - 6  15.84")
#   bidder total : singular "BIDDER", always contains "$"  ("BIDDER 1  $194,125.52  MISSOURI-TEXAS FARMS")
# Writing this as "BIDDERS?" with an optional range -- which looks like the
# obvious generalisation -- makes it swallow the bidder-total rows and write a
# contractor's PROJECT TOTAL in as a unit price for whatever item is open.
CONT_RE = re.compile(r"^\s*BIDDERS\s+(\d+)(?:\s*-\s*(\d+))?\s+(.*?)\s*$")
NUMTOK_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
# "Looks like an item-code row" -- used only to decide when to CLOSE an open
# item, never to parse one.  Must not match a numeric price row.
ITEM_SHAPE_RE = re.compile(r"^\s{0,8}(?:\d{1,2}X\s+)?\d{4,}(?:\s|$)")

# Specialty erosion work, kept STATEWIDE because it barely occurs in DFW.
EROSION_RE = re.compile(
    r"\bgabions?\b|\bmattress(es)?\b|rip\s?rap|soil nails?|rock nails?|rock anchors?|"
    r"retaining wall|\bmse\b|rock filter dam|soil retention blanket|"
    r"erosion control blanket|channel lin|concrete lin|\bflume\b|"
    r"bank stabil|\bscour\b", re.I)


def iso_date(s):
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s.strip())
    if not m:
        return ""
    mo, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yy < 100:
        yy += 2000 if yy < 70 else 1900
    if not (1 <= mo <= 12 and 1 <= dd <= 31):
        return ""
    return "%04d-%02d-%02d" % (yy, mo, dd)


def split_code(code):
    """Split an archive item code into (hyphenated item_no, generation digit).

    Layout is ITM(3 or 4) + DES(4) + SP(0 or 3), giving lengths 7, 8, 10, 11.
    Hyphenating blindly after 3 characters produces a wrong item number for
    every 4-digit ITM, and makes the generation read the ITM's last digit
    instead of the DES generation digit -- which would misdate the 6xxx/7xxx
    code change that the whole crosswalk depends on.
    """
    n = len(code)
    if n in (7, 10):
        itm, des, sp = code[:3], code[3:7], code[7:]
    elif n in (8, 11):
        itm, des, sp = code[:4], code[4:8], code[8:]
    else:
        # Unknown width: keep the old behaviour rather than invent a split, and
        # let the generation fall back to the 4th character.
        return code[:3] + "-" + code[3:], (code[3] if n > 3 else "")
    return itm + "-" + des + (("-" + sp) if sp else ""), des[0]


def split_item_row(rest, n_on_row):
    """rest = everything after the item code.

    Layout: <description...> <UNIT> <QUANTITY> <ENG EST> <price1..price_n>.
    Taken from the right: n_on_row prices, then the engineer's unit price, then
    the quantity; the token just left of those is the unit. Descriptions do hold
    digits ("CONC PAV 8 IN"), but they sit left of the unit, so scanning from the
    right and stopping at the first non-numeric token cannot reach them.
    """
    toks = rest.split()
    want = n_on_row + 2
    nums = []
    i = len(toks) - 1
    while i >= 0 and len(nums) < want and NUMTOK_RE.match(toks[i]):
        nums.append(toks[i])
        i -= 1
    nums.reverse()
    if len(nums) < 2 or i < 0:
        return None
    unit = toks[i]
    desc = " ".join(toks[:i])
    qty = num(nums[0])
    eng = num(nums[1]) if len(nums) > 1 else None
    prices = [num(x) for x in nums[2:]]
    return desc, unit, qty, eng, prices


def parse_tab(text, src_name):
    lines = text.split("\n")
    county, csj, ptype, date = "", "", "", ""
    ee_total, bidders, btotals = None, {}, {}

    for ln in lines[:40]:
        m = COUNTY_RE.match(ln)
        if m and not county:
            county = (m.group(1) or "").strip().upper()
            if m.group(2):
                csj = m.group(2)
        if "CONTROL" in ln and not csj:
            mm = re.search(r"CONTROL\s+([\d\-]+)", ln)
            if mm:
                csj = mm.group(1)
        m = TYPE_RE.match(ln)
        if m:
            ptype, date = m.group(1).strip(), iso_date(m.group(2))
        if not date:
            mm = re.search(r"\bDATE\s+(\d{1,2}/\d{1,2}/\d{2,4})", ln)
            if mm:
                date = iso_date(mm.group(1))

    for ln in lines:
        m = EST_RE.search(ln)
        if m and ee_total is None:
            ee_total = num(m.group(1))
        m = BIDDER_RE.match(ln)
        if m:
            k = int(m.group(1))
            btotals[k] = num(m.group(2))
            bidders[k] = m.group(3).strip()
    n_bidders = len(bidders)
    if not n_bidders:
        return [], None

    recs = []
    cur = None          # the item currently open, for continuation rows
    n_rejected = 0      # item-looking rows we could not parse
    for ln in lines:
        m = CONT_RE.match(ln)
        if m:
            # A continuation row NEVER opens an item.  If nothing is open it is
            # dropped, which is correct: writing it into the previously open
            # item is how one item's prices ended up on another (502-6001
            # BARRICADES/MO read $0.50 against a real bid of $3,500 -- 7,000x).
            if cur is not None:
                lo = int(m.group(1))
                vals = [num(t) for t in m.group(3).split() if NUMTOK_RE.match(t)]
                for off, v in enumerate(vals):
                    idx = lo + off
                    if v is not None and idx in bidders:
                        cur["prices"][idx] = v
            continue
        alt = ALT_ITEM_RE.match(ln)
        m = ITEM_RE.match(ln)
        if alt or m:
            code = alt.group(2) if alt else m.group(1)
            rest = alt.group(3) if alt else m.group(2)
            parsed = split_item_row(rest, min(3, n_bidders))
            if not parsed:
                # Leaving `cur` pointing at the last SUCCESSFUL item means this
                # row's continuation lines overwrite that item's prices.  Close
                # the item instead and lose this row only.
                cur = None
                n_rejected += 1
                continue
            desc, unit, qty, eng, prices = parsed
            cur = {"code": code, "desc": desc, "unit": unit, "qty": qty,
                   "eng": eng, "prices": {},
                   "is_alternate": bool(alt),
                   "alt_group": (int(alt.group(1)) if alt else 0)}
            for off, v in enumerate(prices):
                if v is not None:
                    cur["prices"][off + 1] = v
            recs.append(cur)
            continue
        # A row shaped like an item code (optionally ALT-prefixed) that neither
        # pattern could parse: close the open item so this row's continuations
        # cannot be hijacked onto it.  Deliberately NARROW -- it must not fire
        # on a bare numeric price-continuation row like "   1,234.00  5,678.00",
        # because closing the item there would lose the real continuations that
        # follow.  \d{4,} followed by whitespace/EOL cannot match a figure with
        # a thousands comma or a decimal point.
        if ITEM_SHAPE_RE.match(ln):
            cur = None
            n_rejected += 1

    scope = ("DFW_MSA12" if county in MSA12
             else ("DFW_district14" if county in DFW14 else "other"))
    # The B half of an A+B bid (parse_bidtabs.is_time_bid_item): 800-6001 'NO.
    # OF WORKING DAYS', $/D, quantity = the daily road user cost, one 'price'
    # per bidder = the days bid.  TxDOT ranks on A+B and pays A, and the
    # BIDDER totals this format prints INCLUDE B -- so the printed-total check
    # cannot catch it (42 DFW tabs, 2017-24; median 7.7% of the low bid, up to
    # 28%: tail audit 2026-11-20).  The item leaves the line items, and each
    # bidder's total is its printed total less its B.  The rank-1 bidder is
    # the one awarded (lowest A+B); low_bid is that bidder's A, the contract.
    btime = {}
    for r in recs:
        if is_time_bid_item(r["code"], r["unit"], r["desc"]):
            for k, price in r["prices"].items():
                if price is not None and r["qty"] is not None:
                    btime[k] = btime.get(k, 0.0) + r["qty"] * price
    if btime:
        recs = [r for r in recs if not is_time_bid_item(r["code"], r["unit"], r["desc"])]
    out = []
    for r in recs:
        dn = norm_desc(r["desc"])
        mat = MATERIAL_RE.search(dn)
        fam = item_family(dn)
        size = extract_size_in(dn)
        hyph, gen_digit = split_code(r["code"])
        for k, price in sorted(r["prices"].items()):
            if price is None or price <= 0:
                continue
            out.append({
                "src_file": src_name,
                "source_set": "external",
                "owner": "txdot_archive",
                "county": county,
                "scope": scope,
                "csj": csj,
                "project_type": ptype,
                "bid_date": date,
                "item_code_raw": r["code"],
                "item_no": hyph,
                "code_generation": ("6xxx" if gen_digit == "6"
                                    else ("7xxx" if gen_digit == "7" else "other")),
                "is_alternate": r.get("is_alternate", False),
                "alt_group": r.get("alt_group", 0),
                "description": r["desc"],
                "desc_norm": dn,
                "item_family": fam,
                "size_in": size,
                "material": (mat.group(1).upper() if mat else ""),
                "unit_raw": r["unit"],
                "unit": norm_unit(r["unit"]),
                "quantity": r["qty"],
                "engineer_unit_price": r["eng"],
                "bid_rank": k,
                "is_low_bidder": (k == 1),
                "bidder_raw": bidders.get(k, ""),
                "bidder": canon_bidder(bidders.get(k, "")),
                "n_bidders": n_bidders,
                "unit_price": price,
            })
    printed = dict(btotals)
    if btime:
        btotals = {k: (round(v - btime[k], 2) if (v is not None and k in btime) else v)
                   for k, v in printed.items()}
    low = min([v for v in btotals.values() if v], default=None)
    if btime and btotals.get(1):
        low = btotals[1]          # A of the bidder ranked (and awarded) on A+B
    totals = {"src_file": src_name, "county": county, "scope": scope, "csj": csj,
              "project_type": ptype, "bid_date": date, "n_bidders": n_bidders,
              "ee_total": ee_total,
              "low_bid": low,
              # A only (the B time bid taken out of an A+B tab's printed totals)
              "bidder_totals": ";".join("%d:%s" % (k, btotals[k])
                                        for k in sorted(btotals)),
              "ab_bid": bool(btime),
              "bidder_totals_printed": (";".join("%d:%s" % (k, printed[k])
                                                 for k in sorted(printed)) if btime else ""),
              "time_bid": ";".join("%d:%.2f" % (k, btime[k]) for k in sorted(btime))}
    return out, totals


def main():
    zips = sorted(glob.glob(os.path.join(ZIPS, "*.zip")))
    if not zips:
        print("no zips at", ZIPS)
        return
    all_items, all_tot, all_ero = [], [], []
    n_files = n_dfw = n_ero_tabs = 0
    for zp in zips:
        try:
            zf = zipfile.ZipFile(zp)
        except Exception:
            continue
        with zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.upper().endswith(".TXT"):
                    continue
                n_files += 1
                try:
                    raw = zf.read(info)
                except Exception:
                    continue
                text = raw.decode("cp1252", errors="replace")
                name = "%s::%s" % (os.path.basename(zp), os.path.basename(info.filename))
                items, tot = parse_tab(text, name)
                if not tot:
                    continue
                if tot["scope"] != "other":
                    n_dfw += 1
                    all_items.extend(items)
                    all_tot.append(tot)
                    continue
                # Outside DFW: keep the EROSION items only. Filtering the whole
                # archive by county threw away 85% of the files, and with them
                # the gabion history -- gabion barely appears in DFW highway
                # work, it is concentrated in the San Antonio, Austin and
                # Odessa districts. Keyed on the item description instead, this
                # recovers a 2016-2024 series for the specialty work that
                # matters most here.
                ero = [r for r in items if EROSION_RE.search(r["desc_norm"])]
                if ero:
                    n_ero_tabs += 1
                    all_ero.extend(ero)

    icols = ["src_file", "source_set", "owner", "county", "scope", "csj",
             "project_type", "bid_date", "item_code_raw", "item_no",
             "code_generation", "description", "desc_norm", "item_family",
             "size_in", "material", "unit_raw", "unit", "quantity",
             "engineer_unit_price", "bid_rank", "is_low_bidder", "bidder_raw",
             # Alternate-section rows are now parsed instead of silently
             # poisoning the previous item, so the flag has to reach the CSV --
             # DictWriter uses extrasaction="ignore", which means a field missing
             # from this list is dropped without a word.
             "is_alternate", "alt_group",
             "bidder", "n_bidders", "unit_price"]
    with open(os.path.join(OUT, "txdot_archive_line_items.csv"), "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=icols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_items)
    if all_ero:
        with open(os.path.join(OUT, "txdot_archive_erosion_statewide.csv"), "w",
                  newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=icols, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_ero)

    tcols = ["src_file", "county", "scope", "csj", "project_type", "bid_date",
             "n_bidders", "ee_total", "low_bid", "bidder_totals",
             # A+B tabs: bidder_totals is A; the printed A+B and the B are kept
             "ab_bid", "bidder_totals_printed", "time_bid"]
    with open(os.path.join(OUT, "txdot_archive_project_totals.csv"), "w", newline="",
              encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=tcols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_tot)

    import collections
    print("扫描 %d 个 tab,DFW 区域保留 %d 个" % (n_files, n_dfw))
    print("子目报价行 %d" % len(all_items))
    print("DFW 外另保留含冲刷子目的 tab %d 个,冲刷子目报价行 %d"
          % (n_ero_tabs, len(all_ero)))
    if all_ero:
        eg = collections.Counter(r["bid_date"][:4] for r in all_ero if r["bid_date"])
        gab = [r for r in all_ero if "gabion" in r["desc_norm"]]
        print("=== 全州冲刷子目按年(报价行)===")
        for y in sorted(eg):
            print("  %s  %6d" % (y, eg[y]))
        print("gabion 报价行 %d,涉及 %d 个 tab"
              % (len(gab), len(set(r["src_file"] for r in gab))))
        gy = collections.Counter(r["bid_date"][:4] for r in gab if r["bid_date"])
        print("gabion 按年(tab 数):")
        for y in sorted(gy):
            n = len(set(r["src_file"] for r in gab if r["bid_date"][:4] == y))
            print("  %s  tabs=%3d  行=%5d" % (y, n, gy[y]))
    yr = collections.Counter(t["bid_date"][:4] for t in all_tot if t["bid_date"])
    print("\n=== 按年(项目数)===")
    for y in sorted(yr):
        print("  %s  %4d" % (y, yr[y]))
    nb = collections.Counter(t["n_bidders"] for t in all_tot)
    print("\n=== 投标家数分布 ===")
    for k in sorted(nb):
        print("  %2d 家: %4d 个开标" % (k, nb[k]))
    gen = collections.Counter(r["code_generation"] for r in all_items)
    print("\n=== 编码代次 ===", dict(gen))
    nodate = sum(1 for t in all_tot if not t["bid_date"])
    print("无日期项目: %d" % nodate)
    print("\n-> data/clean/txdot_archive_line_items.csv")
    print("-> data/clean/txdot_archive_project_totals.csv")


if __name__ == "__main__":
    main()
