# Agent guide

This file is for every coding agent and every human contributor. Codex, Cursor, GitHub Copilot,
Jules, Amp, Zed, and other tools read `AGENTS.md` directly. `CLAUDE.md` imports it for Claude
Code. Keep project rules here, in one copy.

[CONTRIBUTING.md](CONTRIBUTING.md) explains the same workflow for humans.
[docs/maintainers.md](docs/maintainers.md) holds extra rules for maintainers. Contributors do not
need it.

## Project

This repository is a Databricks bundle. A Python collector polls the Jobs API into Delta tables,
and an AI/BI dashboard reads them. The `workflow-monitoring` wrapper in
`src/workflow_monitoring_cli.py` helps adopters tick Jobs and deploy.

| Path | Content |
| --- | --- |
| `databricks.yml`, `resources/` | Bundle definition |
| `workflow-monitoring.yml` | Monitored Jobs. Upstream, it lists no Jobs. |
| `src/` | Collector, config rules, schema, wrapper, dashboard sources |
| `scripts/` | Dashboard build, checks, release |
| `tests/` | Unit tests. They need no Databricks workspace. |

## Setup

```sh
uv sync --locked
```

## Checks

Run the fast checks for every change. They need only uv:

```sh
scripts/check.sh
```

The full gate runs the GitHub workflow in Docker with `act`. Run it when Docker and `act` are
available:

```sh
scripts/run-local-validation.sh
```

GitHub runs the same workflow on each pull request. If you cannot run the full gate, say so in the
pull request. Never claim a check that you did not run.

## Project rules

### Source files

- Use fake account values in all tracked files: no workspace URL, workspace ID, warehouse ID,
  profile, token, or real Job ID.
- Edit the dashboard scaffold, a dataset SQL file, or a Vega-Lite file, then run
  `uv run python scripts/build_dashboard.py`. Never edit `src/dashboards/workflow-monitoring.lvdash.json`
  by hand.
- Keep repository metadata aligned with `.github/repository-metadata.yml`.

### Language

- Use simplified technical English: short sentences, active voice.
- Comment non-obvious intent and constraints only.
- Do not use em dashes in code, comments, documentation, or commit messages.

### Databricks workspaces cost money

- Run only live commands that start no compute, for example `databricks bundle validate` and
  read-only list or get calls.
- Do not deploy a bundle, run a Job or notebook, or start a SQL warehouse unless the human asks
  for that command in the current request.
- Never pick a Databricks profile. Ask the human, or use the one they named.
- Keep live SQL validation read-only. Do not create tables or insert test rows.
- Report each skipped live check as not run, with the reason.

### GitHub Actions cost guardrails

- Every job uses exactly `runs-on: ubuntu-latest`. Never use a larger runner, a runner group,
  labels, macOS, Windows, a GPU runner, or a self-hosted runner.
- Every workflow sets `permissions: contents: read`, and every job sets `timeout-minutes: 15`.
  The `release` job in `release.yml` may set `contents: write`.
- Use `concurrency` with `cancel-in-progress: true`. `release.yml` uses `false`, so a publish
  never stops halfway.
- Never use secrets, `pull_request_target`, artifact uploads, paid Marketplace actions, or
  third-party services.
- Pin every `uses:` line to a full commit SHA, with the version in a comment.
- Guard each tool install step with `if: ${{ !env.ACT }}`, so local `act` runs keep the fixed
  image.
- `runner-guard` is the first job in each workflow. Every other job has `needs: runner-guard`.
- Live Databricks checks never run on GitHub. `.act/workflows/databricks-validate.yml` is local.

## Changes and pull requests

- Work on a branch named `feat/`, `fix/`, `docs/`, or `chore/`. Never push to `main`.
- Sign off every commit with `git commit -s` (Developer Certificate of Origin). The `dco` check
  fails without it.
- Use a conventional commit subject of 50 characters or fewer.
- Fill in the pull request template.

### Scope

- Change only what the issue or the request asks for.
- Do not rename or move anything that deployed resources or adopters depend on: the bundle name,
  resource keys, Job task keys, Job and dashboard names, tables, columns, CLI flags, config
  fields, and file paths that resources reference. Propose such a change in the pull request
  body instead.
- List every change that the request did not name, with its reason, or write "None".
- When a change touches `databricks.yml` or `resources/`, list the deployed fields that change.

### Quality

- Diagnose first, then make the smallest durable change that fully meets the requirement.
- Use established libraries and tools. Do not add compatibility layers or unfinished stopgaps.
- Keep names intention-revealing and domain-specific.
- Trace each requirement from input to stored output and its consumers, and map each acceptance
  criterion to a check.
- Before you delete code, check callers, dynamic entry points, generated files, tests, and docs.
- Separate tool or environment failures from code failures, and never claim stronger proof than
  the evidence.
