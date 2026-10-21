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
        return [
            {
                "filename": "README.md",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "changes": 4,
                "patch": "@@ -1 +1,3 @@\n-old\n+new\n",
                "blob_url": "https://example.invalid/blob",
                "contents_url": "https://example.invalid/contents",
            },
            {
                "filename": "assets/logo.png",
                "status": "added",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "binary": True,
            },
        ]

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

    # The index keeps metadata only: no inline patch, no URLs, and it states
    # whether a patch exists instead of embedding one.
    index = json.loads((manifest_path.parent / "files.json").read_text())
    assert [entry["filename"] for entry in index] == ["README.md", "assets/logo.png"]
    assert index[0]["has_patch"] is True and index[1]["has_patch"] is False
    assert all("patch" not in entry for entry in index)
    assert all("blob_url" not in entry and "contents_url" not in entry for entry in index)
    assert index[0]["additions"] == 3 and index[1]["binary"] is True

    # Patches live in their own files and are digest-verified through the manifest.
    assert set(manifest["patches"]) == {"patches/0000-README.md.patch"}
    patch_path = manifest_path.parent / "patches/0000-README.md.patch"
    assert patch_path.read_text() == "@@ -1 +1,3 @@\n-old\n+new\n"
    assert (
        hashlib.sha256(patch_path.read_bytes()).hexdigest()
        == manifest["patches"]["patches/0000-README.md.patch"]["sha256"]
    )

    # The index alone must be smaller than the inline form, which is the whole
    # point: the agent reads the index first and only opens the patches it needs.
    index_bytes = (manifest_path.parent / "files.json").stat().st_size
    inline_form = json.dumps(
        [
            {**entry, "patch": "@@ -1 +1,3 @@\n-old\n+new\n"}
            if entry["has_patch"]
            else entry
            for entry in index
        ],
        ensure_ascii=False,
        indent=2,
    ).encode()
    assert index_bytes < len(inline_form)


def test_duplicate_filenames_do_not_collide(tmp_path):
    """Two entries with the same basename must not overwrite each other's patch."""

    class Renamed(FakeGitHub):
        def files(self, repo: str, number: int) -> list[dict[str, Any]]:
            return [
                {"filename": "a/index.ts", "patch": "@@ a\n"},
                {"filename": "b/index.ts", "patch": "@@ b\n"},
            ]

    event = EventState(
        "owner/repo:pr:2:opened",
        "operation2",
        "owner/repo",
        "pr",
        2,
        "opened",
        "2",
        "claimed",
        None,
    )
    manifest_path = ContextBundle(Renamed(), tmp_path).build(event)  # type: ignore[arg-type]
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest["patches"]) == {
        "patches/0000-index.ts.patch",
        "patches/0001-index.ts.patch",
    }
    assert (manifest_path.parent / "patches/0000-index.ts.patch").read_text() == "@@ a\n"
    assert (manifest_path.parent / "patches/0001-index.ts.patch").read_text() == "@@ b\n"
