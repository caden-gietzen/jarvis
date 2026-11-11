r"""Offline tests for the local web page server (jarvis/web.py) and its startup wiring in __main__.

Run (base interpreter):
    .venv\Scripts\python.exe tests\test_web.py

Plain script, no pytest needed. Uses real localhost sockets on ephemeral ports (127.0.0.1 only).
Prints PASS/FAIL per test, exits non-zero on failure.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
import asyncio
import http.client
import io
import json
import os
import site
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from jarvis import ui  # noqa: E402
from jarvis import web as web_mod  # noqa: E402
from jarvis.events import EventBus  # noqa: E402

TMP = Path(tempfile.gettempdir())
ui._log_path = TMP / "jarvis-test-web.log"  # keep the real log clean
ui.console = Console(file=io.StringIO())  # and the terminal quiet

os.environ["JARVIS_WEB_TOKEN_FILE"] = str(TMP / "jarvis-test-web-token.txt")  # never the real token file
TOKEN = "test-token-abc123"
PASSED, FAILED = [], []


class Hooks:
    def __init__(self):
        self.sent, self.cancelled, self.stops = [], [], 0

    def on_send(self, text):
        self.sent.append(text)

    def on_cancel(self, task_id):
        self.cancelled.append(task_id)
        return {"ok": True, "message": f"stopped {task_id}"}

    def on_stop(self):
        self.stops += 1


class Server:
    """A started WebServer on an ephemeral port with its own bus and hooks."""

    def __init__(self, **kw):
        self.bus = EventBus()
        self.hooks = Hooks()
        self.url_file = TMP / f"jarvis-test-web-url-{id(self)}.txt"
        self.web = web_mod.WebServer(self.hooks.on_send, self.hooks.on_cancel, self.hooks.on_stop,
                                     port=0, token=TOKEN, event_bus=self.bus, url_file=self.url_file, **kw)
        self.url = self.web.start()
        assert self.url, "server did not start"
        self.port = self.web.port

    def req(self, method, path, body=None, headers=None, host=None, raw_body=None):
        """(status, headers, body bytes). host overrides the Host header."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def sse(self, path="/events", headers=None):
        """Open an SSE stream; returns (connection, response). Caller closes the connection."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", "Accept": "text/event-stream"}
        h.update(headers or {})
        c.request("GET", path, headers=h)
        return c, c.getresponse()

    def close(self):
        self.web.stop()
        try:
            self.url_file.unlink()
        except OSError:
            pass


def read_events(resp, n, timeout=8.0):
    """Read n SSE events (ignores comments/pings) -> [(event, id, data)]."""
    out, cur = [], {}
    end = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < end:
        line = resp.fp.readline().decode("utf-8").rstrip("\r\n")
        if line == "":
            if cur.get("event"):
                out.append((cur.get("event"), cur.get("id"), cur.get("data")))
            cur = {}
        elif line.startswith(":"):
            continue
        elif ":" in line:
            k, _, v = line.partition(":")
            cur[k] = v.lstrip(" ")
    return out


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


# ---- tests ---------------------------------------------------------------------------

def test_start_url_and_file():
    s = Server()
    try:
        check(s.url == f"http://127.0.0.1:{s.port}/?token={TOKEN}", s.url)
        check(s.url_file.read_text().strip() == s.url, "url file content")
        check(s.web._sock.getsockname()[0] == "127.0.0.1", "must bind loopback only")
    finally:
        s.close()


def test_page_served_with_token():
    s = Server()
    try:
        st, h, body = s.req("GET", f"/?token={TOKEN}")
        check(st == 200, st)
        check(b"<title>Jarvis</title>" in body, "page html")
        check("sha256-" in h["content-security-policy"] and "unsafe-inline'" not in
              h["content-security-policy"].split("style-src")[0], h["content-security-policy"])
        check("frame-ancestors 'none'" in h["content-security-policy"], "csp frame-ancestors")
        check(h["cache-control"] == "no-store" and h["referrer-policy"] == "no-referrer", h)
        # the hash in the CSP really is the hash of the page's script
        import base64
        import hashlib
        import re
        script = re.findall(r"<script>(.*?)</script>", body.decode(), re.S)[0]
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        check(f"'sha256-{digest}'" in h["content-security-policy"], "csp hash matches script")
    finally:
        s.close()


def test_token_required_and_wrong_rejected():
    s = Server()
    try:
        for path in ("/", "/state", "/events"):
            check(s.req("GET", path)[0] == 401, f"{path} without token")
            check(s.req("GET", f"{path}?token=nope")[0] == 401, f"{path} wrong token")
            check(s.req("GET", f"{path}?token=")[0] == 401, f"{path} empty token")
        for path in ("/send", "/cancel", "/stop-speech"):
            check(s.req("POST", path, {"text": "x"})[0] == 401, f"{path} without token")
            check(s.req("POST", path, {"text": "x"}, {"X-Jarvis-Token": "nope"})[0] == 401, f"{path} wrong")
        check(s.req("GET", "/state", headers={"X-Jarvis-Token": TOKEN})[0] == 200, "header token works")
        check(s.req("GET", f"/state?token={TOKEN}")[0] == 200, "query token works")
        check(s.req("GET", "/state?token=%E2%82%AC")[0] == 401, "non-ascii token is rejected, not a crash")
        check(s.hooks.sent == [] and s.hooks.cancelled == [] and s.hooks.stops == 0, "no hook ran")
    finally:
        s.close()


def test_bad_host_and_origin_rejected():
    s = Server()
    try:
        for host in ("evil.example.com", f"evil.example.com:{s.port}", "127.0.0.1", f"127.0.0.1:{s.port + 1}",
                     f"127.0.0.1.evil.com:{s.port}", "[::1]:" + str(s.port)):
            st = s.req("GET", f"/state?token={TOKEN}", host=host)[0]
            check(st == 403, f"Host {host!r} -> {st}")
        check(s.req("GET", f"/state?token={TOKEN}", host=f"localhost:{s.port}")[0] == 200, "localhost host ok")
        for origin in ("http://evil.example.com", "null", f"https://127.0.0.1:{s.port}", f"http://127.0.0.1:{s.port + 1}"):
            st = s.req("POST", "/send", {"text": "hi"}, {"X-Jarvis-Token": TOKEN, "Origin": origin})[0]
            check(st == 403, f"Origin {origin!r} -> {st}")
        check(s.hooks.sent == [], "rejected origin must not reach the hook")
        ok = s.req("POST", "/send", {"text": "hi"}, {"X-Jarvis-Token": TOKEN, "Origin": f"http://127.0.0.1:{s.port}"})[0]
        check(ok == 200, f"own origin ok ({ok})")
    finally:
        s.close()


def test_state_snapshot():
    s = Server()
    try:
        s.bus.emit("conversation", role="you", text="hello")
        s.bus.emit("task_started", task_id="t1", description="build it", tool_use_id=None, task_type="agent")
        s.bus.emit("permission_request", tool="Bash", summary="run ls")
        st, h, body = s.req("GET", f"/state?token={TOKEN}")
        d = json.loads(body)
        check(st == 200 and d["seq"] == 3, d)
        check(d["conversation"][0]["text"] == "hello", d["conversation"])
        check(d["tasks"][0]["task_id"] == "t1", d["tasks"])
        check(d["pending_permission"]["tool"] == "Bash", d["pending_permission"])
    finally:
        s.close()


def _emit_agent_session(bus):
    bus.emit("agent_dispatch", tool_use_id="tuA", subagent_type="scout", description="Look around", parent=None)
    bus.emit("task_started", task_id="tA", description="Look around", tool_use_id="tuA", task_type="agent")
    bus.emit("agent_dispatch", tool_use_id="tuB", subagent_type="implementer", description="Child job", parent="tuA")
    bus.emit("task_started", task_id="tB", description="Child job", tool_use_id="tuB", task_type="agent")
    bus.emit("tool_use", name="Read", tool_use_id="r1", description="reading C:\\dev\\a\\web.py",
             label="Reading web.py", parent="tuA")
    bus.emit("tool_use", name="Bash", tool_use_id="r2", description="running: pytest", label="Running tests", parent="tuB")
    bus.emit("tool_result", tool_use_id="r1", is_error=False, parent="tuA")
    bus.emit("tool_result", tool_use_id="r2", is_error=True, parent="tuB")


def test_late_page_sees_agent_feed_in_state_and_snapshot():
    s = Server()
    c = None
    try:
        _emit_agent_session(s.bus)
        st, _, body = s.req("GET", f"/state?token={TOKEN}")
        tasks = {t["task_id"]: t for t in json.loads(body)["tasks"]}
        check(st == 200 and set(tasks) == {"tA", "tB"}, tasks.keys())
        check([(e["description"], e["ok"]) for e in tasks["tA"]["feed"]] == [("Reading web.py", True)], tasks["tA"]["feed"])
        check([(e["description"], e["ok"]) for e in tasks["tB"]["feed"]] == [("Running tests", False)], "child feed")
        check(tasks["tB"]["parent"] == "tuA", "child is linked to its parent")
        # a client that connects after all of it happened gets the history in its first snapshot
        c, r = s.sse(f"/events?token={TOKEN}")
        (ev, _, data), = read_events(r, 1)
        snap = {t["task_id"]: t for t in json.loads(data)["tasks"]}
        check(ev == "snapshot" and snap["tA"]["feed"][0]["description"] == "Reading web.py", data)
        # and a live feed entry reaches an already-connected page as a bus event
        s.bus.emit("tool_use", name="Grep", tool_use_id="r3", description="searching: x", label="Searching for x", parent="tuA")
        (ev2, _, data2), = read_events(r, 1)
        d2 = json.loads(data2)
        check(ev2 == "bus" and d2["kind"] == "tool_use" and d2["parent"] == "tuA" and d2["label"] == "Searching for x", data2)
    finally:
        if c:
            c.close()
        s.close()


def test_feed_is_bounded_and_never_carries_raw_output_over_http():
    s = Server()
    try:
        s.bus.emit("task_started", task_id="tA", description="busy", tool_use_id="tuA", task_type="agent")
        for i in range(230):
            s.bus.emit("tool_use", name="Read", tool_use_id=f"r{i}", description=f"reading f{i}.py",
                       label=f"Reading f{i}.py", parent="tuA")
        s.bus.emit("tool_result", tool_use_id="r229", is_error=False, parent="tuA", output="TOP-SECRET-OUTPUT")
        st, _, body = s.req("GET", f"/state?token={TOKEN}")
        feed = json.loads(body)["tasks"][0]["feed"]
        check(len(feed) == 200 and feed[-1]["description"] == "Reading f229.py" and feed[-1]["ok"] is True, len(feed))
        check(set(feed[0]) == {"ts", "tool", "description", "id", "ok"}, feed[0].keys())
    finally:
        s.close()


def test_page_has_detail_timeline_and_theme_ui():
    s = Server()
    try:
        st, h, body = s.req("GET", f"/?token={TOKEN}")
        page = body.decode()
        for needle in ('id="detail"', 'id="timeline"', 'id="dFeed"', 'id="dStop"', 'id="theme"', "jarvis-theme",
                       "prefers-color-scheme", 'id="perm"', 'id="permNone"', "/permission", "/cancel", "TASK_FEED_MAX = 200"):
            check(needle in page, f"page lacks {needle}")
        check("http://" not in page and "https://" not in page, "no external resources")
        import re
        # math (2026-11-10): the vendored KaTeX is linked from this same origin only (/vendor/...), nothing else
        check([x for x in re.findall(r'<script src="([^"]*)"', page)]
              == ["/vendor/katex/katex.min.js", "/vendor/jarvis-math.js"], "only the vendored math scripts")
        check(not [x for x in re.findall(r"<link [^>]*>", page)
                   if 'rel="manifest"' not in x and 'rel="apple-touch-icon"' not in x and 'rel="icon"' not in x
                   and 'href="/vendor/katex/katex.min.css"' not in x],
              "no external style (only the same-origin manifest, touch icon and vendored KaTeX links)")
    finally:
        s.close()


def test_sse_snapshot_then_bus_events():
    s = Server()
    c = None
    try:
        s.bus.emit("conversation", role="you", text="before")
        c, r = s.sse(f"/events?token={TOKEN}")
        check(r.status == 200 and "text/event-stream" in r.getheader("content-type"), r.status)
        (ev, eid, data), = read_events(r, 1)
        snap = json.loads(data)
        check(ev == "snapshot" and eid == "1" and snap["conversation"][0]["text"] == "before", (ev, eid, data))
        s.bus.emit("mode", mode="thinking")
        s.bus.emit("conversation", role="jarvis", text="after")
        got = read_events(r, 2)
        check([g[0] for g in got] == ["bus", "bus"], got)
        check([json.loads(g[2])["kind"] for g in got] == ["mode", "conversation"], got)
        check([g[1] for g in got] == ["2", "3"], got)
    finally:
        if c:
            c.close()
        s.close()


def test_sse_reconnect_since_replays():
    s = Server()
    c = c2 = c3 = None
    try:
        for i in range(5):
            s.bus.emit("narration", text=f"n{i}", level="activity")
        c, r = s.sse(f"/events?token={TOKEN}&since=2")
        got = read_events(r, 3)
        check([g[0] for g in got] == ["bus"] * 3, got)
        check([json.loads(g[2])["seq"] for g in got] == [3, 4, 5], got)
        # Last-Event-ID header works the same
        c2, r2 = s.sse(f"/events?token={TOKEN}", {"Last-Event-ID": "4"})
        got2 = read_events(r2, 1)
        check(json.loads(got2[0][2])["seq"] == 5 and got2[0][0] == "bus", got2)
        # since ahead of the server (it restarted): a fresh snapshot, not silence
        c3, r3 = s.sse(f"/events?token={TOKEN}&since=999")
        check(read_events(r3, 1)[0][0] == "snapshot", "snapshot when since is ahead")
    finally:
        for x in (c, c2, c3):
            if x:
                x.close()
        s.close()


def test_sse_gap_falls_back_to_snapshot():
    bus = EventBus(ring_size=3)
    s = Server.__new__(Server)
    s.bus, s.hooks = bus, Hooks()
    s.url_file = TMP / "jarvis-test-web-url-gap.txt"
    s.web = web_mod.WebServer(s.hooks.on_send, s.hooks.on_cancel, s.hooks.on_stop, port=0, token=TOKEN,
                              event_bus=bus, url_file=s.url_file)
    check(s.web.start(), "start")
    s.port = s.web.port
    c = None
    try:
        for i in range(10):
            bus.emit("narration", text=f"n{i}", level="status")
        c, r = s.sse(f"/events?token={TOKEN}&since=2")  # events 3..7 fell out of the ring
        check(read_events(r, 1)[0][0] == "snapshot", "gap -> snapshot")
    finally:
        if c:
            c.close()
        s.close()


def test_sse_cleanup_on_disconnect():
    s = Server()
    try:
        c, r = s.sse(f"/events?token={TOKEN}")
        read_events(r, 1)
        check(wait_for(lambda: len(s.web._subs) == 1 and len(s.bus._subs) == 1), "subscribed")
        c.close()  # browser tab closed
        s.bus.emit("mode", mode="idle")  # give the server something to try to write
        check(wait_for(lambda: len(s.web._subs) == 0 and len(s.bus._subs) == 0, 10),
              f"subscription leaked: web={len(s.web._subs)} bus={len(s.bus._subs)}")
    finally:
        s.close()


def test_sse_client_cap():
    s = Server()
    conns = []
    old = web_mod.MAX_SSE_CLIENTS
    web_mod.MAX_SSE_CLIENTS = 2
    try:
        for _ in range(2):
            c, r = s.sse(f"/events?token={TOKEN}")
            conns.append(c)
            read_events(r, 1)
        c, r = s.sse(f"/events?token={TOKEN}")
        conns.append(c)
        check(r.status == 429, r.status)
    finally:
        web_mod.MAX_SSE_CLIENTS = old
        for c in conns:
            c.close()
        s.close()


def test_hooks_reached():
    s = Server()
    try:
        H = {"X-Jarvis-Token": TOKEN}
        st, _, body = s.req("POST", "/send", {"text": "  open the pod bay doors  "}, H)
        check(st == 200 and json.loads(body)["ok"], (st, body))
        check(s.hooks.sent == ["open the pod bay doors"], s.hooks.sent)
        st, _, body = s.req("POST", "/cancel", {"task_id": "t9"}, H)
        check(st == 200 and json.loads(body) == {"ok": True, "message": "stopped t9"}, (st, body))
        st, _, body = s.req("POST", "/cancel", {}, H)
        check(s.hooks.cancelled == ["t9", None], s.hooks.cancelled)
        st, _, body = s.req("POST", "/stop-speech", None, H, raw_body=b"")
        check(st == 200 and s.hooks.stops == 1, (st, s.hooks.stops))
        # empty / garbage bodies
        check(s.req("POST", "/send", {"text": "   "}, H)[0] == 400, "blank text")
        check(s.req("POST", "/send", None, H, raw_body=b"not json")[0] == 400, "garbage body")
        check(s.req("POST", "/send", ["x"], H)[0] == 400, "non-object body")
        check(s.req("GET", "/send", None, H)[0] == 405, "GET /send not allowed")
        check(len(s.hooks.sent) == 1, "only the one valid send reached the hook")
    finally:
        s.close()


def test_hook_exception_is_500_not_a_crash():
    s = Server()
    try:
        def boom(text):
            raise RuntimeError("nope")
        s.web.on_send = boom
        H = {"X-Jarvis-Token": TOKEN}
        st, _, body = s.req("POST", "/send", {"text": "x"}, H)
        check(st == 500 and "nope" in json.loads(body)["error"], (st, body))
        check(s.req("GET", "/state", None, H)[0] == 200, "server still serving")
    finally:
        s.close()


def test_oversized_body_rejected():
    s = Server()
    try:
        H = {"X-Jarvis-Token": TOKEN}
        big = {"text": "a" * (web_mod.MAX_BODY + 10)}
        check(s.req("POST", "/send", big, H)[0] == 413, "body over MAX_BODY")
        check(s.req("POST", "/cancel", {"task_id": "x" * (web_mod.MAX_BODY + 10)}, H)[0] == 413, "cancel body")
        mid = {"text": "a" * (web_mod.MAX_TEXT + 1)}  # under MAX_BODY but over MAX_TEXT
        check(s.req("POST", "/send", mid, H)[0] == 413, "text over MAX_TEXT")
        # a lying Content-Length (claims small, sends a lot) must still be cut off
        c = socket.create_connection(("127.0.0.1", s.port), timeout=10)
        payload = b'{"text": "' + b"a" * (web_mod.MAX_BODY * 4) + b'"}'
        c.sendall((f"POST /send HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nX-Jarvis-Token: {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n").encode())
        try:
            c.sendall(payload)
        except OSError:
            pass
        first = b""
        try:
            first = c.recv(200)
        except OSError:
            pass
        c.close()
        check(b" 413 " in first or first == b"", first)
        check(s.hooks.sent == [], "oversized body must not reach the hook")
        check(s.req("GET", "/state", None, H)[0] == 200, "server still serving")
    finally:
        s.close()


def test_start_failure_does_not_raise():
    # 1) port busy and not free to share: falls back to another port (and still works)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    hooks = Hooks()
    w = web_mod.WebServer(hooks.on_send, hooks.on_cancel, hooks.on_stop, port=busy, token=TOKEN,
                          event_bus=EventBus(), url_file=TMP / "jarvis-test-web-url-busy.txt")
    try:
        url = w.start()
        check(url and w.port != busy, f"fallback port expected, got {url}")
    finally:
        w.stop()
        blocker.close()
    # 2) impossible bind address: start() returns None and does not raise
    w2 = web_mod.WebServer(hooks.on_send, hooks.on_cancel, hooks.on_stop, host="203.0.113.77", port=0,
                           token=TOKEN, event_bus=EventBus(), url_file=TMP / "jarvis-test-web-url-bad.txt")
    check(w2.start() is None, "start must return None on failure")
    w2.stop()  # and stop after a failed start is harmless
    # 3) unwritable URL file: still serves
    w3 = web_mod.WebServer(hooks.on_send, hooks.on_cancel, hooks.on_stop, port=0, token=TOKEN,
                           event_bus=EventBus(), url_file=TMP / "no-such-dir-xyz" / "u.txt")
    try:
        check(w3.start() is not None, "unwritable url file must not stop the server")
    finally:
        w3.stop()


def test_stop_ends_sse_and_frees_port():
    s = Server()
    c, r = s.sse(f"/events?token={TOKEN}")
    read_events(r, 1)
    t0 = time.monotonic()
    s.web.stop()
    check(time.monotonic() - t0 < 8, "stop() hung with an SSE client attached")
    c.close()
    try:
        socket.create_connection(("127.0.0.1", s.port), timeout=1).close()
        raise AssertionError("port still accepting after stop()")
    except OSError:
        pass
    try:
        s.url_file.unlink()
    except OSError:
        pass


def test_token_not_logged_or_on_bus():
    log = ui._log_path
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    s = Server()
    try:
        s.req("GET", f"/state?token={TOKEN}")
        s.req("GET", "/state?token=wrong", host="evil.com")
        after = log.read_text(encoding="utf-8") if log.exists() else ""
        check(TOKEN not in after[len(before):], "token leaked into the log")
        check(TOKEN not in json.dumps(s.bus.get_state()) and TOKEN not in json.dumps(s.bus.history(0)),
              "token leaked onto the event bus")
    finally:
        s.close()


# ---- startup wiring (__main__.start_web_ui) -------------------------------------------

class FakeBrain:
    model = "fake-model"

    def __init__(self):
        self.asked, self.stops, self.client = [], 0, None
        self.gate = None

    async def ask(self, text):
        self.asked.append(text)
        if self.gate is not None:
            await self.gate.wait()
        return f"echo: {text}"

    def stop_speech(self):
        self.stops += 1

    def _on_interrupt(self):
        pass


class FakeClient:
    def __init__(self):
        self.interrupted = 0
        self.stopped = []

    async def interrupt(self):
        self.interrupted += 1

    async def stop_task(self, tid):
        self.stopped.append(tid)


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()

    def call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)


def wired(cfg=None):
    from jarvis import __main__ as m
    lt = LoopThread()
    brain = FakeBrain()
    brain.client = FakeClient()
    web = lt.call(_make(m, cfg if cfg is not None else {"web_bind": "local"}, lt.loop, brain))
    return m, lt, brain, web


async def _make(m, cfg, loop, brain):
    return m.start_web_ui(cfg, loop=loop, brain=brain, speaker=None)


def test_wiring_config_switch():
    from jarvis import __main__ as m
    lt = LoopThread()
    try:
        check(lt.call(_make(m, {"web_ui": False}, lt.loop, FakeBrain())) is None, "web_ui false -> no server")
        check(lt.call(_make(m, {"web_bind": "local"}, lt.loop, FakeBrain())) is not None, "web_ui defaults to on")
    finally:
        lt.close()


def test_wiring_send_goes_to_brain_ask():
    from jarvis import __main__ as m
    m_, lt, brain, web = wired()
    web.token = TOKEN
    web.url_file = TMP / "jarvis-test-web-url-wired.txt"
    web.port = 0
    seen = []
    from jarvis.events import bus
    sub = bus.subscribe()
    try:
        check(web.start(), "start")
        s = Server.__new__(Server)
        s.port = web.port
        st, _, body = s.req("POST", "/send", {"text": "what time is it"}, {"X-Jarvis-Token": TOKEN})
        check(st == 200, (st, body))
        check(wait_for(lambda: brain.asked == ["what time is it"]), f"brain.ask not called: {brain.asked}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            ev = sub.get(0.2)
            if ev:
                seen.append(ev)
            if any(e["kind"] == "conversation" and e["role"] == "jarvis" for e in seen):
                break
        convo = [(e["role"], e["text"]) for e in seen if e["kind"] == "conversation"]
        check(convo == [("you", "what time is it"), ("jarvis", "echo: what time is it")], convo)
    finally:
        sub.close()
        web.stop()
        lt.close()


def test_wiring_typed_turns_are_capped_and_survive_errors():
    from jarvis import __main__ as m
    m_, lt, brain, web = wired()
    web.token = TOKEN
    web.url_file = TMP / "jarvis-test-web-url-wired2.txt"
    web.port = 0
    try:
        check(web.start(), "start")
        s = Server.__new__(Server)
        s.port = web.port
        H = {"X-Jarvis-Token": TOKEN}
        brain.gate = asyncio.run_coroutine_threadsafe(_event(), lt.loop).result(5)  # block ask() so they pile up
        codes = [s.req("POST", "/send", {"text": f"m{i}"}, H)[0] for i in range(m.MAX_TYPED_TURNS + 1)]
        time.sleep(0.3)
        check(codes[:m.MAX_TYPED_TURNS] == [200] * m.MAX_TYPED_TURNS, codes)
        check(codes[-1] == 500, f"over the cap should be refused: {codes}")
        lt.loop.call_soon_threadsafe(brain.gate.set)
        check(wait_for(lambda: len(brain.asked) == m.MAX_TYPED_TURNS), brain.asked)
        # a brain that raises must not kill anything
        async def bad(text):
            raise RuntimeError("brain down")
        brain.ask = bad
        time.sleep(0.3)
        check(s.req("POST", "/send", {"text": "again"}, H)[0] == 200, "send after errors")
        time.sleep(0.3)
        check(s.req("GET", "/state", None, H)[0] == 200, "still serving")
    finally:
        web.stop()
        lt.close()


async def _event():
    return asyncio.Event()


def test_wiring_cancel_and_stop_speech():
    from jarvis import __main__ as m
    m_, lt, brain, web = wired()
    web.token = TOKEN
    web.url_file = TMP / "jarvis-test-web-url-wired3.txt"
    web.port = 0
    flag = threading.Event()
    m._reply_stops.add(flag)
    try:
        check(web.start(), "start")
        s = Server.__new__(Server)
        s.port = web.port
        H = {"X-Jarvis-Token": TOKEN}
        st, _, body = s.req("POST", "/cancel", {}, H)
        d = json.loads(body)
        check(st == 200 and d["ok"] and brain.client.interrupted == 1, (st, d, brain.client.interrupted))
        st, _, body = s.req("POST", "/cancel", {"task_id": "abc"}, H)
        check(json.loads(body)["ok"] and brain.client.stopped == ["abc"], (body, brain.client.stopped))
        st, _, _ = s.req("POST", "/stop-speech", None, H, raw_body=b"")
        check(st == 200, st)
        check(wait_for(lambda: brain.stops == 1), "brain.stop_speech not called")
        check(flag.is_set(), "reply speech stop flag not set")
    finally:
        m._reply_stops.discard(flag)
        web.stop()
        lt.close()


# ---- /permission endpoint, tailnet binding, token file, helper start ------------------------

def test_permission_endpoint_status_codes():
    calls = []

    def on_perm(pid, decision):
        calls.append((pid, decision))
        return {"good-id-12345": "ok", "used-id-12345": "handled_voice", "old-id-123456": "expired",
                "voice-id-1234": "voice_only"}.get(pid, "unknown")

    s = Server(on_permission=on_perm)
    try:
        H = {"X-Jarvis-Token": TOKEN}
        post = lambda body, h=H, **kw: s.req("POST", "/permission", body, h, **kw)  # noqa: E731
        check(post({"id": "good-id-12345", "decision": "approve"})[0] == 200, "ok")
        st, _, b = post({"id": "used-id-12345", "decision": "deny"})
        check(st == 409 and json.loads(b) == {"ok": False, "error": "handled", "by": "voice"}, (st, b))
        check(post({"id": "old-id-123456", "decision": "deny"})[0] == 410, "expired")
        check(post({"id": "voice-id-1234", "decision": "approve"})[0] == 403, "voice only")
        check(post({"id": "nope-nope-nope", "decision": "approve"})[0] == 404, "unknown")
        n = len(calls)
        # garbage never reaches the hook
        check(post({"id": 5, "decision": "approve"})[0] == 400, "non-string id")
        check(post({"id": "good-id-12345"})[0] == 400, "missing decision")
        check(post({"decision": "approve"})[0] == 400, "missing id")
        check(post(None, raw_body=b"{{nope")[0] == 400, "garbage json")
        check(post(["a"])[0] == 400, "list body")
        check(post({"id": "x" * (web_mod.MAX_BODY + 5), "decision": "approve"})[0] == 413, "oversized")
        check(len(calls) == n, "bad bodies must not reach the hook")
        check(s.req("POST", "/permission", {"id": "good-id-12345", "decision": "approve"})[0] == 401, "no token")
        check(s.req("POST", "/permission", {"id": "good-id-12345", "decision": "approve"}, H, host="evil.com")[0] == 403,
              "bad host")
        check(s.req("POST", "/permission", {"id": "good-id-12345", "decision": "approve"},
                    {**H, "Origin": "http://evil.com"})[0] == 403, "bad origin")
        check(s.req("GET", "/permission", None, H)[0] == 405, "GET not allowed")
        check(len(calls) == n, "rejected requests must not reach the hook")
        # a hook that blows up is a 500, not a crash
        s.web.on_permission = lambda *a: 1 / 0
        check(post({"id": "good-id-12345", "decision": "deny"})[0] == 500, "hook error")
        check(s.req("GET", "/state", None, H)[0] == 200, "still serving")
        s.web.on_permission = None
        check(post({"id": "good-id-12345", "decision": "deny"})[0] == 400, "no hook wired")
    finally:
        s.close()


def _fake_discover(ip="127.0.0.2", names=("myhost", "myhost.tail1234.ts.net")):
    return lambda: (ip, list(names))


def test_tailnet_mode_binds_both_and_extends_allow_list():
    old = web_mod.TAILNET_NET
    web_mod.TAILNET_NET = web_mod.ipaddress.ip_network("127.0.0.0/8")  # stand-in for the tailnet range
    s = Server(bind="tailnet", discover=_fake_discover())
    try:
        check(len(s.web._socks) == 2, "two listening sockets")
        check(sorted(x.getsockname()[0] for x in s.web._socks) == ["127.0.0.1", "127.0.0.2"], "binds")
        check(all(x.getsockname()[0] != "0.0.0.0" for x in s.web._socks), "never a wildcard")
        check(s.web.tailnet_ip == "127.0.0.2", s.web.tailnet_ip)

        def get(connect_ip, host, token=TOKEN, path="/state"):
            c = http.client.HTTPConnection(connect_ip, s.port, timeout=10)
            c.putrequest("GET", f"{path}?token={token}", skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", host)
            c.endheaders()
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        p = s.port
        check(get("127.0.0.2", f"127.0.0.2:{p}") == 200, "tailnet ip host")
        check(get("127.0.0.1", f"myhost:{p}") == 200, "short MagicDNS name")
        check(get("127.0.0.1", f"myhost.tail1234.ts.net:{p}") == 200, "full MagicDNS name")
        check(get("127.0.0.1", f"127.0.0.1:{p}") == 200, "loopback still fine")
        for bad in (f"evil.com:{p}", f"myhost:{p + 1}", "myhost", f"127.0.0.3:{p}", f"myhost.evil.com:{p}",
                    f"127.0.0.2.evil.com:{p}", f"0.0.0.0:{p}"):
            check(get("127.0.0.2", bad) == 403, f"forged Host {bad!r} must be 403")
        check(get("127.0.0.2", f"127.0.0.2:{p}", token="wrong") == 401, "token still required on the tailnet side")
        # the page's own origin on the tailnet name passes; a foreign one does not
        ok = s.req("POST", "/send", {"text": "hi"}, {"X-Jarvis-Token": TOKEN, "Origin": f"http://myhost:{p}"})[0]
        check(ok == 200, ok)
        check(s.req("POST", "/send", {"text": "hi"}, {"X-Jarvis-Token": TOKEN, "Origin": "http://myhost"})[0] == 403,
              "origin without port")
        st, h, _ = s.req("GET", f"/?token={TOKEN}", host=f"myhost:{p}")
        check(st == 200 and "frame-ancestors 'none'" in h["content-security-policy"], "CSP intact on the tailnet host")
    finally:
        web_mod.TAILNET_NET = old
        s.close()


def test_pwa_manifest_and_icons_token_free():
    import struct
    s = Server()
    try:
        st, h, body = s.req("GET", "/manifest.webmanifest")
        check(st == 200, st)
        check(h["content-type"].startswith("application/manifest+json"), h["content-type"])
        m = json.loads(body)
        check(m["name"] == "Jarvis" and m["short_name"] == "Jarvis" and m["display"] == "standalone", m)
        check(m["background_color"] == "#0e1116" and m["theme_color"] == "#0e1116", "colors")
        sizes = {(i["sizes"], i.get("purpose")) for i in m["icons"]}
        check(("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes, sizes)
        check(TOKEN.encode() not in body and b"token" not in body.lower(), "no token in the manifest")
        check(m["start_url"] == "/", m["start_url"])
        for icon in m["icons"]:
            st, h, data = s.req("GET", icon["src"])
            check(st == 200 and h["content-type"] == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n", icon["src"])
        st, h, data = s.req("GET", "/icons/apple-touch-icon.png")
        check(st == 200 and data[:8] == b"\x89PNG\r\n\x1a\n", "apple-touch-icon")
        check(struct.unpack(">II", data[16:24]) == (180, 180), "apple icon is 180x180")
        check(s.req("GET", "/icons/nope.png")[0] == 404, "unknown icon")
        check(s.req("GET", "/icons/..%5Cweb.py")[0] in (404, 403), "no path tricks")
        # token-free does not mean host-free
        check(s.req("GET", "/manifest.webmanifest", host="evil.com")[0] == 403, "manifest bad host")
        check(s.req("GET", "/icons/icon-192.png", host="evil.com")[0] == 403, "icon bad host")
        # everything else still needs the token
        check(s.req("GET", "/state")[0] == 401, "state still 401")
        check(s.req("GET", "/events")[0] == 401, "events still 401")
        check(s.req("GET", "/")[0] == 401, "page still 401 (sign-in page)")
    finally:
        s.close()


def test_page_head_has_pwa_tags_and_csp_allows_them():
    s = Server()
    try:
        st, h, body = s.req("GET", f"/?token={TOKEN}")
        html = body.decode()
        for needle in ('<link rel="manifest" href="/manifest.webmanifest">', '<meta name="theme-color"',
                       '<meta name="apple-mobile-web-app-capable" content="yes">',
                       '<meta name="apple-mobile-web-app-status-bar-style"',
                       '<meta name="apple-mobile-web-app-title" content="Jarvis">',
                       '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">', "viewport-fit=cover"):
            check(needle in html, needle)
        csp = h["content-security-policy"]
        check("manifest-src 'self'" in csp and "img-src 'self'" in csp and "default-src 'none'" in csp, csp)
        check("unsafe-eval" not in csp and "script-src 'sha256-" in csp, csp)
        # the sign-in page carries the same head tags and a CSP hash
        st, h, body = s.req("GET", "/")
        check(st == 401 and b'rel="manifest"' in body and "script-src 'sha256-" in h["content-security-policy"], st)
        check(TOKEN.encode() not in body, "sign-in page has no secrets")
    finally:
        s.close()


def test_session_cookie_flow():
    s = Server()
    try:
        st, h, _ = s.req("GET", f"/?token={TOKEN}")
        sc = h["set-cookie"]
        check(st == 200 and sc.startswith("jarvis_session="), sc)
        low = sc.lower()
        check("httponly" in low and "secure" in low and "samesite=strict" in low and "path=/" in low, sc)
        check(TOKEN not in sc, "cookie must not carry the raw token")
        cookie = sc.split(";")[0]
        # cookie alone (no token in the URL) opens the page and the API, including POSTs and the SSE stream
        check(s.req("GET", "/", headers={"Cookie": cookie})[0] == 200, "page via cookie")
        check(s.req("GET", "/state", headers={"Cookie": cookie})[0] == 200, "state via cookie")
        check(s.req("POST", "/send", {"text": "via cookie"}, {"Cookie": cookie})[0] == 200, "send via cookie")
        check(s.hooks.sent == ["via cookie"], s.hooks.sent)
        c, r = s.sse(headers={"Cookie": cookie})
        try:
            check(r.status == 200 and read_events(r, 1)[0][0] == "snapshot", "sse via cookie")
        finally:
            c.close()
        check("set-cookie" not in s.req("GET", "/", headers={"Cookie": cookie})[1], "no re-set")
        check("set-cookie" not in s.req("GET", "/?token=nope")[1], "no cookie on a wrong token")
        # bad cookies and the raw token as a cookie are rejected; a wrong query token beats a good cookie
        check(s.req("GET", "/state", headers={"Cookie": "jarvis_session=nope"})[0] == 401, "bad cookie")
        check(s.req("GET", "/state", headers={"Cookie": f"jarvis_session={TOKEN}"})[0] == 401, "raw token as cookie")
        check(s.req("GET", "/state?token=nope", headers={"Cookie": cookie})[0] == 401, "wrong token beats cookie")
        # the cookie does not bypass Host / Origin checks
        check(s.req("POST", "/send", {"text": "x"}, {"Cookie": cookie, "Origin": "http://evil.com"})[0] == 403, "origin")
        check(s.req("GET", "/state", headers={"Cookie": cookie}, host="evil.com")[0] == 403, "host")
        # the session value depends on the token (rotating the token logs every device out)
        w2 = web_mod.WebServer(lambda t: None, lambda t: None, lambda: None, port=0, token="another-token-xyz987")
        check(w2._session != s.web._session, "session value depends on the token")
    finally:
        s.close()


def test_no_local_trust_shortcut_behind_a_proxy():
    """Tailscale Serve connects from 127.0.0.1 and adds identity headers: they must grant nothing."""
    s = Server()
    try:
        fwd = {"X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "me@example.com",
               "X-Forwarded-Proto": "https", "X-Real-IP": "127.0.0.1"}
        for path in ("/state", "/events", "/"):
            check(s.req("GET", path, headers=fwd)[0] == 401, f"{path} with identity headers but no token")
        check(s.req("POST", "/send", {"text": "x"}, fwd)[0] == 401, "send")
        check(s.hooks.sent == [], "no hook ran")
    finally:
        s.close()


def test_magicdns_https_host_and_origin():
    old = web_mod.TAILNET_NET
    web_mod.TAILNET_NET = web_mod.ipaddress.ip_network("127.0.0.0/8")  # stand-in for the tailnet range
    s = Server(bind="tailnet", discover=_fake_discover())
    try:
        name = "myhost.tail1234.ts.net"
        p = s.port
        H = {"X-Jarvis-Token": TOKEN}
        # Tailscale Serve: Host without a port, Origin https://<name>
        check(s.req("GET", "/state", headers=H, host=name)[0] == 200, "bare MagicDNS Host")
        check(s.req("POST", "/send", {"text": "hi"}, {**H, "Origin": f"https://{name}"}, host=name)[0] == 200,
              "https origin")
        check(s.req("GET", "/manifest.webmanifest", host=name)[0] == 200, "manifest on the MagicDNS host")
        check(s.req("GET", f"/?token={TOKEN}", host=name)[0] == 200, "page on the MagicDNS host")
        for bad in ("myhost", "evil.com", f"{name}.evil.com", "127.0.0.2"):
            check(s.req("GET", "/state", headers=H, host=bad)[0] == 403, f"Host {bad!r}")
        for origin in (f"http://{name}", "https://evil.com", "https://myhost", f"https://{name}:{p}"):
            check(s.req("POST", "/send", {"text": "x"}, {**H, "Origin": origin}, host=name)[0] == 403, origin)
        check(s.req("GET", "/state", host=name)[0] == 401, "token still required on that host")
        s2 = Server(bind="local")
        try:
            check(s2.req("GET", "/state", headers=H, host=name)[0] == 403, "local mode: bare name refused")
        finally:
            s2.close()
        # explicit extra hosts (ctor / constant / env) work in any mode; invalid names are ignored
        s3 = Server(bind="local", extra_hosts=["jarvis.example.ts.net", "bad host!"])
        try:
            check(s3.req("GET", "/state", headers=H, host="jarvis.example.ts.net")[0] == 200, "extra host")
            check(s3.req("GET", "/state", headers=H, host="bad host!")[0] == 403, "invalid names are ignored")
        finally:
            s3.close()
    finally:
        web_mod.TAILNET_NET = old
        s.close()


def test_page_has_no_absolute_urls_or_ws():
    import re
    html = (ROOT / "jarvis" / "webui.html").read_text(encoding="utf-8")
    check(not re.search(r"""["'`](https?|wss?)://""", html), "page must use relative URLs (mixed content / wss)")
    check("new WebSocket" not in html, "no websocket")
    check('"/events"' in html, "EventSource is relative")


