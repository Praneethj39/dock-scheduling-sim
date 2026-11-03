-- KPI queries for the yard. analyze.py runs each named block below.
-- Table truck_visits has one row per truck per policy.

-- name: policy_summary
-- Headline numbers for each scheduling policy.
SELECT
    policy,
    COUNT(*)                                              AS trucks,
    ROUND(AVG(yard_wait_min), 1)                          AS avg_yard_wait_min,
    ROUND(AVG(delay_vs_request_min), 1)                   AS avg_delay_vs_request_min,
    ROUND(100.0 * AVG(yard_wait_min > 60), 1)             AS pct_wait_over_60min,
    ROUND(AVG(turnaround_min), 1)                         AS avg_turnaround_min,
    ROUND(SUM(detention_rs) / COUNT(DISTINCT visit_date)) AS detention_rs_per_day
FROM truck_visits
GROUP BY policy
ORDER BY avg_yard_wait_min DESC;

-- name: p90_wait
-- 90th-percentile yard wait: 9 out of 10 trucks wait less than this.
-- SQLite has no PERCENTILE function, so we rank rows with a window function.
WITH ranked AS (
    SELECT policy, yard_wait_min,
           ROW_NUMBER() OVER (PARTITION BY policy ORDER BY yard_wait_min) AS rn,
           COUNT(*)     OVER (PARTITION BY policy)                        AS n
    FROM truck_visits
)
SELECT policy, ROUND(MIN(yard_wait_min), 1) AS p90_yard_wait_min
FROM ranked
WHERE rn >= 0.9 * n
GROUP BY policy;

-- name: by_category
-- Who wins and who loses under each policy?
-- "at risk" = a chilled truck waiting more than 45 min before unloading.
SELECT
    policy,
    category,
    COUNT(*)                                     AS trucks,
    ROUND(AVG(yard_wait_min), 1)                 AS avg_yard_wait_min,
    ROUND(100.0 * AVG(yard_wait_min > 45), 1)    AS pct_wait_over_45min
FROM truck_visits
GROUP BY policy, category
ORDER BY policy, category;

-- name: by_hour
-- Average wait by the hour the truck reached the gate (shows the morning peak).
SELECT policy, arrival_hour,
       COUNT(*)                     AS trucks,
       ROUND(AVG(yard_wait_min), 1) AS avg_yard_wait_min
FROM truck_visits
GROUP BY policy, arrival_hour
ORDER BY policy, arrival_hour;

-- name: dock_utilisation
-- Share of the 6 am - 10 pm working window each dock spent unloading.
SELECT policy, dock_no,
       ROUND(100.0 * SUM(unload_min) / (COUNT(DISTINCT visit_date) * 16 * 60), 1) AS utilisation_pct
FROM truck_visits
GROUP BY policy, dock_no
ORDER BY policy, dock_no;

-- name: worst_vendors
-- Under first-come-first-served, which suppliers' trucks wait the longest?
-- (Useful for deciding whom to put on appointment slots first.)
SELECT vendor_id,
       COUNT(*)                     AS trucks,
       ROUND(AVG(yard_wait_min), 1) AS avg_yard_wait_min,
       ROUND(AVG(arrival_hour), 1)  AS avg_arrival_hour
FROM truck_visits
WHERE policy = 'fcfs'
GROUP BY vendor_id
HAVING COUNT(*) >= 20
ORDER BY avg_yard_wait_min DESC
LIMIT 5;

-- name: slot_push
-- Slots policy only: how far the booking moved trucks from the time they asked for.
SELECT
    ROUND(AVG((julianday(slot_start) - julianday(requested_arrival)) * 1440), 1) AS avg_push_min,
    ROUND(100.0 * AVG((julianday(slot_start) - julianday(requested_arrival)) * 1440 > 60), 1)
                                                                                  AS pct_pushed_over_60min
FROM truck_visits
WHERE policy = 'slots';
