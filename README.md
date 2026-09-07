# github-watch

Akashic API v3 插件。它轮询 GitHub，把可处理事件作为 programmatic Input Message 送入 Akashic，并提供受 operation 约束的 GitHub Tool。

## 行为

- 首次启用只为 open Issue/PR 建立静默基线。
- 基线后新建的 open Issue/PR、draft PR 转为 ready，以及仓库 owner 新发且包含配置 mention 的 comment 会产生稳定事件；commit、编辑、普通 comment、关闭和合并不会唤醒。
- 同一 Issue/PR 复用一个确定性 programmatic Session；每个事件使用由 `operation_id` 推导的确定性 Input Message ID。提交返回即结束轮询，不等待模型输出。
- SQLite 账本持久化事件、operation、Session 和 Input Message identity。进程若在 Message 提交期间退出，启动后用相同 ID 重试，由 Core 的 append 幂等边界消除重复输入。
- 每个事件构建一份证据包和 detached operation checkout。Message 投影达到终态后删除该 checkout；异常退出由 TTL sweeper 与 worktree prune 回收。
- Agent 默认只分析。写操作必须由该 programmatic Input 的真实 Tool execution 发起，并携带对应 `operation_id`。只有 owner mention 事件可以 push 或创建 PR。
- comment、review 和 PR 使用 operation marker 查询去重。push 只允许一个 operation 分支，已记录的同名 push 可重复调用，换名会失败。
- Issue 修复 PR 应用 `Fixes #<issue>` 关联原 Issue；PR 上的后续修改默认更新原 PR，不能把另开替代 PR 当作失败 fallback。
- GET 使用分页、ETag 和传输 cooldown；写请求不自动重发。Git 凭据通过短命 `GIT_ASKPASS` 传入，remote、账本、证据和日志不保存 token。
- 配置通知 channel 后，Agent 可在需要决策、关键阻塞或高价值提醒时调用 `message_push`；插件自身不代发模型结果。

插件只注入通用 `TIMERS`、`PROGRAMMATIC`、`MESSAGE_CATALOG`、`TURN_PROJECTION` 和 `TOOLS` 能力。候选 generation 的 `apply` 只登记生命周期和 Tool，不打开 PEM、数据库或网络；正式 Root 启动后才创建客户端、账本和后台循环。

## Tool

- `github_watch_runtime_info`
- `github_watch_post_comment`
- `github_watch_submit_review`
- `github_watch_push_branch`
- `github_watch_create_pr`

## 配置

复制 `config.example.toml` 到安装后的私有配置目录，填写 GitHub App 的 app id、installation id、PEM 绝对路径、仓库列表和可选通知目标。私钥及 installation token 不写入账本或证据包。

从 3.x 升级到 4.0.0 时，先停止 runtime，再显式删除已经失效的 Turn 超时配置：

```bash
python scripts/migrate_message_runtime_config.py \
  <workspace>/plugin-data/github-watch-github/config.local.toml
```

脚本保留同目录 `config.local.toml.before-message-runtime` 恢复副本并输出 hash receipt；新 runtime 不读取或兼容 `turn_timeout_seconds`。

```bash
AKASHIC_AGENT_ROOT=/path/to/akasic-agent PYTHONPATH=/path/to/akasic-agent python -m pytest -q
```
