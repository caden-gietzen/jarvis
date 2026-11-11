r"""Offline tests for the contribution scanner (skills/contribute-skill/scripts/scan_contribution.py).

Run:
    .venv\Scripts\python.exe tests\test_contribute_skill.py      (macOS/Linux: .venv/bin/python tests/...)

Plain script, no pytest needed. Every "secret" below is obviously fake and is built at runtime from pieces, so neither
this repo's scans nor GitHub secret scanning see a real-looking value in the source.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "contribute-skill" / "scripts" / "scan_contribution.py"
sys.path.insert(0, str(SCRIPT.parent))
import scan_contribution as sc  # noqa: E402

# ---- fake values (assembled at runtime) -------------------------------------------------------------------------
FAKE = {
    "aws-key": "AK" + "IA" + "FAKEFAKEFAKEFAKE",
    "github-token": "gh" + "p_" + "x" * 36,
    "github-token2": "github" + "_pat_" + "y" * 30,
    "slack-token": "xo" + "xb-" + "1234567890-fakefakefake",
    "notion-key": "nt" + "n_" + "f" * 30,
    "anthropic-key": "sk" + "-ant-" + "fake" * 8,
    "openai-key": "sk" + "-" + "fake" * 8,
    "google-key": "AI" + "za" + "F" * 35,
    "jwt": "ey" + "J" + "a" * 12 + ".ey" + "J" + "b" * 12 + "." + "c" * 12,
    "bearer-token": "Bear" + "er " + "fake-bearer-" + "abcdefghij12",
    "secret-assignment": "pass" + "word = '" + "hunter2" * 2 + "'",
    "webhook-url": "https://hooks." + "slack.com/ser" + "vices/T000/B000/" + "f" * 20,
    "email": "ali" + "ce@" + "corp-mail" + ".net",
    "home-path-win": "C:" + "\\" + "Us" + "ers" + "\\" + "alice" + "\\" + "docs",
    "home-path-mac": "/Us" + "ers/" + "alice" + "/docs",
    "home-path-linux": "/ho" + "me/" + "alice" + "/docs",
    "private-ip": ".".join(["192", "168", "1", "23"]),
    "tailnet-ip": ".".join(["100", "101", "102", "103"]),
    "tailnet-host": "my-pc.tail" + "abc12.ts" + ".net",
    "local-host": "alices-macbook." + "local",
    "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "high-entropy": "aZ3" + "kQ9xT" + "7mP2vL8" + "wR5nB1y" + "C6dF4",
}
FAKE_VALUE_OF = {  # the part that must never be echoed back in full
    "aws-key": FAKE["aws-key"], "github-token": FAKE["github-token"], "github-token2": FAKE["github-token2"],
    "slack-token": FAKE["slack-token"], "notion-key": FAKE["notion-key"], "anthropic-key": FAKE["anthropic-key"],
    "openai-key": FAKE["openai-key"], "google-key": FAKE["google-key"], "jwt": FAKE["jwt"],
    "bearer-token": "fake-bearer-" + "abcdefghij12", "secret-assignment": "hunter2" * 2, "webhook-url": "T000/B000/" + "f" * 20,
    "high-entropy": FAKE["high-entropy"],
}
LINE_FOR = {
    "secret-assignment": FAKE["secret-assignment"],
    "bearer-token": "Authorization: " + FAKE["bearer-token"],
}


def line_for(key):
    return LINE_FOR.get(key, "value = " + FAKE[key])


EXAMPLE_MAIL = "you" + "@example" + ".com"
TMPS = []


def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="jarvis-contrib-"))
    TMPS.append(d)
    return d


def run(args, cwd=None, env=None):
    """Run main() in-process; return (exit_code, stdout)."""
    buf = io.StringIO()
    old_env = dict(os.environ)
    if env:
        os.environ.update(env)
    old_cwd = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = sc.main([str(a) for a in args])
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)
    return code, buf.getvalue()


def make(files):
    d = tmpdir()
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def rules_found(d, *extra):
    code, out = run([d, "--json", *extra])
    data = json.loads(out)
    return code, data, {(f["rule"]) for f in data["findings"] if not f["allowed"]}


# ---- tests ------------------------------------------------------------------------------------------------------
def test_clean_passes():
    d = make({
        "SKILL.md": "---\nname: demo\ndescription: A demo.\n---\nUse ~/notes or a relative path. Mail " + EXAMPLE_MAIL + ".\n",
        "scripts/run.py": "import os\nTOKEN = os.environ.get('MY_TOKEN')\nprint('hello')\n",
    })
    code, out = run([d])
    assert code == 0, out
    assert "PASS" in out and "0 blocking" in out, out


def test_each_secret_rule_caught():
    expectations = {
        "aws-key": "aws-key", "github-token": "github-token", "github-token2": "github-token",
        "slack-token": "slack-token", "notion-key": "notion-key", "anthropic-key": "anthropic-key",
        "openai-key": "openai-key", "google-key": "google-key", "jwt": "jwt", "bearer-token": "bearer-token",
        "secret-assignment": "secret-assignment", "webhook-url": "webhook-url", "email": "email",
        "home-path-win": "home-path", "home-path-mac": "home-path", "home-path-linux": "home-path",
        "private-ip": "private-ip", "tailnet-ip": "private-ip", "tailnet-host": "private-host",
        "local-host": "private-host", "private-key": "private-key",
    }
    for key, rule in expectations.items():
        d = make({"a.txt": "first line\n" + line_for(key) + "\n"})
        code, data, found = rules_found(d)
        assert code == 1, f"{key}: exit {code}"
        assert rule in found, f"{key}: expected {rule}, got {found}"
        hit = [f for f in data["findings"] if f["rule"] == rule][0]
        assert hit["file"] == "a.txt" and hit["line"] == 2, f"{key}: wrong location {hit}"


def test_high_entropy_is_warning_unless_strict():
    d = make({"a.txt": "blob = " + FAKE["high-entropy"] + "\n"})
    code, data, found = rules_found(d)
    assert code == 0 and "high-entropy" in found, (code, found)
    code2, _out = run([d, "--strict"])
    assert code2 == 1


def test_example_and_placeholder_values_not_flagged():
    d = make({"a.md": "\n".join([
        "mail " + EXAMPLE_MAIL + " or " + "no" + "reply@" + "anthropic.com",
        "path C:" + "\\" + "Us" + "ers" + "\\" + "<name>" + "\\x and /Us" + "ers/you/x and /ho" + "me/user/x",
        "api_key = os.environ['KEY']", "token = '<your-token>'", "password = '${PASSWORD}'",
        "server 127.0.0.1 and 8.8.8.8", "version 10.0.19045",
    ]) + "\n"})
    code, out = run([d])
    assert code == 0, out


def test_forbidden_files():
    d = make({
        ".env": "A=1\n", "id_rsa": "x\n", "key.pem": "x\n", "my.key": "x\n", "app.log": "x\n", "logs/a.txt": "x\n",
        "credentials.json": "{}\n", "notes-token.txt": "x\n", ".DS_Store": "x\n",
    })
    code, data, found = rules_found(d)
    assert code == 1 and found == {"forbidden-file"}, found
    files = {f["file"] for f in data["findings"]}
    for name in (".env", "id_rsa", "key.pem", "my.key", "app.log", "logs/a.txt", "credentials.json", "notes-token.txt", ".DS_Store"):
        assert name in files, (name, files)


def test_large_and_binary_files():
    d = tmpdir()
    (d / "big.txt").write_bytes(b"a" * (1_000_001))
    (d / "blob.dat").write_bytes(b"ab\x00\x01\x02cd")
    (d / "pic.png").write_bytes(b"\x89PNG")
    code, data, found = rules_found(d)
    assert code == 1 and {"large-file", "binary-file"} <= found, found
    assert sum(1 for f in data["findings"] if f["rule"] == "binary-file") == 2


def test_utf16_text_is_scanned_not_binary():
    d = tmpdir()
    (d / "a.ps1").write_bytes(("x = " + FAKE["github-token"] + "\n").encode("utf-16"))
    code, data, found = rules_found(d)
    assert code == 1 and "github-token" in found and "binary-file" not in found, found


def test_redaction_never_prints_full_secret():
    for key, value in FAKE_VALUE_OF.items():
        d = make({"a.txt": line_for(key) + "\n"})
        code, out = run([d])
        assert code in (0, 1)
        assert value not in out, f"{key}: full value leaked in text output"
        code, out = run([d, "--json"])
        assert value not in out, f"{key}: full value leaked in json output"
    # two secrets on one line: both redacted
    d = make({"a.txt": FAKE["github-token"] + " and " + FAKE["aws-key"] + "\n"})
    _, out = run([d])
    assert FAKE["github-token"] not in out and FAKE["aws-key"] not in out
    assert "REDACTED" in out


def test_allowlist():
    d = make({"a.txt": "mail " + FAKE["email"] + "\n", "b.txt": "mail " + FAKE["email"] + "\n"})
    (d / ".contribution-allow").write_text("email a.txt:1   # reviewed: maintainer address\n", encoding="utf-8")
    # not honoured without --allow
    code, _ = run([d])
    assert code == 1
    code, out = run([d, "--allow"])
    assert code == 1, "b.txt is still blocking"
    assert "b.txt:1" in out and "ALLOWED" in out
    (d / ".contribution-allow").write_text("email *.txt   # all reviewed\n", encoding="utf-8")
    code, data, found = rules_found(d, "--allow")
    assert code == 0 and data["allowed"] == 2, data
    assert "allowlist-file" not in found or code == 0  # the file itself is only a warning
    # wrong line number does not allow
    (d / ".contribution-allow").write_text("email a.txt:5\nemail b.txt:1\n", encoding="utf-8")
    code, data, _ = rules_found(d, "--allow")
    assert code == 1 and data["allowed"] == 1, data
    # explicit allow file outside the folder
    other = tmpdir() / "allow.txt"
    other.write_text("* *.txt\n", encoding="utf-8")
    code, _ = run([d, "--allow-file", other])
    assert code == 0


def test_private_key_can_never_be_allowed():
    d = make({"k.txt": "x\n" + FAKE["private-key"] + "\n", ".contribution-allow": "* *\nprivate-key k.txt:2\nprivate-key *\n"})
    code, data, found = rules_found(d, "--allow")
    assert code == 1 and "private-key" in found, found
    assert not [f for f in data["findings"] if f["rule"] == "private-key" and f["allowed"]]


def test_also_flag_file_and_env():
    d = make({"a.txt": "hello my-very-own-pin-4821 bye\n", "b.txt": "clean\n"})
    lst = tmpdir() / "mine.txt"
    lst.write_text("# my private strings\nmy-very-own-pin-4821\nab\n", encoding="utf-8")
    code, out = run([d])
    assert code == 0
    code, out = run([d, "--also-flag-file", lst])
    assert code == 1 and "user-flag" in out and "a.txt:1" in out, out
    assert "my-very-own-pin-4821" not in out, "flagged literal must not be echoed"
    assert "Ignored 1 entries shorter" in out
    code, out = run([d, "--also-flag-file", lst, "--json"])
    assert "my-very-own-pin-4821" not in out
    # env var, case-insensitive match
    code, out = run([d], env={"CONTRIB_ALSO_FLAG": "MY-VERY-OWN-PIN-4821"})
    assert code == 1 and "user-flag" in out
    code, out = run([d, "--also-flag-env", "MY_LIST"], env={"MY_LIST": "my-very-own-pin-4821"})
    assert code == 1
    # cannot be allowed
    (d / ".contribution-allow").write_text("* *\n", encoding="utf-8")
    code, out = run([d, "--also-flag-file", lst, "--allow"])
    assert code == 1
    # file name match
    d2 = make({"my-very-own-pin-4821.txt": "x\n"})
    code, out = run([d2, "--also-flag-file", lst])
    assert code == 1 and "user-flag" in out


def test_exit_codes_and_usage_errors():
    d = make({"a.txt": "ok\n"})
    assert run([d])[0] == 0
    assert run([d, "--also-flag-file", d / "nope.txt"])[0] == 2
    code, out = run([str(d / "missing-thing")], cwd=d)
    assert code == 2, (code, out)
    code, out = run(["--json", d])
    assert code == 0 and json.loads(out)["blocking"] == 0
    # a single file target
    f = make({"x.txt": line_for("github-token") + "\n"}) / "x.txt"
    assert run([f])[0] == 1


def test_script_runs_as_subprocess():
    d = make({"a.txt": line_for("aws-key") + "\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 1 and FAKE["aws-key"] not in r.stdout and "SCAN RESULT: FAIL" in r.stdout, r.stdout
    d = make({"a.txt": "fine\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert r.returncode == 0


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t" + "@example" + ".com", *a],
                          capture_output=True, text=True)


def test_git_diff_mode():
    if shutil.which("git") is None:
        print("  (git not installed, skipped)")
        return
    repo = make({"README.md": "base\n", "old.txt": "old " + FAKE["github-token"] + "\n"})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # nothing changed: the old secret in the base commit is not part of the contribution
    code, out = run([base], cwd=repo)
    assert code == 0 and "0 files scanned" in out, out
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("ok\n" + line_for("aws-key") + "\n", encoding="utf-8")
    (repo / "elsewhere.txt").write_text("fine\n", encoding="utf-8")
    code, out = run([base, "--expect", "skills/demo/"], cwd=repo)
    assert code == 1 and "skills/demo/SKILL.md:2" in out and "[aws-key]" in out, out
    assert "outside-scope" in out and "elsewhere.txt" in out and "old.txt" not in out, out
    assert FAKE["aws-key"] not in out


def test_repo_skills_folder_is_clean():
    code, out = run([ROOT / "skills"])
    assert code == 0, out


TESTS = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main():
    t0 = time.monotonic()
    failed = 0
    for name, fn in TESTS:
        t = time.monotonic()
        try:
            fn()
            print(f"PASS {name} ({time.monotonic() - t:.1f}s)")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    for d in TMPS:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed in {time.monotonic() - t0:.1f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
