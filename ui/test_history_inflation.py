"""The worksheet's price adjustment must not borrow future information."""

import unittest

from history_inflation import HistoryInflation


def rows(year, price, date=None, market="municipal", precision="precise", n=10):
    return [(market, "job-%s-%s-%s" % (market, year, i), date or "%s-03-01" % year,
             precision, "item-%s" % i, year, price) for i in range(n)]


class HistoryInflationTests(unittest.TestCase):
    def test_adjustment_uses_matched_items_and_allows_measured_falls(self):
        h = HistoryInflation(rows(2024, 100) + rows(2025, 110) + rows(2026, 99))
        a = h.adjust("municipal", 2024, "2026-06-01")
        self.assertEqual(a["status"], "available")
        self.assertAlmostEqual(a["factor"], .99)
        self.assertEqual([s["n_matched"] for s in a["steps"]], [10, 10])

    def test_same_day_and_future_bids_cannot_move_the_index(self):
        base = rows(2024, 100) + rows(2025, 110)
        future = rows(2025, 900, "2025-06-01") + rows(2025, 800, "2025-07-01")
        a = HistoryInflation(base).adjust("municipal", 2024, "2025-06-01")
        b = HistoryInflation(base + future).adjust("municipal", 2024, "2025-06-01")
        self.assertEqual(a, b)

    def test_thin_or_missing_year_never_borrows_another_market(self):
        h = HistoryInflation(rows(2024, 100) + rows(2025, 110, n=9)
                             + rows(2024, 100, market="txdot")
                             + rows(2025, 200, market="txdot"))
        a = h.adjust("municipal", 2024, "2025-06-01")
        self.assertIsNone(a["factor"])
        self.assertEqual(a["steps"][0]["n_matched"], 9)
        self.assertIsNone(h.adjust("txdot", 2024, "2026-06-01")["factor"])

    def test_same_year_is_explicitly_no_intrayear_inflation(self):
        a = HistoryInflation([]).adjust("municipal", 2026, "2026-11-22")
        self.assertEqual(a["factor"], 1.0)
        self.assertIn("within the year", a["notes"][0])

    def test_each_project_and_item_has_one_vote_and_dates_must_be_precise(self):
        data = rows(2024, 100) + rows(2025, 110)
        # One project has a very high third bidder: its median remains 110.
        extra = []
        for row in rows(2025, 110):
            extra.extend([row, (*row[:-1], 500)])
        a = HistoryInflation(data + extra + rows(2025, 900, precision="approx"))
        self.assertAlmostEqual(a.adjust("municipal", 2024, "2025-06-01")["factor"], 1.1)

    def test_invalid_inputs_cannot_become_prices(self):
        h = HistoryInflation(rows(2024, float("inf")) + rows(2025, 110))
        for year, date in ((None, "2026-01-01"), (True, "2026-01-01"),
                           (2025.5, "2026-01-01"), (2025, "bad")):
            self.assertIsNone(h.adjust("municipal", year, date)["factor"])
        self.assertIsNone(h.adjust("municipal", 2024, "2025-06-01")["factor"])


if __name__ == "__main__":
    unittest.main()
