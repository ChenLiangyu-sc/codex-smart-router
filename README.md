# Codex Smart Router

一个默认关闭、会话级开启的 Codex 路由插件。主会话继续使用 GPT-5.6 Sol；开启后，边界清晰的子任务按角色交给 GLM-5.3、GPT-5.6 Terra、可选的纯文本本地模型，或手动开启的 GPT-5.6 Luna，自带安全门、结构化回执、独立熔断和不记录原始提示词的本地遥测。

## 安装与首次使用

1. 在 Codex 的 `/plugins` 中安装并启用 **Sol · GLM · Terra · Luna · Local 智能路由**。
2. 先预览 agent、配置注册和本地 wrapper 安装（含 TOML diff）：`python3 scripts/install_agents.py`
3. 确认后安装：`python3 scripts/install_agents.py --apply`。安装器会备份 `~/.codex/config.toml`，注册六个角色与 `smart_router` MCP，并把完整运行包写入 `~/.codex/smart-router/runtime-releases/`，再原子切换 `runtime-current` 稳定入口；遇到同名配置、符号链接路径或被修改的受管运行包会拒绝覆盖。
4. 如需 GLM 路由，运行 `python3 scripts/configure_glm.py`，在隐藏输入提示中粘贴 Coding Plan Key。密钥只写入 `~/.codex/smart-router/providers.env`（权限 `0600`），不会进入项目、命令行或子模型 shell。
5. 如需先用 GLM-5.3 验证未来的 DeepSeek 本地文本路径，运行 `python3 scripts/configure_local_provider.py --glm-surrogate`。它只写 provider 配置和模型目录，复用现有 `ZHIPU_API_KEY` 的环境变量名，不复制密钥。
6. 新开或恢复一个 Codex 会话，按需组合：
   - `$router-control 开启`：启用 `STABLE`，worker/reviewer 使用 Terra；Luna 保持默认关闭。
   - `$router-control glm 开启`：启用 `GLM_FIRST`，并自动开启当前会话路由。
   - `$router-control local 开启`：启用 `LOCAL_TEXT_FIRST`，并自动开启当前会话路由；scout 优先本地文本 provider，失败按后备链改用其他执行器。
   - `$router-control luna 开启`：启用智能路由并显式开启 Luna；`luna 关闭` 只关 Luna，不改变路由 ON/OFF。
   - `$router-control 经济策略 v1`：临时恢复 v0.4.1 的 `work_units` 兼容门；`经济策略 v2` 切回保守静态门。
7. 此后直接正常提需求；恢复同一会话时无需再次开启。

每个新会话默认 `OFF + STABLE + Local 关闭 + Luna 关闭 + V2_STATIC`（轻任务内部枚举 `LUNA_STABLE` 仅表示未启用 Local 首选，不代表 Luna 开启）：Luna 默认关闭，不作为任何角色首选或隐藏回退；只有 `luna 开启` 后才以 `LUNA_BOUNDED` 承接低风险 bounded 轻任务。旧会话状态迁移时，v0.4.2 及以前隐式启用的 Luna 一律迁移为关闭，因为旧版本没有显式 Luna 授权。可随时使用 `$router-control 状态`、`$router-control 影子模式`、`glm/local/luna 关闭`、`经济策略 v1/v2` 或 `$router-control 关闭`。profile、Luna 和经济门切换都不改变当前会话的 ON/OFF 状态（`glm/local/luna 开启` 会同时把路由设为 ON）。

插件升级后仍建议新开一个 Codex 会话，让新 skill、hook 定义和 MCP schema 在清晰边界上重新加载。已完成 `--apply` 的安装会让新 hook 优先使用 `runtime-current`，因此后续清理版本 cache 不会影响执行；稳定入口缺失时回退当前 `$PLUGIN_ROOT`，两者都缺失时 hook 静默放行，避免阻断发消息。升级前就已加载旧 hook 命令的活跃会话无法自动获得这项新逻辑，应新开会话；安装器不会假装能够改写已经载入内存的 hook。

## 执行分工与自动回退

- **Sol**：高风险、架构决策、破坏性操作、需求不确定和最终整合。
- **本地纯文本 provider**：`LOCAL_TEXT_FIRST` 下只承接批量只读 scout；运行失败或熔断时按后备链改用下一个执行器（Luna 开启时是 Luna，否则是 GLM/Terra）。
- **Luna**：默认关闭。手动开启后仅承接低风险、边界明确的 scout/tester/docs 轻任务，不参与复杂 worker/reviewer、多模态审查或高风险写入。
- **GLM-5.3**：`GLM_FIRST` 下的复杂纯文本 worker/reviewer；Luna 关闭时也可按链承接轻任务。
- **Terra**：`STABLE` 下的 worker/reviewer；轻任务链的终端执行器；也承接图片等多模态任务和 GLM 回退。
- **确定性等待器**：等待进程结束、文件出现/消失或日志出现固定文本时，使用一次最长 3600 秒的阻塞 MCP 调用；不调用模型，也不让主 Agent 轮询。
- **确定性工具 fast path**：文件存在性、精确搜索、hash/元数据、`git status`、schema 校验和单次现有测试命令留在 Sol 当前回合，用最少直接工具调用完成，不启动 child。

