# Linear integration: specification delta

Written 2026-08-12 during the T06 session, after the user confirmed the demo must
run against real Linear issues and projects rather than a local board only.
Revised the same day, revision 01, after an Opus review that executed the
proposed migration against PostgreSQL 16 rather than only reading it.

Revision 01 changed six things: integration state moved off `tasks` into a
tombstoned `linear_task_state` table (D-26); `restored` maps to `unarchive`
rather than `update` (D-25); the reconciler must exclude archived issues and skip
tasks with a pending projection (D-27); reset must fence the projector and
delivery is serialized per task (D-28); the invariant count is reconciled against
D-19 rather than edited (D-29); and every specification delta is assigned to an
owning task (section 8). The decisions are D-24 through D-29, because T06 took
D-23.

This document is the authoritative content for the change. It is not itself a
task. Each block below says which file it belongs in and where. The blocks get
applied by the tasks named in section 8, on their own branches, under the normal
one task one commit protocol in `CLAUDE.md`.

No code or other document is changed by this file landing. The blocks below are
applied by the tasks named in section 8, each on its own branch. T06 is merged,
so nothing here is waiting on it.

---

## 1. What changed and why

The build spec was written for a self-contained demo: an agent operating a
Postgres-backed todo list, with a Next.js board as the only visible surface.
The user has now fixed a harder requirement. The demo has to run on Linear.
Filippo watches real issues move in Linear's own interface, and the agent has
to create issues, change their status, priority, due date, assignee, labels,
and project membership, and archive them.

That requirement is not satisfiable by renaming anything. It needs a real
integration, and the question is where the integration attaches to the existing
architecture without destroying what the architecture exists to prove.

### The failure mode we are avoiding

The obvious design is to route writes through a provider interface, so that
`domain.create_task` dispatches either to Postgres or to Linear. That design
was considered and rejected, and the reason is specific rather than stylistic.

BUILD_SPEC section 7 states, and calls non-negotiable:

> the domain mutation and its `task_events` rows and the `complete()` call all
> happen inside one database transaction. If the process dies before commit,
> nothing happened. If it dies after commit, the lease says `completed` and the
> retry replays. There is no window where the mutation committed and the lease
> did not.

Every reliability property in this build rests on that sentence. Lease stealing
in `idempotency.py` is safe only because a `pending` row provably means the
transaction never committed, so the stolen work left no trace. D-22 depends on
it. The signature demo moment at 8:00, where a mutation commits, the response is
lost, the request repeats, and the Run Inspector shows
`Attempt 1 COMMITTED / Attempt 2 DEDUPLICATED / Mutations 1`, is a direct
demonstration of it.

A Linear GraphQL mutation is a remote HTTP call. It cannot enlist in a Postgres
transaction. If a tool body calls Linear inside step 4, then a process death
between the Linear call and the commit leaves Linear mutated and the lease
`pending`. Lease stealing would then re-execute a mutation that already landed
in Linear. The demo would be asserting exactly-once behaviour while the code
delivered at-most-once-per-attempt against the external system, and the
assertion would be false in precisely the scenario the demo dramatizes.

So the integration attaches after the consistency boundary, not inside it.

### The design

Postgres remains the authoritative writer. Linear is an asynchronously projected
representation of committed state.

```
agent tool
  → policy.check              (scope, divergence, classify, approval)
  → idempotency.acquire
  → ONE POSTGRES TRANSACTION
        domain mutation
        task_events row
        linear_projections row   (written by trigger, see D-24)
        idempotency.complete
  → commit
  → projector drains linear_projections → Linear GraphQL
  → Linear board moves
```

Reads of external change flow the other way, and only as far as a flag:

```
reconciler polls Linear for issues whose updatedAt moved
  → issue we know about, changed outside the agent → set tasks.diverged = true
  → issue we do not know about → import as a new task
  → never merges, never overwrites a local field
```

### What this buys, stated plainly

The failure mode when Linear is slow, rate limited, or down becomes:

```
local mutation:      committed
audit record:        committed
idempotency result:  committed
Linear projection:   pending
```

That is a queue that drains, not a half-applied change. Nothing about the
agent's correctness depends on Linear being reachable. The invariant suite stays
offline and CI-gating. `undo.py` never learns what Linear is, because undo
operates on authoritative local state and its compensating mutations project
outward like any other change.

