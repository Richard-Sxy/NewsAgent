-- 本地 dw 查询库的结构、数量与质量核对；最初用于v1/v2，也适用于沿用这些对象的profile。
-- 请先核对 news_schema_contract 与所选profile；这不是独立 news_sim_v7/news_sim_v8 的专用验收。
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

-- 以下仅统计缺失基线；real-news-v8 初期无历史样本时允许缺基线，不应一概判为错误。
SELECT COUNT(*) AS metric_rows_without_baseline
FROM dw.news_metric_hourly AS m
LEFT JOIN dw.news_metric_baseline_hourly AS b
  ON b.tenant_id = m.tenant_id
 AND b.news_id = m.news_id
 AND b.event_time = m.event_time
WHERE b.news_id IS NULL;

ROLLBACK;
