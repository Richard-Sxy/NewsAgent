# 年度检索旧／新策略对照与补标

本轮已在冻结的545篇新闻、2577个真实BGE分片向量和30个问句查询上执行排序对照。
当前主结果目录为 `output/news-events-annual-20261027/comparison-v1/`。

## 已执行结果

- 新策略30个查询的Top10全部与原人工标注任务一致。
- 旧RRF与新策略各返回300个查询—新闻组合，其中146个重合，共同Top10池共454项。
- 原300项单人标签按query_id、news_id、body_sha256复用；额外154项需人工判定，分布在30个查询。
- 旧RRF的Top5中有69项尚未标注，不能把它们当作不相关，也不能只保留已经标完的查询计算总体结果。
- 新增及相关存储/API回归57项通过；真实查询向量已保存，并独立重算两组排序全部一致。
- 原8877标注数据库与已冻结快照的300个标签逐字段一致；补标数据库初始0标签。

## 现在如何操作

打开 http://127.0.0.1:8879/ 。这是独立补标服务，原8877仍用于原来的300项。
只需判断154项新增候选，无需重做原标注。相关=2、部分相关=1、不相关=0；
无法判断单列。短报道按回答价值判断，信息不足可备注。顺序已经打乱，隐藏策略归属、模型分数及原排名。
标签通过既有同源PUT接口保存，刷新可恢复，支持改标与审计。表中展示的顺序不用于计算检索质量，
因此补标界面不展示该顺序的Precision/nDCG。

完成补标后，在WSL仓库根目录执行：

```bash
var/news-events-annual-20261027/runtime/app-venv/bin/python -B \
  agent/tools/compare_annual_retrieval.py score
```

输出更新为 `results.json` 和 `report.md`。任一共同池候选未判断或无法判断时，
整体旧／新质量指标保持null，并列出具体缺口；不会自动生成标签。

若需要重启补标页面：

```bash
var/news-events-annual-20261027/runtime/app-venv/bin/python -B \
  agent/tools/compare_annual_retrieval.py serve --port 8879
```

此入口无需启动模型。已运行时不重复启动同端口。

## 对照口径

旧策略使用 `deploy/knowledge-lifecycle.local.example.yml` 中的 `query_rrf_k=60`
和 `query_weights={hot:1,warm:1,cold:1}`。这只是仓库本地示例，不能称为企业历史线上参数。
严格参数读取器只支持该示例的单行flow-style权重映射，遇到其他布局会报错，不套隐含默认值。

两组使用同一BGE模型、同一查询向量、同一分层和同一候选预算。每层按最优分片去重，
取最多20篇新闻，再调用现有 `LifecycleKnowledgeSearch` 的旧RRF／新global_similarity分支。
这里统一新闻级候选，仅隔离融合排序变量；没有复刻旧链路中“先截断原始切片再新闻去重”的候选差异。
热温冷沿用年度语料的7／90天日期规则，不采用本地演示的120／480秒规则，也不表示行为热度。

旧RRF分数为 `weight/(60+tier_rank)`，按分数和news_id稳定排序。
新策略按同空间余弦相似度全局排序，复用原查询策略，并显式固定general以保持预算一致。
主体／事件业务重排和事实覆盖语义替代均未启用，不把它们混入本次收益。

## 完整调用链

上游为第一版冻结单人标注包的manifest、export和数据库快照。prepare先核对所有哈希，
从已有真实向量索引复用正文向量，调用OfflineBgeProcess对相同查询编码，保存查询向量及模型身份。
exact_news_candidates生成各层稳定新闻序列，rank_pair把它们接入真实LifecycleKnowledgeSearch的两条排序分支。
FrozenIndex/FrozenLedger/FrozenEmbedder只是隔离的不可变输入Port，不连接生产PG或Milvus，
不据此宣称验证并发迁移门禁或企业性能。

predictions保存两组输出及原文哈希；新策略结果逐项复现原任务后，
supplemental_runs合并两个Top10、复用已有标签并生成缺口。独立SupplementalStore
沿用原SQLite修订控制和审计。ComparisonReviewService通过原create_app与静态页面提供
GET查询、PUT保存和导出，禁止在此实验里增加新查询。

下游score读取冻结原标签和新增人工标签，calculate_comparison对每个查询使用相同union候选池，
以gain=2^grade-1计算nDCG@5和@10，严格Precision只计grade=2；
30个固定查询完整后才汇总，nDCG无正增益时单列有效分母。
输出results.json、逐例缺口和report.md，不输出全库Recall或企业P95。

## 配置、产物与验证

实现入口：
- `agent/tools/compare_annual_retrieval.py`：prepare、serve、score。
- `agent/app/modules/retrieval_lab/comparison.py`：冻结输入适配、实际排序复用、标签合并、盲标存储和评分。
- `agent/tests/test_retrieval_comparison.py`：真实分支、缺标阻断、共同理想排序、证据漂移、参数严格读取、API持久化与修订冲突。

prepare可指定 `--review-dir --package-dir --model-dir --embedding-python --legacy-config --device --output-dir`。
默认使用既有应用Python3.11与隔离编码Python，本机真实BGE；没有新增模型下载或全局依赖。
每次prepare必须使用新目录，避免覆盖实验。serve指定 `--directory --port`；
score指定 `--directory`。完整参数和执行时源码SHA在config.json。
本轮只新增对照模块、CLI、测试与本文档，现有业务服务源码与配置不改。

主产物：
- `comparison.json`：冻结两策略输出、查询及trace。
- `query-vectors/`、`evidence-manifest.json`：真实查询向量、索引和参数身份。
- `pending-pairs.jsonl`、`supplemental.sqlite3`：154项缺口及人工保存库。
- `results.json`、`report.md`：当前状态及后续对比结果。
- `verify_replay.py`、`validation.json`：独立重算与核对证据。

测试入口（WSL）：

```bash
cd agent
../var/news-events-annual-20261027/runtime/app-venv/bin/python -B -m unittest \
  tests.test_retrieval_comparison tests.test_retrieval_lab_store \
  tests.test_retrieval_lab_api -q
```

真实运行的复核不需要GPU，使用同一应用Python执行结果目录的verify_replay.py。
后台8879读接口已从桌面验证；保存路径在独立临时测试库验证，不向真实补标库写入假标签。

## 剩余工作

首先完成人工新增154项，再score计算有效对比。当前是单人、开发性质的问句检索消融：
此前已经查看单策略结果，没有预先冻结独立事件测试组，不能将这30条直接作为独立test。
正式简历结论仍需人工确认业务任务、源新闻关联检索任务、事件组隔离与独立复核。
事实覆盖替代需另设实验；企业ANN容量、端到端P95和并发负载需独立测量。
