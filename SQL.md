# 项目需要对接的中间件

腾讯内部中间件对接：
｜ 能力 ｜ 底层技术 ｜ NewsAgent 使用内容 ｜ 对接方式 ｜
｜--｜--｜--｜--｜
｜实时数据处理｜Oceanus/Flink｜小时或者分钟级别聚合曝光、点击、消费和互动｜聚合后写入数仓，不直接想Agent推送明细｜
｜企业内容中心｜结构化元数据放TDSQL、音频放COS｜新闻内容｜内容中心RPC，禁止 Agent 直接访问内容库表｜
｜企业检索平台｜腾讯云 Elasticsearch Service 搜索增强版｜关键词、全文、向量混合检索｜企业检索RPC，底层使用ES｜
｜Embedding与重排｜腾讯内部Embedding服务｜新闻向量化语义召回等｜检索平台调用｜
｜NewsAgent业务数据库 PostgreSQL｜运行、报告、反馈、索引、审批、版本、记忆｜PostgreSQL协议｜
｜应用部署｜TKE Kubernetes + TCR｜部署 API WOrker、Temporal、前端定时任务｜K8s｜
｜CMS｜腾讯新闻内部的CMS｜草稿提交、发布、撤回、状态回执｜CMS RPC/API｜

# 个人需要完成的内容
## 实现真实企业接入 Client：
    1. 指标 Client
        - 对接 TCHouse 指标服务
        - 查询当前窗口指标和历史基线
        - 校验水位，数据版本和指标口径
        - 返回 NewsMetricSnapshot
    2. 内容 Client
        - 按 news_id 查询标题、正文、来源和发布时间
        - 处理正文缺失、删除和受限状态
    3. 检索 Client
        - 对接企业混元或者统一模型网关
        - 支持结构化输入输出
        - 记录模型、Prompt和Schema版本
        - 处理超时、限流、鉴权和非法输出
## 热温冷分层
    实现统一的资源路由：

    热指标 -> TCHouse-C
    历史基线 -> TCHouse-P
    近期检索 -> ES热索引
    历史检索 -> ES温索引
    在线Artifact -> COS标准存储
    长期Artifact -> COS低频/归档

    项目中需要增加：
    - TierPolicy；
    - TieredNewsMetricSource；
    - TieredKnowledgeSearchClient；
    - Artifact存储层级信息；
    - 降级规则；
    - 每次运行使用的数据层和版本记录。