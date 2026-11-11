r"""Offline, silent tests for the freeze fixes: nothing opens a mic or audio device.

Run (base interpreter; jarvis.cmd only runs the package):
    .venv\Scripts\python.exe tests\test_hangs.py

Plain script, no pytest needed. Prints PASS/FAIL per test, exits non-zero on failure.
"""

import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
_os_np.environ.setdefault("JARVIS_TOKEN_LEDGER", "0")  # nor write the real token ledger
import asyncio
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

import numpy as np  # noqa: E402
import torch  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import listener as listener_mod  # noqa: E402
from jarvis import speaker as speaker_mod  # noqa: E402
from jarvis import ui  # noqa: E402
from jarvis.watchdog import Watchdog  # noqa: E402

ERRORS: list[str] = []
ui.error = lambda text: ERRORS.append(text)  # keep the real log file clean
ui.activity = lambda text: None
ui.status = lambda text: None


# ---- listener ---------------------------------------------------------------------------

class FakeWake:
    models = {"fake": None}

    def predict(self, frame):
        return {"fake": 1.0 if np.max(frame) > 1000 else 0.0}

    def reset(self):
        pass


class FakeVad:
    def __call__(self, x, sr):
        return torch.tensor(1.0 if float(x.abs().max()) > 0.01 else 0.0)

    def reset_states(self):
        pass


class FakeStream:
    def __init__(self):
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


def make_listener(**ep):
    cfg = {"audio": {"sample_rate": 16000, "device": None},
           "wake_word": {"model": "x", "threshold": 0.5},
           "endpointing": {"vad_threshold": 0.5, "end_silence_ms": 240, "no_speech_timeout_s": 1,
                           "max_utterance_s": 5, "followup_window_s": 1, **ep}}
    streams = []

    def factory():
        streams.append(FakeStream())
        return streams[-1]

    li = listener_mod.Listener(cfg, wake=FakeWake(), vad=FakeVad(), stream_factory=factory)
    li._last_frame = time.monotonic() + 1000  # pretend the mic is healthy unless a test says otherwise
    return li, streams


def loud():
    return np.full(listener_mod.FRAME, 5000, dtype=np.int16)


def quiet():
    return np.zeros(listener_mod.FRAME, dtype=np.int16)


def feed(li, frames, delay=0.1):
    """Push frames shortly after the call under test starts (it drains stale audio first)."""
    threading.Timer(delay, lambda: [li._q.put(f) for f in frames]).start()


def test_record_followup_times_out_without_frames():
    li, _ = make_listener()
    t = time.monotonic()
    assert li.record_utterance(0.6) is None
    assert time.monotonic() - t < 1.5


def test_record_after_wake_times_out_without_frames():
    li, _ = make_listener(no_speech_timeout_s=0.5)
    t = time.monotonic()
    assert li.record_utterance() is None  # settle (0.8 s) + no-speech (0.5 s)
    assert time.monotonic() - t < 2.5


def test_record_ends_when_mic_dies_mid_utterance():
    li, _ = make_listener()
    feed(li, [loud()] * 4)
    t = time.monotonic()
    audio = li.record_utterance(1.0)
    assert audio is not None and len(audio) >= 4 * listener_mod.FRAME / 2
    assert time.monotonic() - t < 2.0  # 2 x end_silence (0.48 s) + slack, not max_utterance (5 s)


def test_record_normal_utterance_ends_on_silence():
    li, _ = make_listener()
    feed(li, [loud()] * 3 + [quiet()] * 6)
    audio = li.record_utterance(1.0)
    assert audio is not None and 6 * listener_mod.FRAME <= len(audio) <= 7 * listener_mod.FRAME  # VAD carry may delay the silence count by one frame


