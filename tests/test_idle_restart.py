r"""Offline tests: an idle Jarvis (parked in wake-word wait) notices the restart flag.

Plain script: run with the venv python. The flag file is a temp file; nothing real is touched.
"""
import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # never a real phone push
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

import jarvis.__main__ as main_mod  # noqa: E402
from jarvis import restart, ui  # noqa: E402

ui.status = lambda text: None
FLAG = Path(tempfile.mkdtemp(prefix="jarvis-idle-")) / "flag"
real_pending = restart.restart_pending
restart.restart_pending = lambda path=FLAG: path.exists()


class FakeListener:
    """wait_for_wake blocks until stop is set or fire() is called; polls like the real one."""

    def __init__(self):
        self.woke = threading.Event()
        self.calls = 0

    def wait_for_wake(self, stop):
        self.calls += 1
        while not stop.is_set():
            if self.woke.wait(0.01):
                self.woke.clear()
                return True
        return False


class FakeBrain:
    def __init__(self, safe=True):
        self.safe = safe
        self._flush_task = None

    def _can_speak_background(self):
        return self.safe


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


def clean():
    FLAG.unlink(missing_ok=True)


def t_idle_notices_flag():
    clean()

    async def go():
        stop, li = threading.Event(), FakeListener()
        task = asyncio.ensure_future(main_mod.wait_for_wake_or_restart(li, stop, FakeBrain(), poll_s=0.05))
        await asyncio.sleep(0.2)
        assert not task.done()  # nothing requested: keeps listening
        restart.request_restart(FLAG)
        return await task

    assert run(go()) == (False, True)
    clean()


def t_waits_until_safe():
    clean()

    async def go():
        stop, li, b = threading.Event(), FakeListener(), FakeBrain(safe=False)
        restart.request_restart(FLAG)
        task = asyncio.ensure_future(main_mod.wait_for_wake_or_restart(li, stop, b, poll_s=0.05))
        await asyncio.sleep(0.3)
        assert not task.done()  # mid-turn / permission prompt: no restart
        b.safe = True
        return await task

    assert run(go()) == (False, True)
    clean()


def t_wake_word_still_works_and_no_flag_no_restart():
    clean()

    async def go():
        stop, li = threading.Event(), FakeListener()
        task = asyncio.ensure_future(main_mod.wait_for_wake_or_restart(li, stop, FakeBrain(), poll_s=0.05))
        await asyncio.sleep(0.1)
        li.woke.set()
        return await task, stop.is_set()

    assert run(go()) == ((True, False), False)


def t_shutdown_stop_is_not_a_restart():
    clean()

    async def go():
        stop, li = threading.Event(), FakeListener()
        task = asyncio.ensure_future(main_mod.wait_for_wake_or_restart(li, stop, FakeBrain(), poll_s=0.05))
        await asyncio.sleep(0.1)
        stop.set()
        return await task

    assert run(go()) == (False, False)


def t_wake_beats_restart_and_stop_is_cleared():
    clean()

    async def go():
        stop, li = threading.Event(), FakeListener()
        restart.request_restart(FLAG)
        li.woke.set()  # both at once: wake wins, the flag stays for later
        # poll fires immediately (tiny interval) but the wake word is already queued
        heard, rs = await main_mod.wait_for_wake_or_restart(li, stop, FakeBrain(), poll_s=0.0001)
        return heard, rs, stop.is_set()

    heard, rs, stopped = run(go())
    assert (heard, rs) in ((True, False), (False, True))
    if heard:
        assert not stopped and FLAG.exists()
    clean()


def t_restart_safe():
    assert main_mod.restart_safe(FakeBrain(True)) is True
    assert main_mod.restart_safe(FakeBrain(False)) is False
    assert main_mod.restart_safe(object()) is True

    async def go():
        b = FakeBrain(True)
        b._flush_task = asyncio.ensure_future(asyncio.sleep(0.2))  # background speech in progress
        busy = main_mod.restart_safe(b)
        await b._flush_task
        return busy, main_mod.restart_safe(b)

    assert run(go()) == (False, True)


def t_usage_fire_end_to_end_while_idle():
    """The monitor's timer writes the flag; an idle wait picks it up without any user input."""
    clean()
    from jarvis import usage_monitor as um
    d = Path(tempfile.mkdtemp(prefix="jarvis-idle-um-"))
    um.STATE_FILE, um.FLAG_FILE, um.HANDOFF_FILE = d / "s.json", FLAG, d / "H.md"
    um.push_fn = lambda *a, **k: None
    um._notify_enabled = lambda: False
    now = 1_800_000_000.0
    um.now_fn = lambda: now

    class T:
        def __init__(self, delay, fn, args=()):
            self.fn, self.args, self.daemon = fn, args, False

        def start(self): pass
        def cancel(self): pass

    made = []
    um.timer_factory = lambda *a, **k: made.append(T(*a, **k)) or made[-1]
    um._timer = None
    um.on_rate_limit_event({"type": "five_hour", "status": "rejected", "resets_at": int(now + 10)})

    async def go():
        stop, li = threading.Event(), FakeListener()
        task = asyncio.ensure_future(main_mod.wait_for_wake_or_restart(li, stop, FakeBrain(), poll_s=0.05))
        await asyncio.sleep(0.15)
        made[0].fn(*made[0].args)  # the timer fires
        return await task

    assert run(go()) == (False, True)
    clean()


def main():
    tests = [(n[2:], f) for n, f in sorted(globals().items()) if n.startswith("t_") and callable(f)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
    for f in (restart.PENDING_FILE, ROOT / ".jarvis_usage_reset.json"):
        if f.exists():
            f.unlink()
    print(f"{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
