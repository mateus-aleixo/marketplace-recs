# marketplace-recs

[![ci](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml/badge.svg)](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Next-product recommendations for an online store, built on 42 million real events from one
month of a large multi-category shop. Given the products a visitor has looked at so far in
a session, the system predicts which product they will look at next, using co-visitation
candidates and a LightGBM ranker. It is evaluated on 200,000 sessions from a week that no
model was trained on, and it is served over HTTP, fed by an event stream, rebuilt every
night, deployed on Google Cloud and monitored for drift.

## Results

| model | recall@20 | NDCG@10 | MRR@20 |
|---|---:|---:|---:|
| the most popular products of the last 7 days | 0.085 | 0.027 | 0.021 |
| the session's own products, most recent first | 0.164 | 0.099 | 0.091 |
| item to item: co-visitation of the last product | 0.442 | 0.191 | 0.155 |
| co-visitation of the whole session | 0.444 | 0.186 | 0.149 |
| LightGBM ranker over the candidates | 0.495 | 0.239 | 0.199 |

- **Ranker.** The candidates contain the target in 53.9% of sessions, so the ranker already
  places 92% of the targets it can reach in its top 20. Better candidates are the next gain.
- **Serving.** 2.6 ms p50 per request on one vCPU, with features identical to the offline
  pipeline on 5,000 real sessions, down to the same top 20.
- **Event stream.** A visitor's latest event reaches their recommendations 12 ms later,
  through Redpanda into Redis.
- **Nightly tables.** Rebuilding the tables every night raises recall@20 from 0.495 to 0.512.
  Tables a week old lose 3.1 points by the last day of the week.
- **BigQuery.** The nightly tables built as SQL are identical, row for row, to the local
  build, in 19 s per night.
- **GKE and Cloud Run.** Four autoscaled pods served 400 requests per second at 12 ms p50,
  for €0.13 per million requests, against €0.65 to €0.75 on Cloud Run.
- **Offline A/B test.** Replayed as an online test, interleaving needs about a ninth of the
  visitors an A/B test needs for the same power, and CUPED removes 1.25% of the variance.
- **Monitoring.** A drift check on the live features raised an alert within 87 s when a
  replayed client sent prices in cents for a quarter of the sessions.

Every table is in [docs/results.md](docs/results.md), and
[docs/findings.md](docs/findings.md) explains what each one showed.

## Live API

```
https://recs-api-s3kndyc7za-ew.a.run.app
```

`POST /recommend` takes a session's events, oldest first, and returns the products most
likely to come next. `GET /docs` lists every route. The service scales to zero, so the
first request after a quiet period waits about 6 seconds while an instance starts.

```bash
curl -s https://recs-api-s3kndyc7za-ew.a.run.app/recommend \
  -H 'Content-Type: application/json' \
  -d '{"events": [{"product_id": 1004767}, {"product_id": 1004833}], "k": 5}'
```

## Monitoring

![The Grafana dashboard during a replay of 28 October with a broken client](docs/dashboard.png)

The dashboard during a 24-minute replay of real traffic through the API, with a broken
client sending prices in cents for a quarter of the sessions between minutes 10 and 16.
The drift AUC reads 0.54 to 0.56 before the fault and 0.65 to 0.67 during it, crosses the
alert threshold of 0.6 within 87 seconds, and names `price_ratio` as the feature that
changed.

## How it works

- **Candidates.** Three co-visitation matrices give each session its own products and the
  40 strongest neighbours of its history, 33.6 candidates on average.
- **Ranker.** LightGBM LambdaRank with 24 features: co-visitation scores, the candidate's
  place in the session, its popularity, its price, category and brand compared with the last
  product's, and the session's length and age.
- **Serving.** FastAPI over the exported arrays and the model, in an image without polars.
  CI checks that serving computes the same features as training.
- **Event stream.** Events go to Redpanda, keyed by session, and a consumer writes them to
  Redis, which every API worker reads.
- **Nightly job.** An Airflow 3 DAG checks the day's events, builds a new version of the
  tables, validates it and publishes it, and the API switches without a restart. The build
  can also run as SQL in BigQuery.
- **Google Cloud.** Terraform for the project, a budget that alerts at €5 and €10, BigQuery,
  Cloud Run, and a GKE Autopilot cluster created only for the load tests.
- **Offline experiment.** `replay.py` scores both lists for every session of the test week,
  and `abtest.py` computes delta-method variances, bootstrap power, CUPED and team-draft
  interleaving.
- **Monitoring.** Prometheus and Grafana run under a docker-compose profile, and `drift.py`
  compares sampled live requests with the first traffic each version served.

More detail is in [docs/architecture.md](docs/architecture.md).

## Evaluation design

- Three windows, so no model is scored on the period it was trained on: window A (1 to 17
  October) builds the statistics for the ranker's training sessions, window B (17 to 24
  October) supplies those sessions, and window C (24 to 31 October) is the test.
- Each session is cut once at a seeded random point, and the target is the next product
  after the cut that differs from the last one before it.
- Recall@20, NDCG@10 and MRR@20 over all 200,000 test cases. Ties are broken
  deterministically, so a rerun reproduces every number.

## Reproduce

```bash
pip install -e ".[dev]"
python -m marketplace_recs.data                   # 1.7 GB download, typed Parquet
python -m marketplace_recs.experiment baselines   # runs/baselines_C.json
python -m marketplace_recs.ranker                 # runs/ranker_C.json
python -m marketplace_recs.online export          # model/: arrays and ranker for serving
pytest                                            # needs no data

docker compose --profile monitoring up -d --build # API, stream, drift check, Prometheus, Grafana
python -m marketplace_recs.traffic --minutes 24   # replayed traffic; dashboard at localhost:3000
```

The other commands (load tests, the nightly job, the experiments and the drift evaluation)
are in [docs/architecture.md](docs/architecture.md#commands), and the Google Cloud setup
is in [infra/README.md](infra/README.md).

## Data

REES46's [eCommerce behavior data from multi category
store](https://www.kaggle.com/mkechinov/ecommerce-behavior-data-from-multi-category-store),
collected by [REES46](https://rees46.com): every product view, cart addition and purchase
on the store. October 2019 has 42,448,764 events (40.8 million views, 926,516 carts and
742,849 purchases) from 3.0 million users in 9.2 million sessions, over 166,794 products.
The terms ask for the source to be named, as above. `python -m marketplace_recs.data`
downloads it, and none of the data is committed.

## Limits

The ranker is trained once, and only the tables it reads are rebuilt nightly. The cloud
numbers come from a free trial project, with at most four pods. The experiment is an
offline replay, so it assumes that showing a different list would not have changed what
visitors looked at next. The drift check only watches the features, and it cannot see a
fault that leaves them unchanged.

## License

Code: MIT. Data: REES46, under its own terms, never redistributed here. Built by
[Mateus Aleixo](https://github.com/mateus-aleixo).
