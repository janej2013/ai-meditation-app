/**
 * The typed API client. Mirrors the FastAPI routers' Pydantic models
 * one-for-one — those are the contract, so field names stay snake_case.
 */
import { getIdToken } from '../auth/cognito'

/** Read per call, not at module load, so tests can stub the env after import. */
function baseUrl(): string {
  return import.meta.env.VITE_API_URL ?? ''
}

export type JobStatus = 'PENDING' | 'FROZEN' | 'GENERATING' | 'DONE' | 'FAILED' | 'ROLLED_BACK'

export interface Account {
  available: number
  frozen: number
  plan: string
}

export interface GenerateResponse {
  job_id: string
  status: JobStatus
}

export interface Job {
  job_id: string
  status: JobStatus
  audio_url: string | null
}

export interface CheckoutResponse {
  checkout_url: string
  product_key: string
}

/** Non-2xx from the API, carrying the status the pages branch on (402, 429…). */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

/** Thrown when there is no valid session; routing sends the user to sign in. */
export class NotSignedInError extends Error {
  constructor() {
    super('Not signed in')
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = baseUrl()
  if (!base) throw new Error('VITE_API_URL is not set')

  const token = await getIdToken()
  if (!token) throw new NotSignedInError()

  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      // The ID token, never the access token — see auth/cognito.ts.
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = ((await response.json()) as { detail?: string }).detail ?? detail
    } catch {
      // A non-JSON error body keeps the status text.
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

export function getAccount(): Promise<Account> {
  return request<Account>('/account')
}

export function startGeneration(
  mood: string,
  durationMinutes: number,
): Promise<GenerateResponse> {
  return request<GenerateResponse>('/generate', {
    method: 'POST',
    body: JSON.stringify({ mood, duration_minutes: durationMinutes }),
  })
}

export function getJob(jobId: string): Promise<Job> {
  return request<Job>(`/jobs/${encodeURIComponent(jobId)}`)
}

export function createCheckout(productKey: string): Promise<CheckoutResponse> {
  return request<CheckoutResponse>('/billing/checkout', {
    method: 'POST',
    body: JSON.stringify({ product_key: productKey }),
  })
}

/**
 * The state machine's own execution timeout, published by the Pipeline stack
 * as `JobTimeoutMs`. Polling for less than this reports a failure for an
 * execution that is still running -- and that execution goes on to commit the
 * credit, so the caller is billed for a generation they were told failed, and
 * `frozen >= 1` blocks them from retrying until it finishes.
 *
 * The fallback is the CDK default, and deliberately errs long: waiting too
 * long costs a spinner, waiting too little costs a credit.
 */
const DEFAULT_JOB_TIMEOUT_MS = 30 * 60 * 1000

function jobTimeoutMs(): number {
  const configured = Number(import.meta.env.VITE_JOB_TIMEOUT_MS)
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_JOB_TIMEOUT_MS
}

/**
 * Poll a job until it leaves the pipeline.
 *
 * The interval starts short (the whole pipeline often finishes inside a
 * minute) and backs off; the deadline comes from the state machine, so a job
 * is only ever declared timed out once it truly cannot still be running.
 */
export async function pollJob(
  jobId: string,
  opts: {
    onUpdate?: (job: Job) => void
    signal?: AbortSignal
    initialIntervalMs?: number
    maxIntervalMs?: number
    timeoutMs?: number
  } = {},
): Promise<Job> {
  const {
    onUpdate,
    signal,
    initialIntervalMs = 2000,
    maxIntervalMs = 10000,
    timeoutMs = jobTimeoutMs(),
  } = opts

  const deadline = Date.now() + timeoutMs
  let interval = initialIntervalMs

  for (;;) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')

    const job = await getJob(jobId)
    onUpdate?.(job)
    // The API maps ROLLED_BACK to FAILED, so DONE | FAILED is terminal.
    if (job.status === 'DONE' || job.status === 'FAILED') return job

    if (Date.now() + interval > deadline) {
      throw new ApiError(408, 'Generation timed out')
    }
    await new Promise<void>((resolve, reject) => {
      // An abort that landed while getJob was in flight would otherwise be
      // missed entirely: addEventListener never fires on an already-aborted
      // signal, and the loop would sleep the full interval before noticing.
      if (signal?.aborted) {
        reject(new DOMException('Aborted', 'AbortError'))
        return
      }
      const t = setTimeout(resolve, interval)
      signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(t)
          reject(new DOMException('Aborted', 'AbortError'))
        },
        { once: true },
      )
    })
    interval = Math.min(interval * 1.5, maxIntervalMs)
  }
}
