# Contributing

Run the setup commands below before editing. Setup takes about 2 minutes after the tools are
installed.

## 1. Set up the repository

You need Python 3.11 or newer, `uv` 0.12 or newer, and Databricks CLI 1.13.0 or newer.
Install Go 1.25 or newer and golangci-lint 2.12.2 only for configuration CLI changes.

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
uv sync --locked
git switch main
git pull --ff-only
git switch -c <feat-or-fix>/<short-name>
```

Use `feat/`, `fix/`, `docs/`, or `chore/` followed by a short purpose.

Before a large behavior, configuration, or dashboard design change:

1. Search existing issues and pull requests.
2. Open an issue describing the change.
3. Keep the pull request focused on that one problem.

Small documentation fixes do not need an issue first. Never commit credentials, workspace
URLs, warehouse IDs, or organization-specific job details.

## 2. Edit the owning file

Configuration and collection changes:

| Change | Owning file |
| --- | --- |
| Public YAML format | `schema/workflow-monitoring.schema.json` |
| YAML CLI commands | `cmd/workflow-monitoring/` |
| Validation or generation | `workflow_monitoring_dashboard.py` |
| Jobs API collection | `src/collect_workflow_monitoring_jobs_api.py` |
| Bundle deployment | `databricks.yml` or `resources/` |

Dashboard and documentation changes:

| Change | Owning file |
| --- | --- |
| Dashboard SQL or layout | `src/dashboards/workflow-monitoring.scaffold.lvdash.json` |
| Custom Vega-Lite chart | `src/visualizations/*.vega.json` |
| Generated dashboard | Generated from the dashboard scaffold |
| Generated collector Job | Generated from the public YAML |
| User instructions | `README.md` |

Do not edit generated deployment files directly. Change the owning source, then run:

```sh
uv run python workflow_monitoring_dashboard.py generate
```

Keep these product contracts:

- Every workflow has a Job ID and SLA. Job names come from the Jobs API.
- The collector schedule is fixed at five minutes.
- Omitting `jobs_api_config` creates default storage when the collector first runs.
- Providing `jobs_api_config` uses an existing catalog and schema without creating them.
- Tracked configuration and generated files contain fake values only.

When dependencies change, run `uv lock` and commit `pyproject.toml` with `uv.lock`.

## 3. Run fast checks

```sh
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests -v
uv run python workflow_monitoring_dashboard.py validate
for visualization_file in src/visualizations/*.vega.json; do jq empty "$visualization_file"; done
jq empty src/dashboards/workflow-monitoring.lvdash.json
golangci-lint fmt --diff ./...
golangci-lint run ./...
go test ./...
```

## 4. Run the required local gate

GitHub-hosted Actions are disabled. Run the repository wrapper:

```sh
scripts/run-local-validation.sh
```

The wrapper selects the host architecture, rebuilds the fixed image, and reuses its labelled
Act container for the current checkout. It replaces the container when the image or checkout
changes, then removes only dangling images.

Live Databricks validation is optional unless a maintainer requests it. Every live command must
use the profile you selected as `--profile <name>`.

## 5. Open the pull request

Include:

- why the change is required;
- what changed, including public configuration changes;
- commands run and exact results;
- screenshots for visible dashboard changes;
- validation that could not be run.

Use a conventional commit subject of 50 characters or fewer, such as
`fix: correct monthly deadline`. Pull requests are squash-merged after approval.

Before requesting review, confirm names are clear, tests cover important failures, generated
files are current, tracked values are fake, and documentation matches behavior. Keep the
implementation small and complete.

For releases, follow [RELEASING.md](RELEASING.md). When the GitHub About text, topics, or social
preview changes, update `.github/repository-metadata.yml` and GitHub together.
