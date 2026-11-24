"""Regression tests for the estimator UI on the production estimator.

    .venv/bin/python ui/test_ui.py            # everything
    .venv/bin/python ui/test_ui.py Safety     # one class
    .venv/bin/python ui/run.py --test         # the same, via the launcher

stdlib unittest only. The data-backed tests read the live data/clean and
src/ and assert invariants (equality with the estimator's own output,
direction, refusal, provenance) rather than specific prices, so a data
rebuild changes the numbers without breaking the suite.

The UI prices ONLY through src/estimate_low_bid.py (ui/engine_production.py).
Its own engine of 2026-11-18 (ui/pricing.py) is superseded and not imported;
that engine's suite is archived outside the repository.

Rules pinned here:
  * the OPCC / engineer's estimate is never an input (400);
  * no company (internal) or locked-test row reaches a comparable, a history
    or a reference table;
  * P50 / P80 are the estimator's own numbers, to the last bit;
  * nothing is silently priced: unclassified, unpriced, not-covered and
    owner-set lines are all said out loud;
  * decisions are keyed on dict keys and class ids, never on label text, and
    no CJK text reaches the page on English input.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import os
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app                        # noqa: E402
import engine_production as ep    # noqa: E402
import exporter                   # noqa: E402
import matching                   # noqa: E402
import upstream                   # noqa: E402
from ingest import takeoff        # noqa: E402

E = upstream.estimator
ENG = app.ENGINE


def setUpModule():
    ENG.load()
    if not ENG.state["ready"]:
        raise RuntimeError("the production estimator did not load: %s"
                           % ENG.state["error"])


def item(desc, unit, qty, n=None, **kw):
    d = {"row_id": "r%s" % (n or desc[:12]), "item_no": str(n or ""),
         "description": desc, "unit": unit, "quantity": qty}
    d.update(kw)
    return d


#: A small municipal creek job used throughout.
CREEK = [
    item("Mobilization", "LS", 1, 1),
    item("Unclassified Channel Excavation", "CY", 4200, 2),
    item("Grouted Rock Riprap, 18 inch", "SY", 2600, 3),
    item("Gabion Mattress, 12 inch", "SY", 880, 4),
    item("Block Sod, Bermuda", "SY", 9500, 5),
    item("8 inch PVC SDR-26 Sanitary Sewer Pipe", "LF", 1450, 6),
    item("Trench Safety System", "LF", 1450, 7),
]


def estimate(items, n=4, market="municipal", bid_date="2026-03-02", **kw):
    payload = {"market": market, "n_bidders": n, "bid_date": bid_date,
               "owner": kw.pop("owner", ""), "work_type": kw.pop(
                   "work_type", "erosion"), "items": items}
    payload.update(kw)
    return ENG.estimate(payload)


def direct(items, n=4, market="municipal", bid_date="2026-03-02", owner="",
           work_type="erosion"):
    """The same input straight into estimate_low_bid.estimate()."""
    q = ENG._request({"market": market, "n_bidders": n, "items": items,
                      "bid_date": bid_date})
    its, _ = ENG.build_items(q["raw"], market)
    return E.estimate(ENG.tab, its, n, market,
                      bid_date[:4] if bid_date else None,
                      {"date": bid_date, "owner": owner or None,
                       "work_type": work_type})


def api(method, path, body=None, ctype="application/json"):
    """The API in-process (no socket): (status, decoded body)."""
    data = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
    st, ct, out, _ = app.dispatch(method, path, {"Content-Type": ctype}, data)
    return st, (json.loads(out) if "json" in ct else out)


# ===========================================================================
class Safety(unittest.TestCase):
    """Each of these is a way to get a confident, wrong number with no
    warning. Do not relax them."""

    def test_opcc_is_never_an_input(self):
        for k in ep.FORBIDDEN_INPUTS:
            with self.subTest(field=k):
                st, body = api("POST", "/api/estimate",
                               {"market": "municipal", "n_bidders": 4,
                                "items": CREEK, k: 1234567})
                self.assertEqual(st, 400)
                self.assertIn(k, body["detail"])
        # and refused by the engine itself, not only by the route
        with self.assertRaises(ep.BadRequest):
            estimate(CREEK, opcc=1)

    def test_unclassified_description_is_never_silently_priced(self):
        """"Unobtainium Lattice Deflector" matches no family. Whatever the
        estimator does with it, the line must not come back clean."""
        r = estimate(CREEK + [item("Unobtainium Lattice Deflector", "EA", 3,
                                   99)])
        ln = r["lines"][-1]
        self.assertIn(ln["family"], ("other", "needs_review", ""))
        if ln["status"] == "priced":
            levels = {w["level"] for w in ln["warnings"]}
            self.assertTrue(levels & {"severe", "caution"},
                            "an unclassified line was priced without a flag")
            self.assertTrue(any("unclassified" in w["text"]
                                for w in ln["warnings"]))
            if ln["evidence"]["usable"] == "unclassified":
                self.assertIn("unclassified_priced",
                              {a["code"] for a in r["alerts"]})
        else:
            self.assertEqual(ln["status"], "unpriced")
        c = ENG.classify_one("Unobtainium Lattice Deflector", "EA")
        self.assertTrue(any("unclassified" in x for x in c["issues"]))

    def test_heavy_line_on_thin_evidence_is_named(self):
        """2026-11-21: "Embankment Fill 48,600 CF" priced from the single
        earlier letting in CF carried half the expected low bid. The results
        must name such a line; P50 / P80 must not change."""
        base = [item("Block Sod, Bermuda", "SY", 9500, 1),
                item("Rock Riprap 18 inch", "CY", 640, 2)]
        heavy = base + [item("Embankment Fill", "CF", 48600, 3)]
        r = estimate(heavy, n=6, bid_date="2026-11-21", work_type="")
        ln = r["lines"][-1]
        if ln["status"] == "priced" and 0 < (ln["evidence"]["n_projects"] or 0) < 3 \
                and ln["amount_p50"] >= 0.15 * r["headline"]["p50"]:
            al = [a for a in r["alerts"] if a["code"] == "thin_heavy_lines"]
            self.assertEqual(len(al), 1)
            self.assertIn("Embankment Fill", al[0]["text"])
            self.assertTrue(al[0]["items"][0].startswith("Embankment Fill"))
            self.assertLess(len(al[0]["title"]), 90)
        r2 = estimate(base, n=6, bid_date="2026-11-21", work_type="")
        self.assertNotIn("thin_heavy_lines", {a["code"] for a in r2["alerts"]})
        # an alert only: the same request priced straight through the estimator
        d = direct(heavy, n=6, bid_date="2026-11-21", work_type="")
        self.assertEqual(r["headline"]["p50"], d.get("expected_low"))

    def test_contingency_line_as_a_share_of_the_rest(self):
        """2026-11-21 (user: a contingency is a share, not a number): an
        owner-set line given as amount_pct is priced at that share of the
        other lines' expected low bid (less their own owner-set money), taken
        at face value; the same amount through the estimator directly gives
        the same P50 (UI = production)."""
        rest = [item("Rock Riprap 18 inch", "CY", 640, 1),
                item("Block Sod, Bermuda", "SY", 9500, 2)]
        cont = item("Owner's Contingency Allowance", "LS", 1, 3)
        cont["amount_pct"] = 10
        r = estimate(rest + [cont], n=5, bid_date="2026-11-21")
        r0 = estimate(rest, n=5, bid_date="2026-11-21")
        base = r0["headline"]["p50"] - (r0["totals"]["fixed_total"] or 0.0)
        ln = r["lines"][-1]
        self.assertEqual(ln["status"], "owner_set")
        self.assertAlmostEqual(ln["amount_p50"], round(0.10 * base, 2), places=2)
        self.assertEqual(ln["amount_from_pct"]["pct"], 10.0)
        self.assertNotIn("allowance_missing", {a["code"] for a in r["alerts"]})
        self.assertIn("10% of the subtotal", ln["basis"])
        fixed = dict(cont, amount=ln["amount_p50"])
        fixed.pop("amount_pct")
        d = direct(rest + [fixed], n=5, bid_date="2026-11-21")
        self.assertEqual(r["headline"]["p50"], d.get("expected_low"))
        # an entered amount wins over a share
        both = dict(cont, amount=12345.0)
        r2 = estimate(rest + [both], n=5, bid_date="2026-11-21")
        self.assertEqual(r2["lines"][-1]["amount_p50"], 12345.0)
        self.assertIsNone(r2["lines"][-1]["amount_from_pct"])

    def test_subtotal_then_contingency_then_total(self):
        """2026-11-21 (user): the subtotal first, the contingency below it,
        then the total. The share is of the subtotal, owner-set allowances
        included; P50 and P80 each split into subtotal + contingency, and at
        P80 the contingency is still that share of P80's subtotal."""
        rest = [item("Rock Riprap 18 inch", "CY", 640, 1),
                item("Block Sod, Bermuda", "SY", 9500, 2),
                item("Testing Allowance", "LS", 1, 3, amount=25000.0)]
        cont = item("Owner's Contingency Allowance", "LS", 1, 4, amount_pct=10)
        r0 = estimate(rest, n=5, bid_date="2026-11-21")
        r = estimate(rest + [cont], n=5, bid_date="2026-11-21")
        h0, h = r0["headline"], r["headline"]
        self.assertIsNone(h0["contingency"])
        self.assertAlmostEqual(h["p50"], 1.10 * h0["p50"], places=2)   # allowance in the base
        self.assertAlmostEqual(h["p80"], 1.10 * h0["p80"], places=2)
        c = h["contingency"]
        self.assertEqual((c["row_ids"], c["pct"], c["fixed"]), (["r4"], 10.0, 0))
        for k in ("p50", "p80"):
            s, cv = c[k]["subtotal"], c[k]["contingency"]
            self.assertAlmostEqual(s + cv, h[k], places=4)
            self.assertAlmostEqual(cv, 0.10 * s, places=4)
            self.assertAlmostEqual(s, h0[k], places=2)
        self.assertEqual([ln["below_subtotal"] for ln in r["lines"]],
                         [False, False, False, True])
        # the owner's fixed amount stays at face value at P50 and P80
        fixed = item("Owner's Contingency Allowance", "LS", 1, 4, amount=50000.0)
        rf = estimate(rest + [fixed], n=5, bid_date="2026-11-21")
        cf = rf["headline"]["contingency"]
        self.assertEqual((cf["pct"], cf["fixed"]), (None, 50000.0))
        self.assertEqual((cf["p50"]["contingency"], cf["p80"]["contingency"]), (50000.0, 50000.0))
        # a "contingency item" measured in SY is a bid item: in the subtotal
        ci = estimate(rest + [item("Contingency Item: Sidewalk Repair", "SY", 50, 5)],
                      n=5, bid_date="2026-11-21")
        self.assertFalse(ci["lines"][-1]["below_subtotal"])
        self.assertIsNone(ci["headline"]["contingency"])

    def test_missing_quantity_is_not_priced_and_says_so(self):
        for qty in (None, 0, -5):
            with self.subTest(qty=qty):
                r = estimate(CREEK + [item("Rock Riprap", "SY", qty, 99)])
                ln = r["lines"][-1]
                self.assertEqual(ln["status"], "unpriced")
                self.assertIsNone(ln["amount_p50"])
                self.assertTrue(any(w["level"] == "severe"
                                    for w in ln["warnings"]))
                self.assertIn("lines_unpriced",
                              {a["code"] for a in r["alerts"]})

    def test_single_bidder_gets_no_discount(self):
        r = estimate(CREEK, n=1)
        self.assertEqual(r["competition"]["f"], 1.0)
        self.assertTrue(r["competition"]["note"])

    def test_a_bidder_count_above_the_table_is_priced_off_its_top_bin(self):
        """Round 18 (round 16's clamp, installed 2026-11-21): a bidder count
        above the discount table's top bin is priced off the top bin, not
        refused; any count above it gets the same discount."""
        r40, r60 = estimate(CREEK, n=40), estimate(CREEK, n=60)
        for r in (r40, r60):
            self.assertIsNotNone(r["headline"]["p50"])
            self.assertGreater(r["headline"]["p80"], r["headline"]["p50"])
            self.assertNotIn("no_estimate", {a["code"] for a in r["alerts"]})
        self.assertEqual(r40["competition"]["f"], r60["competition"]["f"])
        self.assertLess(r40["competition"]["f"], 1.0)

    def test_nothing_priced_is_a_refusal_not_zero(self):
        r = estimate([item("Unobtainium Lattice Deflector", "XX", 0, 1)])
        self.assertIsNone(r["headline"]["p50"])
        self.assertFalse(r["ok"])
        self.assertTrue(any(a["level"] == "severe" for a in r["alerts"]))

    def test_p50_always_comes_with_p80_and_bands(self):
        r = estimate(CREEK)
        h = r["headline"]
        self.assertGreater(h["p80"], h["p50"])
        lo, hi = h["p50_band"]
        self.assertLess(lo, h["p50"])
        self.assertGreater(hi, h["p50"])
        lo80, hi80 = h["p80_band"]
        self.assertLess(lo80, lo)
        self.assertGreater(hi80, hi)
        self.assertGreater(h["calibration_n"], 40)

    def test_no_cjk_text_reaches_the_page(self):
        """The estimator's display labels are bilingual in src (for now);
        the UI shows its own English. Checked on a schedule that has every
        block: not covered, owner-set, low confidence."""
        r = estimate(CREEK + [
            item("Submersible Pump, 15 HP", "EA", 2, 8, quote=84000),
            item("Owner's Contingency Allowance", "LS", 1, 9, amount=50000),
            item("Traffic Control", "LS", 1, 10)])
        self.assertFalse(ep.has_cjk(r), "CJK text in the estimate result")
        self.assertFalse(ep.has_cjk(ENG.options()))
        self.assertFalse(ep.has_cjk(ENG.classify(
            [{"row_id": "a", "description": "Mobilization", "unit": "LS"}])))
        csv_text = exporter.to_csv(r).decode("utf-8-sig")
        self.assertFalse(ep.has_cjk(csv_text))

    def test_unit_less_line_is_flagged(self):
        c = ENG.classify_one("Rock Riprap", "")
        self.assertTrue(any("no unit" in x for x in c["issues"]))


# ===========================================================================
class ProductionParity(unittest.TestCase):
    """The UI's numbers are the production estimator's numbers."""

    def test_p50_p80_equal_estimate_low_bid_output(self):
        cases = [
            dict(items=CREEK, n=4, market="municipal", bid_date="2026-03-02"),
            dict(items=CREEK, n=6, market="municipal", bid_date="2024-05-15",
                 owner="frisco", work_type="drainage"),
            dict(items=CREEK, n=5, market="txdot", bid_date="2025-06-01",
                 owner="", work_type=""),
        ]
        for c in cases:
            with self.subTest(market=c["market"], n=c["n"]):
                r = estimate(c["items"], c["n"], c["market"], c["bid_date"],
                             owner=c.get("owner", ""),
                             work_type=c.get("work_type", "erosion"))
                d = direct(c["items"], c["n"], c["market"], c["bid_date"],
                           c.get("owner", ""), c.get("work_type", "erosion"))
                self.assertEqual(r["headline"]["p50"], d["expected_low"])
                self.assertEqual(r["headline"]["p80"], d["expected_low_p80"])
                self.assertEqual(r["totals"]["market_total"],
                                 d["market_total"])
                self.assertEqual(r["competition"]["f"], d["discount"]["f"])

    def test_equal_to_run_schedule_on_the_same_classification(self):
        """The CLI's own path (run_schedule reads a schedule CSV and loads
        its own tables)."""
        import integration_check as ic
        lines = [dict(l, quantity=float(l["quantity"])) for l in CREEK]
        cls = ENG.classify(lines, "municipal")
        r, _ = ic.production_path(lines, cls, "municipal", 4, "2026-03-02",
                                  "", "erosion")
        ui = estimate(CREEK, 4, "municipal", "2026-03-02")
        self.assertEqual(ui["headline"]["p50"], r["expected_low"])
        self.assertEqual(ui["headline"]["p80"], r["expected_low_p80"])

    def test_lines_add_up_to_p50(self):
        r = estimate(CREEK + [item("Owner's Contingency Allowance", "LS", 1,
                                   9, amount=25000)])
        s = sum(l["amount_p50"] or 0 for l in r["lines"])
        self.assertAlmostEqual(s, r["headline"]["p50"],
                               delta=1e-6 * r["headline"]["p50"])

    def test_line_prices_are_the_traces(self):
        r = estimate(CREEK)
        d = direct(CREEK)
        self.assertEqual([l["market_unit_price"] for l in r["lines"]],
                         [t["price"] for t in d["trace"]])

    def test_estimator_switches_are_production_defaults(self):
        """The UI never flips a switch (pricing_rule) -- it would price
        with a rule nobody confirmed."""
        sw = ENG.info["estimator"]["switches"]
        self.assertTrue(sw["COVERAGE_SCOPE"])
        self.assertTrue(sw["COMPARABLES_BLEND"])
        self.assertTrue(sw["LOWCONF"])
        self.assertEqual(sw["MUNI_CELLS"], E.MUNI_CELLS)

    def test_same_input_same_answer(self):
        """The estimator memoises as it goes; a repeat must not drift."""
        a = estimate(CREEK)
        estimate(CREEK + [item("Rock Riprap", "SY", 40, 40)], n=6)
        b = estimate([dict(x) for x in CREEK])
        self.assertEqual(a["headline"]["p50"], b["headline"]["p50"])
        self.assertEqual(a["headline"]["p80"], b["headline"]["p80"])


# ===========================================================================
class NotCovered(unittest.TestCase):
    """Specialty lines with no history are listed, never priced; quotes
    complete the total."""

    PUMP = item("Submersible Pump, 15 HP", "EA", 2, 8)
    GEN = item("Standby Diesel Generator, 150 kW", "EA", 1, 9)

    def test_uncovered_lines_are_listed_and_not_priced(self):
        r = estimate(CREEK + [self.PUMP, self.GEN])
        nc = r["not_covered"]
        self.assertEqual(nc["n"], 2)
        self.assertEqual({l["class"] for l in nc["lines"]},
                         {"pump_equipment", "generator_electrical_gear"})
        for ln in r["lines"][-2:]:
            self.assertEqual(ln["status"], "not_covered")
            self.assertIsNone(ln["amount_p50"])
        self.assertTrue(r["headline"]["covered_only"])
        self.assertIn("not_covered", {a["code"] for a in r["alerts"]})

    def test_uncovered_lines_do_not_move_p50(self):
        base = estimate(CREEK)
        with_unc = estimate(CREEK + [self.PUMP, self.GEN])
        self.assertEqual(base["headline"]["p50"], with_unc["headline"]["p50"])

    def test_complete_total_only_once_every_line_has_a_quote(self):
        none = estimate(CREEK + [self.PUMP, self.GEN])
        self.assertIsNone(none["not_covered"]["complete_total"])
        one = estimate(CREEK + [dict(self.PUMP, quote=84000), self.GEN])
        self.assertIsNone(one["not_covered"]["complete_total"])
        self.assertEqual(one["not_covered"]["quoted_n"], 1)
        both = estimate(CREEK + [dict(self.PUMP, quote=84000),
                                 dict(self.GEN, quote=61000)])
        nc, h = both["not_covered"], both["headline"]
        self.assertEqual(nc["quoted_total"], 145000)
        self.assertAlmostEqual(nc["complete_total"], h["p50"] + 145000,
                               places=6)
        self.assertAlmostEqual(nc["complete_total_p80"], h["p80"] + 145000,
                               places=6)
        # quotes are face value: they do not move the covered P50 either
        self.assertEqual(h["p50"], none["headline"]["p50"])

    def test_mostly_uncovered_schedule_gets_no_estimate(self):
        r = estimate([self.PUMP, self.GEN, item("Mobilization", "LS", 1, 1)])
        self.assertTrue(r["not_covered"]["as_a_whole"])
        self.assertIsNone(r["headline"]["p50"])
        self.assertIn("not_covered_whole", {a["code"] for a in r["alerts"]})

    def test_classification_marks_the_line_before_pricing(self):
        c = ENG.classify_one(self.PUMP["description"], "EA")
        self.assertEqual(c["coverage_class"], "pump_equipment")
        self.assertTrue(c["coverage_label"])


