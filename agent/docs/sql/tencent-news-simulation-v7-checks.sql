-- Trusted operator checks for a new, isolated v7 database (not model-generated SQL).
\set ON_ERROR_STOP on
BEGIN READ ONLY;
DO $$
DECLARE bad bigint;
BEGIN
 SELECT count(*) INTO bad FROM news_sim_v7.news_metric_hourly m
 JOIN (
   SELECT tenant_id,news_id,event_time,snapshot_revision,
    sum(impressions) impressions,sum(clicks) clicks,sum(total_duration_seconds) duration,
    sum(effective_consumptions) effective,sum(interactions) interactions,
    sum(likes) likes,sum(comments) comments,sum(shares) shares,sum(favorites) favorites,
    sum(follows) follows,sum(negative_feedback) negative_feedback,
    sum(video_starts) video_starts,sum(video_completions) video_completions,
    min(unique_users) minimum_uv,max(unique_users) maximum_uv,sum(unique_users) sum_uv,count(*) channels
   FROM news_sim_v7.news_metric_channel_hourly GROUP BY tenant_id,news_id,event_time,snapshot_revision
 ) c USING (tenant_id,news_id,event_time,snapshot_revision)
 WHERE (m.impressions,m.clicks,m.total_duration_seconds,m.effective_consumptions,m.interactions,
  m.likes,m.comments,m.shares,m.favorites,m.follows,m.negative_feedback,m.video_starts,m.video_completions)
 IS DISTINCT FROM (c.impressions,c.clicks,c.duration,c.effective,c.interactions,
  c.likes,c.comments,c.shares,c.favorites,c.follows,c.negative_feedback,c.video_starts,c.video_completions)
 OR c.channels <> 3 OR m.unique_users < c.maximum_uv OR m.unique_users > c.sum_uv;
 IF bad <> 0 THEN RAISE EXCEPTION 'channel/hour reconciliation failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v7.news_metric_hourly m WHERE NOT EXISTS (
  SELECT 1 FROM news_sim_v7.news_metric_channel_hourly c WHERE
   (c.tenant_id,c.news_id,c.event_time,c.snapshot_revision) = (m.tenant_id,m.news_id,m.event_time,m.snapshot_revision));
 IF bad <> 0 THEN RAISE EXCEPTION 'missing channel rows: %',bad; END IF;

 SELECT count(*) INTO bad FROM (
  SELECT tenant_id,news_id,channel_id,date_trunc('hour',event_time) event_time,snapshot_revision,
   sum(impressions) impressions,sum(clicks) clicks,sum(total_duration_seconds) duration,
   sum(effective_consumptions) effective,sum(interactions) interactions,
   sum(likes) likes,sum(comments) comments,sum(shares) shares,sum(favorites) favorites,
   sum(follows) follows,sum(negative_feedback) negative_feedback,
   sum(video_starts) video_starts,sum(video_completions) video_completions,
   max(unique_users) maximum_uv,sum(unique_users) sum_uv
  FROM news_sim_v7.news_metric_5min GROUP BY tenant_id,news_id,channel_id,date_trunc('hour',event_time),snapshot_revision
 ) f LEFT JOIN news_sim_v7.news_metric_channel_hourly c USING (tenant_id,news_id,channel_id,event_time,snapshot_revision)
 WHERE c.news_id IS NULL OR (c.impressions,c.clicks,c.total_duration_seconds,c.effective_consumptions,c.interactions,
  c.likes,c.comments,c.shares,c.favorites,c.follows,c.negative_feedback,c.video_starts,c.video_completions)
 IS DISTINCT FROM (f.impressions,f.clicks,f.duration,f.effective,f.interactions,
  f.likes,f.comments,f.shares,f.favorites,f.follows,f.negative_feedback,f.video_starts,f.video_completions)
 OR c.unique_users < f.maximum_uv OR c.unique_users > f.sum_uv;
 IF bad <> 0 THEN RAISE EXCEPTION '5min/hour reconciliation failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v7.news_behavior_aggregate a
 JOIN news_sim_v7.dim_news n USING (tenant_id,news_id)
 JOIN news_sim_v7.latest_window_snapshot s USING (tenant_id,event_time)
 WHERE n.status <> 'published' OR s.completeness = 'incomplete' OR s.quality_status <> 'passed';
 IF bad <> 0 THEN RAISE EXCEPTION 'ineligible content/window leaked: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v7.news_metric_baseline_hourly b WHERE
  b.sample_count <> (SELECT count(*) FROM news_sim_v7.latest_window_snapshot s
   WHERE s.tenant_id = b.tenant_id AND s.event_time IN
    (b.event_time - interval '24 hours',b.event_time - interval '48 hours',b.event_time - interval '72 hours')
   AND s.completeness IN ('complete','corrected'));
 IF bad <> 0 THEN RAISE EXCEPTION 'baseline historical coverage failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v7.index_cleanup_task t
 JOIN news_sim_v7.content_change_event e USING (tenant_id,event_id,news_id)
 WHERE t.status <> 'pending' OR NOT t.mapping_required OR t.attempt_count <> 0
 OR t.lifecycle_revision <> e.lifecycle_revision
 OR t.through_version <> CASE WHEN e.event_type = 'correct' THEN e.from_version ELSE e.to_version END;
 IF bad <> 0 THEN RAISE EXCEPTION 'cleanup task scope failed: %',bad; END IF;

 SELECT count(*) INTO bad FROM news_sim_v7.content_change_event e WHERE
  (SELECT count(*) FROM news_sim_v7.index_cleanup_task t
   WHERE (t.tenant_id,t.event_id) = (e.tenant_id,e.event_id)) <> 4;
 IF bad <> 0 THEN RAISE EXCEPTION 'cleanup targets missing: %',bad; END IF;

 IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'news_sim_v7'
    AND table_name = 'news_behavior_aggregate') <> 18
 THEN RAISE EXCEPTION '18-column compatibility projection changed'; END IF;
END $$;

SELECT 'news dimensions' AS object,count(*) AS rows FROM news_sim_v7.dim_news
UNION ALL SELECT 'channel/hour',count(*) FROM news_sim_v7.news_metric_channel_hourly
UNION ALL SELECT 'news/hour',count(*) FROM news_sim_v7.news_metric_hourly
UNION ALL SELECT '5min',count(*) FROM news_sim_v7.news_metric_5min
UNION ALL SELECT 'baseline',count(*) FROM news_sim_v7.news_metric_baseline_hourly
UNION ALL SELECT 'eligible complete aggregate',count(*) FROM news_sim_v7.news_behavior_aggregate
UNION ALL SELECT 'pending cleanup tasks',count(*) FROM news_sim_v7.index_cleanup_task;

SELECT tenant_id,completeness,count(*) AS windows FROM news_sim_v7.latest_window_snapshot
GROUP BY tenant_id,completeness ORDER BY tenant_id,completeness;
COMMIT;
