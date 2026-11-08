/** The conversation a refresh rebuilds from (S14).
 *
 * One entry per turn, in order. Each holds the run that answered it, so a
 * restore can replay every run's durable log sequentially — sequence numbers
 * are scoped per run, so replaying two runs at once would collide.
 *
 * A turn whose run never started keeps ``runId: null``: the ask is shown, but
 * there is nothing to replay.
 */

export type Turn = {
  runId: string | null
  userText: string
}

export type Persisted = {
  threadId: string | null
  turns: Turn[]
}

/** Enough to rebuild a working conversation without unbounded storage. */
export const MAX_TURNS = 20

function isTurn(value: unknown): value is Turn {
  if (value === null || typeof value !== 'object') return false
  const turn = value as Record<string, unknown>
  return (
    typeof turn.userText === 'string' &&
    (turn.runId === null || typeof turn.runId === 'string')
  )
}

export function decodePersisted(raw: unknown): Persisted | null {
  if (raw === null || typeof raw !== 'object') return null
  const record = raw as Record<string, unknown>
  const threadId = typeof record.threadId === 'string' ? record.threadId : null

  if (Array.isArray(record.turns)) {
    const turns = record.turns.filter(isTurn)
    return turns.length === 0 ? null : { threadId, turns }
  }

  // The shape written before a thread could hold more than one turn.
  if (isTurn(record)) {
    return { threadId, turns: [{ runId: record.runId, userText: record.userText }] }
  }
  return null
}

export function appendPending(persisted: Persisted | null, userText: string): Persisted {
  const turns = [...(persisted?.turns ?? []), { runId: null, userText } satisfies Turn]
  return { threadId: persisted?.threadId ?? null, turns: turns.slice(-MAX_TURNS) }
}

export function settleLast(persisted: Persisted, threadId: string, runId: string): Persisted {
  const turns = [...persisted.turns]
  const last = turns.length - 1
  if (last >= 0) turns[last] = { ...turns[last], runId }
  return { threadId, turns }
}
