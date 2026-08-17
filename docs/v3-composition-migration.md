# GitHub Watch v3 组合迁移合同

- 状态：implemented / plugin-side verification passed
- 日期：2026-08-17
- 基线：`origin/main@21ea000`
- 恢复点：`backup/github-watch-v3-before-20260815@21ea000`
- Core 依赖：Akashic Core `00b13940c3c2b39d57695cac09ccf446f12dd1b8` background job + durable programmatic Turn port + Tool catalog + typed TurnCommitted

## Goal

把旧 runtime/Tool 类和宿主 side effect 收成为纯 v3 composition：由
`BACKGROUND_JOBS` 登记 `programmatic_turns=True` 的 job，由 `TOOL_CATALOG` 登记五个声明，
并登记 typed `TurnCommitted` listener。迁移不改变 GitHub 发现、SQLite 事件状态机、证据、
checkout、GitHub App 身份、prompt、ack、幂等或不确定失败语义。

## Ownership

- Core 拥有 Timer cadence/coalesce、stable snapshot lease、Session/Turn identity 与 admission、
  Tool registry/执行上下文、typed TurnCommitted 的触发时机和 generation 晋升。
- 插件拥有 GitHub 客户端、SQLite、事件和 operation identity、Session 复用映射、证据、checkout、
  prompt、Tool 实现、远程副作用、重试与恢复。
- `github_watch.ProgrammaticTurnPort` 是插件领域端口；Core 通过
  `BackgroundJobContext.turns` 在正式 job invocation 内提供它。插件 apply 不创建 adapter、
  client、账本或 checkout。
- Tool 授权由 Core `origin_session_key` 与插件账本中的 `operation_id -> thread_id` 对应关系确定。
  Session metadata 不再作为第二个重复 owner；Core 创建 Session 时仍写入不可伪造的插件 owner。

## Protected behavior

- 首次 baseline 静默，open/ready/owner mention 的稳定事件键与去重不变；
- `discovered -> claimed -> context_ready -> turn_submitting -> dispatched` 状态机不变；
- Session 首次创建后先写入 item，Turn 提交前先进入 `turn_submitting`；不确定失败仍不自动重试；
- programmatic Turn 返回 receipt 后立即结束 poll callback，不等待模型结果；
- stable background job invocation 只凭当前 Core context 的精确 lease 完成 Turn admission；candidate、
  detached child 与裸 retired Context 不能借此产生输入；
- 五个 Tool 名称、risk、always-on、prompt 和 operation marker 行为不变；
- TurnCommitted 只清理与 `session_key + turn_id` 同时匹配的 checkout，失败交给 TTL sweeper；
- candidate apply 只登记 descriptor/listener，不读取 PEM、不创建数据库、不创建 GitHub client、不写正式 plugin-data。
- Core Turn port 将已证明未准入的失败转换为 `ProgrammaticTurnPreAdmissionError`，插件可重试；
  `ProgrammaticTurnUncertainError` 与 `submit` 取消表示 admission 不确定，进入 `manual_reconcile`，
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
- v3 wiring test proves candidate apply has no data write, formal job initializes only its assigned data directory,
  and five Tool declarations plus TurnCommitted listener are exact;
- exact Core integration must load the real namespace plugin, observe background-job/Tool/event catalogs, deny candidate
  job side effects, dispatch one programmatic Turn across a reload pointer switch, and release all Root effects;
- plugin PR commit and Core Gate report must bind each other by full SHA and source digest before installation.
