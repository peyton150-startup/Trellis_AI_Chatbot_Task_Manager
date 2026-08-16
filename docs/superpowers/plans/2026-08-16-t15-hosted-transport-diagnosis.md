# T15 Hosted Transport Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the exact cause of the intermittent hosted Vercel-to-ngrok failure without changing D-61 or application code on inference alone.

**Architecture:** Preserve Browser -> relative `/api/*` -> Next.js rewrite -> server-only `TRELLIS_API_ORIGIN` -> FastAPI throughout diagnosis. Correlate repeated direct and rewritten requests with Vercel deployment metadata and backend reachability, then stop with either a configuration-only correction or a new evidence-backed implementation plan.

**Tech Stack:** Vercel CLI and logs, Next.js 16 rewrites, ngrok HTTPS ingress, FastAPI/Uvicorn, PostgreSQL, AG-UI SSE.

**Spec:** `docs/T15_HOSTED_TRANSPORT_HANDOFF.md`

## Global constraints

- T15 owns no implementation files.
- D-61 remains closed unless evidence disproves its assumptions.
- Keep browser requests relative and `TRELLIS_API_ORIGIN` server-only.
- Do not add CORS, point `HttpAgent` at ngrok, alter SSE framing, or touch backend trust-boundary and provider behavior.
- Do not expose secret values in command output or repository files.
- Normal CI remains deterministic and network-free.

---

### Task 1: Establish repository and deployment identity

**Files:**
- Read: `frontend/next.config.ts`
- Read: `frontend/components/Chat.tsx`
- Read: `.vercel/project.json` if present
- Modify: none

**Interfaces:**
- Consumes: D-61, D-62, D-63, current `origin/master`
- Produces: exact local SHA, Vercel project identity, production deployment identity, and environment-variable presence without values

- [ ] **Step 1: Confirm the branch is based on current `origin/master` and record the clean/dirty state.**
- [ ] **Step 2: Confirm the rewrite still maps `/api/:path*` to `${TRELLIS_API_ORIGIN}/api/:path*`.**
- [ ] **Step 3: Confirm `HttpAgent` still uses relative `/api/agui`.**
- [ ] **Step 4: Inspect the production alias and deployment SHA.**
- [ ] **Step 5: Confirm `TRELLIS_API_ORIGIN` exists in Production without printing its value.**

### Task 2: Reproduce and separate the failure paths

**Files:**
- Modify: none

**Interfaces:**
- Consumes: production frontend URL and current backend ingress URL from the handoff
- Produces: timestamped status, content type, Vercel error code, and reachability samples for direct and rewritten requests

- [ ] **Step 1: Sample direct ngrok `GET /api/tasks` repeatedly.**
- [ ] **Step 2: Sample Vercel same-origin `GET /api/tasks` repeatedly.**
- [ ] **Step 3: Send a real browser read-only chat turn through `POST /api/agui`.**
- [ ] **Step 4: Capture Vercel logs for the same window.**
- [ ] **Step 5: Correlate available Ubuntu/ngrok evidence without assuming access that is not configured.**

### Task 3: Decide the correction boundary

**Files:**
- Modify: `docs/T15_HOSTED_TRANSPORT_HANDOFF.md`
- Conditional create: a new pre-T15 implementation plan only if code is proven necessary

**Interfaces:**
- Consumes: Task 1 deployment identity and Task 2 correlated samples
- Produces: exact failure mechanism, smallest correction, file list, and D-61 impact statement

- [ ] **Step 1: Classify the failure as environment/deployment, rewrite-specific, or unresolved.**
- [ ] **Step 2: If configuration-only, apply only the authorized Vercel or Ubuntu configuration change and redeploy the same application contract.**
- [ ] **Step 3: If code is necessary, stop application edits and write a bounded pre-T15 TDD implementation plan with exact files and tests.**
- [ ] **Step 4: If unresolved, record the evidence gap in `docs/OPEN_QUESTIONS.md` and make no speculative code change.**
- [ ] **Step 5: Update the handoff with dated evidence and the disposition.**

### Task 4: Verify and prepare the pull request

**Files:**
- Modify: only files justified by Task 3
- Verify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the smallest proven correction
- Produces: a green local commit, green cumulative GitHub checks, and hosted T15 evidence if the external transport is stable

- [ ] **Step 1: Run `cd frontend && npm ci --no-audit --no-fund`.**
- [ ] **Step 2: Run `cd frontend && npm run build`.**
- [ ] **Step 3: Run `cd backend && ruff check .`.**
- [ ] **Step 4: Run `cd backend && pytest -m "not network"`.**
- [ ] **Step 5: Amend the task commit, push, and open the draft PR.**
- [ ] **Step 6: Wait for every cumulative check and add the new stable check to branch protection after its first success.**
- [ ] **Step 7: Mark the PR ready only after local, hosted, CI, and branch-protection evidence is green.**
