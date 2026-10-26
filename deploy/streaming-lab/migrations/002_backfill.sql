-- Append-only lineage. Existing snapshots/reports and Flink state remain intact.
CREATE TABLE stream.backfills (
  request_id text PRIMARY KEY,
  request_digest text NOT NULL CHECK(length(request_digest)=64),
  snapshot_id text NOT NULL UNIQUE REFERENCES stream.snapshots(snapshot_id),
  base_snapshot_id text NOT NULL REFERENCES stream.snapshots(snapshot_id),
  method_version text NOT NULL CHECK(method_version='synthetic-full-recount-v1'),
  added_event_count integer NOT NULL CHECK(added_event_count>0),
  reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER backfills_immutable BEFORE UPDATE OR DELETE ON stream.backfills
  FOR EACH ROW EXECUTE FUNCTION stream.immutable_snapshot();
