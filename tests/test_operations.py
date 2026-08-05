from __future__ import annotations

from pathlib import Path
from typing import Any

from github_watch_test_package.checkout import CheckoutState
from github_watch_test_package.ledger import EventState
from github_watch_test_package.operations import GitHubOperations


def event(*, kind: str = "issue") -> EventState:
    return EventState(
        event_key=f"owner/repo:{kind}:7:opened",
        operation_id="c" * 32,
        repo="owner/repo",
        kind=kind,
        number=7,
        trigger_kind="opened",
        trigger_id="7",
        status="dispatched",
        thread_id="programmatic:one",
        turn_id="turn:one",
    )


class FakeClient:
    def __init__(self) -> None:
        self.comment_rows: list[dict[str, Any]] = []
        self.review_rows: list[dict[str, Any]] = []
        self.pull_rows: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []

    def comments(self, _repo: str, _number: int) -> list[dict[str, Any]]:
        return self.comment_rows

    def post_comment(self, _repo: str, _number: int, body: str) -> dict[str, Any]:
        self.writes.append(("comment", body))
        return {"html_url": "comment-url"}

    def reviews(self, _repo: str, _number: int) -> list[dict[str, Any]]:
        return self.review_rows

    def post_review(self, _repo: str, _number: int, body: str) -> dict[str, Any]:
        self.writes.append(("review", body))
        return {"html_url": "review-url"}

    def pulls(self, _repo: str) -> list[dict[str, Any]]:
        return self.pull_rows

    def repository(self, _repo: str) -> dict[str, Any]:
        return {"default_branch": "main"}

    def create_pull(self, _repo: str, **values: str) -> dict[str, Any]:
        self.writes.append(("pull", values["body"]))
        return {"html_url": "pull-url"}


class FakeCheckouts:
    def push(self, _operation_id: str, branch_suffix: str) -> str:
        return f"akashic/cccccccccccc-{branch_suffix}"

    def get(self, operation_id: str) -> CheckoutState:
        return CheckoutState(
            operation_id,
            "owner/repo",
            Path("/tmp/repository"),
            "base",
            "akashic/cccccccccccc-fix",
        )


def test_comment_and_review_use_owned_markers_and_deduplicate() -> None:
    client = FakeClient()
    operations = GitHubOperations(client, FakeCheckouts())  # type: ignore[arg-type]
    issue = event()
    pull = event(kind="pr")

    assert operations.post_comment(issue, "分析完成") == {
        "duplicate": False,
        "url": "comment-url",
    }
    marker = f"<!-- akashic-operation:{issue.operation_id} -->"
    assert client.writes[-1] == ("comment", f"{marker}\n分析完成")
    client.comment_rows = [{"body": f"{marker}\nold", "html_url": "old-comment"}]
    assert operations.post_comment(issue, "again") == {
        "duplicate": True,
        "url": "old-comment",
    }

    assert operations.submit_review(pull, "review 完成") == {
        "duplicate": False,
        "url": "review-url",
    }
    assert client.writes[-1][0] == "review"


def test_push_and_create_pull_are_bound_to_operation_branch() -> None:
    client = FakeClient()
    operations = GitHubOperations(client, FakeCheckouts())  # type: ignore[arg-type]
    owner_mention = event()

    assert operations.push_branch(owner_mention, "fix") == {
        "branch": "akashic/cccccccccccc-fix"
    }
    assert operations.create_pull(
        owner_mention,
        title="fix: test",
        body="修复测试",
    ) == {"duplicate": False, "url": "pull-url"}
    assert client.writes[-1][0] == "pull"
    assert f"akashic-operation:{owner_mention.operation_id}" in client.writes[-1][1]
