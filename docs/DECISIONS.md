# Decision Record

Append only. A decision recorded here is closed. Reopening one requires implementation evidence that an assumption was wrong, not a better idea.

---

## Settled before implementation

| # | Decision | Rationale |
|---|---|---|
| D-01 | Pydantic AI as the agent runtime, not LangGraph | Matches the FastAPI and Pydantic stack; native deferred-tool approval and AG-UI integration; avoids a second state store competing with our own Postgres records |
| D-02 | `tasks` is authoritative, `task_events` is an audit log | Full event sourcing adds cost with no interview benefit. "tasks is what is true, task_events is how it became true" |
| D-03 | DBOS and all durable execution engines cut | Runs are resumable at tool boundaries, not automatically recoverable. The limitation is a talking point, not a gap |
| D-04 | Signature reliability moment is the lost-response retry, not crash recovery | Idempotency prevents duplicate mutations; it does not resume interrupted reasoning. Demoing crash recovery without real durability would be dishonest |
| D-05 | Server owns message history | Client-supplied history can be fabricated, including fake tool calls and approvals. The client may send only a user message or a decision |
| D-06 | Framework approval is a UI gate; the `approvals` row is the authority | `policy.check` re-verifies the stored approval inside the tool body on every path, including the approved one |
| D-07 | `agent_runs.id` is the application run | A continuation after an approval is a new agent invocation under the same record. The two are not the same thing |
| D-08 | Runtime model failover cut; circuit breaker cut | One user, ten minutes. Build-time model swap via `MODEL_ID` covers the real risk |
| D-09 | Invariant tests and behavioral evals are separate suites | Invariants: deterministic, no LLM, CI-gating, 100%. Evals: model-dependent, on demand, threshold |
| D-10 | Undo is single-run only, refuses under concurrent change | Partial undo is worse than no undo. The refusal is the interesting half |
| D-11 | Exactly two models build this: Claude Opus 5 and Sol 5.6 | Routing table in BUILD\_SPEC section 1A |

---

## Model review exceptions

On 2026-08-10, the user authorized Terra to perform blind, read-only reviews of the T00 and T00A PRs while Opus usage is unavailable. Terra receives only the task specification, commit diff, and verification evidence. It may report evidence-backed findings but may not edit, generate, or commit repository content. This does not replace the required later Opus review and does not authorize Terra for implementation.

Terra completed the T00A blind review of implementation commit `5d3ab47` on 2026-08-10 and reported no findings. Opus review remains required when credits are available.

On 2026-08-11, the user designated Terra as the default neutral, blind, read-only reviewer for Codex- or Sol-produced pull requests. Terra receives only the task specification, commit diff, and verification evidence, and may report findings but may not edit, generate, stage, or commit repository content. This supersedes the earlier T00 and T00A-only scope for Terra reviews. It does not replace Opus on tasks whose routing explicitly requires Opus review.

On 2026-08-11, the user designated Claude Sonnet as the default neutral, blind, read-only reviewer for Claude-produced pull requests whose implementation model is Opus. Sonnet receives only the task specification, commit diff, and verification evidence, and may report findings but may not edit, generate, stage, or commit repository content. This reviewer assignment does not weaken any explicit routing requirement.

---

## Local environment decisions

On 2026-08-11, the user selected host port `55432` for Trellis PostgreSQL to avoid conflicts with an unrelated local PostgreSQL container that publishes `5432`. PostgreSQL still listens on `5432` inside the Compose network. Host application processes use `DATABASE_URL=postgresql://trellis:trellis@localhost:55432/trellis`.

---

## API facts confirmed at T00

Confirmed on 2026-08-10 using Python 3.12.13, `pydantic-ai` 2.27.0,
`pydantic-ai-slim` 2.27.0, and `ag-ui-protocol` 0.1.19.