def test_wait_for_wake_yields_lock_to_priority_and_wakes():
    li, _ = make_listener()
    done = threading.Event()
    threading.Thread(target=lambda: (li.wait_for_wake(), done.set()), daemon=True).start()
    time.sleep(0.3)
    assert not done.is_set()
    t = time.monotonic()
    assert li.record_utterance(0.3, priority=True) is None  # permission prompt gets the mic
    assert time.monotonic() - t < 1.0
    li._q.put(loud())  # now the wake word
    assert done.wait(2)


def test_wake_watcher_and_recorder_run_together_without_stealing_frames():
    li, _ = make_listener()
    stop = threading.Event()
    woke = []
    th = threading.Thread(target=lambda: woke.append(li.watch_for_wake(stop)), daemon=True)
    th.start()
    time.sleep(0.2)
    feed(li, [loud()] * 3 + [quiet()] * 6)
    audio = li.record_utterance(1.0, priority=True)  # recorder gets every frame; the watcher stays muted
    assert audio is not None and len(audio) >= 3 * listener_mod.FRAME
    assert th.is_alive() and not woke  # recorded speech did not fire the wake word
    stop.set()
    th.join(2)
    assert not th.is_alive() and woke == [False]


def test_stall_reopens_stream():
    li, streams = make_listener()
    old = listener_mod.STALL_S
    listener_mod.STALL_S = 0.4
    try:
        li._last_frame = time.monotonic()
        ERRORS.clear()
        stop = threading.Event()
        threading.Thread(target=lambda: li.watch_for_wake(stop), daemon=True).start()
        time.sleep(1.6)
        stop.set()
    finally:
        listener_mod.STALL_S = old
    # initial + reopen at ~0.4 s + backed-off reopen at ~1.2 s
    assert len(streams) >= 3, len(streams)
    assert streams[-1].started
    assert any("reopening" in e for e in ERRORS), ERRORS


# ---- speaker ----------------------------------------------------------------------------

class FakeOutputStream:
    """Stands in for sd.OutputStream. hang=True never finishes; otherwise it finishes at once."""
    hang = False
    open_mode = None  # "block" = constructor never returns, "raise" = PortAudioError-like
    instances: list = []

    def __init__(self, samplerate, channels, dtype, callback, finished_callback):
        if FakeOutputStream.open_mode == "block":
            time.sleep(30)
        if FakeOutputStream.open_mode == "raise":
            raise OSError("output device disappeared")
        self.finished_callback = finished_callback
        self.aborted = False
        FakeOutputStream.instances.append(self)

    def start(self):
        if not FakeOutputStream.hang:
            threading.Timer(0.02, self.finished_callback).start()

    def abort(self):
        self.aborted = True

    def close(self):
        pass


class FakeEngine:
    def __init__(self, items=None, exc=None, block=False):
        self.items, self.exc, self.block = items or [], exc, block

    def chunks(self, text):
        for it in self.items:
            yield it
        if self.exc:
            raise self.exc
        if self.block:
            time.sleep(30)


def chunk():
    return np.zeros(2205, dtype=np.float32), 22050  # 0.1 s


def make_speaker(engine):
    sp = speaker_mod.Speaker({"tts": {"chime": True, "engine": "none"}})
    sp.muted = False  # the live app's .jarvis_state.json may say muted; tests must not depend on it
    sp.engine = engine
    return sp


