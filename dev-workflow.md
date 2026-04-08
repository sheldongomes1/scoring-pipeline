# Skill: Dev Workflow

Use this skill whenever making code changes in any project — from first edit through merged PR.
Covers: branch setup, lint, unit tests, integration tests, commit, PR creation, PR review, and post-merge cleanup.

---

## Step 1: Confirm Starting State

Before touching any code, run:

```bash
git branch --show-current   # what branch am I on?
git status                  # any uncommitted work already present?
git log --oneline -5        # recent commit context for message style
```

**If on `main` or `master`:** always create a feature branch first — never commit directly to main.
**If dirty working tree:** stash or commit existing work before starting new changes.

---

## Step 2: Create a Feature Branch

Name the branch to describe the change, not the ticket:

```bash
git checkout -b <type>/<short-description>
# Examples:
# feat/anomaly-detail-panel
# fix/duplicate-rows-dedup
# refactor/scorer-mcd-distance
# chore/update-feature-keys
```

Branch types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`

---

## Step 3: Make Changes

Work incrementally. After each logical chunk of work (not necessarily the whole feature):
- Run lint (Step 4) to catch problems early
- Run tests (Step 5) to confirm nothing broke

**Project-specific test and lint commands:**

| Project | Lint | Unit Tests | Integration Tests |
|---|---|---|---|
| `redink-ui` | `npm run lint` | `npm test` | Open `index.html` in browser, verify filter/panel/review state |
| `scoring-pipeline` | `ruff check src/ scripts/` (if configured) | `pytest tests/` (if present) | `python3 scripts/score_quarterly_anomalies.py` on sample data |
| Any Python project | `ruff check .` or `flake8` | `pytest` | Run main entry point end-to-end |
| Any Node project | `npm run lint` | `npm test` | `npm run build` or manual smoke test |

For `redink-ui` specifically, also follow the CLAUDE.md prototype-first rule: build in `prototype.html`, get approval, then copy to `index.html`.

---

## Step 4: Lint

Run lint before every commit. Zero warnings tolerance for new code.

```bash
# redink-ui
npm run lint

# Python (scoring-pipeline, qqq-eval-suite, etc.)
ruff check .
# or
flake8 src/ scripts/
```

**If lint fails:** fix all errors and warnings before proceeding. Do not bypass with `// eslint-disable` or `# noqa` unless there is a specific, documented reason.

---

## Step 5: Run Tests

**Unit tests** — run after every change to core logic files:

```bash
# redink-ui (Vitest + Chai)
npm test

# Python projects
pytest tests/ -v
```

**Integration tests** — run before committing any change that touches data flow, scoring, or UI rendering:

- **redink-ui:** Load `index.html` in a browser. Verify: list renders, filters work, detail panel loads, review state persists after refresh, filing URL opens.
- **scoring-pipeline:** Run `python3 scripts/score_quarterly_anomalies.py` end-to-end. Verify row count, column schema, top anomaly values look reasonable.
- **Any pipeline:** Run the full pipeline on a small sample and inspect output format.

**If tests fail:** fix before committing. Never commit broken tests with a "will fix later" note.

---

## Step 6: Stage and Commit

Stage specific files — never `git add .` or `git add -A` (risks committing secrets, build artifacts, or unrelated changes):

```bash
git add src/utils.js src/utils.test.js   # example — be explicit
git diff --staged                         # review exactly what is staged
```

Write the commit message:

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <imperative short description under 72 chars>

<optional body: why this change was needed, what problem it solves>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

**Commit message rules:**
- Use imperative mood: "add", "fix", "remove" — not "added", "fixes", "removed"
- `<type>` matches branch type: feat / fix / refactor / chore / docs / test
- `<scope>` is the module or area: scorer, review-pack, detail-panel, filters
- Body explains *why*, not *what* (the diff shows what)

**Examples:**
```
feat(scorer): replace L2 norm with MCD Mahalanobis distance

The old L2 norm ignored feature correlations. MCD gives a statistically
correct distance and produces values comparable to the redink-ui output.

fix(dedup): drop duplicate ticker+report_date rows before scoring

period_features.json had 5,445 rows for 1,371 unique filings due to GCS
re-reads. Dedup at load time prevents inflated z-score distributions.
```

---

## Step 7: Push and Create PR

```bash
git push -u origin <branch-name>
```

Then create the PR with `gh`:

```bash
gh pr create --title "<type>(<scope>): short description" --body "$(cat <<'EOF'
## What changed
- Bullet 1: specific change
- Bullet 2: specific change

## Why
One sentence on the motivation or problem solved.

## Test plan
- [ ] `npm test` passes (or `pytest` passes)
- [ ] `npm run lint` passes with zero warnings
- [ ] Manually verified: [describe what you clicked/ran]
- [ ] No regressions in [affected area]

## Schema / API changes
None  (or describe breaking changes)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**PR title must match the commit message format:** `<type>(<scope>): description`

---

## Step 8: PR Self-Review

Before requesting review (or before merging if solo), read the diff yourself:

```bash
gh pr diff
# or
git diff main..HEAD
```

Check every line against this list:

- [ ] Does the change do exactly what the PR description says — nothing more?
- [ ] Are there any hardcoded values, credentials, or local paths that slipped in?
- [ ] Are new functions named clearly and placed in the right module?
- [ ] Do tests cover the new logic — not just the happy path?
- [ ] Are there any `console.log`, `print`, or debug statements left in?
- [ ] Does the lint pass clean with no suppressions?
- [ ] Is the commit message accurate to what actually changed?
- [ ] For `redink-ui`: does the UI still render correctly with `top_anomaly_review_pack.csv` loaded?
- [ ] For `scoring-pipeline`: does the output CSV schema still match `redink-ui/data/` format?

If `/superpowers-reviews` is available, dispatch it with the diff range for a second-pass code review before merging.

---

## Step 9: Merge and Cleanup

After PR is approved (or self-approved after review):

```bash
gh pr merge <PR-number> --squash --delete-branch
# or merge via GitHub UI with "Squash and merge"
```

Then sync local:

```bash
git checkout main
git pull origin main
git branch -d <branch-name>   # delete local branch if not already gone
```

**After merging to main in redink-ui:**
- GitHub Actions `deploy.yml` will auto-deploy to Firebase Hosting
- Verify the deploy succeeded: `gh run list --limit 3`
- Do a quick smoke test on the live URL

**After merging scoring-pipeline changes:**
- If output format changed, copy new CSVs to `redink-ui/data/` and re-deploy
- If GCS outputs are stale, re-run: `python3 scripts/build_review_pack.py --upload-gcs`

---

## Self-Check Before Calling Any Change "Done"

- [ ] Branch created (not committing directly to main)
- [ ] Lint passes with zero errors
- [ ] All tests pass
- [ ] Manual integration smoke test done
- [ ] PR created with description and test plan
- [ ] Diff reviewed line-by-line
- [ ] Merged and branch cleaned up
- [ ] Post-merge verification done (deploy, downstream files updated if needed)

If any box is unchecked, the change is not done.
