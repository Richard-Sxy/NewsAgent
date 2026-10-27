# 向量召回策略：存储分层与相关性排序分开

本轮实现的是**可回放的候选策略**，未自动启用、未替换运行中的生产配置。
用户所说的多层召回已明确为同一模型的热／温／冷三层索引。
当前接入 `LifecycleKnowledgeSearch`，聊天和热点仅在使用该生命周期 runtime 时共享新策略；
普通 `TieredVectorKnowledgeSearchClient`、不可变 BGE 快照和 streaming SQL 词法入口保持原路径。
功能测试不代表新闻语义质量、ANN 召回率或企业容量已经验收。

## 1. 为什么不只增加主题权重

热、温、冷首先描述文档的存储生命周期，不是三个独立相关性专家。
原生命周期检索的文章分为 `tier_weight / (60 + tier_rank)`，不同分区的局部名次被直接比较。
例如 hot=1、cold=0.7 时，热层第27名的 `1/87` 仍大于冷层第1名的 `0.7/61`，
即使冷层原始向量相似度明显更高。默认旧普通客户端也有类似的层权重偏置。
本地生命周期示例的旧权重恰好全为1，所以不能把0.7称为所有已运行配置的实测值。

只给“财经、AI、体育”各加一套权重，会把这个偏置扩展为更多需要维护的参数。
同一主题下，“AI最新产品发布”和“AI发展历程”对历史证据的需求不同。
主题适合作为预算先验；明确的查询意图优先。预算扩大只能增加候选机会，不能保证排在前列。

## 2. 可借鉴的公开实践

