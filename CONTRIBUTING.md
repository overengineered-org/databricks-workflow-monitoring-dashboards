# Contributing

Use this guide when changing the repository. To configure and deploy the dashboard, use the
[README](README.md) instead.

## 1. Prepare the change

From a fresh clone, create a focused branch:

```sh
git clone https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard.git
cd databricks-jobs-monitoring-dashboard
git switch -c <feat-or-fix>/<short-name>
```

Use `feat/`, `fix/`, `docs/`, or `chore/`. In an existing clone, update from `origin/main`
before creating the branch.

### Tools and agents

You need [uv](https://docs.astral.sh/uv/) and Git. The full gate also needs Docker and
[act](https://nektosact.com). The unit tests need no Databricks workspace.

```sh
uv sync --locked
```

Every coding agent reads [AGENTS.md](AGENTS.md). Codex, Cursor, GitHub Copilot, Jules, Amp, and
Zed read it directly, and `CLAUDE.md` imports it for Claude Code. Keep project rules in
`AGENTS.md` only.

The Codex app also reads `.codex/environments/environment.toml`, which runs the same scripts.

To work in a container, open the repository in a dev container, for example in GitHub Codespaces
or VS Code. `.devcontainer/` installs the pinned uv and Databricks CLI and runs
`uv sync --locked`.

Users of the dashboard need the Databricks CLI, uv, and Git. They run the `workflow-monitoring`
wrapper in `src/workflow_monitoring_cli.py` from their clone with `uv run`.

Before a large behavior, configuration, or dashboard design change:

1. Search existing issues and pull requests.
2. Open one issue describing the change.
3. Keep the pull request focused on that problem.

Small documentation fixes do not need an issue. Never commit credentials, workspace URLs,
warehouse IDs, or organization-specific Job details.

## 2. Edit the owning source

| Change | Owning source |
| --- | --- |
| Public YAML format | `src/workflow-monitoring.schema.json` |
| Dashboard build | `scripts/build_dashboard.py` |
| Jobs API collector | `src/collect_workflow_monitoring_jobs_api.py` |
| Collector Job | `resources/workflow-monitoring.collector.job.yml` |
| Bundle resources | `databricks.yml` or `resources/` |
| `workflow-monitoring` wrapper | `src/workflow_monitoring_cli.py` |
| Dashboard | `src/dashboards/workflow-monitoring.scaffold.lvdash.json` |
| Dashboard dataset SQL | `src/dashboards/sql/*.sql` |
| Shared dashboard SQL fragments | `src/dashboards/sql/fragments/*.sql` |
| Fixed-date regression SQL | `src/dashboards/sql/regression.sql` |
| Dashboard SQL constants | `scripts/dashboard_constants.py` |
| Vega-Lite chart | `src/visualizations/*.vega.json` |
| User documentation | `README.md` |

Never edit the built dashboard directly. Maintainers change the dashboard scaffold, a dataset SQL
file, or a Vega-Lite file, then run:

```sh
uv run python scripts/build_dashboard.py
```

Commit the built dashboard.

Keep the bundle name, the dashboard resource key `workflow_monitoring`, the collector Job key, and
the Job task keys stable. Adopters deploy with them.

User-facing behavior belongs in the README:

- [SLA schedules](README.md#choose-an-sla-schedule)
- [Production requirements](README.md#6-deploy-to-production)

Tracked configuration and built files must contain fake values only. When dependencies change,
run `uv lock` and commit `pyproject.toml` with `uv.lock`.

## 3. Validate

Run the fast checks for every change. They need only uv:

```sh
scripts/check.sh
```

They check the lockfile, formatting, lint, tests, the built dashboard, and banned characters. The
`validate` check on GitHub runs the same script.

When Docker and `act` are available, also run the full gate. It runs the GitHub workflow in the
repository's fixed Act image:

```sh
scripts/run-local-validation.sh
```

If you cannot run it, say so in the pull request. Live Databricks validation is optional unless a
maintainer asks for it. It must use a profile that the human chose.

## 4. Dependency updates

Dependabot opens grouped weekly pull requests for `uv` and GitHub Actions. The 7-day cooldown
means that only releases at least 7 days old are proposed.

A `jsonschema` or `pyyaml` bump fails `test_collector_environment_pins_match_uv_lock`. Fix it by
pushing the collector Job pin change to the Dependabot branch.

A Databricks CLI bump fails `test_bundle_cli_minimum_matches_wrapper_and_act_pin`. Fix it by
bumping the CLI in `.act/Dockerfile`, the `databricks.yml` minimum, `MINIMUM_DATABRICKS_CLI_VERSION`
in the wrapper, and the README table.

Docker image tools are bumped by hand. Review a major update by its release notes.

## 5. Sign your commits (DCO)

Every commit in a pull request needs a [Developer Certificate of Origin](https://developercertificate.org)
(DCO) sign-off. The sign-off email must equal the commit author email. The `dco` check fails
otherwise.

```sh
git commit -s -m "fix: correct the run date zone"
```

To fix earlier commits in a branch:

```sh
git rebase --signoff origin/main
git push --force-with-lease
```

## 6. Open the pull request

Fill in the pull request template. Add screenshots for dashboard changes.

Use a conventional commit subject of 50 characters or fewer. Pull requests are squash-merged
after approval.

Before review, confirm names are clear, tests cover important failures, built files are current,
tracked values are fake, and documentation matches behavior.

The version-bump merge publishes the release. Maintainers use the [release guide](RELEASING.md).
