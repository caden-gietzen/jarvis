#!/usr/bin/env python3
"""Check a skill, agent or script for things that must not become public, before it is contributed.

    python scan_contribution.py <folder-or-file>           scan every file in a folder (or one file)
    python scan_contribution.py <git-ref>                  scan files changed versus a base ref (e.g. origin/main),
                                                           including uncommitted and new files; run inside the repo
    python scan_contribution.py <target> --json            machine-readable output
    python scan_contribution.py <target> --also-flag-file my-secrets.txt
                                                           also flag these literal strings (one per line); keep that
                                                           file OUTSIDE the repo. Env var CONTRIB_ALSO_FLAG works too
                                                           (newline-separated, or the name given with --also-flag-env)
    python scan_contribution.py <target> --allow           honour reviewed exceptions in <root>/.contribution-allow

Exit code: 0 = no blocking findings, 1 = at least one blocking finding, 2 = bad usage.
Warnings (high-entropy strings, OS-specific paths, ...) do not block unless --strict is given.
Findings are shown as file:line, the rule and a REDACTED snippet; a full secret value is never printed.

Allowlist file (.contribution-allow), one entry per line, '#' starts a comment (please give a reason):
    rule  path-glob[:line]
    email docs/example.md:12   # maintainer address in the docs, reviewed
    *     tests/fixtures/*     # fake values for tests
Private keys and your own --also-flag strings can never be allowed.

Python 3 standard library only; works on Windows, macOS and Linux.
"""
from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOW_FILE_NAME = ".contribution-allow"
MAX_BYTES = 1_000_000
NEVER_ALLOW = {"private-key", "user-flag"}
SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache"}
DEP_DIRS = {"node_modules", ".venv", "venv", "env"}
BINARY_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".jar", ".pyc", ".pyd", ".zip", ".tar", ".gz",
    ".tgz", ".7z", ".rar", ".bz2", ".xz", ".iso", ".dmg", ".msi", ".pkg", ".deb", ".rpm", ".whl", ".onnx", ".pt",
    ".pth", ".safetensors", ".sqlite", ".db", ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".flac", ".ogg",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".psd", ".woff", ".woff2", ".ttf", ".otf",
    ".vsix", ".crx", ".docx", ".xlsx", ".pptx",
}

# Literal fragments are built by concatenation so this file does not itself contain a home-path literal.
_USERS = "Us" + "ers"
_SEP = r"[\\/]+"
_HOME = "ho" + "me"
_APPS = "Appli" + "cations"
_LIB = "Lib" + "rary"

PLACEHOLDER_WORDS = (
    "example", "changeme", "change_me", "your", "placeholder", "dummy", "sample", "xxxx", "....", "redacted",
    "<", "${", "$(", "%", "{{", "environ", "getenv", "process.env", "none", "null", "true", "false", "todo",
)
PLACEHOLDER_USERS = {
    "user", "username", "you", "yourname", "your-name", "name", "me", "example", "public", "default", "shared",
    "runner", "all users", "all", "foo", "bar", "myuser", "home", "<user>", "<name>", "$user", "%username%",
}


def _is_placeholder_value(v: str) -> bool:
    low = v.lower()
    if low.startswith("$") or any(w in low for w in PLACEHOLDER_WORDS):
        return True
    if len(set(low)) <= 2:  # "aaaaaaaa", "********"
        return True
    return False


def _placeholder_user(name: str) -> bool:
    n = name.strip().lower()
    return n in PLACEHOLDER_USERS or n.startswith(("<", "$", "%", "{", "~", "*", "["))


def _v_email(m: re.Match) -> bool:
    addr = m.group(0).lower()
    local, _, domain = addr.rpartition("@")
    if domain in {"example.com", "example.org", "example.net", "localhost", "users.noreply.github.com", "github.com"}:
        return False if local != "git" and domain == "github.com" else False
    if domain.endswith((".example", ".test", ".invalid", ".localhost", ".example.com", ".example.org")):
        return False
    if "noreply" in local or "no-reply" in local or local in {"git", "you", "user", "name", "email"}:
        return False
    return True


def _v_ip(m: re.Match) -> bool:
    try:
        ip = ipaddress.ip_address(m.group(0))
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return False
    cgnat = ipaddress.ip_network("100." + "64.0.0/10")
    return ip.is_private or ip in cgnat


SLUG = re.compile(r"^[a-z]+(?:[-_.][a-z]+)+$")