- [Meta Explore（2023）](https://engineering.fb.com/2023/08/09/ml-applications/scaling-instagram-explore-recommendations-system/)
  将多来源候选、初排、重模型排序与最终多样性规则分开，并讨论离线评估和参数调优。
  这是推荐系统公开案例，本文只借鉴分阶段和质量／成本权衡，不照搬用户画像或训练授权。
- [Azure AI Search 语义排序](https://learn.microsoft.com/en-us/azure/search/semantic-search-overview)
  在 BM25 或 RRF 的初始结果上进行第二阶段语义重排；重模型在有界候选集合上运行。
- [Milvus RRF／Weighted Ranker](https://milvus.io/docs/reranking.md)
  区分分数加权与名次融合。RRF适合合并多种检索路径；
  [Elastic RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)
  也将其用于不同相关性信号的结果集合。
- [Bing 排序说明](https://support.microsoft.com/en-us/bing/how-bing-delivers-search-results)
  将意图相关性、质量和时效等作为不同信号，并说明其相对重要性可随查询变化。

对 NewsAgent 的工程判断：**同一向量空间的三个分区先合并比较；词法、不同模型等异构通道
才需要名次融合或经过标注校准的分数融合；最终证据使用更严格的相关性和业务门禁。**
这不是对任何公司的内部新闻检索实现或参数的推断。

## 3. 本轮落地链路

```mermaid
flowchart LR
    A[鉴权后的查询或热点标题摘要] --> B[版本化意图和主题预算计划]
    B --> C[热温冷并发向量召回]
    C --> D[逐轮台账校验与新闻去重补召回]
    D --> E[跨层最终有效版本校验]
    E --> F[同空间全局相似度排序]
    F --> G[可选已有事件语义扩展与再次校验]
    G --> H[新闻候选与请求内trace]
    H --> I[热点原有主体事件重排与EvidencePacket]
```

### 查询计划

`QueryRetrievalPolicy.plan()` 输入查询、返回数量、配置候选预算、上限和可选提示，输出
`QueryPlan(intent, topic, reason, tier_limits)`。没有模型调用或外部文本执行。

| 意图 | 识别或显式提示 | 示例预算：最终要10篇、初始候选20、每层上限80 |
| --- | --- | --- |
| general | 无明确线索、主题不明确或冲突 | 热20、温20、冷20 |
| recent | 最新、今天、刚刚、实时 | 热40、温20、冷20 |
| background | 历史背景、发展历程、来龙去脉、演变 | 热20、温20、冷40 |

预算公式为：`min(max_candidates, max(limit, max(min_per_tier, default_candidates,
limit * candidate_multiplier) * intent_multiplier[tier]))`。
所有层始终有至少所需返回数量的候选预算。它不是固定总预算调度：上例recent共80篇目标候选，
general共60篇，可能增加延迟；请求仍受原并发、全局期限、原始切片数和补搜轮次限制。

对于有序精确检索、稳定台账且每层已取得至少最终K篇的情况，各层Top K已足以组成全局Top K；
单独增加某层预算通常不会改变最终结果。意图／主题倍率目前是候选预算实验入口，
不能把它表述为已实现主题差异化排序或时效提升；ANN近似邻域及并发版本门禁需另行测量。

优先级：显式intent → 明确时间意图 → 唯一主题映射 → general。
时间线索冲突回到general；多个主题命中且没有显式主题时也回到general。
显式未知主题被拒绝，不会忽略拼写错误。`retrieval_intent=general` 可以关闭自动意图推断。
词表仅做可解释的轻量推断，不能理解所有否定句和复合意图。正文中的词不构成系统指令，
只能触发已配置且有界的候选计划，不改变权限、模型或配置。

“今天／最新”目前只增加热层预算，**不等于严格的日期过滤，也不保证结果一定最新**。
严格时间范围需要绑定可信业务as_of并在检索前过滤；热层还可能包含被重新加热的历史文章，
不能将tier当作发布日期。此项与查询语义模型列为后续能力。

主题规则在独立候选YAML中提供草稿示例，不读取或覆盖当前内容主题／分类发布。
AI与财经示例默认general，避免把主题永久等同于追新；history可映射background。
正式主题需来自批准目录并经过分组质量评测；不按主题硬过滤新闻。

### 新闻级补召回

`GuardedTieredIndex.search_news_with_trace()` 是新增接口，原 `search/search_with_trace()`
继续保持切片数语义。新接口每轮先限制原始候选前缀，再调用 `ledger.filter_hits()`，
检查层、Embedding身份及排除新闻，按news_id保留最高分片段并稳定排序。
去重新闻不足时扩大检索切片预算，每轮重新校验整批，避免沿用已撤回的批准结果。

`query_max_candidates` 是每次ANN查询的**原始切片**上限，并不保证凑齐等量新闻。
数据耗尽、上限或轮次到达时可以少返回；不能编造候选。
trace的 `termination_reason` 为 `enough_news/exhausted/max_candidates/max_rounds`，
同时记录unique_news、duplicate_chunks与各轮检索／台账校验耗时。

### 排序和分数合同

在相同Embedding版本、维度和同一higher-is-better IP/COSINE度量下，三层候选按原始相似度
统一排序；每篇只保留最优切片，同分按news_id、chunk_id确定顺序。NaN/Inf失败关闭。
本地SQL明确声明IP；Milvus声明其配置度量，并沿用启动时所有collection的schema、
维度及metric核验。不支持L2、混合度量或未经声明的第三方Adapter。

`rank_candidates()` 导出的 `RelatedNews.score=(k+1)/(k+global_rank)` 是兼容已有重排器的
**名次特征**，不是概率、模型置信度或原始相似度。新模式不会再除以旧层权重。
热点下游原 `RelatedNewsReranker` 仍综合主体、事件、标题词和时间；其阈值尚待重新校准。
名次第一恒为1，即使所有候选原始相似度很低；去掉cold先验也可能让原先未过阈值的冷新闻
进入下游。因此该分数不能承担相关性拒答门槛，新策略保持未激活，须用独立标注校准精排和拒答。

`trace.ranking` 描述语义扩展之前的排序。已有 `semantics.enrich()` 仍可补入互补证据、
处理替代关系并改变分数和顺序；该阶段单独记录trace，并保留双端关系核验和最终台账gate。
新策略基础排序不使用 `query_weights`；旧策略仍使用它，已有语义扩展也保留原层资格检查。
候选示例将所有层设为正值，避免把关闭某层的旧配置直接误用到新实验。

## 4. 文件、配置及入口

| 位置 | 职责和上下游 |
| --- | --- |
| `app/api/knowledge.py` | `/api/v1/knowledge/search`；原鉴权后接受可选intent/topic，向query传参，返回原items和新增trace，异常脱敏 |
| `app/modules/knowledge/tier_lifecycle_query.py` | 聊天／热点／知识API共用 `LifecycleKnowledgeSearch`；Embedding校验、并发分层、最终gate、统一排序、原语义扩展 |
| `app/retrieval/query_policy.py` | `QueryRetrievalPolicy`校验、计划与`rank_candidates`；依赖标准库、Pydantic、现有向量DTO |
| `app/modules/knowledge/tier_lifecycle_index.py` | 新闻级去重补召回；依赖原向量Port与PostgreSQL台账，每轮重新校验 |
| `app/modules/knowledge/tier_lifecycle_config.py` | 新增可选`query_retrieval`；其余生命周期参数仍显式必填 |
| `app/modules/knowledge/tier_lifecycle_local_index.py`、`app/clients/enterprise/milvus_vector.py` | 显式声明相似度度量；不更改索引schema、模型或写入路径 |
| `deploy/knowledge-lifecycle.query-aware.example.yml` | 独立本地候选配置，包含完整旧生命周期参数和新策略；不是生产校准值 |

上游装配仍为 `build_native_knowledge_index` → `build_lifecycle_runtime` →
`index.lifecycle_query` → `build_native_knowledge_search` 返回同一query实例。
`KNOWLEDGE_LIFECYCLE_CONFIG_PATH` 选择完整生命周期配置；模型、搜索端点继续使用既有
model-runtime／knowledge-search配置和密钥环境变量，不读取`.env`。
新策略的`version`、完整配置随既有config序列化保留；变更须建新版本，生产发布继续人工审核。
没有自动修改当前环境变量、活动Bundle、容器、数据库或重新计算历史运行。

示例请求（仍须既有租户Header和Bearer鉴权）：

```json
{"query":"人工智能发展历程","limit":10,"retrieval_intent":"background","retrieval_topic":"ai"}
```

未启用候选策略时，原请求保持旧行为；显式新提示返回422，避免静默丢弃。
不传提示时按可解释规则生成计划；热点／聊天不需要修改原 `RelatedNewsSearchQuery` Schema。

## 5. 验证与后续优化

新增测试：

- `tests/test_query_retrieval_policy.py`：计划优先级、冲突／未知、预算界、统一排序、最佳片段及分数身份。
- `tests/test_query_retrieval_integration.py`：旧策略对照、冷层高相关候选、新闻级补召回、HTTP提示、
  语义降级、最终撤回gate和新YAML加载。
- `tests/test_tier_lifecycle_index.py`：新增补搜硬界、超额返回、同分稳定、错误身份和非有限分值。
- `tests/test_tier_lifecycle_config.py`：原必填项仍严格验证，只允许可选候选策略省略。

从`agent/`用Python3.11+运行上述pytest；本轮使用已有Docker镜像、禁网、只读挂载WSL主仓库。
完整定向回归命令：

```bash
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v /home/shi/project/NewsAgent:/workspace:ro -w /workspace/agent \
  --entrypoint python news-agent/writing-agent-service:native-e2e-numpy \
  -m pytest -p no:cacheprovider -o addopts= -q \
  tests/test_query_retrieval_policy.py tests/test_query_retrieval_integration.py \
  tests/test_tier_lifecycle_index.py tests/test_tier_lifecycle_observability.py \
  tests/test_tier_lifecycle_runtime.py tests/test_tier_lifecycle_wiring.py \
  tests/test_tier_lifecycle_config.py tests/test_news_semantics.py \
  tests/test_news_semantics_business.py tests/test_news_semantics_wiring.py \
  tests/test_milvus_vector_index.py tests/test_milvus_search_config.py \
  tests/test_tiered_vector_retrieval.py
```

下一轮需要分别验证，不能由功能测试推导效果：

1. 固定同一tenant／内容release／Embedding／查询和独立qrels，对照旧RRF、全局排序、
   全局排序+意图预算，观察pooled Recall@K、nDCG、无答案误召回及主题／意图分组退化。
   可复用 `tools/score_retrieval_judgments.py`，必须传实际冻结查询、标注、预测及SHA，缺标注即blocked。
2. 同时记录P50/P95、每层有效候选率、重复率、补搜次数、Embedding／索引／台账分段耗时；
   新策略目标候选增加不等于性能提高。本地SQL扫描不能代替Milvus规模测试。
3. 增加独立的词法／实体召回，补齐股票代码、产品型号、数字和新实体；当前未新增BM25/ES服务。
4. 有批准模型和独立标注后，在有界候选上接Cross-Encoder精排，或训练LTR做信号校准；
   当前没有下载模型、训练或假称现有规则是学习排序。
5. 对同事件转载、事实新增、更正和互补证据分别评估，再做事件层去重和多样性。
   [Elastic MMR](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/retrievers/diversify-retriever)
   可作技术参考；直接套MMR可能丢掉必要跟进证据，本轮没有实现MMR。
6. 严格业务时间过滤、可信主题元数据接线、持续影子回放、负载／故障验证和生产审批仍待完成。

本轮13模块定向回归：249 passed、8 skipped。跳过项是需要PostgreSQL advisory lock／DO块的
语义业务用例；本轮禁网SQLite替身没有执行这些PG专属分支。git diff --check通过。
没有执行企业模型、真实Milvus规模压测或独立新闻相关性质量评分。
