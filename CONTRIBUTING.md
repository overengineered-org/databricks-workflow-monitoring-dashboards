# Contributing

GitHub repository metadata is documented in `.github/repository-metadata.yml`. GitHub does not apply this file automatically. If the repository About text, topics, or social preview changes, update both GitHub and this file.

Thanks for helping improve Databricks Workflow Monitoring Dashboards.

This guide explains how to make a focused change and prove that it works.

## Before you start

1. Search existing issues and pull requests for related work.
2. Open an issue before a large behavior, configuration, or dashboard design change.
3. Keep each pull request focused on one problem.
4. Never include credentials, workspace URLs, warehouse IDs, or organization-specific job details.

Small documentation fixes do not need an issue first.

## Set up the project

You need Python 3.11 or newer and `uv` 0.12 or newer.

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
uv sync --locked
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
| Validation or generation | `workflow_monitoring_dashboard.py` |
| Dashboard SQL or layout | `src/dashboards/workflow-monitoring.scaffold.lvdash.json` |
| Custom Vega-Lite chart | `src/visualizations/*.vega.json` |
| Generated dashboard | `src/dashboards/workflow-monitoring.lvdash.json` |
| Bundle deployment | `databricks.yml` or `resources/` |
| User instructions | `README.md` |

Do not edit the generated `.lvdash.json` directly. Change the scaffold, then regenerate it with the public template:

```sh
uv run python workflow_monitoring_dashboard.py generate \
  --config workflow-monitoring.template.yml
```

The public template must contain fake values. Local `workflow-monitoring.yml` and `.databricks/` files are ignored and must stay out of contributions.

## Validate your change

Run the fast checks first:

```sh
uv run ruff format --check workflow_monitoring_dashboard.py tests
uv run ruff check workflow_monitoring_dashboard.py tests
uv run python -m unittest discover -s tests -v
uv run python workflow_monitoring_dashboard.py validate \
  --config workflow-monitoring.template.yml
for visualization_file in src/visualizations/*.vega.json; do jq empty "$visualization_file"; done
jq empty src/dashboards/workflow-monitoring.lvdash.json
```

Then run the required local workflow. GitHub-hosted Actions are disabled for this repository.

```sh
act --container-architecture linux/arm64 \
  --pull=false \
  -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
  -W .act/workflows/validate.yml
```

Use `linux/amd64` on Intel or AMD computers. See the README for building the local runner image and running optional live Databricks SQL validation.

Every live Databricks command must use an explicitly selected `--profile <name>`. Live validation and deployment are not required for ordinary contributions unless a maintainer asks for them.

## Open a pull request

Use a concise title and explain why the change is needed. Include:

- what changed and whether it changes public configuration;
- the commands you ran and their results;
- screenshots for visible dashboard changes;
- related issues;
- any validation you could not run.

Use short conventional commit messages such as `fix: correct monthly deadline`. Keep the first line at 50 characters or fewer.

Pull requests are squash-merged after approval. A passing local workflow does not replace review.

## Review checklist

Before requesting review, confirm:

- names explain their purpose without extra context;
- comments explain only non-obvious behavior;
- tests cover changed behavior and important failures;
- the template and generated dashboard contain fake values only;
- documentation and schema completion match the implementation.

Keep the implementation small and complete. Do not add compatibility layers, unused extension points, or a second path for an existing capability.
