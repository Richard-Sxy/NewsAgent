# 离线语义召回实验：BGE + hnswlib vs FastGPT fullText

日期：2026-09-14
范围：`tencent-news-crawler/experiments/offline_recall.py`
目标：在**完全离线**条件下复现知识库召回链路，并与 FastGPT 在线关键词召回对比，
验证 `docs/knowledge-base-scale-design.md` 中“混合检索 + 融合重排”的判断。

---

## 一、结论

在全部 100 道评测题上：

| 方法 | recall@1 | recall@5 | recall@10 | MRR |
| --- | --- | --- | --- | --- |
| FastGPT fullText（在线基准） | 0.720 | 0.720 | 0.720 | 0.720 |
| 离线 BGE-large-zh + hnswlib | 0.600 | 0.840 | 0.850 | 0.702 |
| 混合（fullText top1 + BGE） | **0.720** | **0.940** | **0.940** | **0.812** |

1. **关键词召回 top-1 更准**（0.72 vs 0.60），适合专名、数字、精确事实。
2. **本地语义召回在 k=5/10 覆盖更强**（0.85 vs 0.72），补足同义与改写。
3. **两者融合后 @5/@10 提升到 0.94，MRR 到 0.812**，明显优于任一单路。

这与设计稿“结构化过滤 + 关键词 + 向量 + 重排，单一手段不够”完全一致。

---

## 二、语料与一个重要偏差

| 语料 | 文章数 | 切片数 | 覆盖评测题 |
| --- | --- | --- | --- |
| 本地 JSON 缓存 `data/articles/` | 12,297 | 43,050 | **49 / 100** |
| FastGPT 原文库（Mongo `dataset_datas`） | 12,187 collections | 36,379 | **100 / 100** |

**关键发现**：本地 JSON 缓存是 FastGPT 已入库内容的一个子集（FastGPT 原文库有 19,436 条
`dataset_datas`），100 道题里有 51 道题的目标文章**根本不在 JSON 缓存中**。

因此用 JSON 语料直接评测会得到严重偏低的假象：

| 方法 | recall@1 | recall@5 | recall@10 | MRR |
| --- | --- | --- | --- | --- |
| 离线 BGE（JSON 全量 100 题） | 0.320 | 0.420 | 0.440 | 0.360 |

在两边都有的 49 题共同子集上对比才公平：

| 方法（49 题共同子集） | recall@1 | recall@5 | recall@10 | MRR |
| --- | --- | --- | --- | --- |
| fullText | 0.776 | 0.776 | 0.776 | 0.776 |
| BGE + hnswlib | 0.653 | 0.857 | 0.898 | 0.734 |

结论：**离线召回评测必须以“目标文章确实在语料中”为前提**，否则指标无意义。

---

## 三、BGE 分主题表现（完整语料，100 题）

| 主题 | n | recall@1 | recall@5 |
| --- | --- | --- | --- |
| 体育 | 24 | 0.750 | 0.958 |
| 财经 | 24 | 0.667 | 0.833 |
| 汽车 | 18 | 0.611 | 0.889 |
| 科技 | 24 | 0.500 | 0.708 |
| 未分类 | 10 | 0.300 | 0.800 |

科技类 `@1` 最低，说明技术类问题的答案更分散、跨切片，单靠向量 top-1 不足，需要重排。

---

## 四、实验配置

### 环境

| 项 | 值 |
| --- | --- |
| Python | 3.11.9 |
| torch | 2.14.0+cu130 |
| sentence-transformers | 6.0.1 |
| hnswlib | 0.8.0 |
| GPU | NVIDIA RTX 4070（12GB） |
| 实验 venv | `tencent-news-crawler/.venv-embed`（已 gitignore） |

### 模型

- `BAAI/bge-large-zh-v1.5`，1024 维，最大 512 token
- 本地路径：`tencent-news-crawler/data/models/bge-large-zh-v1.5/`（1.30GB，ModelScope 直连下载）
- 查询侧加指令：`为这个句子生成表示以用于检索相关文章：`；文档侧不加
- 向量 L2 归一化，内积等价余弦

### 索引与切片

| 项 | 值 |
| --- | --- |
| 切片（JSON 语料） | chunk_size=500，overlap=64，每片前置标题 |
| 切片（FastGPT 语料） | 复用 FastGPT 原有切片（含标题上下文） |
| HNSW space | `ip`（内积） |
| HNSW M | 32 |
| ef_construction | 200 |
| ef（查询） | 64 |
| topk | 10 |

### 耗时

| 阶段 | 规模 | 耗时 |
| --- | --- | --- |
| 向量化（JSON 语料） | 43,050 切片 | 911.6s |
| 向量化（FastGPT 语料） | 36,379 切片 | 763.2s |

---

## 五、混合召回的定义

本实验的“混合”是离线粗融合，**不是** FastGPT 原生 RRF：

```text
候选序列 = [fullText 第一名] + [BGE top-10（去重、去掉与 fullText 重复项）]
再按该序列计算 recall@k / MRR
```

