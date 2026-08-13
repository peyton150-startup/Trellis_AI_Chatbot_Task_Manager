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

## API facts confirmed at T00B

Confirmed on 2026-08-13 against `https://api.linear.app/graphql` using Python
3.12.13 and `httpx` 0.28.1, with a personal API key and demo team `TRE` in
workspace `trellis-app-proejct`.

Unlike the T00 table there is no pinned dependency version to record here.
Linear's GraphQL endpoint is unversioned and live, so the date and the observed
shapes below are the whole provenance. That is precisely why `test_contract.py`
freezes the subset this build depends on and fails when it drifts.

| # | Fact | Confirmed value | Date |
|---|---|---|---|
| 1 | Authentication header format | Send the personal API key raw in `Authorization`, with no `Bearer` prefix. `Bearer <key>` is rejected with HTTP 400 and the message "It looks like you're trying to use an API key as a Bearer token. Remove the Bearer prefix from the Authorization header." Omitting the header returns HTTP 401 `AUTHENTICATION_ERROR`. | 2026-08-13 |
| 2 | Workspace object resolution | Query one collection at a time: `teams(filter: { key: { eq: $key } }, first: 1) { nodes { id key name <collection>(first: 50) { nodes { id name ... } } } }`, where `<collection>` is `states`, `labels`, `members`, or `projects`. Requesting all four in one query costs complexity 13315 against a 10000 ceiling and is rejected, so T26 issues one query per collection. Team `TRE` returned 7 workflow states (`Backlog` backlog, `Todo` unstarted, `In Progress` started, `In Review` started, `Done` completed, `Canceled` canceled, `Duplicate` duplicate), 3 labels, 1 member, 1 project, each carrying a UUID. Names are unique within the team for all four collections, so name to id resolution needs no tiebreak. Labels resolve with `team: null` through `issueLabels`, meaning they are workspace scoped rather than team scoped; a workspace that also defines a team label of the same name could collide, which was not observed here. | 2026-08-13 |
| 3 | Mutation shapes for create, update, archive, unarchive | `issueCreate(input: IssueCreateInput!)` and `issueUpdate(id: String!, input: IssueUpdateInput!)` both return `IssuePayload { lastSyncId: Float!, issue: Issue, success: Boolean! }`. `issueArchive(id: String!, trash: Boolean)` and `issueUnarchive(id: String!)` both return `IssueArchivePayload { lastSyncId: Float!, success: Boolean!, entity: Issue }`. Of the fields this build sets, `IssueCreateInput` carries `id: String` (client supplied and honoured), `teamId: String!`, `title: String`, `description: String`, `stateId: String`, `priority: Int`, `dueDate: TimelessDate`, `assigneeId: String`, `labelIds: [String!]`, and `projectId: String`; `IssueUpdateInput` carries the same set minus `id` and with `teamId: String` nullable. `dueDate` is a date with no time component. `labelIds` replaces the list rather than appending. `Issue.priority` reads back as `Float!` while the input is `Int`. `issueArchive` then `issueUnarchive` round-trips: `archivedAt` is set and then cleared, and title, workflow state, and priority all survive. `issueDelete(id, permanentlyDelete)` exists and this build never calls it. | 2026-08-13 |
| 4 | Change detection | Poll with `issues(filter: { team: { key: { eq: $key } }, updatedAt: { gt: $since } }, first: N, orderBy: updatedAt)`, where `$since` is `DateTimeOrDuration!`. Pagination is `pageInfo { hasNextPage endCursor }` with an opaque cursor. `updatedAt` moved for every field this build sets, tested one field at a time: title, description, priority, dueDate, stateId, assigneeId, labelIds, and projectId. Clearing moves it too, measured separately because setting a field says nothing about clearing it: `labelIds: []` and `projectId: null` each emptied the field and bumped `updatedAt`. Those are the two clear shapes Linear honours, and T26 and T28 should use them. Both clear cases read the field back and confirmed it was genuinely empty before judging the timestamp, so a mutation that silently did nothing cannot be recorded as a field that fails to bump `updatedAt`. Archived issues are excluded from the default query and are returned only with `includeArchived: true`, which is what D-27 requires so a deleted task does not resurrect through the import path. After our own mutation `updatedAt` moves and the mutation's own response payload already carries the new value, with a read-back immediately afterwards agreeing; observed mutation round trips were 0.23 to 0.68 seconds. | 2026-08-13 |
| 5 | Key scope and limits | The personal API key is user scoped, not team scoped. It resolves `viewer` and `organization` and enumerates `teams(first: 50)` with no team parameter anywhere in the request, so the demo team restriction is a policy check in our code and not an API guarantee. Say that out loud in the README rather than implying the key is scoped. Observed response headers were `x-ratelimit-requests-limit: 2500` and `x-ratelimit-complexity-limit: 3000000`, each with a `-remaining` and `-reset` companion. Per D-29 the numbers are an observation and not an architectural constant; the conclusion this build depends on is that they are comfortably above demo and rehearsal usage, which costs roughly 25 calls per reset. | 2026-08-13 |
| 6 | Delivery deduplication | No. All 361 mutations were scanned and none carries an argument advertising idempotency, replay, or deduplication semantics. The only client-controlled identifier is `IssueCreateInput.id`, and replaying `issueCreate` with an id that already exists is rejected with HTTP 400 `INPUT_ERROR` and the message "Entity Issue with id `<uuid>` already exists" rather than returning the original issue. That is a uniqueness constraint and not a replay guarantee, so it does not count under D-25 and the local `UNIQUE(event_id)` constraint is the only deduplication layer, which D-25 states is sufficient. The consequence for T27 is that a retry after an ambiguous create receives an error rather than the issue, so the projector must treat that specific conflict as evidence the create already landed and recover the remote id by query, or it will record a delivered create as failed. | 2026-08-13 |

