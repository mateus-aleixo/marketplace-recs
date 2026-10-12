"""Feature drift: does live traffic still look like the traffic it is compared with?

The check is adversarial validation. A classifier learns to tell reference rows from
current rows. Measured out of fold, its AUC is 0.5 when the two cannot be told apart, and
its feature importances show what moved. The rows are the candidate rows the API ranks,
and the folds are grouped by request: the rows of one request share its session features,
so they never sit on both sides of a fold.

    python -m marketplace_recs.drift evaluate    # runs/drift_C.json: the test week, planted faults
    python -m marketplace_recs.drift serve       # live: the rows the API samples into Redis

The live check compares against the traffic a version served first: once a version goes
live, its first window of sampled requests becomes the baseline, and every later window
is compared with that. Comparing with the training week does not work. Every day of the
test week reads about 0.77 against it, because the tables behind the features were built
over different days, while the ranker served those days as well as ever (runs/drift_C.json).

A request is what a product page sends after each event: the session's events so far. The
evaluation samples requests the same way, with every event equally likely to be the latest.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from .online import Event, Model
from .spec import FEATURES

KEY = "drift:rows"  # Redis list, one entry per sampled request
DATA = Path("data") / "drift"
RUNS = Path("runs")
WINDOW = 1500  # sampled requests in a live check, and in the baseline
# Above this AUC the dashboard alerts. Clean windows sampled across a day stay under 0.52
# (runs/drift_C.json), but consecutive windows of live traffic read up to 0.56, since
# sessions change a little within an hour. A fault in a quarter of sessions reads 0.62.
THRESHOLD = 0.6
PARAMS = {
    "objective": "binary",
    "learning_rate": 0.1,
    "num_leaves": 15,
    "min_data_in_leaf": 100,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}


# -- the check ---------------------------------------------------------------------


def auc(y: np.ndarray, score: np.ndarray) -> float:
    """Area under the ROC curve from ranks (the Mann-Whitney U statistic), ties averaged."""
    y = np.asarray(y, dtype=bool)
    order = np.argsort(score, kind="mergesort")
    ordered = np.asarray(score)[order]
    _, inverse, counts = np.unique(ordered, return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    ranks = np.empty(len(score))
    ranks[order] = ((ends - counts + 1 + ends) / 2)[inverse]
    pos, neg = y.sum(), (~y).sum()
    return float((ranks[y].sum() - pos * (pos + 1) / 2) / (pos * neg))


def _finite(X: np.ndarray) -> np.ndarray:
    # A price ratio over a free product is infinite; LightGBM wants a number or a NaN.
    return np.nan_to_num(np.asarray(X, dtype=np.float32), nan=np.nan, posinf=1e30, neginf=-1e30)


def _cap(X, groups, n: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """At most n requests, chosen at random, with all their rows."""
    ids = np.unique(groups)
    if len(ids) <= n:
        return X, groups
    keep = np.isin(groups, rng.choice(ids, n, replace=False))
    return X[keep], groups[keep]


def _shares(v: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """The share of v in each bin between edges, and last the share that is missing."""
    f = np.isfinite(v)
    counts = np.bincount(np.searchsorted(edges, v[f], "right"), minlength=len(edges) + 1)
    return np.r_[counts, (~f).sum()] / len(v)


def psi(ref: np.ndarray, cur: np.ndarray, bins: int = 10) -> np.ndarray:
    """The population stability index of each column: the reference's deciles, plus one bin
    for missing values. Around 0.1 is a small move, above 0.25 a large one."""
    out = np.zeros(ref.shape[1])
    cuts = np.linspace(0, 1, bins + 1)[1:-1]
    for j in range(ref.shape[1]):
        a = ref[:, j]
        known = a[np.isfinite(a)]
        edges = np.unique(np.quantile(known, cuts)) if len(known) else np.zeros(0)
        pa = np.maximum(_shares(a, edges), 1e-4)
        pb = np.maximum(_shares(cur[:, j], edges), 1e-4)
        out[j] = float(((pb - pa) * np.log(pb / pa)).sum())
    return out


def adversarial(
    ref: np.ndarray,
    cur: np.ndarray,
    ref_groups: np.ndarray,
    cur_groups: np.ndarray,
    *,
    folds: int = 5,
    rounds: int = 60,
    max_requests: int = 3000,
    threads: int = 4,
    seed: int = 0,
) -> dict:
    """How well a classifier tells current rows from reference rows: out-of-fold AUC, the
    features it relied on most (share of split gain) and the largest PSIs. Groups are
    request ids; every row of a request lands in the same fold."""
    import lightgbm as lgb

    rng = np.random.default_rng(seed)
    ref, ref_groups = _cap(_finite(ref), np.asarray(ref_groups), max_requests, rng)
    cur, cur_groups = _cap(_finite(cur), np.asarray(cur_groups), max_requests, rng)
    X = np.vstack([ref, cur])
    y = np.r_[np.zeros(len(ref)), np.ones(len(cur))]
    keys = np.r_[ref_groups.astype(np.int64) * 2, cur_groups.astype(np.int64) * 2 + 1]
    _, group = np.unique(keys, return_inverse=True)
    fold = rng.permutation(group.max() + 1)[group] % folds
    oof = np.zeros(len(y))
    gain = np.zeros(X.shape[1])
    for k in range(folds):
        train = fold != k
        booster = lgb.train(
            PARAMS | {"seed": seed, "num_threads": threads},
            lgb.Dataset(X[train], y[train], feature_name=FEATURES),
            num_boost_round=rounds,
        )
        oof[~train] = booster.predict(X[~train])
        gain += booster.feature_importance("gain")
    share = gain / gain.sum() if gain.sum() > 0 else gain
    stability = psi(ref, cur)
    return {
        "auc": round(auc(y, oof), 4),
        "requests": [int(len(np.unique(ref_groups))), int(len(np.unique(cur_groups)))],
        "rows": [int(len(ref)), int(len(cur))],
        "importance": [[FEATURES[i], round(float(share[i]), 3)] for i in np.argsort(-share)[:3]],
        "psi": [[FEATURES[i], round(float(stability[i]), 3)] for i in np.argsort(-stability)[:3]],
        "psi_all": stability,
    }


# -- requests from the recorded month -------------------------------------------------


def featurize(model: Model, prefixes: list[list[Event]]) -> tuple[np.ndarray, np.ndarray]:
    """The candidate rows the API would rank for each request, and the request each came from."""
    blocks, groups = [], []
    for i, events in enumerate(prefixes):
        _, X = model.features(events)
        blocks.append(X)
        groups.append(np.full(len(X), i, dtype=np.int32))
    if not blocks:
        return np.zeros((0, len(FEATURES)), dtype=np.float32), np.zeros(0, dtype=np.int32)
    return np.vstack(blocks), np.concatenate(groups)


def tables(window: str) -> Path:
    """The serving arrays a window is scored with, exported as the API loads them, cached
    under data/drift/."""
    from . import experiment as ex
    from .features import product_stats
    from .online import write_artifacts
    from .split import WINDOWS, stats_events

    out = DATA / f"tables-{window}"
    if not (out / "ranker.txt").exists():
        write_artifacts(
            out,
            ex.matrices_for(window),
            product_stats(stats_events(window), WINDOWS[window][0]),
            ex.popular_for(window)["product_id"].to_list(),
            ex.DATA / "models" / "ranker.txt",
        )
    return out


def requests(window: str, n: int, seed: int) -> tuple[list[list[Event]], np.ndarray, np.ndarray]:
    """n requests from the sessions that start in `window`, every event equally likely to be
    the latest: their event lists, their session ids and the day of the window they fall on."""
    import polars as pl

    from .split import WINDOWS, events

    start, end = WINDOWS[window]
    lf = events().select(
        "event_time", "event_type", "product_id", "category_id", "brand", "price", "session"
    )
    starts = lf.group_by("session").agg(pl.col("event_time").min().alias("first"))
    inside = starts.filter((pl.col("first") >= start) & (pl.col("first") < end)).select("session")
    ev = (
        lf.join(inside, on="session", how="semi")
        .collect()
        .sort("session", "event_time", maintain_order=True)
        .with_columns(pl.int_range(pl.len()).over("session").alias("pos"))
    )
    rng = np.random.default_rng(seed)
    picked = ev[np.sort(rng.choice(ev.height, size=min(n, ev.height), replace=False))]
    sessions = ev.join(picked.select("session").unique(), on="session", how="semi")
    by_session: dict[int, list[Event]] = {}
    for t, et, p, c, b, pr, s, _ in sessions.iter_rows():
        by_session.setdefault(s, []).append(Event(int(t.timestamp()), et, p, c, b, pr))
    prefixes = [by_session[s][: pos + 1] for s, pos in picked.select("session", "pos").iter_rows()]
    day = ((picked["event_time"].dt.epoch("s") - int(start.timestamp())) // 86_400).to_numpy()
    return prefixes, picked["session"].to_numpy(), day


# What a broken client or upstream feed could send, applied to every event of a session.
FAULTS = {
    "price_in_cents": lambda e: replace(e, price=None if e.price is None else e.price * 100),
    "time_in_ms": lambda e: replace(e, time=e.time * 1000),
    "brand_missing": lambda e: replace(e, brand=None),
    "category_missing": lambda e: replace(e, category_id=None),
    "views_only": lambda e: replace(e, event_type=0),
}


def planted(prefixes, sessions, fault: str, share: float, seed: int) -> list[list[Event]]:
    """The same requests with `fault` in a share of sessions, as one broken client version
    would send them."""
    ids = np.unique(sessions)
    broken = set(ids[np.random.default_rng(seed).random(len(ids)) < share].tolist())
    f = FAULTS[fault]
    return [
        [f(e) for e in p] if s in broken else p for p, s in zip(prefixes, sessions, strict=True)
    ]


def _brief(rep: dict) -> dict:
    return {k: v for k, v in rep.items() if k != "psi_all"}


def _windows(A, Ag, B, Bg, repeats: int, seed: int) -> list[float]:
    """AUCs of `repeats` live-sized checks, each on a fresh random window of both sides."""
    return [
        adversarial(A, B, Ag, Bg, max_requests=WINDOW, seed=seed + i)["auc"] for i in range(repeats)
    ]


def evaluate(n_reference: int = 5000, n_day: int = 3000, repeats: int = 20, seed: int = 0) -> dict:
    """The check on real data. Against the training week: each day of the test week, and two
    random halves of the training week itself. Against the first day of the test week, as
    the live check's baseline: each later day. Against the day before: the same day clean,
    and with each fault planted in a share of its sessions. Then the threshold, from clean
    and faulty windows of the live check's size."""
    t0 = time.perf_counter()
    ref_prefixes, _, _ = requests("B", n_reference, seed)
    R, Rg = featurize(Model(tables("B")), ref_prefixes)
    live = Model(tables("C"))
    prefixes, sessions, day = requests("C", n_day * 8, seed + 1)
    C, Cg = featurize(live, prefixes)
    row_day = day[Cg]
    out: dict = {
        "reference": {"window": "B", "requests": len(ref_prefixes), "rows": len(R)},
        "test_requests_per_day": np.bincount(day).tolist(),
        "threshold": THRESHOLD,
    }
    half = (np.random.default_rng(seed).random(Rg.max() + 1) < 0.5)[Rg]
    out["training_halves"] = _brief(adversarial(R[half], R[~half], Rg[half], Rg[~half]))
    out["against_training"] = []
    out["against_first_day"] = []
    first = row_day == 0
    for d in range(8):
        m = row_day == d
        rep = adversarial(R, C[m], Rg, Cg[m])
        row = {"day": d, **_brief(rep), "largest_psi_any": round(float(rep["psi_all"].max()), 3)}
        out["against_training"].append(row)
        if d:
            out["against_first_day"].append(
                {"day": d, **_brief(adversarial(C[first], C[m], Cg[first], Cg[m]))}
            )
        print("day", d, rep["auc"], out["against_first_day"][-1]["auc"] if d else "", flush=True)

    # Sudden faults: day 1 against day 0, as a check comparing today with yesterday.
    Y, Yg = C[first], Cg[first]
    today = np.flatnonzero(day == 1)
    t_prefixes, t_sessions = [prefixes[i] for i in today], sessions[today]
    T, Tg = featurize(live, t_prefixes)
    out["clean_day_against_previous"] = _brief(adversarial(Y, T, Yg, Tg))
    # What the classifier cannot see: products the frozen tables have never heard of.
    unknown = np.array([p[-1].product_id not in live.index for p in prefixes])
    out["last_product_unknown_by_day"] = [
        round(float(unknown[day == d].mean()), 4) for d in range(8)
    ]
    out["faults"] = []
    broken = {}
    for fault in FAULTS:
        for share in (1.0, 0.25, 0.1, 0.05):
            F, Fg = featurize(live, planted(t_prefixes, t_sessions, fault, share, seed))
            broken[fault, share] = F, Fg
            rep = adversarial(Y, F, Yg, Fg)
            out["faults"].append({"fault": fault, "share": share, **_brief(rep)})
            print(fault, share, rep["auc"], flush=True)

    # The threshold: how often clean windows of the live check's size pass it, and how
    # often faults in a quarter or a tenth of sessions do. Every AUC is kept, so the
    # counts can be read for any threshold.
    def summary(aucs: list[float]) -> dict:
        return {
            "mean": round(float(np.mean(aucs)), 4),
            "min": min(aucs),
            "max": max(aucs),
            "above_threshold": sum(a > THRESHOLD for a in aucs),
            "aucs": aucs,
        }

    out["calibration"] = {
        "window_requests": WINDOW,
        "repeats": repeats,
        "clean": summary(_windows(Y, Yg, T, Tg, repeats, seed)),
        "faults": [
            {
                "fault": fault,
                "share": share,
                **summary(_windows(Y, Yg, *broken[fault, share], repeats, seed)),
            }
            for fault in FAULTS
            for share in (0.25, 0.1)
        ],
    }
    out["seconds"] = round(time.perf_counter() - t0, 1)
    return out


