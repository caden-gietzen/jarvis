r"""Offline tests for the web UI extras: main-agent tool-chip replay, conversation search, usage/voice polish,
the dictation pill/button, and POST /dictation.

Run:
    set JARVIS_NOTIFY_PHONE=0 && .venv\Scripts\python.exe tests\test_webui_extras.py
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"
import http.client
import io
import json
import site
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from jarvis import ui  # noqa: E402
from jarvis import web as web_mod  # noqa: E402
from jarvis.events import RECENT_TOOLS_KEEP, EventBus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-extras-"))
ui._log_path = TMP / "test.log"
ui.console = Console(file=io.StringIO())
TOKEN = "test-token-abc123456789"
HTML = (ROOT / "jarvis" / "webui.html").read_text(encoding="utf-8")
PASSED, FAILED = [], []


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


# ---- (a) events replay -------------------------------------------------------------------------

def test_recent_tools_in_snapshot_with_result():
    b = EventBus()
    b.emit("conversation", role="you", text="do it")
    b.emit("tool_use", name="Read", tool_use_id="t1", description="Read x.py", label="Reading x.py", parent=None)
    b.emit("tool_use", name="Bash", tool_use_id="t2", description="Bash ls", label="Running ls", parent=None)
    b.emit("tool_result", tool_use_id="t1", is_error=False, parent=None)
    b.emit("tool_result", tool_use_id="t2", is_error=True, parent=None)
    b.emit("tool_use", name="Edit", tool_use_id="t3", description="Edit y", label="Editing y", parent=None)
    rt = b.get_state()["recent_tools"]
    check([e["id"] for e in rt] == ["t1", "t2", "t3"], rt)
    check([e["ok"] for e in rt] == [True, False, None], rt)
    check(rt[0]["label"] == "Reading x.py" and rt[0]["seq"] < rt[1]["seq"])
    check(set(rt[0]) == {"seq", "ts", "id", "name", "label", "description", "ok"}, "no raw output fields")


def test_subagent_tools_are_not_main_chips():
    b = EventBus()
    b.emit("tool_use", name="Read", tool_use_id="s1", description="d", label="l", parent="P1")
    b.emit("tool_result", tool_use_id="s1", is_error=False, parent="P1")
    check(b.get_state()["recent_tools"] == [])


def test_recent_tools_bounded_and_snapshot_is_a_copy():
    b = EventBus()
    for i in range(RECENT_TOOLS_KEEP + 25):
        b.emit("tool_use", name="Read", tool_use_id=f"t{i}", description="d", label="l", parent=None)
    rt = b.get_state()["recent_tools"]
    check(len(rt) == RECENT_TOOLS_KEEP and rt[-1]["id"] == f"t{RECENT_TOOLS_KEEP + 24}" and rt[0]["id"] == "t25")
    rt[0]["ok"] = "tampered"
    check(b.get_state()["recent_tools"][0]["ok"] is None)
    b.emit("tool_result", tool_use_id="t0", is_error=False, parent=None)  # evicted id: harmless
    check(len(b.get_state()["recent_tools"]) == RECENT_TOOLS_KEEP)


def test_snapshot_has_dictation_state():
    b = EventBus()
    d = b.get_state()["dictation"]
    check(d["on"] is False and d["phase"] == "idle" and d["since"] is None, d)
    b.emit("dictation", on=True, phase="recording", since=1000.5, max_s=1800)
    d = b.get_state()["dictation"]
    check(d == {"on": True, "phase": "recording", "since": 1000.5, "max_s": 1800}, d)
    b.emit("dictation", on=False, phase="finishing", since=1000.5, max_s=1800)
    d = b.get_state()["dictation"]
    check(d["on"] is False and d["phase"] == "finishing" and d["since"] == 1000.5, d)
    b.emit("dictation", on=False, phase="idle", since=None, max_s=1800)
    d = b.get_state()["dictation"]
    check(d["phase"] == "idle" and d["since"] is None, d)
    json.dumps(b.get_state())


# ---- (b)(c)(d) page markup -----------------------------------------------------------------------

def test_search_markup_and_textcontent_only():
    for needle in ('id="search"', 'type="search"', 'aria-label="Search the conversation"', 'id="searchCount"',
                   'id="searchClear"', 'role="search"', "toLowerCase()", 'e.key === "Escape"', "No matches",
                   "createTextNode", 'el("mark"'):
        check(needle in HTML, f"missing {needle}")
    block = HTML.split("// ---- search:")[1].split("// ---- mode + permission")[0]
    check("innerHTML" not in block and "insertAdjacentHTML" not in block and "outerHTML" not in block,
          "search code never builds HTML from text")
    check("innerHTML" not in HTML and "insertAdjacentHTML" not in HTML, "page never uses innerHTML")


def test_snapshot_renders_recent_tools_merged_by_seq():
    for needle in ("s.recent_tools", "items.sort(function (a, b) { return a.seq - b.seq; })", "markChip(it.r.id"):
        check(needle in HTML, f"missing {needle}")


def test_voice_and_usage_polish_markup():
    for needle in ('id="voicePill"', "Voice muted", "Voice on", '"progressbar"', "aria-valuenow",
                   'role="status"', "(max-width: 520px)", "Usage, current session:", 'aria-label="Conversation"'):
        check(needle in HTML, f"missing {needle}")
    check('id="voicePill" class="pill voice-off hidden"' not in HTML, "voice pill is always visible now")


def test_dictation_markup():
    for needle in ('id="dictToggle"', 'id="dictPill"', "Start dictation", "Stop dictation", '"/dictation"',
                   'case "dictation"', "s.dictation", "Finishing dictation", "dict.phase", 'aria-pressed'):
        check(needle in HTML, f"missing {needle}")
    block = HTML.split("function setDictation")[1].split("function setPerm")[0]
    check("innerHTML" not in block, "setDictation uses textContent only")


def test_mobile_layout_markup():
    check("viewport-fit=cover" in HTML and "100dvh" in HTML and "visualViewport" in HTML, "dynamic viewport + visualViewport")
    check("env(safe-area-inset-bottom)" in HTML, "safe-area insets")
    check("overscroll-behavior: contain" in HTML, "chat scroller contains overscroll")
    form = HTML.split('<form id="form"')[1].split("</form>")[0]
    header = HTML.split("<header>")[1].split("</header>")[0]
    check('id="dictToggle"' in form and 'id="dictToggle"' not in header, "dictation button lives in the input row, not the top bar")
    for bid in ("theme", "voiceToggle", "convToggle", "stopSpeech", "interrupt"):
        check(f'id="{bid}"' in header, f"{bid} stays in the fixed top bar")
    check("min-height: 44px" in HTML, "44px touch targets")


def test_csp_hash_still_matches_page():
    h = web_mod.page_headers(HTML)["Content-Security-Policy"]
    check("sha256-" in h and "unsafe-inline'; " in h and "script-src 'sha256-" in h)
    check(HTML.count("<script>") == 1 and "onclick=" not in HTML, "one inline script, no inline handlers")


# ---- /dictation ---------------------------------------------------------------------------------

class Srv:
    def __init__(self, on_dictation):
        self.bus = EventBus()
        self.calls = []

        def hook(on):
            self.calls.append(on)
            return on_dictation(on)

        self.web = web_mod.WebServer(lambda t: None, lambda t: {"ok": True}, lambda: None, port=0, token=TOKEN,
                                     event_bus=self.bus, url_file=TMP / f"u{id(self)}.txt",
                                     on_dictation=hook if on_dictation else None)
        check(self.web.start(), "server started")
        self.port = self.web.port

    def req(self, body=None, headers=None, host=None, raw=None, path="/dictation", method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}", "X-Jarvis-Token": TOKEN}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                if v is not None:
                    c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()


def test_dictation_route_security_and_validation():
    s = Srv(lambda on: on)
    try:
        st, hd, body = s.req({"on": True})
        check(st == 200 and json.loads(body) == {"ok": True, "on": True}, (st, body))
        check(all(k.lower() in {x.lower() for x in hd} for k in web_mod._SECURITY_HEADERS), "security headers on the answer")
        st, _, body = s.req({"on": False})
        check(st == 200 and json.loads(body)["on"] is False)
        check(s.calls == [True, False])
        n = len(s.calls)
        check(s.req({"on": True}, headers={"X-Jarvis-Token": None})[0] == 401, "no token")
        check(s.req({"on": True}, headers={"X-Jarvis-Token": "wrong"})[0] == 401, "bad token")
        check(s.req({"on": True}, host="evil.example:80")[0] == 403, "bad host")
        check(s.req({"on": True}, headers={"Origin": "http://evil.example"})[0] == 403, "bad origin")
        check(s.req(path="/dictation", method="GET")[0] == 405, "GET refused")
        for bad in ({"on": "true"}, {"on": 1}, {"on": 0}, {"on": None}, {"on": [True]}, {}, {"muted": True}):
            st, _, body = s.req(bad)
            check(st == 400 and json.loads(body)["ok"] is False, (bad, st))
        for raw in (b"not json", b"[true]", b'"on"', b""):
            check(s.req(raw=raw)[0] == 400, raw)
        check(s.req(raw=b"{" + b" " * 20000 + b'"on": true}')[0] == 413, "oversized body")
        check(s.calls == [True, False] and len(s.calls) == n, "no rejected request reached the hook")
    finally:
        s.web.stop()


def test_dictation_route_without_hook_and_hook_failure():
    s = Srv(None)
    try:
        st, _, body = s.req({"on": True})
        check(st == 400 and json.loads(body)["error"] == "dictation is not available in this mode", body)
    finally:
        s.web.stop()

    def boom(on):
        raise RuntimeError("secret detail")

    s = Srv(boom)
    try:
        st, _, body = s.req({"on": True})
        check(st == 500 and "secret detail" not in body.decode() and json.loads(body)["ok"] is False)
    finally:
        s.web.stop()


def test_dictation_state_over_http_snapshot():
    s = Srv(lambda on: on)
    try:
        s.bus.emit("dictation", on=True, phase="recording", since=5.0, max_s=1800)
        s.bus.emit("tool_use", name="Read", tool_use_id="x", description="d", label="Reading", parent=None)
        st, _, body = s.req(path="/state", method="GET")
        data = json.loads(body)
        check(st == 200 and data["dictation"]["phase"] == "recording" and data["recent_tools"][0]["id"] == "x", data)
    finally:
        s.web.stop()


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                PASSED.append(name)
                print(f"PASS {name}")
            except Exception:
                FAILED.append(name)
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
