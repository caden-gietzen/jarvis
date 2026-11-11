r"""Render a textbook chapter (Markdown) to a readable HTML page and open it in Chrome.

    python -m jarvis.mdpreview show <book-or-path> <chapter> [--passage "text"] [--open]
                                    [--to "end"] [--expand sentence|paragraph] [--occurrence N|last] [--caption "..."]
    python -m jarvis.mdpreview next [book] [--open] [--passage ...]   # chapter after the last shown one
    python -m jarvis.mdpreview prev [book] [--open] [--passage ...]
    python -m jarvis.mdpreview serve                                  # run the static server in the foreground

<book> is a folder name under the books root (default: a "textbooks" folder next to the jarvis repo, env JARVIS_BOOKS_ROOT) or a path to a
folder inside it. <chapter> is a file name (with or without .md), a leading number ("4" or "04"), or a
unique substring of the file name ("ch1", "singleton").

Pages are written to logs\mdpreview\<book>\<chapter>.html and served by a read-only static server
bound to 127.0.0.1 only (port 47825, env JARVIS_MDPREVIEW_PORT;
it refuses to start on the Jarvis web UI port 47823 or on a port already in use, never falling back). It serves /out/... (rendered pages),
/books/<book>/... (images, css, txt only; never html/svg/js), /books/workspace/... (images only) and /health, and nothing else.
Every response carries a strict Content-Security-Policy; raw HTML in the Markdown is escaped (comments dropped) except a few
harmless inline tags, and javascript:/data:/vbscript: link targets become "#".

The book name "workspace" selects the workspace folder (default: the "jarvis-workspace" folder next to the jarvis repo, env JARVIS_WORKSPACE_ROOT): its *.md files
(directly in it or in subfolders, e.g. "sub/report.md") can be shown too, and an absolute path to a .md inside either root works. The URL uses the hostname "localhost", which is on the
browser extension's default allowlist, so the existing highlight/scroll tools work on it.

The book name "study-notes" selects the study-notes folder inside the workspace folder (permanent study documents; next/prev walk it).
LaTeX math ($..$, \(..\), $$..$$, \[..\]) is protected from the Markdown engine and drawn in the page by the vendored
KaTeX (jarvis/vendor, served at /vendor/ from an exact allowlist; see jarvis/mathsupport.py and the README).
"""

import argparse
import html
import html.parser
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from jarvis import mathsupport

ROOT = Path(__file__).resolve().parent.parent
BOOKS_ROOT = Path(os.environ.get("JARVIS_BOOKS_ROOT", str(ROOT.parent / "textbooks")))
WORKSPACE_ROOT = Path(os.environ.get("JARVIS_WORKSPACE_ROOT", str(ROOT.parent / "jarvis-workspace")))
WORKSPACE_NAME = "workspace"  # the <book> argument / URL segment that selects WORKSPACE_ROOT
STUDY_NOTES_NAME = "study-notes"  # <book> alias for WORKSPACE_ROOT/study-notes (permanent Markdown study documents)
OUT_ROOT = ROOT / "logs" / "mdpreview"
STATE_PATH = ROOT / "logs" / "mdpreview_state.json"
DEFAULT_PORT = 47825
PORT = int(os.environ.get("JARVIS_MDPREVIEW_PORT", str(DEFAULT_PORT)))
WEB_UI_PORT = 47823  # jarvis.web.DEFAULT_PORT (kept as a constant so this module never imports the web UI)
CLI_PATH = ROOT / "chrome-ext" / "helper" / "jarvis_browser_cli.py"


class PreviewError(Exception):
    """User-facing error (printed without a traceback)."""