# -- live -------------------------------------------------------------------------------


def pack(X: np.ndarray) -> bytes:
    return np.asarray(X, dtype="<f4").tobytes()


def unpack(entries: list[bytes]) -> tuple[np.ndarray, np.ndarray]:
    """The rows of the sampled requests, and the request each row belongs to."""
    blocks = [np.frombuffer(b, dtype="<f4").reshape(-1, len(FEATURES)) for b in entries]
    if not blocks:
        return np.zeros((0, len(FEATURES)), dtype=np.float32), np.zeros(0, dtype=np.int32)
    groups = np.concatenate([np.full(len(x), i, dtype=np.int32) for i, x in enumerate(blocks)])
    return np.vstack(blocks), groups


class Sampler:
    """Copies the feature rows of a share of requests to a capped Redis list for the drift
    check. Requests go out in batches, so a sampled request costs a list append, not a
    round trip. If Redis fails the batch is dropped and counted: monitoring must never fail
    a request."""

    def __init__(self, url: str | None, share: float, cap: int = 5 * WINDOW, batch: int = 50):
        self.share, self.cap, self.batch = share, cap, batch
        self.redis = None
        if url and share > 0:
            import redis

            self.redis = redis.Redis.from_url(url)
        self.buffer: list[bytes] = []
        self.flushed = time.monotonic()
        self.lock = threading.Lock()
        self.dropped = 0

    def record(self, X: np.ndarray) -> None:
        if self.redis is None or len(X) == 0 or random.random() >= self.share:
            return
        with self.lock:
            self.buffer.append(pack(X))
            if len(self.buffer) < self.batch and time.monotonic() - self.flushed < 1.0:
                return
            batch, self.buffer, self.flushed = self.buffer, [], time.monotonic()
        try:
            pipe = self.redis.pipeline(transaction=False)
            pipe.rpush(KEY, *batch)
            pipe.ltrim(KEY, -self.cap, -1)
            pipe.execute()
        except Exception:  # noqa: BLE001
            self.dropped += len(batch)


