# Jarvis: instructions for Claude Code

Jarvis is a voice interface for Claude Code (Python 3.12, managed with uv; built and tested on Windows 11).

## If the user asks you to set this up

If the user says anything like "set this up", "install this", "help me get Jarvis running", or pastes this repo's URL and
asks for setup: **read SETUP.md in this folder and follow it.** Treat it as a guide, not as law: it was written on
Windows and the macOS/Linux parts are untested. Ask which operating system they use FIRST, and ask before every install
or account step. Never use anyone else's tokens, topics or hostnames.

## Working on the code

- Tests are standalone scripts, run from the repo root: `.venv\Scripts\python.exe tests\test_hangs.py`
  (macOS/Linux: `.venv/bin/python tests/test_hangs.py`). Each prints `N/N passed` and exits non-zero on failure. They are
  offline and silent (no microphone, no network).
- `jarvis/permissions.py` is the auto-approve policy and is protected: do not edit it unless the user explicitly asks.
- Never commit secrets: `~/.jarvis_web_token`, `~/.jarvis_ntfy_topic`, `~/.jarvis_browser_token`,
  `~/.jarvis_showcode_token`, `.jarvis_*` state files, `voices/`, `logs/`. They are git-ignored; keep it that way.
  Do not write personal paths, hostnames or tailnet addresses into code, tests or docs; use placeholders.
- Config lives in `config.yaml`; relative folders in it are relative to this repo.
- Windows-only modules are listed in SETUP.md section 1; keep new code degrading gracefully (check `sys.platform`).
- Match the existing style: small modules, docstrings that explain why, tests next to the behaviour they guard.
