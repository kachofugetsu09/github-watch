"""Materialize complete, immutable GitHub evidence for one agent wake."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .github_client import GitHubClient
from .ledger import EventState, utc_now

# One evidence file per changed file; keeps each patch readable on its own.
PATCH_DIRECTORY = "patches"

# Fields kept in the per-file index. Everything else (patch, blob_url, raw_url,
# contents_url, and any future additions) stays out so the index stays small and
# stable across GitHub response changes.
FILE_INDEX_FIELDS = (
    "filename",
    "status",
    "additions",
    "deletions",
    "changes",
    "previous_filename",
    "binary",
)


def index_file(item: dict[str, Any]) -> dict[str, Any]:
    """Project one GitHub file entry onto the index fields plus patch availability."""

    record = {key: item[key] for key in FILE_INDEX_FIELDS if key in item}
    patch = item.get("patch")
    record["has_patch"] = isinstance(patch, str) and bool(patch)
    return record


def patch_filename(position: int, name: str) -> str:
    """Name one patch file by list position so duplicate names cannot collide."""

    return f"{position:04d}-{Path(name).name or 'file'}.patch"


class ContextBundle:
    """Write one content-addressed evidence directory for an event."""

    def __init__(self, client: GitHubClient, root: Path) -> None:
        self._client = client
        self._root = root

    def build(self, event: EventState) -> Path:
        """Fetch the complete item view and return its verified manifest path."""

        # 1. Fetch fresh GitHub state before waking the agent
        item = (
            self._client.pull(event.repo, event.number)
            if event.kind == "pr"
            else self._client.issue(event.repo, event.number)
        )
        payloads: dict[str, Any] = {
            "item.json": item,
            "issue_comments.json": self._client.comments(event.repo, event.number),
            "timeline.json": self._client.timeline(event.repo, event.number),
        }
        # 2. Split the file listing: the index stays inline, patches get one file each.
        patches: dict[str, str] = {}
        if event.kind == "pr":
            head = item.get("head")
            if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
                raise RuntimeError("PR 上下文缺少 head.sha")
            sha = head["sha"]
            listed = self._client.files(event.repo, event.number)
            index: list[dict[str, Any]] = []
            for position, entry in enumerate(listed):
                index.append(index_file(entry))
                patch = entry.get("patch")
                if isinstance(patch, str) and patch:
                    patches[patch_filename(position, str(entry.get("filename", "file")))] = patch
            payloads.update(
                {
                    "files.json": index,
                    "commits.json": self._client.commits(event.repo, event.number),
                    "reviews.json": self._client.reviews(event.repo, event.number),
                    "review_comments.json": self._client.review_comments(
                        event.repo, event.number
                    ),
                    "check_runs.json": self._client.check_runs(event.repo, sha),
                    "combined_status.json": self._client.combined_status(
                        event.repo, sha
                    ),
                }
            )

        # 3. Write immutable evidence and a digest manifest
        bundle_dir = self._root / "evidence" / event.operation_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        digests: dict[str, dict[str, object]] = {}
        for name, payload in payloads.items():
            raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
            (bundle_dir / name).write_bytes(raw)
            digests[name] = self._digest(raw, payload)

        patch_digests: dict[str, dict[str, object]] = {}
        if patches:
            patch_dir = bundle_dir / PATCH_DIRECTORY
            patch_dir.mkdir(parents=True, exist_ok=True)
            for name, patch in patches.items():
                raw = patch.encode()
                (patch_dir / name).write_bytes(raw)
                patch_digests[f"{PATCH_DIRECTORY}/{name}"] = self._digest(raw, patch)

        manifest = {
            "schema_version": 2,
            "created_at": utc_now(),
            "event_key": event.event_key,
            "operation_id": event.operation_id,
            "repo": event.repo,
            "kind": event.kind,
            "number": event.number,
            "trigger": {"kind": event.trigger_kind, "id": event.trigger_id},
            "files": digests,
            "patches": patch_digests,
        }
        manifest_path = bundle_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest_path

    @staticmethod
    def _digest(raw: bytes, payload: Any) -> dict[str, object]:
        record: dict[str, object] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        if isinstance(payload, list):
            record["items"] = len(payload)
        return record
