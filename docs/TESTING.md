# 验证

在插件根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/probe_runtime.py
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/router-control
```

测试覆盖路由分类、经济性门、原子单目标唯一委派槽、一次性 runtime lease 与 task digest 绑定、ON 模式下全部原生 subagent 拒绝（含无 decision 的 Goal continuation 场景）、确定性长等待/取消/超时、receipt v2/objective 绑定与严格限界、GLM wire adapter、格式/运行时故障分类、共享 deadline、Luna 默认关闭与 `LUNA_BOUNDED` 手动开启、v7→v8 状态迁移（隐式 Luna 一律关闭）、Local/GLM/Terra 全路由矩阵、最多两次真实模型 attempt、选择期 bypass 不计入 attempt、回退链字段在 receipt/state/telemetry/status 四处一致，以及既有的写授权、高风险拒绝、profile、熔断、多模态、写租约、稳定 runtime、安装/卸载和密钥隔离行为。

真实 Codex 冒烟建议在非高峰依次验证：环境就绪的新会话在 OFF 时无路由提示；`glm 开启` 后重 profile 为 `GLM_FIRST`；`local 开启` 后轻 profile 为 `LOCAL_TEXT_FIRST`；`luna 开启` 后 Luna 状态为已开启；只读盘点通过本地文本 provider；故障时按链改用下一个执行器（Luna 关闭时不出现 Luna executor）；纯文本审查调用 GLM-5.3；带图片审查调用 Terra；模拟高峰选择 Terra；生产数据库迁移保留 Sol；受控临时仓库中的 worker 只改指定文件；SHADOW 不委派；ON 模式下原生 spawn_agent 被拒绝且不消费委派槽；关闭后不再注入路由指令。receipt 的 `_router_meta` 是实际模型/provider/effort、两套 requested profile、requested luna mode 与回退链的验收证据。

真实测试不应通过耗尽套餐来制造配额错误。配额码、`next_flush_time`、half-open 与并发探针用确定性 fixture 验证；至少保留一次真实 GLM 重任务、一条 GLM surrogate 轻任务、一条真实 Luna 回退和一条真实 Terra（含多模态时优先）调用。surrogate 通过不等于真实 DeepSeek 验收；切换正式内网 endpoint 后还需单独验证 Responses 兼容、工具调用、时延、并发、超时和断连恢复。

## 当前真实任务覆盖记录

截至 v0.3.1，当前构建已经真实验证：`jd-resume-optimizer`、`ppt_notes_pipeline_server-1.0.9`、`flash-sale-assistant`、`web-auto-translate-extension` 四个不同仓库上的只读侦察；GLM-5.3 surrogate 经自定义 Responses provider 实际执行；无效凭据触发 Local→Luna 实际回退；模型 receipt 超限被拒绝并在修复后回退；稳定 runtime 在模拟旧版本 cache 消失时继续执行 hook；从 `runtime-current` 发起的真实 surrogate 任务；Terra reviewer 对本插件稳定运行时补丁的真实只读审查；以及 Luna tester 在受限沙箱中执行 49 个目标测试和 9 个子测试并全部通过。首次 Luna 测试暴露了 hook 测试继承真实 `CODEX_HOME` 的隔离缺陷，修复为临时 home 后同一路径复测通过。

这不等于全面真实任务矩阵。尚未在 v0.3.1 最终构建上重新完成的真实分支包括：Luna docs 的受控写入、Terra worker 写入、GLM heavy reviewer、工作日高峰下真实 Terra 选择、Terra 多模态图片、长时间 monitor、多轮恢复/关闭以及实际 DeepSeek V4 Flash 内网端点。未覆盖项不得仅凭自动化测试表述为“全面真实验证”。

### v0.4.0 回归记录

v0.4.0 在四类真实路径上完成了回归：Luna scout 对 `jd-resume-optimizer` 的 37/37 范围盘点并返回 receipt v2；Luna tester 在 `web-auto-translate-extension/backend` 运行 12 个测试文件、144 项测试和 TypeScript 类型检查，全部通过；Terra worker 仅在受控临时 Git 仓库创建一个指定文件并核对哈希，随后删除该临时仓库；真实 GLM-5.3 reviewer 与 Terra reviewer 分别审查 v0.4 运行时补丁，并发现 objective 校验顺序、超时 usage 回收、非有限数值、租约竞争、取消处理、receipt 限界和路由泛化等问题，均已修复并由自动化回归覆盖。

最终候选版本的自动化套件包含 134 项测试，其中包括 MCP 子进程级取消测试，验证长等待期间主循环仍能接收 `notifications/cancelled`，且成功、超时、取消路径模型 token 均为零。两名独立 subagent reviewer 在最终修复后再次检查运行时并发/等待路径和策略/文档一致性，结论记录在发布交接中。

仍未真实覆盖：正式 DeepSeek V4 Flash 内网 endpoint、Terra 图片输入、工作日 14:00–18:00 的自然时钟真实高峰切换，以及持续数十分钟的生产进程等待。高峰逻辑、Responses 端点约束、长等待超时与取消已经由确定性测试覆盖，但不能替代上述真实环境验收。

### v0.4.1 回归记录

v0.4.1 针对 GLM 官方端点与 MAAS 中转不保证 OpenAI strict Structured Outputs 的现实增加 provider capability adapter。自动化套件覆盖用户报告的 `pass → completed`、扁平 object array、`coverage.scope → mode`、manifest null path、嵌套/空/status-only 回执拒绝、格式故障不打开熔断、writer 不启动第二写者，以及 GLM/Terra 共用一个 deadline。review 后删除了模型格式重试，避免把不可信 raw receipt 送入仍具工具能力的第二个 Codex 子进程。

2026-08-26 13:29–13:51（Asia/Shanghai）对官方 `https://open.bigmodel.cn/api/v1/responses` 做了多次真实 GLM-5.3 Max reviewer 回归。首轮 57 秒内一次完成；中间一轮真实暴露了非标准 coverage 形态并按共享 deadline 回退 Terra，随后 adapter 增加保守的 mode/计数规范化；最终候选在 47 秒内由 `glm_reviewer` 一次完成，canonical receipt v2 校验通过，`receipt_mode=json_object_adapter`，只发生 `status_alias/findings_objects/evidence_objects` 的无模型规范化，没有 Terra fallback。内网 MAAS 端点从当前外网机器不可达，须按 [内网 GLM 接入](INNER_NETWORK_GLM.md) 在内网完成相同验收；此项不得伪称已真实通过。

