from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_plugin_module():
    plugins = ModuleType("agent.plugins")

    class Plugin:
        pass

    class IntervalTrigger:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class PluginJobContext:
        pass

    class PluginJobSpec:
        def __init__(self, **_kwargs: object) -> None:
            pass

    def tool(**_kwargs: object):
        return lambda function: function

    plugins.IntervalTrigger = IntervalTrigger  # type: ignore[attr-defined]
    plugins.Plugin = Plugin  # type: ignore[attr-defined]
    plugins.PluginJobContext = PluginJobContext  # type: ignore[attr-defined]
    plugins.PluginJobSpec = PluginJobSpec  # type: ignore[attr-defined]
    plugins.tool = tool  # type: ignore[attr-defined]

    tools = ModuleType("agent.tools")
    tools_base = ModuleType("agent.tools.base")
    tools_base.get_current_tool_context = lambda: None  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "agent.plugins": plugins,
            "agent.tools": tools,
            "agent.tools.base": tools_base,
        }
    )
    return importlib.import_module("github_watch_test_package.plugin")


def test_runtime_binds_current_data_dir_after_candidate_activation(
    tmp_path: Path, monkeypatch
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
    plugin = plugin_module.GitHubWatchPlugin()
    plugin.context = SimpleNamespace(
        config=config,
        data_dir=candidate_dir,
        workspace=tmp_path,
    )
    ledger_paths: list[Path] = []

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

    monkeypatch.setattr(plugin_module, "EventLedger", FakeLedger)
    monkeypatch.setattr(plugin_module, "GitHubClient", lambda **_kwargs: object())
    monkeypatch.setattr(plugin_module, "CheckoutManager", FakeCheckouts)
    monkeypatch.setattr(plugin_module, "GitHubOperations", lambda *_args: object())
    monkeypatch.setattr(plugin_module, "GitHubWatch", lambda **_kwargs: object())

    plugin.activate()
    assert ledger_paths == []

    plugin.context.data_dir = production_dir
    plugin._ensure_runtime()
    plugin._ensure_runtime()

    assert ledger_paths == [production_dir / "events.sqlite3"]
