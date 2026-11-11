"""Offline tests for the statusline usage source. Plain script; prints PASS/FAIL, exits non-zero on failure."""
import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # never write the real ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"  # never send a real phone push
import json
import os
import site
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from jarvis import statusline_usage as su  # noqa: E402

SCRIPT = ROOT / "scripts" / "statusline_capture.py"
TMP = Path(tempfile.mkdtemp(prefix="jarvis_sl_test_"))
FUTURE = int(time.time()) + 3600

FULL = {
    "session_id": "SECRET-SESSION", "transcript_path": "/home/x/.claude/projects/p/t.jsonl",
    "cwd": "C:/secret/dir", "workspace": {"current_dir": "C:/secret/dir"},
    "model": {"id": "claude-x", "display_name": "Opus Test"},
    "context_window": {"used_percentage": 9},
    "rate_limits": {"five_hour": {"used_percentage": 41.26, "resets_at": FUTURE},
                    "seven_day": {"used_percentage": 12, "resets_at": FUTURE + 86400}},
}


def run_capture(stdin_bytes: bytes, out: Path):
    env = dict(os.environ, JARVIS_STATUSLINE_FILE=str(out))
    t = time.time()
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin_bytes, capture_output=True, env=env, timeout=30)
    return p, time.time() - t


def test_full():
    out = TMP / "full.json"
    p, _ = run_capture(json.dumps(FULL).encode(), out)
    assert p.returncode == 0
    assert p.stdout.decode() == "5h 41% | 7d 12%", p.stdout
    d = json.loads(out.read_text())
    assert d["rate_limits"]["five_hour"] == {"used_percentage": 41.3, "resets_at": FUTURE}, d
    assert d["model"] == "Opus Test"


def test_missing_window():
    out = TMP / "one.json"
    doc = {"rate_limits": {"seven_day": {"used_percentage": 50, "resets_at": FUTURE}}}
    p, _ = run_capture(json.dumps(doc).encode(), out)
    assert p.returncode == 0 and p.stdout.decode() == "7d 50%"
    assert list(json.loads(out.read_text())["rate_limits"]) == ["seven_day"]


def test_no_rate_limits_keeps_old_file():
    out = TMP / "keep.json"
    out.write_text('{"keep": true}')
    for doc in ({"model": {"display_name": "M"}}, {"rate_limits": {}}, {"rate_limits": {"five_hour": {}}}):
        p, _ = run_capture(json.dumps(doc).encode(), out)
        assert p.returncode == 0 and p.stdout == b""
    assert json.loads(out.read_text()) == {"keep": True}


def test_garbage():
    out = TMP / "garbage.json"
    for blob in (b"", b"not json", b"\xff\xfe\x00", b"[1,2]", b"null", b'{"rate_limits": 5}',
                 b'{"rate_limits": {"five_hour": {"used_percentage": "x"}}}',
                 b'{"rate_limits": {"five_hour": {"used_percentage": NaN}}}'):
        p, _ = run_capture(blob, out)
        assert p.returncode == 0, blob
        assert not out.exists(), blob


def test_huge_input():
    out = TMP / "huge.json"
    blob = b'{"x": "' + b"a" * 5_000_000 + b'"}'
    p, dt = run_capture(blob, out)
    assert p.returncode == 0 and not out.exists() and dt < 10


def test_clamp_and_odd_values():
    out = TMP / "clamp.json"
    doc = {"rate_limits": {"five_hour": {"used_percentage": 250, "resets_at": "soon"},
                           "seven_day": {"used_percentage": -4, "resets_at": FUTURE}}}
    p, _ = run_capture(json.dumps(doc).encode(), out)
    d = json.loads(out.read_text())["rate_limits"]
    assert d["five_hour"] == {"used_percentage": 100.0, "resets_at": None}
    assert d["seven_day"]["used_percentage"] == 0.0


def test_whitelist():
    out = TMP / "wl.json"
    run_capture(json.dumps(FULL).encode(), out)
    text = out.read_text()
    for leak in ("SECRET", "secret", "transcript", "cwd", "session", "Users", "claude-x", "context"):
        assert leak not in text, leak
    assert set(json.loads(text)) == {"updated", "model", "rate_limits"}


def test_atomic_write():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sl_capture", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = TMP / "atomic.json"
    mod.write_atomic(str(out), {"a": 1})
    assert json.loads(out.read_text()) == {"a": 1}
    assert not list(TMP.glob("atomic.json.*.tmp"))
    # a failing replace leaves the old file intact and no temp behind
    real = os.replace
    os.replace = lambda *a: (_ for _ in ()).throw(OSError("boom"))
    try:
        try:
            mod.write_atomic(str(out), {"a": 2})
            assert False, "should raise"
        except OSError:
            pass
    finally:
        os.replace = real
    assert json.loads(out.read_text()) == {"a": 1}
    assert not list(TMP.glob("atomic.json.*.tmp"))