# ===========================================================================
class OwnerSet(unittest.TestCase):
    """Owner-set money (allowances, contingencies, force accounts) is taken
    at face value: no discount, no centring, no comparables."""

    def _with(self, amount):
        return estimate(CREEK + [item("Owner's Contingency Allowance", "LS",
                                      1, 9, amount=amount)])

    def test_amount_is_taken_at_face_value(self):
        r = self._with(50000)
        ln = r["lines"][-1]
        self.assertEqual(ln["status"], "owner_set")
        self.assertEqual(ln["amount_p50"], 50000)
        self.assertIsNone(ln["market_unit_price"])
        self.assertEqual(r["totals"]["fixed_total"], 50000)
        self.assertEqual(r["headline"]["fixed_total"], 50000)

    def test_amount_passes_straight_into_p50(self):
        a, b = self._with(50000), self._with(250000)
        self.assertAlmostEqual(a["headline"]["p50"] - 50000,
                               b["headline"]["p50"] - 250000, places=6)
        base = estimate(CREEK)
        self.assertAlmostEqual(a["headline"]["p50"] - 50000,
                               base["headline"]["p50"], places=6)

    def test_allowance_without_amount_is_flagged(self):
        r = self._with(None)
        ln = r["lines"][-1]
        self.assertEqual(ln["status"], "priced")
        self.assertTrue(ln["allowance_like"])
        self.assertIn("allowance_missing", {a["code"] for a in r["alerts"]})


# ===========================================================================
class Allowances(unittest.TestCase):
    """An allowance is owner-set money: its amount comes from the bid form,
    goes in at face value, and is never quietly replaced by a historical
    price."""

    @classmethod
    def setUpClass(cls):
        keys = [k for k in ENG.panel.lines if "Hunterwood" in k]
        if not keys:
            raise unittest.SkipTest("the public Hunterwood letting is not in "
                                    "the data")
        # the public bid form (external rows): item no, description, unit,
        # quantity -- not the user's takeoff file
        rows = sorted(ENG.panel.lines[keys[0]],
                      key=lambda r: (len(r[0]), r[0]))
        cls.items = [item(r[1], r[3], r[4], r[0]) for r in rows]
        cls.allow = [it for it in cls.items
                     if ENG.classify_one(it["description"],
                                         it["unit"])["owner_set_like"]]
        cls.kw = dict(n=5, market="municipal", bid_date="2025-10-09",
                      owner="legistar/coppell", work_type="erosion")

    def _with_amount(self, amount):
        return [dict(it, amount=amount) if it in self.allow else it
                for it in self.items]

    def test_hunterwood_allowance_identical_to_run_schedule(self):
        """Project Allowance = $80,000 (every bidder's figure on the public
        tab): the UI equals run_schedule given amount + owner_fixed=yes."""
        import integration_check as ic
        self.assertEqual(len(self.allow), 1)
        items = self._with_amount(80000.0)
        ui = estimate(items, **self.kw)
        cls = ENG.classify(items, "municipal")
        amounts = {it["row_id"]: it["amount"] for it in items
                   if it.get("amount")}
        r, _ = ic.production_path(items, cls, "municipal", 5, "2025-10-09",
                                  "legistar/coppell", "erosion", amounts)
        self.assertEqual(ui["headline"]["p50"], r["expected_low"])
        self.assertEqual(ui["headline"]["p80"], r["expected_low_p80"])
        self.assertEqual(r["fixed_total"], 80000.0)
        ln = [l for l in ui["lines"] if l["row_id"] == self.allow[0]["row_id"]][0]
        self.assertEqual(ln["status"], "owner_set")
        self.assertEqual(ln["amount_p50"], 80000.0)
        self.assertFalse(ln["amount_missing"])
        self.assertNotIn("allowance_missing", {a["code"] for a in ui["alerts"]})

    def test_missing_amount_keeps_production_number_but_says_so(self):
        ui = estimate(self.items, **self.kw)
        d = direct(self.items, 5, "municipal", "2025-10-09",
                   "legistar/coppell", "erosion")
        self.assertEqual(ui["headline"]["p50"], d["expected_low"])
        ln = [l for l in ui["lines"] if l["row_id"] == self.allow[0]["row_id"]][0]
        self.assertEqual(ln["status"], "priced")
        self.assertTrue(ln["amount_missing"])
        self.assertIn({"level": "severe", "code": "allowance_missing",
                       "text": ep.ALLOWANCE_MISSING}, ln["warnings"])
        al = [a for a in ui["alerts"] if a["code"] == "allowance_missing"]
        self.assertEqual(len(al), 1)
        self.assertEqual(al[0]["level"], "severe")
        self.assertEqual(al[0]["placement"], "above_headline")
        # short and clear (2026-11-21): what is wrong, what to do, which lines
        self.assertEqual(al[0]["title"], "1 owner-set allowance has no amount, so the totals are too low.")
        self.assertIn("Project details", al[0]["action"])
        self.assertEqual(len(al[0]["items"]), 1)
        self.assertIn(al[0]["title"], al[0]["text"])

    def test_detection(self):
        yes = ("Project Allowance", "Owner's Contingency", "Force Account Work",
               "Cash Allowance for Testing", "Owner Directed Work")
        no = ("Owner Furnished Pipe, Install", "Mobilization", "Rock Riprap")
        for d in yes:
            with self.subTest(d=d):
                self.assertTrue(ENG.classify_one(d, "LS")["owner_set_like"])
        for d in no:
            with self.subTest(d=d):
                self.assertFalse(ENG.classify_one(d, "LS")["owner_set_like"])

    def test_file_amounts_apply_to_allowance_lines_only(self):
        out = ENG.classify([
            {"row_id": "a", "description": "Project Allowance", "unit": "EA",
             "quantity": 1, "file_amount": 80000},
            {"row_id": "b", "description": "Force Account", "unit": "LS",
             "quantity": 2, "file_unit_price": 12500},
            {"row_id": "c", "description": "Rock Riprap", "unit": "SY",
             "quantity": 42, "file_amount": 6894.72}], "municipal")
        by = {c["row_id"]: c["import_amount"] for c in out}
        self.assertEqual(by, {"a": 80000.0, "b": 25000.0, "c": None})

    def test_parser_reads_amount_and_unit_price_columns(self):
        p = takeoff.from_text(
            "Item No.,Description,Quantity,Unit,Unit Price,Amount\n"
            "101,Mobilization,1,LS,46156.50,46156.50\n"
            "126,Project Allowance,1,EA,\"80,000.00\",\"80,000.00\"\n")
        self.assertEqual([(l.unit_price, l.amount) for l in p.lines],
                         [(46156.5, 46156.5), (80000.0, 80000.0)])
        p = takeoff.from_text("Item\tDescription\tUnit\tQty\tTotal Price\n"
                              "7\tForce Account Work\tLS\t1\t25000\n")
        self.assertEqual((p.lines[0].amount, p.lines[0].unit_price),
                         (25000.0, None))


# ===========================================================================
_PUBLIC = None


def public_keys():
    """(public letting keys, every other key) of master_panel.csv -- keys
    only, nothing else is read."""
    global _PUBLIC
    if _PUBLIC is None:
        import pandas as pd
        m = pd.read_csv(os.path.join(ep.CLEAN, "master_panel.csv"),
                        encoding="utf-8-sig", dtype=str,
                        keep_default_na=False,
                        usecols=["source_set", "project_key"])
        _PUBLIC = (set(m.loc[m.source_set == "external", "project_key"]),
                   set(m.loc[m.source_set != "external", "project_key"]))
    return _PUBLIC


class References(unittest.TestCase):
    """The reference projects behind each line price: the estimator's own
    cell, public rows only, lettings dated before the bid date."""

    @classmethod
    def setUpClass(cls):
        ENG.load_references()
        if not ENG.refs_state["ready"]:
            raise RuntimeError(ENG.refs_state["error"])
        keys = [k for k in ENG.panel.lines if "Hunterwood" in k]
        rows = sorted(ENG.panel.lines[keys[0]],
                      key=lambda r: (len(r[0]), r[0])) if keys else []
        cls.hw = [item(r[1], r[3], r[4], r[0]) for r in rows]
        cls.hw_req = {"market": "municipal", "n_bidders": 5,
                      "bid_date": "2025-10-09", "owner": "legistar/coppell",
                      "work_type": "erosion", "items": cls.hw}
        cls.creek_req = {"market": "municipal", "n_bidders": 4,
                         "bid_date": "2026-03-02", "work_type": "erosion",
                         "items": CREEK}
        cls.tx_req = {"market": "txdot", "n_bidders": 5,
                      "bid_date": "2025-06-01", "items": CREEK}

    def _all(self, req):
        return [ENG.references(req, it["row_id"]) for it in req["items"]]

    def test_references_reproduce_the_line_price(self):
        """For every priced line: the median of the listed lettings' median
        bids is the estimator's published cell price, and times the trace's
        own factors (size scaling, quantity factor, escalation) it is the
        line's price -- to the last bit."""
        kinds = set()
        for req in (self.hw_req, self.creek_req, self.tx_req):
            for rr in self._all(req):
                if rr.get("special"):
                    continue
                with self.subTest(line=rr["description"],
                                  market=req["market"]):
                    rep = rr["reproduced"]
                    self.assertTrue(rep["rows_match"], rep)
                    self.assertTrue(rep["price_match"], rep)
                    kinds.add(rr["kind"])
        self.assertEqual(kinds, {"tok", "band", "fam"})

    def test_references_are_public_and_dated_before_the_bid_date(self):
        public, other = public_keys()
        self.assertFalse(ENG.refs.keys & other)
        self.assertTrue(ENG.refs.keys <= public)
        for req in (self.hw_req, self.creek_req, self.tx_req):
            for rr in self._all(req):
                for x in rr["references"]:
                    self.assertIn(x["key"], public)
                    self.assertNotIn(x["key"], other)
                    self.assertLess(x["date"], req["bid_date"])

    def test_top_three_are_ranked_first(self):
        rr = ENG.references(self.hw_req, self.hw[2]["row_id"])
        sc = [x["score"] for x in rr["references"]]
        self.assertEqual(sc, sorted(sc, reverse=True))
        self.assertEqual(rr["top"], 3)
        x = rr["references"][0]
        for k in ("project", "owner", "date", "bidders", "low_total", "item",
                  "quantity", "median_unit", "min_unit", "max_unit", "bids",
                  "escalated_unit"):
            self.assertIn(k, x)
        self.assertTrue(x["bids"])

    def test_special_lines_say_so(self):
        req = dict(self.creek_req, items=CREEK + [
            item("Submersible Pump, 15 HP", "EA", 2, 8),
            item("Owner's Contingency Allowance", "LS", 1, 9, amount=50000),
            item("Rock Riprap", "SY", 0, 10)])
        by = {r: ENG.references(req, "r%d" % r)["special"] for r in (8, 9, 10)}
        self.assertEqual(by, {8: "not_covered", 9: "owner_set",
                              10: "unpriced"})

    def test_generic_match_is_warned(self):
        req = dict(self.creek_req, items=CREEK + [
            item("Unobtainium Lattice Deflector", "EA", 3, 99)])
        rr = ENG.references(req, "r99")
        self.assertIn("Generic match", rr["warning"])

    def test_endpoint_and_export(self):
        st, body = api("POST", "/api/references",
                       {"request": self.creek_req, "row_id": "r1"})
        self.assertEqual(st, 200)
        self.assertIn("derivation", body)
        st, _ = api("POST", "/api/references", {"request": self.creek_req})
        self.assertEqual(st, 400)
        st, _ = api("POST", "/api/references",
                    {"request": dict(self.creek_req, opcc=1), "row_id": "r1"})
        self.assertEqual(st, 400)
        result = app._clean(ENG.estimate(self.creek_req))
        st, ct, data, _ = app.dispatch(
            "POST", "/api/export/csv", {"Content-Type": "application/json"},
            json.dumps({"result": result, "request": self.creek_req}).encode())
        self.assertEqual(st, 200)
        self.assertIn("Line references (top 3 per line)",
                      data.decode("utf-8-sig"))
        if exporter.xlsx_available():
            import openpyxl
            st, ct, data, _ = app.dispatch(
                "POST", "/api/export/xlsx",
                {"Content-Type": "application/json"},
                json.dumps({"result": result,
                            "request": self.creek_req}).encode())
            self.assertIn("References",
                          openpyxl.load_workbook(io.BytesIO(data)).sheetnames)


# ===========================================================================
class LowConfidence(unittest.TestCase):

    def test_block_is_keyed_on_class_and_consistent(self):
        r = estimate(CREEK + [item("Traffic Control", "LS", 1, 10)])
        lc = r["low_confidence"]
        by = {g["class"]: g for g in lc["lines"]}
        self.assertIn("mobilization", by)
        self.assertIn("traffic_control", by)
        self.assertTrue(lc["headline_unchanged"])
        for cls, g in by.items():
            with self.subTest(cls=cls):
                self.assertEqual(g["label"], ep.LOWCONF_LABELS[cls])
                ours = sum(l["amount_p50"] or 0 for l in r["lines"]
                           if l["lowconf_class"] == cls)
                self.assertAlmostEqual(g["ours_p50"], ours, places=4)
                h = g["history"]
                if h:
                    self.assertLessEqual(h["p25"], h["median"])
                    self.assertLessEqual(h["median"], h["p75"])
                    self.assertGreaterEqual(h["lettings"],
                                            E.LOWCONF_MIN_LETTINGS)
                    lo, hi = g["implied_range"]
                    self.assertLess(lo, hi)
                    self.assertIn(g["position"], ("above", "below", "within"))

    def test_panel_does_not_move_the_headline(self):
        r = estimate(CREEK)
        d = direct(CREEK)
        self.assertEqual(r["headline"]["p50"], d["expected_low"])
        self.assertTrue(r["low_confidence"]["lines"])

    def test_unit_priced_traffic_control_is_not_low_confidence(self):
        c = ENG.classify_one("Traffic Control", "MO")
        self.assertIsNone(c["lowconf_class"])
        self.assertEqual(c["accuracy_group"], "traffic_control")
        c = ENG.classify_one("Traffic Control", "LS")
        self.assertEqual(c["lowconf_class"], "traffic_control")
        self.assertEqual(c["accuracy_group"], "LS:traffic_control")


# ===========================================================================
class Provenance(unittest.TestCase):
    """No company (internal) or locked-test row as evidence, comparable,
    history or price."""

    @classmethod
    def setUpClass(cls):
        import pandas as pd
        m = pd.read_csv(os.path.join(ep.CLEAN, "master_panel.csv"),
                        encoding="utf-8-sig", dtype=str,
                        keep_default_na=False,
                        usecols=["source_set", "project_key"])
        cls.not_public = set(m.loc[m.source_set != "external",
                                   "project_key"])
        cls.public = set(m.loc[m.source_set == "external", "project_key"])

    def test_comparables_pools_hold_public_lettings_only(self):
        for mk, pool in ENG.tab.comparables.items():
            keys = {Lc["pk"] for Lc in pool.ctx.L}
            with self.subTest(market=mk):
                self.assertTrue(keys)
                self.assertFalse(keys & self.not_public)
                self.assertTrue(keys <= self.public)

    def test_similar_project_cards_never_hold_an_internal_row(self):
        """The cards, their bids and their matched lines come from public
        lettings only -- never a company (internal) or locked-test row."""
        for kw in (dict(market="municipal"),
                   dict(market="municipal", owner="frisco",
                        work_type="drainage", bid_date="2025-06-01"),
                   dict(market="txdot", work_type="")):
            r = estimate(CREEK, **kw)
            cards = r["similar"]["cards"]
            with self.subTest(**kw):
                self.assertTrue(cards)
                keys = {c["key"] for c in cards}
                self.assertFalse(keys & self.not_public)
                self.assertTrue(keys <= self.public)
                for c in cards:
                    self.assertLess(c["date"], r["similar"]["before"])
                    self.assertTrue(c["matched_lines"])
        # what the cards read (bid totals, line prices, names) is indexed
        # for public pool lettings only
        idx = set(ENG.panel.totals) | set(ENG.panel.lines) | \
            set(ENG.panel.meta)
        self.assertFalse(idx & self.not_public)
        self.assertTrue(idx <= self.public)

    def test_bidder_history_is_public(self):
        keys = {h[1] for h in ENG.history.H}
        self.assertFalse(keys & self.not_public)

    def test_reference_tables_are_trained_on_public_rows_only(self):
        for f, ok in (("price_reference.csv", "public_external_only"),
                      ("price_reference_band.csv", "public_external_only"),
                      ("price_reference_tokens.csv", "public_external_only"),
                      ("estimator_calibration.csv", "public_external_only"),
                      ("price_book_txdot.csv", "txdot_published_only")):
            with self.subTest(file=f):
                with open(os.path.join(ep.CLEAN, f), encoding="utf-8-sig",
                          newline="") as fh:
                    vals = {r.get("train_scope") for r in csv.DictReader(fh)}
                self.assertEqual(vals, {ok})

    def test_history_search_reads_public_tables_only(self):
        """The picker's vocabulary is built from external rows only
        (build_vocabulary.py); its candidates carry no project keys."""
        st, body = api("GET", "/api/history/search?q=rock+riprap&unit=SY")
        self.assertEqual(st, 200)
        self.assertTrue(body["candidates"])
        for c in body["candidates"]:
            self.assertIn(c["kind"], ("vocabulary", "txdot", "family"))
            self.assertNotIn("project_key", c)


# ===========================================================================
class Classification(unittest.TestCase):
    """The parser's own per-line rules, so no lookup misses silently."""

    def test_parser_is_linked(self):
        self.assertTrue(upstream.PARSER_AVAILABLE,
                        "src/parse_bidtabs.py did not import: %s"
                        % upstream.IMPORT_ERROR)
        self.assertEqual(upstream.SIZE_BUCKET_SOURCE, "src/build_master.py")

    def test_reproduces_the_panels_own_classification(self):
        """classify_line(description, unit) on public panel rows gives back
        the panel's desc_norm, unit and size bucket, and its family except
        where the pipeline later set needs_review from document evidence."""
        import pandas as pd
        m = pd.read_csv(os.path.join(ep.CLEAN, "master_panel.csv"),
                        encoding="utf-8-sig", dtype=str,
                        keep_default_na=False,
                        usecols=["source_set", "source", "description",
                                 "unit", "desc_norm", "item_family",
                                 "size_bucket"])
        m = m[(m.source_set == "external") & (m.source == "city_bidtab")]
        s = m.drop_duplicates(["description", "unit"]).sample(
            3000, random_state=7)
        bad = []
        for r in s.itertuples():
            c = upstream.classify_line(r.description, r.unit)
            got = (c["desc_norm"], c["unit"], c["size_bucket"])
            if got != (r.desc_norm, r.unit, r.size_bucket):
                bad.append((r.description, got))
            elif r.item_family != "needs_review" and \
                    c["family"] != r.item_family:
                bad.append((r.description, c["family"], r.item_family))
        self.assertEqual(bad[:5], [])

    def test_size_is_the_pipelines(self):
        c = upstream.classify_line('12" Gabion Mattress', "CY")
        self.assertEqual(c["size_in"], 12.0)
        self.assertEqual(c["size_bucket"], "9-13 in")
        # an LF sidewalk is sized by its width in feet, a SY one keeps the
        # thickness (parse_bidtabs.lf_width_size)
        lf = upstream.classify_line("6' Concrete Sidewalk", "LF")
        self.assertEqual((lf["family"], lf["size_in"]), ("sidewalk", 6.0))
        sy = upstream.classify_line('4" Concrete Sidewalk', "SY")
        self.assertEqual(sy["size_in"], 4.0)

    def test_units_use_the_parsers_rules(self):
        self.assertEqual(upstream.classify_line("x", "Lump Sum")["unit"], "LS")
        self.assertEqual(upstream.classify_line(
            "Remove 4 inch sidewalk 0-50 s.y.", "Yard")["unit"], "SY")

    def test_family_override_is_kept_and_reported(self):
        c = ENG.classify_one("Gravel Trail", "SY", family_override="flex_base")
        self.assertEqual(c["family"], "flex_base")
        self.assertTrue(c["family_overridden"])
        self.assertEqual(c["family_parser"], "sidewalk")
        r = estimate(CREEK + [item("Gravel Trail", "SY", 130, 30,
                                   family="flex_base")])
        self.assertEqual(r["lines"][-1]["family"], "flex_base")

    def test_normaliser_is_the_one_that_built_the_vocabulary(self):
        import data_access as da
        vocab = da.table("vocabulary")
        for s in vocab["desc_norm"].dropna().astype(str).head(200):
            self.assertEqual(upstream.norm_desc(s), s)


