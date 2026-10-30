# Contributing

Use this guide when changing the repository. To configure and deploy the dashboard, use the
[README](README.md) instead.

## 1. Prepare the change

From a fresh clone, create a focused branch:

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
git switch -c <feat-or-fix>/<short-name>
```

Use `feat/`, `fix/`, `docs/`, or `chore/`. In an existing clone, update from `origin/main`
before creating the branch.

### Local agent setup (macOS and Linux)

Codex reads `AGENTS.md`. `CLAUDE.md` imports that same file for Claude Code, including versions
that do not load `AGENTS.md` directly. Start either agent from the repository root. Keep project
rules in `AGENTS.md`, not in two separate copies.
The Codex project environment is defined in `.codex/environments/environment.toml`. It runs
`uv sync --locked` when Codex creates a worktree and exposes Bootstrap, Smoke checks, and Full
local gate actions. The full gate uses the same wrapper as section 3.

The local checks need the Python and `uv` versions in [README step 1](README.md#1-clone-and-prepare)
and Go 1.25 or newer. The required local Act gate also needs Git, `act`, and a running Docker
daemon. Start Docker Desktop on macOS or the Docker daemon on Linux before running the gate.
The Act image supplies `golangci-lint`, `jq`, and the pinned Databricks CLI; they are not host
setup requirements for this gate.

From the repository root, prepare the ignored Python environment and check the other local
prerequisites:

```sh
uv sync --locked
go version
act --version
docker info >/dev/null
```

Run the smoke checks and full gate in [section 3](#3-validate). No Databricks workspace login or
profile is needed for these local checks. Live Databricks validation is separate and requires a
profile chosen by the user. If Docker or `act` is unavailable, repair it and report the local
gate as not run. If no profile is chosen, report live validation as not run; never select one
automatically.

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
| Configuration CLI | `cmd/workflow-monitoring/` |
| Dashboard build | `scripts/build_dashboard.py` |
| Jobs API collector | `src/collect_workflow_monitoring_jobs_api.py` |
| Collector Job | `resources/workflow-monitoring.collector.job.yml` |
| Bundle resources | `databricks.yml` or `resources/` |
| Dashboard | `src/dashboards/workflow-monitoring.scaffold.lvdash.json` |
| Vega-Lite chart | `src/visualizations/*.vega.json` |
| User documentation | `README.md` |

Never edit the built dashboard directly. The collector Job is a normal bundle file. Maintainers
change the dashboard scaffold or a Vega-Lite file, then run:

```sh
uv run python scripts/build_dashboard.py
```

Commit the built dashboard.

User-facing behavior belongs in the README:

- [Storage modes](README.md#3-create-the-configuration)
- [SLA schedules](README.md#choose-an-sla-schedule)
- [Production requirements](README.md#before-production)

Tracked configuration and built files must contain fake values only. When dependencies change,
run `uv lock` and commit `pyproject.toml` with `uv.lock`.

## 3. Validate

Use targeted checks while editing:

```sh
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests -v
go test ./...
```

Before opening a pull request, run the required local gate:

```sh
scripts/run-local-validation.sh
```

The wrapper uses the repository's fixed Act image and retained checkout-scoped container.
The `validate` check must pass on each pull request.

Live Databricks validation is optional unless a maintainer requests it. Every live command must
use the selected profile as `--profile <name>`.

## 4. Sign your commits (DCO)

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

## 5. Open the pull request

Include:

- Why the change is needed
- What changed
- Exact validation results
- Screenshots for dashboard changes
- Anything not validated

Use a conventional commit subject of 50 characters or fewer. Pull requests are squash-merged
after approval.

Before review, confirm names are clear, tests cover important failures, built files are current,
tracked values are fake, and documentation matches behavior.

Publishing a version is separate. Maintainers use the [release guide](RELEASING.md).
