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

Return to the repository root. Repeat the add command from
[README step 3](../../README.md#3-create-the-configuration) for each Job ID from the bundle
summary. Job names come from the Jobs API.

Run [README steps 4 and 5](../../README.md#4-generate-and-validate) to generate, deploy, and load
the dashboard.

## 4. Remove the example

If you added the Jobs to monitoring, use `./workflow-monitoring help remove` and remove each Job
ID. Repeat [README steps 4 and 5](../../README.md#4-generate-and-validate). Then remove the four
disposable Jobs:

```sh
cd examples/fast-logistics
databricks bundle destroy --auto-approve -t dev --profile <profile>
```

This does not remove the monitoring bundle or collected historical rows.
