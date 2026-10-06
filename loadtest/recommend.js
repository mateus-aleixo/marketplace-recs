// Load test for POST /recommend: each virtual user sends real test sessions back to back.
//
//   python -m marketplace_recs.loadtest                      # writes loadtest/sessions.json
//   k6 run -e BASE_URL=http://localhost:8080 -e VUS=8 -e OUT=load.json loadtest/recommend.js
import http from "k6/http";
import { check } from "k6";
import { SharedArray } from "k6/data";

const sessions = new SharedArray("sessions", () => JSON.parse(open("./sessions.json")));
const vus = Number(__ENV.VUS || 8);

export const options = {
  scenarios: {
    steady: { executor: "constant-vus", vus: vus, duration: __ENV.DURATION || "30s" },
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

export default function () {
  const events = sessions[Math.floor(Math.random() * sessions.length)];
  const res = http.post(`${__ENV.BASE_URL}/recommend`, JSON.stringify({ events: events, k: 20 }), {
    headers: { "Content-Type": "application/json" },
  });
  check(res, { "status 200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const out = {
    vus: vus,
    requests: data.metrics.http_reqs.values.count,
    rps: Math.round(data.metrics.http_reqs.values.rate * 10) / 10,
    p50_ms: Math.round(d["p(50)"] * 100) / 100,
    p95_ms: Math.round(d["p(95)"] * 100) / 100,
    p99_ms: Math.round(d["p(99)"] * 100) / 100,
    failed: data.metrics.http_req_failed.values.rate,
  };
  return { [__ENV.OUT || "load.json"]: JSON.stringify(out, null, 2) + "\n", stdout: JSON.stringify(out) + "\n" };
}
