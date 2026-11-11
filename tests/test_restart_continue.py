r"""Offline tests for the intentional-restart marker and spoken auto-continue (jarvis/restart.py,
__main__.start_restart). Plain script: run with the venv python. Temp paths only; the real marker and
flag files are never touched; no process is launched.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import json
import site
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from jarvis import restart, ui, usage_monitor  # noqa: E402
import jarvis.__main__ as jmain  # noqa: E402

ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-restart-continue.log"
restart.workspace_missing_message = lambda root=None: None  # the tests must not depend on a workspace folder existing
NOW = 1_800_000_000.0
REAL_MARKER, REAL_FLAG = ROOT / ".jarvis_intentional_restart.json", ROOT / ".jarvis_restart_pending"
results = []


def case(fn):
    try:
        fn()
        print(f"PASS {fn.__name__}")
        results.append(True)
    except Exception:
        print(f"FAIL {fn.__name__}")
        traceback.print_exc()
        results.append(False)
    return fn


def tmp():
    d = Path(tempfile.mkdtemp(prefix="jarvis-rc-"))
    restart.now_fn = lambda: NOW
    restart.sleep_fn = lambda s: None
    return d, d / "marker.json", d / "history.json"


class Rec:
    def __init__(self, fail_speak=False, fail_submit=False):
        self.calls, self.logs = [], []
        self.fail_speak, self.fail_submit = fail_speak, fail_submit

    def speak(self, t):
        self.calls.append(("speak", t))
        if self.fail_speak:
            raise RuntimeError("audio")

    def submit(self, t):
        self.calls.append(("submit", t))
        if self.fail_submit:
            raise RuntimeError("brain")

    def log(self, m):
        self.logs.append(m)


def run(marker, rec, **kw):
    t = restart.schedule_return(rec.submit, rec.speak, rec.log, marker=marker, delay_s=0, **kw)
    t.join(5)


@case
def marker_written_when_restart_performed():
    d, marker, hist = tmp()
    launched = []
    real_write = restart.write_marker
    saved = (restart.flag_reason, restart.clear_restart, restart.write_marker, restart.launch_restart_script)
    try:
        restart.flag_reason = lambda: "usage-limit reset"
        restart.clear_restart = lambda: None
        restart.write_marker = lambda reason: real_write(reason, marker, hist)
        restart.launch_restart_script = lambda: launched.append(1) or True
        jmain.start_restart()
    finally:
        restart.flag_reason, restart.clear_restart, restart.write_marker, restart.launch_restart_script = saved
    data = json.loads(marker.read_text())
    assert launched == [1]
    assert data["reason"] == "usage-limit reset" and data["created_at"] == NOW and data["count"] == 1, data


@case
def flag_reason_defaults():
    d, _, _ = tmp()
    f = d / "flag"
    assert restart.flag_reason(f) == "requested"  # missing
    f.write_text("restart")
    assert restart.flag_reason(f) == "requested"
    f.write_text("")  # a bare touch
    assert restart.flag_reason(f) == "requested"
    f.write_text("usage-limit reset")
    assert restart.flag_reason(f) == "usage-limit reset"


@case
def no_marker_on_plain_start():
    d, marker, hist = tmp()
    rec = Rec()
    run(marker, rec)
    assert rec.calls == [] and rec.logs == [] and not marker.exists()
    assert not REAL_MARKER.exists()


@case
def fresh_marker_one_prompt_one_line_deleted_first():
    d, marker, hist = tmp()
    restart.write_marker("requested", marker, hist)
    rec = Rec()
    seen = []
    orig = rec.speak
    rec.speak = lambda t: (seen.append(marker.exists()), orig(t))
    run(marker, rec)
    assert seen == [False], "marker must be gone before anything is spoken"
    assert rec.calls == [("speak", restart.RETURN_LINE), ("submit", restart.RETURN_PROMPT)], rec.calls
    assert rec.logs == ["Intentional restart (requested): continuing"], rec.logs
    run(marker, rec)  # a second start finds nothing: never loops
    assert len(rec.calls) == 2


@case
def stale_or_corrupt_marker_ignored():
    d, marker, hist = tmp()
    for body in (json.dumps({"created_at": NOW - 16 * 60, "reason": "x", "count": 1}), "{not json",
                 json.dumps({"reason": "x"}), json.dumps({"created_at": True})):
        marker.write_text(body)
        rec = Rec()
        run(marker, rec)
        assert rec.calls == [] and not marker.exists(), body


@case
def switch_off():
    d, marker, hist = tmp()
    restart.write_marker("requested", marker, hist)
    rec = Rec()
    run(marker, rec, enabled=False)
    assert rec.calls == [] and not marker.exists()
    assert rec.logs == ["Intentional restart (requested): not continuing"], rec.logs


@case
def loop_guard():
    d, marker, hist = tmp()
    for _ in range(3):
        restart.write_marker("requested", marker, hist)
    assert json.loads(marker.read_text())["count"] == 3
    rec = Rec()
    run(marker, rec)
    assert rec.calls == [("speak", restart.LOOP_LINE)], rec.calls
    # older restarts fall out of the 10 minute window
    d, marker, hist = tmp()
    for _ in range(2):
        restart.write_marker("requested", marker, hist)
    restart.now_fn = lambda: NOW + 11 * 60
    restart.write_marker("requested", marker, hist)
    assert json.loads(marker.read_text())["count"] == 1


@case
def usage_continue_takes_precedence():
    d, marker, hist = tmp()
    restart.write_marker("usage-limit reset", marker, hist)
    rec = Rec()
    run(marker, rec, usage_continue_pending=True)
    assert rec.calls == [] and not marker.exists()
    # the real helper: pending only with a fresh marker and the switch on
    um = usage_monitor
    saved = (um._read_state, um.now_fn, um._auto_continue_enabled)
    try:
        um.now_fn = lambda: NOW
        um._auto_continue_enabled = lambda: True
        um._read_state = lambda: {"continue_pending": True, "created_at": NOW - 5}
        assert um.continue_pending() is True
        um._read_state = lambda: {"continue_pending": True, "created_at": NOW - 7 * 3600}
        assert um.continue_pending() is False
        um._read_state = lambda: {"continue_pending": False, "created_at": NOW}
        assert um.continue_pending() is False
        um._read_state = lambda: None
        assert um.continue_pending() is False
    finally:
        um._read_state, um.now_fn, um._auto_continue_enabled = saved


@case
def speak_failure_not_fatal():
    d, marker, hist = tmp()
    restart.write_marker("requested", marker, hist)
    rec = Rec(fail_speak=True)
    run(marker, rec)
    assert [c[0] for c in rec.calls] == ["speak", "submit"]  # still continues


@case
def submit_failure_not_fatal():
    d, marker, hist = tmp()
    restart.write_marker("requested", marker, hist)
    rec = Rec(fail_submit=True)
    run(marker, rec)
    assert [c[0] for c in rec.calls] == ["speak", "submit"] and not marker.exists()
    assert any("failed" in m for m in rec.logs)


@case
def typed_turn_speaks_only_when_asked():
    import asyncio

    spoken = []

    class Sp:
        def deadline_for(self, t):
            return 1.0

        def speak(self, t, stop):
            spoken.append(t)
            return True

    class Brain:
        model = "m"

        async def ask(self, t):
            return "All good."

    async def fake_handle(text, speaker, brain):
        return None

    saved = jmain.commands.handle
    jmain.commands.handle = fake_handle
    try:
        asyncio.run(jmain.typed_turn("hi", Sp(), Brain()))
        assert spoken == []
        asyncio.run(jmain.typed_turn("hi", Sp(), Brain(), speak=True))
        assert spoken == ["All good."], spoken
    finally:
        jmain.commands.handle = saved


@case
def failed_launch_deletes_marker():
    d, marker, hist = tmp()
    real_write = restart.write_marker
    saved = (restart.flag_reason, restart.clear_restart, restart.write_marker,
             restart.launch_restart_script, restart.MARKER_FILE)
    try:
        restart.flag_reason = lambda: "requested"
        restart.clear_restart = lambda: None
        restart.write_marker = lambda reason: real_write(reason, marker, hist)
        restart.MARKER_FILE = marker
        restart.launch_restart_script = lambda: False
        jmain.start_restart()
        assert not marker.exists(), "marker must be removed when the launch failed"
        restart.launch_restart_script = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        jmain.start_restart()
        assert not marker.exists()
        restart.launch_restart_script = lambda: True
        jmain.start_restart()
        assert marker.exists(), "marker stays after a successful launch"
    finally:
        (restart.flag_reason, restart.clear_restart, restart.write_marker,
         restart.launch_restart_script, restart.MARKER_FILE) = saved
    # a marker older than 15 minutes is still discarded
    restart.now_fn = lambda: NOW + 16 * 60
    assert restart.take_marker(marker) is None


@case
def launch_restart_script_reports_failure():
    d, _, _ = tmp()
    assert restart.launch_restart_script(window_file=d / "missing.json", log_file=d / "log.txt") is False


class SlowSpeaker:
    """Fake speaker: speak() blocks until its stop event is set (or 5 s)."""

    def __init__(self):
        self.started = __import__("threading").Event()
        self.stopped_by_event = False

    def deadline_for(self, t):
        return 10.0

    def speak(self, t, stop):
        self.started.set()
        self.stopped_by_event = stop.wait(5)
        return True


class FakeBrain:
    def __init__(self):
        self.stopped = 0

    def stop_speech(self):
        self.stopped += 1


@case
def wake_word_stops_plain_speech_and_recording_waits():
    import asyncio

    async def go():
        sp, brain = SlowSpeaker(), FakeBrain()
        task = asyncio.ensure_future(jmain.speak_plain(sp, "I'm back."))
        for _ in range(100):
            if sp.started.is_set():
                break
            await asyncio.sleep(0.02)
        assert sp.started.is_set() and jmain.plain_speaking()
        # recording must wait while plain speech plays
        w = asyncio.ensure_future(jmain.wait_plain_silent(2.0))
        await asyncio.sleep(0.2)
        assert not w.done(), "recording must not start while plain speech is playing"
        jmain.stop_all_speech(brain)  # what the wake word path calls
        await asyncio.wait_for(w, 2)
        await asyncio.wait_for(task, 2)
        assert sp.stopped_by_event and brain.stopped == 1
        assert not jmain.plain_speaking() and not jmain._reply_stops

    asyncio.run(go())


@case
def restart_safe_false_while_plain_speech():
    import asyncio

    class B:
        pass

    async def go():
        sp = SlowSpeaker()
        assert jmain.restart_safe(B()) is True
        task = asyncio.ensure_future(jmain.speak_plain(sp, "reply"))
        for _ in range(100):
            if sp.started.is_set():
                break
            await asyncio.sleep(0.02)
        assert jmain.restart_safe(B()) is False
        jmain.stop_reply_speech()
        await asyncio.wait_for(task, 2)
        assert jmain.restart_safe(B()) is True

    asyncio.run(go())


@case
def real_files_untouched():
    assert not REAL_MARKER.exists() and not REAL_FLAG.exists()


print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
