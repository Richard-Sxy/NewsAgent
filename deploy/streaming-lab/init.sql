-- Isolated synthetic experiment only. No existing NewsAgent database is touched.
CREATE SCHEMA stream;
CREATE TABLE stream.window_heads (
  window_key text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0,
  event_count bigint NOT NULL DEFAULT 0, event_digest text,
  final boolean NOT NULL DEFAULT false, snapshot_id text
);
CREATE TABLE stream.snapshots (
  snapshot_id text PRIMARY KEY CHECK (length(snapshot_id)=64),
  window_key text NOT NULL REFERENCES stream.window_heads(window_key),
  revision bigint NOT NULL, tenant_id text NOT NULL, run_id text NOT NULL,
  window_start_ms bigint NOT NULL, window_end_ms bigint NOT NULL,
  final boolean NOT NULL, payload_text text NOT NULL,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(window_key,revision)
);
CREATE TABLE stream.consumption_ledger (
  snapshot_id text PRIMARY KEY REFERENCES stream.snapshots(snapshot_id),
  workflow_id text NOT NULL UNIQUE,
  state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','started','completed','failed')),
  dispatch_attempts integer NOT NULL DEFAULT 0,
  error_type text, updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE stream.reports (
  snapshot_id text PRIMARY KEY REFERENCES stream.snapshots(snapshot_id),
  run_key text NOT NULL UNIQUE, workflow_id text NOT NULL,
  trace_id text NOT NULL, report jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE stream.rejections (
  window_key text NOT NULL, event_id text NOT NULL, reason text NOT NULL,
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(window_key,event_id,reason)
);
CREATE TABLE stream.invalid_windows (
  window_key text PRIMARY KEY, reason text NOT NULL,
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION stream.immutable_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Published snapshots are immutable'; END $$;
CREATE TRIGGER snapshots_immutable BEFORE UPDATE OR DELETE ON stream.snapshots
  FOR EACH ROW EXECUTE FUNCTION stream.immutable_snapshot();
CREATE INDEX snapshots_run ON stream.snapshots(run_id,tenant_id,revision);
CREATE INDEX ledger_pending ON stream.consumption_ledger(state,updated_at);
