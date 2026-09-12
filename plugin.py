"""Akashic API v3 entrypoint for the GitHub polling bot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from agent.plugin_composition import (
    Context,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    ServiceKey,
    TIMERS,
    TimerStatus,
)
from agent.plugin_contracts import ContentPart
from agent.tool_catalog import validate_tool_parameters
from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
from bus.events_lifecycle import TurnCommitted
from pydantic import BaseModel, Field, field_validator, model_validator

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .github_watch import (
    GitHubWatch,
    ProgrammaticTurnPort,
    ProgrammaticTurnPreAdmissionError,
    ProgrammaticTurnUncertainError,
)
from .ledger import EventLedger, EventState
from .operations import GitHubOperations

logger = logging.getLogger("plugin.github-watch")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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


ToolRisk = Literal["read-only", "read-write", "external-side-effect"]
ToolOutcome = Literal["success", "denied", "error", "interrupted"]


class _ToolRef(Protocol):
    name: str


class _ProviderBoundTool(Protocol):
    @property
    def idempotent(self) -> bool: ...

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: object | None = None,
    ) -> Mapping[str, object] | str: ...

    async def invoke(
        self,
        key: str,
        arguments: Mapping[str, object],
    ) -> object: ...

    async def query(self, key: str) -> object | None: ...


class _ToolCatalog(Protocol):
    async def declare_group(
        self,
        ctx: Context,
        *,
        always_on: bool = False,
        description: str = "未声明用途",
    ) -> object: ...

    async def register(
        self,
        ctx: Context,
        *,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        open: Callable[
            [Mapping[str, object]],
            AbstractAsyncContextManager[_ProviderBoundTool],
        ],
        public: bool = True,
        idempotent: bool = False,
        risk: ToolRisk = "read-write",
        search_hint: str | None = None,
    ) -> _ToolRef: ...

    async def register_authorize(
        self,
        ctx: Context,
        *,
        tool: _ToolRef,
        name: str,
        authorize: Callable[[Mapping[str, object]], Awaitable[str | None]],
    ) -> object: ...


TOOLS = ServiceKey[_ToolCatalog]("tools.v1")


@dataclass(frozen=True, slots=True)
class _InvocationContext:
    """Carry the source Session identity into the legacy handler boundary."""

    origin_session_key: str = ""


@dataclass(frozen=True, slots=True)
class _ToolResult:
    """Return the structural result expected by the ordinary tools owner."""

    outcome: ToolOutcome
    parts: tuple[ContentPart, ...]


ToolHandler = Callable[[Any, Mapping[str, object]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, object]
    handler: ToolHandler
    risk: ToolRisk
    idempotent: bool
    requires_session: bool = False
    code_only: bool = False


_ORIGIN_SESSION_KEY = "__github_watch_origin_session"


class _CallSource(Protocol):
    messages: tuple[Any, ...]


class _Programmatic(Protocol):
    async def call(self, method: str, params: object) -> Mapping[str, object]: ...


PROGRAMMATIC = ServiceKey("programmatic.v1")


@dataclass(frozen=True, slots=True)
class _SessionAdmitParams:
    session_id: str
    persist_memory: bool = False


@dataclass(frozen=True, slots=True)
class _MessageSendParams:
    session_id: str
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class _AcceptedTurn:
    session_id: str
    turn_id: str


class _ProgrammaticTurnPort:
    """Translate the plugin-owned port into the public programmatic service."""

    def __init__(self, service: _Programmatic) -> None:
        self._service = service

    async def create_session(self, *, metadata: Mapping[str, object]) -> str:
        repo = metadata.get("repo")
        item = metadata.get("item")
        if not isinstance(repo, str) or not repo or not isinstance(item, str) or not item:
            raise ValueError("github-watch Session metadata 缺少稳定 repo/item")
        session_id = "programmatic:github-watch:" + sha256(
            f"{repo}\n{item}".encode("utf-8")
        ).hexdigest()
        result = await self._service.call(
            "programmatic/session/admit",
            _SessionAdmitParams(session_id=session_id, persist_memory=False),
        )
        returned = result.get("session_id")
        if returned != session_id:
            raise ProgrammaticTurnPreAdmissionError(
                "programmatic Session receipt returned mismatched identity"
            )
        return session_id

    async def submit(self, session_id: str, content: str) -> _AcceptedTurn:
        message_id = "github-watch:" + sha256(
            f"{session_id}\n{content}".encode("utf-8")
        ).hexdigest()
        result = await self._service.call(
            "programmatic/message/send",
            _MessageSendParams(
                session_id=session_id,
                message_id=message_id,
                text=content,
            ),
        )
        returned_session = result.get("session_id")
        turn_id = result.get("turn_id")
        if returned_session != session_id:
            raise ProgrammaticTurnUncertainError(
                "programmatic Turn receipt returned mismatched Session identity"
            )
        if not isinstance(turn_id, str) or not turn_id:
            raise ProgrammaticTurnUncertainError(
                "programmatic Turn receipt omitted the accepted turn_id"
            )
        return _AcceptedTurn(session_id, turn_id)


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


def _prepare_tool_arguments(
    spec: _ToolSpec,
    arguments: Mapping[str, object],
    source: _CallSource | None,
) -> Mapping[str, object] | str:
    """Validate the public schema and capture the source Session for effects."""

    if not isinstance(arguments, Mapping):
        return "工具参数必须是对象"
    raw = dict(arguments)
    errors = validate_tool_parameters(
        raw,
        schema=cast(Mapping[str, Any], spec.parameters),
    )
    if errors:
        return "; ".join(errors)
    if not spec.requires_session:
        return raw
    if source is None:
        return "github-watch tool requires a live turn context"
    messages = source.messages
    if not isinstance(messages, tuple) or not messages:
        raise ValueError("工具 CallSource 缺少最后一条消息")
    session_key = messages[-1].session_id
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("工具 CallSource 的 Session 身份无效")
    return {**raw, _ORIGIN_SESSION_KEY: session_key}


class _GitHubWatchTool:
    """Adapt one GitHub Watch handler to the ordinary tools provider ABI."""

    def __init__(self, spec: _ToolSpec) -> None:
        self._spec = spec

    @property
    def idempotent(self) -> bool:
        return self._spec.idempotent

    async def prepare(
        self,
        arguments: Mapping[str, object],
        source: _CallSource | None = None,
    ) -> Mapping[str, object] | str:
        return _prepare_tool_arguments(self._spec, arguments, source)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> _ToolResult:
        del key
        origin_session_key = arguments.get(_ORIGIN_SESSION_KEY, "")
        if not isinstance(origin_session_key, str):
            raise ValueError("github-watch tool 的 Session provenance 损坏")
        result = await self._spec.handler(
            _InvocationContext(origin_session_key),
            arguments,
        )
        return _ToolResult("success", (ContentPart("text", result),))

    async def query(self, key: str) -> None:
        del key
        return None


@asynccontextmanager
async def _open_tool(
    spec: _ToolSpec,
    state: Mapping[str, object],
) -> AsyncIterator[_GitHubWatchTool]:
    """Open one stateless provider target; formal GitHub resources stay lazy."""

    if state:
        raise ValueError("github-watch tools 不接受 binding state")
    yield _GitHubWatchTool(spec)


async def _authorize_tool(
    spec: _ToolSpec,
    arguments: Mapping[str, object],
) -> str | None:
    """Apply the plugin-owned event authorization after parameter preparation."""

    if not spec.requires_session:
        return None
    origin_session_key = arguments.get(_ORIGIN_SESSION_KEY)
    operation_id = arguments.get("operation_id")
    if (
        not isinstance(origin_session_key, str)
        or not origin_session_key
        or not isinstance(operation_id, str)
    ):
        raise ValueError("github-watch tool authorization provenance 损坏")
    context = _InvocationContext(origin_session_key)
    try:
        if spec.code_only:
            _authorized_code_event(context, operation_id)
        else:
            _authorized_event(context, operation_id)
    except PermissionError as error:
        return str(error)
    return None


async def run_github_watch_poll(context: Any) -> None:
    """Poll GitHub and admit each discovered event through the invocation port."""

    config = _require_config()
    bound = _ensure_formal_runtime()
    turns = _ProgrammaticTurnPort(cast(_Programmatic, context.require(PROGRAMMATIC)))
    await bound.watch.poll(config.repositories, cast(ProgrammaticTurnPort, turns))


async def _poll_loop(context: Context) -> None:
    """Run one poll and one source-neutral deadline repeatedly until shutdown."""

    while True:
        await run_github_watch_poll(context)
        deadline = datetime.now(UTC) + timedelta(seconds=_require_config().poll_seconds)
        handle = context.require(TIMERS).schedule(deadline)
        try:
            receipt = await handle.result()
        finally:
            await handle.cleanup()
        if receipt.status != TimerStatus.FIRED:
            return


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


def _tool_specs() -> tuple[_ToolSpec, ...]:
    return (
        _ToolSpec(
            name="github_watch_runtime_info",
            description="返回当前 GitHub Watch 插件版本和 checkout 恢复策略。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=run_github_watch_runtime_info,
            risk="read-only",
            idempotent=True,
        ),
        _ToolSpec(
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
            handler=run_github_watch_post_comment,
            risk="external-side-effect",
            idempotent=False,
            requires_session=True,
        ),
        _ToolSpec(
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
            handler=run_github_watch_submit_review,
            risk="external-side-effect",
            idempotent=False,
            requires_session=True,
        ),
        _ToolSpec(
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
            handler=run_github_watch_push_branch,
            risk="external-side-effect",
            idempotent=False,
            requires_session=True,
            code_only=True,
        ),
        _ToolSpec(
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
            handler=run_github_watch_create_pr,
            risk="external-side-effect",
            idempotent=False,
            requires_session=True,
            code_only=True,
        ),
    )


api_version = 3
name = "github-watch"
version = "3.0.0"
desc = "Poll GitHub and wake one stable Akashic Session per issue or PR"
Config = GitHubWatchConfig
inject = (TIMERS, PROGRAMMATIC, TOOLS)


async def apply(ctx: Context, config: GitHubWatchConfig) -> None:
    """Register ordinary tools and a lifecycle-owned polling task."""

    global _config, _data_dir, _bound
    _config = config
    _data_dir = ctx.data_root
    _bound = None

    # 1. Register declarations only; no candidate data or external client is touched.
    catalog = ctx.require(TOOLS)
    _ = await catalog.declare_group(ctx, always_on=True, description=desc)
    for spec in _tool_specs():
        reference = await catalog.register(
            ctx,
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            open=lambda state, spec=spec: _open_tool(spec, state),
            public=True,
            idempotent=spec.idempotent,
            risk=spec.risk,
        )
        if spec.requires_session:
            _ = await catalog.register_authorize(
                ctx,
                tool=reference,
                name="github-watch.event-authorization",
                authorize=lambda arguments, spec=spec: _authorize_tool(spec, arguments),
            )

    # 2. Poll only after the formal runtime is ready; Context.spawn owns cleanup.
    poller: asyncio.Task[None] | None = None

    async def start(_event: object) -> None:
        nonlocal poller
        if poller is not None and not poller.done():
            raise RuntimeError("github-watch poller 已启动")
        poller = await ctx.spawn(_poll_loop(ctx), name="github-watch-poll")

    async def stop(_event: object) -> None:
        nonlocal poller
        if poller is not None and not poller.done():
            _ = poller.cancel()
            _ = await asyncio.gather(poller, return_exceptions=True)
        poller = None

    _ = await ctx.on(RUNTIME_STARTED, start)
    _ = await ctx.on(RUNTIME_STOPPING, stop)

    # 3. Cleanup observes only the matching session and committed Turn identity.
    await ctx.on(AFTER_TURN_COMMITTED, _on_turn_committed)
