r"""Offline, silent tests for the always-on wake word, the frame pump and the cancel tool.

Run (base interpreter; jarvis.cmd only runs the package):
    .venv\Scripts\python.exe tests\test_wake.py

Plain script, no pytest needed. Prints PASS/FAIL per test, exits non-zero on failure.
Nothing opens a mic or audio device; the wake model, VAD, Claude client and speaker are fakes.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
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
from claude_agent_sdk import ResultMessage  # noqa: E402

import jarvis.__main__ as main_mod  # noqa: E402
from jarvis import brain as brain_mod  # noqa: E402
from jarvis import cancel as cancel_mod  # noqa: E402
from jarvis import listener as listener_mod  # noqa: E402
from jarvis import ui  # noqa: E402

ERRORS: list[str] = []
ui.error = lambda text: ERRORS.append(text)  # keep the real log file clean
ui.activity = lambda text: None
ui.status = lambda text: None
ui.jarvis = lambda text, model=None: None
ui.you = lambda text: None
brain_mod.STATE_PATH = Path(tempfile.gettempdir()) / "jarvis-test-wake-state.json"


# ---- fakes ------------------------------------------------------------------------------

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
    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def make_listener():
    cfg = {"audio": {"sample_rate": 16000, "device": None},
           "wake_word": {"model": "x", "threshold": 0.5},
           "endpointing": {"vad_threshold": 0.5, "end_silence_ms": 240, "no_speech_timeout_s": 1,
                           "max_utterance_s": 5, "followup_window_s": 1}}
    li = listener_mod.Listener(cfg, wake=FakeWake(), vad=FakeVad(), stream_factory=FakeStream)
    li._last_frame = time.monotonic() + 1000  # pretend the mic is healthy
    return li


def loud(v=5000):
    return np.full(listener_mod.FRAME, v, dtype=np.int16)


def quiet():
    return np.zeros(listener_mod.FRAME, dtype=np.int16)


def start_watch(li):
    stop, out = threading.Event(), []
    th = threading.Thread(target=lambda: out.append(li.watch_for_wake(stop)), daemon=True)
    th.start()
    time.sleep(0.15)  # subscribed and past its initial model reset
    return stop, out, th


# ---- listener: pump ---------------------------------------------------------------------

def test_pump_fans_each_frame_out_once_to_every_subscriber():
    li = make_listener()
    beats = []
    li.on_frame = lambda: beats.append(1)
    li._ensure_pump()
    for i in range(3):  # nobody is subscribed: frames are dropped, not queued up
        li._q.put(loud(100 + i))
    time.sleep(0.3)
    with li._subscribe() as a, li._subscribe() as b:
        for i in range(5):
            li._q.put(loud(200 + i))
        time.sleep(0.4)
        got_a = [int(a.get_nowait()[0]) for _ in range(a.qsize())]
        got_b = [int(b.get_nowait()[0]) for _ in range(b.qsize())]
    assert got_a == got_b == [200, 201, 202, 203, 204], (got_a, got_b)
    assert len(beats) == 8  # one heartbeat per frame, regardless of subscribers
    assert li._subs == []  # unsubscribed on exit


def test_wake_watch_is_cancellable_and_leaves_no_subscriber():
    li = make_listener()
    stop, out, th = start_watch(li)
    assert len(li._subs) == 1
    t = time.monotonic()
    stop.set()
    th.join(2)
    assert not th.is_alive() and out == [False] and time.monotonic() - t < 1.0
    assert li._subs == []


def test_wake_fires_while_a_recorder_is_not_active():
    li = make_listener()
    stop, out, th = start_watch(li)
    li._q.put(loud())
    th.join(2)
    assert out == [True]


def test_wake_is_muted_during_permission_prompt_and_resumes_after():
    li = make_listener()
    stop, out, th = start_watch(li)
    with li.priority():
        for _ in range(4):
            li._q.put(loud())
        time.sleep(0.4)
        assert th.is_alive() and not out  # neither fired nor ... consumed by the wake model
    time.sleep(0.1)
    li._q.put(loud())  # first frame after the mute is only used to flush the model
    time.sleep(0.2)
    li._q.put(loud())
    th.join(2)
    assert out == [True]


# ---- listener: echo guard ---------------------------------------------------------------

def test_wake_is_not_muted_during_narration_or_after_it():
    # Self-triggering on Jarvis's own voice is accepted: only recorders and permission prompts mute.
    li = make_listener()
    assert not hasattr(li, "speaking") and not hasattr(listener_mod, "ECHO_TAIL_S")
    assert not li.wake_muted()
    stop, out, th = start_watch(li)
    li._q.put(loud())
    th.join(2)
    assert out == [True]
    with li.priority():
        assert li.wake_muted()
    assert not li.wake_muted()


# ---- concurrency in the main loop -------------------------------------------------------

class FakeListener:
    """watch_for_wake blocks until fire() or done; counts live watcher threads."""

    def __init__(self):
        self._fire = threading.Event()
        self.live = 0
        self.watches = 0

    def fire(self):
        self._fire.set()

    def watch_for_wake(self, done):
        self.live += 1
        self.watches += 1
        try:
            while not done.is_set():
                if self._fire.wait(0.02):
                    self._fire.clear()
                    return True
            return False
        finally:
            self.live -= 1


class FakeBrain:
    def __init__(self):
        self.hold_background = False
        self.held_at_stop = []  # hold_background as seen by each stop_speech()
        self.stops = 0
        self.flushes = 0
        self.flush_block = None  # optional Event the flush waits on (until stop_speech)
        self.flush_result = None  # what flush_background reports (True = spoke and finished)

    def stop_speech(self):
        self.stops += 1
        self.held_at_stop.append(self.hold_background)
        if self.flush_block:
            self.flush_block.set()

    async def flush_background(self):
        self.flushes += 1
        if self.flush_block:
            await asyncio.to_thread(self.flush_block.wait, 2)
        return self.flush_result


class patched:
    def __init__(self, mod, **kw):
        self.mod, self.kw = mod, kw

    def __enter__(self):
        self.old = {k: getattr(self.mod, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(self.mod, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


def test_wake_while_asking_takes_a_new_turn_without_deadlock_or_misorder():
    spoken = []

    async def fake_speak(speaker, listener, text):
        spoken.append(text)
        return False

    async def run():
        li, brain = FakeListener(), FakeBrain()
        loop = asyncio.get_running_loop()
        first = loop.create_future()  # the long-running first ask, still working
        second = loop.create_future()
        submitted = []

        async def take_turn(kind):
            assert kind == "command"
            return "stop doing that", "command"

        def submit(text):
            submitted.append(text)
            return second

        asks = [first]
        conv = asyncio.ensure_future(main_mod.converse(
            asks, brain=brain, speaker=None, listener=li, take_turn=take_turn, submit=submit))
        await asyncio.sleep(0.2)
        assert not conv.done()
        li.fire()  # "hey Jarvis" while ask() is still waiting
        await asyncio.sleep(0.3)
        assert brain.stops == 1 and submitted == ["stop doing that"] and len(asks) == 2
        assert not first.done()  # background work was not cancelled for the user
        first.set_result(("old work finished", "m"))
        second.set_result(("sure, anything else?", "m"))
        assert await asyncio.wait_for(conv, 3) == "followup"
        assert spoken == ["old work finished", "sure, anything else?"]
        await asyncio.sleep(0.2)
        assert li.live == 0  # no orphan watcher thread keeps consuming frames

    with patched(main_mod, speak_with_barge_in=fake_speak):
        asyncio.run(run())


def test_wake_during_reply_speech_is_taken_as_a_new_turn():
    calls = []

    async def fake_speak(speaker, listener, text):
        calls.append(text)
        return len(calls) == 1  # barge-in during the first reply

    async def run():
        li, brain = FakeListener(), FakeBrain()
        loop = asyncio.get_running_loop()
        done_first = loop.create_future()
        done_first.set_result(("long answer", "m"))
        second = loop.create_future()
        second.set_result(("short answer", "m"))

        async def take_turn(kind):
            return "actually, do this", "command"

        asks = [done_first]
        mode = await asyncio.wait_for(main_mod.converse(
            asks, brain=brain, speaker=None, listener=li, take_turn=take_turn,
            submit=lambda t: second), 3)
        assert mode == "followup" and calls == ["long answer", "short answer"]
        assert brain.stops == 1

    with patched(main_mod, speak_with_barge_in=fake_speak):
        asyncio.run(run())


def test_wake_during_background_flush_stops_it_and_takes_the_turn():
    async def fake_speak(speaker, listener, text):
        return False

    async def run():
        li, brain = FakeListener(), FakeBrain()
        brain.flush_block = threading.Event()
        loop = asyncio.get_running_loop()
        reply = loop.create_future()

        async def take_turn(kind):
            return "hello", "command"

        def submit(t):
            reply.set_result(("hi", "m"))
            return reply

        conv = asyncio.ensure_future(main_mod.converse(
            [], brain=brain, speaker=None, listener=li, take_turn=take_turn, submit=submit))
        await asyncio.sleep(0.2)
        assert brain.flushes == 1 and not conv.done()  # flush is "speaking"
        li.fire()
        assert await asyncio.wait_for(conv, 3) == "followup"
        assert brain.stops == 1
        assert brain.held_at_stop == [True]  # held before speech was stopped: no next reply can start

    with patched(main_mod, speak_with_barge_in=fake_speak):
        asyncio.run(run())


def test_wake_between_queued_background_items_does_not_speak_the_next_one():
    class Sp:
        spoken = []

        def deadline_for(self, text):
            return 5.0

        def speak(self, text, stop):
            Sp.spoken.append(text)
            stop.wait(0.3)

    async def run():
        li = FakeListener()
        b = brain_mod.Brain(str(ROOT), speaker=Sp())
        b._background_queue[:] = ["first.", "second.", "third."]
        task = asyncio.ensure_future(main_mod.flush_with_wake(b, li))
        await asyncio.sleep(0.1)
        li.fire()
        assert await asyncio.wait_for(task, 3) is True
        assert b.hold_background and not b._can_speak_background()
        await b.flush_background()  # even a flush that is already scheduled stays quiet
        assert Sp.spoken == ["first."], Sp.spoken
        assert b._background_queue[0] == "first."  # the interrupted one is kept for later
        b.hold_background = False  # the caller releases it after the user's turn
        await b.flush_background()
        assert Sp.spoken[1:] == ["first.", "second.", "third."]

    asyncio.run(run())


def test_nothing_heard_after_wake_returns_to_wake_mode():
    async def run():
        li, brain = FakeListener(), FakeBrain()
        loop = asyncio.get_running_loop()
        first = loop.create_future()

        async def take_turn(kind):
            return None, "wake"

        conv = asyncio.ensure_future(main_mod.converse(
            [first], brain=brain, speaker=None, listener=li, take_turn=take_turn, submit=None))
        await asyncio.sleep(0.1)
        li.fire()
        await asyncio.sleep(0.2)
        assert not conv.done()  # the first ask is still outstanding, so keep waiting for it
        first.set_result(("", "m"))  # interrupted/empty: not spoken
        assert await asyncio.wait_for(conv, 3) == "followup"

    async def boom(*a):
        raise AssertionError("empty reply must not be spoken")

    with patched(main_mod, speak_with_barge_in=boom):
        asyncio.run(run())


class QuickSpeaker:
    def __init__(self):
        self.spoken = []

    def deadline_for(self, text):
        return 5.0

    def speak(self, text, stop):
        self.spoken.append(text)
        stop.wait(0.05)


def test_followup_entered_after_background_flush_speech_finishes():
    async def run():
        li, b = FakeListener(), FakeBrain()
        b.flush_result = True
        assert await asyncio.wait_for(main_mod.flush_mode(b, li), 3) == "followup"
        assert b.hold_background is False and li.live == 0

        # self-started flush (idle, wake mode): the main loop is told once, after playback
        events = []
        real = brain_mod.Brain(str(ROOT), speaker=QuickSpeaker())
        real.on_background_spoken = lambda: events.append(len(real.speaker.spoken))
        real._background_queue[:] = ["Task finished."]
        real._schedule_flush()
        await asyncio.wait_for(real._flush_task, 3)
        assert events == [1] and real._flush_task is None

    asyncio.run(run())


def test_no_followup_after_interrupted_background_speech():
    async def run():
        li, b = FakeListener(), FakeBrain()
        b.flush_block = threading.Event()
        b.flush_result = True  # even if the flush reports success, a wake wins
        task = asyncio.ensure_future(main_mod.flush_mode(b, li))
        await asyncio.sleep(0.2)
        li.fire()
        assert await asyncio.wait_for(task, 3) == "command"  # the wake already starts a turn

        # cut off by a permission prompt / new ask (_bg_stop set): no follow-up, item stays queued
        events = []

        class Sp(QuickSpeaker):
            def speak(self, text, stop):
                stop.wait(0.3)

        real = brain_mod.Brain(str(ROOT), speaker=Sp())
        real.on_background_spoken = lambda: events.append(1)
        real._background_queue[:] = ["Task finished."]
        real._schedule_flush()
        await asyncio.sleep(0.1)
        real.interrupt_background()
        await asyncio.wait_for(real._flush_task, 3) if real._flush_task else None
        await asyncio.sleep(0.05)
        assert events == [] and real._background_queue == ["Task finished."]

    asyncio.run(run())


def test_no_followup_after_empty_or_skipped_background_speech():
    async def run():
        li, b = FakeListener(), FakeBrain()
        assert await asyncio.wait_for(main_mod.flush_mode(b, li), 3) == "wake"  # nothing spoken

        events = []
        real = brain_mod.Brain(str(ROOT), speaker=QuickSpeaker())
        real.on_background_spoken = lambda: events.append(1)
        await real._flush_and_notify()  # empty queue
        real._background_queue[:] = [""]
        await real._flush_and_notify()  # blank reply
        real.hold_background = True
        real._background_queue[:] = ["Held back."]
        await real._flush_and_notify()  # user busy: skipped
        assert events == [] and real.speaker.spoken == []

    asyncio.run(run())


def test_followup_that_hears_nothing_returns_to_wake_quietly_without_reflush():
    async def run():
        li, b = FakeListener(), FakeBrain()
        b.flush_result = True
        assert await main_mod.flush_mode(b, li) == "followup"
        b.flush_result = False  # queue is drained; the timed-out follow-up goes back to wake
        assert await main_mod.flush_mode(b, li) == "wake"
        assert b.flushes == 2 and b.stops == 0  # nothing re-spoken, nothing stopped

    asyncio.run(run())


def test_race_wake_stops_its_watcher_thread():
    async def run():
        li = FakeListener()
        work = asyncio.ensure_future(asyncio.sleep(0.1, result="done"))
        assert await main_mod.race_wake(li, work) is False
        assert li.live == 0

    asyncio.run(run())


# ---- brain: FIFO attribution, empty/interrupted results ---------------------------------

def result(text, origin=None, reason=None):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s", result=text, origin=origin, terminal_reason=reason)


class AliveTask:
    def done(self):
        return False


class SinkClient:
    def __init__(self):
        self.sent = []

    async def query(self, prompt):
        async for m in prompt:
            self.sent.append(m)


def make_brain():
    b = brain_mod.Brain(str(ROOT), speaker=None)
    b._reader_task, b.client = AliveTask(), SinkClient()
    return b


def test_two_asks_resolve_fifo_with_human_origin():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("first"))
        await asyncio.sleep(0.05)
        c = asyncio.ensure_future(b.ask("second"))  # sent while the first turn is still running
        await asyncio.sleep(0.05)
        assert [m["message"]["content"] for m in b.client.sent] == ["first", "second"]
        assert all(m["origin"] == {"kind": "human"} for m in b.client.sent)
        await b._on_result(result("one", {"kind": "human"}), [], True)
        await b._on_result(result("two", {"kind": "human"}), [], True)
        assert await a == "one" and await c == "two"

    asyncio.run(run())


def test_merged_turn_releases_the_second_waiter_instead_of_hanging():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("first"))
        await asyncio.sleep(0.05)
        c = asyncio.ensure_future(b.ask("second"))
        await asyncio.sleep(0.05)
        await b._on_result(result("answers both", {"kind": "human"}), [], True)  # CLI folded them
        assert await a == "answers both" and not c.done()
        t = time.monotonic()
        assert await asyncio.wait_for(c, 2) == ""  # released empty (nothing to speak), not 600 s
        assert time.monotonic() - t < 1.5 and not b._outstanding

    with patched(brain_mod, MERGE_GRACE_S=0.3):
        asyncio.run(run())


def test_second_turn_starting_in_time_keeps_its_own_waiter():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("first"))
        await asyncio.sleep(0.05)
        c = asyncio.ensure_future(b.ask("second"))
        await asyncio.sleep(0.05)
        await b._on_result(result("one", {"kind": "human"}), [], True)
        b._turn_seq += 1  # the reader saw the first message of the second turn
        b._turn_started = time.monotonic()
        b._turn_active = True
        await asyncio.sleep(0.5)
        assert not c.done() and len(b._outstanding) == 1
        await b._on_result(result("two", {"kind": "human"}), [], True)
        assert await a == "one" and await c == "two"

    with patched(brain_mod, MERGE_GRACE_S=0.2):
        asyncio.run(run())


def test_interrupted_or_empty_result_resolves_waiter_with_nothing_to_speak():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("do a long thing"))
        await asyncio.sleep(0.05)
        # an interrupted turn: partial narration text exists, but it must not become the reply
        await b._on_result(result("", {"kind": "human"}, "aborted_streaming"), ["I was in the middle of"], True)
        assert await asyncio.wait_for(a, 1) == ""
        assert not b._outstanding and not b._background_queue
        a = asyncio.ensure_future(b.ask("again"))
        await asyncio.sleep(0.05)
        await b._on_result(result("partial", {"kind": "human"}, "aborted_tools"), [], True)
        assert await asyncio.wait_for(a, 1) == ""
        a = asyncio.ensure_future(b.ask("and again"))
        await asyncio.sleep(0.05)
        await b._on_result(result("   ", {"kind": "human"}), [], True)
        assert await asyncio.wait_for(a, 1) == ""

    asyncio.run(run())


def test_empty_reply_from_ask_is_never_spoken_by_the_main_loop():
    async def run():
        li, brain = FakeListener(), FakeBrain()
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(("", "m"))
        assert await asyncio.wait_for(main_mod.converse(
            [fut], brain=brain, speaker=None, listener=li, take_turn=None, submit=None), 3) == "followup"

    async def boom(*a):
        raise AssertionError("spoken")

    with patched(main_mod, speak_with_barge_in=boom):
        asyncio.run(run())


def test_narration_is_silent_while_user_is_recording_and_stoppable():
    class Sp:
        spoken = []

        def deadline_for(self, text):
            return 5.0

        def speak(self, text, stop):
            Sp.spoken.append(text)
            return not stop.wait(0.5)

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=Sp())
        b.mute_narration = True
        await b._say_narration("on it")
        assert Sp.spoken == []
        b.mute_narration = False
        t = asyncio.ensure_future(b._say_narration("working on it"))
        await asyncio.sleep(0.15)
        b.stop_speech()  # "hey Jarvis"
        t0 = time.monotonic()
        await asyncio.wait_for(t, 2)
        assert Sp.spoken == ["working on it"]
        assert time.monotonic() - t0 < 0.4 and not b._narration_stops

    asyncio.run(run())


def test_wake_during_narration_stops_the_narration():
    class Sp:
        spoken = []

        def deadline_for(self, text):
            return 5.0

        def speak(self, text, stop):
            Sp.spoken.append(text)
            stop.wait(3)

    async def fake_speak(*a):
        return False

    async def run():
        li = FakeListener()
        b = brain_mod.Brain(str(ROOT), speaker=Sp())
        loop = asyncio.get_running_loop()
        reply = loop.create_future()
        narr = asyncio.ensure_future(b._say_narration("working on it"))
        await asyncio.sleep(0.15)
        assert b._narration_stops  # narration is being spoken

        async def take_turn(kind):
            return "stop that", "command"

        def submit(t):
            reply.set_result(("ok", "m"))
            return reply

        asks = [loop.create_future()]  # the running turn: still working
        conv = asyncio.ensure_future(main_mod.converse(
            asks, brain=b, speaker=None, listener=li, take_turn=take_turn, submit=submit))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        li.fire()  # "hey Jarvis" in the middle of narration
        await asyncio.wait_for(narr, 2)
        assert time.monotonic() - t0 < 1.0 and not b._narration_stops
        asks[0].set_result(("", "m"))
        assert await asyncio.wait_for(conv, 3) == "followup"

    with patched(main_mod, speak_with_barge_in=fake_speak):
        asyncio.run(run())


def test_cancel_turn_gets_a_spoken_confirmation_naming_what_was_interrupted():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("do the thing"))
        await asyncio.sleep(0.05)
        b._last_activity = "running: pytest"
        b._on_interrupt()  # what the cancel tool calls after interrupt() succeeded
        await b._on_result(result("", {"kind": "human"}, "aborted_streaming"), ["partial"], True)
        said = await asyncio.wait_for(a, 1)
        assert said.startswith("Stopped.") and "half finished" in said and "running: pytest" in said
        assert b._cancel_note == ""
        # the note is one-shot: the next empty result is silent again
        a = asyncio.ensure_future(b.ask("again"))
        await asyncio.sleep(0.05)
        await b._on_result(result("", {"kind": "human"}, "aborted_streaming"), [], True)
        assert await asyncio.wait_for(a, 1) == ""
        # a real reply is never replaced by the note
        b._on_interrupt()
        a = asyncio.ensure_future(b.ask("third"))
        await asyncio.sleep(0.05)
        await b._on_result(result("Done anyway.", {"kind": "human"}), [], True)
        assert await asyncio.wait_for(a, 1) == "Done anyway."

    asyncio.run(run())


def test_permission_prompt_and_recording_cannot_overlap():
    class Li:
        def priority(self):
            from contextlib import nullcontext
            return nullcontext()

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=object(), listener=Li(), transcribe=lambda a: "")
        events = []

        async def fake(*a):
            events.append("prompt")
            return "allowed"

        b._ask_permission_voice = fake
        async with b.user_turn():
            p = asyncio.ensure_future(b.ask_permission("Write", {"file_path": "x"}, None))
            await asyncio.sleep(0.2)
            assert events == [] and not p.done()  # waits while the user is recording
            events.append("recording ended")
        assert await asyncio.wait_for(p, 1) == "allowed"
        assert events == ["recording ended", "prompt"]
        # and the other way round: recording waits for a prompt in progress
        gate = asyncio.Event()

        async def slow(*a):
            events.append("prompt2")
            await gate.wait()
            return "allowed"

        b._ask_permission_voice = slow
        p = asyncio.ensure_future(b.ask_permission("Write", {"file_path": "x"}, None))
        await asyncio.sleep(0.1)

        async def record():
            async with b.user_turn():
                events.append("recording")

        r = asyncio.ensure_future(record())
        await asyncio.sleep(0.2)
        assert not r.done()
        gate.set()
        await asyncio.wait_for(asyncio.gather(p, r), 1)
        assert events[-2:] == ["prompt2", "recording"]
        assert not b._turn_lock.locked()

    with patched(brain_mod, auto_approve=lambda name, inp: None):
        asyncio.run(run())


def test_permission_prompt_denies_if_the_recording_never_ends():
    class Li:
        pass

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=object(), listener=Li(), transcribe=lambda a: "")

        async def boom(*a):
            raise AssertionError("must not speak over the recording")

        b._ask_permission_voice = boom
        async with b.user_turn():
            res = await asyncio.wait_for(b.ask_permission("Write", {"file_path": "x"}, None), 2)
        assert type(res).__name__ == "PermissionResultDeny"
        assert not b._turn_lock.locked()

    with patched(brain_mod, auto_approve=lambda name, inp: None, PERM_WAIT_S=0.2):
        asyncio.run(run())


def test_merge_release_keeps_waiting_while_a_turn_is_active_then_releases():
    async def run():
        b = make_brain()
        a = asyncio.ensure_future(b.ask("first"))
        await asyncio.sleep(0.05)
        c = asyncio.ensure_future(b.ask("second"))
        await asyncio.sleep(0.05)
        await b._on_result(result("answers both", {"kind": "human"}), [], True)
        b._turn_seq += 1  # a background turn started and runs past the grace period
        b._turn_active, b._turn_started = True, time.monotonic()
        await asyncio.sleep(0.5)
        assert not c.done()
        b._turn_active = False  # it ended without answering the second waiter
        assert await asyncio.wait_for(c, 1.5) == ""
        assert await a == "answers both"

    with patched(brain_mod, MERGE_GRACE_S=0.2):
        asyncio.run(run())


def test_permission_prompt_marks_listener_priority():
    class Li:
        depth = []

        def priority(self):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                Li.depth.append("in")
                yield
                Li.depth.append("out")

            return cm()

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=object(), listener=Li(), transcribe=lambda a: "")

        async def fake(*a):
            assert Li.depth == ["in"]
            return "allowed"

        b._ask_permission_voice = fake
        assert await b.ask_permission("Write", {"file_path": "x"}, None) == "allowed"
        assert Li.depth == ["in", "out"]

    with patched(brain_mod, auto_approve=lambda name, inp: None):
        asyncio.run(run())


# ---- cancel tool ------------------------------------------------------------------------

class FakeClient:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, []

    async def stop_task(self, task_id):
        self.calls.append(("stop_task", task_id))
        if self.fail:
            raise self.fail

    async def interrupt(self):
        self.calls.append(("interrupt",))
        if self.fail:
            raise self.fail


def text_of(res):
    return res["content"][0]["text"]


def test_cancel_with_task_id_uses_stop_task():
    c = FakeClient("a")
    res = asyncio.run(cancel_mod.cancel_task_impl({"task_id": "task-42"}, lambda: c))
    assert c.calls == [("stop_task", "task-42")] and not res.get("is_error")
    assert "task-42" in text_of(res) and "half-finished" in text_of(res)


def test_cancel_calls_on_interrupt_only_for_a_successful_turn_interrupt():
    seen = []
    c = FakeClient("a")
    asyncio.run(cancel_mod.cancel_task_impl({"task_id": "t"}, lambda: c, lambda: seen.append("x")))
    assert seen == []
    asyncio.run(cancel_mod.cancel_task_impl({}, lambda: c, lambda: seen.append("x")))
    assert seen == ["x"]
    bad = FakeClient("a", fail=RuntimeError("no"))
    asyncio.run(cancel_mod.cancel_task_impl({}, lambda: bad, lambda: seen.append("y")))
    assert seen == ["x"]


def test_cancel_without_task_id_interrupts_the_turn():
    c = FakeClient("a")
    for args in ({}, {"task_id": ""}, {"task_id": None}):
        c.calls.clear()
        res = asyncio.run(cancel_mod.cancel_task_impl(args, lambda: c))
        assert c.calls == [("interrupt",)] and "half-finished" in text_of(res) and "turn" in text_of(res)


def test_cancel_reads_the_client_at_call_time():
    cur = [FakeClient("old")]
    get = lambda: cur[0]  # noqa: E731
    asyncio.run(cancel_mod.cancel_task_impl({}, get))
    cur[0] = FakeClient("new")  # recovery replaced brain.client
    asyncio.run(cancel_mod.cancel_task_impl({}, get))
    assert cur[0].calls == [("interrupt",)]


def test_cancel_failure_and_timeout_are_reported_not_raised():
    res = asyncio.run(cancel_mod.cancel_task_impl({}, lambda: FakeClient("a", fail=RuntimeError("not connected"))))
    assert res["is_error"] and "not connected" in text_of(res)

    class Hang(FakeClient):
        async def interrupt(self):
            await asyncio.sleep(30)

    with patched(cancel_mod, CANCEL_TIMEOUT_S=0.2):
        t = time.monotonic()
        res = asyncio.run(cancel_mod.cancel_task_impl({}, lambda: Hang("a")))
    assert res["is_error"] and time.monotonic() - t < 2


def test_cancel_tool_is_wired_into_brain():
    b = brain_mod.Brain(str(ROOT), speaker=None)
    assert cancel_mod.TOOL_ID == "mcp__jarvis_control__cancel_task"
    assert cancel_mod.TOOL_ID in b.options.allowed_tools
    assert "jarvis_control" in b.options.mcp_servers and "jarvis" in b.options.mcp_servers
    prompt = b.options.system_prompt["append"]
    assert cancel_mod.TOOL_ID in prompt and "hey Jarvis" in prompt and "half-finished" in prompt
    # the server's handler resolves brain.client when it runs
    assert brain_mod.describe_tool(type("B", (), {"name": cancel_mod.TOOL_ID, "input": {}})()) == "cancelling the current turn"


def test_wake_phrase_variants_are_rewritten_case_insensitively():
    from jarvis.speaker import WAKE_SUBSTITUTE, no_wake_phrase
    for v in ["hey Jarvis", "Hey Jarvis", "HEY JARVIS", "hey, Jarvis", "hi Jarvis", "Hi, jarvis",
              "ok Jarvis", "okay Jarvis", "OK  jarvis", "hay jarvis", "hey_jarvis", "hey-jarvis"]:
        out = no_wake_phrase(f"Just say {v} and I will listen.")
        assert "jarvis" not in out.lower(), (v, out)
        assert out == f"Just say {WAKE_SUBSTITUTE} and I will listen.", (v, out)
    # punctuation around it is kept; several occurrences are all rewritten
    assert no_wake_phrase('Say "hey Jarvis"! Or hey Jarvis.') == f'Say "{WAKE_SUBSTITUTE}"! Or {WAKE_SUBSTITUTE}.'
    assert no_wake_phrase("Hey Jarvis, what time is it?") == f"{WAKE_SUBSTITUTE}, what time is it?"


def test_normal_text_is_untouched_by_the_wake_filter():
    from jarvis.speaker import no_wake_phrase
    for t in ["Jarvis is ready.", "Hey there, Jarvis here.", "They said hi to everyone.",
              "Okay, I'll look at jarvis/brain.py.", "Nothing to see.", ""]:
        assert no_wake_phrase(t) == t, t


def test_speak_path_filters_the_wake_phrase_but_not_the_input_text():
    from jarvis.speaker import Speaker

    seen = []

    class Engine:
        def chunks(self, text):
            seen.append(text)
            return iter(())

    sp = Speaker.__new__(Speaker)
    sp.engine = Engine()
    sp.muted, sp.muted_speaks_permission_prompts = False, True  # __init__ is bypassed
    sp._live, sp._live_lock = set(), threading.Lock()
    original = 'Say "hey Jarvis" when you are ready.'
    assert sp.speak(original, threading.Event()) is True
    assert seen == ['Say "the wake word" when you are ready.'], seen
    assert original == 'Say "hey Jarvis" when you are ready.'  # callers (terminal/log) keep theirs


def test_system_prompt_forbids_speaking_the_wake_phrase():
    p = brain_mod.VOICE_PROMPT
    assert 'Never say "hey Jarvis"' in p and "just call me" in p


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
