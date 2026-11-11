"""Which shell requests may NOT be approved from the web page (voice only).

A permission prompt for a shell tool whose command matches DESTRUCTIVE_SHELL_PATTERNS gets no
Approve/Deny buttons on the page and the /permission endpoint never accepts it: only the spoken
yes/no (or the keyboard) can allow it. The check is deliberately over-eager: a false positive
costs one spoken "yes", a false negative lets a phone approve something destructive.
It fails closed: a shell request whose command is missing or not text counts as destructive.
Normal installs (pip install, uv pip install, npm install) are NOT destructive.
"""

import re

SHELL_TOOLS = frozenset({"Bash", "PowerShell", "mcp__jarvis__run_in_terminal"})

# Case-insensitive regexes, searched in the whole command (every line, every pipeline stage).
DESTRUCTIVE_SHELL_PATTERNS = (
    r"(?<![\w.-])(rm|rmdir|rd|del|erase|ri|unlink|shred|mv|move)(\.exe)?(?![\w-])",   # delete / move away
    r"remove-item",                         # also matches Remove-ItemProperty
    r"clear-(content|item|recyclebin)",
    r"(?<![\w-])format(\.com)?(?![\w-])",   # disk format (but not --format json)
    r"format-volume|mkfs|diskpart|\bdd\s+if=",
    r"\bgit\s+(-\S+\s+)*reset\s+.*--hard",
    r"\bgit\s+(-\S+\s+)*clean\b",
    r"\bgit\s+(-\S+\s+)*push\b",            # any push
    r"\bgit\s+(-\S+\s+)*(checkout|restore)\b.*(\s--(\s|$)|\s\.(\s|$)|--source)",
    r"\bgit\s+(-\S+\s+)*(branch\s+-D|stash\s+(drop|clear)|filter-branch|reflog\s+expire|gc\s+--prune)",
    r"--force|(?<!\w)-force\b|--no-preserve-root|\bgit\b[^\n]*\s-f\b",
    r"(?<![\w-])-(?:rf|fr)\b",   # -rf, -fr, -Rf (combined short flags only; "-First" and "-Filter" must NOT match)
    r"-recurse\b.*-force|-force\b.*-recurse",
    r"\bshutdown(\.exe)?\b|\brestart-computer\b|\bstop-computer\b",
    r"\btaskkill\b|\bstop-process\b|\bkill(all)?\b|\bpkill\b|\bwmic\b.*\bdelete\b",
    r"\breg(\.exe)?\s+(add|delete|import|copy|save|restore)\b",
    r"\bnew-itemproperty\b|\bset-itemproperty\b|\bremove-itemproperty\b",
    r"\bicacls\b|\bchmod\b|\bchown\b|\btakeown\b|\bcacls\b|\bset-acl\b|\bnet\s+(user|localgroup)\b",
    r"\bsc(\.exe)?\s+(delete|stop|config)\b|\bset-executionpolicy\b|\bbcdedit\b|\bvssadmin\b",
    r"\b(curl|wget|iwr|invoke-webrequest|invoke-restmethod|irm)\b[^\n]*\|\s*(sh|bash|zsh|pwsh|powershell|python\d?|cmd|iex)\b",
    r"\|\s*(sh|bash|zsh|iex|invoke-expression)\b",
    r"\|\s*&?\s*\(?\s*\[?scriptblock\]?",
    r"(?<![\w-])iex\b|invoke-expression",
    r"-enc(odedcommand)?\b|frombase64string",   # hides what actually runs
)

_COMPILED = tuple(re.compile(p, re.I) for p in DESTRUCTIVE_SHELL_PATTERNS)


def is_shell_tool(tool_name: str) -> bool:
    return tool_name in SHELL_TOOLS


def is_destructive_command(command) -> bool:
    """True if the command text matches a destructive pattern (or is not usable text at all)."""
    if not isinstance(command, str) or not command.strip():
        return True
    return any(p.search(command) for p in _COMPILED)


def is_destructive_shell(tool_name: str, tool_input) -> bool:
    """True if this is a shell request that must be answered by voice. Non-shell tools: False."""
    if not is_shell_tool(tool_name):
        return False
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    return is_destructive_command(cmd)
