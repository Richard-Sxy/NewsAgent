# 三层新闻召回测试摘要

已实际完成隔离测试：真实公开新闻、真实 BGE 查询编码、三个新建并重载的 HNSW 索引。在线部署仍沿用原单库 BGE 配置。

## 实际结果

- 计划 12 题，完成 12 题，失败 0 题。每题 11 项链路断言全部通过。
- 三个索引都已保存，并用新实例重载，核对全部行号与向量。
- 总耗时 17.29 秒，包含包/模型哈希核验、建图、模型加载、预热与逐题查询；不是服务吞吐或生产性能指标。
- 输入与相关源码在运行前冻结，运行后再次核验摘要一致。
- 语义质量验收为 `blocked`：缺少独立人工相关性标签，不计算语义 Precision、Recall 或 nDCG。

| 实际索引 | 新闻篇数 | 向量切片 | 索引文件 MiB |
|---|---:|---:|---:|
| 热 | 1,000 | 1,598 | 6.66 |
| 温 | 3,000 | 5,268 | 21.96 |
| 冷 | 4,000 | 7,007 | 29.21 |

三层全部为 float32，同一模型和内积度量；这次没有测量化压缩。冻结观察日为北京时间 2026-10-15 00:00，以自然日差 <=1 / <=4 天演练分层。12,187 篇缺结构化日期新闻被排除。

## 专项发现：召回到了，融合却可能排掉

同一个真实查询向量、同一批索引、同样的候选预算，对比两套预注册策略：

| 原生 RRF 策略 | 12 题中最终 Top5 包含冷层的题数 |
|---|---:|
| 默认：hot=1 / warm=1 / cold=0.7 | 0 / 12 |
| 等权诊断：hot=1 / warm=1 / cold=1 | 12 / 12 |

三层是互斥文章集合，rank-only RRF 只使用各层内部名次，不比较跨层原始内积。默认冷层第 1 名得分 `0.7/61`，低于热层或温层第 3 名的 `1/63`；候选充足时，Top5 会被热温层占满。

等权也不代表质量更好。相同名次在各层得分相同，本批再按 news_id 打破同分，12 题的 Top5 都呈现冷 2 / 温 2 / 热 1。这会把某层最接近、但仍主题较远的文章排到前面。例如威马 APP 问题的等权融合第 1 条是华为手机播放视频报道，出题来源威马报道在第 3 位。需要统一相关性排序或精排对照，不能用层覆盖替代质量。

## 六条报道查找题：出题来源出现位置

来源 ID 仅用于出题与溯源，不是独立金标，也不是全库相关集合。以下是观察，不统计准确率。

| 问题 ID | 来源所在层 | 在本层 Top5 的位置 | 等权融合 Top5 位置 | 默认融合 Top5 位置 |
|---|---|---:|---:|---:|
| find-hot-01 | 热 | 1 | 3 | 2 |
| find-hot-02 | 热 | 1 | 3 | 2 |
| find-warm-01 | 温 | 1 | 2 | 1 |
| find-warm-02 | 温 | 1 | 2 | 1 |
| find-cold-01 | 冷 | 2 | 4 | 未进入 |
| find-cold-02 | 冷 | 1 | 1 | 未进入 |

例：冷层踏板工程报道 `20261010A016W000` 在冷层第 2、等权融合第 4，默认融合 Top5 不出现。冷层充电报道 `20261009A04QRA00` 在冷层第 1、等权融合第 1，默认融合 Top5 不出现。

三个源排除专项题均未返回指定的 source_news_id。这只能证明按新闻 ID 排源，不能证明转载或近重复事件去重。

## 评测集与逐题证据

- [12 题清单、来源与检查规则](/Users/shi/Project/myProject/NewsAgent/output/evaluation-inputs/multitier-recall-20261020/README.md)：6 道报道查找、3 道跨层主题、3 道源排除。
- [逐题全部候选与两策略结果](/Users/shi/Project/myProject/NewsAgent/output/evaluation/2026-10-20-multitier-real-bge-v1/report.md)：每题真实编码时间、各层计时、标题、URL、时间、片段、内积分数、两套融合结果。
- [冻结运行规格](/Users/shi/Project/myProject/NewsAgent/output/evaluation/2026-10-20-multitier-real-bge-v1/run-spec.json)。
- [机器结果](/Users/shi/Project/myProject/NewsAgent/output/evaluation/2026-10-20-multitier-real-bge-v1/results.json)。
- [后验资产与断言核验](/Users/shi/Project/myProject/NewsAgent/output/evaluation/2026-10-20-multitier-real-bge-v1/verification.json)。

这些问题依据缓存来源材料编写，不是真实用户流量，所有语义标签均为 draft_unreviewed。未判候选不能当作负例。

## 自己复跑

使用不存在的输出目录，旧结果拒绝覆盖。下例另建 v2 运行目录，仍使用本次固定问题与参数：

```bash
cd /Users/shi/Project/myProject/NewsAgent
PYTHONDONTWRITEBYTECODE=1 USE_TF=0 USE_FLAX=0 \
news/.venv-recall/bin/python agent/tools/test_multitier_news_recall.py \
  --package var/agent/data/news-bge-incremental-20261014-v2 \
  --query-file output/evaluation-inputs/multitier-recall-20261020/queries.jsonl \
  --output-dir output/evaluation/2026-10-20-multitier-real-bge-v2 \
  --as-of 2026-10-15 --hot-days 1 --warm-days 4 \
  --limit 5 --threads 2 --seed 20261020
```

## 调用链、配置、验证与 TODO

上游：只读 v2 清单、切片/向量/模型与 queries.jsonl。CLI → 原生 `_verify_package` / `_read_chunks` → 结构化日期分层 → hnswlib 三图构建、保存与重载 → `LocalBgeQueryEmbedder` 真实查询编码 → 缓存同一真实向量 → `TieredVectorKnowledgeSearchClient` → 只读 `IsolatedThreeTierIndex` → 两套原生 RRF → query-XX-result.json / results.json / report.md。cases 仅供旁路出题与溯源，不进编码器。

核心入口为 [test_multitier_news_recall.py](/Users/shi/Project/myProject/NewsAgent/agent/tools/test_multitier_news_recall.py)。外部依赖是 numpy、hnswlib、torch、sentence-transformers；不调用外部网络。配置为 CPU 2 线程、M=32、efConstruction=200、ef=max(256,pool)、起始 pool=200、每层 50 篇候选、最终 Top5、seed=20261020、总预算 300 秒。

验证入口：上述 CLI 完成 12/12 真实模型与索引链路检查；`agent/tests/test_tiered_vector_retrieval.py` 另有 2/2 Mock 合同测试通过，不计入真实模型结果。后验核验检查冻结输入/源码哈希、逐题断言与资产数量。

本地 Port 的 CPU 调用为同步执行，原生 asyncio 分发不等于三层真实并行；本轮没有 OTel、HTTP、PG、Temporal、业务精排和企业接口。两策略固定先等权后默认，计时受缓存和顺序影响，不据此比较策略性能。

TODO：独立人工相关性标注与候选池审核；跨层统一相关性/精排对照；按真实业务跨度获得三层代表性语料；量化、迁移、撤稿更正、企业集群与 API 全链路性能验证。当前没有改变在线检索配置或应用业务行为。
