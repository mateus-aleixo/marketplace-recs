// Open-model load inside the cluster: requests arrive at a set rate for each step whatever
// the latency, so an overloaded service shows as latency and errors, not as fewer requests.
//
//   k6 run -e BASE_URL=http://api -e RATES=100,200,400 -e STEP=120 cluster.js
//
// NEW_CONNECTIONS=1 opens a connection per request, as from many separate visitors. With
// kept-alive connections, a pod the autoscaler adds receives only connections opened after
// it is ready, and the pods already running keep their queues.
//
// Each request carries its step as a tag, and the summary reports every step on its own:
// one line on stdout, prefixed K6_SUMMARY, which python -m marketplace_recs.cluster reads.
import http from "k6/http";
import exec from "k6/execution";
import { SharedArray } from "k6/data";

const sessions = new SharedArray("sessions", () => JSON.parse(open("./sessions.json")));
const RATES = (__ENV.RATES || "100").split(",").map(Number);
const STEP = Number(__ENV.STEP || 60); // seconds per rate
const URL = `${__ENV.BASE_URL || "http://api"}/recommend`;

const stages = [];
RATES.forEach((rate) => {
  stages.push({ target: rate, duration: "1s" }, { target: rate, duration: `${STEP - 1}s` });
});

// A threshold on a tagged metric is what makes k6 keep that sub-metric in the summary.
const thresholds = {};
RATES.forEach((_, i) => {
  thresholds[`http_req_duration{step:${i}}`] = ["max>=0"];
  thresholds[`http_reqs{step:${i}}`] = ["count>=0"];
  thresholds[`http_req_failed{step:${i}}`] = ["rate>=0"];
});

export const options = {
  discardResponseBodies: true,
  noConnectionReuse: __ENV.NEW_CONNECTIONS === "1",
  scenarios: {
    steps: {
      executor: "ramping-arrival-rate",
      startRate: RATES[0],
      timeUnit: "1s",
      stages: stages,
      preAllocatedVUs: Number(__ENV.VUS || 100),
      maxVUs: Number(__ENV.MAX_VUS || 2000),
    },
  },
  thresholds: thresholds,
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

export default function () {
  const elapsed = Date.now() - exec.scenario.startTime;
  const step = Math.min(Math.floor(elapsed / (STEP * 1000)), RATES.length - 1);
  const events = sessions[Math.floor(Math.random() * sessions.length)];
  http.post(URL, JSON.stringify({ events: events, k: 20 }), {
    headers: { "Content-Type": "application/json" },
    tags: { step: String(step) },
    timeout: "10s",
  });
}

const r2 = (x) => (x === undefined || x === null ? null : Math.round(x * 100) / 100);

export function handleSummary(data) {
  const m = data.metrics;
  const steps = RATES.map((rate, i) => {
    const d = m[`http_req_duration{step:${i}}`];
    const n = m[`http_reqs{step:${i}}`];
    const f = m[`http_req_failed{step:${i}}`];
    return {
      step: i,
      offered_rps: rate,
      requests: n ? n.values.count : 0,
      rps: n ? r2(n.values.count / STEP) : 0,
      p50_ms: d ? r2(d.values["p(50)"]) : null,
      p95_ms: d ? r2(d.values["p(95)"]) : null,
      p99_ms: d ? r2(d.values["p(99)"]) : null,
      max_ms: d ? r2(d.values.max) : null,
      failed: f ? f.values.rate : null,
    };
  });
  const out = {
    base_url: __ENV.BASE_URL || "http://api",
    new_connections: __ENV.NEW_CONNECTIONS === "1",
    step_seconds: STEP,
    dropped_iterations: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
    steps: steps,
  };
  return { stdout: "K6_SUMMARY " + JSON.stringify(out) + "\n" };
}
