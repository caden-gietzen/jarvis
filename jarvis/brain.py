"""Persistent Claude Code session via the Agent SDK.

Loads the same settings the CLI does (~/.claude/CLAUDE.md, ~/.claude/agents/*,
plus the project's own), so the orchestrator workflow applies unchanged.
"""

import asyncio
import contextvars
import dataclasses
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import crashreport, keepawake, statusline_usage, ui
from .cancel import LIST_TOOL_ID as LIST_TASKS_TOOL
from .browser import TOOL_ID as BROWSER_TOOL
from .browser import build_server as build_browser_server
from .cancel import TOOL_ID as CANCEL_TOOL
from .cancel import build_server as build_cancel_server
from .config import LOG_DIR, ROOT, STATE_PATH
from .destructive import is_destructive_shell
from .events import bus, human_label, ingest_system_message
from .notify import push_permission
from .permissions import add_reason, auto_approve, parse_yes_no, summarize_request
from .state_file import read_state, update_state
from .showcode import TOOL_ID as SHOW_CODE_TOOL
from .showcode import TERMINAL_TOOL_ID as RUN_IN_TERMINAL_TOOL
from .showcode import build_server as build_showcode_server

RATE_LIMIT_LOG = LOG_DIR / "rate_limits.jsonl"  # raw rate-limit info as the CLI sends it, one JSON line each


def normalize_utilization(u) -> float | None:
    """Utilization as a 0-100 percent. The SDK documents 0.0-1.0 but the scale is unverified:
    anything above 1.5 is taken to be a percent already."""
    try:
        v = float(u)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:
        return None
    return round(v if v > 1.5 else v * 100.0, 1)


def usage_from_info(info) -> dict:
    """Plain dict from a RateLimitInfo (duck-typed)."""
    g = lambda a: getattr(info, a, None)  # noqa: E731
    resets = g("resets_at")
    return {"type": g("rate_limit_type") or "five_hour", "status": g("status") or "allowed",
            "utilization_pct": normalize_utilization(g("utilization")),
            "resets_at": int(resets) if isinstance(resets, (int, float)) else None,
            "seen_at": int(time.time())}


UNIFIED_WINDOW_TYPES = ("five_hour", "seven_day")
_VALID_STATUS = ("allowed", "allowed_warning", "rejected")
UNIFIED_LOG_FIELDS = ("utilization", "resetsAt", "status")  # the only per-window fields ever logged


def _raw_unified_windows(info) -> dict:
    """raw["unifiedWindows"] of a RateLimitInfo (or of the raw dict itself) as a dict, {} if absent/odd.
    @internal in the CLI schema: it may change, so nothing here may raise."""
    try:
        raw = info.get("raw") if isinstance(info, dict) and "raw" in info else None
        if raw is None:
            raw = info if isinstance(info, dict) else getattr(info, "raw", None)
        uw = raw.get("unifiedWindows") if isinstance(raw, dict) else None
        return uw if isinstance(uw, dict) else {}
    except Exception:
        return {}


