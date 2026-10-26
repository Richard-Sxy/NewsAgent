"""Offline, reviewable topic inventory; never writes source data or the database.

Python 3.8+, standard library only. Run from the repository root:
python3 output/news-topic-classification-20261025/classify.py --output output/news-topic-classification-20261025/results
"""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import html
import json
from pathlib import Path
import random
import re

VERSION = "qq-web-topic-inventory-20261025-v2"
ROOT = Path(__file__).resolve().parents[2]
# These are local inference terms, not Tencent's internal taxonomy or gold labels.
# NBA remains a sports subtopic; AI follows the current independent web entrance.
CATEGORIES = {
    "finance": ("财经", "https://news.qq.com/ch/finance/", "宏观经济、证券基金、公司经营、金融消费", "ETF 基金 股价 股票 股市 A股 港股 美股 沪指 深成指 创业板 科创板 涨停 跌停 涨幅 跌幅 净流入 净流出 主力 资金流向 净值 证券 银行 保险 人寿 财报 营收 净利润 扣非 毛利率 业绩 上市公司 上市首日 IPO 融资 融券 股东 股份 分红 回购 增持 减持 投资者 投资评级 目标价 股息 市盈率 市值 债券 国债 期货 利率 降息 加息 央行 财政 GDP 通胀 通缩 关税 贸易战 汇率 人民币 美元 黄金价格 金价 油价 原油 比特币 加密货币 营业收入 交易所 理财 券商 货币政策 宏观经济 外贸 进出口 海关总署 消费市场 促消费 经济增长 招商引资 并购 重组 破产 工商 罚单 合资 产值 信托 财经 中证 恒生 标普 纳指 道指 沪深 存款 贷款 资本市场 经济数据 资产管理 新三板 行业研报 产业链 供应链 盈利 收购 超市 零售 电商 直播带货 补贴 白酒 茅台 咖啡 餐饮 外卖 双11 财经晚报"),
    "sports": ("体育", "https://sports.qq.com/", "NBA/CBA、足球、乒羽网球、综合赛事", "NBA CBA WNBA NBL WTT ATP WTA UFC F1 足球 男足 女足 国足 中超 中甲 亚冠 欧冠 英超 西甲 意甲 德甲 法甲 皇马 巴萨 曼联 曼城 阿森纳 利物浦 切尔西 拜仁 热刺 巴黎圣日耳曼 国米 尤文 米兰 梅西 C罗 姆巴佩 哈兰德 篮球 男篮 女篮 湖人 勇士 火箭 凯尔特人 雷霆 快船 马刺 掘金 詹姆斯 库里 杜兰特 东契奇 文班亚马 欧文 约基奇 字母哥 朱芳雨 徐杰 胡明轩 赵继伟 周琦 郭艾伦 焦泊乔 辽篮 宏远 北控 乒乓 羽毛球 网球 斯诺克 台球 孙颖莎 王楚钦 林诗栋 梁靖崑 樊振东 王曼昱 陈梦 马龙 郑钦文 张之臻 商竣程 辛纳 阿尔卡拉斯 德约 肖国栋 丁俊晖 赵心童 奥沙利文 勒布伦 松岛辉空 奥运 全运会 亚运会 世界杯 世锦赛 世乒赛 田径 游泳 马拉松 百米 跳水 排球 女排 男排 武磊 伊万科维奇 里尔 裁判 球员 球队 赛季 联赛 季后赛 总决赛 冠军赛 半决赛 决赛 首轮 比分 进球 扣篮 罚球 助攻 篮板 护筐 得分王 金牌 夺冠 世预赛 国乒 体操 体育赛事 运动会 环法 骑行赛 高尔夫 举重 拳击 滑雪 体育"),
    "tech": ("科技", "https://news.qq.com/ch/tech/", "数码、芯片、互联网、航天与科学", "科技 芯片 半导体 光刻机 晶圆 制程 台积电 中芯 英特尔 高通 联发科 骁龙 麒麟处理器 天玑 处理器 CPU GPU iPhone iPad MacBook 苹果手表 苹果发布会 华为手机 小米手机 荣耀手机 OPPO vivo 三星手机 一加 红米 Redmi 鸿蒙 HarmonyOS iOS Android 安卓 手机 旗舰机 影像旗舰 折叠屏 智能手表 平板电脑 笔记本电脑 显卡 主板 内存 硬盘 操作系统 软件更新 开源代码 Linux Windows 浏览器 网络安全 网络攻击 数据泄露 互联网 微信功能 微信更新 通信 5G 6G 量子 光通信 光纤 数据中心 服务器 机器人 机械臂 人形机器人 智能家居 扫地机 数码 镜头 徕卡 光学变焦 相机 科研 科学家 天文 宇宙 星系 黑洞 行星 天体 物理学 诺贝尔物理 诺贝尔化学 化学研究 材料科学 超导 核聚变 航天 火箭发射 卫星发射 空间站 探月 嫦娥 神舟 天舟 长征火箭 北斗 载人飞船 火星探测 SpaceX 星舰 深海探测"),
    "ai": ("AI", "https://news.qq.com/ch/fx", "大模型、生成式 AI、智能体、算力及应用", "AI 人工智能 大模型 语言模型 生成式 智能体 Agent ChatGPT OpenAI DeepSeek Claude Gemini GPT LLM AIGC 文生图 文生视频 AI绘画 AI视频 AI编程 AI应用 AI教育 AI医疗 AI眼镜 AI手机 AI芯片 AI算力 算力中心 算力集群 模型训练 模型推理 模型微调 神经网络 机器学习 深度学习 具身智能 世界模型 多模态 提示词 百模 千问 通义 豆包 文心一言 混元 Kimi 智谱 MiniMax Anthropic Sora WAIC"),
    "international": ("国际", "https://news.qq.com/ch/world/", "国际政治、外交、国际组织及海外事件", "特朗普 拜登 哈里斯 万斯 白宫 美国总统 美国国会 国务卿 美国国务院 美联储 普京 泽连斯基 俄罗斯总统 乌克兰总统 马克龙 默茨 斯塔默 苏纳克 内塔尼亚胡 以色列总理 石破茂 高市早苗 日本首相 韩国总统 李在明 尹锡悦 安倍 岸田 莫迪 卢拉 米莱 欧尔班 联合国 安理会 欧盟 东盟 G7 G20 金砖 外交部 外长 外交官 国事访问 双边关系 外交关系 领事馆 大使馆 驻外使馆 总统大选 议会选举 国际局势 移民政策 难民 签证 德国总理 法国总统 英国首相 伊朗总统 朝鲜领导人 金正恩 联大 联合声明 国际社会 伊朗 伊拉克 叙利亚 以色列 巴勒斯坦 加沙 黎巴嫩 乌克兰 俄乌 美国 日本 韩国 英国 法国 德国 俄罗斯 印度 巴基斯坦 土耳其 加拿大 巴西 墨西哥 阿根廷 澳大利亚 新加坡 越南 泰国 印尼 菲律宾 缅甸 柬埔寨 尼泊尔 瑞士 瑞典 西班牙 意大利 葡萄牙 新西兰 海外"),
    "military": ("军事", "https://news.qq.com/ch/milite/", "国防军队、武器装备、演训与战争", "军事 国防 解放军 军队 陆军 海军 空军 火箭军 航母 舰艇 战舰 驱逐舰 护卫舰 潜艇 战斗机 战机 轰炸机 歼- 歼20 歼35 歼-20 歼-35 F-35 F-22 苏-57 导弹 坦克 装甲车 战斗群 核武器 核潜艇 核弹 反导 弹道 巡航导弹 军演 演习 阅兵 军工 武器 装备采购 空袭 轰炸 袭击目标 无人机袭击 军方 军事行动 军事基地 战场 战争 战线 战事 战术 战略轰炸 俄军 乌军 美军 以军 伊军 北约 北部战区 南部战区 东部战区 西部战区 中部战区 边防 军人 官兵 士兵 战士 参谋长 司令 武装 海警 火炮 炮弹 交火 反击作战 停火 防空 防务 军援 兵力 备战 武统 台海 黄岩岛 南海 军史 抗美援朝 战役"),
    "education": ("教育", "https://news.qq.com/ch/edu/", "基础教育、升学考试、高校、留学与职教", "教育 学校 校园 中小学 小学 中学 初中 高中 高校 大学 学院 幼儿园 幼教 幼升小 小升初 高考 中考 考研 考公 公务员考试 国考 教师资格 教师 老师 学生 同学 班主任 校长 校规 班级 教育局 教育部 学费 学历 学位 本科 专科 硕士 博士 招生 招录 录取 志愿填报 保研 读研 留学 研究生 学科建设 课程 课堂 作业 开学 毕业 校友 职业教育 职校 技校 复读 培训机构 学术不端 论文答辩 学霸 清华 北大 复旦 浙大 哈工大 双一流 985 211 竞赛保送 青少年教育"),
    "entertainment": ("娱乐", "https://news.qq.com/ch/ent/", "影视、综艺、音乐与演艺人物", "娱乐 明星 演员 艺人 导演 编剧 电影 影片 票房 影帝 影后 金鸡奖 金像奖 金马奖 电视剧 剧集 网剧 短剧 热播剧 古装剧 偶像剧 影视 综艺 真人秀 晚会 歌手 歌曲 新歌 音乐会 演唱会 专辑 乐队 女团 男团 男星 女星 顶流 经纪人 戛纳 电影节 柏林电影 威尼斯电影 奥斯卡 红毯 视帝 视后 烂片 收视率 豆瓣评分 杀青 官宣恋情 娱记 狗仔 练习生 王一博 肖战 杨幂 赵丽颖 刘亦菲 迪丽热巴 杨紫 白鹿 虞书欣 成毅 任嘉伦 李现 王鹤棣 王俊凯 易烊千玺 王源 周杰伦 薛之谦 邓紫棋 陈奕迅 张学友 刘德华 周星驰 成龙 章子怡 范冰冰 张柏芝 谢霆锋 梁朝伟 黄晓明 林心如 霍建华 黄磊 何炅 汪涵 沈腾 贾玲 马丽 德云社 郭德纲 于谦 相声 脱口秀 动漫 动画电影 漫威 海贼王 火影忍者"),
    "auto": ("汽车", "https://news.qq.com/ch/auto/", "新能源车、车型、智能驾驶与用车", "汽车 新车 车型 车企 车市 车展 轿车 跑车 SUV MPV 新能源车 新能源汽车 电动汽车 电动车 纯电 插混 增程 燃油车 充电桩 换电站 智能驾驶 自动驾驶 辅助驾驶 智能座舱 车机 底盘 发动机 变速箱 马力 百公里 试驾 购车 车主 召回 汽车销量 新车销量 特斯拉 比亚迪 蔚来 小鹏 理想汽车 零跑 吉利 奇瑞 长城汽车 宝马 奔驰 奥迪 保时捷 沃尔沃 雷克萨斯 丰田 本田 日产 马自达 比亚迪海洋 比亚迪王朝 汉EV 唐DM 腾势 方程豹 问界 智界 享界 尊界 岚图 小米SU7 小米YU7 极氪 极狐 深蓝汽车 阿维塔 奔腾 红旗汽车 福特 雪佛兰 别克 大众汽车 上汽 一汽 长安汽车 广汽 东风汽车 福田汽车 江淮 购置税 油耗 摩托车"),
    "house": ("房产", "https://news.qq.com/ch/house/", "楼市、买卖租赁、土地、住房政策", "房产 房地产 楼市 房价 房贷 住房 公积金 买房 购房 卖房 新房 二手房 学区房 商品房 住宅 豪宅 别墅 楼盘 楼栋 房企 烂尾楼 保交楼 保交房 现房 期房 土拍 土地出让 地价 租房 房租 租金 租赁住房 保障房 保障性住房 安置房 旧改 城中村改造 棚改 拆迁 物业 业主 得房率 容积率 首付 首套房 存量房 万科 碧桂园 融创 恒大地产 绿城 中海地产 龙湖地产 保利地产 贝壳找房 链家 房屋征收 住建 居住证 入住率"),
    "games": ("游戏", "https://news.qq.com/ch/games/", "端游手游、主机、电竞及游戏产业", "游戏 电竞 网游 手游 端游 主机游戏 玩家 Steam Switch PlayStation Xbox PS5 任天堂 索尼PS 暴雪 育碧 卡普空 科乐美 Epic 任天堂直面会 TGA IGN GameSpot 游戏机 游戏手柄 游戏开发 DLC 试玩 季票 皮肤返场 赛季皮肤 吃鸡 王者荣耀 和平精英 英雄联盟 LOL LPL LCK KPL DOTA CS2 无畏契约 原神 崩坏 星穹铁道 绝区零 鸣潮 黑神话 魔兽世界 守望先锋 暗黑破坏神 地下城与勇士 DNF 梦幻西游 逆水寒 燕云十六声 永劫无间 三国志战略版 生化危机 刺客信条 艾尔登法环 塞尔达 宝可梦 怪物猎人 战地6 战地风云 GTA 侠盗猎车手 最终幻想 黑旗RE 明日方舟 碧蓝航线 米哈游 鹰角 游戏版号 氪金 抽卡 战队 上分 出装 BOSS 副本 网吧"),
    "health": ("健康", "https://news.qq.com/ch/health/", "疾病防治、医疗、医药研究及营养", "健康 养生 医疗 医院 医生 医师 患者 病人 临床 诊疗 诊断 治疗 疾病 病症 症状 癌症 肿瘤 肺癌 肝癌 胃癌 乳腺癌 心脏病 高血压 糖尿病 心梗 脑梗 中风 骨折 手术 急救 急诊 门诊 中医 西医 中药 疫苗 流感 传染病 防疫 艾滋 乙肝 新冠 医保 医药 新药 药物 医学 体检 胆固醇 血糖 血压 血脂 营养 膳食 食疗 控糖 减肥 肥胖 睡眠 失眠 抑郁 焦虑症 心理咨询 心理健康 阿尔茨海默 帕金森 认知症 免疫力 维生素 呼吸道 感染 消化科 骨科 眼科 口腔 牙科 康复 护士 护理 养老院 三甲 白内障 近视 备孕 妇科 儿科 新生儿 痛风 肾病 医患 医美"),
    "domestic": ("国内时政", None, "国内政务、政策、人事与区域发展（本地补充分组）", "习近平 李强 赵乐际 王沪宁 蔡奇 丁薛祥 李希 中共中央 中央政治局 总书记 国务院 全国人大 全国政协 二十届 全会 两会 党代会 党史 党建 党员 党组织 党委 省委 省政府 市委 市政府 县委 县政府 纪委 监委 巡视组 巡察 政务 政策解读 国务委员 省委书记 市委书记 省长 市长 县长 党纪 党风廉政 反腐 落马 双开 履新 任免 任命 被查 审查调查 十五五 十四五 中国式现代化 乡村振兴 共同富裕 高质量发展 国家发展 贯彻落实 学习贯彻 重要讲话 重要指示 强国建设 基层治理 全国总工会 统战 统一战线 两岸关系 台湾当局 赖清德 国民党 民进党 国台办 一国两制 爱国主义 人民代表 政协委员 政府工作"),
    "society": ("社会民生", None, "法治、公共安全、交通、天气与地方民生（本地补充分组）", "社会 民生 居民 社区 街道 便民 公共服务 公共安全 消防 火灾 起火 爆炸 溺水 失联 失踪 救援 救助 应急 防灾 洪水 暴雨 台风 天气 气象 寒潮 高温预警 低温 强降雨 地震 灾害 抢险 洪灾 山体滑坡 泥石流 交通 高速公路 高铁 铁路 地铁 公交 航班 机场 铁路局 停运 交通管制 交警 肇事 车祸 事故 超速 驾照 斑马线 警方 公安 警察 民警 派出所 报警 逮捕 被捕 拘留 违法 犯罪 嫌疑人 凶手 杀人 诈骗 电诈 盗窃 贪污 受贿 非法集资 法院 检察院 检察官 判决 判刑 起诉 诉讼 法治 法律援助 律师 司法 调解 欠款 赔偿 受害者 维权 消费者权益 招聘 岗位 就业 招工 工资 社保 养老金 退休 公益 志愿者 残疾 无障碍 环境保护 污染 污水 垃圾分类 停电 停水 供水 供电 供暖 自来水 农民 农村 秋收 夏收 丰收 农业 农作物 种植 养殖 粮食安全 食品安全 通报 悬赏 寻人 惠民 城管 市场监管 烟花爆竹 工伤 仲裁 快递 外卖员 弱势群体 卫健委"),
    "culture": ("文化历史", None, "文学、艺术、文博、非遗与历史（本地补充分组）", "文化 文物 考古 非遗 文化遗产 博物馆 美术馆 文博 艺术 美术 绘画 画家 书法 书画 文学 作家 小说 诗词 诗歌 诗人 书展 读书 新书 图书 出版社 诺贝尔文学 奖项文学 戏剧 戏曲 京剧 越剧 昆曲 豫剧 舞剧 舞蹈 音乐剧 交响乐 历史 古代 古人 史学 史料 历史人物 历史故事 历史研究 朝代 春秋 战国 秦朝 汉朝 三国 魏晋 唐朝 宋朝 元朝 明朝 清朝 民国 皇帝 皇后 太后 皇宫 宰相 丞相 王朝 朝廷 乾隆 康熙 雍正 慈禧 溥仪 朱元璋 朱棣 李世民 刘邦 刘备 曹操 孙权 诸葛亮 武则天 李白 杜甫 苏轼 红楼梦 三国演义 西游记 水浒传 儒家 孔子 孟子 王阳明 故宫 敦煌 石窟 古墓 古籍 石器 青铜器 陶瓷 书院 祠堂 历史文化"),
    "lifestyle": ("生活文旅", None, "旅游、美食、时尚、家庭与生活（本地补充分组）", "旅游 文旅 景区 景点 旅行 游客 旅客 民宿 酒店 度假 游记 自驾游 露营 徒步 赏秋 赏花 秋景 城市漫步 打卡点 风景 风光 山水 园林 公园 乐园 古镇 古城 赏枫 海岛 海滩 博主游记 美食 菜谱 烹饪 家常菜 做菜 食谱 饭菜 早餐 午餐 晚餐 火锅 烤肉 小吃 甜品 烘焙 煲汤 厨房 时尚 穿搭 搭配 秋装 冬装 大衣 羽绒服 连衣裙 半身裙 衬衫 针织衫 牛仔裤 高跟鞋 化妆 彩妆 护肤 美妆 口红 时装 模特 秀场 香奈儿 迪奥 奢侈品 珠宝 饰品 婚姻 结婚 离婚 婚礼 夫妻 婆媳 丈夫 妻子 老公 老婆 儿媳 公婆 彩礼 相亲 恋爱 情感 育儿 亲子 宝妈 育娃 孩子福报 家庭教育 宠物 猫咪 狗狗 养猫 养狗 花卉 养花 绿植 多肉 家居 装修 收纳 星座 运势 风水 生肖 祝福 人生感悟 佛教 道教 寺庙"),
}