# ===========================================================================
class ExpectedBidders(unittest.TestCase):
    """Rule N1 (handoff/scratchpad/diag_twostage/NHAT_RULE.md)."""

    def test_auto_uses_the_rule_and_says_where_it_came_from(self):
        r = estimate(CREEK, n="auto")
        c = r["competition"]
        self.assertEqual(c["source"], "rule N1")
        s = c["suggestion"]
        self.assertEqual(c["n_bidders"], s["n"])
        self.assertIn(s["cell"], ("market, work type, size", "market, size",
                                  "market", "all markets", "default"))
        if s["cell"] != "default":
            self.assertGreaterEqual(s["history_lettings"], ep.N1_MIN)
            self.assertEqual(s["n"], int(math.floor(s["median"] + 0.5)))
        self.assertEqual(r["inputs"]["bidders_source"], "rule N1")

    def test_user_value_wins_and_the_suggestion_is_still_shown(self):
        r = estimate(CREEK, n=7)
        self.assertEqual(r["competition"]["n_bidders"], 7)
        self.assertEqual(r["competition"]["source"], "user")
        self.assertTrue(r["competition"]["suggestion"])

    def test_history_is_strictly_before_the_bid_date(self):
        s = ENG.history.suggest("municipal", "erosion", 1e6, "1990-01-01")
        self.assertEqual((s["n"], s["cell"]), (ep.N1_DEFAULT, "default"))
        s1 = ENG.history.suggest("municipal", "", 1e6, "2024-01-01")
        s2 = ENG.history.suggest("municipal", "", 1e6, "2026-11-01")
        self.assertLessEqual(s1["history_lettings"], s2["history_lettings"])

    def test_size_classes(self):
        self.assertEqual([ep.size_class(x) for x in
                          (0, 499999, 500000, 1999999, 2e6, 9e9)],
                         [0, 0, 1, 1, 2, 2])

    def test_endpoint_matches_the_estimate(self):
        payload = {"market": "municipal", "bid_date": "2025-10-09",
                   "work_type": "erosion", "items": CREEK}
        st, s = api("POST", "/api/bidders/suggest", payload)
        self.assertEqual(st, 200)
        r = estimate(CREEK, n="auto", bid_date="2025-10-09")
        self.assertEqual(s["n"], r["competition"]["n_bidders"])


# ===========================================================================
class ItemAccuracy(unittest.TestCase):

    def test_file_is_present_and_matches_its_thresholds(self):
        meta = ENG.accuracy.meta()
        self.assertTrue(meta["available"], meta["error"])
        th = meta["thresholds"]
        for mk, d in ENG.accuracy.doc["markets"].items():
            for g, s in d["groups"].items():
                with self.subTest(market=mk, group=g):
                    self.assertEqual(s["published"],
                                     s["lines"] >= th["min_lines"]
                                     and s["lettings"] >= th["min_lettings"])
                    self.assertGreaterEqual(s["half_within"], 0)
                    self.assertGreater(s["ratio"], 0)

    def test_weak_groups_are_marked_weak(self):
        a = ENG.accuracy
        self.assertEqual(a.badge("municipal", "mobilization")["weak"],
                         "lump_sum")
        self.assertEqual(a.badge("municipal", "LS:traffic_control")["weak"],
                         "lump_sum")
        self.assertEqual(a.badge("municipal", "other")["weak"],
                         "unclassified")
        self.assertIsNone(a.badge("municipal", "sidewalk")["weak"])
        self.assertEqual(a.badge("municipal", "no_such_group")["tier"],
                         "none")

    def test_badge_text_and_colour(self):
        b = ENG.accuracy.badge("municipal", "barrier_free_ramp")
        hw = ENG.accuracy.doc["markets"]["municipal"]["groups"][
            "barrier_free_ramp"]["half_within"]
        self.assertEqual(b["text"], "typically ±%.0f%%" % (100 * hw))
        self.assertEqual(b["tier"], "good" if hw <= 0.25 else
                         "fair" if hw <= 0.5 else "poor")
        self.assertIn("median bid", b["tooltip"])

    def test_lines_carry_their_badge(self):
        r = estimate(CREEK)
        for l in r["lines"]:
            self.assertIn(l["accuracy"]["tier"],
                          ("good", "fair", "poor", "none", "na"))
        mob = [l for l in r["lines"] if l["description"] == "Mobilization"][0]
        self.assertEqual(mob["accuracy"]["weak"], "lump_sum")

    def test_builder_reproduces_the_file(self):
        import build_item_accuracy as b
        if not (os.path.exists(b.DEFAULT_LINES) and
                os.path.exists(b.DEFAULT_FWD)):
            self.skipTest("line-accuracy outputs not present")
        if ENG.accuracy.doc["source"]["lines_csv_md5"] != b._md5(
                b.DEFAULT_LINES):
            self.skipTest("item_accuracy.json was built from other inputs")
        doc = b.build(b.DEFAULT_LINES, b.DEFAULT_FWD)
        doc = json.loads(json.dumps(doc))
        self.assertEqual(doc["markets"], ENG.accuracy.doc["markets"])


# ===========================================================================
class SimilarProjects(unittest.TestCase):
    """"Similar past projects": comparables.py's own picks, in its order."""

    def _raw(self, market, owner, work_type, bid_date, n=4):
        q = ENG._request({"market": market, "n_bidders": n, "items": CREEK,
                          "bid_date": bid_date})
        its, _ = ENG.build_items(q["raw"], market)
        tgt = {"date": bid_date, "owner": owner or None,
               "work_type": work_type}
        return its, tgt, E.estimate(ENG.tab, its, n, market, bid_date[:4],
                                    tgt)

    CASES = (dict(market="municipal", owner="frisco", work_type="drainage",
                  bid_date="2024-03-01"),
             dict(market="municipal", owner="", work_type="erosion",
                  bid_date="2026-03-02"),
             dict(market="txdot", owner="", work_type="",
                  bid_date="2025-06-01"))

    def test_ranking_matches_comparables_order(self):
        """The cards are comparables.py's chosen jobs in its own order, and
        the similarity shown, recomputed for display, ranks them the same."""
        for kw in self.CASES:
            with self.subTest(**kw):
                its, tgt, r = self._raw(**kw)
                sc = ENG.comparable_scores(r, its, kw["market"], tgt,
                                           kw["bid_date"][:4], 4)
                ctx = ENG.tab.comparables[kw["market"]].ctx
                rank = sorted(sc, key=lambda pk: (
                    -sc[pk]["S"],
                    tuple(-ord(ch) for ch in ctx.L[ctx.pk_index[pk]]["date"]),
                    pk))
                self.assertEqual(rank[:len(r["comparables"]["comps"])],
                                 r["comparables"]["comps"])
                self.assertEqual(len(sc), r["comparables"]["n_candidates"])
                ui = estimate(CREEK, market=kw["market"], owner=kw["owner"],
                              work_type=kw["work_type"],
                              bid_date=kw["bid_date"])
                cards = ui["similar"]["cards"]
                self.assertEqual([c["key"] for c in cards],
                                 r["comparables"]["comps"][:ep.MAX_SIMILAR])
                self.assertEqual([c["rank"] for c in cards],
                                 list(range(1, len(cards) + 1)))
                sims = [c["similarity"] for c in cards]
                self.assertEqual(sims, sorted(sims, reverse=True))

    def test_price_at_uses_comparables_own_transfer(self):
        """Unmasked, the view gives back estimate()'s own comparables price
        to the last bit -- so the per-project figure is price_target's."""
        for kw in self.CASES:
            with self.subTest(**kw):
                its, tgt, r = self._raw(**kw)
                v = ENG._cmp_view(r, its, kw["market"], tgt,
                                  kw["bid_date"][:4], 4)
                self.assertEqual(ENG.price_at(v, kw["market"], tgt)["cmp"],
                                 r["comparables"]["cmp"])
                one = ENG.price_at(v, kw["market"], tgt,
                                   only=r["comparables"]["comps"][0])
                self.assertEqual(one["n_comps"], 1)
                self.assertEqual(one["comps"], r["comparables"]["comps"][:1])

    def test_card_contents(self):
        r = estimate(CREEK, bid_date="2026-03-02")
        cards = r["similar"]["cards"]
        self.assertEqual(len(cards), ep.MAX_SIMILAR)
        for c in cards:
            with self.subTest(rank=c["rank"]):
                b = c["bids"]
                self.assertLessEqual(b["low"], b["median"])
                self.assertLessEqual(b["median"], b["high"])
                self.assertEqual(b["n"], c["bidders"])
                self.assertGreater(c["size_vs_this_job"], 0)
                sh = c["shares"]
                self.assertLessEqual(sh["types"], sh["of_types"])
                self.assertLessEqual(sh["types"], len(c["matched_lines"]))
                self.assertEqual(sh["types"] > 0, bool(c["matched_lines"]))
                self.assertTrue(0 < sh["dollar_share"] <= 1)
                self.assertGreater(c["at_their_prices"]["job"], 0)
                for m in c["matched_lines"]:
                    self.assertGreater(m["their_low_unit"], 0)
                    self.assertTrue(m["their_description"])
                self.assertTrue(c["source"]["document"])
                self.assertLess(c["date"], "2026-03-02")

    def test_fewer_cards_are_explained(self):
        r = estimate(CREEK, bid_date="2020-06-01")
        s = r["similar"]
        self.assertLess(s["n_candidates"], ep.MAX_SIMILAR)
        self.assertEqual(len(s["cards"]), min(s["n_candidates"],
                                              ep.MAX_SIMILAR))
        self.assertIn("Only %d earlier public job" % s["n_candidates"],
                      s["why_fewer"])
        full = estimate(CREEK, bid_date="2026-03-02")["similar"]
        self.assertIsNone(full["why_fewer"])


# ===========================================================================
class Contract(unittest.TestCase):
    """The JSON shape ui/static/app.js draws."""

    def test_estimate_result_shape(self):
        r = estimate(CREEK + [item("Submersible Pump, 15 HP", "EA", 2, 8)])
        for k in ("ok", "inputs", "headline", "context", "alerts",
                  "warnings", "not_covered", "low_confidence", "lines",
                  "similar", "competition", "totals", "engine"):
            self.assertIn(k, r)
        for k in ("p50", "p80", "p50_band", "p80_band", "calibration_n",
                  "p80_multiplier", "covered_only"):
            self.assertIn(k, r["headline"])
        for l in r["lines"]:
            for k in ("row_id", "item_no", "description", "family",
                      "family_label", "unit", "quantity", "status",
                      "unit_price_p50", "amount_p50", "basis", "why",
                      "accuracy", "warnings", "coverage_class",
                      "lowconf_class"):
                self.assertIn(k, l)
            self.assertIn(l["status"], ("priced", "owner_set", "not_covered",
                                        "not_covered_whole", "unpriced"))
            for w in l["warnings"]:
                self.assertIn(w["level"], ("info", "caution", "severe"))
        for a in r["alerts"]:
            self.assertIn(a["level"], ("info", "caution", "severe"))
            self.assertTrue(a["code"] and a["text"])
        self.assertEqual(r["context"]["market"], "municipal")
        json.dumps(app._clean(r), allow_nan=False)

    def test_excluded_lines_are_not_priced(self):
        r = estimate(CREEK + [item("Rock Riprap", "SY", 40, 40,
                                   include=False)])
        self.assertEqual(len(r["lines"]), len(CREEK))

    def test_bad_requests_are_400(self):
        for body in ({"items": []}, {"market": "mars", "items": CREEK},
                     {"items": CREEK, "n_bidders": "many"},
                     {"items": CREEK, "bid_date": "next week"}):
            with self.subTest(body=list(body)):
                st, _ = api("POST", "/api/estimate", body)
                self.assertEqual(st, 400)

    def test_options_and_status(self):
        st, o = api("GET", "/api/options")
        self.assertEqual(st, 200)
        for k in ("owners", "work_types", "families", "units", "discount",
                  "market_accuracy", "item_accuracy"):
            self.assertIn(k, o)
        self.assertTrue(o["owners"]["municipal"])
        st, s = api("GET", "/api/status")
        self.assertTrue(s["ready"])
        self.assertEqual(len(s["estimator"]["md5"]), 32)
        self.assertTrue(s["data"]["latest_letting"])
        self.assertTrue(s["upstream"]["parser_available"])


# ===========================================================================
class Theme(unittest.TestCase):
    """Light and dark themes: every colour is a token defined in both."""

    STATIC = os.path.join(HERE, "static")
    COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|"
                        r"(?<![\w-])(?:white|black|red|green|blue|gr[ae]y|"
                        r"orange|yellow|purple|pink|silver|navy|teal|maroon)"
                        r"(?![\w-])")

    @classmethod
    def setUpClass(cls):
        cls.css = open(os.path.join(cls.STATIC, "style.css"),
                       encoding="utf-8").read()

    def _block(self, sel):
        i = self.css.index(sel + " {")
        return i, self.css.index("}", i)

    def _tokens(self, sel):
        i, j = self._block(sel)
        return dict(re.findall(r"--([\w-]+):\s*([^;]+);", self.css[i:j]))

    def test_no_colour_outside_the_two_token_blocks(self):
        rest = self.css
        for sel in (':root[data-theme="dark"]', ":root"):
            i, j = self._block(sel)
            rest = rest[:i] + rest[j + 1:]
        rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)
        decls = " ".join(re.findall(r"\{([^{}]*)\}", rest))
        self.assertEqual(self.COLOUR.findall(decls), [])

    def test_both_themes_define_the_same_tokens(self):
        light = self._tokens(":root")
        dark = self._tokens(':root[data-theme="dark"]')
        fonts = {"mono", "sans"}
        self.assertEqual(set(light) - fonts, set(dark))
        used = set(re.findall(r"var\(--([\w-]+)\)", self.css))
        self.assertEqual(used - set(light), set())

    def test_scripts_carry_no_colour(self):
        js = open(os.path.join(self.STATIC, "app.js"), encoding="utf-8").read()
        self.assertEqual(re.findall(r"rgba?\(|hsla?\(|#[0-9a-fA-F]{6}\b", js),
                         [])

    def test_theme_is_set_before_first_paint(self):
        html = open(os.path.join(self.STATIC, "index.html"),
                    encoding="utf-8").read()
        head = html[:html.index("</head>")]
        self.assertIn("prefers-color-scheme: dark", head)
        self.assertIn("data-theme", head)
        self.assertIn("try {", head)
        self.assertIn('id="theme-toggle"', html)

    def test_text_contrast_meets_wcag_aa_in_both_themes(self):
        def rgba(h):
            h = h.strip().lstrip("#")
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return [int(h[k:k + 2], 16) / 255 for k in (0, 2, 4)], a

        def over(fg, bg):
            (c, a), (b, _) = rgba(fg), rgba(bg)
            return [a * x + (1 - a) * y for x, y in zip(c, b)]

        def lum(c):
            f = (lambda v: v / 12.92 if v <= 0.03928
                 else ((v + 0.055) / 1.055) ** 2.4)
            r, g, b = (f(v) for v in c)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def ratio(a, b):
            la, lb = lum(a), lum(b)
            return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

        pairs = [("ink", "bg"), ("ink", "panel"), ("ink-dim", "panel-2"),
                 ("ink-faint", "panel"), ("ink-faint", "panel-2"),
                 ("ink-faint", "bg"), ("ink-faint", "sunken"),
                 ("ink-faint", "th"), ("ink-faint", "statusbar"),
                 ("accent", "panel"), ("accent", "panel-2"),
                 ("on-accent", "accent-strong"), ("on-accent", "accent-hover"),
                 ("ok-ink", "ok-bg@panel"), ("warn-ink", "warn-bg@panel"),
                 ("bad-ink", "bad-bg@panel"), ("info-ink", "info-bg@panel"),
                 ("violet-ink", "violet-bg@panel"),
                 ("missing-ink", "missing-bg"), ("bad-ink", "row-bad@panel"),
                 ("ink", "info-bg@panel"), ("warn-ink", "warn-bg@panel-2"),
                 ("bad-ink", "bad-bg@panel-2")]
        for sel in (":root", ':root[data-theme="dark"]'):
            pal = self._tokens(sel)
            for fg, bg in pairs:
                with self.subTest(theme=sel, fg=fg, bg=bg):
                    if "@" in bg:
                        b, base = bg.split("@")
                        bgc = over(pal[b], pal[base])
                    else:
                        bgc = rgba(pal[bg])[0]
                    self.assertGreaterEqual(ratio(rgba(pal[fg])[0], bgc), 4.5)


# ===========================================================================
class ChoiceLists(unittest.TestCase):
    """Owner, work type and market each have an "Other / not sure" path."""

    def test_duplicate_owner_ids_collapse_into_one_entry(self):
        owners = ENG.options()["owners"]["municipal"]
        names = [o["label"] for o in owners]
        self.assertEqual(len(names), len(set(names)))
        cop = [o for o in owners if o["label"] == "Coppell"]
        self.assertEqual(len(cop), 1)
        self.assertEqual(set(cop[0]["ids"]), {"coppell", "legistar/coppell"})
        # the estimator gets the id with the most history
        ctx = ENG.tab.comparables["municipal"].ctx
        n = {i: sum(1 for Lc in ctx.L if Lc["owner"] == i)
             for i in cop[0]["ids"]}
        self.assertEqual(cop[0]["id"], max(n, key=n.get))
        self.assertEqual(cop[0]["lettings"], sum(n.values()))

    def test_other_owner_and_work_type_price_like_blank(self):
        blank = estimate(CREEK, owner="", work_type="")
        other = estimate(CREEK, owner="", work_type="",
                         owner_text="Town of Smallville Utility District",
                         work_type_text="Pond dredging")
        self.assertEqual(other["headline"]["p50"], blank["headline"]["p50"])
        self.assertEqual(other["headline"]["p80"], blank["headline"]["p80"])
        self.assertEqual([c["key"] for c in other["similar"]["cards"]],
                         [c["key"] for c in blank["similar"]["cards"]])
        # an unknown value sent as the id itself is treated the same way
        raw = estimate(CREEK, owner="Town of Smallville Utility District",
                       work_type="Pond dredging")
        self.assertEqual(raw["headline"]["p50"], blank["headline"]["p50"])
        self.assertEqual(raw["inputs"]["owner"], "")
        self.assertEqual(raw["inputs"]["work_type"], "")

    def test_typed_values_reach_the_header_and_the_export(self):
        r = estimate(CREEK, owner="", work_type="",
                     owner_text="Town of Smallville", work_type_text="Pond dredging")
        i = r["inputs"]
        self.assertFalse(i["owner_in_history"])
        self.assertFalse(i["work_type_in_history"])
        self.assertIn("Town of Smallville", i["owner_label"])
        self.assertIn("not in the price history", i["owner_label"])
        self.assertIn("Pond dredging", i["work_type_label"])
        text = exporter.to_csv(app._clean(r)).decode("utf-8-sig")
        self.assertIn("Town of Smallville", text)
        self.assertIn("Pond dredging", text)

    def test_a_typed_known_name_resolves_to_its_history(self):
        r = estimate(CREEK, owner="", owner_text="coppell", work_type="",
                     work_type_text="Creek erosion / streambank")
        self.assertEqual(r["inputs"]["owner"], "legistar/coppell")
        self.assertEqual(r["inputs"]["work_type"], "erosion")
        direct_r = direct(CREEK, owner="legistar/coppell", work_type="erosion")
        self.assertEqual(r["headline"]["p50"], direct_r["expected_low"])

    def test_market_not_sure_is_city_county_and_says_so(self):
        r = estimate(CREEK, market="not_sure")
        self.assertEqual(r["inputs"]["market"], "municipal")
        self.assertTrue(r["inputs"]["market_note"])
        self.assertEqual(r["headline"]["p50"],
                         estimate(CREEK, market="municipal")["headline"]["p50"])


