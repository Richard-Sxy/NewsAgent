-- 新闻访问人群数仓对象：PostgreSQL 逻辑 DDL 草案 v1。
-- 本文件尚未部署、未导入数据；表名不是腾讯内部真实表名。
-- 仅使用 PostgreSQL 通用类型与约束，TDSQL-A 的实际语法、分布键、分区、
-- 权限与查询性能须在已确认的企业版本中另行验收。
-- 本脚本只定义聚合对象，不创建逐用户企业数据表，不含示例 INSERT。
-- CREATE SCHEMA 故意不使用 IF NOT EXISTS：同名对象存在时应报错，禁止覆盖。

-- 企业私域内应存在的四类逻辑实体（这里只描述，不执行 CREATE TABLE）：
-- 1. user_interest_snapshot：键为 tenant_id + actor_key + profile_cutoff +
--    lookback_hours + profile_policy_version + source_revision；字段包括
--    initialized_at、profile_available、history_start/end、knowledge_as_of、
--    watermark_at、scored_event_count。actor_key 是企业批准的域内身份键。
-- 2. user_interest_topic：上述画像键 + topic_id；字段包括 raw_signed_score、
--    normalized_weight、taxonomy_version。原始分数保留负反馈，不能提前截成正数。
-- 3. user_news_topic_contribution：上述画像键 + news_id + topic_id；字段包括
--    raw_signed_score、scored_event_count、content_sha256、decay_anchor_at。
--    使用相同衰减锚点减去目标新闻的全部历史贡献，再重算正分与归一化权重；
--    不能从 normalized_weight 直接减分，也不能用本篇内容标签替代读者兴趣。
-- 4. news_reader_window：键为 tenant_id + news_id + window_start/end +
--    snapshot_id + actor_key；字段包括 reader_definition、first_visit_at、
--    last_visit_at、detail_view_count。由详情访问事件在完整目标窗口内 DISTINCT
--    得到，跨小时、跨渠道 UV 不能直接求和。个人键、事件与上述贡献均留企业内。

-- 发布器必须执行的跨行校验（CHECK/FK 并不能替代）：
-- * 每个新闻同时发布 24h/720h 两行 population，reader_count 与 content_sha256
--   一致；两期限兴趣分母可以不同。lookback 指画像回看期限，不是访问窗口长度。
-- * topic_reader_count <= profiled_reader_count；baseline_topic_count <=
--   baseline_profiled_count；每行主题和分母使用同一目标新闻排除、身份和版本口径。
-- * 只有已绑定新闻且目标窗口完整覆盖时，无访客才可发布 empty/0；未统计、未知、
--   覆盖不足不能补零。无主题行也不能被解释为无人访问，须读取 population。
-- * 内容 SHA 与目标内容版本、主题目录及批准的企业字段映射一致；taxonomy_version
--   必须引用不可变目录。企业目录审批由发布器校验，不由字段名保证。
-- * 历史事件时间在 [profile_cutoff-lookback, profile_cutoff)，且事件/修订只使用
--   knowledge_as_of 时已可见的数据。profile_cutoff 防止窗口内未来特征倒灌；
--   knowledge_as_of 固定数据到达时间。迟到补录重算必须另发 source_revision。
-- * history_start 晚于回看起点时，披露历史截断，不能声称拥有完整 30d/24h 历史。
-- * 同租户、同窗、同发布系列的 revision 单调增加。先选最新修订再判断质量，
--   最新 incomplete/pending 时不得静默回退旧 passed；source_release 需可追溯。
-- * 同一次查询冻结 snapshot_id，发布时原子提交人口、主题与快照；发布后禁止原地
--   修改目录、内容绑定和计数。撤回/纠错另发版本，查询不能混用两个 revision。
-- * reader_population 的基准包含目标新闻读者中有画像的人；同样排除目标文章。
--   若企业采用不同基准总体，须另建口径/合同，不得继续使用这里的包含关系约束。
-- * min_support 是统计稳定性阈值，不代表隐私匿名化策略；企业须单独批准输出权限。
-- 本 DDL 不证明租户隔离、只读权限、事务快照一致性或上述发布冻结已经实现。
-- 第一版仅支持精确去重；近似去重、误差界与契约适配为 TODO，不得冒用精确标记。

BEGIN;

CREATE SCHEMA news_audience_proposal_v1;

