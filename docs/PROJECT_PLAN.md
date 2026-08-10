# Project Plan: Trellis Agent Demo

**Title:** Deliver a live 10 minute demonstration of a todo agent whose destructive actions are provably bounded, on August 17, 2026.

**Companion document:** `trellis-day1-spec.md` holds the frozen architecture, data model, wire contract, and demo script. This document holds the plan for building it.

---

## Step 0: Delivery model

**Hybrid.** The outer structure is fixed and sequential: the architecture is frozen, the deadline is immovable, and the deliverable is a single demonstration. Inside each day the work is iterative, because tool schemas and agent behavior will need adjustment once real model output arrives.

This is the right split because requirements are stable (one stakeholder, one meeting, a written spec) but the agent layer is novel territory where the cost of changing course inside a day is low.

---

## Prerequisite readiness check

Construction quality is capped by what came before it, so before scheduling anything, the three prerequisites:

| Prerequisite | Status | Evidence |
|---|---|---|
| Problem definition | Complete | "Demonstrate that I can take an unreliable probabilistic component and turn it into a trustworthy product." Stated as a problem, not a solution. |
| Requirements | Complete for this scope | Six tools, five tables, wire contract, thirteen invariants, seven demo beats. Each is testable. |
| Architecture | Complete and frozen | Frozen stack, trust boundary, error-handling strategy (single path through validation, policy, lease, domain service), buy-vs-build decisions recorded, cut list recorded. |

**Biggest remaining gap:** the AG-UI approval path is unproven against an experimental API. That is exactly why it is the first activity, timeboxed, with a designed fallback. No other load-bearing unknown remains.

**Verdict: ready to construct.** Further architecture work now is rework, not preparation.

---

## Step 1: Scope statement

| Row | Content |
|---|---|
| **Justification** | Final-stage interview artifact for the Founding Full-Stack Engineer role at Trellis. The brief was two sentences and deliberately unbounded, which makes judgment and restraint part of what is being assessed. |
| **Product scope** | A todo application whose deterministic state is the source of truth, operated by an LLM agent through typed tools behind a server-owned trust boundary, with approvals, idempotency, audit trail, undo, a run inspector, and two classes of automated tests. |
| **Deliverables** | See acceptance criteria table below. |
| **Exclusions** | No durable execution engine, no auth beyond a hardcoded actor, no multi-tenancy, no deployment, no vector store or RAG, no cross-session memory, no multi-agent orchestration, no billing, no mobile, no self-hosted observability stack, no runtime model failover. Recorded with reasons in the README. |
| **Constraints** | Seven calendar days. Solo. Every dependency open source or on a usable free tier. Live demo, so failure happens in front of the stakeholder. |
| **Assumptions** | See assumption table below. |

### Deliverables and acceptance criteria

| # | Deliverable | Acceptance criteria |
|---|---|---|
| D1 | Working application | Prompt reaches agent, tool passes policy, mutation commits, board reflects committed state |
| D2 | Approval flow | Destructive and over-threshold operations show a diff preview and do not execute until approved; reject path leaves state untouched |
| D3 | Trust boundary | Server owns message history; client can send only a user message or a decision; forged or mismatched approvals rejected. `DEMO_UNSAFE_PROMPT_MODE=true` is rejected at startup unless `APP_ENV=demo`, so the deliberate vulnerability cannot be mistaken for a forgotten production switch. |
| D4 | Idempotency | Repeated tool call with same key and same argument hash returns the stored result and commits exactly one mutation. Domain mutation, audit event, and completed invocation result commit atomically in one transaction. An abandoned `pending` lease has defined behaviour: it expires after `LEASE_TTL_SECONDS` and is stolen once, which is safe because a `pending` row means the transaction never committed. |
| D5 | Undo | Single-run revert applies compensating mutations, refuses if any row version moved |
| D6 | Run Inspector | Shows tool calls, attempt status, duration, tokens, cost, and per-attempt deduplication |
| D7 | Invariant suite | Thirteen deterministic tests, no LLM calls, 100% pass, gating CI. Includes a standing regression test that fabricated client-supplied history is discarded in favour of canonical history from `agent_runs`. |
| D8 | Behavioral evals | 15 or more outcome-asserted cases with a recorded pass rate |
| D9 | Demo assets | Seed fixture, reset endpoint, rehearsed script, backup recording |
| D10 | README | Includes "what I deliberately did not build, and why" |

