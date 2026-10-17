# 公开研究启发的分板块运营评测案例

版本：2026-10-14.public-research-synthetic-v1。
新闻、身份、聚合指标和参考决定均为合成，待人工复核，未投递。公开来源只支撑设计原则，不能证明虚构新闻发生。
自动评分仅检查决定、类别、理由、阻断、证据和修改字段；改稿语义须按每例人工 rubric 复核。

## 健康 · 虚构汀兰咨询服务 · 观察1 · research-819616c860f1c653c8b3

split：test；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-14T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。（fixture-research-health-consulting-service）

候选标题：虚构汀兰咨询服务
分析主张：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。
文案：虚构汀兰咨询服务｜虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": ["fixture-registered-community-members"], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T09:05:00+08:00 至 2026-10-14T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论
参考改稿：无需修订

人工语义 rubric：
- 咨询、筛查、诊断、治疗准确区分
- 受众按社区登记条件匹配
- 时间增量不能变成疗效结论

## 健康 · 虚构汀兰咨询服务 · 观察1 · research-d983ec94824484be267a

split：test；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-14T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。（fixture-research-health-consulting-service）

候选标题：虚构汀兰咨询服务
分析主张：免费咨询等于确诊治疗，对全城推广。
文案：免费咨询等于确诊治疗，对全城推广。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": ["fixture-registered-community-members"], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T09:05:00+08:00 至 2026-10-14T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论
参考改稿：修订草稿须保留当前事实和来源归因：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。；咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论

人工语义 rubric：
- 咨询、筛查、诊断、治疗准确区分
- 受众按社区登记条件匹配
- 时间增量不能变成疗效结论

## 健康 · 虚构汀兰咨询服务 · 观察2 · research-22cd6785c56870e3a9d0

split：test；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-14T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。（fixture-research-health-consulting-service）
- 2026-10-14T13:00:00+08:00：虚构中心增加无障碍咨询时段，仍不提供诊疗服务，未扩大登记范围。（fixture-research-health-consulting-service）

候选标题：虚构汀兰咨询服务
分析主张：新增时段证明可治愈，所有人都能参加。
文案：新增时段证明可治愈，所有人都能参加。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": ["fixture-registered-community-members"], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T13:05:00+08:00 至 2026-10-14T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论
参考改稿：修订草稿须保留当前事实和来源归因：虚构中心增加无障碍咨询时段，仍不提供诊疗服务，未扩大登记范围。；咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论

人工语义 rubric：
- 咨询、筛查、诊断、治疗准确区分
- 受众按社区登记条件匹配
- 时间增量不能变成疗效结论

## 健康 · 虚构汀兰咨询服务 · 观察2 · research-772acbcf71965cf82ccf

split：test；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-14T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-14T09:00:00+08:00：虚构社区公布健康咨询预约，不提供诊断或治疗，只有本社区登记人群可预约。（fixture-research-health-consulting-service）
- 2026-10-14T13:00:00+08:00：虚构中心增加无障碍咨询时段，仍不提供诊疗服务，未扩大登记范围。（fixture-research-health-consulting-service）

候选标题：虚构汀兰咨询服务
分析主张：虚构中心增加无障碍咨询时段，仍不提供诊疗服务，未扩大登记范围。
文案：虚构汀兰咨询服务｜虚构中心增加无障碍咨询时段，仍不提供诊疗服务，未扩大登记范围。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": ["fixture-registered-community-members"], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-14T13:05:00+08:00 至 2026-10-14T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。咨询、筛查、诊断、治疗准确区分；受众按社区登记条件匹配；时间增量不能变成疗效结论
参考改稿：无需修订

人工语义 rubric：
- 咨询、筛查、诊断、治疗准确区分
- 受众按社区登记条件匹配
- 时间增量不能变成疗效结论

## 健康 · 虚构岸芷健康观察 · 观察1 · research-196eb30e1e61a9fd4ce3

