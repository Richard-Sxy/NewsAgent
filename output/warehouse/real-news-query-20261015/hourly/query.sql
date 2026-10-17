WITH latest AS (
 SELECT * FROM news_sim_v8.window_snapshot
 WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
 ORDER BY snapshot_revision DESC LIMIT 1
)
SELECT n.tenant_id,n.news_id,n.title,n.category,n.source_url,n.publish_time,n.content_sha256,m.event_time,m.snapshot_revision,m.impressions,m.clicks,m.unique_users,m.detail_page_views,m.direct_page_views,
 m.detail_visitors,m.total_duration_seconds,m.effective_consumptions,m.interactions,
 m.likes,m.comments,m.shares,m.favorites,m.negative_feedback,
 m.clicks::numeric/NULLIF(m.impressions,0) AS ctr,
 m.uv_method,w.watermark_at,w.snapshot_at,w.completeness,w.quality_status,
 m.total_duration_seconds::numeric/NULLIF(m.detail_page_views,0) AS average_duration_per_detail_view
FROM news_sim_v8.news_metric_hourly m
JOIN latest w ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time
 AND w.snapshot_revision=m.snapshot_revision
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
WHERE w.completeness IN ('complete','corrected') AND w.quality_status='passed'
 AND w.watermark_at>=w.window_end AND n.news_id=%(news_id)s LIMIT 1;
