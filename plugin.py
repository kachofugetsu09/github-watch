"""Akashic API v3 entrypoint for the GitHub polling bot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent.plugin_composition import (
    AGENT_INPUT,
    PLUGIN_TOOLS,
    TIMER_SERVICE,
    AgentInputService,
    Context,
    ToolRisk,
)
from agent.tools.base import Tool, get_current_tool_context
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from bus.events_lifecycle import TurnCommitted
from pydantic import BaseModel, Field, field_validator, model_validator

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .github_watch import AgentInputPort, GitHubWatch
from .ledger import EventLedger, EventState
from .operations import GitHubOperations

logger = logging.getLogger("plugin.github-watch")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION = "2.0.0"


class GitHubWatchConfig(BaseModel):
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    pem_path: str = Field(min_length=1)
    repositories: list[str] = Field(min_length=1)
    mention: str = "@akashic-review-bot"
    bot_login: str = "akashic-review-bot[bot]"
    poll_seconds: int = Field(default=120, ge=15)
    checkout_ttl_seconds: int = Field(default=86_400, ge=300)
    notify_channel: str | None = None
    notify_chat_id: str | None = None

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, value: list[str]) -> list[str]:
        invalid = [repo for repo in value if _REPOSITORY.fullmatch(repo) is None]
        if invalid:
            raise ValueError(f"invalid owner/repository values: {invalid}")
        if len(set(value)) != len(value):
            raise ValueError("repositories contains duplicates")
        return value

    @field_validator("mention")
    @classmethod
    def validate_mention(cls, value: str) -> str:
        if not value.startswith("@") or any(character.isspace() for character in value):
            raise ValueError("mention must be one @handle")
        return value

    @model_validator(mode="after")
    def validate_notification_target(self) -> GitHubWatchConfig:
        values = (self.notify_channel, self.notify_chat_id)
        if (values[0] is None) != (values[1] is None):
            raise ValueError("notify_channel and notify_chat_id must be configured together")
        if any(value is not None and not value.strip() for value in values):
            raise ValueError("notification target values must not be blank")
        return self


class _CompositionAgentInput(AgentInputPort):
    """把插件领域端口接到 Core Agent Input 能力。"""

    def __init__(self, ctx: Context, service: AgentInputService) -> None:
        self._ctx = ctx
        self._service = service

    async def create_session(self, metadata: Mapping[str, object]) -> str:
        session = await self._service.create_session(self._ctx, metadata=metadata)
        return session.id

    async def submit(self, session_id: str, content: str) -> str:
        receipt = await self._service.submit(self._ctx, session_id, content)
        return receipt.turn_id


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    ledger: EventLedger
    checkouts: CheckoutManager
    operations: GitHubOperations
    watch: GitHubWatch


class GitHubWatchRuntime:
    """拥有 GitHub Watch 的持久领域对象和运行行为。"""

    def __init__(
        self,
        *,
        config: GitHubWatchConfig,
        data_dir: Path,
        agent_input: AgentInputPort,
    ) -> None:
        self._config = config
        self._data_dir = data_dir
        self._agent_input = agent_input
        self._bound: _BoundRuntime | None = None

    def _ensure_runtime(self) -> None:
        """首次正式调用时初始化 plugin-data 和外部客户端。"""

        if self._bound is not None:
            return

        # 1. 打开插件自有持久账本并恢复可安全重试的阶段
        self._data_dir.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(self._data_dir / "events.sqlite3")
        ledger.integrity_check()
        recovered = ledger.recover_interrupted()
        if any(recovered.values()):
            logger.warning("github-watch recovered interrupted states: %s", recovered)

        # 2. 构造插件自有 GitHub、checkout 和 operation 实现
        client = GitHubClient(
            app_id=self._config.app_id,
            installation_id=self._config.installation_id,
            pem_path=Path(self._config.pem_path).expanduser(),
        )
        checkouts = CheckoutManager(
            client,
            root=self._data_dir / "checkouts",
            mirror_root=self._data_dir / "mirror",
            ttl_seconds=self._config.checkout_ttl_seconds,
        )
        removed = checkouts.sweep()
        if removed:
            logger.warning("github-watch swept expired checkouts count=%d", removed)
        operations = GitHubOperations(client, checkouts)

        # 3. 发布对象引用前完成全部可能失败的初始化
        self._bound = _BoundRuntime(
            ledger=ledger,
            checkouts=checkouts,
            operations=operations,
            watch=GitHubWatch(
                client=client,
                ledger=ledger,
                checkouts=checkouts,
                data_dir=self._data_dir,
                agent_input=self._agent_input,
                mention=self._config.mention,
                bot_login=self._config.bot_login,
                operations=operations,
                notify_channel=self._config.notify_channel,
                notify_chat_id=self._config.notify_chat_id,
            ),
        )

    def _require_runtime(self) -> _BoundRuntime:
        self._ensure_runtime()
        bound = self._bound
        if bound is None:
            raise RuntimeError("github-watch runtime initialization did not commit")
        return bound

    async def poll(self) -> None:
        await self._require_runtime().watch.poll(self._config.repositories)

    def runtime_info(self) -> dict[str, str]:
        return {
            "plugin": name,
            "version": version,
            "checkout_mode": "detached-commit",
            "mirror_recovery": "worktree-prune-before-fetch",
        }

    async def post_comment(self, operation_id: str, body: str) -> str:
        bound = self._require_runtime()
        authorized = self._authorized_event(operation_id)
        result = await asyncio.to_thread(
            bound.operations.post_comment,
            authorized,
            body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def submit_review(self, operation_id: str, body: str) -> str:
        bound = self._require_runtime()
        authorized = self._authorized_event(operation_id)
        result = await asyncio.to_thread(
            bound.operations.submit_review,
            authorized,
            body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def push_branch(self, operation_id: str, branch_suffix: str) -> str:
        bound = self._require_runtime()
        authorized = self._authorized_code_event(operation_id)
        result = await asyncio.to_thread(
            bound.operations.push_branch,
            authorized,
            branch_suffix,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def create_pr(self, operation_id: str, title: str, body: str) -> str:
        bound = self._require_runtime()
        authorized = self._authorized_code_event(operation_id)
        result = await asyncio.to_thread(
            bound.operations.create_pull,
            authorized,
            title=title,
            body=body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def cleanup_committed_turn(self, event: TurnCommitted) -> None:
        await asyncio.to_thread(
            self._cleanup_turn,
            event.session_key,
            event.turn_id,
        )

    def _authorized_event(self, operation_id: str) -> EventState:
        bound = self._require_runtime()
        context = get_current_tool_context()
        if context is None or not context.origin_session_key:
            raise PermissionError("github-watch tool requires a live turn context")
        event = bound.ledger.get_event_by_operation(operation_id)
        if (
            event.status not in {"turn_submitting", "dispatched"}
            or event.thread_id != context.origin_session_key
        ):
            raise PermissionError(
                "operation does not belong to the current dispatched session"
            )
        return event

    def _authorized_code_event(self, operation_id: str) -> EventState:
        event = self._authorized_event(operation_id)
        if event.trigger_kind != "owner_mention":
            raise PermissionError("code changes require an owner mention event")
        return event

    def _cleanup_turn(self, session_key: str, turn_id: str) -> None:
        bound = self._require_runtime()
        event = bound.ledger.get_event_by_turn(turn_id)
        if event is None or event.thread_id != session_key:
            return
        try:
            removed = bound.checkouts.cleanup(event.operation_id)
        except OSError:
            logger.exception(
                "github-watch checkout cleanup deferred to TTL event=%s",
                event.event_key,
            )
            return
        if removed:
            logger.info("github-watch checkout removed event=%s", event.event_key)


class _RuntimeInfoTool(Tool):
    name = "github_watch_runtime_info"
    description = "返回当前 GitHub Watch 插件版本和 checkout 恢复策略。"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, runtime: GitHubWatchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        return json.dumps(
            self._runtime.runtime_info(),
            ensure_ascii=False,
            sort_keys=True,
        )


class _PostCommentTool(Tool):
    name = "github_watch_post_comment"
    description = "以当前 operation 绑定的 GitHub App Bot 发布 Issue comment。"
    parameters = {
        "type": "object",
        "properties": {
            "operation_id": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["operation_id", "body"],
        "additionalProperties": False,
    }

    def __init__(self, runtime: GitHubWatchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, **kwargs: Any) -> str:
        return await self._runtime.post_comment(
            cast(str, kwargs["operation_id"]),
            cast(str, kwargs["body"]),
        )


class _SubmitReviewTool(Tool):
    name = "github_watch_submit_review"
    description = "以 GitHub App Bot 向当前 PR 提交一次 COMMENT review。"
    parameters = {
        "type": "object",
        "properties": {
            "operation_id": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["operation_id", "body"],
        "additionalProperties": False,
    }

    def __init__(self, runtime: GitHubWatchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, **kwargs: Any) -> str:
        return await self._runtime.submit_review(
            cast(str, kwargs["operation_id"]),
            cast(str, kwargs["body"]),
        )


class _PushBranchTool(Tool):
    name = "github_watch_push_branch"
    description = "把当前临时仓库的提交推到 operation 唯一分支。"
    parameters = {
        "type": "object",
        "properties": {
            "operation_id": {"type": "string"},
            "branch_suffix": {"type": "string"},
        },
        "required": ["operation_id", "branch_suffix"],
        "additionalProperties": False,
    }

    def __init__(self, runtime: GitHubWatchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, **kwargs: Any) -> str:
        return await self._runtime.push_branch(
            cast(str, kwargs["operation_id"]),
            cast(str, kwargs["branch_suffix"]),
        )


class _CreatePrTool(Tool):
    name = "github_watch_create_pr"
    description = "为当前 operation 已推送的分支创建 GitHub PR。"
    parameters = {
        "type": "object",
        "properties": {
            "operation_id": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["operation_id", "title", "body"],
        "additionalProperties": False,
    }

    def __init__(self, runtime: GitHubWatchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, **kwargs: Any) -> str:
        return await self._runtime.create_pr(
            cast(str, kwargs["operation_id"]),
            cast(str, kwargs["title"]),
            cast(str, kwargs["body"]),
        )


api_version = 3
name = "github-watch"
version = _VERSION
desc = "Poll GitHub and wake one stable Akashic Session per issue or PR"
Config = GitHubWatchConfig
inject = (AGENT_INPUT, PLUGIN_TOOLS, TIMER_SERVICE)


async def apply(ctx: Context, config: GitHubWatchConfig) -> None:
    """登记轮询、Tool 和 Turn 完成清理能力。"""

    # 1. 候选只验证本地配置，不创建账本或调用外部 GitHub
    pem_path = Path(config.pem_path).expanduser()
    if not pem_path.is_file():
        raise FileNotFoundError(f"GitHub App PEM does not exist: {pem_path}")
    agent_input = _CompositionAgentInput(ctx, ctx.require(AGENT_INPUT))
    runtime = GitHubWatchRuntime(
        config=config,
        data_dir=ctx.runtime.data_dir,
        agent_input=agent_input,
    )

    # 2. Tool 实现归插件所有，Core 只编译声明和执行上下文
    tools = ctx.require(PLUGIN_TOOLS)
    declarations: tuple[tuple[Tool, ToolRisk], ...] = (
        (_RuntimeInfoTool(runtime), "read-only"),
        (_PostCommentTool(runtime), "external-side-effect"),
        (_SubmitReviewTool(runtime), "external-side-effect"),
        (_PushBranchTool(runtime), "external-side-effect"),
        (_CreatePrTool(runtime), "external-side-effect"),
    )
    for tool, risk in declarations:
        _ = await tools.register(ctx, tool, risk=risk, always_on=True)

    # 3. typed event 负责 checkout 清理，stable snapshot Timer 负责串行轮询
    _ = await ctx.on(AFTER_TURN_COMMITTED, runtime.cleanup_committed_turn)
    _ = await ctx.require(TIMER_SERVICE).interval(
        ctx,
        runtime.poll,
        config.poll_seconds,
        name="poll",
    )
