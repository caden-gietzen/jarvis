"""Thread-safe event bus + live state snapshot: the feed the web page (and list_tasks) reads.

emit() may be called from any thread (asyncio brain, audio threads) and never blocks or
raises. Every event is {"seq", "ts", "kind", **data}; seq increases by one per event.
Kinds and fields:
  conversation   role ("you"|"jarvis"), text
  mode           mode ("listening"|"thinking"|"speaking"|"idle")
  tool_use       name, tool_use_id, description, label (short human text), parent (parent_tool_use_id or None)
  tool_result    tool_use_id, is_error (flag only, never the output), parent
  agent_dispatch tool_use_id, subagent_type, description, parent
  task_started   task_id, description, task_kind ("subagent"|"background"|"tool"), tool_use_id, task_type
  task_progress  task_id, description, activity, usage, tool_use_id
  task_done      task_id, status ("done"|"failed"|"stopped"), summary, usage
  narration      text, level ("activity"|"status")
  permission_request  tool, summary, key (public), id (one-time web answer id, None if voice only), voice_only, expires_ts
  permission_result   tool, allowed, key, id, by ("voice"|"web"|"none")
  usage          type (rate-limit window), status ("allowed"|"allowed_warning"|"rejected"), utilization_pct (0-100 or None), source (optional, "statusline"),
                 resets_at (unix seconds or None), seen_at (unix seconds)
  voice          muted (bool: Jarvis's voice is off; text still shows everywhere)
  conversation_mode  on (bool: after a spoken reply Jarvis listens for the answer without the wake word)
  dictation      on (bool: dictating or just starting), phase ("idle"|"starting"|"recording"|"finishing"),
                 since (unix seconds or None), max_s (the hard length limit)
  error          text
Consumers: bus.subscribe() returns a Subscription (sync get() / async aget()); late
subscribers pass since=<last seq> to replay from the ring buffer. bus.get_state() is the snapshot.
"""

import asyncio
import re
import threading
import time
from collections import deque

RING_SIZE = 500
SUB_QUEUE_SIZE = 1000
CONVERSATION_KEEP = 60
FINISHED_KEEP = 50
USAGE_FRESH_S = 15 * 60  # a statusline percent this young survives an SDK event that has no percent
RECENT_TOOLS_KEEP = 40  # main-agent tool chips replayed to a page that opens mid-turn
TASK_FEED_KEEP = 200  # tool calls remembered per task (the page keeps the same cap)
EARLY_FEED_KEEP = 40  # parents whose tool calls arrived before their task_started
PENDING_PERMISSIONS_KEEP = 20
_DISPATCH_TOOLS = ("Agent", "Task")
_STATUS_MAP = {"completed": "done", "done": "done", "failed": "failed",
               "stopped": "stopped", "killed": "stopped"}


def _base(path) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "a file"


_TEST_CMD = re.compile(r"pytest|unittest|(^|[\s/\\])test_\w+\.py|npm (run )?test", re.I)


def human_label(name: str, inp: dict | None) -> str:
    """Short human text for a tool call ("Reading web.py", "Running tests"). Never includes file
    contents or command output; a command is reduced to its first word (plus a plain subcommand)."""
    i = inp if isinstance(inp, dict) else {}
    try:
        if name == "Read":
            return f"Reading {_base(i.get('file_path'))}"
        if name == "Edit":
            return f"Editing {_base(i.get('file_path'))}"
        if name == "Write":
            return f"Writing {_base(i.get('file_path'))}"
        if name in ("Bash", "PowerShell"):
            cmd = str(i.get("command") or "").strip()
            if _TEST_CMD.search(cmd):
                return "Running tests"
            words = cmd.split()
            if not words:
                return "Running a command"
            out = _base(words[0])[:30]
            if len(words) > 1 and re.fullmatch(r"[A-Za-z][A-Za-z-]{0,20}", words[1]):
                out += " " + words[1]
            return f"Running {out}"
        if name in ("Grep", "Glob"):
            return f"Searching for {str(i.get('pattern') or '')[:40]}".rstrip()
        if name == "WebSearch":
            return "Searching the web"
        if name == "WebFetch":
            return "Fetching a web page"
        if name in _DISPATCH_TOOLS:
            return ("Starting agent: " + str(i.get("description") or i.get("subagent_type") or "")[:60]).rstrip(": ")
        if name.startswith("mcp__"):
            return name.split("__")[-1].replace("_", " ").capitalize()
    except Exception:
        pass
    return str(name)


