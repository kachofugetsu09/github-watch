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

    async def start_turn(self, thread_id: str, prompt: str) -> FakeHandle:
        assert thread_id == "thread-1"
        self.prompt = prompt
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
        data_dir=tmp_path,
        control_endpoint="control.sock",
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )

    asyncio.run(watch._dispatch_turn(event, tmp_path / "manifest.json"))

    dispatched = ledger.get_event(event.event_key)
    assert dispatched.status == "dispatched"
    assert dispatched.thread_id == "thread-1"
    assert dispatched.turn_id == "turn-1"
    assert control.closed
    assert control.prompt is not None
    assert "插件不会等待或代发你的最终回复" in control.prompt
    assert f"<!-- akashic-operation:{event.operation_id} -->" in control.prompt
