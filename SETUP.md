# SETUP.md: instructions for Claude (the friend's Claude Code)

You are Claude Code. Someone has cloned this repo (Jarvis, a voice interface for Claude Code) and asked you to set it
up for them. This file is for you. Read all of it first, then walk the user through it step by step.

## 0. Read this before anything else

**Disclaimer: this was developed and tested on Windows 11 only.** The author has not run it on macOS or Linux.
Treat these steps as a guide, not as law. Where a step is Windows-specific, adapt it to the user's system, tell
them what you changed and why, and flag anything you could not verify. If something here conflicts with what you see on
the machine, trust the machine and explain the difference. Parts marked **(untested)** are suggestions only.

Rules for you while doing this:

1. **FIRST, ask which operating system the user is on** (Windows, macOS, Linux). The user is probably on macOS. Remember
   the answer and adapt everything below to it.
2. **Ask before every install and every external account step** (installing software, signing in, creating an account,
   downloading a model, editing anything outside this folder). Say what it is and why, then wait for a yes.
3. **Never reuse anyone else's secrets.** This repo has none. Use placeholders such as `<your-topic>` and
   `<your-pc>.<your-tailnet>.ts.net`, and have the user create their own tokens, topics and hostnames. Never print a
   token or topic back into the chat or into a file that gets committed.
4. Do not touch the user's existing `~/.claude` setup (settings, CLAUDE.md, skills) except to add the optional skills in
   section 11, and only after they say yes. Do not change their Claude Code permission settings. The one Jarvis file
   about permissions you may edit, and only with their yes, is `DEFAULT_WORKSPACE` in `jarvis/permissions.py` (section 4a).
5. Everything optional (phone access, push notifications, Chrome extension, VS Code extension, skills, workspace
   notes) is optional. Ask whether they want each one; do not set up what they do not want.
6. Do not `git push` anything, and do not change this repo's git remotes.

### Ask me about (collect these before you start; keep answers short)

- Operating system and chip (Windows / macOS Apple silicon or Intel / Linux).
- Their name or what Jarvis should call them (used only in their own notes; nothing is hard-coded).
- Where they keep code (the folder that will hold the `jarvis` repo, e.g. `~/dev`). Jarvis works in the folder that
  contains the repo by default (`project_dir: ..`).
- Which microphone and speakers/headphones they use (and whether they have headphones: with speakers, Jarvis can hear
  itself).
- Voice preference: Piper voice (male/female, British/American; see section 6), or text-only replies.
- Whether they want to use the local Whisper speech-to-text (default, private, no account) or Groq (needs an account and
  sends audio to Groq).
- Whether they want **phone access** (the Jarvis page on their phone over Tailscale): yes/no. If yes, do they already
  use Tailscale?
- Whether they want **push notifications** (ntfy) when Jarvis needs a permission or finishes after a usage limit: yes/no.
- Whether they want the **usage-limit auto-restart** (needs a restart mechanism that only exists for Windows today), and
  if so whether Jarvis should **keep the computer awake while waiting** for a limit to reset (section 10).
- Which folder is their **dev folder** (where they keep projects Claude may work in with full permission) (section 4a).
- Whether they use Chrome (for the browser extension) and VS Code (for the show-code extension).
- Whether they want the author's **skills and agents** (section 11) and a **workspace notes folder** (section 12).
- Which Claude model to start with (`sonnet`, `opus`, `haiku`, or their Claude Code default).

## 1. Windows-only parts (be honest about these)

These modules only work on Windows. The rest of Jarvis is plain Python and should be portable but is unverified off
Windows. The code mostly degrades gracefully (checks `sys.platform`, catches errors, logs and carries on), but features
that depend on these parts will simply not work. Suggested macOS equivalents are **untested**.

