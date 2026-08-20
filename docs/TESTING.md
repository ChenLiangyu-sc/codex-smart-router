# 验证

在插件根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/probe_runtime.py
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/router-control
```

测试覆盖路由分类、否定词和中英文显式只读识别、角色级写授权、高风险写入拒绝、会话状态迁移与持久化、中文控制/状态反馈、建议与实际执行区分、PostTool 计数去重、紧凑上下文上限、MCP 写租约按调用 ID/角色释放、工作区进程锁阻止并发 writer、锁存活时禁止租约自愈、receipt 数量/长度校验、角色与 MCP 注册、wrapper 模型固定、安装幂等、冲突保护、停用/恢复和安全卸载。

真实 Codex 冒烟建议依次验证：环境就绪的新会话在 OFF 时无路由提示；开启确认是简洁中文；包含“不要实现、不要修复、不要改代码”的只读盘点只能调用 Luna scout 且答案末尾显示实际路由标签；状态页显示 Luna 实际成功一次；只读审查调用 Terra reviewer；生产数据库迁移任务保留 Sol且不显示路由标签；同一会话连续两次边界清晰的 Terra worker 写任务均成功且第二次不被旧租约阻塞；SHADOW 只展示路由预览不委派；关闭后不再注入路由指令。receipt 的 `_router_meta` 是实际模型/effort 的验收证据。