Two things this table deliberately does not claim.

**The archived recovery boundary is unestablished.** Fact 3 asked what happens
when an archived issue is too old to recover. The probe archived and unarchived
within seconds and never approached a boundary, and none was encountered. No
value is recorded rather than a guessed one, because D-25 forbids inventing a
fallback to create until the behaviour is confirmed. `restored` maps to
`unarchive` unconditionally, and if a boundary is later found this row is where
the correction goes.

**Archiving does not move `updatedAt`.** Observed directly: an `issueArchive`
left `updatedAt` unchanged. Archived issues leave the default poll instead of
appearing as a modification, so a human archiving an issue in Linear is not
detectable by the fact 4 query. T28 cannot use `updatedAt` alone to notice
external archival, and a task whose issue vanishes from the poll is not the same
signal as a task whose issue changed.

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

## Gate B: Linear API (T00B)

**Result:** GATE B: PASS

**The three failure criteria, each checked directly.** Gate B fails if the demo
team's workflow states, projects, labels, or members cannot be enumerated for
enum construction; if `issueArchive` and `issueUnarchive` do not round-trip; or
if issues changed by another actor cannot be detected by an `updatedAt` query.
None of the three holds.

Enumeration works for all four collections, one query per collection, and every
name is unique within the team so resolution needs no tiebreak. Archive and
unarchive round-trip with `archivedAt` set and then cleared and the issue's
fields intact. An `updatedAt` filter detects a change to every field this build
sets, including the label and project changes that fact 4 singled out as the
most likely place for the design to fail.

**Workspace the gate ran against:** organization `trellis-app-proejct`
(`f798e328-66df-4eee-a99a-27bf8bcb3667`), team `TRE`
(`49744eb7-0013-4b96-8e32-d4649e59642f`). This workspace holds no TAD project
tickets; it contained only Linear's four stock onboarding issues before the
probe ran, and the probe touched none of them.

**Demo readiness item found and closed.** The team had zero projects when the
gate first ran. Enumeration succeeded and returned an empty list, which is not a
Gate B failure, but the `project` enum T26 builds at startup would have had no
members and the demo beat that moves an issue between projects would have been
unrunnable. One project, `Trellis Demo`
(`0ac3eb70-b897-4cf9-8675-1ac091e5902e`), was created on 2026-08-13 under
explicit user authorization and left in place. Fact 4's project case was then
confirmed against it. The probe now fails fact 2 with a named message if the
demo team has no projects, so this cannot silently regress.

