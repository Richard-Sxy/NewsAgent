-- 2026-10-23 历史验收查询，可在匹配的历史库逐段运行；PostgreSQL 127.0.0.1:25432，
-- database=newsagent_unified_news_20261023，所有访问人数均为 synthetic 模拟聚合。
-- 本地新闻正文在同库 public Schema；企业对象存储正文接入尚未切换。
-- 2026-10-25 清理后，下面硬编码的旧内容 release 已归档，正文联查可能返回空。
-- 查询当前正文时，先在脚本副本中按 var/news-storage/current-selection.json 替换内容 release，
-- 并选择当前保留且具备受众绑定的 news_id；历史受众来源、snapshot_id 和小时窗口保持原义。
-- 当前正文为空不代表历史受众人数为0；本脚本不是实时分钟窗口查询。
-- 来源 release 是 reader 包 manifest 原始字节SHA，不是企业生产快照。
-- 不需要模型或 ES；全文读取与用户兴趣统计是不同数据链路。
-- 源每个期限最多保留20个主题：未出现主题不等于0人；人数多标签重叠不能相加。

-- 1. 已冻结的来源与实际导入数量。
SELECT source_release_sha256, schema_version, row_counts,
       manifest->>'source_mode' AS source_mode,
       manifest->>'coverage_start' AS coverage_start,
       manifest->>'coverage_end' AS coverage_end, imported_at
FROM news_audience.package_release
WHERE source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5';

-- 2. 先校验本次精确窗口：北京时间2026-09-24 22:00–23:00。
-- 应有1行、complete/passed，水位过window_end；quality_basis为本地导入合同检查。
-- knowledge_as_of是合成包冻结时点，不能声称复原了企业当时事件到达时间。
SELECT tenant_id, snapshot_id, window_start, window_end, profile_cutoff,
       source_mode, source_revision, completeness, quality_status, quality_basis,
       generated_at, watermark_at, knowledge_as_of, knowledge_as_of_basis
FROM news_audience.reader_snapshot
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND window_start = '2026-09-24 22:00:00+08'::timestamptz
  AND window_end = '2026-09-24 23:00:00+08'::timestamptz;

-- 3. 历史正文 + 长期兴趣人群概况。原验收为6位模拟详情读者，不是企业访问量或热度。
-- binding是明确的业务租户到内容域映射，不把public-news-local等同于业务UUID。
SELECT p.news_id, c.title, c.body, c.content_state, p.window_start, p.window_end,
       p.reader_count, p.profiled_reader_count, p.unknown_reader_count,
       p.cold_start_reader_count, p.profile_coverage,
       p.source_topic_count, p.source_topic_limit, p.topic_list_may_be_truncated,
       p.source_mode, p.limitations
FROM news_audience.v_news_reader_population_status p
JOIN news_audience.unified_news_binding b
  ON b.tenant_id=p.tenant_id AND b.source_release_sha256=p.source_release_sha256 AND b.news_id=p.news_id
JOIN public.news_content_selected c
  ON c.tenant_id=b.content_tenant_id AND c.release_id=b.content_release_id
 AND c.news_id=b.news_id AND c.record_sha256=b.record_sha256 AND c.body_sha256=b.body_sha256
WHERE p.tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND p.source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND p.snapshot_id = '90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d'
  AND p.news_id = '20260718A03B9V00' AND p.lookback_hours = 720
  AND b.content_tenant_id = 'public-news-local'
  AND b.content_release_id = 'news-0c74530f2864db20a73d7a96';

-- 4. 同一新闻主要读者标签。只取已保留的一级主题，最多5个；需要短期改lookback=24。
-- share_all是标签人数/全部读者，share_profiled是标签人数/有画像读者。
-- ROUND仅用于界面百分比展示；Python接口的八位半偶舍入仍遵守原Reader契约。
SELECT news_id, topic_id, topic_name, topic_reader_count, reader_count,
       profiled_reader_count, round(100 * share_all, 2) AS share_all_pct,
       round(100 * share_profiled, 2) AS share_profiled_pct,
       baseline_topic_count, baseline_profiled_count, baseline_share,
       lift, low_sample, source_topic_limit, topic_list_may_be_truncated, source_mode
FROM news_audience.v_news_reader_topic_metrics
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND snapshot_id = '90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d'
  AND news_id = '20260718A03B9V00' AND lookback_hours = 720 AND topic_level = 1
ORDER BY topic_reader_count DESC, topic_id
LIMIT 5;

-- 5. 任意新闻查询必须区分“未知”和“确实没有访客”。
-- 只有完整覆盖窗口+源内容绑定都存在，缺人口行才推导empty/0。
-- 画像源中有绑定不等于统一正文库中有相同body；两类资格各自校验。
WITH request AS (
    SELECT '11111111-1111-4111-8111-111111111111'::uuid AS tenant_id,
           '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'::text AS release,
           '2026-09-24 22:00:00+08'::timestamptz AS start_at,
           '2026-09-24 23:00:00+08'::timestamptz AS end_at
), requested_news(news_id) AS (
    VALUES ('20260718A03B9V00'::text), ('not-in-source'::text)
), joined AS (
    SELECT n.news_id, p.status, p.reader_count, b.news_id AS bound_news_id,
           (s.snapshot_id IS NOT NULL AND s.completeness IN ('complete','corrected')
            AND s.quality_status='passed' AND s.watermark_at>=s.window_end
            AND (s.expires_at IS NULL OR s.expires_at>CURRENT_TIMESTAMP)) AS window_ready
    FROM request r CROSS JOIN requested_news n
    LEFT JOIN news_audience.reader_snapshot s
      ON s.tenant_id=r.tenant_id AND s.source_release_sha256=r.release
     AND s.window_start=r.start_at AND s.window_end=r.end_at
    LEFT JOIN news_audience.content_binding b
      ON b.tenant_id=r.tenant_id AND b.source_release_sha256=r.release AND b.news_id=n.news_id
    LEFT JOIN news_audience.news_reader_population p
      ON p.tenant_id=s.tenant_id AND p.snapshot_id=s.snapshot_id
     AND p.news_id=n.news_id AND p.lookback_hours=720
)
SELECT news_id,
       CASE WHEN NOT window_ready OR bound_news_id IS NULL THEN 'unavailable'
            WHEN status IS NULL THEN 'empty' ELSE status END AS reader_status,
       CASE WHEN window_ready AND bound_news_id IS NOT NULL THEN COALESCE(reader_count,0)
            ELSE NULL END AS reader_count
FROM joined ORDER BY news_id;

-- 导入入口：agent/tools/import_reader_audience.py，显式AUDIENCE_DATABASE_URL，不读.env。
-- 来源聚合包 -> SHA/合同校验 -> ReaderWarehouseSource -> 单事务COPY/全量摘要读回
-- -> news_audience表/视图 -> 本文件SQL或CLI query。依赖psycopg、PostgreSQL、SQLite。
-- 2026-10-23 受众导入当时未改原news_content_*表、未切换线上Worker配置。
-- 后续内容清理和独立streaming lab接线另有版本记录；本查询不导入个人事件或逐用户画像。
