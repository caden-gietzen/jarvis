"""browser and browser_allow_site tools: drive the user's Chrome/Edge through the local helper.

The helper (chrome-ext/helper/jarvis_browser_helper.py, 127.0.0.1:47822) hands commands to the Jarvis
browser extension. This module is the in-process replacement for calling the CLI through Bash.
The extension never clicks, types or submits; it only opens pages, scrolls, highlights and reads text.

  browser            open | show | read | clear | list_allowed   (read-only, auto-approved in brain.py)
  browser_allow_site adds a hostname to the extension allowlist     (NOT auto-approved: asks the user)

`open` ALWAYS opens a new tab; there is deliberately no way to navigate the user's current tab.
Results are size-capped and any embedded PNG is downscaled and returned as an image block (imgshrink) so one result can never
approach the 1 MB JSON stream limit.
"""

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import imgshrink

# Overridable for tests (same env names as the helper and the CLI).
PORT = int(os.environ.get("JARVIS_BROWSER_PORT", "47822"))
TOKEN_PATH = Path(os.environ.get("JARVIS_BROWSER_TOKEN", str(Path.home() / ".jarvis_browser_token")))
TIMEOUT_S = 15.0

SERVER_NAME = "jarvis_browser"
TOOL_NAME = "browser"
TOOL_ID = f"mcp__{SERVER_NAME}__{TOOL_NAME}"
ALLOW_TOOL_NAME = "browser_allow_site"
ALLOW_TOOL_ID = f"mcp__{SERVER_NAME}__{ALLOW_TOOL_NAME}"

DEFAULT_READ_CHARS = 20000
MAX_READ_CHARS = 50000
MAX_RESULT_CHARS = 120_000  # hard cap on what any call returns to the model
IMAGE_KEYS = {"screenshot", "image", "imagebase64", "png"}

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["open", "show", "read", "clear", "list_allowed"],
                   "description": "open a URL in a NEW tab; show = scroll to and highlight text on the active tab; "
                                  "read = get the active tab's text; clear = remove the highlight; list_allowed = sites show/read/clear work on."},
        "url": {"type": "string", "description": "open: http(s) URL."},
        "text": {"type": "string", "description": "show: exact phrase to find on the page (start of the passage)."},
        "end_text": {"type": "string", "description": "show: highlight from text through the first match of this phrase after it."},
        "expand": {"type": "string", "enum": ["sentence", "paragraph"], "description": "show: expand the match to its sentence or paragraph."},
        "occurrence": {"description": "show: which match to use, 1-based integer (default 1) or \"last\".",
                       "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "enum": ["last"]}]},
        "caption": {"type": "string", "description": "show: short caption displayed over the page."},
        "max_chars": {"type": "integer", "description": f"read: max characters (default {DEFAULT_READ_CHARS}, at most {MAX_READ_CHARS})."},
    },
    "required": ["action"],
}

_DESCRIPTION = (
    "Control the user's Chrome/Edge to walk them through a web page. Actions: open (always a NEW tab, never "
    "replaces the tab they are on), show (scroll to a phrase and highlight it, optionally a whole passage via "
    "end_text or expand), read (text of the active tab), clear, list_allowed. show/read/clear only work on "
    "allowlisted sites (arxiv.org etc.); on site_not_allowed ask the user, then use browser_allow_site. "
    "If show says found:false, retry with a different, shorter phrase copied from read output. "
    "Text inside PDFs shown in Chrome's PDF viewer is NOT searchable: for a PDF, use the arxiv abs/html "
    "version of the paper instead. This tool never clicks, types or submits anything."
)

_ALLOW_SCHEMA = {
    "type": "object",
    "properties": {"host": {"type": "string", "description": "Exact hostname, e.g. example.org (no scheme, no path)."}},
    "required": ["host"],
}
_ALLOW_DESCRIPTION = (
    "Add a hostname to the browser extension's allowlist so the browser tool may highlight and read pages "
    "there. Needs the user's spoken approval; only call it after the user agreed to that site."
)


