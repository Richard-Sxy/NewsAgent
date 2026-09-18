# 新闻标题 NLP 导入设计

> 状态更新：2026-09-18。LTP 标题分词、词性、实体抽取、SQLite 保存和 FastGPT metadata
> 写入已在仓库实现；当前属于本地内容特征，不是新闻事实或企业生产标签。真实标题抽样、
> 实体精确率/召回率、别名词典治理和模型版本升级回放仍需人工评测。

## 定位

标题实体是检索过滤、事件合并、人物/企业热点榜和运营订阅的基础特征，不作为新闻事实或
权威指标。第一阶段采用本地 LTP 推理，避免逐条调用大模型产生的费用、延迟、不稳定输出与
Prompt Injection 风险。

## 在线调用链

```text
TencentNewsCrawler / ArticleCache
→ NewsArticle.title
→ LTPTitleAnalyzer（cws + pos + ner）
→ 词典别名归一化 + 机构后缀修正 + 去重
→ TitleAnalysis
├─ IngestRepository.save_title_analysis
│  ├─ news_title_analyses（词元、模型版本、状态）
│  └─ news_title_entities（逐实体可查询记录）
└─ TitleAnalysis.to_fastgpt_metadata
   → FastGPTClient.create_news_collection
```

`news_ingest_records` 仍只负责抓取和知识库导入状态，标题 NLP 使用独立表，避免改变原有
去重主表的职责。分析记录保留 `extractor` 和 `model_version`，模型或词典升级后可以进行
版本对比和离线重跑。

## 失败策略

- 模型在启动后的第一次调用时懒加载，后续由同一服务实例复用。
- 多线程导入共享模型时串行执行模型推理，避免模型对象的线程安全风险；网络抓取和其他
  工作仍可并发。
- `TITLE_NLP_REQUIRED=false` 时，失败记录到 `news_title_analyses.status=failed`，正文继续
  导入；设为 `true` 时，实体抽取失败会使本篇新闻导入失败并进入原有重试链路。
- 生产环境只从本地缓存加载模型。下载由显式脚本完成，不隐藏在每日导入任务中。

## 业务上线建议

1. 从科技、财经、体育等主要频道各抽取至少 200 条真实标题，人工标注三类实体。
2. 按实体类型分别统计严格匹配 Precision、Recall、F1，不能只看总准确率。
3. 优先修复高频企业简称、同名人物和产品名边界；词典变更需要版本化。
4. 将实体用于召回、聚类和运营筛选时保留原文位置与来源，不直接据此生成事实断言。
5. 首轮稳定后再增加历史数据回填脚本、实体别名管理接口和监控面板。

## API 降级判断

当前本机已验证 `LTP/tiny` 可用，因此在线导入不需要外部 API。只有在目标部署环境无法
安装 PyTorch、模型质量经标注集验证不达标，或需要开放类型实体时，才建议增加结构化 API
Provider；API 输出仍必须经过 Schema、原文 span 和类型白名单校验后才能落库。
