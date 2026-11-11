---
name: use-tokens-freely
description: Quiet "away" mode for when the user is asleep or at work. Triggered when the user says "use tokens freely" (also "spend tokens freely", "work while I'm away"). Mutes Jarvis's voice, works through near-term tasks (future-tasks.md) then tech debt (long-term.md) with parallel subagents, verifies everything, commits and pushes (only the repos the user authorized), logs blockers, and ends with a written report plus every open question. Ends when the user says "I'm back".
---

# Use tokens freely (away mode)

Definition: push as many near-term and long-term tasks to completion as the tokens allow, quietly; when the usage limit hits, the usage monitor (jarvis/usage_monitor.py) restarts Jarvis after the reset and Jarvis continues by itself from HANDOFF.md; at the end, report and list ALL open questions. Start: "use tokens freely". Stop: "I'm back".

## Hard limits (never cross, even if a subagent suggests it)
- Stay inside the dev folder that holds your repos (the Jarvis `project_dir`). No installs (Node, gh, packages outside throwaway venvs) without the user's yes: log to blocked.md instead.
- Git: Commit AND push, only in the repos the user has authorized (list them in HANDOFF.md; by default the jarvis repo and the jarvis-workspace repo) and only if the repo already exists with a remote; do NOT `git init`, add or change a remote, or force push; scan for secrets before every commit. One commit per finished, verified task; message ends with the required `Co-Authored-By` trailer; never commit secrets (tokens, .env, `.jarvis_*token*`, phone_qr.png, ntfy topic). If there is no repo, skip commits and say so in the report.
- Destructive shell commands (rm -rf style deletes, force flags, kills, formatting, overwriting without backup) and anything that would prompt: skip, write to <workspace>/blocked.md (what, why, file), keep going.
- Do not edit jarvis/permissions.py (protected) or change the permission mode. Do not touch ~/.claude/settings.json.
- Never mark a task "done" on a subagent's word: dispatch the verifier and re-run the tests yourself. Move items to completed-tasks.md only when built, tested AND confirmed live by the user; otherwise file them under "Built, verified offline, awaiting user confirmation" in away-report.md.
- Do not start work that needs the user: live phone/audio/browser checks, tasks with an unanswered blocking question in open-questions.md, accounts/connectors that need sign-in. Skip them and list them.

## Start
1. Say one short sentence aloud ("Starting use-tokens-freely mode, going quiet."), then mute the voice (below). Record the previous mute state.
2. Write <workspace>/spend-freely-state.json: {"active": true, "started": <iso time>, "was_muted": <bool>, "done": [], "skipped": []}. Add a dated HANDOFF.md entry "away mode started" listing the plan. Keep HANDOFF.md current after EVERY finished step, so an automatic restart resumes exactly (after a restart, if the state file says active, keep going).
3. Read HANDOFF.md, tokens-freely-tasks.md, future-tasks.md, long-term.md, open-questions.md, blocked.md. Build the queue: the READY list in <workspace>/tokens-freely-tasks.md first (in order), then near-term items in future-tasks.md, then long-term items; drop whatever the hard limits exclude or tokens-freely-tasks.md's NEVER-away list names. Items in its GAP list are NOT started (they are blocked); once the blocker is answered in open-questions.md, treat the item as READY. Write the queue into the state file.

## Work loop (until the queue is empty, "I'm back", or nothing is left that doesn't need the user)
- For each item: scout if needed (read-only), write a short brief, dispatch an implementer (disjoint file ownership if several run in parallel; they don't see this conversation, so give goal, paths, constraints, exact return format, "do not commit", "no restarts"), then a verifier, fix findings, re-run the full test suites yourself. Run independent items in parallel. Small, clearly scoped tasks need no further approval: the user pre-approved this scope by starting the mode.
- After each verified item: local commit if allowed, update HANDOFF.md, future-tasks.md / long-term.md notes, and state file.
- Decisions that need the user: do NOT stop. Pick nothing risky; append the question to open-questions.md under the right task with the date, skip that item, move on.
- Stay quiet: no speech, no phone pings except the final one. The usage-limit monitor handles a limit by itself (restart + automatic continue); if the state file says active after a restart, just carry on.
- A Jarvis restart is only for loading new code and needs the memory sweep first; batch it, do at most one per hour, and prefer to do none while away unless a change must be loaded to continue.

## Mute helper (never print the token)
The Jarvis web server listens on 127.0.0.1:47823 by default. On macOS/Linux use curl: `curl -s -X POST http://127.0.0.1:47823/voice -H "X-Jarvis-Token: $(cat ~/.jarvis_web_token)" -H 'Content-Type: application/json' -d '{"muted": true}'`. On Windows (PowerShell):
`$t=(Get-Content ~/.jarvis_web_token -Raw).Trim(); Invoke-RestMethod -Method Post -Uri http://127.0.0.1:47823/voice -Headers @{'X-Jarvis-Token'=$t} -ContentType 'application/json' -Body '{"muted": true}'` (use false to unmute). If the page route fails, say so in the report; typing "mute" as a turn also works.

## Stop ("I'm back", queue empty, or nothing left)
1. Let in-flight subagents finish or collect them; start nothing new.
2. FIRST clean <workspace>/open-questions.md: move every question that has already been answered, decided, built or proven live out of each `Open:` list into that section's `Answered:` list (stamped with the date), so the report never makes the user revisit settled questions; only genuinely open questions go into the report. Then write <workspace>/away-report.md: finished and verified (with test counts and what was NOT verified), built but awaiting the user's live confirmation, local commits made (hashes, how to review them), skipped/blocked with reasons, tokens/subagents used if known, and ALL open questions from open-questions.md grouped by task.
3. Restore the voice to its previous mute state (usually unmute). If the user said "I'm back", speak a 3-to-5 sentence summary and offer to walk through it; otherwise send ONE phone push ("Away mode finished: N tasks done, M questions waiting") via jarvis/notify.py (respect JARVIS_NOTIFY_PHONE) and speak the summary on the user's next message.
4. Set "active": false in the state file and add the HANDOFF.md entry.
5. WALKTHROUGH: the report must be a Markdown file (away-report.md) with clear `##`/`###` headings, and when the user wants to go over it, walk through it IN VS CODE with mcp__jarvis__show_code: one section at a time, highlight only the exact lines being discussed (grep the headings with line numbers first), say one short sentence before each call, explain in plain spoken sentences, pause for the user's reaction before the next section. Structure the report in this order so the walkthrough flows: finished and verified, awaiting live confirmation, local commits, skipped/needs you, observations, open questions.

## Notes
- This skill needs no model-free parts: if the user says "I'm back" mid-run, treat it as a stop request at the next safe point (finish the current verification step, then wrap up).
- Related files: jarvis-workspace\future-tasks.md, long-term.md, open-questions.md, blocked.md, HANDOFF.md, completed-tasks.md; jarvis/usage_monitor.py; the intentional-restart continue (jarvis/restart.py).
