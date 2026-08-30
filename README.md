# Databricks Workflow Monitoring Dashboards

Monitor selected Lakeflow Jobs, their SLAs, outcomes, and duration with fresher Jobs API data.

## What gets deployed

```text
Five-minute Lakeflow Job
  -> Jobs API runs for configured job IDs
  -> two governed Delta tables
  -> AI/BI dashboard refresh
```

The dashboard cannot run Python or call the Jobs API. The generated collector Job does that
work first.

This project has one monitoring path: Jobs API polling. It has no system-table dependency.
Cost, compute classification, task details, parameters, identities, and notebook output are
intentionally excluded.

## Quick start

### 1. Install

Required:

- prebuilt `workflow-monitoring` configuration CLI
- Python 3.11 or newer
- `uv` 0.12 or newer
- Databricks CLI 1.13.0 or newer
- `jq`

Docker and `act` are needed only for the full local validation gate.

```sh
uv sync --locked
```

Users do not install Go. Download the archive for your operating system and architecture from
[GitHub Releases](https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards/releases).
For Apple Silicon after the first release:

```sh
repository_url="https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards"
curl -LO "$repository_url/releases/latest/download/workflow-monitoring-darwin-arm64.tar.gz"
tar -xzf workflow-monitoring-darwin-arm64.tar.gz
sudo install -m 0755 workflow-monitoring /usr/local/bin/workflow-monitoring
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

Create the tracked configuration, then add each Job ID:

```sh
workflow-monitoring init \
  --workspace-id <workspace-id> \
  --catalog <existing-catalog> \
  --schema <existing-schema> \
  --force

workflow-monitoring add \
  --job-id <job-id> \
  --status active \
  --completion-time 06:00

workflow-monitoring list
```

Omit `--catalog` and `--schema` together to use automatic storage. Omit `--force` when the
configuration does not exist. Every command also accepts `--config <path>`.

Use one command for each change:

| Task | Command |
| --- | --- |
| Create configuration | `workflow-monitoring init --workspace-id <id>` |
| List workflows | `workflow-monitoring list` |
| Add workflow | `workflow-monitoring add --job-id <id> --completion-time <HH:MM>` |
| Update workflow | `workflow-monitoring update <job-id> <changed flags>` |
| Remove workflow | `workflow-monitoring remove <job-id> --yes` |

The CLI keeps YAML comments and workflow order, and prevents duplicate Job IDs. Commit the
non-secret workspace and Job IDs in adopter repositories. Upstream keeps fake values only.

Choose one storage mode:

| Mode | Configuration | Catalog and schema behavior |
| --- | --- | --- |
| Bring your own | Set both fields | Uses existing objects; skips namespace DDL. |
| Automatic | Omit `jobs_api_config` | Creates `workflow_monitoring.lakeflow_jobs` if missing. |

In both modes, the collector creates its two Delta tables if missing. A partial
`jobs_api_config` block is invalid. Manual YAML editing remains supported for code review and
advanced changes:

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

The first collector run prepares storage according to the selected mode and creates the two
collector tables.

If any workflow is active, deployment creates an unpaused five-minute schedule. To deploy
without automatic runs, keep every workflow inactive and generate again. The schedule then
stays paused.

## What the dashboard answers

### Operations

- Which workflows need attention now?
- What did not succeed today in each workflow's timezone?
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

Inactive entries remain fully validated but are excluded from collection and dashboard
generation. Zero active workflows is valid.

## Collector state

| Table | Key | Stores |
| --- | --- | --- |
| `workflow_run_api_state` | workspace, job, run | Rolling 100-day start, end, and normalized current state |
| `workflow_job_api_state` | workspace, job | Current Job name, last attempt, success, and bounded error |

Collector guarantees:

- fixed five-minute schedule, paused when no workflows are active
- runtime workspace ID verification before storage changes
- workspace and job-scoped checkpoints with idempotent Delta `MERGE`
- rolling 100-day run state, pruned only for the current workspace
- no raw API payload persistence; dashboard refresh after every attempt

The collector runs as the bundle deployer unless the adopter adds bundle `run_as`. Use a
service principal for production.

Required identity access:

1. Read configured Jobs and runs.
2. Read and modify the two collector tables.
3. Refresh the dashboard and use its SQL warehouse.

Storage-specific access:

| Mode | Extra Unity Catalog access |
| --- | --- |
| Bring your own | `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE` on the selected objects. |
| Automatic | Rights to create the default catalog and schema, including child objects. |

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
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests -v
golangci-lint fmt --diff ./...
golangci-lint run ./...
go test ./...
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
- Collection and stored run state cover 100 days. Visible SLA history covers 60 days.
- Current status includes active lifecycle states. History and SLA metrics use terminal runs.
- A deleted or inaccessible Job appears as a collection error.
- One configuration targets one workspace.

## Repository map

| Path | Purpose |
| --- | --- |
| `workflow-monitoring.yml` | Tracked deployment configuration with fake upstream values |
| `cmd/workflow-monitoring` | Configuration CLI |
| `schema/workflow-monitoring.schema.json` | YAML contract and editor hints |
| `workflow_monitoring_dashboard.py` | Validation and generation |
| `src/collect_workflow_monitoring_jobs_api.py` | Jobs API collection |
| `resources/workflow-monitoring.jobs-api.job.yml` | Generated collector Job deployment |
| `src/dashboards/workflow-monitoring.scaffold.lvdash.json` | Dashboard SQL and layout source |
| `src/visualizations/*.vega.json` | Readable custom chart sources |

The public configuration and generated deployment files use fake values. Adopter repositories
can track their own non-secret IDs.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Workflow fails schema validation | Add a positive `job_id` and complete SLA fields. |
| Automatic storage setup fails | Grant catalog and schema creation rights, or configure existing storage. |
| Bring-your-own storage fails | Confirm both objects exist and grant `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE`. |
| `Jobs API collection failed` | Read the bounded error in `workflow_job_api_state`. |
| Workspace ID mismatch | Replace `workspace_id` with the ID reported by the selected profile. |
| `workflow-monitoring` is missing | Download the correct prebuilt archive from GitHub Releases. |
| Dashboard shows collection pending | Run the collector once, then confirm its Job ID list. |
| Dashboard shows stale data | Check collector schedule, latest run, and run identity. |

The [`examples/fast-logistics`](examples/fast-logistics/README.md) bundle has exact commands to
deploy, run, inspect, and remove paused disposable test jobs.

Official references: [AI/BI dashboards](https://docs.databricks.com/aws/en/dashboards/),
[Lakeflow Jobs API 2.2](https://docs.databricks.com/aws/en/reference/jobs-api-2-2-updates),
and [Declarative Automation Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/).

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for changes and
[`RELEASING.md`](RELEASING.md) for local SemVer releases.