CREATE TABLE news_audience_proposal_v1.reader_snapshot (
    tenant_id uuid NOT NULL,                         -- 企业批准的租户/业务域；不能由新闻正文提供。
    snapshot_id text NOT NULL,                       -- 本次不可变聚合快照 ID；查询须显式绑定。
    window_start timestamptz NOT NULL,               -- 新闻访问窗口左边界，包含。
    window_end timestamptz NOT NULL,                 -- 新闻访问窗口右边界，不包含；本合同最多 24h。
    profile_cutoff timestamptz NOT NULL,             -- 兴趣事件截止点，固定为访问窗口开始。
    knowledge_as_of timestamptz NOT NULL,            -- 本版只使用截至此时已到达/可见的源数据修订。
    history_start timestamptz NOT NULL,              -- 可用连续历史覆盖起点，非声称的固定 30d 起点。
    source_revision bigint NOT NULL,                -- 同一发布系列/窗口的单调修订号，纠错另发新版本。
    source_release_sha256 text NOT NULL,             -- 冻结来源发布身份 SHA-256，不是任意查询文本哈希。
    source_mode text NOT NULL,                       -- synthetic 本地合成，enterprise 企业真实聚合。
    taxonomy_version text NOT NULL,                  -- 不可变主题目录版本，与每条主题记录一致。
    profile_policy_version text NOT NULL,            -- 评分、衰减、阈值、回看和排除规则的批准版本。
    identity_policy_version text NOT NULL,           -- 域内身份去重规则版本，不能跨租户合并身份。
    reader_definition text NOT NULL,                 -- 读者定义；v1 为 detail-view-distinct-v1。
    count_method text NOT NULL,                      -- exact_synthetic_distinct / enterprise_exact_distinct。
    generated_at timestamptz NOT NULL,               -- 聚合结果实际生成时间，不是查询请求时间。
    watermark_at timestamptz NOT NULL,               -- 上游已按其合同处理推进到的事件时间边界。
    completeness text NOT NULL,                      -- incomplete / complete / corrected；须结合质量检查。
    quality_status text NOT NULL,                    -- pending / passed / failed，完整不等于质量通过。
    expires_at timestamptz,                          -- 企业在线快照失效时间；合成冻结包可为空。
    min_support bigint NOT NULL DEFAULT 5,           -- lift 四类人数最小支持阈值，版本化配置。
    CONSTRAINT reader_snapshot_pk PRIMARY KEY (tenant_id, snapshot_id),
    CONSTRAINT reader_snapshot_taxonomy_uk UNIQUE (tenant_id, snapshot_id, taxonomy_version),
    CONSTRAINT reader_snapshot_identifiers_ck CHECK (
        btrim(snapshot_id) <> '' AND btrim(taxonomy_version) <> ''
        AND btrim(profile_policy_version) <> '' AND btrim(identity_policy_version) <> ''
    ),
    CONSTRAINT reader_snapshot_window_ck CHECK (
        window_start < window_end AND window_end <= window_start + interval '24 hours'
        AND profile_cutoff = window_start AND history_start <= profile_cutoff
    ),
    CONSTRAINT reader_snapshot_time_ck CHECK (
        knowledge_as_of >= window_end AND generated_at >= knowledge_as_of
        AND watermark_at <= knowledge_as_of
        AND (expires_at IS NULL OR expires_at > generated_at)
    ),
    CONSTRAINT reader_snapshot_revision_ck CHECK (source_revision >= 1 AND min_support >= 5),
    CONSTRAINT reader_snapshot_release_ck CHECK (source_release_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT reader_snapshot_source_ck CHECK (
        (source_mode = 'synthetic' AND count_method = 'exact_synthetic_distinct')
        OR (source_mode = 'enterprise' AND count_method = 'enterprise_exact_distinct'
            AND expires_at IS NOT NULL)
    ),
    CONSTRAINT reader_snapshot_definition_ck CHECK (reader_definition = 'detail-view-distinct-v1'),
    CONSTRAINT reader_snapshot_state_ck CHECK (
        completeness IN ('incomplete', 'complete', 'corrected')
        AND quality_status IN ('pending', 'passed', 'failed')
        AND (completeness = 'incomplete' OR watermark_at >= window_end)
    )
);

CREATE TABLE news_audience_proposal_v1.topic_dictionary (
    taxonomy_version text NOT NULL,                  -- 目录发布版本；全局唯一命名，发布后不可原地改名。
    topic_id text NOT NULL,                          -- 稳定主题 ID，新闻内容和用户兴趣使用同一目录。
    topic_name text NOT NULL,                        -- 该版本展示名称，例如“财经”；名称不能当作查询指令。
    parent_topic_id text,                            -- 父主题 ID；根主题为空，父子关系必须同版本。
    topic_level smallint NOT NULL,                   -- 层级，根为 1；层级与无环关系由发布器跨行验证。
    CONSTRAINT topic_dictionary_pk PRIMARY KEY (taxonomy_version, topic_id),
    CONSTRAINT topic_dictionary_parent_fk FOREIGN KEY (taxonomy_version, parent_topic_id)
        REFERENCES news_audience_proposal_v1.topic_dictionary (taxonomy_version, topic_id),
    CONSTRAINT topic_dictionary_identifiers_ck CHECK (
        btrim(taxonomy_version) <> '' AND btrim(topic_id) <> '' AND btrim(topic_name) <> ''
        AND (parent_topic_id IS NULL OR (btrim(parent_topic_id) <> '' AND parent_topic_id <> topic_id))
    ),
    CONSTRAINT topic_dictionary_level_ck CHECK (
        (topic_level = 1 AND parent_topic_id IS NULL)
        OR (topic_level > 1 AND parent_topic_id IS NOT NULL)
    )
);

CREATE TABLE news_audience_proposal_v1.news_reader_population (
    tenant_id uuid NOT NULL,                         -- 与冻结快照绑定的租户。
    snapshot_id text NOT NULL,                       -- 同一快照的访问窗口、修订和水位来自 reader_snapshot。
    news_id text NOT NULL,                           -- 热点、正文和受众的统一新闻关联键。
    lookback_hours integer NOT NULL,                 -- 兴趣回看期：24 为短期、720 为长期。
    content_sha256 text NOT NULL,                    -- 实际绑定正文的规范化 SHA；不是凭 news_id 猜测版本。
    reader_count bigint NOT NULL,                   -- 整个访问窗口的去重详情访客，长短期应一致。
    profiled_reader_count bigint NOT NULL,           -- 排除目标新闻后，在此回看期仍有可用兴趣的访客数。
    unknown_reader_count bigint NOT NULL,            -- 未具备可用画像的访客，仍计入全体访客分母。
    cold_start_reader_count bigint NOT NULL,         -- 已初始化但无非目标新闻评分事件者；是 unknown 子集。
    baseline_profiled_count bigint NOT NULL,         -- 同租户/截止点/排除规则下基准总体的有画像人数。
    status text NOT NULL,                            -- complete 为有访客，empty 为已完整统计且确实零访客。
    trigger_article_excluded boolean NOT NULL,       -- 明确已排除目标新闻全部历史贡献，v1 必须为 true。
    limitations text[] NOT NULL DEFAULT ARRAY[]::text[], -- 历史截断、合成来源等限制，不替代结构化状态。
    CONSTRAINT news_reader_population_pk PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours),
    CONSTRAINT news_reader_population_snapshot_fk FOREIGN KEY (tenant_id, snapshot_id)
        REFERENCES news_audience_proposal_v1.reader_snapshot (tenant_id, snapshot_id),
    CONSTRAINT news_reader_population_binding_ck CHECK (
        btrim(news_id) <> '' AND lookback_hours IN (24, 720)
        AND content_sha256 ~ '^[0-9a-f]{64}$' AND trigger_article_excluded
    ),
    CONSTRAINT news_reader_population_counts_ck CHECK (
        reader_count >= 0 AND profiled_reader_count >= 0 AND profiled_reader_count <= reader_count
        AND unknown_reader_count = reader_count - profiled_reader_count
        AND cold_start_reader_count >= 0 AND cold_start_reader_count <= unknown_reader_count
        AND baseline_profiled_count >= profiled_reader_count
    ),
    CONSTRAINT news_reader_population_status_ck CHECK (
        (status = 'empty' AND reader_count = 0)
        OR (status = 'complete' AND reader_count > 0)
    )
);

