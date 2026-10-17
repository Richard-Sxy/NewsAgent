-- Aggregate-only, synthetic fixture. A pre-existing schema causes an error.
-- Run the generated warehouse.sql using psql; do not load into the active dw.
CREATE SCHEMA news_sim_v7;

CREATE TABLE news_sim_v7.dim_publisher (
 tenant_id uuid NOT NULL, publisher_id text NOT NULL, publisher_name text NOT NULL,
 publisher_kind text NOT NULL, synthetic boolean NOT NULL CHECK (synthetic),
 PRIMARY KEY (tenant_id,publisher_id)
);
CREATE TABLE news_sim_v7.dim_channel (
 tenant_id uuid NOT NULL, channel_id text NOT NULL, channel_name text NOT NULL,
 platform text NOT NULL CHECK (platform IN ('app','web')),
 synthetic boolean NOT NULL CHECK (synthetic), PRIMARY KEY (tenant_id,channel_id)
);
CREATE TABLE news_sim_v7.dim_news (
 tenant_id uuid NOT NULL, news_id text NOT NULL, title text NOT NULL,
 content_type text NOT NULL CHECK (content_type IN ('article','video')),
 content_format text NOT NULL CHECK (content_format IN ('article','video','gallery','live_text')),
 category text NOT NULL CHECK (category IN ('科技','财经','体育','社会')),
 primary_topic text NOT NULL, event_id text NOT NULL, publisher_id text NOT NULL,
 source text NOT NULL, publish_time timestamptz NOT NULL,
 content_version integer NOT NULL CHECK (content_version > 0),
 lifecycle_revision integer NOT NULL CHECK (lifecycle_revision > 0),
 status text NOT NULL CHECK (status IN ('published','withdrawn','restricted')),
 status_effective_at timestamptz NOT NULL, content_source text NOT NULL,
 synthetic boolean NOT NULL CHECK (synthetic), PRIMARY KEY (tenant_id,news_id),
 FOREIGN KEY (tenant_id,publisher_id) REFERENCES news_sim_v7.dim_publisher
);
CREATE TABLE news_sim_v7.window_snapshot (
 tenant_id uuid NOT NULL, event_time timestamptz NOT NULL,
 snapshot_revision integer NOT NULL CHECK (snapshot_revision > 0),
 window_end timestamptz NOT NULL, watermark_at timestamptz NOT NULL,
 snapshot_at timestamptz NOT NULL,
 completeness text NOT NULL CHECK (completeness IN ('incomplete','complete','corrected')),
 late_corrected_bins bigint NOT NULL CHECK (late_corrected_bins >= 0),
 late_added_clicks bigint NOT NULL CHECK (late_added_clicks >= 0),
 metric_policy_version text NOT NULL, dataset_version text NOT NULL,
 quality_status text NOT NULL CHECK (quality_status IN ('pending','passed')),
 synthetic boolean NOT NULL CHECK (synthetic),
 PRIMARY KEY (tenant_id,event_time,snapshot_revision),
 CHECK (date_trunc('hour',event_time AT TIME ZONE 'UTC') = event_time AT TIME ZONE 'UTC'),
 CHECK (window_end = event_time + interval '1 hour'),
 CHECK (snapshot_at >= watermark_at),
 CHECK (completeness = 'incomplete' OR (watermark_at >= window_end AND quality_status = 'passed'))
);
CREATE TABLE news_sim_v7.news_metric_channel_hourly (
 tenant_id uuid NOT NULL, news_id text NOT NULL, channel_id text NOT NULL,
 event_time timestamptz NOT NULL, snapshot_revision integer NOT NULL,
 impressions bigint NOT NULL CHECK (impressions >= 0),
 clicks bigint NOT NULL CHECK (clicks BETWEEN 0 AND impressions),
 total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
 effective_consumptions bigint NOT NULL CHECK (effective_consumptions BETWEEN 0 AND clicks),
 interactions bigint NOT NULL CHECK (interactions >= 0),
 likes bigint NOT NULL CHECK (likes >= 0), comments bigint NOT NULL CHECK (comments >= 0),
 shares bigint NOT NULL CHECK (shares >= 0), favorites bigint NOT NULL CHECK (favorites >= 0),
 follows bigint NOT NULL CHECK (follows >= 0), negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
 video_starts bigint NOT NULL CHECK (video_starts BETWEEN 0 AND clicks),
 video_completions bigint NOT NULL CHECK (video_completions BETWEEN 0 AND video_starts),
 unique_users bigint NOT NULL CHECK (unique_users BETWEEN 0 AND clicks),
 PRIMARY KEY (tenant_id,news_id,channel_id,event_time,snapshot_revision),
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news,
 FOREIGN KEY (tenant_id,channel_id) REFERENCES news_sim_v7.dim_channel,
 FOREIGN KEY (tenant_id,event_time,snapshot_revision) REFERENCES news_sim_v7.window_snapshot,
 CHECK (interactions = likes + comments + shares + favorites + follows)
);
CREATE TABLE news_sim_v7.news_metric_hourly (
 tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
 snapshot_revision integer NOT NULL,
 impressions bigint NOT NULL CHECK (impressions >= 0),
 clicks bigint NOT NULL CHECK (clicks BETWEEN 0 AND impressions),
 total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
 effective_consumptions bigint NOT NULL CHECK (effective_consumptions BETWEEN 0 AND clicks),
 interactions bigint NOT NULL CHECK (interactions >= 0),
 likes bigint NOT NULL CHECK (likes >= 0), comments bigint NOT NULL CHECK (comments >= 0),
 shares bigint NOT NULL CHECK (shares >= 0), favorites bigint NOT NULL CHECK (favorites >= 0),
 follows bigint NOT NULL CHECK (follows >= 0), negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
 video_starts bigint NOT NULL CHECK (video_starts BETWEEN 0 AND clicks),
 video_completions bigint NOT NULL CHECK (video_completions BETWEEN 0 AND video_starts),
 unique_users bigint NOT NULL CHECK (unique_users BETWEEN 0 AND clicks),
 uv_method text NOT NULL CHECK (uv_method = 'synthetic_interval_union_v1'),
 PRIMARY KEY (tenant_id,news_id,event_time,snapshot_revision),
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news,
 FOREIGN KEY (tenant_id,event_time,snapshot_revision) REFERENCES news_sim_v7.window_snapshot,
 CHECK (interactions = likes + comments + shares + favorites + follows)
);
CREATE TABLE news_sim_v7.news_metric_5min (
 tenant_id uuid NOT NULL, news_id text NOT NULL, channel_id text NOT NULL,
 event_time timestamptz NOT NULL, snapshot_revision integer NOT NULL,
 impressions bigint NOT NULL CHECK (impressions >= 0),
 clicks bigint NOT NULL CHECK (clicks BETWEEN 0 AND impressions),
 unique_users bigint NOT NULL CHECK (unique_users BETWEEN 0 AND clicks),
 total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
 effective_consumptions bigint NOT NULL CHECK (effective_consumptions BETWEEN 0 AND clicks),
 interactions bigint NOT NULL CHECK (interactions >= 0),
 likes bigint NOT NULL CHECK (likes >= 0), comments bigint NOT NULL CHECK (comments >= 0),
 shares bigint NOT NULL CHECK (shares >= 0), favorites bigint NOT NULL CHECK (favorites >= 0),
 follows bigint NOT NULL CHECK (follows >= 0), negative_feedback bigint NOT NULL CHECK (negative_feedback >= 0),
 video_starts bigint NOT NULL CHECK (video_starts BETWEEN 0 AND clicks),
 video_completions bigint NOT NULL CHECK (video_completions BETWEEN 0 AND video_starts),
 PRIMARY KEY (tenant_id,news_id,channel_id,event_time,snapshot_revision),
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news,
 FOREIGN KEY (tenant_id,channel_id) REFERENCES news_sim_v7.dim_channel,
 CHECK (extract(second FROM event_time) = 0 AND extract(minute FROM event_time)::int % 5 = 0),
 CHECK (interactions = likes + comments + shares + favorites + follows)
);
CREATE TABLE news_sim_v7.news_metric_baseline_hourly (
 tenant_id uuid NOT NULL, news_id text NOT NULL, event_time timestamptz NOT NULL,
 sample_count integer NOT NULL CHECK (sample_count BETWEEN 0 AND 3), baseline_version text NOT NULL,
 baseline_impressions numeric(20,2), baseline_clicks numeric(20,2),
 baseline_effective_consumptions numeric(20,2), baseline_interactions numeric(20,2),
 PRIMARY KEY (tenant_id,news_id,event_time),
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news,
 CHECK ((sample_count = 0 AND baseline_impressions IS NULL AND baseline_clicks IS NULL
   AND baseline_effective_consumptions IS NULL AND baseline_interactions IS NULL)
  OR (sample_count > 0 AND baseline_impressions IS NOT NULL AND baseline_clicks IS NOT NULL
   AND baseline_effective_consumptions IS NOT NULL AND baseline_interactions IS NOT NULL
   AND baseline_impressions >= baseline_clicks AND baseline_clicks >= baseline_effective_consumptions
   AND baseline_effective_consumptions >= 0 AND baseline_interactions >= 0))
);
CREATE TABLE news_sim_v7.content_change_event (
 tenant_id uuid NOT NULL, event_id text NOT NULL, news_id text NOT NULL,
 event_type text NOT NULL CHECK (event_type IN ('withdraw','restrict','correct')),
 from_version integer NOT NULL CHECK (from_version > 0),
 to_version integer NOT NULL CHECK (to_version >= from_version), lifecycle_revision integer NOT NULL,
 new_status text NOT NULL CHECK (new_status IN ('published','withdrawn','restricted')),
 effective_at timestamptz NOT NULL, received_at timestamptz NOT NULL,
 reason_code text NOT NULL, synthetic boolean NOT NULL CHECK (synthetic),
 PRIMARY KEY (tenant_id,event_id), UNIQUE (tenant_id,news_id,lifecycle_revision),
 UNIQUE (tenant_id,event_id,news_id),
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news,
 CHECK (received_at >= effective_at)
);
CREATE TABLE news_sim_v7.content_tombstone (
 tenant_id uuid NOT NULL, news_id text NOT NULL, blocked_through_version integer NOT NULL,
 lifecycle_revision integer NOT NULL, event_id text NOT NULL,
 status text NOT NULL CHECK (status IN ('withdrawn','restricted')),
 effective_at timestamptz NOT NULL, policy_version text NOT NULL,
 PRIMARY KEY (tenant_id,news_id),
 FOREIGN KEY (tenant_id,event_id,news_id) REFERENCES news_sim_v7.content_change_event (tenant_id,event_id,news_id),
 CHECK (blocked_through_version > 0 AND lifecycle_revision > 0)
);
CREATE TABLE news_sim_v7.index_cleanup_task (
 tenant_id uuid NOT NULL, task_id text NOT NULL, event_id text NOT NULL, news_id text NOT NULL,
 content_source text NOT NULL, target text NOT NULL, action text NOT NULL,
 through_version integer NOT NULL CHECK (through_version > 0), lifecycle_revision integer NOT NULL,
 status text NOT NULL CHECK (status IN ('pending','running','retry','verified','failed')),
 attempt_count integer NOT NULL CHECK (attempt_count >= 0), mapping_required boolean NOT NULL,
 created_at timestamptz NOT NULL, PRIMARY KEY (tenant_id,task_id),
 UNIQUE (tenant_id,event_id,target),
 FOREIGN KEY (tenant_id,event_id,news_id) REFERENCES news_sim_v7.content_change_event (tenant_id,event_id,news_id)
);
CREATE TABLE news_sim_v7.board_snapshot (
 tenant_id uuid NOT NULL, board_id text NOT NULL, snapshot_id text NOT NULL,
 snapshot_at timestamptz NOT NULL, board_policy_version text NOT NULL,
 source_kind text NOT NULL CHECK (source_kind = 'synthetic_original_board'),
 synthetic boolean NOT NULL CHECK (synthetic), PRIMARY KEY (tenant_id,board_id,snapshot_id)
);
CREATE TABLE news_sim_v7.board_item (
 tenant_id uuid NOT NULL, board_id text NOT NULL, snapshot_id text NOT NULL, news_id text NOT NULL,
 source_rank integer NOT NULL CHECK (source_rank > 0), source_position integer NOT NULL CHECK (source_position > 0),
 source_score numeric(20,2) NOT NULL, PRIMARY KEY (tenant_id,board_id,snapshot_id,news_id),
 UNIQUE (tenant_id,board_id,snapshot_id,source_position),
 FOREIGN KEY (tenant_id,board_id,snapshot_id) REFERENCES news_sim_v7.board_snapshot,
 FOREIGN KEY (tenant_id,news_id) REFERENCES news_sim_v7.dim_news
);

