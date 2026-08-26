# Databricks Workflow Monitoring Dashboards

Monitor selected Lakeflow Jobs, their SLAs, failures, and duration with fresher Jobs API data.

## What gets deployed

```text
Five-minute Lakeflow Job
  -> Jobs API runs for configured job IDs
  -> two governed Delta tables
  -> AI/BI dashboard refresh
```

The dashboard cannot run Python or call the Jobs API. The generated collector Job does that work first.

This project has one monitoring path: Jobs API polling. It has no system-table dependency. Cost, compute classification, task details, parameters, identities, and notebook output are intentionally excluded.

## Quick start

### 1. Install

Required:

- Python 3.11 or newer
- `uv` 0.12 or newer
- Databricks CLI 1.13.0 or newer
- `jq`

Docker and `act` are needed only for the full local validation gate.

```sh
uv sync --locked
```

### 2. Choose a profile

```sh
databricks auth profiles
databricks current-user me --profile <name>
```

Pass `--profile <name>` to every live command. The project never stores or selects a profile.

Find the required IDs:

```sh
databricks auth describe --profile <name> --output json \
  | jq -r '.details.configuration.workspace_id.value'

databricks experimental aitools tools get-default-warehouse \
  --profile <name>

databricks jobs list --profile <name>
```

### 3. Configure

Edit the tracked `workflow-monitoring.yml`. Adopter repositories should commit their own
non-secret workspace and Job IDs. Upstream keeps fake values only.

Choose one storage mode:

| Mode | Configuration | Catalog and schema behavior |
| --- | --- | --- |
| Bring your own | Set both `jobs_api_config.catalog` and `jobs_api_config.schema` | Uses existing objects and skips catalog/schema DDL. |
| Automatic | Omit the entire `jobs_api_config` block | Creates `workflow_monitoring.lakeflow_jobs` if missing. |

In both modes, the collector creates its two Delta tables if missing. A partial
`jobs_api_config` block is invalid.

```yaml
# yaml-language-server: $schema=./schema/workflow-monitoring.schema.json

version: 2
# Keep this block to use an existing catalog and schema.
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

Every workflow entry requires:

- `job_id`: stable Databricks Job ID polled by the collector
- `monitoring_status`: `active` or `inactive`
- `sla`: expected successful completion schedule

The collector gets the current Job name from the Jobs API. Renaming a Databricks Job does not
require a YAML change.

### 4. Generate and validate

```sh
uv run --no-dev python workflow_monitoring_dashboard.py validate
uv run --no-dev python workflow_monitoring_dashboard.py generate

databricks bundle validate --strict -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

Generation writes:

- `src/dashboards/workflow-monitoring.lvdash.json`
- `resources/workflow-monitoring.jobs-api.job.yml`

Never edit either generated file. Edit the tracked YAML or dashboard scaffold, then generate again.

### 5. Deploy

Deployment changes the selected workspace and needs separate approval.

```sh
databricks bundle deploy -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"

databricks bundle run workflow_monitoring_jobs_api_collector \
  -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

The first successful run prepares storage according to the selected mode and creates the two
collector tables. The generated schedule stays paused when no workflows are active.

## What the dashboard answers

### Operations

- Which workflows need attention now?
- What failed today?
- Which SLA deadlines were met or missed?
- Did a late success recover a missed deadline?
- Is collector data pending, failed, or stale?

### Trends

- Run outcomes over time
- Success rate
- Average duration
- Duration by workflow and run
- Latest completed-run details

## SLA rules

| Frequency | Extra field | Example |
| --- | --- | --- |
| Daily | none | `frequency: daily` |
| Weekly | `day_of_week` | `day_of_week: monday` |
| Fortnightly | `first_deadline_date` | `first_deadline_date: "2026-01-12"` |
| Monthly | `day_of_month` | `day_of_month: 31` |

Every workflow also needs `sla.completion_time` in `HH:MM`. `sla.timezone` overrides `default_timezone`.

Monthly days 29, 30, and 31 use the final day when the month is shorter.

| SLA status | Meaning |
| --- | --- |
| `Pending` | Deadline has not arrived and no qualifying success exists. |
| `Met` | A successful run completed after the previous deadline and by this deadline. |
| `Missed` | Deadline passed without a qualifying success. |

A later success does not rewrite a historical miss.

Pause monitoring without deleting configuration:

```yaml
workflows:
  - job_id: 123456
    monitoring_status: inactive
    sla:
      frequency: daily
      completion_time: "06:00"