CREATE TABLE news_audience_proposal_v1.news_reader_topic (
    tenant_id uuid NOT NULL,                         -- 与人口行及快照严格一致的租户。
    snapshot_id text NOT NULL,                       -- 同一不可变聚合发布快照。
    news_id text NOT NULL,                           -- 目标新闻；不得用主题级总人数冒充其访问人群。
    lookback_hours integer NOT NULL,                 -- 与人口行相同的 24/720 小时兴趣回看期。
    topic_id text NOT NULL,                          -- 读者的兴趣主题，非新闻正文分类的直接复制。
    topic_reader_count bigint NOT NULL,              -- 目标新闻读者中具备该兴趣的去重人数。
    baseline_topic_count bigint NOT NULL,            -- 基准画像总体中具备该兴趣的人数。
    taxonomy_version text NOT NULL,                  -- 同时绑定快照目录版本和目录实体。
    CONSTRAINT news_reader_topic_pk PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours, topic_id),
    CONSTRAINT news_reader_topic_population_fk FOREIGN KEY (tenant_id, snapshot_id, news_id, lookback_hours)
        REFERENCES news_audience_proposal_v1.news_reader_population (tenant_id, snapshot_id, news_id, lookback_hours),
    CONSTRAINT news_reader_topic_snapshot_fk FOREIGN KEY (tenant_id, snapshot_id, taxonomy_version)
        REFERENCES news_audience_proposal_v1.reader_snapshot (tenant_id, snapshot_id, taxonomy_version),
    CONSTRAINT news_reader_topic_dictionary_fk FOREIGN KEY (taxonomy_version, topic_id)
        REFERENCES news_audience_proposal_v1.topic_dictionary (taxonomy_version, topic_id),
    CONSTRAINT news_reader_topic_counts_ck CHECK (
        topic_reader_count >= 0 AND baseline_topic_count >= topic_reader_count
    )
);

