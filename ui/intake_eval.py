"""How well does takeoff intake read messy input? Scored on PUBLIC data only.

    .venv/bin/python ui/intake_eval.py                     # local readers, baseline
    .venv/bin/python ui/intake_eval.py --tag after         # after a change
    .venv/bin/python ui/intake_eval.py --reader claude     # the optional Claude reader
                                                           # (needs credentials; costs money)

The set: 25 public city bid-tab PDFs (source_set "external" only; one letting
per file, 8-150 lines, at most one per owner, 20 with a text layer and 5
scanned), chosen by a fixed seed. Ground truth per file: the panel's own bid-form
lines (item no, description, unit, quantity) of that letting. Nothing
internal, nothing from the locked test set, nothing from ui/.inbox or
ui/.cache is read.

Plus messy TEXT variants of 10 of them, generated from the ground truth:
  pdf_copy  text copied out of a PDF: columns squeezed to single spaces,
            descriptions wrapped onto a second line, a header, page footers
            and a SUBTOTAL row
  units     unit words written out ("Each", "L.F.", "Lump Sum", "Sq. Yd."),
            thousands separators, footnote markers in descriptions
  merged    quantity and unit in one cell ("1,745 SY", "1745SY")

Metrics per document (and pooled):
  recovered    share of ground-truth lines found (description word match
               >= 0.5, or the same item number with >= 0.3)
  qty_ok       of the found lines, quantity within 0.5%
  unit_ok      of the found lines, the same normalised unit
  family_ok    of the found lines, the parser gives the same item family
               (the description is usable)
  false_lines  share of parsed lines that match nothing (totals, headers,
               fragments)

And what the grid's intake help does with the lines it found (local reader):
  flagged_wrong   of the found lines with a wrong quantity, unit or family,
                  the share marked low confidence ("check")
  flagged_right   of the found lines read right, the share marked anyway
                  (the cost of the flag: lines checked for nothing)
  suggest_hit     of the flagged lines whose family is wrong, the share
                  whose right family is among the top-3 suggestions

Caveat: src/parse_bidtabs.read_pdf_best built the ground truth from these
very files, so the "bidtab" reader is scored for information only; the
generic readers are the honest measure. Writes
handoff/scratchpad/intake_eval/<tag>/ (results.json, summary.md).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "handoff", "scratchpad", "intake_eval")

import upstream  # noqa: E402
from ingest import takeoff  # noqa: E402

SEED = 20261120


def _tok(s):
    return set(w for w in upstream.norm_desc(s).split() if len(w) > 1)


def _jac(a, b):
    a, b = _tok(a), _tok(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def choose(n=25, n_scanned=5):
    """(file, project key, ground-truth lines) for the evaluation set."""
    import pandas as pd
    m = pd.read_csv(os.path.join(ROOT, "data", "clean", "master_panel.csv"),
                    encoding="utf-8-sig", dtype=str, keep_default_na=False,
                    usecols=["source_set", "source", "project_key", "owner",
                             "text_source", "item_no", "description", "unit",
                             "quantity", "desc_norm"])
    m = m[(m.source_set == "external") & (m.source == "city_bidtab")]
    m = m.assign(file=m.project_key.str.split("::").str[0])
    per_file = m.groupby("file").project_key.nunique()
    one = set(per_file[per_file == 1].index)
    m = m[m.file.isin(one) & m.file.str.lower().str.endswith(".pdf")]
    gt = {}
    for pk, g in m.groupby("project_key"):
        g = g.drop_duplicates(["item_no", "desc_norm", "unit", "quantity"])
        lines = [{"item_no": r.item_no, "description": r.description,
                  "unit": r.unit,
                  "quantity": float(r.quantity) if r.quantity else None}
                 for r in g.itertuples()]
        if 8 <= len(lines) <= 150:
            gt[pk] = (g.file.iloc[0], g.owner.iloc[0],
                      "ocr" if (g.text_source == "ocr").any() else "text",
                      lines)
    rng = random.Random(SEED)
    keys = sorted(gt)
    rng.shuffle(keys)
    pick, owners = [], set()
    for want, kind in ((n - n_scanned, "text"), (n_scanned, "ocr")):
        got = 0
        for pk in keys:
            f, ow, ts, lines = gt[pk]
            if ts != kind or ow in owners or got >= want:
                continue
            if not os.path.exists(os.path.join(ROOT, "data", f)):
                continue
            pick.append({"key": pk, "file": f, "owner": ow, "kind": ts,
                         "truth": lines})
            owners.add(ow)
            got += 1
    return pick


def score(truth, parsed):
    """Greedy one-to-one matching of parsed lines to ground truth."""
    cand = []
    for i, t in enumerate(truth):
        for j, p in enumerate(parsed):
            jac = _jac(t["description"], p.get("description"))
            same_no = bool(t.get("item_no") and p.get("item_no")
                           and str(t["item_no"]).strip().lower()
                           == str(p["item_no"]).strip().lower())
            if jac >= 0.5 or (same_no and jac >= 0.3):
                cand.append((jac + (0.2 if same_no else 0), i, j))
    cand.sort(reverse=True)
    ti, pj, pairs = set(), set(), []
    for sc, i, j in cand:
        if i in ti or j in pj:
            continue
        ti.add(i)
        pj.add(j)
        pairs.append((i, j))
    score.pairs = pairs
    n_t, n_p, n_m = len(truth), len(parsed), len(pairs)
    q_ok = u_ok = f_ok = 0
    for i, j in pairs:
        t, p = truth[i], parsed[j]
        qt, qp = t.get("quantity"), p.get("quantity")
        if (qt is None and qp is None) or (
                qt is not None and qp is not None
                and abs(qt - qp) <= 0.005 * max(abs(qt), 1e-9)):
            q_ok += 1
        if upstream.takeoff_unit(t.get("unit") or "") == \
                upstream.takeoff_unit(p.get("unit") or ""):
            u_ok += 1
        if upstream.item_family(upstream.norm_desc(t["description"])) == \
                upstream.item_family(upstream.norm_desc(p.get("description"))):
            f_ok += 1
    return {"truth": n_t, "parsed": n_p, "matched": n_m,
            "recovered": n_m / n_t if n_t else 0.0,
            "qty_ok": q_ok / n_m if n_m else 0.0,
            "unit_ok": u_ok / n_m if n_m else 0.0,
            "family_ok": f_ok / n_m if n_m else 0.0,
            "false_lines": (n_p - n_m) / n_p if n_p else 0.0}


def assist(truth, parsed, pairs):
    """The intake help on the found lines: is a wrong line flagged, and is
    its right family one click away? (ui/intake.py, as /api/classify runs
    it; classification by the parser, history by the search index.)"""
    import intake
    out = {"wrong": 0, "wrong_flagged": 0, "right": 0, "right_flagged": 0,
           "fam_wrong_flagged": 0, "fam_hit": 0}
    for i, j in pairs:
        t, p = truth[i], parsed[j]
        c = upstream.classify_line(p.get("description") or "", p.get("unit") or "")
        fam_t = upstream.item_family(upstream.norm_desc(t["description"]))
        qt, qp = t.get("quantity"), p.get("quantity")
        q_ok = (qt is None and qp is None) or (
            qt is not None and qp is not None
            and abs(qt - qp) <= 0.005 * max(abs(qt), 1e-9))
        u_ok = upstream.takeoff_unit(t.get("unit") or "") == \
            upstream.takeoff_unit(p.get("unit") or "")
        f_ok = c["family"] == fam_t
        cands = intake.history_candidates(p.get("description") or "", c["unit"])
        a = intake.assess(p, c, intake.best_match_score(cands))
        if q_ok and u_ok and f_ok:
            out["right"] += 1
            out["right_flagged"] += a["low"]
            continue
        out["wrong"] += 1
        out["wrong_flagged"] += a["low"]
        if a["low"] and not f_ok:
            out["fam_wrong_flagged"] += 1
            fams = [sg["family"] for sg in intake.suggestions(cands, c["unit"])]
            out["fam_hit"] += fam_t in fams
    return out


# -- messy text variants of the ground truth ---------------------------------

UNIT_WORDS = {"EA": ["Each", "EA.", "ea"], "LF": ["L.F.", "Lin. Ft.", "lf"],
              "LS": ["Lump Sum", "L.S.", "LUMP"], "SY": ["Sq. Yd.", "S.Y.", "sy"],
              "CY": ["Cu. Yd.", "C.Y.", "cy"], "SF": ["Sq. Ft.", "S.F."],
              "TON": ["Tons", "tn"], "MO": ["Month", "Mo."]}


def _qty(q, sep=True):
    if q is None:
        return ""
    s = ("{:,.2f}" if sep else "{:.2f}").format(q)
    return s[:-3] if s.endswith(".00") else s


def variants(truth, rng):
    out = {}
    lines = ["Item  Description  Unit  Quantity"]
    for k, t in enumerate(truth):
        d = t["description"]
        if len(d) > 38 and " " in d[30:]:
            cut = 30 + d[30:].index(" ")
            lines.append("%s %s %s %s" % (t["item_no"], d[:cut],
                                          t["unit"], _qty(t["quantity"])))
            lines.append("    " + d[cut + 1:])
        else:
            lines.append("%s %s %s %s" % (t["item_no"], d, t["unit"],
                                          _qty(t["quantity"])))
        if k and k % 15 == 0:
            lines.append("Page %d of %d" % (k // 15, len(truth) // 15 + 1))
    lines.append("SUBTOTAL BASE BID $1,234,567.89")
    out["pdf_copy"] = "\n".join(lines)
    lines = ["Item No.\tDescription\tUnit\tQuantity"]
    for t in truth:
        u = rng.choice(UNIT_WORDS.get(t["unit"], [t["unit"]]))
        foot = " (1)" if rng.random() < 0.15 else ""
        lines.append("%s\t%s%s\t%s\t%s" % (t["item_no"], t["description"], foot,
                                           u, _qty(t["quantity"])))
    lines.append("Notes: (1) see specification section 02.")
    out["units"] = "\n".join(lines)
    lines = ["Item No.\tDescription\tQty / Unit"]
    for t in truth:
        glue = "" if rng.random() < 0.5 else " "
        lines.append("%s\t%s\t%s%s%s" % (t["item_no"], t["description"],
                                         _qty(t["quantity"]), glue, t["unit"]))
    out["merged"] = "\n".join(lines)
    return out


# -- readers -------------------------------------------------------------------

def read_local(path):
    """The UI's own intake for an uploaded file."""
    p = takeoff.from_file(path, os.path.basename(path))
    return [l.to_dict() for l in p.lines], p.method