```

Inactive entries remain fully validated but are excluded from collection and dashboard generation. Zero active workflows is valid.

## Collector state

| Table | Key | Stores |
| --- | --- | --- |
| `workflow_run_api_state` | workspace, job, run | Start, end, normalized current state |
| `workflow_job_api_state` | workspace, job | Current Job name, last attempt, success, and sanitized error |

Collector guarantees:

- fixed five-minute schedule, paused when no workflows are active
- workspace and job-scoped checkpoints
- up to 60 days of first-run history
- idempotent Delta `MERGE`
- no raw API payload persistence
- dashboard refresh after every collection attempt

The collector runs as the bundle deployer unless the adopter adds bundle `run_as`. Use a service principal for production.

Required identity access:

1. Read configured Jobs and runs.
2. Read and modify the two collector tables.
3. Refresh the dashboard and use its SQL warehouse.

Storage-specific access:

| Mode | Extra Unity Catalog access |
| --- | --- |
| Bring your own | `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE` on the selected objects. |
| Automatic | Rights to create the default catalog and schema. If either exists, grant the matching traversal and child-creation privileges. |

## Bundle settings

| Variable | Required | Default |
| --- | --- | --- |
| `warehouse_id` | yes | none |
| `dashboard_display_name` | no | `Databricks Workflow Monitoring Dashboard` |
| `dashboard_viewer_group` | no | `users` |

The dashboard uses embedded credentials and grants `CAN_READ` to the viewer group.

## Validation

Fast checks:

```sh
uv run ruff format --check workflow_monitoring_dashboard.py \
  src/collect_workflow_monitoring_jobs_api.py tests
uv run ruff check workflow_monitoring_dashboard.py \
  src/collect_workflow_monitoring_jobs_api.py tests
uv run python -m unittest discover -s tests -v
```

Full local gate:

```sh
docker build --platform linux/arm64 \
  -t databricks-workflow-monitoring-dashboards-act:local \
  -f .act/Dockerfile .

act --container-architecture linux/arm64 --pull=false \
  -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
  -W .act/workflows/validate.yml
```

Use `linux/amd64` on Intel or AMD computers. GitHub-hosted pipelines are disabled.

After the collector has created its tables, validate every generated dashboard query:

```sh
scripts/validate-sql-datasets.sh \
  <profile> <warehouse-id> src/dashboards/workflow-monitoring.lvdash.json
```

This is read-only. Empty results prove SQL parsing, fields, and access, not real metric correctness.

## Limits

- Five minutes is a polling target, not real time.
- The first collection backfills at most 60 days.
- Run and SLA history show the most recent 60 days.
- Current status includes active lifecycle states. History and SLA metrics use terminal runs.
- A deleted or inaccessible Job appears as a collection error.
- One configuration targets one workspace.

## Repository map

| Path | Purpose |
| --- | --- |
| `workflow-monitoring.yml` | Tracked deployment configuration with fake upstream values |
| `schema/workflow-monitoring.schema.json` | YAML contract and editor hints |
| `workflow_monitoring_dashboard.py` | Validation and generation |
| `src/collect_workflow_monitoring_jobs_api.py` | Jobs API collection |
| `resources/workflow-monitoring.jobs-api.job.yml` | Generated collector Job deployment |
| `src/dashboards/workflow-monitoring.scaffold.lvdash.json` | Dashboard SQL and layout source |
| `src/visualizations/*.vega.json` | Readable custom chart sources |

The public configuration and generated deployment files use fake values. Adopter repositories can track their own non-secret IDs.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Workflow fails schema validation | Add a positive `job_id` and complete SLA fields. |
| Automatic storage setup fails | Grant catalog and schema creation rights, or configure existing storage. |
| Bring-your-own storage fails | Confirm both objects exist and grant `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE`. |
| `Jobs API collection failed` | Read the sanitized error in `workflow_job_api_state`. |
| Dashboard shows collection pending | Run the collector once, then confirm its Job ID list. |
| Dashboard shows stale data | Check collector schedule, latest run, and run identity. |

The [`examples/fast-logistics`](examples/fast-logistics/README.md) bundle has exact commands to deploy, run, inspect, and remove paused disposable test jobs.

Official references: [AI/BI dashboards](https://docs.databricks.com/aws/en/dashboards/), [Lakeflow Jobs API 2.2](https://docs.databricks.com/aws/en/reference/jobs-api-2-2-updates), and [Declarative Automation Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for changes and [`RELEASING.md`](RELEASING.md) for local SemVer releases.
