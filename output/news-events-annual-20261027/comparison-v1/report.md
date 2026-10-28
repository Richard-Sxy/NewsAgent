# 旧 RRF 与全局相似度排序：冻结对照

本轮是同候选预算的融合排序消融。实际调用 LifecycleKnowledgeSearch 的两个分支；数据源是冻结的 NumPy 精确检索和不可变新闻门禁替身。
不包含生产 PostgreSQL/Milvus 并发门禁、ANN 性能、语义替代或主体事件业务重排。

- 查询：30；共同 Top10 候选池：454 项。
- 当前缺少明确人工判定：0 项。
- 原 300 项单人标注保留；补标独立保存，未判定和无法判断都不计为负例。
- 旧策略读取本地示例配置的 RRF k 与层权重；三层候选按新闻去重并统一取每层20篇，只比较融合规则。
- 本批查询已看过结果，作为开发对照；未标成独立 test，不报告企业 P95 或完整 Recall。

| 策略 | Precision@5 | 共同池 nDCG@5 | Precision@10 | 共同池 nDCG@10 |
| --- | ---: | ---: | ---: | ---: |
| legacy_rrf | 37.33% | 0.5633509452729536 | 26.00% | 0.6345479054853163 |
| global_similarity | 56.00% | 0.9501287223645629 | 34.00% | 0.9640310638349668 |

## 调用链与复现

prepare：冻结 review 导出和 manifest → 核对语料／模型／向量身份 → OfflineBgeProcess 编码同一查询 → exact_news_candidates 做精确检索和新闻去重 → rank_pair 调用实际 LifecycleKnowledgeSearch 的旧 RRF、新 global_similarity 分支 → 校验新策略 Top10 与原人工任务逐项一致 → 冻结 predictions 和查询向量。
supplemental_runs：合并两组 Top10，按 query_id/news_id/body_sha256 复用原人工标签；新增对打乱顺序后写入独立 supplemental.sqlite3。模型不产生相关性标签。
serve：ComparisonReviewService → 既有 create_app/static UI → PUT save_judgment 事务保存、修订号校验和审计；原8877标注库不写入。
score：冻结原标签 + 补标库 → calculate_comparison → 固定30查询共同候选池 exp2 nDCG@5/@10 和严格Precision → results.json/report.md。任一共同池标签缺失时总体指标保持null。
配置：--review-dir/--package-dir/--model-dir/--embedding-python/--legacy-config/--device/--output-dir；实际参数及源码哈希在 config.json，查询向量和源码身份见 evidence-manifest.json。
入口：agent/tools/compare_annual_retrieval.py prepare|serve|score；serve仅回环8879，补标不需要GPU或模型服务。
测试：agent/tests/test_retrieval_comparison.py；覆盖实际RRF分支、共同分母、缺标阻断、证据漂移、盲标隐藏与API持久化。
TODO：完成新增人工标签后重新score；独立事件组与双审、源新闻业务任务、企业索引性能以及语义替代均需后续实验。
