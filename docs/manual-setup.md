# Manual setup

Use this page to deploy without the `workflow-monitoring` CLI. You need only the Databricks CLI
1.22.1 or later and a signed-in profile. The [README](../README.md#start-here) shows the shorter
path with the CLI.

## 1. Create the project

```sh
databricks bundle init https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard \
  --tag v0.4.0 \
  --template-dir template \
  --output-dir .
cd workflow_monitoring
```

The prompts are the same as in [README step 3](../README.md#3-install-the-cli-and-create-the-project).
Change the `cd` command if you choose another project folder name.

## 2. Get the IDs

```sh
databricks auth profiles
databricks warehouses list --profile <profile>
databricks jobs list --profile <profile>
```

Keep the SQL warehouse ID and each Job ID you want to monitor.

## 3. Save the warehouse ID

Save the warehouse ID for each target in the local file that the Databricks CLI reads. Git
ignores the `.databricks/` folder.

```sh
mkdir -p .databricks/bundle/dev .databricks/bundle/prod
printf '{"warehouse_id": "%s"}\n' <warehouse-id> > .databricks/bundle/dev/variable-overrides.json
cp .databricks/bundle/dev/variable-overrides.json .databricks/bundle/prod/
```

You can also pass `--var="warehouse_id=<warehouse-id>"` to each bundle command.

## 4. Add the Jobs

Edit `workflow-monitoring.yml` and add one entry for each Job:

```yaml
workflows:
  - job_id: <job-id>
    monitoring_status: active
    sla:
      frequency: daily
      completion_time: "06:00"
```

The `yaml-language-server` schema comment at the top of the file gives editor checks while you
type. See [Choose an SLA schedule](../README.md#choose-an-sla-schedule) for the other
frequencies.

## 5. Validate, deploy, and load the first data

`bundle validate` checks the bundle files, not `workflow-monitoring.yml`. The collector run checks
that file and lists every error.

The deploy and run commands create workspace resources and run serverless compute:

```sh
databricks bundle validate --strict -t dev --profile <profile>
databricks bundle deploy -t dev --profile <profile>
databricks bundle run workflow_monitoring_jobs_api_collector -t dev --profile <profile>
```

## 6. Deploy to production

Complete the checklist in [README step 5](../README.md#5-deploy-to-production), then run:

```sh
databricks bundle validate --strict -t prod --profile <profile>
databricks bundle deploy -t prod --profile <profile>
```

## Apply a change

After you edit `workflow-monitoring.yml`, run `databricks bundle deploy`. In dev, also run the
`bundle run` command from step 5, because development mode pauses the collector schedule. In
prod, the next scheduled run applies the change.
