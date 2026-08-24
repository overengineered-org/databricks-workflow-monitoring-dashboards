# Databricks Workflow Monitoring Dashboards

Monitor selected Lakeflow Jobs, their dashboard-defined SLAs, failures, duration, and estimated Databricks list cost from one YAML file.

![Workflow monitoring operations dashboard](docs/images/dashboard-operations-overview.png)

## Pick one monitoring path

An AI/BI dashboard runs SQL. It cannot run Python or poll the Jobs API. For fresher status, this project generates a five-minute Lakeflow collector Job that writes only the fields the dashboard needs.

| Path | Choose it when | Workflow status | Creates |
| --- | --- | --- | --- |
| `system_tables` | Databricks ingestion delay is acceptable | Delayed | Dashboard only |
| `jobs_api` | You need a five-minute polling target | Fresher, not real time | Dashboard, collector Job, two Delta tables |

Cost uses delayed billing system tables in both paths. It is estimated Databricks list cost. It excludes negotiated discounts and classic cloud-provider VM charges.

## Quick start

### 1. Install the tools

You need Python 3.11 or newer, `uv` 0.12 or newer, Databricks CLI 0.292 or newer, and `jq`.

Docker and `act` are needed only for the full local validation gate.

### 2. Choose a Databricks profile

```sh
databricks auth profiles
databricks current-user me --profile <name>
```

Pass `--profile <name>` to every live command. This project never stores or selects a profile.

Find your workspace and SQL warehouse IDs:

```sh
databricks auth describe --profile <name> --output json \
  | jq -r '.details.configuration.workspace_id.value'

databricks experimental aitools tools get-default-warehouse \
  --profile <name>
```

### 3. Create your local configuration

```sh
cp workflow-monitoring.template.yml workflow-monitoring.yml
```

`workflow-monitoring.yml` is ignored by Git in this public repository.

Choose one source:

```yaml
# Delayed, read-only system tables
monitoring_data_source_config:
  source: system_tables
```

```yaml
# Five-minute Jobs API polling target
monitoring_data_source_config:
  source: jobs_api
  storage:
    catalog: workflow_monitoring
    schema: lakeflow_jobs
```

For `jobs_api`, omit `storage` only if the defaults `workflow_monitoring.lakeflow_jobs` are correct. The collector creates that catalog and schema when its run identity has permission. It never chooses an unrelated catalog.

### 4. Add active workflows

```yaml
# yaml-language-server: $schema=./schema/workflow-monitoring.schema.json

version: 2
monitoring_data_source_config:
  source: jobs_api
  storage:
    catalog: workflow_monitoring
    schema: lakeflow_jobs
workspace_id: "1234567890123456"
default_timezone: UTC

workflows:
  - job_name: daily_orders
    job_id: 123456
    monitoring_status: active
    sla:
      frequency: daily
      completion_time: "06:00"
      timezone: Australia/Melbourne
```

`job_id` rules:

- `jobs_api`: required for every active workflow.
- `system_tables`: optional, but preferred because IDs survive renames.

In `jobs_api`, `job_name` is the dashboard label. The collector resolves the workflow directly from `job_id`.

### 5. Validate, generate, then validate the bundle

```sh
uv sync --locked --no-dev
uv run --no-dev python workflow_monitoring_dashboard.py validate
uv run --no-dev python workflow_monitoring_dashboard.py generate

databricks bundle validate --strict -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

Generation creates:

- `src/dashboards/workflow-monitoring.lvdash.json` for both paths.
- `resources/workflow-monitoring.jobs-api.job.yml` only for `jobs_api`.

Never edit the generated dashboard or collector resource directly. Edit `workflow-monitoring.yml` or the dashboard scaffold, then generate again.

Deployment changes the workspace and needs separate approval:

```sh
databricks bundle deploy -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

## What the dashboard answers

### Operations

![SLA delivery and workflow reliability views](docs/images/dashboard-sla-visualizations.png)

- Are workflows meeting their expected completion times?
- What failed today?
- Which workflows need attention now?
- Is reliability improving over 30 days?
- Are configuration or collection problems hiding data?

### Trends and cost

![Workflow trends and estimated cost](docs/images/dashboard-trends-cost.png)

- Run outcomes, duration, and success rate over 30 days.
- Estimated list cost by workflow and day.
- Latest run details and compute type.

Compute type is `Unknown` until matching billing metadata arrives. Cost stays delayed even when workflow status uses `jobs_api`.

## SLA rules

An SLA is the expected successful completion time for one workflow. It does not prove that every downstream table or data product is ready.

| Frequency | Required schedule field | Example |
| --- | --- | --- |
| Daily | none | `frequency: daily` |
| Weekly | `day_of_week` | `day_of_week: monday` |
| Fortnightly | `first_deadline_date` | `first_deadline_date: "2026-01-12"` |
| Monthly | `day_of_month` | `day_of_month: 31` |

Each active workflow also needs `sla.completion_time` in `HH:MM` format. `sla.timezone` overrides `default_timezone`.

Monthly days 29, 30, and 31 use the final day when the month is shorter.

| Dashboard status | Meaning |
| --- | --- |
| `Pending` | Deadline has not arrived and no qualifying success exists. |
| `Met` | A successful run completed after the previous deadline and by this deadline. |
| `Missed` | Deadline passed without a qualifying success. |

A late completion does not rewrite a historical miss. The next deadline starts a new period.

Pause one workflow without deleting it:

```yaml
workflows:
  - job_name: daily_orders
    monitoring_status: inactive
```

Inactive entries are excluded from all generated queries and validation rules. Zero active workflows is valid and produces empty dashboard results.

## How each path resolves workflows

### `system_tables`