def test_local_mode_ignores_tailnet():
    called = []
    s = Server(bind="local", discover=lambda: called.append(1) or ("127.0.0.2", ["myhost"]))
    try:
        check(not called and len(s.web._socks) == 1 and s.web.tailnet_ip is None, "local mode must not look")
        check(s.req("GET", f"/state?token={TOKEN}", host=f"myhost:{s.port}")[0] == 403, "name not allowed locally")
    finally:
        s.close()


def test_tailnet_address_found_on_retry():
    old = (web_mod.TAILNET_NET, web_mod.TAILNET_RETRY_S, web_mod.TAILNET_RETRY_STEP_S)
    web_mod.TAILNET_NET = web_mod.ipaddress.ip_network("127.0.0.0/8")
    web_mod.TAILNET_RETRY_S, web_mod.TAILNET_RETRY_STEP_S = 5.0, 0.05
    calls = []

    def disc():
        calls.append(1)
        return (None, []) if len(calls) < 3 else ("127.0.0.2", ["myhost"])

    s = Server(bind="tailnet", discover=disc)
    try:
        check(len(calls) == 3, f"retried until Tailscale was ready ({len(calls)} calls)")
        check(s.web.tailnet_ip == "127.0.0.2" and len(s.web._socks) == 2, "tailnet socket added after retry")
    finally:
        web_mod.TAILNET_NET, web_mod.TAILNET_RETRY_S, web_mod.TAILNET_RETRY_STEP_S = old
        s.close()


