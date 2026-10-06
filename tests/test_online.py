"""The serving path against the offline pipeline, on a synthetic marketplace.

The real parity check runs on 5,000 test sessions from the data (`python -m
marketplace_recs.online parity`). This one runs in CI with no data: a small synthetic
store goes through the whole offline pipeline (matrices, cases, features, a trained
ranker, the exported arrays), and the online model must reproduce every candidate set,
every feature and every ranking, and the API must serve the same answers.
"""

from __future__ import annotations

import numpy as np
import polars as pl
from fastapi.testclient import TestClient

from marketplace_recs import candidates as cand
from marketplace_recs.online import Model, to_events
from marketplace_recs.serve import app as app_module
from marketplace_recs.spec import FEATURES


class SumBooster:
    """A stand-in ranker for the features test: score = co-visitation total."""

    def predict(self, X):
        return np.asarray(X)[:, FEATURES.index("c_sum")].astype(np.float64)


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


def test_the_model_is_loaded_before_the_first_request(world, monkeypatch):
    monkeypatch.setattr(app_module, "MODEL_DIR", world["dir"])
    app_module.model.cache_clear()
    with TestClient(app_module.app):
        assert app_module._loaded.get("dir") == world["dir"]
    app_module.model.cache_clear()


def test_without_a_model_the_api_starts_and_answers_503(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "MODEL_DIR", tmp_path)
    app_module.model.cache_clear()
    with TestClient(app_module.app) as client:
        assert client.get("/health").json()["model"] == "missing"
        assert client.post("/recommend", json={"events": [{"product_id": 1}]}).status_code == 503


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
