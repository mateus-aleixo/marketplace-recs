"""Every night: rebuild the recommendation tables from the day's events, gate them, and
hand them to the API.

Each run covers one day, data_interval_start to data_interval_end, and starts once that
day is over. The steps live in marketplace_recs.pipeline, where they are tested without
Airflow; this file schedules them and passes each step's result to the next.

    check  ->  build  ->  validate  ->  publish

A failed gate stops the run before publish, so the API keeps serving yesterday's tables.
With RECS_WAREHOUSE=bigquery in the worker's environment, check and build run their SQL
in BigQuery instead of reading the Parquet file on the worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from airflow.sdk import dag, get_current_context, task
from airflow.timetables.interval import CronDataIntervalTimetable


@dag(
    # Explicit, because Airflow 3 reads a bare "@daily" as a trigger: data_interval_start
    # would be the moment the run starts, and the job would rebuild from a day that has
    # not happened yet. With a data interval, a run covers the day that just ended.
    schedule=CronDataIntervalTimetable("@daily", timezone="UTC"),
    start_date=datetime(2019, 10, 24, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["marketplace-recs"],
)
def nightly_tables():
    @task
    def check() -> str:
        from marketplace_recs import pipeline

        day = get_current_context()["data_interval_start"]
        print(pipeline.check_night(day))
        return f"{day:%Y-%m-%d}"

    @task
    def build(day: str) -> str:
        from marketplace_recs import pipeline

        return pipeline.build_night(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC))

    @task
    def validate(version: str) -> str:
        from marketplace_recs import pipeline

        print(pipeline.validate(version))
        return version

    @task
    def publish(version: str) -> str:
        from marketplace_recs import pipeline

        return pipeline.publish(version)

    publish(validate(build(check())))


nightly_tables()
