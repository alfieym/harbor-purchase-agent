# Chat feature (Phase 2)

Phase 1 only reserves this slice; there is no router, service or SSE code yet.

Phase 2 will add:

- `POST /api/chat/stream` returning `text/event-stream`.
- `router.py`, `schemas.py`, `service.py`.
- An adapter mapping AgentScope `AgentEvent` objects from `agent.reply_stream(...)`
  into the contract documented in `docs/api-contract.md`.
- Session state (in-memory first, then `AgentState` plus a storage backend).

Because the endpoint is a POST, the frontend will read it with `fetch` +
`ReadableStream`, not `EventSource`.
