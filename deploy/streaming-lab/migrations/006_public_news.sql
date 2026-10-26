CREATE TABLE stream.public_catalog (
  catalog_id text PRIMARY KEY, manifest jsonb NOT NULL
);
CREATE TABLE stream.public_news (
  catalog_id text NOT NULL REFERENCES stream.public_catalog,
  news_id text NOT NULL, content_type text NOT NULL CHECK(content_type IN ('article','video')),
  record_sha256 text NOT NULL, body_sha256 text NOT NULL, publish_ms bigint NOT NULL,
  PRIMARY KEY(catalog_id,news_id)
);
ALTER TABLE stream.dynamic_feed ADD COLUMN public_catalog_id text REFERENCES stream.public_catalog;
ALTER TABLE stream.dynamic_batches ADD COLUMN public_catalog_id text REFERENCES stream.public_catalog;
CREATE TABLE stream.warehouse_delivery (
  snapshot_id text PRIMARY KEY REFERENCES stream.snapshots,
  delivered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER public_catalog_immutable BEFORE UPDATE OR DELETE ON stream.public_catalog
  FOR EACH ROW EXECUTE FUNCTION stream.immutable_snapshot();
CREATE TRIGGER public_news_immutable BEFORE UPDATE OR DELETE ON stream.public_news
  FOR EACH ROW EXECUTE FUNCTION stream.immutable_snapshot();
