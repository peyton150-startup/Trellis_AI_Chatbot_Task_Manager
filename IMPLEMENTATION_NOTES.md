# Implementation Notes

This document records how major implementations fit into Trellis both as isolated parts and as contributions to the complete demo. Add one entry per major task and keep verification evidence concrete.

## T00: Pydantic AI API probe

**Local role:** `backend/scripts/api_probe.py` is an executable compatibility probe for the exact Pydantic AI and AG-UI versions used at the start of the build. It checks deferred approval imports, static and conditional approval behavior, message-history serialization, AG-UI resume parsing, and tool-call identity across continuation. `.github/workflows/ci.yml` runs this proof in an isolated GitHub-hosted Python 3.12 environment on every PR to `master` and every push to `master`.

**Whole-system role:** The approval bridge, server-owned history, idempotency keys, and AG-UI transport all depend on framework details that are easy to guess incorrectly. T00 freezes those details before database, policy, tool, or frontend code is generated. Its findings become contracts consumed by T04, T05, T08, T10, T12A, and T12B. The GitHub gate converts those contracts from local evidence into a shared merge signal and provides the first stable check name for branch protection.

**Inputs and dependencies:** T00 consumes the six questions in `docs/BUILD_SPEC.md`, Python 3.12, Pydantic AI 2.27.0, and AG-UI Protocol 0.1.19. It uses a deterministic `FunctionModel`, so no provider API key or model call is required.

**Outputs and consumers:** T00 records the following contracts in `docs/DECISIONS.md` for later implementation tasks:

- Persistent message history uses `ModelMessagesTypeAdapter` JSON serialization.
- Always-gated tools use `requires_approval=True`.
- Conditional approval raises `ApprovalRequired` from inside the tool and checks `ctx.tool_call_approved`.
- AG-UI approval resumes use a new POST containing `resume[]` entries.
- Interrupt id `int-<tool_call_id>` maps back to the original `tool_call_id` on continuation.
- Project documentation lives under `docs/`, matching the paths declared by the README and build specification.
- GitHub reports the stable `T00 API probe` check for pull requests and pushes to `master`.

**Verification:** Run `.\.venv\Scripts\python.exe backend\scripts\api_probe.py`. Success requires six `PASS` lines followed by `ALL 6 API FACTS CONFIRMED`. Facts 5 and 6 run real `AGUIAdapter` streams for both approval and denial and assert emitted interrupt, continuation, result, and tool-body evidence. After pushing, run `gh pr checks <pr-number> --watch`; the `T00 API probe` check must succeed.

**Limitations and review status:** The probe uses a deterministic `FunctionModel`, so it validates framework behavior without spending model tokens. Sol implemented T00 under an explicit user-approved routing exception. The user authorized Terra to perform a blind, read-only review without editing repository content. Record that review and the disposition of any findings in the PR. Recheck this PR with Opus before relying on it as final kernel evidence.

## T00A: Disposable AG-UI spike

**Local role:** `spike/backend/app.py` hosts a deterministic Pydantic AI agent behind FastAPI's `POST /ag-ui`. It streams a normal response, emits a fixed `delete_demo_item` tool call gated by `requires_approval=True`, records exact incoming request bodies, and counts actual tool-body executions. `spike/frontend` is a Node 22+ Next.js workbench using `HttpAgent`, `useAgUiRuntime`, assistant-ui message primitives, and the current AG-UI interrupt hooks. It renders conversation content, tool arguments, a dedicated approval card, and a four-stage protocol evidence rail.

**Whole-system role:** T00A answers Gate A before product code depends on it. It proves the selected client, transport, agent adapter, and framework approval mechanism can cross the browser and FastAPI boundary in both directions. A PASS keeps the native AG-UI interrupt design for T12A and T12B. The spike does not prove Trellis's production trust boundary, server-owned history, database approval authority, or idempotency layer; those remain later tasks.

**Inputs and dependencies:** T00A consumes T00's confirmed interrupt prefix, continuation mapping, conditional-approval behavior, and exact Python package versions. The backend runs on Python 3.12 with Pydantic AI 2.27.0 and AG-UI Protocol 0.1.19. The frontend lockfile pins Next.js 16.3.0, React 19.2.8, assistant-ui 0.15.13, the assistant-ui AG-UI runtime 0.0.53, and AG-UI client 0.0.57. The supported frontend runtime is Node 22 or newer on a release matching the locked graph's `^22 || ^24 || >=26` engine contract.

**Outputs and consumers:** T00A records `GATE A: PASS` and the exact request, interrupt, and continuation payloads in `docs/DECISIONS.md`; updates the `/api/agui` transport row in `docs/BUILD_SPEC.md`; and produces the disposable implementation under `spike/`. T12A consumes the proven POST and SSE shapes. T12B consumes the proven `int-<tool_call_id>` decision mapping while adding the real server-owned run identity and approval authority.

**Verification:** The backend imports successfully under Python 3.12. The frontend completes `npm run build` on Node 22.23.2. The original automated headless Chrome flow ran against a checksum-verified Node 20.20.2 build and observed three `TEXT_MESSAGE_CONTENT` events, rendered the normal response, asserted interrupt id `int-delete-spike-item-42`, asserted original tool call id `delete-spike-item-42`, approved and observed execution count 1, reset the backend, denied and observed execution count 0, and inspected both continuation request payloads. Because `/spike-state` polls every 400 ms, the browser waits for DOM readiness and visible evidence instead of `networkidle`. GitHub's required `T00 API probe` and `T00A spike build` checks both passed on commit `5d3ab47`.

**Limitations and review status:** This is intentionally throwaway code with an in-memory execution counter, a deterministic `FunctionModel`, permissive local CORS, and client-carried conversation history. It has no policy layer, PostgreSQL, authentication, durable run record, authoritative approval row, idempotency, or product test suite. Current assistant-ui dependencies transitively install `nanoid` 6.0.1, whose package metadata requires `^22 || ^24 || >=26`; production setup and CI therefore use Node 22 or newer on a supported release. The original locked install, production build, and browser proof also passed on Node 20.20.2 with an unsupported-engine warning. That observation is historical evidence, not a supported runtime promise. Delete `spike/` before T12A and implement the production transport from the recorded contracts, not by reusing spike code. Sol implemented T00A under the user's routing exception. Terra completed a neutral, blind, read-only review of implementation commit `5d3ab47` and reported no findings. Opus review remains required when credits are available.

## T01: Compose, environment, and initial migration

**Local role:** `docker-compose.yml` groups the stack as `trellis-ai-agent` in Docker Desktop and runs PostgreSQL 16 with the project database and credentials, a readiness healthcheck, persistent storage, and the initial migration mounted into PostgreSQL's initialization directory. It publishes unused host port `55432` to container port `5432`, avoiding the unrelated local database that owns host port `5432`. `.env.example` publishes the matching application configuration contract without secrets. `backend/migrations/001_init.sql` creates the six closed enum types, five tables, foreign keys, checks, defaults, primary keys, and indexes specified for the demo.

**Whole-system role:** T01 establishes PostgreSQL as the authoritative state and evidence boundary before application code exists. The schema separates current task state, append-only task history, server-owned agent runs, idempotency leases, and authoritative approval decisions. Every backend task from T02 onward depends on these exact names and constraints, while the Compose stack makes the database reproducible for local development, CI expansion, and the final clean-clone restore drill.

**Inputs and dependencies:** T01 consumes PostgreSQL 16, the exact environment values and verbatim schema in `docs/BUILD_SPEC.md`, and the trust-boundary decisions in `docs/ARCHITECTURE.md` and `docs/DECISIONS.md`. Docker Desktop supplies Docker Engine and Compose. Host application processes connect through `localhost:55432`; containers use PostgreSQL's internal port `5432`. PostgreSQL applies scripts under `/docker-entrypoint-initdb.d` only when it initializes a new data directory.

**Outputs and consumers:** The migration produces `tasks`, `task_events`, `agent_runs`, `tool_invocations`, and `approvals`. T02 consumes `DATABASE_URL` and these identifiers for the connection pool and SQL constants. T04 through T08 rely on the stored ownership, approval, lease, event, and run records. Later seed, tool, undo, inspector, and reset tasks consume the same schema without adding columns or tables.

**Verification:** `docker compose config` passes, and `docker compose up -d --wait` starts healthy container `trellis-ai-agent-postgres-1` on PostgreSQL 16.12. `docker compose exec -T postgres psql -U trellis -d trellis -c "\dt"` lists exactly five public tables named `agent_runs`, `approvals`, `task_events`, `tasks`, and `tool_invocations`. Live catalog assertions confirm six enum types, four explicitly declared indexes, four foreign keys, and two check constraints. A line-for-line comparison confirms `backend/migrations/001_init.sql` matches the verbatim schema block in `docs/BUILD_SPEC.md`. GitHub job `T01 database schema` recreates the Compose stack on an isolated runner, repeats the table and schema-object assertions, and removes its CI-only volume afterward.

