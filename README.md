# AI Meditation App

Users describe how they feel; the system generates a personalised meditation script with an LLM,
synthesises it to speech, and delivers a streamable narration that the PWA mixes background music
under — switchable mid-session. Freemium: one free generation on signup, then paid credits via
Stripe.

Region `ap-southeast-2` (Sydney). See [CLAUDE.md](CLAUDE.md) for the full stack and constraints.

## Current status

All six milestones complete: signup to playback runs end to end — Cognito
auth, Bedrock scripts, Volcano/Polly narration, Stripe billing, and a React
PWA that mixes background music in the browser.

| Milestone | Scope | Status |
|---|---|---|
| 1 | `infra/` skeleton + `data_stack` | done |
| 2 | `auth_stack` + `api_stack` | done |
| 3 | `pipeline_stack` (Polly placeholder) | done |
| 4 | Volcano TTS provider | done |
| 5 | `billing_stack` (Stripe) | done |
| 6 | `frontend_stack` + PWA | done |

## Known gaps

Deliberate deferrals, recorded so they read as decisions rather than oversights.

- **The CDK CloudFormation execution role holds `AdministratorAccess`.** That is what
  `cdk bootstrap` attaches when `--cloudformation-execution-policies` is not given. Scoping it
  down means enumerating every permission this project actually needs — DynamoDB, S3, Lambda,
  Cognito, API Gateway, Step Functions, Bedrock, Polly, Secrets Manager — and that list keeps
  moving while stacks are still being added. Planned for after milestone 5, by re-running
  `cdk bootstrap --cloudformation-execution-policies <scoped-policy-arn>`.

- **Webhook field locations are read defensively, but remain unverified against the account's
  pinned API version.** Stripe has moved the invoice's subscription reference (root →
  `parent.subscription_details.subscription` in 2025-03-31.basil) and the subscription-metadata
  copy (`invoice.metadata` never had it; `subscription_details.metadata` from 2022-11, then under
  `parent`). The handlers now read every known location, with tests for each shape — the failure
  mode being guarded against is the worst a billing bug can take: a single-location read answers
  Stripe `200` while **every renewal silently stops granting credits**, so Stripe never retries and
  nothing alerts. What remains is empirical: drive a renewal through *Testing webhooks locally*
  below against the account's real pinned version and assert the entitlement moved; a `200` on its
  own proves nothing here.

- **A first subscription payment leaves `period_end` unset until the first renewal.** Real
  checkout sessions carry no `current_period_end`, and the opening invoice is deliberately skipped
  (the session is what grants, see `_handle_invoice_paid`). Correctness is unaffected — credits and
  plan land — but a "renews on …" display in milestone 6 would have nothing to show for the first
  period. Either fetch the subscription once in the checkout handler, or accept the gap; decide
  with the frontend.

- **The audio bucket grants read to *any* CloudFront distribution in the account, not just its
  own.** Compare the two policies a deploy writes: `SiteBucket` gets `StringEquals` on
  `distribution/${SiteDistribution}`, while `AudioBucket` gets `StringLike` on `distribution/*`.
  The asymmetry is structural, not sloppiness — the audio bucket belongs to the data stack, so the
  frontend stack can only import it, and handing the real construct to `S3BucketOrigin` would make
  CDK write a distribution-specific policy into a stack the distribution already reads from. CDK
  rejects that cycle, so the grant is written in `data_stack` against a distribution ARN it cannot
  know. The `Cannot update bucket policy of an imported bucket` warning on every synth is this.

  What it costs: anyone who can create a CloudFront distribution in this account can point one at
  the audio bucket and read `jobs/*` without a signature. That needs CloudFront-create permission
  already, so it is not an external exposure — but it does mean the signed-URL requirement rests on
  the distribution's key group alone, with no second line behind it. **It compounds with deploying
  without `-c audio_public_key_pem`**: no key group means `jobs/*` is already unsigned on the
  intended distribution, and this policy means a second one would work too.

  Closing it properly means co-locating the bucket and its distribution in one stack, which trades
  the wildcard for a weaker separation of concerns. Worth revisiting if the audio bucket ever holds
  anything a leak would be expensive.

