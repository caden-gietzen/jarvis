r"""Offline tests for the event bus, live state, SDK lifecycle mapping, ui tee and list_tasks.

Run (base interpreter):
    .venv\Scripts\python.exe tests\test_events.py

Plain script, no pytest needed. Prints PASS/FAIL per test, exits non-zero on failure.
"""

import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
_os_np.environ.setdefault("JARVIS_TOKEN_LEDGER", "0")  # nor write the real token ledger
import asyncio
import io
import site
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage,
    TaskUpdatedMessage, ToolUseBlock)
from rich.console import Console  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import cancel as cancel_mod  # noqa: E402
from jarvis import events  # noqa: E402
from jarvis import ui  # noqa: E402
from jarvis.events import EventBus, ingest_system_message  # noqa: E402

brain_mod.STATE_PATH = Path(tempfile.gettempdir()) / "jarvis-test-events-state.json"
ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-events.log"  # keep the real log clean


def started(tid="t1", desc="do work", tool_use_id=None, task_type=None):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=tid, description=desc, uuid="u",
                              session_id="s", tool_use_id=tool_use_id, task_type=task_type)


def progress(tid="t1", desc="step", last_tool="Read"):
    return TaskProgressMessage(subtype="task_progress", data={}, task_id=tid, description=desc,
                               usage={"total_tokens": 1, "tool_uses": 1, "duration_ms": 5}, uuid="u",
                               session_id="s", last_tool_name=last_tool)


def notified(tid="t1", status="completed"):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=tid, status=status,
                                   output_file="o.txt", summary="sum", uuid="u", session_id="s")


def updated(tid="t1", status="killed"):
    return TaskUpdatedMessage(subtype="task_updated", data={}, task_id=tid, patch={"status": status})


# ---- bus --------------------------------------------------------------------------------

def test_seq_and_timestamps_increase():
    b = EventBus()
    evs = [b.emit("mode", mode=m) for m in ("listening", "thinking", "idle")]
    assert [e["seq"] for e in evs] == [1, 2, 3]
    assert evs[0]["ts"] <= evs[1]["ts"] <= evs[2]["ts"] and evs[0]["kind"] == "mode"


def test_ring_buffer_is_bounded_and_replays():
    b = EventBus(ring_size=10)
    for i in range(25):
        b.emit("narration", text=str(i))
    hist = b.history()
    assert len(hist) == 10 and hist[0]["seq"] == 16 and hist[-1]["seq"] == 25
    late = b.subscribe(since=22)
    assert [late.get_nowait()["seq"] for _ in range(3)] == [23, 24, 25] and late.get_nowait() is None


