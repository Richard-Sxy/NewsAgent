"""Curated fictional newsroom stories and reproducible v2 authoring catalog.

All entities, developments, constraints and judgements are invented fixtures.
The six-stage path is a coverage design, not a claim about real news frequency.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


# Each board has three distinct storylines, each with six substantive updates.
# Every split therefore contains each board, but holds out complete events.
STORIES = {
    "domestic": ("国内", "政策适用范围、办事入口、权威更正", [
        ("澄岚市公共服务补助试点", ["网传所有居民都可领取补助，试点机关尚未公布条件。", "虚构试点公告确认仅面向登记的社区服务机构，个人不能直接申领。", "试点增加材料补交渠道，首次明确补交截止日和受理窗口。", "多篇转载改标题，受理范围、材料和期限均未改变。", "本批申报已结束，旧入口关闭，回顾文章仍有较高点击量。", "公告更正服务机构数量的单位，原文把机构数误写成人数，申领范围未扩大。"]),
        ("青汀县公共图书服务改革", ["社交账号称图书证即将全面收费，没有可核验文件。", "虚构县馆确认基础借阅仍免费，付费仅是自愿配送项目。", "新增偏远社区取书点，公布服务区域与预约规则。", "另一账号引用同一通知，没有新增服务区。", "配送试用预约结束，读者仍可使用基础借阅。", "县馆更正取书点开放日期，周末安排与旧稿不同。"]),
        ("望海区办事预约调整", ["传言所有线下窗口停办，服务中心仅说将测试预约。", "虚构服务中心确认只调整两类预约业务，紧急窗口保留。", "公布替代预约通道和无智能手机者的线下协助点。", "旧截图重新传播，窗口安排没有变化。", "测试结束，原预约入口已切换，旧提醒失去行动价值。", "服务中心更正一项业务名称，旧稿误把咨询服务写成证件办理。"]),
    ]),
    "international": ("国际", "多方归因、影响地区、翻译和时差", [
        ("虚构洛汐港航线中断", ["未署名视频称港口全面关闭，运营方尚未确认。", "虚构港口通告确认仅一条客运航线暂停，货运未停。", "运营方公布替代码头和下一轮评估时点。", "旧画面被再次转载，暂停范围没有扩大。", "航线已恢复，旧停航通知仍被转发。", "运营方更正恢复时刻的时区，旧译文早了数小时。"]),
        ("虚构双岸城市文化交流协议", ["匿名帖称协议已生效，双方尚未发布文本。", "两座虚构城市公布签署文本，说明后续仍需执行安排。", "首批交流项目名单和申请限制公布。", "评论文章重复签署消息，没有新增执行项目。", "首批申请截止，旧申请提示不再可行动。", "双方更正交流名额的口径，旧稿把项目数写成人数。"]),
        ("虚构北岚机场转机流程", ["游客帖称全部转机要求取消，机场未证实。", "虚构机场确认只有指定航站楼流程简化，其他流程照旧。", "新增引导图并说明特殊旅客的办理位置。", "不同语言转载同一引导图，没有新手续。", "试运行结束，旧流程图已被新版本替代。", "机场更正办理楼层，译稿把到达层误写成出发层。"]),
    ]),
    "finance": ("财经", "财务口径、公告归因、避免投资收益承诺", [
        ("虚构澜芯企业季度披露", ["论坛猜测企业巨额亏损，没有正式报表。", "虚构企业披露营收增长但净利润下降，不能等同经营全面改善。", "新增分业务数据，说明一次性费用及审计状态。", "研报转述同一报表，未披露新的经营事实。", "旧季报仍高热，新季度信息尚未发布，不宜冒充最新财报。", "企业更正费用单位，旧稿金额放大，真实口径需重算。"]),
        ("虚构衡浦公司债券兑付", ["投资者帖称已发生违约，托管方没有结算记录。", "虚构发行人确认进入约定兑付日，尚不等于投资者到账。", "托管方公布结算完成记录与适用债券批次。", "旧兑付公告再传播，没有新增批次或到账证据。", "本次兑付观察期结束，不把旧消息包装为即时交易信号。", "托管方更正批次编号，部分旧稿指向另一只债券。"]),
        ("虚构沐舟行业价格调查", ["单店报价被说成行业价格全面上涨，样本不完整。", "虚构调查报告公布样本地区和期间，只反映样本变化。", "增补更多地区并披露样本差异，不能直接作全国因果推断。", "多家媒体转引同一调查，没有新增价格记录。", "促销季结束，旧调查不代表当前成交价。", "调查方更正同比与环比标签，方向解释随口径改变。"]),
    ]),
    "technology": ("科技", "测试与正式上线区分、版本兼容、漏洞披露", [
        ("虚构星梭终端系统升级", ["社区截图称所有设备已获升级，厂商未发布支持清单。", "虚构厂商确认小规模测试，仅支持列出的设备版本。", "新增兼容列表与已知问题，说明回退条件。", "测评转载发布会信息，没有独立兼容测试。", "旧测试招募结束，旧报名链接失效。", "厂商更正支持版本号，旧稿把测试版写成稳定版。"]),
        ("虚构浅湾软件安全补丁", ["匿名账号称存在高危漏洞，没有复现或维护方公告。", "虚构维护方确认特定版本受影响，未说明其他版本也有问题。", "补丁与缓解说明发布，影响范围以维护方文档为准。", "旧漏洞截图被重新发布，没有新增受影响版本。", "补丁提醒已覆盖目标群体，重复全站推送会增加疲劳。", "维护方更正组件名称，旧稿把客户端问题误写成服务端问题。"]),
        ("虚构远墨人工智能测试报告", ["宣传帖声称系统解决全部任务，没有测试设置。", "虚构实验室公开任务集和限制，只在限定任务比较。", "新增失败样本和独立复测，区分能力改善与统计波动。", "转载引用同一分数，没有新的复测数据。", "旧测试使用过时版本，不能当最新产品能力。", "实验室更正评测分母，旧稿通过率需重新计算。"]),
    ]),
    "society": ("社会", "未核实指控、隐私保护、公共服务与因果边界", [
        ("虚构安岚社区临时疏散", ["群聊称某楼存在危险，现场主管部门尚未确认。", "虚构现场公告确认一处检查区临时疏散，不代表整个社区有危险。", "新增安置点和返回评估安排，不公开居民个人资料。", "旧现场照片重新传播，没有新的风险区。", "临时检查结束，居民已返回，旧疏散提示失效。", "公告更正楼栋编号，旧文指向了不相关建筑。"]),
        ("虚构清禾公益项目审查", ["匿名帖指控负责人违法，尚无正式调查结论。", "虚构项目方确认资金接受审查，不能写成违法已坐实。", "审查方披露已核对范围及未决事项，相关方回应被记录。", "评论重复同一指控，没有新证据或回应。", "审查阶段结束，旧帖不能伪装成刚发生的指控。", "审查方更正一笔款项分类，旧稿误当作个人支出。"]),
        ("虚构鸣溪宠物救助站搬迁", ["转发帖称救助站永久关闭，站方尚未确认。", "虚构站方确认临时搬迁，已有动物照护未中断。", "公布新地址、预约规则和领养审核流程。", "换标题转载同一搬迁公告，没有新增领养名额。", "搬迁完成，旧地址和临时志愿报名链接失效。", "站方更正访客开放时间，旧稿把照护时段写成开放时段。"]),
    ]),
    "local": ("本地", "地域精准、办事时效、替代路径", [
        ("虚构澄岚南区供水维护", ["居民群称全城停水，供水单位没有正式范围。", "虚构供水单位确认仅南区两条街道维护。", "新增临时取水点和预计复核时点。", "旧维护图再次转载，范围与计划没有变化。", "维护已结束，旧停水提醒仍有点击但不再适用。", "供水单位更正街道名称，旧稿误伤不受影响区域。"]),
        ("虚构白汀公交线路调整", ["截图称所有公交停运，运营方未发布通知。", "虚构运营方确认仅一条线路绕行，其他线路正常。", "新增临时站点和无障碍接驳说明。", "转载旧绕行图，没有新增停靠点。", "绕行结束，旧路线查询链接已更新。", "运营方更正临时站点方向，旧稿把返程站写成去程站。"]),
        ("虚构青涧社区体育馆预约", ["群聊称体育馆永久免费，无正式预约规则。", "虚构场馆确认仅试开放时段免费，需提前预约。", "增加家庭场次并披露可预约名额。", "旧免费宣传再次出现，没有新增名额。", "试开放结束，旧预约入口关闭。", "场馆更正适用年龄，旧稿把儿童场写成全龄场。"]),
    ]),
    "sports": ("体育", "赛前与赛果、官方计分、球迷受众", [
        ("虚构岚港联赛决赛", ["球迷帖称比赛取消，赛事方未发布安排。", "虚构赛事方确认比赛延迟，尚未取消。", "正式公布新开赛时间及原票处理办法。", "旧延迟通知换标题传播，没有新增赛程。", "比赛已结束，旧开赛提醒不再有用。", "赛事方更正统计表的球队简称，旧稿归属错误。"]),
        ("虚构云桥田径赛纪录", ["现场帖称新纪录诞生，成绩尚未经官方审核。", "虚构赛事方公布临时成绩，纪录认证仍待审核。", "官方完成认证并说明规则和适用组别。", "旧冲线视频再次上传，没有新的认证结论。", "赛事窗口结束，不把旧纪录视频包装成直播。", "赛事方更正组别，旧稿把青年组纪录写成全组纪录。"]),
        ("虚构海隅球队主场调整", ["论坛称球队永久迁走，没有球队或场馆公告。", "虚构球队确认一次主场改址，赛季安排未整体改变。", "新增入场交通说明与无障碍座位登记办法。", "多篇转载同一改址消息，没有新主场安排。", "本场已结束，旧入场通知过期。", "球队更正场馆入口，旧稿给出了工作人员入口。"]),
    ]),
    "entertainment": ("娱乐", "传闻与确认、票务时效、隐私与夸张表达", [
        ("虚构汐光影展首映调整", ["匿名帖称首映取消，主办方尚未确认。", "虚构主办方确认首映延期，其他单元正常。", "公布新场次和原票退改安排。", "旧延期海报再次转载，没有新增场次。", "退改期限结束，旧操作提醒失效。", "主办方更正影片单元，旧稿误写另一部作品。"]),
        ("虚构云霁乐团公益演出", ["饭圈帖猜测成员退出，没有乐团声明。", "虚构乐团确认临时替补，仅涉及一场演出。", "公布演出曲目与实名票务办理窗口。", "评论重复猜测，不构成新增成员变动事实。", "演出结束，旧售票入口失效。", "乐团更正嘉宾署名，旧稿把伴奏人员写成主唱。"]),
        ("虚构墨岸纪录片上线", ["剪辑视频声称全平台免费，没有版权方公告。", "虚构版权方确认指定平台限期试看，正片权益另行说明。", "新增无障碍字幕版本与适用地区说明。", "转载同一预告片，没有新增上线平台。", "试看期结束，旧免费标题具有误导性。", "版权方更正试看时长，旧稿把片段写成全片。"]),
    ]),
    "culture": ("文化", "出处与归属、学术不确定性、活动信息", [
        ("虚构青瓷馆专题展", ["自媒体称发现孤品，没有馆方鉴定资料。", "虚构馆方确认举办专题展，展品归属仍按展签说明。", "新增研究说明和开放预约时段。", "旧展品照片再次流传，没有新增鉴定结论。", "展期结束，旧预约链接关闭。", "馆方更正年代范围，旧稿把推测年代写成确定年代。"]),
        ("虚构岚书古籍数字化", ["论坛称全部馆藏已开放下载，没有平台说明。", "虚构图书馆确认首批目录上线，部分内容仅馆内阅览。", "新增检索字段和授权可下载批次。", "转载同一目录，没有新增公开册数。", "测试入口停用，旧下载指引失效。", "图书馆更正册与卷的计量口径，旧稿数量需调整。"]),
        ("虚构溪原遗址研究", ["网帖直接断言遗址用途，研究团队尚无结论。", "虚构团队公布现场记录，说明用途仍为待验证假设。", "新增测年结果及误差范围，不能跳过学术限定。", "评论重复同一假设，没有新测年或出土记录。", "旧发掘开放日结束，不宜继续推旧参观安排。", "团队更正样本编号，旧稿引用了不相关测年结果。"]),
    ]),
    "education": ("教育", "报名条件、未成年人隐私、日期与机构归因", [
        ("虚构澄溪技能考试报名", ["社群称考试取消，组织机构尚未发布通知。", "虚构机构确认仅调整报名时间，考试仍按计划准备。", "公布资格核验和补交材料窗口。", "旧报名通知再次转载，没有新增资格条件。", "报名结束，旧入口不可提交。", "机构更正适用批次，旧稿把补报名写成所有考生报名。"]),
        ("虚构星河学校开放日", ["家长群称开放日等同录取承诺，学校未确认。", "虚构学校说明开放日仅供了解课程，不授予录取资格。", "增加线上场次和无障碍预约说明。", "重复转发同一海报，没有新招生安排。", "开放日已结束，旧预约提示失效。", "学校更正预约年级，旧稿混淆两个学段。"]),
        ("虚构青桥助学项目", ["转发帖称所有学生自动获资助，没有项目规则。", "虚构项目公布申请范围和审核程序，结果尚未确定。", "新增材料补交渠道，公开聚合进度而非学生名单。", "旧规则换标题传播，没有新增额度。", "本批审核结束，旧申请入口关闭。", "项目方更正材料名称，旧稿要求了不必要的私人信息。"]),
    ]),
    "health": ("健康", "研究与诊疗区分、证据等级、服务预约", [
        ("虚构清岚健康中心预约", ["群聊称服务面向全城免费，没有中心公告。", "虚构中心确认只面向登记社区的预约服务，不提供诊疗承诺。", "新增预约时段和无障碍咨询入口。", "旧服务海报再次传播，没有新增时段。", "预约批次结束，旧入口关闭。", "中心更正服务项目名称，旧稿把咨询写成治疗。"]),
        ("虚构远汀健康观察研究", ["短视频把初步观察称为治愈证明，没有研究原文。", "虚构研究团队公开观察性结果，不能证明疗效或因果。", "新增研究限制和同行评议状态，结论仍限于样本。", "转载同一结果，没有新增试验数据。", "旧摘要被脱离研究时期传播，不能当最新诊疗建议。", "团队更正样本分母，旧稿效果比例需重新计算。"]),
        ("虚构海竹设备检查通知", ["匿名帖称所有设备危险，厂商没有确认批次。", "虚构厂商确认指定批次进行检查，其他批次无此公告。", "公布批次核对入口和客服安排，不收集个人健康明细。", "旧检查通告换标题转载，没有新增批次。", "检查批次已处理，旧登记链接失效。", "厂商更正批次编码，旧稿把另一型号列入范围。"]),
    ]),
    "weather": ("天气", "预警来源、区域时效、解除与更新", [
        ("虚构岚浦沿海大风预警", ["群聊称全省停航，气象与交通部门尚未确认。", "虚构预警只覆盖岚浦沿海，并给出有效时段。", "预警更新影响区域，交通方另行公布航线安排。", "旧预警图重新传播，没有新增区域。", "预警已解除，旧高热图不应继续作为当前风险提示。", "气象方更正区域代码，旧稿误把内陆县列入范围。"]),
        ("虚构青潭城区强降雨", ["短视频称城区将全面淹没，没有可信预测依据。", "虚构气象机构发布特定城区降雨预警，影响程度仍需观察。", "新增预警更新时间及交通部门确认的封闭路段。", "旧积水照片再次上传，没有新增封路信息。", "预警解除且路段恢复，旧绕行提示失效。", "交通方更正路段方向，旧稿把对向车道也列为关闭。"]),
        ("虚构望溪高温服务安排", ["匿名帖称所有活动取消，组织方尚未确认。", "虚构气象机构发布限定区域预警，活动安排以各主办方为准。", "主办方公布延后场次与公共服务点开放时间。", "旧高温截图转发，没有新的活动变更。", "预警结束且活动完成，旧延期通知失效。", "主办方更正开放时间，旧稿把内部准备时段写成公众时段。"]),
    ]),
}


def build_catalog():
    stages = ("emerging", "confirmed", "developing", "repeat", "cooling", "corrected")
    profiles = ("click", "consumption", "interaction", "growth")
    sources, boards = [], []
    for board_index, (board_id, (board_name, focus, stories)) in enumerate(STORIES.items()):
        source_id = f"fictional-record-{board_id}"
        sources.append({"source_id": source_id, "title": f"虚构{board_name}情景事实记录（非外部新闻来源）",
                        "url": f"synthetic://operations-evolution-v2/{board_id}", "synthetic": True})
        board = {"board_id": board_id, "board_name": board_name, "operator_id": f"fixture-operator-{board_id}",
                 "operator_name": f"模拟{board_name}运营员", "scope_type": "operator",
                 "applicable_scenarios": ["待核验线索", "确认与进展", "重复与过期", "更正跟进"],
                 "metric_objectives": ["有效信息触达", "重要增量覆盖", "降低重复打扰", "更正透明度"],
                 "audience_summary": f"明确关注{board_name}主题且当前授权有效的模拟用户；地域事项只覆盖受影响区域。",
                 "risk_focus": focus.split("、"), "stories": []}
        for story_index, (title, updates) in enumerate(stories):
            split = ("development", "validation", "test")[story_index]
            start = datetime(2026, 10, 8 + story_index, 0, tzinfo=timezone.utc)
            story_id = f"{board_id}-thread-{story_index + 1}"
            story = {"story_id": story_id, "title": title, "split": split, "stages": []}
            for index, (stage_name, update) in enumerate(zip(stages, updates), 1):
                observed = start + timedelta(hours=index - 1)
                candidates = []
                for variant in range(2):
                    audience = {"scope": "targeted", "interest_tags": [board_id],
                                "region_codes": [], "subscription_topics": [], "user_segments": []}
                    if board_id in {"local", "weather", "domestic", "education", "health"}:
                        audience = {"scope": "targeted", "region_codes": ["fictional-affected-area"],
                                    "interest_tags": [], "subscription_topics": [], "user_segments": []}
                    copy = f"{title}：{update}"
                    claim = "仅解释当前已提供的事实与聚合指标，不推断真实效果或因果。"
                    decision, category, reasons, revision_fields, revision = "accepted", "positive_example", ["analysis_supported", "useful_recommendation"], [], None
                    block, layer, verdict, alternatives = False, "golden", "correct", None
                    conditions = {"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": False}
                    send_after, expire_at = observed + timedelta(minutes=5), observed + timedelta(minutes=55)
                    if index == 1:
                        block, layer = True, "high_risk"
                        if variant == 0:
                            decision, category, reasons = "deferred", "strategy_judgment", ["insufficient_evidence", "risk_requires_escalation"]
                            alternatives = ["deferred", "rejected"]
                            copy = f"待核验线索：{title}，未经核验的内容暂不作为事实通知。"
                        else:
                            decision, category, reasons, verdict = "rejected", "analysis_error", ["insufficient_evidence", "fact_or_attribution_error"], "incorrect"
                            copy = f"已完全证实：{title}影响所有人！"
                            claim = "仅凭点击增长即可确定网传说法属实，并扩大到所有地区。"
                    elif index == 2 and variant == 1:
                        block, layer = True, "high_risk"
                        decision, category, reasons = "rejected", "strategy_judgment", ["audience_mismatch"]
                        audience = {"scope": "all_users", "region_codes": [], "interest_tags": [], "subscription_topics": [], "user_segments": []}
                        if board_index % 3 == 1:
                            conditions["consent"] = "revoked"
                            reasons.append("channel_or_consent_mismatch")
                        elif board_index % 3 == 2:
                            conditions["frequency_budget"] = "exhausted"
                            reasons.append("frequency_budget_exhausted")
                    elif index == 3 and variant == 1:
                        block, layer, verdict = True, "fresh_bad_case", "partially_correct"
                        decision, category, reasons = "corrected", "analysis_error", ["fact_or_attribution_error", "copy_misleading"]
                        claim = "新增信息证明所有未决事项均已解决，热度变化证明效果提升。"
                        copy = f"{title}：全部问题已解决，效果已经提升！"
                        revision_fields, revision = ["copy"], f"改为带归因和范围的增量事实：{update}；保留未决事项，不推断效果。"
                    elif index == 4:
                        block = True
                        if variant == 0:
                            decision, category, reasons = "deferred", "strategy_judgment", ["timing_inappropriate", "frequency_budget_exhausted"]
                            conditions["frequency_budget"] = "exhausted"
                            copy = f"{title}背景梳理，拟合并进明日专题；当前独立通知额度不足。"
                            send_after, expire_at = observed + timedelta(hours=20), observed + timedelta(hours=22)
                        else:
                            decision, category, reasons = "rejected", "editorial_judgment", ["duplicate_event", "no_material_update"]
                            copy = f"刚刚：{title}重大新消息！"
                    elif index == 5:
                        block = True
                        send_after, expire_at = observed - timedelta(hours=2), observed - timedelta(hours=1)
                        if variant == 0:
                            decision, category, reasons = "corrected", "editorial_judgment", ["content_expired", "copy_misleading"]
                            revision_fields, revision = ["copy", "send_time"], f"删除过期行动提示，改为注明结束状态的回顾：{update}；重设仅用于新回顾建议的审核时间。"
                        else:
                            decision, category, reasons = "rejected", "strategy_judgment", ["content_expired", "timing_inappropriate"]
                            copy = f"即刻办理：{title}旧入口依然开放！"
                    elif index == 6 and variant == 1:
                        block, layer, verdict = True, "fresh_bad_case", "partially_correct"
                        decision, category, reasons = "corrected", "analysis_error", ["fact_or_attribution_error", "copy_misleading"]
                        copy = f"{title}：继续沿用旧口径，不展示更正。"
                        claim = "更正不影响旧表述，可继续按之前的范围和口径表达。"
                        revision_fields, revision = ["copy"], f"明示旧稿错误并改为当前事实：{update}；不得隐藏更正来源与时间。"
                    # Same lifecycle stage can lead to different decisions across desks.
                    # These are explicit fixture constraints, never real newsroom defaults.
                    if variant == 0 and index == 2 and board_id in {"finance", "entertainment"}:
                        block = True
                        decision, category = "deferred", "strategy_judgment"
                        if board_id == "finance":
                            conditions["quiet_hours"] = True
                            reasons = ["timing_inappropriate"]
                        else:
                            conditions["frequency_budget"] = "exhausted"
                            reasons = ["frequency_budget_exhausted"]
                    elif variant == 0 and index == 2 and board_id == "culture":
                        copy += " 本例为非紧急文化信息，放入站内专题更新。"
                    elif variant == 0 and index == 2 and board_id == "society" and story_index == 1:
                        block, layer, verdict = True, "fresh_bad_case", "partially_correct"
                        decision, category, reasons = "corrected", "analysis_error", ["fact_or_attribution_error", "copy_misleading"]
                        copy = "审查已坐实违法，负责人须承担全部责任。"
                        claim = "审查开始等同于违法结论成立。"
                        revision_fields, revision = ["copy"], f"仅说明已确认的审查状态和未决事项：{update}。"
                    elif variant == 0 and index == 3 and board_id in {"education", "international"}:
                        block, layer = True, "high_risk"
                        conditions["consent"] = "unknown" if board_id == "education" else "revoked"
                        decision = "deferred" if board_id == "education" else "rejected"
                        category, reasons = "strategy_judgment", ["channel_or_consent_mismatch"]
                    elif variant == 0 and index == 3 and board_id == "local":
                        block = True
                        send_after, expire_at = observed - timedelta(hours=2), observed - timedelta(hours=1)
                        decision, category, reasons = "corrected", "strategy_judgment", ["content_expired"]
                        revision_fields, revision = ["send_time"], "保留已确认的地域增量事实，按本次更新创建新的待审时间窗；旧时窗审批不可复用。"
                    elif variant == 0 and index == 6 and board_id == "weather":
                        block, layer = True, "high_risk"
                        audience = {"scope": "all_users", "region_codes": [], "interest_tags": [], "subscription_topics": [], "user_segments": []}
                        decision, category, reasons = "corrected", "strategy_judgment", ["audience_mismatch"]
                        revision_fields, revision = ["audience"], "只向曾接收错误区域提示且当前授权有效的模拟受影响群体生成更正待审建议。"
                    elif variant == 0 and index == 6 and board_id == "technology":
                        block, layer = True, "high_risk"
                        conditions["consent"] = "revoked"
                        decision, category, reasons = "rejected", "strategy_judgment", ["channel_or_consent_mismatch"]
                    optional = ["risk_requires_escalation"] if block and "risk_requires_escalation" not in reasons else []
                    expected = {"decision_type": decision, "allowed_decision_types": alternatives or [decision],
                                "category": category, "required_reason_codes": reasons, "optional_reason_codes": optional,
                                "explanation": f"合成参考：当前事实为“{update}”。依据{board_name}职责（{focus}）核对候选范围、表述和当前约束；本判断待人工校准，不代表真实团队策略。",
                                "revision": revision, "blocking_expectations": reasons if block else [],
                                "analysis_verdict": verdict, "layer": layer, "must_state_limitation": True,
                                "must_block": block, "required_revision_fields": revision_fields}
                    candidates.append({"candidate_key": f"v{variant}", "title": title,
                                       "summary": f"当前进展：{update}；全部为虚构新闻情景。",
                                       "analysis_claim": claim, "audience": audience,
                                       "channel": "in_app" if variant == 0 and index == 2 and board_id == "culture" else "app_notification",
                                       "notification_copy": copy, "send_after": send_after.isoformat(), "expire_at": expire_at.isoformat(),
                                       "conditions": conditions, "expected": expected})
                story["stages"].append({"stage_index": index, "stage_name": stage_name, "observed_at": observed.isoformat(),
                                        "prior_stage_refs": list(range(1, index)),
                                        "facts": [{"fact_id": f"{story_id}-fact-{index}", "text": update,
                                                   "available_at": observed.isoformat(), "source_id": source_id}],
                                        "aggregate_profile": profiles[(board_index + story_index + index) % 4],
                                        "content_type": "video" if (board_index + index) % 4 == 0 else "article",
                                        "candidates": candidates})
            board["stories"].append(story)
        boards.append(board)
    return {"schema_version": "operations-evolution-catalog-v2", "dataset_version": "2026-10-10.synthetic-evolution-v2",
            "synthetic": True, "sources": sources, "boards": boards}


def main():
    path = Path(__file__).parent / "seeds" / "operations_evolution_v2.json"
    payload = json.dumps(build_catalog(), ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != payload:
        raise ValueError("catalog already differs; create a new version rather than overwrite")
    path.write_text(payload, encoding="utf-8")
    print(str(path.resolve()))


if __name__ == "__main__":
    main()
