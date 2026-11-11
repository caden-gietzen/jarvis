"""Deferred restart: a spoken request sets a flag, Jarvis restarts once it is listening again.

Files live in the repo root (next to config.yaml):
  .jarvis_window.json       console window handle, pid and hosting shell pid of the running Jarvis
  .jarvis_restart_pending   flag file; exists while a restart is requested
The restart script logs every step to logs/restart.log.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import WORKSPACE_ROOT, workspace_missing_message

ROOT = Path(__file__).resolve().parent.parent
WINDOW_FILE = ROOT / ".jarvis_window.json"
PENDING_FILE = ROOT / ".jarvis_restart_pending"
SCRIPT = ROOT / "scripts" / "restart_jarvis.ps1"
LOG_FILE = ROOT / "logs" / "restart.log"
SHELL_EXES = {"powershell.exe", "pwsh.exe", "cmd.exe"}


def _parent_map() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, exe name) for all processes (Windows toolhelp snapshot)."""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    out: dict[int, tuple[int, str]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def find_shell_pid(pid: int | None = None) -> int:
    """The shell hosting this process's console window; 0 if none.

    Walks parents from pid and returns the NEAREST powershell/pwsh (jarvis.cmd puts a cmd.exe between python
    and that PowerShell, so cmd.exe layers are skipped). It must not keep climbing: after a restart the
    window's PowerShell was started by the previous restart script, which is itself a PowerShell, and the old
    "outermost shell" rule picked that already-finished script, so the real window was never closed.
    With no PowerShell above (a plain cmd.exe window) the outermost cmd.exe in the chain is used.
    """
    if sys.platform != "win32":
        return 0
    found_cmd = 0
    try:
        procs = _parent_map()
        cur = pid or os.getpid()
        seen: set[int] = set()
        while cur in procs and cur not in seen:
            seen.add(cur)
            parent = procs[cur][0]
            if parent in procs:
                exe = procs[parent][1]
                if exe in ("powershell.exe", "pwsh.exe"):
                    return parent
                if exe in SHELL_EXES:
                    found_cmd = parent
                elif found_cmd:
                    break
            cur = parent
    except Exception:
        pass
    return found_cmd


def save_window_identity(path: Path = WINDOW_FILE) -> dict:
    """Record this process's console window handle, pid and hosting shell pid."""
    hwnd = 0
    if sys.platform == "win32":
        hwnd = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    info = {"hwnd": hwnd, "pid": os.getpid(), "shell_pid": find_shell_pid()}
    path.write_text(json.dumps(info), encoding="utf-8")
    return info


def request_restart(path: Path = PENDING_FILE, reason: str = "restart") -> None:
    path.write_text(reason, encoding="utf-8")


def restart_pending(path: Path = PENDING_FILE) -> bool:
    return path.exists()


def clear_restart(path: Path = PENDING_FILE) -> None:
    path.unlink(missing_ok=True)


MARKER_FILE = ROOT / ".jarvis_intentional_restart.json"    # written when a restart is performed
HISTORY_FILE = ROOT / ".jarvis_restart_history.json"        # recent restart times, for the loop guard
MARKER_MAX_AGE_S = 15 * 60       # an older marker is discarded, not acted on
LOOP_WINDOW_S = 10 * 60
LOOP_MAX = 3                     # this many restarts within LOOP_WINDOW_S: no auto-continue
RETURN_DELAY_S = 8.0             # let the brain finish starting before the prompt goes in
RETURN_LINE = "I'm back after a restart, picking up where we left off."
LOOP_LINE = "I restarted several times in a row, so I'm not continuing automatically."
RETURN_PROMPT = (
    "Jarvis was just restarted on purpose. Continue exactly where we left off: read "
    f"{WORKSPACE_ROOT / 'HANDOFF.md'} (newest entries at the bottom), say in one short sentence what "
    "you were doing, and carry on with any unfinished work or pending subagent results. Keep the reply "
    "short and spoken-style."
)

# Test hooks.
now_fn = time.time
sleep_fn = time.sleep


def flag_reason(path: Path = PENDING_FILE) -> str:
    """Why a restart was requested: the flag file's text, 'requested' for the default/empty text."""
    try:
        text = path.read_text(encoding="utf-8").strip()[:80]
    except Exception:
        text = ""
    return "requested" if text in ("", "restart") else text


def write_marker(reason: str, marker: Path = MARKER_FILE, history: Path = HISTORY_FILE) -> bool:
    """Record that the restart now being performed is intentional. Never raises; True if written."""
    try:
        now = float(now_fn())
        try:
            times = [float(t) for t in json.loads(history.read_text(encoding="utf-8"))]
        except Exception:
            times = []
        times = [t for t in times if 0 <= now - t <= LOOP_WINDOW_S] + [now]
        history.write_text(json.dumps(times), encoding="utf-8")
        marker.write_text(json.dumps({"created_at": now, "reason": reason, "count": len(times)}),
                          encoding="utf-8")
        return True
    except Exception:
        return False


def take_marker(marker: Path = MARKER_FILE):
    """Read and DELETE the marker first. Returns {"reason", "count"} if it was fresh and valid, else None
    (missing, stale or corrupt). Never raises."""
    try:
        if not marker.exists():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        finally:
            marker.unlink(missing_ok=True)
        created = data["created_at"]
        if isinstance(created, bool) or not isinstance(created, (int, float)) \
                or not (0 <= now_fn() - float(created) <= MARKER_MAX_AGE_S):
            return None
        count = data.get("count", 1)
        count = count if isinstance(count, int) and not isinstance(count, bool) else 1
        return {"reason": str(data.get("reason") or "requested")[:80], "count": count}
    except Exception:
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def schedule_return(submit, speak, log, enabled=True, usage_continue_pending=False,
                    delay_s: "float | None" = None, marker: Path = MARKER_FILE):
    """After an intentional restart: speak a short line and submit ONE continue prompt (its reply is
    spoken by the caller's submit). Daemon thread, never raises, never blocks. The marker is consumed
    first even when nothing is sent, so it can never loop. A pending usage-limit continue takes
    precedence (it sends its own silent prompt); a restart loop only gets a spoken note."""
    def safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:
            try:
                log(f"Restart return step failed ({type(e).__name__}).")
            except Exception:
                pass

    def run():
        try:
            sleep_fn(RETURN_DELAY_S if delay_s is None else delay_s)
            info = take_marker(marker)
            if info is None:
                return
            safe(log, f"Intentional restart ({info['reason']}): "
                      + ("continuing" if enabled and not usage_continue_pending else "not continuing"))
            if not enabled or usage_continue_pending:
                return
            missing = workspace_missing_message()
            if missing:
                safe(log, missing)  # the continue prompt still goes in; it just has no HANDOFF.md to read
            if info["count"] >= LOOP_MAX:
                safe(speak, LOOP_LINE)
                return
            safe(speak, RETURN_LINE)
            safe(submit, RETURN_PROMPT)
        except Exception:
            pass

    try:
        t = threading.Thread(target=run, daemon=True, name="restart-return")
        t.start()
        return t
    except Exception:
        return None


def _log(log_file: Path, msg: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" [py {os.getpid()}] {msg}\n")
    except Exception:
        pass


def launch_restart_script(window_file: Path = WINDOW_FILE, script: Path = SCRIPT,
                          log_file: Path = LOG_FILE, command: str | None = None,
                          match: str | None = None) -> bool:
    """Start the restart script and do not wait for it. Returns True if it was started, False if not.

    The script is created through WMI (Win32_Process.Create) so its parent is wmiprvse,
    not this process: it must survive the old Jarvis shell's process tree being killed.
    """
    try:
        info = json.loads(window_file.read_text(encoding="utf-8"))
        parts = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f'"{script}"',
                 "-OldHwnd", str(info.get("hwnd", 0)), "-OldPid", str(info.get("pid", 0)),
                 "-OldShellPid", str(info.get("shell_pid", 0)), "-LogFile", f'"{log_file}"']
        if command:
            parts += ["-Command", f'"{command}"']
        if match:
            parts += ["-Match", f'"{match}"']
        cmdline = " ".join(parts)
        _log(log_file, f"launching restart script via CIM: {cmdline}")
        launcher = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                    "-Arguments @{CommandLine=$env:JARVIS_RESTART_CMD}; "
                    'Write-Output "rv=$($r.ReturnValue) pid=$($r.ProcessId)"')
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", launcher],
            env={**os.environ, "JARVIS_RESTART_CMD": cmdline},
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _log(log_file, f"CIM create result: {res.stdout.strip()} {res.stderr.strip()}")
        return res.returncode == 0 and "rv=0" in res.stdout
    except Exception as e:
        _log(log_file, f"launch_restart_script failed: {e!r}")
        return False
