from __future__ import annotations

import asyncio
from pathlib import Path

from github_watch_test_package.github_watch import GitHubWatch
from github_watch_test_package.ledger import EventLedger


class FakeGitHub:
    pass


class FakeAgentInput:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.submitted: list[tuple[str, str]] = []
        self.prompt: str | None = None

    async def create_session(self, metadata: dict[str, object]) -> str:
        self.created.append(dict(metadata))
        return "thread-created"

    async def submit(self, session_id: str, content: str) -> str:
        self.submitted.append((session_id, content))
        self.prompt = content
        return "turn-1"


def test_dispatch_returns_after_turn_admission_without_waiting_for_result(
    tmp_path: Path,
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
    agent_input = FakeAgentInput()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        agent_input=agent_input,
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
    assert agent_input.created == []
    assert [item[0] for item in agent_input.submitted] == ["thread-1"]
    assert agent_input.prompt is not None
    assert "插件不会等待或代发最终回复" in agent_input.prompt
    assert str(checkout) in agent_input.prompt
    assert "github_watch_post_comment" in agent_input.prompt
    assert "禁止使用系统 gh" in agent_input.prompt
    assert "门槛必须很高" in agent_input.prompt
    assert "每个 operation 最多调用一次" in agent_input.prompt
    assert "target_channel='mobile'" in agent_input.prompt
    assert "target_chat_id='main-chat'" in agent_input.prompt
    assert "`Fixes #1`" in agent_input.prompt
    assert "每个 Issue 修复链只创建一个 PR" in agent_input.prompt


def test_dispatch_persists_new_session_before_turn_submission(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
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
    agent_input = FakeAgentInput()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        agent_input=agent_input,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    asyncio.run(
        watch._dispatch_turn(
            event,
            tmp_path / "manifest.json",
            tmp_path / "checkout",
        )
    )

    assert agent_input.created == [
        {
            "skip_post_memory": True,
            "skip_memory_retrieval": True,
            "source": "github-watch",
            "repo": "owner/repo",
            "item": "issue#1",
        }
    ]
    assert agent_input.submitted[0][0] == "thread-created"
    item = ledger.get_item("owner/repo", "issue", 1)
    assert item is not None and item.thread_id == "thread-created"


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
        agent_input=FakeAgentInput(),
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
        agent_input=FakeAgentInput(),
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    assert watch._notification_prompt() == "主 channel 通知未配置，禁止调用 message_push。"
