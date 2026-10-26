-- Dynamic producer progress is separate from immutable warehouse/report facts.
CREATE TABLE stream.dynamic_feed (
  id integer PRIMARY KEY CHECK(id=1), enabled boolean NOT NULL DEFAULT false,
  interval_seconds integer NOT NULL DEFAULT 60 CHECK(interval_seconds BETWEEN 30 AND 3600),
  max_windows integer NOT NULL DEFAULT 1440 CHECK(max_windows BETWEEN 1 AND 1440),
  sequence bigint NOT NULL DEFAULT 0, last_error text,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO stream.dynamic_feed(id) VALUES(1);
CREATE TABLE stream.dynamic_batches (
  sequence bigint PRIMARY KEY, run_id text UNIQUE NOT NULL,
  window_start_ms bigint UNIQUE NOT NULL, generator_version text NOT NULL,
  scenario text NOT NULL, packet_sha256 text NOT NULL,
  event_count integer NOT NULL,
  state text NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','data_sent','marker_sent','completed')),
  data_attempts integer NOT NULL DEFAULT 0 CHECK(data_attempts BETWEEN 0 AND 2),
  marker_attempts integer NOT NULL DEFAULT 0 CHECK(marker_attempts BETWEEN 0 AND 2),
  source_trace_id text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz
);
-- Only aggregates, not individual synthetic events, need to be retained here.
CREATE VIEW stream.news_metric_current AS
SELECT s.tenant_id,s.run_id,s.window_start_ms,s.window_end_ms,s.snapshot_id,s.revision,s.final,
       m->>'news_id' AS news_id,m->>'content_type' AS content_type,
       (m->>'impressions')::bigint AS impressions,(m->>'clicks')::bigint AS clicks,
       (m->>'unique_users')::bigint AS unique_users,
       (m->>'total_duration_seconds')::bigint AS total_duration_seconds,
       (m->>'effective_consumptions')::bigint AS effective_consumptions,
       (m->>'interactions')::bigint AS interactions,s.published_at
FROM stream.window_heads h JOIN stream.snapshots s ON s.snapshot_id=h.snapshot_id
CROSS JOIN LATERAL jsonb_array_elements(s.payload_text::jsonb->'metrics') m
WHERE NOT EXISTS(SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=s.window_key);
