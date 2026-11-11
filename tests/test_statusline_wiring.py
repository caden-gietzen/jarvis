r"""Offline tests for wiring the statusline usage source into brain / events / usage_monitor.

Plain script (PASS/FAIL per test, non-zero exit on failure):  .venv\Scripts\python.exe tests\test_statusline_wiring.py
Nothing real is touched: temp state/flag/statusline files, fake timer and push, SDK classes mocked.
"""
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # never send a real phone push
_os_np.environ.setdefault("JARVIS_TOKEN_LEDGER", "0")  # never write the real token ledger
import asyncio
import importlib
import json
import os
import site
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import statusline_usage as su  # noqa: E402
from jarvis import ui, usage_monitor as um  # noqa: E402
from jarvis.events import EventBus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-slwire-"))
ui._log_path = TMP / "test.log"
ui.error = ui.status = lambda text: None
brain_mod.STATE_PATH = TMP / "state.json"
brain_mod.RATE_LIMIT_LOG = TMP / "rate_limits.jsonl"
os.environ["JARVIS_STATUSLINE_FILE"] = str(TMP / "statusline_usage.json")  # never the real capture file
NOW = 1_800_000_000.0
FUTURE = int(time.time()) + 3600


class env:
    """Set/unset an env var for a block."""
    def __init__(self, key, val):
        self.key, self.val = key, val

    def __enter__(self):
        self.old = os.environ.get(self.key)
        if self.val is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.val

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self.old


def sl(pct, resets=FUTURE, kind="five_hour", seen=None):
    return {"type": kind, "status": su.status_for(pct), "utilization_pct": float(pct), "resets_at": resets,
            "seen_at": int(seen or time.time()), "source": "statusline"}


def sdk(status="allowed", pct=None, resets=FUTURE, kind="five_hour"):
    return {"type": kind, "status": status, "utilization_pct": pct, "resets_at": resets, "seen_at": int(time.time())}


def usage_of(bus, kind="five_hour"):
    return bus.get_state()["usage"].get(kind)


# ---- options construction ----

class FakeOptions:
    last = None

    def __init__(self, **kw):
        self.kw = kw
        FakeOptions.last = kw


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.connected = False

    async def connect(self):
        self.connected = True


def make_brain():
    saved = (brain_mod.ClaudeAgentOptions, brain_mod.ClaudeSDKClient)
    brain_mod.ClaudeAgentOptions, brain_mod.ClaudeSDKClient = FakeOptions, FakeClient
    try:
        return brain_mod.Brain(str(TMP))
    finally:
        brain_mod.ClaudeAgentOptions, brain_mod.ClaudeSDKClient = saved


def test_options_have_settings_when_enabled():
    with env("JARVIS_STATUSLINE_USAGE", "1"):
        make_brain()
    s = FakeOptions.last["settings"]
    assert isinstance(s, str)
    doc = json.loads(s)
    assert doc["statusLine"]["type"] == "command" and "statusline_capture.py" in doc["statusLine"]["command"]
    assert list(doc) == ["statusLine"]  # nothing else leaks into the per-session settings
    assert FakeOptions.last["setting_sources"] is None  # unchanged


def test_options_have_no_settings_by_default():
    import jarvis.config as cfg
    saved = cfg.load_config
    try:
        cfg.load_config = lambda *a, **k: {}
        with env("JARVIS_STATUSLINE_USAGE", None):
            make_brain()
        assert FakeOptions.last["settings"] is None
    finally:
        cfg.load_config = saved


def test_options_have_no_settings_when_disabled_by_env():
    for v in ("0", "false", "off"):
        with env("JARVIS_STATUSLINE_USAGE", v):
            make_brain()
        assert FakeOptions.last["settings"] is None