class Subscription:
    """Bounded per-subscriber queue; when full the OLDEST event is dropped (see .dropped)."""

    def __init__(self, bus: "EventBus", maxlen: int):
        self._bus = bus
        self._q: deque = deque()
        self._maxlen = maxlen
        self._cond = threading.Condition()
        self._waiters: list = []  # (loop, asyncio.Event) of pending aget() calls
        self.dropped = 0
        self.closed = False

    def _push(self, ev: dict) -> None:
        with self._cond:
            if len(self._q) >= self._maxlen:
                self._q.popleft()
                self.dropped += 1
            self._q.append(ev)
            waiters, self._waiters = self._waiters, []
            self._cond.notify_all()
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:  # that loop is closed
                pass

    def get_nowait(self):
        with self._cond:
            return self._q.popleft() if self._q else None

    def get(self, timeout: float | None = None):
        """Blocking get for threads. None on timeout or after close()."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._q:
                if self.closed:
                    return None
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return None
                self._cond.wait(left)
            return self._q.popleft()

    async def aget(self, timeout: float | None = None):
        """Awaitable get for asyncio consumers. None on timeout or after close()."""
        loop = asyncio.get_running_loop()
        end = None if timeout is None else loop.time() + timeout
        while True:
            flag = asyncio.Event()
            with self._cond:
                if self._q:
                    return self._q.popleft()
                if self.closed:
                    return None
                self._waiters.append((loop, flag))
            left = None if end is None else end - loop.time()
            if left is not None and left <= 0:
                return None
            try:
                await asyncio.wait_for(flag.wait(), left)
            except asyncio.TimeoutError:
                return None

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def _close(self) -> None:
        with self._cond:
            self.closed = True
            waiters, self._waiters = self._waiters, []
            self._cond.notify_all()
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:
                pass


class EventBus:
    def __init__(self, ring_size: int = RING_SIZE):
        self._lock = threading.RLock()
        self._seq = 0
        self._ring: deque = deque(maxlen=ring_size)
        self._subs: list[Subscription] = []
        self._mode = "idle"
        self._voice_muted = False
        self._conversation_mode = True  # default on; the main loop emits the saved value at startup
        self._tasks: dict[str, dict] = {}
        self._dispatches: dict[str, dict] = {}  # tool_use_id -> agent_dispatch data
        self._early_feed: dict[str, list] = {}  # parent tool_use_id -> feed entries seen before task_started
        self._conversation: deque = deque(maxlen=CONVERSATION_KEEP)
        self._recent_tools: deque = deque(maxlen=RECENT_TOOLS_KEEP)  # main-agent tool chips, oldest first
        self._dictation = {"on": False, "phase": "idle", "since": None, "max_s": None}
        self._usage: dict[str, dict] = {}  # rate_limit_type -> latest usage data
        self._pending_permissions: dict = {}  # id (or "anon<seq>") -> pending request, oldest first

    # ---- publishing ----------------------------------------------------------------
    def emit(self, kind: str, **data) -> dict | None:
        """Record and fan out one event. Never blocks on consumers, never raises."""
        try:
            with self._lock:
                self._seq += 1
                ev = {"seq": self._seq, "ts": time.time(), "kind": kind, **data}
                self._ring.append(ev)
                try:
                    self._reduce(ev)
                except Exception:  # a state bug must not lose the event
                    pass
                for sub in self._subs:
                    sub._push(ev)
            return ev
        except Exception:
            return None

    # ---- subscribing ---------------------------------------------------------------
    def subscribe(self, since: int | None = None, maxlen: int = SUB_QUEUE_SIZE) -> Subscription:
        """New subscriber. since=N first replays buffered events with seq > N (atomically,
        so nothing is missed or duplicated); since=None gets only future events."""
        sub = Subscription(self, maxlen)
        with self._lock:
            if since is not None:
                for ev in self._ring:
                    if ev["seq"] > since:
                        sub._push(ev)
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub._close()

    def history(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [ev for ev in self._ring if ev["seq"] > since]

    # ---- live state ----------------------------------------------------------------
    def _reduce(self, ev: dict) -> None:
        kind = ev["kind"]
        if kind == "conversation":
            self._conversation.append({"seq": ev["seq"], "ts": ev["ts"],
                                       "role": ev.get("role"), "text": ev.get("text", "")})
        elif kind == "mode":
            self._mode = ev.get("mode", self._mode)
        elif kind == "agent_dispatch":
            if ev.get("tool_use_id"):
                self._dispatches[ev["tool_use_id"]] = ev
                while len(self._dispatches) > 200:
                    self._dispatches.pop(next(iter(self._dispatches)))
        elif kind == "tool_use":
            parent = ev.get("parent")
            if not parent:  # the main agent's own call: kept (bounded) so a late page can show its chip
                self._recent_tools.append({
                    "seq": ev["seq"], "ts": ev["ts"], "id": ev.get("tool_use_id"), "name": ev.get("name") or "",
                    "label": ev.get("label") or "", "description": ev.get("description") or "", "ok": None})
            if parent:  # traffic of a subagent: that task's current activity + feed
                text = ev.get("label") or ev.get("description") or ev.get("name") or ""
                entry = {"ts": ev["ts"], "tool": ev.get("name") or "", "description": text,
                         "id": ev.get("tool_use_id"), "ok": None}
                linked = False
                for t in self._tasks.values():
                    if t.get("tool_use_id") == parent:
                        linked = True
                        t["feed"].append(entry)
                        del t["feed"][:-TASK_FEED_KEEP]
                        t["tool_count"] += 1
                        if t["status"] == "running":
                            t["activity"] = text
                if not linked:  # the task_started may still be on its way
                    lst = self._early_feed.setdefault(parent, [])
                    lst.append(dict(entry))
                    del lst[:-TASK_FEED_KEEP]
                    while len(self._early_feed) > EARLY_FEED_KEEP:
                        self._early_feed.pop(next(iter(self._early_feed)))
        elif kind == "tool_result":
            tid = ev.get("tool_use_id")
            if tid and not ev.get("parent"):
                for e in reversed(self._recent_tools):
                    if e["id"] == tid:
                        e["ok"] = not ev.get("is_error")
                        break
            if tid:
                for t in self._tasks.values():
                    for e in reversed(t["feed"]):
                        if e["id"] == tid:
                            e["ok"] = not ev.get("is_error")
                            return
                for lst in self._early_feed.values():
                    for e in reversed(lst):
                        if e["id"] == tid:
                            e["ok"] = not ev.get("is_error")
                            return
        elif kind == "task_started":
            tid = ev.get("task_id")
            if not tid:
                return
            disp = self._dispatches.get(ev.get("tool_use_id") or "")
            name = ev.get("description") or (disp or {}).get("description") or tid
            kind_ = ev.get("task_kind") or ("subagent" if disp or "agent" in str(ev.get("task_type") or "")
                                            else "background")
            ev["task_kind"] = kind_  # the event carries the resolved kind too
            self._tasks[tid] = {
                "task_id": tid, "name": name, "description": name,
                "kind": kind_,
                "activity": "", "status": "running", "started_at": ev["ts"], "finished_at": None,
                "parent": (disp or {}).get("parent"), "tool_use_id": ev.get("tool_use_id"),
                "subagent_type": (disp or {}).get("subagent_type"), "summary": "",
                "feed": [], "tool_count": 0,
            }
            early = self._early_feed.pop(ev.get("tool_use_id") or "", None)
            if early:
                self._tasks[tid]["feed"] = early[-TASK_FEED_KEEP:]
                self._tasks[tid]["tool_count"] = len(early)
            self._prune()
        elif kind == "task_progress":
            t = self._tasks.get(ev.get("task_id"))
            if t and t["status"] == "running":
                t["activity"] = ev.get("activity") or ev.get("description") or t["activity"]
        elif kind == "task_done":
            t = self._tasks.get(ev.get("task_id"))
            if t and t["status"] == "running":
                t["status"] = _STATUS_MAP.get(ev.get("status"), "done")
                t["finished_at"] = ev["ts"]
                t["summary"] = ev.get("summary") or ""
        elif kind == "voice":
            self._voice_muted = bool(ev.get("muted"))
        elif kind == "conversation_mode":
            self._conversation_mode = bool(ev.get("on"))
        elif kind == "dictation":
            on = bool(ev.get("on"))
            phase = ev.get("phase") or ("recording" if on else "idle")
            self._dictation = {"on": on, "phase": phase, "since": ev.get("since") if phase != "idle" else None,
                               "max_s": ev.get("max_s")}
        elif kind == "usage":
            key = str(ev.get("type") or "five_hour")
            prev = self._usage.get(key)
            if prev and self._keeps_statusline_pct(prev, ev):
                return  # an SDK "allowed" with no percent must not wipe a real statusline percentage
            rec = {k: ev.get(k) for k in ("type", "status", "utilization_pct", "resets_at", "seen_at")}
            if ev.get("source"):
                rec["source"] = ev.get("source")
            self._usage[key] = rec
        elif kind == "permission_request":
            key = ev.get("key") or ev.get("id") or f"anon{ev['seq']}"
            self._pending_permissions[key] = {
                "tool": ev.get("tool"), "summary": ev.get("summary"), "ts": ev["ts"], "key": key,
                "id": ev.get("id"), "voice_only": bool(ev.get("voice_only")), "expires_ts": ev.get("expires_ts")}
            while len(self._pending_permissions) > PENDING_PERMISSIONS_KEEP:
                self._pending_permissions.pop(next(iter(self._pending_permissions)))
        elif kind == "permission_result":
            key = ev.get("key") or ev.get("id")
            if key is None:  # old-style event with no key: it answers the oldest request
                if self._pending_permissions:
                    del self._pending_permissions[next(iter(self._pending_permissions))]
            else:
                self._pending_permissions.pop(key, None)

    @staticmethod
    def _keeps_statusline_pct(prev: dict, ev: dict) -> bool:
        """True when `ev` (no percent, status allowed) must not replace `prev` (a real percent)."""
        if ev.get("utilization_pct") is not None or ev.get("status") != "allowed":
            return False
        if prev.get("utilization_pct") is None:
            return False
        pr, nr = prev.get("resets_at"), ev.get("resets_at")
        if pr is not None and nr is not None:
            return pr == nr  # same window; a different reset time means the window rolled over
        if prev.get("source") != "statusline":
            return False
        try:  # one side has no reset time: keep only while the statusline value is fresh
            return float(ev.get("ts") or time.time()) - float(prev.get("seen_at") or 0) <= USAGE_FRESH_S
        except (TypeError, ValueError):
            return False

    def _prune(self) -> None:
        done = [k for k, t in self._tasks.items() if t["status"] != "running"]
        for k in done[:-FINISHED_KEEP]:
            del self._tasks[k]

    def task_status(self, task_id: str) -> str | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return t["status"] if t else None

    def get_tasks(self, active_only: bool = False) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for t in self._tasks.values():
                if active_only and t["status"] != "running":
                    continue
                c = dict(t)
                c["feed"] = [dict(e) for e in t["feed"]]
                c["elapsed"] = round((t["finished_at"] or now) - t["started_at"], 1)
                out.append(c)
            return out

    def get_state(self) -> dict:
        with self._lock:
            return {"seq": self._seq, "ts": time.time(), "mode": self._mode,
                    "voice_muted": self._voice_muted, "conversation_mode": self._conversation_mode,
                    "dictation": dict(self._dictation),
                    "recent_tools": [dict(e) for e in self._recent_tools],
                    "tasks": self.get_tasks(), "conversation": list(self._conversation),
                    "pending_permission": next(iter(self._pending_permissions.values()), None),
                    "usage": {k: dict(v) for k, v in self._usage.items()}}


bus = EventBus()  # process-wide singleton: `from jarvis.events import bus`


def emit(kind: str, **data):
    return bus.emit(kind, **data)


def ingest_system_message(msg, target: EventBus | None = None) -> None:
    """Map an SDK task-lifecycle SystemMessage to events (duck-typed on the class name, so
    this module needs no SDK import). Unknown messages are ignored. Never raises."""
    b = target or bus
    try:
        name = type(msg).__name__
        g = lambda attr, default=None: getattr(msg, attr, default)  # noqa: E731
        if name == "TaskStartedMessage":
            b.emit("task_started", task_id=g("task_id"), description=g("description", ""),
                   tool_use_id=g("tool_use_id"), task_type=g("task_type"))
        elif name == "TaskProgressMessage":
            b.emit("task_progress", task_id=g("task_id"), description=g("description", ""),
                   activity=g("last_tool_name") or g("description", ""), usage=g("usage"),
                   tool_use_id=g("tool_use_id"))
        elif name == "TaskNotificationMessage":
            b.emit("task_done", task_id=g("task_id"), status=_STATUS_MAP.get(g("status"), "done"),
                   summary=g("summary", ""), usage=g("usage"))
        elif name == "TaskUpdatedMessage":
            patch = g("patch") or {}
            status = patch.get("status") or g("status")
            if status in ("completed", "failed", "killed", "stopped"):
                # The notification may have come first (or never): only the first terminal one counts.
                if b.task_status(g("task_id")) in (None, "running"):
                    b.emit("task_done", task_id=g("task_id"), status=_STATUS_MAP[status],
                           summary=patch.get("summary", ""), usage=None)
            elif status in ("running", "pending", "paused") or patch.get("description"):
                b.emit("task_progress", task_id=g("task_id"), description=patch.get("description", ""),
                       activity=patch.get("description", "") or (status or ""), usage=None, tool_use_id=None)
    except Exception:
        pass
