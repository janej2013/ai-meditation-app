# AI Meditation App

Users describe how they feel; the system generates a personalised meditation script with an LLM,
synthesises it to speech, mixes background music, and delivers a streamable audio file. Freemium:
one free generation on signup, then paid credits via Stripe.

Region `ap-southeast-2` (Sydney). See [CLAUDE.md](CLAUDE.md) for the full stack and constraints.

## Current status

Milestone 2 complete: Cognito auth, the HTTP API, and the FastAPI Lambda.

| Milestone | Scope | Status |
|---|---|---|
| 1 | `infra/` skeleton + `data_stack` | done |
| 2 | `auth_stack` + `api_stack` | done |
| 3 | `pipeline_stack` (Polly placeholder) | not started |
| 4 | Volcano TTS provider | not started |
| 5 | `billing_stack` (Stripe) | not started |
| 6 | `frontend_stack` + PWA | not started |

## Known gaps

Deliberate deferrals, recorded so they read as decisions rather than oversights.

- **The CDK CloudFormation execution role holds `AdministratorAccess`.** That is what
  `cdk bootstrap` attaches when `--cloudformation-execution-policies` is not given. Scoping it
  down means enumerating every permission this project actually needs — DynamoDB, S3, Lambda,
  Cognito, API Gateway, Step Functions, Bedrock, Polly, Secrets Manager — and that list keeps
  moving while stacks are still being added. Planned for after milestone 5, by re-running
  `cdk bootstrap --cloudformation-execution-policies <scoped-policy-arn>`.

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
expire after 90 days. No CORS: delivery is via CloudFront signed URLs, so the bucket stays private
and audio bytes never stream through Lambda.

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

## Local setup

Development targets Linux — natively or through WSL — because that is what Lambda runs and what
the container image for the API is built against.

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
pytest backend/tests -q                 # tests
cd infra && npx cdk synth               # dev (default)
cd infra && npx cdk synth -c env=prod -c allowed_origins=https://app.example.com
cd infra && npx cdk diff
```

`allowed_origins` is required for `env=prod` and synth fails without it — falling back to a
localhost CORS origin in a real deployment would be worse than a loud error. Dev defaults to
`http://localhost:5173` (Vite).

### Calling the deployed API

```bash
cd infra && npx cdk deploy --all --outputs-file ../cdk-outputs.json   # human-only

export API_URL=$(python -c "import json;d=json.load(open('cdk-outputs.json'));\
print(next(v['ApiUrl'] for v in d.values() if 'ApiUrl' in v))")

curl "$API_URL/health"                                   # public

TOKEN=$(python scripts/get_token.py -e me@example.com --outputs-file cdk-outputs.json)
curl -H "Authorization: Bearer $TOKEN" "$API_URL/account"
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
  stacks/         data_stack.py, auth_stack.py, api_stack.py
                  (+ pipeline, billing, frontend to come)
backend/
  pyproject.toml  installable packages: shared, api, functions.*
  shared/         models.py (Pydantic contracts), db.py (credit ledger)
  api/            FastAPI app + Mangum handler, Dockerfile
  functions/      one folder per single-purpose Lambda
  tests/          pytest + moto
scripts/
  get_token.py    prints a Cognito ID token for curl
```