CREATE INDEX metric_hour_lookup ON news_sim_v7.news_metric_hourly (tenant_id,event_time,snapshot_revision,news_id);
CREATE INDEX metric_channel_lookup ON news_sim_v7.news_metric_channel_hourly (tenant_id,event_time,channel_id);

-- Select latest revision first; never fall back from an incomplete revision to old complete data.
CREATE VIEW news_sim_v7.latest_window_snapshot AS
 SELECT DISTINCT ON (tenant_id,event_time) * FROM news_sim_v7.window_snapshot
 ORDER BY tenant_id,event_time,snapshot_revision DESC;

-- Current eligibility is independent of historical metrics and asynchronous physical cleanup.
CREATE VIEW news_sim_v7.eligible_news AS
 SELECT n.* FROM news_sim_v7.dim_news n
 WHERE n.status = 'published' AND NOT EXISTS (
   SELECT 1 FROM news_sim_v7.content_tombstone t WHERE t.tenant_id = n.tenant_id AND t.news_id = n.news_id
 );

-- Exactly the existing 18-column shape; this new schema is NOT enabled in the Agent SQL allowlist.
CREATE VIEW news_sim_v7.news_behavior_aggregate AS
 SELECT n.tenant_id,n.news_id,m.event_time,n.title,n.content_type,n.category,n.source,n.publish_time,
 m.impressions,m.clicks,m.unique_users,m.total_duration_seconds,m.effective_consumptions,m.interactions,
 b.baseline_impressions,b.baseline_clicks,b.baseline_effective_consumptions,b.baseline_interactions
 FROM news_sim_v7.eligible_news n JOIN news_sim_v7.news_metric_hourly m USING (tenant_id,news_id)
 JOIN news_sim_v7.latest_window_snapshot s USING (tenant_id,event_time,snapshot_revision)
 LEFT JOIN news_sim_v7.news_metric_baseline_hourly b USING (tenant_id,news_id,event_time)
 WHERE s.completeness IN ('complete','corrected') AND s.quality_status = 'passed';

