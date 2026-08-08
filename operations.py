"""Repository-bound GitHub effects executed with the App installation identity."""

from __future__ import annotations

from typing import Any

from .checkout import CheckoutManager
from .github_client import GitHubClient
from .ledger import EventState


class GitHubOperations:
    """Deduplicate and execute one event's allowed GitHub effects."""

    def __init__(self, client: GitHubClient, checkouts: CheckoutManager) -> None:
        self._client = client
        self._checkouts = checkouts

    def react(self, event: EventState, content: str) -> dict[str, Any]:
        """React to the issue/PR itself instead of posting a receipt comment."""

        result = self._client.add_reaction(event.repo, event.number, content)
        return {
            "reaction": result.get("content"),
            "url": result.get("html_url"),
            "id": result.get("id"),
        }

    def post_comment(self, event: EventState, body: str) -> dict[str, Any]:
        marker = self._marker(event)
        for comment in self._client.comments(event.repo, event.number):
            if marker in str(comment.get("body") or ""):
                return {"duplicate": True, "url": comment.get("html_url")}
        result = self._client.post_comment(
            event.repo,
            event.number,
            self._marked_body(marker, body),
        )
        return {"duplicate": False, "url": result.get("html_url")}

    def submit_review(self, event: EventState, body: str) -> dict[str, Any]:
        if event.kind != "pr":
            raise ValueError("formal review requires a PR event")
        marker = self._marker(event)
        for review in self._client.reviews(event.repo, event.number):
            if marker in str(review.get("body") or ""):
                return {"duplicate": True, "url": review.get("html_url")}
        result = self._client.post_review(
            event.repo,
            event.number,
            self._marked_body(marker, body),
        )
        return {"duplicate": False, "url": result.get("html_url")}

    def push_branch(self, event: EventState, branch_suffix: str) -> dict[str, str]:
        branch = self._checkouts.push(event.operation_id, branch_suffix)
        return {"branch": branch}

    def create_pull(
        self,
        event: EventState,
        *,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        state = self._checkouts.get(event.operation_id)
        if state.repo != event.repo or state.pushed_branch is None:
            raise RuntimeError("operation has no pushed branch")
        for pull in self._client.pulls(event.repo):
            head = pull.get("head")
            if isinstance(head, dict) and head.get("ref") == state.pushed_branch:
                return {"duplicate": True, "url": pull.get("html_url")}
        repository = self._client.repository(event.repo)
        base = repository.get("default_branch")
        if not isinstance(base, str) or not base:
            raise RuntimeError("repository has no default_branch")
        marker = self._marker(event)
        result = self._client.create_pull(
            event.repo,
            title=title,
            body=self._marked_body(marker, body),
            head=state.pushed_branch,
            base=base,
        )
        return {"duplicate": False, "url": result.get("html_url")}

    @staticmethod
    def _marker(event: EventState) -> str:
        return f"<!-- akashic-operation:{event.operation_id} -->"

    @staticmethod
    def _marked_body(marker: str, body: str) -> str:
        text = body.strip()
        if not text:
            raise ValueError("GitHub body cannot be empty")
        if "<!-- akashic-operation:" in text.casefold():
            raise ValueError("operation marker is owned by github-watch")
        return f"{marker}\n{text}"
