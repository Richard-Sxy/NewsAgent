# 分层检索简历指标执行说明

本次对应的简历内容是：针对历史新闻增长与跨层排序偏差，构建热／温／冷三级检索，
结合跨层 RRF 与主体、事件、时效重排；在独立查询上评价相关报道排序与检索耗时。

**当前尚无可填写的语义质量成绩。** `related_news_review_v1` 是 16 条源新闻的人工审核
起始材料，尚未确定事件分组、split 或 gold，不能把 16 直接填进简历的 N。
本轮只准备材料与离线评分入口，没有执行真实检索质量实验。

## 1. 本次到底评什么

评测任务是“给定一篇真实新闻，找出同主体、同事件或有解释价值的历史关联报道”。
输入必须绑定真实 `source_news_id`、来源、发布时间和正文版本。源新闻本身需要排除，
相关报道由人工判断；源新闻不是自动正例。

真实业务调用链：

```text
HotNewsWorker / 热点运行
→ build_native_knowledge_index / build_native_knowledge_search
→ HotNewsEnrichmentService.enrich
→ NewsContentRepository 按 source_news_id 取规范正文
→ RelatedNewsSearchQuery：源标题 + 源摘要
→ Query Embedding → 热／温／冷并行查询
→ 生命周期与版本门禁、必要时补搜 → 新闻级去重 → 跨层 RRF
→ 补全候选标题、来源、发布时间
→ RelatedNewsReranker：主体／事件／标题关键词／时间接近度
→ 排序后的 news_id → 人工标签 → 确定性评分
```

`HotNewsEnrichmentService.enrich` 的默认配置为候选 20 篇、最终 3 篇。本次 nDCG@5
需要显式指定 `candidate_limit=20, related_limit=5`，记录为离线 Top5 配置；不得把默认
Top3 结果称为完整 Top5 检索。若真实返回不足 5 篇，保留实际结果，不能补造新闻。

业务重排读取源新闻的标题、摘要、正文与发布时间；向量查询文本则是标题与摘要的拼接。
自由问句检索需要另立任务，不能将问句塞进伪造的 `NewsContent`，据此宣称测量了生产
关联新闻链。已存在的 `MulRecall` 自由问句探索数据不能直接充当本次独立 test。

关键源码：

- `app/hot_news_worker.py`：知识检索工厂与生命周期规范正文仓库接线。
- `app/model_runtime/knowledge_factory.py`：选择普通或生命周期检索客户端。
- `app/modules/hot_news/analysis/hot_news_enrichment.py`：输入转换、召回、候选补全、重排。
- `app/clients/knowledge_base.py`：查询与结果契约。
- `app/modules/hot_news/analysis/related_news_reranker.py`：确定性业务重排。

## 2. 固定一条实现，不能混合两种 RRF

当前代码有两条路径：

| 项目 | 普通 `TieredVectorKnowledgeSearchClient` | `LifecycleKnowledgeSearch` |
| --- | --- | --- |
| 主要文件 | `app/retrieval/tiered_vector.py` | `app/modules/knowledge/tier_lifecycle_query.py` |
| 有效性校验 | 索引 Port 过滤 | 逐层台账门禁、补搜及最终台账门禁 |
| 跨层融合 | 每层新闻去重后累加 `weight/(k+rank)` | 台账仅允许新闻当前有效层，取该层 `weight/(k+rank)` |
| 结果分归一 | 除以各层 `weight/(k+1)` 的总和 | 除以最大层权重 `max(weight)/(k+1)` |

`RelatedNewsReranker` 使用的是上述**归一 RRF 分数**，不是原始余弦／内积分。
因此两条链即使使用相同重排权重与阈值，也可能有不同的过滤结果；旧探索结果不能直接
作为新生命周期链的 baseline。运行规格必须记录实际类、配置内容哈希和源码指纹。

若目标是当前生命周期架构，应固定：真实三层索引、相同 Embedding、相同规范内容与
PostgreSQL 台账门禁，再改变待验证参数。若使用静态本地索引，应明确叫“离线静态分层
检索评测”，不宣称验证了生产迁层、更正／撤稿一致性或企业平台性能。

源码中生命周期按年龄分层，支持显式回热；这不自动证明已接通真实访问热度驱动。
当前 BGE snapshot 后端仅暴露 HOT 层、要求 float32；Milvus 的索引由外部平台建立，
生命周期字段校验要求 `FLOAT_VECTOR`。因此本次材料不能支持“生产 FP16 HNSW、温冷
低精度存储”等额外措辞，也不能拿实验目录的量化实现代替生产配置证据。

## 3. 对照方案：先测清楚重排收益

建议冻结两组：