| Part | What it does | On other systems |
|---|---|---|
| `jarvis.cmd`, `bootstrap.py` | Windows launcher (`.\jarvis ...`). It exists because Windows Smart App Control can block the venv's launcher. | Run `uv run python -m jarvis <command>` instead. Skip `jarvis.cmd`. |
| `jarvis/restart.py`, `scripts/restart_jarvis.ps1` | Spoken "restart Jarvis" and the usage-limit auto-restart: PowerShell + WMI (`Win32_Process`) + `ctypes.windll` (find the console window and its parent PowerShell, kill and relaunch). | Not functional. The flag file is still written, but the restart script cannot run. Suggestion (untested): a small shell script that waits for the old PID to exit and relaunches `uv run python -m jarvis run --resume` in the same terminal (on macOS, `osascript` can drive Terminal/iTerm), killing with `kill` instead of `taskkill`. Until then: restart by hand (Ctrl+C, run again). |
| `jarvis/usage_monitor.py` (the part that restarts) | Arms a timer for when the usage limit resets, then requests the restart above. Plain Python, but its payoff depends on `restart.py`. | Runs and logs, but the automatic restart will not complete off Windows. |
| `jarvis/keepawake.py` | **Opt-in** (config key `keep_awake_while_waiting`, off by default, section 10). Keeps the PC awake while a usage-limit restart is pending, using `SetThreadExecutionState`. The sleep/wake detection in the same file (so a sleep is not mistaken for a hang) is always on and harmless. | The hold is a no-op off Windows (the Windows-only API is called only on `win32`). macOS suggestion (untested, not implemented): run `caffeinate -dims` while a restart is pending; Linux: `systemd-inhibit`. |
| `chrome-ext/helper/*.ps1` and `start_browser_helper` in `jarvis/__main__.py` | Start the Chrome helper hidden with PowerShell and add it to the Windows Startup folder. | The helper itself (`jarvis_browser_helper.py`) is stdlib Python: start it by hand with `python chrome-ext/helper/jarvis_browser_helper.py`. Keep `start_browser_helper: false`. A macOS login item or `launchd` agent is the equivalent of the Startup folder (untested). |
| `jarvis/speaker.py` `sapi` engine | Windows' built-in voice via PowerShell. | Use `tts.engine: piper` or `none`. (On macOS the built-in `say` command could be wrapped; untested, not implemented.) |
| `jarvis/__main__.py` Chrome lookup | Finds `chrome.exe` in Program Files. | Falls back to the default browser's "new tab". Fine. |
| `jarvis/web.py` Tailscale path | Looks for `C:\Program Files\Tailscale\tailscale.exe`, then `tailscale` on PATH. | PATH lookup works if the `tailscale` CLI is installed. On macOS the app's CLI may need enabling or a symlink (untested). |
| `scripts/*.ps1`, and `end_session.ps1` in the workspace repo | Windows helpers. | Skip. |
| Skill `tile-windows` | Win32 window tiling. | Windows only; see `skills/README.md`. |
| VS Code extension port | Listens on `127.0.0.2`, which exists on Windows and Linux by default but **not on macOS** (only `127.0.0.1`). | On macOS, either `sudo ifconfig lo0 alias 127.0.0.2` (untested) or change the host in `vscode-ext/extension.js` and `jarvis/showcode.py` together. |

Also hard-wired: `jarvis/permissions.py` has `DEFAULT_WORKSPACE = r"C:\dev"`, the folder inside which reads and edits
are auto-approved without asking. On any other machine nothing matches that path, so Jarvis simply asks for permission
more often (it fails safe). Section 4a tells you what to ask the user and when you may change it. It also expects the
repo folder to be named `jarvis`, inside the workspace folder.

## 2. Prerequisites (check first, then ask before installing anything)

Check what is already there; do not install what exists.

| Need | Check | Install suggestion (ask first) |
|---|---|---|
| Git | `git --version` | macOS: Xcode command line tools (`xcode-select --install`) or Homebrew `brew install git`. |
| Claude Code CLI, signed in | `claude --version`, then `claude auth login` if not signed in | See Anthropic's Claude Code install docs. Jarvis drives the standalone CLI, which needs its own sign-in. A Claude subscription or API access is required (the user's own account). |
| Python 3.12 and **uv** | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (macOS/Linux) or `winget install astral-sh.uv` / `irm https://astral.sh/uv/install.ps1 \| iex` (Windows). uv will fetch Python 3.12 itself (`.python-version` says 3.12). |
| A working microphone | Jarvis can list devices later (`devices`) | macOS will ask for **microphone permission** for the terminal app the first time: the user must allow it in System Settings, Privacy & Security, Microphone. |
| Speakers or headphones | | Headphones avoid Jarvis hearing itself. |
| Node.js | `node --version` | **Only needed** to run the JavaScript tests of the Chrome extension (`chrome-ext/tests/test_content.js`) or to package the VS Code extension. Skip unless the user wants those. |
| Chrome, VS Code | | Only for the optional extensions. |
| Tailscale, ntfy app | | Only for optional phone access / push (sections 8 and 9). |

