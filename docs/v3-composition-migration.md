# GitHub Watch v3 组合迁移合同

- 状态：implemented / plugin-side verification passed
- 日期：2026-08-17
- 基线：`origin/main@21ea000`
- 恢复点：`backup/github-watch-v3-before-20260815@21ea000`
- Core 依赖：Akashic Core 提供 `tools.v1`、`programmatic.v1`、`core.timers` 与正式 runtime lifecycle。

## Goal

把旧 runtime/Tool 类和宿主 side effect 收成为纯 v3 composition：由 `tools.v1` 登记五个声明，
由 `core.timers` 与 runtime lifecycle 持有正式轮询任务；终态读取沿
`programmatic/message/result` 的 accepted Input 身份进行。
迁移不改变 GitHub 发现、SQLite 事件状态机、证据、
checkout、GitHub App 身份、prompt、ack、幂等或不确定失败语义。

## Ownership

- Core 拥有 one-shot Timer、stable snapshot lease、Session/Turn identity 与 admission、
  `tools.v1` registry/执行上下文、programmatic Message/Turn 投影和 generation 晋升。
- 插件拥有 GitHub 客户端、SQLite、事件和 operation identity、Session 复用映射、证据、checkout、
  prompt、Tool 实现、远程副作用、重试与恢复。
- `github_watch.ProgrammaticTurnPort` 是插件领域端口；插件通过 `programmatic.v1` 的
  `session/admit` 与 `message/send` 适配它；send 必须返回真实 accepted `message_id`，
  result 必须按同一 `input_id` 返回投影状态和终态 Output 身份。
  插件 apply 不创建正式资源、client、账本或 checkout。
- Tool 授权由 Core `origin_session_key` 与插件账本中的 `operation_id -> thread_id` 对应关系确定。
  Session metadata 不再作为第二个重复 owner；Core 创建 Session 时仍写入不可伪造的插件 owner。

## Protected behavior

- 首次 baseline 静默，open/ready/owner mention 的稳定事件键与去重不变；
- `discovered -> claimed -> context_ready -> turn_submitting -> dispatched` 状态机不变；
- Session 首次创建后先写入 item，Turn 提交前先进入 `turn_submitting`；不确定失败仍不自动重试；
- programmatic Message 返回 accepted receipt 后结束本轮提交；后续轮询按同一 Input 读取模型结果；
- 只有 `RUNTIME_STARTED` 后的 lifecycle task 才能使用当前 Core context 的精确 lease 完成轮询与
  Turn admission；candidate、detached child 与裸 retired Context 不能借此产生输入；
- 五个 Tool 名称、schema、risk、group always-on、prompt 和 operation marker 行为不变；
- 只有 `programmatic/message/result` 返回 `complete` 才清理与 `session_id + input_id` 同时匹配的
  checkout；`pause/failure` 进入 `manual_reconcile`，清理失败交给 TTL sweeper；
- candidate apply 只登记 descriptor/listener，不读取 PEM、不创建数据库、不创建 GitHub client、不写正式 plugin-data。
- `programmatic.v1` 返回明确的 pre-admission 错误时插件可重试；缺失或不匹配 accepted receipt、
  以及 `submit` 取消表示 admission 不确定，进入 `manual_reconcile`，
  不得由插件自行猜测普通异常的 admission 边界。

## Change and rollback

```yaml
change_type: migration
semantic_delta: compatible
capability_owner: mixed
runtime_patch: required
runtime_patch_reason: "Core must attest stable background-job admission and preserve its exact lease across reload while a programmatic Turn is committed."
authoritative_state_owner: "Core owns Session/Turn; GitHub owns remote facts; plugin SQLite owns event consumption and recovery."
allowed_effects:
  - production stable Timer may create and update plugin-owned SQLite/evidence/checkouts
  - admitted Tool calls may perform explicitly authorized GitHub App writes
forbidden_effects:
  - candidate network calls, formal Session/Turn creation or production plugin-data writes
  - direct Control socket, SessionManager, PluginManager or JobService access
rollback: "Revert the migration commit and restore backup/github-watch-v3-before-20260815; deploy only with the matching Core stack."
```

## Verification

- existing discovery, ledger, checkout, prompt, GitHub client and operation tests stay green;
- domain dispatch tests prove existing Session reuse and new Session identity persistence occur before Turn submission;
- v3 wiring test proves candidate apply has no data write, formal lifecycle initializes only its assigned data directory,
  and five Tool declarations plus programmatic result reader are exact;
- exact Core integration must load the real namespace plugin, observe `tools.v1`/lifecycle/event catalogs, deny candidate
  side effects, dispatch one programmatic Turn across a reload pointer switch, and release all Root effects;
- plugin PR commit and Core Gate report must bind each other by full SHA and source digest before installation.
