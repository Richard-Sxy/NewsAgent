# NewsAgent 三模块演示文稿

> 面向现场演示的讲稿 + 操作手册，覆盖 **热点新闻 / 写作建议 / Data Loop** 三个模块。
> 所有命令均为只读或一次性本地容器，不触碰生产库。

---

## 开场：把系统想成一间"新闻作战室"（约 40 秒）

先不给架构图，先给一个画面：NewsAgent 是一间 7×24 小时运转的 **新闻作战室**。

- 墙上挂着一块 **雷达预警屏**：不是人盯出来的，而是企业行为数据按 `news_id + 时间窗口`
  聚合出的热度榜，谁在升温一眼可见；
- 桌上放着分析 Agent 递上来的 **情报简报**：趋势、关注原因、证据、运营建议，而且每条都
  标清楚——哪些是算出来的数字、哪些是有来源的事实、哪些只是待验证的推测；
- 值班编辑看着简报拍板：**采用、暂缓、还是拒绝**；
- 决定要写成稿，就走 **写作流水线**，在几个关键关卡停下来等人签字；
- 一天结束后，所有"判断错了、漏了、被拒绝了"的 case 进入 **Data Loop 复盘室**，
  变成第二天系统更聪明一点的燃料。

所以今天三块，对应的就是这条运营动线：
**雷达（热点新闻）→ 流水线（写作建议）→ 复盘室（Data Loop，也就是进化能力）**。

三个底层规矩先立住：

1. 统一关联键是 `news_id`，把行为数据、新闻内容、知识索引、分析结果串起来。
2. 指标、基线、热度分由 **Python/SQL 确定性计算**，大模型只负责解释与表达。
3. 高风险动作（Prompt、热度规则、候选配置、生产发布）只能生成候选，**必须人工审批**。

**一句免责声明（讲清楚边界）：** 仓库当前强的是链路和工程约束，不是生产上线。热点离线
演示里的 "模型报告" 默认是确定性占位 Runner，真实大模型需要配置 FastGPT App。

### 通用准备

```bash
cd /home/shi/project/NewsAgent/writing-agent-service
python3 -c "import fastapi, pydantic, httpx, dotenv; print('deps ok')"
```

---

## 模块一：热点新闻（作战室雷达）

### 一句话定位

企业行为数据 → 按 `news_id + 窗口` 聚合指标 → 历史基线 → 热度分 → 排行 →
精确读正文 + 知识库召回关联报道 → 构造可信输入 → 大模型结构化报告 → 业务校验。

### 运营视角（先看画面，再看命令）

这就是作战室最上面那块 **雷达屏**。演示时把屏幕上的每一项翻译成运营能听懂的话：

| 屏幕上看到 | 实际上是什么 | 为什么会亮 |
| --- | --- | --- |
| 热点榜 | 确定性热度分排行 | Python 算出的点击/消费/互动/增长分量加权，不是模型拍的 |
| 关注原因 | 情报简报的三色标注 | 蓝=指标、绿=有来源事实、紫=待验证假设 |
| 运营建议 | 带优先级的动作项 | high/medium/low + 给出理由 |
| 局限 | 这份情报的"不确定性说明" | 无证据、单窗口、未做因果验证，必须讲明白 |

**雷达的价值：** 它从不替你下结论，而是把"哪条新闻在升温、为什么、有几成把握"摆在
同一屏上，让人 30 秒内决定这条要不要跟进。

### 操作步骤

**1. 先看热点榜（纯确定性计算，秒级）**

```bash
python3 -m examples.hot_news_demo
```

预期输出：3 条新闻，热点分 `0.5852 / 0.4380 / 0.2750`，每条带点击/消费/互动/增长
四个分量。

**2. 跑全链路（推荐主演示）**

```bash
python3 -m examples.hot_news_fullchain_demo
```

预期输出：

