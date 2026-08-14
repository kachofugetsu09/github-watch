from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_plugin_module():
    agent = ModuleType("agent")
    composition = ModuleType("agent.plugin_composition")
    tools = ModuleType("agent.tools")
    tools_base = ModuleType("agent.tools.base")
    turn_events = ModuleType("agent.turn_events")
    after_turn = ModuleType("agent.turn_events.after_turn")
    bus = ModuleType("bus")
    events_lifecycle = ModuleType("bus.events_lifecycle")

    class Context:
        pass

    class AgentInputService:
        pass

    class Tool:
        pass

    class TurnCommitted:
        def __init__(self, session_key: str, turn_id: str) -> None:
            self.session_key = session_key
            self.turn_id = turn_id

    composition.AGENT_INPUT = object()  # type: ignore[attr-defined]
    composition.PLUGIN_TOOLS = object()  # type: ignore[attr-defined]
    composition.TIMER_SERVICE = object()  # type: ignore[attr-defined]
    composition.AgentInputService = AgentInputService  # type: ignore[attr-defined]
    composition.Context = Context  # type: ignore[attr-defined]
    composition.ToolRisk = str  # type: ignore[attr-defined]
    tools_base.Tool = Tool  # type: ignore[attr-defined]
    tools_base.get_current_tool_context = lambda: None  # type: ignore[attr-defined]
    after_turn.AFTER_TURN_COMMITTED = object()  # type: ignore[attr-defined]
    events_lifecycle.TurnCommitted = TurnCommitted  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "agent": agent,
            "agent.plugin_composition": composition,
            "agent.tools": tools,
            "agent.tools.base": tools_base,
            "agent.turn_events": turn_events,
            "agent.turn_events.after_turn": after_turn,
            "bus": bus,
            "bus.events_lifecycle": events_lifecycle,
        }
    )
    _ = sys.modules.pop("github_watch_test_package.plugin", None)
    return importlib.import_module("github_watch_test_package.plugin")


class _FakeAgentInput:
    async def create_session(self, _ctx, *, metadata):
        del metadata
        return SimpleNamespace(id="session-1")

    async def submit(self, _ctx, session_id: str, content: str):
        del content
        return SimpleNamespace(session_id=session_id, turn_id="turn-1")


class _FakeTools:
    def __init__(self) -> None:
        self.registrations: list[tuple[object, str, bool]] = []

    async def register(
        self,
        _ctx,
        tool: object,
        *,
        risk: str,
        always_on: bool,
    ) -> None:
        self.registrations.append((tool, risk, always_on))


class _FakeTimer:
    def __init__(self) -> None:
        self.intervals: list[tuple[object, float, str]] = []

    async def interval(
        self,
        _ctx,
        callback: object,
        delay: float,
        *,
        name: str,
    ) -> None:
        self.intervals.append((callback, delay, name))


class _FakeContext:
    def __init__(self, module, data_dir: Path) -> None:
        self.runtime = SimpleNamespace(
            plugin_id="github-watch",
            plugin_dir=data_dir.parent,
            data_dir=data_dir,
            workspace=data_dir.parent,
        )
        self.agent_input = _FakeAgentInput()
        self.tools = _FakeTools()
        self.timer = _FakeTimer()
        self.listeners: list[tuple[object, object]] = []
        self._services = {
            module.AGENT_INPUT: self.agent_input,
            module.PLUGIN_TOOLS: self.tools,
            module.TIMER_SERVICE: self.timer,
        }

    def require(self, key: object) -> object:
        return self._services[key]

    async def on(self, key: object, listener: object) -> None:
        self.listeners.append((key, listener))


