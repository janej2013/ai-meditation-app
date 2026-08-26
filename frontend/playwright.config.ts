/**
 * Browser E2E. The unit tests run in jsdom, which is why two defects reached
 * this branch: `global` is defined there but not in a browser, and jsdom has
 * no notion of a reload. This suite exists for the failures a fake DOM cannot
 * express.
 *
 * The default project stubs every network call, so it needs no AWS account and
 * spends nothing. `smoke` drives the deployed dev stack instead and is opt-in
 * through `make smoke CONFIRM=1`.
 */
import { defineConfig, devices } from '@playwright/test'

const PORT = 5173

export default defineConfig({
  testDir: './e2e',
  // The suite talks to no shared resource, so parallel is safe; CI keeps one
  // worker so a flake is a flake and not a race between workers.
  fullyParallel: true,
  workers: process.env.CI ? 1 : undefined,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: 'retain-on-failure',
  },

  projects: [
    {
      name: 'stubbed',
      testIgnore: /.*\.smoke\.spec\.ts/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'smoke',
      testMatch: /.*\.smoke\.spec\.ts/,
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    // strictPort because the API's CORS allow-list names 5173 exactly; a
    // silent fallback to 5174 fails every request as a cross-origin error.
    command: `npm run dev -- --port ${PORT} --strictPort`,
    // The stubbed project never reaches a real API, but the client refuses
    // to run without a base URL and CI has no .env.local; a fixed stub host
    // (process env outranks .env files in Vite) gives page.route() something
    // to intercept on every machine.
    env: { ...process.env, VITE_API_URL: 'https://api.stub' },
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