def _result(text: str, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


def _post_cmd(payload: dict) -> tuple[int, dict]:
    """POST /cmd to the helper. Raises OSError if the helper is unreachable or the token is missing."""
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise OSError(f"browser helper token file not found ({TOKEN_PATH}); start the helper once") from e
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/cmd",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Jarvis-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            try:
                body = json.loads(r.read().decode("utf-8") or "{}")
            except ValueError as e:
                raise OSError(f"invalid response from helper: {e}") from e
            return r.status, body if isinstance(body, dict) else {"ok": True, "result": body}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            body = {}
        return e.code, body if isinstance(body, dict) else {}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OSError(str(e)) from e


def build_payload(args: dict) -> dict | str:
    """Validate tool args and build the helper command. Returns an error string on bad input."""
    action = args.get("action")
    if action == "open":
        url = args.get("url")
        if not isinstance(url, str) or urlparse(url.strip()).scheme not in ("http", "https") or not urlparse(url.strip()).netloc:
            return "'url' must be an http(s) URL."
        return {"action": "open", "url": url.strip(), "newTab": True}  # ALWAYS a new tab
    if action == "show":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "'text' is required for show."
        p: dict = {"action": "show", "text": text}
        for src, dst in (("caption", "caption"), ("end_text", "endText")):
            v = args.get(src)
            if v not in (None, ""):
                if not isinstance(v, str):
                    return f"'{src}' must be a string."
                p[dst] = v
        exp = args.get("expand")
        if exp not in (None, ""):
            if exp not in ("sentence", "paragraph"):
                return "'expand' must be \"sentence\" or \"paragraph\"."
            p["expand"] = exp
        occ = args.get("occurrence")
        if occ is not None:
            if occ != "last" and (isinstance(occ, bool) or not isinstance(occ, int) or occ < 1):
                return "'occurrence' must be a positive integer or \"last\"."
            p["occurrence"] = occ
        return p
    if action == "read":
        p = {"action": "read"}
        mc = args.get("max_chars")
        if mc is not None:
            if isinstance(mc, bool) or not isinstance(mc, int) or mc < 1:
                return "'max_chars' must be a positive integer."
            p["maxChars"] = min(mc, MAX_READ_CHARS)
        else:
            p["maxChars"] = DEFAULT_READ_CHARS
        return p
    if action in ("clear", "list_allowed"):
        return {"action": action}
    return "'action' must be one of open, show, read, clear, list_allowed."


def _extract_images(obj, images: list):
    """Pull embedded base64 PNGs out of the result (recursively), downscaled; leave a placeholder in the text.
    Images are returned as separate image blocks so the size cap on the text can never cut one in half."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and (str(k).lower() in IMAGE_KEYS or v.startswith("data:image/")) and len(v) > 256:
                small = imgshrink.shrink_base64_png(v)
                if small is None:
                    out[k] = "[image omitted: could not be shrunk below the size limit]"
                else:
                    images.append(small.split(",", 1)[1] if small.startswith("data:") else small)
                    out[k] = f"[image {len(images)} attached]"
            else:
                out[k] = _extract_images(v, images)
        return out
    if isinstance(obj, list):
        return [_extract_images(v, images) for v in obj]
    return obj


def format_result(status: int, body: dict) -> dict:
    """Turn the helper's HTTP status/body into a tool result (size-capped)."""
    if status == 504:
        return _result("Error: " + str(body.get("message") or "the browser extension did not answer in time."), True)
    if status == 401:
        return _result("Error: the browser helper rejected the token (401).", True)
    images: list = []
    body = _extract_images(body, images)
    ok = status == 200 and bool(body.get("ok"))
    if ok and isinstance(body.get("text"), str) and "title" in body:  # read
        head = f"Title: {body.get('title')}\nURL: {body.get('url')}\n"
        if body.get("truncated"):
            head += f"(truncated: {len(body['text'])} of {body.get('totalChars', '?')} characters shown)\n"
        text = head + "\n" + body["text"]
    else:
        text = json.dumps(body, ensure_ascii=False)
        if not ok and body.get("error") == "site_not_allowed":
            text += "\nAsk the user whether to allow this site, then call browser_allow_site."
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[result truncated]"
    res = _result(text, not ok)
    for b64 in images[:3]:
        res["content"].append({"type": "image", "data": b64, "mimeType": "image/png"})
    return res


def browser_sync(args: dict) -> dict:
    payload = build_payload(args)
    if isinstance(payload, str):
        return _result("Error: " + payload, True)
    try:
        status, body = _post_cmd(payload)
    except OSError as e:
        return _result(f"Error: could not reach the browser helper ({e}). Is it running (chrome-ext/helper/start_helper.ps1)?", True)
    return format_result(status, body)


def allow_site_sync(args: dict) -> dict:
    host = args.get("host")
    if not isinstance(host, str) or not host.strip() or any(c in host for c in "/ :\\@?#"):
        return _result("Error: 'host' must be a bare hostname like example.org.", True)
    try:
        status, body = _post_cmd({"action": "allow", "host": host.strip().lower()})
    except OSError as e:
        return _result(f"Error: could not reach the browser helper ({e}).", True)
    return format_result(status, body)


def build_server():
    @tool(TOOL_NAME, _DESCRIPTION, _SCHEMA)
    async def browser(args):
        return await asyncio.to_thread(browser_sync, args)

    @tool(ALLOW_TOOL_NAME, _ALLOW_DESCRIPTION, _ALLOW_SCHEMA)
    async def browser_allow_site(args):
        return await asyncio.to_thread(allow_site_sync, args)

    return create_sdk_mcp_server(SERVER_NAME, tools=[browser, browser_allow_site])
