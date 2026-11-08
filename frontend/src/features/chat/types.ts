/** AG-UI wire types (camelCase, exactly as the backend emits them). */

export type ProductCard = {
  title: string
  brand: string
  category: string
  price_major: number
  currency: string
  weight_kg: number
  material_tags: string[]
  highlights: string[]
  sku_id: string
  score: number | null
}

/** The structured state a `STATE_SNAPSHOT` carries (S14). */
export type RunSnapshot = {
  status: string
  final_text: string
  cards: ProductCard[]
  pending_draft: unknown
}

export type AguiEvent =
  | { type: 'RUN_STARTED'; threadId: string; runId: string }
  | { type: 'RUN_FINISHED'; threadId: string; runId: string; result?: { status?: string } }
  | { type: 'RUN_ERROR'; message: string; code?: string }
  | { type: 'STEP_STARTED'; stepName: string }
  | { type: 'STEP_FINISHED'; stepName: string }
  | { type: 'TEXT_MESSAGE_START'; messageId: string; role?: string }
  | { type: 'TEXT_MESSAGE_CONTENT'; messageId: string; delta: string }
  | { type: 'TEXT_MESSAGE_END'; messageId: string }
  | {
      type: 'TOOL_CALL_START'
      toolCallId: string
      toolCallName: string
      parentMessageId?: string
    }
  | { type: 'TOOL_CALL_ARGS'; toolCallId: string; delta: string }
  | { type: 'TOOL_CALL_END'; toolCallId: string }
  | { type: 'TOOL_CALL_RESULT'; messageId: string; toolCallId: string; content: string }
  | { type: 'STATE_SNAPSHOT'; snapshot: RunSnapshot }
  | { type: 'STATE_DELTA'; delta: unknown }
  | { type: string; [key: string]: unknown }

/** What `GET /agui/runs/{id}` returns: the durable record, not a screen.
 *
 * ``status`` is the server's raw value — it can be ``interrupted``, which is
 * a process restart rather than any of the UI's states. Map it through
 * ``terminalRunStatus`` before showing it.
 */
export type RunRecord = {
  runId: string
  threadId: string
  status: string
  lastSeq: number
  error: string | null
  createdAt: string
  updatedAt: string
}

export type RunStatus = 'idle' | 'running' | 'finished' | 'cancelled' | 'failed'

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

export type ToolActivity = {
  id: string
  name: string
  args: string
  result: string | null
}

export type ChatState = {
  threadId: string | null
  runId: string | null
  status: RunStatus
  lastSeq: number
  messages: ChatMessage[]
  tools: ToolActivity[]
  cards: ProductCard[]
  error: string | null
}
