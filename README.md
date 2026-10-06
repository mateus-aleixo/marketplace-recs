# marketplace-recs

[![ci](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml/badge.svg)](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

**Next-product recommendations for a marketplace session, on 42 million real events:
co-visitation candidates and a LightGBM ranker, scored on 200,000 sessions from a week
no model was fitted on, and served over HTTP at 2.6 ms a request.**

The data is a month of a large multi-category online store. The task is the one a
product page answers in real time: given what a visitor has touched so far in this
session, which product will they touch next?

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

Served from a container limited to 2 vCPUs, with real test sessions as requests:

| concurrent clients | requests/s | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|
| 1 | 364 | 2.6 ms | 3.9 ms | 5.2 ms |
| 4 | 542 | 6.9 ms | 11.1 ms | 13.7 ms |
| 16 | 656 | 23 ms | 45 ms | 59 ms |
| 64 | 675 | 92 ms | 133 ms | 165 ms |

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
uvloop, one client gets 364 requests a second at 2.6 ms p50 through Docker's port
forwarding, and the two vCPUs saturate at about 670 a second.

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
  `GET /sessions/{id}/recommendations`, Prometheus metrics at `/metrics`. The image has no
  polars or pyarrow; CI builds it without a model and checks it answers 503 instead of
  failing to start.
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

docker build -t marketplace-recs . && docker run -p 8080:8080 marketplace-recs
python -m marketplace_recs.loadtest               # request bodies from real test sessions
k6 run -e BASE_URL=http://localhost:8080 -e VUS=4 loadtest/recommend.js
```

## Limits and next steps

The statistics are frozen when the test week starts, and session state lives in one
process. Next: an event stream that updates sessions across replicas as events happen, a
nightly rebuild of the co-visitation tables, and the same measurements on a cloud
deployment.

## License

Code: MIT. Data: REES46, under its own terms, never redistributed here. Built by
[Mateus Aleixo](https://github.com/mateus-aleixo).
