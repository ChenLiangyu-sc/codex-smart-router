# Codex Smart Router

一个默认关闭、会话级开启的 Codex 路由插件。主会话继续使用 GPT-5.6 Sol；开启后，低风险且边界清晰的子任务可交给 GPT-5.6 Terra/Luna，自带安全门、结构化回执和不记录原始提示词的本地遥测。

## 安装与首次使用

1. 在 Codex 的 `/plugins` 中安装并启用 **Sol · Terra · Luna 智能路由**。
2. 先预览 agent、配置注册和本地 wrapper 安装（含 TOML diff）：`python3 scripts/install_agents.py`
3. 确认后安装：`python3 scripts/install_agents.py --apply`。安装器会备份 `~/.codex/config.toml`，注册六个角色与 `smart_router` MCP，遇到同名配置会拒绝覆盖。
4. 新开或恢复一个 Codex 会话，输入 `$router-control 开启`。
5. 此后直接正常提需求；无需每次再次开启。

每个新会话默认 `OFF`。可随时使用 `$router-control 状态`、`$router-control 影子模式` 或 `$router-control 关闭`。

## 日常交互

- 环境正常且路由为 `OFF` 时，新会话保持静默，不占用注意力；开启只需一次，恢复同一会话时状态继续有效。
- `ON` 会先检查六个 agent 与 wrapper；环境缺失或被 `--disable` 停用时保持 `OFF`，并给出可直接复制的 `--apply` / `--enable` 命令。`SHADOW` 不依赖子模型运行时，仍可用于预览。
- `SHADOW` 会在答案末尾显示 `路由预览：…`，方便先观察规则是否符合预期。
- `ON` 中只有实际成功委派才显示 `路由：Luna · …` 或 `路由：Terra · …`；子任务阻塞、失败或运行时不可用会显示 `路由回退：Sol（委派未完成）`，不会把建议误报成执行。
- `$router-control 状态` 同时显示最近路由建议和最近一次实际委派，便于确认 Terra/Luna 是否真的运行。
- 高风险或由 Sol 直接完成的普通任务不额外显示路由标签，减少日常噪音。

## 两个开关

- **插件全局开关**：Codex `/plugins` 中启用或关闭。关闭后，本插件的 hook 和技能不运行，本地 wrapper 即使仍被 Codex 列出也会拒绝执行。
- **会话路由开关**：`OFF / SHADOW / ON`。仅影响当前会话；恢复同一会话会保留状态。

自定义 agent 存放在 `~/.codex/agents/`，角色和本地 wrapper 注册在 `~/.codex/config.toml`，都是插件外的 Codex 用户配置。全局关闭插件不会删除它们，但 wrapper 会 fail closed；可运行 `python3 scripts/install_agents.py --disable` 临时停用 agent 与 wrapper，或 `--uninstall` 安全卸载本插件管理的文件与配置片段。

## 设计边界

- 高风险、架构性、破坏性或无法可靠分类的任务始终留给 Sol。
- 写入角色采用显式授权：提示词必须包含与角色相符、未被否定的正向写操作；“只读”“不要实现/修复/更新”等表达不会获得 `workspace-write`。
- hook 是实用护栏，不是安全沙箱；Codex 自身权限、审批和 sandbox 仍是最终边界。
- 默认只保留脱敏遥测：时间、模式、路由角色、原因代码和提示词哈希，不记录提示词正文。
- v0.1 只允许一层委派，最多一个写入 agent。每次 wrapper 调用的写租约绑定 `tool_use_id`，优先在成功或失败的 `PostToolUse` 后释放；若运行时漏发该事件，下一条用户任务只会在工作区进程锁证明没有 writer 运行时清理遗留租约。receipt 只允许一次自动补正。
- hook 对单次附加上下文设置 512 token 上限；默认 `OFF` 的正常新会话不注入上下文。逐轮路由提示采用紧凑契约，receipt 也限制字段、条目数和单项长度，避免长会话中路由元数据无界增长。
- 主执行路径是本地 `smart_router.route_task`：它调用当前 ChatGPT OAuth 登录下的 `codex exec`，显式固定角色模型并用 JSON Schema 校验 receipt。`run_agent.py` 也可单独用于诊断。

验证方法见 [TESTING.md](docs/TESTING.md)，架构与故障恢复见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。
