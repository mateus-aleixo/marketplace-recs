"""Candidates and features for the ranker.

Candidates are the session's own products (except the last) and the 40 best
co-visitation neighbours of its history. Each (session, candidate) row gets:

    covis     per matrix, the recency-weighted score summed over the history and the
              score from the last product alone
    history   whether the candidate is already in the session, how many events, its
              recency rank, seconds since it was last touched, its strongest event type
    product   events over the last day and the last 7 days, carts and purchases over 7
              days, its price against the last product's, same category, same brand
    session   events and distinct products so far, seconds since the session started

Product statistics come from the events before the window, like the matrices.
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from .candidates import covis_scores, history_items

N_COVIS = 40
KINDS = ("time", "type", "buy2buy")


def product_stats(stats: pl.LazyFrame, end) -> pl.DataFrame:
    recent = stats.filter(pl.col("event_time") >= end - timedelta(days=7))
    counts = recent.group_by("product_id").agg(
        pl.len().alias("p_events_7d"),
        (pl.col("event_time") >= end - timedelta(days=1)).sum().alias("p_events_1d"),
        (pl.col("event_type") == 1).sum().alias("p_carts_7d"),
        (pl.col("event_type") == 2).sum().alias("p_purchases_7d"),
    )
    attrs = stats.group_by("product_id").agg(
        pl.col("price").last().alias("p_price"),
        pl.col("category_id").last().alias("p_category"),
        pl.col("brand").last().alias("p_brand"),
    )
    return attrs.join(counts, on="product_id", how="left").collect()


def candidates(items: pl.DataFrame, matrices: dict[str, pl.DataFrame], last: pl.DataFrame):
    """(session, product_id) candidate rows with their co-visitation scores."""
    scores = None
    for kind in KINDS:
        for only_last, tag in ((False, "all"), (True, "last")):
            s = covis_scores(items, matrices[kind], only_last).rename({"score": f"c_{kind}_{tag}"})
            scores = (
                s
                if scores is None
                else scores.join(s, on=["session", "product_id"], how="full", coalesce=True)
            )
    scores = scores.with_columns(pl.col("^c_.*$").fill_null(0.0)).with_columns(
        sum(pl.col(f"c_{k}_all") for k in KINDS).alias("c_sum")
    )
    top = (
        scores.join(last, on="session")
        .filter(pl.col("product_id") != pl.col("last_product"))
        .sort(["session", "c_sum"], descending=[False, True])
        .group_by("session", maintain_order=True)
        .head(N_COVIS)
        .select("session", "product_id")
    )
    own = (
        items.join(last, on="session")
        .filter(pl.col("product_id") != pl.col("last_product"))
        .select("session", "product_id")
    )
    cands = pl.concat([top, own]).unique()
    return cands.join(scores, on=["session", "product_id"], how="left").with_columns(
        pl.col("^c_.*$").fill_null(0.0)
    )


def build(
    history: pl.DataFrame,
    cases: pl.DataFrame,
    matrices: dict[str, pl.DataFrame],
    products: pl.DataFrame,
) -> pl.DataFrame:
    items = history_items(history)
    last = cases.select("session", "last_product", "cut_time", "target")
    df = candidates(items, matrices, last.select("session", "last_product"))
    session = history.group_by("session").agg(
        pl.len().alias("s_events"),
        pl.col("product_id").n_unique().alias("s_products"),
        pl.col("event_time").min().alias("s_start"),
    )
    last_attrs = (
        history.sort("session", "event_time")
        .group_by("session", maintain_order=True)
        .last()
        .select(
            "session",
            pl.col("price").alias("l_price"),
            pl.col("category_id").alias("l_category"),
            pl.col("brand").alias("l_brand"),
            pl.col("event_type").alias("l_type"),
        )
    )
    df = (
        df.join(items, on=["session", "product_id"], how="left")
        .join(products, on="product_id", how="left")
        .join(last, on="session")
        .join(session, on="session")
        .join(last_attrs, on="session")
        .with_columns(
            pl.col("h_events").is_not_null().cast(pl.Int8).alias("in_history"),
            (pl.col("cut_time") - pl.col("h_last")).dt.total_seconds().alias("h_seconds"),
            (pl.col("cut_time") - pl.col("s_start")).dt.total_seconds().alias("s_seconds"),
            (pl.col("p_price") / pl.col("l_price")).alias("price_ratio"),
            (pl.col("p_category") == pl.col("l_category")).cast(pl.Int8).alias("same_category"),
            (pl.col("p_brand") == pl.col("l_brand")).cast(pl.Int8).alias("same_brand"),
            (pl.col("product_id") == pl.col("target")).cast(pl.Int8).alias("label"),
        )
    )
    return df.select("session", "product_id", "label", *FEATURES)


FEATURES = [
    *[f"c_{k}_{t}" for k in KINDS for t in ("all", "last")],
    "c_sum",
    "in_history",
    "h_events",
    "h_rank",
    "h_seconds",
    "h_max_type",
    "p_events_1d",
    "p_events_7d",
    "p_carts_7d",
    "p_purchases_7d",
    "p_price",
    "price_ratio",
    "same_category",
    "same_brand",
    "s_events",
    "s_products",
    "s_seconds",
    "l_type",
]
