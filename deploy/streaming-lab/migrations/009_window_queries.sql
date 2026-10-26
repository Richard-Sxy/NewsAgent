-- Exact click membership is synthetic-only, like reader_visit. Old minutes
-- without this state must fail coverage checks; never backfill by adding UVs.
CREATE TABLE stream.reader_click (
  session_id text NOT NULL REFERENCES stream.wallclock_session,
  window_key text NOT NULL, tenant_id text NOT NULL, window_ms bigint NOT NULL,
  news_id text NOT NULL, user_id text NOT NULL CHECK(user_id LIKE 'synthetic-user-%'),
  PRIMARY KEY(session_id,window_key,news_id,user_id)
);
CREATE INDEX reader_click_interval ON stream.reader_click(session_id,tenant_id,window_ms);
CREATE INDEX reader_visit_interval ON stream.reader_visit(session_id,tenant_id,window_ms);

CREATE TABLE stream.window_dependencies (
  snapshot_id text NOT NULL REFERENCES stream.snapshots,
  source_snapshot_id text NOT NULL REFERENCES stream.snapshots,
  PRIMARY KEY(snapshot_id,source_snapshot_id),
  CHECK(snapshot_id<>source_snapshot_id)
);
CREATE VIEW stream.invalid_snapshots AS
  SELECT s.snapshot_id FROM stream.snapshots s
  JOIN stream.invalid_windows i USING(window_key)
  UNION
  SELECT d.snapshot_id FROM stream.window_dependencies d
  JOIN stream.snapshots s ON s.snapshot_id=d.source_snapshot_id
  JOIN stream.invalid_windows i ON i.window_key=s.window_key;
