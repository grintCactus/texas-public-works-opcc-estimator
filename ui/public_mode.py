"""Public-demo mode: the same server, safe to put on the open internet.

Off by default -- the local tool is unchanged. It is switched on by
BIDEST_PUBLIC=1 in the environment (or `ui/run.py --public`, which sets it).

Turned off in public mode
  * reading a PDF with Claude, both backends (API and CLI): /api/claude/*
    answers 404 and the status says "off";
  * the bid-tab upload and its inbox (ui/.inbox): /api/bidtab/* answer 404;
  * every endpoint that writes on the server: the corrections memory
    (ui/.local/corrections.json) and saved worksheets (ui/.local/worksheets).
    Both live in the visitor's browser instead (localStorage); the browser
    sends its corrections with each classify request and nothing is kept;
  * serving local original files (/api/source-file). A letting links to its
    recorded source page and to the GitHub mirror (ui/public_mirror.json)
    instead. data/external is not shipped, so the harvest manifests' links
    come from data/clean/source_links.json (see ui/lettings.py).

Uploads
  * takeoffs and worksheets are processed in memory and never stored: the
    parsers need a file path, so a temporary copy exists only while the
    request runs and is deleted when it ends -- in a RAM-backed directory
    when there is one ($BIDEST_TMPDIR, else /dev/shm when it is at least
    256 MB), else in the system temp directory;
  * capped at BIDEST_MAX_UPLOAD_MB (25 MB).

Protection
  * optional access code: with DEMO_ACCESS_CODE set, a visitor enters it
    once per browser session (a session cookie holding an HMAC of the code
    under a per-process key); unset, the site is open. Never hard-coded.
  * per-IP token-bucket rate limits (static files, API, heavy requests,
    uploads, access-code attempts), a cap on concurrent heavy requests and
    on open connections, a socket timeout, no tracebacks in error replies,
    security headers (CSP, nosniff, referrer policy, frame-ancestors).
  * the client IP is the socket peer; with BIDEST_TRUSTED_PROXIES=N (N
    reverse proxies in front: Caddy -> 1) it is the N-th address from the
    right of X-Forwarded-For.

Serving: 0.0.0.0 on $PORT, 7860 by default (the Hugging Face Spaces port).

Settings (environment):
  BIDEST_PUBLIC=1             public mode on
  PORT                        listening port (7860)
  DEMO_ACCESS_CODE            optional access code
  BIDEST_MAX_UPLOAD_MB        upload cap in MB (25)
  BIDEST_TRUSTED_PROXIES      reverse proxies in front (0)
  BIDEST_FRAME_ANCESTORS      CSP frame-ancestors ('self'; a Hugging Face
                              Space embeds the app: https://huggingface.co)
  BIDEST_TMPDIR               RAM-backed temp directory (a tmpfs mount)
  BIDEST_MAX_HEAVY            concurrent heavy requests (4)
  BIDEST_MAX_CONNECTIONS      open connections (128)
  BIDEST_RATE_SCALE           multiply every rate limit (1.0)
  BIDEST_REPO_URL             the source repository shown on the page
"""

from __future__ import annotations

import base64
import collections
import contextlib
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from typing import Dict, Optional, Tuple

log = logging.getLogger("ui.public")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

BANNER = ("Files you upload are processed on this server and not stored. "
          "Do not upload confidential documents.")
DEFAULT_PORT = 7860
DEFAULT_REPO = "https://github.com/grintCactus/texas-public-works-opcc-estimator"
COOKIE = "bidest_access"
ACCESS_PATH = "/access"

#: Routes that do not exist in public mode, with the reason given.
BLOCKED = {
    ("POST", "/api/claude/preview"): "Reading a PDF with Claude",
    ("POST", "/api/claude/count"): "Reading a PDF with Claude",
    ("POST", "/api/claude/read"): "Reading a PDF with Claude",
    ("POST", "/api/bidtab/parse"): "Adding a bid tabulation",
    ("GET", "/api/bidtab/inbox"): "The bid-tabulation inbox",
    ("GET", "/api/source-file"): "Local copies of original documents",
    ("POST", "/api/worksheet/save"): "Saving worksheets on the server",
    ("GET", "/api/worksheet/list"): "Saving worksheets on the server",
    ("GET", "/api/worksheet/load"): "Saving worksheets on the server",
}