-- 此视图只计算指标，不隐式过滤最新失败修订或过期快照。
-- 消费者先绑定 snapshot，再判断 completeness / quality / watermark / expiry。
-- 视图每行是“新闻 × 画像期限 × 主题”；不得 SUM 其重复出现的人口分母。
-- 无 topic 行的新闻不会出现在本视图中，必须另查 population 区分未知与真实空人群。
-- 比例采用 PostgreSQL numeric 的运算精度，零分母输出 NULL；不按接口八位半偶舍入。
-- 转换到现有 ReaderProfileSummary 时须显式完成并校验接口约定的舍入与空状态映射。
CREATE VIEW news_audience_proposal_v1.v_news_reader_topic_metrics AS
WITH rates AS (
    SELECT
        t.tenant_id, t.snapshot_id, t.news_id, t.lookback_hours, t.topic_id,
        t.taxonomy_version, d.topic_name, d.parent_topic_id, d.topic_level,
        s.window_start, s.window_end, s.profile_cutoff, s.knowledge_as_of, s.history_start,
        s.source_revision, s.source_release_sha256, s.source_mode,
        s.profile_policy_version, s.identity_policy_version, s.reader_definition, s.count_method,
        s.generated_at, s.watermark_at, s.completeness, s.quality_status, s.expires_at, s.min_support,
        p.content_sha256, p.status, p.trigger_article_excluded, p.limitations,
        p.reader_count, p.profiled_reader_count, p.unknown_reader_count,
        p.cold_start_reader_count, p.baseline_profiled_count,
        t.topic_reader_count, t.baseline_topic_count,
        t.topic_reader_count::numeric / NULLIF(p.reader_count, 0) AS share_all,
        t.topic_reader_count::numeric / NULLIF(p.profiled_reader_count, 0) AS share_profiled,
        t.baseline_topic_count::numeric / NULLIF(p.baseline_profiled_count, 0) AS baseline_share,
        LEAST(p.profiled_reader_count, p.baseline_profiled_count,
              t.topic_reader_count, t.baseline_topic_count) < s.min_support AS low_sample
    FROM news_audience_proposal_v1.news_reader_topic AS t
    JOIN news_audience_proposal_v1.news_reader_population AS p
      ON (p.tenant_id, p.snapshot_id, p.news_id, p.lookback_hours)
       = (t.tenant_id, t.snapshot_id, t.news_id, t.lookback_hours)
    JOIN news_audience_proposal_v1.reader_snapshot AS s
      ON (s.tenant_id, s.snapshot_id, s.taxonomy_version)
       = (t.tenant_id, t.snapshot_id, t.taxonomy_version)
    JOIN news_audience_proposal_v1.topic_dictionary AS d
      ON (d.taxonomy_version, d.topic_id) = (t.taxonomy_version, t.topic_id)
)
SELECT rates.*,
       CASE WHEN low_sample THEN NULL::numeric
            ELSE share_profiled / NULLIF(baseline_share, 0) END AS lift
FROM rates;

COMMENT ON SCHEMA news_audience_proposal_v1 IS
    '未部署的新闻访问人群聚合逻辑方案；禁止将本对象存在等同于企业实时数仓接入验收。';
COMMENT ON TABLE news_audience_proposal_v1.reader_snapshot IS
    '聚合发布头；事件截止与数据可见截止分开，最新修订选择及发布冻结由发布器实现。';
COMMENT ON TABLE news_audience_proposal_v1.topic_dictionary IS
    '不可变版本的主题目录；企业审批、父子无环和冻结由发布器验证。';
COMMENT ON TABLE news_audience_proposal_v1.news_reader_population IS
    '新闻实际访问人群分母，按长期/短期兴趣分别计算覆盖；缺失不能补为零。';
COMMENT ON TABLE news_audience_proposal_v1.news_reader_topic IS
    '多标签兴趣分子，可同一读者命中多个主题，各主题占比无需相加为100%。';
COMMENT ON VIEW news_audience_proposal_v1.v_news_reader_topic_metrics IS
    '保留状态和版本的主题比例视图；不负责权限或质量门禁，不返回个人身份。';

COMMIT;
