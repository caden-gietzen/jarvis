r"""Offline tests for the real usage percentages from rate_limit_info.raw["unifiedWindows"].

Plain script (PASS/FAIL per test, non-zero exit on failure):  .venv\Scripts\python.exe tests\test_unified_windows.py
Nothing real is touched: temp state/flag/log files, fake timer and push, fake SDK client.
"""
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # never send a real phone push
_os_np.environ.setdefault("JARVIS_TOKEN_LEDGER", "0")  # never write the real token ledger
import asyncio
import importlib
import json
import site
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import crashreport, ui, usage_monitor as um  # noqa: E402
from jarvis.events import EventBus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-unified-"))
ui._log_path = TMP / "test.log"
ui.error = ui.status = lambda text: None
ui.jarvis = lambda text, model=None: None
brain_mod.STATE_PATH = TMP / "state.json"
brain_mod.RATE_LIMIT_LOG = TMP / "rate_limits.jsonl"
crashreport.configure(TMP)
NOW = 1_800_000_000.0
USAGE_KEYS = {"type", "status", "utilization_pct", "resets_at", "seen_at"}  # the existing usage event shape
ORIG_HOOK = um.on_rate_limit_event


def info(uw="__none__", status="allowed", top_util=None, typ="five_hour", resets=1794100200, extra=None):
    raw = {"status": status, "resetsAt": resets, "rateLimitType": typ, "overageStatus": "rejected",
           "overageDisabledReason": "out_of_credits", "isUsingOverage": False}
    if uw != "__none__":
        raw["unifiedWindows"] = uw
    raw.update(extra or {})
    return RateLimitInfo(status=status, resets_at=resets, rate_limit_type=typ, utilization=top_util, raw=raw)


REAL = {"five_hour": {"utilization": 0.7, "resetsAt": 1794100200, "status": "allowed"},
        "seven_day": {"utilization": 0.45, "resetsAt": 1794441600, "status": "allowed"}}


def by_type(lst):
    return {u["type"]: u for u in lst}


def test_realistic_parse():
    ev = by_type(brain_mod.usages_from_unified(info(REAL)))
    assert set(ev) == {"five_hour", "seven_day"}
    f, s = ev["five_hour"], ev["seven_day"]
    assert f["utilization_pct"] == 70.0 and s["utilization_pct"] == 45.0
    assert f["resets_at"] == 1794100200 and s["resets_at"] == 1794441600
    assert f["status"] == "allowed" and f["source"] == "sdk-unified"


def test_event_shape_matches_existing_usage_dict():
    top = brain_mod.usage_from_info(info(REAL))
    assert set(top) == USAGE_KEYS
    for u in brain_mod.usages_from_unified(info(REAL)):
        assert set(u) == USAGE_KEYS | {"source"}, set(u)
        assert isinstance(u["seen_at"], int) and isinstance(u["resets_at"], int)


def test_defensive_missing_and_garbage():
    f = brain_mod.usages_from_unified
    for bad in ("__none__", None, "x", 5, [], [1], {}, {"five_hour": None}, {"five_hour": "70%"},
                {"five_hour": [0.7]}, {"five_hour": {}}, {"seven_day": {"utilization": "abc"}},
                {"five_hour": {"utilization": None}}, {"five_hour": {"utilization": True}},
                {"five_hour": {"utilization": float("nan")}}, {"five_hour": {"utilization": -3}},
                {"five_hour": {"utilization": float("inf")}, "other": 1}):
        out = f(info(bad))
        assert all(u["utilization_pct"] is None or u["utilization_pct"] <= 100 for u in out), (bad, out)
    assert f(info("__none__")) == [] and f(info(None)) == [] and f(info({})) == []
    assert f(None) == [] and f(object()) == [] and f({"raw": "x"}) == [] and f(RateLimitInfo(status="allowed")) == []


def test_only_one_window_and_extra_keys():
    out = brain_mod.usages_from_unified(info({"seven_day": {"utilization": 0.45, "resetsAt": 5, "status": "allowed",
                                                            "surpassedThreshold": 0.5, "weird": {"a": 1}},
                                              "seven_day_overage_included": {"utilization": 0.9},
                                              "mystery": 1}))
    assert [u["type"] for u in out] == ["seven_day"] and out[0]["utilization_pct"] == 45.0


