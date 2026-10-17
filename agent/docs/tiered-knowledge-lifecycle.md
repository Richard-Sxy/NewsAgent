# 冷温热新闻索引配置与运行

这条链路处理新闻正文的新增、更正、撤稿、降层和回热。显式设置
`KNOWLEDGE_LIFECYCLE_CONFIG_PATH` 后，知识入库改为先持久接受任务，再由专用
Temporal Worker 写向量；聊天、热点关联召回和检索测试 API 共用带状态校验的查询服务。
未设置此变量时，现有知识入库和检索行为保持兼容。

## 先运行本地持久回放

在仓库根目录执行，输出目录必须不存在或为空：

```bash
python3.11 agent/tools/replay_tier_lifecycle.py \
  --config deploy/knowledge-lifecycle.local.example.yml \
  --output-dir output/tier-lifecycle-my-first-run
```

程序生成三篇合成新闻分别进入热、温、冷层，然后回放更正、回热、热转温、温转冷、撤稿、
重复与乱序事件，并重新打开数据库检查恢复。输出包括 `report.json`、
`config.snapshot.json` 和 `lifecycle.sqlite3`。报告记录每一步的有效层、版本、切片数及查询计时。
业务时间用于模拟新闻变老，真实单调时钟用于耗时；两者互不替代。

这里使用本地确定性 Embedding 和持久 SQL 精确向量搜索，验证生命周期及恢复语义。
它不代表企业模型质量或 Milvus ANN 性能。回放不读取 `.env`，不调用企业服务，也不修改现有 BGE 包。

## 配置三个集合与生命周期

企业环境分别准备三份配置文件，API、热点 Worker 和索引 Worker 使用同一组文件：

| 配置入口 | 文件与作用 |
| --- | --- |
| `MODEL_RUNTIME_CONFIG_PATH` | 沿用模型配置，填写企业推理及 Embedding 端点、鉴权环境变量名、路由和实际维度 |
| `KNOWLEDGE_SEARCH_CONFIG_PATH` | 从 `deploy/knowledge-search.tiered.example.yml` 复制，填写向量地址、库、三集合和索引搜索参数 |
| `KNOWLEDGE_LIFECYCLE_CONFIG_PATH` | 从 `deploy/knowledge-lifecycle.enterprise.example.yml` 复制，填写分层、切片、查询和任务策略 |

企业模板中的 `null` 必须填写；不能直接启动。密钥值只由环境或企业密钥系统提供。
本地生命周期模板的秒级阈值仅用于演示，不作为企业默认值。

三个 collection 必须不同，并包含 `MilvusTieredVectorIndex.REQUIRED_FIELDS`：
`chunk_id`、`tenant_id`、`news_id`、`content_version`、`chunk_index`、`embedding_version`、
`title`、`excerpt`、`source_url`、`publish_time_epoch`、`vector`。
以应用生成的 `chunk_id` 为字符串主键，不使用自动生成主键；实际向量维度、索引度量与模型一致。
由企业向量平台建立并加载三个集合，给应用对应集合的读写及删除权限。
本链路强制采用 Strong 读一致性进行完整元数据验证和搜索，跨进程查询不能仅依赖写客户端 Session。
应用不自动建库、改索引或重建已有 BGE 数据。