---

## 2. How each addition helps Linear issue management specifically

This section exists because the implementing model will make better decisions if
it knows what each piece is for in product terms, not just structural terms.

**The outbox table and projector** are what make an agent action visible in
Linear at all. Without them the agent changes Postgres and Linear never hears.
With them, "set these three issues to In Progress" becomes three committed local
updates and three GraphQL mutations that land a moment later, in order, with
retry if Linear hiccups. Ordering matters for issue management: if a task is
created and then immediately moved to a project, the create must reach Linear
first or the move targets an issue that does not exist yet. The outbox is
ordered by `task_events.id`, which is the order the mutations actually happened,
so this is handled by construction rather than by hope.

**The trigger that writes the outbox row** exists because "every change to an
issue reaches Linear" is an invariant, and invariants enforced by convention get
broken. If the outbox write lives in application code, then every present and
future call site has to remember it, including `undo.py`, the seed loader, and
anything added under time pressure on day five. Putting it on
`AFTER INSERT ON task_events` makes the rule structural: if a change produced an
audit event, it projects. If it did not produce an audit event, it did not
happen as far as this system is concerned, and it should not appear in Linear
either. That last clause is not a limitation, it is the correct behaviour. A raw
`INSERT` in psql that bypassed the audit log should not silently become a Linear
issue.

**The divergence flag and `EXTERNAL_DIVERGENCE`** are what stop the agent
overwriting a human. Issue management is inherently multi-actor. Filippo will
have Linear open. If he drags an issue to Done and then asks the agent to
reprioritize the board, an agent that does not know about his edit will project
stale local state over the top of it and silently undo his work, on camera. The
flag makes the system notice, and the policy layer then refuses rather than
merges. Refusing is the right call here rather than a cop-out: a merge strategy
for arbitrary field-level conflicts is a genuine distributed systems project,
and the demo's whole thesis is that the boundary refuses cleanly when it cannot
proceed safely. This is the same behaviour `update_task` already has for
`expected_version` conflicts, extended to an actor outside the system.

**The reconciler's import path** is what stops the agent looking broken. If
Filippo creates an issue in Linear and then asks the agent about it, an agent
that only knows its own creations will say it does not exist. Importing makes
the agent's view of the workspace match what the human sees, which is the
minimum bar for something claiming to manage a real issue tracker.

**Name to id resolution built at startup** is what keeps the tool schemas
narrow. BUILD_SPEC section 10 requires enums rather than free strings, because
narrow schemas give the model less room to be wrong. Linear's statuses,
projects, labels, and assignees are workspace-defined objects with UUIDs, not
fixed values. Hardcoding UUIDs into the schema would be brittle and unreadable;
accepting free strings would abandon the property the spec is protecting.
Building the enum members at startup from the demo team's real workflow states,
and resolving name to id inside the adapter, keeps the model working in human
vocabulary while the wire carries ids. It also means the enum is correct for
whatever workspace the demo runs against, which matters because the demo will
not run in the workspace holding the TAD tickets.

**Archive rather than delete** is what makes undo real against Linear.
`issueDelete` is not reversible in a way this demo can rely on. `issueArchive`
is, via `issueUnarchive`. Since the 3:00 demo beat is delete followed by undo,
the destructive path maps to archive. This has a second benefit: archived issues
do not count against the free plan's 250 non-archived issue cap, so repeated
`POST /api/demo/reset` cycles across a week of rehearsal cannot exhaust it.

**Gate B and the contract fixture** exist because this build has already learned
this lesson once. T00 exists because assumptions about the Pydantic AI approval
API turned out to be wrong in a way that changed the tool body shape, and that
discovery on day one is why D-12 exists rather than a broken tool layer. Linear
is a second external contract and gets the same treatment: probe it, write down
what is true, and freeze the subset we depend on into a fixture so a later
change to Linear's schema shows up as a failing contract test rather than a
confusing runtime error during rehearsal.

**`FakeTracker`** is the offline implementation the Linear-facing tasks test
against. Be precise about what it is for, because an earlier draft of this
document overstated it. The two divergence refusals do not need it: both read a
local flag and raise, and neither touches Linear, so both are already offline.
`FakeTracker` is needed by T26 through T29, which exercise resolution, delivery,
and reconciliation, and none of which belong to the invariant suite. It is a test
double, not a provider abstraction. There is one external system and it is
Linear, and nothing here is designed so that Jira could be dropped in.

