# How it works

Each component in more detail. The README has a short summary.

- **Co-visitation.** Three matrices count the products that sessions touch together:
  within 24 hours, weighted towards the end of the period; within 24 hours, weighted by
  what happened to the second product (view 1, cart 6, purchase 3); and carts and
  purchases within 14 days. A session counts a pair once and contributes only its 30 most
  recent events, so a bot cannot dominate the counts. Each product keeps its 20 strongest
  neighbours.
- **Candidates.** The session's own products and the 40 best co-visitation neighbours of
  its history, 33.6 per session on average.
- **Ranker.** 24 features: the six co-visitation scores (summed over the history and from
  the last product alone) and their total; the candidate's place in the session (whether
  it is there, how recently, how often, and its strongest event); its popularity over 1
  and 7 days; its price, category and brand compared with the last product's; and the
  session's length, age and last event type. LambdaRank with 720 trees, chosen by early
  stopping on held-out training sessions.
- **Serving.** FastAPI over the exported arrays (59 MB) and the LightGBM model, with a
  stateless `POST /recommend`, per-session `POST /sessions/{id}/events` and
  `GET /sessions/{id}/recommendations`, and Prometheus metrics at `/metrics`. The model
  loads before the server starts listening. `/recommend` ranks on the event loop, one
  request at a time, so the API runs one worker per vCPU (finding 8). The image contains
  no polars or pyarrow. CI builds it without a model and checks that it answers 503
  instead of failing to start.
- **Event stream.** `POST /sessions/{id}/events` publishes to Redpanda, keyed by session so
  that a session's events stay in order on one partition. A consumer writes them to Redis
  in batches (`stream.py`), and the API's workers read the shared state.
  `docker-compose.yml` runs the four services, and CI starts them and checks that events
  arrive.
- **Nightly job.** `dags/nightly_tables.py` (Airflow 3.3) runs the check, build, validate
  and publish steps in `pipeline.py` once a day. Versions are written to `model/versions/`
  and only become visible when complete. `model/CURRENT` names the version being served,
  and the API switches on its next request. Airflow 3 treats a bare `@daily` as a trigger,
  so the DAG declares a data-interval timetable and each run covers the day that has just
  ended. CI installs Airflow 3.3.2, parses the DAG and runs it once with the steps
  stubbed. With `RECS_WAREHOUSE=bigquery`, the check and build steps run as SQL in
  BigQuery (`warehouse.py`).
- **Google Cloud.** Two Terraform stacks ([infra/](../infra/README.md)). `core` holds the
  project, a budget that sends an email at €5 and €10 of spend, the image registry,
  BigQuery and the Cloud Run service. `gke` holds an Autopilot cluster with the API's
  deployment, service and CPU autoscaler, created for a load test and destroyed after it.
  Cloud Run and GKE run the same image, pinned by digest. k6 runs as a Job inside the
  cluster (`cluster.py`), so a test measures the service and not the internet, and the
  cluster's state is recorded every 5 s during the test. CI checks that both stacks are
  formatted and valid.
- **Offline experiment.** `replay.py` builds both lists for every session of the test
  week, caches them, and passes per-visitor sums to `abtest.py`. Recall@20 is a ratio over
  visitors with a delta-method variance (Deng, Knoblich and Lu, 2018). Power comes from the
  baseline's variance and from 1,000 Poisson bootstrap replicates, each a fresh 50/50
  assignment of resampled visitors, computed as matrix products. CUPED works on the ratio
  linearised per visitor (Deng, Xu, Kohavi and Walker, 2013), with covariates from before
  the week only. Team-draft interleaving (Radlinski, Kurup and Joachims, 2008) is
  vectorised across sessions and checked against a one-session version. A rerun writes the
  same numbers.
- **Monitoring.** With `docker compose --profile monitoring`, Prometheus scrapes the API,
  the stream consumer and the drift check every 5 s, and Grafana shows a dashboard that is
  provisioned from `monitoring/` and versioned with the code. The API exports request
  counts, latency, candidates per request, the share of requests whose last product is
  missing from the tables, and the size of the serving catalogue. Its two workers are
  separate processes, so they write their metrics to a shared directory and `/metrics`
  sums them (prometheus_client's multiprocess mode). Without it, scrapes after 200
  requests read 76 or 124, depending on which worker answered. The consumer exports events
  written, publish-to-readable latency and lag. The API copies the feature rows of a fifth
  of its requests to a capped Redis list, in batches, and the drift worker (`drift.py`)
  compares the latest 1,500 requests with the baseline every 15 s. A test checks that every
  metric the dashboard queries is one the code exports, and CI starts the stack and checks
  that Prometheus scrapes all three targets and Grafana serves the dashboard.
- **One laptop.** Converting the month takes 61 s, building the matrices 83 s, training
  89 s, and scoring the 200,000 test sessions 50 s. Replaying the test week as an online
  experiment takes 6.4 minutes and 4.6 GB, most of it spent building 1.1 million pairs of
  lists, and evaluating the drift check takes 6 minutes.

## Commands

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
python -m marketplace_recs.replay --cases 200000  # the cached sample: both arms as in Results
python -m marketplace_recs.replay                 # runs/abtest_C.json: the test week as an online test
python -m marketplace_recs.drift evaluate         # runs/drift_C.json: the drift check on real data

docker compose up -d --build                      # Redpanda, Redis, the API, the consumer
python -m marketplace_recs.stream replay --speed 120          # a day of events, 120x
docker compose run --rm -v "$PWD/loadtest:/app/loadtest" consumer \
    python -m marketplace_recs.stream e2e --api http://api:8080

docker compose --profile monitoring up -d --build # plus the drift check, Prometheus, Grafana
python -m marketplace_recs.traffic --start 2019-10-28T09:00:00 --minutes 24 --speed 2.5 \
    --fault price_in_cents --share 0.25 --fault-from 10 --fault-to 16
                                                  # dashboard at http://localhost:3000

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