**Limitations and review status:** The initialization mount is intentionally a clean-database bootstrap, not a migration runner; changing the SQL does not reapply it to an existing named volume. T01 creates no backend package, application connection, seed data, or runtime model selection. Those belong to later tasks. Sol implements T01 under its normal routing assignment. No Terra review is authorized for T01; Opus review is not required by the task routing table.

## T02: Configuration, PostgreSQL pool, and SQL constants

**Local role:** `backend/app/config.py` loads the documented environment contract into one frozen, typed Pydantic `Settings` instance and enforces the required unsafe-prompt startup guard. `backend/app/db.py` opens the shared psycopg `ConnectionPool` from `settings.database_url` and returns dictionary-shaped rows. `backend/app/sql.py` is the sole SQL catalog, with all 19 BUILD_SPEC names as uppercase constants. `.github/workflows/ci.yml` adds the unconditional `T02 database connection` gate while preserving the T00, T00A, and T01 jobs.

**Whole-system role:** T02 creates the stable configuration and persistence seams used by every later backend task. The pool makes PostgreSQL, rather than the browser or model, the shared authority for domain state and execution evidence. The centralized SQL catalog prevents policy, idempotency, undo, run, and domain modules from growing independent query strings, while preserving the load-bearing guarded update and lease-acquire statements for T04 through T08.

**Inputs and dependencies:** T02 consumes the T01 schema and `DATABASE_URL=postgresql://trellis:trellis@localhost:55432/trellis`, Python 3.12, Pydantic 2.13.4, python-dotenv 1.2.2, psycopg 3.3.4, psycopg-binary 3.3.4, and psycopg-pool 3.3.1. Configuration also consumes every variable documented in `.env.example`. The pool assumes Compose project `trellis-ai-agent` exposes healthy container `trellis-ai-agent-postgres-1` on host port 55432.

**Outputs and consumers:** Later modules import `settings` for typed environment values and import `pool` for dictionary-row database connections. `sql.py` exports `SELECT_TASKS_FOR_OWNER`, `INSERT_TASK`, `UPDATE_TASK_GUARDED`, `DELETE_TASKS_BY_IDS`, `INSERT_TASK_EVENT`, `SELECT_EVENTS_FOR_RUN`, `INSERT_LEASE`, `SELECT_LEASE`, `COMPLETE_LEASE`, `FAIL_LEASE`, `INSERT_APPROVAL`, `SELECT_APPROVAL`, `DECIDE_APPROVAL`, `INSERT_RUN`, `UPDATE_RUN_STATUS`, `UPDATE_RUN_HISTORY`, `UPDATE_RUN_USAGE`, `SELECT_RUN`, and `SWEEP_ORPHAN_RUNS`. T04 through T08 consume the policy, lease, event, approval, run, and orphan-sweep statements; later domain and tool tasks consume task reads and mutations.

**Verification:** Test-first executable contracts observed the missing configuration, guard, pool, and SQL modules before their implementations. Typed environment overrides and the exact unsafe-mode guard then passed. A live pool connection returned database `trellis`, and a temporary non-repository integration harness executed all 19 SQL constants inside a rolled-back transaction. From `backend`, the exact T02 command `python -c "from app.db import pool; print(pool)"` exited 0 and printed `<psycopg_pool.pool.ConnectionPool 'pool-1' ...>`. The cumulative local baseline also printed all six T00 confirmations, passed the T00A backend protocol proof and Next.js production build, and listed all five T01 tables.

**Limitations and review status:** T02 does not add domain services, request models, policy checks, idempotency behavior, undo, run lifecycle code, or pool shutdown wiring. Those remain owned by later tasks. The task-specific CI gate verifies the exact BUILD_SPEC import command, while the deeper SQL execution contract remains local because the T02 file scope does not authorize a repository test file. Sol 5.6 high implements T02 under its normal routing assignment. Terra performs the required neutral, blind, read-only review, with findings and disposition recorded on the PR. Opus review is not required by the routing table. GitHub CI and branch-protection results are recorded on the PR after the first successful T02 check.

## T03: Models

**Local role:** `backend/app/models.py` defines the closed Pydantic contract for Trellis database records, HTTP requests and responses, run inspection, policy decisions, idempotency lease outcomes, undo outcomes, and the six tool argument schemas. Every model rejects extra fields. Request and tool inputs therefore fail validation instead of silently accepting or merging unknown data.

**Whole-system role:** T03 establishes one typed boundary shared by the later policy, domain, run, route, tool, prompt, and frontend tasks. It keeps task content and mutations inside documented fields, represents all closed status and decision values as enums, and gives the server exact response shapes for the board, approval card, Run Inspector, and undo flow. This is the schema-validation layer that precedes policy, lease acquisition, and database mutation in the trust path.

**Inputs and dependencies:** The models consume the five T01 table schemas, the exact HTTP bodies and responses in `docs/BUILD_SPEC.md`, the documented `RunDetail` JSON shape, the policy, lease, and undo return contracts, and the six tool argument tables. Runtime dependencies are Python 3.12 and Pydantic 2.13.4. The test gate additionally uses pytest 9.1.1.

**Outputs and consumers:** T03 exports task, event, run, invocation, approval, and approval-preview domain models; create-run and approval-decision request models; task-list, run-created, run-detail, and undo response models; approval-requirement, policy-decision, and lease-outcome models; and argument models for `list_tasks`, `create_task`, `update_task`, `bulk_update_tasks`, `delete_tasks`, and `propose_plan`. Task event before and after snapshots preserve the schema's JSONB contract without requiring a full `Task` shape. Run usage serializes `cost_cents` as a JSON number, while the database run domain retains decimal precision. Every approval requirement carries its classified reason. T04 through T12 consume the backend contracts. T13 mirrors the response enums and shapes in frontend TypeScript.

**Verification:** Test-first verification initially failed during collection with `ModuleNotFoundError: No module named 'app.models'`. Focused audit tests then observed string serialization for run-usage cost, an omitted approval-requirement reason, and rejection of partial task-event JSON snapshots before their corrections. From `backend`, prepending `C:\Users\nicol\AppData\Local\Temp\trellis-t03-venv\Scripts` to `PATH` and running `pytest tests/test_models.py` collected 27 tests and reported `27 passed`. The stable unconditional GitHub job `T03 models` enters `backend` and runs the same command. It originally installed only Pydantic and pytest; T00R moved it to the shared pin source in `backend/requirements.txt`.

**Limitations and review status:** T03 defines data contracts only. It does not implement policy checks, idempotency behavior, transactions, domain operations, routes, AG-UI handling, or frontend mirrors. Task event snapshots, approval preview entries, and stored framework history remain JSON values because the frozen specification defines their containers but does not define narrower element schemas; no tool argument accepts arbitrary JSON. Sol 5.6 high implemented T03 under its normal routing assignment. Terra 5.6 high completed a neutral, blind, read-only review and reported one P2 finding: `TaskEvent` JSONB snapshots were too narrowly typed as a full `Task`. The finding was fixed test-first by accepting `JsonValue` snapshots, and the exact suite passed all 27 tests. No Terra findings remain. GitHub branch-protection confirmation remains follow-up work for the task PR. Opus review is not required by the routing table.

## T00R: Probe and CI pin hardening

**Local role:** T00R makes the T00 proof trustworthy under the two conditions where it previously was not. `backend/scripts/api_probe.py` replaces every bare `assert` with an explicit `require` call that raises `ProbeCheckError`, so the checks cannot be stripped by `PYTHONOPTIMIZE`; splits the six facts into a `CHECKS` registry so a failure reports the fact number and name; prints the traceback and a pointer to the affected `docs/DECISIONS.md` row; and stops asserting the private module path `pydantic_ai._deferred`. `backend/requirements.txt` becomes the single pinned dependency source. `.github/workflows/ci.yml` moves every backend job onto that file, adds the stable `T00R probe hardening` job, and stops cancelling in-progress runs on pushes to `master`.

**Whole-system role:** The recorded API facts in `docs/DECISIONS.md` are contracts that T04 through T12B consume without re-deriving. Two failure modes made those contracts weaker than they read. The probe pinned its versions inside the workflow rather than alongside the application, so a later dependency bump would have left the probe proving facts for a version nothing else installed. And the probe reported a failure as `FAIL: AssertionError:` with no fact number and no traceback, so the one event it exists to catch, a dependency upgrade invalidating a frozen fact, would have produced an unreadable signal. T00R also records three consequences that were known but unwritten, including the tool step 0 ordering that T05 and T10 depend on.

