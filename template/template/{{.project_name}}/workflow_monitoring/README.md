# workflow_monitoring

This folder is product-owned. Do not edit these files. Replace the whole folder to upgrade.

`VERSION` records the release that produced these files.

## Upgrade

Create a new copy from the release tag, then replace this folder:

```sh
databricks bundle init https://github.com/overengineered-org/databricks-workflow-monitoring-dashboards \
  --tag <new-tag> \
  --template-dir template \
  --config-file <answers.json> \
  --output-dir /tmp/wm-upgrade

# Then replace workflow_monitoring/ with /tmp/wm-upgrade/<project>/workflow_monitoring/.
```

Keep your configuration. The following user-owned files can need a manual change in a new
version:

- `databricks.yml`
- `workflow-monitoring.yml`
- `resources/*.yml`

Read the release notes for each version. They list the user-owned files that changed.
