"""LaTeX math support shared by the web UI server and the Markdown preview server.

* The vendored KaTeX (jarvis/vendor/katex, see VERSION.txt there) and jarvis/vendor/jarvis-math.js are served
  from an exact allowlist (`vendor_file`): nothing else under vendor/ is ever reachable, nothing comes from the
  internet.
* `protect_math` / `restore_math` let the Markdown preview keep math away from the Markdown engine (underscores,
  asterisks, backslashes would otherwise be mangled) and put it back as inert
  <span class="jv-math" data-tex="..."> elements that jarvis-math.js renders with KaTeX (trust off).

The delimiter rules mirror vendor/jarvis-math.js: $...$ and \\(...\\) inline, $$...$$ and \\[...\\] display, "\\$" a
literal dollar; an opening $ must be followed by non-space, the closing $ preceded by non-space and not followed by
a digit. Code (fenced blocks and inline code spans) is never scanned. Indented code blocks are not recognised.
"""

import html
import re
import secrets
from pathlib import Path

VENDOR_DIR = Path(__file__).with_name("vendor")
KATEX_JS = "/vendor/katex/katex.min.js"
KATEX_CSS = "/vendor/katex/katex.min.css"
MATH_JS = "/vendor/jarvis-math.js"

_FONT_RE = re.compile(r"katex/fonts/KaTeX_[A-Za-z0-9-]+\.woff2")
_FILES = {
    "katex/katex.min.js": "application/javascript",
    "katex/katex.min.css": "text/css",
    "katex/LICENSE": "text/plain",
    "katex/VERSION.txt": "text/plain",
    "jarvis-math.js": "application/javascript",
}


def vendor_file(rel: str):
    """/vendor/<rel> -> (path, content type) for an allowlisted file that exists, else None."""
    if _FONT_RE.fullmatch(rel):
        ctype = "font/woff2"
    elif rel in _FILES:
        ctype = _FILES[rel]
    else:
        return None
    p = VENDOR_DIR / rel
    return (p, ctype) if p.is_file() else None


# ---------------------------------------------------------------- scanning

MAX_MATH_PER_DOC = 5000


def _is_space(ch: str) -> bool:
    return ch == "" or ch.isspace()


def _delim(s: str, i: int, open_len: int, close: str, display: bool):
    j = s.find(close, i + open_len)
    if j == -1:
        return None
    tex = s[i + open_len:j]
    if not tex.strip():
        return None
    return j + len(close), tex, display


def math_at(s: str, i: int):
    """(end, tex, display) for the math starting at s[i], else None."""
    c = s[i]
    if c == "\\":
        n = s[i + 1:i + 2]
        if n == "(":
            return _delim(s, i, 2, "\\)", False)
        if n == "[":
            return _delim(s, i, 2, "\\]", True)
        return None
    if c != "$":
        return None
    if s[i + 1:i + 2] == "$":
        return _delim(s, i, 2, "$$", True)
    if _is_space(s[i + 1:i + 2]):
        return None
    j = i + 1
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n" and s[j + 1:j + 2] == "\n":
            return None
        if ch == "$":
            if _is_space(s[j - 1]) or s[j + 1:j + 2].isdigit():
                return None
            return j + 1, s[i + 1:j], False
        j += 1
    return None


_FENCE_RE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})")


def protect_math(text: str):
    """Replace math (outside code) by tokens. -> (text, nonce, items) with items[n] = (raw, tex, display)."""
    nonce = secrets.token_hex(6).upper()
    out, items = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if (i == 0 or text[i - 1] == "\n") and c in " `~":
            m = _FENCE_RE.match(text, i)
            if m:  # fenced code block: copy through its closing fence line (or the end)
                fence = m.group(1)
                close = re.compile(r"^[ ]{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$", re.M)
                eol = text.find("\n", m.end())
                cm = close.search(text, eol + 1) if eol != -1 else None
                end = (text.find("\n", cm.end()) + 1 or n) if cm else n
                out.append(text[i:end])
                i = end
                continue
        if c == "`":
            k = 1
            while i + k < n and text[i + k] == "`":
                k += 1
            j = text.find("`" * k, i + k)
            while j != -1 and (text[j + k:j + k + 1] == "`"):  # a longer run is not a closer
                j = text.find("`" * k, j + k + 1)
            if j == -1:
                out.append(text[i:i + k])
                i += k
            else:
                out.append(text[i:j + k])
                i = j + k
            continue
        if c == "\\" and text[i + 1:i + 2] == "$":
            out.append("&#36;")  # escaped dollar: literal "$" (Markdown does not know this escape)
            i += 2
            continue
        if c in "$\\" and len(items) < MAX_MATH_PER_DOC:
            m = math_at(text, i)
            if m:
                end, tex, display = m
                out.append(f"JVMATH{nonce}N{len(items)}Z")
                items.append((text[i:end], tex, display))
                i = end
                continue
        out.append(c)
        i += 1
    return "".join(out), nonce, items


def restore_math(body: str, nonce: str, items) -> str:
    """Put the math back into rendered/sanitised HTML: as inert spans in text, as the raw source inside tags."""
    if not items:
        return body
    tok = re.compile(rf"JVMATH{nonce}N(\d+)Z")

    def span(m):
        raw, tex, display = items[int(m.group(1))]
        cls = "jv-math jv-display" if display else "jv-math"
        return (f'<span class="{cls}" data-tex="{html.escape(tex, quote=True)}">'
                f"{html.escape(raw, quote=False)}</span>")

    def plain(m):
        return html.escape(items[int(m.group(1))][0], quote=True)

    parts = re.split(r"(<[^>]*>)", body)
    for k, part in enumerate(parts):
        if k % 2:
            parts[k] = tok.sub(plain, part)  # inside a tag (an attribute value): text only, never markup
        else:
            parts[k] = tok.sub(span, part)
    return "".join(parts)
