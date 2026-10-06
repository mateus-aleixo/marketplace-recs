"""LightGBM LambdaRank over the candidates: trained on window B, scored on window C.

    python -m marketplace_recs.ranker

Training cases come from B with features from A; the test is the same 200,000 C cases
the baselines are scored on, with features from A and B. A training session whose target
is not among its candidates teaches the ranker nothing and is left out; in the test such
a session simply counts as a miss. Training keeps every positive and 30 sampled negatives
per session, and features are built a chunk of sessions at a time to bound memory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import lightgbm as lgb
import polars as pl

from . import candidates as cand
from . import experiment as ex
from .features import FEATURES, build, product_stats
from .metrics import evaluate
from .split import WINDOWS, stats_events

NEGATIVES = 30
CHUNKS = 8
MODELS = Path("data") / "models"


def chunks(history: pl.DataFrame, cases: pl.DataFrame, n: int = CHUNKS):
    sessions = cases["session"]
    for i in range(n):
        part = sessions.filter(sessions.hash(seed=0) % n == i)
        yield (
            history.filter(pl.col("session").is_in(part.implode())),
            cases.filter(pl.col("session").is_in(part.implode())),
        )


def features_for(window: str, sample: bool):
    matrices = ex.matrices_for(window)
    history, cases = ex.cases_for(window)
    products = product_stats(stats_events(window), WINDOWS[window][0])
    for h, c in chunks(history, cases):
        df = build(h, c, matrices, products).with_columns(pl.col(FEATURES).cast(pl.Float32))
        if sample:
            keep = df.filter(pl.col("label") == 1).select("session").unique()
            df = (
                df.join(keep, on="session", how="semi")
                .with_columns(pl.int_range(pl.len()).shuffle(seed=0).over("session").alias("r"))
                .filter((pl.col("label") == 1) | (pl.col("r") < NEGATIVES))
                .drop("r")
            )
        yield df, c


def train(seed: int = 0) -> tuple[lgb.Booster, dict]:
    t0 = time.perf_counter()
    df = pl.concat([d for d, _ in features_for("B", sample=True)]).sort("session")
    sessions = df["session"].unique().sort()
    val = sessions.sample(fraction=0.1, seed=seed)
    is_val = df["session"].is_in(val.implode())

    def dataset(part: pl.DataFrame, ref=None):
        groups = part.group_by("session", maintain_order=True).len()["len"].to_numpy()
        return lgb.Dataset(
            part.select(FEATURES).to_numpy(),
            part["label"].to_numpy(),
            group=groups,
            feature_name=FEATURES,
            reference=ref,
            free_raw_data=True,
        )

    train_ds = dataset(df.filter(~is_val))
    val_ds = dataset(df.filter(is_val), ref=train_ds)
    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [10, 20],
        "learning_rate": 0.05,
        "num_leaves": 63,
        "min_data_in_leaf": 200,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambdarank_truncation_level": 20,
        "seed": seed,
        "verbose": -1,
    }
    booster = lgb.train(
        params,
        train_ds,
        num_boost_round=1000,
        valid_sets=[val_ds],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(100)],
    )
    info = {
        "train_rows": int((~is_val).sum()),
        "train_sessions": int(len(sessions) - len(val)),
        "best_iteration": booster.best_iteration,
        "val_ndcg@10": round(booster.best_score["valid_0"]["ndcg@10"], 4),
        "seconds": round(time.perf_counter() - t0, 1),
    }
    return booster, info


def test(booster: lgb.Booster) -> dict:
    t0 = time.perf_counter()
    pop = ex.popular_for("C")
    scored, all_cases, covered = [], [], 0
    for df, c in features_for("C", sample=False):
        covered += df.filter(pl.col("label") == 1)["session"].n_unique()
        pred = booster.predict(df.select(FEATURES).to_numpy(), num_iteration=booster.best_iteration)
        scored.append(df.select("session", "product_id").with_columns(pl.Series("score", pred)))
        all_cases.append(c)
    cases = pl.concat(all_cases).sort("session")
    run = cand.top_k(pl.concat(scored), cases, pop)
    return {
        **evaluate(run, cases["target"].to_numpy()),
        "candidate_recall": round(covered / cases.height, 4),
        "seconds": round(time.perf_counter() - t0, 1),
    }


def main() -> int:
    booster, info = train()
    MODELS.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODELS / "ranker.txt"), num_iteration=booster.best_iteration)
    gain = booster.feature_importance("gain")
    importance = sorted(
        zip(FEATURES, (gain / gain.sum()).round(4).tolist(), strict=True), key=lambda kv: -kv[1]
    )
    result = {"train": info, "test_C": test(booster), "importance_gain": dict(importance)}
    print(json.dumps(result, indent=2))
    ex.RUNS.mkdir(exist_ok=True)
    (ex.RUNS / "ranker_C.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
