"""AURA-0094 calibration, human review, qualification, and ensemble tests."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from aura_observatory.evaluation_calibration import (
    MAX_DISAGREEMENT,
    MAX_MEAN_ABSOLUTE_ERROR,
    MIN_AGREEMENT,
    CalibrationError,
    CalibrationLabel,
    CalibrationReport,
    CalibrationService,
    DisagreementClass,
    EnsembleMember,
    HumanReview,
    InMemoryCalibrationRepository,
    Qualification,
    combine_ensemble,
)
from aura_observatory.evaluation_judges import JudgeAdvisoryState, JudgeOutcome

IDS = {
    "judge": "10000000-0000-0000-0000-000000000001",
    "case": "10000000-0000-0000-0000-000000000002",
    "result": "10000000-0000-0000-0000-000000000003",
    "calibration": "10000000-0000-0000-0000-000000000012",
    "label": "10000000-0000-0000-0000-000000000004",
    "review": "10000000-0000-0000-0000-000000000005",
    "qualification": "10000000-0000-0000-0000-000000000006",
    "suite": "10000000-0000-0000-0000-000000000007",
    "dataset": "10000000-0000-0000-0000-000000000008",
    "evaluator": "10000000-0000-0000-0000-000000000009",
    "member1": "10000000-0000-0000-0000-000000000010",
    "member2": "10000000-0000-0000-0000-000000000011",
}
NOW = datetime(2026, 10, 19, tzinfo=UTC)


def _label(
    index: int, *, score: float = 0.9, reference: float = 0.9, mismatch: bool = False
) -> CalibrationLabel:
    return CalibrationLabel(
        f"10000000-0000-0000-0000-{index:012d}",
        IDS["case"],
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        reference,
        "pass",
        score,
        "pass" if not mismatch else "fail",
        created_at=NOW,
        idempotency_key=f"20000000-0000-0000-0000-{index:012d}",
        authorization_record_id=IDS["result"],
    )


def _qualification(*, qualified: bool = True) -> Qualification:
    return Qualification(
        IDS["qualification"],
        IDS["suite"],
        "1.0.0",
        IDS["dataset"],
        "1.0.0",
        IDS["evaluator"],
        "1.0.0",
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        qualified=qualified,
        created_at=NOW,
        idempotency_key=IDS["qualification"],
        authorization_record_id=IDS["result"],
        calibration_result_id=IDS["result"],
    )


def _complete_qualification() -> Qualification:
    """Build the contract-shaped provenance required for enabled ensembles."""

    return replace(
        _qualification(),
        qualified_at=NOW,
        model_name="llama3.2:3b",
        model_version="3.2.0",
        model_digest="a" * 64,
        prompt_artifact_reference_id=IDS["label"],
        prompt_artifact_version="1.0.0",
        prompt_artifact_digest="b" * 64,
        rubric_artifact_reference_id=IDS["review"],
        rubric_artifact_version="1.0.0",
        rubric_artifact_digest="c" * 64,
        budget={
            "timeoutMs": 120_000,
            "maxInputBytes": 1_048_576,
            "maxOutputBytes": 8_192,
            "maxOutputTokens": 2_048,
            "maxContextTokens": 4_096,
            "maxConcurrency": 1,
            "temperature": 0,
        },
        provenance_component="aura.observatory.calibration",
        provenance_version="1.0.0",
        provenance_digest="d" * 64,
        calibration_id=IDS["calibration"],
    )


def _complete_report() -> CalibrationReport:
    qualification = _complete_qualification()
    return CalibrationReport(
        qualification.judge_id,
        qualification.judge_version,
        qualification.prompt_version,
        qualification.rubric_version,
        qualification.calibration_version,
        20,
        1.0,
        0.0,
        0.0,
        True,
        dataset_id=qualification.dataset_id,
        dataset_version=qualification.dataset_version,
        suite_id=qualification.suite_id,
        suite_version=qualification.suite_version,
        evaluator_id=qualification.evaluator_id,
        evaluator_version=qualification.evaluator_version,
        calibration_result_id=qualification.calibration_result_id,
        calibration_id=qualification.calibration_id,
        model_name=qualification.model_name,
        model_version=qualification.model_version,
        model_digest=qualification.model_digest,
        seed=qualification.seed,
        budget=qualification.budget,
        prompt_artifact_reference_id=qualification.prompt_artifact_reference_id,
        prompt_artifact_version=qualification.prompt_artifact_version,
        prompt_artifact_digest=qualification.prompt_artifact_digest,
        rubric_artifact_reference_id=qualification.rubric_artifact_reference_id,
        rubric_artifact_version=qualification.rubric_artifact_version,
        rubric_artifact_digest=qualification.rubric_artifact_digest,
        provenance_component=qualification.provenance_component,
        provenance_version=qualification.provenance_version,
        provenance_digest=qualification.provenance_digest,
    )


class _ProductionQualificationRepository:
    """Minimal durable-repository seam for resolver qualification tests."""

    def __init__(self, report: CalibrationReport, qualification: Qualification) -> None:
        self.report = report
        self.qualification = qualification
        self.result_lookups: list[str] = []
        self.scope: dict[str, Any] = {}

    async def get_calibration_result(self, result_id: str) -> CalibrationReport | None:
        self.result_lookups.append(result_id)
        return self.report if result_id == self.report.calibration_result_id else None

    async def is_authorized(
        self,
        _authorization_record_id: str,
        _purpose: str,
        *,
        resource_id: str | None = None,
        scope_fingerprint: str | None = None,
    ) -> bool:
        return resource_id == self.qualification.qualification_id and scope_fingerprint is not None

    async def put_qualification(self, item: Qualification) -> Qualification:
        self.qualification = item
        return item

    async def active_qualification(
        self, **scope: str | int | dict[str, int | float]
    ) -> Qualification:
        self.scope = dict(scope)
        return self.qualification


@pytest.mark.asyncio
async def test_production_qualification_uses_prior_durable_calibration_result_and_exact_scope(
) -> None:
    qualification = _complete_qualification()
    report = _complete_report()
    repository = _ProductionQualificationRepository(report, qualification)
    service = CalibrationService(repository)  # type: ignore[arg-type]

    # The calibration result is a prior durable record, distinct from the
    # evaluation result that authorized this qualification request.
    evaluation_result_id = IDS["case"]
    assert report.calibration_result_id != evaluation_result_id
    assert (
        await service.qualify(
            qualification,
            report,
            owner_authorized=True,
            idempotency_key=qualification.idempotency_key or "",
        )
        is qualification
    )
    assert repository.result_lookups == [report.calibration_result_id]

    assert await service.is_qualified_async(
        suite_id=qualification.suite_id,
        dataset_id=qualification.dataset_id,
        evaluator_id=qualification.evaluator_id,
        judge_id=qualification.judge_id,
        judge_version=qualification.judge_version,
        prompt_version=qualification.prompt_version,
        rubric_version=qualification.rubric_version,
        calibration_version=qualification.calibration_version,
    )
    assert repository.scope["instruction_version"] == qualification.prompt_version


def test_calibration_domain_contracts_round_trip_all_versioned_resources() -> None:
    from test_aura0094_contract import _schema, validate_schema

    label = replace(
        _label(777),
        reference_label="accepted",
        judge_label="accepted",
        calibration_id=IDS["calibration"],
        result_id=IDS["result"],
        reference_outcome=JudgeOutcome.PASS,
    )
    review = HumanReview(
        IDS["review"],
        IDS["case"],
        IDS["result"],
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        "pass",
        0.9,
        DisagreementClass.NONE,
        created_at=NOW,
        idempotency_key=IDS["review"],
        authorization_record_id=IDS["result"],
        calibration_id=IDS["calibration"],
        score_id=IDS["result"],
        outcome=JudgeOutcome.PASS,
        review_label="accepted",
    )
    resources = (
        label.as_contract(),
        review.as_contract(),
        _complete_report().as_contract(),
        _complete_qualification().as_contract(),
    )
    schema = _schema()
    for resource in resources:
        validate_schema(resource, schema, schema, "$instance")


@pytest.mark.asyncio
async def test_calibration_uses_exact_20_label_boundary_and_thresholds() -> None:
    service = CalibrationService()
    for index in range(1, 21):
        await service.label(_label(index))
    report = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
        dataset_id=IDS["dataset"],
        dataset_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        calibration_result_id=IDS["result"],
    )
    assert report.sample_count == 20
    assert report.agreement == 1.0
    assert report.mean_absolute_error == 0.0
    assert report.disagreement_rate == 0.0
    assert report.eligible is True
    assert report.threshold_agreement == MIN_AGREEMENT
    assert report.threshold_mean_absolute_error == MAX_MEAN_ABSOLUTE_ERROR
    assert report.threshold_disagreement_rate == MAX_DISAGREEMENT

    too_few = CalibrationService()
    for index in range(1, 20):
        await too_few.label(_label(index))
    report = await too_few.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    assert report.sample_count == 19
    assert report.eligible is False


@pytest.mark.asyncio
async def test_calibration_rejects_incompatible_versions_and_reports_disagreement() -> None:
    service = CalibrationService()
    for index in range(1, 21):
        await service.label(_label(index, score=0.7, reference=0.9, mismatch=index <= 5))
    await service.label(_label(999, score=0.9, reference=0.9))
    report = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
        dataset_id=IDS["dataset"],
        dataset_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        calibration_result_id=IDS["result"],
    )
    assert report.sample_count == 21
    assert report.agreement < MIN_AGREEMENT
    assert report.mean_absolute_error > MAX_MEAN_ABSOLUTE_ERROR
    assert report.disagreement_rate > MAX_DISAGREEMENT
    assert report.eligible is False

    incompatible = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-2",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    assert incompatible.sample_count == 0
    assert incompatible.eligible is False


@pytest.mark.asyncio
async def test_reviews_are_idempotent_and_comments_are_opaque_references() -> None:
    repository = InMemoryCalibrationRepository()
    service = CalibrationService(repository)
    review = HumanReview(
        IDS["review"],
        IDS["case"],
        IDS["result"],
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        "pass",
        0.9,
        DisagreementClass.NONE,
        comment_artifact_reference_id=IDS["label"],
        created_at=NOW,
        idempotency_key=IDS["review"],
        authorization_record_id=IDS["result"],
        calibration_id=IDS["suite"],
        score_id=IDS["result"],
    )
    assert await service.review(review) is review
    assert await service.review(review) is review
    assert repository.audits[-1] == {"kind": "review", "outcome": "duplicate"}
    conflicting = HumanReview(
        review.review_id,
        review.case_id,
        review.result_id,
        review.judge_id,
        review.judge_version,
        review.prompt_version,
        review.rubric_version,
        review.calibration_version,
        "fail",
            0.1,
            DisagreementClass.SCORE,
            created_at=NOW,
            idempotency_key=IDS["review"],
            authorization_record_id=IDS["result"],
    )
    with pytest.raises(CalibrationError, match="idempotency_conflict"):
        await service.review(conflicting)


@pytest.mark.asyncio
async def test_comment_artifact_must_be_authorized_for_human_review_purpose() -> None:
    class _PurposeRepository(InMemoryCalibrationRepository):
        def __init__(self, purpose: str) -> None:
            super().__init__()
            self.purpose = purpose

        async def is_comment_artifact_authorized(
            self,
            _artifact_reference_id: str,
            *,
            artifact_version: str | None = None,
            artifact_digest: str | None = None,
        ) -> bool:
            return (
                self.purpose == "comment"
                and artifact_version == "1.0.0"
                and artifact_digest == "a" * 64
            )

    review = HumanReview(
        IDS["review"],
        IDS["case"],
        IDS["result"],
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        "pass",
        0.9,
        DisagreementClass.NONE,
        comment_artifact_reference_id=IDS["label"],
        comment_artifact_version="1.0.0",
        comment_artifact_digest="a" * 64,
        created_at=NOW,
        idempotency_key=IDS["review"],
        authorization_record_id=IDS["result"],
    )
    for purpose in ("prompt", "rubric", "input"):
        with pytest.raises(CalibrationError, match="artifact_unauthorized"):
            await CalibrationService(_PurposeRepository(purpose)).review(review)
    assert await CalibrationService(_PurposeRepository("comment")).review(review) is review


@pytest.mark.asyncio
async def test_qualification_requires_owner_and_calibration_then_revoke_is_advisory() -> None:
    service = CalibrationService()
    for index in range(1, 21):
        await service.label(_label(index))
    report = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    qualification = _qualification()
    with pytest.raises(CalibrationError, match="qualification_denied"):
        await service.qualify(
            qualification, report, owner_authorized=False, idempotency_key=IDS["qualification"]
        )
    assert (
        await service.qualify(
            qualification, report, owner_authorized=True, idempotency_key=IDS["qualification"]
        )
        is qualification
    )
    assert service.is_qualified(
        suite_id=IDS["suite"],
        dataset_id=IDS["dataset"],
        evaluator_id=IDS["evaluator"],
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    revoked = await service.revoke(
        IDS["qualification"], at=NOW, authorization_record_id=IDS["result"]
    )
    assert revoked.active is False
    assert not service.is_qualified(
        suite_id=IDS["suite"],
        dataset_id=IDS["dataset"],
        evaluator_id=IDS["evaluator"],
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )


@pytest.mark.asyncio
async def test_revoked_qualification_replay_returns_revoked_truth_and_revoke_is_idempotent(
) -> None:
    repository = InMemoryCalibrationRepository()
    service = CalibrationService(repository)
    for index in range(1, 21):
        await service.label(_label(index))
    report = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    qualification = _qualification()
    first = await service.qualify(
        qualification,
        report,
        owner_authorized=True,
        idempotency_key=qualification.idempotency_key or "",
    )
    revoked = await service.revoke(
        qualification.qualification_id,
        at=NOW,
        authorization_record_id=IDS["result"],
    )
    assert revoked.state == "revoked"
    assert revoked.active is False

    # Replaying the original immutable qualification cannot resurrect it.
    replay = await repository.put_qualification(first)
    assert replay.state == "revoked"
    assert replay.active is False

    repeated = await service.revoke(
        qualification.qualification_id,
        at=NOW,
        authorization_record_id=IDS["result"],
    )
    assert repeated.state == "revoked"
    assert repeated.revoked_at == revoked.revoked_at
    assert sum(audit["outcome"] == "revoked" for audit in repository.audits) == 1


def _member(
    member_id: str, score: float | None, state: JudgeAdvisoryState = JudgeAdvisoryState.CALIBRATED
) -> EnsembleMember:
    return EnsembleMember(
        member_id,
        IDS["judge"],
        "1.0.0",
        500_000,
        state,
        score,
        JudgeOutcome.PASS,
        "calibration-1",
        IDS["qualification"],
    )


def test_ensemble_is_deterministic_and_advisory_for_incomplete_members() -> None:
    qualifications = {
        IDS["member1"]: _qualification(),
        IDS["member2"]: _qualification(),
    }
    result = combine_ensemble(
        IDS["suite"],
        [_member(IDS["member1"], 0.8), _member(IDS["member2"], 0.6)],
        all_qualified=True,
        qualification_states=qualifications,
    )
    assert result.score is None
    assert result.outcome is JudgeOutcome.INCONCLUSIVE
    assert result.failure_code == "incomplete_calibration"
    assert result.advisory_state is JudgeAdvisoryState.INCONCLUSIVE

    for state, score, _outcome in [
        (JudgeAdvisoryState.ADVISORY, 0.9, JudgeOutcome.PASS),
        (JudgeAdvisoryState.CALIBRATED, None, JudgeOutcome.INCONCLUSIVE),
    ]:
        incomplete = combine_ensemble(
            IDS["suite"],
            [_member(IDS["member1"], score, state), _member(IDS["member2"], 0.6)],
            all_qualified=True,
            qualification_states=qualifications,
        )
        assert incomplete.advisory_state is JudgeAdvisoryState.INCONCLUSIVE
        assert incomplete.outcome is JudgeOutcome.INCONCLUSIVE
        assert incomplete.score is None

    missing = combine_ensemble(IDS["suite"], [], all_qualified=False)
    assert missing.failure_code == "incomplete_calibration"


@pytest.mark.asyncio
async def test_concurrent_duplicate_review_has_one_immutable_record() -> None:
    repository = InMemoryCalibrationRepository()
    service = CalibrationService(repository)
    review = HumanReview(
        IDS["review"],
        IDS["case"],
        IDS["result"],
        IDS["judge"],
        "1.0.0",
        "prompt-1",
        "rubric-1",
        "calibration-1",
        "pass",
        0.9,
        DisagreementClass.NONE,
        created_at=NOW,
        idempotency_key=IDS["review"],
        authorization_record_id=IDS["result"],
    )
    results = await asyncio.gather(*(service.review(review) for _ in range(8)))
    assert all(item == review for item in results)
    assert len(repository.reviews) == 1
    assert sum(audit["outcome"] == "accepted" for audit in repository.audits) == 1


@pytest.mark.asyncio
async def test_calibration_tolerance_is_inclusive_at_point_one_and_exclusive_above() -> None:
    inclusive = CalibrationService()
    for index in range(1, 21):
        await inclusive.label(_label(index, score=0.8, reference=0.9))
    report = await inclusive.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    assert report.agreement == 1.0
    assert report.eligible is True

    exclusive = CalibrationService()
    for index in range(1, 21):
        await exclusive.label(_label(index, score=0.7999, reference=0.9))
    report = await exclusive.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    assert report.agreement == 0.0
    assert report.eligible is False


def test_ensemble_requires_exact_integer_ppm_and_exact_versions() -> None:
    valid = [_member(IDS["member1"], 0.8), _member(IDS["member2"], 0.6)]
    qualifications = {
        IDS["member1"]: _complete_qualification(),
        IDS["member2"]: _complete_qualification(),
    }
    assert (
        combine_ensemble(
            IDS["suite"],
            valid,
            all_qualified=True,
            required_member_ids=[IDS["member1"], IDS["member2"]],
            required_versions={IDS["judge"]: ("1.0.0", "calibration-1")},
            qualification_states=qualifications,
        ).score
        == 0.7
    )
    wrong_version = EnsembleMember(
        IDS["member2"],
        IDS["judge"],
        "2.0.0",
        500_000,
        JudgeAdvisoryState.CALIBRATED,
        0.6,
        JudgeOutcome.PASS,
        "calibration-1",
        IDS["qualification"],
    )
    wrong_qualification = replace(
        qualifications[IDS["member2"]], judge_version="2.0.0"
    )
    denied = combine_ensemble(
        IDS["suite"],
        [valid[0], wrong_version],
        all_qualified=True,
        qualification_states={
            IDS["member1"]: qualifications[IDS["member1"]],
            IDS["member2"]: wrong_qualification,
        },
    )
    assert denied.score is None
    assert denied.outcome is JudgeOutcome.INCONCLUSIVE
    assert denied.advisory_state is JudgeAdvisoryState.INCONCLUSIVE
    with pytest.raises(CalibrationError, match="validation"):
        EnsembleMember(
            IDS["member1"],
            IDS["judge"],
            "1.0.0",
            0,
            JudgeAdvisoryState.CALIBRATED,
            0.8,
            JudgeOutcome.PASS,
            "calibration-1",
        ).validate()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("suite_id", "20000000-0000-0000-0000-000000000001"),
        ("suite_version", "2.0.0"),
        ("dataset_id", "20000000-0000-0000-0000-000000000002"),
        ("dataset_version", "2.0.0"),
        ("evaluator_id", "20000000-0000-0000-0000-000000000003"),
        ("evaluator_version", "2.0.0"),
        ("judge_version", "2.0.0"),
        ("prompt_version", "prompt-2"),
        ("rubric_version", "rubric-2"),
        ("calibration_version", "calibration-2"),
        ("calibration_result_id", "20000000-0000-0000-0000-000000000004"),
        ("model_name", "llama3.1:8b"),
        ("model_version", "3.1.0"),
        ("model_digest", "e" * 64),
        ("prompt_artifact_reference_id", "20000000-0000-0000-0000-000000000005"),
        ("prompt_artifact_version", "2.0.0"),
        ("prompt_artifact_digest", "f" * 64),
        ("rubric_artifact_reference_id", "20000000-0000-0000-0000-000000000006"),
        ("rubric_artifact_version", "2.0.0"),
        ("rubric_artifact_digest", "1" * 64),
        ("seed", 1),
        ("budget", {"timeoutMs": 60_000}),
    ],
)
def test_ensemble_mismatched_qualification_never_becomes_calibrated(
    field: str, value: object
) -> None:
    qualifications = {
        IDS["member1"]: _complete_qualification(),
        IDS["member2"]: _complete_qualification(),
    }
    qualifications[IDS["member1"]] = replace(
        qualifications[IDS["member1"]], **{field: value}
    )
    result = combine_ensemble(
        IDS["suite"],
        [_member(IDS["member1"], 0.8), _member(IDS["member2"], 0.6)],
        all_qualified=True,
        required_member_ids=[IDS["member1"], IDS["member2"]],
        required_versions={IDS["judge"]: ("1.0.0", "calibration-1")},
        qualification_states=qualifications,
    )
    assert result.advisory_state is JudgeAdvisoryState.INCONCLUSIVE
    assert result.outcome is JudgeOutcome.INCONCLUSIVE
    assert result.score is None


@pytest.mark.asyncio
async def test_qualification_conflict_and_revoke_are_authorized_and_audited() -> None:
    repository = InMemoryCalibrationRepository()
    service = CalibrationService(repository)
    qualification = _qualification()
    report = CalibrationReport(
        qualification.judge_id,
        qualification.judge_version,
        qualification.prompt_version,
        qualification.rubric_version,
        qualification.calibration_version,
        20,
        1.0,
        0.0,
        0.0,
        True,
        dataset_id=qualification.dataset_id,
        dataset_version=qualification.dataset_version,
        suite_id=qualification.suite_id,
        suite_version=qualification.suite_version,
        evaluator_id=qualification.evaluator_id,
        evaluator_version=qualification.evaluator_version,
        calibration_result_id=qualification.calibration_result_id,
    )
    assert (
        await service.qualify(
            qualification,
            report,
            owner_authorized=True,
            idempotency_key=IDS["qualification"],
        )
        is qualification
    )
    assert (
        await service.qualify(
            qualification,
            report,
            owner_authorized=True,
            idempotency_key=IDS["qualification"],
        )
        is qualification
    )
    conflicting = _qualification()
    object.__setattr__(conflicting, "reason", "different_reason")
    with pytest.raises(CalibrationError, match="idempotency_conflict"):
        await service.qualify(
            conflicting,
            report,
            owner_authorized=True,
            idempotency_key=IDS["qualification"],
        )
    with pytest.raises(CalibrationError, match="authorization_required"):
        await service.revoke(qualification.qualification_id, at=NOW)
    revoked = await service.revoke(
        qualification.qualification_id, at=NOW, authorization_record_id=IDS["result"]
    )
    assert revoked.state == "revoked"
    assert {entry["outcome"] for entry in repository.audits} >= {"accepted", "duplicate", "revoked"}