**Inputs and dependencies:** T00R consumes the completed T00 and T00A work, the six API facts in `docs/DECISIONS.md`, the cumulative CI protocol in `CLAUDE.md`, and the existing five required status checks. It depends on Python 3.12 and the eleven pins now declared in `backend/requirements.txt`. It adds no new third-party action and changes no action SHA.

**Outputs and consumers:** T00R produces `backend/requirements.txt` as the single backend pin source, which every later backend task and CI job installs from; a sixth stable check name, `T00R probe hardening`, for branch protection; and three appended decisions. D-12 fixes the conditional-approval raise as step 0 of the tool body, ahead of `arguments_hash` and `idempotency.acquire`, which T05 and T10 must follow. D-13 fixes the order for removing the `T00A spike build` required check before the T12A pull request deletes `spike/`. D-14 makes the pin source binding. The probe's `CHECKS` registry is also the seam the new CI job uses to inject a failure.

**Verification:** Against the pinned environment, `python -B backend/scripts/api_probe.py` printed six `PASS` lines and `ALL 6 API FACTS CONFIRMED` with exit 0, including under `PYTHONOPTIMIZE=1`. All eleven pins in `backend/requirements.txt` matched the installed distributions. Injecting a failure into fact 4 produced exit 1, the three preceding `PASS` lines, `FAIL 4/6 conditional approval: RuntimeError: injected probe failure`, a traceback, the `docs/DECISIONS.md` pointer, and no success line. The pre-T00R probe was checked for contrast: with a single false assertion injected into fact 1 it printed `FAIL: AssertionError:` with no fact number or traceback, and under `PYTHONOPTIMIZE=1` the same broken probe printed `ALL 6 API FACTS CONFIRMED` and exited 0. Cumulative regressions under the shared pin source: `pytest tests/test_models.py` reported `27 passed`, and the T00A spike protocol printed all four of its proofs.

**Limitations and review status:** Each backend job now installs the whole pinned set, so jobs are slower and share one install failure surface. That cost is accepted in D-14 in exchange for every gate running against the real application environment. `spike/backend/requirements.txt` keeps its own pins by design under D-14 and dies with the spike at T12A, so pydantic-ai is still pinned in two places until then. The probe still uses a deterministic `FunctionModel`, so facts 5 and 6 hold for a hardcoded `tool_call_id` and remain an assumption to re-verify against a provider-generated identifier at T12A. `PYTHONOPTIMIZE=1` is checked rather than `2`, because level 2 also strips docstrings, which pydantic-ai reads for tool descriptions, and that is a separate concern from assertion stripping. T00R changed two lines of `CLAUDE.md` beyond the four authorized files, both of which this commit would otherwise make false: the local setup command still installed a different dependency set than CI, and the required-check list omitted three existing checks. Claude Opus 5 implemented T00R under the user's explicit scope expansion of 2026-08-12. Claude Sonnet performs the required neutral, blind, read-only review, with findings and disposition recorded on the pull request.

## T04: Kernel errors and policy

**Local role:** `backend/app/errors.py` is the closed table of twelve error codes from BUILD_SPEC section 6. `PolicyError` carries `code`, `http_status`, and `message`; each subclass fixes a code and status, so a raise site selects a class rather than composing a string, and `ERRORS_BY_CODE` exposes the whole table for verification. `backend/app/policy.py` is the authoritative gate. `arguments_hash` is the single definition of an argument hash in the codebase. `classify` is the pure requirement computation. `check` transcribes section 6's six numbered steps in order: scope, classify, early return when no approval is needed, the raise when no approval row exists, the four ordered approval verifications, and the approved return. `backend/tests/test_invariants.py` holds six of the thirteen invariant tests. `backend/app/sql.py` gains `SELECT_TASK_OWNERS` and `TRUNCATE_ALL_STATE`. `.github/workflows/ci.yml` adds the unconditional `T04 kernel policy` gate while preserving all six earlier jobs.

**Whole-system role:** This is the first half of the trust boundary the whole demo rests on. Every mutation in Trellis passes `policy.check` immediately before it touches the database, on every path including the one the framework already approved, because framework approval is a UI gate and the row in `approvals` is the authorization record. The check order is the security property, not an implementation detail: scope runs first so that an approval gate can never answer a question about ids the actor does not own, which is the disclosure D-12 describes and T12B prohibits. `arguments_hash` becomes the identity of a tool call for both the approval bridge and the T05 idempotency lease, so the two agree by construction rather than by coincidence. `classify` is step 0 of every mutating tool body under D-12, which is why it must stay pure and importable without a database.

**Inputs and dependencies:** T04 consumes the T01 schema, T02's `settings`, connection pool, and SQL catalog, and T03's `ApprovalRequirement`, `PolicyDecision`, `Approval`, `ApprovalReason`, `ApprovalState`, and `ToolName` models. It consumes D-12's finding that section 6's stated framework-gating premise holds only for `delete_tasks`, and D-14's single pin source. Runtime dependencies are Python 3.12, Pydantic 2.13.4, and psycopg 3.3.4. The invariant tests additionally require a live PostgreSQL 16, because `check` reads ownership from the database and a faked lookup would prove only the fake.

**Outputs and consumers:** T04 exports `PolicyError` and its twelve subclasses, which every later rejection uses; `arguments_hash`, which T05's lease and T10's tool bodies both call and nothing else reimplements; `classify`, which T10 calls at step 0; and `check`, which every mutating tool calls at step 2 of the five-step body. Three decisions are appended. D-15 fixes the `check` signature, the `APPROVAL_REQUIRED` against `APPROVAL_NOT_FOUND` split, the `models.Approval` row type, and the decision to exercise expiry through an already-expired row rather than a clock parameter, which T05 reuses for `lease_expires_at`. D-16 records the T04-only test-authorship routing exception and the intended Sol chain for the split kernel tasks that follow. D-17 records the scope-loading contract. `SELECT_TASK_OWNERS` and `TRUNCATE_ALL_STATE` join the catalog, and T09's `POST /api/demo/reset` consumes the latter rather than duplicating it.

**Verification:** Test-first. The six tests were written against section 6's text with no kernel file in the tree, and `pytest tests/test_invariants.py` reported `collected 0 items / 1 error` with `ImportError: cannot import name 'policy' from 'app'`. Because a collection failure means no fixture ever executes, the complete fixture path was validated separately against live PostgreSQL by a throwaway script outside the repository: truncate, two `agent_runs` rows, tasks for both actors, `SELECT_TASK_OWNERS` returning both owners, a missing id returning no row, an empty id list returning no row, an approval inserted with a negative TTL, the row loaded back, `Approval.model_validate` accepting it, the preview validating against `ApprovalPreview`, the row proven already expired, a same-`tool_call_id` row under a second run, and the database left empty. After implementation, `pytest tests/test_invariants.py -v` reported `6 passed`. The complete section 6 error table was then verified locally with the same inline Python the CI job runs, printing `PASS all 12 section 6 error codes match their HTTP status and raise as PolicyError`. Finally, because one model authored both the kernel and the tests that judge it, thirteen single-line mutations were applied to the finished `policy.py` one at a time, each run and reverted: the approval gate moved ahead of scope, step 4 allowing instead of raising, step 5a dropping its `run_id` comparison, step 5a dropping its `tool_call_id` comparison, step 5b's hash comparison removed, step 5c's expiry comparison inverted, `delete_tasks` removed from the destructive set, the blast radius comparison changed to `>=` in `check` and in `classify`, step 6 rejecting a genuine approval instead of allowing it, step 5d reporting a pending approval as already decided, step 5d reporting a denied approval as merely requiring approval, and `arguments_hash` no longer sorting keys. All thirteen were detected, every one of the six tests failed under at least one, `policy.py` restored to sha256 `1f84e47943f135585b881a837796e5d1d1546807e7fa4a2b6106fe316e3acef7` matching its pre-mutation baseline, and the suite returned green. The step 5d branches use two separate targeted mutations rather than one transposition, because the two scenarios are sequential `pytest.raises` blocks in one test and a single swap would abort the test at the first failure, leaving the second branch as unproven as it was before. Several mutations exist because self-audits found the suite one-sided: every assertion was a rejection, so nothing exercised step 6 and a `check` that rejected every non-null approval row would have passed unchallenged; both the fixture and the kernel computed the stored hash with the same function, so a broken canonicalization would have agreed with itself; and neither branch of step 5d was reached by any test in this patch or by any of the thirteen named in section 11. A positive control, a known-answer digest, and the two step 5d scenarios were added before those four mutations could be detected. Cumulative regressions: `pytest tests/test_models.py` reported `27 passed`.

