# Hunyuan 小模型、微调与评测工作区

统一入口：`/home/shi/project/NewsAgent/small_model`。后续模型实验在本目录管理，
WSL Ubuntu-20.04 为主环境。2026-10-25 从原分散目录迁入，权重与冻结数据原字节保留。

## 目录与当前状态

```text
small_model/
├── models/Hunyuan-7B-Instruct/       原始 BF16 权重、许可证、固定下载清单
├── environments/hunyuan/            原独立 GPU Python 环境
├── environments/query-eval/         原独立评测 Python 环境（venv-20261025）
├── scripts/                        安装、资产校验、推理服务、进程控制、QLoRA 冒烟
├── data/query_semantics_v1/         24 条开发问题与 draft 标签
├── configs/query_semantics/         基座/LoRA、本地/HTTP 配置示例
├── configs/hunyuan-wsl-query-20261025/ 已准备的本机 HTTP 基座评测配置与启动脚本
├── evaluation/                     共享评测器与测试的链接入口
├── packages/                       冻结评测包、已有可搬运 ZIP
├── training/                       训练数据导出与后续训练工作区
├── adapters/                       后续 LoRA 产物，当前没有已训练 adapter
├── runs/                           新实验预测、评分、比较报告
├── runtime/hunyuan/                服务日志、PID 回执、部署报告、私有 Python 与缓存
└── docs/                           WSL、Windows、查询理解评测指南
```

`evaluation/*.py` 链接到 `../agent/evaluation/` 的现有实现。它们复用 NewsAgent 的
`QueryUnderstanding`、Prompt 渲染和确定性 Resolver；保留原位置与内容，避免使既有
评测包的源码哈希失效。模型专用脚本、权重、环境、数据、配置与指南实际存放在本目录，
旧路径只保留兼容链接。共享业务代码无需复制第二份。

权重、环境、缓存、adapter、训练导出及运行结果默认不入 Git；24 条开发样例、
配置示例、脚本和指南正常版本管理。历史回执保留当时路径，不追溯改写实验身份。
Windows 的 `E:\NewsAgent` 副本没有随本次 WSL 整理变更；本目录兼容链接需在 WSL 使用。

## 日常入口

以下命令都从仓库根目录执行：

```bash
cd /home/shi/project/NewsAgent

# 不加载模型：进程状态、资产校验、已有冻结包合同校验。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py status
small_model/environments/hunyuan/bin/python small_model/scripts/assets.py
small_model/environments/query-eval/venv-20261025/bin/python \
  small_model/evaluation/run_query_semantics.py validate \
  --package small_model/packages/query-semantics-dev-wsl-20261025-v2

# 需要真实推理时启动；输出 ready 才表示加载完成。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py start

# 服务 ready 后运行现成的 24 题探索评测，结果写入 small_model/runs/。
bash small_model/configs/hunyuan-wsl-query-20261025/run-base-dev.sh

# 完成后停止服务、释放显存。
small_model/environments/hunyuan/bin/python small_model/scripts/manage.py stop
```

正式训练尚未实现。`scripts/smoke.py --qlora-backward` 只运行短合成样本反向传播，
不做 optimizer step、不保存 adapter。先停止推理服务再执行该 GPU 检查。
训练数据导出、审核及隔离要求见 [training/README.md](training/README.md)。

安装依赖见 [WSL 指南](docs/hunyuan-wsl.md)，Windows 脚本说明见
[Windows 指南](docs/hunyuan-windows.md)，完整评测契约见
[查询理解与 LoRA 评测](docs/query-semantics.md)。当前 `run-local` 不提供 NF4 加载；
本机现成评测使用 NF4 HTTP 服务，不能直接用未量化本地 runner 替代。

## 调用链、配置与验证

- 推理入口：`manage.py start` → `serve.py` → `runtime.load_model()` →
  `assets.verify()` → 本地 Transformers tokenizer/权重 → bitsandbytes NF4/CUDA。
  HTTP 请求经 `ChatRequest`、chat template/token 预算与 `generate()`，返回
  Chat Completions；服务回执和部署检查写入 `runtime/hunyuan/`。
- 微调准备：审核后的 train cases/labels 与 holdout 台账 →
  `run_query_semantics.py export-train` → `validate_cases/validate_gold` →
  `StructuredInferenceService.render_input` → assistant 目标 JSON →
  `train.messages.jsonl` 和 `training-manifest.json`。未接正式 optimizer/adapter 保存。
- 评测入口：`prepare/validate` 冻结和核验输入、标签、Prompt、Schema、源码摘要 →
  `run` 经 HTTP 模型或 `run-local` 经 Transformers/PEFT →
  `StructuredInferenceService` 解析 → `resolve_structured_query` → 逐题预测 →
  `score/compare` 确定性评分。正式测试标签不得进入模型输入或训练。
- 配置：`scripts/requirements.txt` 与 CUDA wheel 锁定文件管理模型依赖；
  `configs/` 指定模型身份、endpoint/adapter、生成预算及凭证环境变量名；
  `packages/*/runtime.json` 和 `manifest.json` 固定 Prompt/Schema/输入/源码版本。
  不读取 `.env`，不用整理目录的机会改变生产路由或已有实验设置。

回归入口（不加载 GPU）：

```bash
small_model/environments/hunyuan/bin/python -B small_model/scripts/test_control.py
# 在具备 pytest 和 Agent 测试依赖的环境下，从 agent/ 执行既有专项：
python -m pytest tests/test_query_semantics_seed.py tests/test_query_semantics_preparation.py \
  tests/test_query_semantics_runner.py tests/test_query_semantics_local.py \
  tests/test_query_semantics_scoring.py
```

迁移清单见 [layout-migration-20261025.json](layout-migration-20261025.json)，记录原目录到
新目录的对应关系及受保护文件 SHA-256。验证结果见
[layout-verification-20261025.json](layout-verification-20261025.json)。

TODO：审核并扩充 train/validation/test；实现正式 QLoRA 训练、checkpoint 与 adapter
台账/加载；运行基座和 LoRA 同条件评测；验证长文本显存与吞吐。目录整理与合同校验
不代表模型质量或训练效果验收。
