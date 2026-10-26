# 共享评测入口

本目录的五个 Python 文件链接到 `../../agent/evaluation/` 原始实现，直接在此调用 CLI
即可；源码没有移动或改写，已有评测包绑定的源码 SHA-256 保持有效。`tests` 链接到
已有 Agent 测试目录，专项入口为其中的五个 `test_query_semantics_*.py`。

`run_query_semantics.py` 提供 prepare、validate、bundle、export-train、run、run-local、
score、compare。公共输入在 `../data/`，冻结包在 `../packages/`，配置在 `../configs/`，
新输出在 `../runs/`。它复用 QueryUnderstanding 与确定性 Resolver，详见
[完整调用链](../README.md#调用链配置与验证)和[评测指南](../docs/query-semantics.md)。
