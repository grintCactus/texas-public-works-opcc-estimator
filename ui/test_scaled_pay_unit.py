"""A printed 100 LF pay unit and its equivalent quantity in feet have the same amount."""

import io
from pathlib import Path
import sys
import unittest
from unittest import mock

import fitz
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_production as ep
from ingest import takeoff
import upstream
import worksheet


class ScaledPayUnitTest(unittest.TestCase):
    def test_read_items_distinguishes_a_unit_column_from_a_quantity(self):
        header = "Item,Description,Unit,Quantity\n"
        for label in ("100 LF", "100.0 LF", "per 100.00 LF"):
            with self.subTest(unit=label):
                parsed = takeoff.from_text(header + f"101S-B,Preparing right-of-way,{label},45")
                line, = parsed.lines
                self.assertEqual((line.unit, line.quantity), ("100 LF", 45))
        for text in ("Preparing right-of-way  45  100 LF", "Preparing right-of-way  100 LF  45"):
            with self.subTest(text=text):
                line, = takeoff.from_text(text).lines
                self.assertEqual((line.unit, line.quantity), ("100 LF", 45))
        line, = takeoff.from_text("Lay pipe 100 LF").lines
        self.assertEqual((line.unit, line.quantity), ("LF", 100))
        line, = takeoff.from_text(header + "101S-B,Preparing right-of-way,100 LF,").lines
        self.assertIsNone(line.quantity)

    def test_pdf_import_uses_the_printed_unit(self):
        parsed = {
            "item_no": "101S-B", "description": "Preparing right-of-way",
            "quantity": 45, "unit_raw": "100 LF", "unit": "LF",
        }
        with mock.patch.object(upstream, "read_pdf_best", return_value=[parsed]):
            line, = takeoff._pdf_bidtab("public-form.pdf", "public-form.pdf").lines
        self.assertEqual((line.unit, line.quantity), ("100 LF", 45))

    def test_units_of_one_foot_and_unknown_pack_sizes(self):
        engine = ep.ProductionEngine()
        for unit in ("LF", "1 LF", "per 1 LF", "1.0 LF", "CWT", "LS"):
            with self.subTest(unit=unit):
                item = {"unit": unit, "quantity": 45, "description": "Preparing right-of-way"}
                rows, _ = engine.build_items([item])
                expected = unit if unit in ("CWT", "LS") else "LF"
                self.assertEqual((rows[0]["unit"], rows[0]["quantity"]), (expected, 45))
        for unit in ("50 LF", "100,0 LF", "1.2 LF"):
            with self.subTest(unit=unit):
                self.assertEqual(upstream.takeoff_unit(unit), unit.upper())
                with self.assertRaisesRegex(ep.BadRequest, "Confirm its quantity and unit"):
                    engine.build_items([{"unit": unit, "quantity": 45, "description": "Preparing right-of-way"}])

    def test_printed_units_and_dollars_survive_both_exports(self):
        engine = ep.ProductionEngine()
        group = {"item_no": "101S-B", "description": "Preparing right-of-way",
                 "row_id": "r1", "unit": "100 LF", "quantity": 45}
        ordinary = {**group, "row_id": "r2", "item_no": "101-LF", "unit": "LF"}
        items, classes = engine.build_items([group, ordinary])
        canonical, _ = engine.build_items([{**group, "unit": "LF", "quantity": 4500}, ordinary])
        self.assertEqual(items, canonical)

        lines = []
        for item, classification in zip(items, classes):
            trace = {"price": 2.5, "price_key": "family", "quantity_effective": item["quantity"]}
            lines.append(engine._line(classification, item, trace, 1, "municipal"))
        grouped_line, ordinary_line = lines
        self.assertEqual((grouped_line["bid_form_unit"], grouped_line["bid_form_quantity"]), ("100 LF", 45))
        self.assertEqual((grouped_line["amount_p50"], ordinary_line["amount_p50"]), (11250, 112.5))

        edits = [{"row_id": line["row_id"], "your_unit": 2.5, "src": "you"} for line in lines]
        sheet = {"lines": edits, "contingency_pct": 0}
        result = {"lines": lines, "headline": {"p50": 11362.5},
                  "inputs": {"market": "municipal"}, "totals": {}}
        totals = worksheet.totals(result, sheet)
        self.assertEqual(totals["your_opcc"], 11362.5)
        grouped_values, ordinary_values = worksheet.opcc_rows(totals, sheet)[:2]
        self.assertEqual(grouped_values[2:4], ["100 LF", 45])
        self.assertEqual(tuple(map(float, grouped_values[4:6])), (250, 11250))
        self.assertEqual(ordinary_values[2:4], ["LF", 45])

        workbook = openpyxl.load_workbook(io.BytesIO(worksheet.to_xlsx(result, sheet, None)), data_only=True)
        try:
            opcc = workbook["OPCC"]
            for code, expected in (("101S-B", ["100 LF", 45, 250, 11250]), ("101-LF", ["LF", 45, 2.5, 112.5])):
                row = next(cell.row for cell in opcc["A"] if cell.value == code)
                self.assertEqual([opcc.cell(row, col).value for col in (3, 4, 5, 6)], expected)
        finally:
            workbook.close()

        with fitz.open(stream=worksheet.to_pdf(result, sheet, None), filetype="pdf") as pdf:
            text = " ".join(" ".join(page.get_text().split()) for page in pdf)
        self.assertIn("100 LF 45 $250 $11,250", text)
        self.assertIn("LF 45 $2.50 $113", text)
        self.assertIn("$11,363", text)

    def test_printed_rounding_matches_spreadsheet_half_ties(self):
        self.assertEqual(worksheet._usd(112.5), "$113")
        self.assertEqual(worksheet._usd(-112.5), "$-113")
        self.assertEqual(worksheet._usd(112.49), "$112")
        self.assertEqual(worksheet._usd(2.505, cents=True), "$2.51")


if __name__ == "__main__":
    unittest.main()