def metrics() -> dict:
    """The drift worker's gauges. Created on first use, so the API, which imports this
    module for the sampler, does not report them."""
    from prometheus_client import Counter, Gauge

    if not hasattr(metrics, "m"):
        metrics.m = {
            "auc": Gauge("recs_drift_auc", "Out-of-fold AUC, latest window against the baseline"),
            "threshold": Gauge("recs_drift_threshold", "AUC above which the dashboard alerts"),
            "requests": Gauge("recs_drift_requests", "Sampled requests in the latest window"),
            "baseline": Gauge("recs_drift_baseline_requests", "Sampled requests in the baseline"),
            "importance": Gauge(
                "recs_drift_importance", "Share of the classifier's gain, top features", ["feature"]
            ),
            "psi": Gauge("recs_drift_psi", "Population stability index per feature", ["feature"]),
            "checks": Counter("recs_drift_checks_total", "Checks run, by outcome", ["outcome"]),
            "seconds": Gauge("recs_drift_check_seconds", "Time the last check took"),
        }
    return metrics.m


class Monitor:
    """The live check. When a version goes live, the sampled requests collected so far are
    dropped and the first WINDOW after that become its baseline; from then on each check
    compares the latest WINDOW requests with the baseline."""

    def __init__(self, r, window: int = WINDOW, min_requests: int = 300, threads: int = 1):
        self.r, self.window, self.min_requests, self.threads = r, window, min_requests, threads
        self.version = None
        self.baseline: tuple[np.ndarray, np.ndarray] | None = None

    def step(self, version: str) -> dict | None:
        m = metrics()
        if version != self.version:
            # A new version starts from scratch: rows the old one ranked belong to neither.
            self.version, self.baseline = version, None
            self.r.delete(KEY)
            m["baseline"].set(0)
            m["auc"].set(float("nan"))  # no reading until the new baseline is in
            m["checks"].labels("new_version").inc()
            return None
        waiting = self.r.llen(KEY)
        if self.baseline is None:
            if waiting < self.window:
                m["checks"].labels("collecting").inc()
                return None
            pipe = self.r.pipeline(transaction=True)
            pipe.lrange(KEY, -self.window, -1)
            pipe.delete(KEY)  # later windows hold only requests served after the baseline
            entries, _ = pipe.execute()
            self.baseline = unpack(entries)
            m["baseline"].set(len(entries))
            m["checks"].labels("baseline").inc()
            return None
        if waiting < self.min_requests:
            m["checks"].labels("collecting").inc()
            return None
        t0 = time.perf_counter()
        X, groups = unpack(self.r.lrange(KEY, -self.window, -1))
        base, base_groups = self.baseline
        rep = adversarial(
            base, X, base_groups, groups, max_requests=self.window, threads=self.threads
        )
        m["auc"].set(rep["auc"])
        m["requests"].set(rep["requests"][1])
        m["importance"].clear()
        for feature, share in rep["importance"]:
            m["importance"].labels(feature).set(share)
        for feature, value in zip(FEATURES, rep["psi_all"], strict=True):
            m["psi"].labels(feature).set(value)
        m["checks"].labels("alert" if rep["auc"] > THRESHOLD else "ok").inc()
        m["seconds"].set(time.perf_counter() - t0)
        return rep


