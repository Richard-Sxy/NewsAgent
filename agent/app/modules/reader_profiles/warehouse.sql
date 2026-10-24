-- Local PostgreSQL projection of frozen, aggregate-only reader profile packages.
-- Enterprise TDSQL-A compatibility and enterprise source mappings are not certified.
-- The caller owns the transaction, checks schema absence before initialization,
-- and validates the schema version before reusing an existing schema.
-- CREATE without IF NOT EXISTS is deliberate: never overwrite another schema.
-- Schema contract: news-reader-audience-v1.
-- Publisher checks, not expressible by these row constraints:
--   * publish package, coverage snapshots, populations and topics atomically;
--   * bind every population to content_binding using its snapshot's release;
--   * validate matching 24h/720h rows, content SHA, topic counts and source ranks;
--   * validate immutable dictionary ancestry and source package hashes;
--   * topic numerators must not exceed their corresponding population counts;
--   * preserve source Top-K: an absent topic does not mean a zero count;
--   * never reconstruct personal users or sum finalized UV across windows.

CREATE SCHEMA news_audience;

CREATE TABLE news_audience.package_release (
    source_release_sha256 text PRIMARY KEY,
    manifest jsonb NOT NULL,
    policy jsonb NOT NULL,
    schema_version text NOT NULL,
    row_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    projection_digests jsonb NOT NULL DEFAULT '{}'::jsonb,
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT package_release_digest_ck CHECK (source_release_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT package_release_payload_ck CHECK (
        jsonb_typeof(manifest) = 'object' AND jsonb_typeof(policy) = 'object'
        AND jsonb_typeof(row_counts) = 'object' AND jsonb_typeof(projection_digests) = 'object'
        AND btrim(schema_version) <> ''
    )
);

CREATE TABLE news_audience.content_binding (
    tenant_id uuid NOT NULL,
    source_release_sha256 text NOT NULL,
    news_id text NOT NULL,
    content_sha256 text,
    content_topic_ids jsonb NOT NULL,
    binding jsonb NOT NULL,
    CONSTRAINT content_binding_pk PRIMARY KEY (tenant_id, source_release_sha256, news_id),
    CONSTRAINT content_binding_release_fk FOREIGN KEY (source_release_sha256)
        REFERENCES news_audience.package_release (source_release_sha256),
    CONSTRAINT content_binding_identity_ck CHECK (
        btrim(news_id) <> ''
        AND (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT content_binding_payload_ck CHECK (
        jsonb_typeof(content_topic_ids) = 'array' AND jsonb_typeof(binding) = 'object'
    )
);

CREATE TABLE news_audience.unified_news_binding (
    tenant_id uuid NOT NULL,
    source_release_sha256 text NOT NULL,
    news_id text NOT NULL,
    content_tenant_id text NOT NULL,
    content_release_id text NOT NULL,
    record_sha256 text NOT NULL,
    body_sha256 text NOT NULL,
    binding_method text NOT NULL,
    bound_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unified_news_binding_pk PRIMARY KEY (
        tenant_id, source_release_sha256, news_id, content_tenant_id, content_release_id
    ),
    CONSTRAINT unified_news_binding_source_fk FOREIGN KEY (tenant_id, source_release_sha256, news_id)
        REFERENCES news_audience.content_binding (tenant_id, source_release_sha256, news_id),
    CONSTRAINT unified_news_binding_identity_ck CHECK (
        btrim(content_tenant_id) <> '' AND btrim(content_release_id) <> ''
        AND record_sha256 ~ '^[0-9a-f]{64}$' AND body_sha256 ~ '^[0-9a-f]{64}$'
        AND binding_method = 'news_id_and_body_sha256'
    )
);

CREATE TABLE news_audience.reader_snapshot (
    tenant_id uuid NOT NULL,
    snapshot_id text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    profile_cutoff timestamptz NOT NULL,
    knowledge_as_of timestamptz NOT NULL,
    knowledge_as_of_basis text NOT NULL,
    history_start timestamptz NOT NULL,
    source_revision bigint NOT NULL,
    source_release_sha256 text NOT NULL,
    source_mode text NOT NULL,
    taxonomy_version text NOT NULL,
    profile_policy_version text NOT NULL,
    identity_policy_version text NOT NULL,
    reader_definition text NOT NULL,
    count_method text NOT NULL,
    generated_at timestamptz NOT NULL,
    watermark_at timestamptz NOT NULL,
    completeness text NOT NULL,
    quality_status text NOT NULL,
    quality_basis text NOT NULL,
    expires_at timestamptz,
    min_support bigint NOT NULL DEFAULT 5,
    CONSTRAINT reader_snapshot_pk PRIMARY KEY (tenant_id, snapshot_id),
    CONSTRAINT reader_snapshot_taxonomy_uk UNIQUE (tenant_id, snapshot_id, taxonomy_version),
    CONSTRAINT reader_snapshot_window_release_uk UNIQUE (
        tenant_id, source_release_sha256, window_start, window_end
    ),
    CONSTRAINT reader_snapshot_release_fk FOREIGN KEY (source_release_sha256)
        REFERENCES news_audience.package_release (source_release_sha256),
    CONSTRAINT reader_snapshot_identifiers_ck CHECK (
        btrim(snapshot_id) <> '' AND btrim(taxonomy_version) <> ''
        AND btrim(profile_policy_version) <> '' AND btrim(identity_policy_version) <> ''
        AND btrim(knowledge_as_of_basis) <> '' AND btrim(quality_basis) <> ''
    ),
    CONSTRAINT reader_snapshot_window_ck CHECK (
        window_start < window_end AND window_end <= window_start + interval '24 hours'
        AND profile_cutoff = window_start AND history_start <= profile_cutoff
    ),
    CONSTRAINT reader_snapshot_time_ck CHECK (
        knowledge_as_of >= window_end AND generated_at >= knowledge_as_of
        AND watermark_at <= knowledge_as_of
        AND (expires_at IS NULL OR expires_at > generated_at)
    ),
    CONSTRAINT reader_snapshot_revision_ck CHECK (source_revision >= 1 AND min_support >= 5),
    CONSTRAINT reader_snapshot_source_ck CHECK (
        (source_mode = 'synthetic' AND count_method = 'exact_synthetic_distinct')
        OR (source_mode = 'enterprise' AND count_method = 'enterprise_exact_distinct'
            AND expires_at IS NOT NULL)
    ),
    CONSTRAINT reader_snapshot_definition_ck CHECK (reader_definition = 'detail-view-distinct-v1'),
    CONSTRAINT reader_snapshot_state_ck CHECK (
        completeness IN ('incomplete', 'complete', 'corrected')
        AND quality_status IN ('pending', 'passed', 'failed')
        AND (completeness = 'incomplete' OR watermark_at >= window_end)
    )
);

CREATE TABLE news_audience.topic_dictionary (
    taxonomy_version text NOT NULL,
    topic_id text NOT NULL,
    topic_name text NOT NULL,
    parent_topic_id text,
    topic_level smallint NOT NULL,
    CONSTRAINT topic_dictionary_pk PRIMARY KEY (taxonomy_version, topic_id),
    CONSTRAINT topic_dictionary_parent_fk FOREIGN KEY (taxonomy_version, parent_topic_id)
        REFERENCES news_audience.topic_dictionary (taxonomy_version, topic_id),
    CONSTRAINT topic_dictionary_identifiers_ck CHECK (
        btrim(taxonomy_version) <> '' AND btrim(topic_id) <> '' AND btrim(topic_name) <> ''
        AND (parent_topic_id IS NULL OR (btrim(parent_topic_id) <> '' AND parent_topic_id <> topic_id))
    ),
    CONSTRAINT topic_dictionary_level_ck CHECK (
        (topic_level = 1 AND parent_topic_id IS NULL)
        OR (topic_level > 1 AND parent_topic_id IS NOT NULL)
    )
);

CREATE TABLE news_audience.news_reader_population (
    tenant_id uuid NOT NULL,
    snapshot_id text NOT NULL,
    news_id text NOT NULL,
    lookback_hours integer NOT NULL,
    content_sha256 text,
    source_has_body_hash boolean NOT NULL,
    reader_count bigint NOT NULL,
    profiled_reader_count bigint NOT NULL,
    unknown_reader_count bigint NOT NULL,
    cold_start_reader_count bigint NOT NULL,
    baseline_profiled_count bigint NOT NULL,
    status text NOT NULL,
    trigger_article_excluded boolean NOT NULL,
    limitations text[] NOT NULL DEFAULT ARRAY[]::text[],
    source_payload_sha256 text NOT NULL,
    source_topic_limit integer NOT NULL,
    topics_are_top_k boolean NOT NULL,
    source_topic_count integer NOT NULL,
    CONSTRAINT news_reader_population_pk PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours),
    CONSTRAINT news_reader_population_snapshot_fk FOREIGN KEY (tenant_id, snapshot_id)
        REFERENCES news_audience.reader_snapshot (tenant_id, snapshot_id),
    CONSTRAINT news_reader_population_binding_ck CHECK (
        btrim(news_id) <> '' AND lookback_hours IN (24, 720)
        AND (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$')
        AND source_has_body_hash = (content_sha256 IS NOT NULL)
        AND trigger_article_excluded
    ),
    CONSTRAINT news_reader_population_counts_ck CHECK (
        reader_count >= 0 AND profiled_reader_count >= 0 AND profiled_reader_count <= reader_count
        AND unknown_reader_count = reader_count - profiled_reader_count
        AND cold_start_reader_count >= 0 AND cold_start_reader_count <= unknown_reader_count
        AND baseline_profiled_count >= profiled_reader_count
    ),
    CONSTRAINT news_reader_population_status_ck CHECK (
        (status = 'empty' AND reader_count = 0)
        OR (status = 'complete' AND reader_count > 0)
    ),
    CONSTRAINT news_reader_population_source_ck CHECK (
        source_payload_sha256 ~ '^[0-9a-f]{64}$'
        AND source_topic_limit BETWEEN 1 AND 50
        AND source_topic_count BETWEEN 0 AND source_topic_limit
        AND (status <> 'empty' OR source_topic_count = 0)
    )
);

CREATE TABLE news_audience.news_reader_topic (
    tenant_id uuid NOT NULL,
    snapshot_id text NOT NULL,
    news_id text NOT NULL,
    lookback_hours integer NOT NULL,
    topic_id text NOT NULL,
    topic_reader_count bigint NOT NULL,
    baseline_topic_count bigint NOT NULL,
    taxonomy_version text NOT NULL,
    source_rank integer NOT NULL,
    CONSTRAINT news_reader_topic_pk PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours, topic_id),
    CONSTRAINT news_reader_topic_rank_uk UNIQUE (tenant_id, snapshot_id, news_id, lookback_hours, source_rank),
    CONSTRAINT news_reader_topic_population_fk FOREIGN KEY (tenant_id, snapshot_id, news_id, lookback_hours)
        REFERENCES news_audience.news_reader_population (tenant_id, snapshot_id, news_id, lookback_hours),
    CONSTRAINT news_reader_topic_snapshot_fk FOREIGN KEY (tenant_id, snapshot_id, taxonomy_version)
        REFERENCES news_audience.reader_snapshot (tenant_id, snapshot_id, taxonomy_version),
    CONSTRAINT news_reader_topic_dictionary_fk FOREIGN KEY (taxonomy_version, topic_id)
        REFERENCES news_audience.topic_dictionary (taxonomy_version, topic_id),
    CONSTRAINT news_reader_topic_counts_ck CHECK (
        topic_reader_count >= 0 AND baseline_topic_count >= topic_reader_count
        AND source_rank BETWEEN 1 AND 50
    )
);

-- reader_snapshot_window_release_uk also supplies the exact tenant/release/window index.
CREATE INDEX news_reader_population_news_idx
    ON news_audience.news_reader_population (tenant_id, news_id, snapshot_id, lookback_hours);
CREATE INDEX news_reader_topic_count_idx
    ON news_audience.news_reader_topic (
        tenant_id, snapshot_id, news_id, lookback_hours, topic_reader_count DESC, topic_id
    );
CREATE INDEX news_reader_topic_filter_idx
    ON news_audience.news_reader_topic (tenant_id, snapshot_id, topic_id, lookback_hours, news_id);
CREATE INDEX unified_news_binding_content_idx
    ON news_audience.unified_news_binding (
        content_tenant_id, content_release_id, news_id, tenant_id, source_release_sha256
    );

-- No implicit latest revision, quality filter, expiration filter or cross-window sum.
-- SQL numeric ratios are raw ratios, not the API's eight-place HALF_EVEN rounding.
CREATE VIEW news_audience.v_news_reader_topic_metrics AS
WITH rates AS (
    SELECT
        t.tenant_id, t.snapshot_id, t.news_id, t.lookback_hours, t.topic_id,
        t.taxonomy_version, d.topic_name, d.parent_topic_id, d.topic_level,
        s.window_start, s.window_end, s.profile_cutoff, s.knowledge_as_of,
        s.knowledge_as_of_basis, s.history_start,
        s.source_revision, s.source_release_sha256, s.source_mode,
        s.profile_policy_version, s.identity_policy_version, s.reader_definition, s.count_method,
        s.generated_at, s.watermark_at, s.completeness, s.quality_status, s.quality_basis,
        s.expires_at, s.min_support,
        p.content_sha256, p.source_has_body_hash, p.status, p.trigger_article_excluded, p.limitations,
        p.reader_count, p.profiled_reader_count, p.unknown_reader_count,
        p.cold_start_reader_count, p.baseline_profiled_count,
        p.source_payload_sha256, p.source_topic_limit, p.topics_are_top_k, p.source_topic_count,
        (p.topics_are_top_k AND p.source_topic_count >= p.source_topic_limit) AS topic_list_may_be_truncated,
        t.source_rank, t.topic_reader_count, t.baseline_topic_count,
        t.topic_reader_count::numeric / NULLIF(p.reader_count, 0) AS share_all,
        t.topic_reader_count::numeric / NULLIF(p.profiled_reader_count, 0) AS share_profiled,
        t.baseline_topic_count::numeric / NULLIF(p.baseline_profiled_count, 0) AS baseline_share,
        LEAST(p.profiled_reader_count, p.baseline_profiled_count,
              t.topic_reader_count, t.baseline_topic_count) < s.min_support AS low_sample
    FROM news_audience.news_reader_topic AS t
    JOIN news_audience.news_reader_population AS p
      ON (p.tenant_id, p.snapshot_id, p.news_id, p.lookback_hours)
       = (t.tenant_id, t.snapshot_id, t.news_id, t.lookback_hours)
    JOIN news_audience.reader_snapshot AS s
      ON (s.tenant_id, s.snapshot_id, s.taxonomy_version)
       = (t.tenant_id, t.snapshot_id, t.taxonomy_version)
    JOIN news_audience.topic_dictionary AS d
      ON (d.taxonomy_version, d.topic_id) = (t.taxonomy_version, t.topic_id)
)
SELECT rates.*,
       CASE WHEN low_sample THEN NULL::numeric
            ELSE share_profiled / NULLIF(baseline_share, 0) END AS lift
FROM rates;

-- Existing population rows only: never materialize all content x covered windows.
-- A query for a missing population must first prove a content binding AND a covered,
-- complete window using LEFT JOINs before it can conclude that readership is empty.
CREATE VIEW news_audience.v_news_reader_population_status AS
SELECT p.*,
       s.window_start, s.window_end, s.profile_cutoff, s.knowledge_as_of,
       s.knowledge_as_of_basis, s.history_start,
       s.source_revision, s.source_release_sha256, s.source_mode, s.taxonomy_version,
       s.profile_policy_version, s.identity_policy_version, s.reader_definition, s.count_method,
       s.generated_at, s.watermark_at, s.completeness, s.quality_status, s.quality_basis,
       s.expires_at, s.min_support,
       (p.topics_are_top_k AND p.source_topic_count >= p.source_topic_limit) AS topic_list_may_be_truncated,
       p.profiled_reader_count::numeric / NULLIF(p.reader_count, 0) AS profile_coverage
FROM news_audience.news_reader_population AS p
JOIN news_audience.reader_snapshot AS s
  ON (s.tenant_id, s.snapshot_id) = (p.tenant_id, p.snapshot_id);

COMMENT ON SCHEMA news_audience IS
    'news-reader-audience-v1: frozen aggregate reader packages; not enterprise connectivity certification.';
COMMENT ON TABLE news_audience.package_release IS
    'Immutable source identity is SHA256 of raw manifest bytes; embedded manifest identity is a different digest.';
COMMENT ON TABLE news_audience.content_binding IS
    'Source-package news membership; nullable content hash preserves title-only source records.';
COMMENT ON TABLE news_audience.unified_news_binding IS
    'Explicit business-tenant to public-content mapping verified by exact news ID and nonempty body hash; no tenant aliasing.';
COMMENT ON TABLE news_audience.reader_snapshot IS
    'Covered aggregate window; latest-revision selection, quality gating and consistent reads are caller responsibilities.';
COMMENT ON TABLE news_audience.topic_dictionary IS
    'Immutable versioned topic names and ancestry; publication validates full ancestry and source identity.';
COMMENT ON TABLE news_audience.news_reader_population IS
    'Actual detail visitors and separate 24h/720h interest denominators; missing observations are not zero.';
COMMENT ON COLUMN news_audience.news_reader_population.source_has_body_hash IS
    'Whether the source population has a nonnull body SHA; source news membership is separately checked in content_binding.';
COMMENT ON COLUMN news_audience.news_reader_population.source_payload_sha256 IS
    'SHA256 of the original UTF-8 payload_json string in reader_profiles.sqlite, identical in the two interest rows.';
COMMENT ON COLUMN news_audience.news_reader_population.topics_are_top_k IS
    'True means the source export applies a Top-K cap; fewer than K retained topics does not certify an exhaustive taxonomy.';
COMMENT ON TABLE news_audience.news_reader_topic IS
    'Retained source Top-K topic counts and original rank; absent topics are not zero and overlapping counts are not additive.';
COMMENT ON VIEW news_audience.v_news_reader_topic_metrics IS
    'Raw numeric shares and lift from stored counts; low support suppresses lift; all snapshot status fields remain visible.';
COMMENT ON VIEW news_audience.v_news_reader_population_status IS
    'Existing populations joined to their exact coverage snapshot; no synthetic empty rows or content/window Cartesian product.';
