"""Local web page served from inside the Jarvis process: conversation stream, live agents panel,
typed input. Server-Sent Events carry the event bus to the page; plain POSTs carry the page's
actions back. Audio stays in Python; the page can Approve/Deny a permission prompt (first answer wins against
voice), except destructive shell commands, which stay voice-only.

Security: bound to 127.0.0.1 (plus the Tailscale IPv4 in tailnet mode, never a wildcard), a token
(persistent, in ~/.jarvis_web_token) on every request (query ?token= or the
X-Jarvis-Token header), and the Host/Origin headers must be this server's own (DNS-rebinding).
The server runs on its own thread with its own event loop; failing to start never raises.

Auth: the token (query ?token= / X-Jarvis-Token) or, after one successful tokened visit to /, an HttpOnly;
Secure;SameSite=Strict session cookie holding an HMAC derived from the token (never the token itself; rotating
the token invalidates it). There is no "local" exemption: requests from 127.0.0.1 (Tailscale Serve) need it too.
GET / without auth is a 401 sign-in page. PWA: /manifest.webmanifest and /icons/<name>.png are token-free (no
secrets in them) but still Host/Origin-checked. Behind an HTTPS proxy the MagicDNS name is accepted as a bare
Host (no port) with Origin https://<name>.

Math: GET /vendor/<file> serves the vendored KaTeX and jarvis-math.js (token-free, Host/Origin-checked, exact
allowlist in mathsupport.vendor_file); only the main page's CSP allows them ('self' for script, style and font).

Endpoints: GET / (page), GET /events (SSE), GET /state, POST /send, POST /cancel, POST /stop-speech,
POST /permission (Approve/Deny with a one-time id; destructive shell commands are voice-only),
POST /voice ({"muted": bool}: Jarvis's voice off/on), POST /dictation ({"on": bool}: start / stop dictation
on the PC microphone; every page learns the state from the "dictation" event), POST /conversation
({"on": bool}: conversation mode, listening for your answer after each spoken reply; every page learns it
from the "conversation_mode" event).
SSE: event "snapshot" (data = bus.get_state()) then event "bus" per bus event, id = seq. A client
reconnecting with ?since=N or Last-Event-ID: N gets the missed events instead of a snapshot
(or a snapshot if they are no longer buffered).
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import mathsupport, ui
from .config import LOG_DIR
from .events import bus

DEFAULT_PORT = 47823  # 47821 = VS Code extension, 47822 = Chrome helper
DEFAULT_TOKEN_FILE = Path.home() / ".jarvis_web_token"
TAILSCALE_EXE = Path(r"C:\Program Files\Tailscale\tailscale.exe")
TAILSCALE_TIMEOUT_S = 5.0
TAILNET_RETRY_S = 20.0  # at startup, keep looking this long for the Tailscale address before going local-only
TAILNET_RETRY_STEP_S = 2.0
TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")  # the only non-loopback range we will ever bind
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MAX_TEXT = 4000
MAX_BODY = 16 * 1024  # POST bodies are tiny JSON; anything bigger is refused before it is parsed
MAX_SSE_CLIENTS = 16
PAGE_PATH = Path(__file__).with_name("webui.html")
URL_FILE = LOG_DIR / "web_url.txt"
START_TIMEOUT_S = 10.0
SSE_WAIT_S = 15.0
LOGIN_PATH = Path(__file__).with_name("weblogin.html")
STATIC_DIR = Path(__file__).with_name("static")
ICON_FILES = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")  # served token-free
COOKIE_NAME = "jarvis_session"
COOKIE_MAX_AGE_S = 365 * 24 * 3600
# Extra DNS names the page may be reached by over HTTPS through a reverse proxy (Tailscale Serve) on the
# default port, i.e. Host: name and Origin: https://name. The MagicDNS name is added automatically in
# tailnet mode; this is for anything else. Also read from the JARVIS_WEB_EXTRA_HOSTS env var (comma list).
EXTRA_ALLOWED_HOSTS: tuple[str, ...] = ()
MANIFEST = {
    "id": "/",
    "name": "Jarvis",
    "short_name": "Jarvis",
    "description": "Jarvis voice assistant: conversation, agents and controls.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0e1116",
    "theme_color": "#0e1116",
    "icons": [
        {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

def _csp(script_src: str, math: bool = False) -> str:
    # math=True (only for the page that loads the vendored KaTeX): script-src also 'self' (served from the exact
    # allowlist /vendor/..., never user content), style-src 'self' for katex.min.css, font-src 'self' for its fonts.
    # Inline scripts stay hash-only; there is no 'unsafe-inline' for scripts and no 'unsafe-eval'.
    style_src, font_src = "'unsafe-inline'", ""
    if math:
        script_src += " 'self'"
        style_src = "'self' 'unsafe-inline'"
        font_src = "font-src 'self'; "
    return (f"default-src 'none'; script-src {script_src}; style-src {style_src}; {font_src}"
            "connect-src 'self'; manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # the token is in the URL
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": _csp("'none'"),
}


def page_headers(html: str) -> dict:
    """Security headers for the page: the CSP only allows the page's own inline script (by hash)."""
    headers = dict(_SECURITY_HEADERS)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    math = "/vendor/katex/katex.min.js" in html  # only the page that links the vendored KaTeX gets 'self' for it
    if scripts:
        hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(x.encode("utf-8")).digest()).decode() + "'"
                          for x in scripts)
        headers["Content-Security-Policy"] = _csp(hashes, math)
    elif math:
        headers["Content-Security-Policy"] = _csp("'none'", math)
    return headers


