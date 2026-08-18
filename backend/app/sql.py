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

# The T09 administrative fixture insert, under D-48. It accepts exactly the
# identity and semantic fields the fixed seed owns. Status, version, and both
# timestamps remain schema-owned, unlike INSERT_TASK_RESTORED, whose undo path
# legitimately carries historical values across a compensation.
INSERT_SEED_TASK = """
INSERT INTO tasks (id, owner_id, title, notes, due_date, priority, blocked_by)
VALUES (
  %(id)s::uuid,
  %(owner_id)s::uuid,
  %(title)s,
  %(notes)s,
  %(due_date)s::date,
  %(priority)s::task_priority,
  %(blocked_by)s::uuid
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

# T12B. The live card for one run, for RunDetail.pending_approval.
#
# SELECT_APPROVAL cannot serve: it is keyed on (run_id, tool_call_id) and a
# RunDetail read knows only the run. Both predicates below are load bearing. A
# decided row is not a card, and D-56 freezes at most one simultaneously pending
# approval per application run, so a row that survived its expiry must not be
# rendered as something the user can still act on.
#
# No LIMIT, on the reasoning D-39 recorded for SELECT_ALL_EVENTS_FOR_RUN and
# D-42 repeated for SELECT_INVOCATIONS_FOR_RUN. A bound here would not return a
# shorter answer, it would hide a violated invariant behind a plausible one. The
# caller counts the rows and refuses rather than taking the first, because
# taking the first is the rule D-45 names and rejects.
SELECT_PENDING_APPROVALS_FOR_RUN = """
SELECT *
  FROM approvals
 WHERE run_id = %(run_id)s
   AND decision = 'pending'
   AND expires_at > now()
 ORDER BY tool_call_id;
"""

# T12B, the D-51 continuation lookup: interruptId -> tool_call_id -> approval
# row -> agent_runs.id.
#
# approvals is PRIMARY KEY (run_id, tool_call_id), so a provider-generated call
# id is unique within a run and not across runs. D-51 requires the resolution to
# distinguish zero eligible rows, which refuses, exactly one, which resolves,
# and more than one, which refuses as ambiguous. There is deliberately no LIMIT
# and no appeal to identifier entropy: probability is not uniqueness.
#
# The join is what actor scopes it. approvals carries no actor, so without it a
# call id would enumerate another actor's pending approval. The status predicate
# means a replayed continuation against a run that already finished resolves
# nothing, so a double continuation is refused at the transport rather than left
# to the idempotency lease.
#
# decision <> 'pending' is the ordering rule from BUILD_SPEC section 10 stated as
# a predicate: the server persists the human decision before it constructs any
# framework continuation, so a continuation naming an undecided row is not
# eligible. Expiry is deliberately not filtered here; policy.check step 5c is the
# authority on it and runs inside the tool body on every path. See D-58.
SELECT_APPROVAL_FOR_CONTINUATION = """
SELECT a.*
  FROM approvals a
  JOIN agent_runs r ON r.id = a.run_id
 WHERE a.tool_call_id = %(tool_call_id)s
   AND a.decision <> 'pending'
   AND r.actor_id = %(actor_id)s
   AND r.status = 'awaiting_approval'
 ORDER BY a.run_id;
"""

# T12B. The preview read, for the approval card only.
#
# Minimum authority on purpose. SELECT_TASKS_BY_IDS_FOR_UPDATE would serve the
# same rows but takes row locks for a read that mutates nothing and holds them
# for the length of an approval the user may never answer.
# SELECT_TASKS_FOR_OWNER cannot serve: it has no id filter and carries a LIMIT.
#
# owner_id is defence in depth rather than the scope check. The scope check is
# policy.resolve_scope, which runs first and refuses the whole call before any
# task detail is fetched, because a preview built from a foreign row has already
# disclosed it by the time an authoritative check could catch the mutation.
SELECT_TASKS_BY_IDS_FOR_OWNER = """
SELECT *
  FROM tasks
 WHERE owner_id = %(owner_id)s
   AND id = ANY(%(task_ids)s::uuid[])
 ORDER BY id;
