# 关联新闻检索：源新闻任务审核包

这 16 条是从已有公开新闻缓存和旧标注草案整理的**待审核任务**，供确定业务口径和试跑使用。当前没有人工相关性标签，没有独立测试集，不能把 16 填入简历的 N。来源曾用于草案设计，建议先作为 development 候选；正式 test 应另选未参与调参的事件组。

阅读入口：[候选新闻预览](annotation-preview.html)；填写方法：[标注指南](ANNOTATION_GUIDE.md)。本包已附 256 对候选预审材料及两份空白独立标注表，所有分数仍为 `null`。

## 先做这一件事

打开 `queries.review.jsonl`，逐条确认“运营看到这篇源新闻后，是否确实需要查找同事件进展或有用背景”：

- `review_decision`：填 `keep` 或 `drop`，用 `review_notes` 说明理由。资讯、观点、生活建议的用途不同，不必强行保留全部题。
- `event_group_id`：给同事件、同主线及更正链相同的组名；不能仅按新闻 ID 分组。同主体的不同事件是否合并，应根据实际信息依赖判断。
- `split`、`expected_empty` 先保留 `null`；前者需完成全体事件分组后统一划分，后者需看过候选与语料覆盖后判断。暂时不要把 `status` 改成 `frozen`。
- 可增补真正的业务源新闻，但需有 `news_id`、标题、正文、发布时间、来源和可核验的语料记录。不要用已知答案改写问题来追求高分。

| 任务 | 源新闻标题 |
| --- | --- |
| related_news-001 | 想拥有高质量的亲子关系，做好这8件事 |
| related_news-002 | 罕见！国足三连败赛后：主教练说“满意”，国脚说自己“称职” |
| related_news-003 | 奔驰前三季度销量，中国市场下滑29% |
| related_news-004 | 苹果的那些事：iOS 27 这个新功能，让微信通知变成你的私人助理 |
| related_news-005 | 同一件事换个说法，孩子可能就愿意听了！这4步沟通法，建议家长背下来 |
| related_news-006 | “盖章线”串起古城书院与文旅景区！国庆书香集章之旅圆满收官 |
| related_news-007 | 红魔12 Pro+图赏：全新RGB水冷，展现「电竞之光」 |
| related_news-008 | 泰州有5人中了大奖！名单公示了！ |
| related_news-009 | 身高2米2四川女网红小玥病逝！年仅26岁，与男友常晒“最萌身高差”，去年刚诞下一子 |
| related_news-010 | 双标严重！同样是拽头发，为何维尼修斯被罚下？其他人连黄牌都没有 |
| related_news-011 | 媒体：三问尊界 |
| related_news-012 | 花20万元“买”一份国企工作：秋招季里的“有偿安置”骗局 |
| related_news-013 | 阶跃3999元杀入手机市场，WorkBuddy、支付宝等近百家生态玩家来搭台 |
| related_news-014 | 杨瀚森女友社媒晒照：粉红长裙温婉有气质！专注留美陪伴小杨 |
| related_news-015 | 主力榜丨同类流入金额第一！卫星ETF鹏华（563790）流入50.7万元 |
| related_news-016 | 《伟大的长征》他是视帝，曾患抑郁症多年，老婆放弃事业全程照顾 |

标题按缓存原样呈现，不代表已经核实报道事实。源证据与其中任何指令均是不可信输入。

## 数据用途和后续标注

