# 本地真实新闻召回实验

本实验帮助 NewsAgent 在企业 Embedding 与生产向量平台接入前，获得可复跑的本地召回
质量、索引近似损失和性能观测。实验使用完整公开新闻、真实开源 Embedding 权重与
HNSW 索引；不使用应用中的确定性模型替身，也不读取 `.env` 或企业配置。

这是一条隔离的离线实验链路，不改变 `agent/` 的知识 API、热点 Worker、生产 Bundle、
审批边界或默认 PostgreSQL/HOT 检索装配。`sentence-transformers` 和 `hnswlib` 仅为
离线模型推理、索引算法依赖，不是 Agent/知识库运行时框架。

## 2026-10-06 的实际结果

- 请求 465 个公开新闻 URL：115 个题集目标 URL、350 个固定种子抽样的其他新闻。
  成功解析 441 篇，24 个页面失败；失败不是静默跳过，均进入采集 manifest。
- 441 篇完整新闻切成 2223 个切片；150 个历史问题中 144 个正例在库，6 个库外问题。
- 模型 `BAAI/bge-small-zh-v1.5`，512 维，CPU 4 线程；模型固定 commit 为
  `7999e1d3359715c523056ef9478215996d62a620`。模型与索引为本次实际下载/构建。
- 标题加正文每块最多 400 字，正文重叠 64 字，模型上限 512 tokens；本次无截断。

| 方法 | 全量150题 Recall@1 | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| BM25，中文字与二元词元 | 91.33% | 96.00% | 96.00% | 0.9317 | 0.9388 |
| BGE 精确向量搜索 | 81.33% | 95.33% | 96.00% | 0.8712 | 0.8935 |
| BGE + HNSW | 81.33% | 95.33% | 96.00% | 0.8712 | 0.8935 |
| BM25 + BGE/HNSW + RRF | 88.67% | 96.00% | 96.00% | 0.9186 | 0.9291 |

全量分母保留6个库外问题。可回答144题的 BGE/HNSW Recall@5 为99.31%，BM25与混合
召回为100%。每题目前只有一个标注正例，HitRate与Recall数值相同；Precision@10
最高只有0.1是单正例标签的结果，不能解释为九篇其他新闻都经人工判定不相关。

本次 HNSW 与精确搜索的新闻 Top10 集合一致率为99.87%，切片Top10一致率为100%。
新闻边界上多个切片聚合、同分候选的排序可产生少量集合差异。

本机观测：文档 Embedding 53.30秒，约41.70切片/秒；查询 Embedding P95 26.00ms，
HNSW候选搜索P95 0.98ms，两者相加的P95 26.52ms；索引构建0.77秒，实际索引文件
4.66MiB，文档向量文件4.34MiB。全进程峰值RSS950.86MiB包含模型、词法基线和所有
数组，不是HNSW独占内存。

HNSW先为融合准备50篇新闻候选，再截断Top10评质量；实际取200至400切片，
`effective ef = max(base ef, candidate_count)` 为200至400，不能将本次结果描述成
固定efSearch64或独立Top10服务延迟。每题只测一次，模型/索引已加载且只部分预热，
不包含企业网络、并发负载或Agent执行。小语料上的精确扫描也很快，不能据此推算
百万级索引的性能。

实际报告：`news/data/local_recall/runs/public-v1/report.md` 和 `report.json`。
逐题结果、全部K值、库外问题、模型版本、依赖版本、参数和资产哈希均在JSON中。
这些本地输出及公开正文、模型、向量被目录内 `.gitignore` 排除，不进入生产代码。

## 完整调用链与边界

```text
CLI题集 expected_urls + 公开URL候选列表
→ collect_recall_articles.plan_urls（校验、去重、seed抽样）
→ collect_articles / fetch_html / parse_article（有界并发、正文容器清洗）
→ 每篇文章JSON + collection_manifest.json（失败清单和冻结哈希）
→ recall_dataset.load_articles_chunks（只读manifest选中语料，版本/ID/哈希校验）
→ load_questions（独立读取查询和URL正例，不把reference_answer加入索引）
→ download_recall_model（公开Hub版本解析、safetensors与配置、文件哈希）
→ SentenceTransformer（标题+正文切片；查询加BGE检索指令；L2归一化）
→ 精确点积 / HNSW / 字与二元词元BM25
→ hnsw_news扩大切片候选，按news_id补齐新闻TopK
→ rrf_news融合候选排名
→ query_metrics / aggregate_metrics / ann_overlap
→ report.json / report.md / chunks.jsonl / vectors.npy / index.bin
```

核心文件位于 `news/experiments/`。主入口是 `local_recall_benchmark.py`；
`recall_dataset.py`负责语料与题集，`recall_metrics.py`负责纯Python指标。
外部网络依赖只有公开新闻页面与公开模型Hub；下载后基准主入口离线执行。
依赖声明在 `news/requirements-recall.txt`；本次实际安装的全部版本冻结在
`news/requirements-recall.lock.txt`，本机隔离环境为 `news/.venv-recall/`。

文章目录可替换为获准本地使用的企业新闻JSON，最小格式如下；实验查询仍须独立标注。
当前示例身份解析限定标准 `https://news.qq.com/rain/a/<news_id>`，企业其他URL格式
需要在明确ID合同后增加适配，不能静默从文件名猜ID。