- 窗口 `2026-09-03T10:00:00+08:00 → 11:00`，抓取 650 条行为记录；
- 每条新闻输出结构化报告：趋势判断、主导驱动、关注原因、运营建议、局限；
- 关注原因按 `metric / evidence / hypothesis` 分类，带置信度和 `observed/inferred`；
- 结尾打印 `request_id`、`usage`、校验通过时间。

**3. 打开 "有证据" 分支（对比演示）**

```bash
python3 -m examples.hot_news_fullchain_demo --min-overlap 1
```

预期：多出 `evidence` 类型关注原因和 "关联背景"，证据 `news_id` 落在输入白名单内。
对照默认（无证据）时，报告会显式输出 `limitations: 本次没有可用的关联报道证据…`。

**4. 展示契约（原始 JSON）**

```bash
python3 -m examples.hot_news_fullchain_demo --raw
```

**5. 切换到真实大模型**

配置 `FASTGPT_BASE_URL`、`FASTGPT_API_KEY`、`FASTGPT_HOT_NEWS_APP_ID` 后：

```bash
python3 -m examples.hot_news_fullchain_demo --real
python3 -m examples.hot_news_llm_smoke
```

### 讲稿

> "把这块屏当成值班编辑的雷达：它扫得比人快，但从不越权替你下结论。它给出的每条消息
> 都分三色——**蓝的是算出来的数字，绿的是有来源的事实，紫的是还没证实的推测**。关注
> 原因里每一条数字都能反查指标快照、每一条事实都绑定 `news_id`、每一条假设都显式标成
> inferred。新闻正文和检索结果都当不可信输入，模型输出里的指令不能改变系统行为。
> 离线 Runner 只用来验证链路和校验器，真实建议要走 `--real`。"

---

## 模块二：写作建议（质检流水线 · 写作任务工作台）

### 一句话定位

围绕一个选题，用 Temporal 编排：Research → 人工确认 → Outline → 人工确认 →
分章节写作 → Reviewer → 定点返工（≤3 轮）→ 人工确认终稿 → 发布。
其中 Research 产出的 **资料包就是 "写作建议"**：关键事实、时间线、证据冲突、
证据缺口、候选角度。

### 运营视角（把写作想成一条有质检关卡的流水线）

流水线上有四个工位、三道人工关卡，缺料和带病件绝不会流到下一站：

```text
[Research 工位] ──┐
  出资料包/写作建议  │
                    ├─ 人工关卡①：这份资料靠不靠谱？  approve / revise
[Outline 工位] ────┘
  出提纲            └─ 人工关卡②：结构对不对？
[章节写作工位] ────────→ [Reviewer 质检工位]
                          ├─ 通过 → 组装
                          └─ 不合格 → 只退回指定章节返工（≤3 轮，超限转人工）
[终稿] ───────────────── 人工关卡③：签发
```

**运营在这里的角色不是"写"，而是"验收和拍板"**：

| 人工关卡 | 运营看的重点 | 不放行会怎样 |
| --- | --- | --- |
| 资料包 | 事实有没有来源、冲突有没有标注、证据缺口清不清楚 | 退回补充检索，重跑 Research |
| 提纲 | 结构、角度、章节顺序 | 退回重排提纲 |
| 终稿 | 引用是否覆盖、是否可直接发布 | 退回到指定章节定点返工 |

### 前置

完整栈需要 Postgres + Temporal + Redis + MinIO + FastAPI + Worker，以及 Research /
Writer / Reviewer 三个 FastGPT App。没有真实 FastGPT 时，用仓库自带的 Mock 生成
"能过 Pydantic 校验的假内容"，只验证本项目自身链路。

```bash
cd /home/shi/project/NewsAgent/writing-agent-service
# 没有真实 FastGPT 时，另开一个终端：
python3 tools/mock_fastgpt.py --port 3000
# 主栈（把 FASTGPT_BASE_URL 指向 Mock，App ID 随便填）
docker compose --env-file .env -f deploy/docker-compose.yml up -d
curl --fail http://localhost:8000/ready
```