def test_tailnet_failures_fall_back_to_local():
    old_retry = web_mod.TAILNET_RETRY_S
    web_mod.TAILNET_RETRY_S = 0.0  # no waiting in the failure cases
    try:
        _tailnet_failure_cases()
    finally:
        web_mod.TAILNET_RETRY_S = old_retry


def _tailnet_failure_cases():
    for disc, why in ((lambda: (None, []), "no address"),
                      (lambda: ("0.0.0.0", ["x"]), "wildcard refused"),
                      (lambda: ("192.168.1.5", []), "LAN address refused"),
                      (lambda: ("100.64.0.1", ["x"]), "unbindable address (not ours)")):
        s = Server(bind="tailnet", discover=disc)
        try:
            check(len(s.web._socks) == 1 and s.web.tailnet_ip is None, why)
            check(s.req("GET", f"/state?token={TOKEN}")[0] == 200, why + ": local still serves")
        finally:
            s.close()


def test_discover_tailnet_parsing_and_failures():
    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc

    def runner_ok(cmd, **kw):
        check(kw.get("timeout") == 5.0, "5 s timeout")
        if "ip" in cmd:
            return R("100.64.0.99\n")
        return R(json.dumps({"Self": {"DNSName": "Myhost.tail1234.ts.net."}}))

    ip, names = web_mod.discover_tailnet(runner_ok)
    check(ip == "100.64.0.99" and names == ["myhost.tail1234.ts.net", "myhost"], (ip, names))

    def runner_nostatus(cmd, **kw):
        if "ip" in cmd:
            return R("100.64.0.99\n")
        raise OSError("boom")

    ip, names = web_mod.discover_tailnet(runner_nostatus)
    check(ip == "100.64.0.99" and names, "name falls back to the host name")
    for bad in (lambda c, **k: R("", 1), lambda c, **k: R("8.8.8.8\n"), lambda c, **k: R("garbage"),
                lambda c, **k: R("fd7a:115c::1"), lambda c, **k: R("")):
        check(web_mod.discover_tailnet(bad) == (None, []), "bad output -> local only")

    def timeout(cmd, **kw):
        raise web_mod.subprocess.TimeoutExpired(cmd, 5)

    check(web_mod.discover_tailnet(timeout) == (None, []), "timeout is never fatal")