def _text_of(result: dict) -> str:
    try:
        return result["content"][0]["text"]
    except Exception:
        return ""


def token_file() -> Path:
    return Path(os.environ.get("JARVIS_WEB_TOKEN_FILE") or DEFAULT_TOKEN_FILE)


def _restrict_to_user(path: Path) -> None:
    """Best effort: only the current user may read the token file. Never raises."""
    try:
        if os.name == "nt":
            import getpass
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(R,W)"],
                           capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.chmod(path, 0o600)
    except Exception:
        ui.error("Couldn't restrict the web token file to your user account.")


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    _restrict_to_user(path)  # before the secret goes in
    path.write_text(token + "\n", encoding="utf-8")


def load_token(path: Path | None = None) -> str:
    """The persistent web token (created on first use). Never logged. Falls back to a random
    per-run token if the file can't be used."""
    path = path or token_file()
    try:
        try:
            tok = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            tok = ""
        if not _TOKEN_RE.match(tok):
            tok = secrets.token_urlsafe(24)
            _write_token(path, tok)
        return tok
    except Exception as e:
        ui.error(f"Couldn't use the web token file ({type(e).__name__}); using a one-run token.")
        return secrets.token_urlsafe(24)


def rotate_token(path: Path | None = None) -> str:
    """Write a brand-new token (raises OSError if it can't). Returns it."""
    path = path or token_file()
    tok = secrets.token_urlsafe(24)
    _write_token(path, tok)
    return tok


