---
name: router-control
description: "显式控制当前 Codex 会话中的 Sol/GLM/Terra/Luna/本地文本模型智能路由：开启 ON、切换重/轻任务 profile、影子评估、关闭、查看状态或解释路由。仅在用户点名 $router-control 或明确要求控制本插件时使用。"
---

# Smart Router 控制

只控制和解释路由，不修改项目文件。优先原样采用 hook 注入的 `SMART_ROUTER_UI_REPLY`，不要猜测状态。

## 命令

- `$router-control 开启`：本会话自动判断；低风险任务可委派，高风险任务保留给 Sol。
- `$router-control glm 开启`：同时开启路由并切换为 `GLM_FIRST`。Luna 继续处理轻任务；复杂纯文本 worker/reviewer 优先 GLM-5.3 Max；工作日 14:00–18:00（Asia/Shanghai）、GLM 熔断或存在必需图片时改用 Terra。
- `$router-control glm 关闭`：回到 `STABLE`，但不改变当前会话的 ON/OFF 状态。
- `$router-control local 开启`：同时开启路由并切换为 `LOCAL_TEXT_FIRST`。批量只读 scout 优先已配置的本地文本 provider，失败或熔断时回退 Luna；tester/docs 保持 Luna。
- `$router-control local 关闭`：回到 `LUNA_STABLE`，但不改变当前会话的 ON/OFF 状态。
- `$router-control 影子模式`：只显示路由预览，不委派。
- `$router-control 关闭`：本会话全部由 Sol 处理。
- `$router-control 状态`：显示模式、环境、最近建议与实际执行。
- `$router-control 帮助`：显示命令和全局开关说明。

也接受 `/router on|shadow|off|status|help`、`/router glm on|off` 与 `/router local on|off` 形式。状态仅绑定当前 Codex 会话；恢复同一会话会继续保留，新会话默认 `OFF + STABLE + LUNA_STABLE`。重任务和轻任务 profile 正交，可同时开启。

环境未就绪时，给出插件目录中的 `python3 scripts/install_agents.py --apply`，不要自动安装。全局关闭使用 `/plugins`；本地 agent/wrapper 可由安装器的 `--disable` 或 `--uninstall` 管理。

v0.4 每个用户目标默认只允许一次委派。路由角色必须通过同步 `smart_router.route_task` 执行，禁止原生 spawn；等待类任务使用一次 `smart_router.wait_for_condition`，不得让模型轮询。已有合格 receipt 时，只验收其中的 `parent_verification`、异常和少量抽样，不重新读取全部覆盖材料。

v0.4.1 中 GLM receipt 由 wrapper 的无模型 `json_object_adapter` 规范化；不要把 `glm_receipt_format_failure` 表述成 GLM 服务宕机，也不要因此建议用户重置 provider 熔断。GLM 与 Terra fallback 共用同一个总 timeout；不得追加一个模型格式修复步骤。

GLM 凭据未配置时，提示用户在插件目录运行 `python3 scripts/configure_glm.py`；不得在对话、项目文件、agent TOML 或遥测中收集/回显 Key。图片委派只传递任务真正需要的本地图片路径；纯文本 PDF 先提取文本，视觉 PDF 先转成需要的页面图像。

本地文本 provider 未配置时，提示用户运行 `python3 scripts/configure_local_provider.py --help`；若用户正在用 GLM 模拟 DeepSeek 路径，则使用 `--glm-surrogate`。必须称其为 surrogate，不得把测试结果表述为 DeepSeek 的真实性能或兼容性结论。

需要解释详细规则时读取 [routing-policy.md](references/routing-policy.md)。
