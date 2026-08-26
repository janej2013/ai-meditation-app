/**
 * Server-sent events over `fetch`.
 *
 * `EventSource` cannot send headers, and the runner wants the ID token in
 * one, so the stream is read by hand: frames are blank-line separated,
 * `event:` names the type, `data:` carries JSON, and a leading colon is a
 * comment (the runner's `: ping` heartbeat). A frame may straddle two chunks,
 * so the parser keeps whatever has not ended in a blank line.
 */

export interface SseEvent<T = unknown> {
  event: string
  data: T
}

export function parseFrame(frame: string): SseEvent | null {
  let event: string | null = null
  const data: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice('event:'.length).trim()
    else if (line.startsWith('data:')) data.push(line.slice('data:'.length).trimStart())
  }
  if (event === null) return null
  const raw = data.join('\n')
  return { event, data: raw ? (JSON.parse(raw) as unknown) : null }
}

/** Yield each event from a streamed SSE body until it ends or `signal` aborts. */
export async function* readSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const onAbort = () => void reader.cancel().catch(() => undefined)
  signal?.addEventListener('abort', onAbort, { once: true })
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let cut: number
      while ((cut = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        const parsed = parseFrame(frame)
        if (parsed) yield parsed
      }
    }
    // A final frame without the trailing blank line.
    const last = parseFrame(buffer)
    if (last) yield last
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }
}
