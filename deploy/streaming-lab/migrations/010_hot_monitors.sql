-- Durable, opt-in discovery over complete synthetic minute snapshots.
CREATE TABLE stream.hot_monitors (
  monitor_id text PRIMARY KEY CHECK (monitor_id ~ '^[a-f0-9]{64}$'),
  tenant_id text NOT NULL,
  session_id text NOT NULL REFERENCES stream.wallclock_session(id),
  window_minutes integer NOT NULL CHECK (window_minutes IN (5,15)),
  policy_version text NOT NULL,
  initial_start_ms bigint NOT NULL CHECK (initial_start_ms > 0 AND initial_start_ms % 60000 = 0),
  next_start_ms bigint NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  status text NOT NULL DEFAULT 'waiting_source'
    CHECK (status IN ('waiting_source','published','paused','blocked','backpressure')),
  last_snapshot_id text REFERENCES stream.snapshots(snapshot_id),
  source_end_ms bigint,
  error_type text,
  checked_at timestamptz,
  next_poll_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id,session_id,window_minutes,policy_version),
  CHECK (next_start_ms >= initial_start_ms AND next_start_ms % 60000 = 0)
);
CREATE TABLE stream.hot_monitor_windows (
  monitor_id text NOT NULL REFERENCES stream.hot_monitors(monitor_id),
  window_start_ms bigint NOT NULL,
  snapshot_id text NOT NULL REFERENCES stream.snapshots(snapshot_id),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (monitor_id,window_start_ms)
);
CREATE INDEX hot_monitors_due ON stream.hot_monitors(next_poll_at) WHERE enabled;
