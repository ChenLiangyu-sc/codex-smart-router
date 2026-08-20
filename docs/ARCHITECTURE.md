# 架构与故障恢复

`UserPromptSubmit` hook 读取当前 `session_id` 的状态，识别控制命令，并对普通任务做保守分类。`ON` 时，它把建议角色、风险和约束作为 developer context 注入主 Sol 会话；Sol 调用本地 `smart_router.route_task` typed tool，wrapper 再以固定模型启动临时 Codex child。

正常且默认关闭的新会话不注入额外上下文。`SessionStart` 只在安装未就绪或恢复一个已开启/影子会话时提供紧凑提示；`SessionStart` 与 `UserPromptSubmit` 均设置 512 token 的附加上下文上限。普通路由契约只携带角色、写权限、调用/回退规则和用户可见结果标签，不重复完整策略。完整规则按需保留在 skill reference 中。

receipt 的数组数量和字段长度有界，但 findings/evidence 为完整路径和完整句子预留 800 字符，避免紧约束迫使模型拆碎证据。运行时会拒绝恰好撞上硬上限的疑似截断项，以及 `evidence`、`validation` 等字段名碎片；这类结果按 wrapper 失败处理，由 Sol 明确回退，而不是把语义破损的 receipt 当成成功证据。

`PreToolUse` 对本插件的 `router_*` agent 和 wrapper 工具做二次检查：OFF/SHADOW 禁止路由，ON 时要求角色与当前判断一致。可写角色还必须有本轮分类产生的 `write_authorized=true`；该授权只接受与角色相符、未被否定的正向写操作，并拒绝显式只读和高风险任务。wrapper 在子进程入口再次执行同一授权检查，避免主任务与下发任务不一致时扩大权限。

原生 subagent 路径要求 `fork_turns=none`。wrapper 使用 `--output-schema` 强制 receipt；原生 `SubagentStop` 第一次不合格可要求补正，之后不再循环。

同一会话最多一个写入 agent。`PreToolUse` 创建的租约记录角色、来源、工作区、turn 与当前 `tool_use_id`：原生 agent 由 `SubagentStop` 清理，MCP wrapper 无论成功或失败都优先由对应 `PostToolUse` 清理。释放时必须同时匹配来源、角色和调用 ID。wrapper 执行期间还持有按工作区哈希隔离的 OS 文件锁，防止跨轮次、跨会话或 hook 状态竞争导致两个 writer 同时写同一工作区。

若 Codex 在某条失败路径漏发 `PostToolUse`，下一次 `UserPromptSubmit` 只会在 OS 锁证明没有 writer 运行时清理其 MCP 租约；锁仍被占用时保持 fail-closed。安装新版本后，无调用 ID 的 v0.1 遗留租约会被视为不可归属状态并安全清除。

安装器在 `[agents]` 下注册六个 `config_file`，并以绝对路径注册 `[mcp_servers.smart_router]`。它使用带标记的最小配置片段、安装前备份和哈希所有权，卸载时只移除仍完全匹配的受管内容。

状态和遥测默认位于 Codex 分配的 `$PLUGIN_DATA`。若环境没有该变量，则退回 `~/.codex/plugin-data/codex-smart-router/`。状态文件按会话 ID 的 SHA-256 保存，避免把 ID 暴露在文件名中。

状态 schema v3 还记录每个会话的实际 wrapper 成功/失败计数和最近一次执行。`PostToolUse` 以有界的最近 128 个 `tool_use_id + role` 键去重，可抵御非相邻的乱序事件重放且不会让状态无界增长；可写角色只有在事件与活跃租约精确匹配时才计数和释放。状态页因此能区分分类器的“最近建议”和 runtime 已确认的“最近实际执行”。旧 schema 会原位补齐字段且保留当前模式。

故障时采用 fail-open 执行、fail-safe 路由：hook 自身异常不阻塞 Codex，但不会建议子 agent；主 Sol 继续处理任务。清理 30 天未使用的状态文件不会影响项目文件。

agent 安装器只管理它自己安装且哈希仍匹配的文件。遇到同名用户文件或配置会拒绝覆盖；用户修改过的已安装文件/配置片段在卸载时也会保留并报告。

## 与 sol-advisor 的关系

[DannyMac180/sol-advisor](https://github.com/DannyMac180/sol-advisor) 适合作为路由治理的设计参考：默认由主模型执行、委派前明确声明、子任务契约自包含、失败时收紧权限、最终由父代理依据 diff 和运行证据验收。本插件借鉴这些原则，但没有复制其实现，也不把它作为运行时依赖或直接代码底座。原因是本插件还需要会话级开关、自动分类、MCP wrapper、写租约生命周期和安装/全局停用机制，直接从现有小型实现演进更可控。
