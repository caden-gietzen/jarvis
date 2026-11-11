"""Token ledger report and tagging. See jarvis/token_ledger.py for the file format.

Run:  .venv\\Scripts\\python.exe scripts\\token_report.py [--since 2026-11-07] [--json]
      ... --set-task "name"            tag following turns with a task (empty string clears)
      ... --add-subagent TYPE --tokens N [--tool-uses N] [--duration-ms N] [--model M]   manual entry
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import token_ledger as tl  # noqa: E402


def migrate_old_costs(records: list[dict]) -> list[dict]:
    """One-time, in-memory migration (the file is never rewritten): turn rows lacking cost_total_usd are OLD rows
    whose cost_usd is the cumulative session cost; convert consecutive old rows to deltas in file order
    (a decrease starts a new total, never negative). Returns new list; rows with cost_total_usd are untouched."""
    out: list[dict] = []
    prev = None
    for r in records:
        if r.get("kind", "turn") != "turn" or "cost_total_usd" in r:
            prev = None if r.get("kind", "turn") == "turn" else prev
            out.append(r)
            continue
        c = r.get("cost_usd")
        if not isinstance(c, (int, float)):
            out.append(r)
            continue
        c = float(c)
        delta = c if prev is None or c < prev else c - prev
        prev = c
        n = dict(r)
        n["cost_usd"] = max(delta, 0.0)
        n["cost_total_usd"] = c
        out.append(n)
    return out


def main(argv=None, ledger: Path | None = None, task_path: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description="Jarvis token ledger report")
    ap.add_argument("--since", help="only records on/after this date, YYYY-MM-DD")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--set-task", metavar="NAME", help="set (or with '' clear) the current task tag")
    ap.add_argument("--add-subagent", metavar="TYPE", help="add a manual subagent entry")
    ap.add_argument("--tokens", type=int, default=0)
    ap.add_argument("--tool-uses", type=int, default=0)
    ap.add_argument("--duration-ms", type=int, default=0)
    ap.add_argument("--model")
    a = ap.parse_args(argv)

    if a.set_task is not None:
        tl.set_task(a.set_task, task_path)
        print(f"task set to {a.set_task!r}" if a.set_task.strip() else "task cleared")
        return 0
    if a.add_subagent:
        ok = tl.append({"ts": tl._now(), "kind": "subagent", "agent_type": a.add_subagent[:60], "model": a.model,
                        "task": tl.read_task(task_path), "tokens": a.tokens, "tool_uses": a.tool_uses,
                        "duration_ms": a.duration_ms, "status": "manual"}, ledger)
        print("added" if ok else "write failed")
        return 0 if ok else 1

    recs = migrate_old_costs(tl.load(ledger))  # migrate over ALL rows, then filter
    if a.since:
        recs = [r for r in recs if str(r.get("ts", ""))[:10] >= a.since]
    agg = tl.aggregate(recs)
    if a.json:
        print(json.dumps(agg, indent=2, sort_keys=True))
    else:
        print(tl.format_report(agg, a.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
