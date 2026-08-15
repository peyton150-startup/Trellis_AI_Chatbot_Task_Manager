# R2 review and execution checkpoint

Status: **AUTHORIZED, NOT YET DISPATCHED.**

This document is the durable record for the R2 checkpoint defined in
`docs/BUILD_SPEC.md` section "R2 review and execution gate". It holds the pinned
SHAs, the user-approved rulings, the reviewer handoff exactly as it will be
issued, and the space the returned report and its carry-forward obligations
occupy. It exists before dispatch so that the obligations R2 hands to T13 have a
home the T13 author will actually open.

This file and its pointer in `IMPLEMENTATION_NOTES.md` are documentation only.
They add no code, no gate, no endpoint, and no dependency, and the branch
carrying them is cut from the reviewed SHA so that SHA stays frozen and remains
an ancestor.

## Pinned SHAs

| Role | SHA | Identity |
| --- | --- | --- |
| R1 baseline, already certified | `e9048a2` | T08: Runs and wire contract |
| R2 reviewed and executed SHA | `09b75db` | Merge of PR #33, T12B: integrate approval interrupts |
| T12B implementation commit | `c4239a7` | Contained in `09b75db` |

The R2 window is `e9048a2..09b75db`, which is T09 through T12B. Observed diff
shape: 38 files, 7079 insertions, 4817 deletions, including the deletion of the
`spike/` tree.

**Do not derive `R2_SHA` from a local worktree HEAD.** A worktree left on the
T12A merge yields `6bb480e`, which contains no T12B. Every gate through T12A
passes at that commit, so R2 would have returned a clean PASS for work it never
reviewed.

## Repository state observed at `09b75db`

These facts were confirmed against the pinned SHA and are the basis for the
rulings below.

- 39 tracked files in total, all under `backend/`, `docs/`, and `.github/`.
- Zero tracked `package.json`. No `package-lock.json`, no `.tsx`, no
  `next.config`. There is no production frontend of any kind.
- `.github/workflows/ci.yml` contains no npm, node, setup-node, pnpm, or yarn
  reference.
- Task gates present in `ci.yml`: `T09 seed and reset`, `T10 tools`,
  `T11 prompts`, `T12A AG-UI transport`, `T12B approval interrupts`, alongside
  every earlier gate from T00 forward.
- FastAPI routes available for protocol-level probing: `GET /api/tasks`,
  `POST /api/runs`, `GET /api/runs/{run_id}`, the approval decision route,
  `POST /api/agui`, `POST /api/demo/reset`.

## User-approved rulings

1. **One Sonnet reviewer for the entire T09 through T12B window.** No Terra
   split. `CLAUDE.md` defaults Sol-authored work to Terra, and commit `761e00b`
   records T09, T11, and five of the six T10 tools as Sol-authored. The single
   reviewer is chosen for coherence: R2 is one architectural checkpoint whose
   important properties cross task boundaries, and its highest-risk surface, the
   T10 `create_task` reference plus T12A and T12B, is Opus-authored. This is a
   recorded user decision, not reviewer discretion.
2. **T12A at R2 is a protocol-level claim only.** The production Next.js and
   assistant-ui client does not exist until T13. Every T12A claim is about the
   FastAPI surface, and the reviewer must state that limit rather than soften
   the criteria to fit what is present.
3. **The frontend build gate is conditional and falsifiable, not deleted.**
   Deleting it would hide a real architectural fact, that R2 certifies AG-UI
   transport before any client exists. The gate is the thing that surfaces it.
4. **The repository is not edited to repair its own contradiction before
   review.** The reviewer records the contradiction as an OBSERVATION. An
   independent reviewer recording it is better evidence than the author
   asserting it, and no documentation edit is allowed to move the reviewed SHA.
5. **The correction lands in T13's PR**, where the frontend arrives and the
   requirement becomes executable.

### The contradiction being recorded, not fixed

`CLAUDE.md` line 90 and `docs/BUILD_SPEC.md` lines 1170 to 1178 item 5 both
require an unconditional `npm run build` at R2. At `09b75db` there is no Node
package, so that requirement cannot pass at the SHA it governs. `CLAUDE.md` line
56 does carry a "once the application scaffold exists" qualifier, but it governs
a different list and does not reach the R2 gate list. The source of truth is
internally contradictory at this checkpoint.

### User-approved R2 interpretation

The literal unconditional `npm run build` requirement conflicts with the frozen
pre-T13 repository state, because `09b75db` contains no Node package. For this
R2 only, the reviewer applies the conditional build check in section 7 of the
handoff and records the source-of-truth contradiction as an OBSERVATION. This
interpretation does not modify the reviewed repository SHA. T13 must correct the
repository documentation and make the frontend build an unconditional cumulative
gate when `frontend/package.json` is introduced.

