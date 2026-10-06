-- Events per day in [@start_ts, @end_ts), and the last one each day: what the nightly
-- job's first gate compares.
SELECT
  TIMESTAMP_TRUNC(event_time, DAY) AS d,
  COUNT(*) AS n,
  MAX(event_time) AS last
FROM recs.events
WHERE event_time >= @start_ts AND event_time < @end_ts AND session IS NOT NULL
GROUP BY d
ORDER BY d
