# GitHub Watch v3 组合迁移合同

- 状态：accepted / implementation approved
- 日期：2026-08-15
- 基线：`origin/main@21ea000`
- 恢复点：`backup/github-watch-v3-before-20260815@21ea000`
- Core 依赖：Akashic Agent Timer snapshot scheduling PR #421、Agent Input stable admission PR #422、TurnCommitted async serial PR #423

## Goal

把固定 `PluginJobSpec`、`ControlClient`、`@tool` registry side effect 和 after-turn module
翻译为 `core.timer`、`core.agent_input`、`core.tools` 与 typed `TurnCommitted`。迁移只改变宿主
接入方式，不改变 GitHub 发现、SQLite 事件状态机、证据、checkout、GitHub App 身份、prompt、
ack、幂等或不确定失败语义。

## Ownership

- Core 拥有 Timer cadence/coalesce、stable snapshot lease、Session/Turn identity 与 admission、
  Tool registry/执行上下文、typed TurnCommitted 的触发时机和 generation 晋升。
- 插件拥有 GitHub 客户端、SQLite、事件和 operation identity、Session 复用映射、证据、checkout、
  prompt、Tool 实现、远程副作用、重试与恢复。
- `github_watch.AgentInputPort` 是插件领域端口；`plugin._CompositionAgentInput` 是唯一 Core adapter。
  领域协调器不导入 `ControlClient`，也不取得 `SessionManager`。
- Tool 授权由 Core `origin_session_key` 与插件账本中的 `operation_id -> thread_id` 对应关系确定。
  Session metadata 不再作为第二个重复 owner；Core 创建 Session 时仍写入不可伪造的插件 owner。

## Protected behavior

- 首次 baseline 静默，open/ready/owner mention 的稳定事件键与去重不变；
- `discovered -> claimed -> context_ready -> turn_submitting -> dispatched` 状态机不变；
- Session 首次创建后先写入 item，Turn 提交前先进入 `turn_submitting`；不确定失败仍不自动重试；
- Agent Input 返回 identity 后立即结束 poll callback，不等待模型结果；
- 旧 stable Timer callback 跨 reload 只凭原 task 的精确 stable lease 完成 Agent Input；candidate、
  detached child 与裸 retired Context 不能借此产生输入；
- 五个 Tool 名称、risk、always-on、prompt 和 operation marker 行为不变；
- TurnCommitted 只清理与 `session_key + turn_id` 同时匹配的 checkout，失败交给 TTL sweeper；
- candidate apply 只验证 PEM 与声明能力，不创建数据库、不调用 GitHub、不写正式 plugin-data。

## Change and rollback

```yaml
change_type: migration
semantic_delta: compatible
capability_owner: mixed
runtime_patch: required
runtime_patch_reason: "Core must attest stable Timer admission and preserve its exact lease across reload while Agent Input is committed."
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
- v3 wiring test proves candidate apply has no data write, production Timer initializes only its assigned data directory,
  and five Tool declarations plus TurnCommitted listener are exact;
- exact Core integration must load the real namespace plugin, observe Timer/Tool/event catalogs, deny candidate Timer
  side effects, dispatch one fake Agent Input across a reload pointer switch, and release all Root effects;
- plugin PR commit and Core Gate report must bind each other by full SHA and source digest before installation.