**Observed proof:** `python -B backend/scripts/linear_probe.py` against team
`TRE` printed `PASS` for all six facts and the closing line
`ALL 6 LINEAR API FACTS CONFIRMED`. Every fact is reproduced in the T00B table
above with the query or mutation that established it. The probe writes only to
the demo team, archives every issue it creates, and never calls `issueDelete`.
After the run the team held the same four onboarding issues it started with.

**Symptom, if FAIL:** Not applicable. All three criteria passed.

**Consequence if FAIL:** Linear is cut. The demo runs on the local board, the
projection design goes in the README as the integration shape that was designed
but not built, and T00L and T26 through T29 are never written. No workaround is
attempted; Gate A had a designed fallback and this gate has one too.

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

---

## Contract corrections recorded at T05

Recorded on 2026-08-12, before any T05 kernel code was written. Each closes a
gap where BUILD_SPEC section 7 requires an operation it provides no means to
perform. No existing decision above is amended, and `docs/BUILD_SPEC.md` is not
edited, following the precedent D-12 set and D-15 through D-17 continued.

### D-18: `idempotency.complete` takes the caller's connection as a required keyword argument

Section 7 states the ordering requirement as non-negotiable:

```
the domain mutation and its task_events rows and the complete() call all
happen inside one database transaction
```

Section 10 step 4 shows the same thing:

```
4. with transaction:
       result = domain.<operation>(...)
       domain.write_events(...)
       idempotency.complete(run_id, tool_call_id, result)
```

The signature printed in section 7 passes no connection:

```
complete(run_id, tool_call_id, result) -> None
```

`backend/app/db.py` opens a `ConnectionPool` and every `pool.connection()` call
yields a distinct connection with its own transaction. As printed, `complete`
would therefore commit in a transaction of its own, which is exactly the window
section 7 says must not exist: the mutation could commit while the lease did
not, or the reverse.

The signature is:

```python
complete(run_id, tool_call_id, result, *, conn) -> None
```

All three printed positional parameters keep their names and order. `conn` is
required and keyword-only, so no caller can silently commit the lease
separately. `complete` does not commit; the caller owns the transaction, and
committing inside would end it early and reintroduce the same window. An
optional `conn` was rejected because a caller who forgot it would get precisely
the broken behavior, which fails open rather than closed. A module-level
transaction context manager inside `idempotency.py` was rejected because section
7 never asks this file to own transaction management, and it would hide the
connection in module state.

**`acquire` and `fail` keep their printed signatures and open their own
connections.** This is not an inconsistency, it is the semantics. `acquire` must
commit its `INSERT_LEASE` immediately or no concurrent retry can ever conflict
with it, and a lease no one else can observe is not a lease. `fail` runs after
the domain transaction has already rolled back, so there is no transaction left
for it to join.

### D-19: the two guarded lease statements, and T05's file list

Section 7 requires two reacquires, and is explicit that the predicate belongs
inside the UPDATE:

```
Never take a lease without the guard in the UPDATE statement itself:
lease_expires_at < now() when stealing an expired lease, status = 'failed'
when reacquiring a failed one. A read-then-write without the guard is the bug,
not a style preference. Only the retry whose UPDATE touches a row may execute.
```

Section 5's statement list contains `INSERT_LEASE`, `SELECT_LEASE`,
`COMPLETE_LEASE`, and `FAIL_LEASE`. None carries either guard and none can be
adapted, because `COMPLETE_LEASE` and `FAIL_LEASE` are unconditional writes.
CLAUDE.md and section 5 both forbid a SQL string anywhere except
`backend/app/sql.py`.

**Two constants are added to `backend/app/sql.py`:**

```sql
REACQUIRE_FAILED_LEASE   guarded on status = 'failed'
STEAL_EXPIRED_LEASE      guarded on lease_expires_at < now()
```

Both use `RETURNING *`, so ownership is decided by whether a row came back.
Exactly one racing caller can receive one, and only that caller may execute.

**T05's file list is expanded** under the user's explicit authorization of
2026-08-12 to `backend/app/idempotency.py`, `backend/app/sql.py`, the applicable
portion of `backend/tests/test_invariants.py`, `.github/workflows/ci.yml`,
`IMPLEMENTATION_NOTES.md`, and this file. The last two are the required
companion files CLAUDE.md already names for every task.

