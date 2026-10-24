# 公开研究启发的分板块运营评测案例

版本：2026-10-14.public-research-synthetic-v1。
新闻、身份、聚合指标和参考决定均为合成，待人工复核，未投递。公开来源只支撑设计原则，不能证明虚构新闻发生。
自动评分仅检查决定、类别、理由、阻断、证据和修改字段；改稿语义须按每例人工 rubric 复核。

## 天气 · 虚构南汐降雨出行 · 观察1 · research-add7e5cfa72434f97383

split：validation；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-13T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。
- [健全突发事件信息管理体系 提高突发事件预防和应对能力](https://www.mem.gov.cn/gk/zcjd/202408/t20240827_498920.shtml)：法律制度解读，不是具体事件通报或腾讯内部制度；合成事件不据此变成真实事实。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。（fixture-research-weather-closure-authority）

候选标题：虚构南汐降雨出行
分析主张：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。
文案：虚构南汐降雨出行｜虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T09:05:00+08:00 至 2026-10-13T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定
参考改稿：无需修订

人工语义 rubric：
- 天气风险与部门行动决定区分
- 停航引用交通方、停课等待教育方
- 缺少公告不能从模型概率补决定

## 天气 · 虚构南汐降雨出行 · 观察1 · research-f11679c78b2fe5fbfda0

split：validation；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-13T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。
- [健全突发事件信息管理体系 提高突发事件预防和应对能力](https://www.mem.gov.cn/gk/zcjd/202408/t20240827_498920.shtml)：法律制度解读，不是具体事件通报或腾讯内部制度；合成事件不据此变成真实事实。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。（fixture-research-weather-closure-authority）

候选标题：虚构南汐降雨出行
分析主张：预警发布就等于全城停课停航。
文案：预警发布就等于全城停课停航。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T09:05:00+08:00 至 2026-10-13T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定
参考改稿：修订草稿须保留当前事实和来源归因：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。；天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定

人工语义 rubric：
- 天气风险与部门行动决定区分
- 停航引用交通方、停课等待教育方
- 缺少公告不能从模型概率补决定

## 天气 · 虚构南汐降雨出行 · 观察2 · research-7321aba3e456b106eafb

split：validation；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-13T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。
- [健全突发事件信息管理体系 提高突发事件预防和应对能力](https://www.mem.gov.cn/gk/zcjd/202408/t20240827_498920.shtml)：法律制度解读，不是具体事件通报或腾讯内部制度；合成事件不据此变成真实事实。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。（fixture-research-weather-closure-authority）
- 2026-10-13T13:00:00+08:00：虚构交通部门只确认两条航线取消，学校安排仍未明确。（fixture-research-weather-closure-authority）

候选标题：虚构南汐降雨出行
分析主张：两条航线停运证明所有航线和学校关闭。
文案：两条航线停运证明所有航线和学校关闭。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T13:05:00+08:00 至 2026-10-13T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定
参考改稿：修订草稿须保留当前事实和来源归因：虚构交通部门只确认两条航线取消，学校安排仍未明确。；天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定

人工语义 rubric：
- 天气风险与部门行动决定区分
- 停航引用交通方、停课等待教育方
- 缺少公告不能从模型概率补决定

## 天气 · 虚构南汐降雨出行 · 观察2 · research-a4337e992dd7e2df9eb0

split：validation；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-13T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。
- [健全突发事件信息管理体系 提高突发事件预防和应对能力](https://www.mem.gov.cn/gk/zcjd/202408/t20240827_498920.shtml)：法律制度解读，不是具体事件通报或腾讯内部制度；合成事件不据此变成真实事实。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构南汐气象台今天09:00发布本市城区暴雨黄色预警，有效至18:00；尚无教育或交通部门停课停航公告。（fixture-research-weather-closure-authority）
- 2026-10-13T13:00:00+08:00：虚构交通部门只确认两条航线取消，学校安排仍未明确。（fixture-research-weather-closure-authority）

候选标题：虚构南汐降雨出行
分析主张：虚构交通部门只确认两条航线取消，学校安排仍未明确。
文案：虚构南汐降雨出行｜虚构交通部门只确认两条航线取消，学校安排仍未明确。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T13:05:00+08:00 至 2026-10-13T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。天气风险与部门行动决定区分；停航引用交通方、停课等待教育方；缺少公告不能从模型概率补决定
参考改稿：无需修订

人工语义 rubric：
- 天气风险与部门行动决定区分
- 停航引用交通方、停课等待教育方
- 缺少公告不能从模型概率补决定

## 天气 · 虚构榆川高温预警 · 观察1 · research-51f960d65b16e27dae9a

split：test；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-14T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。（fixture-research-weather-expired-service）

候选标题：虚构榆川高温预警
分析主张：因旧图热度高继续按当前预警推送。
文案：因旧图热度高继续按当前预警推送。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T09:05:00+08:00 至 2026-10-14T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading, content_expired；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知
参考改稿：修订草稿须保留当前事实和来源归因：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。；禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知

人工语义 rubric：
- 禁止传播过时预警作当前风险
- 新旧日期和服务安排分别核对
- 更正当前事实，未授权用户不能通知

## 天气 · 虚构榆川高温预警 · 观察1 · research-9a0a8f364cfdbbaa5723

split：test；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-14T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。（fixture-research-weather-expired-service）

候选标题：虚构榆川高温预警
分析主张：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。
文案：虚构榆川高温预警｜虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T09:05:00+08:00 至 2026-10-14T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知
参考改稿：无需修订

人工语义 rubric：
- 禁止传播过时预警作当前风险
- 新旧日期和服务安排分别核对
- 更正当前事实，未授权用户不能通知

## 天气 · 虚构榆川高温预警 · 观察2 · research-2476c4f6febe074fdb79

split：test；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-14T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。（fixture-research-weather-expired-service）
- 2026-10-14T13:00:00+08:00：虚构榆川气象台今天13:00发布仅东区的高温橙色预警，有效至18:00；新服务点15:00开放，与昨日已解除记录并存。（fixture-research-weather-expired-service）

候选标题：虚构榆川高温预警
分析主张：虚构榆川气象台今天13:00发布仅东区的高温橙色预警，有效至18:00；新服务点15:00开放，与昨日已解除记录并存。
文案：虚构榆川高温预警｜虚构榆川气象台今天13:00发布仅东区的高温橙色预警，有效至18:00；新服务点15:00开放，与昨日已解除记录并存。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T13:05:00+08:00 至 2026-10-14T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知
参考改稿：无需修订

人工语义 rubric：
- 禁止传播过时预警作当前风险
- 新旧日期和服务安排分别核对
- 更正当前事实，未授权用户不能通知

## 天气 · 虚构榆川高温预警 · 观察2 · research-8066e30ae630250e2704

split：test；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-14T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构榆川气象台今天09:00确认昨天全区高温黄色预警已解除，今天公众服务点安排另有正式公告。（fixture-research-weather-expired-service）
- 2026-10-14T13:00:00+08:00：虚构榆川气象台今天13:00发布仅东区的高温橙色预警，有效至18:00；新服务点15:00开放，与昨日已解除记录并存。（fixture-research-weather-expired-service）

候选标题：虚构榆川高温预警
分析主张：用昨天服务点时间替代今天新公告。
文案：用昨天服务点时间替代今天新公告。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T13:05:00+08:00 至 2026-10-14T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading, content_expired；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知
参考改稿：修订草稿须保留当前事实和来源归因：虚构榆川气象台今天13:00发布仅东区的高温橙色预警，有效至18:00；新服务点15:00开放，与昨日已解除记录并存。；禁止传播过时预警作当前风险；新旧日期和服务安排分别核对；更正当前事实，未授权用户不能通知

人工语义 rubric：
- 禁止传播过时预警作当前风险
- 新旧日期和服务安排分别核对
- 更正当前事实，未授权用户不能通知

## 天气 · 虚构芦湾大风预警 · 观察1 · research-27914a0c47fd292c2e82

split：development；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-12T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。（fixture-research-weather-warning-update）

候选标题：虚构芦湾大风预警
分析主张：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。
文案：虚构芦湾大风预警｜虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T09:05:00+08:00 至 2026-10-12T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警
参考改稿：无需修订

人工语义 rubric：
- 台站、时间、区域与级别一并保留
- 解除和延长分区说明
- 不能扩大或改写官方预警

## 天气 · 虚构芦湾大风预警 · 观察1 · research-327678a6dea7f131ffba

split：development；模拟运营：模拟天气运营员；观察情景：观察1。
观察时间：2026-10-12T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。（fixture-research-weather-warning-update）

候选标题：虚构芦湾大风预警
分析主张：全省均受同级预警，推给全国。
文案：全省均受同级预警，推给全国。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T09:05:00+08:00 至 2026-10-12T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警
参考改稿：修订草稿须保留当前事实和来源归因：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。；台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警

人工语义 rubric：
- 台站、时间、区域与级别一并保留
- 解除和延长分区说明
- 不能扩大或改写官方预警

## 天气 · 虚构芦湾大风预警 · 观察2 · research-04981815abda53b21539

split：development；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-12T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。（fixture-research-weather-warning-update）
- 2026-10-12T13:00:00+08:00：虚构芦湾气象台今天13:00解除甲县预警，乙县蓝色预警延长至18:00；旧截图不再代表两县当前状态。（fixture-research-weather-warning-update）

候选标题：虚构芦湾大风预警
分析主张：继续转发旧全域预警截图，不展示解除。
文案：继续转发旧全域预警截图，不展示解除。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T13:05:00+08:00 至 2026-10-12T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading, content_expired；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警
参考改稿：修订草稿须保留当前事实和来源归因：虚构芦湾气象台今天13:00解除甲县预警，乙县蓝色预警延长至18:00；旧截图不再代表两县当前状态。；台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警

人工语义 rubric：
- 台站、时间、区域与级别一并保留
- 解除和延长分区说明
- 不能扩大或改写官方预警

## 天气 · 虚构芦湾大风预警 · 观察2 · research-ab836132ff6b7be52dba

split：development；模拟运营：模拟天气运营员；观察情景：观察2。
观察时间：2026-10-12T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [气象灾害预警信号发布与传播管理办法](https://www.cma.gov.cn/gzk/202005/W020220628619402421665.pdf)：日期为PDF原令公布日；不得让模型制造官方预警或将气象预警当成交通、学校停运决定。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构芦湾气象台今天09:00发布沿海甲乙两县大风蓝色预警，有效至15:00，其他地区不在范围。（fixture-research-weather-warning-update）
- 2026-10-12T13:00:00+08:00：虚构芦湾气象台今天13:00解除甲县预警，乙县蓝色预警延长至18:00；旧截图不再代表两县当前状态。（fixture-research-weather-warning-update）

候选标题：虚构芦湾大风预警
分析主张：虚构芦湾气象台今天13:00解除甲县预警，乙县蓝色预警延长至18:00；旧截图不再代表两县当前状态。
文案：虚构芦湾大风预警｜虚构芦湾气象台今天13:00解除甲县预警，乙县蓝色预警延长至18:00；旧截图不再代表两县当前状态。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T13:05:00+08:00 至 2026-10-12T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。台站、时间、区域与级别一并保留；解除和延长分区说明；不能扩大或改写官方预警
参考改稿：无需修订

人工语义 rubric：
- 台站、时间、区域与级别一并保留
- 解除和延长分区说明
- 不能扩大或改写官方预警
