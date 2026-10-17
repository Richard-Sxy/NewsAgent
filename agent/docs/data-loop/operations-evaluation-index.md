# 运营建议评测集位置与本次扩充

研究与核对日期：2026-10-14。新版将公开资料转成可复核的运营判断情景；公开资料是真实来源，新闻事件、运营身份、指标和参考答案均为合成，待人工校准，未执行推送。12板块是本项目的评测分类，不声称是腾讯新闻当前客户端的完整频道清单。

## 先看哪里

本次扩充包在 [`agent/evaluation/datasets/operations_research_v1/`](../../evaluation/datasets/operations_research_v1/README.md)。

| 用途 | 文件 |
|---|---|
| 总入口与12板块导航 | [README.md](../../evaluation/datasets/operations_research_v1/README.md) |
| 人工阅读：情景、候选、参考决定、设计来源、改稿检查要点 | [operations_casebook.md](../../evaluation/datasets/operations_research_v1/operations_casebook.md) |
| 模型输入：144条无答案候选 | [operations_inputs.json](../../evaluation/datasets/operations_research_v1/operations_inputs.json) |
| 独立测试输入：48条判断 | [operations_inputs_test.json](../../evaluation/datasets/operations_research_v1/operations_inputs_test.json) |
| 程序答案与完整账本 | [operations_cases.json](../../evaluation/datasets/operations_research_v1/operations_cases.json) |
| 13条来源、发布日期、浏览日期与证据边界 | [source_registry.json](../../evaluation/datasets/operations_research_v1/source_registry.json) |
| 数量、类别、理由码与分板块统计 | [summary.json](../../evaluation/datasets/operations_research_v1/summary.json) |
| 所有产物的SHA-256和种子内容哈希 | [manifest.json](../../evaluation/datasets/operations_research_v1/manifest.json) |
| 供既有热点分析评测器读取的独立投影 | [hot_news_analysis_test.json](../../evaluation/datasets/operations_research_v1/hot_news_analysis_test.json) |
| 可复现的版本化素材目录 | [operations_research_v1.json](../../evaluation/seeds/operations_research_v1.json) |

每板块另有 `operations_casebook_<board_id>.md` 和 `operations_inputs_board_<board_id>.json`，在包内README可直接点击。全部主线的两个观察时点保持同一split，development/validation/test各48条运营判断；分析投影去重后分别24个新闻快照。

## 已有包与新版关系

| 素材包 | 位置 | 内容与用途 |
|---|---|---|
| 合成运营v1 | [operations_synthetic_v1](../../evaluation/datasets/operations_synthetic_v1/operations_casebook.md) | 48例、24对照事件，运营四种决定及反馈分类基础覆盖 |
| 主线演变v2 | [operations_evolution_v2](../../evaluation/datasets/operations_evolution_v2/README.md) | 432判断、216快照、36主线、12板块，六阶段历史演变与个人作用域 |
| 本次公开研究扩充v1 | [operations_research_v1](../../evaluation/datasets/operations_research_v1/README.md) | 新增144判断、72快照、36主线、13来源；两时点与板块特定反例，人工语义检查要点 |
| 热点分析基础种子 | [hot_news_eval_seed_v1.json](../../evaluation/datasets/hot_news_eval_seed_v1.json) | 既有热点分析Schema与指标/证据契约评测，区别于运营建议评分 |

前三个独立运营包合计624条候选判断。旧包保留，未混合重划split，不能把624当成同一个经过校准的正式模型测试集。新版使用自己的 `run_operations_research` 入口；旧v2命令不能直接读取新版账本。

这些文件是离线素材，不是数据库中已二审的正式Golden/Fresh/High-risk数据集。正式DataLoop仍须由可关联热点运行产生反馈、标注、独立二审和冻结；新增素材没有虚构run_id、审批人或投递记录。

## 分板块运营优化与评测覆盖

下表为根据公开资料推导的项目建议，不是腾讯内部运营制度。统一原则是先保证事实与服务价值，再选择内容形式、受众和时间；指标不足时提出验证方案，不编造收益。