def test_speed():
    out = TMP / "speed.json"
    run_capture(b"{}", out)  # warm
    _, dt = run_capture(json.dumps(FULL).encode(), out)
    assert dt < 1.0, dt  # process start included; the script itself is a few ms


def _write(path, five=None, seven=None, updated=None):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    Path(path).write_text(json.dumps({"updated": updated or int(time.time()), "model": None, "rate_limits": rl}))


def test_read_usage():
    p = TMP / "ru.json"
    now = time.time()
    assert su.read_usage(TMP / "missing.json") is None
    _write(p, (41.0, FUTURE), (12.0, FUTURE))
    d = su.read_usage(p)
    assert d["five_hour"] == {"utilization_pct": 41.0, "resets_at": FUTURE} and d["seven_day"]["utilization_pct"] == 12.0
    assert isinstance(d["updated"], int)
    # old file but window still in the future -> kept
    _write(p, (41.0, FUTURE), updated=int(now) - 3 * 3600)
    assert su.read_usage(p)["five_hour"]["utilization_pct"] == 41.0
    # old file, window already reset -> dropped
    _write(p, (41.0, int(now) - 10), updated=int(now) - 3 * 3600)
    assert su.read_usage(p) is None
    # old file, no reset time -> stale; fresh file without reset time -> kept
    _write(p, (41.0, None), updated=int(now) - 3600)
    assert su.read_usage(p) is None
    _write(p, (41.0, None))
    assert su.read_usage(p) is not None
    p.write_text("garbage{")
    assert su.read_usage(p) is None
    p.write_text('{"updated": "x", "rate_limits": 3}')
    assert su.read_usage(p) is None


def test_status_thresholds():
    assert [su.status_for(x) for x in (0, 79.9, 80, 99.9, 100)] == \
        ["allowed", "allowed", "allowed_warning", "allowed_warning", "rejected"]


def test_poller_changes_only():
    p = TMP / "poll.json"
    got = []
    poller = su.UsagePoller(got.append, path=p, interval=0.05)
    poller.poll_once()
    assert got == []  # no file
    _write(p, (41.0, FUTURE), (12.0, FUTURE + 1))
    poller.poll_once()
    assert [(e["type"], e["utilization_pct"]) for e in got] == [("five_hour", 41.0), ("seven_day", 12.0)]
    poller.poll_once()
    assert len(got) == 2  # same mtime
    _write(p, (41.0, FUTURE), (12.0, FUTURE + 1))
    os.utime(p, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    poller.poll_once()
    assert len(got) == 2  # rewritten, values identical
    _write(p, (85.0, FUTURE), (12.0, FUTURE + 1))
    os.utime(p, ns=(time.time_ns() + 2 * 10**9, time.time_ns() + 2 * 10**9))
    poller.poll_once()
    assert len(got) == 3 and got[-1]["type"] == "five_hour" and got[-1]["status"] == "allowed_warning"
    assert all(e["source"] == "statusline" for e in got)


def test_poller_survives_bad_file_and_callback():
    p = TMP / "bad.json"
    p.write_text("{{{")
    calls = []

    def cb(e):
        calls.append(e)
        raise RuntimeError("cb failed")

    poller = su.UsagePoller(cb, path=p, interval=0.05)
    poller.poll_once()
    assert calls == []
    _write(p, (10.0, FUTURE))
    os.utime(p, ns=(time.time_ns() + 10**9,) * 2)
    poller.poll_once()
    assert len(calls) == 1


def test_poller_thread_stop():
    p = TMP / "thread.json"
    got = []
    poller = su.UsagePoller(got.append, path=p, interval=0.05)
    poller.start()
    _write(p, (30.0, FUTURE))
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.05)
    poller.stop()
    assert got and not poller._thread.is_alive()
    poller.stop()  # idempotent


def test_event_shape_matches_brain():
    from jarvis import brain
    info = type("I", (), {"rate_limit_type": "five_hour", "status": "allowed", "utilization": 0.4,
                          "resets_at": FUTURE})()
    brain_keys = set(brain.usage_from_info(info))
    ev = su.usage_from_statusline("five_hour", {"utilization_pct": 41.0, "resets_at": FUTURE})
    assert set(ev) == brain_keys | {"source"}, (set(ev), brain_keys)
    assert isinstance(ev["resets_at"], int) and isinstance(ev["seen_at"], int) and isinstance(ev["utilization_pct"], float)


def test_settings_json():
    s = su.settings_json()
    d = json.loads(s)
    sl = d["statusLine"]
    assert sl["type"] == "command" and sl["command"] == su.statusline_command()
    assert sl["command"].startswith('"') and sl["command"].endswith('statusline_capture.py"')
    assert "\\" not in sl["command"]
    c = su.statusline_command("C:/Program Files/Py/python.exe", "C:/my dir/s.py")
    assert c == '"C:/Program Files/Py/python.exe" "C:/my dir/s.py"'
    assert su.SCRIPT.exists()


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
