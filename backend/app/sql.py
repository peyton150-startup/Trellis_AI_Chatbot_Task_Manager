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
