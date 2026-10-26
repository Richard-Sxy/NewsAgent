-- Synthetic-only state; never receives enterprise user identifiers.
CREATE TABLE stream.wallclock_session (
 id text PRIMARY KEY, catalog_id text NOT NULL REFERENCES stream.public_catalog,
 start_ms bigint NOT NULL, manifest jsonb NOT NULL, enabled boolean NOT NULL DEFAULT false,
 last_second bigint, attempts integer NOT NULL DEFAULT 0, last_error text,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE stream.wallclock_gap (
 session_id text NOT NULL REFERENCES stream.wallclock_session, start_ms bigint NOT NULL,
 end_ms bigint NOT NULL, PRIMARY KEY(session_id,start_ms)
);
CREATE TABLE stream.metric_preview (
 window_key text PRIMARY KEY, payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE stream.reader_offset (
 session_id text PRIMARY KEY REFERENCES stream.wallclock_session, next_offset bigint NOT NULL DEFAULT 0,
 watermark_ms bigint NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE stream.reader_seen (
 session_id text NOT NULL, tenant_id text NOT NULL, event_id text NOT NULL,
 window_ms bigint NOT NULL, fingerprint text NOT NULL,
 PRIMARY KEY(session_id,tenant_id,event_id)
);
CREATE TABLE stream.synthetic_reader_history (
 session_id text NOT NULL, tenant_id text NOT NULL, user_id text NOT NULL CHECK(user_id LIKE 'synthetic-user-%'),
 news_id text NOT NULL, minute_ms bigint NOT NULL, weighted_score double precision NOT NULL CHECK(weighted_score>0),
 PRIMARY KEY(session_id,tenant_id,user_id,news_id,minute_ms)
);
CREATE INDEX reader_history_time ON stream.synthetic_reader_history(session_id,tenant_id,minute_ms);
CREATE TABLE stream.reader_visit (
 session_id text NOT NULL, window_key text NOT NULL, tenant_id text NOT NULL, window_ms bigint NOT NULL,
 news_id text NOT NULL, user_id text NOT NULL CHECK(user_id LIKE 'synthetic-user-%'),
 PRIMARY KEY(session_id,window_key,news_id,user_id)
);
CREATE TABLE stream.reader_window (
 window_key text PRIMARY KEY, session_id text NOT NULL REFERENCES stream.wallclock_session,
 tenant_id text NOT NULL, run_id text NOT NULL, start_ms bigint NOT NULL,
 event_count bigint NOT NULL DEFAULT 0, last_event_ms bigint NOT NULL DEFAULT 0,
 items jsonb, final boolean NOT NULL DEFAULT false, delivered boolean NOT NULL DEFAULT false, revision bigint NOT NULL DEFAULT 0,
 watermark_ms bigint NOT NULL DEFAULT 0, generated_at timestamptz,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