- **A：相同三层召回、过滤、新闻去重和 RRF，不做业务重排。**
- **B：与 A 相同，再加入当前主体、事件、关键词及时间重排。**

两组使用同一查询、同一语料与时间截面、同一候选数、Embedding、索引版本和运行环境。
该比较只证明“在三层 RRF 候选上的业务重排收益”，不能单独证明 RRF 或分层本身的收益。

若需要证明 RRF，另加固定候选上的“原始向量分合并”消融，并明确不同距离的方向与尺度；
混用内积和二值 Hamming 距离时不能直接按数值大小排序。若不是过去真实线上方案，称为
“消融对照”，不要将其描述为原线上 baseline。若需要证明热／温／冷分层的收益，还需
同语料单层对照，以及容量、内存、构建与迁移成本证据。

当前业务重排基础权重为 RRF .40、主体 .25、事件 .15、关键词 .15、时间 .05；
主体冲突或候选标题缺少同主体会进一步降权，默认阈值 .30。主体／事件来自有限词表
和正则规则，时间分是与源新闻发布时间的绝对差，不是对 `as_of` 的衰减函数。
未来证据必须在冻结语料时排除，不应指望该重排器替代时间门禁。

## 4. 人工材料与 N 的确定

审核入口为 `evaluation/datasets/retrieval/related_news_review_v1/`：

- `queries.review.jsonl`：待确认的源新闻检索任务。
- `source-evidence.jsonl`：源新闻证据，供核对标题、摘要、正文与时间。
- `candidate-evidence.jsonl`：预审候选新闻的正文证据。
- `judgments-reviewer-a.jsonl`、`judgments-reviewer-b.jsonl`：两份独立空白标注表，
  每份对应 16 个任务、每任务 16 个候选，共 256 对；不能由同一人填写两份冒充独立评审。
- `ANNOTATION_GUIDE.md`：阅读顺序、标注字段与预审注意事项。
- `manifest.json`、`README.md`：准备范围、文件身份和填写说明。

上述候选池是便于试标的 **preview／provisional 预审池**，来源包括旧的源切片向量近邻、
词面匹配和随机抽样，尚未覆盖本次真实 A/B 输出。可以先用它确认任务及相关性分级，
不能将预审池直接冻结后作为最终质量分数。正式运行后必须合并 A/B 等预登记方案的候选，
对新增或发生变化的查询—候选对补充独立盲审，再核对两个方案 Top5 的完整标注覆盖。

用户需要确认业务任务、材料内容、事件组及相关性标准。先按事件主线分组，同一事件及其
更正链不能跨 `development/validation/test`；用 development 调参、validation 选方案，
test 只做最终评估。16 条起始材料不能因名字或文件位置而自动获得独立性。

**N = 冻结的 test 中、未参与调参且有人工确认正例的查询数。** 同一查询的多次请求、
多个候选、多个层、同事件的多篇报道都不能重复增加独立查询数；同时报告事件组数量。
无答案任务单独统计拒答／误召回，不计入该 nDCG 样本数。不得看到运行失败后剔除查询。

将 A、B 及预先登记的补充检索方案候选合并，隐藏方案名称和原排序，供真实独立评审者
按同一规则盲审。当前评分合同要求两位不同评审者；有分歧时由第三位仲裁。不能用助手
重复两次作答冒充两个独立人工评审。全部将要评分的 Top5 必须有标签；未判不是 0 分。

标签为 0–3 分，具体分级说明应先经业务确认。评分器把 `grade >= 2` 视为正例。
冻结材料应保留：人工身份与意见、仲裁结果、正文证据哈希、语料成员资格、`as_of`、
时间范围、分组与 split、候选池构建方式、索引包与配置／源码身份。

## 5. 评分口径与命令

使用 `tools/score_retrieval_judgments.py`，已支持显式 `--ks 5`。这是**人工候选池上的
pooled exp2 nDCG@5**：

```text
gain = 2^grade - 1
DCG@5 = Σ gain(rank) / log2(rank + 1)
IDCG@5 = 同一人工候选池内，按 grade 降序排列的理想 DCG@5
nDCG@5 = DCG@5 / IDCG@5
A / B = 对同一固定 test 查询集合逐查询 nDCG@5 求均值
```

它不证明候选池外不存在更相关报道，不等同于全库完整相关性 gold。输出字段为
`metrics["pooled_ndcg_exp2@5"].mean`；样本数读 `answerable_queries`，并核对逐例完整性。
事件组 bootstrap 区间用于展示不确定性；小样本和少量事件组的证据仍有限。

预测文件每条形如：

```json
{"query_id":"<冻结查询 ID>","package_manifest_sha256":"<已核实的索引包摘要>","news_ids":["<排序后的新闻 ID>"]}
```

