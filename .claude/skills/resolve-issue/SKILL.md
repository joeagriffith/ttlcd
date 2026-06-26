---
name: resolve-issue
description: Resolve an open GitHub issue in the ttlcd-panel repo end-to-end — implement the fix/feature, pass a review+test gate, then commit, push, and restart the daemon. Use when processing the ttlcd issue queue (auto-fix loop) or fixing a specific ttlcd issue by number. Project-local maintenance workflow; not for using the panel.
---

# resolve-issue — ttlcd auto-fix workflow

A runbook for resolving **one** GitHub issue on `joeagriffith/ttlcd` autonomously
and safely. Process issues one at a time. Repo: `/home/joe/projects/ttlcd`
(work from there; venv at `.venv`; `gh` is authenticated; daemon managed by `ttlcd`).

## 0. Preconditions
- `cd /home/joe/projects/ttlcd`. Confirm `gh auth status` is logged in.
- Baseline must be clean: `git status` shows no uncommitted changes, and
  `.venv/bin/python -m pytest -q` is green. If the baseline is dirty/red, STOP
  and report — do not build on a broken base.

## 1. Pick the issue
- If an issue number was given (e.g. `$ARGUMENTS`), use it.
- Else choose the oldest open issue **not** already labelled `in-progress`,
  `needs-human`, or `needs-info`:
  ```bash
  gh issue list --repo joeagriffith/ttlcd --state open \
    --search "-label:in-progress -label:needs-human -label:needs-info" \
    --json number,title,createdAt --jq 'sort_by(.createdAt) | .[0]'
  ```
- If none, STOP (nothing to do).
- **Claim it** so the loop can't double-process: `gh issue edit N --add-label in-progress`.

## 2. Understand & plan
- `gh issue view N --comments`. Restate the problem/request in your own words.
- If it's a bug, reproduce it first (write a failing test or a repro script).
- If the issue is **underspecified, ambiguous, or risky** (destructive ops,
  security-sensitive, unclear acceptance criteria): comment asking the specific
  question, `gh issue edit N --add-label needs-info --remove-label in-progress`,
  and STOP. Do not guess.

## 3. Implement
- Make the **smallest focused change** that resolves the issue. Match existing
  conventions (see `docs/ARCHITECTURE.md`). Touch only what's necessary.
- Add or update **tests** (every bug fix gets a regression test) and any affected
  **docs** (README / docs/ / SKILL.md). Keep the diff reviewable.

## 4. GATE — must fully pass before shipping
1. **Tests:** `.venv/bin/python -m pytest -q` → all green.
2. **Review:** run the `/code-review` skill (high effort) on the working diff, or
   spawn a reviewer subagent over `git diff`. The gate **passes only if** there
   are no high- or medium-confidence findings. Fix anything it flags and
   re-review until clean (max ~2 rounds).
- If the gate cannot be made to pass: see step 6 (do NOT ship).

## 5. Ship (only after the gate passes)
```bash
git add -A
git commit -m "Fix #N: <concise summary>"     # use "Implement #N:" for features
git push
ttlcd restart                                  # reload the daemon on the new code
# verify the panel came back:
curl -s --max-time 2 localhost:8770/health     # expect "panel":"running" (wait ~20s)
```
End with the Co-Authored-By trailer on the commit per the repo norm. Then close
the issue with a summary + the commit SHA:
```bash
gh issue close N --comment "Fixed in <sha>: <what changed>. Daemon restarted; panel verified streaming."
```
(`Fix #N` in the message also auto-links the commit.)

## 6. If you can't resolve it
- Do NOT push broken or unreviewed code. Restore a clean tree:
  `git restore -SW . && git clean -fd` (or `git stash`) so the next run starts clean.
- Comment on the issue with what you tried and the blocker, then
  `gh issue edit N --add-label needs-human --remove-label in-progress`. STOP.

## Guardrails
- One issue per run. Never push if tests or review fail. Never `git push --force`.
- Keep changes minimal and on-topic; don't bundle unrelated refactors.
- If a fix would change the daemon's USB/driver behaviour, be extra conservative
  (the panel is live) — prefer adding tests and `--no-panel` validation; a bad
  driver change can wedge the hardware.
- Always leave the working tree clean and the daemon running at the end.

## How this is invoked
Designed to be driven by a polling loop, e.g.:
`/loop 5m resolve the next open ttlcd GitHub issue using the resolve-issue skill`
or invoked directly for one issue: `/resolve-issue 42`.
