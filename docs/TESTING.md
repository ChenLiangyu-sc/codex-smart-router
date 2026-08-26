# 验证

在插件根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/probe_runtime.py
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/router-control
```

测试覆盖路由分类、否定词和中英文显式只读识别、角色级写授权、高风险写入拒绝、会话状态及重/轻 profile 迁移与持久化、动态 profile 防串用和原生 agent 拒绝、中文控制/状态反馈、建议与实际执行区分、PostTool 计数去重、紧凑上下文上限、MCP 写租约按调用 ID/角色释放、工作区进程锁阻止并发 writer、锁存活时禁止租约自愈、receipt 数量/长度校验、角色与 MCP 注册、稳定 runtime 在版本 cache 消失后的 hook 连续性、稳定入口与完整 release 原子切换、同版本并发发布收敛、双运行时缺失和 Python 非零时 hook 放行、未受管 runtime-current 拒绝、包内中间符号链接拒绝、卸载冲突的全局预检、wrapper 模型/provider 固定、Key 不进入命令参数与 child shell、工作日高峰边界、周末行为、多模态强制 Terra、严格 provider/policy 校验、GLM 与本地文本独立熔断/半开/generation 竞态、本地文本到 Luna 的只读回退、tester/docs 保持 Luna、非 Git 或证据缺失时禁止 writer fallback、安装幂等、停用/恢复和安全卸载。

真实 Codex 冒烟建议在非高峰依次验证：环境就绪的新会话在 OFF 时无路由提示；`glm 开启` 后重 profile 为 `GLM_FIRST`；`local 开启` 后轻 profile 为 `LOCAL_TEXT_FIRST`；只读盘点通过本地文本 provider；故障时改用 Luna；tester/docs 仍用 Luna；纯文本审查调用 GLM-5.3 Max；带图片审查调用 Terra；模拟高峰选择 Terra；生产数据库迁移保留 Sol；受控临时仓库中的 worker 只改指定文件；SHADOW 不委派；关闭后不再注入路由指令。receipt 的 `_router_meta` 是实际模型/provider/effort、两套 requested profile 与 fallback 的验收证据。

真实测试不应通过耗尽套餐来制造配额错误。配额码、`next_flush_time`、half-open 与并发探针用确定性 fixture 验证；至少保留一次真实 GLM 重任务、一条 GLM surrogate 轻任务、一条真实 Luna 回退和一条真实 Terra（含多模态时优先）调用。surrogate 通过不等于真实 DeepSeek 验收；切换正式内网 endpoint 后还需单独验证 Responses 兼容、工具调用、时延、并发、超时和断连恢复。

## 当前真实任务覆盖记录

截至 v0.3.1，当前构建已经真实验证：`jd-resume-optimizer`、`ppt_notes_pipeline_server-1.0.9`、`flash-sale-assistant`、`web-auto-translate-extension` 四个不同仓库上的只读侦察；GLM-5.3 surrogate 经自定义 Responses provider 实际执行；无效凭据触发 Local→Luna 实际回退；模型 receipt 超限被拒绝并在修复后回退；稳定 runtime 在模拟旧版本 cache 消失时继续执行 hook；从 `runtime-current` 发起的真实 surrogate 任务；Terra reviewer 对本插件稳定运行时补丁的真实只读审查；以及 Luna tester 在受限沙箱中执行 49 个目标测试和 9 个子测试并全部通过。首次 Luna 测试暴露了 hook 测试继承真实 `CODEX_HOME` 的隔离缺陷，修复为临时 home 后同一路径复测通过。

这不等于全面真实任务矩阵。尚未在 v0.3.1 最终构建上重新完成的真实分支包括：Luna docs 的受控写入、Terra worker 写入、GLM heavy reviewer、工作日高峰下真实 Terra 选择、Terra 多模态图片、长时间 monitor、多轮恢复/关闭以及实际 DeepSeek V4 Flash 内网端点。未覆盖项不得仅凭自动化测试表述为“全面真实验证”。