def _norm_epoch(v) -> int | None:
    """Epoch seconds from seconds or milliseconds; None if not a sane number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    if v <= 0:
        return None
    return int(v / 1000) if v > 1e11 else int(v)


def usages_from_unified(info) -> list[dict]:
    """One usage dict (source "sdk-unified") per five_hour / seven_day window in raw["unifiedWindows"].
    Defensive: wrong types, missing keys, odd scales or unknown statuses never raise; unusable windows are skipped."""
    out = []
    try:
        uw = _raw_unified_windows(info)
        for name in UNIFIED_WINDOW_TYPES:
            try:
                w = uw.get(name)
                if not isinstance(w, dict):
                    continue
                u = w.get("utilization")
                pct = None if isinstance(u, bool) else normalize_utilization(u)
                if pct is not None:
                    if pct == float("inf"):
                        pct = None
                    else:
                        pct = min(pct, 100.0)
                status = w.get("status")
                if status not in _VALID_STATUS:
                    # never derive "rejected" from a percent alone (scale ambiguity could arm the restart monitor):
                    # only the CLI's own valid status may say rejected
                    status = None if pct is None else ("allowed_warning" if pct >= 80 else "allowed")
                if status is None or (pct is None and status != "rejected"):
                    continue  # nothing real to show
                out.append({"type": name, "status": status, "utilization_pct": pct,
                            "resets_at": _norm_epoch(w.get("resetsAt")), "seen_at": int(time.time()),
                            "source": "sdk-unified"})
            except Exception:
                continue
    except Exception:
        return []
    return out


def publish_unified(info, top: dict | None = None) -> None:
    """Publish the unifiedWindows of a rate-limit event as usage events. Never raises. When the top-level
    event of the same type already says something is wrong (not "allowed"), that one is kept as is."""
    try:
        for u in usages_from_unified(info):
            if top and top.get("type") == u["type"] and top.get("status") not in (None, "allowed"):
                continue
            publish_usage(u)
    except Exception:
        pass


def _scalar(v):
    return v if v is None or isinstance(v, (bool, int, float, str)) else None


def _log_rate_limit(info) -> None:
    try:
        src = info if isinstance(info, dict) else None
        g = (lambda a: src.get(a)) if src is not None else (lambda a: getattr(info, a, None))  # noqa: E731
        # whitelisted fields only: the CLI's full `raw` dict is never written to disk
        entry = {"type": g("rate_limit_type"), "status": g("status"), "utilization": g("utilization"),
                 "resets_at": g("resets_at"), "overage_status": g("overage_status")}
        uw = _raw_unified_windows(info)
        if uw:
            entry["unified_windows"] = {
                str(k)[:40]: {f: _scalar(w.get(f)) for f in UNIFIED_LOG_FIELDS}
                for k, w in list(uw.items())[:8] if isinstance(w, dict)}
        RATE_LIMIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RATE_LIMIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "info": entry}, default=repr) + "\n")
    except Exception:
        pass


def publish_usage(usage: dict, raw_info=None) -> None:
    """Emit the usage event and tell the usage monitor. Never raises."""
    try:
        if raw_info is not None:
            _log_rate_limit(raw_info)
        bus.emit("usage", **usage)
    except Exception:
        pass
    try:
        from . import usage_monitor  # written separately; absent or broken must not matter
        usage_monitor.on_rate_limit_event(dict(usage))
    except Exception:
        pass


def statusline_enabled() -> bool:
    """Default OFF: the CLI never runs the status line headless (verified live), the real percentages come from
    the rate-limit event's unifiedWindows. Config key `statusline_usage` or env JARVIS_STATUSLINE_USAGE=1 enables;
    env 0 forces it off."""
    env = os.environ.get("JARVIS_STATUSLINE_USAGE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        from .config import load_config
        return bool(load_config().get("statusline_usage", False))
    except Exception:
        return False


def statusline_settings() -> str | None:
    """Inline per-session settings JSON (the CLI's flag-settings layer; ~/.claude/settings.json is untouched)."""
    try:
        return statusline_usage.settings_json() if statusline_enabled() else None
    except Exception:
        return None


def _rejected_usage() -> dict:
    # A guess made from a bare 429 (no reset time from the CLI). The usage monitor treats resets_at=None
    # as a fallback: it never overrides or re-arms a real reset time.
    return {"type": "five_hour", "status": "rejected", "utilization_pct": None,
            "resets_at": None, "seen_at": int(time.time())}


VOICE_PROMPT ="""\
You are being used through a voice interface ("Jarvis"). The user's messages are \
speech-to-text transcripts: expect mis-transcriptions and infer intent. Your final \
reply in each turn will be read aloud, so keep it short and spoken-style: plain \
sentences, no tables, no code blocks, no file paths unless they matter. The user can \
also see the full text in a terminal. Never say "hey Jarvis" (or hi/ok Jarvis) in anything that will be \
spoken: it would trigger the wake word on your own voice. To refer to it, say "just call me" \
or "say the wake word"."""


SAY_GRACE_S = None  # test override; normally None = use the configured tts.speak_grace_s (speaker.SPEAK_GRACE_S)


def _say_grace_s() -> float:
    if SAY_GRACE_S is not None:
        return SAY_GRACE_S
    from . import speaker as speaker_mod  # lazy: pulls in audio libs
    return speaker_mod.SPEAK_GRACE_S


LOST_MSG = "I lost my connection to Claude; reconnecting."
INTERRUPTED_MSG = "That request was interrupted when the connection dropped. Please say it again."
RECONNECTING_MSG = "I'm still reconnecting to Claude. Please try again in a moment."
BACK_MSG = "I'm reconnected to Claude."
FRESH_MSG = "I couldn't resume the previous conversation, so this is a fresh session."
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)  # wait between reconnect attempts; the last repeats forever
CONNECT_TIMEOUT_S = 60.0
DISCONNECT_TIMEOUT_S = 30.0  # the SDK's own close() escalates (stdin close, terminate, kill) in up to ~20 s
CLOSE_WAIT_S = 30.0  # how long close() waits for pending disconnects
HEALTHY_S = 60.0  # a connection this old counts as healthy: the loss streak starts over
UNSTABLE_LOSSES = 3  # this many losses within UNSTABLE_WINDOW_S = "unstable connection"
UNSTABLE_WINDOW_S = 300.0
UNSTABLE_MSG = "The connection to Claude is unstable; I'll keep retrying."
QUERY_TIMEOUT_S = 30.0
ASK_TIMEOUT_S = 600.0  # max wait for a human-classified result before ask() gives up and returns
NO_REPLY_MSG = "Sorry, I lost track of that request. Please say it again."
RESUME_FAIL_AFTER = 3  # failed connects with resume before falling back to a fresh session
# The only wording the bundled CLI uses when a session can't be resumed.
_RESUME_ERR = re.compile(r"no conversation found with session id", re.I)
MERGE_GRACE_S = 20.0  # after a result, how long other waiting ask()s wait for their own turn to start
INTERRUPTED_REASONS = ("aborted_streaming", "aborted_tools")  # ResultMessage.terminal_reason of a cancelled turn
# Claude Code permission mode for the Jarvis session. The user's global settings use bypassPermissions, which
# would skip can_use_tool entirely (no spoken prompt, no folder rules, no phone approval), so set it explicitly.
# Override with env JARVIS_PERMISSION_MODE (default, acceptEdits, plan, bypassPermissions).
PERMISSION_MODE = os.environ.get("JARVIS_PERMISSION_MODE", "bypassPermissions")  # user chose bypass again 2026-11-06 (testing used "default")
PERM_WAIT_S = 120.0  # a permission prompt waits this long for the user's recording to end, then denies
WEB_APPROVED_MSG = "Approved from the web page."
WEB_DENIED_MSG = "Denied from the web page."
PERM_DONE_KEEP = 50  # finished web answer ids remembered, to tell "handled by voice" from "unknown"
CANCEL_MSG = "Stopped. Some edits may be half finished."
TURN_STALE_S = 900.0  # a turn that long without a ResultMessage no longer blocks background speech


def describe_tool(block: ToolUseBlock) -> str:
    i = block.input
    match block.name:
        case "Agent" | "Task":
            return f"dispatching {i.get('subagent_type', 'agent')}: {i.get('description', '')}"
        case "Read":
            return f"reading {i.get('file_path', '')}"
        case "Edit" | "Write":
            return f"{block.name.lower()} {i.get('file_path', '')}"
        case "Bash" | "PowerShell":
            return f"running: {i.get('command', '')[:100]}"
        case "Grep" | "Glob":
            return f"searching: {i.get('pattern', '')}"
        case "mcp__jarvis__show_code":
            name = Path(str(i.get("file", ""))).name or "a file"
            a, b = i.get("start_line"), i.get("end_line")
            if a and b and b != a:
                return f"showing {name} lines {a} to {b}"
            return f"showing {name}" + (f" line {a}" if a else "")
        case "mcp__jarvis_browser__browser":
            act = str(i.get("action", ""))
            return f"browser: opening {i.get('url', '')[:80]} in a new tab" if act == "open" else f"browser: {act}"
        case "mcp__jarvis_browser__browser_allow_site":
            return f"allowing {i.get('host', '')} for the browser tool"
        case "mcp__jarvis_control__cancel_task":
            return f"cancelling {i['task_id']}" if i.get("task_id") else "cancelling the current turn"
        case "mcp__jarvis__run_in_terminal":
            return f"running in your VS Code terminal: {str(i.get('command', ''))[:100]}"
        case _:
            return block.name


async def ask_permission_keyboard(tool_name, tool_input, context):
    """Fallback: answer at the keyboard."""
    detail = json.dumps(tool_input)[:300]
    ui.status(f"\nPermission needed: {tool_name} {detail}")
    answer = await asyncio.to_thread(input, "Allow? [y/N] ")
    if answer.strip().lower().startswith("y"):
        return PermissionResultAllow()
    return PermissionResultDeny(message="The user declined this action.")