def test_token_persistence_and_rotate():
    path = TMP / f"jarvis-test-token-{id(object())}.txt"
    path.unlink(missing_ok=True)
    calls = []
    orig = web_mod._restrict_to_user
    web_mod._restrict_to_user = lambda p: calls.append(p)
    try:
        t1 = web_mod.load_token(path)
        check(len(t1) >= 24 and path.read_text().strip() == t1, "created")
        check(web_mod.load_token(path) == t1, "second load returns the same token")
        check(calls, "file is restricted to the user before the secret is written")
        t2 = web_mod.rotate_token(path)
        check(t2 != t1 and path.read_text().strip() == t2 and web_mod.load_token(path) == t2, "rotate")
        path.write_text("short\n")
        t3 = web_mod.load_token(path)
        check(t3 != "short" and len(t3) >= 24, "an unusable token file is replaced")
        os.environ["JARVIS_WEB_TOKEN_FILE"] = str(path)
        check(web_mod.token_file() == path, "env override")
        os.environ["JARVIS_WEB_TOKEN_FILE"] = str(TMP / "jarvis-test-web-token.txt")
        # an unwritable location never raises and still gives a usable token
        bad = TMP / "jarvis-test-notadir.txt"
        bad.write_text("x")
        t4 = web_mod.load_token(bad / "sub" / "token")
        check(len(t4) >= 24, "fallback one-run token")
    finally:
        web_mod._restrict_to_user = orig
        path.unlink(missing_ok=True)


