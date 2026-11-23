"""Fuzz tests of reading a takeoff (QA 2026-11-20).

User: "whatever I enter, if you can't read it, show that you can't -- don't
let the page fall apart." Over 300 generated inputs -- random garbage, binary
bytes, emoji, right-to-left text, control characters, a 50,000-character
line, 5,000 tiny lines, only tabs / commas / numbers, CSV with broken quotes,
empty and corrupt spreadsheets and PDFs, wrong file types, an empty file --
go through the API in-process (app.dispatch). Every answer must be 200 with
line items or the structured {unreadable: {reason, hint}}, within a time
limit; never a 500. Lines that come back are classified and estimated, and
those calls must not fail either.

    .venv/bin/python -m unittest ui/test_fuzz.py        (or ui/run.py --test)
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import time
import unittest
from unittest import mock
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app                        # noqa: E402

SEED = 20261120
TIME_LIMIT_S = 10.0


def setUpModule():
    if not app.ENGINE.state.get("ready"):
        app.ENGINE.load()


def call(method, path, body=b"", ctype="application/json"):
    t0 = time.time()
    st, ct, out, _ = app.dispatch(method, path, {"Content-Type": ctype}, body)
    return st, (json.loads(out) if "json" in ct else out), time.time() - t0


def multipart(name, data):
    b = "----fuzzb0undary"
    body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
             % (b, name)).encode("utf-8") + data + ("\r\n--%s--\r\n" % b).encode())
    return body, "multipart/form-data; boundary=%s" % b


def text_cases(rng):
    """Over 250 nasty pastes."""
    out = []
    alphabet = ("abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                "\t\t ,,,;;||..--$$%%\"\"''()[]{}<>/\\\n\n")
    for k in range(90):                                   # random garbage
        out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 400))))
    for k in range(30):                                   # any unicode
        out.append("".join(chr(rng.randint(1, 0x2FFF)) for _ in range(rng.randint(1, 300))))
    for k in range(20):                                   # emoji and RTL
        out.append("\n".join("%d\t%s\t%s\t%s" % (
            i, rng.choice(["Rock riprap \U0001F600", "مرحبا שלום",
                           "‮LS‬ Mobilization", "\U0001F477‍♂️ Trench safety",
                           "Block sod אבג", "﻿BOM item"]),
            rng.choice(["SY", "LS", "\U0001F4CF", "ش", ""]),
            rng.choice(["1,200", "١٢٣", "\U0001F522", "7"])) for i in range(rng.randint(1, 12))))
    for k in range(20):                                   # control characters
        out.append("".join(chr(rng.randint(0, 31)) + rng.choice(["Rock", "LS", "12", "\x7f"])
                           for _ in range(rng.randint(1, 60))))
    out.append("Rock riprap " * 4200)                     # one 50,000-character line
    out.append("A" * 50000 + "\tSY\t10")                 # one 50,000-character word
    out.append("\n".join(rng.choice(["a", "1", "x y", "--", ",", "\t", "LS"]) for _ in range(5000)))
    out.append("\n".join(rng.choice(["101 Rock riprap SY 10", "x"]) for _ in range(1900)))
    for filler in ("\t", ",", "|", ";", " ", "\n", "0", "1,234", "$"):
        out.append(filler * rng.randint(1, 3000))         # only tabs / commas / numbers
    for k in range(25):                                   # numbers only
        out.append("\n".join(",".join(str(rng.randint(0, 99999)) for _ in range(rng.randint(1, 8)))
                             for _ in range(rng.randint(1, 40))))
    for k in range(25):                                   # CSV with broken quotes
        rows = []
        for i in range(rng.randint(2, 15)):
            rows.append('%d,"Rock riprap, 18 inch%s,SY,%s' % (
                i, rng.choice(['', '"', '""', '"""']), rng.choice(['1,200', '"12', '3"', ''])))
        out.append("\n".join(rows))
    for k in range(25):                                   # a real paste, damaged
        base = ["101\tMobilization\tLS\t1", "102\tRock Riprap, 18 inch\tSY\t2,600",
                "103\tBlock Sod\tSY\t9,500", "104\tSilt Fence\tLF\t1,450"]
        ls = []
        for l in base:
            cells = l.split("\t")
            op = rng.randint(0, 5)
            if op == 0:
                cells.insert(rng.randint(0, len(cells)), "")
            elif op == 1:
                del cells[rng.randint(0, len(cells) - 1)]
            elif op == 2:
                cells[rng.randint(0, len(cells) - 1)] = "<script>alert(1)</script>"
            elif op == 3:
                cells = [c * rng.randint(1, 30) for c in cells]
            ls.append(rng.choice(["\t", "  ", " | ", ","]).join(cells))
        out.append("\n".join(ls))
    out += ["", " ", "\n\n\n", "\x00", "\x00" * 1000, "null", "{}", "[]", "\"\"",
            '"><b>bold</b>', "<img src=x onerror=alert(1)>\tLS\t1"]
    return out


