"""Helper/CLI pass-through tests for show endText/expand. Run: python -m unittest discover tests"""
import json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from pathlib import Path
from unittest import mock

tmp = tempfile.mkdtemp()
PORT = 47990
os.environ.update(JARVIS_BROWSER_PORT=str(PORT), JARVIS_BROWSER_TOKEN=str(Path(tmp) / "tok"),
                  JARVIS_BROWSER_TIMEOUT="3", JARVIS_BROWSER_POLL_WAIT="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "helper"))
import jarvis_browser_helper as H  # noqa: E402
import jarvis_browser_cli as C  # noqa: E402


def req(method, path, body=None, token=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                               data=None if body is None else json.dumps(body).encode(),
                               headers={"X-Jarvis-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = H.ensure_token()
        cls.srv = H.Server(("127.0.0.1", PORT), H.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def roundtrip(self, cmd):
        out = {}
        t = threading.Thread(target=lambda: out.update(r=req("POST", "/cmd", cmd, self.tok)))
        t.start()
        code, job = req("GET", "/poll", token=self.tok)
        self.assertEqual(code, 200)
        req("POST", "/result", {"id": job["id"], "result": {"ok": True, "found": True, "matchCount": 1}}, self.tok)
        t.join()
        return job["command"], out["r"]

    def test_helper_passes_new_fields(self):
        cmd, (code, res) = self.roundtrip({"action": "show", "text": "a", "endText": "b", "expand": "sentence"})
        self.assertEqual(cmd["endText"], "b"); self.assertEqual(cmd["expand"], "sentence")
        self.assertEqual(code, 200); self.assertTrue(res["found"])

    def test_helper_rejects_bad_expand(self):
        code, _ = req("POST", "/cmd", {"action": "show", "text": "a", "expand": "page"}, self.tok)
        self.assertEqual(code, 400)
        code, _ = req("POST", "/cmd", {"action": "show", "text": "a", "endText": 5}, self.tok)
        self.assertEqual(code, 400)

    def run_cli(self, argv):
        sent = {}
        def fake(payload, timeout=15.0):
            sent.update(payload); return 200, {"ok": True}
        with mock.patch.object(sys, "argv", ["cli"] + argv), mock.patch.object(C, "send", fake), \
                mock.patch("builtins.print"):
            C.main()
        return sent

    def test_cli_flags(self):
        p = self.run_cli(["show", "x", "--to", "y", "--expand", "paragraph", "--caption", "c"])
        self.assertEqual(p, {"action": "show", "text": "x", "caption": "c", "endText": "y", "expand": "paragraph"})

    def test_cli_unchanged_without_flags(self):
        self.assertEqual(self.run_cli(["show", "x"]), {"action": "show", "text": "x"})

    def test_cli_occurrence_and_debug(self):
        p = self.run_cli(["show", "x", "--occurrence", "3", "--debug"])
        self.assertEqual(p, {"action": "show", "text": "x", "occurrence": 3, "debug": True})
        self.assertEqual(self.run_cli(["show", "x", "--occurrence", "last"])["occurrence"], "last")

    def test_cli_rejects_bad_occurrence(self):
        for bad in ("0", "-2", "abc"):
            with mock.patch.object(sys, "argv", ["cli", "show", "x", "--occurrence", bad]), \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                C.main()

    def test_helper_passes_occurrence_and_debug(self):
        cmd, (code, res) = self.roundtrip({"action": "show", "text": "a", "occurrence": "last", "debug": True})
        self.assertEqual(cmd["occurrence"], "last"); self.assertIs(cmd["debug"], True)
        self.assertEqual(code, 200)
        cmd, _ = self.roundtrip({"action": "show", "text": "a", "occurrence": 2})
        self.assertEqual(cmd["occurrence"], 2)

    def test_helper_rejects_bad_occurrence_and_debug(self):
        for bad in (0, -1, 1.5, "first", True, [1]):
            code, _ = req("POST", "/cmd", {"action": "show", "text": "a", "occurrence": bad}, self.tok)
            self.assertEqual(code, 400, bad)
        code, _ = req("POST", "/cmd", {"action": "show", "text": "a", "debug": "yes"}, self.tok)
        self.assertEqual(code, 400)

    def test_cli_prints_non_ascii_under_cp1252(self):
        """Piped stdout on Windows defaults to cp1252; U+200A and emoji used to raise UnicodeEncodeError."""
        import subprocess
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import jarvis_browser_cli as C; "
                "C.send = lambda payload, timeout=15.0: (200, {'ok': True, 'text': 'a\\u200ab \\U0001F600 \\u2192'}); "
                "sys.argv = ['cli', 'clear']; sys.exit(C.main())")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONIOENCODING"] = "cp1252"  # force the failing encoding on any platform
        out = subprocess.run([sys.executable, "-c", code, str(Path(C.__file__).parent)],
                             capture_output=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        text = out.stdout.decode("utf-8")
        self.assertIn(" ", text)
        self.assertIn("\U0001F600", text)


if __name__ == "__main__":
    unittest.main()
