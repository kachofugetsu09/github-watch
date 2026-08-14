from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from github_watch_test_package.github_client import GitHubTransportUnavailable
from github_watch_test_package.github_watch import GitHubWatch
from github_watch_test_package.ledger import EventLedger


class FakeGitHub:
    def __init__(self) -> None:
        self.issue_rows: list[dict[str, Any]] = []
        self.pull_rows: list[dict[str, Any]] = []
        self.comment_rows: dict[int, list[dict[str, Any]]] = {}

    def repository(self, repo: str) -> dict[str, Any]:
        return {"full_name": repo, "owner": {"login": "owner"}}

    def issues(self, repo: str) -> list[dict[str, Any]]:
        return self.issue_rows

    def pulls(self, repo: str) -> list[dict[str, Any]]:
        return self.pull_rows

    def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.comment_rows.get(number, [])


class FakeCheckouts:
    def sweep(self) -> list[str]:
        return []


def item(number: int, updated_at: str) -> dict[str, Any]:
    return {"number": number, "updated_at": updated_at}


def comment(comment_id: int, login: str, body: str) -> dict[str, Any]:
    return {"id": comment_id, "user": {"login": login}, "body": body}


def build_watch(tmp_path: Path, fake: FakeGitHub) -> tuple[GitHubWatch, EventLedger]:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    watch = GitHubWatch(
        client=fake,  # type: ignore[arg-type]
        ledger=ledger,
        checkouts=FakeCheckouts(),  # type: ignore[arg-type]
        data_dir=tmp_path,
        agent_input=object(),  # type: ignore[arg-type]
        mention="@akashic-review-bot",
        bot_login="akashic-review-bot[bot]",
    )
    return watch, ledger


def test_poll_contains_exhausted_transport_failure(
    tmp_path: Path,
    caplog,
) -> None:
    fake = FakeGitHub()

    def unavailable(_repo: str) -> dict[str, Any]:
        raise GitHubTransportUnavailable(
            "GET",
            "/repos/owner/repo",
            retry_at=1_300.0,
            attempts=3,
            detail="unexpected eof",
        )

    fake.repository = unavailable  # type: ignore[method-assign]
    watch, _ = build_watch(tmp_path, fake)

    asyncio.run(watch.poll(["owner/repo"]))

    assert "GET retries exhausted" in caplog.text


def test_baseline_is_silent_and_new_items_are_once_only(tmp_path):
    fake = FakeGitHub()
    fake.issue_rows = [item(1, "t1")]
    fake.comment_rows[1] = [comment(10, "owner", "@akashic-review-bot old")]
    watch, ledger = build_watch(tmp_path, fake)

    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == []

    fake.issue_rows.insert(0, item(2, "t2"))
    watch._discover_repository("owner/repo")
    events = ledger.pending_events()
    assert [event.event_key for event in events] == ["owner/repo:issue:2:opened"]

    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == events


def test_only_new_owner_mention_comment_reawakens(tmp_path):
    fake = FakeGitHub()
    fake.pull_rows = [item(7, "t1")]
    watch, ledger = build_watch(tmp_path, fake)
    watch._discover_repository("owner/repo")

    # A commit/state update changes updated_at but creates no event.
    fake.pull_rows = [item(7, "t2")]
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == []

    # Owner ordinary comments and collaborator mentions only move the cursor.
    fake.comment_rows[7] = [
        comment(1, "owner", "ordinary"),
        comment(2, "collaborator", "@akashic-review-bot please review"),
    ]
    fake.pull_rows = [item(7, "t3")]
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == []

    # A new owner mention gets one stable event identity.
    fake.comment_rows[7].append(
        comment(3, "owner", "@Akashic-Review-Bot please revisit")
    )
    fake.pull_rows = [item(7, "t4")]
    watch._discover_repository("owner/repo")
    events = ledger.pending_events()
    assert [event.event_key for event in events] == ["owner/repo:pr:7:comment:3"]

    # Editing the same comment changes updated_at but not its immutable id.
    fake.comment_rows[7][-1]["body"] = "@akashic-review-bot edited"
    fake.pull_rows = [item(7, "t5")]
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == events


def test_draft_pr_is_observed_but_open_transition_wakes(tmp_path):
    fake = FakeGitHub()
    fake.pull_rows = [item(9, "t1") | {"draft": True}]
    watch, ledger = build_watch(tmp_path, fake)
    watch._discover_repository("owner/repo")

    # Baseline is silent even for a draft PR.
    assert ledger.pending_events() == []

    # A brand-new draft PR is recorded but never wakes an event.
    fake.pull_rows.append(item(10, "t2") | {"draft": True})
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == []

    # draft -> open transition creates exactly one ready_for_review event.
    fake.pull_rows[1]["draft"] = False
    fake.pull_rows[1]["updated_at"] = "t3"
    watch._discover_repository("owner/repo")
    events = ledger.pending_events()
    assert [event.event_key for event in events] == ["owner/repo:pr:10:ready"]
    assert events[0].trigger_kind == "ready_for_review"

    # Re-observation after the transition is silent.
    fake.pull_rows[1]["updated_at"] = "t4"
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == events


def test_code_and_longer_login_do_not_count_as_mentions(tmp_path):
    fake = FakeGitHub()
    fake.issue_rows = [item(8, "t1")]
    watch, ledger = build_watch(tmp_path, fake)
    watch._discover_repository("owner/repo")

    fake.comment_rows[8] = [
        comment(1, "owner", "`@akashic-review-bot` is an example"),
        comment(2, "owner", "@akashic-review-bot-helper please run"),
        comment(3, "owner", "```\n@akashic-review-bot\n```"),
    ]
    fake.issue_rows = [item(8, "t2")]
    watch._discover_repository("owner/repo")
    assert ledger.pending_events() == []


def test_agent_operation_comment_never_reawakens_owner_thread(tmp_path):
    fake = FakeGitHub()
    fake.issue_rows = [item(9, "t1")]
    watch, ledger = build_watch(tmp_path, fake)
    watch._discover_repository("owner/repo")

    fake.comment_rows[9] = [
        comment(
            1,
            "owner",
            "<!-- akashic-operation:abc -->\n@akashic-review-bot analysis complete",
        )
    ]
    fake.issue_rows = [item(9, "t2")]
    watch._discover_repository("owner/repo")

    assert ledger.pending_events() == []
