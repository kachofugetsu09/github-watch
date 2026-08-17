from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
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
    observations = asyncio.run(_exercise())
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


async def _exercise() -> dict[str, object]:
    """Load pure-v3 catalogs, publish a candidate, and dispose the activity owner."""

    from agent.plugins.composable import ComposablePlugin
    from agent.plugins.generation_activity_host import ActivityHost
    from agent.plugins.generation_job_host import BackgroundJobActivityAdapter
    from agent.plugins.manager import PluginManager
    from agent.tools.registry import ToolRegistry
    from bus.event_bus import EventBus

    with tempfile.TemporaryDirectory(prefix="github-watch-v3-gate-") as raw:
        temp_root = Path(raw)
        plugin_dir = temp_root / "plugins" / "github-watch"
        _copy_plugin(plugin_dir)
        workspace = temp_root / "workspace"
        pem_path = temp_root / "github-app.pem"
        pem_path.write_text("fake", encoding="utf-8")
        _write_config(workspace, pem_path)
        event_bus = EventBus()
        manager = PluginManager(
            plugin_dirs=[temp_root / "plugins"],
            event_bus=event_bus,
            tool_registry=ToolRegistry(),
            workspace=workspace,
            installed_cache_root=temp_root / "plugin-home/cache",
        )
        jobs = BackgroundJobActivityAdapter(
            event_bus,
            manager.snapshot_store,
            workspace=str(workspace),
            interval_poll_seconds=60,
        )
        manager.bind_activity_host(ActivityHost((jobs,)))
        created: list[tuple[str, dict[str, object]]] = []
        sessions: dict[str, dict[str, object]] = {}
        runtime_data_paths: list[Path] = []
        fake_watch_count = 0

        class FakeConversationRuntime:
            async def start_turn(self, _request: object, **_kwargs: object) -> object:
                return SimpleNamespace(
                    id="turn-1",
                    result=lambda: asyncio.sleep(0),
                )

        async def create_session(
            *,
            key: str,
            metadata: dict[str, object],
        ) -> None:
            created.append((key, dict(metadata)))
            sessions[key] = dict(metadata)

        async def read_session(key: str) -> dict[str, object] | None:
            metadata = sessions.get(key)
            if metadata is None:
                return None
            return {"key": key, "metadata": dict(metadata)}

        jobs.bind_conversation_runtime(
            FakeConversationRuntime(),
            programmatic_session_creator=create_session,
            programmatic_session_reader=read_session,
        )

        try:
            await manager.load_all()
            generation = manager.generation("github-watch")
            snapshot = manager.current_snapshot
            if (
                generation is None
                or snapshot is None
                or snapshot.composition_root is None
                or not isinstance(generation.instance, ComposablePlugin)
            ):
                raise RuntimeError("GitHub Watch v3 generation did not load")
            module = generation.instance.module

            class FakeLedger:
                def __init__(self, path: Path) -> None:
                    runtime_data_paths.append(path)

                def integrity_check(self) -> None:
                    pass

                def recover_interrupted(self) -> dict[str, int]:
                    return {}

            class FakeCheckouts:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

                def sweep(self) -> int:
                    return 0

            class FakeWatch:
                def __init__(self, **_kwargs: object) -> None:
                    nonlocal fake_watch_count
                    fake_watch_count += 1

                async def poll(self, _repositories: list[str], turns: Any) -> None:
                    session_id = await turns.create_session(metadata={"source": "gate"})
                    _ = await turns.submit(session_id, "inspect gate event")

            async def run_formal_job(
                bucket: str,
                *,
                expected_snapshot_id: str,
                expected_generation_id: str,
                expected_module: object,
            ) -> None:
                binding = jobs.active_binding
                if binding is None or len(binding.jobs) != 1:
                    raise RuntimeError("GitHub Watch formal job binding is unavailable")
                if binding.snapshot_id != expected_snapshot_id:
                    raise RuntimeError("GitHub Watch job binding is not exact snapshot")
                job = next(iter(binding.jobs.values()))
                if job.binding.generation_id != expected_generation_id:
                    raise RuntimeError("GitHub Watch job binding is not exact generation")
                expected_handler = getattr(
                    expected_module,
                    job.binding.handler_export,
                    None,
                )
                if job.handler is not expected_handler:
                    raise RuntimeError("GitHub Watch job handler is not exact module export")
                await jobs.enqueue_interval(
                    binding,
                    job.key,
                    interval_bucket=bucket,
                )
                for _ in range(200):
                    if (
                        not binding.pending_admission
                        and not binding.queued
                        and not binding.running
                    ):
                        return
                    await asyncio.sleep(0.01)
                raise RuntimeError("GitHub Watch formal job did not settle")

            setattr(module, "EventLedger", FakeLedger)
            setattr(module, "GitHubClient", lambda **_kwargs: object())
            setattr(module, "CheckoutManager", FakeCheckouts)
            setattr(module, "GitHubOperations", lambda *_args: object())
            setattr(module, "GitHubWatch", FakeWatch)

            # 3. Run the committed handler through the real ActivityHost and Turn port.
            await run_formal_job(
                "gate-initial",
                expected_snapshot_id=snapshot.snapshot_id,
                expected_generation_id=generation.generation_id,
                expected_module=module,
            )
            if len(created) != 1:
                raise RuntimeError("programmatic Session was not created exactly once")
            if fake_watch_count != 1 or len(runtime_data_paths) != 1:
                raise RuntimeError("initial formal runtime ownership mismatch")

            # 4. Publish a candidate and prove its Root emits no external effect.
            plugin_path = plugin_dir / "plugin.py"
            source = plugin_path.read_text(encoding="utf-8")
            plugin_path.write_text(
                source.replace('_VERSION = "3.0.0"', '_VERSION = "3.0.1"'),
                encoding="utf-8",
            )
            candidate = await manager.prepare_candidate("github-watch")
            if candidate is None or candidate.runtime_snapshot is None:
                raise RuntimeError("GitHub Watch candidate was not prepared")
            candidate_root = candidate.runtime_snapshot.composition_root
            if candidate_root is None or candidate_root.receipt().external_effects:
                raise RuntimeError("candidate emitted an external effect")
            if (
                fake_watch_count != 1
                or len(runtime_data_paths) != 1
                or len(created) != 1
            ):
                raise RuntimeError("candidate executed formal job or opened formal data")
            publication = await manager.publish_prepared("github-watch")
            if publication["publication_state"] != "committed":
                raise RuntimeError(f"candidate did not commit: {publication}")

            # 5. Prove the rebuilt formal module owns a fresh exact job binding.
            promoted = manager.generation("github-watch")
            if promoted is None or not isinstance(promoted.instance, ComposablePlugin):
                raise RuntimeError("published GitHub Watch generation is unavailable")
            promoted_module = promoted.instance.module
            setattr(promoted_module, "EventLedger", FakeLedger)
            setattr(promoted_module, "GitHubClient", lambda **_kwargs: object())
            setattr(promoted_module, "CheckoutManager", FakeCheckouts)
            setattr(promoted_module, "GitHubOperations", lambda *_args: object())
            setattr(promoted_module, "GitHubWatch", FakeWatch)
            current_after_promotion = manager.current_snapshot
            if current_after_promotion is None:
                raise RuntimeError("promoted snapshot is unavailable")
            await run_formal_job(
                "gate-promoted",
                expected_snapshot_id=current_after_promotion.snapshot_id,
                expected_generation_id=promoted.generation_id,
                expected_module=promoted_module,
            )
            if len(created) != 2 or fake_watch_count != 2:
                raise RuntimeError("promoted formal job did not admit a second Turn")

            current = manager.current_snapshot
            if current is None or current.tool_registry is None:
                raise RuntimeError("published snapshot has no ToolRegistry")
            tool_names = current.tool_registry.get_registered_order()
            expected_tools = [
                "github_watch_create_pr",
                "github_watch_post_comment",
                "github_watch_push_branch",
                "github_watch_runtime_info",
                "github_watch_submit_review",
            ]
            if tool_names != expected_tools:
                raise RuntimeError(f"Tool catalog mismatch: {tool_names}")
            job_catalog = current.background_job_catalog
            if job_catalog is None or [descriptor.name for descriptor in job_catalog.descriptors] != ["poll"]:
                raise RuntimeError("background job catalog mismatch")
            await manager.snapshot_store.retry_drains()
            return {
                "services": list(current.composition_topology.services)
                if current.composition_topology is not None
                else [],
                "listeners": list(current.composition_topology.listeners)
                if current.composition_topology is not None
                else [],
                "background_jobs": [descriptor.name for descriptor in job_catalog.descriptors],
                "tools": tool_names,
                "candidate_external_effects": list(candidate_root.receipt().external_effects),
                "runtime_data_paths": [str(path.relative_to(temp_root)) for path in runtime_data_paths],
                "fake_watch_count": fake_watch_count,
                "programmatic_sessions": created,
            }
        finally:
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
