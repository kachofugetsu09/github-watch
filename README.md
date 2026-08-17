# github-watch

Akashic API v3 组合插件，不使用 webhook。

## 行为

- 首次启用只建立基线，不处理已有 Issue/PR。
- 只轮询 open Issue 和 open PR；关闭或已合并对象不进入 baseline，也不再追踪。
- 默认每 120 秒由 Core background job 检查一次；ETag 减少重复列表传输，稳定事件键和 SQLite 账本避免重复处理。
- 基线后新建的 Issue/PR 只触发一次 programmatic turn；PR 默认提交一次 `COMMENT` review。
- commit、状态、编辑和普通 comment 不唤醒。
- 只有仓库 owner 新发的、包含 `@akashic-review-bot` 的 comment 可以再次唤醒。
- 每个 Issue/PR 通过 invocation-scoped programmatic Turn port 复用同一 Session；每次都会生成新的完整证据包。
- 插件只等 `turn/start` 入队成功，不等待 turn 完成，也不接收或代发最终回复。
- 每个仓库复用 `plugin-data` 下不含凭证的裸镜像；每次 turn 以 detached commit 创建唯一
  operation worktree。typed TurnCommitted 事件删除工作目录，异常退出由 TTL sweeper 和
  worktree prune 回收。
- `github_watch_runtime_info` 只读返回当前插件版本和 checkout 恢复策略，供正式候选验证使用。
- Agent 默认只分析，并通过 `github_watch_*` 工具以 GitHub App Bot 身份发布 comment/review。
  只有 owner mention 明确要求修改或创建 PR 时，才允许在临时仓库提交、push 和创建 PR。
- 从 Issue 创建的修复 PR 必须使用 `Fixes #<issue>` 关联并在合入后自动关闭 Issue；每个 Issue
  修复链默认只创建一个 PR。PR 上的后续修改默认不得递归另开替代 PR，工具无法更新当前 PR
  时必须在原 PR 说明阻塞；只有 owner 明确要求时才能另开并声明 supersedes 关系。
- 配置主 channel 后，Agent 只在需要维护者决策、出现关键阻塞/风险，或非常值得立即告知时
  选择性调用一次 `message_push`；普通成功、常规 review 和过程进度不推送。

SQLite 账本记录 `event_key -> operation_id -> thread_id -> turn_id -> dispatched`。`turn/start`
请求开始后的不确定失败不会自动重试，避免重复唤醒；App 写操作使用 operation marker 去重。

插件只注入 `core.background_jobs` 和 `core.tool_catalog`，并登记 `AFTER_TURN_COMMITTED`
listener。Core 通过 `BackgroundJobContext.turns` 提供 invocation-scoped Session/Turn 准入；
GitHub 客户端、SQLite、证据、checkout、幂等与重试仍由插件实现。

## 配置

复制 `config.example.toml` 为仓库外的私有配置，填入 GitHub App 的 app id、installation id、
PEM 绝对路径和可选主 channel。私钥和 installation token 不会写入插件源码、账本或证据包。

该版本要求包含 stable background-job snapshot scheduling、programmatic Turn admission、严格
Tool catalog handler 校验与 typed TurnCommitted event 的 Akashic Core。
