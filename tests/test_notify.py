r"""Offline tests for the ntfy phone push (jarvis/notify.py) and its hook in Brain.ask_permission.

Run (base interpreter):
    .venv\Scripts\python.exe tests\test_notify.py

Plain script, no pytest needed. urllib is mocked: nothing touches the network.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import asyncio
import io
import os
import site
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import PermissionResultAllow  # noqa: E402
from rich.console import Console  # noqa: E402

from jarvis import brain as brain_mod  # noqa: E402
from jarvis import notify, ui  # noqa: E402

brain_mod.STATE_PATH = Path(tempfile.gettempdir()) / "jarvis-test-notify-state.json"
ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-notify.log"

TOPIC = "sekrit-topic-xyz123"
TMP = Path(tempfile.mkdtemp(prefix="jarvis-notify-"))


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """Replaces urllib.request.urlopen; records requests."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if self.fail:
            raise self.fail
        return FakeResp()


def setup(topic=TOPIC, fail=None, notify_env="1"):
    """Fresh state: topic file, mocked urlopen, cleared limiter. Returns (net, console buffer)."""
    f = TMP / "topic"
    if topic is None:
        f = TMP / "does-not-exist"
        if f.exists():
            f.unlink()
    else:
        f.write_text(topic, encoding="utf-8")
    os.environ["JARVIS_NTFY_TOPIC_FILE"] = str(f)
    os.environ["JARVIS_NOTIFY_PHONE"] = notify_env
    notify._last_sent = 0.0
    notify._recent.clear()
    net = Net(fail)
    urllib.request.urlopen = net
    buf = io.StringIO()
    ui.console = Console(file=buf, width=200, highlight=False, color_system=None)
    return net, buf


def send(summary):
    t = notify.push_permission(summary)
    if t:
        t.join(5)
    return t


def test_sends_one_post_with_headers_and_body():
    net, _ = setup()
    assert send("Edit brain.py") is not None
    assert len(net.calls) == 1
    req, timeout = net.calls[0]
    assert req.full_url == "https://ntfy.sh/" + TOPIC and req.get_method() == "POST"
    assert req.data == b"Edit brain.py" and timeout and timeout <= 10
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["title"] == "Jarvis needs permission" and h["tags"] == "lock" and h["priority"] == "default"
    h["title"].encode("ascii")
    assert notify.last_status == 200


def test_missing_or_empty_topic_file_does_nothing_silently():
    net, buf = setup(topic=None)
    assert send("x") is None and net.calls == [] and buf.getvalue() == ""
    net, buf = setup(topic="  \n")
    assert send("x") is None and net.calls == [] and buf.getvalue() == ""


def test_disabled_by_env():
    net, _ = setup(notify_env="0")
    assert send("x") is None and net.calls == []


def test_rate_limit_one_per_ten_seconds():
    net, _ = setup()
    send("first")
    assert send("second") is None  # different text, but too soon
    assert len(net.calls) == 1
    notify._last_sent -= notify.MIN_INTERVAL_S + 1  # pretend 10 s passed
    send("third")
    assert len(net.calls) == 2


def test_dedupe_same_summary_within_60s():
    net, _ = setup()
    send("same")
    notify._last_sent -= notify.MIN_INTERVAL_S + 1
    assert send("same") is None and len(net.calls) == 1
    notify._last_sent -= notify.MIN_INTERVAL_S + 1
    for k in notify._recent:
        notify._recent[k] -= notify.DEDUPE_S + 1  # 60 s passed
    send("same")
    assert len(net.calls) == 2


def test_truncates_to_200_chars_and_collapses_whitespace():
    net, _ = setup()
    send("a" * 500)
    assert net.calls[0][0].data == b"a" * 200
    setup()
    net2 = urllib.request.urlopen
    send("run\n  this\tnow")
    assert net2.calls[0][0].data == b"run this now"


def test_non_ascii_summary_is_utf8_body_and_ascii_headers():
    net, _ = setup()
    send("Edit caf\u00e9.py")
    req = net.calls[0][0]
    assert req.data.decode("utf-8") == "Edit caf\u00e9.py"
    for k, v in req.header_items():
        v.encode("ascii")


def test_failure_never_raises_and_topic_never_logged():
    err = OSError(f"connection to https://ntfy.sh/{TOPIC} failed")  # error text carries the topic
    net, buf = setup(fail=err)
    t = send("Edit x")
    assert t is not None and len(net.calls) == 1
    out = buf.getvalue() + (ui._log_path.read_text(encoding="utf-8") if ui._log_path.exists() else "")
    assert "failed" in buf.getvalue() and TOPIC not in out, out
    # even a broken topic-file path or odd input never raises
    os.environ["JARVIS_NTFY_TOPIC_FILE"] = str(TMP / "no" / "such" / "dir" / "f")
    assert notify.push_permission("x") is None
    assert notify.push_permission(None) is None


def test_push_does_not_block_caller():
    net, _ = setup()

    def slow(req, timeout=None):
        time.sleep(1.0)
        return FakeResp()

    urllib.request.urlopen = slow
    t0 = time.monotonic()
    th = notify.push_permission("slow one")
    assert time.monotonic() - t0 < 0.5
    th.join(5)


def _brain_with_stub():
    b = brain_mod.Brain(str(ROOT), speaker=None)

    async def fake_inner(tool_name, tool_input, context):
        return PermissionResultAllow()

    b._ask_permission = fake_inner
    return b


def test_ask_permission_auto_approved_sends_nothing_and_real_prompt_sends_one():
    net, _ = setup()
    b = _brain_with_stub()
    orig = brain_mod.auto_approve
    try:
        brain_mod.auto_approve = lambda tool, inp, *a, **k: "test auto"
        r = asyncio.run(b.ask_permission("Read", {"file_path": "a.py"}, None))
        assert isinstance(r, PermissionResultAllow)
        time.sleep(0.2)
        assert net.calls == [], "auto-approved call must not push"

        brain_mod.auto_approve = lambda tool, inp, *a, **k: None
        r = asyncio.run(b.ask_permission("Bash", {"command": "rm -rf build"}, None))
        assert isinstance(r, PermissionResultAllow)
        for _ in range(50):
            if net.calls:
                break
            time.sleep(0.05)
        time.sleep(0.2)
        assert len(net.calls) == 1, len(net.calls)
        assert net.calls[0][0].data, "body should be the summary"
    finally:
        brain_mod.auto_approve = orig


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
