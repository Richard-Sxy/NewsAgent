# 100项写作 A/B 本地评测结果（2026-10-29）

已用本地 Hunyuan-7B NF4 实际生成 100项任务的 A/B 两稿，共200篇；
200/200完成，失败/未知/未运行均为0，客户端提交和GPU生成均200次，没有用隐性重试补成功。
正式生成耗时约41分52秒，Token共489915。4次开发集smoke单列，不计入200分母。

**完成生成不等于事实全部正确，更不等于项目上线验收。**

## 数据与独立性

- 545篇原始公开缓存中有77个候选事件组；59组满足双正文要求。
- evaluation为50个候选事件组×两类完整写作任务=100项，development另9组18项。
- 选中118篇完整缓存正文，未截断；100项任务覆盖10个规则推断类别。
- 实际分类组数、每篇来源、正文SHA及去重审计见 ../scale100-v1/manifest.json 和 source-manifest.json。
- 事件边界、类别、来源真实性及标准答案仍待人工复核；200篇不能当200个独立事件。
- 旧三条财务正则在新增任务中均不覆盖，不将其空结果算成数值检查通过。

## 预先固定规则的检查结果

| 信号（每臂100篇） | 基线 | 候选 |
| --- | ---: | ---: |
| 生成完成且正常stop | 100 | 100 |
| 显式已知来源ID或完整URL匹配 | 5 | 0 |
| 残留(@ref)标记 | 2 | 0 |
| 固定算法下符合250–350字符 | 4 | 8 |

算法与每篇SHA、信号见 ../scale100-audit-v1/audit.md / audit.json。
字数按公开算法计Unicode码点，含标题，去URL、来源ID、Markdown结构符和空白。
机构名称或自然语言来源说明不计为显式ID/URL匹配；
匹配引用仍不能证明具体陈述获得该来源支持。

该候选Prompt在显式引用方面没有改善，篇幅服从性也较差。
不把该表改写为事实错误率、报告可用率、人工修订收益或生产发布依据。
下一版应在独立开发集完善结构化引用与篇幅校验，再另冻版本和测试；
不能看完本轮结果后改规则，仍将本轮当未见测试集。

## 人工下一步

1. ../scale100-v1/human-review.md：核对50个evaluation候选事件组及对应标准答案；
   私有结构表位于 private/gold-review.draft.json，全部status=draft、批准字段false/null。
2. ../scale100-review-v1/review-template.json：200条稿件评分记录，所有语义评分均为null。
   先确认必需事实、数字适用性，再依据原文逐稿标注并引用错误证据。
3. 预登记的10对20篇已在 http://127.0.0.1:18882/ 启动真人计时页面。
   20项均未自动开始；阅读证据、核对与修订计时，中断暂停。
   Windows保留8882–8981端口，因此本机使用18882，没有调整系统网络设置。
4. 统一标准完成并人工审核后，才计算事实错误稿比例和每篇修订分钟数。

## 目前能用于简历的内容

可写“构建覆盖50个候选事件组、100项任务的写作评测集，
完成200篇A/B稿件的本地真实模型生成与格式/引用规则检查，保留来源、模型、请求和稿件哈希支持复查”。
人工确认事件边界后才去掉“候选”。

原简历写作错误率A/B、人工修订C/D仍未测出。
本轮也不产生新的检索nDCG、交互查询P95或LoRA收益。
本地模型为Hunyuan NF4，不是企业Hy4，也没有训练adapter。
直接成稿比较与先前原生Research链路失败分开保留。

## 调用链、环境和复现

年度缓存 → prepare_writing_scale.load_sources/group_sources/choose_pair/task_input
→ 冻结公共inputs、来源manifest、draft gold与editing-subset
→ run_writing_batch.prepare冻结两臂Prompt、模型与200请求
→ run/continue以HTTP调用8019批量服务
→ RequestCache + TorchBatchBackend + 既有Runtime读取校验的本地权重
→ NF4/BF16批量生成 → immutable result/response/draft/metadata
→ verify_writing_run_portable只读验证 → audit_writing_batch_outputs固定规则
→ prepare_writing_scale_review待审评分表和配对计时包
→ manual_editing_server真人计时 → editing_time人工审核后统计。

完整协议、环境、CLI及测试见 ../../../agent/docs/writing-scale100-20261029.md。
工具工程测试94项通过，不作为94个人工质量样本。
外部依赖仅本地CUDA/torch/transformers/bitsandbytes及固定权重；
编排/审计/人工计时使用标准库，不读取.env或调用企业接口。
8019模型服务已在200稿完成后停止，持久缓存保留；18882计时页继续提供服务。
TODO：新金标/事件边界人工审核、200稿语义评分、10对真实编辑计时、原生RWR修复与企业端验收。
