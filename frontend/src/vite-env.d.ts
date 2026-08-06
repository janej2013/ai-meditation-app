/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the HTTP API (no trailing slash). */
  readonly VITE_API_URL: string
  /** Cognito user pool id (ap-southeast-2_...). */
  readonly VITE_COGNITO_USER_POOL_ID: string
  /** Cognito SPA app client id. */
  readonly VITE_COGNITO_CLIENT_ID: string
  /** CloudFront domain serving jobs/* (signed) and assets/* (BGM). */
  readonly VITE_AUDIO_DOMAIN: string
  /** Pipeline execution timeout in ms, from the Pipeline stack's JobTimeoutMs. */
  readonly VITE_JOB_TIMEOUT_MS: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
