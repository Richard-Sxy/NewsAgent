SELECT tenant_id,event_time,snapshot_revision,window_end,watermark_at,snapshot_at,
 completeness,quality_status,policy_version
FROM news_sim_v8.window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid AND event_time=%(hour)s::timestamptz
ORDER BY snapshot_revision DESC LIMIT 1;