This is a user decision, not reviewer discretion. The reviewer may not extend it
to any other gate.

## Reviewer handoff as issued

### 1. The immutable review target is already known. Verify it; do not derive it.

R1 reviewed T07 and T08 at `e9048a2`. R2 reviews the window T09 through T12B at
`R2_SHA = 09b75db`, the merge of PR #33. The T12B commit itself is `c4239a7`.

Do not run `git rev-parse HEAD` in a local worktree to establish `R2_SHA`, for
the reason recorded above.

Before reviewing, confirm all of the following against a fresh clone:

- `git cat-file -e 09b75db` resolves.
- `git merge-base --is-ancestor e9048a2 09b75db` succeeds.
- `09b75db` is the current tip of `origin/master`, or if it is not, the only
  commits ahead of it are documentation-only and must be listed in the report.
- The review operates on a detached checkout of `09b75db`, not on a branch that
  can move.

Inspect the raw diff `e9048a2..09b75db`.

Do not include author chat reasoning, PR discussion, or any summary claiming
where defects probably are in the blind-review prompt. Give the reviewer the
governing task specifications, the raw diff, and the verification evidence,
neutrally.

### 2. Reviewer

Do not have Opus review its own T12A/T12B implementation.

Dispatch one Claude Sonnet agent as the neutral, blind, read-only R2 reviewer
for the entire review window from T09 through T12B.

Sonnet reviews all of:

- T09 seed and reset
- T10 `create_task` reference implementation
- T10 remaining five transcribed tools
- T11 prompts and provenance
- T12A AG-UI transport and server-owned history
- T12B approval boundary
- all cross-cutting changes introduced in the R2 window

Do not split the review between Sonnet and Terra. Record the routing choice and
the user-approved R2 interpretation, both reproduced above, in the R2 report.

The reviewer must not edit, generate, stage, commit, or fix repository content.

It must operate in exactly three phases, and must not run code before the READ
phase is complete:

```text
READ -> EXECUTE -> RECONCILE
```

### 3. READ phase

Review `e9048a2..09b75db` against `CLAUDE.md`, `docs/BUILD_SPEC.md`, and the
decisions those tasks explicitly consume.

Do not relitigate unchanged T07 or T08 code already certified by R1. Re-enter
earlier code only where T09 through T12B changed a shared file or created a
regression against an earlier invariant.

#### T09, seed and reset, D-48 exception

- Zero body bytes accepted.
- Any body bytes, including JSON, `null`, and whitespace, rejected 422 before
  mutation.
- Administrative seed writer remains narrowly scoped.
- `INSERT_SEED_TASK` has only the authority the fixture needs.
- Reset is one transaction across truncate plus all inserts.
- Successful reset leaves only the 11 fixture tasks and no audit or control
  history.
- Deterministic IDs and semantic fields are stable, but timestamps and order are
  not falsely treated as fixture contracts.
- A late real constraint failure restores the complete controlled pre-reset
  baseline.

T09 was intentionally deferred to this checkpoint rather than separately
reviewed.

#### T10, tools

Deep-review `create_task` as the reference implementation, then verify the other
tools preserve the same load-bearing orchestration where applicable.

- Actor-bound completed-replay preflight.
- Replay remains reachable even when a successful delete removed the original
  target.
- Fresh operations resolve scope before surfacing approval.
- Approval classification and blast-radius count are not confused with ownership
  targets.
- `blocked_by` cannot create an existence or scope oracle.
- Policy runs before new mutation authority is acquired.
- Mutation, `task_events`, and `idempotency.complete` share one PostgreSQL
  transaction.
- A failure cannot overwrite an already-completed lease.
- Read-only and `propose_plan` behavior do not accidentally create domain
  mutations or events.

Do not spend review budget line-by-line reviewing six copies independently.
Review the reference and the shared structural invariants, then spot-check
transcription.

#### T11, prompts and provenance

- Task titles and notes remain untrusted data.
- Safe rendering keeps them inside the delimited data block.
- Embedded task text cannot become system instruction.
- Unsafe rendering exists only as the deliberate later demo path.
- Clarification behavior is stated rather than guessed.
- All six tool roles are represented consistently with the typed tool surface.

#### T12A, AG-UI transport and server-owned history

This is a high-risk boundary.

**Scope limit for R2.** The production Next.js and assistant-ui browser client
does not exist at `09b75db`. The repository contains no tracked `package.json`
and no frontend source of any kind. T13 is the first frontend task. Every T12A
claim at R2 is therefore a protocol-level claim about the FastAPI surface, and
the reviewer must state that limit explicitly in the report rather than
softening the criteria to fit what is present.

