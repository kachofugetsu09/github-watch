from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from github_watch_test_package.github_watch import (
    GitHubWatch, _input_message_id, _session_id,
)
from github_watch_test_package.ledger import EventLedger


class FakeGitHub:
    pass


class FakeMessages:
    def __init__(self) -> None:
        self.admitted: list[str] = []
        self.submitted: list[tuple[str, str, str]] = []

    async def admit(self, session_id: str) -> None:
        self.admitted.append(session_id)

    async def submit(self, session_id: str, message_id: str, content: str) -> str:
        self.submitted.append((session_id, message_id, content))
        return message_id


def _event(ledger: EventLedger, *, trigger: str = "opened", trigger_id: str = "1"):
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    event = ledger.create_event(
        event_key=f"owner/repo:issue:1:{trigger}:{trigger_id}",
        repo="owner/repo", kind="issue", number=1,
        trigger_kind=trigger, trigger_id=trigger_id,
    )
    assert event is not None
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")
    return event


def _watch(ledger: EventLedger, tmp_path: Path) -> GitHubWatch:
    return GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger, checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path, mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )


def test_dispatch_admits_stable_session_and_exact_input_message(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event(ledger)
    messages = FakeMessages()
    watch = _watch(ledger, tmp_path)

    asyncio.run(watch._dispatch_message(
        event, tmp_path / "manifest.json", tmp_path / "checkout", messages,
    ))

    expected_session = _session_id(event)
    expected_input = _input_message_id(event)
    assert messages.admitted == [expected_session]
    assert [(row[0], row[1]) for row in messages.submitted] == [
        (expected_session, expected_input)
    ]
    assert "github_watch_post_comment" in messages.submitted[0][2]
    saved = ledger.get_event(event.event_key)
    assert saved.status == "dispatched"
    assert saved.thread_id == expected_session
    assert saved.input_message_id == expected_input
    assert saved.turn_id is None


def test_next_event_reuses_session_and_gets_distinct_input(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    first = _event(ledger)
    watch = _watch(ledger, tmp_path)
    messages = FakeMessages()
    asyncio.run(watch._dispatch_message(
        first, tmp_path / "manifest.json", tmp_path / "checkout", messages,
    ))
    second = ledger.create_event(
        event_key="owner/repo:issue:1:comment:2", repo="owner/repo",
        kind="issue", number=1, trigger_kind="owner_mention", trigger_id="2",
    )
    assert second is not None
    ledger.transition(second.event_key, expected=("discovered",), status="claimed")
    ledger.transition(second.event_key, expected=("claimed",), status="context_ready")
    asyncio.run(watch._dispatch_message(
        second, tmp_path / "manifest-2.json", tmp_path / "checkout-2", messages,
    ))

    assert messages.admitted[0] == messages.admitted[1]
    assert messages.submitted[0][1] != messages.submitted[1][1]


def test_submission_failure_requeues_same_idempotent_message(tmp_path: Path, monkeypatch) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)
    event = ledger.create_event(
        event_key="owner/repo:issue:1:opened", repo="owner/repo",
        kind="issue", number=1, trigger_kind="opened", trigger_id="1",
    )
    assert event is not None

    class Checkouts:
        def prepare(self, _event: object, _item: object) -> object:
            return SimpleNamespace(path=tmp_path / "checkout")

        def cleanup(self, _operation_id: str) -> bool:
            return True

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger, checkouts=Checkouts(),  # type: ignore[arg-type]
        data_dir=tmp_path, mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    class Failing(FakeMessages):
        async def submit(self, session_id: str, message_id: str, content: str) -> str:
            self.submitted.append((session_id, message_id, content))
            raise RuntimeError("connection closed after append")

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    (evidence / "item.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch._context, "build", lambda _event: manifest)
    failing = Failing()
    with pytest.raises(RuntimeError, match="connection closed"):
        asyncio.run(watch._process_event(event, failing))
    current = ledger.get_event(event.event_key)
    assert current.status == "discovered"
    assert current.input_message_id == _input_message_id(event)


def test_prompt_preserves_review_pr_and_notification_rules(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event(ledger, trigger="owner_mention", trigger_id="2")
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger, checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path, mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]", notify_channel="mobile",
        notify_chat_id="main-chat",
    )
    prompt = watch._build_prompt(event, tmp_path / "manifest.json", tmp_path / "checkout")
    assert "只有这条 mention 明确要求修改代码" in prompt
    assert "Fixes #1" in prompt
    assert "每个 Issue 修复链只创建一个 PR" in prompt
    assert "target_channel='mobile'" in prompt
    assert "每个 operation 最多调用一次" in prompt
    assert "对应 Message 工作单元终结后删除" in prompt


def test_prompt_forbids_notification_without_target(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    watch = _watch(ledger, tmp_path)
    assert watch._notification_prompt() == "主 channel 通知未配置，禁止调用 message_push。"