class patched:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {}
        for k, v in self.kw.items():
            mod, name = k.split("__")
            m = {"sp": speaker_mod, "br": brain_mod}[mod]
            self.old[k] = getattr(m, name)
            setattr(m, name, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            mod, name = k.split("__")
            setattr({"sp": speaker_mod, "br": brain_mod}[mod], name, v)


def fake_sd(hang):
    FakeOutputStream.hang = hang
    FakeOutputStream.open_mode = None
    FakeOutputStream.instances = []

    def boom(*a, **k):
        raise AssertionError("sd.play/sd.stop (shared global stream) must not be used")

    return patched(sp__sd=type("SD", (), {"OutputStream": FakeOutputStream, "CallbackStop": Exception,
                                          "play": boom, "stop": boom, "get_stream": boom}))


def test_speak_normal():
    with fake_sd(False):
        assert make_speaker(FakeEngine([chunk(), chunk()])).speak("hi", threading.Event()) is True


def test_speak_times_out_when_playback_hangs():
    with fake_sd(True), patched(sp__SPEAK_MARGIN_S=1.0, sp__CHUNK_MARGIN_S=0.3):
        t = time.monotonic()
        ERRORS.clear()
        assert make_speaker(FakeEngine([chunk()])).speak("hi", threading.Event()) is False
        assert time.monotonic() - t < 2.0
        time.sleep(0.1)
        assert FakeOutputStream.instances[0].aborted
        assert any("hung" in e for e in ERRORS), ERRORS


def test_speak_barge_in_stops_playback():
    with fake_sd(True):
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        t = time.monotonic()
        assert make_speaker(FakeEngine([chunk()])).speak("hi", stop) is False
        assert time.monotonic() - t < 1.0


def test_speak_producer_error_is_logged_and_returns():
    with fake_sd(False):
        ERRORS.clear()
        t = time.monotonic()
        assert make_speaker(FakeEngine([chunk()], exc=RuntimeError("piper exploded"))).speak("hi", threading.Event()) is False
        assert time.monotonic() - t < 2.0
        assert any("piper exploded" in e for e in ERRORS), ERRORS


def test_speak_producer_stuck_hits_chunk_timeout():
    with fake_sd(False), patched(sp__CHUNK_GET_TIMEOUT_S=0.3):
        t = time.monotonic()
        assert make_speaker(FakeEngine(block=True)).speak("hi", threading.Event()) is False
        assert time.monotonic() - t < 1.5


def test_speak_returns_when_stream_open_blocks():
    with fake_sd(False), patched(sp__OPEN_TIMEOUT_S=0.3):
        FakeOutputStream.open_mode = "block"
        ERRORS.clear()
        t = time.monotonic()
        assert make_speaker(FakeEngine([chunk()])).speak("hi", threading.Event()) is False
        assert time.monotonic() - t < 1.5
        assert any("hung" in e for e in ERRORS), ERRORS


def test_speak_survives_output_device_error():
    with fake_sd(False):
        FakeOutputStream.open_mode = "raise"
        ERRORS.clear()
        assert make_speaker(FakeEngine([chunk()])).speak("hi", threading.Event()) is False  # no exception
        assert any("device disappeared" in e for e in ERRORS), ERRORS


def test_chime_does_not_touch_speech_stream():
    with fake_sd(True), patched(sp__SPEAK_MARGIN_S=1.0, sp__CHUNK_MARGIN_S=0.5):
        sp = make_speaker(FakeEngine([chunk()]))
        th = threading.Thread(target=sp.speak, args=("hi", threading.Event()), daemon=True)
        th.start()
        time.sleep(0.2)
        sp.chime()  # would raise via sd.play/sd.stop if it used the global stream
        time.sleep(0.2)
        assert not FakeOutputStream.instances[0].aborted  # speech stream untouched
        assert len(FakeOutputStream.instances) == 2  # chime has its own stream
        th.join(3)


# ---- brain ------------------------------------------------------------------------------

class FakeSpeaker:
    def __init__(self):
        self.spoken, self.calls = [], 0

    def deadline_for(self, text):
        return 0.2

    def speak(self, text, stop):
        self.calls += 1
        if self.calls == 1:  # first reply: audio hangs, ignoring stop for a while
            time.sleep(1.5)
            return False
        self.spoken.append(text)
        return True


def make_brain(speaker):
    b = brain_mod.Brain(str(ROOT), speaker=speaker)
    return b


def test_background_speech_timeout_releases_lock_and_later_replies_spoken():
    async def run():
        sp = FakeSpeaker()
        b = make_brain(sp)
        b._background_queue += ["first reply.", "second reply."]
        locked_seen = []

        async def watch():
            for _ in range(8):
                locked_seen.append(b._perm_lock.locked())
                await asyncio.sleep(0.05)

        t = time.monotonic()
        await asyncio.gather(b.flush_background(), watch())
        assert time.monotonic() - t < 1.2  # first hung 1.5 s but we gave up after ~0.3 s
        assert not any(locked_seen), "background speech must not hold _perm_lock"
        assert sp.spoken == ["second reply."]
        # the scheduler path: a new reply after a timed-out flush still gets a task and is spoken
        sp.calls = 0
        b._background_queue.append("third reply.")
        b._background_queue.append("fourth reply.")
        b._schedule_flush()
        assert b._flush_task is not None
        await asyncio.wait_for(b._flush_task, 3)
        assert b._flush_task is None or b._flush_task.done()
        b._background_queue.append("fifth reply.")
        b._schedule_flush()  # must schedule again, not be blocked by an unfinished task
        assert b._flush_task is not None
        await asyncio.wait_for(b._flush_task, 3)
        assert sp.spoken[-2:] == ["fourth reply.", "fifth reply."], sp.spoken

    with patched(br__SAY_GRACE_S=0.1):
        asyncio.run(run())


def test_interrupted_background_reply_is_requeued_but_timeout_is_dropped():
    class WaitsForStop:
        def __init__(self):
            self.spoken = []

        def deadline_for(self, text):
            return 5.0

        def speak(self, text, stop):
            if stop.wait(0.5 if self.spoken is not None and text == "hang." else 3):
                return False
            self.spoken.append(text)
            return True

    async def run():
        sp = WaitsForStop()
        b = make_brain(sp)
        b._background_queue.append("important news.")
        task = asyncio.create_task(b.flush_background())
        await asyncio.sleep(0.2)
        b.interrupt_background()  # "hey Jarvis"
        await asyncio.wait_for(task, 2)
        assert b._background_queue == ["important news."]  # back at the front, not lost
        sp.speak = lambda text, stop: sp.spoken.append(text) or True
        await b.flush_background()
        assert sp.spoken == ["important news."]
        # a timeout, by contrast, drops the item (no retry loop)
        sp.speak = lambda text, stop: time.sleep(1) or False
        b._background_queue.append("stuck.")
        await b.flush_background()
        assert b._background_queue == []

    with patched(br__SAY_GRACE_S=0.1):
        ERRORS.clear()
        asyncio.run(run())


def test_speak_with_barge_in_has_outer_timeout_and_swallows_errors():
    import jarvis.__main__ as main_mod

    class HangSpeaker:
        def deadline_for(self, text):
            return 0.2

        def speak(self, text, stop):
            time.sleep(3)

    class BoomSpeaker(HangSpeaker):
        def speak(self, text, stop):
            raise OSError("PortAudioError")

    class Li:
        ended = False

        def watch_for_wake(self, done):
            while not done.is_set():
                time.sleep(0.02)
            Li.ended = True
            return False

    async def run():
        t = time.monotonic()
        assert await main_mod.speak_with_barge_in(HangSpeaker(), Li(), "hello") is False
        assert time.monotonic() - t < 1.5 and Li.ended  # watcher ended too
        assert await main_mod.speak_with_barge_in(BoomSpeaker(), Li(), "hello") is False

    main_mod.SPEAK_GRACE_S = 0.1
    ERRORS.clear()
    asyncio.run(run())
    assert any("timed out" in e for e in ERRORS) and any("PortAudioError" in e for e in ERRORS), ERRORS


def test_turn_active_recovers():
    async def run():
        b = make_brain(FakeSpeaker())
        b._turn_active, b._turn_started = True, time.monotonic()
        assert not b._can_speak_background()
        b._turn_started = time.monotonic() - brain_mod.TURN_STALE_S - 1
        assert b._can_speak_background()  # stale flag ignored

        b._turn_active, b._turn_started = True, time.monotonic()

        class Task:
            def done(self):
                return False

        class Client:
            async def query(self, text):
                pass

        b._reader_task, b.client = Task(), Client()
        fut = asyncio.ensure_future(b.ask("hello"))
        await asyncio.sleep(0.05)
        assert not b._turn_active  # a new ask clears it
        b._outstanding[0].set_result("ok")
        assert await fut == "ok"

    asyncio.run(run())


def test_ask_does_not_hang_when_result_is_classified_background():
    from claude_agent_sdk import ResultMessage

    def result(origin):
        return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                             num_turns=1, session_id="s", result="done", origin=origin)

    class Task:
        def done(self):
            return False

    class Client:
        sent = []

        async def query(self, prompt):
            async for m in prompt:  # ask() sends a stamped stream message
                Client.sent.append(m)

    async def run():
        b = make_brain(FakeSpeaker())
        b._reader_task, b.client = Task(), Client()
        brain_mod.STATE_PATH = Path(tempfile.gettempdir()) / "jarvis-test-state.json"
        fut = asyncio.ensure_future(b.ask("hello"))
        await asyncio.sleep(0.05)
        assert Client.sent[0]["origin"] == {"kind": "human"}
        # the CLI attributes the turn to a background task: the ask stays pending, not resolved
        await b._on_result(result({"kind": "task-notification"}), [], False)
        assert not fut.done() and len(b._outstanding) == 1
        # no human result ever arrives: ask() must give up, not hang, and unblock background speech
        t = time.monotonic()
        reply = await asyncio.wait_for(fut, 2)
        assert reply == brain_mod.NO_REPLY_MSG and time.monotonic() - t < 1.5
        assert not b._outstanding and b._can_speak_background()
        # and a human-classified result still resolves normally
        fut = asyncio.ensure_future(b.ask("again"))
        await asyncio.sleep(0.05)
        await b._on_result(result({"kind": "human"}), [], False)
        assert await asyncio.wait_for(fut, 1) == "done"

    old_state, old_jarvis = brain_mod.STATE_PATH, ui.jarvis
    ui.jarvis = lambda text, model=None: None  # keep the real log file clean
    try:
        with patched(br__ASK_TIMEOUT_S=0.3):
            asyncio.run(run())
    finally:
        brain_mod.STATE_PATH, ui.jarvis = old_state, old_jarvis