def test_config_switch_disables():
    import jarvis.config as cfg
    saved = cfg.load_config
    try:
        cfg.load_config = lambda *a, **k: {"statusline_usage": False}
        with env("JARVIS_STATUSLINE_USAGE", None):
            assert brain_mod.statusline_enabled() is False and brain_mod.statusline_settings() is None
        cfg.load_config = lambda *a, **k: {"statusline_usage": True}
        with env("JARVIS_STATUSLINE_USAGE", None):
            assert brain_mod.statusline_enabled() is True
        cfg.load_config = lambda *a, **k: {}  # key absent: off
        with env("JARVIS_STATUSLINE_USAGE", None):
            assert brain_mod.statusline_enabled() is False
        with env("JARVIS_STATUSLINE_USAGE", "1"):
            assert brain_mod.statusline_enabled() is True  # env can enable
        def boom(*a, **k): raise OSError("no config")
        cfg.load_config = boom
        with env("JARVIS_STATUSLINE_USAGE", None):
            assert brain_mod.statusline_enabled() is False  # broken config never breaks startup
    finally:
        cfg.load_config = saved


# ---- poller -> publish_usage ----

def test_poller_publishes_usage_events_with_source():
    path = TMP / "poll.json"
    path.write_text(json.dumps({"updated": int(time.time()), "rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": FUTURE}}}), encoding="utf-8")
    got = []
    mod = importlib.import_module("jarvis.usage_monitor")
    saved_hook, saved_emit = mod.on_rate_limit_event, brain_mod.bus.emit
    hook_calls = []
    mod.on_rate_limit_event = lambda info: hook_calls.append(info)
    brain_mod.bus.emit = lambda kind, **d: got.append((kind, d))
    try:
        p = su.UsagePoller(lambda u: brain_mod.publish_usage(u), path=path)
        p.poll_once()
    finally:
        mod.on_rate_limit_event, brain_mod.bus.emit = saved_hook, saved_emit
    assert len(got) == 1 and got[0][0] == "usage"
    d = got[0][1]
    assert d["source"] == "statusline" and d["utilization_pct"] == 42.0 and d["type"] == "five_hour"
    assert len(hook_calls) == 1 and hook_calls[0]["source"] == "statusline"
    assert not (TMP / "rate_limits.jsonl").exists()  # no raw_info: nothing extra logged


# ---- merge rule in the event reducer ----

def test_merge_allowed_null_keeps_statusline_pct_same_reset():
    bus = EventBus()
    bus.emit("usage", **sl(41))
    bus.emit("usage", **sdk("allowed", None, FUTURE))
    u = usage_of(bus)
    assert u["utilization_pct"] == 41.0 and u["source"] == "statusline" and u["resets_at"] == FUTURE


def test_merge_allowed_null_no_resets_keeps_when_fresh():
    bus = EventBus()
    bus.emit("usage", **sl(41))
    bus.emit("usage", **sdk("allowed", None, None))
    assert usage_of(bus)["utilization_pct"] == 41.0


def test_merge_not_kept_when_statusline_stale():
    bus = EventBus()
    bus.emit("usage", **sl(41, resets=None, seen=time.time() - 3600))
    bus.emit("usage", **sdk("allowed", None, None))
    assert usage_of(bus)["utilization_pct"] is None


def test_merge_not_kept_when_window_rolled_over():
    bus = EventBus()
    bus.emit("usage", **sl(41))
    bus.emit("usage", **sdk("allowed", None, FUTURE + 18000))  # a later window
    u = usage_of(bus)
    assert u["utilization_pct"] is None and u["resets_at"] == FUTURE + 18000


def test_merge_other_window_untouched():
    bus = EventBus()
    bus.emit("usage", **sl(41))
    bus.emit("usage", **sdk("allowed", None, FUTURE, kind="seven_day"))
    assert usage_of(bus)["utilization_pct"] == 41.0
    assert usage_of(bus, "seven_day")["utilization_pct"] is None


def test_merge_does_not_hide_rejected_or_real_pct():
    bus = EventBus()
    bus.emit("usage", **sl(41))
    bus.emit("usage", **sdk("rejected", None, FUTURE))
    assert usage_of(bus)["status"] == "rejected"
    bus.emit("usage", **sl(50))
    bus.emit("usage", **sdk("allowed_warning", 83.0, FUTURE))  # SDK with a real percent wins
    u = usage_of(bus)
    assert u["utilization_pct"] == 83.0 and u["status"] == "allowed_warning"


def test_merge_plain_sdk_only_unchanged():
    bus = EventBus()
    bus.emit("usage", **sdk("allowed", None, FUTURE))
    assert usage_of(bus)["utilization_pct"] is None and "source" not in usage_of(bus)


