import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createSseParser } from './sse.ts'

test('a frame split across chunks is not emitted until it is complete', () => {
  const push = createSseParser()

  assert.deepEqual(push('id: 1\nda'), [])
  assert.deepEqual(push('ta: {"type":"RUN_STARTED"}\n\n'), [
    { id: 1, data: '{"type":"RUN_STARTED"}' },
  ])
})

test('comment heartbeats are not frames', () => {
  const push = createSseParser()

  assert.deepEqual(push(': heartbeat\n\n'), [])
  assert.deepEqual(push('id: 2\ndata: {}\n\n'), [{ id: 2, data: '{}' }])
})

test('CRLF line endings and multi-line data are handled', () => {
  const push = createSseParser()

  assert.deepEqual(push('id: 7\r\ndata: {"a":\r\ndata: 1}\r\n\r\n'), [
    { id: 7, data: '{"a":\n1}' },
  ])
})

test('several frames in one chunk are all returned in order', () => {
  const push = createSseParser()

  assert.deepEqual(push('id: 1\ndata: a\n\nid: 2\ndata: b\n\n'), [
    { id: 1, data: 'a' },
    { id: 2, data: 'b' },
  ])
})

test('a frame without an id still yields its data', () => {
  const push = createSseParser()

  assert.deepEqual(push('data: {}\n\n'), [{ id: null, data: '{}' }])
})
