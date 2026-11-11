r"""Offline tests for rate-limit/usage capture, the event feed and the web page. No audio, no real Claude.

Run:  .venv\Scripts\python.exe tests\test_usage.py     (plain script, PASS/FAIL per test)
"""

import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
_os_np.environ.setdefault("JARVIS_TOKEN_LEDGER", "0")  # nor write the real token ledger
import asyncio
import importlib
import json
import site
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage, TextBlock  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import crashreport, ui  # noqa: E402
from jarvis.events import EventBus  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
ui._log_path = TMP / "test.log"
ui.error = lambda text: None
ui.status = lambda text: None
ui.jarvis = lambda text, model=None: None
brain_mod.STATE_PATH = TMP / "state.json"
brain_mod.RATE_LIMIT_LOG = TMP / "rate_limits.jsonl"
crashreport.configure(TMP)

HOOK_CALLS: list = []


def install_hook(raises=False):
    """Replace the usage monitor's entry point (the module is imported lazily by brain)."""
    mod = importlib.import_module("jarvis.usage_monitor")

    def hook(info):
        HOOK_CALLS.append(info)
        if raises:
            raise RuntimeError("monitor exploded")
    mod.on_rate_limit_event = hook


class FakeClient:
    script: list = []

    def __init__(self, options):
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, text):
        if not isinstance(text, str):
            text = [m async for m in text][0]["message"]["content"]
        await self.inbox.put(text)

    async def set_model(self, model):
        pass

    async def receive_messages(self):
        while True:
            text = await self.inbox.get()
            for m in FakeClient.script:
                yield m
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="sess-1", result=f"reply to {text}")


def rl(status="allowed", util=0.4, typ="five_hour", resets=2000000000):
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status=status, resets_at=resets, rate_limit_type=typ,
                                                        utilization=util), uuid="u", session_id="s")


async def run_turn(script, hook_raises=False):
    brain_mod.bus = EventBus()  # fresh bus per scenario
    brain_mod.RATE_LIMIT_LOG.unlink(missing_ok=True)
    HOOK_CALLS.clear()
    install_hook(hook_raises)
    FakeClient.script = script
    brain_mod.ClaudeSDKClient = FakeClient
    b = brain_mod.Brain(str(ROOT), model="sonnet", speaker=None)
    await b.start()
    reply = await asyncio.wait_for(b.ask("hi"), 3)
    await b.close()
    return reply, brain_mod.bus


def usage_events(bus):
    return [e for e in bus.history() if e["kind"] == "usage"]


def test_normalization():
    n = brain_mod.normalize_utilization
    assert n(0.4) == 40.0
    assert n(63) == 63.0
    assert n(1.0) == 100.0
    assert n(0) == 0.0
    assert n(None) is None and n("x") is None and n(-1) is None


def test_reader_handles_rate_limit_event():
    reply, bus = asyncio.run(run_turn([rl(util=0.4), rl("allowed_warning", 63, "seven_day", 2000100000)]))
    assert reply == "reply to hi", reply  # the turn still completes
    evs = usage_events(bus)
    assert len(evs) == 2, evs
    assert evs[0]["type"] == "five_hour" and evs[0]["utilization_pct"] == 40.0 and evs[0]["status"] == "allowed"
    assert evs[0]["resets_at"] == 2000000000 and isinstance(evs[0]["seen_at"], int)
    assert evs[1]["type"] == "seven_day" and evs[1]["utilization_pct"] == 63.0
    lines = brain_mod.RATE_LIMIT_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    raw = json.loads(lines[0])["info"]
    assert raw["status"] == "allowed" and raw["utilization"] == 0.4
    assert set(raw) == {"type", "status", "utilization", "resets_at", "overage_status"}, raw  # no `raw` dump
    assert "raw" not in raw
    assert len(HOOK_CALLS) == 2 and HOOK_CALLS[0]["utilization_pct"] == 40.0


def test_monitor_hook_raising_never_breaks_reader():
    reply, bus = asyncio.run(run_turn([rl()], hook_raises=True))
    assert reply == "reply to hi", reply
    assert len(usage_events(bus)) == 1 and len(HOOK_CALLS) == 1


def test_get_state_includes_usage():
    bus = EventBus()
    assert bus.get_state()["usage"] == {}
    bus.emit("usage", type="five_hour", status="allowed", utilization_pct=10.0, resets_at=5, seen_at=1)
    bus.emit("usage", type="seven_day", status="allowed", utilization_pct=50.0, resets_at=9, seen_at=1)
    bus.emit("usage", type="five_hour", status="allowed_warning", utilization_pct=80.0, resets_at=5, seen_at=2)
    u = bus.get_state()["usage"]
    assert set(u) == {"five_hour", "seven_day"}
    assert u["five_hour"]["utilization_pct"] == 80.0 and u["five_hour"]["status"] == "allowed_warning"
    assert u["seven_day"]["utilization_pct"] == 50.0
    json.dumps(bus.get_state())  # must be serialisable for the SSE snapshot


def test_rejected_429_result():
    bad = ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                        session_id="sess-1", result="limit", api_error_status=429)
    FakeClient.script = []
    asyncio.run(run_turn([bad]))  # the 429 ResultMessage ends a turn by itself
    # fresh run to inspect: bus is module-level after run_turn
    evs = usage_events(brain_mod.bus)
    assert evs and evs[0]["status"] == "rejected" and evs[0]["type"] == "five_hour", evs
    assert evs[0]["resets_at"] is None and evs[0]["utilization_pct"] is None
    assert HOOK_CALLS and HOOK_CALLS[0]["status"] == "rejected"
    assert brain_mod.bus.get_state()["usage"]["five_hour"]["status"] == "rejected"


def test_rejected_assistant_rate_limit_error():
    msg = AssistantMessage(content=[TextBlock(text="limit")], model="m", error="rate_limit")
    _, bus = asyncio.run(run_turn([msg]))
    evs = usage_events(bus)
    assert len(evs) == 1 and evs[0]["status"] == "rejected", evs
    assert HOOK_CALLS and HOOK_CALLS[0]["status"] == "rejected"


def test_page_has_usage_panel():
    html = (ROOT / "jarvis" / "webui.html").read_text(encoding="utf-8")
    for needle in ('id="usage"', 'id="usagePill"', 'case "usage"', "no usage data yet", "s.usage"):
        assert needle in html, needle
    from jarvis.web import page_headers
    csp = page_headers(html)["Content-Security-Policy"]
    assert "script-src 'sha256-" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.monotonic()
    for name, fn in tests:
        t = time.monotonic()
        try:
            fn()
            print(f"PASS {name} ({time.monotonic() - t:.1f}s)")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.monotonic() - t0:.1f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
