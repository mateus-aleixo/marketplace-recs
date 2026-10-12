"""The drift check, its live parts, and the dashboard's contract with the code."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fakeredis
import numpy as np
import pytest
from fastapi.testclient import TestClient

from marketplace_recs import drift
from marketplace_recs.online import Model, to_events
from marketplace_recs.spec import FEATURES

ROOT = Path(__file__).resolve().parents[1]


def requests_like(n: int, seed: int, rows: int = 30, shift: dict | None = None):
    """n synthetic requests of `rows` candidate rows each: per-row noise plus a value per
    request in the session columns, as real requests have."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n * rows, len(FEATURES))).astype(np.float32)
    for j in (FEATURES.index("s_events"), FEATURES.index("s_seconds")):
        X[:, j] = np.repeat(rng.gamma(2.0, 3.0, n), rows)
    for name, f in (shift or {}).items():
        j = FEATURES.index(name)
        X[:, j] = f(X[:, j])
    return X, np.repeat(np.arange(n), rows).astype(np.int32)


def test_auc_counts_pairs_and_halves_ties():
    rng = np.random.default_rng(0)
    y = rng.random(400) < 0.3
    s = np.round(rng.random(400), 1) + 0.1 * y
    pos, neg = s[y], s[~y]
    pairs = (pos[:, None] > neg).sum() + 0.5 * (pos[:, None] == neg).sum()
    assert drift.auc(y, s) == pytest.approx(pairs / (len(pos) * len(neg)))


def test_the_check_reads_a_half_when_nothing_moved():
    a, ag = requests_like(800, seed=1)
    b, bg = requests_like(800, seed=2)
    assert 0.45 < drift.adversarial(a, b, ag, bg)["auc"] < 0.55


def test_the_check_finds_and_names_the_feature_that_moved():
    a, ag = requests_like(800, seed=3)
    b, bg = requests_like(800, seed=4, shift={"price_ratio": lambda v: v / 100})
    rep = drift.adversarial(a, b, ag, bg)
    assert rep["auc"] > 0.95
    assert rep["importance"][0][0] == "price_ratio"
    assert rep["psi"][0][0] == "price_ratio"


def test_psi_is_near_zero_for_the_same_distribution_and_large_for_a_shift():
    rng = np.random.default_rng(5)
    a = rng.normal(size=(5000, 3))
    b = rng.normal(size=(5000, 3))
    assert drift.psi(a, b).max() < 0.02
    b[:, 1] += 1.0
    b[:200, 2] = np.nan  # values going missing count too
    p = drift.psi(a, b)
    assert p[1] > 0.5 and p[2] > 0.05


def test_each_planted_fault_moves_the_features_it_should(world):
    model = Model(world["dir"])
    sessions = to_events(world["history"])
    events = max(sessions.values(), key=len)
    _, X = model.features(events)

    def col(name, faulty):
        j = FEATURES.index(name)
        return model.features([drift.FAULTS[faulty](e) for e in events])[1][:, j], X[:, j]

    got, clean = col("price_ratio", "price_in_cents")
    ok = np.isfinite(clean) & (clean != 0)
    assert ok.any() and np.allclose(got[ok], clean[ok] / 100, rtol=1e-5)
    got, clean = col("s_seconds", "time_in_ms")
    assert np.allclose(got, clean * 1000)
    assert np.isnan(col("same_brand", "brand_missing")[0]).all()
    assert np.isnan(col("same_category", "category_missing")[0]).all()
    assert (col("l_type", "views_only")[0] == 0).all()


def test_a_fault_hits_whole_sessions_and_only_its_share():
    from marketplace_recs.online import Event

    prefixes = [[Event(t, 0, 1, price=10.0)] for t in range(2000)]
    sessions = np.arange(2000) // 4  # four requests a session
    out = drift.planted(prefixes, sessions, "price_in_cents", 0.25, seed=0)
    hit = np.array([p[0].price == 1000.0 for p in out])
    assert 0.2 < hit.mean() < 0.3
    assert (hit.reshape(-1, 4).min(1) == hit.reshape(-1, 4).max(1)).all()


def test_the_sampler_batches_rows_into_a_capped_list():
    sampler = drift.Sampler(None, share=1.0, cap=3, batch=2)
    sampler.redis = fakeredis.FakeRedis()
    for i in range(5):
        sampler.record(np.full((i + 1, len(FEATURES)), i, dtype=np.float32))
    entries = sampler.redis.lrange(drift.KEY, 0, -1)
    assert len(entries) == 3  # five recorded, two flushes of two, then capped at three
    X, groups = drift.unpack(entries)
    assert X.shape == (2 + 3 + 4, len(FEATURES))
    assert groups.tolist() == [0, 0, 1, 1, 1, 2, 2, 2, 2]


