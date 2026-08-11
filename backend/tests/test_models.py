import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalReason,
    ApprovalRequirement,
    BulkUpdateTasksArgs,
    CreateRunRequest,
    CreateTaskArgs,
    DeleteTasksArgs,
    LeaseAction,
    LeaseOutcome,
    ListTasksArgs,
    PolicyDecision,
    Priority,
    ProposePlanArgs,
    RunCreatedResponse,
    RunDetail,
    RunStatus,
    Task,
    TaskEvent,
    TaskStatus,
    TasksResponse,
    UndoReason,
    UndoResult,
    UpdateTaskArgs,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
TASK_ID = UUID("00000000-0000-0000-0000-000000000101")
RUN_ID = UUID("00000000-0000-0000-0000-000000000201")
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": TASK_ID,
        "owner_id": ACTOR_ID,
        "title": "Task A",
        "notes": "interview",
        "due_date": date(2026, 8, 17),
        "priority": "critical",
        "status": "open",
        "blocked_by": None,
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


def run_detail_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "awaiting_approval",
        "prompt": "Delete completed tasks",
        "pending_approval": {
            "tool_call_id": "call-1",
            "tool_name": "delete_tasks",
            "required_reason": "destructive",
            "preview": {"creates": [], "updates": [], "deletes": []},
            "expires_at": "2026-08-11T12:05:00Z",
        },
        "steps": [
            {
                "tool_call_id": "call-1",
                "tool_name": "delete_tasks",
                "attempt": 1,
                "status": "deduplicated",
                "duration_ms": 0,
                "error": None,
            }
        ],
        "usage": {
            "model_calls": 1,
            "tool_calls": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cost_cents": 0.125,
        },
        "can_undo": True,
        "error": None,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("length", [0, 501])
def test_task_rejects_titles_outside_documented_bounds(length: int) -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(task_payload(title="x" * length))


def test_task_parses_schema_enums_and_temporal_values() -> None:
    task = Task.model_validate(task_payload())

    assert task.priority is Priority.CRITICAL
    assert task.status is TaskStatus.OPEN
    assert task.due_date == date(2026, 8, 17)
    assert task.id == TASK_ID


def test_task_event_accepts_partial_json_snapshots() -> None:
    event = TaskEvent(
        id=1,
        task_id=TASK_ID,
        run_id=RUN_ID,
        actor_id=ACTOR_ID,
        operation="updated",
        before={"version": 1},
        after={"version": 2},
        created_at=NOW,
    )

    assert event.before == {"version": 1}
    assert event.after == {"version": 2}


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CreateRunRequest, {"user_message": "Create Task A"}),
        (ApprovalDecisionRequest, {"decision": "approved"}),
    ],
)
def test_http_request_models_reject_extra_keys(
    model: type[CreateRunRequest] | type[ApprovalDecisionRequest],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "message_history": []})


def test_approval_request_rejects_pending_decision() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(decision="pending")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (ListTasksArgs, {}),
        (CreateTaskArgs, {"title": "Task A"}),
        (
            UpdateTaskArgs,
            {"task_id": str(TASK_ID), "expected_version": 1},
        ),
        (BulkUpdateTasksArgs, {"task_ids": [str(TASK_ID)]}),
        (DeleteTasksArgs, {"task_ids": [str(TASK_ID)]}),
        (ProposePlanArgs, {"summary": "Prepare", "steps": ["Review tasks"]}),
    ],
)
def test_each_tool_argument_model_rejects_extra_keys(
    model: type[object], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "free_form": {"anything": True}})


def test_list_tasks_enforces_limit_ceiling() -> None:
    assert ListTasksArgs(limit=50).limit == 50

    with pytest.raises(ValidationError):
        ListTasksArgs(limit=51)


def test_list_tasks_rejects_free_text_filter() -> None:
    with pytest.raises(ValidationError):
        ListTasksArgs.model_validate({"filter": "title like '%Task%'"})