Disk and network: the first `uv sync` downloads a few hundred MB to about 1 GB (PyTorch for the voice-activity model,
ONNX runtimes, faster-whisper, Piper). The first run also downloads the Whisper model (about 150 MB for `base.en`)
from Hugging Face. Tell the user and get a yes before starting.

## 3. Clone and create the environment

Suggested layout (a folder that holds both repos; Jarvis uses its parent folder as its project by default):

```
~/dev/
  jarvis/              <- this repo
  jarvis-workspace/    <- optional notes + tooling (section 12)
```

If the user already cloned this repo somewhere, work from there; the sibling layout is just the default.

```
cd <the jarvis repo folder>
uv sync                      # creates .venv from pyproject.toml / uv.lock (ask first: big download)
```

Check the environment works without a microphone or network:

```
uv run python -c "import jarvis, faster_whisper, piper, openwakeword, sounddevice, silero_vad; print('imports ok')"
```

(Windows: `.venv\Scripts\python.exe -c ...`; macOS/Linux: `.venv/bin/python -c ...` or `uv run python -c ...`.)

If `uv sync` fails on a package for the user's platform (for example a missing macOS wheel), report the exact error
and propose an option instead of guessing; do not edit `pyproject.toml` or `uv.lock` without asking.

## 4. Configure: `config.yaml`

Defaults are conservative: web page on this computer only, browser helper off, phone push does nothing until a topic file exists. Show the user the file
and walk through the options that matter (all optional; the defaults work):

- `project_dir` (default `..`, the folder that holds the repo) and `allowed_dirs` (default none): where Claude Code
  works and may edit. Relative paths are relative to the repo. Use an absolute path to point elsewhere. You can also
  pass `--project <path>` on the command line.
- `audio.device`: `null` = default microphone, or a name/index from the `devices` command.
- `wake_word.model` (`hey_jarvis` by default; other bundled models: `alexa`, `hey_mycroft`, `timer`, `weather`) and
  `wake_word.threshold` (raise if it triggers falsely, lower if it misses).
- `endpointing.*`: how long a pause ends the user's turn (`end_silence_ms`), how long it keeps listening after a
  reply without the wake word (`followup_window_s`).
