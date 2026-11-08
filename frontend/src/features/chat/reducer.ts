/** The one place AG-UI events become screen state (S14).
 *
 * Pure and sequence-aware: replaying the durable log from ``after=0`` rebuilds
 * the same screen a live stream produced, and an event that arrives twice (a
 * reconnect overlapping the replay) is ignored by its ``seq``.
 *
 * The hard rule from the design: product cards come only from
 * ``STATE_SNAPSHOT``, which carries the structured ``CommerceIntentResponse``.
 * ``TEXT_MESSAGE_CONTENT`` is prose; nothing here parses it for products.
 */

import type {
  AguiEvent,
  ChatMessage,
  ChatState,
  ProductCard,
  RunStatus,
  ToolActivity,
} from './types.ts'

export const initialState: ChatState = {
  threadId: null,
  runId: null,
  status: 'idle',
  lastSeq: 0,
  messages: [],
  tools: [],
  cards: [],
  error: null,
}

export function withUserMessage(state: ChatState, text: string): ChatState {
  const index = state.messages.filter((message) => message.role === 'user').length + 1
  return {
    ...state,
    messages: [...state.messages, { id: `user-${index}`, role: 'user', content: text }],
    error: null,
  }
}

function runStatus(status: string | undefined): RunStatus {
  return status === undefined ? 'running' : (terminalRunStatus(status) ?? 'running')
}

/** The durable record's status, when it already ended the run.
 *
 * ``interrupted`` is a process restart, not a user action, so it reads as a
 * failure. Anything else (``running``, an unknown value) is still live.
 */
export function terminalRunStatus(status: string): RunStatus | null {
  switch (status) {
    case 'finished':
    case 'cancelled':
    case 'failed':
      return status
    case 'interrupted':
      return 'failed'
    default:
      return null
  }
}

/** A dropped stream is a client failure — unless the run already ended.
 *
 * The connection can reset right after the terminal frame; that must not
 * relabel a finished or cancelled run as failed.
 */
export function noteStreamFailure(state: ChatState, message: string): ChatState {
  if (state.status !== 'running' && state.status !== 'idle') return state
  return { ...state, status: 'failed', error: message }
}

function upsertMessage(
  messages: ChatMessage[],
  messageId: string,
  update: (current: ChatMessage) => ChatMessage,
): ChatMessage[] {
  const existing = messages.findIndex((message) => message.id === messageId)
  if (existing === -1) {
    const created: ChatMessage = { id: messageId, role: 'assistant', content: '' }
    return [...messages, update(created)]
  }
  const next = [...messages]
  next[existing] = update(next[existing])
  return next
}

function upsertTool(
  tools: ToolActivity[],
  toolCallId: string,
  update: (current: ToolActivity) => ToolActivity,
): ToolActivity[] {
  const existing = tools.findIndex((tool) => tool.id === toolCallId)
  if (existing === -1) {
    const created: ToolActivity = { id: toolCallId, name: 'tool', args: '', result: null }
    return [...tools, update(created)]
  }
  const next = [...tools]
  next[existing] = update(next[existing])
  return next
}

function finish(state: ChatState, status: RunStatus, error: string | null = null): ChatState {
  return { ...state, status, error }
}

export function applyEvent(state: ChatState, seq: number, event: AguiEvent): ChatState {
  if (seq <= state.lastSeq) return state
  const next: ChatState = { ...state, lastSeq: seq }

  switch (event.type) {
    case 'RUN_STARTED': {
      const started = event as { threadId: string; runId: string }
      return { ...next, threadId: started.threadId, runId: started.runId, status: 'running' }
    }
    case 'TEXT_MESSAGE_START': {
      const start = event as { messageId: string }
      return {
        ...next,
        messages: upsertMessage(next.messages, start.messageId, (current) => current),
      }
    }
    case 'TEXT_MESSAGE_CONTENT': {
      const content = event as { messageId: string; delta: string }
      return {
        ...next,
        messages: upsertMessage(next.messages, content.messageId, (current) => ({
          ...current,
          content: current.content + content.delta,
        })),
      }
    }
    case 'TOOL_CALL_START': {
      const start = event as { toolCallId: string; toolCallName: string }
      return {
        ...next,
        tools: upsertTool(next.tools, start.toolCallId, (current) => ({
          ...current,
          name: start.toolCallName,
        })),
      }
    }
    case 'TOOL_CALL_ARGS': {
      const args = event as { toolCallId: string; delta: string }
      return {
        ...next,
        tools: upsertTool(next.tools, args.toolCallId, (current) => ({
          ...current,
          args: current.args + args.delta,
        })),
      }
    }
    case 'TOOL_CALL_RESULT': {
      const result = event as { toolCallId: string; content: string }
      return {
        ...next,
        tools: upsertTool(next.tools, result.toolCallId, (current) => ({
          ...current,
          result: result.content,
        })),
      }
    }
    case 'STATE_SNAPSHOT': {
      const snapshot = (
        event as { snapshot: { status: string; final_text: string; cards: ProductCard[] } }
      ).snapshot
      const messageId = state.runId === null ? 'assistant' : `msg_${state.runId}`
      const withText =
        snapshot.final_text && !next.messages.some((message) => message.id === messageId)
          ? {
              ...next,
              messages: upsertMessage(next.messages, messageId, (current) =>
                current.content === '' ? { ...current, content: snapshot.final_text } : current,
              ),
            }
          : next
      return {
        ...withText,
        cards: snapshot.cards ?? [],
        status: runStatus(snapshot.status),
      }
    }
    case 'RUN_FINISHED': {
      const finished = event as { result?: { status?: string } }
      return finish(next, runStatus(finished.result?.status ?? 'finished'))
    }
    case 'RUN_ERROR': {
      const error = event as { message: string }
      return finish(next, 'failed', error.message)
    }
    default:
      return next
  }
}
