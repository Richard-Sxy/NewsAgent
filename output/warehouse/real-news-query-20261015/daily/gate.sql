SELECT count(*) AS observed_windows,
 count(*) FILTER (WHERE completeness IN ('complete','corrected') AND quality_status='passed'
   AND watermark_at>=window_end) AS passed_windows,
 count(*) FILTER (WHERE completeness NOT IN ('complete','corrected') OR quality_status<>'passed'
   OR watermark_at<window_end) AS excluded_windows,
 min(event_time) AS earliest_window,max(event_time) AS latest_window
FROM news_sim_v8.latest_window_snapshot
WHERE tenant_id=%(tenant_id)s::uuid
 AND event_time >= %(start_date)s::date::timestamp AT TIME ZONE 'Asia/Shanghai'
 AND event_time < (%(end_date)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai';
