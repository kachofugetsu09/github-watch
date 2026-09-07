# GitHub Watch Message runtime design

## 主链

```text
GitHub poll ──> plugin ledger ──> evidence + checkout
                                      │
                                      ▼
                            deterministic Input Message
                                      │
                                      ▼
                         ordinary react / Tool execution
                                      │
                                      ▼
                    Message terminal projection ──> cleanup
```

Core 拥有 Message append、Session 准入、Turn 投影、Tool execution 和 generation lifecycle。插件拥有 GitHub 发现规则、事件与 operation identity、证据、checkout、GitHub App 写操作及其恢复。两边只通过通用组合能力连接；Core 不识别 GitHub Watch 的事件或状态机。

## 状态和恢复

SQLite 的事件状态依次为 `discovered -> claimed -> context_ready -> message_submitting -> dispatched -> completed`。`message_submitting` 保存确定性 Session 与 Input Message ID；启动恢复把它退回 `discovered` 并以相同 ID 重试。Core 接受相同 Message 后返回同一 identity，因此提交结果丢失不会生成第二条输入。旧 `turn_submitting` 行无法证明 Message identity，启动时保留为需人工核对的状态。

每个 operation checkout 从远端精确 commit 创建。插件只会删除通过 operation ID 验证的目录；正常删除由已提交 Input 所属投影的终态触发，长期遗留由 TTL sweeper 回收。远端写操作不随内存或 checkout 回滚。comment、review 和 PR 先查 operation marker；push 把一个 operation 永久绑定到一个分支名。

## Tool 授权

Tool `prepare` 从不可变 `CallSource.messages` 取得 Session，要求其中包含账本记录的原始 programmatic Input，并核对 `operation_id -> session_id`。push 与 create PR 还要求事件来自 owner mention。参数在这一边界用严格 schema 校验；通过后不再重复猜测来源。

候选 `apply` 无外部效果。正式 `RUNTIME_STARTED` 打开插件状态并启动两个 Fiber：Timer 驱动的 poll loop，以及 Message catalog head 驱动的 cleanup loop。`RUNTIME_STOPPING` 取消同一 generation 的任务，避免热更新后继续使用旧 Root。
