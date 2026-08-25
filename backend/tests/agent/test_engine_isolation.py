"""The two boundaries the engines must keep (docs/agent-runner-plan.md §3.4).

* Nothing shared and nothing native imports a framework: the hand-built
  engine is the default and must stay complete on its own.
* Engines do not touch the table: ``checkpoint.py`` is the one bridge.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
FRAMEWORKS = ("langchain", "langgraph")

SHARED_AND_NATIVE = sorted(
    [*AGENT_DIR.glob("*.py"), *AGENT_DIR.glob("tools/*.py"), *AGENT_DIR.glob("native/**/*.py")]
)
ENGINE_FILES = [
    path for path in SHARED_AND_NATIVE if path.name != "checkpoint.py" and "tools" not in path.parts
]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_agent_package_has_files():
    assert any(p.name == "loop.py" for p in SHARED_AND_NATIVE)


@pytest.mark.parametrize("path", SHARED_AND_NATIVE, ids=lambda p: str(p.relative_to(AGENT_DIR)))
def test_no_framework_imports_outside_the_langgraph_package(path: Path):
    offending = {name for name in imported_modules(path) if name.split(".")[0] in FRAMEWORKS}
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: str(p.relative_to(AGENT_DIR)))
def test_engines_do_not_import_the_store(path: Path):
    offending = {name for name in imported_modules(path) if name.startswith("shared.db")}
    assert not offending, f"{path.name} imports the store: {sorted(offending)}"