| 板块／ID | 可操作的优化建议 | 新增主线与重点反例 | 主要依据 |
|---|---|---|---|
| 国内 `domestic` | 将政策整理为适用范围、生效时间、材料与办事问答；汇总同一政策，突出新的服务细则 | 征求意见→地方正式试点；低点击服务问答；部分恢复→透明更正。不能写成全国生效或全量恢复 | 腾讯产品、CAC真实性、应急信息解读 |
| 国际 `international` | 建事件时间线，保留具名信源、时间与时区；将当日进展、背景画面和观点分开 | 单方声称→货运试行；冲突统计→共同口径；去年演习→当日会谈。不得平均伤亡数字或把资料画面当新事态 | 腾讯产品、2026来源标注通报 |
| 财经 `finance` | 原始公告链接与期间、单位、同比/环比等口径一起呈现；用图解解释，不承诺交易收益 | 毛利率百分比/百分点；并购传闻→接洽；图解点击变化→因果限制 | 财经治理原文、腾讯精品内容演讲、项目计算约束 |
| 科技 `technology` | 解释用途、研究/产品阶段、比较条件与限制；提供知识专题和核验问题清单 | 厂商演示→测试限制；AI演绎夹外部指令；名单测试→两地试点。不能跳到量产、全量上线或性能排名 | 科学编辑指南、CAC真实性、腾讯产品 |
| 社会 `society` | 按已确认、待核实、澄清与公共服务维护进展；互动围绕真实信息缺口 | 调查→部分核查；匿名帮助→结案；旧事故画面→当前施工服务。不能预设责任或用个人隐私拉互动 | 应急信息解读、CAC真实性 |
| 地方 `local` | 精确按地区和资格服务；恢复信息及时替换旧指引；非紧急专题优先站内阅读 | 公交绕行→恢复；预约静默→撤销通知；城市漫步→额度不足。不能把站内可读等同于主动通知授权 | 腾讯产品、应急信息解读、项目授权约束 |
| 体育 `sports` | 区分赛前、进行中、终场与赛后解释；核对时区、赛会状态和主题订阅 | 改期→延期；暂态比分→官方赛果；战术解读→订阅撤销。允许有知识价值的赛后专题，不只奖励即时热度 | 腾讯事件/精品内容方向、CAC真实性 |
| 娱乐 `entertainment` | 内容围绕作品、创作过程与文化话题；公告聚合去重，商业活动显著标识 | 同一公告首屏多稿→制作访谈；匿名家庭传闻→公开回应；品牌合作→新物料。规则不能扩大为全站永远仅一篇 | 娱乐明星信息规范原文 |
| 文化 `culture` | 发展中小馆、知识关联和长内容；摘要/目录提高查找效率，参观服务另核馆方公告 | AI复原→局部考古支持；低点击长访谈→摘要；数字导览→线下展期结束 | 腾讯SSV数字文化、精品内容演讲、CAC真实性 |
| 教育 `education` | 用年、省、批次、资格、时间和官方入口组织服务卡；提供政策解读与官方咨询 | 补报名→截止；往年参考→本年条件；匿名成果→课程资源。拒绝保录取、内部名额和学生隐私营销 | 教育部2025服务部署、项目隐私边界 |
| 健康 `health` | 保留研究层级、样本、适用人群和修订；科普专业资质与疗效证据分别检查 | 观察研究→混杂限制；未核资质带货→资质核验；健康咨询→新增时段。认证不证明产品疗效，咨询不是诊疗 | 腾讯医典、医疗科普规范、科学编辑指南 |
| 天气 `weather` | 显示官方台站、地区、级别、时间和状态；解除与更新及时同步，交通/停课引用各自部门 | 沿海预警→分县解除/延长；降雨预警→限定航线停运；旧预警解除→新预警。不得由模型改级别或编停课命令 | 气象预警传播办法、应急信息解读 |