def _tailscale_cmd(args, runner=subprocess.run) -> str | None:
    exe = str(TAILSCALE_EXE) if TAILSCALE_EXE.exists() else shutil.which("tailscale")
    if not exe:
        return None
    r = runner([exe, *args], capture_output=True, text=True, timeout=TAILSCALE_TIMEOUT_S,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout if r.returncode == 0 else None


def discover_tailnet(runner=subprocess.run) -> tuple[str | None, list[str]]:
    """(tailnet IPv4 or None, host names it answers to). Read-only `tailscale ip -4` / `status --json`;
    never raises, logs one line on failure. Only an address inside 100.64.0.0/10 is ever returned."""
    try:
        out = _tailscale_cmd(["ip", "-4"], runner)
        ip = ipaddress.ip_address((out or "").split()[0]) if out and out.split() else None
        if ip is None or ip.version != 4 or ip not in TAILNET_NET:
            ui.activity("Tailscale address not found; web page stays local-only.")
            return None, []
        names: list[str] = []
        try:
            import json as _json
            st = _json.loads(_tailscale_cmd(["status", "--json"], runner) or "{}")
            dns = str((st.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
            if dns:
                names += [dns, dns.split(".")[0]]
        except Exception:
            pass
        if not names:
            names = [socket.gethostname().lower()]
        return str(ip), [n for n in dict.fromkeys(names) if _NAME_RE.match(n)]
    except Exception as e:
        ui.activity(f"Tailscale lookup failed ({type(e).__name__}); web page stays local-only.")
        return None, []


class WebServer:
    """on_send(text) -> None, on_cancel(task_id|None) -> {"ok", "message"} (may block), on_stop_speech() -> None,
    on_permission(id, decision) -> status string from Brain.resolve_permission.
    All are called from server threads; they must be thread-safe.

    bind="local": 127.0.0.1 only. bind="tailnet": also the Tailscale IPv4 (never 0.0.0.0), and the
    Host/Origin allow-list gains that address and the MagicDNS names."""

    def __init__(self, on_send, on_cancel, on_stop_speech, *, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, token: str | None = None, event_bus=bus, url_file=URL_FILE,
                 on_permission=None, bind: str = "local", discover=None, on_voice=None, on_dictation=None,
                 on_conversation=None, extra_hosts=None):
        self.on_send, self.on_cancel, self.on_stop_speech = on_send, on_cancel, on_stop_speech
        self.on_permission = on_permission
        self.on_voice = on_voice  # on_voice(muted: bool) -> the new muted state; sets it and emits the event
        self.on_dictation = on_dictation  # on_dictation(on: bool) -> whether a dictation is now active
        self.on_conversation = on_conversation  # on_conversation(on: bool) -> the new state; persists and emits
        self.bind_mode = bind
        self._discover = discover or discover_tailnet
        self.tailnet_ip: str | None = None
        self.host, self.port = host, port
        self.token = token or secrets.token_urlsafe(24)
        self.bus = event_bus
        self.url_file = url_file
        self.url: str | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._socks: list = []
        self._subs: set = set()
        self._subs_lock = threading.Lock()
        self._stopping = False
        self._allowed_hosts: set[str] = set()  # "host:port" values accepted over plain http
        self._https_hosts: set[str] = set()  # bare DNS names accepted behind an HTTPS proxy (Host: name, Origin: https://name)
        env_hosts = [h.strip().lower() for h in os.environ.get("JARVIS_WEB_EXTRA_HOSTS", "").split(",")]
        self._extra_hosts = [h.lower() for h in (*EXTRA_ALLOWED_HOSTS, *(extra_hosts or ()), *env_hosts)
                             if h and _NAME_RE.match(h.lower())]
        self._session = hmac.new(self.token.encode("utf-8"), b"jarvis-web-session-v1", hashlib.sha256).hexdigest()

    # ---- lifecycle -----------------------------------------------------------------
    def start(self) -> str | None:
        """Start serving. Returns the URL with token, or None (logged) if it could not start."""
        try:
            import uvicorn

            sock = self._bind()
            self._sock = sock
            self._socks = [sock]
            self.port = sock.getsockname()[1]
            self._allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
            if self.bind_mode == "tailnet":
                self._add_tailnet()
            self._https_hosts |= set(self._extra_hosts)
            config = uvicorn.Config(self._build_app(), log_config=None, access_log=False, lifespan="off",
                                    timeout_graceful_shutdown=2)
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(target=self._run, args=(self._socks,), name="jarvis-web", daemon=True)
            self._thread.start()
            end = time.monotonic() + START_TIMEOUT_S
            while not self._server.started and self._thread.is_alive() and time.monotonic() < end:
                time.sleep(0.02)
            if not self._server.started:
                raise RuntimeError("server did not come up")
            self.url = f"http://127.0.0.1:{self.port}/?token={self.token}"
            try:
                self.url_file.write_text(self.url + "\n", encoding="utf-8")
            except OSError as e:
                ui.error(f"Couldn't write the web UI URL file ({e}).")
            return self.url
        except Exception as e:  # never crash Jarvis because the page could not start
            ui.error(f"Web UI not started ({type(e).__name__}: {e}).")
            self.stop()
            return None

    def _add_tailnet(self) -> None:
        """Also listen on the Tailscale IPv4 (same port) and accept its Host names. Never fatal."""
        ip, names = self._discover()
        # Tailscale may still be starting (e.g. right after login): retry a few seconds before giving up.
        deadline = time.monotonic() + TAILNET_RETRY_S
        while not ip and time.monotonic() < deadline:
            time.sleep(min(TAILNET_RETRY_STEP_S, max(0.0, deadline - time.monotonic())))
            ip, names = self._discover()
        if not ip:
            return
        try:
            if ipaddress.ip_address(ip) not in TAILNET_NET:  # belt and braces: never a wildcard or LAN address
                raise ValueError("not a tailnet address")
            sock = self._bind_one(ip, self.port)
        except Exception as e:
            ui.activity(f"Web page not served on the tailnet ({type(e).__name__}); local-only.")
            return
        self._socks.append(sock)
        self.tailnet_ip = ip
        for h in [ip, *names]:
            self._allowed_hosts.add(f"{h}:{self.port}")
        # Tailscale Serve answers on 443 with Host: <full MagicDNS name> (no port) and Origin: https://<name>.
        self._https_hosts |= {n for n in names if "." in n}

    @staticmethod
    def _bind_one(host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: nobody else may share or steal the port
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, port))
            sock.listen(64)
            return sock
        except OSError:
            sock.close()
            raise

    def _bind(self) -> socket.socket:
        """Listening socket on host:port; if that port is taken (another Jarvis, an old one still
        closing) fall back to any free port. The URL is what matters, and it is printed."""
        last: OSError | None = None
        for port in ([self.port, 0] if self.port else [0]):
            try:
                return self._bind_one(self.host, port)
            except OSError as e:
                last = e
        raise last  # type: ignore[misc]

    def _run(self, socks) -> None:
        try:
            self._server.run(sockets=list(socks))
        except BaseException as e:  # uvicorn exits with SystemExit when startup fails
            if not self._stopping:
                ui.error(f"Web UI server stopped ({type(e).__name__}: {e}).")

    def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.should_exit = True
        with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:  # wake the SSE generators so they end
            sub.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(5)
        for sk in self._socks or ([self._sock] if self._sock is not None else []):
            try:
                sk.close()
            except OSError:
                pass

    # ---- app -----------------------------------------------------------------------
    def _build_app(self):
        from starlette.applications import Starlette
        from starlette.routing import Route

        return Starlette(routes=[
            Route("/", self._page),
            Route("/manifest.webmanifest", self._manifest),
            Route("/icons/{name}", self._icon),
            Route("/vendor/{rel:path}", self._vendor),
            Route("/events", self._events),
            Route("/state", self._state),
            Route("/send", self._send, methods=["POST"]),
            Route("/cancel", self._cancel, methods=["POST"]),
            Route("/stop-speech", self._stop_speech, methods=["POST"]),
            Route("/permission", self._permission, methods=["POST"]),
            Route("/voice", self._voice, methods=["POST"]),
            Route("/dictation", self._dictation, methods=["POST"]),
            Route("/conversation", self._conversation, methods=["POST"]),
        ])

    def _guard(self, request, *, auth: bool = True):
        """None if the request may proceed, else the rejecting Response. Host and Origin are always
        checked; auth=False skips only the token (manifest and icons carry no secrets). There is no
        "local" exemption: a request from 127.0.0.1 (e.g. via Tailscale Serve) needs the token too."""
        from starlette.responses import PlainTextResponse

        host = (request.headers.get("host") or "").lower()
        if host not in self._allowed_hosts and host not in self._https_hosts:
            return PlainTextResponse("Bad Host", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None:
            ok = {f"http://{h}" for h in self._allowed_hosts} | {f"https://{h}" for h in self._https_hosts}
            if origin.lower() not in ok:
                return PlainTextResponse("Bad Origin", status_code=403)
        if auth and self._auth_via(request) is None:
            return PlainTextResponse("Missing or invalid token", status_code=401)
        return None

    def _auth_via(self, request) -> str | None:
        """"token" (query or X-Jarvis-Token header), "cookie" (the HttpOnly session cookie), or None.
        A token that is supplied but wrong fails outright (the cookie is not tried). Constant-time."""
        given = request.query_params.get("token") or request.headers.get("x-jarvis-token") or ""
        if given:
            ok = secrets.compare_digest(given.encode("utf-8"), self.token.encode("utf-8"))
            return "token" if ok else None
        cookie = request.cookies.get(COOKIE_NAME) or ""
        if cookie and secrets.compare_digest(cookie.encode("utf-8"), self._session.encode("utf-8")):
            return "cookie"
        return None

    async def _page(self, request):
        from starlette.responses import HTMLResponse

        if (bad := self._guard(request, auth=False)) is not None:
            return bad
        via = self._auth_via(request)
        if via is None:  # 401 plus a small page asking for the token (an installed app with no session lands here)
            html = LOGIN_PATH.read_text(encoding="utf-8")
            return HTMLResponse(html, status_code=401, headers=page_headers(html))
        html = PAGE_PATH.read_text(encoding="utf-8")
        resp = HTMLResponse(html, headers=page_headers(html))
        if via == "token":  # remember this browser/app: later visits need no token in the URL
            resp.set_cookie(COOKIE_NAME, self._session, max_age=COOKIE_MAX_AGE_S, path="/", secure=True,
                            httponly=True, samesite="strict")
        return resp

    async def _manifest(self, request):
        from starlette.responses import JSONResponse

        if (bad := self._guard(request, auth=False)) is not None:
            return bad
        headers = dict(_SECURITY_HEADERS)
        headers["Cache-Control"] = "no-cache"
        return JSONResponse(MANIFEST, media_type="application/manifest+json", headers=headers)

    async def _icon(self, request):
        from starlette.responses import PlainTextResponse, Response

        if (bad := self._guard(request, auth=False)) is not None:
            return bad
        name = request.path_params.get("name", "")
        if name not in ICON_FILES:  # whitelist: no path tricks
            return PlainTextResponse("Not found", status_code=404)
        try:
            data = (STATIC_DIR / name).read_bytes()
        except OSError:
            return PlainTextResponse("Not found", status_code=404)
        headers = dict(_SECURITY_HEADERS)
        headers["Cache-Control"] = "public, max-age=86400"
        return Response(data, media_type="image/png", headers=headers)

    async def _vendor(self, request):
        """Vendored KaTeX and jarvis-math.js (public library files, no secrets: token-free like the icons, but
        Host/Origin-checked). Exact allowlist in mathsupport.vendor_file; nothing else is reachable."""
        from starlette.responses import PlainTextResponse, Response

        if (bad := self._guard(request, auth=False)) is not None:
            return bad
        hit = mathsupport.vendor_file(request.path_params.get("rel", ""))
        if hit is None:
            return PlainTextResponse("Not found", status_code=404)
        path, ctype = hit
        try:
            data = path.read_bytes()
        except OSError:
            return PlainTextResponse("Not found", status_code=404)
        headers = dict(_SECURITY_HEADERS)
        headers["Cache-Control"] = "no-cache" if path.name == "jarvis-math.js" else "public, max-age=86400"
        return Response(data, media_type=ctype, headers=headers)

    async def _state(self, request):
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        return JSONResponse(self.bus.get_state(), headers=_SECURITY_HEADERS)

    async def _events(self, request):
        from sse_starlette.sse import EventSourceResponse
        from starlette.responses import PlainTextResponse

        if (bad := self._guard(request)) is not None:
            return bad
        with self._subs_lock:
            if len(self._subs) >= MAX_SSE_CLIENTS:
                return PlainTextResponse("Too many open pages", status_code=429)
        raw = request.query_params.get("since") or request.headers.get("last-event-id")
        try:
            since = int(raw) if raw not in (None, "") else None
        except ValueError:
            since = None
        state = self.bus.get_state()
        send_snapshot = since is None or since > state["seq"]
        if not send_snapshot and since < state["seq"]:
            hist = self.bus.history(since)
            send_snapshot = not hist or hist[0]["seq"] != since + 1  # gap: no longer buffered
        sub = self.bus.subscribe(since=state["seq"] if send_snapshot else since)
        with self._subs_lock:
            self._subs.add(sub)

        async def gen():
            dropped = 0
            try:
                if send_snapshot:
                    yield {"event": "snapshot", "id": str(state["seq"]), "data": json.dumps(state, default=str)}
                while not self._stopping:
                    ev = await sub.aget(SSE_WAIT_S)
                    if ev is None:
                        if sub.closed:
                            break
                        continue
                    if sub.dropped != dropped:  # we fell behind and lost events: resync
                        dropped = sub.dropped
                        snap = self.bus.get_state()
                        yield {"event": "snapshot", "id": str(snap["seq"]), "data": json.dumps(snap, default=str)}
                        if ev["seq"] <= snap["seq"]:
                            continue
                    yield {"event": "bus", "id": str(ev["seq"]), "data": json.dumps(ev, default=str)}
            finally:
                sub.close()
                with self._subs_lock:
                    self._subs.discard(sub)

        return EventSourceResponse(gen(), ping=15, headers=_SECURITY_HEADERS)

    async def _json_body(self, request) -> dict | None:
        """The parsed JSON object ({} if malformed), or None if the body is over MAX_BODY (enforced
        while reading, so a lying or missing Content-Length can't get past it)."""
        try:
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return None
        except ValueError:
            return None
        data = bytearray()
        try:
            async for chunk in request.stream():
                data += chunk
                if len(data) > MAX_BODY:
                    return None
            body = json.loads(bytes(data).decode("utf-8")) if data else {}
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def _send(self, request):
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
        if len(text) > MAX_TEXT:
            return JSONResponse({"ok": False, "error": "text too long"}, status_code=413)
        try:
            await asyncio.to_thread(self.on_send, text)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
        return JSONResponse({"ok": True})

    async def _cancel(self, request):
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "message": "body too large"}, status_code=413)
        task_id = str(body.get("task_id") or "").strip() or None
        try:
            res = await asyncio.to_thread(self.on_cancel, task_id)
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"{type(e).__name__}: {e}"}, status_code=500)
        return JSONResponse(res if isinstance(res, dict) else {"ok": True, "message": str(res or "")})

    async def _permission(self, request):
        """POST {id, decision: "approve"|"deny"}: answer the pending prompt that issued `id`."""
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        pid, decision = body.get("id"), body.get("decision")
        if not isinstance(pid, str) or not isinstance(decision, str) or self.on_permission is None:
            return JSONResponse({"ok": False, "error": "bad"}, status_code=400)
        try:
            code = await asyncio.to_thread(self.on_permission, pid, decision)
        except Exception as e:
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500)
        status, payload = _perm_response(str(code))
        return JSONResponse(payload, status_code=status, headers=_SECURITY_HEADERS)

    async def _voice(self, request):
        """POST {"muted": true|false}: Jarvis's voice off / on. Returns {"ok", "muted"}; every page
        learns the new state from the "voice" event."""
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        muted = body.get("muted")
        if not isinstance(muted, bool) or self.on_voice is None:
            return JSONResponse({"ok": False, "error": "bad"}, status_code=400, headers=_SECURITY_HEADERS)
        try:
            state = await asyncio.to_thread(self.on_voice, muted)
        except Exception as e:
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500,
                                headers=_SECURITY_HEADERS)
        return JSONResponse({"ok": True, "muted": bool(muted if state is None else state)},
                            headers=_SECURITY_HEADERS)

    async def _dictation(self, request):
        """POST {"on": true|false}: start / stop dictation. Strict: "on" must be a real boolean."""
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        on = body.get("on")
        if self.on_dictation is None:
            return JSONResponse({"ok": False, "error": "dictation is not available in this mode"},
                                status_code=400, headers=_SECURITY_HEADERS)
        if not isinstance(on, bool):
            return JSONResponse({"ok": False, "error": "bad"}, status_code=400, headers=_SECURITY_HEADERS)
        try:
            active = await asyncio.to_thread(self.on_dictation, on)
        except Exception as e:
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500,
                                headers=_SECURITY_HEADERS)
        return JSONResponse({"ok": True, "on": bool(active)}, headers=_SECURITY_HEADERS)

    async def _conversation(self, request):
        """POST {"on": true|false}: conversation mode on / off. Strict: "on" must be a real boolean."""
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        body = await self._json_body(request)
        if body is None:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        on = body.get("on")
        if not isinstance(on, bool) or self.on_conversation is None:
            return JSONResponse({"ok": False, "error": "bad"}, status_code=400, headers=_SECURITY_HEADERS)
        try:
            state = await asyncio.to_thread(self.on_conversation, on)
        except Exception as e:
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=500,
                                headers=_SECURITY_HEADERS)
        return JSONResponse({"ok": True, "on": bool(on if state is None else state)}, headers=_SECURITY_HEADERS)

    async def _stop_speech(self, request):
        from starlette.responses import JSONResponse

        if (bad := self._guard(request)) is not None:
            return bad
        try:
            await asyncio.to_thread(self.on_stop_speech)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
        return JSONResponse({"ok": True})


_PERM_STATUS = {"bad": 400, "unknown": 404, "expired": 410, "voice_only": 403}


def _perm_response(code: str):
    """(http status, json body) for a Brain.resolve_permission result."""
    if code == "ok":
        return 200, {"ok": True}
    if code.startswith("handled_"):
        by = code.split("_", 1)[1]
        return 409, {"ok": False, "error": "handled", "by": by}
    return _PERM_STATUS.get(code, 400), {"ok": False, "error": code}


def make_cancel_hook(loop, brain, cancel_mod):
    """on_cancel for WebServer: runs the cancel tool's code on the main loop, same as the model's tool."""
    def on_cancel(task_id):
        coro = cancel_mod.cancel_task_impl({"task_id": task_id or ""}, lambda: brain.client, brain._on_interrupt)
        res = asyncio.run_coroutine_threadsafe(coro, loop).result(cancel_mod.CANCEL_TIMEOUT_S + 5)
        return {"ok": not res.get("is_error"), "message": _text_of(res)}

    return on_cancel
