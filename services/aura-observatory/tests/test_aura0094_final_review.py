"""Adversarial production-path checks required by the AURA-0094 final review."""
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

import pytest
from aura_observatory.adapters.evaluation_orchestration import (
    PostgresEvaluationOrchestrationRepository,
)
from aura_observatory.evaluation_calibration import (
    CalibrationError,
    CalibrationReport,
    CalibrationService,
    DisagreementClass,
    HumanReview,
    InMemoryCalibrationRepository,
)
from aura_observatory.evaluation_orchestration import (
    EvaluationOrchestrationService,
    InMemoryEvaluationOrchestrationRepository,
    LocalWorkAdmission,
    LocalWorkItem,
    OrchestrationError,
)
from test_aura0094_calibration import IDS, NOW, _label, _qualification


def _payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class _LocalQueueRepository:
    """Durable-port fake: production service is exercised, not a fake service."""

    work: LocalWorkItem | None

    def __post_init__(self) -> None:
        self.events: list[str] = []

    async def claim_local_work(self, **_: object) -> LocalWorkItem | None:
        self.events.append("claim")
        work, self.work = self.work, None
        return work

    async def complete_local_work(self, work: LocalWorkItem, **_: object) -> bool:
        self.events.append(f"complete:{work.work_id}")
        return work.lease_token == "lease"

    async def fail_local_work(self, work: LocalWorkItem, **_: object) -> bool:
        self.events.append(f"fail:{work.work_id}")
        return work.lease_token == "lease"


@pytest.mark.asyncio
async def test_production_orchestration_service_claims_completes_and_fails_through_ports() -> None:
    work = LocalWorkItem("00000000-0000-0000-0000-000000000091", "judge", "lease")
    repository = _LocalQueueRepository(work)
    service = EvaluationOrchestrationService(repository)  # type: ignore[arg-type]

    claimed = await service.claim_local_work()
    assert claimed == work
    assert claimed is not None
    await service.complete_local_work(claimed, {"outcome": "pass"})
    assert repository.events == ["claim", f"complete:{work.work_id}"]

    repository.work = work
    claimed = await service.claim_local_work()
    assert claimed == work
    assert claimed is not None
    await service.fail_local_work(claimed, "provider_timeout")
    assert repository.events[-1] == f"fail:{work.work_id}"


@pytest.mark.asyncio
async def test_production_orchestration_rejects_lease_lost_terminal_updates() -> None:
    work = LocalWorkItem("00000000-0000-0000-0000-000000000092", "judge", "wrong")
    repository = _LocalQueueRepository(work)
    service = EvaluationOrchestrationService(repository)  # type: ignore[arg-type]
    claimed = await service.claim_local_work()
    assert claimed is not None
    with pytest.raises(OrchestrationError, match="local_work_lease_lost"):
        await service.complete_local_work(claimed, None)
    with pytest.raises(OrchestrationError, match="local_work_lease_lost"):
        await service.fail_local_work(claimed, "provider_timeout")