# ---------------------------------------------------------------- rendering

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]*)(")', re.I)

# script-src 'self' only serves the vendored KaTeX and jarvis-math.js (exact allowlist at /vendor/); the page's own
# markup never carries a script (raw <script> in Markdown is escaped), /books/ never serves js, /out/ only html we render.
CSP = ("default-src 'none'; script-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_ALLOWED_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "img", "table", "thead", "tbody", "tr", "th", "td",
                 "pre", "code", "ul", "ol", "li", "blockquote", "hr", "em", "strong", "br", "sup", "sub", "kbd", "span"}
_VOID_TAGS = {"br", "hr", "img"}
_PLAIN_ATTR = re.compile(r"[\w .:-]+")


def _safe_url(url: str) -> str:
    """javascript:/data:/vbscript: targets (even obfuscated with whitespace/control chars) become '#'."""
    if re.match(r"(javascript|data|vbscript):", re.sub(r"[\x00-\x20\x7f]+", "", url).lower()):
        return "#"
    return url


def _clean_attrs(tag: str, attrs) -> str:
    out = []
    for k, v in attrs:
        k, v = k.lower(), (v or "")
        if k == "id" and re.fullmatch(r"[\w.:-]+", v):
            pass
        elif (k == "href" and tag == "a") or (k == "src" and tag == "img"):
            v = _safe_url(v)
        elif k in ("title", "alt") and tag in ("a", "img"):
            pass
        elif k == "class" and tag in ("code", "pre") and _PLAIN_ATTR.fullmatch(v):
            pass
        elif k == "start" and tag == "ol" and v.isdigit():
            pass
        elif k == "style" and tag in ("th", "td") and re.fullmatch(r"text-align:\s*(left|right|center);?", v.strip()):
            pass
        else:
            continue  # every other attribute (on*, style, srcset, ...) is dropped
        out.append(f' {k}="{html.escape(v, quote=True)}"')
    return "".join(out)


class _Sanitizer(html.parser.HTMLParser):
    """Rebuilds the rendered body: allowlisted tags/attributes survive, other tags are shown as escaped text,
    comments are dropped. Code blocks are already escaped by the Markdown renderer, so they pass through unchanged."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in _ALLOWED_TAGS:
            self.out.append(f"<{tag}{_clean_attrs(tag, attrs)}>")
        else:
            self.out.append(html.escape(self.get_starttag_text() or f"<{tag}>"))

    def handle_startendtag(self, tag, attrs):
        if tag in _ALLOWED_TAGS:
            self.out.append(f"<{tag}{_clean_attrs(tag, attrs)}>" + ("" if tag in _VOID_TAGS else f"</{tag}>"))
        else:
            self.out.append(html.escape(self.get_starttag_text() or f"<{tag}/>"))

    def handle_endtag(self, tag):
        if tag in _ALLOWED_TAGS:
            if tag not in _VOID_TAGS:
                self.out.append(f"</{tag}>")
        else:
            self.out.append(html.escape(f"</{tag}>"))

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        self.out.append(html.escape(f"<!{decl}>"))

    def handle_pi(self, data):
        self.out.append(html.escape(f"<?{data}>"))

    def unknown_decl(self, data):
        self.out.append(html.escape(f"<![{data}]>"))


def sanitize_html(body: str) -> str:
    p = _Sanitizer()
    p.feed(body)
    p.close()
    return "".join(p.out)

CSS = """
:root{color-scheme:light dark;--bg:#fdfdfc;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--code:#f3f4f6;--link:#0b5fc1}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e4e6ea;--muted:#9aa2ad;--line:#343a42;--code:#22262c;--link:#6cb0ff}}
body{background:var(--bg);color:var(--fg);margin:0;font:18px/1.7 Georgia,'Iowan Old Style',serif}
main{max-width:46rem;margin:0 auto;padding:2rem 1.25rem 6rem}
h1,h2,h3,h4,h5,h6{font-family:system-ui,'Segoe UI',sans-serif;line-height:1.25;margin:2em 0 .6em;scroll-margin-top:1rem}
h1{font-size:2rem;margin-top:.5em}h2{font-size:1.5rem;border-bottom:1px solid var(--line);padding-bottom:.2em}h3{font-size:1.2rem}
a{color:var(--link)}
p,li{overflow-wrap:break-word}
img{max-width:100%;height:auto;display:block;margin:1rem auto}
code{font:0.88em/1.4 Consolas,'Cascadia Mono',monospace;background:var(--code);padding:.1em .3em;border-radius:4px}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:.8rem 1rem;overflow-x:auto;line-height:1.45}
pre code{background:none;padding:0;white-space:pre}
table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font:15px/1.45 system-ui,'Segoe UI',sans-serif;margin:1rem 0}
th,td{border:1px solid var(--line);padding:.4rem .7rem;vertical-align:top;text-align:left}
th{background:var(--code)}
blockquote{margin:1rem 0;padding:.1rem 1rem;border-left:4px solid var(--line);color:var(--muted)}
hr{border:0;border-top:1px solid var(--line);margin:2rem 0}
.jv-display{display:block;text-align:center;overflow-x:auto;overflow-y:hidden;margin:.8rem 0;padding:.2rem 0}
.jv-math .katex{font-size:1.1em}
.jv-math-error{font-family:Consolas,'Cascadia Mono',monospace;font-size:.9em;color:#b3261e;white-space:pre-wrap}
"""


def _rewrite_img(m, book: str, chapter_dir_rel: str) -> str:
    src = m.group(2)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^/|^#", src):  # absolute or other scheme: leave alone
        return m.group(0)
    rel = os.path.normpath(os.path.join(chapter_dir_rel, html.unescape(src))).replace("\\", "/")
    return f'{m.group(1)}/books/{quote(book)}/{quote(rel)}{m.group(3)}'


def render_markdown(text: str, book: str = "book", chapter_dir_rel: str = "", title: str = "") -> str:
    """Markdown text -> a complete self-contained HTML page (images point at /books/<book>/...)."""
    import markdown  # imported lazily so the server and argument parsing work without it
    text, nonce, math_items = mathsupport.protect_math(text)  # math must not reach the Markdown engine
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"], output_format="html")
    body = _COMMENT_RE.sub("", body)  # comment markers; inside code they are escaped, so this is safe
    body = sanitize_html(body)  # raw HTML in the Markdown is escaped (allowlist), dangerous link targets neutralised
    body = _IMG_SRC_RE.sub(lambda m: _rewrite_img(m, book, chapter_dir_rel), body)
    body = mathsupport.restore_math(body, nonce, math_items)  # inert <span data-tex>, rendered by KaTeX in the page
    if not title:
        t = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
        title = re.sub(r"<[^>]+>", "", t.group(1)) if t else "Chapter"
    math_head = math_tail = ""
    if math_items:  # the math assets are only linked from pages that contain math
        math_head = f'<link rel="stylesheet" href="{mathsupport.KATEX_CSS}">'
        math_tail = f'<script src="{mathsupport.KATEX_JS}"></script><script src="{mathsupport.MATH_JS}"></script>'
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(html.unescape(title))}</title>{math_head}<style>{CSS}</style></head>\n"
            f"<body><main>\n{body}\n</main>{math_tail}</body></html>\n")


# ---------------------------------------------------------------- books and chapters

def book_base(folder: Path, books_root: Path = None, workspace_root: Path = None):
    """-> (name used in URLs/state, folder that relative image paths and page paths are measured from)."""
    books_root = Path(books_root or BOOKS_ROOT)
    ws = Path(workspace_root or WORKSPACE_ROOT)
    try:
        f = folder.resolve()
        if not f.is_relative_to(books_root.resolve()) and f.is_relative_to(ws.resolve()):
            return WORKSPACE_NAME, ws.resolve()
    except OSError:
        pass
    return folder.name, folder


def resolve_book(book: str, books_root: Path = None, workspace_root: Path = None) -> Path:
    books_root = Path(books_root or BOOKS_ROOT)
    ws_root = Path(workspace_root or WORKSPACE_ROOT)
    if book.strip().lower() == WORKSPACE_NAME:
        if not ws_root.is_dir():
            raise PreviewError(f"workspace folder not found: {ws_root} (see SETUP.md section 3a, "
                               "or run scripts/ensure_workspace.py --clone)")
        return ws_root.resolve()
    if book.strip().lower() == STUDY_NOTES_NAME:
        sn = ws_root / STUDY_NOTES_NAME
        if not sn.is_dir():
            raise PreviewError(f"Study notes folder not found: {sn}")
        return sn.resolve()
    cand = Path(book)
    folder = cand if cand.is_absolute() or len(cand.parts) > 1 else books_root / book
    folder = folder.resolve()
    if not folder.is_dir():
        avail = ", ".join(sorted(p.name for p in books_root.iterdir() if p.is_dir())) if books_root.is_dir() else "none"
        raise PreviewError(f"Book not found: {book!r} (looked in {folder}). Available books: {avail}, {WORKSPACE_NAME}, {STUDY_NOTES_NAME}")
    if not (folder.is_relative_to(books_root.resolve()) or folder.is_relative_to(ws_root.resolve())):
        raise PreviewError(f"Book folder must be inside {books_root} (or the workspace {ws_root}) "
                           f"so its images can be served: {folder}")
    return folder


def list_chapters(folder: Path) -> list:
    """Chapter files in reading order (sorted by file name; a book's README.md is its table of contents, skipped)."""
    skip_readme = book_base(folder)[0] != WORKSPACE_NAME
    return sorted((p for p in folder.glob("*.md") if not (skip_readme and p.name.lower() == "readme.md")),
                  key=lambda p: p.name.lower())


def resolve_chapter(folder: Path, chapter: str) -> Path:
    chapters = list_chapters(folder)
    low = chapter.strip().lower()
    for p in chapters:
        if p.name.lower() in (low, low + ".md"):
            return p
    if low.isdigit():
        hits = [p for p in chapters if (m := re.match(r"(\d+)", p.name)) and int(m.group(1)) == int(low)]
        if len(hits) == 1:
            return hits[0]
    hits = [p for p in chapters if low and low in p.name.lower()]
    if len(hits) == 1:
        return hits[0]
    listing = "\n".join(f"  {p.name}" for p in (hits or chapters))
    if hits:
        raise PreviewError(f"Chapter {chapter!r} is ambiguous in {folder.name}; matches:\n{listing}")
    raise PreviewError(f"Chapter {chapter!r} not found in {folder.name}. Chapters:\n{listing}")


def locate_show(book: str, chapter: str):
    """`show` arguments -> (folder, markdown file). Accepts an absolute .md path inside either root, and for the
    workspace a relative path such as "sub/report.md"."""
    books_root, ws_root = BOOKS_ROOT.resolve(), WORKSPACE_ROOT.resolve()
    if Path(chapter).is_absolute():
        md = Path(chapter).resolve()
        if md.suffix.lower() != ".md" or not md.is_file():
            raise PreviewError(f"Not an existing Markdown file: {chapter}")
        if md.is_relative_to(books_root):
            rel = md.relative_to(books_root)
            if len(rel.parts) < 2:
                raise PreviewError(f"Markdown file is not inside a book folder: {md}")
            return books_root / rel.parts[0], md
        if md.is_relative_to(ws_root):
            return md.parent, md
        raise PreviewError(f"Markdown file must be inside {BOOKS_ROOT} or the workspace {WORKSPACE_ROOT}: {md}")
    folder = resolve_book(book)
    if book_base(folder)[0] == WORKSPACE_NAME and re.search(r"[\\/]", chapter):
        parts = [p for p in re.split(r"[\\/]+", chapter.strip()) if p]
        if not parts or any(p in (".", "..") or ":" in p for p in parts):
            raise PreviewError(f"Invalid workspace path: {chapter!r}")
        if not parts[-1].lower().endswith(".md"):
            parts[-1] += ".md"
        md = folder.joinpath(*parts).resolve()
        if not md.is_file() or not md.is_relative_to(ws_root):
            raise PreviewError(f"Workspace file not found: {chapter!r}")
        return md.parent, md
    return folder, resolve_chapter(folder, chapter)


def neighbour(folder: Path, current: str, step: int) -> Path:
    chapters = [p.name for p in list_chapters(folder)]
    if current not in chapters:
        raise PreviewError(f"Last shown chapter {current!r} is no longer in {folder.name}.")
    i = chapters.index(current) + step
    if not 0 <= i < len(chapters):
        raise PreviewError("Already at the last chapter." if step > 0 else "Already at the first chapter.")
    return folder / chapters[i]


def render_chapter(folder: Path, md_path: Path, out_root: Path = None) -> Path:
    out_root = Path(out_root or OUT_ROOT)
    name, base = book_base(folder)
    rel_dir = md_path.parent.relative_to(base).as_posix()
    page = render_markdown(md_path.read_text(encoding="utf-8"), name, "" if rel_dir == "." else rel_dir)
    out = out_root / name / md_path.relative_to(base).with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def page_url(book: str, stem: str, port: int = None) -> str:
    return f"http://localhost:{port or PORT}/out/{quote(book)}/{quote(stem, safe='/')}.html"


# ---------------------------------------------------------------- server

def safe_join(base: Path, rel: str):
    """Resolve rel under base; None if it is empty, has traversal/odd characters, or escapes base."""
    if not rel or "\x00" in rel or "\\" in rel or ":" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    try:
        target = (base / Path(*parts)).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    return target


# /books/ serves only these static types (never html/htm/svg/xhtml/js, which could run script on this origin);
# the workspace root serves images only.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
STATIC_TYPES = {**IMAGE_TYPES, ".css": "text/css", ".txt": "text/plain"}
HEALTH_BODY = json.dumps({"service": "jarvis-mdpreview"}).encode("utf-8")


def make_handler(books_root: Path, out_root: Path, port: int, workspace_root: Path = None):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    workspace_root = Path(workspace_root or WORKSPACE_ROOT)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisMdPreview"

        def log_message(self, *a):  # quiet
            pass

        def _security_headers(self):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")

        def _fail(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()

        def _serve(self, head: bool):
            if self.headers.get("Host", "") not in hosts:  # DNS-rebinding guard
                return self._fail(403)
            path = unquote(urlsplit(self.path).path)
            target = None
            ctype = None
            if path == "/health":
                return self._send(HEALTH_BODY, "application/json", head)
            if path.startswith("/out/"):
                target = safe_join(out_root, path[len("/out/"):])
                if target is not None:
                    if target.suffix.lower() != ".html":
                        return self._fail(403)
                    ctype = "text/html"
            elif path.startswith("/vendor/"):  # vendored KaTeX + jarvis-math.js: exact allowlist
                hit = mathsupport.vendor_file(path[len("/vendor/"):])
                if hit is not None:
                    target, ctype = hit
            elif path.startswith("/books/"):
                rest = path[len("/books/"):]
                allowed = STATIC_TYPES
                if rest.startswith(WORKSPACE_NAME + "/"):
                    target = safe_join(workspace_root, rest[len(WORKSPACE_NAME) + 1:])
                    allowed = IMAGE_TYPES
                else:
                    target = safe_join(books_root, rest)
                if target is not None:
                    ext = target.suffix.lower()
                    if ext == ".md":
                        target = None
                    elif ext not in allowed:
                        return self._fail(403)  # html, htm, svg, xhtml, js and anything else not allowlisted
                    else:
                        ctype = allowed[ext]
            if target is None:
                return self._fail(404)
            self._send(target.read_bytes(), ctype, head)

        def _send(self, data: bytes, ctype: str, head: bool):
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

        def _no(self):
            self._fail(405)
        do_POST = do_PUT = do_DELETE = do_PATCH = _no

    return Handler


class _ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let us bind a port that is already taken


def make_server(books_root: Path = None, out_root: Path = None, port: int = None,
                workspace_root: Path = None) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:port or raise PreviewError (web UI port, or port already in use). No fallback port."""
    port = PORT if port is None else port
    if port == WEB_UI_PORT:
        raise PreviewError(f"Port {port} is the Jarvis web UI port. Set JARVIS_MDPREVIEW_PORT to a different "
                           f"port (default {DEFAULT_PORT}).")
    try:
        srv = _ExclusiveServer(("127.0.0.1", port),
                               make_handler(Path(books_root or BOOKS_ROOT), Path(out_root or OUT_ROOT), port,
                                            Path(workspace_root or WORKSPACE_ROOT)))
    except OSError as e:
        raise PreviewError(f"Port {port} is already in use ({e}). The preview page URL must stay predictable, "
                           f"so there is no fallback: free the port or set JARVIS_MDPREVIEW_PORT.") from e
    srv.daemon_threads = True
    return srv


def port_open(port: int = None) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port or PORT), timeout=0.5):
            return True
    except OSError:
        return False


def is_preview_server(port: int = None) -> bool:
    """True if what listens on the port answers /health as the Jarvis preview server."""
    port = port or PORT
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        try:
            c.putrequest("GET", "/health", skip_host=True)
            c.putheader("Host", f"localhost:{port}")
            c.endheaders()
            r = c.getresponse()
            data = json.loads(r.read(1024).decode("utf-8", "replace"))
        finally:
            c.close()
        return r.status == 200 and isinstance(data, dict) and data.get("service") == "jarvis-mdpreview"
    except (OSError, ValueError, http.client.HTTPException):
        return False


def _foreign_port_error() -> PreviewError:
    return PreviewError(f"Port {PORT} is in use by something that is not the Jarvis preview server (no valid "
                        f"/health answer). Free it or set JARVIS_MDPREVIEW_PORT to another port.")


def ensure_server() -> None:
    """Start the server as a detached background process if nothing listens on the port."""
    if PORT == WEB_UI_PORT:
        raise PreviewError(f"Port {PORT} is the Jarvis web UI port; set JARVIS_MDPREVIEW_PORT to another port.")
    if port_open():
        if is_preview_server():
            return
        raise _foreign_port_error()
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen([sys.executable, "-m", "jarvis.mdpreview", "serve"], cwd=str(ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(40):
        if port_open():
            if is_preview_server():
                return
            raise _foreign_port_error()
        time.sleep(0.1)
    raise PreviewError(f"The preview server did not start on 127.0.0.1:{PORT}.")


# ---------------------------------------------------------------- state and browser

def load_state(path: Path = None) -> dict:
    try:
        return json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(book: str, chapter: str, path: Path = None) -> None:
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"book": book, "chapter": chapter}), encoding="utf-8")


def build_show_payload(passage: str, end_text=None, expand=None, occurrence=None, caption=None) -> dict:
    """The same payload jarvis_browser_cli.py builds for `show`."""
    payload = {"action": "show", "text": passage}
    if caption:
        payload["caption"] = caption
    if end_text:
        payload["endText"] = end_text
    if expand:
        payload["expand"] = expand
    if occurrence is not None:
        payload["occurrence"] = occurrence
    return payload


def browser_send(payload: dict):
    """Send one command through the browser CLI's send() (token + helper port handling). -> (status, body)."""
    spec = importlib.util.spec_from_file_location("jarvis_browser_cli", CLI_PATH)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    try:
        return cli.send(payload)
    except SystemExit as e:
        raise PreviewError("Could not reach the browser helper (is it running? see chrome-ext\\README.md).") from e


def show_in_browser(url: str, passage=None, open_page=False, send=None, retry_delay=0.7, **opts) -> list:
    send = send or browser_send
    results = []
    if open_page:
        # always a NEW tab: never replace the tab the user is looking at (user request 2026-11-07)
        results.append(send({"action": "open", "url": url, "newTab": True}))
    if passage:
        for attempt in range(3):  # the page may still be settling right after open
            res = send(build_show_payload(passage, **opts))
            if res[0] == 200 and res[1].get("found"):
                break
            if attempt < 2:
                time.sleep(retry_delay)
        results.append(res)
    return results


# ---------------------------------------------------------------- commands

def cmd_show(args, send=None) -> int:
    if args.cmd == "show":
        folder, md = locate_show(args.book, args.chapter)
    else:
        folder = resolve_book(args.book)
        name, base = book_base(folder)
        last = load_state()
        if last.get("book") != name or not last.get("chapter"):
            raise PreviewError("No chapter of this book shown yet. Use: show <book> <chapter>")
        step = 1 if args.cmd == "next" else -1
        if name == WORKSPACE_NAME:  # the walk stays in the folder of the last shown file
            cur = (base / last["chapter"]).resolve()
            if not cur.is_relative_to(base):
                raise PreviewError("Last shown workspace file is outside the workspace.")
            folder = cur.parent
            md = neighbour(folder, cur.name, step)
        else:
            md = neighbour(folder, last["chapter"], step)
    render_chapter(folder, md)
    name, base = book_base(folder)
    rel = md.relative_to(base)
    save_state(name, rel.as_posix())
    ensure_server()
    url = page_url(name, rel.with_suffix("").as_posix())
    print(f"{name}: {rel.as_posix()}\n{url}")
    if args.open or args.passage:
        results = show_in_browser(url, args.passage, args.open, send=send, end_text=args.to,
                                  expand=args.expand, occurrence=args.occurrence, caption=args.caption)
        for status, body in results:
            print(f"HTTP {status} {json.dumps(body, ensure_ascii=False)}")
        if any(s != 200 or not b.get("ok") for s, b in results):
            return 1
    return 0


def occurrence_arg(v: str):
    if v.strip().lower() == "last":
        return "last"
    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError('occurrence must be a positive integer or "last"')
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jarvis.mdpreview", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--passage", help="text to scroll to and highlight")
        sp.add_argument("--open", action="store_true", help="open the page in Chrome")
        sp.add_argument("--to", help="end phrase: highlight from --passage through this phrase")
        sp.add_argument("--expand", choices=["sentence", "paragraph"])
        sp.add_argument("--occurrence", type=occurrence_arg)
        sp.add_argument("--caption")
    s = sub.add_parser("show")
    s.add_argument("book")
    s.add_argument("chapter")
    common(s)
    for name in ("next", "prev"):
        n = sub.add_parser(name)
        n.add_argument("book", nargs="?", help="default: the last shown book")
        common(n)
    sub.add_parser("serve")
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            srv = make_server()
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        if args.cmd in ("next", "prev") and args.book is None:
            args.book = load_state().get("book")
            if not args.book:
                raise PreviewError("No chapter shown yet. Use: show <book> <chapter>")
        return cmd_show(args)
    except PreviewError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
