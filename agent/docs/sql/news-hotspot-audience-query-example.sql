-- 新闻热点 -> 实际读者兴趣 -> 热点 Agent：参数化查询示例，2026-10-23。
-- 状态：设计/教学 SQL，不是已上线的查询接口；本文件没有执行任何建表或导入。
-- dw.news_behavior_aggregate 是已有 v7 对象；news_audience_proposal_v1.*
-- 来自同目录 news-reader-audience-proposal-v1.sql，是未由应用自动装配的设计对象。
-- 它不同于已实现的本地 news_audience 历史投影及 news_realtime 实时模拟投影。
-- :name 是 SQLAlchemy named bind，不是 psql 直接可执行的变量语法。
-- tenant_id 来自认证上下文；news_ids 是服务端权威榜单返回的有界 text[]，每批最多20篇。
-- table/schema 名称固定；不接受模型任意指定表名、任意 SQL 或个人身份列。

-- 0. 接入设计要求企业提供的两类聚合作业（尚待合同映射与联调；实体见 DDL 顶部注释）：
-- A. 事件去重/清洗 -> 新闻完整小时指标 -> dw.news_behavior_aggregate。
-- B. 同源详情访问去重集合 + 截止窗口开始前的个人兴趣原始分数
--    -> 排除目标文章全部历史贡献 -> 重新归一化 -> 新闻读者概况/主题人数。
-- 企业内 actor_key 不出数仓。旧 synthetic_profiles 的每日快照不能直接替代 B。
-- 实时是持续更新与水位推进；首期仍使用完整单小时，不把当前未结束窗口算成完整榜。

-- 1. 已有 SQL 实体：读取点击 Top100 候选所需的指标。
-- 参数 window_end 必须等于 window_start + 1 hour，且窗口整点、有时区。
-- 注意：这里的 ORDER BY clicks 是候选策略，绝不是最终“热度排名”。
-- 候选数量和策略需写入运行记录；对候选的热度排名不等于全站所有文章的热度排名。
-- 当前 v7 视图没有修订/水位字段，企业接入必须另取指标快照元数据；不能只靠这段 SQL
-- 证明数据完整或与画像同源。现有代码链路使用受控模板、AST 检查和指标合同校验。
SELECT news_id, event_time, title, content_type, category, source, publish_time,
       impressions, clicks, unique_users, total_duration_seconds,
       effective_consumptions, interactions,
       baseline_impressions, baseline_clicks,
       baseline_effective_consumptions, baseline_interactions
FROM dw.news_behavior_aggregate
WHERE tenant_id = :tenant_id
  AND event_time >= :window_start AND event_time < :window_end
ORDER BY clicks DESC, news_id ASC
LIMIT :candidate_limit;

-- 2. Python HotScoreCalculator / HotNewsRanker 对有效候选计算权威热度、排序。
-- 不另写未经批准的 SQL 加权分来替代它。输出 news_ids + score + rule version
-- + metric_snapshot_ref；本轮运行再冻结 reader_snapshot_id 和内容版本绑定。
-- 如需要“直接 SELECT 最终榜单”，建议增设 hot_news_rank_snapshot：
-- PK(tenant_id, run_id, news_id)，UNIQUE(tenant_id, run_id, rank_no)，字段：
-- window_start/end, rank_no, hot_score, score_components, score_policy_version,
-- metric_snapshot_ref, reader_snapshot_id, candidate_policy_version,
-- content_binding_release_id, computed_at, source_mode。
-- 此榜单对象为待实现投影，不在本次 DDL 中创建，也不冒充已有实体。

-- 3. 读取“确切版本”的受众快照并由应用作门禁，不隐式取 latest。
-- 新运行：先由快照解析器选择目标窗口最新修订，再判断质量；最新不合格应 unavailable，
-- 不能先过滤 passed 后回退旧版。历史回放则固定原运行已记录的快照。
SELECT *
FROM news_audience_proposal_v1.reader_snapshot
WHERE tenant_id = :tenant_id AND snapshot_id = :reader_snapshot_id;
-- 应恰好1行；验证窗口相同、profile_cutoff=window_start、已批准的各类版本、
-- source_mode、source_release、watermark>=window_end、completeness 为 complete/corrected、
-- quality_status=passed、expires_at 尚未过期。历史回放另按原 knowledge_as_of 判断可见性。
-- 指标和画像独立作业的 snapshot_id 不必相同，但必须绑定到同一运行并验证源、窗口、
-- 身份口径兼容；同一事务只能固定数据库可见性，不能自动证明两个作业业务口径一致。

