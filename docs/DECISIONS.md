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

**Result:** SUPERSEDED BY [D-63](#d-63-nvidia-hosted-glm-52-is-the-sole-runtime-provider)

Ten prompts, both candidates, scored on correct tool behavior, clarification where appropriate, latency, and cost. Table goes in the README when complete. `MODEL_ID` is set from this, not from reputation.

---

## Cuts taken during the build

| Day | Cut | Position in cut order | Reason |
|---|---|---|---|
| 2026-08-13 | Resume affordance and orphan sweep, activity S | 2 | Funds part of the Linear expansion. Credits 0.25d. See D-36. |
| 2026-08-13 | Behavioral evals, fifteen cases to ten, activity X | 4 | Funds part of the Linear expansion. Credits ~0.17d, being a one third reduction of a 0.50d activity. See D-36. |
| 2026-08-13 | Model bakeoff, ten prompts to five, activity AB | 5 | Taken as scope reduction, credited at 0.00d. Halving prompts does not halve setup, scoring, and write-up. See D-36. |
| 2026-08-13 | External OTel trace viewer | 1 | Named as Linear funding by `docs/LINEAR_INTEGRATION.md`, credited at 0.00d. Activity U budgets instrumentation, which the cut keeps. See D-36. |
| 2026-08-13 | STRETCH items | not in the cut order | Named as Linear funding by `docs/LINEAR_INTEGRATION.md`, credited at 0.00d. No STRETCH activity exists in the 11.0d baseline. See D-36. |

---

## Hardening decisions recorded at T00R

Recorded on 2026-08-12 after an audit of the completed T00 and T00A work. Each
one closes a consequence that was discovered earlier but never written down. No
existing decision above is amended.

### D-12: conditional approval raises at tool step 0

**Superseded in part by D-59.** In the ordering sentence below, only the clause
"ahead of `arguments_hash`" is superseded. The clause "ahead of
`idempotency.acquire`", and the `LEASE_IN_FLIGHT` deadlock rationale this entry
gives for it, remain in force and are restated in D-59. No sentence in this
entry has been rewritten.

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
---

## Linear schedule ledger recorded at T06

### D-36: the Linear expansion is priced at 1.50d, of which about 0.4d has quantified funding

D-31 requires that anything adding a task to section 12, or editing a KERNEL
file, carry an explicit re-plan against `docs/PROJECT_PLAN.md` and a decision
naming what was cut to pay for it. The Linear expansion trips both halves. It
adds six tasks (T00B, T00L, T26 through T29) and edits KERNEL `undo.py` for the
`EXTERNALLY_MODIFIED` precheck. This decision closes that requirement.

**The cost is not a new estimate.** `docs/LINEAR_INTEGRATION.md` already records
it: "Estimated cost for T00B, T00L, and T26 through T29 together: about a day
and a half." That aggregate stands. No implementation evidence contradicts it,
so this decision adopts 1.50d rather than substituting a fresh guess.

**The original funding plan named two sources that credit nothing.** The same
passage continues: "Paid for from the STRETCH items first, then cut order item
1, the external OTel viewer, keeping the instrumentation. If more is needed,
evals drop from fifteen cases to ten and the bakeoff from ten prompts to five."
Audited against the step 3 activity table:

| Named source | Credit | Why |
|---|---|---|
| STRETCH items | **0.00d** | STRETCH is a change-control bucket for ideas arriving after Day 2, which stay there per R6. No STRETCH activity appears in the 11.0d baseline. Cancelling work that was never scheduled frees no scheduled effort. |
| Cut order 1, external OTel viewer | **0.00d** | Activity U budgets 0.25d for OTel *instrumentation*, and the cut explicitly keeps the instrumentation and drops only the viewer. No viewer effort is identifiable anywhere in the activity table, so there is nothing to credit. |
| Cut order 2, resume and orphan sweep | **0.25d** | Activity S, removed in full. |
| Evals fifteen cases to ten | **~0.17d** | Activity X is 0.50d for fifteen cases. Ten cases is a one third reduction, not a half. Straight line, 0.33d remains. |
| Bakeoff ten prompts to five | **0.00d** | Recorded as taken, credited at zero. Halving the prompt count does not halve setup, scoring, and write-up, and no defensible decomposition of activity AB's 0.25d exists. |

Quantified payment is therefore about **0.42d**, written as ~0.4d, against a
1.50d expansion. The expansion was approved against a funding line whose first
two tranches were worth nothing, which is the finding this decision exists to
record. Neither tranche was dishonest; neither was priced.

**The ledger, reconciled.**

```
Original PROJECT_PLAN effort                 11.00d
Recorded Linear expansion                    +1.50d
                                             ------
Revised gross plan                           12.50d

T00B already delivered                       -0.25d
                                             ------
Remaining gross Linear work                   1.25d

Quantified schedule cuts                     -0.42d
                                             ------
Remaining unoffset schedule pressure         ~0.83d
```

The full funding gap is ~1.08d (1.50 minus 0.42). Of that, 0.25d has already
been spent on T00B, leaving ~0.83d as future pressure. The residual is accepted
as buffer exposure under R5, estimate overruns, whose stated mitigation is the
pre-agreed cut order applied without renegotiation on the evening a slip
appears. No further cut is manufactured here to make the arithmetic balance.

**D-35 is deliberately not credited.** Review compression saves model review
budget, not implementation effort. No activity in the step 3 table is review,
and PROJECT_PLAN's 11.0d is plausible only on the assumption that review stays
human and outside the estimate. Booking D-35 against this debt would change no
number in the ledger while making it appear balanced.

**No per-task allocation is recorded.** The 1.50d is authoritative only as an
aggregate. Dividing it into six equal quarters would launder a convenient split
into the plan and is not done. T27 and T28 carry the highest overrun risk, T27
for the projector ordering, serialization, retry, and atomic writeback, and T28
for reconciliation and divergence detection.

**T28 is the first Linear-specific contingency, without a manufactured savings
figure.** If schedule pressure requires another Linear-specific reduction after
T15 is green, T28 implementation is cut before anything on the never-cut list,
and the reconciliation and divergence design remains documented in
`docs/LINEAR_INTEGRATION.md` and the README as designed but not built. Cutting
it is defensible because Linear is a projected surface and not the authority, so
the outbound projection path stands on its own. A schedule credit is recorded
only when T28 is actually estimated or cut. This decision does not derive a
per-task fraction from the aggregate in order to claim relief it cannot size.

**The T07 kernel exposure, which is not an effort number.** T00L and T07 add
`EXTERNALLY_MODIFIED` logic to KERNEL `undo.py` at the same time that D-35 has
compressed T07 review and ranked it fourth in the remaining review budget. Those
two decisions were taken independently and compound. The effort ledger above
does not change, because T07 already carries activity O at 0.50d and a few
kernel lines cannot be priced honestly at another quarter day. The mitigation is
a review exception rather than a schedule one: when T07 and T08 are reviewed
together at the post-T08 checkpoint, the Linear-added kernel delta receives
focused review even though the task as a whole does not.

**Limitation: this audit covers Linear only.** T00R is a task that exists, has
merged, and appears nowhere in the step 3 activity table, and the marker and
lint work recorded as D-32 through D-34 was likewise never priced. The 11.0d
baseline is therefore itself understated by an unmeasured amount, and 12.50d
should be read as "11.0d as originally recorded, plus Linear," not as a complete
current estimate. Pricing those is not attempted here. D-31 exists so that the
next such addition is priced when it is proposed rather than reconstructed
afterwards, which is what this decision had to do.

---

## Undo decisions recorded at T07

Recorded on 2026-08-13, before any T07 code was written. D-36 is taken by the
Linear schedule ledger, so this block starts at D-37.

### D-37: BUILD_SPEC section 12 governs sequencing, and LINEAR_INTEGRATION section 8 is a proposal

`CLAUDE.md` names both files as sources of truth and they disagree about what
T07 is. Section 12 lists T07 as `undo.py` "as specified" with no T00L row.
Section 8 of `docs/LINEAR_INTEGRATION.md` sequences T00L before T07 and gives
T07's done-when as "As specified, plus the diverged refusal". See Q-08.

**BUILD_SPEC section 12 governs.** T07 implements section 8 of BUILD_SPEC as
written. It does not implement the `EXTERNALLY_MODIFIED` precheck from
LINEAR_INTEGRATION section 4.4. That clause is a proposed delta to BUILD_SPEC
section 8 rather than part of it, so this is not T07 shipping an incomplete
section 8; it is the Linear document standing ahead of the ratified plan.

The LINEAR_INTEGRATION section 8 task table is **proposed and unratified**
except for the T00B row, which section 12 absorbed when T00B landed. T00L is now
carried in section 12 as well, with its deferral stated in the row, so the two
tables no longer disagree about whether it exists. T00L is not a T07
prerequisite.

**Cost, stated rather than hidden.** If T00L is taken, the divergence precheck
becomes a second edit to a merged KERNEL file, which D-31 prices at an explicit
re-plan and a named cut. That cost was accepted in exchange for keeping T07 on
the critical path to T08 and the ugly demo bar.

**D-36's review commitment is re-aimed, not dropped.** D-36 states that the
Linear-added kernel delta receives focused review when T07 and T08 are reviewed
together at the post-T08 checkpoint. With T07 shipping without the delta, that
clause has nothing to point at. It becomes this instead: at checkpoint 1 the
reviewer examines the shipped `undo.py` for whether the T00L divergence precheck
can be retrofitted into its precheck pass, and at what cost. That is the
question this decision defers rather than answers, so it is the one worth a
reviewer's attention.

### D-38: projected-state undo semantics

Section 8's precheck compares every event against current database state, which
refuses a valid undo for any run that touched one task more than once. See Q-09
for the two reproducible cases.

**Terminal events for each task are validated against current database state;
earlier same-task events are validated against projected historical state
derived from the event snapshots. Compensation maintains a separate
physical-version projection. Every mutating compensation is guarded at its write
boundary: updates by expected version, deletes by expected version, and restores
by primary-key uniqueness. Any failed guard or conflict aborts and rolls back
the entire undo.**

**The two projections are different numbers and are computed differently.** The
precheck tracks the historical version carried in the snapshots. The apply pass
tracks the physical version in the row, which moves forward as each compensation
lands, because history is append-only and a compensation is a new forward
mutation. They diverge as soon as the first compensation lands:

```
run: X at v1 --update--> v2 (event U) --delete--> gone (event D)

precheck, reverse order, historical
  D is terminal for X: the row must be absent            ok
  projected := D.before                                  v2, exists
  U.after["version"] == 2 == projected                   ok

apply, reverse order, physical
  undo D: re-insert from D.before at version 3           before.version + 1
  undo U: guarded update, expected_version = 3           not U.after["version"]
          the row lands at v4
```

Guard on the historical 2 and the update touches zero rows and the undo dies
mid-apply. Compare the precheck against the physical projection and nothing ever
matches.

**The projected precheck is stronger than the literal one, not weaker.**
`undo_run` loads one run's events, so a foreign write can land between two of
that run's own events on the same task. The terminal check still passes, because
the newest event agrees with the database. The projected comparison then finds
that the earlier event's `after["version"]` does not match the state the later
event recorded, and refuses. Undoing that earlier event would have destroyed a
change the run never made.

**A trap worth naming.** The T06 delete cascade writes an `updated` event where
`before["version"] == after["version"]`, because `ON DELETE SET NULL` is a
foreign key action and does not run the guarded update. Code that derives the
projected historical version as `after["version"] - 1` instead of reading
`before["version"]` breaks there and only there.

**Row locking is a strengthening, not the correctness condition.**
`SELECT_TASKS_BY_IDS_FOR_UPDATE` is used in the precheck, in the canonical id
order D-23 established, so the common conflicts surface in the precheck where
they carry a precise reason. It cannot be the contract, because an absent row
cannot be locked and the `deleted` events are precisely the ones whose expected
state is absence. The guards above are what make the apply pass correct, and T07
must be correct without the locks.

**Repeated invocation is not redo.** Compensation events keep the original
`run_id` per section 8 step 4, so a second call would load both the original and
the compensation wave, and undoing that combined history is not a well-defined
inverse of anything. Calling it redo would turn an accidental consequence of
event selection into a contract. T07 defines undo as a single application to an
eligible run. Once compensation events exist for a run, that run is no longer
eligible, and `RunDetail.can_undo` with the T18 exposure is where that
eligibility is enforced. A future redo needs its own design. `undo.py` still
understands a `restored` event, because section 8 requires the precheck to, but
understanding one is not a claim that a second undo is supported.

**A savepoint around the restore insert was considered and rejected.** A unique
violation aborts the transaction, so the translation to `ROW_RECREATED` happens
after the rollback at the boundary rather than in place. A savepoint would let
the rest of the undo proceed past a conflict, and there is no correct undo that
proceeds past a conflict. Its only purpose would be a capability that must never
be exercised, in a KERNEL file whose central promise is the opposite.

### D-39: T07 persistence and read expansion

Section 12 lists T07's files as `undo.py`. Three statements and two domain entry
points are added. See Q-10.

```sql
DELETE_TASK_GUARDED        delete one task only if its version still matches
INSERT_TASK_RESTORED       re-insert under the original id, version, created_at
SELECT_ALL_EVENTS_FOR_RUN  every event for one run, descending, no LIMIT
```

```python
domain.delete_task_guarded(owner_id, task_id, expected_version, *, conn)
domain.restore_task(owner_id, snapshot, *, version, conn)
```

**Why the guarded delete exists.** Of undo's three compensations, the delete was
the only one with no guard available. `UPDATE_TASK_GUARDED` refuses on version
and the restore insert refuses on the primary key, but `DELETE_TASKS_BY_IDS`
carries only `owner_id` and `id`. That is correct on the tool path, where the
policy check and the row lock run immediately before it in one transaction. On
the undo path the precheck is a separate pass, so without a predicate on the
write a concurrent change landing in between is deleted rather than refused,
while undo reports success. Zero rows deleted means the row moved or vanished;
undo reports `VERSION_CONFLICT` for both and the precheck keeps the finer
distinction.

`DeleteTasksArgs` is deliberately not given an `expected_version` field. It is
the `delete_tasks` tool's argument model, and changing a tool contract to serve
undo would put the cost in the wrong place. The domain entry point takes the id
and version directly instead.

**Why the restore insert exists.** `INSERT_TASK` lets the database generate the
id and defaults `version` to 1. Section 8 requires the original id from
`event.before` and a version continuing from the deleted row's plus one.
`created_at` is carried across for the same reason: the row is the same task
returning, and `SELECT_TASKS_FOR_OWNER` orders on it. The caller supplies the
version rather than this statement deriving it, because the plus-one rule is
undo semantics and the domain layer executes writes.

**The undo event read is deliberately unbounded, and this is the rule rather
than a filename.** Section 5 requires a `LIMIT` on every list query, and that
rule governs paginated reads for display. Undo is all-or-nothing, so a truncated
read does not return a shorter answer, it returns a wrong one: the events past
the bound are silently not compensated and the result still reports success. No
fixed bound is provably safe either, because `bulk_update_tasks` places no cap
on `task_ids`. `SELECT_EVENTS_FOR_RUN` keeps its `LIMIT` and remains the
statement for display reads. Do not add a `LIMIT` to `SELECT_ALL_EVENTS_FOR_RUN`.

### D-40: T07-only test-authorship exception

Section 11 routes `test_invariants.py` to Sol. T07's done-when is
`test_stale_undo_refused` passing, and Sol was not reachable. D-16 was T04-only
and D-20 was T05-only, both explicitly, so T07 needs its own. Granted by the
user on 2026-08-13, scoped to T07 alone, and not precedent for T08.

Compensating measures, matching T05: the red run is preserved before any
implementation exists, single-line mutations are applied one at a time and each
required to behave as predicted, both production files are restored and verified
by digest, and the blind review at checkpoint 1 receives the task specification,
the diff, and the verification evidence only.

**The exception includes multi-scenario expansion of the existing named
invariant, and does not increase the section 11 collected-test count.** Section
11 lists thirteen test names verbatim, and this file's collected count is meant
to equal the count of proven invariants. The semantics D-38 settles are
constructible as tests and belong in tests rather than hidden in a CI gate, but
adding six names would widen an enumerated list. `test_stale_undo_refused`
therefore carries seven scenarios under one name, following the shape T04
established for `test_forged_approval_rejected`, which covers three forgery
shapes plus a positive control. Three of the seven are positive controls, and
they are not padding: an `undo_run` that refused unconditionally would pass every
refusal scenario. Each of the four refusal scenarios asserts zero writes on both
surfaces, task rows including `version` and `updated_at` and the run's event
count, rather than leaving that to one shared check that a single clean path
could satisfy.

### D-41: undo orchestration and cascade-event boundary

`undo.py` may construct and relabel compensation `PendingTaskEvent` payloads,
but all task and event persistence executes through the domain layer, which
remains the only writer of `tasks` and `task_events`.

The domain functions report the physical operation they performed: `update_task`
emits `updated`, `restore_task` emits `created`, `delete_task_guarded` emits
`deleted`. Undo relabels the direct compensation to `restored`, which section 8
names, so the operation each layer reports always describes what that layer did.

**Undoing a `created` event through the domain layer retains all D-23 cascade
audit events, so one inverse operation may legitimately emit one direct
compensation event plus N cascade events.** Section 8's singular event wording
describes the direct compensation and does not suppress domain-required audit
records. Suppressing them would reopen the hole D-23 closed, where an
`ON DELETE SET NULL` write reaches the database without passing through any
function that could audit it.

**The cascade events are not relabelled.** The direct inverse of the run's event
is the compensation and is named `restored`. The rows a compensating delete
produces through `ON DELETE SET NULL` are fresh side effects rather than
inverses of anything, and keep the `updated` semantics D-23 gave them.
Relabelling them would make the event log claim a pointer was restored when it
was cleared.

---

## Runs and wire contract decisions recorded at T08

Recorded on 2026-08-14, before any T08 code was written, in the same order the
T07 block established. D-42 through D-45.

### D-42: T08 scope, the approval facade, and the unbounded invocation read

**The test-authorship exception is granted afresh for T08.** Section 11 routes
`test_invariants.py` to Sol. T08's done-when is 12 of 13 invariants passing,
`test_extra_body_keys_rejected` and `test_unsafe_prompt_mode_requires_demo_env`
do not exist, and D-40 states in terms that its T07 exception is not precedent
for T08. Without a fresh ruling the task is mechanically impossible. Granted by
the user on 2026-08-14, scoped to T08 alone, and not precedent for T09.

Compensating measures match T05 and T07: the red run is preserved before any
implementation exists, single-line mutations are applied one at a time and each
required to behave as predicted, every touched file is restored and verified by
digest, and the blind review at checkpoint 1 receives the task specification,
the diff, and the verification evidence only.

**T08's file list is `runs.py`, `main.py`, `sql.py`, and
`tests/test_invariants.py`,** plus the `.github/workflows/ci.yml` and
`IMPLEMENTATION_NOTES.md` companions CLAUDE.md requires of every task, and the
`docs/BUILD_SPEC.md` and `docs/DECISIONS.md` edits this decision block records.

**Approval reads live in `runs.py`, and no `approvals.py` is created.** Section
10's five-step tool body names `approvals.load(run_id, tool_call_id)`, section 3
has no such file in its "Create exactly this" tree, no task in section 12
creates one, and `INSERT_APPROVAL`, `SELECT_APPROVAL`, and `DECIDE_APPROVAL`
have sat in `sql.py` without a caller since T02. `policy.check` takes the
approval row as a parameter and never writes one, so the reader has to exist
somewhere by T10, which is two tasks before the approval bridge.

Approval rows are run-scoped control state, so `runs.load_approval` is the
smallest coherent home and section 10 is amended to name it. A new module would
widen the file tree without buying separation on this schedule. T10 can
therefore read an approval without owning approval persistence, and T12B gains
`runs.py` and `sql.py` for creation, decision, and the `pending_approval` read.

**`SELECT_INVOCATIONS_FOR_RUN` is deliberately unbounded,** on the reasoning
D-39 recorded for `SELECT_ALL_EVENTS_FOR_RUN`. Section 5 requires a `LIMIT` on
every list query, and that rule is about paginated reads for display.
`RunDetail.steps` is not one: the section 9 wire shape carries no cursor and no
truncation flag, so a fixed bound would not return a shorter answer, it would
return a false one, claiming a complete run while silently omitting steps. No
fixed bound is provably safe either, because `bulk_update_tasks` places no cap
on `task_ids`. `SELECT_LEASE` keeps its key and remains the single-row read.
Ordering is `created_at ASC, tool_call_id ASC`; the second key is a presentation
tie-breaker for rows sharing a timestamp, not a claim that lexical id order
reconstructs causal order.

### D-43: RunStep semantics, and what the persistence model cannot prove

**`RunStep.status` is the persisted invocation status, and `deduplicated` is
structurally unreachable at T08.** `tool_invocations.status` is
`pending | completed | failed`. Section 9's wire enum adds a fourth value that
is computed when a lease returns `REPLAY`, and `idempotency.acquire` returns
`REPLAY` straight off a read of a completed row and writes nothing. The row is
not rewritten, no second attempt is recorded, and a later
`GET /api/runs/{id}` reading Postgres has nothing to render.

The narrow statement is the correct one. **Attempt number is durable for every
path that rewrites the lease row.** `REACQUIRE_FAILED_LEASE` and
`STEAL_EXPIRED_LEASE` both increment `attempt`. Successful replay is the
exceptional path, the one that reads without writing, so replay occurrence and
attribution are what is missing rather than attempt history generally. T08
therefore populates `tool_call_id`, `tool_name`, `attempt`, `duration_ms`,
`error`, and the three reachable statuses truthfully, and never synthesizes a
`deduplicated` step.

`agent_runs.tool_calls` exceeding `count(tool_invocations)` can establish that
additional attempts occurred, and in the single-tool demo beat it supports an
aggregate replay inference. It cannot attribute a replay to a particular tool
call once several exist, so it is not a general solution and T08 does not use
it.

**This is a T20 prerequisite, not a note.** ARCHITECTURE's 8:00 demo beat reads
`Attempt 1 COMMITTED / Attempt 2 DEDUPLICATED / Mutations 1`. Before T20 claims
that surface, the project must decide whether to persist attempt history or to
use the narrow single-tool reconstruction for that one display. As specified
today, T20 is not executable.

**`duration_ms` is the elapsed time of the most recent persisted attempt,
anchored at `lease_expires_at - LEASE_TTL_SECONDS`.** `created_at` is the wrong
anchor: neither recovery statement rewrites it, so a stolen lease measured from
it is charged for the dead holder's entire expiry window and a three second tool
reports as roughly two minutes. Every path that grants execution sets
`lease_expires_at = now() + ttl`, and `COMPLETE_LEASE` and `FAIL_LEASE` set
`completed_at` without touching it, so subtracting the TTL recovers the moment
this attempt was granted. A terminal row measures to `completed_at`, a pending
row to a database-side `now()` returned by the same statement, so an idle step's
duration cannot drift with the reader's clock skew.

**The branch is on `status`, not on `completed_at IS NULL`, and that is load
bearing.** `REACQUIRE_FAILED_LEASE` returns a row to `pending` and bumps
`attempt` without clearing `completed_at`, so a reacquired row is pending while
still carrying the previous attempt's completion stamp, which is earlier than
the current attempt's start. Branching on the timestamp takes the terminal path
and subtracts a later anchor from an earlier stamp. Both readings are covered by
mutation in the T08 gate.

**Limitation.** The reconstruction assumes `LEASE_TTL_SECONDS` has not changed
since an attempt acquired its lease. Changing it while historical rows remain
makes the derived start inaccurate for those rows, which is why the result is
clamped at zero rather than allowed to go negative.

### D-44: endpoint ownership, `can_undo`, and actor-scoped `OUT_OF_SCOPE`

**Each section 9 endpoint belongs to the task that owns its behavior.**

```
T08   GET  /api/tasks
      POST /api/runs
      GET  /api/runs/{id}
T09   POST /api/demo/reset              T09 gains main.py
T12A  POST /api/agui
T12B  POST /api/runs/{id}/approvals/{tool_call_id}
T18   POST /api/runs/{id}/undo
CUT   POST /api/runs/{id}/resume
```

T12A, T12B, and T18 already list `main.py`. T09's file list is corrected to
include it, because its done-when is an HTTP response and `seed.py` alone cannot
produce one.

**`/api/runs/{id}/resume` is removed from section 9's table.** D-36 credited
"Cut order 2, resume and orphan sweep, 0.25d, Activity S, removed in full" and
spent that credit inside the 0.42d quantified payment against the Linear
expansion. Building the endpoint at T08 would make the only fully credited cut
in that ledger fiction. Section 9 said "Exactly these endpoints. No others."
while listing one that a later decision had already cut, and this resolves the
contradiction in favour of D-36 as the most recent and the only one that moved a
number. No not-implemented placeholder is created, because a placeholder is
still an endpoint. If resume is ever reinstated it is `interrupted` only;
approval continuation has its own bridge and must not gain a second path around
it.

**`can_undo` is eligibility to attempt compensation, not a promise of success.**

```
can_undo =
    actor owns the run
    AND status in {completed, failed, interrupted}
    AND the run has at least one task_event
    AND the run has no restored compensation event
```

The status clause is a safety condition: a `running` or `awaiting_approval` run
can still commit further effects, and compensating a live run races its own
continuation. `failed` and `interrupted` stay eligible because either can have
committed tools before the run stopped.

The fourth clause is what D-38 requires, and it matters more than it looks.
Compensation events carry the original `run_id`, so a second undo would load the
original wave together with its own compensations and invert nothing well
defined. `undo.py` processes a `restored` event rather than refusing it, by
design, so **this predicate is the only thing preventing a second undo**, and
D-41 guarantees the detector is sound by relabelling every direct compensation
to `restored`. T18 must enforce the same predicate server-side before calling
the kernel rather than only hiding the button.

Two limitations are recorded rather than solved. The undo precheck can still
refuse an eligible run with `ROW_DISAPPEARED`, `VERSION_CONFLICT`, or
`ROW_RECREATED` when current state has moved, so `can_undo` true is not a
guarantee. And a route-level read followed by `undo_run` refuses repeated
sequential calls but does not by itself prove that two simultaneous undo
requests cannot both pass eligibility before either writes compensation. That
concurrency question belongs to T18 and is not solved by expanding the kernel
today.

**`OUT_OF_SCOPE` is widened from tasks to actor-scoped resource resolution.**
Section 9 requires the resolver to reject a run that does not exist or belongs
to another actor and never names the code. `OutOfScopeError`'s docstring scopes
it to tasks. At API resource resolution it means: the requested actor-scoped
resource is unavailable to this actor, without distinguishing absence from
ownership failure. That preserves the non-enumeration property the architectural
invariant already states for tasks. `errors.py` is not edited to reword the
docstring, because it is KERNEL and a cosmetic edit would trip D-31 for nothing.

### D-45: contracts deferred out of T08, with their deadlines

Four things are unresolved. None blocks T08, and each has a task before which it
must be settled.

**No legal error code exists for a valid, actor-owned run whose current status
forbids the requested action.** Section 6 closes the vocabulary at twelve codes
and says every rejection uses one of them. None of them means this.
`VALIDATION_ERROR`, `OUT_OF_SCOPE`, and the approval-specific codes must not be
bent to fit. The endpoint ownership in D-44 means no T08 route needs this
rejection, so it is recorded rather than solved. **Resolve before T12B and
T18**, both of which reject on run state. Adding a thirteenth code edits KERNEL
`errors.py` and therefore trips D-31, which is why it is not done casually here.

**`RunDetail.pending_approval` is null at T08 and T12B owns it.** Nothing writes
an approval row until the bridge exists, so null is observationally correct
today. The genuinely unspecified question is what the field means when a model
produces more than one approval-required call in a turn: the wire shape carries
exactly one pending approval and Pydantic AI's deferred result can carry a list.
T08 does not invent a first-row-wins rule. **Resolve at T12B**, which gains
`runs.py` and `sql.py` for it.

**No durable representation exists for a successful replay.** See D-43.
**Resolve before T20.**

**Concurrent undo eligibility.** See D-44. **Resolve at T18.**

---

## Schedule and review decisions recorded on 2026-08-14

### D-46: T00B stays after T06 and the remaining Linear expansion moves after T25

T00B is complete and remains in its executed position after T06 and before T07.
It is not repeated. Its GATE B PASS remains the prerequisite for every remaining
Linear task.

The optional Linear expansion now runs only after the core sequence reaches T25.
Its exact order is `T25 -> T00L -> T26 -> T27 -> T28 -> T29`. If the expansion
is cut, none of T00L or T26 through T29 runs.

This decision supersedes the earlier sequencing portions of D-36 and D-37 and
the pre-T07 proposal that formerly appeared in `docs/LINEAR_INTEGRATION.md`
section 8. It does not change D-36's 1.50d aggregate estimate, recorded funding,
or contingency order. Because the remaining expansion starts after T25, the
T28 contingency is evaluated after T25 rather than after T15. T00L now owns the
`EXTERNALLY_MODIFIED` retrofit to the already merged KERNEL `undo.py`, so
`undo.py` is in T00L's authorized file list.

### D-47: R2 is a same-SHA review and execution gate before T13

After T12B, the R2 blind review is pinned to an immutable commit SHA and covers
the T10 reference tool, T11 prompts, and the T12A/T12B transport, trust boundary,
and approval path. That exact SHA is then executed through the T12A/T12B
verification path in a fresh Vercel Sandbox with dependencies installed from the
repository's declared dependency and lock files and secrets provided through the
sandbox environment only.

R2 passes only when the blind review has no unresolved BLOCK findings, the fresh
sandbox boots and completes the T12A/T12B path, `cd backend && ruff check .`
passes, `cd backend && pytest -m "not network"` passes, and `npm run build`
passes. A sandbox provisioning or execution failure is an R2 BLOCK. The general
pinned-clone fallback for a review whose sandbox cannot be provisioned does not
apply to R2.

Any fix that changes the reviewed SHA invalidates the entire R2 result. The blind
review, fresh-sandbox execution, and all three deterministic gates rerun against
the new SHA before T13 starts.

## Seed and reset decisions recorded at T09

Recorded on 2026-08-14 after the missing route produced the expected 404 red
probe and before any T09 production code was written.

### D-48: reset is a narrow administrative writer with fixed data and a reviewed routing exception

**Sol may author all of T09, including the bodyless-request guard.** The user
granted a T09-only routing exception on 2026-08-14. It covers exactly one wire
rule: zero request-body bytes continue, and any request-body bytes raise
`VALIDATION_ERROR` with HTTP 422 before handler mutation. It authorizes no other
wire-contract change. D-49 later reassigns T11 to Sol; T12A and T12B keep their
OPUS ONLY routing. The routing exception does not create a standalone T09
review. Per the user's 2026-08-14 clarification, review remains batched at
checkpoint 2 after T12B. That checkpoint receives the final T09 SHA; any later
T09 fix changes the SHA reviewed there.

**T09's implementation file list expands to `seed.py`, `main.py`, and `sql.py`.**
D-44 already put `main.py` in T09. This decision adds `sql.py` for one narrow
administrative insert and adds `docs/ARCHITECTURE.md`, `docs/BUILD_SPEC.md`,
`docs/DECISIONS.md`, and `docs/OPEN_QUESTIONS.md` so the exception and the
closed fixture choices travel with the code. CLAUDE.md's CI and implementation
note companions remain implicit task files.

**`seed.py` is the sole administrative exception to normal writer ownership.**
`domain.py` remains the only writer of task business state during normal
application operation. Reset is not an agent or domain mutation: it destroys
all demo state and reconstructs a baseline in one transaction. `seed.py` may
execute only `TRUNCATE_ALL_STATE` and `INSERT_SEED_TASK` as that reset
orchestration. It writes no `task_events`, run, invocation, or approval rows and
does not commit. The route owns the connection and commits only after all eleven
inserts return. Any failure rolls the whole operation back.

**`INSERT_SEED_TASK` has minimum authority.** The caller supplies `id`,
`owner_id`, `title`, `notes`, `due_date`, `priority`, and `blocked_by`. SQL does
not accept caller-controlled status, version, or timestamps. The schema fixes
status to `open`, version to 1, and both timestamps to database values. The
statement stays separate from `INSERT_TASK_RESTORED`, whose compensation path
legitimately needs caller-controlled historical state.

**The fixture identity and calendar are frozen.** The namespace literal is
`8367986a-6f6a-5895-a6ac-41a894ffdb5c`. It was derived once as UUID5 of URL
namespace name `https://trellis.local/demo-fixture/v1`; the literal is the
runtime contract. Task ids are UUID5 of that namespace with names `task:A`
through `task:K`. Dates are literal data, not calculated from the process clock:

```text
today       2026-08-17
Friday      2026-08-21
overdue 2   2026-08-15
overdue 5   2026-08-12
next week   2026-08-24
```

The BUILD_SPEC section 13 Note column is literal task data. Task B's note is
`interview` and its separate `blocked_by` value is Task A's deterministic id.
Task timestamps and response ordering are not deterministic fixture fields.

**Reset produces baseline state, not historical activity.** After a successful
reset, `tasks` contains exactly the eleven fixture rows and `task_events`,
`agent_runs`, `tool_invocations`, and `approvals` contain no rows. Two resets
produce the same task ids and semantic fixture fields. Atomicity is proved
through the production route and writer: a test-only copy of the fixture gives
a late row an already-used deterministic id, the real primary-key constraint
aborts the reset, and every row in the pre-reset closed baseline plus the
complete owner task list remains identical. No production fault switch or
fixture-injection API is added.

## Model allocation decision recorded on 2026-08-14

### D-49: T09 through T12B use the original mixed T10 split, with boundary-first Opus triage

The active authoring allocation is:

```text
T09   Sol
T10   Opus: create_task only
      Sol: remaining five tools
T11   Sol
T12A  Opus
T12B  Opus
```

For T10, Opus authors only the complete `create_task` reference implementation,
including policy, idempotency lease, domain mutation and event, and invocation
completion in one transaction. Sol transcribes the other five tools against
that exact structure without redesigning the reference.

This preferred split consumes Opus capacity beyond the two substantial T12A and
T12B passes. If only two substantial Opus authoring passes actually remain,
both go to T12A and T12B and Sol writes all six T10 tools from BUILD_SPEC section
10. That fallback is authorized for T10 only. T11 is Sol work in either case.

The final T10 result receives non-authoring, read-only review only after all six
tools exist and T10 verification passes. D-35's compressed cadence remains in
force: this review occurs inside R2, against R2's immutable same SHA, and focuses
on `create_task` as the reference plus the shared transaction shape. This
decision adds no intermediate review checkpoint. A T10 change after review
invalidates the R2 result under D-47.

## Ordering correction recorded on 2026-08-15

### D-50: scope resolves before the conditional approval raise, and D-12 gains the mechanism it never had

Recorded after a blind review of merged T10 found that `bulk_update_tasks` and
`delete_tasks` classify and raise `ApprovalRequired` before any actor-scope load.

**The defect, reproduced.** Against `cc1970f`, a direct call carrying four
foreign, four nonexistent, or three owned plus one foreign task ids raised
`ApprovalRequired` where the contract requires `OUT_OF_SCOPE`. The same held for
an unapproved single-target `delete_tasks` naming another actor's row, because
that tool classifies destructive at any count. Seven cases, all failing with the
identical symptom. The evidence table is in `IMPLEMENTATION_NOTES.md`.

**Why no gate saw it.** `verify_owned_scope_refusal` exercised one foreign target
below the threshold, which cannot reach the raise for `bulk_update_tasks`, and it
passed `approved=tool_name == "delete_tasks"`, which skips step 0 entirely, so
the `OutOfScopeError` it observed came from `policy.check` and proved nothing
about the unapproved direct call BUILD_SPEC section 12 requires to work.

**D-12 is the root cause, not T10 alone.** D-12 requires three things that cannot
all hold: an immediate classify-and-raise as step 0, that `policy.py` needs no
change, and that "actor scope is resolved before the raise, never after." The
owner load is private and `policy.check` cannot serve as the pre-raise call,
because on a conditional call with no approval row it raises the
`APPROVAL_REQUIRED` `PolicyError` rather than the framework's `ApprovalRequired`.
D-12 asserted a property and supplied no mechanism. Under rule 0.1 that
contradiction should have been written to `docs/OPEN_QUESTIONS.md` and stopped
on. It was not, which is the process finding; see Q-17.

**The resolution.** `policy.resolve_scope(actor_id, target_task_ids)` becomes
public and is step 1 of `check`, so there is one definition of the scope rule
rather than two spellings that can drift. The two tools whose classification can
require approval call it between the replay preflight and step 0.

The step sits after the replay preflight, not before it. Q-12 exists because a
committed delete removes its own targets, so a scope load ahead of replay would
raise `OUT_OF_SCOPE` on a byte-identical repeat and make the stored result
unreachable. The refusal path carries the same race recheck step 2 already uses.

**Uniformity is now a named exception.** The other four tools are unchanged.
Their classification is inert at a count of zero or one, so they cannot reach the
raise with scope unknown, and adding the step would buy a second database read
and no property. BUILD_SPEC section 10's identical-body rule therefore has a
recorded two-tool exception rather than a silent one.

**What this does not claim.** No disclosure occurred and none was prevented. The
AG-UI interrupt message is built from the model's proposed arguments, not from a
database lookup, so owned, foreign, and nonexistent ids produce byte-identical
output and it conveys no ownership or existence fact. Nothing in the application
writes an approval row or moves a run to `awaiting_approval` today:
`runs.set_status` has no callers and T12B owns both. The confirmed consequence is
a wrong control outcome on the direct-call surface, changing no state and taking
no lease. T12B's preview guard remains indispensable and is not weakened by this
change, because declaratively gated `delete_tasks` never enters its body before
the framework defers.

**`check` still runs its own scope step on every path.** D-06 is unaffected. The
pre-raise call cannot substitute, because ownership can move between the
deferral and the approved continuation. The gate proves that adversarially by
transferring a target's owner after approval and asserting the continuation
refuses and deletes nothing.

**D-31 payment is not settled here.** This decision records the ordering fix and
its evidence. It does not name a schedule cut, and the merged T10 kernel
expansion under Q-12 remains unratified. Both are the user's to settle before
T12B and R2.

---

## AG-UI transport decisions recorded at T12A

Recorded on 2026-08-15, before any T12A code was written, in the order the T04,
T05, T07, and T08 blocks established. D-51 through D-53.

### D-51: application run identity is server-owned, and section 9's universal resolver rule is narrowed

**One `agent_runs` record is one user turn, one unit of application work.** Not a
conversation. The lifecycle says so from four directions: `agent_runs.prompt` is
singular and `NOT NULL`, a run reaches terminal states such as `completed` and
stamps `ended_at`, undo is scoped to one run under D-10, and D-44's `can_undo`
predicate is about that run's own mutations. A run spanning a whole conversation
would hold turn one's text in `prompt` forever, make `RunDetail.prompt`
misdescribe every later turn, and turn undo into "revert everything this
conversation ever did".

**The initial AG-UI turn creates its own application run.**

```text
POST /api/agui
  |
  +-- newest user message ---> accepted, the only value taken from the payload
  +-- threadId --------------> read for nothing
  +-- runId -----------------> read for nothing
  +-- messages, state, tools, context, forwardedProps, resume --> read for nothing
  |
  v
runs.create(actor_id, accepted_message, model_id)
  |
  v
server-generated agent_runs.id, the application run_id carried into tool context
```

`agent_runs.prompt` and the message handed to the model therefore originate from
the same accepted value by construction, so the two cannot diverge.

**A pre-flight `POST /api/runs` handshake was considered and rejected.** Under it
the client submits the message twice, once to `/api/runs`, which persists it as
`agent_runs.prompt`, and again inside `RunAgentInput.messages`, which is what
T12A proof 2 requires the transport to act on. Nothing forces the two strings to
match, so a client could make `RunDetail.prompt` and the Run Inspector display a
benign prompt beside a hostile executed one. Closing that would mean either
ignoring the AG-UI message, contradicting proof 2, or adding a comparison to the
kernel wire contract. Minting the run from the accepted message removes the
second copy instead of reconciling it.

`POST /api/runs` is unchanged and keeps its contract. It is simply not a
prerequisite of the AG-UI path. Both surfaces call the same `runs.create`
primitive, so one statement issues every application run id.

**Section 9's wire-contract bullet is narrowed, and this is the rule-0.1 item.**
The bullet said, universally, that a browser thread or run identifier arriving in
a request is resolved to an `agent_runs` row and rejected unless that row exists,
belongs to `actor_id`, and is in a status that permits the action. It cannot
describe an initial AG-UI turn, because no application run exists yet, and its
status clause requires a rejection none of section 6's twelve codes expresses,
which is exactly the gap D-45 deferred to T12B and T18. Written before the error
vocabulary closed and before the AG-UI entry surface existed, it is a rule with
no branch for creation. Section 9 now separates creating, operating on, and
continuing a run.

T12A proof 6 is **not** the contradiction. "Never trusted as given" is satisfied
by an identifier that is never read, and the same paragraph delegates the mapping
to T12A in terms. The overstatement is one level up.

**The continuation mapping, specified here and implemented by T12B.** A
continuation does not create authority, it recovers it:

```text
resume[].interruptId -> tool_call_id -> pending approvals row -> agent_runs.id
                                                                     |
                                                      new framework invocation,
                                                      same application run
```

The `approvals` row is the authority under D-06, so recovering the run through it
is stronger than resolving a client-chosen thread string, which would only sit
alongside the authorization record. T12B must not assume a provider-generated
`tool_call_id` is globally unique: the lookup distinguishes zero eligible rows,
which refuses, exactly one, which resolves, and more than one, which refuses as
ambiguous. Probability is not an invariant. That lookup needs a new statement in
`sql.py`, which is already in T12B's file list.

**The server-issued run id travels outward on `thread_id`.** The rebuilt run
input carries `agent_runs.id` as `threadId`, so the adapter echoes it on
`RUN_STARTED` and `RUN_FINISHED` and the browser learns the application run id
over the protocol it already speaks. T12B's approvals endpoint and T20's Run
Inspector both need it. It travels outward only; a value arriving in that field
on a later request is still read for nothing.

**What this does not solve.** History does not carry across turns in a
conversation, because history is per run and the schema has no thread column.
The clarifying-question beat at T17 needs it. That is a consequence of the frozen
data model rather than of this decision, and it is recorded as Q-18 to resolve
before T17. T12A records only the mapping it proves.

### D-52: T12A's file list, and how its gate proves a client that does not exist

**The file list is `agent.py` and `main.py`,** plus the
`.github/workflows/ci.yml` and `IMPLEMENTATION_NOTES.md` companions CLAUDE.md
requires of every task, the `docs/BUILD_SPEC.md` and `docs/DECISIONS.md` edits
this block records, and `tests/test_invariants.py`.

The test file is not an expansion of convenience. Section 11 names thirteen
invariants and the T08 row states that the thirteenth,
`test_agui_forged_history_ignored`, unblocks at T12A. It was deliberately absent
rather than skipped so the collected count always matched the proven count, and
leaving it absent now would mean shipping the transport it guards while the
standing regression test for it stays unwritten. Section 11 routes the file to
Sol; the same test-authorship exception D-42 granted for T08 applies, with the
same compensating measure, which is that R2 receives the specification, the diff,
and the verification evidence only.

**Q-14 resolves as option A: T12A proves the server side of proofs 1, 3, 4, and
5 and writes no frontend code.** No `frontend/` path is tracked, `Board.tsx` and
`useBoard.ts` belong to T13 and `Chat.tsx` to T14, and satisfying those four
sentences literally would mean writing files two later tasks own, which rule 0.4
forbids. Gate A already proved the browser half end to end in a real browser,
which is what the disposable spike existed for. The gate therefore posts a real
`RunAgentInput`, asserts the SSE sequence a client renders, including
`TOOL_CALL_RESULT`, and asserts that `GET /api/tasks` returns committed state for
the refetch. T13, T14, and T15 prove the rendered half against real components.

**Q-16 resolves as option C, with the live half explicitly outstanding.** The
T12A CI job must be deterministic: CI holds no provider secret, and BUILD_SPEC
excludes network tests from the default gate on purpose. The job drives the
identical agent, prompt, six tools, and transport against a `FunctionModel`, so
what it proves is the wiring and the trust boundary rather than the model's
behaviour, which is what the eval suite owns. That is not a redefinition of
T12A's live verification. The BUILD_SPEC prompt `Create a task called Test AG-UI`
against the configured runtime model remains required as separate evidence, and
`MODEL_ID` and a provider credential were unavailable when T12A's implementation
and deterministic verification completed. **Live T12A verification is pending**
and T12A is not marked completely verified until it has run.

The `model` parameter on `build_agent` is the injection point that makes the
deterministic gate possible. It is not a second selector and not a provider
router: `MODEL_ID` remains the only configured runtime model and the default path
is exactly the `Agent(settings.model_id)` line section 1 prints. The agent is
built lazily behind `get_agent`, because constructing it at import would make
importing `app.main` require a configured model, and every deterministic test
imports `app.main`.

### D-53: what T12A deliberately does not do

**No approval bridge.** `delete_tasks` registers with `requires_approval=True`
and the agent's output type includes `DeferredToolRequests`, because both are
transport shape proven at Gate A. Nothing else about approvals exists here. T12A
writes no `approvals` row, moves no run to `awaiting_approval`, honours no client
decision, and builds no preview. D-50 already recorded that nothing in the
application does any of it today.

A consequence follows and is stated rather than hidden: a run whose output is
`DeferredToolRequests` is not finished, so T12A does not mark it `completed`, and
it stays `running` until T12B owns the transition. The orphan sweep that would
otherwise reap it was cut at D-36. The exposure is one task wide, T12B is next,
and inventing a status transition that T12B must then redo would be worse.

**A client-supplied `resume[]` is discarded, not honoured.** The rebuilt run
input carries no `resume`, so `AGUIAdapter.deferred_tool_results` is None and a
client-asserted approval continues nothing. Section 10's bridge requires the
server to construct the resume result from its own stored decision, and a
transport that honoured the client's claim would be asking the browser whether
the browser approved. The gate proves this directly: a payload carrying a forged
`interruptId` for a call with no stored row creates no approval row and commits
no deletion.

**`render_task_block` still has no caller.** T11 shipped the renderer and T12A
wires `SYSTEM_PROMPT` as the agent's instructions, which is the prompt boundary
this task needs. It does not call the renderer, because section 10's provenance
rule places task content in the data position and never in the instruction
position, so where its output belongs is a question about the shape of the user
turn rather than about transport. T23's file list is `prompts.py` and `seed.py`,
which cannot add a caller in `agent.py`. Recorded as Q-19, to resolve before T23.

**A policy refusal inside a tool body fails the run.** `PolicyError` propagates
through the agent rather than being returned to the model as a retryable result,
so a refused mutation ends the turn with `RUN_ERROR` and a `failed` run. That is
truthful and commits nothing, and shaping refusals into model-visible retries is
T19's degraded-state work, not transport.

**`cost_cents` stays at zero.** Pydantic AI reports `RunUsage.cost` as None
without a pricing source. `model_calls`, `tool_calls`, `input_tokens`, and
`output_tokens` are recorded truthfully from `RunUsage`; an invented cost in an
audit row would be worse than a zero.

## Frontend origin decision recorded at T13

### D-61: Next.js is the browser's same-origin facade for FastAPI

**Q-20 resolves as option A.** Browser code calls the authoritative API through
relative `/api/*` paths. Next.js rewrites those requests to the FastAPI origin
configured by the server-only `TRELLIS_API_ORIGIN` environment variable. Its
local default is `http://127.0.0.1:8000`.

The browser therefore never receives or chooses the backend origin. T13 adds no
FastAPI CORS policy and no Next.js route handler, and it does not duplicate any
backend endpoint. PostgreSQL remains authoritative behind FastAPI; Next.js only
forwards the HTTP request and owns no task state.

The T13 file list is expanded by the user's 2026-08-15 approval to include
`frontend/next.config.ts` and `.env.example`. `frontend/next.config.ts` owns the
rewrite and `.env.example` declares `TRELLIS_API_ORIGIN`. This expansion is in
addition to the minimum production frontend scaffold and companion files
authorized in the T13 handoff.

### D-62: T14 aligns the direct AG-UI client to the adapter's exact class version

T14 pins the direct `@ag-ui/client` dependency to 0.0.57. That is the exact
version consumed by `@assistant-ui/react-ag-ui@0.0.54`, so npm installs one
deduplicated `HttpAgent` class for both the application and adapter.

The T13 graph pinned the direct client to 0.0.58 while the adapter declared
`^0.0.57`. Under semver rules for a `0.0.x` package, that range excludes
0.0.58, so a clean install produced two clients. The official integration
shape then failed TypeScript checking because each `HttpAgent` declaration owns
its own private `_debug` member. Matching public methods do not make classes
with different private-member origins assignable.

The resolution changes only the direct client pin and regenerated lock graph.
It does not cast across the mismatch, override the adapter's declared range, or
upgrade the adapter. A clean `npm ci` now deduplicates both consumers to 0.0.57,
and the production build accepts the documented `HttpAgent` to
`useAgUiRuntime` boundary. This resolves Q-21 as option A.

---

## Approval bridge decisions recorded at T12B

Recorded on 2026-08-15, before any T12B code was written, in the order the T04,
T05, T07, T08, and T12A blocks established. D-54 through D-58. The three
preconditions D-45 and D-50 left for the user were settled first, in session,
and D-54 through D-56 record those rulings.

### D-54: the merged T10 kernel expansion is ratified retrospectively, unpriced

D-50 closed by stating that "the merged T10 kernel expansion under Q-12 remains
unratified" and that the D-31 payment was the user's to settle before T12B and
R2. This decision settles it. Authorized by the user on 2026-08-15.

**What is ratified.** Two KERNEL edits that merged with T10 and were never
priced under D-31:

```text
idempotency.replay_completed()   backend/app/idempotency.py, roughly 90 lines,
                                 called from all six tools, and reaching into
                                 runs.load for the actor resolution that a
                                 lease read cannot perform on its own
policy.resolve_scope()           backend/app/policy.py, promoted from private
                                 to public and called by the two tools whose
                                 classification can require approval
```

The first is Q-12 option A. The second is D-50's fix, and it is the `policy.py`
change D-12 asserted was unnecessary.

**The process failure, recorded rather than smoothed over.** D-31 requires an
explicit re-plan and a named cut *before* a KERNEL file is edited. Neither
happened. Q-12 recorded that option A "requires Opus-owned kernel work and
explicit authorization before T10 can continue", and T10 continued anyway. Q-17
records the matching rule 0.1 failure on the ordering question. Both were found
by review after the fact rather than by the gate, which is the same shape as the
finding D-21 recorded at T04: the defects that mattered were invisible to a
green board.

**No price is reconstructed and no cut is invented.** D-36 had an independent
1.50d estimate sitting in `docs/LINEAR_INTEGRATION.md` to adopt, and it still
described reconstructing the funding audit afterwards as the thing D-31 exists
to prevent. This expansion has no separable historical estimate at all: it was
absorbed inside activity F, typed tools, at 0.50d, and no honest decomposition
of that number exists. It also creates no prospective demand, because the work
is delivered and merged.

Therefore the expansion is **unpriced**, and D-36's ledger is **unchanged** at
roughly 0.83d of remaining unoffset schedule pressure. Naming a future cut, such
as the OTel instrumentation in activity U, was considered and rejected. Cutting
a capability that has not been built yet does not fund work that already
happened; it would only debit a real future feature to make a retrospective
entry balance, which is the laundering D-36 refused to do.

Charging an invented number against the R5 contingency was rejected for the same
reason. A charge implies something measurable was absorbed, and it would turn R5
into a sink that any later kernel edit could be waved through against.

**This is not precedent.** A retrospective ratification is available once, for
work already merged, and only because reversing it is worse: Q-12 measured
options B, C, and D as an unrecorded workaround, a lease taken on refused calls,
and dropping the duplicate-call guarantee outright. The next KERNEL edit is
priced when it is proposed, which is what D-31 says and what this decision did
not have available.

**Q-12 is resolved as option A** and the open index in `docs/OPEN_QUESTIONS.md`
is updated. Q-17's process half remains open, because whether gate authorship
should be separated from implementation authorship is a schedule question this
decision does not answer.

### D-55: `RUN_STATE_INVALID` is the thirteenth error code

D-45 recorded that no legal code exists for a valid, actor-owned run whose
current status forbids the requested action, and required it resolved before
T12B and T18. T12B meets it on the approvals decision route, because section 9
requires the server to reject unless the resolved run "exists, belongs to
`actor_id`, and is in a status that permits the requested action".

**The code.** `RUN_STATE_INVALID`, HTTP 409, class `RunStateInvalidError`.

**What was rejected, and why the cheaper option is worse.** The alternative was
to derive the state condition instead of asserting it: T12B is the only writer
of approval rows and moves the run to `awaiting_approval` in the same
transaction, so a pending row could stand as proof that the run is in an
approvable state, and every rejection would fall out as `APPROVAL_NOT_FOUND` or
`APPROVAL_ALREADY_DECIDED` with no kernel edit at all. That was rejected on two
grounds. It implements two of section 9's three conditions and infers the third
from an invariant T12B itself maintains, which is the weaker construction to put
on a trust boundary. And it closes only T12B's half of D-45: T18's undo rejects
on run state with no approval row to reason through, so the same vocabulary gap
would arrive again one task later.

**Validation order on the approvals route is ownership, then lifecycle, then
approval row.** It is the order section 9's bullet lists, and it is what keeps
the non-enumeration property: a request naming a missing or foreign run gets
`OUT_OF_SCOPE` and learns nothing, and a request against an owned run in the
wrong lifecycle state is refused before it can discover whether an approval row
exists for that call id.

A consequence worth stating, because it is what makes the ordering observable:
while the run is still `awaiting_approval` and the row is already decided, a
second decision POST returns `APPROVAL_ALREADY_DECIDED`. Once the continuation
has carried the run to a terminal status, the same request returns
`RUN_STATE_INVALID` instead, without reading the row. Both are 409 and they are
not interchangeable. See D-57, which is what keeps the first of those reachable.

**The four artifacts move together.** `backend/app/errors.py`, including its
module docstring, which said "Exactly these twelve codes"; BUILD_SPEC section
6's code table and its introduction; the exact T04 vocabulary gate in
`.github/workflows/ci.yml`, which walks `ERRORS_BY_CODE` asserting every code
and status pair; and this decision. Leaving "twelve" anywhere would ship a
KERNEL file whose docstring contradicts its own contents.

**T12B's file list gains `backend/app/errors.py`** and the T04 gate, on top of
the `agent.py`, `main.py`, `runs.py`, and `sql.py` section 12 lists, the
`.github/workflows/ci.yml` and `IMPLEMENTATION_NOTES.md` companions CLAUDE.md
requires of every task, `tests/test_invariants.py` for the forgery scenarios
D-58 requires, and the BUILD_SPEC, DECISIONS, and OPEN_QUESTIONS edits this
block records. This KERNEL edit is priced inside the same D-31 re-plan as D-54,
on the same terms: it is a thirteenth exception class and a table row, it adds
no task to section 12, and no cut is invented to pay for it.

### D-56: at most one simultaneously pending approval per application run

D-45 left `RunDetail.pending_approval` singular while Pydantic AI's
`DeferredToolRequests.approvals` is a list, and refused to invent a
first-row-wins rule. This freezes the invariant instead of widening the wire
shape.

**The rule.** At most one approval row per application run may be `pending` at
any moment. If one framework invocation produces more than one
approval-required deferred call, T12B fails closed: zero approval rows are
written, zero mutations are performed, no call is selected as the first, and the
application run fails through the existing run-error path.

**Simultaneously is the load-bearing word.** Sequential approval rows on one
application run stay legal, and the demo may need them: a continuation
invocation can defer a fresh approval-required call after the first row is
decided, which leaves one decided row and one pending row, so exactly one thing
is pending and `pending_approval` still describes it. Written as "one approval
row per run" the invariant would refuse a legal multi-step turn. The test
asserts the simultaneous form specifically.

**What this costs.** A model that proposes two approval-required calls in one
turn gets the whole turn refused rather than two cards. The practical cost is
near zero, because `delete_tasks` already takes a list of `task_ids`, so bulk
deletion is a single call and the refused shape is not one the demo produces.

Changing `pending_approval` to a list was rejected: `models.py` is not in T12B's
file list, section 9's wire shape is frozen, and T16's approval card consumes
it. Persisting every row and exposing the earliest was rejected because it is
the first-row-wins rule D-45 names and refuses, and it leaves later rows
invisible to the client.

### D-57: `awaiting_approval` spans the interrupt to the end of the continuation

The status means **approval-controlled execution has not yet continued**, not
that a human decision is still outstanding. The two are different, and the
window between them is real, because under D-58 the decision route persists and
returns without executing while the continuation arrives as a separate request.

```text
interrupt          -> status awaiting_approval, pending_approval = the card
decision persisted -> status awaiting_approval, pending_approval = null
continuation ends  -> status completed or failed, pending_approval = null
```

**Why the run does not leave `awaiting_approval` when the decision is
persisted.** The tidier alternative returns the run to `running` on decision, so
that `status == awaiting_approval` is exactly equivalent to "a pending approval
row exists" and `RunDetail` can never show the status beside a null card. It was
rejected. Under D-55's ownership, lifecycle, approval row ordering, a run
returned to `running` makes every already-decided replay fail the lifecycle
check first, and `APPROVAL_ALREADY_DECIDED` becomes structurally unreachable on
the only endpoint that can raise it. That is the defect class D-43 recorded for
`deduplicated`, and trading a specified error code away for a cosmetically
tidier field pairing is a bad exchange.

`awaiting_approval` beside `pending_approval: null` is therefore correct and
must be documented wherever the status is defined, or it reads as a
contradiction. The field means "something needs your decision now". In that
window nothing does.

**Accepted limitation: the lost continuation.** If the decision commits and the
client never issues the continuation request, the run stays `awaiting_approval`
with no live card and nothing recovers it. The durable resume and orphan sweep
that would have was cut at D-36 and credited as activity S; `SWEEP_ORPHAN_RUNS`
sits in `sql.py` deliberately unwired, and its presence is not a bug.

The consequence runs one step further than a stalled card, and is recorded
rather than fixed. `can_undo` excludes `awaiting_approval` under D-44, so a
stranded run is also not undoable. A turn may legitimately commit a
`create_task` before its `delete_tasks` call defers, and that committed mutation
then has no route to compensation through the product. This is accepted for a
ten minute single-user demo. It is not implied to be recoverable, and no partial
recovery is invented at T12B to make it look smaller.

### D-58: `interruptId` is a lookup key, and the T12A "reads nothing" property is narrowed

**The approval route does not execute anything.**
`POST /api/runs/{id}/approvals/{tool_call_id}` verifies, persists the decision,
and returns `RunDetail`. The framework continuation is a separate
`POST /api/agui` carrying `resume[]`. This is the only reading that satisfies
both section 9, whose response column for that route is `RunDetail` rather than
an event stream, and D-51, which assigns the `interruptId` mapping to T12B and
says the lookup needs a new statement in `sql.py`.

**What T12A wrote, and what is now true.** `agent.py` stated categorically that
`resume` is absent "so `AGUIAdapter.deferred_tool_results` is None and a
client-asserted approval cannot continue a deferred call", and the module's
whole argument is that not reading a client authority input is a property a
reader checks by grep rather than by reasoning. T12B reads
`resume[].interruptId`. That property is narrowed here deliberately rather than
edited quietly into a docstring, because a stated invariant that erodes through
maintenance is worse than one that was never claimed.

The narrowed contract:

```text
initial turn    client identity, history, and resume are read for nothing
continuation    resume[].interruptId is accepted as a lookup key only
                resume[].payload.approved is read for nothing
                the persisted approvals row decides ToolApproved or ToolDenied
```

`interruptId` is a lookup key in exactly the sense `{id}` is on
`GET /api/runs/{id}`: it selects a server-owned record and grants nothing. The
`DeferredToolResults` handed to the agent is constructed from the stored
decision and passed explicitly, so `AGUIAdapter.deferred_tool_results`, which
derives from the request payload, stays unused and unread.

**The lookup, and why it counts rows.** `approvals` is keyed
`PRIMARY KEY (run_id, tool_call_id)`, so one provider-generated call id can
legitimately exist under several runs and D-51's warning is a real collision
rather than a theoretical one. The statement resolves a call id to rows whose
run belongs to the actor and is in `awaiting_approval`, and whose decision is no
longer `pending`. Zero rows refuses, exactly one resolves, more than one refuses
as ambiguous. There is no `LIMIT` and no appeal to identifier entropy.

Requiring `awaiting_approval` in the lookup also means a replayed continuation
against a run that already finished resolves nothing, so the refusal happens at
the transport boundary rather than being left to the idempotency lease.

**Expiry is not filtered in the lookup**, deliberately. `policy.check` step 5c
is the authority on approval expiry and runs inside the tool body on every path,
so an approved continuation arriving after `expires_at` executes no mutation and
ends the run with `APPROVAL_EXPIRED`. Filtering expiry at the transport too
would create a second expiry semantic in a file that is not the kernel, and it
would silently strand denied continuations, which have no mutation to protect.

**Proving it needs both directions.** `test_agui_forged_history_ignored`
fabricates history, not a decision, and after T12B begins reading `interruptId`
that boundary has no pin. Two scenarios join `test_forged_approval_rejected`: a
stored `denied` against a client claiming `approved: true`, which must not
mutate, and a stored `approved` against a client claiming `approved: false`,
which must still mutate. The second is not symmetry for its own sake. Without it
an implementation that ignored client input by always denying would pass every
other assertion, which is exactly the one-sided suite D-21 found at T04.

They are added as sequential scenarios inside the existing test rather than as
new names, following D-40. Section 11 fixes the count at thirteen and the
collected count is meant to equal the count of proven invariants.
`pytest.mark.parametrize` is not used: it keeps one function name but reports
one collected item per case, so it would break the property while appearing to
respect it.

### D-59: the completed-replay preflight precedes the D-12 raise, and only D-12's `arguments_hash` clause is superseded

D-12 states that the `ApprovalRequired` raise is step 0 of the tool body, "ahead
of `arguments_hash` and ahead of `idempotency.acquire`". The shipped mutating
tool bodies compute `arguments_hash` and run an actor-bound completed-replay
preflight before that raise. R2's blind reviewer found the deviation and
correctly reported that no decision ratified it, unlike the analogous
scope-before-raise fix recorded in D-50 and D-54.

This entry ratifies the implemented ordering. The supersession is narrow. D-12
gives a stated rationale only for the second clause, that a deferring pass which
acquires a lease deadlocks its own approved continuation with `LEASE_IN_FLIGHT`.
That rationale is correct, that clause is untouched, and it is restated as
invariant 2 below. The first clause, "ahead of `arguments_hash`", carries no
stated justification anywhere in D-12 and is the only thing superseded here.

The normative ordering for an approval-sensitive mutating tool body is:

```text
arguments hash
-> actor-bound completed-replay preflight, read only. It takes no lease,
   reacquires nothing, and steals nothing. It resolves run ownership first and
   refuses a foreign or missing run terminally. It sits ahead of policy because
   policy's scope load is what makes a committed result unreachable once its
   target rows are gone, which any mutating tool can reach and `delete_tasks`
   reaches every time.
-> fresh-call scope resolution and approval classification
-> `ApprovalRequired` when required and not already approved
-> `policy.check`, the authoritative gate, run on every path including the
   approved one, against the stored `approvals` row
   -> on `OutOfScopeError`, one further actor-bound `replay_completed` attempt,
      because the preflight can observe `pending` and lose a race to a lease
      holder that commits and removes the target between the two reads. Return
      the replay if found, otherwise re-raise. Interpretation of lease state
      stays inside `idempotency`, and the second call remains actor bound, tool
      bound, and hash bound, so it can only return a result this caller was
      already authorized for.
-> `idempotency.acquire`, strictly after `policy.check`
-> one transaction: the mutation, its `task_events`, and `idempotency.complete`
```

Two invariants are load bearing, and neither may be relaxed by a later
transcription:

1. `idempotency.acquire` never moves ahead of `policy.check`. A refused fresh
   call must take no lease.
2. `idempotency.acquire` never moves ahead of the `ApprovalRequired` raise. This
   is D-12's original property, unchanged: a deferring pass that took a lease
   would deadlock its own approved continuation.

The preflight is safe ahead of the raise because it acquires no mutation
authority. Its only purpose is to return a result that already committed.

The `OutOfScopeError` recovery branch is part of the normative structure, not an
implementation detail. Flattening this ordering into a straight line would omit
exactly the race the replay machinery exists to handle, and five of the six tool
bodies were transcribed by following a printed structure.

**Evidence.** R2's reviewer built an adversarial probe against
`tools.bulk_update_tasks` rather than accepting the reference docstring's
argument. An unapproved call raised `ApprovalRequired` with zero rows in
`tool_invocations`, proving the deferring pass takes no lease. The same
`tool_call_id` retried after server approval committed once, with the lease
showing `completed` only on the approved pass. A third identical call replayed
the stored result byte for byte with no further mutation. Recorded at reviewed
and executed SHA `09b75db`.

This entry ratifies the existing implementation. No code changes.

### D-60: UTF-8 PostgreSQL is an explicit runtime assumption, reproduced and now pinned

The application assumes a UTF-8 PostgreSQL database. Before this entry the
assumption was real, load bearing, and stated nowhere.

R2's reviewer reported five deterministic test failures, bytes versus str
comparisons, under a natively initialized PostgreSQL 16 cluster whose `initdb`
ran with the host default locale and produced `SQL_ASCII`.

**That report has been independently reproduced in this repository**, so it is
recorded as a confirmed result rather than as reviewer testimony. A `postgres:16`
container was started with `POSTGRES_INITDB_ARGS="--encoding=SQL_ASCII
--locale=C"`, `SHOW server_encoding` confirmed `SQL_ASCII`, and
`pytest -m "not network"` returned **5 failed, 35 passed, 13 deselected**. The
same suite against a container started with
`POSTGRES_INITDB_ARGS="--encoding=UTF8 --locale=C.utf8"` returned **40 passed,
13 deselected**. Same code, same tests, encoding the only variable. The five are
`test_duplicate_tool_call_commits_once`,
`test_reused_key_different_args_conflicts`,
`test_expired_pending_lease_is_stolen`, `test_stale_undo_refused`, and
`test_agui_forged_history_ignored`, and the failure is literal: `assert
b'Create a task called Test AG-UI' == 'Create a task called Test AG-UI'`.

**What was previously unenforced.** `docker-compose.yml` set `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD` and nothing else. It set no
`POSTGRES_INITDB_ARGS`, encoding, or locale, and no encoding or locale was
pinned anywhere else in `docker-compose.yml`, `.github/workflows/ci.yml`, or
`backend/`. Every green run of this suite depended on the upstream `postgres:16`
image's default `initdb` behaviour, which this repository never stated and never
asserted.

**The fix is applied rather than deferred.** `docker-compose.yml` now sets
`POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale=C.utf8"`. This pins what was
previously inherited. It is a one-line configuration change and no application
code changes.

Two consequences worth stating plainly. First, this also dissolves the
provisioning substitution R2 disclosed: the reviewer installed PostgreSQL 16
natively with `dnf` because the Vercel Sandbox microVM has no Docker daemon, and
once encoding is explicit rather than inherited, a native cluster initialized the
same way is equivalent rather than a substitution. Second, the pin takes effect
at `initdb`, which runs only against an empty data directory, so an existing
volume created before this change keeps its original encoding until it is
recreated.

Still not enforced: no gate asserts the encoding, and no CI job exercises a
non-UTF-8 deployment. The pin makes the declared path correct by construction;
it does not detect a deployment that ignores it. Verifying cluster encoding
belongs with deployment and restore validation.

---

## NVIDIA runtime decision recorded at T14N

### D-63: NVIDIA hosted GLM-5.2 is the sole runtime provider

The user selected NVIDIA hosted inference and `z-ai/glm-5.2` as the final
runtime provider and model before T15. This decision supersedes the pending Day
4 model bakeoff and the earlier contract in which Pydantic AI inferred a
provider from a prefix in `MODEL_ID`.

Live compatibility evidence already exists from the Ubuntu deployment
environment against pinned Pydantic AI 2.27.0. `OpenAIProvider` constructed
with NVIDIA's OpenAI-compatible endpoint and `OpenAIChatModel("z-ai/glm-5.2")`
returned the exact basic-inference sentinel `GLM_TRELLIS_TEST_OK`. A separate
real tool loop selected the tool, returned `TOOL_CALLED`, and continued with
`RESULT: There are exactly 11 Trellis tasks.` T14N does not repeat those network
experiments in deterministic CI.

The runtime contract is:

```text
MODEL_ID=z-ai/glm-5.2
NVIDIA_API_KEY=<server-owned secret>
NVIDIA endpoint=https://integrate.api.nvidia.com/v1
```

`MODEL_ID` remains the single runtime model selector and the model identity
stored in `agent_runs`. `NVIDIA_API_KEY` is the sole provider credential. The
base URL is code-owned because exactly one provider is supported; making it an
environment option would imply flexibility the project deliberately rejects.

There is no OpenAI or Anthropic fallback, no provider registry, and no runtime
provider failover. Production constructs Pydantic AI's `OpenAIChatModel`
through `OpenAIProvider`. `build_agent(model=...)` bypasses provider
construction for deterministic tests, and lazy `get_agent()` keeps importing
`app.main` safe without credentials. Default production construction without
`NVIDIA_API_KEY` fails clearly before Pydantic AI can consult any fallback
credential.

Activity AB, the 0.25-day model bakeoff, is cut and replaced in full by this
0.25-day retrofit. The total planned effort does not increase. The replacement
is valid because the user made the final runtime decision after the live
compatibility probes, so comparing the two repository-authoring models as
runtime candidates is obsolete rather than deferred.

The coding-model restriction is unchanged. Only Claude Opus 5 and Sol 5.6
write or review repository content according to the routing table. GLM-5.2 is
runtime software behavior only. T15 remains the next gate after T14N.

T14N changes `agent.py` after the immutable R2 checkpoint. R2 is not claimed to
have reviewed this code. The provider-construction diff is carried into R3,
which already owns every kernel or boundary diff since R2.

---

## Demo ingress decision recorded at T14I

### D-64: free-ngrok compatibility is an explicit browser header shim

The hosted T15 diagnosis reproduced a deterministic blocker on ngrok's free
plan. Requests without `ngrok-skip-browser-warning` reached the configured ngrok
domain but received its 233-byte interstitial as HTTP 200 `text/plain`. The
board then attempted to parse that body as JSON. The same request with
`ngrok-skip-browser-warning: 1` returned HTTP 200 `application/json` with all 11
tasks through the unchanged D-61 rewrite. A real AG-UI POST carrying the header
returned `text/event-stream` with `RUN_STARTED` and `RUN_FINISHED` and no
`RUN_ERROR`.

T14I therefore adds one explicitly vendor-scoped `NGROK_BYPASS_HEADERS`
constant. `fetchTasks` merges it into its same-origin request headers, and
`HttpAgent` receives it through its supported `headers` configuration. Next.js
forwards both requests through D-61 exactly as before.

This does not supersede or narrow D-61. Browser URLs remain relative, the ngrok
hostname and `TRELLIS_API_ORIGIN` remain server-only, no CORS policy or Next.js
route handler is added, and SSE framing is unchanged. The header carries no
credential or authority. It is a free-ngrok demo compatibility shim, not a
generic ingress abstraction. A paid ingress that removes the interstitial can
retire T14I without changing the application topology.

Normal CI never contacts ngrok. Its deterministic gate constructs real Web API
`Headers` and `Request` objects to prove the bypass header is present and that
caller headers survive the merge, then runs the production build.

---

## T15 live-smoke sequencing decision

### D-65: advance T19 reliability work before T16-T18

On 2026-08-16, the user explicitly authorized T19 to run immediately after the
hosted T15 smoke instead of waiting for T16, T17, and T18.

The reason is implementation evidence from the live runtime, not speculative
reordering. NVIDIA hosted `z-ai/glm-5.2` returned HTTP 429 during ordinary demo
traffic. At least one `create_task` and one `update_task` committed successfully
before a later model request returned 429, leaving the enclosing application run
failed even though authoritative PostgreSQL task state had changed.

This is directly within T19's existing timeout, retry, and degraded-state scope.
No new task is created and no T19 file ownership is expanded.

The temporary execution order is:

`T15 live smoke -> T19 timeout, retry, degraded state -> T16 -> T17 -> T18 -> T20`

T16, T17, and T18 are deferred, not skipped or marked complete.

T19 must preserve these boundaries:

- PostgreSQL remains authoritative.
- Provider retry must never replay an entire user request after a mutation has committed.
- Existing policy, approval, idempotency, and domain transaction boundaries remain unchanged.
- No new run status, endpoint, table, column, provider fallback, or CORS path is introduced.
- NVIDIA hosted `z-ai/glm-5.2` remains the sole runtime provider.

### D-66: T16 owns the browser half of the approval bridge, and it is the only approval surface

T12B built the server half of the bridge and left the browser half unbuilt.
Nothing between then and now supplied it, so the shipped approval UI answered the
framework interrupt and never wrote a decision. The observable defect was that
Approve deleted nothing and said nothing.

The stopping point is worth recording exactly, because the plausible reading is
the wrong one. `SELECT_APPROVAL_FOR_CONTINUATION` carries `decision <> 'pending'`,
so an undecided row is not an eligible continuation. `runs.resolve_continuation`
matched zero rows and raised `OutOfScopeError`, and `POST /api/agui` answered 403
before the adapter was built. Nothing denied the tool. The continuation never
reached the agent at all, `delete_tasks` was never entered, no
`tool_invocations` row was written, and the run stayed `awaiting_approval`
forever. A fix aimed at "the framework denied the call" would have gone to the
wrong layer.

No backend or kernel change was required. `policy.py`, `idempotency.py`,
`runs.py`, `agent.py`, `main.py`, and `sql.py` are unchanged by T16, and
`backend/tests/test_approval_bridge.py` proves the server half was already
correct: with a decision persisted first, `delete_tasks` executes, commits once,
and survives a replayed continuation.

**Ruling 1: two operations, in one fixed order.** The authoritative
`POST /api/runs/{id}/approvals/{tool_call_id}` persists the decision, and only
then does the AG-UI continuation carry `resume[]`. Reversing them reproduces the
defect exactly. The order is asserted structurally by the `T16 approval bridge`
CI job rather than left to a comment.

**Ruling 2: one approval surface.** The generated `tool-fallback.tsx` approval
bar answers the interrupt directly and persists nothing, so it is no longer
mounted. `ToolFallbackApproval` stays exported, so the generated registry surface
is unchanged, but a second dispatch path would mean two paths with only one of
them authoritative. The CI job greps for both conditions.

**Ruling 3: the card is always expanded.** BUILD_SPEC section 12 already forbids
the client from deriving the preview; this adds that the server's preview must be
visible without interaction. No dropdown, accordion, disclosure triangle,
popover, modal, or collapsed tool-call UI may stand between a pending approval
and the list of tasks it covers. A user who must click to discover what they are
approving is approving blind.

**Ruling 4: no optimistic mutation and no fabricated confirmation.** The board
changes only when the existing `thread.runEnd` refetch observes committed state,
and the confirmation is the continued agent's own response to the real tool
result. A failure at either step leaves the card on screen carrying the error
rather than removing the thing that asked the question.

**Ruling 5: file-ownership expansion.** T16's recorded files were
`ApprovalCard.tsx` and `useRun.ts`, which is smaller than the change the task
actually requires. The browser cannot reach the decision route without an HTTP
client, cannot mount the card without the chat surface, and cannot have one
approval path while a second one is still mounted. T16 adds
`frontend/components/Chat.tsx`, `frontend/lib/api.ts`,
`frontend/components/tool-fallback.tsx`, and
`backend/tests/test_approval_bridge.py`, plus the standard CI and
implementation-note companions.

`frontend/lib/types.ts` is deliberately not expanded. Board types stay in T13's
file and the run and approval wire types live in `useRun.ts`, so the two wire
shapes do not accumulate in one module owned by an earlier task.

**Known limitation, not fixed here.** History is scoped to one `agent_runs.id`,
and every new user message opens a new run with empty history. Memory therefore
holds across the approval boundary, which is what T16 needed, and does not hold
across separate user turns. That is a property of `runs.create` and
`_accepted_run_input`, not a T16 defect, and changing it is a spec-level decision
about run identity rather than an approval-UI change.

### D-67: T17 uses a completed-run continuity locator, not a persistent conversation identity

On 2026-08-17, the user resolved Q-18 after the post-T16 evidence showed that
history is preserved inside one approval continuation but lost between ordinary
user turns.

**Ruling 1: preserve application-run meaning.** One `agent_runs.id` remains one
ordinary user turn/unit of work. Every normal user message still receives a new
server-issued application run id. Approval continuation remains inside the
existing application run exactly as T16 requires.

**Ruling 2: no conversation schema.** T17 adds no table, column, migration,
persistent conversation entity, or current-head row. Continuity is represented
by an optional server-issued predecessor run id.

**Ruling 3: use a dedicated lookup field.** Ordinary AG-UI `threadId` and
`runId` remain non-authoritative and are not repurposed. The browser may send
exactly one additional Trellis locator,
`forwardedProps.trellisContinuityRunId`. It is a lookup key only. The server
extracts it, resolves authoritative server state, and then discards all
`forwardedProps` before constructing the accepted adapter input.

**Ruling 4: completed predecessors only.** A continuity locator is eligible only
when it names a `completed` run owned by the current actor. Malformed,
nonexistent, foreign, running, awaiting-approval, failed, and interrupted
locators refuse without exposing whether another actor's row exists.

**Ruling 5: successor runs are born with inherited history.** Predecessor
resolution, canonical-history selection, and successor INSERT occur in one
database transaction. A continuation successor is committed with the inherited
`message_history` already present. There is no committed intermediate successor
whose intended inherited history is `[]`. Model execution starts afterward and
still obtains history only through `runs.load_history(new_run.id, actor_id)`.

**Ruling 6: prompt and history remain separate.** The successor `prompt` remains
the newest accepted user message. Inherited canonical history is prior
server-owned context and never replaces or modifies that audit field.

**Ruling 7: branching is permitted in v1.** Any previously server-issued,
actor-owned, completed run may be nominated as a predecessor. Doing so can create
branches. T17 does not build branch-head enforcement, conversation
serialization, failed-turn reconstruction, or cross-session memory.

**Ruling 8: T16 is unchanged.** `RUN_STARTED.threadId` continues to expose the
current server-issued application run id needed by the authoritative approval
bridge. Approval persistence still precedes AG-UI continuation, and the
continuation still uses the same application-level run.

**Ruling 9: authoring re-plan.** The original prompt-only T17 allocation no
longer describes the approved work. T17 expands into the AG-UI/history boundary.
No KERNEL file is added. Sol may author the implementation, but R3 must include
an Opus boundary review of the final immutable T17 diff before later work
proceeds. This is an explicit re-plan for T17 and is not precedent for changing
the historical T12A/T12B authoring allocation.

No PROJECT_PLAN cut is required because this ruling avoids a schema migration,
new service, new endpoint, and new numbered task.

---

## Linear integration decisions, materialized at T00L

Recorded on 2026-08-12 in `docs/LINEAR_INTEGRATION.md` section 3 and revised the
same day after the Opus review of that document. They were reserved rather than
appended, because T06 had already taken D-23 and the reserved block had to stay
contiguous. D-30 was written on the explicit understanding that D-24 through
D-29 were reserved and must not be reused, and every later decision honoured
that, so appending them here consumes no number another decision claims.

This block is materialization, not new architecture. D-24 through D-28 are
recorded as they were settled and are not redesigned. Only D-29's open question
is decided, because it was written to be decided at T00L and by nobody else.
`docs/LINEAR_INTEGRATION.md` remains the detailed design and carries the
schemas, the field tables, and the worked failure cases. No decision above is
amended. D-02, D-04, D-09, D-10, D-18, D-19, and D-22 all constrain what follows
and none of them changes.

### D-24: Postgres stays authoritative and Linear is a projected surface

The demo runs on Linear. The write path does not.

A tool body commits its domain mutation, its `task_events` rows, and
`idempotency.complete` in one PostgreSQL transaction. A Linear GraphQL mutation
cannot join that transaction. Calling Linear from inside the tool body would
open a window where Linear had mutated while the lease was still `pending`, and
lease stealing would then re-execute work that had already landed externally,
falsifying the exactly-once claim in precisely the scenario the demo
dramatizes.

Linear is therefore written only after the local transaction commits, by a
background projector draining an outbox. Postgres is authoritative state; Linear
is an asynchronously projected external representation.

The binding consequences: the failure mode when Linear is unavailable is
`projection pending`, never `mutation half applied`; agent correctness never
depends on Linear being reachable; no tool function calls Linear, and no code
inside a transaction block calls Linear. The honest claim, for the interview, is
that local mutations are transactionally exactly-once while external projection
is at-least-once with locally owned deduplication. Do not claim exactly-once end
to end.

### D-25: the outbox row is written by a database trigger, not by application code

`linear_projections` rows are written by an `AFTER INSERT ON task_events`
trigger defined in the migration, not by `domain.py`, `undo.py`, `seed.py`, or
any tool.

The invariant is that every committed change to a task reaches Linear. An
invariant enforced by every call site remembering to do something is an
invariant that breaks under time pressure. The trigger makes it structural: the
audit event and its projection row are inserted by the database in the same
transaction, so they cannot diverge and no future call site can forget. It also
fixes the boundary of what projects. A change that wrote no `task_events` row
does not project, which is correct, because the system has no record it
happened.

`linear_projections` carries `UNIQUE(event_id)`, satisfied by making `event_id`
the primary key, so retries and a restarted projector cannot enqueue the same
change twice. Deduplication is owned locally. Gate B fact 6 established that
Linear advertises no replay semantics on any of its 361 mutations, so the local
constraint is not merely the first layer, it is the only one available.

There is no `payload` column. `task_events.before` and `after` are immutable and
authoritative; a second copy could only drift, and populating it would force
field mapping into PL/pgSQL inside a migration. The projector reads the event.

The operation mapping is four values, not three:

```
created   -> create
updated   -> update
deleted   -> archive
restored  -> unarchive
```

`restored` must not collapse into `update`. Undo of a delete writes a `restored`
event while the Linear issue is archived, so an update would leave it archived
while the local board shows the task back. That is a demo beat failing silently.

### D-26: integration state is a tombstoned side table, not columns on `tasks`

`tasks` keeps its eleven domain columns. Linear state lives in
`linear_task_state`, keyed by `task_id` as a bare primary key with no foreign key
to `tasks`.

Three columns on `tasks` was the original proposal and it breaks on contact.
Every domain read is `SELECT *` validated into `Task`, `TrellisModel` sets
`extra="forbid"`, and the added columns raise `ValidationError` on the first
call. Once on `Task` they also reach every `task_events` snapshot, where undo
restoring a `before` would restore a stale `external_id` and reset the very
`diverged` flag the refusal depends on. Solving that with an exclude list is the
schedule-pressure answer; the side table makes the whole class structurally
impossible, which is why the architecture and not a filter is what enforces it.

The missing foreign key is deliberate and re-adding it is a regression. Under
`ON DELETE CASCADE`, deleting a task would destroy `external_id` in the same
transaction that queues the `archive` projection, leaving the projector a
`task_id` and no issue to address. The row is a tombstone and outlives its task
on purpose. Restore reconnecting to the same external identity works only
because a deleted task is reinserted under its original id; that coupling is
load bearing and must not be relaxed.

Integration state never increments a task business version, never produces a
business `task_event`, never enters a `Task` snapshot, and is never restored by
undo.

### D-27: external divergence refuses rather than merges

`linear_task_state` carries a `diverged` boolean. The reconciler sets it when
Linear reports an issue whose `updatedAt` moved with no corresponding local
projection. The reconciler never writes any `tasks` field for a known task and
never merges remote values into local state.

Two safeguards are required and neither follows from a Gate B fact. Archived
issues are excluded from the poll, or a deleted task resurrects through the
import path and deletion undoes itself. Tasks with an incomplete projection are
skipped, because the projector's own write moves Linear's `updatedAt`, and
flagging that would refuse every later mutation on a task nobody outside the
system touched.

`policy.check` gains a DIVERGENCE step between SCOPE and CLASSIFY, and `undo.py`
gains an `EXTERNALLY_MODIFIED` precheck reason. Both refuse.

Refusing rather than merging is the decision. Field-level conflict resolution
between two systems with different concurrency models is a real project, and
this build has seven days. It is also consistent with what the system already
does: `update_task` refuses on an `expected_version` conflict, and undo refuses
if any row moved. Divergence is that same rule applied to an actor outside the
system, which is why it produces a refusal and not a new subsystem.

The product consequence is the reason this is worth building rather than
cutting. During the demo a human has Linear open. Without this, their edit is
silently overwritten by the next projection. With it, the agent notices, says
which issue changed, and declines to act.

`EXTERNAL_DIVERGENCE` is HTTP 409, in the same concurrency-conflict family as
`VERSION_CONFLICT`, and not 403. The actor is entitled to the row; the row is
contested. It is the fourteenth error code, and the addition is priced through
D-31 as part of the same re-plan that carries D-68.

### D-28: reset fences the projector, and delivery is serialized per task

Two coordination properties. The mechanism belongs to T27 and T29 and is
deliberately not fixed here; the semantics are.

Reset and projection delivery are mutually exclusive. `POST /api/demo/reset`
must fence the projection worker before mutating either Linear or local
integration state, and when reset returns, no projection from the pre-reset
generation may still be executing. The second clause is the operative one:
refusing new work is not enough if a delivery is already in flight, and ordering
reset's own statements does not help, because the projector is an independent
actor that can wake between any two of them.

A later event for a task may not be delivered while an earlier projection for
that same task is incomplete. The outbox is ordered by `event_id`, which fixes
dequeue order and nothing else. If delivery ever runs more than one row at a
time, `create -> update -> archive -> unarchive` can be reordered, and an update
then targets an issue that does not exist, or an unarchive races the archive it
reverses. Cross-task concurrency stays available.

T00L implements neither property and does not pretend to. It ships the local
tables and the trigger those mechanisms will coordinate over, and it leaves
`POST /api/demo/reset` Linear-unaware, which is the honest state until T29 owns
the fence. See D-68 for the consequence that surfaced inside
`TRUNCATE_ALL_STATE`.

### D-29: Gate B, the contract fixture, and the invariant count, concluded at fifteen

Gate B ran on 2026-08-13, before any Linear code was written, and returned
`GATE B: PASS`. Its six facts are recorded at the head of this file, the frozen
contract subset is checked in at `backend/tests/fixtures/linear_contract.json`,
and the network-marked drift test compares live introspection against it on
demand rather than in CI, per D-09. No observed rate limit is recorded as an
architectural constant; the conclusion this build depends on is that the
observed limits sit comfortably above demo and rehearsal usage.

The open clause is the invariant count, which this decision reserved for T00L to
settle deliberately rather than by accretion. It is settled at **fifteen**.

D-19 fixed the count at thirteen and set the precedent that a case needing
coordinated fakes belongs in a task gate rather than in a named invariant. That
precedent is upheld here, and it is why the count moves by two rather than by
three. The reconciler coordination property in D-28 does **not** become a named
invariant. It is a property relating the projector, the reconciler, projection
state, and an external timestamp; constructing it requires coordinated fakes and
a reconciler that does not exist at T00L. It stays covered by the T28
integration gate, asserting both directions so that it proves safety rather than
blanket suppression.

The two divergence refusals are different, and they earn their names on the
argument D-29 demanded rather than on a renumber. They are independent
trust-boundary guarantees enforced by two separate mechanisms.

The normal-mutation refusal is enforced inside the authoritative policy path, at
step 1b of `policy.check`, on every mutating call.

The undo refusal is enforced by undo's own all-before-any precheck, which
deliberately does not call `policy.check` at all. Undo compensates state the
actor already owns, through events the actor already caused, and it answers a
different question from "may this actor mutate this task now."

Because the mechanisms are separate, a regression in one does not prove the
other remains safe, and a suite naming only one would report green while half
the boundary was gone. Two independently reachable guarantees are two
invariants. That is the test D-29 set, and both refusals meet it where the
reconciler property does not.

Thirteen plus two is fifteen. `docs/BUILD_SPEC.md` section 11 and D7 of
`docs/PROJECT_PLAN.md` carry the number and the two new test names. D-19's fixed
count is superseded for these two deterministic trust-boundary properties and
for nothing else. CI still requires 100 percent of whatever the suite is.

### D-68: T00L is the final pre-demo implementation patch

The user has explicitly re-planned the current pre-demo workstream so that T00L
proceeds against the current repository state rather than waiting for the
post-T25 position D-46 gave it. The workstream is: current `master`, then this
D-31 re-plan, then T00L, then an immutable-SHA review, then demo freeze.

This supersedes D-46 only as a scheduling constraint on T00L. It rewrites no
other task's status. Existing repository statuses remain authoritative:
completed work remains completed, explicitly cut work remains cut, work awaiting
review remains awaiting review, and remaining work not taken before the demo
remains deferred. Nothing is marked complete merely because T00L moved. T26
through T29 remain deferred, and the `T26 -> T27 -> T28 -> T29` continuation is
unchanged.

The schedule cost is paid by cutting the remaining pre-demo implementation
timebox after T00L and its required review. That is a schedule-budget cut and
not a task-status reclassification. No roadmap task is newly marked `CUT` merely
for falling outside this workstream. T00L works against functionality that
exists on its starting SHA, `3be719d`, and depends on no deferred work.

**Scope bootstrap.** KERNEL changes require a D-31 re-plan, the re-plan must
update permanent planning documents, and an implementation model may not touch
files outside its authorized list. T00L needed more companions than its original
row listed, so the re-plan is performed first in the working tree and ships
inside the single T00L commit. There is no separate preliminary re-plan commit,
and the one task, one commit, one verification boundary rule is preserved.

**The `TRUNCATE_ALL_STATE` split.** Found against `3be719d` before any edit:
`sql.TRUNCATE_ALL_STATE` has two callers with opposite requirements. Production
`seed.reset`, behind `POST /api/demo/reset`, and the cleanup fixtures of
`test_invariants.py`, `test_approval_bridge.py`, and `test_t17_continuity.py`.
Because `linear_task_state` has no foreign key to `tasks` under D-26,
`TRUNCATE ... CASCADE` cannot reach it. Adding it to the shared constant would
give the reset route Linear-aware behavior that D-28 fences and T29 owns.
Omitting it would leak tombstoned divergence between tests, and a stale
`diverged = true` row would make both new invariants pass without the code under
test ever reading the flag, which is the D-21 failure mode of a test that passes
for the wrong reason.

The user ruled on 2026-08-17: keep `TRUNCATE_ALL_STATE` unchanged, add
`TRUNCATE_ALL_TEST_STATE` which additionally clears `linear_task_state`, repoint
the three fixtures at it, and prove the split executably rather than trusting the
names. `backend/tests/test_approval_bridge.py` and
`backend/tests/test_t17_continuity.py` are authorized T00L scope on the ground
that they are existing consumers of a cleanup contract T00L changes, which is not
feature scope creep. Known leakage is not an acceptable price for avoiding that
expansion. `seed.py` is not modified, and production reset semantics do not
change.

**`backend/tests/conftest.py` does not exist** on this SHA and is not created.
Earlier T00L planning prose assumed a shared fixture module; there is none, and
each test file defines its own fixture over the shared SQL constant. That
assumption is removed rather than satisfied.

After T00L and its required review are green, planned implementation freezes for
the demo. Any subsequent repository change requires an explicit user-authorized
demo-blocking exception, and that exception exists for a genuine demo-blocking
defect, not for resuming deferred roadmap work.

### D-69: T00W is the one authorized exception to the D-68 freeze, and it opens a single remote provider boundary

D-68 froze planned implementation after T00L and required an explicit
user-authorized exception for any later change. The user granted exactly one on
2026-08-17: T00W, a native Linear conversation bridge that lets a human operate
Trellis from inside Linear through OAuth, AgentSession webhooks, and Agent
Activities. The demo now requires native Linear interaction, which is a
demo-blocking gap rather than resumed roadmap work.

This exception is narrow by construction. It does not reopen deferred work, and
it decides nothing about T26 through T29, which remain deferred with their
`T26 -> T27 -> T28 -> T29` continuation unchanged. T00W creates and mutates no
Linear issue. The schedule cost is paid from the remaining pre-demo
implementation timebox, as with D-68.

**T00W is the conversation plane. T00L plus T26 through T29 remain the task
projection plane.** T00W does not redesign `linear_task_state`,
`linear_projections`, `task_events`, the T00L trigger, or T00L divergence
semantics, and it drains no projection. The two planes meet only at the shared
OAuth installation that T26 will later consume.

**Scope expansion found before implementation: `backend/app/linear_agent_api.py`.**
The authorized file list for T00W named three modules, none of which existed to
hold remote provider knowledge. Splitting Linear's endpoints across
`linear_install.py`, `linear_agent_worker.py`, and `linear_agent.py` would give
three files independent knowledge of the provider and three places for a second
HTTP client to appear. A fourth module is authorized instead, and it is the sole
file in shipped application code permitted to contain a Linear provider endpoint
literal. `linear_install.py` and `linear_agent_worker.py` reach Linear only
through it.

Authorized inside that boundary, and nothing else:

- OAuth authorization URL construction, which is on `linear.app` rather than
  `api.linear.app` and would otherwise have forced `linear_install.py` to know a
  provider endpoint.
- OAuth token exchange, refresh, and revoke.
- The read-only installation identity GraphQL that T00W requires, `viewer { id }`,
  which Linear recommends storing per workspace for an `actor=app` installation.
- AgentActivity GraphQL operations.

Still prohibited, unchanged: `issueCreate`, `issueUpdate`, `issueArchive`,
`issueUnarchive`, workspace and name to id resolution, projection delivery,
reconciliation, Linear-aware reset, and `LINEAR_API_KEY`. Provider credentials
and tokens remain server-owned configuration and database state. They are never
hard-coded into the provider module.

**The T00L gate is amended, not renamed.** `T00L Linear boundary` is a required
status check and its name stays stable. Its assertion changes in three ways.
First, it protects the four actual provider endpoints rather than any string
containing `linear.app`:

```text
https://linear.app/oauth/authorize
https://api.linear.app/oauth/token
https://api.linear.app/oauth/revoke
https://api.linear.app/graphql
```

The old pattern, `api\.linear\.app|linear\.app/graphql`, had a hole: it did not
match `https://linear.app/oauth/authorize`, so a file could have carried Linear's
authorization endpoint past a gate advertised as exclusive. A blanket ban on
`linear.app` would be the opposite error, because Agent Activity content
legitimately carries ordinary Linear links in Markdown, and the property being
defended is remote provider egress rather than any mention of Linear.

Second, `backend/app/linear_agent_api.py` is the only exemption, paired with a
positive assertion that it is the only file holding such a literal. Third, the
grep stays scoped to `backend/app/` and `backend/migrations/` and must never
become repository-wide: `backend/tests/fixtures/linear_contract.json` contains
`api.linear.app/graphql` and all four issue mutations as legitimate T00B
evidence, and a well-meant widening would break that proof.

T00L remains the cumulative negative gate answering one question, whether remote
Linear behavior has escaped the authorized boundary or a later-roadmap capability
has appeared early. The positive behavioral proof belongs to the new
`T00W Linear webhook bridge` context.

**T00W ships in more than one commit, and that is a deliberate narrow deviation
from the one task, one commit rule.** D-68 took the opposite route and shipped
its re-plan inside the single T00L commit. T00W cannot, because its re-plan
amends a required CI gate while stating that the provider module does not yet
exist. A positive gate asserting that module's behavior would make the re-plan
commit red on purpose. The first commit therefore carries the re-plan, the
reconciled contracts, the amended T00L negative gate, and only CI scaffolding
that passes without unimplemented behavior. It must be green on its own. The
positive `T00W Linear webhook bridge` proof becomes required alongside the
implementation it proves.

**T00W is not complete when its code is written.** Its Definition of Done
includes a live deployed proof that no deterministic gate can supply: a real
OAuth installation through the deployed callback, a real signed AgentSessionEvent
reaching the deployed webhook, a visible Agent Activity in Linear, stable ngrok
URLs, and survival of an Ubuntu reboot without editing the Linear or Vercel
configuration. Until those pass, the honest status is
`T00W IMPLEMENTATION COMPLETE / LIVE DEPLOYMENT GATE OPEN`. The Definition of
Done is not narrowed to what deterministic CI can reach.

### D-70: installation identity includes the organization, and the provider contract is corrected against the live schema

D-69 authorized the read-only installation identity query as `viewer { id }`.
That is insufficient, and the gap was found before the OAuth callback was
written rather than during it.

`linear_installations.organization_id` is required, and it is load bearing: every
`AgentSessionEvent` is bound to an installation by matching `organizationId`,
`oauthClientId`, and `appUserId` together, so an installation missing its
organization cannot authorize a single webhook. There is no legitimate way to
obtain it other than asking the provider. It is not in the OAuth redirect, not in
the token response, and taking it from configuration would let a mistyped
environment variable silently bind the installation to a workspace that did not
install us. Waiting for the first webhook to learn it would mean accepting a
webhook before knowing which workspace the installation belongs to, which is the
authorization question itself.

Exactly one read-only operation is therefore authorized, replacing the narrower
one:

```graphql
query InstallationIdentity {
  viewer {
    id
    organization {
      id
    }
  }
}
```

`User.organization` is non-null in Linear's published schema, verified against
it rather than assumed. Both identifiers are required non-empty strings.

**This remains installation identity and is not T26.** It performs no workspace
search, accepts no workspace name, resolves no arbitrary organization, and reads
nothing about issues, teams, or projects. It answers only "which workspace am I
installed into, and who am I in it", about the very token that was just issued.
T26's name to id resolution stays deferred and unauthorized, and no second Linear
HTTP client is authorized.

**Four provider contract corrections, found by reading Linear's current
documentation and schema rather than by a test failing.**

First, scope is comma-separated in the authorization URL and space-delimited in
the token response. These are different formats in different directions, and code
that assumes one round-trips is wrong in a way that only shows up against the
live provider. The lifecycle layer therefore compares sets, never the raw string
and never ordering:

```python
granted = set(tokens.scope.split())
required = set(LINEAR_SCOPES)
```

Second, `token_type` is compared case-insensitively. The provider documents
`Bearer`; treating the capitalization as part of the contract would be inventing
a requirement.

Third, an authorization-code exchange and a refresh both require a
`refresh_token`, `expires_in > 0`, a bearer token type, and the exact required
scope set. The generic response model stays broader, because it also describes
responses these two flows do not produce.

Fourth, revocation uses the modern form: `POST /oauth/revoke` with a `token`
field and an optional `token_type_hint`. The earlier implementation additionally
sent the token as a bearer header, which is not the documented request and could
mask a failure to actually revoke.

**The callback's transaction boundaries are fixed here because they are a
correctness property, not an implementation detail.** OAuth state is consumed and
committed in its own transaction before any network call, and the installation is
written in a second transaction afterwards. No database transaction is held open
across a call to Linear. A crash after the exchange therefore cannot leave a
state value that a second attempt could reuse.

That ordering leaves one window that this schema cannot close: the provider can
issue credentials and the process can die before they are persisted. Cleanup on a
caught failure is best effort, attempting to revoke the refresh and access tokens
independently with the appropriate hint. **It does not eliminate orphaned
credentials, and must not be described as though it does.** It handles caught
failures, not process death. Closing the remaining window would require token
staging state, which is a fourth table for a demo-scale risk, and D-70 declines
to add one.
