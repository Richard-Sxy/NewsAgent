# 100 项写作评测审核入口

review-template.json 保留全部计划 A/B 稿，所有质量字段仍待人工审核。
先在数据集 human-review.md 中核对事件边界与金标，再逐稿填写引用证据。
editing-readiness.json 使用生成前登记的 10 对；失败样本不自动替换。

若 editing-tasks.json 已就绪，在 agent 目录运行：

    python -m evaluation.manual_editing_server --package <本目录>/editing-tasks.json --state-dir <本目录>/manual-editing-state --port 18882

真人点击开始后才计时；阅读、核查、修改均计时，中断暂停。gold_status=draft 不会自动变成已审核，旧两例的批准不覆盖这些材料。
未知质量不记零错；完成生成不等于完成语义评分或生产验收。

本机实际页面：http://127.0.0.1:18882/ 。Windows 8882 位于系统排除范围，因此使用18882；未修改系统网络设置。
WSL不可自动转发时，可在 Windows PowerShell 的 agent 目录，用 Windows python 执行同一条计时命令。