每个 routed task 最多实际调用两个模型。轻任务优先级为：scout 且 `LOCAL_TEXT_FIRST`、Local 可用 → Local；Luna 已开启且任务符合 `LUNA_BOUNDED` → Luna；`GLM_FIRST` 且 GLM 可用、非高峰 → GLM；否则 Terra。选择阶段因配置缺失、Key 缺失、熔断或高峰被跳过的 provider 不计入模型 attempt。允许的对包括 Local → Luna / GLM / Terra、Luna → Terra、GLM → Terra；类似 Local → GLM → Terra 的三连回退被硬性禁止。

GLM 默认在周一至周五 `14:00–18:00`（`Asia/Shanghai`）切换到 Terra，周末不切换。遇到 Coding Plan 配额错误时，插件从响应中的 `next_flush_time` 建立跨项目共享熔断；短暂故障、认证错误和订阅异常也会按不同策略降级。恢复时间到达后只放行一个半开探针，成功才关闭熔断。策略可在本地 `~/.codex/smart-router/policy.json` 覆盖，格式与默认值见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

v0.4.1 不再假设 GLM 官方端点或 MAAS 中转会严格执行 OpenAI `text.format/json_schema strict`。GLM 先返回较浅的 wire receipt，wrapper 再以无模型、确定性的 adapter 生成严格 receipt v2；格式偏差不会打开 provider 熔断。无法安全转换的只读任务才在同一个总 deadline 内回退链上下一个执行器，不再额外调用一个“格式修复模型”。内网 MAAS 可通过 policy 显式配置，见 [内网 GLM 接入](docs/INNER_NETWORK_GLM.md)。

本地文本 provider 使用独立的 60 秒运行时熔断，回退走 planner 链；它不会打开或关闭重任务 GLM 熔断。当前 GLM-5.3 surrogate 只用于验证路由、Responses 协议、密钥隔离和回退，不代表 DeepSeek V4 Flash 的真实质量、时延、并发能力或端点兼容性。正式接入的内网服务必须提供 Codex 当前支持的 OpenAI Responses API；可用 `configure_local_provider.py --help` 查看无鉴权内网端点和自定义配置参数。

## 日常交互

- 环境正常且路由为 `OFF` 时，新会话保持静默，不占用注意力；开启只需一次，恢复同一会话时状态继续有效。
- `ON` 会先检查六个 agent 与 wrapper；环境缺失或被 `--disable` 停用时保持 `OFF`，并给出可直接复制的 `--apply` / `--enable` 命令。`SHADOW` 不依赖子模型运行时，仍可用于预览。
- `SHADOW` 会在答案末尾显示 `路由预览：…`，方便先观察规则是否符合预期。
- `ON` 中只有实际成功委派才显示 receipt 给出的真实标签，例如 `路由：GLM-5.3 · …` 或 `路由：Terra · …`；子任务阻塞、失败或运行时不可用会显示 `路由回退：Sol（委派未完成）`。
- `$router-control 状态` 同时显示重/轻两套配置、Luna 开关、provider 健康状态、最近路由建议和最近一次实际委派的完整回退链（实际执行路径、回退原因、每个 attempt 的耗时与 token，以及“某 provider 未尝试”的选择期原因），便于确认究竟运行了哪个模型。
- 路由为 `ON` 时，所有原生 `Agent`/`spawn_agent`（包括 explorer、worker、reviewer 和外部插件 agent）都会被拒绝：有当前 `DELEGATE` decision 时提示改用同步 `smart_router.route_task`；没有 decision（例如自动 Goal continuation——Codex 对它不触发 `UserPromptSubmit`）时要求 Sol inline 完成且不得复用旧 lease。需要原生 subagent 时请先关闭 Smart Router。`OFF`/`SHADOW` 保持原生 agent 兼容。
- 高风险或由 Sol 直接完成的普通任务不额外显示路由标签，减少日常噪音。
- `V2_STATIC` 不再让“仓库、目录、多个、路径、manifest、长提示词”等弱信号直接推动委派。单文件、单工具、微小修改、短验证和缺少独立边界的任务留给 Sol；只有达到角色规模门的 bounded package 才支付 child 固定成本。
- 同一用户目标内 4–12 个同角色只读项会被提示合并进一次 child 调用；不会跨用户轮次等待凑批，也不会合并 writer。

