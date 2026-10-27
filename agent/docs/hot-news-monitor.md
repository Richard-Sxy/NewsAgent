# 聊天页持续热点

聊天页快捷操作区下方展示持续监控结果。选择5或15分钟后开启，后台按一分钟步长推进；
用户关闭页面不停止监控，暂停/恢复是同租户共享监控操作，需要 hot-news:admin；
hot-news:read 只能查看。原来的“写一篇文章”“记住我的偏好”仍由用户主动操作。

## 完整调用链

1. ChatView → HotNewsMonitorPanel → hotNewsMonitorApi。浏览器只携带会话 Cookie。
2. /api/v1/hot-news-monitor 的 POST/PUT → 网关身份和 ADMIN 检查 → 隔离流式源。
   start_latest 选当前租户最新的、已开启采集的 source session，登记/复用原 hot_monitors。
   重复开启不重置游标；没有活动源返回409，不自动启动采集。
3. 原 monitor.process_one → 完整分钟/水位/基线/读者画像 → publish_window →
   持久派发台账 → 原 Temporal SnapshotWorkflow → DiscoveryRanker → 原分析与校验 →
   stream.reports。分析保存时增加经过正文取数绑定的 title，避免推送再取错新闻标题。
4. notifications.feed 在同一只读事务取每个监控最近的已完成报告；
   project_notices 只接受完整、有效、窗口时间匹配、达到原 hot-discovery-v2 门槛且已选中的新闻。
   公开正文、合成行为和本地模型明确标注；不生成新的热度公式。
5. 主 API 通过受限服务端 HTTP 客户端读小型投影，校验租户、Schema、大小和超时；
   /events 每5秒检查一次，以 SSE 下发变化后的整份卡片集合；无变化发心跳。
   连接最长约一分钟后重连重新鉴权。断线恢复重新读取事实源，不重做分析或写入聊天消息。
6. 前端按 tenant + news_id 派生的稳定 notice_id 更新卡片，最多3条。
   显示北京时间窗口、热度、点击/人数以及“查看分析”。同一新闻跨窗口更新原卡。
   最近已完成窗口无热点时不回退旧热榜；窗口失效、暂停、源暂停或监控不健康时撤下。
   窗口结束超过5分钟的结果过期，浏览器也每5秒清理过期卡片。

## 配置与运行边界

API 新增 HOT_NEWS_MONITOR_URL，指向隔离 streaming-lab 服务的 origin。
默认未配置，不启用；只允许 local/development/test/e2e。
现有本地 Compose 可叠加 deploy/docker-compose.hot-monitor.local.yml。
源服务、主 API 和前端都必须运行本次 WSL 代码；仅修改源文件不会替换旧镜像或 Windows 挂载。
流式源需要已有迁移010、运行中的 monitor/dispatcher/Temporal，并有已开启的 wallclock source
及完整指标/画像。新功能不修改迁移、不自动开启历史暂停的新闻采集。
旧报告缺少冻结标题时不作为新推送，等待本次代码产生的新报告。

外部依赖为现有 PostgreSQL、Temporal、隔离流式源和企业网关身份机制；不新增模型调用。
流式实验接口只能放在既有隔离网络/本机边界，浏览器访问主 API，不能直连无鉴权实验端口。
前端更新时延约为报告完成后下一次5秒检查，再加网络耗时，不代表数仓端到端5秒延迟。

## 验证入口与未完成项

- Python：tests/test_hot_news_monitor.py、test_streaming_window_query.py、test_hot_discovery.py、
  test_streaming_lab.py、test_streaming_dispatcher.py、test_hot_news_api.py。
  真实SQL测试复用 WINDOW_QUERY_TEST_DSN（数据库名必须 test_window_ 开头），
  WINDOW_QUERY_TEST_INIT_SQL 和 LAB_MIGRATIONS，必须使用一次性空数据库。
- 前端：node --test tests/hot-news-monitor.test.mjs tests/conversations.test.mjs；
  vue-tsc、相关文件 ESLint 与 Vite 构建。
- 待完成：当前常驻部署切换与开启实际持续采集、真实企业源 Adapter/企业验收、
  多用户长稳和负载验证。此功能不实现手机/站外推送，也不将5/15分钟监控改成全天查询。


2026-10-27 本次验证：后端118通过、4项依赖型测试跳过；前端专项54通过，类型检查、
相关文件ESLint及Vite构建通过。真实PostgreSQL测试覆盖已完成报告→隔离源HTTP→主API
的租户绑定、投影和暂停；SSE验证包含变更通知、心跳、续连接和鉴权。模型仍为本地Port。
部署核对：流式服务仍挂载 E:\NewsAgent；主API的业务代码来自镜像，当前并未部署本次修改。