| 参数 | 语义及约束 |
| --- | --- |
| `embedding_version`、`expected_embedding_dimensions` | 与模型运行配置一致；相同维度但不同模型不能混用 |
| `policy_version`、`chunking_version` | 可追溯的策略和切片版本；接受任务时冻结配置 |
| `chunk_chars`、`chunk_overlap` | 字符数，满足 `0 <= overlap < chunk_chars`；模型 token 限制另行按接口合同确认 |
| `max_chunks_per_document`、`max_vector_coordinates_per_document` | 整篇切片数和切片数×维度的上限；在切片分配及 Embedding 前拒绝超预算任务，避免高重叠耗尽内存 |
| `embedding_batch_size`、`vector_batch_size` | 模型输入条数和向量写入/验证批量；前者不能超模型配置上限，也受现有 1 MiB 模型响应上限影响 |
| `hot_max_age_seconds`、`cold_min_age_seconds` | `0 <= age < Th` 热，`Th <= age < Tc` 温，`age >= Tc` 冷；`0 < Th < Tc` |
| `reheat_hold_seconds` | 显式回热后的保持时间，期满回到年龄策略 |
| `migration_interval_seconds`、`scan_batch_size` | 后台重新评估间隔及分页数量；扫描包含撤稿记录，以再次清理迟到写入 |
| `dispatch_interval_seconds`、`task_queue` | PG待处理任务的恢复派发周期和专属 Temporal 队列 |
| `max_attempts`、重试间隔、Activity/Workflow超时 | 有限重试及总预算；耗尽后保留失败记录，由显式 retry 创建新任务 |
| `query_candidate_limit`、`query_max_candidates` | 每层初始切片候选和有界补搜上限，新闻去重后数量可能更少 |
| `query_refill_factor`、`query_request_timeout_seconds` | 扩大候选比例及全查询预算，失败不返回未经校验的证据 |
| `query_rrf_k`、`query_weights` | 排名融合参数；权重不会自动缩短该层检索耗时 |
| `query_max_concurrency` | 同一进程查询服务的并发上限，进入该上限前的排队也计入总超时 |
| `max_search_rows` | 仅本地 SQL 精确搜索的扫描上限 |

若使用 HNSW，搜索 YAML 的 `ef` 必须覆盖生命周期 `query_max_candidates`，包括补召回可能扩大的请求。
`M`、`efConstruction` 由企业平台建索引时配置。三个集合采用相同资源和索引时，不保证热层必然比冷层快。

正文缺失、发布时间缺失/无时区/晚于当前业务时间会被拒绝，不伪造为冷层数据。
已有新闻按其接受时冻结的策略进行降层和回热；改 YAML 不会静默改写历史任务或模型身份。
策略变更需新版本，并明确重投内容的修订号及重建范围。

## 迁移数据库并启动

先在测试库应用 Alembic `20261015_0023`，增加 `tier_lifecycle_news`、
`tier_lifecycle_jobs`、`tier_lifecycle_vectors` 三张表。最后一张只用于本地持久向量替身。
迁移不会把旧 PostgreSQL/BGE 文档自动转换到新链路，首次启用应从可信正文源重新投递。

在 `agent` 目录、企业连接与前述配置变量已准备好的进程环境中执行：

```bash
python3.11 -m alembic upgrade head
python3.11 -m app.knowledge_lifecycle_worker
```

API及标准热点Worker按原有部署方式启动并传入相同配置变量。
API、热点 Worker 和专用索引 Worker 启动时均检查生命周期表和向量集合契约。
Milvus 预检检查主键、字段类型、维度、VARCHAR 容量、向量索引度量和已加载状态；不会自动修复集合。
集合存在 Adapter 无法提供的额外必填字段时拒绝启动；允许可空、合法默认值或服务端生成的兼容字段。
`ef: null` 属于配置错误；实际 HNSW 索引要求明确提供覆盖最大候选数的整数 `ef`。
单进程默认一次处理一个索引 Activity，以优先保证首版可控运行。
多副本的限流需按模型、向量服务和PG总配额配置；正确性由数据库锁、修订号和条件激活保证。
本地服务测试允许 `environment=e2e` 且搜索后端为 PostgreSQL/未设置，使用同一 PostgreSQL 的 SQL 向量替身。
生产环境要求真实 Milvus；只读 `bge_snapshot` 不能作为增量写入目标。

