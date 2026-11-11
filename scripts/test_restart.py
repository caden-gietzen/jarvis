"""Simulated end-to-end restart test. Never touches the real Jarvis.

Run:  uv run python scripts/test_restart.py

Starts a fake "old Jarvis" in a NEW powershell window (-NoExit -> python), triggers the real
restart.launch_restart_script with a fake command/match and temp files, then checks that a new
fake process is running and the old fake python AND old shell are gone. Only processes whose
command line carries the unique marker (or their parent shells) are cleaned up.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import restart  # noqa: E402

MARKER = "fakejarvis_test"
FAKE_PY = '''
import sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from jarvis import restart
if "old" in sys.argv:
    print(restart.save_window_identity(Path({win!r})))
time.sleep(150)  # self-limiting
'''


def ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                          text=True).stdout.strip()


def fake_pythons() -> list[dict]:
    out = ps("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             f"Where-Object {{ $_.CommandLine -match '{MARKER}\\s+run' }} | "
             "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    if not out:
        return []
    d = json.loads(out)
    return d if isinstance(d, list) else [d]


def alive(pid: int) -> bool:
    return bool(ps(f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -Expand Id"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_restart_test_"))
    win, log = tmp / "window.json", tmp / "restart.log"
    fake = tmp / "fake.py"
    fake.write_text(FAKE_PY.format(root=str(ROOT), win=str(win)), encoding="utf-8")
    py = sys.executable
    created: set[int] = set()
    failures: list[str] = []
    try:
        assert not fake_pythons(), "fake processes already running; aborting"
        # 1. Old fake Jarvis in a new window, as a user would start it.
        old_cmd = f"& '{py}' '{fake}' {MARKER} run old"
        old_shell = int(ps(f"(Start-Process powershell -PassThru -ArgumentList '-NoExit','-Command',\"{old_cmd}\").Id"))
        created.add(old_shell)
        for _ in range(30):
            if win.exists() and win.read_text(encoding="utf-8").strip():
                break
            time.sleep(1)
        info = json.loads(win.read_text(encoding="utf-8"))
        print("old identity:", info, "old shell:", old_shell)
        assert info["shell_pid"] == old_shell, f"shell_pid {info['shell_pid']} != {old_shell}"
        created.add(info["pid"])

        # 2. Trigger the real launch path with the fake command.
        new_cmd = f"& '{py}' '{fake}' {MARKER} run new"
        restart.launch_restart_script(window_file=win, log_file=log, command=new_cmd,
                                      match=rf"{MARKER}\s+run")
        # 3. Wait for the swap.
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(2)
            procs = fake_pythons()
            news = [p for p in procs if "run new" in p["CommandLine"]]
            if news and not alive(info["pid"]) and not alive(old_shell):
                break
        news = [p for p in fake_pythons() if "run new" in p["CommandLine"]]
        for p in news:
            created.add(p["ProcessId"])
            created.add(p["ParentProcessId"])
        if not news:
            failures.append("no new fake process running")
        if alive(info["pid"]):
            failures.append("old fake python still alive")
        if alive(old_shell):
            failures.append("old shell still alive")
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        for needle in ("launching restart script", "restart script start", "started new window",
                       "new Jarvis detected", "terminating old tree", "RESULT: done"):
            if needle not in text:
                failures.append(f"log missing: {needle}")
        print("---- restart.log ----\n" + text)
    finally:
        # Cleanup: only pids this test created (shells + marker pythons).
        for p in fake_pythons():
            created.add(p["ProcessId"])
        for pid in created:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    if failures:
        print("FAIL:", failures)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