def test_web_token_cli_rotates_and_never_prints_token():
    from jarvis import __main__ as m
    path = TMP / f"jarvis-test-token-cli-{id(object())}.txt"
    path.unlink(missing_ok=True)
    os.environ["JARVIS_WEB_TOKEN_FILE"] = str(path)
    orig = web_mod._restrict_to_user
    web_mod._restrict_to_user = lambda p: None
    buf = io.StringIO()
    old_console = ui.console
    ui.console = Console(file=buf, width=300, highlight=False)
    try:
        t1 = web_mod.load_token()
        check(m.cmd_web_token(False) == 0, "show")
        check(m.cmd_web_token(True) == 0, "rotate")
        t2 = path.read_text().strip()
        check(t2 != t1 and len(t2) >= 24, "token replaced")
        check(t1 not in buf.getvalue() and t2 not in buf.getvalue(), "CLI must not print the token")
        check(t1 not in ui._log_path.read_text(encoding="utf-8") and t2 not in ui._log_path.read_text(encoding="utf-8"),
              "token must not reach the log")
    finally:
        ui.console = old_console
        web_mod._restrict_to_user = orig
        os.environ["JARVIS_WEB_TOKEN_FILE"] = str(TMP / "jarvis-test-web-token.txt")
        path.unlink(missing_ok=True)


