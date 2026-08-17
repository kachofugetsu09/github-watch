from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_plugin_module():
    agent = ModuleType("agent")
    composition = ModuleType("agent.plugin_composition")
    turn_events = ModuleType("agent.turn_events")
    after_turn = ModuleType("agent.turn_events.after_turn")
    bus = ModuleType("bus")
    events_lifecycle = ModuleType("bus.events_lifecycle")

    class Context:
        pass

    class ProgrammaticTurnPreAdmissionError(RuntimeError):
        pass

    class ProgrammaticTurnUncertainError(RuntimeError):
        pass

    @dataclass(frozen=True)
    class IntervalTrigger:
        seconds: int

    @dataclass(frozen=True)
    class BackgroundJobDefinition:
        name: str
        triggers: tuple[object, ...]
        handler_export: str
        programmatic_turns: bool = False

    @dataclass(frozen=True)
    class PluginToolDefinition:
        name: str
        description: str
        parameters: dict[str, object]
        handler_export: str
        risk: str
        always_on: bool

    class TurnCommitted:
        def __init__(self, session_key: str, turn_id: str) -> None:
            self.session_key = session_key
            self.turn_id = turn_id

    composition.BACKGROUND_JOBS = object()  # type: ignore[attr-defined]
    composition.TOOL_CATALOG = object()  # type: ignore[attr-defined]
    composition.BackgroundJobDefinition = BackgroundJobDefinition  # type: ignore[attr-defined]
    composition.Context = Context  # type: ignore[attr-defined]
    composition.IntervalTrigger = IntervalTrigger  # type: ignore[attr-defined]
    composition.PluginToolDefinition = PluginToolDefinition  # type: ignore[attr-defined]
    composition.ProgrammaticTurnPreAdmissionError = ProgrammaticTurnPreAdmissionError  # type: ignore[attr-defined]
    composition.ProgrammaticTurnUncertainError = ProgrammaticTurnUncertainError  # type: ignore[attr-defined]
    after_turn.AFTER_TURN_COMMITTED = object()  # type: ignore[attr-defined]
    events_lifecycle.TurnCommitted = TurnCommitted  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "agent": agent,
            "agent.plugin_composition": composition,
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
        self.registrations: list[object] = []

    async def register(self, _ctx: object, definition: object) -> None:
        self.registrations.append(definition)


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
        self.listeners: list[tuple[object, object]] = []
        self._services = {
            module.BACKGROUND_JOBS: self.jobs,
            module.TOOL_CATALOG: self.catalog,
        }

    def require(self, key: object) -> object:
        return self._services[key]

    async def on(self, key: object, listener: object) -> None:
        self.listeners.append((key, listener))


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
    assert len(candidate.jobs.registrations) == 1
    job = candidate.jobs.registrations[0]
    assert job.programmatic_turns is True
    assert job.handler_export == "run_github_watch_poll"
    assert len(candidate.catalog.registrations) == 5
    assert [definition.name for definition in candidate.catalog.registrations] == [
        "github_watch_runtime_info",
        "github_watch_post_comment",
        "github_watch_submit_review",
        "github_watch_push_branch",
        "github_watch_create_pr",
    ]
    assert all(definition.always_on for definition in candidate.catalog.registrations)
    assert candidate.listeners[0][0] is plugin_module.AFTER_TURN_COMMITTED
    assert inspect.iscoroutinefunction(candidate.listeners[0][1]) is False

    async def unexpected_client(**_kwargs: object) -> object:
        raise AssertionError("candidate must not construct a GitHub client")

    monkeypatch.setattr(plugin_module, "GitHubClient", unexpected_client)
    with pytest.raises(RuntimeError, match="candidate GitHub Watch job"):
        asyncio.run(plugin_module.run_github_watch_poll(SimpleNamespace(turns=None)))
    assert candidate.data_root.exists() is False

    asyncio.run(plugin_module.apply(formal, config))
    formal_job = formal.jobs.registrations[0]
    assert formal_job.programmatic_turns is True


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

    turns = _FakeTurns()
    asyncio.run(plugin_module.run_github_watch_poll(SimpleNamespace(turns=turns)))

    assert ledger_paths == [context.data_root / "events.sqlite3"]
    assert poll_calls == [(["owner/repo"], turns)]


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
    ):
        assert legacy_name not in source
        assert legacy_name not in coordinator
