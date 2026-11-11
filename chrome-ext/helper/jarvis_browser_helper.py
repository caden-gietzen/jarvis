"""Local helper for the Jarvis Chrome extension (stdlib only).

Chrome extensions cannot listen on a port, so this process does it for them:

  Jarvis / CLI  --POST /cmd-->  helper  <--GET /poll (long-poll)--  extension
                                helper  <--POST /result-----------  extension

Every endpoint needs the shared token (X-Jarvis-Token), stored in ~/.jarvis_browser_token.
Binds to 127.0.0.1 only.

Env overrides (used by tests): JARVIS_BROWSER_PORT, JARVIS_BROWSER_TOKEN (token file path),
JARVIS_BROWSER_TIMEOUT (seconds /cmd waits), JARVIS_BROWSER_POLL_WAIT (seconds /poll waits),
JARVIS_BROWSER_EXT_ORIGIN (optional: pin the exact chrome-extension://<id> origin).
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("JARVIS_BROWSER_PORT", "47822"))
TOKEN_PATH = Path(os.environ.get("JARVIS_BROWSER_TOKEN", str(Path.home() / ".jarvis_browser_token")))
CMD_TIMEOUT_S = float(os.environ.get("JARVIS_BROWSER_TIMEOUT", "10"))
POLL_WAIT_S = float(os.environ.get("JARVIS_BROWSER_POLL_WAIT", "20"))
EXT_ORIGIN = os.environ.get("JARVIS_BROWSER_EXT_ORIGIN", "").strip().rstrip("/")

MAX_CMD_BODY = 128 * 1024        # /cmd
MAX_RESULT_BODY = 2 * 1024 * 1024  # /result (page text can be large)
MAX_DRAIN = 8 * 1024 * 1024      # discard up to this much of a rejected body so the client sees our 413

ACTIONS = {"open", "show", "clear", "read", "allow", "list_allowed"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


# ---- token ---------------------------------------------------------------------------

def ensure_token() -> str:
    """Create the token file if missing/invalid (never overwrite a valid one). Returns the token."""
    tok = read_token()
    if tok:
        return tok
    tok = secrets.token_hex(24)  # 48 hex chars
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
        return tok
    except FileExistsError:
        # Either another helper just created it, or it exists but is too short: replace if invalid.
        again = read_token()
        if again:
            return again
        TOKEN_PATH.write_text(tok, encoding="utf-8")
        return tok


def read_token() -> str:
    try:
        tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return tok if len(tok) >= 32 else ""


def token_ok(given) -> bool:
    expected = read_token()
    if not expected or not isinstance(given, str):
        return False
    # Hash both so compare_digest gets equal-length inputs and length is not leaked.
    a = hashlib.sha256(expected.encode("utf-8")).digest()
    b = hashlib.sha256(given.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


# ---- command broker ------------------------------------------------------------------

class Job:
    def __init__(self, command: dict):
        self.id = uuid.uuid4().hex
        self.command = command
        self.event = threading.Event()
        self.result = None


class Broker:
    def __init__(self):
        self.cond = threading.Condition()
        self.queue: list[Job] = []
        self.pending: dict[str, Job] = {}
        self.last_poll = 0.0

    def submit(self, command: dict, timeout: float):
        job = Job(command)
        with self.cond:
            self.queue.append(job)
            self.pending[job.id] = job
            self.cond.notify_all()
        if job.event.wait(timeout):
            return job.result
        with self.cond:  # timed out: make sure the extension never runs it late
            if job in self.queue:
                self.queue.remove(job)
            self.pending.pop(job.id, None)
        if job.event.is_set():  # result raced in at the last moment
            return job.result
        return None

    def next_job(self, wait: float):
        deadline = time.monotonic() + wait
        with self.cond:
            self.last_poll = time.time()
            while not self.queue:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.last_poll = time.time()
                    return None
                self.cond.wait(left)
            self.last_poll = time.time()
            return self.queue.pop(0)

    def requeue(self, job: Job):
        with self.cond:
            if job.id in self.pending:
                self.queue.insert(0, job)
                self.cond.notify_all()

    def deliver(self, job_id: str, result) -> bool:
        with self.cond:
            job = self.pending.pop(job_id, None)
        if not job:
            return False
        job.result = result
        job.event.set()
        return True


BROKER = Broker()


# ---- HTTP ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JarvisBrowserHelper"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("JARVIS_BROWSER_VERBOSE"):
            sys.stderr.write("[helper] " + (fmt % args) + "\n")

    def _send(self, code: int, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self, limit: int):
        """Returns parsed JSON dict, or None after having sent an error response."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.close_connection = True
            self._send(411, {"ok": False, "error": "Content-Length required"})
            return None
        try:
            n = int(raw_len)
            if n < 0:
                raise ValueError
        except ValueError:
            self.close_connection = True
            self._send(400, {"ok": False, "error": "bad Content-Length"})
            return None
        if n > limit:
            self.close_connection = True
            left = min(n, MAX_DRAIN)
            try:
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass
            try:
                self._send(413, {"ok": False, "error": "body too large"})
            except OSError:
                pass
            return None
        data = self.rfile.read(n) if n else b""
        try:
            obj = json.loads(data.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": "invalid JSON"})
            return None
        if not isinstance(obj, dict):
            self._send(400, {"ok": False, "error": "body must be a JSON object"})
            return None
        return obj

    def _origin_ok(self, path: str) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        if path in ("/poll", "/result", "/status"):
            origin = origin.rstrip("/")
            if EXT_ORIGIN:
                return origin == EXT_ORIGIN
            return origin.startswith("chrome-extension://") and len(origin) > len("chrome-extension://")
        return False  # /cmd (and anything else) never accepts browser-originated requests

    def _route(self, method: str):
        path = (self.path or "").split("?")[0]
        if str(self.headers.get("Host", "")).lower() not in HOSTS:
            self.close_connection = True
            return self._send(403, {"ok": False, "error": "bad Host header"})
        if not self._origin_ok(path):
            self.close_connection = True
            return self._send(403, {"ok": False, "error": "origin not allowed"})
        if not token_ok(self.headers.get("X-Jarvis-Token")):
            self.close_connection = True
            return self._send(401, {"ok": False, "error": "missing or invalid X-Jarvis-Token"})

        if method == "POST" and path == "/cmd":
            cmd = self._read_body(MAX_CMD_BODY)
            if cmd is None:
                return
            if cmd.get("action") not in ACTIONS:
                return self._send(400, {"ok": False, "error": "unknown action; expected one of " + ", ".join(sorted(ACTIONS))})
            if cmd.get("action") == "show":
                if "endText" in cmd and not isinstance(cmd["endText"], str):
                    return self._send(400, {"ok": False, "error": "endText must be a string"})
                if cmd.get("expand") not in (None, "", "sentence", "paragraph"):
                    return self._send(400, {"ok": False, "error": 'expand must be "sentence" or "paragraph"'})
                occ = cmd.get("occurrence")
                if occ is not None and occ != "last" and (isinstance(occ, bool) or not isinstance(occ, int) or occ < 1):
                    return self._send(400, {"ok": False, "error": 'occurrence must be a positive integer or "last"'})
                if "debug" in cmd and not isinstance(cmd["debug"], bool):
                    return self._send(400, {"ok": False, "error": "debug must be a boolean"})
            result = BROKER.submit(cmd, CMD_TIMEOUT_S)
            if result is None:
                connected = (time.time() - BROKER.last_poll) < POLL_WAIT_S + 10
                return self._send(504, {
                    "ok": False, "error": "timeout", "extensionConnected": connected,
                    "message": ("The browser extension did not answer in time." if connected else
                                "The browser extension is not connected (is Chrome open, the extension loaded and its token set?)."),
                })
            return self._send(200, result if isinstance(result, dict) else {"ok": True, "result": result})

        if method == "GET" and path == "/poll":
            job = BROKER.next_job(POLL_WAIT_S)
            if job is None:
                return self._send(204)
            try:
                self._send(200, {"id": job.id, "command": job.command})
            except OSError:
                BROKER.requeue(job)
            return

        if method == "POST" and path == "/result":
            body = self._read_body(MAX_RESULT_BODY)
            if body is None:
                return
            job_id = body.get("id")
            if not isinstance(job_id, str) or "result" not in body:
                return self._send(400, {"ok": False, "error": "need id and result"})
            if not BROKER.deliver(job_id, body["result"]):
                return self._send(404, {"ok": False, "error": "unknown or expired id"})
            return self._send(200, {"ok": True})

        if method == "GET" and path == "/status":
            age = time.time() - BROKER.last_poll if BROKER.last_poll else None
            return self._send(200, {"ok": True, "extensionConnected": age is not None and age < POLL_WAIT_S + 10,
                                    "lastPollAgeS": age})

        return self._send(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        self._safe("GET")

    def do_POST(self):
        self._safe("POST")

    def do_PUT(self):
        self._safe("PUT")

    def do_DELETE(self):
        self._safe("DELETE")

    def _safe(self, method):
        try:
            self._route(method)
        except (OSError, ConnectionError):
            self.close_connection = True
        except Exception as e:  # never kill the server thread
            try:
                self._send(500, {"ok": False, "error": str(e)})
            except OSError:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows reuse would allow two helpers to share the port


def main() -> int:
    if "--print-token" in sys.argv:
        print(ensure_token())
        return 0
    ensure_token()
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"Could not listen on 127.0.0.1:{PORT}: {e}", file=sys.stderr)
        return 1
    print(f"Jarvis browser helper listening on 127.0.0.1:{PORT} (token file: {TOKEN_PATH})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
