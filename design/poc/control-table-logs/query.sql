-- Log Analytics SQL over the control-table rows logged by log_rows.py.
-- Run in Logs Explorer > Log Analytics with FROM `PROJECT.global._Default._AllLogs`,
-- or through the linked BigQuery dataset with FROM `PROJECT.control_table_logs._AllLogs`.
-- Fields are the agent's role names, so nothing here is specific to one table.

-- 1. Today's picture: how many runs failed, where, and how widely
SELECT
  COALESCE(JSON_VALUE(json_payload.date), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.date'))   AS business_date,
  COALESCE(JSON_VALUE(json_payload.source), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.source')) AS source,
  COALESCE(JSON_VALUE(json_payload.system), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.system')) AS system,
  COUNTIF(COALESCE(JSON_VALUE(json_payload.status), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.status')) = 'FAILED')  AS failed,
  COUNTIF(COALESCE(JSON_VALUE(json_payload.status), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.status')) = 'RUNNING') AS running,
  COUNT(*)                                             AS runs,
  COUNT(DISTINCT IF(COALESCE(JSON_VALUE(json_payload.status), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.status')) = 'FAILED', COALESCE(JSON_VALUE(json_payload.unit), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.unit')), NULL)) AS books_hit,
  ANY_VALUE(IF(COALESCE(JSON_VALUE(json_payload.status), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.status')) = 'FAILED', COALESCE(JSON_VALUE(json_payload.error), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.error')), NULL))     AS example_error
FROM `__PROJECT__.control_table_logs._AllLogs`
WHERE log_name IN ('projects/__PROJECT__/logs/control-table', 'projects/__PROJECT__/logs/dataflow.googleapis.com%2Fworker') AND COALESCE(COALESCE(JSON_VALUE(json_payload.kind), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.kind')), JSON_VALUE(PARSE_JSON(JSON_VALUE(json_payload.message)), '$.kind')) = 'control-table-row'
  AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY business_date, source, system
HAVING failed > 0
ORDER BY business_date DESC, failed DESC;
