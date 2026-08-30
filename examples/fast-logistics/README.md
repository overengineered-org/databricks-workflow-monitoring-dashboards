# Fast Logistics example

This disposable four-job example creates the outcome, recovery, late-delivery, and duration
shapes used by the workflow-monitoring dashboard.

It contains no workspace URL, workspace ID, job ID, user name, profile name, or credential. Jobs
deploy with schedules paused. Nothing runs until you explicitly trigger a job or unpause a
schedule.

## Deploy

From the repository root, choose a Databricks CLI profile and run:

```sh
cd examples/fast-logistics
databricks bundle validate --strict -t dev --profile <profile>
databricks bundle deploy -t dev --profile <profile>
databricks bundle summary -t dev --profile <profile>
```

Deployment creates four jobs with paused schedules. It does not start compute.

## Run

Trigger only the scenario you need:

```sh
databricks bundle run fast_logistics_on_time \
  -t dev --profile <profile>
```

The recovery scenario fails first, then succeeds when retried:

```sh
databricks bundle run fast_logistics_recovery \
  -t dev --profile <profile> --params run_mode=scheduled

databricks bundle run fast_logistics_recovery \
  -t dev --profile <profile> --params run_mode=retry
```

All jobs use serverless compute and have a 60-second timeout.

## Remove

When finished, remove the disposable resources:

```sh
databricks bundle destroy --auto-approve -t dev --profile <profile>
```

To monitor these jobs, use each ID shown by `bundle summary`:

```sh
workflow-monitoring add \
  --job-id <job-id> \
  --status active \
  --completion-time 06:00
```

Run the command once per Job ID. Then generate and deploy the root monitoring bundle. Job names
come from the Jobs API.
