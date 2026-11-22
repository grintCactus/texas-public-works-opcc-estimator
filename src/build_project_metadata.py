"""
Project-level metadata: title, owner, work type, and the awarded amount.

Two things the line-item tables cannot carry:

  * WORK TYPE. The target practice is creek erosion / channel / outfall repair,
    and that is exactly where our sample is thinnest, while paving data is
    plentiful. Without a work-type tag the summary cannot say how much usable
    evidence exists for the work they actually bid.
  * THE AWARDED AMOUNT. The Legistar manifests carry each item's council title,
    which usually names the low bidder and the contract amount. That is an
    INDEPENDENT figure to check parsed bid totals against -- a different source
    from the tabulation we parsed.

Outputs
  data/clean/project_metadata.csv
  data/clean/award_reconciliation.csv   parsed low bid vs council-approved award
"""
import csv
import glob
import os
import re

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CLEAN = os.path.join(DATA, "clean")

# Ordered: the first match wins, so the specialty work the project cares about is
# tested before the generic paving/utility terms that often co-occur with it.
WORK_RULES = [
    ("erosion_creek",   r"erosion|creek|channel|outfall|gabion|streambank|stream bank|"
                        r"scour|bank stabil|dredg|\bpond\b|detention|\bculvert\b.{0,20}repair"),
    ("bridge",          r"\bbridge\b|\bbr\b.{0,10}replac|superstructure"),
    ("drainage_storm",  r"drainage|storm\s*(water|sewer|drain)|\bswm\b|flood"),
    ("water_sewer",     r"\bwater\b|wastewater|sanitary|sewer|\bww\b|lift station|pump station|"
                        r"\bmain\b.{0,20}replac"),
    ("paving_street",   r"paving|pavement|street|roadway|reclamation|overlay|mill|alley|"
                        r"reconstruct|resurfac|concrete repair|panel"),
    ("sidewalk_ada",    r"sidewalk|\bada\b|barrier free|ramp|trail"),
    ("traffic_signal",  r"signal|intersection|signage|striping|marking|illumination|street light"),
    ("landscape",       r"landscape|mowing|irrigation|median|tree|grounds maintenance"),
    ("facility",        r"\bbuilding\b|facility|roof|hvac|\bpark\b.{0,20}improv|playground"),
]
WORK_RULES = [(n, re.compile(p, re.I)) for n, p in WORK_RULES]

MONEY_RE = re.compile(r"\$\s?([\d]{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{5,}(?:\.\d{2})?)")
WINNER_RE = re.compile(
    r"(?:low bidder|lowest responsible bidder|lowest responsive bidder|"
    r"contract (?:with|to)|award(?:ed)? to|execute a contract with)\s+"
    r"([A-Z][A-Za-z0-9&.,'\- ]{3,60}?)(?:\s+(?:in|for|of|at|dba)\b|[,.]|$)")


# Not construction. Collin County's bid-tab folder is mostly goods and service
# contracts -- plumbing hourly rates, Ford F-350s, software subscriptions -- and
# letting those into a construction unit-price reference would corrupt it with
# items that merely share a unit (HR, EA, MO).
NOT_CONSTRUCTION_RE = re.compile(
    r"hourly rate|software|subscription|licen[cs]e|computer|laptop|server|"
    r"\bvehicle|\btruck\b|\bsedan\b|\bford\b|\bchevrolet\b|ambulance|"
    r"uniform|janitorial|\bfuel\b|\btires?\b|office supplies|furniture|"
    r"copier|printer|\btoner\b|insurance|banking|audit|actuarial|"
    r"temporary staffing|security guard|\bfood\b|catering|\bmeals?\b|"
    r"\bchemicals?\b|\bammunition\b|\bradios?\b|\bbody cam|"
    r"maintenance agreement|annual supply|purchase of|lease of|"
    r"\bplumbing contract|\bhvac contract|elevator (service|maintenance)",
    re.I)

CONSTRUCTION_HINT_RE = re.compile(
    r"paving|pavement|street|roadway|drainage|storm|sewer|water (line|main)|"
    r"erosion|creek|channel|bridge|sidewalk|reconstruct|overlay|reclamation|"
    r"culvert|outfall|gabion|riprap|signal|illumination|\bcip\b|improvements?\b",
    re.I)


def work_type(text):
    t = text or ""
    for name, rx in WORK_RULES:
        if rx.search(t):
            return name
    return "other"


def is_construction(text):
    """Explicit exclusion beats a positive-only test here: titles are terse and
    a construction project often matches no keyword at all, while a goods
    contract almost always names the goods."""
    t = text or ""
    if NOT_CONSTRUCTION_RE.search(t) and not CONSTRUCTION_HINT_RE.search(t):
        return "no"
    if CONSTRUCTION_HINT_RE.search(t):
        return "yes"
    return "unknown"