**Limitations and review status:** The largest limitation is authorship. Under D-16 the user granted a T04-only routing exception, so Claude Opus 5 wrote both the kernel and the six tests that prove it, where section 11 routes the test file to Sol. A self-consistent pair can be green and prove nothing, and Sonnet's final review sees that pair rather than two independent readings. The mutation evidence exists to carry the weight the authorship split would otherwise have carried, and section 11's OPUS REVIEWS half is satisfied only vacuously. Test-first ordering is evidenced by the preserved collection-failure transcript rather than by commit ordering, since everything lands in one commit. The six tests construct six of the twelve error codes and cover both branches of step 5d behaviorally. Neither 5d branch is on the production path: under D-12 step 0 raises before `check` is reached, and the server persists the decision before constructing a continuation, so both are defense against retries, races, bypasses, and incorrect continuation sequencing. The remaining six codes are covered by the complete-table check now and by their owning tasks later: `IDEMPOTENCY_CONFLICT` and `LEASE_IN_FLIGHT` at T05, `VERSION_CONFLICT` at T07, `VALIDATION_ERROR` at T08, and `TOOL_TIMEOUT` at T19. `MODEL_TIMEOUT` is named by no test in section 11 and remains covered by the table check alone. Step 5a is defense against a caller passing the wrong approval row, not the forgery gate, which is step 4 and the hash check at step 5b; in production the caller loads the row by the same two keys, so 5a can essentially never fire. `len(target_task_ids)` is preserved without deduplication per D-17, so duplicate ids inflate the blast radius count and fail closed, which will look like a bug and is not listed in section 14. `backend/app/__init__.py` is specified in section 3 and absent, so `app` resolves as a namespace package; this is recorded as Q-04 rather than fixed, because the file belongs to T02's list. T04 adds no domain mutation, no lease, no undo, no routes, and no tools. Claude Opus 5 implemented T04 under the user's explicit authorizations of 2026-08-12 covering the `check` signature, the `sql.py` scope expansion, and the test-authorship exception. Claude Sonnet performs the required neutral, blind, read-only review, receiving BUILD_SPEC section 6 directly as an independent reference for the tests, with findings and disposition recorded on the pull request.

## T05: Kernel idempotency

**Local role:** `backend/app/idempotency.py` owns the lease that makes one tool call commit at most one mutation. `acquire` executes `INSERT_LEASE`, whose `ON CONFLICT DO NOTHING` is what makes it a lease, and returns EXECUTE to the single caller that got a row back. On conflict it re-reads the row, rejects a changed `arguments_hash` before looking at status, replays a completed row's stored result, reacquires a failed row through a guard on `status = 'failed'`, steals an expired pending row through a guard on `lease_expires_at < now()`, and otherwise polls a live holder every 250ms up to eight times before raising `LEASE_IN_FLIGHT`. `complete` marks the row completed inside the caller's transaction. `fail` marks it failed on its own connection, because the transaction it would have joined has already rolled back. `backend/app/sql.py` gains `REACQUIRE_FAILED_LEASE` and `STEAL_EXPIRED_LEASE`, the only two statements in the catalog carrying a concurrency predicate. `backend/tests/test_invariants.py` grows from six tests to nine. `.github/workflows/ci.yml` adds the unconditional `T05 kernel idempotency` gate while preserving all seven earlier jobs.

**Whole-system role:** This is the second half of the trust boundary, and it is the half D-04 names as the signature reliability moment: not crash recovery, which the architecture deliberately does not claim, but the lost-response retry, where the model or the transport reissues a tool call whose result never arrived. Without the lease that retry duplicates a mutation. With it the retry replays a stored result and the database is untouched. The single-transaction rule is what makes the guarantee honest rather than approximate: because the domain mutation, its `task_events` rows, and `complete` commit together, a `pending` row is proof that nothing happened, which is the entire reason an expired lease can be stolen and re-executed safely. `arguments_hash` from T04 is the identity of a tool call for both the approval bridge and this lease, so the two agree by construction rather than by coincidence. T20's Run Inspector reads `attempt` and the COMMITTED against DEDUPLICATED distinction from the rows this module writes, and T21's orphan sweep is checked against lease stealing.

**Inputs and dependencies:** T05 consumes the T01 `tool_invocations` schema, T02's `settings`, connection pool, and SQL catalog, T03's `LeaseOutcome`, `LeaseAction`, `LeaseStatus`, and `ToolInvocation` models, and T04's `arguments_hash`, `IdempotencyConflictError`, and `LeaseInFlightError`. It consumes D-12's ordering, under which the conditional `ApprovalRequired` raise is step 0 of the tool body, ahead of hashing and ahead of `acquire`, because a deferring pass that acquired a lease it never completed would leave the approved continuation failing against its own lease with `LEASE_IN_FLIGHT`. It consumes D-15's technique for exercising expiry through an already expired row rather than an injected clock. Runtime dependencies are Python 3.12, Pydantic 2.13.4, and psycopg 3.3.4. The three tests require a live PostgreSQL 16, because leases are database rows and every guard lives inside an UPDATE.

**Outputs and consumers:** T05 exports `acquire`, `complete`, and `fail`, which T10 calls at steps 3 and 4 of the identical five-step tool body, plus two SQL constants. Four decisions are appended. D-18 fixes `complete`'s required keyword-only connection and records why `acquire` and `fail` keep their printed signatures. D-19 fixes the two guarded statements, the expanded file list, and the limits of what the three tests can prove about a concurrency control. D-20 records the fresh T05-only test-authorship exception. D-21 revises D-16's seven-step chain to five, using T04's observed review yield. D-22 was added after the blind execution review and resolves an internal contradiction in section 7: its prose forbids taking a lease without a guard in the UPDATE, while its poll block prints `becomes "failed" -> EXECUTE`, which is an unguarded grant. The prose governs, and the poll hands control back to the guarded reacquire.

**Verification:** Test-first. The three tests were written against section 7's text with no kernel file in the tree, and `pytest tests/test_invariants.py -v` reported `collected 0 items / 1 error` with `ImportError: cannot import name 'idempotency' from 'app' (unknown location)`. After implementation the same command reported `9 passed`, which is section 12's definition of done for T05. The T05 gate's guard check was then run locally against live PostgreSQL and printed `PASS both section 7 guards refuse the row they must and take the row they must`. Finally, because one model authored both the kernel and the tests that judge it, twelve single-line mutations were applied one at a time, each run and reverted: `complete` committing its own transaction, REPLAY returning a null result, `acquire` always reporting it won the insert, the hash mismatch check disabled, the hash check moved behind the completed branch, the expiry comparison inverted, the steal never attempted, poll exhaustion returning EXECUTE instead of raising, the steal guard removed from its UPDATE, the reacquire guard removed from its UPDATE, the steal no longer incrementing `attempt`, and the reacquire no longer clearing `error`. All twelve were detected. `idempotency.py` was restored to sha256 `31b99387abb6b8f02842b7273a1c57197743138c85e093801ac7e55f1023dbd6` and `sql.py` to `a08c400cbb00e6c1caf951264107d9bbd499f0d6b2d0469e320e294897019d8f`, both matching their pre-mutation baselines, and the suite and the gate were green afterwards. Files were read and written as bytes throughout, because `Path.write_text` translates newlines on Windows and a digest check would then correctly fail on an unmutated file. Cumulative regressions: `pytest tests/test_models.py` reported `27 passed`.

The whole mutation table was then reproduced independently inside a disposable Vercel Sandbox microVM running PostgreSQL 16.14, against the VM's own clone of pushed commit `406e735` rather than any local file. It observed the same two baseline digests, `9 passed` before and after, all twelve mutations killed, both files restored by digest, and the gate green. This converts the mutation evidence from self-reported, which is what it was at T04, to independently reproducible: the transcript names the SHA, and anyone with the public repository can rerun it. A thirteenth mutation was added after the review, replacing `_poll`'s `return None` on a failed row with a direct `return LeaseOutcome(action=LeaseAction.EXECUTE)`, the literal reading of section 7's poll block. The D-22 gate reported `attempt is 1, expected 2` and `status is failed, expected pending`, and `idempotency.py` was restored to its baseline digest. All thirteen are detected.

