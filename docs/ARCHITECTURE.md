# 架构与故障恢复

`UserPromptSubmit` hook 读取当前 `session_id` 的状态，识别控制命令，并对普通任务做保守分类。每个新会话从 `OFF + STABLE + LUNA_STABLE + LUNA_DISABLED + V2_STATIC` 开始；`glm 开启` 原子切换重任务配置，`local 开启` 原子切换轻任务配置，`luna 开启` 显式启用 `LUNA_BOUNDED`，三者都会把会话设为 `ON`，恢复同一会话时沿用。`glm/local/luna 关闭` 与 `经济策略 v1/v2` 只切换对应开关，不改变 ON/OFF。`ON` 时，hook 把建议角色、两套执行配置、Luna 模式、风险和约束作为 developer context 注入主 Sol 会话；Sol 调用本地 `smart_router.route_task` typed tool，wrapper 再按 planner 链以固定模型/provider 启动临时 Codex child。

执行层采用"角色 × provider"矩阵和有序候选链：worker/reviewer 属于复杂 lane，`STABLE` 映射 Terra，`GLM_FIRST` 动态选择 GLM-5.3 或 Terra（图片输入强制 Terra）。scout/tester/docs 属于轻 lane，优先级为 Local（仅 scout 且 `LOCAL_TEXT_FIRST`）→ Luna（仅 `LUNA_BOUNDED`）→ GLM（`GLM_FIRST` 且可用非高峰）→ Terra。monitor 不再映射任何模型：planner 对 `router_monitor` 直接抛错、`run_task` 同样拒绝（`luna_monitor` 执行器规格已移除），等待一律由 `wait_for_condition` 在 MCP 进程内做单次阻塞长等待。图片输入只允许 worker/reviewer 并强制 Terra，纯文本 Local/GLM 永不接收图片。选择阶段因配置缺失、Key 缺失、熔断、高峰或多模态被跳过的 provider 记为 selection bypass，不计为模型 attempt；wrapper 硬性限制每个 routed task 最多两次真实模型调用，因此 Local → GLM → Terra 这类三连回退在结构上不可能发生。共享 deadline 在下一个 executor 启动前耗尽时走专用 `DeadlineExhausted` 分支：该 executor 未被调用，不会记为其 provider 的 runtime failure、不写其健康熔断，候选链立即终止，稳定原因码为 `shared_deadline_exhausted_before_fallback`（`fallback_stage=deadline`），`attempted_executors/attempt_usage` 只包含真正调用过的模型。

v0.4.2-alpha 把经济性判断升级为三门式静态代理：安全/能力门先保留高风险与不确定任务；确定性门把文件存在性、精确查询、元数据、git 状态、schema 和单次现有测试命令标记为 `TOOL_ONLY`，由当前 Sol 回合直接调用工具；规模门再要求独立 bounded package、`4+` 回合桶和角色最小项数。轻角色（scout/tester/docs）至少 4 项，Local scout 至少 8 项，GLM worker/reviewer 至少 5 项。泛化词、路径出现、prompt 长度和旧 `work_units` 只进入遥测，不再单独产生委派。`V1_COMPAT` 保留旧门作为会话级 kill switch。

这不是伪装成精确成本模型：当前 Codex hook API 无法可靠观测 Always-Sol 反事实 token、会员额度或 receipt 后父级验收 token，因此 telemetry 将这些维度标记为 unavailable，`cost_estimate_status=cold_start_static_proxy`。SHADOW 记录 task bucket、deterministic kind、路径/显式数量、预计项数、bounded/micro/coalesce 标志与 reason codes，但不启动 child。积累配对 replay 数据前，不以虚构 P75/P50 放宽路由。

同一 prompt 已包含 4–12 个同角色只读项时，developer context 要求把它们合并进唯一一次 child 调用。v0.4.2-alpha 不跨用户轮次排队、不等待凑批、不合并 writer，也不改变一个用户目标一个委派槽。语义多模态属于能力路由：decision 强制 worker/reviewer，PreToolUse 要求实际传入图片路径，`run_task` 再按实际 task 独立检查并在无图时 fail closed；有图才交由 provider policy 强制 Terra。图片尺寸、EXIF、页数和 hash 仍走确定性工具。

每个普通用户 prompt 生成 SHA-256 `decision_id`、随机 `lease_id` 和一个委派槽；hook 以 session 文件锁执行原子 compare-and-swap。MCP 随后再次原子消费 lease，并校验 role 与规范化 task digest；hook 缺失、并发重复、task 被替换或 lease 重放都会在模型启动前拒绝。

