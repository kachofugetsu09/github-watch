from __future__ import annotations

import hashlib
import json
from typing import Any

from github_watch_test_package.context_bundle import ContextBundle
from github_watch_test_package.ledger import EventState


class FakeGitHub:
    def pull(self, repo: str, number: int) -> dict[str, Any]:
        return {"number": number, "head": {"sha": "abc"}}

    def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [{"id": 1}]

    def timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [{"event": "opened"}]

    def commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [{"sha": "abc"}]

    def files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return [{"filename": "README.md"}]

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        return []

    def combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        return {"state": "pending"}

    def pull_diff(self, repo: str, number: int) -> str:
        return "diff --git a/README.md b/README.md\n"


def test_pr_bundle_contains_complete_views_and_verified_digests(tmp_path):
    event = EventState(
        "owner/repo:pr:1:opened",
        "operation",
        "owner/repo",
        "pr",
        1,
        "opened",
        "1",
        "claimed",
        None,
        None,
    )
    manifest_path = ContextBundle(FakeGitHub(), tmp_path).build(event)  # type: ignore[arg-type]
    manifest = json.loads(manifest_path.read_text())
    expected = {
        "item.json",
        "issue_comments.json",
        "timeline.json",
        "commits.json",
        "files.json",
        "reviews.json",
        "review_comments.json",
        "check_runs.json",
        "combined_status.json",
    }
    assert set(manifest["files"]) == expected
    for name, record in manifest["files"].items():
        raw = (manifest_path.parent / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["sha256"]
