"""Tiny test client for the Jarvis browser helper.

  python jarvis_browser_cli.py open https://arxiv.org/abs/1706.03762 [--same-tab]   (opens a NEW tab by default)
  python jarvis_browser_cli.py show "attention is all you need" [--caption "the title"] [--to "end phrase"] [--expand sentence|paragraph] [--occurrence N|last] [--debug]
  python jarvis_browser_cli.py read [--max 5000]
  python jarvis_browser_cli.py clear
  python jarvis_browser_cli.py allow example.org
  python jarvis_browser_cli.py list_allowed
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("JARVIS_BROWSER_PORT", "47822"))
TOKEN_PATH = Path(os.environ.get("JARVIS_BROWSER_TOKEN", str(Path.home() / ".jarvis_browser_token")))


def send(payload: dict, timeout: float = 15.0):
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"Token file not found: {TOKEN_PATH} (start the helper once to create it)", file=sys.stderr)
        sys.exit(2)
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/cmd",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Jarvis-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        print(f"Could not reach the helper on port {PORT}: {e}", file=sys.stderr)
        sys.exit(3)


def occurrence_arg(v: str):
    if v.strip().lower() == "last":
        return "last"
    try:
        n = int(v)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError('occurrence must be a positive integer or "last"')
    return n


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # cp1252 consoles cannot print page text (U+200A, emoji, ...)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    o = sub.add_parser("open"); o.add_argument("url")
    # Default (user request 2026-11-07): ALWAYS open a new tab so the tab the user is looking at is never replaced.
    o.add_argument("--new-tab", action="store_true", help="open in a new tab (this is the default)")
    o.add_argument("--same-tab", action="store_true", help="navigate the current tab instead of opening a new one")
    s = sub.add_parser("show"); s.add_argument("text"); s.add_argument("--caption")
    s.add_argument("--to", dest="end_text", help="end phrase: highlight from TEXT through this phrase")
    s.add_argument("--expand", choices=["sentence", "paragraph"], help="expand the match to its sentence or paragraph")
    s.add_argument("--occurrence", type=occurrence_arg, help='which match to use: 1-based number (default 1) or "last"')
    s.add_argument("--debug", action="store_true", help="include a compact diag object in the result")
    r = sub.add_parser("read"); r.add_argument("--max", type=int, dest="max_chars")
    sub.add_parser("clear")
    a = sub.add_parser("allow"); a.add_argument("host")
    sub.add_parser("list_allowed")
    args = p.parse_args()

    payload: dict = {"action": args.action}
    if args.action == "open":
        payload.update(url=args.url, newTab=not args.same_tab)
    elif args.action == "show":
        payload["text"] = args.text
        if args.caption:
            payload["caption"] = args.caption
        if args.end_text:
            payload["endText"] = args.end_text
        if args.expand:
            payload["expand"] = args.expand
        if args.occurrence is not None:
            payload["occurrence"] = args.occurrence
        if args.debug:
            payload["debug"] = True
    elif args.action == "read" and args.max_chars:
        payload["maxChars"] = args.max_chars
    elif args.action == "allow":
        payload["host"] = args.host

    status, body = send(payload)
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if status == 200 and body.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
