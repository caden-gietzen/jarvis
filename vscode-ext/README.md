# jarvis-show-code

Tiny VS Code extension: an HTTP server on `127.0.0.2:47821` that opens a file, selects/highlights a line range and scrolls to it.

## Protocol
- `GET /ping` returns `{"ok":true,"name":"jarvis-show-code"}`
- `POST /show` with JSON `{"file":"C:\\abs\\path.py","startLine":40,"endLine":55,"note":"optional"}`
  - Lines are 1-based and inclusive; `endLine` is optional; clamped to document length.
  - 200: `{"ok":true,"file":...,"selected":{"startLine":n,"endLine":m},"totalLines":t}`
  - 400 bad input, 404 file missing (must be an absolute path to an existing file).
  - Highlight clears on next cursor move or after 60s; `note` shows in the status bar for 30s.
- `POST /terminal` with JSON `{"command":"pytest -q","cwd":"C:\\optional\\dir"}` runs a command in the integrated terminal of the focused window.
  - Uses `vscode.window.activeTerminal` if there is one, otherwise creates a terminal named `Jarvis`. Then `terminal.show(true)` (editor keeps focus) and `sendText(command, true)`.
  - `cwd` (existing absolute directory) is applied ONLY when a new terminal is created; an already-open terminal keeps its own directory.
  - `command` must be a non-empty string under 2000 chars with no newline characters, else 400. Bad `cwd` is also 400.
  - 200: `{"ok":true,"terminal":"<name>","created":false}`. The command output is not returned.
  - Requires header `X-Jarvis-Token` equal to the contents of `%USERPROFILE%\.jarvis_showcode_token` (constant-time compare), else 401. The extension creates that file (48 random hex chars) on activation if it does not exist; the Python side (`jarvis/showcode.py`) reads it. `/show` and `/ping` do not need the token.
- Requests with an `Origin` header, or a `Host` other than `127.0.0.2:47821` / `localhost:47821`, are rejected (403).
- Focus-follows-port: the most recently focused VS Code window owns the port and keeps it when blurred, so requests still work while you are in another app. A window that gains focus and finds the port taken sends `POST /release` (requires `X-Jarvis-Token`) to the owner, which answers 200 and closes its server only if its own window is not focused (409 otherwise), then retries listening up to 5 times, 300 ms apart. A window activated while unfocused listens only if nothing owns the port. Errors are logged to the "Jarvis Show Code" output channel.

## Install
Package: `npx @vscode/vsce package`, then `code --install-extension jarvis-show-code-0.0.2.vsix --force`.
Or without npm: copy this folder to `%USERPROFILE%\.vscode\extensions\jarvis.jarvis-show-code-0.0.2\`.
Then run "Developer: Reload Window" in VS Code.

Test: `curl http://127.0.0.2:47821/ping`
