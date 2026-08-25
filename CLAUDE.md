# CLAUDE.md

## Project overview

AI Meditation App — a production web/PWA product for Australian users. Users describe how they feel — optionally alongside a picture, which a vision step turns into mood keywords — and the system generates a personalised meditation script (LLM), synthesises it to speech (TTS), mixes background music, and delivers a streamable audio file. Freemium model: 1 free generation on signup, then paid credits/subscription via Stripe.

This is a real product AND a portfolio project demonstrating AWS + GenAI engineering. Code quality, IaC hygiene, and security practices matter as much as features.

## Tech stack (fixed — do not substitute)

- **IaC**: AWS CDK v2, Python. Region `ap-southeast-2` (Sydney). CloudFront ACM certs in `us-east-1`.
- **Backend API**: FastAPI + Mangum on Lambda (container image, built with Docker).
- **Pipeline**: AWS Step Functions (Standard) orchestrating small single-purpose zip Lambdas.
- **LLM / vision**: Amazon Bedrock, Amazon Nova Lite by default for both the script and the picture description, invoked on demand in ap-southeast-2 (bare model id, no cross-region profile, so user text and pictures stay in Sydney). Claude Haiku remains a supported override via `-c bedrock_model_id=`; use a cross-region inference profile only if a chosen model is unavailable in ap-southeast-2.
- **TTS**: Volcano Engine TTS (primary) and Amazon Polly (fallback), behind a `TTSProvider` abstraction. Never call a TTS vendor SDK/API directly from business logic.
- **Data**: DynamoDB, single-table design, on-demand billing.
- **Auth**: Cognito User Pool, JWT authorizer on API Gateway HTTP API.
- **Payments**: Stripe Checkout + webhooks. Never build custom payment UI.
- **Frontend**: React + Vite, PWA (manifest + service worker), hosted on S3 + CloudFront.
- **Audio mixing**: in the browser, via the Web Audio API. The pipeline delivers narration only; the PWA mixes a user-selectable BGM track under it at playback time, so the listener can switch tracks or change the music volume mid-session. BGM is licensed from Pixabay and lives under `assets/` on the audio bucket, uploaded by hand via `make upload-bgm` (the licence forbids redistributing the files, so they are not in git and not in the CDK asset; only the CI probe `silence.mp3` ships with a deploy). `backend/functions/mix_audio/` still holds a server-side ffmpeg mixer for a future download/share feature — it is kept green by its unit tests but is **not deployed**; see README.
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
                    freeze_credit, generate_script, synthesize,
                    commit_credit, rollback_credit (generation chain);
                    describe_picture (picture machine, pre-job)
                    mix_audio/ is retained but NOT deployed (browser mixes
                    instead); init_user/ is a Cognito trigger, not a task
  shared/           Shared package (Lambda layer): models.py, db.py, tts/
  tests/
