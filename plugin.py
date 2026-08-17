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
    BACKGROUND_JOBS,
    TOOL_CATALOG,
    BackgroundJobDefinition,
    Context,
    IntervalTrigger,
    PluginToolDefinition,
)
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from bus.events_lifecycle import TurnCommitted
from pydantic import BaseModel, Field, field_validator, model_validator

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .github_watch import (
    GitHubWatch,
    ProgrammaticTurnPort,
)
from .ledger import EventLedger, EventState
from .operations import GitHubOperations

logger = logging.getLogger("plugin.github-watch")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION = "3.0.0"


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


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    ledger: EventLedger
    checkouts: CheckoutManager
    operations: GitHubOperations
    watch: GitHubWatch


_config: GitHubWatchConfig | None = None
_data_dir: Path | None = None
_bound: _BoundRuntime | None = None


def _require_config() -> GitHubWatchConfig:
    config = _config
    if config is None:
        raise RuntimeError("github-watch plugin has not been applied")
    return config


def _require_data_dir() -> Path:
    data_dir = _data_dir
    if data_dir is None:
        raise RuntimeError("github-watch plugin data root has not been bound")
    return data_dir


def _ensure_formal_runtime() -> _BoundRuntime:
    """Create the formal plugin runtime only when a formal handler executes."""

    global _bound
    if _bound is not None:
        return _bound
    config = _require_config()
    data_dir = _require_data_dir()

    # 1. Open plugin-owned durable state and recover only known safe phases.
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger = EventLedger(data_dir / "events.sqlite3")
    ledger.integrity_check()
    recovered = ledger.recover_interrupted()
    if any(recovered.values()):
        logger.warning("github-watch recovered interrupted states: %s", recovered)

    # 2. Build GitHub, checkout, and operation owners inside the formal boundary.
    client = GitHubClient(
        app_id=config.app_id,
        installation_id=config.installation_id,
        pem_path=Path(config.pem_path).expanduser(),
    )
    checkouts = CheckoutManager(
        client,
        root=data_dir / "checkouts",
        mirror_root=data_dir / "mirror",
        ttl_seconds=config.checkout_ttl_seconds,
    )
    removed = checkouts.sweep()
    if removed:
        logger.warning("github-watch swept expired checkouts count=%d", removed)
    operations = GitHubOperations(client, checkouts)

    # 3. Publish the complete runtime only after all initialization succeeds.
    _bound = _BoundRuntime(
        ledger=ledger,
        checkouts=checkouts,
        operations=operations,
        watch=GitHubWatch(
            client=client,
            ledger=ledger,
            checkouts=checkouts,
            data_dir=data_dir,
            mention=config.mention,
            bot_login=config.bot_login,
            operations=operations,
            notify_channel=config.notify_channel,
            notify_chat_id=config.notify_chat_id,
        ),
    )
    return _bound


def _require_bound_runtime() -> _BoundRuntime:
    bound = _bound
    if bound is None:
        raise RuntimeError("github-watch formal runtime has not been initialized")
    return bound


def _runtime_info() -> dict[str, str]:
    return {
        "plugin": name,
        "version": version,
        "checkout_mode": "detached-commit",
        "mirror_recovery": "worktree-prune-before-fetch",
    }


def _authorized_event(context: Any, operation_id: str) -> EventState:
    """Authorize an operation against the explicit Core tool provenance."""

    origin_session_key = context.origin_session_key
    if not origin_session_key:
        raise PermissionError("github-watch tool requires a live turn context")
    bound = _ensure_formal_runtime()
    event = bound.ledger.get_event_by_operation(operation_id)
    if (
        event.status not in {"turn_submitting", "dispatched"}
        or event.thread_id != origin_session_key
    ):
        raise PermissionError(
            "operation does not belong to the current dispatched session"
        )
    return event


def _authorized_code_event(context: Any, operation_id: str) -> EventState:
    event = _authorized_event(context, operation_id)
    if event.trigger_kind != "owner_mention":
        raise PermissionError("code changes require an owner mention event")
    return event


async def run_github_watch_poll(context: Any) -> None:
    """Poll GitHub and admit each discovered event through the invocation port."""

    turns = context.turns
    if turns is None:
        raise RuntimeError("candidate GitHub Watch job cannot access programmatic Turns")
    config = _require_config()
    bound = _ensure_formal_runtime()
    await bound.watch.poll(config.repositories, cast(ProgrammaticTurnPort, turns))


async def run_github_watch_runtime_info(
    context: Any,
    arguments: Mapping[str, object],
) -> str:
    del arguments
    _ = context
    _ensure_formal_runtime()
    return json.dumps(_runtime_info(), ensure_ascii=False, sort_keys=True)


