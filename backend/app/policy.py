"""KERNEL. Actor scope, blast radius, and approval requirement.

Transcribed from BUILD_SPEC section 6. The check order below is the
specification, not a style choice: a reordering that looks equivalent leaks the
existence of other actors' rows, because an approval gate reached before the
scope check answers a question about ids the actor does not own.

D-15 adds run_id and tool_call_id as required keyword arguments to check. Step
5a compares the approval row against the current call, and the signature printed
in section 6 and the call site printed in section 10 pass neither value, so step
5a is unimplementable without them.

D-17 covers the scope load: SELECT_TASK_OWNERS, the explicit skip on an empty
target list, and the lazy pool import.

D-27 adds step 1b, DIVERGENCE, between SCOPE and CLASSIFY. It reads local
integration state only. No step of this module calls Linear or depends on Linear
being reachable.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from . import sql
from .config import settings
from .errors import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalMismatchError,
    ApprovalNotFoundError,
    ApprovalRequiredError,
    ExternalDivergenceError,
    OutOfScopeError,
)
from .models import Approval, ApprovalReason, ApprovalRequirement, ApprovalState, PolicyDecision


DESTRUCTIVE_TOOLS = frozenset({"delete_tasks"})


def arguments_hash(arguments: dict) -> str:
    """The one definition of an argument hash. Nothing else computes one."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def classify(
    tool_name: str,
    arguments: dict,
    target_count: int,
) -> ApprovalRequirement:
    """Called when the tool is proposed, and when deciding whether a tool
    registers as approval-required. Pure, no database, no actor check.

    Under D-12 this is also step 0 of every mutating tool body, ahead of
    arguments_hash and ahead of idempotency.acquire, which is why it must stay
    importable without a live database.
    """
    destructive = tool_name in DESTRUCTIVE_TOOLS
    over_blast = target_count > settings.blast_radius_threshold
    required = destructive or over_blast
    reason = ApprovalReason.DESTRUCTIVE if destructive else ApprovalReason.BLAST_RADIUS
    return ApprovalRequirement(required=required, reason=reason)


def resolve_scope(actor_id: UUID, target_task_ids: list[UUID]) -> None:
    """Step 1 of `check`, exposed so a tool body can run it on its own.

    D-12's closing paragraph requires actor scope to be resolved before the
    conditional `ApprovalRequired` raise, never after, and then supplies no
    mechanism for doing so: it prints an immediate classify-and-raise, keeps the
    owner load private, and states that `policy.py` needs no change. Those three
    cannot all hold. D-50 resolves the contradiction by publishing the scope
    check rather than by weakening the ordering requirement.

    This is a function rather than a copy of the loop inside `check` because two
    spellings of the scope rule would drift, and the rule is the one place where
    a divergence is an authorization bug rather than an inconsistency. `check`
    calls it too, so there is exactly one definition.

    Raising here does not make `check`'s own step 1 redundant. `check` remains
    the authoritative gate and runs on every path including the approved
    continuation, where scope may have moved since the pre-raise call. See D-06.
    """
    owners = _load_task_owners(target_task_ids)
    for task_id in target_task_ids:
        if owners.get(task_id) != actor_id:
            # A missing row and another actor's row both land here and produce
            # the identical error. Do not distinguish them.
            raise OutOfScopeError()


