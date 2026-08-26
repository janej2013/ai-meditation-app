import { describe, expect, it } from 'vitest'
import { EMPTY_BODY_SHA256, sha256Hex } from './sha256'

describe('sha256Hex', () => {
  it('hashes the empty body to the well-known digest', async () => {
    expect(await sha256Hex('')).toBe(EMPTY_BODY_SHA256)
    expect(EMPTY_BODY_SHA256).toBe(
      'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    )
  })

  it('matches a known vector', async () => {
    expect(await sha256Hex('abc')).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
  })
})
