-- Operator-owned, aggregate-only v8 validation. This does not connect the Agent.
-- Run after warehouse.sql in a new isolated PostgreSQL 16 database.
-- No mutation, extensions, network calls, vector deletion or user event storage.
-- Scenario-coverage assertions target the full default fixture, not tiny injected
-- catalogs used by Python unit tests.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';

DO $$
DECLARE bad bigint;
BEGIN
 -- Real cached content and simulated behavior are separate provenance claims.
 SELECT count(*) INTO bad FROM news_sim_v8.dim_news
 WHERE news_content_synthetic OR NOT metric_data_synthetic
  OR NOT category_data_synthetic OR NOT lifecycle_data_synthetic
  OR content_type <> 'article' OR title LIKE '【合成】%'
  OR length(content) = 0 OR source_url <> 'https://news.qq.com/rain/a/' || news_id
  OR content_sha256 !~ '^[0-9a-f]{64}$' OR source_sha256 !~ '^[0-9a-f]{64}$'
  OR source_manifest_sha256 IS NULL OR source_manifest_sha256 !~ '^[0-9a-f]{64}$';
 IF bad <> 0 THEN RAISE EXCEPTION 'real-content/simulated-metric provenance failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.dim_news
 WHERE tenant_id NOT IN ('11111111-1111-4111-8111-111111111111'::uuid,
                        '33333333-3333-4333-8333-333333333333'::uuid);
 IF bad <> 0 THEN RAISE EXCEPTION 'unexpected tenant in real catalog: %',bad; END IF;

 SELECT count(*) INTO bad FROM (
  SELECT news_id FROM news_sim_v8.dim_news GROUP BY news_id
  HAVING count(*) <> 2 OR count(DISTINCT (title,content,source_url,publish_time,
   source_content_version,content_sha256,source_sha256,source_manifest_sha256)) <> 1
 ) inconsistent_catalog;
 IF bad <> 0 THEN RAISE EXCEPTION 'tenant catalog identity/provenance mismatch: %',bad; END IF;

 IF NOT EXISTS (SELECT 1 FROM news_sim_v8.dim_news) THEN
  RAISE EXCEPTION 'real news catalog is empty';
 END IF;

 -- The main tenant never claims real withdrawal/correction based on a simulation.
 SELECT count(*) INTO bad FROM news_sim_v8.dim_news
 WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
  AND (status <> 'published' OR lifecycle_scenario <> 'none'
       OR content_version <> source_content_version OR fixture_content_version <> content_version
       OR lifecycle_revision <> 1);
 IF bad <> 0 THEN RAISE EXCEPTION 'main tenant fabricated a real lifecycle claim: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.content_change_event
 WHERE tenant_id <> '33333333-3333-4333-8333-333333333333'
  OR NOT lifecycle_data_synthetic OR reason_code NOT LIKE 'isolated_simulated_%';
 IF bad <> 0 THEN RAISE EXCEPTION 'lifecycle simulation escaped isolated tenant: %',bad; END IF;

 -- A publication can be inside the first aggregate bin; only its remaining
 -- post-publication seconds are modeled by the generator. Earlier full bins
 -- and hours must not exist (zero rows are not fabricated historical samples).
 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_5min m
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE m.event_time + interval '5 minutes' <= n.publish_time;
 IF bad <> 0 THEN RAISE EXCEPTION 'prepublication five-minute bins exist: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_5min m
 LEFT JOIN news_sim_v8.window_snapshot s
  ON (s.tenant_id,s.event_time,s.snapshot_revision) =
     (m.tenant_id,date_trunc('hour',m.event_time),m.snapshot_revision)
 WHERE s.tenant_id IS NULL OR m.event_time + interval '5 minutes' > s.snapshot_at;
 IF bad <> 0 THEN RAISE EXCEPTION 'five-minute rows lack a parent or include future observations: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_5min
 WHERE effective_consumptions > detail_page_views - direct_page_views;
 IF bad <> 0 THEN RAISE EXCEPTION 'click-attributed effective reads include direct-only visits: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_hourly m
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE m.event_time + interval '1 hour' <= n.publish_time;
 IF bad <> 0 THEN RAISE EXCEPTION 'prepublication hourly rows exist: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_baseline_hourly b
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE b.event_time + interval '1 hour' <= n.publish_time;
 IF bad <> 0 THEN RAISE EXCEPTION 'prepublication baseline targets exist: %',bad; END IF;

 -- Snapshot selection precedes completeness filtering. Never reuse an older
 -- complete revision when the latest revision is incomplete.
 SELECT count(*) INTO bad FROM news_sim_v8.latest_window_snapshot s
 WHERE s.snapshot_revision <> (SELECT max(w.snapshot_revision)
  FROM news_sim_v8.window_snapshot w
  WHERE (w.tenant_id,w.event_time) = (s.tenant_id,s.event_time));
 IF bad <> 0 THEN RAISE EXCEPTION 'latest-window selection is not latest-first: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_behavior_aggregate a
 JOIN news_sim_v8.latest_window_snapshot s USING (tenant_id,event_time)
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE s.completeness NOT IN ('complete','corrected') OR s.quality_status <> 'passed'
  OR n.status <> 'published' OR EXISTS (
   SELECT 1 FROM news_sim_v8.content_tombstone t
   WHERE (t.tenant_id,t.news_id) = (a.tenant_id,a.news_id));
 IF bad <> 0 THEN RAISE EXCEPTION 'incomplete or blocked content leaked into authoritative aggregate: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.latest_window_snapshot s
 WHERE s.completeness = 'incomplete' AND EXISTS (
  SELECT 1 FROM news_sim_v8.news_behavior_aggregate a
  WHERE (a.tenant_id,a.event_time) = (s.tenant_id,s.event_time));
 IF bad <> 0 THEN RAISE EXCEPTION 'latest incomplete window fell back to old complete rows: %',bad; END IF;

 -- Verify every projected value and its multiplicity, not only the window
 -- labels: both omitted valid rows and duplicate/older revisions must fail.
 WITH expected AS (
  SELECT n.tenant_id,n.news_id,m.event_time,n.title,n.content_type,n.category,n.source,n.publish_time,
   m.impressions,m.clicks,m.unique_users,m.total_duration_seconds,m.effective_consumptions,m.interactions,
   b.baseline_impressions,b.baseline_clicks,b.baseline_effective_consumptions,b.baseline_interactions
  FROM news_sim_v8.dim_news n JOIN news_sim_v8.news_metric_hourly m USING (tenant_id,news_id)
  JOIN news_sim_v8.latest_window_snapshot s USING (tenant_id,event_time,snapshot_revision)
  LEFT JOIN news_sim_v8.news_metric_baseline_hourly b USING (tenant_id,news_id,event_time)
  WHERE n.status = 'published' AND s.completeness IN ('complete','corrected')
   AND s.quality_status = 'passed' AND NOT EXISTS (
    SELECT 1 FROM news_sim_v8.content_tombstone t
    WHERE (t.tenant_id,t.news_id) = (n.tenant_id,n.news_id))
 ), mismatch AS (
  (SELECT * FROM news_sim_v8.news_behavior_aggregate EXCEPT ALL SELECT * FROM expected)
  UNION ALL
  (SELECT * FROM expected EXCEPT ALL SELECT * FROM news_sim_v8.news_behavior_aggregate)
 ) SELECT count(*) INTO bad FROM mismatch;
 IF bad <> 0 THEN RAISE EXCEPTION 'authoritative projection lost, duplicated or used stale values: %',bad; END IF;

 IF NOT EXISTS (
  SELECT 1 FROM news_sim_v8.latest_window_snapshot s
  WHERE s.completeness='incomplete' AND EXISTS (
   SELECT 1 FROM news_sim_v8.window_snapshot old
   WHERE (old.tenant_id,old.event_time)=(s.tenant_id,s.event_time)
    AND old.snapshot_revision<s.snapshot_revision
    AND old.completeness IN ('complete','corrected') AND old.quality_status='passed')
 ) THEN RAISE EXCEPTION 'latest-incomplete-over-old-complete scenario is missing'; END IF;

 -- All additive metrics reconcile independently; UV is checked with union
 -- bounds rather than summed across bins/channels/hours.
 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_hourly m
 FULL JOIN (
  SELECT tenant_id,news_id,event_time,snapshot_revision,count(*) AS channel_count,
   sum(impressions) AS impressions,sum(clicks) AS clicks,
   sum(detail_page_views) AS detail_page_views,sum(direct_page_views) AS direct_page_views,
   sum(total_duration_seconds) AS total_duration_seconds,
   sum(effective_consumptions) AS effective_consumptions,sum(interactions) AS interactions,
   sum(likes) AS likes,sum(comments) AS comments,sum(shares) AS shares,
   sum(favorites) AS favorites,sum(negative_feedback) AS negative_feedback,
   max(unique_users) AS max_click_uv,sum(unique_users) AS sum_click_uv,
   max(detail_visitors) AS max_visit_uv,sum(detail_visitors) AS sum_visit_uv
  FROM news_sim_v8.news_metric_channel_hourly
  GROUP BY tenant_id,news_id,event_time,snapshot_revision
 ) c USING (tenant_id,news_id,event_time,snapshot_revision)
 WHERE m.news_id IS NULL OR c.news_id IS NULL OR c.channel_count <> 3
  OR (m.impressions,m.clicks,m.detail_page_views,m.direct_page_views,m.total_duration_seconds,
      m.effective_consumptions,m.interactions,m.likes,m.comments,m.shares,m.favorites,m.negative_feedback)
  IS DISTINCT FROM
     (c.impressions,c.clicks,c.detail_page_views,c.direct_page_views,c.total_duration_seconds,
      c.effective_consumptions,c.interactions,c.likes,c.comments,c.shares,c.favorites,c.negative_feedback)
  OR m.unique_users NOT BETWEEN c.max_click_uv AND c.sum_click_uv
  OR m.detail_visitors NOT BETWEEN c.max_visit_uv AND c.sum_visit_uv;
 IF bad <> 0 THEN RAISE EXCEPTION 'channel-to-hour conservation or UV union bounds failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_hourly m
 FULL JOIN (
  SELECT tenant_id,news_id,date_trunc('hour',event_time) AS event_time,snapshot_revision,
   sum(impressions) AS impressions,sum(clicks) AS clicks,
   sum(detail_page_views) AS detail_page_views,sum(direct_page_views) AS direct_page_views,
   sum(total_duration_seconds) AS total_duration_seconds,
   sum(effective_consumptions) AS effective_consumptions,sum(interactions) AS interactions,
   sum(likes) AS likes,sum(comments) AS comments,sum(shares) AS shares,
   sum(favorites) AS favorites,sum(negative_feedback) AS negative_feedback,
   max(unique_users) AS max_click_uv,sum(unique_users) AS sum_click_uv,
   max(detail_visitors) AS max_visit_uv,sum(detail_visitors) AS sum_visit_uv
  FROM news_sim_v8.news_metric_5min
  GROUP BY tenant_id,news_id,date_trunc('hour',event_time),snapshot_revision
 ) f USING (tenant_id,news_id,event_time,snapshot_revision)
 WHERE m.news_id IS NULL OR f.news_id IS NULL
  OR (m.impressions,m.clicks,m.detail_page_views,m.direct_page_views,m.total_duration_seconds,
      m.effective_consumptions,m.interactions,m.likes,m.comments,m.shares,m.favorites,m.negative_feedback)
  IS DISTINCT FROM
     (f.impressions,f.clicks,f.detail_page_views,f.direct_page_views,f.total_duration_seconds,
      f.effective_consumptions,f.interactions,f.likes,f.comments,f.shares,f.favorites,f.negative_feedback)
  OR m.unique_users NOT BETWEEN f.max_click_uv AND f.sum_click_uv
  OR m.detail_visitors NOT BETWEEN f.max_visit_uv AND f.sum_visit_uv;
 IF bad <> 0 THEN RAISE EXCEPTION 'five-minute-to-hour conservation or UV union bounds failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_daily d
 FULL JOIN (
  SELECT m.tenant_id,m.news_id,m.event_time::date AS stat_date,count(*) AS observed_hours,
   sum(m.impressions) AS impressions,sum(m.clicks) AS clicks,
   sum(m.detail_page_views) AS detail_page_views,sum(m.direct_page_views) AS direct_page_views,
   sum(m.total_duration_seconds) AS total_duration_seconds,
   sum(m.effective_consumptions) AS effective_consumptions,sum(m.interactions) AS interactions,
   sum(m.likes) AS likes,sum(m.comments) AS comments,sum(m.shares) AS shares,
   sum(m.favorites) AS favorites,sum(m.negative_feedback) AS negative_feedback,
   max(m.unique_users) AS max_click_uv,sum(m.unique_users) AS sum_click_uv,
   max(m.detail_visitors) AS max_visit_uv,sum(m.detail_visitors) AS sum_visit_uv
  FROM news_sim_v8.news_metric_hourly m
  JOIN news_sim_v8.latest_window_snapshot s USING (tenant_id,event_time,snapshot_revision)
  WHERE s.completeness IN ('complete','corrected') AND s.quality_status = 'passed'
  GROUP BY m.tenant_id,m.news_id,m.event_time::date
 ) h USING (tenant_id,news_id,stat_date)
 WHERE d.news_id IS NULL OR h.news_id IS NULL OR d.observed_hours <> h.observed_hours
  OR (d.impressions,d.clicks,d.detail_page_views,d.direct_page_views,d.total_duration_seconds,
      d.effective_consumptions,d.interactions,d.likes,d.comments,d.shares,d.favorites,d.negative_feedback)
  IS DISTINCT FROM
     (h.impressions,h.clicks,h.detail_page_views,h.direct_page_views,h.total_duration_seconds,
      h.effective_consumptions,h.interactions,h.likes,h.comments,h.shares,h.favorites,h.negative_feedback)
  OR d.unique_users NOT BETWEEN h.max_click_uv AND h.sum_click_uv
  OR d.detail_visitors NOT BETWEEN h.max_visit_uv AND h.sum_visit_uv
  OR (d.excluded_hours > 0 AND d.completeness = 'complete');
 IF bad <> 0 THEN RAISE EXCEPTION 'latest-valid-hour-to-day conservation, coverage or UV bounds failed: %',bad; END IF;

 -- Missing historical coverage stays NULL; real zeros remain valid samples.
 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_baseline_hourly b
 LEFT JOIN LATERAL (
  SELECT count(*) AS samples,round(avg(m.impressions),2) AS impressions,
   round(avg(m.clicks),2) AS clicks,round(avg(m.effective_consumptions),2) AS effective_consumptions,
   round(avg(m.interactions),2) AS interactions
  FROM news_sim_v8.news_metric_hourly m
  JOIN news_sim_v8.latest_window_snapshot s USING (tenant_id,event_time,snapshot_revision)
  WHERE (m.tenant_id,m.news_id) = (b.tenant_id,b.news_id)
   AND m.event_time IN (b.event_time - interval '24 hours',b.event_time - interval '48 hours',
                       b.event_time - interval '72 hours')
   AND s.completeness IN ('complete','corrected') AND s.quality_status = 'passed'
 ) h ON true
 WHERE b.sample_count <> h.samples
  OR (b.baseline_impressions,b.baseline_clicks,b.baseline_effective_consumptions,b.baseline_interactions)
  IS DISTINCT FROM (h.impressions,h.clicks,h.effective_consumptions,h.interactions);
 IF bad <> 0 THEN RAISE EXCEPTION 'article-specific baseline coverage, NULLs or values failed: %',bad; END IF;

 -- The fixture should contain overlapping audience evidence, not silently
 -- implement UV by adding the per-channel or per-hour UV numbers.
 IF NOT EXISTS (
  SELECT 1 FROM news_sim_v8.news_metric_hourly m JOIN (
   SELECT tenant_id,news_id,event_time,snapshot_revision,sum(unique_users) AS click_uv,
    sum(detail_visitors) AS visit_uv FROM news_sim_v8.news_metric_channel_hourly
   GROUP BY tenant_id,news_id,event_time,snapshot_revision
  ) c USING (tenant_id,news_id,event_time,snapshot_revision)
  WHERE m.unique_users < c.click_uv AND m.detail_visitors < c.visit_uv
 ) THEN RAISE EXCEPTION 'no cross-channel overlapping-audience scenario was generated'; END IF;

 IF NOT EXISTS (SELECT 1 FROM news_sim_v8.news_metric_hourly
  WHERE clicks = 0 AND direct_page_views > 0 AND detail_page_views > 0
   AND detail_visitors > 0 AND unique_users = 0)
 THEN RAISE EXCEPTION 'no direct-access-without-click scenario was generated'; END IF;

 IF NOT EXISTS (SELECT 1 FROM news_sim_v8.news_operational_hourly
  WHERE impressions = 0 AND clicks = 0 AND ctr IS NULL AND direct_page_views > 0)
 THEN RAISE EXCEPTION 'undefined CTR was replaced by zero or its scenario is missing'; END IF;

 -- Cleanup is a pending simulated projection task; no external adapter runs.
 SELECT count(*) INTO bad FROM news_sim_v8.index_cleanup_task t
 LEFT JOIN news_sim_v8.content_change_event e USING (tenant_id,event_id,news_id)
 WHERE t.tenant_id <> '33333333-3333-4333-8333-333333333333'
  OR e.event_id IS NULL OR t.status <> 'pending' OR t.attempt_count <> 0 OR NOT t.mapping_required
  OR NOT t.lifecycle_data_synthetic
  OR t.through_version <> CASE WHEN e.event_type = 'correct' THEN e.from_version ELSE e.to_version END;
 IF bad <> 0 THEN RAISE EXCEPTION 'cleanup simulated scope/version/pending state failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.content_tombstone t
 LEFT JOIN news_sim_v8.content_change_event e USING (tenant_id,event_id,news_id)
 WHERE e.event_id IS NULL OR e.event_type = 'correct'
  OR t.blocked_through_version <> e.to_version OR t.lifecycle_revision <> e.lifecycle_revision
  OR t.status <> e.new_status OR t.effective_at <> e.effective_at
  OR t.tenant_id <> '33333333-3333-4333-8333-333333333333' OR NOT t.lifecycle_data_synthetic;
 IF bad <> 0 THEN RAISE EXCEPTION 'tombstone event/news/version binding failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.content_change_event e
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE e.event_type <> n.lifecycle_scenario OR e.new_status <> n.status
  OR e.to_version <> n.fixture_content_version OR e.from_version <> n.content_version
  OR e.lifecycle_revision <> n.lifecycle_revision;
 IF bad <> 0 THEN RAISE EXCEPTION 'simulated lifecycle changed real content version or binding: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.news_operational_hourly
 WHERE ctr IS DISTINCT FROM clicks::numeric / NULLIF(impressions,0)
  OR click_attributed_effective_rate IS DISTINCT FROM effective_consumptions::numeric / NULLIF(clicks,0)
  OR average_duration_per_detail_view IS DISTINCT FROM total_duration_seconds::numeric / NULLIF(detail_page_views,0);
 IF bad <> 0 THEN RAISE EXCEPTION 'operational ratio denominator/NULL semantics failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.content_change_event e
 WHERE (SELECT array_agg(t.target ORDER BY t.target) FROM news_sim_v8.index_cleanup_task t
  WHERE (t.tenant_id,t.event_id,t.news_id) = (e.tenant_id,e.event_id,e.news_id))
  IS DISTINCT FROM ARRAY['bge_snapshot','milvus','postgres_knowledge','retrieval_cache'];
 IF bad <> 0 THEN RAISE EXCEPTION 'cleanup target mapping failed: %',bad; END IF;

 IF (SELECT count(*) FROM information_schema.columns
  WHERE table_schema = 'news_sim_v8' AND table_name = 'news_behavior_aggregate') <> 18
 THEN RAISE EXCEPTION '18-column future compatibility projection changed'; END IF;

 SELECT count(*) INTO bad FROM information_schema.columns
 WHERE table_schema = 'news_sim_v8'
  AND column_name IN ('user_id','device_id','session_id','view_id','client_ip','ip_address','cookie_id');
 IF bad <> 0 THEN RAISE EXCEPTION 'raw behavior identity fields were persisted: %',bad; END IF;

 -- Content-only real articles must not claim behavior evidence.
 SELECT count(*) INTO bad FROM news_sim_v8.news_metric_hourly m
 JOIN news_sim_v8.dim_news n USING (tenant_id,news_id)
 WHERE NOT n.metric_available;
 IF bad <> 0 THEN RAISE EXCEPTION 'content-only articles have behavior metrics: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v8.dim_news n
 LEFT JOIN news_sim_v8.news_content_feature f USING (tenant_id,news_id)
 WHERE f.news_id IS NULL OR f.content_sha256 <> n.content_sha256
  OR f.metric_available <> n.metric_available;
 IF bad <> 0 THEN RAISE EXCEPTION 'content-only feature rows are missing or inconsistent: %',bad; END IF;

 -- Revision identity and same-tenant parent bindings are database contracts,
 -- not just assumptions made by the generator's deterministic replay.
 IF (SELECT count(*) FROM pg_constraint c
   JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace s ON s.oid=r.relnamespace
   WHERE s.nspname='news_sim_v8' AND r.relkind='r' AND c.contype='p') <> 13
 THEN RAISE EXCEPTION 'one or more fixture tables lack a replay-safe primary key'; END IF;
