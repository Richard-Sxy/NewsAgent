-- Completion acknowledgement loss must replay the same batch, not allocate another window.
ALTER TABLE stream.dynamic_batches ADD COLUMN activity_key text;
UPDATE stream.dynamic_batches SET activity_key='legacy:'||run_id;
ALTER TABLE stream.dynamic_batches ALTER COLUMN activity_key SET NOT NULL;
ALTER TABLE stream.dynamic_batches ADD CONSTRAINT dynamic_activity_unique UNIQUE(activity_key);
