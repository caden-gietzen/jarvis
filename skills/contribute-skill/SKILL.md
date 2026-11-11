---
name: contribute-skill
description: Helps the user share a skill, agent or helper script they made by proposing it to the public Jarvis repo as a pull request. Use when the user says "share this skill", "contribute this", "send this upstream", "make a pull request for my skill", or after you offered to share something and they said yes. Checks it works, removes personal details, runs a secret scanner, shows the exact public diff, and only forks, pushes or opens a pull request after the user says yes at each step.
---

# Contribute a skill (or agent, or script) to Jarvis

Upstream repo: `caden-gietzen/jarvis` on GitHub. Work in plain language: many users have never made a pull request.
Everything here works on Windows, macOS and Linux; use `python3` on macOS/Linux and `python` (or `py`) on Windows.

**Golden rule: nothing leaves the user's machine without their explicit yes.** Ask separately before forking, before
pushing and before opening the pull request. A yes to one step is not a yes to the next.

## 0. Explain what a pull request is (first-timers)

Say it in a few sentences, before anything else: a pull request is a polite proposal: "here is a change, would you
like it in your project?" The maintainer reads it, may comment or ask for changes, and then accepts or declines.
Nothing is merged automatically. What the user submits becomes PUBLIC (anyone can read it, and the history is
permanent), which is why we clean and review it first. They can stop at any point.

## 1. Confirm what is shared, and that it is theirs

- Which skill, agent (a single `.md` in `~/.claude/agents/`) or helper script? One thing per pull request.
- Ask: "Did you write this yourself (with Claude's help is fine), and are you OK with it being published under the
  project's MIT license?" If it contains someone else's code, text or data, see "What Claude must refuse" below.
- Not a skill (a bug report, a feature idea, a question)? Do not build a pull request: offer to open an issue instead
  (`.github/ISSUE_TEMPLATE/skill-idea.md` is the form for skill ideas), after the same sanitizing and a yes.

## 2. Locate it

Usually `~/.claude/skills/<name>/` (skill), `~/.claude/agents/<name>.md` (agent) or a script the skill calls. Read every
file that would be shared, fully, and list them for the user. Pick a short lowercase-with-dashes name that is not
already in this repo's `skills/` folder. If the user has a clone of the repo, use it for the fork step below.

## 3. Make sure it works (or say honestly that it does not)

Run it, or run its script with a harmless example, and note the real result (OS, versions, command, output). If you
cannot run it (needs hardware, an account, another OS), write "not tested" in the pull request: honest is better than
guessing. Do not run anything destructive just to test; ask first.

## 4. Sanitize

Open each file and fix, by hand, with the user:

- Personal paths (home folders such as a Windows user folder, `/Users/<name>`, `/home/<name>`): use `~`, an environment
  variable, or a path relative to the skill. Same for the user's own workspace folders.
- Usernames, real names, emails (use an obvious example address), hostnames, IP addresses (LAN or tailnet), `*.ts.net` / `*.local`
  names, phone-push topics, webhook URLs.
- Tokens, passwords, keys, `.env` content; also anything read from the user's token files. Replace with a documented
  environment variable the user must set.
- Private notes, task lists, chat content, anything copied out of the user's own workspace or other projects.
- OS-specific assumptions: a hard-coded Windows path (drive letters, AppData) or macOS path (`/Applications`,
  `~/Library`) is fine only if the skill says "Windows only" / "macOS only" at the top and, ideally, suggests an
  equivalent. Otherwise make it portable (`pathlib`, `os.path`, `Path.home()`, `sys.platform` checks).
- Scripts should be Python 3 standard library only where possible, or say what they need.

The user's own known secrets can be checked without ever writing them into the repo: put them one per line in a file
OUTSIDE the repo (for example `~/.contrib-flag.txt`) and pass `--also-flag-file` in step 5. Never print or copy those
values yourself.

## 5. Run the scanner

Put the contribution in a staging folder (for example `skills/<name>/` inside a clone of the repo) and run:

```
python3 ~/.claude/skills/contribute-skill/scripts/scan_contribution.py skills/<name> [--also-flag-file ~/.contrib-flag.txt]
```

(Windows: `python` instead of `python3`; inside a clone you can use `skills/contribute-skill/scripts/...`.) Later, to
check everything a branch changes: `python3 .../scan_contribution.py origin/main --expect skills/<name>/`.

It prints `file:line`, the rule and a redacted snippet, and ends with a `SCAN RESULT:` line. Exit code 0 means no
blocking findings. Fix every blocking finding and re-run until clean; review every warning (OS-specific paths,
long random-looking strings). A reviewed false positive can go in `.contribution-allow` (`rule path[:line]  # reason`)
and be honoured with `--allow`; list each one in the pull request, and do not commit that file. Private keys and
the user's own flagged strings can never be allowed. A clean scan is a safety net, not proof: still read the files.

## 6. Show exactly what becomes public, get a yes

Show the user, in full: the list of files and the complete diff (`git diff --stat` then `git diff`, plus the new files'
contents). Say plainly: "This is exactly what anyone on the internet will be able to read." Ask for an explicit yes.
Any edit afterwards means re-running the scanner and showing the diff again.

## 7. Fork, branch, add, push, open the pull request (one yes per outward step)

Check `gh --version` and `gh auth status`. With the GitHub CLI, after a yes for each step:

1. Fork and clone: `gh repo fork caden-gietzen/jarvis --clone` (a fork is the user's own copy on their GitHub account).
2. Branch: `git switch -c add-<name>`.
3. Add the files under `skills/<name>/` (`SKILL.md` and any scripts; agents go to `agents/<name>.md`). Add one short
   row to the table in `skills/README.md`. Nothing else: no unrelated edits, no reformatting.
4. Re-run the scanner on the branch, then commit with a plain message ("Add <name> skill"). Use the user's own git
   identity; if none is configured, ask what name and email to use (a GitHub no-reply address is a good choice).
5. Push to the fork (yes first): `git push -u origin add-<name>`.
6. Open the pull request (yes first): fill `.github/PULL_REQUEST_TEMPLATE.md` honestly (what was really tested, what
   was not, the scanner summary line, the AI disclosure) and run
   `gh pr create --repo caden-gietzen/jarvis --base main --title "Add <name> skill" --body-file <filled-template>`.
   Show the user the description before sending it.

Without `gh`: offer to install it (https://cli.github.com, ask first), or give these web steps: 1) open
https://github.com/caden-gietzen/jarvis and click Fork; 2) in the fork press "Add file", then "Upload files" (or use git
on the command line), put the files in `skills/<name>/` and choose "Create a new branch" when committing;
3) GitHub then shows "Compare & pull request": click it, fill in the template, and press "Create pull request".

After opening: give the user the PR link, tell them the maintainer will review it (it may take a while, there may be
requests for changes, nothing merges automatically) and that they can close it whenever they like. If changes are
requested, make them on the same branch, re-scan, show the diff, and push only after a yes.

## What Claude must refuse

Say no, kindly and briefly, and explain why; offer a safe alternative where there is one:

- Secrets of any kind: keys, tokens, passwords, push-notification topics, session cookies, `.env` files, private keys.
  Replace them with documented environment variables.
- Other people's private data: chat logs, emails, contacts, customer or employer data, someone else's notes.
- Anything that sends the user's data somewhere (exfiltration), or runs destructive commands (deleting files, force
  pushes, formatting disks, killing processes) without clear, prominent disclosure and a confirmation prompt inside the
  skill itself. Hidden network calls and obfuscated code are never acceptable.
- Copyrighted material that cannot be licensed under MIT: copied book text, paid course content, someone else's code
  without a compatible license (and attribution).
- Large or binary files (over about 1 MB, images, audio, models, archives, installers): skills are text.
- Skipping the user's yes at any step, or forking, pushing or opening a pull request on your own.
- Touching the user's real repositories or the upstream `main` branch directly: all work goes through the fork.

If the scanner keeps finding things the user insists are fine, do not argue it away: stop, and suggest they ask the
maintainer in an issue.