def test_emit_from_many_threads_keeps_every_event_in_order():
    b = EventBus(ring_size=10000)
    sub = b.subscribe(maxlen=5000)
    def work(n):
        for i in range(200):
            b.emit("narration", text=f"{n}-{i}")
    ths = [threading.Thread(target=work, args=(n,)) for n in range(16)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    got = [sub.get_nowait() for _ in range(3200)]
    seqs = [e["seq"] for e in got]
    assert seqs == list(range(1, 3201)), "gap or reorder"
    for n in range(16):  # per-thread order preserved
        mine = [int(e["text"].split("-")[1]) for e in got if e["text"].startswith(f"{n}-")]
        assert mine == list(range(200))


def test_slow_subscriber_drops_oldest_and_never_blocks_emit():
    b = EventBus()
    slow, fast = b.subscribe(maxlen=5), b.subscribe()
    t = time.monotonic()
    for i in range(50):
        b.emit("narration", text=str(i))
    assert time.monotonic() - t < 1.0
    assert slow.dropped == 45 and [slow.get_nowait()["seq"] for _ in range(5)] == [46, 47, 48, 49, 50]
    assert fast.dropped == 0


def test_unsubscribe_stops_delivery_and_wakes_blocked_getter():
    b = EventBus()
    s = b.subscribe()
    out = []
    th = threading.Thread(target=lambda: out.append(s.get(timeout=5)))
    th.start()
    time.sleep(0.1)
    b.unsubscribe(s)
    th.join(2)
    assert out == [None] and not th.is_alive()
    b.emit("narration", text="x")
    assert s.get_nowait() is None


def test_sync_get_receives_from_another_thread():
    b = EventBus()
    s = b.subscribe()
    threading.Timer(0.1, lambda: b.emit("error", text="boom")).start()
    ev = s.get(timeout=3)
    assert ev and ev["kind"] == "error" and ev["text"] == "boom"
    assert s.get(timeout=0.05) is None


def test_async_consumer_gets_events_emitted_from_other_threads():
    async def run():
        b = EventBus()
        s = b.subscribe()
        threading.Timer(0.1, lambda: b.emit("narration", text="hi")).start()
        ev = await s.aget(timeout=3)
        assert ev["text"] == "hi"
        assert await s.aget(timeout=0.05) is None
        b.emit("narration", text="queued")  # already queued: returns at once
        assert (await s.aget(timeout=1))["text"] == "queued"

    asyncio.run(run())


def test_emit_survives_closed_event_loop_and_bad_subscriber():
    b = EventBus()
    s = b.subscribe()
    loop = asyncio.new_event_loop()
    waiter = loop.run_until_complete(asyncio.sleep(0))  # noqa: F841
    s._waiters.append((loop, asyncio.Event()))
    loop.close()
    assert b.emit("narration", text="ok") is not None  # call_soon_threadsafe raises RuntimeError: swallowed
    assert s.get_nowait()["text"] == "ok"
    assert b.emit("narration", text=object()) is not None  # unserialisable data is not our problem


# ---- state ------------------------------------------------------------------------------

def test_state_task_lifecycle_done():
    b = EventBus()
    ingest_system_message(started("a", "refactor", task_type="local_agent"), b)
    st = b.get_state()["tasks"]
    assert len(st) == 1 and st[0]["status"] == "running" and st[0]["task_id"] == "a"
    assert st[0]["kind"] == "subagent" and st[0]["name"] == "refactor" and st[0]["elapsed"] >= 0
    ingest_system_message(progress("a", "editing files", "Edit"), b)
    assert b.get_state()["tasks"][0]["activity"] == "Edit"
    ingest_system_message(notified("a", "completed"), b)
    t = b.get_state()["tasks"][0]
    assert t["status"] == "done" and t["finished_at"] is not None
    done_elapsed = t["elapsed"]
    time.sleep(0.05)
    assert b.get_state()["tasks"][0]["elapsed"] == done_elapsed  # frozen once finished
    assert b.get_tasks(active_only=True) == []


def test_state_failed_and_stopped_and_updated_killed():
    b = EventBus()
    for tid in ("f", "s", "k"):
        ingest_system_message(started(tid), b)
    ingest_system_message(notified("f", "failed"), b)
    ingest_system_message(notified("s", "stopped"), b)
    ingest_system_message(updated("k", "killed"), b)  # terminal state only via task_updated
    by = {t["task_id"]: t["status"] for t in b.get_state()["tasks"]}
    assert by == {"f": "failed", "s": "stopped", "k": "stopped"}


def test_duplicate_terminal_messages_emit_one_task_done():
    b = EventBus()
    ingest_system_message(started("a"), b)
    ingest_system_message(updated("a", "completed"), b)
    ingest_system_message(notified("a", "completed"), b)  # a late notification is state-neutral
    done = [e for e in b.history() if e["kind"] == "task_done"]
    assert len([e for e in done if e["task_id"] == "a"]) == 2  # both events are on the feed...
    assert b.get_state()["tasks"][0]["status"] == "done"  # ...state only moved once


def test_agent_dispatch_names_the_task_and_subagent_tool_use_sets_activity():
    b = EventBus()
    b.emit("agent_dispatch", tool_use_id="tu1", subagent_type="scout", description="find the thing", parent=None)
    ingest_system_message(started("a", "", tool_use_id="tu1"), b)
    t = b.get_state()["tasks"][0]
    assert t["kind"] == "subagent" and t["name"] == "find the thing" and t["subagent_type"] == "scout"
    b.emit("tool_use", name="Grep", tool_use_id="x", description="searching: foo", parent="tu1")
    assert b.get_state()["tasks"][0]["activity"] == "searching: foo"


def test_state_conversation_and_mode_and_permission():
    b = EventBus()
    assert b.get_state()["mode"] == "idle"
    b.emit("conversation", role="you", text="hello")
    b.emit("conversation", role="jarvis", text="hi")
    b.emit("mode", mode="speaking")
    b.emit("permission_request", tool="Bash", summary="run ls")
    st = b.get_state()
    assert [(c["role"], c["text"]) for c in st["conversation"]] == [("you", "hello"), ("jarvis", "hi")]
    assert st["mode"] == "speaking" and st["pending_permission"]["tool"] == "Bash"
    b.emit("permission_result", tool="Bash", allowed=True)
    assert b.get_state()["pending_permission"] is None


def test_pending_permissions_are_tracked_by_key():
    b = EventBus()
    b.emit("permission_request", tool="A", summary="a", key="p1", id="nonce-one", voice_only=False, expires_ts=99.0)
    b.emit("permission_request", tool="B", summary="b", key="p2", id=None, voice_only=True, expires_ts=99.0)
    st = b.get_state()["pending_permission"]
    assert st["tool"] == "A" and st["id"] == "nonce-one" and st["voice_only"] is False and st["expires_ts"] == 99.0
    b.emit("permission_result", tool="A", allowed=True, key="p1", id="nonce-one", by="web")
    st = b.get_state()["pending_permission"]
    assert st["tool"] == "B" and st["id"] is None and st["voice_only"] is True, st
    b.emit("permission_result", tool="B", allowed=False, key="p2", id=None, by="voice")
    assert b.get_state()["pending_permission"] is None
    b.emit("permission_result", tool="X", allowed=False, key="never-requested")  # unknown key: harmless
    assert b.get_state()["pending_permission"] is None


def test_finished_tasks_are_pruned():
    b = EventBus()
    for i in range(events.FINISHED_KEEP + 20):
        ingest_system_message(started(f"t{i}"), b)
        ingest_system_message(notified(f"t{i}"), b)
    ingest_system_message(started("live"), b)
    ids = [t["task_id"] for t in b.get_state()["tasks"]]
    assert len(ids) <= events.FINISHED_KEEP + 1 and "live" in ids


# ---- SDK messages -> events -------------------------------------------------------------

def test_sdk_lifecycle_messages_map_to_events():
    b = EventBus()
    for m in (started("a", "d", "tu"), progress("a"), notified("a", "failed"), updated("a", "killed")):
        ingest_system_message(m, b)
    ingest_system_message(object(), b)  # unknown: ignored, no raise
    kinds = [(e["kind"], e.get("status")) for e in b.history()]
    assert kinds[:3] == [("task_started", None), ("task_progress", None), ("task_done", "failed")], kinds
    ev = b.history()[0]
    assert ev["task_id"] == "a" and ev["description"] == "d" and ev["tool_use_id"] == "tu"
    assert b.history()[1]["activity"] == "Read"


def test_brain_reader_emits_lifecycle_and_tool_events_without_changing_flow():
    class Stream:
        def __init__(self, msgs):
            self.msgs = msgs

        async def receive_messages(self):
            for m in self.msgs:
                yield m

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=None)
        b._reader_task = object()  # not the current task: end of stream doesn't start recovery
        agent = ToolUseBlock(id="tu9", name="Agent", input={"subagent_type": "scout", "description": "look"})
        sub = ToolUseBlock(id="tu10", name="Read", input={"file_path": "a.py"})
        b.client = Stream([
            started("z", "look", "tu9"),
            AssistantMessage(content=[agent], model="m"),
            AssistantMessage(content=[sub], model="m", parent_tool_use_id="tu9"),
            progress("z"),
            notified("z", "completed"),
        ])
        start = events.bus.get_state()["seq"]
        await b._reader()
        evs = events.bus.history(start)
        kinds = [e["kind"] for e in evs]
        assert "task_started" in kinds and "task_done" in kinds and "agent_dispatch" in kinds, kinds
        tool = [e for e in evs if e["kind"] == "tool_use"]
        assert [(e["name"], e["parent"]) for e in tool] == [("Agent", None), ("Read", "tu9")]
        assert tool[0]["description"] == "dispatching scout: look" and tool[1]["description"] == "reading a.py"
        disp = [e for e in evs if e["kind"] == "agent_dispatch"][0]
        assert disp["subagent_type"] == "scout" and disp["tool_use_id"] == "tu9"
        assert not b._outstanding and b._turn_active  # turn accounting is untouched by the feed

    asyncio.run(run())


