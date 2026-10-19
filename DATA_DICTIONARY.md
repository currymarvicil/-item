# 数据口径与派生文件

唯一经验数据来源为题目提供的 data/maintenance_tasks.csv。原始文件不改写。

## 原始字段

| 字段 | 类型 / 解释 | 处理 |
| --- | --- | --- |
| task_id | 任务唯一标识，按文本保留 | 完全重复行去重；同 ID 冲突版本全部隔离 |
| dispatch_time | 下发并调度出发的带时区时间 | 解析为 aware datetime；UTC+8 日期用于切分 |
| telemetry_synced_at | 派工所依赖的遥测最后更新时间 | 保留 Z 时区；不得按本地无时区时间读取 |
| plant_zone | 区域，名义类别 | 不转成有序数值，不推断真实物理位置 |
| device_type | 设备机械类别，名义类别 | 不是单台设备 ID，不能据此识别重复设备 |
| reported_health_score | 健康分，低分更紧急 | 0 是合法值；数值有限才接受 |
| continuous_run_hours | 题目称为当天连续运转小时 | 有 1,064 个值大于 24，保留原值并标记语义矛盾；不作主策略输入 |
| task_success | 1 成功，0 因现场状态不符而中止 | 空值保持未知，不插补；只对已知标签计算回测 |

## cleaned_tasks.csv 的新增字段

- source_row：原始 CSV 的首次保留行号，含表头，首条记录为第 2 行。
- telemetry_age_minutes：(dispatch_time − telemetry_synced_at) 的分钟差。
- success：原始标签的整数形式；缺失时为空。
- outcome_missing：1 表示结果缺失，0 表示已知。
- run_hours_definition_conflict：1 表示连续运行数值 >24 小时，与题目“当天”定义矛盾；不代表已确诊数据损坏。
- dispatch_date：派工时刻对应 UTC+8 的日历日期。

raw 和 cleaned 的数值文本格式可能不同（如 19 与 19.0），数值含义保留；原始字节以 data 中 CSV 为准。task_id 不参与预测。

## 输出约定

- metrics.json：全部主要结果、质量统计、时间切分、策略、缺失标签边界、情景与限制。
- policy.json：训练期选中的规则，不含逐任务结果。
- quality_audit.csv：删除/隔离记录、原始行号与原因；目前 55 条均为完全重复。
- age_bins.csv、zone_cross.csv、device_cross.csv、health_cross.csv：计数、比例与 Wilson 区间，不是额外样本。
- daily.csv、policy_by_day.csv：每日结果完整度和规则的逐日条件回测。
- candidate_search_train.csv：198 个训练期候选及排序；没有用后段标签来选阈值。
- policy_evaluation.csv：训练与后段的策略对照；运转时长一行仅为诊断对照。
- threshold_sensitivity.csv、effectiveness_sensitivity.csv：明确标记的阈值对照和 q 假设情景。
- task_decisions.csv：1,108 个任务逐项核实建议；historical_failure_in_selected_group 的空白表示结果未知，不能解释为 0。该字段只用于历史核对，不输入放行规则。
- overview.svg：由上述真实计数与假设情景绘制，不引入外部统计。

工时是人力投入，不是自然经过时长。核实动作、核实有效性、紧急响应改善、现场实际状态均没有在题目数据中被观察，不能将模型情景当成现场试验。
