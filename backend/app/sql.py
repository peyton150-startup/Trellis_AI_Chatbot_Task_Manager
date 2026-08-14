SELECT_TASKS_FOR_OWNER = """
SELECT *
  FROM tasks
 WHERE owner_id = %(owner_id)s
   AND (%(status)s::task_status IS NULL OR status = %(status)s::task_status)
   AND (%(due_before)s::date IS NULL OR due_date <= %(due_before)s::date)
   AND (%(due_after)s::date IS NULL OR due_date >= %(due_after)s::date)
   AND (%(priority)s::task_priority IS NULL OR priority = %(priority)s::task_priority)
 ORDER BY due_date ASC NULLS LAST, created_at ASC, id ASC
 LIMIT %(limit)s;
"""

# The scope load for BUILD_SPEC section 6 step 1, added at T04 under D-17.
# Section 5 lists no statement that loads owners by a set of task ids, and
# SELECT_TASKS_FOR_OWNER cannot serve because it filters by owner_id, has no id
# filter, and carries a LIMIT. A missing id returns no row and a foreign id
# returns a non-matching owner_id, so both fail the same comparison and produce
# the identical OUT_OF_SCOPE that section 6 requires.
SELECT_TASK_OWNERS = """
SELECT id, owner_id
  FROM tasks
 WHERE id = ANY(%(task_ids)s::uuid[]);
"""

# T06 needs the complete pre-mutation rows for task_events snapshots. Loading
# them under FOR UPDATE on the caller's connection keeps the snapshot and the
# following guarded mutation in one transaction.
#
# ORDER BY id, not the caller's array order. FOR UPDATE locks rows in the order
# the sort produces, so ordering by the request would let one caller lock A then
# B while a concurrent caller passing the same ids reversed locks B then A. That
# is a lock cycle and PostgreSQL resolves it by aborting one side with
# DeadlockDetected. A canonical order every caller shares cannot cycle. Nothing
# downstream reads this row order: domain keys the result by id and replays the
# request order itself.
SELECT_TASKS_BY_IDS_FOR_UPDATE = """
SELECT *
  FROM tasks
 WHERE owner_id = %(owner_id)s
   AND id = ANY(%(task_ids)s::uuid[])
 ORDER BY id
 FOR UPDATE;
"""

# tasks.blocked_by is a self reference declared ON DELETE SET NULL, so deleting
# a task silently rewrites every surviving row that pointed at it. That write
# never passes through domain, so without this statement it produces no
# task_events row, and an unaudited mutation is one undo cannot reverse and the
# audit log cannot explain. This loads and locks those rows before the delete so
# their pre-cascade state can be snapshotted.
#
# Rows already inside the delete set are excluded: they get their own deleted
# event carrying the same information. The owner filter is deliberate and its
# residue is recorded as a limitation, because blocked_by is not owner scoped
# and narrowing it would require a schema change section 4 forbids.
SELECT_TASKS_BLOCKED_BY_IDS = """
SELECT *
  FROM tasks
 WHERE owner_id = %(owner_id)s
   AND blocked_by = ANY(%(task_ids)s::uuid[])
   AND NOT (id = ANY(%(task_ids)s::uuid[]))
 ORDER BY id
 FOR UPDATE;
"""

INSERT_TASK = """
INSERT INTO tasks (owner_id, title, notes, due_date, priority, blocked_by)
VALUES (
  %(owner_id)s,
  %(title)s,
  %(notes)s,
  %(due_date)s,
  %(priority)s,
  %(blocked_by)s
)
RETURNING *;
"""

UPDATE_TASK_GUARDED = """
UPDATE tasks
   SET title = COALESCE(%(title)s, title),
       notes = COALESCE(%(notes)s, notes),
       due_date = CASE WHEN %(set_due_date)s THEN %(due_date)s ELSE due_date END,
       priority = COALESCE(%(priority)s, priority),
       status = COALESCE(%(status)s, status),
       blocked_by = CASE WHEN %(set_blocked_by)s THEN %(blocked_by)s ELSE blocked_by END,
       version = version + 1,
       updated_at = now()
 WHERE id = %(id)s
   AND owner_id = %(owner_id)s
   AND version = %(expected_version)s
RETURNING *;
"""

DELETE_TASKS_BY_IDS = """
DELETE FROM tasks
 WHERE owner_id = %(owner_id)s
   AND id = ANY(%(task_ids)s::uuid[])
RETURNING *;
"""

