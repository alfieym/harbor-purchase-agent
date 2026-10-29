# Chat UI (Phase 2)

Phase 1 has no chat UI. This directory is reserved for:

- A message list and input box.
- A streaming client reading `POST /api/chat/stream` with `fetch` +
  `ReadableStream` (not `EventSource`, because the request is a POST).
- Rendering for `text_delta`, `tool_call_start`, `tool_result`, `reply_end`
  and `error` events defined in `docs/api-contract.md`.