Verify that a client request contributes only the newly accepted user message.
Client-supplied message history, tool state, context, forwarded props, resume
data, thread id, and run id must not become authority.

Keep "must not become authority" rather than "must be ignored": T12B
legitimately reads `resume[].interruptId` as a lookup key.

Verify:

- Canonical history comes from PostgreSQL.
- Application run identity is server issued and server resolved.
- Browser identifiers are lookup hints, not grants.
- The AG-UI SSE stream is emitted correctly by the production FastAPI endpoint
  `POST /api/agui` and can be consumed according to the proven protocol shape.
  The actual Next.js and assistant-ui browser client is not present at this SHA
  and is deferred to T13. Make no browser rendering claim.
- The committed PostgreSQL task state is available through the authoritative
  task API `GET /api/tasks` that T13's board will refetch. No browser rendering
  claim is made at R2.
- Forged history cannot manufacture a prior tool call or approval.
- One requested create produces one committed mutation rather than duplicated
  work.

#### T12B, approval boundary, highest priority

Review the entire approval bridge end to end. The required shape is:

```text
model proposes approval-required tool
-> framework interrupt
-> server creates authoritative pending approval
-> browser sends only approve or deny decision
-> server validates stored approval
-> server persists decision
-> server constructs continuation
-> new underlying model invocation under the same application agent_runs.id
-> tool body rechecks stored approval
-> mutation may commit
```

Prove all seven BUILD_SPEC requirements, including both approve and deny.

Specifically attack:

- Forged approval with no pending row.
- Wrong actor.
- Wrong application run.
- Wrong tool call or interrupt mapping.
- Argument-hash mismatch.
- Expired approval.
- Already-decided approval.
- Continuation carrying client authority it should not have.
- Approve accidentally creating a second application run.
- Denial mutating anything.
- Approve committing more than once.

**Preview leakage is a first-class review target.** Approval preview is
generated before the eventual tool-body `policy.check`, so preview generation
itself must validate actor scope before fetching or displaying task details. A
missing or foreign target must not disclose its title or details, must not
generate a preview, and must not create a pending approval row. A later mutation
refusal is not sufficient, because the disclosure would already have happened.

#### Cross-cutting, from the spike deletion in this window

- No `spike/` code was carried into the production integration.
- The removal of the `T00A spike build` gate left no dangling required status
  check and no orphaned workflow reference.
- The task gate inventory in `ci.yml` still runs every established check from
  T00 forward on every pull request, with no path filters, changed-file
  shortcuts, or conditional skips introduced during T09 through T12B.

At the end of READ, report evidence-backed findings with severity, exact file
and line, and the violated contract, plus the open hypotheses that only
execution can settle. An explicit no-findings read result is valid.

### 4. EXECUTE phase, fresh Vercel Sandbox

Only after READ is complete, provision a fresh Vercel Sandbox.

Clone and pin exactly `09b75db`. Record both the reviewed SHA and the executed
SHA. They must be identical.

Install from the repository's declared dependency and lock files only. Use
Python 3.12 and PostgreSQL 16 according to the repository contracts. Node is not
required at this SHA; see the conditional build gate in section 7.

Inject secrets only through sandbox environment variables. Do not write secrets
or sandbox-specific configuration into the repository.

There is no pinned-clone or host fallback for R2. If the Vercel Sandbox cannot
be provisioned or cannot execute the required path, report R2 BLOCK.

#### Cumulative execution

Run the task-specific gates for T09, T10, T11, T12A, and T12B. These exist in
`.github/workflows/ci.yml` under exactly these check names:

```text
T09 seed and reset
T10 tools
T11 prompts
T12A AG-UI transport
T12B approval interrupts
```

Use the exact verification logic from `ci.yml`. Do not rewrite easier
substitutes.

Then run the R2 deterministic gates against that same SHA, using the repository
and CI defined working directories:

```text
cd backend && ruff check .
cd backend && pytest -m "not network"
```

Then evaluate the conditional frontend build gate. Enumerate tracked Node
packages at the reviewed SHA:

```text
git ls-tree -r --name-only 09b75db | grep -E '(^|/)package\.json$' | grep -v node_modules/
```

If that command returns one or more paths, `npm run build` must pass in the
directory the repository designates for the production frontend, and its output
must be recorded. If it returns nothing, record the literal command and its
empty output as the evidence, then record `N/A: no tracked package.json at
09b75db`. The bare sentence without the command output is not acceptable
evidence.

#### Protocol-level execution of the T12A and T12B path

