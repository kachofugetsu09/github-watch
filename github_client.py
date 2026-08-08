"""GitHub App client with in-memory credentials and conditional pagination."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

API = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_ACCEPT = "application/vnd.github+json"


class GitHubApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"GitHub API {method} {path} -> {status}: {detail}")
        self.status = status


class GitHubRateLimited(GitHubApiError):
    def __init__(self, method: str, path: str, retry_at: float, detail: str) -> None:
        super().__init__(method, path, 429, detail)
        self.retry_at = retry_at


class GitHubClient:
    """Authenticate as one GitHub App installation and expose REST operations."""

    def __init__(self, *, app_id: int, installation_id: int, pem_path: Path) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._pem_path = pem_path
        self._token = ""
        self._token_expires_at = 0.0
        self._blocked_until = 0.0
        self._page_cache: dict[str, tuple[str, Any, str | None]] = {}

    def installation_token(self) -> str:
        """Return a live installation token without persisting it."""

        if self._token and time.time() < self._token_expires_at - 300:
            return self._token
        payload = self.request(
            "POST",
            f"/app/installations/{self._installation_id}/access_tokens",
            token=self._make_jwt(),
            body={},
        )
        if not isinstance(payload, dict):
            raise TypeError("installation token response is not an object")
        token = payload.get("token")
        expires_at = payload.get("expires_at")
        if not isinstance(token, str) or not token:
            raise RuntimeError("installation token response has no token")
        if not isinstance(expires_at, str):
            raise TypeError("installation token response has no expires_at")
        self._token = token
        self._token_expires_at = datetime.fromisoformat(
            expires_at.replace("Z", "+00:00")
        ).timestamp()
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, object] | None = None,
        accept: str = DEFAULT_ACCEPT,
    ) -> Any:
        """Execute one GitHub request and decode its JSON response."""

        raw, _, _ = self._request_raw(
            method, path, token=token, body=body, accept=accept
        )
        return json.loads(raw) if raw else None

    def paginate(
        self, path: str, *, accept: str = DEFAULT_ACCEPT
    ) -> list[dict[str, Any]]:
        """Follow GitHub Link headers and reuse each page through its ETag."""

        next_path: str | None = self._with_page_size(path)
        items: list[dict[str, Any]] = []
        while next_path is not None:
            payload, next_path = self._conditional_page(next_path, accept)
            if not isinstance(payload, list):
                raise TypeError(f"paginated response is not an array: {path}")
            for item in payload:
                if not isinstance(item, dict):
                    raise TypeError(f"paginated member is not an object: {path}")
                items.append(item)
        return items

    def request_text(self, path: str, *, accept: str) -> str:
        """Read a non-JSON GitHub representation with installation auth."""

        raw, _, _ = self._request_raw("GET", path, accept=accept)
        return raw.decode("utf-8", errors="replace")

    def repository(self, repo: str) -> dict[str, Any]:
        return self._require_object(self.request("GET", f"/repos/{repo}"), "repository")

    def issues(self, repo: str) -> list[dict[str, Any]]:
        rows = self.paginate(
            f"/repos/{repo}/issues?state=open&sort=created&direction=desc"
        )
        return [row for row in rows if "pull_request" not in row]

    def pulls(self, repo: str) -> list[dict[str, Any]]:
        return self.paginate(
            f"/repos/{repo}/pulls?state=open&sort=created&direction=desc"
        )

    def issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._require_object(
            self.request("GET", f"/repos/{repo}/issues/{number}"), "issue"
        )

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        return self._require_object(
            self.request("GET", f"/repos/{repo}/pulls/{number}"), "pull"
        )

    def comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/issues/{number}/comments")

    def timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(
            f"/repos/{repo}/issues/{number}/timeline",
            accept="application/vnd.github.mockingbird-preview+json",
        )

    def commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/commits")

    def files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/files")

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/reviews")

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{repo}/pulls/{number}/comments")

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        path = f"/repos/{repo}/commits/{sha}/check-runs"
        next_path: str | None = self._with_page_size(path)
        items: list[dict[str, Any]] = []
        while next_path is not None:
            payload, next_path = self._conditional_page(next_path, DEFAULT_ACCEPT)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("check_runs"), list
            ):
                raise TypeError("check-runs response is invalid")
            items.extend(
                self._require_object(item, "check-run")
                for item in payload["check_runs"]
            )
        return items

    def combined_status(self, repo: str, sha: str) -> dict[str, Any]:
        return self._require_object(
            self.request("GET", f"/repos/{repo}/commits/{sha}/status"),
            "combined-status",
        )

    def pull_diff(self, repo: str, number: int) -> str:
        return self.request_text(
            f"/repos/{repo}/pulls/{number}",
            accept="application/vnd.github.diff",
        )

    def post_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._require_object(
            self.request(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                body={"body": body},
            ),
            "comment",
        )

    def add_reaction(self, repo: str, number: int, content: str) -> dict[str, Any]:
        return self._require_object(
            self.request(
                "POST",
                f"/repos/{repo}/issues/{number}/reactions",
                body={"content": content},
            ),
            "reaction",
        )

    def post_review(self, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._require_object(
            self.request(
                "POST",
                f"/repos/{repo}/pulls/{number}/reviews",
                body={"body": body, "event": "COMMENT"},
            ),
            "review",
        )

    def create_pull(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        return self._require_object(
            self.request(
                "POST",
                f"/repos/{repo}/pulls",
                body={
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                },
            ),
            "pull",
        )

    def _conditional_page(self, path: str, accept: str) -> tuple[Any, str | None]:
        cache_key = f"{accept}\n{path}"
        cached = self._page_cache.get(cache_key)
        headers = {"If-None-Match": cached[0]} if cached else None
        raw, response_headers, status = self._request_raw(
            "GET",
            path,
            accept=accept,
            headers=headers,
            allow_not_modified=True,
        )
        if status == 304:
            if cached is None:
                raise RuntimeError(f"304 response without local cache: {path}")
            return cached[1], cached[2]
        payload = json.loads(raw) if raw else None
        next_path = self._next_path(response_headers.get("Link"))
        etag = response_headers.get("ETag")
        if etag:
            self._page_cache[cache_key] = (etag, payload, next_path)
        return payload, next_path

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, object] | None = None,
        accept: str = DEFAULT_ACCEPT,
        headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[bytes, Any, int]:
        """Execute a request while preserving cache and rate-limit metadata."""

        # 1. Honor a server-declared limit without issuing more HTTP requests
        if token is None and time.time() < self._blocked_until:
            raise GitHubRateLimited(method, path, self._blocked_until, "local backoff")
        auth = token or self.installation_token()
        request_headers = {
            "Authorization": f"Bearer {auth}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "akashic-review-bot",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, method=method, headers=request_headers
        )

        # 2. Preserve a 304 and classify explicit throttling separately
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(), response.headers, response.status
        except urllib.error.HTTPError as error:
            if error.code == 304 and allow_not_modified:
                return b"", error.headers, 304
            detail = error.read().decode(errors="replace")[:1000]
            if error.code in (403, 429):
                retry_at = self._retry_at(error.headers)
                if retry_at is not None:
                    self._blocked_until = retry_at
                    raise GitHubRateLimited(method, path, retry_at, detail) from error
            raise GitHubApiError(method, path, error.code, detail) from error

    def _make_jwt(self) -> str:
        now = int(time.time())
        header = self._b64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = self._b64url(
            json.dumps(
                {"iat": now - 60, "exp": now + 600, "iss": self._app_id},
                separators=(",", ":"),
            ).encode()
        )
        signing = f"{header}.{payload}".encode()
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(self._pem_path)],
            input=signing,
            capture_output=True,
            check=True,
        ).stdout
        return f"{header}.{payload}.{self._b64url(signature)}"

    @staticmethod
    def _with_page_size(path: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}per_page=100"

    @staticmethod
    def _next_path(link: str | None) -> str | None:
        if not link:
            return None
        for entry in link.split(","):
            url, *parameters = entry.strip().split(";")
            if not any(parameter.strip() == 'rel="next"' for parameter in parameters):
                continue
            parsed = urllib.parse.urlparse(url.strip().strip("<>"))
            if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                raise RuntimeError(f"GitHub pagination escaped API origin: {url}")
            return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        return None

    @staticmethod
    def _retry_at(headers: Any) -> float | None:
        retry_after = headers.get("Retry-After")
        if retry_after and str(retry_after).isdigit():
            return time.time() + int(retry_after)
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset and str(reset).isdigit():
            return float(reset)
        return None

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def _require_object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError(f"{label} response is not an object")
        return value
