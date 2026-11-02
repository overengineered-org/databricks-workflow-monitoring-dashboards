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

A first dev deployment takes about 10 minutes. You do not clone this repository. The
`workflow-monitoring` CLI downloads one release and guides you through every step.

### 1. Install the tools

| Tool | Minimum | Install |
| --- | --- | --- |
| Databricks CLI | 1.22.1 | [Install the Databricks CLI](https://docs.databricks.com/dev-tools/cli/install) |
| uv | Any current release | [Install uv](https://docs.astral.sh/uv/getting-started/installation/) |

On macOS with Homebrew:

```sh
brew tap databricks/tap
brew install databricks uv
```

### 2. Sign in to Databricks

Run this command with your workspace URL, for example `https://dbc-1234.cloud.databricks.com`.
It opens your browser, signs you in, and saves a profile. Skip it if
`databricks auth profiles` already shows a valid profile for the workspace.

```sh
databricks auth login --host https://dbc-1234.cloud.databricks.com
```

### 3. Install the CLI and create the project

```sh
uv tool install git+https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard@v0.4.0
workflow-monitoring init
```

`init` asks its questions in this order:

1. **Databricks profile.** Pick one of your signed-in profiles. The CLI never picks one for you
   and never stores one.
2. **Project settings.** Press enter to accept each default, or type your own value:

   | Prompt | Default | Use |
   | --- | --- | --- |
   | Project folder and bundle name | `workflow_monitoring` | The folder that `init` creates. |
   | Catalog and schema | `workflow_monitoring`, `lakeflow_jobs` | Where the collector writes its three tables. |
   | Create the catalog and schema | `yes` | Answer `no` to use an existing catalog and schema. |
   | Default timezone | `UTC` | IANA timezone for SLA deadlines. |
   | Dashboard viewer group | `users` | The group that can view the dashboard. |

3. **SQL warehouse.** Pick the warehouse that runs the dashboard queries. The CLI saves it in
   `.databricks/bundle/<target>/variable-overrides.json`, a local file that Git ignores.
4. **Jobs to monitor.** Press space to tick a Job and enter to confirm. Each ticked Job gets an
   active daily SLA at `06:00`.

Then `init` validates the bundle. Up to this point, the CLI only reads from the workspace. It
starts no compute.

### 4. Deploy and load the first data

```sh
cd workflow_monitoring
workflow-monitoring deploy
```

`deploy` validates the bundle, asks you to confirm, deploys to the `dev` target, and runs the
collector once. This step creates workspace resources and runs serverless compute. Done means
one collector Job, three Delta tables, and one refreshed AI/BI dashboard exist in your
workspace.

Track the project in your own Git repository:

```sh
git init
git add .
git commit -m "Add workflow monitoring project"
```

### 5. Deploy to production

Complete this checklist before the first prod deploy:

1. Add bundle `run_as` configuration for a service principal.
2. Grant the run identity access to configured Jobs and the three Delta tables.
3. Grant dashboard refresh access and SQL warehouse access.
4. Confirm the viewer group in the bundle settings.

Then deploy the prod target:

```sh
workflow-monitoring deploy --target prod
```

The prod target unpauses the collector schedule. The collector then runs every 5 minutes on
serverless compute.

To set up without the `workflow-monitoring` CLI, follow [Manual setup](docs/manual-setup.md).

## Your project

```text
workflow_monitoring/
  databricks.yml                              You own: bundle variables and targets
  workflow-monitoring.yml                     You own: monitored Jobs and SLAs
  resources/                                  You own: collector Job and dashboard resources
  workflow_monitoring/                        Product-owned: do not edit
```

Do not edit the inner `workflow_monitoring/` folder. To upgrade it, see [Upgrade](#upgrade).

## Manage monitored Jobs

Run these commands in the project folder:

```sh
workflow-monitoring select-jobs
workflow-monitoring deploy
```

`select-jobs` lists the Jobs in your workspace and marks the Jobs that are already monitored. It
keeps your comments, and it checks the file with the collector rules in your project before it
writes. `deploy` publishes the change. In dev, it also runs the collector once. In prod, the
next scheduled collector run applies the change.

| Option | Use |
| --- | --- |
| `--profile <name>` | Use this profile instead of picking one. Works with every command. |
| `--name-contains <text>` | Show only Jobs whose name has this text. |
| `--completion-time <HH:MM>` | Set the daily SLA time for the new Jobs. |
| `--dry-run` | Print the new entries and write nothing. |

To use a weekly, fortnightly, or monthly SLA, or to pause a Job, edit `workflow-monitoring.yml`.
Set `monitoring_status` to `inactive` to exclude a Job without deleting its configuration. See
[Choose an SLA schedule](#choose-an-sla-schedule).

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

Product files live in `workflow_monitoring/` and are product-owned. To upgrade, install the new
CLI release, replace your `workflow_monitoring/` folder with the one from the new release, apply
the listed user-owned file changes, then run `workflow-monitoring deploy`.

The steps are in `workflow_monitoring/README.md` in your project.

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
`workflow_monitoring/collector/collect_workflow_monitoring_jobs_api.py`.
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
| Deploy without the `workflow-monitoring` CLI | [Manual setup](docs/manual-setup.md) |
| Upgrade the product files | `workflow_monitoring/README.md` in your project |
| Try disposable sample Jobs | [Fast Logistics example](examples/fast-logistics/README.md) |
| Change repository code | [Contributing](CONTRIBUTING.md) |
| Publish a version | [Releasing](RELEASING.md) |
| Learn AI/BI dashboards | [Databricks documentation](https://docs.databricks.com/aws/en/dashboards/) |

## License

This project uses the [Apache License 2.0](LICENSE).
Files that you copy from this repository into your project keep this license.
