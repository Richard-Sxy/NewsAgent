# 年度新闻召回与人工标注工作台

本地入口：`http://127.0.0.1:8877/`。这是本次年度真实新闻的隔离实验工作台；原有主服务和生产配置没有切换。

## 使用方式

1. 从左侧选择查询。已准备科技、AI、财经、股票、汽车、数码、国际、军事、体育、游戏各 3 个查询，共 30 个，每个取前 10 篇，共 300 个“查询—新闻”判定任务。它们是发现线索产生的查询草稿，可添加自己的问题，不代表真实流量分布。
2. 阅读问题和新闻正文，选择相关、部分相关、不相关、无法判断。标注人可填自己的名称，备注可选。
3. 点击“保存并下一篇”。保存成功后写入本地 SQLite；刷新、重开浏览器或重启服务可恢复。支持改标，历史修订保留；另一窗口已改过同一项时拒绝覆盖并提示重新加载。
4. 展开“查看召回链路与人工评测指标”，查看各层预算、实际候选、去重、最终分布和耗时。模型分数默认折叠。
5. 完成后点右上角“导出标注”；也可分批保存，不必一次完成全部任务。无需另行查找新闻。

## 实际冻结结果（2026-10-27）

- 来源 559 条，全部写入统一内容合同的隔离新闻库；完全相同正文归并 14 个冗余来源后，检索新闻 545 篇。原来源及证据仍保留。
- 语料版本 `annual-1f977788a4e26f1bb710fee5`；内容发布版本 `news-b08d194a6744415f3a0143d7`。
- 本地真实 `bge-large-zh-v1.5:d9eb05180107021f8e72`，1024 维、CLS 池化、L2 归一化；没有使用哈希替身生成本轮向量。
- 2577 个分片，实际最大 503 tokens，截断数 0；向量文件 SHA256 为 `7c29525164700a14ed5a58f1fd661756b88049493b7e326c82f26fda34438154`。
- 固定日期 2026-10-27，按发布日年龄分配：0–7 天热层 5 篇，8–90 天温层 115 篇，更早冷层 425 篇。此规则不包含真实用户热度，不是生产生命周期迁移。
- 构建 82 条带双端原文与日期的报道先后候选；同日只标同日候选，不强加先后。专题合集不构造独立事件链。这些候选不证明语义同事实、回应或因果，不影响当前召回排序。
- 30 个实际查询均已执行并冻结。正式人工库初始 0 个标注；浏览器测试的假标签只存在独立验证副本。

## 数据与完整调用链

上游是 `var/news-events-annual-20261027/corpus-v1` 及三个事件／专题发现 JSON。

`prepare_annual_retrieval_lab.py` 调用 `verify_records` 校验 raw/parsed/body 哈希，再逐来源核验截止日期；`prepare_records` 按完全相同正文哈希确定代表，保留其他来源，保留原日期精度。它生成新闻、关系候选和均衡板块查询草稿；`build_package → save_package → FrozenNewsStore.import_release` 保存不可变内容发布及隔离 SQLite 新闻库。日级日期没有被补成午夜，缺时区的时间没有变成精确时间戳。

`serve_retrieval_lab.py → RetrievalLabEngine` 强制核验新闻文件哈希、行数和模型文件身份。`chunk_news` 用正文 480 字、重叠 60 字、标题前 24 字切片；`OfflineBgeProcess → embedding_worker.py` 启动独立 Python 进程，用本机模型离线编码，禁用远程代码和模型下载。分片逐批检查 token 上限，向量检查维度、有限值和归一化，批输入哈希支持中断续建。下游保存 `vector-index/vectors.npy`、批缓存、索引清单及日志。

每个查询通过 `QueryRetrievalPolicy.plan` 给三层分配预算；实验引擎精确计算余弦相似度，在各层按新闻去重并补齐候选，再通过 `rank_candidates` 做全局排序。板块是软提示，不做硬过滤；所有层均可召回。结果带模型／语料／策略版本、匹配分片、分层计数和实际阶段耗时。该规模使用 NumPy 精确检索，不能作为 ANN 或企业向量库的性能验收。

`LabService.seed/search → RetrievalLabStore.put_run` 冻结完整查询、候选正文、证据和 trace。`create_app` 提供 HTTP API，静态页面通过 GET 读取任务，通过 PUT 发送人工选择。`save_judgment` 使用 SQLite 事务、预期修订号和审计表持久保存；`calculate_metrics` 确定性计算人工覆盖和符合条件的指标。每份实验数据库固定语料／模型／策略组合，版本漂移需要另建实验库。单项完整业务 trace 不是 OTel TraceID，本工作台没有伪造生产遥测。

