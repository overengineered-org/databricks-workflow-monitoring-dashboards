# Contributing

This guide explains how to make a focused change and prove that it works.

## Before you start

1. Search existing issues and pull requests for related work.
2. Open an issue before a large behavior, configuration, or dashboard design change.
3. Keep each pull request focused on one problem.
4. Never include credentials, workspace URLs, warehouse IDs, or organization-specific job details.

Small documentation fixes do not need an issue first.

## Set up the project

You need Python 3.11 or newer, `uv` 0.12 or newer, and Databricks CLI 1.13.0 or newer.
Install Go 1.25 or newer and golangci-lint 2.12.2 only when changing the
configuration CLI.

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
uv sync --locked
```

When `pyproject.toml` dependencies change, update and commit the lockfile in the same pull
request:

```sh
uv lock
git add pyproject.toml uv.lock
```

Create a branch from the latest `main`:

```sh
git switch main
git pull --ff-only
git switch -c <feat-or-fix>/<short-name>
```

Use `feat/`, `fix/`, `docs/`, or `chore/` followed by a short purpose.

## Know which file owns the change

| Change | File |
| --- | --- |
| Public YAML format | `schema/workflow-monitoring.schema.json` |
| YAML CRUD | `cmd/workflow-monitoring/` |
| Validation or generation | `workflow_monitoring_dashboard.py` |
| Jobs API collection | `src/collect_workflow_monitoring_jobs_api.py` |
| Dashboard SQL or layout | `src/dashboards/workflow-monitoring.scaffold.lvdash.json` |
| Custom Vega-Lite chart | `src/visualizations/*.vega.json` |
| Generated dashboard | `src/dashboards/workflow-monitoring.lvdash.json` |
| Generated collector Job | `resources/workflow-monitoring.jobs-api.job.yml` |
| Bundle deployment | `databricks.yml` or tracked files in `resources/` |
| User instructions | `README.md` |

Do not edit the generated `.lvdash.json` directly. Change the scaffold, then regenerate it with
the tracked public configuration:

```sh
uv run python workflow_monitoring_dashboard.py generate
```

`workflow-monitoring.yml` and both generated deployment files are tracked and must contain fake
values upstream. `.databricks/` remains ignored.

The product has one Jobs API path. Every workflow requires a Job ID and SLA. Job names come
from the Jobs API. Generation always creates the fixed five-minute collector resource. Omitting
`jobs_api_config` enables automatic default storage creation. Providing it selects an existing
catalog and schema, so the generated collector skips catalog and schema DDL.

## Validate your change

Run the fast checks first:

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

Then run the required local workflow. GitHub-hosted Actions are disabled for this repository.

```sh
scripts/run-local-validation.sh
```

The script chooses the host architecture, reuses the labelled local Act container, and prunes
dangling images. See the README for the reuse rules and optional live Databricks SQL validation.

Every live Databricks command must use an explicitly selected `--profile <name>`. Live validation
and deployment are not required for ordinary contributions unless a maintainer asks for them.

## Open a pull request

Use a concise title and explain why the change is needed. Include:

- what changed and whether it changes public configuration;
- the commands you ran and their results;
- screenshots for visible dashboard changes;
- related issues;
- any validation you could not run.

Use short conventional commit messages such as `fix: correct monthly deadline`. Keep the first
line at 50 characters or fewer.

Pull requests are squash-merged after approval. A passing local workflow does not replace
review.

Maintainers release only from clean, synchronized `main`. See [RELEASING.md](RELEASING.md) for
the local check and publish commands.

## Review checklist

Before requesting review, confirm:

- names explain their purpose without extra context;
- comments explain only non-obvious behavior;
- tests cover changed behavior and important failures;
- the tracked configuration and generated dashboard contain fake values only;
- documentation and schema completion match the implementation.

Keep the implementation small and complete. Do not add compatibility layers, unused extension
points, or a second path for an existing capability.

## Repository metadata

GitHub does not apply `.github/repository-metadata.yml` automatically. When the About text,
topics, or social preview changes, update GitHub and this file together.
