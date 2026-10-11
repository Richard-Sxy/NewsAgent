# 板块运营数据与新闻主线参考

432条候选判断、216个新闻观察快照、36条虚构主线；全部待人工复核。

完整答案请看 operations_casebook.md；模型只读 operations_inputs*.json。

| 板块 | 中文案例册 | 无答案评测输入 |
|---|---|---|
| 文化 | [36例参考](operations_casebook_culture.md) | [盲输入](operations_inputs_board_culture.json) |
| 国内 | [36例参考](operations_casebook_domestic.md) | [盲输入](operations_inputs_board_domestic.json) |
| 教育 | [36例参考](operations_casebook_education.md) | [盲输入](operations_inputs_board_education.json) |
| 娱乐 | [36例参考](operations_casebook_entertainment.md) | [盲输入](operations_inputs_board_entertainment.json) |
| 财经 | [36例参考](operations_casebook_finance.md) | [盲输入](operations_inputs_board_finance.json) |
| 健康 | [36例参考](operations_casebook_health.md) | [盲输入](operations_inputs_board_health.json) |
| 国际 | [36例参考](operations_casebook_international.md) | [盲输入](operations_inputs_board_international.json) |
| 本地 | [36例参考](operations_casebook_local.md) | [盲输入](operations_inputs_board_local.json) |
| 社会 | [36例参考](operations_casebook_society.md) | [盲输入](operations_inputs_board_society.json) |
| 体育 | [36例参考](operations_casebook_sports.md) | [盲输入](operations_inputs_board_sports.json) |
| 科技 | [36例参考](operations_casebook_technology.md) | [盲输入](operations_inputs_board_technology.json) |
| 天气 | [36例参考](operations_casebook_weather.md) | [盲输入](operations_inputs_board_weather.json) |

每板块3条独立主线，每主线6阶段与2个候选。development/validation/test各保留一条完整主线。
候选层是素材用途标签，层间可能复用同一主线观察；不能将三层文件当作互斥的训练/验证/测试集。
分析投影按新闻快照去重，全量216例、每split72例；运营评分保持432条候选固定分母。
源目录、产物和清单均版本化；当前没有真实执行结果或生产审批。