# ---- per-task feed ----------------------------------------------------------------------

def tool_ev(b, parent, name="Read", tid=None, **inp):
    tid = tid or f"x{b._seq}"
    b.emit("tool_use", name=name, tool_use_id=tid, description=f"reading {inp.get('file_path', '')}",
           label=events.human_label(name, inp), parent=parent)
    return tid


def test_feed_is_linked_to_the_right_task():
    b = EventBus()
    ingest_system_message(started("A", "agent a", "tuA"), b)
    ingest_system_message(started("B", "agent b", "tuB"), b)
    tool_ev(b, "tuA", file_path=r"C:\dev\x\web.py")
    tool_ev(b, "tuB", "Bash", command="python -m pytest tests")
    tool_ev(b, None, file_path="main.py")  # the main agent's own call belongs to no task feed
    tool_ev(b, "tuUnknown", file_path="orphan.py")
    tasks = {t["task_id"]: t for t in b.get_tasks()}
    assert [e["description"] for e in tasks["A"]["feed"]] == ["Reading web.py"], tasks["A"]["feed"]
    assert [e["description"] for e in tasks["B"]["feed"]] == ["Running tests"]
    assert tasks["A"]["feed"][0]["tool"] == "Read" and tasks["A"]["feed"][0]["ts"] > 0
    assert tasks["A"]["activity"] == "Reading web.py" and tasks["A"]["tool_count"] == 1


