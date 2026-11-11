r"""Offline tests for jarvis.mdpreview (Markdown chapter -> HTML, local static server, next/prev, browser calls).

Run (base interpreter):
    .venv\Scripts\python.exe tests\test_mdpreview.py

Plain script, no pytest needed. Never opens Chrome, never touches the real state files.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"
import contextlib
import http.client
import os
import io
import json
import re
import site
import socket
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from jarvis import mdpreview as mp  # noqa: E402

CHAPTER = """# Chapter One

Intro paragraph with *emphasis* and a [link](#sec).

<!-- transcribed-from-image: 00003 -->
## Section A

| Name | Value |
|---|---|
| alpha | 1 |

![Diagram](images/00002.jpeg)

```python
x = 1  # <!-- not a marker --> kept
print("a very long line " * 20)
```
"""


def make_books(tmp: Path) -> Path:
    books = tmp / "books"
    b = books / "demo"
    (b / "images").mkdir(parents=True)
    (b / "images" / "00002.jpeg").write_bytes(b"JPEGDATA")
    (b / "00-front.md").write_text("# Front\n\ntext", encoding="utf-8")
    (b / "01-ch1-intro.md").write_text(CHAPTER, encoding="utf-8")
    (b / "02-ch2-second.md").write_text("# Second\n\nbody", encoding="utf-8")
    (b / "README.md").write_text("# TOC", encoding="utf-8")
    (tmp / "secret.txt").write_text("SECRET", encoding="utf-8")
    return books


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path, skip_host=True)
    c.putheader("Host", host or f"localhost:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def test_render():
    page = mp.render_markdown(CHAPTER, "demo")
    assert "<h1 id=\"chapter-one\">Chapter One</h1>" in page, page[:300]
    assert 'id="section-a"' in page
    assert "<table>" in page and "<th>Name</th>" in page and "<td>alpha</td>" in page
    assert "<pre>" in page and "<code" in page and "a very long line" in page
    assert "transcribed-from-image" not in page  # marker hidden
    assert "&lt;!-- not a marker --&gt;" in page  # comment text inside code is kept (escaped)
    assert "prefers-color-scheme:dark" in page and "overflow-x:auto" in page
    assert "<title>Chapter One</title>" in page


def test_image_resolution():
    page = mp.render_markdown(CHAPTER, "demo")
    assert 'src="/books/demo/images/00002.jpeg"' in page, page
    nested = mp.render_markdown("![x](../images/a b.png)", "demo", "sub")
    assert 'src="/books/demo/images/a%20b.png"' in nested, nested
    ext = mp.render_markdown("![x](https://example.com/a.png)", "demo")
    assert 'src="https://example.com/a.png"' in ext


def test_chapter_resolution_and_order():
    with tempfile.TemporaryDirectory() as d:
        books = make_books(Path(d))
        folder = mp.resolve_book("demo", books)
        names = [p.name for p in mp.list_chapters(folder)]
        assert names == ["00-front.md", "01-ch1-intro.md", "02-ch2-second.md"], names
        assert mp.resolve_chapter(folder, "01-ch1-intro.md").name == "01-ch1-intro.md"
        assert mp.resolve_chapter(folder, "1").name == "01-ch1-intro.md"
        assert mp.resolve_chapter(folder, "ch2").name == "02-ch2-second.md"
        assert mp.neighbour(folder, "01-ch1-intro.md", 1).name == "02-ch2-second.md"
        assert mp.neighbour(folder, "01-ch1-intro.md", -1).name == "00-front.md"
        for cur, step in (("02-ch2-second.md", 1), ("00-front.md", -1)):
            try:
                mp.neighbour(folder, cur, step)
                raise AssertionError("expected PreviewError")
            except mp.PreviewError as e:
                assert "Already at the" in str(e)


def test_missing_chapter_and_book():
    with tempfile.TemporaryDirectory() as d:
        books = make_books(Path(d))
        folder = mp.resolve_book("demo", books)
        try:
            mp.resolve_chapter(folder, "nonexistent")
            raise AssertionError("expected PreviewError")
        except mp.PreviewError as e:
            assert "Chapter 'nonexistent' not found in demo" in str(e) and "01-ch1-intro.md" in str(e), str(e)
        try:
            mp.resolve_book("nobook", books)
            raise AssertionError("expected PreviewError")
        except mp.PreviewError as e:
            assert "Book not found" in str(e) and "demo" in str(e), str(e)
        try:
            mp.resolve_book(str(Path(d)), books)  # outside the books root
            raise AssertionError("expected PreviewError")
        except mp.PreviewError as e:
            assert "must be inside" in str(e)


def test_server_and_traversal():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        books = make_books(tmp)
        out = tmp / "out"
        folder = mp.resolve_book("demo", books)
        mp.render_chapter(folder, folder / "01-ch1-intro.md", out)
        port = free_port()
        srv = mp.make_server(books, out, port)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            assert srv.server_address[0] == "127.0.0.1"
            st, body = get(port, "/out/demo/01-ch1-intro.html")
            assert st == 200 and b"Chapter One" in body
            st, body = get(port, "/books/demo/images/00002.jpeg")
            assert st == 200 and body == b"JPEGDATA"
            bad = ["/books/demo/../../secret.txt", "/books/%2e%2e/secret.txt", "/books/demo/%2e%2e/%2e%2e/secret.txt",
                   "/out/..%2f..%2fsecret.txt", "/books/demo\\..\\..\\secret.txt", "/books/demo/01-ch1-intro.md",
                   "/books/C:/Windows/win.ini", "/etc/passwd", "/", "/books/", "/out/demo/"]
            for p in bad:
                st, body = get(port, p)
                assert st in (403, 404) and b"SECRET" not in body, (p, st)
            st, _ = get(port, "/out/demo/01-ch1-intro.html", host="evil.example.com")
            assert st == 403
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/out/demo/01-ch1-intro.html", body=b"x")
            assert c.getresponse().status == 405
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()


def test_default_port_and_web_port_clash():
    import re
    assert mp.DEFAULT_PORT == 47825 and mp.WEB_UI_PORT == 47823
    web_src = (ROOT / "jarvis" / "web.py").read_text(encoding="utf-8")
    m = re.search(r"^DEFAULT_PORT\s*=\s*(\d+)", web_src, re.M)
    assert m and int(m.group(1)) == mp.WEB_UI_PORT, "web UI port changed: update mdpreview.WEB_UI_PORT"
    assert mp.DEFAULT_PORT != mp.WEB_UI_PORT
    try:
        mp.make_server(port=mp.WEB_UI_PORT)
        raise AssertionError("expected PreviewError")
    except mp.PreviewError as e:
        assert "web UI port" in str(e)
    saved = mp.PORT
    mp.PORT = mp.WEB_UI_PORT
    try:
        mp.ensure_server()
        raise AssertionError("expected PreviewError")
    except mp.PreviewError:
        pass
    finally:
        mp.PORT = saved


def test_port_in_use_refuses():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            mp.make_server(port=port)
            raise AssertionError("expected PreviewError")
        except mp.PreviewError as e:
            assert "already in use" in str(e) and str(port) in str(e)
        saved = mp.PORT
        mp.PORT = port
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                assert mp.main(["serve"]) == 2
        finally:
            mp.PORT = saved
        assert "already in use" in err.getvalue()


def test_payload_and_browser_calls():
    assert mp.build_show_payload("abc") == {"action": "show", "text": "abc"}
    full = mp.build_show_payload("abc", end_text="xyz", expand="sentence", occurrence="last", caption="c")
    assert full == {"action": "show", "text": "abc", "caption": "c", "endText": "xyz",
                    "expand": "sentence", "occurrence": "last"}, full
    sent = []

    def fake(payload):
        sent.append(payload)
        return 200, {"ok": True, "found": True}

    res = mp.show_in_browser("http://localhost:1/out/x/y.html", "phrase", True, send=fake, expand="paragraph")
    assert sent[0] == {"action": "open", "url": "http://localhost:1/out/x/y.html", "newTab": True}, sent
    assert sent[1] == {"action": "show", "text": "phrase", "expand": "paragraph"}, sent
    assert len(res) == 2
    # retries when not found, then returns the last result
    sent.clear()
    nf = lambda p: (sent.append(p), (200, {"ok": True, "found": False}))[1]  # noqa: E731
    mp.show_in_browser("u", "p", False, send=nf, retry_delay=0)
    assert len(sent) == 3 and all(p["action"] == "show" for p in sent)


def test_cmd_show_next_prev_state():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        books = make_books(tmp)
        saved = (mp.BOOKS_ROOT, mp.OUT_ROOT, mp.STATE_PATH, mp.ensure_server, mp.browser_send)
        sent = []
        mp.BOOKS_ROOT, mp.OUT_ROOT, mp.STATE_PATH = books, tmp / "out", tmp / "state.json"
        mp.ensure_server = lambda: None
        mp.browser_send = lambda p: (sent.append(p), (200, {"ok": True, "found": True}))[1]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mp.main(["show", "demo", "ch1", "--passage", "Intro paragraph", "--open", "--expand", "sentence"])
            assert rc == 0, buf.getvalue()
            assert (tmp / "out" / "demo" / "01-ch1-intro.html").is_file()
            assert mp.load_state()["chapter"] == "01-ch1-intro.md"
            assert [p["action"] for p in sent] == ["open", "show"]
            assert sent[0]["url"].endswith("/out/demo/01-ch1-intro.html") and sent[0]["url"].startswith("http://localhost:")
            assert sent[1]["expand"] == "sentence" and sent[1]["text"] == "Intro paragraph"
            with contextlib.redirect_stdout(io.StringIO()):
                assert mp.main(["next"]) == 0
            assert mp.load_state()["chapter"] == "02-ch2-second.md"
            with contextlib.redirect_stdout(io.StringIO()):
                assert mp.main(["prev"]) == 0
                assert mp.main(["prev"]) == 0
            assert mp.load_state()["chapter"] == "00-front.md"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert mp.main(["prev"]) == 2
                assert mp.main(["show", "demo", "zzz"]) == 2
            assert "Already at the first chapter" in err.getvalue() and "not found" in err.getvalue()
        finally:
            mp.BOOKS_ROOT, mp.OUT_ROOT, mp.STATE_PATH, mp.ensure_server, mp.browser_send = saved


def test_real_book_renders():
    book = Path(os.environ.get("JARVIS_TEST_BOOK_DIR", "")) / "design-patterns"  # optional: a real converted book
    if not book.is_dir():
        return
    with tempfile.TemporaryDirectory() as d:
        md = book / "04-ch1-introduction.md"
        out = mp.render_chapter(book, md, Path(d))
        page = out.read_text(encoding="utf-8")
        assert "transcribed-from-image" not in page and "<table>" in page
        assert 'src="/books/design-patterns/images/' in page


MALICIOUS = """# Evil
<script>alert(1)</script>

<img src="x" onerror="alert(1)">

<iframe src="http://evil.example"></iframe>

[click](javascript:alert(1)) and [ws]( JaVa&#x0A;Script:alert(2)) and [d](data:text/html;base64,AAAA)
and [v](vbscript:x) and [ok](https://example.com/page)

![bad](javascript:alert(3)) ![good](images/ok.png)

<a href="javascript:alert(4)" onclick="x()">raw anchor</a>

Safe: H<sub>2</sub>O, x<sup>2</sup>, <kbd>Ctrl</kbd>, <span class="x" onclick="y()">span</span>, <br> end.

<div onclick="z()">div text</div>

<!-- hidden-marker -->
visible after
"""


def test_sanitised_rendering():
    page = mp.render_markdown(MALICIOUS, "demo")
    body = page.split("<main>")[1].split("</main>")[0]
    low = body.lower()
    assert "<script" not in low and "<iframe" not in low and "<div" not in low, body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body, body
    assert 'onerror' not in body.lower()  # the raw <img> keeps only its allowlisted attributes
    assert 'href="javascript' not in low and 'href="data:' not in low and 'href="vbscript' not in low
    assert 'href="#"' in body and 'href="https://example.com/page"' in body
    assert 'src="javascript' not in low and 'src="/books/demo/images/ok.png"' in body
    assert "hidden-marker" not in body and "visible after" in body
    for keep in ("<sub>2</sub>", "<sup>2</sup>", "<kbd>Ctrl</kbd>", "<span>span</span>", "<br>"):
        assert keep in body, (keep, body)
    # every real tag in the output is on the allowlist and carries only allowlisted attributes
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>", body):
        assert m.group(2).lower() in mp._ALLOWED_TAGS, m.group(0)
        assert not re.search(r"\bon\w+=", m.group(3)), m.group(0)
    assert "<meta" in page and "<script" not in page.split("<main>")[0].lower()  # the page itself has no script


def get_full(port, path, host=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"localhost:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, body, headers


class _Served:
    """Context manager: a real preview server on a free port over temp roots."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.books = make_books(tmp)
        self.ws = tmp / "ws"
        (self.ws / "pics").mkdir(parents=True)
        (self.ws / "pics" / "a.png").write_bytes(b"PNGDATA")
        (self.ws / "pics" / "evil.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
        (self.ws / "away-report.md").write_text("# Away\n\nreport body ![p](pics/a.png)", encoding="utf-8")
        (self.ws / "secret.txt").write_text("WSSECRET", encoding="utf-8")
        d = self.books / "demo"
        (d / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
        (d / "evil.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
        (d / "evil.htm").write_text("x", encoding="utf-8")
        (d / "evil.xhtml").write_text("x", encoding="utf-8")
        (d / "evil.js").write_text("alert(1)", encoding="utf-8")
        (d / "style.css").write_text("p{}", encoding="utf-8")
        (d / "notes.txt").write_text("note", encoding="utf-8")
        (d / "images" / "x.png").write_bytes(b"P")
        (d / "images" / "x.gif").write_bytes(b"G")
        (d / "images" / "x.webp").write_bytes(b"W")
        (d / "images" / "x.jpg").write_bytes(b"J")
        (d / "99-evil.md").write_text(MALICIOUS, encoding="utf-8")
        self.out = tmp / "out"
        self.port = free_port()

    def __enter__(self):
        folder = mp.resolve_book("demo", self.books, self.ws)
        mp.render_chapter(folder, folder / "99-evil.md", self.out)
        self.srv = mp.make_server(self.books, self.out, self.port, self.ws)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


def test_csp_and_static_allowlist():
    with tempfile.TemporaryDirectory() as d, _Served(Path(d)) as s:
        st, body, h = get_full(s.port, "/out/demo/99-evil.html")
        assert st == 200 and h["content-type"].startswith("text/html")
        assert h["content-security-policy"] == mp.CSP and "frame-ancestors 'none'" in h["content-security-policy"]
        assert "default-src 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
        assert b"<script" not in body.lower()
        st, body, h = get_full(s.port, "/out/demo/99-evil.html", method="HEAD")
        assert st == 200 and h["content-security-policy"] == mp.CSP
        for name, ctype in (("images/x.png", "image/png"), ("images/x.gif", "image/gif"), ("images/x.webp", "image/webp"),
                            ("images/x.jpg", "image/jpeg"), ("images/00002.jpeg", "image/jpeg"),
                            ("style.css", "text/css"), ("notes.txt", "text/plain")):
            st, body, h = get_full(s.port, "/books/demo/" + name)
            assert st == 200 and h["content-type"].startswith(ctype) and h["x-content-type-options"] == "nosniff", (name, st, h)
        for name in ("evil.html", "evil.htm", "evil.svg", "evil.xhtml", "evil.js"):
            st, body, h = get_full(s.port, "/books/demo/" + name)
            assert st == 403 and b"alert" not in body and b"<script" not in body, (name, st)
        st, _, _ = get_full(s.port, "/books/demo/99-evil.md")
        assert st == 404
        st, _, h = get_full(s.port, "/books/demo/evil.html")
        assert "content-security-policy" in h


def test_health_endpoint_and_identity():
    with tempfile.TemporaryDirectory() as d, _Served(Path(d)) as s:
        st, body, h = get_full(s.port, "/health")
        assert st == 200 and json.loads(body) == {"service": "jarvis-mdpreview"}
        assert h["content-type"] == "application/json"
        st, _, _ = get_full(s.port, "/health", host="evil.example.com")
        assert st == 403
        assert mp.is_preview_server(s.port) is True
        saved = mp.PORT
        mp.PORT = s.port
        try:
            mp.ensure_server()  # recognised: returns without spawning anything
        finally:
            mp.PORT = saved


def _foreign_listener(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = []

    def loop():
        srv.settimeout(0.2)
        while not stop:
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                c.settimeout(1)
                c.recv(4096)
                if reply:
                    c.sendall(reply)
            except OSError:
                pass
            finally:
                c.close()
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return srv, stop, t


def test_foreign_port_holder_is_refused():
    other = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n"
             b'{"service":"x"}\n')
    for reply in (b"", b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", other):
        srv, stop, t = _foreign_listener(reply)
        port = srv.getsockname()[1]
        saved = mp.PORT
        mp.PORT = port
        try:
            assert mp.is_preview_server(port) is False
            try:
                mp.ensure_server()
                raise AssertionError("expected PreviewError")
            except mp.PreviewError as e:
                assert "not the Jarvis preview server" in str(e) and str(port) in str(e)
            err = io.StringIO()
            saved_state = mp.STATE_PATH, mp.OUT_ROOT, mp.BOOKS_ROOT
            with tempfile.TemporaryDirectory() as d:
                books = make_books(Path(d))
                mp.STATE_PATH, mp.OUT_ROOT, mp.BOOKS_ROOT = Path(d) / "state.json", Path(d) / "out", books
                try:
                    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                        rc = mp.main(["show", "demo", "ch1"])
                finally:
                    mp.STATE_PATH, mp.OUT_ROOT, mp.BOOKS_ROOT = saved_state
            assert rc != 0 and "not the Jarvis preview server" in err.getvalue()
        finally:
            mp.PORT = saved
            stop.append(1)
            t.join(2)
            srv.close()


def make_workspace(tmp: Path) -> Path:
    ws = tmp / "ws"
    (ws / "pics").mkdir(parents=True)
    (ws / "sub").mkdir()
    (ws / "pics" / "a.png").write_bytes(b"PNGDATA")
    (ws / "away-report.md").write_text("# Away\n\nreport body ![p](pics/a.png)", encoding="utf-8")
    (ws / "HANDOFF.md").write_text("# Handoff", encoding="utf-8")
    (ws / "README.md").write_text("# Readme", encoding="utf-8")
    (ws / "notes.txt").write_text("n", encoding="utf-8")
    (ws / "sub" / "deep.md").write_text("# Deep\n\n![q](../pics/a.png)", encoding="utf-8")
    (ws / "sub" / "deep2.md").write_text("# Deep2", encoding="utf-8")
    return ws


def test_workspace_chapters_and_render():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ws = make_workspace(tmp)
        books = make_books(tmp)
        folder = mp.resolve_book("workspace", books, ws)
        assert folder == ws.resolve()
        saved = mp.WORKSPACE_ROOT, mp.BOOKS_ROOT
        mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = ws, books
        try:
            names = [p.name for p in mp.list_chapters(folder)]
            assert names == ["away-report.md", "HANDOFF.md", "README.md"], names  # name order, README kept
            assert mp.resolve_chapter(folder, "away-report").name == "away-report.md"
            assert mp.resolve_chapter(folder, "away-report.md").name == "away-report.md"
            assert mp.neighbour(folder, "away-report.md", 1).name == "HANDOFF.md"
            out = mp.render_chapter(folder, folder / "away-report.md", tmp / "out")
            assert out == tmp / "out" / "workspace" / "away-report.html"
            assert 'src="/books/workspace/pics/a.png"' in out.read_text(encoding="utf-8")
            f2, md = mp.locate_show("workspace", "sub/deep")
            assert md == (ws / "sub" / "deep.md").resolve() and f2 == md.parent
            out = mp.render_chapter(f2, md, tmp / "out")
            assert out == tmp / "out" / "workspace" / "sub" / "deep.html"
            assert 'src="/books/workspace/pics/a.png"' in out.read_text(encoding="utf-8")  # ../pics from sub/
            f3, md3 = mp.locate_show("anything", str(ws / "away-report.md"))
            assert md3 == (ws / "away-report.md").resolve()
            f4, md4 = mp.locate_show("anything", str(books / "demo" / "01-ch1-intro.md"))
            assert f4 == (books / "demo").resolve() and md4.name == "01-ch1-intro.md"
            for bad in (str(tmp / "secret.txt"), str(ws / "notes.txt"), str(Path(d).parent / "x.md"),
                        "sub/../../secret", "../secret.md", "sub/nothere"):
                try:
                    mp.locate_show("workspace", bad)
                    raise AssertionError(f"expected PreviewError for {bad}")
                except mp.PreviewError:
                    pass
        finally:
            mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = saved
        # books behaviour unchanged: README skipped, folder name used
        assert [p.name for p in mp.list_chapters(mp.resolve_book("demo", books, ws))] == \
            ["00-front.md", "01-ch1-intro.md", "02-ch2-second.md"]


def test_workspace_server_images_only():
    with tempfile.TemporaryDirectory() as d, _Served(Path(d)) as s:
        st, body, h = get_full(s.port, "/books/workspace/pics/a.png")
        assert st == 200 and body == b"PNGDATA" and h["content-type"] == "image/png"
        st, body, _ = get_full(s.port, "/books/workspace/pics/evil.svg")
        assert st == 403 and b"onload" not in body
        for p in ("/books/workspace/away-report.md", "/books/workspace/secret.txt", "/books/workspace/../secret.txt",
                  "/books/workspace/%2e%2e/x.png", "/books/workspace/pics/../../x.png", "/books/workspace/",
                  "/books/workspace/pics\\a.png"):
            st, body, _ = get_full(s.port, p)
            assert st in (403, 404) and b"WSSECRET" not in body and b"PNGDATA" not in body, (p, st)
        st, _, _ = get_full(s.port, "/books/demo/images/00002.jpeg")
        assert st == 200  # books route unchanged


def test_cmd_show_workspace():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        ws = make_workspace(tmp)
        books = make_books(tmp)
        saved = (mp.BOOKS_ROOT, mp.WORKSPACE_ROOT, mp.OUT_ROOT, mp.STATE_PATH, mp.ensure_server, mp.browser_send)
        sent = []
        mp.BOOKS_ROOT, mp.WORKSPACE_ROOT, mp.OUT_ROOT, mp.STATE_PATH = books, ws, tmp / "out", tmp / "state.json"
        mp.ensure_server = lambda: None
        mp.browser_send = lambda p: (sent.append(p), (200, {"ok": True, "found": True}))[1]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mp.main(["show", "workspace", "away-report.md", "--open", "--passage", "report body"])
            assert rc == 0, buf.getvalue()
            assert sent[0]["url"].endswith("/out/workspace/away-report.html"), sent
            assert sent[1]["text"] == "report body"
            assert (tmp / "out" / "workspace" / "away-report.html").is_file()
            assert mp.load_state() == {"book": "workspace", "chapter": "away-report.md"}
            with contextlib.redirect_stdout(io.StringIO()):
                assert mp.main(["next"]) == 0
            assert mp.load_state()["chapter"] == "HANDOFF.md"
            with contextlib.redirect_stdout(io.StringIO()):
                assert mp.main(["prev", "workspace"]) == 0
                assert mp.main(["show", "workspace", "sub/deep"]) == 0
                assert mp.load_state()["chapter"] == "sub/deep.md"
                assert mp.main(["next"]) == 0
            assert mp.load_state()["chapter"] == "sub/deep2.md"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert mp.main(["next"]) == 2
                assert mp.main(["show", "workspace", "../x"]) == 2
            assert "Already at the last chapter" in err.getvalue()
            # a book's state does not leak into the workspace and vice versa
            with contextlib.redirect_stdout(io.StringIO()):
                assert mp.main(["show", "demo", "ch1"]) == 0
            assert mp.load_state()["book"] == "demo"
            with contextlib.redirect_stderr(io.StringIO()):
                assert mp.main(["next", "workspace"]) == 2
        finally:
            mp.BOOKS_ROOT, mp.WORKSPACE_ROOT, mp.OUT_ROOT, mp.STATE_PATH, mp.ensure_server, mp.browser_send = saved


def test_workspace_env_override():
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        env = dict(__import__("os").environ, JARVIS_WORKSPACE_ROOT=d, JARVIS_NOTIFY_PHONE="0", JARVIS_TOKEN_LEDGER="0")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); from jarvis import mdpreview as m; "
                            "print(m.WORKSPACE_ROOT)" % ROOT], env=env, capture_output=True, text=True, timeout=30)
        assert r.stdout.strip() == d, (r.stdout, r.stderr)


def main() -> int:
    tests =[v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
