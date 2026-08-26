/**
 * The companion in a real browser: a streamed turn rendered as it arrives,
 * the proposal card, and confirm landing on the generating screen. Every
 * network call is stubbed -- the runner is a Function URL that costs per
 * invocation, and the unit tests already cover the states; this exists for
 * the things jsdom cannot express (a real ReadableStream over fetch,
 * `crypto.subtle` in the page, the header rewriting the dev proxy sees).
 *
 * The page reads its ID token from `sessionStorage['drift:e2e-id-token']`
 * in dev builds only, so no Cognito user is needed.
 */
import { expect, test, type Page } from '@playwright/test'

const SSE_PROPOSAL =
  'event: tool\ndata: {"name":"get_session_history"}\n\n' +
  ': ping\n\n' +
  'event: tool\ndata: {"name":"finalize_meditation_brief"}\n\n' +
  'event: proposal\ndata: {"duration_minutes":10}\n\n' +
  'event: delta\ndata: {"text":"I\'ve put together a slow, ten-minute meditation "}\n\n' +
  'event: delta\ndata: {"text":"with shoreline imagery. Start it whenever you like, or tell me what to change."}\n\n' +
  'event: done\ndata: {"turn":1,"job_id":null,"awaiting_confirmation":true,"turns_left":11}\n\n'

async function stubRunner(page: Page) {
  const captured: { headers: Record<string, string>; method: string; url: string }[] = []

  await page.addInitScript(() => {
    sessionStorage.setItem('drift:e2e-id-token', 'e2e.id.token')
  })
  await page.route('**/account', (route) =>
    route.fulfill({ json: { available: 95, frozen: 0, plan: 'pro' } }),
  )
  await page.route('**/jobs**', (route) =>
    route.fulfill({ json: { jobs: [], job_id: 'job-9', status: 'GENERATING' } }),
  )
  await page.route('**/agent/**', (route) => {
    const req = route.request()
    captured.push({ headers: req.headers(), method: req.method(), url: req.url() })
    const path = new URL(req.url()).pathname
    if (path === '/agent/sessions' && req.method() === 'POST')
      return route.fulfill({
        status: 201,
        json: { session_id: 's1', turn: 0, engine: 'native', model_id: 'm', insights_count: 1 },
      })
    if (path === '/agent/sessions/s1/turns')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: SSE_PROPOSAL,
      })
    if (path === '/agent/sessions/s1' && req.method() === 'GET')
      return route.fulfill({
        json: {
          session_id: 's1',
          status: 'ACTIVE',
          turn: 1,
          job_id: null,
          pending: {
            brief: 'A slow ten-minute meditation over a shoreline at dusk, with long pauses.',
            duration_minutes: 10,
          },
          turns: [],
        },
      })
    if (path === '/agent/sessions/s1/confirm')
      return route.fulfill({ status: 200, json: { job_id: 'job-9' } })
    return route.fulfill({ status: 404, json: { detail: 'unstubbed' } })
  })
  return captured
}

test('a turn streams, proposes, and confirm starts the meditation', async ({ page }) => {
  const captured = await stubRunner(page)

  await page.goto('/companion')
  await expect(page.getByText('Tell me how tonight feels')).toBeVisible()

  await page.getByLabel('Message').fill('tired but wired')
  await page.getByLabel('Send').click()

  await expect(page.getByText('tired but wired')).toBeVisible()
  await expect(page.getByText(/Start it whenever you like/)).toBeVisible()
  await expect(page.getByText('10 min')).toBeVisible()
  await expect(page.getByText('Uses 1 credit · 95 left')).toBeVisible()

  // The runner sees the token in X-Id-Token, never Authorization (the OAC
  // overwrites that), and every POST carries the payload hash.
  const turn = captured.find((c) => c.url.endsWith('/turns'))!
  expect(turn.headers['x-id-token']).toBe('e2e.id.token')
  expect(turn.headers['authorization']).toBeUndefined()
  expect(turn.headers['x-amz-content-sha256']).toMatch(/^[0-9a-f]{64}$/)

  await page.getByText('Read the brief').click()
  await expect(page.getByText(/with long pauses/)).toBeVisible()

  await page.getByText('Start the meditation').click()
  await expect(page).toHaveURL(/\/generating\/job-9$/)
  expect(captured.some((c) => c.url.endsWith('/confirm') && c.method === 'POST')).toBe(true)
})

test('?engine=langgraph talks to the other behaviour', async ({ page }) => {
  const seen: string[] = []
  await page.addInitScript(() => {
    sessionStorage.setItem('drift:e2e-id-token', 'e2e.id.token')
  })
  await page.route('**/account', (route) =>
    route.fulfill({ json: { available: 95, frozen: 0, plan: 'pro' } }),
  )
  await page.route('**/agent/memory', (route) =>
    route.fulfill({ json: { insights: [], sessions_this_month: 0, sessions_per_month: 30 } }),
  )
  await page.route('**/agent-lg/**', (route) => {
    const req = route.request()
    seen.push(`${req.method()} ${new URL(req.url()).pathname}`)
    if (req.url().endsWith('/agent-lg/sessions'))
      return route.fulfill({
        status: 201,
        json: { session_id: 'lg1', turn: 0, engine: 'langgraph', model_id: 'm', insights_count: 0 },
      })
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body:
        'event: delta\ndata: {"text":"From the graph."}\n\n' +
        'event: done\ndata: {"turn":1,"job_id":null,"awaiting_confirmation":false,"turns_left":11}\n\n',
    })
  })

  await page.goto('/companion?engine=langgraph')
  await page.getByLabel('Message').fill('hello')
  await page.getByLabel('Send').click()

  await expect(page.getByText('From the graph.')).toBeVisible()
  expect(seen).toEqual(['POST /agent-lg/sessions', 'POST /agent-lg/sessions/lg1/turns'])
  await expect(page.locator('.companion')).toHaveAttribute('data-engine', 'langgraph')
})

test('a free account is shown the Pro screen', async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('drift:e2e-id-token', 'e2e.id.token')
  })
  await page.route('**/account', (route) =>
    route.fulfill({ json: { available: 1, frozen: 0, plan: 'free' } }),
  )
  await page.route('**/agent/memory', (route) =>
    route.fulfill({ status: 403, json: { detail: 'plan_required' } }),
  )
  await page.route('**/agent/sessions', (route) =>
    route.fulfill({ status: 403, json: { detail: 'plan_required' } }),
  )

  await page.goto('/companion')
  await page.getByLabel('Message').fill('hello')
  await page.getByLabel('Send').click()

  await expect(page.getByText('Companion is part of Pro')).toBeVisible()
  await page.getByText('See Pro').click()
  await expect(page).toHaveURL(/\/plans\?plan=plan_pro$/)
})
