# Skills and agents shared with this repo

These are the Claude Code skills and sub-agents the author uses with Jarvis, with personal paths and names removed.
A skill is a folder with a `SKILL.md`; Claude Code loads it from `~/.claude/skills/<name>/` and uses it when a request
matches its description. Agents are single Markdown files in `~/.claude/agents/`.

| Name | What it does | Platform |
|---|---|---|
| `tile-windows` | Tiles two app windows (default Chrome and VS Code) side by side on the second monitor | Windows only (PowerShell + Win32); macOS/Linux ideas are in its SKILL.md, untested |
| `use-tokens-freely` | "Away mode": mutes Jarvis and works through your task lists with parallel agents, then writes a report. Expects a `jarvis-workspace` folder with `HANDOFF.md` etc. (see the jarvis-workspace repo, `templates/`). | all (mute helper has a curl version) |
| `../agents/scout, implementer, verifier` | The read-only investigator, the code-changing agent and the test/review agent used by the orchestrator workflow | all |
| `../agents/paper-finder` | Finds papers on a topic for a walkthrough; edit its "Default focus" paragraph to your own field | all |
| `orchestrator-CLAUDE.md.example` | The "orchestrator mode" instructions the author keeps in his global `~/.claude/CLAUDE.md`. A starting point, not a requirement. | all |

## Share your own

Made a skill others might like? Add it as `skills/<name>/SKILL.md` plus a row in the table above and open a pull request (your Claude can walk you through it: `SETUP.md` section 17).

## Install

    python skills/install.py            # dry run: shows what would be copied where
    python skills/install.py --yes      # copy skills to ~/.claude/skills and agents to ~/.claude/agents
    python skills/install.py --yes --only tile-windows

Existing skills/agents with the same name are skipped unless you add `--force`. Restart Claude Code (or start a new
session) so it notices the new skills. The originals on the author's machine are never touched by any of this.

`orchestrator-CLAUDE.md.example` is not installed automatically because it would change how every Claude Code session
behaves. If you want it, read it and merge the parts you like into your own `~/.claude/CLAUDE.md`.

## Check a skill is installed

Start Claude Code and ask "what skills do you have?", or run `/skills`; you should see the names above.
