"""show_code and run_in_terminal tools for the user's focused VS Code window.

Talks to the jarvis-show-code VS Code extension over local HTTP. If the extension
isn't reachable, show_code falls back to `code -g file:line`, which only moves the cursor.
run_in_terminal has NO fallback: it sends the command to the extension's /terminal endpoint
(authenticated with the shared token file) or does nothing.
"""

import asyncio
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

# Overridable for tests.
PORT = int(os.environ.get("JARVIS_SHOWCODE_PORT", "47821"))
TIMEOUT_S = 3.0
TOKEN_PATH = Path(os.environ.get("JARVIS_SHOWCODE_TOKEN_FILE", str(Path.home() / ".jarvis_showcode_token")))
# Optional full path to the VS Code CLI; when unset or missing, "code" is looked up on PATH.
CODE_CMD = os.environ.get("JARVIS_CODE_CMD", "")

SERVER_NAME = "jarvis"
TOOL_NAME = "show_code"
TOOL_ID = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
TERMINAL_TOOL_NAME = "run_in_terminal"
TERMINAL_TOOL_ID = f"mcp__{SERVER_NAME}__{TERMINAL_TOOL_NAME}"

_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "Absolute path of the file to show."},
        "start_line": {"type": "integer", "description": "First line to highlight (1-based)."},
        "end_line": {"type": "integer", "description": "Last line to highlight, inclusive. Defaults to start_line."},
        "note": {"type": "string", "description": "Optional short caption shown in the editor."},
    },
    "required": ["file", "start_line"],
}

_DESCRIPTION = (
    "Open a file in the user's VS Code and highlight a line range so you can talk them "
    "through it. Highlight ONLY the exact lines that changed or matter: a single line "
    "(end_line omitted) when one line is the point, never the whole surrounding function. "
    "Read-only. If the result says only the cursor moved, no range is highlighted, so "
    "describe the range in words."
)

_TERMINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Single-line command to run (under 2000 chars, no newlines)."},
        "cwd": {"type": "string", "description": "Optional absolute directory. Only used if a new terminal has to be created; an already-open terminal keeps its own directory."},
    },
    "required": ["command"],
}

_TERMINAL_DESCRIPTION = (
    "Run a command in the integrated terminal of the user's focused VS Code window, so "
    "they see the output in the terminal they are already looking at. Use it when the "
    "user asks you to run something. It needs the user's spoken approval. The output is "
    "not returned to you; ask the user what they see if you need it."
)