## 存储与接口

实验根：`var/news-events-annual-20261027/retrieval-lab-v1/`。

- `content-release/` 与 `news.sqlite3`：559 条版本化正文和来源，使用既有统一新闻 Schema。
- `manifest.json`、`news.jsonl`、`queries.jsonl`：冻结实验清单、545 篇规范新闻和 30 个查询。
- `relations.jsonl`、`event-chains.jsonl`：有证据的报道时间线候选。
- `vector-index/`：持久化向量、模型哈希、缓存及构建证据。
- `judgments.sqlite3`：正式人工任务、当前判断、改标审计。保留该文件即可恢复进度。

HTTP：`GET /api/status`、`GET /api/queries`、`GET /api/runs/{id}`、`POST /api/search`、`PUT /api/runs/{id}/judgments/{news_id}`、`GET /api/export`。写请求使用 JSON、同源检查及 `X-Lab-Request: 1`，限制请求大小；服务仅监听本机回环地址。页面将新闻和来源文本安全转义。

`output/news-events-annual-20261027/lab-validation/` 是浏览器验收副本及截图，里面的 UI-SMOKE 标签不是人工金标，也不会合并到正式标注库。

## 指标含义

标签等级为相关 2、部分相关 1、不相关 0；无法判断没有等级。未判定和无法判断都不作负例。

- 人工判定覆盖率：已经明确判断的候选数／候选数；无法判断另外计数。
- Precision@10（仅相关）：前 10 篇均明确判定后，相关篇数／10。部分相关不计入严格相关数。
- 候选池 nDCG@10：整个冻结候选池明确判定后，使用等级增益 `2^grade - 1` 和该池的理想排序计算。当前默认池就是前 10 篇，因此只评价这批结果的排序，不能测量漏召回。
- 全库 Recall、笼统“准确率”保持空值；没有完整语料相关性金标时不能给出这类结论。当前为人工单审实验，不自动晋升原有 Data Loop 的双审金标或生产策略。

## 启动与配置

在 WSL 仓库根目录执行：

```bash
var/news-events-annual-20261027/runtime/app-venv/bin/python \
  agent/tools/serve_retrieval_lab.py --device cuda --port 8877
```

重启会校验并复用已有向量和查询，不重写已有人工判断。`--build-only` 只构建与准备查询；`--package-dir`、`--model-dir`、`--database`、`--embedding-python` 指定隔离路径，`--device cpu` 可使用 CPU。已运行时不要重复启动同端口。默认模型目录为 `var/knowledge-snapshots/rag-recall-demo/data/models/bge-large-zh-v1.5`。

运行环境：应用 Python 3.11.9，FastAPI、SQLAlchemy、NumPy 等精确依赖见 `deploy/requirements.retrieval-lab.lock.txt`；编码环境是工作区 Python 3.8 venv，复用本机 Torch 2.4.1+cu121 和 CUDA，安装 Transformers 4.44.2、Tokenizers 0.19.1。已在 RTX 4070 验证。两套环境位于本次 runtime 目录，没有改全局依赖，不读取 `.env`。

## 验证入口

```bash
cd agent
../var/news-events-annual-20261027/runtime/app-venv/bin/python -m unittest \
  tests.test_retrieval_lab_store tests.test_prepare_annual_retrieval_lab \
  tests.test_retrieval_lab_engine tests.test_retrieval_lab_api -q
node --test tests/test_retrieval_lab_frontend.cjs
```

71 项后端测试和 9 项前端测试通过。真实全量 BGE 构建、30 查询冻结和桌面侧 HTTP 检查通过；Chrome 无头浏览器在独立副本上验证了保存、刷新恢复、改标修订、切换新闻不预选、导出、真实自定义语义查询和移动端宽度。保存验收使用单独数据库，正式标注仍为空。内置浏览器自动化因宿主沙箱初始化失败，验收改用已安装的 Chrome 与 Playwright 完成。

浏览器脚本 `agent/tools/check_retrieval_lab_ui.cjs` 仅接受验证副本端口 8878，需 `NEWSAGENT_PLAYWRIGHT_PATH` 指向已安装 Playwright。不要将副本启动参数指向正式 `judgments.sqlite3`。

## 剩余边界

这一步已经能够进行相关性标注。仍需后续工作的是：根据你的判断评估与调整策略、补齐薄弱板块、语义同事实与回应／更正等关系的更深校验、基线策略对比与多策略候选池、完整 Recall 金标、生产生命周期入库与跨层迁移、企业 ANN 性能及日内历史回放。当前仅折叠完全相同正文，新的事实不会因共享主题被合并；当前关系数据是时间线候选，尚不能宣称完成因果逻辑链。
