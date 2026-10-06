# marketplace-recs

[![ci](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml/badge.svg)](https://github.com/mateus-aleixo/marketplace-recs/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

**Next-product recommendations for a marketplace session, on 42 million real events:
co-visitation candidates and a LightGBM ranker, scored on 200,000 sessions from a week
no model was fitted on.**

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
gain are the last product's time-weighted co-visitation score (22%) and how recently a
candidate was touched in the session (20%). Sessions go back and forth between a few
products, which is also why the session's own products, most recent first, beat
popularity by almost a factor of two.

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
  age and last event type. LambdaRank, 472 trees chosen by early stopping on held-out
  training sessions.
- **One laptop.** Converting the month takes 61 s, building the matrices 83 s, training
  2 minutes, and scoring the 200,000 test sessions 40 s.

## Reproduce

```bash
pip install -e ".[train,dev]"
python -m marketplace_recs.data                   # 1.7 GB download, typed Parquet
python -m marketplace_recs.experiment baselines   # runs/baselines_C.json
python -m marketplace_recs.ranker                 # runs/ranker_C.json
pytest                                            # needs no data
```

## Limits and next steps

Everything above is offline, and the statistics are frozen when the test week starts.
Next is serving it the way a marketplace would: an API that answers from live session
state, an event stream that updates sessions as they happen, and a nightly rebuild of the
co-visitation tables, measured under load.

## License

Code: MIT. Data: REES46, under its own terms, never redistributed here. Built by
[Mateus Aleixo](https://github.com/mateus-aleixo).
