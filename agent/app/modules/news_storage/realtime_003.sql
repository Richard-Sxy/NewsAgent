-- Clock provenance and upstream-aware freshness; immutable migration 001/002 stay unchanged.
CREATE OR REPLACE VIEW news_realtime.news_live_current AS
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
 (l.checked_at>clock_timestamp()-interval '15 seconds' AND (l.final OR l.source_updated_at>clock_timestamp()-interval '15 seconds')) AS source_check_fresh,
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

CREATE OR REPLACE VIEW news_realtime.news_detail_current AS
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
  coalesce(s.report->>'event_time_mode','accelerated_synthetic') AS event_time_mode,
  coalesce(s.report->>'reader_profile_status','unavailable_for_this_minute_window') AS reader_profile_status,
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
