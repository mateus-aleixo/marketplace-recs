"""Load tests inside the GKE cluster: k6 runs as a Job beside the API, and the pods, the
autoscaler and the nodes are read every few seconds while it runs.

    python -m marketplace_recs.cluster --label ramp --rates 100,200,400,800 --step 120
    python -m marketplace_recs.cluster --label cloud-run --rates 50,100 --step 60 \\
        --base-url https://<service>.run.app --no-pods

kubectl must already point at the cluster (`terraform -chdir=infra/gke output credentials`
prints the command), and the k6 image (loadtest/Dockerfile) must be in the registry. Each
run is stored under its label in runs/load_gke.json: per step, the requests k6 offered and
got through, their latency, and the API pods ready during the step; and the full timeline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

OUT = Path("runs") / "load_gke.json"
SAMPLE_SECONDS = 5
now, sleep = time.time, time.sleep  # replaced by a fake clock in the tests


def kubectl(*args: str, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, check=True
    )
    return done.stdout


def get(*args: str) -> dict:
    return json.loads(kubectl("get", *args, "-o", "json"))


def job(name: str, image: str, env: dict, cpu: str) -> dict:
    """A one-shot k6 Job with requests equal to limits, as Autopilot bills them."""
    resources = {"cpu": cpu, "memory": "2Gi"}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": {"app": "k6"}},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app": "k6"}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "k6",
                            "image": image,
                            "args": ["run", "--quiet", "cluster.js"],
                            "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                            "resources": {"requests": resources, "limits": resources},
                        }
                    ],
                },
            },
        },
    }


def k6_millicores(name: str) -> int | None:
    """k6's own CPU, to show the load generator is not what limits a step."""
    try:
        line = kubectl("top", "pod", "-l", f"job-name={name}", "--no-headers").split()
        return int(line[1].removesuffix("m"))
    except (subprocess.CalledProcessError, IndexError, ValueError):
        return None  # not running yet, or no metrics for it yet


def sample(t0: float, pods: bool, name: str = "") -> dict:
    """What the cluster looks like now, `t` seconds after the Job was created."""
    nodes = get("nodes")["items"]
    out = {
        "t": round(now() - t0, 1),
        "nodes": len(nodes),
        "node_types": sorted(
            n["metadata"]["labels"].get("node.kubernetes.io/instance-type", "?") for n in nodes
        ),
        "k6_mcpu": k6_millicores(name) if name else None,
    }
    if pods:
        status = get("deployment", "api").get("status", {})
        hpa = get("hpa", "api").get("status", {})
        cpu = None
        for m in hpa.get("currentMetrics") or []:
            if m.get("type") == "Resource":
                cpu = m["resource"]["current"].get("averageUtilization")
        out.update(
            ready=status.get("readyReplicas", 0),
            pods=status.get("replicas", 0),
            wanted=hpa.get("desiredReplicas"),
            cpu_pct=cpu,
        )
    return out


def started_at(name: str) -> float:
    """When the k6 container started, in epoch seconds, from the node's clock."""
    pod = get("pods", "-l", f"job-name={name}")["items"][0]
    state = pod["status"]["containerStatuses"][0]["state"]
    stamp = (state.get("terminated") or state.get("running"))["startedAt"]
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def run(
    label: str,
    rates: list[int],
    step: int,
    image: str,
    base_url: str = "http://api",
    cpu: str = "2",
    pods: bool = True,
    new_connections: bool = False,
) -> dict:
    name = f"k6-{label}-{int(now())}"
    env = {"BASE_URL": base_url, "RATES": ",".join(map(str, rates)), "STEP": step}
    if new_connections:
        env["NEW_CONNECTIONS"] = "1"
    kubectl("apply", "-f", "-", stdin=json.dumps(job(name, image, env, cpu)))
    t0, samples = now(), []
    while True:
        tick = now()
        samples.append(sample(t0, pods, name))
        status = get("job", name).get("status", {})
        if status.get("succeeded") or status.get("failed"):
            break
        sleep(max(0.0, SAMPLE_SECONDS - (now() - tick)))
    logs = kubectl("logs", f"job/{name}")
    line = next(x for x in logs.splitlines() if x.startswith("K6_SUMMARY "))
    summary = json.loads(line.removeprefix("K6_SUMMARY "))

    # Steps are in k6's time; samples in time since the Job was created.
    offset = started_at(name) - t0
    for s in summary["steps"]:
        lo, hi = offset + s["step"] * step, offset + (s["step"] + 1) * step
        inside = [x for x in samples if lo <= x["t"] < hi]
        if pods and inside:
            s["pods_ready_max"] = max(x["ready"] for x in inside)
            s["pods_ready_end"] = inside[-1]["ready"]
            util = [x["cpu_pct"] for x in inside if x["cpu_pct"] is not None]
            s["cpu_pct_mean"] = round(sum(util) / len(util)) if util else None
        if inside:
            s["nodes_max"] = max(x["nodes"] for x in inside)
            k6 = [x["k6_mcpu"] for x in inside if x.get("k6_mcpu") is not None]
            s["k6_mcpu_max"] = max(k6) if k6 else None
    return {
        "label": label,
        "started": datetime.fromtimestamp(t0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "k6_job": name,
        "k6_cpu": cpu,
        "k6_offset_s": round(offset, 1),
        **summary,
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="the run's key in runs/load_gke.json")
    ap.add_argument("--rates", required=True, help="requests per second per step, e.g. 100,200")
    ap.add_argument("--step", type=int, default=120, help="seconds per step")
    ap.add_argument("--image", required=True, help="the k6 image, from loadtest/Dockerfile")
    ap.add_argument("--base-url", default="http://api")
    ap.add_argument("--cpu", default="2", help="vCPUs for k6")
    ap.add_argument("--no-pods", action="store_true", help="the target is not the cluster's API")
    ap.add_argument("--new-connections", action="store_true", help="a connection per request")
    a = ap.parse_args(argv)
    rates = [int(r) for r in a.rates.split(",")]
    result = run(
        a.label,
        rates,
        a.step,
        a.image,
        a.base_url,
        a.cpu,
        pods=not a.no_pods,
        new_connections=a.new_connections,
    )
    runs = json.loads(OUT.read_text()) if OUT.exists() else {}
    runs[a.label] = result
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(runs, indent=2) + "\n")
    for s in result["steps"]:
        print(json.dumps({k: v for k, v in s.items() if k != "samples"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
