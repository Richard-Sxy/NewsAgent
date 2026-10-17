"""Render an implementation map and a blank planning worksheet.

Chain: main -> worksheet()/render() -> UTF-8 JSON / Diagram + ReportLab -> PDF.
Inputs: reviewed repository contracts and proposed implementation guidance.
Config: user_fill fields stay null; fixed A3 landscape and embedded Chinese fonts.
QA: JSON parse/null checks, pypdf one-page/text checks, Poppler visual inspection.
TODO: enterprise service contracts and all planned indexing/state-machine work.
The worksheet is NOT an application runtime configuration or a benchmark result.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent/evaluation"))
from render_real_news_warehouse_pdf import (  # noqa: E402
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def worksheet() -> dict:
    def field(key, unit, meaning, mapping, constraint):
        return {
            "name": key, "user_fill": None, "unit": unit,
            "meaning": meaning, "implementation_status": mapping,
            "constraint_or_decision": constraint,
        }

    return {
        "worksheet_version": "planning-v1",
        "purpose": "参数填写和实现检查表；不能直接作为现有 YAML 加载",
        "status_legend": {
            "已有YAML": "现有解析器已支持；按对应模型或检索配置填写",
            "已有代码参数": "构造函数/策略对象已支持；尚需接配置和依赖注入",
            "企业侧合同": "从平台获得并冻结；不能根据本地模拟猜测",
            "待实现": "需要先增加配置Schema、行为实现与测试",
        },
        "rules": [
            "所有 user_fill 留空，由使用者填写；密钥只填环境变量名或密钥引用，不能填写密钥值。",
            "已有配置 extra=forbid；待实现字段不能直接塞进当前模型/检索 YAML。",
            "首版统一模型、切片版本和索引类型，三集合预加载；逻辑分层不保证延时排序。",
            "本文是实施建议，企业参数和生产架构尚未据此变更。",
        ],
        "groups": {
            "1_enterprise_connections": [
                field("content_source_contract", "接口合同", "正文/发布时间/版本或有序修订号/撤稿/分页游标", "企业侧合同", "明确重复、乱序、更正、撤稿与游标过期语义；更新时间不能单独充当版本"),
                field("embedding_endpoint_and_route", "URL及路由", "模型接口及固定模型路由；只记录api_key_env名称", "已有YAML：embedding.url / model_routes / api_key_env", "企业环境 provider=openai_compatible；现有模型文件还要求inference/prompts，保留原合同"),
                field("postgres_test_database", "连接引用", "隔离测试库、账号与连接预算", "企业侧合同", "可进行本项目版本化迁移；只记录密钥引用"),
                field("milvus_connection", "连接参数", "uri/database/token_env，服务和SDK版本", "已有YAML：milvus.uri / database / token_env", "uri中不带凭据；版本、权限、配额、索引支持向平台确认"),
                field("milvus_collections", "三个名称", "hot/warm/cold各自独立集合", "已有YAML：milvus.collections", "名称不同；字段匹配adapter，手动chunk_id主键，维度和度量一致；新增投影字段需迁移Schema"),
                field("temporal_namespace_queue", "命名空间/队列", "复用已有Temporal，新增索引工作流与Worker队列", "企业侧合同 + 待实现工作流", "调度成功不等于索引完成；PG任务状态可被恢复派发"),
                field("test_tenant_and_monitoring", "身份/地址", "可信测试租户、日志/指标/告警入口", "企业侧合同", "隔离数据和写权限；缓存命中也须过状态校验"),
            ],
            "2_vector_identity_and_chunking": [
                field("embedding_revision", "不可变版本", "权重revision、预处理、归一化规则及query/document前缀", "企业侧合同 + 待实现完整身份绑定", "相同维度不代表兼容；服务路由若静默换模型必须阻止混用"),
                field("expected_embedding_dimensions", "维", "存储和查询向量维数", "已有YAML：embedding.expected_embedding_dimensions", "代码允许1..65536；必须等于模型实际维度和collection维度，不是性能调参旋钮"),
                field("metric_type", "IP/COSINE", "向量检索相似度", "已有YAML：milvus.metric_type", "与已建索引一致；IP和COSINE只有满足归一化约定时才可对应"),
                field("chunk_chars_and_overlap", "字符", "确定性切片长度和重叠", "已有代码参数：PostgresKnowledgeStore(chunk_chars, chunk_overlap)", "chunk_chars>0且0<=overlap<chunk_chars；字符数不是token数，标题/前缀也计入模型输入预算"),
                field("chunking_version", "版本", "切片器/清洗/标题拼接规则版本", "待实现：投影身份与chunk_id纳入此版本", "改变规则需要新投影，不能复用旧切片编号覆盖旧内容"),
                field("embedding_batch_size", "条/批", "每次Embedding输入条数", "已有YAML：embedding.max_batch_size；现有PG工厂将其接到入库批次", "YAML允许1..2048但服务可能更小；同时满足token、条数、维度和当前HTTP响应1MiB上限；查询端当前不自动拆批"),
                field("embedding_token_budgets", "token/条与token/批", "单条和整批token上限", "企业侧合同 + 待实现切片后的token预算检查", "不得静默截断导致正文缺失；超限需重新切片并记录规则版本"),
            ],
            "3_lifecycle": [
                field("hot_age_boundary", "秒", "未显式提升时HOT的年龄上界Th", "待实现", "0<Th<Tc；age=virtual_now-publish_time；HOT为age<Th"),
                field("cold_age_boundary", "秒", "未显式提升时COLD的年龄下界Tc", "待实现", "WARM为Th<=age<Tc，COLD为age>=Tc；边界无重叠无缺口"),
                field("unknown_or_future_time_policy", "策略", "缺失或未来发布时间的处理", "待实现", "单独待核验，不伪造日期或默认为冷层；记录排除量"),
                field("reheat_hold_and_cooldown", "秒", "显式回热事件后的保持期及再次迁移冷却期", "待实现", "首版回放确定性提升事件；后续接Python/SQL热度规则，抑制反复搬迁"),
                field("migration_interval_and_batch", "秒/篇", "扫描频率与每批迁移文章数", "待实现", "过快增加竞争和服务负载；过慢增加层级滞后；时间和批大小分别填写"),
                field("visibility_consistency_and_deadline", "模式/秒", "写入后及后续查询的可见性保证", "企业侧合同 + 待实现一致性参数传递/校验", "ACK或contains不等于Search可见；跨进程读者必须能继承同等可见性保证，不能只验证写客户端Session"),
                field("content_correction_policy", "策略", "更正接受后旧版能否继续服务", "待实现；建议首版严格阻断旧版", "PG先提升最低可服务版本，新版未就绪时该新闻暂不可用；测量此空窗"),
            ],
            "4_recall_and_index": [
                field("index_type_M_efConstruction", "索引/整数", "HNSW连接数M和构建搜索宽度efConstruction", "企业侧建索引参数；不是当前搜索YAML字段", "首版各层相同；调大通常增加内存/构建成本，变更按服务规则重建索引"),
                field("candidate_limit", "篇", "融合阶段的候选新闻上限", "已有代码参数：RelatedNewsSearchQuery.candidate_limit", "与最终证据条数以及单层切片limit区分；上层仍可能再过滤截断"),
                field("per_tier_limit", "切片/层", "每层请求的向量命中数量", "已有代码参数：TierSearchPolicy.per_tier_limit", "实际limit=max(per_tier_limit,candidate_limit)；去重/状态过滤后可能不足篇数"),
                field("ef", "候选宽度", "HNSW查询阶段的搜索宽度", "已有YAML：milvus.search_params.ef", "至少满足部署版本对实际单层limit的约束；调大常提高近似召回并增加耗时；当前三层共用"),
                field("rrf_k_and_weights", "正数/权重", "RRF平滑参数与各层权重", "已有代码参数：TierSearchPolicy", "rrf_k不是TopK；权重改变排名，不自动减少该层耗时；迁移双副本须按有效层过滤后融合"),
                field("refill_budget", "轮次/切片数", "状态过滤与新闻去重不足时扩大候选的上限", "待实现", "设置总deadline和最大候选预算；达到上限返回真实不足，不拿旧稿补足"),
                field("query_routing_mode", "策略", "首版三层并发全查，后续才评估按条件查冷层", "已有并发核心；聊天入口需统一", "三层并发总耗时受慢层影响；跳过冷层需独立评测召回损失"),
            ],
            "5_timeouts_and_capacity": [
                field("embedding_timeout_seconds", "秒", "单次模型HTTP超时", "已有YAML：embedding.timeout_seconds", "代码允许>0且<=300；单请求预算仍需扣除排队/检索/校验"),
                field("vector_timeout_seconds", "秒", "单次向量调用超时", "已有YAML：milvus.timeout_seconds", "代码允许>0且<=60；search显式传递，写入/检查/删除需补全deadline语义；当前各层共用"),
                field("request_deadline_seconds", "秒", "检索全链路deadline", "待统一入口与接配置", "覆盖排队+Embedding+max(各层检索)+PG校验+融合；重试共享剩余预算"),
                field("query_and_ingest_concurrency", "并发数", "在线查询与后台索引的独立资源上限", "已有每批max_query_concurrency；全局/进程/企业限流待实现", "一条查询可发三次层级请求；批内semaphore不能代替跨请求全局限制"),
                field("activity_timeouts_and_retry", "秒/次数", "StartToClose单次、ScheduleToClose含重试总预算、退避及最大尝试", "Temporal已有能力；新索引工作流待接", "只重试暂时错误；schema/维度/权限错误分类处理；避免SDK/Activity多层重试相乘"),
                field("heartbeat_and_checkpoint", "秒/批次", "长任务存活信号和可恢复进度", "待实现", "按已完成批次持久化；任务重试能重复执行；每篇新闻用generation做条件激活"),
                field("postgres_pool_budget", "连接数", "API/Worker各进程连接池和数据库总配额", "现有部署参数需核对", "考虑所有副本及迁移/监控预留；远程Embedding/向量调用不占用长DB事务"),
            ],
            "6_test_and_acceptance": [
                field("replay_fixture_and_seed", "数据集/种子", "固定事件文件与查询集版本", "待实现回放器", "包含新增、更正、撤稿、降层、回热、重复、乱序、故障；每轮重置隔离状态"),
                field("virtual_clock", "ISO8601/推进序列", "控制新闻年龄的固定业务时间", "待实现", "只改变分层判断；耗时用真实单调时钟，不能用虚拟时间计算RPC延时"),
                field("workload", "QPS/事件每秒/并发/时长", "查询和变更混合流量", "待实现负载器", "每次只变一个维度；报告实际到达率/完成率/排队，不只报告成功请求延时"),
                field("latency_and_freshness_slos", "毫秒/秒/比例", "端到端p95/p99、各层耗时、更新可见、撤稿屏蔽、失败和恢复预算", "使用者约定，待测试实现", "样本量不足不宣称可靠p99；撤稿按PG提交后新发起查询判定；空结果/缺层另统计"),
                field("quality_protocol", "查询集/判定集", "人工相关性与ANN近似误差分开", "已有单层ANN工具；三层及人工相关性需接协议", "精确基线使用同模型、同数据快照、同租户/版本过滤；向量自查询不能替代语义质量"),
                field("resource_or_fault_experiments", "实验方案", "资源/索引参数变化与故障延时注入", "待实现，可在稳定基线后单独运行", "同资源三集合不保证HOT快于COLD；sleep/网络延时注入只证明预算和故障处理，不能宣称索引性能"),
            ],
        },
        "implementation_gates": [
            "G1：固定模型身份；一篇文档由真实Embedding生成向量，隔离Milvus集合可检索。",
            "G2：PG同事务保存事件/状态/job；派发失败可恢复，游标不跳过未保存事件；Worker切片/Embedding/写目标/可见性校验/条件激活。",
            "G3：统一查询入口；有效版本、投影、层及撤稿Gate通过，跨层重复副本不重复计分。",
            "G4：更正/撤稿/降层/回热；迁移复制并验证→PG条件切有效层→精确清理旧投影；旧任务不能重新激活。",
            "G5：在派发、写入、激活、清理边界中断Worker；重放重复/乱序事件；验证重启和断连恢复。",
            "G6：固定查询集混合负载；报告参数版本、各层数据量、延时、质量、新鲜度与积压。",
        ],
    }


def render(output: Path) -> None:
    d = Diagram(output)
    d.c.setTitle("NewsAgent - 分层召回实现与测试路线")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / IMPLEMENTATION PLAYBOOK / 2026-10-15", 32, 20, 10, GREEN, True)
    d.text("照着这条链路实现，再用事件回放验收", 32, 43, 28, INK, True)
    d.text("第一版目标：内容状态正确、任务可恢复、延时可解释。具体阈值与企业参数由你填写。", 32, 88, 13, MUTED)
    d.box(32, 117, 1125, 38, PALE_AMBER)
    d.text("当前已有：模型Port、PG知识存储、Milvus适配、分层检索核心；待接：持久任务、有效版本校验、迁移恢复与统一查询。", 46, 128, 11, AMBER, True)

    d.text("先打通一个闭环，再扩成三层", 32, 173, 16, INK, True)
    nodes = [
        (32, 200, "① 企业内容接口", "news_id / 修订号 / 正文\n发布时间 / 撤稿 / 游标"),
        (266, 210, "② PostgreSQL", "同事务：文档状态 + 索引任务\n版本 / 有效投影 / generation"),
        (510, 195, "③ Temporal Worker", "确定性切片 → Embedding\n分批 / 限流 / 超时 / 重试"),
        (739, 190, "④ Milvus三集合", "HOT / WARM / COLD\n写入 → 校验完整与可见"),
        (963, 194, "⑤ 条件激活", "PG核对版本和generation\n生效后清理旧投影"),
    ]
    for x, width, title, body in nodes:
        d.box(x, 207, width, 88, PALE_BLUE, BLUE)
        d.text(title, x+11, 218, 13, BLUE, True)
        d.para(body, x+11, 249, width-22, 10, 33)
    for x1, x2 in ((232, 259), (476, 503), (705, 732), (929, 956)):
        d.arrow(x1, 251, x2, 251, BLUE)
    d.box(32, 310, 1125, 50, PALE_GREEN, GREEN)
    d.text("查询入口", 47, 322, 13, GREEN, True)
    d.text("同版本Query向量 → 三层并发 → PG有效状态校验 → 新闻去重 / 融合 → 证据与耗时Trace", 143, 323, 13, GREEN, True)

    d.text("四种内容变化：先写清允许服务哪个版本，再写代码", 32, 380, 16, INK, True)
    cards = [
        (32, "新增", "落库待处理 → 新向量可检索\n→ PG激活 → 查询可返回"),
        (318, "更正", "PG先阻断旧版 → 构建新版\n→ 条件激活；期间可暂缺该稿"),
        (604, "撤稿", "PG提交墓碑 → 新查询屏蔽\n→ 异步清理全部层的旧切片"),
        (890, "迁移 / 回热", "复制 → 验证 → PG切有效层\n→ 清理；旧任务不得复活内容"),
    ]
    for x, title, body in cards:
        d.box(x, 417, 267, 78, white, GREEN)
        d.text(title, x+12, 427, 13, GREEN, True)
        d.para(body, x+12, 454, 243, 10, 32)

    d.box(32, 515, 548, 155, PALE_BLUE, BLUE)
    d.text("参数分三类填写", 48, 527, 16, BLUE, True)
    d.para("先冻结身份：模型revision / 维度 / 归一化 / metric / 切片版本\n再定策略：年龄边界Th<Tc / 回热保持 / 迁移周期\n最后调容量：候选切片数 / ef / batch / 并发 / deadline", 48, 559, 514, 11, 52)
    d.text("ef约束看实际切片limit；chunk_chars是字符，不是token。", 48, 621, 10.5, INK, True)
    d.text("现有YAML不接受新字段；待实现参数在填写表中单独标明。", 48, 644, 10, MUTED)
    d.box(607, 515, 550, 155, PALE_AMBER, AMBER)
    d.text("分层差异怎样测出来", 623, 527, 16, AMBER, True)
    d.para("固定业务时钟，让新闻跨边界；逐条回放更正、撤稿、回热。\n耗时用真实单调时钟；记录三层数量、变更滞后、各层p95。\n三集合共用资源不保证冷热延时差；稳定后再改变资源/索引。", 623, 559, 516, 11, 52)
    d.text("并发全查：总耗时受最慢层影响，分层本身不保证更快。", 623, 621, 10.5, INK, True)
    d.text("注入sleep/网络延时用于故障与超时验证，不能当真实性能。", 623, 644, 10, MUTED)

    d.text("每一关有证据，才进入下一关", 32, 690, 16, INK, True)
    stages = [
        (32, "G1 合同与单篇", "真实模型 + 隔离集合"),
        (223, "G2 入库恢复", "落库 / 派发 / 重启"),
        (414, "G3 查询正确", "版本 / 租户 / 去重"),
        (605, "G4 生命周期", "更新 / 撤稿 / 迁移"),
        (796, "G5 故障回放", "重复 / 乱序 / 断连"),
        (987, "G6 混合负载", "延时 / 新鲜度 / 积压"),
    ]
    for x, title, body in stages:
        d.box(x, 724, 170, 57, white, BLUE)
        d.text(title, x+10, 732, 12, BLUE, True)
        d.text(body, x+10, 756, 10, MUTED)
    d.text("交付每轮参数快照 + 事件序列 + 查询Trace + 失败明细；通过阈值由你约定，逻辑错误、撤稿残留和跨租户返回必须为零。", 32, 795, 10.5, INK)
    d.text("本图为实施指导，不表示待开发能力已接入。参考：Milvus HNSW / Consistency；Temporal Retry Policies。", 32, 817, 9, MUTED)
    d.c.linkURL("https://milvus.io/docs/v2.5.x/hnsw.md", (647, 7, 760, 22), relative=0)
    d.c.linkURL("https://milvus.io/docs/v2.5.x/consistency.md", (765, 7, 881, 22), relative=0)
    d.c.linkURL("https://docs.temporal.io/encyclopedia/retry-policies", (885, 7, 1110, 22), relative=0)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    target = ROOT / "output/tiered-recall-implementation-2026-10-15/parameters.worksheet.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(worksheet(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pdf = ROOT / "output/pdf/NewsAgent_分层召回实现与测试路线_2026-10-15.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    render(pdf)
    print(json.dumps({"worksheet": str(target), "pdf": str(pdf)}, ensure_ascii=False))
