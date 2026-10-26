-- Read-only presentation of one tenant / minute / article. No new stored facts.
-- Topic labels are bound to the exact content and tag releases of each catalog.
CREATE VIEW news_realtime.news_reader_overview AS
SELECT
 n.window_start AT TIME ZONE 'Asia/Shanghai' AS "窗口_北京时间",
 n.title AS "新闻标题",
 content_labels.names AS "新闻内容标签",
 n.reader_count AS "详情读者人数",
 CASE WHEN n.reader_profile IS NULL THEN '尚无画像'
      ELSE coalesce(interests.short_names,'无已知兴趣') END AS "近24小时兴趣_TOP3",
 CASE WHEN n.reader_profile IS NULL THEN '尚无画像'
      ELSE coalesce(interests.long_names,'无已知兴趣') END AS "近30天兴趣_TOP3",
 CASE WHEN n.final THEN '已定稿' ELSE '暂定' END AS "窗口状态",
 CASE WHEN n.source_check_fresh THEN '检查有效' ELSE '检查已过期' END AS "同步检查",
 n.impressions AS "曝光次数",n.clicks AS "点击次数",n.unique_users AS "指标UV",
 (n.reader_profile->'short_term'->>'profiled_reader_count')::bigint AS "短期已画像人数",
 (n.reader_profile->'short_term'->>'unknown_reader_count')::bigint AS "短期未知人数",
 (n.reader_profile->'short_term'->>'cold_start_reader_count')::bigint AS "短期冷启动人数",
 (n.reader_profile->'long_term'->>'profiled_reader_count')::bigint AS "长期已画像人数",
 (n.reader_profile->'long_term'->>'unknown_reader_count')::bigint AS "长期未知人数",
 (n.reader_profile->'long_term'->>'cold_start_reader_count')::bigint AS "长期冷启动人数",
 n.last_event_at AT TIME ZONE 'Asia/Shanghai' AS "最新事件_北京时间",
 n.projected_at AT TIME ZONE 'Asia/Shanghai' AS "入库时间_北京时间",
 n.checked_at AT TIME ZONE 'Asia/Shanghai' AS "检查时间_北京时间",
 '公开新闻缓存 + 模拟读者行为'::text AS "数据来源",
 n.url AS "新闻链接",n.body AS "新闻正文",
 n.reader_profile AS "完整人群画像_JSON",
 n.tenant_id,n.news_id,n.window_key,n.window_start,n.window_end,n.revision,
 n.catalog_id,n.content_release_id,n.tag_release_id
FROM news_realtime.news_live_current n
JOIN news_realtime.catalog cat ON cat.catalog_id=n.catalog_id
LEFT JOIN public.news_content_tag_releases tr
 ON tr.tenant_id=cat.manifest->>'content_tenant_id'
 AND tr.content_release_id=n.content_release_id AND tr.tag_release_id=n.tag_release_id
LEFT JOIN LATERAL (
 SELECT jsonb_object_agg(t->>'topic_id',t->>'name') AS names
 FROM jsonb_array_elements(tr.policy->'catalog'->'topics') t
) labels ON true
LEFT JOIN LATERAL (
 SELECT string_agg(coalesce(labels.names->>t.topic_id,t.topic_id),' / ' ORDER BY t.ord) AS names
 FROM jsonb_array_elements_text(n.topic_ids) WITH ORDINALITY t(topic_id,ord)
) content_labels ON true
LEFT JOIN LATERAL (
 SELECT
  string_agg(x.label,'; ' ORDER BY x.readers DESC,x.topic_id) FILTER (WHERE x.period='short_term') AS short_names,
  string_agg(x.label,'; ' ORDER BY x.readers DESC,x.topic_id) FILTER (WHERE x.period='long_term') AS long_names
 FROM (SELECT d.period,p.value->>'topic_id' AS topic_id,(p.value->>'reader_count')::bigint AS readers,
   coalesce(labels.names->>(p.value->>'topic_id'),p.value->>'topic_id') || ' ' ||
   round((p.value->>'share_all')::numeric*100,1)::text || '%' AS label
  FROM (VALUES ('short_term'),('long_term')) d(period)
  CROSS JOIN LATERAL (
   SELECT value FROM jsonb_array_elements(n.reader_profile->d.period->'topics')
   ORDER BY (value->>'reader_count')::bigint DESC,value->>'topic_id' LIMIT 3
  ) p
 ) x
) interests ON true;

COMMENT ON VIEW news_realtime.news_reader_overview IS
 '新闻与模拟读者兴趣同屏展示；每行=租户+一分钟窗口+新闻。兴趣占比以本篇全部详情读者为分母，标签可重叠，不能相加；不是个人画像。';
COMMENT ON COLUMN news_realtime.news_reader_overview."近24小时兴趣_TOP3" IS
 '按标签读者人数降序，topic_id打破平局；百分比为share_all。仅展示前3项，全部已输出兴趣见完整人群画像_JSON。';
COMMENT ON COLUMN news_realtime.news_reader_overview."近30天兴趣_TOP3" IS
 '近720小时聚合，标签可重叠；不展示不代表人数为0，冷启动和未知人数单独列出。';
COMMENT ON COLUMN news_realtime.news_reader_overview."同步检查" IS
 '沿用底层source_check_fresh检查；检查有效不代表该新闻窗口是当前时间，须同时查看窗口与最新事件时间。';