def read_bidtab(path):
    """src/parse_bidtabs.read_pdf_best (circular on this set)."""
    stats = {"cells_blank_price": 0, "sheets_no_header": []}
    out = upstream.read_pdf_best(path, stats)
    recs = out[0] if isinstance(out, tuple) else out
    seen, lines = set(), []
    for r in recs or []:
        k = (r.get("item_no"), r.get("description"), r.get("unit"),
             r.get("quantity"))
        if k in seen:
            continue
        seen.add(k)
        lines.append({"item_no": r.get("item_no"),
                      "description": r.get("description"),
                      "unit": r.get("unit"), "quantity": r.get("quantity")})
    return lines, "parse_bidtabs"


def pooled(rows):
    t = sum(r["truth"] for r in rows)
    m = sum(r["matched"] for r in rows)
    p = sum(r["parsed"] for r in rows)
    w = lambda k: (sum(r[k] * r["matched"] for r in rows) / m) if m else 0.0  # noqa: E731
    return {"docs": len(rows), "truth": t, "parsed": p, "matched": m,
            "recovered": m / t if t else 0.0, "qty_ok": w("qty_ok"),
            "unit_ok": w("unit_ok"), "family_ok": w("family_ok"),
            "false_lines": (p - m) / p if p else 0.0,
            "zero_docs": sum(1 for r in rows if r["matched"] == 0)}


