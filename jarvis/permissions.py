"""Pure helpers for spoken permission prompts: what to say, and how to read the answer."""

import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlparse

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_SHELL_TOOLS = {"Bash", "PowerShell"}
_MAX_COMMAND_CHARS = 60
_MAX_REASON_CHARS = 120

_YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "approve", "approved", "allow", "okay", "ok", "affirmative"}
_YES_PHRASES = ("go ahead", "do it")
_NO_WORDS = {"no", "nope", "nah", "deny", "denied", "don't", "dont", "stop", "cancel", "negative"}
_NO_PHRASES = ("do not",)
_WORD = re.compile(r"[a-z']+")


def parse_yes_no(text: str | None) -> bool | None:
    """True for yes, False for no, None if unclear or empty. Negation wins over agreement."""
    if not text:
        return None
    t = text.lower().replace("’", "'")
    words = set(_WORD.findall(t))
    joined = " ".join(_WORD.findall(t))
    if words & _NO_WORDS or any(p in joined for p in _NO_PHRASES):
        return False
    if words & _YES_WORDS or any(p in joined for p in _YES_PHRASES):
        return True
    return None


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut + " and so on"


def shorten_reason(text: str | None, limit: int = 200) -> str:
    """Collapse whitespace and keep roughly the first two sentences, capped at limit chars."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(sentences[:2])
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return text.rstrip(" .!?:;,")


def add_reason(summary: str, reason: str | None) -> str:
    """Append ', because <reason>.' to a summary sentence; unchanged if there is no reason."""
    reason = shorten_reason(reason)
    if not reason:
        return summary
    return f"{summary.rstrip('.')}, because {reason}."


def _path_parts(path: str) -> tuple[str, str]:
    p = Path(path)
    return p.name, p.parent.name


def summarize_request(tool_name: str, tool_input: dict | None) -> str:
    """One or two short sentences describing a tool call, fit to be read aloud."""
    i = tool_input or {}
    reason = str(i.get("description") or "").strip()

    if tool_name in _SHELL_TOOLS:
        what = reason or _shorten(str(i.get("command", "")).splitlines()[0] if i.get("command") else "", _MAX_COMMAND_CHARS)
        what = _shorten(what, _MAX_REASON_CHARS).rstrip(".")
        return f"I want to run a command: {what}." if what else "I want to run a command."

    if tool_name == "mcp__jarvis__run_in_terminal":
        cmd = _shorten(str(i.get("command", "")), _MAX_REASON_CHARS).rstrip(".")
        return f"I want to run this command in your VS Code terminal: {cmd}." if cmd else "I want to run a command in your VS Code terminal."

    if tool_name in _EDIT_TOOLS:
        path = str(i.get("file_path") or i.get("notebook_path") or "")
        if path:
            name, folder = _path_parts(path)
            where = f", in {folder}" if folder else ""
            return f"I want to edit {name}{where}."
        return "I want to edit a file."

    if tool_name == "WebFetch":
        domain = urlparse(str(i.get("url", ""))).netloc
        return f"I want to fetch {domain}." if domain else "I want to fetch a web page."

    base = f"I want to use {tool_name}."
    if reason:
        base += f" {_shorten(reason, _MAX_REASON_CHARS).rstrip(".")}."
    return base


# ---------------------------------------------------------------------------
# Auto-approve policy: calls that may run without a spoken yes/no.
# Everything uncertain returns None, which means "ask the user".
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE = r"C:\dev"

_FILE_TOOLS = {"Read", "Glob", "Grep", "LS", "Edit", "Write", "MultiEdit", "NotebookEdit"}
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_PATHISH_KEY = re.compile(r"path|file|dir|cwd", re.I)
_GLOB_KEYS = {"pattern": {"Glob"}, "glob": {"Grep"}}
# Edits to these always ask: the policy itself and the code that enforces it.
# brain.py was removed from this set on 2026-11-06 at the user's request (unattended web UI work).
_PROTECTED_REL = {os.path.normcase(r"jarvis\jarvis\permissions.py")}
_PROTECTED_DIRS = {".claude", ".git"}

# redirects, expansion, subexpressions, script blocks, non-ASCII (PowerShell accepts smart quotes/dashes)
_FORBIDDEN_CHARS = re.compile(r"[<>`$%^@{}()?]|[^\x20-\x7e]")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;")

_READONLY_COMMANDS = {
    "cat", "type", "more", "less", "head", "tail", "ls", "dir", "pwd", "cd", "echo", "where", "which",
    "whoami", "date", "tree", "wc", "sort", "uniq", "findstr", "grep", "rg",
    "get-content", "get-childitem", "get-location", "select-string", "write-output", "write-host",
    "measure-object", "select-object", "where-object", "sort-object", "format-list", "format-table",
}
_GIT_READONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame"}
_GIT_BRANCH_FLAGS = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--verbose", "--show-current"}
_GIT_BAD_ARG = re.compile(r"^(--output|--ext-diff|--textconv|-O|--open-files-in-pager|--exec-path|--config|-c$)", re.I)
_INTERPRETERS = {"python": ".py .pyw", "python3": ".py .pyw", "py": ".py .pyw", "node": ".js .mjs .cjs"}
_PY_MODULES = {"py_compile", "pytest", "unittest"}
_INTERP_PREFLAG = re.compile(r"-(u|B|I|E|s|S|q|\d(\.\d+)?)$")


def _real(path: str, base: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.join(base, path)))


def _inside(path, workspace: str, base: str | None = None) -> bool:
    """True if path, fully resolved (.., symlinks, junctions, 8.3 names), lies in workspace."""
    try:
        if not isinstance(path, str) or not path or "\0" in path or path.startswith("~"):
            return False
        ws = os.path.normcase(os.path.realpath(workspace))
        rp = _real(path, base or workspace)
        return os.path.commonpath([rp, ws]) == ws
    except (ValueError, OSError):
        return False


def _bad_glob(pattern) -> bool:
    """A glob pattern that could reach outside its root: absolute, UNC, home, or with '..'."""
    if not isinstance(pattern, str):
        return True
    p = pattern.replace("/", "\\")
    return (p.startswith(("\\", "~")) or re.match(r"[A-Za-z]:", p) is not None
            or ".." in p.split("\\") or "\0" in p)


def _file_tool_ok(tool_name: str, i: dict, workspace: str) -> str | None:
    paths = [(k, v) for k, v in i.items() if _PATHISH_KEY.search(k) and k not in _GLOB_KEYS]
    for _, v in paths:
        if not isinstance(v, str) or not _inside(v, workspace):
            return None
    if not paths and tool_name not in {"Glob", "Grep", "LS"}:
        return None  # Read/Edit/Write without a path is ambiguous
    for k, tools in _GLOB_KEYS.items():
        if tool_name in tools and i.get(k) is not None and _bad_glob(i[k]):
            return None
    if tool_name in _WRITE_TOOLS:
        ws = os.path.normcase(os.path.realpath(workspace))
        for _, v in paths:
            rel = os.path.relpath(_real(v, workspace), ws)
            if rel in _PROTECTED_REL or _PROTECTED_DIRS & set(rel.split("\\")):
                return None
    verb = "edit" if tool_name in _WRITE_TOOLS else "read"
    return f"{verb} inside {workspace}"


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def _arg_ok(tok: str, base: str, workspace: str) -> bool:
    """Is this command-line argument harmless: a flag, or a path inside the workspace."""
    tok = _unquote(tok)
    if not tok:
        return True
    if tok.startswith("-"):
        if re.fullmatch(r"-+[\w-]*", tok):
            return True
        m = re.search(r"[=:]", tok)  # -Path:C:\x  or  --git-dir=C:\x
        tok = tok[m.end():] if m else ""
        if not tok:
            return True
    if re.fullmatch(r"/[A-Za-z?]", tok):
        return True  # cmd-style switch such as /s or /b
    return _inside(tok, workspace, base)


def _interpreter_ok(name: str, args: list[str], base: str, workspace: str) -> bool:
    exts = tuple(_INTERPRETERS[name].split())
    k = 0
    while k < len(args) and _INTERP_PREFLAG.fullmatch(args[k]):
        k += 1
    if k >= len(args):
        return False
    if args[k] == "-m":
        if name == "node" or k + 1 >= len(args) or _unquote(args[k + 1]) not in _PY_MODULES:
            return False
        rest = args[k + 2:]
    else:
        script = _unquote(args[k])
        if script.startswith("-") or not script.lower().endswith(exts) or not _inside(script, workspace, base):
            return False
        rest = args[k + 1:]
    return all(_arg_ok(t, base, workspace) for t in rest)


def _segment_ok(tokens: list[str], base: str, workspace: str) -> bool:
    if not tokens:
        return False
    first = _unquote(tokens[0])
    args = tokens[1:]
    if "\\" in first or "/" in first:
        # Only an interpreter that lives inside the workspace (e.g. the project venv) may be called by path.
        stem = os.path.basename(first.replace("/", "\\")).lower()
        stem = stem[:-4] if stem.endswith(".exe") else stem
        if stem not in _INTERPRETERS or not _inside(first, workspace, base):
            return False
        return _interpreter_ok(stem, args, base, workspace)
    name = first.lower()
    name = name[:-4] if name.endswith(".exe") else name

    if name in _INTERPRETERS:
        return _interpreter_ok(name, args, base, workspace)
    if name == "git":
        if not args:
            return False
        sub, rest = _unquote(args[0]).lower(), args[1:]
        if any(_GIT_BAD_ARG.match(_unquote(t)) for t in rest):
            return False
        if sub == "branch":  # listing only; a bare name would create a branch
            return all(_unquote(t).lower() in _GIT_BRANCH_FLAGS for t in rest)
        if sub == "remote":
            return [_unquote(t).lower() for t in rest] in (["-v"], ["--verbose"])
        if sub not in _GIT_READONLY:
            return False
        return all(_arg_ok(t, base, workspace) for t in rest)
    if name not in _READONLY_COMMANDS:
        return False
    plain = [_unquote(t) for t in args]
    if name == "sort" and any(re.match(r"(-o|--output|/o)", t, re.I) for t in plain):
        return False  # sort can write its result to a file
    if name == "rg" and any(re.match(r"--(pre|hostname-bin)", t, re.I) for t in plain):
        return False  # these run external programs
    if name == "date" and any(not t.startswith("+") for t in plain):
        return False  # cmd's date sets the clock
    if name == "cd":
        targets = [t for t in plain if not re.fullmatch(r"/d", t, re.I)]
        return len(targets) == 1 and targets[0] not in ("-", "+") and _arg_ok(targets[0], base, workspace)
    return all(_arg_ok(t, base, workspace) for t in args)


def command_is_readonly(command, cwd, workspace: str = DEFAULT_WORKSPACE) -> bool:
    """True if a shell command only prints things and touches nothing outside the workspace.

    Deliberately strict: it rejects redirection, expansion, subexpressions, script blocks,
    background '&', newlines and non-ASCII, then requires every pipe/;/&& segment to start with
    a known read-only command. Anything it cannot parse means 'ask'.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    base = workspace
    if cwd not in (None, ""):
        if not isinstance(cwd, str) or not _inside(cwd, workspace):
            return False
        base = os.path.join(workspace, cwd)
    if _FORBIDDEN_CHARS.search(command) or "&" in command.replace("&&", ""):
        return False
    for seg in _SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(seg, posix=False)
        except ValueError:
            return False
        if not _segment_ok(tokens, base, workspace):
            return False
    return True


def auto_approve(tool_name: str, tool_input: dict, workspace: str = DEFAULT_WORKSPACE) -> str | None:
    """A short reason if this tool call may run without asking the user, else None (ask)."""
    try:
        i = tool_input or {}
        if not isinstance(i, dict):
            return None
        if tool_name in _FILE_TOOLS:
            return _file_tool_ok(tool_name, i, workspace)
        if tool_name == "mcp__jarvis__run_in_terminal":
            if command_is_readonly(i.get("command"), i.get("cwd"), workspace):
                return f"read-only terminal command inside {workspace}"
            return None
        if tool_name in _SHELL_TOOLS:
            if i.get("run_in_background") or i.get("dangerouslyDisableSandbox"):
                return None
            if command_is_readonly(i.get("command"), None, workspace):
                return f"read-only shell command inside {workspace}"
        return None
    except Exception:
        return None