### Assumptions

| Issue | Approach |
|---|---|
| assistant-ui interrupt API is experimental and may not work as documented | Timeboxed spike on Day 1 with a designed fallback that costs about two hours |
| Model tool-selection reliability is unknown until tested | Narrow schemas, enums, required fields; model behind an env var so a stronger model can be swapped in |
| Provider may be degraded or rate-limited on demo day | Env var swap, plus a backup recording |
| Demo machine could fail | Repo pushed daily, docker compose reproducible, backup recording on a second device |
| Estimate may be optimistic | Pre-agreed cut order, applied without renegotiation |

### Stakeholders

| Role | Who | Note |
|---|---|---|
| Initiator and sole decision maker | Filippo Spinella, founding engineer | Wrote the brief; his judgment is the only acceptance gate |
| Driver | Nic Reilly | Owns every work package |
| Implementer | Nic, with Claude Opus 5 and Sol 5.6 only | Kernel and boundary code authored by Opus, bulk implementation by Sol, per the routing table in the build spec. No third model touches the repository, including for drills, prose, or test data. |
| Supporter | Trellis hiring process | Downstream consumer of the outcome, no input on the build |

### Requirement priority

- **Must have:** D1, D2, D3, D4, D6, D7, D9
- **Should have:** D5, D8, D10
- **Nice to have:** resume affordance and orphan sweep, OTel instrumentation, proactive plan detection

---

## Step 2: Work breakdown structure

```
1. Trellis Agent Demo
   1.1 Foundation
       1.1.1 AG-UI interrupt spike and decision record
       1.1.2 Provisional model selection and env wiring
       1.1.3 Walking skeleton (board, chat, one tool, one streaming turn)
   1.2 Domain and boundary
       1.2.1 Schema and migrations (five tables)
       1.2.2 Domain services with transactional task_events
       1.2.3 Six typed tools and Pydantic schemas
       1.2.4 Policy layer (actor scope, blast radius, approval flags)
       1.2.5 Wire contract (server-owned history, two client shapes)
       1.2.6 Seed fixture and reset endpoint
       1.2.7 Eval assertions designed on paper
   1.3 Human control
       1.3.1 Approval interrupt and diff preview
       1.3.2 Reject path
       1.3.3 Clarifying question on ambiguity
       1.3.4 Compensating undo with version guards
   1.4 Reliability
       1.4.1 Idempotency lease and 409 on hash mismatch
       1.4.2 Timeout, bounded retry, degraded state in UI
       1.4.3 Resume affordance and orphan sweep
       1.4.4 Run Inspector panel
       1.4.5 OpenTelemetry instrumentation
   1.5 Proof
       1.5.1 DEMO_UNSAFE_PROMPT_MODE toggle and injection path
       1.5.2 Deterministic invariant suite in CI
       1.5.3 Behavioral eval suite
       1.5.4 Model bakeoff against 10 representative prompts
   1.6 Delivery
       1.6.1 Visual polish
       1.6.2 README and decision record
       1.6.3 Backup recording
       1.6.4 Surprise-change drill
       1.6.5 Rehearsal
```

### WBS dictionary (selected packages)

| Package | Effort | Depends on | Risk |
|---|---|---|---|
| 1.1.1 Spike | 0.5d | none | Experimental API; fallback designed, capped at 3h |
| 1.1.3 Skeleton | 0.75d | 1.1.1 | Seam between four technologies |
| 1.2.4 Policy layer | 0.5d | 1.2.3 | The correctness kernel; everything else depends on it being right |
| 1.2.5 Wire contract | 0.25d | 1.2.4 | AG-UI adapter wants to send history; must be discarded not merged |
| 1.3.4 Undo | 0.5d | 1.2.2 | Version-guard semantics are the interesting half; cut partial undo, not the feature |
| 1.4.1 Idempotency | 0.5d | 1.2.4 | Lease states, especially pending-while-in-flight |
| 1.4.4 Run Inspector | 0.75d | 1.4.1 | The demo surface; three of seven beats resolve here |
| 1.5.2 Invariant suite | 0.5d | 1.2.4, 1.4.1 | Must not call the LLM or it will be disabled under pressure |

