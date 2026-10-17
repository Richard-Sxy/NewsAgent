# 腾讯新闻主题受众接口执行计划

本次实现服务新闻运营的实际动作：运营人员在已完成的热点报告中选择一篇新闻和主题标签，
查询企业画像系统按标签去重后的受众规模，结合通知资格判断是否值得进入人工推送决策。
用户已确认，腾讯新闻依据每个用户访问的新闻内容与主题生成兴趣标签，并据此推荐和推送。
个人兴趣生成、推荐排序和最终投递继续由企业已有系统负责。

## 企业落地链路

```text
C端访问与浏览 → 企业行为数仓和画像算法 → 用户主题与细分兴趣标签
                                                   ↓
运营热点工作台 → 已授权的run/news → 版本化主题目录 → 标签组合
                                                   ↓
                         企业聚合受众接口 → 去重人数与通知资格人数
                                                   ↓
                    NewsAgent身份/版本/水位/时效校验 → 人工建议预览
                                                   ↓
                              现有企业审批与推送平台（后续联调）
```

NewsAgent 不拉取接收者名单、设备Token或企业原始浏览明细。新增接口不执行真实推送，
不把已有候选策略的校验通过状态当作发送授权。预览不承诺持久幂等重放；其fingerprint仅
标识相同冻结输入与受众快照。

## 执行步骤

1. **主题目录**：建立稳定ID、一级主题、二级细分、启停和来源标识。科技、财经、AI、军事、
   教育以及科技下自然来自用户描述；其他细分是可编辑草案。目录单独版本化，本地草案
   只能用于模拟，企业运行必须换成已批准的真实目录。
2. **企业受众Port**：实现固定URL的HTTP Adapter和严格聚合响应契约。绑定租户、热点运行、
   新闻、标签版本、组合规则、时间窗、画像版本、水位和失效时间。企业按组合统一去重，
   不把分标签人数相加。仅本地环境可显式使用相同Port的合成替身。
3. **热点业务接入**：先验证当前租户已有这篇新闻的分析，再外调受众服务。目录查询使用
   现有热点读取权限，预估使用运营决策权限。页面在用户点击后加载目录和请求预估，选择
   或新闻变化立即作废旧结果，合成数据与失效时间可见。
4. **合同与回归**：验证目录父子关系、组合去重、身份错配、未知标签、过期/不完整水位、
   超时、限流、额外用户明细拒绝，以及页面迟到响应防护；同时运行既有相关热点/API测试。
5. **交付与长期背景**：记录调用链、配置、测试和企业联调TODO，更新已确认的长期业务背景，
   交付一页中文PDF。

## 接口合同

- `GET /api/v1/audience/topics` 返回有版本的两级目录与当前来源模式。
- `POST /api/v1/audience/runs/{run_id}/news/{news_id}/preview` 接受目录版本、包含/排除的标签
  和显式 `any/all` 组合。租户和操作者只来自可信网关，不接受请求体身份。
- 企业Adapter向配置的完整endpoint发送 `AudienceQuery`。该地址是本项目建议的接入合同，
  尚不是腾讯新闻真实IDL；企业网关可映射其内部标签服务。
- 聚合响应保留 `snapshot_id/profile_version/source_version/taxonomy_version`、窗口、
  `watermark/generated_at/expires_at` 和 `matched_users/eligible_users`，不含逐用户字段。
- 输出固定 `preview_only=true/manual_review_required=true/dispatch_enabled=false`。
  零资格受众返回观察建议；依赖失败时明确不可用，不回退合成数据或旧估算。

## 技术参考与采用方式

