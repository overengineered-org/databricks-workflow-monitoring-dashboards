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

A first dev deployment takes about 10 minutes when your Databricks profile already works.

### 1. Create the project

Do not clone this repository to deploy the dashboard. `databricks bundle init` reads one release
tag and creates a small project that contains only the deployment files.

You need:

| Tool | Minimum | Use |
| --- | --- | --- |
| Databricks CLI | 1.22.1 | Required |
| [uv](https://docs.astral.sh/uv/) | Any current release | Optional: installs the `workflow-monitoring` CLI |

**With the `workflow-monitoring` CLI (recommended).** Install the CLI once. It creates the project,
lists the Jobs in your workspace, and adds the Jobs that you tick to `workflow-monitoring.yml`:

```sh
uv tool install git+https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard@v0.3.0
databricks auth profiles
workflow-monitoring init --profile <profile>
cd workflow_monitoring
```

`init` runs `databricks bundle init` for the release tag of the CLI, then offers the Job picker
from [step 3](#3-create-the-configuration). The picker only reads the Jobs list. It starts no
compute.

**With the Databricks CLI only.**

```sh
databricks bundle init https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard \
  --tag v0.3.0 \
  --template-dir template \
  --output-dir .
cd workflow_monitoring
```

v0.2.0 is the first release with the template. v0.3.0 adds the `workflow-monitoring` CLI.

The command creates a project folder named by the `project_name` prompt. The default is
`workflow_monitoring`. Change the `cd` command if you choose another name. The other prompts set
the catalog, schema, catalog creation, default timezone, and dashboard viewer group.

The project contains these files:

```text
workflow_monitoring/
  .gitignore                                  You own
  databricks.yml                              You own: bundle variables and targets
  workflow-monitoring.yml                     You own: monitored Jobs and SLAs
  resources/
    workflow-monitoring.collector.job.yml     You own: collector Job and schedule
    workflow-monitoring.dashboard.yml         You own: dashboard resource
  workflow_monitoring/                        Product-owned: do not edit
    README.md                                 Upgrade steps
    VERSION
    collector/                                Collector Python files
    dashboard/workflow-monitoring.lvdash.json
    workflow-monitoring.schema.json
```

Do not edit the inner `workflow_monitoring/` folder. To upgrade the product files, replace that
folder with a newer release. The folder `README.md` has the steps.

Track the project in your own Git repository. The `.gitignore` file excludes the local
`.databricks/` folder.

```sh
git init
git add .
git commit -m "Add workflow monitoring project"
```

### 2. Get the required IDs

Choose a Databricks profile. This project never selects or stores one for you.

```sh
databricks auth profiles
databricks current-user me --profile <profile>

databricks warehouses list --profile <profile>
databricks jobs list --profile <profile>
```

Keep the SQL warehouse ID and each Job ID you want to monitor.

Set the warehouse ID in your shell. The bundle commands in this README read it. Set it again in
each new shell:

```sh
export BUNDLE_VAR_warehouse_id=<warehouse-id>
```

You can also pass `--var="warehouse_id=<warehouse-id>"` to each bundle command.

### 3. Create the configuration

Tick the Jobs to monitor with the CLI. Run it from the project folder:

```sh
workflow-monitoring select-jobs --profile <profile>
```

The picker shows each Job name and ID and marks the Jobs that are already in the file. Press
space to tick a Job and enter to confirm. The CLI adds each ticked Job as an active daily SLA at
`06:00`, keeps your comments, and checks the file with the collector rules before it writes.

| Option | Use |
| --- | --- |
| `--name-contains <text>` | Show only Jobs whose name has this text. |
| `--completion-time <HH:MM>` | Set the daily SLA time for the new Jobs. |
| `--dry-run` | Print the new entries and write nothing. |

To use a weekly, fortnightly, or monthly SLA, edit the entry after the CLI adds it. See
[Choose an SLA schedule](#choose-an-sla-schedule).

Without the CLI, edit `workflow-monitoring.yml` and add your first Job:

```yaml
version: 3

default_timezone: UTC

workflows:
  - job_id: <job-id>
    monitoring_status: active
    sla:
      frequency: daily
      completion_time: "06:00"
```

The collector reads this file on every run and replaces the active workflow rows in
`workflow_sla_config`.

The `catalog` and `schema` init prompts set the bundle variables of the same names in
`databricks.yml`. The `create_catalog_and_schema_if_missing` prompt sets catalog creation:

| Answer | Result |
| --- | --- |
| `yes` (default) | The collector creates the catalog and schema when they are missing. The run identity needs the rights to create them. |
| `no` | You bring an existing catalog and schema. The collector never runs `CREATE CATALOG` or `CREATE SCHEMA`. |

Both answers create the three collector tables when missing. To change the answer later, edit
the `create_catalog_and_schema_if_missing` default in `databricks.yml`.

### 4. Validate the bundle

```sh
databricks bundle validate --strict -t dev --profile <profile>
```

This command checks the bundle files. It does not read `workflow-monitoring.yml`. The collector
Job run checks that file and lists every error. The `yaml-language-server` schema comment at the
top of `workflow-monitoring.yml` gives editor checks while you type.

### 5. Deploy and load the first data

These commands create workspace resources and run serverless compute:

```sh
databricks bundle deploy -t dev --profile <profile>

databricks bundle run workflow_monitoring_jobs_api_collector -t dev --profile <profile>
```

Done means one collector Job, three Delta tables, and one refreshed AI/BI dashboard exist in the
selected workspace. The dashboard shows data after the first collector run.

### 6. Deploy to production

Complete this checklist before the first prod deploy:

1. Add bundle `run_as` configuration for a service principal.
2. Grant the run identity access to configured Jobs and the three Delta tables.
3. Grant dashboard refresh access and SQL warehouse access.
4. Confirm the viewer group in the bundle settings.

Then validate and deploy the prod target:

```sh
databricks bundle validate --strict -t prod --profile <profile>
databricks bundle deploy -t prod --profile <profile>
```

The prod target unpauses the collector schedule. The collector then runs every 5 minutes on
serverless compute.

## Manage monitored Jobs

Run `workflow-monitoring select-jobs --profile <profile>` to add Jobs, or edit
`workflow-monitoring.yml`. Keep Job IDs unique and valid for the selected SLA schedule.
Set `monitoring_status` to `inactive` to exclude a Job without deleting its configuration.

Run `databricks bundle deploy` to publish the change. In the prod target, the next scheduled
collector run applies it. The collector Job runs every 5 minutes on the `0 0/5 * * * ?` schedule
in UTC, set in `resources/workflow-monitoring.collector.job.yml`. In the dev target, the bundle
uses `mode: development`, so the schedule is paused and no scheduled run applies the change. In
dev, run `databricks bundle run workflow_monitoring_jobs_api_collector` as in
[step 5](#5-deploy-and-load-the-first-data) to apply the change now.

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

Product files live in `workflow_monitoring/` and are product-owned. To upgrade, render the new
release tag into a temporary folder, replace your `workflow_monitoring/` folder, apply the
listed user-owned file changes, then run `databricks bundle deploy`.

The steps are in `workflow_monitoring/README.md` in your rendered project.

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
| Upgrade the product files | `workflow_monitoring/README.md` in your project |
| Try disposable sample Jobs | [Fast Logistics example](examples/fast-logistics/README.md) |
| Change repository code | [Contributing](CONTRIBUTING.md) |
| Publish a version | [Releasing](RELEASING.md) |
| Learn AI/BI dashboards | [Databricks documentation](https://docs.databricks.com/aws/en/dashboards/) |

## License

This project uses the [Apache License 2.0](LICENSE).
Files that you copy from this repository into your project keep this license.
