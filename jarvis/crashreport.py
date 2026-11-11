"""Crash reports: write logs/crash-<time>.txt whenever something goes wrong, by default.

What a report holds: the exception (with chained causes), every thread's stack, a snapshot of
what Jarvis was doing (main-loop mode, last utterance, Claude session state, recent activity
lines), and the tail of today's log. Hard crashes in native code (segfault/abort) cannot run
Python, so `install()` also points faulthandler at logs/fatal.txt for the life of the process.

Nothing in here may raise: every public function swallows its own failures (stderr fallback).
Ctrl+C / SystemExit are never reported. Reports are rate limited per kind, and only the
newest KEEP_FILES crash files are kept.
"""

import asyncio
import errno
import faulthandler
import itertools
import os
import platform
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import ui
from .config import LOG_DIR

MIN_GAP_S = 30.0  # at most one report per kind in this window
KEEP_FILES = 50
LOG_TAIL_LINES = 100
ACTIVITY_LINES = 20
FATAL_MAX_BYTES = 1_000_000

_dir: Path = LOG_DIR
_last: dict[str, float] = {}
_state: dict[str, object] = {}
_providers: dict[str, object] = {}
_fatal_file = None
_lock = threading.Lock()
_seq = itertools.count(1)


def configure(log_dir=None):
    """Point reports at another directory (tests) and forget rate-limit history."""
    global _dir
    _dir = Path(log_dir) if log_dir else LOG_DIR
    _last.clear()


def note(**kw):
    """Record bits of state for the snapshot (e.g. mode="thinking")."""
    _state.update(kw)


def register(name: str, provider):
    """provider() returns a dict (or any value) included in the snapshot under `name`."""
    _providers[name] = provider


def _tail(path, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except Exception as e:
        return [f"(couldn't read log {path}: {e})"]


def _snapshot() -> list[str]:
    lines = [f"python: {sys.version.split()[0]} ({platform.platform()})"]
    try:
        from . import __version__
    except Exception:
        __version__ = "unknown"
    lines.append(f"jarvis: {__version__}")
    for k, v in _state.items():
        lines.append(f"{k}: {v}")
    lines.append(f"last user utterance: {getattr(ui, 'LAST_YOU', '')!r}")
    for name, fn in _providers.items():
        try:
            val = fn()
        except Exception as e:
            val = f"(provider failed: {e})"
        if isinstance(val, dict):
            for k, v in val.items():
                lines.append(f"{name}.{k}: {v}")
        else:
            lines.append(f"{name}: {val}")
    return lines


def _prune():
    files = sorted(_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_FILES]:
        try:
            old.unlink()
        except OSError:
            pass


def _is_ignored(exc) -> bool:
    return isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)) or (
        isinstance(exc, asyncio.CancelledError))


def is_stdout_error(exc) -> bool:
    """A broken/closed stdout (e.g. `jarvis voices | head`): not a Jarvis bug."""
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.EINVAL, errno.EPIPE):
        for fs, _ in traceback.walk_tb(exc.__traceback__):
            name = fs.f_code.co_filename.replace("\\", "/")
            if name.endswith(("/rich/console.py", "/jarvis/ui.py")) or name.endswith("<stdout>"):
                return True
    return False


def _open_unique():
    """Create a new crash file that cannot collide with another report (same second, another
    thread or another process): name = time to the microsecond + pid + counter, opened with
    exclusive create; on a collision try the next name."""
    last = None
    for _ in range(100):
        stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
        path = _dir / f"crash-{stamp}-{os.getpid()}-{next(_seq)}.txt"
        try:
            return open(path, "x", encoding="utf-8")
        except FileExistsError as e:
            last = e
    raise last


