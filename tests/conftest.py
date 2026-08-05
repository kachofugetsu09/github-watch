from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

if importlib.util.find_spec("agent") is None:
    agent = ModuleType("agent")
    control = ModuleType("agent.control")
    client = ModuleType("agent.control.client")
    client.ControlClient = object  # type: ignore[attr-defined]
    sys.modules.update(
        {"agent": agent, "agent.control": control, "agent.control.client": client}
    )

ROOT = Path(__file__).parents[1]
PACKAGE = "github_watch_test_package"
spec = importlib.util.spec_from_file_location(
    PACKAGE,
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot create github-watch test package")
module = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = module
spec.loader.exec_module(module)
