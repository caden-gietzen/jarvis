r"""Offline tests for LaTeX math support: the vendored KaTeX, jarvis.mathsupport, the Markdown preview and the web UI.

Run:  .venv\Scripts\python.exe tests\test_math.py   (from the repo root)

Plain script, no pytest. Everything is local (127.0.0.1); nothing is fetched from the internet. The last test drives a
real headless Chrome through tests/math_browser_check.mjs (needs node, puppeteer-core and Chrome; it is SKIPPED, and
says so, when any of them is missing). Set JARVIS_MATH_SHOTS=<folder> to also save screenshots there.
"""

import os as _os_ledger; _os_ledger.environ.setdefault('JARVIS_TOKEN_LEDGER', '0')  # tests must never write the real token ledger
import os as _os_np; _os_np.environ["JARVIS_NOTIFY_PHONE"] = "0"
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import site
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from jarvis import mathsupport as ms  # noqa: E402
from jarvis import mdpreview as mp  # noqa: E402
from jarvis import ui  # noqa: E402
from jarvis import web as web_mod  # noqa: E402
from jarvis.events import EventBus  # noqa: E402

TMP = Path(tempfile.gettempdir())
ui._log_path = TMP / "jarvis-test-math.log"
ui.console = Console(file=io.StringIO())
os.environ["JARVIS_WEB_TOKEN_FILE"] = str(TMP / "jarvis-test-math-token.txt")
TOKEN = "test-token-math-123456"

CONVEX = r"Minimize a convex $f(x)=\lVert Ax-b\rVert_2^2$ subject to $x\in C$ with $C$ convex:" "\n\n" \
         r"$$\min_{x\in C} f(x)\quad\text{s.t.}\quad g_i(x)\le 0,\; Ax=b$$"

XSS = [
    r"$\href{javascript:window.__pwn=1}{click}$",
    r"$\url{javascript:window.__pwn=1}$",
    r"$\htmlClass{evilclass}{x}$",
    r"$\htmlId{evilid}{x}$",
    r"$\htmlStyle{color:red}{x}$",
    r"$\htmlData{k=v}{x}$",
    r"$\includegraphics{http://evil.example/x.png}$",
    "$" + "{" * 200 + "x" + "}" * 200 + "$",                     # huge nesting
    r"$\def\a{\a\a}\a$",                                       # macro bomb (maxExpand)
    "$" + "x" * 5000 + "$",                                    # too long
    ]
CAPPED = r"$\rule{99999em}{99999em}$"                            # huge size: KaTeX caps it at maxSize (10em), no error