def main():
    rows = []

    # ---- Legistar manifests ---------------------------------------------
    for mf in sorted(glob.glob(os.path.join(DATA, "external", "legistar",
                                            "_manifest_*.csv"))):
        with open(mf, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                rel = (r.get("file") or "").replace("\\", "/")
                if not rel:
                    continue
                title = (r.get("matter_title") or "").strip()
                money = MONEY_RE.findall(title)
                amounts = []
                for m in money:
                    try:
                        amounts.append(float(m.replace(",", "")))
                    except ValueError:
                        pass
                w = WINNER_RE.search(title)
                rows.append({
                    "src_file": "external/legistar/" + rel,
                    "owner": r.get("client", ""),
                    "source_group": "legistar",
                    "project_title": title[:300],
                    "agenda_date": r.get("intro_date", ""),
                    "attachment": r.get("attachment_name", ""),
                    "award_amount": max(amounts) if amounts else None,
                    "awarded_to": (w.group(1).strip() if w else ""),
                    "work_type": work_type(title),
                    "is_construction": is_construction(title),
                    "url": r.get("url", ""),
                })

    # ---- everything else: infer from the path ---------------------------
    seen = set(r["src_file"] for r in rows)
    for dirpath, _d, fnames in os.walk(os.path.join(DATA, "external")):
        rel_dir = os.path.relpath(dirpath, DATA).replace("\\", "/")
        if rel_dir == "external":
            continue
        owner = rel_dir.split("/")[1] if "/" in rel_dir else ""
        for f in fnames:
            if not f.lower().endswith((".pdf", ".xlsx")):
                continue
            rel = "%s/%s" % (rel_dir, f)
            if rel in seen:
                continue
            pretty = re.sub(r"[_\-]+", " ", os.path.splitext(f)[0])
            pretty = re.sub(r"^\d{4}[- ]\d{2}[- ]\d{2}\s*", "", pretty)
            rows.append({
                "src_file": rel, "owner": owner, "source_group": owner,
                "project_title": pretty[:300], "agenda_date": "",
                "attachment": "", "award_amount": None, "awarded_to": "",
                "work_type": work_type(pretty),
                "is_construction": is_construction(pretty), "url": "",
            })
    for f in sorted(os.listdir(DATA)):
        if f.lower().endswith((".pdf", ".xlsx")):
            pretty = re.sub(r"[_\-]+", " ", os.path.splitext(f)[0])
            rows.append({
                "src_file": f, "owner": "internal", "source_group": "internal",
                "project_title": pretty[:300], "agenda_date": "", "attachment": "",
                "award_amount": None, "awarded_to": "",
                "work_type": work_type(pretty),
                "is_construction": is_construction(pretty), "url": "",
            })

    meta = pd.DataFrame(rows).drop_duplicates("src_file")
    meta.to_csv(os.path.join(CLEAN, "project_metadata.csv"), index=False,
                encoding="utf-8-sig")

    print("project metadata: %d rows" % len(meta))
    print("\n=== Work types ===")
    print(meta.work_type.value_counts().to_string())
    print("\n=== Creek erosion / drainage projects (the core work type) ===")
    key = meta[meta.work_type.isin(["erosion_creek", "drainage_storm"])]
    print("%d projects in all" % len(key))
    print(key.work_type.value_counts().to_string())
    print("\nExamples:")
    for r in key.head(14).itertuples(index=False):
        print("  [%-15s] %-10s %s" % (r.work_type, r.owner, str(r.project_title)[:76]))

    # ---- award reconciliation -------------------------------------------
    li = os.path.join(CLEAN, "line_items.csv")
    if os.path.exists(li):
        d = pd.read_csv(li, encoding="utf-8-sig", low_memory=False)
        d["is_engineer_estimate"] = d.is_engineer_estimate.astype(str).str.lower().isin(
            ["true", "1", "yes"])
        for c in ("quantity", "unit_price", "extended_reported", "extended_calc"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d["ext"] = d.extended_calc.fillna(d.extended_reported)
        base = d[(~d.is_engineer_estimate) & (d.section_role == "base")
                 & (d.ext_mismatch != "Y") & d.ext.notna()]
        tot = base.groupby(["src_file", "bidder"])["ext"].sum().reset_index()
        low = tot.groupby("src_file")["ext"].min().rename("parsed_low_bid").reset_index()
        chk = low.merge(meta[meta.award_amount.notna()][
            ["src_file", "owner", "project_title", "award_amount", "work_type"]],
            on="src_file", how="inner")
        if len(chk):
            chk["ratio"] = chk.parsed_low_bid / chk.award_amount
            chk["match"] = (chk.ratio.sub(1).abs() <= 0.02).map({True: "OK", False: "DIFF"})
            chk.to_csv(os.path.join(CLEAN, "award_reconciliation.csv"), index=False,
                       encoding="utf-8-sig")
            ok = (chk.match == "OK").sum()
            print("\n=== Parsed low bids against the award amounts in council minutes ===")
            print("%d projects can be compared; %d agree (<=2%%) = %.0f%%"
                  % (len(chk), ok, ok / len(chk) * 100))
            print("\nExamples that do not agree (the 8 ratios farthest from 1):")
            bad = chk[chk.match != "OK"].assign(
                dev=lambda x: (x.ratio - 1).abs()).nlargest(8, "dev")
            for r in bad.itertuples(index=False):
                print("  ratio %6.3f  parsed=%12.0f minutes=%12.0f  %s"
                      % (r.ratio, r.parsed_low_bid, r.award_amount,
                         str(r.project_title)[:50]))

    print("\n-> data/clean/project_metadata.csv")


if __name__ == "__main__":
    main()
