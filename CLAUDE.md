# Trellis Agent Demo

## Project overview

Trellis is a technical interview artifact: an LLM operates a todo application through six typed tools while deterministic application code owns state, authorization, approvals, idempotency, audit history, and undo. The implementation uses FastAPI, Pydantic AI, PostgreSQL, Next.js, assistant-ui, and AG-UI. The model is measured; the trust boundary is proven.

## Sources of truth

- Read `docs/BUILD_SPEC.md` before implementation. Execute its tasks in order and do not invent missing contracts.
- Read `docs/ARCHITECTURE.md` for the trust boundary, data model, and demo rationale.
- Read `docs/DECISIONS.md` for closed API and architecture decisions.
- Read `docs/LINEAR_INTEGRATION.md` for the Linear projection design, why
  Postgres stays authoritative, and the tasks that implement it.
- Record genuinely missing or contradictory requirements in `docs/OPEN_QUESTIONS.md` and stop.
- Record how major implementations fit locally and system-wide in `IMPLEMENTATION_NOTES.md`.

## Environment and commands

- Required runtimes: Python 3.12, Node 22 or newer, PostgreSQL 16 in Docker. Use a Node release supported by the locked dependency graph; the current graph supports `^22 || ^24 || >=26`.
- T00 isolated setup on PowerShell:

```powershell
$python312 = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python312 --version
& $python312 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

The version command must print Python 3.12.x. Outside Codex, replace `$python312` with the exact path to a Python 3.12 interpreter.

- Verify T00:

```powershell
.\.venv\Scripts\python.exe backend\scripts\api_probe.py
```

- Watch the GitHub CI gate for a PR:

```powershell
gh pr checks <pr-number> --watch
```

The required checks are named `T00 API probe`, `T00A spike build`, `T00R probe hardening`, `T01 database schema`, `T02 database connection`, and `T03 models`. Keep check names stable after branch protection references them.

### Per-task CI gate protocol

- Every task PR must add or update a stable task-specific CI job that directly runs that task's verification. An earlier task's checks are regressions, not a substitute for the new task's gate.
- CI gates are cumulative. Every pull request runs every established task check from T00 through the current task, regardless of which files changed, so later work cannot silently break earlier proofs.
- Do not add path filters, changed-file shortcuts, conditional skips, or workflow splits that prevent an established gate from running on every pull request unless the user explicitly approves the exception.
- Name each new check `T## <short verification name>` and keep that name stable after it is referenced by branch protection.
- Treat `.github/workflows/ci.yml` and `IMPLEMENTATION_NOTES.md` as required companion files for each task, even when the task table lists only implementation files. Do not use this exception for unrelated changes.
- Open the PR as a draft. Run the verification locally, push the branch, and wait until the new task check and all existing required checks pass.
- After the new check has reported successfully at least once, add its exact name to the `master` branch's required status checks. Preserve strict up-to-date checks, admin enforcement, conversation resolution, and all existing required contexts.
- Mark the PR ready only after local verification, all GitHub checks, and branch protection are confirmed. Never merge, squash, rebase, close, or otherwise finalize the PR; only the user may do that.

- Full CI commands, once the application scaffold exists, run in this order:

```text
cd backend && ruff check .
cd backend && pytest -m "not network"
cd frontend && npm run build
```

The working directory is part of the contract. Ruff resolves configuration by directory hierarchy, so `backend/pyproject.toml` governs `backend/` and nothing else, and a bare `ruff check` from the repository root would also lint the disposable `spike/` tree.

- Three test markers, registered in `backend/pyproject.toml`. `eval` and `contract` are taxonomy, describing what a test is. `network` is an execution property and is the only one that decides what CI collects. Both external suites carry one of each: `test_evals.py` is `eval` plus `network`, and `test_contract.py` is `contract` plus `network`. Run them on demand with `pytest -m eval` and `pytest -m contract`. See the test marker contract in `docs/BUILD_SPEC.md` section 11.
- Ruff's rule set is pinned as `select = ["E4", "E7", "E9", "F"]`. Findings outside it are deferred lint-adoption debt, not violations, and "passes ruff's current defaults" is not a second gate.
- Keep GitHub Actions permissions at least privilege and pin third-party actions to immutable commit SHAs.
- Use the task-specific verification command in `docs/BUILD_SPEC.md` before starting the next task.

## Code and workflow conventions