本地文本配置位于 `~/.codex/smart-router/local-provider.json`，配套模型目录位于 `local-models.json`，均为 `0600`。加载器只接受固定 schema、受限 provider/model/env 标识和 `wire_api=responses`，并拒绝用户信息、query/fragment 与默认不安全的公网 HTTP；不会把配置内容直接拼接成任意 TOML。依据 [Codex config reference](https://developers.openai.com/codex/config-reference)，当前自定义 model provider 只支持 Responses wire API，因此未来的 DeepSeek 内网网关也必须实现该协议。无鉴权内网服务可省略 `env_key`。

`python3 scripts/configure_local_provider.py --glm-surrogate` 生成一个明确标记为 surrogate 的 GLM-5.3 Medium 配置，复用现有 GLM provider 端点和 `ZHIPU_API_KEY` 环境变量名但不复制凭据。它验证的是轻任务路由控制面和 Responses 数据面，不替代 DeepSeek V4 Flash 的质量、吞吐、故障与端点兼容性测试。

GLM 默认策略为 `Asia/Shanghai`、周一至周五、`14:00 <= 当前时间 < 18:00` 使用 Terra；周末仍可使用 GLM。可选的 `~/.codex/smart-router/policy.json` 允许覆盖 `timezone`、`peak_weekdays`、`peak_start`、`peak_end`、两类 cooldown、`glm_base_url` 和 `allow_insecure_glm_http`，非法文件会 fail closed 到 Terra。官方默认端点是 HTTPS；内网 HTTP hostname 必须同时显式设置 `allow_insecure_glm_http=true`，URL 中的凭据、query 和 fragment 一律拒绝。

provider 凭据从进程环境或权限必须为 `0600` 的 `~/.codex/smart-router/providers.env` 读取。wrapper 通过子进程环境把 Key 交给 Codex provider，但命令参数只有环境变量名；同时按照 Codex 配置使用动态 `shell_environment_policy.exclude` 从 child shell 过滤所有已知 provider Key，并用 `--strict-config` 阻止未知配置静默失效。状态、receipt 和异常信息仅保留 Key 指纹或脱敏文本。

正常且默认关闭的新会话不注入额外上下文。`SessionStart` 只在安装未就绪或恢复一个已开启/影子会话时提供紧凑提示；`SessionStart` 与 `UserPromptSubmit` 均设置 512 token 的附加上下文上限。普通路由契约只携带角色、写权限、调用/回退规则和用户可见结果标签，不重复完整策略。完整规则按需保留在 skill reference 中。

receipt v2 的字段集合、数组数量和所有文本长度均有界，额外字段会被拒绝；findings/evidence 为完整路径和完整句子预留 800 字符。新增 `objective_id`、`coverage`、`evidence_manifest`、`inconsistencies` 和最多三项 `parent_verification`。strict provider 必须返回匹配当前 decision 的 objective；GLM 的 objective 由可信 wrapper 在 adapter 阶段绑定，不依赖模型复制。主 Agent 把 coverage 当作已覆盖边界，只复核 manifest 哈希、异常、parent_verification 和少量样本。

v0.4.1 把结构化输出能力放入 `ExecutorSpec.receipt_mode`：OpenAI executor 使用 `strict_json_schema`；GLM 官方端点和 GLM MAAS 都使用 `json_object_adapter`。GLM 收到较浅的 `receipt-wire.schema.json`，主进程只做有界、可审计的确定性转换：状态别名、`coverage.scope → mode`、扁平标量对象数组、manifest 的 null path，以及运行时 `schema_version/objective_id`。嵌套对象、非有限数、超限数组和非法哈希不会被猜测修复。最终结果仍须通过 canonical receipt v2 schema。

安装器为同步 `smart_router` MCP 固定写入 `tool_timeout_sec = 1200`，使长任务在 wrapper 的共享 deadline 内完成后仍能唤醒主 Agent。升级和卸载继续按 manifest 精确验证受管 TOML；唯一兼容例外是 Codex 自动插入到 MCP markers 内的严格 `[hooks.state]` / `trusted_hash = "sha256:..."` 表。安装器会保留并移出这段状态，其他任何受管区漂移仍然 fail closed。

adapter 要求明确 status，且至少有一项 summary、action、finding、evidence、validation、risk 或 manifest；`{}`、仅 status、嵌套对象和其他无法安全转换的输出都会被拒绝。wrapper 不再把不可信 child raw 文本交给第二个模型做格式修复。只读任务直接在剩余 deadline 内回退 Terra；格式问题说明 capability/contract 不匹配，不会打开 GLM provider-health circuit，HTTP、鉴权、配额、断连和真实 timeout 才会。primary 与 fallback 共享调用开始时建立的一个 end-to-end deadline，fallback 不能重置完整 timeout。

`PreToolUse` 对本插件的 `router_*` agent 和 wrapper 工具做二次检查：OFF/SHADOW 禁止路由，ON 时要求角色与当前判断一致。可写角色还必须有本轮分类产生的 `write_authorized=true`；该授权只接受与角色相符、未被否定的正向写操作，并拒绝显式只读和高风险任务。wrapper 在子进程入口再次执行同一授权检查，避免主任务与下发任务不一致时扩大权限。

路由为 `ON` 时，所有原生 `Agent`/`spawn_agent` 调用都会被拒绝，包括 explorer、worker、reviewer 和外部插件提供的 agent：原生 child 会继承 Sol，绕过 `route_task` 契约、receipt v2、单委派约束和 provider usage 台账，并允许父 Sol 在 child 异步运行时继续投入。拒绝不消费委派槽，也不会从模型自行生成的 spawn task 合成写授权或可写 lease。存在未消费的 `DELEGATE` decision（委派槽 `available`）时，拒绝信息要求改用当前 `decision_id/lease_id` 调用同步 `smart_router.route_task`；委派槽 `started/running` 时要求等待现有同步任务回执，`completed/failed` 时要求整合已有回执、不得再次委派，槽位与 decision/lease 不匹配时按无有效 decision 处理；没有有效 decision 时（例如 INLINE_SOL 后、或恢复的 ON 会话），要求 Sol inline 完成且不得复用旧 lease。自动 Goal continuation 是一个已知盲区：当前 Codex hook 没有 Goal continuation 事件，Goal 恢复不会自动产生新的 `UserPromptSubmit` decision，因此它落入"无 decision 拒绝"分支。用户明确需要原生 subagent 时应先关闭 Smart Router；本版本不提供 native compatibility 开关。`OFF`/`SHADOW` 模式保持现有原生 agent 行为。`route_task` 同步等待隔离的 `codex exec` 完成并直接返回 schema 强制的 receipt，因此父 Sol 不依赖 `SubagentStop` 通知重新激活。

同一会话最多一个写入 agent。`PreToolUse` 创建的 MCP 租约记录角色、工作区、turn 与当前 `tool_use_id`，成功或失败都由对应 `PostToolUse` 清理；释放时必须同时匹配来源、角色和调用 ID。wrapper 执行期间还持有按工作区哈希隔离的 OS 文件锁，防止跨轮次、跨会话或 hook 状态竞争导致两个 writer 同时写同一工作区。

`wait_for_condition` 支持进程退出、文件出现/消失和文件包含固定文本，默认 900 秒、最长 3600 秒。MCP 主循环把长调用放入工作线程，继续读取 `notifications/cancelled`；等待线程用 event-aware wait 立即响应取消。timeout/cancelled 均返回 `isError=true`，阻止父流程把未满足条件当作成功；成功、超时与取消都记录实际 `duration_ms` 且模型 token 为零。v0.4 不承诺跨 Codex 进程持久化后台监控。

若 Codex 在某条失败路径漏发 `PostToolUse`，下一次 `UserPromptSubmit` 只会在 OS 锁证明没有 writer 运行时清理其 MCP 租约；锁仍被占用时保持 fail-closed。安装新版本后，无调用 ID 的 v0.1 遗留租约会被视为不可归属状态并安全清除。

安装器在 `[agents]` 下注册六个 `config_file`，先把运行 hook/MCP 所需的脚本、schema 与 agent 定义写成不可变完整包 `~/.codex/smart-router/runtime-releases/<content-id>/`，校验后通过同目录临时符号链接和 `os.replace` 原子切换 `runtime-current`；`[mcp_servers.smart_router]` 与 hook 都使用该稳定入口。这样任一调用只会看到完整的旧包或完整的新包，不会混用逐文件更新中的版本；两个安装进程同时发布相同 content id 时，后到者只接受已发布包的完整哈希匹配并清理自身 staging。hook 只接受 `runtime-current` 指向 `runtime-releases/<20 位十六进制 id>` 的受管形态，稳定入口无效或缺失时回退当前 `$PLUGIN_ROOT`；两者均缺失或 Python 执行失败则退出 0、静默放行主会话。安装器拒绝管理根、release 根及包内任一中间路径上的符号链接，依靠哈希所有权保护不可变包；卸载先完整预检 agent、配置与 runtime，存在冲突时不删除任何一项。升级前已载入旧版本 hook 命令的会话仍需重开，因为磁盘安装无法改写其内存中的命令。

状态和遥测默认位于 Codex 分配的 `$PLUGIN_DATA`。若环境没有该变量，则退回 `~/.codex/plugin-data/codex-smart-router/`。状态文件按会话 ID 的 SHA-256 保存，避免把 ID 暴露在文件名中。

状态 schema v8 记录 `current_delegation`、两套 provider profile、`luna_mode`、经济门、实际成功/失败计数和最近一次执行。v7 及更早的状态没有显式 Luna 用户授权，迁移时隐式的 `LUNA_STABLE` 一律变为 `LUNA_DISABLED`。`PostToolUse` 以最近 128 个 `tool_use_id + role` 键去重。`_router_meta`、session `last_execution`、telemetry `route_execution_finished` 与 `$router-control 状态` 使用同一组回退链字段：`selected_executor`、`attempted_executors`、`final_executor`、`route_path`、`route_path_label`、`fallback_occurred`、`fallback_stage`（selection/runtime/receipt/deadline）、`fallback_reason_code`、`selection_bypass_reason`、复数字段 `selection_bypass_reasons`（按 provider 记录全部选择期跳过原因，而非只保留第一个）、`attempt_usage` 和 `duration_ms`；`route_label` 继续表示最终执行者。`selection_bypass_reason` 区分”未尝试”（如 `local_config_missing`、`local_key_missing`、`local_circuit_open`、`glm_peak_window`、`glm_circuit_open`）与”尝试后回退”（`*_runtime_failure`、`*_receipt_format_failure`）。状态页按 attempt 显示 成功/失败、秒级耗时和 input/output token，outcome 恒定显示在标题括号中，“未尝试”（selection bypass）单独一行，“回退原因”仅在多次真实 attempt 时显示，单次 attempt 失败显示“失败原因”，deadline 未启动回退显示“回退未启动原因”；轻任务内部枚举 `LUNA_STABLE` 在用户界面显示为 “Local：关闭”，不再与 Luna 开关混淆。全部候选执行器都失败时，`RoutedTaskFailure` 携带同一份 `_router_meta` 台账，MCP 返回 `status=failed` 的结构化结果（含 route_path、attempt_usage 与 fallback 原因），因此失败路径的 state/telemetry/状态页与成功路径同样完整。遥测只保存数值、原因代码和 prompt 哈希，不保存子模型推理正文、密钥或鉴权头。usage adapter 只累计 `turn.completed` 的 per-turn usage；没有该事件的旧/中转流只取最后 snapshot，并用 `usage_stream_kind`、`usage_counter_semantics`、`usage_adapter_version` 明示口径。真实”是否发现有效问题”、主 Sol inline token、duplicate read ratio 和人工复核耗时仍需后续可选验收或离线 replay 补充，当前不伪造。

GLM provider 健康状态位于 `~/.codex/smart-router/provider-health.json`，因此对所有项目共享。配额错误优先采用服务端 `next_flush_time`；短暂故障使用短冷却，认证失败在 Key 指纹变化前保持熔断，订阅异常使用较长冷却。到期后状态进入 half-open，并发调用中仅一个探针被放行。每次状态变化递增 generation；成功请求只有在其启动 generation 仍是当前 generation 时才能关闭熔断，避免较早的成功响应覆盖较新的 quota/auth 失败。

本地文本健康状态独立保存在 `local-provider-health.json`。运行或认证失败先打开短熔断，期间 scout 按链改用下一个执行器；配置或 Key 指纹变化会重置熔断，到期后同样只允许一个半开探针。它不修改 GLM 重任务健康状态；GLM surrogate 因共享同一真实上游，只能验证逻辑隔离，无法模拟独立本地硬件故障。

可写 executor 运行失败或 receipt 无法规范化，且无法确认没有修改工作区时，继续沿用现有安全策略：禁止启动第二个 writer，交回 Sol。worker 只有在 Codex JSON 事件流包含明确终止事件、没有工具活动、Git 前后指纹都成功且未变化时才允许回退；非 Git 工作区、事件流不完整、指纹失败、超时或检测到变化时都禁止自动启动第二个 writer。只读角色没有此限制，直接在共享 deadline 内回退到链上下一个执行器（如 GLM → Terra）。

所有 `router_*` 角色都必须通过 MCP wrapper 执行；ON 模式下任何原生 subagent 都会被拒绝（见上文 PreToolUse 一节）。安装的 `router_monitor.toml` 仅为旧配置兼容占位，hook 不允许启动它。

故障时采用 fail-open 执行、fail-safe 路由：hook 自身异常不阻塞 Codex，但不会建议子 agent；主 Sol 继续处理任务。清理 30 天未使用的状态文件不会影响项目文件。

agent 安装器只管理它自己安装且哈希仍匹配的文件。遇到同名用户文件或配置会拒绝覆盖；用户修改过的已安装文件/配置片段在卸载时也会保留并报告。

## 与 sol-advisor 的关系

[DannyMac180/sol-advisor](https://github.com/DannyMac180/sol-advisor) 适合作为路由治理的设计参考：默认由主模型执行、委派前明确声明、子任务契约自包含、失败时收紧权限、最终由父代理依据 diff 和运行证据验收。本插件借鉴这些原则，但没有复制其实现，也不把它作为运行时依赖或直接代码底座。原因是本插件还需要会话级开关、自动分类、MCP wrapper、写租约生命周期和安装/全局停用机制，直接从现有小型实现演进更可控。
