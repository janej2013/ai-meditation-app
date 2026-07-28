# CLAUDE.md

## Project overview

AI Meditation App — a production web/PWA product for Australian users. Users describe how they feel; the system generates a personalised meditation script (LLM), synthesises it to speech (TTS), mixes background music, and delivers a streamable audio file. Freemium model: 1 free generation on signup, then paid credits/subscription via Stripe.

This is a real product AND a portfolio project demonstrating AWS + GenAI engineering. Code quality, IaC hygiene, and security practices matter as much as features.

## Tech stack (fixed — do not substitute)

- **IaC**: AWS CDK v2, Python. Region `ap-southeast-2` (Sydney). CloudFront ACM certs in `us-east-1`.
- **Backend API**: FastAPI + Mangum on Lambda (container image, built with Docker).
- **Pipeline**: AWS Step Functions (Standard) orchestrating small single-purpose zip Lambdas.
- **LLM**: Amazon Bedrock, Claude Haiku (use cross-region inference profile if the model is unavailable in ap-southeast-2).
- **TTS**: Volcano Engine TTS (primary) and Amazon Polly (fallback), behind a `TTSProvider` abstraction. Never call a TTS vendor SDK/API directly from business logic.
- **Data**: DynamoDB, single-table design, on-demand billing.
- **Auth**: Cognito User Pool, JWT authorizer on API Gateway HTTP API.
- **Payments**: Stripe Checkout + webhooks. Never build custom payment UI.
- **Frontend**: React + Vite, PWA (manifest + service worker), hosted on S3 + CloudFront.
- **Audio mixing**: ffmpeg (Lambda layer or in container image), pre-bundled royalty-free BGM tracks.
- **Python**: 3.12, type hints everywhere, Pydantic v2 models, `ruff` for lint/format, `pytest` for tests.

## Repository layout

```
infra/            CDK app. One stack per concern:
  stacks/
    data_stack.py       DynamoDB table + audio S3 bucket (+ lifecycle rules)
    auth_stack.py       Cognito User Pool + app client
    api_stack.py        HTTP API + JWT authorizer + API Lambda (container)
    pipeline_stack.py   Step Functions + step Lambdas
    billing_stack.py    Stripe webhook route + secrets wiring
    frontend_stack.py   S3 + CloudFront + Route53 + ACM (us-east-1 cert)
backend/
  api/              FastAPI app (Mangum handler), routers/, deps.py, Dockerfile
  functions/        One folder per Step Functions task Lambda (zip):
                    freeze_credit, generate_script, synthesize, mix_audio,
                    commit_credit, rollback_credit
  shared/           Shared package (Lambda layer): models.py, db.py, tts/
  tests/
frontend/           React + Vite PWA
.github/workflows/  CI/CD (OIDC role assumption, no long-lived AWS keys)
```

## Hard constraints (never violate)

1. **All credit/entitlement mutations go through `backend/shared/db.py`.** Freeze, commit, and rollback are DynamoDB conditional updates within a single user partition. Never write raw `update_item` calls for credits elsewhere. All three operations must be idempotent (safe to retry with the same `job_id`).
2. **Generation flow**: API Lambda only validates JWT + starts the Step Functions execution and returns a `job_id`. All heavy work (Bedrock, TTS, ffmpeg, S3 upload) happens inside the state machine. Never call Bedrock or TTS synchronously from the API Lambda.
3. **State machine failure handling**: every task has a `Catch` routing to `rollback_credit`. The external TTS call additionally gets `Retry` with exponential backoff (2–3 attempts) before falling through to `Catch`.
4. **Secrets** (Volcano TTS key, Stripe secret + webhook signing secret): Secrets Manager or SSM SecureString only. Never in code, `.env` committed files, plaintext Lambda env vars, or CDK context.
5. **Stripe webhooks must verify the signature** before any state change. Entitlement updates from webhooks must be idempotent (key on Stripe event id).
6. **Audio delivery**: CloudFront signed URLs to S3 objects. Never stream audio bytes through Lambda.
7. **No PII in prompts or logs.** The LLM prompt must instruct the model not to repeat user personal details verbatim in the script. Log `job_id`s and status, never user input text or generated scripts at INFO level.
8. **`cdk deploy` and any command that spends money or touches live AWS resources is human-only.** Claude may run `cdk synth`, `cdk diff`, `ruff`, `pytest`, and local builds.

## DynamoDB single-table conventions

- Table name from CDK output; env var `TABLE_NAME`.
- `PK = USER#<cognito_sub>`. Item types under a user partition via `SK`:
  - `SK = PROFILE`
  - `SK = ENTITLEMENT` — fields: `available` (int), `frozen` (int), `plan`, `period_end`
  - `SK = SUB#<stripe_subscription_id>`
  - `SK = JOB#<job_id>` — fields: `status` (PENDING | FROZEN | GENERATING | DONE | FAILED | ROLLED_BACK), `audio_key`, timestamps
- Freeze: `available >= 1` condition → `available -= 1, frozen += 1`.
- Commit: `frozen >= 1` condition → `frozen -= 1`.
- Rollback: condition on job status not already committed → `frozen -= 1, available += 1`.
- GSI only if a real access pattern requires it — propose before adding.

## Commands

- Lint/format: `ruff check . && ruff format --check .`
- Tests: `pytest backend/tests -q`
- Synth: `cd infra && cdk synth`
- Diff: `cd infra && cdk diff`
- Frontend dev: `cd frontend && npm run dev`

Run lint + tests + synth before declaring any backend/infra task complete.

## Milestones (work in this order; do not skip ahead)

1. `infra/` skeleton + `data_stack` (DynamoDB + audio bucket). Done when `cdk synth` passes.
2. `auth_stack` + `api_stack`: signup/login via Cognito, `GET /account` returns entitlement. Done when deployed manually and curl-able with a JWT.
3. `pipeline_stack`: full state machine with Polly as TTS placeholder. Done when a job runs end-to-end and produces a playable file in S3.
4. Volcano TTS provider implementation + provider routing config.
5. `billing_stack`: Stripe Checkout + webhook → entitlement update.
6. `frontend_stack` + PWA frontend.

## Style notes

- Small, single-purpose Lambdas in `functions/`; shared logic only in `shared/`.
- Pydantic models are the contract between steps; state machine payloads validate on entry to each Lambda.
- Prefer boring, explicit code over clever abstractions. This repo is read by hiring managers.
- Conventional commits (`feat:`, `fix:`, `infra:`, `docs:`).
- Update `README.md` architecture notes when stacks change.