| # | Fact | Confirmed value | Date |
|---|---|---|---|
| 1 | Deferred approval type and import names | Import `DeferredToolRequests`, `DeferredToolResults`, `ToolApproved`, and `ToolDenied` directly from `pydantic_ai`. | 2026-08-10 |
| 2 | Tool approval parameter name | Register an always-gated tool with `requires_approval=True` on `@agent.tool` or `@agent.tool_plain`. The framework defers before entering the tool body. | 2026-08-10 |
| 3 | Message history serialization and restore | Serialize with `ModelMessagesTypeAdapter.dump_json(messages)` and restore with `ModelMessagesTypeAdapter.validate_json(payload)`. The serialized JSON array can be stored in `agent_runs.message_history` as `jsonb`. | 2026-08-10 |
| 4 | How conditional approval is expressed | `requires_approval` accepts only a boolean. Passing a callable was tested: the callable was never invoked and its truthiness made the tool always defer. For conditional approval, raise `ApprovalRequired` from the tool when the arguments cross the threshold and guard the raise with `not ctx.tool_call_approved`. The probe confirms 3 task ids execute directly and 4 defer when the threshold is 3. | 2026-08-10 |
| 5 | AG-UI resume request shape | Send a new POST to the same AG-UI route with the same `threadId`, a new `runId`, and `resume: [{"interruptId":"int-<tool_call_id>","status":"resolved","payload":{"approved":true}}]`. Use `approved:false` for denial. Real `AGUIAdapter` streams confirm both paths emit an interrupt, accept this continuation, and finish successfully. The exact route path is fixed by T00A. | 2026-08-10 |
| 6 | Whether `tool_call_id` survives the continuation | Yes. A real `AGUIAdapter` run emits interrupt id `int-<tool_call_id>` and maps the continuation back to the original call. The resumed run emits `TOOL_CALL_RESULT` for that id without a second `TOOL_CALL_START`; approval executes the tool body once with the original `ctx.tool_call_id`, while denial does not execute it. | 2026-08-10 |

---

## Gate A: AG-UI interrupt path (T00A, Day 1)

**Result:** GATE A: PASS

**Frontend runtime floor:** Node 22 or newer on a release supported by the locked graph, currently `^22 || ^24 || >=26`. The selected assistant-ui packages transitively install `nanoid` 6.0.1 with that engine contract. The T00A graph happened to install, build, and pass its original browser proof on Node 20.20.2 while emitting an unsupported-engine warning. That observation is retained as compatibility evidence, but production setup, package metadata, and CI use the supported Node 22+ floor.

**Browser verification readiness:** The T00A workbench polls `/spike-state` every 400 ms to expose tool execution and request evidence. A browser can therefore never satisfy Playwright's `networkidle` condition by design. Automated verification waits for DOM readiness, then asserts explicit visible controls, streamed content, interrupt fields, continuation content, request bodies, and server execution state.

**HTTP method and path for the AG-UI transport:** The assistant-ui `HttpAgent` sends `POST /ag-ui` in the disposable spike. The production path remains `/api/agui` as declared by the wire contract. The JSON body is an AG-UI `RunAgentInput` with:

```json
{
  "threadId": "<conversation id>",
  "runId": "<new invocation id>",
  "tools": [],
  "context": [],
  "forwardedProps": {},
  "state": null,
  "messages": [
    { "id": "<message id>", "role": "user", "content": "<prompt>" }
  ]
}
```

The continuation is a new POST with the same `threadId`, a new `runId`, the assistant-ui message transcript, and:

```json
{
  "resume": [
    {
      "interruptId": "int-delete-spike-item-42",
      "status": "resolved",
      "payload": { "approved": true }
    }
  ]
}
```

Denial uses the same shape with `"approved": false`.

**Interrupt payload shape:** The adapter emits `RUN_FINISHED` with this outcome after the `TOOL_CALL_START`, `TOOL_CALL_ARGS`, and `TOOL_CALL_END` events:

```json
{
  "type": "interrupt",
  "interrupts": [
    {
      "id": "int-delete-spike-item-42",
      "reason": "tool_call",
      "message": "Approve delete_demo_item({\"item_id\": \"demo-task-7\"})?",
      "toolCallId": "delete-spike-item-42",
      "responseSchema": {
        "properties": {
          "approved": { "type": "boolean" },
          "editedArgs": { "type": "object" },
          "reason": { "type": "string" }
        },
        "required": ["approved"],
        "type": "object"
      }
    }
  ]
}
```

**Observed proof:** A normal response rendered from three streamed `TEXT_MESSAGE_CONTENT` events. Before either decision the tool execution count was 0. Approval continued the original `delete-spike-item-42` call and produced exactly one tool-body execution. After reset, denial continued the same identifier shape and left the execution count at 0. The browser rendered all four protocol stages as PASS.

**Symptom, if FAIL:** Not applicable. The native interrupt path passed.

**Consequence if FAIL:** chat continues to stream over AG-UI; approvals move to `GET /api/runs/{id}` plus `POST /api/runs/{id}/approvals/{tool_call_id}`, with the approval card rendered from run state. T12B's seven proofs apply to the fallback unchanged.

---

## Gate C: Day 2 seam checkpoint

**Result:** PENDING

If Next.js, AG-UI, FastAPI, and Pydantic AI are not talking end to end by end of Day 2, collapse to AI SDK plus a plain FastAPI domain API. This decision does not slide to Day 3.

---

## Runtime model selection (Day 4 bakeoff)

**Result:** PENDING

Ten prompts, both candidates, scored on correct tool behavior, clarification where appropriate, latency, and cost. Table goes in the README when complete. `MODEL_ID` is set from this, not from reputation.

---

## Cuts taken during the build

| Day | Cut | Position in cut order | Reason |
|---|---|---|---|
| | | | |

---

## Hardening decisions recorded at T00R

Recorded on 2026-08-12 after an audit of the completed T00 and T00A work. Each
one closes a consequence that was discovered earlier but never written down. No
existing decision above is amended.

### D-12: conditional approval raises at tool step 0

API fact 4 established that `requires_approval` accepts only a boolean, so
`delete_tasks` gates declaratively while `bulk_update_tasks` must raise
`ApprovalRequired` from inside its own tool body, guarded by
`not ctx.tool_call_approved`. Three consequences follow, and all three are
binding.

The premise stated in BUILD_SPEC section 6, that the agent framework gates an
approval-required call before the tool function runs, holds only for
`delete_tasks`. On the conditional path the body does run, as far as the raise,
and then runs again after approval.

The raise is therefore step 0 of the tool body, ahead of `arguments_hash` and
ahead of `idempotency.acquire`. If it sits anywhere after lease acquisition, the
first deferring pass acquires a lease it never completes, and the approved
continuation then fails against its own lease with `LEASE_IN_FLIGHT`.

Every mutating tool carries the identical step 0:

```python
requirement = policy.classify(tool_name, arguments, len(target_ids))
if requirement.required and not ctx.tool_call_approved:
    raise ApprovalRequired(metadata={"reason": requirement.reason})
```

It is inert for ungated tools, and for `delete_tasks` the framework gate fires
first so step 0 is never reached. The identical five-step body in section 10
therefore still holds, with this step in front of it. `policy.py` needs no
change: `classify` is already pure and correct for both paths.

Actor scope is resolved before the raise, never after. An `ApprovalRequired`
raised for target ids the actor does not own would build an approval card
describing another actor's rows, which is exactly the disclosure T12B prohibits.

### D-13: drop the T00A required check before the spike is deleted

BUILD_SPEC requires deleting `spike/` before T12A, and `T00A spike build` is a
required status check on `master` with admin enforcement enabled. A T12A pull
request that deletes the directory would fail its own required check, because
`npm ci` would run against a path that no longer exists, and branch protection
cannot be edited from inside that pull request.

The order is fixed: remove `T00A spike build` from master branch protection
first, then delete the CI job and the `spike/` tree in the T12A pull request.
Strict up-to-date checks, admin enforcement, conversation resolution, the GitHub
Actions app bindings, and the force-push and deletion restrictions are all
preserved while doing it.

