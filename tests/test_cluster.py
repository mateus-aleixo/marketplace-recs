"""The cluster driver against a fake kubectl and a fake clock: the Job it submits, and
each k6 step matched to the pods that were ready during it."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from marketplace_recs import cluster


def test_each_step_gets_the_pods_ready_during_it(monkeypatch):
    clock = {"t": 1_000.0}
    submitted = {}
    ready_at = {0: 1, 15: 2, 25: 3}  # seconds after submission -> ready pods

    def ready(t: float) -> int:
        return max(n for start, n in ready_at.items() if t >= start)

    def kubectl(*args, stdin=None):
        elapsed = clock["t"] - 1_000.0
        if args[0] == "apply":
            submitted.update(json.loads(stdin))
            return ""
        if args[0] == "top":
            return "k6-t-1000-abcde   1500m   300Mi\n"
        if args[0] == "logs":
            steps = [
                {"step": i, "offered_rps": r, "requests": 10 * r, "rps": r, "p50_ms": 3.0}
                for i, r in enumerate((100, 200))
            ]
            body = {"base_url": "http://api", "step_seconds": 10, "steps": steps}
            return "noise\nK6_SUMMARY " + json.dumps(body) + "\n"
        kind = args[1]
        if kind == "nodes":
            labels = {"node.kubernetes.io/instance-type": "ek-standard-8"}
            out = {"items": [{"metadata": {"labels": labels}}]}
        elif kind == "deployment":
            out = {"status": {"readyReplicas": ready(elapsed), "replicas": 3}}
        elif kind == "hpa":
            cpu = {"type": "Resource", "resource": {"current": {"averageUtilization": 80}}}
            out = {"status": {"desiredReplicas": 3, "currentMetrics": [cpu]}}
        elif kind == "job":
            out = {"status": {"succeeded": 1} if elapsed >= 30 else {}}
        elif kind == "pods":
            stamp = datetime.fromtimestamp(1_005, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            state = {"terminated": {"startedAt": stamp}}
            out = {"items": [{"status": {"containerStatuses": [{"state": state}]}}]}
        return json.dumps(out)

    monkeypatch.setattr(cluster, "kubectl", kubectl)
    monkeypatch.setattr(cluster, "now", lambda: clock["t"])
    monkeypatch.setattr(cluster, "sleep", lambda s: clock.update(t=clock["t"] + s))

    result = cluster.run("t", [100, 200], 10, "registry/k6:1", cpu="2")

    container = submitted["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env == {"BASE_URL": "http://api", "RATES": "100,200", "STEP": "10"}
    assert container["resources"]["requests"] == container["resources"]["limits"]
    # k6 started 5 s after submission: step 0 covers 5-15 s, step 1 covers 15-25 s.
    assert result["k6_offset_s"] == 5.0
    first, second = result["steps"]
    assert (first["pods_ready_max"], second["pods_ready_max"]) == (1, 2)
    assert first["cpu_pct_mean"] == 80 and second["nodes_max"] == 1
    assert first["k6_mcpu_max"] == 1500
    assert result["k6_cpu"] == "2" and len(result["samples"]) == 7
