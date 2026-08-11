"""Own short-lived Git checkouts authenticated as the GitHub App installation."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .github_client import GitHubClient
from .ledger import EventState

_LOGGER = logging.getLogger("plugin.github-watch")


_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_BRANCH_SUFFIX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


@dataclass(frozen=True)
class CheckoutState:
    operation_id: str
    repo: str
    path: Path
    base_sha: str
    pushed_branch: str | None


class CheckoutManager:
    """Create, authenticate, inspect, push, and remove operation-owned repositories."""

    def __init__(
        self,
        client: GitHubClient,
        *,
        root: Path | None = None,
        mirror_root: Path | None = None,
        ttl_seconds: int = 86_400,
    ) -> None:
        self._client = client
        self.root = root or Path(tempfile.gettempdir()) / "akashic-github-watch"
        self._mirror_root = mirror_root or self.root / "mirror"
        self._ttl_seconds = ttl_seconds

    def prepare(self, event: EventState, item: dict[str, Any]) -> CheckoutState:
        """Checkout one exact PR head from a persistent mirror without credentials."""

        # 1. Establish a unique operation directory and credential-free remote.
        operation_dir = self._operation_dir(event.operation_id)
        self.cleanup(event.operation_id)
        operation_dir.mkdir(parents=True, mode=0o700)
        os.chmod(operation_dir, 0o700)
        repository_dir = operation_dir / "repository"
        try:
            # 2. Persist one full mirror per repository; refresh incrementally.
            mirror = self._ensure_mirror(event.repo, operation_dir)
            self._refresh_mirror(mirror, operation_dir)

            # 3. Create a local worktree at the PR head or the default branch.
            if event.kind == "pr":
                head_sha = self._pr_head_sha(item)
                self._run(
                    [
                        "git",
                        "-C",
                        str(mirror),
                        "worktree",
                        "add",
                        "--detach",
                        str(repository_dir),
                        f"refs/pull/{event.number}/head",
                    ]
                )
                actual = self._git_output(repository_dir, "rev-parse", "HEAD")
                if actual != head_sha:
                    raise RuntimeError(
                        f"PR checkout identity mismatch: expected={head_sha} actual={actual}"
                    )
            else:
                default_branch = self._git_output(mirror, "symbolic-ref", "--short", "HEAD")
                head_sha = self._git_output(mirror, "rev-parse", default_branch)
                self._run(
                    [
                        "git",
                        "-C",
                        str(mirror),
                        "worktree",
                        "add",
                        "--detach",
                        str(repository_dir),
                        head_sha,
                    ]
                )

            # 4. Bind commits to the App identity (shared mirror config, idempotent).
            self._run(
                [
                    "git",
                    "-C",
                    str(repository_dir),
                    "config",
                    "user.name",
                    "akashic-review-bot[bot]",
                ]
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(repository_dir),
                    "config",
                    "user.email",
                    "akashic-review-bot[bot]@users.noreply.github.com",
                ]
            )
            state = CheckoutState(
                operation_id=event.operation_id,
                repo=event.repo,
                path=repository_dir,
                base_sha=head_sha,
                pushed_branch=None,
            )
            self._write_state(state)
            return state
        except BaseException:
            self.cleanup(event.operation_id)
            raise

    def get(self, operation_id: str) -> CheckoutState:
        """Read and validate one live checkout's non-secret ownership record."""

        operation_dir = self._operation_dir(operation_id)
        state_path = operation_dir / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        expected = {"schema_version", "operation_id", "repo", "base_sha", "pushed_branch"}
        if set(payload) != expected or payload["schema_version"] != 1:
            raise ValueError(f"invalid checkout state: {state_path}")
        if payload["operation_id"] != operation_id:
            raise ValueError(f"checkout operation mismatch: {state_path}")
        repo = payload["repo"]
        base_sha = payload["base_sha"]
        pushed_branch = payload["pushed_branch"]
        if not isinstance(repo, str) or not isinstance(base_sha, str):
            raise TypeError(f"invalid checkout identity: {state_path}")
        if pushed_branch is not None and not isinstance(pushed_branch, str):
            raise TypeError(f"invalid checkout branch: {state_path}")
        repository_dir = operation_dir / "repository"
        if not (repository_dir / ".git").exists():
            raise FileNotFoundError(f"checkout repository missing: {repository_dir}")
        return CheckoutState(operation_id, repo, repository_dir, base_sha, pushed_branch)

    def push(self, operation_id: str, branch_suffix: str) -> str:
        """Push the committed checkout HEAD to one operation-unique non-force branch."""

        if _BRANCH_SUFFIX.fullmatch(branch_suffix) is None:
            raise ValueError("branch_suffix must match [a-z0-9][a-z0-9._-]{0,39}")
        state = self.get(operation_id)
        status = self._git_output(state.path, "status", "--porcelain")
        if status:
            raise RuntimeError("checkout has uncommitted changes")
        head = self._git_output(state.path, "rev-parse", "HEAD")
        if head == state.base_sha:
            raise RuntimeError("checkout has no committed changes")
        branch = f"akashic/{operation_id[:12]}-{branch_suffix}"
        self._run_authenticated(
            state.path.parent,
            [
                "git",
                "-c",
                "credential.helper=",
                "-C",
                str(state.path),
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            ],
        )
        pushed = CheckoutState(
            state.operation_id,
            state.repo,
            state.path,
            state.base_sha,
            branch,
        )
        self._write_state(pushed)
        return branch

    def cleanup(self, operation_id: str) -> bool:
        """Remove exactly one validated operation directory and its worktree."""

        operation_dir = self._operation_dir(operation_id)
        if not operation_dir.exists():
            return False
        if operation_dir.is_symlink() or not operation_dir.is_dir():
            raise RuntimeError(f"refusing unsafe checkout cleanup: {operation_dir}")
        repository_dir = operation_dir / "repository"
        self._remove_worktree(repository_dir)
        shutil.rmtree(operation_dir)
        return True

    def _remove_worktree(self, repository_dir: Path) -> None:
        """Detach one operation worktree from its mirror when a worktree exists."""

        gitfile = repository_dir / ".git"
        if not gitfile.is_file():
            return
        raw = gitfile.read_text(encoding="utf-8", errors="replace").strip()
        if not raw.startswith("gitdir:"):
            return
        gitdir = Path(raw.split(":", 1)[1].strip())
        if not gitdir.is_absolute() or not gitdir.is_dir():
            return
        mirror = gitdir.parent.parent.parent
        if not (mirror / "objects").is_dir() or not (mirror / "HEAD").is_file():
            return
        try:
            self._run(
                [
                    "git",
                    "-C",
                    str(mirror),
                    "worktree",
                    "remove",
                    "--force",
                    str(repository_dir),
                ]
            )
        except Exception:
            logger = _LOGGER
            if logger is not None:
                logger.warning(
                    "github-watch worktree remove failed path=%s", repository_dir
                )

    def _mirror_path(self, repo: str) -> Path:
        owner, name = repo.split("/", 1)
        return self._mirror_root / owner / f"{name}.git"

    def _ensure_mirror(self, repo: str, operation_dir: Path) -> Path:
        """Clone the repository once as a bare mirror; reuse it afterwards."""

        mirror = self._mirror_path(repo)
        if (mirror / "HEAD").is_file() and (mirror / "objects").is_dir():
            return mirror
        mirror.parent.mkdir(parents=True, exist_ok=True)
        self._run_authenticated(
            operation_dir,
            [
                "git",
                "-c",
                "credential.helper=",
                "clone",
                "--mirror",
                f"https://github.com/{repo}.git",
                str(mirror),
            ],
        )
        return mirror

    def _refresh_mirror(self, mirror: Path, operation_dir: Path) -> None:
        """Incrementally fetch open PR heads and branch refs into the mirror."""

        # 1. Drop administrative records whose operation worktrees no longer exist.
        self._run(
            [
                "git",
                "-C",
                str(mirror),
                "worktree",
                "prune",
                "--expire",
                "now",
            ]
        )

        # 2. Refresh refs after no stale worktree can keep a branch checked out.
        self._run_authenticated(
            operation_dir,
            [
                "git",
                "-c",
                "credential.helper=",
                "-C",
                str(mirror),
                "fetch",
                "origin",
                "--prune",
                "+refs/pull/*/head:refs/pull/*/head",
                "+refs/heads/*:refs/heads/*",
            ],
        )

    def sweep(self) -> int:
        """Remove expired operation directories left by interrupted turns."""

        if not self.root.exists():
            return 0
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError(f"refusing unsafe checkout root: {self.root}")
        cutoff = time.time() - self._ttl_seconds
        removed = 0
        for candidate in self.root.iterdir():
            if _OPERATION_ID.fullmatch(candidate.name) is None:
                continue
            if candidate.stat().st_mtime >= cutoff:
                continue
            removed += int(self.cleanup(candidate.name))
        return removed

    def _run_authenticated(self, operation_dir: Path, command: list[str]) -> None:
        askpass = operation_dir / ".askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_WATCH_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        environment = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITHUB_WATCH_TOKEN": self._client.installation_token(),
        }
        try:
            self._run(command, env=environment)
        finally:
            askpass.unlink(missing_ok=True)

    @staticmethod
    def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
        subprocess.run(
            command,
            env=env,
            check=True,
            text=True,
            capture_output=True,
        )

    @classmethod
    def _git_output(cls, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def _operation_dir(self, operation_id: str) -> Path:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError(f"invalid operation_id: {operation_id!r}")
        if self.root.exists() and self.root.is_symlink():
            raise RuntimeError(f"refusing symlink checkout root: {self.root}")
        root = self.root.resolve(strict=False)
        operation_dir = root / operation_id
        if operation_dir.parent != root:
            raise RuntimeError(f"checkout path escaped root: {operation_dir}")
        return operation_dir

    def _write_state(self, state: CheckoutState) -> None:
        state_path = state.path.parent / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": state.operation_id,
                    "repo": state.repo,
                    "base_sha": state.base_sha,
                    "pushed_branch": state.pushed_branch,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        state_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _pr_head_sha(item: dict[str, Any]) -> str:
        head = item.get("head")
        if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise RuntimeError("PR checkout missing head.sha")
        return head["sha"]
