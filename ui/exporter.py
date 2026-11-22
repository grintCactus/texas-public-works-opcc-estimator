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
import re
from typing import Any, Dict, List

P80_TEXT = ("a cautious figure: the low bid came in at or below this in "
            "about 80% of past lettings, so an OPCC at P80 falls short about 1 "
            "time in 5")

# ---------------------------------------------------------------------------
# Spreadsheet safety: text is never run as a formula
# ---------------------------------------------------------------------------
# Descriptions come from the user's takeoff and from third-party bid tabs,
# notes from the user. A spreadsheet runs a cell that starts with = + - @ as
# a formula ("=HYPERLINK(...)", "-Remove fence" -> #NAME?), and openpyxl
# stores any string starting with "=" as a live formula. Every export
# (estimate, worksheet, full bid tab) writes its cells through these.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
_NUMBERISH = re.compile(r"[+\-−]?\$?[\d,]*\.?\d+%?")


def safe_text(v):
    """A CSV cell: text that starts with = + - @ (tab, CR) gets a leading
    apostrophe, so a spreadsheet shows it as text. Numbers, and numeric text
    such as "-96.9%" or "+20.3%", are unchanged."""
    if isinstance(v, str) and v[:1] in _FORMULA_LEAD \
            and not _NUMBERISH.fullmatch(v.strip()):
        return "'" + v
    return v


def safe_row(row):
    return [safe_text(v) for v in row]


