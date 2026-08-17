"""KERNEL. Error codes and exception classes, from BUILD_SPEC section 6.

Exactly these fourteen codes. Every rejection in the system uses one of them. No
ad hoc strings. The code and HTTP status are fixed per class, so a raise site
chooses a class rather than composing a code.

The table closed at twelve from T04 until T12B added `RUN_STATE_INVALID` under
D-55, which is why several older comments in this repository say twelve, and
T00L added `EXTERNAL_DIVERGENCE` under D-27, which is why several say thirteen.
Adding a fifteenth is a KERNEL edit and trips D-31 the same way both of those
did.
"""


class PolicyError(Exception):
    """Base class for every rejection.

    Subclasses set code and http_status. The base defines neither, so raising it
    directly fails loudly rather than emitting an unclassified rejection.
    """

    code: str
    http_status: int

    def __init__(self, message: str | None = None) -> None:
        self.message = message if message is not None else self.code
        super().__init__(self.message)


class OutOfScopeError(PolicyError):
    """Any target task is missing or has owner_id != actor_id.

    Missing and not-yours are deliberately indistinguishable. See section 6
    step 1 and section 14.
    """

    code = "OUT_OF_SCOPE"
    http_status = 403


class ApprovalRequiredError(PolicyError):
    """Operation is destructive or over the blast radius threshold.

    Raised at section 6 step 4 when no approval row exists, and again at step 5d
    when the stored decision is still pending. Not interchangeable with
    ApprovalNotFoundError. See D-15.
    """

    code = "APPROVAL_REQUIRED"
    http_status = 202


class ApprovalNotFoundError(PolicyError):
    """No pending approval row for this run and tool_call_id.

    Section 6 step 5a only. This is defense against a caller passing the wrong
    approval row, not the primary protection against a forged approval, which is
    step 4 and the hash check at step 5b. See D-15.
    """

    code = "APPROVAL_NOT_FOUND"
    http_status = 403


class ApprovalMismatchError(PolicyError):
    """Stored arguments_hash differs from the call's hash."""

    code = "APPROVAL_MISMATCH"
    http_status = 403


class ApprovalExpiredError(PolicyError):
    """expires_at < now()."""

    code = "APPROVAL_EXPIRED"
    http_status = 403


class ApprovalAlreadyDecidedError(PolicyError):
    """decision != 'pending'."""

    code = "APPROVAL_ALREADY_DECIDED"
    http_status = 409


class IdempotencyConflictError(PolicyError):
    """Same key, different arguments_hash."""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class LeaseInFlightError(PolicyError):
    """Same key, status pending, poll exhausted."""

    code = "LEASE_IN_FLIGHT"
    http_status = 409


class VersionConflictError(PolicyError):
    """Guarded update returned zero rows."""

    code = "VERSION_CONFLICT"
    http_status = 409


class ToolTimeoutError(PolicyError):
    """Tool exceeded TOOL_TIMEOUT_SECONDS."""

    code = "TOOL_TIMEOUT"
    http_status = 504


class ModelTimeoutError(PolicyError):
    """Model call exceeded MODEL_TIMEOUT_SECONDS."""

    code = "MODEL_TIMEOUT"
    http_status = 504


class ValidationFailedError(PolicyError):
    """Pydantic schema rejection."""

    code = "VALIDATION_ERROR"
    http_status = 422


class RunStateInvalidError(PolicyError):
    """A resolved, actor-owned run whose current status forbids the action.

    The thirteenth code, added at T12B under D-55. D-45 recorded that none of the
    original twelve expressed this and that VALIDATION_ERROR, OUT_OF_SCOPE, and
    the approval-specific codes must not be bent to fit.

    Reached only after ownership has already resolved, and that ordering is the
    point. A run that does not exist and a run belonging to another actor both
    stay OutOfScopeError, so this class never distinguishes them and the
    non-enumeration property section 6 step 1 establishes for tasks is unchanged
    for runs. Raising it before an ownership check would leak which run ids are
    real.
    """

    code = "RUN_STATE_INVALID"
    http_status = 409


class ExternalDivergenceError(PolicyError):
    """A target task was modified in Linear outside this system.

    The fourteenth code, added at T00L under D-27. Raised at section 6 step 1b,
    after SCOPE and before CLASSIFY, when any target task carries a
    `linear_task_state` row with `diverged = true`.

    409 rather than 403, because this is a concurrency conflict in the same
    family as `VERSION_CONFLICT`, and not an authorization result. The actor is
    entitled to the row; the row is contested.

    The ordering is the same non-enumeration rule `RunStateInvalidError` records
    for runs. Reached only after ownership has resolved, so a missing task and
    another actor's task both stay `OutOfScopeError` whatever their integration
    state says. Raising this before the scope check would turn local integration
    bookkeeping into an oracle for which task ids exist.
    """

    code = "EXTERNAL_DIVERGENCE"
    http_status = 409


# The complete section 6 table, in specification order. The T04 CI gate walks
# this to assert all fourteen code and status pairs, because the six T04
# invariant tests only ever construct five of them and an unexercised transposed
# status would otherwise surface at T05, in a file T05 may not edit. T12B added
# the thirteenth entry and updated that gate in the same commit, under D-55, and
# T00L added the fourteenth the same way under D-27.
ERRORS_BY_CODE = {
    error.code: error
    for error in (
        OutOfScopeError,
        ApprovalRequiredError,
        ApprovalNotFoundError,
        ApprovalMismatchError,
        ApprovalExpiredError,
        ApprovalAlreadyDecidedError,
        IdempotencyConflictError,
        LeaseInFlightError,
        VersionConflictError,
        ToolTimeoutError,
        ModelTimeoutError,
        ValidationFailedError,
        RunStateInvalidError,
        ExternalDivergenceError,
    )
}