### 操作步骤（UI 演示路径）

1. 浏览器打开 `http://localhost:8000/console`（同源单文件控制台）；
   或 Vue 前端 `cd frontend && npm run dev` 后打开 `http://127.0.0.1:5173/jobs`。
2. 切到 **"写作任务工作台"**，填写：
   - 选题：`请基于知识库资料撰写一篇面向科技从业者的新闻综述，主题为人工智能大模型推动新闻行业智能化转型。`
   - 场景：`research_package`（只看研究建议）或 `assisted_writing`
   - 幂等键：留空自动生成，重复提交不会创建第二条任务
3. 点 "创建任务"。等价的命令行入口：

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs \
  -H "X-Tenant-ID: 11111111-1111-4111-8111-111111111111" \
  -H "X-User-ID: 22222222-2222-4222-8222-222222222222" \
  -H "Content-Type: application/json" \
  -d '{"topic":"AI大模型推动新闻行业智能化转型","scenario":"research_package","requirements":{},"idempotency_key":"demo-1"}'
```

4. 任务列表出现 `waiting_human`（research gate），打开详情：
   - **查看资料包** → `GET /api/v1/jobs/{id}/research-package`
     （展示 facts / timeline / conflicts / evidence_gaps / suggested_angles）
   - **查看 RAG 指标** → `GET /api/v1/jobs/{id}/research-metrics`
   - **导出 Markdown** → `GET /api/v1/jobs/{id}/research-package/export?format=markdown`
5. 人工门禁：gate=`research`、action=`approve`，提交 →
   `POST /api/v1/jobs/{id}/decisions`。之后是 outline gate。
6. 审批后自动进入章节写作 → Reviewer；Reviewer 可指定章节返工，最多 3 轮，超限转人工。
7. final gate approve 后可导出终稿：`GET /api/v1/jobs/{id}/export`；发布走
   `POST /api/v1/jobs/{id}/publish`（本地 mock CMS 在 `:18080`）。

### 讲稿

> "写作建议不是让模型直接写稿，而是给编辑配了一条带质检的流水线。Research 工位先把
> **可追溯事实、时间线、冲突、证据缺口** 摆出来，再给候选角度——这就是写作建议。每个
> fact 带 `source_url` 和置信度，引用覆盖率会被业务校验。人只在三道高价值关卡上签字，
> 不写稿也不盯流程；机器保证带病件不流到下一站。"

---

## 模块三：Data Loop（进化能力）

### 一句话定位

把校验失败、低置信、运营拒绝、人工纠错统一转成 **Feedback Case**；人工修正后按
cutoff 冻结成 **不可变评测集**；在 golden / fresh bad case / high-risk 三层上离线回放
候选与线上基线；用 **确定性 Gate** 判定；人工审批后激活新 Bundle，或显式回滚。

### 先讲清楚：Data Loop 到底在"进化"什么

很多人一听"自进化"，以为是模型半夜自己训练自己。**这里不是，也不允许。** NewsAgent
的进化对象是四类 **可控资产**：

1. 分析 Prompt；
2. 热度权重和阈值配置；
3. 关联新闻重排参数；
4. 输出 Schema 和校验规则。

模型本身不自动训练，当前也没有企业训练平台。系统真正做的是：
**把运营今天用真金白银换来的判断，沉淀成明天线上运行的新版本配置。**
人从"执行者"变成"审核者"，系统每转一圈就长一点。

### 一个比喻：Data Loop 就是系统的"错题本 → 补考 → 升级"

| 阶段 | 现实里的动作 | Data Loop 里对应什么 |
| --- | --- | --- |
| 记错题 | 这次哪条判断错了/漏了 | 校验失败、低置信、运营拒绝 → `Feedback Case` |
| 老师订正 | 人工给出正确答案 | 标注员修正 + 独立二审批准 |
| 出考卷 | 把错题编进卷子 | 按 cutoff 冻结成不可变评测集（三层） |
| 模拟考 | 新解法先做一遍卷子 | 候选配置在 golden/fresh/high-risk 上回放 |
| 划及格线 | 达到标准才算过 | 确定性 Gate 判定 |
| 老师签字 | 同意后才改教学方案 | 人工 APPROVE / REJECT，超时默认不晋升 |
| 升级 | 新方案正式启用 | 候选成为 Production Bundle，下次线上运行读取 |
| 后悔药 | 发现不行就退回 | 显式回滚到上一稳定版本 |

**一句话：它让"犯错"不再是纯损耗，而是变成资产。** 同样的坑，系统只让人踩一次。

### 没有 Data Loop vs 有 Data Loop

| | 没有 Data Loop | 有 Data Loop |
| --- | --- | --- |
| 犯错后 | 人工改一次，下个窗口可能还犯 | 错误沉淀成 Feedback Case，进入评测集 |
| 调 Prompt / 阈值 | 拍脑袋改，没有回归证据 | 候选在固定评测集回放，出了证据再上线 |
| 上线方式 | 直接改线上配置，出事先救火 | 审批 + 账本 + 幂等键，随时可回滚 |
| 系统状态 | 静态，靠人反复打补丁 | 每轮循环长一点，且版本全程可追溯 |

### 三个安全阀（讲给担心"失控"的人）

1. **三层评测**：golden 保基本盘、fresh bad case 追新错误、high-risk 防老问题复发。
2. **双基线对比**：候选同时对比线上基线和上一实验基线，防止局部优化造成全局退化。
3. **人工闸门 + 超时默认不晋升 + 显式回滚**：高风险动作永远不会自动发布。

> 讲到这里给听众一个结论：**Data Loop 不是要一个会自己改代码的"大 Agent"，
> 而是给系统装了一条有护栏、可审计、能回退的进化回路。**

### 前置：独立 Compose（自带确定性 FastGPT 替身）

```bash
cd /home/shi/project/NewsAgent/writing-agent-service
docker compose -f deploy/docker-compose.data-loop-e2e.yml up -d --build
docker compose -f deploy/docker-compose.data-loop-e2e.yml ps
curl --fail http://localhost:18000/ready          # API
curl --fail http://localhost:13000/health         # FastGPT Stub
docker compose -f deploy/docker-compose.data-loop-e2e.yml exec -T api alembic current
# 预期 head = 20260910_0013
```

Temporal UI：`http://localhost:18233`；MinIO Console：`http://localhost:19001`。

