# Fast Logistics example

This disposable four-job example creates the outcome, recovery, late-delivery, and duration shapes used by the workflow-monitoring dashboard.

It contains no workspace URL, workspace ID, job ID, user name, profile name, or credential. Jobs deploy with schedules paused. Nothing runs until you explicitly trigger a job or unpause a schedule.

## Deploy

Choose a Databricks CLI profile, then run:

```sh
cd examples/fast-logistics
databricks bundle validate --profile <profile>
databricks bundle deploy --profile <profile>
```

Run any job manually from the bundle output. The `recovery` job intentionally fails when `run_mode` is `scheduled`; rerun it with `run_mode=retry` to model recovery. All jobs use serverless compute and have a 60-second timeout.

## Remove

When finished, remove the disposable resources:

```sh
databricks bundle destroy --auto-approve --profile <profile>
```

To monitor these jobs, copy `workflow-monitoring.template.yml`, then add the job names and IDs returned by the deployment. Validate and generate from the repository root.
