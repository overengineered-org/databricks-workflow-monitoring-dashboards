# Databricks Workflow Monitoring Dashboards

An open-source monitoring solution for selected Databricks
[Lakeflow Jobs](https://docs.databricks.com/aws/en/jobs/monitor). It polls the Jobs API every
five minutes and presents current health, failures, run duration, and configured service-level
agreement (SLA) performance in a single AI/BI dashboard.

Data teams use it when several important Jobs need one shared operational view. Job names come
from Databricks. Your configuration contains only the Job IDs and completion deadlines that your
team wants to monitor.

## Why use it

Lakeflow Jobs shows run history one Job at a time. System tables support broader historical
analysis but can lag. This project uses the Jobs API for fresher operational status across only
the Jobs your team selects.

You deploy three components:

```text
workflow-monitoring.yml
  -> five-minute Jobs API collector
  -> two governed Delta tables
  -> one AI/BI monitoring dashboard
```

The generated collector calls the API and refreshes the dashboard. The dashboard does not run
Python or call the Jobs API directly.

## Start here

A first dev deployment takes about 10 minutes when your Databricks profile already works.

### 1. Clone and prepare

You need:

| Tool | Minimum |
| --- | --- |
| Python | 3.11 |
| `uv` | 0.12 |
| Databricks CLI | 1.13.0 |
| `jq` | current |

```sh
git clone https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards.git
cd databricks-workflow-monitoring-dashboards
uv sync --locked
./workflow-monitoring --version
```

The repository-local command downloads the matching prebuilt CLI into the ignored
`.workflow-monitoring/` directory. It needs no Go installation, administrator rights, `sudo`, or
global `PATH` change.

Windows:

```powershell
py scripts/run_workflow_monitoring.py --version
```

If GitHub downloads are blocked, set `WORKFLOW_MONITORING_RELEASE_DOWNLOAD_ROOT` to an approved
mirror that uses the same `releases/download` path.

### 2. Get the required IDs

Choose a Databricks profile. This project never selects or stores one for you.

```sh
databricks auth profiles
databricks current-user me --profile <profile>

databricks auth describe --profile <profile> --output json \
  | jq -r '.details.configuration.workspace_id.value'
databricks warehouses list --profile <profile>
databricks jobs list --profile <profile>
```

Keep the workspace ID, SQL warehouse ID, and each Job ID you want to monitor.

### 3. Create the configuration

Replace the tracked fake configuration and add your first Job:

```sh
./workflow-monitoring init \
  --workspace-id <workspace-id> \
  --force

./workflow-monitoring add \
  --job-id <job-id> \
  --status active \
  --completion-time 06:00

./workflow-monitoring list
```

This default creates `workflow_monitoring.lakeflow_jobs` when the collector first runs.

To use an existing catalog and schema instead:

```sh
./workflow-monitoring init \
  --workspace-id <workspace-id> \
  --catalog <existing-catalog> \
  --schema <existing-schema> \
  --force
```

Bring-your-own storage never creates the catalog or schema. Both modes create the two collector
tables when missing.

### 4. Generate and validate

```sh
uv run --no-dev python workflow_monitoring_dashboard.py validate
uv run --no-dev python workflow_monitoring_dashboard.py generate

databricks bundle validate --strict -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Review the generated dashboard and collector Job before deployment. Do not edit generated files
directly. Change `workflow-monitoring.yml` or the dashboard scaffold, then generate again.

### 5. Deploy and load the first data

These commands create workspace resources and run serverless compute:

```sh
databricks bundle deploy -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"

databricks bundle run workflow_monitoring_jobs_api_collector \
  -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Done means one collector Job, two Delta tables, and one refreshed AI/BI dashboard exist in the
selected workspace.

## Manage monitored Jobs

The built-in help is the command reference:

```sh
./workflow-monitoring --help
./workflow-monitoring help <command>
```

The CLI supports `list`, `add`, `update`, and `remove`. It rejects duplicate Job IDs and invalid
schedules while preserving YAML comments and workflow order.

After a configuration change, repeat [steps 4 and 5](#4-generate-and-validate).

## Choose an SLA schedule

Every active workflow needs a completion time. The timezone defaults to
`default_timezone` in `workflow-monitoring.yml`.

| Frequency | Extra value |
| --- | --- |
| Daily | None |
| Weekly | `day_of_week` |
| Fortnightly | `first_deadline_date` |
| Monthly | `day_of_month` |

The dashboard reports each deadline as:

| Status | Meaning |
| --- | --- |
| `Pending` | The deadline has not arrived. |
| `Met` | A successful run completed before the deadline. |
| `Missed` | The deadline passed without a qualifying success. |

A later success does not rewrite a historical miss. Monthly days 29, 30, and 31 use the final
day in shorter months.

## What the dashboard answers

- Which selected Jobs need attention now?
- Which runs failed or did not complete successfully?
- Which SLA deadlines were met, missed, or completed late?
- How are success rate and run duration changing?
- Is Jobs API collection healthy and current?

## Before production

1. Add bundle `run_as` configuration for a service principal.
2. Grant the run identity access to configured Jobs and the two Delta tables.
3. Grant dashboard refresh access and SQL warehouse access.
4. Confirm the viewer group in the bundle settings.

Five minutes is a polling target, not real-time alerting. Run state is retained for 100 days;
Jobs API reconciliation uses its 60-day run history. Stored nonterminal runs the API no longer
returns remain unchanged until local retention expires. Visible SLA history covers 60 days. Cost,
task details, parameters, identities, and notebook output are outside this dashboard's scope.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| CLI download blocked | Use an approved release mirror, then rerun `./workflow-monitoring`. |
| Configuration invalid | Run `./workflow-monitoring list`, then complete the reported field. |
| Workspace ID mismatch | Copy the ID from `databricks auth describe --profile <profile>`. |
| Storage creation denied | Grant namespace rights or use an existing catalog and schema. |
| Dashboard data stale | Check the collector schedule, latest run, and run identity. |

Collector errors are stored in `workflow_job_api_state` with bounded error text.

## Documentation map

| Goal | Read |
| --- | --- |
| Deploy the dashboard | This README |
| Try disposable sample Jobs | [Fast Logistics example](examples/fast-logistics/README.md) |
| Change repository code | [Contributing](CONTRIBUTING.md) |
| Publish a version | [Releasing](RELEASING.md) |
| Learn AI/BI dashboards | [Databricks documentation](https://docs.databricks.com/aws/en/dashboards/) |
