"""timeline-v5 扩充版确定性数仓：主题、来源与场景比冻结的 v4 更丰富，独立于 v4 数据身份。"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json

from app.sql_assistant.scaled_profiles import (
    TABLES, TENANTS, ScaledTableHasher, _metrics, _normal,
    iter_scaled_rows, validate_news_count,
)
from app.sql_assistant.timeline_profiles import START, END, HOURS, timeline_scenarios


TIMELINE_V5_PROFILE = "timeline-v5"
TIMELINE_V5_VERSION = "news-timeline-20260910-20261009-v2"
# 每栏目 24 个主题，共 96 个；与 v4 的 48 个主题集不同，标题、来源和波动也独立演进。
TOPICS_V5 = {
    "科技": ("人工智能工具应用", "大模型评测进展", "国产芯片研发", "先进制程产线观察",
             "机器人场景测试", "人形机器人零部件", "卫星通信试验", "低轨星座组网",
             "新能源汽车技术", "智能驾驶路测", "智能手机新品观察", "折叠屏供应链",
             "数据安全研究", "隐私计算应用", "开源软件生态", "量子计算研究",
             "工业数字化", "智能制造产线", "低空飞行技术", "电动航空适航",
             "新能源储能", "固态电池中试", "算力基础设施", "绿色数据中心"),
    "财经": ("国庆消费市场", "假期零售客流", "旅游订单变化", "免税经济观察",
             "制造企业经营", "专精特新企业", "跨境贸易观察", "航运口岸吞吐",
             "物流运价变化", "快递旺季调度", "地方消费补贴", "以旧换新政策",
             "能源价格观察", "成品油调价窗口", "中小企业融资", "供应链金融试点",
             "零售业经营", "会员店扩张", "汽车市场需求", "新能源车渗透率",
             "绿色产业投资", "碳市场交易", "就业市场服务", "灵活用工观察"),
    "体育": ("足球联赛赛程", "青训梯队建设", "篮球联赛训练", "外援注册观察",
             "城市马拉松筹备", "赛道志愿服务", "网球赛事观察", "青少年网球培养",
             "青年游泳赛事", "场馆泳池改造", "校园运动活动", "体教融合试点",
             "全民健身服务", "社区健身器材", "冰雪运动备赛", "室内雪场运营",
             "自行车赛事路线", "骑行安全宣传", "排球赛事训练", "沙滩排球巡回赛",
             "体育场馆开放", "智慧场馆系统", "运动健康研究", "运动损伤康复"),
    "社会": ("假期出行服务", "高速充电保障", "城市交通疏导", "地铁客流调度",
             "社区养老服务", "老年助餐点覆盖", "公共安全演练", "应急避难场所",
             "极端天气应对", "内涝排水改造", "学校食堂管理", "校园周边交通",
             "公共阅读活动", "城市书房运营", "医疗预约服务", "分级诊疗推进",
             "无障碍设施建设", "盲道整治观察", "社区便民改造", "老旧小区加装电梯",
             "生态环境监测", "垃圾分类督导", "公共文化展览", "非遗展演活动"),
}
STAGES = ("启动期", "进展期", "观察期", "复盘期")
SOURCES = ("一线观察室", "产业观察组", "城市记录站", "数据透视组",
           "现场连线组", "深度调研组", "区域扫描组", "趋势追踪组")
TOPICS_PER_CATEGORY = 24


def timeline_v5_scenarios():
    """在 v4 的 13 个场景之外追加 4 个 v5 专属对比场景，共 17 个。"""
    cases = [deepcopy(case) for case in timeline_scenarios()]
    for identity, label, previous_day, current_day, previous_hour, current_hour, signal in (
        ("weekday-weekend", "工作日与周末同小时", 7, 9, 19, 19, "weekend_effect"),
        ("intra-day-contrast", "同日内早晚窗口", 29, 29, 10, 19, "intra_day_contrast"),
        ("video-morning-surge", "视频早高峰跨周", 22, 29, 8, 8, "video_morning_surge"),
        ("midday-zero-baseline", "午间小样本与零基线", 29, 29, 11, 12, "known_zero_baseline"),
    ):
        cases.append(dict(id=identity, label=label,
            description="比较两个独立窗口；仅比较共同新闻，不推断中间连续走势或跨小时去重人数。",
            question="查询点击量最高的前100条新闻", row_limit=100,
            reference_start=(START + timedelta(days=previous_day, hours=previous_hour)).isoformat(),
            current_start=(START + timedelta(days=current_day, hours=current_hour)).isoformat(),
            current_end=(START + timedelta(days=current_day, hours=current_hour + 1)).isoformat(),
            expected_signals=[signal, "matched_cohort_only"]))
    return cases


def iter_timeline_v5_rows(table, news_per_tenant=1200):
    count = validate_news_count(news_per_tenant)
    if table not in TABLES:
        raise ValueError("unsupported timeline table")
    if table == "dim_news":
        for row in iter_scaled_rows(table, count):
            index = int(row["news_id"].rsplit("-", 1)[1])
            # 每组12篇（一个完整栏目循环）共享主题与波动，组号对24取模覆盖全部主题。
            group = (index - 1) // 12
            category = row["category"]
            topic = TOPICS_V5[category][group % TOPICS_PER_CATEGORY]
            stage = STAGES[(index // 6) % len(STAGES)]
            row["publish_time"] = (START - timedelta(hours=12) - timedelta(hours=index % 48)
                                   + timedelta(seconds=index))
            row["title"] = f"[合成热点样本v5] {category}·{topic}（{stage}）观察记录{index:06d}"
            row["source"] = f"合成{SOURCES[(index - 1) % len(SOURCES)]}"
            yield row
        return
    for tenant_index, tenant in enumerate(TENANTS):
        isolation = 1 if tenant_index == 0 else 8
        for index in range(1, count + 1):
            group = (index - 1) // 12
            for offset in range(HOURS):
                day, hour = divmod(offset, 24)
                # 主题级每日轮换波动叠加日趋势、周末效应；突发小时与 v4 不同。
                wave = ((group + day) % 5) * 6
                factor = 80 + day * 2 + (20 if (START + timedelta(days=day)).weekday() >= 5 else 0) + wave
                hot = (index + day * 7) % 31 < 3
                burst = 3 if hot and hour in {18, 20, 21, 23} else 1
                row = dict(tenant_id=tenant, news_id=f"scale-news-{index:06d}",
                           event_time=START + timedelta(hours=offset))
                if table == "news_metric_hourly":
                    values = _metrics(index, hour, count)
                    row.update({key: value * factor // 100 * burst * isolation for key, value in values.items()})
                    row["total_duration_seconds"] = Decimal(row["total_duration_seconds"])
                else:
                    normal = _normal(index)
                    row.update({f"baseline_{key}": Decimal(0 if hour in {12, 13} else normal[key] * factor // 100 * isolation)
                                for key in ("impressions", "clicks", "effective_consumptions", "interactions")})
                yield row


@lru_cache(maxsize=3)
def _manifest(count):
    hashes = {}
    for table in TABLES:
        hasher = ScaledTableHasher(table)
        for row in iter_timeline_v5_rows(table, count):
            hasher.update(row)
        hashes[table] = dict(sha256=hasher.digest(), rows=hasher.count)
    identity = dict(dataset_profile=TIMELINE_V5_PROFILE, dataset_version=TIMELINE_V5_VERSION,
                    news_per_tenant=count, hours_per_news=HOURS, total_tenants=2,
                    window_start=START.isoformat(), window_end=END.isoformat(),
                    hash_protocol="news-scaled-fixture-v1", tables=hashes,
                    enterprise_scenarios=timeline_v5_scenarios())
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return {**identity, "dataset_sha256": sha256(encoded).hexdigest()}


def timeline_v5_manifest(news_per_tenant=1200):
    return deepcopy(_manifest(validate_news_count(news_per_tenant)))
