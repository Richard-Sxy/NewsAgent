"""One-page Chinese diagram from the implemented audience API example."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent/evaluation'))
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, white, HexColor,
)


def render():
    evidence = json.loads((ROOT/'output/audience-interface-20261016/api-example.json').read_text())
    catalog, preview = evidence['catalog'], evidence['preview']
    assert preview['snapshot']['source_mode'] == 'synthetic' and preview['dispatch_enabled'] is False
    assert len(catalog['topics']) == 25 and preview['preview_only'] is True
    out = ROOT/'output/pdf/newsagent-enterprise-audience-integration-2026-10-16.pdf'
    d = Diagram(out)
    d.c.setTitle('腾讯新闻主题标签与受众接口落地')
    d.c.setSubject('企业画像与推送边界、两级目录、聚合受众API、运营面板及验证范围')
    d.c.setFillColor(HexColor('#FAFCFD'))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text('NEWSAGENT / ENTERPRISE INTEREST AUDIENCE / 2026-10-16', 32, 23, 10, GREEN, True)
    d.text('主题标签接入，让热点分析服务新闻运营', 32, 46, 28, INK, True)
    d.text('暂不需要补全标签表：先用已确认主题与可配置草案，正式对接时替换为企业批准目录。', 32, 92, 13.4, MUTED)
    d.box(32, 121, 1125, 31, PALE_AMBER)
    d.text('本次已实现：主题目录 → 标签组合 → 去重聚合受众预估 → 运营人工建议；真实画像算法与最终投递由企业承担。', 45, 130, 11.3, AMBER)

    d.text('A  企业业务链路与本次接口边界', 32, 173, 17, GREEN, True)
    stages = [
        (32, '用户访问新闻', '内容与主题浏览\n企业侧数据与授权', False),
        (261, '企业兴趣画像', '主题 + 细分标签\n算法 / 更新 / 资格', False),
        (490, '聚合受众接口', '按标签规则统一去重\n人数 + 版本 + 水位', False),
        (719, 'NewsAgent运营', '热点详情按需预估\n校验后供人工判断', False),
        (948, '企业审批与投递', '实际通知与频控\n送达 / 展示 / 点击', True),
    ]
    for x, title, body, dashed in stages:
        fill = PALE_GREEN if x in (490, 719) else white
        color = GREEN if x in (490, 719) else AMBER if dashed else MUTED
        d.box(x, 208, 207, 103, fill, color, dashed=dashed)
        d.text(title, x+14, 220, 15.5, color, True)
        d.para(body, x+14, 252, 178, 11.2, 38)
        if x < 948:
            d.arrow(x+207, 261, x+223, 261, GREEN if x < 719 else AMBER, dashed=x == 719)
    d.text('企业个人标签与名单留在企业侧；本项目不接收设备Token或原始浏览日志。虚线为后续真实审批/投递联调。', 32, 326, 10.8, MUTED)

    d.box(32, 357, 352, 218, white, BLUE)
    d.text('B  两级目录：5个主题 + 20个细分', 47, 371, 15.5, BLUE, True)
    entries = [('科技', '自然、航天、数码、科学研究'), ('财经', '宏观、产业、金融市场、公司'),
               ('AI', '大模型、应用、算力、研究'), ('军事', '国防、装备、演训、历史'),
               ('教育', '基础教育、高考、高等、职业')]
    for i, (parent, children) in enumerate(entries):
        d.text(parent, 48, 409+i*25, 12, INK, True)
        d.text('→ '+children, 105, 409+i*25, 10.8, INK)
    d.para('一级主题与“科技-自然”来自用户；其余细分为草案。\n稳定ID、单父节点、启停、目录版本均可配置。', 47, 541, 322, 9.6, 30, MUTED)

    d.box(404, 357, 370, 218, white, GREEN)
    d.text('C  实际接口与校验链', 419, 371, 15.5, GREEN, True)
    d.text('GET /api/v1/audience/topics', 419, 410, 11.5, GREEN, True)
    d.text('POST /api/v1/audience/runs/{run}/news/{news}/preview', 419, 437, 9.2, GREEN, True)
    d.para('网关权限 → 本租户已完成的新闻分析\n→ 标签包含/排除、任一/全部匹配\n→ 企业HTTP或本地同Port → 聚合快照\n→ 身份/指纹/目录/窗口/水位/时效校验', 419, 463, 339, 11, 68)
    d.para('预览固定需人工决策，dispatch_enabled=false。\n前端选择变化或新闻变化即作废旧值，过期需重算。', 419, 541, 339, 9.6, 30, MUTED)

    d.box(794, 357, 363, 218, PALE_AMBER, AMBER)
    d.text('D  已验证的合成API样例', 809, 371, 15.5, AMBER, True)
    snapshot = preview['snapshot']
    d.text('科技 或 科技-自然 → 同一批去重受众', 809, 409, 11.2, INK, True)
    d.text(str(snapshot['matched_users']), 812, 439, 35, GREEN, True)
    d.text(str(snapshot['eligible_users']), 990, 439, 35, GREEN, True)
    d.text('匹配人数', 812, 485, 10.6, MUTED)
    d.text('符合通知资格', 990, 485, 10.6, MUTED)
    d.para('父子标签人数不相加；这是合成快照。\n真实API与Service已串通，热点仓储为测试替身。\n未调用企业服务，未创建发送或审批任务。', 809, 518, 330, 10, 45, AMBER)

    d.box(32, 596, 1125, 99, PALE_GREEN, GREEN)
    d.text('E  实现与验证', 47, 608, 16, GREEN, True)
    d.text('后端 168 项通过', 47, 643, 17, GREEN, True)
    d.text('前端受众 15 项 + 相邻 57 项通过', 328, 643, 14, GREEN, True)
    d.text('类型检查 / 构建 / 改动文件lint通过', 785, 643, 12.7, GREEN, True)
    d.text('核心：TopicCatalog → AudiencePreviewService → HttpAudienceProfileClient / LocalAudienceProfileClient → AudienceSnapshot', 47, 674, 9.6, MUTED)

    d.box(32, 714, 1125, 70, PALE_BLUE, BLUE)
    d.text('配置与企业联调', 47, 724, 14.5, BLUE, True)
    d.para('默认关闭；本地追加 docker-compose.audience.local.yml。企业需 approved目录、HTTPS endpoint、密钥注入及画像/资格口径。\n依赖：FastAPI、httpx、Pydantic、PostgreSQL；业务复用现有热点链。待办：真实标签ID/接口合同、内部身份、SLA、审批与投递。', 47, 751, 1090, 10.2, 31)
    d.text('公开技术参考：腾讯云标签组合/推送接口；FCM投递分阶段；APNs有效期。参考不代表腾讯新闻内部采用相同实现。', 32, 803, 9.3, MUTED)
    d.c.linkURL('https://cloud.tencent.cn/document/product/548/39064', (32, 13, 1150, 34), relative=0)
    d.c.showPage()
    d.c.save()
    print(out)


if __name__ == '__main__':
    render()
