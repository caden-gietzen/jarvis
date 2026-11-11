r"""Offline tests for the Jarvis `browser` tool (jarvis/browser.py) and PNG downscaling (jarvis/imgshrink.py).

Run: <jarvis repo>\.venv\Scripts\python.exe -m unittest discover <jarvis repo>\chrome-ext\tests
Uses the real helper on a private port plus a fake "extension" thread; nothing touches Chrome.
"""
import asyncio, base64, json, os, site, struct, sys, tempfile, threading, unittest, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
site.addsitedir(str(ROOT / ".venv" / "Lib" / "site-packages"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "chrome-ext" / "helper"))

tmp = tempfile.mkdtemp()
PORT = 47990  # same as test_passthrough: whichever module imports the helper first fixes its port
os.environ.update(JARVIS_BROWSER_PORT=str(PORT), JARVIS_BROWSER_TOKEN=str(Path(tmp) / "tok"),
                  JARVIS_BROWSER_TIMEOUT="3", JARVIS_BROWSER_POLL_WAIT="1")
import jarvis_browser_helper as H  # noqa: E402
from jarvis import browser as B  # noqa: E402
from jarvis import imgshrink as I  # noqa: E402
import numpy as np  # noqa: E402


def text_of(res):
    return res["content"][0]["text"]


class FakeExtension(threading.Thread):
    """Polls the helper like the Chrome extension and answers with replies[action]."""
    def __init__(self, replies):
        super().__init__(daemon=True)
        self.replies, self.seen, self.stop = replies, [], False

    def run(self):
        import urllib.request
        while not self.stop:
            job = H.BROKER.next_job(0.2)
            if job is None:
                continue
            self.seen.append(job.command)
            H.BROKER.deliver(job.id, self.replies.get(job.command["action"], {"ok": True}))


class HelperCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        H.ensure_token()
        cls.srv = H.Server(("127.0.0.1", PORT), H.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        B.PORT, B.TOKEN_PATH = PORT, H.TOKEN_PATH

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def run_with(self, replies, args, fn=B.browser_sync):
        ext = FakeExtension(replies); ext.start()
        try:
            return fn(args), ext.seen
        finally:
            ext.stop = True; ext.join(2)


class BrowserToolTests(HelperCase):
    # ---- (a) the tool ----
    def test_open_is_always_new_tab(self):
        res, seen = self.run_with({"open": {"ok": True, "tabId": 5}}, {"action": "open", "url": "https://arxiv.org/abs/1706.03762", "newTab": False, "same_tab": True})
        self.assertFalse(res.get("is_error"))
        self.assertEqual(seen, [{"action": "open", "url": "https://arxiv.org/abs/1706.03762", "newTab": True}])

    def test_open_rejects_non_http(self):
        for bad in ("javascript:alert(1)", "file:///C:/x", "arxiv.org", "", None):
            res = B.browser_sync({"action": "open", "url": bad})
            self.assertTrue(res.get("is_error"), bad)

    def test_show_maps_fields(self):
        res, seen = self.run_with({"show": {"ok": True, "found": True, "matchCount": 2, "occurrence": 2}},
                                  {"action": "show", "text": "abc", "end_text": "xyz", "expand": "sentence", "occurrence": 2, "caption": "hi"})
        self.assertEqual(seen[0], {"action": "show", "text": "abc", "endText": "xyz", "expand": "sentence", "occurrence": 2, "caption": "hi"})
        self.assertIn('"found": true', text_of(res))

    def test_validation_errors(self):
        for a in ({"action": "show"}, {"action": "show", "text": "a", "expand": "word"}, {"action": "show", "text": "a", "occurrence": 0},
                  {"action": "read", "max_chars": -1}, {"action": "nope"}, {}):
            self.assertTrue(B.browser_sync(a).get("is_error"), a)

    def test_read_formats_text_and_caps_max_chars(self):
        reply = {"ok": True, "title": "Paper", "url": "https://arxiv.org/x", "text": "Body text", "truncated": True, "totalChars": 99999}
        res, seen = self.run_with({"read": reply}, {"action": "read", "max_chars": 10**9})
        self.assertEqual(seen[0]["maxChars"], B.MAX_READ_CHARS)
        t = text_of(res)
        self.assertIn("Title: Paper", t); self.assertIn("Body text", t); self.assertIn("truncated", t)
        _, seen = self.run_with({}, {"action": "read"})
        self.assertEqual(seen[0]["maxChars"], B.DEFAULT_READ_CHARS)

    def test_site_not_allowed_is_error_with_hint(self):
        res, _ = self.run_with({"show": {"ok": False, "error": "site_not_allowed", "host": "evil.test"}}, {"action": "show", "text": "x"})
        self.assertTrue(res.get("is_error")); self.assertIn("browser_allow_site", text_of(res))

    def test_allow_site(self):
        res, seen = self.run_with({"allow": {"ok": True}}, {"host": "Example.ORG"}, fn=B.allow_site_sync)
        self.assertFalse(res.get("is_error")); self.assertEqual(seen[0], {"action": "allow", "host": "example.org"})
        for bad in ("http://x.com", "x.com/path", "", "a b"):
            self.assertTrue(B.allow_site_sync({"host": bad}).get("is_error"), bad)

    def test_extension_not_connected_gives_error(self):
        res = B.browser_sync({"action": "clear"})  # nobody polling: helper answers 504
        self.assertTrue(res.get("is_error")); self.assertIn("extension", text_of(res).lower())

    def test_helper_down_gives_error(self):
        old = B.PORT
        B.PORT = 47999
        try:
            res = B.browser_sync({"action": "clear"})
        finally:
            B.PORT = old
        self.assertTrue(res.get("is_error")); self.assertIn("helper", text_of(res))

    def test_registration(self):
        self.assertEqual(B.TOOL_ID, "mcp__jarvis_browser__browser")
        self.assertEqual(B.ALLOW_TOOL_ID, "mcp__jarvis_browser__browser_allow_site")
        srv = B.build_server()
        self.assertEqual(srv["name"], "jarvis_browser")
        self.assertEqual(B._SCHEMA["properties"]["action"]["enum"], ["open", "show", "read", "clear", "list_allowed"])
        self.assertNotIn("allow", B._SCHEMA["properties"]["action"]["enum"])  # allowing a site is a separate, asked tool
        brain_src = (ROOT / "jarvis" / "brain.py").read_text(encoding="utf-8")
        self.assertIn("BROWSER_TOOL]", brain_src)           # auto-approved
        self.assertNotIn("ALLOW_TOOL", brain_src)           # browser_allow_site is NOT auto-approved

    def test_server_handlers_run(self):
        # call the registered async handler path via the sync functions the tool wraps
        res = asyncio.run(asyncio.to_thread(B.browser_sync, {"action": "open", "url": "ftp://x"}))
        self.assertTrue(res.get("is_error"))

    # ---- (c) oversized screenshot never reaches the model ----
    def test_huge_screenshot_in_result_is_shrunk(self):
        big = I.encode_png(np.random.default_rng(1).integers(0, 256, (1600, 2400, 3), dtype=np.uint8))
        self.assertGreater(len(big), 5_000_000)
        # full path: fake extension replies with an oversized screenshot; the helper accepts it (<2 MB body limit is
        # why we also test format_result directly with a bigger one)
        res, _ = self.run_with({"list_allowed": {"ok": True, "screenshot": base64.b64encode(I.shrink_png(big, 1600, 1_400_000)).decode()}},
                               {"action": "list_allowed"})
        self.assertLess(len(json.dumps(res)), 700_000)
        out = B.format_result(200, {"ok": True, "screenshot": "data:image/png;base64," + base64.b64encode(big).decode()})
        self.assertLess(len(json.dumps(out)), 700_000)           # whole tool result is far below the 1 MB limit
        self.assertNotIn("base64", text_of(out))                  # text carries only a placeholder
        imgs = [c for c in out["content"] if c.get("type") == "image"]
        self.assertEqual(len(imgs), 1); self.assertEqual(imgs[0]["mimeType"], "image/png")
        png = base64.b64decode(imgs[0]["data"])
        w, h = I._png_dims(png)
        self.assertLessEqual(max(w, h), I.DEFAULT_MAX_DIM); self.assertLessEqual(len(png), I.DEFAULT_MAX_BYTES)

    def test_unshrinkable_image_is_dropped_not_passed(self):
        junk = base64.b64encode(b"\xff\xd8\xff" + os.urandom(3_000_000)).decode()  # a JPEG-ish blob we cannot decode
        t = text_of(B.format_result(200, {"ok": True, "image": junk}))
        self.assertIn("image omitted", t); self.assertLess(len(t), 1000)
        self.assertEqual(len(B.format_result(200, {"ok": True, "image": junk})["content"]), 1)

    def test_text_result_is_capped(self):
        t = text_of(B.format_result(200, {"ok": True, "title": "t", "url": "u", "text": "a" * 500_000}))
        self.assertLessEqual(len(t), B.MAX_RESULT_CHARS + 50)


def png_with_filters(img, filters):
    """Hand-encode an RGB(A) PNG using the given PNG filter type for each row."""
    h, w, ch = img.shape
    raw = bytearray()
    prev = np.zeros(w * ch, dtype=np.int64)
    for y in range(h):
        row = img[y].reshape(-1).astype(np.int64)
        ft = filters[y % len(filters)]
        out = np.zeros_like(row)
        for i in range(w * ch):
            a = row[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if ft == 0: pr = 0
            elif ft == 1: pr = a
            elif ft == 2: pr = b
            elif ft == 3: pr = (a + b) // 2
            else:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out[i] = (row[i] - pr) & 255
        raw += bytes([ft]) + bytes(out.astype(np.uint8))
        prev = row
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ctype = {3: 2, 4: 6}[ch]
    return I.SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


class ImgShrinkTests(unittest.TestCase):
    def test_decode_all_filter_types_roundtrip(self):
        rng = np.random.default_rng(7)
        for ch in (3, 4):
            img = rng.integers(0, 256, (12, 9, ch), dtype=np.uint8)
            w, h, c, dec = I.decode_png(png_with_filters(img, [0, 1, 2, 3, 4]))
            self.assertEqual((w, h, c), (9, 12, ch)); self.assertTrue((dec == img).all())

    def test_shrink_large_png(self):
        img = np.random.default_rng(3).integers(0, 256, (1200, 2000, 4), dtype=np.uint8)
        data = I.encode_png(img)
        out = I.shrink_png(data, max_dim=800, max_bytes=300_000)
        self.assertIsNotNone(out); self.assertLessEqual(len(out), 300_000)
        w, h = I._png_dims(out); self.assertLessEqual(max(w, h), 800)
        self.assertEqual(I.decode_png(out)[3].shape[2], 4)

    def test_small_png_unchanged_and_non_png_rejected(self):
        small = I.encode_png(np.zeros((10, 10, 3), dtype=np.uint8))
        self.assertEqual(I.shrink_png(small), small)
        self.assertIsNone(I.shrink_png(b"not a png" * 100000))

    def test_flat_big_screenshot_keeps_proportions(self):
        img = np.zeros((900, 1600, 3), dtype=np.uint8); img[:, :800] = 255
        out = I.shrink_png(I.encode_png(img), max_dim=400, max_bytes=10**6)
        w, h = I._png_dims(out); self.assertEqual((w, h), (400, 225))


if __name__ == "__main__":
    unittest.main()