超时或错误须保留独立逐例执行记录。已尝试但没有输出的查询仍在分母，按预注册规则在
预测中保留空 `news_ids`；不能丢行，也不能将未执行查询伪装成已执行的空输出。

以下为**模板，所有尖括号项都尚待冻结核实，不可原样执行或编造哈希**。在 `agent/`
目录执行，A 与 B 各运行一次，输出路径使用新的实验目录，不能覆盖历史报告：

```bash
.venv/bin/python tools/score_retrieval_judgments.py \
  --queries '<冻结的 queries.jsonl 路径>' \
  --qrels '<完成双审和仲裁的 qrels.jsonl 路径>' \
  --predictions '<A 或 B 的 predictions.jsonl 路径>' \
  --expected-queries-sha256 '<预注册的查询文件 SHA-256>' \
  --expected-qrels-sha256 '<冻结标签文件 SHA-256>' \
  --expected-predictions-sha256 '<该次预测文件 SHA-256>' \
  --package-sha256 '<该次检索实际使用的冻结索引包 manifest SHA-256>' \
  --split test \
  --ks 5 \
  --output '<新实验目录>/score-A-or-B.json'
```

评分器会拒绝未审核／未冻结查询、事件跨 split、缺失预测、未判 TopK、自召回与文件身份
漂移。`status=scored` 只表示依照输入完成评分，`acceptance_status` 仍是 `not_approved`；
语料与证据的真实冻结审计不能只靠评分器代替。

离线评分测试入口：

```bash
.venv/bin/python -m pytest tests/test_retrieval_judgment_scoring.py -q
```

## 6. T 的计时边界

简历若写“检索 P95”，建议固定为 B 方案单源查询的业务检索边界：从进入
`HotNewsEnrichmentService.enrich` 到收到重排后 Top5，包括源内容读取、query Embedding、
三层搜索、门禁／补搜、RRF、候选元信息读取及业务重排；不含上游数仓取数、热点计算、
下游 LLM 分析或报告写作。计时依赖应使用与实际被测链一致的后端，缓存读取也要说明。

若另外测 HTTP API，明确客户端起终点、网络、鉴权、排队、反序列化是否包含。
`tools/benchmark_tier_lifecycle.py` 当前测 `/api/v1/knowledge/search`：包含该 API 的
Embedding、三层检索／门禁和融合，但**没有候选正文补全及业务重排**。其 P95 可单独
报告为“知识搜索 API P95”，不能移入上述完整重排链的 T。

固定硬件、并发、连接与缓存状态、预热次数、正式请求次数、超时和失败率，并同时报告
Embedding／搜索／门禁／融合／重排分项。P95 使用预先固定的计算方法；成功请求 P95
必须配合失败数与超时数，不能仅丢弃慢失败后报告漂亮值。重复请求用于时延分布估计，
不会增加语义质量的 N。旧 `native_recall`、ANN-only 或未含重排的 HTTP 耗时不能代填 T。

## 7. 收尾清单与允许结论

| 阶段 | 当前状态／待完成内容 |
| --- | --- |
| 真实运行 | TODO：冻结实际后端、三层构成、Embedding、内容和代码版本；运行 A/B 并保存逐例候选及错误 |
| 候选池盲审 | TODO：合并方案候选，真实双审、分歧仲裁；检查每个方案 Top5 均已判 |
| 冻结 | TODO：审核源新闻、事件分组、split、as_of、正文／索引／配置身份，保留开发与测试隔离证据 |
| 质量评分 | TODO：使用同一冻结 test 与 qrels 执行上述命令，核对 N、A、B 和逐例退化 |
| 延迟 | TODO：按预登记的同环境业务边界测 B，记录 P95、失败率与请求数 |
| 简历填写 | TODO：仅用该轮可追溯报告填写 N、A、B、T；未改善就如实报告，继续在 development 优化 |

工具调用链为：冻结 JSONL／哈希 → `load_jsonl` → `score` 校验并计算 → 机器报告；
离线评分只依赖 Python 标准库，不访问模型、数据库或外网。业务运行另外依赖 Embedding、
索引、规范正文仓库及可选 PostgreSQL 生命周期台账；关键配置为模型运行时、知识搜索、
生命周期 YAML，以及重排配置和候选／输出数量。本说明不激活新配置、不发布生产服务。

完成上述工作后，可支持的表述是：“在 N 条未参与调参的关联新闻测试查询及人工标注
候选池上，业务重排后的 nDCG@5 从 A 提升到 B；在所述环境与计时边界下 P95 为 T ms。”
若独立性或标注尚未完成，只能称探索／开发集结果；若只完成单测，只能称评分合同通过。
