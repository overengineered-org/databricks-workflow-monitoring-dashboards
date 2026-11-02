# Fast Logistics example

Deploy the four paused example jobs with step 1. Deployment takes about 2 minutes and does not
start compute.

The jobs create the dashboard's outcome, recovery, late-delivery, and duration scenarios. The
bundle contains no workspace URL, workspace ID, job ID, user name, profile name, or credential.

This example is the only flow that needs a clone of this repository. The monitoring project
comes from `databricks bundle init` in [README step 1](../../README.md#1-create-the-project).

## 1. Deploy paused jobs

Clone this repository, then use the Databricks CLI profile you selected:

```sh
git clone https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard.git
cd databricks-jobs-monitoring-dashboard/examples/fast-logistics
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

Go to your monitoring project folder. Add each Job ID from the bundle summary to
`workflow-monitoring.yml`, as in [README step 3](../../README.md#3-create-the-configuration).
Job names come from the Jobs API.

In the monitoring project, run [README steps 4 and 5](../../README.md#4-validate-the-bundle)
to validate, deploy, and load the dashboard.

## 4. Remove the example

If you added the Jobs to monitoring, remove each Job entry from `workflow-monitoring.yml` in
the monitoring project. Repeat [README steps 4 and 5](../../README.md#4-validate-the-bundle)
there. Then remove the four disposable Jobs. Run this from the folder that holds your clone:

```sh
cd databricks-jobs-monitoring-dashboard/examples/fast-logistics
databricks bundle destroy --auto-approve -t dev --profile <profile>
```

This does not remove the monitoring bundle or collected historical rows.
