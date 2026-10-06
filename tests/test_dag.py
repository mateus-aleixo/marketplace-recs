"""The nightly DAG, with Airflow installed: it parses, wires its four steps in order,
closes the right day, and a run hands each step's result to the next.

Airflow does not run on Windows, so this is skipped there; CI's dags job installs it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

DAGS = Path(__file__).resolve().parent.parent / "dags"
# Set before Airflow reads its config: dag.test() serialises the DAG from this folder.
os.environ.setdefault("AIRFLOW__CORE__DAGS_FOLDER", str(DAGS))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")

pytest.importorskip("airflow")


@pytest.fixture(scope="module")
def dag():
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))  # 3.x: example DAGs follow core.load_examples
    assert bag.import_errors == {}
    return bag.get_dag("nightly_tables")


def test_four_steps_in_order(dag):
    assert [t.task_id for t in dag.topological_sort()] == ["check", "build", "validate", "publish"]


def test_once_a_day_one_run_at_a_time_no_backfill(dag):
    assert type(dag.timetable).__name__ == "CronDataIntervalTimetable"
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_a_run_closes_the_day_that_just_ended_and_hands_each_result_on(dag, monkeypatch):
    """Started at midnight going into 26 October, the run covers the 25th: its data
    interval is the last complete day, and each step receives the previous step's result."""
    from marketplace_recs import pipeline, split

    calls = []
    monkeypatch.setattr(split, "events", lambda *a, **k: "events")
    monkeypatch.setattr(
        pipeline, "check", lambda day, ev: calls.append(("check", f"{day:%Y-%m-%d}"))
    )
    monkeypatch.setattr(
        pipeline, "build", lambda day, ev: calls.append(("build", f"{day:%Y-%m-%d}")) or "v1"
    )
    monkeypatch.setattr(pipeline, "validate", lambda v: calls.append(("validate", v)) or {})
    monkeypatch.setattr(pipeline, "publish", lambda v: calls.append(("publish", v)) or v)
    run = dag.test(logical_date=datetime(2019, 10, 26, tzinfo=UTC))
    assert f"{run.data_interval_start:%Y-%m-%d}" == "2019-10-25"
    assert calls == [
        ("check", "2019-10-25"),
        ("build", "2019-10-25"),
        ("validate", "v1"),
        ("publish", "v1"),
    ]