def _result(text: str, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


def _post_show(payload: dict) -> tuple[int, dict]:
    """POST /show. Returns (status, json). Raises OSError if the extension is unreachable."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/show",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            try:
                body = json.loads(r.read().decode("utf-8") or "{}")
            except ValueError as e:  # non-JSON body: not our extension
                raise OSError(f"invalid response from extension: {e}") from e
            if not isinstance(body, dict):
                raise OSError("invalid response from extension: not a JSON object")
            return r.status, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return e.code, body
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OSError(str(e)) from e


def _code_cli_jump(path: str, line: int) -> None:
    exe = CODE_CMD if CODE_CMD and Path(CODE_CMD).exists() else shutil.which("code")
    if not exe:
        raise FileNotFoundError("VS Code CLI not found")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [exe, "-g", f"{path}:{line}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def show_code_sync(args: dict, project_dir: str) -> dict:
    """Blocking implementation; run in a thread."""
    raw = str(args.get("file", "")).strip()
    if not raw:
        return _result("Error: 'file' is required.", True)
    p = Path(raw)
    if not p.is_absolute():
        p = Path(project_dir) / p
    if not p.is_file():
        return _result(f"Error: file not found: {p}. Pass an absolute path to an existing file.", True)
    path = str(p.resolve())

    try:
        start = int(args.get("start_line"))
    except (TypeError, ValueError):
        return _result("Error: 'start_line' must be an integer.", True)
    if start < 1:
        return _result("Error: 'start_line' must be 1 or greater.", True)
    end_raw = args.get("end_line")
    try:
        end = start if end_raw in (None, "") else int(end_raw)
    except (TypeError, ValueError):
        return _result("Error: 'end_line' must be an integer.", True)
    if end < start:
        return _result("Error: 'end_line' must not be before 'start_line'.", True)

    payload = {"file": path, "startLine": start, "endLine": end}
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return _result("Error: 'note' must be a string.", True)
    if note:
        payload["note"] = note

    try:
        status, body = _post_show(payload)
    except OSError:
        # Extension not running: fall back to moving the cursor with the CLI.
        try:
            _code_cli_jump(path, start)
        except OSError as e:
            return _result(f"Error: the VS Code extension isn't running and the code CLI failed ({e}).", True)
        return _result(
            f"The show-code extension is not running, so I only opened {p.name} in VS Code with the "
            f"cursor at line {start}. No range was highlighted. Describe lines {start} to {end} "
            "verbally and tell the user where to look."
        )

    if status == 200 and body.get("ok"):
        sel = body.get("selected", {})
        return _result(
            f"Showing {p.name} lines {sel.get('startLine', start)} to {sel.get('endLine', end)} "
            f"(file has {body.get('totalLines', '?')} lines). The range is highlighted on screen."
        )
    return _result(f"Error: VS Code extension rejected the request ({status}): {body.get('error', 'unknown error')}", True)


def _read_token() -> str:
    try:
        tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return tok if len(tok) >= 32 else ""


def _post_terminal(payload: dict, token: str) -> tuple[int, dict]:
    """POST /terminal with the shared token. Raises OSError if the extension is unreachable."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/terminal",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Jarvis-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            try:
                body = json.loads(r.read().decode("utf-8") or "{}")
            except ValueError as e:
                raise OSError(f"invalid response from extension: {e}") from e
            if not isinstance(body, dict):
                raise OSError("invalid response from extension: not a JSON object")
            return r.status, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return e.code, body if isinstance(body, dict) else {}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OSError(str(e)) from e


_NOT_RUN_HINT = "The command was NOT run. The user may need to reload the VS Code window (Developer: Reload Window)."


def run_in_terminal_sync(args: dict) -> dict:
    """Blocking implementation; run in a thread."""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return _result("Error: 'command' is required and must be a non-empty string. The command was NOT run.", True)
    if "\n" in command or "\r" in command:
        return _result("Error: 'command' must be a single line (no newlines). The command was NOT run.", True)
    if len(command) >= 2000:
        return _result("Error: 'command' must be under 2000 characters. The command was NOT run.", True)
    payload = {"command": command}
    cwd = args.get("cwd")
    if cwd not in (None, ""):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return _result("Error: 'cwd' must be an existing absolute directory. The command was NOT run.", True)
        payload["cwd"] = cwd

    token = _read_token()
    if not token:
        return _result(f"Error: the show-code token file ({TOKEN_PATH}) is missing or invalid. {_NOT_RUN_HINT}", True)
    try:
        status, body = _post_terminal(payload, token)
    except OSError as e:
        return _result(f"Error: could not reach the VS Code extension ({e}). {_NOT_RUN_HINT}", True)

    if status == 200 and body.get("ok"):
        made = " (a new terminal was created)" if body.get("created") else ""
        return _result(
            f"Sent the command to the VS Code terminal '{body.get('terminal', '?')}'{made}. "
            "The output is visible on the user's screen; it is not returned to you."
        )
    if status == 401:
        return _result(f"Error: the extension rejected the token (401). {_NOT_RUN_HINT}", True)
    return _result(
        f"Error: VS Code extension rejected the request ({status}): {body.get('error', 'unknown error')}. "
        "The command was NOT run.", True)


def build_server(project_dir: str):
    @tool(TOOL_NAME, _DESCRIPTION, _SCHEMA)
    async def show_code(args):
        return await asyncio.to_thread(show_code_sync, args, project_dir)

    @tool(TERMINAL_TOOL_NAME, _TERMINAL_DESCRIPTION, _TERMINAL_SCHEMA)
    async def run_in_terminal(args):
        return await asyncio.to_thread(run_in_terminal_sync, args)

    return create_sdk_mcp_server(SERVER_NAME, tools=[show_code, run_in_terminal])
