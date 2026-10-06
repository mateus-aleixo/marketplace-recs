-- Per product, from every event before @end_ts: features.product_stats in SQL.
--
--   activity  events over the last day and the last 7 days, carts and purchases over 7
--   latest    price, category and brand of its most recent event, as the catalogue
--             stood at the end of the night
WITH events AS (
  SELECT event_time, event_type, product_id, category_id, brand, price, seq
  FROM recs.events
  WHERE event_time < @end_ts AND session IS NOT NULL
),
latest AS (
  SELECT product_id, price AS p_price, category_id AS p_category, brand AS p_brand
  FROM events
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY event_time DESC, seq DESC) = 1
),
activity AS (
  SELECT
    product_id,
    COUNTIF(event_time >= TIMESTAMP_SUB(@end_ts, INTERVAL 1 DAY)) AS p_events_1d,
    COUNT(*) AS p_events_7d,
    COUNTIF(event_type = 1) AS p_carts_7d,
    COUNTIF(event_type = 2) AS p_purchases_7d
  FROM events
  WHERE event_time >= TIMESTAMP_SUB(@end_ts, INTERVAL 7 DAY)
  GROUP BY product_id
)
SELECT
  @version AS version,
  product_id,
  p_events_1d,
  p_events_7d,
  p_carts_7d,
  p_purchases_7d,
  p_price,
  p_category,
  p_brand
FROM latest
LEFT JOIN activity USING (product_id)
