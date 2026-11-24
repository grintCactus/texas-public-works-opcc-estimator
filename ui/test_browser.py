"""In-browser tests of the page's interactions (Playwright, optional).

The regular suite has no JavaScript engine, so these run only where the
Playwright package and a browser are installed; otherwise they are skipped
(`ui/run.py --test` reports them as skipped). They are kept here because
some regressions -- a click that toggles, a click that must not -- only show
in a real browser.

    # a Python with `playwright` installed (and its browsers):
    PLAYWRIGHT_BROWSERS_PATH=<browsers> <python-with-playwright> -m unittest ui/test_browser.py
    # against a server that is already running:
    BIDEST_E2E_URL=http://127.0.0.1:8017 ... -m unittest ui/test_browser.py
    # several engines (default chromium):
    BIDEST_E2E_ENGINES=chromium,webkit,firefox ...

Without BIDEST_E2E_URL a local server is started with the project's own
interpreter (.venv/bin/python ui/run.py) on a free port and stopped after.
Nothing is downloaded, saved on the server or sent anywhere.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # the project venv: no browser engine
    sync_playwright = None

SAMPLE_READY = ("() => typeof S !== 'undefined' && S.status && S.status.ready "
                "&& S.options")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def read_xlsx(path):
    """{sheet name: {cell: value}} from an .xlsx, values as saved (a formula's
    cached result included): numbers as floats, text as text."""
    import zipfile
    import xml.etree.ElementTree as ET
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(path)
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(m + "si"):
            ss.append("".join(t.text or "" for t in si.iter(m + "t")))
    names = [s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).iter(m + "sheet")]
    out = {}
    for k, name in enumerate(names, 1):
        cells = {}
        for c in ET.fromstring(z.read("xl/worksheets/sheet%d.xml" % k)).iter(m + "c"):
            t, v = c.get("t"), c.find(m + "v")
            if t == "s" and v is not None:
                cells[c.get("r")] = ss[int(v.text)]
            elif t == "inlineStr":
                cells[c.get("r")] = "".join(x.text or "" for x in c.iter(m + "t"))
            elif v is not None and v.text is not None:
                try:
                    cells[c.get("r")] = float(v.text)
                except ValueError:
                    cells[c.get("r")] = v.text
        out[name] = cells
    return out

@unittest.skipIf(sync_playwright is None, "Playwright is not installed here")
class PageInteractions(unittest.TestCase):
    """QA 2026-11-20: a click anywhere on a worksheet line opens its
    references (not on the fields inside it); a similar-project card's
    header opens the whole bid; the jump bar and the hint are there."""

    proc = None

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get("BIDEST_E2E_URL", "").rstrip("/")
        if not cls.url:
            port = _free_port()
            py = os.path.join(ROOT, ".venv", "bin", "python")
            cls.proc = subprocess.Popen(
                [py, os.path.join(HERE, "run.py"), "--no-browser", "--port", str(port)],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cls.url = "http://127.0.0.1:%d" % port
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(cls.url + "/api/health", timeout=2) as r:
                        if b'"references": true' in r.read():
                            break
                except OSError:
                    pass
                time.sleep(1)
        cls.pw = sync_playwright().start()
        cls.engines = [e.strip() for e in os.environ.get(
            "BIDEST_E2E_ENGINES", "chromium").split(",") if e.strip()]

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()
        if cls.proc is not None:
            cls.proc.terminate()
            cls.proc.wait(timeout=30)

    def takeoff_page(self, engine, width=1280, height=900, extra=()):
        browser = getattr(self.pw, engine).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url + "/")
        page.wait_for_function(SAMPLE_READY, timeout=60000)
        page.click("#btn-sample")
        if extra:          # lines added after the sample's nine
            page.evaluate("t => { const a = document.querySelector('#paste-text'); a.value += '\\n' + t; }",
                          "\n".join(extra))
        page.click("#btn-parse-text")
        page.wait_for_function("n => S.lines.length === n && S.lines.every(l => l.cls)", arg=9 + len(extra))
        return page, errors

    def estimate_page(self, engine, width=1280, height=900, extra=()):
        page, errors = self.takeoff_page(engine, width, height, extra)
        page.click("#btn-estimate")
        page.wait_for_function("() => S.result && S.ws.refs && S.ws.refsReq === S.resultReq",
                               timeout=90000)
        # no smooth scrolling under test: boxes are measured right away
        page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
        page.wait_for_timeout(700)
        return page, errors

    def expanded(self, page, row):
        return page.get_attribute('tr[data-line="%s"]' % row, "aria-expanded")

    def assert_project_total(self, page, expected=None):
        worksheet = page.inner_text("#ws-totals .yours .big")
        self.assertEqual(page.inner_text('#r-headline [data-basis="history"] .big'), worksheet)
        self.assertEqual(page.inner_text("#result-foot tr.wstotal td.money"), worksheet)
        if expected is not None:
            self.assertAlmostEqual(float(worksheet.replace("$", "").replace(",", "")),
                                   expected, delta=0.5)

    def test_phone_rows_keep_review_price_and_amount_in_view(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine, width=390)
                for width in (320, 390):
                    page.set_viewport_size({"width": width, "height": 844})
                    for selector in ("#review-body tr[data-row]:first-child",
                                     "#result-body tr.st-priced[data-line]"):
                        row = page.locator(selector).first
                        self.assertEqual(row.evaluate("row => getComputedStyle(row).display"), "grid")
                        bounds = row.locator("td").evaluate_all("""cells => cells
                            .filter(cell => cell.getBoundingClientRect().width > 0)
                            .map(cell => [cell.getBoundingClientRect().left, cell.getBoundingClientRect().right])""")
                        self.assertTrue(all(left >= 0 and right <= width + 1 for left, right in bounds),
                                        (width, selector, bounds))
                    self.assertTrue(page.locator('#result-body tr.st-priced input.yourp').first.is_visible())
                self.assertEqual(errors, [])

    def test_invalid_bidder_counts_do_not_change_the_estimate(self):
        """A typed count must never be truncated or silently replaced by auto."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                before = page.evaluate("() => S.result.headline")
                requests = []
                page.on("request", lambda r: requests.append(r.url)
                        if r.url.endswith("/api/estimate") else None)
                for value in ("1.5", "0", "-2", "61", "1e309"):
                    with self.subTest(value=value):
                        page.fill("#in-bidders", value)
                        self.assertTrue(page.is_disabled("#btn-estimate"))
                        self.assertEqual(page.get_attribute("#in-bidders", "aria-invalid"), "true")
                        self.assertIn("whole number", page.inner_text("#bidders-hint"))
                        self.assertIn("whole number", page.inner_text("#estimate-hint"))
                        page.click("#btn-reestimate")
                        self.assertEqual(page.evaluate("() => S.result.headline"), before)
                self.assertEqual(requests, [])
                page.fill("#in-bidders", "4")
                self.assertTrue(page.is_enabled("#btn-estimate"))
                self.assertEqual(page.get_attribute("#in-bidders", "aria-invalid"), "false")
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result.competition.n_bidders === 4 && !S.stale")
                self.assertEqual(len(requests), 1)
                page.fill("#in-bidders", "1.5")
                page.click("#btn-bidders-reset")
                self.assertTrue(page.is_enabled("#btn-estimate"))
                page.fill("#in-bidders", "")
                self.assertTrue(page.is_enabled("#btn-estimate"))
                self.assertEqual(page.get_attribute("#in-bidders", "aria-invalid"), "false")
                self.assertEqual(errors, [])

    def test_an_overflowing_worksheet_price_is_rejected_and_can_be_corrected(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                field = 'tr[data-line="r3"] input.yourp'
                before = page.evaluate("() => S.result.headline")
                previous = page.input_value(field)
                previous_unit = page.evaluate("() => S.ws.lines.r3.your_unit")
                page.fill(field, "1" + "0" * 308)
                page.press(field, "Tab")
                page.wait_for_selector('#toasts .toast')
                self.assertIn("previous price is kept", page.inner_text("#toasts"))
                self.assertEqual(page.input_value(field), previous)
                self.assertEqual(page.evaluate("() => S.ws.lines.r3.your_unit"), previous_unit)
                self.assertTrue(page.evaluate("() => { const t = wsTotals(S.result); "
                                             "return [t.sub, t.cont, t.yours].every(Number.isFinite); }"))
                self.assertNotIn("∞", page.inner_text("#ws-totals"))
                page.fill(field, "44")
                page.press(field, "Tab")
                page.wait_for_function("() => S.ws.lines.r3.your_unit === 44")
                self.assertEqual(page.evaluate("() => wsAmount(S.result.lines.find(l => l.row_id === 'r3'), "
                                               "S.ws.lines.r3)"), 184800)
                self.assertEqual(page.evaluate("() => S.result.headline"), before)
                self.assertEqual(errors, [])

    def test_a_click_on_a_line_toggles_its_references(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                row = 'tr[data-line="r3"]'
                self.assertEqual(self.expanded(page, "r3"), "false")
                self.assertEqual(page.locator("button.twisty").count(), 0)
                page.click(row + " td:nth-child(2)")          # the description
                page.wait_for_selector('[data-refs="r3"][data-loaded="1"]')
                self.assertEqual(self.expanded(page, "r3"), "true")
                self.assertTrue(page.is_visible("#detail-r3"))
                self.assertEqual(page.inner_text(row + " .chev"), "▾")
                details = page.locator("#detail-r3")
                self.assertIn("Historical bids behind model P50", details.inner_text())
                history = page.evaluate("() => S.ws.refs.r3.history_price")
                recent = details.locator(".history-evidence")
                projects = recent.locator(".history-project")
                self.assertEqual(projects.count(), len(history.get("references", [])))
                for index, ref in enumerate(history.get("references", [])):
                    text = projects.nth(index).inner_text()
                    self.assertIn(ref["project"], text)
                    self.assertIn("Median at bid date", text)
                    self.assertIn("Price adjustment", text)
                    self.assertIn(f'Median in {history["target_year"]} dollars', text)
                summary = recent.locator(".history-summary")
                if history["status"] == "available":
                    self.assertEqual(projects.count(), 3)
                    adjusted = sorted(ref["adjusted_median"] for ref in history["references"])
                    for value in adjusted:
                        self.assertIn(page.evaluate("v => rate(v)", value), summary.inner_text())
                else:
                    self.assertIn(history["reason"], summary.inner_text())
                page.click('#r-headline button[data-basis="p70"]')
                page.wait_for_selector('#r-headline button[data-basis="p70"][aria-pressed="true"]')
                self.assertTrue(recent.is_visible())
                self.assertEqual(projects.count(), len(history.get("references", [])))
                page.click(row + " td:nth-child(3)")          # the quantity cell
                self.assertEqual(self.expanded(page, "r3"), "false")
                self.assertFalse(page.is_visible("#detail-r3"))
                self.assertEqual(errors, [])

    def test_a_click_inside_a_field_does_not_toggle(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                row = 'tr[data-line="r4"]'
                page.click(row + " input.yourp")
                page.click(row + " input.note")
                page.select_option(row + " select.pick", "model_p70")
                self.assertEqual(self.expanded(page, "r4"), "false")
                page.wait_for_function("sel => document.querySelector(sel).value !== ''", arg=row + " input.yourp")
                picked = page.input_value(row + " input.yourp")
                self.assertGreater(float(picked.replace(",", "")), 0)
                for basis in ("p50", "history"):
                    page.click(f'#r-headline button[data-basis="{basis}"]')
                    page.wait_for_selector(f'#r-headline button[data-basis="{basis}"][aria-pressed="true"]')
                    self.assertEqual(page.input_value(row + " input.yourp"), picked)
                    self.assertEqual(page.input_value(row + " select.pick"), "model_p70")
                page.fill(row + " input.note", "checked with the vendor")
                page.keyboard.press("Enter")       # Enter in a field: no toggle
                self.assertEqual(self.expanded(page, "r4"), "false")
                self.assertEqual(errors, [])

    def test_the_reference_column_shows_and_changes_the_source(self):
        """2026-11-21 (user): a column of its own, "Reference", says where each
        unit price comes from. It starts on the approved recent median when
        available, short enough to read whole; picking another there
        changes the price, which flashes; a price rounded from its source
        says so; a price you type reads "your price"."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                self.assertIn("\treference\t", page.inner_text("#result-table thead").lower())
                row = 'tr[data-line="r3"]'
                sel = row + " td.refcell select.pick"
                shown = lambda: page.evaluate("s => { const e = document.querySelector(s); "
                                              "return e.options[e.selectedIndex].text; }", sel)
                hp = page.evaluate("() => S.ws.refs.r3.history_price")
                if hp["status"] == "available":
                    self.assertTrue(shown().startswith("Recent bid median"), shown())
                else:
                    self.assertEqual(page.input_value(row + " input.yourp"), "")
                self.assertTrue(page.evaluate("() => Object.values(S.ws.lines).every(w => w.src !== 'model')"))
                want = page.evaluate("() => S.ws.refs && S.ws.refs.r3 && S.ws.refs.r3.range.median")
                page.select_option(sel, "ref_median")
                page.wait_for_timeout(300)
                self.assertTrue(shown().startswith("Ref. median ("), shown())
                got = float(page.input_value(row + " input.yourp").replace(",", ""))
                self.assertAlmostEqual(got, page.evaluate("p => nicePrice(p, 'up')", want), places=2)
                self.assertIn("flash", page.get_attribute(row + " input.yourp", "class"))
                self.assertEqual(self.expanded(page, "r3"), "false")
                # a price you type: the column says so
                page.fill(row + " input.yourp", "44")
                page.press(row + " input.yourp", "Tab")
                page.wait_for_timeout(300)
                self.assertEqual(shown().lower(), "your price")
                self.assertEqual(errors, [])

    def test_opcc_conventions_on_the_worksheet(self):
        """Automatic prices round up by default; optional rounding still works;
        mobilization and temporary erosion control are the engineer's to
        fill in; the references show the average bid. P50/P70 guidance
        opens in the User Manual without losing the worksheet."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                self.assertEqual(page.input_value("#ws-round"), "up")
                for k in ("r3", "r4", "r7"):
                    hp = page.evaluate("k => S.ws.refs[k].history_price", k)
                    value = page.input_value('tr[data-line="%s"] input.yourp' % k)
                    if hp["status"] == "available":
                        want = page.evaluate("v => nicePrice(v, 'up')", hp["unit_price"])
                        self.assertEqual(float(value), want)
                        self.assertEqual(hp["n_projects"], 3)
                    else:
                        self.assertEqual(value, "")
                self.assertEqual(page.evaluate("() => [nicePrice(48, 'up'), nicePrice(137.2, 'up'), "
                                               "nicePrice(2022, 'nearest'), nicePrice(37585, 'up'), "
                                               "nicePrice(6.07, 'up'), nicePrice(41.3, 'off')]"),
                                 [50, 140, 2000, 38000, 6.5, 41.3])
                # mobilization (r1) and temporary erosion control (r2): blank
                for k in ("r1", "r2"):
                    self.assertEqual(page.input_value('tr[data-line="%s"] input.yourp' % k), "")
                    self.assertEqual(page.get_attribute('tr[data-line="%s"] input.yourp' % k,
                                                        "placeholder").lower(), "engineer input")
                    self.assertIn("engineer input", page.inner_text('tr[data-line="%s"]' % k).lower())
                # the references show the average bid
                page.click('tr[data-line="r3"] td:nth-child(2)')
                page.wait_for_selector('[data-refs="r3"][data-loaded="1"]', timeout=30000)
                self.assertIn("average bid $/unit", page.inner_text('[data-refs="r3"] thead').lower())
                # Guidance has its own tab; reading it leaves this work intact.
                self.assertEqual(page.locator("#r-headline details.meaning, #r-headline .howcalc").count(), 0)
                self.assertEqual(page.get_attribute("#user-manual", "href"), "/manual.html")
                self.assertEqual(page.get_attribute("#user-manual", "target"), "_blank")
                before = page.evaluate("() => ({result: S.result, worksheet: S.ws})")
                with page.context.expect_page() as opened:
                    page.click("#user-manual")
                manual = opened.value
                manual_errors = []
                manual.on("pageerror", lambda e: manual_errors.append(str(e)))
                manual.wait_for_load_state()
                self.assertEqual(manual.url, self.url + "/manual.html")
                self.assertIn("User Manual", manual.title())
                guidance = manual.locator("#p50-p70")
                self.assertIn("P50", guidance.inner_text())
                self.assertIn("P70", guidance.inner_text())
                self.assertIn("not been independently validated", guidance.inner_text())
                charts = manual.locator("#p50p70-charts svg.pp")
                self.assertEqual(charts.count(), 2)
                for chart in charts.all():
                    self.assertTrue(chart.is_visible())
                manual.set_viewport_size({"width": 375, "height": 812})
                self.assertLessEqual(manual.evaluate("() => Math.max(document.documentElement.scrollWidth, "
                                                    "document.body.scrollWidth)"), 375)
                self.assertEqual(manual_errors, [])
                manual.close()
                self.assertEqual(page.url, self.url + "/")
                self.assertEqual(page.evaluate("() => ({result: S.result, worksheet: S.ws})"), before)
                self.assertEqual(errors, [])

    def test_a_not_covered_line_with_your_number_stops_reminding(self):
        """2026-11-21 (user): once a not-covered line has your number (a
        worksheet price, or a vendor quote), the reminders about it go."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                k = page.evaluate("() => S.result.lines.find(l => l.status === 'not_covered').row_id")
                self.assertTrue(page.locator('#r-warnings [data-ack="not_covered"]').is_visible())
                self.assertIn("no bid history", page.inner_text("#r-warnings"))
                self.assertEqual(page.evaluate("k => wsJudgment(S.result.lines.find(l => l.row_id === k))", k),
                                 ["not covered"])
                inp = 'tr[data-line="%s"] input.yourp' % k
                page.fill(inp, "45000")
                page.press(inp, "Tab")
                page.wait_for_timeout(400)
                self.assertEqual(page.locator('#r-warnings [data-ack="not_covered"]').count(), 0)
                self.assertNotIn("no bid history", page.inner_text("#r-warnings"))
                self.assertIn("$90,000", page.inner_text("#r-notcovered"))
                self.assertEqual(page.evaluate("k => wsJudgment(S.result.lines.find(l => l.row_id === k))", k), [])
                self.assertEqual(errors, [])

    def test_not_covered_shows_past_bids_on_hover(self):
        """2026-11-21 (user): the past public bids for a not-covered line
        show in a small box when the pointer is on its "not covered" badge,
        not as a line of their own; the line does not open."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                k = page.evaluate("() => S.result.lines.find(l => l.status === 'not_covered').row_id")
                badge = 'tr[data-line="%s"] .badge.unc.hashint' % k
                self.assertFalse(page.is_visible("#hint-pop"))
                page.hover(badge)
                page.wait_for_selector("#hint-pop:not([hidden])")
                text = page.inner_text("#hint-pop")
                self.assertIn("Past public bids", text)
                self.assertIn("not in P50 / P70", text)
                self.assertIn("per EA", text)
                page.mouse.move(5, 5)
                self.assertFalse(page.is_visible("#hint-pop"))
                page.click(badge)                    # a tap: the box, no toggle
                self.assertTrue(page.is_visible("#hint-pop"))
                self.assertEqual(self.expanded(page, k), "false")
                self.assertEqual(errors, [])

    def test_a_not_covered_line_has_one_number(self):
        """2026-11-21 (user): the vendor quote under Project details, the
        line's amount cell in the takeoff and its price on the worksheet are
        one number: typing in any of them changes the others, with no new
        estimate (P50 / P80 leave the line out)."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                k = page.evaluate("() => S.result.lines.find(l => l.status === 'not_covered').row_id")
                q = page.evaluate("k => S.result.lines.find(l => l.row_id === k).quantity", k)
                ws_in = 'tr[data-line="%s"] input.yourp' % k
                pd_in = '#quotes-box input[data-fk="q:%s"]' % k
                tk_in = '#review-table input[data-fk="g:%s:quote"]' % k
                num = lambda sel: float(page.input_value(sel).replace(",", "") or "nan")
                # the worksheet price (per unit) sets the quote everywhere
                page.fill(ws_in, "45000")
                page.press(ws_in, "Tab")
                page.wait_for_timeout(400)
                self.assertEqual(page.evaluate("k => S.lines.find(l => l.row_id === k).quote", k), 45000 * q)
                self.assertEqual(num(pd_in), 45000 * q)
                self.assertEqual(num(tk_in), 45000 * q)
                # the quote under Project details sets the worksheet price
                page.fill(pd_in, "120,000")
                page.press(pd_in, "Tab")
                page.wait_for_timeout(400)
                self.assertAlmostEqual(num(ws_in), 120000 / q, places=2)
                self.assertEqual(num(tk_in), 120000)
                # and the takeoff's amount cell sets both
                page.fill(tk_in, "90,000")
                page.press(tk_in, "Tab")
                page.wait_for_timeout(400)
                self.assertEqual(num(pd_in), 90000)
                self.assertAlmostEqual(num(ws_in), 90000 / q, places=2)
                self.assertIn("$90,000", page.inner_text("#r-notcovered"))
                self.assertFalse(page.evaluate("() => S.stale"))
                self.assertEqual(errors, [])

    def test_recent_bid_defaults_preserve_models_and_export_their_sources(self):
        """Start from recent bids, switch automatic prices, and export the chosen basis."""
        import io
        import openpyxl
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.select_option("#ws-round", "off")
                headline = page.evaluate("() => S.result.headline")
                page.evaluate("""() => {
                    const target = S.result.lines.find(l => l.row_id === 'r3');
                    const refs = [1, 2, 3].map(k => ({key: 'public-test-' + k,
                        project: 'Public reference ' + k, owner: 'Public owner', date: '2025-0' + k + '-10',
                        item_no: '10', item: target.description, unit: target.unit, quantity: target.quantity,
                        median_unit: (10.345 + k) / 1.05, adjusted_median: 10.345 + k, escalation: 1.05,
                        index_evidence: {reason: 'Matched public bid index', notes: ['Prior dates only']},
                        match_basis: ['Same unit and item'], source: {url: 'https://example.org/bids/' + k}}));
                    S.ws.refs.r3.history_price = {status: 'available', unit_price: 12.345, n_projects: 3,
                        references: refs, reason: 'Three matching recent projects', evidence: ['Adjusted to bid-year prices'],
                        method: 'recent_matching_bid_medians_v1', price_basis: 'bid_date_adjusted', target_year: 2026};
                    // Deliberately conflicting model evidence must not describe the history default.
                    target.basis = 'Priced from 2 earlier projects';
                    S.ws.refs.r3.n_refs = 2;
                    S.ws.refs.r3.range = {low: 1.68, median: 2.5, high: 4.5, mean: 2.9};
                    S.ws.refs.r4.history_price = {status: 'insufficient', unit_price: null, n_projects: 2,
                        references: refs.slice(0, 2), reason: 'Only two matching recent projects; enter your unit price.'};
                    S.ws.refs.r5.range = {low: 90, median: 96.5, high: 110};
                    S.ws.refs.r5.n_refs = 3;
                    S.ws.lines.r3 = {src: '', note: ''}; S.ws.lines.r4 = {src: '', note: ''};
                    renderWsNow();
                }""")
                price = 'tr[data-line="r3"] input.yourp'
                self.assertEqual(page.input_value(price), "12.35")
                self.assertEqual(page.input_value('tr[data-line="r4"] input.yourp'), "")
                self.assertIn("rounded to cents", page.inner_text('tr[data-line="r3"]').lower())
                self.assertEqual(page.inner_text("#th-model").lower(), "unit price comparison")
                row = page.locator('tr[data-line="r3"]')
                comparison = row.locator(".unit-comparison")
                rates = {basis: comparison.locator(f'[data-rate="{basis}"] .comparison-value')
                         for basis in ("history", "p50", "p70")}
                displayed = {basis: rate.inner_text() for basis, rate in rates.items()}
                p50_rate = page.evaluate("() => S.result.lines.find(l => l.row_id === 'r3').unit_price_p50")
                for basis, expected in (("history", 12.345), ("p50", p50_rate),
                                        ("p70", p50_rate * headline["p70_multiplier"])):
                    self.assertTrue(rates[basis].is_visible())
                    self.assertAlmostEqual(float(displayed[basis].lstrip("$").replace(",", "")),
                                           expected, delta=0.0051)
                self.assertIn("3 recent projects", row.locator(".desccell").inner_text())
                self.assertNotIn("2 earlier projects", row.inner_text())
                self.assertNotIn("2 references", row.inner_text())
                reference_range = row.locator(".ref-range").inner_text()
                self.assertIn("$11.35 – $13.35", reference_range)
                self.assertIn("Median $12.35 · 3 projects", reference_range)
                self.assertNotIn("$1.68", reference_range)
                selected = row.locator(".selected-source")
                self.assertEqual(selected.inner_text(), "Recent bid median · $12.35")
                self.assertTrue(selected.is_visible())
                self.assertTrue(selected.evaluate("e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight"))
                self.assertIn("2 recent projects · 3 required", page.inner_text('tr[data-line="r4"] .ref-range'))
                self.assertEqual(page.locator('#ws-totals .basis button').count(), 0)
                before = page.evaluate("() => wsDoc()")
                cards = {basis: page.locator(f'#r-headline button[data-basis="{basis}"]')
                         for basis in ("history", "p50", "p70")}
                for basis, card in cards.items():
                    self.assertTrue(card.is_enabled())
                    self.assertEqual(card.get_attribute("aria-pressed"), str(basis == "history").lower())
                priced = sum(line["your_amount"] for line in before["worksheet"]["lines"]
                             if line["your_amount"] is not None)
                self.assert_project_total(page, priced * 1.10)
                self.assertNotIn("past jobs exceeded", page.inner_text("#ws-totals"))
                record = next(l for l in before["worksheet"]["lines"] if l["row_id"] == "r3")
                self.assertEqual(before["worksheet"]["basis"], "history")
                self.assertIsNone(before["worksheet"]["basis_multiplier"])
                self.assertEqual(record["raw"], 12.345)
                self.assertEqual(record["your_unit"], 12.35)
                self.assertEqual(len(record["history_price"]["references"]), 3)
                qty = page.evaluate("() => S.result.lines.find(l => l.row_id === 'r3').quantity")
                self.assertAlmostEqual(record["your_amount"], qty * 12.35)
                for k in (1, 2, 3):
                    self.assertIn("Public reference " + str(k), record["source"])
                    self.assertIn("2025-0%d-10" % k, record["source"])
                payload = page.evaluate("() => ({result: S.result, request: S.resultReq, worksheet: wsDoc().worksheet})")
                response = page.request.post(self.url + "/api/worksheet/export/xlsx", data=payload)
                self.assertEqual(response.status, 200)
                wb = openpyxl.load_workbook(io.BytesIO(response.body()), data_only=True)
                exported = "\n".join(str(c.value) for sh in wb for row in sh for c in row if c.value is not None)
                for k in (1, 2, 3):
                    self.assertIn("Public reference " + str(k), exported)
                    self.assertIn("2025-0%d-10" % k, exported)
                # The same schedule includes a hand-entered rate, an explicitly
                # selected bid reference and a vendor quote alongside the automatic rate.
                typed = 'tr[data-line="r4"] input.yourp'
                page.fill(typed, "123.45")
                page.press(typed, "Tab")
                note = 'tr[data-line="r3"] input.note'
                page.fill(note, "Checked excavation quantity")
                page.press(note, "Tab")
                page.select_option('tr[data-line="r5"] select.pick', "ref_median")
                vendor = '#quotes-box input[data-fk="q:r9"]'
                page.fill(vendor, "50,000")
                page.press(vendor, "Tab")
                edits = page.evaluate("() => wsDoc().worksheet.lines")
                protected = {l["row_id"]: (l["your_unit"], l["your_amount"], l["src"])
                             for l in edits if l["row_id"] in ("r4", "r5", "r9")}
                self.assertEqual(protected["r4"][0], 123.45)
                self.assertEqual(protected["r5"][2], "ref_median")
                self.assertEqual(protected["r9"][1:], (50000, "quote"))
                manual_total = sum(line["your_amount"] for line in edits if line["your_amount"] is not None) * 1.10
                self.assert_project_total(page, manual_total)
                excavation = page.evaluate("() => S.result.lines.find(l => l.row_id === 'r3')")
                totals = {}
                for step, (basis, key) in enumerate((("p50", None), ("p70", "Enter"), ("p50", "Space"))):
                    if key:
                        cards[basis].focus()
                        cards[basis].press(key)
                    else:
                        cards[basis].click()
                    page.wait_for_function("b => document.querySelector('button[data-basis=' + b + ']').getAttribute('aria-pressed') === 'true'", arg=basis)
                    for option, card in cards.items():
                        self.assertEqual(card.get_attribute("aria-pressed"), str(option == basis).lower())
                    doc = page.evaluate("() => wsDoc()")
                    self.assertEqual(doc["worksheet"]["basis"], basis)
                    multiplier = 1 if basis == "p50" else headline["p70_multiplier"]
                    self.assertEqual(doc["worksheet"]["basis_multiplier"], multiplier)
                    rate = excavation["unit_price_p50"] * multiplier
                    lines = {l["row_id"]: l for l in doc["worksheet"]["lines"]}
                    self.assertAlmostEqual(lines["r3"]["your_unit"], rate)
                    self.assertAlmostEqual(lines["r3"]["your_amount"], qty * rate)
                    self.assertAlmostEqual(float(page.input_value(price).replace(",", "")), rate, delta=0.0051)
                    self.assertEqual({key: value.inner_text() for key, value in rates.items()}, displayed)
                    self.assertEqual(page.input_value(note), "Checked excavation quantity")
                    for row_id, preserved in protected.items():
                        l = lines[row_id]
                        self.assertEqual((l["your_unit"], l["your_amount"], l["src"]), preserved)
                    total = doc["totals"]["your_opcc"]
                    self.assert_project_total(page, total)
                    if step == 2:
                        self.assertEqual(total, totals["p50"])
                    totals[basis] = total
                    if basis == "p70":
                        with page.expect_download() as download:
                            page.click("#btn-export-xlsx")
                        with download.value.path().open("rb") as stream:
                            book = openpyxl.load_workbook(stream, data_only=True)
                        summary = {label: value for label, value in
                                   book["Summary"].iter_rows(max_col=2, values_only=True)}
                        self.assertIn("P70", summary["Prices start from"])
                        self.assertAlmostEqual(summary["Project Total"], total, places=2)
                        opcc_row = next(row for row in book["OPCC"].values if row[1] == excavation["description"])
                        self.assertAlmostEqual(opcc_row[4], rate, places=4)
                        book.close()
                self.assertGreater(totals["p70"], totals["p50"])
                self.assertEqual(page.evaluate("() => S.result.headline"), headline)
                cards["history"].click()
                page.wait_for_selector('#r-headline button[data-basis="history"][aria-pressed="true"]')
                self.assertEqual(page.input_value(price), "12.35")
                self.assertEqual(page.input_value(note), "Checked excavation quantity")
                self.assert_project_total(page, manual_total)
                restored = page.evaluate("() => wsDoc().worksheet")
                self.assertEqual(restored["basis"], "history")
                for line in restored["lines"]:
                    if line["row_id"] in protected:
                        self.assertEqual((line["your_unit"], line["your_amount"], line["src"]),
                                         protected[line["row_id"]])
                # A missing project percentile cannot replace a usable basis.
                cards["p50"].click()
                page.evaluate("""() => {
                    delete S.result.headline.p70; delete S.result.headline.p70_multiplier;
                    renderHeadline(S.result); renderWsNow();
                }""")
                self.assertTrue(cards["p70"].is_disabled())
                self.assertEqual(cards["p50"].get_attribute("aria-pressed"), "true")
                self.assertEqual(page.evaluate("() => wsDoc().worksheet.basis"), "p50")
                # Existing saved P80 worksheets are interpreted without migration.
                page.evaluate("() => { S.ws.basis = 'p80'; S.ws.lines.r3.src = 'model'; renderWsNow(); }")
                saved = page.evaluate("() => wsDoc()")
                self.assertEqual(saved["worksheet"]["basis"], "p80")
                self.assertEqual(saved["worksheet"]["basis_multiplier"], headline["p80_multiplier"])
                self.assertEqual(saved["model"]["p80"], headline["p80"])
                self.assertEqual(page.inner_text("#th-model").lower(), "unit price comparison")
                self.assertEqual(errors, [])

    def test_recent_bid_loading_preserves_typed_prices_and_ignores_stale_responses(self):
        """Only current references fill untouched defaults; typing and custom
        picks survive late or failed reference requests."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.select_option("#ws-round", "off")
                page.evaluate("""() => {
                    window.savedFetch = window.fetch; window.pendingRefs = [];
                    window.fetch = (url, options) => url === '/api/worksheet/refs'
                        ? new Promise((resolve, reject) => pendingRefs.push({
                            resolve: body => resolve(new Response(JSON.stringify(body),
                                {status: 200, headers: {'Content-Type': 'application/json'}})),
                            reject, body: JSON.parse(options.body)}))
                        : savedFetch(url, options);
                    window.testRefs = Object.values(S.ws.refs);
                    const hp = {status: 'available', unit_price: 12.345, n_projects: 3,
                        references: [1, 2, 3].map(k => ({key: 'ref-' + k, project: 'Public reference ' + k,
                            date: '2025-01-0' + k, median_unit: 12.345, adjusted_median: 12.345})),
                        reason: 'Three recent projects', evidence: [], method: 'recent_matching_bid_medians_v1', price_basis: 'bid_date_adjusted'};
                    testRefs.forEach(r => { if (r.row_id === 'r3') r.history_price = hp; });
                    S.ws.lines.r3 = {src: '', note: ''};
                }""")
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 1")
                price = 'tr[data-line="r3"] input.yourp'
                self.assertEqual(page.input_value(price), "")
                page.fill(price, "43.21")
                page.press(price, "Tab")
                page.evaluate("() => pendingRefs[0].resolve({lines: testRefs})")
                page.wait_for_function("() => S.ws.refsReq === S.resultReq")
                self.assert_project_total(page)
                self.assertEqual(page.input_value(price), "43.21")
                self.assertEqual(page.evaluate("() => S.ws.lines.r3.src"), "you")
                # Two overlapping requests: resolving the old one does not
                # install its references, and an old failure does not set an error.
                page.evaluate("() => { S.ws.lines.r3 = {src: '', note: ''}; }")
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 2")
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 3")
                page.evaluate("() => pendingRefs[1].reject(new Error('old request failed'))")
                page.wait_for_timeout(100)
                self.assertEqual(page.evaluate("() => S.ws.refsError"), "")
                self.assertIsNone(page.evaluate("() => S.ws.refs"))
                page.evaluate("() => pendingRefs[2].resolve({lines: testRefs})")
                page.wait_for_function("() => S.ws.refsReq === S.resultReq")
                self.assertEqual(page.input_value(price), "12.35")
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 4")
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 5")
                page.evaluate("""() => {
                    const old = JSON.parse(JSON.stringify(testRefs));
                    old.find(r => r.row_id === 'r3').history_price.unit_price = 999;
                    pendingRefs[3].resolve({lines: old});
                }""")
                page.wait_for_timeout(100)
                self.assertIsNone(page.evaluate("() => S.ws.refs"))
                self.assertEqual(page.input_value(price), "")
                page.evaluate("() => pendingRefs[4].resolve({lines: testRefs})")
                page.wait_for_function("() => S.ws.refsReq === S.resultReq")
                self.assertEqual(page.input_value(price), "12.35")
                # A deliberate reference pick is not an automatic default.
                page.select_option('tr[data-line="r3"] select.pick', "ref_median")
                picked = page.input_value(price)
                page.click("#btn-estimate")
                page.wait_for_function("() => pendingRefs.length === 6")
                page.evaluate("() => pendingRefs[5].reject(new Error('references unavailable'))")
                page.wait_for_function("() => !!S.ws.refsError")
                self.assertEqual(page.input_value(price), picked)
                self.assertIn("could not be loaded", page.inner_text("#ws-totals"))
                self.assert_project_total(page)
                self.assertEqual(errors, [])

    def test_the_contingency_changes_numbers_not_the_layout(self):
        """2026-11-21 (user): picking 0%, 5% or 10% changes the numbers
        only; nothing on the page moves."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.takeoff_page(engine)
                sel = "#in-contingency"
                self.assertEqual(page.locator("#sec-project " + sel).count(), 1)
                self.assertTrue(page.is_visible(sel))
                self.assertFalse(page.is_visible("#sec-results"))
                self.assertEqual(page.input_value(sel), "10")
                self.assertTrue(page.eval_on_selector(sel, """e =>
                    Boolean(e.compareDocumentPosition(document.querySelector('#btn-estimate')) &
                            Node.DOCUMENT_POSITION_FOLLOWING)"""))
                page.select_option(sel, "15")
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws.refsReq === S.resultReq", timeout=90000)
                self.assertEqual(page.input_value("#ws-round"), "up")
                self.assertEqual(page.locator('#ws-totals select').count(), 0)
                totals = page.evaluate("() => wsDoc().totals")
                self.assertGreater(totals["subtotal"], 0)
                self.assertEqual(totals["contingency_pct"], 15)
                self.assert_project_total(page, totals["subtotal"] * 1.15)
                page.evaluate("document.documentElement.style.scrollBehavior = 'auto'")
                boxes = lambda: page.evaluate("""() => [
                    '#in-contingency', '#ws-totals .wstot .cmp',
                    '#ws-totals .wstot .yours', '#ws-meta', '#result-table'].map(q => {
                      const b = document.querySelector(q).getBoundingClientRect();
                      return [Math.round(b.x), Math.round(b.y + window.scrollY)]; })""")
                page.select_option(sel, "10")
                page.wait_for_timeout(300)
                ref = boxes()
                for v in ("0", "5", "10", "0"):
                    page.select_option(sel, v)
                    page.wait_for_timeout(300)
                    self.assert_project_total(page, totals["subtotal"] * (1 + int(v) / 100))
                    self.assertEqual(boxes(), ref, v)
                    self.assertEqual(page.locator("#ws-totals .cguide table").count(), 0)
                    self.assertNotIn("Your OPCC is", page.inner_text("#ws-totals .cguide"))
                    self.assertNotIn("past jobs exceeded", page.inner_text("#ws-totals .cguide"))
                    self.assertEqual(page.locator("#result-foot tr.wscont").count(), 1)
                self.assertIn("(0% of the subtotal)", page.inner_text("#result-foot tr.wscont"))
                self.assertIn("$0", page.inner_text("#result-foot tr.wscont"))
                self.assertIn("construction contingency 0% $0", page.inner_text("#ws-totals .yours").lower())
                self.assertEqual(errors, [])

    def test_every_edit_reaches_the_exported_excel(self):
        """2026-11-21 (user): change every editable thing on the page to a
        different value, export, and check the Excel really carries each
        change: the heading, P50 / P70, rounding, a typed price, an engineer's
        number, a Reference pick, a note, a not-covered quote, the
        contingency -- line by line and in the totals."""
        import tempfile
        from worksheet import price_source
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.select_option("#ws-round", "off")                       # no rounding
                page.wait_for_timeout(300)
                for sel, val in (("#ws-project", "Test Creek Repair 7"), ("#ws-prepared", "QA"),
                                 ("#ws-projno", "T-777")):
                    page.fill(sel, val)
                    page.press(sel, "Tab")
                def price(row, v):
                    page.fill('tr[data-line="%s"] input.yourp' % row, v)
                    page.press('tr[data-line="%s"] input.yourp' % row, "Tab")
                    page.wait_for_timeout(300)
                price("r1", "30000")                                         # engineer input
                page.select_option('tr[data-line="r3"] td.refcell select.pick', "ref_median")
                page.wait_for_timeout(300)
                page.fill('tr[data-line="r4"] input.note', "checked with the vendor")
                page.press('tr[data-line="r4"] input.note', "Tab")
                price("r5", "33")                                            # a typed price
                k = page.evaluate("() => S.result.lines.find(l => l.status === 'not_covered').row_id")
                q = '#quotes-box input[data-fk="q:%s"]' % k                   # a vendor quote
                page.fill(q, "50,000")
                page.press(q, "Tab")
                page.wait_for_timeout(300)
                page.select_option("#in-contingency", "15")
                page.wait_for_timeout(400)
                # what the page shows now
                doc = page.evaluate("() => wsDoc()")
                lines = {l["row_id"]: l for l in doc["worksheet"]["lines"]}
                by_desc = {l["description"]: l for l in page.evaluate("() => S.result.lines")}
                tot = doc["totals"]
                self.assertEqual(lines["r1"]["your_unit"], 30000)
                self.assertEqual(lines["r5"]["your_unit"], 33)
                self.assertEqual(lines[k]["your_amount"], 50000)
                # export and read the file
                with page.expect_download() as dl:
                    page.click("#btn-export-xlsx")
                path = os.path.join(tempfile.mkdtemp(), "export.xlsx")
                dl.value.save_as(path)
                x = read_xlsx(path)
                o, sm = x["OPCC"], x["Summary"]
                self.assertEqual((o.get("A3"), o.get("F4"), o.get("F5")), ("Test Creek Repair 7", "QA", "T-777"))
                head = next(int(c[1:]) for c, v in o.items() if c.startswith("A") and v == "Item No.")
                seen = 0
                for r in range(head + 1, head + 60):
                    d = o.get("B%d" % r)
                    if d not in by_desc:
                        continue
                    l = by_desc[d]
                    w = lines[l["row_id"]]
                    if w["your_amount"] is None:
                        self.assertIsNone(o.get("F%d" % r), d)
                    else:
                        self.assertAlmostEqual(o["F%d" % r], w["your_amount"], places=2, msg=d)
                    if l["status"] not in ("owner_set",) and w["your_unit"] is not None:
                        self.assertAlmostEqual(o["E%d" % r], w["your_unit"], places=4, msg=d)
                    expected_source = price_source(w)
                    if w["your_unit"] is None and w["your_amount"] is None:
                        self.assertEqual(o.get("E%d" % r), "No price yet", d)
                        expected_source = "No price yet" + (" (%s)" % expected_source if expected_source else "")
                    self.assertEqual(o.get("G%d" % r) or "", expected_source, d)
                    if w["src"] == "history":
                        for ref in w["history_price"]["references"]:
                            self.assertIn(ref["project"], o["G%d" % r], d)
                            self.assertIn(ref["date"], o["G%d" % r], d)
                    self.assertEqual(o.get("H%d" % r) or "", w["note"] or "", d)     # beside the source
                    seen += 1
                self.assertEqual(seen, len(by_desc))
                rows = {v: int(c[1:]) for c, v in o.items() if c.startswith("B") and isinstance(v, str)}
                self.assertAlmostEqual(o["F%d" % rows["Sub-Total"]], tot["subtotal"], places=2)
                self.assertAlmostEqual(o["F%d" % rows["Construction Contingency (15%)"]],
                                       tot["subtotal"] * 0.15, places=2)
                self.assertAlmostEqual(o["F%d" % rows["Project Total"]], tot["your_opcc"], places=2)
                # the summary says where the prices started, and the same total
                srow = {v: c for c, v in sm.items() if isinstance(v, str)}
                self.assertIn("recent matching public bid medians", sm["B" + srow["Prices start from"][1:]])
                self.assertAlmostEqual(sm["B" + srow["Project Total"][1:]], tot["your_opcc"], places=2)
                self.assertEqual(errors, [])

    def test_a_note_typed_goes_into_the_export(self):
        """2026-11-21 (user): a note typed on a line, and the export clicked
        straight after, carries the note (the OPCC sheet shows it beside the
        line's source)."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.fill('tr[data-line="r4"] input.note', "checked with the vendor")
                with page.expect_request(lambda r: "/api/worksheet/export/xlsx" in r.url) as req:
                    with page.expect_download():
                        page.click("#btn-export-xlsx")
                body = json.loads(req.value.post_data)
                notes = {l["row_id"]: l["note"] for l in body["worksheet"]["lines"]}
                self.assertEqual(notes["r4"], "checked with the vendor")
                self.assertEqual(errors, [])

    def test_one_export_in_its_own_step(self):
        """2026-11-21 (user): the exports are in the Export step only -- the
        OPCC workbook (Excel) and the OPCC as it prints (PDF); no worksheet
        toolbar, no CSV or JSON, no other download button."""
        import zipfile
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                self.assertTrue(page.is_visible("main > #sec-export:last-child"))
                self.assertEqual(page.get_attribute("#jump-export", "href"), "#sec-export")
                page.click("#jump-export")
                self.assertTrue(page.url.endswith("#sec-export"))
                self.assertEqual(page.locator("#sec-export button").count(), 2)
                for gone in ("#ws-tools", "#ws-filter", "#ws-save", "#ws-open", "#ws-xlsx",
                             "#ws-csv", "#ws-json", "#btn-export-csv"):
                    self.assertEqual(page.locator(gone).count(), 0, gone)
                body = page.inner_text("body")
                for word in ("Download CSV", "Download JSON", "only lines that need your judgment"):
                    self.assertNotIn(word, body)
                with page.expect_download() as dl:
                    page.click("#btn-export-xlsx")
                self.assertTrue(dl.value.suggested_filename.endswith(".xlsx"))
                with zipfile.ZipFile(dl.value.path()) as z:
                    names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
                self.assertEqual(names, ["OPCC", "Summary", "Lines", "References"])
                # and the OPCC as it prints, as a PDF (user, 2026-11-21)
                with page.expect_download() as dl2:
                    page.click("#btn-export-pdf")
                self.assertTrue(dl2.value.suggested_filename.endswith(".pdf"))
                with open(dl2.value.path(), "rb") as fh:
                    self.assertTrue(fh.read(5).startswith(b"%PDF"))
                self.assertEqual(errors, [])

    def test_warnings_are_short_and_tick_to_gray(self):
        """2026-11-21 (user): each warning is short (what, what to do, which
        lines) and has a box to tick once read; ticked, it turns gray.
        Nothing is blocked by it."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                cards = page.locator("#r-warnings .notice.ack")
                n = cards.count()
                self.assertGreater(n, 0)
                for k in range(n):
                    self.assertLess(len(cards.nth(k).locator("b").inner_text()), 110)
                first = cards.nth(0)
                self.assertNotIn("acked", first.get_attribute("class"))
                first.locator("input[type=checkbox]").check()
                self.assertIn("acked", first.get_attribute("class"))
                self.assertEqual(page.eval_on_selector("#r-warnings .notice.ack.acked",
                                                       "e => getComputedStyle(e).backgroundColor"),
                                 page.evaluate("() => getComputedStyle(document.querySelector('.notice.acked'))"
                                               ".backgroundColor"))
                # the tick holds when the warnings are drawn again
                page.evaluate("() => renderWarnings(S.result)")
                self.assertIn("acked", page.locator("#r-warnings .notice.ack").nth(0).get_attribute("class"))
                # nothing blocked: the export still works
                self.assertTrue(page.is_enabled("#btn-export-xlsx"))
                self.assertEqual(errors, [])

    def test_change_opens_on_our_reading(self):
        """Bug 24: the Change dialog opened on the last item type."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.evaluate("() => { document.querySelector('#sec-input').scrollIntoView(); }")
                # a line with no item type of its own (the memory may hold others)
                k = page.evaluate("() => S.lines.findIndex(l => !l.family)")
                self.assertGreaterEqual(k, 0)
                page.locator("#review-body tr").nth(k).get_by_role("button", name="Change", exact=True).click()
                page.wait_for_selector("#modal-host .modal select")
                self.assertEqual(page.eval_on_selector("#modal-host .modal select", "s => s.value"), "")
                self.assertIn("Not sure", page.eval_on_selector(
                    "#modal-host .modal select", "s => s.options[s.selectedIndex].text"))
                self.assertIn("Each line is matched to an item type", page.inner_text("#modal-host .modal"))
                page.click("#modal-host .modal button.close")
                self.assertEqual(errors, [])

    def test_new_item_needs_engineer_price_and_keeps_it_in_export(self):
        """An explicit New item choice leaves the price to the engineer;
        basis, rounding and re-estimating cannot replace that entered price."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                takeoff_row = page.locator('#review-body tr[data-row="r3"]')
                price = '#result-body tr[data-line="r3"] input.yourp'
                original = page.evaluate("() => S.result.lines.find(l => l.row_id === 'r3')")
                self.assertEqual(original["status"], "priced")
                prior_price = page.input_value(price)
                missing_before = page.evaluate("() => wsTotals(S.result).missing")
                takeoff_row.get_by_role("button", name="Change", exact=True).click()
                picker = page.locator("#modal-host .modal")
                self.assertIn("New item / Other", picker.locator('option[value="__new_item__"]').inner_text())
                picker.locator("select").select_option("__new_item__")
                picker.get_by_role("button", name="Apply", exact=True).click()
                page.wait_for_function("() => S.lines.find(l => l.row_id === 'r3').pricing_mode === 'engineer' && S.stale")
                # The existing result still describes the previous estimate.
                self.assertEqual(page.input_value(price), prior_price)
                page.click("#btn-reestimate")
                page.wait_for_function("() => !S.stale && S.result.lines.find(l => l.row_id === 'r3').pricing_mode === 'engineer'", timeout=60000)
                self.assertEqual(page.input_value(price), "")
                self.assertEqual(page.get_attribute(price, "placeholder").lower(), "engineer input")
                self.assertGreaterEqual(page.evaluate("() => wsTotals(S.result).missing"), missing_before)
                blank = page.evaluate("() => wsDoc().worksheet.lines.find(l => l.row_id === 'r3')")
                self.assertIsNone(blank["your_unit"])
                self.assertIn("engineer input", blank["judgment"])
                self.assertEqual(blank["source"], "Engineer input")

                page.fill(price, "123.45")
                page.press(price, "Tab")
                page.wait_for_function("() => S.ws.lines.r3.your_unit === 123.45")
                dialogs = []
                page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
                for basis in ("p50", "p70"):
                    page.click('#r-headline .bigcard[data-basis="%s"]' % basis)
                    self.assertEqual(page.evaluate("() => S.ws.basis"), basis)
                    self.assertEqual(page.input_value(price), "123.45")
                self.assertEqual(dialogs, [])
                for rounding in ("nearest", "off", "up"):
                    page.select_option("#ws-round", rounding)
                    page.wait_for_timeout(300)
                    self.assertEqual(page.input_value(price), "123.45")
                with page.expect_response(lambda r: r.url.endswith("/api/estimate") and r.request.method == "POST") as estimated:
                    page.click("#btn-estimate")
                estimated.value.finished()
                page.wait_for_function("() => !document.querySelector('#btn-estimate').disabled && S.ws.refs", timeout=60000)
                self.assertEqual(page.input_value(price), "123.45")
                doc = page.evaluate("() => wsDoc()")
                requested = next(l for l in doc["request"]["items"] if l["row_id"] == "r3")
                entered = next(l for l in doc["worksheet"]["lines"] if l["row_id"] == "r3")
                self.assertEqual(requested["pricing_mode"], "engineer")
                self.assertNotIn("your_unit", requested)
                self.assertEqual(entered["your_unit"], 123.45)
                self.assertEqual(entered["src"], "you")
                self.assertIn("Engineer", entered["source"])
                with page.expect_download() as downloaded:
                    page.click("#btn-export-xlsx")
                opcc = read_xlsx(downloaded.value.path())["OPCC"]
                row = next(int(cell[1:]) for cell, value in opcc.items()
                           if cell.startswith("B") and value == original["description"])
                self.assertAlmostEqual(opcc["E%d" % row], 123.45, places=4)
                self.assertAlmostEqual(opcc["F%d" % row], 123.45 * original["quantity"], places=2)
                self.assertEqual(opcc["G%d" % row], entered["source"])

                # Returning to our reading removes the explicit mode, while
                # keeping the price the engineer already entered.
                takeoff_row.get_by_role("button", name="Change", exact=True).click()
                picker = page.locator("#modal-host .modal")
                self.assertEqual(picker.locator("select").input_value(), "__new_item__")
                picker.locator("select").select_option("")
                picker.get_by_role("button", name="Apply", exact=True).click()
                page.wait_for_function("() => !S.lines.find(l => l.row_id === 'r3').pricing_mode && S.stale")
                page.click("#btn-reestimate")
                page.wait_for_function("() => !S.stale && !S.result.lines.find(l => l.row_id === 'r3').pricing_mode", timeout=60000)
                self.assertFalse(page.evaluate("() => engineerInput(S.result.lines.find(l => l.row_id === 'r3'))"))
                self.assertEqual(page.input_value(price), "123.45")
                page.fill(price, "")
                page.press(price, "Tab")
                page.wait_for_function("() => S.ws.refsReq === S.resultReq")
                # Clearing the manual price restores the selected P70 model rate.
                page.wait_for_function("() => S.ws.basis === 'p70' && S.ws.lines.r3.src === 'model'")
                expected_price = page.evaluate("""() => {
                    const line = S.result.lines.find(l => l.row_id === 'r3');
                    return priceText(nicePrice(line.unit_price_p50 * S.result.headline.p70_multiplier, 'up'));
                }""")
                self.assertEqual(page.input_value(price), expected_price)
                self.assertEqual(errors, [])

    def test_unmatched_item_is_manual_only_after_explicit_choice(self):
        """Automatic unclassified lines retain their existing behavior;
        the inline New item action makes the engineer-input choice explicit."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine,
                    extra=["199\tUnobtainium Lattice Deflector\tEA\t3"])
                line = page.evaluate("() => S.result.lines.find(l => l.item_no === '199')")
                key = line["row_id"]
                self.assertIn(line["family"], ("other", "needs_review", ""))
                self.assertFalse(line.get("pricing_mode"))
                self.assertFalse(page.evaluate("k => engineerInput(S.result.lines.find(l => l.row_id === k))", key))
                action = page.locator('#review-body tr[data-row="%s"] .suggest [data-action="new-item"]' % key)
                self.assertTrue(action.is_visible())
                self.assertIn("New item / Other", action.inner_text())
                action.click()
                page.wait_for_function("k => S.lines.find(l => l.row_id === k).pricing_mode === 'engineer' && S.stale", arg=key)
                page.click("#btn-reestimate")
                page.wait_for_function("k => !S.stale && S.result.lines.find(l => l.row_id === k).pricing_mode === 'engineer'", arg=key, timeout=60000)
                price = '#result-body tr[data-line="%s"] input.yourp' % key
                self.assertEqual(page.input_value(price), "")
                self.assertEqual(page.get_attribute(price, "placeholder").lower(), "engineer input")
                self.assertTrue(page.evaluate("k => engineerInput(S.result.lines.find(l => l.row_id === k))", key))
                self.assertEqual(errors, [])

    def test_new_item_chosen_during_estimate_discards_the_old_response(self):
        """A late response cannot hide a newer classification change or
        replace the worksheet before the latest inputs are estimated."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                before = page.evaluate("() => ({result: S.result, worksheet: S.ws})")
                price = '#result-body tr[data-line="r3"] input.yourp'
                previous_price = page.input_value(price)
                held = []
                page.route("**/api/estimate", lambda route: held.append(route), times=1)
                with page.expect_request(lambda r: r.url.endswith("/api/estimate") and r.method == "POST"):
                    page.click("#btn-estimate")
                # Only the request is paused; the user can still change a row.
                page.locator('#review-body tr[data-row="r3"]').get_by_role(
                    "button", name="Change", exact=True).click()
                picker = page.locator("#modal-host .modal")
                picker.locator("select").select_option("__new_item__")
                picker.get_by_role("button", name="Apply", exact=True).click()
                page.wait_for_function("() => S.lines.find(l => l.row_id === 'r3').pricing_mode === 'engineer' && S.stale")
                self.assertEqual(len(held), 1)
                with page.expect_response(lambda r: r.url.endswith("/api/estimate") and r.request.method == "POST") as outdated:
                    held[0].continue_()
                outdated.value.finished()
                page.get_by_text("Inputs changed while estimating. Click Estimate again to use the latest items.",
                                 exact=True).wait_for(state="visible")
                self.assertTrue(page.is_visible("#stale-banner"))
                self.assertTrue(page.evaluate("() => S.stale"))
                self.assertEqual(page.evaluate("() => ({result: S.result, worksheet: S.ws})"), before)
                self.assertEqual(page.input_value(price), previous_price)
                self.assertFalse(page.evaluate("() => S.result.lines.find(l => l.row_id === 'r3').pricing_mode"))

                page.click("#btn-reestimate")
                page.wait_for_function("() => !S.stale && S.result.lines.find(l => l.row_id === 'r3').pricing_mode === 'engineer'", timeout=60000)
                self.assertEqual(page.input_value(price), "")
                self.assertEqual(page.get_attribute(price, "placeholder").lower(), "engineer input")
                self.assertFalse(page.is_visible("#stale-banner"))
                self.assertEqual(errors, [])

    def test_the_takeoff_template_downloads_from_the_first_step(self):
        """2026-11-21 (user): the takeoff step starts with a blank Excel
        template to download, fill in and paste back."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                browser = getattr(self.pw, engine).launch()
                self.addCleanup(browser.close)
                page = browser.new_page(accept_downloads=True)
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.url + "/")
                link = page.locator("#sec-input #template-bar a#btn-template")
                self.assertTrue(link.is_visible())
                self.assertIn("template", link.inner_text().lower())
                with page.expect_download() as dl:
                    link.click()
                d = dl.value
                self.assertEqual(d.suggested_filename, "takeoff_template.xlsx")
                with open(d.path(), "rb") as fh:
                    self.assertEqual(fh.read(2), b"PK")      # an .xlsx (zip)
                self.assertEqual(errors, [])

    def test_a_long_description_wraps_in_the_takeoff_grid(self):
        """2026-11-21 (user): "a long description is not wrapped, it is all
        written in one line -- you must wrap". The description box grows
        with its text, nothing is cut off, and Enter still moves on."""
        long_ = ("Mobilization/Site Preparation, Clearing and Grubbing (Including Tree Removal "
                 "(all sizes), Riprap, Fencing, Trash and other Deleterious Material, and "
                 "Temporary Construction Fence), in accordance with the plans and specifications")
        for engine in self.engines:
            with self.subTest(engine=engine):
                browser = getattr(self.pw, engine).launch()
                self.addCleanup(browser.close)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.url + "/")
                page.wait_for_function(SAMPLE_READY, timeout=60000)
                page.fill("#paste-text", "100\t%s\tLS\t1\n101\tTrench Safety System\tLF\t1450" % long_)
                page.click("#btn-parse-text")
                page.wait_for_function("() => S.lines.length === 2 && S.lines.every(l => l.cls)")
                box = page.locator('tr[data-row="r1"] [data-col="description"]')
                self.assertEqual(box.evaluate("e => e.tagName"), "TEXTAREA")
                self.assertEqual(box.input_value(), long_)
                fit = box.evaluate("e => [e.scrollHeight <= e.clientHeight + 2, e.scrollWidth <= e.clientWidth + 2,"
                                   " e.getBoundingClientRect().height]")
                self.assertTrue(fit[0] and fit[1], fit)
                self.assertGreater(fit[2], 40)                      # more than one line tall
                short = page.locator('tr[data-row="r2"] [data-col="description"]')
                self.assertLess(short.evaluate("e => e.getBoundingClientRect().height"), 40)
                box.click()
                page.keyboard.press("Enter")                       # moves on; no line break typed
                self.assertEqual(page.evaluate("() => document.activeElement.dataset.i"), "1")
                self.assertNotIn("\n", page.evaluate("() => S.lines[0].description"))
                self.assertEqual(errors, [])

    def test_an_allowance_alert_follows_the_amount_filled_in(self):
        """2026-11-21 (user): "it flagged one owner-set allowance with no
        amount; I filled it in and it is still there". The red alert above
        the estimate drops as soon as the amount is in, a note asks for
        Re-estimate, and after Re-estimate nothing is left."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                browser = getattr(self.pw, engine).launch()
                self.addCleanup(browser.close)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.url + "/")
                page.wait_for_function(SAMPLE_READY, timeout=60000)
                page.fill("#paste-text", "1\tMobilization\tLS\t1\n2\tIrrigation Repair Allowance\tLS\t1\n"
                                         "3\tGrouted Rock Riprap, 18 inch\tSY\t900")
                page.click("#btn-parse-text")
                page.wait_for_function("() => S.lines.length === 3 && S.lines.every(l => l.cls)")
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws && S.ws.refs", timeout=60000)
                top = page.locator("#r-top-alerts")
                self.assertIn("no amount", top.inner_text())
                inp = page.locator("#ownerset-box input.num").first
                inp.fill("25000")
                inp.dispatch_event("change")
                page.wait_for_timeout(500)
                self.assertNotIn("no amount", top.inner_text())
                self.assertIn("Re-estimate", top.inner_text())
                self.assertEqual(page.locator("#result-body tr.st-amount-missing").count(), 0)
                page.click("#btn-reestimate")
                page.wait_for_function("() => S.result && !S.stale", timeout=60000)
                page.wait_for_timeout(300)
                self.assertNotIn("no amount", page.locator("#r-top-alerts").inner_text())
                self.assertEqual(errors, [])

    def test_ai_mode_is_a_place_kept_in_the_upload_step(self):
        """2026-11-21 (user): an "AI mode" button where files are uploaded:
        drawings and documents go in through the one drop area, then "Read by
        AI" lists the work and the quantities; it replaces "Read a PDF with
        Claude". Not built yet: the window is kept, nothing is sent."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                browser = getattr(self.pw, engine).launch()
                self.addCleanup(browser.close)
                page = browser.new_page()
                errors, posts = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
                page.goto(self.url + "/")
                page.click('#input-tabs button[data-pane="file"]')
                btn = page.locator("#btn-ai-mode")
                self.assertTrue(btn.is_visible())
                self.assertEqual(btn.inner_text().strip(), "AI mode")
                self.assertFalse(page.locator("#ai-mode-panel").is_visible())
                btn.click()
                panel = page.locator("#ai-mode-panel")
                self.assertTrue(panel.is_visible())
                self.assertEqual(btn.get_attribute("aria-expanded"), "true")
                self.assertIn("not available yet", panel.inner_text().lower())
                self.assertTrue(page.locator("#btn-ai-read").is_disabled())
                # one way in for files: the drop area; the old Claude PDF
                # button is folded into AI mode (user, 2026-11-21)
                self.assertEqual(page.locator("#pane-file .dropzone").count(), 1)
                self.assertEqual(page.locator("#btn-claude").count(), 0)
                self.assertNotIn("with claude", page.locator("#pane-file").inner_text().lower())
                btn.click()
                self.assertFalse(panel.is_visible())
                self.assertEqual(posts, [])
                self.assertEqual(errors, [])

    def test_a_contingency_line_is_a_share_picked_from_a_list(self):
        """2026-11-21 (user): a contingency is a percentage picked from a
        list. The line has no item type and no Change button; in the
        worksheet it is that share of your other line amounts, with no
        references; the worksheet's own contingency is a list too."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.takeoff_page(engine, extra=["110\tOwner's Contingency Allowance\tLS\t1"])
                self.assertEqual(page.input_value("#in-contingency"), "0")
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws.refsReq === S.resultReq", timeout=90000)
                k = page.evaluate("() => S.lines.find(l => /contingency/i.test(l.description)).row_id")
                row = 'tr[data-row="%s"]' % k
                self.assertEqual(page.locator(row).get_by_role("button", name="Change", exact=True).count(), 0)
                self.assertEqual(page.inner_text(row + " .clscell").strip(), "Contingency")
                self.assertEqual(page.input_value(row + " select.cpct"), "10")
                # the worksheet: the items, then SUBTOTAL, the contingency
                # below it (that share of your subtotal, nothing to open),
                # then TOTAL (user, 2026-11-21)
                wrow = 'tr[data-line="%s"]' % k
                self.assertEqual(page.locator("#result-body " + wrow).count(), 0)
                foot = page.eval_on_selector_all("#result-foot tr", "t => t.map(r => r.className)")
                self.assertEqual([c.split()[0] for c in foot], ["wssub", "controw", "wscont", "wstotal"])
                self.assertEqual(page.locator(wrow + " .chev").count(), 0)
                self.assertEqual(page.locator(wrow + " select.pick").count(), 0)
                share = page.evaluate("k => { const l = S.result.lines.find(x => x.row_id === k);"
                                      " return wsAmount(l, wsLine(l)) / wsSubtotal(); }", k)
                self.assertAlmostEqual(share, 0.10, places=6)
                t = page.evaluate("() => { const t = wsTotals(S.result); return [t.sub, t.below, t.yours]; }")
                self.assertAlmostEqual(t[2], t[0] * 1.10, places=4)
                self.assert_project_total(page, t[0] * 1.10)
                # The worksheet carries the breakdown; the headline only shows totals.
                self.assertEqual(page.locator("#r-headline .bigcard .csplit").count(), 0)
                self.assertIn("10% of the subtotal", page.inner_text("#result-foot tr.controw"))
                for row_class, amount in (("wssub", t[0]), ("controw", t[1]), ("wstotal", t[2])):
                    shown = page.inner_text("#result-foot tr.%s td.money.p50" % row_class)
                    self.assertAlmostEqual(float(shown.replace("$", "").replace(",", "")), amount, delta=0.5)
                # the schedule carries a contingency: the worksheet's own one
                # starts at 0%, picked from a list
                sel = "#in-contingency"
                self.assertEqual(page.input_value(sel), "0")
                page.select_option(sel, "15")
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate("() => S.ws.contingency"), 15)
                self.assert_project_total(page, t[0] * 1.25)
                # 15% on the line goes into the next estimate as a share
                page.select_option(row + " select.cpct", "15")
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate(
                    "() => estimateRequest(5).items.find(i => /contingency/i.test(i.description)).amount_pct"), 15)
                # or the owner's fixed amount, typed in the box that appears
                page.select_option(row + " select.cpct", "fixed")
                page.wait_for_selector(row + " .cpctcell input")
                page.fill(row + " .cpctcell input", "50,000")
                page.press(row + " .cpctcell input", "Tab")
                page.wait_for_timeout(300)
                it = page.evaluate("() => estimateRequest(5).items.find(i => /contingency/i.test(i.description))")
                self.assertEqual(it["amount"], 50000)
                self.assertIsNone(it["amount_pct"])
                page.click("#btn-estimate")
                page.wait_for_function("""() => S.result.lines.some(l =>
                    l.status === 'owner_set' && l.fixed_amount === 50000)""")
                for basis in ("p50", "p70"):
                    page.click('#r-headline button[data-basis="%s"]' % basis)
                    self.assertEqual(page.evaluate("""() => wsDoc().worksheet.lines
                        .find(l => l.row_id === 'r10').your_amount"""), 50000)
                self.assertEqual(errors, [])

    def test_the_keyboard_toggles_a_focused_line(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                page.focus('tr[data-line="r5"]')
                page.keyboard.press("Enter")
                self.assertEqual(self.expanded(page, "r5"), "true")
                page.keyboard.press(" ")
                self.assertEqual(self.expanded(page, "r5"), "false")

    def test_a_card_header_opens_the_whole_bid(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                card = "#r-similar .simcard >> nth=0"
                page.click(card + " >> .simhead")
                page.wait_for_selector("#sim-detail table.btgrid")
                self.assertEqual(page.get_attribute(card + " >> .simhead", "aria-expanded"), "true")
                self.assertGreater(page.locator("#sim-detail tr.hl").count(), 0)
                self.assertEqual(page.inner_text(card + " >> .simfoot button"), "Hide full bid")
                page.click(card + " >> .simhead")
                self.assertEqual(page.locator("#sim-detail table").count(), 0)

    def paste_page(self, engine, text, width=1280, height=900):
        browser = getattr(self.pw, engine).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url + "/")
        page.wait_for_function(SAMPLE_READY, timeout=60000)
        page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
        page.fill("#paste-text", text)
        page.click("#btn-parse-text")
        page.wait_for_function("() => S.lines.length && S.lines.every(l => l.cls)")
        return page, errors

    def grid_rows(self, page):
        return page.eval_on_selector_all("#review-body tr", """trs => trs.map(tr => [
            tr.cells.length,
            ...['item_no', 'description', 'unit', 'quantity'].map(c => {
              const i = tr.querySelector('[data-col="' + c + '"]');
              return i ? i.value + (i.classList.contains('flag') ? '!' : '')
                + (i.classList.contains('assumed') ? '~' : '') : null;
            })])""")

    def test_a_messy_paste_keeps_every_cell_in_its_column(self):
        """The user's paste of 2026-11-20: cells missing, a fragment, a
        trailing space -- and at phone width the page never scrolls
        sideways."""
        with open(os.path.join(HERE, "fixtures", "messy_paste_2026-11-20.txt"),
                  encoding="utf-8") as fh:
            text = fh.read()
        want = [[8, "101", "Mobilization", "LS~", "1~"],
                [8, "102", "Temporary Erosion Control", "LS~", "1"],
                [8, "103", "Unclassified", "CY", "4,200"],
                [8, "104", "Grouted Rock Riprap, 18 inch", "SY", "2,600"],
                [8, "105", "Gabion Mattress, 12 inch", "SY", "880"],
                [8, "106", "8 inch PVC SDR-26 Sanitary Sewer Pipe", "LF", "1,450"],
                [8, "107", "Block Sod, Bermuda", "SY", "9,500"],
                [8, "108", "Trench Safety System", "LF", "1,450"],
                [8, "109", "Submersible Pump, 15 HP", "EA", "2"],
                [8, "110", "Owner's Contingency Allowance", "LS", "1"]]
        for engine in self.engines:
            for width, height in ((1280, 900), (375, 667)):
                with self.subTest(engine=engine, width=width):
                    page, errors = self.paste_page(engine, text, width, height)
                    self.assertEqual(self.grid_rows(page), want)
                    cls = page.inner_text('tr[data-row="r3"] .clscell')
                    self.assertIn("unclear description", cls)
                    # history suggestions for the fragment: "... Excavation"
                    self.assertIn("EXCAVATION", page.inner_text(
                        'tr[data-row="r3"] .suggest').upper())
                    self.assertIn("assumed", page.inner_text('tr[data-row="r1"] .clscell').lower())
                    sw = page.evaluate("() => [document.documentElement.scrollWidth, "
                                       "document.documentElement.clientWidth]")
                    self.assertLessEqual(sw[0], sw[1])
                    self.assertEqual(errors, [])

    def test_a_line_without_a_quantity_is_left_out_and_said_so(self):
        text = "101\tMobilization\tLS\t\n102\tRock riprap\tSY\t1,745\n"
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.paste_page(engine, text)
                q = page.get_attribute('tr[data-row="r1"] input[data-col="quantity"]', "placeholder")
                self.assertEqual(q, "needs input")
                self.assertIn("left out of the estimate", page.inner_text("#estimate-hint"))
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result")
                sent = page.evaluate("() => S.resultReq.items.map(i => i.item_no)")
                self.assertEqual(sent, ["102"])
                self.assertIn("101 Mobilization", page.inner_text("#r-warnings"))

    def test_the_status_bar_is_plain_and_about_has_the_details(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                bar = page.inner_text("#status-items")
                self.assertTrue(bar.startswith("Prices from public bids"), bar)
                for word in ("md5", "estimate_low_bid", "Parser", "OCR", "MB"):
                    self.assertNotIn(word, bar)
                page.click("#btn-about")
                about = page.inner_text("#modal-host .modal")
                for word in ("md5", "Parser", "OCR", "Loaded"):
                    self.assertIn(word, about)

    # -- reordering by dragging, adding rows (user requests, 2026-11-20) ------
    def order(self, page):
        return page.evaluate("() => S.lines.map(l => l.row_id)")

    def drag(self, page, src_sel, dst_y, steps=12, release=True):
        """Press on a non-input part of a row and drag it to page y."""
        box = page.locator(src_sel).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        for k in range(1, steps + 1):
            page.mouse.move(x, y + (dst_y - y) * k / steps)
            page.wait_for_timeout(16)
        if release:
            page.mouse.up()
            page.wait_for_timeout(350)

    def test_the_insert_function(self):
        page, errors = self.estimate_page(self.engines[0])
        got = page.evaluate("() => moveTo([1,2,3,4,5,6,7,8,9,10], 9, 2)")
        self.assertEqual(got, [1, 2, 10, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(page.evaluate("() => moveTo([1,2,3,4], 0, 4)"), [2, 3, 4, 1])
        self.assertEqual(page.evaluate("() => moveTo([1,2,3,4], 2, 2)"), [1, 2, 3, 4])

    def test_dragging_a_row_inserts_it_and_the_others_slide(self):
        for engine in [e for e in self.engines if e in ("chromium", "webkit")] or self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine, 1280, 1600)
                page.locator("#review-table").scroll_into_view_if_needed()
                self.assertEqual(page.locator(".handle, .drop-before, .drop-after, .insert-line").count(), 0)
                before = self.order(page)
                # takeoff grid: the last line, by its classification cell, up
                # to just under the second line
                r2 = page.locator('tr[data-row="r2"]').bounding_box()
                self.drag(page, 'tr[data-row="r9"] .clscell', r2["y"] + r2["height"] * 0.75,
                          release=False)
                # live: the rows it passed have slid down already
                moved = page.evaluate("() => ['r3', 'r8'].map(k => document.querySelector("
                                      "'tr[data-row=\"' + k + '\"]').style.transform)")
                self.assertTrue(all("translateY" in m for m in moved), moved)
                page.mouse.up()
                page.wait_for_timeout(400)
                want = before[:2] + [before[-1]] + before[2:-1]
                self.assertEqual(self.order(page), want)
                dom = page.eval_on_selector_all("#review-body tr[data-row]", "t => t.map(r => r.dataset.row)")
                self.assertEqual(dom, want)
                # the estimate's line table follows without pricing again
                ws = page.eval_on_selector_all("#result-body tr[data-line]", "t => t.map(r => r.dataset.line)")
                self.assertEqual(ws, [k for k in want if k in ws])
                self.assertFalse(page.evaluate("() => S.stale"))
                self.assertEqual(errors, [])

    def test_click_toggles_drag_moves_and_fields_do_neither(self):
        for engine in [e for e in self.engines if e in ("chromium", "webkit")] or self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine, 1280, 1600)
                page.evaluate("() => document.querySelector('tr[data-line=\"r4\"]').scrollIntoView({block: 'center', behavior: 'instant'})")
                ws_before = page.eval_on_selector_all("#result-body tr[data-line]", "t => t.map(r => r.dataset.line)")
                # a press on a field, then a pointer move: no drag, no toggle
                inp = page.locator('tr[data-line="r4"] input.note').bounding_box()
                page.mouse.move(inp["x"] + 10, inp["y"] + 5)
                page.mouse.down()
                page.mouse.move(inp["x"] + 10, inp["y"] + 80, steps=8)
                page.mouse.up()
                self.assertEqual(self.expanded(page, "r4"), "false")
                self.assertEqual(page.eval_on_selector_all(
                    "#result-body tr[data-line]", "t => t.map(r => r.dataset.line)"), ws_before)
                # a drag of more than 6 px on the worksheet: moves, no toggle
                r6 = page.locator('tr[data-line="r6"]').bounding_box()
                self.drag(page, 'tr[data-line="r2"] td:nth-child(3)', r6["y"] + r6["height"] * 0.8)
                ws_after = page.eval_on_selector_all("#result-body tr[data-line]", "t => t.map(r => r.dataset.line)")
                self.assertNotEqual(ws_after, ws_before)
                self.assertEqual(sorted(ws_after), sorted(ws_before))
                self.assertEqual(self.expanded(page, "r2"), "false")
                # the takeoff grid has the same order
                self.assertEqual([k for k in self.order(page) if k in ws_after], ws_after)
                # a click (no movement) still toggles
                page.click('tr[data-line="r2"] td:nth-child(3)')
                self.assertEqual(self.expanded(page, "r2"), "true")

    def test_escape_cancels_a_drag(self):
        page, errors = self.estimate_page(self.engines[0], 1280, 1600)
        page.locator("#review-table").scroll_into_view_if_needed()
        before = self.order(page)
        r1 = page.locator('tr[data-row="r1"]').bounding_box()
        self.drag(page, 'tr[data-row="r8"] .clscell', r1["y"] + 5, release=False)
        page.keyboard.press("Escape")
        page.mouse.up()
        page.wait_for_timeout(400)
        self.assertEqual(self.order(page), before)

    def test_alt_arrows_move_a_line(self):
        page, errors = self.estimate_page(self.engines[0])
        before = self.order(page)
        page.focus('tr[data-row="r3"] [data-col="description"]')
        page.keyboard.press("Alt+ArrowUp")
        self.assertEqual(self.order(page), [before[0], before[2], before[1]] + before[3:])
        self.assertIn("moved to position 2", page.inner_text("#sr-live") or
                      page.evaluate("() => new Promise(r => setTimeout(() => r(document.querySelector('#sr-live').textContent), 80))"))

    def test_a_long_press_drags_on_a_phone_and_a_swipe_does_not(self):
        for engine in [e for e in self.engines if e in ("chromium", "webkit")] or self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine, 390, 844)
                before = self.order(page)
                js = """async ([key, dy, hold]) => {
                  const cell = document.querySelector('tr[data-row="' + key + '"] .clscell');
                  const r = cell.getBoundingClientRect();
                  const x = r.left + 20, y = r.top + r.height / 2;
                  const ev = (t, yy) => new PointerEvent(t, {pointerId: 7, pointerType: 'touch',
                    isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: yy, button: 0});
                  cell.dispatchEvent(ev('pointerdown', y));
                  await new Promise(res => setTimeout(res, hold));
                  for (let k = 1; k <= 10; k++) {
                    window.dispatchEvent(ev('pointermove', y + dy * k / 10));
                    await new Promise(res => requestAnimationFrame(() => res()));
                  }
                  window.dispatchEvent(ev('pointerup', y + dy));
                  await new Promise(res => setTimeout(res, 400));
                  return S.lines.map(l => l.row_id);
                }"""
                page.evaluate("() => document.querySelector('tr[data-row=\"r3\"]').scrollIntoView({block: 'center', behavior: 'instant'})")
                h = page.locator('tr[data-row="r1"]').bounding_box()["height"]
                # a quick swipe: no drag
                self.assertEqual(page.evaluate(js, ["r1", 3 * h, 50]), before)
                # a long press, then down past three rows: inserted there
                got = page.evaluate(js, ["r1", 3.6 * h, 450])
                self.assertEqual(got[0], before[1])
                self.assertEqual(sorted(got), sorted(before))
                self.assertNotEqual(got, before)

    def test_add_row_sits_under_the_last_row(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine, 375, 667)
                follows = page.evaluate("() => document.querySelector('#grid-wrap').compareDocumentPosition("
                                        "document.querySelector('#btn-add-row')) & Node.DOCUMENT_POSITION_FOLLOWING")
                self.assertTrue(follows)
                self.assertEqual(page.locator("#grid-head #btn-add-row").count(), 0)
                h = page.locator("#btn-add-row").bounding_box()["height"]
                self.assertGreaterEqual(h, 36)
                n = page.evaluate("() => S.lines.length")
                page.click("#btn-add-row")
                self.assertEqual(page.evaluate("() => S.lines.length"), n + 1)
                last = page.eval_on_selector_all("#review-body tr[data-row]", "t => t[t.length - 1].dataset.row")
                self.assertEqual(last, page.evaluate("() => S.lines[S.lines.length - 1].row_id"))
                self.assertEqual(page.evaluate("() => document.activeElement.dataset.col"), "description")
                page.keyboard.type("Silt fence")
                page.keyboard.press("Enter")          # Enter on the last row adds one more
                self.assertEqual(page.evaluate("() => S.lines.length"), n + 2)

    # -- anything pasted: read it, or say so; the page holds (2026-11-20) ------
    NASTY = [
        "<script>alert(1)</script>\tLS\t1",
        "<img src=x onerror=alert(1)>\tSY\t10",
        '"><b>bold</b>\tEA\t2',
        "A" * 3000 + "\tSY\t5",
        "Rock riprap " * 400,
        "\U0001F477 Trench safety \U0001F4CF\tLF\t1,450",
        "مرحبا שלום\tSY\t12",
        "‮LS‬ Mobilization\t‮LS\t1",
        "\x00\x01\x02 control\tLS\t1",
        "\t\t\t\t\t\t\t\t",
        ",,,,,,,,,,,,,,",
        "1\n2\n3\n4\n5",
        '101,"Rock riprap, 18 inch,SY,1,200\n102,Block sod",SY,"9,500',
        "\n".join("x" for _ in range(1500)),
        "101\tMobilization\t\n102\t\t\t\n103\tUnclassified \tCY\t4,200",
        "Mobilization LS 1\n" + "Item Description Unit Qty\n" * 5,
        "TOTAL BID $1,234,567.89\nSUBTOTAL\nGRAND TOTAL",
        "101  " + "Supercalifragilisticexpialidocious" * 20 + "  SY  100",
        "#$%^&*()_+{}|:<>?~`-=[]\\;',./",
        "101\tRock riprap\tSY\t1e309",
    ]

    def test_nasty_pastes_keep_the_page_whole(self):
        engines = [e for e in self.engines if e in ("chromium", "webkit")] or self.engines
        for engine in engines:
            for width, height in ((1280, 900), (375, 667)):
                browser = getattr(self.pw, engine).launch()
                try:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    errors, dialogs = [], []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
                    page.goto(self.url + "/")
                    page.wait_for_function(SAMPLE_READY, timeout=60000)
                    for k, text in enumerate(self.NASTY):
                        with self.subTest(engine=engine, width=width, case=k):
                            page.evaluate("() => { S.parse = null; }")
                            page.fill("#paste-text", text)
                            page.click("#btn-parse-text")
                            page.wait_for_function(
                                "() => S.parse && (S.parse.unreadable || (S.lines.length && S.lines.every(l => l.cls)))",
                                timeout=20000)
                            if page.evaluate("() => !!S.parse.unreadable"):
                                self.assertIn("Couldn’t read this", page.inner_text("#parse-notes"))
                            else:
                                cells = page.eval_on_selector_all("#review-body tr", "t => t.map(r => r.cells.length)")
                                self.assertTrue(cells and set(cells) == {8}, cells)
                                page.wait_for_timeout(400)      # classification settles
                                if page.evaluate("() => !document.querySelector('#btn-estimate').disabled"):
                                    page.evaluate("() => document.querySelector('#btn-estimate').click()")
                                    page.wait_for_function("() => S.result || document.querySelector('.toast.err')",
                                                           timeout=20000)
                            sw = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
                            self.assertLessEqual(sw[0], sw[1] + 1, (k, sw))
                            self.assertEqual(page.locator("#review-body img, #r-headline img, #result-body img").count(), 0)
                            self.assertEqual(page.locator("#review-body b, #result-body b").count(), 0)
                    self.assertEqual(dialogs, [])
                    self.assertEqual([e for e in errors if "favicon" not in e], [])
                finally:
                    browser.close()

    def test_garbage_is_listed_as_not_read_and_never_widens_the_page(self):
        """Coordinator, 2026-11-20 ("what about garbled input?"): binary-ish garbage is "Couldn't
        read" with its lines listed; a real line with zero-width characters and
        an RTL override is read clean; nothing widens the page at 360 / 768 /
        1440 px and nothing on the page can reorder text."""
        import random
        rng = random.Random(1)
        binaryish = "".join(chr(rng.randint(1, 255)) for _ in range(600))
        rtl = ("101​ Mobili‍zation‮ LS 1\n"
               "‮ç¬¬ä¸€è¡Œ 12 LF 3.5\n"
               "asdkj@#$%^&*()_+ ~~ ||||" + "x" * 400 + "\n")
        engines = [e for e in self.engines if e in ("chromium", "webkit")] or self.engines
        for engine in engines:
            browser = getattr(self.pw, engine).launch()
            try:
                for width in (360, 768, 1440):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(self.url + "/")
                    page.wait_for_function(SAMPLE_READY, timeout=60000)
                    page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
                    for label, text in (("binary-ish", binaryish), ("rtl", rtl)):
                        with self.subTest(engine=engine, width=width, case=label):
                            page.evaluate("() => { S.parse = null; }")
                            page.evaluate("t => { document.querySelector('#paste-text').value = t; }", text)
                            page.click("#btn-parse-text")
                            page.wait_for_function(
                                "() => S.parse && (S.parse.unreadable || (S.lines.length && S.lines.every(l => l.cls)))",
                                timeout=20000)
                            notes = page.inner_text("#parse-notes")
                            self.assertIn("could not be read", notes)
                            if label == "binary-ish":
                                self.assertIn("Couldn’t read this", notes)
                                self.assertIn("Couldn't read any line items from this.", notes)
                                self.assertEqual(page.evaluate("() => S.lines.length"), 0)
                            else:
                                self.assertEqual(page.evaluate(
                                    "() => S.lines.map(l => [l.item_no, l.description, l.unit, l.quantity])"),
                                    [["101", "Mobilization", "LS", 1]])
                                self.assertIn("2 lines could not be read", notes)
                            page.click("#unread-note > summary")         # open the list
                            items = page.eval_on_selector_all("#unread-note li .rawtext",
                                                              "els => els.map(e => e.textContent)")
                            self.assertTrue(items)
                            self.assertTrue(all(len(t) <= 120 for t in items), items)
                            page_text = page.evaluate("() => document.body.innerText + [...document.querySelectorAll('input')].map(i => i.value).join(' ')")
                            for ch in "‪‫‭‮⁦⁧⁨​‍":
                                self.assertNotIn(ch, page_text)
                            sw = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
                            self.assertLessEqual(sw[0], sw[1] + 1, (engine, width, label, sw))
                            box = page.evaluate("() => document.querySelector('#unread-note').getBoundingClientRect().right")
                            self.assertLessEqual(box, width + 1)
                    self.assertEqual(errors, [])
                    page.close()
            finally:
                browser.close()

    def test_unit_hint_and_printed_cap_picks_leave_the_estimate_alone(self):
        """Batch 3: the CF demo gets "Converted from CY bids" quick picks and
        a "5% Max" mobilization line gets "Printed cap"; picking them changes
        your OPCC, never P50 / P80 (the same request through the API)."""
        demo = "\n".join(["101\tMobilization (5% Max)\tLS\t1", "102\tExcavation\tCF\t113,400",
                          "103\tEmbankment\tCF\t48,600", "104\tConcrete Sidewalk, 4 inch\tSF\t12,000",
                          "105\tBlock Sod\tSF\t30,000", "106\tReinforced Concrete Pipe, 24 inch\tLF\t800"])
        for engine in self.engines:
            with self.subTest(engine=engine):
                browser = getattr(self.pw, engine).launch()
                self.addCleanup(browser.close)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(self.url + "/")
                page.wait_for_function(SAMPLE_READY, timeout=60000)
                page.evaluate("t => { document.querySelector('#paste-text').value = t; }", demo)
                page.click("#btn-parse-text")
                page.wait_for_function("() => S.lines.length === 6 && S.lines.every(l => l.cls)", timeout=30000)
                page.wait_for_function("() => S.lines.filter(l => l.cls.unit_hint).length === 2", timeout=30000)
                self.assertIn("unit: few bids", page.inner_text("#review-body").lower())
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws.refs", timeout=30000)
                page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
                p50, p70, p80 = page.evaluate("() => [S.result.headline.p50, S.result.headline.p70, S.result.headline.p80]")
                opts = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#result-body tr.clickrow')]
                    .map(tr => [S.lines.find(l => l.row_id === tr.dataset.line).item_no,
                                [...(tr.querySelector('select.pick') || {options: []}).options].map(o => o.text)]))""")
                self.assertTrue(any(o.startswith("Converted from CY bids: $") and "/CF" in o for o in opts["102"]), opts["102"])
                self.assertTrue(any(o.startswith("Converted from CY bids: $") for o in opts["103"]), opts["103"])
                self.assertTrue(any(o.startswith("Printed cap: 5% of the total bid = $") for o in opts["101"]), opts["101"])
                for k in ("104", "105", "106"):
                    self.assertFalse(any(o.startswith(("Converted", "Printed cap")) for o in opts[k]), k)
                self.assertIn("Bidders usually price it at or just under the printed cap.",
                              page.inner_text("#result-body"))
                # pick both: your OPCC moves, the estimate does not
                k101 = page.evaluate("() => S.lines.find(l => l.item_no === '101').row_id")
                k103 = page.evaluate("() => S.lines.find(l => l.item_no === '103').row_id")
                page.select_option('tr[data-line="%s"] select.pick' % k103, "unit")
                page.wait_for_timeout(300)
                page.select_option('tr[data-line="%s"] select.pick' % k101, "cap")
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate("() => [S.result.headline.p50, S.result.headline.p70, S.result.headline.p80]"), [p50, p70, p80])
                api = page.evaluate("""async () => { const r = await fetch('/api/estimate', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(S.resultReq)});
                    const j = await r.json(); return [j.headline.p50, j.headline.p70, j.headline.p80]; }""")
                self.assertEqual(api, [p50, p70, p80])
                your103 = float(page.input_value('tr[data-line="%s"] input.yourp' % k103))
                self.assertTrue(0.3 < your103 < 3.0, your103)            # about $1/CF, not $11.25
                self.assertEqual(errors, [])

    def test_jump_bar_and_hint(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                hrefs = page.eval_on_selector_all("#r-jump a", "as => as.map(a => a.getAttribute('href'))")
                self.assertEqual(hrefs, ["#result-table", "#p-similar", "#p-worksheet"])
                self.assertTrue(page.is_visible("#ws-hint"))
                page.click("#ws-hint-close")
                self.assertFalse(page.is_visible("#ws-hint"))
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws.refs")
                self.assertFalse(page.is_visible("#ws-hint"))      # remembered
                order = page.eval_on_selector_all(
                    "#sec-results > .panel", "ps => ps.map(p => p.id)")
                self.assertLess(order.index("p-worksheet"), order.index("p-similar"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