---

## 3. New decisions, for `docs/DECISIONS.md`

Append after D-23, under a new heading. T06 took D-23 for its two `sql.py`
constants, so the Linear decisions are D-24 through D-29.

```markdown
---

## Linear integration decisions

Recorded on 2026-08-12 after the user fixed the requirement that the demo runs
against real Linear issues and projects, and revised the same day after the Opus
review of this document. No decision above is amended. D-02, D-04, D-09, D-10,
D-18, D-19, and D-22 all constrain what follows, and none of them changes.

### D-24: Postgres stays authoritative and Linear is a projected surface

The demo runs on Linear. The write path does not.

A tool body commits its domain mutation, its `task_events` rows, and
`idempotency.complete` in one Postgres transaction, per BUILD_SPEC section 7.
A Linear GraphQL mutation cannot join that transaction. Calling Linear from
inside the tool body would create a window where Linear mutated and the lease
was still `pending`, and lease stealing would then re-execute work that had
already landed externally. That would falsify the exactly-once claim in exactly
the scenario the 8:00 demo moment dramatizes.

Therefore Linear is written to only after the local transaction commits, by a
background projector reading an outbox table. Ownership is:

```
Postgres = authoritative state
Linear   = asynchronously projected external representation
```

Consequences, all binding:

The failure mode when Linear is unavailable is `projection pending`, never
`mutation half applied`. Agent correctness does not depend on Linear being
reachable.

`undo.py` is not modified for Linear beyond the precheck clause in D-27. Undo
applies compensating mutations to authoritative local state, those mutations
write `task_events` rows like any other, and the projector carries the resulting
state outward. Undo has no knowledge of Linear.

No tool function calls Linear. No code inside a `with transaction` block calls
Linear. A tool that awaits a Linear response before committing is the specific
defect this decision exists to prevent.

The honest claim, for the interview: local mutations are transactionally
exactly-once; external projection is at-least-once with locally-owned
deduplication. Do not claim exactly-once end to end.

### D-25: the outbox row is written by a database trigger, not by application code

`linear_projections` rows are written by an `AFTER INSERT ON task_events`
trigger defined in the migration, not by `domain.py`, `undo.py`, `seed.py`, or
any tool.

The invariant being protected is "every committed change to a task reaches
Linear." An invariant enforced by every call site remembering to do something is
an invariant that breaks under time pressure. A trigger makes it structural: an
audit event and its projection row are inserted in the same transaction by the
database itself, so they cannot diverge and no future call site can forget.

This also fixes the boundary of what projects. A change that did not write a
`task_events` row does not project, and that is correct. A raw `INSERT` into
`tasks` that bypassed the audit log must not silently become a Linear issue,
because the system has no record that it happened.

`linear_projections` carries `UNIQUE(event_id)`, so retries and a restarted
projector cannot enqueue the same change twice. Delivery deduplication is owned
locally rather than delegated to a Linear-side idempotency facility. A
client-supplied key counts as a second layer only if Gate B establishes
documented replay semantics for it; accepting the field is not the same as
guaranteeing the behaviour. Never the only layer in any case.

The outbox carries no `payload` column. `event_id` is the primary key and
references `task_events(id)`, whose `before` and `after` are immutable, so a
payload column would be a second representation of the same change that can
drift, and populating it would force field mapping into PL/pgSQL inside a
migration. The projector reads the event.

The operation mapping is four values, not three:

```
created   -> create
updated   -> update
deleted   -> archive
restored  -> unarchive
```

`restored` must not collapse into `update`. Undo of a delete writes a `restored`
event, and the Linear issue at that point is archived, so an update leaves it
archived while the local board shows the task back. That is the 3:00 demo beat
failing silently.

### D-26: integration state is a tombstoned side table, not columns on `tasks`

`tasks` keeps its eleven domain columns. Linear state lives in
`linear_task_state`, keyed by `task_id` as a bare primary key **with no foreign
key to `tasks`**.

Three columns on `tasks` was the original proposal and it breaks on contact.
Every domain read is `SELECT *` validated into `Task`, `TrellisModel` sets
`extra="forbid"`, and the added columns raise `ValidationError` on the first
call. Once on `Task` they also reach every `task_events` snapshot, where undo
restoring a `before` would restore a stale `external_id` and reset the
`diverged` flag the refusal depends on. Solving that with an exclude list is the
schedule-pressure answer; the side table makes it structurally impossible.

The missing foreign key is deliberate and re-adding it is a regression. With
`ON DELETE CASCADE`, deleting a task destroys `external_id` in the same
transaction that queues the `archive` projection, leaving the projector a
`task_id` and no issue to address. The row is a tombstone and outlives the task
on purpose.

```
task created    state row created when a Linear identity is first established
task updated    state row retained
task deleted    state row retained as a tombstone
                the archive projection uses the retained external_id
