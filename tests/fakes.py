"""Test doubles shared by the collector test modules."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace


class ExpectedMissingRun(Exception):
    """Stand in for the Jobs SDK's ResourceDoesNotExist error."""


class StoredRunStateDataFrame:
    """Apply the collector's Spark filters to in-memory run rows."""

    def __init__(self, run_state_rows: list[SimpleNamespace]) -> None:
        self.rows = run_state_rows

    def where(self, predicate: str) -> StoredRunStateDataFrame:
        if predicate.startswith("workspace_id ="):
            workspace_id = predicate.split("'")[1]
            self.rows = [row for row in self.rows if row.workspace_id == workspace_id]
        elif predicate.startswith("job_id ="):
            job_id = predicate.split("'")[1]
            self.rows = [row for row in self.rows if row.job_id == job_id]
        elif predicate.startswith("period_start_time >= TIMESTAMP"):
            run_history_start_at = datetime.fromisoformat(predicate.split("'")[1])
            self.rows = [row for row in self.rows if row.period_start_time >= run_history_start_at]
        elif predicate.startswith("period_start_time < TIMESTAMP"):
            incremental_start_at = datetime.fromisoformat(predicate.split("'")[1])
            self.rows = [row for row in self.rows if row.period_start_time < incremental_start_at]
        elif predicate.startswith("result_state NOT IN"):
            terminal_states = set(re.findall(r"'([A-Z_]+)'", predicate))
            self.rows = [row for row in self.rows if row.result_state not in terminal_states]
        return self

    def select(self, *_column_names: str) -> StoredRunStateDataFrame:
        return self

    def toLocalIterator(self) -> Iterator[SimpleNamespace]:
        yield from self.rows
