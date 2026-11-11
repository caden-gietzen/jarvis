"""Shared reader/writer for .jarvis_state.json (session id, project dir, voice_muted).

Several threads write different keys of the same file, so every update is a read-modify-write under one
module-level lock, and the file is replaced atomically (temp file in the same folder, then os.replace)."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def _read_once(p: Path) -> dict | None:
    """The parsed dict, {} if the file is absent, None if it couldn't be read or parsed."""
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_locked(p: Path) -> dict:
    data = _read_once(p)
    if data is None:
        data = _read_once(p)  # retry once: a failed read must not drop keys of a valid file
    return data if data is not None else {}


def read_state(path) -> dict:
    """The state dict; {} if absent or corrupt. A failed read or parse is retried once."""
    with _LOCK:  # also keeps readers out of the way of os.replace (Windows refuses it on an open file)
        return _read_locked(Path(path))


def _replace(tmp: str, p: Path) -> None:
    for attempt in range(20):  # a reader in another process can briefly hold the file open on Windows
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def update_state(path, **updates) -> dict:
    """Merge `updates` into the state file atomically and return the new state. May raise OSError."""
    p = Path(path)
    with _LOCK:
        state = {**_read_locked(p), **updates}
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(state))
            _replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return state