def test_scales_and_clamps():
    def f(u):
        return by_type(brain_mod.usages_from_unified(info({"five_hour": {"utilization": u}}))).get("five_hour")
    assert f(0.7)["utilization_pct"] == 70.0
    assert f(70)["utilization_pct"] == 70.0  # already 0-100
    # a percent alone never derives "rejected" (only the CLI's own valid status may), so no restart can be armed by scale ambiguity
    assert f(1.0)["utilization_pct"] == 100.0 and f(1.0)["status"] == "allowed_warning"
    assert f(250)["utilization_pct"] == 100.0  # out of range: clamped
    assert f(0.85)["status"] == "allowed_warning" and f(0.3)["status"] == "allowed"
    assert f("0.5")["utilization_pct"] == 50.0  # numeric string tolerated
    assert f(None) is None and f("abc") is None and f(-1) is None and f(float("nan")) is None


def test_reset_timestamps():
    def f(r):
        out = by_type(brain_mod.usages_from_unified(info({"five_hour": {"utilization": 0.5, "resetsAt": r}})))
        return out["five_hour"]["resets_at"]
    assert f(1794100200) == 1794100200
    assert f(1794100200000) == 1794100200  # ms
    assert f(1794100200.5) == 1794100200
    for bad in (None, "1794100200", True, -5, 0, float("nan"), float("inf"), [1]):
        assert f(bad) is None, bad


def test_status_unknown_or_valid():
    def f(w):
        return by_type(brain_mod.usages_from_unified(info({"five_hour": w}))).get("five_hour")
    assert f({"utilization": 0.5, "status": "weird"})["status"] == "allowed"  # derived from percent
    assert f({"utilization": 0.5, "status": 7})["status"] == "allowed"
    assert f({"utilization": 0.95, "status": "weird"})["status"] == "allowed_warning"
    assert f({"utilization": 0.95, "status": "allowed"})["status"] == "allowed"  # valid window status wins
    assert f({"utilization": None, "status": "rejected"})["status"] == "rejected"
    assert f({"utilization": None, "status": "allowed"}) is None


# ---- usage_monitor ----

class FakeTimer:
    made = []

    def __init__(self, delay, fn, args=()):
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self): pass
    def cancel(self): pass


def um_setup():
    d = Path(tempfile.mkdtemp(prefix="jarvis-unified-um-"))
    um.STATE_FILE, um.FLAG_FILE, um.HANDOFF_FILE = d / "state.json", d / "flag", d / "HANDOFF.md"
    um.now_fn = lambda: NOW
    um.timer_factory = FakeTimer
    um._timer = None
    FakeTimer.made = []
    um.push_fn = lambda msg, title=None: None
    um.sleep_fn = lambda s: None
    um._auto_continue_enabled = lambda: True
    um._notify_enabled = lambda: True


def uw_usage(w, name="five_hour"):
    return by_type(brain_mod.usages_from_unified(info({name: w})))[name]


def test_monitor_not_armed_by_allowed_windows():
    um_setup()
    for u in brain_mod.usages_from_unified(info(REAL)):
        um.on_rate_limit_event(u)
    um.on_rate_limit_event(uw_usage({"utilization": 0.9, "resetsAt": int(NOW + 3600)}))  # warning
    assert not um.STATE_FILE.exists() and not FakeTimer.made and not um.FLAG_FILE.exists()


def test_monitor_rejected_window_needs_resets_at():
    um_setup()
    um.on_rate_limit_event(uw_usage({"utilization": 1.0, "status": "rejected"}))  # no resets_at
    assert not um.STATE_FILE.exists() and not FakeTimer.made
    um.on_rate_limit_event(uw_usage({"utilization": 1.0, "status": "rejected", "resetsAt": int(NOW + 3600)}))
    assert um.STATE_FILE.exists() and len(FakeTimer.made) == 1
    assert json.loads(um.STATE_FILE.read_text(encoding="utf-8"))["resets_at"] == NOW + 3600


def test_monitor_window_allowed_does_not_clear_armed_limit():
    um_setup()
    um.on_rate_limit_event({"type": "five_hour", "status": "rejected", "utilization_pct": None,
                            "resets_at": int(NOW + 3600), "seen_at": int(NOW)})
    um.now_fn = lambda: NOW + 4000
    um.on_rate_limit_event(uw_usage({"utilization": 0.05, "resetsAt": int(NOW + 90000)}))
    assert um.STATE_FILE.exists()


def test_real_sdk_rejected_still_arms():
    um_setup()
    um.on_rate_limit_event(brain_mod.usage_from_info(info(None, status="rejected", resets=int(NOW + 3600))))
    assert um.STATE_FILE.exists() and len(FakeTimer.made) == 1


# ---- logging ----

def _logged(i):
    brain_mod.RATE_LIMIT_LOG.unlink(missing_ok=True)
    brain_mod._log_rate_limit(i)
    return json.loads(brain_mod.RATE_LIMIT_LOG.read_text(encoding="utf-8").splitlines()[-1])["info"]


