# writing-agent-service

新闻研究、辅助写作、热点分析与数据闭环的后端（Python 3.11 / FastAPI / Temporal /
PostgreSQL / Redis / S3-MinIO）。本文件只记录**设计思路**，实现细节和完整清单见仓库
根目录 `README.md` 与 `PROJECT_CONTEXT.md`。

## 一、核心思路

1. **用有边界的 Loop 组合系统，不做无限运行的大 Agent。**
   分三条 Loop：在线洞察（Online Insight）、数据闭环（Data Loop）、模型闭环（Model Loop）。
   每次运行只处理一个窗口，有明确的输入、输出和终止条件。

2. **谁控制、谁计算、谁决策要分层。**
   - 控制流 / 时序 / 权限 → Temporal 编排；
   - 数字 / 规则 / 校验 → 确定性 Python/SQL；
   - 开放文本理解与生成 → LLM Agent；
   - 高风险变更与对外动作 → 人工 Gate。

3. **权威数字只由确定性代码计算。**
   指标、基线、热度分量、排行、门禁判定全部用 Python/SQL；LLM 只做趋势解释、假设和
   表达，不得生成权威数字。

4. **运行必须幂等、可重放、可恢复。**
   统一用稳定幂等键（如 `tenant_id + window + production_bundle_version`）；先落库再启动
   工作流；有副作用的操作最多自动重试一次，失败转只读诊断。

5. **高风险动作只能生成候选，必须人工审批。**
   Prompt、热度规则、模型、生产配置、发布与回滚都需要人工批准；超时默认不晋升、不发布。

6. **一切外部文本都按不可信输入处理。**
   新闻正文、检索结果、模型输出、归因建议都可能包含 Prompt Injection，只作为数据，
   不允许其中的指令改变系统行为。

7. **数据边界：只留必要的聚合与证据。**
   不保存企业原始用户行为明细，只保存聚合指标、受控输入快照、已校验输出、证据引用、
   版本与哈希。

## 二、目录与思路的对应

| 目录 | 职责 | 对应思路 |
| --- | --- | --- |
| `app/workflows/` | Temporal 编排，唯一控制流 | 有边界运行、串行/分支/重试/超时/Gate |
| `app/activities/` | Workflow 与业务之间的薄适配 | 编排不直接写业务逻辑 |
| `app/services/` | 业务编排与用例入口 | 输入构造、校验、组合、幂等 |
| `app/services/agents/` | LLM Runner（结构化输入输出） | 只解释与表达，不产权威数字 |
| `app/analytics/` | 指标、基线、热度、排行、富化、重排 | 确定性计算层 |
| `app/schemas/` | Pydantic 契约 | 成功结果必须是强类型，不退化自由文本 |
| `app/models/` + `app/repositories/` + `alembic/` | ORM、仓储、迁移 | 独立聚合根、租户隔离、不可变快照 |
| `app/domain/` | 错误、状态机、场景等纯领域逻辑 | 与基础设施解耦，可单测 |
| `app/clients/` | FastGPT、知识库、CMS、企业 Adapter | 外部依赖收口在边界 |
| `app/retrieval/` | 关联新闻特征与确定性重排 | 检索是可重建投影，重排是规则 |
| `app/api/` | 只读查询、运营决策、人工 Gate 入口 | 可信网关注入身份，权限最小化 |

## 三、数据与状态思路

- 热点运行、热点事件、反馈、评测、生产 Bundle、Memory 各自独立聚合根，不挂在写作任务上。
- 职责划分：PostgreSQL 存状态/快照/版本；S3/MinIO 存不可变报告与评测产物；
  Redis 只做缓存与事件推送；FastGPT 只做检索投影，不是业务事实源。
- 所有版本化资产（Prompt、热度配置、重排参数、Schema、模型）记录在 Production Bundle，
  支持双基准比较与回滚。

## 四、运行与验证

```bash
python -m pytest -o addopts="" -q                          # 默认全量回归
python -m evaluation.run_hot_news_eval                     # 校验离线评测集
python -m evaluation.run_fault_drills                      # 故障分类演练
python -m app.hot_news_scheduler ensure                    # 幂等创建热点 Schedule
docker compose -f deploy/docker-compose.data-loop-e2e.yml up -d --build
```

生产依赖（企业 RPC、网关/IdP、真实 FastGPT App、Schedule 注册）由部署侧接入；
未配置时依赖工厂 fail-closed，不提供隐式假数据。