### D-14: `backend/requirements.txt` is the single backend pin source

Every backend job in `.github/workflows/ci.yml` installs from
`backend/requirements.txt`. Inline `pip install` pin lists in the workflow are
not permitted for backend jobs.

The reason is that the T00 probe's pins previously lived only in the workflow.
Bumping an application dependency elsewhere would have left the probe proving
API facts for a version the application no longer installed, while
`docs/DECISIONS.md` continued to present those facts as current. Facts are only
worth recording if the thing that proves them runs against the versions actually
in use.

The cost is accepted deliberately: each backend job installs the whole pinned
set, so jobs are slower and share one install failure surface. The exchange is
that every gate now runs against the real application environment.

`spike/backend/requirements.txt` is the one exception and keeps its own pins.
That tree is disposable and is deleted at T12A under D-13, so coupling it to the
production pin source would only create something to unwind.

---

## Contract corrections recorded at T04

Recorded on 2026-08-12, before any T04 kernel code was written. Each one closes
a gap where BUILD_SPEC section 6 requires an operation it provides no means to
perform. No existing decision above is amended, and `docs/BUILD_SPEC.md` is not
edited, following the precedent D-12 set when it corrected the stated premise of
section 6 without rewriting it.

### D-15: `policy.check` takes `run_id` and `tool_call_id` as required keyword arguments

Section 6 step 5a requires `check` to verify that the approval row matches the
current call:

```
5. VERIFY APPROVAL, in this order:
   a. approval_row.run_id and tool_call_id match the current call,
      else APPROVAL_NOT_FOUND
```

The signature printed in section 6 and the call site printed in section 10 pass
neither value:

```
check(actor_id, tool_name, arguments, target_task_ids, approval_row)
```

Step 5a is therefore unimplementable as printed. The tool body holds both
values already: section 10 states that every tool takes `ctx` carrying
`actor_id` and the application `run_id`, and `tool_call_id` is
`ctx.tool_call_id`. The call site simply does not forward them.

The signature is:

```python
check(
    actor_id,
    tool_name,
    arguments,
    target_task_ids,
    approval_row,
    *,
    run_id,
    tool_call_id,
) -> PolicyDecision
```

All five positional parameters keep their specified names and order. `run_id`
and `tool_call_id` are required keyword arguments, so no caller can silently
skip step 5a. The call site printed in section 10 is incomplete and every caller
must forward both.

**What step 5a is and is not.** It is defense against a caller passing the wrong
approval row. It is not the primary protection against a forged approval. In
production the caller loads the row with `SELECT_APPROVAL`, which is keyed on
`(run_id, tool_call_id)`, so a returned row matches by construction and 5a can
essentially never fire. Forgery is closed by step 4, which raises
`APPROVAL_REQUIRED` when no row exists, and by step 5b, which rejects a row
whose `arguments_hash` does not cover the arguments actually being executed. Do
not describe 5a as the forgery gate.

**`APPROVAL_REQUIRED` and `APPROVAL_NOT_FOUND` are not interchangeable.**
`approval_row is None` at step 4 raises `APPROVAL_REQUIRED`, HTTP 202.
`APPROVAL_NOT_FOUND`, HTTP 403, belongs to step 5a alone, where a non-null row's
`run_id` or `tool_call_id` does not match the current call.

**`approval_row` is `models.Approval | None`**, built with
`Approval.model_validate(row)` from a `SELECT_APPROVAL` result. Note that
`Approval.preview` is `ApprovalPreview` with `extra="forbid"`, so a stored
preview must carry exactly `creates`, `updates`, and `deletes`.

**No clock parameter.** `check` reads the production clock for step 5c. Expiry
is exercised by writing an `approvals` row that is already expired, which
`INSERT_APPROVAL` produces from a substantially negative
`approval_ttl_seconds`, and the test confirms the loaded row is expired before
invoking `check` so a silently failed fixture cannot produce a false pass.
Adding an injectable `now` to a correctness-kernel file to simulate something
the database clock already does is rejected. The same technique covers T05,
whose theft guard evaluates `lease_expires_at < now()` server side inside the
UPDATE.