### 步骤 A：准备到人工闸门

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-approve.json
```

自动完成：Base Bundle 初始化 → 一次在线热点运行 → 运营拒绝 → Feedback Case →
两个独立合成身份完成标签提交/二审 → 两层基准集冻结 → Candidate 创建 → Fresh 集自动
冻结 → 三层回放。命令只在以下三条同时成立时成功退出：

```text
phase == waiting_approval
gate_passed == true
waiting_for_approval == true
```

此时 Active Bundle 仍是 Base，不能提前切换。把输出的 `tenant_id`、`workflow_id`、
`analysis_run_id`、`candidate_id`、三个 `dataset_id` 记下来。

### 步骤 B：审核人做真实决定（关键动作）

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner decide --state /state/manual-approve.json \
  --reason "人工审核已核对三层数据集、评估报告和版本差异"
```

命令不会默认批准，审核人必须亲自输入字面量：

```text
APPROVE
```

批准后驱动会验证：Candidate `activated` → Active Bundle 切到 Candidate → 再启动一次
真实 HotNews Workflow → 新 `analysis_runs` 使用 Candidate Bundle 版本 → FastGPT Stub
的新调用全部使用 Candidate App ID。

### 步骤 C：再核验并回滚

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify  --state /state/manual-approve.json
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner rollback --state /state/manual-approve.json
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify  --state /state/manual-approve.json
```

预期：`rollback_ledger_created=true`、`rollback_restored_base=true`，三个 Dataset ID
互不相同。

### 步骤 D：人工拒绝路径（换一份状态文件）

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-reject.json
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner decide  --state /state/manual-reject.json --reason "人工审核拒绝该候选版本"
# 输入 REJECT
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify  --state /state/manual-reject.json
```

