# Repeatable entry points for everything this repo is checked with.
#
# Each target encodes the prerequisites that are easy to get wrong by hand and
# have cost time before: which directory a command must run from, that the CDK
# CLI has to come from infra/node_modules rather than whatever npx downloads,
# and that Python tooling lives in a virtualenv outside the checkout.
#
# WSL/Linux only, per CLAUDE.md -- the Lambda runtime, the layer build and the
# container images all assume a Linux toolchain.

VENV ?= $(HOME)/.venvs/meditation/bin
PY := $(VENV)/python
RUFF := $(VENV)/ruff

# Deployment selectors: `make diff ENV=prod`, `make deploy STACKS=Meditation-dev-Api`.
# Empty STACKS means every stack: `cdk diff` does that natively (and rejects
# --all with a warning), while `cdk deploy` needs the explicit flag -- so only
# the deploy recipe substitutes it.
ENV ?= dev
STACKS ?=

# The CloudFront URL-signing public key (README: frontend delivery). A deploy
# that omits it silently removes the key group and leaves jobs/* unsigned --
# the compound gap in README Known gaps -- so deploy refuses to run without
# the file unless ALLOW_UNSIGNED=1 states that is intentional.
AUDIO_PUB_KEY ?= cf-signing.pub.pem

.DEFAULT_GOAL := help
.PHONY: help check lint test synth layers diff deploy fe-check fe-lint fe-test fe-build \
        e2e e2e-ui e2e-install e2e-auth smoke dev

help: ## List the targets
	@grep -hE '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------------
# Backend and infrastructure
# ----------------------------------------------------------------------

check: lint test synth fe-check ## Everything CLAUDE.md asks for before calling a task done

lint: ## ruff check + format --check
	$(RUFF) check .
	$(RUFF) format --check .

test: ## Backend and infra unit tests
	$(PY) -m pytest -q

synth: ## cdk synth (dev). Needs no AWS credentials and no Docker.
	# cdk.json runs a bare `python app.py`; putting the venv first on PATH
	# means this works from a shell that never activated it.
	cd infra && PATH="$(VENV):$$PATH" CDK_DOCKER=/bin/false npm run synth --silent

layers: ## Build the shared Lambda layer (synth only warns when it is missing)
	PATH="$(VENV):$$PATH" scripts/build_layers.sh

diff: ## cdk diff $(ENV) against what is deployed. Needs AWS credentials.
	cd infra && PATH="$(VENV):$$PATH" npm run diff --silent -- -c env=$(ENV) $(STACKS)

deploy: layers ## cdk deploy $(ENV) -- HUMAN-ONLY (CLAUDE.md 8): spends money, needs Docker
ifeq ($(ENV),prod)
ifndef CONFIRM
	@echo "Refusing to deploy prod without confirmation."
	@echo "Re-run as:  make deploy ENV=prod CONFIRM=1"
	@exit 1
endif
endif
ifeq ($(wildcard $(AUDIO_PUB_KEY)),)
ifndef ALLOW_UNSIGNED
	@echo "Refusing to deploy: $(AUDIO_PUB_KEY) not found."
	@echo "Deploying without the signing public key removes the CloudFront key"
	@echo "group and leaves jobs/* unsigned (README: Known gaps). Generate it"
	@echo "(README: frontend delivery) or re-run with ALLOW_UNSIGNED=1."
	@exit 1
endif
endif
	# Depends on `layers`: a deploy without the shared layer succeeds and then
	# fails at runtime, which synth only warns about. Docker must be running --
	# the API Lambda is a container image built here. CDK's own approval prompt
	# for IAM changes is deliberately left on.
	# The key travels as a file path, not as inline PEM: `npm run` re-quotes
	# child arguments and flattens a multiline value to its first line, which
	# CloudFront rejects as an invalid key.
	cd infra && PATH="$(VENV):$$PATH" npm run cdk --silent -- deploy -c env=$(ENV) \
		$(if $(wildcard $(AUDIO_PUB_KEY)),-c audio_public_key_file="$(abspath $(AUDIO_PUB_KEY))",) \
		$(if $(STACKS),$(STACKS),--all)

# ----------------------------------------------------------------------
# Frontend
# ----------------------------------------------------------------------

fe-check: fe-lint fe-test fe-build ## Frontend lint, unit tests and production build

fe-lint:
	cd frontend && npm run lint

fe-test:
	cd frontend && npm test

fe-build:
	cd frontend && npm run build

# ----------------------------------------------------------------------
# End-to-end
#
# `e2e` stubs every network call, so it needs no AWS account, spends nothing
# and is deterministic. `smoke` is the opposite and is guarded accordingly.
# ----------------------------------------------------------------------

e2e-install: ## One-time: fetch the browser and the system libraries it links
	# --with-deps installs the shared libraries Chromium needs (libnspr4 and
	# friends), which a clean WSL image does not carry. It shells out to apt,
	# so this is the one target that asks for sudo.
	cd frontend && npx playwright install --with-deps chromium

e2e: ## Browser E2E against stubbed APIs (no AWS, no cost)
	# --project=stubbed, not the default all-projects run: the day a
	# *.smoke.spec.ts exists, a bare `playwright test` would drive the deployed
	# stack and spend money -- from the one target whose help text says it never
	# does, bypassing the CONFIRM gate below.
	cd frontend && npx playwright test --project=stubbed

e2e-ui: ## The same suite in Playwright's inspector, for debugging a failure
	cd frontend && npx playwright test --ui --project=stubbed

e2e-auth: ## How to refresh the signed-in storage fixture
	@echo "Cognito uses SRP, so a session is captured rather than faked:"
	@echo
	@echo "  1. make dev, then sign in normally in the browser"
	@echo "  2. DevTools console:  copy(JSON.stringify(localStorage))"
	@echo "  3. Paste into frontend/e2e/.auth/local-storage.json"
	@echo
	@echo "The file is gitignored: it holds real tokens for a real account."

smoke: ## Tier A against the deployed dev stack -- SPENDS A CREDIT AND MONEY
ifndef CONFIRM
	@echo "Refusing to run: this drives the deployed dev stack, consumes a"
	@echo "generation credit and bills Bedrock and TTS for real."
	@echo "Re-run as:  make smoke CONFIRM=1"
	@exit 1
endif
	cd frontend && npx playwright test --project=smoke

# ----------------------------------------------------------------------
# Development
# ----------------------------------------------------------------------

.PHONY: dev
dev: ## Vite dev server on the port the API's CORS allow-list names
	cd frontend && npm run dev -- --port 5173 --strictPort