**Limitations and review status:** Two coverage limitations are structural rather than oversights. Section 11 names three tests for T05 and none constructs a failed lease, so the reacquire branch and `fail` are exercised by the CI gate alone and by no named test. And the two guards are concurrency controls while all three tests are sequential, so no test can construct the race they defend against, namely a caller whose SELECT observed one state while a competitor changed the row before its UPDATE ran. What the gate proves instead is that each predicate sits inside its UPDATE rather than in a preceding SELECT, by running the statement against a row it must refuse and a row it must take. Both follow T04's precedent of covering in the gate what the named tests cannot construct. Do not read the three tests as proving the guards. A third gate check, added under D-22, covers the poll branch that observes a lease becoming failed, which is likewise unreachable from three sequential tests; it is the only check in the suite that uses a second thread, and it exists because a blind execution review showed that the literal reading of section 7's poll block lets two concurrent callers both execute one tool call. The reacquire preserves `completed_at` and `result` from the previous attempt, because section 7's SET list does not clear them; that is transcription rather than a judgment that stale values are wanted, and `complete` overwrites both on the next success. The steal guard is `lease_expires_at < now()` alone, exactly as section 7 prints it, so a completed row whose expiry had passed would satisfy it; that path is unreachable while `LEASE_TTL_SECONDS` exceeds `TOOL_TIMEOUT_SECONDS`, which section 7 requires and the defaults of 120 against 20 satisfy with margin, and adding a status predicate was rejected as an unrequested improvement to a transcription-only file. `RESOLVE_ATTEMPTS` bounds the re-SELECT loop that section 7 describes without bounding it, and exhausting it raises `LEASE_IN_FLIGHT`. T05 adds no domain mutation, no undo, no routes, and no tools, so the transaction in the tests' `_commit_work` helper is a stand-in for T06's `domain.py`. `backend/app/__init__.py` remains absent as Q-04 records. The T05 questions that D-18 and D-19 answer are not recorded in `docs/OPEN_QUESTIONS.md` as Q-01 through Q-03 were, because that file is outside T05's authorized list. Claude Opus 5 implemented T05 under the user's explicit authorizations of 2026-08-12 covering the `complete` signature, the `sql.py` scope expansion, and the test-authorship exception. Claude Sonnet performed the required neutral, blind, read-only review against a throwaway clone, with an execution pass in a disposable microVM, receiving no question list and no area guidance. It returned one finding, and the finding was accepted: commit `406e735` resolved section 7's poll-block contradiction correctly but recorded the resolution only in an inline comment, in the same commit that documented four other corrections as decisions. The reviewer demonstrated the stakes rather than asserting them, building the race in the microVM and observing one EXECUTE against the shipped code and two against a build patched to the literal spec text. D-22 and a gate check are the disposition, and the gate is itself proven by the thirteenth mutation. The review also confirmed the digests, the nine passing tests, both guards, and D-18's transaction boundary independently, and correctly reported that it sampled three of the twelve mutations rather than reproducing all twelve, leaving the other nine unverified by it; those twelve are separately covered by the full microVM reproduction recorded above. This finding is the first time in T04 or T05 that a blind review found something the author had not, and it is direct evidence for D-21's judgment that an execution pass is worth more than the static review it replaced.

## T06: Domain services and events

**Local role:** `backend/app/domain.py` is the only application writer of
`tasks` and `task_events`. It exports `list_tasks`, `create_task`,
`update_task`, `bulk_update_tasks`, `delete_tasks`, `write_events`, and
`read_events`. Every public function takes `conn` as a required keyword-only
argument. No function imports the pool, opens another connection, commits, or
rolls back. Mutators return a `MutationResult` containing typed task rows and
`PendingTaskEvent` values; the caller writes those events with `write_events`
before committing. Single updates use the caller's `expected_version`. Bulk
updates lock complete rows and use each locked current version in the guarded
statement. Missing, foreign, stale, or concurrently removed targets fail closed
with `VERSION_CONFLICT` rather than producing a partial result.

**Whole-system role:** T06 supplies the transaction seam required by BUILD_SPEC
sections 7 and 10. A T10 tool can now execute its domain mutation, append every
audit event, and call `idempotency.complete(..., conn=conn)` inside one
caller-owned transaction. A rollback leaves all three absent, while a commit
lands all three together, which makes a pending lease honest evidence that no
domain work committed. Complete JSON-safe task snapshots give T07 the original
id and fields needed to restore a deletion and the after version needed to
refuse stale undo. The delete path also turns the schema's implicit
`ON DELETE SET NULL` writes into explicit updated events, preserving the claim
that the audit log explains every owned domain mutation.

**Inputs and dependencies:** T06 consumes the T01 task and event schema; T02's
dictionary-row psycopg connections and centralized SQL catalog; T03's task,
event, and tool-argument models; T05's caller-owned completion contract from
D-18; and D-02, D-10, D-17, and D-19. Runtime behavior depends on PostgreSQL 16
for guarded updates, foreign-key actions, and row locks. D-23 adds
`SELECT_TASKS_BY_IDS_FOR_UPDATE` for complete canonical-order target snapshots
and `SELECT_TASKS_BLOCKED_BY_IDS` for complete pre-cascade snapshots. No SQL
string lives in `domain.py`.

**Outputs and consumers:** T10 consumes the five domain operations and
`write_events` inside its identical tool transaction. T07 consumes complete
`created`, `updated`, and `deleted` event snapshots and relies on the cascade
event order: pointer-clearing updates receive lower event ids than deletions,
so reverse-order undo restores deleted blockers before restoring references.
`MutationResult.tasks` contains created or updated post-mutation rows, or the
deleted rows for a deletion; cascade-affected survivors are events but are not
part of the delete tool's result. Reads are bounded through the existing SQL
limits and also join an explicit caller connection for one consistent API.

**Verification:** Test-first evidence is preserved. Before `domain.py` existed,
the complete T06 gate failed with `ImportError: cannot import name 'domain' from
'app' (unknown location)`. The green gate runs against live PostgreSQL and
prints `PASS T06: rollback and commit boundaries on every mutating path,
complete snapshots, guarded update, bulk update, delete, audited delete
cascade, canonical lock order, and event reads`. It explicitly rolls back
create, single update, bulk update, and delete; commits a mutation, its event,
and lease completion together; checks every snapshot key; verifies explicit
null updates, stale-version rejection, duplicate-id behavior, event order, and
the foreign-key cascade; proves both request orders produce the same lock
sequence; and runs a two-thread concurrent smoke case. The pre-change and
post-change cumulative suite reports 36 tests. The required Opus execution pass
also ran all six public functions with pool access replaced by a raising object,
reproduced the old request-order deadlock, observed no deadlock over 200 rounds
with canonical ordering, and reproduced the gate and 36 tests in a clean-room
Ubuntu 26.04 environment with PostgreSQL 16.14 and Python 3.12.13. Sol then
applied three defects one at a time to the reviewed result. Restoring request
order in the locking query failed with `lock order follows the request`;
dropping the cascade events failed with `the delete cascade was not audited`;
and adding an internal commit to `update_task` failed with `update_task
committed internally`. Both production files were restored to their baseline
SHA-256 digests after the mutations.

**Limitations and review status:** `model_fields_set` is the only signal that
distinguishes an omitted nullable field from an explicit null. T10 must pass the
argument object validated from the real payload; rebuilding it from a full
`model_dump()` marks every field as present and can clear `due_date` or
`blocked_by`. T07 cannot pass a complete event snapshot directly to
`UpdateTaskArgs`, whose extra-field rejection requires undo to project only the
mutable fields. A foreign actor's row may point at a deleted task because the
schema has no ownership constraint on `blocked_by`; PostgreSQL clears that
pointer, but T06 deliberately neither audits nor undoes a cross-actor write.
Direct targets and referencing rows are locked in two statements, leaving a
narrow concurrent-delete interleaving. Section 12's required Opus review found
the transaction boundary correct, then found the request-order deadlock, the
unaudited delete cascade, and the one-path-only rollback proof by executing the
code. All three findings are accepted and covered by the expanded gate. At the
user's direction Opus applied the fixes to the uncommitted worktree; D-23 records
that T06-only routing exception. Sol reviewed the code and reproduced the gate
before accepting ownership. No T07 code, endpoint, tool, or invariant-test count
changes in T06.

## Linear integration specification, revision 01

**Local role:** Adds `docs/LINEAR_INTEGRATION.md`, the single authoritative
description of how this build reaches Linear. It carries the six Linear
decisions, D-24 through D-29, the schema delta for `002_linear.sql`, the deltas
for BUILD_SPEC, ARCHITECTURE, and PROJECT_PLAN, and the then-proposed task
sequence T00B, T00L, T07, then T26 through T29. It implements nothing and
changes no behaviour. Revision 02 below supersedes only that sequence.