之所以粗：FastGPT `searchMode=fullTextRecall` 每题只返回 1 条，无法提供 k>1 的关键词候选，
因此无法做标准 RRF。要得到真正的 RRF，需要多路各返回 top-k 候选后再融合。

---

## 六、产物与复现

### 产物

| 路径 | 说明 |
| --- | --- |
| `experiments/offline_recall.py` | 离线流程脚本（extract/embed/index/eval） |
| `data/experiments/offline_recall/` | JSON 语料实验（chunks/embeddings/index/结果） |
| `data/experiments/offline_recall_fg/` | FastGPT 完整语料实验 |
| `data/models/bge-large-zh-v1.5/` | 本地嵌入模型 |

以上 `data/` 产物与 `.venv-embed` 均已在 `.gitignore` 中排除。

### 复现

```bash
cd tencent-news-crawler

# JSON 语料（本地 1.2 万篇）
.venv-embed/bin/python experiments/offline_recall.py extract
.venv-embed/bin/python experiments/offline_recall.py embed --device cuda --batch-size 64
.venv-embed/bin/python experiments/offline_recall.py index --m 32 --ef-construction 200 --ef 64
.venv-embed/bin/python experiments/offline_recall.py eval --device cuda --topk 10 --ef 64

# FastGPT 完整语料（需导出后给每个子命令加 --exp-dir）
.venv-embed/bin/python experiments/offline_recall.py embed \
  --exp-dir data/experiments/offline_recall_fg --device cuda
```

FastGPT 语料导出（Mongo 原文库 dataset `6a82c8acf4e9a82e680be9eb`）：

```bash
docker exec -i fastgpt-mongo mongo -u <user> -p <pwd> --authenticationDatabase admin \
  --quiet fastgpt < export_chunks.js > fg_chunks_raw.jsonl
```

再按 `collectionId` 还原 `news_id`，转成 `chunks.jsonl`（见实验过程）。

---

## 七、热层（L0）HNSW 构建

对应设计稿 L0 热层：近 N 天、全精度、内存 HNSW（`m=32, ef_construction=200`）。

### 命令

```bash
.venv-embed/bin/python experiments/offline_recall.py hot \
  --exp-dir data/experiments/offline_recall_fg --days 30
```

`--days` 可调，`--now YYYY-MM-DD` 可选（默认取语料最大发布时间，本语料为 2026-09-09）。

### 结果

| 热层 | cutoff | 文章 | 切片 | fp32 | fp16 存档 | 可答题 |
| --- | --- | --- | --- | --- | --- | --- |
| hot_30d | 2026-08-10 | 12,184 | 36,350 | 142.0MB | 71.0MB | **97 / 100** |
| hot_7d | 2026-09-02 | 10,547 | 27,929 | 109.1MB | 54.5MB | 0 / 100 |

热层索引检索基线（hot_30d）：`recall@1=0.590, @5=0.820, @10=0.830, MRR=0.689`
（对比全量语料 0.600/0.840/0.850/0.702，差异来自 3 道题目标不在热层）。

### 产物

`data/experiments/offline_recall_fg/hot_30d/`：

| 文件 | 说明 |
| --- | --- |
| `chunks.jsonl` | 热层切片，附 `global_index` 指回全量行号 |
| `embeddings.npy` | fp32 向量（hnswlib 需要） |
| `embeddings_fp16.npy` | fp16 存档（体积减半，演示存储策略） |
| `index.bin` | hnswlib 索引（internally fp32） |
| `manifest.json` | 窗口、参数、存储字节、`answerable_question_ids` |

热层目录可直接作为 `eval` 的 `--exp-dir` 使用：

```bash
.venv-embed/bin/python experiments/offline_recall.py eval \
  --exp-dir data/experiments/offline_recall_fg/hot_30d --topk 10 --ef 64
```

### 手接召回优化时的注意

1. **测热层召回应只用 `answerable_question_ids`（97 题）**，否则未覆盖的题会拉低指标。
2. 本语料只有约 40 天，30 天窗口≈全量；要观察分层效果应把 `--days` 调小（如 7/14）。
3. hnswlib 只支持 fp32，`index.bin` 仍是 fp32；fp16 仅作存储演示。
4. `now` 用的是语料最大发布时间，不是真实系统时间；真实场景应改用运行时刻。

---

## 八、冷/温/热三层召回骨架

对应设计稿第 2 节“分层存储与索引”和第 1.2 节“冷热层更新策略”，落在
`experiments/tiered_recall/`，用于做多层召回优化。

### 模块

