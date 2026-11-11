"""Hang detector: dumps every thread's stack to logs/hang-<time>.txt when Jarvis stops making progress.

Two heartbeats:
  mic  - beaten whenever a mic frame is consumed. Only expected while Jarvis should be
         listening, so it is exempt inside `busy(...)` scopes (Claude thinking, Whisper
         transcribing, background speech), where the mic is legitimately unread.
  loop - beaten by a tiny asyncio task once a second. Always expected: if it stops,
         something is blocking the event loop.
Trade-off: a busy scope that really hangs (e.g. Claude never answers) is only caught
after its max_s cap (default 15 min), not after stall_s.
"""

import faulthandler
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from . import keepawake, ui
from .config import LOG_DIR


class Watchdog:
    def __init__(self, stall_s: float = 10.0, check_s: float = 1.0, min_gap_s: float = 60.0,
                 dump_dir=LOG_DIR, clock=time.monotonic, wall=time.time,
                 resume_gap_s: float = keepawake.RESUME_GAP_S):
        self.stall_s, self.check_s, self.min_gap_s = stall_s, check_s, min_gap_s
        self.dump_dir = dump_dir
        self.resume_gap_s = resume_gap_s
        self._clock = clock
        self._wall = wall
        now = clock()
        self._last_tick, self._last_wall = now, wall()
        self._beats = {"mic": now, "loop": now}
        self._busy: dict[int, tuple[str, float]] = {}  # token -> (name, expires at)
        self._token = 0
        self._last_dump = -1e9
        self._lock = threading.Lock()

    def beat(self, name: str = "mic"):
        self._beats[name] = self._clock()

    @contextmanager
    def busy(self, name: str, max_s: float = 900.0):
        """The mic is legitimately unread in here."""
        with self._lock:
            self._token += 1
            tok = self._token
            self._busy[tok] = (name, self._clock() + max_s)
        try:
            yield
        finally:
            with self._lock:
                self._busy.pop(tok, None)
            self.beat("mic")  # the listening clock restarts when we leave

    def _stalled(self) -> str | None:
        now = self._clock()
        with self._lock:
            busy = any(exp > now for _, exp in self._busy.values())
        for name, last in self._beats.items():
            if name == "mic" and busy:
                continue
            if now - last >= self.stall_s:
                return f"{name} stalled for {now - last:.0f}s"
        return None

    def _resumed(self) -> bool:
        """True if the PC slept since the last pass: the gap between our own ticks is far longer
        than normal AND the wall clock advanced by as much. Resets the stall timers if so."""
        now, wall = self._clock(), self._wall()
        gap, wall_gap = now - self._last_tick, wall - self._last_wall
        self._last_tick, self._last_wall = now, wall
        keepawake.heartbeat()
        if gap <= self.resume_gap_s or wall_gap <= self.resume_gap_s:
            return False
        keepawake.mark_resume()
        for name in self._beats:
            self._beats[name] = now
        ui.activity(f"System resumed from sleep (about {max(1, round(wall_gap / 60))} minutes); "
                    "skipping hang dump.")
        return True

    def check(self):
        """One watchdog pass. Returns the dump path if a dump was written."""
        if self._resumed():
            return None
        why = self._stalled()
        now = self._clock()
        if not why or now - self._last_dump < self.min_gap_s:
            return None
        self._last_dump = now
        path = self.dump_dir / f"hang-{datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Jarvis watchdog: {why}\n\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception as e:
            ui.error(f"Watchdog: {why}, but writing the thread dump failed ({e}).")
            return None
        ui.error(f"Watchdog: {why}. Thread dump written to {path}")
        return path

    def start(self):
        def run():
            while True:
                time.sleep(self.check_s)
                try:
                    self.check()
                except Exception as e:  # the watchdog must never die quietly
                    ui.error(f"Watchdog error ({e}).")

        threading.Thread(target=run, daemon=True, name="watchdog").start()


async def loop_heartbeat(wd: Watchdog):
    import asyncio

    while True:
        wd.beat("loop")
        await asyncio.sleep(1.0)
