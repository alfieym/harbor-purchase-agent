import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  MAX_TURNS,
  appendPending,
  decodePersisted,
  settleLast,
} from './turns.ts'

test('the legacy single-run record still restores its one turn', () => {
  // The shape shipped before multi-turn persistence.
  const decoded = decodePersisted({ threadId: 't1', runId: 'r1', userText: '找台灯' })

  assert.deepEqual(decoded, {
    threadId: 't1',
    turns: [{ runId: 'r1', userText: '找台灯' }],
  })
})

test('a turn whose run never started keeps the ask without a run id', () => {
  const decoded = decodePersisted({
    threadId: 't1',
    turns: [{ runId: null, userText: '找台灯' }],
  })

  assert.deepEqual(decoded, { threadId: 't1', turns: [{ runId: null, userText: '找台灯' }] })
})

test('garbage is not a conversation', () => {
  assert.equal(decodePersisted(null), null)
  assert.equal(decodePersisted('nope'), null)
  assert.equal(decodePersisted({}), null)
  assert.equal(decodePersisted({ turns: [] }), null)
  assert.equal(decodePersisted({ turns: [{ nope: 1 }] }), null)
})

test('appending a pending turn keeps the earlier runs in order', () => {
  const first = decodePersisted({ threadId: 't1', runId: 'r1', userText: '一' })
  const second = appendPending(first, '二')

  assert.deepEqual(second, {
    threadId: 't1',
    turns: [
      { runId: 'r1', userText: '一' },
      { runId: null, userText: '二' },
    ],
  })
})

test('settling a pending turn records its run and thread', () => {
  const pending = appendPending(
    { threadId: 't1', turns: [{ runId: 'r1', userText: '一' }] },
    '二',
  )

  assert.deepEqual(settleLast(pending, 't2', 'r2'), {
    threadId: 't2',
    turns: [
      { runId: 'r1', userText: '一' },
      { runId: 'r2', userText: '二' },
    ],
  })
})

test('the persisted log is bounded so localStorage cannot grow without end', () => {
  let saved = decodePersisted({ threadId: 't1', runId: 'r0', userText: '0' })
  for (let index = 1; index <= MAX_TURNS + 5; index++) {
    saved = appendPending(saved, String(index))
  }

  assert.equal(saved.turns.length, MAX_TURNS)
  assert.equal(saved.turns[saved.turns.length - 1].userText, String(MAX_TURNS + 5))
})
