-- Real cached articles, synthetic metrics/lifecycle; an independent immutable v8 package.
-- CREATE SCHEMA intentionally fails on an existing schema. Never replace a previous dataset.
CREATE SCHEMA news_sim_v8;

CREATE TABLE news_sim_v8.dim_behavior_policy (
    tenant_id uuid NOT NULL CHECK (tenant_id IN ('11111111-1111-4111-8111-111111111111','33333333-3333-4333-8333-333333333333')),
    policy_version text NOT NULL, click_definition text NOT NULL, uv_definition text NOT NULL,
    pv_definition text NOT NULL, effective_definition text NOT NULL,
    metric_data_synthetic boolean NOT NULL CHECK (metric_data_synthetic),
    PRIMARY KEY (tenant_id, policy_version)
);
CREATE TABLE news_sim_v8.dim_channel (
    tenant_id uuid NOT NULL, channel_id text NOT NULL, channel_name text NOT NULL,
    platform text NOT NULL CHECK (platform IN ('app','web')),
    dimension_data_synthetic boolean NOT NULL CHECK (dimension_data_synthetic),
    PRIMARY KEY (tenant_id, channel_id)
);
CREATE TABLE news_sim_v8.dim_news (
    tenant_id uuid NOT NULL CHECK (tenant_id IN ('11111111-1111-4111-8111-111111111111','33333333-3333-4333-8333-333333333333')),
    news_id text NOT NULL, title text NOT NULL CHECK (length(title) > 0),
    content text NOT NULL CHECK (length(content) > 0), source_url text NOT NULL CHECK (source_url LIKE 'https://%'),
    content_type text NOT NULL CHECK (content_type = 'article'), category text NOT NULL,
    category_data_synthetic boolean NOT NULL CHECK (category_data_synthetic), source text NOT NULL,
    publish_time timestamptz NOT NULL, publish_time_raw text NOT NULL,
    source_content_version integer NOT NULL CHECK (source_content_version > 0),
    content_version integer NOT NULL CHECK (content_version = source_content_version),
    fixture_content_version integer NOT NULL CHECK (fixture_content_version >= content_version),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    source_file text NOT NULL, source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
    source_package text NOT NULL,
    source_manifest_sha256 text CHECK (source_manifest_sha256 ~ '^[a-f0-9]{64}$'),
    source_html_sha256 text CHECK (source_html_sha256 ~ '^[a-f0-9]{64}$'),
    news_content_synthetic boolean NOT NULL CHECK (NOT news_content_synthetic),
    metric_data_synthetic boolean NOT NULL CHECK (metric_data_synthetic),
    metric_available boolean NOT NULL,
    lifecycle_data_synthetic boolean NOT NULL CHECK (lifecycle_data_synthetic),
    lifecycle_scenario text NOT NULL CHECK (lifecycle_scenario IN ('none','withdraw','restrict','correct')),
    status text NOT NULL CHECK (status IN ('published','withdrawn','restricted')),
    lifecycle_revision integer NOT NULL CHECK (lifecycle_revision > 0), status_effective_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, news_id),
    CHECK (tenant_id <> '11111111-1111-4111-8111-111111111111' OR
           (status = 'published' AND lifecycle_scenario = 'none' AND fixture_content_version = content_version)),
    CHECK (status_effective_at >= publish_time)
);
CREATE TABLE news_sim_v8.news_content_feature (
    tenant_id uuid NOT NULL, news_id text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    metric_available boolean NOT NULL,
    title_chars integer NOT NULL CHECK (title_chars > 0),
    content_chars integer NOT NULL CHECK (content_chars > 0),
    paragraph_count integer NOT NULL CHECK (paragraph_count >= 0),
    sentence_count integer NOT NULL CHECK (sentence_count >= 0),
    digit_token_count integer NOT NULL CHECK (digit_token_count >= 0),
    keyword_top jsonb NOT NULL CHECK (jsonb_typeof(keyword_top) = 'array'),
    PRIMARY KEY (tenant_id, news_id),
    FOREIGN KEY (tenant_id, news_id) REFERENCES news_sim_v8.dim_news(tenant_id, news_id)
);
CREATE TABLE news_sim_v8.window_snapshot (
    tenant_id uuid NOT NULL, event_time timestamptz NOT NULL,
    snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0), window_end timestamptz NOT NULL,
    watermark_at timestamptz NOT NULL, snapshot_at timestamptz NOT NULL,
    completeness text NOT NULL CHECK (completeness IN ('complete','corrected','incomplete')),
    quality_status text NOT NULL CHECK (quality_status IN ('passed','pending')),
    policy_version text NOT NULL, metric_data_synthetic boolean NOT NULL CHECK (metric_data_synthetic),
    PRIMARY KEY (tenant_id, event_time, snapshot_revision),
    FOREIGN KEY (tenant_id, policy_version) REFERENCES news_sim_v8.dim_behavior_policy(tenant_id, policy_version),
    CHECK (event_time = date_trunc('hour',event_time)),
    CHECK (window_end = event_time + interval '1 hour'), CHECK (snapshot_at >= watermark_at),
    CHECK ((completeness IN ('complete','corrected') AND quality_status = 'passed' AND watermark_at >= window_end)
        OR (completeness = 'incomplete' AND quality_status = 'pending' AND watermark_at < window_end))
);
CREATE TABLE news_sim_v8.news_metric_5min (
    tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
    snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0),

    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    detail_page_views bigint NOT NULL CHECK (detail_page_views >= 0),
    direct_page_views bigint NOT NULL CHECK (direct_page_views >= 0 AND direct_page_views <= detail_page_views),
    total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
    effective_consumptions bigint NOT NULL CHECK (effective_consumptions >= 0 AND effective_consumptions <= clicks AND effective_consumptions <= detail_page_views - direct_page_views),
    interactions bigint NOT NULL CHECK (interactions >= 0),
    likes bigint NOT NULL CHECK (likes >= 0),
    comments bigint NOT NULL CHECK (comments >= 0),
    shares bigint NOT NULL CHECK (shares >= 0),
    favorites bigint NOT NULL CHECK (favorites >= 0),
    negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
    unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
    detail_visitors bigint NOT NULL CHECK (detail_visitors >= 0 AND detail_visitors <= detail_page_views),
    CHECK (interactions = likes + comments + shares + favorites),
    PRIMARY KEY (tenant_id, news_id, event_time, snapshot_revision),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    CHECK (date_part('second',event_time) = 0 AND mod(date_part('minute',event_time)::integer,5) = 0)
);
CREATE TABLE news_sim_v8.news_metric_channel_hourly (
    tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
    snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0),
    channel_id text NOT NULL,

    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    detail_page_views bigint NOT NULL CHECK (detail_page_views >= 0),
    direct_page_views bigint NOT NULL CHECK (direct_page_views >= 0 AND direct_page_views <= detail_page_views),
    total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
    effective_consumptions bigint NOT NULL CHECK (effective_consumptions >= 0 AND effective_consumptions <= clicks AND effective_consumptions <= detail_page_views - direct_page_views),
    interactions bigint NOT NULL CHECK (interactions >= 0),
    likes bigint NOT NULL CHECK (likes >= 0),
    comments bigint NOT NULL CHECK (comments >= 0),
    shares bigint NOT NULL CHECK (shares >= 0),
    favorites bigint NOT NULL CHECK (favorites >= 0),
    negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
    unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
    detail_visitors bigint NOT NULL CHECK (detail_visitors >= 0 AND detail_visitors <= detail_page_views),
    CHECK (interactions = likes + comments + shares + favorites),
    PRIMARY KEY (tenant_id,news_id,event_time,snapshot_revision,channel_id),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    FOREIGN KEY (tenant_id,channel_id) REFERENCES news_sim_v8.dim_channel(tenant_id,channel_id),
    FOREIGN KEY (tenant_id,event_time,snapshot_revision) REFERENCES news_sim_v8.window_snapshot(tenant_id,event_time,snapshot_revision)
);
CREATE TABLE news_sim_v8.news_metric_hourly (
    tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
    snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0),

    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    detail_page_views bigint NOT NULL CHECK (detail_page_views >= 0),
    direct_page_views bigint NOT NULL CHECK (direct_page_views >= 0 AND direct_page_views <= detail_page_views),
    total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
    effective_consumptions bigint NOT NULL CHECK (effective_consumptions >= 0 AND effective_consumptions <= clicks AND effective_consumptions <= detail_page_views - direct_page_views),
    interactions bigint NOT NULL CHECK (interactions >= 0),
    likes bigint NOT NULL CHECK (likes >= 0),
    comments bigint NOT NULL CHECK (comments >= 0),
    shares bigint NOT NULL CHECK (shares >= 0),
    favorites bigint NOT NULL CHECK (favorites >= 0),
    negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
    unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
    detail_visitors bigint NOT NULL CHECK (detail_visitors >= 0 AND detail_visitors <= detail_page_views),
    CHECK (interactions = likes + comments + shares + favorites),
    uv_method text NOT NULL CHECK (uv_method = 'temporary_interval_union_v8.1'), policy_version text NOT NULL,
    PRIMARY KEY (tenant_id,news_id,event_time,snapshot_revision),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    FOREIGN KEY (tenant_id,event_time,snapshot_revision) REFERENCES news_sim_v8.window_snapshot(tenant_id,event_time,snapshot_revision),
    FOREIGN KEY (tenant_id,policy_version) REFERENCES news_sim_v8.dim_behavior_policy(tenant_id,policy_version)
);
CREATE TABLE news_sim_v8.news_metric_daily (
    tenant_id uuid NOT NULL, news_id text NOT NULL, stat_date date NOT NULL,

    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
    detail_page_views bigint NOT NULL CHECK (detail_page_views >= 0),
    direct_page_views bigint NOT NULL CHECK (direct_page_views >= 0 AND direct_page_views <= detail_page_views),
    total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
    effective_consumptions bigint NOT NULL CHECK (effective_consumptions >= 0 AND effective_consumptions <= clicks AND effective_consumptions <= detail_page_views - direct_page_views),
    interactions bigint NOT NULL CHECK (interactions >= 0),
    likes bigint NOT NULL CHECK (likes >= 0),
    comments bigint NOT NULL CHECK (comments >= 0),
    shares bigint NOT NULL CHECK (shares >= 0),
    favorites bigint NOT NULL CHECK (favorites >= 0),
    negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
    unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
    detail_visitors bigint NOT NULL CHECK (detail_visitors >= 0 AND detail_visitors <= detail_page_views),
    CHECK (interactions = likes + comments + shares + favorites),
    coverage_start timestamptz NOT NULL, coverage_end timestamptz NOT NULL,
    observed_hours integer NOT NULL CHECK (observed_hours BETWEEN 1 AND 24),
    completeness text NOT NULL CHECK (completeness IN ('complete','partial')),
    excluded_hours integer NOT NULL CHECK (excluded_hours >= 0),
    snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0),
    uv_method text NOT NULL CHECK (uv_method = 'temporary_interval_union_v8.1'), policy_version text NOT NULL,
    PRIMARY KEY (tenant_id,news_id,stat_date),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    FOREIGN KEY (tenant_id,policy_version) REFERENCES news_sim_v8.dim_behavior_policy(tenant_id,policy_version),
    CHECK (coverage_start < coverage_end),
    CHECK (completeness <> 'complete' OR (excluded_hours = 0 AND
        coverage_end = (stat_date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai'))
);
CREATE TABLE news_sim_v8.news_metric_baseline_hourly (
    tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
    sample_count integer NOT NULL CHECK (sample_count BETWEEN 0 AND 3), baseline_version text NOT NULL,
    baseline_impressions numeric(20,2) CHECK (baseline_impressions >= 0),
    baseline_clicks numeric(20,2) CHECK (baseline_clicks >= 0),
    baseline_effective_consumptions numeric(20,2) CHECK (baseline_effective_consumptions >= 0),
    baseline_interactions numeric(20,2) CHECK (baseline_interactions >= 0),
    PRIMARY KEY (tenant_id,news_id,event_time),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    CHECK ((sample_count = 0 AND baseline_impressions IS NULL AND baseline_clicks IS NULL
        AND baseline_effective_consumptions IS NULL AND baseline_interactions IS NULL)
      OR (sample_count > 0 AND baseline_impressions IS NOT NULL AND baseline_clicks IS NOT NULL
        AND baseline_effective_consumptions IS NOT NULL AND baseline_interactions IS NOT NULL)),
    CHECK (baseline_clicks <= baseline_impressions AND baseline_effective_consumptions <= baseline_clicks)
);
CREATE TABLE news_sim_v8.content_change_event (
    tenant_id uuid NOT NULL CHECK (tenant_id = '33333333-3333-4333-8333-333333333333'),
    event_id text NOT NULL, news_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('withdraw','restrict','correct')),
    from_version integer NOT NULL CHECK (from_version > 0), to_version integer NOT NULL CHECK (to_version >= from_version),
    lifecycle_revision integer NOT NULL CHECK (lifecycle_revision > 1),
    new_status text NOT NULL CHECK (new_status IN ('withdrawn','restricted','published')),
    effective_at timestamptz NOT NULL, received_at timestamptz NOT NULL, reason_code text NOT NULL,
    lifecycle_data_synthetic boolean NOT NULL CHECK (lifecycle_data_synthetic),
    PRIMARY KEY (tenant_id,event_id), UNIQUE (tenant_id,news_id,lifecycle_revision),
    UNIQUE (tenant_id,event_id,news_id),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    CHECK (received_at >= effective_at),
    CHECK ((event_type = 'correct' AND to_version > from_version AND new_status = 'published')
        OR (event_type = 'withdraw' AND new_status = 'withdrawn')
        OR (event_type = 'restrict' AND new_status = 'restricted'))
);
CREATE TABLE news_sim_v8.content_tombstone (
    tenant_id uuid NOT NULL, news_id text NOT NULL, event_id text NOT NULL,
    blocked_through_version integer NOT NULL CHECK (blocked_through_version > 0),
    lifecycle_revision integer NOT NULL CHECK (lifecycle_revision > 1),
    status text NOT NULL CHECK (status IN ('withdrawn','restricted')), effective_at timestamptz NOT NULL,
    lifecycle_data_synthetic boolean NOT NULL CHECK (lifecycle_data_synthetic),
    PRIMARY KEY (tenant_id,news_id),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    FOREIGN KEY (tenant_id,event_id,news_id) REFERENCES news_sim_v8.content_change_event(tenant_id,event_id,news_id)
);
CREATE TABLE news_sim_v8.index_cleanup_task (
    tenant_id uuid NOT NULL, task_id text NOT NULL, event_id text NOT NULL, news_id text NOT NULL,
    target text NOT NULL CHECK (target IN ('postgres_knowledge','milvus','bge_snapshot','retrieval_cache')),
    action text NOT NULL, through_version integer NOT NULL CHECK (through_version > 0),
    status text NOT NULL CHECK (status = 'pending'), attempt_count integer NOT NULL CHECK (attempt_count = 0),
    mapping_required boolean NOT NULL CHECK (mapping_required), lifecycle_data_synthetic boolean NOT NULL CHECK (lifecycle_data_synthetic),
    created_at timestamptz NOT NULL, PRIMARY KEY (tenant_id,task_id), UNIQUE (tenant_id,event_id,target),
    FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v8.dim_news(tenant_id,news_id),
    FOREIGN KEY (tenant_id,event_id,news_id) REFERENCES news_sim_v8.content_change_event(tenant_id,event_id,news_id)
);

-- Child rows cannot begin before their real article's containing publication bucket.
CREATE FUNCTION news_sim_v8.enforce_publication_and_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE published timestamptz; first_bucket timestamptz;
BEGIN
    SELECT publish_time INTO STRICT published FROM news_sim_v8.dim_news
      WHERE tenant_id = NEW.tenant_id AND news_id = NEW.news_id;
    IF TG_TABLE_NAME = 'news_metric_5min' THEN
        first_bucket := date_trunc('hour',published) + floor(extract(minute FROM published)/5) * interval '5 minutes';
        IF NOT EXISTS (SELECT 1 FROM news_sim_v8.window_snapshot WHERE tenant_id = NEW.tenant_id
          AND event_time = date_trunc('hour',NEW.event_time) AND snapshot_revision = NEW.snapshot_revision) THEN
            RAISE EXCEPTION 'five-minute row has no same-tenant parent snapshot';
        END IF;
    ELSE first_bucket := date_trunc('hour',published);
    END IF;
    IF NEW.event_time < first_bucket THEN RAISE EXCEPTION 'metrics before real publication bucket are forbidden'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER metric_publication_5min BEFORE INSERT OR UPDATE ON news_sim_v8.news_metric_5min
    FOR EACH ROW EXECUTE FUNCTION news_sim_v8.enforce_publication_and_snapshot();
CREATE TRIGGER metric_publication_hour BEFORE INSERT OR UPDATE ON news_sim_v8.news_metric_hourly
    FOR EACH ROW EXECUTE FUNCTION news_sim_v8.enforce_publication_and_snapshot();
CREATE TRIGGER metric_publication_channel BEFORE INSERT OR UPDATE ON news_sim_v8.news_metric_channel_hourly
    FOR EACH ROW EXECUTE FUNCTION news_sim_v8.enforce_publication_and_snapshot();
CREATE TRIGGER metric_publication_baseline BEFORE INSERT OR UPDATE ON news_sim_v8.news_metric_baseline_hourly
    FOR EACH ROW EXECUTE FUNCTION news_sim_v8.enforce_publication_and_snapshot();

CREATE INDEX metric_hour_news_idx ON news_sim_v8.news_metric_hourly(tenant_id,event_time,news_id,snapshot_revision);
CREATE INDEX metric_5min_news_idx ON news_sim_v8.news_metric_5min(tenant_id,event_time,news_id,snapshot_revision);
CREATE VIEW news_sim_v8.latest_window_snapshot AS
    SELECT DISTINCT ON (tenant_id,event_time) * FROM news_sim_v8.window_snapshot
    ORDER BY tenant_id,event_time,snapshot_revision DESC;
CREATE VIEW news_sim_v8.eligible_news AS
    SELECT n.* FROM news_sim_v8.dim_news n WHERE n.status = 'published'
    AND NOT EXISTS (SELECT 1 FROM news_sim_v8.content_tombstone t WHERE t.tenant_id = n.tenant_id AND t.news_id = n.news_id);
-- Exactly the old 18 field names; no runtime compatibility or Agent connection is claimed.
CREATE VIEW news_sim_v8.news_behavior_aggregate AS
    SELECT m.tenant_id,m.news_id,m.event_time,n.title,n.content_type,n.category,n.source,n.publish_time,
        m.impressions,m.clicks,m.unique_users,m.total_duration_seconds,m.effective_consumptions,m.interactions,
        b.baseline_impressions,b.baseline_clicks,b.baseline_effective_consumptions,b.baseline_interactions
    FROM news_sim_v8.news_metric_hourly m
    JOIN news_sim_v8.latest_window_snapshot w ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time AND w.snapshot_revision=m.snapshot_revision
    JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
    LEFT JOIN news_sim_v8.news_metric_baseline_hourly b ON b.tenant_id=m.tenant_id AND b.news_id=m.news_id AND b.event_time=m.event_time
    WHERE w.completeness IN ('complete','corrected') AND w.quality_status='passed';
CREATE VIEW news_sim_v8.news_operational_hourly AS
    SELECT m.*,n.title,n.source_url,n.publish_time,n.category,n.status,n.lifecycle_data_synthetic,
        w.watermark_at,w.snapshot_at,w.completeness,w.quality_status,
        b.sample_count,b.baseline_version,b.baseline_impressions,b.baseline_clicks,
        b.baseline_effective_consumptions,b.baseline_interactions,
        m.clicks::numeric/NULLIF(m.impressions,0) AS ctr,
        m.effective_consumptions::numeric/NULLIF(m.clicks,0) AS click_attributed_effective_rate,
        m.total_duration_seconds::numeric/NULLIF(m.detail_page_views,0) AS average_duration_per_detail_view
    FROM news_sim_v8.news_metric_hourly m
    JOIN news_sim_v8.latest_window_snapshot w ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time AND w.snapshot_revision=m.snapshot_revision
    JOIN news_sim_v8.dim_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
    LEFT JOIN news_sim_v8.news_metric_baseline_hourly b ON b.tenant_id=m.tenant_id AND b.news_id=m.news_id AND b.event_time=m.event_time;

