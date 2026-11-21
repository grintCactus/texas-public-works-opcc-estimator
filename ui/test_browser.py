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

import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

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

    def estimate_page(self, engine, width=1280, height=900):
        browser = getattr(self.pw, engine).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(self.url + "/")
        page.wait_for_function(SAMPLE_READY, timeout=60000)
        page.click("#btn-sample")
        page.click("#btn-parse-text")
        page.wait_for_function("() => S.lines.length === 10 && S.lines.every(l => l.cls)")
        page.click("#btn-estimate")
        page.wait_for_function("() => S.result && S.ws.refs", timeout=30000)
        # no smooth scrolling under test: boxes are measured right away
        page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
        page.wait_for_timeout(700)
        return page, errors

    def expanded(self, page, row):
        return page.get_attribute('tr[data-line="%s"]' % row, "aria-expanded")

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
                page.select_option(row + " select.pick", "model")
                self.assertEqual(self.expanded(page, "r4"), "false")
                page.fill(row + " input.note", "checked with the vendor")
                page.keyboard.press("Enter")       # Enter in a field: no toggle
                self.assertEqual(self.expanded(page, "r4"), "false")
                self.assertEqual(errors, [])

    def test_the_quick_pick_reads_pick_and_names_its_source(self):
        """QA 2026-11-20 (bug 23): the 96 px quick-pick box showed the applied
        source's long label, cut to "Model price !". It is a menu now: it
        always reads "pick…", and the chosen source shows under the price."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                shown = page.evaluate("() => [...document.querySelectorAll('#result-body select.pick')]"
                                      ".map(s => s.options[s.selectedIndex].text)")
                self.assertTrue(shown)
                self.assertEqual(set(shown), {"pick…"})
                row = 'tr[data-line="r3"]'
                want = page.evaluate("() => S.ws.refs && S.ws.refs.r3 && S.ws.refs.r3.range.median")
                page.select_option(row + " select.pick", "ref_median")
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate(
                    "r => { const s = document.querySelector(r + ' select.pick'); "
                    "return s.options[s.selectedIndex].text; }", row), "pick…")
                self.assertIn("Reference median", page.inner_text(row + " td:has(.yourcell) .dim.small"))
                got = float(page.input_value(row + " input.yourp").replace(",", ""))
                self.assertAlmostEqual(got, round(want, 2), places=2)
                self.assertEqual(self.expanded(page, "r3"), "false")
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

    def test_a_contingency_line_is_a_share_picked_from_a_list(self):
        """2026-11-21 (user): a contingency is a percentage picked from a
        list. The line has no item type and no Change button; in the
        worksheet it is that share of your other line amounts, with no
        references; the worksheet's own contingency is a list too."""
        for engine in self.engines:
            with self.subTest(engine=engine):
                page, errors = self.estimate_page(engine)
                k = page.evaluate("() => S.lines.find(l => /contingency/i.test(l.description)).row_id")
                row = 'tr[data-row="%s"]' % k
                self.assertEqual(page.locator(row).get_by_role("button", name="Change", exact=True).count(), 0)
                self.assertEqual(page.inner_text(row + " .clscell").strip(), "Contingency")
                self.assertEqual(page.input_value(row + " select.cpct"), "10")
                # the worksheet: that share of your other line amounts, nothing to open
                wrow = 'tr[data-line="%s"]' % k
                self.assertEqual(page.locator(wrow + " .chev").count(), 0)
                self.assertEqual(page.locator(wrow + " select.pick").count(), 0)
                share = page.evaluate("k => { const l = S.result.lines.find(x => x.row_id === k);"
                                      " return wsAmount(l, wsLine(l)) / wsShareBase(); }", k)
                self.assertAlmostEqual(share, 0.10, places=6)
                # the schedule carries a contingency: the worksheet's own one
                # starts at 0%, picked from a list
                sel = '#ws-totals select[data-fk="ws:contingency"]'
                self.assertEqual(page.input_value(sel), "0")
                page.select_option(sel, "15")
                page.wait_for_timeout(300)
                self.assertEqual(page.evaluate("() => S.ws.contingency"), 15)
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
              const i = tr.querySelector('input[data-col="' + c + '"]');
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
                    self.assertIn("assumed", page.inner_text('tr[data-row="r1"] .clscell'))
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
                self.drag(page, 'tr[data-row="r10"] .clscell', r2["y"] + r2["height"] * 0.75,
                          release=False)
                # live: the rows it passed have slid down already
                moved = page.evaluate("() => ['r3', 'r9'].map(k => document.querySelector("
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
        page.focus('tr[data-row="r3"] input[data-col="description"]')
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
                self.assertIn("unit: few bids", page.inner_text("#review-body"))
                page.click("#btn-estimate")
                page.wait_for_function("() => S.result && S.ws.refs", timeout=30000)
                page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
                p50, p80 = page.evaluate("() => [S.result.headline.p50, S.result.headline.p80]")
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
                self.assertEqual(page.evaluate("() => [S.result.headline.p50, S.result.headline.p80]"), [p50, p80])
                api = page.evaluate("""async () => { const r = await fetch('/api/estimate', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(S.resultReq)});
                    const j = await r.json(); return [j.headline.p50, j.headline.p80]; }""")
                self.assertEqual(api, [p50, p80])
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
