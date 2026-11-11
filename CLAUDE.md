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

## Sharing skills back (standing instruction)

If the user creates or notably improves a skill, agent or helper script (in `~/.claude/skills` or this repo's
`skills/`) that others might like, offer once per session, at a natural moment, to help propose it upstream as a pull
request; gently, never nagging, and stop asking if they say no. Never fork, push or open a pull request without their
explicit yes. Explain in plain words what a pull request is. Sanitize first (no personal paths, hostnames/IPs, tokens,
emails, private notes) and show the final diff for approval. Full steps: SETUP.md section 17. Ideas that are not skills
go in an issue.
