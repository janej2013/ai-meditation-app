/**
 * SHA-256 of a request body, hex encoded.
 *
 * CloudFront's origin access control signs every request it forwards to the
 * agent's Lambda Function URL, and Lambda refuses unsigned payloads: any
 * request with a method that may carry a body must state the body's hash in
 * `x-amz-content-sha256`, even when the body is empty. Locally the header is
 * ignored, so it is sent unconditionally rather than guessed per environment.
 */

/** SHA-256 of the empty string -- what a bodiless POST or DELETE sends. */
export const EMPTY_BODY_SHA256 =
  'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

export async function sha256Hex(body: string): Promise<string> {
  if (body === '') return EMPTY_BODY_SHA256
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body))
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}