def test_v3_apply_rebuilds_production_runtime_without_touching_candidate_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_module = _load_plugin_module()
    pem_path = tmp_path / "github-app.pem"
    pem_path.write_text("test")
    candidate_dir = tmp_path / "candidate"
    production_dir = tmp_path / "production"
    config = plugin_module.GitHubWatchConfig(
        app_id=1,
        installation_id=2,
        pem_path=str(pem_path),
        repositories=["owner/repo"],
    )
    ledger_paths: list[Path] = []
    poll_calls: list[list[str]] = []

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
        async def poll(self, repositories: list[str]) -> None:
            poll_calls.append(list(repositories))

    monkeypatch.setattr(plugin_module, "EventLedger", FakeLedger)
    monkeypatch.setattr(plugin_module, "GitHubClient", lambda **_kwargs: object())
    monkeypatch.setattr(plugin_module, "CheckoutManager", FakeCheckouts)
    monkeypatch.setattr(plugin_module, "GitHubOperations", lambda *_args: object())
    monkeypatch.setattr(plugin_module, "GitHubWatch", lambda **_kwargs: FakeWatch())

    candidate = _FakeContext(plugin_module, candidate_dir)
    production = _FakeContext(plugin_module, production_dir)
    asyncio.run(plugin_module.apply(candidate, config))
    asyncio.run(plugin_module.apply(production, config))

    assert ledger_paths == []
    assert candidate_dir.exists() is False
    assert [tool.name for tool, _, _ in production.tools.registrations] == [
        "github_watch_runtime_info",
        "github_watch_post_comment",
        "github_watch_submit_review",
        "github_watch_push_branch",
        "github_watch_create_pr",
    ]
    assert [risk for _, risk, _ in production.tools.registrations] == [
        "read-only",
        "external-side-effect",
        "external-side-effect",
        "external-side-effect",
        "external-side-effect",
    ]
    assert all(always_on for _, _, always_on in production.tools.registrations)
    assert production.listeners[0][0] is plugin_module.AFTER_TURN_COMMITTED
    assert len(production.timer.intervals) == 1
    callback, delay, timer_name = production.timer.intervals[0]
    assert (delay, timer_name) == (120, "poll")

    asyncio.run(callback())

    assert ledger_paths == [production_dir / "events.sqlite3"]
    assert poll_calls == [["owner/repo"]]


def test_composition_agent_input_preserves_core_identities() -> None:
    plugin_module = _load_plugin_module()
    ctx = object()
    service = _FakeAgentInput()
    adapter = plugin_module._CompositionAgentInput(ctx, service)

    session_id = asyncio.run(adapter.create_session({"source": "github-watch"}))
    turn_id = asyncio.run(adapter.submit(session_id, "prompt"))

    assert session_id == "session-1"
    assert turn_id == "turn-1"


def test_tool_authorization_binds_operation_to_core_origin_session(
    tmp_path: Path,
    monkeypatch,
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

    config = plugin_module.GitHubWatchConfig(
        app_id=1,
        installation_id=2,
        pem_path=str(tmp_path / "unused.pem"),
        repositories=["owner/repo"],
    )
    runtime = plugin_module.GitHubWatchRuntime(
        config=config,
        data_dir=tmp_path,
        agent_input=object(),
    )
    runtime._bound = plugin_module._BoundRuntime(
        ledger=FakeLedger(),
        checkouts=object(),
        operations=object(),
        watch=object(),
    )
    monkeypatch.setattr(
        plugin_module,
        "get_current_tool_context",
        lambda: SimpleNamespace(origin_session_key="session-1"),
    )

    assert runtime._authorized_event("a" * 32) is event

    monkeypatch.setattr(
        plugin_module,
        "get_current_tool_context",
        lambda: SimpleNamespace(origin_session_key="session-other"),
    )
    with pytest.raises(PermissionError, match="current dispatched session"):
        runtime._authorized_event("a" * 32)


def test_v3_entrypoint_does_not_import_legacy_plugin_categories() -> None:
    root = Path(__file__).parents[1]
    source = (root / "plugin.py").read_text(encoding="utf-8")
    coordinator = (root / "github_watch.py").read_text(encoding="utf-8")

    for legacy_name in (
        "PluginJobSpec",
        "PluginJobContext",
        "IntervalTrigger",
        "ControlClient",
        "after_turn_modules",
        "@tool",
    ):
        assert legacy_name not in source
        assert legacy_name not in coordinator
