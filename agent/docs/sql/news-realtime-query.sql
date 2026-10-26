-- Connection: 127.0.0.1:25432 / newsagent_unified_news_20261023
-- Public cached content + synthetic behavior. Do not sum UV across windows or aliases.
SET TIME ZONE 'Asia/Shanghai';

-- 1. Per-release identity dedup and exact-body dedup; these SELECTs do not delete rows.
-- Separate authorized cleanup on 2026-10-25 pruned database rows and archived the old release.
-- Frozen disk packages and pre-cleanup backups retain the complete historical input.
SELECT tenant_id,release_id,count(*) AS original_news_ids,
       count(*) FILTER(WHERE news_id=canonical_news_id) AS unique_content,
       count(*) FILTER(WHERE news_id<>canonical_news_id) AS aliases,
       count(*) FILTER(WHERE news_id=canonical_news_id AND content_state='body_available') AS captured_bodies
FROM public.news_content_dedup_map
GROUP BY tenant_id,release_id;

-- 2. Inspect same-body aliases; their historical behavior remains attached to its original news ID.
SELECT news_id,canonical_news_id,duplicate_members,body_sha256,title
FROM public.news_content_dedup_map
WHERE duplicate_members>1 ORDER BY canonical_news_id,news_id;

-- 3. Latest stored final revision per minute, joined to exact frozen text and content tags.
-- Stored windows may be historical; inspect window time and freshness before describing them as live.
-- source_check_fresh tells whether the source invalidation check was successful in the last 30s.
SELECT tenant_id,run_id,window_start,window_end,news_id,title,topic_ids,label_status,
       impressions,clicks,unique_users,total_duration_seconds,effective_consumptions,interactions,
       round(ctr*100,2) AS ctr_pct,hot_score,metric_source_mode,reader_profile_status,
       source_check_fresh,source_checked_at,snapshot_id
FROM news_realtime.news_detail_current
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
ORDER BY window_start DESC,hot_score DESC,news_id LIMIT 30;

-- 4. Full article text: one exact latest final window, with matching content release and body hash.
WITH latest AS (
  SELECT max(window_start) AS start_at FROM news_realtime.news_detail_current
  WHERE tenant_id='11111111-1111-4111-8111-111111111111' AND source_check_fresh
)
SELECT n.news_id,n.title,n.body,n.url,n.content_release_id,n.tag_release_id,
       n.record_sha256,n.body_sha256,n.topic_ids,n.hot_score,n.report
FROM news_realtime.news_detail_current n JOIN latest l ON n.window_start=l.start_at
WHERE n.tenant_id='11111111-1111-4111-8111-111111111111'
ORDER BY n.hot_score DESC,n.news_id;

-- 5. Local simulation publication/replication latency and recorded model/Temporal trace identities.
-- Trace IDs and model_mode do not establish an enterprise model call or production acceptance.
SELECT run_id,tenant_id,revision,snapshot_id,source_published_at,projected_at,
       extract(epoch FROM projected_at-source_published_at) AS projection_seconds,
       report->>'trace_id' AS analysis_trace_id,report->>'workflow_id' AS workflow_id,
       report->>'model_mode' AS model_mode
FROM news_realtime.snapshot ORDER BY projected_at DESC LIMIT 20;

-- 6. Historical hourly audience uses its own frozen source/window: see news-audience-local-query.sql.
-- Minute click UV does not mean detail readers, and historical audience is never filled into live minutes.
