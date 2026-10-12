# Results

Every measured result, with how it was measured. The README has the short version, and [findings.md](findings.md) explains each one.

## Offline evaluation

The test set is 200,000 sessions from the last week of October 2019. Each session is cut
at a random point, and the target is the next product the visitor looked at. Every model
is built only from the weeks before.

| model | recall@20 | NDCG@10 | MRR@20 |
|---|---:|---:|---:|
| the most popular products of the last 7 days | 0.085 | 0.027 | 0.021 |
| the session's own products, most recent first | 0.164 | 0.099 | 0.091 |
| item to item: co-visitation of the last product | 0.442 | 0.191 | 0.155 |
| co-visitation of the whole session | 0.444 | 0.186 | 0.149 |
| LightGBM ranker over the candidates | 0.495 | 0.239 | 0.199 |

## Serving latency

Serving latency from a container limited to one vCPU with one worker, which is how it runs
on Cloud Run and GKE, using real test sessions as requests:

| concurrent clients | requests per second | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|
| 1 | 337 | 2.6 ms | 4.2 ms | 5.1 ms |
| 4 | 477 | 6.8 ms | 14.3 ms | 16.8 ms |
| 16 | 483 | 31 ms | 58 ms | 68 ms |
| 64 | 481 | 128 ms | 151 ms | 259 ms |

## Event stream

Events through Redpanda (which speaks the Kafka API) into Redis, which every API worker
reads:

| measure | result |
|---|---|
| one consumer working through a backlog | 35,800 events per second (the store's busiest minute had 34 per second) |
| publish to readable, at 2,480 events per second | 20 ms p50, 29 ms p95 |
| an event posted to the API, to a recommendation that uses it | 12 ms p50, 21 ms p95, none of 200 sessions missed |

## Nightly tables

The same test week with the tables rebuilt every night instead of frozen at the start of
the week, with the same ranker:

| tables | recall@20 | NDCG@10 | recall@20 on the last day |
|---|---:|---:|---:|
| frozen for the week | 0.495 | 0.239 | 0.483 |
| rebuilt nightly | 0.512 | 0.246 | 0.515 |

## BigQuery

On Google Cloud ([infra/](../infra/README.md)), the nightly tables built as SQL in BigQuery,
compared with the polars build of the same night:

| night | rows compared | identical | same top 20 from the API | BigQuery |
|---|---:|---:|---:|---:|
| 23 October | 5,852,752 | all | 5,000 of 5,000 sessions | 19 s, 3.3 GB scanned |
| 27 October | 6,087,817 | all | 5,000 of 5,000 sessions | 19 s, 3.9 GB scanned |

## GKE Autopilot

The API on GKE Autopilot, one vCPU per pod, loaded by k6 from inside the cluster with a new
connection for each request, and what Autopilot bills for the pods:

| pods | requests per second | p50 | p95 | p99 | € per million requests |
|---|---:|---:|---:|---:|---:|
| 1 | 100 | 6.1 ms | 8.4 ms | 10.5 ms | 0.13 |
| 1 | 150 | 6.5 ms | 181 ms | 505 ms | 0.09 |
| 4 | 400 | 12 ms | 50 ms | 130 ms | 0.13 |
| 3, chosen by the autoscaler | 300 | 8.6 ms | 44 ms | 134 ms | 0.13 |
| 4, chosen by the autoscaler | 400 | 12 ms | 124 ms | 338 ms | 0.13 |

## Cloud Run

The same image on Cloud Run, called from the same region, with at most two instances:

| requests per second | p50 | p95 | p99 | instances | € per million requests |
|---:|---:|---:|---:|---:|---:|
| 50 | 11.7 ms | 15.6 ms | 19.6 ms | 1 | 0.75 |
| 100 | 11.2 ms | 21.7 ms | 37.4 ms | 2 | 0.68 |
| 150 | 14.9 ms | 53.6 ms | 64.0 ms | 2 | 0.65 |

## Offline A/B test

The whole test week replayed as an online experiment: 1,126,667 sessions from 653,559
visitors, each scored with both lists. Smaller effects come from treatments that show the
ranker's list in only some of the sessions. The table gives the days until each test has
80% power at a 5% significance level (two-sided), and how many times more visitors the
A/B test needs than interleaving:

| ranker's list shown in | true lift in recall@20 | A/B test | interleaving | visitors, A/B test over interleaving |
|---|---:|---:|---:|---:|
| every session | 5.3 points | 1 day | 1 day | 8.7 |
| half of the sessions | 2.7 points | 1 day | 1 day | 9.3 |
| a fifth | 1.1 points | 1 day | 1 day | 9.2 |
| a tenth | 0.51 points | 3 days | 1 day | 9.8 |
| a twentieth | 0.27 points | more than the 8 days (74% power at the end) | 1 day | 8.8 |

## Drift check

The drift check against faults planted in one day of real requests, each in a share of
the sessions. The number is the AUC of a classifier that tries to tell the day from the
day before (0.5 means no difference). Clean, the day reads 0.508.

| planted fault | in every session | in a quarter | in a tenth | feature the check names |
|---|---:|---:|---:|---|
| prices sent in cents | 0.999 | 0.628 | 0.569 | price_ratio |
| timestamps sent in milliseconds | 0.986 | 0.618 | 0.544 | s_seconds |
| brand missing | 0.945 | 0.616 | 0.549 | same_brand |
| category missing | 1.000 | 0.627 | 0.565 | same_category |
| every event sent as a view | 0.529 | 0.513 | 0.513 | none |

The dashboard below is from a 24-minute replay of 28 October through the API, 2.5 times
faster than real time, with a broken client sending prices in cents for a quarter of the
sessions between minutes 10 and 16. The drift AUC reads 0.54 to 0.56 before the fault,
crosses the alert threshold of 0.6 within 87 seconds of the fault starting, stays at 0.65 to
0.67 with `price_ratio` as the top feature, and falls back under 0.6 two minutes after the
fault ends.

![The Grafana dashboard during a replay of 28 October with a broken client](dashboard.png)

## How the results were measured

- **Three windows**, so no model is trained on the period it is scored on. Window A, 1 to
  17 October, builds the statistics for the ranker's training sessions. Window B, 17 to 24
  October, supplies those sessions. Window C, 24 to 31 October, is the test, scored with
  statistics from A and B.
- **One case per session.** Each session is cut once at a seeded random point. The target
  is the first product after the cut that differs from the last product before it, so
  reloading a page does not count as a prediction. Only sessions that touch at least two
  products can be cut, and about half of all sessions touch only one.
- **Recall@20, NDCG@10 and MRR@20** over all 200,000 cases. A case whose target never
  appears in the list counts as a miss, so no case is left out.
- **Deterministic.** Ties between events in the same second, and between candidates with
  equal scores, are broken by event order and product id, so a rerun reproduces every
  number here.
