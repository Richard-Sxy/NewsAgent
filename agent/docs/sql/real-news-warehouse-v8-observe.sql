-- Bounded evidence after the versioned reconciliation checks have passed.
\set ON_ERROR_STOP on
BEGIN READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SET LOCAL statement_timeout = '120s';
SELECT jsonb_build_object(
 'counts', jsonb_build_object(
  'dim_behavior_policy',(SELECT count(*) FROM news_sim_v8.dim_behavior_policy),
  'dim_channel',(SELECT count(*) FROM news_sim_v8.dim_channel),
  'dim_news',(SELECT count(*) FROM news_sim_v8.dim_news),
  'window_snapshot',(SELECT count(*) FROM news_sim_v8.window_snapshot),
  'news_metric_5min',(SELECT count(*) FROM news_sim_v8.news_metric_5min),
  'news_metric_channel_hourly',(SELECT count(*) FROM news_sim_v8.news_metric_channel_hourly),
  'news_metric_hourly',(SELECT count(*) FROM news_sim_v8.news_metric_hourly),
  'news_metric_daily',(SELECT count(*) FROM news_sim_v8.news_metric_daily),
  'news_metric_baseline_hourly',(SELECT count(*) FROM news_sim_v8.news_metric_baseline_hourly),
  'content_change_event',(SELECT count(*) FROM news_sim_v8.content_change_event),
  'content_tombstone',(SELECT count(*) FROM news_sim_v8.content_tombstone),
  'index_cleanup_task',(SELECT count(*) FROM news_sim_v8.index_cleanup_task)
 ),
 'reference_metrics',(SELECT to_jsonb(sample) FROM (
  SELECT news_id,title,source_url AS url,publish_time,event_time,snapshot_revision,
   impressions,clicks,unique_users,detail_page_views,direct_page_views,detail_visitors,
   total_duration_seconds,effective_consumptions,interactions,ctr,
   sample_count,baseline_version,completeness,quality_status,watermark_at,snapshot_at
  FROM news_sim_v8.news_operational_hourly
  WHERE tenant_id='11111111-1111-4111-8111-111111111111'
   AND news_id='20260903A05YQE00'
   AND completeness IN ('complete','corrected') AND quality_status='passed'
  ORDER BY event_time DESC LIMIT 1
 ) sample),
 'queryable_hours',(SELECT jsonb_agg(to_jsonb(status_counts)) FROM (
  SELECT tenant_id,completeness,quality_status,count(*) AS hours
  FROM news_sim_v8.latest_window_snapshot
  GROUP BY tenant_id,completeness,quality_status ORDER BY tenant_id,completeness
 ) status_counts),
 'compatibility_view_columns',(SELECT jsonb_agg(column_name ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='news_sim_v8' AND table_name='news_behavior_aggregate'),
 'database_bytes',pg_database_size(current_database()),
 'table_count',(SELECT count(*) FROM information_schema.tables WHERE table_schema='news_sim_v8' AND table_type='BASE TABLE'),
 'view_count',(SELECT count(*) FROM information_schema.views WHERE table_schema='news_sim_v8'),
 'missing_baseline_rows',(SELECT count(*) FROM news_sim_v8.news_metric_baseline_hourly WHERE sample_count=0),
 'partial_daily_rows',(SELECT count(*) FROM news_sim_v8.news_metric_daily WHERE completeness='partial'),
 'pending_cleanup_tasks',(SELECT count(*) FROM news_sim_v8.index_cleanup_task WHERE status='pending')
);
COMMIT;