任务的队列、重试、超时、写入及清理批量遵循接受任务时冻结的配置。更换 `task_queue` 后，
旧队列的 Worker 必须保留到旧任务处理完毕。同一队列的 Worker 必须连接同一套数据库、
索引和兼容模型契约；不同部署使用不同队列。扫描工作流按队列区分身份，每处理完一页，
携带游标 `continue_as_new`，避免前几页反复消耗整个运行预算而让后续新闻长期得不到处理。
单条新闻扫描或清理失败会保存错误类别并继续后续新闻；数据库故障、错误保存失败仍重试整页。

## 数据与状态的调用链

```text
爬虫或内容接口 POST /api/v1/knowledge/documents
→ KnowledgeDocument
→ KnowledgeLifecycleService.accept
→ PG 同事务保存最新正文、单调修订号、当前状态与索引任务
→ 返回 outcomes.collection_id 作为 job_id
→ 专用 Worker 从PG派发 KnowledgeIndexWorkflow
→ Activity.process → split_text → EmbeddingPort
→ 保存完整投影清单 → 分批 upsert → 完整元数据校验
→ PG核对当前generation后激活目标层 → 清理旧投影
```

接收成功不等于索引就绪。现有 `created/updated/skipped/failed` 结果结构保留，
`detail` 说明已接受；通过文档或任务状态判断是否可以查询。
本文的 `content_version` 是来源的单调修订号，撤稿也必须用更大修订号；
同号同内容幂等，同号不同内容/操作冲突，旧号拒绝。修订号不能仅用接收时间临时猜测。

更正接受后立即清空旧版的有效投影，新版建立期间该新闻暂不返回。
迁移保留旧有效层，只有目标投影验证完成且条件更新成功后才切换。
撤稿先提交状态，之后开始的查询立即被状态校验挡住，物理删除由后台执行。
与变更并发的查询按其状态读取时点解释；缓存和历史报告不自动改写成当前状态。

状态由事件和 Worker 更新，不在 YAML 中手工设置：

| 台账字段 | 值与含义 |
| --- | --- |
| 新闻 `status` | `pending` 等待新投影；`active` 存在可用投影；`retracted` 已撤稿。文档状态API将active映射为ready |
| `desired_tier` / `active_tier` | 策略要求的层 / 目前实际可查询的层；迁移期间允许不同 |
| 任务 `status` | `pending` 待处理、`retry` 可重试、`failed` 终止、`complete` 处理完成；不代表每个complete任务都曾激活 |
| 任务 `phase` | `pending → embedded → active → complete`；撤稿或过期任务可直接清理后complete |
| `source_revision` / `generation` | 来源正文修订号 / 内部投影代次；纯迁移只递增generation，撤稿与正文更正递增来源修订号 |
| `active_job_id` / `current_job_id` | 当前可查询投影 / 当前预期任务；条件更新必须匹配当前代次，迟到任务不能激活 |
| `cleanup_dirty` / `last_error` | 是否仍有清理工作 / 最近错误类别；失败信息不保存SDK敏感错误全文 |

`phase=active,status=failed` 可能表示目标已激活但旧层清理失败，此时正确的新投影仍可查，
不因清理异常回滚为旧新闻。查看新闻状态和任务状态两个维度，不能只凭一项判定。

每个任务具有独立投影切片ID。迟到任务可以写入自己的旧副本，但不能激活新状态，也不能清理其他任务的有效副本。
失败和进程中断后复用已保存向量；首次整篇向量快照保存之前中断可能重新调用 Embedding。
纯层间迁移会尝试复用当前有效源投影的向量，生成新的任务切片 ID 后写入目标层。
只有正文修订号、正文内容、模型身份和维度、切片版本及参数全部一致，且完整向量清单通过校验，
才能跳过 Embedding。源记录不存在、不完整或不兼容时重新 Embedding；已保存的目标任务快照
损坏时明确失败，不静默覆盖。更正正文或变更模型、切片方式仍需重新计算向量。
因此迁移节省的是模型调用，目标写入、可见性验证、条件激活和源层删除仍会执行。

## 测试 API

所有接口沿用 `Authorization: Bearer <知识入库凭据>` 和可信 `X-Tenant-ID`；
不要把凭据写入命令历史或提交到配置。租户头的可信性继续由企业网关和入库账号权限保证。

