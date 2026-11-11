r"""Offline tests for Claude-connection recovery and crash reports. No audio, no real Claude.

Run (base interpreter):
    .venv\Scripts\python.exe tests\test_recovery.py

Plain script, no pytest needed. Prints PASS/FAIL per test, exits non-zero on failure.
Logs, crash reports and the session-state file all go to a temp directory.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
import asyncio
import faulthandler
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

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402
from claude_agent_sdk._errors import CLIJSONDecodeError  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import crashreport, ui  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
ERRORS: list[str] = []
ui._log_path = TMP / "test.log"  # never touch the real log
brain_mod.STATE_PATH = TMP / "state.json"
crashreport.configure(TMP)


def _err(text):
    ERRORS.append(text)
    ui._log("ERROR", text)


ui.error = _err
ui.status = lambda text: None
PRINTED: list[str] = []
ui.jarvis = lambda text, model=None: PRINTED.append(text)
brain_mod.BACKOFF_S = (0.05, 0.1, 0.2)
brain_mod.DISCONNECT_TIMEOUT_S = 0.5
brain_mod.CLOSE_WAIT_S = 2.0


# ---- fake SDK client --------------------------------------------------------------------

class FakeClient:
    """Stands in for ClaudeSDKClient. Behaviour is set per instance index via `plan`:
    plan[i] = {"fail": exc | "eof" | None, "connect_error": exc | None}. Each instance answers
    queries ("reply to X") until its `fail` fires on a query, after yielding one message."""
    instances: list = []
    plan: dict = {}
    connect_calls = 0

    def __init__(self, options):
        self.options = options
        self.idx = len(FakeClient.instances)
        FakeClient.instances.append(self)
        self.cfg = FakeClient.plan.get(self.idx, {})
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.queries, self.models, self.disconnected = [], [], False

    async def connect(self):
        FakeClient.connect_calls += 1
        if self.cfg.get("connect_hang"):
            await asyncio.sleep(30)
        err = self.cfg.get("connect_error")
        if callable(err):
            err = err(self.options)
        if err:
            raise err

    async def disconnect(self):
        await asyncio.sleep(self.cfg.get("disconnect_s", 0))
        self.disconnected = True

    async def query(self, text):
        if not isinstance(text, str):  # ask() sends a stamped stream message, like the SDK accepts
            text = [m async for m in text][0]["message"]["content"]
        self.queries.append(text)
        await self.inbox.put(text)

    async def set_model(self, model):
        self.models.append(model)

    async def receive_messages(self):
        while True:
            text = await self.inbox.get()
            fail = self.cfg.get("fail")
            yield AssistantMessage(content=[TextBlock(text="working")], model="claude-fake")
            if fail == "eof":
                return
            if fail:
                raise fail
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="sess-1", result=f"reply to {text}")


def reset_fake(plan=None):
    FakeClient.instances, FakeClient.plan, FakeClient.connect_calls = [], plan or {}, 0
    brain_mod.ClaudeSDKClient = FakeClient
    ERRORS.clear()
    PRINTED.clear()
    crashreport.configure(TMP)


class QuietSpeaker:
    """Brain only needs a truthy speaker; with hold_background nothing is spoken, notices stay queued."""

    def deadline_for(self, text):
        return 1.0

    def speak(self, text, stop):
        return True


def new_brain(model="sonnet", speaker=True):
    b = brain_mod.Brain(str(ROOT), model=model, speaker=QuietSpeaker() if speaker else None)
    b.hold_background = True
    return b


async def drain(b):
    """Wait for background disconnects of old clients."""
    if b._bg_discards:
        await asyncio.wait(list(b._bg_discards), timeout=3)


async def wait_until(pred, timeout=5.0):
    t = time.monotonic()
    while not pred():
        assert time.monotonic() - t < timeout, "timed out waiting"
        await asyncio.sleep(0.01)


async def settle(b):
    """Wait for any recovery to finish."""
    await wait_until(lambda: not b._recovering)


def assert_clean(b):
    assert not b._outstanding and not b._turn_active
    assert not b._perm_lock.locked() and not b._flush_lock.locked()
    assert b._reader_task is not None and not b._reader_task.done()


async def drop_scenario(fail, expect_in_log=None):
    """Healthy ask, model switch, then a failure during the 2nd ask, then recovery."""
    reset_fake({0: {"fail": None}})
    FakeClient.plan[0] = {"fail": None}
    b = new_brain()
    await b.start()
    assert await asyncio.wait_for(b.ask("hello"), 3) == "reply to hello"
    assert b.session_id == "sess-1"
    await b.set_model("opus")  # user switched by voice: reconnect must keep this
    FakeClient.instances[0].cfg["fail"] = fail
    t = time.monotonic()
    reply = await asyncio.wait_for(b.ask("do the thing"), 3)  # resolves, doesn't hang or raise
    assert time.monotonic() - t < 2
    assert "interrupted" in reply and "lost my connection" in reply, reply
    await settle(b)
    await drain(b)
    old, new = FakeClient.instances[0], FakeClient.instances[1]
    assert old.disconnected
    assert new.options.resume == "sess-1" and new.options.model == "opus", (new.options.resume, new.options.model)
    assert old.models == ["opus"]
    assert_clean(b)
    assert await asyncio.wait_for(b.ask("again"), 3) == "reply to again"  # next turn works
    assert_clean(b)
    assert brain_mod.BACK_MSG in b._background_queue
    reports = list(TMP.glob("crash-*.txt"))
    assert reports, "connection failure must write a crash report"
    await b.close()
    return b


def test_reconnect_after_json_decode_error():
    exc = CLIJSONDecodeError("x" * 50, ValueError("JSON message exceeded maximum buffer size"))
    for f in TMP.glob("crash-*.txt"):  # reports left by earlier tests must not be mistaken for ours
        f.unlink()
    asyncio.run(drop_scenario(exc))
    # content, not mtime: file times can tie on coarse filesystem clocks
    texts = [p.read_text(encoding="utf-8") for p in TMP.glob("crash-*.txt")]
    assert any("CLIJSONDecodeError" in t for t in texts), texts


def test_reconnect_after_generic_error():
    asyncio.run(drop_scenario(RuntimeError("transport exploded")))


def test_reconnect_after_early_end_of_stream():
    asyncio.run(drop_scenario("eof"))


def test_failed_write_is_handled():
    async def run():
        reset_fake()
        b = new_brain()
        await b.start()

        async def bad_query(text):
            raise ConnectionError("pipe closed")

        b.client.query = bad_query
        reply = await asyncio.wait_for(b.ask("hi"), 3)
        assert "interrupted" in reply
        await settle(b)
        assert len(FakeClient.instances) == 2
        assert_clean(b)
        assert await asyncio.wait_for(b.ask("hi again"), 3) == "reply to hi again"
        await b.close()

    asyncio.run(run())


def test_ask_while_reconnecting_answers_locally():
    async def run():
        reset_fake({1: {"connect_error": ConnectionError("down")}, 2: {"connect_error": ConnectionError("down")}})
        b = new_brain()
        await b.start()
        b.client.cfg["fail"] = RuntimeError("boom")
        await asyncio.wait_for(b.ask("one"), 3)
        assert b._recovering
        assert await asyncio.wait_for(b.ask("two"), 1) == brain_mod.RECONNECTING_MSG
        assert not b._outstanding
        await settle(b)
        assert await asyncio.wait_for(b.ask("three"), 3) == "reply to three"
        await b.close()

    asyncio.run(run())


def test_backoff_retries_until_connect_succeeds():
    async def run():
        down = ConnectionError("cli not starting")
        reset_fake({i: {"connect_error": down} for i in (1, 2, 3)})
        b = new_brain()
        await b.start()
        b.client.cfg["fail"] = RuntimeError("boom")
        await asyncio.wait_for(b.ask("one"), 3)
        t = time.monotonic()
        await settle(b)
        took = time.monotonic() - t
        # initial connect + 3 failures + 1 success; waits 0.05 + 0.1 + 0.2 between them
        assert FakeClient.connect_calls == 5, FakeClient.connect_calls
        assert took >= 0.3, took
        assert sum("Reconnect attempt" in e for e in ERRORS) == 3, ERRORS
        await drain(b)
        assert len(FakeClient.instances) == 5 and all(c.disconnected for c in FakeClient.instances[:4])
        assert_clean(b)
        await b.close()

    asyncio.run(run())


def test_resume_failure_falls_back_to_fresh_session():
    async def run():
        def no_resume(options):
            return ConnectionError("exit code 1") if options.resume else None

        reset_fake({i: {"connect_error": no_resume} for i in range(1, 10)})
        b = new_brain()
        await b.start()
        await asyncio.wait_for(b.ask("hello"), 3)
        assert b.session_id == "sess-1"
        b.client.cfg["fail"] = RuntimeError("boom")
        await asyncio.wait_for(b.ask("one"), 3)
        await settle(b)
        last = FakeClient.instances[-1]
        assert last.options.resume is None and b.session_id is None
        assert any(c.options.resume == "sess-1" for c in FakeClient.instances[1:-1])
        assert brain_mod.FRESH_MSG in b._background_queue
        assert await asyncio.wait_for(b.ask("fresh"), 3) == "reply to fresh"
        await b.close()

    asyncio.run(run())


def test_resume_error_text_falls_back_immediately():
    async def run():
        def bad(options):
            return RuntimeError("No conversation found with session ID") if options.resume else None

        reset_fake({1: {"connect_error": bad}})
        b = new_brain()
        await b.start()
        await asyncio.wait_for(b.ask("hello"), 3)
        b.client.cfg["fail"] = RuntimeError("boom")
        await asyncio.wait_for(b.ask("one"), 3)
        await settle(b)
        assert len(FakeClient.instances) == 3  # original, failed resume, fresh
        assert FakeClient.instances[-1].options.resume is None
        await b.close()

    asyncio.run(run())


def test_loss_resets_locks_and_turn_state():
    async def run():
        reset_fake()
        b = new_brain()
        await b.start()
        await b._perm_lock.acquire()  # a permission prompt was open when the stream died
        b._turn_active, b._last_text = True, "stale"
        b._bg_stop = threading.Event()
        stop = b._bg_stop
        b._on_stream_lost(RuntimeError("boom"))
        assert not b._perm_lock.locked() and not b._turn_active and b._last_text == ""
        assert stop.is_set()
        await settle(b)
        assert_clean(b)
        await b.close()

    asyncio.run(run())


def test_close_during_recovery_is_clean():
    async def run():
        reset_fake({i: {"connect_error": ConnectionError("down")} for i in range(1, 50)})
        b = new_brain()
        await b.start()
        b._on_stream_lost(RuntimeError("boom"))
        await asyncio.sleep(0.1)
        await asyncio.wait_for(b.close(), 3)
        assert b._recover_task is None

    asyncio.run(run())


# ---- crash reports ----------------------------------------------------------------------

def read_report(path):
    return Path(path).read_text(encoding="utf-8")


def test_crash_report_contents():
    reset_fake()
    ui._log("YOU", "unique-log-line-123")
    ui.you("what is the weather")
    ui.activity("reading something.py")
    crashreport.note(mode="thinking")
    crashreport.register("run", lambda: {"voice": "ryan"})

    async def run():
        b = new_brain("opus")
        fut = asyncio.get_running_loop().create_future()
        b._outstanding.append(fut)
        try:
            try:
                raise ValueError("the root cause")
            except ValueError as e:
                raise RuntimeError("outer failure") from e
        except RuntimeError as e:
            return crashreport.report("contents", e, "ctx"), b

    path, b = asyncio.run(run())
    assert path and path.exists() and path.parent == TMP and path.name.startswith("crash-")
    text = read_report(path)
    for needle in ("RuntimeError: outer failure", "the root cause", "direct cause", "Traceback",
                   "mode: thinking", "what is the weather", "outstanding_asks: 1", "model_choice: opus",
                   "session_id:", "project_dir:", "run.voice: ryan", "python:", "jarvis:",
                   "reading something.py", "unique-log-line-123", "thread 0x", "== threads =="):
        assert needle in text, needle
    assert any(str(path) in e for e in ERRORS), ERRORS


def test_keyboard_interrupt_is_not_a_crash():
    reset_fake()
    assert crashreport.report("kbd", KeyboardInterrupt()) is None
    assert crashreport.report("exit", SystemExit(1)) is None
    crashreport._orig_excepthook, orig = (lambda *a: None), crashreport._orig_excepthook
    try:
        crashreport._excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    finally:
        crashreport._orig_excepthook = orig
    n = len(list(TMP.glob("crash-*.txt")))
    crashreport.report("kbd", KeyboardInterrupt())
    assert len(list(TMP.glob("crash-*.txt"))) == n


def test_excepthooks_write_reports():
    reset_fake()
    before = set(TMP.glob("crash-*.txt"))
    orig_ex, orig_th = crashreport._orig_excepthook, crashreport._orig_thread_hook
    crashreport._orig_excepthook = lambda *a: None
    crashreport._orig_thread_hook = lambda args: None
    try:
        try:
            raise ValueError("main thread boom")
        except ValueError as e:
            crashreport._excepthook(type(e), e, e.__traceback__)
        mid = set(TMP.glob("crash-*.txt")) - before
        assert len(mid) == 1 and "main thread boom" in read_report(next(iter(mid)))

        old_hook = threading.excepthook
        threading.excepthook = crashreport._thread_hook
        try:
            def worker():
                raise KeyError("worker boom")

            th = threading.Thread(target=worker, name="w1")
            th.start()
            th.join()
        finally:
            threading.excepthook = old_hook
        new = set(TMP.glob("crash-*.txt")) - before - mid
        assert len(new) == 1
        txt = read_report(next(iter(new)))
        assert "worker boom" in txt and "w1" in txt
    finally:
        crashreport._orig_excepthook, crashreport._orig_thread_hook = orig_ex, orig_th


def test_asyncio_handler_writes_report():
    reset_fake()
    before = set(TMP.glob("crash-*.txt"))

    async def run():
        loop = asyncio.get_running_loop()
        crashreport.install_loop()
        loop.set_exception_handler(lambda l, c: crashreport.loop_handler(
            type("L", (), {"default_exception_handler": lambda self, ctx: None})(), c))

        async def bad():
            raise OSError("task blew up")

        t = asyncio.create_task(bad())
        await asyncio.sleep(0.05)
        del t  # never awaited: "Task exception was never retrieved" goes to the handler
        import gc
        gc.collect()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    new = set(TMP.glob("crash-*.txt")) - before
    assert new and "task blew up" in read_report(next(iter(new)))


def test_rate_limit_and_pruning():
    reset_fake()
    p1 = crashreport.report("rl", RuntimeError("a"))
    assert p1
    assert crashreport.report("rl", RuntimeError("b")) is None  # same kind within 30 s
    assert crashreport.report("other", RuntimeError("c"))  # other kinds are independent
    old = crashreport.MIN_GAP_S
    crashreport.MIN_GAP_S = 0.0
    try:
        for i in range(60):
            crashreport.report("burst", RuntimeError(str(i)))
    finally:
        crashreport.MIN_GAP_S = old
    assert len(list(TMP.glob("crash-*.txt"))) <= crashreport.KEEP_FILES


def test_same_second_reports_get_distinct_files():
    import datetime as _dt

    reset_fake()
    for f in TMP.glob("crash-*.txt"):
        f.unlink()

    class Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, 3, 4, 5, 6)

    orig_dt, orig_gap = crashreport.datetime, crashreport.MIN_GAP_S
    crashreport.datetime, crashreport.MIN_GAP_S = Frozen, 0.0
    try:
        paths = [crashreport.report("frozen", RuntimeError(str(i))) for i in range(5)]
        # a name that is already taken must be skipped, not raised
        taken = crashreport._dir / f"crash-20260102-030405-000006-{__import__('os').getpid()}-{next(crashreport._seq) + 1}.txt"
        taken.write_text("occupied", encoding="utf-8")
        paths.append(crashreport.report("frozen", RuntimeError("collide")))
    finally:
        crashreport.datetime, crashreport.MIN_GAP_S = orig_dt, orig_gap
    assert all(paths) and len(set(paths)) == 6, paths
    assert taken.read_text(encoding="utf-8") == "occupied"
    assert len(list(TMP.glob("crash-*.txt"))) == 7


def test_reporter_never_raises():
    reset_fake()
    crashreport.configure(TMP / "state.json" / "not-a-dir")  # mkdir under a file fails
    assert crashreport.report("bad-dir", RuntimeError("x")) is None
    crashreport.configure(TMP)


def test_faulthandler_enabled_to_fatal_txt():
    d = TMP / "fatal-test"
    was = faulthandler.is_enabled()
    crashreport.install(log_dir=d, hooks=False)
    try:
        assert faulthandler.is_enabled()
        fatal = d / "fatal.txt"
        assert fatal.exists() and "Jarvis started" in fatal.read_text(encoding="utf-8")
        crashreport.install(log_dir=d, hooks=False)  # second run appends a header
        assert fatal.read_text(encoding="utf-8").count("Jarvis started") == 2
    finally:
        crashreport.configure(TMP)
        if not was:
            pass  # leave it enabled; the process is about to exit


# ---- review fixes -----------------------------------------------------------------------

def test_old_client_disconnect_is_background_and_not_cancelled():
    async def run():
        reset_fake({0: {"disconnect_s": 0.4}})
        b = new_brain()
        await b.start()
        old = b.client
        t = time.monotonic()
        b._on_stream_lost(RuntimeError("boom"))
        await settle(b)
        assert time.monotonic() - t < 0.3, "recovery must not wait for the old CLI to die"
        assert not old.disconnected
        await drain(b)
        assert old.disconnected  # ran to completion, not cancelled at the old 5 s bound
        await b.close()

    asyncio.run(run())


def test_old_cli_is_killed_if_disconnect_hangs():
    class Proc:
        returncode = None
        killed = False

        def kill(self):
            Proc.killed = True

    async def run():
        reset_fake({0: {"disconnect_s": 10}})
        old_t = brain_mod.DISCONNECT_TIMEOUT_S
        brain_mod.DISCONNECT_TIMEOUT_S = 0.1
        try:
            b = new_brain()
            await b.start()
            b.client._transport = type("T", (), {"_process": Proc()})()
            b._on_stream_lost(RuntimeError("boom"))
            await settle(b)
            await drain(b)
            assert Proc.killed
            await b.close()
        finally:
            brain_mod.DISCONNECT_TIMEOUT_S = old_t

    asyncio.run(run())


def test_close_mid_connect_discards_half_started_client():
    async def run():
        reset_fake({1: {"connect_hang": True}})
        b = new_brain()
        await b.start()
        b._on_stream_lost(RuntimeError("boom"))
        await wait_until(lambda: len(FakeClient.instances) == 2)
        await asyncio.sleep(0.05)  # inside connect()
        await asyncio.wait_for(b.close(), 5)
        assert FakeClient.instances[1].disconnected, "half-started client must be disconnected"
        assert FakeClient.instances[0].disconnected

    asyncio.run(run())


def test_generic_session_word_does_not_drop_the_session():
    async def run():
        def err(options):
            return RuntimeError("failed to open session worker") if options.resume else None

        reset_fake({1: {"connect_error": err}})
        b = new_brain()
        await b.start()
        await asyncio.wait_for(b.ask("hello"), 3)
        b.client.cfg["fail"] = RuntimeError("boom")
        await asyncio.wait_for(b.ask("one"), 3)
        await settle(b)
        assert FakeClient.instances[2].options.resume == "sess-1"  # retried with resume, not fresh
        assert b.session_id == "sess-1"
        await b.close()

    asyncio.run(run())


def test_streak_resets_after_healthy_connection():
    async def run():
        reset_fake()
        b = new_brain()
        await b.start()
        b._streak = 2
        b._connected_at = time.monotonic() - brain_mod.HEALTHY_S - 1  # idle for a long time
        b._on_stream_lost(RuntimeError("cli exited overnight"))
        assert b._streak == 1
        await settle(b)
        b._on_stream_lost(RuntimeError("again right away"))  # just reconnected: counts up
        assert b._streak == 2
        await settle(b)
        await b.close()

    asyncio.run(run())


def test_notices_coalesce_when_flapping():
    async def run():
        reset_fake()
        b = new_brain()
        await b.start()
        for _ in range(5):
            b._on_stream_lost(RuntimeError("flap"))
            await settle(b)
        # outages 1-2: the unspoken "lost" notice is cancelled by the reconnect; outage 3 says "unstable"
        # once; 4-5 stay quiet; no "reconnected" chatter while unstable.
        assert b._background_queue == [brain_mod.UNSTABLE_MSG], b._background_queue
        assert PRINTED.count(brain_mod.UNSTABLE_MSG) == 1
        assert PRINTED.count(brain_mod.LOST_MSG) == 2
        assert brain_mod.BACK_MSG not in PRINTED
        await b.close()

    asyncio.run(run())


def test_lost_and_back_once_per_outage():
    async def run():
        reset_fake({1: {"connect_error": ConnectionError("down")}})
        b = new_brain()
        await b.start()
        b._on_stream_lost(RuntimeError("boom"))
        assert b._background_queue == [brain_mod.LOST_MSG]
        b._announce(brain_mod.LOST_MSG)  # duplicates are not queued
        assert b._background_queue == [brain_mod.LOST_MSG]
        b._background_queue.clear()  # pretend it was spoken
        await settle(b)
        assert b._background_queue == [brain_mod.BACK_MSG]
        await b.close()

    asyncio.run(run())


def test_chat_mode_prints_notices_and_queues_nothing():
    async def run():
        reset_fake()
        b = new_brain(speaker=False)
        await b.start()
        for _ in range(2):
            b._on_stream_lost(RuntimeError("boom"))
            await settle(b)
        assert b._background_queue == []
        assert brain_mod.LOST_MSG in PRINTED and brain_mod.BACK_MSG in PRINTED
        await b.close()

    asyncio.run(run())


def test_broken_stdout_is_not_a_crash():
    import io

    from rich.console import Console

    reset_fake()
    for f in TMP.glob("crash-*.txt"):
        f.unlink()
    assert crashreport.report("pipe", BrokenPipeError(32, "Broken pipe")) is None

    class BadOut(io.StringIO):
        def write(self, s):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

    try:
        Console(file=BadOut()).print("hello")
    except OSError as e:
        assert crashreport.is_stdout_error(e)
        assert crashreport.report("voices", e) is None
    else:
        raise AssertionError("expected OSError")
    assert not list(TMP.glob("crash-*.txt"))
    assert crashreport.report("other-oserror", OSError(22, "Invalid argument")) is not None  # elsewhere: real


def test_loop_handler_ignores_message_only_entries():
    reset_fake()
    for f in TMP.glob("crash-*.txt"):
        f.unlink()

    class L:
        def default_exception_handler(self, ctx):
            pass

    crashreport.loop_handler(L(), {"message": "Task was destroyed but it is pending!"})
    assert not list(TMP.glob("crash-*.txt"))


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
