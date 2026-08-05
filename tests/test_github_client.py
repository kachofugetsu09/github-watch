from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from github_watch_test_package.github_client import GitHubClient


class FakePagedClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__(app_id=1, installation_id=2, pem_path=Path("unused"))
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, object] | None = None,
        accept: str = "application/vnd.github+json",
        headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[bytes, Any, int]:
        del method, token, body, accept, allow_not_modified
        self.calls.append((path, headers))
        if headers is not None:
            return b"", {}, 304
        if "page=2" in path:
            return json.dumps([{"id": 2}]).encode(), {"ETag": '"two"'}, 200
        link = '<https://api.github.com/resource?per_page=100&page=2>; rel="next"'
        return json.dumps([{"id": 1}]).encode(), {"ETag": '"one"', "Link": link}, 200


def test_pagination_follows_link_and_reuses_conditional_pages():
    client = FakePagedClient()
    assert client.paginate("/resource") == [{"id": 1}, {"id": 2}]
    assert client.paginate("/resource") == [{"id": 1}, {"id": 2}]
    assert client.calls == [
        ("/resource?per_page=100", None),
        ("/resource?per_page=100&page=2", None),
        ("/resource?per_page=100", {"If-None-Match": '"one"'}),
        ("/resource?per_page=100&page=2", {"If-None-Match": '"two"'}),
    ]
