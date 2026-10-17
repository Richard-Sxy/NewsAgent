SELECT tenant_id,event_time,snapshot_revision,window_end,watermark_at,snapshot_at,
 completeness,quality_status,policy_version,
 (snapshot_revision=max(snapshot_revision) OVER ()) AS is_latest,
 count(*) OVER () AS matched_rows,
 TRUE AS diagnostic_only
FROM news_sim_v8.window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
ORDER BY snapshot_revision DESC LIMIT 4;