- `stt.engine`: `local` (faster-whisper on the CPU; `stt.local.model` is `base.en` by default; `small.en` is more accurate
  and slower) or `groq` (needs the user's own `GROQ_API_KEY` environment variable; audio is sent to Groq).
- `tts.engine`: `piper` (needs a voice file, section 6), `sapi` (Windows only), or `none` (text only).
  `tts.piper.voice` and `length_scale` (below 1 = faster speech).
- `claude.model`: `sonnet`, `opus`, `haiku` or `null` for the user's Claude Code default.
- `notify_phone`, `notify_on_usage_resume` (push via ntfy, section 9), `web_ui`, `web_bind` (`local` or `tailnet`,
  section 8), `start_browser_helper`, `open_web_ui_on_start`, `auto_continue_after_limit`,
  `continue_after_intentional_restart`, `keep_awake_while_waiting` (off by default, section 10), `token_ledger`,
  `dictation_max_s`. Comments in the file explain each.

Environment variables worth knowing: `JARVIS_WORKSPACE_ROOT` (the notes folder; default `../jarvis-workspace`),
`JARVIS_BOOKS_ROOT` (folders of Markdown books shown by the Markdown preview; default `../textbooks`), `JARVIS_CODE_CMD` (full path to
the VS Code `code` command if it is not on PATH), `JARVIS_NOTIFY_PHONE=0`, `JARVIS_OPEN_WEB_UI=0`.

## 4a. Permissions: the dev folder (ASK the user; change nothing until they say yes)

`jarvis/permissions.py` decides what Jarvis approves automatically and what it asks about. One constant there,
`DEFAULT_WORKSPACE`, names the folder inside which Claude's reads and edits are approved **without asking**. It ships as
the author's `C:\dev`, which does not exist on other machines, so by default the new user gets a permission prompt for
nearly everything. Do not edit this file on your own. Say this to the user, in plain words:

> Jarvis can auto-approve file reads and edits inside one folder so it does not interrupt you every few seconds. I
> suggest creating a folder called `dev` for the projects you want Claude to have full permission over (`~/dev` on
> macOS, `C:\dev` on Windows), or you can name another folder. Anything inside that folder can be read and changed by
> Claude without asking you first, so put only projects there that you are comfortable with that, and keep anything
> private or irreplaceable elsewhere. Do you want me to set that up, and which folder should it be?

Only after the user says yes: create the folder if needed (ask first), then set `DEFAULT_WORKSPACE` in
`jarvis/permissions.py` to that folder (use a raw string on Windows, an absolute path elsewhere; check how the
constant is compared in that file and adapt it if it assumes Windows paths), and re-run `tests/test_permission_web.py`
and `tests/test_destructive.py`. If they say no, leave the file alone: Jarvis then asks for permission more often,
which is safe. The jarvis repo is expected to live inside this dev folder (section 3).

## 5. First run

Do these in order; each step proves one more piece.

1. `devices`: list microphones. Pick one if the default is wrong and put its name/index in `audio.device`.
2. `chat`: type to Claude with no audio. Proves the Claude Code sign-in and the Agent SDK work.
3. Voice (section 6), then `say "Hello"`.
4. `listen`: wake word and transcription only, no Claude. Say "hey Jarvis" and a sentence; check the transcript.
   The first run downloads the Whisper model (ask first).
5. `run`: the real thing (voice to Claude, spoken replies).

How to invoke: Windows `.\jarvis <command>`; macOS/Linux `uv run python -m jarvis <command>`. Commands: `devices`,
`listen`, `chat`, `run` (options `--project PATH`, `--resume`), `say "text"` (`--voice NAME`, `--all`), `voices`,
`web-token` (`--rotate`).

macOS: if the microphone is silent or errors, the terminal app probably lacks microphone permission (System Settings,
Privacy & Security, Microphone). If `sounddevice` cannot find PortAudio, tell the user what you saw before installing
anything (`brew install portaudio` is the usual fix; untested here).

## 6. Wake word and voice choices

- **Wake word**: "hey Jarvis" via the bundled openWakeWord model (no account, runs locally). Alternatives are listed in
  section 4. Custom wake words are possible with openWakeWord but are not set up here.
- **Piper voices are NOT in the repo** (the `voices/` folder is git-ignored). Ask the user which they like, then, after
  they say yes to the download (each is about 60 to 120 MB, from Hugging Face), fetch into `voices/`:

  ```
  uv run python -m piper.download_voices --download-dir voices en_GB-alan-medium
  ```

  Other good ones: `en_GB-cori-high`, `en_GB-jenny_dioco-medium`, `en_GB-northern_english_male-medium`,
  `en_US-lessac-high`, `en_US-ryan-high` (the full list is at the Piper voices page on Hugging Face). Set
  `tts.piper.voice` in `config.yaml` to the file stem. `voices` lists what is installed. While Jarvis runs the user can
  say "switch to the Ryan voice" (it knows the names alan, cori, jenny, northern, lessac, ryan; other voices keep
  working through the config).
- No voice yet? `tts.engine: none` gives text-only replies, which is enough to test everything else.

Spoken commands handled by Jarvis itself: voice switching, "switch to Sonnet/Opus/Haiku", "mute" / "voice off" /
"voice on", "start dictation" / "stop dictation", and a spoken restart (Windows only today).

## 7. The web page (local, built in)

With `web_ui: true`, `run` serves a page on `127.0.0.1:47823` (conversation, agent panel, typed input, mute, dictation) and
prints a link with a per-user token, also saved to `logs/web_url.txt`. The token lives in `~/.jarvis_web_token`
(created on first run; `web-token` shows where, `web-token --rotate` replaces it). It is the page's password: never paste
it into chat, commits or screenshots. `open_web_ui_on_start` opens the page in a new browser tab on a cold start.

## 8. Phone access over Tailscale (OPTIONAL; ask first, skip if not wanted)

Lets the user open the Jarvis page (an installable web app) on their phone, privately. Nothing is exposed to the
public internet. It needs the user's own Tailscale account on the PC and the phone.

1. Ask whether they already have Tailscale. If not, offer to guide the sign-up and install (their account, their
   consent). Both devices must be on the same tailnet.
2. In `config.yaml` set `web_bind: tailnet`. Jarvis then also listens on this PC's Tailscale address (never `0.0.0.0`).
   Restart Jarvis.
3. For an installable HTTPS app on the phone, use Tailscale Serve (the user's own MagicDNS name will look like
   `<your-pc>.<your-tailnet>.ts.net`; find it with `tailscale status` or the admin console):

   ```
   tailscale serve --bg http://127.0.0.1:47823
   ```

   (Ask first; it needs HTTPS certificates enabled for the tailnet in the Tailscale admin console. Undo with
   `tailscale serve --https=443 off`.)
4. Get the link to the phone without typing the token: in the `jarvis-workspace` repo run
   `python make_phone_qr.py --https-host <your-pc>.<your-tailnet>.ts.net` (needs `pip install "qrcode[pil]"` in a
   throwaway venv; ask first). The PNG **contains the token**: scan it, then delete it.
5. Windows firewall: the existing "Tailscale-In" rules normally suffice. macOS: allow incoming connections if asked.

## 9. Push notifications with ntfy (OPTIONAL; ask first)

Jarvis can push a short phone notification when it needs a permission and when it resumes after a usage limit.

1. Ask the user to install the **ntfy** app on their phone (their choice).
2. Have them pick a long random private topic name (it acts as the password: anyone who knows it can read the
   notifications). You can generate one: `python -c "import secrets; print('jarvis-' + secrets.token_urlsafe(16))"`.
   Do not echo it back into chat history if you can avoid it.
3. Write it to `~/.jarvis_ntfy_topic` (one line, the topic only) and have them subscribe to it in the app.
4. Set `notify_phone: true` (and `notify_on_usage_resume: true` if wanted) in `config.yaml`.
   Push goes through the public ntfy.sh service; notifications contain short permission summaries, nothing secret.

## 10. Usage-limit auto-restart monitor (OPTIONAL; Windows only today)

What it does: when Claude hits a subscription usage limit, `jarvis/usage_monitor.py` reads the reset time from the rate
limit events, arms a timer, and after the reset restarts Jarvis and feeds it one "continue where we left off" prompt that
points at `HANDOFF.md` in the workspace folder. Config: `notify_on_usage_resume`, `auto_continue_after_limit`,
`continue_after_intentional_restart`.

- On Windows this works end to end (restart script via PowerShell). The user needs a workspace with a `HANDOFF.md`
  (section 12) for the continue prompt to be useful.
- **Keep the computer awake while waiting (`keep_awake_while_waiting`, default `false`): ASK the user.** Explain: when
  Claude hits a usage limit, Jarvis waits for the reset and then restarts itself. If the computer goes to sleep during
  that wait, the timer cannot fire, so the restart happens only after they wake it. With this switch on, Jarvis asks
  Windows not to sleep (only while it is waiting, and it lets go as soon as the wait is over). It only matters if they
  leave the machine unattended while a limit is pending; if they are usually at the computer, leave it off. It costs
  power and battery while waiting. The code is Windows-only today (`SetThreadExecutionState`); on macOS nothing would
  happen until a `caffeinate` version is written, so say so. Then set `keep_awake_while_waiting: true` or `false` in
  `config.yaml` to match their answer. Missing key means off.
- Elsewhere the monitor still tracks limits and logs, but the restart step is not implemented (section 1). Be upfront:
  say that on macOS the user has to restart by hand after a limit, and offer to write the small relaunch script
  described in section 1 if they want it (untested; their call).

## 11. Skills and agents (OPTIONAL)

`skills/` holds the author's Claude Code skills (with personal paths removed) and `agents/` holds four sub-agents.
Read `skills/README.md`, tell the user what each does, and ask which they want. Then:

```
python skills/install.py            # dry run
python skills/install.py --yes      # copy to ~/.claude/skills and ~/.claude/agents (never overwrites without --force)
```

`tile-windows` is Windows only. `use-tokens-freely` expects the workspace repo (section 12).
`orchestrator-CLAUDE.md.example` is a template for the user's own `~/.claude/CLAUDE.md`; show it, never install it
automatically.

## 12. The workspace folder (OPTIONAL but recommended)

The `jarvis-workspace` repo (a separate repo, `https://github.com/caden-gietzen/jarvis-workspace`; ask before cloning) is a
folder next to this one that holds the user's own working notes and the helper tooling. To make **their own**:

1. Clone it next to this repo as `jarvis-workspace` (or any path, then set `JARVIS_WORKSPACE_ROOT`).
2. Copy `templates/*.md` to its root. These become `HANDOFF.md` (running log read after every restart, newest entries
   at the bottom), `open-questions.md`, `future-tasks.md`, `long-term.md`, `completed-tasks.md`, `blocked.md`.
   They are git-ignored there on purpose so personal notes are not committed by accident.
3. Fill in `HANDOFF.md` with the user's standing rules (for example: work only inside the dev folder, ask before
   installing, never commit secrets).