def _p(v):
    return "-" if v is None else "%.0f%%" % (100 * v)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--reader", default="local",
                    choices=("local", "bidtab", "claude"))
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--no-assist", action="store_true",
                    help="skip the intake-help metrics (for a run on older "
                         "code that has no intake help)")
    a = ap.parse_args(argv)
    docs = choose(a.n)
    rng = random.Random(SEED)
    res = {"files": [], "text": collections.defaultdict(list),
           "tag": a.tag, "reader": a.reader}
    claude = None
    if a.reader == "claude":
        from ingest import claude_reader
        claude = claude_reader
        st = claude.status()
        if not st["available"]:
            print("Claude reader unavailable: %s" % st["reason"])
            return 2
    spend = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0}
    for d in docs:
        path = os.path.join(ROOT, "data", d["file"])
        t0 = time.time()
        try:
            if a.reader == "local":
                lines, method = read_local(path)
            elif a.reader == "bidtab":
                lines, method = read_bidtab(path)
            else:
                rr = claude.read_pdf(path, consent=True)
                lines, method = rr["lines"], "claude"
                for k in ("input_tokens", "output_tokens"):
                    spend[k] += rr["usage"].get(k, 0)
                spend["usd"] += rr["usage"].get("usd", 0.0)
            err = ""
        except Exception as e:  # noqa: BLE001
            lines, method, err = [], "error", "%s: %s" % (type(e).__name__, e)
        sc = score(d["truth"], lines)
        if a.reader == "local" and lines and not a.no_assist:
            sc["assist"] = assist(d["truth"], lines, score.pairs)
        sc.update({"file": os.path.basename(d["file"]), "owner": d["owner"],
                   "kind": d["kind"], "method": method, "error": err,
                   "seconds": round(time.time() - t0, 2)})
        res["files"].append(sc)
        print("%-5s %-62s %3d/%3d found, qty %3.0f%%, unit %3.0f%%, false %3.0f%%  %s %s"
              % (d["kind"], os.path.basename(d["file"])[:62], sc["matched"],
                 sc["truth"], 100 * sc["qty_ok"], 100 * sc["unit_ok"],
                 100 * sc["false_lines"], method, err[:60]))
    if a.reader == "local":
        for d in docs[:10]:
            for name, text in variants(d["truth"], rng).items():
                p = takeoff.from_text(text, name)
                sc = score(d["truth"], [l.to_dict() for l in p.lines])
                sc.update({"file": os.path.basename(d["file"]),
                           "variant": name})
                res["text"][name].append(sc)
    summary = {"pdf": {"all": pooled(res["files"]),
                       "text_layer": pooled([r for r in res["files"]
                                             if r["kind"] == "text"]),
                       "scanned": pooled([r for r in res["files"]
                                          if r["kind"] == "ocr"])},
               "text": {k: pooled(v) for k, v in res["text"].items()},
               "spend": spend if a.reader == "claude" else None}
    tot = collections.Counter()
    for r in res["files"]:
        tot.update(r.get("assist") or {})
    if tot:
        summary["assist"] = {
            "wrong_lines": tot["wrong"], "right_lines": tot["right"],
            "flagged_wrong": tot["wrong_flagged"] / tot["wrong"] if tot["wrong"] else None,
            "flagged_right": tot["right_flagged"] / tot["right"] if tot["right"] else None,
            "family_wrong_flagged": tot["fam_wrong_flagged"],
            "suggest_hit": (tot["fam_hit"] / tot["fam_wrong_flagged"]
                            if tot["fam_wrong_flagged"] else None)}
    res["summary"] = summary
    od = os.path.join(OUT, a.tag + ("" if a.reader == "local"
                                    else "_" + a.reader))
    os.makedirs(od, exist_ok=True)
    with open(os.path.join(od, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    rows = ["| set | docs | lines | found | qty ok | unit ok | family ok | "
            "false lines | docs with 0 |", "|---|---|---|---|---|---|---|---|---|"]
    for name, s in list(summary["pdf"].items()) + [
            ("text: " + k, v) for k, v in summary["text"].items()]:
        rows.append("| %s | %d | %d | %.0f%% | %.0f%% | %.0f%% | %.0f%% | %.0f%% | %d |"
                    % (name, s["docs"], s["truth"], 100 * s["recovered"],
                       100 * s["qty_ok"], 100 * s["unit_ok"],
                       100 * s["family_ok"], 100 * s["false_lines"],
                       s["zero_docs"]))
    if summary.get("assist"):
        s2 = summary["assist"]
        rows.append("")
        rows.append("Intake help on the found PDF lines: %d read wrong, %s of them "
                    "flagged; %d read right, %s of them flagged anyway; of %d "
                    "flagged lines with the wrong family, the right one is in "
                    "the top-3 suggestions for %s."
                    % (s2["wrong_lines"], _p(s2["flagged_wrong"]),
                       s2["right_lines"], _p(s2["flagged_right"]),
                       s2["family_wrong_flagged"], _p(s2["suggest_hit"])))
    if summary["spend"]:
        rows.append("")
        rows.append("Claude spend: %(input_tokens)d input tokens, "
                    "%(output_tokens)d output tokens, $%(usd).2f" % spend)
    open(os.path.join(od, "summary.md"), "w", encoding="utf-8").write(
        "\n".join(rows) + "\n")
    print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
