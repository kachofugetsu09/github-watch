from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from github_watch_test_package.checkout import CheckoutManager
from github_watch_test_package.ledger import EventState


class FakeClient:
    def installation_token(self) -> str:
        return "installation-token"


def event(*, kind: str = "pr") -> EventState:
    return EventState(
        event_key=f"owner/repo:{kind}:7:opened",
        operation_id="a" * 32,
        repo="owner/repo",
        kind=kind,
        number=7,
        trigger_kind="opened",
        trigger_id="7",
        status="claimed",
        thread_id=None,
        turn_id=None,
    )


def test_prepare_pr_binds_exact_head_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    manager = CheckoutManager(
        FakeClient(),
        root=tmp_path / "checkouts",
        mirror_root=tmp_path / "mirror",
    )  # type: ignore[arg-type]
    commands: list[list[str]] = []
    authenticated_commands: list[list[str]] = []

    def authenticated(_operation_dir: Path, command: list[str]) -> None:
        authenticated_commands.append(command)
        if "clone" in command:
            mirror = Path(command[-1])
            (mirror / "objects").mkdir(parents=True)
            (mirror / "HEAD").write_text("ref: refs/heads/main\n")

    def run(command: list[str], **_kwargs: object) -> None:
        commands.append(command)
        if "worktree" in command and "add" in command:
            (Path(command[-2]) / ".git").mkdir(parents=True)

    monkeypatch.setattr(manager, "_run_authenticated", authenticated)
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(
        manager,
        "_git_output",
        lambda _repository, *arguments: "deadbeef" if arguments[-1] == "HEAD" else "",
    )

    state = manager.prepare(event(), {"head": {"sha": "deadbeef"}})

    assert state.path == tmp_path / "checkouts" / ("a" * 32) / "repository"
    assert state.base_sha == "deadbeef"
    assert (state.path.parent / "state.json").stat().st_mode & 0o777 == 0o600
    assert any(
        "worktree" in command and "refs/pull/7/head" in command
        for command in commands
    )
    assert any(
        "clone" in command and "--mirror" in command
        for command in authenticated_commands
    )
    assert manager.get("a" * 32) == state
    assert manager.cleanup("a" * 32)
    assert not state.path.parent.exists()


def test_mirror_is_cloned_once_and_reused(tmp_path: Path, monkeypatch) -> None:
    manager = CheckoutManager(
        FakeClient(),
        root=tmp_path / "checkouts",
        mirror_root=tmp_path / "mirror",
    )  # type: ignore[arg-type]
    authenticated_commands: list[list[str]] = []

    def authenticated(_operation_dir: Path, command: list[str]) -> None:
        authenticated_commands.append(command)
        if "clone" in command:
            mirror = Path(command[-1])
            (mirror / "objects").mkdir(parents=True)
            (mirror / "HEAD").write_text("ref: refs/heads/main\n")

    monkeypatch.setattr(manager, "_run_authenticated", authenticated)
    monkeypatch.setattr(manager, "_run", lambda command, **_kwargs: None)
    monkeypatch.setattr(
        manager,
        "_git_output",
        lambda _repository, *arguments: "deadbeef" if arguments[-1] == "HEAD" else "",
    )

    first = event()
    manager.prepare(first, {"head": {"sha": "deadbeef"}})
    manager.cleanup("a" * 32)

    second = EventState(
        event_key="owner/repo:pr:7:opened",
        operation_id="b" * 32,
        repo="owner/repo",
        kind="pr",
        number=7,
        trigger_kind="opened",
        trigger_id="7",
        status="claimed",
        thread_id=None,
        turn_id=None,
    )
    manager.prepare(second, {"head": {"sha": "deadbeef"}})

    clones = [command for command in authenticated_commands if "clone" in command]
    assert len(clones) == 1
    mirror = Path(clones[0][-1])
    assert (mirror / "HEAD").is_file()
    fetches = [command for command in authenticated_commands if "fetch" in command]
    assert len(fetches) == 2
    assert any("+refs/pull/*/head:refs/pull/*/head" in command for command in fetches)


def test_cleanup_removes_attached_worktree(tmp_path: Path, monkeypatch) -> None:
    manager = CheckoutManager(
        FakeClient(),
        root=tmp_path / "checkouts",
        mirror_root=tmp_path / "mirror",
    )  # type: ignore[arg-type]
    operation_dir = tmp_path / "checkouts" / ("a" * 32)
    repository_dir = operation_dir / "repository"
    mirror = tmp_path / "mirror" / "owner" / "repo.git"
    (mirror / "objects").mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main\n")
    (mirror / ".git" / "worktrees" / ("a" * 32)).mkdir(parents=True)
    repository_dir.mkdir(parents=True)
    (repository_dir / ".git").write_text(
        f"gitdir: {mirror}/.git/worktrees/{'a' * 32}\n", encoding="utf-8"
    )

    commands: list[list[str]] = []
    monkeypatch.setattr(manager, "_run", lambda command, **_kwargs: commands.append(command))

    assert manager.cleanup("a" * 32)
    assert not operation_dir.exists()
    assert any(
        "worktree" in command and "remove" in command for command in commands
    )


def test_sweep_removes_only_expired_operation_directories(tmp_path: Path) -> None:
    root = tmp_path / "checkouts"
    manager = CheckoutManager(  # type: ignore[arg-type]
        FakeClient(),
        root=root,
        ttl_seconds=300,
    )
    expired = root / ("a" * 32)
    current = root / ("b" * 32)
    unrelated = root / "notes"
    for path in (expired, current, unrelated):
        path.mkdir(parents=True)
    old = time.time() - 600
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))

    assert manager.sweep() == 1
    assert not expired.exists()
    assert current.exists()
    assert unrelated.exists()


def test_cleanup_rejects_invalid_operation_identity(tmp_path: Path) -> None:
    manager = CheckoutManager(FakeClient(), root=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid operation_id"):
        manager.cleanup("../escape")