Boot the backend components required by the T12A and T12B path and execute the
AG-UI plus approval flow against the sandboxed code at the protocol boundary:
drive `POST /api/agui`, consume the SSE stream directly, exercise the approval
interrupt and its continuation, and confirm committed state through
`GET /api/tasks`. No browser automation is expected or accepted as evidence at
this SHA.

Reproduce the author gates first, then add independent adversarial probes
targeted at the hypotheses raised in READ. Do not substitute a green CI page for
sandbox execution.

### 5. RECONCILE phase

Revisit every READ finding and hypothesis against the execution evidence. Label
each one CONFIRMED, WITHDRAWN, MODIFIED, or NEW DURING EXECUTION.

Separate:

- **BLOCK**: trust-boundary violation, specified behavior wrong, same-SHA
  violation, sandbox failure, deterministic gate failure, build failure, or
  anything that invalidates the interview claim.
- **NON-BLOCKING**: real, but not required to proceed to T13.
- **OBSERVATION**: evidence or limitation worth recording, but not a defect.

Do not turn preference or possible refactoring into a BLOCK.

### 6. If a BLOCK is found

The reviewer must not fix it. Report it and stop for author remediation.

Any fix changes `R2_SHA`, which makes all previous R2 evidence stale. After a
new SHA exists, rerun from the beginning: blind READ, fresh Vercel Sandbox
execution, cumulative T09 through T12B gates, ruff, `pytest -m "not network"`,
the conditional frontend build gate re-evaluated against the new SHA, then
reconcile. Do not reuse the old review or sandbox result.

Note that a remediation commit can itself introduce a tracked `package.json`.
The conditional gate is evaluated against the new SHA, not carried over.

### 7. Required final report

```text
R1 baseline: e9048a2
Reviewed SHA: <sha>
Executed SHA: <sha>
Sandbox: <fresh Vercel sandbox identity>
Reviewer routing decision: single Sonnet reviewer for the full T09-T12B window,
  user approved, per section 2
R2 interpretation applied: conditional frontend build gate, user approved,
  per section 2
```

Then: READ findings, execution results, independent probes, reconciliation,
claims not independently verified, carry-forward obligations, and the verdict.

R2 PASS requires all of the following for the same immutable SHA:

1. No unresolved blind-review BLOCK.
2. Same SHA reviewed and executed.
3. Fresh Vercel Sandbox successfully boots and exercises the T12A and T12B path
   at the protocol boundary.
4. Cumulative T09 through T12B verification succeeds.
5. `cd backend && ruff check .` passes.
6. `cd backend && pytest -m "not network"` passes.
7. Frontend build gate: if any tracked `package.json` exists at the reviewed
   SHA, excluding `node_modules/`, then `npm run build` must pass. If none
   exists, the reviewer records the enumeration command, its empty output, and
   `N/A: no tracked package.json at 09b75db`, and states which branch applied.
8. The carry-forward obligations below are emitted verbatim in the report.

#### Required OBSERVATION

The reviewer must record, as an OBSERVATION and not a BLOCK, that the
source-of-truth documents require an unconditional production frontend build at
R2 while the reviewed SHA contains no Node package. Cite the exact locations:
`CLAUDE.md` line 90, and `docs/BUILD_SPEC.md` lines 1170 to 1178, item 5.

If PASS, stop. Do not start T13.

## Carry-forward obligations to T13

These exist because the deliberate no-edit decision leaves the T13 author with
no other durable carrier for an obligation they did not create. They must be
emitted verbatim in the R2 report, and PASS item 8 makes their emission a
condition of passing.

- **CF-1.** T13 must correct `CLAUDE.md` line 90 and the `docs/BUILD_SPEC.md`
  R2 gate list so the frontend build requirement is stated in a way that is
  executable at the SHA it applies to.
- **CF-2.** T13 must add `npm run build` as an unconditional task gate in
  `.github/workflows/ci.yml` at the moment `frontend/package.json` is
  introduced, named per the repository's `T## <short verification name>`
  convention, and it becomes cumulative on every later pull request. It does not
  wait for T15. Deferring to T15 would let T13 and T14 merge a frontend that
  does not build, which contradicts the cumulative gate rule in `CLAUDE.md`.
- **CF-3.** T13 is not complete until CF-1 and CF-2 are both satisfied.

## R2 result

Not yet dispatched. This section is filled in from the reviewer's returned
report, in this pull request, without modifying `09b75db`.

```text
R1 baseline:                     e9048a2
Reviewed SHA:                    pending
Executed SHA:                    pending
Sandbox identity:                pending
Routing decision recorded:       pending
Interpretation recorded:         pending
READ findings:                   pending
Execution results:               pending
Independent probes:              pending
Reconciliation:                  pending
Claims not independently verified: pending
Carry-forward obligations emitted: pending
R2 verdict:                      pending
```