def _v_assign(m: re.Match) -> bool:
    val = m.group("val")
    quoted = bool(m.group("q"))
    if _is_placeholder_value(val):
        return False
    if SLUG.match(val) or val.endswith(":"):  # a name like "github-token" or "Authorization:", not a credential
        return False
    if quoted:
        return len(val) >= 8
    # unquoted (YAML, .env style): only when it really looks like a credential
    return len(val) >= 16 and any(c.isdigit() for c in val) and any(c.isalpha() for c in val)


def _v_bearer(m: re.Match) -> bool:
    return not _is_placeholder_value(m.group("val"))


def _v_home(m: re.Match) -> bool:
    return not _placeholder_user(m.group("val"))


def _v_urlcred(m: re.Match) -> bool:
    return not _is_placeholder_value(m.group("val")) and not _placeholder_user(m.group("user"))


# (rule, severity, regex, message, redaction group, validator)
RULES = [
    ("private-key", "block", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----", "private key block", 0, None),
    ("aws-key", "block", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "AWS access key id", 0, None),
    ("github-token", "block", r"\bgh[pousr]_[A-Za-z0-9]{30,}", "GitHub token", 0, None),
    ("github-token", "block", r"\bgithub_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained token", 0, None),
    ("slack-token", "block", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "Slack token", 0, None),
    ("notion-key", "block", r"\b(?:ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{30,})", "Notion integration key", 0, None),
    ("anthropic-key", "block", r"\bsk-ant-[A-Za-z0-9_-]{10,}", "Anthropic API key", 0, None),
    ("openai-key", "block", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}", "OpenAI-style API key", 0, None),
    ("google-key", "block", r"\bAIza[0-9A-Za-z_-]{30,}", "Google API key", 0, None),
    ("jwt", "block", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "JSON web token", 0, None),
    ("bearer-token", "block", r"(?i)\bBearer\s+(?P<val>[A-Za-z0-9._~+/=-]{20,})", "Bearer token in a header", 0, _v_bearer),
    ("secret-assignment", "block",
     r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|credential|auth[_-]?key)[\w.-]*['\"]?\s*[:=]\s*"
     r"(?P<q>['\"]?)(?P<val>[^\s'\"#,;)\]}]{8,})",
     "secret-looking value assigned to a key/token/password name", "val", _v_assign),
    ("url-credentials", "block", r"\b[a-z][a-z0-9+.-]*://(?P<user>[^\s/:@'\"]+):(?P<val>[^\s/@'\"]+)@", "user:password inside a URL", "val", _v_urlcred),
    ("webhook-url", "block",
     r"https?://(?:hooks\.slack\.com/services/|(?:discord|discordapp)\.com/api/webhooks/|[\w.-]+\.webhook\.office\.com/|"
     r"outlook\.office\.com/webhook/|ntfy\.sh/[\w-]{6,}|[\w.-]*zapier\.com/hooks/|hooks\.zapier\.com/|"
     r"api\.telegram\.org/bot)[^\s'\"]*", "webhook / push-topic URL (it is a secret)", 0, None),
    ("email", "block", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
     "email address (use an obvious placeholder such as you" + "@example" + ".com)", 0, _v_email),
    ("home-path", "block", r"\b[A-Za-z]:" + _SEP + _USERS + _SEP + r"(?P<val>[^\\/\s\"'<>|:*?]+)",
     "absolute Windows home path (use ~, %USERPROFILE% or a relative path)", "val", _v_home),
    ("home-path", "block", r"(?<![\w.])/" + _USERS + r"/(?P<val>[^/\s\"'<>|:*?]+)",
     "absolute macOS home path (use ~ or a relative path)", "val", _v_home),
    ("home-path", "block", r"(?<![\w.])/" + _HOME + r"/(?P<val>[^/\s\"'<>|:*?]+)",
     "absolute Linux home path (use ~ or a relative path)", "val", _v_home),
    ("private-ip", "block", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)",
     "private / LAN / tailnet IP address", 0, _v_ip),
    ("private-host", "block", r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b", "Tailscale (*.ts.net) hostname", 0, None),
    ("private-host", "block", r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|home\.arpa)\b(?!\.?[\w-])",
     "local network hostname (*.local / *.lan)", 0, None),
    ("os-specific-path", "warn",
     r"(?:\b[A-Za-z]:\\[\w.-]|%(?:APPDATA|LOCALAPPDATA|PROGRAMFILES)%|(?<![\w.])/" + _APPS + r"/|~/" + _LIB + r"/|\bAppData[\\/])",
     "hard-coded OS-specific path: fine only if the skill says it is Windows-only/macOS-only, else make it portable", 0, None),
]
COMPILED = [(r, s, re.compile(p), msg, g, v) for r, s, p, msg, g, v in RULES]