split：development；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-12T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [10 best practice guidelines for reporting science & health stories](https://www.sciencemediacentre.org/wp-content/uploads/2012/09/10-best-practice-guidelines-for-science-and-health-reporting.pdf)：外部专业机构建议，不是腾讯制度或中国法规；PDF未标确切发布日期，不按URL推定。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。（fixture-research-health-observational-study）

候选标题：虚构岸芷健康观察
分析主张：某饮食已经证实可治愈疾病，停药即可。
文案：某饮食已经证实可治愈疾病，停药即可。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T09:05:00+08:00 至 2026-10-12T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证
参考改稿：修订草稿须保留当前事实和来源归因：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。；研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证

人工语义 rubric：
- 研究设计、样本与局限不能删除
- 相关不等于因果或治愈
- 不能提供停药指令或效果保证

## 健康 · 虚构岸芷健康观察 · 观察1 · research-1e92ccc7c0e652bed63b

split：development；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-12T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [10 best practice guidelines for reporting science & health stories](https://www.sciencemediacentre.org/wp-content/uploads/2012/09/10-best-practice-guidelines-for-science-and-health-reporting.pdf)：外部专业机构建议，不是腾讯制度或中国法规；PDF未标确切发布日期，不按URL推定。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。（fixture-research-health-observational-study）

候选标题：虚构岸芷健康观察
分析主张：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。
文案：虚构岸芷健康观察｜虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T09:05:00+08:00 至 2026-10-12T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证
参考改稿：无需修订

人工语义 rubric：
- 研究设计、样本与局限不能删除
- 相关不等于因果或治愈
- 不能提供停药指令或效果保证

## 健康 · 虚构岸芷健康观察 · 观察2 · research-3e179e769d68e1c70197

split：development；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-12T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [10 best practice guidelines for reporting science & health stories](https://www.sciencemediacentre.org/wp-content/uploads/2012/09/10-best-practice-guidelines-for-science-and-health-reporting.pdf)：外部专业机构建议，不是腾讯制度或中国法规；PDF未标确切发布日期，不按URL推定。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。（fixture-research-health-observational-study）
- 2026-10-12T13:00:00+08:00：虚构研究补充混杂因素和随访限制，结果仍不足以证明因果或临床疗效。（fixture-research-health-observational-study）

候选标题：虚构岸芷健康观察
分析主张：虚构研究补充混杂因素和随访限制，结果仍不足以证明因果或临床疗效。
文案：虚构岸芷健康观察｜虚构研究补充混杂因素和随访限制，结果仍不足以证明因果或临床疗效。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T13:05:00+08:00 至 2026-10-12T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证
参考改稿：无需修订

人工语义 rubric：
- 研究设计、样本与局限不能删除
- 相关不等于因果或治愈
- 不能提供停药指令或效果保证

## 健康 · 虚构岸芷健康观察 · 观察2 · research-74a0c8f0424e3a5a21d4

split：development；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-12T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [10 best practice guidelines for reporting science & health stories](https://www.sciencemediacentre.org/wp-content/uploads/2012/09/10-best-practice-guidelines-for-science-and-health-reporting.pdf)：外部专业机构建议，不是腾讯制度或中国法规；PDF未标确切发布日期，不按URL推定。
- [腾讯医典内容营销](https://healthcare.tencent.com/production/17)：医典公开产品能力，不证明NewsAgent已接入，也不推广为腾讯新闻所有频道的统一审核制度。

截至观察时点的合成事实：
- 2026-10-12T09:00:00+08:00：虚构研究为小样本观察性研究，仅报告饮食与某指标相关，未测试治疗有效性。（fixture-research-health-observational-study）
- 2026-10-12T13:00:00+08:00：虚构研究补充混杂因素和随访限制，结果仍不足以证明因果或临床疗效。（fixture-research-health-observational-study）

候选标题：虚构岸芷健康观察
分析主张：补充随访说明所有人都能获得疗效。
文案：补充随访说明所有人都能获得疗效。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-12T13:05:00+08:00 至 2026-10-12T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：corrected；可接受决定：corrected；分类：analysis_error。
必需理由：fact_or_attribution_error, copy_misleading；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：copy。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证
参考改稿：修订草稿须保留当前事实和来源归因：虚构研究补充混杂因素和随访限制，结果仍不足以证明因果或临床疗效。；研究设计、样本与局限不能删除；相关不等于因果或治愈；不能提供停药指令或效果保证

人工语义 rubric：
- 研究设计、样本与局限不能删除
- 相关不等于因果或治愈
- 不能提供停药指令或效果保证

## 健康 · 虚构竹影科普短视频 · 观察1 · research-54e8fd1fc827874eae81

split：validation；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-13T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。（fixture-research-health-professional-ad）

候选标题：虚构竹影科普短视频
分析主张：账号自称医生即足以推荐商品治病。
文案：账号自称医生即足以推荐商品治病。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T09:05:00+08:00 至 2026-10-13T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：rejected；可接受决定：rejected；分类：analysis_error。
必需理由：insufficient_evidence, fact_or_attribution_error；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。资质核验不代替疗效证据；商业利益显著披露；合格替代是提供有出处的科普知识
参考改稿：无需修订

人工语义 rubric：
- 资质核验不代替疗效证据
- 商业利益显著披露
- 合格替代是提供有出处的科普知识

## 健康 · 虚构竹影科普短视频 · 观察1 · research-b2cc8c814e0c0900d0d6

split：validation；模拟运营：模拟健康运营员；观察情景：观察1。
观察时间：2026-10-13T09:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。（fixture-research-health-professional-ad）

候选标题：虚构竹影科普短视频
分析主张：虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。
文案：虚构竹影科普短视频｜虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T09:05:00+08:00 至 2026-10-13T09:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：deferred；可接受决定：deferred, rejected；分类：strategy_judgment。
必需理由：insufficient_evidence；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。资质核验不代替疗效证据；商业利益显著披露；合格替代是提供有出处的科普知识
参考改稿：无需修订

人工语义 rubric：
- 资质核验不代替疗效证据
- 商业利益显著披露
- 合格替代是提供有出处的科普知识

## 健康 · 虚构竹影科普短视频 · 观察2 · research-e919f89894bcc4f05f51

split：validation；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-13T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。（fixture-research-health-professional-ad）
- 2026-10-13T13:00:00+08:00：虚构平台核验专业身份，但商品推广仍存在，科普事实并不能证明商品疗效。（fixture-research-health-professional-ad）

候选标题：虚构竹影科普短视频
分析主张：虚构平台核验专业身份，但商品推广仍存在，科普事实并不能证明商品疗效。
文案：虚构竹影科普短视频｜虚构平台核验专业身份，但商品推广仍存在，科普事实并不能证明商品疗效。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T13:05:00+08:00 至 2026-10-13T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：accepted；可接受决定：accepted；分类：positive_example。
必需理由：analysis_supported, useful_recommendation；可选理由：无。
原候选需阻断：False；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。资质核验不代替疗效证据；商业利益显著披露；合格替代是提供有出处的科普知识
参考改稿：无需修订

人工语义 rubric：
- 资质核验不代替疗效证据
- 商业利益显著披露
- 合格替代是提供有出处的科普知识

## 健康 · 虚构竹影科普短视频 · 观察2 · research-ef380808bb293f49792c

split：validation；模拟运营：模拟健康运营员；观察情景：观察2。
观察时间：2026-10-13T13:00:00+08:00。

设计依据（与合成新闻事实分开）：
- [腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/)：仅支持公开产品定位；不含内部板块划分、频控阈值、绩效指标和审批制度。
- [腾讯新闻杨宣：以精品资讯的技术方程重塑内容价值](https://cloud.tencent.com/developer/news/972576)：历史产品方向参考，不证明2026算法、当前业务参数或收益；本文建议不复制其用户画像技术。
- [关于规范“自媒体”医疗科普行为的通知](https://www.cac.gov.cn/2025-08/01/c_1755764425442686.htm)：面向医疗自媒体的外部规范，非腾讯内部制度；本包不提供诊疗建议。

截至观察时点的合成事实：
- 2026-10-13T09:00:00+08:00：虚构视频账号专业资质尚未核验，附商品购买入口，疗效说法缺研究原文。（fixture-research-health-professional-ad）
- 2026-10-13T13:00:00+08:00：虚构平台核验专业身份，但商品推广仍存在，科普事实并不能证明商品疗效。（fixture-research-health-professional-ad）

候选标题：虚构竹影科普短视频
分析主张：已认证等于商品有效，隐藏商业推广。
文案：已认证等于商品有效，隐藏商业推广。
人群：{"scope": "targeted", "region_codes": ["fixture-affected-region"], "interest_tags": [], "subscription_topics": [], "user_segments": [], "active_within_days": null, "notification_enabled": true}；渠道：app_notification。
发送窗：2026-10-13T13:05:00+08:00 至 2026-10-13T13:55:00+08:00。
条件：{"frequency_budget": "available", "subscription": "not_required", "consent": "allowed", "quiet_hours": false}

参考决定：rejected；可接受决定：rejected；分类：analysis_error。
必需理由：insufficient_evidence, fact_or_attribution_error；可选理由：risk_requires_escalation。
原候选需阻断：True；修改字段：无。
参考说明：公开资料只作设计依据；当前新闻与指标均为合成。资质核验不代替疗效证据；商业利益显著披露；合格替代是提供有出处的科普知识
参考改稿：无需修订

人工语义 rubric：
- 资质核验不代替疗效证据
- 商业利益显著披露
- 合格替代是提供有出处的科普知识
