"""Windows sleep helpers: keep the PC awake while a usage-limit restart is pending, and notice
when the PC has just woken from sleep (so sleep is not mistaken for a hang or a lost connection).

Keep-awake: SetThreadExecutionState is per-thread, so one dedicated long-lived thread holds the
request (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) until told to stop, then clears it. Safe no-op on
non-Windows; never raises.

Resume detection: the watchdog calls heartbeat() every tick and mark_resume() when it sees a gap
in its own ticks that the wall clock confirms. resume_suspected() lets other code (the Claude
reconnect path) ask "did we just wake from sleep?", even if the watchdog has not ticked yet.
"""

import atexit
import sys
import threading
import time

from . import ui

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
RESUME_GAP_S = 30.0  # a tick gap this much longer than normal, confirmed by the wall clock = sleep
RESUME_WINDOW_S = 120.0  # a connection loss this soon after a resume is blamed on the sleep

# Test hooks.
mono_fn = time.monotonic
wall_fn = time.time


def _log(msg: str) -> None:
    try:
        ui.activity(msg)
    except Exception:
        pass


def _set_state(flags: int) -> None:
    """Call SetThreadExecutionState for the calling thread. Test hook; no-op off Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


class KeepAwake:
    def __init__(self, reason: str = "usage-limit restart pending"):
        self.reason = reason
        self._lock = threading.Lock()
        self._stop = None
        self._thread = None

    @property
    def active(self) -> bool:
        return self._thread is not None

    def acquire(self) -> None:
        """Start holding the request (idempotent). Never raises."""
        try:
            with self._lock:
                if self._thread is not None:
                    return
                stop = threading.Event()
                ready = threading.Event()

                def run():
                    try:
                        _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                    except Exception:
                        pass
                    ready.set()
                    stop.wait()
                    try:
                        _set_state(ES_CONTINUOUS)
                    except Exception:
                        pass

                t = threading.Thread(target=run, daemon=True, name="keepawake")
                t.start()
                ready.wait(5)
                self._stop, self._thread = stop, t
            _log(f"Keeping the PC awake ({self.reason}).")
        except Exception:
            pass

    def release(self) -> None:
        """Let the PC sleep again (idempotent). Never raises."""
        try:
            with self._lock:
                t, stop = self._thread, self._stop
                self._thread = self._stop = None
            if t is None:
                return
            stop.set()
            t.join(5)
            _log("Released the keep-awake request; the PC may sleep again.")
        except Exception:
            pass


usage_pending = KeepAwake()
atexit.register(usage_pending.release)


# ---- resume detection -----------------------------------------------------------------------

_state = {"mark": None, "tick": None, "tick_wall": None}


def reset() -> None:
    """Forget all resume state (tests)."""
    _state.update(mark=None, tick=None, tick_wall=None)


def heartbeat() -> None:
    """The watchdog is alive right now."""
    _state["tick"], _state["tick_wall"] = mono_fn(), wall_fn()


def mark_resume() -> None:
    _state["mark"] = mono_fn()


def resume_suspected(window_s: float = RESUME_WINDOW_S) -> bool:
    """True if a sleep/resume was detected within window_s, or the watchdog's own ticks have a
    sleep-sized gap that nobody has marked yet. Never raises."""
    try:
        now = mono_fn()
        mark = _state["mark"]
        if mark is not None and now - mark <= window_s:
            return True
        tick, tick_wall = _state["tick"], _state["tick_wall"]
        return (tick is not None and now - tick > RESUME_GAP_S
                and wall_fn() - tick_wall > RESUME_GAP_S)
    except Exception:
        return False
