# 2026 年八月腾讯新闻数据写作评测集 v1

版本：`tencent-news-data-writing-2026-08.v1`；状态：`draft_pending_human_review`。
这是一套供 Research、Writer、Reviewer 分阶段评测的离线草案，尚未运行模型。

## 内容与抽样边界

| 维度 | 当前内容 |
| --- | --- |
| 规模 | 27 个 case，9 个事件组，11 份新闻缓存 |
| 题材 | 金融 12、AI 12、科技机器人 3 |
| 任务 | Research、Write、Review 各 9 |
| 划分 | development、validation、test 各 9；按事件组、来源及相同正文哈希隔离 |
| Review | 3 个无错对照、6 个人工错误稿；development、validation、test 各含 1 个无错对照 |
| 证据不足 | 小米样机 Research 应返回 `request_evidence`，不能补造量产信息 |
| 数值标签 | 18 项由 `Decimal` 运算复核；不是模型答题成绩 |

来源是用户授权使用的仓库现有缓存，路径见每项 `cache_reference`。
报道元数据日期为 2026 年 8 月 12、19、20、22 日，缓存抓取于 10 月。
它们不是八月当日留存快照，也不代表腾讯官方热榜、八月全月热点排名或全月代表性样本。
`hotness_verified=false`；来源均标为 `cache_only`，尚未核对原始公告。
新闻事实必须表述为“缓存报道所述”，不能将缓存来源等同于独立核真。

`published_at` 缺少时区的原始字段按 `Asia/Shanghai` 归一；`as_of` 只限制引用资料的
发表元数据时间，不能证明正文在当时的版本或证明其之后未被更新。
种子保留来源、事实定位和正文 UTF-8 SHA-256；构建器不回读缓存正文、不联网核验来源。

本集没有企业用户行为明细，也没有合成企业流量指标。
Review 中的人工错误稿以及 1 段注入文本是专门构造的评测材料，均非报道原文；
对抗材料不得作为新增新闻证据或系统指令。

## 文件与模型输入隔离

- `seed.json`：编辑入口，包含来源、事实、case、难度和预期标签。
- `inputs.jsonl`：27 条推理输入记录，展开各 case 的来源；已剥离 `variant` 和 `expected`。
- `labels.jsonl`：仅供评分的预期动作、事实、禁用结论、限制说明、数值检查和人工量表。
- `manifest.json`：数量、划分、文件哈希、来源正文哈希与验证范围。

未来运行器按记录的 `task` 路由角色，模型请求只发送 `record.input`。
不得把整条记录、`seed.json`、`labels.jsonl`、`manifest.json` 或带答案的预览发送给模型。
预览和标签只能用于审核、评分；构建数据文件本身不会调用模型。
所有 Review 请求采用统一中性表述，不以“错误稿”或“无错”等措辞泄漏答案。

Research 输入目前是人工整理的事实卡片，不包含新闻全文；只评测给定事实的整理与一致性，
不评测原文抽取、检索或独立核真。事实卡片中的人工判题提示已移至私有 `labels.jsonl`，
输入来源仍保留报道归属，供模型正确表达“缓存报道所述”。

## 构建、校验与测试

从仓库根目录执行：

```sh
cd agent
python3 -m evaluation.data_writing_dataset \
  --seed evaluation/datasets/data_writing_august_2026_v1/seed.json \
  --output-dir evaluation/datasets/data_writing_august_2026_v1
```

只读校验现有产物是否与确定性重建逐字节一致：

```sh
python3 -m evaluation.data_writing_dataset \
  --seed evaluation/datasets/data_writing_august_2026_v1/seed.json \
  --output-dir evaluation/datasets/data_writing_august_2026_v1 \
  --check
```

标准库测试入口（同样在 `agent` 目录下）：

```sh
python3 -m unittest discover -s tests -p 'test_data_writing*.py'
```

两个测试文件覆盖结构、引用范围、数据划分泄漏、标签隔离、数值运算、确定性生成以及
实际八月样例中的中性审校指令、无错对照、缺证处理和答案提示检查。
测试与 `--check` 通过仅说明这些离线约束成立，不说明事实已核真或模型质量达标。

## 完整调用链

上游入口是上述 CLI 与 `seed.json`。核心实现位于
[`evaluation/data_writing_dataset.py`](../../data_writing_dataset.py)：

```text
CLI 参数 --seed / --output-dir / --check
  → build_dataset
  → load_seed：读取 JSON，拒绝重复键
  → render_dataset → validate_seed → _numeric_checks
      校验 Schema、来源与事实引用、发表时间、跨划分泄漏和 Decimal 标签
  → 按 case_id 排序，分离 inputs / labels，生成规范 JSON 字节和 manifest 哈希
  → 写入三个产物；--check 时只比对既有文件
```

运行依赖只有 Python 标准库，无运行环境变量、企业接口、数据库或模型服务依赖。
配置由 CLI 参数和种子中的版本、语料窗口、划分、来源、数值单位及标签容差提供。
下游产物用于后续 Port／Workflow 评测运行器和人工评分，当前尚未接通真实模型执行。

展示链路由 `evaluation/render_data_writing_preview.py` 生成预览，目标交付为仓库根目录的
`output/data-writing-evaluation-2026-08-v1/preview.html` 及配套 ZIP；
单页说明 PDF 为 `output/pdf/NewsAgent_八月热点数据写作评测集_2026-10-15.pdf`。
这些文件用于人工审阅，不属于模型输入。预览器还对照本地缓存重新检查新闻身份、标题、
发表和抓取时间、正文哈希以及事实定位边界；这不等于核验报道内容的真实性。
HTML 预览只用标准库生成；PDF 另需 ReportLab 和 macOS 的 Arial Unicode 中文字体。
预览器参数为 `--seed`、`--output-dir` 和 `--pdf`，仍不读取 `.env` 或调用企业服务。

## 待完成

- 接入 Research／Writer／Reviewer Port 或 Workflow 运行器，保存模型、Prompt 版本及实际响应。
- 逐句核对事实、来源归属、时态、权限和注入处理；人工检查全文数字覆盖，当前自动运算标签不能替代语义评分。
- 在标签和预览隔离的条件下开展盲测，报告分任务、分事件的结果及失败案例。
- 复核原始公告与新闻原页，补齐可审计的发表时点版本证据。
- 扩充完整八月的日期、题材与难例覆盖，另行定义热点与代表性抽样依据。
