"""Install the shareable skills and agents into your Claude Code folder (~/.claude).

    python skills/install.py            show what would be copied (dry run, changes nothing)
    python skills/install.py --yes      copy it
    python skills/install.py --yes --only tile-windows   just one skill or agent
    python skills/install.py --yes --force               overwrite ones you already have

Existing skills/agents are never overwritten unless --force is given. Nothing is deleted.
Standard library only; works on Windows, macOS and Linux.
"""
import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CLAUDE_HOME = Path.home() / ".claude"


def plan(only: str | None):
    items = []  # (kind, name, source, destination)
    for d in sorted(HERE.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            items.append(("skill", d.name, d, CLAUDE_HOME / "skills" / d.name))
    agents = REPO / "agents"
    if agents.is_dir():
        for f in sorted(agents.glob("*.md")):
            items.append(("agent", f.stem, f, CLAUDE_HOME / "agents" / f.name))
    return [i for i in items if not only or i[1] == only]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually copy (default is a dry run)")
    ap.add_argument("--force", action="store_true", help="overwrite existing skills/agents")
    ap.add_argument("--only", help="install just this skill or agent name")
    args = ap.parse_args()
    items = plan(args.only)
    if not items:
        print("Nothing matched.")
        return 1
    for kind, name, src, dst in items:
        exists = dst.exists()
        verb = "SKIP (exists; use --force)" if exists and not args.force else ("overwrite" if exists else "install")
        print(f"{verb:28s} {kind:6s} {name:22s} -> {dst}")
        if not args.yes or (exists and not args.force):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(src, dst)
    if not args.yes:
        print("\nDry run only. Re-run with --yes to copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
