# 本次可填写的简历数字

## 检索质量
N=30条开发查询（不能称独立测试）；共同池454项人工判断。
nDCG@5：0.5633509453→0.9501287224，简历可保留0.563→0.950。
Precision@5：37.33%→56.00%；29条改善、1条持平。
对照是同模型、同候选预算的本地等权RRF与全局余弦融合，不含主体/事件业务重排。

推荐表述：
“优化热/温/冷跨层融合排序，在30条开发查询、454项单人相关性判断上，
全局余弦融合相较等权RRF基线，nDCG@5由0.563提升至0.950，Precision@5由37.3%提升至56.0%。”

新增154项全部标为不相关，按用户标签原样计分；建议人工抽查，不自动改标。
该质量实验采用冻结7/90日分层；18.25万文档/91.25万向量的7/30/365日合成容量及218ms
属于另一个本地索引测试，不将它们说成同一真实新闻实验。

## 写作gold审核
用户已明确确认两组标准答案审核完成，gold-review.json已记为approved。
可写2个开发事件组、2项完整报告任务、12项必需事实、6项数值检查。
本次仅记录审核状态，写作错误稿比例、真实人工修订分钟及LoRA收益仍待真实实验，
不能从gold审核本身推导。未改写任何事实、公式、容差或稿件判断。

## 调用链、配置、依赖、验证和TODO
检索：原300标签+新增154标签→SQLite一致性备份→compare_annual_retrieval.score
→calculate_comparison→results.json/report.md→verify_metrics独立重算DCG/IDCG/Precision。
验证入口：python -B output/resume-quality-20261028/verified-results-v1/verify_metrics.py；
verification.json已保存，重复复核需改为新的输出文件，避免覆盖冻结证据。

审核：本聊天用户明确确认→gold顶层status/reviewer/reviewed_at→validate_gold
核对两例输入哈希和数值契约→后续run_report_pilot prepare冻结。
确认时间为记录批准时间，reviewer=chat_user，不推断用户的个人身份。
原始待审文件备份和确认来源在../gold-approval-v1/；两例审核契约校验已实际通过。
脚本及审核元数据只用Python标准库；score CLI复用现有应用环境，不依赖模型/企业服务。
配置沿用原冻结RRF参数、候选预算和数据；不更改生产Prompt或分层策略。
TODO：真实报告A/B生成、人工逐稿评分与计时、LoRA训练adapter及同条件对照、独立检索test。