def test_optional_tool_enums_are_closed() -> None:
    assert ListTasksArgs(status="done", priority="high").status is TaskStatus.DONE
    assert CreateTaskArgs(title="Task A", priority="low").priority is Priority.LOW
    assert UpdateTaskArgs(
        task_id=TASK_ID, expected_version=1, status="open", priority="medium"
    ).status is TaskStatus.OPEN
    assert BulkUpdateTasksArgs(
        task_ids=[TASK_ID], status="done", priority="critical"
    ).priority is Priority.CRITICAL

    with pytest.raises(ValidationError):
        ListTasksArgs(status="archived")


def test_tool_arguments_do_not_accept_arbitrary_json() -> None:
    with pytest.raises(ValidationError):
        CreateTaskArgs.model_validate({"title": "Task A", "notes": {"raw": True}})

    with pytest.raises(ValidationError):
        ProposePlanArgs.model_validate(
            {"summary": "Prepare", "steps": [{"instruction": "delete all"}]}
        )


def test_update_arguments_preserve_explicit_null_for_nullable_fields() -> None:
    args = UpdateTaskArgs.model_validate(
        {
            "task_id": TASK_ID,
            "expected_version": 1,
            "due_date": None,
            "blocked_by": None,
        }
    )

    assert {"due_date", "blocked_by"}.issubset(args.model_fields_set)


def test_http_response_models_match_documented_shapes() -> None:
    task = Task.model_validate(task_payload())
    tasks_response = TasksResponse(tasks=[task])
    created_response = RunCreatedResponse(run_id=RUN_ID)

    assert tasks_response.tasks == [task]
    assert created_response.run_id == RUN_ID


def test_run_detail_parses_wire_enums_and_usage() -> None:
    detail = RunDetail.model_validate(run_detail_payload())

    assert detail.status is RunStatus.AWAITING_APPROVAL
    assert detail.pending_approval is not None
    assert detail.pending_approval.required_reason is ApprovalReason.DESTRUCTIVE
    assert detail.steps[0].status.value == "deduplicated"
    assert detail.usage.cost_cents == Decimal("0.125")


def test_run_usage_serializes_cost_cents_as_json_number() -> None:
    detail = RunDetail.model_validate(run_detail_payload())

    payload = json.loads(detail.model_dump_json())
    assert isinstance(payload["usage"]["cost_cents"], float)


def test_run_detail_rejects_unknown_wire_status() -> None:
    with pytest.raises(ValidationError):
        RunDetail.model_validate(run_detail_payload(status="paused"))


def test_policy_lease_and_undo_results_use_closed_outcomes() -> None:
    requirement = ApprovalRequirement(
        required=True, reason=ApprovalReason.BLAST_RADIUS
    )
    decision = PolicyDecision(
        allow=True,
        approval_required=True,
        reason=ApprovalReason.BLAST_RADIUS,
    )
    lease = LeaseOutcome(action=LeaseAction.REPLAY, result={"tasks": []})
    undo = UndoResult(applied=0, refused=True, reason=UndoReason.VERSION_CONFLICT)

    assert requirement.reason is ApprovalReason.BLAST_RADIUS
    assert decision.reason is ApprovalReason.BLAST_RADIUS
    assert lease.action is LeaseAction.REPLAY
    assert undo.reason is UndoReason.VERSION_CONFLICT


def test_policy_result_rejects_undocumented_reason() -> None:
    with pytest.raises(ValidationError):
        PolicyDecision(allow=False, approval_required=True, reason="admin_override")


def test_approval_requirement_requires_classification_reason() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequirement(required=False)


def test_undo_result_rejects_undocumented_reason() -> None:
    with pytest.raises(ValidationError):
        UndoResult(applied=0, refused=True, reason="PARTIAL_UNDO")


def test_decision_enum_excludes_pending_state() -> None:
    assert ApprovalDecision.APPROVED.value == "approved"
    assert ApprovalDecision.DENIED.value == "denied"
    assert {decision.value for decision in ApprovalDecision} == {"approved", "denied"}