---

## Step 3: Schedule and critical path

Effort in days. Solo project, so nearly everything is serial and the critical path runs through most of the build.

| ID | Activity | Effort | Predecessor | Slack |
|---|---|---|---|---|
| A | Disposable AG-UI spike, Gate A (T00A) | 0.50 | none | 0 |
| B | Provisional model configured | 0.25 | none | 1.0 |
| C | Walking skeleton | 0.75 | A | 0 |
| D | Schema and migrations | 0.25 | C | 0 |
| E | Domain services | 0.50 | D | 0 |
| F | Typed tools | 0.50 | E | 0 |
| G | Policy layer | 0.50 | F | 0 |
| H | Wire contract | 0.25 | G | 0 |
| I | Seed fixture and reset | 0.25 | D | 1.5 |
| J | Eval assertions on paper | 0.25 | F | 2.5 |
| K | **Gate C: seam checkpoint** | 0 | H | 0 |
| L | Approval interrupt integration (T12B) and diff UI | 0.75 | K | 0 |
| M | Reject path | 0.25 | L | 0.5 |
| N | Clarifying question | 0.25 | G | 1.5 |
| P | **Ugly demo bar** | 0 | M, N | 0 |
| O | Undo with version guards | 0.50 | E, P | 0.50 |
| Q | Idempotency lease | 0.50 | G | 0 |
| R | Timeout, retry, degraded state | 0.25 | Q | 0.5 |
| S | Resume and orphan sweep | 0.25 | Q | 1.0 |
| T | Run Inspector | 0.75 | Q | 0 |
| U | OTel instrumentation | 0.25 | T | 1.5 |
| V | Injection toggle | 0.25 | H, T | 0 |
| W | Invariant suite | 0.50 | Q, H | 0 |
| X | Behavioral evals | 0.50 | J, I | 1.0 |
| Y | Visual polish | 0.50 | V | 0 |
| Z | README and backup recording | 0.50 | Y | 0.5 |
| AA | Rehearsal, five passes | 0.50 | Y | 0 |
| AB | Model bakeoff, 10 prompts | 0.25 | T | 0.50 |
| AC | Surprise-change drill | 0.25 | W | 0.25 |

**Delivery spine.** The formal forward and backward pass is not worth running on a solo seven-day build, and an earlier version of this line stated a path that did not match its own dependency table. What matters is the order that cannot be resequenced:

```
Gate A spike (disposable)
  → walking skeleton
  → domain services
  → typed tools
  → policy layer
  → wire contract
  → approval and reject
  → UGLY DEMO GATE
  → idempotency
  → Run Inspector
  → injection proof
  → polish
  → rehearsal
```

Everything not on that spine has slack and appears in the cut order below.

Undo is deliberately **not** on the spine. It is a should-have deliverable, and an earlier version of this plan had it gating the ugly demo bar, which was an inconsistency: a should-have was blocking a must-have. Day 3 now reaches a working agent through approval, reject, and clarification, and undo lands after that gate. If Day 3 goes badly you still finish the day with a working agent.

**Total estimated effort: 11.0 days across 7 calendar days.**

That is the most important number in this plan and it should not be softened. At an ordinary pace the scope does not fit. It is plausible only at the throughput demonstrated on Datum, where an agent did the bulk implementation while review stayed human. The margin is thin, which is why the cut order is pre-agreed rather than negotiated on Day 5, and why the STRETCH list is empty of anything the demo needs.

### Milestone view