END $$;

-- One exact JSON evidence object for automated capture; counts never use estimates.
SELECT jsonb_build_object(
 'schema','news_sim_v8',
 'behavior_policy_rows',(SELECT count(*) FROM news_sim_v8.dim_behavior_policy),
 'channel_dimension_rows',(SELECT count(*) FROM news_sim_v8.dim_channel),
 'real_news_count',(SELECT count(DISTINCT news_id) FROM news_sim_v8.dim_news),
 'dimension_rows',(SELECT count(*) FROM news_sim_v8.dim_news),
 'content_feature_rows',(SELECT count(*) FROM news_sim_v8.news_content_feature),
 'metric_available_dimensions',(SELECT count(*) FROM news_sim_v8.dim_news WHERE metric_available),
 'content_only_dimensions',(SELECT count(*) FROM news_sim_v8.dim_news WHERE NOT metric_available),
 'five_minute_rows',(SELECT count(*) FROM news_sim_v8.news_metric_5min),
 'channel_hour_rows',(SELECT count(*) FROM news_sim_v8.news_metric_channel_hourly),
 'hour_rows',(SELECT count(*) FROM news_sim_v8.news_metric_hourly),
 'daily_rows',(SELECT count(*) FROM news_sim_v8.news_metric_daily),
 'baseline_rows',(SELECT count(*) FROM news_sim_v8.news_metric_baseline_hourly),
 'window_revisions',(SELECT count(*) FROM news_sim_v8.window_snapshot),
 'latest_windows',(SELECT count(*) FROM news_sim_v8.latest_window_snapshot),
 'eligible_complete_rows',(SELECT count(*) FROM news_sim_v8.news_behavior_aggregate),
 'simulated_lifecycle_events',(SELECT count(*) FROM news_sim_v8.content_change_event),
 'simulated_tombstones',(SELECT count(*) FROM news_sim_v8.content_tombstone),
 'pending_cleanup_tasks',(SELECT count(*) FROM news_sim_v8.index_cleanup_task),
 'missing_baseline_rows',(SELECT count(*) FROM news_sim_v8.news_metric_baseline_hourly WHERE sample_count=0),
 'undefined_ctr_rows',(SELECT count(*) FROM news_sim_v8.news_metric_hourly WHERE impressions=0),
 'duplicate_hour_revision_groups',(SELECT count(*) FROM (
   SELECT tenant_id,news_id,event_time,snapshot_revision FROM news_sim_v8.news_metric_hourly
   GROUP BY tenant_id,news_id,event_time,snapshot_revision HAVING count(*)>1) duplicates),
 'cross_tenant_join_contract','composite tenant_id/news_id keys; Agent/RLS integration not yet implemented',
 'cleanup_execution','none; pending tasks only'
) AS validation_counts;

SELECT tenant_id,completeness,quality_status,count(*) AS latest_windows
FROM news_sim_v8.latest_window_snapshot GROUP BY tenant_id,completeness,quality_status
ORDER BY tenant_id,completeness,quality_status;
COMMIT;