frontend/           React + Vite PWA
.github/workflows/  CI/CD (OIDC role assumption, no long-lived AWS keys)
```

## Hard constraints (never violate)

1. **All credit/entitlement mutations go through `backend/shared/db.py`.** Freeze, commit, and rollback are DynamoDB conditional updates within a single user partition. Never write raw `update_item` calls for credits elsewhere. All three operations must be idempotent (safe to retry with the same `job_id`).
2. **Generation flow**: API Lambda only validates JWT + starts a Step Functions execution and returns an id. All heavy work (Bedrock, TTS, S3 upload) happens inside a state machine — the generation chain for a job, the one-task picture machine for reading an upload before any job exists. Never call Bedrock or TTS synchronously from the API Lambda. A picture session takes no mood text: the picture is the whole brief, and it is described (client polls `GET /pictures/{id}`) before Begin, which is the first moment a credit is frozen; every picture route requires a credit in hand because that read is uncompensated spend.
3. **State machine failure handling**: every task has a `Catch` routing to `rollback_credit`. The external TTS call additionally gets `Retry` with exponential backoff (2–3 attempts) before falling through to `Catch`.
4. **Secrets** (Volcano TTS key, Stripe secret + webhook signing secret): Secrets Manager or SSM SecureString only. Never in code, `.env` committed files, plaintext Lambda env vars, or CDK context.
5. **Stripe webhooks must verify the signature** before any state change. Entitlement updates from webhooks must be idempotent (key on Stripe event id).
6. **Audio delivery**: CloudFront signed URLs to S3 objects. Never stream audio bytes through Lambda. Applies to per-job narration under `jobs/` and uploaded pictures under `pictures/` (re-sampled into the cloud on a revisit); the shared BGM under `assets/` carries no user content and is served as ordinary cached CloudFront objects so the browser can switch tracks without a round trip for a new signature.
7. **No PII in prompts or logs.** The LLM prompt must instruct the model not to repeat user personal details verbatim in the script; the vision prompt must not describe people or transcribe text in the picture. Keywords and summaries derived from a user's picture are user content: they live on the JOB item like `mood_text`, never in the state machine payload, and never in INFO logs. Log `job_id`s, status and counts only.
8. **`cdk deploy` and any command that spends money or touches live AWS resources is human-only.** Claude may run `cdk synth`, `cdk diff`, `ruff`, `pytest`, and local builds.
9. **Uploaded pictures are written only under `pictures/<cognito_sub>/` via a presigned S3 POST that fixes key, content type and size.** They are kept (a future replay variant may re-weave them) and expire by the bucket's lifecycle rule alone — no business code deletes a user's object, and no pipeline or API Lambda holds `s3:DeleteObject` on them. (Deployment custodians are the exception: dev's `auto_delete_objects` on stack destroy, and the BucketDeployment handler's default grant.)

## DynamoDB single-table conventions

- Table name from CDK output; env var `TABLE_NAME`.
- `PK = USER#<cognito_sub>`. Item types under a user partition via `SK`:
  - `SK = PROFILE`
  - `SK = ENTITLEMENT` — fields: `available` (int), `frozen` (int), `plan`, `period_end`
  - `SK = SUB#<stripe_subscription_id>`
  - `SK = PICTURE#<picture_id>` — fields: `status` (PENDING | DESCRIBED | FAILED), `keywords`, `summary`, `expires_at` (TTL); written by `POST /pictures/upload` and the picture state machine, before any job exists
  - `SK = JOB#<job_id>` — fields: `status` (PENDING | FROZEN | GENERATING | DONE | FAILED | ROLLED_BACK | DELETED), `audio_key`, `mood_text` (words jobs) or `picture_key` / `picture_keywords` / `picture_summary` (picture jobs), timestamps
  - `SK = AGENT#<session_id>` — companion session header: `status` (ACTIVE | FINALIZED | ABANDONED | FAILED), `turn` (committed turns; the fencing token), `engine`, `in_flight`, `usage_*`, `expires_at` (TTL)
  - `SK = AGENT#<session_id>#T<nnnn>` — one checkpoint per turn: the user text, the assistant content and every tool round in Converse wire form. User content: never logged
  - `SK = MEMORY` — insights the agent saved across sessions. User content; the user can view and clear it
  - `SK = AGENTQUOTA#<yyyy-mm>` — monthly companion-session counter, `expires_at` (TTL)
- Freeze: `available >= 1` condition → `available -= 1, frozen += 1`.
- Commit: `frozen >= 1` condition → `frozen -= 1`.
- Rollback: condition on job status not already committed → `frozen -= 1, available += 1`.
- GSI only if a real access pattern requires it — propose before adding.

## Commands

**Run everything from WSL (Ubuntu), never from Windows PowerShell or cmd.** The
Lambda runtime, the layer build (`scripts/build_layers.sh`), the Docker image
builds and the CDK CLI all assume a Linux toolchain; running them from Windows
produces layers and images that fail at runtime. The repo lives at
`/mnt/d/Jane/Project/meditation` inside WSL, and the virtualenv there is the one
`ruff`, `pytest` and `cdk` must come from.

- Lint/format: `ruff check . && ruff format --check .`
- Tests: `pytest` (covers `backend/tests` and `infra/tests`; the CDK tests skip
  themselves when `node` is not on PATH)
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