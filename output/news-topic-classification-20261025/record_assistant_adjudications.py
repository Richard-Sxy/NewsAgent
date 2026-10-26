"""Record source-bound AI semantic decisions for clear residual titles.

These are assistant judgments, never human gold labels. Uninformative titles
are deliberately left with their information-insufficient annotation.
"""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'agent/tools'))
from annotate_uncertain_news import inputs

DECISIONS=[
 ('20261006A0AFCI00','lifestyle','medium','讨论坦诚程度与人际吸引力，属于情感关系建议。'),
 ('20261006A0B09N00','tech','medium','苹果品牌标识调整属于科技企业与产品资讯。'),
 ('20261006A0B4XN00','ai','medium','WorkBuddy助手版本及自动任务功能更新，归AI应用。'),
 ('20261006A0BF0X00','games','medium','服务器、游戏物资和角色门派竞争明确指向天龙八部网游。'),
 ('20261006A0BGOJ00','ai','medium','参数规模、激活参数和开源模型发布明确属于大模型。'),
 ('20261006A0BH8600','lifestyle','low','标题将默认脚本作为自我识别能力讨论，暂按个人成长归类。'),
 ('20261006A0BHMD00','lifestyle','low','宝藏小城的走红主题按文旅生活内容归类。'),
 ('20261006A0BIG200','culture','medium','书单和阅读推荐属于文化。'),
 ('20261006A0BIOT00','tech','medium','社交网络申请顶级域名属于互联网平台资讯。'),
 ('20261006A0BL5T00','education','medium','科普基地的评级管理属于科学教育设施。'),
 ('20261006A0BLO600','entertainment','low','对具名文艺作品的表现作比较，按娱乐作品评论归类。'),
 ('20261006V0A0TE00','lifestyle','medium','蔬菜栽种的株行距属于种植园艺技巧。'),
 ('20261006V0A1SY00','lifestyle','low','游园与江南景观的体验表达按文旅归类。'),
 ('20261006V0A3TV00','lifestyle','medium','手工植物繁殖实操属于园艺生活技巧。'),
 ('20261006V0APY100','tech','medium','工业自动化的控制电压与电气原理属于技术科普。'),
 ('20261006V0AQTH00','society','medium','保税仓售假和监管漏洞是社会消费维权议题。'),
 ('20261006V0BDXN00','tech','low','标题指向工业发展与设计，暂归工程科技评论。'),
 ('20261006V0BEKY00','tech','medium','机械设备密封结构的适用性属于工程技术。'),
 ('20261006V0BF1400','lifestyle','medium','个人修养和处世境界属于生活哲理。'),
 ('20261006V0BF4B00','tech','medium','锌溴电池研发属于能源材料技术。'),
 ('20261006V0BFC900','lifestyle','medium','个人成长与自我提升属于生活建议。'),
 ('20261006V0BFLF00','lifestyle','medium','朋友关系疏远属于情感人际关系。'),
 ('20261006V0BFPX00','culture','medium','历史人物与旧照片的身份考证属于历史文化。'),
 ('20261006V0BG9K00','culture','medium','历史人物会面往事属于近现代史料。'),
 ('20261006V0BGAG00','tech','medium','弹簧秤拆解与结构原理属于机械科普。'),
 ('20261006V0BGI500','lifestyle','medium','情感执念与心理调节属于生活情感。'),
 ('20261006V0BGOW00','tech','medium','变压器及电网的电磁原理属于技术科普。'),
 ('20261006V0BGRJ00','lifestyle','low','标题讨论情感困扰，暂按生活心理归类。'),
 ('20261006V0BGV300','lifestyle','medium','婚恋缘分概念属于情感生活。'),
 ('20261006V0BGW500','tech','medium','盾构机的仿生设计灵感属于工程技术。'),
 ('20261006V0BGWC00','finance','low','发达国家的长期发展讨论按经济发展主题归类。'),
 ('20261006V0BH0400','lifestyle','medium','爱情意义讨论属于情感生活。'),
 ('20261006V0BHAH00','auto','low','车辆展车模式与操作风险按汽车使用场景归类。'),
 ('20261006V0BHR100','tech','medium','现代粮仓的机械化和自动化储粮属于工程技术。'),
 ('20261006V0BIA300','auto','medium','车辆通过限宽墩的新手与熟手差异属于驾驶技巧。'),
 ('20261006V0BIT900','lifestyle','medium','个人成功条件的建议属于生活成长。'),
 ('20261006V0BIYP00','lifestyle','low','顺利处世的建议按生活成长归类。'),
 ('20261006V0BIZY00','tech','medium','钢筋套筒的施工连接技术属于工程科普。'),
 ('20261006V0BIZZ00','lifestyle','medium','运气与处世的生活建议。'),
 ('20261006V0BJ0M00','tech','medium','磁力和机械结合的结构设计属于工程技术。'),
 ('20261006V0BJ3H00','entertainment','low','标题是歌词式音乐表达，暂归音乐娱乐。'),
 ('20261006V0BJNB00','tech','medium','缝纫机的机械工作原理科普。'),
 ('20261006V0BJX600','tech','medium','海底光缆的敷设及防护技术。'),
 ('20261006V0BJX900','tech','medium','跨海大桥的建造工程技术。'),
 ('20261006V0BJXA00','tech','medium','FPSO海上工程装备的工作原理。'),
 ('20261006V0BK2J00','lifestyle','medium','人际关系的心理调适建议。'),
 ('20261006V0BK3Q00','auto','low','具名07产品的预售订单话题，按车型市场资讯归类。'),
 ('20261006V0BKDI00','tech','medium','机械陀螺仪的物理原理。'),
 ('20261006V0BKEM00','tech','medium','齿轮组成的机械装置与制作。'),
 ('20261006V0BKK400','tech','medium','液力和磁力传动的技术原理。'),
 ('20261006V0BKM900','lifestyle','medium','做人处世与人生观建议。'),
 ('20261006V0BKNM00','tech','medium','门锁机械结构及工作原理。'),
 ('20261006V0BKQE00','education','medium','以作文为内容形式的写作教育材料。'),
 ('20261006V0BKQJ00','tech','medium','陀螺仪感知方向和速度的传感器原理。'),
 ('20261006V0BL3M00','tech','medium','超高建筑基础与桩基工程。'),
 ('20261006V0BL4600','culture','low','木工手作技艺和工匠比赛按工艺文化归类。'),
 ('20261006V0BL4A00','tech','medium','铁路钢轨的热膨胀工程原理。'),
 ('20261006V0BL6600','tech','medium','光学颜色感知和打印机原理属于科学技术。'),
 ('20261006V0BL8R00','society','medium','民间手写船号技师的人物报道。'),
 ('20261006V0BL9100','lifestyle','low','应对生活不顺的建议。'),
 ('20261006V0BLB000','lifestyle','low','个人提升建议。'),
 ('20261006V0BLGG00','tech','medium','电线杆材料选择的电力工程讨论。'),
 ('20261006V0BLIE00','lifestyle','medium','人生修养与处世建议。'),
 ('20261006V0BLJQ00','tech','medium','永动机及相关科学研究的科普议题。'),
 ('20261006V0BLL500','tech','medium','轴承制造与钢珠装配的机械工程。'),
 ('20261006V0BLLK00','lifestyle','medium','人际沟通边界建议。'),
 ('20261006V0BLN800','lifestyle','medium','烦恼时的情绪与沟通管理。'),
 ('20261006V0BLNM00','entertainment','medium','具名作品和角色剧情的娱乐内容。'),
 ('20261006V0BLPR00','tech','medium','铁路道岔变轨及调度装置的技术科普。'),
 ('20261006V0BLQ000','tech','medium','货轮设备结构与用途科普。'),
 ('20261006V0BLSH00','auto','medium','路虎卫士和神行者车型的选车比较。'),
 ('20261006V0BLUH00','auto','medium','车顶帐篷这一汽车用品及营销话题。'),
 ('20261007A07K2Y00','tech','medium','域名、DNS和浏览器证书劫持属于网络安全。'),
 ('20261011A0718S00','tech','medium','星闪无线通信的国家技术标准属于通信科技。'),
 ('20261013A0DUVT00','ai','low','人机共笔指向AI协作写作的议题，缺少正文故标低置信。'),
 ('20261014A091AO00','ai','medium','企业微信与WorkBuddy助手集成并自动执行任务，属于AI办公应用。'),
]
_,source=inputs()
by_id={r['news_id']:r for r in source}
result=[]
for news_id,category,confidence,reason in DECISIONS:
    row=by_id[news_id]
    result.append({'news_id':news_id,'record_sha256':row['record_sha256'],'category':category,
                   'confidence':confidence,'reason':reason,'evidence_quote':row['title'],
                   'source':'assistant_semantic_adjudication','human_verified':False})
path=ROOT/'var/news-storage/uncertain-annotations-20261025/full-v2/assistant-adjudications.json'
path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps({'count':len(result),'path':str(path)},ensure_ascii=False))