- Never use Claude Flow for this project. Exclude `.claude-flow/`, `.ruflo/`,
  and `.ruvector/` from repository content, task context, verification scope,
  commits, reviews, and pull requests.
- One task, one commit, with message `T##: <task name>`.
- From T07 forward, follow the compressed review schedule in `docs/BUILD_SPEC.md` section 1A. Implementation, commits, and verification stay task-local; only dedicated review is batched. Stop at the review checkpoints after T08 and T12B. R2, the checkpoint after T12B, must pass before T13 starts. After T15 is green, reduce review depth as specified there.
- Open a separate PR after T00 and after T00A. Pause after the T00A PR for user review.
- Never merge, squash and merge, rebase and merge, close, or otherwise finalize a PR. Only the user may merge PRs.
- Write implementation-specific PR descriptions. List files and components added or changed, exact interfaces and data flow, observed verification evidence, limitations, reviewer findings and dispositions, and follow-up ownership. Do not substitute only an architectural summary. Link to durable documentation where it contains the full detail, then summarize the relevant facts in the PR.
- Do not use em dashes in files, comments, or commit messages.
- Do not touch files outside the current task's file list unless the user explicitly expands the task.
- Follow the model-routing table in `docs/BUILD_SPEC.md`. If the user explicitly authorizes a temporary routing exception, mark the PR for review by the required model.
- Review neutrally. Give a blind reviewer only the task specification, commit diff, and verification evidence. Do not imply that defects exist. Require evidence-backed findings and allow an explicit no-findings result.
- Use Terra as the default neutral, blind, read-only reviewer for Codex- or Sol-produced PRs under the user's 2026-08-11 instruction. Give Terra only the task specification, commit diff, and verification evidence. Terra must not edit, generate, stage, or commit repository content. Record its findings and their disposition in the PR.
- Use Claude Sonnet as the default neutral, blind, read-only reviewer for Claude-produced PRs whose implementation model is Opus. Give Sonnet only the task specification, commit diff, and verification evidence. Sonnet must not edit, generate, stage, or commit repository content. Record its findings and their disposition in the PR.
- A Sonnet review is one agent, not two, and it runs in three phases in this order: read, execute, reconcile. Do not dispatch a separate executor and reviewer in parallel, and do not let the agent run the code before it has read the diff.
  1. **Read.** Review the commit diff against the task specification before running anything, while still blind to any result. Produce the findings this pass supports, plus the open hypotheses that only execution can settle.
  2. **Execute.** Reproduce the author's own verification gate, then run independent probes, and design those probes to test the hypotheses phase 1 raised rather than only replaying the author's gate.
  3. **Reconcile.** Revisit the phase 1 findings against what execution showed. Confirm, withdraw, or add, and report which happened to each.
- Read before execute, because a reviewer that sees a green board first anchors on it and stops looking. Findings the gate cannot reach are the ones worth having: the T00B review on 2026-08-13 found that `docs/DECISIONS.md` recorded fact 4 as tested across ten cases including cleared labels and a cleared project, while the shipped probe tested at most eight and cleared nothing. That is invisible to every passing test and was found by reading.
- The execution phase runs in a Vercel Sandbox against a pinned commit SHA, never in a live worktree and never on the host. Name the Vercel Sandbox in the reviewer's prompt every time, including for a small or targeted follow-up pass. "An isolated clone" is not a substitute and does not satisfy this rule: a clone isolates the repository from the agent, while the sandbox isolates the host from whatever the agent runs. If a sandbox genuinely cannot be provisioned, the reviewer reports the failure verbatim, falls back to the pinned clone, and labels every result with the environment that produced it, so a host-run result is never mistaken for a sandboxed one.
- The pinned-clone fallback does not apply to R2. R2 is a same-SHA review and execution gate: the blind review must have no unresolved BLOCK findings; that exact SHA must boot and complete the T12A/T12B verification path in a fresh Vercel Sandbox; `cd backend && ruff check .` and `cd backend && pytest -m "not network"` must pass. At the reviewed SHA `09b75db`, the frontend build gate applied only if a tracked production `package.json` existed; none did, so that gate was N/A. Beginning with T13, `cd frontend && npm run build` is an unconditional cumulative gate. A sandbox provisioning or execution failure is an R2 BLOCK. Any fix that changes the SHA invalidates the entire R2 result and requires the blind review, fresh-sandbox execution, and all applicable deterministic gates to rerun before T13.
- The reviewer records both the SHA reviewed and the SHA it executed against, and states plainly which claims it could not independently verify, such as anything needing a live credential the sandbox is deliberately denied. Executing in a sandbox is not an exception to the read-only rule above: the agent still must not edit, generate, stage, or commit repository content.
- Terra review does not replace Opus when the routing table says `SOL WRITES, OPUS REVIEWS` or otherwise requires Opus review.
- Kernel files are transcription-only. Preserve the specified check order and transaction boundaries.
- Put all SQL statements in `backend/app/sql.py` as uppercase constants.
- Use typed Pydantic request, response, domain, and tool argument models. Reject extra request keys.
- Add a focused entry to `IMPLEMENTATION_NOTES.md` for every major task. State its local role, whole-system role, produced contracts, verification, and known limitations.

