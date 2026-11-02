# Maintainer guide

These rules apply to maintainers and to the agents that work for them. Contributors follow
[AGENTS.md](../AGENTS.md) and [CONTRIBUTING.md](../CONTRIBUTING.md) only.

To make your own agent follow this page, import it from a personal file that Git ignores, for
example `CLAUDE.local.md` with the line `@docs/maintainers.md`, or `AGENTS.override.md` for
Codex.

## Before editing

- Verify `pwd`, `git status -sb`, the current branch, remotes, and worktrees.
- Start from current `origin/main`, in a fresh worktree when possible.
- Stage explicit paths and preserve unrelated changes.
- Use GitHub CLI. Never run `gh auth refresh` unless the maintainer asks for it.
- If GitHub fails only inside an agent sandbox, retry that command with escalation before you
  report a blocker.

## Approvals

- Merge, deploy, and production writes are separate approvals.
- Do not merge a Dependabot pull request without maintainer approval.
- Squash-merge only after a real review or explicit approval.

## Merge and proof

- Before merging, recheck the head, reviews, threads, checks, `origin/main`, and mergeability.
- After a conflict, rebase on current `origin/main`, validate, then push with
  `--force-with-lease`.
- After a merge, verify the merged head, `main`, the related issue, and safe cleanup.
- Report local, GitHub, workspace, deployed, and live proof separately.

## Live Databricks checks

The maintainer tests on Databricks Free Edition and never uses a paid workspace for testing.
Free Edition compute has a fair-usage quota. Above the quota, compute stops for the rest of the
day. The cost rules in [AGENTS.md](../AGENTS.md#databricks-workspaces-cost-money) apply.

The local live validation is `.act/workflows/databricks-validate.yml`. It needs the
`DATABRICKS_CONFIG_PROFILE` and `DATABRICKS_WAREHOUSE_ID` values that you choose. It validates the
bundle and the dashboard SQL and deploys nothing.

## Releases

Follow [RELEASING.md](../RELEASING.md).
