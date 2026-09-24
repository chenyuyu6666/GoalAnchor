"""Fixed-fixture regression and version comparison primitives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .core import ContractValidationError


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a boolean")
    return value


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    return tuple(_text(item, f"{field_name}[]") for item in value)


@dataclass(frozen=True)
class RegressionOutcome:
    fixture_id: str
    failure_class: str
    expected_passed: bool
    actual_passed: bool
    finding_codes: tuple[str, ...] = ()
    expected_finding_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_id", _text(self.fixture_id, "fixture_id"))
        object.__setattr__(self, "failure_class", _text(self.failure_class, "failure_class"))
        object.__setattr__(self, "expected_passed", _bool(self.expected_passed, "expected_passed"))
        object.__setattr__(self, "actual_passed", _bool(self.actual_passed, "actual_passed"))
        object.__setattr__(self, "finding_codes", _strings(self.finding_codes, "finding_codes"))
        object.__setattr__(
            self,
            "expected_finding_codes",
            _strings(self.expected_finding_codes, "expected_finding_codes"),
        )

    @property
    def aligned(self) -> bool:
        return self.expected_passed == self.actual_passed and (
            not self.expected_finding_codes
            or self.finding_codes == self.expected_finding_codes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "failure_class": self.failure_class,
            "expected_passed": self.expected_passed,
            "actual_passed": self.actual_passed,
            "finding_codes": list(self.finding_codes),
            "expected_finding_codes": list(self.expected_finding_codes),
            "aligned": self.aligned,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RegressionOutcome:
        if not isinstance(payload, Mapping):
            raise ContractValidationError("RegressionOutcome must be a JSON object")
        return cls(
            fixture_id=payload["fixture_id"],
            failure_class=payload["failure_class"],
            expected_passed=payload["expected_passed"],
            actual_passed=payload["actual_passed"],
            finding_codes=payload.get("finding_codes", ()),
            expected_finding_codes=payload.get("expected_finding_codes", ()),
        )


@dataclass(frozen=True)
class VersionRun:
    revision: str
    fixtures_sha256: str
    acceptance_sha256: str
    outcomes: tuple[RegressionOutcome, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _text(self.revision, "revision"))
        object.__setattr__(self, "fixtures_sha256", _text(self.fixtures_sha256, "fixtures_sha256"))
        object.__setattr__(
            self, "acceptance_sha256", _text(self.acceptance_sha256, "acceptance_sha256")
        )
        outcomes = tuple(self.outcomes)
        if not outcomes:
            raise ContractValidationError("outcomes must contain at least one item")
        if any(not isinstance(item, RegressionOutcome) for item in outcomes):
            raise ContractValidationError("outcomes must contain RegressionOutcome values")
        identifiers = [item.fixture_id for item in outcomes]
        if len(identifiers) != len(set(identifiers)):
            raise ContractValidationError("outcome fixture IDs must be unique")
        object.__setattr__(self, "outcomes", outcomes)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def aligned_count(self) -> int:
        return sum(item.aligned for item in self.outcomes)

    @property
    def accepted_count(self) -> int:
        return sum(item.actual_passed for item in self.outcomes)

    @property
    def rejected_count(self) -> int:
        return self.total - self.accepted_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "fixtures_sha256": self.fixtures_sha256,
            "acceptance_sha256": self.acceptance_sha256,
            "total": self.total,
            "aligned_count": self.aligned_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VersionRun:
        if not isinstance(payload, Mapping):
            raise ContractValidationError("VersionRun must be a JSON object")
        return cls(
            revision=payload["revision"],
            fixtures_sha256=payload["fixtures_sha256"],
            acceptance_sha256=payload["acceptance_sha256"],
            outcomes=tuple(RegressionOutcome.from_dict(item) for item in payload["outcomes"]),
        )


@dataclass(frozen=True)
class VersionComparison:
    before_revision: str
    after_revision: str
    comparable: bool
    not_comparable_reason: str | None
    metric_delta: Mapping[str, int]
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "comparable": self.comparable,
            "not_comparable_reason": self.not_comparable_reason,
            "metric_delta": dict(self.metric_delta),
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
        }


def compare_version_runs(before: VersionRun, after: VersionRun) -> VersionComparison:
    """Compare two runs only when fixture and acceptance inputs are identical."""

    if not isinstance(before, VersionRun) or not isinstance(after, VersionRun):
        raise ContractValidationError("compare_version_runs expects VersionRun values")
    if before.fixtures_sha256 != after.fixtures_sha256:
        return VersionComparison(
            before.revision,
            after.revision,
            False,
            "fixture inputs differ",
            {},
            (),
            (),
        )
    if before.acceptance_sha256 != after.acceptance_sha256:
        return VersionComparison(
            before.revision,
            after.revision,
            False,
            "acceptance criteria differ",
            {},
            (),
            (),
        )
    before_by_id = {item.fixture_id: item for item in before.outcomes}
    after_by_id = {item.fixture_id: item for item in after.outcomes}
    if set(before_by_id) != set(after_by_id):
        return VersionComparison(
            before.revision,
            after.revision,
            False,
            "fixture ID sets differ",
            {},
            (),
            (),
        )
    regressions = tuple(
        fixture_id
        for fixture_id in sorted(before_by_id)
        if before_by_id[fixture_id].aligned and not after_by_id[fixture_id].aligned
    )
    improvements = tuple(
        fixture_id
        for fixture_id in sorted(before_by_id)
        if not before_by_id[fixture_id].aligned and after_by_id[fixture_id].aligned
    )
    return VersionComparison(
        before.revision,
        after.revision,
        True,
        None,
        {
            "total": after.total - before.total,
            "aligned_count": after.aligned_count - before.aligned_count,
            "accepted_count": after.accepted_count - before.accepted_count,
            "rejected_count": after.rejected_count - before.rejected_count,
        },
        regressions,
        improvements,
    )


__all__ = [
    "RegressionOutcome",
    "VersionComparison",
    "VersionRun",
    "compare_version_runs",
]
