"""Phone push notifications through ntfy (https://ntfy.sh) for real permission prompts.

Notification only: the answer still comes by voice. The topic is a secret kept in a file
(default ~/.jarvis_ntfy_topic, override with JARVIS_NTFY_TOPIC_FILE); it is never logged.
Disable with `notify_phone: false` in config.yaml or JARVIS_NOTIFY_PHONE=0.
push_permission never raises and never blocks: the POST runs in a daemon thread.
"""

import os
import threading
import time
import urllib.request
from pathlib import Path

from . import ui

NTFY_URL = "https://ntfy.sh/"
DEFAULT_TOPIC_FILE = Path.home() / ".jarvis_ntfy_topic"
TITLE = "Jarvis needs permission"
TAGS = "lock"
TIMEOUT_S = 5.0
MAX_CHARS = 200
MIN_INTERVAL_S = 10.0  # at most one push per this long
DEDUPE_S = 60.0  # same summary within this long is dropped

_lock = threading.Lock()
_last_sent = 0.0
_recent: dict[str, float] = {}
last_status: int | None = None  # HTTP status of the most recent send (for diagnostics)


def _enabled() -> bool:
    if os.environ.get("JARVIS_NOTIFY_PHONE", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        from .config import load_config
        return bool(load_config().get("notify_phone", True))
    except Exception:
        return True


def _read_topic() -> str:
    try:
        path = Path(os.environ.get("JARVIS_NTFY_TOPIC_FILE") or DEFAULT_TOPIC_FILE)
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _allow(summary: str) -> bool:
    """Rate limit and dedupe. Records the send when it returns True."""
    global _last_sent
    now = time.monotonic()
    with _lock:
        if _last_sent and now - _last_sent < MIN_INTERVAL_S:
            return False
        for k in [k for k, t in _recent.items() if now - t >= DEDUPE_S]:
            del _recent[k]
        if summary in _recent:
            return False
        _last_sent = now
        _recent[summary] = now
        return True


def _send(topic: str, body: str, title: str = TITLE) -> None:
    global last_status
    try:
        req = urllib.request.Request(
            NTFY_URL + topic,
            data=body.encode("utf-8"),
            method="POST",
            headers={"Title": title, "Priority": "default", "Tags": TAGS},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            last_status = getattr(resp, "status", None)
    except Exception as e:
        # Never include the topic: urllib errors can carry the URL, so log only the type.
        try:
            ui.activity(f"phone notification failed ({type(e).__name__})")
        except Exception:
            pass


def push_permission(summary: str, title: str | None = None) -> "threading.Thread | None":
    """Fire-and-forget push. Returns the sender thread (for tests) or None if nothing was sent."""
    try:
        if not summary or not _enabled():
            return None
        topic = _read_topic()
        if not topic:
            return None
        body = " ".join(str(summary).split())[:MAX_CHARS]
        if not body or not _allow(body):
            return None
        t = threading.Thread(target=_send, args=(topic, body, " ".join(str(title).split()) if title else TITLE),
                             daemon=True, name="ntfy-push")
        t.start()
        return t
    except Exception:
        return None