## Architecture — data layer

`infra/stacks/data_stack.py` provisions the two persistent resources:

**DynamoDB `AppTable`** — single-table design, on-demand billing, AWS-managed encryption.
Point-in-time recovery is enabled in prod only: dev data is disposable and recreated by
redeploying, so the backup charge buys nothing there. No GSI; one will be added only when a real
access pattern needs it.

```
PK = USER#<cognito_sub>
  SK = PROFILE
  SK = ENTITLEMENT          available, frozen, plan, period_end
  SK = SUB#<stripe_subscription_id>
  SK = JOB#<job_id>         status, audio_key, created_at, updated_at
```

**S3 `AudioBucket`** — generated audio. All public access blocked, SSE-S3, TLS enforced, objects
expire after 90 days. The bucket stays private and audio bytes never stream through Lambda:
delivery is a CloudFront signed URL for `jobs/*` and plain cached CloudFront objects for
`assets/*` — see *frontend delivery* below. CORS lives on the distribution's response headers
policy, not the bucket.

Both resources use `RemovalPolicy.DESTROY` in dev and `RETAIN` in prod, selected by the `env`
context value. Stacks are named `Meditation-<env>-<Concern>` so dev and prod coexist in one account.

## The credit ledger

`backend/shared/db.py` is the only place credits are mutated. Freeze, commit and rollback are each
a single `TransactWriteItems` with exactly two `Update` items, always in this order:

| index | item | role |
|---|---|---|
| 0 | `SK = ENTITLEMENT` | moves the counters |
| 1 | `SK = JOB#<job_id>` | advances job status — **the idempotency guard** |

| operation | entitlement condition → update | job condition → update |
|---|---|---|
| freeze | `available >= 1` → `available -1, frozen +1` | `attribute_not_exists(PK) OR status = PENDING` → `FROZEN` |
| commit | `frozen >= 1` → `frozen -1` | `status IN (FROZEN, GENERATING)` → `DONE` |
| rollback | `frozen >= 1` → `frozen -1, available +1` | `status IN (FROZEN, GENERATING, FAILED)` → `ROLLED_BACK` |

Step Functions retries tasks and routes every failure to `rollback_credit`, so all three run more
than once in production. On a replay the job has already moved past the transition, its condition
fails, the whole transaction is cancelled, and the counters are untouched — the call returns
`applied=False` rather than raising.

Two details are load-bearing:

- **`CancellationReasons` is read job-first.** When both conditions fail the reasons are
  `[ConditionalCheckFailed, ConditionalCheckFailed]`. Checking the entitlement reason first would
  report a legitimate retry by a user who has since spent down to zero as `InsufficientCreditsError`.
- **Rollback's status allow-list excludes `DONE` and `PENDING`.** A consumed credit is never
  refunded, and a job that never froze anything can't drive `frozen` negative — which matters
  because every task including `freeze_credit` itself catches to `rollback_credit`.

## Architecture — auth and API

`infra/stacks/auth_stack.py` — Cognito user pool: email sign-in alias, self-signup, email
verification by code, `EMAIL_ONLY` recovery, password minimum 8 with upper/lower/digit but **no
symbol requirement** (length is the lever that matters; symbols mostly add friction). The SPA app
client has no secret and enables SRP always, plus `USER_PASSWORD_AUTH` **in dev only** so the API
can be exercised with curl.

`infra/stacks/api_stack.py` — HTTP API with a Cognito JWT authorizer:

```
issuer   https://cognito-idp.ap-southeast-2.amazonaws.com/<user-pool-id>
audience <app-client-id>
identity $request.header.Authorization
```

The authorizer is attached as `default_authorizer`, and `GET /health` opts out explicitly with
`HttpNoneAuthorizer`. That direction matters: a route added later is protected by omission rather
than exposed by it.

**The API requires ID tokens, not access tokens.** Cognito access tokens carry neither an `aud`
claim nor `email`, so `api/deps.py` rejects anything whose `token_use` is not `id`. `scripts/
get_token.py` prints the ID token by default for this reason.

