"""CSV and XLSX export of one estimate result (the /api/estimate response).

Everything the results page shows goes into the file, in the page's order:
project details, the two headline figures with their bands, the accuracy
context, alerts and estimator notes, the not-covered lines and the complete
total, the low-confidence lump sums, the line table, the competition step
the similar past projects with their matched lines, and -- given the
estimate request -- each line's top-3 reference projects. XLSX needs openpyxl
(installed in the project venv); the CSV needs nothing.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from typing import Any, Dict, List

P80_TEXT = ("the low bid came in at or below this in about 80% of past "
            "lettings")


class ExportError(ValueError):
    pass


def _money(v):
    return round(float(v), 2) if isinstance(v, (int, float)) else ""


def _pct(v, nd=1):
    return ("%.*f%%" % (nd, 100 * v)) if isinstance(v, (int, float)) else ""


def _check(result):
    if not isinstance(result, dict) or not result.get("lines"):
        raise ExportError(
            "Nothing to export: the payload carried no priced line items. "
            "This endpoint takes the ESTIMATE RESULT, as {\"result\": <the "
            "/api/estimate response>} -- not an estimate request.")


def sections(result: Dict[str, Any]) -> List[tuple]:
    """[(title, header row or None, rows)] -- the shared content of both
    formats."""
    _check(result)
    inp = result.get("inputs") or {}
    hl = result.get("headline") or {}
    ctx = result.get("context") or {}
    eng = result.get("engine") or {}
    comp = result.get("competition") or {}
    sug = comp.get("suggestion") or {}
    out = []

    out.append(("Project", None, [
        ["Market", inp.get("market_label") or inp.get("market")],
        ["Owner", inp.get("owner_label") or "(not given)",
         inp.get("owner") or ""],
        ["Work type", inp.get("work_type_label") or "(not given)",
         inp.get("work_type") or ""],
        ["Bid date", inp.get("bid_date") or "(not given)",
         "prices escalated to %s" % inp.get("to_year")
         if inp.get("to_year") else "no escalation"],
        ["Expected bidders", inp.get("n_bidders"),
         "suggested by rule N1" if inp.get("bidders_source") == "rule N1"
         else "entered by the user"],
        ["Estimator", "src/estimate_low_bid.py md5 %s" % eng.get("md5"),
         eng.get("rule") or ""],
        ["Data", "master_panel.csv built %s" % eng.get("data"),
         "latest letting %s" % eng.get("latest_letting")],
        ["Generated", _dt.datetime.now().strftime("%Y-%m-%d %H:%M")],
    ]))

    cov = " (covered lines only)" if hl.get("covered_only") else ""
    b50, b80 = hl.get("p50_band") or ["", ""], hl.get("p80_band") or ["", ""]
    out.append(("Estimate", ["Figure", "Amount", "Note"], [
        ["Expected low bid (P50)%s" % cov, _money(hl.get("p50")),
         "the expected low bid"],
        ["Recommended OPCC (P80)%s" % cov, _money(hl.get("p80")),
         P80_TEXT + (" (P50 x %.3f)" % hl["p80_multiplier"]
                     if hl.get("p80_multiplier") else "")],
        ["Half of past lettings landed between", _money(b50[0]),
         _money(b50[1])],
        ["80% of past lettings landed between", _money(b80[0]),
         _money(b80[1])],
        ["Calibrated on", hl.get("calibration_n"),
         "%s lettings, each priced only from lettings before it"
         % (hl.get("calibration_market") or "")],
        ["Owner-set money inside P50 (face value)",
         _money(hl.get("fixed_total")), ""],
    ]))

    if ctx:
        out.append(("Forward accuracy of this market", None, [[
            ctx.get("label"),
            "P50 median error %s" % _pct(ctx.get("p50_median_error"), 0),
            "P80 under the low bid on %s of lettings"
            % _pct(ctx.get("p80_under"), 0),
            "median overshoot %s" % _pct(ctx.get("median_overshoot"), 0),
            "%s lettings, as of %s" % (ctx.get("lettings"), ctx.get("as_of")),
        ]]))

    rows = [[a.get("level"), a.get("text")] for a in result.get("alerts")
            or []]
    rows += [["estimator note", w] for w in result.get("warnings") or []]
    out.append(("Alerts and estimator notes", ["Level", "Text"],
                rows or [["(none)"]]))

    nc = result.get("not_covered") or {}
    rows = [[l.get("item_no"), l.get("description"), l.get("class_label"),
             l.get("quantity"), l.get("unit"),
             _money(l.get("quote")) if l.get("quote") else "get a quote"]
            for l in nc.get("lines") or []]
    if nc.get("lines"):
        rows.append([])
        rows.append(["", "Totals above EXCLUDE these lines."])
        if nc.get("complete_total") is not None:
            rows.append(["", "Complete total = P50 + quotes", "", "", "",
                         _money(nc.get("complete_total"))])
            if nc.get("complete_total_p80") is not None:
                rows.append(["", "Complete total = P80 + quotes", "", "", "",
                             _money(nc.get("complete_total_p80"))])
        else:
            rows.append(["", "No complete total until every line has a "
                             "quote (%d of %d quoted)"
                         % (nc.get("quoted_n") or 0, nc.get("n") or 0)])
    if nc.get("as_a_whole"):
        rows.append(["", "NOT COVERED AS A WHOLE: " +
                     "; ".join(nc.get("why") or [])])
    out.append(("Not covered -- get a vendor quote",
                ["Item", "Description", "Class", "Quantity", "Unit",
                 "Vendor quote"], rows or [["(none)"]]))

    lc = result.get("low_confidence") or {}
    rows = []
    for g in lc.get("lines") or []:
        h = g.get("history") or {}
        rng = g.get("implied_range") or ["", ""]
        rows.append([g.get("label"), ", ".join(str(x) for x in g.get("items")
                                               or [] if x),
                     _money(g.get("ours_p50")), _pct(g.get("share_of_p50")),
                     _pct(h.get("p25")), _pct(h.get("median")),
                     _pct(h.get("p75")), h.get("lettings", ""),
                     h.get("size_label", ""), _money(rng[0]), _money(rng[1]),
                     g.get("position") or ""])
    if rows:
        rows.append([lc.get("guide")])
    out.append(("Low confidence (mobilization / general-condition lump sums)",
                ["Class", "Items", "Ours at P50 level", "Share of P50",
                 "Earlier low bidders p25", "median", "p75", "Lettings",
                 "Job size", "Implied low", "Implied high", "Ours is"],
                rows or [["(none)"]]))

    rows = []
    for l in result.get("lines") or []:
        acc = l.get("accuracy") or {}
        rows.append([
            l.get("item_no"), l.get("description"),
            l.get("family_label") or l.get("family"),
            l.get("size_bucket") or "", l.get("quantity"), l.get("unit"),
            _money(l.get("unit_price_p50")) if l.get("unit_price_p50") else "",
            _money(l.get("amount_p50")) if l.get("amount_p50") else "",
            _money(l.get("market_unit_price"))
            if l.get("market_unit_price") else "",
            l.get("status"), l.get("basis"), acc.get("text") or "",
            "; ".join(w.get("text", "") for w in l.get("warnings") or []),
        ])
    tot = sum(l.get("amount_p50") or 0.0 for l in result.get("lines") or [])
    rows.append([])
    rows.append(["", "Sum of lines at P50 level (= P50)", "", "", "", "", "",
                 _money(tot)])
    out.append(("Lines", ["Item", "Description", "Classification", "Size",
                          "Quantity", "Unit", "Unit price (P50 level)",
                          "Amount (P50 level)", "Market unit price (all bids)",
                          "Status", "Basis", "Typical accuracy", "Warnings"],
                rows))

    out.append(("Competition", None, [
        ["Bidders used", comp.get("n_bidders"), comp.get("source")],
        ["Discount factor f(n)", comp.get("f"),
         "%s table, %s-bidder bin, %s lettings%s" % (
             comp.get("table"), comp.get("bin"), comp.get("lettings"),
             "; size-aware D1 discount (TxDOT T1)" if comp.get("d1") else "")],
        ["N1 suggestion", sug.get("n", ""),
         ("median of %s earlier %s" % (sug.get("history_lettings"),
                                       sug.get("cell_label")))
         if sug else ""],
    ]))
    sim = result.get("similar") or {}
    rows = []
    for c in sim.get("cards") or []:
        b = c.get("bids") or {}
        sh = c.get("shares") or {}
        at = c.get("at_their_prices") or {}
        src = c.get("source") or {}
        rows.append([
            c.get("rank"), c.get("project"), c.get("owner"), c.get("date"),
            c.get("work_type"), c.get("bidders"), _money(b.get("low")),
            _money(b.get("second")), _money(b.get("median")),
            _money(b.get("high")),
            ("%.2f" % c["size_vs_this_job"]) if c.get("size_vs_this_job")
            else "",
            "%s of %s item types, %s of the dollars"
            % (sh.get("types"), sh.get("of_types"),
               _pct(sh.get("dollar_share"), 0)),
            round(c["similarity"], 4) if c.get("similarity") is not None
            else "",
            _money(at.get("job")) if at else "",
            src.get("document") or "", src.get("url") or ""])
    if sim.get("why_fewer"):
        rows.append([sim["why_fewer"]])
    if sim.get("cards"):
        rows.append([])
        rows.append(["", "Comparables price (5-job blend)",
                     _money(sim.get("cmp_price"))])
        rows.append(["", "Line-priced bid part", _money(sim.get("line_price"))])
        rows.append(["", "Geometric mean, before centring",
                     _money(sim.get("blend"))])
    out.append(("Similar past projects",
                ["Rank", "Project", "Owner", "Bid date", "Work type",
                 "Bidders", "Low bid", "2nd low", "Median bid", "High bid",
                 "Size vs this job (their low / our P50)", "Shares",
                 "Similarity", "This job at their low-bid prices",
                 "Source document", "Source URL"], rows or [["(none)"]]))
    rows = []
    for c in sim.get("cards") or []:
        for m in c.get("matched_lines") or []:
            rows.append([c.get("rank"), m.get("item_no"),
                         m.get("description"), m.get("quantity"),
                         m.get("unit"), _money(m.get("our_unit_p50")),
                         m.get("their_item_no"), m.get("their_description"),
                         m.get("their_quantity"),
                         _money(m.get("their_low_unit")),
                         _money(m.get("their_median_unit")),
                         m.get("their_bids_on_line"),
                         round(m["match"], 2) if m.get("match") else ""])
    if rows:
        out.append(("Similar past projects: matched lines",
                    ["Project rank", "Our item", "Our description", "Qty",
                     "Unit", "Our unit price (P50 level)", "Their item",
                     "Their description", "Their qty",
                     "Their low bidder $/unit", "Their median bidder $/unit",
                     "Bids on the line", "Word match"], rows))

    rows = []
    for x in result.get("top_references") or []:
        rows.append([x.get("item_no"), x.get("description"), x.get("unit"),
                     x.get("status"), "", "", "", "", "", "", "", "",
                     x.get("text") or ""])
        if x.get("warning"):
            rows.append(["", "", "", "", "", "", "", "", "", "", "", "",
                         x["warning"]])
        for k, rf in enumerate(x.get("references") or [], 1):
            rows.append(["", "#%d" % k, rf.get("project"), rf.get("owner"),
                         rf.get("date"), rf.get("bidders"),
                         _money(rf.get("low_total")), rf.get("item"),
                         rf.get("quantity"), _money(rf.get("median_unit")),
                         _money(rf.get("low_unit")),
                         _money(rf.get("escalated_unit")),
                         (rf.get("source") or {}).get("document") or ""])
    if rows:
        out.append(("Line references (top 3 per line)",
                    ["Item", "Description / rank", "Unit / project",
                     "Status / owner", "Letting date", "Bidders",
                     "Project low bid", "Item as written", "Their qty",
                     "Median bid $/unit", "Low bid $/unit",
                     "Median bid in bid-year dollars", "How priced / source"],
                    rows))
    return out


def to_csv(result: Dict[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# Bid estimate -- expected low bid (P50) and recommended "
                "OPCC (P80), production estimator"])
    for title, head, rows in sections(result):
        w.writerow([])
        w.writerow(["## " + title])
        if head:
            w.writerow(head)
        for r in rows:
            w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def to_xlsx(result: Dict[str, Any]) -> bytes:
    import openpyxl
    from openpyxl.styles import Font

    secs = sections(result)
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = "Summary"
    sheet_of = {"Lines": "Lines", "Not covered -- get a vendor quote":
                "Not covered",
                "Low confidence (mobilization / general-condition lump sums)":
                "Low confidence", "Similar past projects": "Similar projects",
                "Similar past projects: matched lines": "Matched lines",
                "Line references (top 3 per line)": "References"}
    bold = Font(bold=True)
    for title, head, rows in secs:
        if title in sheet_of:
            ws = wb.create_sheet(sheet_of[title])
            ws.append([title])
            ws.cell(row=1, column=1).font = bold
        else:
            ws = summary
            if ws.max_row > 1 or ws.cell(row=1, column=1).value:
                ws.append([])
            ws.append([title])
            ws.cell(row=ws.max_row, column=1).font = bold
        if head:
            ws.append(head)
            for c in ws[ws.max_row]:
                c.font = bold
        for r in rows:
            ws.append(list(r))
    for ws in wb.worksheets:
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col
                         if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = \
                min(60, max(9, width + 1))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
