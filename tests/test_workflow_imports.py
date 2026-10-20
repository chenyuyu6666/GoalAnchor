"""Compatibility checks for the split workflow modules and public façade."""

from __future__ import annotations

import importlib


def test_workflow_modules_and_supported_import_identities() -> None:
    contracts = importlib.import_module("goalanchor.workflow_contracts")
    legacy = importlib.import_module("goalanchor.workflow_legacy")
    strict = importlib.import_module("goalanchor.workflow_strict")
    facade = importlib.import_module("goalanchor.workflow")
    package = importlib.import_module("goalanchor")
    controller = importlib.import_module("goalanchor.codex_controller")
    text_host = importlib.import_module("goalanchor.text_host")
    workflow_cli = importlib.import_module("goalanchor.workflow_cli")
    store = importlib.import_module("goalanchor.workflow_store")
    protocol = importlib.import_module("goalanchor.workflow_protocol")

    contract_exports = (
        "WorkflowPolicy",
        "StrictWorkflowError",
        "TrustedEventProofProvider",
        "TrustedRuntimeBindingProvider",
        "TrustedReviewProofProvider",
        "TrustedProofUnavailable",
        "TrustedProofRejected",
        "TrustedBindingUnavailable",
        "TrustedBindingStale",
    )
    for name in contract_exports:
        assert getattr(facade, name) is getattr(contracts, name)

    assert facade.Workflow is legacy.Workflow
    assert facade.StrictWorkflow is strict.StrictWorkflow
    assert strict.StrictWorkflow.__mro__[1] is legacy.Workflow
    assert facade.WorkflowStoreConflict is store.WorkflowStoreConflict
    assert facade.parse_time is protocol.parse_time

    package_exports = (
        "StrictWorkflow",
        "StrictWorkflowError",
        "TrustedBindingStale",
        "TrustedBindingUnavailable",
        "TrustedEventProofProvider",
        "TrustedProofRejected",
        "TrustedProofUnavailable",
        "TrustedRuntimeBindingProvider",
    )
    for name in package_exports:
        assert getattr(package, name) is getattr(facade, name)

    assert controller.Workflow is facade.Workflow
    assert text_host.Workflow is facade.Workflow
    assert callable(workflow_cli.main)
