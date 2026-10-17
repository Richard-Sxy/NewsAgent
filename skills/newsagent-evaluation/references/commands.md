# 命令入口与副作用

以下相对路径以仓库 `agent/` 为 cwd；`news/` 部分另注明。先确认解释器和依赖，使用已有运行环境，不为评测启动/部署整套环境。下列是入口目录，不是要求全部执行的清单。

## 0. 禁止隐式加载 .env

`app/config.py::Settings`、`app/knowledge/bootstrap.py::VideoIngestSettings`、`news/config.py::Settings` 存在 dotenv 默认配置。用户禁止读取敏感值，因此不使用直接 `source .env` 或打印环境；测试也要避免框架自动加载文件。

Agent离线测试可用以下同进程调用方式（Python 3.11+，将测试列表替换为所选文件）：

```bash
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
from app.config import Settings
Settings.model_config["env_file"] = None
from app.knowledge.bootstrap import VideoIngestSettings
VideoIngestSettings.model_config["env_file"] = None
import pytest
raise SystemExit(pytest.main([
    "tests/test_hot_news_evaluation.py", "tests/test_evaluation_gate.py",
    "-o", "addopts=", "-p", "no:cacheprovider", "-q",
]))
PY
```

运行下述模块时，可在同一前置代码后使用 `runpy` 保留原CLI：

```python
import runpy, sys
sys.argv = ["evaluation.run_hot_news_eval"]  # 其余参数逐项放在数组中
runpy.run_module("evaluation.run_hot_news_eval", run_name="__main__", alter_sys=True)
```

下文 `python -m ...` 表示真实CLI入口；若入口导入 Settings，应通过上述无dotenv同进程方式运行。新入口先检查顶层导入/子进程：父进程设置不自动保护子进程。无法排除隐式dotenv时停止该项，使用不读配置的独立校验；不以“未打印值”为由读取敏感文件。`news/config.py` 如有实例化副作用，须先检查当前代码再选隔离测试路径。

## 1. 不调用模型/业务服务的校验

```bash
python -m evaluation.run_hot_news_eval
python -m evaluation.run_hot_news_dialogue
python -m evaluation.run_operations_evolution validate --verify-artifacts
python -m evaluation.run_operations_research validate --verify-artifacts
python -m evaluation.data_writing_dataset \
  --seed evaluation/datasets/data_writing_august_2026_v1/seed.json \
  --output-dir evaluation/datasets/data_writing_august_2026_v1 --check
python -m evaluation.run_fault_drills
```

前五项只校验数据/标签/哈希；`dataset_valid` 不是模型质量。fault drills 用仓库内故障替身检查分类，不是真实RPC故障。写作 `--check` 不重建/覆盖冻结集。

## 2. 按链路选择的离线合同回归

使用第0节方式将下列文件放入 `pytest.main`；先确认存在，不能把重命名文件悄悄丢弃后称全部覆盖。

| 链路 | 代表性文件（`tests/` 下） |
| --- | --- |
| 热点/权威指标 | `test_hot_news_evaluation.py`, `test_hot_news_orchestration.py`, `test_hot_news_analysis_attempt.py`, `test_ranking.py`, `test_hot_score.py`, `test_prompt_injection_harness.py` |
| SQL | `test_sql_assistant_guard.py`, `test_sql_assistant_planner.py`, `test_sql_assistant_service.py`, `test_hot_news_sql_binding.py`, `test_text2sql_metric_source.py` |
| 索引/标注 | `test_bge_ann_evaluation.py`, `test_index_tokenization_audit.py`, `test_retrieval_judgments.py`, `test_retrieval_judgment_scoring.py`, `test_native_knowledge_indexing.py` |
| 生命周期 | `test_tier_lifecycle_config.py`, `test_tier_lifecycle_store.py`, `test_tier_lifecycle_index.py`, `test_tier_lifecycle_runtime.py`, `test_tier_lifecycle_replay.py`, `test_tier_lifecycle_stability.py`, `test_lifecycle_news_content.py` |
| 对话/Memory | `test_hot_news_dialogue_eval.py`, `test_conversation_security.py`, `test_conversation_stream.py`, `test_conversation_tool_checkpoints.py`, `test_conversation_memory.py`, `test_memory_promotion.py` |
| 数据分析 | `test_data_analysis_engine.py`, `test_data_analysis_comparison.py`, `test_data_analysis_output_integrity.py`, `test_data_analysis_runner.py`, `test_conversation_data_analysis.py`, `test_data_analysis_remote.py` |
| 写作 | `test_native_writing_chain.py`, `test_writing_schema.py`, `test_artifact_pipeline.py`, `test_data_writing_august_sample.py` |
| Data Loop | `test_evaluation_gate.py`, `test_offline_replay.py`, `test_data_loop_workflow.py`, `test_operations_evolution.py`, `test_production_bundle_application.py` |

pytest中有 fake/monkeypatch、opt-in skip；报告真实执行数和跳过原因，不把这些称为真实模型/数据库/Temporal验收。完整回归仅在变更范围或失败要求时扩大。

## 3. 模型报告质量（显式执行）

```bash
python -m evaluation.run_hot_news_eval --run-model \
  --runtime-config /absolute/approved-runtime.yml \
  --prompt-version VERIFIED_PROMPT --model-route APPROVED_ROUTE
```

替换为已核实配置/版本。CLI只有stdout，无 `--output`；调用方将stdout写入新的报告文件，同时保留stderr/退出码并检查JSON完整性。首次输出路径必须不存在。该CLI允许local provider，需按真实配置标记质量证据等级，不把 `--run-model` 字面当真实模型。