#: Rate classes: (requests per minute, burst).
RATES = {
    "static": (300, 120),
    "api": (240, 120),
    "heavy": (60, 20),
    "upload": (12, 6),
    "login": (6, 10),
}
HEAVY = {"/api/estimate", "/api/references", "/api/worksheet/refs",
         "/api/export/csv", "/api/export/xlsx", "/api/worksheet/export/csv",
         "/api/worksheet/export/xlsx", "/api/worksheet/export/pdf", "/api/letting",
         "/api/letting/csv",
         "/api/bidders/suggest"}
UPLOAD = {"/api/takeoff/file", "/api/takeoff/text"}
#: Answered without the access code (container health checks).
OPEN_PATHS = {"/api/health", ACCESS_PATH, "/favicon.ico"}


class Config:
    """Public-mode settings, read from the environment by configure()."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.enabled = str(env.get("BIDEST_PUBLIC", "")).strip().lower() in (
            "1", "true", "yes", "on")
        self.access_code = str(env.get("DEMO_ACCESS_CODE") or "").strip()
        self.max_upload_mb = _int(env.get("BIDEST_MAX_UPLOAD_MB"), 25, 1, 512)
        self.trusted_proxies = _int(env.get("BIDEST_TRUSTED_PROXIES"), 0, 0, 5)
        fa = str(env.get("BIDEST_FRAME_ANCESTORS") or "").strip()
        self.frame_ancestors = (fa if re.fullmatch(r"[A-Za-z0-9 :/.'*_-]+", fa)
                                else "'self'")
        self.max_heavy = _int(env.get("BIDEST_MAX_HEAVY"), 4, 1, 64)
        self.max_connections = _int(env.get("BIDEST_MAX_CONNECTIONS"), 128,
                                    8, 4096)
        try:
            self.rate_scale = max(0.01, float(env.get("BIDEST_RATE_SCALE")
                                              or 1.0))
        except ValueError:
            self.rate_scale = 1.0
        self.socket_timeout = 30.0
        repo = str(env.get("BIDEST_REPO_URL") or DEFAULT_REPO).strip()
        self.repo_url = repo if repo.startswith("https://") else DEFAULT_REPO
        self.port = _int(env.get("PORT"), DEFAULT_PORT, 1, 65535)
        self.tmp_wanted = str(env.get("BIDEST_TMPDIR") or "").strip()
        self.tmpdir, self.tmp_kind = "", ""

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def _int(v, default, lo, hi):
    try:
        x = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


CFG = Config({})
_SECRET = secrets.token_bytes(32)
#: tempfile.tempdir as it was before public mode pointed it at RAM (only
#: restored when this module changed it)
_SAVED_TEMPDIR: list = []
_lock = threading.Lock()


def enabled() -> bool:
    return CFG.enabled


def configure(env=None) -> Config:
    """(Re)read the settings. Public mode also points the process's temp
    files at a RAM-backed directory when there is one; turning it off puts
    the temp directory back."""
    global CFG, _SECRET, _heavy
    cfg = Config(env)
    with _lock:
        _SECRET = secrets.token_bytes(32)
        _buckets.clear()
        _heavy = threading.BoundedSemaphore(cfg.max_heavy)
        if _SAVED_TEMPDIR:
            tempfile.tempdir = _SAVED_TEMPDIR.pop()
        if cfg.enabled:
            cfg.tmpdir, cfg.tmp_kind = _ram_tmpdir(cfg.tmp_wanted)
            if cfg.tmpdir:
                _SAVED_TEMPDIR.append(tempfile.tempdir)
                tempfile.tempdir = cfg.tmpdir
        CFG = cfg
    return cfg


# ---------------------------------------------------------------------------
# Temporary files: RAM first
# ---------------------------------------------------------------------------

def _mount_fstype(path: str) -> str:
    """The file-system type of the mount holding `path` (Linux), or ""."""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            mounts = [ln.split()[1:3] for ln in fh if len(ln.split()) >= 3]
    except OSError:
        return ""
    real = os.path.realpath(path)
    best, kind = "", ""
    for mp, fs in mounts:
        mp = mp.replace("\\040", " ")
        if (real == mp or real.startswith(mp.rstrip("/") + "/")) \
                and len(mp) >= len(best):
            best, kind = mp, fs
    return kind


def _ram_tmpdir(wanted: str) -> Tuple[str, str]:
    """(directory, "ram" | "disk") for upload copies: $BIDEST_TMPDIR when
    set, else /dev/shm when it is at least 256 MB, else ("", "disk")."""
    cands = [(wanted, True)] if wanted else []
    cands.append(("/dev/shm", False))
    for d, explicit in cands:
        if not (os.path.isdir(d) and os.access(d, os.W_OK)):
            continue
        try:
            st = os.statvfs(d)
        except OSError:
            continue
        if not explicit and st.f_frsize * st.f_blocks < 256 * 1024 * 1024:
            continue
        sub = os.path.join(d, "bidest-uploads")
        try:
            os.makedirs(sub, mode=0o700, exist_ok=True)
        except OSError:
            continue
        fs = _mount_fstype(sub)
        return sub, ("ram" if fs in ("tmpfs", "ramfs") else
                     "disk" if fs else "unknown")
    return "", "disk"


# ---------------------------------------------------------------------------
# Access code
# ---------------------------------------------------------------------------

def _token() -> str:
    return hmac.new(_SECRET, b"bidest-access:" + CFG.access_code.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def has_access(headers) -> bool:
    if not CFG.access_code:
        return True
    raw = headers.get("Cookie") or ""
    try:
        c = SimpleCookie()
        c.load(raw)
    except CookieError:
        return False
    m = c.get(COOKIE)
    return bool(m) and hmac.compare_digest(m.value, _token())


def _secure(headers) -> bool:
    if CFG.trusted_proxies and (headers.get("X-Forwarded-Proto") or "") \
            .split(",")[-1].strip().lower() == "https":
        return True
    return False


def _cookie_header(headers) -> str:
    framed = CFG.frame_ancestors.strip() not in ("'self'", "'none'")
    secure = _secure(headers) or framed
    parts = ["%s=%s" % (COOKIE, _token()), "Path=/", "HttpOnly",
             "SameSite=None" if framed else "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _safe_next(v: str) -> str:
    v = str(v or "/")
    return v if v.startswith("/") and not v.startswith("//") and \
        "\\" not in v and len(v) < 300 else "/"


def access_page(message: str = "", nxt: str = "/") -> bytes:
    msg = ('<p class="err">%s</p>' % html.escape(message)) if message else ""
    return ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Access code</title>
<style>
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 16px;
       background: Canvas; color: CanvasText; }
main { max-width: 26rem; margin: 12vh auto 0; }
input, button { font: inherit; padding: .45rem .6rem; }
input { width: 100%%; box-sizing: border-box; margin: .4rem 0 .8rem; }
.err { font-weight: 600; }
small { opacity: .75; }
</style></head><body><main>
<h1>Texas Public Works OPCC Estimator</h1>
<p>This demo asks for an access code once per browser session.</p>
%s
<form method="post" action="%s">
<input type="hidden" name="next" value="%s">
<label for="code">Access code</label>
<input id="code" name="code" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Continue</button>
</form>
<p><small>%s</small></p>
</main></body></html>
""" % (msg, ACCESS_PATH, html.escape(_safe_next(nxt), quote=True),
       html.escape(BANNER))).encode("utf-8")


