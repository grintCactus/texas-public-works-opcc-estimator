"""Regression checks for covered non-LS project dollars and comparison denominators."""
import json
from pathlib import Path
import tempfile
import unittest

from src.opcc_accuracy import lump_sum, paired, project_score, read_receipts, summarize


def forecasts(p50=120, p70=156, p80=180):
    return dict(p50=p50, p70=p70, p80=p80)


def line(key, unit, status, market=None, winner=None, **fields):
    return dict(row_id=key, unit=unit, status=status, market_amount=market,
                winner_amount=winner, **fields)


class ProjectAccuracyTests(unittest.TestCase):
    def test_union_and_original_winner(self):
        # A second contractor could win the shortened schedule. Only the
        # original winner's extensions enter this receipt.
        rows = [line("a", "EA", "priced", 50, 60, engineer_amount=65),
                line("b", "LS", "priced", 50, 37),
                line("c", "LF", "not_covered", winner=500),
                line("d", "LS", "not_covered_whole", winner=100)]
        result = project_score(rows, forecasts())
        self.assertEqual(result["counts"], dict(total=4, lump_sum=2, not_covered=2,
                                               both=1, excluded=3, eligible=1))
        self.assertEqual(result["actual"], 60)
        self.assertEqual(result["engineer"], 65)
        self.assertEqual(result["predictions"], forecasts(60, 78, 90))
        rows[2]["winner_amount"] = None
        rows[3]["winner_amount"] = 999999
        self.assertEqual(project_score(rows, forecasts()), result)

    def test_fixed_at_face_and_percentile_allocation(self):
        rows = [line("ea", "EA", "priced", 40, 40),
                line("ls", "EA", "priced", 60, 90, unit_raw="L.S. (1 JOB)"),
                line("fixed", "ALLOW", "owner_set", winner=5, fixed_amount=5),
                line("lsfixed", "LS", "owner_set", winner=15, fixed_amount=15)]
        result = project_score(rows, forecasts(120, 145, 170))
        self.assertEqual(result["predictions"], forecasts(45, 55, 65))
        self.assertEqual(result["actual"], 45)

    def test_full_reconciliation_and_zero_bid(self):
        rows = [line("a", "EA", "priced", 60, 0),
                line("b", "SY", "priced", 40, 80, engineer_amount=90)]
        result = project_score(rows, forecasts())
        self.assertEqual(result["predictions"], forecasts())
        self.assertEqual(result["actual"], 80)
        self.assertIsNone(result["engineer"])

    def test_unit_spellings_and_one_ea(self):
        for u in ("ls", "L.S.", "L S", "LUMP SUM", "Lump-Sum", "LSUM", "Per Lump Sum"):
            with self.subTest(u=u):
                self.assertTrue(lump_sum({"unit": "EA", "unit_raw": u}))
        self.assertFalse(lump_sum({"unit": "EA", "quantity": 1, "description": "Mobilization"}))
        self.assertFalse(lump_sum({"unit": "EA", "description": "Includes a lump sum allowance"}))
        self.assertTrue(lump_sum({"description": "Unit of payment is a lump sum"}))
        self.assertFalse(lump_sum({"unit": "EA", "description": "Pay unit is lump sum for alternate only"}))

    def test_unpriced_excluded_line_and_project_holds(self):
        good = line("k", "EA", "priced", 120, 60)
        self.assertEqual(project_score([good, line("s", "LS", "unpriced")],
                                       forecasts())["status"], "scored")
        for rows, reason in [([], "no_eligible_lines"),
                             ([line("s", "LS", "priced", 10)], "no_eligible_lines"),
                             ([line("s", "CY", "not_covered_whole")], "no_eligible_lines"),
                             ([good, line("w", "LF", "unpriced")], "unpriced_eligible_line"),
                             ([good, line("w", "LF", "priced", 100)], "missing_winner_extension"),
                             ([good, good], "missing_or_duplicate_row_id"),
                             ([line("k", "EA", "priced", 10, 0)], "nonpositive_actual"),
                             ([line("k", "", "priced", 20, 30)], "unknown_eligible_unit")]:
            with self.subTest(reason=reason):
                self.assertIn(reason, project_score(rows, forecasts())["reasons"])

    def test_all_fixed_and_unavailable_forecast(self):
        rows = [line("x", "DOL", "owner_set", winner=20, fixed_amount=20)]
        self.assertEqual(project_score(rows, forecasts(20, 20, 20))["predictions"]["p80"], 20)
        self.assertIn("unallocatable_forecast", project_score(rows, forecasts())["reasons"])
        budget_only = project_score(rows, {"p50": 20})
        self.assertEqual(budget_only["predictions"], {"p50": 20})
        self.assertEqual(budget_only["missing_quantiles"]["p70"], "unavailable_forecast")

    def test_summary_is_one_vote_per_scored_letting(self):
        a = project_score([line("a", "EA", "priced", 10, 10, engineer_amount=12)],
                          forecasts(9, 10, 12))
        b = project_score([line("a", "EA", "priced", 1000, 1000)], forecasts(900, 950, 999))
        held = project_score([], forecasts())
        summary = summarize([a, b, held])
        self.assertEqual((summary["scored"], summary["held"]), (2, 1))
        self.assertEqual(summary["quantiles"]["p80"]["not_under_rate"], 0.5)
        self.assertAlmostEqual(summary["quantiles"]["p80"]["median_absolute_error"], 0.1005)
        self.assertEqual(summary["engineer"]["n"], 1)
        self.assertEqual(summary["models_on_engineer_projects"]["p80"]["n"], 1)
        self.assertIsNone(summarize([held])["quantiles"]["p80"]["not_under_rate"])

    def test_different_scope_or_winner_is_not_a_paired_gain(self):
        a = project_score([line("a", "EA", "priced", 120, 60)], forecasts())
        b = project_score([line("b", "EA", "priced", 120, 60)], forecasts())
        c = project_score([line("a", "EA", "priced", 120, 61)], forecasts())
        for other in (b, c):
            result = paired({"job": a, "unchanged": a}, {"job": other, "unchanged": a})
            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["unmatched"]["job"], "different_rows_or_winning_amounts")

    def test_input_stream_rejects_duplicate_project(self):
        record = {"project_key": "public-demo", "headline": forecasts(),
                  "lines": [line("a", "EA", "priced", 50, 50)]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.jsonl"
            path.write_text(json.dumps(record) + "\n")
            self.assertEqual(read_receipts(path)["public-demo"]["status"], "scored")
            with path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            with self.assertRaisesRegex(ValueError, "unique"):
                read_receipts(path)

    def test_historical_forecasts_without_p70(self):
        rows = [line("a", "LF", "priced", 40, 40)]
        old = project_score(rows, {"p50": 40, "p80": 48})
        new = project_score(rows, forecasts(39, 44, 46))
        self.assertEqual(old["status"], "scored")
        metrics = paired({"job": old}, {"job": new})
        self.assertEqual(metrics["missing_paired_quantiles"], {"job": ["p70"]})
        for arm in ("baseline", "candidate"):
            self.assertEqual(metrics[arm]["quantiles"]["p70"]["n"], 0)
            self.assertEqual(metrics[arm]["quantiles"]["p80"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
