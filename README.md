# Jarvis

> Want to set this up for yourself? Open this folder in Claude Code and say "set this up"; Claude will follow
> [SETUP.md](SETUP.md). Built and tested on Windows 11; macOS and Linux are untested (see SETUP.md).

Voice front end for Claude Code. Say "hey Jarvis", talk, and it sends your words to a
persistent Claude Code session that loads your normal `~/.claude` setup (your CLAUDE.md and agents;
examples are in `skills/`). Everything you say and everything it
replies is shown in the terminal and appended to `logs/<date>.log`.

## One-time setup

Jarvis drives the standalone Claude Code CLI, which needs its own sign-in:

    claude auth login

## Commands (run from this folder)

    .\jarvis devices     # list microphones
    .\jarvis listen      # wake word + transcription only
    .\jarvis chat        # type to Claude, no audio
    .\jarvis run         # voice -> Claude, spoken replies
    .\jarvis say "hi"    # test the voice (--voice NAME, or --all to hear each)
    .\jarvis voices      # list installed voices
    .\jarvis run --project path/to/folder --resume

Windows Smart App Control blocks uv's unsigned venv launcher (`.venv\Scripts\python.exe`), so
`jarvis.cmd` starts the base Python 3.12 with the venv's packages. `uv add` still works for installing packages.

`--resume` continues the last Jarvis session. Settings live in `config.yaml`.

## Speech to text

`stt.engine: local` uses faster-whisper on the CPU. Measured on this machine for a 4.5 s
clip: base.en 1.6 s, small.en 5 s, distil-large-v3 14 s. For better accuracy without the
wait, set `engine: groq` and put a free Groq API key in `GROQ_API_KEY` (audio is sent to Groq).

## Talking

A chime means it's listening. After a reply it keeps listening for a few seconds, so you
can answer without the wake word. Say "hey Jarvis" while it's talking to cut it off and
give a new instruction. Replies are spoken by Piper (`voices/en_GB-alan-medium.onnx`);
code blocks and tables are left to the terminal. Set `tts.engine: none` for text only.

## Spoken commands (handled by Jarvis, not sent to Claude)

- "Switch to the Ryan voice" - voices: alan, cori, jenny, northern, lessac, ryan.
  Default is `tts.piper.voice` in config.yaml. Add more by dropping a Piper .onnx + .json in `voices/`.
- "Switch to Sonnet" / "use Opus" / "Haiku" / "Fable" - changes the Claude model for the session.

## Folders

Claude works in `project_dir` (default `..`, the folder that holds this repo) and may also read and edit anything in
`allowed_dirs` (empty by default). Relative paths in config.yaml are relative to this repo.

## Permissions

Reads and anything your settings already allow run without asking. Anything else
(edits, most shell commands) asks `Allow? [y/N]` in the terminal. Voice confirmation is step 5.

## Math (LaTeX) in the web page and in study documents

Jarvis can show LaTeX math. KaTeX 0.19.1 (MIT, files and license in `jarvis\vendor\katex`, provenance and checksums in
`jarvis\vendor\katex\VERSION.txt`) is vendored and served by the Jarvis servers themselves; nothing is loaded from the
internet. Delimiters: `$...$` or `\(...\)` inline, `$$...$$` or `\[...\]` display, `\$` for a literal dollar. A dollar
sign next to a digit or a space ("$5 and $10") is not math.

- Web page conversation: replies and typed messages are scanned for math; text stays text nodes, math is drawn by
  `jarvis\vendor\jarvis-math.js` with `katex.render` (never innerHTML). Rejected math (any `\href`, `\url`, `\html*`,
  `\includegraphics`, more than 2000 characters, deeper than 40 braces, more than 200 macro expansions, sizes capped at
  10 em) is shown as plain source text. Page search highlights the text around the math; a match that sits only inside an
  equation keeps the message visible but marks nothing.
- Security: the page CSP gains only `script-src 'self'` (next to the existing hash), `style-src 'self'` and
  `font-src 'self'`, and `/vendor/...` serves an exact allowlist of files (`jarvis\mathsupport.py`). The sign-in page keeps the strict policy.
  Restart Jarvis for web.py changes to go live.
- Markdown preview: `python -m jarvis.mdpreview show ...` protects math from the Markdown engine, emits inert
  `<span class="jv-math" data-tex="...">` elements and (only on pages that contain math) links the same two vendored scripts.
  The preview server's CSP gained `script-src 'self'` and `style-src 'self'`; `/vendor/` is allowlisted there too. No preview restart is needed for new pages, but an already running preview server must be restarted once to serve `/vendor/` (it is a detached process).
- Study documents (permanent): Markdown files in `study-notes` in the workspace folder (`jarvis-workspace`, or `JARVIS_WORKSPACE_ROOT`). Open one with
  `python -m jarvis.mdpreview show study-notes convexification-example --open --passage "lossless convexification"`;
  `next`/`prev` walk the folder in file-name order. Sample: `study-notes\convexification-example.md`.
- Highlighter: the math is rendered as HTML only (no hidden MathML twin), so its text appears once on the page
  (`$C \subseteq \mathbb{R}^n$` is seen as `C⊆Rn`, a superscript is just the next character). Highlight the prose around
  an equation, or pass the glyph text of the equation as the passage; LaTeX source text (for example `\subseteq`) is never on the page, so do not use it as a passage.
  Screen readers get the TeX source through `role="math"` and `aria-label`.
- Spoken replies: do not read LaTeX aloud symbol by symbol. Say the idea in words ("minimize a convex function over a
  convex set") and let the page or the preview show the equation.
- Tests: `.venv\Scripts\python.exe tests\test_math.py` (offline; the last test drives headless Chrome via
  `tests\math_browser_check.mjs` and is skipped when node, Chrome or puppeteer-core are missing).

## License and third-party code

Jarvis is MIT licensed (see `LICENSE`). It bundles KaTeX 0.19.1, also MIT licensed (copyright Khan Academy and other contributors), unmodified under `jarvis/vendor/katex/` together with its own `LICENSE` file; see `jarvis/vendor/katex/VERSION.txt` for provenance. Python dependencies keep their own licenses (see `pyproject.toml` / `uv.lock`).