def test_wiring_uses_persistent_token_and_tailnet_default():
    from jarvis import __main__ as m
    path = TMP / "jarvis-test-web-token.txt"
    orig = web_mod._restrict_to_user
    web_mod._restrict_to_user = lambda p: None
    lt = LoopThread()
    try:
        w = lt.call(_make(m, {"web_bind": "local"}, lt.loop, FakeBrain()))
        check(w.token == web_mod.load_token(path) and w.bind_mode == "local", "persistent token, local bind")
        w2 = lt.call(_make(m, {}, lt.loop, FakeBrain()))
        check(w2.bind_mode == "tailnet", "default bind is tailnet")
        w3 = lt.call(_make(m, {"web_bind": "garbage"}, lt.loop, FakeBrain()))
        check(w3.bind_mode == "local", "unknown value falls back to local")
    finally:
        web_mod._restrict_to_user = orig
        lt.close()


def test_helper_start_mocked():
    from jarvis import __main__ as m
    script = TMP / "fake_start_helper.ps1"
    script.write_text("x")
    launched = []

    def popen(cmd, **kw):
        launched.append((cmd, kw))

    m._helper_started = False
    check(m.start_browser_helper({"start_browser_helper": False}, port_check=lambda p: False, popen=popen,
                                 script=script) is False and not launched, "switch off")
    check(m.start_browser_helper({}, port_check=lambda p: True, popen=popen, script=script) is False
          and not launched, "already listening -> no launch")
    m._helper_started = False
    check(m.start_browser_helper({}, port_check=lambda p: (p == m.HELPER_PORT and False), popen=popen,
                                 script=script) is True, "launches")
    cmd, kw = launched[0]
    check(cmd[0].lower().endswith("powershell.exe") and "-File" in cmd and cmd[-1] == str(script)
          and "Hidden" in cmd, cmd)
    flags = kw["creationflags"]
    # CREATE_NO_WINDOW and DETACHED_PROCESS together make the Windows launch fail silently: never combine them.
    check(flags & m.subprocess.CREATE_NO_WINDOW and not (flags & m.subprocess.DETACHED_PROCESS), "hidden, not detached")
    check(kw["stdin"] == m.subprocess.DEVNULL and kw["stdout"] == m.subprocess.DEVNULL, "no inherited handles")
    check(m.start_browser_helper({}, port_check=lambda p: False, popen=popen, script=script) is False
          and len(launched) == 1, "only once per process")
    # never fatal: popen raising, script missing
    m._helper_started = False
    def boom(cmd, **kw):
        raise OSError("no")
    check(m.start_browser_helper({}, port_check=lambda p: False, popen=boom, script=script) is False, "popen error")
    m._helper_started = False
    check(m.start_browser_helper({}, port_check=lambda p: False, popen=popen, script=TMP / "nope.ps1") is False
          and len(launched) == 1, "missing script")
    m._helper_started = False
    check(m._port_listening(1) is False, "port 1 is not listening")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        check(m._port_listening(s.getsockname()[1]) is True, "detects a listener")
    finally:
        s.close()
    m._helper_started = True  # leave it set: nothing else in this process may launch a real one


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    order = {fn.__name__: i for i, fn in enumerate(
        sorted(TESTS, key=lambda f: f.__code__.co_firstlineno))}
    for fn in sorted(TESTS, key=lambda f: order[f.__name__]):
        try:
            fn()
            PASSED.append(fn.__name__)
            print(f"PASS {fn.__name__}")
        except Exception:
            FAILED.append(fn.__name__)
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