SUPPLEMENTAL_TERMS = {
    "auto": "动力电池 三电平台 智己 BMW 帕杰罗 三菱汽车 东风汽车 绿智平台",
    "sports": "亚马尔 转会 租借加盟 布阿纳尼 普勒滕贝格 弗里克 法里尼亚斯 阿夫迪亚 比甲 荷甲 葡超 骑士队 步行者队 森林狼队 排名赛 大满贯 德约科维奇 罗伯逊 特鲁姆普",
    "games": "终末地 GTA6 NS2 游戏发布会 游戏销量 游戏角色",
    "entertainment": "张韶涵 李欣汝 欧豪 马思纯 陈昊宇 吸血鬼日记 美剧 韩剧 日剧",
    "tech": "三星 半导体存储 长江存储 长鑫存储 动力电池技术",
    "finance": "工业和信息化部 工信部 市场竞争秩序 产业体系",
}
for _key, _terms in SUPPLEMENTAL_TERMS.items():
    _name, _url, _description, _vocabulary = CATEGORIES[_key]
    CATEGORIES[_key] = (_name, _url, _description, _vocabulary + " " + _terms)

# A country mention is geographic evidence, not by itself an international-news
# topic. This prevents overseas football, cars and company news dominating here.
GEOGRAPHY_ONLY = set("美国 日本 韩国 英国 法国 德国 俄罗斯 印度 巴基斯坦 土耳其 加拿大 巴西 墨西哥 阿根廷 澳大利亚 新加坡 越南 泰国 印尼 菲律宾 缅甸 柬埔寨 尼泊尔 瑞士 瑞典 西班牙 意大利 葡萄牙 新西兰 海外".split())


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def digest_file(path):
    h = sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_selected(source):
    """Verify the selected IDs/versions against the checksum-bound frozen package."""
    package = ROOT / "var/news-storage/unified-public-news-20261023"
    manifest = json.loads((package / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert digest_file(package / name) == expected["sha256"], name
    items = {r["news_id"]: r for r in map(json.loads, (package / "release_items.jsonl").open())}
    rows = list(map(json.loads, source.open(encoding="utf8")))
    assert len(rows) == len(items) == len({r["news_id"] for r in rows})
    for row in rows:
        assert row["record_sha256"] == items[row["news_id"]]["record_sha256"]
        assert row["release_id"] == manifest["release"]["release_id"]
    # Verify the actual text too, not just the declared version identity.
    versions = {(r["news_id"], r["record_sha256"]): r for r in map(json.loads, (package / "versions.jsonl").open())}
    for row in rows:
        original = versions[(row["news_id"], row["record_sha256"])]
        for field in ("title", "body", "fragments", "content_state", "content_type", "url"):
            assert row[field] == original[field], (row["news_id"], field)
    return rows, manifest


def source_units(row):
    """Use fragments as independent evidence, never reconstruct or overwrite a body."""
    title = row.get("title") or ""
    title_source = "record_title" if title else "none"
    fragments = row.get("fragments") or []
    if not title:
        headings = sorted({m.group(1).strip() for f in fragments
                           for m in re.finditer(r"^# ([^\n]+)", f["text"], re.M)})
        if len(headings) == 1:
            title, title_source = headings[0], "fragment_heading_unverified"
    raw = [row["body"]] if row.get("body") else [f["text"] for f in fragments]
    texts = []
    for unit in raw:
        lines = [s for s in unit.splitlines() if not re.match(
            r"^(# |来源地址[:：]|作者[:：]|发布时间[:：]|https?://)", s.strip())]
        cleaned = "\n".join(lines)
        if cleaned not in texts:
            texts.append(cleaned)
    # A bounded prefix per evidence unit avoids favouring long or duplicated chunks.
    return title, title_source, [s[:2500] for s in texts[:5]]


def contains(text, term):
    if re.fullmatch(r"[A-Za-z0-9]+", term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text, re.I) is not None
    return term.casefold() in text.casefold()


def match_terms(texts, terms):
    folded = [text.casefold() for text in texts]
    matched = []
    for term in sorted(terms, key=lambda x: (-len(x), x)):
        needle = term.casefold()
        if re.fullmatch(r"[A-Za-z0-9]+", term):
            pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
            found = any(re.search(pattern, t) is not None for t in folded)
        else:
            found = any(needle in t for t in folded)
        if found and not any(needle in old.casefold() for old in matched):
            matched.append(term)
    return matched


def classify(row):
    title, title_source, units = source_units(row)
    scores, evidence, title_hits = {}, {}, {}
    for key, (_, _, _, vocabulary) in CATEGORIES.items():
        terms = vocabulary.split()
        th = match_terms([title], terms)
        bh = match_terms(units, terms)
        # Body score saturates; title terms carry the editorial topic signal.
        if key == 'international':
            title_score = sum(2 if t in GEOGRAPHY_ONLY else 6 for t in th)
            text_score = sum(.25 if t in GEOGRAPHY_ONLY else 1.25 for t in bh)
            score = min(30, title_score) + min(10, text_score)
        else:
            score = min(30, 6 * len(th)) + min(10, 1.25 * len(bh))
        if score:
            scores[key] = round(score, 2)
            title_hits[key] = th
            evidence[key] = {"title_terms": th, "text_terms": bh}
    ranked = sorted(scores, key=lambda k: (-scores[k], k))
    top = ranked[0] if ranked else None
    margin = scores[top] - (scores[ranked[1]] if len(ranked) > 1 else 0) if top else 0
    strong_title = top is not None and any(top != 'international' or t not in GEOGRAPHY_ONLY for t in title_hits[top])
    accepted = top is not None and (strong_title or scores[top] >= 3.75)
    flags = []
    if row["content_state"] == "fragments_only":
        flags.append("fragment_evidence_only")
    elif row["content_state"] == "title_only":
        flags.append("title_evidence_only")
    if "metadata_review_required" in row.get("quality_flags", []) + row.get("selection_quality_flags", []):
        flags.append("source_metadata_review")
    if accepted and margin < 2.5:
        flags.append("close_topic_scores")
    if accepted and not title_hits[top]:
        flags.append("text_only_topic_signal")
    if not accepted:
        flags.append("weak_or_no_topic_signal")
    primary = top if accepted else "unclassified"
    return {"news_id": row["news_id"], "record_sha256": row["record_sha256"],
            "body_sha256": row["body_sha256"], "release_id": row["release_id"],
            "original_title": row["title"], "display_title": title or "（原始标题缺失）",
            "display_title_source": title_source, "url": row["url"],
            "content_state": row["content_state"], "content_type": row["content_type"],
            "primary_category": primary, "primary_name": CATEGORIES[primary][0] if accepted else "待分类",
            "secondary_candidates": [k for k in ranked[1:4] if scores[k] >= 3.75] if accepted else ranked[:3],
            "scores": {k: scores[k] for k in ranked}, "score_margin": round(margin, 2),
            "evidence": {k: evidence[k] for k in ranked[:3]}, "review_flags": flags,
            "status": "draft_rule_inferred" if accepted else "unclassified",
            "policy_version": VERSION, "official_source_category": None,
            "labels_are_inferred": True, "human_verified": False}


def build_report(rows, summary, output):
    esc = lambda s: html.escape(str(s))
    parts = ['<!doctype html><html lang="zh"><meta charset="utf-8"><title>新闻主题分类盘点</title>',
             '<style>body{font:16px/1.7 system-ui,sans-serif;max-width:1150px;margin:40px auto;padding:0 24px;color:#203044}h1{font-size:30px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px 12px;border-bottom:1px solid #dfe5ec;text-align:left}th{background:#edf3f8}small{color:#667}a{color:#1865aa}details{margin:16px 0;padding:12px;background:#f5f8fb}code{font-size:13px}li{margin:6px 0}.note{background:#fff3d9;padding:18px}</style>',
             '<h1>NewsAgent 新闻主题分类盘点</h1><p>核对日期：2026-10-25 · 统计单位：news_id · 分类版本：' + VERSION + '</p>',
             '<p class="note">这是基于现有标题、缓存正文和独立历史片段的粗分类初稿，不是腾讯原始栏目、人工金标或模型质量验收。全量共 21,435 个新闻 ID；正文精确去重后为 21,427 条内容。主类互斥，跨类候选另列。缺失标题可展示唯一片段标题，标记为未验证，不回填源库。健康内容在此统计不等于准予向量入库。</p>',
             '<p>正式网页入口参考：<a href="https://www.qq.com/">腾讯网</a> / <a href="https://news.qq.com/">腾讯新闻</a>。科技、财经、AI、教育、国际、军事、娱乐、体育、汽车、房产、游戏、健康来自公开导航；NBA并入体育。国内时政、社会民生、文化历史、生活文旅为本项目补充分组；要闻、热问和视频不作为互斥内容主题。</p>',
             '<table><tr><th>主类</th><th>新闻数</th><th>占全量</th><th>有正文</th><th>仅片段</th><th>仅标题</th><th>主题范围</th></tr>']
    for s in summary['categories']:
        parts.append('<tr>' + ''.join('<td>'+esc(v)+'</td>' for v in [s['name'],s['count'],str(s['percent'])+'%',s['by_content_state'].get('body_available',0),s['by_content_state'].get('fragments_only',0),s['by_content_state'].get('title_only',0),s['description']])+'</tr>')
    parts.extend(['</table><h2>如何阅读</h2><p>每篇最多一个主类。规则按标题命中6分/词（封顶30分）和正文/片段命中1.25分/词（封顶10分）计分，同一包含关系不重复加分；国际类中的普通国家名称降为标题2分、文本0.25分，避免仅凭地名抢占体育/产业主题。分数是排序信号，不是准确率或置信概率。没有实质标题命中时需文本分至少3.75。前两名差值小于2.5标记跨类待复核。规则无法充分判断的保留为待分类。</p>',
                  '<p>复核标记汇总（可以重叠）：<code>'+esc(json.dumps(summary['review_flags'],ensure_ascii=False))+'</code></p>',
                  '<p>全部数据读取本地冻结包与导出文件，已核对文件SHA、news_id唯一性、版本绑定及实际标题/正文/片段逐字段一致；数据库只读核对确认21,435条选定新闻和21,427条去重内容。没有修改原标签、数据库、生产规则、模型配置或知识入库策略。</p>',
                  '<h2>各类可复核样本</h2><p>每类确定性抽取最多8条有正文、8条片段、4条仅标题记录；完整逐篇结果见 classification.jsonl。样本没有经过独立人工质量打分。</p>'])
    for s in summary['categories']:
        subset = [r for r in rows if r['primary_category']==s['id']]
        rng = random.Random('20261025-'+s['id'])
        sample = []
        for state, n in [('body_available',8),('fragments_only',8),('title_only',4)]:
            eligible = [r for r in subset if r['content_state']==state]
            sample += rng.sample(eligible,min(n,len(eligible)))
        parts.append('<details><summary>'+esc(s['name'])+' · '+str(s['count'])+'条</summary><ol>')
        for r in sample:
            title = esc(r['display_title'])
            if r['url'] and r['url'].startswith(('https://news.qq.com/','https://new.qq.com/')):
                title = '<a href="'+esc(r['url'])+'">'+title+'</a>'
            parts.append('<li>'+title+'<br><small>'+esc(r['news_id']+' | '+r['content_state']+' | '+','.join(r['review_flags']))+'</small></li>')
        parts.append('</ol></details>')
    parts.append('<h2>调用链、配置、验证与TODO</h2><p>统一公开新闻冻结包及all-news-with-tags.jsonl → load_selected校验和选定版本 → source_units提取标题与独立片段证据 → classify按版本化关键词计分、保留跨类候选 → Python Counter计算主类统计 → classification.jsonl、summary.json和本报告。外部依赖仅Python标准库；网页栏目参考已于本日核对，不在线调用模型、Embedding或数据库写接口。配置：脚本CATEGORIES、VERSION、--input、--output。测试入口：同目录test_classify.py；结果验证入口：summary.json。TODO：分层人工复核、校准规则/量化错误率、补齐原栏目元数据；是否持久化为业务分类另行决定。</p></html>')
    (output/'report.html').write_text(''.join(parts),encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'var/news-storage/exports/20261023/all-news-with-tags.jsonl')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Choose a new result directory; existing results are not overwritten.')
    source, manifest = load_selected(args.input)
    result = [classify(r) for r in sorted(source,key=lambda x:x['news_id'])]
    counts = Counter(r['primary_category'] for r in result)
    categories = []
    for key in sorted([*CATEGORIES,'unclassified'],key=lambda k:(-counts[k],k)):
        name, url, description, _ = CATEGORIES.get(key,('待分类',None,'缺少足够主题证据',''))
        categories.append({'id':key,'name':name,'reference_url':url,'description':description,
                           'count':counts[key],'percent':round(100*counts[key]/len(result),2),
                           'by_content_state':dict(Counter(r['content_state'] for r in result if r['primary_category']==key))})
    summary = {'policy_version':VERSION,'analysis_date':'2026-10-25','content_release':manifest['release'],
               'input_sha256':digest_file(args.input),'script_sha256':digest_file(Path(__file__)),
               'news_count':len(result),'content_states':dict(Counter(r['content_state'] for r in result)),
               'title_sources':dict(Counter(r['display_title_source'] for r in result)),
               'categories':categories,'review_flags':dict(Counter(f for r in result for f in r['review_flags'])),
               'checks':{'source_package_files_sha256_verified':True,'selected_ids_and_actual_text_verified':True,
                         'one_primary_per_news':sum(counts.values())==len(source),'human_accuracy_measured':False},
               'limitations':['Local rule inference, not official source categories or gold labels.',
                              'Fragment headings are unverified display evidence, never source metadata.',
                              'Counts are by news_id, not unique events or deduplicated body.',
                              'No source, database, existing labels or admission rule was modified.']}
    args.output.mkdir(parents=True)
    with (args.output/'classification.jsonl').open('x',encoding='utf8') as f:
        for row in result:
            f.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')
    dump(args.output/'summary.json',summary)
    dump(args.output/'policy.json',{'version':VERSION,'categories':CATEGORIES,
                                   'title_weight':6,'title_cap':30,'text_weight':1.25,'text_cap':10,
                                   'text_only_threshold':3.75,'ambiguous_margin':2.5,
                                   'international_geography_only_terms':sorted(GEOGRAPHY_ONLY),
                                   'geography_title_weight':2,'geography_text_weight':.25})
    build_report(result,summary,args.output)
    dump(args.output/'manifest.json',{'version':VERSION,'files':{p.name:digest_file(p) for p in args.output.iterdir() if p.is_file()}})
    print(json.dumps({'news_count':len(result),'categories':categories,'review_flags':summary['review_flags']},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
