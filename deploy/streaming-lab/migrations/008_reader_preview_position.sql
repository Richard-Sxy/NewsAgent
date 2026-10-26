-- Align anonymous provisional audiences to the exact Flink Kafka-prefix position.
ALTER TABLE stream.reader_visit ADD COLUMN first_sequence bigint NOT NULL DEFAULT 0;