task restored   the same task_id rejoins the existing state row
                the projection unarchives the existing issue
demo reset      tombstones cleared explicitly, after the remote reset, behind
                the projector fence in D-28
permanent GC    out of scope for this build, deliberately
```

Restore reconnecting to the same external identity works only because BUILD_SPEC
section 8 reinserts a deleted task under its original id. That coupling is load
bearing and must not be relaxed.

This also keeps the only-writer rule true rather than merely promised. The
wording it replaces:

> `domain.py` is the only writer of task business state and the only producer of
> `task_events`. Projection metadata is integration state and may be written by
> the projector and reconciler without changing task version or producing domain
> events.

### D-27: external divergence refuses rather than merges

`linear_task_state` carries a `diverged` boolean. The reconciler sets it when
Linear reports an issue whose `updatedAt` moved without a corresponding local
projection. The reconciler never writes any `tasks` field for a known task, and
never merges remote values into local state.

Two safeguards are required, and neither is satisfied by a Gate B fact.

**Archived issues are excluded from the poll.** Without this a deleted task
resurrects: the local row is gone, the Linear issue is archived, and the import
path below sees an issue with no local row and recreates it. Deletion undoes
itself, and the demo reset makes it worse by archiving the whole team.

**Tasks with an incomplete projection are skipped.** The projector's own write
moves Linear's `updatedAt`. Between that mutation and the local record of it,
the reconciler can observe a moved timestamp with no matching record and flag
divergence against a task nobody outside the system touched, after which
`policy.check` refuses every subsequent mutation on it and the only recovery is
clearing a flag by hand. A pending projection means the system expects
`updatedAt` to move. Projection completion and the observed
`external_updated_at` are persisted atomically in one local statement, so there
is no window where one is set and the other is not.

`policy.check` gains a DIVERGENCE step, and `undo.py` gains an
`EXTERNALLY_MODIFIED` precheck reason. Both refuse.

The reason to refuse rather than merge is that field-level conflict resolution
between two systems with different concurrency models is a real project, and
this build has seven days. It is also consistent with what the system already
does: `update_task` refuses on `expected_version` conflict and undo refuses if
any row moved. Divergence is the same rule applied to an actor outside the
system, which is why it produces a refusal and not a new subsystem.

Product consequence, and the reason this is worth building rather than cutting:
during the demo a human will have Linear open. Without this, a human edit is
silently overwritten by the next projection. With it, the agent notices, says
which issue changed, and declines to act.

The reconciler's second job is import. A Linear issue with no local row is
inserted as a new task owned by the demo actor. That is a local write driven by
a remote read, it touches no external system, and it exists so the agent's view
of the workspace matches the human's.

### D-28: reset fences the projector, and delivery is serialized per task

Two coordination properties. The mechanism belongs to T27 and is deliberately not
fixed here; the semantics are.

**Reset and projection delivery are mutually exclusive.** `POST /api/demo/reset`
must fence the projection worker before mutating either Linear or local
integration state, and when reset returns, no projection from the pre-reset
generation may still be executing. The second clause is the operative one:
refusing new work is not enough if a delivery is already in flight. Ordering
reset's own statements does not help, because the projector is an independent
actor and can wake between any two of them.

An advisory lock the projector takes per delivery and reset takes exclusively
fits. If there is exactly one worker under application control, pausing and
draining it is simpler and preferred. Introduce a reset generation counter only
if the chosen mechanism cannot guarantee the drain.

**A later event for a task may not be delivered while an earlier projection for
that same task is incomplete.** The outbox is ordered by `event_id`, which fixes
dequeue order and nothing else. If delivery ever runs more than one row at a
time, `ORDER BY event_id` alone permits a later event for a task to complete
before an earlier one, and `create -> update -> archive -> unarchive` depends on
that not happening: out of order, an update targets an issue that does not exist
yet, or an unarchive races the archive it reverses. The tombstone reconnection in
D-26 assumes the same thing. A single worker, a per-task claim, or a per-task
advisory lock all satisfy it. Cross-task concurrency stays available, which is
where the throughput is.

**A projection whose changed fields have no Linear representation is completed
without a remote call.** T06 emits an `updated` event when a delete clears
another task's `blocked_by`, and `blocked_by` has no Linear field. The projector
detects that no mapped field changed and marks the row completed rather than
issuing an empty mutation and counting it as work.

### D-29: Gate B, the contract fixture, and the invariant count

Linear is a second external contract and gets the same treatment T00 gave
Pydantic AI: probe first, record what is true, do not build on assumption.

Gate B runs before any Linear code is written and produces three artifacts: the
answers in this file, a checked-in contract fixture, and an explicit
`GATE B: PASS` or `GATE B: FAIL`.

The fixture holds only the types and mutations this build depends on, not
Linear's whole schema. A marked, network-dependent drift test compares live
introspection against it. That test is excluded from CI for the same reason the
evals are, per D-09, and run on demand.

Do not record any observed rate limit as an architectural constant. Record the
observed value and the conclusion that it is comfortably above demo and
rehearsal usage. The number belongs to an external service contract and can
change; the conclusion is what this build depends on.

**The invariant count is not changed by editing a number.** D-19 already ruled on
this question: "No fourteenth named test is added; section 11 fixes the count at
thirteen, and T04 set the precedent of covering an unreachable case in the gate."
An earlier draft moved thirteen to fifteen without engaging that, and then nearly
moved it to sixteen. Two things follow.

The reconciler coordination property in D-28 does **not** become a named
invariant. It is a coordination property between the projector, the reconciler,
projection state, and an external timestamp, and constructing it needs
coordinated fakes. It is covered by the T28 integration gate rather than added to
the named invariant suite, consistent with D-19's treatment of concurrency states
that require coordinated fakes to construct. The gate asserts both directions, so
it proves safety rather than blanket suppression:

```
Given  a pending projection, and external updatedAt has moved
When   reconciliation runs
Then   the task is NOT marked diverged

