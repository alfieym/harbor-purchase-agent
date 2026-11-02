# Commerce intents feature (S5)

Turns one CLI conversation into one HTTP request. `POST /commerce/intents` is a
one-shot JSON endpoint (not streaming): the model runs a ReAct turn against the
shared catalog search and returns product cards.

> Naming note: the module still lives in `features/chat/` (the S5 slice started
> as "chat"); its public contract is the **commerce intents** endpoint.

## Contract

`POST /commerce/intents` (root-level, per the S5 checklist):

```json
{ "buyer_id": "u1", "shopping_session_id": "s1", "raw_query": "2000 以内的吸尘器" }
```

→ `200`:

```json
{ "final_text": "命中 3 条、展示前 3 条", "cards": [ { "...": "ProductCard" } ] }
```

Errors use the unified envelope `{"error": {"code", "message"}}`:

| Condition | Status | code |
|---|---|---|
| Model turn exceeds `turn_timeout_seconds` | 504 | `http_504` |
| `product_search` tool ended in ERROR | 502 | `http_502` |
| Model produced no structured output | 502 | `http_502` |
| Model key missing | 503 | `http_503` |

## Layout

- `schemas.py` — `CommerceIntentRequest` / `CommerceIntentResponse` (`final_text` + `cards`).
- `service.py` — `SessionStore` (keyed by `(buyer_id, shopping_session_id)`, TTL 30 min
  + LRU cap 1000, per-session `asyncio.Lock`) and `run_turn` (resolve session → lock →
  timed `agent.reply` → parse `ProductCardList`).
- `router.py` — `POST /commerce/intents`, maps service errors onto the envelope.

## Session model

- Keyed by `(buyer_id, shopping_session_id)`, so different buyers never share
  context and one buyer can run many shopping sessions in parallel.
- Create-or-resume: a key not seen before starts a fresh session (no 404).
- Same-session requests serialize on a lock (`Agent.reply` is not reentrant);
  cross-session requests run in parallel.

## Concurrency (S5-4 / S5-5)

- The `product_search` tool offloads the synchronous `service.search`
  (Qdrant + embedding HTTP) to a worker thread via `asyncio.to_thread`, so it
  never blocks the event loop — two sessions search in parallel.
- `SessionStore` holds a per-session `asyncio.Lock`, so two requests to the same
  session serialize (the second waits for the first).

## Deferred

- `POST /api/chat/stream` (SSE, `text/event-stream`) per `docs/api-contract.md` is a
  later phase; this slice only does one-shot JSON.
