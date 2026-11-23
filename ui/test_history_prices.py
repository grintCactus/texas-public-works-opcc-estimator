"""Synthetic worksheet-history selection tests; no model scoring or corpus rebuild."""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "ui"))
import history_prices as hp


DESCRIPTION = "18 in RCP storm sewer pipe class III"


def rows(project, median, date="2026-03-01", **changes):
    base = {"source_set": "external", "source": "city_bidtab",
            "is_engineer_estimate": "False", "competition_class": "competitive",
            "project_kind": "construction", "price_basis": "all_bids",
            "project_key": project, "item_no": "1", "desc_norm": DESCRIPTION.lower(),
            "description": DESCRIPTION, "unit": "LF", "item_family": "storm_pipe",
            "quantity": "100", "size_in": "18", "size_bucket": "18", "material": "rcp",
            "bid_date": date, "date_precision": "precise", "year": date[:4],
            "item_key": "city|storm sewer pipe"}
    base.update(changes)
    return [dict(base, bidder="ACME, LLC", bidder_norm="ACME, LLC", unit_price=median - 2),
            dict(base, bidder="Beta Construction", bidder_norm="Beta Construction", unit_price=median + 2)]


def item(**changes):
    out = {"description": DESCRIPTION, "family": "storm_pipe", "unit": "LF",
           "quantity": 100, "size_in": 18, "size_bucket": "18"}
    out.update(changes)
    return out


def adjustment(market, source, date):
    return {"status": "available", "factor": 1.0,
            "method": "synthetic_index", "market": market,
            "from_year": source, "target_year": int(date[:4]),
            "as_of": date, "steps": [], "reason": "Synthetic annual adjustment.", "notes": []}


def make_index(data):
    available = {r["project_key"]: r["bid_date"] for r in data}
    return hp.HistoryPriceIndex(data, available)


def select(index, target=None, **kwargs):
    return index.select(target or item(), bid_date=kwargs.pop("bid_date", "2026-06-01"),
                        market=kwargs.pop("market", "municipal"),
                        status=kwargs.pop("status", "priced"),
                        adjust=kwargs.pop("adjust", adjustment), **kwargs)


