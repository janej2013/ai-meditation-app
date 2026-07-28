# AI Meditation App

Users describe how they feel; the system generates a personalised meditation script with an LLM,
synthesises it to speech, mixes background music, and delivers a streamable audio file. Freemium:
one free generation on signup, then paid credits via Stripe.

Region `ap-southeast-2` (Sydney). See [CLAUDE.md](CLAUDE.md) for the full stack and constraints.

## Current status

Milestone 1 complete: CDK skeleton, data stack, and the shared credit ledger.

| Milestone | Scope | Status |
|---|---|---|
| 1 | `infra/` skeleton + `data_stack` | done |
| 2 | `auth_stack` + `api_stack` | not started |
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
cd infra && npx cdk synth -c env=prod
cd infra && npx cdk diff
```

`cdk.json` runs `python app.py`, so the virtualenv must be active before any `cdk` command —
otherwise CDK fails with `No module named 'aws_cdk'`.

`cdk bootstrap` and `cdk deploy` are human-only — they spend money and touch live AWS resources.

## Repository layout

```
.prototools       Node version pin (22.21.0) for the CDK CLI
infra/            CDK app, one stack per concern
  app.py          entry point; env selected via -c env=dev|prod
  stacks/         data_stack.py (+ auth, api, pipeline, billing, frontend to come)
backend/
  pyproject.toml  installable `shared` package (ships as a Lambda layer)
  shared/         models.py (Pydantic contracts), db.py (credit ledger)
  tests/          pytest + moto
```