def xlsx_text_only(wb) -> None:
    """Store every cell openpyxl took for a formula ("=..." text) as plain
    text: this workbook writes values, never formulas."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.data_type == "f":
                    c.data_type = "s"


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
         "the middle estimate of the low bid: as likely to be too high as "
         "too low"],
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
                 "Earlier low bidders p25", "median", "p75", "Past jobs",
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
            src.get("document") or "", src.get("url") or "",
            src.get("mirror_url") or ""])
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
                 "Source document", "source_url", "mirror_url"],
                rows or [["(none)"]]))
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
                     x.get("text") or "", "", ""])
        if x.get("warning"):
            rows.append(["", "", "", "", "", "", "", "", "", "", "", "",
                         x["warning"], "", ""])
        for k, rf in enumerate(x.get("references") or [], 1):
            rows.append(["", "#%d" % k, rf.get("project"), rf.get("owner"),
                         rf.get("date"), rf.get("bidders"),
                         _money(rf.get("low_total")), rf.get("item"),
                         rf.get("quantity"), _money(rf.get("median_unit")),
                         _money(rf.get("low_unit")),
                         _money(rf.get("escalated_unit")),
                         (rf.get("source") or {}).get("document") or "",
                         (rf.get("source") or {}).get("url") or "",
                         (rf.get("source") or {}).get("mirror_url") or ""])
    if rows:
        out.append(("Line references (top 3 per line)",
                    ["Item", "Description / rank", "Unit / project",
                     "Status / owner", "Letting date", "Bidders",
                     "Project low bid", "Item as written", "Their qty",
                     "Median bid $/unit", "Low bid $/unit",
                     "Median bid in bid-year dollars", "How priced / source",
                     "source_url", "mirror_url"],
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
            w.writerow(safe_row(head))
        for r in rows:
            w.writerow(safe_row(r))
    return ("﻿" + buf.getvalue()).encode("utf-8")


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# The blank takeoff template (user, 2026-11-21: download it, fill it in,
# paste it back or upload it, then estimate)
# ---------------------------------------------------------------------------
# One sheet only: the takeoff reader (ingest/takeoff.py) reads the sheet with
# the most items, so a second sheet of examples could win over a short
# takeoff. Its header row is mapped by name (Item No. / Description / Unit /
# Quantity / an amount column read for owner-set allowances only) and the
# notes above the header are skipped, in a file and in a paste alike.

#: The blank takeoff is laid out like the OPCC it becomes (user, 2026-11-21:
#: "the same format as the output, just not filled in"): the same title and
#: project block, the same columns. Fill in the first four; Unit Cost and
#: Total Cost stay blank except an allowance's printed amount (Total Cost).
TEMPLATE_TITLE = "OPINION OF PROBABLE CONSTRUCTION COSTS"
TEMPLATE_HEADER = ["Item No.", "Description", "Units", "Quantity",
                   "Unit Cost", "Total Cost"]
TEMPLATE_NOTES = [
    "How to fill this in",
    "One pay item per row: item no., the description as the bid form words "
    "it, units and quantity.",
    "Leave Unit Cost and Total Cost blank: the estimator prices every line.",
    "Only for an allowance the bid form prints a dollar amount for, put that "
    "amount in Total Cost.",
    "Leave the contingency out: you pick yours on the page.",
    "Then upload this file, or copy the header row and your rows into the "
    "paste box.",
]
#: The units offered in the Units column's list (others can still be typed:
#: the list only warns).
TEMPLATE_UNITS = ["LS", "EA", "LF", "SY", "SF", "CY", "CF", "TON", "GAL",
                  "AC", "MO", "DAY", "HR", "STA", "VF", "LB", "MI", "ALLOW"]
TEMPLATE_ROWS = 300
TEMPLATE_HEAD_ROW = 8


def takeoff_template_xlsx() -> bytes:
    """The blank takeoff workbook, in the OPCC's own layout: the title, the
    project block (date, prepared by, project no.), ESTIMATED QUANTITIES with
    the OPCC's columns and TEMPLATE_ROWS blank lines in a thin grid (a form
    to fill in keeps its lines), a unit list and number checks; how to fill
    it in sits beside it (column H), outside the printed form and above the
    header the reader looks for. The OPCC's serif headings and gray band,
    on US Letter."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    def F(size=10, bold=False, color=None, italic=False):
        return Font(name="Arial", size=size, bold=bold, color=color, italic=italic)

    def T(size, color=None, italic=False):     # the OPCC's serif headings
        return Font(name="Times New Roman", size=size, bold=True, color=color, italic=italic)
    thin = Side(style="thin", color="000000")
    med = Side(style="medium", color="000000")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Takeoff"
    ws.sheet_view.showGridLines = False
    for col, width in zip("ABCDEFGH", (7, 62, 8, 11, 12, 14, 3, 90)):
        ws.column_dimensions[col].width = width
    ws.merge_cells("A1:F1")
    ws["A1"] = TEMPLATE_TITLE
    ws["A1"].font = T(14)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24
    ws.merge_cells("A3:C5")
    ws["A3"] = "(project name)"
    ws["A3"].font = T(16, color="A6A6A6", italic=True)
    ws["A3"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for r, lab in ((3, "DATE:"), (4, "PREPARED BY:"), (5, "PROJECT NO.:")):
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
        ws.cell(row=r, column=4, value=lab).font = T(10)
    ws.merge_cells("A7:C7")
    ws["A7"] = "ESTIMATED QUANTITIES"
    ws["A7"].font = F(10, True)
    from openpyxl.styles import PatternFill
    for j in range(1, 7):                      # the OPCC's gray band
        ws.cell(row=7, column=j).fill = PatternFill("solid", fgColor="BFBFBF")
    head = TEMPLATE_HEAD_ROW
    first, last = head + 1, head + TEMPLATE_ROWS
    for j, name in enumerate(TEMPLATE_HEADER, start=1):
        c = ws.cell(row=head, column=j, value=name)
        c.font = F(10, True)
        c.alignment = Alignment(horizontal="center", vertical="bottom")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r in range(head, last + 1):
        for j in range(1, 7):
            c = ws.cell(row=r, column=j)
            c.border = box
            if r > head:
                c.font = F(10)
                if j == 2:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
                elif j in (1, 3):
                    c.alignment = Alignment(horizontal="center", vertical="top")
                elif j >= 5:
                    c.number_format = '"$"#,##0.00'
    # the title's rule and a thin frame round the printed form (A-F)
    for j in range(1, 7):
        c = ws.cell(row=1, column=j)
        c.border = Border(left=thin if j == 1 else None, right=thin if j == 6 else None,
                          top=thin, bottom=med)
    for r in range(2, head):
        ws.cell(row=r, column=1).border = Border(left=thin)
        ws.cell(row=r, column=6).border = Border(right=thin)
    # how to fill it in: beside the form, one line each (no wrap: the title
    # and project rows keep their height)
    for k, text in enumerate(TEMPLATE_NOTES, start=1):
        c = ws.cell(row=k, column=8, value=text)
        c.font = F(10, k == 1, color=None if k == 1 else "595959")
    unit = DataValidation(
        type="list", formula1='"%s"' % ",".join(TEMPLATE_UNITS),
        allow_blank=True, showErrorMessage=True, errorStyle="warning",
        errorTitle="Units", error="Not one of the usual units. Keep it if "
        "the bid form uses it.", showInputMessage=True, promptTitle="Units",
        prompt="As on the bid form: LS, EA, LF, SY, CY, TON ...")
    qty = DataValidation(
        type="decimal", operator="greaterThanOrEqual", formula1="0",
        allow_blank=True, showErrorMessage=True, errorStyle="warning",
        errorTitle="Quantity", error="A quantity is a number, such as 1 or "
        "4,200.")
    amt = DataValidation(
        type="decimal", operator="greaterThanOrEqual", formula1="0",
        allow_blank=True, showErrorMessage=True, errorStyle="warning",
        errorTitle="Amount", error="Dollars, only for an allowance the bid "
        "form prints an amount for.")
    for dv, rng in ((unit, "C%d:C%d" % (first, last)), (qty, "D%d:D%d" % (first, last)),
                    (amt, "E%d:F%d" % (first, last))):
        ws.add_data_validation(dv)
        dv.add(rng)
    ws.freeze_panes = "A%d" % first
    ws.print_title_rows = "%d:%d" % (head, head)
    ws.print_area = "A1:F%d" % last
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
    xlsx_text_only(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