4. The tooling there (for example the phone QR script) is optional.

## 13. Optional extensions

- **Chrome/Edge extension** (`chrome-ext/`): lets Jarvis open a page, scroll to a passage and highlight it; it never clicks
  or types. Follow `chrome-ext/README.md`: start the local helper (stdlib Python, port 47822), load `chrome-ext/extension`
  unpacked at `chrome://extensions` with Developer mode on, paste the token the helper creates in
  `~/.jarvis_browser_token` into the extension's options page (never into chat). On Windows `start_helper.ps1` does the
  start; elsewhere start `chrome-ext/helper/jarvis_browser_helper.py` with Python by hand. Optional Node tests:
  `node chrome-ext/tests/test_content.js`.
- **VS Code extension** (`vscode-ext/`): lets Jarvis open a file and highlight lines while it explains. Install per
  `vscode-ext/README.md` (`code --install-extension <vsix>` after packaging with `npx @vscode/vsce package`, which needs
  Node, or copy the folder into `~/.vscode/extensions/`). Then "Developer: Reload Window". Check with
  `curl http://127.0.0.2:47821/ping` (macOS note in section 1).

## 14. Verification checklist

Offline checks first (no microphone, no accounts). The test suites are plain scripts; each prints
`N/N passed` and exits non-zero on failure. From the repo root:

```
# macOS/Linux
for t in tests/test_*.py; do echo "== $t"; uv run python "$t" | tail -1; done
# Windows (PowerShell)
Get-ChildItem tests\test_*.py | ForEach-Object { "== $_"; .venv\Scripts\python.exe $_.FullName | Select-Object -Last 1 }
```

Expect all suites to pass on Windows. Off Windows, record which fail and why (path separators, `os.name == "nt"`
assumptions) and report them to the user rather than editing tests to pass. `tests/test_math.py` and
`tests/math_browser_check.mjs`, if present, need Node and a browser and are not part of the basic check.

Then live checks, one at a time, telling the user what to do and what to expect:

- [ ] `devices` lists the right microphone.
- [ ] `chat`: a typed question gets an answer.
- [ ] `say "Hello"` is heard (or text-only if `tts.engine: none`).
- [ ] `listen`: "hey Jarvis" triggers a chime and the transcript matches what was said.
- [ ] `run`: a spoken question is answered aloud; the terminal and `logs/<date>.log` show both sides.
- [ ] Saying "hey Jarvis" while it talks cuts it off.
- [ ] The web page opens from the printed link and shows the conversation; typed input works.
- [ ] A permission prompt appears for a file edit and can be answered by voice or on the page.
- [ ] (Optional) phone page opens over Tailscale; ntfy test push arrives; browser/VS Code extensions respond.
- [ ] No token, topic or hostname ended up in any file inside the repo (`git status` should show only intended changes;
  `.jarvis_*`, `voices/`, `logs/` are git-ignored).

## 15. Troubleshooting