async def run_github_watch_post_comment(
    context: Any,
    arguments: Mapping[str, object],
) -> str:
    event = _authorized_event(context, cast(str, arguments["operation_id"]))
    bound = _require_bound_runtime()
    result = await asyncio.to_thread(
        bound.operations.post_comment,
        event,
        cast(str, arguments["body"]),
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


async def run_github_watch_submit_review(
    context: Any,
    arguments: Mapping[str, object],
) -> str:
    event = _authorized_event(context, cast(str, arguments["operation_id"]))
    bound = _require_bound_runtime()
    result = await asyncio.to_thread(
        bound.operations.submit_review,
        event,
        cast(str, arguments["body"]),
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


async def run_github_watch_push_branch(
    context: Any,
    arguments: Mapping[str, object],
) -> str:
    event = _authorized_code_event(context, cast(str, arguments["operation_id"]))
    bound = _require_bound_runtime()
    result = await asyncio.to_thread(
        bound.operations.push_branch,
        event,
        cast(str, arguments["branch_suffix"]),
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


async def run_github_watch_create_pr(
    context: Any,
    arguments: Mapping[str, object],
) -> str:
    event = _authorized_code_event(context, cast(str, arguments["operation_id"]))
    bound = _require_bound_runtime()
    result = await asyncio.to_thread(
        bound.operations.create_pull,
        event,
        title=cast(str, arguments["title"]),
        body=cast(str, arguments["body"]),
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _cleanup_committed_turn(event: TurnCommitted) -> None:
    bound = _bound
    if bound is None:
        return
    owned = bound.ledger.get_event_by_turn(event.turn_id)
    if owned is None or owned.thread_id != event.session_key:
        return
    try:
        removed = bound.checkouts.cleanup(owned.operation_id)
    except OSError:
        logger.exception(
            "github-watch checkout cleanup deferred to TTL event=%s",
            owned.event_key,
        )
        return
    if removed:
        logger.info("github-watch checkout removed event=%s", owned.event_key)


def _on_turn_committed(event: TurnCommitted) -> None:
    _cleanup_committed_turn(event)


def _tool_definitions() -> tuple[PluginToolDefinition, ...]:
    return (
        PluginToolDefinition(
            name="github_watch_runtime_info",
            description="返回当前 GitHub Watch 插件版本和 checkout 恢复策略。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler_export="run_github_watch_runtime_info",
            risk="read-only",
            always_on=True,
        ),
        PluginToolDefinition(
            name="github_watch_post_comment",
            description="以当前 operation 绑定的 GitHub App Bot 发布 Issue comment。",
            parameters={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["operation_id", "body"],
                "additionalProperties": False,
            },
            handler_export="run_github_watch_post_comment",
            risk="external-side-effect",
            always_on=True,
        ),
        PluginToolDefinition(
            name="github_watch_submit_review",
            description="以 GitHub App Bot 向当前 PR 提交一次 COMMENT review。",
            parameters={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["operation_id", "body"],
                "additionalProperties": False,
            },
            handler_export="run_github_watch_submit_review",
            risk="external-side-effect",
            always_on=True,
        ),
        PluginToolDefinition(
            name="github_watch_push_branch",
            description="把当前临时仓库的提交推到 operation 唯一分支。",
            parameters={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "branch_suffix": {"type": "string"},
                },
                "required": ["operation_id", "branch_suffix"],
                "additionalProperties": False,
            },
            handler_export="run_github_watch_push_branch",
            risk="external-side-effect",
            always_on=True,
        ),
        PluginToolDefinition(
            name="github_watch_create_pr",
            description="为当前 operation 已推送的分支创建 GitHub PR。",
            parameters={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["operation_id", "title", "body"],
                "additionalProperties": False,
            },
            handler_export="run_github_watch_create_pr",
            risk="external-side-effect",
            always_on=True,
        ),
    )


api_version = 3
name = "github-watch"
version = _VERSION
desc = "Poll GitHub and wake one stable Akashic Session per issue or PR"
Config = GitHubWatchConfig
inject = (BACKGROUND_JOBS, TOOL_CATALOG)


async def apply(ctx: Context, config: GitHubWatchConfig) -> None:
    """Register pure-v3 job, Tool catalog descriptors, and committed-turn cleanup."""

    global _config, _data_dir, _bound
    _config = config
    _data_dir = ctx.data_root
    _bound = None

    # 1. Register declarations only; no candidate data or external client is touched.
    await ctx.require(BACKGROUND_JOBS).register(
        ctx,
        BackgroundJobDefinition(
            name="poll",
            triggers=(IntervalTrigger(config.poll_seconds),),
            handler_export="run_github_watch_poll",
            programmatic_turns=True,
        ),
    )
    catalog = ctx.require(TOOL_CATALOG)
    for definition in _tool_definitions():
        await catalog.register(ctx, definition)

    # 2. Cleanup observes only the matching session and committed Turn identity.
    await ctx.on(AFTER_TURN_COMMITTED, _on_turn_committed)
