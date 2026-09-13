# 企业级新闻知识库：容量与架构设计

本文不讨论某个具体项目，而是一次**企业级规模推演**：按「每天 8000 篇新闻、保留 10 年」
的输入，推导知识库的容量、索引分层、写入/检索路径、成本模型与失败模式。

> 假设：8000 篇/天；图文为主，视频占比忽略；平均每篇 1500 tokens；切片 800 tokens、
> overlap 100；平均 6 切片/篇；向量维度 1024（如 bge-large-zh）；带元数据与原文。
> 口径不同时，结论按同一方法重算即可。

企业的行为数据
   -> 指标聚合 / 基线 / 热度 / 排行
   —> 富化(HotNewsEnrichmentService)
      -> 按 news_id 精确获取正文(NewsContentRepository)
      -> 关联新闻召回(KnowledgeSearchClient)
         -> Rerank
      -> EvidencePacket
      -> 热点分析Agent (只做解释证据，不产出权威数字)
      -> 校验 / 持久化 analysis_runs

执行新闻链路：
   召回命中一条新闻
      -> HeatTracker.touch(news_id)  (热度 +1/ 点击 +3)
      -> TierPolicy.decide(age, heat, ...)  (纯函数算目标层)
      -> TierMigrationService 按分区迁移      （fp16->int8->binary）
      -> 分层反过来决定召回的延迟和精度

---

## 0. 结论先行

1. **10 年约 2920 万篇、约 1.75 亿切片**，向量原始数据量在 **0.7～1.1 TB**（取决于精度），
   加文本与元数据后总量约 **1～1.5 TB**。
2. **磁盘不是瓶颈，内存和查询延迟才是。** 10 年的向量不可能全部驻留内存做单一 HNSW。
3. 必须**分层**：热层（近 30 天）全精度常驻内存；温层（近 1 年）量化+磁盘；冷层（1～10 年）
   对象存储+按需检索。
4. **成本大头是 embedding 计算与索引内存，不是对象存储。** 10 年 embedding 约 440 亿 tokens，
   用量化与本地模型可显著压降。
5. **向量库是可重建的检索投影，不是事实源。** 原文与元数据必须独立、可回放、可重建索引。

### 0.1 这层硬件的架构

Embedding = hunyuan Embedding -> API 实现
向量库节点  =  64-128 vCPU / 256-512 GB RAM / NVMe
冷层存储  =  2-4 TB NVMe 或对象存储
Redis  =  16-32GB  热度计算
PostgreSQL = 8-16 vCPU / 32-64GB 状态与台帐
对象存储  =  S3/MinIO  原文
应用  =  8-16 vCPU * N

---

## 1. 规模测算

### 1.1 数量级

新闻文本数量级：
8000/天  ->  292万/10年  ->  切片 512 分片(overlap 64-100)  ->  大概平均4个切片  ->  总计 1 亿切片数量
基本参数：
Embedding： Hunyuan Embedding
向量维度：   1024
距离度量：   内积度量
精读：      fp16
分片：      512 overlap(64-100)
索引：      热(3月)(m=32, ef_construction=200)
           温(1年)HNSW-int8 / IVF-PQ
           冷(1年以外)DiskANN
检索方式    混合检索: dense + spare(BM25/学习稀疏) + metadata过滤 + RRF -> 重排序
ANN候选    召回 top 100 - 200
重排模型    Youtu-Reranker(腾讯优图实验室为了企业级知识检索场景微调的模型) 显著提升证据精度
元数据      news_id,content_version,chunk_index,channel,publish_time,content_type,entities
新闻度SLO(核心用户体验的等级目标)   热点业务队时效铭感
版本治理    content_version + 蓝绿索引重建 ()

视频数量：
腾讯新闻一般是短视频，视频长度一般会小于100M，所以这部分就利用腾讯内部的ASR技术去做一个

> 切片数对容量影响最大，它由「正文长度 ÷ (chunk - overlap)」决定，而不是由文章数直接决定。
> 这部分做一个简单的判断，大部分新闻内容，只包含简单的文本200字不到，直接切片，对于深度报道，按照800字/100字切片
> 对于视频转出来的文本，按照同样的切片策略


