# Redis 基础知识（Python 实践）

本目录是 Redis 入门学习材料：一份知识总结 + 7 个可运行的 Python 示例脚本。
每个脚本自包含，带中文注释，按编号顺序阅读即可。

## 运行准备

```bash
# 1. 安装 Python 客户端（任意 Python 3.10+ 环境）
pip install redis

# 2. 启动一个本地 Redis（最简单的方式，用完即删）
docker run --rm -d --name learn-redis -p 6379:6379 redis:7

# 3. 运行示例（在本目录下）
python 00_connect.py
python 03_set_zset_hotrank.py
```

所有脚本统一连接 `127.0.0.1:6379` 的 **db 15**（避免碰到其他项目的数据），
键名都带 `learn:` 前缀，跑完可执行 `redis-cli -n 15 FLUSHDB` 清理。
也可以用环境变量 `LEARN_REDIS_URL` 指向别的实例，例如：
`LEARN_REDIS_URL=redis://127.0.0.1:6390/15 python 00_connect.py`。

---

## 一、Redis 是什么

Redis（Remote Dictionary Server）是**基于内存的键值数据库**：

- 数据主要放内存，读写微秒到毫秒级，比磁盘数据库快 2~4 个数量级；
- 命令执行是**单线程**的（网络 IO 多路复用），所以单个命令天然原子，
  不存在竞态条件——这是 `INCR` 能做计数器、`SET NX` 能做锁的根本原因；
- value 不是简单字符串，而是丰富的**数据结构**（见下表），很多操作
  复杂度 O(1) 或 O(logN)。

## 二、核心数据类型

| 类型 | 一句话 | 典型命令 | 典型场景 |
|---|---|---|---|
| String | 字符串/数字/二进制，上限 512MB | SET GET INCR | 缓存、计数器、分布式锁 |
| Hash | 字段-值映射（小型对象） | HSET HGET HGETALL | 用户/商品对象缓存 |
| List | 双端链表（底层 quicklist） | LPUSH RPOP LRANGE | 消息队列、最新列表 |
| Set | 无序去重集合 | SADD SISMEMBER SCARD | 标签、抽奖去重 |
| ZSet | 带分数的有序去重集合 | ZADD ZINCRBY ZREVRANGE | **排行榜/热榜**、延迟队列 |
| HyperLogLog | 基数估计草图，固定 ~12KB | PFADD PFCOUNT PFMERGE | **UV 去重统计** |
| Bitmap | 位图（String 的位操作） | SETBIT BITCOUNT | 签到、活跃标记 |
| Stream | 持久化消息队列 | XADD XREADGROUP | 事件流（比 pub/sub 可靠） |
| Geo | 经纬度 | GEOADD GEORADIUS | 附近的人/店 |

## 三、过期与内存淘汰

- `EXPIRE key seconds` / `SET key value EX seconds`：TTL 到期后键被删除；
- 删除策略 = **惰性删除**（访问时检查）+ **定期删除**（后台抽样）；
- 内存满了按 `maxmemory-policy` 淘汰，常用：
  `allkeys-lru`（缓存场景，最近最少使用先淘汰）、
  `noeviction`（默认，写报错）、`volatile-ttl`（挑快过期的）。

## 四、持久化

| 方式 | 原理 | 特点 |
|---|---|---|
| RDB | 定时全量快照二进制 | 恢复快、可能丢最后一次快照后的数据 |
| AOF | 追加每条写命令 | 丢得少、文件大、有后台重写压缩 |
| 混合（默认推荐） | AOF 里嵌 RDB 全量 + 增量命令 | 兼顾两者 |

注意：持久化是**宕机恢复手段**，不改变"内存数据库"本质，也不替代真正的数仓。

## 五、高可用形态

- 主从复制：一主多从，读可分流；
- 哨兵 Sentinel：自动故障转移（主挂了选新主）；
- Cluster：16384 个槽分片到多节点，容量水平扩展。

## 六、缓存三大经典问题

| 问题 | 定义 | 应对 |
|---|---|---|
| 缓存穿透 | 查询**不存在**的 key，每次都打到数据库 | 缓存空值（短 TTL）；布隆过滤器前置拦截 |
| 缓存击穿 | 某个**热点 key** 过期瞬间，大量请求同时打到数据库 | 互斥锁重建（只放一个请求去查库）；热点不过期 |
| 缓存雪崩 | **大批 key 同时过期**或 Redis 整体不可用 | TTL 加随机抖动；多级缓存；限流降级 |

## 七、Python 客户端（redis-py）要点

```python
import redis

r = redis.Redis(host="127.0.0.1", port=6379, db=15, decode_responses=True)
```

- `decode_responses=True`：返回 str 而不是 bytes，几乎总是要加；
- 连接池：`redis.Redis()` 内置连接池，模块级复用一个客户端即可，不要每次新建；
- `pipeline()`：把多条命令打包一次网络往返，`transaction=True` 时以 MULTI/EXEC 包裹；
- 发布订阅：`r.pubsub()`，注意 pub/sub **不持久化**，离线期间的消息永久丢失，
  可靠场景用 Stream。

## 八、和热点数仓场景的关系（呼应学习脉络）

本系列的重点脚本 `03_set_zset_hotrank.py` 和 `04_hyperloglog_uv.py`
演示的就是实时热榜的在线层数据模型：

```
ZINCRBY hot:rank:2026101210 1 "N123"   # 小时桶内累加点击 → ZREVRANGE 取 TopN
PFADD    hot:uv:2026101210   "u10086"  # 小时桶 HLL 去重 → PFMERGE 跨桶合并 UV
EXPIRE   hot:rank:2026101210 7200      # TTL 即窗口的关闭与淘汰
```

要点回顾：时间窗口的本体是 **key 命名**（每桶一个 key）；
HLL 的合并（PFMERGE）正是"任意窗口 UV"能实现的原因。

## 文件清单

| 文件 | 主题 |
|---|---|
| 00_connect.py | 连接、连接池、String 基本读写 |
| 01_string_counter_ttl.py | 计数器 INCR、TTL、SET NX 锁雏形 |
| 02_hash_list.py | Hash 对象缓存、List 简易队列 |
| 03_set_zset_hotrank.py | Set 去重、ZSet 实时热榜（分钟桶/小时桶） |
| 04_hyperloglog_uv.py | HyperLogLog 统计 UV、跨桶合并 |
| 05_pipeline_pubsub.py | Pipeline 批量与事务、发布订阅 |
| 06_cache_patterns.py | 旁路缓存与穿透/击穿/雪崩应对 |