def test_permission_prompt_cuts_off_background_speech():
    async def run():
        b = make_brain(FakeSpeaker())
        b._bg_stop = threading.Event()
        b._interrupt_background()
        assert b._bg_stop.is_set()

    asyncio.run(run())


# ---- watchdog ---------------------------------------------------------------------------

def test_watchdog_dumps_on_stall_not_when_fresh():
    with tempfile.TemporaryDirectory() as d:
        now = [1000.0]
        wd = Watchdog(stall_s=10, min_gap_s=60, dump_dir=Path(d), clock=lambda: now[0])
        wd.beat("loop")
        now[0] += 5
        assert wd.check() is None and not list(Path(d).glob("hang-*"))  # fresh
        now[0] += 6
        wd.beat("loop")  # loop is fine, mic has been silent 11 s
        ERRORS.clear()
        path = wd.check()
        assert path and path.exists() and "Thread" in path.read_text()
        assert any(str(path) in e for e in ERRORS)
        now[0] += 20
        assert wd.check() is None  # rate limited
        now[0] += 60
        wd.beat("loop")
        assert wd.check() is not None  # a minute later, allowed again


def test_watchdog_quiet_while_busy_but_not_for_loop_stall():
    with tempfile.TemporaryDirectory() as d:
        now = [1000.0]
        wd = Watchdog(stall_s=10, min_gap_s=60, dump_dir=Path(d), clock=lambda: now[0])
        with wd.busy("thinking"):
            for _ in range(5):
                now[0] += 8
                wd.beat("loop")
                assert wd.check() is None  # 40 s without mic frames, but Claude is thinking
        assert wd.check() is None  # leaving busy restarts the listening clock
        now[0] += 11
        wd.beat("loop")
        assert wd.check() is not None  # now it counts
        with tempfile.TemporaryDirectory() as d2:
            wd2 = Watchdog(stall_s=10, dump_dir=Path(d2), clock=lambda: now[0])
            with wd2.busy("thinking"):
                now[0] += 11  # event loop heartbeat missing: always a hang
                assert wd2.check() is not None


