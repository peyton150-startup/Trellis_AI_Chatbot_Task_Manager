# T00A Disposable AG-UI Spike

This directory proves Gate A only: assistant-ui can send a message to FastAPI, Pydantic AI can stream AG-UI events back, a gated tool call can become a renderable interrupt, and approve or deny can continue the run with the correct tool-body behavior.

Delete this directory before T12A. It is evidence, not a product foundation.

## What each part adds

- `backend/app.py`: deterministic streaming agent, approval-gated delete tool, `POST /ag-ui`, local CORS, health and reset endpoints, exact request capture, and an execution counter.
- `backend/requirements.txt`: exact Python packages used for the proof.
- `backend/verify.py`: protocol-level streaming, interrupt, approval, denial, identity, and execution assertions used by CI.
- `frontend/components/runtime-provider.tsx`: assistant-ui runtime connected to FastAPI through AG-UI `HttpAgent`.
- `frontend/components/chat-thread.tsx`: assistant-ui conversation, composer, streamed text, and tool-call rendering.
- `frontend/components/approval-card.tsx`: reactive AG-UI interrupt display with approve and deny continuation payloads.
- `frontend/components/protocol-workbench.tsx`: four-stage message, stream, interrupt, and resume evidence plus server execution and request counts.
- `frontend/app/globals.css`: responsive protocol-workbench presentation with reduced-motion handling.
- `frontend/package-lock.json`: exact Node dependency graph used by the Node 20 build.

## Run it

Use Python 3.12 and Node 20.

```powershell
python -m venv .venv-spike
.\.venv-spike\Scripts\python.exe -m pip install -r spike\backend\requirements.txt
Set-Location spike\backend
..\..\.venv-spike\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
Set-Location spike\frontend
npm ci
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Open `http://127.0.0.1:3000`.

## Manual proof

1. Send `Show me the streaming transport proof`. The response must stream and the Message and Stream stages must pass.
2. Send `Delete the demo task`. The approval card must show interrupt `int-delete-spike-item-42` and tool call `delete-spike-item-42`. Tool-body executions must remain 0.
3. Approve. The response must say the tool ran, Resume must pass, and tool-body executions must become 1.
4. POST `/reset`, send the delete prompt again, and deny. The response must say deletion was denied and tool-body executions must remain 0.

The exact initial request, interrupt event, continuation request, and observed automated proof are recorded in `docs/DECISIONS.md`.

## Known dependency warning

`@assistant-ui/react-ag-ui` currently brings in `nanoid` 6.0.1, whose package metadata declares Node 22 or newer. npm therefore prints an engine warning on Node 20. The exact locked graph installs, builds, and completes the browser proof on Node 20.20.2. This disposable spike records the warning and does not add an unverified override.
