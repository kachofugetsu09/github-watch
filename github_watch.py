"""Polling coordinator for idempotent GitHub issue and pull-request handling."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.control.client import ControlClient

from .context_bundle import ContextBundle
from .checkout import CheckoutManager
from .github_client import GitHubClient
from .ledger import EventLedger, EventState
from .operations import GitHubOperations

logger = logging.getLogger("plugin.github-watch")


class GitHubWatch:
    """Discover allowed events and submit each wake to one stable thread."""

    def __init__(
        self,
        *,
        client: GitHubClient,
        ledger: EventLedger,
        checkouts: CheckoutManager,
        data_dir: Path,
        control_endpoint: str,
        mention: str,
        bot_login: str,
        operations: GitHubOperations | None = None,
        notify_channel: str | None = None,
        notify_chat_id: str | None = None,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._checkouts = checkouts
        self._operations = operations
        self._context = ContextBundle(client, data_dir)
        self._control_endpoint = control_endpoint
        escaped_mention = re.escape(mention.casefold())
        self._mention_pattern = re.compile(
            rf"(?<![A-Za-z0-9-]){escaped_mention}(?![A-Za-z0-9-])"
        )
        self._bot_login = bot_login.casefold()
        self._notify_channel = notify_channel
        self._notify_chat_id = notify_chat_id

    async def poll(self, repositories: list[str]) -> None:
        """Poll repositories serially, then drain each newly discovered event once."""

        # 1. Recover checkout storage before reading new remote state.
        removed = await asyncio.to_thread(self._checkouts.sweep)
        if removed:
            logger.warning("github-watch swept expired checkouts count=%d", removed)

        # 2. Discover events and advance observation cursors
        for repo in repositories:
            await asyncio.to_thread(self._discover_repository, repo)

        # 3. Run only durable events that have never begun an external effect
        for event in self._ledger.pending_events():
            await self._process_event(event)

    def _discover_repository(self, repo: str) -> None:
        """Classify fresh GitHub state without treating updated_at as a wake signal."""

        repository = self._client.repository(repo)
        owner = self._require_login(repository.get("owner"), "repository.owner")
        rows = [("issue", row) for row in self._client.issues(repo)]
        rows.extend(("pr", row) for row in self._client.pulls(repo))

        # 1. First observation is a silent baseline
        if not self._ledger.has_baseline(repo):
            baseline: list[tuple[str, int, str, int, bool]] = []
            for kind, item in rows:
                number, updated_at = self._item_identity(item)
                comments = self._client.comments(repo, number)
                baseline.append(
                    (
                        kind,
                        number,
                        updated_at,
                        self._maximum_comment_id(comments),
                        self._item_draft(kind, item),
                    )
                )
            self._ledger.establish_baseline(repo, baseline)
            logger.info(
                "github-watch baseline established repo=%s items=%d", repo, len(rows)
            )
            return

        # 2. New objects wake once; updates wake only on a new owner mention
        #    comment or a draft-to-open transition.
        for kind, item in rows:
            number, updated_at = self._item_identity(item)
            draft = self._item_draft(kind, item)
            current = self._ledger.get_item(repo, kind, number)
            if current is None:
                comments = self._client.comments(repo, number)
                last_comment_id = self._maximum_comment_id(comments)
                inserted = self._ledger.insert_item(
                    repo, kind, number, updated_at, last_comment_id, draft=draft
                )
                if inserted and not draft:
                    self._ledger.create_event(
                        event_key=f"{repo}:{kind}:{number}:opened",
                        repo=repo,
                        kind=kind,
                        number=number,
                        trigger_kind="opened",
                        trigger_id=str(number),
                    )
                continue
            if current.last_updated_at == updated_at:
                continue
            if draft != current.draft:
                if not draft:
                    self._ledger.create_event(
                        event_key=f"{repo}:{kind}:{number}:ready",
                        repo=repo,
                        kind=kind,
                        number=number,
                        trigger_kind="ready_for_review",
                        trigger_id=str(number),
                    )
                self._ledger.observe_item(
                    repo,
                    kind,
                    number,
                    updated_at=updated_at,
                    last_comment_id=current.last_comment_id,
                    draft=draft,
                )
                continue
            comments = self._client.comments(repo, number)
            for comment in comments:
                comment_id = self._comment_id(comment)
                if comment_id <= current.last_comment_id:
                    continue
                login = self._require_login(comment.get("user"), "comment.user")
                body = comment.get("body")
                if (
                    login.casefold() == owner.casefold()
                    and login.casefold() != self._bot_login
                    and isinstance(body, str)
                    and not self._has_operation_marker(body)
                    and self._contains_mention(body)
                ):
                    self._ledger.create_event(
                        event_key=f"{repo}:{kind}:{number}:comment:{comment_id}",
                        repo=repo,
                        kind=kind,
                        number=number,
                        trigger_kind="owner_mention",
                        trigger_id=str(comment_id),
                    )
            self._ledger.observe_item(
                repo,
                kind,
                number,
                updated_at=updated_at,
                last_comment_id=self._maximum_comment_id(comments),
            )

    async def _process_event(self, event: EventState) -> None:
        """Run one event through explicit pre-effect and external-effect phases."""

        self._ledger.transition(
            event.event_key, expected=("discovered",), status="claimed"
        )
        try:
            manifest = await asyncio.to_thread(self._context.build, event)
            item = json.loads((manifest.parent / "item.json").read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise TypeError("github-watch item evidence is not an object")
            checkout = await asyncio.to_thread(self._checkouts.prepare, event, item)
        except Exception as exc:
            await asyncio.to_thread(self._checkouts.cleanup, event.operation_id)
            self._ledger.transition(
                event.event_key,
                expected=("claimed",),
                status="discovered",
                error=repr(exc),
            )
            logger.exception(
                "github-watch context failed safely; retry next poll event=%s",
                event.event_key,
            )
            return
        self._ledger.transition(
            event.event_key,
            expected=("claimed",),
            status="context_ready",
            artifact_id=str(manifest),
        )

        try:
            await self._dispatch_turn(event, manifest, checkout.path)
        except Exception as exc:
            current = self._ledger.get_event(event.event_key)
            if current.status == "context_ready":
                await asyncio.to_thread(self._checkouts.cleanup, event.operation_id)
                self._ledger.transition(
                    event.event_key,
                    expected=("context_ready",),
                    status="discovered",
                    error=repr(exc),
                )
                logger.exception(
                    "github-watch dispatch failed safely; retry next poll event=%s",
                    event.event_key,
                )
                return
            self._ledger.transition(
                event.event_key,
                expected=(current.status,),
                status="dispatch_unconfirmed",
                error=repr(exc),
            )
            logger.exception(
                "github-watch dispatch is uncertain; refusing retry event=%s",
                event.event_key,
            )
            return
        logger.info(
            "github-watch dispatched event=%s thread=%s turn=%s",
            event.event_key,
            self._ledger.get_event(event.event_key).thread_id,
            self._ledger.get_event(event.event_key).turn_id,
        )

    async def _dispatch_turn(
        self,
        event: EventState,
        manifest: Path,
        checkout_path: Path,
    ) -> None:
        """Submit one durable turn and return immediately after admission."""

        async with await ControlClient.connect(self._control_endpoint) as client:
            item = self._ledger.get_item(event.repo, event.kind, event.number)
            if item is None:
                raise RuntimeError(f"event item missing: {event.event_key}")
            thread_id = item.thread_id
            if thread_id is None:
                thread = await client.start_thread(
                    metadata={
                        "skip_post_memory": True,
                        "skip_memory_retrieval": True,
                        "source": "github-watch",
                        "repo": event.repo,
                        "item": f"{event.kind}#{event.number}",
                    }
                )
                raw_thread_id = thread.get("id") or thread.get("threadId")
                if not isinstance(raw_thread_id, str) or not raw_thread_id:
                    raise RuntimeError(f"thread/start missing id: {thread}")
                thread_id = raw_thread_id
                self._ledger.set_thread(event.repo, event.kind, event.number, thread_id)
            self._ledger.transition(
                event.event_key,
                expected=("context_ready",),
                status="turn_submitting",
                thread_id=thread_id,
            )
            handle = await client.start_turn(
                thread_id, self._build_prompt(event, manifest, checkout_path)
            )
            self._ledger.transition(
                event.event_key,
                expected=("turn_submitting",),
                status="dispatched",
                turn_id=handle.id,
            )
            await self._ack_dispatched(event)

    async def _ack_dispatched(self, event: EventState) -> None:
        """Post a receipt emoji on the item once a turn has been admitted."""

        if self._operations is None:
            return
        try:
            await asyncio.to_thread(
                self._operations.post_comment,
                event,
                ":eyes: 已收到，分身开始处理",
            )
        except Exception:
            logger.warning(
                "github-watch ack comment failed event=%s", event.event_key, exc_info=True
            )

    def _build_prompt(
        self,
        event: EventState,
        manifest: Path,
        checkout_path: Path,
    ) -> str:
        permission = (
            "这是仓库 owner 的新 @mention。只有这条 mention 明确要求修改代码或创建 PR 时，"
            "你才可以使用本地 shell 修改代码或创建 PR；否则只能分析并发布 comment。"
            if event.trigger_kind == "owner_mention"
            else "这是对新对象的首次处理。只做分析，严禁修改代码、push 或创建 PR。"
        )
        delivery = (
            "完成分析后必须调用 github_watch_submit_review 发布一条 COMMENT review。"
            if event.kind == "pr"
            else "完成分析后必须调用 github_watch_post_comment 发布一条 comment。"
        )
        pull_request_contract = (
            f"如果本轮为当前 Issue 创建修复 PR，PR 正文必须包含独立一行 `Fixes #{event.number}`，"
            "让 PR 合入默认分支时自动关闭当前 Issue。每个 Issue 修复链只创建一个 PR；"
            "后续修改应继续使用该 PR，禁止递归创建替代 PR。"
            if event.kind == "issue"
            else "当前对象已经是 PR。owner 要求修改、补测试或继续处理时，默认只在当前 PR 上"
            "review/comment，禁止把 github_watch_create_pr 当作无法更新当前 PR 的 fallback。"
            "若现有工具无法把提交推回当前 PR head，必须在当前 PR 评论说明阻塞并请求 owner 决策。"
            "只有 owner 明确要求另开替代 PR 时才可创建；替代 PR 必须保留关联 Issue 的 closing "
            "keyword，并在正文说明它 supersedes 当前 PR。"
        )
        notification = self._notification_prompt()
        return f"""[github-watch fire-and-forget]
