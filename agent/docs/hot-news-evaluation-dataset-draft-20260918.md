# 热点分析评测集候选草稿（待人工审核）

> 状态：draft，不是已冻结的 Evaluation Dataset。
>
> 采集说明：候选标题和热度来自腾讯新闻热榜聚合页面，页面显示“更新于约 6 小时前”；
> 文章正文优先使用腾讯新闻 `view.inews.qq.com` 页面核验。热榜热度只能作为候选选择信号，
> 不能直接作为分析结论或权威指标。正式入集前必须补齐项目自己的 `analysis_input_snapshot`、
> 运营标签、独立审核和证据引用。

## 建议的首批结构

本批先构建 12 条候选：

- Golden：6 条，覆盖政策、科技、民生、社会事件； 
- High-risk：6 条，优先覆盖标题容易误导、事实与推断容易混淆、需要谨慎引用的案例；
- Fresh bad case：不在本文件手工指定，等 Data Loop 运行时从最近窗口自动冻结。

正式冻结前需要把同一条候选关联到系统中的 `feedback_case_id`。目前的文章 ID 只是外部来源
标识，不等于 NewsAgent 内部 `news_id`。

## 运营策略规则



## A. Golden 候选

| draft_id | source_article_id | 标题 | 热榜热度 | 建议覆盖 | 正文核验 |

