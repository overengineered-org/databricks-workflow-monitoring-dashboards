# Databricks notebook source
"""Small, intentionally non-production workflow used by the Fast Logistics example."""

import time

dbutils.widgets.text("scenario", "on_time")  # noqa: F821
dbutils.widgets.text("run_mode", "scheduled")  # noqa: F821
workflow_scenario = dbutils.widgets.get("scenario")  # noqa: F821
workflow_run_mode = dbutils.widgets.get("run_mode")  # noqa: F821

if workflow_scenario == "recovery" and workflow_run_mode == "scheduled":
    raise RuntimeError("Intentional recovery failure. Run the job manually to demonstrate recovery.")

if workflow_scenario == "duration":
    time.sleep(20)
else:
    time.sleep(5)

print({"scenario": workflow_scenario, "run_mode": workflow_run_mode, "status": "completed"})
