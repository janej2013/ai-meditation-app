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
# checkpoint.py is the one bridge to the store; local_harness.py, smoke.py
# and cli.py are the harness inlined until A4 (they claim, run and commit
# turns, as agent_runner will).
LOCAL_DRIVERS = ("checkpoint.py", "local_harness.py", "smoke.py", "cli.py")
LANGGRAPH_FILES = sorted(AGENT_DIR.glob("langgraph/**/*.py"))
ENGINE_FILES = [
    path
    for path in [*SHARED_AND_NATIVE, *LANGGRAPH_FILES]
    if path.name not in LOCAL_DRIVERS and "tools" not in path.parts
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
    assert any(p.name == "graph.py" for p in LANGGRAPH_FILES)


def test_langgraph_is_the_only_place_frameworks_are_imported():
    """The framework engine is optional: nothing else may need it."""
    outside = [p for p in AGENT_DIR.glob("**/*.py") if "langgraph" not in p.parts]
    for path in outside:
        offending = {n for n in imported_modules(path) if n.split(".")[0] in FRAMEWORKS}
        assert not offending, f"{path.relative_to(AGENT_DIR)} imports {sorted(offending)}"
    inside = {n for p in LANGGRAPH_FILES for n in imported_modules(p)}
    assert any(n.split(".")[0] in FRAMEWORKS for n in inside)


@pytest.mark.parametrize("path", SHARED_AND_NATIVE, ids=lambda p: str(p.relative_to(AGENT_DIR)))
def test_no_framework_imports_outside_the_langgraph_package(path: Path):
    offending = {name for name in imported_modules(path) if name.split(".")[0] in FRAMEWORKS}
    assert not offending, f"{path.name} imports {sorted(offending)}"


ALL_AGENT_FILES = sorted(AGENT_DIR.glob("**/*.py"))
HTTP_LAYER = ("api", "fastapi")


@pytest.mark.parametrize("path", ALL_AGENT_FILES, ids=lambda p: str(p.relative_to(AGENT_DIR)))
def test_agent_never_imports_the_http_layer(path: Path):
    """The agent must run without FastAPI on the path, and must not reach
    into the API's request-scoped helpers -- shared logic lives in shared/."""
    offending = {name for name in imported_modules(path) if name.split(".")[0] in HTTP_LAYER}
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize(
    "path",
    [p for p in ALL_AGENT_FILES if p.name not in ("local_harness.py", "smoke.py", "cli.py")],
    ids=lambda p: str(p.relative_to(AGENT_DIR)),
)
def test_agent_never_imports_the_harness(path: Path):
    """Dependencies point one way: the harness imports the agent. The local
    drivers are the exception -- they *are* the harness on a laptop."""
    offending = {name for name in imported_modules(path) if name.startswith("agent_runner")}
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: str(p.relative_to(AGENT_DIR)))
def test_engines_do_not_import_the_store(path: Path):
    offending = {name for name in imported_modules(path) if name.startswith("shared.db")}
    assert not offending, f"{path.name} imports the store: {sorted(offending)}"