def test_feed_is_bounded_per_task():
    b = EventBus(ring_size=50)
    ingest_system_message(started("A", "a", "tuA"), b)
    ingest_system_message(started("B", "b", "tuB"), b)
    for i in range(260):
        tool_ev(b, "tuA", file_path=f"f{i}.py")
    tool_ev(b, "tuB", file_path="only.py")
    t = {x["task_id"]: x for x in b.get_tasks()}
    assert len(t["A"]["feed"]) == events.TASK_FEED_KEEP == 200
    assert t["A"]["feed"][-1]["description"] == "Reading f259.py" and t["A"]["feed"][0]["description"] == "Reading f60.py"
    assert len(t["B"]["feed"]) == 1 and t["A"]["tool_count"] == 260


def test_tool_calls_before_task_started_are_attached_later():
    b = EventBus()
    tool_ev(b, "tuA", file_path="early.py")
    ingest_system_message(started("A", "a", "tuA"), b)
    tool_ev(b, "tuA", file_path="late.py")
    assert [e["description"] for e in b.get_tasks()[0]["feed"]] == ["Reading early.py", "Reading late.py"]


def test_tool_result_marks_success_or_error_only():
    b = EventBus()
    ingest_system_message(started("A", "a", "tuA"), b)
    ok = tool_ev(b, "tuA", file_path="a.py")
    bad = tool_ev(b, "tuA", "Bash", command="make")
    pend = tool_ev(b, "tuA", file_path="c.py")
    b.emit("tool_result", tool_use_id=ok, is_error=False, parent="tuA")
    b.emit("tool_result", tool_use_id=bad, is_error=True, parent="tuA")
    assert [e["ok"] for e in b.get_tasks()[0]["feed"]] == [True, False, None]
    assert pend


def test_feed_finished_task_keeps_history_and_is_replayed_in_state():
    b = EventBus()
    ingest_system_message(started("A", "a", "tuA"), b)
    tool_ev(b, "tuA", file_path="a.py")
    ingest_system_message(notified("A"), b)
    tool_ev(b, "tuA", file_path="straggler.py")  # a late call still lands on the finished task
    st = b.get_state()["tasks"][0]
    assert st["status"] == "done" and st["summary"] == "sum"
    assert [e["description"] for e in st["feed"]] == ["Reading a.py", "Reading straggler.py"]
    st["feed"].append({"x": 1})  # state is a copy
    assert len(b.get_tasks()[0]["feed"]) == 2


def test_human_labels_are_short_and_do_not_leak_arguments():
    hl = events.human_label
    assert hl("Read", {"file_path": r"C:\dev\jarvis\jarvis\web.py"}) == "Reading web.py"
    assert hl("Edit", {"file_path": "/a/b/c.py"}) == "Editing c.py"
    assert hl("Bash", {"command": "pytest -q tests"}) == "Running tests"
    assert hl("Bash", {"command": "python tests/test_web.py"}) == "Running tests"
    assert hl("Bash", {"command": "curl -H 'Authorization: Bearer SECRETTOKEN' http://x"}) == "Running curl"
    assert hl("Bash", {"command": "git status --short"}) == "Running git status"
    assert hl("Bash", {"command": "export API_KEY=hunter2hunter2"}) == "Running export"
    assert "hunter2" not in hl("Bash", {"command": "echo hunter2"}) and hl("Bash", {}) == "Running a command"
    assert hl("WebSearch", {"query": "my secret"}) == "Searching the web"
    assert hl("Weird", None) == "Weird" and hl("mcp__jarvis__show_code", {}) == "Show code"


