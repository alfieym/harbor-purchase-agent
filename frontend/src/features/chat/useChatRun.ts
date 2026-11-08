/** The chat screen's single stateful unit (S14).
 *
 * It owns one run at a time. Everything a run produces arrives as numbered
 * AG-UI events; this hook only moves them through ``applyEvent`` and keeps
 * the persistence that lets a refresh rebuild the screen:
 *
 * - every turn of the thread is stored as ``{runId, userText}``, in order;
 * - restoring replays each run's durable log from ``after=0``, one after the
 *   other — sequence numbers are scoped per run, so interleaving two replays
 *   would collide and the idempotence guard would drop the second;
 * - a dropped stream is a *client* problem: nothing here cancels the run.
 *   ``cancel`` is a separate, explicit command, and ``resume`` first asks the
 *   durable record whether there is still anything left to follow.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { cancelRun, getRun, startRun, streamRunEvents } from './api.ts'
import {
  applyEvent,
  initialState,
  noteStreamFailure,
  terminalRunStatus,
  withUserMessage,
} from './reducer.ts'
import { appendPending, decodePersisted, settleLast } from './turns.ts'
import type { AguiEvent, ChatState } from './types.ts'

const RUN_KEY = 'harbor.chat.run'
const SESSION_KEY = 'harbor.chat.session'
const BUYER_ID = 'buyer-demo'

function readStored<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key)
    return raw === null ? null : (JSON.parse(raw) as T)
  } catch {
    return null
  }
}

function writeStored(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Private mode or a full quota: the screen still works, it just cannot
    // rebuild after a refresh.
  }
}

function clearStored(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch {
    // Nothing to do; the stale key is harmless.
  }
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause)
}

/** A fresh run gets a fresh sequence space — seq is scoped to the run. */
function beginRun(state: ChatState): ChatState {
  return {
    ...state,
    lastSeq: 0,
    runId: null,
    status: 'running',
    tools: [],
    cards: [],
    error: null,
  }
}

function shoppingSessionId(): string {
  const saved = readStored<string>(SESSION_KEY)
  if (typeof saved === 'string' && saved !== '') return saved
  const created = crypto.randomUUID()
  writeStored(SESSION_KEY, created)
  return created
}

export function useChatRun() {
  const [state, setState] = useState<ChatState>(initialState)
  const [busy, setBusy] = useState(false)
  const stateRef = useRef(state)
  stateRef.current = state
  // ``generation`` discards frames from a stream that a newer one replaced.
  const generation = useRef(0)
  // ``epoch`` discards a restore that the user interrupted by sending.
  const epoch = useRef(0)
  const streaming = useRef(false)
  const abortRef = useRef<AbortController | null>(null)

  const follow = useCallback(async (runId: string, after: number) => {
    const mine = ++generation.current
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    streaming.current = true
    setBusy(true)
    try {
      await streamRunEvents(runId, {
        after,
        signal: controller.signal,
        onFrame: (seq, event) => {
          if (generation.current !== mine) return
          setState((current) => applyEvent(current, seq, event as AguiEvent))
        },
      })
    } catch (cause) {
      if (controller.signal.aborted || generation.current !== mine) return
      setState((current) => noteStreamFailure(current, describe(cause)))
    } finally {
      if (generation.current === mine) {
        streaming.current = false
        abortRef.current = null
        setBusy(false)
      }
    }
  }, [])

  useEffect(() => {
    const saved = decodePersisted(readStored<unknown>(RUN_KEY))
    if (saved === null) return
    const mine = epoch.current
    void (async () => {
      for (const turn of saved.turns) {
        if (epoch.current !== mine) return
        setState((current) => withUserMessage(beginRun(current), turn.userText))
        if (turn.runId === null) {
          // The ask was persisted, the run never started: say so instead of
          // leaving the screen claiming to be processing.
          setState((current) => noteStreamFailure(current, '这一轮没有启动成功'))
          continue
        }
        await follow(turn.runId, 0)
      }
    })()
    return () => {
      epoch.current += 1
      generation.current += 1
      abortRef.current?.abort()
    }
  }, [follow])

  const send = useCallback(
    async (text: string) => {
      const content = text.trim()
      if (content === '' || streaming.current) return
      // Claim the slot before the first await: a double submit would otherwise
      // start two runs while ``startRun`` is still in flight.
      streaming.current = true
      epoch.current += 1
      const threadId = stateRef.current.threadId
      const before = decodePersisted(readStored<unknown>(RUN_KEY))
      // Persist the ask before the request: if the tab reloads while the run
      // is starting, the user still sees what they typed.
      writeStored(RUN_KEY, appendPending(before, content))
      setState((current) => withUserMessage(beginRun(current), content))
      try {
        const started = await startRun({
          threadId,
          buyerId: BUYER_ID,
          shoppingSessionId: shoppingSessionId(),
          text: content,
        })
        const pending = decodePersisted(readStored<unknown>(RUN_KEY))
        writeStored(
          RUN_KEY,
          pending === null
            ? appendPending(before, content)
            : settleLast(pending, started.threadId, started.runId),
        )
        // Know the run before its first frame: a stream that dies before
        // RUN_STARTED must still be resumable and cancellable.
        setState((current) => ({
          ...current,
          runId: started.runId,
          threadId: started.threadId,
        }))
        await follow(started.runId, 0)
      } catch (cause) {
        streaming.current = false
        // The turn never started; do not leave a phantom ask in storage.
        if (before === null) clearStored(RUN_KEY)
        else writeStored(RUN_KEY, before)
        setState((current) => noteStreamFailure(current, describe(cause)))
      }
    },
    [follow],
  )

  const cancel = useCallback(async () => {
    const runId = stateRef.current.runId
    if (runId === null) return
    try {
      await cancelRun(runId)
    } catch (cause) {
      setState((current) => ({ ...current, error: describe(cause) }))
    }
  }, [])

  const resume = useCallback(async () => {
    const { runId, lastSeq } = stateRef.current
    if (runId === null || streaming.current) return
    // Ask the durable record first: a run that already ended has nothing left
    // to follow, and flipping to "running" would strand the screen forever.
    try {
      const record = await getRun(runId)
      const ended = terminalRunStatus(record.status)
      if (ended !== null) {
        setState((current) => ({
          ...current,
          status: ended,
          error: record.error ?? current.error,
          lastSeq: Math.max(current.lastSeq, record.lastSeq),
        }))
        return
      }
    } catch {
      // The record is unreadable; the stream reports the failure itself.
    }
    setState((current) => ({ ...current, status: 'running', error: null }))
    await follow(runId, lastSeq)
  }, [follow])

  return { state, busy, send, cancel, resume }
}
