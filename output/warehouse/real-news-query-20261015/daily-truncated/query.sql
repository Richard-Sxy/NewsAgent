SELECT n.tenant_id,n.news_id,n.title,n.category,n.source_url,n.publish_time,n.content_sha256,m.stat_date,m.impressions,m.clicks,m.unique_users,m.detail_page_views,m.direct_page_views,
 m.detail_visitors,m.total_duration_seconds,m.effective_consumptions,m.interactions,
 m.likes,m.comments,m.shares,m.favorites,m.negative_feedback,
 m.clicks::numeric/NULLIF(m.impressions,0) AS ctr,m.coverage_start,m.coverage_end,
 count(*) OVER () AS matched_rows,
 m.observed_hours,m.excluded_hours,m.completeness,m.snapshot_revision,m.uv_method
FROM news_sim_v8.news_metric_daily m
JOIN news_sim_v8.eligible_news n ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
WHERE m.tenant_id=%(tenant_id)s::uuid AND n.news_id=%(news_id)s
 AND m.stat_date BETWEEN %(start_date)s::date AND %(end_date)s::date
ORDER BY m.stat_date ASC LIMIT %(limit)s;