@pytest.mark.asyncio
async def test_in_memory_local_work_enqueue_is_typed_idempotent_and_content_free() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    payload = {"request_id": IDS["result"], "case_id": IDS["case"]}
    admission = LocalWorkAdmission(
        IDS["label"], IDS["result"], IDS["review"], "judge", IDS["qualification"],
        _payload_digest(payload),
        payload, datetime.now(UTC),
    )
    assert await repository.enqueue_local_work(admission) is True
    assert await repository.enqueue_local_work(admission) is False
    assert set(repository.local_work) == {admission.work_id}
    assert not any(
        token in json.dumps(payload).casefold()
        for token in ("prompt", "response", "content")
    )

    conflicting_payload = {"request_id": IDS["result"], "case_id": IDS["review"]}
    conflicting = replace(
        admission,
        fingerprint=hashlib.sha256(
            json.dumps(conflicting_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        payload=conflicting_payload,
    )
    with pytest.raises(OrchestrationError, match="idempotency_conflict"):
        await repository.enqueue_local_work(conflicting)


@pytest.mark.asyncio
async def test_in_memory_enqueue_claim_complete_and_failure_are_terminal() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    payload = {"request_id": IDS["result"], "case_id": IDS["case"]}
    admission = LocalWorkAdmission(
        IDS["label"], IDS["result"], IDS["review"], "judge", IDS["qualification"],
        _payload_digest(payload),
        payload, datetime.now(UTC),
    )
    admission_two = replace(
        admission, work_id=IDS["member1"], idempotency_key=IDS["member2"]
    )
    await repository.enqueue_local_work(admission_two)
    service = EvaluationOrchestrationService(repository)
    claimed = await service.claim_local_work()
    assert claimed is not None
    await service.complete_local_work(claimed, {"outcome": "pass"})
    assert await service.claim_local_work() is None

    await repository.enqueue_local_work(admission)
    claimed = await service.claim_local_work()
    assert claimed is not None
    await service.fail_local_work(claimed, "provider_timeout")
    assert await service.claim_local_work() is None


@pytest.mark.asyncio
async def test_postgres_enqueue_derives_payload_from_job_evaluator_definition_and_artifact_rows(
) -> None:
    class _Result:
        def __init__(self, row: object | None = None, rows: list[object] | None = None) -> None:
            self.row, self.rows = row, rows or []
            self.rowcount = 1

        async def fetchone(self) -> object | None:
            return self.row

        async def fetchall(self) -> list[object]:
            return self.rows

    job_id, attempt_id = IDS["result"], IDS["review"]
    dataset_id, suite_id, evaluator_id = IDS["dataset"], IDS["suite"], IDS["evaluator"]
    artifact_id = IDS["member1"]
    judge_id = IDS["judge"]
    envelope = {
        "data": {
            "caseIds": [IDS["case"]],
            "inputArtifactReferences": [{"artifactReferenceId": IDS["member1"]}],
            "targetComponent": {"componentId": "aura.core", "componentVersion": "1.0.0"},
        }
    }
    definition = {
        "judgeId": judge_id,
        "judgeVersion": "1.0.0",
        "modelName": "llama3.2:3b",
        "modelVersion": "3.2.0",
        "modelDigest": "a" * 64,
        "promptVersion": "1.0.0",
        "rubricVersion": "1.0.0",
        "calibrationVersion": "1.0.0",
        "promptArtifact": {
            "artifactReferenceId": IDS["label"],
            "datasetId": dataset_id,
            "authorizationRecordId": IDS["result"],
            "sha256": "b" * 64,
            "byteCount": 1,
            "intendedUse": "prompt",
        },
        "rubricArtifact": {
            "artifactReferenceId": IDS["review"],
            "datasetId": dataset_id,
            "authorizationRecordId": IDS["result"],
            "sha256": "c" * 64,
            "byteCount": 1,
            "intendedUse": "rubric",
        },
        "labelIds": ["pass", "fail", "inconclusive"],
        "seed": 0,
        "temperature": 0.0,
        "maxOutputBytes": 8192,
        "maxOutputTokens": 2048,
        "maxInputBytes": 1048576,
        "contextTokens": 4096,
        "maxConcurrency": 1,
        "metric": "evaluation.local_judge.score",
    }
    evaluator = {
        "kind": "local_judge",
        "judgeDefinition": {
            "judgeDefinitionId": judge_id,
            "judgeDefinitionVersion": "1.0.0",
        },
    }
    input_row = (
        IDS["member1"],
        dataset_id,
        IDS["result"],
        "d" * 64,
        4,
        "text/plain",
        "full_content",
        "evaluation_input",
        "available",
        "input",
        "1.0.0",
        "owner_supplied_evaluation",
        "controlled_evaluation",
        "manual_only",
        None,
        job_id,
        suite_id,
    )
    prompt_row = (
        IDS["label"], dataset_id, IDS["result"], "b" * 64, 1, "text/plain",
        "full_content", "evaluation_input", "available", "prompt", "1.0.0",
        "owner_supplied_evaluation", "controlled_evaluation", "manual_only", None,
        None, None,
    )
    rubric_row = (
        IDS["review"], dataset_id, IDS["result"], "c" * 64, 1, "text/plain",
        "full_content", "evaluation_input", "available", "rubric", "1.0.0",
        "owner_supplied_evaluation", "controlled_evaluation", "manual_only", None,
        None, None,
    )
    artifact_rows: list[object] = [input_row, prompt_row, rubric_row]
    inserted_payloads: list[dict[str, object]] = []

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, query: str, params: tuple[object, ...]) -> _Result:
            if query.lstrip().startswith("SELECT j.job_id"):
                return _Result(
                    (
                        job_id,
                        IDS["result"],
                        IDS["qualification"],
                        dataset_id,
                        suite_id,
                        evaluator_id,
                        "1.0.0",
                        envelope,
                        attempt_id,
                        "local_judge",
                        evaluator,
                    )
                )
            if "SELECT definition_json" in query:
                return _Result((definition,))
            if "SELECT a.artifact_reference_id" in query:
                return _Result(rows=artifact_rows)
            if query.lstrip().startswith("INSERT INTO observatory_evaluation_local_work"):
                inserted_payloads.append(json.loads(str(params[6])))
                return _Result((str(params[0]),))
            raise AssertionError(f"unexpected query: {query[:80]}")

    # The adapter expects an async transaction context nested under the connection.
    class _FactoryConnection(_Connection):
        def transaction(self) -> _Transaction:
            return _Transaction()

    class _Transaction:
        async def __aenter__(self) -> _Transaction:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, query: str, params: tuple[object, ...]) -> _Result:
            return await connection.execute(query, params)

    connection = _Connection()
    repository = PostgresEvaluationOrchestrationRepository(lambda: _FactoryConnection())
    inserted = await repository.enqueue_admitted_local_work(job_id=job_id, now=datetime.now(UTC))
    assert inserted == 1
    assert len(inserted_payloads) == 1
    admitted = inserted_payloads[0]
    assert admitted["job_id"] == job_id
    admitted_definition = cast(Mapping[str, object], admitted["definition"])
    admitted_inputs = cast(list[Mapping[str, object]], admitted["input_artifacts"])
    assert admitted_definition["judge_id"] == judge_id
    assert admitted_inputs[0]["artifact_reference_id"] == artifact_id
    assert not any(
        token in json.dumps(admitted).casefold()
        for token in ("prompt bytes", "response", "credential")
    )

    # Missing joins are rejected before any local work row is inserted.
    artifact_rows.pop()
    assert await repository.enqueue_admitted_local_work(
        job_id=job_id, now=datetime.now(UTC)
    ) == 0

    mismatched_input = (*input_row[:15], IDS["member2"], suite_id)
    artifact_rows[:] = [mismatched_input, prompt_row, rubric_row]
    with pytest.raises(OrchestrationError, match="local_work_artifact_invalid"):
        await repository.enqueue_admitted_local_work(
            job_id=job_id, now=datetime.now(UTC)
        )