Given  no pending projection, and external updatedAt has moved
When   reconciliation runs
Then   the task IS marked diverged
```

The two divergence refusals get the same scrutiny rather than a renumber. Either
the refusal in `policy.check` and the `EXTERNALLY_MODIFIED` refusal in `undo.py`
genuinely are new trust-boundary invariants, in which case T00L records that
D-19's count is superseded and why; or they belong in a gate as well and the
count stays at thirteen. T00L decides this deliberately and writes the resulting
number into D7 of `docs/PROJECT_PLAN.md` and BUILD_SPEC section 11. Thirteen to
fifteen to sixteen by accretion is the outcome this clause exists to prevent. CI
still requires 100 percent of whatever the suite is.

On FAIL: Linear is cut, the demo runs on the local board, and the projection
design is described in the README as the integration shape. Record the specific
symptom, as Gate A did.
```

---

## 4. `docs/BUILD_SPEC.md` deltas

### 4.1 Section 3, file tree

Add under `backend/app/`:

```
      linear.py               Linear GraphQL client, name to id resolution
      projector.py            drains linear_projections to Linear
      reconciler.py           polls Linear, sets diverged, imports new issues
```

Add under `backend/scripts/`:

```
      linear_probe.py
```

Add under `backend/tests/`:

```
      fakes.py                FakeTracker, the offline implementation
      test_contract.py        marked, network, excluded from CI
      fixtures/
        linear_contract.json  the frozen subset of Linear's schema
```

Add under `backend/migrations/`:

```
      002_linear.sql
```

### 4.2 Section 4, database schema

`001_init.sql` is already merged and must not be edited. The delta ships as
`002_linear.sql`.

**No columns are added to `tasks`.** See D-26. Integration state lives in its own
table, keyed by `task_id` with no foreign key:

```sql
linear_task_state
  task_id             uuid primary key,   -- no foreign key, deliberately
  external_id         text,               -- null until the create projects
  external_updated_at timestamptz,        -- Linear's updatedAt as last observed
  diverged            boolean not null default false,
  last_reconciled_at  timestamptz
```

The absent foreign key is the point. `ON DELETE CASCADE` would destroy
`external_id` in the same transaction that queues the `archive` projection. The
row is a tombstone; D-26 carries its lifecycle and the reason restore reconnects
to the same issue.

Outbox table:

```sql
linear_projections
  event_id      bigint primary key references task_events(id),
  task_id       uuid not null,
  operation     linear_operation not null,   -- create|update|archive|unarchive
  status        linear_delivery not null,    -- pending|completed|failed
  attempt_count int not null default 0,
  remote_id     text,                        -- Linear issue id, on success
  last_error    text,
  created_at    timestamptz not null default now(),
  completed_at  timestamptz
```

`event_id` is the primary key rather than a separate surrogate, which gives the
`UNIQUE(event_id)` guarantee in D-25 for free and makes the ordering key the same
as the audit ordering key. There is no `payload` column; the projector reads the
event, per D-25.

`operation` and `status` are `CREATE TYPE` enums, matching how section 4 already
handles closed value sets. A bare `text` column with a comment listing the legal
values is not the house style, and `approvals.required_reason` shows the `text`
plus `CHECK` alternative if an enum is inconvenient.

The trigger, per D-25, fires `AFTER INSERT ON task_events` and inserts the
corresponding `pending` row, mapping `created` to `create`, `updated` to
`update`, `deleted` to `archive`, and `restored` to `unarchive`.

**`TRUNCATE_ALL_STATE` needs a comment update.** Its `CASCADE` now reaches
`linear_projections` through the foreign key to `task_events`, so the constant
that documents a five-table reset performs a six-table one. `linear_task_state`
has no foreign key and is **not** reached by the cascade, which is why D-26
clears it explicitly.

### 4.3 Section 6, `policy.py`

`check` gains one step, between SCOPE and CLASSIFY. The position is not
arbitrary and must not be moved.

```
1. SCOPE
   (unchanged)

1b. DIVERGENCE
   If any task in target_task_ids has a linear_task_state row with
   diverged = true:
       raise EXTERNAL_DIVERGENCE
   A task with no linear_task_state row has never been projected and
   cannot have diverged.

2. CLASSIFY
   (unchanged)
```

After SCOPE, because a divergence refusal on a row the actor does not own would
disclose that the row exists, which is the same leak the existing step ordering
comment warns about. Before CLASSIFY, because divergence refuses regardless of
whether the operation would have required approval, and running it later would
mean building an approval card for a mutation that is going to be refused
anyway.

`errors.py` gains one code, in the section 6 table:

| Code | HTTP | Raised when |
|---|---|---|
| `EXTERNAL_DIVERGENCE` | 409 | A target task was modified in Linear outside this system |

409 rather than 403, because this is a concurrency conflict in the same family
as `VERSION_CONFLICT`, not an authorization failure.

### 4.4 Section 8, `undo.py`

The precheck pass gains one condition, applied to every event regardless of
operation, checked alongside the existing three refusal reasons:

```
     for each event:
       if the task's linear_task_state row has diverged = true:
           refuse, reason="EXTERNALLY_MODIFIED"
       ... existing operation-specific checks unchanged ...
```

The flag is read from `linear_task_state`, not from the task row, and undo never
writes that table. Restoring a task restores domain state only; its integration
state is already there under the same `task_id`, per D-26.

Precheck-all-then-apply is unchanged. The apply pass is unchanged and is still a
single local transaction. Undo does not call Linear.

### 4.5 Section 10, the six tools

The tool count does not change, and no tool is renamed. Linear project
membership, status, labels, and assignee are fields on a task, reached through
the existing `update_task` and `bulk_update_tasks`.

Fields added to the update argument models:

| Field | Type | Note |
|---|---|---|
| `status` | Enum, built at startup | Members come from the demo team's workflow states |
| `labels` | list of Enum, built at startup | Replaces the list rather than appending. Replacement is what the approval diff can preview cleanly. |
| `project` | Enum, built at startup | Linear project membership |
| `assignee` | Enum, built at startup | Members of the demo team |

