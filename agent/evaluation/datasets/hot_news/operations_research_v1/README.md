# 运营建议评测扩充：公开研究设计与合成情景

版本：2026-10-14.public-research-synthetic-v1。
12板块、36条主线、72个观察快照、144条候选判断。
新闻事实、指标与运营身份均为合成；参考判断待人工校准，全部未投递。公开来源只解释设计依据。

[来源登记](source_registry.json) · [全部答案与人工 rubric](operations_casebook.md) · [全部盲输入](operations_inputs.json) · [数据统计](summary.json)

| 板块 | 参考案例册 | 无答案输入 |
|---|---|---|
| 文化 | [12例](operations_casebook_culture.md) | [盲输入](operations_inputs_board_culture.json) |
| 国内 | [12例](operations_casebook_domestic.md) | [盲输入](operations_inputs_board_domestic.json) |
| 教育 | [12例](operations_casebook_education.md) | [盲输入](operations_inputs_board_education.json) |
| 娱乐 | [12例](operations_casebook_entertainment.md) | [盲输入](operations_inputs_board_entertainment.json) |
| 财经 | [12例](operations_casebook_finance.md) | [盲输入](operations_inputs_board_finance.json) |
| 健康 | [12例](operations_casebook_health.md) | [盲输入](operations_inputs_board_health.json) |
| 国际 | [12例](operations_casebook_international.md) | [盲输入](operations_inputs_board_international.json) |
| 地方 | [12例](operations_casebook_local.md) | [盲输入](operations_inputs_board_local.json) |
| 社会 | [12例](operations_casebook_society.md) | [盲输入](operations_inputs_board_society.json) |
| 体育 | [12例](operations_casebook_sports.md) | [盲输入](operations_inputs_board_sports.json) |
| 科技 | [12例](operations_casebook_technology.md) | [盲输入](operations_inputs_board_technology.json) |
| 天气 | [12例](operations_casebook_weather.md) | [盲输入](operations_inputs_board_weather.json) |

每板块每split保留一条完整独立主线，每主线两个观察时点、每时点两个对照候选；不强迫真实新闻走六阶段模板。
自动评分复用既有固定分母评分器；manual_rubric 不进入模型输入，也不被自动当成语义评分。
板块运营仍需复核合理替代动作、理由码和改稿语义；正式标签二审、模型质量与真实运营收益另行验收。
源目录、不可变产物与 manifest 均版本化；不修改生产 Prompt/Bundle，不训练或投递。