def check(
    actor_id: UUID,
    tool_name: str,
    arguments: dict,
    target_task_ids: list[UUID],
    approval_row: Approval | None,
    *,
    run_id: UUID,
    tool_call_id: str,
    blast_radius_count: int | None = None,
) -> PolicyDecision:
    """Called inside the tool body, immediately before any mutation, on EVERY
    path including the approved one. This is the authoritative gate.

    Framework-level approval is a UI gate, not an authorization boundary. The
    authoritative record is the row in approvals, in Postgres, written by the
    server. Verifying it a second time here is deliberate defense in depth and
    must not be optimized away.
    """
    # 1. SCOPE
    resolve_scope(actor_id, target_task_ids)

    # 1b. DIVERGENCE. T00L, under D-27. The position is specified, not
    # incidental, and moving it in either direction breaks something.
    #
    # After SCOPE, because refusing on a row the actor does not own would
    # disclose that the row exists. That is the same leak the check-order note
    # at the top of this module warns about, and local integration bookkeeping
    # must never become an oracle for which task ids are real. A foreign
    # diverged task and a missing one both raise OutOfScopeError above and never
    # reach this line.
    #
    # Before CLASSIFY, because divergence refuses whether or not the operation
    # would have required approval. Running it later would build an approval
    # card, and on the AG-UI path prompt a human, for a mutation already decided
    # to be refused.
    #
    # This is the point at which fresh work is denied mutation authority: check
    # is called inside the tool body immediately before any mutation, on every
    # path, so a raise here means no business write and no task_events row.
    _refuse_if_diverged(target_task_ids)

    # 2. CLASSIFY
    destructive = tool_name in DESTRUCTIVE_TOOLS
    # len, not a distinct count. Four references to one id count as four and
    # fail closed. See D-17.
    #
    # Scope and blast radius are two different questions asked of two different
    # lists, and conflating them inflates the approval gate. A tool may need an
    # id checked for ownership without that id being something the call mutates:
    # `create_task` and `bulk_update_tasks` both resolve `blocked_by`, which
    # names a row they point at rather than a row they change. Counting it made
    # three ids plus one blocker cross a threshold of three, gating a call whose
    # contract says the gate depends on `len(task_ids)`.
    #
    # The default preserves the original behavior exactly, and it defaults in the
    # conservative direction rather than the permissive one: a caller that
    # forgets it over-counts and gates too often, which fails closed. That is why
    # this is optional where D-15's run_id and D-18's conn are required. Those
    # two fail open when omitted; this one does not.
    count = (
        len(target_task_ids) if blast_radius_count is None else blast_radius_count
    )
    over_blast = count > settings.blast_radius_threshold
    requires_approval = destructive or over_blast
    reason = ApprovalReason.DESTRUCTIVE if destructive else ApprovalReason.BLAST_RADIUS

    # 3.
    if not requires_approval:
        return PolicyDecision(allow=True, approval_required=False)

    # 4.
    if approval_row is None:
        # AG-UI path: should not reach here, because the framework gated the
        # call earlier. If it does, something bypassed the gate and failing
        # closed is correct. Direct and test paths: the caller creates the
        # approval row and pauses.
        raise ApprovalRequiredError()

    # 5. VERIFY APPROVAL, in this order.
    if approval_row.run_id != run_id or approval_row.tool_call_id != tool_call_id:
        raise ApprovalNotFoundError()

    if approval_row.arguments_hash != arguments_hash(arguments):
        raise ApprovalMismatchError()

    if approval_row.expires_at <= _now():
        raise ApprovalExpiredError()

    if approval_row.decision != ApprovalState.APPROVED:
        if approval_row.decision == ApprovalState.PENDING:
            raise ApprovalRequiredError()
        raise ApprovalAlreadyDecidedError()

    # 6.
    return PolicyDecision(allow=True, approval_required=True, reason=reason)


def _refuse_if_diverged(task_ids: list[UUID]) -> None:
    """Step 1b of `check`. Call only after scope has resolved.

    Not exposed the way `resolve_scope` is. D-50 published the scope check
    because D-12 requires a tool body to run it before the conditional
    ApprovalRequired raise; nothing needs divergence that early, and a second
    public entry point would invite a call site that ran it before proving
    ownership, which is the one ordering this step must not have.
    """
    if not task_ids:
        return

    # Lazy, for the reason given in _load_task_owners.
    from .db import pool

    with pool.connection() as conn:
        rows = conn.execute(
            sql.SELECT_DIVERGED_TASK_IDS, {"task_ids": list(task_ids)}
        ).fetchall()

    if rows:
        raise ExternalDivergenceError()


def _load_task_owners(task_ids: list[UUID]) -> dict[UUID, UUID]:
    """Load owner_id for every id in task_ids.

    An empty target list skips the query and satisfies scope. Tools such as
    create_task and propose_plan have no targets, and the comparison above would
    be vacuously true, so the skip is explicit rather than accidental.
    """
    if not task_ids:
        return {}

    # Imported here, not at module scope. db.py opens its ConnectionPool at
    # import time, so a module-level import would make importing policy require
    # a live database, including for classify and arguments_hash, which section
    # 6 calls pure and which T10 imports for step 0 of every tool body.
    from .db import pool

    with pool.connection() as conn:
        rows = conn.execute(
            sql.SELECT_TASK_OWNERS, {"task_ids": list(task_ids)}
        ).fetchall()
    return {row["id"]: row["owner_id"] for row in rows}


def _now() -> datetime:
    return datetime.now(timezone.utc)
