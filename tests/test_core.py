"""Splits, co-visitation, candidates and metrics on data small enough to check by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from marketplace_recs import candidates as cand
from marketplace_recs.covisit import TYPE_WEIGHT, matrix
from marketplace_recs.metrics import evaluate, target_rank
from marketplace_recs.split import runs

T0 = datetime(2019, 10, 1, tzinfo=UTC)


def events(rows):
    """rows: (session, minutes after T0, event_type, product_id)."""
    return pl.DataFrame(
        {
            "session": [r[0] for r in rows],
            "event_time": [T0 + timedelta(minutes=r[1]) for r in rows],
            "event_type": [r[2] for r in rows],
            "product_id": [r[3] for r in rows],
        },
        schema={
            "session": pl.Int32,
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
        },
    )


# -- metrics -------------------------------------------------------------------


def test_target_rank_and_metrics_by_hand():
    run = np.array([[5, 6, 7], [8, 9, 10], [11, 12, 13]])
    target = np.array([5, 10, 99])
    assert target_rank(run, target).tolist() == [1, 3, 0]
    m = evaluate(run, target)
    assert m["recall@20"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["mrr@20"] == pytest.approx((1 + 1 / 3) / 3, abs=1e-4)
    assert m["ndcg@10"] == pytest.approx((1 + 1 / np.log2(4)) / 3, abs=1e-4)


# -- split ---------------------------------------------------------------------


def test_runs_give_the_next_different_product():
    df = runs(events([(1, 0, 0, 10), (1, 1, 0, 10), (1, 2, 0, 20), (1, 3, 1, 20), (1, 4, 0, 30)]))
    # a cut after either 10 predicts 20, after either 20 predicts 30, after 30 nothing
    assert df["target"].to_list() == [20, 20, 30, 30, None]


def test_runs_do_not_cross_sessions():
    df = runs(events([(1, 0, 0, 10), (1, 1, 0, 20), (2, 2, 0, 30), (2, 3, 0, 40)]))
    assert df["target"].to_list() == [20, None, 40, None]


# -- co-visitation ---------------------------------------------------------------


def prepared(rows):
    return events(rows).select(
        "session", pl.col("event_time").dt.epoch("s").alias("ts"), "event_type", "product_id"
    )


def test_pairs_outside_24_hours_do_not_count():
    df = prepared([(1, 0, 0, 10), (1, 30, 0, 20), (1, 25 * 60, 0, 30)])
    m = matrix(df, "type", parts=1)
    pairs = set(zip(m["product_id"].to_list(), m["neighbour"].to_list(), strict=True))
    assert pairs == {(10, 20), (20, 10)}


def test_a_session_counts_a_pair_once_and_type_weights_the_neighbour():
    # session 1 views 10 twice and carts 20; session 2 views 10 and buys 20
    df = prepared([(1, 0, 0, 10), (1, 1, 0, 10), (1, 2, 1, 20), (2, 0, 0, 10), (2, 1, 2, 20)])
    m = matrix(df, "type", parts=2)
    w = m.filter((pl.col("product_id") == 10) & (pl.col("neighbour") == 20))["weight"][0]
    assert w == pytest.approx(TYPE_WEIGHT[1] + TYPE_WEIGHT[2])


# -- candidates ------------------------------------------------------------------


def test_top_k_never_returns_the_last_product_and_pads_with_popular():
    cases = pl.DataFrame(
        {"session": [1, 2], "last_product": [10, 30]},
        schema={"session": pl.Int32, "last_product": pl.Int32},
    )
    scores = pl.DataFrame(
        {"session": [1, 1, 1], "product_id": [10, 20, 21], "score": [9.0, 5.0, 7.0]},
        schema={"session": pl.Int32, "product_id": pl.Int32, "score": pl.Float64},
    )
    pop = pl.DataFrame({"product_id": [30, 20, 40, 50], "pop": [9, 8, 7, 6]})
    run = cand.top_k(scores, cases, pop, k=4)
    assert run[0].tolist() == [21, 20, 30, 40]  # 10 is its last product; 20 is not repeated
    assert run[1].tolist() == [20, 40, 50, -1]  # no scores: popular, minus its own 30


def test_history_recency_rank_starts_at_zero_for_the_last_product():
    # 20 and the second 10 share a timestamp: position, not time, decides which is last
    ev = events([(1, 0, 0, 10), (1, 9, 0, 20), (1, 9, 1, 10)])
    items = cand.history_items(ev.with_columns(pl.int_range(pl.len()).cast(pl.Int32).alias("pos")))
    ranks = dict(zip(items["product_id"].to_list(), items["h_rank"].to_list(), strict=True))
    assert ranks == {10: 0, 20: 1}