def file_cases(rng):
    """Over 50 nasty uploads: (file name, bytes)."""
    out = [("empty.csv", b""), ("empty.xlsx", b""), ("empty.pdf", b""),
           ("program.exe", b"MZ\x90\x00" + bytes(rng.getrandbits(8) for _ in range(4000))),
           ("archive.zip", b"PK\x03\x04" + bytes(rng.getrandbits(8) for _ in range(3000))),
           ("picture.png", b"\x89PNG\r\n\x1a\n" + bytes(rng.getrandbits(8) for _ in range(2000))),
           ("noext", b"101 Rock riprap SY 10"), ("weird.name.CSV", b"101,Rock riprap,SY,10\n"),
           ("../../etc/passwd.csv", b"101,Rock riprap,SY,10\n"),
           ("legacy.xls", b"\xd0\xcf\x11\xe0" + bytes(200)), ("old.doc", b"\xd0\xcf\x11\xe0")]
    for k in range(15):                                   # random bytes as each type
        ext = rng.choice([".csv", ".xlsx", ".pdf", ".docx", ".txt", ".tsv"])
        out.append(("random%d%s" % (k, ext),
                    bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 5000)))))
    for k in range(6):                                    # a zip that is not a workbook
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("hello.txt", "not a workbook %d" % k)
        out.append(("fake%d.xlsx" % k, buf.getvalue()))
    for k in range(6):                                    # a PDF cut short
        pdf = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
               b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
        out.append(("cut%d.pdf" % k, pdf[:rng.randint(5, len(pdf))]))
    for k in range(8):                                    # text with odd encodings
        s = "101,Röck rïprap — 18″,SY,1 200\n102,Block sod,SY,9,500\n"
        enc = rng.choice(["utf-16", "utf-16-le", "latin-1", "cp1252", "utf-8-sig", "utf-32"])
        try:
            data = s.encode(enc)
        except UnicodeEncodeError:
            data = s.encode("utf-8")
        out.append(("enc%d.csv" % k, data))
    try:
        import openpyxl
        for k in range(4):                                # real workbooks, nasty cells
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["Item", "Description", "Unit", "Qty"])
            for i in range(rng.randint(0, 30)):
                ws.append([rng.choice([i, None, "x"]), rng.choice(["Rock riprap", None, "", "=1+1", "‮LS"]),
                           rng.choice(["SY", None, 12]), rng.choice([10, None, "abc", -5, 1e12])])
            buf = io.BytesIO()
            wb.save(buf)
            out.append(("book%d.xlsx" % k, buf.getvalue()))
    except ImportError:
        pass
    return out


