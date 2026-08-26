# Codex Smart Router

一个默认关闭、会话级开启的 Codex 路由插件。主会话继续使用 GPT-5.6 Sol；开启后，边界清晰的子任务按角色交给 GPT-5.6 Luna、GLM-5.3 Max、GPT-5.6 Terra 或可选的纯文本本地模型，自带安全门、结构化回执、独立熔断和不记录原始提示词的本地遥测。

## 安装与首次使用

1. 在 Codex 的 `/plugins` 中安装并启用 **Sol · GLM · Terra · Luna · Local 智能路由**。
2. 先预览 agent、配置注册和本地 wrapper 安装（含 TOML diff）：`python3 scripts/install_agents.py`
3. 确认后安装：`python3 scripts/install_agents.py --apply`。安装器会备份 `~/.codex/config.toml`，注册六个角色与 `smart_router` MCP，并把完整运行包写入 `~/.codex/smart-router/runtime-releases/`，再原子切换 `runtime-current` 稳定入口；遇到同名配置、符号链接路径或被修改的受管运行包会拒绝覆盖。
4. 如需 GLM 路由，运行 `python3 scripts/configure_glm.py`，在隐藏输入提示中粘贴 Coding Plan Key。密钥只写入 `~/.codex/smart-router/providers.env`（权限 `0600`），不会进入项目、命令行或子模型 shell。
5. 如需先用 GLM-5.3 验证未来的 DeepSeek 本地文本路径，运行 `python3 scripts/configure_local_provider.py --glm-surrogate`。它只写 provider 配置和模型目录，复用现有 `ZHIPU_API_KEY` 的环境变量名，不复制密钥。
6. 新开或恢复一个 Codex 会话，按需组合：
   - `$router-control 开启`：启用 `STABLE`，worker/reviewer 使用 Terra。
   - `$router-control glm 开启`：启用 `GLM_FIRST`，并自动开启当前会话路由。
   - `$router-control local 开启`：启用 `LOCAL_TEXT_FIRST`，并自动开启当前会话路由；scout 优先本地文本 provider，失败自动回退 Luna。
7. 此后直接正常提需求；恢复同一会话时无需再次开启。

每个新会话默认 `OFF + STABLE + LUNA_STABLE`。可随时使用 `$router-control 状态`、`$router-control 影子模式`、`glm/local 关闭` 或 `$router-control 关闭`。`glm 关闭` 和 `local 关闭` 只恢复各自的执行配置，不改变当前会话的 ON/OFF 状态。

插件升级后仍建议新开一个 Codex 会话，让新 skill、hook 定义和 MCP schema 在清晰边界上重新加载。已完成 `--apply` 的安装会让新 hook 优先使用 `runtime-current`，因此后续清理版本 cache 不会影响执行；稳定入口缺失时回退当前 `$PLUGIN_ROOT`，两者都缺失时 hook 静默放行，避免阻断发消息。升级前就已加载旧 hook 命令的活跃会话无法自动获得这项新逻辑，应新开会话；安装器不会假装能够改写已经载入内存的 hook。

## 执行分工与自动回退

- **Sol**：高风险、架构决策、破坏性操作、需求不确定和最终整合。
- **本地纯文本 provider**：`LOCAL_TEXT_FIRST` 下只承接批量只读 scout；配置缺失、凭据缺失、运行失败或熔断时自动改用 Luna。
- **Luna**：默认承接批量 scout、tester、docs；启用本地文本 provider 后仍保留 tester/docs 和只读回退。
- **GLM-5.3 Max**：`GLM_FIRST` 下的复杂纯文本 worker/reviewer。
- **Terra**：`STABLE` 下的 worker/reviewer；也承接图片等多模态任务和 GLM 回退。
- **确定性等待器**：等待进程结束、文件出现/消失或日志出现固定文本时，使用一次最长 3600 秒的阻塞 MCP 调用；不调用模型，也不让主 Agent 轮询。

GLM 默认在周一至周五 `14:00–18:00`（`Asia/Shanghai`）切换到 Terra，周末不切换。遇到 Coding Plan 配额错误时，插件从响应中的 `next_flush_time` 建立跨项目共享熔断；短暂故障、认证错误和订阅异常也会按不同策略降级。恢复时间到达后只放行一个半开探针，成功才关闭熔断。策略可在本地 `~/.codex/smart-router/policy.json` 覆盖，格式与默认值见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

v0.4.1 不再假设 GLM 官方端点或 MAAS 中转会严格执行 OpenAI `text.format/json_schema strict`。GLM 先返回较浅的 wire receipt，wrapper 再以无模型、确定性的 adapter 生成严格 receipt v2；格式偏差不会打开 provider 熔断。无法安全转换的只读任务才在同一个总 deadline 内回退 Terra，不再额外调用一个“格式修复模型”。内网 MAAS 可通过 policy 显式配置，见 [内网 GLM 接入](docs/INNER_NETWORK_GLM.md)。