# ---------------------------------------------------------------------------
# Client address and rate limits
# ---------------------------------------------------------------------------

def client_ip(peer: str, headers) -> str:
    """The socket peer, or with N trusted proxies the N-th entry from the
    right of X-Forwarded-For (falling back to the peer when it is short or
    not an address)."""
    n = CFG.trusted_proxies
    if n:
        xff = [x.strip() for x in (headers.get("X-Forwarded-For") or "")
               .split(",") if x.strip()]
        if len(xff) >= n:
            cand = xff[-n]
            try:
                return str(ipaddress.ip_address(cand))
            except ValueError:
                pass
    return peer


_buckets: Dict[Tuple[str, str], list] = {}
_heavy = threading.BoundedSemaphore(4)


def rate_class(method: str, path: str) -> str:
    if path == ACCESS_PATH and method == "POST":
        return "login"
    if not path.startswith("/api/"):
        return "static"
    if path in UPLOAD:
        return "upload"
    if path in HEAVY:
        return "heavy"
    return "api"


def take(ip: str, cls: str, now: Optional[float] = None) -> float:
    """Spend one token of (ip, class); 0.0 when allowed, else the seconds to
    wait."""
    per_min, burst = RATES[cls]
    rate = per_min * CFG.rate_scale / 60.0
    cap = max(1.0, burst * CFG.rate_scale)
    now = time.monotonic() if now is None else now
    with _lock:
        if len(_buckets) > 20000:
            for k in [k for k, v in _buckets.items() if now - v[1] > 600]:
                del _buckets[k]
        b = _buckets.get((ip, cls))
        if b is None:
            b = _buckets[(ip, cls)] = [cap, now]
        tokens = min(cap, b[0] + (now - b[1]) * rate)
        b[1] = now
        if tokens >= 1.0:
            b[0] = tokens - 1.0
            return 0.0
        b[0] = tokens
        return (1.0 - tokens) / rate if rate > 0 else 60.0