class FuzzIntake(unittest.TestCase):
    """Never a 500; lines or a plain 'could not read', in time."""

    @classmethod
    def setUpClass(cls):
        rng = random.Random(SEED)
        cls.texts = text_cases(rng)
        cls.files = file_cases(rng)

    def check_reply(self, st, body, dt, label):
        self.assertNotEqual(st, 500, label)
        self.assertLess(dt, TIME_LIMIT_S, label)
        if st == 413:                     # over the upload cap: a plain message
            self.assertIn("detail", body)
            return None
        self.assertEqual(st, 200, (label, body if isinstance(body, dict) else ""))
        self.assertIsInstance(body.get("lines"), list, label)
        if not body["lines"]:
            u = body.get("unreadable") or {}
            self.assertTrue(u.get("reason"), label)
            self.assertTrue(u.get("hint"), label)
        for l in body["lines"]:
            self.assertLessEqual(len(l["description"]), 300, label)
        return body

    def follow_through(self, body, label):
        """Classify and estimate what was read: no 500 there either."""
        lines = [{"row_id": "r%d" % i, "description": l["description"], "unit": l["unit"],
                  "quantity": l["quantity"], "item_no": l["item_no"]}
                 for i, l in enumerate(body["lines"][:400], 1)]
        if not lines:
            return
        st, out, dt = call("POST", "/api/classify",
                           json.dumps({"market": "municipal", "lines": lines}).encode())
        self.assertEqual(st, 200, label)
        self.assertLess(dt, TIME_LIMIT_S, label)
        req = {"market": "municipal", "n_bidders": "auto", "bid_date": "2026-11-20",
               "items": [dict(l, family="", amount=None, quote=None) for l in lines
                         if isinstance(l["quantity"], (int, float)) and l["quantity"] > 0]}
        if not req["items"]:
            return
        st, out, dt = call("POST", "/api/estimate", json.dumps(req).encode())
        self.assertIn(st, (200, 400), label)
        self.assertLess(dt, TIME_LIMIT_S, label)

    def test_at_least_300_cases(self):
        self.assertGreaterEqual(len(self.texts) + len(self.files), 300)

    def test_pasted_text(self):
        for k, text in enumerate(self.texts):
            label = "text #%d (%r...)" % (k, text[:40])
            with self.subTest(case=k):
                st, body, dt = call("POST", "/api/takeoff/text",
                                    json.dumps({"text": text}).encode())
                body = self.check_reply(st, body, dt, label)
                if body is not None and k % 3 == 0:
                    self.follow_through(body, label)

    def test_uploaded_files(self):
        for k, (name, data) in enumerate(self.files):
            label = "file #%d %s" % (k, name)
            with self.subTest(case=k, name=name):
                body, ctype = multipart(name, data)
                st, out, dt = call("POST", "/api/takeoff/file", body, ctype)
                out = self.check_reply(st, out, dt, label)
                if out is not None:
                    self.follow_through(out, label)

    def test_limits_say_so_plainly(self):
        st, body, _ = call("POST", "/api/takeoff/text", json.dumps(
            {"text": "\n".join("%d Rock riprap SY 10" % i for i in range(5000))}).encode())
        self.assertEqual(st, 200)
        self.assertIn("reads up to", body["unreadable"]["reason"])
        st, body, _ = call("POST", "/api/takeoff/text", json.dumps(
            {"text": "x" * 600000}).encode())
        self.assertEqual(st, 200)
        self.assertIn("characters long", body["unreadable"]["reason"])
        lines = [{"row_id": "r%d" % i, "description": "Rock", "unit": "SY", "quantity": 1}
                 for i in range(2500)]
        st, body, _ = call("POST", "/api/classify", json.dumps(
            {"market": "municipal", "lines": lines}).encode())
        self.assertEqual(st, 400)
        self.assertIn("Split the takeoff", body["detail"])

    def test_non_text_json_bodies(self):
        for body in (b"\xff\xfe\x00", b"[1,2]", b'{"text": 5}', b'{"text": null}',
                     b'{"text": ["a"]}', b"{" * 1000):
            st, out, _ = call("POST", "/api/takeoff/text", body)
            self.assertIn(st, (200, 400), body[:20])
            self.assertNotEqual(st, 500)


def _latin1(seed, n=600):
    rng = random.Random(seed)
    return "".join(chr(rng.randint(0, 255)) for _ in range(n))


