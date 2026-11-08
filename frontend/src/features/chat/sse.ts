/** Incremental SSE parsing (S14).
 *
 * The server frames every event as ``id: <seq>`` + ``data: <json>`` with no
 * ``event:`` line — the type lives inside the JSON — and sends ``: heartbeat``
 * comments while a run is quiet. Chunks arrive at arbitrary boundaries, so the
 * parser keeps the incomplete tail and only emits complete frames.
 */

export type SseFrame = {
  id: number | null
  data: string
}

const FRAME_SEPARATOR = /\r?\n\r?\n/

export function createSseParser(): (chunk: string) => SseFrame[] {
  let buffer = ''

  return function push(chunk: string): SseFrame[] {
    buffer += chunk
    const frames: SseFrame[] = []
    for (;;) {
      const match = FRAME_SEPARATOR.exec(buffer)
      if (match === null) break
      const block = buffer.slice(0, match.index)
      buffer = buffer.slice(match.index + match[0].length)
      const frame = parseBlock(block)
      if (frame !== null) frames.push(frame)
    }
    return frames
  }
}

function parseBlock(block: string): SseFrame | null {
  let id: number | null = null
  const data: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (line === '' || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'id') {
      const parsed = Number(value)
      id = Number.isFinite(parsed) ? parsed : null
    } else if (field === 'data') {
      data.push(value)
    }
  }
  if (data.length === 0) return null
  return { id, data: data.join('\n') }
}