def test_watchdog_busy_cap_expires():
    with tempfile.TemporaryDirectory() as d:
        now = [1000.0]
        wd = Watchdog(stall_s=10, dump_dir=Path(d), clock=lambda: now[0])
        with wd.busy("thinking", max_s=30):
            now[0] += 20
            wd.beat("loop")
            assert wd.check() is None
            now[0] += 15
            wd.beat("loop")
            assert wd.check() is not None  # busy for longer than its cap = suspicious


def test_watchdog_sleep_resume_skips_dump_and_resets_timers():
    from jarvis import keepawake
    keepawake.reset()
    activity: list[str] = []
    real_activity, ui.activity = ui.activity, activity.append
    try:
        with tempfile.TemporaryDirectory() as d:
            now, wall = [1000.0], [5_000_000.0]
            wd = Watchdog(stall_s=10, dump_dir=Path(d), clock=lambda: now[0], wall=lambda: wall[0])
            now[0] += 1
            wall[0] += 1
            assert wd.check() is None
            now[0] += 24118  # slept for ~6.7 h; both clocks advanced
            wall[0] += 24118
            ERRORS.clear()
            assert wd.check() is None and not list(Path(d).glob("hang-*")) and not ERRORS
            assert len(activity) == 1 and "resumed from sleep" in activity[0] \
                and "skipping hang dump" in activity[0] and "402 minutes" in activity[0]
            assert keepawake.resume_suspected()
            now[0] += 1
            wall[0] += 1
            assert wd.check() is None  # timers were reset: no stall right after waking
            now[0] += 11  # a real stall afterwards is still reported
            wall[0] += 11
            wd.beat("loop")
            assert wd.check() is not None
    finally:
        ui.activity = real_activity
        keepawake.reset()


