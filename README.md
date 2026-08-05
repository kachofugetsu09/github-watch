# github-watch

Akashic API v2 轮询插件，不使用 webhook。

## 行为

- 首次启用只建立基线，不处理已有 Issue/PR。
- 基线后新建的 Issue/PR 只触发一次 programmatic turn。
- commit、状态、编辑和普通 comment 不唤醒。
- 只有仓库 owner 新发的、包含 `@akashic-review-bot` 的 comment 可以再次唤醒。
- 每个 Issue/PR 复用同一 Control thread；每次都会生成新的完整证据包。
- 插件只等 `turn/start` 入队成功，不等待 turn 完成，也不接收或代发最终回复。
- Agent 默认只分析并自行发布一条 comment。只有 owner mention 明确要求修改或创建 PR 时，Agent 才被允许执行该操作。

SQLite 账本记录 `event_key -> operation_id -> thread_id -> turn_id -> dispatched`。`turn/start` 请求开始后的不确定失败不会自动重试，避免重复唤醒；Agent 发送 comment 前使用 operation marker 去重。

## 配置

复制 `config.example.toml` 为仓库外的私有配置，填入 GitHub App 的 app id、installation id 和 PEM 绝对路径。私钥和 installation token 不会写入插件源码、账本或证据包。
