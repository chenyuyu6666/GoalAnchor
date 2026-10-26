from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from goalanchor import (
    AgentRequest,
    AgentRole,
    ArbiterResult,
    ReviewContext,
    ReviewerResult,
    ReviewRisk,
    ReviewVerdict,
)

BRIDGE = Path(__file__).parents[1] / "scripts" / "zcode_goalanchor_bridge.py"


def _request() -> AgentRequest:
    return AgentRequest(
        role=AgentRole.REVIEWER,
        agent_id="reviewer-1",
        round_number=1,
        risk=ReviewRisk.LOW,
        context=ReviewContext(
            goal="判断候选方案是否满足目标",
            prompt="只依据冻结证据进行审核",
            constraints=("不能偷换目标",),
            evidence_refs=("E-1",),
            candidate="候选方案 A",
        ),
    )


def _arbiter_request() -> AgentRequest:
    base = _request()
    reviewers = tuple(
        ReviewerResult(
            reviewer_id=reviewer_id,
            round_number=1,
            verdict=ReviewVerdict.PASS,
            context_fingerprint=base.context.fingerprint,
        )
        for reviewer_id in ("reviewer-1", "reviewer-2")
    )
    return AgentRequest(
        role=AgentRole.ARBITER,
        agent_id="arbiter",
        round_number=1,
        risk=ReviewRisk.MEDIUM,
        context=base.context,
        reviewer_results=reviewers,
    )


def _meta_request() -> AgentRequest:
    base = _arbiter_request()
    arbiter = ArbiterResult(
        arbiter_id="arbiter",
        round_number=1,
        verdict=ReviewVerdict.PASS,
        summary="汇总 Reviewer",
        rationale="证据一致",
        reviewer_ids=("reviewer-1", "reviewer-2"),
        context_fingerprint=base.context.fingerprint,
        disagrees_with_reviewers=False,
    )
    return AgentRequest(
        role=AgentRole.META_REVIEWER,
        agent_id="meta-reviewer",
        round_number=1,
        risk=ReviewRisk.MEDIUM,
        context=base.context,
        reviewer_results=base.reviewer_results,
        arbiter_result=arbiter,
    )


def _run(
    tmp_path: Path,
    fake_cli: str,
    request: AgentRequest,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fake_zcode.py"
    fake.write_text(fake_cli, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "GOALANCHOR_ZCODE_NODE": sys.executable,
            "GOALANCHOR_ZCODE_CLI": str(fake),
            "GOALANCHOR_ZCODE_CWD": str(tmp_path),
        }
    )
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(BRIDGE)],
        input=json.dumps(request.to_dict(), ensure_ascii=False) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=15,
    )


def _reviewer_payload(request: AgentRequest) -> dict[str, object]:
    return {
        "reviewer_id": request.agent_id,
        "round_number": request.round_number,
        "verdict": "PASS",
        "findings": [],
        "isolated": True,
        "context_fingerprint": request.context.fingerprint,
    }


def _stdout_cli(raw: str) -> str:
    return f"import sys\nsys.stdout.write({raw!r})\n"


def test_bridge_validates_and_canonicalizes_reviewer_result(tmp_path: Path) -> None:
    request = _request()
    fake_cli = (
        "import json\n"
        "print(json.dumps({\n"
        f"'reviewer_id': 'reviewer-1', 'round_number': 1, 'verdict': 'PASS',\n"
        "'findings': [], 'isolated': True,\n"
        f"'context_fingerprint': '{request.context.fingerprint}'\n"
        "}))\n"
    )
    result = _run(tmp_path, fake_cli, request)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["reviewer_id"] == "reviewer-1"
    assert payload["context_fingerprint"] == request.context.fingerprint


