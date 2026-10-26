"""Versioned editorial boundaries used after local semantic classification.

Rules require explicit topic evidence. Keep both the original model prediction
and rule matches; this policy never removes an article or declares a gold label.
"""
import re

VERSION='news-editorial-boundaries-20261025-v1'


def decide(row):
    title=row['title']
    text='\n'.join(row['excerpts'])
    context=title+'\n'+text
    predicted=row['primary_category']

    def hit(pattern,where):
        match=re.search(pattern,where,re.I|re.ASCII)
        return match.group(0) if match else None

    # Securities discussion takes precedence over the sector of the company.
    term=hit(r'股票|股价|股市|A股|港股|美股|涨停|跌停|持仓|增持|减持|回购股份|买入评级|维持.{0,10}评级|目标价|解禁|股东大会|权益分派|证券研报|每股收益|投资评级',title)
    if term:
        return 'stocks','explicit_securities_topic',term
    # Esports belongs to games even when carried by a sports publisher.
    term=hit(r'\b(?:LCK|LPL|KPL|DOTA2?|CS2|BLG|T1|cvMax)\b|王者荣耀|英雄联盟|电竞|上单|打野|卡莎|无畏契约|和平精英|原神|星穹铁道|魔兽世界|梦幻西游|游戏攻略|游戏更新|新英雄|手游|端游|网游|国服|新皮肤|体验服',title)
    if not term:
        term=hit(r'\b(?:LCK|LPL|KPL)\b|王者荣耀|英雄联盟|无畏契约|魔兽世界|梦幻西游|高能英雄|角色扮演游戏',text[:650])
    if not term and re.search(r'\bAG\b',title,re.ASCII) and '虎扑' in text:
        term='AG / 虎扑'
    if term and not re.search(r'诈骗|判刑|盗窃|沉迷|未成年人保护',title):
        return 'games','explicit_game_or_esports',term
    term=hit(r'汉密尔顿|维斯塔潘|皮亚斯特里|勒克莱尔|诺里斯|阿隆索|高芙|萨巴伦卡|梅尔滕斯|兹维列夫|阿尔卡拉斯|奥斯塔彭科|斯瓦泰克|德米纳尔|张本智和|张本美和|伊藤美诚|石宇奇|安洗莹|约书亚|泰森.{0,3}富里|前锋|中锋|门将|后卫|中场|主帅|绿茵|中网|美网|温网|法网|澳网|世锦赛|世预赛|亚锦赛|冬奥|冬运会|大冬会|省运会|全运会|排位赛|杆位|\bF1\b',title)
    if term:
        return 'sports','explicit_sport_subject',term
    # Strong AI terminology in the headline identifies the article's subject.
    term=hit(r'(?<![a-z])AI(?![a-z])|人工智能|大模型|智能体|生成式|DeepSeek|ChatGPT|OpenAI|具身智能|多模态|机器学习|神经网络',title)
    if term:
        return 'ai','explicit_ai_subject',term
    term=hit(r'机器人|机械臂|光刻机|量子计算|核聚变|半导体技术|航天技术|基因编辑技术',title)
    if term and predicted in {'health','education','society','lifestyle','insufficient','tech'}:
        return 'tech','technology_subject_over_application_sector',term
    term=hit(r'微信.{0,30}(?:功能|单删|版本更新)|科技装备展|数智共治中心|网络安全漏洞|信息安全实验室|区块链|公共数据|数据安全|数据泄露|个资遭窃|帐号.{0,5}遭窃',title)
    if term:
        return 'tech','explicit_technology_product_or_system',term
    term=hit(r'iPhone|iPad|MacBook|鸿蒙|HarmonyOS|\biOS\b|\bAndroid\b|OPPO|\bvivo\b|一加.{0,8}(?:手机|发布)|折叠屏|智能手表|平板电脑|笔记本电脑|数码相机|扫地机器人',title)
    if term and predicted in {'tech','digital','insufficient'}:
        return 'digital','explicit_consumer_electronics',term
    term=hit(r'自由现金流|政策性金融|金融工具|资产证券化|消费类ABS|消费信贷|净息差|净利润|财报|财险|寿险|金融监管|经济增长|外贸出口|石油产量|原油产量|物流网',title)
    if term:
        return 'finance','explicit_financial_subject',term
    term=hit(r'新能源重卡|重卡|车企|汽车行业|新能源汽车|车型上市|新车上市|购车|智驾|自动驾驶|智能驾驶',title)
    if term and not re.search(r'事故|撞|碾压|伤亡|起火',title):
        return 'auto','explicit_vehicle_subject',term
    term=hit(r'航母|驱逐舰|战斗机|轰炸机|核潜艇|军演|阅兵|解放军|歼[-－]?[二三\d]|F[-－]?(?:22|35)|抗美援朝|抗日战争|淞沪会战|红军|国防部|俄军|乌军|以军|军援',title)
    if term:
        return 'military','explicit_military_subject',term
    # Overseas social incidents must not fall into the domestic society channel.
    term=hit(r'美国|日本|韩国|英国|法国|德国|俄罗斯|加拿大|澳大利亚|新西兰|巴西|墨西哥|阿根廷|印度|巴基斯坦|尼泊尔|泰国|缅甸|柬埔寨|菲律宾|巴拿马|北海道|东京|纽约|洛杉矶|巴黎|伦敦',title)
    if term and predicted=='society' and not re.search(r'国内|中国.{0,6}(?:进口|出口)|来华|在华|南京|上海|北京',title):
        return 'international','overseas_social_event',term
    term=hit(r'老大.{0,12}老二|夫妻相处|婚姻经营|育儿|出生顺序|亲子关系',title)
    if term and predicted in {'education','health','lifestyle','insufficient'}:
        return 'lifestyle','family_life_subject',term
    if predicted=='insufficient' and len(re.findall(r'哲学|著作|译者|思想者|文学|书写|艺术',text))>=3:
        return 'culture','substantial_philosophy_or_literature_context',hit(r'哲学|著作|译者|思想者|文学|书写|艺术',text)
    return predicted,None,None