Enum members are built at startup from the live workspace, per section 2 of this
document. The model works in names. `linear.py` resolves name to id. No tool
argument ever carries a raw UUID, and no field accepts an arbitrary string.

Do not add `create_project`. Project assignment is the operation the demo needs;
project creation adds an approval case and an eval case for no demo value.

### 4.6 Section 11, tests

Two candidate additions:

```
✓ mutation against a diverged task                → rejected
✓ undo with a diverged task in the run            → refused, EXTERNALLY_MODIFIED
```

Both are offline already, because both read a local flag and raise without
touching Linear. Neither requires `FakeTracker`.

**Whether they become named invariants is T00L's decision to make explicitly,
under D-29, not a number to edit.** D-19 fixed the count at thirteen and set the
precedent of covering coordination cases in a gate instead. The reconciler
coordination proof goes to T28's gate for exactly that reason. If T00L concludes
these two are genuine trust-boundary invariants, it records that D-19's count is
superseded and why, and updates every reference to "thirteen" here and in D7 of
`docs/PROJECT_PLAN.md`. If not, they go to a gate and the count stands.

`test_contract.py` is new, marked, network-dependent, and excluded from CI on
the same grounds as `test_evals.py` under D-09.

### 4.7 Section 12, task list

See section 8 of this document for the corrected sequence and the reason it
differs from the placement discussed earlier in the session.

---

## 5. `docs/ARCHITECTURE.md` deltas

**Part 2, data model:** add the `linear_task_state` and `linear_projections`
tables, with a sentence stating that the trigger, not application code, writes
the projection row, and a sentence stating that `tasks` gains no columns and
`linear_task_state` is a tombstone with no foreign key. Also carry the revised
only-writer wording from D-26 into `CLAUDE.md` and the `domain.py` module
docstring.

**Part 4, layered defenses table:** add a row.

| Layer | Protects against |
|---|---|
| Divergence refusal | acting on state a human changed outside the system |

**Part 5, tools table:** note that `update_task` and `bulk_update_tasks` carry
status, labels, project, and assignee, with enum members built at startup.

**Part 8, frozen stack:** the diagram gains one line below the domain services
layer, and the cut list gains nothing. Linear is not a framework addition, it is
a projection target.

```
Domain services (only writer)
        │
Postgres: tasks | task_events | agent_runs | tool_invocations | approvals
                | linear_projections | linear_task_state
        │
projector (async, after commit)  →  Linear
reconciler (poll, read only)     ←  Linear
```

**Part 6, seed fixture:** `POST /api/demo/reset` now resets both sides. It fences
the projector per D-28, archives everything in the demo team, clears queued
projections and `linear_task_state` tombstones, recreates the eleven fixture
tasks locally, projects them, and releases the fence. Roughly twenty five API
calls per reset. The fence is not optional: without it the reset races a
background worker on the same machine, on the control you press between takes.

**Part 10, cut order:** insert Linear projection between the current items 1 and
2, so it is cut before the resume affordance but after the OTel viewer. If it is
cut, the demo runs on the local board and the README describes the projection
design. The never-cut list does not change.

**Part 11, demo script:** the 3:00 and 7:00 beats gain a variant worth
rehearsing. Filippo edits an issue in Linear directly, then asks the agent to
act on it. The agent refuses and names the issue. This is the divergence
refusal, and it is a stronger demonstration than an unexplained `psql` edit
because the external actor is visibly a human in a real product.

---

## 6. `docs/PROJECT_PLAN.md` deltas

D7 changes from thirteen deterministic tests to fifteen.

The RAID log gains one risk: a live external dependency in the demo path.
Mitigation is the local board rendering committed state independently, so a
Linear outage degrades the demo to a narrated projection queue rather than a
failure. Gate B on day one is the early detection.

---

## 7. What is deliberately not being built

Worth stating in the README alongside the existing exclusions, because each of
these is a question a reviewer may ask.

No provider abstraction. There is one external system and it is Linear.
`linear.py` is a client, not an interface with one implementation. `FakeTracker`
exists for offline tests, not to suggest Jira could be dropped in.

No webhooks. Polling with a reconciliation cursor is sufficient at demo scale.
Webhooks are an event delivery design choice rather than an optimization, and
they become worth it if low-latency inbound sync or dropping the cursor becomes
a requirement. Neither is true here.