# The two T07 compensation writes, added under D-39. Section 5 lists neither,
# and neither existing statement can be adapted to.
#
# DELETE_TASKS_BY_IDS carries no version predicate, which is safe for the tool
# path because policy.check and the domain lock run immediately before it inside
# one transaction. It is not safe for undo: the precheck that established the
# version is a separate pass, and without a predicate here a concurrent write
# landing in between would be deleted rather than refused, while undo reported
# success. Of undo's three compensations that is the only one where nothing else
# could refuse, because an update is guarded by version and a restore by the
# primary key. Zero rows deleted therefore means the row moved or vanished; undo
# reports VERSION_CONFLICT for both and the precheck keeps the finer
# distinction.
DELETE_TASK_GUARDED = """
DELETE FROM tasks
 WHERE id = %(id)s
   AND owner_id = %(owner_id)s
   AND version = %(expected_version)s
RETURNING *;
"""

# INSERT_TASK cannot restore a deletion. It lets the database generate the id
# and defaults version to 1, while section 8 requires the original id from
# event.before and a version continuing from the deleted row's plus one, because
# history is append-only and never rewound. created_at is carried across for the
# same reason: the row is the same task returning, not a new one, and
# SELECT_TASKS_FOR_OWNER orders on created_at.
#
# The primary key is this statement's guard. The precheck already establishes
# that the id is absent, so a unique violation here means the row reappeared
# between the two passes. undo lets that abort the transaction and translates it
# to a ROW_RECREATED refusal rather than catching it in place, because an
# all-or-nothing operation has nothing to continue with.
INSERT_TASK_RESTORED = """
INSERT INTO tasks (
  id, owner_id, title, notes, due_date, priority, status, blocked_by,
  version, created_at, updated_at
)
VALUES (
  %(id)s::uuid,
  %(owner_id)s::uuid,
  %(title)s,
  %(notes)s,
  %(due_date)s::date,
  %(priority)s::task_priority,
  %(status)s::task_status,
  %(blocked_by)s::uuid,
  %(version)s,
  %(created_at)s::timestamptz,
  now()
)
RETURNING *;
"""

INSERT_TASK_EVENT = """
INSERT INTO task_events (task_id, run_id, actor_id, operation, before, after)
VALUES (
  %(task_id)s,
  %(run_id)s,
  %(actor_id)s,
  %(operation)s,
  %(before)s,
  %(after)s
)
RETURNING *;
"""

SELECT_EVENTS_FOR_RUN = """
SELECT *
  FROM task_events
 WHERE run_id = %(run_id)s
 ORDER BY id DESC
 LIMIT %(limit)s;
"""

# Deliberately unbounded, under D-39. Do not add a LIMIT to this statement.
#
# Section 5 requires a LIMIT on every list query, and that rule is about
# paginated reads for display. This is not one. Undo is all-or-nothing by
# specification, so a truncated read does not return a shorter answer, it returns
# a wrong one: the events past the bound are silently not compensated and the
# result still reports success. No fixed bound is provably safe either, because
# bulk_update_tasks places no cap on task_ids and one approved call can write
# arbitrarily many events. SELECT_EVENTS_FOR_RUN above keeps its LIMIT and
# remains the statement for display reads.
SELECT_ALL_EVENTS_FOR_RUN = """
SELECT *
  FROM task_events
 WHERE run_id = %(run_id)s
 ORDER BY id DESC;
"""

# Deliberately unbounded, under D-42, on the same reasoning D-39 recorded for
# SELECT_ALL_EVENTS_FOR_RUN. Section 5 requires a LIMIT on every list query, and
# that rule is about paginated reads for display. RunDetail.steps is not one:
# the wire shape in section 9 carries no cursor and no truncation flag, so a
# fixed bound would not return a shorter answer, it would return a false one,
# claiming a complete run while silently omitting steps. No fixed bound is
# provably safe either, because bulk_update_tasks places no cap on task_ids.
# SELECT_LEASE remains the single-row read and keeps its key.
#
# now() rides along because RunStep.duration_ms for a still-pending row is
# measured against it. lease_expires_at was written by the database clock, so
# reading the observation time from any other clock would make an idle step's
# duration drift with the caller's skew.
#
# The tool_call_id tie-breaker makes ordering deterministic when two rows share
# a created_at. It is a presentation device, not a claim that lexical id order
# reconstructs causal order.
SELECT_INVOCATIONS_FOR_RUN = """
SELECT *, now() AS observed_at
  FROM tool_invocations
 WHERE run_id = %(run_id)s
 ORDER BY created_at ASC, tool_call_id ASC;
"""

INSERT_LEASE = """
INSERT INTO tool_invocations
  (run_id, tool_call_id, tool_name, arguments_hash, status, lease_expires_at)
VALUES
  (%(run_id)s, %(tool_call_id)s, %(tool_name)s, %(arguments_hash)s, 'pending',
   now() + make_interval(secs => %(lease_ttl_seconds)s))
ON CONFLICT (run_id, tool_call_id) DO NOTHING
RETURNING run_id;
"""

