# Security policy

## Report a vulnerability

Do not open a public issue for a security problem. Report it privately on GitHub:

1. Open the repository's **Security** tab.
2. Select **Report a vulnerability**.
3. Describe the problem, the affected version, and the steps to reproduce it.

Do not include real workspace URLs, tokens, or other credentials in the report. Use fake values.

The maintainers answer as soon as they can and agree a disclosure date with you.

## Supported versions

Only the latest release gets security fixes. Upgrade with `git pull`, as the
[README](README.md#upgrade) describes.

## Scope

In scope:

- The collector in `src/collect_workflow_monitoring_jobs_api.py` and its config rules
- The `workflow-monitoring` wrapper in `src/workflow_monitoring_cli.py`
- The bundle definition in `databricks.yml` and `resources/`
- The GitHub workflows in `.github/workflows/`

Out of scope: Databricks itself, the Databricks CLI, and your workspace configuration. Report
those to Databricks.
