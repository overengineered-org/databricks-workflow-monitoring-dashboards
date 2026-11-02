# Fast Logistics example

Deploy the four paused example jobs with step 1. Deployment takes about 2 minutes and does not
start compute.

The jobs create the dashboard's outcome, recovery, late-delivery, and duration scenarios. The
bundle contains no workspace URL, workspace ID, job ID, user name, profile name, or credential.

Run these commands in your clone of this repository. Set your profile name first:

```sh
PROFILE=DEFAULT
```

## 1. Deploy paused jobs

```sh
cd examples/fast-logistics
databricks bundle validate --strict -t dev --profile "$PROFILE"
databricks bundle deploy -t dev --profile "$PROFILE"
cd ../..
```

All schedules remain paused.

## 2. Run one scenario

Trigger only the scenario you need. Run these commands in `examples/fast-logistics`:

```sh
databricks bundle run fast_logistics_on_time -t dev --profile "$PROFILE"
```

The recovery scenario fails first, then succeeds on retry:

```sh
databricks bundle run fast_logistics_recovery -t dev --profile "$PROFILE" --params run_mode=scheduled
databricks bundle run fast_logistics_recovery -t dev --profile "$PROFILE" --params run_mode=retry
```

All jobs use serverless compute and have a 60-second timeout. Running a job can start compute.

## 3. Add the jobs to monitoring

From the repository root, tick the four Fast Logistics Jobs, then deploy:

```sh
uv run workflow-monitoring select-jobs --profile "$PROFILE" --name-contains "Fast Logistics"
uv run workflow-monitoring deploy --profile "$PROFILE"
```

## 4. Remove the example

If you added the Jobs to monitoring, remove each Fast Logistics entry from
`workflow-monitoring.yml`, then run `uv run workflow-monitoring deploy` again. Then remove the four
disposable Jobs:

```sh
cd examples/fast-logistics
databricks bundle destroy --auto-approve -t dev --profile "$PROFILE"
```

This does not remove the monitoring bundle or collected historical rows.
