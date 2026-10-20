"""Compatibility checks for the split store modules and old import path."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


def test_workflow_store_facade_preserves_public_object_identity() -> None:
    core = importlib.import_module("goalanchor.workflow_store_core")
    strict = importlib.import_module("goalanchor.workflow_store_strict")
    facade = importlib.import_module("goalanchor.workflow_store")

    assert facade.WorkflowStore is strict.WorkflowStore
    assert facade.WorkflowStoreConflict is core.WorkflowStoreConflict
    assert facade.Payload is core.Payload
    assert facade.Expected is core.Expected
    assert facade.WorkflowStore.__mro__[1] is core.WorkflowStoreCore
    assert set(facade.__all__) == {"WorkflowStore", "WorkflowStoreConflict", "Payload", "Expected"}


@pytest.mark.parametrize(
    "module_order",
    (
        (
            "goalanchor.workflow_store",
            "goalanchor.workflow_store_strict",
            "goalanchor.workflow_store_core",
        ),
        (
            "goalanchor.workflow_store_core",
            "goalanchor.workflow_store_strict",
            "goalanchor.workflow_store",
        ),
    ),
)
def test_workflow_store_modules_import_in_fresh_processes(module_order: tuple[str, ...]) -> None:
    code = """
import importlib
import sys

for module_name in sys.argv[1:]:
    importlib.import_module(module_name)

core = importlib.import_module("goalanchor.workflow_store_core")
strict = importlib.import_module("goalanchor.workflow_store_strict")
facade = importlib.import_module("goalanchor.workflow_store")
assert facade.WorkflowStore is strict.WorkflowStore
assert facade.WorkflowStore.__mro__[1] is core.WorkflowStoreCore
"""
    result = subprocess.run(
        [sys.executable, "-c", code, *module_order],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