class _ExactAuthorizationRepository(InMemoryCalibrationRepository):
    def __init__(self, allowed: set[tuple[str, str, str | None, str | None]]) -> None:
        super().__init__()
        self.allowed = allowed
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.comment_refs: set[str] = set()

    async def is_authorized(
        self,
        record_id: str,
        action: str,
        *,
        resource_id: str | None = None,
        scope_fingerprint: str | None = None,
    ) -> bool:
        call = (record_id, action, resource_id, scope_fingerprint)
        self.calls.append(call)
        return call in self.allowed

    async def is_comment_artifact_authorized(self, artifact_reference_id: str) -> bool:
        return artifact_reference_id in self.comment_refs


def _review() -> HumanReview:
    return HumanReview(
        IDS["review"], IDS["case"], IDS["result"], IDS["judge"], "1.0.0",
        "prompt-1", "rubric-1", "calibration-1", "pass", 0.9,
        DisagreementClass.NONE,
        created_at=NOW, idempotency_key=IDS["review"], authorization_record_id=IDS["result"],
        comment_artifact_reference_id=IDS["label"],
    )


@pytest.mark.asyncio
async def test_calibration_actions_require_exact_owner_purpose_resource_and_scope() -> None:
    label = _label(401)
    review = _review()
    qualification = _qualification()
    report = CalibrationReport(
        IDS["judge"], "1.0.0", "prompt-1", "rubric-1", "calibration-1", 20,
        0.9, 0.1, 0.1, True, dataset_id=IDS["dataset"], dataset_version="1.0.0",
        suite_id=IDS["suite"], suite_version="1.0.0", evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0", calibration_result_id=IDS["result"],
    )
    # Build exact authorization tuples from the service's opaque scope digest.
    probe = _ExactAuthorizationRepository(set())
    probe_service = CalibrationService(probe)
    with pytest.raises(CalibrationError, match="authorization_required"):
        await probe_service.label(label)
    label_scope = probe.calls[-1][3]
    probe.calls.clear()
    with pytest.raises(CalibrationError, match="authorization_required"):
        await probe_service.review(review)
    review_scope = probe.calls[-1][3]
    probe.calls.clear()
    with pytest.raises(CalibrationError, match="qualification_denied"):
        await probe_service.qualify(
            qualification,
            report,
            owner_authorized=True,
            idempotency_key=qualification.idempotency_key or "",
        )
    qualify_scope = probe.calls[-1][3]

    allowed: set[tuple[str, str, str | None, str | None]] = {
        (IDS["result"], "calibration_label", label.label_id, label_scope),
        (IDS["result"], "human_review", review.review_id, review_scope),
        (IDS["result"], "qualification", qualification.qualification_id, qualify_scope),
        (IDS["result"], "revocation", qualification.qualification_id, None),
    }
    repository = _ExactAuthorizationRepository(allowed)
    repository.comment_refs.add(IDS["label"])
    service = CalibrationService(repository)
    assert await service.label(label) is label
    assert await service.review(review) is review
    assert (
        await service.qualify(
            qualification,
            report,
            owner_authorized=True,
            idempotency_key=qualification.idempotency_key or "",
        )
        is qualification
    )
    revoked = await service.revoke(
        qualification.qualification_id,
        at=NOW,
        authorization_record_id=IDS["result"],
    )
    assert revoked.active is False

    # Same record with a different purpose/resource/scope never authorizes an action.
    denied = _ExactAuthorizationRepository(
        {(IDS["result"], "human_review", review.review_id, review_scope)}
    )
    denied_service = CalibrationService(denied)
    with pytest.raises(CalibrationError, match="authorization_required"):
        await denied_service.label(label)
    assert denied.labels == {}
    with pytest.raises(CalibrationError, match="artifact_unauthorized"):
        await denied_service.review(review)
    assert denied.reviews == {}


@pytest.mark.asyncio
async def test_revocation_zero_row_is_rejected_without_acceptance_audit() -> None:
    class _NoRowRepository(_ExactAuthorizationRepository):
        async def revoke_qualification(self, *_: object, **__: object) -> None:
            return None

    qualification = _qualification()
    repository = _NoRowRepository(
        {(IDS["result"], "revocation", qualification.qualification_id, None)}
    )
    repository.qualifications[qualification.qualification_id] = qualification
    service = CalibrationService(repository)
    with pytest.raises(CalibrationError, match="not_found"):
        await service.revoke(
            qualification.qualification_id,
            at=NOW,
            authorization_record_id=IDS["result"],
        )
    assert not any(a.get("outcome") == "revoked" for a in repository.audits)
