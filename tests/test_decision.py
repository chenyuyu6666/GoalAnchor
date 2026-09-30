from __future__ import annotations

import pytest

from goalanchor import (
    ActionType,
    Authorization,
    AuthorizationKind,
    AuthorizationScope,
    ContractValidationError,
    DecisionActor,
    DecisionRecord,
    LifecycleState,
    check_action_authorization,
)

NOW = "2026-09-19T10:00:00+08:00"


def make_grant(**overrides) -> Authorization:
    values = {
        "authorization_id": "A-1",
        "kind": AuthorizationKind.AUTONOMY_GRANT,
        "scope": AuthorizationScope.SESSION,
        "scope_ref": "session-1",
        "actions": (ActionType.SOURCE_EDIT,),
        "granted_by": DecisionActor.USER,
        "granted_at": "2026-09-19T09:00:00+08:00",
        "expires_at": "2026-09-19T11:00:00+08:00",
    }
    values.update(overrides)
    return Authorization(**values)


def test_decision_record_is_audit_data_not_design_necessity() -> None:
    record = DecisionRecord(
        record_id="D-1",
        selected_option="保留兼容适配层",
        alternatives=("重写旧规则",),
        evidence_refs=("E-1",),
        constraint_refs=("C-1",),
        reason="保持现有行为兼容",
        decided_by=DecisionActor.USER,
        created_at=NOW,
        authorized_by=DecisionActor.USER,
        authorization_id="A-1",
        goal_id="G-1",
    )
    assert DecisionRecord.from_dict(record.to_dict()) == record
    assert "necessary_conditions_proven" not in record.to_dict()


def test_authorization_round_trip_preserves_scope_and_expiry() -> None:
    grant = make_grant()
    assert Authorization.from_dict(grant.to_dict()) == grant
    assert grant.kind is AuthorizationKind.AUTONOMY_GRANT


def test_safe_actions_are_automatic_and_source_edits_require_authorization() -> None:
    automatic = check_action_authorization(
        ActionType.TEST,
        scope=AuthorizationScope.SESSION,
        scope_ref="session-1",
        at=NOW,
    )
    assert automatic.passed

    edit = check_action_authorization(
        ActionType.SOURCE_EDIT,
        scope=AuthorizationScope.SESSION,
        scope_ref="session-1",
        at=NOW,
    )
    assert not edit.passed
    assert edit.findings[0].code == "AUTHORIZATION_REQUIRED"
    assert edit.next_state is LifecycleState.HUMAN_DECISION_REQUIRED


def test_matching_grant_allows_action_but_wrong_or_expired_scope_does_not() -> None:
    grant = make_grant()
    allowed = check_action_authorization(
        ActionType.SOURCE_EDIT,
        scope=AuthorizationScope.SESSION,
        scope_ref="session-1",
        at=NOW,
        authorizations=(grant,),
    )
    assert allowed.passed

    wrong_scope = check_action_authorization(
        ActionType.SOURCE_EDIT,
        scope=AuthorizationScope.SESSION,
        scope_ref="session-2",
        at=NOW,
        authorizations=(grant,),
    )
    assert not wrong_scope.passed

    expired = check_action_authorization(
        ActionType.SOURCE_EDIT,
        scope=AuthorizationScope.SESSION,
        scope_ref="session-1",
        at="2026-09-19T12:00:00+08:00",
        authorizations=(grant,),
    )
    assert not expired.passed


def test_critical_actions_require_explicit_confirmation() -> None:
    result = check_action_authorization(
        ActionType.DELETE,
        scope=AuthorizationScope.PROJECT,
        scope_ref="GoalAnchor",
        at=NOW,
    )
    assert not result.passed
    assert result.findings[0].code == "EXPLICIT_CONFIRMATION_REQUIRED"


def test_authorization_and_decision_validation_rejects_ambiguous_records() -> None:
    with pytest.raises(ContractValidationError, match="timezone"):
        make_grant(granted_at="2026-09-19T09:00:00")
    with pytest.raises(ContractValidationError, match="provided together"):
        DecisionRecord(
            record_id="D-2",
            selected_option="A",
            reason="test",
            decided_by=DecisionActor.USER,
            created_at=NOW,
            authorized_by=DecisionActor.USER,
        )