def test_brain_reader_feeds_task_and_never_puts_tool_output_on_the_bus():
    from claude_agent_sdk import ToolResultBlock, UserMessage

    class Stream:
        def __init__(self, msgs):
            self.msgs = msgs

        async def receive_messages(self):
            for m in self.msgs:
                yield m

    async def run():
        b = brain_mod.Brain(str(ROOT), speaker=None)
        b._reader_task = object()
        agent = ToolUseBlock(id="tuF", name="Agent", input={"subagent_type": "scout", "description": "look"})
        rd = ToolUseBlock(id="tuR", name="Read", input={"file_path": r"C:\dev\jarvis\jarvis\web.py"})
        sh = ToolUseBlock(id="tuS", name="Bash", input={"command": "pytest tests"})
        b.client = Stream([
            started("F", "look", "tuF"),
            AssistantMessage(content=[agent], model="m"),
            AssistantMessage(content=[rd], model="m", parent_tool_use_id="tuF"),
            UserMessage(content=[ToolResultBlock(tool_use_id="tuR", content="SECRET FILE CONTENTS password=hunter2",
                                                 is_error=False)], parent_tool_use_id="tuF"),
            AssistantMessage(content=[sh], model="m", parent_tool_use_id="tuF"),
            UserMessage(content=[ToolResultBlock(tool_use_id="tuS", content="SECRET OUTPUT", is_error=True)],
                        parent_tool_use_id="tuF"),
            UserMessage(content="plain string user message"),
        ])
        start = events.bus.get_state()["seq"]
        await b._reader()
        evs = events.bus.history(start)
        blob = repr(evs) + repr(events.bus.get_state())
        assert "SECRET" not in blob and "hunter2" not in blob
        res = [e for e in evs if e["kind"] == "tool_result"]
        assert [(e["tool_use_id"], e["is_error"], e["parent"]) for e in res] == [("tuR", False, "tuF"), ("tuS", True, "tuF")]
        task = [t for t in events.bus.get_tasks() if t["task_id"] == "F"][0]
        assert [(e["description"], e["ok"]) for e in task["feed"]] == [("Reading web.py", True), ("Running tests", False)]
        assert not b._outstanding and b._turn_active

    asyncio.run(run())


# ---- ui tee -----------------------------------------------------------------------------

def test_ui_tee_emits_events_and_leaves_console_output_intact():
    buf = io.StringIO()
    old = ui.console
    ui.console = Console(file=buf, width=80, highlight=False, color_system=None)
    start = events.bus.get_state()["seq"]
    try:
        ui.you("what time is it")
        ui.jarvis("It is noon.", "opus")
        ui.activity("reading x.py")
        ui.status("starting")
        ui.error("bad thing")
    finally:
        ui.console = old
    out = buf.getvalue()
    for want in ("You: what time is it", "Jarvis: (opus)", "It is noon.", "· reading x.py", "starting", "bad thing"):
        assert want in out, (want, out)
    evs = events.bus.history(start)
    conv = [(e["role"], e["text"]) for e in evs if e["kind"] == "conversation"]
    assert conv == [("you", "what time is it"), ("jarvis", "It is noon.")]
    assert [e["level"] for e in evs if e["kind"] == "narration"] == ["activity", "status"]
    assert [e["text"] for e in evs if e["kind"] == "error"] == ["bad thing"]
    assert any(e["kind"] == "mode" and e["mode"] == "thinking" for e in evs)


# ---- list_tasks -------------------------------------------------------------------------

def test_list_tasks_reports_only_running_tasks():
    b = EventBus()
    res = asyncio.run(cancel_mod.list_tasks_impl({}, lambda: b))
    assert "No background tasks" in res["content"][0]["text"]
    ingest_system_message(started("run1", "build site"), b)
    ingest_system_message(progress("run1", "x", "Bash"), b)
    ingest_system_message(started("old", "finished job"), b)
    ingest_system_message(notified("old"), b)
    text = asyncio.run(cancel_mod.list_tasks_impl({}, lambda: b))["content"][0]["text"]
    assert "id=run1" in text and "build site" in text and "activity: Bash" in text and "elapsed:" in text
    assert "old" not in text


def test_server_registers_both_tools_and_brain_allows_list_tasks():
    srv = cancel_mod.build_server(lambda: None)
    assert srv["name"] == "jarvis_control"
    assert cancel_mod.LIST_TOOL_ID == "mcp__jarvis_control__list_tasks"
    b = brain_mod.Brain(str(ROOT), speaker=None)
    assert cancel_mod.LIST_TOOL_ID in b.options.allowed_tools
    assert cancel_mod.LIST_TOOL_ID in b.options.system_prompt["append"]
    assert "list_tasks" in cancel_mod._DESCRIPTION


def test_bus_singleton_is_importable():
    from jarvis.events import bus
    assert isinstance(bus, EventBus) and ui.bus is bus


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