```json
{
  "url": "https://news.qq.com/rain/a/EXAMPLE_NEWS_ID",
  "news_id": "EXAMPLE_NEWS_ID",
  "title": "示例新闻标题",
  "content": "获准用于本地评测的正文",
  "publish_time": "2026-10-06T08:00:00+08:00",
  "content_version": 1
}
```

题集格式沿用 `tests/retrieval_questions*.json` 的 `id/question/topic/expected_urls`。
允许多条 `expected_urls`；跨文件重复题ID以文件名命名空间区分。当前输入适配器的
标签为二元相关；指标核心支持分级相关，但分级标签JSON适配仍待补充。

## 重跑

需要 Python 3.11+。当前本机已安装隔离环境与模型，直接执行下方基准即可。
在新环境中，先用3.11+解释器创建 `.venv-recall` 并安装 `requirements-recall.txt`。
本机现有 `news/.venv/bin/python` 是3.11，可用于创建；系统默认python3是3.9，不能
直接用来安装这些模型依赖。

从仓库根目录：

```bash
news/.venv/bin/python -m venv news/.venv-recall
news/.venv-recall/bin/python -m pip install -r news/requirements-recall.lock.txt
cd news
.venv-recall/bin/python -m experiments.download_recall_model \
  --revision 7999e1d3359715c523056ef9478215996d62a620 \
  --output-dir data/local_recall/models/bge-small-zh-v1.5
.venv-recall/bin/python -m experiments.collect_recall_articles \
  --questions tests/retrieval_questions.json tests/retrieval_questions_semantic.json \
  --url-file data/urls-2026-09-03-articles-extra-2000.txt \
  --distractors 350 --seed 42 --workers 4 --timeout 15 \
  --output-dir data/local_recall/articles-v1
.venv-recall/bin/python -m experiments.local_recall_benchmark \
  --articles-dir data/local_recall/articles-v1 \
  --questions tests/retrieval_questions.json tests/retrieval_questions_semantic.json \
  --model data/local_recall/models/bge-small-zh-v1.5 \
  --output-dir data/local_recall/runs/public-v2 \
  --device cpu --threads 4 --batch-size 24 \
  --chunk-chars 400 --overlap 64 --ks 1 5 10
```

采集器对部分失败返回非零退出码并保留成功正文及manifest，应先检查失败范围再决定
是否跑这批不完整语料。重跑同目录会复用合法正文缓存，但会重新尝试此前失败的页面，
因此可能改变语料；重现public-v1应保留原始目录和manifest，仅重跑基准。不要在
已冻结的实验目录里混入其他新闻。基准拒绝覆盖已有report，除非显式`--overwrite`。

`--snapshots FILE...` 是历史返回片段池的显式替代入口，只适合冒烟验证；候选池偏差
可能抬高召回率，不能与完整正文基准混报。本次public-v1没有使用该入口。

## 指标和配置

- `HitRate@K`：新闻TopK是否至少含一个标注相关项。
- `Recall@K`：新闻TopK命中的正例数／本题全部标注正例数，逐题宏平均。
- `Precision@K`：命中的标注正例数／K；少于K结果也使用K作分母。
- `MRR@K`：第一个标注相关项的倒数排名，超过K记0。
- `nDCG@K`：对相关等级的`2^grade-1`增益做位置折扣并归一化；本次全为二元等级。
- ANN一致率：HNSW与同模型精确搜索的TopK集合交集比例，独立于人工相关性标签。
- 语料覆盖率：区分全量、全部正例在库、部分正例在库和完全库外问题。

主配置是语料/题集路径、模型目录、CPU/MPS/CUDA设备、线程与batch大小、切片大小与
overlap、模型token上限、TopK、HNSW的M/efConstruction/base efSearch及随机种子。
默认本地模型512维不是企业Embedding的既定维度；接企业模型时必须重新生成整个
匹配版本的向量，并重算指标。

## 测试入口与剩余TODO

从 `news/` 执行：

```bash
.venv-recall/bin/python -m unittest discover -s tests -p 'test*recall*.py' -v
```

本次专项49个测试通过，依赖检查 `pip check` 通过。测试覆盖多正例与分级指标、重复切片补齐、库外标签分母、URL/版本/身份、冻结哈希、
正文清洗、公开下载边界、缓存、抽样复现和失败清单。测试不调用企业服务或网络。

后续：

1. **独立人工评测集**：按“问题找来源新闻”和“新闻找关联报道”分开标注，增加同事件
   多篇相关、主题相同但事件不同、时间错误等硬负例，以及单独评估的无答案问题。
2. **语料规模与代表性**：扩大授权语料、跨日期/频道分层抽样，设置开发集与冻结测试集；
   当前结果没有证明真实业务分布上的质量，也没有证明混合检索优于BM25。
3. **性能**：独立TopK与融合候选路径分开计时，完整预热、重复采样、并发负载、长稳、
   向量缓存与重建峰值；当前不报告生产QPS、P99或企业端到端SLO。
4. **企业联调**：替换获批Embedding接口，接生产向量平台或本地Milvus；补齐内容更新/
   下架删除、租户与细粒度ACL；本次hnswlib不是企业平台或真实模型质量验收。
5. **分层实验**：在全库基线之外验证冷热路由、预算、量化、迁移与恢复，不能把浮点
   roundtrip估值当真实量化索引内存。现有历史分层实验脚本未在本次修改或验收。

参考：[BGE官方模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)、
[HNSW官方实现](https://github.com/nmslib/hnswlib)。
