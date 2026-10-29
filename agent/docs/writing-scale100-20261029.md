# 写作评测扩充到 100 项：本地执行协议（2026-10-29）

本轮目标是扩大真实缓存材料上的写作评测覆盖。100 项是本次约定的任务规模，
不是行业统一上线门槛，也不等于已有 100 个人工审核样本。

## 样本与分母

- 上游：`var/news-events-annual-20261027/retrieval-lab-v1/news.jsonl`。
  545 篇公开缓存报道、77 个发现阶段的候选事件组；正文和事件边界均未经独立核真。
- 资格：每组至少两篇不同来源 URL 的完整缓存正文，每篇 200–5000 Unicode 字符；
  选中的两篇正文合计不超过 10000 字符。不为凑数复制新闻、切碎一篇成多个事件或截断正文。
- 冻结目标：50 个 evaluation 候选组 × 2 类完整短稿任务 = 100 项；
  另 9 个 development 候选组 × 2 = 18 项。两个 split 不共享事件组。
- 每项 baseline / candidate 两稿，共计划 200 个 evaluation 请求。
  200 篇不算 200 个独立事件；同组两个任务存在相关性。
- 类别来自发现阶段的规则标签，不能当作人工金标。实际覆盖与体育类别偏斜须随数据清单报告。
- 新金标全部 draft；旧 2 例的人工审核不会转移到新题。
- 计时子集在生成前预登记 10 项配对任务，共 20 篇，不能根据结果挑选容易修改的稿件。
- 现有 3 条财务正则在年度语料未匹配，不能把未覆盖记成“数字核验通过”。
  无此类计算项标为 unsupported / not_applicable，语义适用性仍需人判断。

## 对照和执行范围

本轮为 `generation_only_exploratory`，比较公开证据写作 Prompt：
baseline 是正常的新闻写作要求，candidate 增加来源、时间、单位、计划与已发生的证据约束。
双方使用相同来源、Hunyuan 7B NF4 模型修订、temperature=0 和 1024 输出 Token 上限。
不得刻意削弱基线。私有标准答案和审核标签不进入模型请求。

直接成稿生成与项目原生 Research → Outline → Writer → Reviewer 分开报告。
先前本地原生链路 4/4 次在 Research 阶段失败，该事实不会由批量直写成功覆盖。

本地服务只监听 127.0.0.1:8019，profile=batch-writing-v1；
模型修订为 6fd6ecb05e76589bc43b79f49e3619445c6b4593。
最多 4 请求一起推理；长输入按 2 条拆分，OOM 回退须记录尝试次数。
持久请求 ID + 请求体哈希绑定服务缓存。已启动但结果未知的请求只查询恢复，
不自动重新提交，不以重试隐藏失败。

每个批次包含两个任务各自 A/B 四条。单次 decode 的 max_time=240 秒，
每请求 300 秒仅是软预期而非强制 timeout；批 HTTP timeout=900 秒。
OOM 回退可能包含多次 decode，真实次数单独记录，不能声称每请求最多 300 秒。
每轮软墙时 7200 秒，到时停止新派发，已派发请求继续落盘；断点续跑只派发从未提交的请求。

## 评分和计时

生成完成率：以全部 200 个计划请求为固定分母，报告 complete / failed / pending / not_run；
finish_reason=length 或错误不能记成完整稿件。模型耗时不能替代编辑耗时。

质量审核：先按事件核对来源、事件边界和任务适用性，再填写必需事实、数字与禁止断言。
每稿检查事实主体、时间、数字单位、来源归属、计划状态、证据支持与可交付性。
事实错误稿比例以每臂全部 100 个计划任务为范围；生成失败另报，
完整性门禁失败不能在算比例时悄悄删掉。未审核保留 null，不按零错计。

人工计时：阅读证据、核对和修改都计入有效时间，中断显式暂停；统一完成标准。
保留编辑者、顺序、开始/结束/暂停、原稿/终稿哈希与人工签名。
自动生成、机器评分或助手修改的时间不能填入人工分钟数。
配对缺失或标准未确认时，不输出简历中的 C / D。

最终误差与区间按事件组聚合，不把同组两任务当独立抽样。
即使 100 个真正独立且具有代表性的样本均零错，按二项分布零失败公式
1 - 0.05^(1/100)，单侧 95% 错误率上界仍约 2.95%。
本轮仅 50 个候选事件组，不满足该独立 100 例假设。

## 上线判断与简历

100 项能扩大本地评测覆盖。生产上线还需代表性与高风险样本、
企业模型/数据的完整链路、权限隔离、失败恢复、负载与监控验收及既有人审节点。
本轮不自动发布生产模型或修改线上 Prompt。

数据集真实落盘后可写“构建覆盖 50 个候选事件组、100 项任务的写作评测集”；
人工复核事件边界完成后才去掉“候选”。
A/B 生成完成不直接给出错误率收益，更不能给出不存在的人工修订分钟数或 LoRA 收益。

## 可复现调用链

