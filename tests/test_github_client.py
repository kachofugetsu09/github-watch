from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from github_watch_test_package.github_client import (
    TRANSPORT_COOLDOWN_SECONDS,
    GitHubClient,
    GitHubTransportUnavailable,
)


class FakeResponse:
    def __init__(self, payload: bytes = b"{}") -> None:
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def transport_client() -> GitHubClient:
    client = GitHubClient(app_id=1, installation_id=2, pem_path=Path("unused"))
    client._token = "token"
    client._token_expires_at = float("inf")
    return client


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


def test_issue_and_pull_lists_request_only_open_objects() -> None:
    issue_client = FakePagedClient()
    assert issue_client.issues("owner/repo") == [{"id": 1}, {"id": 2}]
    assert issue_client.calls[0][0].startswith(
        "/repos/owner/repo/issues?state=open&"
    )

    pull_client = FakePagedClient()
    assert pull_client.pulls("owner/repo") == [{"id": 1}, {"id": 2}]
    assert pull_client.calls[0][0].startswith(
        "/repos/owner/repo/pulls?state=open&"
    )


class FakeWriteClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__(app_id=1, installation_id=2, pem_path=Path("unused"))
        self.writes: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, object] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        del token, accept
        self.writes.append((method, path, body))
        return {"id": 1}


def test_review_and_pull_writes_use_rest_endpoints_and_comment_review_event() -> None:
    client = FakeWriteClient()

    assert client.post_review("owner/repo", 7, "body") == {"id": 1}
    assert client.create_pull(
        "owner/repo",
        title="title",
        body="body",
        head="branch",
        base="main",
    ) == {"id": 1}
    assert client.writes == [
        (
            "POST",
            "/repos/owner/repo/pulls/7/reviews",
            {"body": "body", "event": "COMMENT"},
        ),
        (
            "POST",
            "/repos/owner/repo/pulls",
            {
                "title": "title",
                "body": "body",
                "head": "branch",
                "base": "main",
            },
        ),
    ]


def test_get_retries_transient_tls_and_partial_read_failures() -> None:
    client = transport_client()
    failures = [
        urllib.error.URLError(ssl.SSLEOFError(8, "unexpected eof")),
        http.client.IncompleteRead(b"partial", 10),
        FakeResponse(b'{"ok": true}'),
    ]

    with (
        patch(
            "github_watch_test_package.github_client.urllib.request.urlopen",
            side_effect=failures,
        ) as urlopen,
        patch("github_watch_test_package.github_client.time.sleep") as sleep,
    ):
        assert client.request("GET", "/repos/owner/repo") == {"ok": True}

    assert urlopen.call_count == 3
    assert sleep.call_count == 2


def test_write_transport_failure_is_never_retried() -> None:
    client = transport_client()
    failure = urllib.error.URLError(ssl.SSLEOFError(8, "unexpected eof"))

    with patch(
        "github_watch_test_package.github_client.urllib.request.urlopen",
        side_effect=failure,
    ) as urlopen:
        with pytest.raises(urllib.error.URLError):
            client.request("POST", "/repos/owner/repo/issues/1/comments", body={})

    assert urlopen.call_count == 1


def test_installation_token_exchange_retries_transport_failure() -> None:
    client = transport_client()
    path = f"/app/installations/{client._installation_id}/access_tokens"
    failure = urllib.error.URLError(ssl.SSLEOFError(8, "unexpected eof"))

    with (
        patch(
            "github_watch_test_package.github_client.urllib.request.urlopen",
            side_effect=[failure, FakeResponse(b'{"token": "fresh"}')],
        ) as urlopen,
        patch("github_watch_test_package.github_client.time.sleep") as sleep,
    ):
        raw, _, status = client._request_raw(
            "POST",
            path,
            token="app-jwt",
            body={},
        )

    assert json.loads(raw) == {"token": "fresh"}
    assert status == 200
    assert urlopen.call_count == 2
    assert sleep.call_count == 1


def test_get_retry_exhaustion_enters_cooldown_then_recovers() -> None:
    client = transport_client()
    now = [1_000.0]
    failure = urllib.error.URLError(ssl.SSLEOFError(8, "unexpected eof"))

    with (
        patch(
            "github_watch_test_package.github_client.urllib.request.urlopen",
            side_effect=failure,
        ) as urlopen,
        patch("github_watch_test_package.github_client.time.sleep"),
        patch(
            "github_watch_test_package.github_client.time.time",
            side_effect=lambda: now[0],
        ),
    ):
        with pytest.raises(GitHubTransportUnavailable) as exhausted:
            client.request("GET", "/repos/owner/repo")
        assert exhausted.value.attempts == 3
        assert exhausted.value.retry_at == now[0] + TRANSPORT_COOLDOWN_SECONDS

        with pytest.raises(GitHubTransportUnavailable) as cooldown:
            client.request("GET", "/repos/owner/repo")
        assert cooldown.value.attempts == 0
        assert urlopen.call_count == 3

    now[0] += TRANSPORT_COOLDOWN_SECONDS
    with (
        patch(
            "github_watch_test_package.github_client.urllib.request.urlopen",
            return_value=FakeResponse(b'{"ok": true}'),
        ),
        patch(
            "github_watch_test_package.github_client.time.time",
            side_effect=lambda: now[0],
        ),
    ):
        assert client.request("GET", "/repos/owner/repo") == {"ok": True}
    assert client._transport_blocked_until == 0.0


def test_http_not_modified_marks_transport_recovered() -> None:
    client = transport_client()
    client._transport_degraded = True
    response = urllib.error.HTTPError(
        "https://api.github.com/repos/owner/repo/issues",
        304,
        "Not Modified",
        {},
        None,
    )

    with patch(
        "github_watch_test_package.github_client.urllib.request.urlopen",
        side_effect=response,
    ):
        raw, _, status = client._request_raw(
            "GET",
            "/repos/owner/repo/issues",
            allow_not_modified=True,
        )

    assert raw == b""
    assert status == 304
    assert client._transport_degraded is False
    assert client._transport_blocked_until == 0.0
