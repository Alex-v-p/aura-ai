"""In-process owner/API coverage for approval-backed reminder lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from aura_core.domains.governance.approvals.public import ApprovalStatus
from aura_core.entrypoints.api.state import AppState
from fastapi import FastAPI

from conftest import owner_client


@pytest.mark.asyncio
async def test_approval_list_detail_and_decision_are_owner_scoped_and_csrf_protected(
    api_app: FastAPI,
) -> None:
    owner, session = await owner_client(api_app)
    foreign, _ = await owner_client(api_app, subject="other-owner")
    try:
        aura = cast(AppState, api_app.state.aura)
        service = aura.approval_service
        created = await service.create_pending(
            "https://authentik.dev.example",
            "owner-subject",
            content="private approval payload",
            due_at=datetime(2030, 1, 1, tzinfo=UTC),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        listed = await owner.get("/api/v1/approvals")
        assert listed.status_code == 200
        item = next(
            row
            for row in listed.json()["items"]
            if row["id"] == str(created.approval.id)
        )
        assert "proposal" not in item
        assert "content" not in item

        detail = await owner.get(f"/api/v1/approvals/{created.approval.id}")
        assert detail.status_code == 200
        assert detail.json()["proposal"]["content"] == "private approval payload"

        foreign_detail = await foreign.get(f"/api/v1/approvals/{created.approval.id}")
        assert foreign_detail.status_code == 403

        missing_csrf = await owner.post(
            f"/api/v1/approvals/{created.approval.id}/approve",
            headers={"Idempotency-Key": str(uuid4())},
            json={"expectedVersion": 1},
        )
        assert missing_csrf.status_code == 403

        approved = await owner.post(
            f"/api/v1/approvals/{created.approval.id}/approve",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={"expectedVersion": 1},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == ApprovalStatus.APPROVED.value
    finally:
        await owner.aclose()
        await foreign.aclose()