"""

INSERT_RUN = """
INSERT INTO agent_runs (actor_id, prompt, model)
VALUES (%(actor_id)s, %(prompt)s, %(model)s)
RETURNING *;
"""

# D-67. A successor ordinary turn is born with the predecessor's
# server-owned canonical history already persisted.
INSERT_RUN_WITH_HISTORY = """
INSERT INTO agent_runs (actor_id, prompt, model, message_history)
VALUES (
    %(actor_id)s,
    %(prompt)s,
    %(model)s,
    %(message_history)s
)
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
# 13, added at T04 under D-17 because agent_runs has no delete statement. T09
# consumes this rather than duplicating it. RESTART IDENTITY resets the
# task_events bigserial so event ids stay hand-checkable across a reset.
#
# Since T00L this statement names five tables and clears six. CASCADE reaches
# linear_projections through its foreign key to task_events, which is correct:
# an outbox row for an event that no longer exists has nothing to deliver.
#
# It does NOT reach linear_task_state, which has no foreign key to tasks by
# design under D-26. That is deliberate and must not be repaired by adding the
# table here. Production reset stays Linear-unaware: clearing tombstones is a
# remote-ordering problem that belongs to the reset fence in D-28 and is
# deferred to T29. Deterministic tests need the opposite and use
# TRUNCATE_ALL_TEST_STATE below.
TRUNCATE_ALL_STATE = """
TRUNCATE TABLE tasks, task_events, agent_runs, tool_invocations, approvals
RESTART IDENTITY CASCADE;
"""

# T00L. Test-only cleanup, never reachable from a route.
#
# linear_task_state is a tombstone that deliberately survives deletion of its
# task, so nothing in the business truncate above can clear it. Left alone
# between deterministic tests it leaks: a stale diverged row keyed to a task id
# a later test reuses would make the T00L divergence refusals pass without the
# code under test ever reading the flag correctly. A test that passes for the
# wrong reason is the failure mode the invariant suite exists to prevent, so the
# suite clears integration state explicitly and production does not.
#
# The split is proven rather than trusted to naming: the T00L gate asserts that
# seed.reset uses TRUNCATE_ALL_STATE and that this statement is the one the
# deterministic fixtures call.
# T00W adds the four transport tables here and deliberately not above. Under
# D-69 production reset must never revoke an OAuth installation or discard
# accepted webhook work: resetting the demo board is a business operation, and
# an operator who resets the board does not expect to reinstall the app.
TRUNCATE_ALL_TEST_STATE = """
TRUNCATE TABLE tasks, task_events, agent_runs, tool_invocations, approvals,
               linear_task_state, linear_installations, linear_oauth_states,
               linear_agent_inbox, linear_agent_sessions
RESTART IDENTITY CASCADE;
"""

# T00L, under D-27. Step 1b of policy.check, and the undo precheck's divergence
# pass.
#
# Returns only the diverged ids, so a task with no linear_task_state row has
# never been projected and cannot have diverged. The caller has already proven
# ownership of every id it passes, which is why this statement is not owner
# scoped: scoping it would imply it were safe to call before the scope check,
# and it is not. See the ordering comment in policy.check.
SELECT_DIVERGED_TASK_IDS = """
SELECT task_id
  FROM linear_task_state
 WHERE task_id = ANY(%(task_ids)s::uuid[])
   AND diverged;
"""

SWEEP_ORPHAN_RUNS = """
UPDATE agent_runs
   SET status = 'interrupted',
       error = %(error)s,
       ended_at = now()
 WHERE status = 'running'
RETURNING *;
"""


# ---------------------------------------------------------------------------
# T00W. OAuth installation and AgentSession transport. See D-69 and D-70.
# ---------------------------------------------------------------------------

INSERT_LINEAR_OAUTH_STATE = """
INSERT INTO linear_oauth_states (state_hash, expires_at)
VALUES (%(state_hash)s, now() + make_interval(secs => %(ttl_seconds)s))
RETURNING *;
"""

# The whole single-use guarantee, in one statement.
#
# Matching, unexpired, and unconsumed are all predicates on the UPDATE rather
# than checks a caller performs first. A SELECT that observed an unconsumed row
# followed by an unguarded UPDATE is the check-then-act race that lets two
# callbacks both consume one state, and the OAuth state exists precisely to make
# a replayed callback fail. Zero rows back means refuse, and the caller cannot
# learn which of the three reasons applied.
CONSUME_LINEAR_OAUTH_STATE = """
UPDATE linear_oauth_states
   SET consumed_at = now()
 WHERE state_hash = %(state_hash)s
   AND expires_at > now()
   AND consumed_at IS NULL
RETURNING *;
"""

INSERT_LINEAR_INSTALLATION = """
INSERT INTO linear_installations (
  organization_id, oauth_client_id, app_user_id, allowed_linear_user_id,
  access_token, refresh_token, access_token_expires_at, granted_scopes
) VALUES (
  %(organization_id)s, %(oauth_client_id)s, %(app_user_id)s,
  %(allowed_linear_user_id)s, %(access_token)s, %(refresh_token)s,
  now() + make_interval(secs => %(expires_in)s), %(granted_scopes)s
)
RETURNING *;
"""

SELECT_ACTIVE_LINEAR_INSTALLATION = """
SELECT *
  FROM linear_installations
 WHERE status = 'active';
"""

