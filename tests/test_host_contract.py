from __future__ import annotations

import pytest

from goalanchor.core import ContractValidationError
from goalanchor.host_contract import capabilities, decision, decode_envelope, envelope


def test_wire_version_and_unknown_semantics_fail_closed():
    assert decode_envelope(envelope("question", {"id": "Q1"})) == ("question", {"id": "Q1"})
    for raw in ('{"protocol":"v99","kind":"question","data":{}}',
                '{"protocol":"goalanchor.workflow.v1","kind":"question","data":{},"required":true}'):
        with pytest.raises(ContractValidationError):
            decode_envelope(raw)


@pytest.mark.parametrize("host", ["codex", "claude", "dsh", "zcode", "pi-desktop"])
def test_same_governance_and_honest_host_fallback(host):
    assert decision(host, has_baseline=True, has_evidence=True, review_passed=True,
                    authorized=False, execution_supported=True) == "WAIT_AUTHORIZATION"
    assert decision(host, has_baseline=True, has_evidence=True, review_passed=True,
                    authorized=True, execution_supported=False) == "UNAVAILABLE"
    assert capabilities(host)["native_enforcement"] != "VERIFIED"


def test_unknown_host_and_missing_prerequisites_never_allow():
    with pytest.raises(ContractValidationError):
        capabilities("invented")
    assert decision("codex", has_baseline=False, has_evidence=True, review_passed=True,
                    authorized=True, execution_supported=True) == "BLOCKED"


def test_t47_skill_only_never_inherits_app_server_enforcement():
    assert capabilities("codex")["native_enforcement"] == "ADVISORY"
    assert capabilities("codex", mode="app-server")["native_enforcement"] == "LIMITED"
    assert capabilities("dsh", mode="app-server")["native_enforcement"] == "UNAVAILABLE"