def test_a_sampler_whose_redis_fails_drops_the_batch_and_never_raises():
    class Broken:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis is down")

    sampler = drift.Sampler(None, share=1.0, batch=1)
    sampler.redis = Broken()
    sampler.record(np.ones((3, len(FEATURES)), dtype=np.float32))
    assert sampler.dropped == 1


def push(r, X, groups):
    r.rpush(drift.KEY, *[drift.pack(X[groups == g]) for g in np.unique(groups)])


def test_the_monitor_takes_a_baseline_then_compares_later_windows():
    r = fakeredis.FakeRedis()
    monitor = drift.Monitor(r, window=300, min_requests=100)
    assert monitor.step("v1") is None  # the first version seen: start collecting
    push(r, *requests_like(200, seed=6))
    assert monitor.step("v1") is None and monitor.baseline is None  # still collecting
    push(r, *requests_like(100, seed=7))
    assert monitor.step("v1") is None and monitor.baseline is not None
    assert r.llen(drift.KEY) == 0  # the baseline's requests are not compared with themselves

    push(r, *requests_like(300, seed=8))
    assert monitor.step("v1")["auc"] < drift.THRESHOLD
    push(r, *requests_like(300, seed=9, shift={"same_brand": lambda v: v * np.nan}))
    rep = monitor.step("v1")
    assert rep["auc"] > drift.THRESHOLD and rep["importance"][0][0] == "same_brand"
    assert drift.metrics()["auc"]._value.get() == rep["auc"]

    # A new version drops what the old one ranked and collects a baseline of its own.
    assert monitor.step("v2") is None and monitor.baseline is None
    assert r.llen(drift.KEY) == 0


def test_the_api_samples_the_rows_it_ranked(world, monkeypatch):
    from marketplace_recs.serve import app as app_module

    monkeypatch.setattr(app_module, "MODEL_DIR", world["dir"])
    sampler = drift.Sampler(None, share=1.0, batch=1)
    sampler.redis = fakeredis.FakeRedis()
    monkeypatch.setattr(app_module, "sampler", sampler)
    app_module.model.cache_clear()
    s = world["cases"]["session"][0]
    events = to_events(world["history"])[s]
    body = {"events": [{"product_id": e.product_id, "time": e.time} for e in events], "k": 5}
    assert TestClient(app_module.app).post("/recommend", json=body).status_code == 200
    X, _ = drift.unpack(sampler.redis.lrange(drift.KEY, 0, -1))
    plain = [type(e)(e.time, 0, e.product_id) for e in events]
    assert np.array_equal(X, Model(world["dir"]).features(plain)[1], equal_nan=True)
    app_module.model.cache_clear()


def test_with_several_workers_the_metrics_of_all_of_them_are_summed(tmp_path):
    """Each process stands for one uvicorn worker: three requests in one, two in another."""
    count = (
        "from marketplace_recs.serve import app; "
        "[app.REQUESTS.labels('recommend', '200').inc() for _ in range({n})]; "
        "print(app.generate_latest(app._registry()).decode())"
    )
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for n in (3, 2):
        out = subprocess.run(
            [sys.executable, "-c", count.format(n=n)], env=env, capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
    assert 'recs_requests_total{route="recommend",status="200"} 5.0' in out.stdout


def exported_names() -> set[str]:
    """Every sample name the API, the consumer and the drift worker export."""
    from prometheus_client import REGISTRY

    from marketplace_recs import stream
    from marketplace_recs.serve import app  # noqa: F401  (registers the API's metrics)

    stream.metrics()
    drift.metrics()
    suffixes = {"counter": ("_total",), "histogram": ("_bucket", "_sum", "_count"), "gauge": ("",)}
    return {m.name + s for m in REGISTRY.collect() for s in suffixes.get(m.type, ("",))}


def test_the_dashboard_asks_only_for_metrics_the_code_exports():
    dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/recs.json").read_text("utf-8"))
    exprs = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    wanted = {m for e in exprs for m in re.findall(r"recs_[a-z_]+", e)}
    assert wanted and wanted <= exported_names(), wanted - exported_names()
    # The alert colour and line on the dashboard are the threshold the drift worker uses.
    steps = [
        s["value"]
        for p in dashboard["panels"]
        if p["targets"][0]["expr"] == "recs_drift_auc"
        for s in p["fieldConfig"]["defaults"].get("thresholds", {}).get("steps", [])
        if s["value"] is not None
    ]
    assert steps and set(steps) == {drift.THRESHOLD}


def test_prometheus_scrapes_the_services_compose_runs_on_their_ports():
    compose = (ROOT / "docker-compose.yml").read_text("utf-8")
    scrape = (ROOT / "monitoring/prometheus.yml").read_text("utf-8")
    for service, port in (("api", 8080), ("consumer", 9101), ("drift", 9102)):
        assert f'"{service}:{port}"' in scrape
        assert re.search(rf"^  {service}:\n", compose, re.M)
    assert '"9101"' in compose and '"9102"' in compose