# The installation binding an AgentSessionEvent must match. All three provider
# identifiers together, because any one alone is insufficient: an organization
# can install several apps, one OAuth client serves many organizations, and an
# app user is only meaningful within its workspace.
SELECT_LINEAR_INSTALLATION_FOR_EVENT = """
SELECT *
  FROM linear_installations
 WHERE organization_id = %(organization_id)s
   AND oauth_client_id = %(oauth_client_id)s
   AND app_user_id = %(app_user_id)s
   AND status = 'active';
"""

# Revocation, guarded by when the revocation actually happened.
#
# `created_at <= %(revocation_created_at)s` is the reinstall guard and it is the
# reason this statement takes the provider's event time at all. Linear retries a
# failed delivery for up to six hours, so a revocation of an installation that
# has since been replaced can arrive after the replacement exists. Without the
# predicate that replay would revoke the new installation and take the demo down
# with a stale message.
#
# The event's own `createdAt` is used rather than `webhookTimestamp`, because
# the first is when the revocation happened and the second is when this delivery
# attempt was sent. A six hour old revocation redelivered now carries a fresh
# delivery timestamp and would defeat the guard entirely.
REVOKE_LINEAR_INSTALLATION = """
UPDATE linear_installations
   SET status = 'revoked',
       updated_at = now()
 WHERE organization_id = %(organization_id)s
   AND oauth_client_id = %(oauth_client_id)s
   AND status = 'active'
   AND created_at <= %(revocation_created_at)s
RETURNING *;
"""

# Durable ingress. `ON CONFLICT DO NOTHING` rather than catching a unique
# violation, because a duplicate delivery is ordinary expected traffic and not
# an exceptional condition. Catching `UniqueViolation` would also catch a
# constraint the caller never reasoned about and report it as a duplicate.
#
# Zero rows back means one of the two dedupe identities already exists, and the
# caller classifies which by reading them back. It never means the insert
# failed for an unrelated reason: those still raise.
INSERT_LINEAR_INBOX = """
INSERT INTO linear_agent_inbox (
  delivery_id, body_sha256, organization_id, agent_session_id, action,
  payload, status, refusal_reason
) VALUES (
  %(delivery_id)s, %(body_sha256)s, %(organization_id)s, %(agent_session_id)s,
  %(action)s, %(payload)s, %(status)s, %(refusal_reason)s
)
ON CONFLICT DO NOTHING
RETURNING *;
"""

# Conflict classification. Read both identities separately so the caller can
# tell an ordinary duplicate from the case where one identity matches one row
# and the other matches a different row.
SELECT_LINEAR_INBOX_BY_DELIVERY = """
SELECT * FROM linear_agent_inbox WHERE delivery_id = %(delivery_id)s;
"""

SELECT_LINEAR_INBOX_BY_BODY = """
SELECT * FROM linear_agent_inbox WHERE body_sha256 = %(body_sha256)s;
"""


# T00W worker dequeue.
#
# Correctness is in the predicate, not merely ORDER BY:
#
# * a row must itself be pending, due, and unleased/lease-expired;
# * any earlier pending row for the same organization + AgentSession blocks it,
#   even when that earlier row is leased or backing off via not_before;
# * therefore prompt N+1 cannot overtake prompt N;
# * another AgentSession remains independently claimable;
# * FOR UPDATE SKIP LOCKED lets concurrent workers avoid waiting on the same
#   candidate without weakening the per-session ordering predicate.
#
# The lease and attempt increment happen in the same statement that chooses the
# row. There is no SELECT-then-UPDATE claim race.
CLAIM_LINEAR_INBOX = """
WITH candidate AS (
    SELECT i.id
      FROM linear_agent_inbox AS i
     WHERE i.status = 'pending'
       AND i.not_before <= now()
       AND (i.claimed_until IS NULL OR i.claimed_until <= now())
       AND NOT EXISTS (
           SELECT 1
             FROM linear_agent_inbox AS earlier
            WHERE earlier.organization_id = i.organization_id
              AND earlier.agent_session_id = i.agent_session_id
              AND earlier.status = 'pending'
              AND (earlier.received_at, earlier.id)
                  < (i.received_at, i.id)
       )
     ORDER BY i.received_at, i.id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE linear_agent_inbox AS inbox
   SET claimed_until = now() + (%(lease_seconds)s * interval '1 second'),
       attempt_count = inbox.attempt_count + 1
  FROM candidate
 WHERE inbox.id = candidate.id
RETURNING inbox.*;
"""


# ---------------------------------------------------------------------------
# T00W worker. Session continuity, turn finalization, and token rotation.
# ---------------------------------------------------------------------------

# The per-AgentSession continuity cursor, created on demand.
#
# `ON CONFLICT ... DO UPDATE` rather than `DO NOTHING`, because `DO NOTHING`
# returns no row on the second call and the caller needs the current cursor, not
# an absence it would have to re-read in a second statement that could race.
UPSERT_LINEAR_AGENT_SESSION = """
INSERT INTO linear_agent_sessions (organization_id, agent_session_id)
VALUES (%(organization_id)s, %(agent_session_id)s)
ON CONFLICT (organization_id, agent_session_id)
DO UPDATE SET updated_at = now()
RETURNING *;
"""

