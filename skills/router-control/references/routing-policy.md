# 路由策略

路由器采用安全优先的保守策略。它只给主 Sol 会话提出角色建议，最终交付责任仍在主会话。

| 任务特征 | 建议执行者 | 原因 |
| --- | --- | --- |
| 搜索、盘点、读日志、收集证据 | `router_scout`（Local Text；必要时 Luna） | `LOCAL_TEXT_FIRST` 只替换只读轻任务，运行失败自动回退 |
| 复杂纯文本实现或修复 | `router_worker`（GLM-5.3 Max；必要时 Terra） | `GLM_FIRST` 下优先 GLM，高峰/熔断/多模态回退 Terra |
| 代码审查、独立复核 | `router_reviewer`（GLM-5.3 Max；必要时 Terra high） | 纯文本优先 GLM，Terra 保留多模态与异构复核价值 |
| 测试编写与验证 | `router_tester`（Luna） | 适合机械验证；必要时可写测试文件 |
| 文档更新 | `router_docs`（Luna） | 低风险、范围清晰 |
| 等待、轮询、状态检查 | `router_monitor`（Local Text；必要时 Luna low） | 避免 Sol 消耗在等待上；本地 provider 不可用时保持可持续运行 |
| 小任务、高风险、架构任务或不确定任务 | 主 Sol | 委派收益不足或错误代价高 |

重任务 `STABLE / GLM_FIRST` 与轻任务 `LUNA_STABLE / LOCAL_TEXT_FIRST` 正交。GLM 时段和额度回退只影响 worker/reviewer：工作日 `14:00 <= time < 18:00`（`Asia/Shanghai`）在委派前直接选 Terra；额度、认证和限流错误由用户级 circuit breaker 共享给所有项目。到达服务端 `next_flush_time` 后只允许一个半开探测，成功后自动恢复 GLM。

`LOCAL_TEXT_FIRST` 仅影响 scout/monitor；tester/docs 因可能写入而继续由 Luna 执行。本地文本 provider 使用独立短熔断并回退 Luna，不修改 GLM 重任务健康状态。它是纯文本路径，不能接收图片。GLM-5.3 surrogate 只能验证路由、Responses provider、凭据隔离和回退，不能证明 DeepSeek V4 Flash 的真实质量、时延或端点兼容性。

包含图片的 worker/reviewer 任务强制 Terra；Luna 角色不接收图片。GLM writer 失败时，只有在没有工具活动且工作区指纹未变的情况下才能自动启动 Terra writer；存在可能已写入的证据时返回 Sol 检查，避免双 writer 冲突。

硬性保留给 Sol 的信号包括：身份验证、权限、密钥、支付、安全边界、生产部署、不可逆删除、数据库迁移、并发一致性、跨系统架构和需求边界不清。

可写角色必须取得显式、角色级的写授权：`router_worker` 需要实现/修复/创建等动作，`router_tester` 需要新增、修复或运行测试，`router_docs` 需要更新或编写文档。否定表达和显式只读约束优先；例如“只读盘点，不要实现、不要修复、不要改代码”只能进入只读角色。“如何实现、实现方案、修复建议、propose a fix”等规划短语中的关键词不构成写授权，除非同一任务另有独立的正向写操作。分类器不确定时留给 Sol，不能用可写 sandbox 试探。

当前版本仍仅允许一层委派，子 agent 不再派生子 agent；同一时间最多一个可写角色；MCP 写租约绑定调用 ID，优先在调用完成后释放，运行时漏发完成事件时只有在工作区进程锁空闲后才能自愈；子 agent 必须返回结构化 receipt；receipt 不合格时最多修复一次。