| Day | Milestone | Gate |
|---|---|---|
| 1 | Gate A resolved, provisional model configured, skeleton running | Decision record written |
| 2 | Domain, tools, policy, wire contract, seed | **Hard stop:** seam works end to end or collapse to AI SDK |
| 3 | Approvals, reject, clarification. Undo after the gate | **Complete ugly demo:** prompt to committed board update |
| 4 | Idempotency, retry, Run Inspector, model bakeoff | Reliability beats demonstrable |
| 5 | Injection toggle, both test suites | Proof beats demonstrable |
| 6 | Polish, README, backup recording, change drill | Feature freeze at end of day |
| 7 | Five rehearsals, buffer | Demo ready |

### Two drills

Neither adds a feature. Both rehearse something the demo may require.

**Model bakeoff, Day 4 evening, 0.25 days.** This chooses `MODEL_ID`, the model the agent calls at runtime, which is a separate decision from which model writes the code. The candidates are the same two models used to build the project and no others. Do not pick from reputation. Once the six tools exist, run 10 representative prompts through each candidate: simple create, single update, multi-task request, ambiguous request, destructive request, malicious task content, date movement, dependency reasoning, irrelevant request, awkward phrasing. Score correct tool behavior, clarification where appropriate, latency, and cost. Record the table in the README.

This is scheduled on Day 4 rather than Day 1 because on Day 1 there is one tool and nothing meaningful to measure. Day 1 picks a provisional default to unblock the skeleton; Day 4 makes the real choice against the actual workload. The interview answer that comes out of it is worth the 0.25 days on its own: the model was chosen against this agent's workload, not against a general benchmark.

**Surprise-change drill, Day 6 evening, 0.25 days.** Only two models touch this project, so the change requests are sealed rather than outsourced. On Day 2, before the policy layer is deeply familiar, have Opus write five candidate change requests to `docs/DRILL.md` and do not read the file until Day 6. Examples of the shape: "bulk updates require approval at 2 tasks instead of 3," "completed tasks may never be deleted," "undo must also be blocked if the run is older than one hour."

On Day 6, pick two at random and give yourself 30 minutes each to locate the policy, change it, add or amend an invariant test, run the suite, and explain the design out loud.

This runs on Day 6, not Day 7. Day 7 has rehearsal and buffer and no room to discover a comprehension gap. The drill matters specifically because coding agents author much of the implementation: the repo has to be your system mentally regardless of who typed it, and Filippo may well open the code rather than just watch the screen.

### Cut order

Referenced throughout this plan and now stated once, here, so the evening control rule is executable rather than aspirational. Cut from the top.

**Cut first, in this order:**

1. External OTel trace visualization (keep the instrumentation, drop the viewer)
2. Resume affordance and orphan sweep
3. Undo
4. Behavioral eval count, 15 down to 8 to 10
5. Model bakeoff breadth, 10 prompts down to 5
6. Injection unsafe-mode comparison (keep the defense, drop the on-camera before-and-after)

**Never cut:**

- Walking skeleton
- Server-owned trust boundary
- Approvals
- Idempotency
- Run Inspector
- Deterministic invariant suite
- Seed fixture and reset
- Rehearsal

Cutting item 6 costs a demo beat, so it is last. Cutting item 3 shortens the human-control beat to approve and reject, which still stands on its own.

### Compression options if Day 2 slips

Adding people is not available and would not help on work this coupled. The available levers, in order of soundness: cut scope per the pre-agreed order, then extend working hours, then accept a less polished surface. Do not assume lost time is recovered later; delays compound rather than self-correct.

---

## Step 4: Responsibilities and risk

### Responsibility assignment

| Package | Accountable | Implementation | Review |
|---|---|---|---|
| Policy layer, wire contract, idempotency | Nic | Stronger model, kernel tier | Human line-by-line, plus blind review by a non-authoring model |
| Domain services, schema | Nic | Stronger model | Human |
| UI, Run Inspector, polish | Nic | Cheaper model, bulk tier | Human, visual |
| Test suites | Nic | Cheaper model | Human, adversarial reading |
| Demo script and rehearsal | Nic | n/a | Self, five passes |

The kernel and bulk split is the process already proven on Ratchet and Datum. The boundary code is kernel tier and gets the stronger model plus a non-authoring reviewer. Everything else is bulk.

### RAID log

**Risks**

