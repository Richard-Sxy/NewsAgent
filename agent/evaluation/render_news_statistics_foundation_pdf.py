"""Illustrate the upstream counting pipeline and independently count fictional events.

This is a two-page educational PDF generator, not a deployed event pipeline.
No enterprise behavior records, credentials, database or model calls are used.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from render_tencent_warehouse_poster import WarehousePoster, INK, MUTED, GREEN, GREEN_BG, BLUE, BLUE_BG, ORANGE, ORANGE_BG, GRAY, RED, W, H
from render_tencent_warehouse_architecture_pdf import DATASET, file_hash, load_evidence

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=ROOT/"output/pdf/NewsAgent_新闻点击与访问统计_底层架构图.pdf"
TZ=timezone(timedelta(hours=8))
DATE="2026-10-14"


@dataclass(frozen=True)
class ToyEvent:
    event_id: str
    event_type: str
    user_key: str
    occurred: str
    received: str
    view_id: str | None = None
    channel: str = "recommend"
    note: str = ""
    tenant_id: str = "T1"
    news_id: str = "N1"

    @property
    def event_time(self):
        return datetime.fromisoformat(f"{DATE}T{self.occurred}+08:00")

    @property
    def received_time(self):
        return datetime.fromisoformat(f"{DATE}T{self.received}+08:00")

    @property
    def payload_identity(self):
        # Delivery envelopes may change on retry; immutable business payload may not.
        return (self.tenant_id,self.news_id,self.event_type,self.user_key,self.occurred,self.view_id,self.channel)


def toy_events():
    return [
        ToyEvent("e1","impression","A","10:01:00","10:01:01",note="看见推荐卡片"),
        ToyEvent("e2","click","A","10:02:00","10:02:01",note="第一次点击"),
        ToyEvent("e3","detail_view","A","10:02:01","10:02:02","v1",note="详情页成功打开"),
        ToyEvent("e2","click","A","10:02:00","10:03:00",note="同一次点击重试：丢弃"),
        ToyEvent("e4","impression","A","10:04:00","10:04:01",note="再次看见卡片"),
        ToyEvent("e5","click","A","10:04:01","10:04:02",note="真实第二次点击：保留"),
        ToyEvent("e6","detail_view","A","10:04:02","10:04:03","v2",note="第二次详情访问"),
        ToyEvent("e7","impression","B","10:15:00","10:15:01",note="B 看见卡片"),
        ToyEvent("e8","click","B","10:15:01","10:15:02",note="B 点击"),
        ToyEvent("e9","detail_view","B","10:15:02","11:02:00","v3",note="迟到：仍归 10 点小时"),
        ToyEvent("e10","impression","C","10:32:00","10:32:01",note="C 只曝光，没有访问"),
        ToyEvent("e11","detail_view","A","11:01:00","11:01:01","v4","direct","A 直达：没有推荐点击"),
        ToyEvent("e12","detail_view","B","10:15:02","11:03:00","v3",note="新事件 ID，重复 v3：丢弃"),
    ]


def count_toy(events, *, received_before=None):
    """Count one fixed T1/N1 teaching scope, never a production multi-tenant metric source."""
    if any(event.tenant_id!="T1" or event.news_id!="N1" for event in events):
        raise ValueError("this teaching example is limited to T1/N1")
    seen, views, accepted, rejected = {}, {}, [], []
    for event in sorted(events,key=lambda e:e.received_time):
        if received_before is not None and event.received_time>=received_before:
            continue
        event_key=(event.tenant_id,event.event_id)
        if event_key in seen:
            if seen[event_key] != event.payload_identity:
                raise ValueError("conflicting payload for the same event ID")
            rejected.append((event,"event_retry"))
            continue
        seen[event_key]=event.payload_identity
        if event.event_type=="detail_view":
            if not event.view_id:
                raise ValueError("detail_view needs a stable view_id")
            visit_key=(event.tenant_id,event.news_id,event.view_id)
            visit_identity=(event.user_key,event.occurred,event.channel)
            if visit_key in views:
                if views[visit_key] != visit_identity:
                    raise ValueError("conflicting payload for the same view ID")
                rejected.append((event,"view_retry"))
                continue
            views[visit_key]=visit_identity
        accepted.append(event)
    buckets=defaultdict(list)
    for event in accepted:
        buckets[event.event_time.replace(minute=0,second=0,microsecond=0)].append(event)
    def summary(rows):
        exposures=[e for e in rows if e.event_type=="impression" and e.channel=="recommend"]
        clicks=[e for e in rows if e.event_type=="click" and e.channel=="recommend"]
        visits=[e for e in rows if e.event_type=="detail_view"]
        return {"impressions":len(exposures),"clicks":len(clicks),"view_pv":len(visits),
                "view_uv":len({e.user_key for e in visits}),"click_uv":len({e.user_key for e in clicks}),
                "exposure_uv":len({e.user_key for e in exposures}),"all_behavior_uv":len({e.user_key for e in rows}),
                "visitors":sorted({e.user_key for e in visits}),
                "ctr":Decimal(len(clicks))/len(exposures) if exposures else None}
    return {"hourly":{hour.strftime("%H:00"):summary(rows) for hour,rows in sorted(buckets.items())},
            "total":summary(accepted),"rejected":rejected,"accepted":accepted}


def verify_example():
    events=toy_events()
    result=count_toy(events)
    first,second,total=result["hourly"]["10:00"],result["hourly"]["11:00"],result["total"]
    assert len(events)==13 and len(result["accepted"])==11
    assert [(e.event_id,why) for e,why in result["rejected"]]==[("e2","event_retry"),("e12","view_retry")]
    assert (first["impressions"],first["clicks"],first["view_pv"],first["view_uv"],first["all_behavior_uv"])==(4,3,3,2,3)
    assert first["ctr"]==Decimal("0.75")
    assert (second["impressions"],second["clicks"],second["view_pv"],second["view_uv"],second["ctr"])==(0,0,1,1,None)
    assert (total["view_pv"],total["view_uv"])==(4,2)
    before=count_toy(events,received_before=datetime(2026,10,14,11,0,tzinfo=TZ))["hourly"]["10:00"]
    assert (before["view_pv"],before["view_uv"])==(2,1)
    # A replay of the whole batch cannot inflate the aggregates.
    replay=count_toy(events+events)
    assert replay["hourly"]==result["hourly"] and replay["total"]==result["total"]
    for conflict in ([events[1],replace(events[1],user_key="other")],
                     [events[2],replace(events[2],event_id="other",user_key="other")]):
        try:
            count_toy(conflict)
        except ValueError:
            pass
        else:
            raise AssertionError("conflicting identities must be rejected")
    # The diagram's existing-code annotations are verified against current files.
    metrics=(ROOT/"agent/app/analytics/metrics.py").read_text(encoding="utf-8")
    entities=(ROOT/"agent/app/analytics/entities.py").read_text(encoding="utf-8")
    assert 'group["user_ids"].add(record.user_id)' in metrics
    assert 'ctr = Decimal("0")' in metrics
    assert 'DETAIL_VIEW' not in entities
    return events,result,before


class StatisticsFoundationGuide(WarehousePoster):
    def __init__(self,output,evidence,font,bold_font,example):
        super().__init__(output,evidence,font,bold_font)
        self.c.setTitle("NewsAgent 新闻点击与访问统计 - 底层架构图")
        self.c.setSubject("事件埋点、去重、时间分桶、状态、聚合快照、PV/UV/CTR与本地实现边界")
        self.example=example

    def start(self,index,title,subtitle):
        self.c.setFillColor(HexColor("#FCFDFE"))
        self.c.rect(0,0,W,H,fill=1,stroke=0)
        self.c.bookmarkPage(f"statistics-{index}")
        self.c.addOutlineEntry(title,f"statistics-{index}",level=0)
        self.text("NEWSAGENT / COUNTING FROM THE BOTTOM UP",34,25,10,GREEN,True)
        self.text(title,34,52,29,INK,True)
        self.text(subtitle,34,96,12.5,MUTED)

    def end(self,index):
        self.line(34,H-32,W-34,H-32,GRAY,.8)
        self.text("新闻业务教学图 | 事件样例均为虚构 | 企业原始用户明细留在上游 | 2026-10-14",34,H-23,9,MUTED)
        self.text(f"0{index} / 02",W-72,H-23,9,MUTED)
        self.c.showPage()

    def mini_phone(self,x,y):
        self.rect(x,y,34,65,INK,radius=7)
        self.rect(x+4,y+8,26,45,white,radius=3)
        for i in range(3):
            self.line(x+8,y+18+i*10,x+26,y+18+i*10,BLUE,3)
        self.circle(x+17,y+59,2,GRAY)

    def icon(self,kind,x,y):
        if kind=="phone":
            self.mini_phone(x+28,y)
            self.circle(x+72,y+42,7,HexColor("#EBC9AE"))
            self.line(x+74,y+37,x+84,y+30,ORANGE,1.3)
        elif kind=="queue":
            for i in range(3):
                self.rect(x+9+i*8,y+7+i*15,79,19,white,BLUE,radius=4)
                self.text(f"p{i}: event / offset",x+15+i*8,y+13+i*15,7.8,BLUE)
        elif kind=="filter":
            self.poly([(x+7,y+5),(x+88,y+5),(x+60,y+33),(x+60,y+54),(x+38,y+64),(x+38,y+33)],HexColor("#ACD7C6"),GREEN)
            self.text("event_id",x+46,y+10,9,GREEN,True,center=True)
            self.text("去重",x+47,y+38,11,GREEN,True,center=True)
        elif kind=="state":
            self.gear(x+36,y+31,26,GREEN)
            self.gear(x+67,y+44,18,HexColor("#85BDAA"))
            self.text("SUM / DISTINCT",x+48,y+65,8.5,GREEN,center=True)
        elif kind=="db":
            self.database(x+16,y+1,63,65)
        elif kind=="api":
            self.rect(x+9,y+5,80,55,white,BLUE,radius=6)
            self.text("PV    UV",x+49,y+18,13,BLUE,True,center=True)
            self.text("CTR / 版本",x+49,y+41,10,BLUE,center=True)
        elif kind=="monitor":
            self.rect(x+9,y+4,80,54,INK,radius=5)
            self.rect(x+14,y+10,70,39,white,radius=2)
            for i,h in enumerate((10,19,25)):
                self.rect(x+23+i*17,y+44-h,9,h,BLUE,radius=1)
            self.line(x+49,y+59,x+49,y+67,INK,4)
            self.line(x+32,y+68,x+65,y+68,INK,3)

    def foundations(self):
        self.start(1,"点击量、访问量：数仓到底怎样算？","核心：记录每次行为 → 消除重复上报 → 按新闻和发生时间分组 → 累加次数、合并去重人群。")
        self.badge("企业上游目标链路：工程示意，技术栈待确认",34,128,BLUE,BLUE_BG)
        self.badge("本地 v7：直接生成聚合样本",396,128,GREEN,GREEN_BG)
        self.badge("访问 PV / UV：建议新增口径",665,128,ORANGE,ORANGE_BG)
        self.text("工程选项示意：消息流（如 Kafka）、状态计算（如 Flink）、列式 OLAP；它们不属于本地 v7 运行依赖。",34,157,9.5,MUTED)
        self.rect(32,173,962,231,BLUE_BG,HexColor("#CBDCF0"),radius=15)
        self.rect(1016,173,142,231,GREEN_BG,radius=15)
        self.text("上游负责采集、明细、计算与数仓；NewsAgent 只读聚合快照",48,184,12.5,BLUE,True)
        stages=(
            ("客户端埋点","phone","看见 / 点击 / 详情成功\n带事件 ID 和原发生时间","发生端"),
            ("接收与缓冲","queue","校验格式、可靠保存\n消息重试与按分区回放","接入 / ODS"),
            ("清洗与去重","filter","event_id 去投递重试\nview_id 去重复访问上报","清洗 / DWD"),
            ("分桶与累计","state","按租户、新闻、渠道\n和事件时间维护状态","计算 + 状态"),
            ("聚合快照","db","5 分钟 / 小时 / 日\n修订 / 质量 / 版本","汇总 / DWS"),
            ("指标服务","api","先取最新修订\n再验完整性与质量\n不回退旧版","服务 / ADS"),
            ("热点运营","monitor","Python 排行与校验\nAgent 解释与表达","聚合消费端"),
        )
        for i,(title,kind,desc,layer) in enumerate(stages):
            x=32+i*164
            self.rect(x+7,211,128,167,white,radius=10)
            self.text(f"{i+1}  {title}",x+71,223,12.5,BLUE if i<6 else GREEN,True,center=True)
            self.icon(kind,x+23,250)
            self.para(desc,x+16,329,110,10.1,maxh=45)
            self.text(layer,x+71,387,9.5,MUTED,center=True)
            if i<6:
                self.route([(x+141,284),(x+160,284)],BLUE,width=1.8)
        self.text("窗口计算器：内存里到底存什么？",34,422,17,INK,True)
        self.rect(32,457,255,210,HexColor("#F2F5F8"),radius=13)
        self.text("一条行为包（教学示例）",47,470,13,INK,True)
        packet=("event_id: e2","event_type: click","news_id: N1 / user_key: A","event_time: 10:02:00","received_at: 10:02:01","channel: recommend","tenant: T1（可信入口映射）")
        for i,line in enumerate(packet):
            self.text(line,48,499+i*21,11,INK)

        self.route([(291,560),(310,560)],GREEN,width=1.8)
        self.rect(317,457,545,210,GREEN_BG,HexColor("#C4DED2"),radius=13)
        self.text("key = 租户 + 新闻 + 渠道 + 小时桶",333,470,13,GREEN,True)
        states=(
            ("次数计数器","impression += 1\nclick += 1\n成功访问按 view_id 计数"),
            ("用户集合 / 去重状态","访问用户：{A, B}\n访问 UV = 集合大小\n跨桶需合并集合 / 状态"),
            ("防重 + 恢复状态","已见事件：{e1, e2, ...}\n重试丢弃，真二次点击保留\n检查点：状态 + 消费进度"),
        )
        for i,(title,body) in enumerate(states):
            x=331+i*172
            self.rect(x,506,161,117,white,radius=8)
            self.text(title,x+80,519,11.2,GREEN,True,center=True)
            self.para(body,x+10,545,141,10.4,maxh=62)
        self.text("精确集合 / Bitmap 或近似 HLL；近似结果需记录方法与误差口径。",333,640,10,MUTED)
        self.route([(866,560),(883,560)],GREEN,width=1.8)
        self.rect(891,457,267,210,HexColor("#F2F5F8"),radius=13)
        self.text("写出一行快照（目标合同）",906,470,13,INK,True)
        lines=("T1 / N1 / recommend","窗口 [10:00, 11:00)","revision: 2 / policy: v1","曝光 4 / 点击 3 / 访问 PV 3","访问 UV 2 / 推荐 CTR 75%")
        for i,line in enumerate(lines):
            self.text(line,908,502+i*24,11.1,INK)
        self.text("detail_view 与访问字段尚未接入 v7",908,640,9.8,ORANGE)

        self.rect(32,685,548,89,BLUE_BG,radius=12)
        self.text("为什么查询不用每次扫描所有点击？",47,698,13,BLUE,True)
        self.para("预聚合把事件变成少量窗口行；按时间分区、新闻与时间键排序 / 索引裁剪扫描。企业可选列存 OLAP；本地模拟使用 PostgreSQL。",47,725,518,10.5,maxh=35)
        self.rect(596,685,562,89,ORANGE_BG,radius=12)
        self.text("重放、迟到和补数怎样不翻倍？",611,698,13,ORANGE,True)
        self.para("持久去重 + 检查点恢复 + 幂等写出。全量 rev2 替代旧快照，不与 rev1 相加；迟到按原事件时间修订。消息 ACK 本身不保证端到端恰好一次。",611,725,532,10.5,maxh=35)
        self.end(1)

    def ledger(self):
        events,result,before=self.example
        self.start(2,"13 条上报，怎样算出 PV、UV 和 CTR？","T1 / N1；A、B、C 和所有事件 ID 都是虚构。曝光与归因点击属于推荐场景，详情访问也包括直达。")
        self.badge("事件去重：同一 event_id 只算一次",34,129,BLUE,BLUE_BG)
        self.badge("业务去重：同一 view_id 只算一次访问",337,129,GREEN,GREEN_BG)
        self.badge("用户去重：真实多次访问，PV 增加而 UV 不重复",667,129,ORANGE,ORANGE_BG)

        self.text("原始上报 → 清洗事实（玩具账本）",34,175,15,INK,True)
        x0,y0=32,202
        widths=(57,75,40,83,83,56,244)
        headers=("事件 ID","行为","人","发生时间","收到时间","view_id","处理说明")
        tw=sum(widths)
        self.rect(x0,y0,tw,28,INK,radius=4)
        xx=x0
        for header,width in zip(headers,widths):
            self.text(header,xx+7,y0+8,10.1,white,True)
            xx+=width
        labels={"impression":"曝光","click":"点击","detail_view":"详情成功"}
        for index,event in enumerate(events):
            y=y0+28+index*24
            warning=index in (3,9,12)
            fill=ORANGE_BG if warning else white if index%2 else HexColor("#F1F5F9")
            self.rect(x0,y,tw,24,fill,radius=0)
            cells=(event.event_id,labels[event.event_type],event.user_key,event.occurred,event.received,event.view_id or "-",event.note)
            xx=x0
            for cell,width in zip(cells,widths):
                self.text(cell,xx+7,y+7,10,ORANGE if warning else INK,width=width-14)
                xx+=width
        self.text("13 条上报 → 去掉 e2 重试、重复 v3 → 11 条可计量事实",34,562,12,GREEN,True)
        self.para("说明：view_id 的稳定边界由真实埋点合同决定；重复访问上报即使换了 event_id，也不应该创建新访问。真实二次打开必须用新的 view_id。",34,590,638,10.5,MUTED,maxh=34)

        self.rect(755,202,403,132,GREEN_BG,radius=13)
        first=result["hourly"]["10:00"]
        self.text("10 点小时 · 最终修订",773,216,17,GREEN,True)
        self.text(f"曝光 {first['impressions']} / 点击 {first['clicks']} / 访问 PV {first['view_pv']}",773,254,19,INK,True)
        self.text(f"访问 UV = {first['view_uv']} 人：A、B",773,286,17,GREEN,True)
        self.text("推荐 CTR = 3 / 4 = 75%",773,311,11.5,MUTED)
        self.rect(755,350,403,132,BLUE_BG,radius=13)
        second=result["hourly"]["11:00"]
        self.text("11 点小时 · A 直达访问",773,364,17,BLUE,True)
        self.text(f"点击 {second['clicks']} / 访问 PV {second['view_pv']} / 访问 UV {second['view_uv']}",773,402,18,INK,True)
        self.para("没有推荐曝光，CTR 不可定义；访问量与点击量需要独立埋点、独立统计。",773,435,368,11,MUTED,maxh=34)
        self.rect(755,499,403,128,ORANGE_BG,radius=13)
        self.text("两个小时合并",773,513,17,ORANGE,True)
        self.text(f"访问 PV = {result['total']['view_pv']}；访问 UV = {result['total']['view_uv']}",773,549,19,INK,True)
        self.para("用户集合 {A,B} ∪ {A} = {A,B}。\n2 + 1 不是跨小时 UV；应合并状态再计数。",773,584,368,11,MUTED,maxh=35)

        self.rect(32,645,715,124,BLUE_BG,radius=12)
        self.text("e9 到得晚，但属于原来的 10 点小时",47,659,14,BLUE,True)
        self.para(f"e9 在 10:15:02 发生，11:02 才收到。未收到时，10 点访问 PV={before['view_pv']}、UV={before['view_uv']}；在批准迟到策略内修订后变为 3、2。超过策略预算应进补数 / 质量台账。",47,689,685,10.5,maxh=34)
        self.para("水位反映事件时间进度；要结合迟到策略、源端覆盖和质量检查。阅读时长应先归并会话和心跳，累计心跳先差分 / 取合法最大值，再判断有效消费。",47,734,685,9.9,MUTED,maxh=30)
        self.rect(763,645,395,124,HexColor("#FCEEF0"),radius=12)
        self.text("你的项目当前还缺什么？",779,659,14,RED,True)
        self.para("v7 没有 detail_view / 独立访问 PV、UV。\n旧内存计算器 unique_users 含只曝光的人；\nv7 则是合成点击人群，二者还需统一口径。\n零曝光 CTR：v7 为 NULL，旧计算器为 0。",779,690,363,10.5,maxh=65)
        self.source_links()
        self.end(2)

    def source_links(self):
        # Source anchors are linked without turning the poster into a documentation dump.
        links=(
            ("[1] Flink：事件时间 / 水位","https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/event-time/generating_watermarks/"),
            ("[2] Flink：检查点","https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/"),
            ("[3] ClickHouse：精确 UV","https://clickhouse.com/docs/reference/functions/aggregate-functions/uniqExact"),
        )
        for index,(label,url) in enumerate(links):
            x=34+index*244
            self.text(label,x,784,9.3,BLUE)
            self.c.linkURL(url,(x,H-798,x+230,H-782),relative=0,thickness=0)
        self.text("可选工程依据，不代表腾讯新闻内部实现",775,784,9.3,MUTED)

    def render(self):
        self.foundations()
        self.ledger()
        self.c.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",type=Path,default=DATASET)
    parser.add_argument("--output",type=Path,default=OUTPUT)
    parser.add_argument("--font",type=Path,default=Path("/System/Library/Fonts/STHeiti Light.ttc"))
    parser.add_argument("--bold-font",type=Path,default=Path("/System/Library/Fonts/STHeiti Medium.ttc"))
    args=parser.parse_args()
    evidence=load_evidence(args.dataset)
    example=verify_example()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    StatisticsFoundationGuide(args.output,evidence,args.font,args.bold_font,example).render()
    print({"output":str(args.output.resolve()),"pages":2,"sha256":file_hash(args.output),
           "fictional_events":len(example[0]),"accepted_facts":len(example[1]['accepted'])})


if __name__=="__main__":
    main()
