# marketplace-recs

[![ci](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml/badge.svg)](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

**Next-product recommendations for a marketplace session, on 42 million real events:
co-visitation candidates and a LightGBM ranker, scored on 200,000 sessions from a week
no model was fitted on, served over HTTP at 2.6 ms a request, with a visitor's events
reaching their recommendations 12 ms after they happen.**

The data is a month of a large multi-category online store. The task is the one a
product page answers in real time: given what a visitor has touched so far in this
session, which product will they touch next?

It also runs on Google Cloud, every resource in Terraform: the API is live on Cloud Run
and scales to zero, the nightly tables are built in BigQuery and come out identical, row for row,
to the local build, and the API was load-tested on GKE Autopilot from one pod to four.

## Live API

```
https://recs-api-s3kndyc7za-ew.a.run.app
```

`POST /recommend` takes a session's events, oldest first, and returns the products most
likely to come next; `GET /docs` lists every route. The service scales to zero, so the
first request after a quiet spell waits about 6 seconds for an instance to start. The
per-session routes work too, with each instance keeping its own sessions, since a free
demo pays for no Redis.

```bash
curl -s https://recs-api-s3kndyc7za-ew.a.run.app/recommend \
  -H 'Content-Type: application/json' \
  -d '{"events": [{"product_id": 1004767}, {"product_id": 1004833}], "k": 5}'
```

Two Samsung phones in, five Samsung phones out, the first phone the visitor saw at the
top: visitors often go back to a product they left.

## Results

200,000 sessions from the last week of October 2019, each cut at a random point, the
target being the next product the visitor touched. Every model is built only from the
weeks before.

| model | recall@20 | NDCG@10 | MRR@20 |
|---|---:|---:|---:|
| the most popular products of the last 7 days | 0.085 | 0.027 | 0.021 |
| the session's own products, most recent first | 0.164 | 0.099 | 0.091 |
| item to item: co-visitation of the last product | 0.442 | 0.191 | 0.155 |
| co-visitation of the whole session | 0.444 | 0.186 | 0.149 |
| **LightGBM ranker over the candidates** | **0.495** | **0.239** | **0.199** |

Served from a container limited to one vCPU, with one worker, the shape it runs in on
Cloud Run and GKE, with real test sessions as requests:

| concurrent clients | requests/s | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|
| 1 | 337 | 2.6 ms | 4.2 ms | 5.1 ms |
| 4 | 477 | 6.8 ms | 14.3 ms | 16.8 ms |
| 16 | 483 | 31 ms | 58 ms | 68 ms |
| 64 | 481 | 128 ms | 151 ms | 259 ms |

Events through Redpanda (the Kafka API) into Redis, which every API worker reads:

| measure | result |
|---|---|
| one consumer draining a backlog | 35,800 events/s, against 34 a second in the store's busiest minute |
| publish to readable, at 2,480 events/s | 20 ms p50, 29 ms p95 |
| an event posted to the API, to a recommendation that uses it | 12 ms p50, 21 ms p95, none of 200 sessions missed |

The same test week with the tables rebuilt every night instead of frozen at its start,
the ranker unchanged:

| tables | recall@20 | NDCG@10 | recall@20 on the last day |
|---|---:|---:|---:|
| frozen for the week | 0.495 | 0.239 | 0.483 |
| rebuilt nightly | **0.512** | **0.246** | **0.515** |

On Google Cloud ([infra/](infra/README.md)), the nightly tables built as SQL in BigQuery,
compared with the polars build of the same night:

| night | rows compared | identical | same top 20 from the API | BigQuery |
|---|---:|---:|---:|---:|
| 23 October | 5,852,752 | all | 5,000 of 5,000 sessions | 19 s, 3.3 GB scanned |
| 27 October | 6,087,817 | all | 5,000 of 5,000 sessions | 19 s, 3.9 GB scanned |

The API on GKE Autopilot, one vCPU per pod, loaded by k6 from inside the cluster with a
connection per request, and what Autopilot bills for the pods:

| pods | requests/s | p50 | p95 | p99 | € per million requests |
|---|---:|---:|---:|---:|---:|
| 1 | 100 | 6.1 ms | 8.4 ms | 10.5 ms | 0.13 |
| 1 | 150 | 6.5 ms | 181 ms | 505 ms | 0.09 |
| 4 | 400 | 12 ms | 50 ms | 130 ms | 0.13 |
| 3, by the autoscaler | 300 | 8.6 ms | 44 ms | 134 ms | 0.13 |
| 4, by the autoscaler | 400 | 12 ms | 124 ms | 338 ms | 0.13 |

The same image on Cloud Run, called from the same region, at most two instances:

| requests/s | p50 | p95 | p99 | instances | € per million requests |
|---:|---:|---:|---:|---:|---:|
| 50 | 11.7 ms | 15.6 ms | 19.6 ms | 1 | 0.75 |
| 100 | 11.2 ms | 21.7 ms | 37.4 ms | 2 | 0.68 |
| 150 | 14.9 ms | 53.6 ms | 64.0 ms | 2 | 0.65 |

## Findings

### 1. The ranker adds a quarter to NDCG@10, and stops where the candidates stop

Ranking the candidates with LightGBM (LambdaRank) lifts NDCG@10 from 0.191 to 0.239
and recall@20 from 0.442 to 0.495 over the item-to-item baseline. The candidates
contain the target in 53.9% of sessions, so the ranker already puts 92% of the targets
it can reach into its top 20. The next gain is in the candidates, not the ranker.

That ceiling is not cold start. Only 1.3% of targets are products never seen before the
test week, and only 1.2% of the last products have no co-visitation neighbours. The
missing targets are products that do have history but are not among the 40 strongest
neighbours of anything the session touched.

### 2. The last product carries most of the signal

Co-visitation from the last product alone scores the same as co-visitation from the whole
session, 0.442 against 0.444 recall@20. The ranker agrees: its two strongest features by
gain are the last product's time-weighted co-visitation score (21%) and how recently a
candidate was touched in the session (18%). Sessions go back and forth between a few
products, which is also why the session's own products, most recent first, beat
popularity by almost a factor of two.

### 3. Serving computes the same features as training, checked to the last ranking

The offline features come from batch joins over 200,000 sessions at once; a request has
one session and 2 ms. So serving has its own feature code, over compact arrays, and a
difference between the two would not raise: it would quietly rank worse in production
than offline. On 5,000 real test sessions the two paths give the same candidate set
every time, every feature within 1.1e-7 relative (float32 rounding), missing values in
the same places, and **the same top 20 in all 5,000 sessions**. The same check runs in
CI on a synthetic store, with a trained ranker, through the HTTP API.

### 4. The first load test measured TCP, not the model

The model ranks a session in 1.25 ms in process, yet the first load test measured 47 ms
a request and 21 requests a second from one client. Inside the container, a new
connection per request took 3.4 ms and a kept-alive one 44 ms, a constant penalty on
reused connections that is consistent with the 40 ms delayed-ACK stall. It was the same
with either of uvicorn's HTTP parsers on asyncio, and gone on uvloop: 2.1 ms. With
uvloop, and the route since moved onto the event loop (finding 8), one client gets 337
requests a second at 2.6 ms p50 through Docker's port forwarding, and one vCPU saturates at
about 480 a second.

### 5. An event reaches its recommendations in 12 ms, once two waits were found

With two workers, an event posted to one was invisible to the other, so session state
moved to Redis, written by a consumer that reads every event from Redpanda in order per
session. The first measurement put an event 367 ms behind at the median. The consumer
asked for batches of 2,000 with a 0.5 s wait, and at 2,480 events a second a batch rarely
fills, so most events sat out the wait. A 50 ms wait gave 45 ms; a 10 ms wait gave 49 ms,
because librdkafka leaves Nagle's algorithm on and that, not the wait, was now the floor.
Both changed, an event is readable 20 ms after it is published at the median, and a
backlog still drains at 35,800 events a second, since full batches never wait.

A restarted consumer also left 7 sessions in 200 more than 5 seconds stale: the old
process died on SIGTERM without leaving its group, and the new one waited out its session
timeout. With a clean exit, events flow again within half a second of a restart.

Through the API, from inside the compose network, posting an event takes 0.67 ms and an
event is in a recommendation 12 ms later. From Windows, through Docker Desktop's port
forwarding, every POST took 44 ms whatever the client did; those numbers measured the
forwarding, so the published ones come from inside the network.

### 6. Tables a week old cost 3 points of recall@20, and a nightly job wins them back

Every result above scores the test week with tables built before it starts, so by its
last day they are a week old. Scored day by day on the same 200,000 cases, with the
co-visitation matrices, product counts and popularity rebuilt each night from every
earlier event and the ranker left as it is, recall@20 rises from 0.495 to 0.512. The
gap grows with the frozen tables' age: 0.7 points on the second day, 2.1 on the fifth,
3.1 on the last.

The nightly job is an Airflow DAG over four plain functions: check the day's events are
complete and between half and twice the trailing week's median; build a new version; let
it through only if it loads, its catalogue moved less than 25%, its neighbour lists
overlap the serving version's, and it ranks a fixed set of sessions cleanly; then switch
a pointer the API reads on every request. On real data a night takes about 2 minutes:
1,435,327 events for 25 October, 1.01 times the trailing median, 159,663 products, 0.95
overlap. Publishing 26 October while recommendations were being requested, the API
moved to the new tables without a restart and without a failed request.

### 7. In BigQuery the nightly tables come out identical, once its sums stopped moving

The nightly statistics also run as SQL in BigQuery ([`sql/`](src/marketplace_recs/sql)):
one self-join over each session's 30 most recent events builds all three co-visitation
matrices, and window functions build the product statistics. Only the finished tables
come back, so the export, the gates and the API run unchanged. The first comparison with
the polars build matched the type and buy-to-buy matrices row for row, but 31 of the
time matrix's 2.8 million rows differed. Each was a tie: two neighbours whose weights
agreed to 16 significant digits, competing for a product's last place in its top 20.
Floating-point sums depend on the order of their terms, BigQuery adds them in a
different order from one run to the next, and so the same night, built twice, gave two
different tables. The time weight is linear in the event's time, so the SQL now sums
integer seconds and divides once per pair. On both nights compared, every neighbour row,
every product statistic and every exported serving array is identical to the polars
build, a rebuild writes the same rows, and the ranker scores the same 0.4951 recall@20 on
the test week with either.

CI cannot reach BigQuery, so it transpiles the SQL to DuckDB with sqlglot and runs it on
the synthetic store against the polars build. Thirteen deliberate changes to the SQL
(the 30-event cut, each event weight, both time limits, the cart filter, the time
weight, the tie-break, the one-day and seven-day windows, the latest-price rule) each
fail that test; three of them passed it until the synthetic store was given purchases,
events on the night's last day and a price that changes twice within a second.

A night takes 19 s of BigQuery and scans 3.9 GB, inside the free tier's terabyte a
month, where polars takes 169 s on a laptop. Reading the tables back, exporting,
checking and publishing included, a night on BigQuery runs in 40 s.

### 8. On GKE, a health check and the thread pool each wrecked the tail

The first ramp on GKE Autopilot fell over at 200 requests a second and never recovered:
up to 1,600 offered, it served at most 700 a second, at about 3 s p50. Kubernetes probed
readiness on `/health`, which FastAPI runs in its thread pool. On a saturated pod the
probe waited behind the requests being ranked, missed its 1 s timeout, and the pod left
the service. Its kept-alive connections went on sending it requests, so it stayed
saturated, while new connections piled onto the next pod, which then failed the same
way: of the 7 pods the autoscaler started, between none and five were in the service,
usually two. On an overloaded one-vCPU container, `/health` took 1.3 to 1.6 s, and a
coroutine route, `/ready`, 32 to 64 ms.

With the probe on `/ready`, one pod still answered 60 to 150 requests a second at 6 ms
p50 but 0.2 to 1.3 s p99. The kernel had throttled it in 1,329 of 4,132 scheduling
periods. LightGBM releases Python's GIL while it predicts, so a second thread of the pool
ran meanwhile, and a pod allowed one vCPU spent its quota in half of each 100 ms period
and stood still for the rest. Ranked on the event loop instead, one request at a time,
the pod was throttled in 6 of 4,344 periods, p99 fell to 9 to 12 ms at the same rates,
and a request cost 4.9 ms of CPU instead of 5.6. Letting the thread pool burst to a
second vCPU also fixed the latency, at 6.3 ms of CPU a request and only while the node
had a core to spare. The event loop has a price of its own: with two workers in one
container, kept-alive connections stay with whichever worker accepted them, that worker
can no longer borrow the other core, and 16 clients got 295 requests a second where the
thread pool had served 656. So the API runs one worker per vCPU, and scales by adding
pods or instances.

That brought back the first failure in another form: a saturated event loop answers
`/ready` only after its queue, and from 800 requests a second pods left the service
again. Readiness is now a TCP check. uvicorn listens only once the model is loaded, and
the kernel completes the handshake while the event loop is busy, so a saturated pod stays
in the service and the autoscaler adds pods instead.

### 9. A pod takes 100 requests a second at 10 ms p99, and the free trial stops the cluster at four

With a connection per request, as from many separate visitors, one pod answered 100
requests a second at 6.1 ms p50 and 10.5 ms p99, and saturated near 190. On kept-alive
connections the same pod had held 150 a second at 12 ms p99; here 150 lifted p99 to
505 ms, since accepting a connection for every request costs CPU too. Four pods took 400
a second at 130 ms p99 and saturated near 560, 140 a pod against 190 for one alone, with
two nodes shared between them and k6. Holding pods at 70% of their vCPU, the autoscaler followed a ramp
from one pod to four. Its one bad minute was the first doubling, from 100 to 200 requests
a second: CPU is averaged over a window, so the second pod was asked for 30 s later and
ready 5 s after that, and the requests that queued on the first pod meanwhile waited up
to 8 s, 3.7% of them past k6's 10 s limit. Billed on what the pods request, a million
requests cost €0.13 at 100 a second a pod.

The free trial sets the ceiling. A trial project gets 12 vCPUs and 250 GB of SSD and
cannot ask for more, and every Autopilot node boots from 100 GB of SSD, so the cluster
holds two nodes. Autopilot built one of 8 vCPUs and one of 2, and its request for a third
failed on quota: four API pods fit beside k6's three vCPUs, and the measurements stop
near 580 requests a second. Beyond that, the per-pod numbers are what would scale.

### 10. Cloud Run costs five times as much a request, and nothing while it waits

On Cloud Run the same image answered 50 to 150 requests a second at 11 to 15 ms p50 from
the same region, a few milliseconds more than inside the cluster, most likely the trip
through Google's front end. Cloud Run bills an instance while it holds a request, and at these
rates two instances were billed for most of every minute, 90 instance-seconds of it at
100 requests a second: €0.65 to €0.75 a million requests, against €0.13 on GKE. GKE bills
a pod whether or not it serves, though, €35 a month for one, and that buys 51 million
requests on Cloud Run, 20 a second on average. Below that, and for a public demo that
waits for visitors, Cloud Run is the cheaper home; above it, pods are.

Scaling to zero has its own cost. An idle instance went away about a quarter of an hour
after the last request, and the next request waited 6.4 s while one started; Cloud Run
measured the start at 5.1 to 6.1 s, where the same container is ready in 1.9 s on a
laptop.

## Data

REES46's [eCommerce behavior data from multi category
store](https://www.kaggle.com/mkechinov/ecommerce-behavior-data-from-multi-category-store),
collected by [REES46](https://rees46.com): every product view, cart addition and purchase
on the store. October 2019 holds 42,448,764 events (40.8 million views, 926,516 carts,
742,849 purchases) from 3.0 million users in 9.2 million sessions, over 166,794 products.
The terms ask for the source to be named, as above. `python -m marketplace_recs.data`
downloads the month from REES46 and writes it as typed Parquet; nothing from it is
committed.

## Evaluation design

- **Three windows**, so nothing is fitted on the period it is scored on:
  A, October 1 to 17, builds the statistics for the ranker's training sessions; B,
  October 17 to 24, supplies those sessions; C, October 24 to 31, is the test, scored
  with statistics from A and B.
- **One case per session.** Each session is cut once at a seeded random point. The target
  is the first product after the cut that differs from the last one before it, so
  reloading a page is not a prediction. Only sessions that touch at least two products
  can be cut; about half touch one.
- **Recall@20, NDCG@10 and MRR@20** over all 200,000 cases. A case whose target never
  appears counts as a miss, never as missing.
- **Deterministic.** Ties between events in the same second, and between candidates with
  equal scores, are broken by event order and product id, so a rerun reproduces every
  number here.

## How it works

- **Co-visitation.** Three matrices count the products sessions touch together: within 24
  hours, weighted towards the end of the period; within 24 hours, weighted by what
  happened to the second product (view 1, cart 6, purchase 3); and carts and purchases
  within 14 days. A session counts a pair once and contributes only its 30 most recent
  events, so a bot cannot dominate. Each product keeps its 20 strongest neighbours.
- **Candidates.** The session's own products and the 40 best co-visitation neighbours of
  its history: 33.6 a session on average.
- **Ranker.** 24 features: the six co-visitation scores (summed over the history and from
  the last product alone) and their total; the candidate's place in the session (in it or
  not, how recently, how often, its strongest event); its popularity over 1 and 7 days;
  its price, category and brand against the last product's; and the session's length,
  age and last event type. LambdaRank, 720 trees chosen by early stopping on held-out
  training sessions.
- **Serving.** FastAPI over the exported arrays (59 MB) and the LightGBM model, with a
  stateless `POST /recommend` and per-session `POST /sessions/{id}/events` and
  `GET /sessions/{id}/recommendations`, Prometheus metrics at `/metrics`. The model loads
  before the server listens. `/recommend` ranks on the event loop, one request at a time,
  so the API runs one worker per vCPU (finding 8). The image has no polars or pyarrow;
  CI builds it without a model and checks it answers 503 instead of failing to start.
- **Event stream.** `POST /sessions/{id}/events` publishes to Redpanda, keyed by session
  so a session's events stay in order on one partition; a consumer writes them to Redis in
  batches (`stream.py`); the API's workers read the shared state. `docker-compose.yml`
  runs the four, and CI starts them and watches events arrive.
- **Nightly job.** `dags/nightly_tables.py` (Airflow 3.3) runs `pipeline.py`'s check,
  build, validate and publish once a day. Versions are written to `model/versions/` and
  become visible only when complete; `model/CURRENT` names the one serving, and the API
  swaps on the next request. Airflow 3 reads a bare `@daily` as a trigger, so the DAG
  declares a data-interval timetable and each run covers the day that just ended. CI
  installs Airflow 3.3.2, parses the DAG and runs it once with the steps stubbed. With
  `RECS_WAREHOUSE=bigquery`, check and build run as SQL in BigQuery (`warehouse.py`).
- **Google Cloud.** Two Terraform stacks ([infra/](infra/README.md)). `core` holds the
  project, a budget that emails at €5 and €10 of spend, the image registry,
  BigQuery and the Cloud Run service; `gke` holds an Autopilot cluster with the API's
  deployment, service and CPU autoscaler, created for a load test and destroyed after
  it. Cloud Run and GKE run the same image, pinned by digest. k6 runs as a Job inside
  the cluster (`cluster.py`), so a test measures the service and not the internet, and
  the cluster's state is read every 5 s while it runs. CI checks both stacks are
  formatted and valid.
- **One laptop.** Converting the month takes 61 s, building the matrices 83 s, training
  89 s, and scoring the 200,000 test sessions 50 s.

## Reproduce

```bash
pip install -e ".[dev]"
python -m marketplace_recs.data                   # 1.7 GB download, typed Parquet
python -m marketplace_recs.experiment baselines   # runs/baselines_C.json
python -m marketplace_recs.ranker                 # runs/ranker_C.json
python -m marketplace_recs.online export          # model/: arrays and ranker for serving
python -m marketplace_recs.online parity          # runs/parity_C.json
pytest                                            # needs no data

docker build -t marketplace-recs . && docker run -p 8080:8080 --cpus 1 marketplace-recs
python -m marketplace_recs.loadtest               # request bodies from real test sessions
k6 run -e BASE_URL=http://localhost:8080 -e VUS=4 loadtest/recommend.js

python -m marketplace_recs.refresh                # runs/refresh_C.json: frozen against nightly
python -m marketplace_recs.pipeline 2019-10-25    # one night: check, build, validate, publish

docker compose up -d --build                      # Redpanda, Redis, the API, the consumer
python -m marketplace_recs.stream replay --speed 120          # a day of events, 120x
docker compose run --rm -v "$PWD/loadtest:/app/loadtest" consumer \
    python -m marketplace_recs.stream e2e --api http://api:8080

# Google Cloud, step by step in infra/README.md
terraform -chdir=infra/core apply                 # project, budget, registry, BigQuery, Cloud Run
python -m marketplace_recs.warehouse load         # the month into BigQuery
python -m marketplace_recs.warehouse parity 2019-10-23        # SQL against polars
python -m marketplace_recs.pipeline 2019-10-27 --warehouse bigquery
terraform -chdir=infra/gke apply                  # Autopilot, only for a load test
python -m marketplace_recs.cluster --label ramp --image <registry>/k6:2 \
    --rates 100,200,300,450,600,800,1000 --step 90 --new-connections
terraform -chdir=infra/gke destroy
```

## Limits and next steps

The ranker is trained once; only the tables it reads are rebuilt nightly, and the nightly
job runs from a laptop against BigQuery, not on a managed Airflow. The cloud numbers
come from a free trial project: the cluster was measured up to four pods, with k6 on the
same nodes, and the live API keeps its sessions per instance, without Redis. Next: a
Grafana dashboard and a drift check on the live features, and an offline replay of the
ranker against the baseline with a power analysis, CUPED and interleaving.

## License

Code: MIT. Data: REES46, under its own terms, never redistributed here. Built by
[Mateus Aleixo](https://github.com/mateus-aleixo).