仓库: {event.repo}
对象: {event.kind} #{event.number}
触发: {event.trigger_kind} {event.trigger_id}
操作标识: {event.operation_id}
证据清单: {manifest}
临时仓库: {checkout_path}

你处在这个 Issue/PR 的稳定专用 thread 中，上一次评论和完整历史都在该 thread 与本次证据包中。
先读 manifest 和全部证据，再只在临时仓库中读取、测试或修改，以新鲜 GitHub 证据为准。{permission}
禁止使用系统 gh、个人 GitHub 凭证或直接 git push；所有 GitHub 写操作必须使用 github_watch_* 工具，
这些工具会绑定当前 operation、仓库和 Bot installation identity。{delivery}
{pull_request_contract}
{notification}
工具会自动添加并检查 operation marker，不要自行写 marker。若明确获准修改并创建 PR，先在临时仓库
完成并提交改动，再调用 github_watch_push_branch，最后调用 github_watch_create_pr。
插件不会等待或代发最终回复；GitHub 写操作失败时让本轮明确失败，不要伪装成功。临时仓库由宿主在
本轮 after-turn 后删除，崩溃遗留由下一轮 TTL sweeper 回收。"""

    def _notification_prompt(self) -> str:
        """生成当前 turn 的窄范围选择性通知合同。"""

        if self._notify_channel is None or self._notify_chat_id is None:
            return "主 channel 通知未配置，禁止调用 message_push。"
        return f"""你可以选择是否用 message_push 通知维护者的主 channel，但门槛必须很高：