No OAuth. A personal API key is sufficient for a single hardcoded actor. Note
that Linear personal keys are user-scoped rather than team-scoped, so the demo
team restriction is a policy check in our code and not an API guarantee. Say
that out loud rather than implying the key is scoped.

No bidirectional field sync. The reconciler flags and imports. It never merges.

Not built as a Linear Agent. Linear's own agent platform installs an agent into
a workspace as a member that replies in comment threads. Building there would
mean losing the assistant-ui chat, the approval card and its diff preview, the
Run Inspector, and the undo control, which are four of the ten acceptance
criteria including two must-haves. A comment thread cannot show a diff preview
or a deduplicated retry.

---

## 8. Task sequence, ratified

Earlier in this session the schema delta was placed in T01 and the policy change
in T04. That was wrong, and the correction matters. T00B later completed after
T06 and before T07. The core sequence through T25 now finishes before the
remaining Linear expansion begins, so the delta ships as optional post-T25 work.

| ID | Task | Model | Files | Done when |
|---|---|---|---|---|
| T00B | Gate B: Linear API probe, completed after T06 | **OPUS ONLY** | `scripts/linear_probe.py`, `tests/fixtures/linear_contract.json`, `tests/test_contract.py`, `tests/fakes.py`, `docs/DECISIONS.md`, `docs/PROJECT_PLAN.md`, BUILD_SPEC section 12 row, `CLAUDE.md` sources-of-truth line | Six facts recorded, fixture written, GATE B PASS |
| T00L | Linear boundary retrofit, after T25 | **OPUS ONLY** | `migrations/002_linear.sql`, `errors.py`, `policy.py`, `sql.py`, `undo.py`, `models.py` if needed, `tests/test_invariants.py`, BUILD_SPEC sections 3, 4, 6, 11, `docs/ARCHITECTURE.md` parts 2 and 4 | Invariant suite passes at whatever count D-29 concludes, no network |
| T26 | Linear client and name to id resolution | SOL | `linear.py`, `config.py`, BUILD_SPEC section 10, `docs/ARCHITECTURE.md` part 5 | Enums build from the live workspace; `FakeTracker` satisfies the same contract |
| T27 | Projector worker | SOL | `projector.py`, `docs/ARCHITECTURE.md` part 8 | Outbox drains in order, serialized per task, remote id written back atomically with completion, unmapped updates completed without a remote call, retry with backoff |
| T28 | Reconciler | SOL WRITES, OPUS REVIEWS | `reconciler.py`, `docs/ARCHITECTURE.md` parts 10 and 11 | External edit sets `diverged`; archived issues excluded; a pending projection does not cause divergence; an issue created in Linear imports |
| T29 | Linear-aware reset | SOL | `seed.py`, `main.py`, `docs/ARCHITECTURE.md` part 6 | Reset fences the projector, archives the team, clears tombstones, and recreates eleven on both sides |

**Documentation is part of every one of these tasks, not a follow-up.** An
earlier draft wrote out the BUILD_SPEC, ARCHITECTURE, and PROJECT_PLAN deltas
without assigning any of them to a task, which would have let the code become
correct while the permanent specification stayed wrong. The doc files above are
in each task's file list for that reason. A task whose code lands without its
documentation block is not done.

Ordering, with reasons.

**T00B remains where it completed, after T06 and before T07.** Its GATE B PASS is
the prerequisite for every remaining Linear task. It is not rerun merely because
the optional expansion starts later.

**T00L and T26 through T29 run after T25 only.** The exact order is
`T25 -> T00L -> T26 -> T27 -> T28 -> T29`. T00L carries the migration and the
boundary retrofit, including the explicit second edit to merged KERNEL
`undo.py`. T26 consumes that foundation, T27 consumes the client, T28 consumes
projected state, and T29 resets the complete integration.

The core Trellis demo therefore reaches its clean-clone and rehearsal bar before
optional external integration work begins. If the Linear expansion is cut, none
of T00L or T26 through T29 runs.

Estimated cost for T00B, T00L, and T26 through T29 together: about a day and a
half. Paid for from the STRETCH items first, then cut order item 1, the external
OTel viewer, keeping the instrumentation. If more is needed, evals drop from
fifteen cases to ten and the bakeoff from ten prompts to five. Nothing on the
never-cut list is touched.
