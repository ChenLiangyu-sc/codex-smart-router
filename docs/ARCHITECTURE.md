# 架构与故障恢复

`UserPromptSubmit` hook 读取当前 `session_id` 的状态，识别控制命令，并对普通任务做保守分类。每个新会话从 `OFF + STABLE + LUNA_STABLE` 开始；`glm 开启` 原子切换重任务配置，`local 开启` 原子切换轻任务配置，两者都会把会话设为 `ON`，恢复同一会话时沿用。`ON` 时，hook 把建议角色、两套执行配置、风险和约束作为 developer context 注入主 Sol 会话；Sol 调用本地 `smart_router.route_task` typed tool，wrapper 再以固定模型/provider 启动临时 Codex child。

执行层与角色层分开且采用正交 profile，避免组合枚举膨胀：worker/reviewer 在 `STABLE` 映射 Terra，在 `GLM_FIRST` 动态选择 GLM-5.3 Max 或 Terra；scout/monitor 在 `LUNA_STABLE` 映射 Luna，在 `LOCAL_TEXT_FIRST` 动态选择经验证的纯文本 provider 或 Luna。tester/docs 首版仍固定 Luna，因为它们可能写入。图片输入只允许 worker/reviewer，并强制 Terra。

本地文本配置位于 `~/.codex/smart-router/local-provider.json`，配套模型目录位于 `local-models.json`，均为 `0600`。加载器只接受固定 schema、受限 provider/model/env 标识和 `wire_api=responses`，并拒绝用户信息、query/fragment 与默认不安全的公网 HTTP；不会把配置内容直接拼接成任意 TOML。依据 [Codex config reference](https://developers.openai.com/codex/config-reference)，当前自定义 model provider 只支持 Responses wire API，因此未来的 DeepSeek 内网网关也必须实现该协议。无鉴权内网服务可省略 `env_key`。

`python3 scripts/configure_local_provider.py --glm-surrogate` 生成一个明确标记为 surrogate 的 GLM-5.3 Medium 配置，复用现有 GLM provider 端点和 `ZHIPU_API_KEY` 环境变量名但不复制凭据。它验证的是轻任务路由控制面和 Responses 数据面，不替代 DeepSeek V4 Flash 的质量、吞吐、故障与端点兼容性测试。

GLM 默认策略为 `Asia/Shanghai`、周一至周五、`14:00 <= 当前时间 < 18:00` 使用 Terra；周末仍可使用 GLM。可选的 `~/.codex/smart-router/policy.json` 仅允许覆盖 `timezone`、`peak_weekdays`、`peak_start`、`peak_end`、`transient_cooldown_seconds` 和 `subscription_cooldown_seconds`，非法文件会 fail closed 到 Terra。

provider 凭据从进程环境或权限必须为 `0600` 的 `~/.codex/smart-router/providers.env` 读取。wrapper 通过子进程环境把 Key 交给 Codex provider，但命令参数只有环境变量名；同时按照 Codex 配置使用动态 `shell_environment_policy.exclude` 从 child shell 过滤所有已知 provider Key，并用 `--strict-config` 阻止未知配置静默失效。状态、receipt 和异常信息仅保留 Key 指纹或脱敏文本。

正常且默认关闭的新会话不注入额外上下文。`SessionStart` 只在安装未就绪或恢复一个已开启/影子会话时提供紧凑提示；`SessionStart` 与 `UserPromptSubmit` 均设置 512 token 的附加上下文上限。普通路由契约只携带角色、写权限、调用/回退规则和用户可见结果标签，不重复完整策略。完整规则按需保留在 skill reference 中。

receipt 的数组数量和字段长度有界，但 findings/evidence 为完整路径和完整句子预留 800 字符，避免紧约束迫使模型拆碎证据。运行时会拒绝恰好撞上硬上限的疑似截断项，以及 `evidence`、`validation` 等字段名碎片；这类结果按 wrapper 失败处理，由 Sol 明确回退，而不是把语义破损的 receipt 当成成功证据。

`PreToolUse` 对本插件的 `router_*` agent 和 wrapper 工具做二次检查：OFF/SHADOW 禁止路由，ON 时要求角色与当前判断一致。可写角色还必须有本轮分类产生的 `write_authorized=true`；该授权只接受与角色相符、未被否定的正向写操作，并拒绝显式只读和高风险任务。wrapper 在子进程入口再次执行同一授权检查，避免主任务与下发任务不一致时扩大权限。

原生 subagent 路径要求 `fork_turns=none`。wrapper 使用 `--output-schema` 强制 receipt；原生 `SubagentStop` 第一次不合格可要求补正，之后不再循环。

同一会话最多一个写入 agent。`PreToolUse` 创建的租约记录角色、来源、工作区、turn 与当前 `tool_use_id`：原生 agent 由 `SubagentStop` 清理，MCP wrapper 无论成功或失败都优先由对应 `PostToolUse` 清理。释放时必须同时匹配来源、角色和调用 ID。wrapper 执行期间还持有按工作区哈希隔离的 OS 文件锁，防止跨轮次、跨会话或 hook 状态竞争导致两个 writer 同时写同一工作区。

若 Codex 在某条失败路径漏发 `PostToolUse`，下一次 `UserPromptSubmit` 只会在 OS 锁证明没有 writer 运行时清理其 MCP 租约；锁仍被占用时保持 fail-closed。安装新版本后，无调用 ID 的 v0.1 遗留租约会被视为不可归属状态并安全清除。

安装器在 `[agents]` 下注册六个 `config_file`，先把运行 hook/MCP 所需的脚本、schema 与 agent 定义写成不可变完整包 `~/.codex/smart-router/runtime-releases/<content-id>/`，校验后通过同目录临时符号链接和 `os.replace` 原子切换 `runtime-current`；`[mcp_servers.smart_router]` 与 hook 都使用该稳定入口。这样任一调用只会看到完整的旧包或完整的新包，不会混用逐文件更新中的版本；两个安装进程同时发布相同 content id 时，后到者只接受已发布包的完整哈希匹配并清理自身 staging。hook 只接受 `runtime-current` 指向 `runtime-releases/<20 位十六进制 id>` 的受管形态，稳定入口无效或缺失时回退当前 `$PLUGIN_ROOT`；两者均缺失或 Python 执行失败则退出 0、静默放行主会话。安装器拒绝管理根、release 根及包内任一中间路径上的符号链接，依靠哈希所有权保护不可变包；卸载先完整预检 agent、配置与 runtime，存在冲突时不删除任何一项。升级前已载入旧版本 hook 命令的会话仍需重开，因为磁盘安装无法改写其内存中的命令。

状态和遥测默认位于 Codex 分配的 `$PLUGIN_DATA`。若环境没有该变量，则退回 `~/.codex/plugin-data/codex-smart-router/`。状态文件按会话 ID 的 SHA-256 保存，避免把 ID 暴露在文件名中。

状态 schema v5 记录 `execution_profile` 与 `light_profile`、每个会话的实际 wrapper 成功/失败计数和最近一次执行。`PostToolUse` 以有界的最近 128 个 `tool_use_id + role` 键去重，可抵御非相邻的乱序事件重放且不会让状态无界增长；可写角色只有在事件与活跃租约精确匹配时才计数和释放。状态页因此能区分分类器的“最近建议”和 runtime 已确认的模型/执行结果。旧 schema 会原位补齐字段且保留当前模式。

GLM provider 健康状态位于 `~/.codex/smart-router/provider-health.json`，因此对所有项目共享。配额错误优先采用服务端 `next_flush_time`；短暂故障使用短冷却，认证失败在 Key 指纹变化前保持熔断，订阅异常使用较长冷却。到期后状态进入 half-open，并发调用中仅一个探针被放行。每次状态变化递增 generation；成功请求只有在其启动 generation 仍是当前 generation 时才能关闭熔断，避免较早的成功响应覆盖较新的 quota/auth 失败。

本地文本健康状态独立保存在 `local-provider-health.json`。运行或认证失败先打开短熔断，期间 scout/monitor 直接走 Luna；配置或 Key 指纹变化会重置熔断，到期后同样只允许一个半开探针。它不修改 GLM 重任务健康状态；GLM surrogate 因共享同一真实上游，只能验证逻辑隔离，无法模拟独立本地硬件故障。

GLM 运行时失败时，只读 reviewer 可安全重试 Terra。worker 只有在 Codex JSON 事件流包含明确终止事件、没有工具活动、Git 前后指纹都成功且未变化时才重试；非 Git 工作区、事件流不完整、指纹失败、超时或检测到变化时都禁止自动启动第二个 writer。这比无条件 fallback 更保守。

`GLM_FIRST` 的 worker/reviewer 和 `LOCAL_TEXT_FIRST` 的 scout/monitor 必须通过 MCP wrapper 执行，因为原生 agent TOML 无法在单次调用中动态切换 provider；hook 会拒绝这些动态角色的原生调用。其余固定 Luna 角色仍可按既有安全约束使用。

故障时采用 fail-open 执行、fail-safe 路由：hook 自身异常不阻塞 Codex，但不会建议子 agent；主 Sol 继续处理任务。清理 30 天未使用的状态文件不会影响项目文件。

agent 安装器只管理它自己安装且哈希仍匹配的文件。遇到同名用户文件或配置会拒绝覆盖；用户修改过的已安装文件/配置片段在卸载时也会保留并报告。

## 与 sol-advisor 的关系

[DannyMac180/sol-advisor](https://github.com/DannyMac180/sol-advisor) 适合作为路由治理的设计参考：默认由主模型执行、委派前明确声明、子任务契约自包含、失败时收紧权限、最终由父代理依据 diff 和运行证据验收。本插件借鉴这些原则，但没有复制其实现，也不把它作为运行时依赖或直接代码底座。原因是本插件还需要会话级开关、自动分类、MCP wrapper、写租约生命周期和安装/全局停用机制，直接从现有小型实现演进更可控。
