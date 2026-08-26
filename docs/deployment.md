# Deployment runbook

Everything here is **human-only** (CLAUDE.md constraint 8): `cdk bootstrap`,
`cdk deploy`, secret creation and `make upload-bgm` all spend money or touch
live AWS. Claude runs `synth`, `diff`, lint and tests; a person runs this file.

Both environments live in one account, region `ap-southeast-2`, stacks named
`Meditation-<env>-<Concern>`. **Dev deploys itself from CI** on every merge to
main; **prod is entirely manual** — there is no prod CI job, deliberately.

## One-time, per account

Done once; both environments share them.

1. **CDK bootstrap** — `cd infra && npx cdk bootstrap`. (Known gap: this
   attaches `AdministratorAccess` to the CFN execution role; the scoped
   re-bootstrap is planned — see README "Known gaps".)
2. **Bedrock model access** — console → Bedrock → Model access → enable
   Amazon Nova Lite (`amazon.nova-lite-v1:0`) in ap-southeast-2. Listed ≠
   enabled; without this every generation fails with AccessDenied and rolls
   the credit back.
3. **CI deploy role** (dev automation) — the OIDC role
   `gha-meditation-dev-deploy` trusted for GitHub environment `dev`, plus the
   GitHub Actions variables `AWS_ACCOUNT_ID` and `AUDIO_PUBLIC_KEY_PEM`.

## Per environment, before the first deploy

The secret names below are the defaults and are **shared by dev and prod in
the same account** unless you point an env elsewhere with
`-c volcano_secret_name=` / `-c stripe_secret_name=`. Prod should use live
Stripe keys — if dev is on test keys, split the names.

| # | What | How | dev | prod |
|---|---|---|---|---|
| 1 | Volcano TTS secret `meditation/volcano-tts` | README "Credentials" — `{"api_key","app_id"}` | required | required |
| 2 | CloudFront signing keypair | README "frontend delivery": `openssl genrsa` → `cf-signing.pem` + `cf-signing.pub.pem`; private key into secret `meditation/cloudfront-signing-key` | required | required — generate a **separate** keypair |
| 3 | Stripe secret `meditation/stripe` | README "Stripe credentials" — `{"secret_key","webhook_secret"}`; `webhook_secret` comes from step 8, so create with a placeholder first | required | required, live keys |
| 4 | BGM tracks | `make upload-bgm ENV=<env>` — licensed tracks are not in git and not in the CDK asset | required | required |

## Dev

Normal path: **merge to main and CI does everything** — diff, deploy all
stacks, regenerate `frontend/.env.production` from stack outputs, build and
publish the PWA, invalidate CloudFront, smoke the API and the audio edge.

Manual path (first deploy, or debugging with CI out of the loop):

```bash
make diff ENV=dev                 # review; needs cf-signing.pub.pem in the repo root
make deploy ENV=dev               # layers build first; Docker must be running
make upload-bgm ENV=dev           # once, and after adding a track
```

After a manual deploy, the PWA still points at old outputs until you rebuild
it: regenerate `frontend/.env.production` from `cdk-outputs.json` (the CI step
"Frontend env from stack outputs" is the reference), then
`npm run build` + `aws s3 sync frontend/dist s3://<SiteBucketName> --delete`
+ a CloudFront invalidation — or just let the next CI run do it. For local
work against dev, `make dev` (Vite on :5173) is in the CORS allow-list; keep
`frontend/.env.local` in sync with the outputs, `VITE_JOB_TIMEOUT_MS`
included, whenever a deploy changes them.

A debug deploy can swap the Bedrock model without touching git:
`make deploy ENV=dev STACKS=Meditation-dev-Pipeline BEDROCK_MODEL=...` — any
merge to main reverts it.

## Prod

No CI. Every step is deliberate; the Makefile refuses prod without `CONFIRM=1`.

1. Prerequisites table above, prod column — including its own signing keypair
   and live Stripe keys.
2. **Choose the site origin.** `allowed_origins` is required context for
   prod; synth fails without it. A custom domain needs an existing Route53
   hosted zone, passed as `-c domain_name=` + `-c hosted_zone_id=`
   (frontend stack; the us-east-1 ACM cert is created for you). Without a
   domain the CloudFront default domain is the origin.
3. Review: `make diff ENV=prod ALLOWED_ORIGINS=https://app.example.com`.
4. Deploy: `make deploy ENV=prod CONFIRM=1 ALLOWED_ORIGINS=https://app.example.com`
   (add `AUDIO_PUB_KEY=<prod-key>.pem` if the prod public key is not at the
   default path). Outputs land in `cdk-outputs.json`.
