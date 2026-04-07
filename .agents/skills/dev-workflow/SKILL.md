---
name: dev-workflow
description: "Use when making any code change in scoring-pipeline — from first edit through merged PR. Covers: branch setup, lint, tests, commit, push, PR creation, PR self-review, and post-merge cleanup."
---

# Dev Workflow — scoring-pipeline

Use this skill whenever making code changes — from first edit through merged PR.

> Full workflow defined in `dev-workflow.md` at repo root. This skill is the invocable entry point.

---

## Quick Reference

| Step | Action | Command |
|------|--------|---------|
| 1 | Confirm state | `git branch --show-current && git status` |
| 2 | Create branch | `git checkout -b <type>/<description>` |
| 3 | Make changes | Edit files incrementally |
| 4 | Lint | `ruff check .` |
| 5 | Tests | `pytest tests/ -v` (if present) |
| 6 | Stage + commit | `git add <files> && git commit` |
| 7 | Push + PR | `git push -u origin <branch> && gh pr create` |
| 8 | Self-review | `gh pr diff` — line-by-line check |
| 9 | Merge + cleanup | `gh pr merge --squash --delete-branch` |

---

## Branch naming

```
feat/anomaly-scorer-mcd
fix/dedup-period-features
refactor/scorer-z-score
chore/update-feature-keys
docs/scoring-methodology
test/scorer-edge-cases
```

---

## Commit message format

```
<type>(<scope>): <imperative description under 72 chars>

<optional body: why this change was needed>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

**Scopes for this project:** scorer, flatten, review-pack, features, upload, scripts, output

---

## PR body template

```
## What changed
- <specific change 1>
- <specific change 2>

## Why
<one sentence on the motivation>

## Test plan
- [ ] `ruff check .` passes with zero warnings
- [ ] `pytest tests/` passes
- [ ] Pipeline runs end-to-end on sample data
- [ ] Output CSV schema unchanged (or describe changes)
- [ ] No regressions in downstream consumers (redink-ui, qqq-eval-suite)

## Schema / API changes
None  (or describe breaking changes to quarterly_scores_detailed.csv or top_anomaly_review_pack.csv)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## Self-review checklist (Step 8)

Before merging:

- [ ] Branch created — not committing directly to main
- [ ] `ruff check .` passes with zero errors
- [ ] `pytest tests/ -v` passes (if tests present)
- [ ] No hardcoded local paths or credentials
- [ ] No debug `print()` statements left in
- [ ] Output CSV schema still matches `redink-ui/data/` format
- [ ] Commit message is accurate to what changed
- [ ] PR description matches the actual diff

---

## Post-merge (Step 9)

```bash
git checkout main
git pull origin main
git branch -d <branch-name>
```

**If output format changed:**
- Copy new CSVs to `redink-ui/data/` and re-deploy
- Re-run: `python3 scripts/build_review_pack.py --upload-gcs`

---

## For code review (before merge)

Use `/superpowers-reviews` → "Requesting Code Review" workflow:

```bash
BASE_SHA=$(git rev-parse origin/main)
HEAD_SHA=$(git rev-parse HEAD)
# Then dispatch code-reviewer subagent with template at .agents/skills/superpowers-reviews/code-reviewer.md
```
