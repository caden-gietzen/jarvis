r"""Offline tests for conversation mode (follow-up listening after a reply on/off).

Run:
    set JARVIS_NOTIFY_PHONE=0 && set JARVIS_TOKEN_LEDGER=0 && .venv\Scripts\python.exe tests\test_conversation.py

Plain script. No audio, no network beyond a loopback test server. Every state file is a temp path.
Covers: phrase matching (positives and negatives, no collision with dictation / mute), default ON with no
saved value, persistence round trip, ON -> "followup" and OFF -> "wake" after a reply (also muted and
dictation), flush_mode, the voice commands through local_command, POST /conversation (token, Host/Origin,
strict JSON), event + snapshot, page markup (textContent only, no CSP change).
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')
import asyncio
import http.client
import io
import json
import os
import re
import site
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))
os.environ["JARVIS_NOTIFY_PHONE"] = "0"

from rich.console import Console  # noqa: E402

from jarvis import __main__ as main_mod  # noqa: E402
from jarvis import events, speaker as speaker_mod, ui, web as web_mod  # noqa: E402
from jarvis.dictation import dictation_command  # noqa: E402
from jarvis.events import bus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-conv-"))
STATE = TMP / "state.json"
speaker_mod.STATE_PATH = STATE
ui._log_path = TMP / "test.log"
ui.console = Console(file=io.StringIO())
TOKEN = "test-token-conv-123456"
HTML = (ROOT / "jarvis" / "webui.html").read_text(encoding="utf-8")
PASSED, FAILED = [], []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def reset_state(**kw):
    STATE.write_text(json.dumps(kw))
    main_mod._conversation_on = True


# ---------------------------------------------------------------- phrases
def test_phrases():
    for t in ["conversation mode on", "Conversation mode on.", "CONVERSATION MODE ON!", "start conversation mode",
              "Start conversation mode.", "enter conversation mode", "Enter conversation mode!",
              "  start   conversation mode  ", "Jarvis, start conversation mode", "please enter conversation mode",
              "conversation mode on please"]:
        check(speaker_mod.conversation_command(t) == "on", f"{t!r} should turn on")
    for t in ["conversation mode off", "Conversation mode off.", "stop conversation mode", "Stop conversation mode.",
              "exit conversation mode", "Exit conversation mode!", "Jarvis, exit conversation mode",
              "stop conversation mode please"]:
        check(speaker_mod.conversation_command(t) == "off", f"{t!r} should turn off")
    for t in ["conversation", "let's have a conversation", "start a conversation",
              "I want to start conversation mode later", "what is conversation mode",
              "can you turn conversation mode on", "conversation mode", "stop", "stop talking",
              "start dictation", "stop dictation", "mute", "voice on", "voice off",
              "enter the conversation mode settings", "conversation mode on the phone",
              "the conversation mode is off", "hey Jarvis", "", None]:
        check(speaker_mod.conversation_command(t) is None, f"{t!r} must not be a conversation command")


def test_no_collision_with_dictation_and_mute():
    for t in ["conversation mode on", "conversation mode off", "start conversation mode", "stop conversation mode",
              "enter conversation mode", "exit conversation mode"]:
        check(dictation_command(t) is None, f"{t!r} must not be a dictation command")
        check(speaker_mod.voice_command(t) is None, f"{t!r} must not be a mute command")
    for t in ["start dictation", "stop dictation", "dictation mode on", "mute", "unmute", "voice off", "voice on"]:
        check(speaker_mod.conversation_command(t) is None, f"{t!r} must not be a conversation command")


# ---------------------------------------------------------------- state
def test_default_on_and_persistence():
    STATE.unlink(missing_ok=True)
    check(speaker_mod.load_conversation_mode(STATE) is True, "no file -> ON")
    STATE.write_text(json.dumps({"voice_muted": True}))
    check(speaker_mod.load_conversation_mode(STATE) is True, "no key -> ON")
    STATE.write_text("{not json")
    check(speaker_mod.load_conversation_mode(STATE) is True, "corrupt file -> ON")
    STATE.write_text(json.dumps({"conversation_mode": "off"}))
    check(speaker_mod.load_conversation_mode(STATE) is True, "junk value -> ON")
    STATE.write_text(json.dumps({"voice_muted": True, "session_id": "abc"}))
    speaker_mod.save_conversation_mode(False, STATE)
    st = json.loads(STATE.read_text())
    check(st == {"voice_muted": True, "session_id": "abc", "conversation_mode": False}, f"other keys kept: {st}")
    check(speaker_mod.load_conversation_mode(STATE) is False, "saved OFF is read back")
    speaker_mod.save_conversation_mode(True, STATE)
    check(speaker_mod.load_conversation_mode(STATE) is True, "saved ON is read back")


def test_set_conversation_mode_persists_and_emits():
    reset_state()
    sub = bus.subscribe()
    check(main_mod.set_conversation_mode(False) is False and main_mod.conversation_mode() is False, "set off")
    check(json.loads(STATE.read_text())["conversation_mode"] is False, "persisted off")
    main_mod.set_conversation_mode(True, persist=False)
    check(json.loads(STATE.read_text())["conversation_mode"] is False, "persist=False leaves the file alone")
    evs = []
    while (e := sub.get_nowait()) is not None:
        evs.append(e)
    sub.close()
    got = [e["on"] for e in evs if e["kind"] == "conversation_mode"]
    check(got == [False, True], f"events {got}")


def test_snapshot_default_and_event():
    b = events.EventBus()
    check(b.get_state()["conversation_mode"] is True, "snapshot default is ON")
    b.emit("conversation_mode", on=False)
    check(b.get_state()["conversation_mode"] is False, "event updates the snapshot")
    b.emit("conversation_mode", on=True)
    check(b.get_state()["conversation_mode"] is True, "and back")
    check(isinstance(b.get_state()["conversation"], list), "the chat log field is untouched")


# ---------------------------------------------------------------- loop behaviour
class FakeListener:
    def watch_for_wake(self, done):
        done.wait(0.05)
        return False


class FakeBrain:
    hold_background = False
    spoke = None

    def stop_speech(self):
        pass

    async def flush_background(self):
        return self.spoke


class FakeSpeaker:
    muted = False


async def _converse_once(speaker, on):
    main_mod._conversation_on = on
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    fut.set_result(("a reply", "m"))

    async def fake_speak(sp, li, text):
        return False

    async def take_turn(kind):
        return None, "wake"

    old = main_mod.speak_with_barge_in
    main_mod.speak_with_barge_in = fake_speak
    try:
        return await asyncio.wait_for(main_mod.converse([fut], brain=FakeBrain(), speaker=speaker,
                                                        listener=FakeListener(), take_turn=take_turn,
                                                        submit=lambda t: fut), 5)
    finally:
        main_mod.speak_with_barge_in = old


def test_on_returns_followup_off_returns_wake():
    check(asyncio.run(_converse_once(FakeSpeaker(), True)) == "followup", "ON -> followup")
    check(asyncio.run(_converse_once(FakeSpeaker(), False)) == "wake", "OFF -> wake (no follow-up window)")
    sp = FakeSpeaker()
    sp.muted = True
    check(asyncio.run(_converse_once(sp, True)) == "wake", "muted still returns to wake")
    check(asyncio.run(_converse_once(sp, False)) == "wake", "muted + off -> wake")
    main_mod._conversation_on = True


def test_dictation_wins():
    class D:
        phase = "recording"
        active = True

    old = main_mod._dictation
    main_mod._dictation = D()
    try:
        check(asyncio.run(_converse_once(FakeSpeaker(), True)) == "wake", "dictation active -> wake even when ON")
        check(main_mod.after_reply_mode(FakeSpeaker()) == "wake", "after_reply_mode: dictation wins")
    finally:
        main_mod._dictation = old
        main_mod._conversation_on = True
    check(main_mod.after_reply_mode(FakeSpeaker()) == "followup", "back to followup when idle and ON")


def test_flush_mode_respects_the_switch():
    async def run(on, spoke):
        main_mod._conversation_on = on
        b = FakeBrain()
        b.spoke = spoke
        return await asyncio.wait_for(main_mod.flush_mode(b, FakeListener()), 5)
    check(asyncio.run(run(True, True)) == "followup", "background speech + ON -> followup")
    check(asyncio.run(run(False, True)) == "wake", "background speech + OFF -> wake")
    check(asyncio.run(run(True, None)) == "wake", "nothing spoken -> wake")
    main_mod._conversation_on = True


def test_loop_source_guards():
    src = (ROOT / "jarvis" / "__main__.py").read_text(encoding="utf-8")
    check('if idle["waiting"] and _conversation_on:' in src, "background speech must not open a window when OFF")
    check("if _conversation_on:  # (it may have been switched off since)" in src, "idle 'spoke' re-checks the switch")
    check("set_conversation_mode(load_conversation_mode(), persist=False)" in src, "saved choice loaded at start")
    check('if mode == "followup" and not _conversation_on:' in src, "a switched-off follow-up falls back to wake")


# ---------------------------------------------------------------- voice commands
def test_local_command_voice():
    reset_state()
    r = asyncio.run(main_mod.local_command("Conversation mode off.", None, None))
    check(r == "Conversation mode off." and main_mod.conversation_mode() is False, f"off: {r!r}")
    check(json.loads(STATE.read_text())["conversation_mode"] is False, "persisted")
    r = asyncio.run(main_mod.local_command("start conversation mode", FakeSpeaker(), None))
    check(r == "Conversation mode on." and main_mod.conversation_mode() is True, f"on: {r!r}")
    check(len(r.split()) <= 4, "very short acknowledgement")


def test_typed_page_turn_is_not_spoken():
    reset_state()
    spoken = []

    async def fake_plain(sp, text):
        spoken.append(text)

    old = main_mod.speak_plain
    main_mod.speak_plain = fake_plain
    try:
        asyncio.run(main_mod.typed_turn("conversation mode off", FakeSpeaker(), None))  # typed: shown, not spoken
        check(spoken == [] and main_mod.conversation_mode() is False, f"typed turn spoke {spoken}")
    finally:
        main_mod.speak_plain = old
        main_mod._conversation_on = True


# ---------------------------------------------------------------- page route
class Srv:
    def __init__(self, on_conversation):
        self.calls = []

        def hook(on):
            self.calls.append(on)
            return on_conversation(on)

        self.web = web_mod.WebServer(lambda t: None, lambda t: {"ok": True}, lambda: None, port=0, token=TOKEN,
                                     event_bus=bus, url_file=TMP / "url.txt", on_conversation=hook)
        assert self.web.start(), "server did not start"
        self.port = self.web.port

    def req(self, method, path, body=None, headers=None, host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Type", "application/json")
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


def test_post_conversation():
    reset_state()
    srv = Srv(lambda on: main_mod.set_conversation_mode(on))
    tok = {"X-Jarvis-Token": TOKEN}
    try:
        st, _ = srv.req("POST", "/conversation", {"on": False})
        check(st == 401 and srv.calls == [], f"token required, got {st}")
        st, _ = srv.req("POST", "/conversation", {"on": False}, headers={**tok, "Origin": "http://evil.example"})
        check(st == 403 and srv.calls == [], f"bad Origin, got {st}")
        st, _ = srv.req("POST", "/conversation", {"on": False}, headers=tok, host="evil.example")
        check(st == 403 and srv.calls == [], f"bad Host, got {st}")
        for bad in ({}, {"on": "yes"}, {"on": 1}, {"on": 0}, {"on": None}, {"muted": True}, {"on": [True]}):
            st, _ = srv.req("POST", "/conversation", bad, headers=tok)
            check(st == 400 and srv.calls == [], f"bad body {bad} -> {st}")
        st, _ = srv.req("POST", "/conversation", raw=b"{nope", headers=tok)
        check(st == 400 and srv.calls == [], f"malformed JSON -> {st}")
        st, _ = srv.req("POST", "/conversation", raw=b"x" * (web_mod.MAX_BODY + 10), headers=tok)
        check(st == 413 and srv.calls == [], f"oversize -> {st}")
        st, _ = srv.req("GET", f"/conversation?token={TOKEN}")
        check(st == 405, f"GET refused, got {st}")
        sub = bus.subscribe()
        st, body = srv.req("POST", "/conversation", {"on": False}, headers=tok)
        check(st == 200 and json.loads(body) == {"ok": True, "on": False}, f"off -> {st} {body}")
        check(main_mod.conversation_mode() is False, "mode is off")
        evs = []
        while (e := sub.get_nowait()) is not None:
            evs.append(e)
        sub.close()
        check(any(e["kind"] == "conversation_mode" and e["on"] is False for e in evs), "event emitted")
        st, body = srv.req("GET", f"/state?token={TOKEN}")
        check(json.loads(body)["conversation_mode"] is False, "snapshot carries conversation_mode")
        st, body = srv.req("POST", "/conversation", {"on": True}, headers=tok)
        check(json.loads(body) == {"ok": True, "on": True}, "on")
        check(json.loads(srv.req("GET", f"/state?token={TOKEN}")[1])["conversation_mode"] is True, "snapshot on")
    finally:
        srv.web.stop()
    # no hook -> refused; failing hook -> 500 without details
    web = web_mod.WebServer(lambda t: None, lambda t: {}, lambda: None, port=0, token=TOKEN, event_bus=bus,
                            url_file=TMP / "url2.txt")
    web.start()
    try:
        s2 = Srv.__new__(Srv)
        s2.port = web.port
        check(s2.req("POST", "/conversation", {"on": True}, headers=tok)[0] == 400, "no hook -> 400")
    finally:
        web.stop()

    def boom(on):
        raise RuntimeError("secret detail")
    srv = Srv(boom)
    try:
        st, body = srv.req("POST", "/conversation", {"on": True}, headers=tok)
        check(st == 500 and b"secret detail" not in body, f"500 without details, got {st} {body}")
    finally:
        srv.web.stop()
    main_mod._conversation_on = True


# ---------------------------------------------------------------- page markup
def test_page_markup():
    for needle in ('id="convPill"', 'id="convToggle"', "Conversation on", "Conversation off", "aria-pressed",
                   '"/conversation"', 'case "conversation_mode"', "s.conversation_mode", "Turn conversation off",
                   "Turn conversation on", "aria-label"):
        check(needle in HTML, f"page lacks {needle}")
    m = re.search(r"function setConversation\(.*?\n  }\n", HTML, re.S)
    check(m is not None, "setConversation found")
    body = m.group(0)
    check("textContent" in body and "innerHTML" not in body and "insertAdjacentHTML" not in body, "textContent only")
    check('setAttribute("aria-pressed"' in body and 'setAttribute("aria-label"' in body, "aria state kept")
    check("innerHTML" not in HTML, "no innerHTML anywhere in the page")
    h = web_mod.page_headers(HTML)["Content-Security-Policy"]
    check("script-src 'sha256-" in h, "CSP script-src is hash based")
    check("default-src 'none'" in h and "connect-src 'self'" in h, "CSP otherwise unchanged")


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            PASSED.append(name)
            print(f"PASS {name}")
        except Exception:
            FAILED.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
