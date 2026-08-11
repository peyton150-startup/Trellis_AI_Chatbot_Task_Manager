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