**What the guards can and cannot be shown to do.** They are concurrency
controls. The three tests section 11 names for T05 are sequential, so none of
them can construct the race the guards defend against: a caller whose SELECT
observed one state while a competitor changed the row before its UPDATE ran.
What is provable without concurrency is that the predicate sits inside the
UPDATE rather than in a preceding SELECT, and the T05 CI gate proves it by
running each statement directly against a row it must refuse and a row it must
take. Do not describe the three tests as proving the guards.

### D-20: T05 test-authorship routing exception

D-16 scoped its exception to T04 alone and said so explicitly. T05 therefore
needed its own. On 2026-08-12 the user granted a fresh exception, scoped to T05
alone, under which Claude Opus 5 writes the three T05 invariant tests as well as
the kernel.

The reasoning differs from D-16's. At T04 the exception was forced, because Sol
was unreachable. At T05 it is chosen: section 12 tags `idempotency.py` OPUS
ONLY, so the kernel was never Sol's to write, and the compensating evidence
available at T05 is stronger than the authorship split it replaces. The
execution pass reproduces the mutation table independently, which is the one
control no static reviewer at T04 could provide.

The cost is unchanged and is restated rather than diminished: one model writes
both the kernel and the tests that judge it, a self-consistent pair can be green
and prove nothing, and the final blind review sees that pair rather than two
independent readings. D-16's compensating measures 1 through 4 apply unchanged.
Measure 5 becomes the T05 gate's guard check. The exception is T05 only and is
not precedent for T07 or T08.

### D-21: the D-16 chain is revised from seven steps to five

D-16 recorded the intended seven-step chain as deliberately not binding, and
asked for it to be confirmed or revised once T04 supplied evidence on its cost
against its yield. T04 has now supplied that evidence, and it does not support
the chain as written.

**What the evidence says.** T04 ran two blind static reviews. Both returned no
findings, and neither surfaced anything the author had not already found. Each
read on the order of 150k tokens. Over the same period, the defects that were
actually found in T04's tests were found by designing mutations: the suite was
one-sided with every assertion a rejection, so nothing exercised section 6 step
6 and a `check` rejecting every non-null approval row would have passed
unchallenged; the fixture and the kernel computed the stored hash with the same
function, so a broken canonicalization agreed with itself; and neither branch of
step 5d was reached by any test. Four real defects, none found by review. One
reviewer also executed the author's mutation script despite a read-only
instruction, which is why reviews now run against a throwaway clone rather than
the working tree.

**The revision.** Two of the seven steps are static specification-conformance
reviews of tests that cannot yet run, and D-16 already notes that neither
establishes runtime behavior. Section 11 mandates the Opus one. The Terra
component review is therefore dropped, and Sol resolves nothing between them.
The chain becomes:

1. Sol authors its assigned invariant-test slice and validates the fixtures with
   a throwaway script outside the repository.
2. Opus performs the section 11 pre-implementation review of those tests.
3. Opus implements and owns the production kernel and the final package.
4. Mutation evidence is produced for every test in the slice, one single-line
   mutation per defect, applied one at a time, run, recorded, and reverted, with
   the kernel verified restored by digest and the suite green afterwards. This
   is mandatory, not a compensating measure for an exception, because it is the
   step that has actually found defects.
5. A blind, read-only final review of the pull request, against a throwaway
   clone, with an execution pass where the task's behavior is observable only at
   runtime.

**What this trades away, stated plainly.** Sol's work loses its one independent
review as Sol's work, which D-16 identified as the only point where that
happens. The judgment is that a static review which found nothing at T04 is
worth less than the execution pass that replaces it, not that the review had no
value. Reopening this requires evidence that a dropped review would have caught
something, which is exactly the standard this document's preamble sets.

### D-22: a polled lease that becomes failed is reacquired through the guard, not executed directly

Recorded on 2026-08-12 after the blind execution review of commit `406e735`
raised it. It corrects an omission in that commit rather than a defect in its
behavior, and it closes an internal contradiction in section 7 that the commit
resolved silently.

**The contradiction.** Section 7's prose is unambiguous:

> Never take a lease without the guard in the UPDATE statement itself:
> `lease_expires_at < now()` when stealing an expired lease, `status = 'failed'`
> when reacquiring a failed one. A read-then-write without the guard is the bug,
> not a style preference. Only the retry whose UPDATE touches a row may execute.

The pseudocode in the same section prints the opposite for one branch. Inside
the poll loop:

```
otherwise poll: re-SELECT every 250ms, up to 8 times (2s total)
    becomes "completed" -> REPLAY
    becomes "failed"    -> EXECUTE
    still "pending"     -> raise LEASE_IN_FLIGHT
```

Read literally, `becomes "failed" -> EXECUTE` is a read followed by an
unguarded grant, which is precisely what the prose forbids. The two cannot both
be transcribed.

**The resolution.** The prose governs. `_poll` returns `None` when it observes
`failed`, and `_resolve_conflict` routes that back through the switch, where the
guarded `REACQUIRE_FAILED_LEASE` decides which caller wins. The poll block's
`EXECUTE` is read as naming the branch to take, not as a direct return.

**Why the literal reading is unsafe, measured rather than argued.** The review
built the race in a disposable microVM: two callers polling one live lease while
its holder calls `fail()` mid-poll. Against the shipped code, exactly one caller
received EXECUTE and the other received `LEASE_IN_FLIGHT`. Against a build
patched to return EXECUTE directly from the poll, both callers received EXECUTE.
Two concurrent executions of one tool call is the exact failure the module
exists to prevent, and D-04 names this retry as the signature reliability
moment, so the branch is not an edge case.

**What was done wrong, and is corrected here.** Commit `406e735` resolved the
contradiction correctly but recorded it only in an inline code comment, in the
same commit that documented four other corrections as D-18 through D-21. Rule 1
of section 0 and section 1A both require a contradiction to be written down
rather than resolved by picking a side. Recording it here is the correction. A
matching `docs/OPEN_QUESTIONS.md` entry is not added, because that file is
outside the file list authorized for T05.

**Coverage.** The branch is reachable by none of section 11's three T05 tests,
which are sequential, and it was reachable by no gate check as committed. The
T05 gate now exercises it directly: a live lease is failed from a second thread
while `acquire` polls, and the gate asserts the outcome is EXECUTE with
`attempt` incremented to 2 and status back to `pending`. A direct return from
the poll leaves `attempt` at 1 and the row `failed`, so the assertion
distinguishes the two readings rather than merely observing that something
executed. No fourteenth named test is added; section 11 fixes the count at
thirteen, and T04 set the precedent of covering an unreachable case in the gate.

---

## Domain decisions recorded at T06

Recorded on 2026-08-12 after the required Opus execution review. This closes
two gaps where BUILD_SPEC section 5 does not provide the reads T06 needs to
produce complete audit snapshots and account for database-driven mutations.

### D-23: domain snapshot and delete-cascade reads live in the SQL catalog

Section 12 makes `backend/app/domain.py` the only writer of `tasks` and
`task_events`, and defines T06 as a round trip through create, update, and event
reads. Section 8 makes the snapshot requirement stronger than a partial diff:
T07 reads `event.after["version"]` during its precheck and
`event.before["id"]` when restoring a deletion. A complete pre-mutation task row
is therefore part of T06's output contract.

Section 5 provides `UPDATE_TASK_GUARDED`, which returns only the post-update
row. It provides no statement that loads complete target rows by id before a
mutation. `SELECT_TASKS_FOR_OWNER` cannot serve because it accepts filters,
orders for display, and carries a limit of at most 50. `SELECT_TASK_OWNERS`
cannot serve because it returns only `id` and `owner_id`. Inline SQL in
`domain.py` is forbidden by CLAUDE.md and section 5.

**Two constants are added to `backend/app/sql.py`:**

```sql
SELECT_TASKS_BY_IDS_FOR_UPDATE  complete owned target rows, ordered by id, locked
SELECT_TASKS_BLOCKED_BY_IDS     complete owned rows whose blocked_by is a target, locked
```