Claims reach the application without any token parsing: API Gateway validates signature, issuer,
audience and expiry before the Lambda runs, Mangum exposes the raw event as `scope["aws.event"]`,
and `deps.py` reads `requestContext.authorizer.jwt.claims`. Re-verifying in FastAPI would duplicate
the authorizer while being easier to get wrong.

### Free credit on signup

A Cognito post-confirmation trigger (`backend/functions/init_user/`) creates the user's `PROFILE`
and `ENTITLEMENT` items with `available=1`. The write itself lives in
`EntitlementStore.initialize_user`, not in the handler, because creating `ENTITLEMENT` is an
entitlement mutation and constraint 1 routes those through `shared/db.py`.

Two independent conditional puts, each guarded by `attribute_not_exists(PK)` and each swallowing
its own `ConditionalCheckFailedException` — Cognito can invoke a trigger more than once. They are
deliberately *not* a transaction: if an earlier partial write left `PROFILE` present but
`ENTITLEMENT` missing, a transaction would fail as a whole and never repair the gap, while
independent puts heal it.

The trigger never fails a signup. Any DynamoDB error is logged and swallowed, which trades a
guaranteed entitlement for a guaranteed signup. `GET /account` lazily calls the same idempotent
`initialize_user` and is the compensating control that closes the gap.

### Container images

Both Lambdas are container images built from `backend/` as the Docker context, with layers ordered
least- to most-volatile (dependencies → `shared/` → app code) so editing a router rebuilds only the
final `COPY`. `init_user` gets its own slim image without FastAPI because it sits in the signup
path where cold start is felt.

`cdk synth` does **not** require Docker — verified by synthesizing with `CDK_DOCKER=/bin/false`.
Images are built at `cdk deploy`.

## Architecture — the generation pipeline

`infra/stacks/pipeline_stack.py` builds a Standard state machine over five small
zip Lambdas:

```
FreezeCredit ──► GenerateScript ──► Synthesize ──► CommitCredit ──► Succeed
     │                  │                │              │
     │ InsufficientCreditsError          └──────────────┤ States.ALL
     ▼                                                  ▼
InsufficientCredits (Fail)                    RollbackCredit ──► GenerationFailed (Fail)
   no refund — nothing was frozen
```

Execution timeout 10 minutes; per-state timeouts 30 s / 120 s / 180 s / 30 s.