# ===========================================================================
class Export(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.r = estimate(CREEK + [
            item("Submersible Pump, 15 HP", "EA", 2, 8, quote=84000),
            item("Owner's Contingency Allowance", "LS", 1, 9, amount=50000)])

    def test_csv_has_every_block(self):
        st, ct, data, hdr = app.dispatch(
            "POST", "/api/export/csv", {"Content-Type": "application/json"},
            json.dumps({"result": app._clean(self.r)}).encode())
        self.assertEqual(st, 200)
        self.assertIn("attachment", hdr["Content-Disposition"])
        text = data.decode("utf-8-sig")
        for s in ("Expected low bid (P50)", "Recommended OPCC (%s)"
                  % exporter.recommended_basis(self.r).upper(),
                  "Not covered", "Low confidence", "Lines",
                  "Similar past projects", "matched lines", "Competition",
                  "Forward accuracy",
                  "Submersible Pump", "Complete total"):
            self.assertIn(s, text)
        rows = list(csv.reader(io.StringIO(text)))
        self.assertTrue(any(r and r[0] == "2" for r in rows))

    def test_xlsx_opens(self):
        if not exporter.xlsx_available():
            self.skipTest("openpyxl not installed")
        import openpyxl
        st, ct, data, _ = app.dispatch(
            "POST", "/api/export/xlsx", {"Content-Type": "application/json"},
            json.dumps({"result": app._clean(self.r)}).encode())
        self.assertEqual(st, 200)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["Summary", "Not covered",
                                         "Low confidence", "Lines",
                                         "Similar projects", "Matched lines"])

    def test_export_of_nothing_is_refused(self):
        for body in ({}, {"result": {"lines": []}}, {"items": CREEK}):
            st, _ = api("POST", "/api/export/csv", body)
            self.assertEqual(st, 400)


# ===========================================================================
class P70Exports(unittest.TestCase):
    """P70 is a separate figure; saved P80 prices and totals stay intact."""

    def setUp(self):
        self.result = {
            "headline": {"p50": 1000.0, "p70": 1200.0, "p80": 1400.0,
                         "p70_multiplier": 1.2, "p80_multiplier": 1.4,
                         "recommended_basis": "p70", "contingency": {
                             "p70": {"subtotal": 1200.0, "contingency": 0.0}}},
            "lines": [
                {"row_id": "a", "item_no": "1", "description": "Public test item",
                 "unit": "CY", "quantity": 10, "status": "priced",
                 "unit_price_p50": 100.0, "amount_p50": 1000.0},
                {"row_id": "b", "item_no": "2", "description": "Quoted equipment",
                 "unit": "EA", "quantity": 1, "status": "not_covered",
                 "unit_price_p50": None, "amount_p50": None}],
            "not_covered": {"lines": [{"item_no": "2", "description": "Quoted equipment",
                                        "quantity": 1, "unit": "EA", "quote": 500.0}],
                            "complete_total": 1500.0, "complete_total_p70": 1700.0,
                            "complete_total_p80": 1900.0},
            "context": {"label": "Public validation fixture", "lettings": 100,
                        "p50_median_error": 0.2, "p80_under": 0.14,
                        "median_overshoot": 0.45},
        }
        self.ws = {"basis": "p70", "basis_multiplier": 1.2,
                   "contingency_pct": 10, "lines": [
                       {"row_id": "a", "src": "you", "your_unit": 123.45,
                        "src_label": "your price"},
                       {"row_id": "b", "src": "quote", "your_amount": 500.0}]}

    def test_totals_keep_p70_p80_and_engineer_prices_separate(self):
        import worksheet
        t = worksheet.totals(self.result, self.ws)
        self.assertAlmostEqual(t["your_opcc"], 1907.95)
        self.assertEqual(t["p70"], 1200.0)
        self.assertEqual(t["p80"], 1400.0)
        self.assertEqual(t["model_p70"], 1700.0)
        self.assertEqual(t["model_p80"], 1900.0)
        self.assertEqual(t["p70_split"], (1200.0, 0.0))
        self.assertAlmostEqual(t["vs_p70"]["amount"], 207.95)
        self.assertAlmostEqual(t["vs_p80"]["amount"], 7.95)
        self.assertAlmostEqual(t["suggested_contingency_pct"], 20.0)

    def test_workbook_uses_true_p70_and_preserves_saved_p80_basis(self):
        import openpyxl
        import worksheet
        for basis, multiplier in (("p70", 1.2), ("p80", 1.4), ("p50", 1.0)):
            with self.subTest(basis=basis):
                ws = dict(self.ws, basis=basis, basis_multiplier=multiplier)
                wb = openpyxl.load_workbook(io.BytesIO(
                    worksheet.to_xlsx(self.result, ws, None)), data_only=True)
                by = {r[0].value: r for r in wb["Summary"].iter_rows() if r[0].value}
                self.assertIn(basis.upper(), by["Prices start from"][1].value)
                self.assertEqual(by["Recommended OPCC (P70) + not-covered lines"][1].value, 1700.0)
                self.assertEqual(by["P80 baseline + not-covered lines"][1].value, 1900.0)
                self.assertAlmostEqual(by["Project Total"][1].value, 1907.95)
                line = next(r for r in wb["OPCC"].iter_rows()
                            if r[1].value == "Public test item")
                self.assertEqual(line[4].value, 123.45)
                self.assertEqual(line[5].value, 1234.5)
                self.assertIn("independent P70 validation is not yet available",
                              " ".join(str(c.value) for r in wb["Summary"] for c in r if c.value))

    def test_estimate_export_separates_trial_and_baseline_statistics(self):
        blocks = {name: rows for name, _, rows in exporter.sections(self.result)}
        figures = {r[0]: r for r in blocks["Estimate"]}
        self.assertEqual(figures["Recommended OPCC (P70)"][1], 1200.0)
        self.assertEqual(figures["P80 baseline"][1], 1400.0)
        history = str(blocks["Forward accuracy of this market (P50 / P80 baseline)"])
        self.assertIn("P80 under the low bid on 14%", history)
        self.assertNotIn("P70 under", history)
        text = exporter.to_csv(self.result).decode("utf-8-sig")
        self.assertIn("Complete total = P70 + quotes", text)
        self.assertIn("Complete total = P80 + quotes (baseline)", text)

    def test_legacy_result_keeps_p80_recommendation(self):
        import copy
        import worksheet
        old = copy.deepcopy(self.result)
        for key in ("p70", "p70_multiplier", "recommended_basis"):
            old["headline"].pop(key)
        self.assertEqual(exporter.recommended_basis(old), "p80")
        self.assertIn("P80 (recommended OPCC", worksheet.price_basis_label(old, {"basis": "p80"}))
        figures = dict((name, rows) for name, _, rows in exporter.sections(old))["Estimate"]
        self.assertTrue(any(row[0] == "Recommended OPCC (P80)" and row[1] == 1400 for row in figures))

    def test_p70_numeric_validation_refuses_bool_and_infinite_text(self):
        for value in (True, "Infinity", "1e999"):
            for field in ("p70", "p70_multiplier"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(app.ApiError):
                        app._validate_worksheet_export_numbers({"headline": {field: value}}, {})


class HistoryPriceExports(unittest.TestCase):
    """Historical worksheet choices retain their prices and source snapshots."""

    def setUp(self):
        base = P70Exports()
        base.setUp()
        self.result = base.result
        selected = []
        for key, date, median, factor in (
                ("public-a", "2023-04-05", 70.25, 1.1),
                ("public-b", "2024-06-12", 80.12, 1.07),
                ("public-c", "2025-01-10", 90.5, 1.0)):
            year = int(date[:4])
            selected.append({
                "key": key, "project": key.title(), "owner": "Public owner",
                "date": date, "item_no": "10", "item": "Public test item",
                "unit": "CY", "quantity": 10, "n_bids": 4,
                "median_unit": median, "adjusted_median": median * factor,
                "escalation": factor, "match_basis": ["Same unit", "Matching item wording"],
                "source": {"document": key + ".pdf", "url": "https://example.org/" + key},
                "index_evidence": {
                    "status": "available", "factor": factor,
                    "method": "prior_public_matched_bid_index_v1", "market": "municipal",
                    "from_year": year, "target_year": 2025, "as_of": "2025-03-10",
                    "steps": [{"from_year": year, "to_year": 2025,
                               "n_matched": 24, "factor": factor}] if year < 2025 else [],
                    "reason": "", "notes": []}})
        self.start = statistics.median(p["adjusted_median"] for p in selected)
        self.history = {
            "status": "available", "unit_price": self.start, "n_projects": 3,
            "references": selected, "reason": "",
            "evidence": ["3 distinct public projects within 24 months", "Same unit"],
            "method": "recent_matching_bid_medians_v1", "price_basis": "bid_date_adjusted",
            "bid_date": "2025-03-10", "target_year": 2025}
        self.ws = {"basis": "history", "basis_multiplier": None, "round": "off",
                   "contingency_pct": 0, "lines": [
                       {"row_id": "a", "src": "history", "your_unit": self.start,
                        "raw": self.start, "history_price": self.history,
                        "source": "Stale model P70 label"},
                       {"row_id": "b", "src": "quote", "your_amount": 500.0}]}

    def test_history_unit_and_cached_total_are_not_rescaled(self):
        import openpyxl
        import worksheet
        wb = openpyxl.load_workbook(io.BytesIO(
            worksheet.to_xlsx(self.result, self.ws, None)), data_only=True)
        row = next(r for r in wb["OPCC"].iter_rows() if r[1].value == "Public test item")
        self.assertEqual(row[4].value, self.start)
        self.assertAlmostEqual(row[5].value, self.start * 10)
        self.assertIn("Median of 3 matching public projects", row[6].value)
        self.assertNotIn("Stale model P70", row[6].value)
        for p in self.history["references"]:
            self.assertIn(p["date"], row[6].value)
        summary = {r[0].value: r[1].value for r in wb["Summary"].iter_rows() if r[0].value}
        self.assertIn("recent matching public bid medians", summary["Prices start from"])
        self.assertNotIn("P70", summary["Prices start from"])
        self.assertAlmostEqual(summary["Project Total"], self.start * 10 + 500)
        self.assertEqual(summary["Expected low bid (P50) + not-covered lines"], 1500)
        self.assertEqual(summary["Recommended OPCC (P70) + not-covered lines"], 1700)
        self.assertEqual(summary["P80 baseline + not-covered lines"], 1900)
        headers = [c.value for c in wb["Lines"][2]]
        self.assertIn("Model P50 historical accuracy", headers)
        self.assertNotIn("Typical accuracy", headers)
        line = next(r for r in wb["Lines"].iter_rows(min_row=3) if r[1].value == "Public test item")
        self.assertEqual(line[headers.index("Historical starting $/unit")].value, self.start)
        self.assertEqual(line[headers.index("Your $/unit")].value, self.start)
        self.assertIn("within 24 months", line[headers.index("History evidence")].value)

    def test_selected_pay_item_snapshots_survive_missing_or_changed_refs(self):
        import copy
        import worksheet
        selected = self.history["references"]
        same_project_other_item = dict(selected[0], item_no="99", item="Different pay item")
        refs = {"lines": [{"row_id": "a", "picks": [copy.deepcopy(selected[0]),
                                                       same_project_other_item]}]}
        for current, expected_n in ((None, 3), (refs, 4)):
            with self.subTest(references_available=bool(current)):
                _, header, rows = next(s for s in worksheet.sections(self.result, self.ws, current)
                                       if s[0] == "References appendix")
                self.assertEqual(len(rows), expected_n)
                by = [dict(zip(header, row)) for row in rows]
                self.assertEqual([r["Reference use"] for r in by[:3]],
                                 ["Selected history median"] * 3)
                self.assertEqual([r["Reference key"] for r in by[:3]],
                                 [p["key"] for p in selected])
                for original, saved in zip(selected, by):
                    self.assertEqual(saved["Median bid $/unit"], original["median_unit"])
                    self.assertEqual(saved["Inflation-adjusted median $/unit"], original["adjusted_median"])
                    self.assertEqual(saved["Inflation factor"], original["escalation"])
                    self.assertEqual(saved["Adjusted price year"], 2025)
                    self.assertIn("2025-03-10", saved["Index evidence"])
                    self.assertIn("Same unit", saved["Match evidence"])
                    self.assertEqual(saved["source_url"], original["source"]["url"])
                if current:
                    self.assertEqual(by[-1]["Their item no."], "99")
                    self.assertEqual(by[-1]["Reference use"], "Other reference")

    def test_missing_adjustment_stays_blank_with_original_price_auditable(self):
        import worksheet
        self.history.update(status="insufficient", unit_price=None,
                            reason="The historical inflation index is unavailable.")
        self.ws["lines"][0].update(your_unit=None, raw=None)
        p = self.history["references"][0]
        p.update(adjusted_median=None, escalation=None, median_esc=999.0)
        p["index_evidence"].update(status="unavailable", factor=None,
                                   reason="Not enough earlier matched bids.")
        t = worksheet.totals(self.result, self.ws)
        self.assertEqual(t["your_opcc"], 500.0)
        self.assertEqual(t["missing"], ["a"])
        _, header, rows = next(s for s in worksheet.sections(self.result, self.ws, None)
                               if s[0] == "References appendix")
        first = dict(zip(header, rows[0]))
        self.assertIsNone(first["Inflation-adjusted median $/unit"])
        self.assertIsNone(first["Inflation factor"])
        self.assertEqual(first["Median bid $/unit"], 70.25)
        self.assertEqual(first["Reference use"], "History evidence, not selected")
        self.assertIn("index is unavailable", worksheet.price_source(self.ws["lines"][0]))

    def test_rounding_and_engineer_prices_do_not_change_saved_starting_price(self):
        import worksheet
        self.ws["round"] = "up"
        self.ws["lines"][0]["your_unit"] = 90.0
        self.result["lines"] += [
            {"row_id": "c", "description": "Engineer input", "status": "priced",
             "pricing_mode": "engineer", "quantity": 1, "unit_price_p50": 60},
            {"row_id": "d", "description": "Fixed allowance", "status": "owner_set",
             "quantity": 1, "fixed_amount": 25},
            {"row_id": "e", "description": "Hand-entered price", "status": "priced",
             "quantity": 2, "unit_price_p50": 100}]
        self.ws["lines"] += [{"row_id": "c", "src": "", "your_unit": None},
                             {"row_id": "d", "src": "owner-set"},
                             {"row_id": "e", "src": "you", "your_unit": 12.3456}]
        t = worksheet.totals(self.result, self.ws)
        self.assertAlmostEqual(t["your_opcc"], 900 + 500 + 25 + 2 * 12.3456)
        self.assertEqual(t["missing"], ["c"])
        self.assertEqual(self.ws["lines"][0]["history_price"]["unit_price"], self.start)
        for basis in ("p50", "p70", "p80"):
            self.assertIn(basis.upper(), worksheet.price_basis_label(self.result, {"basis": basis}))

    def test_nested_history_numbers_reject_bool_and_nonfinite_text(self):
        for field, value in (("adjusted_median", True), ("factor", "Infinity")):
            with self.subTest(field=field):
                snapshot = {"history_price": {"references": [{field: value}]}}
                with self.assertRaises(app.ApiError) as raised:
                    app._validate_worksheet_export_numbers({}, {"lines": [snapshot]})
                self.assertEqual(raised.exception.status, 400)


class Http(unittest.TestCase):
    """The real server on a socket: upload, classify, estimate, static."""

    @classmethod
    def setUpClass(cls):
        cls.srv = app.make_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _req(self, path, data=None, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_upload_classify_estimate(self):
        text = ("Item No.,Description,Quantity,Unit\r\n"
                "1,Mobilization,1,LS\r\n"
                "2,Unclassified Channel Excavation,\"4,200\",CY\r\n"
                "3,\"Grouted Rock Riprap, 18\"\" thick\",2600,SY\r\n")
        b = "----t%d" % os.getpid()
        body = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                "filename=\"Créek take-off.csv\"\r\nContent-Type: "
                "text/csv\r\n\r\n" % b).encode("utf-8") + \
            text.encode("utf-8") + ("\r\n--%s--\r\n" % b).encode()
        st, _, out = self._req("/api/takeoff/file", body,
                               "multipart/form-data; boundary=" + b)
        self.assertEqual(st, 200, out[:300])
        lines = json.loads(out)["lines"]
        self.assertEqual([l["quantity"] for l in lines], [1.0, 4200.0, 2600.0])
        st, _, out = self._req("/api/estimate", json.dumps(
            {"market": "municipal", "n_bidders": 4, "bid_date": "2026-03-02",
             "items": lines}).encode())
        self.assertEqual(st, 200, out[:300])
        r = json.loads(out)
        self.assertTrue(r["headline"]["p50"] > 0)

    def test_opcc_refused_over_http(self):
        st, _, out = self._req("/api/estimate", json.dumps(
            {"items": CREEK, "n_bidders": 4,
             "engineer_estimate": 1}).encode())
        self.assertEqual(st, 400)

    def test_static_and_traversal(self):
        st, h, out = self._req("/")
        self.assertEqual(st, 200)
        self.assertIn(b"Bid Estimator", out)
        self.assertIn("no-store", h["Cache-Control"])
        for p in ("/../app.py", "/%2e%2e/app.py", "/static/../../src/"):
            st, _, _ = self._req(p)
            self.assertEqual(st, 404, p)
        st, _, _ = self._req("/api/nothing-here")
        self.assertEqual(st, 404)


# ===========================================================================
class Matching(unittest.TestCase):
    """The history search behind the classification picker."""

    def test_scope_modifier_mismatch_is_penalised(self):
        cands = matching.candidates("6 inch Reinforced Concrete Pavement",
                                    "SY", limit=10)
        self.assertTrue(cands)
        self.assertNotIn("remove", cands[0].label.lower())

    def test_size_drives_the_match(self):
        for size in ("8", "24"):
            with self.subTest(size=size):
                cands = matching.candidates("%s inch RCP Storm Sewer" % size,
                                            "LF", limit=6)
                self.assertIn(size, " ".join(c.label.lower()
                                             for c in cands[:3]))

    def test_single_project_item_never_wins(self):
        for q, u in (("Traffic Control", "MO"), ("Block Sod, Bermuda", "SY")):
            with self.subTest(q=q):
                cands = matching.candidates(q, u, limit=8)
                self.assertGreaterEqual(cands[0].n_projects, 3)

    def test_unclassified_never_wins_silently(self):
        best = matching.best("Zorblatt Quantum Widget", "EA")
        self.assertIsNotNone(best)
        self.assertLess(best.score, matching.WEAK_MATCH)

    def test_search_gives_a_family_to_adopt(self):
        st, body = api("GET", "/api/history/search?q=gabion+mattress")
        self.assertEqual(st, 200)
        self.assertTrue(all(c["item_family"] for c in body["candidates"]))

    def test_empty_query_matches_nothing(self):
        self.assertEqual(matching.candidates("", "SY"), [])
        st, body = api("GET", "/api/history/search?q=")
        self.assertEqual(body["candidates"], [])


# ===========================================================================
class TakeoffTemplate(unittest.TestCase):
    """2026-11-21 (user): a blank Excel takeoff to download, fill in and
    paste back (or upload), laid out like the OPCC it becomes ("the same
    format as the output, just not filled in"). It must read back exactly,
    by both paths."""

    # item no., description, units, quantity, total cost (an allowance's
    # printed amount only)
    ROWS = [("101", "Mobilization", "LS", 1, None),
            ("102", "Unclassified Channel Excavation", "CY", 4200, None),
            ("103", "Grouted Rock Riprap, 18 inch", "SY", 2600, None),
            ("104", "Testing Allowance", "LS", 1, 25000),
            ("105", "Owner's Contingency Allowance", "LS", 1, None)]
    WANT = [("101", "Mobilization", "LS", 1.0, None),
            ("102", "Unclassified Channel Excavation", "CY", 4200.0, None),
            ("103", "Grouted Rock Riprap, 18 inch", "SY", 2600.0, None),
            ("104", "Testing Allowance", "LS", 1.0, 25000.0),
            ("105", "Owner's Contingency Allowance", "LS", 1.0, None)]

    @classmethod
    def setUpClass(cls):
        st, ct, cls.data, hdr = app.dispatch(
            "GET", "/api/takeoff/template.xlsx", {}, b"")
        cls.status, cls.ctype, cls.hdr = st, ct, hdr
        cls.dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    @staticmethod
    def got(parse):
        return [(l["item_no"], l["description"], l["unit"], l["quantity"],
                 l["amount"]) for l in parse["lines"]]

    def filled(self):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(self.data))
        ws = wb.active
        first = exporter.TEMPLATE_HEAD_ROW + 1
        for k, (no, desc, unit, qty, amt) in enumerate(self.ROWS):
            for j, v in ((1, no), (2, desc), (3, unit), (4, qty), (6, amt)):
                if v is not None:
                    ws.cell(row=first + k, column=j, value=v)
        return wb

    def test_it_downloads_as_one_sheet_laid_out_like_the_opcc(self):
        import openpyxl
        import worksheet
        self.assertEqual(self.status, 200)
        self.assertIn("spreadsheetml", self.ctype)
        self.assertIn('attachment; filename="takeoff_template.xlsx"',
                      self.hdr["Content-Disposition"])
        wb = openpyxl.load_workbook(io.BytesIO(self.data))
        self.assertEqual(wb.sheetnames, ["Takeoff"])
        ws = wb.active
        # the OPCC's title, project block, heading and columns
        self.assertEqual(ws["A1"].value, worksheet.OPCC_TITLE)
        self.assertEqual([ws["D3"].value, ws["D4"].value, ws["D5"].value],
                         ["DATE:", "PREPARED BY:", "PROJECT NO.:"])
        self.assertEqual(ws["A7"].value, "ESTIMATED QUANTITIES")
        head = exporter.TEMPLATE_HEAD_ROW
        self.assertEqual([c.value for c in ws[head]][:6], exporter.TEMPLATE_HEADER)
        self.assertEqual(exporter.TEMPLATE_HEADER, worksheet.OPCC_HEADER[:6])
        self.assertFalse(ws.sheet_view.showGridLines)
        b = ws.cell(row=head + 5, column=3).border
        self.assertEqual([b.left.style, b.right.style, b.top.style, b.bottom.style], ["thin"] * 4)
        # the OPCC's serif title, gray band and US Letter paper
        self.assertEqual(ws["A1"].font.name, "Times New Roman")
        self.assertEqual(ws["A7"].fill.fgColor.rgb[-6:], "BFBFBF")
        self.assertEqual(str(ws.page_setup.paperSize), "1")
        kinds = sorted(dv.type for dv in ws.data_validations.dataValidation)
        self.assertEqual(kinds, ["decimal", "decimal", "list"])
        self.assertIsNone(app.pm.blocked("GET", "/api/takeoff/template.xlsx"))

    def test_a_filled_template_brings_its_project_block(self):
        """2026-11-21 (user): the template's project name, project no. and
        prepared by head the worksheet and the export without retyping."""
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(self.data))
        ws = wb.active
        ws["A3"] = "City of Example\nCreek Bank Repair"
        ws["F4"] = "AB"
        ws["F5"] = "Bid 26-001"
        head = exporter.TEMPLATE_HEAD_ROW
        for k, (d, u, q) in enumerate((("Mobilization", "LS", 1), ("Grouted Rock Riprap", "SY", 900))):
            ws.cell(row=head + 1 + k, column=1, value=k + 1)
            ws.cell(row=head + 1 + k, column=2, value=d)
            ws.cell(row=head + 1 + k, column=3, value=u)
            ws.cell(row=head + 1 + k, column=4, value=q)
        p = os.path.join(self.dir, "filled.xlsx")
        wb.save(p)
        r = takeoff.from_file(p, "filled.xlsx").to_dict()
        self.assertEqual(len(r["lines"]), 2)
        self.assertEqual(r["project"], {"name": "City of Example \u2013 Creek Bank Repair",
                                        "prepared_by": "AB", "project_no": "Bid 26-001"})
        blank = takeoff.from_file(self._blank(), "t.xlsx").to_dict()
        self.assertEqual(blank["project"], {})            # "(project name)" is not a name

    def _blank(self):
        p = os.path.join(self.dir, "blank2.xlsx")
        with open(p, "wb") as fh:
            fh.write(self.data)
        return p

    def test_blank_reads_as_nothing_not_as_unreadable_rows(self):
        p = os.path.join(self.dir, "blank.xlsx")
        with open(p, "wb") as fh:
            fh.write(self.data)
        r = takeoff.from_file(p, "takeoff_template.xlsx").to_dict()
        self.assertEqual((len(r["lines"]), r["n_unread"]), (0, 0))

    def test_filled_in_reads_back_by_upload_and_by_paste(self):
        wb = self.filled()
        p = os.path.join(self.dir, "filled.xlsx")
        wb.save(p)
        self.assertEqual(self.got(takeoff.from_file(p, "filled.xlsx").to_dict()), self.WANT)
        # what Excel copies: tab-separated, numbers as displayed
        def cell(v, j):
            return "" if v is None else ("{:,.2f}".format(v) if j >= 4 else
                                         "{:,}".format(v) if j == 3 else str(v))
        rows = "\n".join("\t".join(cell(v, j) for j, v in enumerate((no, d, u, q, None, a)))
                         for no, d, u, q, a in self.ROWS)
        paste = "\t".join(exporter.TEMPLATE_HEADER) + "\n" + rows
        self.assertEqual(self.got(takeoff.from_text(paste).to_dict()), self.WANT)
        # select-all of the whole sheet: the title, the project block and the
        # notes beside them come along and are skipped
        ws = wb.active
        last = exporter.TEMPLATE_HEAD_ROW + len(self.ROWS)
        whole = "\n".join("\t".join(cell(c.value, j) if j in (3, 4, 5) and isinstance(c.value, (int, float))
                                     else ("" if c.value is None else str(c.value))
                                     for j, c in enumerate(r[:8]))
                          for r in ws.iter_rows(min_row=1, max_row=last))
        r = takeoff.from_text(whole).to_dict()
        self.assertEqual((self.got(r), r["n_unread"]), (self.WANT, 0))


