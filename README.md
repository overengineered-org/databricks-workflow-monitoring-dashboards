# Databricks Jobs Monitoring Dashboard

A deployable Databricks bundle that monitors selected Databricks
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
  -> three governed Delta tables
  -> one AI/BI monitoring dashboard
```

The collector calls the API and refreshes the dashboard. The dashboard does not run Python or
call the Jobs API directly.

## Start here

A first dev deployment takes about 10 minutes. This repository is the Databricks bundle. You clone
it, tick the Jobs to monitor, and deploy. The `workflow-monitoring` wrapper runs the Databricks CLI
commands for you.

### 1. Install the tools

| Tool | Minimum | Install |
| --- | --- | --- |
| Databricks CLI | 1.22.1 | [Install the Databricks CLI](https://docs.databricks.com/dev-tools/cli/install) |
| uv | Any current release | [Install uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Git | Any current release | [Install Git](https://git-scm.com/downloads) |

On macOS with Homebrew:

```sh
brew tap databricks/tap
brew install databricks uv git
```

### 2. Sign in to Databricks

Run this command with your workspace URL. It opens your browser, signs you in, and saves a
profile. Skip it if `databricks auth profiles` already shows a valid profile for the workspace.

```sh
databricks auth login --host https://dbc-1234.cloud.databricks.com
```

### 3. Clone this repository

To keep your configuration in your own GitHub repository, fork this repository first and clone
the fork.

```sh
git clone https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard.git
cd databricks-jobs-monitoring-dashboard
```

### 4. Tick the Jobs to monitor

```sh
uv run workflow-monitoring select-jobs
```

The CLI asks you to pick one of your signed-in Databricks profiles. It never picks or stores one
for you. Then it lists the Jobs in the workspace. Press space to tick a Job and enter to confirm.
Each ticked Job is added to `workflow-monitoring.yml` with an active daily SLA at `06:00`. This
step only reads from the workspace. It starts no compute.

### 5. Deploy and load the first data

```sh
uv run workflow-monitoring deploy
```

`deploy` asks for the SQL warehouse that runs the dashboard queries and saves it in
`.databricks/bundle/dev/variable-overrides.json`, a local file that Git ignores. Then it validates
the bundle, asks you to confirm, deploys to the `dev` target, and runs the collector once. This
step creates workspace resources and runs serverless compute. When it is done, it prints the
command that opens the dashboard.

### 6. Deploy to production

Complete this checklist before the first prod deploy:

1. Add bundle `run_as` configuration for a service principal.
2. Grant the run identity access to configured Jobs and the three Delta tables.
3. Grant dashboard refresh access and SQL warehouse access.
4. Confirm the viewer group in the bundle settings.

Then deploy the prod target. It asks for its own SQL warehouse the first time.

```sh
uv run workflow-monitoring deploy --target prod
```

The prod target unpauses the collector schedule. The collector then runs every 5 minutes on
serverless compute.

To deploy without the wrapper, follow [Manual setup](docs/manual-setup.md).

## What you change

| File | What to change |
| --- | --- |
| `workflow-monitoring.yml` | The monitored Jobs, their SLAs, and `default_timezone`. |
| `databricks.yml` | Optional: the variable defaults below. |

| Variable | Default | Use |
| --- | --- | --- |
| `catalog` | `workflow_monitoring` | Where the collector writes its three tables. |
| `schema` | `lakeflow_jobs` | Where the collector writes its three tables. |
| `create_catalog_and_schema_if_missing` | `true` | Set to `false` to use an existing catalog and schema. The collector then never runs `CREATE CATALOG` or `CREATE SCHEMA`. |
| `dashboard_viewer_group` | `users` | The group that can view the dashboard. |

Do not change the other files. `git pull` updates them. See [Upgrade](#upgrade).

## Manage monitored Jobs

```sh
uv run workflow-monitoring select-jobs
uv run workflow-monitoring deploy
```

`select-jobs` marks the Jobs that are already monitored. It keeps your comments, and it checks the
file with the collector rules before it writes. `deploy` publishes the change. In dev, it also runs
the collector once. In prod, the next scheduled collector run applies the change.

| Option | Command | Use |
| --- | --- | --- |
| `--profile <name>` | Both | Use this profile instead of picking one. |
| `--name-contains <text>` | `select-jobs` | Show only Jobs whose name has this text. |
| `--completion-time <HH:MM>` | `select-jobs` | Set the daily SLA time for the new Jobs. |
| `--dry-run` | `select-jobs` | Print the new entries and write nothing. |
| `--target dev\|prod` | `deploy` | Choose the bundle target. The default is `dev`. |
| `--warehouse-id <id>` | `deploy` | Use and save this SQL warehouse for the target. |
| `--yes` | `deploy` | Deploy without questions. |

To use a weekly, fortnightly, or monthly SLA, or to pause a Job, edit `workflow-monitoring.yml`.
Set `monitoring_status` to `inactive` to exclude a Job without deleting its configuration. See
[Choose an SLA schedule](#choose-an-sla-schedule).

### Deploy from CI

Without `--profile`, the wrapper uses the Databricks CLI environment variables when
`DATABRICKS_HOST` is set. For example, with a service principal:

```sh
export DATABRICKS_HOST=https://dbc-1234.cloud.databricks.com
export DATABRICKS_CLIENT_ID=00000000-0000-0000-0000-000000000000
export DATABRICKS_CLIENT_SECRET="$SERVICE_PRINCIPAL_SECRET"
uv run workflow-monitoring deploy --target prod --warehouse-id 1234567890abcdef --yes
```

## Config errors

The collector reads `workflow-monitoring.yml` before it writes any table. A bad file fails the
run. The run output lists every error with its path, for example
`workflows[0].sla.day_of_week: not allowed when frequency is daily`. Errors come as one list,
not one at a time.

Open the collector Job run output in the workspace to read the errors.

The tables do not change on a failed load. The collector reads the config file first, and it
creates no table and writes no row when the read fails. `workflow_sla_config` keeps the rows of
the last good run. The dashboard reads those rows, so it keeps the last good configuration.

## Upgrade

Pull the new release into your clone, then deploy:

```sh
git pull
uv run workflow-monitoring deploy
```

If you forked the repository, merge the new release tag from this repository into your fork. Only
`workflow-monitoring.yml` and `databricks.yml` can conflict, and only when you changed them. Read
the release notes for each version. They list the changes that need an action.

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

## Limits

Five minutes is a polling target, not real-time alerting. Run state follows the 60-day Jobs API
history window. Runs outside that window, or no longer returned by the API, are removed instead of
being shown with stale state. Visible SLA history covers 60 days. Cost, task details, parameters,
identities, and notebook output are outside this dashboard's scope.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Configuration invalid | Open the collector Job run output, fix the listed errors, then redeploy. |
| Storage creation denied | Grant namespace rights or use an existing catalog and schema. |
| Dashboard data stale | Check the collector schedule, latest run, and run identity. |

Collector errors are stored in `workflow_job_api_state` with bounded error text.

### Remove data from an old workspace

The collector retention delete only removes rows of the current workspace. It uses
`WHERE workspace_id = '<current>'` in the collector at
`src/collect_workflow_monitoring_jobs_api.py`.
Rows from an old workspace stay forever.

Get the old workspace ID, then run one delete per table in your SQL editor:

```sql
DELETE FROM <catalog>.<schema>.workflow_run_api_state WHERE workspace_id = '<old-workspace-id>';
DELETE FROM <catalog>.<schema>.workflow_job_api_state WHERE workspace_id = '<old-workspace-id>';
DELETE FROM <catalog>.<schema>.workflow_sla_config WHERE workspace_id = '<old-workspace-id>';
```

This deletes data permanently. Run it only for a workspace you no longer monitor.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you change the repository.

## Documentation map

| Goal | Read |
| --- | --- |
| Deploy the dashboard | This README |
| Deploy without the wrapper | [Manual setup](docs/manual-setup.md) |
| Try disposable sample Jobs | [Fast Logistics example](examples/fast-logistics/README.md) |
| Change repository code | [Contributing](CONTRIBUTING.md) |
| Publish a version | [Releasing](RELEASING.md) |
| Learn AI/BI dashboards | [Databricks documentation](https://docs.databricks.com/aws/en/dashboards/) |

## License

This project uses the [Apache License 2.0](LICENSE).
Files that you copy from this repository keep this license.
