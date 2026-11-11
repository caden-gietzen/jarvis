r"""Offline tests for scripts/ensure_workspace.py and the missing-workspace code paths.
Plain script: run with the venv python. A local bare repo stands in for the network; temp dirs only.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import contextlib
import io
import json
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
sys.path.insert(0, str(ROOT / "scripts"))

import ensure_workspace as ew  # noqa: E402
from jarvis import config, mdpreview, restart, ui, usage_monitor  # noqa: E402

ui._log_path = Path(tempfile.gettempdir()) / "jarvis-test-ensure-workspace.log"
results = []


def case(fn):
    try:
        fn()
        results.append(True)
    except Exception:
        results.append(False)
        print(f"FAIL {fn.__name__}")
        traceback.print_exc()
    return fn


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


def make_bare(tmp: Path) -> Path:
    """A bare repo shaped like jarvis-workspace: templates/*.md and a .gitignore listing the root notes."""
    src = tmp / "src"
    (src / "templates").mkdir(parents=True)
    (src / "templates" / "HANDOFF.md").write_text("# handoff template\n", encoding="utf-8")
    (src / "templates" / "blocked.md").write_text("# blocked template\n", encoding="utf-8")
    (src / ".gitignore").write_text("/HANDOFF.md\n/blocked.md\n", encoding="utf-8")
    git("init", "-q", cwd=src)
    git("add", ".", cwd=src)
    git("commit", "-q", "-m", "init", cwd=src)
    bare = tmp / "remote.git"
    git("clone", "-q", "--bare", str(src), str(bare))
    return bare


def run_main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ew.main(list(argv))
    return rc, out.getvalue()


@case
def report_only_changes_nothing():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        rc, out = run_main("--path", str(ws))
        assert rc == 1 and "workspace folder not found" in out, out
        assert not ws.exists()


@case
def clone_and_copy_templates_without_overwriting():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = make_bare(tmp)
        ws = tmp / "ws"
        rc, out = run_main("--clone", "--path", str(ws), "--repo", str(bare))
        assert rc == 0, out
        assert (ws / "HANDOFF.md").read_text(encoding="utf-8") == "# handoff template\n"
        assert (ws / "blocked.md").is_file()
        assert "copied templates:" in out and "HANDOFF.md" in out and "blocked.md" in out, out
        assert "ok" in out and "WARNING" not in out, out
        # a second run neither re-clones nor overwrites the user's notes
        (ws / "HANDOFF.md").write_text("my notes\n", encoding="utf-8")
        rc, out = run_main("--clone", "--path", str(ws), "--repo", str(bare))
        assert rc == 0 and "found" in out and "none" in out, out
        assert (ws / "HANDOFF.md").read_text(encoding="utf-8") == "my notes\n"


@case
def failed_clone_reports_and_returns_2():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        rc, out = run_main("--clone", "--path", str(ws), "--repo", str(Path(d) / "nope.git"))
        assert rc == 2 and "clone failed" in out, out


@case
def env_var_selects_folder():
    key = "JARVIS_WORKSPACE_ROOT"
    saved = _os_ledger.environ.get(key)
    target = str(Path(tempfile.gettempdir()) / "somewhere-ws")
    _os_ledger.environ[key] = target
    try:
        assert ew.default_path() == Path(target)
    finally:
        if saved is None:
            del _os_ledger.environ[key]
        else:
            _os_ledger.environ[key] = saved


@case
def missing_message_is_one_line_and_points_at_setup():
    with tempfile.TemporaryDirectory() as d:
        msg = config.workspace_missing_message(Path(d) / "nope")
        assert msg and "\n" not in msg and "SETUP.md" in msg and "workspace folder not found" in msg
        assert config.workspace_missing_message(Path(d)) is None


@case
def usage_handoff_creates_missing_folder_without_crash():
    with tempfile.TemporaryDirectory() as d:
        saved = usage_monitor.HANDOFF_FILE
        usage_monitor.HANDOFF_FILE = Path(d) / "new" / "ws" / "HANDOFF.md"
        try:
            usage_monitor._append_handoff("five_hour", 1_800_000_000.0)
            assert usage_monitor.HANDOFF_FILE.is_file()
        finally:
            usage_monitor.HANDOFF_FILE = saved


@case
def restart_return_logs_missing_workspace_and_still_continues():
    with tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "marker.json"
        marker.write_text(json.dumps({"created_at": time.time(), "reason": "test", "count": 1}), encoding="utf-8")
        saved = restart.WORKSPACE_ROOT
        restart.WORKSPACE_ROOT = Path(d) / "nope"
        sent, logs = [], []
        orig = restart.workspace_missing_message
        restart.workspace_missing_message = lambda root=None: config.workspace_missing_message(Path(d) / "nope")
        try:
            t = restart.schedule_return(sent.append, lambda s: None, logs.append, delay_s=0, marker=marker)
            t.join(5)
        finally:
            restart.WORKSPACE_ROOT = saved
            restart.workspace_missing_message = orig
        assert any("workspace folder not found" in x for x in logs), logs
        assert sent == [restart.RETURN_PROMPT], sent


@case
def mdpreview_missing_workspace_is_a_clean_error():
    with tempfile.TemporaryDirectory() as d:
        try:
            mdpreview.resolve_book("workspace", Path(d), Path(d) / "nope")
        except mdpreview.PreviewError as e:
            assert "workspace folder not found" in str(e) and "SETUP.md" in str(e)
        else:
            raise AssertionError("expected PreviewError")


print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
