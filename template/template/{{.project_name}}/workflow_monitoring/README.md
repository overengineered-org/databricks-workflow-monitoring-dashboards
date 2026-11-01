# workflow_monitoring

This folder is product-owned. Do not edit these files. Replace the whole folder to upgrade.

`VERSION` records the release that produced these files.

## Upgrade

This folder has no `.tmpl` files, so the prompt answers do not change it. Any valid answers work.
The `project_name` answer sets the output folder name.

Create a new copy from the release tag, then replace this folder:

```sh
printf '%s\n' '{"project_name": "workflow_monitoring"}' > /tmp/wm-upgrade-answers.json

databricks bundle init https://github.com/overengineered-org/databricks-jobs-monitoring-dashboard \
  --tag <new-tag> \
  --template-dir template \
  --config-file /tmp/wm-upgrade-answers.json \
  --output-dir /tmp/wm-upgrade

# Then replace workflow_monitoring/ with /tmp/wm-upgrade/workflow_monitoring/workflow_monitoring/.
```

You can also omit `--config-file` and accept the defaults.

Keep your configuration. The following user-owned files can need a manual change in a new
version:

- `databricks.yml`
- `workflow-monitoring.yml`
- `resources/*.yml`

Read the release notes for each version. They list the user-owned files that changed.
