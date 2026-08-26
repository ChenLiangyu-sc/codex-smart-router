# 内网 GLM / MAAS 接入

v0.4.1 对官方 GLM 与 MAAS 中转使用同一套代码；差异只放在本地 policy，不需要维护内网 fork。先安装插件和稳定 runtime，再在 `~/.codex/smart-router/policy.json` 写入：

```json
{
  "glm_base_url": "http://<internal-maas-host>/v1",
  "allow_insecure_glm_http": true
}
```

内网已有 HTTPS 时应使用 HTTPS 并保持 `allow_insecure_glm_http=false`。URL 不得包含用户名、密码、query 或 fragment。Key 仍通过 `python3 scripts/configure_glm.py` 写入权限为 `0600` 的 `providers.env`，不要放进 policy、项目或命令行。

在工作日非 `14:00–18:00`（Asia/Shanghai）执行一次只读验收：

```bash
python3 scripts/run_agent.py \
  --role router_reviewer \
  --profile GLM_FIRST \
  --timeout 120 \
  --task '只读检查一个明确的小文件并返回一项可复核证据；不要修改文件。'
```

验收 `_router_meta`：`provider` 应是 `zhipu_glm_coding`，`receipt_mode` 应是 `json_object_adapter`。正常可转换时 `attempted_executors` 只有 `glm_reviewer`；无法安全转换时只读任务直接出现 `terra_reviewer` 和 `fallback_reason=glm_receipt_format_failure`。格式失败不应把 `provider-health.json` 打开，也不会启动第二个格式模型。

建议再做三条内网 fixture：用户报告的 `status=pass / object[] / coverage.scope / path=null` 应直接适配；嵌套对象或空回执应直接回退 Terra 但不打开熔断；模拟 HTTP 断连应打开运行时熔断并回退。MAAS 是否透传 `text.format` 可单独探测，但插件正确性不再依赖它严格执行 schema。
