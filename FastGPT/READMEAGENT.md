目前我想要将Agent的核心链路内容完善一下

一、目前需要创建一条新的后端Agent链路，和FastGPT的基础链路不相同，架构设计参考如下：

1.智能体业务链路：研究 -> 写作 -> 审核 -> 返工
2.后端状态链路： 任务创建 -> checkpoint -> 恢复 -> 完成
3.前端交互链路： 任务配置 -> 进度展示 -> 人工审核 -> 结果导出

二、如何组织这三个 Agent：

1.研究Agent
工作流开始 -> 参数校验 -> Query拆解 -> 并行执行(原文知识库检索、QA知识库检索、可选外部搜索) -> Rerank -> 事实抽取 -> 证据冲突检测 -> JSON输出
输出结果必须是结构化研究包：

{
    "facts": [],
    "timeline": [],
    "conflicts": [],
    "evidence_gaps": [],
    "suggested_angles": []
}

2.写作Agent
工作流开始 -> 生成提纲 -> 生成章节计划 -> 逐节写作 -> 全文合成 -> 全文一致性修订 -> 输出稿件
这个逐节循环建议由 Orchestrator 控制，每次只调用 Writer Agent 生成一个章节。 Writer App接收：

输出：
{
    "job_id": "...",
    "section_id": "S01",
    "outline": {},
    "research_package": {},
    "previous_sections_summary": {},
    "revision_instruction": null
}

3.审核Agent
不要把全文写作做成一个节点。内部拆分成：
工作流开始 -> 事实核验 -> 引用核验 -> 时间一致性检查 -> 结构检查 -> 风格检查 -> 风险检查 -> 汇总问题 -> 输出审核决策

输出：
{
    "decision": "approve",
    "issues": [],
    "section_decisions": [],
    "scores": {}
}

4.调度监督Agent
他是主Agent，但不要让它自由规划整一个任务，它只负责轻量决策：
-根据审核结果选择下一步
-将问题分配给 Research 或 Writer
-判读那是否需要人工介入
-生成最终标题、摘要和编辑说明

主工作流通过 FastGPT 已有的 appModule 调用三个子 App
FastGPT已经具备子应用分派能力，执行映射位于 FastGPT/packages/service/core/workflow/dispatch/constants.ts:45

三、整体的业务链路：

完整链路不要设计成 Agent 之间自由聊天，而是设计成显式状态机：

  CREATED
    ↓
  RESEARCHING
    ↓
  RESEARCH_REVIEW
    ├── 人工补充要求 → RESEARCHING
    └── 确认
         ↓
  OUTLINING
    ↓
  OUTLINE_REVIEW
    ├── 人工修改 → OUTLINING
    └── 确认
         ↓
  DRAFTING
    ├── SECTION_01
    ├── SECTION_02
    ├── SECTION_03
    └── ...
         ↓
  ASSEMBLING
    ↓
  REVIEWING
    ├── approve ───────────→ FINAL_REVIEW
    ├── rewrite → REVISING → REVIEWING
    ├── research → RESEARCHING
    └── human_review ──────→ WAITING_HUMAN
                                ↓
                           FINAL_APPROVED
                                ↓
                             PUBLISHED

必须设置循环上限：

reseach_retries <= 2
section_rewrite <= 2
review_rounds <= 3

超过限制进入 WAITING_HUMAN，不要无限调用模型。

四：节点和 checkpoint 的关系

需要把一个可独立重跑的业务步骤定义成一个 checkpoint：

研究 | Research App | research_package_v1
提纲 | Writer App/outline | outline_v1
章节写作 | Writer App/section | section_S01_v1
全文合成 | Writer App/assemble | draft_v1
审核 | Reviewer App | review_round_1
定点修改 | Writer App/revise | section_S01_v2
最终确认 | 人工操作 | final_v1

Orchestrator 必须在收到结果后完成：

检查 HTTP 成功 -> 校验 JSON Schema -> 校验 fact_id 和引用 -> 保存 artifact -> 保存Agent执行记录 -> 原子更新 step/job 状态 -> 发布下一步事件

校验失败时，当前 checkpoint 不提交

五、

建议完成的架构如下：

用Python完成：写作任务状态机/Checkpoint/子Agent的调度/中间稿件版本/SSE任务调度
用FastGPT完成：Agent提示词/RAG检索/Agent Loop、工具选择/写作工作台

六、

前端不应该只是一个聊天框。长链路新闻创作更适合任务工作台。

页面1:任务列表

展示：选题/当前阶段/完成进度/当前Agent/最后更新时间/是否等待人工操作/失败原因

页面2:创建任务

输入项：选题/写作类型：快讯、综述、深度稿、新闻稿/目标字数/目标读者/时间范围/新闻分类/指定来源/文风/是否允许外部检索/必须覆盖的信息/禁止表达的内容

七、

人工交互点不要每一步都让用户确认，只在高价值点暂停：
1.研究包确认：来源是否充分，角度是否正确。
2.提纲确认：文章结构是否符合预期。
3.审核异常：事实冲突或证据缺失。
4.最终稿确认：批准导出或发布

前端操作应提供：确认并继续/修改后继续/要求补充研究/重新生成当前章节/仅修改选中段落/回退到上一版本/批准最终稿

八、

进度事件：
后端通过SSE推送结构化事件：
{
    "event": "step.completed",
    "job_id": "job_123",
    "step": "section_drafting",
    "section_id": "S03",
    "progress": {
        "completed": 3,
        "total": 6
    }
}

前端与FastGPT的边界：
浏览器 -> Writing API -> Irchestrator + Worker -> FastGPT Agent Apps
FastGPT原来的聊天界面可以保留，用于：调试单个Agent/查看运行节点细节/测试提示词/临时回答