安装 `0.4.1+codex.20260826055427` 并切换 `runtime-current` 后，又从稳定 runtime 入口执行了一次官方 GLM reviewer：106 秒内一次完成，adapter 规范化 status、扁平 findings/inconsistencies 和 coverage scope，`receipt_format_error=null`，没有 Terra fallback。该任务同时发现新 schema 在提交前仍是 untracked 文件；发布流程随后必须把它纳入 Git commit，不能只依赖本机 cache。

v0.4.1 安装器补丁恢复同步 MCP 的 `tool_timeout_sec = 1200`，并为 Codex 写入 managed markers 内的 `hooks.state` trusted hashes 增加窄范围兼容解析。回归覆盖：幂等安装与按内网报告冻结的 v0.4.0 已安装配置 fixture 在升级预览中不删除长超时、reinstall/uninstall/check-uninstall 保留合法 hook 状态、非 hooks 漂移仍 fail closed 且阻断整套卸载，以及 GLM 与 DeepSeek 同时配置时两个 API key 均进入子进程 shell 排除列表。

### v0.4.2-alpha 经济门回归记录

v0.4.2-alpha 引入默认 `V2_STATIC` 和会话级 `V1_COMPAT` kill switch。回归覆盖 TOOL_ONLY、弱词不直接委派、Luna/Local/GLM 规模门、4–12 项只读合并提示、路径与 basename 去重、破坏性图片/文件操作留 Sol、语义多模态无图 fail closed/有图强制 Terra，以及 `turn.completed`/legacy snapshot/fallback attempt 的 usage 口径。

发布候选的自动化套件为 167/167 通过，plugin 和 router-control skill validator 均通过。单次综合 subagent review 发现的混合语言删除漏判、MCP/runtime 多模态旁路和 docs/tester 角色优先级问题已修复，窄范围复核无剩余 finding。