def check(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(port, path, host=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host or f"localhost:{port}")
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read(), dict((k.lower(), v) for k, v in r.getheaders())
    finally:
        c.close()


# ---------------------------------------------------------------- vendored files

def test_vendored_katex_is_real_and_recorded():
    v = ms.VENDOR_DIR / "katex"
    ver = (v / "VERSION.txt").read_text(encoding="utf-8")
    check("KaTeX 0.19.1" in ver and "MIT" in ver and "registry.npmjs.org" in ver)
    for name in ("katex.min.js", "katex.min.css", "LICENSE"):
        digest = hashlib.sha256((v / name).read_bytes()).hexdigest()
        check(digest in ver, f"{name} sha256 not recorded / file modified")
    check("The MIT License" in (v / "LICENSE").read_text(encoding="utf-8"))
    check(len(list((v / "fonts").glob("*.woff2"))) == 20)
    css = (v / "katex.min.css").read_text(encoding="utf-8")
    check("http://" not in css.replace("http://www.w3.org", "") and "https://" not in css, "CSS must not reference the internet")
    check(all(f"fonts/{m}" for m in re.findall(r"url\((fonts/[^)]+\.woff2)\)", css)))
    for m in set(re.findall(r"url\((fonts/[^)]+\.woff2)\)", css)):
        check((v / m).is_file(), f"font {m} missing")


def test_vendor_allowlist():
    for rel in ("katex/katex.min.js", "katex/katex.min.css", "katex/LICENSE", "jarvis-math.js",
                "katex/fonts/KaTeX_Main-Regular.woff2"):
        check(ms.vendor_file(rel) is not None, rel)
    for rel in ("", "katex", "katex/", "../mathsupport.py", "katex/../../web.py", "katex/fonts/../../../web.py",
                "katex/fonts/KaTeX_Main-Regular.ttf", "katex/fonts/KaTeX_Main-Regular.woff", "katex/fonts/x.woff2",
                "katex/contrib/auto-render.min.js", "katex\\katex.min.js", "katex/katex.min.js/", "katex/%2e%2e/x",
                "jarvis-math.js.map", "other.js"):
        check(ms.vendor_file(rel) is None, rel)


# ---------------------------------------------------------------- scanner (Python side)

def test_scanner_inline_display_and_text():
    t, nonce, items = ms.protect_math(r"a $x_1 + y_2$ b \(\alpha\) c $$\int_0^1 f$$ d \[\beta\] e")
    check([(i[1], i[2]) for i in items] == [("x_1 + y_2", False), (r"\alpha", False), (r"\int_0^1 f", True),
                                              (r"\beta", True)], items)
    check("$" not in t and "\\" not in t and t.count("JVMATH") == 4, t)


def test_scanner_currency_escapes_and_code():
    for text in ("costs $5 and $10 total", "a $ b $ c", "just one $ sign", r"escaped \$x\$ stays", "$$ $$", "$  $"):
        _, _, items = ms.protect_math(text)
        check(not items, (text, items))
    t, _, items = ms.protect_math(r"price \$5 then $x$")
    check(len(items) == 1 and "&#36;5" in t, t)
    t, _, items = ms.protect_math("code `$a_b$` and\n```\n$$x_1$$\n\\(y\\)\n```\nand\n~~~\n$z$\n~~~\nreal $r$")
    check([i[1] for i in items] == ["r"], items)
    check(ms.protect_math("$a\n\nb$")[2] == [], "inline math never spans a blank line")
    check(ms.protect_math("$$a\n\nb$$")[2][0][1] == "a\n\nb", "display math may")


def test_markdown_does_not_mangle_math_and_html_is_inert():
    page = mp.render_markdown("# T\n\nUse $a_1 * b_2 * c_3$ and $\\{x\\}$ here.\n\n$$\nM = \\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}\n$$\n")
    check('data-tex="a_1 * b_2 * c_3"' in page, page)
    check("\\begin{pmatrix} a &amp; b \\\\ c &amp; d \\end{pmatrix}" in page, page)  # attribute-escaped, backslashes intact
    check("<em>" not in page and "JVMATH" not in page)
    check('<link rel="stylesheet" href="/vendor/katex/katex.min.css">' in page)
    check('<script src="/vendor/katex/katex.min.js"></script><script src="/vendor/jarvis-math.js"></script>' in page)
    check(len(re.findall(r"<script", page)) == 2, "only the two vendored scripts")
    plain = mp.render_markdown("# No math\n\ncosts $5 or $10.")
    check("<script" not in plain and "/vendor/" not in plain, "math assets only on pages that have math")


def test_math_in_attributes_cannot_inject_markup():
    page = mp.render_markdown('[link $x$](http://a/$y$"onmouseover="alert(1)) and <b onclick="x">$z$</b> and `$c$`'
                              '\n\n<script>alert(1)</script>\n\n![img $i$](p.png "t $w$")')
    check("JVMATH" not in page)
    import html.parser

    class Attrs(html.parser.HTMLParser):
        bad = []

        def handle_starttag(self, tag, attrs):
            self.bad += [(tag, k) for k, _ in attrs if k.startswith("on") or k == "style"]

    p = Attrs()
    p.feed(page)
    check(not p.bad, p.bad)
    check(page.count("<script") == 2, page)  # the two vendored ones; raw <script> in the source is escaped
    check("&lt;script&gt;" in page)
    check('data-tex="c"' not in page, "inline code is not math")
    for m in re.finditer(r'<[^>]*>', page):
        check('class="jv-math' not in m.group(0) or m.group(0).startswith("<span"), m.group(0))


def test_xss_tex_is_only_ever_an_attribute_value():
    src = "\n\n".join(XSS[:9])
    page = mp.render_markdown(src)
    check(page.count('<span class="jv-math"') == 9, page.count("jv-math"))
    check("<a " not in page and "href=" not in page.split("<main>")[1].split("</main>")[0].replace('data-tex="', ""),
          "no anchor created server side")
    body = page.split("<main>")[1].split("</main>")[0]
    check(re.search(r"<(?!/?(p|span)\b)[a-z]", body) is None, "only p and span elements in the body")
    check('"' not in re.sub(r'(class|data-tex)="[^"]*"', "", re.sub(r"<(/?)(p|span)", "", body)).replace("&quot;", ""),
          "tex cannot break out of its attribute")


# ---------------------------------------------------------------- servers

def test_preview_serves_vendor_and_study_notes():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        ws, out, books = d / "ws", d / "out", d / "books"
        (ws / "study-notes").mkdir(parents=True)
        books.mkdir()
        (ws / "study-notes" / "alpha.md").write_text("# Alpha\n\nInline $x^2$.\n", encoding="utf-8")
        (ws / "study-notes" / "beta.md").write_text("# Beta\n\n$$y$$\n", encoding="utf-8")
        (ws / "secret.js").write_text("alert(1)", encoding="utf-8")
        folder = mp.resolve_book("study-notes", books, ws)
        check(folder == (ws / "study-notes").resolve())
        saved = mp.WORKSPACE_ROOT, mp.BOOKS_ROOT
        mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = ws, books
        try:
            f, md = mp.locate_show("study-notes", "alpha")
            check(md.name == "alpha.md")
            f, md = mp.locate_show("workspace", "study-notes/beta")
            check(md.name == "beta.md")
            check(mp.neighbour(f, "alpha.md", 1).name == "beta.md")
            check(mp.book_base(f)[0] == "workspace")
            try:
                mp.resolve_book("study-notes", books, d / "nowhere")
                check(False, "missing study-notes folder must raise")
            except mp.PreviewError:
                pass
            page_path = mp.render_chapter(folder, md, out)
        finally:
            mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = saved
        check(page_path.exists() and "jv-display" in page_path.read_text(encoding="utf-8"))
        port = free_port()
        srv = mp.make_server(books, out, port, ws)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            st, body, h = http_get(port, "/out/workspace/study-notes/beta.html")
            check(st == 200 and b"jv-math" in body, (st, body[:200]))
            csp = h["content-security-policy"]
            check("script-src 'self';" in csp and "style-src 'self' 'unsafe-inline'" in csp and "font-src 'self'" in csp, csp)
            check("unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp)
            for path, ctype in (("/vendor/katex/katex.min.js", "application/javascript"),
                                ("/vendor/katex/katex.min.css", "text/css"), ("/vendor/jarvis-math.js", "application/javascript"),
                                ("/vendor/katex/LICENSE", "text/plain"), ("/vendor/katex/fonts/KaTeX_Main-Regular.woff2", "font/woff2")):
                st, body, h = http_get(port, path)
                check(st == 200 and h["content-type"].startswith(ctype) and h["x-content-type-options"] == "nosniff"
                      and "content-security-policy" in h and len(body) > 100, (path, st, h.get("content-type")))
            for path in ("/vendor/", "/vendor/katex", "/vendor/katex/fonts/", "/vendor/../mdpreview.py",
                         "/vendor/katex/../../mdpreview.py", "/vendor/%2e%2e/mdpreview.py", "/vendor/katex/%2e%2e/%2e%2e/web.py",
                         "/vendor/katex/fonts/KaTeX_Main-Regular.ttf", "/vendor/jarvis-math.js.map"):
                st, body, _ = http_get(port, path)
                check(st in (403, 404) and b"def " not in body, (path, st))
            st, _, _ = http_get(port, "/vendor/katex/katex.min.js", host="evil.example.com")
            check(st == 403, "DNS-rebinding guard also covers /vendor")
            st, _, _ = http_get(port, "/books/workspace/secret.js")
            check(st in (403, 404), "workspace still serves images only")
        finally:
            srv.shutdown()
            srv.server_close()


class WebServer:
    def __init__(self):
        self.bus = EventBus()
        self.web = web_mod.WebServer(lambda t: None, lambda t: {"ok": True}, lambda: None, port=0, token=TOKEN,
                                     event_bus=self.bus, url_file=TMP / f"jarvis-test-math-url-{id(self)}.txt")
        self.url = self.web.start()
        check(self.url, "web server did not start")
        self.port = self.web.port

    def get(self, path, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", host or f"127.0.0.1:{self.port}")
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            c.endheaders()
            r = c.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            c.close()

    def close(self):
        self.web.stop()


def test_web_serves_vendor_with_narrow_csp():
    s = WebServer()
    try:
        st, h, body = s.get(f"/?token={TOKEN}")
        csp = h["content-security-policy"]
        check(st == 200 and re.search(r"script-src 'sha256-[A-Za-z0-9+/=]+' 'self'; ", csp), csp)
        check("style-src 'self' 'unsafe-inline'; font-src 'self'; " in csp and "default-src 'none'" in csp, csp)
        check("unsafe-eval" not in csp and not re.search(r"script-src[^;]*unsafe-inline", csp), csp)
        check("connect-src 'self'" in csp and "frame-ancestors 'none'" in csp and "img-src 'self'" in csp, csp)
        # the sign-in page (no math) keeps the strict policy
        st, h2, _ = s.get("/")
        check(st == 401 and "'self'" not in h2["content-security-policy"].split("connect-src")[0], h2["content-security-policy"])
        # vendor files: token-free, Host-checked, exact allowlist
        for path, ctype in (("/vendor/katex/katex.min.js", "application/javascript"), ("/vendor/jarvis-math.js", "application/javascript"),
                            ("/vendor/katex/katex.min.css", "text/css"), ("/vendor/katex/LICENSE", "text/plain"),
                            ("/vendor/katex/fonts/KaTeX_Math-Italic.woff2", "font/woff2")):
            st, h, body = s.get(path)
            check(st == 200 and h["content-type"].startswith(ctype) and h["x-content-type-options"] == "nosniff"
                  and len(body) > 100, (path, st))
        for path in ("/vendor/", "/vendor/katex", "/vendor/../web.py", "/vendor/katex/../../web.py", "/vendor/%2e%2e/web.py",
                     "/vendor/katex/fonts/KaTeX_Main-Regular.ttf", "/vendor/jarvis-math.js.map", "/vendor/katex/fonts/x.woff2"):
            st, h, body = s.get(path)
            check(st in (403, 404) and b"def " not in body, (path, st))
        check(s.get("/vendor/jarvis-math.js", host="evil.example.com")[0] == 403, "Host check")
        check(s.get("/vendor/jarvis-math.js", headers={"Origin": "http://evil.example.com"})[0] == 403, "Origin check")
    finally:
        s.close()


def test_page_message_rendering_is_not_innerhtml():
    html = web_mod.PAGE_PATH.read_text(encoding="utf-8")
    js = (ms.VENDOR_DIR / "jarvis-math.js").read_text(encoding="utf-8")
    for name, src in (("webui.html", html), ("jarvis-math.js", js)):
        check("innerHTML" not in src, f"{name} uses innerHTML")
        check("insertAdjacentHTML" not in src and "document.write" not in src and "eval(" not in src, name)
    check("trust: distrust" in js and "throw new Error" in js and "maxExpand" in js and "maxSize" in js and 'output: "html"' in js)
    check("JarvisMath.fill(body, text, q)" in html)
    check(html.index("/vendor/katex/katex.min.js") < html.index("/vendor/jarvis-math.js") < html.index("<script>\n"))


def test_conversation_math_sample_text_survives_the_bus():
    """The server never touches the text: the model's LaTeX reaches the page verbatim (rendering is client side)."""
    s = WebServer()
    try:
        s.bus.emit("conversation", role="jarvis", text=CONVEX)
        st, h, body = s.get(f"/state?token={TOKEN}")
        check(st == 200 and json.loads(body)["conversation"][-1]["text"] == CONVEX)
    finally:
        s.close()


# ---------------------------------------------------------------- real browser

def _first_existing(*cands):
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    return Path("missing")


# Optional overrides: JARVIS_TEST_NODE, JARVIS_TEST_CHROME, JARVIS_TEST_PUPPETEER_DIR (a folder that holds
# node_modules/puppeteer-core; not part of the repo). Without them the headless Chrome part is skipped.
NODE = _first_existing(os.environ.get("JARVIS_TEST_NODE"), shutil.which("node"), r"C:\Program Files\nodejs\node.exe")
CHROME = _first_existing(os.environ.get("JARVIS_TEST_CHROME"), shutil.which("google-chrome"), shutil.which("chrome"),
                         r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PPTR = Path(os.environ.get("JARVIS_TEST_PUPPETEER_DIR") or (ROOT.parent / "jarvis-workspace" / "scratch-ui"))


def test_headless_chrome_renders_and_blocks_xss():
    if not (NODE.exists() and CHROME.exists() and (PPTR / "node_modules" / "puppeteer-core").exists()):
        print("SKIP headless Chrome part (node, Chrome or puppeteer-core not found)")
        return
    shots = os.environ.get("JARVIS_MATH_SHOTS", "")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        ws, out, books = d / "ws", d / "out", d / "books"
        (ws / "study-notes").mkdir(parents=True)
        books.mkdir()
        text = "# Doc\n\n" + CONVEX + "\n\nLossless convexification of the constraint set:\n\n$$\\int_0^1 \\lVert u(t)\\rVert\\,dt \\le \\sigma$$"
        doc = text + "\n\n## Hostile math\n\n" + "\n\n".join(XSS + [CAPPED]) + "\n"
        (ws / "study-notes" / "doc.md").write_text(doc, encoding="utf-8")
        folder = mp.resolve_book("study-notes", books, ws)
        saved = mp.WORKSPACE_ROOT, mp.BOOKS_ROOT
        mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = ws, books
        try:
            mp.render_chapter(folder, folder / "doc.md", out)
        finally:
            mp.WORKSPACE_ROOT, mp.BOOKS_ROOT = saved
        port = free_port()
        srv = mp.make_server(books, out, port, ws)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        w = WebServer()
        try:
            w.bus.emit("conversation", role="you", text="Explain convexification.")
            w.bus.emit("conversation", role="jarvis", text=CONVEX)
            w.bus.emit("conversation", role="jarvis", text=r"Then \(\alpha+\beta\) inline and \[\sum_{i=1}^n i = \tfrac{n(n+1)}{2}\] display, "
                       "and a price of $5 or $10 stays text.")
            for x in XSS + [CAPPED]:
                w.bus.emit("conversation", role="jarvis", text="hostile: " + x)
            args = [str(NODE), str(ROOT / "tests" / "math_browser_check.mjs"), str(PPTR), str(CHROME),
                    f"http://localhost:{port}/out/workspace/study-notes/doc.html", f"{w.url}"]
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                args.append(shots)
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            check(r.returncode == 0, r.stderr[-2000:])
            res = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            srv.shutdown()
            srv.server_close()
            w.close()
    n_bad = len(XSS)
    for where in ("preview", "web"):
        x = res[where]
        check(x["katex"] >= 5, (where, "math should render", x["katex"]))
        check(not x["pwn"], (where, "script ran"))
        check(x["jsAnchors"] == 0 and x["anchorsInMath"] == 0, (where, "javascript: anchor"))
        check(x["injectedClass"] == 0, (where, "htmlClass was honoured"))
        check(x["scripts"] == 0 or where == "preview", (where, "script elements in content"))
        check(x["errors"] == n_bad, (where, "every hostile expression must be rejected and shown as plain text", x["errors"], x["errorTexts"]))
        check(x["csp"] == [], (where, "CSP violations", x["csp"]))
        check(x["external"] == [], (where, "requests leaving the machine", x["external"]))
        check(len(x["fontsLoaded"]) >= 1 and any("KaTeX" in f for f in x["fontsLoaded"]), (where, x["fontsLoaded"]))
        check(x["maxRulePx"] <= 400, (where, "size cap", x["maxRulePx"]))
        check(x["mathml"] == 0, (where, "output is HTML only, so the page text is not doubled"))
        check(x["roleMath"] >= 5, (where, "role=math with aria-label"))
    check(res["preview"]["displays"] >= 2 and res["web"]["displays"] >= 2)
    check(res["web"]["search"]["marks"] >= 1 and res["web"]["search"]["katex"] >= 1, res["web"]["search"])
    print("  highlighter view of one rendered inline equation:", ascii(res["preview"]["visibleTextOfFirstMath"]))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
