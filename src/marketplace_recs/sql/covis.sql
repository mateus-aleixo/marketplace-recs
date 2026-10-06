-- Co-visitation from every event before @end_ts: covisit.py's three matrices in one pass.
--
--   time     pairs within 24 hours, weighted 1 to 4 by how late the first event came
--   type     the same pairs, weighted by the second product's event: view 1, cart 6,
--            purchase 3
--   buy2buy  carts and purchases only, within 14 days
--
-- A session counts a pair once, at its strongest weight, and only its 30 most recent
-- distinct (product, event type) events take part. Each product keeps its 20 strongest
-- neighbours, ties going to the lower product id.
--
-- Floating-point sums depend on the order BigQuery adds them in, which changes between
-- runs, so neighbours with equal weights would swap places from one run of a night to the
-- next. The time weight is linear in the first event's time, so it is summed exactly, as
-- integer seconds, and computed once per pair: the same night always gives the same tables.
WITH latest AS (
  -- A session's distinct (product, event type) pairs, each at its latest event.
  SELECT session, product_id, event_type, MAX(UNIX_SECONDS(event_time)) AS ts, MAX(seq) AS seq
  FROM recs.events
  WHERE event_time < @end_ts AND session IS NOT NULL
  GROUP BY session, product_id, event_type
),
prepared AS (
  -- Ties within a second go to the event later in the file.
  SELECT session, product_id, event_type, ts
  FROM latest
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY session ORDER BY ts DESC, seq DESC) <= 30
),
bounds AS (
  SELECT MIN(ts) AS t0, MAX(ts) AS t1 FROM prepared
),
per_session AS (
  SELECT
    a.session,
    a.product_id,
    b.product_id AS neighbour,
    -- The pair's strongest time weight comes from its latest first event.
    MAX(IF(ABS(a.ts - b.ts) < 86400, a.ts, NULL)) AS time_ts,
    MAX(IF(ABS(a.ts - b.ts) < 86400,
           CASE b.event_type WHEN 0 THEN 1.0 WHEN 1 THEN 6.0 WHEN 2 THEN 3.0 END, NULL))
      AS type_w,
    MAX(IF(a.event_type > 0 AND b.event_type > 0, 1.0, NULL)) AS buy2buy_w
  FROM prepared AS a
  JOIN prepared AS b
    ON a.session = b.session AND a.product_id != b.product_id
  WHERE ABS(a.ts - b.ts) < 14 * 86400
  GROUP BY a.session, a.product_id, b.product_id
),
summed AS (
  -- Each session adds 1 + 3 (ts - t0) / (t1 - t0) to the time weight.
  SELECT
    product_id,
    neighbour,
    COUNT(time_ts) + 3.0 * SUM(time_ts - t0) / GREATEST(MAX(t1) - MAX(t0), 1) AS time_w,
    SUM(type_w) AS type_w,
    SUM(buy2buy_w) AS buy2buy_w
  FROM per_session
  CROSS JOIN bounds
  GROUP BY product_id, neighbour
),
long AS (
  SELECT
    kind,
    product_id,
    neighbour,
    CASE kind WHEN 'time' THEN time_w WHEN 'type' THEN type_w ELSE buy2buy_w END AS weight
  FROM summed
  CROSS JOIN UNNEST(['time', 'type', 'buy2buy']) AS kind
)
SELECT
  @version AS version,
  kind,
  product_id,
  neighbour,
  weight,
  ROW_NUMBER() OVER (PARTITION BY kind, product_id ORDER BY weight DESC, neighbour) AS rank
FROM long
WHERE weight IS NOT NULL
QUALIFY rank <= 20