FORBIDDEN_NAMES = [
    (".env", "environment file"), (".env.*", "environment file"), ("*.env", "environment file"),
    ("*.pem", "key/certificate file"), ("*.key", "key file"), ("*.p12", "key store"), ("*.pfx", "key store"),
    ("*.keystore", "key store"), ("id_rsa*", "ssh key"), ("id_ed25519*", "ssh key"), ("id_ecdsa*", "ssh key"),
    ("*token*", "file name looks like a token file (review; allow with a reason if it is plain source)"),
    ("*secret*", "file name looks like a secrets file"), ("credentials*", "credentials file"),
    (".netrc", "credentials file"), (".npmrc", "may contain tokens"), ("*.log", "log file"),
    (".DS_Store", "macOS junk file"), ("Thumbs.db", "Windows junk file"), ("*.sqlite", "database"),
    ("*.sqlite3", "database"), ("*.db", "database"), (".jarvis_*", "Jarvis runtime state/token file"),
]


def _entropy(s: str) -> float:
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
HEX_ONLY = re.compile(r"^[0-9a-fA-F]{32,}$")


def _entropy_hit(tok: str) -> bool:
    if "/" in tok and tok.count("/") >= 2:  # looks like a path
        return False
    core = tok.strip("=_-+/")
    if len(core) < 24:
        return False
    has_d = any(c.isdigit() for c in core)
    has_a = any(c.isalpha() for c in core)
    if not (has_d and has_a):
        return False
    if HEX_ONLY.match(core):
        return len(core) >= 32
    return _entropy(core) >= 4.3


class Finding:
    __slots__ = ("file", "line", "rule", "severity", "message", "snippet", "allowed", "span", "raw_len")

    def __init__(self, file, line, rule, severity, message, snippet="", span=None):
        self.file, self.line, self.rule, self.severity = file, line, rule, severity
        self.message, self.snippet, self.span = message, snippet, span
        self.allowed = False

    def as_dict(self):
        return {"file": self.file, "line": self.line, "rule": self.rule, "severity": self.severity,
                "message": self.message, "snippet": self.snippet, "allowed": self.allowed}


def redact(value: str) -> str:
    n = len(value)
    keep = min(4, n // 6)
    return f"{value[:keep]}[REDACTED {n} chars]"


def _merge(spans):
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def make_snippet(line: str, spans) -> str:
    spans = _merge(spans)
    parts, pos = [], 0
    first_start = spans[0][0] if spans else 0
    for s, e in spans:
        parts.append(line[pos:s])
        parts.append(redact(line[s:e]))
        pos = e
    parts.append(line[pos:])
    text = "".join(parts).strip()
    if len(text) > 160:
        # keep the window around the first redaction
        idx = text.find("[REDACTED")
        idx = max(0, idx - 60) if idx >= 0 else 0
        text = ("..." if idx else "") + text[idx:idx + 160] + "..."
    return text


def load_extra_strings(file_path: str | None, env_name: str | None):
    items = []
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        items += text.splitlines()
    for name in {env_name or "CONTRIB_ALSO_FLAG", "CONTRIB_ALSO_FLAG"}:
        val = os.environ.get(name)
        if val:
            items += val.splitlines() if "\n" in val else val.split(os.pathsep)
    out, short = [], 0
    for raw in items:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) < 4:
            short += 1
            continue
        out.append(s)
    return sorted(set(out)), short


def load_allowlist(path: Path):
    entries = []
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        parts = body.split()
        if len(parts) < 2:
            continue
        rule, target = parts[0], parts[1]
        line = None
        m = re.match(r"^(.*):(\d+)$", target)
        if m and not re.match(r"^[A-Za-z]$", m.group(1)):
            target, line = m.group(1), int(m.group(2))
        entries.append((rule, target.replace("\\", "/"), line))
    return entries


def is_allowed(f: Finding, entries) -> bool:
    if f.rule in NEVER_ALLOW:
        return False
    for rule, glob, line in entries:
        if rule not in ("*", f.rule):
            continue
        if not fnmatch.fnmatch(f.file, glob):
            continue
        if line is not None and line != f.line:
            continue
        return True
    return False