@contextlib.contextmanager
def heavy_slot(path: str, wait: float = 30.0):
    """A slot among the concurrent heavy requests (yields False when none
    frees up in time)."""
    if path not in HEAVY and path not in UPLOAD:
        yield True
        return
    sem = _heavy
    ok = sem.acquire(timeout=wait)
    try:
        yield ok
    finally:
        if ok:
            sem.release()


# ---------------------------------------------------------------------------
# What a request meets before the API
# ---------------------------------------------------------------------------

def _json(status: int, obj: dict, extra: Optional[dict] = None):
    return (status, "application/json; charset=utf-8",
            json.dumps(obj).encode("utf-8"), dict(extra or {}))


def before(method: str, raw_path: str, headers, peer: str, read_body):
    """Public-mode checks ahead of dispatch(). Returns a reply tuple
    (status, content type, body, headers) to send instead, or None to go on.
    `read_body(limit)` reads the request body (the access form only)."""
    path = urllib.parse.urlsplit(raw_path).path
    ip = client_ip(peer, headers)
    cls = rate_class(method, path)
    wait = take(ip, cls)
    if wait > 0:
        return _json(429, {"detail": "Too many requests from your address; "
                                     "try again in %d s." % max(1, round(wait))},
                     {"Retry-After": str(max(1, int(wait + 0.999)))})
    try:
        n = int(headers.get("Content-Length") or 0)
    except ValueError:
        return _json(400, {"detail": "Bad Content-Length."})
    if n > CFG.max_upload_bytes + 1024 * 1024:
        return _json(413, {"detail": "Request body too large (the limit is "
                                     "%d MB)." % CFG.max_upload_mb})
    if path == ACCESS_PATH:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
        nxt = (query.get("next") or ["/"])[0]
        if not CFG.access_code:
            return (303, "text/plain; charset=utf-8", b"",
                    {"Location": _safe_next(nxt)})
        if method == "POST":
            form = urllib.parse.parse_qs(
                read_body(min(n, 8192)).decode("utf-8", "replace"))
            code = (form.get("code") or [""])[0]
            nxt = (form.get("next") or [nxt])[0]
            if hmac.compare_digest(code.strip().encode("utf-8"),
                                   CFG.access_code.encode("utf-8")):
                return (303, "text/plain; charset=utf-8", b"",
                        {"Location": _safe_next(nxt),
                         "Set-Cookie": _cookie_header(headers),
                         "Cache-Control": "no-store"})
            return (401, "text/html; charset=utf-8",
                    access_page("That code is not right.", nxt),
                    {"Cache-Control": "no-store"})
        return (200, "text/html; charset=utf-8", access_page("", nxt),
                {"Cache-Control": "no-store"})
    if path in OPEN_PATHS or has_access(headers):
        return None
    if path.startswith("/api/"):
        return _json(401, {"detail": "This demo needs its access code.",
                           "login": ACCESS_PATH})
    return (303, "text/plain; charset=utf-8", b"",
            {"Location": ACCESS_PATH + "?next=" +
             urllib.parse.quote(_safe_next(raw_path), safe="/")})


