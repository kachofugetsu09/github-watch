from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import ModuleType, SimpleNamespace
from typing import Generic, TypeVar

import pytest


def _read_static_identity(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Read manifest and literal plugin identity without importing the entrypoint."""

    # 1. Parse only the static manifest and declared source path.
    manifest = tomllib.loads(
        (root / "akashic.plugin.toml").read_text(encoding="utf-8")
    )
    entrypoint = root / str(manifest["entrypoint"])
    tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))

    # 2. Resolve the identity assignments from the entrypoint AST.
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"name", "version", "api_version"}:
                continue
            if isinstance(node.value, ast.Constant):
                values[target.id] = node.value.value
            elif isinstance(node.value, ast.Name):
                values[target.id] = values[node.value.id]
    return manifest, {
        "name": values["name"],
        "version": values["version"],
        "api_version": values["api_version"],
    }


def test_static_manifest_matches_import_free_entrypoint_identity() -> None:
    root = Path(__file__).parents[1]
    manifest, identity = _read_static_identity(root)

    assert manifest == {
        "schema_version": 1,
        "name": "github-watch",
        "version": "3.0.0",
        "api_version": 3,
        "entrypoint": "plugin.py",
    }
    assert identity == {
        "name": manifest["name"],
        "version": manifest["version"],
        "api_version": manifest["api_version"],
    }


def _load_plugin_module():
    agent = ModuleType("agent")
    composition = ModuleType("agent.plugin_composition")
    contracts = ModuleType("agent.plugin_contracts")
    tool_catalog = ModuleType("agent.tool_catalog")
    turn_events = ModuleType("agent.turn_events")
    after_turn = ModuleType("agent.turn_events.after_turn")
    bus = ModuleType("bus")
    events_lifecycle = ModuleType("bus.events_lifecycle")

    class Context:
        pass

    T = TypeVar("T")

    @dataclass(frozen=True)
    class ServiceKey(Generic[T]):
        name: str

    @dataclass(frozen=True)
    class ContentPart:
        kind: str
        value: object

    def validate_tool_parameters(arguments: object, *, schema: dict[str, object]) -> list[str]:
        if not isinstance(arguments, dict):
            return ["参数必须是对象"]
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return ["schema 无效"]
        errors = [f"缺少参数: {key}" for key in required if key not in arguments]
        errors.extend(
            f"未知参数: {key}" for key in arguments if key not in properties
        )
        return errors

    class TurnCommitted:
        def __init__(self, session_key: str, turn_id: str) -> None:
            self.session_key = session_key
            self.turn_id = turn_id

    composition.ServiceKey = ServiceKey  # type: ignore[attr-defined]
    composition.Context = Context  # type: ignore[attr-defined]
    composition.RUNTIME_STARTED = object()  # type: ignore[attr-defined]
    composition.RUNTIME_STOPPING = object()  # type: ignore[attr-defined]
    composition.TIMERS = ServiceKey("core.timers")  # type: ignore[attr-defined]
    composition.TimerStatus = SimpleNamespace(FIRED="fired")  # type: ignore[attr-defined]
    contracts.ContentPart = ContentPart  # type: ignore[attr-defined]
    tool_catalog.validate_tool_parameters = validate_tool_parameters  # type: ignore[attr-defined]
    after_turn.AFTER_TURN_COMMITTED = object()  # type: ignore[attr-defined]
    events_lifecycle.TurnCommitted = TurnCommitted  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "agent": agent,
            "agent.plugin_composition": composition,
            "agent.plugin_contracts": contracts,
            "agent.tool_catalog": tool_catalog,
            "agent.turn_events": turn_events,
            "agent.turn_events.after_turn": after_turn,
            "bus": bus,
            "bus.events_lifecycle": events_lifecycle,
        }
    )
    _ = sys.modules.pop("github_watch_test_package.plugin", None)
    return importlib.import_module("github_watch_test_package.plugin")


class _FakeJobs:
    def __init__(self) -> None:
        self.registrations: list[object] = []

    async def register(self, _ctx: object, definition: object) -> None:
        self.registrations.append(definition)


class _FakeCatalog:
    def __init__(self) -> None:
        self.groups: list[object] = []
        self.registrations: list[object] = []
        self.authorizations: list[object] = []

    async def declare_group(self, _ctx: object, **kwargs: object) -> None:
        self.groups.append(SimpleNamespace(**kwargs))

    async def register(self, _ctx: object, **kwargs: object) -> object:
        reference = SimpleNamespace(name=kwargs["name"])
        self.registrations.append(SimpleNamespace(reference=reference, **kwargs))
        return reference

    async def register_authorize(self, _ctx: object, **kwargs: object) -> None:
        self.authorizations.append(SimpleNamespace(**kwargs))


class _FakeContext:
    def __init__(self, module: ModuleType, data_dir: Path) -> None:
        self.data_root = data_dir
        self.runtime = SimpleNamespace(
            plugin_id="github-watch",
            plugin_dir=data_dir.parent,
            data_dir=data_dir,
            workspace=data_dir.parent,
        )
        self.jobs = _FakeJobs()
        self.catalog = _FakeCatalog()
        self.programmatic = SimpleNamespace()
        self.listeners: list[tuple[object, object]] = []
        self._services = {
            module.TIMERS: SimpleNamespace(),
            module.PROGRAMMATIC: self.programmatic,
            module.TOOLS: self.catalog,
        }

    def require(self, key: object) -> object:
        return self._services[key]

    async def on(self, key: object, listener: object) -> None:
        self.listeners.append((key, listener))

    async def spawn(self, _coroutine: object, *, name: str) -> object:
        raise AssertionError(f"测试上下文不应在 apply 阶段启动任务: {name}")


class _FakeTurns:
    pass


def test_v3_apply_registers_candidate_inert_descriptors_without_pem_or_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_module = _load_plugin_module()
    config = plugin_module.GitHubWatchConfig(
        app_id=1,
        installation_id=2,
        pem_path=str(tmp_path / "missing.pem"),
        repositories=["owner/repo"],
    )
    candidate = _FakeContext(plugin_module, tmp_path / "candidate")
    formal = _FakeContext(plugin_module, tmp_path / "formal")

    asyncio.run(plugin_module.apply(candidate, config))

    assert candidate.data_root.exists() is False
    assert len(candidate.catalog.registrations) == 5
    assert [registration.name for registration in candidate.catalog.registrations] == [
        "github_watch_runtime_info",
        "github_watch_post_comment",
        "github_watch_submit_review",
        "github_watch_push_branch",
        "github_watch_create_pr",
    ]
    assert candidate.catalog.groups[0].always_on is True
    assert [registration.risk for registration in candidate.catalog.registrations] == [
        "read-only",
        "external-side-effect",
        "external-side-effect",
        "external-side-effect",
        "external-side-effect",
    ]
    assert len(candidate.catalog.authorizations) == 4
    assert any(key is plugin_module.AFTER_TURN_COMMITTED for key, _ in candidate.listeners)
    assert any(key is plugin_module.RUNTIME_STARTED for key, _ in candidate.listeners)
    assert any(key is plugin_module.RUNTIME_STOPPING for key, _ in candidate.listeners)
    assert sum(inspect.iscoroutinefunction(listener) for _, listener in candidate.listeners) == 2

    async def unexpected_client(**_kwargs: object) -> object:
        raise AssertionError("candidate must not construct a GitHub client")

    monkeypatch.setattr(plugin_module, "GitHubClient", unexpected_client)
    assert candidate.data_root.exists() is False
    assert candidate.data_root.exists() is False

    asyncio.run(plugin_module.apply(formal, config))
    assert formal.data_root.exists() is False


def test_formal_job_lazily_builds_runtime_and_passes_invocation_turn_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_module = _load_plugin_module()
    config = plugin_module.GitHubWatchConfig(
        app_id=1,
        installation_id=2,
        pem_path=str(tmp_path / "missing.pem"),
        repositories=["owner/repo"],
    )
    context = _FakeContext(plugin_module, tmp_path / "formal")
    asyncio.run(plugin_module.apply(context, config))
    ledger_paths: list[Path] = []
    poll_calls: list[tuple[list[str], object]] = []

    class FakeLedger:
        def __init__(self, path: Path) -> None:
            ledger_paths.append(path)

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
        async def poll(self, repositories: list[str], turns: object) -> None:
            poll_calls.append((list(repositories), turns))

    monkeypatch.setattr(plugin_module, "EventLedger", FakeLedger)
    monkeypatch.setattr(plugin_module, "GitHubClient", lambda **_kwargs: object())
    monkeypatch.setattr(plugin_module, "CheckoutManager", FakeCheckouts)
    monkeypatch.setattr(plugin_module, "GitHubOperations", lambda *_args: object())
    monkeypatch.setattr(plugin_module, "GitHubWatch", lambda **_kwargs: FakeWatch())

    class FakeProgrammatic:
        async def call(self, method: str, params: object) -> dict[str, object]:
            if method == "programmatic/session/admit":
                return {"session_id": params.session_id}  # type: ignore[attr-defined]
            return {"session_id": params.session_id, "turn_id": "turn-1"}  # type: ignore[attr-defined]

    context._services[plugin_module.PROGRAMMATIC] = FakeProgrammatic()
    asyncio.run(plugin_module.run_github_watch_poll(context))

    assert ledger_paths == [context.data_root / "events.sqlite3"]
    assert len(poll_calls) == 1
    assert poll_calls[0][0] == ["owner/repo"]
    turns = poll_calls[0][1]
    assert isinstance(turns, plugin_module._ProgrammaticTurnPort)
    session_id = asyncio.run(
        turns.create_session(metadata={"repo": "owner/repo", "item": "issue#1"})
    )
    assert session_id.startswith("programmatic:github-watch:")
    receipt = asyncio.run(turns.submit(session_id, "inspect"))
    assert receipt.session_id == session_id
    assert receipt.turn_id == "turn-1"


def test_v3_handlers_have_exact_core_signatures() -> None:
    plugin_module = _load_plugin_module()
    job_signature = inspect.signature(plugin_module.run_github_watch_poll)
    assert tuple(job_signature.parameters) == ("context",)
    assert inspect.iscoroutinefunction(plugin_module.run_github_watch_poll)
    for name in (
        "run_github_watch_runtime_info",
        "run_github_watch_post_comment",
        "run_github_watch_submit_review",
        "run_github_watch_push_branch",
        "run_github_watch_create_pr",
    ):
        handler = getattr(plugin_module, name)
        assert tuple(inspect.signature(handler).parameters) == (
            "context",
            "arguments",
        )
        assert inspect.iscoroutinefunction(handler)


def test_tool_authorization_binds_operation_to_explicit_origin_session(
    tmp_path: Path,
) -> None:
    plugin_module = _load_plugin_module()
    event = SimpleNamespace(
        status="dispatched",
        thread_id="session-1",
        repo="owner/repo",
        kind="issue",
        number=1,
    )

    class FakeLedger:
        def get_event_by_operation(self, operation_id: str) -> object:
            assert operation_id == "a" * 32
            return event

    plugin_module._bound = plugin_module._BoundRuntime(
        ledger=FakeLedger(),
        checkouts=object(),
        operations=object(),
        watch=object(),
    )
    context = SimpleNamespace(origin_session_key="session-1")

    assert plugin_module._authorized_event(context, "a" * 32) is event

    with pytest.raises(PermissionError, match="current dispatched session"):
        plugin_module._authorized_event(
            SimpleNamespace(origin_session_key="session-other"),
            "a" * 32,
        )


def test_v3_entrypoint_has_no_legacy_runtime_categories() -> None:
    root = Path(__file__).parents[1]
    source = (root / "plugin.py").read_text(encoding="utf-8")
    coordinator = (root / "github_watch.py").read_text(encoding="utf-8")

    for legacy_name in (
        "AGENT_INPUT",
        "PLUGIN_TOOLS",
        "TIMER_SERVICE",
        "AgentInputService",
        "PluginJobSpec",
        "PluginJobContext",
        "ControlClient",
        "after_turn_modules",
        "get_current_tool_context",
        "TurnAdmissionPreconditionFailure",
        "TurnAdmissionUncertain",
        "class Tool",
        "_CompositionAgentInput",
        "GitHubWatchRuntime",
        "BACKGROUND_JOBS",
        "BackgroundJobDefinition",
        "IntervalTrigger",
        "TOOL_CATALOG",
        "PluginToolDefinition",
    ):
        assert legacy_name not in source
        assert legacy_name not in coordinator


def test_tools_v1_archive_opens_real_provider_and_preserves_five_descriptors() -> None:
    """Execute one real tools.v1 archive target through the Core ToolCatalog."""

    root = Path(__file__).parents[1]
    core_root = next(
        (
            Path(path)
            for path in sys.path
            if (Path(path) / "agent/plugin_composition").is_dir()
            and (Path(path) / "plugins/tools/plugin.py").is_file()
        ),
        None,
    )
    if core_root is None:
        pytest.fail("该验收必须在带 Core PYTHONPATH 的环境运行")
    script = dedent(
        r'''
        import asyncio
        import importlib.util
        import json
        import sys
        from contextlib import asynccontextmanager
        from pathlib import Path
        from types import ModuleType, SimpleNamespace

        from agent.plugin_composition import TIMERS
        from plugins.tools.plugin import ToolCatalog

        plugin_root = Path(sys.argv[1])
        package = ModuleType("github_watch_real")
        package.__path__ = [str(plugin_root)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location(
            "github_watch_real.plugin", plugin_root / "plugin.py",
            submodule_search_locations=[],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载 GitHub Watch entrypoint")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Context:
            def __init__(self):
                self.data_root = plugin_root / ".tmp" / "real-archive"
                self.runtime = SimpleNamespace(plugin_id="github-watch")
                self.root_instance_token = object()
                self.services = {}
                self.listeners = []

            async def effect(self, setup, *, label):
                _ = label
                cleanup = setup()
                return SimpleNamespace(close=cleanup)

            @asynccontextmanager
            async def runtime_scope(self):
                yield

            def require(self, key):
                return self.services[key]

            async def on(self, key, listener):
                self.listeners.append((key, listener))

        class RecordingCatalog(ToolCatalog):
            def __init__(self, ctx):
                super().__init__(ctx)
                self.refs = []

            async def register(self, ctx, **kwargs):
                ref = await super().register(ctx, **kwargs)
                self.refs.append(ref)
                return ref

        async def main():
            ctx = Context()
            catalog = RecordingCatalog(ctx)
            ctx.services[module.TOOLS] = catalog
            ctx.services[module.PROGRAMMATIC] = object()
            ctx.services[TIMERS] = object()
            config = module.GitHubWatchConfig(
                app_id=1, installation_id=2, pem_path="unused.pem",
                repositories=["owner/repo"],
            )
            await module.apply(ctx, config)
            module._ensure_formal_runtime = lambda: None
            expected = {
                "github_watch_runtime_info",
                "github_watch_post_comment",
                "github_watch_submit_review",
                "github_watch_push_branch",
                "github_watch_create_pr",
            }
            if {ref.name for ref in catalog.refs} != expected:
                raise AssertionError("五个 GitHub Watch 工具未完整注册")
            descriptors = {
                ref.name: ref.description for ref in catalog.refs
            }
            if descriptors["github_watch_runtime_info"]["risk"] != "read-only":
                raise AssertionError("runtime_info 风险声明丢失")
            for name in sorted(expected - {"github_watch_runtime_info"}):
                if descriptors[name]["risk"] != "external-side-effect":
                    raise AssertionError(f"{name} 风险声明丢失")
            runtime_ref = next(
                ref for ref in catalog.refs if ref.name == "github_watch_runtime_info"
            )
            metadata = {"tool": runtime_ref.description, "prepare": None}
            async with catalog.open(metadata) as target:
                prepared = await target.prepare({})
                result = await target.invoke("archive-call", prepared)
            body = result.parts[0].value
            if result.outcome != "success" or not isinstance(body, str):
                raise AssertionError("tools.v1 archive call 未返回成功结构结果")
            payload = json.loads(body)
            if payload != {
                "checkout_mode": "detached-commit",
                "mirror_recovery": "worktree-prune-before-fetch",
                "plugin": "github-watch",
                "version": "3.0.0",
            }:
                raise AssertionError(f"archive runtime_info 结果不符: {payload!r}")
            print(json.dumps({"names": sorted(expected), "outcome": result.outcome}))

        asyncio.run(main())
        ''',
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(core_root), str(root)))
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout) == {
        "names": [
            "github_watch_create_pr",
            "github_watch_post_comment",
            "github_watch_push_branch",
            "github_watch_runtime_info",
            "github_watch_submit_review",
        ],
        "outcome": "success",
    }


def test_programmatic_adapter_rejects_message_ack_without_accepted_turn() -> None:
    """A message ACK cannot be persisted as a fake Turn identity."""

    plugin_module = _load_plugin_module()

    class MessageAckOnly:
        async def call(self, method: str, params: object) -> dict[str, object]:
            if method == "programmatic/session/admit":
                return {"session_id": params.session_id}  # type: ignore[attr-defined]
            return {
                "session_id": params.session_id,  # type: ignore[attr-defined]
                "message_id": "input-1",
            }

    async def submit() -> None:
        port = plugin_module._ProgrammaticTurnPort(MessageAckOnly())
        session_id = await port.create_session(
            metadata={"repo": "owner/repo", "item": "issue#1"}
        )
        with pytest.raises(
            plugin_module.ProgrammaticTurnUncertainError,
            match="accepted turn_id",
        ):
            await port.submit(session_id, "body")

    asyncio.run(submit())
