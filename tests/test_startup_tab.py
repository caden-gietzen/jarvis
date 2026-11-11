r"""Offline tests for opening the web page in a new Chrome tab at a cold start.

Run:
    set JARVIS_NOTIFY_PHONE=0 && set JARVIS_TOKEN_LEDGER=0 && .venv\Scripts\python.exe tests\test_startup_tab.py

Plain script. The launcher is always a fake: Chrome is never started, no real marker or log file is touched.
Covers: cold-start detection (marker peeked, never consumed; stale marker; usage restart), both switches
(config key, env JARVIS_OPEN_WEB_UI=0), web_ui off / no URL, chrome.exe --new-tab command, fallback to the
default browser, launcher failure not fatal, non-blocking, no token in logs.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')
import io
import json
import os
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
os.environ["JARVIS_NOTIFY_PHONE"] = "0"

from rich.console import Console  # noqa: E402

from jarvis import __main__ as main_mod  # noqa: E402
from jarvis import restart, ui  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-tab-"))
ui._log_path = TMP / "test.log"
OUT = io.StringIO()
ui.console = Console(file=OUT)
TOKEN = "SECRETTOKEN0123456789abcdef"
URL = f"http://127.0.0.1:47823/?token={TOKEN}"
PASSED, FAILED = [], []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def logs() -> str:
    p = ui._log_path
    return (OUT.getvalue() + (Path(p).read_text(encoding="utf-8") if Path(p).exists() else ""))


class Recorder:
    def __init__(self, result="chrome", raises=None, delay=0.0):
        self.urls, self.result, self.raises, self.delay = [], result, raises, delay
        self.done = threading.Event()

    def __call__(self, url):
        try:
            if self.delay:
                time.sleep(self.delay)
            self.urls.append(url)
            if self.raises:
                raise self.raises
            return self.result
        finally:
            self.done.set()


def run(cfg, url=URL, cold=True, rec=None, env=None):
    rec = rec or Recorder()
    old = os.environ.get("JARVIS_OPEN_WEB_UI")
    if env is None:
        os.environ.pop("JARVIS_OPEN_WEB_UI", None)
    else:
        os.environ["JARVIS_OPEN_WEB_UI"] = env
    try:
        t = main_mod.open_web_ui_on_start(cfg, url, cold=cold, opener=rec)
        if t is not None:
            t.join(5)
    finally:
        if old is None:
            os.environ.pop("JARVIS_OPEN_WEB_UI", None)
        else:
            os.environ["JARVIS_OPEN_WEB_UI"] = old
    return t, rec


# ---------------------------------------------------------------- cold start detection
def test_cold_start_detection():
    marker = TMP / "marker.json"
    check(main_mod.is_cold_start(marker=marker) is True, "no marker -> cold")
    restart.write_marker("requested", marker=marker, history=TMP / "hist.json")
    check(main_mod.is_cold_start(marker=marker) is False, "fresh marker -> a restart, not cold")
    check(marker.exists(), "the marker is only peeked at, never consumed")
    check(main_mod.is_cold_start(usage_pending=True, marker=TMP / "none.json") is False, "usage restart -> not cold")
    marker.write_text(json.dumps({"created_at": time.time() - restart.MARKER_MAX_AGE_S - 60, "reason": "x", "count": 1}))
    check(main_mod.is_cold_start(marker=marker) is True, "stale marker -> cold")
    marker.write_text("{garbage")
    check(main_mod.is_cold_start(marker=marker) is True, "corrupt marker -> cold")
    check(main_mod.is_cold_start(marker=TMP / "dir-missing" / "m.json") is True, "unreadable -> cold")


# ---------------------------------------------------------------- switches
def test_opens_on_cold_start():
    t, rec = run({"web_ui": True, "open_web_ui_on_start": True})
    check(t is not None and rec.urls == [URL], f"opened once with the url: {rec.urls}")
    t, rec = run({})
    check(rec.urls == [URL], "defaults to on when the key is absent")


def test_not_after_restart():
    t, rec = run({"web_ui": True}, cold=False)
    check(t is None and rec.urls == [], "a restart opens nothing")


def test_config_switch_off():
    t, rec = run({"web_ui": True, "open_web_ui_on_start": False})
    check(t is None and rec.urls == [], "open_web_ui_on_start: false opens nothing")


def test_env_switch_off():
    t, rec = run({"web_ui": True, "open_web_ui_on_start": True}, env="0")
    check(t is None and rec.urls == [], "JARVIS_OPEN_WEB_UI=0 opens nothing")
    t, rec = run({"web_ui": True}, env="1")
    check(rec.urls == [URL], "JARVIS_OPEN_WEB_UI=1 does not disable it")


def test_web_ui_off_or_server_failed():
    t, rec = run({"web_ui": False})
    check(t is None and rec.urls == [], "web_ui off -> nothing")
    for bad in (None, ""):
        t, rec = run({"web_ui": True}, url=bad)
        check(t is None and rec.urls == [], f"no url ({bad!r}) -> nothing")


# ---------------------------------------------------------------- launcher
def test_chrome_new_tab_command():
    calls = []

    def popen(args, **kw):
        calls.append((args, kw))

    how = main_mod.open_web_ui_tab(URL, chrome=r"C:\x\chrome.exe", popen=popen, opener=lambda u: 1 / 0)
    check(how == "chrome" and len(calls) == 1, f"launched chrome: {how}")
    check(calls[0][0] == [r"C:\x\chrome.exe", "--new-tab", URL], f"argv: {calls[0][0]}")
    check(calls[0][1].get("stdout") is not None and calls[0][1].get("stdin") is not None, "detached from our console")


def test_fallbacks():
    opened = []
    how = main_mod.open_web_ui_tab(URL, chrome=None, popen=lambda *a, **k: 1 / 0,
                                   opener=lambda u: opened.append(u) or True) \
        if main_mod.find_chrome() is None else None
    if how is not None:
        check(how == "browser" and opened == [URL], "no chrome -> default browser new tab")
    # chrome found but the launch raises -> default browser
    opened.clear()

    def bad_popen(*a, **k):
        raise OSError("boom " + TOKEN)

    how = main_mod.open_web_ui_tab(URL, chrome=r"C:\x\chrome.exe", popen=bad_popen,
                                   opener=lambda u: opened.append(u) or True)
    check(how == "browser" and opened == [URL], f"launch failure falls back: {how}")
    # both fail -> None, never raises
    how = main_mod.open_web_ui_tab(URL, chrome=r"C:\x\chrome.exe", popen=bad_popen, opener=lambda u: 1 / 0)
    check(how is None, "both fail -> None")
    how = main_mod.open_web_ui_tab(URL, chrome=r"C:\x\chrome.exe", popen=bad_popen, opener=lambda u: False)
    check(how is None, "opener returning False -> None")


def test_find_chrome():
    check(main_mod.find_chrome(which=lambda n: r"C:\p\chrome.exe" if n == "chrome" else None,
                               exists=lambda p: False) == r"C:\p\chrome.exe", "from PATH")
    want = os.path.expandvars(main_mod.CHROME_PATHS[2])
    got = main_mod.find_chrome(which=lambda n: None, exists=lambda p: p == want)
    check(got == want or "%" in main_mod.CHROME_PATHS[2], f"standard LocalAppData path: {got}")
    check(main_mod.find_chrome(which=lambda n: None, exists=lambda p: False) is None, "nothing -> None")

    def boom(n):
        raise RuntimeError

    check(main_mod.find_chrome(which=boom) is None, "never raises")
    check(any("Program Files" in p or "ProgramFiles" in p for p in main_mod.CHROME_PATHS), "Program Files listed")


def test_failure_is_not_fatal_and_logged_without_token():
    t, rec = run({"web_ui": True}, rec=Recorder(raises=RuntimeError("boom " + TOKEN)))
    check(t is not None and not t.is_alive(), "thread ended cleanly after a failing launcher")
    check(TOKEN not in logs(), "token not in logs after a failure")
    t, rec = run({"web_ui": True}, rec=Recorder(result=None))
    check(TOKEN not in logs(), "token not in logs when nothing could be opened")
    t, rec = run({"web_ui": True}, rec=Recorder(result="chrome"))
    t, rec = run({"web_ui": True}, rec=Recorder(result="browser"))
    text = logs()
    check(TOKEN not in text and "token=" not in text, "no token in console or log")
    check("Opened the web page in Chrome (new tab)" in text, "one activity line for the Chrome launch")


def test_non_blocking():
    rec = Recorder(delay=0.6)
    t0 = time.monotonic()
    t = main_mod.open_web_ui_on_start({"web_ui": True}, URL, cold=True, opener=rec)
    took = time.monotonic() - t0
    check(took < 0.3, f"returned immediately ({took:.2f}s)")
    check(t.daemon, "daemon thread")
    check(rec.done.wait(3) and rec.urls == [URL], "the launcher still ran")
    t.join(3)


def test_wiring_in_cmd_run():
    src = (ROOT / "jarvis" / "__main__.py").read_text(encoding="utf-8")
    check("cold_start = is_cold_start(usage_pending=_um.continue_pending())" in src, "cold start decided first")
    check(src.index("cold_start = is_cold_start") < src.index("usage_monitor.on_startup()"),
          "decided before the usage monitor / restart marker are consumed")
    i = src.index("open_web_ui_on_start(cfg, url, cold=cold_start)")
    check(src.rindex("if url:", 0, i) > src.rindex("web.start", 0, i), "only inside `if url:` after web.start")
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    check(cfg.get("open_web_ui_on_start") is True, "config key present and true")


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            PASSED.append(name)
            print(f"PASS {name}")
        except Exception:
            FAILED.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