def test_watchdog_real_hang_without_suspend_still_dumps():
    from jarvis import keepawake
    keepawake.reset()
    with tempfile.TemporaryDirectory() as d:
        now, wall = [1000.0], [5_000_000.0]
        wd = Watchdog(stall_s=10, dump_dir=Path(d), clock=lambda: now[0], wall=lambda: wall[0])
        now[0] += 500  # monotonic jumped but the wall clock did not: not a sleep
        wall[0] += 1
        assert wd.check() is not None
        assert not keepawake.resume_suspected()
    keepawake.reset()


def test_stream_lost_after_resume_reconnects_without_crash_report():
    from jarvis import crashreport, keepawake
    activity: list[str] = []
    real_activity, ui.activity = ui.activity, activity.append
    reports = []
    real_report, crashreport.report = crashreport.report, lambda *a, **k: reports.append(a)
    t = [1000.0]
    real_mono, keepawake.mono_fn = keepawake.mono_fn, lambda: t[0]
    try:
        async def run(expect_report):
            b = make_brain(FakeSpeaker())
            b._recover = lambda *a, **k: asyncio.sleep(0)
            b._on_stream_lost(RuntimeError("Claude Code stream ended"))
            assert bool(reports) == expect_report
            b._closing = True

        keepawake.reset()
        keepawake.mark_resume()
        t[0] += 60  # within 2 minutes of the resume
        asyncio.run(run(False))
        assert any("no crash report" in a for a in activity)
        reports.clear()
        t[0] += 300  # long after the resume: reported as before
        asyncio.run(run(True))
    finally:
        ui.activity, crashreport.report, keepawake.mono_fn = real_activity, real_report, real_mono
        keepawake.reset()


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.monotonic()
    for name, fn in tests:
        t = time.monotonic()
        try:
            fn()
            print(f"PASS {name} ({time.monotonic() - t:.1f}s)")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.monotonic() - t0:.1f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