| 操作 | 请求 | 验证点 |
| --- | --- | --- |
| 新增或更正 | `POST /api/v1/knowledge/documents`，沿用已有请求 Schema | 响应 job_id，状态先 pending 后 ready |
| 文档状态 | `GET /api/v1/knowledge/documents/{document_id}/status` | current_job_id、active_tier、desired_tier、generation、index_status、last_error |
| 任务状态 | `GET /api/v1/knowledge/index-jobs/{job_id}` | 状态、阶段、失败次数；不暴露正文、向量或配置 |
| 生命周期预检 | `GET /api/v1/knowledge/lifecycle/readiness` | 验证 PG 台账及三集合契约；不代表 Worker 已消费或模型服务可用 |
| 撤稿 | `POST /api/v1/knowledge/news/{news_id}/retract`，正文 `{"source_revision": 新修订号}` | 等删除完成前就不能再查询到旧稿 |
| 回热 | `POST /api/v1/knowledge/news/{news_id}/reheat` | 保持期内有效层升HOT，源层清理不会留下重复计分 |
| 重试耗尽任务 | `POST /api/v1/knowledge/index-jobs/{job_id}/retry` | 仅当前failed任务可重试，创建新job和generation |
| 召回测时 | `POST /api/v1/knowledge/search`，正文 `{"query": "问题", "limit": 返回篇数}` | items含层与版本；trace含排队、Embedding、各层搜索及校验、最终校验、融合、总耗时 |

一次入库的最小请求形状如下，发布时间按测试业务时钟填写真实值：

```json
{
  "documents": [{
    "document_id": "article-demo-001",
    "news_id": "demo-001",
    "content_version": 1,
    "title": "合成测试新闻",
    "text": "用于验证三层状态的合成新闻正文。",
    "publish_time": "2026-10-15T10:00:00+08:00",
    "source_url": "https://example.test/news/demo-001"
  }]
}
```

准备三个不同news_id，发布时间分别取当前时间减去 `Th/2`、`(Th+Tc)/2`、`Tc+一个时间单位`，
分别验证热、温、冷。更正保持news_id/document_id不变，修订号递增并修改正文；
撤稿使用下一个修订号。回热先调用reheat，再观察desired/active变化及保持期到期后的降层。

聊天的 `search_knowledge`、热点关联召回与上述搜索接口都走 `LifecycleKnowledgeSearch`：
Query Embedding → 三层并发 → 每层PG有效状态过滤/有界补搜 → 合并后再次校验 → 新闻去重与加权排序。
数据库或向量服务失败时直接报错，不使用未经校验的旧结果兜底。
搜索依赖失败返回 503 `knowledge_search_unavailable`；全查询预算耗尽返回 504
`knowledge_search_timeout`。错误响应保留已产生的分段 trace，只记录错误类别。
一层失败或请求取消时，等待中的其余查询会被取消并收回；底层同步 SDK 的在途 RPC
仍须等其实际完成或 SDK 超时，不能把 Python 任务取消等同于远端请求终止。
Milvus Adapter 为每个客户端设置有界 RPC 容量，生命周期工厂取
`3 × query_max_concurrency`，搜索、写入和预检共享该容量；额度只有在真实同步调用结束时才释放。
等待额度的请求可以取消，不会再提交线程任务。该限制只作用于当前进程客户端；SDK 若忽略超时，
在途线程无法强杀，关闭客户端也必须等待它结束，仍需企业 SDK/网络超时和进程健康策略配合。
热点 Worker 另通过 `LifecycleNewsContentRepository` 从台账读取已激活的正文，
召回后再次核对源投影与候选切片，避免旧缓存标题覆盖新版本结果。启用前必须先让热点候选源新闻
入库并达到ready；缺失、待更正或已撤稿的源新闻会触发现有数据质量阻断。
`/ready` 仍检查既有PG、Redis、Temporal连通性，不能单独证明索引Worker在消费或集合可检索；
发布验收应观察一条新任务到ready，并实际搜索其新版本。

