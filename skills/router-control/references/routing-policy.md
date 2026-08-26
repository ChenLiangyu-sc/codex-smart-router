# 路由策略

路由器采用安全优先的保守策略。它只给主 Sol 会话提出角色建议，最终交付责任仍在主会话。

| 任务特征 | 建议执行者 | 原因 |
| --- | --- | --- |
| 搜索、盘点、读日志、收集证据 | `router_scout`（Local Text → Luna/GLM → Terra 链） | `LOCAL_TEXT_FIRST` 只替换只读轻任务的首选；失败按链回退，选择期被跳过的 provider 不计入 attempt |
| 复杂纯文本实现或修复 | `router_worker`（GLM-5.3；必要时 Terra） | `GLM_FIRST` 下优先 GLM，高峰/熔断/多模态回退 Terra |
| 代码审查、独立复核 | `router_reviewer`（GLM-5.3；必要时 Terra high） | 纯文本优先 GLM，Terra 保留多模态与异构复核价值 |
| 低风险 bounded 测试编写与验证 | `router_tester`（Luna 需显式开启；否则 GLM/Terra 链） | 适合机械验证；必要时可写测试文件 |
| 低风险 bounded 文档更新 | `router_docs`（Luna 需显式开启；否则 GLM/Terra 链） | 低风险、范围清晰 |
| 架构设计、复杂跨模块归因、安全审查、生产决策 | 主 Sol 或 `router_reviewer`（Terra） | Luna 不参与复杂 lane；高风险由安全门保留给 Sol |
| 等待、轮询、状态检查 | 确定性 `wait_for_condition`（无模型执行器，route_task 拒绝 `router_monitor`） | 一次长等待，不调用模型、不产生轮询 token；完成后原 Sol turn 继续 |
| 文件存在性、精确搜索、hash/元数据、git 状态、schema、单次现有测试命令 | `TOOL_ONLY` | 当前 Sol 用最少直接工具调用完成，不支付 child startup |
| 小任务、高风险、架构任务或不确定任务 | 主 Sol | 委派收益不足或错误代价高 |

重任务 `STABLE / GLM_FIRST`、轻任务 `LUNA_STABLE / LOCAL_TEXT_FIRST`（用户界面显示为 Local 关闭/开启）与 Luna 开关正交。GLM 时段和额度回退同时影响两个 lane：工作日 `14:00 <= time < 18:00`（`Asia/Shanghai`）在委派前直接选 Terra；额度、认证和限流错误由用户级 circuit breaker 共享给所有项目。到达服务端 `next_flush_time` 后只允许一个半开探测，成功后自动恢复 GLM。

Luna 默认关闭（`LUNA_DISABLED`）：不作为任何角色首选，也不作为隐藏回退；旧版本隐式启用的状态迁移后同样关闭。`$router-control luna 开启` 后进入 `LUNA_BOUNDED`，只承接低风险、边界明确的 scout/tester/docs；复杂 worker/reviewer、多模态审查、架构归因、安全审查、生产决策和高风险写入不使用 Luna。`luna 关闭` 不改变路由 ON/OFF。

轻任务优先级链：1) scout 且 `LOCAL_TEXT_FIRST`、Local 可用 → Local；2) Luna 已开启且任务符合 `LUNA_BOUNDED` → Luna；3) `GLM_FIRST` 且 GLM 可用、非高峰 → GLM；4) 否则 Terra。因此 GLM 关闭 + Luna 关闭时轻任务直接 Terra；GLM 开启 + Local 关闭 + Luna 关闭时先 GLM、必要时 Terra；Local 开启 + Luna 关闭 + GLM 开启时 Local 失败后回退 GLM；Local 开启 + Luna 关闭 + GLM 关闭时 Local 失败后回退 Terra。每个 routed task 最多实际调用两个模型；Local → Luna → GLM、Local → GLM → Terra 等三连回退被硬性禁止。可写 executor 可能已修改工作区且无法确认完成状态时，禁止启动第二个 writer，交回 Sol。图片输入不得发送给纯文本 Local 或 GLM。