class _PermRequest:
    """One non-auto-approved permission prompt that the web page may also answer.

    state: "pending" until exactly one party claims it ("voice" or "web"); claiming happens under
    Brain._perm_reg_lock, so the first answer wins and a second one is refused. `id` is the
    one-time secret the page must send back (None for voice-only requests, which the page can never
    answer); `key` is a public label for the event feed."""

    def __init__(self, tool, key, nonce, voice_only, loop):
        self.tool, self.key, self.id, self.voice_only, self.loop = tool, key, nonce, voice_only, loop
        self.expires = time.monotonic() + PERM_WAIT_S
        self.expires_ts = time.time() + PERM_WAIT_S
        self.state = "pending"
        self.decision = None
        self.event = asyncio.Event()  # set (on the loop) when the page answers


_CURRENT_PERM: contextvars.ContextVar = contextvars.ContextVar("jarvis_current_perm", default=None)


def _load_state() -> dict:
    return read_state(STATE_PATH)


class Brain:
    def __init__(self, project_dir: str, model: str | None = None, resume: bool = False,
                 allowed_dirs: list[str] | None = None, speaker=None, listener=None,
                 transcribe=None, answer_window_s: float = 6.0):
        self.speaker = speaker
        self.listener = listener
        self.transcribe = transcribe
        self.answer_window_s = answer_window_s
        self._last_text = ""  # assistant's latest narration this turn, used as the spoken reason
        self._perm_lock = asyncio.Lock()  # parallel tool calls must not talk over each other
        # Held while the user's own turn is being recorded and while a permission prompt owns the
        # mic, so the two never overlap. Order: _turn_lock, then _perm_lock, then the listener's
        # recorder lock; nothing takes them the other way round.
        self._turn_lock = asyncio.Lock()
        self._perm_reg_lock = threading.Lock()  # guards the web-answer registry below (web threads call in)
        self._perm_requests: list = []
        self._perm_done: OrderedDict = OrderedDict()  # finished web answer id -> who answered
        self._perm_seq = 0
        self._last_activity = ""  # describe_tool of the latest tool call: what a cancel interrupted
        self._cancel_note = ""  # set by the cancel tool; spoken in place of an empty interrupted result
        self.project_dir = str(Path(project_dir))
        state = _load_state()
        # Sessions are stored per working folder: only resume one saved from this same folder.
        same_project = state.get("project_dir") == self.project_dir
        resume_id = state.get("session_id") if resume and same_project else None
        self.allowed_dirs = [str(Path(d)) for d in allowed_dirs or []]
        prompt = (
            VOICE_PROMPT
            + f"\n\nThe source code of this voice interface is in {ROOT}. Voice and model switches "
            "(\"switch to the Ryan voice\", \"use Sonnet\") are handled by Jarvis before they reach you."
        )
        prompt += (
            "\n\nWhen the user asks to look at, review, or be walked through a change or file, use "
            f"{SHOW_CODE_TOOL} to put one chunk on screen at a time. Say one short spoken sentence "
            "BEFORE each call (narration before tool calls is spoken aloud), then explain why that "
            "chunk matters in plain spoken sentences, then pause and let the user react before "
            "moving to the next chunk. Highlight ONLY the exact lines that changed or matter: a "
            "single line when one line changed, never the surrounding function. Never read code "
            "aloud symbol by symbol; describe what it does. If the tool says only the cursor moved, "
            "describe the line range verbally. When the user asks you to run something, use "
            f"{RUN_IN_TERMINAL_TOOL} so they see the output in their own VS Code terminal; it asks "
            "their spoken permission first, and the output is not returned to you."
        )
        prompt += (
            "\n\nThe user can say \"hey Jarvis\" while you are still working; the work keeps running "
            "and their words arrive as a new message. Talk to them first: answer, take the new "
            f"instruction, or tell them where things stand. Only call {CANCEL_TOOL} if they want the "
            "work stopped or it is clearly the right thing to do (a task_id stops just that background "
            "task; no task_id interrupts the whole turn). Stopped work can leave edits half-finished, "
            "so tell the user what was stopped and what to check. "
            f"{LIST_TASKS_TOOL} lists the running tasks with their ids, for stopping one."
        )
        if self.allowed_dirs:
            prompt += " Besides the working directory you may also work in: " + ", ".join(self.allowed_dirs) + "."
        self.options = ClaudeAgentOptions(
            cwd=str(Path(project_dir)),
            add_dirs=self.allowed_dirs,
            # Default is 1 MB; a single screenshot read comes back as one base64 message bigger than that.
            max_buffer_size=64 * 1024 * 1024,
            system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
            setting_sources=None,  # same as the CLI: user + project + local
            mcp_servers={"jarvis": build_showcode_server(self.project_dir),
                         "jarvis_control": build_cancel_server(lambda: self.client, self._on_interrupt),  # client read at call time
                         "jarvis_browser": build_browser_server()},
            allowed_tools=[SHOW_CODE_TOOL, CANCEL_TOOL, LIST_TASKS_TOOL, BROWSER_TOOL],  # read-only: skip the voice yes/no prompt. run_in_terminal and browser_allow_site are deliberately NOT here: they must ask.
            can_use_tool=self.ask_permission,
            permission_mode=PERMISSION_MODE,
            model=model,
            resume=resume_id,
            settings=statusline_settings(),
        )
        self._usage_poller = None
        self.client = ClaudeSDKClient(self.options)
        self._reader_task: asyncio.Task | None = None
        self._outstanding: deque[asyncio.Future] = deque()  # one future per unanswered ask()
        self._turn_active = False
        self._turn_started = 0.0
        self._bg_stop: threading.Event | None = None  # set to cut off background speech in progress
        self._flush_lock = asyncio.Lock()  # one background flush at a time
        self._background_queue: list[str] = []  # background replies waiting to be spoken
        self._flush_task: asyncio.Task | None = None
        self.on_background_spoken = None  # main loop sets: called after self-started background speech ended normally
        self.hold_background = False  # caller sets while the user is talking / being answered
        self.dictation_busy = None  # main loop sets: callable, True while a dictation (or its notes) owns the mic
        self.mute_narration = False  # caller sets while the user is recording: narration stays text-only
        self._narration_stops: set[threading.Event] = set()  # stop flags of narration being spoken
        self._turn_seq = 0  # counts turns started by the reader
        self._merge_tasks: set[asyncio.Task] = set()
        self.session_id = resume_id
        self.model: str | None = None  # reported by Claude Code once the first reply arrives
        self._model_choice = model  # what the user selected last (startup model or voice switch)
        self._recover_task: asyncio.Task | None = None
        self._recovering = False
        self._closing = False
        self._streak = 0  # consecutive connection losses with no completed turn in between
        self._bg_discards: set[asyncio.Task] = set()
        self._connected_at = 0.0  # when the current client connected (monotonic)
        self._outage_announced = False  # "lost connection" told for the current outage
        self._unstable_announced = False
        self._loss_times: deque[float] = deque()
        crashreport.register("brain", self.snapshot)

    def snapshot(self) -> dict:
        return {"model": self.model, "model_choice": self._model_choice,
                "session_id": self.session_id, "project_dir": self.project_dir,
                "outstanding_asks": len(self._outstanding), "turn_active": self._turn_active,
                "recovering": self._recovering, "loss_streak": self._streak,
                "reader_alive": bool(self._reader_task and not self._reader_task.done()),
                "background_queue": len(self._background_queue)}

    def _voice_silenced(self, permission: bool = False) -> bool:
        """True if the user turned the voice off (permission prompts may still be spoken)."""
        sp = self.speaker
        if getattr(sp, "dictating", False) is True:
            return True  # the mic is recording a dictation: nothing is spoken, not even a permission prompt
        if not getattr(sp, "muted", False):
            return False
        return not (permission and getattr(sp, "muted_speaks_permission_prompts", True))

    async def _say(self, text: str, stop: threading.Event | None = None, permission: bool = False) -> bool:
        """Speak with a hard deadline so a hung audio device can't wedge the caller.
        Returns True if `stop` cut it off (an interrupt), False otherwise (done, timeout, error).
        permission=True marks a spoken yes/no prompt, which is still spoken while the voice is off."""
        stop = stop or threading.Event()
        if self._voice_silenced(permission):
            return False  # voice off: nothing to play, no "speaking" state
        bus.emit("mode", mode="speaking")
        perm_var = perm_token = None
        try:
            if permission:
                from .speaker import PERMISSION_SPEECH as perm_var
                perm_token = perm_var.set(True)  # to_thread copies the context into the speak thread
            await asyncio.wait_for(asyncio.to_thread(self.speaker.speak, text, stop),
                                   self.speaker.deadline_for(text) + _say_grace_s())
        except asyncio.TimeoutError:
            ui.error("Speaking timed out; moving on without it.")
            stop.set()  # ask the worker thread to give up if it's still alive
            return False
        except asyncio.CancelledError:
            stop.set()
            raise
        finally:
            if perm_token is not None:
                perm_var.reset(perm_token)
            bus.emit("mode", mode="thinking" if self._turn_active else "idle")
        return stop.is_set()

    async def _say_narration(self, text: str):
        """Speak a line said before a tool call, so you hear 'on it' while work happens.
        Shares the permission lock so it never talks over a yes/no question. "hey Jarvis"
        stays live while it plays: stop_speech() sets its stop flag."""
        stop = threading.Event()
        self._narration_stops.add(stop)
        try:
            async with self._perm_lock:
                if stop.is_set() or self.mute_narration:
                    return  # "hey Jarvis" arrived while this was waiting its turn
                await self._say(text, stop)
        except Exception as e:  # audio trouble must never break the turn
            ui.error(f"Couldn't speak narration ({e}).")
        finally:
            self._narration_stops.discard(stop)

    def _dictating(self) -> bool:
        """True while a dictation owns the mic (or its start/stop notes are still being spoken)."""
        if getattr(self.speaker, "dictating", False) is True:
            return True
        fn = self.dictation_busy
        try:
            return bool(fn and fn())
        except Exception:
            return False

    async def _wait_dictation_over(self) -> None:
        """A voice prompt never starts (and never listens) during a dictation: it stays visible on the page
        and phone, and a page answer cancels this wait like any other. Voice resumes once it is over."""
        if not self._dictating():
            return
        ui.status("Permission needed, but you're dictating: answer on the page or phone.")
        while self._dictating():
            await asyncio.sleep(0.25)

    async def _listen(self) -> str:
        if self._dictating():
            return ""
        # priority: the main loop's wake-word listening steps aside so we get the frames
        audio = await asyncio.to_thread(self.listener.record_utterance, self.answer_window_s, True)
        if audio is None:
            return ""
        return await asyncio.to_thread(self.transcribe, audio)

    async def ask_permission(self, tool_name, tool_input, context):
        """Speak what Claude wants to do, listen for yes or no. Keyboard if voice isn't available.
        The web page may answer too (first answer wins), except destructive shell commands."""
        if auto_approve(tool_name, tool_input):
            return await self._ask_permission(tool_name, tool_input, context)
        summary = summarize_request(tool_name, tool_input)
        voice_only = is_destructive_shell(tool_name, tool_input)
        req = self._new_perm_request(tool_name, voice_only)
        bus.emit("permission_request", tool=tool_name, summary=summary, key=req.key, id=req.id,
                 voice_only=voice_only, expires_ts=req.expires_ts)
        push_permission(summary)  # phone alert via ntfy; never raises or blocks
        result = None
        try:
            result = await self._race_permission(req, tool_name, tool_input, context)
            return result
        finally:
            by = self._finish_perm_request(req)
            bus.emit("permission_result", tool=tool_name, allowed=isinstance(result, PermissionResultAllow),
                     key=req.key, id=req.id, by=by)

    # ---- web page answers (one-time ids; first answer wins) ----------------------------------
    def _new_perm_request(self, tool_name, voice_only):
        with self._perm_reg_lock:
            self._perm_seq += 1
            req = _PermRequest(tool_name, f"p{self._perm_seq}", None if voice_only else secrets.token_urlsafe(16),
                               voice_only, asyncio.get_running_loop())
            self._perm_requests.append(req)
        _CURRENT_PERM.set(req)  # the voice path (a child task) finds its request here
        return req

    def _claim(self, req, who) -> bool:
        """Take the answer for `who` ("voice"|"web"). False if somebody else already did."""
        with self._perm_reg_lock:
            if req.state == "pending":
                req.state = who
            return req.state == who

    def _finish_perm_request(self, req) -> str:
        """Retire the request: its id stops working. Returns who answered ("voice"|"web"|"none")."""
        with self._perm_reg_lock:
            by = req.state if req.state in ("voice", "web") else "none"
            if req in self._perm_requests:
                self._perm_requests.remove(req)
            if req.id:
                self._perm_done[req.id] = by
                while len(self._perm_done) > PERM_DONE_KEEP:
                    self._perm_done.popitem(last=False)
        return by

    def resolve_permission(self, nonce, decision) -> str:
        """Called from web server threads: the page's Approve/Deny. Returns "ok", "bad" (malformed),
        "unknown", "expired", "voice_only" or "handled_<voice|web|none>". Only "ok" changes anything.
        Every id comparison is constant time."""
        if (not isinstance(nonce, str) or not 8 <= len(nonce) <= 128 or not nonce.isascii()
                or decision not in ("approve", "deny")):
            return "bad"
        given = nonce.encode("ascii")
        match = None
        with self._perm_reg_lock:
            for req in self._perm_requests:  # no early exit: same work whichever one matches
                if req.id and hmac.compare_digest(req.id.encode("ascii"), given):
                    match = req
            if match is None:
                found = None
                for done_id, by in self._perm_done.items():
                    if hmac.compare_digest(done_id.encode("ascii"), given):
                        found = by
                return f"handled_{found}" if found else "unknown"
            if match.voice_only:
                return "voice_only"
            if match.state != "pending":
                return f"handled_{match.state}"
            if time.monotonic() >= match.expires:
                return "expired"
            match.decision = decision
            match.state = "web"
        try:
            match.loop.call_soon_threadsafe(match.event.set)
        except RuntimeError:  # the loop is gone; the request dies with it
            pass
        return "ok"

    async def _race_permission(self, req, tool_name, tool_input, context):
        """Run the voice (or keyboard) prompt and wait for the page at the same time."""
        voice = asyncio.ensure_future(self._ask_permission(tool_name, tool_input, context))
        waiter = asyncio.ensure_future(req.event.wait())
        try:
            await asyncio.wait({voice, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if voice.done() and self._claim(req, "voice"):
                return voice.result()
            # The page answered first. Stop the spoken prompt / listening (cancel releases the turn
            # lock, perm lock and listener priority through the voice path's own finally blocks).
            voice.cancel()
            await asyncio.gather(voice, return_exceptions=True)
            allowed = req.decision == "approve"
            await self._announce_web(allowed)
            if allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(message="The user denied this from the web page.")
        finally:
            waiter.cancel()
            if not voice.done():  # we were cancelled ourselves (interrupted turn)
                voice.cancel()

    async def _announce_web(self, allowed) -> None:
        """Say who answered. Never talks over the user's recording or another prompt, never raises."""
        msg = WEB_APPROVED_MSG if allowed else WEB_DENIED_MSG
        ui.status(msg)
        if not self.speaker or self._turn_lock.locked() or self._perm_lock.locked():
            return
        try:
            async with self._turn_lock:
                async with self._perm_lock:
                    await self._say(msg)
        except Exception as e:  # audio trouble must not lose the answer
            ui.error(f"Couldn't speak the web answer ({e}).")

    async def _ask_permission(self, tool_name, tool_input, context):
        reason = auto_approve(tool_name, tool_input)
        if reason:
            ui.activity(f"auto-approved: {reason}")
            return PermissionResultAllow()
        if not (self.speaker and self.listener and self.transcribe):
            return await ask_permission_keyboard(tool_name, tool_input, context)
        await self._wait_dictation_over()  # (before any lock is taken: nothing else is held up meanwhile)
        # Never talk over the user's recording (or get our own voice transcribed as their command).
        try:
            await asyncio.wait_for(self._turn_lock.acquire(), PERM_WAIT_S)
        except asyncio.TimeoutError:
            ui.error("Permission prompt skipped: the user was still talking. Denied for safety.")
            return PermissionResultDeny(message="The user was busy speaking, so this was not approved. Ask again.")
        try:
            self._interrupt_background()
            # While the prompt runs, "hey Jarvis" must neither fire nor eat the mic frames.
            guard = self.listener.priority() if self.listener else nullcontext()
            with guard:
                return await self._ask_permission_voice(tool_name, tool_input, context)
        finally:
            self._turn_lock.release()

    @asynccontextmanager
    async def user_turn(self):
        """Hold while recording the user: a permission prompt waits until it ends, and this
        waits for a prompt in progress, so Jarvis's voice and the user's never overlap."""
        async with self._turn_lock:
            yield

    def _on_interrupt(self):
        """The cancel tool interrupted the whole turn: remember what it was doing, so the
        (empty) result can be turned into a spoken confirmation."""
        what = f" I was {self._last_activity}." if self._last_activity else ""
        self._cancel_note = CANCEL_MSG + what

    async def _ask_permission_voice(self, tool_name, tool_input, context):
        async with self._perm_lock:
            try:
                summary = add_reason(summarize_request(tool_name, tool_input), self._last_text)
                ui.status(f"\nPermission needed: {tool_name} {json.dumps(tool_input)[:300]}")
                while True:
                    await self._wait_dictation_over()
                    await self._say(f"{summary} Say yes or no.", permission=True)
                    heard = await self._listen()
                    if not heard and self._dictating():
                        continue  # a dictation took the mic: ask again by voice once it is over
                    answer = parse_yes_no(heard)
                    if answer is None:
                        await self._say("Sorry, yes or no?", permission=True)
                        heard = await self._listen()
                        if not heard and self._dictating():
                            continue
                        answer = parse_yes_no(heard)
                    break
                req = _CURRENT_PERM.get()
                if req is not None and not self._claim(req, "voice"):
                    return None  # the web page answered while we listened: its answer stands
                if answer:
                    await self._say("Okay, going ahead.")
                    return PermissionResultAllow()
                await self._say("Okay, I won't." if answer is False else "I didn't catch that, so I won't.")
                return PermissionResultDeny(message="User denied by voice")
            except Exception as e:  # audio or STT trouble: never crash the session
                ui.error(f"Voice confirmation failed ({e}); falling back to the keyboard.")
                return await ask_permission_keyboard(tool_name, tool_input, context)

    async def start(self):
        await self.client.connect()
        self._connected_at = time.monotonic()
        self._reader_task = asyncio.create_task(self._reader())
        self._start_usage_poller()

    def _start_usage_poller(self) -> None:
        try:
            if getattr(self, "_usage_poller", None) is None and statusline_enabled():
                self._usage_poller = statusline_usage.UsagePoller(lambda u: publish_usage(u))
                self._usage_poller.start()
        except Exception:
            self._usage_poller = None

    def _stop_usage_poller(self) -> None:
        try:
            if getattr(self, "_usage_poller", None):
                self._usage_poller.stop()
        except Exception:
            pass
        self._usage_poller = None

    async def close(self):
        self._stop_usage_poller()
        self._closing = True
        ui.status("Shutting down (closing Claude Code; can take a few seconds)...")
        for task in (self._recover_task, self._reader_task, *self._merge_tasks):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = self._recover_task = None
        self._fail_outstanding(RuntimeError("closed"))
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._discard(self.client)
        if self._bg_discards:  # let each old CLI finish its own terminate/kill escalation
            await asyncio.wait(list(self._bg_discards), timeout=CLOSE_WAIT_S)
        await asyncio.sleep(0)  # let the subprocess transport finish before the loop goes away

    def _discard(self, client):
        """Disconnect a client defensively, in a background task that is never cancelled early
        (the SDK's close() escalates stdin-close -> terminate -> kill, which a cancel would skip).
        Nothing waits on it; errors are ignored. If it still hasn't finished after
        DISCONNECT_TIMEOUT_S, the CLI process is killed as a last resort."""
        task = asyncio.get_running_loop().create_task(self._disconnect_bg(client))
        self._bg_discards.add(task)
        task.add_done_callback(self._bg_discards.discard)
        return task

    async def _disconnect_bg(self, client):
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except asyncio.CancelledError:
            pass
        except BaseException as e:  # includes anyio cancel-scope errors and timeouts
            ui.activity(f"(ignored error while disconnecting: {type(e).__name__}: {e})")
        try:  # last resort: the CLI must not outlive its client
            proc = getattr(getattr(client, "_transport", None), "_process", None)
            if proc is not None and getattr(proc, "returncode", 0) is None:
                ui.activity("old Claude CLI still running after disconnect; killing it")
                proc.kill()
        except Exception as e:
            ui.activity(f"(couldn't kill the old CLI: {type(e).__name__}: {e})")

    async def set_model(self, model: str):
        self._model_choice = model  # a reconnect must keep the user's choice
        if self._recovering:
            return  # applied when the new client is created
        try:
            await asyncio.wait_for(self.client.set_model(model), QUERY_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._on_stream_lost(e)

    # ---- turn accounting -------------------------------------------------------------
    # One long-lived reader task owns client.receive_messages() for the whole session and
    # splits the stream into turns, each ended by a ResultMessage. A turn is either
    #  - a user turn: answers the oldest outstanding ask() (FIFO), or
    #  - a background turn: the CLI woke itself up (e.g. a background subagent finished and
    #    posted a task-notification). Its text is spoken/logged, never returned from ask().
    # Classification: ResultMessage.origin is authoritative when present ("human" = user turn,
    # any other kind = background). When absent we assume the CLI processes input strictly
    # sequentially: a turn that begins while an ask() is outstanding is the user's (oldest
    # waiter first); a turn that begins with nobody waiting is background, and stays
    # background even if a query() is sent mid-flight (that query is queued behind it, so the
    # next turn is the one that answers it). Known gap: a query sent just before a
    # not-yet-started background turn's first message is mis-attributed (without origin).

    async def _reader(self):
        pending_text: list[str] = []
        turn_user: bool | None = None  # provisional classification of the turn in flight
        try:
            async for msg in self.client.receive_messages():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        self.model = msg.data.get("model", self.model)
                    else:  # task_started / task_progress / task_notification / task_updated
                        ingest_system_message(msg)
                        try:  # token ledger: never allowed to break the reader
                            if msg.subtype == "task_notification":
                                from . import token_ledger
                                token_ledger.record_task_notification(msg, self.model)
                        except Exception:
                            pass
                    continue
                if isinstance(msg, UserMessage):  # tool results: success/error flag only, never the output
                    if isinstance(msg.content, list):
                        for blk in msg.content:
                            if isinstance(blk, ToolResultBlock):
                                bus.emit("tool_result", tool_use_id=blk.tool_use_id,
                                         is_error=bool(blk.is_error), parent=msg.parent_tool_use_id)
                    continue
                if isinstance(msg, RateLimitEvent):
                    try:
                        top = usage_from_info(msg.rate_limit_info)
                        publish_usage(top, msg.rate_limit_info)
                        publish_unified(msg.rate_limit_info, top)
                    except Exception:
                        pass
                    continue
                if not isinstance(msg, (AssistantMessage, ResultMessage)):
                    continue
                if (isinstance(msg, AssistantMessage) and getattr(msg, "error", None) == "rate_limit") or \
                        (isinstance(msg, ResultMessage) and getattr(msg, "api_error_status", None) == 429):
                    publish_usage(_rejected_usage())
                if turn_user is None:  # first real message of a new turn
                    turn_user = bool(self._outstanding)
                    self._turn_seq += 1
                    self._turn_active = True
                    self._turn_started = time.monotonic()
                    bus.emit("mode", mode="thinking")
                    if turn_user:
                        self._last_text = ""
                        self._last_activity = ""
                if isinstance(msg, AssistantMessage):
                    await self._on_assistant(msg, pending_text, turn_user)
                else:
                    self._turn_active = False
                    self._streak = 0  # a turn completed: the connection is healthy
                    bus.emit("mode", mode="idle")
                    await self._on_result(msg, pending_text, turn_user)
                    pending_text = []
                    turn_user = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ui.error(f"Claude Code stream failed ({type(e).__name__}: {e}).")
            if self._reader_task is asyncio.current_task():
                self._on_stream_lost(e)
            return
        if self._reader_task is asyncio.current_task():
            self._on_stream_lost(RuntimeError("Claude Code stream ended"))

    # ---- connection loss and recovery ------------------------------------------------------
    # Any failure of the stream (exception, decode error, CLI exiting, failed write) lands in
    # _on_stream_lost, which synchronously resets turn state and resolves any in-flight ask()
    # with a spoken failure (we do NOT re-send it: the CLI may already have acted on it, and a
    # duplicate tool call is worse than asking the user to repeat), then starts _recover() in
    # its own task. _recover keeps retrying with backoff, forever, resuming the same session
    # with the currently selected model; if resuming keeps failing it starts a fresh session.

    def _on_stream_lost(self, exc: BaseException):
        if self._closing or self._recovering:
            return
        self._recovering = True
        now = time.monotonic()
        if self._connected_at and now - self._connected_at >= HEALTHY_S:
            self._streak = 0  # it had been up for a while: an idle CLI exiting is not a crash loop
        self._streak += 1
        if keepawake.resume_suspected():  # the PC just woke from sleep: the dead stream is not a bug
            ui.activity("Claude stream ended right after the PC woke from sleep; reconnecting "
                        "(no crash report).")
        else:
            crashreport.report("claude-connection", exc, "Claude connection lost; reconnecting")
        ui.error(f"Lost the Claude connection ({type(exc).__name__}: {exc}); reconnecting.")
        # Notices: once per outage; if it flaps, say "unstable" once and go quiet until stable.
        self._loss_times.append(now)
        while self._loss_times and now - self._loss_times[0] > UNSTABLE_WINDOW_S:
            self._loss_times.popleft()
        notice = ""
        if len(self._loss_times) >= UNSTABLE_LOSSES:
            if not self._unstable_announced:
                self._unstable_announced, notice = True, UNSTABLE_MSG
                self._outage_announced = False
        else:
            self._unstable_announced = False
            if not self._outage_announced:
                self._outage_announced, notice = True, LOST_MSG
        had_waiters = bool(self._outstanding)
        self._turn_active = False
        self._last_text = ""
        self._cancel_note = ""
        if self._perm_lock.locked():  # a permission prompt died with the connection
            self._perm_lock = asyncio.Lock()
        if self._bg_stop:
            self._bg_stop.set()
        reply = (notice + " " if notice else "") + INTERRUPTED_MSG  # the waiter's reply carries the notice
        while self._outstanding:
            fut = self._outstanding.popleft()
            if not fut.done():
                fut.set_result(reply)
        if notice and not had_waiters:
            self._announce(notice)
        self._recover_task = asyncio.create_task(self._recover())

    def _announce(self, text: str):
        """Show a notice; with a speaker also queue it, spoken when the user is idle.
        Duplicates are not queued, and without a speaker (chat) nothing is queued at all."""
        ui.jarvis(text, "local")
        if not self.speaker or text in self._background_queue:
            return
        self._background_queue.append(text)
        self._schedule_flush()

    def _announce_reconnected(self, fell_back: bool):
        self._connected_at = time.monotonic()
        ui.status("Reconnected to Claude.")
        if fell_back:
            self._announce(FRESH_MSG)
        if self._outage_announced:  # only if the user was told about this outage
            self._outage_announced = False
            if LOST_MSG in self._background_queue:  # never spoken yet: cancel it, say nothing more
                self._background_queue.remove(LOST_MSG)
            elif not fell_back:
                self._announce(BACK_MSG)

    async def _recover(self):
        try:
            await self._recover_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a bug in recovery itself must not strand us
            crashreport.report("claude-recover-bug", e)
            ui.error(f"Reconnect logic failed ({type(e).__name__}: {e}); retrying.")
            self._recovering = False
            self._on_stream_lost(e)

    async def _recover_loop(self):
        old_reader, self._reader_task = self._reader_task, None
        if old_reader and not old_reader.done():
            old_reader.cancel()
            try:
                await old_reader
            except (asyncio.CancelledError, Exception):
                pass
        self._discard(self.client)  # background: don't make recovery wait for the old CLI to die
        if self._streak > 1:  # lost again right after reconnecting: don't spin
            await asyncio.sleep(BACKOFF_S[min(self._streak - 2, len(BACKOFF_S) - 1)])
        use_resume = bool(self.session_id) and self._streak < 3
        fell_back = bool(self.session_id) and not use_resume
        resume_fails = 0
        attempt = 0
        while True:
            attempt += 1
            if fell_back and self.session_id:
                ui.error("Resuming the session keeps failing; starting a fresh one.")
                self.session_id = None
            opts = dataclasses.replace(
                self.options, model=self._model_choice or self.model,
                resume=self.session_id if use_resume else None)
            ui.activity(f"reconnect attempt {attempt} (resume={opts.resume}, model={opts.model})")
            client = ClaudeSDKClient(opts)
            try:
                await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_S)
                break
            except asyncio.CancelledError:
                self._discard(client)  # half-started client (close() during recovery)
                raise
            except Exception as e:
                ui.error(f"Reconnect attempt {attempt} failed ({type(e).__name__}: {e}).")
                crashreport.report("claude-reconnect", e, f"reconnect attempt {attempt}")
                self._discard(client)
                if use_resume:
                    resume_fails += 1
                    if resume_fails >= RESUME_FAIL_AFTER or _RESUME_ERR.search(str(e)):
                        use_resume, fell_back = False, True
                        continue  # retry at once, fresh
                await asyncio.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        self.client = client
        self._reader_task = asyncio.create_task(self._reader())
        self._recovering = False
        self._announce_reconnected(fell_back)

    def _fail_outstanding(self, exc: Exception):
        while self._outstanding:
            fut = self._outstanding.popleft()
            if not fut.done():
                fut.set_exception(exc)

    async def _on_assistant(self, msg: AssistantMessage, pending_text: list[str], user_turn: bool):
        sub = msg.parent_tool_use_id is not None
        if not sub:
            self.model = msg.model
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                # Narration before a tool call is progress, not the answer.
                for t in pending_text:
                    ui.activity(t)
                    if user_turn and self.speaker and t.strip():
                        await self._say_narration(t)
                pending_text.clear()
                ui.activity(("  " if sub else "") + describe_tool(block))
                bus.emit("tool_use", name=block.name, tool_use_id=block.id,
                         description=describe_tool(block), label=human_label(block.name, block.input),
                         parent=msg.parent_tool_use_id)
                if block.name in ("Agent", "Task"):
                    try:
                        from . import token_ledger
                        token_ledger.note_dispatch(block.id, block.input.get("subagent_type", "agent"))
                    except Exception:
                        pass
                    bus.emit("agent_dispatch", tool_use_id=block.id,
                             subagent_type=block.input.get("subagent_type", "agent"),
                             description=block.input.get("description", ""),
                             parent=msg.parent_tool_use_id)
                if block.name != CANCEL_TOOL:
                    self._last_activity = describe_tool(block)
            elif isinstance(block, TextBlock) and not sub:
                pending_text.append(block.text)
                if user_turn and block.text.strip():
                    self._last_text = block.text

    async def _on_result(self, msg: ResultMessage, pending_text: list[str], user_turn: bool):
        self.session_id = msg.session_id
        try:  # token ledger: never allowed to break the reader
            from . import token_ledger
            token_ledger.record_result(msg, self.model)
        except Exception:
            pass
        update_state(STATE_PATH, session_id=msg.session_id, project_dir=self.project_dir)  # keeps voice_muted
        reply = msg.result or "\n\n".join(pending_text)
        if msg.terminal_reason in INTERRUPTED_REASONS or not reply.strip():
            reply = self._cancel_note  # cancelled or empty: say only the cancel confirmation, if any
        self._cancel_note = ""
        kind = (msg.origin or {}).get("kind")
        provisional = user_turn
        if kind is not None:
            user_turn = kind == "human"
        if msg.is_error:
            ui.error("Claude Code returned an error:")
        waiters = len(self._outstanding)
        resolved = False
        if user_turn and self._outstanding:
            fut = self._outstanding.popleft()
            if not fut.done():  # a cancelled ask() just discards its reply
                fut.set_result(reply)
                resolved = True
            if self._outstanding:
                self._watch_merged_turn()
        elif reply.strip():
            ui.jarvis(reply, self.model)  # logged like any other JARVIS line
            self._background_queue.append(reply)
            self._schedule_flush()
        # Diagnostics for "silent reply after an interruption": how was this result attributed?
        ui.activity(f"result: origin={msg.origin!r} provisional_user_turn={provisional} "
                    f"user_turn={user_turn} outstanding={waiters}->{len(self._outstanding)} "
                    f"resolved_future={resolved}")

    def _watch_merged_turn(self):
        """Another ask() was sent while this turn ran, so its waiter is still pending. The CLI
        may have folded that message into the turn that just ended, in which case no turn will
        ever answer it: give it MERGE_GRACE_S to start, then release it with an empty reply."""
        waiting = list(self._outstanding)

        async def check():
            await asyncio.sleep(MERGE_GRACE_S)
            # A turn still running may answer it: keep waiting while one is (it ends or goes stale).
            while self._turn_active and time.monotonic() - self._turn_started < TURN_STALE_S:
                await asyncio.sleep(MERGE_GRACE_S)
            for fut in waiting:
                if fut in self._outstanding:
                    self._outstanding.remove(fut)
                    if not fut.done():
                        fut.set_result("")
            self._schedule_flush()

        task = asyncio.get_running_loop().create_task(check())
        self._merge_tasks.add(task)
        task.add_done_callback(self._merge_tasks.discard)

    def stop_speech(self):
        """"hey Jarvis": cut off background speech and any narration being spoken."""
        self.interrupt_background()
        for stop in list(self._narration_stops):
            stop.set()

    def _can_speak_background(self) -> bool:
        turn_busy = self._turn_active and time.monotonic() - self._turn_started < TURN_STALE_S
        return not (self.hold_background or self._outstanding or turn_busy
                    or self._perm_lock.locked())

    def interrupt_background(self):
        """Cut off background speech in progress (wake word, permission prompt, new ask)."""
        if self._bg_stop:
            self._bg_stop.set()

    _interrupt_background = interrupt_background

    def _schedule_flush(self):
        if (self.speaker and self._background_queue and self._can_speak_background()
                and not (self._flush_task and not self._flush_task.done())):
            self._flush_task = asyncio.create_task(self._flush_and_notify())

    async def _flush_and_notify(self):
        """Self-started flush (nobody is waiting on it): tell the main loop if it spoke."""
        if await self.flush_background() and self.on_background_spoken:
            try:
                self.on_background_spoken()
            except Exception as e:  # a callback problem must not break the flush slot
                ui.error(f"Follow-up hand-off failed ({e}).")

    async def flush_background(self) -> bool:
        """Speak queued background-turn replies, if the user isn't busy. The caller sets
        hold_background while the user is speaking/being answered, then calls this.
        True only if something was spoken and playback ended normally (not cut off by a
        wake word, permission prompt or new ask): the cue to listen for a follow-up."""
        from .speaker import for_speech  # lazy: pulls in audio libs
        spoke = interrupted = False
        # Not under _perm_lock: a stuck speaker must never block permission prompts. Instead
        # a permission prompt (or a new ask) cuts background speech off via _bg_stop.
        async with self._flush_lock:
            try:
                while self.speaker and self._background_queue and self._can_speak_background():
                    text = self._background_queue.pop(0)
                    self._bg_stop = threading.Event()
                    try:
                        spoken = for_speech(text)
                        if not spoken.strip():
                            continue  # nothing to say (finally still clears _bg_stop)
                        said_interrupted = await self._say(spoken, self._bg_stop)
                        if self._voice_silenced():
                            continue  # voice is off (or was just turned off): dropped, it is in the text
                        if said_interrupted:
                            # Interrupted (not failed): speak it at the next flush.
                            self._background_queue.insert(0, text)
                            interrupted = True
                            break
                        spoke = True
                    except Exception as e:  # audio trouble must not end the flush loop
                        ui.error(f"Couldn't speak background reply ({e}); skipped (it is in the log).")
                    finally:
                        self._bg_stop = None
            finally:
                if self._flush_task is asyncio.current_task():
                    self._flush_task = None  # always free the slot for the next background reply
        return spoke and not interrupted

    async def ask(self, text: str) -> str:
        """Send one user turn; activity is shown by the reader; return the final reply.
        Never raises for connection trouble: returns a short message instead."""
        if self._recovering:
            return RECONNECTING_MSG
        if self._reader_task is None or self._reader_task.done():
            self._on_stream_lost(RuntimeError("Claude Code stream is not running"))
            return RECONNECTING_MSG
        self._turn_active = False  # a stuck flag from a lost ResultMessage must not outlive a new ask
        self._interrupt_background()
        fut = asyncio.get_running_loop().create_future()
        self._outstanding.append(fut)
        try:
            await asyncio.wait_for(self.client.query(self._human_prompt(text)), QUERY_TIMEOUT_S)
        except asyncio.CancelledError:
            if fut in self._outstanding:
                self._outstanding.remove(fut)
            raise
        except Exception as e:  # the write failed: the connection is gone
            if fut in self._outstanding:
                self._outstanding.remove(fut)
            self._on_stream_lost(e)
            return LOST_MSG + " " + INTERRUPTED_MSG
        try:
            # shield: on timeout wait_for must not cancel a future the reader may still resolve
            return await asyncio.wait_for(asyncio.shield(fut), ASK_TIMEOUT_S)
        except asyncio.TimeoutError:
            # No human-classified result ever came (misattributed turn?). Never hang the
            # main loop, and never leave a dead entry that blocks background speech.
            ui.error(f"No reply to the user's request within {ASK_TIMEOUT_S:.0f}s; giving up on it "
                     f"(outstanding={len(self._outstanding)}, turn_active={self._turn_active}).")
            if fut in self._outstanding:
                self._outstanding.remove(fut)
            fut.cancel()
            self._turn_active = False
            self._schedule_flush()
            return NO_REPLY_MSG
        except asyncio.CancelledError:
            if fut in self._outstanding:
                self._outstanding.remove(fut)
            raise

    @staticmethod
    async def _human_prompt(text: str):
        """The user's prompt as a stream message stamped origin=human, so the CLI attributes
        the resulting turn to the user (the SDK honors only the 'human' kind from a host)."""
        yield {"type": "user", "message": {"role": "user", "content": text},
               "parent_tool_use_id": None, "origin": {"kind": "human"}}
