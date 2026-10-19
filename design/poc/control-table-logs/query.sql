-- Log Analytics SQL over the control-table rows logged by log_rows.py.
-- Run in Logs Explorer > Log Analytics with FROM `PROJECT.global._Default._AllLogs`,
-- or through the linked BigQuery dataset with FROM `PROJECT.control_table_logs._AllLogs`.
-- Fields are the agent's role names, so nothing here is specific to one table.

-- 1. Today's picture: how many runs failed, where, and how widely
SELECT
  JSON_VALUE(json_payload.date)   AS business_date,
  JSON_VALUE(json_payload.source) AS source,
  JSON_VALUE(json_payload.system) AS system,
  COUNTIF(JSON_VALUE(json_payload.status) = 'FAILED')  AS failed,
  COUNTIF(JSON_VALUE(json_payload.status) = 'RUNNING') AS running,
  COUNT(*)                                             AS runs,
  COUNT(DISTINCT IF(JSON_VALUE(json_payload.status) = 'FAILED', JSON_VALUE(json_payload.unit), NULL)) AS books_hit,
  ANY_VALUE(IF(JSON_VALUE(json_payload.status) = 'FAILED', JSON_VALUE(json_payload.error), NULL))     AS example_error
FROM `__PROJECT__.control_table_logs._AllLogs`
WHERE log_name = 'projects/__PROJECT__/logs/control-table' AND JSON_VALUE(json_payload.kind) = 'control-table-row'
  AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY business_date, source, system
HAVING failed > 0
ORDER BY business_date DESC, failed DESC;
