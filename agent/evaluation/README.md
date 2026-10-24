# 热点 Agent 样本、评测与故障演练

这套工具不依赖企业 RPC，可以先完成三件事：定义稳定样本格式、建立 Agent 评测基线、验证
故障是否被正确重试或阻塞。它不是完整 Data Loop，也不会自动修改 Prompt、规则或生产版本。

数据集已按 `sql_security / business_sql / retrieval / writing / hot_news` 分类，
各目录用途和保留依据见 [数据集索引](datasets/README.md)。

## 1. 你现在怎样补评测内容

从 `datasets/hot_news/hot_news_eval_seed_v1.json` 复制一个 case，只填写两部分：

1. `analysis_input`：热点新闻、确定性指标、热度分量，以及允许模型看到的关联证据。
2. `expected`：允许的主驱动、必须/禁止引用的证据、必须引用的指标，以及是否必须说明局限。

不需要人工编写一整篇“标准分析报告”。这种最小标签更容易保持一致，也允许模型使用不同但
合理的表达。

当前 `datasets/hot_news/hot_news_eval_seed_v1.json` 已按下列分层补齐 60 条
（`dataset_version` 为 `2026-10-13.v3`；前 30 条沿用 v2 六大基础分层，后 30 条为 v3 扩充）：

| 分层 | 数量 | 重点 |
|---|---:|---|
| 明显热点 | 5 | 点击、消费、互动或增长主驱动 |
| 低样本/非热点 | 5 | `insufficient_data` 和 limitation |
| 有效关联证据 | 5 | 必须引用同主体、同事件证据 |
| 主体冲突 | 5 | 同为财报但公司不同、同名实体等 |
| 证据不足/元数据缺失 | 5 | 不得编造 URL、新闻 ID 或事实 |
| 安全与边界 | 5 | 新闻正文含指令、无依据数字、确定性因果 |
| 时段覆盖（v3） | 5 | 早高峰/午间/晚高峰/深夜/凌晨窗口 |
| 突发事件（v3） | 5 | 台风、地铁停运、召回、山火、云故障；含跨主体干扰证据 |
| 旧闻翻炒与时效（v3） | 5 | 旧政策、旧数据、周年回顾、过期总结、旧访谈剪辑 |
| 地域事件（v3） | 5 | 同题材不同地域的必须/禁止证据 |
| 多主驱动与内容形态（v3） | 5 | 均衡分量、纪录片消费、深度互动、图文视频伴随、长期记忆参与 |
| 边界与安全追加（v3） | 5 | 证据摘要注入、正文伪造数字、同分冲突证据、软文、视频字幕指令 |

继续扩充到 100 条时，再增加多语言样本、跨栏目联动、连续多窗口同主体、时政敏感边界和真实脱敏回填案例。
真实样本必须脱敏，只保留聚合指标和允许引用的新闻证据，不能复制企业用户行为明细。

校验数据集但不调用模型：

```bash
python -m evaluation.run_hot_news_eval
```

显式运行模型评测：

```bash
ENTERPRISE_MODEL_API_KEY=*** python -m evaluation.run_hot_news_eval \
  --run-model \
  --runtime-config deploy/model-runtime.enterprise.yml \
  --model-route YOUR_APPROVED_MODEL_ROUTE
```

脚本不会输出 API Key。报告包含 Schema/业务校验通过率、主驱动准确率、必要证据召回率和
必要指标覆盖率。数据集内容哈希用于确认两次评测使用的是同一版本。

## 2. 没有企业 RPC 时怎样验证接入

现有 `tests/test_enterprise_hot_news_adapters.py` 就是企业契约模拟层。Fake Client 返回与未来
RPC 相同的 Pydantic DTO，已覆盖：

- watermark 和分页；
- 企业枚举、毫秒时长与领域对象转换；
- 基线策略版本和数据版本漂移；
- 内容批量完整性与显式 missing；
- 检索自身排除、切片去重和索引版本。

你拿到 IDL 后，只需要把 Fake Client 换成生成 SDK 的薄封装。测试样本和业务断言继续复用。

## 3. 故障演练

运行：

```bash
python -m evaluation.run_fault_drills
```

当前计划包含 9 类场景：RPC 超时、不可用、限流、鉴权失败、watermark 不完整、版本漂移、
内容部分响应、模型伪造证据和 PostgreSQL 短暂失败。脚本通过真实 `HotNewsActivities` 错误
边界验证：

- 超时、限流、服务不可用和短暂持久化失败允许 Temporal 最多再试一次；
- 鉴权、数据不完整、响应契约错误和模型违规直接阻塞并告警；
- 每次失败都写入低基数监控事件，不把 `tenant_id`、`news_id` 或用户标识放进指标标签。

这属于仓库级故障分类演练，并不宣称真实网络故障已经验证。拿到企业测试环境后，应继续执行
真实超时、限流、连接中断、部分成功和恢复后的幂等重放。

## 4. 建议的生产告警

| 告警 | 建议条件 | 处理方式 |
|---|---|---|
| 数据水位落后 | watermark 连续两个窗口未覆盖 `window_end` | 阻塞分析，联系数仓负责人 |
| RPC 失败率 | 5 分钟失败率持续高于基线 | 按 `error_type` 定位依赖服务 |
| 模型校验失败 | Schema/证据/指标校验失败率异常上升 | 禁止发布，保留 bad case |
| 窗口无结果 | 有行为记录但排行或分析数为 0 | 检查口径、内容缺失与过滤阈值 |
| 调度延迟 | 窗口结束后超过 SLA 尚未完成 | 检查 Temporal 队列与下游 RT |
| 幂等异常 | 同一幂等键出现不一致结果 | 硬阻塞并检查版本/写入链路 |

`InMemoryHotNewsRunMetrics` 只用于本地测试和演练。生产部署时实现相同 `HotNewsRunMetrics`
协议，对接企业 Metrics SDK；热点 Worker 的 Bootstrap 已支持注入该实现。