- 需要维护者作出选择、批准或补充关键信息，导致当前工作无法安全继续；或者
- 出现阻塞、高风险、关键失败，或你非常确信这是维护者会希望立刻知道的重要结果。
普通成功、常规 review、过程进度、重复结论和无须行动的信息禁止推送。每个 operation 最多调用一次
message_push；消息必须简短，写明仓库与 Issue/PR、为何值得打扰，以及需要维护者做什么（如有）。
固定参数：target_channel={self._notify_channel!r}，target_chat_id={self._notify_chat_id!r}。
message_push 是 fire-and-forget；调用后继续完成 GitHub comment/review，不等待主 channel 回复。"""

    @staticmethod
    def _item_identity(item: dict[str, Any]) -> tuple[int, str]:
        number = item.get("number")
        updated_at = item.get("updated_at")
        if not isinstance(number, int) or not isinstance(updated_at, str):
            raise TypeError("GitHub item missing number/updated_at")
        return number, updated_at

    @staticmethod
    def _item_draft(kind: str, item: dict[str, Any]) -> bool:
        if kind != "pr":
            return False
        return bool(item.get("draft", False))

    @staticmethod
    def _comment_id(comment: dict[str, Any]) -> int:
        comment_id = comment.get("id")
        if not isinstance(comment_id, int):
            raise TypeError("GitHub comment missing integer id")
        return comment_id

    @classmethod
    def _maximum_comment_id(cls, comments: list[dict[str, Any]]) -> int:
        return max((cls._comment_id(comment) for comment in comments), default=0)

    @staticmethod
    def _require_login(value: object, label: str) -> str:
        if not isinstance(value, dict) or not isinstance(value.get("login"), str):
            raise TypeError(f"{label} missing login")
        return value["login"]

    def _contains_mention(self, body: str) -> bool:
        without_fences = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
        without_code = re.sub(r"`[^`]*`", "", without_fences)
        return self._mention_pattern.search(without_code.casefold()) is not None

    @staticmethod
    def _has_operation_marker(body: str) -> bool:
        return "<!-- akashic-operation:" in body.casefold()