**Whole-system role:** It settles where an external system attaches without
breaking what the architecture exists to prove. A Linear GraphQL mutation cannot
join the Postgres transaction BUILD_SPEC section 7 calls non-negotiable, so
calling Linear inside a tool body would leave a window where Linear mutated and
the idempotency lease was still pending, and lease stealing would re-execute work
that had already landed externally. That is the 8:00 demo moment asserting
something false. The document fixes Linear as an asynchronously projected surface
written only after the local transaction commits, which keeps the invariant suite
offline, keeps `undo.py` ignorant of Linear, and makes the failure mode when
Linear is unreachable a queue that drains rather than a half-applied change.

**Inputs and dependencies:** BUILD_SPEC sections 4, 5, 6, 7, 8, 10, 11, and 12;
D-02, D-04, D-09, D-10, D-18, D-19, and D-22; the T06 domain layer, whose
`SELECT *` into a model that forbids extra fields decided the schema shape; and
the merged migration `001_init.sql`, which cannot be edited.

**Outputs and consumers:** D-24 through D-29. T00B consumes the six Gate B facts
and the contract fixture requirement. T00L consumes the schema delta, the
`EXTERNAL_DIVERGENCE` code, the `policy.check` divergence step, and the invariant
count reconciliation. Revision 01 proposed that T07 consume the
`EXTERNALLY_MODIFIED` precheck; D-37 deferred it and D-46 assigns the retrofit to
T00L. T26 through T29 consume the client surface, projector coordination,
reconciler safeguards, and the fenced reset. Each documentation delta names its
owning task, so no block is unassigned.

**Verification:** Documentation only, so the evidence is that the design survives
execution rather than that code passes. An Opus review ran the proposed
`002_linear.sql` against live PostgreSQL 16 and reverted it. Six claims were
checked. Adding three columns to `tasks` raised `ValidationError: 3 validation
errors for Task`, which moved integration state into `linear_task_state`. A
`restored` event enqueued `update`, which added `unarchive` to the operation
mapping. `ON DELETE CASCADE` destroyed the `external_id` the archive projection
needs, which made the side table a tombstone with no foreign key. The tombstone
preserved that id past a delete and a restored task rejoined its state by
original id. All nine established gates pass on this pull request unchanged,
since no code is touched.

**Limitations and review status:** Two safeguards, the reconciler skipping
pending projections and excluding archived issues, are design conclusions that
cannot be executed without a live Linear workspace, and their external
assumptions are Gate B facts 4 and 6 rather than proven behaviour. D-28 fixes the
fencing and per-task ordering semantics but deliberately leaves the mechanism to
T27. D-29 defers the invariant count to T00L's reconciliation against D-19 rather
than choosing a number here. The blind Sonnet review that CLAUDE.md requires for
Opus-produced pull requests was waived at the user's direction because this
change is documentation with no code, no CI gate, and no behavioural effect; the
waiver is recorded in the pull request. This entry exists because CLAUDE.md names
`IMPLEMENTATION_NOTES.md` a required companion file, even though this pull
request is not a numbered task.

## T00B: Gate B, the Linear API probe and contract fixture

**Local role:** Establishes, against the live Linear GraphQL API, the six facts
that the Linear-facing tasks are built on, and freezes the subset of Linear's
schema this build depends on so a change to it fails as a named test rather than
as a confusing runtime error. It adds no application code and touches no kernel
file. Four artifacts: `backend/scripts/linear_probe.py`, which confirms the six
facts and writes only to the demo team; `backend/tests/fixtures/linear_contract.json`,
the frozen subset; `backend/tests/test_contract.py`, a marked drift test that
introspects the live endpoint and compares; and `backend/tests/fakes.py`, the
offline `FakeTracker` the later Linear tasks test against.

**Whole-system role:** The demo runs on Linear, but the write path does not, and
this task is what makes that claim safe to build on. BUILD_SPEC section 7 puts
the domain mutation, its `task_events` rows, and `idempotency.complete` in one
Postgres transaction, and a Linear GraphQL call cannot join it. Calling Linear
from inside a tool body would leave a window where Linear had mutated and the
lease was still `pending`, and lease stealing would then re-execute work that had
already landed externally, falsifying the exactly-once claim in exactly the
scenario the 8:00 demo moment dramatizes. So Postgres stays authoritative and
Linear is an asynchronously projected surface, per D-24, and nothing here moves
where the write path terminates.

Four downstream tasks consume these facts. **T00L** takes the workspace object
shapes and the divergence semantics into `migrations/002_linear.sql`, the
`EXTERNAL_DIVERGENCE` code, the `policy.check` divergence step, and the
`EXTERNALLY_MODIFIED` retrofit to merged `undo.py`. **T26** builds
tool argument enums at startup from fact 2's enumeration and resolves name to id
using fact 2's uniqueness result. **T27** delivers mutations using fact 3's input
shapes and must handle fact 6's finding that a replayed create conflicts rather
than replaying. **T28** polls with fact 4's filter, cursor, and archived
exclusion. If Gate B had failed, none of those would have been written.

**Inputs and dependencies:** `docs/LINEAR_INTEGRATION.md` at revision 01, which
landed separately as PR #10 and is the authoritative specification for this task
and the four that follow. D-24 through D-29 are the Linear decisions; D-02, D-09,
D-12, D-14, D-19, and D-22 constrain what this task may do. A personal Linear API
key and a demo team separate from the one holding the TAD tickets, read from the
environment as `LINEAR_API_KEY` and `LINEAR_TEAM_KEY`; `backend/app/config.py` is
deliberately out of scope, and wiring Linear settings there belongs to T26. The
test marker taxonomy and the lint contract from D-32 and D-34, which landed on
the clarification branch because resolving them required files outside this
task's list.

**Outputs and consumers:** The six-fact table and the `GATE B: PASS` block in
`docs/DECISIONS.md`, which are the durable record and the reason rerunning the
probe is unnecessary. The contract fixture, consumed by `test_contract.py` now
and by T26 and T27 as the reference for what Linear's shapes are. `FakeTracker`,
consumed by T26 through T29 so their tests run offline. Two findings that change
downstream work: a replayed `issueCreate` under the same client-supplied id is
rejected as a uniqueness conflict rather than returning the original issue, so
T27 must read that error as evidence the create already landed instead of
recording a delivered create as failed; and archiving does not move `updatedAt`,
so T28 cannot detect an external archival from the fact 4 query alone.

**Verification:** The live half is `python -B backend/scripts/linear_probe.py`
against team `TRE`, which prints `PASS` for each of the six facts and
`ALL 6 LINEAR API FACTS CONFIRMED`, exit 0. It writes only to the demo team,
archives every issue it creates in a `finally`, and never calls `issueDelete`.
The offline half is the `T00B Linear contract` CI gate, which proves three
things: the fixture parses and holds the depended-on subset with `issueDelete`
absent; `pytest -m "not network"` does not collect `test_contract.py` while
`pytest -m contract` does, asserting the collected count as well as the absence
so an empty suite cannot pass by accident; and `FakeTracker` satisfies its
surface, including the three counterintuitive behaviours it exists to encode.
The gate declares `needs: lint` rather than repeating `ruff check`, so it cannot
report success unless the lint contract passed. Locally,
`pytest -m contract` passes 13 tests against the live API with 36 deselected, and
`cd backend && ruff check .` exits 0.

**Limitations and review status:** The live half is not automated in CI, because
it needs a secret and a network and adding a Linear API key to GitHub Actions is
out of scope; the recorded facts are therefore point-in-time against an
unversioned endpoint, which is precisely why the drift test exists. The archived
recovery boundary in fact 3 is unestablished rather than assumed: the probe never
approached one, and D-25 forbids inventing a fallback until the behaviour is
confirmed. The demo team had zero projects when the gate first ran, which is not
a Gate B failure since enumeration succeeded, but would have left T26's `project`
enum with no members; one project was created under explicit user authorization
and the probe now fails fact 2 with a named message if none exists. `FakeTracker`
is a test double, not a provider abstraction: there is one external system and it
is Linear, `linear.py` is a client rather than an interface with one
implementation, and nothing here is designed so that Jira could be dropped in.
The probe creates one throwaway issue per state-sensitive fact rather than the
single issue originally authorized, because sharing one would couple the checks;
that is recorded as D-33. The blind Sonnet review that CLAUDE.md requires for
Opus-produced pull requests is outstanding and is the next step.

## T07: Kernel undo