### 1.2 存储量（1.75 亿切片）

存储量：
热/温层  fp16 + 冷层 int8/binary，十年总量压缩到 -0.5-0.8T，大部分不在线

延迟目标：
热层内存 HNSW：  144万切片    P99：3-8ms
温层  int8  HNSW/DiskANN  1700W. P99 20-50ms
冷层  DiskANN             1.58亿 P99  30-80ms
端到端(含重排)                    P99 200ms以内

向量库选择： Milvus

冷热层更新策略：
分层为主，访问频率为辅：
热层   30天  fp16   内存 HNSW         每日
温层   1年   int8   磁盘 HNSW/IVF-PQ  每周
冷层   10年  binary 磁盘 DiskANN      每月

冷热层更新算法：
整体架构：
Temporal Schedule (每天 02:00)
   -> VectorTierMigrationWorkflow (一次只处理越界分区)
      -> Activity: migrate_vector_partitions (分区整块迁移 + 改精度)
      -> Activity: promote_vector_exceptions (热度回升条目逐条提升)
   -> TierLedger(台账) + VectorStore(Milvus适配器) + HeatTracker(衰减计数)

晋升和下降策略
class TierPolicy:
   hot_ttl_days: int = 30
   warm_ttl_days: int = 365
   promote_warm: float = 5.0   warm -> hot 的晋升处理
   promote_cold: float = 8.0   cold -> warm 的晋升处理
   min_hot_days: int = 7
   promote_consecutive: int = 2  连续达标次数，防抖
   这边的策略：
      老事件热度回流 cold -> warm
      温层热点回升   warm -> hot
      提前降温      hot  -> warm

热度衰减计算：
   """生产环境端部署：记录一次访问并返回更新后的热度。"""
   """指数衰减计算： H <- H * 2^(-elapsed / tau_half) + weight 做一个半衰期的计算"""
   def touch(self, news_id: str, weight: float = 1.0, now: float | None = None) -> None:
      self._validate_identity(tenant_id=tenant_id, news_id=news_id)
      if weight <= 0:  raise ValueError("")

      args = [self._resolve_now(now), str(weight), str(self._tau_seconds, str(self._ttl_seconds))]
      try:
         raw = await self._touch_script(keys=[self._key(tenant_id=tenant_id, news_id=news_id)], args=args)
      except RedisError from exc:
         raise ValueError(f"touch heat failed: tenant_id=tenant_id, news_id=news_id")
      return self._to_float(raw)  # byte.decode() -> float

   生产环境 touch 用 Lua 脚本保证原子； get 也可以批量 pipeline

   def get(self, news_id: str, *, now: float | None = None) -> float:
      """读取当前热度()"""
      self._validate_identity(tenant_id=tenant_id, news_id=news_id)
      args = [self._resolve_now(now), str(self._tau_seconds)]
      try:
         raw = await self._get_script(keys=[self._key(tenant_id=tenant_id, news_id=news_id)], args=args)
      except RedisError as exc:
         raise HeatTrackerError(f"get heat")
      return self._float(raw)

   async def get_many(self, *, tenant_id: str, news_id: Iterable[str], now: float | None = None):
      """批量读取热度，使用pipeline批量逐key执行只读脚本，兼容Cluster(避免多key脚本跨slot，返回顺序与入参一致)"""
      核心实现
      try:
         pipe = self._redis.pipeline(transaction=False)
         for news_id in unique_ids:
            self._get_script(keys=,args=,client=)
            results = await pipe.execute()
         # pipeline表示的是 一次网络传输 把脚本全部执行
         # 创建缓冲区，往缓冲区里面加入指令，把指令全部发送过去

迁移执行：
   class VectorStore
   class TierLedger
   class TierMigrationService:
      def __init__(self, VectorStore, TierLedger, policy, encoder):
      async def migrate_partitions(self, now: datetime) -> dict:
         moved = []
         for month. target in self._boundary_partition(now):
            current == await self.ledger.get_partition_tier(month)
            if current == target:
               continue
            await self._migrate_month(month, current or self._infer(month), target)
            await self.ledger.set_partition_tier(month, target)
            moved.append({"month": month, "from": current, "to": target})
         return {"moved": moved}

      def _boundary_partitions(self, now: datetime)：

      async def _migrate_month(self, month: str, src: str, dst: str) -> None:
         offset = 0
         while True:
            batch = await self.store.scan(tier=src, month=month, limit=1000, offset=offset)
            if not batch:
               break
            
