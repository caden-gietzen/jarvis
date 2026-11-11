r"""Offline tests for the voice on/off (mute) toggle.

Run:
    set JARVIS_NOTIFY_PHONE=0 && .venv\Scripts\python.exe tests\test_voice_mute.py

Plain script. No audio, no network: the Speaker gets a fake engine and a fake _play. Every state file is
a temp path (the real .jarvis_state.json is never touched). Covers: phrase matching (no false positives),
speak() silent while muted, spoken permission prompts while muted (and silent with the config key off),
persistence, POST /voice, muting cuts speech in progress, local_command / speak_plain / typed_turn /
background and narration speech all respecting the flag.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import asyncio
import http.client
import io
import json
import os
import site
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

os.environ["JARVIS_NOTIFY_PHONE"] = "0"  # never a real push

from rich.console import Console  # noqa: E402

from jarvis import __main__ as main_mod  # noqa: E402
from jarvis import brain as brain_mod  # noqa: E402
from jarvis import speaker as speaker_mod  # noqa: E402
from jarvis import state_file, ui, web as web_mod  # noqa: E402
from jarvis.events import bus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-mute-"))
STATE = TMP / "state.json"
speaker_mod.STATE_PATH = STATE
brain_mod.STATE_PATH = TMP / "brain-state.json"
ui._log_path = TMP / "test.log"
ui.console = Console(file=io.StringIO())
TOKEN = "test-token-abc123"
PASSED, FAILED = [], []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


class FakeEngine:
    def __init__(self):
        self.texts = []

    def chunks(self, text):
        self.texts.append(text)
        import numpy as np
        yield np.zeros(100, dtype="float32"), 22050


def make_speaker(muted=False, permission_prompts=True, hold_s=0.0):
    """A real Speaker (engine none at first) with a fake engine and a fake _play. hold_s: how long a play lasts
    (ends early when stop is set, like the real one)."""
    cfg = {"tts": {"chime": False, "engine": "none"}, "muted_speaks_permission_prompts": permission_prompts}
    STATE.write_text(json.dumps({"voice_muted": muted}))
    sp = speaker_mod.Speaker(cfg)
    sp.engine = FakeEngine()
    sp.started = threading.Event()

    def play(audio, sr, stop, deadline):
        sp.started.set()
        return "stopped" if stop.wait(hold_s) else "done"

    sp._play = play
    return sp


# ---------------------------------------------------------------- phrase matching
def test_phrases():
    for t in ["mute", "Mute.", "MUTE!", "voice off", "Voice off.", "go quiet", "be quiet", "Be quiet.", "mute voice",
              "turn off your voice", "Turn off your voice.", "Jarvis, mute", "please mute", "mute please", "  mute  "]:
        check(speaker_mod.voice_command(t) == "mute", f"{t!r} should mute")
    for t in ["unmute", "Unmute.", "un-mute", "voice on", "Voice on!", "turn on your voice", "you can talk again",
              "You can talk again.", "Jarvis, voice on", "unmute voice", "Unmute voice.", "unmute your voice",
              "Unmute yourself!", "un-mute yourself", "please unmute voice"]:
        check(speaker_mod.voice_command(t) == "unmute", f"{t!r} should unmute")
    for t in ["stop", "stop talking", "Stop talking.", "mute the music", "can you mute the tv", "please don't mute",
              "I want to mute the microphone in the call", "what does the mute button do", "voice", "voice off the record",
              "turn on your voice recorder", "the voice on the left", "don't be quiet", "go quietly", "", None,
              "switch to the Ryan voice", "use Sonnet", "mute everything", "unmute the speakers", "hey Jarvis", "unmute voice recorder",
              "unmute yourself now", "unmute your voice box"]:
        check(speaker_mod.voice_command(t) is None, f"{t!r} must not be a voice command")


# ---------------------------------------------------------------- speaker
def test_muted_speak_is_silent():
    sp = make_speaker(muted=True)
    stop = threading.Event()
    check(sp.speak("Hello there.", stop) is True, "muted speak returns True")
    check(sp.engine.texts == [], "muted speak must not synthesize")
    sp.set_muted(False)
    check(sp.speak("Hello there.", stop) is True and sp.engine.texts == ["Hello there."], "unmuted speaks")


def test_still_prints_when_muted():
    sp = make_speaker(muted=True)
    printed = []
    old = ui.jarvis
    ui.jarvis = lambda text, label="": printed.append(text)

    class FakeBrain:
        model = "m"

        async def ask(self, text):
            return "A reply."
    try:
        asyncio.run(main_mod.typed_turn("hello", sp, FakeBrain(), speak=True))
    finally:
        ui.jarvis = old
    check(printed == ["A reply."], f"reply still printed, got {printed}")
    check(sp.engine.texts == [], "reply not spoken while muted")


def test_permission_speech_while_muted():
    sp = make_speaker(muted=True, permission_prompts=True)
    tok = speaker_mod.PERMISSION_SPEECH.set(True)
    try:
        sp.speak("Can I run this? Say yes or no.", threading.Event())
    finally:
        speaker_mod.PERMISSION_SPEECH.reset(tok)
    check(sp.engine.texts == ["Can I run this? Say yes or no."], "permission prompt spoken while muted")
    sp.speak("Not a prompt.", threading.Event())
    check(len(sp.engine.texts) == 1, "ordinary speech still silent")

    sp = make_speaker(muted=True, permission_prompts=False)
    tok = speaker_mod.PERMISSION_SPEECH.set(True)
    try:
        sp.speak("Can I run this? Say yes or no.", threading.Event())
    finally:
        speaker_mod.PERMISSION_SPEECH.reset(tok)
    check(sp.engine.texts == [], "permission prompt silent when the config key is false")


def test_persistence():
    p = TMP / "persist.json"
    check(speaker_mod.load_muted(p) is False, "missing file -> not muted")
    p.write_text(json.dumps({"session_id": "abc", "project_dir": "x"}))
    speaker_mod.save_muted(True, p)
    d = json.loads(p.read_text())
    check(d == {"session_id": "abc", "project_dir": "x", "voice_muted": True}, f"other keys kept: {d}")
    check(speaker_mod.load_muted(p) is True, "round trip muted")
    speaker_mod.save_muted(False, p)
    check(speaker_mod.load_muted(p) is False, "round trip unmuted")
    p.write_text("not json")
    check(speaker_mod.load_muted(p) is False, "garbage -> not muted")
    # a Speaker starts from the saved flag, and set_muted saves it
    sp = make_speaker(muted=True)
    check(sp.muted is True, "Speaker starts muted from the state file")
    sp.set_muted(False)
    check(json.loads(STATE.read_text())["voice_muted"] is False, "set_muted persisted")
    sp.set_muted(True, persist=False)
    check(json.loads(STATE.read_text())["voice_muted"] is False, "persist=False leaves the file alone")
    STATE.write_text(json.dumps({"voice_muted": False}))


def test_brain_state_write_keeps_flag():
    # the session-id write in Brain._on_result merges instead of overwriting
    brain_mod.STATE_PATH.write_text(json.dumps({"voice_muted": True, "session_id": "old"}))
    merged = state_file.update_state(brain_mod.STATE_PATH, session_id="new", project_dir="p")
    check(merged["voice_muted"] is True and merged["session_id"] == "new", "merge keeps voice_muted")
    check(brain_mod._load_state() == merged, "_load_state reads what update_state wrote")
    src = (ROOT / "jarvis" / "brain.py").read_text(encoding="utf-8")
    check("update_state(STATE_PATH" in src, "brain._on_result must use the shared state helper")


def test_state_file_concurrent_writers():
    p = TMP / "concurrent.json"
    p.write_text(json.dumps({"other": 1}))
    errors = []

    def muter():
        for i in range(150):
            speaker_mod.save_muted(i % 2 == 0, p)

    def session():
        try:
            for i in range(150):
                state_file.update_state(p, session_id=f"s{i}", project_dir="p")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        for _ in range(150):
            brain_mod_read = state_file.read_state(p)
            if "other" not in brain_mod_read:
                errors.append(AssertionError(f"reader saw a torn/partial file: {brain_mod_read}"))

    ts = [threading.Thread(target=f) for f in (muter, session, muter, session, reader)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    check(not errors, f"errors: {errors}")
    d = json.loads(p.read_text())
    check({"voice_muted", "session_id", "project_dir", "other"} <= set(d), f"all keys survive: {d}")
    check(d["session_id"] == "s149", f"last session write kept: {d}")
    check(not list(TMP.glob("concurrent.json.*.tmp")), "no temp files left behind")


def test_state_file_corrupt():
    p = TMP / "corrupt.json"
    check(state_file.read_state(p) == {}, "missing -> {}")
    p.write_text("{not json")
    check(state_file.read_state(p) == {}, "corrupt -> {}")
    p.write_text("[1, 2]")
    check(state_file.read_state(p) == {}, "non-dict -> {}")
    state_file.update_state(p, a=1)
    check(json.loads(p.read_text()) == {"a": 1}, "update over corrupt file rewrites it")
    # a transient bad read (writer mid-flight) is retried once, so valid keys are not dropped
    p.write_text(json.dumps({"keep": True}))
    real = state_file._read_once
    calls = []

    def flaky(path):
        calls.append(1)
        return None if len(calls) == 1 else real(path)
    state_file._read_once = flaky
    try:
        d = state_file.update_state(p, b=2)
    finally:
        state_file._read_once = real
    check(d == {"keep": True, "b": 2} and len(calls) == 2, f"retry kept keys: {d}, calls {len(calls)}")


def test_mute_cuts_speech_in_progress():
    sp = make_speaker(muted=False, hold_s=10)
    stop = threading.Event()
    res = {}
    t = threading.Thread(target=lambda: res.update(r=sp.speak("A long reply. Really long.", stop)), daemon=True)
    t.start()
    check(sp.started.wait(3), "speech started")
    t0 = time.monotonic()
    sp.set_muted(True, persist=False)
    t.join(3)
    check(not t.is_alive(), "speak ended after muting")
    check(time.monotonic() - t0 < 2, "cut off promptly")
    check(res["r"] is False, "cut speech reports False")
    sp.set_muted(False, persist=False)


def test_mute_does_not_cut_permission_prompt():
    sp = make_speaker(muted=False, hold_s=0.6)
    res = {}

    def go():
        tok = speaker_mod.PERMISSION_SPEECH.set(True)
        res["r"] = sp.speak("Delete it? Say yes or no.", threading.Event())

    t = threading.Thread(target=go, daemon=True)
    t.start()
    check(sp.started.wait(3), "prompt started")
    sp.set_muted(True, persist=False)
    t.join(5)
    check(res.get("r") is True, "a permission prompt in progress is not cut by muting")
    sp.set_muted(False, persist=False)


# ---------------------------------------------------------------- __main__ glue
def test_local_command_and_speak_plain():
    sp = make_speaker(muted=False)
    saved = []

    async def body():
        r = await main_mod.local_command("Mute.", sp, None)
        check(r == "Voice off.", f"mute reply {r!r}")
        check(sp.engine.texts == ["Voice off."], f"ack spoken once before muting: {sp.engine.texts}")
        check(sp.muted is True, "muted afterwards")
        await main_mod.speak_plain(sp, r)  # the caller's own attempt to speak the reply
        check(sp.engine.texts == ["Voice off."], "no second speech while muted")
        check(await main_mod.speak_with_barge_in(sp, None, "hello") is False, "barge-in speech skipped when muted")
        check(sp.engine.texts == ["Voice off."], "barge-in speech silent")
        r = await main_mod.local_command("voice on", sp, None)
        check(r == "Voice on." and sp.muted is False, "unmuted")
        check(sp.engine.texts == ["Voice off."], "voice loop caller speaks 'Voice on.', not local_command")
        await main_mod.speak_plain(sp, r)
        check(sp.engine.texts[-1] == "Voice on.", "caller speaks 'Voice on.' once the voice is back")
        n = len(sp.engine.texts)
        sp.set_muted(True, persist=False)
        r = await main_mod.local_command("unmute", sp, None, speak_reply=False)  # typed turn: not spoken by the caller
        check(r == "Voice on." and sp.engine.texts[n:] == ["Voice on."], "typed unmute speaks its own ack")
        # not caught: stop talking / ordinary sentences / chat mode
        check(await main_mod.local_command("stop talking", sp, None) is None, "stop talking is not mute")
        check(await main_mod.local_command("can you mute the music for me", sp, None) is None, "sentence not caught")
        check(sp.muted is False, "still unmuted")
        check("no voice" in (await main_mod.local_command("mute", None, None)).lower(), "chat mode: no voice to mute")
    asyncio.run(body())
    check(json.loads(STATE.read_text())["voice_muted"] is False, "final state persisted")


def test_converse_follow_up_mode():
    # muted: a spoken reply is not said, so there is no follow-up window to offer
    src = (ROOT / "jarvis" / "__main__.py").read_text(encoding="utf-8")
    check("return after_reply_mode(speaker)" in src and "is_muted(speaker)" in src, "converse ends in wake mode while muted")


# ---------------------------------------------------------------- brain
class FakeListener:
    def priority(self):
        import contextlib
        return contextlib.nullcontext()

    def record_utterance(self, followup_s=0.0, priority=False):
        return "audio"


def make_brain(sp, answers=("yes",)):
    ans = list(answers)
    return brain_mod.Brain(str(ROOT), speaker=sp, listener=FakeListener(),
                           transcribe=lambda audio: ans.pop(0) if ans else "")


def test_brain_permission_prompt_spoken_while_muted():
    async def body(prompts, expect_prompt):
        sp = make_speaker(muted=True, permission_prompts=prompts)
        b = make_brain(sp)
        res = await b.ask_permission("WebFetch", {"url": "http://example.com/x"}, None)
        check(type(res).__name__ == "PermissionResultAllow", f"yes allowed ({type(res).__name__})")
        said = sp.engine.texts
        check(any("yes or no" in t.lower() for t in said) == expect_prompt, f"prompt spoken={expect_prompt}: {said}")
        check(not any("going ahead" in t.lower() for t in said), f"acknowledgement stays silent while muted: {said}")
        check(brain_mod_perm_var_clear(), "permission flag not leaked")

    asyncio.run(asyncio.wait_for(body(True, True), 20))
    asyncio.run(asyncio.wait_for(body(False, False), 20))


def brain_mod_perm_var_clear():
    return speaker_mod.PERMISSION_SPEECH.get() is False


def test_brain_narration_and_background_respect_mute():
    async def body():
        sp = make_speaker(muted=True)
        b = make_brain(sp)
        await b._say_narration("On it.")
        check(sp.engine.texts == [], "narration silent while muted")
        b._background_queue.append("A background agent finished.")
        spoke = await b.flush_background()
        check(spoke is False, "nothing counts as spoken while muted (no follow-up window)")
        check(b._background_queue == [], "queued background reply dropped, not stuck")
        check(sp.engine.texts == [], "background reply silent while muted")
        sp.set_muted(False, persist=False)
        b._background_queue.append("Another one finished.")
        check(await b.flush_background() is True, "unmuted: background speech works")
        check(sp.engine.texts == ["Another one finished."], f"spoken: {sp.engine.texts}")
        await b._say_narration("Back on.")
        check(sp.engine.texts[-1] == "Back on.", "narration spoken when unmuted")
    asyncio.run(asyncio.wait_for(body(), 20))


def test_say_emits_no_speaking_mode_while_muted():
    async def body():
        sp = make_speaker(muted=True)
        b = make_brain(sp)
        sub = bus.subscribe()
        await b._say("hello")
        ev = sub.get_nowait()
        sub.close()
        check(ev is None, f"no events while muted, got {ev}")
    asyncio.run(asyncio.wait_for(body(), 10))


# ---------------------------------------------------------------- events + web
def test_event_snapshot():
    sub = bus.subscribe()
    bus.emit("voice", muted=True)
    check(bus.get_state()["voice_muted"] is True, "snapshot shows muted")
    ev = sub.get_nowait()
    check(ev and ev["kind"] == "voice" and ev["muted"] is True, f"event: {ev}")
    bus.emit("voice", muted=False)
    check(bus.get_state()["voice_muted"] is False, "snapshot shows unmuted")
    sub.close()


class Srv:
    def __init__(self, sp):
        self.sp = sp
        self.calls = []

        def on_voice(muted):
            self.calls.append(muted)
            sp.set_muted(muted, persist=False)
            return sp.muted

        self.web = web_mod.WebServer(lambda t: None, lambda t: {"ok": True}, lambda: None, port=0, token=TOKEN,
                                     event_bus=bus, url_file=TMP / "url.txt", on_voice=on_voice)
        assert self.web.start(), "server did not start"
        self.port = self.web.port

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                c.putheader(k, v)
            if data is not None:
                c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


def test_post_voice():
    sp = make_speaker(muted=False)
    srv = Srv(sp)
    tok = {"X-Jarvis-Token": TOKEN}
    try:
        st, _ = srv.req("POST", "/voice", {"muted": True})
        check(st == 401 and srv.calls == [], f"token required, got {st}")
        st, _ = srv.req("POST", "/voice", {"muted": True}, headers={**tok, "Origin": "http://evil.example"})
        check(st == 403 and srv.calls == [], f"bad Origin rejected, got {st}")
        st, _ = srv.req("POST", "/voice", {"muted": True}, headers=tok, host="evil.example")
        check(st == 403 and srv.calls == [], f"wrong Host rejected, got {st}")
        for bad in ({}, {"muted": "yes"}, {"muted": 1}, {"muted": None}):
            st, _ = srv.req("POST", "/voice", bad, headers=tok)
            check(st == 400 and srv.calls == [], f"bad body {bad} -> {st}")
        sub = bus.subscribe()
        st, body = srv.req("POST", "/voice", {"muted": True}, headers=tok)
        check(st == 200 and json.loads(body) == {"ok": True, "muted": True}, f"mute -> {st} {body}")
        check(sp.muted is True, "speaker muted")
        evs = []
        while (e := sub.get_nowait()) is not None:
            evs.append(e)
        check(any(e["kind"] == "voice" and e["muted"] is True for e in evs), f"voice event emitted: {evs}")
        st, body = srv.req("GET", f"/state?token={TOKEN}")
        check(st == 200 and json.loads(body)["voice_muted"] is True, "snapshot carries voice_muted")
        st, body = srv.req("POST", "/voice", {"muted": False}, headers=tok)
        check(st == 200 and json.loads(body) == {"ok": True, "muted": False}, "unmute")
        check(json.loads(srv.req("GET", f"/state?token={TOKEN}")[1])["voice_muted"] is False, "snapshot unmuted")
        sub.close()
    finally:
        srv.web.stop()
    # a server without a voice hook (no speaker) refuses
    web = web_mod.WebServer(lambda t: None, lambda t: {}, lambda: None, port=0, token=TOKEN, event_bus=bus,
                            url_file=TMP / "url2.txt")
    web.start()
    try:
        s2 = Srv.__new__(Srv)
        s2.port = web.port
        st, _ = s2.req("POST", "/voice", {"muted": True}, headers=tok)
        check(st == 400, f"no hook -> 400, got {st}")
    finally:
        web.stop()


def test_post_voice_500_has_security_headers():
    def boom(muted):
        raise RuntimeError("x")
    web = web_mod.WebServer(lambda t: None, lambda t: {}, lambda: None, port=0, token=TOKEN, event_bus=bus,
                            url_file=TMP / "url3.txt", on_voice=boom)
    web.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=10)
        body = json.dumps({"muted": True}).encode()
        c.request("POST", "/voice", body, {"Host": f"127.0.0.1:{web.port}", "X-Jarvis-Token": TOKEN,
                                           "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        check(r.status == 500, f"500 expected, got {r.status}")
        for k in web_mod._SECURITY_HEADERS:
            check(r.getheader(k) is not None, f"500 response missing {k}")
    finally:
        web.stop()


def test_page_wiring():
    html = (ROOT / "jarvis" / "webui.html").read_text(encoding="utf-8")
    for needle in ('id="voicePill"', 'id="voiceToggle"', 'aria-pressed', "Unmute voice", "Mute voice", '"/voice"',
                   "s.voice_muted", 'case "voice"'):
        check(needle in html, f"page missing {needle}")
    check("innerHTML" not in html.split("function setVoice")[1].split("function setMode")[0], "setVoice uses textContent only")
    headers = web_mod.page_headers(html)
    check("sha256-" in headers["Content-Security-Policy"], "CSP still hash based")


def test_config_key():
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    check(cfg.get("muted_speaks_permission_prompts") is True, "config key present and true")


def test_main_imports_and_say_ignores_mute():
    check(callable(main_mod.local_command) and callable(main_mod.is_muted), "helpers exist")
    src = (ROOT / "jarvis" / "__main__.py").read_text(encoding="utf-8")
    check("speaker.muted = False" in src, "`say` always speaks")


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
    total = len(PASSED) + len(FAILED)
    print(f"\n{len(PASSED)}/{total} passed")
    if STATE.exists() and json.loads(STATE.read_text()).get("voice_muted"):
        print("note: temp state left muted (temp file only)")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
