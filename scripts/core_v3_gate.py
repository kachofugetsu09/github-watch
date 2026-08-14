from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_FILES = (
    "__init__.py",
    "checkout.py",
    "context_bundle.py",
    "github_client.py",
    "github_watch.py",
    "ledger.py",
    "operations.py",
    "plugin.py",
)


def main() -> None:
    """Run the real v3 namespace path against one exact Core checkout."""

    # 1. Bind both repositories before importing Core code.
    parser = argparse.ArgumentParser(description="GitHub Watch v3 Core gate")
    _ = parser.add_argument("--core", required=True, type=Path)
    _ = parser.add_argument("--expected-core", required=True)
    _ = parser.add_argument("--require-clean-plugin", action="store_true")
    args = parser.parse_args()
    core = cast(Path, args.core).resolve(strict=True)
    expected_core = cast(str, args.expected_core)
    if _git(core, "rev-parse", "HEAD") != expected_core:
        raise RuntimeError("Core HEAD does not match --expected-core")
    plugin_dirty = _git(PLUGIN_ROOT, "status", "--porcelain").splitlines()
    if cast(bool, args.require_clean_plugin) and plugin_dirty:
        raise RuntimeError(f"plugin worktree is dirty: {plugin_dirty}")
    sys.path.insert(0, str(core))

    # 2. Exercise only temporary plugin-data and fake external boundaries.
    observations = asyncio.run(_exercise(core))
    report = {
        "status": "passed",
        "core_head": expected_core,
        "core_tree": _git(core, "rev-parse", "HEAD^{tree}"),
        "plugin_head": _git(PLUGIN_ROOT, "rev-parse", "HEAD"),
        "plugin_tree": _git(PLUGIN_ROOT, "rev-parse", "HEAD^{tree}"),
        "plugin_dirty": plugin_dirty,
        "plugin_source_digest": _source_digest(),
        "observations": observations,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


async def _exercise(core: Path) -> dict[str, object]:
    """Load, reload, dispatch, clean up, and fully dispose one real plugin."""

    from agent.plugins.composable import ComposablePlugin
    from agent.plugins.manager import PluginManager
    from agent.plugins.snapshot import bind_runtime_snapshot, reset_runtime_snapshot
    from agent.tools.registry import ToolRegistry
    from agent.turn_events.after_turn import AFTER_TURN_COMMITTED
    from bus.event_bus import EventBus
    from bus.events_lifecycle import TurnCommitted

    del core
    with tempfile.TemporaryDirectory(prefix="github-watch-v3-gate-") as raw:
        temp_root = Path(raw)
        plugin_dir = temp_root / "plugins" / "github-watch"
        _copy_plugin(plugin_dir)
        workspace = temp_root / "workspace"
        pem_path = temp_root / "github-app.pem"
        pem_path.write_text("fake", encoding="utf-8")
        _write_config(workspace, pem_path)

        manager = PluginManager(
            plugin_dirs=[temp_root / "plugins"],
            event_bus=EventBus(),
            tool_registry=ToolRegistry(),
            workspace=workspace,
            installed_cache_root=temp_root / "plugin-home/cache",
        )
        created: list[tuple[str, dict[str, object]]] = []
        submitted: list[tuple[str, str, str]] = []

        async def create_session(
            plugin_id: str,
            metadata: Mapping[str, object],
        ) -> str:
            created.append((plugin_id, dict(metadata)))
            return "session-1"

        async def submit(
            plugin_id: str,
            session_id: str,
            content: str,
            metadata: Mapping[str, object],
        ) -> str:
            del metadata
            submitted.append((plugin_id, session_id, content))
            return "turn-1"

        manager.bind_agent_input(create_session=create_session, submit=submit)
        callback_release = asyncio.Event()
        callback_started = asyncio.Event()
        callback_finished = asyncio.Event()
        lease_release = asyncio.Event()
        cleanup_operations: list[str] = []
        runtime_data_paths: list[Path] = []
        fake_watch_count = 0

        try:
            await manager.load_all()
            generation = manager.generation("github-watch")
            old_snapshot = manager.current_snapshot
            if (
                generation is None
                or old_snapshot is None
                or old_snapshot.composition_root is None
                or not isinstance(generation.instance, ComposablePlugin)
            ):
                raise RuntimeError("GitHub Watch v3 generation did not load")
            old_root = old_snapshot.composition_root
            module = generation.instance.module

            class FakeLedger:
                def __init__(self, path: Path) -> None:
                    runtime_data_paths.append(path)

                def integrity_check(self) -> None:
                    pass

                def recover_interrupted(self) -> dict[str, int]:
                    return {}

                def get_event_by_turn(self, turn_id: str) -> object | None:
                    if turn_id != "turn-1":
                        return None
                    return SimpleNamespace(
                        operation_id="a" * 32,
                        event_key="owner/repo:issue:1:opened",
                        thread_id="session-1",
                    )

            class FakeCheckouts:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def sweep(self) -> int:
                    return 0

                def cleanup(self, operation_id: str) -> bool:
                    cleanup_operations.append(operation_id)
                    return True

            class FakeWatch:
                def __init__(self, **kwargs: object) -> None:
                    nonlocal fake_watch_count
                    fake_watch_count += 1
                    self._agent_input = cast(Any, kwargs["agent_input"])

                async def poll(self, repositories: list[str]) -> None:
                    if repositories != ["owner/repo"]:
                        raise AssertionError(repositories)
                    callback_started.set()
                    await callback_release.wait()
                    session_id = await self._agent_input.create_session(
                        {"source": "github-watch"}
                    )
                    _ = await self._agent_input.submit(session_id, "gate prompt")

            setattr(module, "EventLedger", FakeLedger)
            setattr(module, "GitHubClient", lambda **_kwargs: object())
            setattr(module, "CheckoutManager", FakeCheckouts)
            setattr(module, "GitHubOperations", lambda *_args: object())
            setattr(module, "GitHubWatch", FakeWatch)

            timer = old_snapshot.timers["github-watch:poll"]

            async def invoke_old_timer() -> None:
                lease = manager.snapshot_store.lease()
                if not lease.stable_at_claim:
                    raise RuntimeError("old Timer did not claim stable")
                token = bind_runtime_snapshot(lease)
                try:
                    await timer.invoke()
                    callback_finished.set()
                    await lease_release.wait()
                finally:
                    reset_runtime_snapshot(token)
                    await lease.release()

            callback_task = asyncio.create_task(invoke_old_timer())
            await asyncio.wait_for(callback_started.wait(), timeout=2)

            # 3. Publish a new generation while the old stable callback is leased.
            plugin_path = plugin_dir / "plugin.py"
            source = plugin_path.read_text(encoding="utf-8")
            plugin_path.write_text(
                source.replace('_VERSION = "2.0.0"', '_VERSION = "2.0.1"'),
                encoding="utf-8",
            )
            candidate = await manager.prepare_candidate("github-watch")
            if candidate is None or candidate.runtime_snapshot is None:
                raise RuntimeError("GitHub Watch candidate was not prepared")
            candidate_root = candidate.runtime_snapshot.composition_root
            if candidate_root is None or candidate_root.receipt().external_effects:
                raise RuntimeError("candidate emitted an external effect")
            publication = await manager.publish_prepared("github-watch")
            if publication["publication_state"] != "committed":
                raise RuntimeError(f"candidate did not commit: {publication}")
            if old_snapshot.state != "retired":
                raise RuntimeError("old snapshot did not retire")

            # 4. The old task completes Agent Input, then its async listener cleans up.
            callback_release.set()
            await asyncio.wait_for(callback_finished.wait(), timeout=2)
            committed = TurnCommitted(
                session_key="session-1",
                channel="test",
                chat_id="chat",
                input_message="gate prompt",
                persisted_user_message="gate prompt",
                assistant_response="done",
                tools_used=[],
                turn_id="turn-1",
            )
            result = await old_root.context.serial(AFTER_TURN_COMMITTED, committed)
            if result is not None:
                raise RuntimeError("TurnCommitted listener returned Bail")
            lease_release.set()
            await asyncio.wait_for(callback_task, timeout=2)
            await manager.snapshot_store.retry_drains()

            current = manager.current_snapshot
            if current is None or current.tool_registry is None:
                raise RuntimeError("published snapshot has no ToolRegistry")
            tool_names = current.tool_registry.get_registered_order()
            expected_tools = [
                "github_watch_runtime_info",
                "github_watch_post_comment",
                "github_watch_submit_review",
                "github_watch_push_branch",
                "github_watch_create_pr",
            ]
            if tool_names != expected_tools:
                raise RuntimeError(f"Tool catalog mismatch: {tool_names}")
            if old_root.receipt().effects:
                raise RuntimeError("retired Root retained effects")
            return {
                "services": list(current.composition_topology.services)
                if current.composition_topology is not None
                else [],
                "listeners": list(current.composition_topology.listeners)
                if current.composition_topology is not None
                else [],
                "timers": sorted(current.timers),
                "tools": tool_names,
                "agent_input_created": created,
                "agent_input_submitted": [
                    (plugin_id, session_id, content)
                    for plugin_id, session_id, content in submitted
                ],
                "runtime_data_paths": [str(path.relative_to(temp_root)) for path in runtime_data_paths],
                "fake_watch_count": fake_watch_count,
                "cleanup_operations": cleanup_operations,
                "old_root_effects_after_drain": list(old_root.receipt().effects),
            }
        finally:
            callback_release.set()
            lease_release.set()
            await manager.terminate_all()


def _copy_plugin(target: Path) -> None:
    target.mkdir(parents=True)
    for name in _SOURCE_FILES:
        shutil.copy2(PLUGIN_ROOT / name, target / name)


def _write_config(workspace: Path, pem_path: Path) -> None:
    data_dir = workspace / "plugin-data/github-watch-builtin"
    data_dir.mkdir(parents=True)
    (data_dir / "config.local.toml").write_text(
        "\n".join(
            (
                "app_id = 1",
                "installation_id = 2",
                f"pem_path = {json.dumps(str(pem_path))}",
                'repositories = ["owner/repo"]',
                "poll_seconds = 15",
                "checkout_ttl_seconds = 300",
                "",
            )
        ),
        encoding="utf-8",
    )


def _source_digest() -> str:
    digest = hashlib.sha256()
    for name in _SOURCE_FILES:
        digest.update(name.encode())
        digest.update((PLUGIN_ROOT / name).read_bytes())
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    main()
