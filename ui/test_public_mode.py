"""Public-demo mode (ui/public_mode.py): what is off, what the browser keeps,
the access code, rate limits, the upload cap, headers -- and that the local
tool is unchanged when the mode is off.

    .venv/bin/python -m unittest ui/test_public_mode.py   (from the repo root)

None of these tests loads the estimator tables; every test puts the process
back into local mode when it ends.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app  # noqa: E402
import intake  # noqa: E402
import lettings  # noqa: E402
import public_mode as pm  # noqa: E402

FAMILIES = {"riprap", "excavation", "gabion"}


class PublicBase(unittest.TestCase):
    env = {"BIDEST_PUBLIC": "1"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pubmode_test_")
        self.tempdir_before = tempfile.tempdir
        self._env = mock.patch.dict(os.environ, {
            "BID_UI_CORRECTIONS": os.path.join(self.tmp, "corr.json"),
            "BID_UI_WORKSHEETS": os.path.join(self.tmp, "ws")})
        self._env.start()
        pm.configure(dict(self.env))
        self._fam = mock.patch.object(app, "_known_families",
                                      lambda: set(FAMILIES))
        self._fam.start()

    def tearDown(self):
        self._fam.stop()
        pm.configure({})
        self._env.stop()
        self.assertEqual(tempfile.tempdir, self.tempdir_before)
        for dp, dns, fns in os.walk(self.tmp, topdown=False):
            for f in fns:
                os.remove(os.path.join(dp, f))
            for d in dns:
                os.rmdir(os.path.join(dp, d))
        os.rmdir(self.tmp)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        st, ctype, out, extra = app.dispatch(method, path, h, data)
        return st, (json.loads(out) if "json" in ctype else out), extra


class OffInPublicMode(PublicBase):
    def test_blocked_routes_answer_404(self):
        for method, path in [("POST", "/api/claude/preview"),
                             ("POST", "/api/claude/count?consent=yes"),
                             ("POST", "/api/claude/read?consent=yes"),
                             ("POST", "/api/bidtab/parse"),
                             ("GET", "/api/bidtab/inbox"),
                             ("GET", "/api/source-file?path=external/x/y.pdf"),
                             ("POST", "/api/worksheet/save"),
                             ("GET", "/api/worksheet/list"),
                             ("GET", "/api/worksheet/load?file=a.json")]:
            st, d, _ = self.call(method, path, {})
            self.assertEqual(st, 404, path)
            self.assertIn("public demo", d["detail"])

    def test_claude_status_is_off_without_probing(self):
        with mock.patch.object(app.claude_reader, "status",
                               side_effect=AssertionError("probed")):
            st, d, _ = self.call("GET", "/api/claude/status")
            self.assertEqual(st, 200)
            self.assertFalse(d["available"])
            st, d, _ = self.call("GET", "/api/status")
            self.assertEqual(st, 200)
            self.assertFalse(d["claude_reader"]["available"])

    def test_status_has_the_public_block(self):
        st, d, _ = self.call("GET", "/api/status")
        self.assertEqual(st, 200)
        p = d["public"]
        self.assertTrue(p["enabled"])
        self.assertEqual(p["banner"], pm.BANNER)
        self.assertEqual(p["max_upload_mb"], 25)
        self.assertFalse(p["access_code"])
        self.assertEqual(d["corrections"]["mode"], "browser")
        self.assertFalse(d["bidtab_import"]["available"])
        self.assertNotIn(os.path.dirname(HERE), json.dumps(d["corrections"]))

    def test_health(self):
        st, d, _ = self.call("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])

    def test_banner_text_is_on_the_page(self):
        with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as fh:
            page = " ".join(fh.read().split())
        self.assertIn("Files you upload are processed on this server and not "
                      "stored. Do not upload confidential documents.", page)
        # the Claude PDF button is folded into AI mode, which is on the
        # public page too (a kept place, not built: nothing is sent)
        self.assertNotIn('id="btn-claude"', page)
        self.assertIn('id="btn-ai-mode"', page)
        self.assertIn("local-only", page.split('id="bidtab-fold"')[0][-80:])

    def test_page_is_marked_public(self):
        st, page, _ = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b'<html lang="en" data-public="1">', page)

    def test_errors_carry_no_traceback(self):
        def boom(req):
            raise RuntimeError("secret detail /some/path")
        with mock.patch.dict(app.ROUTES, {("GET", "/api/boom"): boom}), \
                self.assertLogs("ui", level="ERROR") as logged:
            st, d, _ = self.call("GET", "/api/boom")
        self.assertIn("secret detail", "\n".join(logged.output))  # kept here
        self.assertEqual(st, 500)
        self.assertNotIn("traceback", d)
        self.assertNotIn("secret detail", json.dumps(d))


class BrowserKeepsCorrections(PublicBase):
    def test_save_and_forget_write_nothing(self):
        st, d, _ = self.call("POST", "/api/corrections", {
            "description": "Grouted Rock Riprap, 18 inch", "family": "riprap",
            "unit": "sy"})
        self.assertEqual(st, 200)
        self.assertEqual(d["stored"], "browser")
        self.assertEqual(d["key"], intake.Corrections.key(
            "Grouted Rock Riprap, 18 inch"))
        self.assertEqual(d["saved"]["family"], "riprap")
        st, d2, _ = self.call("POST", "/api/corrections/forget", {
            "description": "Grouted Rock Riprap, 18 inch"})
        self.assertEqual(st, 200)
        self.assertEqual(d2["key"], d["key"])
        self.assertFalse(os.path.exists(os.environ["BID_UI_CORRECTIONS"]))
        st, d3, _ = self.call("GET", "/api/corrections")
        self.assertEqual(d3["mode"], "browser")

    def test_unknown_family_refused(self):
        st, d, _ = self.call("POST", "/api/corrections", {
            "description": "x", "family": "not_a_family"})
        self.assertEqual(st, 400)

    def test_request_corrections_are_validated(self):
        key = intake.Corrections.key("Gabion Mattress 12 inch")
        bc = intake.BrowserCorrections({
            key: {"family": "gabion", "unit": "sy", "times": "3"},
            "bad family": {"family": "nope"},
            "no family": {"unit": "LF"},
            7: {"family": "gabion"},
            "x" * 500: {"family": "gabion"},
        }, FAMILIES)
        self.assertEqual(bc.status()["entries"], 1)
        e = bc.get("GABION  mattress 12 inch")
        self.assertEqual((e["family"], e["unit"], e["times"]), ("gabion", "SY", 3))
        self.assertIsNone(bc.get("something else"))
        self.assertEqual(intake.BrowserCorrections("junk").status()["entries"], 0)
        many = {"k%d" % i: {"family": "gabion"} for i in range(5000)}
        self.assertEqual(intake.BrowserCorrections(many, FAMILIES)
                         .status()["entries"],
                         intake.BrowserCorrections.MAX_ENTRIES)


class LinksInPublicMode(PublicBase):
    def test_no_local_copy_and_precomputed_links(self):
        f = "external/someowner/2024-01-01_a.pdf"
        links = os.path.join(self.tmp, "links.json")
        with open(links, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "links": {
                f: {"url": "https://example.gov/a.pdf", "kind": "council agenda attachment"},
                "external/x/b.pdf": {"url": "javascript:alert(1)"}}}, fh)
        with mock.patch.dict(os.environ, {lettings.LINKS_ENV: links}), \
                mock.patch.object(lettings, "resolve_source",
                                  side_effect=AssertionError("local file")):
            src = lettings.SourceLinks().get(f + "::L1")
            self.assertEqual(src["url"], "https://example.gov/a.pdf")
            self.assertEqual(src["local_path"], "")
            self.assertEqual(lettings.load_links_file(links).keys(), {f})


class AccessCodeAndLimits(PublicBase):
    env = {"BIDEST_PUBLIC": "1", "DEMO_ACCESS_CODE": "open sesame",
           "BIDEST_MAX_UPLOAD_MB": "1"}

    def setUp(self):
        super().setUp()
        self.srv = app.make_server("127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def login(self):
        st, h, _ = self.req("POST", "/access", body="code=open+sesame&next=/",
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        self.assertEqual(st, 303)
        return h["Set-Cookie"].split(";")[0]

    def test_code_required_once_per_session(self):
        st, h, _ = self.req("GET", "/")
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].startswith("/access"))
        st, h, body = self.req("GET", "/api/status")
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(body)["login"], "/access")
        st, h, body = self.req("GET", "/access")
        self.assertEqual(st, 200)
        self.assertIn(b'name="code"', body)
        st, h, _ = self.req("POST", "/access", body="code=wrong",
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        self.assertEqual(st, 401)
        self.assertNotIn("Set-Cookie", h)
        cookie = self.login()
        self.assertNotIn("open", cookie)                 # an HMAC, not the code
        st, h, body = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(st, 200)
        self.assertIn(b'data-public="1"', body)
        self.assertIn("frame-ancestors 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Python", h.get("Server", ""))
        cookie_attrs = None
        st, h, _ = self.req("POST", "/access", body="code=open+sesame",
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        cookie_attrs = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie_attrs)
        self.assertNotIn("Max-Age", cookie_attrs)        # a session cookie
        self.assertNotIn("Expires", cookie_attrs)
        st, _, _ = self.req("GET", "/api/health")         # open for probes
        self.assertEqual(st, 200)

    def test_redirect_target_stays_on_site(self):
        st, h, _ = self.req("POST", "/access",
                            body="code=open+sesame&next=//evil.example/",
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        self.assertEqual(h["Location"], "/")

    def test_upload_cap(self):
        # refused on the declared length, before the body is read
        cookie = self.login()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(("POST /api/takeoff/file?name=a.csv HTTP/1.1\r\n"
                       "Host: x\r\nCookie: %s\r\nContent-Type: text/csv\r\n"
                       "Content-Length: %d\r\n\r\n" % (cookie, 3 * 1024 * 1024))
                      .encode() + b"x" * 1000)
            out = b""
            while True:
                b = s.recv(4096)
                if not b:
                    break
                out += b
        self.assertTrue(out.startswith(b"HTTP/1.1 413"), out[:80])
        self.assertIn(b"1 MB", out)

    def test_rate_limit(self):
        cookie = self.login()
        codes = []
        for _ in range(int(pm.RATES["upload"][1]) + 3):
            st, h, _ = self.req("POST", "/api/takeoff/text",
                                body=json.dumps({"text": "1\tMobilization\tLS\t1"}),
                                headers={"Cookie": cookie,
                                         "Content-Type": "application/json"})
            codes.append(st)
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)

    def test_login_attempts_are_limited(self):
        codes = []
        for _ in range(int(pm.RATES["login"][1]) + 2):
            st, _, _ = self.req("POST", "/access", body="code=nope",
                                headers={"Content-Type":
                                         "application/x-www-form-urlencoded"})
            codes.append(st)
        self.assertEqual(codes[-1], 429)


class ClientAddress(PublicBase):
    env = {"BIDEST_PUBLIC": "1", "BIDEST_TRUSTED_PROXIES": "1"}

    def test_rightmost_forwarded_address(self):
        h = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
        self.assertEqual(pm.client_ip("10.0.0.2", h), "203.0.113.9")
        self.assertEqual(pm.client_ip("10.0.0.2", {}), "10.0.0.2")
        self.assertEqual(pm.client_ip("10.0.0.2",
                                      {"X-Forwarded-For": "not an ip"}),
                         "10.0.0.2")

    def test_peer_without_trusted_proxies(self):
        pm.configure({"BIDEST_PUBLIC": "1"})
        self.assertEqual(pm.client_ip("10.0.0.2",
                                      {"X-Forwarded-For": "203.0.113.9"}),
                         "10.0.0.2")

    def test_bucket_refills(self):
        t0 = 1000.0
        burst = pm.RATES["heavy"][1]
        waits = [pm.take("198.51.100.1", "heavy", t0) for _ in range(burst + 1)]
        self.assertEqual(waits[:burst], [0.0] * burst)
        self.assertGreater(waits[-1], 0)
        self.assertEqual(pm.take("198.51.100.1", "heavy", t0 + 60), 0.0)


class BoundedConnections(PublicBase):
    env = {"BIDEST_PUBLIC": "1", "BIDEST_MAX_CONNECTIONS": "8"}

    def test_extra_connection_gets_503(self):
        srv = app.make_server("127.0.0.1", 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        held = []
        try:
            for _ in range(8):
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                held.append(s)
            time.sleep(0.3)
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            held.append(s)
            data = s.recv(200)
            self.assertTrue(data.startswith(b"HTTP/1.1 503"), data)
        finally:
            for s in held:
                s.close()
            srv.shutdown()
            srv.server_close()


class LocalModeUnchanged(unittest.TestCase):
    def setUp(self):
        pm.configure({})

    def test_off_by_default(self):
        self.assertFalse(pm.enabled())
        self.assertIsNone(pm.blocked("GET", "/api/source-file")
                          if pm.enabled() else None)
        st, ctype, page, extra = app.dispatch("GET", "/")
        self.assertEqual(st, 200)
        self.assertNotIn(b"data-public", page)
        self.assertNotIn("Content-Security-Policy", extra)

    def test_local_routes_still_answer(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"BID_UI_WORKSHEETS": d}):
            st, _, body, _ = app.dispatch("GET", "/api/worksheet/list")
            self.assertEqual(st, 200)

    def test_flag_values(self):
        for v, on in (("1", True), ("true", True), ("yes", True), ("0", False),
                      ("", False), ("no", False)):
            self.assertEqual(pm.Config({"BIDEST_PUBLIC": v}).enabled, on, v)
        c = pm.Config({"BIDEST_PUBLIC": "1"})
        self.assertEqual((c.port, c.max_upload_mb, c.access_code), (7860, 25, ""))
        self.assertEqual(pm.Config({"PORT": "8080"}).port, 8080)
        self.assertEqual(pm.Config({"BIDEST_FRAME_ANCESTORS":
                                    "'self'; script-src *"}).frame_ancestors,
                         "'self'")


if __name__ == "__main__":
    unittest.main()