四个用户项目的预注册矩阵共 20 个场景：`jd-resume-optimizer`、`ppt_notes_pipeline_server-1.0.9`、`flash-sale-assistant`、`web-auto-translate-extension`。使用 `SMART_ROUTER_EVAL_WORKSPACE_ROOT=/home/chenliangyu/workspace` 时，测试会同时确认 fixture 引用的真实项目路径存在；该矩阵检查分类而不伪称执行 20 次模型任务。

2026-08-26 对 `jd-resume-optimizer` 执行了一次真实 Luna scout：只读检查 4/4 个 optimization-runtime 文件，47.953 秒内返回合法 receipt v2，无写入。usage adapter 记录 `input=56006`、`cached_input=32256`、`cache_write=0`、`output=1908`、`reasoning=422`，口径为 `exec_per_turn/per_turn_sum`。这一单例证明 child startup 可能很大，但不足以校准 P75/P50；主 Sol inline token 和 parent verification token 仍明确标为 unavailable。

### v0.4.3-alpha 稳定性回归记录

v0.4.3-alpha（不含 Kimi K3）完成四项稳定性变更并回归：

1. **native spawn 全拒绝**：ON 模式下所有原生 `Agent`/`spawn_agent`（explorer/worker/reviewer/外部插件 agent）被拒绝且不消费委派槽；有当前 `DELEGATE` decision 时拒绝信息指向同步 `route_task`；INLINE_SOL、TOOL_ONLY 与无 last_decision（自动 Goal continuation，当前 Codex hook 无该事件）场景分别有独立拒绝话术，均不得复用旧 lease。OFF/SHADOW 保持原生 agent 兼容。未增加通配 PreToolUse matcher，也未拦截 rg/find/sed 等普通工具。
2. **回退链可观测性**：`_router_meta`、session `last_execution`、telemetry `route_execution_finished` 与 `$router-control 状态` 一致记录 `selected_executor`、`attempted_executors`、`final_executor`、`route_path`、`route_path_label`、`fallback_occurred`、`fallback_stage`、`fallback_reason_code`、`selection_bypass_reason`、`attempt_usage`（含 per-attempt model label、耗时与 token）和 `duration_ms`；`route_label` 保持最终执行者语义。Local 未配置/Key 缺失/熔断、GLM 高峰/熔断等“未尝试”与运行/receipt 失败后的回退在状态页分开展示。
3. **Luna 默认关闭**：新增 `luna_mode`（`LUNA_DISABLED`/`LUNA_BOUNDED`），默认关闭；`$router-control luna 开启/关闭` 与 `routerctl.py luna-on/luna-off` 控制；`luna 开启` 同时置 ON，`luna 关闭` 不动 ON/OFF；schema v7→v8 迁移把旧隐式 LUNA_STABLE 一律迁移为 LUNA_DISABLED；route_task 的 `luna_mode` 由 PreToolUse 与会话状态强一致校验。Luna 关闭时任何链中都没有 Luna executor。
4. **新路由矩阵与 attempt planner**：轻 lane 优先级 Local（仅 scout + LOCAL_TEXT_FIRST）→ Luna（仅 LUNA_BOUNDED）→ GLM（GLM_FIRST 且可用非高峰）→ Terra；复杂 lane GLM_FIRST 时 GLM→Terra、STABLE 时 Terra、多模态强制 Terra。每个 routed task 最多两次真实模型调用（`MAX_MODEL_ATTEMPTS=2`）；选择期 bypass 不计入 attempt；`Local → GLM → Terra` 等三连回退在结构上不可能发生；writer 可能已修改工作区时禁止第二个 writer。

自动化套件 192 项全部通过（1 项既有 skip）；plugin manifest/结构验证与 router-control skill 验证通过；安装器 dry-run、重复安装、卸载、旧 schema 迁移相关测试全部保留并通过。真实冒烟结果见下节。

#### v0.4.3-alpha 真实冒烟记录（2026-08-26，Asia/Shanghai 非高峰）

