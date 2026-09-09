"""GitHub polling, programmatic Message admission, and operation-bound tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agent.control.timer import TimerStatus
from agent.plugin_composition import Context, RUNTIME_STARTED, RUNTIME_STOPPING, ServiceKey
from agent.plugin_composition.messages import MESSAGE_CATALOG
from agent.plugin_composition.timers import TIMERS
from plugins.programmatic.control import AdmitParams, PROGRAMMATIC, Programmatic, SendParams
from plugins.tools.api import BoundTool, CallSource, InvalidArguments, Result
from plugins.tools.plugin import TOOLS, ToolView
from plugins.turn_projection.plugin import TURN_PROJECTION, TurnProjection
from session.log import MessageCatalog
from session.message import ContentPart, Input
from session.message_codec import json_value

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .github_watch import GitHubWatch, ProgrammaticMessagePort
from .ledger import EventLedger, EventState
from .operations import GitHubOperations

logger = logging.getLogger("plugin.github-watch")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubWatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
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


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class BodyInput(OperationInput):
    body: str = Field(min_length=1)


class PushInput(OperationInput):
    branch_suffix: str = Field(min_length=1)


class PullInput(OperationInput):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


_MODELS: dict[str, type[BaseModel]] = {
    "post_comment": BodyInput,
    "submit_review": BodyInput,
    "push_branch": PushInput,
    "create_pr": PullInput,
}


class _ProgrammaticMessages(ProgrammaticMessagePort):
    """Adapt the ordinary programmatic source without inventing a Turn identity."""
    def __init__(self, api: Programmatic) -> None:
        self._api = api

    async def admit(self, session_id: str) -> None:
        result = await self._api.call(
            "programmatic/session/admit", AdmitParams(session_id=session_id),
        )
        if result.get("session_id") != session_id:
            raise RuntimeError("programmatic Session admission identity mismatch")

    async def submit(self, session_id: str, message_id: str, content: str) -> str:
        result = await self._api.call(
            "programmatic/message/send",
            SendParams(session_id=session_id, message_id=message_id, text=content),
        )
        accepted = result.get("message_id")
        if accepted != message_id:
            raise RuntimeError("programmatic Message admission identity mismatch")
        return cast(str, accepted)


class Runtime:
    """Own one generation's client, ledger, timer loop, and Message cleanup follower."""
    def __init__(self, ctx: Context, config: GitHubWatchConfig) -> None:
        self._ctx = ctx
        self._config = config
        self._bound: tuple[EventLedger, CheckoutManager, GitHubOperations, GitHubWatch] | None = None

    def bind(self) -> tuple[EventLedger, CheckoutManager, GitHubOperations, GitHubWatch]:
        """Open plugin state and external clients only inside the formal runtime."""
        if self._bound is not None:
            return self._bound
        data_dir = self._ctx.data_root
        data_dir.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(data_dir / "events.sqlite3")
        ledger.integrity_check()
        recovered = ledger.recover_interrupted()
        if any(recovered.values()):
            logger.warning("github-watch recovered interrupted states: %s", recovered)
        client = GitHubClient(
            app_id=self._config.app_id,
            installation_id=self._config.installation_id,
            pem_path=Path(self._config.pem_path).expanduser(),
        )
        checkouts = CheckoutManager(
            client, root=data_dir / "checkouts", mirror_root=data_dir / "mirror",
            ttl_seconds=self._config.checkout_ttl_seconds,
        )
        operations = GitHubOperations(client, checkouts)
        watch = GitHubWatch(
            client=client, ledger=ledger, checkouts=checkouts, data_dir=data_dir,
            mention=self._config.mention, bot_login=self._config.bot_login,
            operations=operations, notify_channel=self._config.notify_channel,
            notify_chat_id=self._config.notify_chat_id,
        )
        self._bound = (ledger, checkouts, operations, watch)
        return self._bound

    async def run(self) -> None:
        """Poll and cleanup in independent loops owned by the same generation Fiber."""
        self.bind()
        async with asyncio.TaskGroup() as group:
            _ = group.create_task(self._poll_loop(), name="github-watch:poll")
            _ = group.create_task(self._cleanup_loop(), name="github-watch:cleanup")

    async def _poll_loop(self) -> None:
        while True:
            try:
                async with self._ctx.runtime_scope():
                    _, _, _, watch = self.bind()
                    await watch.poll(
                        self._config.repositories,
                        _ProgrammaticMessages(self._ctx.require(PROGRAMMATIC)),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("github-watch poll failed; next interval will retry")
            await self._wait(datetime.now(UTC) + timedelta(seconds=self._config.poll_seconds))

    async def _wait(self, deadline: datetime) -> None:
        handle = self._ctx.require(TIMERS).schedule(deadline)
        try:
            receipt = await handle.result()
            if receipt.status not in {TimerStatus.FIRED, TimerStatus.CANCELLED}:
                raise RuntimeError("github-watch timer returned invalid status")
        except asyncio.CancelledError:
            _ = await handle.cancel()
            raise
        finally:
            await handle.cleanup()

    async def _cleanup_loop(self) -> None:
        catalog = self._ctx.require(MESSAGE_CATALOG)
        async for _heads in catalog.follow():
            async with self._ctx.runtime_scope():
                self.cleanup_completed(catalog, self._ctx.require(TURN_PROJECTION))

    def cleanup_completed(self, catalog: MessageCatalog, projection: TurnProjection) -> None:
        """Delete only checkout files whose admitted Input has reached a Message terminal."""
        ledger, checkouts, _, _ = self.bind()
        for event in ledger.dispatched_events():
            if event.thread_id is None or event.input_message_id is None:
                continue
            try:
                messages = catalog.reader(event.thread_id).snapshot()
            except (KeyError, ValueError):
                continue
            turn = next((
                turn for turn in projection.project(messages, "programmatic")
                if event.input_message_id in turn.message_ids
            ), None)
            if turn is None or turn.status == "open":
                continue
            if not checkouts.cleanup(event.operation_id):
                logger.debug("github-watch checkout already absent event=%s", event.event_key)
            ledger.transition(event.event_key, expected=("dispatched",), status="completed")

    def authorize(self, operation_id: str, session_id: str, *, code: bool = False) -> EventState:
        ledger, _, _, _ = self.bind()
        event = ledger.get_event_by_operation(operation_id)
        if event.status not in {"message_submitting", "dispatched"} or event.thread_id != session_id:
            raise PermissionError("operation does not belong to the current programmatic Session")
        if code and event.trigger_kind != "owner_mention":
            raise PermissionError("code changes require an owner mention event")
        return event

    def operation(self, action: str, event: EventState, arguments: Mapping[str, object]) -> object:
        _, _, operations, _ = self.bind()
        if action == "post_comment":
            return operations.post_comment(event, cast(str, arguments["body"]))
        if action == "submit_review":
            return operations.submit_review(event, cast(str, arguments["body"]))
        if action == "push_branch":
            return operations.push_branch(event, cast(str, arguments["branch_suffix"]))
        if action == "create_pr":
            return operations.create_pull(
                event, title=cast(str, arguments["title"]), body=cast(str, arguments["body"]),
            )
        raise AssertionError(action)


class GitHubTool(BoundTool):
    """Bind one operation to the immutable Message prefix that requested it."""
    idempotent = True

    def __init__(self, runtime: Runtime, action: str) -> None:
        self._runtime = runtime
        self._action = action

    async def prepare(self, arguments: Mapping[str, object],
                      source: CallSource | None = None) -> Mapping[str, object]:
        if self._action == "runtime_info":
            if arguments:
                raise InvalidArguments("runtime info 不接受参数")
            return {}
        model = _MODELS[self._action]
        try:
            request = model.model_validate(json_value(arguments))
        except ValidationError as error:
            raise InvalidArguments(str(error)) from error
        if source is None or not source.messages:
            raise InvalidArguments("GitHub 写操作需要实际 Message 调用来源")
        session_id = source.messages[-1].session_id
        operation_id = cast(str, request.operation_id)
        event = self._runtime.authorize(
            operation_id, session_id,
            code=self._action in {"push_branch", "create_pr"},
        )
        if event.input_message_id is None or not any(
            message.message_id == event.input_message_id and isinstance(message.body, Input)
            for message in source.messages
        ):
            raise InvalidArguments("GitHub operation 缺少原 programmatic Input")
        return {**request.model_dump(mode="json"), "_session_id": session_id}

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        _ = key
        if self._action == "runtime_info":
            value: object = {
                "plugin": name, "version": version,
                "checkout_mode": "detached-commit",
                "mirror_recovery": "worktree-prune-before-fetch",
            }
        else:
            session_id = arguments.get("_session_id")
            operation_id = arguments.get("operation_id")
            if not isinstance(session_id, str) or not isinstance(operation_id, str):
                raise ValueError("prepared GitHub operation identity is invalid")
            event = self._runtime.authorize(
                operation_id, session_id,
                code=self._action in {"push_branch", "create_pr"},
            )
            value = await asyncio.to_thread(self._runtime.operation, self._action, event, arguments)
        return Result("success", (ContentPart("text", json.dumps(value, ensure_ascii=False, sort_keys=True)),))

    async def query(self, key: str) -> Result | None:
        _ = key
        return None


def _definitions() -> tuple[tuple[str, str, Mapping[str, object], str], ...]:
    return (
        ("github_watch_runtime_info", "返回 GitHub Watch 版本和 checkout 恢复策略。",
         {"type": "object", "properties": {}, "additionalProperties": False}, "runtime_info"),
        ("github_watch_post_comment", "以当前 operation 的 GitHub App 发布 Issue comment。",
         BodyInput.model_json_schema(), "post_comment"),
        ("github_watch_submit_review", "以当前 operation 的 GitHub App 提交 COMMENT review。",
         BodyInput.model_json_schema(), "submit_review"),
        ("github_watch_push_branch", "把当前临时仓库提交推到 operation 唯一分支。",
         PushInput.model_json_schema(), "push_branch"),
        ("github_watch_create_pr", "为当前 operation 已推送分支创建 GitHub PR。",
         PullInput.model_json_schema(), "create_pr"),
    )


api_version = 3
name = "github-watch"
version = "4.0.0"
desc = "轮询 GitHub，以 programmatic Message 启动工作并提供受约束的 GitHub 工具。"
Config = GitHubWatchConfig
inject = (TIMERS, PROGRAMMATIC, MESSAGE_CATALOG, TURN_PROJECTION, TOOLS)


GITHUB_WATCH_TOOLS = ServiceKey[ToolView]("github-watch.tools.v1")


async def apply(ctx: Context, config: GitHubWatchConfig) -> None:
    """注册普通 Tool 与生命周期；候选 Root 不打开 PEM、数据库或网络。"""
    runtime = Runtime(ctx, config)
    catalog = ctx.require(TOOLS)
    await catalog.declare_group(ctx, always_on=True, description=desc)
    refs = []
    for tool_name, description, parameters, action in _definitions():
        @asynccontextmanager
        async def open_tool(_state: Mapping[str, object], action: str = action) -> AsyncGenerator[GitHubTool]:
            yield GitHubTool(runtime, action)

        refs.append(await catalog.register(
            ctx, name=tool_name, description=description, parameters=parameters,
            open=open_tool, idempotent=True,
            risk="read-only" if action == "runtime_info" else "external-side-effect",
        ))

    await ctx.provide(GITHUB_WATCH_TOOLS, catalog.view(*refs))

    watcher: asyncio.Task[None] | None = None

    async def start(_event: object) -> None:
        nonlocal watcher
        runtime.bind()
        watcher = await ctx.spawn(runtime.run(), name="github-watch")

    async def stop(_event: object) -> None:
        nonlocal watcher
        if watcher is not None:
            watcher.cancel()
            _ = await asyncio.gather(watcher, return_exceptions=True)
        watcher = None

    _ = await ctx.on(RUNTIME_STARTED, start)
    _ = await ctx.on(RUNTIME_STOPPING, stop)
