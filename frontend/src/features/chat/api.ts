/** The AG-UI HTTP client (S14): start, follow, cancel, inspect.
 *
 * Nothing here knows about React. ``fetch`` is injectable so the tests run
 * against real ``Response`` objects without a browser or a server.
 *
 * ``streamRunEvents`` is the whole client-side state machine: the server
 * replays from ``after`` and then follows the live run until its terminal
 * frame, so the same function serves a fresh run, a reconnecting tab, and a
 * refresh that rebuilds the screen from the durable log.
 */

import { createSseParser } from './sse.ts'
import type { AguiEvent, RunRecord } from './types.ts'

const BASE = ''

type Deps = { fetch?: typeof fetch }

export type StartRunInput = {
  threadId: string | null
  buyerId: string
  shoppingSessionId: string
  text: string
}

export type StartRunResult = { runId: string; threadId: string }

export type StreamOptions = Deps & {
  /** Skip events at or below this sequence — the client's ``lastSeq``. */
  after: number
  signal?: AbortSignal
  onFrame: (seq: number, event: AguiEvent) => void
}

export type CancelResult = { runId: string; status: string }

function resolveFetch(deps: Deps): typeof fetch {
  return deps.fetch ?? globalThis.fetch
}

async function failure(response: Response): Promise<Error> {
  let detail = ''
  try {
    const body: unknown = await response.json()
    if (body !== null && typeof body === 'object' && 'detail' in body) {
      detail = String((body as { detail: unknown }).detail)
    }
  } catch {
    detail = ''
  }
  return new Error(detail || `后端返回 HTTP ${response.status}`)
}

export async function startRun(
  input: StartRunInput,
  deps: Deps = {},
): Promise<StartRunResult> {
  const response = await resolveFetch(deps)(`${BASE}/agui/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({
      threadId: input.threadId,
      buyerId: input.buyerId,
      shoppingSessionId: input.shoppingSessionId,
      messages: [{ role: 'user', content: input.text }],
    }),
  })
  if (!response.ok) throw await failure(response)
  return (await response.json()) as StartRunResult
}

export async function streamRunEvents(runId: string, options: StreamOptions): Promise<void> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  // The header is what a reconnecting EventSource sends; ``after`` is what a
  // plain fetch sends. Both mean "resume", so send both.
  if (options.after > 0) headers['Last-Event-ID'] = String(options.after)

  const url = `${BASE}/agui/runs/${encodeURIComponent(runId)}/events?after=${options.after}`
  const response = await resolveFetch(options)(url, { headers, signal: options.signal })
  if (!response.ok) throw await failure(response)
  if (response.body === null) throw new Error('后端没有返回事件流')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const push = createSseParser()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      for (const frame of push(decoder.decode(value, { stream: true }))) {
        if (frame.id === null) {
          // Every durable event is numbered; an unnumbered one cannot be
          // applied idempotently, so it is a protocol violation, not noise.
          throw new Error('事件流中出现没有编号的帧')
        }
        options.onFrame(frame.id, JSON.parse(frame.data) as AguiEvent)
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export async function cancelRun(runId: string, deps: Deps = {}): Promise<CancelResult> {
  const response = await resolveFetch(deps)(
    `${BASE}/agui/runs/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST', headers: { Accept: 'application/json' } },
  )
  if (!response.ok) throw await failure(response)
  return (await response.json()) as CancelResult
}

export async function getRun(runId: string, deps: Deps = {}): Promise<RunRecord> {
  const response = await resolveFetch(deps)(`${BASE}/agui/runs/${encodeURIComponent(runId)}`, {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await failure(response)
  return (await response.json()) as RunRecord
}
