"""KERNEL. Error codes and exception classes, from BUILD_SPEC section 6.

Exactly these twelve codes. Every rejection in the system uses one of them. No
ad hoc strings. The code and HTTP status are fixed per class, so a raise site
chooses a class rather than composing a code.
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


# The complete section 6 table, in specification order. The T04 CI gate walks
# this to assert all twelve code and status pairs, because the six T04 invariant
# tests only ever construct five of them and an unexercised transposed status
# would otherwise surface at T05, in a file T05 may not edit.
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
    )
}
