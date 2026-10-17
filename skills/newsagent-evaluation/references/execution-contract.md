# 执行与判定合同

这是证据协议，不修改应用 Schema、数据库和生产 Policy。

## A. 目标与可证明范围

| 目标 | 必须证据 | 允许结论 | 不能外推 |
| --- | --- | --- | --- |
| 契约/确定性回归 | 当前源码、固定输入、真实断言、命令/退出码/计数 | 此版本合同/计算满足断言 | 语言质量、企业 RPC、生产效果 |
| 离线模型质量 | 审核冻结样本、真实模型身份、独立 oracle、逐例预测和固定分母 | 指定数据/模型的指标和对照 | 分布代表性、未执行的上游、真实收益 |
| 隔离集成 | 隔离栈、实际入口/身份、业务引用和持久终态 | 该栈组件正确衔接 | 企业接入、长稳、正式人工审批 |
| 企业接口合同 | 获准测试端点、真实 SDK/HTTP、鉴权/枚举/错误/维度 | 指定依赖合同兼容 | 语义质量、生产容量 |
| 负载/恢复 | 硬件/镜像/并发/时长、端到端计时、资源/错误/恢复 | 已测范围内容量和恢复 | 超出观测范围的 SLO |
| 真实运营效果 | 授权试点、匿名聚合、运营审核、对照/归因 | 耗时、修改成本、事实质量、采纳/跟进价值 | 无试验支持的 CTR/收益因果提升 |

分别声明 inference、Embedding、仓库、内容、索引、Workflow、标注员的真实/合成/替身状态。真实 BGE 不使 local inference 成为真实模型，真实 PostgreSQL 不使合成仓库成为企业数据。

## B. 运行规格与冻结

新建 `output/evaluation/<日期>-<独立run-id>/`，禁止覆盖旧实验。仅盘点可缩减为报告，不必创建不执行实验的全套产物。

正式评分前定稿 `run-spec.json`：

1. 问题、目标、起止点、包含/排除、权限依据。
2. 来源/授权/人工审核，case/query/news/event_group/split 引用、数量/分母。Golden/Fresh/High-risk 是风险层，dev/validation/test 是隔离层，不能互代。
3. 观察时点和窗口 `[start,end)`、时区/偏移、水位、同小时基线、可见新闻版本；不以执行当天替换 as_of。
4. HEAD、相关 tracked diff 的 SHA-256、相关未跟踪代码逐文件 SHA-256；不能只记路径。排除 `.env`/凭证文件，不输出 diff 内容。记录源码与镜像分别对应哪个版本。
5. byte SHA-256 与应用 canonical SHA-256 分字段保存。HotNews `content_sha256` 是 Pydantic 序列化哈希；检索 CLI 绑定文件字节摘要，不能互换。
6. 当前/候选/上一实验 Bundle、Prompt/route/model、Schema/Validator、热度/基线/重排、切片/Embedding/索引、SQL契约/编译器、配置身份；不适用项说明原因。
7. oracle、单位/精度/容差、成功/失败规则、分组、公式、绝对阈值/非劣界/批准者。批准者未知不冒填。
8. 命令、解释器/依赖、硬件/线程/并发、seed/warmup/重复次数、预算/停止条件。

不直接保存配置或模型原文。公开安全 YAML 可记录摘要；疑有内联凭证的配置不读，使用已有安全配置入口。清单仅含必要聚合、审计与证据引用。

## C. 对照与评分

- 当前/候选采用同 case/oracle/仓库/范围/as_of、相同失败预算与有效参数。上一实验来自真实可追溯资产；缺失时仅遵照已批准 Policy 豁免，不能临时关闭要求。
- 检索实验需从同查询重新编码→检索→过滤→重排→评分，不能只替换分析 Prompt。版本只有名称无可重放资产时不具备该项评测条件。
- 开发材料/test 分离，转载/更正/阶段关系按事件组拆分。合成包可证明边界覆盖，不自动代表真实新闻分布。
- 模型不看标签、不生成自己的 gold。明确选用的 LLM judge 只作辅助，记录独立模型/rubric并经人工校准，不覆盖确定性 oracle、业务 Validator 或人工审核。
- 所有预注册输入保留在分母。超时/空结果/失败不丢弃；意外异常整体中止时报告完成/失败/未执行数和已知下界，不能以成功子集宣布整体通过。
- 未审核标签/未判 TopK/缺预测/身份错配阻断对应质量评分，不能“排除后通过”。区分执行失败的空输出与缺文件/评测污染。
- 事先约定重复次数并保留全部结果，不挑最好一次。报总体、关键分组、样本与独立事件组数、适用分母。scorer 没实现的 paired bootstrap/分组区间列缺口，不假称已完成。