### D-16: T04 test-authorship routing exception, and the intended chain

T04 is the first task where one pull request would contain work owned by two
models. Section 11 routes `tests/test_invariants.py` as SOL WRITES, OPUS
REVIEWS. Section 12 tags the T04 kernel files OPUS ONLY and defines T04's
completion as six of those same tests passing. The two assignments meet inside
one task.

**The exception.** On 2026-08-12 the user authorized Claude Opus 5 to write the
six T04 invariant tests as well as the kernel, for T04 only. Sol was not
reachable for this task, and T04 cannot ship without the tests: section 12's
definition of done is exactly those six passing, and the required T04 status
check has nothing to run without them. Deferring the tests would leave the
kernel merged and unproven, which is worse than the exception.

**What the exception costs, stated plainly.** One model now writes both the
kernel and the tests that judge it. That is the pairing section 11 splits
authorship to prevent, because a self-consistent pair can be green and prove
nothing, and the same misreading of section 6 can be encoded twice. Sonnet's
final review sees that consistent pair rather than two independent readings.
Nothing below removes this. It reduces it.

**Compensating measures. All are required, none is optional.**

1. The tests are written first, against section 6's text, with no kernel file
   in the tree. The resulting collection failure is preserved as the test-first
   ordering evidence, following the precedent set by T02 and T03.
2. Because collection failure means no fixture ever executes under pytest, the
   fixture path is validated separately against live PostgreSQL with a
   throwaway script outside the repository, and that script and its output are
   preserved.
3. Every one of the six tests is shown to fail under at least one single-line
   mutation of the finished kernel, each mutation representing the defect that
   test guards. Mutations are applied one at a time, run, recorded, and
   reverted. The kernel is then verified restored by digest and the full suite
   run green again. No mutation survives into the commit. This is what carries
   the weight the authorship split would otherwise have carried: it proves each
   test detects a specific defect in the code that ships, not merely that it
   fails when nothing exists. Where two scenarios sit in one test as sequential
   assertions, each gets its own targeted mutation rather than one transposition
   covering both, because a test aborts at its first failure and a single
   mutation would leave the later scenario unproven.
4. Sonnet reviews the six tests against BUILD_SPEC section 6 directly, not only
   against `policy.py`, so the review has a reference independent of the
   implementation. The reviewer is given no question list and no area guidance,
   and it runs against a throwaway clone rather than the working tree, so it
   cannot alter the artifact it is reviewing.
5. The T04 CI gate asserts all twelve code and HTTP status pairs from section
   6's table, because the six tests construct only six of them and a transposed
   status on an unexercised code would otherwise surface at T05, in a file T05
   may not edit.

Coverage as delivered: the six tests behaviorally exercise six of the twelve
codes, including both branches of section 6 step 5d. Neither 5d branch is on the
production path, since D-12 step 0 raises before `check` is reached and the
server persists the decision before constructing a continuation, so both are
defense against retries, races, bypasses, and incorrect continuation
sequencing. The remaining six codes are covered by the complete-table check now
and by their owning tasks later, except `MODEL_TIMEOUT`, which no test in
section 11 names and which the table check alone covers.

**Section 11's OPUS REVIEWS half is satisfied vacuously here.** Opus reviewing
tests Opus wrote is not a review. It is recorded as satisfied only because the
authoring model is the one section 11 names as reviewer, and no claim is made
that an independent pre-implementation review occurred.

**Scope.** The exception is T04 only. It does not extend to T05, T07, or T08,
and it is not precedent for any later task.

**The intended chain, for the split kernel tasks that follow.** When Sol is
reachable, the pattern below applies. It is recorded now so it is not
redesigned under time pressure, and it is deliberately not binding, because its
cost against its yield is unmeasured. Confirm or revise it in a later decision
using observed review yield, elapsed time, findings, and round trips.

