"""Candidates and the baselines that need no training.

Every scorer takes the case histories (each session's events up to its cut) and returns
(session, product_id, score) rows; `top_k` turns them into a run. The last product of a
history is never recommended: by the task's definition the target differs from it.

    popular      the most-touched products of the last 7 days before the window
    history      the session's own products, most recent first
    item2item    co-visitation neighbours of the last product only: "people who viewed
                 this also viewed", the standard production baseline
    covis        co-visitation neighbours of the whole history, recent events weighted up
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl

K = 20


def history_items(history: pl.DataFrame) -> pl.DataFrame:
    """One row per (session, product) in a history, with recency rank 0 = last."""
    return (
        history.group_by("session", "product_id")
        .agg(
            pl.len().alias("h_events"),
            pl.col("event_time").max().alias("h_last"),
            pl.col("event_type").max().alias("h_max_type"),
        )
        .with_columns(
            pl.col("h_last")
            .rank("ordinal", descending=True)
            .over("session")
            .cast(pl.Int32)
            .sub(1)
            .alias("h_rank")
        )
    )


def popular(stats: pl.LazyFrame, end, days: int = 7) -> pl.DataFrame:
    """(product_id, pop) over the `days` before `end`, most popular first."""
    return (
        stats.filter(pl.col("event_time") >= end - timedelta(days=days))
        .group_by("product_id")
        .agg(pl.len().alias("pop"))
        .sort(["pop", "product_id"], descending=[True, False])
        .collect()
    )


def last_products(history: pl.DataFrame) -> pl.DataFrame:
    return history.group_by("session").agg(pl.col("product_id").last().alias("last_product"))


def covis_scores(
    items: pl.DataFrame, matrix: pl.DataFrame, only_last: bool = False
) -> pl.DataFrame:
    """Neighbour weights summed over a history, each history product weighted
    1 / (1 + recency rank), each matrix row scaled by its product's best weight."""
    m = matrix.with_columns(
        (pl.col("weight") / pl.col("weight").max().over("product_id")).alias("w")
    )
    src = items.filter(pl.col("h_rank") == 0) if only_last else items
    return (
        src.select("session", "product_id", (1.0 / (1 + pl.col("h_rank"))).alias("r"))
        .join(m.select("product_id", "neighbour", "w"), on="product_id")
        .group_by("session", "neighbour")
        .agg((pl.col("r") * pl.col("w")).sum().alias("score"))
        .rename({"neighbour": "product_id"})
    )


def combined_covis(items: pl.DataFrame, matrices: dict[str, pl.DataFrame], only_last=False):
    parts = [covis_scores(items, m, only_last) for m in matrices.values()]
    return pl.concat(parts).group_by("session", "product_id").agg(pl.col("score").sum())


def top_k(
    scores: pl.DataFrame,
    sessions: pl.DataFrame,
    pop: pl.DataFrame,
    k: int = K,
) -> np.ndarray:
    """A (n_cases, k) run in `sessions` order: each session's best-scored products,
    never its last product, padded with the most popular products it lacks."""
    last = sessions.select("session", "last_product")
    ranked = (
        scores.join(last, on="session")
        .filter(pl.col("product_id") != pl.col("last_product"))
        .sort(["session", "score", "product_id"], descending=[False, True, False])
        .group_by("session", maintain_order=True)
        .head(k)
        .group_by("session", maintain_order=True)
        .agg("product_id")
    )
    filler = pop["product_id"].head(k + 64).to_list()
    by_session = dict(zip(ranked["session"].to_list(), ranked["product_id"].to_list(), strict=True))
    out = np.full((sessions.height, k), -1, dtype=np.int64)
    for i, (s, lastp) in enumerate(zip(sessions["session"], sessions["last_product"], strict=True)):
        row = by_session.get(s, [])
        if len(row) < k:
            seen = set(row) | {lastp}
            row = row + [p for p in filler if p not in seen][: k - len(row)]
        out[i, : len(row)] = row
    return out


def baseline_scores(name: str, history, matrices, pop) -> pl.DataFrame:
    items = history_items(history)
    if name == "popular":
        return pl.DataFrame(
            schema={"session": pl.Int32, "product_id": pl.Int32, "score": pl.Float64}
        )
    if name == "history":
        return items.select(
            "session", "product_id", (-pl.col("h_rank")).cast(pl.Float64).alias("score")
        )
    if name == "item2item":
        return combined_covis(items, matrices, only_last=True)
    if name == "covis":
        return combined_covis(items, matrices)
    raise ValueError(name)