腾讯公开介绍支持事件驱动、互动与可信内容定位；其历史演讲支持长期信息价值、专业深度与多角度延展。这些方向用于本项目的内容设计，未复制企业用户画像、算法实现、频控数值或流量阈值。[腾讯产品页](https://www.tencent.com/zh-cn/products/tencent-news/)、[腾讯新闻2022年精品内容演讲](https://cloud.tencent.com/developer/news/972576)。

研究记录包含历史资料及无发布日期的产品页，登记了明确的适用边界。例如2025教育部署不能提供2026各省报名日期，2021专项财经治理不表示该次专项行动仍在进行，腾讯医典产品质控不能推广为所有新闻板块的制度。全部来源详见来源登记，未把打开失败的搜索摘要列为已浏览证据。

## 评测完善建议

本次已增加：设计来源与虚构事实分开、板块专属场景、两个观察时点累计事实、同样指标的两候选对照、候选顺序无答案暗示、独立主线split、每例人工改稿要点、分板块/人员/时点/主线统计。高热度不能覆盖过期、失实或撤销授权；低点击也不能直接否定办事解释和文化知识价值。

自动评分仍是结构化参考一致性：决定、反馈类别、受控理由码、原候选阻断、证据引用、修改字段。缺答按所选全部案例的固定分母失败；未知/未来证据、身份错配、重复case_id拒绝。它不会因为出现“修订copy”字段就证明文案修好了。

人工校准建议对事实准确、板块价值、动作可执行、受众/时间/渠道、修订语义五维各记0/1/2分，并保留评审说明。事实捏造、未经同意的主动通知或绕过审批应单独记录关键失败。合理替代动作和理由码要经过运营复核另建版本，不能把种子唯一参考误认为唯一业务真理。此人工评分方案是建议，尚未接入自动评分器。

目前两时点共享少量合成事实，模型仍可利用模板特点，不能据此证明跨真实新闻泛化。跨信源检索、长时间复杂演变、同板块多运营权限、负反馈/退订聚合质量、真实流量分布与投递收益需要后续独立数据。

## 完整调用链

```text
搜索并实际浏览公开来源（只存元数据、短述与适用边界）
→ operations_research_seed.build_catalog()：36条虚构主线和候选参考
→ seeds/operations_research_v1.json：版本化素材目录
→ run_operations_research build
→ OperationsResearchCatalog / ResearchStory / ResearchObservation 严格校验
→ build_dataset()：只累计观察时点已知事实，固定主线split与个人作用域
→ metric_snapshot / metrics_view / HotScoreCalculator：聚合计数、CTR、分量与热度
→ ResearchCase / OperationsResearchDataset / validate_dataset：复算与隔离校验
→ write_artifacts()：答案账本、中文案例册、盲输入、来源登记、分析投影、哈希清单
→ 离线调用模型仅提供 operations_inputs*.json，自行保存 EvolutionPrediction 格式预测
→ run_operations_research score → score_predictions：固定分母和分组评分
→ 运营人员按 manual_rubric 校准改稿与参考标签
→ 后续另行接正式反馈/标签二审/冻结账本，当前没有导入或发布
```

核心模块为 [`operations_research.py`](../../evaluation/operations_research.py)、素材作者入口 [`operations_research_seed.py`](../../evaluation/operations_research_seed.py) 和命令入口 [`run_operations_research.py`](../../evaluation/run_operations_research.py)。复用既有 `EvolutionCandidate`、`ExpectedJudgment`、`SyntheticScope`、`EvolutionPrediction` 与 `operations_evolution_eval.score_predictions()`，保留四种决定和反馈Schema；没有改v2或生产运行代码。分析投影可由既有 `load_evaluation_dataset()` / `run_hot_news_eval` 读取，分析契约与运营审核分开计分。

依赖已有Python 3.11、Pydantic和项目指标/Schema模块；构建、验证与评分不连接数据库、Temporal、网络或模型，不读取`.env`。新的CLI没有run-model命令；预测可由批准的外部离线模型流程产生，不能读参考答案后声称模型通过率。`accepted`表示参考候选可进入人工审核，不表示已批准或允许发送。

## 配置与测试入口

在仓库 `agent/` 目录，用装有项目依赖的Python环境执行。本次使用系统 `python`；当前 `agent/.venv` 尚无Pydantic/pytest。

```bash
python -m evaluation.operations_research_seed
python -m evaluation.run_operations_research build
python -m evaluation.run_operations_research validate --verify-artifacts
python -m evaluation.run_hot_news_eval --dataset evaluation/datasets/operations_research_v1/hot_news_analysis_test.json
python -m evaluation.run_operations_research score --split test --predictions /absolute/path/predictions.json --output /absolute/path/new-score.json
```

`run_hot_news_eval` 默认只验证数据集，不带 `--run-model` 不调用模型。新版配置项为 `--catalog`、`--dataset`、`--output`、`--verify-artifacts`、`--predictions`、`--split`、`--board-id`、`--owner-user-id`；没有新增环境变量、生产Prompt或Bundle版本。已有不同内容的素材与结果路径会拒绝覆盖，应另建版本或实验目录。

预测格式沿用 `EvolutionPrediction`；`case_id`和`scope_sha256`取自盲输入，证据ID只取本例 `analysis_input.related_news`。

```json
{
  "case_id": "替换为本例输入的case_id",
  "scope_sha256": "替换为本例输入的64位hash",
  "decision_type": "deferred",
  "operator_feedback": {
    "category": "strategy_judgment",
    "reason_codes": ["insufficient_evidence"]
  },
  "blocked": true,
  "evidence_news_ids": ["替换为本例提供的证据news_id"],
  "revision_fields": [],
  "revision": null
}
```

预测文件使用上述对象数组，或包含`predictions`数组的运行结果。仅供格式说明，不是任一具体案例的答案。模型批量读取测试输入时不应同时访问案例册、完整账本、种子或人工rubric。

测试入口为 [`tests/test_operations_research.py`](../../tests/test_operations_research.py)。与旧两版一同回归时显式禁用Settings的dotenv读取：

```bash
python -c 'from app.config import Settings; Settings.model_config["env_file"] = None; import pytest; raise SystemExit(pytest.main(["tests/test_operations_research.py", "tests/test_operations_evolution.py", "tests/test_operations_synthetic.py", "-o", "addopts=", "-q"]))'
```

本次验证结果：三个相关模块共117项通过；新版38个文件在临时目录重建，与交付目录逐字节一致；哈希、投影一致性和索引本地链接通过。既有热点分析loader验证test投影24个快照成功。这些结果验证素材与离线契约，未调用真实模型，也不是运营效果或生产质量验收。

TODO：真实运营人员校准与独立二审；将改稿语义评分接入有版本的评审流程；增加真实匿名聚合/非模板主线与信源冲突检索；接入正式run/feedback/冻结账本；在批准模型上做盲测；经批准后单独测量真实投递效果。本包不修改长期项目背景，不授予自动训练、策略上线或新闻投递权限。
