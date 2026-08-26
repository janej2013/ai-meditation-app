import { describe, expect, it } from 'vitest'
import { parseFrame, readSse } from './sse'

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c))
      controller.close()
    },
  })
}

async function collect(chunks: string[]) {
  const out = []
  for await (const e of readSse(streamOf(chunks))) out.push(e)
  return out
}

describe('SSE parsing', () => {
  it('parses one frame', () => {
    expect(parseFrame('event: delta\ndata: {"text":"hi"}')).toEqual({
      event: 'delta',
      data: { text: 'hi' },
    })
  })

  it('ignores comment frames (the heartbeat)', () => {
    expect(parseFrame(': ping')).toBeNull()
  })

  it('keeps colons inside the data', () => {
    expect(parseFrame('event: delta\ndata: {"text":"a: b"}')).toEqual({
      event: 'delta',
      data: { text: 'a: b' },
    })
  })

  it('reads a frame split across chunks, and several in one', async () => {
    const events = await collect([
      'event: tool\ndata: {"name":"get_sess',
      'ion_history"}\n\n: ping\n\nevent: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {"text":"b"}\n\n',
      'event: done\ndata: {"turn":1,"job_id":null,"awaiting_confirmation":false,"turns_left":11}',
    ])

    expect(events.map((e) => e.event)).toEqual(['tool', 'delta', 'delta', 'done'])
    expect(events[0].data).toEqual({ name: 'get_session_history' })
    expect(events[3].data).toMatchObject({ turn: 1, turns_left: 11 })
  })
})
