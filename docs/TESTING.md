# 验证

在插件根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/probe_runtime.py
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/router-control
```

测试覆盖路由分类、经济性门、原子单目标唯一委派槽、一次性 runtime lease 与 task digest 绑定、外部 agent 去重、原生 router agent 全面拒绝、确定性长等待/取消/超时、receipt v2/objective 绑定与严格限界、token/耗时台账，以及既有的写授权、高风险拒绝、profile、熔断、多模态、写租约、稳定 runtime、安装/卸载和密钥隔离行为。

真实 Codex 冒烟建议在非高峰依次验证：环境就绪的新会话在 OFF 时无路由提示；`glm 开启` 后重 profile 为 `GLM_FIRST`；`local 开启` 后轻 profile 为 `LOCAL_TEXT_FIRST`；只读盘点通过本地文本 provider；故障时改用 Luna；tester/docs 仍用 Luna；纯文本审查调用 GLM-5.3 Max；带图片审查调用 Terra；模拟高峰选择 Terra；生产数据库迁移保留 Sol；受控临时仓库中的 worker 只改指定文件；SHADOW 不委派；关闭后不再注入路由指令。receipt 的 `_router_meta` 是实际模型/provider/effort、两套 requested profile 与 fallback 的验收证据。

真实测试不应通过耗尽套餐来制造配额错误。配额码、`next_flush_time`、half-open 与并发探针用确定性 fixture 验证；至少保留一次真实 GLM 重任务、一条 GLM surrogate 轻任务、一条真实 Luna 回退和一条真实 Terra（含多模态时优先）调用。surrogate 通过不等于真实 DeepSeek 验收；切换正式内网 endpoint 后还需单独验证 Responses 兼容、工具调用、时延、并发、超时和断连恢复。

## 当前真实任务覆盖记录

截至 v0.3.1，当前构建已经真实验证：`jd-resume-optimizer`、`ppt_notes_pipeline_server-1.0.9`、`flash-sale-assistant`、`web-auto-translate-extension` 四个不同仓库上的只读侦察；GLM-5.3 surrogate 经自定义 Responses provider 实际执行；无效凭据触发 Local→Luna 实际回退；模型 receipt 超限被拒绝并在修复后回退；稳定 runtime 在模拟旧版本 cache 消失时继续执行 hook；从 `runtime-current` 发起的真实 surrogate 任务；Terra reviewer 对本插件稳定运行时补丁的真实只读审查；以及 Luna tester 在受限沙箱中执行 49 个目标测试和 9 个子测试并全部通过。首次 Luna 测试暴露了 hook 测试继承真实 `CODEX_HOME` 的隔离缺陷，修复为临时 home 后同一路径复测通过。

这不等于全面真实任务矩阵。尚未在 v0.3.1 最终构建上重新完成的真实分支包括：Luna docs 的受控写入、Terra worker 写入、GLM heavy reviewer、工作日高峰下真实 Terra 选择、Terra 多模态图片、长时间 monitor、多轮恢复/关闭以及实际 DeepSeek V4 Flash 内网端点。未覆盖项不得仅凭自动化测试表述为“全面真实验证”。

### v0.4.0 回归记录

v0.4.0 在四类真实路径上完成了回归：Luna scout 对 `jd-resume-optimizer` 的 37/37 范围盘点并返回 receipt v2；Luna tester 在 `web-auto-translate-extension/backend` 运行 12 个测试文件、144 项测试和 TypeScript 类型检查，全部通过；Terra worker 仅在受控临时 Git 仓库创建一个指定文件并核对哈希，随后删除该临时仓库；真实 GLM-5.3 reviewer 与 Terra reviewer 分别审查 v0.4 运行时补丁，并发现 objective 校验顺序、超时 usage 回收、非有限数值、租约竞争、取消处理、receipt 限界和路由泛化等问题，均已修复并由自动化回归覆盖。

最终候选版本的自动化套件包含 134 项测试，其中包括 MCP 子进程级取消测试，验证长等待期间主循环仍能接收 `notifications/cancelled`，且成功、超时、取消路径模型 token 均为零。两名独立 subagent reviewer 在最终修复后再次检查运行时并发/等待路径和策略/文档一致性，结论记录在发布交接中。

仍未真实覆盖：正式 DeepSeek V4 Flash 内网 endpoint、Terra 图片输入、工作日 14:00–18:00 的自然时钟真实高峰切换，以及持续数十分钟的生产进程等待。高峰逻辑、Responses 端点约束、长等待超时与取消已经由确定性测试覆盖，但不能替代上述真实环境验收。
