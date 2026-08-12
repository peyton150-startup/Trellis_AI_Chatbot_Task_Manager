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


def check(
    actor_id: UUID,
    tool_name: str,
    arguments: dict,
    target_task_ids: list[UUID],
    approval_row: Approval | None,
    *,
    run_id: UUID,
    tool_call_id: str,
) -> PolicyDecision:
    """Called inside the tool body, immediately before any mutation, on EVERY
    path including the approved one. This is the authoritative gate.

    Framework-level approval is a UI gate, not an authorization boundary. The
    authoritative record is the row in approvals, in Postgres, written by the
    server. Verifying it a second time here is deliberate defense in depth and
    must not be optimized away.
    """
    # 1. SCOPE
    owners = _load_task_owners(target_task_ids)
    for task_id in target_task_ids:
        if owners.get(task_id) != actor_id:
            # A missing row and another actor's row both land here and produce
            # the identical error. Do not distinguish them.
            raise OutOfScopeError()

    # 2. CLASSIFY
    destructive = tool_name in DESTRUCTIVE_TOOLS
    # len, not a distinct count. Four references to one id count as four and
    # fail closed. See D-17.
    count = len(target_task_ids)
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
