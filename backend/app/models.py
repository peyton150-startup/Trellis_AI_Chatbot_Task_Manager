from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from app.limits import (
    BROWSER_USER_MESSAGE_MAX_CHARS,
    BULK_TASK_IDS_MAX,
    DELETE_TASK_IDS_MAX,
    PLAN_STEP_MAX_CHARS,
    PLAN_STEPS_MAX,
    PLAN_SUMMARY_MAX_CHARS,
    TASK_NOTES_MAX_CHARS,
)


class TrellisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(str, Enum):
    OPEN = "open"
    DONE = "done"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


Priority = TaskPriority


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class EventOperation(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    RESTORED = "restored"


class LeaseStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalReason(str, Enum):
    DESTRUCTIVE = "destructive"
    BLAST_RADIUS = "blast_radius"


class ToolName(str, Enum):
    LIST_TASKS = "list_tasks"
    GET_TASK_HISTORY = "get_task_history"
    RESOLVE_TASK_REFERENCE = "resolve_task_reference"
    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    BULK_UPDATE_TASKS = "bulk_update_tasks"
    DELETE_TASKS = "delete_tasks"
    PROPOSE_PLAN = "propose_plan"


class ToolStepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    DEDUPLICATED = "deduplicated"


class LeaseAction(str, Enum):
    EXECUTE = "EXECUTE"
    REPLAY = "REPLAY"


class UndoReason(str, Enum):
    ROW_DISAPPEARED = "ROW_DISAPPEARED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    ROW_RECREATED = "ROW_RECREATED"
    # T00L, under D-27. An affected task carries diverged local integration
    # state, so someone changed the corresponding Linear issue outside this
    # system and undoing would overwrite a change this run never made. Read from
    # linear_task_state, never from the task row, which is why it survives the
    # task being deleted. Undo refuses and never clears the flag.
    EXTERNALLY_MODIFIED = "EXTERNALLY_MODIFIED"


TaskTitle = Annotated[str, Field(min_length=1, max_length=500)]

# D-74. Every free text field an actor or a model can grow without bound
# carries an explicit ceiling. The values live in `limits.py` because they are
# safety invariants rather than tunables; see that module for the argument.
TaskNotes = Annotated[str, Field(max_length=TASK_NOTES_MAX_CHARS)]
# D-78. Only the new fragment, never the whole value. The lower bound is the
# point: an empty append is a call that asks for nothing, and answering it
# with a version increment and an event would record a mutation that did not
# happen. The upper bound is a cheap early refusal; the binding check is the
# merged size, which cannot be known until the authoritative row is locked.
#
# The description is part of the JSON schema Pydantic AI generates and sends to
# the provider, so it states the append contract at the field itself rather than
# leaving it to be inferred from the tool description alone. The distinction
# then reaches the model in three mutually reinforcing places: the system
# prompt, the tool description, and this parameter schema.
AppendedTaskNotes = Annotated[
    str,
    Field(
        min_length=1,
        max_length=TASK_NOTES_MAX_CHARS,
        description=(
            "Only the new note text to append. "
            "Do not include the task's existing notes. "
            "The server preserves the existing notes and appends this fragment. "
            "Including existing note text would duplicate it."
        ),
    ),
]
UserMessageText = Annotated[str, Field(max_length=BROWSER_USER_MESSAGE_MAX_CHARS)]
PlanSummary = Annotated[str, Field(max_length=PLAN_SUMMARY_MAX_CHARS)]
PlanStep = Annotated[str, Field(max_length=PLAN_STEP_MAX_CHARS)]


class Task(TrellisModel):
    id: UUID
    owner_id: UUID
    title: TaskTitle
    notes: str = ""
    due_date: date | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.OPEN
    blocked_by: UUID | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class TaskEvent(TrellisModel):
    id: int
    task_id: UUID
    run_id: UUID | None = None
    actor_id: UUID
    operation: EventOperation
    before: JsonValue | None = None
    after: JsonValue | None = None
    created_at: datetime


class AgentRun(TrellisModel):
    id: UUID
    actor_id: UUID
    prompt: str
    status: RunStatus = RunStatus.RUNNING
    message_history: list[JsonValue] = Field(default_factory=list)
    model: str
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: Decimal = Decimal("0")
    error: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class ToolInvocation(TrellisModel):
    run_id: UUID
    tool_call_id: str
    tool_name: ToolName
    arguments_hash: str
    status: LeaseStatus = LeaseStatus.PENDING
    attempt: int = 1
    lease_expires_at: datetime
    result: JsonValue | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ApprovalPreview(TrellisModel):
    creates: list[JsonValue] = Field(default_factory=list)
    updates: list[JsonValue] = Field(default_factory=list)
    deletes: list[JsonValue] = Field(default_factory=list)


class Approval(TrellisModel):
    run_id: UUID
    tool_call_id: str
    tool_name: ToolName
    arguments: dict[str, JsonValue]
    arguments_hash: str
    required_reason: ApprovalReason
    preview: ApprovalPreview
    decision: ApprovalState = ApprovalState.PENDING
    expires_at: datetime
    decided_at: datetime | None = None


class CreateRunRequest(TrellisModel):
    user_message: UserMessageText


RunRequest = CreateRunRequest


class ApprovalDecisionRequest(TrellisModel):
    decision: ApprovalDecision


ApprovalRequest = ApprovalDecisionRequest


class TasksResponse(TrellisModel):
    tasks: list[Task]


class TaskHistoryEffect(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


class TaskHistoryChange(TrellisModel):
    field: Literal[
        "title",
        "notes",
        "due_date",
        "priority",
        "status",
        "blocked_by",
    ]
    before: JsonValue | None = None
    after: JsonValue | None = None


class TaskHistoryState(TrellisModel):
    title: TaskTitle
    notes: str
    due_date: date | None
    priority: TaskPriority
    status: TaskStatus
    blocked_by: UUID | None


class TaskHistoryEntry(TrellisModel):
    event_id: int
    operation: EventOperation
    effect: TaskHistoryEffect
    occurred_at: datetime
    version_before: int | None = None
    version_after: int | None = None
    snapshot: TaskHistoryState | None = None
    changes: list[TaskHistoryChange] = Field(default_factory=list)


class TaskHistoryResponse(TrellisModel):
    task_id: UUID
    exists_now: bool
    current_version: int | None = None
    entries: list[TaskHistoryEntry]
    next_before_event_id: int | None = None


class RunCreatedResponse(TrellisModel):
    run_id: UUID


CreateRunResponse = RunCreatedResponse


class PendingApproval(TrellisModel):
    tool_call_id: str
    tool_name: ToolName
    required_reason: ApprovalReason
    preview: ApprovalPreview
    expires_at: datetime


class RunStep(TrellisModel):
    tool_call_id: str
    tool_name: ToolName
    attempt: int
    status: ToolStepStatus
    duration_ms: int
    error: str | None = None


class RunUsage(TrellisModel):
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cost_cents: float


class RunDetail(TrellisModel):
    id: UUID
    status: RunStatus
    prompt: str
    pending_approval: PendingApproval | None = None
    steps: list[RunStep]
    usage: RunUsage
    can_undo: bool
    error: str | None = None


class ApprovalRequirement(TrellisModel):
    required: bool
    reason: ApprovalReason


class PolicyDecision(TrellisModel):
    allow: bool
    approval_required: bool
    reason: ApprovalReason | None = None


class LeaseOutcome(TrellisModel):
    action: LeaseAction
    result: JsonValue | None = None


class UndoResult(TrellisModel):
    applied: int
    refused: bool
    reason: UndoReason | None = None


class ListTasksArgs(TrellisModel):
    status: TaskStatus | None = None
    due_before: date | None = None
    due_after: date | None = None
    priority: TaskPriority | None = None
    limit: int = Field(default=50, ge=1, le=50)

    # D-77. Narrow the same bounded owner read to titles that occur more than
    # once among the rows that survive the other filters.
    #
    # A filter on the existing collection read rather than a ninth tool, because
    # this is the same question `list_tasks` already answers with a different
    # predicate, and the browser profile is exactly eight model-visible tools.
    #
    # It is not a free-text search and must not become one. Membership is
    # whole-title, case-insensitive equality computed in SQL over current `tasks`
    # rows. Reopening a general title filter here would relitigate D-73, which
    # deliberately put single-reference resolution behind
    # `resolve_task_reference` and its own bounded-query proof.
    duplicates_only: bool = False


class GetTaskHistoryArgs(TrellisModel):
    task_id: UUID
    limit: int = Field(default=20, ge=1, le=50)
    before_event_id: int | None = Field(default=None, ge=1)


def _normalized_reference(value: str) -> str:
    """Strip a task reference before it becomes an idempotency identity.

    Tool arguments are hashed after this model validates and before the tool
    body runs, so normalizing here is what makes `" Fence "` and `"Fence"` the
    same call rather than two hashes running the same search. Stripping inside
    domain would be too late.

    Case is deliberately preserved. The search is case-insensitive, but folding
    the stored reference would also merge two spellings into one idempotency
    identity, which is a wider claim than D-73 needs.
    """
    return value.strip() if isinstance(value, str) else value


# The length bound is measured after stripping, so padding cannot smuggle a
# reference past the limit and a whitespace-only reference fails `min_length`
# rather than reaching domain as an empty search.
TaskReference = Annotated[
    str,
    BeforeValidator(_normalized_reference),
    Field(min_length=1, max_length=500),
]


class ResolveTaskReferenceArgs(TrellisModel):
    reference: TaskReference
    # The floor of two is a correctness bound, not a preference. Exact matches
    # sort first, so a window of at least two always leaves a competing
    # candidate visible and truncation cannot look like uniqueness.
    limit: int = Field(default=10, ge=2, le=20)


class TaskReferenceCandidate(TrellisModel):
    task_id: UUID
    matched_title: TaskTitle
    current_title: TaskTitle | None = None
    current_version: int | None = None
    exists_now: bool


class ResolveTaskReferenceResponse(TrellisModel):
    reference: str
    # The deterministic decision, carried as the whole candidate rather than a
    # bare id. The consumer needs `exists_now` and `current_version` to act on
    # the result, and returning the id alone would make the model join it back
    # into `candidates` to find them.
    resolved: TaskReferenceCandidate | None = None
    candidates: list[TaskReferenceCandidate]


class CreateTaskArgs(TrellisModel):
    title: TaskTitle
    notes: TaskNotes = ""
    due_date: date | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    blocked_by: UUID | None = None


class MutableTaskFields(TrellisModel):
    title: TaskTitle | None = None
    notes: TaskNotes | None = None
    due_date: date | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    blocked_by: UUID | None = None


class UpdateTaskArgs(MutableTaskFields):
    task_id: UUID
    expected_version: int

    # D-78. Append lives here and deliberately not on `MutableTaskFields`.
    # `BulkUpdateTasksArgs` inherits that base, so a field added there would
    # expose bulk append to the model, and bulk append is not authorized: it
    # would need per-row merging, per-row final-size validation, and set-based
    # semantics that this decision does not provide.
    append_notes: AppendedTaskNotes | None = None

    @model_validator(mode="after")
    def _one_note_intent(self) -> "UpdateTaskArgs":
        """Replacement and append are alternatives, never a combined request.

        A call carrying both does not have one obvious meaning. Refusing it is
        cheaper than inventing an order, and it keeps the model's two verbs
        mapped onto two fields rather than onto a precedence rule nobody can
        read off the schema.
        """
        if self.append_notes is not None and "notes" in self.model_fields_set:
            raise ValueError(
                "notes replaces the whole value and append_notes adds to it; "
                "send one or the other"
            )
        return self


class BulkUpdateTasksArgs(MutableTaskFields):
    # One accepted call may not name more than this many targets. It does not
    # bound how many such calls a run may make; that is a separate dimension.
    #
    # D-79 added the lower bound. Before it, a call naming no targets validated
    # and returned an empty success, which reads to the model as "the bulk
    # update worked" when nothing was asked for and nothing happened. There is
    # no request that legitimately means "change these zero tasks", so it is a
    # malformed call and now says so.
    task_ids: list[UUID] = Field(min_length=1, max_length=BULK_TASK_IDS_MAX)

    @model_validator(mode="after")
    def _requires_a_structurally_effective_operation(self) -> "BulkUpdateTasksArgs":
        """Refuse a call carrying no operation that can reach the SET list.

        Be exact about the claim. This is a structural test, not a semantic one:
        it establishes that the patch contains at least one operation capable of
        writing a column, never that the stored value will end up different.
        Setting priority to high on a task that is already high is structurally
        effective and stays valid, as it should, because the caller asked for a
        state and the row will be in it.

        What it rejects is the patch that cannot write anything at all.
        `UPDATE_TASK_GUARDED` and its bulk counterpart apply `title`, `notes`,
        `priority`, and `status` through COALESCE, where an explicit null is
        indistinguishable from an omission, so a patch of only those nulls
        reaches the SET list and leaves every column as it found it. `due_date`
        and `blocked_by` are the two fields whose null is a value, carried by a
        set flag, so naming either of them is always an operation, including as
        a clear. `notes=""` is a real clear and stays valid.

        Without this, such a call still locked every target, still incremented
        every version, and still wrote an event per task in which before and
        after are identical. That is a version increment and an audit row
        recording a mutation that did not happen.

        This is a cross-field rule, so it lives after validation rather than in
        the JSON schema, which means the model cannot infer it from the schema
        the way it infers the `task_ids` bounds. The tool description carries it
        in words for that reason, and an invalid call is corrected through
        Pydantic AI's retry channel rather than reaching the tool body.
        """
        effective = (
            self.title is not None
            or self.notes is not None
            or self.priority is not None
            or self.status is not None
            or "due_date" in self.model_fields_set
            or "blocked_by" in self.model_fields_set
        )
        if not effective:
            raise ValueError(
                "bulk_update_tasks needs at least one field to change: send a "
                "value for title, notes, priority, or status, or send due_date "
                "or blocked_by (null clears them). Omit fields you are not "
                "changing; a null title, notes, priority, or status is read as "
                "omitted and changes nothing"
            )
        return self


class DeleteTasksArgs(TrellisModel):
    task_ids: list[UUID] = Field(max_length=DELETE_TASK_IDS_MAX)


class ProposePlanArgs(TrellisModel):
    summary: PlanSummary
    steps: list[PlanStep] = Field(max_length=PLAN_STEPS_MAX)
