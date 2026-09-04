# 新闻事件聚类 Baseline

## 目标

第一版用于建立可复现的实验基线，而不是替代后续语义模型。它将同一时间窗口内、标题和正文高度相似的新闻合并为事件簇，并输出供人工复核的事件级数据集。

## 特征与方法

- 英文、数字词元和中文字符二元组。
- 标题 TF-IDF 余弦相似度，权重 0.65。
- 标题加正文前 800 字的 TF-IDF 相似度，权重 0.25。
- 指数时间衰减，权重 0.10，默认衰减周期 1.5 天。
- 仅比较发布时间相差不超过 3 天的文章。
- 综合分数不低于 0.48 时连接文章，使用连通分量形成事件簇。

阈值 0.48 来自首轮 300/500 篇语料体检。该结果仅是初始参数，必须在人工 Gold Dataset 上重新选择。

## 构建数据集

```bash
python -m scripts.build_event_dataset \
  --output data/events/event_clusters.json
```

输出中 `needs_review=true` 表示多文章事件簇，应优先人工确认是否发生误合并。单文章簇也需要抽样，并为可能被错误拆分的文章补充 `same_event` 标注。

## 人工标注格式

创建 JSONL 文件，每行一对文章：

```json
{"left_article_id":"A1","right_article_id":"A2","relation":"same_event","note":"同一发布会的两篇报道"}
{"left_article_id":"A1","right_article_id":"A3","relation":"unrelated","note":"主体不同"}
```

`relation` 可选：

- `same_event`：同一现实事件。
- `related_event`：主题相关但不是同一事件。
- `unrelated`：无关。

标注时应混合正例与难负例。难负例包括同一公司不同事件、同一人物不同发言、同一赛事不同场次，以及标题相似但时间明显不同的新闻。

## 评测

```bash
python -m scripts.evaluate_event_clustering \
  --clusters data/events/event_clusters.json \
  --annotations datasets/annotations/event_pairs.jsonl
```

输出 Pairwise Precision、Recall、F1 和 Accuracy。没有人工标注前，簇数量和最大簇大小只能用于体检，不能作为算法准确率。

## 已知局限

- 中文字符二元组不能理解实体别名和事件论元。
- 连通分量可能因中间文章产生链式误合并。
- 仅使用发布时间，尚未抽取真正的事件发生时间。
- 来源可信度、事件类型和实体冲突尚未进入相似度。
- 当前复杂度约为时间窗口内两两比较，数据扩大后需增加候选召回索引。

下一版应在人工标注集上比较 Sentence Embedding、实体重合度、事件类型与关键论元冲突特征，并使用 DBSCAN 或层次聚类替代单一阈值图。
