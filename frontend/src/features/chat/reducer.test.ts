import assert from 'node:assert/strict'
import { test } from 'node:test'

import { applyEvent, initialState, noteStreamFailure, terminalRunStatus, withUserMessage } from './reducer.ts'
import type { AguiEvent, ChatState, ProductCard } from './types.ts'

const CARD: ProductCard = {
  title: '台灯',
  brand: 'Lumo',
  category: '家居',
  price_major: 99,
  currency: 'CNY',
  weight_kg: 0.4,
  material_tags: [],
  highlights: ['无频闪'],
  sku_id: 'X1-S1',
  score: null,
}

const SUCCESS: AguiEvent[] = [
  { type: 'RUN_STARTED', threadId: 't1', runId: 'r1' },
  { type: 'STEP_STARTED', stepName: 'agent' },
  { type: 'TEXT_MESSAGE_START', messageId: 'msg_r1', role: 'assistant' },
  { type: 'TOOL_CALL_START', toolCallId: 'c1', toolCallName: 'product_search' },
  { type: 'TOOL_CALL_ARGS', toolCallId: 'c1', delta: '{"query":"台灯"}' },
  { type: 'TOOL_CALL_END', toolCallId: 'c1' },
  { type: 'TOOL_CALL_RESULT', messageId: 'msg_c1', toolCallId: 'c1', content: 'found 1' },
  { type: 'TEXT_MESSAGE_CONTENT', messageId: 'msg_r1', delta: '找到 1 件台灯' },
  { type: 'TEXT_MESSAGE_END', messageId: 'msg_r1' },
  {
    type: 'STATE_SNAPSHOT',
    snapshot: {
      status: 'finished',
      final_text: '找到 1 件台灯',
      cards: [CARD],
      pending_draft: null,
    },
  },
  { type: 'STEP_FINISHED', stepName: 'agent' },
  { type: 'RUN_FINISHED', threadId: 't1', runId: 'r1', result: { status: 'finished' } },
]

function replay(events: AguiEvent[], state: ChatState = initialState): ChatState {
  // A resumed stream continues from wherever the state stopped, not from 1.
  return events.reduce((current, event) => applyEvent(current, current.lastSeq + 1, event), state)
}

test('a successful run rebuilds messages, tools and cards in order', () => {
  const state = replay(SUCCESS)

  assert.equal(state.status, 'finished')
  assert.equal(state.lastSeq, SUCCESS.length)
  assert.equal(state.runId, 'r1')
  assert.deepEqual(state.messages, [
    { id: 'msg_r1', role: 'assistant', content: '找到 1 件台灯' },
  ])
  assert.deepEqual(state.tools, [
    { id: 'c1', name: 'product_search', args: '{"query":"台灯"}', result: 'found 1' },
  ])
  assert.deepEqual(state.cards, [CARD])
})

test('cards never come from text content, only from STATE_SNAPSHOT', () => {
  const state = replay([
    { type: 'RUN_STARTED', threadId: 't1', runId: 'r1' },
    {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'msg_r1',
      delta: '{"cards":[{"sku_id":" FAKE "}]}',
    },
  ])

  assert.deepEqual(state.cards, [])
})

test('an event that was already applied is ignored by its sequence number', () => {
  const once = applyEvent(initialState, 1, {
    type: 'RUN_STARTED',
    threadId: 't1',
    runId: 'r1',
  })

  const again = applyEvent(once, 1, {
    type: 'TEXT_MESSAGE_CONTENT',
    messageId: 'msg_r1',
    delta: '不应该出现',
  })

  assert.deepEqual(again, once)
})

test('a content delta can arrive without its TEXT_MESSAGE_START (resumed replay)', () => {
  const state = replay(
    [{ type: 'TEXT_MESSAGE_CONTENT', messageId: 'msg_r1', delta: '半句话' }],
    { ...initialState, lastSeq: 8 },
  )

  assert.deepEqual(state.messages, [{ id: 'msg_r1', role: 'assistant', content: '半句话' }])
})

test('RUN_ERROR ends the run and keeps the stable message', () => {
  const state = replay([
    { type: 'RUN_STARTED', threadId: 't1', runId: 'r1' },
    { type: 'RUN_ERROR', message: '本轮处理失败', code: 'TURN_FAILED' },
  ])

  assert.equal(state.status, 'failed')
  assert.equal(state.error, '本轮处理失败')
})

test('RUN_FINISHED reports a cancelled run as cancelled', () => {
  const state = replay([
    { type: 'RUN_STARTED', threadId: 't1', runId: 'r1' },
    { type: 'RUN_FINISHED', threadId: 't1', runId: 'r1', result: { status: 'cancelled' } },
  ])

  assert.equal(state.status, 'cancelled')
})

test('the user message is kept in front of the replayed assistant answer', () => {
  const asked = withUserMessage(initialState, '找台灯')
  const answered = replay(SUCCESS, asked)

  assert.deepEqual(
    answered.messages.map((message) => message.role),
    ['user', 'assistant'],
  )
  assert.equal(answered.messages[0].content, '找台灯')
})

test('an interrupted run reads as a failure, never as running', () => {
  assert.equal(terminalRunStatus('interrupted'), 'failed')
  assert.equal(terminalRunStatus('finished'), 'finished')
  assert.equal(terminalRunStatus('cancelled'), 'cancelled')
  assert.equal(terminalRunStatus('running'), null)
  // S15: a registered-but-not-yet-claimed run is still live.
  assert.equal(terminalRunStatus('queued'), null)
})

test('a stream teardown cannot overwrite a run that already ended', () => {
  const finished = replay(SUCCESS)

  const after = noteStreamFailure(finished, '连接中断')

  assert.equal(after.status, 'finished')
  assert.equal(after.error, null)
})

test('a stream teardown during a live run is a visible failure', () => {
  const running = applyEvent(initialState, 1, {
    type: 'RUN_STARTED',
    threadId: 't1',
    runId: 'r1',
  })

  const after = noteStreamFailure(running, '连接中断')

  assert.equal(after.status, 'failed')
  assert.equal(after.error, '连接中断')
})
