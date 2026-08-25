# 验证

在插件根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/probe_runtime.py
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/router-control
```

测试覆盖路由分类、否定词和中英文显式只读识别、角色级写授权、高风险写入拒绝、会话状态/profile 迁移与持久化、GLM profile 防串用及原生 worker/reviewer 拒绝、中文控制/状态反馈、建议与实际执行区分、PostTool 计数去重、紧凑上下文上限、MCP 写租约按调用 ID/角色释放、工作区进程锁阻止并发 writer、锁存活时禁止租约自愈、receipt 数量/长度校验、角色与 MCP 注册、wrapper 模型/provider 固定、Key 不进入命令参数与 child shell、工作日高峰边界、周末行为、Luna 路径稳定、多模态强制 Terra、严格 policy 校验、配额熔断/半开/generation 竞态、非 Git 或证据缺失时禁止 writer fallback、安装幂等、冲突保护、停用/恢复和安全卸载。

真实 Codex 冒烟建议在非高峰依次验证：环境就绪的新会话在 OFF 时无路由提示；`glm 开启` 后 profile 为 `GLM_FIRST`；只读盘点调用 Luna；纯文本审查调用 GLM-5.3 Max；带图片审查调用 Terra；模拟高峰选择 Terra；真实 GLM 错误能脱敏且安全回退；生产数据库迁移保留 Sol；受控临时仓库中的 worker 只改指定文件；SHADOW 不委派；关闭后不再注入路由指令。receipt 的 `_router_meta` 是实际模型/provider/effort 与 fallback 的验收证据。

真实测试不应通过耗尽套餐来制造配额错误。配额码、`next_flush_time`、half-open 与并发探针用确定性 fixture 验证；至少保留一次真实 GLM、一条真实 Luna、一条真实 Terra（含多模态时优先）调用，以覆盖 provider 兼容性。
