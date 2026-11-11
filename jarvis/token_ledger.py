"""Token ledger: an append-only record of what each Jarvis turn (and subagent) cost, so the workflow can be
made cheaper. Report with:  .venv\\Scripts\\python.exe scripts\\token_report.py

File: logs\\token_ledger.jsonl, one JSON object per line, UTF-8. Never any conversation text, prompts,
tool input/output, secrets, tokens or URLs: numbers and short labels only.

kind "turn" (one per ResultMessage, i.e. per Jarvis turn):
    ts (ISO-8601 local, seconds), kind, model, task, input_tokens, output_tokens, cache_read,
    cache_creation, cost_usd, cost_total_usd, sid, duration_ms, num_turns
    cost_total_usd is the SDK's CUMULATIVE session cost at that turn (ResultMessage.total_cost_usd);
    cost_usd is the DELTA since the previous turn of the same session (never negative; a decrease or a new
    session starts a new total, so delta = the value). sid is a short hash of the session id (no raw id).
    OLD rows (before this change) lack cost_total_usd and hold the cumulative value in cost_usd; the report
    converts them to deltas on the fly (scripts\token_report.py, migrate_old_costs) without rewriting the file.
kind "subagent" (one per finished task_notification that carries usage; the SDK does expose it):
    ts, kind, agent_type, model, task, tokens (total_tokens), tool_uses, duration_ms, status
    A subagent's tokens may also be inside the parent turn's totals, so the report keeps kinds separate
    and never adds subagent tokens onto turn totals.
Missing numbers are 0 / null. Extra keys are ignored by the report.

task: the tag in logs\\current_task.txt (first line, max 80 chars), set with
    scripts\\token_report.py --set-task "name"   (--set-task "" clears it). Read on every write.
Manual subagent entry: scripts\\token_report.py --add-subagent TYPE --tokens N [--tool-uses N] [--model M]

Safety: every public write function swallows all errors (returns False), writes under a lock, and never
blocks the reader. Disable the default-path write with `token_ledger: false` in config.yaml or env
JARVIS_TOKEN_LEDGER=0 (also false/no/off). Test suites set JARVIS_TOKEN_LEDGER=0 (or pass explicit temp paths)
so they never pollute the real file; no other variable (e.g. JARVIS_NOTIFY_PHONE) affects the ledger.
Env JARVIS_TOKEN_LEDGER_PATH overrides the default ledger file location (read at call time).
The config value is cached for 60 s. The tool_use_id -> agent type map is also persisted (last 200 entries) to
logs\token_dispatch.json (env JARVIS_TOKEN_DISPATCH_PATH overrides) so notifications after a restart get labelled.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

try:
    from .config import LOG_DIR
except Exception:  # pragma: no cover - config unavailable must not matter
    LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

LEDGER_PATH = Path(LOG_DIR) / "token_ledger.jsonl"
TASK_PATH = Path(LOG_DIR) / "current_task.txt"

_lock = threading.Lock()
DISPATCH_PATH = Path(LOG_DIR) / "token_dispatch.json"
_dispatch_types: dict[str, str] = {}  # tool_use_id -> subagent_type, to label task notifications
_MAX_DISPATCH = 200
_CFG_TTL = 60.0
_cfg_cache: list = [0.0, True, False]  # [monotonic stamp, value, valid]
_cost_state: dict[str, dict[str, float]] = {}  # ledger path -> {sid -> last cumulative cost}
_TAIL_BYTES = 256 * 1024


def dispatch_path() -> Path:
    override = os.environ.get("JARVIS_TOKEN_DISPATCH_PATH", "").strip()
    return Path(override) if override else DISPATCH_PATH


def ledger_path() -> Path:
    """The default ledger file: env JARVIS_TOKEN_LEDGER_PATH if set, else the default LEDGER_PATH under logs/."""
    override = os.environ.get("JARVIS_TOKEN_LEDGER_PATH", "").strip()
    return Path(override) if override else LEDGER_PATH


def enabled() -> bool:
    if os.environ.get("JARVIS_TOKEN_LEDGER", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    now = time.monotonic()
    if _cfg_cache[2] and now - _cfg_cache[0] < _CFG_TTL:
        return bool(_cfg_cache[1])
    try:
        from .config import load_config
        val = bool(load_config().get("token_ledger", True))
    except Exception:
        val = True
    _cfg_cache[0], _cfg_cache[1], _cfg_cache[2] = now, val, True
    return val


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_task(path: Path | None = None) -> str | None:
    try:
        t = (path or TASK_PATH).read_text(encoding="utf-8").strip().splitlines()
        t = t[0].strip()[:80] if t else ""
        return t or None
    except Exception:
        return None


def set_task(name: str, path: Path | None = None) -> None:
    p = path or TASK_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    name = (name or "").strip()
    if name:
        p.write_text(name[:80] + "\n", encoding="utf-8")
    elif p.exists():
        p.unlink()


def append(record: dict, path: Path | None = None) -> bool:
    """Append one record. Never raises. True if written."""
    try:
        p = path or ledger_path()
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:
        return False


def _int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _sid(msg) -> str:
    sid = getattr(msg, "session_id", None)
    return hashlib.sha1(str(sid).encode("utf-8", "replace")).hexdigest()[:10] if sid else ""


def _load_cost_state(p: Path) -> dict[str, float]:
    """Per-session last cumulative cost for ledger p; recovered once from the ledger tail. Call under _lock."""
    key = str(p)
    st = _cost_state.get(key)
    if st is not None:
        return st
    st = {}
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > _TAIL_BYTES:
            lines = lines[1:]  # first line may be cut
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("kind") == "turn" and isinstance(r.get("cost_total_usd"), (int, float)):
                st[str(r.get("sid") or "")] = float(r["cost_total_usd"])
    except Exception:
        pass
    _cost_state[key] = st
    return st


def _cost_delta(p: Path | None, sid: str, cum: float | None) -> float | None:
    """Delta since the previous turn of this session (state tracked per ledger path); None if no cost."""
    if cum is None:
        return None
    if p is None:
        return max(cum, 0.0)
    with _lock:
        st = _load_cost_state(p)
        prev = st.get(sid)
        st[sid] = cum
    if prev is None or cum < prev:
        return max(cum, 0.0)
    return max(cum - prev, 0.0)


def turn_record(msg, model: str | None = None, task_path: Path | None = None, cost_path: Path | None = None) -> dict:
    """Build a turn row. cost_usd is the delta vs the previous turn of the session when cost_path (the ledger
    that will receive the row) is given; without it there is no history and delta = the cumulative value."""
    u = getattr(msg, "usage", None) or {}
    if not isinstance(u, dict):
        u = {}
    mu = getattr(msg, "model_usage", None)
    if isinstance(mu, dict) and mu:
        try:  # the model that produced the most output; keys are model ids
            model = max(mu, key=lambda k: _int((mu[k] or {}).get("outputTokens", (mu[k] or {}).get("output_tokens"))))
        except Exception:
            pass
    cost = getattr(msg, "total_cost_usd", None)
    cum = float(cost) if isinstance(cost, (int, float)) else None
    sid = _sid(msg)
    return {
        "ts": _now(), "kind": "turn", "model": model, "task": read_task(task_path),
        "input_tokens": _int(u.get("input_tokens")), "output_tokens": _int(u.get("output_tokens")),
        "cache_read": _int(u.get("cache_read_input_tokens")),
        "cache_creation": _int(u.get("cache_creation_input_tokens")),
        "cost_usd": _cost_delta(cost_path, sid, cum), "cost_total_usd": cum, "sid": sid,
        "duration_ms": _int(getattr(msg, "duration_ms", 0)), "num_turns": _int(getattr(msg, "num_turns", 0)),
    }


def record_result(msg, model: str | None = None, path: Path | None = None) -> bool:
    """Hook for brain._on_result. Never raises."""
    try:
        if path is None and not enabled():
            return False
        p = path or ledger_path()
        return append(turn_record(msg, model, cost_path=p), p)
    except Exception:
        return False


def _read_dispatch(p: Path) -> dict[str, str]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return {str(k): str(v)[:60] for k, v in d.items()}
    except Exception:
        pass
    return {}


def _write_dispatch(p: Path, d: dict[str, str]) -> None:
    """Atomic write of the last _MAX_DISPATCH entries. Never raises."""
    try:
        items = list(d.items())[-_MAX_DISPATCH:]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(dict(items), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def note_dispatch(tool_use_id, subagent_type, path: Path | None = None) -> None:
    """Remember which agent type a dispatch (tool_use id) was, to label its completion; persisted to the dispatch
    file (explicit path, or the default when the ledger is enabled). Never raises."""
    try:
        if tool_use_id:
            tid, typ = str(tool_use_id), str(subagent_type or "agent")[:60]
            with _lock:
                _dispatch_types.pop(tid, None)
                if len(_dispatch_types) >= _MAX_DISPATCH:
                    _dispatch_types.pop(next(iter(_dispatch_types)), None)
                _dispatch_types[tid] = typ
                if path is not None or enabled():
                    p = path or dispatch_path()
                    d = _read_dispatch(p)
                    d.pop(tid, None)
                    d[tid] = typ
                    _write_dispatch(p, d)
    except Exception:
        pass


def _take_dispatch(tuid, path: Path | None, persist: bool) -> str | None:
    tid = str(tuid)
    with _lock:
        atype = _dispatch_types.pop(tid, None)
        p = path or dispatch_path()
        d = _read_dispatch(p)
        if tid in d:
            atype = atype or d.pop(tid)
            d.pop(tid, None)
            if persist:
                _write_dispatch(p, d)
    return atype


def record_task_notification(msg, model: str | None = None, path: Path | None = None,
                             task_path: Path | None = None, dispatch_file: Path | None = None) -> bool:
    """Hook for a TaskNotificationMessage that carries usage. Never raises."""
    try:
        if path is None and not enabled():
            return False
        u = getattr(msg, "usage", None)
        if not isinstance(u, dict):
            return False
        tuid = getattr(msg, "tool_use_id", None)
        atype = _take_dispatch(tuid, dispatch_file, dispatch_file is not None or path is None) if tuid else None
        return append({
            "ts": _now(), "kind": "subagent", "agent_type": atype or "unknown", "model": model,
            "task": read_task(task_path), "tokens": _int(u.get("total_tokens")),
            "tool_uses": _int(u.get("tool_uses")), "duration_ms": _int(u.get("duration_ms")),
            "status": str(getattr(msg, "status", "") or "")[:20],
        }, path)
    except Exception:
        return False


# ---- reading and aggregation (used by scripts/token_report.py) -------------------------------

def load(path: Path | None = None, since: str | None = None) -> list[dict]:
    """All valid records (dicts), skipping blank/corrupt lines; optional since='YYYY-MM-DD'."""
    out: list[dict] = []
    try:
        with open(path or ledger_path(), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not isinstance(r, dict):
                    continue
                if since and str(r.get("ts", ""))[:10] < since:
                    continue
                out.append(r)
    except Exception:
        pass
    return out


def _rec_tokens(r: dict) -> dict:
    if r.get("kind") == "subagent":
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": _int(r.get("tokens"))}
    i, o = _int(r.get("input_tokens")), _int(r.get("output_tokens"))
    cr, cc = _int(r.get("cache_read")), _int(r.get("cache_creation"))
    return {"input": i, "output": o, "cache_read": cr, "cache_creation": cc, "total": i + o + cr + cc}


def _add(bucket: dict, r: dict) -> None:
    t = _rec_tokens(r)
    for k, v in t.items():
        bucket[k] = bucket.get(k, 0) + v
    c = r.get("cost_usd")
    bucket["cost_usd"] = bucket.get("cost_usd", 0.0) + (float(c) if isinstance(c, (int, float)) else 0.0)
    bucket["count"] = bucket.get("count", 0) + 1
    bucket["duration_ms"] = bucket.get("duration_ms", 0) + _int(r.get("duration_ms"))


def aggregate(records: list[dict]) -> dict:
    """Totals plus groupings by kind, task, agent (subagent type or 'turn'), model and day.
    Each group maps key -> {count, input, output, cache_read, cache_creation, total, cost_usd, duration_ms}."""
    res: dict = {"totals": {}, "by_kind": {}, "by_task": {}, "by_agent": {}, "by_model": {}, "by_day": {}}
    for r in records:
        kind = str(r.get("kind") or "turn")
        keys = {
            "by_kind": kind,
            "by_task": str(r.get("task") or "(untagged)"),
            "by_agent": str(r.get("agent_type") or "unknown") if kind == "subagent" else kind,
            "by_model": str(r.get("model") or "unknown"),
            "by_day": str(r.get("ts", ""))[:10] or "unknown",
        }
        _add(res["totals"], r)
        for g, k in keys.items():
            _add(res[g].setdefault(k, {}), r)
    return res


def _fmt_table(title: str, group: dict, limit: int = 15) -> str:
    rows = sorted(group.items(), key=lambda kv: (-kv[1].get("total", 0), kv[0]))[:limit]
    lines = [title, f"  {'name':<28}{'n':>6}{'tokens':>14}{'output':>12}{'cost$ (delta)':>14}"]
    for k, b in rows:
        lines.append(f"  {k[:27]:<28}{b.get('count', 0):>6}{b.get('total', 0):>14,}{b.get('output', 0):>12,}"
                     f"{b.get('cost_usd', 0.0):>14.2f}")
    return "\n".join(lines)


def format_report(agg: dict, since: str | None = None) -> str:
    t = agg["totals"]
    if not t:
        return "No token ledger records" + (f" since {since}" if since else "") + "."
    head = (f"Token ledger{' since ' + since if since else ''}: {t['count']} records, {t['total']:,} tokens "
            f"({t['output']:,} output), session cost deltas ${t['cost_usd']:.2f}")
    parts = [head, "(subagent tokens may also be counted inside their parent turn; kinds are not summed)", ""]
    parts.append(_fmt_table("By kind", agg["by_kind"]))
    parts.append(_fmt_table("By task", agg["by_task"]))
    parts.append(_fmt_table("By process (subagent type / turn)", agg["by_agent"]))
    parts.append(_fmt_table("By model", agg["by_model"]))
    days = dict(sorted(agg["by_day"].items(), reverse=True))
    parts.append(_fmt_table("By day (newest first)", days, limit=14).replace("name ", "day  ", 1))
    return "\n\n".join(parts)
