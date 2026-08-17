from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.plugin_composition import (
    ProgrammaticTurnPreAdmissionError,
    ProgrammaticTurnUncertainError,
)
from github_watch_test_package.github_watch import GitHubWatch
from github_watch_test_package.ledger import EventLedger


class FakeGitHub:
    pass


class FakeTurns:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.submitted: list[tuple[str, str]] = []
        self.prompt: str | None = None

    async def create_session(self, *, metadata: dict[str, object]) -> str:
        self.created.append(dict(metadata))
        return "thread-created"

    async def submit(self, session_id: str, content: str) -> object:
        self.submitted.append((session_id, content))
        self.prompt = content
        return SimpleNamespace(session_id=session_id, turn_id="turn-1")


def _event_in_context_ready(ledger: EventLedger):
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
    return event


def _prepare_context(monkeypatch, watch: GitHubWatch, tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest = evidence / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    (evidence / "item.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch._context, "build", lambda _event: manifest)
    monkeypatch.setattr(
        watch._checkouts,
        "prepare",
        lambda _event, _item: SimpleNamespace(path=tmp_path / "checkout"),
    )


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
    turns = FakeTurns()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
        notify_channel="mobile",
        notify_chat_id="main-chat",
    )

    checkout = tmp_path / "checkout"
    asyncio.run(watch._dispatch_turn(event, tmp_path / "manifest.json", checkout, turns))

    dispatched = ledger.get_event(event.event_key)
    assert dispatched.status == "dispatched"
    assert dispatched.thread_id == "thread-1"
    assert dispatched.turn_id == "turn-1"
    assert turns.created == []
    assert [item[0] for item in turns.submitted] == ["thread-1"]
    assert turns.prompt is not None
    assert "插件不会等待或代发最终回复" in turns.prompt
    assert str(checkout) in turns.prompt
    assert "github_watch_post_comment" in turns.prompt
    assert "禁止使用系统 gh" in turns.prompt
    assert "门槛必须很高" in turns.prompt
    assert "每个 operation 最多调用一次" in turns.prompt
    assert "target_channel='mobile'" in turns.prompt
    assert "target_chat_id='main-chat'" in turns.prompt
    assert "`Fixes #1`" in turns.prompt
    assert "每个 Issue 修复链只创建一个 PR" in turns.prompt


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
    turns = FakeTurns()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    asyncio.run(
        watch._dispatch_turn(
            event,
            tmp_path / "manifest.json",
            tmp_path / "checkout",
            turns,
        )
    )

    assert turns.created == [
        {
            "skip_memory_retrieval": True,
            "source": "github-watch",
            "repo": "owner/repo",
            "item": "issue#1",
        }
    ]
    assert turns.submitted[0][0] == "thread-created"
    item = ledger.get_item("owner/repo", "issue", 1)
    assert item is not None and item.thread_id == "thread-created"


def test_dispatch_reuses_durable_session_across_invocations_without_duplicate_turn(
    tmp_path: Path,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.establish_baseline("owner/repo", [])
    assert ledger.insert_item("owner/repo", "issue", 1, "t1", 0)

    first_event = ledger.create_event(
        event_key="owner/repo:issue:1:opened",
        repo="owner/repo",
        kind="issue",
        number=1,
        trigger_kind="opened",
        trigger_id="1",
    )
    assert first_event is not None
    ledger.transition(first_event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(first_event.event_key, expected=("claimed",), status="context_ready")
    first_turns = FakeTurns()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    awaitable = watch._dispatch_turn(
        first_event,
        tmp_path / "manifest.json",
        tmp_path / "checkout",
        first_turns,
    )
    asyncio.run(awaitable)

    second_event = ledger.create_event(
        event_key="owner/repo:issue:1:comment:2",
        repo="owner/repo",
        kind="issue",
        number=1,
        trigger_kind="owner_mention",
        trigger_id="2",
    )
    assert second_event is not None
    ledger.transition(second_event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(second_event.event_key, expected=("claimed",), status="context_ready")

    class ReuseTurns:
        def __init__(self) -> None:
            self.submitted: list[tuple[str, str]] = []

        async def submit(self, session_id: str, content: str) -> object:
            self.submitted.append((session_id, content))
            return SimpleNamespace(session_id=session_id, turn_id="turn-2")

        async def create_session(self, *, metadata: dict[str, object]) -> str:
            del metadata
            raise AssertionError("durable Session should be reused")

    second_turns = ReuseTurns()
    asyncio.run(
        watch._dispatch_turn(
            second_event,
            tmp_path / "manifest.json",
            tmp_path / "checkout",
            second_turns,
        )
    )

    assert len(first_turns.submitted) == 1
    assert len(second_turns.submitted) == 1
    assert second_turns.submitted[0][0] == "thread-created"
    assert ledger.get_event(first_event.event_key).turn_id == "turn-1"
    assert ledger.get_event(second_event.event_key).turn_id == "turn-2"


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
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    assert watch._notification_prompt() == "主 channel 通知未配置，禁止调用 message_push。"


def test_dispatch_in_process_failure_is_retryable_before_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)
    cleanup_calls: list[str] = []

    class FailingTurns:
        async def submit(self, _session_id: str, _content: str) -> object:
            raise ProgrammaticTurnPreAdmissionError("Core admission precondition failed")

    class Checkouts:
        def prepare(self, _event: object, _item: object) -> object:
            return SimpleNamespace(path=tmp_path / "checkout")

        def cleanup(self, operation_id: str) -> bool:
            cleanup_calls.append(operation_id)
            return True

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=Checkouts(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    _prepare_context(monkeypatch, watch, tmp_path)

    asyncio.run(watch._process_event(event, FailingTurns()))

    assert ledger.get_event(event.event_key).status == "discovered"
    assert cleanup_calls == [event.operation_id]

    retry_turns = FakeTurns()
    asyncio.run(watch._process_event(ledger.get_event(event.event_key), retry_turns))
    assert ledger.get_event(event.event_key).status == "dispatched"
    assert len(retry_turns.submitted) == 1


def test_dispatch_does_not_guess_for_an_unclassified_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)
    cleanup_calls: list[str] = []

    class UnexpectedTurns:
        async def submit(self, _session_id: str, _content: str) -> object:
            raise RuntimeError("unclassified Core failure")

    class Checkouts:
        def prepare(self, _event: object, _item: object) -> object:
            return SimpleNamespace(path=tmp_path / "checkout")

        def cleanup(self, operation_id: str) -> bool:
            cleanup_calls.append(operation_id)
            return True

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=Checkouts(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    _prepare_context(monkeypatch, watch, tmp_path)

    with pytest.raises(RuntimeError, match="unclassified Core failure"):
        asyncio.run(watch._process_event(event, UnexpectedTurns()))

    assert ledger.get_event(event.event_key).status == "turn_submitting"
    assert cleanup_calls == []


def test_prompt_failure_stays_before_uncertain_turn_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")
    turns = FakeTurns()
    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    monkeypatch.setattr(
        watch,
        "_build_prompt",
        lambda *_args: (_ for _ in ()).throw(OSError("prompt input unavailable")),
    )

    with pytest.raises(OSError, match="prompt input unavailable"):
        asyncio.run(
            watch._dispatch_turn(
                event,
                tmp_path / "manifest.json",
                tmp_path / "checkout",
                turns,
            )
        )

    assert ledger.get_event(event.event_key).status == "context_ready"
    assert turns.submitted == []


@pytest.mark.parametrize(
    "receipt",
    (
        SimpleNamespace(session_id="wrong-session", turn_id="turn-1"),
        SimpleNamespace(session_id="thread-1", turn_id=""),
    ),
)
def test_invalid_core_receipt_is_uncertain_after_submission(
    tmp_path: Path,
    receipt: object,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)
    ledger.transition(event.event_key, expected=("discovered",), status="claimed")
    ledger.transition(event.event_key, expected=("claimed",), status="context_ready")

    class InvalidReceiptTurns(FakeTurns):
        async def submit(self, session_id: str, content: str) -> object:
            self.submitted.append((session_id, content))
            return receipt

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=object(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    turns = InvalidReceiptTurns()

    with pytest.raises(ProgrammaticTurnUncertainError):
        asyncio.run(
            watch._dispatch_turn(
                event,
                tmp_path / "manifest.json",
                tmp_path / "checkout",
                turns,
            )
        )

    assert ledger.get_event(event.event_key).status == "turn_submitting"
    assert len(turns.submitted) == 1


def test_dispatch_cancelled_admission_requires_manual_reconcile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)

    class CancelledTurns:
        calls = 0

        async def submit(self, _session_id: str, _content: str) -> object:
            self.calls += 1
            raise asyncio.CancelledError

    class Checkouts:
        def prepare(self, _event: object, _item: object) -> object:
            return SimpleNamespace(path=tmp_path / "checkout")

        def cleanup(self, _operation_id: str) -> bool:
            raise AssertionError("uncertain admission must not retry cleanup")

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=Checkouts(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    _prepare_context(monkeypatch, watch, tmp_path)

    turns = CancelledTurns()
    asyncio.run(watch._process_event(event, turns))

    assert ledger.get_event(event.event_key).status == "manual_reconcile"
    assert turns.calls == 1


def test_dispatch_core_uncertain_failure_requires_manual_reconcile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    event = _event_in_context_ready(ledger)

    class UncertainTurns:
        calls = 0

        async def submit(self, _session_id: str, _content: str) -> object:
            self.calls += 1
            raise ProgrammaticTurnUncertainError("receipt could not be confirmed")

    class Checkouts:
        def prepare(self, _event: object, _item: object) -> object:
            return SimpleNamespace(path=tmp_path / "checkout")

        def cleanup(self, _operation_id: str) -> bool:
            raise AssertionError("uncertain admission must not retry cleanup")

    watch = GitHubWatch(
        client=FakeGitHub(),  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=Checkouts(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    _prepare_context(monkeypatch, watch, tmp_path)

    turns = UncertainTurns()
    asyncio.run(watch._process_event(event, turns))

    assert ledger.get_event(event.event_key).status == "manual_reconcile"
    assert turns.calls == 1
