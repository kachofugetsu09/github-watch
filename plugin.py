"""Akashic API v2 entrypoint for the GitHub polling bot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from agent.plugins import (
    IntervalTrigger,
    Plugin,
    PluginJobContext,
    PluginJobSpec,
    tool,
)
from agent.tools.base import get_current_tool_context
from pydantic import BaseModel, Field, field_validator, model_validator

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .github_watch import GitHubWatch
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
    poll_seconds: int = Field(default=60, ge=15)
    checkout_ttl_seconds: int = Field(default=86_400, ge=300)
    control_endpoint: str | None = None
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
    def validate_notification_target(self) -> "GitHubWatchConfig":
        values = (self.notify_channel, self.notify_chat_id)
        if (values[0] is None) != (values[1] is None):
            raise ValueError("notify_channel and notify_chat_id must be configured together")
        if any(value is not None and not value.strip() for value in values):
            raise ValueError("notification target values must not be blank")
        return self


class GitHubWatchPlugin(Plugin):
    api_version = 2
    name = "github-watch"
    version = "1.2.7"
    desc = "Poll GitHub and wake one stable Akashic thread per issue or PR"
    ConfigModel = GitHubWatchConfig

    def activate(self) -> None:
        """Initialize the durable ledger after the generation becomes active."""

        config = cast(GitHubWatchConfig, self.context.config)
        data_dir = self.context.data_dir
        workspace = self.context.workspace
        if data_dir is None or workspace is None:
            raise RuntimeError("github-watch requires plugin data_dir and workspace")
        pem_path = Path(config.pem_path).expanduser()
        if not pem_path.is_file():
            raise FileNotFoundError(f"GitHub App PEM does not exist: {pem_path}")
        data_dir.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(data_dir / "events.sqlite3")
        ledger.integrity_check()
        recovered = ledger.recover_interrupted()
        if any(recovered.values()):
            logger.warning("github-watch recovered interrupted states: %s", recovered)
        endpoint = config.control_endpoint or str(workspace / "akashic.sock")
        client = GitHubClient(
            app_id=config.app_id,
            installation_id=config.installation_id,
            pem_path=pem_path,
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
        self._ledger = ledger
        self._checkouts = checkouts
        self._operations = GitHubOperations(client, checkouts)
        self._watch = GitHubWatch(
            client=client,
            ledger=ledger,
            checkouts=checkouts,
            data_dir=data_dir,
            control_endpoint=endpoint,
            mention=config.mention,
            bot_login=config.bot_login,
            operations=self._operations,
            notify_channel=config.notify_channel,
            notify_chat_id=config.notify_chat_id,
        )

    def jobs(self) -> list[PluginJobSpec]:
        config = cast(GitHubWatchConfig, self.context.config)
        return [
            PluginJobSpec(
                id="poll",
                triggers=[IntervalTrigger(seconds=config.poll_seconds)],
                handler=self.poll,
                coalesce=True,
            )
        ]

    async def poll(self, ctx: PluginJobContext) -> None:
        config = cast(GitHubWatchConfig, ctx.plugin_context.config)
        await self._watch.poll(config.repositories)

    def after_turn_modules(self) -> list[object]:
        return [_CheckoutCleanupModule(self)]

    @tool(
        name="github_watch_post_comment",
        risk="external-side-effect",
        always_on=True,
    )
    async def post_comment(
        self,
        event: object,
        operation_id: str,
        body: str,
    ) -> str:
        """以当前 github-watch operation 绑定的 GitHub App Bot 身份发布 Issue comment。

        Args:
            operation_id: Prompt 中给出的 32 位 operation ID。
            body: 不含 operation marker 的中文评论正文。
        """

        authorized = self._authorized_event(operation_id)
        result = await asyncio.to_thread(
            self._operations.post_comment,
            authorized,
            body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    @tool(
        name="github_watch_submit_review",
        risk="external-side-effect",
        always_on=True,
    )
    async def submit_review(
        self,
        event: object,
        operation_id: str,
        body: str,
    ) -> str:
        """以 GitHub App Bot 身份向当前 PR 提交一次 COMMENT review。

        Args:
            operation_id: Prompt 中给出的 32 位 operation ID。
            body: 不含 operation marker 的中文 review 正文。
        """

        authorized = self._authorized_event(operation_id)
        result = await asyncio.to_thread(
            self._operations.submit_review,
            authorized,
            body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    @tool(
        name="github_watch_push_branch",
        risk="external-side-effect",
        always_on=True,
    )
    async def push_branch(
        self,
        event: object,
        operation_id: str,
        branch_suffix: str,
    ) -> str:
        """用 GitHub App token 将当前临时仓库的已提交改动推到 operation 唯一分支。

        Args:
            operation_id: Prompt 中给出的 32 位 operation ID。
            branch_suffix: 小写字母数字开头的简短分支后缀。
        """

        authorized = self._authorized_code_event(operation_id)
        result = await asyncio.to_thread(
            self._operations.push_branch,
            authorized,
            branch_suffix,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    @tool(
        name="github_watch_create_pr",
        risk="external-side-effect",
        always_on=True,
    )
    async def create_pr(
        self,
        event: object,
        operation_id: str,
        title: str,
        body: str,
    ) -> str:
        """以 GitHub App Bot 身份为当前 operation 已推送的分支创建 PR。

        Args:
            operation_id: Prompt 中给出的 32 位 operation ID。
            title: PR 标题。
            body: 不含 operation marker 的 PR 正文。
        """

        authorized = self._authorized_code_event(operation_id)
        result = await asyncio.to_thread(
            self._operations.create_pull,
            authorized,
            title=title,
            body=body,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    def _authorized_event(self, operation_id: str) -> EventState:
        context = get_current_tool_context()
        if context is None or not context.origin_session_key:
            raise PermissionError("github-watch tool requires a live turn context")
        event = self._ledger.get_event_by_operation(operation_id)
        if (
            event.status not in {"turn_submitting", "dispatched"}
            or event.thread_id != context.origin_session_key
        ):
            raise PermissionError("operation does not belong to the current dispatched thread")
        manager = self.context.session_manager
        if manager is None:
            raise RuntimeError("github-watch requires session_manager")
        session = manager.get_existing(context.origin_session_key)
        metadata = session.metadata
        if (
            metadata.get("source") != "github-watch"
            or metadata.get("repo") != event.repo
            or metadata.get("item") != f"{event.kind}#{event.number}"
        ):
            raise PermissionError("session metadata does not match github-watch event")
        return event

    def _authorized_code_event(self, operation_id: str) -> EventState:
        event = self._authorized_event(operation_id)
        if event.trigger_kind != "owner_mention":
            raise PermissionError("code changes require an owner mention event")
        return event

    def _cleanup_turn(self, session_key: str, turn_id: str) -> None:
        event = self._ledger.get_event_by_turn(turn_id)
        if event is None or event.thread_id != session_key:
            return
        try:
            removed = self._checkouts.cleanup(event.operation_id)
        except OSError:
            logger.exception(
                "github-watch checkout cleanup deferred to TTL event=%s",
                event.event_key,
            )
            return
        if removed:
            logger.info("github-watch checkout removed event=%s", event.event_key)


class _CheckoutCleanupModule:
    slot = "plugin.github_watch.checkout_cleanup"
    requires = ("after_turn.fanout_committed",)

    def __init__(self, plugin: GitHubWatchPlugin) -> None:
        self._plugin = plugin

    async def run(self, frame: Any) -> Any:
        state = frame.input.state
        metadata = state.msg.metadata or {}
        turn_id = metadata.get("control_turn_id")
        if isinstance(turn_id, str) and turn_id:
            await asyncio.to_thread(
                self._plugin._cleanup_turn,
                state.session_key,
                turn_id,
            )
        return frame