#: The coordinator's cases (2026-11-20, "what about garbled input?"), through the API: what
#: must come back. "unreadable" = no line item, the plain verdict, the lines
#: listed as could-not-read; otherwise the exact items.
OUTCOMES = [
    ("symbols and keyboard mash", "asdkj@#$%^&*()_+ ~~ ||||", "unreadable"),
    ("600 random latin-1 characters", _latin1(1), "unreadable"),
    ("keyboard mash on two lines", "fjdksla;fjdksla\nqwerqwer zxcv", "unreadable"),
    ("two lines of Chinese prose", "\u8fd9\u662f\u4e00\u6bb5\u4e2d\u6587\u8bf4\u660e\uff0c\u4e0d\u662f\u5de5\u7a0b\u91cf\u6e05\u5355\u3002\n\u8bf7\u628a\u8868\u683c\u7c98\u8d34\u5230\u8fd9\u91cc\uff0c\u8c22\u8c22\u3002",
     "unreadable"),
    ("JSON", '{"a": [1,2,3]}', "unreadable"),
    ('"x" * 20000', "x" * 20000, "unreadable"),
    ("numbers only", "1 2 3 4 5 6 7 8 9\n10 11 12 13", "unreadable"),
    ("mojibake", "ç¬¬ä¸€è¡Œ 12 LF 3.5", "unreadable"),
    ("zero-width, joiner, RTL override", "101​ Mobili‍zation‮ LS 1",
     [("101", "Mobilization", "LS", 1.0)]),
    ("zero-width space in the quantity", "102 Rip​rap CY 4​200",
     [("102", "Riprap", "CY", 4200.0)]),
    ("leading emoji", "\U0001F6A7\U0001F6A7 101 \U0001F4A5 Mobilization LS 1",
     [("101", "Mobilization", "LS", 1.0)]),
    ("headings only", "BASE BID\nSTORM DRAINAGE\n", "unreadable"),
]


class GarbageOutcomes(unittest.TestCase):
    """Garbage comes back as "Couldn't read", never as line items."""

    def post(self, text):
        st, body, dt = call("POST", "/api/takeoff/text", json.dumps({"text": text}).encode())
        self.assertEqual(st, 200)
        self.assertLess(dt, TIME_LIMIT_S)
        return body

    def test_each_case_comes_back_as_specified(self):
        for label, text, want in OUTCOMES:
            with self.subTest(label):
                body = self.post(text)
                if want == "unreadable":
                    self.assertEqual(body["lines"], [], label)
                    self.assertEqual(body["unreadable"]["reason"], app.NO_ITEMS)
                    self.assertEqual(body["unreadable"]["hint"], app.READ_HINT)
                    self.assertGreaterEqual(body["n_unread"] + body["n_skipped"], 1)
                else:
                    self.assertNotIn("unreadable", body)
                    self.assertEqual([(l["item_no"], l["description"], l["unit"], l["quantity"])
                                      for l in body["lines"]], want, label)
                    self.assertEqual(body["n_unread"], 0)
                for l in body["lines"]:
                    for ch in l["description"]:
                        self.assertNotIn(ch, "​‍‮﻿�", label)

    def test_random_bytes_never_become_items(self):
        for seed in range(1, 31):
            with self.subTest(seed=seed):
                body = self.post(_latin1(seed))
                self.assertEqual(body["lines"], [], seed)
                self.assertTrue(body["unreadable"])

    def test_the_readable_lines_of_a_mixed_paste_are_estimated(self):
        body = self.post("101 Mobilization LS 1\nç¬¬ä¸€è¡Œ 12 LF 3.5\n"
                         "102 Rock riprap SY 1,200\nasdkj@#$%^&*()_+ ~~ ||||\n")
        self.assertEqual([l["item_no"] for l in body["lines"]], ["101", "102"])
        self.assertEqual(body["n_unread"], 2)
        self.assertNotIn("unreadable", body)
        items = [dict(row_id="r%d" % i, description=l["description"], unit=l["unit"],
                      quantity=l["quantity"], item_no=l["item_no"], family="", amount=None,
                      quote=None) for i, l in enumerate(body["lines"], 1)]
        st, out, _ = call("POST", "/api/estimate", json.dumps(
            {"market": "municipal", "n_bidders": "auto", "bid_date": "2026-11-20",
             "items": items}).encode())
        self.assertEqual(st, 200)
        self.assertEqual(len(out["lines"]), 2)

    def test_a_file_of_garbage_is_unreadable_too(self):
        body, ctype = multipart("noise.csv", _latin1(7).encode("latin-1"))
        st, out, _ = call("POST", "/api/takeoff/file", body, ctype)
        self.assertEqual(st, 200)
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["unreadable"]["reason"], app.NO_ITEMS)
        body, ctype = multipart("legacy.xls", b"\xd0\xcf\x11\xe0" + bytes(64))
        st, out, _ = call("POST", "/api/takeoff/file", body, ctype)
        self.assertIn(".xls", out["unreadable"]["reason"])       # a file-level reason stays


