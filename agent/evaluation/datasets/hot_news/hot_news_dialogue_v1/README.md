# 热点 Agent 对话评测 v1

`cases.json` 固定 30 个场景、33 轮对话，冻结 `timeline-v4` 合成数据覆盖：北京时间
2026-09-10 00:00 至 2026-10-10 00:00（左闭右开，样本截至 2026-10-09）。
`manifest.json` 保存原始 JSON 的 SHA-256；修改场景后必须显式发布新版本和新哈希。

12 轮预期完成取数；其余验证澄清、拒绝或已有运行读取。日期只有天而无小时，应澄清，
不能取默认样本冒充全天。跨小时 UV、企业原榜、发布时间筛选、未知栏目、手写 SQL
和越权指令都不应创建 SQL/Workflow。中文数量、五种指标、四个栏目、两种内容类型、
首日/中段/末日小时、同一运行追问均在集合内。

`date_followup_pending` 明确记录跨轮补全日期及小时尚未实现：首轮应澄清，第二轮
仅给日期和小时应保留待确认。它通过边界检查后记为 `expected_pending`，不计通过，
不计成功取数。用户应在补充时重复完整查询，例如
“查询2026-10-09 19:00至20:00点击率最高的前5条视频新闻”。

## 执行

从仓库 `agent/` 目录使用已安装依赖的 Python：

```sh
python -m evaluation.run_hot_news_dialogue
python -m evaluation.run_hot_news_dialogue --run --output evaluation/results/hot-news-dialogue-local.json
python -m evaluation.run_hot_news_dialogue --run --case hour_last_day --case date_needs_hour
```

默认只校验集合与哈希，不发送 HTTP。实际交互默认经已授权本地前端代理
`http://127.0.0.1:5175/api/v1/conversations`，身份由代理注入。直连隔离 API 时：

```sh
python -m evaluation.run_hot_news_dialogue --run --base-url http://127.0.0.1:8000 --direct-api-identity
```

当前 `news-agent-timeline-demo` 本地容器已含运行器；宿主 Python 未安装依赖时可运行：

```sh
docker exec news-agent-timeline-demo-api-1 python -m evaluation.run_hot_news_dialogue --run --base-url http://127.0.0.1:8000 --direct-api-identity --output /tmp/hot-news-dialogue-report.json
docker cp news-agent-timeline-demo-api-1:/tmp/hot-news-dialogue-report.json ./hot-news-dialogue-report.json
```

宿主直接调用时 API 为 `http://127.0.0.1:28050`；5175 为前端代理。

直连选项只使用仓库公开的本地演示 Token、合成租户、演示用户和 admin 权限；不读 `.env`。
用户身份与 5175 代理一致，便于打开这些评测会话；每批次 evaluation UUID、每个会话
conversation UUID 和每条消息 request UUID 仍然独立。
地址仅接受带端口的 HTTP loopback origin，不跟随重定向，不使用环境代理。
运行前读取 `/runtime`；必须明确为 `model_provider=local` 且 `query_enabled=true`，
否则尚未创建会话即停止。每个 case 创建独立的服务器 UUID 会话，每条消息使用新
`request_id` UUID；请求顺序执行，每轮只提交一次。默认请求超时 180 秒、上限 240 秒；
最大集合 40 场景/60 轮。超时或 HTTP 错误记录为 `blocked`，不重试提交；保存已提交
消息、request UUID、预期、耗时和安全错误类型/HTTP状态码，响应记录为 null，不保存
原始响应错误正文、异常细节或身份头。失败轮次之后的依赖追问不会继续提交。
运行器保留会话和热点运行，便于在热点 Agent 页面继续查看，不删除历史。

## 调用链与判分

CLI → `load_dataset()` 校验结构、30 天冻结范围和 manifest 哈希 →
`run_evaluation()` 预检本地运行时、创建会话并逐轮调用 `POST /messages` →
现有 `ConversationAgentService` / `query_hot_news` / SQL Guard / Temporal 热点链 →
查询已保存的 `/api/v1/hot-news/runs/{run_id}` → `report_items()` 对齐只读报告投影 →
`evaluate_turn()` 确定性核对工具次数、状态、意图、SQL参数、窗口、运行绑定和榜单快照 →
`summarize()` → 逐轮问答、工具轨迹、判分失败项及统计 JSON。

外部依赖只有本地 HTTP API 和 Python `httpx`、`pydantic`；数仓、Temporal、模型与
PostgreSQL 仍由现有服务链管理。配置项为 `--dataset`、`--run`、`--base-url`、
`--direct-api-identity`、`--case`、`--timeout` 和 `--output`。
报告统计分别保留 `passed`、`failed`、`blocked`、`expected_pending` 与
`completed_query_count`；`turn_count` 与 `attempted_turn_count` 都表示消息提交尝试，
`completed_response_count` 表示已返回的结构化轮次，区分已提交但没有答复的请求。
正确拒绝算合同通过，但不算成功取数。已有运行读取需与真实
运行详情一致，省略追问还需指向上一轮同一 run。成功取数必须与保存的 SQL/参数/窗口、
榜单投影一致，并在回答中显示实际 run/query 引用；礼貌或流畅文本本身不能证明成功。

测试入口：

```sh
python -m pytest tests/test_hot_news_dialogue_eval.py -q
```

测试覆盖集合冻结与数量、非本地地址拒绝、先检查运行时、无提交重试、运行引用、SQL
及窗口漂移、工具超额、真实报告证据缺失和 pending 的独立统计。

TODO：跨轮日期/指标条件补全、真实跨小时去重 UV，以及企业推理/数仓合同和语言质量
验收。本集合只验证本地规则 Port 与集成，不代表企业模型质量；未来更换模型后需要
另建开放式语言理解及真实企业数据的脱敏评测。