-- 4. 先查主要人群分母，再查主题。每次只选择一种期限，避免 24h/720h 连接扩行。
-- news_ids 是已通过步骤2的新闻ID数组，lookback_hours 只能是24或720。
-- 保留没有画像行的新闻，明确 unavailable；不要 COALESCE(reader_count,0)。
WITH requested AS (
    SELECT DISTINCT unnest(CAST(:news_ids AS text[])) AS news_id
)
SELECT n.news_id, p.lookback_hours, p.content_sha256,
       CASE WHEN p.news_id IS NULL THEN 'unavailable' ELSE p.status END AS profile_status,
       p.reader_count, p.profiled_reader_count, p.unknown_reader_count,
       p.cold_start_reader_count, p.baseline_profiled_count,
       p.profiled_reader_count::numeric / NULLIF(p.reader_count, 0) AS profile_coverage
FROM requested AS n
LEFT JOIN news_audience_proposal_v1.news_reader_population AS p
  ON p.tenant_id = :tenant_id AND p.snapshot_id = :reader_snapshot_id
 AND p.news_id = n.news_id AND p.lookback_hours = :lookback_hours
ORDER BY n.news_id;

-- 5. 每篇新闻的主要读者兴趣：一级主题 Top5。
-- share_all=主题人数/全部详情访客；share_profiled=主题人数/有画像访客。
-- 多标签重叠，占比不要求相加为1。默认一级主题，避免把父子主题当独立互斥人群。
-- 全量统计完成后才 Top K；SQL 比率保持原精度，映射到现有 ReaderProfileBatch 时
-- 必须使用 Python ratio_decimal 的半偶舍入与既有一致性校验，不直接套数据库 ROUND。
WITH ranked_topics AS (
    SELECT t.*,
           row_number() OVER (
               PARTITION BY t.tenant_id, t.snapshot_id, t.news_id, t.lookback_hours
               ORDER BY t.topic_reader_count DESC, t.topic_id ASC
           ) AS topic_rank
    FROM news_audience_proposal_v1.v_news_reader_topic_metrics AS t
    WHERE t.tenant_id = :tenant_id AND t.snapshot_id = :reader_snapshot_id
      AND t.news_id = ANY(CAST(:news_ids AS text[]))
      AND t.lookback_hours = :lookback_hours AND t.topic_level = 1
)
SELECT news_id, topic_id, topic_name, topic_reader_count,
       reader_count, profiled_reader_count, share_all, share_profiled,
       baseline_profiled_count, baseline_topic_count, baseline_share, lift, low_sample
FROM ranked_topics
WHERE topic_rank <= :topic_top_k
ORDER BY news_id, topic_rank;

-- 6. 按业务域 -> 公开内容域的显式绑定取正文，不把两个 tenant_id 直接等同。
-- 企业版本建议独立映射实体 business_news_content_binding（待实现）：
-- 本地已有 news_audience.unified_news_binding；不要把此建议理解为本地尚无正文绑定。
-- PK(business_tenant_id, binding_release_id, news_id)，字段 content_tenant_id,
-- content_release_id, content_record_sha256, body_sha256, tag_release_id, bound_at。
-- 绑定只有在新闻ID、正文哈希/来源权限校验通过后生成，不能把所有新新闻都补上行为。
-- 此条在统一新闻数据库 newsagent_unified_news_20261023 中执行，不能跨数据库直接 JOIN：
SELECT news_id, title, body, content_state, body_sha256, topic_ids, tag_status
FROM news_content_tagged_selected
WHERE tenant_id = :content_tenant_id
  AND content_release_id = :content_release_id AND tag_release_id = :tag_release_id
  AND news_id = ANY(CAST(:bound_news_ids AS text[]));

-- 7. 最终调用链：候选指标 -> HotNewsRanker -> 上述人群/主题查询
-- -> 新 SQL 聚合 Adapter（待实现，同 NewsReaderProfilePort）
-- -> ReaderProfileService -> HotNewsEnrichmentService
-- -> HotNewsAnalysisInputBuilder -> NativeHotNewsAnalysisRunner -> 报告校验与持久化。
-- 模型收到正文、热度及分量、主要标签人数/占比/基准/局限和快照引用；不收到 actor_key。
-- 现有 enterprise_http 聚合 Adapter 代码可复用；企业真实服务仍需联调验收。
-- 配置：沿用 READER_PROFILE_ENABLED 等开关；SQL backend/DSN/schema/权限尚待实现，
-- 不改现有冻结 news-warehouse-v7 或放宽 SQL AST 白名单。
-- 测试入口：本设计需 PostgreSQL DDL/查询合同验证；未来 Adapter 复用
-- agent/tests/test_reader_profile_*.py，另加缺失/快照错配/跨租户/跨期限/迟到修订场景。
-- TODO：企业表名与源口径映射、批准主题目录、物理分区/分布、聚合发布作业、跨行校验、
-- SQL Adapter、内容业务域映射、榜单投影、数据库/网关授权、在线端到端验收。
