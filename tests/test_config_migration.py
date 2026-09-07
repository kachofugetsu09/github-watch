from __future__ import annotations

from pathlib import Path
import stat
import tomllib

import pytest

from scripts.migrate_message_runtime_config import migrate


def test_removes_retired_turn_timeout_with_exact_backup(tmp_path: Path) -> None:
    config = tmp_path / "config.local.toml"
    original = b'app_id = 1\nturn_timeout_seconds = 900\nrepositories = ["a/b"]\n'
    config.write_bytes(original)
    config.chmod(0o600)

    receipt = migrate(config)

    assert receipt["status"] == "migrated"
    assert (
        config.with_name("config.local.toml.before-message-runtime").read_bytes()
        == original
    )
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE(
            config.with_name("config.local.toml.before-message-runtime").stat().st_mode
        )
        == 0o600
    )
    assert tomllib.loads(config.read_text()) == {"app_id": 1, "repositories": ["a/b"]}
    assert migrate(config)["status"] == "already_current"


def test_refuses_ambiguous_or_unsafe_input(tmp_path: Path) -> None:
    config = tmp_path / "config.local.toml"
    config.write_text('turn_timeout_seconds = "old"\n')
    with pytest.raises(ValueError, match="must be an integer"):
        migrate(config)

    target = tmp_path / "target.toml"
    target.write_text("app_id = 1\n")
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        migrate(link)


def test_does_not_delete_an_unowned_temporary_file(tmp_path: Path) -> None:
    config = tmp_path / "config.local.toml"
    config.write_text("turn_timeout_seconds = 900\n")
    temporary = tmp_path / "config.local.toml.message-runtime.tmp"
    temporary.write_text("owned by another run\n")

    with pytest.raises(FileExistsError):
        migrate(config)

    assert temporary.read_text() == "owned by another run\n"
