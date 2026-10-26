-- Versioned, local public-content/synthetic-metrics integration. Existing releases stay frozen.
CREATE TABLE news_realtime.catalog (
  catalog_id text PRIMARY KEY, manifest jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE news_realtime.catalog_news (
  catalog_id text NOT NULL REFERENCES news_realtime.catalog,
  news_id text NOT NULL, content_type text NOT NULL CHECK(content_type IN ('article','video')),
  record_sha256 text NOT NULL, body_sha256 text NOT NULL, publish_ms bigint NOT NULL,
  PRIMARY KEY(catalog_id,news_id)
);
CREATE VIEW public.news_content_dedup_map AS
SELECT c.*, min(news_id) OVER groups AS canonical_news_id, count(*) OVER groups AS duplicate_members
FROM public.news_content_selected c
WINDOW groups AS (PARTITION BY tenant_id,release_id,content_type,
  CASE WHEN body_sha256 IS NOT NULL THEN 'body:'||body_sha256 ELSE 'news:'||news_id END);
CREATE VIEW public.news_content_deduplicated AS
SELECT * FROM public.news_content_dedup_map WHERE news_id=canonical_news_id;

CREATE TABLE news_realtime.snapshot (
  snapshot_id text PRIMARY KEY, catalog_id text NOT NULL REFERENCES news_realtime.catalog,
  window_key text NOT NULL, revision bigint NOT NULL CHECK(revision>0),
  tenant_id text NOT NULL, run_id text NOT NULL, window_start_ms bigint NOT NULL,
  window_end_ms bigint NOT NULL CHECK(window_end_ms=window_start_ms+60000),
  payload_text text NOT NULL, report jsonb NOT NULL,
  source_published_at timestamptz NOT NULL, projected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(window_key,revision), UNIQUE(snapshot_id,catalog_id),
  CHECK(encode(sha256(convert_to(payload_text,'UTF8')),'hex')=snapshot_id)
);
CREATE TABLE news_realtime.metric (
  snapshot_id text NOT NULL, catalog_id text NOT NULL, news_id text NOT NULL,
  impressions bigint NOT NULL CHECK(impressions>=0), clicks bigint NOT NULL CHECK(clicks>=0 AND clicks<=impressions),
  unique_users bigint NOT NULL CHECK(unique_users>=0 AND unique_users<=clicks),
  total_duration_seconds bigint NOT NULL CHECK(total_duration_seconds>=0),
  effective_consumptions bigint NOT NULL CHECK(effective_consumptions>=0 AND effective_consumptions<=clicks),
  interactions bigint NOT NULL CHECK(interactions>=0), hot_score numeric NOT NULL CHECK(hot_score>=0),
  PRIMARY KEY(snapshot_id,news_id),
  FOREIGN KEY(snapshot_id,catalog_id) REFERENCES news_realtime.snapshot(snapshot_id,catalog_id),
  FOREIGN KEY(catalog_id,news_id) REFERENCES news_realtime.catalog_news(catalog_id,news_id)
);
CREATE TABLE news_realtime.invalid_window (
  window_key text PRIMARY KEY, reason text NOT NULL, observed_at timestamptz NOT NULL
);
CREATE TABLE news_realtime.sync_status (
  id integer PRIMARY KEY CHECK(id=1), last_success_at timestamptz NOT NULL
);
CREATE INDEX realtime_window ON news_realtime.snapshot(tenant_id,window_start_ms,revision DESC);
CREATE FUNCTION news_realtime.immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Published realtime/catalog facts are immutable'; END $$;
CREATE TRIGGER frozen_catalog BEFORE UPDATE OR DELETE ON news_realtime.catalog FOR EACH ROW EXECUTE FUNCTION news_realtime.immutable();
CREATE TRIGGER frozen_catalog_news BEFORE UPDATE OR DELETE ON news_realtime.catalog_news FOR EACH ROW EXECUTE FUNCTION news_realtime.immutable();
CREATE TRIGGER frozen_snapshot BEFORE UPDATE OR DELETE ON news_realtime.snapshot FOR EACH ROW EXECUTE FUNCTION news_realtime.immutable();
CREATE TRIGGER frozen_metric BEFORE UPDATE OR DELETE ON news_realtime.metric FOR EACH ROW EXECUTE FUNCTION news_realtime.immutable();
CREATE VIEW news_realtime.news_detail_current AS
WITH heads AS (
  SELECT DISTINCT ON (window_key) * FROM news_realtime.snapshot ORDER BY window_key,revision DESC
)
SELECT s.snapshot_id,s.revision,s.tenant_id,s.run_id,s.catalog_id,
  to_timestamp(s.window_start_ms/1000.0) AS window_start,to_timestamp(s.window_end_ms/1000.0) AS window_end,
  m.news_id,c.title,c.body,c.url,c.content_type,c.content_state,c.record_sha256,c.body_sha256,
  cat.manifest->>'content_release_id' AS content_release_id,
  cat.manifest->>'tag_release_id' AS tag_release_id,t.topic_ids,t.label_status,
  m.impressions,m.clicks,m.unique_users,m.total_duration_seconds,m.effective_consumptions,m.interactions,m.hot_score,
  m.clicks::numeric/nullif(m.impressions,0) AS ctr,
  'synthetic'::text AS metric_source_mode,'public_cached'::text AS content_source_mode,
  'accelerated_synthetic'::text AS event_time_mode,
  'unavailable_for_this_minute_window'::text AS reader_profile_status,
  s.report,s.source_published_at,s.projected_at,
  sync.last_success_at AS source_checked_at,
  (sync.last_success_at > clock_timestamp()-interval '30 seconds') AS source_check_fresh
FROM heads s JOIN news_realtime.metric m USING(snapshot_id,catalog_id)
JOIN news_realtime.catalog cat USING(catalog_id)
JOIN news_realtime.catalog_news cn USING(catalog_id,news_id)
JOIN public.news_content_selected c ON c.tenant_id=cat.manifest->>'content_tenant_id'
  AND c.release_id=cat.manifest->>'content_release_id' AND c.news_id=m.news_id AND c.record_sha256=cn.record_sha256 AND c.body_sha256=cn.body_sha256
JOIN public.news_content_tags t ON t.tenant_id=c.tenant_id AND t.content_release_id=c.release_id
  AND t.tag_release_id=cat.manifest->>'tag_release_id' AND t.news_id=c.news_id AND t.record_sha256=c.record_sha256 AND t.body_sha256=c.body_sha256
LEFT JOIN news_realtime.sync_status sync ON sync.id=1
WHERE NOT EXISTS(SELECT 1 FROM news_realtime.invalid_window i WHERE i.window_key=s.window_key);