class TakeoffParsing(unittest.TestCase):
    """ingest/takeoff.py -- unchanged, still the input path."""

    def test_total_rows_are_dropped(self):
        for s in ("TOTAL BASE BID", "Total", "SUBTOTAL ALT #1", "GRAND TOTAL",
                  "BASE BID TOTAL", "Alternate 1 Total", "Bid Schedule",
                  "SCHEDULE OF ITEMS", "Summary", "Part II", "Division 2",
                  "Page 3", "Continued", "Amount in Words"):
            with self.subTest(s=s):
                self.assertTrue(takeoff._is_not_item(s))

    def test_real_items_containing_total_words_are_kept(self):
        """"Total Station Survey Equipment" is a real surveying pay item."""
        for s in ("Total Station Survey Equipment",
                  "Bidder Supplied Fill Material", "Mobilization",
                  "Concrete Sidewalk, 4 inch", "Grouted Rock Riprap, 18 inch",
                  "Clearing and Grubbing"):
            with self.subTest(s=s):
                self.assertFalse(takeoff._is_not_item(s))

    def test_tab_separated_paste(self):
        p = takeoff.from_text(
            "Mobilization\tLS\t1\n"
            "Unclassified Channel Excavation\tCY\t4,200\n"
            "Grouted Rock Riprap, 18 inch\tSY\t2,600\n")
        self.assertEqual(len(p.lines), 3)
        self.assertEqual(p.lines[1].quantity, 4200.0)

    def test_csv_with_item_numbers_and_quoted_commas(self):
        p = takeoff.from_text(
            "Item No.,Description,Quantity,Unit\n"
            "103,Clearing and Grubbing,\"1,745\",SY\n"
            "112,\"12\"\" Gabion Mattress\",108,CY\n")
        self.assertEqual([(l.item_no, l.quantity) for l in p.lines],
                         [("103", 1745.0), ("112", 108.0)])
        self.assertIn("Gabion", p.lines[1].description)

    def test_common_header_layouts_all_parse(self):
        """A bare "Item" column once stole the description role and the
        takeoff came back EMPTY with no error."""
        layouts = [
            ["Item", "Description", "Unit", "Quantity", "Unit Price"],
            ["Item No.", "Description", "Unit", "Est. Quantity", "Unit Price"],
            ["ITEM", "DESCRIPTION", "UNIT", "QTY", "UNIT COST", "AMOUNT"],
            ["No.", "Bid Item Description", "UOM", "Plan Qty"],
            ["#", "Item Description", "U/M", "Quantity"],
            ["Description", "Unit", "Qty"],
        ]
        fill = {"desc": "Unclassified Channel Excavation", "unit": "CY",
                "qty": "4,200", "item_no": "1", "price": "85.00"}
        for header in layouts:
            with self.subTest(header=header):
                _, cmap = takeoff._find_header([header])
                row = [fill.get(next((k for k, v in cmap.items() if v == j),
                                     "?"), "x") for j in range(len(header))]
                p = takeoff._lines_from_grid([header, row], "t.xlsx", "test")
                self.assertEqual(len(p.lines), 1)
                self.assertEqual(p.lines[0].quantity, 4200.0)

    def test_price_column_is_never_read_as_quantity(self):
        grid = [["Item", "Description", "Unit", "Quantity", "Unit Price"],
                ["1", "Unclassified Channel Excavation", "CY", "4200", "85.00"]]
        p = takeoff._lines_from_grid(grid, "t.xlsx", "test")
        self.assertEqual(p.lines[0].quantity, 4200.0)

    def test_missing_unit_or_quantity_is_flagged_not_dropped(self):
        p = takeoff.from_text("Some Mystery Item With No Numbers At All\n")
        self.assertEqual(len(p.lines), 1)
        self.assertEqual(p.lines[0].confidence, "low")

    def test_empty_input(self):
        p = takeoff.from_text("")
        self.assertEqual(p.lines, [])
        self.assertTrue(p.notes)


# ===========================================================================
class OCRGate(unittest.TestCase):
    """Images must fail loudly, never invent line items."""

    def test_reports_unavailable_without_an_engine(self):
        from ingest import ocr
        if upstream.OCR_AVAILABLE:
            self.skipTest("a real OCR engine is installed")
        self.assertFalse(ocr.available())
        with self.assertRaises(ocr.OCRUnavailable):
            ocr.extract_words("nope.png")

    def test_contract_violation_is_caught(self):
        from ingest import ocr
        for bad in ({"text": "x"}, {"x0": 1, "x1": 2, "top": 3}, "a string"):
            with self.subTest(bad=bad):
                with self.assertRaises(ocr.OCRContractError):
                    ocr._coerce(bad)


# ===========================================================================
class DataContract(unittest.TestCase):
    """The tables the history search reads (data_access)."""

    def test_search_tables_load_with_their_columns(self):
        import data_access as da
        for name in ("vocabulary", "txdot_price_book", "price_reference"):
            with self.subTest(table=name):
                df = da.table(name)
                for col in da.TABLES[name].required:
                    self.assertIn(col, df.columns)
                self.assertFalse(str(df.columns[0]).startswith("﻿"))

    def test_missing_required_column_raises_with_a_useful_message(self):
        import data_access as da
        import pandas as pd
        spec = da.TableSpec("fake.csv", required=("a", "b"))
        with self.assertRaises(da.DataContractError) as cm:
            da._check_columns(pd.DataFrame({"a": [1], "c": [2]}), spec,
                              "fake.csv")
        msg = str(cm.exception)
        for s in ("fake.csv", "b", "c"):
            self.assertIn(s, msg)

    def test_estimator_inputs_are_present(self):
        self.assertEqual(ENG.tab.missing, [])
        for mk in ("municipal", "txdot"):
            self.assertIn(mk, ENG.tab.calib)


# ===========================================================================
class IntakeAssist(unittest.TestCase):
    """/api/classify's intake help: low-confidence reasons, the top-3
    history suggestions (same unit), and remembered corrections."""

    @classmethod
    def setUpClass(cls):
        import intake
        matching.index()
        cls._ready = app._matching_state["ready"]
        app._matching_state["ready"] = True
        cls.dir = tempfile.mkdtemp()
        cls._corr = app.CORRECTIONS
        app.CORRECTIONS = intake.Corrections(
            os.path.join(cls.dir, "corrections.json"))
        cls.fam = upstream.item_family(upstream.norm_desc(
            "Grouted Rock Riprap, 18 inch"))

    @classmethod
    def tearDownClass(cls):
        app._matching_state["ready"] = cls._ready
        app.CORRECTIONS = cls._corr
        shutil.rmtree(cls.dir)

    def classify(self, *lines):
        st, r = api("POST", "/api/classify",
                    {"market": "municipal", "lines": list(lines)})
        self.assertEqual(st, 200, r)
        return r["lines"]

    def test_a_garbled_line_is_flagged_with_same_unit_suggestions(self):
        c, = self.classify({"row_id": "a", "description":
                            "Unclassifled Channel Excavatlon", "unit": "CY",
                            "quantity": 4200})
        a = c["intake"]
        self.assertTrue(a["low"])
        codes = [x["code"] for x in a["reasons"]]
        self.assertTrue({"weak_match", "unclassified"} & set(codes), codes)
        self.assertTrue(1 <= len(a["suggestions"]) <= 3)
        self.assertTrue(all(sg["unit"] == "CY" for sg in a["suggestions"]))
        want = upstream.item_family(upstream.norm_desc(
            "Unclassified Channel Excavation"))
        self.assertIn(want, [sg["family"] for sg in a["suggestions"]])

    def test_a_clean_line_is_not_flagged(self):
        c, = self.classify({"row_id": "a", "description":
                            "Unclassified Channel Excavation", "unit": "CY",
                            "quantity": 4200})
        self.assertFalse(c["intake"]["low"], c["intake"])
        self.assertIsNone(c["intake"]["suggestions"])

    def test_missing_unit_and_quantity_are_reasons(self):
        c, = self.classify({"row_id": "a", "description": "Block Sod, Bermuda",
                            "unit": "", "quantity": None})
        codes = [x["code"] for x in c["intake"]["reasons"]]
        self.assertIn("no_unit", codes)
        self.assertIn("no_quantity", codes)
        self.assertTrue(c["intake"]["suggestions"])

    def test_a_remembered_correction_comes_back_tagged(self):
        desc = "Zork widget assembly, type Q"
        st, r = api("POST", "/api/corrections",
                    {"description": desc, "family": self.fam, "unit": "sy",
                     "source": "suggestion"})
        self.assertEqual(st, 200, r)
        c, = self.classify({"row_id": "a", "description": "ZORK  widget "
                            "assembly, Type Q", "unit": "", "quantity": 10})
        self.assertEqual(c["family"], self.fam)
        self.assertEqual(c["remembered"]["family"], self.fam)
        self.assertEqual(c["remembered"]["unit"], "SY")
        self.assertEqual(c["unit"], "SY")
        # not applied over the line's own family, nor when the estimator
        # chose to keep the parser's
        c, = self.classify({"row_id": "a", "description": desc, "unit": "SY",
                            "quantity": 10, "family": "fence"})
        self.assertNotIn("remembered", c)
        c, = self.classify({"row_id": "a", "description": desc, "unit": "SY",
                            "quantity": 10, "keep_parser": True})
        self.assertNotIn("remembered", c)
        st, r = api("POST", "/api/corrections/forget", {"description": desc})
        self.assertTrue(r["forgotten"])
        c, = self.classify({"row_id": "a", "description": desc, "unit": "",
                            "quantity": 10})
        self.assertNotIn("remembered", c)

    def test_only_known_families_can_be_remembered(self):
        st, r = api("POST", "/api/corrections",
                    {"description": "Zork", "family": "not_a_family"})
        self.assertEqual(st, 400)
        st, r = api("POST", "/api/corrections",
                    {"description": "Zork", "family": "other"})
        self.assertEqual(st, 400)

    def test_the_estimate_never_reads_the_memory(self):
        api("POST", "/api/corrections", {"description": CREEK[2]["description"],
                                         "family": "fence"})
        try:
            r = estimate(CREEK)
            self.assertEqual(r["headline"]["p50"], direct(CREEK)["expected_low"])
        finally:
            api("POST", "/api/corrections/forget",
                {"description": CREEK[2]["description"]})


_ROWCOUNT = None


def external_row_counts():
    """{key: number of public rows} and the keys that also have other rows
    -- read from master_panel.csv's two key columns, nothing else."""
    global _ROWCOUNT
    if _ROWCOUNT is None:
        import pandas as pd
        m = pd.read_csv(os.path.join(ep.CLEAN, "master_panel.csv"),
                        encoding="utf-8-sig", dtype=str,
                        keep_default_na=False,
                        usecols=["source_set", "project_key"])
        ext = m.source_set == "external"
        n = m.loc[ext].groupby("project_key").size().to_dict()
        both = set(m.loc[ext, "project_key"]) & set(m.loc[~ext, "project_key"])
        _ROWCOUNT = (n, both)
    return _ROWCOUNT


