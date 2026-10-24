# NewsAgent 评测链路打印图

使用 A4、横向、适合页面打印。PNG 为 3508 × 2480 像素、300 DPI；SVG 是可缩放副本，
其中文字体取决于打开它的系统，直接打印优先使用 PNG。没有生成 PDF。

## 对应窗口和来源

- `writing-evaluation-execution.png` 对应“简历润色(评测)”，来源为该窗口近期记录及
  `agent/evaluation/REPORT_PILOT.md`。链路：审核给定材料/gold → prepare 冻结 →
  Research → Outline → 分章节 Writer → Assemble → 首轮 Reviewer → 人工评分表 →
  score 确定性汇总 → 同条件候选 Prompt 运行 → compare。外部依赖是 DeepSeek 原生
  推理接口；冻结模型/Prompt、超时、调用预算、源码与输入。核心业务入口为
  `run_report_pilot`、`run_report_case`、`score_reports`；输出逐阶段产物与 JSON/Markdown。
  Reviewer 检出与误报针对自然生成稿，不是固定注错探针。TODO：人工标签确认、真实模型实验、
  独立验证集；本轮不包含搜索、自动修订或 Temporal 恢复。
- `retrieval-evaluation-execution.png` 对应“简历润色(评测) (2)”，来源为该窗口近期记录及
  `agent/evaluation/RETRIEVAL_RESUME.md`。链路：确认源新闻和事件分组 → 冻结索引/内容 →
  `HotNewsEnrichmentService.enrich` → 标题摘要 Query Embedding → 三层检索/门禁/去重/RRF →
  A 不做业务重排、B 加入 `RelatedNewsReranker` → 合并真实候选 → 独立双审/仲裁 →
  `score_retrieval_judgments.py` → pooled exp2 nDCG@5 与独立性能报告。
  外部依赖是规范正文、Embedding、实际索引及适用的生命周期台账；Top5 配置为候选 20、
  输出 5。N 仅统计有人工正例且未参与调参的独立测试查询，无答案任务另计。
  TODO：独立 test、真实 A/B 运行、正式候选池盲审与完整检索链性能测试。
- 打包时另附前一张查询解析与 LoRA SFT 图，来自 `../query-semantics-sft/`。

所有图都是执行计划。开发材料数量、离线契约检查或历史探索数据没有被改写成真实质量成绩。

## 图像生成与核验

上游：已核对的执行说明和窗口记录。转换：将阶段、条件、评分边界与 TODO 写成固定节点。
核心：`render_diagrams.py` 的 `begin/column/node/arrow/finish`，使用 Pillow 与本机中文字体。
配置：A4 横向、3508 × 2480、300 DPI，字体与布局坐标位于脚本顶部。输出：每项 PNG、SVG
及预览图；图内无企业明细、凭证或实际评测数值。生成过程不调用模型、检索服务或生产系统。

从仓库根目录重新生成：

```bash
/Users/shi/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  output/diagrams/evaluation-print-set/render_diagrams.py
```

验证：脚本测量文字宽度并阻止越界，生成后逐张检查中文、节点、箭头与排版；另核验 PNG
像素尺寸和 DPI。这是图像核验，不是重新执行业务评测。实际打印机裁切与纸张边距依用户设置。
