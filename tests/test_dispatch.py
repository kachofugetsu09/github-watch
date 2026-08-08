from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from github_watch_test_package import github_watch as watch_module
from github_watch_test_package.github_watch import GitHubWatch
from github_watch_test_package.ledger import EventLedger


class FakeGitHub:
    pass


class FakeHandle:
    id = "turn-1"

    async def result(self) -> dict[str, Any]:
        raise AssertionError("fire-and-forget dispatch must not await turn result")

    async def interrupt(self) -> dict[str, Any]:
        raise AssertionError("accepted fire-and-forget turn must not be interrupted")


class FakeControl:
    def __init__(self) -> None:
        self.prompt: str | None = None
        self.closed = False

    async def __aenter__(self) -> FakeControl:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def start_turn(
        self, thread_id: str, prompt: str, detached: bool = False
    ) -> FakeHandle:
        assert thread_id == "thread-1"
        self.prompt = prompt
        self.detached = detached
        return FakeHandle()


def test_dispatch_returns_after_turn_admission_without_waiting_for_result(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    ledger.set_thread("owner/repo", "issue", 1, "thread-1")
    event = ledger.create_event(
        event_key="owner/repo:issue:1:opened",
        repo="owner/repo",
        kind="issue",
        number=1,
        trigger_kind="opened",
        trigger_id="1",
    )
    assert event is not None
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")
    control = FakeControl()

    class FakeControlClient:
        @staticmethod
        async def connect(endpoint: str) -> FakeControl:
            assert endpoint == "control.sock"
            return control

    monkeypatch.setattr(watch_module, "ControlClient", FakeControlClient)
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        control_endpoint="control.sock",
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
        notify_channel="mobile",
        notify_chat_id="main-chat",
    )

    checkout = tmp_path / "checkout"
    asyncio.run(watch._dispatch_turn(event, tmp_path / "manifest.json", checkout))

    dispatched = ledger.get_event(event.event_key)
    assert dispatched.status == "dispatched"
    assert dispatched.thread_id == "thread-1"
    assert dispatched.turn_id == "turn-1"
    assert control.closed
    assert control.detached
    assert control.prompt is not None
    assert "插件不会等待或代发最终回复" in control.prompt
    assert str(checkout) in control.prompt
    assert "github_watch_post_comment" in control.prompt
    assert "禁止使用系统 gh" in control.prompt
    assert "门槛必须很高" in control.prompt
    assert "每个 operation 最多调用一次" in control.prompt
    assert "target_channel='mobile'" in control.prompt
    assert "target_chat_id='main-chat'" in control.prompt
    assert "`Fixes #1`" in control.prompt
    assert "每个 Issue 修复链只创建一个 PR" in control.prompt


def test_pr_prompt_forbids_recursive_replacement_pull_request(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "pr", 7, "t1", 0)
    event = ledger.create_event(
        event_key="owner/repo:pr:7:comment:99",
        repo="owner/repo",
        kind="pr",
        number=7,
        trigger_kind="owner_mention",
        trigger_id="99",
    )
    assert event is not None
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        control_endpoint="control.sock",
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    prompt = watch._build_prompt(event, tmp_path / "manifest.json", tmp_path / "checkout")

    assert "当前对象已经是 PR" in prompt
    assert "禁止把 github_watch_create_pr 当作无法更新当前 PR 的 fallback" in prompt
    assert "必须在当前 PR 评论说明阻塞并请求 owner 决策" in prompt
    assert "只有 owner 明确要求另开替代 PR 时才可创建" in prompt
    assert "supersedes 当前 PR" in prompt


def test_prompt_forbids_message_push_without_notification_target(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        control_endpoint="control.sock",
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    assert watch._notification_prompt() == "主 channel 通知未配置，禁止调用 message_push。"