```bash
python -m evaluation.run_operations_evolution run-model \
  --runtime-config /absolute/approved-runtime.yml \
  --model-route APPROVED_ROUTE --split development \
  --max-cases 200 --output /absolute/new-experiment/predictions.json
```

外部推理前确认已有端点/路由/数据出域授权与预算；不在命令中填真实密钥。`--max-cases` 按实际选集/预算设置，不把200当必须样本量。此runner拒绝local且要求已注入凭证。`--resume` 仅可复用完全相同身份的检查点；它不重试已记录失败，硬崩溃的最后请求仍需确认端点幂等。

## 4. 离线运营评分与对照

```bash
python -m evaluation.run_operations_evolution score --split test \
  --predictions /absolute/frozen-predictions.json \
  --output /absolute/new-score.json
python -m evaluation.run_operations_evolution compare --split test \
  --candidate /absolute/candidate.json --current /absolute/current.json \
  --previous /absolute/previous.json \
  --policy evaluation/seeds/operations_evolution_gate_v2.json \
  --output /absolute/new-comparison.json
```

上述policy是合成测试设计，不是企业标准；筛选小组后可能不足最低样本数，不可临时降门槛让结果通过。`compare` 退出0仍可能 gate失败，读取 `gate.gate_passed`/`failures`。operations_research 没有 run-model 命令，不编造入口。

## 5. 人工新闻相关性评分

先阅读 `evaluation/news_retrieval_protocol.v1.json`；未完成问题复核、双人盲标/仲裁、事件分组/split、语料/time/evidence/pool审计时只准备标注，不评分。

```bash
python tools/score_retrieval_judgments.py \
  --queries /absolute/queries.jsonl --qrels /absolute/qrels.jsonl \
  --predictions /absolute/predictions.jsonl \
  --expected-queries-sha256 FROZEN_QUERY_BYTE_SHA \
  --expected-qrels-sha256 FROZEN_QRELS_BYTE_SHA \
  --expected-predictions-sha256 FROZEN_PREDICTION_BYTE_SHA \
  --package-sha256 FROZEN_PACKAGE_SHA --split test \
  --output /absolute/new-semantic-report.json
```

退出2表示blocked；退出0是scored，`acceptance_status`仍not_approved。禁止为计算摘要而读取.env；三个输入应是冻结授权文件。新系统Top20出现未判文章需补盲标并另冻结版本，不能当无关。

ANN入口 `python tools/evaluate_bge_ann.py --help`；实际执行显式给 package/manifest/base-manifest/snapshot/version/预期行数/新闻数/维度/旧行边界/seed/ks/efs/pool/threads/warmup/区间次数/output，不沿用默认历史规模。索引只读，输出放包外。重建/下载模型/生产切换不属于评测准备的默认授权。

`tools/query_news_recall.py` 可供单次查询观察，但当前仅实例化默认 `RelatedNewsReranker()`，没有完整候选参数绑定/批量predictions导出，不能声称用它已完成重排参数对照；版本化候选资产与完整检索评测runner仍需补齐。

## 6. 有业务副作用的隔离集成

- `python -m evaluation.run_hot_news_dialogue --run --base-url http://127.0.0.1:VERIFIED_PORT --output /absolute/new-dialogue.json`：创建会话、轮次，可能触发热点Workflow；不清理业务历史。预检local/query_enabled，端口从已授权栈确认。默认地址只是代码默认，不能视为当前可用栈。
- `tests/e2e/test_data_loop_full_chain.py`：`RUN_DATA_LOOP_E2E=1`，使用专用PG/S3/Temporal；**会模拟approve/activate/rollback**，仅在明确授权的隔离E2E执行，不是人工审批证明。
- `test_data_loop_time_skipping.py` / `test_data_loop_analysis_replay.py`：分别读 `RUN_TEMPORAL_TIME_SKIPPING` / `RUN_TEMPORAL_ANALYSIS_REPLAY`，需专用测试服务。
- SQL/聊天HTTP分别检查当前测试的 `SQL_WAREHOUSE_INTEGRATION_URL`、`SQL_ASSISTANT_E2E_BASE_URL`、`CONVERSATION_E2E_BASE_URL`；没有专用目标则not_run/skip。
- 生命周期回放：`python tools/replay_tier_lifecycle.py --config deploy/knowledge-lifecycle.local.example.yml --output-dir /absolute/new-replay`，使用SQLite/确定性Port，非语义或ANN验收。
- Milvus live 需 `NEWSAGENT_MILVUS_LIFECYCLE_LIVE=1` 和获准隔离平台，会写删合成投影。数据分析Docker需 `NEWSAGENT_ANALYSIS_DOCKER_E2E=1`，独立服务Docker另有 `NEWSAGENT_ANALYSIS_SERVICE_DOCKER_E2E=1`。不开启未经核实的opt-in开关。

## 7. 前端与采集模块

涉及UI契约时在 `agent/frontend/` 检查 `npm test`、`npm run lint`、`npm run build` 的实际脚本和配置；不得顺带部署。当前 `vite.config.ts` 显式调用 `loadEnv`，因此原目录直接build会隐式读取dotenv文件；遵循本任务无dotenv边界时，仅可在排除所有 `.env`/`.env.*` 的隔离副本使用已批准公开配置构建，否则该项not_run/blocked，不直接运行。测试/lint也先核实入口不加载dotenv。浏览器验收检查SSE/卡片真实ID/状态/确认/权限，不能仅凭build通过。

`news/` 测试依赖自己的配置和NLP模型，先检查是否加载dotenv/联网；选 `test_discovery_ingest_service.py`、`test_news_ingest_service.py`、`test_newsagent_knowledge_client.py` 等实际受影响模块。历史聚类评测另报missing/evaluated，不并入检索质量分。