def test_bridge_rejects_unbound_result(tmp_path: Path) -> None:
    request = _request()
    fake_cli = (
        "import json\n"
        "print(json.dumps({'reviewer_id':'other','round_number':1,'verdict':'PASS',"
        "'findings':[],'isolated':True,'context_fingerprint':'wrong'}))\n"
    )
    result = _run(tmp_path, fake_cli, request)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "not bound" in result.stderr


def test_bridge_binds_arbiter_and_meta_roles(tmp_path: Path) -> None:
    arbiter = _arbiter_request()
    arbiter_cli = (
        "import json\n"
        "print(json.dumps({"
        "'arbiter_id':'arbiter','round_number':1,'verdict':'PASS',"
        "'summary':'ok','rationale':'evidence agrees',"
        "'reviewer_ids':['reviewer-1','reviewer-2'],"
        f"'context_fingerprint':'{arbiter.context.fingerprint}',"
        "'disagrees_with_reviewers':False}))\n"
    )
    arbiter_result = _run(tmp_path / "arbiter", arbiter_cli, arbiter)
    assert arbiter_result.returncode == 0, arbiter_result.stderr
    assert json.loads(arbiter_result.stdout)["arbiter_id"] == "arbiter"

    meta = _meta_request()
    meta_cli = (
        "import json\n"
        "print(json.dumps({"
        "'reviewer_id':'meta-reviewer','round_number':1,'arbiter_id':'arbiter',"
        "'verdict':'PASS','summary':'independent check','findings':[],"
        f"'context_fingerprint':'{meta.context.fingerprint}',"
        "'agrees_with_arbiter':True,'isolated':True}))\n"
    )
    meta_result = _run(tmp_path / "meta", meta_cli, meta)
    assert meta_result.returncode == 0, meta_result.stderr
    assert json.loads(meta_result.stdout)["reviewer_id"] == "meta-reviewer"


def test_bridge_preserves_cli_failure_as_fail_closed(tmp_path: Path) -> None:
    request = _request()
    fake_cli = "import sys\nprint('provider unavailable', file=sys.stderr)\nsys.exit(23)\n"
    result = _run(tmp_path, fake_cli, request)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "code 23" in result.stderr