def report(kind: str, exc: BaseException | None = None, context: str = "") -> Path | None:
    """Write a crash report. Returns its path, or None if skipped (ignored, rate limited,
    already reported, or failed). Never raises."""
    try:
        if exc is not None:
            if _is_ignored(exc) or is_stdout_error(exc) or getattr(exc, "_jarvis_reported", False):
                return None
        with _lock:
            now = time.monotonic()
            if now - _last.get(kind, -1e9) < MIN_GAP_S:
                return None
            _last[kind] = now
        if exc is not None:
            try:
                exc._jarvis_reported = True
            except Exception:
                pass
        _dir.mkdir(parents=True, exist_ok=True)
        f = _open_unique()
        path = Path(f.name)
        with f:
            f.write(f"Jarvis crash report: {kind}\n")
            f.write(f"time: {datetime.now().isoformat(timespec='seconds')}\n")
            if context:
                f.write(f"context: {context}\n")
            f.write("\n== exception ==\n")
            if exc is not None:
                f.write(f"{type(exc).__name__}: {exc}\n\n")
                f.write("".join(traceback.format_exception(exc)))  # includes chained causes
            else:
                f.write("(none)\n")
            f.write("\n== state ==\n")
            f.write("\n".join(_snapshot()) + "\n")
            f.write(f"\n== last {ACTIVITY_LINES} activity lines ==\n")
            recent = [x for x in list(getattr(ui, "RECENT", [])) if "] ACTIVITY:" in x]
            f.write("\n".join(recent[-ACTIVITY_LINES:]) + "\n")
            f.write("\n== threads ==\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.write(f"\n== last {LOG_TAIL_LINES} log lines ({getattr(ui, '_log_path', '?')}) ==\n")
            f.write("\n".join(_tail(getattr(ui, "_log_path", ""), LOG_TAIL_LINES)) + "\n")
        try:
            _prune()
        except Exception:
            pass
        _say_error(f"Crash report ({kind}) written to {path}")
        return path
    except Exception as e:  # the reporter must never be the thing that fails
        try:
            print(f"[crashreport failed: {e!r}; original: {kind} {exc!r}]", file=sys.stderr)
        except Exception:
            pass
        return None


def _say_error(text: str):
    try:
        ui.error(text)
    except Exception:
        print(text, file=sys.stderr)


# ---- hooks -------------------------------------------------------------------------------

_orig_excepthook = sys.excepthook
_orig_thread_hook = threading.excepthook


def _excepthook(exc_type, exc, tb):
    try:
        if exc is not None and exc.__traceback__ is None and tb is not None:
            exc = exc.with_traceback(tb)
        report("unhandled", exc, "sys.excepthook")
    except Exception:
        pass
    _orig_excepthook(exc_type, exc, tb)


def _thread_hook(args):
    try:
        report("thread", args.exc_value,
               f"uncaught in thread {getattr(args.thread, 'name', '?')}")
    except Exception:
        pass
    _orig_thread_hook(args)


def loop_handler(loop, context):
    """asyncio exception handler: report real exceptions, then use the default logging."""
    try:
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
            pass  # shutdown noise from a subprocess transport being collected
        elif exc is not None:  # message-only entries ("Task was destroyed but it is pending") are noise
            report("asyncio", exc, msg)
    except Exception:
        pass
    try:
        loop.default_exception_handler(context)
    except Exception:
        pass


def install_loop(loop=None):
    try:
        (loop or asyncio.get_running_loop()).set_exception_handler(loop_handler)
    except Exception as e:
        _say_error(f"Couldn't install the asyncio crash handler ({e}).")


def install(log_dir=None, hooks: bool = True):
    """Enable faulthandler -> logs/fatal.txt (kept open for the process lifetime) and the
    sys/threading excepthooks. Safe to call more than once."""
    global _fatal_file
    try:
        if log_dir is not None:
            configure(log_dir)
        _dir.mkdir(parents=True, exist_ok=True)
        path = _dir / "fatal.txt"
        mode = "w" if path.exists() and path.stat().st_size > FATAL_MAX_BYTES else "a"
        f = open(path, mode, encoding="utf-8")
        f.write(f"\n===== Jarvis started {datetime.now().isoformat(timespec='seconds')} "
                f"(pid {os.getpid()}) =====\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        old, _fatal_file = _fatal_file, f
        if old:
            try:
                old.close()
            except Exception:
                pass
    except Exception as e:
        _say_error(f"Couldn't enable the fatal-crash log ({e}).")
    if hooks:
        sys.excepthook = _excepthook
        threading.excepthook = _thread_hook
