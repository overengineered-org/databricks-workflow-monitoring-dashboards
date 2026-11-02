# Manual setup

Use this page to deploy with the Databricks CLI only, without the `workflow-monitoring` wrapper.
Complete [README steps 1 to 3](../README.md#start-here) first: install the tools, sign in, and
clone this repository. Run every command below from the repository root.

## 1. Set your values

Replace the example values with your profile name and SQL warehouse ID. The commands below read
these shell variables.

```sh
PROFILE=DEFAULT
WAREHOUSE_ID=1234567890abcdef
```

To find them:

```sh
databricks auth profiles
databricks warehouses list --profile "$PROFILE"
databricks jobs list --profile "$PROFILE"
```

## 2. Save the warehouse ID

The Databricks CLI reads this local file for each target. Git ignores the `.databricks/` folder.

```sh
mkdir -p .databricks/bundle/dev .databricks/bundle/prod
printf '{"warehouse_id": "%s"}\n' "$WAREHOUSE_ID" > .databricks/bundle/dev/variable-overrides.json
cp .databricks/bundle/dev/variable-overrides.json .databricks/bundle/prod/
```

## 3. Add the Jobs

Edit `workflow-monitoring.yml` and add one entry for each Job ID:

```yaml
workflows:
  - job_id: 123456
    monitoring_status: active
    sla:
      frequency: daily
      completion_time: "06:00"
```

The `yaml-language-server` schema comment at the top of the file gives editor checks while you
type. See [Choose an SLA schedule](../README.md#choose-an-sla-schedule) for the other
frequencies.

## 4. Validate, deploy, and load the first data

`bundle validate` checks the bundle files, not `workflow-monitoring.yml`. The collector run checks
that file and lists every error.

The deploy and run commands create workspace resources and run serverless compute:

```sh
databricks bundle validate --strict -t dev --profile "$PROFILE"
databricks bundle deploy -t dev --profile "$PROFILE"
databricks bundle run workflow_monitoring_jobs_api_collector -t dev --profile "$PROFILE"
```

## 5. Deploy to production

Complete the checklist in [README step 6](../README.md#6-deploy-to-production), then run:

```sh
databricks bundle validate --strict -t prod --profile "$PROFILE"
databricks bundle deploy -t prod --profile "$PROFILE"
```

## Apply a change

After you edit `workflow-monitoring.yml`, run `databricks bundle deploy`. In dev, also run the
`bundle run` command from step 4, because development mode pauses the collector schedule. In
prod, the next scheduled run applies the change.