class RequestValidation(unittest.TestCase):
    """Malformed fields must give useful 400s without changing valid prices."""

    def request(self, **changes):
        payload = {"market": "municipal", "n_bidders": 5,
                   "bid_date": "2025-03-10",
                   "items": [{"row_id": "r1", "item_no": "1",
                              "description": "Rock riprap", "unit": "SY",
                              "quantity": 100}]}
        payload.update(changes)
        return payload

    def estimate(self, payload):
        st, out, _ = call("POST", "/api/estimate", json.dumps(payload).encode())
        return st, out

    def test_worksheet_references_wait_for_the_index_during_startup(self):
        with mock.patch.object(app.ENGINE, "refs", None), mock.patch.object(
                app.ENGINE, "refs_state", {"ready": False, "error": ""}):
            st, out, _ = call("POST", "/api/worksheet/refs", json.dumps(
                {"request": self.request()}).encode())
            self.assertEqual(st, 503)
            self.assertIn("still loading", out["detail"])
            with self.assertRaises(app.ep.EngineNotReady):
                app.ENGINE.top_references(self.request())

    def test_wrong_items_types_are_400(self):
        for value in (1, True, False, {}, "text", [None], [1]):
            with self.subTest(value=value):
                st, out = self.estimate(self.request(items=value))
                self.assertEqual(st, 400)
                self.assertIn("items", out["detail"])

    def test_fractional_boolean_or_nonfinite_bidders_are_400(self):
        for value in (1.5, True, False, float("inf"), float("-inf"),
                      float("nan"), "1.5", [], {}):
            with self.subTest(value=repr(value)):
                st, out = self.estimate(self.request(n_bidders=value))
                self.assertEqual(st, 400)
                self.assertTrue(out["detail"])

    def test_valid_whole_number_bidders_keep_the_same_prices(self):
        st, expected = self.estimate(self.request())
        self.assertEqual(st, 200)
        for value in (5.0, "5", " 5 "):
            with self.subTest(value=value):
                st, out = self.estimate(self.request(n_bidders=value))
                self.assertEqual(st, 200)
                self.assertEqual(out["inputs"]["n_bidders"], 5)
                self.assertEqual(out["headline"], expected["headline"])
                self.assertEqual(out["lines"], expected["lines"])

    def test_invalid_and_deep_json_are_400(self):
        bodies = (b"\xff", b"{", b"[]", b'{"text":NaN}',
                  b'{"text":Infinity}', b'{"text":1e999}',
                  b'{"text":' + b"[" * 200 + b"0" + b"]" * 200 + b"}")
        for body in bodies:
            with self.subTest(body=body[:30]):
                st, out, _ = call("POST", "/api/takeoff/text", body)
                self.assertEqual(st, 400)
                self.assertTrue(out["detail"])

    def test_unsafe_line_numbers_are_400(self):
        for field in ("quantity", "amount", "quote", "amount_pct"):
            for value in (1e308, -(10 ** 400), "Infinity", "NaN", True):
                with self.subTest(field=field, value=repr(value)[:30]):
                    payload = self.request()
                    payload["items"][0][field] = value
                    st, out = self.estimate(payload)
                    self.assertEqual(st, 400)
                    self.assertIn(field, out["detail"])

    def test_duplicate_row_ids_are_400(self):
        for second_id in ("r1", None):
            payload = self.request()
            first = dict(payload["items"][0], row_id="r2")
            second = dict(first, row_id=second_id, item_no="2")
            if second_id is not None:
                first["row_id"] = second_id
            payload["items"] = [first, second]
            st, out = self.estimate(payload)
            self.assertEqual(st, 400)
            self.assertIn("different row_id", out["detail"])

    def test_excluded_duplicate_does_not_affect_prices(self):
        payload = self.request()
        st, expected = self.estimate(payload)
        self.assertEqual(st, 200)
        payload["items"].append(dict(payload["items"][0], include=False))
        st, out = self.estimate(payload)
        self.assertEqual(st, 200)
        self.assertEqual(out["headline"], expected["headline"])
        self.assertEqual(out["lines"], expected["lines"])