def decode(data: bytes):
    """Return text, or None if the data is binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(rel: str, text: str, extras):
    findings = []
    extras_l = [(s, s.lower()) for s in extras]
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        hits = []  # (rule, severity, message, span_to_redact)
        covered = []
        for rule, sev, rx, msg, grp, val in COMPILED:
            for m in rx.finditer(line):
                if val is not None and not val(m):
                    continue
                span = m.span(grp) if grp else m.span()
                hits.append((rule, sev, msg, span))
                covered.append(m.span())
        low = line.lower()
        for s, sl in extras_l:
            start = 0
            while True:
                i = low.find(sl, start)
                if i < 0:
                    break
                hits.append(("user-flag", "block", "matches a string from your own --also-flag list", (i, i + len(s))))
                covered.append((i, i + len(s)))
                start = i + len(s)
        for m in ENTROPY_TOKEN.finditer(line):
            if any(m.start() < ce and cs < m.end() for cs, ce in covered):
                continue
            if _entropy_hit(m.group(0)):
                hits.append(("high-entropy", "warn", "long random-looking string: key, token or hash? "
                             "(review; allow if it is harmless)", m.span()))
        if not hits:
            continue
        snippet = make_snippet(line, [h[3] for h in hits])
        seen = set()
        for rule, sev, msg, span in hits:
            key = rule
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(rel, lineno, rule, sev, msg, snippet, span))
    return findings


def scan_file(root: Path, rel: str, path: Path, extras):
    findings = []
    name = path.name
    parts = rel.split("/")
    for pat, why in FORBIDDEN_NAMES:
        if fnmatch.fnmatch(name.lower(), pat.lower()) or fnmatch.fnmatch(name, pat):
            findings.append(Finding(rel, 0, "forbidden-file", "block", f"should not be contributed: {why}"))
            break
    if "logs" in parts[:-1] or "log" in parts[:-1]:
        findings.append(Finding(rel, 0, "forbidden-file", "block", "should not be contributed: log folder"))
    if any(p in DEP_DIRS for p in parts[:-1]):
        findings.append(Finding(rel, 0, "forbidden-file", "block", "dependency/virtualenv folder; do not contribute"))
        return findings
    if path.is_symlink():
        findings.append(Finding(rel, 0, "symlink", "warn", "symbolic link skipped (links break on other machines)"))
        return findings
    try:
        size = path.stat().st_size
    except OSError as e:
        findings.append(Finding(rel, 0, "unreadable", "warn", f"could not read: {e}"))
        return findings
    if size > MAX_BYTES:
        findings.append(Finding(rel, 0, "large-file", "block", f"{size / 1e6:.1f} MB is over the 1 MB limit"))
        return findings
    if path.suffix.lower() in BINARY_EXTS:
        findings.append(Finding(rel, 0, "binary-file", "block", f"binary/media file type ({path.suffix.lower()}); skills are text"))
        return findings
    try:
        data = path.read_bytes()
    except OSError as e:
        findings.append(Finding(rel, 0, "unreadable", "warn", f"could not read: {e}"))
        return findings
    text = decode(data)
    if text is None:
        findings.append(Finding(rel, 0, "binary-file", "block", "binary content; skills are text"))
        return findings
    findings += scan_text(rel, text, extras)
    # file names themselves may carry a flagged string
    for s in extras:
        if s.lower() in rel.lower():
            findings.append(Finding(rel, 0, "user-flag", "block", "file name contains a string from your --also-flag list",
                                    redact(s)))
    return findings


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def collect_dir(target: Path):
    root = target if target.is_dir() else target.parent
    files = []
    if target.is_file():
        return root, [(target.name, target)]
    for dirpath, dirnames, filenames in os.walk(target):
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS:
                continue
            if d in DEP_DIRS:
                files.append((str((Path(dirpath) / d).relative_to(root)).replace("\\", "/") + "/", Path(dirpath) / d))
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            files.append((str(p.relative_to(root)).replace("\\", "/"), p))
    return root, files


def collect_git(base: str):
    top = _git(["rev-parse", "--show-toplevel"], ".")
    if top.returncode != 0:
        raise SystemExit("error: not inside a git repository (needed to compare against a base ref)")
    root = Path(top.stdout.strip())
    if _git(["rev-parse", "--verify", "--quiet", base + "^{commit}"], root).returncode != 0:
        raise SystemExit(f"error: '{base}' is neither an existing path nor a git ref")
    changed = _git(["diff", "--name-only", "--diff-filter=ACMR", "-z", base], root).stdout.split("\0")
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], root).stdout.split("\0")
    names = sorted({n for n in changed + untracked if n})
    return root, [(n, root / n) for n in names if (root / n).exists()]


def run(args) -> tuple[dict, int]:
    extras, short = load_extra_strings(args.also_flag_file, args.also_flag_env)
    target = args.target
    if not args.ref and os.path.exists(target):
        root, files = collect_dir(Path(target).resolve())
        mode = "path"
    else:
        root, files = collect_git(target)
        mode = "git-diff"
    allow_path = Path(args.allow_file) if args.allow_file else root / ALLOW_FILE_NAME
    entries = load_allowlist(allow_path) if (args.allow or args.allow_file) else []
    findings = []
    scanned = 0
    for rel, p in files:
        if rel == ALLOW_FILE_NAME:
            findings.append(Finding(rel, 0, "allowlist-file", "warn",
                                    "reviewed-exceptions file: do not contribute it; list the exceptions in the pull request instead"))
            continue
        if rel.endswith("/"):  # a dependency folder
            findings.append(Finding(rel, 0, "forbidden-file", "block", "dependency/virtualenv folder; do not contribute"))
            continue
        scanned += 1
        findings += scan_file(root, rel, p, extras)
    for prefix in args.expect or []:
        pre = prefix.replace("\\", "/")
        for rel, _ in files:
            if not rel.startswith(pre) and rel != "skills/README.md" and not rel.endswith("/"):
                findings.append(Finding(rel, 0, "outside-scope", "warn",
                                        f"changed file outside {pre} (only the one listing line in skills/README.md is expected)"))
    for f in findings:
        f.allowed = is_allowed(f, entries)
    if args.strict:
        for f in findings:
            if f.severity == "warn":
                f.severity = "block"
    findings.sort(key=lambda f: (f.file, f.line, f.rule))
    blocking = [f for f in findings if f.severity == "block" and not f.allowed]
    warns = [f for f in findings if f.severity == "warn" and not f.allowed]
    allowed = [f for f in findings if f.allowed]
    result = {
        "mode": mode, "root": str(root), "files_scanned": scanned,
        "blocking": len(blocking), "warnings": len(warns), "allowed": len(allowed),
        "extra_strings_checked": len(extras), "extra_strings_ignored_too_short": short,
        "findings": [f.as_dict() for f in findings],
    }
    return result, (1 if blocking else 0)


def summary_line(r: dict) -> str:
    status = "FAIL" if r["blocking"] else "PASS"
    return (f"SCAN RESULT: {status} - {r['files_scanned']} files scanned, {r['blocking']} blocking, "
            f"{r['warnings']} warnings, {r['allowed']} allowed (reviewed)")


def print_report(r: dict, out=None):
    out = out or sys.stdout
    groups = [("BLOCKING (fix these before contributing)", lambda f: f["severity"] == "block" and not f["allowed"]),
              ("WARNINGS (review each one; fix, or say why it is fine in the pull request)",
               lambda f: f["severity"] == "warn" and not f["allowed"]),
              ("ALLOWED by your allowlist (list these in the pull request)", lambda f: f["allowed"])]
    print(f"Scanned {r['files_scanned']} file(s) under {r['root']} ({r['mode']})", file=out)
    for title, pred in groups:
        items = [f for f in r["findings"] if pred(f)]
        if not items:
            continue
        print(f"\n{title}:", file=out)
        for f in items:
            where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            print(f"  {where}  [{f['rule']}] {f['message']}", file=out)
            if f["snippet"]:
                print(f"      {f['snippet']}", file=out)
    if r["extra_strings_checked"]:
        print(f"\nAlso checked {r['extra_strings_checked']} string(s) from your own list (never printed).", file=out)
    if r["extra_strings_ignored_too_short"]:
        print(f"Ignored {r['extra_strings_ignored_too_short']} entries shorter than 4 characters in your list.", file=out)
    print("\n" + summary_line(r), file=out)
    if r["blocking"]:
        print("Do not contribute until the blocking findings are fixed. If one is a reviewed false positive, "
              "add it to .contribution-allow and re-run with --allow (private keys can never be allowed).", file=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a folder or file to scan, or a git ref (e.g. origin/main) to scan the files changed versus it")
    ap.add_argument("--ref", action="store_true", help="treat target as a git ref even if a path with that name exists")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--strict", action="store_true", help="treat warnings as blocking too")
    ap.add_argument("--allow", action="store_true", help=f"honour reviewed exceptions in <root>/{ALLOW_FILE_NAME}")
    ap.add_argument("--allow-file", help="use this allowlist file instead (implies --allow)")
    ap.add_argument("--also-flag-file", help="file with extra literal strings to flag, one per line (keep it outside the repo)")
    ap.add_argument("--also-flag-env", help="name of an environment variable holding extra strings to flag "
                                           "(default CONTRIB_ALSO_FLAG; newline- or path-separator-separated)")
    ap.add_argument("--expect", action="append", metavar="PREFIX",
                    help="git mode: warn about changed files outside this path prefix, e.g. skills/my-skill/ (repeatable)")
    args = ap.parse_args(argv)
    try:
        result, code = run(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    if args.json:
        result["summary"] = summary_line(result)
        print(json.dumps(result, indent=2))
    else:
        print_report(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
