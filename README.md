# Trellis AI Chatbot Task Manager

An agent that manages a todo list, built as a technical interview artifact.

The todo list is the domain. The subject is agent infrastructure: an LLM operating a deterministic application through typed tools, behind a server-owned trust boundary, with approvals, idempotency, an audit trail, and a run inspector that makes every action legible.

**Thesis: the model is measured; the boundary is proven.**

## Documents

| Document | What it is | Read it when |
|---|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The frozen architecture, trust boundary, data model, cut order, and demo script | You want to know what this is and why |
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Scope statement, WBS, delivery spine, risk log, quality plan, control loop, closure | You want to know how it gets built in seven days |
| [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) | Implementation-grade spec: schema, kernel pseudocode, wire contract, 27 tasks with verifications | You are writing the code |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisions made and their rationale, plus API facts confirmed at T00 | You are about to reopen a settled question |

## Status

Architecture frozen. Implementation starts at T00.

## Working rules

- The architecture does not change unless implementation proves an assumption wrong. A better idea is not a reason.
- Anything proposed after Day 2 is filed as STRETCH and stays there.
- Exactly two models touch this repository: Claude Opus 5 and Sol 5.6. Task-level routing is in BUILD\_SPEC section 1A.
- Four KERNEL files (`policy.py`, `idempotency.py`, `undo.py`, and the wire contract in `main.py`) are Opus only. A plausible-but-wrong line in those survives every smoke test and fails in front of a reviewer.
- Every task ends with a runnable verification. Tasks are not batched.

## Stack

Next.js and TypeScript with assistant-ui over the AG-UI protocol, FastAPI, Pydantic AI, PostgreSQL. Everything open source or on a usable free tier.

## Deliberately not built

No durable execution engine, no auth beyond a hardcoded actor, no multi-tenancy, no deployment, no vector store or retrieval, no cross-session memory, no multi-agent orchestration, no billing, no mobile, no self-hosted observability stack, no runtime model failover.

Reasons for each are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The list is part of the deliverable.
