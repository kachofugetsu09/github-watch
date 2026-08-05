# github-watch

Akashic API v2 轮询插件，不使用 webhook。

## 行为

- 首次启用只建立基线，不处理已有 Issue/PR。
- 基线后新建的 Issue/PR 只触发一次 programmatic turn；PR 默认提交一次 `COMMENT` review。
- commit、状态、编辑和普通 comment 不唤醒。
- 只有仓库 owner 新发的、包含 `@akashic-review-bot` 的 comment 可以再次唤醒。
- 每个 Issue/PR 复用同一 Control thread；每次都会生成新的完整证据包。
- 插件只等 `turn/start` 入队成功，不等待 turn 完成，也不接收或代发最终回复。
- 每次 turn 在 `/tmp/akashic-github-watch/<operation-id>/repository` 使用 GitHub App token
  clone 精确仓库状态；after-turn 删除目录，异常退出由 TTL sweeper 回收。
- Agent 默认只分析，并通过 `github_watch_*` 工具以 GitHub App Bot 身份发布 comment/review。
  只有 owner mention 明确要求修改或创建 PR 时，才允许在临时仓库提交、push 和创建 PR。
- 配置主 channel 后，Agent 只在需要维护者决策、出现关键阻塞/风险，或非常值得立即告知时
  选择性调用一次 `message_push`；普通成功、常规 review 和过程进度不推送。

SQLite 账本记录 `event_key -> operation_id -> thread_id -> turn_id -> dispatched`。`turn/start`
请求开始后的不确定失败不会自动重试，避免重复唤醒；App 写操作使用 operation marker 去重。

## 配置

复制 `config.example.toml` 为仓库外的私有配置，填入 GitHub App 的 app id、installation id、
PEM 绝对路径和可选主 channel。私钥和 installation token 不会写入插件源码、账本或证据包。
