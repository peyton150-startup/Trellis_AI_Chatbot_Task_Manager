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

## API facts confirmed at T00

Confirmed on 2026-08-10 using Python 3.12.13, `pydantic-ai` 2.27.0,
`pydantic-ai-slim` 2.27.0, and `ag-ui-protocol` 0.1.19.

| # | Fact | Confirmed value | Date |
|---|---|---|---|
| 1 | Deferred approval type and import names | Import `DeferredToolRequests`, `DeferredToolResults`, `ToolApproved`, and `ToolDenied` directly from `pydantic_ai`. | 2026-08-10 |
| 2 | Tool approval parameter name | Register an always-gated tool with `requires_approval=True` on `@agent.tool` or `@agent.tool_plain`. The framework defers before entering the tool body. | 2026-08-10 |
| 3 | Message history serialization and restore | Serialize with `ModelMessagesTypeAdapter.dump_json(messages)` and restore with `ModelMessagesTypeAdapter.validate_json(payload)`. The serialized JSON array can be stored in `agent_runs.message_history` as `jsonb`. | 2026-08-10 |
| 4 | How conditional approval is expressed | `requires_approval` accepts only a boolean. For conditional approval, raise `ApprovalRequired` from the tool when the arguments cross the threshold and guard the raise with `not ctx.tool_call_approved`. The probe confirms 3 task ids execute directly and 4 defer when the threshold is 3. | 2026-08-10 |
| 5 | AG-UI resume request shape | Send a new POST to the same AG-UI route with the same `threadId`, a new `runId`, and `resume: [{"interruptId":"int-<tool_call_id>","status":"resolved","payload":{"approved":true}}]`. Use `approved:false` for denial. The exact route path is fixed by T00A. | 2026-08-10 |
| 6 | Whether `tool_call_id` survives the continuation | Yes. Pydantic AI emits interrupt id `int-<tool_call_id>`. `AGUIAdapter` removes the `int-` prefix and constructs `DeferredToolResults.approvals` keyed by the original `tool_call_id`; the resumed tool receives that same id in `ctx.tool_call_id`. | 2026-08-10 |

---

## Gate A: AG-UI interrupt path (T00A, Day 1)

**Result:** PENDING

**HTTP method and path for the AG-UI transport:**

**Interrupt payload shape:**

**Symptom, if FAIL:**

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
