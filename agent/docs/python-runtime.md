# NewsAgent 独立 Python 运行时

状态：2026-10-02。Agent 编排、Prompt/模型版本、结构化输出校验、新闻知识入库、
切片、Embedding、向量检索、QA 入库与索引状态均由本项目的 Python 代码负责。
外部只调用企业内部的 OpenAI-compatible Chat Completions 和 Embeddings 原始接口。

## 运行链路

```text
Vue / 爬虫 / 定时任务
  → FastAPI / Temporal Activity
  → Python Agent Runner + PromptRegistry
  → InferencePort / EmbeddingPort
  → 企业模型接口（本地为同 Port 的确定性替身）
  → Pydantic Schema + 业务 Validator
  → PostgreSQL / 向量索引 / MinIO
  → 查询 API / SSE / 人工审批
```

指标、基线、热度和权威数字由 Python/SQL 确定性计算。新闻正文、检索结果和模型输出
一律视为不可信输入，不能改变系统指令、租户边界、证据白名单或发布门禁。

## 核心模块

- `app/model_runtime/`：YAML 加载、Prompt Registry、模型 Port、HTTP 适配器、结构化解析、
  本地推理与 Embedding 替身。
- `app/knowledge/`：规范化文档、确定性切片、Embedding、版本化索引、PostgreSQL 台账和 QA。
- `app/retrieval/`：本地向量索引、分层召回、业务重排和证据约束。
- `app/agents/` 与 `app/services/agents/`：热点和写作链各 Agent Runner。
- `app/services/production_bundle.py`：Prompt、模型配置、校验器和规则版本的不可变 Bundle，
  候选评测通过后仍要求人工批准和激活。

## 配置

生产参考 `deploy/model-runtime.enterprise.example.yml`，本地参考
`deploy/model-runtime.local.yml`。YAML 只保存 URL、路由、Prompt 版本、Embedding 维度、
超时与批量限制；密钥值通过 `ENTERPRISE_MODEL_API_KEY` 和
`ENTERPRISE_EMBEDDING_API_KEY` 注入，不写入仓库。

应用只接受 `MODEL_RUNTIME_BACKEND=native`，并通过
`MODEL_RUNTIME_CONFIG_PATH` 指向只读 YAML。知识写接口另由 `KNOWLEDGE_INGEST_TOKEN`
保护。

## 验证入口

```bash
docker compose -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
docker compose -f deploy/docker-compose.native-e2e.yml --profile test run --rm test-runner
```

本地验收覆盖 Vue → FastAPI → Temporal → Python Agent → 本地模型 Port → 严格校验 →
PostgreSQL/MinIO，以及知识入库 → 切片 → Embedding → 检索。企业上线前还需对真实接口做
鉴权、限流、超时、错误码、JSON 输出、向量维度和数据出域的合同测试。