Both run on the caller's connection and use `FOR UPDATE`, so the authoritative
before snapshot and the following mutation share one transaction. The first
orders by `id`, not request order. PostgreSQL takes row locks in the produced
order, and request order lets two callers with reversed id arrays lock A then B
and B then A. The required Opus execution review reproduced that deadlock. A
canonical id order removed it over 200 concurrent rounds. Domain code keys the
rows by id and replays the caller's distinct id order, so no consumer observes
the lock ordering.

The second statement closes a mutation hidden in the schema. `blocked_by` uses
`ON DELETE SET NULL`, so deleting a blocker rewrites surviving tasks without
calling `UPDATE_TASK_GUARDED`. T06 snapshots and locks the owned referencing
rows before deletion, reads their database-produced state after deletion, and
emits ordinary `updated` events for the cleared pointers. Those events are
written before the `deleted` events. Section 8 undoes in descending event id
order, so this makes the deleted blocker restore first and the pointer restore
second. Reversing the event order attempts to restore a foreign key reference
before its target exists.

Passing snapshots into the domain layer was rejected because it moves
authoritative reads outside the only-writer boundary and admits stale or
partial audit records. Reconstructing the cascade's after row in Python was
rejected because the database is the authority for the foreign-key action.
Adding triggers, columns, or a new event operation was rejected because section
4 closes the schema and section 8 already defines `updated` for this shape.

**Limits.** The cascade query filters by `owner_id`. The schema permits a
foreign actor's task to reference the deleted task, and PostgreSQL will clear
that pointer without an event. Auditing it would introduce a cross-actor domain
write and undo path, while preventing it requires a schema-level ownership
constraint section 4 does not provide. T06 records the limitation rather than
inventing either. `delete_tasks` also locks direct targets and referencing rows
in two statements, so a narrower interleaving between concurrent deletes
remains possible even though bulk target locks now have a canonical order.

**Review provenance.** Section 12 routes T06 as Sol writes and Opus reviews.
Opus verified the required transaction boundary, then the user directed Opus to
apply the three execution-review fixes to the uncommitted worktree before
handing it back. That edit to Sol-owned files is a T06-only routing exception.
Sol read the applied code against this decision and the build contracts,
reproduced the extended PostgreSQL gate, and accepted ownership of the result.
The exception does not change the routing of any later task.
---

## Spec clarifications recorded at T06

D-24 through D-29 are reserved by `docs/LINEAR_INTEGRATION.md` section 3 and are
not yet appended to this file. The next free number after that reserved block is
D-30, and this decision takes it. Do not reuse a reserved number.

### D-30: T22 uses Pydantic AI's own instrumentation, and the working API is version-specific

T22 asks for spans on model and tool operations. Pydantic AI emits them already,
so `telemetry.py` configures a tracer provider and turns instrumentation on. It
does not wrap model calls or tool bodies. Hand instrumentation would duplicate
the framework spans and would put telemetry code inside the five-step tool body
that section 10 specifies, which is the one place in the build where an extra
line is expensive.

Confirmed on 2026-08-13 against the pinned `pydantic-ai` 2.27.0, by running an
agent with an in-memory span exporter and reading the finished spans.

| # | Fact | Confirmed value |
|---|---|---|
| 1 | How instrumentation is enabled | `Agent.instrument_all(InstrumentationSettings(tracer_provider=...))`, imported from `pydantic_ai`. `Agent(instrument=...)` is not a constructor argument in this version and raises `TypeError`. |
| 2 | Spans emitted by one tool-calling turn | `invoke_agent`, one `chat` per model request, and `execute_tool <name>` per tool call, carrying `gen_ai.operation.name` values of `invoke_agent`, `chat`, and `execute_tool`, and `gen_ai.tool.name` on the tool span. |
| 3 | Span counts | A single tool-calling turn emits **two** `chat` spans, one before the tool call and one after. `tests/test_telemetry.py` therefore asserts at least one span of each class and never an exact total. |
| 4 | Packages | `opentelemetry-api` is a hard dependency of `pydantic-ai-slim` at `>=1.28.0`. `opentelemetry-sdk` is not a declared dependency and is present today only transitively, through the `logfire` extra that `pydantic-ai` pulls in. `telemetry.py` imports the SDK directly, so T22 pins `opentelemetry-sdk==1.44.0` in `backend/requirements.txt` per D-14. |

