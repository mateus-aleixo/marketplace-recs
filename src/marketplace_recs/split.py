"""Time windows and test cases.

Three consecutive windows, so nothing a model is fitted on comes from the period it is
scored on:

    A   Oct 1 to Oct 17    candidate statistics for the ranker's training sessions
    B   Oct 17 to Oct 24   sessions the ranker learns from
    C   Oct 24 to Nov 1    test sessions, scored with statistics from A and B

A case is a session prefix. Each sampled session that starts inside a window is cut once,
at a seeded random point: the history is the events up to the cut, and the target is the
first product after it that differs from the last product in the history. Repeated views
of one page are one step, so a model cannot score by predicting a reload.

A session belongs to the window it starts in and keeps all its events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

EVENTS = Path("data") / "events-2019-Oct.parquet"


def utc(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)


WINDOWS = {
    "A": (utc("2019-10-01"), utc("2019-10-17")),
    "B": (utc("2019-10-17"), utc("2019-10-24")),
    "C": (utc("2019-10-24"), utc("2019-11-01")),
}
# The statistics each window's cases are scored with: everything before the window.
STATS_FOR = {"B": ("A",), "C": ("A", "B")}


def events(path: Path = EVENTS) -> pl.LazyFrame:
    return pl.scan_parquet(path).filter(pl.col("session").is_not_null())


def between(lf: pl.LazyFrame, start: datetime, end: datetime) -> pl.LazyFrame:
    return lf.filter((pl.col("event_time") >= start) & (pl.col("event_time") < end))


def stats_events(window: str, lf: pl.LazyFrame | None = None) -> pl.LazyFrame:
    """Every event before `window` starts: what its cases may be scored with."""
    lf = events() if lf is None else lf
    return lf.filter(pl.col("event_time") < WINDOWS[window][0])


def runs(df: pl.DataFrame) -> pl.DataFrame:
    """Per event, the next different product in its session: the target if the
    session were cut right after this event. Null in the session's last run."""
    df = df.sort("session", "event_time", maintain_order=True)
    run = (pl.col("product_id") != pl.col("product_id").shift(1).over("session")).fill_null(True)
    df = df.with_columns(run.cum_sum().over("session").alias("run"))
    heads = df.group_by("session", "run").agg(pl.col("product_id").first().alias("run_product"))
    nxt = heads.with_columns((pl.col("run") - 1).alias("run")).rename({"run_product": "target"})
    return df.join(nxt, on=["session", "run"], how="left", maintain_order="left")


def cases(
    window: str, n: int, seed: int, lf: pl.LazyFrame | None = None
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(history events, one row per case) for up to n sessions starting in `window`."""
    lf = events() if lf is None else lf
    start, end = WINDOWS[window]
    # Only sessions that touch two distinct products leave anything to predict: about
    # half of all sessions view a single product and leave.
    per = lf.group_by("session").agg(
        pl.col("event_time").min().alias("first"), pl.col("product_id").n_unique().alias("products")
    )
    sessions = (
        per.filter((pl.col("first") >= start) & (pl.col("first") < end) & (pl.col("products") >= 2))
        .select("session")
        .sort("session")
        .collect()
    )
    sessions = sessions.sample(n=min(n, sessions.height), seed=seed)
    ev = lf.join(sessions.lazy(), on="session", how="semi").collect()
    ev = runs(ev).with_row_index("row")
    # One cut per session, uniformly among the events that leave something to predict.
    valid = ev.filter(pl.col("target").is_not_null())
    pick = (
        valid.with_columns(pl.int_range(pl.len()).shuffle(seed=seed).alias("u"))
        .sort("u")
        .group_by("session", maintain_order=True)
        .first()
        .select(
            "session",
            pl.col("row").alias("cut_row"),
            pl.col("event_time").alias("cut_time"),
            pl.col("product_id").alias("last_product"),
            "target",
        )
        .sort("session")
    )
    history = (
        ev.join(pick.select("session", "cut_row"), on="session", how="inner")
        .filter(pl.col("row") <= pl.col("cut_row"))
        .drop("row", "cut_row", "run", "target")
    )
    return history, pick.drop("cut_row")