| 文件 | 职责 |
| --- | --- |
| `policy.py` | `TierPolicy`：按时间定层 + 按热度晋升/提前降温（纯函数） |
| `heat.py` | `HeatTracker`：指数半衰期衰减 `H <- H*2^(-elapsed/tau)+weight`，附 Lua 参考 |
| `quantize.py` | fp32/fp16/int8/binary 量化与还原、按精度记账存储字节 |
| `store.py` | `TierStore`：每层一个 hnswlib 索引，支持增/删/检索/持久化 |
| `ledger.py` | `TierLedger`：分区层归属、个体层覆盖、晋升连续计数（JSON，原子写） |
| `migration.py` | `TierMigrationService`：分区整块迁移 + 热度例外晋升，先写后删、幂等 |
| `router.py` | `TieredRouter`：per-tier / 全层 RRF / 级联升级，支持层权重 |
| `corpus.py` | 语料读取、发布时间解析、按分区切层 |
| `build_tiers.py` / `simulate.py` / `evaluate.py` | 构建 / 迁移演练 / 多层评测 |

### 命令

```bash
# 构建三层（rank 或 age 切分；层精度 hot=fp32, warm=int8, cold=binary）
.venv-embed/bin/python -m experiments.tiered_recall build \
  --exp-dir data/experiments/offline_recall_fg \
  --out-dir data/experiments/tiered_fg \
  --assignment rank --hot-parts 1 --warm-parts 1 --warm-ttl-days 60

# 迁移演练（合成热度 -> 分区迁移 + 热度晋升，写独立目录）
.venv-embed/bin/python -m experiments.tiered_recall simulate \
  --out-dir data/experiments/tiered_fg --now 2026-10-15 --runs 2

# 多层召回评测
.venv-embed/bin/python -m experiments.tiered_recall eval \
  --out-dir data/experiments/tiered_fg --questions tests/retrieval_questions.json \
  --tier-weights hot=0.2,warm=1,cold=1
```

### 构建结果（rank 切分，2026-09/08/07）

| 层 | 精度 | 切片 | 存储 |
| --- | --- | --- | --- |
| hot | fp32 | 27,929 | 114.4 MB |
| warm | int8 | 8,421 | 8.7 MB |
| cold | binary | 29 | 3.7 KB |

### 多层召回（100 题）

| 模式 | recall@1 | recall@5 | recall@10 | MRR |
| --- | --- | --- | --- | --- |
| hot_only | 0.000 | 0.000 | 0.000 | 0.000 |
| warm_only | 0.660 | 0.870 | 0.890 | 0.750 |
| cold_only | 0.030 | 0.030 | 0.030 | 0.030 |
| all_tiers（等权 RRF） | 0.000 | 0.800 | 0.880 | 0.372 |
| all_tiers（hot=0.2,warm=1） | **0.660** | **0.880** | **0.920** | 0.733 |
| cascade | 0.000 | 0.000 | 0.000 | 0.000 |

要点：

1. 评测题目标几乎都在 **warm（2026-08）**，所以 `hot_only=0` 是正常的；这正是
   分层要解决的问题。
2. 三层精度不同 → 原始相似度**不可比**。等权 RRF 能让 `@5/@10` 回升，但 top-1 被更近的
   hot 层占据；调层权重（`hot=0.2,warm=1`）后 `@1` 恢复并超过单层。
3. `cascade` 是**延迟优化**：hot 结果足够就不再下探，因此在本题集上等于 hot_only；
   要按置信度升级需要跨层可比的分数（建议配合重排）。
4. binary（cold）单层 `@1=0.03`，量化损失极大；int8（warm）`@1=0.66`，损失可接受。

### 迁移演练（now=2026-10-15，2 轮）

第 1 轮分区迁移：`2026-08 warm->cold`（1,637 向量）、`2026-09 hot->warm`（10,547 向量）。
第 2 轮热度例外：79 条（连续 2 轮达标）晋升，如 warm->hot / cold->warm。
迁移后 hot 17,382 / warm 17,410 / cold 1,587；迁移后 `cold_only`（binary）拿到
`@1=0.590 @5=0.730 @10=0.780`，说明 8 月文章已随分区下沉且二值向量仍可检索。

### 手接召回优化时的注意

1. 层权重、`rrf_k`、每层候选数 `per_tier_k` 都是旋钮，可直接调参对比。
2. 当前无查询时间过滤；真实系统应按 query 时间窗裁剪层，才能解决 top-1 被近层占据。
3. `now` 为仿真日期；`news_meta.json` 不随迁移更新（评测里的 “expected tier”
   仍是初始层），迁移只改 store 与 ledger。
4. 迁移是进程内内存操作，生产应换成 Milvus alias / 分区加载状态切换。

---

## 九、尚未完成 / 注意

1. **在线语义路径仍不可用**：aiproxy → 阿里云 DashScope `text-embedding-v4` 账户欠费
   （错误码 `Arrearage`），本实验是本地替代，未修复线上。
2. **评测集有偏**：每题仅 1 个 `expected_urls`，无负例与多正例，指标偏乐观
   （与 `docs/news-ingest-recall-optimization.md` 判断一致）。
3. fullText 基准每题只返回 1 条，其 `@5/@10` 与 `@1` 恒等；本报告“混合”为粗融合。
4. 索引为单机 hnswlib，尚未实现设计稿的冷热分层、int8/PQ 量化、Milvus 分区与 DiskANN。
5. `experiments/offline_recall.py` 目前是实验脚本，尚未补 pytest；当前仓库存量环境无法跑全量测试。