def test_statusline_after_sdk_replaces():
    bus = EventBus()
    bus.emit("usage", **sdk("allowed", None, FUTURE))
    bus.emit("usage", **sl(30))
    assert usage_of(bus)["utilization_pct"] == 30.0


# ---- usage_monitor ----

class FakeTimer:
    made = []

    def __init__(self, delay, fn, args=()):
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self): pass
    def cancel(self): pass


def um_setup():
    d = Path(tempfile.mkdtemp(prefix="jarvis-slwire-um-"))
    um.STATE_FILE, um.FLAG_FILE, um.HANDOFF_FILE = d / "state.json", d / "flag", d / "HANDOFF.md"
    um.now_fn = lambda: NOW
    um.timer_factory = FakeTimer
    um._timer = None
    FakeTimer.made = []
    um.push_fn = lambda msg, title=None: None
    um.sleep_fn = lambda s: None
    um._auto_continue_enabled = lambda: True
    um._notify_enabled = lambda: True


def test_monitor_not_armed_by_allowed_or_warning_statusline():
    um_setup()
    for pct in (10, 85):
        um.on_rate_limit_event(sl(pct, resets=int(NOW + 3600)))
    assert not um.STATE_FILE.exists() and not FakeTimer.made and not um.FLAG_FILE.exists()


def test_monitor_statusline_rejected_without_resets_does_not_arm():
    um_setup()
    um.on_rate_limit_event(sl(100, resets=None))
    assert not um.STATE_FILE.exists() and not FakeTimer.made


def test_monitor_statusline_rejected_with_resets_arms():
    um_setup()
    um.on_rate_limit_event(sl(100, resets=int(NOW + 3600)))
    assert um.STATE_FILE.exists() and len(FakeTimer.made) == 1
    assert json.loads(um.STATE_FILE.read_text(encoding="utf-8"))["resets_at"] == NOW + 3600


def test_monitor_statusline_allowed_does_not_clear_armed_limit():
    um_setup()
    um.on_rate_limit_event({"type": "five_hour", "status": "rejected", "utilization_pct": None,
                            "resets_at": int(NOW + 3600), "seen_at": int(NOW)})
    assert um.STATE_FILE.exists()
    um.now_fn = lambda: NOW + 4000  # past the reset: an SDK "allowed" would clear it
    um.on_rate_limit_event(sl(5, resets=int(NOW + 90000)))
    assert um.STATE_FILE.exists()


# ---- brain start / close with the poller ----

def _run(coro):
    return asyncio.run(coro)


def test_start_and_stop_poller_never_raise():
    with env("JARVIS_STATUSLINE_USAGE", "1"):
        b = make_brain()

        async def noop_reader():
            await asyncio.sleep(3600)
        b._reader = noop_reader

        async def go():
            await b.start()
            assert b.client.connected and b._usage_poller is not None
            assert b._usage_poller._thread.is_alive()
            t = b._usage_poller._thread
            b._stop_usage_poller()
            assert b._usage_poller is None and not t.is_alive()
            b._stop_usage_poller()  # idempotent
            b._reader_task.cancel()
        _run(go())


def test_poller_disabled_not_started():
    with env("JARVIS_STATUSLINE_USAGE", "0"):
        b = make_brain()
        b._start_usage_poller()
        assert b._usage_poller is None


def test_poller_failures_are_swallowed():
    b = make_brain()
    saved = su.UsagePoller

    class Boom:
        def __init__(self, *a, **k): raise RuntimeError("no poller")
    su.UsagePoller = Boom
    try:
        with env("JARVIS_STATUSLINE_USAGE", "1"):
            b._start_usage_poller()  # must not raise
        assert b._usage_poller is None
    finally:
        su.UsagePoller = saved

    class BadStop:
        def stop(self): raise RuntimeError("stop failed")
    b._usage_poller = BadStop()
    b._stop_usage_poller()  # must not raise
    assert b._usage_poller is None
    b2 = make_brain()
    del b2._usage_poller
    b2._stop_usage_poller()  # object built without the attribute


def test_publish_usage_callback_exceptions_do_not_escape():
    saved = brain_mod.bus.emit
    brain_mod.bus.emit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bus down"))
    try:
        brain_mod.publish_usage(sl(50))
    finally:
        brain_mod.bus.emit = saved


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