定时任务：
   Temporal任务：
      from temporalio.common import RetryPolicy
      Workflow ID固定为 vector-tier-migration, Temporal能保证同一时刻只有一个实例，天然防重复跑


   cron兜底：
      0 2 * * * flock -n /tmp/vector-tier.lock /app/.venv/bin/python -m app.vector_tier_worker --once >> /app/logs/tier.log 2>&1

幂等与一致性要点：
   迁移幂等：向量主键 news_id:version:chunk_index 不变，重放只覆盖
   先写后删： 目标层对账通过才删除原层，失败可以重跑
   路由切换：用 Milvus alias / 分区加载状态，切换原子； 查询按照 alias + 时间过滤
   例外提升：连续N次才提升，降级后冷却，避免抖动
   可观测性：每次运行记录迁移条数、耗时、对账差异、提升数目

### 1.3 Embedding 计算量

| 指标 | 计算 | 结果 |
| --- | --- | --- |
| tokens/天 | 8000 × 1500 | 1200 万 |
| tokens/年 | ×365 | 43.8 亿 |
| tokens/10 年 | ×10 | **438 亿** |
| API 成本（按 $0.02/1M tokens） | 438 亿 / 1M × $0.02 | **约 $876** |

这边调用的是
Embedding模型本身不消耗多少的API，但是切片策略版本分层就会考虑到，不同切片的版本发布，会考虑到这个问题

---

## 2. 分层存储与索引（核心设计）

查询分布极度倾斜：**绝大多数检索命中近期内容**，历史只在事件回溯、证据核查时用到。
因此按「热度/时间」分层，而不是把所有数据塞进一个索引。

| 层 | 时间范围 | 数据量（切片） | 存储/索引 | 目标延迟 |
| --- | --- | --- | --- | --- |
| L0 热 | 近 30 天 | 30×8000×6 ≈ 144 万 | 内存 HNSW，fp16/fp32 | < 50 ms |
| L1 温 | 30 天～1 年 | ~1700 万 | 磁盘 + 量化（int8/PQ），按天/周分区 | < 300 ms |
| L2 冷 | 1～10 年 | ~1.58 亿 | 对象存储 + 分区倒排/向量，按需加载或批量 | 秒级可接受 |

- **L0 全内存可行性**：144 万 × 1024 × 4B ≈ 5.9 GB，任何大内存节点都能常驻，甚至可多副本。
- **L1 量化**：int8 后 ~17 GB，磁盘 + 内存映射即可，召回损失用重排补偿。
- **L2 冷层**：不建全局 HNSW，改为「元数据/时间分区 + 关键词召回 + 小范围向量精排」，
  或对特定事件按需构建临时索引。

这部分是不同精度的向量的转换：
   python实现的代码：
      import numpy as np
      def to_fp16(vec: np.ndarray) -> np.ndarray:
         """fp32 -> fp16: 直接改 dtype， 省一半"""
         return vec.astype(np.float16)
迁移时不需要重建HNSW的边：
   HNSW支持增量插入，把一条向量插入到目标索引的时候，算法为他找邻居、连边，复杂度约o(log N)
   热层(fp16)/温层(int8) 都做HNSW处理
   热层(1w篇/6w切片/123MB向量内存/18MBHNSW图/总内存140MB)  构建时长(并行16-32核)秒级
      单篇文章插入：1篇6切片 = 3-20ms
         计算依据： ef_construction * 2M = 128*64 = 8192次距离计算，每次 1024 维乘加(约 2 KFLOPs)，共约 16 MFLOPs：单盒有效算力 GFLOPs，所以是毫秒级。
   温层()


> 分层原则：**越热越精确、越贵；越冷越便宜、越可近似。** 全量高精度是成本陷阱。

---

## 3. 写入路径

### 3.1 吞吐