本地文本 provider 使用独立的 60 秒运行时熔断，回退链为 `Local Text → Luna → Sol`；它不会打开或关闭重任务 GLM 熔断。当前 GLM-5.3 surrogate 只用于验证路由、Responses 协议、密钥隔离和回退，不代表 DeepSeek V4 Flash 的真实质量、时延、并发能力或端点兼容性。正式接入的内网服务必须提供 Codex 当前支持的 OpenAI Responses API；可用 `configure_local_provider.py --help` 查看无鉴权内网端点和自定义配置参数。

## 日常交互

- 环境正常且路由为 `OFF` 时，新会话保持静默，不占用注意力；开启只需一次，恢复同一会话时状态继续有效。
- `ON` 会先检查六个 agent 与 wrapper；环境缺失或被 `--disable` 停用时保持 `OFF`，并给出可直接复制的 `--apply` / `--enable` 命令。`SHADOW` 不依赖子模型运行时，仍可用于预览。
- `SHADOW` 会在答案末尾显示 `路由预览：…`，方便先观察规则是否符合预期。
- `ON` 中只有实际成功委派才显示 receipt 给出的真实标签，例如 `路由：Luna · …`、`路由：GLM-5.3 Max · …` 或 `路由：Terra · …`；子任务阻塞、失败或运行时不可用会显示 `路由回退：Sol（委派未完成）`。
- `$router-control 状态` 同时显示重/轻两套配置、provider 健康状态、最近路由建议和最近一次实际委派，便于确认究竟运行了哪个模型。
- 高风险或由 Sol 直接完成的普通任务不额外显示路由标签，减少日常噪音。
- 短小、紧耦合、预计少于约三个 Sol 回合的任务直接由 Sol 完成；批量盘点、成组测试和范围明确的较大任务才支付委派固定成本。

## 四层控制

- **插件全局开关**：Codex `/plugins` 中启用或关闭。关闭后，本插件的 hook 和技能不运行，本地 wrapper 即使仍被 Codex 列出也会拒绝执行。
- **会话路由开关**：`OFF / SHADOW / ON`。仅影响当前会话；恢复同一会话会保留状态。
- **会话重任务配置**：`STABLE / GLM_FIRST`，只影响 worker/reviewer。
- **会话轻任务配置**：`LUNA_STABLE / LOCAL_TEXT_FIRST`，只影响 scout；等待路径始终无模型；与重任务配置正交，可同时开启。

自定义 agent 存放在 `~/.codex/agents/`，版本化运行包与稳定入口位于 `~/.codex/smart-router/runtime-releases/` 和 `runtime-current`，角色和本地 wrapper 注册在 `~/.codex/config.toml`，都是插件外的 Codex 用户配置。全局关闭插件不会删除它们，但 wrapper 会 fail closed；可运行 `python3 scripts/install_agents.py --disable` 临时停用 agent 与 wrapper，或 `--uninstall` 安全卸载仍与安装哈希一致的受管文件与配置片段。

## 设计边界

- 高风险、架构性、破坏性或无法可靠分类的任务始终留给 Sol。
- 写入角色采用显式授权：提示词必须包含与角色相符、未被否定的正向写操作；“只读”“不要实现/修复/更新”等表达不会获得 `workspace-write`。
- hook 是实用护栏，不是安全沙箱；Codex 自身权限、审批和 sandbox 仍是最终边界。
- 默认只保留脱敏遥测：时间、模式、路由角色、原因代码和提示词哈希，不记录提示词正文。
- 每个用户目标默认只有一个委派槽；已经派出 Hegel 等外部 subagent 时，同一目标不会再追加 Luna/Terra 审核。路由角色禁用原生 spawn，统一通过同步 MCP 执行并在返回时自动恢复主 Sol。hook 以文件锁原子消费槽位，并签发一次性 `lease_id`；MCP 再校验 decision、role 与 task digest 后才启动任务。每次 wrapper 写租约绑定 `tool_use_id`；运行时漏发完成事件时，下一条用户任务只会在工作区进程锁证明没有 writer 运行时清理遗留租约。
- receipt v2 严格拒绝额外字段并限制每个 manifest 字段长度，记录覆盖范围、不一致项、结构化证据清单和最多三项父级抽查建议。主 Agent 只复核关键哈希、异常和少量样本，不重新通读已覆盖材料。执行元数据只记录模型、provider、耗时和 token usage，不保存子模型推理正文。
- hook 对单次附加上下文设置 512 token 上限；默认 `OFF` 的正常新会话不注入上下文。逐轮路由提示采用紧凑契约，receipt 也限制字段、条目数和单项长度，避免长会话中路由元数据无界增长。
- 主执行路径是本地 `smart_router.route_task`：它调用隔离的 `codex exec`，固定角色模型/provider，并始终在主进程校验最终 receipt v2。OpenAI 模型使用 strict schema；GLM 使用 `json_object_adapter`，不把上游“接受了 schema 参数”等同于“真正执行了 schema”。本地配置不能注入任意 TOML；HTTP 默认只接受 localhost 或私有/回环 IP，其他 HTTP hostname 需要显式允许。GLM 运行失败后，只有只读任务或可以确认没有发生写入的 worker 才自动重试 Terra；出现工具活动或工作区变化时禁止第二个 writer，交回 Sol 处理。`run_agent.py` 也可单独用于诊断。

验证方法见 [TESTING.md](docs/TESTING.md)，架构与故障恢复见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。