## 五层控制

- **插件全局开关**：Codex `/plugins` 中启用或关闭。关闭后，本插件的 hook 和技能不运行，本地 wrapper 即使仍被 Codex 列出也会拒绝执行。
- **会话路由开关**：`OFF / SHADOW / ON`。仅影响当前会话；恢复同一会话会保留状态。
- **会话重任务配置**：`STABLE / GLM_FIRST`，影响 worker/reviewer，也在 Luna 关闭时为轻任务提供 GLM 候选。
- **会话轻任务配置**：`LUNA_STABLE / LOCAL_TEXT_FIRST`（用户界面显示为 Local 关闭/开启），只影响 scout 的 Local 首选；等待路径始终无模型；与重任务配置正交，可同时开启。
- **会话 Luna 开关**：默认 `LUNA_DISABLED`；`luna 开启` 后为 `LUNA_BOUNDED`，仅限低风险 bounded 轻任务。
- **会话经济门**：默认 `V2_STATIC`；`V1_COMPAT` 是升级回退开关，只恢复 v0.4.1 的 `work_units` 判断，不改变安全、权限、单委派和 provider 规则。

自定义 agent 存放在 `~/.codex/agents/`，版本化运行包与稳定入口位于 `~/.codex/smart-router/runtime-releases/` 和 `runtime-current`，角色和本地 wrapper 注册在 `~/.codex/config.toml`，都是插件外的 Codex 用户配置。全局关闭插件不会删除它们，但 wrapper 会 fail closed；可运行 `python3 scripts/install_agents.py --disable` 临时停用 agent 与 wrapper，或 `--uninstall` 安全卸载仍与安装哈希一致的受管文件与配置片段。

## 设计边界

- 高风险、架构性、破坏性或无法可靠分类的任务始终留给 Sol。
- 写入角色采用显式授权：提示词必须包含与角色相符、未被否定的正向写操作；“只读”“不要实现/修复/更新”等表达不会获得 `workspace-write`。
- hook 是实用护栏，不是安全沙箱；Codex 自身权限、审批和 sandbox 仍是最终边界。
- 默认只保留脱敏遥测：时间、模式、任务桶、gate 特征、路由角色、原因代码和提示词哈希，不记录提示词正文。当前 hook API 无法可靠提供主 Sol inline token 与父级验收 token，相关字段明确标记为 unavailable，不伪造 P75/P50 成本。
- 每个用户目标默认只有一个委派槽。路由为 `ON` 时原生 `Agent`/`spawn_agent` 一律拒绝（包括 Hegel 等外部插件 agent），拒绝不消费委派槽；有当前 `DELEGATE` decision 时提示改用同步 `smart_router.route_task`，没有 decision（如自动 Goal continuation）时要求 Sol inline 完成且不得复用旧 lease。`OFF`/`SHADOW` 保持原生 agent 兼容。hook 以文件锁原子消费槽位，并签发一次性 `lease_id`；MCP 再校验 decision、role 与 task digest 后才启动任务。每次 wrapper 写租约绑定 `tool_use_id`；运行时漏发完成事件时，下一条用户任务只会在工作区进程锁证明没有 writer 运行时清理遗留租约。
- receipt v2 严格拒绝额外字段并限制每个 manifest 字段长度，记录覆盖范围、不一致项、结构化证据清单和最多三项父级抽查建议。主 Agent 只复核关键哈希、异常和少量样本，不重新通读已覆盖材料。usage adapter 优先只累计 `turn.completed` 的 per-turn usage；旧事件流只取最后一个 snapshot，避免重复累加，并分别保留 cached/cache-write/reasoning 字段及 attempt 级 fallback 台账。
- hook 对单次附加上下文设置 512 token 上限；默认 `OFF` 的正常新会话不注入上下文。逐轮路由提示采用紧凑契约，receipt 也限制字段、条目数和单项长度，避免长会话中路由元数据无界增长。
- 主执行路径是本地 `smart_router.route_task`：它调用隔离的 `codex exec`，固定角色模型/provider，并始终在主进程校验最终 receipt v2。OpenAI 模型使用 strict schema；GLM 使用 `json_object_adapter`，不把上游“接受了 schema 参数”等同于“真正执行了 schema”。本地配置不能注入任意 TOML；HTTP 默认只接受 localhost 或私有/回环 IP，其他 HTTP hostname 需要显式允许。GLM 运行失败后，只有只读任务或可以确认没有发生写入的 worker 才自动重试 Terra；出现工具活动或工作区变化时禁止第二个 writer，交回 Sol 处理。`run_agent.py` 也可单独用于诊断。

验证方法见 [TESTING.md](docs/TESTING.md)，架构与故障恢复见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。
