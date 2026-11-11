"""Subscription usage via the Claude Code statusline feature.

The CLI runs scripts/statusline_capture.py (configured through ClaudeAgentOptions(settings=...)), which
writes logs/statusline_usage.json. This module builds that settings string, reads the file, and polls it,
turning changes into usage events shaped like brain.usage_from_info().
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "statusline_capture.py"
DEFAULT_FILE = ROOT / "logs" / "statusline_usage.json"
STALE_SECONDS = 15 * 60
POLL_SECONDS = 5.0
WINDOWS = ("five_hour", "seven_day")


def usage_file() -> Path:
    return Path(os.environ.get("JARVIS_STATUSLINE_FILE") or DEFAULT_FILE)


def statusline_command(python: str | None = None, script: str | Path | None = None) -> str:
    """`"<python>" "<script>"` with forward slashes so it works under both cmd and Git Bash."""
    py = Path(python or sys.executable).as_posix()
    sc = Path(script or SCRIPT).as_posix()
    return f'"{py}" "{sc}"'


def settings_json(command: str | None = None) -> str:
    """Inline JSON for ClaudeAgentOptions(settings=...); leaves the user's global settings alone."""
    return json.dumps({"statusLine": {"type": "command", "command": command or statusline_command()}})


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return v


def read_usage(path=None, now: float | None = None, stale_seconds: float = STALE_SECONDS) -> dict | None:
    """{five_hour: {utilization_pct, resets_at}, seven_day: {...}, updated: ts} or None.

    The file is stale when older than stale_seconds, unless a window's reset time is still in the
    future. Windows whose reset time has passed are dropped. Never raises."""
    try:
        now = time.time() if now is None else now
        with open(path or usage_file(), "r", encoding="utf-8") as f:
            doc = json.load(f)
        updated = _num(doc.get("updated"))
        if updated is None:
            return None
        rl = doc.get("rate_limits")
        if not isinstance(rl, dict):
            return None
        out: dict = {}
        for key in WINDOWS:
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            pct = _num(w.get("used_percentage"))
            if pct is None:
                continue
            resets = _num(w.get("resets_at"))
            resets = int(resets) if resets is not None else None
            if resets is not None and resets <= now:
                continue  # that window has rolled over
            if resets is None and now - updated > stale_seconds:
                continue
            out[key] = {"utilization_pct": round(max(0.0, min(100.0, float(pct))), 1), "resets_at": resets}
        if not out:
            return None
        out["updated"] = int(updated)
        return out
    except Exception:
        return None


def status_for(pct: float) -> str:
    return "rejected" if pct >= 100 else "allowed_warning" if pct >= 80 else "allowed"


def usage_from_statusline(kind: str, window: dict, updated=None) -> dict:
    """Same shape as brain.usage_from_info(), plus source."""
    pct = float(window["utilization_pct"])
    return {"type": kind, "status": status_for(pct), "utilization_pct": pct,
            "resets_at": window.get("resets_at"), "seen_at": int(updated or time.time()),
            "source": "statusline"}


class UsagePoller:
    """Polls the capture file every `interval` seconds; calls callback(usage_dict) when a window changes."""

    def __init__(self, callback, path=None, interval: float = POLL_SECONDS):
        self._cb = callback
        self._path = path
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict = {}
        self._mtime = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="statusline-usage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=2)

    def poll_once(self) -> None:
        try:
            p = self._path or usage_file()
            try:
                mtime = os.stat(p).st_mtime_ns
            except OSError:
                return
            if mtime == self._mtime:
                return
            self._mtime = mtime
            data = read_usage(p)
            if not data:
                return
            for key in WINDOWS:
                w = data.get(key)
                if not w:
                    continue
                sig = (w["utilization_pct"], w["resets_at"])
                if self._last.get(key) == sig:
                    continue
                self._last[key] = sig
                try:
                    self._cb(usage_from_statusline(key, w, data.get("updated")))
                except Exception:
                    pass
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._interval)
