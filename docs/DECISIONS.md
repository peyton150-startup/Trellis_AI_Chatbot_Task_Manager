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