| G-001 | 20260819V0347I00 | 直击朱雀三号回收降落瞬间：火箭缓缓落下稳稳落地，现场掌声雷动 | 253万 | 科技突破、事件热度、事实证据 | [腾讯新闻](https://news.qq.com/rain/a/20260819V0347I00?id=20260819V0347I00&path=a&app=news&redirect_pc=1) |
| G-002 | 20260818A0C0HV00 | 北京楼市新政后首周：二手住宅网签量增长10% | 232.3万 | 政策影响、指标引用、限制条件 | [腾讯新闻](https://news.qq.com/rain/a/20260818A0C0HV00?id=20260818A0C0HV00&path=a&app=news&redirect_pc=1) |
| G-003 | 20260818A0BTZI00 | 激发下沉市场活力 释放县域消费潜能——多部门详解活跃县域消费18条举措 | 221.6万 | 政策解读、宏观指标、证据链 | [腾讯新闻](https://news.qq.com/rain/a/20260818A0BTZI00?id=20260818A0BTZI00&path=a&app=news&redirect_pc=1) |
| G-004 | 20260819A063T900 | 未来5年，优化医保便民这么干 | 208.9万 | 公共服务政策、事实与规划区分 | [腾讯新闻](https://news.qq.com/rain/a/20260819A063T900?id=20260819A063T900&path=a&app=news&redirect_pc=1) |
| G-005 | 20260819A06DDJ00 | 公安部门悬赏100万元寻找被盗石狮子 | 216.2万 | 文物保护、社会事件、证据引用 | [腾讯新闻](https://news.qq.com/rain/a/20260819A06DDJ00?id=20260819A06DDJ00&path=a&app=news&redirect_pc=1) |
| G-006 | 20260819A03TO700 | 贵州一对夫妻均不愿直接抚养子女，法院判决不准离婚 | 189.4万 | 法律事实、未成年人保护、避免过度推断 | [腾讯新闻](https://news.qq.com/rain/a/20260819A03TO700?id=20260819A03TO700&path=a&app=news&redirect_pc=1) |

这边我做一个简单的总结：科技突破，事实证据，政策影响，社会事件，法律事实，这类新闻有强事实证据，推送性明显

## B. High-risk 回归候选

这些样本不是因为“热度高”就自动进入高风险集，而是因为标题和正文之间存在较高的
误读、过度推断或事实核验风险。审核时应重点确认 `required_evidence_news_ids`、
`forbidden_evidence_news_ids` 和 `must_state_limitation`。

| draft_id | source_article_id | 标题 | 热榜热度 | 风险点 | 正文核验 |

| R-001 | 20260819V04ZL100 | 河南周口一河道大量鱼类聚集，有市民徒手一小时抓几百斤，渔政制止 | 205.4万 | 标题数字、现场原因、不可把个案写成普遍现象 | [腾讯新闻](https://news.qq.com/rain/a/20260819V04ZL100?id=20260819V04ZL100&path=a&app=news&redirect_pc=1) |
| R-002 | 20260819A05U5U00 | 视频丨51个赛项提前看！世界人形机器人运动会到底比什么？ | 210.5万 | 赛事信息和科技能力边界，避免把宣传语当作性能结论 | [腾讯新闻](https://news.qq.com/rain/a/20260819A05U5U00?id=20260819A05U5U00&path=a&app=news&redirect_pc=1) |
| R-003 | 20260819A05HI900 | 独臂少年刘宸瑞，考入清华！ | 208.1万 | 个体励志故事，禁止扩大为因果或群体结论 | [腾讯新闻](https://news.qq.com/rain/a/20260819A05HI900?id=20260819A05HI900&path=a&app=news&redirect_pc=1) |
| R-004 | 20260819A042DN00 | 吸烟能放松、解压？答案是 | 206.3万 | 健康结论、因果表述、必须保留限制条件 | [腾讯新闻](https://news.qq.com/rain/a/20260819A042DN00?id=20260819A042DN00&path=a&app=news&redirect_pc=1) |
| R-005 | 20260819A05ACD00 | “我爸公司负债48亿”“父债子还”，97年上市公司创二代带货，引发热议 | 184.2万 | 标题叙事与公告事实区分、关联交易数字核验 | [腾讯新闻](https://news.qq.com/rain/a/20260819A05ACD00?id=20260819A05ACD00&path=a&app=news&redirect_pc=1) |
| R-006 | 20260819A0413D00 | DeepSeek，为何要招土木人才？ | 174.6万 | 从招聘信息推断企业战略时避免过度因果化 | [腾讯新闻](https://news.qq.com/rain/a/20260819A0413D00?id=20260819A0413D00&path=a&app=news&redirect_pc=1) |

这边我做一个简单的总结：个体案例避免推广到范例，点的原因避免推广到面

> 本批草稿已避免 G/R 两个 Cohort 直接复用同一篇文章；正式冻结前仍需按内部
> `news_id`、事件簇和内容哈希做一次最终去重。

## C. 每条样本需要你审核的字段

审核人不要只确认“标题是否热门”，还要填写以下内容：

```yaml
internal_news_id: "待从 NewsAgent 内容库补齐"
feedback_case_id: "待生成或关联"
layer: golden | high_risk_regression
verdict: correct | incorrect | partially_correct
allowed_dominant_drivers: []
required_evidence_news_ids: []
forbidden_evidence_news_ids: []
required_metric_keys: []
must_state_limitation: true | false
severity: low | medium | high | critical
operator_comment: "人工说明"
review_decision: approve | request_changes
review_reason: "独立复核意见"
```

## D. 审核规则

1. 热榜热度只用于选择候选，不直接写入 `expected` 标签。
2. 文章正文、检索结果和模型输出都视为不可信输入，必须由人工确认事实与证据。
3. 每条 High-risk Case 必须有独立审核人；严重案例建议双人复核后再冻结。
4. 同一事件不能因为标题不同而重复进入多个 Cohort，除非审核记录明确说明用途不同。
5. 没有完整 `analysis_input_snapshot` 的候选不能进入正式数据集。
6. 正式冻结后创建 `v2`，不修改 `v1`。

## E. 冻结前验收标准

```text
Golden：至少 10 条，覆盖至少 4 类问题/场景
High-risk：至少 10 条，critical 案例覆盖率 100%
标签独立复核率：100%
缺少必需证据：0
重复 feedback_case_id：0
重复事件跨 Cohort：人工确认
所有案例有 lineage、label_version、approved_by、approved_at
```

## 来源

- 腾讯新闻热榜候选列表（第三方页面展示的腾讯新闻榜单）：
  https://www.46.la/tool/tencent-hot-rank-tool