- [腾讯云移动推送标签功能](https://intl.cloud.tencent.com/zh/document/product/1024/35392)
  展示自定义标签及与或组合；本项目据此把主题选择与平台受众解析分开。
- [腾讯云移动推送接口](https://cloud.tencent.cn/document/product/548/39064)
  提供组合标签、离线有效期和渠道参数；本次只预估，投递时须另行确认实际平台口径。
- [腾讯云智能推荐数据结构](https://cloud.tencent.com/document/product/1541/78996)
  区分内容、行为和推荐追踪标识；本项目沿用新闻ID关联，接收企业聚合画像而非复制用户明细。
- [FCM主题消息](https://firebase.google.com/docs/cloud-messaging/topic-messaging)
  明确主题广播与小群体低延迟投递的适用性不同，因此标签不能替代完整的投递决策。
- [FCM投递观测](https://firebase.google.com/docs/cloud-messaging/understand-delivery)
  区分受理、到达等阶段；受众预估人数不等于送达、展示或点击人数。
- [APNs请求与失效机制](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns)
  支持失效时间和尽力投递；后续接入需要保留新闻时效与结果回执。

以上是公开推送与推荐产品的工程参考，不能证明腾讯新闻内部采用相同服务或算法。

## 验收状态

2026-10-16 已完成步骤1至5的代码与本地合同验证：两级目录、企业HTTP Adapter、同Port
合成替身、受权热点绑定与聚合校验、可选API生命周期装配，以及热点详情的按需预估面板。
本地目录为5个一级主题、20个细分标签，共25节点。目录是部署时冻结的YAML快照；
企业网关须按相同版本映射内部真实标签ID，不会把公开栏目自动认定为企业画像标签。

后端受众4模块及相邻热点/推送/健康检查共168项通过。测试覆盖HTTP合同、两级目录、
权限、串租户/串新闻、未来或不完整水位、失效、60秒有效期、默认关闭、启动装配及关闭清理。
存在上游Starlette和OTel弃用提示，不影响本次通过结果。前端受众专项15项通过，相邻
运营反馈和分析57项通过，类型检查、生产构建和改动文件ESLint通过。

`output/audience-interface-20261016/api-example.json` 保存了真实路由与服务调用合成Port
的示例结果；已授权热点报告的仓储使用测试替身。该验证不连接企业系统、真实数据库或
推送平台，不代表生产数据、企业身份、标签算法和实际投递收益验收。没有重启现有业务实例。

## 实际调用链与文件位置

`HotNewsView` → `HotNewsRankTable` → `AudiencePreviewPanel` → `api/audience.ts`
→ `/api/v1/audience/topics` / `.../preview` → 可信网关身份与现有热点权限
→ `AudiencePreviewService` → `PostgresHotNewsRunStore.get_analysis_memory`
→ `TopicCatalog.validate_ids` → `AudienceQuery` 与指纹 → `AudienceProfilePort`
→ `HttpAudienceProfileClient` 或 `LocalAudienceProfileClient` → `AudienceSnapshot`
→ 身份/版本/窗口/水位/时效重复核验 → Python受众建议 → 页面展示。

`main._business_lifespan` 从配置加载目录、创建服务并关闭HTTP Client。依赖继续使用现有
Pydantic、PyYAML、httpx、FastAPI和PostgreSQL；没有新增包、表或数据库迁移，也不调用
模型生成标签人数。HTTP使用请求指纹头做追踪，不以此承诺幂等结果缓存。

## 配置与本地使用

默认 `AUDIENCE_ENABLED=false`。本地隔离栈可在现有Compose文件最后追加
`deploy/docker-compose.audience.local.yml`，它仅为API选择synthetic并只读挂载目录。
需要先重建包含当前源码的后端与前端；已有进程不会自动获得新代码。原生基础栈示例：

```bash
cd agent
docker compose -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.audience.local.yml build migrate
docker compose -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.audience.local.yml up -d api
```

以上只适用于对应的原生本地栈。BGE等其他演示栈应保留其原有项目名、镜像、Compose文件
和数据卷，重建对应镜像后将受众overlay最后追加。前端可使用既有开发服务器或重建其镜像。
在 `/hot-news` 展开已完成分析的新闻，点击“选择受众主题”，纳入或排除主题并选择任一/全部，
点击“预估受众”。目录、新闻或选择变化后旧结果立即作废；过期结果需重新预估。

企业模板为 `deploy/audience.enterprise.env.example` 和
`deploy/audience-topics.enterprise.example.yml`。生产模式需要批准目录、HTTPS完整endpoint
和由密钥系统注入的 `AUDIENCE_API_TOKEN`，不读取示例中的空Token作为有效凭证。
`AUDIENCE_TIMEOUT_SECONDS` 默认5秒，响应上限 `AUDIENCE_MAX_RESPONSE_BYTES` 默认65536；
`AUDIENCE_MAX_SNAPSHOT_AGE_SECONDS` 默认900秒，同时限制快照年龄和最大有效期。
HTTP不重定向、不使用环境代理、不自动重试；企业失败不回退本地模拟。

本次PDF生成链为 `output/diagrams/render_audience_integration_20261016.py`
→ 合成API示例JSON → ReportLab与本机中文字体
→ `output/pdf/newsagent-enterprise-audience-integration-2026-10-16.pdf`，仅一页。
它是交付说明，不进入业务运行时。通过页数、中文文字抽取及PNG渲染检查排版。

测试入口：

```bash
cd agent
python -m pytest -o addopts='' -q tests/test_audience_taxonomy.py tests/test_audience_client.py tests/test_audience_preview.py tests/test_audience_runtime.py tests/test_hot_news_api.py tests/test_push_policy.py tests/test_push_strategy.py tests/test_health.py
cd frontend
node --test tests/audience-preview.test.mjs
npm run build
```

## 企业联调待办

真实标签ID与层级、批准目录版本、画像及资格口径、接口鉴权/TLS与内部URL、窗口和水位、
受众聚合去重、通知同意与频控策略、SLA与超时需要企业合同确认。实际审批、投递及其回执
继续单独联调；本地合成链路不代表真实推荐收益或企业接口验收。