### Major implementation documentation standard

Every major task must add or update an `IMPLEMENTATION_NOTES.md` entry before its commit. Use these headings:

```markdown
## T##: Task name

**Local role:** What this component does by itself and what it owns.

**Whole-system role:** Why the complete Trellis architecture needs it, which risks it controls, and which demo behavior it enables.

**Inputs and dependencies:** Earlier contracts, files, services, and assumptions it consumes.

**Outputs and consumers:** Interfaces, state, events, or decisions it produces and the later tasks that rely on them.

**Verification:** Exact commands and observable evidence that prove the task works.

**Limitations and review status:** Known gaps, temporary decisions, routing exceptions, and required follow-up review.
```

Do not mark a major task complete if its implementation note explains only the component in isolation. It must also explain how the component changes or supports the project as a whole.

## Architectural invariants

- PostgreSQL is authoritative. The browser and model never own application state or message history.
- A browser-supplied run id is a lookup key, not authority. Resolve actor ownership and resumable state server-side.
- The approvals table is authoritative. Framework approval is only the UI gate, and the tool body rechecks the stored approval before mutation.
- One `agent_runs.id` is one application run. Approval continuation creates a new model invocation under that same application run.
- Every mutation goes through the policy layer, idempotency lease, domain transaction, and audit event.
- Task titles and notes are untrusted data. They enter prompts only through the delimited renderer in `prompts.py`.
- Missing tasks and another actor's tasks return the same out-of-scope result.
- Undo is all-or-nothing and refuses after concurrent version changes.

## Be careful with

- `policy.py`, `idempotency.py`, `undo.py`, and the wire contract in `main.py` are correctness-kernel files.
- Do not implement remote Linear issue behavior before T26. No workspace discovery, name to id resolution, projector, reconciler, delivery retry, or Linear-aware reset. T00L ships the local boundary only, and no deterministic test may require `LINEAR_API_KEY`, the network, or a live Linear workspace.
- T00W is the one authorized exception under D-69, and it opens a conversation plane rather than a projection plane. `backend/app/linear_agent_api.py` is the sole file in shipped application code permitted to hold a Linear provider endpoint literal, and it may perform only OAuth authorization URL construction, token exchange, refresh, revoke, the read-only `viewer { id }` installation identity query, and AgentActivity GraphQL. Every other module reaches Linear through it and must not open a second HTTP client. `issueCreate`, `issueUpdate`, `issueArchive`, `issueUnarchive`, and `LINEAR_API_KEY` remain prohibited, and no deterministic T00W test may require the network or a live workspace.
- Do not add frameworks, tables, columns, endpoints, status values, or speculative features outside `docs/BUILD_SPEC.md`.
- Linear integration state lives in `linear_task_state` and `linear_projections`, never as columns on `tasks`. `linear_task_state.task_id` has no foreign key to `tasks.id` on purpose, because the row is a tombstone that must outlive its task. Integration bookkeeping never increments a task business version, never produces a business `task_events` row, never enters a `Task` snapshot, and is never restored by undo.
- `sql.TRUNCATE_ALL_STATE` and `sql.TRUNCATE_ALL_TEST_STATE` are deliberately separate and must not be merged. Production `seed.reset` uses the first and stays Linear-unaware; only deterministic test fixtures use the second, which also clears the tombstone table. See D-68.
- Do not reuse disposable `spike/` code in the production integration. Delete it before T12A.
- Do not treat client-supplied AG-UI message history or approval payloads as authoritative.
- For an interface that intentionally polls, browser verification waits for DOM readiness and explicit visible controls or state. Do not wait for `networkidle`, because recurring requests make network idleness impossible by design.
