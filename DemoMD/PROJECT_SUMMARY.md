# NewsAgent 项目摘要

NewsAgent 是以 Python 实现的新闻运营 Agent 系统。它将企业已有行为/内容数据、新闻采集、
热点分析、知识检索、研究写作、反馈评测和人工审批连接成可审计闭环。

## 已实现

- 腾讯新闻发现、正文抓取、缓存、URL 幂等、重试、标题 NLP 和事件聚类实验。
- Python 知识 API、规范化文档、确定性切片、企业 Embedding Port、版本化索引与 QA 入库。
- 行为数据 Port、指标聚合、基线、热度、排行、正文精确查询和关联新闻确定性重排。
- Python 结构化 Agent 调用：版本化 Prompt → 推理 Port → Pydantic → 业务 Validator。
- 热点 API/SSE、Temporal Worker/Schedule、PostgreSQL/MinIO 持久化和人工决策。
- 研究写作 Workflow、Checkpoint、不可变 Artifact、Reviewer 定点返工、人工 Gate 和 CMS Port。
- Data Loop、三层评测集、确定性门禁、人工批准、激活、回滚和 Memory。
- Vue 运营前端与独立本地 Compose 全链路。

## 技术边界

- Agent 应用层和知识库能力由本项目实现，不依赖外部 Agent 框架运行时。
- 外部只调用企业原始 Chat Completions 与 Embeddings 接口。
- 指标、基线、热度和权威数字由 Python/SQL 计算；LLM 只解释与表达。
- 新闻、检索和模型输出不可信；高风险配置与发布必须人工审批。
- 不保存企业原始用户行为明细，只保存聚合快照、版本和证据引用。

## 当前未完成

- 企业模型端点、鉴权、限流、错误码和向量维度的真实合同测试。
- 企业行为/内容/基线 RPC Adapter 的真实 SDK 联调。
- 生产持久化向量索引和索引恢复/重建压测。
- Gateway/IdP、企业密钥系统、网络治理、容量与长稳测试。
- 前端写作与 Data Loop 页面更完整的本地交互验收。

长期状态以 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) 为准。