Recorded because the obvious spelling fails. A model writing `telemetry.py` from
memory reaches for the constructor argument first, and against this pin that
raises a `TypeError` which reads like a typo rather than a version difference.
The cost of finding this at T22 is an hour of the wrong kind of debugging; the
cost of reading it here is a minute.

### D-31: new tasks and kernel edits are gated on an explicit re-plan

Rule 0.2 previously banned a list of implementation categories: ORMs, caching,
message queues, background workers, auth, migration frameworks, state management
libraries. The list is still correct and still binding, but it turned out to be
the wrong shape for the question that actually arrives, which is whether some
proposed addition counts as one of the banned categories at all. That question is
arguable, and arguing it is not work.

Rule 0.2 now carries a second gate that does not depend on categories. Anything
that adds a task to section 12, and anything that edits a KERNEL file, requires
an explicit re-plan against `docs/PROJECT_PLAN.md` and a decision here naming
what was cut to pay for it. Everything else that is not already in
`docs/BUILD_SPEC.md` waits until after T15 is green and goes to
`docs/OPEN_QUESTIONS.md`.

The gate is deliberately indifferent to whether an addition is hosted or
self-run, and to whether it is infrastructure or a product integration behind the
tool and policy boundary. Those distinctions do not predict cost. Task count and
kernel reach do.

This is recorded rather than left implicit because the Linear integration in
`docs/LINEAR_INTEGRATION.md` is exactly the shape the gate is meant to catch: it
adds six tasks (T00B, T00L, T26 through T29), two of them OPUS ONLY, it
introduces a background projector worker draining an outbox table, and it edits
KERNEL `undo.py` to add the `EXTERNALLY_MODIFIED` precheck. Two of those, the
worker and the outbox, are named in the first sentence of rule 0.2. The
integration may still be the right call, and this decision does not reverse it.
What it does is require that the schedule cost be stated and paid rather than
absorbed, and the payment is still outstanding.

---

## Spec corrections recorded at T00B

Recorded on 2026-08-13 after T00B stopped at three contradictions between the
specification and the repository. Each was measured against `954bd83` rather
than reasoned about. No decision above is amended. These close Q-05, Q-06, and
Q-07 in `docs/OPEN_QUESTIONS.md`.

### D-32: taxonomy and execution are separate test markers, and only execution gates CI

Three markers, registered in `backend/pyproject.toml`. `eval` and `contract`
describe what a test is. `network` describes what it requires. Only `network`
decides what the default suite collects.

```
tests/test_contract.py    @pytest.mark.contract  @pytest.mark.network
tests/test_evals.py       @pytest.mark.eval      @pytest.mark.network
```

Default CI becomes `pytest -m "not network"`. The on-demand suites are
`pytest -m eval` and `pytest -m contract`, neither carrying an exclusion clause.

The problem this closes is that no pytest configuration existed and no `eval`
marker was registered, while the T00B gate had to prove that
`pytest -m "not eval"` did not collect a network-dependent contract test. Any
marker other than `eval` left it collected; marking it `eval` would have
classified a provider contract test as a behavioral evaluation, and would then
have pulled a network test into the behavioral suite at T24 the first time
anyone ran `pytest -m eval`. Two candidate designs were tried and each produced
a different latent T24 failure, which is why this decision fixes the marker
assignment for every known external suite now rather than leaving it to the task
that writes each one.

`network` is named for a property of the test rather than for today's policy. A
name like `nonci` would encode the current CI arrangement into the test and stop
being true the moment external tests are run deliberately in a protected job.

The correction is not a search and replace of the CI command. Every statement
equating `eval` with CI exclusion had to move, including the `test_evals.py`
heading in BUILD_SPEC section 11, or a later reader would conclude that marking
a test `eval` keeps it out of CI and ship a network test the default gate
collects.

`backend/pyproject.toml` was created rather than a new `pytest.ini`, because
BUILD_SPEC section 3 has specified that file since the tree was written and it
had simply never been created. It carries tool configuration only, with no
`[build-system]` and no `[project]` table, so it does not make `backend` an
installable distribution and does not resolve Q-04.

### D-33: the probe creates one throwaway issue per state-sensitive fact