- 平均：8000 篇/天 ÷ 86400 s ≈ 0.09 篇/s ≈ 0.56 切片/s。
- 峰值：新闻事件集中爆发时可达平均的 5～10 倍，按 **5～6 切片/s、峰值 30+ 切片/s** 设计。
- 结论：吞吐不高，**瓶颈在 embedding 的批量与限流**，不在写入带宽。

### 3.2 设计要点

```text
新闻清洗并落原文（对象存储 + 元数据库）
  → 同事务写入索引 Outbox
  → 异步 Index Worker 消费
  → 批量分片 + 批量 embedding
  → 写入向量库（幂等键 news_id + content_version + chunk_index）
  → 更新索引状态
```

- **幂等**：`news_id + content_version + chunk_index` 作为唯一键，重放不产生重复向量。
- **批量**：embedding 按批调用（如 64～256 条），兼顾吞吐与限流。
- **背压与死信**：队列积压时降速；超过重试上限进死信并告警，不无限重试。
- **对账**：定时比对原文表与索引状态，修复漏索引、半成功、版本不一致。

---

## 4. 检索架构

单一向量检索不够，企业级需要**混合检索**：

1. **结构化过滤**：时间窗口、频道、内容类型、实体、`news_id`（走元数据索引）。
2. **关键词召回**：BM25/倒排，保证专有名词、数字、罕见词精确命中。
3. **向量召回**：语义近似，弥补同义表达。
4. **融合与重排**：RRF/加权融合 → cross-encoder 或规则重排。
5. **去重与去自身**：按 `news_id`/事件聚合，排除当前新闻。

- **时间分区索引**：按天/周/月建分区，查询只命中相关分区，避免全库扫描。
- **多租户**：按租户隔离集合或分区，配合权限过滤，避免跨租户召回。
- **索引新鲜度 SLO**：明确「新闻发布 → 可检索」的延迟目标（如 5 分钟内），并监控积压。

---

## 5. 生命周期与治理

- **版本化**：内容更正/更新产生新 `content_version`，新版本索引成功后再失效旧版本。
- **撤稿/删除**：必须能定位并删除对应切片向量；删除不彻底会造成合规风险。
- **TTL 与归档**：冷层可压缩、降精度、转低频存储；过期内容按策略清理。
- **可重建**：向量与切片文本均可由原文+分片策略+模型重建，因此索引不是事实源。
- **评测闭环**：固定评测集 + 召回率/准确率/延迟指标，模型或分片变更必须过评测再上线。

---

## 6. 成本模型

| 成本项 | 主要驱动 | 优化方向 |
| --- | --- | --- |
| 对象存储 | 原文 + 切片文本 + 冷向量 | 生命周期、压缩、冷归档 |
| 内存/实例 | 热层 HNSW 常驻 | 分层、量化、只留近期 |
| Embedding 计算 | tokens 总量 + 重跑 | 本地模型、批量、避免全量重建 |
| 查询算力 | QPS × 复杂度 | 缓存、分区裁剪、近似检索 |
| 运维 | 分片、副本、对账 | 托管服务 vs 自建 |

> 经验：**存储便宜、内存和重跑昂贵。** 设计时应优先压热层规模、控制重跑频率。

---

## 7. 选型与分片

- **托管/一体（如 FastGPT 内置、云向量库）**：上手快，适合中小规模；超大规模受实例与成本约束。
- **专用向量库（Milvus/Qdrant 等）**：分区、量化、横向扩展成熟，适合 10 年量级。
- **搜索引擎（Elasticsearch/OpenSearch）**：原生混合检索与过滤，向量能力可用，运维熟悉。
- **pgvector**：与业务库同源、事务友好，但超大规模与高并发需谨慎评估。

**分片策略**：按时间分区 + 按租户/业务分片；避免单一超大索引导致重平衡和查询放大。

---

## 8. 失败模式与 SLO