class ExportTextValidation(unittest.TestCase):
    def export_payload(self):
        return {"result": {"inputs": {}, "headline": {"p50": 10000, "p80": 12000},
                           "lines": [{"row_id": "r1", "item_no": "1",
                                      "description": "Rock riprap", "unit": "SY",
                                      "quantity": 100, "status": "priced"}]},
                "worksheet": {"project_name": "Synthetic project", "contingency_pct": 10,
                              "lines": [{"row_id": "r1", "your_unit": 100, "src": "you"}]}}

    def test_unsafe_worksheet_numbers_are_400_before_either_export(self):
        for kind in ("xlsx", "pdf"):
            for field in ("your_unit", "your_amount", "quantity", "contingency_pct"):
                for value in (1e308, 10 ** 400, "1e308", "Infinity", "NaN"):
                    with self.subTest(kind=kind, field=field, value=repr(value)[:30]):
                        payload = self.export_payload()
                        if field == "quantity":
                            payload["result"]["lines"][0][field] = value
                        elif field == "contingency_pct":
                            payload["worksheet"][field] = value
                        else:
                            payload["worksheet"]["lines"][0][field] = value
                        st, out, _ = call("POST", "/api/worksheet/export/" + kind,
                                          json.dumps(payload).encode())
                        self.assertEqual(st, 400)
                        self.assertIn("too large", out["detail"])

    def test_normal_worksheet_totals_and_cached_values_are_unchanged(self):
        import worksheet
        import openpyxl
        payload = self.export_payload()
        totals = worksheet.totals(payload["result"], payload["worksheet"])
        self.assertEqual(totals["subtotal"], 10000)
        self.assertEqual(totals["contingency"], 1000)
        self.assertEqual(totals["your_opcc"], 11000)
        for kind in ("xlsx", "pdf"):
            st, ct, data, _ = app.dispatch(
                "POST", "/api/worksheet/export/" + kind,
                {"Content-Type": "application/json"}, json.dumps(payload).encode())
            self.assertEqual(st, 200)
            if kind == "xlsx":
                wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
                self.assertEqual(wb["OPCC"]["F9"].value, 10000)
                self.assertIn(11000, [cell.value for cell in wb["OPCC"]["F"]])
            else:
                self.assertTrue(data.startswith(b"%PDF"))

    def test_control_characters_do_not_break_the_worksheet_export(self):
        import openpyxl
        result = {"inputs": {"owner_label": "City\x00A"},
                  "headline": {"p50": 100, "p80": 120},
                  "lines": [{"row_id": "r1", "item_no": "1\x00",
                             "description": "Rock\x0briprap \u00d8 \u2013",
                             "unit": "SY\x01", "quantity": 1,
                             "status": "priced"}]}
        ws = {"project_name": "Bad\x00Title", "prepared_by": "A\x0bB",
              "project_no": "P\x1f1", "contingency_pct": 10,
              "lines": [{"row_id": "r1", "your_unit": 100, "src": "you",
                         "src_label": "typed\x0fprice",
                         "note": "=1+1\x00\nNext line\tkept"}]}
        payload = {"result": result, "worksheet": ws}
        original = json.dumps(payload)
        st, ct, data, _ = app.dispatch(
            "POST", "/api/worksheet/export/xlsx",
            {"Content-Type": "application/json"}, original.encode())
        self.assertEqual(st, 200)
        self.assertIn("spreadsheet", ct)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb["OPCC"]["A3"].value, "BadTitle")
        self.assertEqual(wb["OPCC"]["F4"].value, "AB")
        self.assertEqual(wb["OPCC"]["F5"].value, "P1")
        self.assertEqual(wb["OPCC"]["A9"].value, "1")
        self.assertEqual(wb["OPCC"]["B9"].value, "Rockriprap \u00d8 \u2013")
        self.assertEqual(wb["OPCC"]["C9"].value, "SY")
        self.assertEqual(wb["OPCC"]["G9"].value, "typedprice")
        self.assertEqual(wb["OPCC"]["H9"].value, "=1+1\nNext line\tkept")
        self.assertEqual(wb["OPCC"]["H9"].data_type, "s")
        self.assertEqual(json.dumps(payload), original)
        for sh in wb.worksheets:
            for row in sh.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        self.assertNotRegex(cell.value, r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
        cached = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        self.assertEqual(cached["OPCC"]["F9"].value, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