**Local role:** `backend/app/undo.py` compensates one agent run's `task_events`
in reverse, entirely or not at all. `undo_run(run_id, actor_id)` loads every
event for the run in descending id order, runs a read-only precheck over all of
them, applies the compensations in one transaction, and returns an `UndoResult`.
A refusal is a return value rather than an exception, because it is an answer the
user sees. Undoing a `created` event deletes the task, a `deleted` event
re-inserts it under its original id, and an `updated` or `restored` event
restores the six mutable fields from the before snapshot. History is
append-only: every compensation is a new forward mutation evented as `restored`
under the original `run_id`, and no `task_events` row is ever deleted or
rewritten. `backend/app/sql.py` gains `DELETE_TASK_GUARDED`,
`INSERT_TASK_RESTORED`, and `SELECT_ALL_EVENTS_FOR_RUN`. `backend/app/domain.py`
gains `delete_task_guarded` and `restore_task`, so the domain layer remains the
only writer of both tables. `backend/tests/test_invariants.py` grows from nine
tests to ten, with the tenth carrying seven scenarios.
`.github/workflows/ci.yml` adds the unconditional `T07 kernel undo` gate while
preserving all nine earlier jobs.

**Whole-system role:** Undo is the third of the four properties the trust
boundary is built to demonstrate, after authorization and idempotency, and it is
the one an interviewer can break by hand: edit a task in another tab, then press
undo. Its value is not that it reverses work but that it refuses to when the
world moved underneath it, and refuses completely rather than partially. That
depends on every earlier task holding: T06's complete JSON snapshots supply the
original id, the restorable fields, and the versions the precheck compares;
T06's delete-cascade ordering under D-23 is what lets a deleted blocker be
restored before any pointer to it; T04's actor scope is inherited through the
owner-scoped statements, so a run belonging to another actor reaches no rows.
T18 exposes the button and owns the eligibility gate that keeps undo a single
application. T20's Run Inspector reads the same event rows. The demo script at
3:00 is "delete everything except interview work, approve, then undo", and this
is the second half of that beat.

**Inputs and dependencies:** T07 consumes the T01 `tasks` and `task_events`
schema including the `ON DELETE SET NULL` self reference, T02's dictionary-row
pool and SQL catalog, T03's `UndoResult`, `UndoReason`, `TaskEvent`, `Task`, and
`UpdateTaskArgs` models, T04's `VersionConflictError`, and all seven T06 domain
functions plus the two added here. It consumes T06's `model_fields_set`
contract, which is why `_restore_arguments` passes all six restorable fields
explicitly including the ones whose value is null. Five decisions were taken
before any code was written: D-37 sequencing, D-38 undo semantics, D-39 the
persistence and read expansion, D-40 the test-authorship exception, D-41 the
orchestration and cascade-event boundary. Runtime dependencies are Python 3.12,
Pydantic 2.13.4, and psycopg 3.3.4. The tests and the gate require live
PostgreSQL 16, because every guard this module relies on is evaluated inside a
SQL statement.

**Outputs and consumers:** T07 exports `undo_run`, which T18 calls behind
`POST /api/runs/{id}/undo`, plus three SQL constants and two domain entry
points. `RunDetail.can_undo` in T08 and the T18 exposure own the eligibility
gate that D-38 requires: compensation events keep the original `run_id`, so a
run that already carries them is no longer eligible, and repeated invocation is
not redo. The compensation events this module writes are ordinary `task_events`
rows and are read by T20 like any others.

**Verification:** Test-first. `test_stale_undo_refused` was written against
section 8's text with no kernel file in the tree, and
`pytest tests/test_invariants.py -q` reported `ImportError: cannot import name
'undo' from 'app' (unknown location)`. After implementation the same command
reported `10 passed`, which is section 12's definition of done for T07. The
cumulative default suite reports `37 passed, 13 deselected` and
`cd backend && ruff check .` reports `All checks passed!`. The T07 gate was run
locally against live PostgreSQL and printed `PASS T07: guards on the
compensating writes, unbounded event read, delete cascade round trip, one direct
compensation plus N audited cascade events, no partial undo on a later conflict,
and no half-applied undo under a concurrent writer`, with its race section
reporting `10 applied in full, 10 refused with zero writes`.

Because one model authored both the kernel and the tests that judge it under
D-40, fourteen single-line mutations were applied one at a time, each run and
reverted: the precheck comparing against the database instead of the projection,
the apply pass guarding on the historical version, a restore reusing the deleted
version instead of continuing past it, the absence check removed, the existence
check removed, the version comparison removed, a refusal reporting applied rows,
the refusal path committing, every event relabelled, no event relabelled, the
guard removed from `DELETE_TASK_GUARDED`, a `LIMIT` added to
`SELECT_ALL_EVENTS_FOR_RUN`, the restore generating a new id, and the guarded
delete dropping its cascade audit. Twelve were detected, nine by the named test
and four by the gate. `undo.py` was restored to sha256
`3cba4e7900dce660a5e1b20d9f679eb9f8e59a1f18c844599e9e137c79590153`, `sql.py` to
`633984231c570363cbc6d4f09f654676e17c280b1a766f93773c083837a3af72`, and
`domain.py` to `762ef0ea528cb0aeb4a754f793946f70b81b0456120fbcb834d2098a42413e03`,
all matching their pre-mutation baselines. Files were read and written as bytes
throughout, because `sql.py` and `domain.py` are CRLF in the worktree while
`undo.py` is LF, and a pattern encoded with the wrong ending silently matches
nothing.

**Limitations and review status:** Two of the fourteen mutations are
**equivalent under the current test surface and are reported as such rather than
counted as kills.** Removing the precheck's `ROW_RECREATED` branch is masked by
the primary key on `INSERT_TASK_RESTORED`, which produces an identical
observable refusal; that is the redundancy D-38 intends, and the consequence is
that the precheck branch is not independently proven. A discriminator exists,
because the `task_events` sequence advances non-transactionally and would move
only on the backstop path, but reading a sequence means raw SQL in the gate
against the single-catalog rule, and it would assert which of two correct
defenses fired rather than a behaviour. Making the refusal path commit instead
of roll back is equivalent because the precheck writes nothing; the rollback is
lock hygiene. The checkpoint 1 reviewer should look at the first of these
specifically.

The primary key backstop's translation into a `ROW_RECREATED` refusal is
therefore exercised only at the statement level, never end to end, because the
row must reappear between the two passes and `SELECT_TASKS_BY_IDS_FOR_UPDATE`
cannot lock a row that is absent. `_effective_operation`'s resolution of a
`restored` event by snapshot shape is likewise unexercised, because reaching it
requires a second undo of the same run, which D-38 makes ineligible. That
eligibility is enforced by `RunDetail.can_undo` and T18 rather than by this
module, which is an asymmetry with `policy.check`, where the kernel re-verifies
an approval the framework already gated; it is recorded here rather than closed,
because closing it needs a fourth `UndoReason` member and `models.py` is T03's
file. Undo does not resolve the run against `agent_runs`; the wire contract in
T08 owns that, and a foreign run refuses here through the owner-scoped
statements rather than being distinguished. A task restored with a `blocked_by`
pointing at a blocker some other actor deleted fails the foreign key and is
reported as `ROW_DISAPPEARED`, which is the closest of the three reasons section
8 provides. `backend/app/__init__.py` remains absent as Q-04 records.

Claude Opus 5 implemented T07 under the user's authorizations of 2026-08-13
recorded as D-37 through D-41, all granted before any code was written and after
the user pushed back on three of the proposals: row locking was demoted from a
correctness requirement to a strengthening in favour of a guarded delete, the
savepoint was rejected, and the extra semantics were required to become test
scenarios rather than gate-only checks. The blind review that CLAUDE.md requires
for Opus-produced pull requests is batched with T08 at review checkpoint 1 under
D-35, with the read, execute, and reconcile phases and a Vercel Sandbox
execution pass against the pinned commit SHA. D-37 adds one item to that
review's scope: whether the T00L divergence precheck can be retrofitted into
this precheck pass, and at what cost.

## T08: Runs and wire contract

**Local role:** `runs.py` owns the run record lifecycle and is the only place in
the codebase that resolves a run identifier to a run, reads or writes
`agent_runs.message_history`, or reads an approval row. `main.py` is the FastAPI
application and enforces the four wire-contract rules from BUILD_SPEC section 9
before any handler body executes, and maps every `PolicyError` to the HTTP
status its class fixes in `errors.py`.

**Whole-system role:** This is the task where the trust boundary becomes
reachable from outside the process. Everything T04 through T07 proved was true
of functions called directly; T08 is what makes those guarantees true of an HTTP
request from a browser. Three properties matter beyond this task. A client
supplied run id becomes a lookup key rather than a grant, because every route
that accepts one passes it through `runs.load`, which resolves it against
`agent_runs` and refuses identically whether the run is missing or belongs to
another actor. Message history becomes server owned, because `runs.load_history`
is the single function that produces one and no request model carries a message
list, which is the property `test_agui_forged_history_ignored` will regression
test at T12A. And `RunDetail.can_undo` becomes the enforcement point for D-38's
single application rule, which nothing else in the system enforces: `undo.py`
processes a `restored` event rather than refusing it, so without this predicate a
second undo would run over its own compensation wave.

