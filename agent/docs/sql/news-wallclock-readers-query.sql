-- DBeaver: NewsAgent Unified News + Audience
-- 127.0.0.1:25432 / newsagent_unified_news_20261023
-- Read-only. All readers are synthetic; news content is locally cached public text.
BEGIN READ ONLY;

-- 1. Latest stored wall-clock simulation windows; rerun the query to refresh (for example every 5s).
-- Results can be historical: inspect window_start, final and source_check_fresh; this SQL does not poll.
SELECT window_start,window_end,title,impressions,clicks,unique_users,
       reader_count,revision,final,reader_profile_status,watermark,source_check_fresh,
       projected_at,latest_event_to_sql_seconds
FROM news_realtime.news_live_current
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
ORDER BY window_start DESC,news_id LIMIT 12;

-- 2. Long/short interest populations. Overlapping topics and window UV must not be summed.
SELECT window_start,title,lookback_hours,reader_count,profiled_reader_count,
       unknown_reader_count,cold_start_reader_count,topic_id,topic_reader_count,
       share_all,share_profiled,baseline_share,lift,low_sample,final
FROM news_realtime.reader_interest_current
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
ORDER BY window_start DESC,news_id,lookback_hours,topic_reader_count DESC LIMIT 120;

-- 3. Immutable audience evidence. This table contains aggregate JSON, never user IDs.
SELECT tenant_id,run_id,to_timestamp(start_ms/1000.0) AS window_start,
       revision,to_timestamp(watermark_ms/1000.0) AS watermark,generated_at,
       source_release_sha256,identity_sha256,items
FROM news_realtime.reader_final
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
ORDER BY start_ms DESC LIMIT 3;

-- 4. Stored results from the isolated simulation hotspot Agent and deterministic scoring chain.
-- A matching event_time_mode does not establish current service health or enterprise acceptance.
SELECT window_start,title,hot_score,event_time_mode,reader_profile_status,
       report->>'production_bundle_version' AS bundle,
       report->>'reader_profile_context_sha256' AS reader_context,
       report->>'trace_id' AS trace_id
FROM news_realtime.news_detail_current
WHERE tenant_id='11111111-1111-4111-8111-111111111111' AND event_time_mode='wall_clock'
ORDER BY window_start DESC,hot_score DESC LIMIT 9;
COMMIT;
