"""The feature contract shared by the offline pipeline and the online model.

Kept free of polars so the serving image can import it without the data stack.
"""

N_COVIS = 40  # co-visitation candidates kept per session
KINDS = ("time", "type", "buy2buy")

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