-- Ratios belong to a separate operational projection; undefined denominator remains NULL.
CREATE VIEW news_sim_v7.news_operational_hourly AS
 SELECT m.*,n.content_format,n.primary_topic,n.status,s.completeness,s.watermark_at,s.snapshot_at,
 n.content_version,n.lifecycle_revision,
 m.clicks::numeric / NULLIF(m.impressions,0) AS ctr,
 m.effective_consumptions::numeric / NULLIF(m.clicks,0) AS effective_consumption_rate,
 m.total_duration_seconds::numeric / NULLIF(m.clicks,0) AS average_duration_per_click,
 m.interactions::numeric / NULLIF(m.clicks,0) AS interaction_event_rate,
 m.video_completions::numeric / NULLIF(m.video_starts,0) AS video_completion_rate,
 m.negative_feedback::numeric / NULLIF(m.clicks,0) AS negative_feedback_event_rate
 FROM news_sim_v7.news_metric_hourly m JOIN news_sim_v7.dim_news n USING (tenant_id,news_id)
 JOIN news_sim_v7.latest_window_snapshot s USING (tenant_id,event_time,snapshot_revision);

-- A synthetic original board keeps original rank and position after lifecycle filtering.
CREATE VIEW news_sim_v7.current_board_item AS
 SELECT i.* FROM news_sim_v7.board_item i JOIN news_sim_v7.eligible_news n USING (tenant_id,news_id);