| ID | Risk | Likelihood | Impact | Response |
|---|---|---|---|---|
| R1 | AG-UI interrupt API does not behave as documented | Medium | Medium | Timeboxed spike, fallback endpoint designed in advance |
| R2 | Four-technology seam does not integrate by Day 2 | Medium | High | Hard stop, collapse to AI SDK plus plain FastAPI |
| R3 | Model picks wrong tools, demo looks unreliable | Medium | High | Narrow schemas, enums, required fields, env var to swap to a stronger model |
| R4 | Provider degraded on demo day | Low | High | Env var swap, backup recording |
| R5 | Estimate overruns (11.0 days into 7) | High | Medium | Pre-agreed cut order applied without renegotiation, same evening the slip appears |
| R6 | Scope creep from continued architecture improvement | High | High | Architecture frozen; new ideas filed STRETCH and stay there |
| R7 | Demo machine or environment failure | Low | High | Daily push, reproducible compose, tested restore, backup recording on a second device |
| R8 | Invariant tests become slow or flaky and get disabled | Medium | Medium | They must not call the LLM; CI gates on this suite only |

**Actions:** resolve R1 by end of Day 1. Confirm R3 mitigation once 10 real prompts have been run.

**Issues:** none open at baseline.

**Decisions (recorded, closed):** Pydantic AI over LangGraph. DBOS cut. Runtime failover cut. Circuit breaker cut. `tasks` authoritative, not event-sourced. Signature moment 4 is the lost-response retry, not crash recovery.

---

## Step 5: Quality plan

### Explicit quality objectives

Quality characteristics trade off against each other, and teams reliably build what they are told to optimize. For this project, in priority order:

1. **Correctness of the boundary.** Every invariant holds under adversarial input. This is the thesis.
2. **Reliability.** Repeated and interrupted operations produce exactly one mutation.
3. **Usability of the demo surface.** State and consequences legible at a glance.
4. **Robustness.** Degrades visibly rather than hanging.

Deliberately not optimized: portability, reusability, extensibility, performance beyond human perception, maintainability beyond readability. Saying that out loud is what stops effort leaking into them.

### Defect detection strategy

Testing alone tops out well short of what combining techniques achieves, and inspection finds cause and symptom together where testing finds only a symptom. So: two techniques, not one.

| Technique | Applied to | Cadence |
|---|---|---|
| Non-authoring model review, then human read | Policy layer, wire contract, idempotency lease | Every kernel PR |
| Deterministic invariant suite | Boundary behavior | Every commit, CI gate, 100% required |
| Behavioral evals | Agent tool selection | On demand and before the demo, threshold |
| Smoke test | Full path, prompt to committed board update | Every build |
| Manual dirty testing | Approval and undo paths | Day 5 |

### Test case design for the invariant suite

Constructed deliberately rather than by feel, because developer-written tests skew toward confirming rather than breaking:

- **Boundary analysis** on the blast radius threshold: one below, exactly at, one above.
- **Bad data classes** against every tool: no data, too much data, wrong type, uninitialized.
- **Dirty cases** on the wire contract: forged approval, mismatched argument hash, expired approval, cross-actor mutation, client-supplied history.
- **Round, hand-verifiable numbers** in fixtures so an expected result can be checked by eye.
- Written before the code where practical. A requirement too vague to write a test for is a requirement that needs work.

Any routine in the policy layer whose decision-point count exceeds ten gets split. That code will be read aloud in an interview.

---

## Step 6: Integration and build discipline

**Strategy: T-shaped, then feature-oriented.** One deep end-to-end vertical slice first (the walking skeleton) to validate the riskiest architectural assumption, then breadth one complete feature at a time onto that skeleton. Big-bang integration is not an option here; with four technologies meeting at two seams, simultaneous failures would be undiagnosable and there is no schedule room for that.

**Daily build and smoke test.** Build and run the smoke test every day, not weekly. The smoke test is the ugly-demo path: prompt, agent, tool, policy check, commit, board update. It evolves as the system grows, because a stale smoke test manufactures false confidence. Build health is a top priority precisely on the days when schedule pressure makes cutting corners tempting.