5. **Publish the PWA** against prod outputs: write `frontend/.env.production`
   from `cdk-outputs.json` (same five variables as the CI step), then
   `npm run build`, `aws s3 sync frontend/dist s3://<SiteBucketName> --delete`,
   `aws cloudfront create-invalidation --distribution-id <SiteDistributionId> --paths '/*'`.
6. `make upload-bgm ENV=prod`.
7. **Register the Stripe webhook**: dashboard → Webhooks → add endpoint with
   the deployed webhook URL (billing stack output), events per README
   "Architecture — billing"; put the resulting `whsec_...` into
   `meditation/stripe` (`webhook_secret`) — the Lambda reads the secret per
   container, so no redeploy, but a cold start may lag a secret update.
8. Verify (step list below).

## Post-deploy verification, either env

```bash
curl -fsS "$API_URL/health"
curl -fsS -o /dev/null -D - \
  -H "Origin: https://smoke-test.invalid" -H "Priority: u=1, i" \
  "https://$AUDIO_DOMAIN/assets/bgm/silence.mp3" | grep -i "access-control-allow-origin: \*"
```

Then one real end-to-end check in the browser: sign up → generate (spends a
credit and bills Bedrock + TTS — this is the paid smoke, `make smoke CONFIRM=1`
automates it against dev) → the waiting screen has background music → the
player plays narration over the same unbroken track → for prod, a Stripe test
purchase and its webhook landing as an entitlement update.

Dev's cost guards, for reference: generation is capped at 1 minute outside
prod (`DURATION_MINUTES_OVERRIDE`), so a dev end-to-end run costs cents.

## The companion agent (`Meditation-<env>-Agent`)

Two functions off one image — `AgentFunction` (the hand-built engine, `AGENT_ENGINE=native`)
and `AgentFunctionLangGraph` (`AGENT_ENGINE=langgraph`) — each behind its own IAM-only Function
URL, fronted by the site distribution as `agent/*` and `agent-lg/*`. Both are zero-cost while
idle, so both are always deployed; the PWA uses the native one unless opened as
`/companion?engine=langgraph`. A session is pinned to the engine that opened it: sent to the
other function it is refused with 409 `wrong_engine`.

Deployed with everything else by `make deploy ENV=<env>`; on its own, deploy the agent stack
**and** the frontend stack, because the site distribution's two behaviours and the invoke
permissions live in the latter:

```bash
make deploy ENV=dev STACKS="Meditation-dev-Agent Meditation-dev-Frontend"
```

The first deploy pushes the runner image (~280 MB, `backend/agent_runner/Dockerfile`) to the CDK
bootstrap ECR repository and takes a few minutes; later ones push layers only. Nothing in the
stack runs while idle, so a deployed agent costs nothing until someone talks to it.

Model override for a session of debugging, same shape as `BEDROCK_MODEL`:

```bash
make deploy ENV=dev STACKS=Meditation-dev-Agent AGENT_MODEL=au.anthropic.claude-haiku-4-5-20251001-v1:0
```

`us.`, `eu.`, `apac.` and `global.` profiles are refused at synth: the conversation stays in
Australia.

**Concurrency ceiling.** The functions' reserved concurrency (the agent's cost ceiling) is off
by default; when set, the number is split between the two functions (the native engine gets
the larger half of an odd total; fewer than 2 fails the synth), because it is one account quota
either way. It is off by default: Lambda refuses a reservation that would leave the account under 10 unreserved
executions, and a new account's whole concurrency quota is 10 -- which already caps the function
at 10. Once the quota has been raised (Service Quotas → Lambda → *Concurrent executions*), turn
the ceiling on:

```bash
make deploy ENV=dev STACKS=Meditation-dev-Agent AGENT_CONCURRENCY=10
```

### Verifying

