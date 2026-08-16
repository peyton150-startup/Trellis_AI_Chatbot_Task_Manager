# T15 hosted transport diagnosis handoff

**Status:** Diagnosis-only handoff captured on 2026-08-16. This document is not
an architecture decision, does not supersede D-61, and assigns no implementation
files to T15. Revalidate all hosted URLs and runtime observations before relying
on them.

We have reached the T15 ugly-demo gate, but the first real hosted Vercel smoke
has exposed a transport or deployment failure. Diagnose it against the
repository's existing contracts before changing code.

## First read, in this order

1. `CLAUDE.md`
2. `docs/BUILD_SPEC.md`
   - review-budget and T15 section
   - T13, T14, T14N, and T15 task rows
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md`
   - especially D-61, D-62, and D-63
5. `IMPLEMENTATION_NOTES.md` entries for T13, T14, and T14N
6. PR #37, T14 chat
7. PR #38, T14N NVIDIA runtime
8. PR #39
9. Current implementation:
   - `frontend/next.config.ts`
   - `frontend/components/Chat.tsx`
   - `frontend/lib/api.ts`
   - `frontend/app/page.tsx`
   - any existing `frontend/app/api` routes
   - backend `POST /api/agui` wiring only as read-only context

Do not assume the proposed fix is a Next.js route handler. D-61 is a closed
architecture decision and explicitly chose relative `/api/*` browser requests
forwarded by a Next.js rewrite using server-only `TRELLIS_API_ORIGIN`, with no
Next.js route handler.

T15 itself owns no files. If implementation work is required to unblock T15,
treat it as a bounded pre-T15 corrective task and explicitly re-plan it before
authoring code. Do not silently mutate T14 or T15 scope.

## Current deployed evidence

Production frontend:
`https://trellis-ai-chatbot-task-manager.vercel.app`

Backend ingress observed during this handoff:
`https://drum-raving-ferment.ngrok-free.dev`

Observed:

1. Direct backend read succeeds:

   ```text
   GET https://drum-raving-ferment.ngrok-free.dev/api/tasks
   -> HTTP 200 application/json
   -> 11 seeded tasks
   ```

2. Vercel same-origin read can succeed through D-61:

   ```text
   GET https://trellis-ai-chatbot-task-manager.vercel.app/api/tasks
   -> HTTP 200 application/json
   -> 11 seeded tasks
   ```

   Therefore the basic relative `/api/tasks` contract, FastAPI, PostgreSQL, and
   configured `TRELLIS_API_ORIGIN` were demonstrated working at least once.

3. During a real browser chat attempt, the deployed application reported:

   ```text
   /api/agui  -> HTTP 502
   /api/tasks -> HTTP 502
   ```

   Vercel surfaced `DNS_HOSTNAME_NOT_FOUND`.

4. Immediately after that real browser attempt:

   ```text
   journalctl -u trellis-backend.service --since "1 minute ago"
   -- No entries --
   ```

   Neither the chat request nor failed board refetch reached Uvicorn during that
   failure window.

5. A later PowerShell `GET` to the exact deployed Vercel `/api/tasks` endpoint
   returned HTTP 200 again.

   The evidence does not yet prove a FastAPI failure, AG-UI request-body failure,
   NVIDIA or GLM failure, PostgreSQL failure, permanently wrong
   `TRELLIS_API_ORIGIN`, or SSE/rewrite limitation. It currently suggests an
   intermittent hosted upstream-resolution or Vercel-to-backend transport
   failure, but diagnosis must prove the cause rather than infer it.

6. An earlier manual `POST /api/agui` returned FastAPI 500 because curl supplied
   an empty body. The traceback was Pydantic validation of `b''`. That was a
   deliberately malformed diagnostic request and is not evidence about the real
   assistant-ui request.

7. Runtime provider configuration was separately verified on Ubuntu:
   `MODEL_ID=z-ai/glm-5.2` and `NVIDIA_API_KEY` are loaded by the running backend.
   T14N preserves the injected `FunctionModel` deterministic seam. Do not alter
   provider or model code while investigating this transport failure.

## T14I diagnosis and disposition

Diagnosis executed on 2026-08-16 against production deployment
`dpl_CVTrda43LoK2Nmw2uFVb5t8XMAid`, which served merged `master` SHA
`fc7c21dcb800a55f366d07c015d1a93da410c5c4`. Vercel listed
`TRELLIS_API_ORIGIN` as a Sensitive variable scoped to Production and Preview;
its value is intentionally non-readable after creation.

Ten alternating direct-ngrok reads and ten Vercel same-origin reads all reached
ngrok. Without a bypass header, the response was HTTP 200 `text/plain` with
ngrok's 233-byte free-plan browser interstitial, not task JSON. That body
reproduced the deployed board's `Unexpected token '<'` parse failure. With
`ngrok-skip-browser-warning: 1`, both the direct request and D-61 rewrite
returned HTTP 200 `application/json` with 11 tasks.

A read-only AG-UI POST through the production Vercel origin with the same header
returned HTTP 200 `text/event-stream` after 19.4 seconds. Its 12,060-byte SSE
body contained `RUN_STARTED` and `RUN_FINISHED` and no `RUN_ERROR`. This proves
the unchanged rewrite carries the AG-UI stream once the free-ngrok interstitial
is bypassed. A route handler is not justified.

`DNS_HOSTNAME_NOT_FOUND` was observed once but was not reproduced during this
diagnosis. The event itself is confirmed; whether it represents a persistent
defect, and whether it has any causal relationship to the reproducible ngrok
interstitial failure, remain unconfirmed. Q-22 preserves that distinction and
requires renewed correlation if the DNS error recurs.

The bounded T14I correction keeps D-61 unchanged and adds the explicitly scoped
`NGROK_BYPASS_HEADERS` demo shim to both `fetchTasks` and `HttpAgent`. Its CI
gate is offline and never contacts ngrok. Hosted Preview and T15 verification
remain required before the pull request can be marked ready.

## Repository contracts that must survive

- The browser continues using relative same-origin `/api/*` URLs.
- `TRELLIS_API_ORIGIN` remains server-only.
- The browser never receives or chooses the backend origin.
- PostgreSQL remains authoritative.
- Board state is refetched from committed backend state.
- Chat continues to use the AG-UI transport.
- Do not add CORS as a workaround.
- Do not point `HttpAgent` directly at ngrok.
- Do not touch backend trust-boundary, approval, tool, policy, idempotency, or
  provider code without new evidence requiring it.
- Preserve the one page-owned `BoardState` and `thread.runEnd` refetch contract
  established by T14.
- Preserve D-62's deduplicated `@ag-ui/client` 0.0.57 contract.
- Preserve D-63's NVIDIA-only runtime.
- Do not weaken existing CI.

## Phase 1: diagnosis only

Before editing anything:

1. Confirm the current master SHA and working tree are clean.
2. Inspect the exact current rewrite and `HttpAgent` configuration.
3. Inspect Vercel production deployment configuration and logs if available.
   Verify which deployment serves the production domain and whether
   `TRELLIS_API_ORIGIN` is present in the applicable Production environment. Do
   not print secret values.
4. Reproduce several times, separating these paths:
   - direct ngrok `GET /api/tasks`
   - deployed Vercel `GET /api/tasks`
   - real browser `POST /api/agui`
   - board refetch after the browser run
5. Correlate each hosted failure with:
   - Vercel status, error, and log evidence
   - Ubuntu Uvicorn access logs
   - ngrok tunnel status and logs if available
6. Determine whether `DNS_HOSTNAME_NOT_FOUND` is:
   - intermittent resolution of the ngrok hostname from Vercel
   - stale deployment or environment configuration
   - a Vercel external-rewrite-specific problem
   - a broader Vercel server-side fetch problem
   - something else
7. Do not claim a Next.js route handler fixes this unless evidence shows why it
   changes the failing mechanism. A route handler that calls `fetch()` from
   Vercel may still depend on the same upstream DNS resolution.

Stop after diagnosis and report:

1. exact failure mechanism
2. evidence supporting it
3. whether code changes are required
4. smallest proposed correction
5. exact files that would change
6. whether that correction supersedes or narrows D-61

If the cause cannot be established, record the unresolved contradiction or gap
in `docs/OPEN_QUESTIONS.md` as `CLAUDE.md` requires rather than guessing.

## Phase 2: only if a code change is proven necessary

Do not silently edit T15. T15 owns no files.

Create a bounded pre-T15 corrective task in the plan and record why it exists.
If the fix changes D-61, add a new decision entry that explicitly supersedes or
narrows D-61 rather than rewriting its history.

If evidence proves that `/api/agui` requires a Next.js route handler while
ordinary API reads can remain on the rewrite, the acceptable architecture would
be:

```text
browser
-> same-origin POST /api/agui
-> Next.js server-side proxy
-> TRELLIS_API_ORIGIN/api/agui
-> FastAPI
```

Retain the existing rewrite for the rest of `/api/*` if that is the smallest
correct design. Implement this only if diagnosis proves the rewrite is the
failing mechanism.

If a route handler is required, it must:

- preserve the AG-UI POST body
- preserve the required content type
- stream the upstream response body instead of converting it to JSON or text
- propagate the meaningful upstream status
- prevent browser authority over the backend origin
- keep `TRELLIS_API_ORIGIN` server-only
- introduce no CORS
- introduce no browser persistence or optimistic task mutation
- avoid overlapping or double routing for `/api/agui`
- leave backend wire and trust behavior untouched

Follow `CLAUDE.md` workflow:

- one bounded task and one implementation commit
- draft PR on first push
- task-specific cumulative CI gate
- `.github/workflows/ci.yml` companion update
- `IMPLEMENTATION_NOTES.md` companion entry
- BUILD_SPEC and DECISIONS updates if scope or architecture changes
- no unrelated files
- do not merge the PR; the user owns merge

## Verification bar

A local build alone is insufficient because T14 already proved local transport.
The correction exists specifically to unblock the hosted T15 gate.

Required:

1. `cd frontend && npm run build`
2. `cd backend && ruff check .`
3. `cd backend && pytest -m "not network"`
4. all cumulative GitHub checks green
5. deployed Vercel `GET /api/tasks` returns HTTP 200 JSON
6. real browser `POST /api/agui` reaches Ubuntu, proven by Uvicorn or journal
   evidence
7. AG-UI response visibly streams in the Vercel frontend
8. issue a safe real command: `Create a task called T15 Hosted Smoke`
9. prove the complete sequence:

   ```text
   prompt
   -> NVIDIA GLM agent
   -> typed create_task
   -> deterministic policy
   -> PostgreSQL commit
   -> thread.runEnd
   -> board refetch
   -> T15 Hosted Smoke visibly appears
   ```

10. confirm exactly one mutation committed for the request

That final sequence is the actual T15 ugly-demo bar. Do not proceed to T16
until the hosted prompt-to-committed-board path is green.

## Reference context

- [D-61 and later decisions](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/blob/master/docs/DECISIONS.md)
- [BUILD_SPEC and T15 gate](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/blob/master/docs/BUILD_SPEC.md)
- [Architecture](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/blob/master/docs/ARCHITECTURE.md)
- [PR #37: T14 chat](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/pull/37)
- [PR #38: T14N NVIDIA runtime](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/pull/38)
- [PR #39: README update](https://github.com/peyton150-startup/Trellis_AI_Chatbot_Task_Manager/pull/39)
