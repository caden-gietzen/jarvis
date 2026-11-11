# Jarvis Browser (Chrome / Edge, Manifest V3)

Lets Jarvis open a page, scroll to a passage, highlight it and read the page text. It never
clicks, types or submits anything.

Chrome extensions cannot listen on a port, so a small local helper does:

    Jarvis / CLI --POST /cmd--> helper (127.0.0.1:47822) <--GET /poll-- extension
                                                         <--POST /result--

All requests need the shared token in `~/.jarvis_browser_token` (in your home folder) (header
`X-Jarvis-Token`). The helper creates the file on first start.

## 1. Start the helper

    powershell -File <jarvis repo>\chrome-ext\helper\start_helper.ps1

(Runs `jarvis_browser_helper.py` with the base Python named in `<jarvis repo>\.venv\pyvenv.cfg` (`home =`), stdlib only.
Stop with Ctrl+C.)

To start it automatically at login, run `powershell -File <jarvis repo>\chrome-ext\helper\install_autostart.ps1`
(creates `Jarvis Browser Helper.lnk` in your Startup folder, which runs `start_helper.ps1` hidden);
`install_autostart.ps1 -Uninstall` removes it. It does not start the helper until the next login; run
`start_helper.ps1` by hand once if you want it now. After editing helper Python, restart the helper;
after editing anything under `extension\`, reload the extension at `chrome://extensions`.

## 2. Load the extension

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and pick `<jarvis repo>\chrome-ext\extension`.
4. The options page opens. Run `start_helper.ps1 -CopyToken` (copies the token to the clipboard),
   paste it into the Token box and click **Save token**. Status should change to "connected".

## 3. Try it

    python helper\jarvis_browser_cli.py open https://arxiv.org/abs/1706.03762
    python helper\jarvis_browser_cli.py show "dominant sequence transduction models" --caption "the key claim"
    python helper\jarvis_browser_cli.py show "The dominant sequence" --to "attention mechanisms alone"
    python helper\jarvis_browser_cli.py show "dominant sequence transduction" --expand sentence
    python helper\jarvis_browser_cli.py show "dominant sequence transduction" --expand paragraph
    python helper\jarvis_browser_cli.py show "the" --occurrence 3        # third match instead of the first
    python helper\jarvis_browser_cli.py show "the" --occurrence last
    python helper\jarvis_browser_cli.py show "dominant sequence" --debug  # adds a compact diag object
    python helper\jarvis_browser_cli.py read
    python helper\jarvis_browser_cli.py clear

## Jarvis tool: `browser` (restart Jarvis to load it)

Jarvis no longer has to shell out to the CLI. `jarvis/browser.py` registers an in-process MCP server
`jarvis_browser` (wired in `jarvis/brain.py`) with two tools:

