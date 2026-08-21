from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGE = "github_watch_test_package"

# The domain unit tests load the plugin package without a full Core checkout.
# Keep the exact Core admission error identities available at that boundary.
if "agent.plugin_composition" not in sys.modules:
    agent = ModuleType("agent")
    composition = ModuleType("agent.plugin_composition")

    class ProgrammaticTurnPreAdmissionError(RuntimeError):
        def __init__(self, message: str, *, reason: str | None = None) -> None:
            super().__init__(message)
            self.reason = reason

    class ProgrammaticTurnUncertainError(RuntimeError):
        pass

    composition.ProgrammaticTurnPreAdmissionError = ProgrammaticTurnPreAdmissionError  # type: ignore[attr-defined]
    composition.ProgrammaticTurnUncertainError = ProgrammaticTurnUncertainError  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "agent": agent,
            "agent.plugin_composition": composition,
        }
    )

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
