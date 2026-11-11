# Contributing to Jarvis

Thanks for thinking about it. Jarvis is a hobby project that grows when people share what they built on top of it.
You do not need to be an expert, and you do not need to know Git well: your Claude can do the mechanical parts.

## Why contribute

If you made a skill, agent or helper script that saved you time, it will probably save someone else time too, and
others will improve it in return. Small contributions are welcome.

## What is welcome

- **Skills** (a folder with a `SKILL.md`), **agents** (a single Markdown file) and small **helper scripts** that go with them.
- Documentation fixes and clearer setup steps, especially for macOS and Linux (the author built this on Windows).
- Bug fixes, with a test where it makes sense (see the `tests/` folder).
- Ideas: open an issue (the "Skill idea" form) if you do not want to build it yourself.

## The simple path

Tell your Claude: **"share this skill"**. The `contribute-skill` skill (`skills/contribute-skill/SKILL.md`, installed
by `python skills/install.py --yes`) takes over: it checks the skill works, removes personal details, runs a scanner
for secrets and private paths, shows you exactly what will become public, and only with your yes at each step forks the
repo, pushes a branch and opens a pull request. A pull request is a polite proposal that the maintainer reads and
accepts or declines; nothing merges automatically.

Prefer to do it by hand? Fork, add `skills/<name>/` (plus one line in `skills/README.md`), run
`python skills/contribute-skill/scripts/scan_contribution.py skills/<name>`, and open a pull request using the
template in `.github/PULL_REQUEST_TEMPLATE.md`.

## What reviewers look for

- **It works**, and you say honestly what you tested and on which system ("not tested" is fine).
- **Portable, or honest about the OS.** Python 3 standard library where possible; if it is Windows-only or macOS-only,
  say so at the top and suggest equivalents.
- **Small and focused.** One skill per pull request; nothing outside `skills/<name>/` except one listing line.
- **Safe.** No secrets, personal paths, hostnames or emails. No hidden network calls, and no destructive commands
  without clear disclosure and a confirmation.
- **Documented.** A clear description (so Claude knows when to use it) and an example.
- **Tested** where there is code to test.

## What will be declined

Anything containing secrets or other people's private data; code that sends data out or deletes things without
disclosure; copyrighted material you do not have the right to share; large or binary files; unrelated changes bundled
in; and changes to `jarvis/permissions.py` (the auto-approve policy), which is protected unless the maintainer asks.

## License

Jarvis is MIT licensed (see `LICENSE`). By contributing you agree your contribution is released under the same license.

## Be kind

Be respectful and patient, assume good intent, and keep feedback about the work, not the person. First-time
contributors are especially welcome. The maintainer may decline or ask for changes; that is part of the process, not a
judgement of you.