1. Sol authors its assigned invariant-test slice and validates the database
   fixtures with a throwaway script outside the repository.
2. Terra receives only the applicable specification and decisions, Sol's test
   diff, the fixture-validation script, and its output.
3. Terra performs a blind, read-only component review.
4. Sol resolves Terra's findings.
5. Opus performs the section 11 pre-implementation review of the accepted tests.
6. Opus implements and owns the production kernel and the final package.
7. Sonnet performs the blind, read-only final review of the Opus pull request.

Two properties of that chain are worth recording with it. Terra's assignment
would extend beyond the 2026-08-11 decision's wording, which covers Sol-produced
pull requests, to an uncommitted Sol-authored component inside an Opus-owned
one, because that is the only point at which Sol's work is reviewed as Sol's.
And both pre-implementation reviews are static specification-conformance
reviews: they read tests that cannot yet run, so they assess assertions,
scenarios, and fixture design, and neither establishes runtime behavior.

The full Sol prompt for the six T04 tests was written before the exception was
granted and is preserved outside the repository at
`C:\Users\nicol\trellis-handoffs\T04-sol-invariant-tests-prompt.md`, with notes
on what to change to reuse it for T05's three tests.

**Independence, described narrowly wherever it is claimed.** Even under the
intended chain, D-15 and D-17 fix the signature, the error-code split, the
`approval_row` type, the expiry mechanism, and the assertion style before any
test is written. Sol would independently translate frozen contracts into tests
without having seen `policy.py`. It would not independently choose those
contracts. Do not describe that as a clean-room split.

**Authorship trailers.** None are added to the T04 commit. No verified Git
identity exists for Sol, and inventing one would put a false attribution in the
permanent history. Authorship is recorded in `IMPLEMENTATION_NOTES.md` and the
pull request instead.

### D-17: `policy.check` scope loading

Section 6 step 1 requires `check` to load `owner_id` for every id in
`target_task_ids`, but `check` is passed no connection, CLAUDE.md requires all
SQL to live in `backend/app/sql.py` as uppercase constants, and section 5's
statement list contains nothing that loads owners by a set of task ids.
`SELECT_TASKS_FOR_OWNER` cannot serve: it filters by `owner_id`, has no id
filter, and carries a LIMIT, so it cannot distinguish a missing task from
another actor's task by id.

**One constant is added to `backend/app/sql.py`:**

```sql
SELECT id, owner_id
  FROM tasks
 WHERE id = ANY(%(task_ids)s::uuid[]);
```

**T04's file list is expanded** to include `backend/app/sql.py` under the user's
explicit authorization of 2026-08-12. The alternatives were rejected: inline SQL
in `policy.py` breaks the single-catalog rule in CLAUDE.md and section 5, and
passing preloaded ownership into `check` moves an authoritative kernel check out
to every call site, which is the opposite of what section 6 is for.

**Missing and foreign ids produce the identical `OUT_OF_SCOPE`,** as section 6
requires. A missing id returns no row; a foreign id returns a row whose
`owner_id` differs. Both fail the same comparison and neither is distinguished
in the error.

**An empty `target_task_ids` skips the query and satisfies scope.** Tools such
as `create_task` and `propose_plan` have no targets. The query would be
vacuously satisfied, but relying on that is relying on an accident, so the skip
is explicit.

**The pool is imported lazily, inside the loading function.** `backend/app/db.py`
opens its `ConnectionPool` at import time with `open=True`, so a module-level
import would make importing `policy` require a live database, including for
`classify()` and `arguments_hash()`. Section 6 calls `classify` pure with no
database, and T10 imports it for step 0 of every tool body under D-12. The lazy
import keeps that true.

**`len(target_task_ids)` is preserved without deduplication.** Section 6 step 2
specifies `count = len(target_task_ids)`, so four references to one id count as
four and require approval while touching one row. This is transcription, and it
fails closed. It is not listed in section 14 and will look like a bug later, so
it is recorded here.
