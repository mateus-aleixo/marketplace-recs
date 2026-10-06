"""The ranker for one live session, without polars or batch joins.

The offline pipeline builds features with joins over hundreds of thousands of sessions
at once, which is the wrong shape for a request. This module computes the same 24
features for one session from compact arrays: neighbour lists in CSR form and
per-product statistics, all indexed by a dense product index. A feature that differed
between the two paths would not raise; it would quietly rank worse in production than
offline. `python -m marketplace_recs.online parity` measures the two against each other
on real test sessions, and tests/test_online.py does the same on synthetic data in CI.

    python -m marketplace_recs.online export      # model/: arrays + ranker, from window C
    python -m marketplace_recs.online parity      # offline against online on 5,000 sessions
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .spec import FEATURES, KINDS, N_COVIS

SERVE = Path("model")  # gitignored; the Docker image copies it in
TOP_POPULAR = 200


@dataclass(frozen=True)
class Event:
    time: int  # epoch seconds
    event_type: int  # 0 view, 1 cart, 2 purchase
    product_id: int
    category_id: int | None = None
    brand: str | None = None
    price: float | None = None


class Model:
    """Everything a request needs, loaded once."""

    def __init__(self, directory: Path = SERVE, booster=None):
        d = Path(directory)
        self.products = np.load(d / "products.npy")
        self.index = {int(p): i for i, p in enumerate(self.products)}
        self.csr = {
            k: (
                np.load(d / f"{k}_indptr.npy"),
                np.load(d / f"{k}_nbr.npy"),
                np.load(d / f"{k}_w.npy"),
            )
            for k in KINDS
        }
        with np.load(d / "stats.npz") as z:
            self.stats = {k: z[k] for k in z.files}
        self.brands = json.loads((d / "brands.json").read_text(encoding="utf-8"))
        self.brand_code = {b: i for i, b in enumerate(self.brands)}
        self.popular = np.load(d / "popular.npy").tolist()
        if booster is None:
            import lightgbm as lgb

            booster = lgb.Booster(model_file=str(d / "ranker.txt"))
        self.booster = booster

    # -- features ---------------------------------------------------------------

    def features(self, events: list[Event]) -> tuple[list[int], np.ndarray]:
        """Candidates in product-id order, and their (n, 24) float32 feature matrix."""
        if not events:
            return [], np.zeros((0, len(FEATURES)), dtype=np.float32)
        hist: dict[int, list] = {}  # product -> [events, last_time, last_pos, max_type]
        for pos, e in enumerate(events):
            h = hist.setdefault(e.product_id, [0, e.time, pos, e.event_type])
            h[0] += 1
            h[1] = max(h[1], e.time)
            h[2] = pos
            h[3] = max(h[3], e.event_type)
        order = sorted(hist, key=lambda p: -hist[p][2])
        rank = {p: r for r, p in enumerate(order)}
        last = events[-1]
        lastp = last.product_id

        scores: dict[str, dict[int, float]] = {}
        for k in KINDS:
            indptr, nbr, w = self.csr[k]
            s_all: dict[int, float] = {}
            s_last: dict[int, float] = {}
            for p, r in rank.items():
                i = self.index.get(p)
                if i is None:
                    continue
                a, b = indptr[i], indptr[i + 1]
                rw = 1.0 / (1 + r)
                for q, wq in zip(nbr[a:b].tolist(), w[a:b].tolist(), strict=True):
                    s_all[q] = s_all.get(q, 0.0) + rw * wq
                    if r == 0:
                        s_last[q] = s_last.get(q, 0.0) + rw * wq
            scores[f"c_{k}_all"], scores[f"c_{k}_last"] = s_all, s_last

        pool = set().union(*(scores[f"c_{k}_all"] for k in KINDS)) - {lastp}

        def c_sum(q: int) -> float:
            total = 0
            for k in KINDS:
                total = total + scores[f"c_{k}_all"].get(q, 0.0)
            return total

        top = sorted(pool, key=lambda q: (-c_sum(q), q))[:N_COVIS]
        cands = sorted(set(top) | (set(hist) - {lastp}))

        st = self.stats
        l_cat = last.category_id
        l_brand = self.brand_code.get(last.brand) if last.brand is not None else None
        l_price = last.price
        s_start = min(e.time for e in events)
        X = np.full((len(cands), len(FEATURES)), np.nan, dtype=np.float64)
        col = {f: j for j, f in enumerate(FEATURES)}
        for row, q in enumerate(cands):
            x = X[row]
            for k in KINDS:
                x[col[f"c_{k}_all"]] = scores[f"c_{k}_all"].get(q, 0.0)
                x[col[f"c_{k}_last"]] = scores[f"c_{k}_last"].get(q, 0.0)
            x[col["c_sum"]] = c_sum(q)
            h = hist.get(q)
            x[col["in_history"]] = 1.0 if h else 0.0
            if h:
                x[col["h_events"]] = h[0]
                x[col["h_rank"]] = rank[q]
                x[col["h_seconds"]] = last.time - h[1]
                x[col["h_max_type"]] = h[3]
            i = self.index.get(q)
            if i is not None and st["known"][i]:
                for f in ("p_events_1d", "p_events_7d", "p_carts_7d", "p_purchases_7d", "p_price"):
                    x[col[f]] = st[f][i]
                if l_price is not None and not np.isnan(st["p_price"][i]):
                    with np.errstate(divide="ignore", invalid="ignore"):  # as polars: inf or NaN
                        x[col["price_ratio"]] = np.float32(st["p_price"][i]) / np.float32(l_price)
                if l_cat is not None and st["p_category"][i] >= 0:
                    x[col["same_category"]] = float(st["p_category"][i] == l_cat)
                if l_brand is not None and st["p_brand"][i] >= 0:
                    x[col["same_brand"]] = float(st["p_brand"][i] == l_brand)
                elif last.brand is not None and st["p_brand"][i] >= 0:
                    x[col["same_brand"]] = 0.0  # a brand the catalogue has never seen
            x[col["s_events"]] = len(events)
            x[col["s_products"]] = len(hist)
            x[col["s_seconds"]] = last.time - s_start
            x[col["l_type"]] = last.event_type
        return cands, X.astype(np.float32)

    def recommend(self, events: list[Event], k: int = 20) -> list[tuple[int, float]]:
        cands, X = self.features(events)
        ranked: list[tuple[int, float]] = []
        if cands:
            pred = self.booster.predict(X)
            ranked = sorted(zip(cands, pred.tolist(), strict=True), key=lambda t: (-t[1], t[0]))[:k]
        if len(ranked) < k:
            seen = {p for p, _ in ranked} | ({events[-1].product_id} if events else set())
            ranked += [(p, 0.0) for p in self.popular if p not in seen][: k - len(ranked)]
        return ranked


# -- export ------------------------------------------------------------------------


def write_artifacts(out: Path, matrices, stats, popular: list[int], ranker_file: Path) -> dict:
    """Write everything Model loads: matrices as CSR, product statistics as aligned
    arrays, the popular list and the ranker. `stats` is features.product_stats output."""
    import polars as pl

    out.mkdir(parents=True, exist_ok=True)
    ids = set(stats["product_id"].to_list())
    for m in matrices.values():
        ids |= set(m["product_id"].to_list()) | set(m["neighbour"].to_list())
    products = np.array(sorted(ids), dtype=np.int64)
    np.save(out / "products.npy", products)
    index = pl.DataFrame({"product_id": products, "i": np.arange(len(products))}).with_columns(
        pl.col("product_id").cast(pl.Int32)
    )
    for kind, m in matrices.items():
        # The same normalisation the offline scorer applies: each row over its product's best.
        m = m.with_columns(
            (pl.col("weight") / pl.col("weight").max().over("product_id")).alias("w")
        )
        m = m.join(index, on="product_id").sort("i", "neighbour")
        counts = np.bincount(m["i"].to_numpy(), minlength=len(products))
        indptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        np.save(out / f"{kind}_indptr.npy", indptr)
        np.save(out / f"{kind}_nbr.npy", m["neighbour"].to_numpy().astype(np.int32))
        np.save(out / f"{kind}_w.npy", m["w"].to_numpy())
    brands = sorted(b for b in stats["p_brand"].unique().to_list() if b is not None)
    code = {b: i for i, b in enumerate(brands)}
    s = index.join(stats, on="product_id", how="left").sort("i")

    def f32(c):
        return s[c].cast(pl.Float32).fill_null(np.nan).to_numpy()

    np.savez(
        out / "stats.npz",
        known=s["p_price"].is_not_null().to_numpy() | s["p_events_7d"].is_not_null().to_numpy(),
        p_events_1d=f32("p_events_1d"),
        p_events_7d=f32("p_events_7d"),
        p_carts_7d=f32("p_carts_7d"),
        p_purchases_7d=f32("p_purchases_7d"),
        p_price=f32("p_price"),
        p_category=s["p_category"].fill_null(-1).to_numpy(),
        p_brand=np.array(
            [code.get(b, -1) if b is not None else -1 for b in s["p_brand"].to_list()],
            dtype=np.int32,
        ),
    )
    (out / "brands.json").write_text(json.dumps(brands), encoding="utf-8")
    np.save(out / "popular.npy", np.asarray(popular[:TOP_POPULAR], dtype=np.int64))
    shutil.copy(ranker_file, out / "ranker.txt")
    return {"products": len(products), "brands": len(brands)}


def export(window: str = "C", out: Path = SERVE) -> dict:
    """The serving artifacts as of the start of `window`, with the trained ranker."""
    from . import experiment as ex
    from .features import product_stats
    from .split import WINDOWS, stats_events

    meta = write_artifacts(
        out,
        ex.matrices_for(window),
        product_stats(stats_events(window), WINDOWS[window][0]),
        ex.popular_for(window)["product_id"].to_list(),
        Path("data") / "models" / "ranker.txt",
    )
    meta = {"window": window, **meta}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


# -- parity ------------------------------------------------------------------------


def to_events(history) -> dict[int, list[Event]]:
    out: dict[int, list[Event]] = {}
    rows = history.sort("session", "pos").select(
        "session", "event_time", "event_type", "product_id", "category_id", "brand", "price"
    )
    for s, t, et, p, c, b, pr in rows.iter_rows():
        out.setdefault(s, []).append(Event(int(t.timestamp()), et, p, c, b, pr))
    return out


def parity(n: int = 5000) -> dict:
    """The offline pipeline against Model on the same test sessions: candidate sets,
    every feature value, and the final top 20."""
    import polars as pl

    from . import candidates as cand
    from . import experiment as ex
    from .features import build, product_stats
    from .split import WINDOWS, stats_events

    history, cases = ex.cases_for("C")
    cases = cases.head(n)
    history = history.join(cases.select("session"), on="session", how="semi")
    offline = build(
        history, cases, ex.matrices_for("C"), product_stats(stats_events("C"), WINDOWS["C"][0])
    ).with_columns(pl.col(FEATURES).cast(pl.Float32))
    model = Model()
    pred = model.booster.predict(offline.select(FEATURES).to_numpy())
    offline_run = cand.top_k(
        offline.select("session", "product_id").with_columns(pl.Series("score", pred)),
        cases,
        ex.popular_for("C"),
    )
    per_session = to_events(history)
    worst, cand_mismatch, nan_mismatch, rows = 0.0, 0, 0, 0
    for s, part in offline.group_by("session", maintain_order=True):
        cands, X = model.features(per_session[s[0]])
        if cands != part["product_id"].to_list():
            cand_mismatch += 1
            continue
        A = part.select(FEATURES).to_numpy()
        same = (A == X) | (np.isnan(A) & np.isnan(X))  # equal infinities count as equal
        nan_mismatch += int((np.isnan(A) != np.isnan(X)).sum())
        with np.errstate(invalid="ignore"):
            diff = np.where(same, 0.0, np.abs(A - X) / np.maximum(1.0, np.abs(A)))
        worst = max(worst, float(np.nanmax(diff)) if diff.size else 0.0)
        rows += len(cands)
    top_equal = sum(
        [p for p, _ in model.recommend(per_session[s])] == offline_run[i].tolist()
        for i, s in enumerate(cases["session"].to_list())
    )
    return {
        "sessions": cases.height,
        "candidate_set_mismatches": cand_mismatch,
        "feature_rows": rows,
        "missing_value_mismatches": nan_mismatch,
        "max_relative_feature_diff": worst,
        "identical_top20_lists": top_equal,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["export", "parity"])
    a = ap.parse_args(argv)
    if a.what == "export":
        print(export())
    else:
        result = parity()
        print(result)
        Path("runs").mkdir(exist_ok=True)
        Path("runs/parity_C.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
