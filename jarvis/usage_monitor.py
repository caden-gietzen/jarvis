"""Resume Jarvis by itself after the Claude subscription usage limit resets.

Plain Python, no model calls (the model cannot think while rate-limited). brain.py reports every
rate-limit event to on_rate_limit_event(). Rejected limit types are tracked per type; the timer is
armed for the SOONEST reset among them (+ a small margin) and re-armed when that changes. When it
fires: a "continue pending" marker is written to the state, the restart flag file is created (the
main loop restarts Jarvis when it next enters follow-up listening, see restart.py) and one phone
push goes out. On the next startup schedule_auto_continue() clears the marker and submits one
"continue the handoff" prompt to the brain. State lives in .jarvis_usage_reset.json so a manual
restart while limited still resumes later (on_startup re-arms). A restart is never repeated for the
same reset time. If the weekly limit is the blocking one, the 5-hour reset will not unblock: Jarvis
simply hits the limit again, a new reset is armed, and nothing loops (one restart per distinct reset).
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from . import keepawake, notify, restart, ui
from .config import WORKSPACE_ROOT

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".jarvis_usage_reset.json"
FLAG_FILE = restart.PENDING_FILE
HANDOFF_FILE = WORKSPACE_ROOT / "HANDOFF.md"
MARGIN_S = 120.0  # wait this long past the reset before restarting
FIVE_HOUR_S = 5 * 3600
RESUME_MSG = "Usage limit reset. Restarting Jarvis to resume."
BACK_MSG = "Usage limit reset; Jarvis is back."
PUSH_TITLE = "Jarvis: usage limit reset"
CONTINUE_MAX_AGE_S = 6 * 3600  # an older continue marker is discarded, not acted on
AUTO_CONTINUE_DELAY_S = 8.0  # let the brain finish starting before the prompt goes in
CONTINUE_PROMPT = (
    "The usage limit has reset. Continue where we left off: read "
    f"{WORKSPACE_ROOT / 'HANDOFF.md'} (newest entries at the bottom), open-questions.md and "
    "future-tasks.md, resume any unfinished work or stopped subagents, follow the away rules in "
    f"HANDOFF.md (stay inside {WORKSPACE_ROOT.parent}, never push; local commits only if the away-mode rules allow them, log blocked items to blocked.md), and report briefly "
    "when done."
)

# Test hooks: replace these (or pass nothing and patch the module attributes).
now_fn = time.time
timer_factory = threading.Timer
push_fn = notify.push_permission
sleep_fn = time.sleep

_lock = threading.RLock()
_timer = None


def _log(msg: str) -> None:
    try:
        ui.activity(msg)
    except Exception:
        pass


def _fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


def _cfg_flag(key: str, default: bool = True) -> bool:
    try:
        from .config import load_config
        return bool(load_config().get(key, default))
    except Exception:
        return default


def _notify_enabled() -> bool:
    return _cfg_flag("notify_on_usage_resume")


def _auto_continue_enabled() -> bool:
    return _cfg_flag("auto_continue_after_limit")


def _keep_awake_enabled() -> bool:
    """Opt-in: hold the PC awake while waiting for a usage limit to reset. Off unless config.yaml
    says keep_awake_while_waiting: true (a missing key or an unreadable config means off)."""
    return _cfg_flag("keep_awake_while_waiting", default=False)


def _push(msg: str) -> None:
    try:
        if _notify_enabled():
            push_fn(msg, title=PUSH_TITLE)
    except Exception:
        pass


def _read_state():
    """The state dict, None if there is none, or False if the file is corrupt."""
    try:
        data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("resets_at"), (int, float)) \
            or isinstance(data.get("resets_at"), bool):
        return False
    return data


def _write_state(state: dict) -> None:
    path = Path(STATE_FILE)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _delete_state() -> None:
    try:
        Path(STATE_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def _cancel_timer(release: bool = True) -> None:
    global _timer
    if _timer is not None:
        try:
            _timer.cancel()
        except Exception:
            pass
        _timer = None
    if release:
        keepawake.usage_pending.release()


def _arm_timer(resets_at: float) -> None:
    global _timer
    _cancel_timer(release=False)
    if _keep_awake_enabled():
        keepawake.usage_pending.acquire()  # a sleeping PC would never fire the timer
    try:
        delay = max(0.0, float(resets_at) + MARGIN_S - now_fn())
        t = timer_factory(delay, _fire, args=(float(resets_at),))
        t.daemon = True
        _timer = t
        t.start()
    except BaseException:
        _timer = None
        keepawake.usage_pending.release()
        raise


def _types(state) -> dict:
    t = state.get("types") if isinstance(state, dict) else None
    return dict(t) if isinstance(t, dict) else {}


def _earliest(types: dict):
    """(kind, resets_at) of the soonest reset among the tracked (rejected) types, or None."""
    best = None
    for kind, info in types.items():
        try:
            ra = float(info["resets_at"])
        except Exception:
            continue
        if best is None or ra < best[1]:
            best = (kind, ra)
    return best


def _owns_hold(resets_at: float) -> bool:
    """True if this fire belongs to the armed timer, or no timer is armed (a stale fire for a
    different reset must not release a newer pending timer's hold)."""
    t = _timer
    if t is None:
        return True
    try:
        return float(t.args[0]) == float(resets_at)
    except Exception:
        return True


def _fire(resets_at: float) -> None:
    """Timer callback: marker + restart flag + push, once for this reset time. Never raises."""
    try:
        with _lock:
            try:
                state = _read_state()
                if not state or state.get("resumed") or float(state["resets_at"]) != float(resets_at):
                    return  # cleared, or this reset was already handled: never restart twice
                state["resumed"] = True  # first, so a failure below can't cause a restart loop
                state["continue_pending"] = True
                state["continue_reset"] = float(resets_at)
                state["created_at"] = float(now_fn())
                _write_state(state)
                try:
                    restart.request_restart(Path(FLAG_FILE), "usage-limit reset")
                except Exception as e:
                    _log(f"Usage limit reset but the restart flag could not be written ({type(e).__name__}).")
                    return
                _log("Usage limit reset; restart requested.")
                _push(RESUME_MSG)
            finally:
                if _owns_hold(resets_at):
                    keepawake.usage_pending.release()  # the wait is over, however it ended
    except Exception:
        pass


def _append_handoff(kind: str, resets_at: float) -> None:
    try:
        line = (f"{datetime.now():%Y-%m-%d %H:%M}: usage limit ({kind}) hit; resets {_fmt(resets_at)}; "
                "Jarvis will restart itself at the reset and continue by itself (jarvis/usage_monitor.py). "
                "Read open-questions.md and the queue, resume unfinished work.\n")
        with Path(HANDOFF_FILE).open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _on_allowed(kind: str) -> None:
    state = _read_state()
    if not state:
        return
    types = _types(state)
    cur = types.get(kind)
    if cur is None:
        if "types" in state or state.get("type") != kind:
            return
        reset = state["resets_at"]  # legacy state without per-type info
    else:
        reset = cur["resets_at"]
    if now_fn() < float(reset):
        return  # allowed before that limit's reset: not yet trusted
    types.pop(kind, None)
    best = _earliest(types)
    if best is None:
        _cancel_timer()
        if state.get("continue_pending"):  # keep the marker for the next startup
            state["types"] = {}
            _write_state(state)
        else:
            _delete_state()
        _log("Usage limit cleared.")
        return
    state.update(types=types, type=best[0], resets_at=best[1], resumed=False, armed_at=float(now_fn()))
    _write_state(state)
    _arm_timer(best[1])
    _log(f"Usage limit ({kind}) cleared; next reset {_fmt(best[1])} ({best[0]}).")


def on_rate_limit_event(info: dict) -> None:
    """Called by brain.py for every rate-limit event. Never raises."""
    try:
        if not isinstance(info, dict):
            return
        kind = str(info.get("type") or "unknown")
        status = info.get("status")
        if info.get("source") in ("statusline", "sdk-unified"):
            # Statusline / unifiedWindows readings never arm or clear anything by themselves; only a rejected one that carries
            # a real reset time may arm (a percent >= 100). Otherwise leave it to the SDK's rejected event.
            if status != "rejected" or isinstance(info.get("resets_at"), bool) \
                    or not isinstance(info.get("resets_at"), (int, float)):
                return
        with _lock:
            if status == "allowed":
                _on_allowed(kind)
                return
            if status != "rejected":
                return
            armed_at = float(now_fn())
            resets_at = info.get("resets_at")
            if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
                resets_at = None
            state = _read_state() or {}
            fallback = resets_at is None
            if fallback:
                if kind != "five_hour":
                    _log(f"Usage limit ({kind}) hit, reset time unknown; not scheduling a restart.")
                    return
                # A guess (bare 429). Only used when nothing else is armed, so it can never replace a
                # real reset time nor re-arm / re-log on every repeated 429.
                try:
                    armed = (not state.get("resumed") and "resets_at" in state
                             and float(state["resets_at"]) + MARGIN_S > armed_at)
                    in_flight = bool(state.get("resumed")) and bool(state.get("continue_pending")) \
                        and Path(FLAG_FILE).exists()  # a restart is already on its way
                except Exception:
                    armed, in_flight = False, False
                if armed or in_flight:
                    return
                resets_at = armed_at + FIVE_HOUR_S
            if state.get("continue_reset") is not None and float(resets_at) == state.get("continue_reset"):
                return  # this reset was already acted on (whatever type reports it)
            types = {k: v for k, v in _types(state).items()
                     if isinstance(v, dict) and isinstance(v.get("resets_at"), (int, float))
                     and float(v["resets_at"]) + MARGIN_S > armed_at}  # drop limits already over
            types[kind] = {"status": "rejected", "resets_at": float(resets_at)}
            kind_t, target = _earliest(types)
            if state.get("resets_at") == target:
                state["types"] = types  # same soonest reset (any type): keep the timer, remember the types
                _write_state(state)
                return
            new = {k: v for k, v in state.items() if k in ("continue_pending", "continue_reset", "created_at")}
            new.update(type=kind_t, resets_at=target, armed_at=armed_at, resumed=False, types=types)
            _write_state(new)
            _arm_timer(target)
            _append_handoff(kind_t, target)
            _log(f"Usage limit ({kind_t}) hit; Jarvis will restart itself after {_fmt(target)}.")
    except Exception:
        pass


def on_startup() -> None:
    """Call once at startup. Non-fatal: handles state left by a previous run."""
    try:
        with _lock:
            state = _read_state()
            if state is None:
                return
            if state is False:
                _delete_state()
                _log("Usage state file was corrupt; removed it.")
                return
            if state.get("resumed"):
                return
            resets_at = float(state["resets_at"])
            if resets_at <= now_fn():
                state["resumed"] = True
                _write_state(state)
                _push(BACK_MSG)
                if not state.get("continue_pending"):
                    _delete_state()  # with a carried-over continue marker the state stays so it still fires
                _log("Usage limit has reset; Jarvis is back.")
            else:
                _arm_timer(resets_at)
                _log(f"Usage limit still active; will restart after {_fmt(resets_at)}.")
    except Exception:
        pass


def pending_continue_prompt() -> "str | None":
    """The prompt to submit if a fresh continue marker exists, else None. The marker is cleared FIRST
    (also when stale or when auto-continue is off), so this can never loop. Never raises."""
    try:
        with _lock:
            state = _read_state()
            if not state or not state.get("continue_pending"):
                return None
            created = state.get("created_at")
            state["continue_pending"] = False
            _write_state(state)  # cleared before anything else happens
            if isinstance(created, bool) or not isinstance(created, (int, float)) \
                    or not (0 <= now_fn() - float(created) <= CONTINUE_MAX_AGE_S):
                _log("Discarded a stale usage-limit continue marker.")
                return None
            if not _auto_continue_enabled():
                _log("Usage limit reset; auto-continue is off.")
                return None
            return CONTINUE_PROMPT
    except Exception:
        return None


def continue_pending() -> bool:
    """True if a fresh usage-limit continue marker will make schedule_auto_continue send its prompt.
    Read-only (does not clear anything). Never raises."""
    try:
        with _lock:
            state = _read_state()
            if not state or not state.get("continue_pending"):
                return False
            created = state.get("created_at")
            return (not isinstance(created, bool) and isinstance(created, (int, float))
                    and 0 <= now_fn() - float(created) <= CONTINUE_MAX_AGE_S
                    and _auto_continue_enabled())
    except Exception:
        return False


def schedule_auto_continue(submit, delay_s: "float | None" = None) -> "threading.Thread | None":
    """Start a daemon thread that waits a few seconds (brain readiness), then submits the continue
    prompt through submit(text) if a fresh marker exists. Never raises, never blocks the caller."""
    def run():
        try:
            sleep_fn(AUTO_CONTINUE_DELAY_S if delay_s is None else delay_s)
            prompt = pending_continue_prompt()
            if prompt is None:
                return
            try:
                submit(prompt)
                _log("Usage limit reset: continuing the handoff automatically.")
            except Exception as e:
                _log(f"Auto-continue could not submit its prompt ({type(e).__name__}).")
        except Exception:
            pass

    try:
        t = threading.Thread(target=run, daemon=True, name="usage-auto-continue")
        t.start()
        return t
    except Exception:
        return None