def test_log_only_whitelisted_window_fields():
    uw = {"five_hour": {"utilization": 0.7, "resetsAt": 1794100200, "status": "allowed", "surpassedThreshold": 0.5,
                        "secret": "tok"},
          "seven_day": {"utilization": {"nested": 1}, "resetsAt": [1], "status": "allowed", "x": 1}}
    e = _logged(info(uw, extra={"token": "SECRET", "sessionId": "abc"}))
    assert set(e) == {"type", "status", "utilization", "resets_at", "overage_status", "unified_windows"}, set(e)
    w = e["unified_windows"]
    assert w["five_hour"] == {"utilization": 0.7, "resetsAt": 1794100200, "status": "allowed"}
    assert set(w["seven_day"]) == {"utilization", "resetsAt", "status"}
    assert w["seven_day"]["utilization"] is None and w["seven_day"]["resetsAt"] is None  # non-scalars dropped
    text = brain_mod.RATE_LIMIT_LOG.read_text(encoding="utf-8")
    assert "SECRET" not in text and "tok" not in text and "surpassedThreshold" not in text
    # no windows / garbage: the old shape, no crash
    assert "unified_windows" not in _logged(info("__none__"))
    assert "unified_windows" not in _logged(info("garbage"))


# ---- reader ----

class FakeClient:
    script: list = []

    def __init__(self, options):
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def connect(self): pass
    async def disconnect(self): pass

    async def query(self, text):
        if not isinstance(text, str):
            text = [m async for m in text][0]["message"]["content"]
        await self.inbox.put(text)

    async def set_model(self, model): pass

    async def receive_messages(self):
        while True:
            text = await self.inbox.get()
            for m in FakeClient.script:
                yield m
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s", result=f"reply to {text}")


HOOK = []


async def run_turn(script):
    brain_mod.bus = EventBus()
    brain_mod.RATE_LIMIT_LOG.unlink(missing_ok=True)
    HOOK.clear()
    um.on_rate_limit_event = lambda i: HOOK.append(i)
    FakeClient.script = script
    brain_mod.ClaudeSDKClient = FakeClient
    b = brain_mod.Brain(str(ROOT), model="sonnet", speaker=None)
    await b.start()
    reply = await asyncio.wait_for(b.ask("hi"), 3)
    await b.close()
    return reply, brain_mod.bus


def ev(i):
    return RateLimitEvent(rate_limit_info=i, uuid="u", session_id="s")


def _run(script):
    try:
        return asyncio.run(run_turn(script))
    finally:
        um.on_rate_limit_event = ORIG_HOOK


def test_reader_publishes_windows_into_state():
    reply, bus = _run([ev(info(REAL))])
    assert reply == "reply to hi"
    st = bus.get_state()["usage"]
    assert st["five_hour"]["utilization_pct"] == 70.0 and st["seven_day"]["utilization_pct"] == 45.0
    assert st["five_hour"]["source"] == "sdk-unified" and st["five_hour"]["resets_at"] == 1794100200
    kinds = [e for e in bus.history() if e["kind"] == "usage"]
    assert len(kinds) == 3  # top-level + two windows
    assert len(HOOK) == 3 and sum(1 for h in HOOK if h.get("source") == "sdk-unified") == 2
    line = json.loads(brain_mod.RATE_LIMIT_LOG.read_text(encoding="utf-8").splitlines()[0])["info"]
    assert line["unified_windows"]["seven_day"]["utilization"] == 0.45


def test_reader_rejected_top_level_kept():
    rej = info({"five_hour": {"utilization": 1.0, "status": "rejected", "resetsAt": 5},
                "seven_day": {"utilization": 0.4, "resetsAt": 9}}, status="rejected", resets=1794100200)
    reply, bus = _run([ev(rej)])
    st = bus.get_state()["usage"]
    assert reply == "reply to hi"
    assert st["five_hour"]["status"] == "rejected" and st["five_hour"]["resets_at"] == 1794100200  # top-level wins
    assert "source" not in st["five_hour"] and st["seven_day"]["utilization_pct"] == 40.0


def test_reader_survives_garbage():
    script = [ev(info(g)) for g in (None, "x", 5, [], {"five_hour": "bad", "seven_day": [1]},
                                    {"five_hour": {"utilization": object()}})]
    script.append(ev(RateLimitInfo(status="allowed", raw=None)))
    script.append(ev(RateLimitInfo(status="allowed", raw="oops")))
    reply, bus = _run(script)
    assert reply == "reply to hi"


def test_publish_unified_never_raises():
    saved = brain_mod.bus.emit
    brain_mod.bus.emit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bus down"))
    try:
        brain_mod.publish_unified(info(REAL), {"type": "five_hour", "status": "allowed"})
        brain_mod.publish_unified(None, None)
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
