"""Polling coordinator for idempotent GitHub issue and pull-request handling."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from agent.control.client import ControlClient

from .context_bundle import ContextBundle
from .github_client import GitHubClient
from .ledger import EventLedger, EventState

logger = logging.getLogger("plugin.github-watch")


class GitHubWatch:
    """Discover allowed events, wake stable threads, and publish verified comments."""

    def __init__(
        self,
        *,
        client: GitHubClient,
        ledger: EventLedger,
        data_dir: Path,
        control_endpoint: str,
        mention: str,
        bot_login: str,
        turn_timeout_seconds: int,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._context = ContextBundle(client, data_dir)
        self._control_endpoint = control_endpoint
        escaped_mention = re.escape(mention.casefold())
        self._mention_pattern = re.compile(
            rf"(?<![A-Za-z0-9-]){escaped_mention}(?![A-Za-z0-9-])"
        )
        self._bot_login = bot_login.casefold()
        self._turn_timeout_seconds = turn_timeout_seconds

    async def poll(self, repositories: list[str]) -> None:
        """Poll repositories serially, then drain each newly discovered event once."""

        # 1. Discover events and advance observation cursors
        for repo in repositories:
            await asyncio.to_thread(self._discover_repository, repo)

        # 2. Run only durable events that have never begun an external effect
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
            baseline: list[tuple[str, int, str, int]] = []
            for kind, item in rows:
                number, updated_at = self._item_identity(item)
                comments = self._client.comments(repo, number)
                baseline.append(
                    (kind, number, updated_at, self._maximum_comment_id(comments))
                )
            self._ledger.establish_baseline(repo, baseline)
            logger.info(
                "github-watch baseline established repo=%s items=%d", repo, len(rows)
            )
            return

        # 2. New objects wake once; updates wake only on a new owner mention comment
        for kind, item in rows:
            number, updated_at = self._item_identity(item)
            current = self._ledger.get_item(repo, kind, number)
            if current is None:
                comments = self._client.comments(repo, number)
                last_comment_id = self._maximum_comment_id(comments)
                inserted = self._ledger.insert_item(
                    repo, kind, number, updated_at, last_comment_id
                )
                if inserted:
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
        except Exception as exc:
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
            response = await self._run_turn(event, manifest)
        except asyncio.TimeoutError:
            self._ledger.transition(
                event.event_key,
                expected=("turn_running",),
                status="failed_timeout",
                error=f"turn exceeded {self._turn_timeout_seconds}s and was interrupted",
            )
            return
        except Exception as exc:
            current = self._ledger.get_event(event.event_key)
            self._ledger.transition(
                event.event_key,
                expected=(current.status,),
                status="failed_turn",
                error=repr(exc),
            )
            logger.exception("github-watch turn failed event=%s", event.event_key)
            return
        if not response.strip():
            self._ledger.transition(
                event.event_key,
                expected=("turn_running",),
                status="failed_empty_response",
                error="agent returned an empty final response",
            )
            return
        response = response.strip()[:60_000]
        self._ledger.transition(
            event.event_key,
            expected=("turn_running",),
            status="comment_posting",
            response=response,
        )

        # Once POST begins, any failure is uncertain and must never auto-repeat.
        comment_body = f"<!-- akashic-operation:{event.operation_id} -->\n{response}"
        try:
            comment = await asyncio.to_thread(
                self._client.post_comment,
                event.repo,
                event.number,
                comment_body,
            )
            comment_id = self._comment_id(comment)
            comments = await asyncio.to_thread(
                self._client.comments, event.repo, event.number
            )
            confirmed = any(
                self._comment_id(row) == comment_id and row.get("body") == comment_body
                for row in comments
            )
            if not confirmed:
                raise RuntimeError(f"comment {comment_id} readback mismatch")
        except Exception as exc:
            self._ledger.transition(
                event.event_key,
                expected=("comment_posting",),
                status="comment_unconfirmed",
                error=repr(exc),
            )
            logger.exception("github-watch comment uncertain event=%s", event.event_key)
            return
        self._ledger.transition(
            event.event_key,
            expected=("comment_posting",),
            status="completed",
            artifact_id=str(comment_id),
        )
        logger.info(
            "github-watch completed event=%s thread=%s turn=%s comment=%s",
            event.event_key,
            self._ledger.get_event(event.event_key).thread_id,
            self._ledger.get_event(event.event_key).turn_id,
            comment_id,
        )

    async def _run_turn(self, event: EventState, manifest: Path) -> str:
        """Reuse one item thread and interrupt timed-out remote work explicitly."""

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
                status="turn_running",
                thread_id=thread_id,
            )
            handle = await client.start_turn(
                thread_id, self._build_prompt(event, manifest)
            )
            self._ledger.transition(
                event.event_key,
                expected=("turn_running",),
                status="turn_running",
                turn_id=handle.id,
            )
            try:
                turn = await asyncio.wait_for(
                    handle.result(), timeout=self._turn_timeout_seconds
                )
            except asyncio.TimeoutError:
                await handle.interrupt()
                raise
        response = turn.get("finalResponse")
        if not isinstance(response, str):
            raise TypeError(f"turn/completed missing finalResponse: {turn}")
        return response

    @staticmethod
    def _build_prompt(event: EventState, manifest: Path) -> str:
        permission = (
            "这是仓库 owner 的新 @mention。只有这条 mention 明确要求修改代码或创建 PR 时，"
            "你才可以使用本地 shell 修改代码或创建 PR；否则严禁改动仓库或 GitHub 状态。"
            if event.trigger_kind == "owner_mention"
            else "这是对新对象的首次处理。只做分析，严禁修改代码、push、创建 PR 或直接评论。"
        )
        return f"""[github-watch]
仓库: {event.repo}
对象: {event.kind} #{event.number}
触发: {event.trigger_kind} {event.trigger_id}
证据清单: {manifest}

你处在这个 Issue/PR 的稳定专用 thread 中，上一次评论和完整历史都在该 thread 与本次证据包中。
先读 manifest 和其列出的全部文件，以新鲜 GitHub 证据为准。{permission}
不要自己调用 GitHub 评论 API；最终回复只输出要由插件发布的中文 comment 正文，不要加代码块或机器可读包装。"""

    @staticmethod
    def _item_identity(item: dict[str, Any]) -> tuple[int, str]:
        number = item.get("number")
        updated_at = item.get("updated_at")
        if not isinstance(number, int) or not isinstance(updated_at, str):
            raise TypeError("GitHub item missing number/updated_at")
        return number, updated_at

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