SELECT_LEASE = """
SELECT *
  FROM tool_invocations
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s;
"""

COMPLETE_LEASE = """
UPDATE tool_invocations
   SET status = 'completed',
       result = %(result)s,
       error = NULL,
       completed_at = now()
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s
RETURNING *;
"""

FAIL_LEASE = """
UPDATE tool_invocations
   SET status = 'failed',
       error = %(error)s,
       completed_at = now()
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s
RETURNING *;
"""

# The two guarded reacquires from BUILD_SPEC section 7, added at T05 under D-19.
# Section 5 lists no statement carrying either guard and none can be adapted to,
# because COMPLETE_LEASE and FAIL_LEASE are unconditional writes.
#
# The guard is inside the UPDATE in both, and that placement is the whole point.
# A SELECT that observes the state followed by an unguarded UPDATE is not
# equivalent: two retries can both observe the same row and both proceed, which
# is the duplicate execution the lease exists to prevent. Both statements return
# the row they touched, so ownership is decided by whether a row came back.
# Only the caller whose guarded UPDATE succeeds may execute.

REACQUIRE_FAILED_LEASE = """
UPDATE tool_invocations
   SET status = 'pending',
       attempt = attempt + 1,
       error = NULL,
       lease_expires_at = now() + make_interval(secs => %(lease_ttl_seconds)s)
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s
   AND status = 'failed'
RETURNING *;
"""

STEAL_EXPIRED_LEASE = """
UPDATE tool_invocations
   SET status = 'pending',
       attempt = attempt + 1,
       lease_expires_at = now() + make_interval(secs => %(lease_ttl_seconds)s)
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s
   AND lease_expires_at < now()
RETURNING *;
"""

INSERT_APPROVAL = """
INSERT INTO approvals (
  run_id,
  tool_call_id,
  tool_name,
  arguments,
  arguments_hash,
  required_reason,
  preview,
  expires_at
)
VALUES (
  %(run_id)s,
  %(tool_call_id)s,
  %(tool_name)s,
  %(arguments)s,
  %(arguments_hash)s,
  %(required_reason)s,
  %(preview)s,
  now() + make_interval(secs => %(approval_ttl_seconds)s)
)
RETURNING *;
"""

SELECT_APPROVAL = """
SELECT *
  FROM approvals
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s;
"""

DECIDE_APPROVAL = """
UPDATE approvals
   SET decision = %(decision)s,
       decided_at = now()
 WHERE run_id = %(run_id)s
   AND tool_call_id = %(tool_call_id)s
   AND decision = 'pending'
RETURNING *;
"""

INSERT_RUN = """
INSERT INTO agent_runs (actor_id, prompt, model)
VALUES (%(actor_id)s, %(prompt)s, %(model)s)
RETURNING *;
"""

UPDATE_RUN_STATUS = """
UPDATE agent_runs
   SET status = %(status)s,
       error = %(error)s,
       ended_at = CASE
         WHEN %(status)s::run_status IN ('completed', 'failed', 'interrupted')
         THEN now()
         ELSE ended_at
       END
 WHERE id = %(run_id)s
RETURNING *;
"""

UPDATE_RUN_HISTORY = """
UPDATE agent_runs
   SET message_history = %(message_history)s
 WHERE id = %(run_id)s
RETURNING *;
"""

UPDATE_RUN_USAGE = """
UPDATE agent_runs
   SET model_calls = model_calls + %(model_calls)s,
       tool_calls = tool_calls + %(tool_calls)s,
       input_tokens = input_tokens + %(input_tokens)s,
       output_tokens = output_tokens + %(output_tokens)s,
       cost_cents = cost_cents + %(cost_cents)s
 WHERE id = %(run_id)s
RETURNING *;
"""

SELECT_RUN = """
SELECT *
  FROM agent_runs
 WHERE id = %(run_id)s
   AND actor_id = %(actor_id)s;
"""

# The five-table reset specified for POST /api/demo/reset in BUILD_SPEC section
# 13, added at T04 under D-17 because the invariant fixtures need it and
# agent_runs has no delete statement. T09 consumes this rather than duplicating
# it. RESTART IDENTITY resets the task_events bigserial so event ids stay
# hand-checkable across a reset.
TRUNCATE_ALL_STATE = """
TRUNCATE TABLE tasks, task_events, agent_runs, tool_invocations, approvals
RESTART IDENTITY CASCADE;
"""

SWEEP_ORPHAN_RUNS = """
UPDATE agent_runs
   SET status = 'interrupted',
       error = %(error)s,
       ended_at = now()
 WHERE status = 'running'
RETURNING *;
"""
