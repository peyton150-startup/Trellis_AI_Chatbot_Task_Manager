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
