"""Co-visitation: the products sessions touch together, as candidate generators.

Three matrices, each built only from the events before the window it serves:

    time     two products touched within 24 hours in one session, weighted from 1 to 4
             by how late in the period the pair happened, since a month of stock and
             prices drifts
    type     the same pairs weighted by what happened to the second product: a view 1, a
             cart 6, a purchase 3
    buy2buy  carts and purchases only, within 14 days

A session contributes each pair once, and only its 30 most recent distinct (product,
event type) events take part, so a bot paging through thousands of products cannot
dominate a matrix. Each matrix keeps the top 20 neighbours of every product.
"""

from __future__ import annotations

import polars as pl

TOP = 20
PER_SESSION = 30
TYPE_WEIGHT = {0: 1.0, 1: 6.0, 2: 3.0}
DAY = 86_400


def prepare(lf: pl.LazyFrame) -> pl.DataFrame:
    """The events that take part: deduplicated, the 30 most recent per session."""
    return (
        lf.select(
            "session", pl.col("event_time").dt.epoch("s").alias("ts"), "event_type", "product_id"
        )
        .collect()
        .unique(["session", "product_id", "event_type"], keep="last")
        .sort(["session", "ts"], descending=[False, True])
        .filter(pl.int_range(pl.len()).over("session") < PER_SESSION)
    )


def _weight(kind: str, t0: int, t1: int) -> pl.Expr:
    if kind == "time":
        return 1.0 + 3.0 * (pl.col("ts") - t0) / max(t1 - t0, 1)
    if kind == "type":
        return pl.col("event_type_b").replace_strict(TYPE_WEIGHT, return_dtype=pl.Float64)
    return pl.lit(1.0)


def matrix(df: pl.DataFrame, kind: str, parts: int = 4, top: int = TOP) -> pl.DataFrame:
    """(product_id, neighbour, weight), the top `top` neighbours of each product."""
    if kind == "buy2buy":
        df = df.filter(pl.col("event_type") > 0)
    gap = 14 * DAY if kind == "buy2buy" else DAY
    t0, t1 = int(df["ts"].min()), int(df["ts"].max())
    partials = []
    for p in range(parts):
        chunk = df.filter(pl.col("session") % parts == p)
        pairs = (
            chunk.join(chunk, on="session", suffix="_b")
            .filter(
                (pl.col("product_id") != pl.col("product_id_b"))
                & ((pl.col("ts") - pl.col("ts_b")).abs() < gap)
            )
            .group_by("session", "product_id", "product_id_b")
            .agg(_weight(kind, t0, t1).max().alias("w"))  # one count per session
        )
        partials.append(pairs.group_by("product_id", "product_id_b").agg(pl.col("w").sum()))
    return (
        pl.concat(partials)
        .group_by("product_id", "product_id_b")
        .agg(pl.col("w").sum())
        .sort(["product_id", "w", "product_id_b"], descending=[False, True, False])
        .group_by("product_id", maintain_order=True)
        .head(top)
        .rename({"product_id_b": "neighbour", "w": "weight"})
        .with_columns(pl.col("weight").cast(pl.Float32))
    )


def build(lf: pl.LazyFrame, parts: int = 4) -> dict[str, pl.DataFrame]:
    df = prepare(lf)
    return {kind: matrix(df, kind, parts) for kind in ("time", "type", "buy2buy")}