def serve(redis_url: str, model_dir: Path, every: float, port: int) -> None:
    """Run the live check every `every` seconds, with the gauges served on `port`."""
    import redis
    from prometheus_client import start_http_server

    from .pipeline import serving_dir

    start_http_server(port)
    metrics()["threshold"].set(THRESHOLD)
    metrics()["auc"].set(float("nan"))
    monitor = Monitor(redis.Redis.from_url(redis_url))
    while True:
        t0 = time.monotonic()
        try:
            rep = monitor.step(serving_dir(model_dir).name)
            if rep:
                top = rep["importance"][0][0]
                print(
                    f"drift auc {rep['auc']:.3f} on {rep['requests'][1]} requests, top {top}",
                    flush=True,
                )
        except redis.RedisError as e:
            metrics()["checks"].labels("redis_error").inc()
            print(f"drift: {e}", flush=True)
        time.sleep(max(0.0, every - (time.monotonic() - t0)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["evaluate", "serve"])
    ap.add_argument("--every", type=float, default=15.0, help="seconds between live checks")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RECS_METRICS_PORT", 9102)))
    a = ap.parse_args(argv)
    if a.what == "evaluate":
        result = evaluate()
        RUNS.mkdir(exist_ok=True)
        (RUNS / "drift_C.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result["calibration"]["clean"]))
    else:
        serve(
            os.environ.get("RECS_REDIS", "redis://localhost:6379/0"),
            Path(os.environ.get("RECS_MODEL_DIR", "model")),
            a.every,
            a.port,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
