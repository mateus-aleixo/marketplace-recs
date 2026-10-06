"""The serving path against the offline pipeline, on a synthetic marketplace.

The real parity check runs on 5,000 test sessions from the data (`python -m
marketplace_recs.online parity`). This one runs in CI with no data: a small synthetic
store goes through the whole offline pipeline (matrices, cases, features, a trained
ranker, the exported arrays), and the online model must reproduce every candidate set,
every feature and every ranking, and the API must serve the same answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import lightgbm as lgb
import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from marketplace_recs import candidates as cand
from marketplace_recs.covisit import build as covis
from marketplace_recs.features import build, product_stats
from marketplace_recs.online import Model, to_events, write_artifacts
from marketplace_recs.serve import app as app_module
from marketplace_recs.spec import FEATURES
from marketplace_recs.split import WINDOWS, cases

UTC = UTC


def synthetic_events(seed: int = 0) -> pl.DataFrame:
    """300 sessions before the test window, 120 inside it. Products 1..80 in 8 categories;
    sessions wander within a category and sometimes jump, view mostly, cart sometimes."""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(420):
        day = datetime(2019, 10, 20, tzinfo=UTC) if s < 300 else datetime(2019, 10, 25, tzinfo=UTC)
        t = day + timedelta(minutes=int(rng.integers(0, 60 * 20)))
        p = int(rng.integers(1, 81))
        for _ in range(int(rng.integers(2, 9))):
            if rng.random() < 0.3:
                p = int(rng.integers(1, 81))
            else:
                p = (p // 10) * 10 + int(rng.integers(0, 10)) or 1
            etype = 1 if rng.random() < 0.1 else 0
            rows.append((t, etype, p, p // 10, f"brand{p % 4}" if p % 7 else None, 10.0 + p, s))
            t += timedelta(seconds=int(rng.integers(5, 300)))
    return pl.DataFrame(
        rows,
        schema={
            "event_time": pl.Datetime("us", "UTC"),
            "event_type": pl.UInt8,
            "product_id": pl.Int32,
            "category_id": pl.Int64,
            "brand": pl.String,
            "price": pl.Float32,
            "session": pl.Int32,
        },
        orient="row",
    ).sort("event_time", "session", maintain_order=True)


class SumBooster:
    """A stand-in ranker for the features test: score = co-visitation total."""

    def predict(self, X):
        return np.asarray(X)[:, FEATURES.index("c_sum")].astype(np.float64)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    ev = synthetic_events()
    start = WINDOWS["C"][0]
    stats = ev.lazy().filter(pl.col("event_time") < start)
    matrices = covis(stats, parts=2)
    products = product_stats(stats, start)
    history, cs = cases("C", 200, seed=0, lf=ev.lazy())
    offline = build(history, cs, matrices, products).with_columns(pl.col(FEATURES).cast(pl.Float32))
    pop = cand.popular(stats, start)

    # A real (tiny) LambdaRank model, so the exported file is what serving loads.
    groups = offline.group_by("session", maintain_order=True).len()["len"].to_numpy()
    booster = lgb.train(
        {"objective": "lambdarank", "min_data_in_leaf": 1, "verbose": -1, "seed": 0},
        lgb.Dataset(offline.select(FEATURES).to_numpy(), offline["label"].to_numpy(), group=groups),
        num_boost_round=5,
    )
    out = tmp_path_factory.mktemp("serve")
    booster.save_model(str(out / "trained.txt"))
    write_artifacts(out, matrices, products, pop["product_id"].to_list(), out / "trained.txt")
    return {"history": history, "cases": cs, "offline": offline, "pop": pop, "dir": out}


def test_the_synthetic_world_has_cases(world):
    assert world["cases"].height > 50
    assert world["offline"]["label"].sum() > 0


def test_online_features_match_the_offline_pipeline(world):
    model = Model(world["dir"], booster=SumBooster())
    per_session = to_events(world["history"])
    checked = 0
    for (s,), part in world["offline"].group_by("session", maintain_order=True):
        cands, X = model.features(per_session[s])
        assert cands == part["product_id"].to_list()
        A = part.select(FEATURES).to_numpy()
        np.testing.assert_array_equal(np.isnan(A), np.isnan(X))
        np.testing.assert_allclose(np.nan_to_num(X), np.nan_to_num(A), rtol=1e-6, atol=1e-6)
        checked += 1
    assert checked == world["offline"]["session"].n_unique()


def test_online_rankings_match_the_offline_ranker(world):
    model = Model(world["dir"])
    offline = world["offline"]
    pred = model.booster.predict(offline.select(FEATURES).to_numpy())
    scores = offline.select("session", "product_id").with_columns(pl.Series("score", pred))
    run = cand.top_k(scores, world["cases"], world["pop"], k=10)
    per_session = to_events(world["history"])
    for i, s in enumerate(world["cases"]["session"].to_list()):
        online = [p for p, _ in model.recommend(per_session[s], k=10)]
        expected = [p for p in run[i].tolist() if p >= 0]
        assert online[: len(expected)] == expected


def test_the_api_serves_the_same_answers(world, monkeypatch):
    monkeypatch.setattr(app_module, "MODEL_DIR", world["dir"])
    app_module.model.cache_clear()
    client = TestClient(app_module.app)
    model = Model(world["dir"])
    s = world["cases"]["session"][0]
    events = to_events(world["history"])[s]
    body = {
        "events": [
            {
                "product_id": e.product_id,
                "event_type": ["view", "cart", "purchase"][e.event_type],
                "time": e.time,
                "category_id": e.category_id,
                "brand": e.brand,
                "price": e.price,
            }
            for e in events
        ],
        "k": 10,
    }
    r = client.post("/recommend", json=body)
    assert r.status_code == 200
    got = [x["product_id"] for x in r.json()["recommendations"]]
    assert got == [p for p, _ in model.recommend(events, k=10)]

    for e in body["events"]:
        assert client.post("/sessions/abc/events", json=e).status_code == 200
    r = client.get("/sessions/abc/recommendations", params={"k": 10})
    assert [x["product_id"] for x in r.json()["recommendations"]] == got
    assert client.get("/sessions/nobody/recommendations").status_code == 404
    assert 'recs_requests_total{route="recommend",status="200"}' in client.get("/metrics").text
    app_module.model.cache_clear()


def test_the_session_store_is_bounded():
    from marketplace_recs.online import Event
    from marketplace_recs.sessions import MemorySessions

    store = MemorySessions(max_sessions=2, max_events=3)

    for i in range(5):
        store.append("a", Event(i, 0, i))
    assert [e.product_id for e in store.get("a")] == [2, 3, 4]
    store.append("b", Event(0, 0, 1))
    store.append("c", Event(0, 0, 1))
    assert store.get("a") == []  # least recently active session dropped
    assert len(store) == 2
