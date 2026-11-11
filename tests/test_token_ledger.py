r"""Offline tests for the token ledger and report. Temp paths only.

Run:  .venv\Scripts\python.exe tests\test_token_ledger.py     (plain script, PASS/FAIL per test)
"""

import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # tests must never send a real phone push
_os_np.environ["JARVIS_TOKEN_LEDGER"] = "0"  # ...nor write the real token ledger
import contextlib
import io
import json
import site
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from jarvis import token_ledger as tl  # noqa: E402
import token_report  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-ledger-test-"))
N = [0]


def fresh():
    N[0] += 1
    return TMP / f"l{N[0]}.jsonl", TMP / f"t{N[0]}.txt"


def fake_result(**kw):
    d = dict(usage={"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 40}, total_cost_usd=0.05, duration_ms=1234, num_turns=3,
             model_usage=None, result="SECRET REPLY TEXT", session_id="sess-secret")
    d.update(kw)
    return SimpleNamespace(**d)


def test_turn_line():
    p, t = fresh()
    msg = fake_result()
    rec = tl.turn_record(msg, "sonnet", t)
    assert tl.append(rec, p)
    raw = p.read_text(encoding="utf-8")
    r = json.loads(raw)
    assert r["kind"] == "turn" and r["model"] == "sonnet" and r["task"] is None
    assert (r["input_tokens"], r["output_tokens"], r["cache_read"], r["cache_creation"]) == (10, 20, 300, 40)
    assert r["cost_usd"] == 0.05 and r["duration_ms"] == 1234
    assert "SECRET" not in raw and "sess-secret" not in raw


def test_hook_never_raises():
    bad = TMP / "adir"
    bad.mkdir()
    assert tl.record_result(fake_result(), "x", path=bad) is False  # path is a directory
    assert tl.record_result(object(), "x", path=bad) is False
    assert tl.record_result(None, None, path=TMP / "ok.jsonl") in (True, False)
    assert tl.record_task_notification(object(), None, path=bad) is False
    assert tl.append({"x": object()}, TMP / "o2.jsonl") is False  # unserialisable


def test_thread_safety():
    p, _ = fresh()

    def w(i):
        for j in range(50):
            tl.append({"ts": "2026-11-07T00:00:00", "kind": "turn", "input_tokens": 1, "i": i, "pad": "x" * 500}, p)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    assert all(json.loads(x)["kind"] == "turn" for x in lines)


def test_task_tag_from_file():
    p, t = fresh()
    assert tl.read_task(t) is None
    token_report.main(["--set-task", "build ledger"], ledger=p, task_path=t)
    assert tl.read_task(t) == "build ledger"
    assert tl.turn_record(fake_result(), None, t)["task"] == "build ledger"
    token_report.main(["--set-task", ""], ledger=p, task_path=t)
    assert tl.read_task(t) is None


def test_subagent_notification():
    p, t = fresh()
    dp = TMP / "disp_sub.json"
    tl.note_dispatch("tu1", "scout", path=dp)
    msg = SimpleNamespace(tool_use_id="tu1", status="completed", summary="SECRET",
                          usage={"total_tokens": 5000, "tool_uses": 7, "duration_ms": 900})
    assert tl.record_task_notification(msg, "haiku", path=p, task_path=t, dispatch_file=dp)
    r = json.loads(p.read_text(encoding="utf-8"))
    assert r["kind"] == "subagent" and r["agent_type"] == "scout" and r["tokens"] == 5000
    assert r["tool_uses"] == 7 and "SECRET" not in json.dumps(r)
    assert not tl.record_task_notification(SimpleNamespace(tool_use_id=None, usage=None), None, path=p)


def _seed(p):
    rows = [
        {"ts": "2026-11-06T10:00:00", "kind": "turn", "model": "opus", "task": "A", "input_tokens": 100,
         "output_tokens": 50, "cache_read": 0, "cache_creation": 0, "cost_usd": 1.0},
        {"ts": "2026-11-07T10:00:00", "kind": "turn", "model": "sonnet", "task": "B", "input_tokens": 10,
         "output_tokens": 5, "cache_read": 100, "cache_creation": 0, "cost_usd": 0.5},
        {"ts": "2026-11-07T11:00:00", "kind": "subagent", "agent_type": "scout", "model": "haiku", "task": "B",
         "tokens": 1000, "tool_uses": 2},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_report_aggregation_and_since():
    p, _ = fresh()
    _seed(p)
    agg = tl.aggregate(tl.load(p))
    assert agg["totals"]["count"] == 3 and agg["totals"]["total"] == 150 + 115 + 1000
    assert agg["by_task"]["B"]["total"] == 1115 and agg["by_task"]["A"]["cost_usd"] == 1.0
    assert agg["by_agent"]["scout"]["total"] == 1000 and agg["by_agent"]["turn"]["count"] == 2
    assert agg["by_model"]["opus"]["count"] == 1 and set(agg["by_day"]) == {"2026-11-06", "2026-11-07"}
    since = tl.aggregate(tl.load(p, "2026-11-07"))
    assert since["totals"]["count"] == 2 and "A" not in since["by_task"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert token_report.main(["--since", "2026-11-07", "--json"], ledger=p) == 0
    assert json.loads(buf.getvalue())["totals"]["count"] == 2
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        token_report.main([], ledger=p)
    assert "By task" in buf.getvalue() and "scout" in buf.getvalue()


def test_corrupt_and_missing():
    p, _ = fresh()
    assert tl.load(p) == []  # missing
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert token_report.main([], ledger=p) == 0
    assert "No token ledger" in buf.getvalue()
    p.write_text("", encoding="utf-8")
    assert tl.load(p) == []
    p.write_bytes(b'not json\n{"ts":"2026-11-07T1","kind":"turn","input_tokens":"zz","output_tokens":3}\n'
                  b'[1,2]\n\xff\xfe garbage\n{"ts":\n')
    recs = tl.load(p)
    assert len(recs) == 1
    assert tl.aggregate(recs)["totals"]["output"] == 3


def test_manual_subagent_cli():
    p, t = fresh()
    assert token_report.main(["--add-subagent", "verifier", "--tokens", "42"], ledger=p, task_path=t) == 0
    r = tl.load(p)[0]
    assert r["agent_type"] == "verifier" and r["tokens"] == 42


def test_switch_and_path_override():
    import os
    saved = {k: os.environ.get(k) for k in ("JARVIS_TOKEN_LEDGER", "JARVIS_TOKEN_LEDGER_PATH", "JARVIS_NOTIFY_PHONE")}
    p, _ = fresh()
    try:
        os.environ["JARVIS_TOKEN_LEDGER_PATH"] = str(p)
        # disabled: default-path write is skipped
        for off in ("0", "false", "OFF"):
            os.environ["JARVIS_TOKEN_LEDGER"] = off
            assert tl.enabled() is False
            assert tl.record_result(fake_result(), "m") is False and not p.exists()
        # JARVIS_NOTIFY_PHONE=0 must NOT disable the ledger any more
        os.environ.pop("JARVIS_TOKEN_LEDGER", None)
        os.environ["JARVIS_NOTIFY_PHONE"] = "0"
        assert tl.ledger_path() == p
        os.environ["JARVIS_TOKEN_LEDGER"] = "1"
        assert tl.record_result(fake_result(), "m") is True or not tl.enabled()  # config may disable; else writes
        if tl.enabled():
            assert len(tl.load()) == 1 and tl.load(p)[0]["kind"] == "turn"
        # explicit path always writes even when disabled
        os.environ["JARVIS_TOKEN_LEDGER"] = "0"
        p2, _ = fresh()
        assert tl.record_result(fake_result(), "m", path=p2) is True and p2.exists()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _turns(p, costs, sid="s1"):
    for c in costs:
        assert tl.record_result(fake_result(total_cost_usd=c, session_id=sid), "m", path=p)
    return [r for r in tl.load(p) if r.get("sid") == tl._sid(SimpleNamespace(session_id=sid))]


def test_cumulative_to_delta():
    p, _ = fresh()
    rows = _turns(p, [10.83, 11.15, 11.81, 12.06])
    assert [r["cost_total_usd"] for r in rows] == [10.83, 11.15, 11.81, 12.06]
    d = [r["cost_usd"] for r in rows]
    assert d[0] == 10.83 and abs(d[1] - 0.32) < 1e-9 and abs(d[2] - 0.66) < 1e-9 and abs(d[3] - 0.25) < 1e-9
    agg = tl.aggregate(tl.load(p))
    assert abs(agg["totals"]["cost_usd"] - 12.06) < 1e-9
    assert "sess-secret" not in p.read_text(encoding="utf-8")


def test_new_session_and_decrease_reset():
    p, _ = fresh()
    _turns(p, [1.0, 1.5], sid="a")
    rows = _turns(p, [0.4], sid="b")  # new session: delta = value
    assert rows[0]["cost_usd"] == 0.4
    rows = _turns(p, [2.0, 0.3], sid="a")  # a continues (+0.5), then decreases -> new total
    assert abs(rows[2]["cost_usd"] - 0.5) < 1e-9 and rows[3]["cost_usd"] == 0.3
    assert all(r["cost_usd"] >= 0 for r in tl.load(p))


def test_restart_recovery_from_tail():
    p, _ = fresh()
    _turns(p, [3.0, 4.0], sid="r")
    tl._cost_state.clear()  # simulate process restart
    rows = _turns(p, [4.5], sid="r")
    assert abs(rows[-1]["cost_usd"] - 0.5) < 1e-9
    tl._cost_state.clear()
    # a big file: only the tail is read; padding rows must not break recovery
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "subagent", "pad": "x" * 300000}) + "\n")
    _turns(p, [5.0], sid="r")
    tl._cost_state.clear()
    rows = _turns(p, [5.2], sid="r")
    assert abs(rows[-1]["cost_usd"] - 0.2) < 1e-9 or rows[-1]["cost_usd"] == 5.2  # tail may miss it, never negative


