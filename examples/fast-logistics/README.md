# Fast Logistics example

Deploy the four paused example jobs with step 1. Deployment takes about 2 minutes and does not
start compute.

The jobs create the dashboard's outcome, recovery, late-delivery, and duration scenarios. The
bundle contains no workspace URL, workspace ID, job ID, user name, profile name, or credential.

## 1. Deploy paused jobs

From the repository root, use the Databricks CLI profile you selected:

```sh
cd examples/fast-logistics
databricks bundle validate --strict -t dev --profile <profile>
databricks bundle deploy -t dev --profile <profile>
databricks bundle summary -t dev --profile <profile>
```

The summary shows the four Job IDs. Keep them for step 3. All schedules remain paused.

## 2. Run one scenario

Trigger only the scenario you need:

```sh
databricks bundle run fast_logistics_on_time \
  -t dev --profile <profile>
```

The recovery scenario fails first, then succeeds on retry:

```sh
databricks bundle run fast_logistics_recovery \
  -t dev --profile <profile> --params run_mode=scheduled

databricks bundle run fast_logistics_recovery \
  -t dev --profile <profile> --params run_mode=retry
```

All jobs use serverless compute and have a 60-second timeout. Running a job can start compute.

## 3. Add the jobs to monitoring

Complete steps 1 to 3 in the root [README](../../README.md) first. Then return to the repository
root and add each Job ID:

```sh
cd ../..
workflow-monitoring add \
  --job-id <job-id> \
  --status active \
  --completion-time 06:00
```

Run the command once per Job ID. Job names come from the Jobs API. Generate and deploy the root
monitoring bundle with steps 4 and 5 in the root README.

## 4. Remove the example

If you added the jobs to monitoring, remove each Job ID and redeploy the root bundle first:

```sh
cd ../..
workflow-monitoring remove <job-id> --yes
uv run --no-dev python workflow_monitoring_dashboard.py generate
databricks bundle deploy -t dev --profile <profile> \
  --var="warehouse_id=<warehouse-id>"
```

Run the remove command once per Job ID. Then remove the four disposable jobs:

```sh
cd examples/fast-logistics
databricks bundle destroy --auto-approve -t dev --profile <profile>
```

This does not remove the monitoring bundle or collected historical rows.

Next: run the three commands in step 1 with your selected profile.
