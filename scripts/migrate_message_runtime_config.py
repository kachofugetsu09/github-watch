#!/usr/bin/env python3
"""Remove the retired Turn timeout from one private GitHub Watch config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

_RETIRED_KEY = "turn_timeout_seconds"
_ASSIGNMENT = re.compile(r"(?m)^turn_timeout_seconds[ \t]*=[^\n]*(?:\n|$)")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_exclusive(path: Path, value: bytes) -> None:
    """Create one private file without a world-readable pre-chmod window."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate(config_path: Path) -> dict[str, object]:
    """Back up and atomically rewrite one config, or report it already current."""

    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError(f"config must be a regular file: {config_path}")
    original = config_path.read_bytes()
    config = tomllib.loads(original.decode("utf-8"))
    if _RETIRED_KEY not in config:
        return {
            "schema_version": 1,
            "status": "already_current",
            "config": str(config_path),
            "sha256": _digest(original),
        }
    old_value = config[_RETIRED_KEY]
    if not isinstance(old_value, int) or isinstance(old_value, bool):
        raise ValueError(f"{_RETIRED_KEY} must be an integer before migration")
    text = original.decode("utf-8")
    matches = tuple(_ASSIGNMENT.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"cannot locate one top-level {_RETIRED_KEY} assignment")

    backup = config_path.with_name(config_path.name + ".before-message-runtime")
    if backup.exists() or backup.is_symlink():
        raise FileExistsError(f"recovery backup already exists: {backup}")
    updated = (text[: matches[0].start()] + text[matches[0].end() :]).encode("utf-8")
    parsed = tomllib.loads(updated.decode("utf-8"))
    expected = dict(config)
    del expected[_RETIRED_KEY]
    if parsed != expected:
        raise AssertionError(
            "config migration changed fields other than the retired timeout"
        )

    _write_exclusive(backup, original)
    shutil.copystat(config_path, backup, follow_symlinks=False)
    _sync_directory(config_path.parent)
    temporary = config_path.with_name(config_path.name + ".message-runtime.tmp")
    temporary_created = False
    try:
        _write_exclusive(temporary, updated)
        temporary_created = True
        shutil.copystat(config_path, temporary, follow_symlinks=False)
        os.replace(temporary, config_path)
        temporary_created = False
        _sync_directory(config_path.parent)
    except BaseException:
        if temporary_created:
            temporary.unlink()
        raise
    return {
        "schema_version": 1,
        "status": "migrated",
        "config": str(config_path),
        "removed": _RETIRED_KEY,
        "before_sha256": _digest(original),
        "after_sha256": _digest(updated),
        "recovery_backup": str(backup),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.config), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