def blocked(method: str, path: str) -> Optional[str]:
    """The feature name when this route is off in public mode."""
    if (method, path) in BLOCKED:
        return BLOCKED[(method, path)]
    if path.startswith("/api/claude/") and path != "/api/claude/status":
        return "Reading a PDF with Claude"
    if path.startswith("/api/bidtab/"):
        return "Adding a bid tabulation"
    return None


# ---------------------------------------------------------------------------
# Headers and the page
# ---------------------------------------------------------------------------

_inline_hashes: Optional[str] = None


def _script_hashes() -> str:
    """CSP hashes of index.html's inline scripts (the pre-paint theme)."""
    global _inline_hashes
    if _inline_hashes is None:
        try:
            with open(os.path.join(STATIC_DIR, "index.html"),
                      encoding="utf-8") as fh:
                page = fh.read()
        except OSError:
            page = ""
        hs = []
        for m in re.finditer(r"<script>(.*?)</script>", page, re.S):
            d = hashlib.sha256(m.group(1).encode("utf-8")).digest()
            hs.append("'sha256-%s'" % base64.b64encode(d).decode("ascii"))
        _inline_hashes = " ".join(hs)
    return _inline_hashes


def security_headers() -> Dict[str, str]:
    csp = ("default-src 'self'; script-src 'self' %s; "
           "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
           "base-uri 'none'; form-action 'self'; frame-ancestors %s"
           % (_script_hashes(), CFG.frame_ancestors))
    return {"X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": csp}


def mark_page(data: bytes) -> bytes:
    """index.html served in public mode: <html data-public="1">, so the page
    hides the local-only features before its first paint."""
    return data.replace(b'<html lang="en">',
                        b'<html lang="en" data-public="1">', 1)


def status() -> dict:
    """The "public" block of /api/status."""
    return {"enabled": CFG.enabled, "banner": BANNER,
            "access_code": bool(CFG.access_code),
            "max_upload_mb": CFG.max_upload_mb,
            "uploads": ("processed in memory; a temporary copy exists only "
                        "while the request runs (%s) and is deleted after it"
                        % ("RAM-backed" if CFG.tmp_kind == "ram"
                           else "temporary directory")),
            "corrections": "kept in your browser (localStorage)",
            "worksheets": "saved in your browser (localStorage) or "
                          "downloaded as a file",
            "repo_url": CFG.repo_url,
            "off": sorted(set(BLOCKED.values()))}


CLAUDE_OFF = {"available": False, "backend": "none", "label": "off",
              "reason": "Reading with Claude is off in the public demo."}
BIDTAB_OFF = {"available": False,
              "reason": "Adding bid tabulations is off in the public demo."}


# ---------------------------------------------------------------------------
# The server: a bounded number of connections
# ---------------------------------------------------------------------------

class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most `max_connections` open connections;
    one more is answered 503 and closed instead of getting a thread."""

    daemon_threads = True
    _BUSY = (b"HTTP/1.1 503 Service Unavailable\r\n"
             b"Content-Type: text/plain\r\nContent-Length: 12\r\n"
             b"Retry-After: 5\r\nConnection: close\r\n\r\nserver busy\n")

    def __init__(self, addr, handler, max_connections: int = 128):
        super().__init__(addr, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(self._BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


configure()