上游真实缓存 news.jsonl → prepare_writing_scale 分组、过滤、去重、冻结 split 和来源哈希
→ inputs.jsonl + source-manifest + private draft gold + human-review.md + 预登记计时子集
→ run_writing_batch prepare 冻结公开输入、Prompt、模型和请求 ID
→ run / continue 通过 8019 HTTP 调用 serve_writing_batch_eval
→ Runtime 离线读取本地 BF16 权重，bitsandbytes NF4 双重量化、BF16 计算
→ 持久服务缓存 + 每稿 response / draft / metadata
→ audit_writing_batch_outputs 记录格式/引用信号
→ prepare_writing_scale_review 质量审核表与配对人工计时包
→ manual_editing_server 真人计时 → editing_time 审核后汇总。

依赖：WSL、CUDA、Hunyuan7B 本地固定权重、GPU 环境 torch/transformers/bitsandbytes；
数据准备和 HTTP 编排使用 Python 标准库。完整应用环境记录在
deploy/requirements.writing-eval.lock.txt，GPU 依赖来自现有隔离环境。
新协议不读取 .env，不调用企业模型 API。

测试入口：agent/tests/test_writing_scale_dataset.py、
test_writing_batch_runner.py、test_writing_batch_service.py，
以及复用计时组件的 test_manual_editing_server.py 和 test_editing_time.py。
工程测试通过仅证明工具行为，不等于模型质量合格。

剩余 TODO：100 项金标与事件边界复核、200 稿语义评分、
10 对真实人工计时、原生 RWR 失败修复与复测、企业环境验收。
最终数值以冻结运行目录内的执行与审核结果为准。

参考：
- [Google：ML Test Score](https://research.google/pubs/the-ml-test-score-a-rubric-for-ml-production-readiness-and-technical-debt-reduction/)
- [NIST：比例置信区间与精确二项法](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)


## 本轮已执行结果与继续入口

2026-10-29已完成正式200/200篇生成；模型服务8019已结束释放GPU。
结果见 output/resume-quality-20261028/scale100-results-v1/report.md 和 results.json。
规则审计显示显式ID/URL引用匹配5/100→0/100，篇幅250–350字符4/100→8/100，
不支持候选版本已达到上线质量的结论；事实金标、语义评分与人工计时仍待完成。

本机使用 http://127.0.0.1:18882/ 做预登记10对20稿真人计时。
8882属于Windows系统排除端口，因此使用18882。现服务由Windows标准库Python运行，
直接读取同一工作区任务/状态；未开始计时，不改原8881两例服务。
在Windows PowerShell的agent目录，或端口可访问的WSL内，可运行：
```bash
python -m evaluation.manual_editing_server \
  --package ../output/resume-quality-20261028/scale100-review-v1/editing-tasks.json \
  --state-dir ../output/resume-quality-20261028/scale100-review-v1/manual-editing-state \
  --port 18882
```
PowerShell可将该命令写成一行。不要同时启动两个进程共享同一state目录。
新电脑首次启动使用新的空state目录；需延续计时则另行完整迁移已停止服务的state，
Git仅同步固定任务包，不上传实时人工草稿/事件。

在其他电脑拉取后，从仓库根目录只读核验（无需模型、GPU或旧绝对路径）：
```bash
python agent/evaluation/verify_writing_run_portable.py \
  --run-dir output/resume-quality-20261028/scale100-run-v1 \
  --dataset output/resume-quality-20261028/scale100-v1
```

原始run中的绝对路径仅作来源记录；被冻结的旧runner直接status可能仍要求原目录，
所以迁移核验使用独立portable入口，不修改原plan封印。
重新生成需要在本机用prepare另建run，不重放带start但无结果的旧请求。
Git包含选中的完整118篇来源及200篇稿件，原545篇var缓存和模型权重不上传。

agent目录下重新导出审核包（--out须新目录）：
```bash
python -m evaluation.prepare_writing_scale_review \
  --run-dir ../output/resume-quality-20261028/scale100-run-v1 \
  --dataset ../output/resume-quality-20261028/scale100-v1 \
  --out ../output/resume-quality-20261028/scale100-review-v2
```
重新规则审计使用evaluation.audit_writing_batch_outputs，同样提供--run-dir、--dataset和新的--out。
数据准备只运行evaluation.prepare_writing_scale；需原始545篇本地var缓存。
批次复现使用evaluation.run_writing_batch prepare/run/continue，参数见各--help；
实际启动GPU服务见Hunyuan7B/scripts/serve_writing_batch_eval.py。

本轮94项工程检查通过（数据11、runner16、service13、review7、输出信号10、portable13、计时页12、计时统计12）。
portable验证器只映射显式本机数据集路径，校验原封印、来源、请求、响应、完整health与原稿SHA，
不调用模型、不改状态。结果/代码设置精确Git -text，避免Windows CRLF转换破坏原始字节哈希。

当前未完成TODO以结果报告为准：金标和事件边界复核、200稿语义评分、10对真实计时、
独立开发迭代与新测试、原生RWR和企业服务验收。已完成的200稿不需要重跑。