SELECT_LINEAR_AGENT_SESSION = """
SELECT *
  FROM linear_agent_sessions
 WHERE organization_id = %(organization_id)s
   AND agent_session_id = %(agent_session_id)s;
"""

# The run this inbox row executed, recorded before the model is invoked.
#
# Written first so that a crash mid-turn leaves the link durable. The worker
# treats a claimed row that already carries a run_id as a turn whose outcome it
# cannot re-derive, and refuses to execute it a second time. That is the whole
# duplicate-mutation defense, and it only works if this write precedes execution.
SET_LINEAR_INBOX_RUN = """
UPDATE linear_agent_inbox
   SET run_id = %(run_id)s
 WHERE id = %(id)s
RETURNING *;
"""

# Terminal success for one turn. Half of an atomic pair; see the worker.
COMPLETE_LINEAR_INBOX = """
UPDATE linear_agent_inbox
   SET status = 'completed',
       claimed_until = NULL,
       completed_at = now(),
       run_id = %(run_id)s,
       last_error = %(last_error)s
 WHERE id = %(id)s
RETURNING *;
"""

# The other half. Advances the continuity cursor to the run just completed.
#
# This statement and COMPLETE_LINEAR_INBOX run in one transaction and commit
# together. A cursor advanced without the completion would replay the turn on a
# successor it already influenced; a completion without the advance would drop
# the conversation's history. Neither is acceptable, so neither is separable.
ADVANCE_LINEAR_AGENT_SESSION = """
INSERT INTO linear_agent_sessions (
  organization_id, agent_session_id, last_completed_run_id
) VALUES (
  %(organization_id)s, %(agent_session_id)s, %(run_id)s
)
ON CONFLICT (organization_id, agent_session_id)
DO UPDATE SET last_completed_run_id = EXCLUDED.last_completed_run_id,
              updated_at = now()
RETURNING *;
"""

# Transient release. The row stays pending, the lease is dropped, and not_before
# moves forward so a hot loop cannot spin on a failing row.
#
# `run_id` is set back to NULL deliberately. The caller reaches this path only
# after establishing that no mutation committed, which is what makes a second
# execution safe; leaving the id set would make the duplicate-execution guard
# refuse the retry this path exists to permit.
RELEASE_LINEAR_INBOX = """
UPDATE linear_agent_inbox
   SET claimed_until = NULL,
       run_id = NULL,
       not_before = now() + make_interval(secs => %(backoff_seconds)s),
       last_error = %(last_error)s
 WHERE id = %(id)s
RETURNING *;
"""

# Terminal local failure. Distinct from 'refused': refused is a permanent answer
# reached at ingress about authorization, and failed is a turn that was accepted
# and could not be completed. The CHECK constraint keeps refusal_reason NULL here.
FAIL_LINEAR_INBOX = """
UPDATE linear_agent_inbox
   SET status = 'failed',
       claimed_until = NULL,
       completed_at = now(),
       run_id = %(run_id)s,
       last_error = %(last_error)s
 WHERE id = %(id)s
RETURNING *;
"""

SELECT_ACTIVE_LINEAR_INSTALLATION_BY_ID = """
SELECT *
  FROM linear_installations
 WHERE id = %(id)s
   AND status = 'active';
"""

# Rotation persistence, compare-and-swap.
#
# `refresh_token = %(spent_refresh_token)s` is the whole mechanism, and it is
# here rather than expressed as a `FOR UPDATE` lock for one reason: Linear
# rotates refresh tokens, and the refresh itself is a network call. Holding a row
# lock across that call pins a connection for the provider's full timeout, on the
# path of an acknowledgement Linear expects within ten seconds.
#
# Guarding on the token that was actually spent gives the same safety without the
# lock. Two workers that both refreshed produce two rotations; the first to reach
# this statement matches and wins, and the second matches nothing, updates zero
# rows, and learns from the empty result that it must re-read rather than
# overwrite the winner's live credential. Last-write-wins would instead persist a
# token the provider has already superseded.
#
# COALESCE keeps the existing refresh token when a response carries none, rather
# than nulling a credential that still works.
ROTATE_LINEAR_INSTALLATION_TOKENS = """
UPDATE linear_installations
   SET access_token = %(access_token)s,
       refresh_token = COALESCE(%(refresh_token)s, refresh_token),
       access_token_expires_at = now() + make_interval(secs => %(expires_in)s),
       updated_at = now()
 WHERE id = %(id)s
   AND status = 'active'
   AND refresh_token = %(spent_refresh_token)s
RETURNING *;
"""