**Inputs and dependencies:** `models.py` from T03 for every request and response
model, including the `TrellisModel` base whose `extra="forbid"` is what turns an
undeclared key into a parse failure. `errors.py` and `policy.py` from T04 for the
twelve code and status pairs. `idempotency.py` from T05 for the lease semantics
`RunStep` renders. `domain.py` from T06 for `list_tasks` and for the event
records `can_undo` reads. `undo.py` from T07 and D-38 for the eligibility rule.
`sql.py` from T02, which already carried every run statement this task needed
except the invocation read.

**Outputs and consumers:** `runs.create`, `load`, `load_history`, `save_history`,
`set_status`, `record_usage`, `load_approval`, and `detail`. Three endpoints:
`GET /api/tasks`, `POST /api/runs`, `GET /api/runs/{id}`. `sql.py` gains
`SELECT_INVOCATIONS_FOR_RUN`, deliberately unbounded under D-42. T10 consumes
`runs.load_approval` at step 2 of the five step tool body. T12A consumes
`load_history` and `save_history`. T12B consumes `load_approval` and owns
approval creation, decision, and `pending_approval`. T18 consumes the `can_undo`
predicate and must enforce it server side. T20 consumes `RunDetail.steps`.

**Verification:**

```
cd backend && ruff check .                    All checks passed!
cd backend && pytest -m "not network"         39 passed, 13 deselected
```

39 is the 37 that passed on master plus the two invariants this task adds,
`test_extra_body_keys_rejected` and `test_unsafe_prompt_mode_requires_demo_env`,
which brings the count to 12 of 13. The red run was preserved first: with the
tests written and no implementation, collection failed with
`ImportError: cannot import name 'runs' from 'app'`.

The `T08 runs and wire contract` CI gate carries what those two names
structurally cannot reach, on the precedent of T04's error table, T05's lease
guards, and T07's atomicity checks: the resolver returning byte identical
rejections for a missing and a foreign run, `duration_ms` anchored to the granted
attempt, a reacquired pending row measured from its reacquire rather than its
stale `completed_at`, a replay producing no `deduplicated` step, deterministic
step ordering in both the acquisition-order and tied-timestamp cases, the read
staying unbounded, `can_undo` across the full status matrix and after
compensation, and a route surface with no resume endpoint. The tied-timestamp
case is checked twice, once through `runs.detail` and once with the planner
denied the primary key index, for the reason recorded below.

Nine single-line mutations were applied one at a time, each run against both
detectors, reverted, and the file verified restored by digest: `SELECT_RUN`
dropping its `actor_id` predicate, `SELECT_INVOCATIONS_FOR_RUN` dropping the
`tool_call_id` tie-break from its `ORDER BY`, that same read gaining a `LIMIT`,
`_duration_ms` anchoring on `created_at`, `_duration_ms` branching on
`completed_at` rather than on `status`, `_can_undo` no longer excluding a
compensated run, `_can_undo` no longer excluding a run with no events,
`UNDOABLE_STATUSES` admitting a running run, and the validation handler
returning 400 instead of 422. All nine are detected, eight by the gate and the
handler status by both the gate and `test_extra_body_keys_rejected`. `runs.py`
was restored to sha256
`e042cb92dee128b5daf9f7e1bd5aa6528dff2d4e8d0b715427b5a07da8174eda`, `main.py` to
`d6899356869749a28fbccbf53162b675cf7a24a295087a279d211d8a54b6ca0d`, and `sql.py`
to `aea941b1650d2087191e695659942ffd52af51e42f45fde8f55fd2c1416f615c`, all
matching their pre-mutation baselines. Files were read and written as bytes
throughout, because all three are CRLF in the worktree.

This table is a fresh derivation, not a record of the run T08 originally
reported. That run claimed eight mutations with seven killed and one equivalent
but recorded neither the mutations themselves nor any digest, which D-21 step 4
makes mandatory rather than optional, and an unrecorded table cannot be audited
by anyone. Nothing in this table confirms or contradicts the earlier count; it
replaces it.

The tie-break mutation is why the gate changed. It survived the gate as
originally shipped, and the cause is that `tool_invocations` is keyed
`(run_id, tool_call_id)`, so the planner feeds the sort from that index and the
rows arrive already ordered by `tool_call_id`; the result is identical whether or
not the statement asks for the second key. That is the same masking that makes
T07's `ROW_RECREATED` branch equivalent under the primary key backstop. The
statement itself was already correct, because PostgreSQL guarantees nothing about
ties without the second key, so the repair belongs in the gate and not in
`sql.py`. The tied-timestamp check now also reads with `enable_indexscan` and
`enable_bitmapscan` off, where a seq scan supplies heap order instead, and it
asserts first that `EXPLAIN` reports no index scan so the probe cannot pass
vacuously. Under that plan the mutation is detected.

**Limitations and review status:** Four contracts are deferred with deadlines,
all recorded in D-45. There is no legal error code for a valid actor-owned run
whose status forbids the requested action, which must be resolved before T12B
and T18; T08's endpoint allocation avoids needing it. `RunDetail.pending_approval`
is null and T12B owns both its population and the unspecified question of what
it means when one turn produces several approval-required calls. A successful
replay has no durable representation, so `RunStep.status = deduplicated` is
structurally unreachable and ARCHITECTURE's 8:00 demo beat is not executable as
specified until T20's prerequisite is settled. Concurrent undo eligibility is
T18's.

`duration_ms` assumes `LEASE_TTL_SECONDS` has not changed since an attempt
acquired its lease, and clamps at zero rather than reporting a negative
reconstruction. `detail` reads all events for a run to compute `can_undo`, using
the same statement `undo.py` uses so display and enforcement cannot drift; on a
polled endpoint that is more work than an aggregate would be, and it is a
deliberate trade for agreement over efficiency at demo scale.

Reviewed at checkpoint 1 together with T07, per D-35. Routing exception D-42
granted for test authorship, scoped to T08 alone and not precedent for T09.

Checkpoint 1 ran against pinned `e9048a2`: one Claude Sonnet agent, blind and
read-only, reading both diffs before executing and executing in a Vercel Sandbox
on a fresh clone pinned to that SHA. It returned no findings. It reproduced ruff,
`39 passed, 13 deselected`, the twelve invariants, and both the T07 and T08 gate
scripts against live PostgreSQL, and added one probe of its own, a three touch
same task undo that neither the named test nor either gate constructs, which
passed with state exactly restored. It stated that it could not verify these jobs
on GitHub's own runners, `npm run build`, or the branch protection configuration.
It did not examine the mutation claim, which is what left the gap above
unreported: an unrecorded mutation table is invisible to every passing gate, the
same class of defect the T00B review found by reading rather than by running.

## Documentation schedule reconciliation, revision 02

**Local role:** Reconciles the governing documents with BUILD_SPEC section 12.
T00B remains complete after T06. T00L and T26 through T29 become an optional
post-T25 sequence, and R2 becomes an explicit same-SHA review and execution gate
between T12B and T13.

**Whole-system role:** The schedule now protects the core interview artifact
from optional integration work and makes the approval boundary executable in a
fresh sandbox before UI work depends on it. The R2 rule prevents a static review,
host result, or result from a different commit from being treated as proof of the
T12A/T12B trust boundary.

**Inputs and dependencies:** BUILD_SPEC section 1A, section 11's marker and lint
contracts, section 12's task order, D-35 through D-37, the completed T00B GATE B
PASS, and the detailed Linear contracts in `docs/LINEAR_INTEGRATION.md`.

**Outputs and consumers:** D-46 records the final Linear order and assigns the
merged `undo.py` retrofit to T00L. D-47 records the R2 same-SHA gate.
`CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/PROJECT_PLAN.md`,
`docs/LINEAR_INTEGRATION.md`, and Q-08 now direct later tasks to the same order
and stopping conditions.

**Verification:** Documentation-only checks compare every authoritative T00L,
T26 through T29, and R2 reference against BUILD_SPEC; verify the exact lint,
test, and build commands; check task order and table structure; scan changed
lines for em dashes; and run `git diff --check`.

**Limitations and review status:** No application code, CI workflow, dependency,
database, or external system changes here. The 1.50d Linear estimate and D-36's
funding ledger remain unchanged. R2 has not run; this change defines the gate
that must run after T12B.
