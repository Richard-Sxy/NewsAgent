-- 本地合成数仓核对；v1/v2 通用。
-- 多条受信任 SELECT 供运维执行，不是模型允许生成的查询。
-- 不创建/修改表，不改数据；只输出规模、约束和汇总质量计数。
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT version, schema_sha256
FROM dw.news_schema_contract
ORDER BY version;

SELECT
    (SELECT COUNT(*) FROM dw.dim_news) AS news_rows,
    (SELECT COUNT(*) FROM dw.news_metric_hourly) AS metric_rows,
    (SELECT COUNT(*) FROM dw.news_metric_baseline_hourly) AS baseline_rows,
    (SELECT COUNT(*) FROM dw.news_behavior_aggregate) AS view_rows;

SELECT COUNT(DISTINCT tenant_id) AS tenants,
       MIN(event_time) AS first_bucket,
       MAX(event_time) AS last_bucket
FROM dw.news_metric_hourly;

SELECT t.relname AS table_name,
       c.conname AS constraint_name,
       pg_get_constraintdef(c.oid) AS constraint_sql
FROM pg_constraint AS c
JOIN pg_class AS t ON t.oid = c.conrelid
JOIN pg_namespace AS n ON n.oid = t.relnamespace
WHERE n.nspname = 'dw'
  AND t.relname IN ('dim_news', 'news_metric_hourly', 'news_metric_baseline_hourly')
ORDER BY t.relname, c.conname;

SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'dw'
  AND tablename IN ('dim_news', 'news_metric_hourly', 'news_metric_baseline_hourly')
ORDER BY tablename, indexname;

SELECT
    COUNT(*) FILTER (WHERE impressions < 0 OR clicks < 0
        OR clicks > impressions OR unique_users < 0 OR unique_users > clicks
        OR total_duration_seconds < 0 OR effective_consumptions < 0
        OR effective_consumptions > clicks OR interactions < 0) AS invalid_metric_rows,
    COUNT(*) FILTER (WHERE
        date_trunc('hour', event_time AT TIME ZONE 'UTC')
        <> event_time AT TIME ZONE 'UTC') AS non_hour_aligned_rows
FROM dw.news_metric_hourly;

SELECT COUNT(*) AS metric_rows_without_baseline
FROM dw.news_metric_hourly AS m
LEFT JOIN dw.news_metric_baseline_hourly AS b
  ON b.tenant_id = m.tenant_id
 AND b.news_id = m.news_id
 AND b.event_time = m.event_time
WHERE b.news_id IS NULL;

ROLLBACK;