- `mcp__jarvis_browser__browser`: `{action: open|show|read|clear|list_allowed, url?, text?, end_text?, expand?, occurrence?, caption?, max_chars?}`.
  Auto-approved (no voice yes/no): it only opens, scrolls, highlights and reads. `open` always opens a NEW tab
  (`newTab:true` is forced; the tool has no way to navigate the user's current tab) and accepts http(s) URLs only.
  `read` returns `Title / URL / text` (default 20000 chars, at most 50000).
- `mcp__jarvis_browser__browser_allow_site`: `{host}` adds a hostname to the allowlist. NOT auto-approved: Jarvis asks first.

It talks to the helper on 127.0.0.1:47822 with the token file, exactly like the CLI (env `JARVIS_BROWSER_PORT`,
`JARVIS_BROWSER_TOKEN`). The helper must be running and the extension connected; otherwise the tool returns a clear error.
Code changes in `jarvis/browser.py` or `brain.py` only take effect after Jarvis is restarted.

Size safety: every result is capped (120 000 chars of text), and any base64 PNG in a result (`screenshot`, `image`, a
`data:image/` URL...) is downscaled by `jarvis/imgshrink.py` (stdlib + numpy, no Pillow; non-interlaced 8-bit PNG only) to at most
1280 px and 400 KB and returned as an image block, or dropped with a note if it cannot be shrunk. The same function works on files:
`python -m jarvis.imgshrink in.png out.png [max_dim] [max_bytes]` (run from the jarvis repo folder). The extension itself has no screenshot
command today; this is the guard for when one is added or a screenshot file must be read.

## Tests

    <jarvis repo>\.venv\Scripts\python.exe -m unittest discover <jarvis repo>\chrome-ext\tests
    node <jarvis repo>\chrome-ext\tests\test_content.js        (prints ok)

## Allowed sites

`show`, `clear` and `read` only work on the active tab if its hostname is on the allowlist
(exact hostname match). Defaults: arxiv.org, www.arxiv.org, ar5iv.org, ar5iv.labs.arxiv.org,
example.com, localhost. Otherwise the result is `{ok:false, error:"site_not_allowed", host}`
and the page is not touched. `open` works for any http(s) URL.

Add a site either:
- in the extension options page (one hostname per line, **Save sites**), or
- `python helper\jarvis_browser_cli.py allow example.org` (this is what Jarvis uses after you
  say yes). `list_allowed` shows the current list.

## Commands (POST /cmd, JSON)

- `{action:"open", url, newTab?}`
- `{action:"show", text, caption?, endText?, expand?, occurrence?, debug?}` returns `{ok, found, matchCount, occurrence}` (plus `diag` when `debug:true`); `found:false` means retry with another phrase.
  - `occurrence`: which match of `text` to use, 1-based (default 1) or `"last"`. The result echoes the chosen `occurrence` and the total `matchCount`; if the page has fewer matches the result is `found:false` with `matchCount` set. CLI: `--occurrence N|last`. The helper rejects values that are not a positive integer or `"last"` (HTTP 400).
  - `debug`: CLI `--debug`. Adds `diag` to the result: `{rect, scrollY, viewport, highlightApi, registered, boxPresent, el}`. Omitted otherwise. (`rect` is the viewport-relative bounding box of the highlight, `registered` whether the CSS highlight is set, `boxPresent` whether the outline box exists.)
  - `endText`: highlight the whole passage from the start of `text` through the end of the first match of `endText` after it (spans elements).
  - `expand`: `"sentence"` or `"paragraph"` expands the match to its full sentence, or to its enclosing block (p, li, blockquote, ...). Combines with `endText`. Sentence detection does not break after common abbreviations (`Fig.`, `Eq.`, `Sec.`, `No.`, `vs.`, `cf.`, `et al.`, `e.g.`, `i.e.`, `Dr.`, ...), dotted forms like `U.S.`, single-letter initials (`D. Malyuta`) or inside decimals (`3.5`). Limitation: a sentence that really ends in a single letter or one of those words is not split there.
  - Passage modes draw a semi-transparent yellow highlight plus an orange outline box and scroll it into view; `clear` removes it. Without these fields only the phrase is highlighted, as before.
- `{action:"clear"}`
- `{action:"read", maxChars?}` returns `{title, url, text, truncated}` (default 20000 chars). Jarvis's own overlays (caption, outline box, bands) are hidden while the text is read, so they never appear in it; real page figure captions are kept.
- `{action:"allow", host}`, `{action:"list_allowed"}`

`/cmd` waits up to 10 s for the extension, else HTTP 504 `{error:"timeout"}`.

## Notes

- The service worker long-polls the helper and a 30 s alarm restarts it if Chrome kills it;
  it reconnects with backoff (1 s up to 15 s) if the helper is not running.
- Not supported: `<br>`-only line breaks count as no space when matching; text inside
  iframes, shadow DOM and PDFs is not searched. **PDFs opened in Chrome's built-in viewer are not searchable**:
  `show` and `read` see nothing there. Open the arXiv abs/HTML (or ar5iv) version of the paper instead.
- The CLI forces UTF-8 on stdout/stderr so page text with unusual characters prints on Windows consoles.
- Env overrides for the helper: `JARVIS_BROWSER_PORT`, `JARVIS_BROWSER_TOKEN` (token file path).
  The extension port is fixed at 47822 in `background.js`.
