# GitHub Watch 轮询设计

## 目标语义

- 不使用 webhook；由 Akashic Plugin API v2 的 interval job 串行轮询。
- 首次启用只建立 baseline，不回复已有 Issue/PR。
- baseline 后的新 Issue/PR 只唤醒一次，默认只分析并 comment。
- commit、label、state、普通 comment 和 comment edit 不唤醒。
- 只有 repository owner 新发的 `@akashic-review-bot` comment 可以再唤醒。
- owner mention 默认仍只 comment；它明确要求修改或创建 PR 时，程序化 Agent 才可使用本地 Shell 执行。

## 所有权和流程

```text
┌──────────────────────┐
│ GitHub authority     │  Issue/PR/comment/owner
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Conditional poll     │  stable sort + Link + ETag
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ SQLite event ledger  │  event/operation/thread/turn/artifact
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Evidence bundle      │  full pages + PR diff + SHA-256 manifest
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Stable Control thread│  fresh turn per allowed event
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Verified App comment │  operation marker + API readback
└──────────────────────┘
```

GitHub 保存远程权威事实；`events.sqlite3` 保存消费和恢复事实；Akashic
`sessions.db` 只追加稳定 thread 与 turn 消息。thread metadata 关闭 memory retrieval 和
post-memory，避免 GitHub 任务进入长期记忆。

## 事件与恢复

稳定事件键：

- `repo:issue:number:opened`
- `repo:pr:number:opened`
- `repo:kind:number:comment:comment_id`

`updated_at` 只是“需要检查 comment cursor”的候选信号，不是执行身份。编辑原
comment 不改变 `comment_id`，因此不会重复事件。

```text
discovered → claimed → context_ready → turn_running
                                      ↓
                              comment_posting → completed
```

- `claimed/context_ready` 在重启后可安全回到 `discovered`，因为外部效果还未开始。
- `turn_running/comment_posting` 中断后转为 `manual_reconcile`，不自动重试。
- comment 内含稳定 operation marker；POST 返回后重新 GET comments，ID 和正文均一致才进入 `completed`。

## 上下文和凭证

每次唤醒在 `plugin-data/evidence/<operation-id>/` 重新抓取 item、timeline、Issue
comments；PR 额外包含 commits、files、reviews、review comments、checks、combined status
和 GitHub diff media type 的完整 patch。`manifest.json` 记录每个文件的字节数、对象数和
SHA-256。稳定 thread 提供旧对话，新 evidence 始终覆盖旧事实。

GitHub App 私钥只由仓库外的绝对路径引用，installation token 只驻留内存。源码、
prompt、SQLite、evidence、Git remote 和日志都不写入凭证。

## 轮询实践

请求使用认证、固定周期、稳定排序和串行调度。分页跟随 GitHub `Link` header，
每页保留 ETag 并发送 `If-None-Match`；`304` 复用已验证页。`Retry-After` 或
rate-limit reset 存在时，客户端在本地阻断新 HTTP 请求直到恢复时间。

参考：

- <https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api>
- <https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api>
- <https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app>