| Symptom | Likely cause and what to try |
|---|---|
| Nothing is heard / no chime | Wrong microphone (`devices`, set `audio.device`); on macOS the terminal lacks microphone permission. |
| It triggers on its own or reacts to its own voice | Use headphones; raise `wake_word.threshold`; lower the volume. |
| It misses "hey Jarvis" | Lower `wake_word.threshold` a little (0.3 to 0.4); speak a bit slower; check the microphone level. |
| Transcription is poor or slow | Try `stt.local.model: small.en` (slower) or `base.en`; or `stt.engine: groq` with the user's own key. |
| `No voice called X` / silence | The Piper voice file is missing from `voices/` (section 6); `voices` lists installed ones. |
| `chat` fails to start or says not signed in | Run `claude auth login`; check `claude --version` works in the same terminal. |
| `uv sync` fails on a package | Report the exact error and platform; do not edit the lockfile without asking. |
| Windows: `.venv\Scripts\python.exe` blocked | Smart App Control; use `.\jarvis ...` (it starts the base Python named in `.venv\pyvenv.cfg`). |
| Permission prompts for everything | Expected until a dev folder is set: `permissions.py` auto-approves only inside `DEFAULT_WORKSPACE` (section 4a). |
| Web page link refused or 403 | Use the exact printed link (it contains the token); the Host/Origin checks reject other names. After `--rotate`, old links stop working. |
| Phone cannot connect | `web_bind: tailnet` set and Jarvis restarted? Both devices signed in to the same tailnet? Firewall? Serve running (`tailscale serve status`)? |
| ntfy push never arrives | Topic file has exactly one line; `notify_phone: true`; the app is subscribed to the same topic; `JARVIS_NOTIFY_PHONE` is not `0`. |
| Restart or usage auto-restart does nothing | Expected off Windows (section 1). Check `logs/restart.log` on Windows. |
| VS Code highlight does nothing | Extension not installed/reloaded, or `127.0.0.2` is not available (macOS). |
| Something wrote into `~/.claude` unexpectedly | Only `skills/install.py --yes` does, and only the skills/agents the user approved. |

When something is wrong, read `logs/<date>.log` first. If a problem looks like a bug in the code rather than setup, tell
the user plainly what you found, and propose a minimal fix instead of rewriting large parts.

## 16. When you are done

Summarize for the user, in plain language: what is working and was actually verified, what you skipped or could not test
(be specific: macOS, restart, phone), the commands they will use day to day, and where their secrets live
(`~/.jarvis_web_token`, `~/.jarvis_ntfy_topic`, `~/.jarvis_browser_token`: never share or commit them). Offer to write
down anything useful in their own `jarvis-workspace/HANDOFF.md`.

## 17. Contributing skills back (optional)

Jarvis is shared so people can build on it. If the user makes a skill (or agent, or helper script) that others might
find useful, offer to help them share it. Do this gently and only with their say-so:

- **When to ask.** When they create a new skill or clearly improve one in `~/.claude/skills` (or this repo's `skills/`
  folder), when they finish one, or, now and then, when they have skills that are not in this repo. At most once per
  session. Never nag; if they say no, stop asking.
- **Explain first.** Many people have never made a pull request. Say it plainly: a pull request is a polite proposal
  ("here is a change, would you like it?") that the maintainer can read, comment on, accept or decline. Nothing is
  merged automatically; the maintainer reviews every one.
- **Never act without an explicit yes.** Do not fork, push or open a pull request until the user clearly says so.
- **Clean it first.** Remove personal paths, hostnames and IP addresses, tokens, emails, private notes, and anything
  taken from the user's own workspace. Show the user the final diff and get their approval before going on.
- **How.** Fork `caden-gietzen/jarvis` under the user's own GitHub account. Add the skill as `skills/<name>/` with a
  `SKILL.md`, plus a one-line entry in `skills/README.md`. Make a branch, push it to their fork, and open a pull request
  to the upstream repo (with `gh pr create` if the GitHub CLI is installed, otherwise walk them through the GitHub web
  page: "Compare & pull request"). Keep it small, and describe what the skill does and how to try it.
- **Other ideas.** For anything that is not a skill (a bug, a feature idea), suggest opening an issue instead.
- **Tooling.** Follow the `contribute-skill` skill (`skills/contribute-skill/SKILL.md`, installed by `skills/install.py`): it
  runs `skills/contribute-skill/scripts/scan_contribution.py` (secrets, personal paths, hostnames, emails), fills the pull request
  template `.github/PULL_REQUEST_TEMPLATE.md` and applies the rules in `CONTRIBUTING.md`. Do not duplicate those steps here.