```bash
SITE=https://<SiteUrl>        # Frontend stack output
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$SITE/agent/sessions" \
  -H "x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# 401: the behaviour reaches the function, which wants an ID token (in X-Id-Token -- the OAC
#      signature overwrites Authorization, so a bearer token sent that way never arrives)

curl -s -o /dev/null -w '%{http_code}\n' -X POST "$(aws cloudformation describe-stacks \
  --stack-name Meditation-dev-Agent --query "Stacks[0].Outputs[?OutputKey=='AgentFunctionUrl'].OutputValue" \
  --output text)agent/sessions"
# 403: the Function URL itself is IAM-only; nobody but CloudFront gets past it

# The same two checks for the LangGraph function: agent-lg/* through the site (401), and
# the AgentLangGraphFunctionUrl output directly (403).
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$SITE/agent-lg/sessions" \
  -H "x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

**Both engines, end to end.** With a Pro account, open `/companion` and `/companion?engine=langgraph`
in turn and take each to a proposal (confirm one, Start over on the other — a conversation
costs nothing). CloudWatch → Dashboards → `Meditation-<env>-Agent` then shows a point on each
engine's `TurnLatency` line, with tokens and cache reads beside them; that dashboard (the
account's first, so free) is where the comparison note's charts come from.

With a Pro account's ID token, the sequence in README ("Calling the deployed API") opens a
session, streams a turn and confirms a proposal; a minute later the job is DONE like any other.
If a `/agent/*` request ever comes back as a 200 `index.html` page, a distribution-wide error
mapping has crept back in: SPA routing is a viewer-request rewrite on the site behaviour only,
precisely so the runner's 401/403/404 reach the browser as themselves. When in doubt read
`x-cache: Error from cloudfront` and the function's log group, not the status code. An empty log
group means the Function URL refused CloudFront before Lambda ran (both `lambda:InvokeFunctionUrl`
and `lambda:InvokeFunction` must be granted -- the frontend stack does both).

Metrics land in CloudWatch under `Meditation/Agent` (`AgentTurns`, `TurnLatency`, token counts,
`AgentTurnErrors` by reason) with one line of JSON per turn in the function's log group — ids and
counts only, never a word of the conversation.

### Cost

Nothing runs while idle: no VPC, no load balancer, no provisioned concurrency, and the Function
URL bills per invocation. What a conversation costs is the model, plus a rounding error of
Lambda.

**The model, estimated** (`docs/agent-runner-plan.md` §7). Assumptions: a full 12-turn session
accumulates about 40k input tokens with roughly 70% of them served from the prompt cache (cache
reads bill at a tenth of the input rate) and produces about 4k output tokens. Bedrock on-demand
prices in `ap-southeast-2`, per million tokens in / out:

| Model | Per full session | 30 sessions (one Pro account's monthly cap) |
|---|---|---|
| **Nova Lite** (`amazon.nova-lite-v1:0`, $0.06 / $0.24) — the default | ≈ $0.004 | ≈ $0.10 |
| Claude Haiku 4.5 (`au.` profile, $1 / $5) | ≈ $0.035 | ≈ $1 |
| Claude Sonnet 4.6 (`au.` profile, $3 / $15) | ≈ $0.10–0.12 | ≈ $3–4 |

Most sessions are shorter than twelve turns, so these are ceilings. The cap is enforced in code
— `AGENT_SESSIONS_PER_MONTH = 30` (`backend/shared/models.py`), 12 turns and 4 tool rounds a
turn (`backend/agent/budget.py`) — so the Bedrock exposure per Pro account per month is the
right-hand column, whatever the listener does. That figure is a **pricing input**: Pro's price
has to cover it plus twenty generations' script and narration, and the decision is a product
one, not made here.

**Lambda.** 512 MB for up to 120 s a turn (`AGENT_MEMORY_MB`, `AGENT_TIMEOUT`,
`infra/stacks/agent_stack.py`). A turn observed on dev takes 3–30 s depending on tool rounds;
at $0.0000166667 per GB-second, thirty seconds of 0.5 GB is $0.00025, so a twelve-turn session
is on the order of a third of a cent — an order of magnitude below the model even on Nova Lite.
The container image sits in the CDK bootstrap ECR repository (~280 MB, a few cents a month).

**Measured.** Every turn adds its token counts to the session header (`usage_input_tokens`,
`usage_output_tokens`, `usage_cache_read_tokens` on `AGENT#<session>`, summed by `commit_turn`)
and emits them as metrics: namespace `Meditation/Agent`, dimension `Engine`, names
`InputTokens`, `OutputTokens`, `CacheReadTokens`, `TurnLatency`, `AgentTurns`, `ToolErrors`,
`AgentTurnErrors`, `AgentConfirmations` (`backend/agent_runner/turns.py`,
`backend/agent_runner/metrics.py`). A month's real numbers:

- CloudWatch → Metrics → `Meditation/Agent` → `SUM(InputTokens)`, `SUM(CacheReadTokens)`,
  `SUM(OutputTokens)` over the month, times the model's prices; `SUM(AgentTurns)` divided by
  `SUM(AgentConfirmations)` says how many turns a meditation takes to reach.
- Cost Explorer → filter *Service = Amazon Bedrock*, group by *Usage type* (the model shows in the
  usage type string), and *Service = AWS Lambda* with the function name tag — the two together are
  the agent's bill; nothing else in the account is attributable to it.
- The cache-hit rate to watch is `CacheReadTokens / (InputTokens + CacheReadTokens)`; the
  estimate above assumes about 70%, which the eval runs on Nova Lite have shown.

**The evals.** `make agent-evals CONFIRM=1` runs the twenty cases in
`backend/tests/agent/evals/` — about sixty model calls (`run.py`) — and prints each case's
tokens; at Nova Lite's prices a full run is on the order of a cent, on Claude Haiku a few tens
of cents. They are the one Bedrock spend outside a conversation and are never run by CI.
