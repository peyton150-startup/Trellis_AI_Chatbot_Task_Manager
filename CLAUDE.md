# Trellis Agent Demo

## Project overview

Trellis is a technical interview artifact: an LLM operates a todo application through six typed tools while deterministic application code owns state, authorization, approvals, idempotency, audit history, and undo. The implementation uses FastAPI, Pydantic AI, PostgreSQL, Next.js, assistant-ui, and AG-UI. The model is measured; the trust boundary is proven.

## Sources of truth

- Read `docs/BUILD_SPEC.md` before implementation. Execute its tasks in order and do not invent missing contracts.
- Read `docs/ARCHITECTURE.md` for the trust boundary, data model, and demo rationale.
- Read `docs/DECISIONS.md` for closed API and architecture decisions.
- Record genuinely missing or contradictory requirements in `docs/OPEN_QUESTIONS.md` and stop.
- Record how major implementations fit locally and system-wide in `IMPLEMENTATION_NOTES.md`.

## Environment and commands

- Required runtimes: Python 3.12, Node 22 or newer, PostgreSQL 16 in Docker. Use a Node release supported by the locked dependency graph; the current graph supports `^22 || ^24 || >=26`.
- T00 isolated setup on PowerShell:

```powershell
$python312 = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python312 --version
& $python312 -m venv .venv
.\.venv\Scripts\python.exe -m pip install pydantic-ai==2.27.0 "pydantic-ai-slim[ag-ui]==2.27.0" fastapi==0.141.1 uvicorn==0.52.1 httpx==0.28.1
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

The required checks are named `T00 API probe`, `T00A spike build`, and `T01 database schema`. Keep check names stable after branch protection references them.

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
ruff check
pytest -m "not eval"
npm run build
```

- Run behavioral evals only on demand. They are excluded from CI.
- Keep GitHub Actions permissions at least privilege and pin third-party actions to immutable commit SHAs.
- Use the task-specific verification command in `docs/BUILD_SPEC.md` before starting the next task.

## Code and workflow conventions

- One task, one commit, with message `T##: <task name>`.
- Open a separate PR after T00 and after T00A. Pause after the T00A PR for user review.
- Never merge, squash and merge, rebase and merge, close, or otherwise finalize a PR. Only the user may merge PRs.
- Write implementation-specific PR descriptions. List files and components added or changed, exact interfaces and data flow, observed verification evidence, limitations, reviewer findings and dispositions, and follow-up ownership. Do not substitute only an architectural summary. Link to durable documentation where it contains the full detail, then summarize the relevant facts in the PR.
- Do not use em dashes in files, comments, or commit messages.
- Do not touch files outside the current task's file list unless the user explicitly expands the task.
- Follow the model-routing table in `docs/BUILD_SPEC.md`. If the user explicitly authorizes a temporary routing exception, mark the PR for review by the required model.
- Review neutrally. Give a blind reviewer only the task specification, commit diff, and verification evidence. Do not imply that defects exist. Require evidence-backed findings and allow an explicit no-findings result.
- Use Terra as the default neutral, blind, read-only reviewer for Codex- or Sol-produced PRs under the user's 2026-08-11 instruction. Give Terra only the task specification, commit diff, and verification evidence. Terra must not edit, generate, stage, or commit repository content. Record its findings and their disposition in the PR.
- Use Claude Sonnet as the default neutral, blind, read-only reviewer for Claude-produced PRs whose implementation model is Opus. Give Sonnet only the task specification, commit diff, and verification evidence. Sonnet must not edit, generate, stage, or commit repository content. Record its findings and their disposition in the PR.
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
- Do not add frameworks, tables, columns, endpoints, status values, or speculative features outside `docs/BUILD_SPEC.md`.
- Do not reuse disposable `spike/` code in the production integration. Delete it before T12A.
- Do not treat client-supplied AG-UI message history or approval payloads as authoritative.
- For an interface that intentionally polls, browser verification waits for DOM readiness and explicit visible controls or state. Do not wait for `networkidle`, because recurring requests make network idleness impossible by design.