1. Resolve by `job_id` when present.
2. Otherwise resolve by exact `job_name`.
3. Rank the latest job record before filtering deleted jobs.
4. Show missing, ambiguous, or mismatched configuration in Workflow health.

New jobs and renames can temporarily appear missing until `system.lakeflow.jobs` ingests the change.

### `jobs_api`

```text
Five-minute Lakeflow Job
  -> Jobs API list-runs for configured job IDs
  -> Delta MERGE into governed state
  -> AI/BI dashboard refresh
```

The collector owns two managed Delta tables:

| Table | Key | Stores |
| --- | --- | --- |
| `workflow_run_api_state` | workspace, job, run | Start, end, normalized run state |
| `workflow_api_collection_status` | workspace, job | Last attempt, last success, sanitized error |

The design is intentionally small:

- No raw API payloads, task details, parameters, identities, or notebook output.
- Delta `MERGE` makes retries and overlapping polls idempotent.
- Checkpoints are isolated by workspace and job.
- The first successful poll backfills up to 60 days of Jobs API history.
- Workflow health shows pending, failed, or stale collection state.

The generated Job runs as the bundle deployer unless the adopter adds bundle `run_as`. A user identity is acceptable for a pilot. Production should use a service principal that can:

1. Read configured Jobs and their runs.
2. Create or use the configured catalog and schema.
3. Create, read, and modify the two collector tables.
4. Refresh the dashboard and use its SQL warehouse.

## Bundle settings

| Variable | Required | Default |
| --- | --- | --- |
| `warehouse_id` | yes | none |
| `dashboard_display_name` | no | `Databricks Workflow Monitoring Dashboard` |
| `dashboard_viewer_group` | no | `users` |

The dashboard uses embedded credentials and grants `CAN_READ` to the viewer group. The deployment identity must be able to use the warehouse and read the required system tables.

Validate both targets before deployment:

```sh
databricks bundle validate --strict -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"

databricks bundle validate --strict -t prod --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

## Validation

### Fast local checks

```sh
uv sync --locked
uv run ruff format --check workflow_monitoring_dashboard.py \
  src/collect_workflow_monitoring_jobs_api.py tests
uv run ruff check workflow_monitoring_dashboard.py \
  src/collect_workflow_monitoring_jobs_api.py tests
uv run python -m unittest discover -s tests -v
```

### Full local gate

GitHub-hosted pipelines are disabled. Build and run the repository's local ARM64 runner:

```sh
docker build --platform linux/arm64 \
  -t databricks-workflow-monitoring-dashboards-act:local \
  -f .act/Dockerfile .

act --container-architecture linux/arm64 --pull=false \
  -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
  -W .act/workflows/validate.yml
```

Use `linux/amd64` on Intel or AMD machines.

### Live read-only SQL checks

```sh
scripts/validate-sql-datasets.sh \
  <profile> <warehouse-id> src/dashboards/workflow-monitoring.lvdash.json
```

This sends each dataset query and its measures to the selected warehouse. It also checks daily, weekly, fortnightly, monthly, month-end, and timezone calculations.

It does not create tables or insert rows. Empty example results prove SQL parsing, referenced fields, and access. They do not prove real workflow metrics.

Run the complete live Act gate with the ignored config mounted read-only:

```sh
act --container-architecture linux/arm64 --pull=false \
  --container-options "-v ${PWD}/workflow-monitoring.yml:/tmp/workflow-monitoring.yml:ro -v ${HOME}/.databrickscfg:/root/.databrickscfg:ro" \
  -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
  -W .act/workflows/databricks-validate.yml \
  -s DATABRICKS_CONFIG_PROFILE=<name> \
  -s DATABRICKS_WAREHOUSE_ID=<warehouse-id>
```

The Act gate needs a profile whose credentials are readable inside Linux, such as a service principal or PAT profile in `.databrickscfg`. A macOS Keychain-backed user OAuth profile works with the direct host commands above, but not inside the container.

## Repository map

| Path | Owner |
| --- | --- |
| `workflow-monitoring.template.yml` | Public configuration example |
| `schema/workflow-monitoring.schema.json` | Configuration contract and editor hints |
| `workflow_monitoring_dashboard.py` | Validation and generation |
| `src/collect_workflow_monitoring_jobs_api.py` | Jobs API collection |
| `src/dashboards/workflow-monitoring.scaffold.lvdash.json` | Dashboard SQL and layout source |
| `src/visualizations/*.vega.json` | Readable custom chart sources |

The generator serializes four custom Vega-Lite charts into the dashboard. Databricks custom visualizations are in Public Preview, so verify rendering in the target workspace.

Upstream examples and generated dashboard files must use fake account values. Adopter repositories can follow their own security policy.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `job_id is required for jobs_api` | Add an ID to every active workflow. |
| Collector cannot create storage | Grant the run identity required Unity Catalog privileges or choose existing governed storage. |
| `Jobs API collection failed` | Read the sanitized error in `workflow_api_collection_status`. |
| `does not match schema` | Use editor hints and check source-specific fields. |
| Configuration problem in dashboard | Check workspace ID, job ID, and exact system-table job name. |

## Optional demo and contributing

[`examples/fast-logistics`](examples/fast-logistics/README.md) is a paused-by-default disposable job bundle for reproducing dashboard shapes. Deploy it only when needed and destroy it afterwards.

Official references: [AI/BI dashboards](https://docs.databricks.com/aws/en/dashboards/), [Lakeflow Jobs API 2.2](https://docs.databricks.com/aws/en/reference/jobs-api-2-2-updates), [system tables](https://docs.databricks.com/aws/en/admin/system-tables/), and [Declarative Automation Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and pull-request guidance.
