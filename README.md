# Databricks Workflow Monitoring Dashboards

Clone the repository, then follow the five steps below. A first dev deployment takes about
10 minutes when your Databricks profile already works.

## Start here: deploy in five steps

### 1. Install

You need:

| Tool | Minimum |
| --- | --- |
| Python | 3.11 |
| `uv` | 0.12 |
| Databricks CLI | 1.13.0 |
| `jq` | current |
| `workflow-monitoring` CLI | latest release, or Go 1.25 before the first release |

Install missing tools with the official guides for
[Python](https://www.python.org/downloads/),
[uv](https://docs.astral.sh/uv/getting-started/installation/),
[Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install), or
[jq](https://jqlang.org/download/).

Clone the repository and install the locked Python environment:

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
uv sync --locked
```

After the first binary release, users do not install Go. Download the archive for your operating
system from [GitHub Releases](https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards/releases).

Apple Silicon example:

```sh
repository_url="https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards"
curl -fLO "$repository_url/releases/latest/download/workflow-monitoring-darwin-arm64.tar.gz"
tar -xzf workflow-monitoring-darwin-arm64.tar.gz
sudo install -m 0755 workflow-monitoring /usr/local/bin/workflow-monitoring
workflow-monitoring --version
```

If the Releases page has no CLI archive yet, build a temporary local binary:

```sh
go build -o /tmp/workflow-monitoring ./cmd/workflow-monitoring
export PATH="/tmp:$PATH"
workflow-monitoring --version
```

Docker and `act` are required only for contributor validation.

### 2. Select a Databricks profile

List profiles, then verify the one you will use:

```sh
databricks auth profiles
databricks current-user me --profile <profile>
```

Pass `--profile <profile>` to every live Databricks command. This project never selects or stores
a profile.

Get the three required IDs:

```sh
databricks auth describe --profile <profile> --output json \
  | jq -r '.details.configuration.workspace_id.value'

databricks warehouses list --profile <profile>

databricks jobs list --profile <profile>
```

### 3. Create the monitoring configuration

Replace the tracked fake configuration:

```sh
workflow-monitoring init \
  --workspace-id <workspace-id> \
  --force

workflow-monitoring add \
  --job-id <job-id> \
  --status active \
  --completion-time 06:00

workflow-monitoring list
```

This uses automatic storage: `workflow_monitoring.lakeflow_jobs`. If you must use existing Unity
Catalog objects, add both flags to `init`:

```sh
--catalog <existing-catalog> --schema <existing-schema>
```

### 4. Generate and validate

Generate the dashboard and collector Job, then validate the bundle:

```sh
uv run --no-dev python workflow_monitoring_dashboard.py validate
uv run --no-dev python workflow_monitoring_dashboard.py generate

databricks bundle validate --strict -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Generation writes these reviewed outputs:

- `src/dashboards/workflow-monitoring.lvdash.json`
- `resources/workflow-monitoring.jobs-api.job.yml`

Do not edit generated files. Edit `workflow-monitoring.yml` or the dashboard scaffold, then
generate again.

### 5. Deploy and run once

Deployment writes resources to the selected workspace. Review the generated files first.

```sh
databricks bundle deploy -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"

databricks bundle run workflow_monitoring_jobs_api_collector \
  -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Working result: one five-minute collector Job, two governed Delta tables, and one refreshed AI/BI
dashboard. The first collector run creates missing tables and loads Jobs API state.

## What gets deployed

```text
Five-minute Lakeflow Job
  -> Jobs API runs for configured Job IDs
  -> two governed Delta tables
  -> AI/BI dashboard refresh
```

The dashboard cannot run Python or call the Jobs API. The collector Job polls the API first.

This repository has one monitoring path: Jobs API polling. It does not use system tables. Cost,
compute classification, task details, parameters, identities, and notebook output are excluded.

## Configuration reference

### Change workflows with the CLI

| Task | Command |
| --- | --- |
| Create configuration | `workflow-monitoring init --workspace-id <id>` |
| List workflows | `workflow-monitoring list` |
| Add workflow | `workflow-monitoring add --job-id <id> --completion-time <HH:MM>` |
| Update workflow | `workflow-monitoring update <job-id> <changed flags>` |
| Remove workflow | `workflow-monitoring remove <job-id> --yes` |

Every command accepts `--config <path>`. The CLI preserves YAML comments and workflow order,
rejects duplicate Job IDs, and validates schedule fields.

### Choose storage

| Mode | Configuration | Result |
| --- | --- | --- |
| Automatic | Omit `jobs_api_config` | Creates `workflow_monitoring.lakeflow_jobs` if missing. |
| Bring your own | Set catalog and schema | Uses existing objects and skips namespace DDL. |

Both modes create the two collector tables if missing. Providing only one storage field is
invalid.

Manual YAML editing remains supported:

```yaml
# yaml-language-server: $schema=./schema/workflow-monitoring.schema.json

version: 2
jobs_api_config:
  catalog: workflow_monitoring
  schema: lakeflow_jobs
workspace_id: "1234567890123456"
default_timezone: UTC

workflows:
  - job_id: 123456
    monitoring_status: active
    sla:
      frequency: daily
      completion_time: "06:00"
      timezone: Australia/Melbourne
```

Job names come from the Jobs API. Renaming a Databricks Job does not require a YAML change.

### Set SLA rules

| Frequency | Required extra field | Example |
| --- | --- | --- |
| Daily | none | `frequency: daily` |
| Weekly | `day_of_week` | `day_of_week: monday` |
| Fortnightly | `first_deadline_date` | `first_deadline_date: "2026-01-12"` |
| Monthly | `day_of_month` | `day_of_month: 31` |

Every workflow needs `sla.completion_time` in `HH:MM`. `sla.timezone` overrides
`default_timezone`. Monthly days 29, 30, and 31 use the final day in shorter months.

| SLA status | Meaning |
| --- | --- |
| `Pending` | Deadline has not arrived and no qualifying success exists. |
| `Met` | A successful run completed within this deadline window. |
| `Missed` | Deadline passed without a qualifying success. |

A later success does not rewrite a historical miss.

Pause one workflow without deleting it:

```sh
workflow-monitoring update <job-id> --status inactive
uv run --no-dev python workflow_monitoring_dashboard.py generate
```

Inactive workflows remain validated but are excluded from collection. Zero active workflows is
valid and keeps the generated collector schedule paused.

## Dashboard results

### Operations

- Workflows needing attention now
- Runs that did not succeed today
- Met and missed SLA deadlines
- Late successes after missed deadlines
- Pending, failed, or stale collector data

### Trends

- Run outcomes over time
- Success rate
- Average duration
- Duration by workflow and run
- Latest completed-run details

## Operations reference

### Collector state

| Table | Key | Stores |
| --- | --- | --- |
| `workflow_run_api_state` | workspace, job, run | Rolling 100-day run state |
| `workflow_job_api_state` | workspace, job | Job name, collection times, and bounded error |

The collector guarantees:

- Fixed five-minute schedule, paused when no workflows are active
- Workspace ID verification before storage changes
- Workspace and Job-scoped idempotent Delta `MERGE`
- Rolling 100-day run state for the configured workspace
- Dashboard refresh after every collection attempt

The collector runs as the bundle deployer unless the adopter adds bundle `run_as`. Use a service
principal for production.

### Required access

The run identity needs:

1. Read access to configured Jobs and runs.
2. Read and write access to the two collector tables.
3. Refresh access to the dashboard and access to its SQL warehouse.

| Storage mode | Extra Unity Catalog access |
| --- | --- |
| Bring your own | `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE` |
| Automatic | Create the default catalog, schema, and child tables |

### Bundle settings

| Variable | Required | Default |
| --- | --- | --- |
| `warehouse_id` | yes | none |
| `dashboard_display_name` | no | `Databricks Workflow Monitoring Dashboard` |
| `dashboard_viewer_group` | no | `users` |

The dashboard uses embedded credentials and grants `CAN_READ` to the viewer group.

## Contributor validation

Fast checks:

```sh
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests -v
golangci-lint run ./...
```

Full local gate, usually under 2 minutes after the first run:

```sh
scripts/run-local-validation.sh
```

The wrapper rebuilds one fixed image and reuses one labelled Act container while that image is
current. It replaces a stale container and removes only unused dangling images. GitHub-hosted
pipelines are disabled.

Read-only live SQL validation:

```sh
scripts/validate-sql-datasets.sh \
  <profile> <warehouse-id> src/dashboards/workflow-monitoring.lvdash.json
```

Empty results prove SQL parsing, referenced fields, and access. They do not prove workflow metric
correctness.

## Limits

- Five minutes is a polling target, not real time.
- Stored run state covers 100 days; visible SLA history covers 60 days.
- Current status includes active states; history uses terminal runs.
- Deleted or inaccessible Jobs appear as collection errors.
- One configuration targets one workspace.

## Repository map

Configuration and deployment:

| Path | Purpose |
| --- | --- |
| `workflow-monitoring.yml` | Tracked configuration with fake upstream values |
| `cmd/workflow-monitoring` | Configuration CLI |
| `schema/workflow-monitoring.schema.json` | YAML contract and editor hints |
| `workflow_monitoring_dashboard.py` | Validation and generation |

Collection and dashboard:

| Path | Purpose |
| --- | --- |
| `src/collect_workflow_monitoring_jobs_api.py` | Jobs API collector |
| `resources/workflow-monitoring.jobs-api.job.yml` | Generated collector Job |
| `src/dashboards/workflow-monitoring.scaffold.lvdash.json` | Dashboard source |
| `src/visualizations/*.vega.json` | Custom chart sources |

Upstream files use fake values. Adopter repositories can track their own non-secret IDs.

## Troubleshooting

### Setup

| Problem | Fix |
| --- | --- |
| Configuration fails validation | Add a positive `job_id` and complete SLA fields. |
| Automatic storage fails | Grant namespace creation rights or use existing storage. |
| Existing storage fails | Confirm both objects exist and grant traversal plus `CREATE TABLE`. |
| CLI is missing | Download the correct archive from GitHub Releases. |

### Dashboard

| Problem | Fix |
| --- | --- |
| Workspace ID mismatch | Copy the ID from `databricks auth describe`. |
| Jobs API collection failed | Read the bounded error in `workflow_job_api_state`. |
| Collection is pending | Run the collector once and confirm configured Job IDs. |
| Data is stale | Check the collector schedule, latest run, and run identity. |

## More help

Repository guides:

- [Fast Logistics disposable example](examples/fast-logistics/README.md)
- [Contributing](CONTRIBUTING.md)
- [Releasing](RELEASING.md)

Databricks documentation:

- [AI/BI dashboard documentation](https://docs.databricks.com/aws/en/dashboards/)
- [Lakeflow Jobs API 2.2](https://docs.databricks.com/aws/en/reference/jobs-api-2-2-updates)
- [Declarative Automation Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/)

Next: run the clone command in step 1.
