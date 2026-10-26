# 微调工作区

当前没有正式训练脚本、已训练 adapter 或训练收益成绩。
`../scripts/smoke.py --qlora-backward` 只验证短合成样本的 loss/backward，optimizer_steps=0。

审核后的数据按语义组隔离 train、validation、test；现有 24 条 development 样例不能
改名作为独立 test。导出入口是 `../evaluation/run_query_semantics.py export-train`，
接受 `--cases`、`--labels`、`--holdout-cases`、`--package`、`--out`。
输出放入本目录下新建的版本目录，例如 `query-semantics-training-v1/`，禁止覆盖旧版。

调用链：已审核 cases/labels → 分组与二审校验 → 共享 Prompt/Schema 渲染 →
`train.messages.jsonl` + `training-manifest.json` → 后续正式训练脚本（TODO）→
`../adapters/<版本>/` → 同条件基座/LoRA 评测 → `../runs/<实验>/`。
依赖为共享评测器、Pydantic 与 NewsAgent Schema；未来训练使用现有独立 GPU 环境。

正式训练需实现 assistant-only loss、optimizer step、checkpoint 保存/重载、底模和数据
摘要绑定、validation 选优；先停止占用同一卡的推理服务。未审核标签保持 draft，不能代签。
现有导出与隔离测试在 `../../agent/tests/test_query_semantics_preparation.py`。
完整步骤和参数边界见 [评测指南](../docs/query-semantics.md)。
