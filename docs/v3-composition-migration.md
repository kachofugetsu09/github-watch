# GitHub Watch Message runtime migration

- 状态：implemented
- 版本：4.0.0
- 恢复点：`/tmp/akashic-fleet-rewrite-backups/github-watch-before-3d7637d.bundle`

4.0 删除插件专属 background-job、programmatic Turn、Tool catalog 和 `TurnCommitted` 事件接入。现行插件只组合 Core 的 Timer、programmatic Message、Message catalog、Turn projection 和普通 Tool 能力。

迁移保留 GitHub 发现规则、稳定 event/operation identity、历史 SQLite 行、证据包、checkout、GitHub App 身份和 operation marker。SQLite 以 additive migration 增加 `input_message_id`；旧 `turn_id` 只作为历史字段保留。已进入旧 `turn_submitting` 且无法证明 Message identity 的行保持人工核对，不能自动猜测为已提交或未提交。

验收由测试中的真实 `PluginManager` generation lifecycle 与真实 Message/Tool 路径覆盖：候选加载无外部效果，正式启动产生确定性 Input，Tool 写入本地 GitHub HTTP fixture，Message 终态触发 checkout cleanup，重复启动不产生第二条输入或第二次远端效果。