# ===========================================================================
class FullBidTab(unittest.TestCase):
    """The whole bid of a letting: public rows only, totals as the panel's."""

    @classmethod
    def setUpClass(cls):
        ENG.tabs.ensure()
        pools = [pk for pk in sorted(ENG.panel.lettings) if pk in ENG.panel.totals]
        step = max(1, len(pools) // 12)
        cls.sample = pools[::step][:12]
        hw = [k for k in ENG.panel.lines if "Hunterwood" in k]
        cls.sample += hw[:1]

    def test_the_view_never_contains_an_internal_row(self):
        n_ext, both = external_row_counts()
        public, other = public_keys()
        keys = self.sample + sorted(both)[:10]
        for pk in keys:
            with self.subTest(i=keys.index(pk)):
                rows = ENG.tabs.rows(pk)
                self.assertTrue(rows)
                self.assertTrue(all(r["source_set"] == "external" for r in rows))
                self.assertEqual(len(rows), n_ext[pk])
                v = ENG.tabs.view(pk, ENG)
                self.assertTrue(v["public_only"])
                self.assertEqual(v["n_rows"], n_ext[pk])
        # a letting with no public row has no view and no HTTP answer
        only_other = sorted(other - public)
        self.assertFalse(set(ENG.tabs.keys()) & set(only_other))
        for pk in only_other[:5]:
            self.assertIsNone(ENG.tabs.view(pk, ENG))
            st, _ = api("GET", "/api/letting?key=" + urllib.parse.quote(pk))
            self.assertEqual(st, 404)

    def test_bidder_totals_equal_the_panels(self):
        checked = 0
        for pk in self.sample:
            v = ENG.tabs.view(pk, ENG)
            cl = v["classes"]
            if cl["competition_class"] != ["competitive"] or \
                    cl["project_kind"] != ["construction"] or \
                    "uneven_proposal" in cl["price_basis"]:
                continue
            got = sorted(b["total"] for b in v["bidders"] if b["priced_lines"])
            want = ENG.panel.totals[pk]
            with self.subTest(project=v["project"]):
                self.assertEqual(len(got), len(want))
                for a, b in zip(got, want):
                    self.assertAlmostEqual(a, b, delta=max(0.01, 1e-9 * abs(b)))
                low = [b for b in v["bidders"] if b["low"]]
                self.assertEqual(len(low), 1)
                # extended amounts add up to each bidder's total
                for k, b in enumerate(v["bidders"]):
                    s = sum(ln["prices"][k]["amount"] or 0 for ln in v["lines"]
                            if ln["prices"][k])
                    self.assertAlmostEqual(s, b["total"], delta=0.01)
            checked += 1
        self.assertGreaterEqual(checked, 5)

    def test_http_view_and_csv(self):
        pk = self.sample[0]
        st, v = api("GET", "/api/letting?key=" + urllib.parse.quote(pk))
        self.assertEqual(st, 200)
        self.assertEqual(v["key"], pk)
        self.assertTrue(v["lines"] and v["bidders"])
        st, ct, data, hdr = app.dispatch(
            "GET", "/api/letting/csv?key=" + urllib.parse.quote(pk))
        self.assertEqual(st, 200)
        self.assertIn("text/csv", ct)
        self.assertTrue(hdr["Content-Disposition"].startswith("attachment"))
        text = data.decode("utf-8-sig")
        self.assertIn("Full bid tabulation", text)
        self.assertIn("public rows only", text)
        st, _ = api("GET", "/api/letting?key=no-such-letting")
        self.assertEqual(st, 404)

    def test_engineers_estimate_is_labelled_and_never_an_input(self):
        ee = None
        for pk in sorted(ENG.tabs.keys())[:400]:
            v = ENG.tabs.view(pk, ENG)
            if v and v["engineer_estimate"]:
                ee = v
                break
        if ee is None:
            self.skipTest("no public tab with a printed engineer's estimate "
                          "in the first 400 lettings")
        self.assertIn("never an input", ee["engineer_estimate"]["label"])
        self.assertFalse(any(b["name"].lower().startswith("engineer")
                             for b in ee["bidders"]))
        st, r = api("POST", "/api/estimate", {"market": "municipal",
                                              "items": CREEK,
                                              "engineer_estimate": 1.0})
        self.assertEqual(st, 400)

    def test_the_source_file_endpoint_stays_inside_data_external(self):
        local = None
        for pk in self.sample + sorted(ENG.tabs.keys()):
            src = ENG.sources.get(pk)
            if src["local_path"]:
                local = src
                break
        if local is None and not os.path.isdir(os.path.join(ROOT, "data", "external")):
            self.skipTest("no source documents in this checkout (it runs on the public bundle)")
        self.assertIsNotNone(local)
        st, ct, data, hdr = app.dispatch(
            "GET", "/api/source-file?path=" + urllib.parse.quote(local["local_path"]))
        self.assertEqual(st, 200)
        self.assertEqual(hdr.get("X-Content-Type-Options"), "nosniff")
        if local["local_path"].lower().endswith(".pdf"):
            self.assertEqual(ct, "application/pdf")
            self.assertTrue(hdr["Content-Disposition"].startswith("inline"))
            self.assertTrue(data.startswith(b"%PDF"))
        top = sorted(f for f in os.listdir(os.path.join(ROOT, "data"))
                     if f.lower().endswith((".pdf", ".xlsx")))
        bad = ["external/../" + f for f in top[:3]] + top[:3] + [
            "external/../clean/master_panel.csv", "../ui/.inbox",
            "external/../../ui/.cache", "/etc/passwd",
            "external/" + "../" * 6 + "etc/passwd",
            local["local_path"].replace("external/", "external/./", 1),
            "external/_manifest_for_parsing.csv"]
        for rel in bad:
            with self.subTest(path=rel[:40]):
                st, _, _, _ = app.dispatch(
                    "GET", "/api/source-file?path=" + urllib.parse.quote(rel))
                self.assertEqual(st, 404)


# ===========================================================================
class SourceLinksShown(unittest.TestCase):
    """Every project shown carries its public page when one is recorded."""

    @classmethod
    def setUpClass(cls):
        import lettings
        ENG.load_references()
        # the links the page itself uses: a working copy's harvest manifests,
        # or the public bundle's own links where there are no manifests
        cls.links = lettings.SourceLinks().links()
        keys = [k for k in ENG.panel.lines if "Hunterwood" in k]
        rows = sorted(ENG.panel.lines[keys[0]],
                      key=lambda r: (len(r[0]), r[0])) if keys else []
        cls.req = {"market": "municipal", "n_bidders": 5,
                   "bid_date": "2025-10-09", "work_type": "erosion",
                   "items": [item(r[1], r[3], r[4], r[0]) for r in rows]}

    def _check(self, src, pk):
        f = pk.split("::")[0]
        link = self.links.get(f)
        if link:
            self.assertEqual(src["url"], link["url"])
            self.assertTrue(src["label"].startswith("Source page: " + src["host"]))
        if src["url"]:
            self.assertTrue(src["url"].startswith(("http://", "https://")))
        else:
            self.assertEqual(src["no_url_note"], "no public link recorded")
        return bool(link)

    def test_every_reference_with_a_manifest_url_renders_that_url(self):
        with_url = n = 0
        for it in self.req["items"]:
            rr = ENG.references(self.req, it["row_id"])
            for x in rr.get("references") or []:
                n += 1
                with_url += self._check(x["source"], x["key"])
        self.assertGreater(n, 20)
        self.assertGreater(with_url, 0)

    def test_similar_cards_carry_the_source_link(self):
        r = estimate(CREEK)
        cards = r["similar"]["cards"]
        self.assertTrue(cards)
        for c in cards:
            self._check(c["source"], c["key"])

    def test_the_page_links_only_http_and_its_own_api(self):
        with open(os.path.join(HERE, "static", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        hrefs = re.findall(r"href:\s*([^,}]+)", js)
        self.assertTrue(hrefs)
        for h in hrefs:
            h = h.strip()
            with self.subTest(href=h[:50]):
                # a checked http(s) URL, the UI's own API, an in-page anchor,
                # or a file the page itself just made (an export download)
                self.assertTrue(h in ("url", "mirror") or h.startswith(
                    ("'/api/", "'#'", "URL.createObjectURL(")), h)
        self.assertIn("const url = safeHttp(src.url);", js)
        self.assertRegex(js, r"function safeHttp\(u\) \{ return /\^https\?:")

    def test_exports_carry_source_url_columns(self):
        r = estimate(CREEK)
        req = {"market": "municipal", "n_bidders": 4, "bid_date": "2026-03-02",
               "work_type": "erosion", "items": CREEK}
        r = dict(r, top_references=ENG.top_references(req))
        secs = {name: (head, rows) for name, head, rows in exporter.sections(app._clean(r))}
        for name in ("Similar past projects", "Line references (top 3 per line)"):
            self.assertEqual(secs[name][0][-2:], ["source_url", "mirror_url"])
        urls = [row[-2] for row in secs["Line references (top 3 per line)"][1] if row[-2]]
        # the mirror column is blank or a link into the public data repository
        mirrors = [row[-1] for row in secs["Line references (top 3 per line)"][1] if row[-1]]
        self.assertTrue(all(m.startswith("https://github.com/grintCactus/texas-public-works-bid-tabs/") for m in mirrors))
        self.assertTrue(all(u.startswith(("http://", "https://")) for u in urls))


# ===========================================================================
class NotCoveredHint(unittest.TestCase):
    """2026-11-21 (user): a not-covered line shows, in a small box on hover,
    what past public jobs paid for lines like it -- a rough idea only, never
    priced into P50 / P80."""

    @classmethod
    def setUpClass(cls):
        ENG.load_references()

    def test_classify_and_estimate_carry_the_hint(self):
        got = ENG.classify([{"row_id": "a", "description": "Submersible Pump, 15 HP", "unit": "EA"},
                            {"row_id": "b", "description": "Block Sod, Bermuda", "unit": "SY"}], "municipal")
        got = got if isinstance(got, list) else got["lines"]
        pump, sod = got
        self.assertTrue(pump["coverage_class"])
        h = pump["nc_hint"]
        self.assertEqual(h["unit"], "EA")
        self.assertGreater(h["n_items"], 0)
        self.assertLessEqual(h["p25"], h["median"])
        self.assertLessEqual(h["median"], h["p75"])
        self.assertLessEqual(len(h["examples"]), 3)
        self.assertIsNone(sod["nc_hint"])
        r = estimate(CREEK + [item("Submersible Pump, 15 HP", "EA", 2, 9)])
        nc = [l for l in r["lines"] if l["status"] == "not_covered"]
        self.assertEqual(len(nc), 1)
        self.assertGreater(nc[0]["nc_hint"]["n_items"], 0)
        self.assertTrue(nc[0]["nc_hint"]["escalated"])
        # the hint never prices the line: P50 / P80 leave it out
        self.assertIsNone(nc[0]["amount_p50"])
        self.assertTrue(all("nc_hint" not in l for l in r["lines"] if l["status"] != "not_covered"))


class Worksheet(unittest.TestCase):
    """The OPCC worksheet: the estimator's prices next to the model's --
    output only, saved under ui/.local/worksheets, exported with sources."""

    @classmethod
    def setUpClass(cls):
        ENG.load_references()
        cls.req = {"market": "municipal", "n_bidders": 4, "bid_date": "2026-03-02",
                   "work_type": "erosion", "items": CREEK}
        cls.result = app._clean(ENG.estimate(cls.req))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"BID_UI_WORKSHEETS": self.dir})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir)

    def ws(self, factor=1.0, contingency=None):
        return {"contingency_pct": contingency, "project_name": "Creek test",
                "lines": [{"row_id": l["row_id"],
                           "your_unit": (l["unit_price_p50"] or 0) * factor
                           if l["unit_price_p50"] else None,
                           "src": "you", "src_label": "your price",
                           "note": "test note %s" % l["row_id"]}
                          for l in self.result["lines"]]}

    def test_reference_ranges_are_the_lines_references_escalated(self):
        st, x = api("POST", "/api/worksheet/refs", {"request": self.req})
        self.assertEqual(st, 200, x)
        by = {v["row_id"]: v for v in x["lines"]}
        checked = 0
        for it in CREEK:
            rr = ENG.references(self.req, it["row_id"])
            v = by[it["row_id"]]
            refs = rr.get("references") or []
            self.assertEqual(v["n_refs"], len(refs))
            esc = sorted((r.get("escalated_unit") or r.get("median_unit")) for r in refs)
            if not esc:
                self.assertIsNone(v["range"])
                continue
            self.assertAlmostEqual(v["range"]["low"], esc[0])
            self.assertAlmostEqual(v["range"]["high"], esc[-1])
            self.assertAlmostEqual(v["range"]["median"], statistics.median(esc))
            self.assertEqual(v["latest"]["date"], max(r["date"] for r in refs))
            for pk in v["picks"]:
                self.assertTrue(pk["source"]["url"] == "" or
                                pk["source"]["url"].startswith(("http://", "https://")))
            checked += 1
        self.assertGreaterEqual(checked, 4)

    def test_totals_are_recomputed_on_the_server(self):
        import worksheet
        t = worksheet.totals(self.result, self.ws(1.1, 5))
        want = sum(l["unit_price_p50"] * 1.1 * l["quantity"]
                   for l in self.result["lines"] if l["unit_price_p50"])
        self.assertAlmostEqual(t["subtotal"], want, places=4)
        self.assertAlmostEqual(t["your_opcc"], want * 1.05, places=4)
        self.assertAlmostEqual(t["vs_p50"]["amount"],
                               want * 1.05 - self.result["headline"]["p50"], places=4)
        self.assertAlmostEqual(t["suggested_contingency_pct"], 100 * (
            self.result["headline"][exporter.recommended_basis(self.result)]
            / self.result["headline"]["p50"] - 1))

    def test_a_contingency_share_follows_your_own_line_amounts(self):
        """2026-11-21: a contingency line given as a share (picked from a
        list) sits below the subtotal and is that share of YOUR subtotal
        (every other line, allowances included); in the model's P50 it is the
        share of the model's subtotal. Subtotal, contingency, total."""
        import worksheet
        cont = item("Owner's Contingency Allowance", "LS", 1, 99)
        cont["amount_pct"] = 15
        allow = item("Testing Allowance", "LS", 1, 98, amount=25000.0)
        req = dict(self.req, items=CREEK + [allow, cont])
        res = app._clean(ENG.estimate(req))
        ln = res["lines"][-1]
        self.assertEqual(ln["status"], "owner_set")
        self.assertTrue(ln["below_subtotal"])
        self.assertFalse(res["lines"][-2]["below_subtotal"])     # the allowance
        self.assertEqual(ln["amount_from_pct"]["pct"], 15.0)
        for factor in (1.0, 1.3):
            with self.subTest(factor=factor):
                ws = {"contingency_pct": 5, "lines": [
                    {"row_id": l["row_id"], "src": "you",
                     "your_unit": (l["unit_price_p50"] or 0) * factor
                     if l["unit_price_p50"] else None}
                    for l in res["lines"]]}
                t = worksheet.totals(res, ws)
                items_ = sum(a for l, w, a in t["rows"] if a is not None)
                self.assertNotIn(ln["row_id"], [l["row_id"] for l, w, a in t["rows"]])
                (bl, bw, mine), = t["below"]
                self.assertEqual(bl["row_id"], ln["row_id"])
                self.assertAlmostEqual(t["subtotal"], items_, places=4)
                self.assertAlmostEqual(mine, 0.15 * t["subtotal"], places=4)
                self.assertAlmostEqual(t["schedule_contingency"], mine, places=4)
                self.assertAlmostEqual(t["contingency"], 0.05 * t["subtotal"], places=4)
                self.assertAlmostEqual(t["your_opcc"], t["subtotal"] * 1.20, places=4)
                self.assertAlmostEqual(t["owner_set"], 25000.0, places=4)
        # the export shows SUBTOTAL, the contingency, TOTAL below the items
        body = {"result": res, "request": req, "worksheet": ws}
        st, ct, data, hdr = app.dispatch("POST", "/api/worksheet/export/csv",
                                         {"Content-Type": "application/json"},
                                         json.dumps(body).encode())
        self.assertEqual(st, 200)
        text = data.decode("utf-8-sig")
        a = text.index(",SUBTOTAL,")
        b = text.index("15% of the subtotal", a)
        c = text.index(",PROJECT TOTAL,", a)
        self.assertTrue(a < b < c, (a, b, c))
        self.assertIn("Additional construction contingency (5% of the subtotal)", text)
        # and the deliverable sheet: Sub-Total, the contingency line, the
        # additional construction contingency, Project Total
        a = text.index(",Sub-Total,")
        b = text.index("(15% of the sub-total)", a)
        c = text.index(",Additional Construction Contingency (5%),", a)
        d = text.index(",Project Total,", a)
        self.assertTrue(a < b < c < d, (a, b, c, d))
        # a fixed owner-set amount stays at face value, below the subtotal too
        fixed = dict(cont, amount=50000.0)
        fixed.pop("amount_pct")
        res2 = app._clean(ENG.estimate(dict(self.req, items=CREEK + [fixed])))
        t2 = worksheet.totals(res2, {"lines": []})
        self.assertEqual([a for l, w, a in t2["below"]], [50000.0])

    def test_export_has_prices_notes_totals_and_sourced_references(self):
        body = {"result": self.result, "request": self.req, "worksheet": self.ws(1.0, 10)}
        st, ct, data, hdr = app.dispatch("POST", "/api/worksheet/export/csv",
                                         {"Content-Type": "application/json"},
                                         json.dumps(body).encode())
        self.assertEqual(st, 200)
        text = data.decode("utf-8-sig")
        for s_ in ("YOUR OPCC", "Model P50", "Model P80", "Construction contingency %",
                   "test note r1", "References appendix", "source_url",
                   "output only"):
            self.assertIn(s_, text)
        self.assertIn("http", text)
        st, ct, data, hdr = app.dispatch("POST", "/api/worksheet/export/xlsx",
                                         {"Content-Type": "application/json"},
                                         json.dumps(body).encode())
        self.assertEqual(st, 200)
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["OPCC", "Summary", "Lines", "References"])
        head = [c.value for c in wb["References"][2]]
        self.assertEqual(head[-2:], ["source_url", "mirror_url"])
        # the first sheet is the deliverable, laid out as an OPCC is (user,
        # 2026-11-21): a title and project block, then the items with their
        # unit cost source, Sub-Total, Construction Contingency and Project
        # Total as live formulas, and a blank interim-review block. Generic:
        # no firm and no names.
        import worksheet
        ws = dict(self.ws(1.0, 10), prepared_by="AB", project_no="X-1")
        t = worksheet.totals(self.result, ws)
        sh = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, ws, None)))["OPCC"]
        self.assertEqual(sh["A1"].value, worksheet.OPCC_TITLE)
        self.assertEqual((sh["D4"].value, sh["F4"].value), ("PREPARED BY:", "AB"))
        self.assertEqual((sh["D5"].value, sh["F5"].value), ("PROJECT NO.:", "X-1"))
        # printed: columns A-F on a portrait page; G-H (date, source) are notes
        self.assertIn("$F$", str(sh.print_area))
        self.assertEqual(sh.page_setup.orientation, "portrait")
        rows = {c.row: [x.value for x in sh[c.row]] for c in sh["B"] if c.value is not None}
        head = next(r for r, v in rows.items() if v[:len(worksheet.OPCC_HEADER)] == worksheet.OPCC_HEADER)
        items_ = [r for r in range(head + 1, head + 1 + len(t["rows"]))]
        self.assertEqual([sh.cell(row=r, column=2).value for r in items_],
                         [l["description"] for l, w, a in t["rows"]])
        total = 0.0
        for r in items_:
            f = sh.cell(row=r, column=6).value
            if isinstance(f, str):
                self.assertEqual(f, "=D%d*E%d" % (r, r))
                total += sh.cell(row=r, column=4).value * sh.cell(row=r, column=5).value
            elif f is not None:
                total += f
        self.assertAlmostEqual(total, t["subtotal"], places=4)
        lab = {v[1]: r for r, v in rows.items()}
        sub, cc, pt = lab["Sub-Total"], lab["Construction Contingency (10%)"], lab["Project Total"]
        self.assertTrue(sub < cc < pt)
        self.assertEqual(sh.cell(row=sub, column=6).value, "=SUM(F%d:F%d)" % (items_[0], items_[-1]))
        self.assertEqual(sh.cell(row=cc, column=6).value, "=F%d*0.1" % sub)
        self.assertEqual(sh.cell(row=pt, column=6).value, "=F%d+F%d" % (sub, cc))
        # each line's note shows on the OPCC sheet itself, beside its source
        # (user, 2026-11-21: "I added notes and the export showed nothing")
        # the note sits beside the source, outside the printed OPCC (user,
        # 2026-11-21: "notes go where the cost source is, outside the table")
        self.assertEqual(sh.cell(row=head, column=8).value, "Notes")
        self.assertNotIn("Date", worksheet.OPCC_HEADER)        # no date column (user, 2026-11-21)
        self.assertTrue(str(sh.print_area).startswith("'OPCC'!$A$1:$F$"))
        noted = {sh.cell(row=r, column=2).value: sh.cell(row=r, column=8).value for r in items_}
        # the team's look: a heavy frame and interim box, a gray ESTIMATED QUANTITIES band
        self.assertEqual(sh.cell(row=items_[0], column=1).border.left.style, "medium")
        self.assertEqual(sh.cell(row=items_[0], column=6).border.right.style, "medium")
        self.assertEqual(sh["A7"].fill.fgColor.rgb[-6:], "BFBFBF")
        self.assertEqual(noted[t["rows"][0][0]["description"]], "test note %s" % t["rows"][0][0]["row_id"])
        # the interim-review box sits at the bottom right (C-F), below the
        # Project Total, as the team's OPCC places it
        box = next(c.row for c in sh["C"] if c.value == "INTERIM REVIEW ONLY")
        self.assertGreater(box, pt)
        self.assertIn("C%d:F%d" % (box, box), [str(m) for m in sh.merged_cells.ranges])
        self.assertEqual(sh.cell(row=box, column=3).border.top.style, "medium")
        # from Units to Total Cost, line for line as on the team's printout:
        # left-aligned, close together, with "Company" and the firm
        # registration no. to fill in (user, 2026-11-21); no names
        nb = len(worksheet.INTERIM_LINES)
        self.assertEqual([sh.cell(row=box + k, column=3).value for k in range(nb)],
                         list(worksheet.INTERIM_LINES))
        self.assertIn("Company", worksheet.INTERIM_LINES)
        self.assertTrue(any(x.startswith("TBPE Firm Registration No.") for x in worksheet.INTERIM_LINES))
        self.assertEqual({sh.cell(row=box + k, column=3).alignment.horizontal for k in range(nb)}, {"left"})
        self.assertEqual(sh.cell(row=box + nb - 1, column=6).border.bottom.style, "medium")
        self.assertTrue(str(sh.print_area).endswith("$F$%d" % (box + nb - 1)))   # the box closes the frame
        # every formula carries its result too, so a viewer that does not
        # calculate (Quick Look, phone and mail previews) shows the totals,
        # not $0 (user, 2026-11-21: the export was unreadable)
        data = worksheet.to_xlsx(self.result, ws, None)
        vals = openpyxl.load_workbook(io.BytesIO(data), data_only=True)["OPCC"]
        self.assertAlmostEqual(vals.cell(row=sub, column=6).value, t["subtotal"], places=4)
        self.assertAlmostEqual(vals.cell(row=cc, column=6).value, t["contingency"], places=4)
        self.assertAlmostEqual(vals.cell(row=pt, column=6).value, t["your_opcc"], places=4)
        for r in items_:
            if isinstance(sh.cell(row=r, column=6).value, str):
                self.assertAlmostEqual(vals.cell(row=r, column=6).value,
                                       sh.cell(row=r, column=4).value * sh.cell(row=r, column=5).value)
        # the Summary is one page in the OPCC's style (2026-11-21: a plain
        # list read as unusable in Google Sheets): your OPCC, then the model
        summ = openpyxl.load_workbook(io.BytesIO(data))["Summary"]
        self.assertEqual(summ["A1"].value, "OPCC SUMMARY")
        self.assertFalse(summ.sheet_view.showGridLines)
        by = {str(r[0].value): r for r in summ.iter_rows() if r[0].value}
        self.assertAlmostEqual(by["Project Total"][1].value, t["your_opcc"], places=4)
        self.assertAlmostEqual(by["Construction Contingency (10%)"][1].value, t["contingency"], places=4)
        self.assertEqual(by["Project Total"][1].number_format, '"$"#,##0')
        rec = exporter.recommended_basis(self.result)
        vs = by["Recommended OPCC (%s)" % rec.upper()]
        self.assertAlmostEqual(vs[2].value, t["vs_" + rec]["amount"], places=4)
        self.assertAlmostEqual(vs[3].value, t["vs_" + rec]["ratio"], places=6)
        self.assertEqual(vs[3].number_format, "+0.0%;-0.0%;0.0%")
        # it prints as the team's printout (user, 2026-11-21: "this feel", on
        # Letter): no lines inside the table or under the totals, serif
        # headings, Letter paper; the gridlines show the cells on screen only
        op = openpyxl.load_workbook(io.BytesIO(data))["OPCC"]
        self.assertTrue(op.sheet_view.showGridLines)
        self.assertFalse(op.print_options.gridLines)
        self.assertEqual(str(op.page_setup.paperSize), "1")           # US Letter
        for name_ in openpyxl.load_workbook(io.BytesIO(data)).sheetnames:
            self.assertEqual(str(openpyxl.load_workbook(io.BytesIO(data))[name_].page_setup.paperSize), "1")
        b = op.cell(row=items_[0], column=3).border
        self.assertEqual([b.left.style, b.right.style, b.top.style, b.bottom.style], [None] * 4)
        self.assertIsNone(op.cell(row=sub, column=6).border.top.style)
        self.assertIsNone(op.cell(row=pt, column=6).border.bottom.style)
        self.assertEqual(op["A1"].font.name, "Times New Roman")
        self.assertEqual(op["A3"].font.name, "Times New Roman")
        self.assertEqual(op["D4"].font.name, "Times New Roman")
        self.assertEqual(op.cell(row=items_[0], column=2).font.name, "Arial")
        # the references: each past project's own item as it was bid -- its
        # words, unit, quantity and prices (user, 2026-11-21)
        refs = app.ENGINE.worksheet_refs(app._plain_request(self.req))
        wb_r = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, ws, refs)))["References"]
        hd = [c.value for c in wb_r[2]]
        for col in ("Project", "Their item (as bid)", "Their unit", "Their quantity",
                    "Low bid $/unit", "Average bid $/unit", "Median bid $/unit", "Bids"):
            self.assertIn(col, hd)
        self.assertEqual(hd[-2:], ["source_url", "mirror_url"])
        body = [[c.value for c in r] for r in wb_r.iter_rows(min_row=3)]
        self.assertTrue(body)
        ix = {h: k for k, h in enumerate(hd)}
        pick0 = next(x for x in refs["lines"] if x["picks"])["picks"][0]
        row0 = next(r for r in body if r[ix["Project"]] == pick0["project"])
        self.assertEqual(row0[ix["Their item (as bid)"]], pick0["item"])
        self.assertAlmostEqual(row0[ix["Their quantity"]], pick0["quantity"])
        self.assertTrue(all(r[ix["Their item (as bid)"]] for r in body))
        # 0%: the contingency row stays, at $0 (as on the page and in the
        # usual OPCC layout)
        ws0 = dict(ws, contingency_pct=0)
        sh0 = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, ws0, None)))["OPCC"]
        c0 = next(c.row for c in sh0["B"] if c.value == "Construction Contingency (0%)")
        self.assertEqual(sh0.cell(row=c0, column=6).value[:2], "=F")
        v0 = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, ws0, None)), data_only=True)["OPCC"]
        self.assertEqual(v0.cell(row=c0, column=6).value, 0.0)
        # a long description wraps, and its row is given the height it needs
        # (a viewer that does not size rows would cut it off)
        long_ = dict(self.result, lines=[dict(self.result["lines"][0], description=" ".join(
            ["Remove and dispose of existing concrete channel lining, including saw cutting"] * 3))]
            + self.result["lines"][1:])
        sh3 = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(long_, ws, None)))["OPCC"]
        r3 = next(c.row for c in sh3["B"] if str(c.value or "").startswith("Remove and dispose"))
        self.assertTrue(sh3.cell(row=r3, column=2).alignment.wrap_text)
        self.assertGreaterEqual(sh3.row_dimensions[r3].height, 12.75 * 3)
        self.assertEqual(worksheet.text_lines("Short", 62), 1)
        self.assertGreater(worksheet.text_lines("W" * 200, 62), 2)
        # a description that looks like a formula stays text
        bad = dict(self.result, lines=[dict(self.result["lines"][0], description="=HYPERLINK(1)")]
                   + self.result["lines"][1:])
        wb2 = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(bad, ws, None)))
        cell = next(c for c in wb2["OPCC"]["B"] if c.value == "=HYPERLINK(1)")
        self.assertEqual(cell.data_type, "s")

    def test_the_pdf_is_the_opcc_as_printed(self):
        """2026-11-21 (user): a PDF beside the Excel, "the Excel's printed
        first page": title, project block, the items, Sub-Total, the
        contingency, Project Total, the interim-review box, page numbers."""
        import pdfplumber
        ws = dict(self.ws(1.0, 10), prepared_by="AB", project_no="X-1")
        body = {"result": self.result, "request": self.req, "worksheet": ws}
        st, ct, data, hdr = app.dispatch("POST", "/api/worksheet/export/pdf",
                                         {"Content-Type": "application/json"},
                                         json.dumps(body).encode())
        self.assertEqual((st, ct), (200, "application/pdf"))
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(".pdf", hdr["Content-Disposition"])
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            n = len(pdf.pages)
        import worksheet
        t = worksheet.totals(self.result, ws)
        for s_ in (worksheet.OPCC_TITLE, "Creek test", "PREPARED BY:", "AB", "X-1",
                   "ESTIMATED QUANTITIES", "Sub-Total", "Construction Contingency (10%)",
                   "Project Total", "$" + format(round(t["your_opcc"]), ","),
                   "INTERIM REVIEW ONLY", "Page 1 of %d" % n):
            self.assertIn(s_, text)
        for l, w, a in t["rows"]:
            self.assertIn(l["description"][:20], text)
        self.assertNotIn("Unit Cost Source", text)          # working notes stay out
        # the date is the user's own day, sent by the page (the server is on UTC)
        with pdfplumber.open(io.BytesIO(worksheet.to_pdf(self.result, dict(ws, today="2026-03-02")))) as pdf:
            self.assertIn("03/02/2026", pdf.pages[0].extract_text())
        import openpyxl
        sh_d = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, dict(ws, today="2026-03-02"), None)))["OPCC"]
        self.assertEqual(str(sh_d["F3"].value)[:10], "2026-03-02")
        self.assertEqual(worksheet._today({"today": "not a date"}), __import__("datetime").date.today())
        # a line with no price says so on paper, never a silent blank read as $0
        ws_np = dict(ws, lines=[dict(x, your_unit=None) if k == 0 else x for k, x in enumerate(ws["lines"])])
        pdf_np = worksheet.to_pdf(self.result, ws_np)
        with pdfplumber.open(io.BytesIO(pdf_np)) as pdf:
            self.assertIn("No price yet", pdf.pages[0].extract_text())
        import openpyxl
        sh_np = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(self.result, ws_np, None)))["OPCC"]
        first_desc = worksheet.totals(self.result, ws_np)["rows"][0][0]["description"]
        r_np = next(c.row for c in sh_np["B"] if c.value == first_desc)
        self.assertEqual(sh_np.cell(row=r_np, column=5).value, "No price yet")
        self.assertIsNone(sh_np.cell(row=r_np, column=6).value)
        # US Letter, and as on the printout no lines inside the table: only
        # the frame, the gray band and the interim box are drawn as boxes
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            p0 = pdf.pages[0]
            self.assertEqual((round(p0.width), round(p0.height)), (612, 792))
            # drawn lines: the frame and the interim box (stroked), the title
            # rule; the gray band is the one colored fill (cells are white)
            stroked = [r for r in p0.rects if r.get("stroke")]
            colored = [r for r in p0.rects if r.get("fill")
                       and tuple(r.get("non_stroking_color") or (1, 1, 1)) != (1.0, 1.0, 1.0)]
            self.assertLessEqual(len(stroked), 2)
            self.assertLessEqual(len(p0.lines), 1)
            self.assertEqual(len(colored), 1)
            fonts = {ch["fontname"] for ch in p0.chars}
            words = p0.extract_words()
            last = pdf.pages[-1]
            rects = last.rects
            lwords = last.extract_words()
        self.assertTrue(any("Times" in f for f in fonts))
        # the interim box runs from the Units column to Total Cost, about
        # twice as wide as tall, its lines left-aligned (user, 2026-11-21)
        for s_ in ("Document Incomplete: Not Intended", "P.E. Serial No", "Company",
                   "TBPE Firm Registration No."):
            self.assertIn(s_, text)
        units = next(w for w in words if w["text"] == "Units")
        title = next(w for w in lwords if w["text"] == "INTERIM")
        box = next(r for r in rects if r["x0"] < title["x0"] < r["x1"] and r["top"] < title["top"] < r["bottom"])
        self.assertTrue(units["x0"] - 38 <= box["x0"] <= units["x0"])
        self.assertTrue(1.6 <= box["width"] / box["height"] <= 2.3)
        self.assertLess(title["x0"] - box["x0"], 10)                 # left-aligned, not centred

    def test_save_list_and_reopen(self):
        doc = {"request": self.req, "worksheet": self.ws(1.2), "totals": {"your_opcc": 1.0}}
        st, r = api("POST", "/api/worksheet/save", {"name": "Creek / test 1", "worksheet": doc})
        self.assertEqual(st, 200, r)
        self.assertEqual(os.listdir(self.dir), [r["file"]])
        st, lst = api("GET", "/api/worksheet/list")
        self.assertEqual([w["file"] for w in lst["worksheets"]], [r["file"]])
        st, back = api("GET", "/api/worksheet/load?file=" + urllib.parse.quote(r["file"]))
        self.assertEqual(st, 200)
        self.assertEqual(back["worksheet"]["lines"], doc["worksheet"]["lines"])
        self.assertEqual(back["request"]["items"], self.req["items"])
        for bad in ("../x.json", "/etc/passwd", "a/b.json", "x.txt", "..json", ""):
            st, _ = api("GET", "/api/worksheet/load?file=" + urllib.parse.quote(bad))
            self.assertIn(st, (400, 404), bad)

    def test_the_default_folder_is_local_and_excluded_from_git(self):
        import worksheet
        self.assertEqual(os.path.relpath(worksheet.DEFAULT_DIR, ROOT),
                         os.path.join("ui", ".local", "worksheets"))
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("ui/.local/", fh.read().split())

    def test_your_prices_are_output_only(self):
        """HARD RULE: what the estimator picks never feeds back into the
        estimator, its tables, training or calibration."""
        clean = os.path.join(ROOT, "data", "clean")
        before = {f: os.stat(os.path.join(clean, f)).st_mtime_ns
                  for f in os.listdir(clean)}
        tab_id = id(ENG.tab)
        p50 = ENG.estimate(self.req)["headline"]["p50"]
        wild = self.ws(100.0, 500)
        for kind in ("csv", "xlsx"):
            app.dispatch("POST", "/api/worksheet/export/" + kind,
                         {"Content-Type": "application/json"},
                         json.dumps({"result": self.result, "request": self.req,
                                     "worksheet": wild}).encode())
        api("POST", "/api/worksheet/save", {"name": "wild", "worksheet": {
            "request": self.req, "worksheet": wild}})
        api("POST", "/api/worksheet/refs", {"request": self.req})
        # the estimate is unchanged, the tables are the same objects, and
        # nothing under data/clean was touched
        self.assertEqual(ENG.estimate(self.req)["headline"]["p50"], p50)
        self.assertEqual(id(ENG.tab), tab_id)
        after = {f: os.stat(os.path.join(clean, f)).st_mtime_ns for f in os.listdir(clean)}
        self.assertEqual(before, after)
        self.assertEqual(os.listdir(self.dir), ["wild.json"])
        # a request carrying worksheet prices is refused, not read
        for bad in ({"worksheet": wild}, {"your_opcc": 1e6}, {"contingency_pct": 10}):
            st, r = api("POST", "/api/estimate", dict(self.req, **bad))
            self.assertEqual(st, 400, bad)
        items = [dict(CREEK[0], your_unit=1e6)] + CREEK[1:]
        st, r = api("POST", "/api/estimate", dict(self.req, items=items))
        self.assertEqual(st, 400)
        self.assertIn("output only", r["detail"])
        # the page never puts them into an estimate request
        with open(os.path.join(HERE, "static", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        i = js.index("function estimateRequest(")
        body = js[i:js.index("\n}\n", i)]
        for k in ("S.ws", "your_unit", "your_amount", "contingency", "worksheet"):
            self.assertNotIn(k, body)
        # and no module that loads or prices reads a worksheet
        for mod in ("engine_production.py", "matching.py", "data_access.py",
                    "upstream.py", "lettings.py"):
            with open(os.path.join(HERE, mod), encoding="utf-8") as fh:
                self.assertNotIn("worksheet", fh.read().lower().replace(
                    "worksheet_refs", "").replace("opcc worksheet", ""), mod)


# ===========================================================================
def _js():
    with open(os.path.join(HERE, "static", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def _js_function(js, name):
    i = js.index("function %s(" % name)
    return js[i:js.index("\n}\n", i)]


class QaRegressions20261120(unittest.TestCase):
    """The bugs found by the end-to-end QA pass of 2026-11-20
    (reports/ui_qa_2026-11-20.md), one test each."""

    @classmethod
    def setUpClass(cls):
        import intake
        matching.index()
        cls._ready = app._matching_state["ready"]
        app._matching_state["ready"] = True
        cls.dir = tempfile.mkdtemp()
        cls._corr = app.CORRECTIONS
        app.CORRECTIONS = intake.Corrections(
            os.path.join(cls.dir, "corrections.json"))
        cls.js = _js()

    @classmethod
    def tearDownClass(cls):
        app._matching_state["ready"] = cls._ready
        app.CORRECTIONS = cls._corr
        shutil.rmtree(cls.dir)

    def classify(self, *lines):
        st, r = api("POST", "/api/classify",
                    {"market": "municipal", "lines": list(lines)})
        self.assertEqual(st, 200, r)
        return r["lines"]

    # -- server -------------------------------------------------------------
    def test_a_malformed_items_list_is_a_400_not_a_500(self):
        for items in (["x"], [1, 2], "abc", {"a": 1}):
            st, r = api("POST", "/api/estimate", {"items": items, "n_bidders": 4})
            self.assertEqual(st, 400, (items, r))
            self.assertIn("items", r["detail"])

    def test_confirming_the_parsers_family_is_remembered_too(self):
        """A suggestion picked for a weak-match line whose family is the
        parser's own was saved but never applied: the line came back
        flagged 'check: weak match' on the next run."""
        line = {"row_id": "a", "description": "Remove Ex. Aerial Crossing",
                "unit": "LS", "quantity": 1}
        c, = self.classify(line)
        self.assertIn("weak_match", [x["code"] for x in c["intake"]["reasons"]])
        st, r = api("POST", "/api/corrections", {
            "description": line["description"], "family": c["family_parser"],
            "source": "suggestion"})
        self.assertEqual(st, 200, r)
        try:
            c2, = self.classify(line)
            self.assertEqual(c2["remembered"]["family"], c["family_parser"])
            self.assertEqual(c2["family"], c["family_parser"])
            self.assertFalse(c2["intake"]["low"], c2["intake"])
        finally:
            api("POST", "/api/corrections/forget", {"description": line["description"]})
        c3, = self.classify(line)
        self.assertNotIn("remembered", c3)

    def test_an_odd_quantity_is_checked_without_family_suggestions(self):
        c, = self.classify({"row_id": "a", "description": "Mobilization",
                            "unit": "LS", "quantity": 5200})
        self.assertEqual([x["code"] for x in c["intake"]["reasons"]], ["odd_quantity"])
        self.assertIsNone(c["intake"]["suggestions"])
        c, = self.classify({"row_id": "a", "description": "Mobilization",
                            "unit": "LS", "quantity": 1})
        self.assertFalse(c["intake"]["low"])

    def test_exports_never_hold_a_live_formula(self):
        import openpyxl
        items = [item(d, "SY", 10, k) for k, d in enumerate(
            ['=HYPERLINK("http://example.com","x")', "+1 Rock Riprap",
             "-Remove fence", "@SUM(A1)", "Grouted Rock Riprap, 18 inch"], 1)]
        req = {"market": "municipal", "n_bidders": 4, "bid_date": "2026-03-02",
               "items": items}
        result = app._clean(ENG.estimate(req))
        ws = {"lines": [{"row_id": l["row_id"], "your_unit": 5.0, "src": "you",
                         "note": "=1+1 note"} for l in result["lines"]]}
        for path, body in (("/api/export/", {"result": result, "request": req}),
                           ("/api/worksheet/export/", {"result": result, "request": req,
                                                       "worksheet": ws})):
            st, ct, data, _ = app.dispatch("POST", path + "xlsx",
                                           {"Content-Type": "application/json"},
                                           json.dumps(body).encode())
            self.assertEqual(st, 200)
            wb = openpyxl.load_workbook(io.BytesIO(data))
            # the only formulas are the OPCC sheet's own totals (2026-11-21:
            # Total Cost = Quantity x Unit Cost, Sub-Total, contingency,
            # Project Total), written by worksheet.py in column F; no text
            # from a description or a note ever becomes one
            own = re.compile(r"=(D\d+\*E\d+|SUM\(F\d+:F\d+\)|F\d+\*[0-9.e-]+|F\d+(\+F\d+)*|0)")
            formulas = [(sh.title, c.coordinate, c.value) for sh in wb.worksheets
                        for row in sh.iter_rows() for c in row if c.data_type == "f"]
            stray = [f for f in formulas if not (f[0] == "OPCC" and f[1].startswith("F")
                                                 and own.fullmatch(str(f[2])))]
            self.assertEqual(stray, [], path)
            if path == "/api/worksheet/export/":
                self.assertTrue(formulas)
                texts = [c.value for row in wb["OPCC"].iter_rows() for c in row
                         if isinstance(c.value, str) and c.value.startswith(("=", "+", "-", "@"))
                         and c.data_type != "f"]
                self.assertIn('=HYPERLINK("http://example.com","x")', texts)
            else:
                self.assertEqual(formulas, [], path)
            st, ct, data, _ = app.dispatch("POST", path + "csv",
                                           {"Content-Type": "application/json"},
                                           json.dumps(body).encode())
            cells = [v for row in csv.reader(io.StringIO(data.decode("utf-8-sig")))
                     for v in row]
            live = [v for v in cells if v and v[0] in "=+-@"
                    and not re.fullmatch(r"[+\-]?\$?[\d,]*\.?\d+%?", v)]
            self.assertEqual(live, [], path)

    # -- page (static: the suite has no JavaScript engine) -------------------
    def test_rebuilds_wait_for_the_click_that_ended_an_edit(self):
        """Editing a cell and clicking straight into the next one lost the
        click (the change event rebuilt the block under the pointer): grid
        quantity / owner-set cells, the owner-set box, the worksheet's
        price, pick and contingency."""
        js = self.js
        self.assertIn("function afterInput(", js)
        self.assertIn("function restoreFocus(", js)
        for name in ("renderGrid", "renderOwnerSet", "renderQuotes", "renderWorksheet"):
            body = _js_function(js, name)
            self.assertIn("focusState()", body, name)
            self.assertIn("restoreFocus(", body, name)
        for m in re.finditer(r"onchange: e => \{(.*?)\n\s*\}", js, re.S):
            body = m.group(1)
            self.assertNotRegex(body, r"(?<!afterInput\()\brender(Grid|OwnerSet|Worksheet|WsTotals)\(",
                                body[:120])
        for fk in ("'g', l.row_id, 'quantity'", "'o', l.row_id", "'ws', l.row_id, 'price'",
                   "'ws:contingency'"):
            self.assertIn(fk, js)

    def test_did_you_mean_is_not_rebuilt_under_the_click(self):
        body = _js_function(self.js, "renderChoiceNote")
        self.assertIn("note.dataset.key === key", body)

    def test_every_edited_line_is_classified(self):
        self.assertNotIn("debounce(async (l)", self.js)
        self.assertIn("classifyPending.add(l)", self.js)

    def test_a_removed_quote_leaves_your_opcc(self):
        body = _js_function(self.js, "wsLine")
        self.assertIn("w.src === 'quote' && !(l.quote > 0)", body)
        self.assertIn("w.src === 'owner-set' && l.status !== 'owner_set'", body)

    def test_the_model_side_carries_the_not_covered_lines(self):
        body = _js_function(self.js, "wsTotals")
        self.assertIn("nc += a", body)
        self.assertIn("m50: plus(h.p50)", body)
        self.assertIn("vs(t.m50)", _js_function(self.js, "renderWsTotals"))

    def test_the_status_bar_count_follows_the_memory(self):
        for name in ("rememberCorrection", "forgetCorrection"):
            self.assertIn("refreshCorrectionsStatus(r)", _js_function(self.js, name))

    # -- user requests of the evening (row click, plain words, messy paste) ----
    def test_a_click_on_a_line_opens_its_references(self):
        """The toggle was a tiny dot. Now the whole row is the target (fields
        and controls inside it excepted), with a chevron, keyboard and
        aria-expanded; the similar cards open on their header."""
        js = self.js
        self.assertNotIn("twisty", js)
        self.assertIn("const ROW_CONTROLS = 'input, select, textarea, button, a, label';", js)
        body = _js_function(js, "resultRow")
        for k in ("tabindex: '0'", "'aria-expanded'", "if (rowToggles(e)) toggle();",
                  "e.key !== 'Enter' && e.key !== ' '", "class: 'chev'"):
            self.assertIn(k, body)
        card = _js_function(js, "simCard")
        self.assertIn("class: 'simhead'", card)
        self.assertIn("head.addEventListener('click', e => { if (rowToggles(e)) toggle(); });", card)
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertIn('href="#result-table">Lines</a>', html)
        self.assertIn('href="#p-similar">Similar projects</a>', html)
        self.assertIn('href="#p-worksheet">Worksheet</a>', html)
        self.assertLess(html.index('id="p-worksheet"'), html.index('id="p-similar"'))
        self.assertLess(html.index('id="p-similar"'), html.index('id="p-notcovered"'))
        self.assertLess(html.index('id="bidtab-fold"'), html.index('id="sec-export"'))
        self.assertIn('id="ws-hint"', html)
        hint = _js_function(js, "showRowHint")
        self.assertIn("try {", hint)

    def test_the_status_bar_is_plain_and_the_details_are_under_about(self):
        js = self.js
        bar = _js_function(js, "renderStatusBar")
        for k in ("md5", "Parser", "OCR", "rss_mb", "Claude reading"):
            self.assertNotIn(k, bar)
        self.assertIn("Prices from public bids through", bar)
        self.assertIn("about 10 seconds", bar)
        about = _js_function(js, "aboutRows")
        for k in ("md5", "Parser", "OCR", "Loaded"):
            self.assertIn(k, about)
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertIn('id="btn-about">About this tool</button>', html)

    def test_plain_words_on_the_page(self):
        js = self.js
        for gone in ("Estimator notes (", "'Rule N1 suggests'", "'Discount f(n)'",
                     "Bands calibrated on", "Loading the production estimator",
                     "'Parser’s family'", "keep the parser\\u2019s classification",
                     "'Model P50 '", "(comparables.py)"):
            self.assertNotIn(gone, js)
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        for gone in ("backtest", "calibration the estimate uses", "1.5&times;",
                     "bid-tab parser's own rules"):
            self.assertNotIn(gone, html)
        r = estimate(CREEK)
        for ln in r["lines"]:
            for gone in ("Jaccard", "cell", "Family median", "bucket"):
                self.assertNotIn(gone, ln["basis"])
        for a in r["alerts"]:
            self.assertNotIn("reference cells", a["text"])

    def test_quantity_less_lines_are_left_out_and_named(self):
        js = self.js
        req = _js_function(js, "estimateRequest")
        self.assertIn("S.lines.filter(l => l.include && !missingQuantity(l))", req)
        # a short warning you tick once read (2026-11-21), naming the lines
        rw = _js_function(js, "renderWarnings")
        self.assertIn("code: 'left_out'", rw)
        self.assertIn("lo.map(rowName)", rw)
        self.assertIn("leftOutText(out)", js)                    # and on the Estimate button
        grid = _js_function(js, "gridRow")
        # every line but a contingency (a share of the rest: no quantity
        # needed, 2026-11-21) says it needs a unit and a quantity
        self.assertIn("placeholder: cont ? '' : 'needs input'", grid)
        self.assertIn("needQ = !(l.quantity > 0) && !cont", grid)
        self.assertIn("if (isContingencyLine(l)) return false;", _js_function(js, "missingQuantity"))
        self.assertIn("' assumed'", grid)

    def test_no_page_wide_horizontal_scroll_on_a_phone(self):
        """The status bar's nowrap items made a 375 px page 520 px wide.
        On a phone they scroll in their own row; no rule outside a table
        or a scroll box sets a width a 375 px screen (343 px of content)
        cannot hold."""
        css = open(os.path.join(HERE, "static", "style.css"), encoding="utf-8").read()
        i = css.index("@media (max-width: 760px) {\n  header.topbar")
        mobile = css[i:css.index("\n}\n", i)]
        self.assertRegex(mobile, r"\.statusbar #status-items \{[^}]*flex-wrap: nowrap;"
                                 r"[^}]*overflow-x: auto;")
        flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        inside_scroll = ("table", "td", "th", ".btwrap", ".bidsbox", ".modal",
                         ".tablewrap")
        for sel, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", flat):
            for prop, px in re.findall(r"(?<![-\w])(min-width|width):\s*(\d+)px", decls):
                if int(px) <= 343 or any(k in sel for k in inside_scroll):
                    continue
                self.fail("%s { %s: %spx } is wider than a phone" % (sel.strip(), prop, px))
        for sel in (".tablewrap", ".btwrap"):
            self.assertRegex(flat, re.escape(sel) + r" \{[^}]*overflow: auto")

    def test_a_low_confidence_line_without_item_numbers_shows_none(self):
        """Bug 18: a lump sum with no item number showed an empty "item "."""
        body = _js_function(self.js, "renderLowConf")
        self.assertIn("const its = (g.items || []).filter(x => x);", body)
        self.assertIn("its.length ? el('div', { class: 'dim', text: 'item ' + its.join(', ') }) : null",
                      body)

    def test_change_opens_on_our_reading_not_on_the_last_item_type(self):
        """Bug 24: with no item type chosen, `selected: l.family && ...` gave
        every option selected="" (el() sets any non-false value), so the
        dialog opened on the LAST item type ("Wingwalls") and Apply set it."""
        body = _js_function(self.js, "openPicker")
        self.assertIn("!!l.family && f.id === l.family", body)
        self.assertNotIn("selected: l.family &&", self.js)

    def test_the_change_dialog_speaks_of_item_types(self):
        body = _js_function(self.js, "openPicker")
        self.assertIn("Each line is matched to an item type (for example", body)
        self.assertIn("text: f.label,", body)                 # no "· internal_id"
        for old in ("'Use family'", "'Use family + '", "'Family'", "'usable'", "'too thin'"):
            self.assertNotIn(old, body)
        self.assertIn("'plenty of bids'", body)
        self.assertIn("'Use this item type and its unit (' + c.unit + ')'", body)

    def test_no_explanatory_text_under_12px_and_no_ellipsis(self):
        """User, 22:35: "the not-covered text, write it bigger" and "check
        and so on are all dots, I cannot see the details". Every text is
        12px or more (only the symbols in small circles are smaller), the
        not-covered texts and warnings are bigger, and nothing is cut with
        an ellipsis the user cannot open."""
        css = open(os.path.join(HERE, "static", "style.css"), encoding="utf-8").read()
        flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        small_ok = {".jump .num, .secnum", ".help", ".chev", ".simtop .rank"}
        for sel, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", flat):
            for px in re.findall(r"font-size:\s*([\d.]+)px", decls):
                if float(px) < 12:
                    self.assertIn(" ".join(sel.split()), small_ok, (sel.strip(), px))
        self.assertNotIn("text-overflow", flat)
        for rule, size in ((".notice {", "14px"),
                           (".panel > h2 {", "14px"), ("#r-notcovered table.mini {", "13px"),
                           (".badge.unc     {", "13px")):
            i = flat.index(rule)
            self.assertIn("font-size: " + size, flat[i:flat.index("}", i)], rule)
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertNotIn("&hellip;</span>", html)
        self.assertNotIn("slice(0, 21) + '\\u2026'", self.js)
        self.assertIn("ls.map(l => ((l.item_no", self.js)                      # the whole list

    def test_ai_mode_replaces_the_claude_button(self):
        """2026-11-21 (user): "Read a PDF with Claude" is folded into AI mode
        ("Read by AI", not built yet); files go in through the one drop area."""
        html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertNotIn('id="btn-claude"', html)
        self.assertIn('id="btn-ai-mode"', html)
        self.assertIn('id="btn-ai-read" type="button" disabled>Read by AI<', html)
        self.assertEqual(html.count('class="dropzone"'), 2)      # the takeoff's and the bid tab's
        self.assertNotIn("brew install", html)
        self.assertIn("panel.hidden = !open", _js_function(self.js, "initAiMode"))
        self.assertNotIn("Read with Claude", _js_function(self.js, "aboutRows"))

    def test_estimator_notes_name_item_types_not_codes(self):
        out = ep.plain_codes("1 of 10 lines (pump_equipment) are specialty items; "
                             "erosion_control; usable='yes'; not_a_code")
        self.assertIn("(Pumps / pump, lift or booster station)", out)
        self.assertNotIn("pump_equipment", out)
        self.assertIn("not_a_code", out)          # only known ids are replaced

    def test_the_reference_column_holds_the_source(self):
        """Bug 23 (a 96 px box cut the source to "Model price !"), then
        2026-11-21 (user): the source has a column of its own, "Reference",
        with short names that fit, set to the line's current source
        (browser test: test_browser.py)."""
        body = _js_function(self.js, "resultRow")
        self.assertIn("tr.appendChild(el('td', { class: 'refcell' }, ref));", body)
        self.assertIn("text: p.short || p.label, title: p.label", body)
        self.assertIn("selected: !!cur && p.v === cur.v", body)



# ===========================================================================
#: The experiments' demo (reports/unit_conversion_2026-11-20.md) plus a
#: mobilization line that prints its cap.
UNIT_DEMO = [
    item("Mobilization (5% Max)", "LS", 1, 101),
    item("Excavation", "CF", 113400, 102),
    item("Embankment", "CF", 48600, 103),
    item("Concrete Sidewalk, 4 inch", "SF", 12000, 104),
    item("Block Sod", "SF", 30000, 105),
    item("Reinforced Concrete Pipe, 24 inch", "LF", 800, 106),
]


class UnitHintAndCap(unittest.TestCase):
    """Batch 3 (2026-11-20): presentation only. A line whose unit is thin in
    the history is offered a price converted from a unit of the same kind
    (U3's evidence and factors, reused in the UI, src untouched); a
    mobilization line that prints a cap is offered the cap. Neither changes
    P50 / P80."""

    def test_the_factor_table(self):
        f = ep.unit_factor
        self.assertEqual([f(a, b) for a, b in (("SF", "SY"), ("AC", "SY"), ("CF", "CY"),
                                               ("LB", "TON"), ("CWT", "TON"), ("FT", "LF"),
                                               ("MG", "GAL"), ("SY", "SF"), ("sq. ft.", "SY"))],
                         [(1, 9), (4840, 1), (1, 27), (1, 2000), (1, 20), (1, 1),
                          (1000, 1), (9, 1), None])
        self.assertEqual(f("cf", "cy"), (1, 27))

    def test_never_converted(self):
        f = ep.unit_factor
        for a, b in (("VF", "LF"), ("STA", "LF"), ("MI", "LF"), ("LMI", "LF"),
                     ("SQ", "SF"), ("SQ", "SY"), ("YD", "LF"), ("YD", "CY"),
                     ("SY", "CY"), ("LF", "SF"), ("TON", "CY"), ("EA", "LF"), ("LS", "EA")):
            self.assertIsNone(f(a, b), (a, b))
        self.assertEqual([u for u in ep.UNIT_NEVER_CONVERT
                          if ep.unit_spelling(u) in ep.UNIT_CONVERSIONS], [])

    def evidence(self):
        # pool rows as estimate_low_bid._reference_pool gives them:
        # (market, letting, family, unit, size, qty, price, low?, bidder, desc_norm, owner, year)
        rows, fks = [], []
        def add(pk, fam, unit):
            rows.append(("municipal", pk, fam, unit, "", 100.0, 10.0, True, "b", "x", "", "2024"))
            fks.append(fam)
        for i in range(6):
            add("cy%d" % i, "embankment_fill", "CY")
        add("cf0", "embankment_fill", "CF")
        add("late", "embankment_fill", "CF")
        add("oth", "other", "CY")
        for i in range(5):
            add("sf%d" % i, "sidewalk", "SF")
        for i in range(3):
            add("pv%d" % i, "pavers", "SF")
        for i in range(2):
            add("py%d" % i, "pavers", "SY")
        add("cy0", "embankment_fill", "CY")       # the same letting twice: once
        avail = {pk: "2023-0%d-01" % (1 + i % 8) for i, pk in enumerate(sorted({r[1] for r in rows}))}
        avail["late"] = "2026-06-01"
        return ep.UnitEvidence(rows, fks, avail)

    def test_evidence_counts_lettings_before_the_bid_date(self):
        ev = self.evidence()
        self.assertEqual(ev.n("embankment_fill", "CY", "2025-01-01"), 6)
        self.assertEqual(ev.n("embankment_fill", "CF", "2025-01-01"), 1)
        self.assertEqual(ev.n("embankment_fill", "CF", "2027-01-01"), 2)
        self.assertEqual(ev.n("embankment_fill", "cf", "2025-01-01"), 1)
        self.assertEqual(ev.n("embankment_fill", "CY", "2022-01-01"), 0)
        self.assertEqual(ev.n("other", "CY"), 0)             # never for the catch-all
        self.assertEqual(ev.n("sidewalk", "SF", "2025-01-01"), 5)

    def test_when_the_hint_shows(self):
        ev = self.evidence()
        t = ep.unit_hint_target
        D = "2025-01-01"
        self.assertEqual(t(ev, "embankment_fill", "CF", "Embankment", D), ("CY", 1, 27, 1, 6))
        self.assertIsNone(t(ev, "sidewalk", "SF", "Sidewalk", D))       # 5 lettings: enough
        self.assertIsNone(t(ev, "pavers", "SF", "Pavers", D))          # the other unit is thin too
        self.assertIsNone(t(ev, "other", "CF", "x", D))
        self.assertIsNone(t(ev, "needs_review", "CF", "x", D))
        self.assertIsNone(t(ev, "", "CF", "x", D))
        self.assertIsNone(t(ev, "embankment_fill", "EA", "x", D))
        self.assertIsNone(t(ev, "embankment_fill", "SQ", "x", D))
        self.assertIsNone(t(ev, "embankment_fill", "CF", "x", "2023-01-02"))   # CY not usable yet
        self.assertIsNone(t(ev, "embankment_fill", "CY", "x", D))      # its own unit is usable
        # an unpriced line is offered the other unit even when its own has 5
        self.assertIsNone(t(ev, "sidewalk", "SF", "x", D, unpriced=True))   # no other usable unit
        self.assertEqual(ep.unit_evidence_text("CF", "CY", 1, 570),
                         "Only 1 earlier letting prices this in CF; 570 price it in CY.")
        self.assertEqual(ep.unit_evidence_text("CF", "CY", 0, 570),
                         "No earlier letting prices this in CF; 570 price it in CY.")

    def test_the_cap_parser(self):
        cap = ep.printed_cap
        for d in ("Mobilization (5% Max)", "Mobilization, Max 5% of Total Bid",
                  "Mobilization not to exceed 5%", "MOBILIZATION (5% MAX)",
                  "Mobilization, 5 % maximum", "Mobilization - NTE 5% of contract",
                  "Mobilization (not to exceed 5 percent)"):
            self.assertEqual(cap(d), {"pct": 0.05, "basis": "total", "text": "5%"}, d)
        self.assertEqual(cap("Mobilization, Bonds and Insurance (Max 4.5% of the rest of the bid)"),
                         {"pct": 0.045, "basis": "rest", "text": "4.5%"})
        self.assertEqual(cap("Mobilization 3% max of the remaining items")["basis"], "rest")
        for d in ("Mobilization", "Mobilization 5%", "Mobilization 100% max", "Mobilization, Bonds"):
            self.assertIsNone(cap(d), d)

    def test_the_change_search_names_an_item_types_average_plainly(self):
        self.assertEqual(app._family_average_label("gabion_mattress", {
            "unit": "SY", "n_projects": 19, "label": "gabion_mattress (SY) -- family average"}),
            ("All gabion mattresses (SY) \u2014 average of 19 earlier projects", ""))
        self.assertEqual(app._family_average_label("other", {
            "unit": "EA", "n_projects": 855, "label": "unclassified (EA) -- no item family matched"})[0],
            "Unclassified lines (EA) \u2014 no item type matched")
        st, x = api("GET", "/api/history/search?q=gabion+mattress&unit=SY&limit=30")
        self.assertEqual(st, 200)
        for c in x["candidates"]:
            self.assertNotIn("family average", c["label"])
            self.assertNotIn("pooled across", c.get("detail") or "")
            self.assertNotIn("_", c["label"] if c["kind"] == "family" else "")

    def test_the_demo_gets_its_hints_and_the_estimate_does_not_move(self):
        ENG.load_references()
        req = {"market": "municipal", "n_bidders": "auto", "bid_date": "2026-11-20",
               "items": UNIT_DEMO}
        before = ENG.estimate(req)
        st, w = api("POST", "/api/worksheet/refs", {"request": req})
        self.assertEqual(st, 200, w)
        after = ENG.estimate(req)
        for k in ("p50", "p80"):
            self.assertEqual(before["headline"][k], after["headline"][k])
        hints = {v["row_id"]: v.get("unit_hint") for v in w["lines"]}
        for rid in ("r102", "r103"):
            h = hints[rid]
            self.assertIsNotNone(h, rid)
            self.assertEqual((h["unit"], h["target"]), ("CF", "CY"))
            self.assertLess(h["own"], 5)
            self.assertGreaterEqual(h["n_target"], 5)
            self.assertAlmostEqual(h["unit_price"] * 27, h["unit_price_target"], places=6)
            self.assertTrue(0.3 < h["market_unit_price"] < 3.0, h)    # about $1/CF, not $11.25
            self.assertIn("price it in CY", h["text"])
            self.assertIn("Reference only", h["text"])
        for rid in ("r101", "r104", "r105", "r106"):
            self.assertIsNone(hints[rid], rid)            # SF sidewalk and sod have 5+
        lines = {l["row_id"]: l for l in before["lines"]}
        self.assertEqual(lines["r101"]["printed_cap"], {"pct": 0.05, "basis": "total", "text": "5%"})
        self.assertIsNone(lines["r106"]["printed_cap"])
        # the takeoff grid's evidence
        st, c = api("POST", "/api/classify", {"market": "municipal", "bid_date": "2026-11-20",
                                              "lines": UNIT_DEMO})
        got = {x["row_id"]: (x.get("unit_hint") or {}).get("target") for x in c["lines"]}
        self.assertEqual(got, {"r101": None, "r102": "CY", "r103": "CY", "r104": None,
                               "r105": None, "r106": None})
        # UI == production: the same items straight into estimate()
        r = direct(UNIT_DEMO, n=before["inputs"]["n_bidders"], bid_date="2026-11-20",
                   work_type="")
        self.assertAlmostEqual(r["expected_low"], before["headline"]["p50"], places=6)

    def test_the_page_offers_both_picks(self):
        js = _js()
        picks = _js_function(js, "wsPicks")
        self.assertIn("v: 'unit'", picks)
        self.assertIn("'Converted from ' + uh.target + ' bids: '", picks)
        self.assertIn("v: 'cap'", picks)
        self.assertIn("'Printed cap: ' + l.printed_cap.text", picks)
        amt = _js_function(js, "printedCapAmount")
        self.assertIn("cap.basis === 'rest' ? c * rest : c / (1 - c) * rest", amt)
        self.assertIn("Bidders usually price it at or just under the printed cap.", _js_function(js, "capText"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
