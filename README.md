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
  -> three governed Delta tables
  -> one AI/BI monitoring dashboard
```

The collector calls the API and refreshes the dashboard. The dashboard does not run Python or
call the Jobs API directly.

## Start here

A first dev deployment takes about 10 minutes when your Databricks profile already works.

### 1. Create the project

You need:

| Tool | Minimum |
| --- | --- |
| Databricks CLI | 1.22.1 |

```sh
databricks bundle init https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards \
  --tag v0.2.0 \
  --template-dir template \
  --output-dir .
cd workflow_monitoring
```

v0.2.0 is the first release with the template.

The command creates a project folder named by the `project_name` prompt. The default is
`workflow_monitoring`. Change the `cd` command if you choose another name.

The command copies the product files into `workflow_monitoring/` and creates the bundle and
configuration files that you own. Do not edit `workflow_monitoring/`. To upgrade the product
files, replace that folder with a newer release. The folder `README.md` has the steps.

### 2. Get the required IDs

Choose a Databricks profile. This project never selects or stores one for you.

```sh
databricks auth profiles
databricks current-user me --profile <profile>

databricks warehouses list --profile <profile>
databricks jobs list --profile <profile>
```

Keep the SQL warehouse ID and each Job ID you want to monitor.

### 3. Create the configuration

Edit `workflow-monitoring.yml` and add your first Job:

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
`workflow_sla_config`. To use an existing catalog and schema, set the `catalog` and `schema`
bundle variables. Keep `create_catalog_and_schema_if_missing` set to `false` so the collector
never runs `CREATE CATALOG` or `CREATE SCHEMA`. Both modes create the three collector tables when
missing.

### 4. Validate the configuration

```sh
databricks bundle validate --strict -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Configuration errors appear in the collector Job run output. Do not edit the built dashboard
directly. Maintainers rebuild it after scaffold changes. See
[Contributing](CONTRIBUTING.md#2-edit-the-owning-source).

### 5. Deploy and load the first data

These commands create workspace resources and run serverless compute:

For the first deploy, pass `--var="create_catalog_and_schema_if_missing=true"` so the collector
can create the default catalog and schema. Leave it set to `false` when you bring your own
catalog and schema.

```sh
databricks bundle deploy -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>" \
  --var="create_catalog_and_schema_if_missing=true"

databricks bundle run workflow_monitoring_jobs_api_collector \
  -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Done means one collector Job, three Delta tables, and one refreshed AI/BI dashboard exist in the
selected workspace. The dashboard shows data after the first collector run.

## Manage monitored Jobs

Edit `workflow-monitoring.yml`. Keep Job IDs unique and valid for the selected SLA schedule.
Set `monitoring_status` to `inactive` to exclude a Job without deleting its configuration.

After a configuration change, repeat [step 5](#5-deploy-and-load-the-first-data).

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
2. Grant the run identity access to configured Jobs and the three Delta tables.
3. Grant dashboard refresh access and SQL warehouse access.
4. Confirm the viewer group in the bundle settings.

Five minutes is a polling target, not real-time alerting. Run state follows the 60-day Jobs API
history window. Runs outside that window, or no longer returned by the API, are removed instead of
being shown with stale state. Visible SLA history covers 60 days. Cost, task details, parameters,
identities, and notebook output are outside this dashboard's scope.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Configuration invalid | Edit `workflow-monitoring.yml`, then run `databricks bundle validate`. |
| Storage creation denied | Grant namespace rights or use an existing catalog and schema. |
| Dashboard data stale | Check the collector schedule, latest run, and run identity. |

Collector errors are stored in `workflow_job_api_state` with bounded error text.

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
