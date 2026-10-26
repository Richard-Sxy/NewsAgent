CREATE OR REPLACE VIEW public.news_content_current_categories AS
 SELECT c.tenant_id,
    c.release_id,
    c.identity_sha256,
    c.news_id,
    c.record_sha256,
    c.body_sha256,
    c.title,
    c.body,
    c.url,
    c.publish_time,
    c.content_type,
    c.content_state,
    c.item_quality_flags,
    c.record,
    d.target_category,
    d.action AS category_status,
    d.review_flags
   FROM news_content_selected c
     JOIN news_content_prune_runs p ON p.target_release_id = c.release_id::text
     JOIN news_content_prune_decisions d ON d.operation_id = p.operation_id AND d.news_id = c.news_id::text AND d.record_sha256 = c.record_sha256::text
  WHERE d.action <> 'remove_other'::text;