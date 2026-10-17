# 简洁界面与对话测试

本轮只调整前端呈现。默认展示业务内容、当前中文状态与下一步；编号、技术版本、原始
JSON、旧快照及高级启动表单按需展开。确认卡仍展示主题、篇幅、实际配置差异、评测
来源与反馈驳回副作用。未知、失败、排队和人工待办不会被折叠成成功。

## 完整调用链

`AppShell → /chat → ChatView → conversationsApi.runtime/list/detail`：沿用同源网关身份，
按真实 `available_tools` 生成快捷示例。打开 /chat 时读取历史列表，但不自动选择旧会话；
带 conversation 参数的链接仍恢复同一会话。示例仅写入内存草稿，发送第一条消息后才
沿用既有 create/sendStream 创建会话和轮次，不另起写入端口。

`已保存 turn.tools → writingConfirmation / capabilityConfirmation / toolReceipts →
CapabilityReceiptsPanel → 各业务卡片`：原有严格解析、租户读取和链接校验保持。
`preferCapabilityCards` 只在整轮成功、每条能力结果均有完整有效业务卡片时，把重复原文
收进“完整答复”；原文不改写。候选、失败、混合工具、缺快照、未知和未落地结果仍显示
完整正文，避免丢掉解释。此判定不调用外部服务，也不从模型文本提取编号或状态。

卡片先展示当前状态；原始快照在“当时快照”，实体版本和时间在“记录信息”。写作与评测
沿用 `useWritingJobPolling/useEvaluationPolling → receipt GET → 既有 SQL/Temporal` 的
3秒/15秒只读刷新。评测旧排队记录和当前已启动状态分开，不把旧回执当当前结论。
确认仍走 `appendInput` 的原 token/request UUID；后端验证后薄适配原有业务 Service。

`卡片安全 deep_link → Jobs/DataLoop/HotNews 页面 → 原有 API/store → 当前实体与人工
操作`：高级参数、手动启动、SQL轨迹、编号和原始载荷改为默认关闭的详情，字段和操作
保留。批准后继续激活的现有副作用、修改说明必填、标签二审和确认边界仍显示。

外部依赖沿用 Vue、Vue Router、Pinia、Vite 与既有 API；后端仍依赖 PostgreSQL、Temporal、
模型 Port 和 S3。没有新增 Python/npm 包、环境变量、业务表或权限。

## 开始测试

打开 [5178 对话页](http://127.0.0.1:5178/chat)，选择示例后点击发送。建议依次测试：

| 输入 | 预期 |
| --- | --- |
| 查看当前样本的热点新闻 | 样本热点与新闻摘要，可以继续追问第一条新闻 |
| 记住：我更关注科技新闻 | 当前用户的短期记忆卡 |
| 写一篇人工智能发展趋势，800字，客观中立 | 先出现确认卡；确认后显示真实任务或排队状态 |
| 我有哪些写作任务 | 当前租户任务卡，点击进入同一任务 |
| 长期记住：我更关注科技新闻 | 长期候选，人工晋升前不生效 |

5178含候选/评测运行环境；5174是原native-e2e开发入口，实际可用能力以runtime为准。
当前演示租户已有待人工批准的评测任务，占用长任务名额；新增写作/评测排队属于真实
状态，不代表前端故障。可以先测试热点、记忆与只读进度；人工决定仍在对应工作台。
数据为本地样本，不能作为实时今日新闻或企业模型质量验收。

## 配置、验证与部署

沿用 `config.json` 的 appTitle/environment/apiBaseUrl 和同源网关身份；没有新增配置项。
AppShell把标准NewsAgent演示标题缩短，完整配置标题仍保留在title属性；导航和状态用中文。
宽屏保持历史侧栏，窄屏默认收起历史；空白欢迎区自动高度，输入框不再被说明和空白挤出。

测试入口：`cd agent/frontend && npm test`、`npm run lint`、`npm run build`（含typecheck）。
332项前端测试通过，lint和构建通过。新增presentation测试覆盖失败/混合/未知/缺快照
不能折叠正文；现有卡片测试补齐旧确认字段、待办fallback；会话测试验证权限示例只填
草稿、空白入口和显式会话恢复。既有live Vite测试需要环回地址临时监听，沙箱内EPERM
不视为产品失败，获准本地监听后全量通过。报告在 output/conversation-hooks/frontend-simplification-*。

浏览器实测：默认空白对话、权限快捷示例仅填草稿、输入框可直接使用；旧cohorts与窗口
字段显示为中文来源摘要；历史评测卡刷新为“评测完成·待人工审批·门禁通过”，同一
Workflow详情显示审批入口和批准后激活说明。新版页面已在5178部署，证据截图为
output/conversation-hooks/frontend-simplified-chat.jpg与frontend-simplified-evaluation.jpg。

部署沿用 `Dockerfile.conversation-hooks.cached` 与已核验的本地Nginx基础镜像，只传dist：

```sh
docker build --pull=false -f agent/frontend/Dockerfile.conversation-hooks.cached -t news-agent/frontend:conversation-hooks-demo agent/frontend/dist
docker compose --env-file /dev/null -p news-agent-bge-demo -f agent/deploy/docker-compose.native-e2e.yml -f agent/deploy/docker-compose.bge-snapshot.yml -f agent/deploy/docker-compose.bge-demo.yml -f agent/deploy/docker-compose.conversation-hooks-bge-demo.yml up -d --no-deps web
```

## 未完成 TODO

本轮界面精简无剩余实现项。统一能力的三人正式人工评审、实际评审台人工决定与重排调参
后续工程仍以根目录TODO.md为准。候选目前未提供独立完整业务快照卡，保留候选原文和
详情入口；本轮不增加候选业务协议。用户接下来的对话测试意见待实际使用后收集。
