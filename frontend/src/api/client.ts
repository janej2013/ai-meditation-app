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
  /** Set once the pipeline has described the user's picture; null until then. */
  picture_keywords: string[] | null
  /** DONE picture jobs: a signed URL to the upload, for the revisit cloud. */
  picture_url?: string | null
}

/** A one-shot S3 POST, signed by the API, for one JPEG. */
export interface PictureUpload {
  picture_id: string
  url: string
  fields: Record<string, string>
  expires_in: number
}

export interface DreamscapeItem {
  job_id: string
  /** Picture jobs only; text jobs carry mood_excerpt instead. */
  keywords: string[] | null
  mood_excerpt: string | null
  duration_minutes: number | null
  source_type: 'picture' | 'text'
  created_at: string | null
}

export interface DreamscapeList {
  items: DreamscapeItem[]
  next_cursor: string | null
  /** The whole collection's size, not this page's. */
  total: number
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
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function getAccount(): Promise<Account> {
  return request<Account>('/account')
}

/** A session drifts from words or from a picture -- exactly one of them. */
export function startGeneration(
  source: { mood: string } | { pictureId: string },
  durationMinutes: number,
): Promise<GenerateResponse> {
  return request<GenerateResponse>('/generate', {
    method: 'POST',
    body: JSON.stringify({
      duration_minutes: durationMinutes,
      ...('mood' in source ? { mood: source.mood } : { picture_id: source.pictureId }),
    }),
  })
}

export type PictureStatus = 'PENDING' | 'DESCRIBING' | 'DESCRIBED' | 'FAILED'

export interface Picture {
  picture_id: string
  status: PictureStatus
  /** Present once DESCRIBED. */
  keywords: string[] | null
}

/** Ask for the vision step; the API starts it and the client polls. */
export function describePicture(pictureId: string): Promise<Picture> {
  return request<Picture>(`/pictures/${encodeURIComponent(pictureId)}/describe`, {
    method: 'POST',
  })
}

export function getPicture(pictureId: string): Promise<Picture> {
  return request<Picture>(`/pictures/${encodeURIComponent(pictureId)}`)
}

/**
 * Sleep that an AbortSignal can cut short. An abort that landed before the
 * call would otherwise be missed entirely -- addEventListener never fires on
 * an already-aborted signal -- so it is checked first.
 */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const t = setTimeout(resolve, ms)
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(t)
        reject(new DOMException('Aborted', 'AbortError'))
      },
      { once: true },
    )
  })
}

/**
 * Describe an upload and wait for the reading: resolves to the keywords once
 * the vision step has read the picture, throws if the attempt failed. The
 * API only starts a state machine (never a synchronous model call), so this
 * is a short poll with the same growing interval as pollJob.
 */
export async function describeUploadedPicture(
  pictureId: string,
  opts: {
    signal?: AbortSignal
    initialIntervalMs?: number
    maxIntervalMs?: number
    timeoutMs?: number
  } = {},
): Promise<string[]> {
  const { signal, initialIntervalMs = 1500, maxIntervalMs = 5000, timeoutMs = 90_000 } = opts
  const deadline = Date.now() + timeoutMs
  let interval = initialIntervalMs
  let picture = await describePicture(pictureId)
  while (picture.status === 'PENDING' || picture.status === 'DESCRIBING') {
    if (Date.now() + interval > deadline)
      throw new ApiError(408, 'Reading the picture timed out')
    await sleep(interval, signal)
    interval = Math.min(interval * 1.5, maxIntervalMs)
    picture = await getPicture(pictureId)
  }
  if (picture.status === 'FAILED' || !picture.keywords)
    throw new ApiError(422, 'The picture could not be read')
  return picture.keywords
}

export function createPictureUpload(): Promise<PictureUpload> {
  return request<PictureUpload>('/pictures/upload', { method: 'POST' })
}

/**
 * Upload a prepared JPEG straight to S3 and return the id to hand to
 * `startGeneration`. The bytes never touch the API: it only signs the policy,
 * and the policy is what S3 enforces (one key, one content type, a size cap).
 */
export async function uploadPicture(picture: Blob): Promise<string> {
  const upload = await createPictureUpload()

  const form = new FormData()
  for (const [name, value] of Object.entries(upload.fields)) form.append(name, value)
  // S3 ignores everything after the file part, so it goes last.
  form.append('file', picture)

  const response = await fetch(upload.url, { method: 'POST', body: form })
  if (!response.ok) throw new ApiError(response.status, 'Picture upload failed')
  return upload.picture_id
}

export function listDreamscapes(cursor?: string): Promise<DreamscapeList> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return request<DreamscapeList>(`/dreamscapes${query}`)
}

export function deleteDreamscape(jobId: string): Promise<void> {
  return request<void>(`/dreamscapes/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
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
const DEFAULT_JOB_TIMEOUT_MS = 35 * 60 * 1000 // pipeline_stack EXECUTION_TIMEOUT

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
    await sleep(interval, signal)
    interval = Math.min(interval * 1.5, maxIntervalMs)
  }
}