def test_bridge_rejects_non_object_request() -> None:
    result = subprocess.run(
        [sys.executable, str(BRIDGE)],
        input="[]\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "request must be an object" in result.stderr


@pytest.mark.parametrize(
    "flags",
    [
        {"status": "error", "error": {"code": "TURN_FAILED"}},
        {"status": "success", "success": False},
        {"status": "success", "resultType": "cancelled"},
        {"status": "success", "error": None},
        {"status": "success", "model": "unverified-model"},
        {"status": "success", "projection": {"status": "error"}},
        {},
    ],
)
def test_bridge_rejects_untrusted_envelope(tmp_path: Path, flags: dict[str, object]) -> None:
    request = _request()
    raw = json.dumps({**flags, "result": _reviewer_payload(request)})
    result = _run(tmp_path, _stdout_cli(raw), request)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("marker", ["status", "resultType"])
@pytest.mark.parametrize("as_text", [False, True])
def test_bridge_accepts_explicit_success_envelope(tmp_path: Path, marker: str, as_text: bool) -> None:
    request = _request()
    payload = _reviewer_payload(request)
    raw = json.dumps({marker: "success", "response": json.dumps(payload) if as_text else payload})
    result = _run(tmp_path, _stdout_cli(raw), request)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == payload


def test_bridge_rejects_observed_zcode_017_envelope_without_terminal_success(tmp_path: Path) -> None:
    request = _request()
    # Shape read from the installed CLI bundle, not a native successful model run.
    raw = json.dumps(
        {
            "type": "result",
            "sessionId": "sess_fixture",
            "traceId": "trace_fixture",
            "response": json.dumps(_reviewer_payload(request)),
            "eventCount": 2,
            "projection": {"status": "idle", "turnCount": 1, "totalTokenCount": 10},
        }
    )
    result = _run(tmp_path, _stdout_cli(raw), request)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "unsupported CLI envelope" in result.stderr


@pytest.mark.parametrize("wrapper", ["trailing-error", "leading-error", "markdown", "duplicate-key"])
def test_bridge_requires_one_unambiguous_json_object(tmp_path: Path, wrapper: str) -> None:
    request = _request()
    raw = json.dumps(_reviewer_payload(request))
    if wrapper == "trailing-error":
        raw += '\n{"error":"turn failed"}'
    elif wrapper == "leading-error":
        raw = '{"error":"turn failed"}\n' + raw
    elif wrapper == "markdown":
        raw = "```json\n" + raw + "\n```"
    else:
        raw = raw[:-1] + ', "verdict":"FAIL", "verdict":"PASS"}'
    result = _run(tmp_path, _stdout_cli(raw), request)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("metadata", [{"model": "wrong"}, {"sessionId": "reused"}, {"success": False}])
def test_bridge_does_not_erase_direct_result_metadata(tmp_path: Path, metadata: dict[str, object]) -> None:
    request = _request()
    result = _run(tmp_path, _stdout_cli(json.dumps({**_reviewer_payload(request), **metadata})), request)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("role", ["reviewer", "meta"])
def test_bridge_requires_explicit_isolated(tmp_path: Path, role: str) -> None:
    request = _request() if role == "reviewer" else _meta_request()
    payload = _reviewer_payload(request)
    del payload["isolated"]
    if role == "meta":
        payload.update(arbiter_id="arbiter", summary="review", agrees_with_arbiter=True)
    result = _run(tmp_path, _stdout_cli(json.dumps(payload)), request)
    assert result.returncode != 0
    assert result.stdout == ""


def test_bridge_refuses_strict_requests_before_launch(tmp_path: Path) -> None:
    base = _request()
    request = replace(base, context=replace(base.context, strict_binding={"baseline_id": "b1"}))
    marker = tmp_path / "launched"
    fake = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    fake += _stdout_cli(json.dumps(_reviewer_payload(request)))
    result = _run(tmp_path, fake, request)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Strict" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "-1", "not-a-timeout"])
def test_bridge_rejects_invalid_timeout_before_launch(tmp_path: Path, timeout: str) -> None:
    request = _request()
    marker = tmp_path / "launched"
    fake = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    fake += _stdout_cli(json.dumps(_reviewer_payload(request)))
    result = _run(tmp_path, fake, request, {"GOALANCHOR_ZCODE_TIMEOUT_SECONDS": timeout})
    assert result.returncode != 0
    assert result.stdout == ""
    assert not marker.exists()


def test_bridge_timeout_does_not_echo_frozen_prompt(tmp_path: Path) -> None:
    request = replace(_request(), context=replace(_request().context, candidate="PRIVATE_PROMPT_CANARY"))
    result = _run(
        tmp_path,
        "import time\ntime.sleep(2)\n",
        request,
        {"GOALANCHOR_ZCODE_TIMEOUT_SECONDS": "0.05"},
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "timed out" in result.stderr
    assert "PRIVATE_PROMPT_CANARY" not in result.stderr
    assert "Traceback" not in result.stderr


def test_bridge_does_not_echo_provider_diagnostics(tmp_path: Path) -> None:
    fake = "import sys\nprint('PRIVATE_PROVIDER_CANARY', file=sys.stderr)\nsys.exit(23)\n"
    result = _run(tmp_path, fake, _request())
    assert result.returncode != 0
    assert result.stdout == ""
    assert "code 23" in result.stderr
    assert "PRIVATE_PROVIDER_CANARY" not in result.stderr


def test_bridge_missing_cli_has_explicit_safe_error(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "raise AssertionError('must not run')\n",
        _request(),
        {"GOALANCHOR_ZCODE_CLI": str(tmp_path / "missing.cjs")},
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "GOALANCHOR_ZCODE_CLI" in result.stderr
    assert "Traceback" not in result.stderr