预期：Candidate 为 `rejected`，Active Bundle 仍是 Base，不产生激活。

### 步骤 E：真人标签分工（可选，突出职责分离）

```bash
e2e-runner prepare-feedback --state /state/manual-label.json   # 跑到 Feedback Case
e2e-runner label-submit      --state /state/manual-label.json   # 输入 SUBMIT
e2e-runner label-review      --state /state/manual-label.json --reason "独立二审已核对…"
                                                                # 输入 APPROVE
e2e-runner prepare           --state /state/manual-label.json   # 继续到发布闸门
```

系统会拒绝自审：同一人不能既提交标签又批准标签，也不能批准自己提交的 Candidate。

### 步骤 F：账本与 Artifact 复核（讲审计）

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml exec -T postgres \
  psql -U newsagent_e2e -d newsagent_e2e
```

在 psql 中核对 `analysis_runs` / `feedback_labels` / `evaluation_datasets` /
`configuration_candidates` / `candidate_evaluation_runs` / `promotion_decisions` /
`production_bundles`。预期：

- 批准链有且仅有一条 `approve` 和一条 `activate`，回滚后多一条 `rollback`；
- `approve.actor_id != candidate.proposed_by`；
- 任意时刻每个租户只有一个 `active` Bundle。

MinIO 侧用 `mc stat` / `mc cat | sha256sum` 核对对象 Metadata 与数据库
`content_sha256` / `artifact_sha256` 一致。

### 步骤 G：48 小时无人审批（超时默认不发布）

```bash
RUN_TEMPORAL_TIME_SKIPPING=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest tests/test_data_loop_time_skipping.py -q
```

预期：逻辑时间推进 ≥48 小时，只记 `system/reject`，`activate_bundle` 不执行。

### 讲稿

> "注意看这条流水：**运营拒绝 → 变成 Feedback Case → 人工订正 → 冻结成评测集 →
> 生成候选配置 → 三层回放 → 人工批准 → 激活新 Bundle → 下一次线上运行读到新版本。**
> 这就是 Data Loop 的全部意义：它不是自动训练模型，而是把人的一次判断，变成系统永久
> 长出来的一小块能力。而且每一步都有幂等键、账本、审计和回滚，超时默认不晋升。
> FastGPT 替身只是让演示可重复，真实发布前还要用真实 FastGPT App 和企业
> Gateway/IdP 再跑一遍。"

---

## 收尾总结（约 30 秒）

回到开场的作战室画面，三个模块正好串成一条**运营动线**，也是三个闭环：

| 模块 | 作战室里的位置 | 闭环 | 给运营的价值 |
| --- | --- | --- | --- |
| 热点新闻 | 墙上的雷达屏 | Online Insight Loop | 30 秒看清"什么在升温、为什么、几成把握" |
| 写作建议 | 带质检的流水线 | 研究/写作编排 | 人只签字不盯流程，带病件不流到下一站 |
| Data Loop | 收盘后的复盘室 | 反馈 → 评测 → 晋升 | 每次犯错都变成系统永久的能力增量 |

三条收口纪律：

1. **数字和事实由确定性代码算，大模型只做解释与候选**，不碰权威数字；
2. **高风险动作永不自动发布**，只能生成候选，必须人工审批；
3. **进化的对象是 Prompt、热度配置、重排参数、校验规则**，且全程版本化、可审计、可回滚。

最后一句：**这套系统真正稀缺的不是模型能力，而是把运营判断变成可复现资产的那条回路。**
