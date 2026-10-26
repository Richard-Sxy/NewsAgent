-- Mutable live projections are separate from immutable final facts in migration 001.
CREATE TABLE news_realtime.live_window (
 window_key text PRIMARY KEY, session_id text NOT NULL, catalog_id text NOT NULL REFERENCES news_realtime.catalog,
 tenant_id text NOT NULL, run_id text NOT NULL, start_ms bigint NOT NULL,
 metrics jsonb NOT NULL, readers jsonb NOT NULL, revision bigint NOT NULL CHECK(revision>0),
 final boolean NOT NULL, watermark_ms bigint NOT NULL, event_count bigint NOT NULL,
 last_event_ms bigint NOT NULL, source_updated_at timestamptz NOT NULL,
 projected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX live_window_time ON news_realtime.live_window(tenant_id,start_ms DESC);
CREATE TABLE news_realtime.reader_final (
 window_key text PRIMARY KEY, session_id text NOT NULL, source_release_sha256 text NOT NULL,
 tenant_id text NOT NULL, run_id text NOT NULL, start_ms bigint NOT NULL,
 revision bigint NOT NULL, watermark_ms bigint NOT NULL, generated_at timestamptz NOT NULL,
 items jsonb NOT NULL, identity_sha256 text NOT NULL
);
CREATE TRIGGER frozen_reader_final BEFORE UPDATE OR DELETE ON news_realtime.reader_final
 FOR EACH ROW EXECUTE FUNCTION news_realtime.immutable();
CREATE VIEW news_realtime.news_live_current AS
SELECT l.window_key,l.session_id,l.revision,l.tenant_id,l.run_id,l.catalog_id,
 to_timestamp(l.start_ms/1000.0) AS window_start,to_timestamp((l.start_ms+60000)/1000.0) AS window_end,
 m->>'news_id' AS news_id,c.title,c.url,c.body,c.content_type,t.topic_ids,
 (m->>'impressions')::bigint AS impressions,(m->>'clicks')::bigint AS clicks,
 (m->>'unique_users')::bigint AS unique_users,
 (m->>'total_duration_seconds')::bigint AS total_duration_seconds,
 (m->>'effective_consumptions')::bigint AS effective_consumptions,(m->>'interactions')::bigint AS interactions,
 r.value AS reader_profile,(r.value->>'reader_count')::bigint AS reader_count,
 CASE WHEN l.final THEN 'complete' ELSE 'provisional' END AS reader_profile_status,
 l.final,to_timestamp(l.watermark_ms/1000.0) AS watermark,
 to_timestamp(l.last_event_ms/1000.0) AS last_event_at,l.source_updated_at,l.projected_at,l.checked_at,
 l.checked_at>clock_timestamp()-interval '15 seconds' AS source_check_fresh,
 extract(epoch FROM l.projected_at-to_timestamp(l.last_event_ms/1000.0)) AS latest_event_to_sql_seconds,
 'wall_clock'::text AS event_time_mode,'synthetic'::text AS metric_source_mode,
 'public_cached'::text AS content_source_mode,
 cat.manifest->>'content_release_id' AS content_release_id,cat.manifest->>'tag_release_id' AS tag_release_id
FROM news_realtime.live_window l
CROSS JOIN LATERAL jsonb_array_elements(l.metrics) m
JOIN news_realtime.catalog cat USING(catalog_id)
JOIN news_realtime.catalog_news cn ON cn.catalog_id=l.catalog_id AND cn.news_id=m->>'news_id'
JOIN public.news_content_selected c ON c.tenant_id=cat.manifest->>'content_tenant_id'
 AND c.release_id=cat.manifest->>'content_release_id' AND c.news_id=cn.news_id
 AND c.record_sha256=cn.record_sha256 AND c.body_sha256=cn.body_sha256
JOIN public.news_content_tags t ON t.tenant_id=c.tenant_id AND t.content_release_id=c.release_id
 AND t.tag_release_id=cat.manifest->>'tag_release_id' AND t.news_id=c.news_id
 AND t.record_sha256=c.record_sha256 AND t.body_sha256=c.body_sha256
LEFT JOIN LATERAL jsonb_array_elements(l.readers) r ON r.value->>'news_id'=cn.news_id
WHERE NOT EXISTS(SELECT 1 FROM news_realtime.invalid_window i WHERE i.window_key=l.window_key);
CREATE VIEW news_realtime.reader_interest_current AS
SELECT n.window_key,n.tenant_id,n.run_id,n.news_id,n.title,n.window_start,n.window_end,n.final,
 n.reader_count,n.reader_profile_status,n.source_check_fresh,n.watermark,
 (d.value->>'lookback_hours')::integer AS lookback_hours,
 (d.value->>'profiled_reader_count')::bigint AS profiled_reader_count,
 (d.value->>'unknown_reader_count')::bigint AS unknown_reader_count,
 (d.value->>'cold_start_reader_count')::bigint AS cold_start_reader_count,
 p.value->>'topic_id' AS topic_id,(p.value->>'reader_count')::bigint AS topic_reader_count,
 (p.value->>'share_all')::numeric AS share_all,(p.value->>'share_profiled')::numeric AS share_profiled,
 (p.value->>'baseline_share')::numeric AS baseline_share,(p.value->>'lift')::numeric AS lift,
 (p.value->>'low_sample')::boolean AS low_sample
FROM news_realtime.news_live_current n
CROSS JOIN LATERAL (VALUES (n.reader_profile->'long_term'),(n.reader_profile->'short_term')) d(value)
LEFT JOIN LATERAL jsonb_array_elements(d.value->'topics') p ON true;
