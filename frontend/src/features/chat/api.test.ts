import assert from 'node:assert/strict'
import { test } from 'node:test'

import { cancelRun, getRun, startRun, streamRunEvents } from './api.ts'

type Seen = { seq: number; event: unknown }

function fakeFetch(
  responder: (url: string, init: RequestInit) => Response,
): { fetch: typeof fetch; calls: { url: string; init: RequestInit }[] } {
  const calls: { url: string; init: RequestInit }[] = []
  const impl = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    calls.push({ url, init })
    return responder(url, init)
  }) as typeof fetch
  return { fetch: impl, calls }
}

test('startRun posts the AG-UI body and returns the run identity', async () => {
  const { fetch, calls } = fakeFetch(
    () =>
      new Response(JSON.stringify({ runId: 'r1', threadId: 't1' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      }),
  )

  const started = await startRun(
    { threadId: null, buyerId: 'b1', shoppingSessionId: 's1', text: '找台灯' },
    { fetch },
  )

  assert.deepEqual(started, { runId: 'r1', threadId: 't1' })
  assert.equal(calls[0].url, '/agui/runs')
  assert.equal(calls[0].init.method, 'POST')
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
    threadId: null,
    buyerId: 'b1',
    shoppingSessionId: 's1',
    messages: [{ role: 'user', content: '找台灯' }],
  })
})

test('startRun surfaces a non-2xx response with its detail', async () => {
  const { fetch } = fakeFetch(
    () =>
      new Response(JSON.stringify({ detail: 'run 未启用' }), {
        status: 503,
        headers: { 'content-type': 'application/json' },
      }),
  )

  await assert.rejects(
    startRun({ threadId: null, buyerId: 'b1', shoppingSessionId: 's1', text: 'hi' }, { fetch }),
    /run 未启用/,
  )
})

test('streamRunEvents hands every frame to the callback with its sequence', async () => {
  const { fetch, calls } = fakeFetch(
    () =>
      new Response(
        ': heartbeat\n\n' +
          'id: 1\ndata: {"type":"RUN_STARTED","threadId":"t1","runId":"r1"}\n\n' +
          'id: 2\ndata: {"type":"RUN_FINISHED","threadId":"t1","runId":"r1"}\n\n',
        { status: 200, headers: { 'content-type': 'text/event-stream' } },
      ),
  )

  const seen: Seen[] = []
  await streamRunEvents('r1', {
    after: 0,
    fetch,
    onFrame: (seq, event) => seen.push({ seq, event }),
  })

  assert.deepEqual(seen, [
    { seq: 1, event: { type: 'RUN_STARTED', threadId: 't1', runId: 'r1' } },
    { seq: 2, event: { type: 'RUN_FINISHED', threadId: 't1', runId: 'r1' } },
  ])
  assert.equal(calls[0].url, '/agui/runs/r1/events?after=0')
})

test('a resumed stream asks the server to skip what was already applied', async () => {
  const { fetch, calls } = fakeFetch(
    () => new Response('id: 9\ndata: {"type":"STEP_STARTED","stepName":"agent"}\n\n'),
  )

  await streamRunEvents('r1', { after: 8, fetch, onFrame: () => {} })

  assert.equal(calls[0].url, '/agui/runs/r1/events?after=8')
  assert.equal(new Headers(calls[0].init.headers).get('last-event-id'), '8')
})

test('a body split across reads is reassembled before a frame fires', async () => {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('id: 1\nda'))
      controller.enqueue(encoder.encode('ta: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\n'))
      controller.close()
    },
  })
  const { fetch } = fakeFetch(() => new Response(body))

  const seen: Seen[] = []
  await streamRunEvents('r1', { after: 0, fetch, onFrame: (seq, event) => seen.push({ seq, event }) })

  assert.deepEqual(seen, [
    { seq: 1, event: { type: 'RUN_STARTED', threadId: 't', runId: 'r' } },
  ])
})

test('cancelRun is a command, not an inference from a closed socket', async () => {
  const { fetch, calls } = fakeFetch(
    () => new Response(JSON.stringify({ runId: 'r1', status: 'cancelled' }), { status: 200 }),
  )

  const result = await cancelRun('r1', { fetch })

  assert.deepEqual(result, { runId: 'r1', status: 'cancelled' })
  assert.equal(calls[0].url, '/agui/runs/r1/cancel')
  assert.equal(calls[0].init.method, 'POST')
})

test('getRun reads the durable snapshot for a refresh', async () => {
  const record = {
    runId: 'r1',
    threadId: 't1',
    status: 'finished',
    lastSeq: 12,
    error: null,
    createdAt: '2026-11-08T00:00:00Z',
    updatedAt: '2026-11-08T00:00:01Z',
  }
  const { fetch, calls } = fakeFetch(() => new Response(JSON.stringify(record)))

  assert.deepEqual(await getRun('r1', { fetch }), record)
  assert.equal(calls[0].url, '/agui/runs/r1')
})