class HistoricalPrices(unittest.TestCase):
    def test_earliest_valid_calendar_date_does_not_crash_reference_selection(self):
        result = select(make_index(rows("a", 20)), bid_date="0001-01-01")
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["unit_price"])

    def test_three_projects_each_get_one_bidder_median_without_pooling_pay_items(self):
        data = rows("a", 20) + rows("b", 40) + rows("c", 60)
        duplicate = dict(data[0], bidder="Acme LLC", bidder_norm="Acme LLC")
        data += [duplicate] * 20
        data += rows("a", 1000, item_no="2")
        result = select(make_index(data))
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["unit_price"], 40)
        self.assertEqual([r["median_unit"] for r in result["references"]], [20, 40, 60])
        self.assertEqual([r["n_bids"] for r in result["references"]], [2, 2, 2])
        self.assertEqual({r["item_no"] for r in result["references"]}, {"1"})

    def test_duplicate_and_conflicting_quotes_do_not_create_bidder_evidence(self):
        data = rows("a", 20) + rows("b", 40) + rows("c", 60)
        data += [dict(data[0], unit_price=999)]
        index = make_index(data)
        result = select(index)
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["unit_price"])
        self.assertEqual(result["n_projects"], 2)
        self.assertEqual(index.excluded["conflicting_bidder_quotes"], 1)
        one_bidder = rows("d", 80)[:1] * 10
        self.assertEqual(select(make_index(one_bidder))["n_projects"], 0)

    def test_quantity_disagreement_and_metadata_conflict_are_not_pooled(self):
        data = rows("a", 20) + rows("b", 40) + rows("c", 60)
        data[1]["quantity"] = "200"
        data[3]["size_in"] = "24"
        result = select(make_index(data))
        self.assertEqual(result["n_projects"], 1)
        self.assertEqual(result["references"][0]["key"], "c")

    def test_public_construction_bids_only_and_index_deduplicates_units(self):
        good = rows("a", 20) + rows("b", 40) + rows("c", 60)
        rejected = []
        for i, change in enumerate(({"source_set": "internal"}, {"source_set": "public_test"},
                {"is_engineer_estimate": "True"}, {"competition_class": "sole"},
                {"project_kind": "non_construction"}, {"price_basis": "uneven_proposal"})):
            rejected += rows("reject%d" % i, 1000, **change)
        index = make_index(good + rejected + [good[0]])
        self.assertEqual(select(index)["unit_price"], 40)
        self.assertEqual(len(index.index_rows), 6)
        self.assertEqual({r[4].split("|unit=")[-1] for r in index.index_rows}, {"LF"})
        self.assertEqual({r[1] for r in index.index_rows}, {"a", "b", "c"})

    def test_twenty_four_month_hard_window_same_day_and_future_excluded(self):
        data = rows("boundary", 20, "2024-06-01") + rows("recent", 40, "2026-05-01")
        data += rows("too-old", 100, "2024-05-31")
        data += rows("same-day", 100, "2026-06-01") + rows("future", 100, "2026-06-02")
        result = select(make_index(data))
        self.assertEqual(result["n_projects"], 2)
        self.assertIsNone(result["unit_price"])
        self.assertEqual({r["key"] for r in result["references"]}, {"boundary", "recent"})
        self.assertEqual(result["window_start"], "2024-06-01")
        self.assertEqual(hp.years_before(dt.date(2024, 2, 29), 2), dt.date(2022, 2, 28))

    def test_preceding_year_is_preferred_then_score_and_stable_identity(self):
        data = rows("old-perfect", 1000, "2025-05-31")
        data += rows("new-c", 60, "2025-06-01", quantity="1000")
        data += rows("new-b", 40, "2025-06-01", quantity="1000")
        data += rows("new-a", 20, "2025-06-01", quantity="1000")
        result = select(make_index(list(reversed(data))))
        self.assertEqual([r["key"] for r in result["references"]], ["new-a", "new-b", "new-c"])
        self.assertEqual(result["unit_price"], 40)
        self.assertTrue(all(r["within_12_months"] for r in result["references"]))
        self.assertEqual(result["references"][0]["quantity_ratio"], 10)

    def test_exact_unit_size_material_spec_and_operation_are_hard_gates(self):
        data = rows("ok", 20)
        changes = [
            {"unit": "SY"}, {"size_in": "24"}, {"size_in": ""}, {"size_bucket": "24"},
            {"material": "pvc"},
            {"desc_norm": "24 in rcp storm sewer pipe class iii", "description": "24 in RCP storm sewer pipe class III"},
            {"desc_norm": "18 in rcp storm sewer pipe class iv", "description": "18 in RCP storm sewer pipe class IV"},
            {"desc_norm": "remove existing 18 in rcp storm sewer pipe class iii", "description": "Remove existing 18 in RCP storm sewer pipe class III"},
            {"desc_norm": "abandon existing 18 in rcp storm sewer pipe class iii", "description": "Abandon existing 18 in RCP storm sewer pipe class III"},
        ]
        for i, change in enumerate(changes):
            data += rows("bad%d" % i, 100, **change)
        result = select(make_index(data))
        self.assertEqual([r["key"] for r in result["references"]], ["ok"])

    def test_shared_numeric_token_is_not_enough_and_single_letter_class_is_checked(self):
        target = item(description="18 in RCP storm sewer pipe class 3")
        data = rows("wrong-diameter", 20, description="24 in RCP storm sewer pipe class 3",
                    desc_norm="24 in rcp storm sewer pipe class 3")
        self.assertEqual(select(make_index(data), target)["n_projects"], 0)
        data = rows("class-b", 20, description="18 in RCP storm sewer pipe class B",
                    desc_norm="18 in rcp storm sewer pipe class b")
        self.assertEqual(select(make_index(data), item(description="18 in RCP storm sewer pipe class A"))["n_projects"], 0)

    def test_approximate_dates_and_later_project_availability_do_not_pass(self):
        data = rows("approx", 20, date_precision="approx") + rows("not-yet-complete", 40)
        index = hp.HistoryPriceIndex(data, {"approx": "2026-03-01", "not-yet-complete": "2026-06-01"})
        self.assertEqual(select(index)["n_projects"], 0)

    def test_markets_are_not_mixed_and_unknown_source_is_not_txdot(self):
        data = rows("city", 20) + rows("state", 40, source="txdot")
        data += rows("unknown", 60, source="unknown_source")
        index = make_index(data)
        self.assertEqual([r["key"] for r in select(index)["references"]], ["city"])
        self.assertEqual([r["key"] for r in select(index, market="txdot")["references"]], ["state"])
        self.assertNotIn("unknown", {r[1] for r in index.index_rows})

    def test_missing_index_item_key_does_not_turn_units_into_matching_items(self):
        data = rows("a", 20, item_key="") + rows("b", 40, item_key="") + rows("c", 60, item_key="")
        index = make_index(data)
        self.assertEqual(index.index_rows, [])
        # A separately supported same-year adjustment can still use the
        # pay-item evidence; lacking the index key does not erase the bids.
        self.assertEqual(select(index)["unit_price"], 40)

    def test_identical_description_material_aliases_do_not_create_false_conflict(self):
        data = rows("a", 20, material="concrete") + rows("b", 40) + rows("c", 60)
        self.assertEqual(select(make_index(data))["unit_price"], 40)
        description = "18 in DIP water pipe class III"
        data = []
        for key, price in (("a", 20), ("b", 40), ("c", 60)):
            data += rows(key, price, material="DUCTILE IRON", description=description,
                         desc_norm=description.lower())
        self.assertEqual(select(make_index(data), item(description=description))["unit_price"], 40)

    def test_same_project_item_number_with_conflicting_description_is_excluded(self):
        data = rows("a", 20) + rows("b", 40) + rows("c", 60)
        data += rows("a", 100, description=DESCRIPTION + " reinforced",
                     desc_norm=DESCRIPTION.lower() + " reinforced")
        result = select(make_index(data))
        self.assertEqual(result["n_projects"], 2)
        self.assertEqual({r["key"] for r in result["references"]}, {"b", "c"})

    def test_manual_owner_set_specialty_generic_and_lump_sum_stay_blank(self):
        index = make_index(rows("a", 20) + rows("b", 40) + rows("c", 60))
        for status in ("owner_set", "not_covered", "not_covered_whole", "unpriced"):
            with self.subTest(status=status):
                self.assertEqual(select(index, status=status)["status"], "ineligible")
        self.assertEqual(select(index, pricing_mode="engineer")["status"], "ineligible")
        for target in (item(unit="LS"), item(family="other"), item(family="needs_review"),
                       item(family="mobilization")):
            self.assertEqual(select(index, target)["status"], "ineligible")

    def test_adjust_each_project_median_before_taking_the_three_project_median(self):
        data = rows("a", 20, "2025-12-01") + rows("b", 30) + rows("c", 40)
        def adjust(market, year, date):
            return dict(adjustment(market, year, date), factor=3 if year == 2025 else 1)
        target = item()
        before = copy.deepcopy(target)
        result = select(make_index(data), target, adjust=adjust)
        self.assertEqual(result["unit_price"], 40)
        self.assertEqual(sorted(r["median_unit"] for r in result["references"]), [20, 30, 40])
        self.assertEqual(sorted(r["adjusted_median"] for r in result["references"]), [30, 40, 60])
        self.assertEqual(target, before)

    def test_missing_index_keeps_selected_raw_evidence_and_does_not_swap_candidates(self):
        data = rows("a", 20, "2026-04-01") + rows("b", 30, "2026-03-01")
        data += rows("missing-index", 40, "2025-12-01")
        data += rows("worse-match", 50, "2026-05-01", quantity="100000")
        def adjust(market, year, date):
            return (dict(adjustment(market, year, date), status="unavailable", factor=None)
                    if year == 2025 else adjustment(market, year, date))
        result = select(make_index(data), adjust=adjust)
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["unit_price"])
        self.assertEqual({r["key"] for r in result["references"]}, {"a", "b", "missing-index"})
        ref = next(r for r in result["references"] if r["key"] == "missing-index")
        self.assertEqual(ref["median_unit"], 40)
        self.assertIsNone(ref["adjusted_median"])


if __name__ == "__main__":
    unittest.main()
