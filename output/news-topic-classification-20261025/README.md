# 腾讯新闻栏目参考下的本地内容粗分类

原始盘点使用 `results-v2/`；`results/` 是首轮探索结果，保留供对照，不作为最终统计。
原始盘点阶段仅创建离线分析文件。随后用户确认保留10类并暂留不确定新闻，已执行当前库
清理：删除5,066篇，保留16,369篇。随后按用户要求将原4,714篇不确定新闻全部自动补标：
1,807篇目标类、2,895篇范围外、12篇综合/信息不足；当前目标类合计13,462篇，总新闻数不变。
当前结果见 [自动标注报告](./auto-annotations-report.html)，不需要用户人工补标。
完整调用链、模型与数据库配置、测试入口和限制见
[自动标注说明](../../agent/docs/news-auto-annotations-20261025.md)。
[首次清理记录](./prune-report.html) 与 [清理及恢复说明](../../agent/docs/news-category-prune-20261025.md)
保留历史口径；下文21,435条统计为清理前盘点。

## 输入和口径

- 实际数据库：`newsagent_unified_news_20261023`，租户 `public-news-local`。
- 固定内容 release：`news-0c74530f2864db20a73d7a96`。
- 统计单位：21,435 个不同新闻 ID；按相同正文去重为21,427条内容，不是事件数。
- 有正文8,441条、仅历史片段11,794条、仅标题1,200条。
- `tencent-web-reference.json` 保存2026-10-25实际读取腾讯官方网页HTML得到的导航链接及页面SHA。
- 主体使用科技、财经、AI、教育、国际、军事、娱乐、体育、汽车、房产、游戏、健康；
  NBA归入体育。国内时政、社会民生、文化历史、生活文旅是本地补充分组。
- 这是规则粗分，不是原始栏目、人工金标或经过质量验收的模型分类。每条保留主类候选、
  次类候选、命中词、分数和复核标记；同分按稳定ID排序只为复现，不表示语义更准确。
- 历史片段独立提供文本证据，唯一H1标题仅用于带标记的展示，不回填原始元数据、不拼接正文。
- 健康主题纳入内容盘点；推断结果不得当作已确认的健康来源栏目用于知识库准入/删除。

## 完整调用链

既有公开采集缓存/历史片段/标题目录
→ 既有 `unify_news_storage.py` / `FrozenNewsStore` 的统一冻结包及带标签导出
→ 本次 `classify.py:load_selected` 校验原包文件SHA、选定ID/版本和实际文本字段
→ `source_units` 获取标题和独立片段证据
→ `classify` 按 `CATEGORIES` / `GEOGRAPHY_ONLY` 的版本化规则打分，保留跨类及输入不足标记
→ Python `Counter` 按唯一主类统计
→ `classification.jsonl`（全部逐篇明细）、`summary.json`（统计和验证）、
`policy.json`（完整规则快照）、`report.html`（中文报告及分层样本）、`manifest.json`（文件SHA）。

外部依赖仅Python 3.8+标准库；新闻不传往外部模型或在线分类接口。
数据库盘点采用 `psql` 的 `BEGIN READ ONLY`，ID+版本摘要与本地结果交叉核对，
证据在 `database-inventory.json`；该MD5仅作跨实现身份对照，完整源文件使用SHA-256。
配置项为代码中的 `VERSION`、`CATEGORIES`、评分阈值、地名降权，以及 `--input` / `--output`。
CLI不读取 `.env`，输出目录已存在时拒绝覆盖。

## 复现和测试

在WSL仓库根运行，重跑时选择新的输出目录：

```bash
python3 output/news-topic-classification-20261025/classify.py \
  --output output/news-topic-classification-20261025/reproduction

cd output/news-topic-classification-20261025
python3 -m unittest -v test_classify
```

10项回归测试通过，涵盖明显分类、地名干扰、片段重复/标题冲突、输入不变和复核标记。
它们验证脚本边界，不测真实分类准确率。最终数据完整性复核见 `verification.json`。

## 未完成TODO

本批4,714篇原不确定新闻的自动主类标注已完成，不再安排用户人工补标。
独立分类准确率评测、原始正式栏目补齐及未来新增新闻的持续分类仍未完成。
自动标注已有独立版本表和SQL视图，但未接入在线Agent或自动删除/准入策略，
未覆盖既有12主题标签体系；后续业务接入仍需明确栏目合同。