## 实际跑延时测试

先完成三层数据投递，确认每篇新闻的状态为 `ready` 且 `active_tier` 正确，然后调用预检和搜索。
压测程序只发搜索请求，不会自动导入新闻、启动 Worker、迁移数据或改变参数。
查询文件为 UTF-8 JSONL，每行严格包含三个字段，例如：

```json
{"id":"q-001","query":"城市公交线路调整","limit":10}
{"id":"q-002","query":"科技行业动态","limit":10}
```

在仓库根目录运行下面的命令；大写变量由你填写，鉴权令牌通过已注入环境的
`KNOWLEDGE_INGEST_TOKEN` 提供。工具不读取 `.env`，不记录令牌或查询正文。

```bash
python3.11 agent/tools/benchmark_tier_lifecycle.py \
  --url "$LIFECYCLE_API_URL" \
  --tenant-id "$TEST_TENANT_ID" \
  --token-env KNOWLEDGE_INGEST_TOKEN \
  --query-file "$QUERY_FILE" \
  --requests "$MEASURED_REQUESTS" \
  --concurrency "$CLIENT_CONCURRENCY" \
  --warmup "$WARMUP_REQUESTS" \
  --timeout "$CLIENT_TIMEOUT_SECONDS" \
  --output-dir "$NEW_OUTPUT_DIR"
```

所有参数均必填，输出目录必须为空或不存在。`--requests` 是正式请求总数，`--warmup` 是额外预热请求数；
`--concurrency` 是客户端并行请求数，与服务端 `query_max_concurrency` 区分。
`--timeout` 包含连接池等待及响应读取；要观察服务端 504，通常应比服务端总预算略大。
工具使用一个共享连接池，关闭自动重试和重定向；每个请求完成后才发下一个，是固定并发闭环负载，
不是固定到达 QPS。命令退出码 0 表示全部请求成功，1 表示预热或正式请求有失败，2 表示参数/文件错误。

输出 `manifest.json` 保存参数和查询文件摘要；`samples.jsonl` 记录逐次状态、结果数和允许的数值指标；
`report.json` 汇总错误率及 p50/p95/p99。预热不计入正式分位数；分位数仅使用成功正式请求，
失败数和错误率单列，缺失指标用 null 和样本数 0 表示，不伪造成零延时。
保存配置快照、索引类型、各层新闻和切片数量、向量平台资源配置，才能公平比较不同实验。

| trace v2 字段 | 测量边界 |
| --- | --- |
| `queue_ms` | 等待本进程查询并发额度 |
| `embedding_ms` | Query Embedding 及向量契约校验 |
| `tiers.<tier>.vector_search_ms` | 各轮向量 Adapter 的等待时间总和，含 SDK 排队、网络和映射，不是 Milvus 服务端独占耗时 |
| `tiers.<tier>.state_gate_ms` | 各轮 PG 有效投影校验等待时间总和 |
| `tiers.<tier>.filter_ms` | Python 过滤本轮候选耗时 |
| `tiers.<tier>.rounds` / `refill_rounds` | 每轮 requested/retrieved/accepted 和分段耗时 / 追加补搜轮数 |
| `tiers.<tier>.search_and_gate_ms` | 该层搜索、校验及补搜总时间 |
| `final_gate_ms` / `fusion_ms` | 三层汇总后的最终有效性校验 / 去重及融合 |
| `total_ms` / `client_wall_ms` | 服务端查询总时间 / 客户端从发送到响应体读完的时间 |

三层并行运行，不能把三层耗时相加解释为端到端延时。单次三层结果也不构成召回质量评测；
质量比较仍需固定查询、相关性标注和统一正文版本。想比较迁移期间的延时，应先测稳定数据基线，
再在另一进程投递更正、撤稿、回热事件并扫描降层，分别保留事件时间、任务阶段和查询样本。

