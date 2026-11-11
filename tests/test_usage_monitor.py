r"""Offline tests for jarvis/usage_monitor.py. Plain script: run with the venv python.

All paths are redirected to a temp dir; the timer and push are fakes. Nothing real is touched.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import site
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from jarvis import keepawake, ui, usage_monitor as um  # noqa: E402

ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-usage.log"
REAL_FLAG = ROOT / ".jarvis_restart_pending"
NOW = 1_800_000_000.0
TITLES = []


class FakeTimer:
    made = []

    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args = delay, fn, args
        self.daemon = False
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn(*self.args)


ES_CALLS = []
REAL_SET_STATE = keepawake._set_state
REAL_KEEP_AWAKE_ENABLED = um._keep_awake_enabled


def setup():
    keepawake.usage_pending.release()
    ES_CALLS.clear()
    keepawake._set_state = ES_CALLS.append  # never touch the real power state
    d = Path(tempfile.mkdtemp(prefix="jarvis-usage-"))
    um.STATE_FILE = d / "state.json"
    um.FLAG_FILE = d / "flag"
    um.HANDOFF_FILE = d / "HANDOFF.md"
    um.now_fn = lambda: NOW
    um.timer_factory = FakeTimer
    um._timer = None
    FakeTimer.made = []
    pushes = []
    TITLES.clear()
    um.push_fn = lambda msg, title=None: (pushes.append(msg), TITLES.append(title))
    um.sleep_fn = lambda s: None
    um._auto_continue_enabled = lambda: True
    um._notify_enabled = lambda: True
    um._keep_awake_enabled = lambda: True
    return d, pushes


def rej(**kw):
    info = {"type": "five_hour", "status": "rejected", "utilization_pct": 100.0,
            "resets_at": int(NOW + 3600), "seen_at": int(NOW)}
    info.update(kw)
    return info


def state():
    import json
    return json.loads(um.STATE_FILE.read_text(encoding="utf-8"))


def t_arm():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    s = state()
    assert s["type"] == "five_hour" and s["resets_at"] == NOW + 3600 and s["resumed"] is False and s["armed_at"] == NOW
    assert len(FakeTimer.made) == 1 and FakeTimer.made[0].started and FakeTimer.made[0].daemon
    assert FakeTimer.made[0].delay == 3600 + um.MARGIN_S


def t_no_arm():
    d, _ = setup()
    um.on_rate_limit_event(rej(status="allowed"))
    um.on_rate_limit_event(rej(status="allowed_warning"))
    assert not um.STATE_FILE.exists() and not FakeTimer.made and not um.HANDOFF_FILE.exists()


def t_dedupe():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    um.on_rate_limit_event(rej())
    assert len(FakeTimer.made) == 1
    assert um.HANDOFF_FILE.read_text(encoding="utf-8").count("usage limit") == 1


def t_five_hour_fallback():
    d, _ = setup()
    um.on_rate_limit_event(rej(resets_at=None))
    assert state()["resets_at"] == NOW + 5 * 3600 and len(FakeTimer.made) == 1
    d, _ = setup()
    um.on_rate_limit_event(rej(type="seven_day", resets_at=None))
    assert not um.STATE_FILE.exists() and not FakeTimer.made


def t_fire():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    assert um.FLAG_FILE.exists()
    assert state()["resumed"] is True
    assert pushes == [um.RESUME_MSG]
    FakeTimer.made[0].fire()  # a second fire must do nothing
    um.on_rate_limit_event(rej())  # nor may a repeat event re-arm it
    assert pushes == [um.RESUME_MSG] and len(FakeTimer.made) == 1
    um.FLAG_FILE.unlink()
    FakeTimer.made[0].fire()
    assert not um.FLAG_FILE.exists()


def t_push_switch():
    d, pushes = setup()
    um._notify_enabled = lambda: False
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    assert um.FLAG_FILE.exists() and pushes == []


def t_allowed_clears():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    um.on_rate_limit_event(rej(status="allowed"))  # before the reset: kept
    assert um.STATE_FILE.exists()
    um.now_fn = lambda: NOW + 4000
    um.on_rate_limit_event(rej(status="allowed", type="seven_day"))  # other type: kept
    assert um.STATE_FILE.exists()
    um.on_rate_limit_event(rej(status="allowed"))
    assert not um.STATE_FILE.exists() and FakeTimer.made[0].cancelled


def t_startup_past():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    um.now_fn = lambda: NOW + 7200
    FakeTimer.made.clear()
    um.on_startup()
    assert not um.STATE_FILE.exists() and pushes == [um.BACK_MSG]
    assert not um.FLAG_FILE.exists() and not FakeTimer.made


def t_startup_future():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made.clear()
    um.now_fn = lambda: NOW + 600
    um.on_startup()
    assert len(FakeTimer.made) == 1 and FakeTimer.made[0].delay == 3600 + um.MARGIN_S - 600
    assert um.STATE_FILE.exists() and pushes == []


def t_startup_corrupt_and_missing():
    d, pushes = setup()
    um.on_startup()  # no file: nothing
    um.STATE_FILE.write_text("{not json", encoding="utf-8")
    um.on_startup()
    assert not um.STATE_FILE.exists() and not pushes and not FakeTimer.made
    um.STATE_FILE.write_text('{"resets_at": "x"}', encoding="utf-8")
    um.on_startup()
    assert not um.STATE_FILE.exists()


def t_startup_resumed_noop():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    pushes.clear()
    FakeTimer.made.clear()
    um.FLAG_FILE.unlink()
    um.now_fn = lambda: NOW + 7200
    um.on_startup()
    assert not pushes and not FakeTimer.made and not um.FLAG_FILE.exists()


def t_handoff_missing_dir():
    d, _ = setup()
    um.HANDOFF_FILE = d / "nope" / "HANDOFF.md"
    um.on_rate_limit_event(rej())
    assert um.STATE_FILE.exists()


def t_handoff_line():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    text = um.HANDOFF_FILE.read_text(encoding="utf-8")
    assert "usage limit (five_hour) hit; resets" in text and "open-questions.md" in text and text.endswith("\n")


def two_rejected():
    um.on_rate_limit_event(rej(type="seven_day", resets_at=int(NOW + 90000)))
    um.on_rate_limit_event(rej())  # five_hour, sooner


def t_soonest_of_two():
    d, _ = setup()
    two_rejected()
    s = state()
    assert s["type"] == "five_hour" and s["resets_at"] == NOW + 3600 and set(s["types"]) == {"five_hour", "seven_day"}
    assert FakeTimer.made[-1].delay == 3600 + um.MARGIN_S and FakeTimer.made[-2].cancelled
    d, _ = setup()  # later-reset event arriving second does not move the timer
    um.on_rate_limit_event(rej())
    um.on_rate_limit_event(rej(type="seven_day", resets_at=int(NOW + 90000)))
    assert len(FakeTimer.made) == 1 and state()["type"] == "five_hour"


def t_rearm_when_earlier_rejected_clears():
    d, pushes = setup()
    two_rejected()
    um.now_fn = lambda: NOW + 4000  # past the five-hour reset
    um.on_rate_limit_event(rej(status="allowed"))
    s = state()
    assert s["type"] == "seven_day" and s["resets_at"] == NOW + 90000 and list(s["types"]) == ["seven_day"]
    t = FakeTimer.made[-1]
    assert t.delay == 90000 + um.MARGIN_S - 4000 and not t.cancelled and FakeTimer.made[-2].cancelled
    t.fire()
    assert um.FLAG_FILE.exists() and pushes == [um.RESUME_MSG]


def t_fire_marker_flag_and_titled_push():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    s = state()
    assert s["continue_pending"] is True and s["continue_reset"] == NOW + 3600 and s["created_at"] == NOW
    assert um.FLAG_FILE.exists() and pushes == [um.RESUME_MSG] and TITLES == ["Jarvis: usage limit reset"]
    FakeTimer.made[0].fire()
    assert len(pushes) == 1


def t_no_refire_after_consumed():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    assert um.pending_continue_prompt() == um.CONTINUE_PROMPT
    um.FLAG_FILE.unlink()
    um.on_rate_limit_event(rej())  # same reset again: no re-arm, no fire
    um.on_startup()
    assert len(FakeTimer.made) == 1 and len(pushes) == 1 and not um.FLAG_FILE.exists()
    assert um.pending_continue_prompt() is None


def pending(age=10.0, **extra):
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    um.now_fn = lambda: NOW + 3600 + age
    return pushes


def t_startup_submits_one_prompt_and_clears_first():
    pending()
    seen = []

    def submit(text):
        seen.append((text, state()["continue_pending"]))  # marker must already be cleared here

    th = um.schedule_auto_continue(submit, delay_s=0)
    th.join(5)
    assert seen == [(um.CONTINUE_PROMPT, False)]
    um.schedule_auto_continue(submit, delay_s=0).join(5)
    assert len(seen) == 1  # a second startup does not repeat it
    assert "HANDOFF.md" in um.CONTINUE_PROMPT and "blocked.md" in um.CONTINUE_PROMPT


def t_stale_pending_discarded():
    pending(age=um.CONTINUE_MAX_AGE_S + 60)
    seen = []
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert seen == [] and state()["continue_pending"] is False


def t_switch_off_no_submit():
    pending()
    um._auto_continue_enabled = lambda: False
    seen = []
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert seen == [] and state()["continue_pending"] is False


def t_submit_failure_not_fatal():
    pending()

    def boom(text):
        raise RuntimeError("brain not ready")

    th = um.schedule_auto_continue(boom, delay_s=0)
    th.join(5)
    assert not th.is_alive() and state()["continue_pending"] is False
    seen = []
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert seen == []


def t_no_marker_no_submit():
    d, _ = setup()
    seen = []
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert seen == []


def t_garbage():
    d, _ = setup()
    for bad in (None, 5, "x", [], {}, {"status": "rejected"}, {"status": "rejected", "type": "five_hour", "resets_at": "soon"},
                {"status": "rejected", "type": "seven_day", "resets_at": True}, {"status": object()}):
        um.on_rate_limit_event(bad)
    um.on_startup()


def t_fallback_never_overrides_real_reset():
    d, _ = setup()
    um.on_rate_limit_event(rej())  # real five_hour reset
    um.on_rate_limit_event(rej(resets_at=None))  # bare-429 fallback, repeated
    um.on_rate_limit_event(rej(resets_at=None))
    assert state()["resets_at"] == NOW + 3600 and len(FakeTimer.made) == 1
    assert um.HANDOFF_FILE.read_text(encoding="utf-8").count("usage limit") == 1


def t_fallback_arms_once_when_nothing_armed():
    d, _ = setup()
    um.on_rate_limit_event(rej(resets_at=None))
    um.on_rate_limit_event(rej(resets_at=None))
    assert state()["resets_at"] == NOW + 5 * 3600 and len(FakeTimer.made) == 1
    assert um.HANDOFF_FILE.read_text(encoding="utf-8").count("usage limit") == 1


def t_fallback_ignored_when_other_type_armed_or_restart_in_flight():
    d, _ = setup()
    um.on_rate_limit_event(rej(type="seven_day", resets_at=int(NOW + 90000)))
    um.on_rate_limit_event(rej(resets_at=None))  # something is armed: a guess must not add anything
    assert state()["type"] == "seven_day" and len(FakeTimer.made) == 1
    d, _ = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()  # flag + marker written, restart on its way
    um.now_fn = lambda: NOW + 3700
    um.on_rate_limit_event(rej(resets_at=None))
    assert len(FakeTimer.made) == 1 and state()["resumed"] is True


def t_same_reset_other_type_no_second_restart():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    FakeTimer.made[0].fire()
    um.FLAG_FILE.unlink()
    um.now_fn = lambda: NOW + 3700
    um.on_rate_limit_event(rej(type="seven_day"))  # same reset time, different type
    um.on_rate_limit_event(rej(type="unknown"))
    assert len(FakeTimer.made) == 1 and not um.FLAG_FILE.exists() and pushes == [um.RESUME_MSG]
    # also while still armed (before fire): same reset under another type keeps one timer / one line
    d, _ = setup()
    um.on_rate_limit_event(rej())
    um.on_rate_limit_event(rej(type="seven_day"))
    assert len(FakeTimer.made) == 1
    assert um.HANDOFF_FILE.read_text(encoding="utf-8").count("usage limit") == 1


def t_startup_past_keeps_continue_marker():
    d, pushes = setup()
    um.on_rate_limit_event(rej())
    s = state()
    s["continue_pending"], s["created_at"] = True, NOW  # carried over from an earlier fire
    um.STATE_FILE.write_text(__import__("json").dumps(s), encoding="utf-8")
    um.now_fn = lambda: NOW + 7200
    FakeTimer.made.clear()
    um.on_startup()
    assert um.STATE_FILE.exists() and state()["continue_pending"] is True and state()["resumed"] is True
    assert not FakeTimer.made and not um.FLAG_FILE.exists()
    seen = []
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert seen == [um.CONTINUE_PROMPT]
    um.on_startup()  # never loops
    um.schedule_auto_continue(seen.append, delay_s=0).join(5)
    assert len(seen) == 1


def t_keepawake_held_while_pending():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    assert keepawake.usage_pending.active
    assert ES_CALLS == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]
    um.on_rate_limit_event(rej(resets_at=int(NOW + 1800)))  # re-armed: still held, not re-acquired
    assert keepawake.usage_pending.active and len(ES_CALLS) == 1
    FakeTimer.made[-1].fire()
    assert not keepawake.usage_pending.active
    assert ES_CALLS[-1] == keepawake.ES_CONTINUOUS


def t_keepawake_is_opt_in_off_means_no_hold():
    d, _ = setup()
    um._keep_awake_enabled = lambda: False
    um.on_rate_limit_event(rej())
    assert not keepawake.usage_pending.active and ES_CALLS == []
    assert FakeTimer.made and FakeTimer.made[-1].started  # the restart timer itself still runs


def t_keepawake_config_flag_defaults_off():
    import jarvis.config as cfg
    real = cfg.load_config
    try:
        cfg.load_config = lambda *a, **k: {}
        assert REAL_KEEP_AWAKE_ENABLED() is False  # key missing = off
        cfg.load_config = lambda *a, **k: {"keep_awake_while_waiting": True}
        assert REAL_KEEP_AWAKE_ENABLED() is True
        cfg.load_config = lambda *a, **k: {"keep_awake_while_waiting": False}
        assert REAL_KEEP_AWAKE_ENABLED() is False

        def boom(*a, **k):
            raise RuntimeError("no config")
        cfg.load_config = boom
        assert REAL_KEEP_AWAKE_ENABLED() is False  # unreadable config = off
    finally:
        cfg.load_config = real


def t_keepawake_released_when_cleared():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    assert keepawake.usage_pending.active
    um.now_fn = lambda: NOW + 7200
    um.on_rate_limit_event({"type": "five_hour", "status": "allowed"})
    assert not keepawake.usage_pending.active
    assert ES_CALLS[-1] == keepawake.ES_CONTINUOUS


def t_keepawake_stale_fire_keeps_hold():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    um._fire(NOW + 1)  # a fire for some other reset time must not release the current wait
    assert keepawake.usage_pending.active


def t_keepawake_never_raises():
    setup()
    keepawake.usage_pending.release()
    keepawake.usage_pending.release()  # idempotent

    def boom(flags):
        raise OSError("nope")

    keepawake._set_state = boom
    k = keepawake.KeepAwake()
    k.acquire()
    k.release()
    assert not k.active


def t_keepawake_win32_call_uses_ctypes():
    import ctypes
    import types
    calls = []
    fake = types.SimpleNamespace(
        kernel32=types.SimpleNamespace(SetThreadExecutionState=lambda v: calls.append(v.value)))
    orig_win, orig_sys = getattr(ctypes, "windll", None), keepawake.sys
    try:
        ctypes.windll = fake
        keepawake.sys = types.SimpleNamespace(platform="win32")
        REAL_SET_STATE(keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED)
        keepawake.sys = types.SimpleNamespace(platform="linux")
        REAL_SET_STATE(1)  # off Windows: a no-op
    finally:
        keepawake.sys = orig_sys
        if orig_win is None:
            del ctypes.windll
        else:
            ctypes.windll = orig_win
    assert calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]


def t_keepawake_released_when_write_state_raises():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    assert keepawake.usage_pending.active
    real = um._write_state

    def boom(state):
        raise OSError("disk full")

    um._write_state = boom
    try:
        FakeTimer.made[-1].fire()
    finally:
        um._write_state = real
    assert not keepawake.usage_pending.active


def t_keepawake_released_when_state_missing_or_resumed():
    d, _ = setup()
    um.on_rate_limit_event(rej())
    um._delete_state()
    FakeTimer.made[-1].fire()  # state gone
    assert not keepawake.usage_pending.active
    d, _ = setup()
    um.on_rate_limit_event(rej())
    st = um._read_state()
    st["resumed"] = True
    um._write_state(st)
    FakeTimer.made[-1].fire()  # already handled
    assert not keepawake.usage_pending.active


def t_keepawake_released_when_timer_creation_fails():
    d, _ = setup()

    def bad_factory(delay, fn, args=()):
        raise RuntimeError("no threads")

    um.timer_factory = bad_factory
    try:
        um._arm_timer(NOW + 3600)
    except RuntimeError:
        pass
    assert not keepawake.usage_pending.active
    assert um._timer is None
    um.timer_factory = FakeTimer


def cleanup_real_files():
    keepawake.usage_pending.release()
    for f in (REAL_FLAG, ROOT / ".jarvis_usage_reset.json"):
        if f.exists():
            f.unlink()


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
    cleanup_real_files()
    print(f"{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    main()