## D. 现有评分器的语义

1. `app/evaluation/hot_news.py::HotNewsEvaluationService` 检查合同、主驱动、必须/禁止证据、必须指标、限制说明。空 required 集合 coverage=1，须同时报有该标签的案例数；非空 limitation 不证明解释充分或建议可用。单次离线调用与生产分析的反馈纠错重试不同。
2. `services/data_loop/offline_replay.py::_to_metric_set` 的 `evidence_precision` 实际为“forbidden_evidence_count==0 的案例比例”，不是逐引用语义 precision；合同失败的 forbidden 也可能为0，靠独立 critical_failures 拦截。metric_coverage 是 required_metric_keys 覆盖，不核验正文全部数字。
3. `EvaluationGate` 对三风险层逐一检查最低样本数、通过率、上述 evidence_precision、metric_coverage、critical_failures，及当前/上一实验退化。`CohortGateThreshold` 默认 min_case_count=1、三项 min_rate=0、max_critical_failures=0 只是默认值。需读实际版本化 Policy，不能据默认值判业务合格。
4. 检索 scorer 对 grade>=2 计二元相关，nDCG gain=`2^grade-1`；Recall/nDCG 是池口径。它检查标签/包身份，不验证全部语料成员/正文摘要/as_of/池完备性，后者需冻结审计。保留 `acceptance_status=not_approved`。
5. `operations_evolution_eval.py` 合成主线评分独立于正式 `EvaluationGate`；默认 GatePolicy/seeds 不是已批准生产 Policy。改稿字段匹配不证明改稿语义正确。
6. Reviewer 与生成器同链路互评仅支持返工控制，不是独立事实验收。正文逐数字/引用核验、编辑耗时/使用价值没有专用 runner 时列缺口。

## E. 判定与停止

- `passed`：完整样本、适用 oracle、冻结标准均已完成且满足，注明环境和结论范围。
- `failed`：身份/标签合法，实际结果违反适用标准，列案例与硬失败。
- `blocked`：缺标签、版本/数据漂移、标准未批准、预算/依赖/权限不足，无法验收；可留探索指标。
- `not_run`：未选择/未授权/不适用/计划未执行，注明原因和影响。

硬违规不能被均分抵消。准备故障阻断相关评分；归因文案/PDF故障不改变已算分数，但须报告交付缺口。

所有重试纳入原预算。提交不确定先查状态，仅可证明幂等时最多手工再提交一次，之后诊断。保留各 Workflow 原有有界重试策略，不统一修改。不要搬用 TAOTIAN 的自动训练、Reward 自动生效、轮询间隔或20小时发布超时。

## F. 报告要求

报告至少含：

1. 问题/目标/链路/状态、环境组成、结论范围、标准批准状态。
2. 完整入口→转换→函数/类→外部 Port→持久化/Artifact/API/SSE，配置、测试和 TODO。
3. 运行规格/哈希/源码/镜像/provider/三基线/窗口/split/as_of/标签审核身份。
4. 准确命令、起止时间/退出码、case/request/query/run/workflow/evaluation ID、业务终态、尝试和预算、脱敏日志/产物摘要。
5. 分母：计划/完成/失败/blocked/not_run/skip/xfail/deselected 数量和原因；跳过不计通过。
6. 绝对指标/基线差值/关键分组/硬违规/bad case/已实现区间或重复与限制；无标签项只报覆盖不造分。
7. 实际 policy_version/hash、失败原因、Artifact/批准引用；停止在人工评审，列人工标签、企业合同、负载、正文事实/数字、真实价值 TODO。

推荐产物：`run-spec.json`、`commands.json`、`results.json`、原生 runner 输出、`report.md` 和单页中文 PDF。原生输出不足时在旁路摘要补解释，不修改原 Gate，不把摘要声称为应用原生能力。