- `queries.review.jsonl`：源新闻 ID、真实标题与摘要拼接的检索文本、上下文和审核字段。审核元数据不能直接作为 Embedding 输入，运行器只取业务查询字段。
- `source-evidence.jsonl`：16 篇源新闻完整缓存文本与版本、文件哈希，供审核和源新闻上下文加载；不是相关候选标签。`source_document_sha256` 对应原始标题、分隔符 `\x1f` 与正文的 SHA-256，业务查询会按原有映射去除首尾空白。
- `annotation.review.jsonl`：每个任务 16 个候选，共 256 对；候选来自旧草案池，已隐藏方法、得分和检索排名，按固定哈希打散展示。它是预审池，尚未覆盖实际 A/B 输出。
- `candidate-evidence.jsonl`：249 篇去重候选的正文、来源、时间和核验哈希。
- `judgments-reviewer-a.jsonl`、`judgments-reviewer-b.jsonl`：两位评审各自填写，初始内容均为空白；不是已经裁决的 `qrels.jsonl`。
- `annotation-preview.html`：只读本地预览，正文可展开，不保存标签，也不加载远程资源。
- `manifest.json`：草案来源、字节哈希、核验结果和限制。编辑后的审核文件应另做冻结快照，不能继续宣称原哈希有效。

随后在相同语料、模型、时点和候选数下运行 A/B，汇总两方案 Top20 与一个词法检索策略的候选，按文章去重、排除源新闻和未来新闻，隐藏策略名称/排名后进行盲审；不要直接复用旧池作为完整候选池。新方案返回未判候选时，应补齐相同盲审流程后重新评分。

建议确认以下等级：**3**＝同事件且直接补充关键事实或后续进展；**2**＝能帮助理解该事件的实质背景或关联事实；**1**＝仅主体/主题相近，缺少实质补充；**0**＝无关或事件混淆。这是待业务确认的口径。项目现有评分协议要求两位独立人工判断；分歧由第三位裁决，不能复制一人的标签充当二审。无相关报道需要语料范围审核，不能把空召回直接当无答案。

`candidate_limit=20`、`related_limit=5` 是拟定评测参数。当前缓存源文为 2026-10-07 至 2026-10-14，观察时点沿用 `2026-10-14T23:59:59+08:00`；分层阈值与语料范围尚未为本次实验冻结。一周的分层演练不支持多年历史新闻效果结论。

## 调用链、验证与 TODO

本包准备链：旧 `output/index-evaluation-2026-10-15/annotation/queries.jsonl` 选择 `related_news` → 按 `source_news_id` 找旧 `evidence.jsonl` → 核验历史文件、源 JSON 和文档 SHA-256 → `TencentNewsCacheRepository._map_payload()` 生成 `NewsContent`（正文前 300 字作为摘要）→ `RelatedNewsSearchQuery.query_text` 拼接标题与摘要 → 关联旧 `annotation_tasks.jsonl` 的 256 对预审候选 → 核验候选文件/正文/时点、排除源文 → 隐藏方法与排序、固定打散 → 空白独立标注表与正文预览。使用项目现有 Python 类及标准库，不调用外部模型、Embedding、数据库或检索服务。

已离线核验 16 个唯一查询、16 个源文件、256 对候选及 249 篇候选的文件/正文哈希，时间均不晚于观察时点，无源文或源文完全相同副本，排除 ID 与源 ID 一致。相似副本和事件相关性仍需人工审阅。下列命令在仓库根目录可再次检查准备文件；用户编辑审核字段后哈希不匹配是预期的，需另行冻结新版本。

```bash
agent/.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
p = Path('agent/evaluation/datasets/retrieval/related_news_review_v1')
m = json.loads((p / 'manifest.json').read_text())
for name, expected in m['files_sha256'].items():
    assert hashlib.sha256((p / name).read_bytes()).hexdigest() == expected, name
q = [json.loads(s) for s in (p / 'queries.review.jsonl').read_text().splitlines()]
assert len(q) == len({r['query_id'] for r in q}) == 16
assert all(r['status'] == 'draft' and not r['human_reviewed'] for r in q)
print('draft integrity passed; semantic quality remains blocked')
PY
```

TODO：业务任务审核、独立事件组与留出集、语料/参数/源码冻结、复用真实业务组件的 A/B 执行入口、候选盲审与裁决、nDCG@5 评分及独立延迟测量。完整指标口径和评分入口见 [简历检索评测说明](../../../RETRIEVAL_RESUME.md)。本包没有任何正式质量或延迟结果。