- **GLM reviewer（第一次）**：真实 GLM-5.3 子进程出现 transport 层瞬态失败（错误码 1302），按设计打开 120 秒 transient 熔断，Terra 回退因共享 end-to-end deadline 已耗尽而拒绝启动，任务以 `ChildFailure` 失败。属 provider 侧失败，非代码缺陷；熔断在冷却后半开自愈。
- **GLM reviewer（重试）**：`selected_executor=glm_reviewer`，GLM 完成请求但 wire receipt 未通过 adapter 校验（`fallback_stage=receipt`、`fallback_reason_code=glm_receipt_format_failure`），未打开熔断；`terra_reviewer` 在共享 deadline 内完成，`route_path=[glm_reviewer, terra_reviewer]`、`route_path_label="GLM-5.3 → Terra"`、`fallback_occurred=true`，usage/duration 全量记录，结论正确。该运行真实验证了规格要求的"GLM receipt 格式失败后回退 Terra"场景。
- **Local surrogate scout**：`LOCAL_TEXT_FIRST` 下 `local_scout` 完成请求但 receipt 不满足 strict schema（GLM 上游不保证 strict 结构，属已知 surrogate 兼容性现实，不等于 DeepSeek 结论），`local_receipt_format_failure` 回退 `terra_scout` 完成，`route_path_label="GLM-5.3 surrogate → Terra"`，local 熔断保持关闭。
- 未执行：正式 DeepSeek 内网端点、Kimi K3（本版本明确不包含）、Terra 多模态图片真实调用。运行期间未打印任何 Key 或 providers.env 内容。

#### 第二轮 review 修复回归（Sol reviewer changes requested 之后）

1. **双失败台账（P1）**：全部候选执行器失败时，`run_task` 抛出携带完整 `_router_meta` 的 `RoutedTaskFailure`（route_path、逐 attempt usage/耗时、fallback 原因、聚合 usage 与流口径），MCP 返回 `status=failed` 的结构化结果而不是纯文本错误；writer 抑制路径同样保留单 attempt 台账。新增“GLM+Terra 连续失败”“writer 可能已写入时禁止第二次执行”“MCP 结构化失败”与 hook 级“双失败进入 state/telemetry/状态页”测试。
2. **monitor 无模型执行器（P1）**：planner 对 `router_monitor` 直接抛错，`run_task` 同样拒绝；`luna_monitor` 执行器规格移除；“Luna 关闭零 Luna executor”矩阵纳入 monitor 断言。
3. **GLM effort 统一 max（P1）**：`glm_scout/glm_tester/glm_docs` 恢复 `max`，与 `glm_worker/glm_reviewer` 一致；新增执行器 effort 断言。
4. **复数 selection bypass（P2）**：`_router_meta`/state/telemetry 新增 `selection_bypass_reasons`（按 provider 记录全部跳过原因），状态页逐项展示；显示码映射与 planner 实际代码对齐（`invalid_policy`、`local_config_*`）。
5. **DELEGATE 拒绝话术按委派槽状态区分（P2）**：`available` 提示调用 route_task，`started/running` 提示等待现有同步回执，`completed/failed` 提示整合回执，decision/lease 不匹配按无有效 decision 处理。
6. **状态页措辞（P2/P3）**：轻任务展示改为 “Local：开启/关闭”，`local 关闭` 回复与 README/SKILL 不再向用户显示内部枚举 `LUNA_STABLE`；删除 TESTING.md 中与真实冒烟记录矛盾的句子。

#### 第三轮 review 修复回归

1. **deadline 误报（P1）**：共享 deadline 在下一 executor 启动前耗尽时改走专用 `DeadlineExhausted` 分支——不调用该 provider 的失败记录、不标记为其 runtime failure、立即终止候选链；`fallback_stage=deadline`、原因码 `shared_deadline_exhausted_before_fallback`；`attempted_executors/attempt_usage` 只含真实调用。新增“GLM 耗尽 deadline 后 Terra 未调用不得记为 terra 失败”与“Local 链耗尽 deadline 后 Luna/GLM/Terra 均不启动也不误记”两个回归测试。
2. **状态页信息分层（P2）**：outcome 恒显于标题括号；“未尝试：Local 配置缺失；GLM 高峰时段”独立成行（按链优先级排序，不受 state 持久化键排序影响）；多次真实 attempt 显示“回退原因”，单次失败显示“失败原因”，deadline 未启动回退显示“回退未启动原因”。新增对应渲染测试。