There is no mix step — see [Mixing happens in the browser](#mixing-happens-in-the-browser).

**Retries name concrete exception classes, never `States.ALL`.** `generate_script`
retries `BedrockTransientError`, `synthesize` retries `TTSTransientError`, and
both add the four Lambda transport errors — 3 attempts, 2 s base, ×2 backoff. A
Pydantic `ValidationError` or a Bedrock `ValidationException` is permanent, so it
falls straight to `Catch` instead of burning three attempts on something that
cannot succeed.

`LambdaInvoke` is constructed with `retry_on_service_exceptions=False`. CDK
otherwise prepends its own transport-error retry with `maxAttempts: 6`, and
Step Functions applies the *first* matching rule — so that default would
silently override the explicit 3-attempt policy.

### Where DONE is written

`commit_credit` writes `status=DONE`, in the same `TransactWriteItems` that
decrements `frozen`. `synthesize` writes only `audio_key`.

This is not a style choice. `commit_credit`'s job condition is
`status IN (FROZEN, GENERATING)`; if `synthesize` had already set DONE, that
condition would fail, the whole transaction would cancel, commit would take its
replay path — and **the credit would stay frozen forever**. Writing DONE inside
the commit transaction is what makes "credit consumed" and "job finished" one
atomic fact.

Likewise `rollback_credit` writes `ROLLED_BACK`, not `FAILED`: `FAILED` is
inside rollback's own allow-list, so writing it would let a retried rollback
pass its own condition and **refund twice**. `GET /jobs/{id}` maps `ROLLED_BACK`
to `"failed"` for clients — the internal status keeps the extra fact that the
credit was returned.

### Keeping user text out of the execution history

Step Functions retains execution input for 90 days and shows it in the console,
so constraint 7 rules it out for user input. The state machine payload carries
only ids and S3 keys:

```python
class PipelineState(BaseModel):
    user_id: str
    job_id: str
    duration_minutes: int
    script_key: str | None  # set by generate_script
    narration_key: str | None  # set by synthesize — what the file is
    audio_key: str | None  # set by synthesize — what the client plays
```

`POST /generate` writes `mood_text` onto the JOB item and `generate_script`
reads it back from DynamoDB. The generated script goes to S3 for the same
reason. Handlers log lengths and ids, never content.

### TTS providers — Volcano primary, Polly fallback

`shared/tts/` exposes `TTSProvider` (a `Protocol`) and a neutral `VoiceConfig`;
`get_provider()` reads `TTS_PROVIDER` and returns one of:

| provider | role | module |
|---|---|---|
| `volcano` | **primary** — Volcano Engine (Doubao) Seed-TTS 2.0 | `shared/tts/volcano.py` |
| `polly` | fallback | `shared/tts/polly.py` |

Falling back is a context change, not a code change:

```bash
cd infra && npm run cdk -- deploy -c tts_provider=polly
```

Both providers keep their IAM grants in either configuration, so switching back
and forth never needs an IAM redeploy. Vendor modules are imported lazily inside
the factory, so a Polly-only deployment never touches Secrets Manager.

Each vendor gets its own chunk size — `MAX_POLLY_CHARS` (2500, under Polly's
3000 billed-character cap) and `MAX_VOLCANO_CHARS` (1000, conservative against
a streaming endpoint the vendor documents no hard limit for). Splitting on
paragraph boundaries does double duty: the prompt puts a blank line exactly
where the listener should pause, so every seam in the concatenated MP3 lands on
an intended silence rather than mid-phrase.

**Three things about the Volcano contract are easy to get wrong**, and each has
a test because each fails quietly:

- `additions` must be a JSON **string**, not a nested object — the server
  otherwise ignores it and the context-prompt tuning silently stops applying.
- **HTTP 200 does not mean success.** Failures (bad credentials, unknown voice,
  resource not enabled) arrive as a non-zero `code` *inside* a 200 body, so the
  newline-delimited stream is checked line by line.
- `code` **20000000 is the end-of-stream marker**, not an error.

Voice ids beginning `S_` are cloned voices and route to the `volcano_icl`
cluster; everything else uses `volcano_tts`. The default is the integration
doc's English preset with the
meditation style prompt translated to English. No `emotion`/`emotion_scale` is
sent by default — the delivery direction is the context prompt's job; the pair
is an opt-in override via `VolcanoTuning` (used by `scripts/tts_preview.py`).

Transport is `urllib3` — botocore already vendors it, so the shared layer gains
no dependency. `retries=False`: the state machine owns retry policy, and a
second layer underneath it would multiply against those three attempts. HTTP 429
and 5xx and every transport failure become `TTSTransientError` (which the
`Synthesize` state retries); in-stream error codes and other 4xx become
`TTSError`, which goes straight to `Catch`.

#### Credentials

Constraint 4 forbids plaintext Lambda environment variables for vendor keys, so
only an ARN is injected as `VOLCANO_SECRET_ARN` and the provider reads the value
through Secrets Manager once per container.

**Create the secret by hand before deploying** — CDK references it with
`Secret.from_secret_name_v2` and deliberately does not generate it, because a
generated value would land in the CloudFormation template and in `cdk diff`
output:

```bash
aws secretsmanager create-secret \
  --name meditation/volcano-tts \
  --region ap-southeast-2 \
  --secret-string '{"api_key":"<Access Token>","app_id":"<App ID>"}'
```

`api_key` and `app_id` are both required. The vendor doc calls the App Id
header optional, but seed-tts-2.0 rejects requests without it (HTTP 400, code
45000000), so the provider refuses to load a secret missing either field.
Override the name with `-c volcano_secret_name=...`. Only
the synthesize Lambda is granted `secretsmanager:GetSecretValue`, and only on
that one secret — asserted in `infra/tests/test_volcano_secret.py`.

### Mixing happens in the browser

The pipeline ships **narration only**. The PWA fetches a BGM track from
`assets/` and mixes it under the narration with the Web Audio API at playback
time, routing each source through its own `GainNode`.

The deciding requirement is that a listener can **switch background music
mid-session**. A server-side mix bakes one track into one file, so every switch
would mean re-rendering and re-downloading — there is no server-side design that
satisfies this. Client-side mixing also gives the listener an independent music
volume and the option of no music at all.

What it buys structurally:

| | server-side mix | browser mix |
|---|---|---|
| Pipeline | 5 states, ffmpeg Lambda at 1 GB + 1 GB `/tmp` | 4 states, no ffmpeg |
| Lambda layers | shared + ffmpeg (~80 MB binary) | shared only |
| Stored per job | narration **and** mixed output | narration |
| BGM copies | baked into every generated file | one CDN object, shared by all users |
| Delivery | one signed URL | signed URL for narration; BGM is public, cacheable |

BGM under `assets/` carries no user content, so it is served as an ordinary
cached CloudFront object — the browser can switch tracks without a round trip
for a fresh signature. Per-job narration under `jobs/` still requires a signed
URL (constraint 6).

**Verify before relying on this:** on iOS Safari an `AudioContext` is suspended
when the page is backgrounded, and listening with the screen off is the core use
case for a meditation app. Anchoring each source in a real `<audio>` element via
`createMediaElementSource` (rather than `AudioBufferSourceNode`) keeps the media
session alive, but this needs testing on a physical device before the frontend
commits to it.

#### The retained server-side mixer

`backend/functions/mix_audio/` still holds the ffmpeg mixer, for a possible
"download this session" or share feature — the one thing browser mixing cannot
do, since there is no single artefact to hand off. Nothing deploys it:
`pipeline_stack.py` has no `MixAudio` state, no ffmpeg layer, and
`scripts/build_layers.sh` no longer builds the binary by default.

It is kept honest by its unit tests rather than by a dormant CDK branch. The
tests stub `_run` and `probe_duration`, so they need no ffmpeg binary and run in
CI like everything else; a CDK path that is never synthesized would rot silently
and give false confidence. Re-enabling means re-adding the wiring (recoverable
from git history) and running `scripts/build_layers.sh ffmpeg`.

The filter graph it builds:

```bash
ffmpeg -y -i narration.mp3 -stream_loop -1 -i bgm.mp3 \
  -filter_complex "\
    [0:a]apad=pad_dur=5[voice]; \
    [1:a]volume=0.2,afade=t=in:st=0:d=4,afade=t=out:st=${TAIL}:d=4[bg]; \
    [voice][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]" \
  -map "[out]" -c:a libmp3lame -b:a 128k -ar 44100 final.mp3
```

`normalize=0` is load-bearing: `amix` divides by input count by default, which
would silently halve the narration. With normalization off the inputs are
summed, so `alimiter` catches the clipping that can follow. The same volume,
fade and tail values are what the browser mixer should reproduce.

### Bedrock model id

Claude Haiku is not available directly in `ap-southeast-2`, so the default is
the APAC cross-region inference profile. **Availability is per-account** —
confirm yours and override if it differs:

```bash
aws bedrock list-inference-profiles --region ap-southeast-2
cd infra && npm run synth -- -c bedrock_model_id=<your-profile-id>
```

A cross-region profile needs `bedrock:InvokeModel` on **both** the profile ARN
and the foundation-model ARN in every region the profile can route to; granting
only the profile fails at runtime with an opaque `AccessDenied`.

## Architecture — billing

`infra/stacks/billing_stack.py` owns no Lambda and no HTTP API. It hangs two
routes off the API stack's existing integration and grants that stack's
function read access to the Stripe secret:

| route | auth | why |
|---|---|---|
| `POST /billing/checkout` | Cognito JWT (the API's default authorizer) | a signed-in user starting a purchase |
| `POST /billing/webhook` | **none** — `HttpNoneAuthorizer` | Stripe's servers cannot hold a Cognito token |

Checkout is a **hosted Stripe page**. There is no custom payment UI anywhere in
this project and card details never reach us — the endpoint creates a Checkout
Session and returns its URL.

The client names a **product key** (`pack_10`), never a price id. The catalogue
in `backend/api/products.py` resolves the key to a Stripe price, so a client
cannot name an arbitrary price and buy something the catalogue does not offer.
Price ids are not secrets, so they live in code and can be overridden with the
`STRIPE_PRODUCTS` env var once the real Stripe products exist.

### The webhook is anonymous; the signature is the authentication

Constraint 5: `stripe.Webhook.construct_event` verifies the raw body against
the `Stripe-Signature` header **before any state change**, and a failure returns
400 having written nothing. An attacker who POSTs without the signing secret can
reach the Lambda but cannot move a credit — covered by tests that forge a
signature, tamper with a signed body, and replay a stale timestamp.

The handler is `async` because the signature is computed over the *raw* bytes:
re-serialising a parsed payload would change them and break the MAC.

### Idempotency

Stripe retries any delivery that is not 2xx, so the same event arrives more than
once. Each entitlement change is a single `TransactWriteItems` carrying:

| index | item | role |
|---|---|---|
| 0 | `SK = ENTITLEMENT` | moves the balance / plan |
| 1 | `SK = EVENT#<stripe_event_id>` | `attribute_not_exists(PK)` — **the dedupe guard** |
| 2 | `SK = SUB#<subscription_id>` | subscription record (subscription events only) |

A redelivered event finds the marker already written, the transaction cancels,
and `applied=False` comes back as a **success** — the webhook returns 200 so
Stripe stops retrying. This is the same shape as the freeze/commit/rollback
transactions, with the event marker where the job status guard sits.

Markers carry `expires_at` and the table's TTL reaps them after 30 days (Stripe
retries for ~3). Only these items carry the attribute, so nothing else expires.

| Stripe event | effect |
|---|---|
| `checkout.session.completed` | credit pack → `available += N`; subscription → set plan + `period_end`, grant the period, write `SUB#` |
| `invoice.paid` | renewal → advance `period_end`, grant the new period |
| `customer.subscription.deleted` | back to `free`, clear `period_end`, **leave `available` alone** — paid-for credits stay spendable |

Anything else is logged and acknowledged with 200. An unknown price id is a
`WARNING` and a 200, so Stripe stops retrying an event this deployment can never
act on. Logs carry the event id, type and outcome — never the event body, which
holds customer email and billing details (constraint 7).

`stripe` is an **API-only dependency**. `shared/db.py` never imports it: the
router pulls plain values out of the event and hands those over, so the step
Lambdas' layer stays vendor-free.

### Stripe credentials

Create the secret by hand before deploying — CDK references it with
`Secret.from_secret_name_v2` and deliberately does not generate it, because a
generated value would land in the template and in `cdk diff` output
(constraint 4):

```bash
aws secretsmanager create-secret \
  --name meditation/stripe \
  --region ap-southeast-2 \
  --secret-string '{"secret_key":"sk_live_...","webhook_secret":"whsec_..."}'
```

Both fields are required — without `webhook_secret` the webhook cannot verify a
signature, and constraint 5 forbids acting on an unverified event. Override the
name with `-c stripe_secret_name=...`. Only `STRIPE_SECRET_ARN` is injected into
the Lambda; the values are read through Secrets Manager once per container.

### Testing webhooks locally

The Stripe CLI forwards real events to a local server, so you can exercise the
handler without deploying:

```bash
stripe login
stripe listen --forward-to http://localhost:8000/billing/webhook
# prints a whsec_... — put it in the local secret the app reads

stripe trigger checkout.session.completed
stripe trigger invoice.paid
stripe trigger customer.subscription.deleted
```

`stripe listen` mints its **own** signing secret, distinct from the one in the
dashboard for a deployed endpoint. Against the deployed API, register
`<ApiUrl>/billing/webhook` in the Stripe dashboard and use the signing secret it
issues there.

### Before deploying

```bash
scripts/build_layers.sh      # shared package layer (default)
scripts/build_layers.sh ffmpeg   # only when working on the retained mix_audio
```

The layer directories are gitignored. `cdk synth` succeeds without the shared
layer and emits a warning; a deploy without it would fail at runtime. The ffmpeg
layer is not deployed — see [layers/README.md](layers/README.md) for its source.

## Architecture — frontend delivery and the PWA

`infra/stacks/frontend_stack.py` owns what the browser fetches directly:

**The site.** A private S3 bucket (all public access blocked, no website
endpoint) behind CloudFront with OAC. SPA routing maps 403 *and* 404 to
`/index.html` — 403 matters because OAC deliberately withholds `ListBucket`,
so S3 answers missing keys with AccessDenied. A custom domain is optional
context (`-c domain_name=... -c hosted_zone_id=...`); the ACM certificate is
created in us-east-1 via `cross_region_references=True`, and without a domain
the stack serves on the CloudFront default domain so synth never needs a real
hosted zone.

**Audio (constraint 6).** One distribution over the audio bucket, two
behaviours with opposite rules:

| path | access | why |
|---|---|---|
| `jobs/*` | **signed URL** (trusted key group) | one user's narration |
| `assets/*` | public, cached | shared BGM — the player switches tracks mid-session without a round trip |

`GET /jobs/{id}` now mints CloudFront signed URLs (`api/cloudfront_signer.py`,
RSA-SHA1 canned policy, 15-minute expiry) instead of S3 presigning; the API
Lambda's `s3:GetObject` grant is gone. The signing key pair is operator-made:

```bash
openssl genrsa -out cf-signing.pem 2048
openssl rsa -in cf-signing.pem -pubout -out cf-signing.pub.pem

aws secretsmanager create-secret --name meditation/cloudfront-signing-key   --region ap-southeast-2 --secret-string file://cf-signing.pem

cd infra && npm run cdk -- deploy -c audio_public_key_pem="$(cat ../cf-signing.pub.pem)"
```

Only the private key's ARN reaches the Lambda; the public half becomes a
CloudFront `PublicKey` + `KeyGroup`. Without the context value the stack still
synthesises — `jobs/*` simply has no key group until the key is wired.

**One CDK trap, documented in `data_stack.py`:** the OAC bucket policy must
live in the *data* stack and name `distribution/*` rather than the specific
distribution ARN — pinning it would make Data and Frontend reference each
other, which CDK rejects as a cycle.

### The PWA (`frontend/`)

React + Vite + TypeScript; pages/components/api/auth/audio split. Visuals and
flow follow the Claude Design prototype (warm dark oklch palette, DM Sans/DM
Mono — see `src/styles/tokens.css`).

- **Auth**: `amazon-cognito-identity-js` (SRP; no Amplify). Sign-up with the
  emailed six-digit code, sign-in, sign-out. Every API call carries the **ID
  token** — the authorizer checks `aud` and the backend enforces
  `token_use=id`, so the access token would 401. `getSession()` refreshes
  expired tokens from the stored refresh token transparently.
- **Flow**: home (mood + duration → `POST /generate`; 402 routes to plans,
  429 shows "already in progress") → generating (poll `GET /jobs/{id}`,
  2s→10s backoff, 10-minute cap matching the state machine timeout) → player
  on DONE / refund screen on FAILED.
- **Playback**: Web Audio dual-track mix (`src/audio/mixer.ts`) — narration
  through one GainNode, looped BGM through another at 20%. Switching BGM
  replaces only the BGM source; the narration never stops. Both fetches are
  CORS (`mode: 'cors'`), which the audio distribution's response headers
  policy exists to satisfy.
- **Billing**: plans page → `POST /billing/checkout` → redirect to the
  Stripe-hosted page. `/billing/success` lands on the account page and
  re-fetches the balance.
- **PWA**: manifest + `vite-plugin-pwa`. Precache: app shell; CacheFirst:
  `assets/bgm/*` (immutable, small). **NetworkOnly, deliberately**: all API
  paths and `jobs/*` — signed URLs expire and credit/job state must be live,
  so caching either would serve dead links or stale balances.

```bash
cd frontend
cp .env.example .env.local   # fill from cdk-outputs.json
npm install
npm run dev                  # or: test / lint / format:check / build
```

Deploying the built app is `aws s3 sync dist/ s3://<SiteBucketName>` plus a
CloudFront invalidation — both human-run, like every deploy in this repo.

### Housekeeping

Two costs grow quietly with every deploy and are bounded by convention, not by
the templates:

- **ECR image assets.** Each deploy pushes new digests of the two container
  images into the CDK bootstrap repository and nothing deletes the old ones.
  `cd infra && npm run gc` (CDK's `gc --unstable=gc`) deletes assets no stack
  references any more. It touches live resources, so it is human-run, like
  every deploy.
- **Lambda log groups.** Every function now declares its own log group with
  one-month retention (the implicit default is *never expire*). If a new
  function is added, give it a log group — `test_cost_hygiene.py` guards the
  existing ones.

## Local setup

**Run every command from WSL (Ubuntu), never from Windows PowerShell or cmd.**
Lambda runs Linux, the layer build and the API container image are Linux
artefacts, and the CDK CLI resolves the Python app through the Linux
virtualenv — building any of it from Windows produces output that fails at
runtime.

```bash
proto install node                       # 22.21.0, pinned in .prototools; nvm/fnm work too

python3 -m venv ~/.venvs/meditation
~/.venvs/meditation/bin/pip install -e "backend[dev]" ruff -r infra/requirements.txt

cd infra && npm install                  # pins the CDK CLI
```

Keep the virtualenv outside the checkout when the repo sits on a Windows drive under WSL
(`/mnt/d/...`): the 9p mount makes an in-repo `.venv` markedly slower.

## Commands

```bash
source ~/.venvs/meditation/bin/activate

ruff check . && ruff format --check .   # lint/format
pytest                                  # backend/tests + infra/tests
cd infra && npm run synth                       # dev (default)
cd infra && npm run synth -- -c env=prod -c allowed_origins=https://app.example.com
cd infra && npm run diff
```

`allowed_origins` is required for `env=prod` and synth fails without it — falling back to a
localhost CORS origin in a real deployment would be worse than a loud error. Dev defaults to
`http://localhost:5173` (Vite).

### Calling the deployed API

```bash
cd infra && npm run cdk -- deploy --all --outputs-file ../cdk-outputs.json   # human-only

export API_URL=$(python -c "import json;d=json.load(open('cdk-outputs.json'));\
print(next(v['ApiUrl'] for v in d.values() if 'ApiUrl' in v))")

curl "$API_URL/health"                                   # public

TOKEN=$(python scripts/get_token.py -e me@example.com --outputs-file cdk-outputs.json)
curl -H "Authorization: Bearer $TOKEN" "$API_URL/account"

# Start a generation, then poll it.
JOB=$(curl -s -X POST "$API_URL/generate" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mood":"wound up after a long week","duration_minutes":10}' | jq -r .job_id)

curl -s -H "Authorization: Bearer $TOKEN" "$API_URL/jobs/$JOB" | jq
# once status is DONE, audio_url holds a 15-minute presigned link
```

`get_token.py` reads the password from `COGNITO_PASSWORD` or an interactive prompt — deliberately
never from argv, which would land it in shell history and the process list.

`cdk.json` runs `python app.py`, so the virtualenv must be active before any `cdk` command —
otherwise CDK fails with `No module named 'aws_cdk'`.

`cdk bootstrap` and `cdk deploy` are human-only — they spend money and touch live AWS resources.

## Repository layout

```
.prototools       Node version pin (22.21.0) for the CDK CLI
pyproject.toml    ruff + pytest config for the whole repo
infra/            CDK app, one stack per concern
  app.py          entry point; env selected via -c env=dev|prod
  stacks/         data_stack.py, auth_stack.py, api_stack.py,
                  pipeline_stack.py, billing_stack.py, frontend_stack.py
  tests/          CDK assertions; skipped when node is absent
backend/
  pyproject.toml  installable packages: shared, api, functions.*
  shared/         models.py, db.py (credit ledger), pipeline.py (step
                  contracts), tts/ (provider abstraction)
  api/            FastAPI app + Mangum handler, Dockerfile
  functions/      one folder per single-purpose Lambda
  tests/          pytest + moto
layers/           generated Lambda layers (gitignored)
assets/bgm/       background music synced to the audio bucket, mixed in-browser
scripts/
  get_token.py       prints a Cognito ID token for curl
  build_layers.sh    builds the shared layer (ffmpeg on request)
```
