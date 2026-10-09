# Milvus 新闻知识检索

聊天的 `search_knowledge` 和标准 `app.hot_news_worker` 的关联新闻检索现在可共用
Milvus YAML 配置。未设置路径时保留原 PostgreSQL 检索；指定 Milvus 后，配置/认证/连接/
查询失败直接报错，不回退 PostgreSQL。会话、摘要、热点运行与报告仍使用 PostgreSQL。
本次只接查询，不创建 collection、不迁移数据、不改变知识入库/QA 的 PostgreSQL 台账。

## 配置与启动

复制 `agent/deploy/knowledge-search.milvus.example.yml` 为部署私有配置，填写地址和集合：

```yaml
schema_version: 1
backend: milvus
milvus:
  uri: http://milvus.example.internal:19530
  database: default
  token_env: MILVUS_TOKEN # 未启用认证时填 null；YAML 不放密钥值
  timeout_seconds: 5
  collections:
    hot: news_hot_v1
  metric_type: IP
  search_params:
    ef: 96
```

`hot/warm/cold` 可任选非空子集。一个集合只配置一次，不将同一集合重复配置给多个层。
API 聊天仅查询已配置的层；热点 Worker 对相同层执行召回和 RRF 合并。
`metric_type` 支持 IP/COSINE，必须与现有索引一致；其他索引类型按其参数设置
`search_params`（例如不需要 HNSW ef 时使用 `{}`）。租户和 Embedding 版本过滤不能关闭。

在 `agent/` 安装可选依赖：

```sh
python -m pip install '.[milvus]'
export KNOWLEDGE_SEARCH_CONFIG_PATH=/absolute/path/knowledge-search.milvus.yml
export CONVERSATION_KNOWLEDGE_ENABLED=true
```

同时沿用现有 `CONVERSATION_ENABLED`、`MODEL_RUNTIME_CONFIG_PATH` 和数据库等配置。
有认证时在进程/密钥系统提供 YAML 中 `token_env` 指定的变量；缺少或为空会拒绝启动。
API 和热点 Worker 必须使用同一检索 YAML 以及同一 Embedding 模型路由/维度。
YAML 在启动时加载，修改后重启相关进程。

Compose：将实际文件保存为 `agent/deploy/knowledge-search.milvus.yml`，在已有部署命令上
追加 `-f agent/deploy/docker-compose.milvus-search.yml`（放在基础配置之后）。Overlay
给 `migrate` 的共享镜像构建启用 `[milvus]`，给 API/热点 Worker 挂载 YAML 并设置路径。
先构建共享镜像，再重建 API 和热点 Worker；不会自动启动或写入 Milvus。认证变量名默认
`MILVUS_TOKEN`，若 YAML 使用其他名字，也要传递相应变量。Docker 中 `127.0.0.1` 指容器
自身；连接宿主机可使用 `http://host.docker.internal:19530`，远端则填实际可达地址。
专用 `examples.native_hot_news_e2e_worker` 仍显式使用隔离 PostgreSQL 样本，不能当作
标准 Milvus Worker 的验收。

## 现有 collection 契约

按用户确认，当前沿用项目字段，不自动猜测现有库字段映射。collection 必须已存在、
建好向量索引并可搜索，包含 `MilvusTieredVectorIndex.REQUIRED_FIELDS`：

| 字段 | 含义 |
| --- | --- |
| vector | 向量字段，维度及生成模型必须与查询 Embedding 相同 |
| tenant_id、embedding_version | 搜索过滤字段，值与可信网关租户/模型路由一致 |
| chunk_id、news_id | 切片与新闻标识 |
| content_version、chunk_index | 内容版本与切片序号 |
| title、excerpt、source_url | 新闻标题、正文切片与来源 |
| publish_time_epoch | 发布时间，Unix 秒整数 |

搜索始终带 tenant_id 和 embedding_version 过滤；只有维度一致但模型不同仍不能正确召回。
如果已有新闻没有这些字段，需另行提供字段映射或建立兼容投影，不能移除租户过滤。

## 完整调用链

聊天：`/chat` → conversations API → `ConversationAgentService.run_turn()` → 模型选
`search_knowledge` → `ConversationTools.execute()` → `EmbeddingPort.embed()` 将问题
转换为向量 → YAML 工厂注入的 `MilvusTieredVectorIndex.search()` → `pymilvus` 向 Milvus
发起带租户/模型版本过滤的搜索 → `VectorSearchHit` → Python 排序、新闻去重、来源 URL
检查 → 标记 `evidence_is_untrusted` 的工具快照 → 会话检查点/最终答复 → PostgreSQL/SSE。

热点：标准 Worker 启动 → `build_native_knowledge_index()` → 同一 YAML 工厂 →
`build_native_knowledge_search()` → `ModelQueryEmbedder` → 各已配置层的 Milvus 搜索 →
`TieredVectorKnowledgeSearchClient` RRF 合并 → 原有证据构造、模型分析和确定性校验。

配置：`Settings.knowledge_search_config_path` → `load_knowledge_search_config()` 严格
校验 YAML → `build_knowledge_search_index()` 读取认证变量 → `MilvusClient` 创建连接。
关闭：API/Worker finally → `close_knowledge_search_index()` → SDK close。
同步 SDK 搜索在 `asyncio.to_thread` 执行，每次 RPC 显式传递 `timeout_seconds`。
连接参数参考 [MilvusClient 官方接口](https://milvus.io/api-reference/pymilvus/v2.5.x/MilvusClient/Client/MilvusClient.md)。

## 测试与未完成项

```sh
cd agent
python -m pytest tests/test_milvus_search_config.py tests/test_milvus_vector_index.py \
  tests/test_milvus_search_live.py tests/test_tiered_vector_retrieval.py \
  tests/test_native_knowledge_indexing.py tests/test_conversation_agent.py \
  tests/test_conversation_security.py tests/test_model_runtime_config_and_http.py
```

离线测试覆盖 YAML、认证缺失、参数传递、单层/禁用层、聊天和热点检索、租户/版本过滤、
失败不回退及连接释放。Fake SDK 只验证合同与链路，不代表真实 Milvus 服务验收。

真实只读合同测试：设置 `NEWSAGENT_MILVUS_LIVE=1`、实际
`KNOWLEDGE_SEARCH_CONFIG_PATH`、真实 `MODEL_RUNTIME_CONFIG_PATH` 和
`NEWSAGENT_MILVUS_TEST_TENANT_ID`，可选 `NEWSAGENT_MILVUS_TEST_QUERY`，再执行
`python -m pytest tests/test_milvus_search_live.py`。测试不会创建集合或写入数据。

TODO：实际地址、集合字段/向量维度/模型和租户数据联调；真实服务过滤与超时/召回质量验收；
需要时另行建设 Milvus 入库/更新/删除同步与索引重建。本次未改生产 Prompt/模型/Bundle，
未自动部署或声明企业验收完成。
