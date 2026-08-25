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

```sh
cp workflow-monitoring.template.yml workflow-monitoring.yml
```

`workflow-monitoring.yml` is ignored by Git.

```yaml
# yaml-language-server: $schema=./schema/workflow-monitoring.schema.json

version: 2
jobs_api_config:
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

`jobs_api_config` is optional. Omit it to use `workflow_monitoring.lakeflow_jobs`.

Every active workflow requires:

- `job_id`: stable Databricks Job ID polled by the collector
- `job_name`: dashboard label
- `sla`: expected successful completion schedule

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

Never edit either generated file. Edit the local YAML or dashboard scaffold, then generate again.

### 5. Deploy

Deployment changes the selected workspace and needs separate approval.

```sh
databricks bundle deploy -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"

databricks bundle run workflow_monitoring_jobs_api_collector \
  -t dev --profile <name> \
  --var="warehouse_id=<warehouse-id>"
```

The first successful run creates the configured catalog, schema, and tables when the run identity has permission.

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

Every active workflow also needs `sla.completion_time` in `HH:MM`. `sla.timezone` overrides `default_timezone`.

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
  - job_name: daily_orders
    monitoring_status: inactive
```

Inactive entries skip active-workflow validation and dashboard generation. Zero active workflows is valid.

## Collector state

| Table | Key | Stores |
| --- | --- | --- |
| `workflow_run_api_state` | workspace, job, run | Start, end, normalized current state |
| `workflow_api_collection_status` | workspace, job | Last attempt, success, and sanitized error |

Collector guarantees:

- fixed five-minute schedule
- workspace and job-scoped checkpoints
- up to 60 days of first-run history
- idempotent Delta `MERGE`
- no raw API payload persistence
- dashboard refresh after every collection attempt

The collector runs as the bundle deployer unless the adopter adds bundle `run_as`. Use a service principal for production.

Required identity access:

1. Read configured Jobs and runs.
2. Create or use the configured catalog and schema.
3. Create, read, and modify the two tables.
4. Refresh the dashboard and use its SQL warehouse.

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
- Current status includes active lifecycle states. History and SLA metrics use terminal runs.
- A deleted or inaccessible Job appears as a collection error.
- One configuration targets one workspace.

## Repository map

| Path | Purpose |
| --- | --- |
| `workflow-monitoring.template.yml` | Public configuration example |
| `schema/workflow-monitoring.schema.json` | YAML contract and editor hints |
| `workflow_monitoring_dashboard.py` | Validation and generation |
| `src/collect_workflow_monitoring_jobs_api.py` | Jobs API collection |
| `src/dashboards/workflow-monitoring.scaffold.lvdash.json` | Dashboard SQL and layout source |
| `src/visualizations/*.vega.json` | Readable custom chart sources |

The public template and generated dashboard use fake values. Local adopter configuration stays ignored.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Active workflow fails schema validation | Add a positive `job_id` and complete SLA fields. |
| Collector cannot create storage | Grant Unity Catalog privileges or configure existing governed storage. |
| `Jobs API collection failed` | Read the sanitized error in `workflow_api_collection_status`. |
| Dashboard shows collection pending | Run the collector once, then confirm its Job ID list. |
| Dashboard shows stale data | Check collector schedule, latest run, and run identity. |

The [`examples/fast-logistics`](examples/fast-logistics/README.md) bundle creates paused disposable jobs for testing dashboard shapes.

Official references: [AI/BI dashboards](https://docs.databricks.com/aws/en/dashboards/), [Lakeflow Jobs API 2.2](https://docs.databricks.com/aws/en/reference/jobs-api-2-2-updates), and [Declarative Automation Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for changes and [`RELEASING.md`](RELEASING.md) for local SemVer releases.