## 回归与故障验收

```bash
cd agent
python3.11 -m pytest -q tests/test_tier_lifecycle_config.py \
  tests/test_tier_lifecycle_store.py tests/test_tier_lifecycle_index.py \
  tests/test_tier_lifecycle_runtime.py tests/test_tier_lifecycle_wiring.py \
  tests/test_tier_lifecycle_worker.py tests/test_tier_lifecycle_replay.py \
  tests/test_tier_lifecycle_migration.py tests/test_lifecycle_news_content.py \
  tests/test_tier_lifecycle_vector_reuse.py tests/test_tier_lifecycle_stability.py \
  tests/test_tier_lifecycle_observability.py tests/test_milvus_lifecycle_preflight.py \
  tests/test_milvus_rpc_budget.py \
  tests/test_benchmark_tier_lifecycle.py
```

真实PostgreSQL用 `NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL` 显式指定测试库；
真实Temporal测试再设置 `NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_ADDRESS`，命名空间变量
`NEWSAGENT_TIER_LIFECYCLE_TEST_TEMPORAL_NAMESPACE` 可选，默认default。
用例创建随机schema、随机队列和工作流ID，仅清理自身测试资源。
迁移测试实际执行本次Alembic revision，校验列、主键、唯一约束、索引与空表回退，
并检查任一生命周期表有数据时拒绝回退，避免误删状态记录。
服务、数据库与SDK版本按部署环境锁定；Milvus真实服务还需单独写入验收，Fake SDK不代替平台验收。

已经提供真实 Milvus 的条件验收入口。在独立测试集合配置好后，显式设置
`KNOWLEDGE_SEARCH_CONFIG_PATH`、`KNOWLEDGE_LIFECYCLE_CONFIG_PATH` 和对应鉴权环境变量，再运行：

```bash
cd agent
NEWSAGENT_MILVUS_LIFECYCLE_LIVE=1 python3.11 -m pytest -q tests/test_milvus_lifecycle_live.py
```

该测试在三层创建带随机租户和 ID 的合成单位向量，逐层执行写入、完整元数据验证、检索、
租户隔离和删除确认，只清理自身记录；不会建集合、清空集合或调用 Embedding。
若清理失败，断言输出测试随机租户及本次切片 ID，可据此处理残留测试记录。
这是写入/查询合同验收，仍不是模型质量或规模性能验收。
预检字段依据 Milvus 官方的 [describe_collection](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Collections/describe_collection.md)、
[describe_index](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Management/describe_index.md) 和
[get_load_state](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Management/get_load_state.md)；SDK 版本需与企业平台匹配。

断点依次放在：PG已接受但尚未派发、向量部分写入、目标验证后尚未激活、激活后尚未清理。
恢复后检查任务不丢、旧版本不复活、撤稿不返回、有效层投影不被迟到清理删除。
同一查询集比较不同配置时，保留参数快照、各层数据量、p50/p95、错误率、结果数和召回质量。

## 外部依赖与待完成项

运行依赖 PostgreSQL、Temporal、企业 Embedding 及 Milvus；本地回放使用 SQLite 和隔离模型/向量Port。
Redis/S3继续原有业务用途，不参与索引状态仲裁。核心实现入口为
`tier_lifecycle_factory.py`、`tier_lifecycle.py`、`tier_lifecycle_store.py`、
`tier_lifecycle_query.py` 和 `knowledge_lifecycle_worker.py`。

企业真实配额、网络、模型质量、向量服务性能和长稳验收仍需在目标环境完成。
QA生成的一篇新闻多个子文档尚未纳入新闻生命周期，启用本链路时 `/qa` 明确返回409，
避免在旧库写成功却无法从三层查询到。历史正文/向量清单目前保留用于审计及迟到写入清理，
归档保留策略、大规模扫描优化和跨副本全局限流是后续容量工作。
已有完整热点演示数据集与新数仓profile的接线，不由此功能自动改变。