**Configuration management.** Solo project, so this stays light: git with commits at least twice daily, remote pushed every day, docker compose as the reproducible environment. One requirement that is not optional, because it is the one people skip: **restore from a clean clone at least once, on Day 6.** A backup that has never been restored is not a backup.

**Change control.** Any idea arriving after Day 2 is logged as a Linear issue labeled STRETCH with a one-line cost estimate. It is not implemented on the day it is thought of. Logging rather than implementing ad hoc is what stops good ideas from being lost and bad ideas from being absorbed. A high volume of such requests would be a signal that the architecture work was insufficient, which is a useful thing to notice rather than to suppress.

---

## Step 7: Control loop and measurement

**Baseline:** the schedule in Step 3, 11.0 effort-days against 7 calendar days.

A seven-day solo build does not need earned value. Formal cost and schedule performance indices exist to coordinate people and money across a long contract, and paperwork that feels like ceremony is a signal the process has outgrown the project. An earlier version of this plan carried a full EVM system with PV, EV, AC, SPI, and EAC. It is cut. The number it would have computed is already known and does not improve with better arithmetic: the estimate exceeds the calendar, and the only real lever is cutting scope when a milestone slips.

**Loop:** at the end of each day, ten minutes.

| Check | Threshold |
|---|---|
| Planned packages completed | If short by more than half a day of work, cut tonight from the pre-agreed order |
| Smoke test | Green, or fixing it is tomorrow's first task |
| Invariant suite | 13 of 13, no exceptions |
| Blocker | Named, with the next action |
| Cut decision | Recorded, or explicitly "none" |

The cut happens the same evening the slip appears, not the next morning. Delays compound rather than self-correct, and the cut order exists precisely so this decision requires no deliberation.

---

## Step 8: Communication plan

| Audience | Information | Medium | Direction | Frequency |
|---|---|---|---|---|
| Filippo | Confirmation of format and timing | Email | Two-way | Once, immediately |
| Filippo | The demo itself | Live call | Two-way | Day 7 |
| Filippo | Repo and README | Link after the call | One-way push | Once |
| Self | Status, variance, cut decisions | `docs/STATUS.md` | n/a | Daily |
| Self | Decisions and their rationale | `docs/DECISIONS.md` | n/a | On each decision |

### Daily status template

```
DAY N

Planned:      <packages>
Completed:    <packages>
Smoke test:   green | red
Invariants:   n/13
Blocked on:   <item or none>
Cut today:    <item or none>
Actions needed tomorrow:  <one line>
```

The actions-needed line is the point of the report. Everything above it is context.

---

## Step 9: Day 7 operational checklist

Not an engineering activity. Run it after the final rehearsal and before the call. Every line is something that has ended a live demo for someone.

```
[ ] clean clone boots
[ ] docker compose up succeeds from scratch
[ ] seed and reset tested, twice
[ ] demo model configured in .env
[ ] API key valid, checked today
[ ] invariant suite green, 13 of 13
[ ] behavioral eval result recorded and quotable
[ ] backup video opens and plays
[ ] browser tabs prepared and ordered
[ ] notifications disabled, system and browser
[ ] screen sharing tested at demo resolution
[ ] laptop on power, second device holds the backup video
```

---

## Step 10: Closure

Planned from the start rather than improvised at the end.

1. **Hand over.** Repo link, README including the exclusions list, `DECISIONS.md`, and the compose file that brings the whole thing up in one command.
2. **Acceptance.** The demo is the acceptance event. Prepared answers for the two questions that decide it: why an LLM instead of a form, and what would come next.
3. **Archive.** Tag the demo commit. Keep the backup recording. Store the seed fixture and both suites as the reusable part.
4. **Retrospective**, within 48 hours of the call, against these questions:
   - Which estimate was most wrong, and in which direction?
   - Did the cut order hold, or was it renegotiated under pressure?
   - Did the architecture freeze hold?
   - Which invariant caught something real during the build?
   - What did Filippo actually ask about, versus what the plan predicted?

That last question is the one worth writing down carefully. It is the only external measurement of whether the judgment in this plan was correct, and it transfers to the next build regardless of the outcome.