| 失败模式 | 影响 | 应对 |
| --- | --- | --- |
| 索引落后 | 新新闻搜不到 | 监控积压，新鲜度 SLO 告警 |
| 召回下降 | 分析质量下降 | 固定评测集 + 线上点击/采纳反馈 |
| 分区失衡/热点分片 | 延迟抖动 | 预分片、副本、查询路由 |
| 删除不彻底 | 合规风险 | 删除即校验，定期审计 |
| 全量重建耗时 | 停机/成本 | 蓝绿索引 + 影子重建 + 切换 |
| 跨租户召回 | 数据泄露 | 租户级集合/分区 + 强制过滤 |

---

## 9. 设计原则（可迁移）

1. **分层**：按时间/热度分层，越热越精确，冷数据近似化。
2. **可重建**：向量库是投影，事实在原文与元数据，索引随时可重建。
3. **幂等**：写入、重放、重建都用稳定键，避免重复与漂移。
4. **异步**：写入与索引解耦，队列缓冲、背压、对账。
5. **混合检索**：结构化过滤 + 关键词 + 向量 + 重排，单一手段不够。
6. **评测驱动**：任何模型/分片/策略变更先过离线评测与线上指标。
7. **成本可控**：优先压热层内存与重跑次数，而不是省存储。
8. **合规优先**：删除、撤稿、租户隔离是一等公民，不是事后补丁。

---

## 附：一页速算表

```text
篇/天        8,000
篇/10 年     2,920 万
切片/10 年   1.75 亿
向量 fp32    ~718 GB
向量 fp16    ~359 GB
向量 int8    ~179 GB
文本+原文+元数据  ~300 GB
10 年总量    ~0.8–1.6 TB
热层(30 天)  144 万切片 / ~6 GB(fp32)
Embedding    438 亿 tokens / ~$876(按 API 价)
```



补充企业环境的知识：
Redis企业环境的利用：
2025年曝出了CVE事件，这个漏洞允许经过身份验证的用户通过恶意Lua脚本逃逸沙箱，在Redis主机上执行任意代码
Redis7.0开始，官方推出了RedisFunctions，用来代替传统的脚本EVAL
解决了Lua脚本几个老问题：脚本持久化在RDB/AOF中、自动复制到副本，通过FCALL按名字调用，不需要客户端每次发送脚本源码或者管理SHA1

Functions应用场景：
   秒杀库存扣减这类核心逻辑：
      业务有多个分片、多个客户端，用EVALSHA经常会遇到"script not found"-因为脚本缓存容易失去，Redis重启或者故障切换之后就丢失了
      Functions把扣减库存作为函数库家再一次，随RDB/AOF持久化、随主从自动同步，从根本上消灭脚本找不到的问题
   需要组织成库的复用逻辑：
      比方说有一组限流函数(令牌桶、滑动窗口、固定窗口),用Lua只能散落管理
   需要灰度发布和版本管理的逻辑：
      FUNCTION LOAD REPLACE 可以原子的替换整个库，配合双写影子校验，能实现业务中无中断的版本切换
   只读函数需要跑在副本上：
      Functions 提供了 FCALL_R0，可以安全的在只读副本上执行只读函数，分流副压力。Lua脚本的EVAL默认在主节点上执行。
EVAL:直接执行Lua脚本源码
SHA1:脚本指纹  每一个Lua脚本算出来40为十六进制哈希值，相当于脚本的身份证号
FCALL:是Redis7.0引入的新命令，用来调用已经加载的 Redis Functions


附录A：
KEYS[1] = 热度 key
ARGV[1] = 当前时间
ARGV[2] = 本次访问权重
ARGV[3] = 半衰期(秒)
ARGV[4] = TTL(秒)
_TOUCH_LUA = """
local key = KEYS[1]
local now
if AVGV[1] = '' then
   local t = redis.call('TIME')
   now = tonumber(t[1]) + tonumber(t[2]) / 10000
else
   now = tonumber(ARGV[1])
end
local weight = tonumber(ARGV[2])
local tau = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local h = tonumber(redis.call('HGET', key, 'h') or '0')
local ts = tonumber(redis.call('HGET', key, 'ts') or now)
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
if h > 0 and elapsed > 0 then
   h = h * (0.5 ^ (elapesd / tau))
end
h = h + weight

redis.call('HSET', key, 'h', h, 'ts', now)
redis.call('EXPIRE', key, ttl)
return tostring()
"""