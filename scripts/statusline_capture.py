"""Claude Code statusLine command: capture subscription usage for Jarvis.

Reads the statusline JSON from stdin, keeps ONLY a whitelist of fields (rate-limit percentages and reset
times, model display name, timestamp) and writes them atomically to logs/statusline_usage.json
(override with env JARVIS_STATUSLINE_FILE). Prints a short status string. Never raises, always exits 0.
Stdlib only and imports kept minimal so it starts fast.
"""
import json
import os
import sys
import time

MAX_INPUT = 2_000_000  # bytes; anything bigger is not a statusline document


def _default_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "logs", "statusline_usage.json")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _window(d):
    if not isinstance(d, dict):
        return None
    pct = _num(d.get("used_percentage"))
    if pct is None:
        return None
    resets = _num(d.get("resets_at"))
    return {"used_percentage": round(max(0.0, min(100.0, float(pct))), 1),
            "resets_at": int(resets) if resets is not None else None}


def extract(doc) -> dict:
    """Whitelisted subset of a statusline document."""
    out = {"updated": int(time.time()), "model": None, "rate_limits": {}}
    if not isinstance(doc, dict):
        return out
    model = doc.get("model")
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str):
        out["model"] = name[:80]
    rl = doc.get("rate_limits")
    if isinstance(rl, dict):
        for key in ("five_hour", "seven_day"):
            w = _window(rl.get(key))
            if w is not None:
                out["rate_limits"][key] = w
    return out


def status_text(data: dict) -> str:
    parts = []
    rl = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key)
        if w:
            parts.append(f"{label} {w['used_percentage']:.0f}%")
    return " | ".join(parts)


def write_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 0
        data = extract(json.loads(raw.decode("utf-8", "replace")))
        # Without any window keep the previous good file rather than overwriting it with nothing.
        if data["rate_limits"]:
            try:
                write_atomic(os.environ.get("JARVIS_STATUSLINE_FILE") or _default_path(), data)
            except Exception:
                pass
        text = status_text(data)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