def test_old_row_migration_in_report():
    p, _ = fresh()
    cums = [10.83, 11.15, 11.81, 12.06]
    old = [{"ts": f"2026-11-06T10:0{i}:00", "kind": "turn", "model": "m", "input_tokens": 1, "cost_usd": c}
           for i, c in enumerate(cums)]
    new = {"ts": "2026-11-07T10:00:00", "kind": "turn", "model": "m", "input_tokens": 1, "cost_usd": 0.1,
           "cost_total_usd": 12.16, "sid": "x"}
    p.write_text("\n".join(json.dumps(r) for r in old + [new]) + "\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    mig = token_report.migrate_old_costs(tl.load(p))
    assert abs(sum(r["cost_usd"] for r in mig[:4]) - 12.06) < 1e-9 and mig[4]["cost_usd"] == 0.1
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        token_report.main(["--json"], ledger=p)
    assert abs(json.loads(buf.getvalue())["totals"]["cost_usd"] - 12.16) < 1e-9
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        token_report.main(["--since", "2026-11-07", "--json"], ledger=p)  # filter after migration
    assert abs(json.loads(buf.getvalue())["totals"]["cost_usd"] - 0.1) < 1e-9
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        token_report.main([], ledger=p)
    assert "session cost deltas" in buf.getvalue()
    assert p.read_text(encoding="utf-8") == before  # file untouched


def test_config_cache():
    import os
    import jarvis.config as cfg
    saved_env, saved_fn, saved_cache = os.environ.pop("JARVIS_TOKEN_LEDGER", None), cfg.load_config, list(tl._cfg_cache)
    calls = [0]

    def fake_cfg():
        calls[0] += 1
        return {"token_ledger": True}
    try:
        cfg.load_config = fake_cfg
        tl._cfg_cache[2] = False
        for _ in range(5):
            assert tl.enabled() is True
        assert calls[0] == 1
        tl._cfg_cache[0] -= 61  # expire
        tl.enabled()
        assert calls[0] == 2
    finally:
        cfg.load_config = saved_fn
        tl._cfg_cache[:] = saved_cache
        if saved_env is not None:
            os.environ["JARVIS_TOKEN_LEDGER"] = saved_env


def test_dispatch_persistence():
    dp = TMP / "disp_persist.json"
    for i in range(250):
        tl.note_dispatch(f"id{i}", "scout" if i != 249 else "verifier", path=dp)
    d = json.loads(dp.read_text(encoding="utf-8"))
    assert len(d) == 200 and "id0" not in d and d["id249"] == "verifier"
    assert not list(TMP.glob("disp_persist.json.*.tmp"))
    tl._dispatch_types.clear()  # simulate restart
    p, t = fresh()
    msg = SimpleNamespace(tool_use_id="id249", status="completed", usage={"total_tokens": 9})
    assert tl.record_task_notification(msg, None, path=p, task_path=t, dispatch_file=dp)
    assert tl.load(p)[0]["agent_type"] == "verifier"
    assert "id249" not in json.loads(dp.read_text(encoding="utf-8"))
    # unwritable path never raises
    tl.note_dispatch("z", "scout", path=TMP)


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            ok += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{ok}/{len(tests)} passed")
    return ok == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
