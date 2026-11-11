#!/usr/bin/env python3
"""Check for (and optionally fetch) the companion jarvis-workspace folder. Standard library only.

    python scripts/ensure_workspace.py                    report only; never changes anything
    python scripts/ensure_workspace.py --clone            clone it if missing, then copy the templates
    python scripts/ensure_workspace.py --path <folder>    use another folder (also set JARVIS_WORKSPACE_ROOT)

The folder is JARVIS_WORKSPACE_ROOT if set, otherwise a "jarvis-workspace" folder next to this repo.
Templates (templates/*.md) are copied to the workspace root; existing files are never overwritten.
Exit code: 0 = workspace present (or created), 1 = missing and not cloned, 2 = clone failed.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/caden-gietzen/jarvis-workspace"
ROOT = Path(__file__).resolve().parent.parent


def default_path() -> Path:
    env = os.environ.get("JARVIS_WORKSPACE_ROOT")
    return Path(env) if env else ROOT.parent / "jarvis-workspace"


def copy_templates(ws: Path) -> list:
    """Copy templates/*.md to the workspace root, never overwriting. Returns the names copied."""
    copied = []
    tdir = ws / "templates"
    if not tdir.is_dir():
        return copied
    for src in sorted(tdir.glob("*.md")):
        dst = ws / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            copied.append(src.name)
    return copied


def ignored_templates(ws: Path):
    """-> (root template files that git ignores, those it does not). Empty lists if git cannot tell."""
    names = [p.name for p in sorted((ws / "templates").glob("*.md")) if (ws / p.name).exists()]
    ok, bad = [], []
    for n in names:
        try:
            r = subprocess.run(["git", "-C", str(ws), "check-ignore", "-q", n], capture_output=True)
        except OSError:
            return [], []
        (ok if r.returncode == 0 else bad).append(n)
    return ok, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clone", action="store_true", help="clone the workspace repo if the folder is missing")
    ap.add_argument("--path", help="workspace folder (default: JARVIS_WORKSPACE_ROOT or ../jarvis-workspace)")
    ap.add_argument("--repo", default=REPO_URL, help=argparse.SUPPRESS)  # tests point this at a local bare repo
    a = ap.parse_args(argv)
    ws = Path(a.path).expanduser() if a.path else default_path()

    if ws.is_dir():
        print(f"workspace folder found: {ws}")
    elif not a.clone:
        print(f"workspace folder not found: {ws} (run again with --clone to fetch it from {REPO_URL})")
        return 1
    else:
        print(f"cloning {a.repo} into {ws}")
        try:
            r = subprocess.run(["git", "clone", "-q", a.repo, str(ws)], capture_output=True, text=True)
        except OSError as e:
            print(f"clone failed: git could not run ({e})")
            return 2
        if r.returncode != 0:
            print("clone failed: " + (r.stderr.strip().splitlines() or ["git reported an error"])[-1])
            return 2
        print("cloned.")

    copied = copy_templates(ws)
    print("copied templates: " + (", ".join(copied) if copied else "none (already present or no templates folder)"))
    ok, bad = ignored_templates(ws)
    if ok and not bad:
        print("git ignores the personal note files there: ok")
    elif bad:
        print("WARNING: git does NOT ignore: " + ", ".join(bad) + " (do not commit your notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
