r"""Offline tests for dictation (jarvis/dictation.py, Listener.record_dictation, the __main__ glue).

Run:
    set JARVIS_NOTIFY_PHONE=0 && .venv\Scripts\python.exe tests\test_dictation.py

Plain script. No mic, no models, no audio: fake VAD/wake/stream, fake record and transcribe functions.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"
import asyncio
import io
import site
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import torch  # noqa: E402
from rich.console import Console  # noqa: E402

import jarvis.__main__ as main_mod  # noqa: E402
from jarvis import listener as listener_mod  # noqa: E402
from jarvis import speaker as speaker_mod  # noqa: E402
from jarvis import ui  # noqa: E402
from jarvis.dictation import Dictation, dictation_command  # noqa: E402

ui.console = Console(file=io.StringIO())
ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-dictation.log"
PASSED, FAILED = [], []


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.01)
    return False


class FakeWake:
    models = {"fake": None}

    def predict(self, frame):
        return {"fake": 0.0}

    def reset(self):
        pass


class FakeVad:
    def __call__(self, x, sr):
        return torch.tensor(1.0 if float(x.abs().max()) > 0.01 else 0.0)

    def reset_states(self):
        pass


class FakeStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass


def make_listener():
    cfg = {"audio": {"sample_rate": 16000, "device": None}, "wake_word": {"model": "x", "threshold": 0.5},
           "endpointing": {"vad_threshold": 0.5, "end_silence_ms": 240, "no_speech_timeout_s": 1,
                           "max_utterance_s": 5, "followup_window_s": 1}}
    li = listener_mod.Listener(cfg, wake=FakeWake(), vad=FakeVad(), stream_factory=FakeStream)
    li._last_frame = time.monotonic() + 1000
    return li


def loud(v=5000):
    return np.full(listener_mod.FRAME, v, dtype=np.int16)


def quiet():
    return np.zeros(listener_mod.FRAME, dtype=np.int16)


class Harness:
    """A Dictation with a scripted record(): segments are pushed by the test; stop ends it."""

    def __init__(self, texts=None, max_s=60, fail_record=False, fail_stt=False):
        self.done = []
        self.events = []
        self.recording = []
        self.texts = texts or {}
        self.on_seg = None
        self.hit_max = False
        self.started = threading.Event()
        self.fail_record, self.fail_stt = fail_record, fail_stt

        def record(stop, on_segment, max_s_):
            self.on_seg = on_segment
            self.started.set()
            if self.fail_record:
                raise RuntimeError("mic gone")
            stop.wait(10)
            return "max" if self.hit_max else "stopped"

        def transcribe(audio):
            if self.fail_stt:
                raise RuntimeError("stt boom")
            return self.texts.get(int(audio[0]), "")

        self.d = Dictation(record, transcribe, lambda t, r, e: self.done.append((t, r, e)), max_s=max_s,
                           on_recording=self.recording.append, emit=lambda kind, **kw: self.events.append(kw))

    def seg(self, n):
        self.on_seg(np.array([n], dtype=np.float32))


# ---- phrases ---------------------------------------------------------------------------------

def test_phrase_positives():
    for t in ("start dictation", "Start dictation.", "begin dictation", "Jarvis, start dictation please",
              "turn on dictation", "start dictating", "dictation on", "stop dictation", "Stop dictation!",
              "end dictation", "finish dictation", "stop dictating", "dictation off", "please stop dictation"):
        check(dictation_command(t) is not None, t)
    check(dictation_command("start dictation") == "start" and dictation_command("dictation off") == "stop")


def test_phrase_negatives():
    for t in ("", None, "I want to start dictation later", "stop dictation of the report", "start a dictation",
              "dictation", "what is dictation", "please do not stop dictation now", "start the dictation software",
              "mute", "voice off"):
        check(dictation_command(t) is None, repr(t))


def test_cap_values():
    c = listener_mod.dictation_cap_s
    check(c(None) == 1800 and c("x") == 1800 and c(0) == 1800 and c(-5) == 1800 and c(float("nan")) == 1800)
    check(c(60) == 60 and c(1800) == 1800)
    check(c(10 ** 9) == listener_mod.ABSOLUTE_MAX_DICTATION_S and c(float("inf")) == 1800)


# ---- listener: segments, no end-of-speech cut ------------------------------------------------

def run_dictation(li, stop, segs, max_s=30):
    res = []
    th = threading.Thread(target=lambda: res.append(li.record_dictation(stop, segs.append, max_s)), daemon=True)
    th.start()
    time.sleep(0.15)
    return th, res


def test_long_pause_does_not_end_recording_and_segments_accumulate():
    li = make_listener()
    segs, stop = [], threading.Event()
    th, res = run_dictation(li, stop, segs)
    for _ in range(4):
        li._q.put(loud())
    for _ in range(40):  # 3.2 s of silence: a normal turn would have ended after 240 ms
        li._q.put(quiet())
    for _ in range(4):
        li._q.put(loud(7000))
    time.sleep(0.6)
    check(th.is_alive(), "still recording after a long pause")
    check(len(segs) == 1, f"first segment delivered at the pause, got {len(segs)}")
    check(li.wake_muted(), "wake word steps aside while dictating")
    stop.set()
    th.join(3)
    check(not th.is_alive() and res == ["stopped"])
    check(len(segs) == 2, f"partial segment flushed on stop, got {len(segs)}")
    check(float(np.abs(segs[1]).max()) > 0.2, "second segment holds the second burst")
    check(li._subs == [] and not li.wake_muted())


def test_frames_queued_before_stop_are_not_lost():
    li = make_listener()
    segs, stop = [], threading.Event()
    th, res = run_dictation(li, stop, segs)
    for _ in range(6):
        li._q.put(loud())
    time.sleep(0.5)
    stop.set()
    th.join(3)
    total = sum(len(s) for s in segs)
    check(total >= 6 * listener_mod.FRAME, f"all 6 frames kept, got {total}")


def test_max_length_hard_stop():
    li = make_listener()
    segs, stop = [], threading.Event()
    th, res = run_dictation(li, stop, segs, max_s=0.5)
    th.join(4)
    check(not th.is_alive() and res == ["max"], res)


def test_waiting_for_recorder_lock_ends_on_stop():
    li = make_listener()
    li._rec_lock.acquire()
    try:
        stop, segs = threading.Event(), []
        th, res = run_dictation(li, stop, segs)
        check(th.is_alive())
        stop.set()
        th.join(2)
        check(not th.is_alive() and res == ["stopped"])
    finally:
        li._rec_lock.release()
    check(li._recording == 0)


# ---- controller ---------------------------------------------------------------------------------

def test_accumulate_until_off_then_one_message():
    h = Harness({1: "hello there", 2: "this is a long pause", 3: "and the end."})
    check(h.d.reserve() and not h.d.reserve(), "second reserve refused")
    h.d.launch()
    check(h.started.wait(2) and h.recording == [True] and h.d.phase == "recording")
    h.seg(1)
    time.sleep(0.2)
    h.seg(2)
    h.seg(3)
    time.sleep(0.2)
    check(h.done == [], "nothing is sent while still on")
    check(h.d.stop() and h.d.phase == "finishing")
    check(wait_for(lambda: h.done), "sent after off")
    check(h.done == [("hello there this is a long pause and the end.", "stopped", None)], h.done)
    check(wait_for(lambda: h.d.phase == "idle") and h.recording == [True, False])
    check(h.events[-1]["phase"] == "idle" and h.events[-1]["on"] is False)
    check(any(e["phase"] == "recording" and e["on"] for e in h.events))


def test_stop_phrase_segment_ends_and_is_dropped():
    h = Harness({1: "write this down", 2: "Stop dictation.", 3: "ignored afterwards"})
    h.d.reserve(); h.d.launch(); h.started.wait(2)
    h.seg(1); h.seg(2); h.seg(3)
    check(wait_for(lambda: h.done), "phrase ended it")
    check(h.done[0][0] == "write this down" and h.done[0][1] == "stopped", h.done)


def test_max_reason_sends_what_it_has():
    h = Harness({1: "some words"})
    h.hit_max = True
    h.d.reserve(); h.d.launch(); h.started.wait(2)
    h.seg(1)
    time.sleep(0.2)
    h.d.stop()
    check(wait_for(lambda: h.done) and h.done[0][:2] == ("some words", "max"), h.done)


def test_record_failure_reports_error_and_returns_to_idle():
    h = Harness({}, fail_record=True)
    h.d.reserve(); h.d.launch()
    check(wait_for(lambda: h.done) and h.done[0][1] == "error" and isinstance(h.done[0][2], RuntimeError))
    check(wait_for(lambda: h.d.phase == "idle") and h.recording == [True, False])


def test_stt_failure_on_every_segment_is_error_not_silence():
    h = Harness({}, fail_stt=True)
    h.d.reserve(); h.d.launch(); h.started.wait(2)
    h.seg(1)
    time.sleep(0.2)
    h.d.stop()
    check(wait_for(lambda: h.done) and h.done[0][1] == "error", h.done)


def test_stop_before_launch_finishes_empty_and_abort_frees():
    h = Harness()
    h.d.reserve()
    h.d.stop()
    h.d.launch()
    check(h.done == [("", "stopped", None)] and h.d.phase == "idle" and h.recording == [False], h.recording)
    h2 = Harness()
    h2.d.reserve()
    h2.d.abort()
    check(not h2.d.active and h2.d.reserve())


def test_events_carry_max_and_since():
    h = Harness(max_s=123)
    h.d.reserve()
    check(h.events[0]["phase"] == "starting" and h.events[0]["on"] is True and h.events[0]["max_s"] == 123
          and h.events[0]["since"] is not None)
    h.d.abort()


# ---- glue in __main__ -----------------------------------------------------------------------------

def test_restart_safe_false_while_dictating():
    h = Harness()
    old = main_mod._dictation
    main_mod._dictation = h.d
    try:
        brain = SimpleNamespace()
        check(main_mod.restart_safe(brain) is True)
        h.d.reserve()
        check(not main_mod.restart_safe(brain), "false while starting")
        h.d.launch(); h.started.wait(2)
        check(not main_mod.restart_safe(brain), "false while recording")
        h.d.stop()
        check(not main_mod.restart_safe(brain), "false while finishing")
        check(wait_for(lambda: not h.d.active))
        check(main_mod.restart_safe(brain) is True)
        main_mod._pending_add(1)
        check(not main_mod.restart_safe(brain), "false while the text is being handed over")
        main_mod._pending_add(-1)
        check(main_mod.restart_safe(brain) is True)
    finally:
        main_mod._dictation = old


def test_local_command_start_stop_and_not_available():
    async def go():
        sp = SimpleNamespace(muted=True)  # muted: the notes are silent, recording still works
        brain = SimpleNamespace()
        old = main_mod._dictation
        try:
            main_mod._dictation = None
            r = await main_mod.local_command("start dictation", sp, brain)
            check(r == main_mod.DICT_NO_MIC_REPLY, r)
            h = Harness({1: "typed words"})
            main_mod._dictation = h.d
            r = await main_mod.local_command("Start dictation.", sp, brain)
            check(r == main_mod.DICT_START_NOTE and h.d.phase == "recording", (r, h.d.phase))
            check(await main_mod.local_command("start dictation", sp, brain) == main_mod.DICT_ALREADY_REPLY)
            h.started.wait(2)
            h.seg(1)
            time.sleep(0.2)
            r = await main_mod.local_command("stop dictation", sp, brain)
            check(r == "Stopping dictation.")
            check(wait_for(lambda: h.done) and h.done[0][0] == "typed words")
            wait_for(lambda: not h.d.active)
            check(await main_mod.local_command("stop dictation", sp, brain) == main_mod.DICT_NOT_ON_REPLY)
        finally:
            main_mod._dictation = old
    asyncio.run(go())


def test_speaker_silenced_while_dictating_even_permission_prompts():
    ns = SimpleNamespace(dictating=True, muted=False, muted_speaks_permission_prompts=True)
    check(speaker_mod.Speaker.silenced(ns) and speaker_mod.Speaker.silenced(ns, permission=True))
    ns.dictating = False
    check(not speaker_mod.Speaker.silenced(ns) and not speaker_mod.Speaker.silenced(ns, permission=True))
    ns.muted = True
    check(speaker_mod.Speaker.silenced(ns) and not speaker_mod.Speaker.silenced(ns, permission=True))
    check(speaker_mod.Speaker.dictating is False, "class default is off")


# ---- verifier fixes ----------------------------------------------------------------------------------

def test_stop_phrase_at_end_of_segment_ends_and_is_stripped():
    from jarvis.dictation import split_stop_suffix
    check(split_stop_suffix("that's all stop dictation") == ("that's all", True))
    check(split_stop_suffix("That's all, Stop Dictation.") == ("That's all", True))
    check(split_stop_suffix("stop dictation") == ("", True))
    for t in ("stop dictation of the report", "I said stop dictation was fun", "unstop dictation", "stopdictation",
              "nothing here"):
        check(split_stop_suffix(t) == (t, False), t)
    h = Harness({1: "first part", 2: "and that's all, stop dictation!", 3: "ignored afterwards"})
    h.d.reserve(); h.d.launch(); h.started.wait(2)
    h.seg(1); h.seg(2); h.seg(3)
    check(wait_for(lambda: h.done), "phrase at the end of a segment ended it")
    check(h.done[0] == ("first part and that's all", "stopped", None), h.done)
    h2 = Harness({1: "the stop dictation feature is neat"})  # inside a segment: no false positive
    h2.d.reserve(); h2.d.launch(); h2.started.wait(2)
    h2.seg(1)
    time.sleep(0.3)
    check(h2.d.phase == "recording" and h2.done == [], "phrase inside a segment keeps recording")
    h2.d.stop()
    check(wait_for(lambda: h2.done) and h2.done[0][0] == "the stop dictation feature is neat")


def test_utterance_recorders_give_way_to_dictation():
    li = make_listener()
    res = []
    li._rec_lock.acquire()  # some recorder holds the mic
    try:
        th = threading.Thread(target=lambda: res.append(li.record_utterance(1.0, True)), daemon=True)
        th.start()
        time.sleep(0.3)
        check(th.is_alive(), "waiting for the lock")
        stop, segs = threading.Event(), []
        dth, _ = run_dictation(li, stop, segs)  # a dictation asks for the mic
        th.join(2)
        check(not th.is_alive() and res == [None], "the waiting utterance gave up instead of blocking")
        t0 = time.monotonic()
        check(li.record_utterance(1.0) is None and time.monotonic() - t0 < 0.5, "new ones return at once")
        stop.set()
        dth.join(2)
    finally:
        li._rec_lock.release()
    check(li._recording == 0 and li._priority == 0 and li._dictations == 0)
    check(li._rec_lock.acquire(timeout=1), "lock is free and not leaked")
    li._rec_lock.release()


class PermSpeaker:
    def __init__(self):
        self.dictating, self.muted, self.said = False, False, []
        self.muted_speaks_permission_prompts = True

    def speak(self, text, stop=None):
        self.said.append(text)

    def deadline_for(self, text):
        return 5.0


class PermListener:
    def __init__(self):
        self.calls = 0
        self.fn = None

    def record_utterance(self, followup_s=0.0, priority=False):
        self.calls += 1
        return self.fn() if self.fn else np.zeros(10, dtype=np.float32)

    def priority(self):
        from contextlib import nullcontext
        return nullcontext()


def make_perm_brain(answers=("yes",)):
    from jarvis import brain as brain_mod
    brain_mod.STATE_PATH = Path(tempfile.gettempdir()) / "jarvis-test-dict-brain-state.json"
    sp, li = PermSpeaker(), PermListener()
    ans = list(answers)
    b = brain_mod.Brain(str(ROOT), speaker=sp, listener=li, transcribe=lambda a: ans.pop(0) if ans else "")
    return b, sp, li


def test_permission_during_dictation_is_page_only_and_never_blocks():
    async def go():
        b, sp, li = make_perm_brain()
        sp.dictating = True
        task = asyncio.ensure_future(b.ask_permission("WebFetch", {"url": "http://example.com/x"}, None))
        await asyncio.sleep(0.6)
        check(not task.done() and sp.said == [] and li.calls == 0, (sp.said, li.calls))
        check(not b._turn_lock.locked() and not b._perm_lock.locked(), "no lock held while waiting")
        req = b._perm_requests[0]
        check(b.resolve_permission(req.id, "approve") == "ok")
        res = await asyncio.wait_for(task, 3)
        check(type(res).__name__ == "PermissionResultAllow" and sp.said == [] and li.calls == 0, (res, sp.said))
        # dictation over: the same prompt is spoken and listened to by voice
        b2, sp2, li2 = make_perm_brain(["yes"])
        sp2.dictating = True
        t2 = asyncio.ensure_future(b2.ask_permission("WebFetch", {"url": "http://example.com/y"}, None))
        await asyncio.sleep(0.4)
        check(li2.calls == 0)
        sp2.dictating = False
        res2 = await asyncio.wait_for(t2, 5)
        check(type(res2).__name__ == "PermissionResultAllow" and li2.calls == 1, (res2, li2.calls))
        check(any("yes or no" in t.lower() for t in sp2.said), sp2.said)
    asyncio.run(go())


def test_dictation_starting_mid_listen_does_not_deny():
    async def go():
        b, sp, li = make_perm_brain(["yes"])
        first = {"n": 0}

        def fn():
            first["n"] += 1
            if first["n"] == 1:
                sp.dictating = True  # the user starts a dictation while we listen: the recorder gives up
                return None
            return np.zeros(10, dtype=np.float32)

        li.fn = fn
        task = asyncio.ensure_future(b.ask_permission("WebFetch", {"url": "http://example.com/z"}, None))
        await asyncio.sleep(0.6)
        check(not task.done(), "not denied while dictating")
        sp.dictating = False
        res = await asyncio.wait_for(task, 5)
        check(type(res).__name__ == "PermissionResultAllow", res)
        check(sum("yes or no" in t.lower() for t in sp.said) == 2, sp.said)  # asked again after the dictation
    asyncio.run(go())


def test_brain_dictation_busy_hook_covers_notes():
    async def go():
        b, sp, li = make_perm_brain()
        flag = {"v": True}
        b.dictation_busy = lambda: flag["v"]
        check(b._dictating() and b._voice_silenced(False) is False)
        sp.dictating = True
        check(b._voice_silenced(True) is True and await b._listen() == "")
    asyncio.run(go())


def test_run_dictation_start_cancel_and_failures_never_stick():
    old_dict, old_speak = main_mod._dictation, main_mod.speak_plain

    async def go():
        sp = SimpleNamespace(muted=False, dictating=False)
        # 1. cancelled during the start note
        h = Harness()
        main_mod._dictation = h.d

        async def slow_note(speaker, text):
            await asyncio.sleep(30)

        main_mod.speak_plain = slow_note
        check(h.d.reserve())
        t = asyncio.ensure_future(main_mod.run_dictation_start(sp, None))
        await asyncio.sleep(0.2)
        t.cancel()
        try:
            await t
            check(False, "CancelledError must propagate")
        except asyncio.CancelledError:
            pass
        check(h.d.phase == "idle" and sp.dictating is False, "idle again after cancel")
        check(h.d.reserve(), "can reserve again")
        h.d.abort()

        # 2. the note raises
        async def bad_note(speaker, text):
            raise RuntimeError("tts gone")

        main_mod.speak_plain = bad_note
        h = Harness(); main_mod._dictation = h.d
        h.d.reserve()
        await main_mod.run_dictation_start(sp, None)
        check(h.d.phase == "idle" and sp.dictating is False)

        async def ok_note(speaker, text):
            return None

        main_mod.speak_plain = ok_note
        # 3. launch() raises
        h = Harness(); main_mod._dictation = h.d
        h.d.reserve()

        def boom():
            raise RuntimeError("launch failed")

        h.d.launch = boom
        await main_mod.run_dictation_start(sp, None)
        check(h.d.phase == "idle" and sp.dictating is False)
        # 4. cancelled while waiting for plain speech to end
        h = Harness(); main_mod._dictation = h.d
        h.d.reserve()
        main_mod._plain_active += 1
        try:
            t = asyncio.ensure_future(main_mod.run_dictation_start(sp, None))
            await asyncio.sleep(0.2)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        finally:
            main_mod._plain_active -= 1
        check(h.d.phase == "idle")

    try:
        asyncio.run(go())
    finally:
        main_mod._dictation, main_mod.speak_plain = old_dict, old_speak


def test_launch_failures_inside_dictation_leave_idle_and_mic_flag_off():
    for which in ("trans", "rec"):
        rec_flags = []
        done = []
        d = Dictation(lambda stop, on_seg, m: (stop.wait(5), "stopped")[1], lambda a: "x",
                      lambda t, r, e: done.append((t, r, e)), max_s=10, on_recording=rec_flags.append,
                      emit=lambda *a, **k: None)
        d.reserve()
        real = threading.Thread
        bad_name = "dictation-stt" if which == "trans" else "dictation-rec"

        class Boom(real):
            def start(self):
                if self.name == bad_name:
                    raise RuntimeError("can't start thread")
                super().start()

        threading.Thread = Boom
        try:
            d.launch()  # must not raise
        finally:
            threading.Thread = real
        check(wait_for(lambda: d.phase == "idle" and done), (which, d.phase, done))
        check(done[0][1] == "error" and rec_flags[-1] is False, (which, done, rec_flags))
        check(d.reserve(), "can start again")
        d.abort()


def test_on_dictation_hook_returns_reservation_on_every_failure():
    from jarvis import web as web_mod
    old_dict, old_tok = main_mod._dictation, web_mod.load_token
    web_mod.load_token = lambda: "tok-test-0123456789"
    cfg = {"web_ui": True, "web_port": 0, "web_bind": "local"}

    async def go():
        h = Harness()
        main_mod._dictation = h.d
        sp = SimpleNamespace(muted=False, dictating=False)

        class DeadLoop:
            def call_soon_threadsafe(self, fn):
                raise RuntimeError("loop closed")

        srv = main_mod.start_web_ui(cfg, loop=DeadLoop(), brain=SimpleNamespace(), speaker=sp)
        check(srv is not None and srv.on_dictation is not None, "hook built")
        try:
            srv.on_dictation(True)
            check(False, "should raise")
        except RuntimeError:
            pass
        check(h.d.phase == "idle", "reservation returned when scheduling fails")

        class InlineLoop:
            def call_soon_threadsafe(self, fn):
                fn()

        orig = main_mod.run_dictation_start

        def broken(*a, **k):
            raise RuntimeError("no coroutine")

        main_mod.run_dictation_start = broken
        try:
            srv2 = main_mod.start_web_ui(cfg, loop=InlineLoop(), brain=SimpleNamespace(), speaker=sp)
            try:
                srv2.on_dictation(True)
            except RuntimeError:
                pass
            check(h.d.phase == "idle", "reservation returned when go() fails")
        finally:
            main_mod.run_dictation_start = orig

    try:
        asyncio.run(go())
    finally:
        main_mod._dictation, web_mod.load_token = old_dict, old_tok


def test_restart_cannot_beat_dictated_text():
    from jarvis import restart as restart_mod
    old_dict, old_pending = main_mod._dictation, main_mod._dictation_pending
    old_rp = restart_mod.restart_pending
    main_mod._dictation = None
    main_mod._dictation_pending = 0
    restart_mod.restart_pending = lambda: True

    async def go():
        idle, wake_stop = {"dictated": None}, threading.Event()
        brain = SimpleNamespace()
        main_mod._pending_add(1)  # what on_dictation_done does before finish_dictation runs
        main_mod.hand_over_dictated(idle, "hello there", wake_stop)  # finish_dictation, idle branch
        check(wake_stop.is_set() and not main_mod.restart_safe(brain), "restart not safe: text not taken yet")

        class L:
            def wait_for_wake(self, stop):
                stop.wait(5)
                return False

        wake_stop.clear()

        async def later():
            await asyncio.sleep(0.15)
            wake_stop.set()
        asyncio.ensure_future(later())
        heard, restart_now = await main_mod.wait_for_wake_or_restart(L(), wake_stop, brain, poll_s=0.02)
        check(not heard and not restart_now, "poller must not trigger a restart while the text waits")
        check(main_mod.take_dictated(idle) == "hello there")
        check(main_mod.restart_safe(brain) and main_mod._dictation_pending == 0, "safe once handed over")
        check(main_mod.take_dictated(idle) is None and main_mod._dictation_pending == 0)

    try:
        asyncio.run(go())
    finally:
        main_mod._dictation, main_mod._dictation_pending = old_dict, old_pending
        restart_mod.restart_pending = old_rp


def test_dictation_busy_covers_pending_notes():
    old_dict, old_pending = main_mod._dictation, main_mod._dictation_pending
    try:
        main_mod._dictation, main_mod._dictation_pending = None, 0
        check(main_mod.dictation_busy() is False)
        main_mod._pending_add(1)
        check(main_mod.dictation_busy() is True)
    finally:
        main_mod._dictation, main_mod._dictation_pending = old_dict, old_pending


def test_page_start_and_stop_speak_nothing_voice_start_speaks_short_ack():
    old_dict, old_speak = main_mod._dictation, main_mod.speak_plain
    spoken = []

    async def note(speaker, text):
        spoken.append(text)

    async def go():
        sp = SimpleNamespace(muted=False, dictating=False)
        main_mod.speak_plain = note
        # page start: nothing spoken, recording begins at once
        h = Harness(); main_mod._dictation = h.d
        check(h.d.reserve())
        await main_mod.run_dictation_start(sp, None, announce=False)
        check(spoken == [] and h.d.phase == "recording", (spoken, h.d.phase))
        h.d.stop("page")
        check(h.d.stop_why == "page")
        check(wait_for(lambda: not h.d.active))
        # the stop-from-page decision: silent even when empty; voice-empty and max are spoken; normal stop silent
        check(main_mod.dictation_note_spoken("stopped", "", True) is False)
        check(main_mod.dictation_note_spoken("stopped", "hi", True) is False)
        check(main_mod.dictation_note_spoken("stopped", "hi", False) is False)
        check(main_mod.dictation_note_spoken("stopped", "", False) is True)
        check(main_mod.dictation_note_spoken("max", "hi", True) is True)
        # voice start: one short ack, spoken BEFORE recording begins
        h = Harness(); main_mod._dictation = h.d
        order = []

        async def note2(speaker, text):
            order.append(("speak", h.d.phase, text))

        main_mod.speak_plain = note2
        r = await main_mod.local_command("start dictation", sp, SimpleNamespace())
        check(order == [("speak", "starting", "Dictating.")], order)
        check(r == "Dictating." and h.d.phase == "recording", (r, h.d.phase))
        h.d.stop("command")
        check(h.d.stop_why == "command")
        wait_for(lambda: not h.d.active)
        # the hook passes announce=False for the page
        seen = {}
        orig = main_mod.run_dictation_start

        async def fake(speaker, brain=None, cut_speech=False, announce=True):
            seen["announce"], seen["cut"] = announce, cut_speech
        main_mod.run_dictation_start = fake
        from jarvis import web as web_mod
        old_tok = web_mod.load_token
        web_mod.load_token = lambda: "tok-test-0123456789"
        try:
            h = Harness(); main_mod._dictation = h.d

            class InlineLoop:
                def call_soon_threadsafe(self, fn):
                    fn()
            srv = main_mod.start_web_ui({"web_ui": True, "web_port": 0, "web_bind": "local"}, loop=InlineLoop(),
                                        brain=SimpleNamespace(), speaker=sp)
            srv.on_dictation(True)
            await asyncio.sleep(0.1)
            check(seen == {"announce": False, "cut": True}, seen)
            h.d.abort()
        finally:
            main_mod.run_dictation_start = orig
            web_mod.load_token = old_tok

    try:
        asyncio.run(go())
    finally:
        main_mod._dictation, main_mod.speak_plain = old_dict, old_speak


def test_config_key():
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    check(cfg.get("dictation_max_s") == 1800)


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