The rule is a property, not a count:

> Each fact that requires uncontaminated initial state creates its own throwaway
> issue and archives it in a `finally`. Pre-existing workspace issues are never
> modified.

The T00B prompt authorized "a single throwaway issue". Three are required and
sharing one would couple the checks. Fact 3 ends with its issue archived, so
fact 4 would have to unarchive it before running, contaminating the
archived-exclusion check that fact 4 exists to make. Fact 6 must create an issue
under a client-supplied id that has never been used, so it cannot reuse one at
all.

The property is recorded instead of the number because "up to three" would be an
artifact of today's probe and would break the moment a further state-sensitive
fact is added, which is the same defect as the original "a single throwaway
issue".

### D-34: the lint contract is a pinned version, a pinned rule set, and a pinned surface

BUILD_SPEC section 11 has claimed since it was written that CI runs
`ruff check`. Nothing did. Ruff was absent from `backend/requirements.txt`, no
repository-owned ruff configuration existed, and ruff 0.16.3 reported 14
violations under `backend/` on master, every one of them in a file T00B is
forbidden to touch and two of them in KERNEL files. A gate requiring ruff clean
over a surface the task may not repair is not satisfiable at any version pin.

All three halves are required:

```
tool      ruff==0.16.3 in backend/requirements.txt
policy    select = ["E4", "E7", "E9", "F"] in backend/pyproject.toml
surface   cd backend && ruff check .
```

Ruff goes in the single backend pin source because D-14's *mechanism* forbids
inline pin lists for backend jobs. D-14's stated rationale, about the probe
proving API facts for versions the application does not install, does not reach
a linter, and no `requirements-dev.txt` is introduced: this repository
deliberately established one backend pin source and a second one would reopen
D-14 for no benefit here.

The rule set is pinned because a version pin alone does not define a contract.
Ruff resolves configuration by directory hierarchy and can fall back to a
user-level configuration before its own defaults, so an unconfigured repository
may enforce a different policy on every machine. Ruff's defaults also widen
between releases: 0.16.3 enables `I`, `UP`, `BLE`, `FURB`, `RUF`, and `SIM`,
well beyond the `E4, E7, E9, F` it defaulted to for years.

The surface is pinned because ruff's configuration discovery is
directory-relative. A bare `ruff check` from the repository root would lint the
disposable `spike/` tree, which holds 3 of the 22 repository-wide violations and
is deleted before T12A, and would let throwaway code block a gate.

The selected set is the defect tier rather than the style tier, and `master`
passes it at exit 0. That is validation, not the reason for the choice; had the
set needed contorting to fit master, the honest answer would have been to scope
the gate to T00B-owned files instead. Two naive alternatives were measured and
are worse: `select = ["E", "F"]` yields 51 `E501` line-too-long violations,
because ruff's default selects only `E4, E7, E9` and thereby omits `E501`, and
`["E", "F", "I"]` yields 59.

The 14 baseline findings are deferred lint-adoption debt owned by a future task,
not violations of this contract and not something to silence. Findings from
ruff's broader default set are outside the contract and must not become an
unwritten second gate. T00B voluntarily clears the five in `linear_probe.py` as
hygiene, and that choice is not a requirement on anything else.

Cleaning all 14 first was considered and rejected on schedule: it adds a task to
section 12 and edits two KERNEL files for lint, which trips both halves of the
D-31 gate and would require a named cut while the Linear expansion is already
unpaid. Option B touches neither `policy.py` nor `idempotency.py`, so it needs
no re-plan.

### D-35: dedicated review is compressed into checkpoints from T07 forward

The active schedule exception changes review cadence, not implementation
discipline. Tasks retain their order, author routing, file ownership, separate
commits, and task-local verification. Dedicated model review is batched after
T08 for undo and the wire boundary, and after T12B for the reference tool,
transport, trust boundary, and approval path.

Remaining review budget is spent in this order:
`T12B > T08 > T12A > T07 > T10 > everything else`. T21 receives no dedicated
review budget. Once the T15 ugly-demo smoke path is green, finishing, testing,
reproducibility, and rehearsal take priority over deeper review. The complete
task-by-task treatment is authoritative in BUILD_SPEC section 1A.