GLM 官方端点和 MAAS 中转统一按 `json_object_adapter` 能力处理：先以无模型 adapter 确定性转换 shallow wire receipt；无法安全转换的只读任务在剩余 deadline 内回退链上下一个执行器，不启动第二个格式模型。格式偏差不计为 provider 健康故障；运行时失败才触发熔断。内网 base URL 只允许通过本地 policy 显式配置。

包含图片的 worker/reviewer 任务强制 Terra；轻角色不接收图片。可写 executor 失败时，只有在没有工具活动且工作区指纹未变的情况下才能自动回退；存在可能已写入的证据时返回 Sol 检查，避免双 writer 冲突。

硬性保留给 Sol 的信号包括：身份验证、权限、密钥、支付、安全边界、生产部署、不可逆删除、数据库迁移、并发一致性、跨系统架构和需求边界不清。

可写角色必须取得显式、角色级的写授权：`router_worker` 需要实现/修复/创建等动作，`router_tester` 需要新增、修复或运行测试，`router_docs` 需要更新或编写文档。否定表达和显式只读约束优先；例如“只读盘点，不要实现、不要修复、不要改代码”只能进入只读角色。“如何实现、实现方案、修复建议、propose a fix”等规划短语中的关键词不构成写授权，除非同一任务另有独立的正向写操作。分类器不确定时留给 Sol，不能用可写 sandbox 试探。

每个用户目标由 `decision_id + lease_id` 绑定一个原子委派槽。路由为 `ON` 时所有原生 subagent（含外部插件 agent）一律拒绝且不消费委派槽：有当前 `DELEGATE` decision 时提示改用同步 `smart_router.route_task`；无 decision 时（含自动 Goal continuation——当前 Codex hook 没有该事件，Goal 恢复不会触发 UserPromptSubmit）要求 Sol inline 完成，不得复用旧 lease。需要原生 subagent 时先关闭 Smart Router。MCP 校验 task digest 并拒绝 lease 重放；wrapper 调用还必须携带与会话一致的 `luna_mode`。

默认 `V2_STATIC` 只把达到规模门的独立工作包交给 child：轻角色至少 4 项、Local scout 至少 8 项、GLM worker/reviewer 至少 5 项；语义多模态按能力例外处理。单文件/单工具/短验证、缺少明确边界、writer 只有“全仓/批量”而没有具体数量或路径时都留 Sol。`work_units` 只保留为兼容遥测；`V1_COMPAT` 可恢复 v0.4.1 行为。静态门没有主 Sol 和父级复核 token 可观测性，不得称为已校准成本模型。

回退链在 receipt `_router_meta`、session `last_execution`、telemetry `route_execution_finished` 和 `$router-control 状态` 四处一致记录 `selected_executor`、`attempted_executors`、`final_executor`、`route_path`、`route_path_label`、`fallback_occurred`、`fallback_stage`（selection/runtime/receipt/deadline）、`fallback_reason_code`、`selection_bypass_reason`、复数字段 `selection_bypass_reasons`（记录全部选择期跳过原因）、`attempt_usage` 与 `duration_ms`；`route_label` 继续表示最终执行者。共享 deadline 耗尽导致回退未启动时，原因码为 `shared_deadline_exhausted_before_fallback`，未被调用的 executor 不会出现在 attempt 台账中，也不会被记为其 provider 的失败。所有候选执行器都失败时，wrapper 抛出携带同一台账的 `RoutedTaskFailure`，MCP 返回 `status=failed` 的结构化结果，失败路径的记录与成功路径同样完整。`selection_bypass_reason`（如 `local_config_missing`、`glm_peak_window`）表示“未尝试”，与运行失败（`*_runtime_failure`）和 receipt 格式失败（`*_receipt_format_failure`）区分。

同一目标内 4–12 个只读项可合并为一次 child；不跨轮次排队、不拆成多个 subagent、不合并 writer。receipt v2 返回 coverage、evidence_manifest、inconsistencies 和最多三项 parent_verification；主 Agent 不重新通读全部输入。
