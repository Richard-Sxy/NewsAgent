ALTER TABLE stream.consumption_ledger
  ADD COLUMN lease_token text,
  ADD COLUMN lease_until timestamptz,
  ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE INDEX ledger_dispatch_due ON stream.consumption_ledger(next_attempt_at)
  WHERE state IN ('pending','started');
CREATE TABLE stream.dispatch_events (
  id bigserial PRIMARY KEY,
  snapshot_id text NOT NULL REFERENCES stream.snapshots(snapshot_id),
  action text NOT NULL,
  attempt integer NOT NULL,
  error_type text,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX dispatch_events_snapshot ON stream.dispatch_events(snapshot_id,id);
